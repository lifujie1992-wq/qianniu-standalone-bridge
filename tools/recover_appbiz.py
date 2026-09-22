import argparse
import hashlib
import json
import re
import struct
from bisect import bisect_right
from pathlib import Path
from typing import Any

import capstone
import pefile


KEYWORDS = (
    "sendtextmsg",
    "groupsendtextmsg",
    "localsendmsg",
    "onsendnewmsgstart",
    "onshoprobotlocalsendmsgs",
    "messagebiz",
    "appmessageservice",
    "messageservice",
)

RTTI_TYPES = (
    ".?AVCMessageBiz@@",
    ".?AVIAppMessageService@@",
    ".?AVCAppMessageService@@",
)


def is_rtti_target(type_name: str) -> bool:
    if type_name in RTTI_TYPES:
        return True
    return (
        type_name.startswith(".?AV?$_Func_impl_no_alloc@")
        and ("??SendTextMsg@" in type_name or "??GroupSendTextMsg@" in type_name)
        and ("@CMessageBiz@@" in type_name or "@CAppMessageService@@" in type_name)
    )


def printable_strings(image: bytes, minimum: int = 4) -> list[tuple[int, str]]:
    pattern = re.compile(rb"[\x20-\x7e]{%d,}\x00" % minimum)
    return [
        (match.start(), match.group()[:-1].decode("ascii", "replace"))
        for match in pattern.finditer(image)
    ]


def runtime_functions(pe: pefile.PE) -> list[tuple[int, int]]:
    rows = sorted(
        (item.struct.BeginAddress, item.struct.EndAddress)
        for item in getattr(pe, "DIRECTORY_ENTRY_EXCEPTION", [])
        if item.struct.EndAddress > item.struct.BeginAddress
    )
    return rows


def containing_function(
    functions: list[tuple[int, int]], starts: list[int], rva: int
) -> tuple[int, int] | None:
    index = bisect_right(starts, rva) - 1
    if index >= 0 and functions[index][0] <= rva < functions[index][1]:
        return functions[index]
    return None


def disassembler() -> capstone.Cs:
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.skipdata = True
    return md


RIP_REF = re.compile(r"\[rip\s*([+-])\s*(0x[0-9a-f]+)\]", re.IGNORECASE)


def instruction_refs(address: int, size: int, operands: str) -> list[int]:
    refs = []
    for sign, raw_value in RIP_REF.findall(operands):
        displacement = int(raw_value, 16)
        refs.append(address + size + (displacement if sign == "+" else -displacement))
    return refs


def direct_call_target(mnemonic: str, operands: str) -> int | None:
    if mnemonic != "call" or not re.fullmatch(r"0x[0-9a-f]+", operands, re.IGNORECASE):
        return None
    return int(operands, 16)


def imported_modules(pe: pefile.PE) -> list[str]:
    return sorted(
        {
            entry.dll.decode("ascii", "replace")
            for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])
        },
        key=str.lower,
    )


def exports(pe: pefile.PE) -> list[dict[str, Any]]:
    directory = getattr(pe, "DIRECTORY_ENTRY_EXPORT", None)
    rows = []
    for symbol in getattr(directory, "symbols", []):
        rows.append({
            "name": symbol.name.decode("ascii", "replace") if symbol.name else None,
            "ordinal": symbol.ordinal,
            "rva": hex(symbol.address),
        })
    return rows


def section_contains(section: Any, rva: int) -> bool:
    start = section.VirtualAddress
    end = start + max(section.Misc_VirtualSize, section.SizeOfRawData)
    return start <= rva < end


def recover_rtti_vtables(
    pe: pefile.PE,
    image: bytes,
    strings: list[tuple[int, str]],
    functions: list[tuple[int, int]],
) -> list[dict[str, Any]]:
    base = pe.OPTIONAL_HEADER.ImageBase
    text_sections = [
        section for section in pe.sections
        if section.Characteristics & 0x20000000
    ]
    output = []
    starts = [row[0] for row in functions]
    strings_by_rva = {rva: value for rva, value in strings}
    md = disassembler()
    function_cache: dict[int, dict[str, Any]] = {}

    def annotate(function_rva: int) -> dict[str, Any]:
        if function_rva in function_cache:
            return function_cache[function_rva]
        boundary = containing_function(functions, starts, function_rva)
        if not boundary:
            result = {"boundary": None, "string_refs": [], "direct_calls": []}
            function_cache[function_rva] = result
            return result
        begin, end = boundary
        refs = []
        calls = []
        for address, size, mnemonic, operands in md.disasm_lite(image[begin:end], begin):
            for target in instruction_refs(address, size, operands):
                value = strings_by_rva.get(target)
                if value and len(value) <= 1000:
                    refs.append({"instruction_rva": hex(address), "rva": hex(target), "value": value})
            target = direct_call_target(mnemonic, operands)
            if target is not None:
                calls.append({"instruction_rva": hex(address), "target_rva": hex(target)})
        result = {
            "boundary": {"begin_rva": hex(begin), "end_rva": hex(end), "size": end - begin},
            "string_refs": refs,
            "direct_calls": calls,
        }
        function_cache[function_rva] = result
        return result

    for name_rva, type_name in strings:
        if not is_rtti_target(type_name) or name_rva < 16:
            continue
        type_descriptor_rva = name_rva - 16
        needle = struct.pack("<I", type_descriptor_rva)
        cursor = 0
        locators = []
        while True:
            hit = image.find(needle, cursor)
            if hit < 0:
                break
            cursor = hit + 1
            locator_rva = hit - 12
            if locator_rva < 0 or locator_rva + 24 > len(image):
                continue
            signature, offset, cd_offset, type_rva, class_rva, self_rva = struct.unpack_from(
                "<IIIIII", image, locator_rva
            )
            if signature != 1 or type_rva != type_descriptor_rva or self_rva != locator_rva:
                continue
            locator_va = base + locator_rva
            pointer = struct.pack("<Q", locator_va)
            vtables = []
            pointer_cursor = 0
            while True:
                pointer_hit = image.find(pointer, pointer_cursor)
                if pointer_hit < 0:
                    break
                pointer_cursor = pointer_hit + 1
                vtable_rva = pointer_hit + 8
                entries = []
                for index in range(256):
                    entry_offset = vtable_rva + index * 8
                    if entry_offset + 8 > len(image):
                        break
                    function_va = struct.unpack_from("<Q", image, entry_offset)[0]
                    function_rva = function_va - base
                    if not any(section_contains(section, function_rva) for section in text_sections):
                        break
                    entries.append({
                        "slot": index,
                        "function_rva": hex(function_rva),
                        **annotate(function_rva),
                    })
                if entries:
                    vtables.append({"vtable_rva": hex(vtable_rva), "entries": entries})
            locators.append({
                "locator_rva": hex(locator_rva),
                "object_offset": offset,
                "constructor_displacement_offset": cd_offset,
                "class_descriptor_rva": hex(class_rva),
                "vtables": vtables,
            })
        output.append({
            "type": type_name,
            "type_descriptor_rva": hex(type_descriptor_rva),
            "locators": locators,
        })
    return output


def recover(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    pe = pefile.PE(data=raw, fast_load=False)
    image = pe.get_memory_mapped_image()
    functions = runtime_functions(pe)
    starts = [row[0] for row in functions]
    all_strings = printable_strings(image)
    selected = [
        (rva, value)
        for rva, value in all_strings
        if any(keyword in value.lower() for keyword in KEYWORDS)
    ]
    selected_by_rva = {rva: value for rva, value in selected}
    rtti_vtables = recover_rtti_vtables(pe, image, all_strings, functions)
    vtable_targets = {
        int(vtable["vtable_rva"], 16): item["type"]
        for item in rtti_vtables
        for locator in item["locators"]
        for vtable in locator["vtables"]
    }

    text = next(section for section in pe.sections if section.Name.rstrip(b"\0") == b".text")
    text_rva = text.VirtualAddress
    text_size = max(text.Misc_VirtualSize, text.SizeOfRawData)
    md = disassembler()
    xrefs = []
    vtable_xrefs = []
    candidate_functions: set[tuple[int, int]] = set()
    for address, size, mnemonic, operands in md.disasm_lite(
        image[text_rva:text_rva + text_size], text_rva
    ):
        for target in instruction_refs(address, size, operands):
            vtable_type = vtable_targets.get(target)
            if vtable_type is not None:
                function = containing_function(functions, starts, address)
                vtable_xrefs.append({
                    "type": vtable_type,
                    "vtable_rva": hex(target),
                    "instruction_rva": hex(address),
                    "instruction": f"{mnemonic} {operands}",
                    "function": (
                        {"begin_rva": hex(function[0]), "end_rva": hex(function[1])}
                        if function else None
                    ),
                })
            label = selected_by_rva.get(target)
            if label is None:
                continue
            function = containing_function(functions, starts, address)
            if function:
                candidate_functions.add(function)
            xrefs.append({
                "string": label,
                "string_rva": hex(target),
                "instruction_rva": hex(address),
                "instruction": f"{mnemonic} {operands}",
                "function": (
                    {"begin_rva": hex(function[0]), "end_rva": hex(function[1])}
                    if function else None
                ),
            })

    function_reports = []
    for begin, end in sorted(candidate_functions):
        refs = []
        calls = []
        for address, size, mnemonic, operands in md.disasm_lite(image[begin:end], begin):
            for target in instruction_refs(address, size, operands):
                value = selected_by_rva.get(target)
                if value:
                    refs.append({
                        "instruction_rva": hex(address),
                        "target_rva": hex(target),
                        "string": value,
                    })
            target = direct_call_target(mnemonic, operands)
            if target is not None:
                calls.append({"instruction_rva": hex(address), "target_rva": hex(target)})
        function_reports.append({
            "begin_rva": hex(begin),
            "end_rva": hex(end),
            "size": end - begin,
            "sha256": hashlib.sha256(image[begin:end]).hexdigest(),
            "string_refs": refs,
            "direct_calls": calls,
        })

    return {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "machine": hex(pe.FILE_HEADER.Machine),
        "image_base": hex(pe.OPTIONAL_HEADER.ImageBase),
        "image_size": pe.OPTIONAL_HEADER.SizeOfImage,
        "timestamp": pe.FILE_HEADER.TimeDateStamp,
        "imports": imported_modules(pe),
        "exports": exports(pe),
        "rtti_vtables": rtti_vtables,
        "rtti_vtable_xrefs": vtable_xrefs,
        "selected_strings": [
            {"rva": hex(rva), "value": value} for rva, value in selected
        ],
        "xrefs": xrefs,
        "functions": function_reports,
    }


def compare(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    by_string: dict[str, dict[str, list[str]]] = {}
    for version, result in results.items():
        for xref in result["xrefs"]:
            function = xref.get("function") or {}
            begin = function.get("begin_rva")
            if not begin:
                continue
            by_string.setdefault(xref["string"], {}).setdefault(version, [])
            if begin not in by_string[xref["string"]][version]:
                by_string[xref["string"]][version].append(begin)
    return {"functions_by_referenced_string": by_string}


def main() -> int:
    parser = argparse.ArgumentParser(description="Recover AppBiz send-related functions")
    parser.add_argument("--old", required=True, type=Path)
    parser.add_argument("--new", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    results = {"9.77.01N": recover(args.old), "9.97.59N": recover(args.new)}
    payload = {"versions": results, "comparison": compare(results)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output.resolve()),
        "versions": {
            version: {
                "selected_strings": len(result["selected_strings"]),
                "xrefs": len(result["xrefs"]),
                "functions": len(result["functions"]),
                "exports": len(result["exports"]),
                "rtti_vtables": sum(
                    len(locator["vtables"])
                    for item in result["rtti_vtables"]
                    for locator in item["locators"]
                ),
            }
            for version, result in results.items()
        },
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
