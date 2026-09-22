import argparse
import re
from pathlib import Path

import capstone
import pefile


PRINTABLE = re.compile(rb"[\x20-\x7e]{3,}\x00")
RIP_REF = re.compile(r"\[rip\s*([+-])\s*(0x[0-9a-f]+)\]", re.IGNORECASE)


def main() -> int:
    parser = argparse.ArgumentParser(description="Disassemble one x64 PE runtime function")
    parser.add_argument("pe", type=Path)
    parser.add_argument("rva", type=lambda value: int(value, 0))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--size", type=lambda value: int(value, 0))
    args = parser.parse_args()

    pe = pefile.PE(str(args.pe), fast_load=False)
    image = pe.get_memory_mapped_image()
    boundary = next(
        (
            (item.struct.BeginAddress, item.struct.EndAddress)
            for item in pe.DIRECTORY_ENTRY_EXCEPTION
            if item.struct.BeginAddress <= args.rva < item.struct.EndAddress
        ),
        None,
    )
    if args.size:
        boundary = (args.rva, args.rva + args.size)
    if not boundary:
        raise SystemExit(f"runtime function not found for RVA {hex(args.rva)}")
    begin, end = boundary
    strings = {
        match.start(): match.group()[:-1].decode("ascii", "replace")
        for match in PRINTABLE.finditer(image)
    }
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    lines = [f"function {hex(begin)}..{hex(end)} size={end - begin}"]
    for address, size, mnemonic, operands in md.disasm_lite(image[begin:end], begin):
        comments = []
        for sign, value in RIP_REF.findall(operands):
            displacement = int(value, 16)
            target = address + size + (displacement if sign == "+" else -displacement)
            if target in strings:
                comments.append(f"{hex(target)} {strings[target]!r}")
        suffix = f" ; {' | '.join(comments)}" if comments else ""
        lines.append(f"{address:08x}  {mnemonic:<8} {operands}{suffix}")
    rendered = "\n".join(lines) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
