import argparse
import bisect
from pathlib import Path

import capstone
import pefile


def main() -> int:
    parser = argparse.ArgumentParser(description="Find x64 instructions referencing a member offset.")
    parser.add_argument("pe", type=Path)
    parser.add_argument("offset", type=lambda value: int(value, 0))
    args = parser.parse_args()

    pe = pefile.PE(str(args.pe), fast_load=False)
    image = pe.get_memory_mapped_image()
    functions = sorted(
        (item.struct.BeginAddress, item.struct.EndAddress)
        for item in pe.DIRECTORY_ENTRY_EXCEPTION
    )
    starts = [begin for begin, _ in functions]
    text_section = next(
        section for section in pe.sections if section.Name.rstrip(b"\0") == b".text"
    )
    begin = text_section.VirtualAddress
    end = begin + max(text_section.Misc_VirtualSize, text_section.SizeOfRawData)
    disassembler = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    disassembler.skipdata = True
    needle = f"0x{args.offset:x}"
    found = set()
    for address, _, mnemonic, operands in disassembler.disasm_lite(image[begin:end], begin):
        if needle not in operands or "[" not in operands:
            continue
        index = bisect.bisect_right(starts, address) - 1
        function = functions[index] if index >= 0 and address < functions[index][1] else (0, 0)
        row = (function[0], function[1], address, mnemonic, operands)
        if row in found:
            continue
        found.add(row)
        print(
            f"function={function[0]:#x}..{function[1]:#x} "
            f"instruction={address:#x} {mnemonic} {operands}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
