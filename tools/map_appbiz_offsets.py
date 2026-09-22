"""Map the AppBiz text-send path from a known client build onto a new one.

Input: the AppBiz.dll of a build whose offsets are already known, plus the
AppBiz.dll of a new build. Output: the offsets the bridge needs for the new
build, cross-checked against the service vtable layout the Frida agent asserts
at attach time.

Three independent signals are combined, and every derived value is confirmed by
at least one of them:

1. Function anchor matching - take 16-byte runs around a known function RVA in
   the old image and look for a unique occurrence in the new .text section.
   Survives pointer/immediate churn between releases.
2. Vtable content matching - map the pointer array of a known vtable through
   step 1, then locate that pointer sequence in the new image. Tolerates a few
   entries that fail to map.
3. Relative layout - the ratios between the service vtable, its two secondary
   vtables and the MessageBiz vtable are stable across the known builds. Used
   only to propose candidates, which the content/shape checks must confirm.

No step assumes a constant image shift, so a release that inserts or removes
code still lands correctly.

Usage:
    py -3.10 tools/map_appbiz_offsets.py --old <old AppBiz.dll> --new <new AppBiz.dll> [--output out.json]
"""

from __future__ import annotations

import argparse
import collections
import json
import struct
import sys
from pathlib import Path

# Known-good offsets, keyed by SizeOfImage. Mirrors the HOOK_PROFILES table in
# appbiz_agent.js and the AppBizProfile table in native/appbiz_adapter.cpp.
KNOWN_PROFILES: dict[int, dict[str, object]] = {
    0x1F05000: {
        "version": "9.97.59N",
        "service_vtable": 0x18AF478,
        "secondary_vtable_1": 0x18AF5E0,
        "secondary_vtable_2": 0x18AF5F0,
        "message_biz_vtable": 0x18AD4F8,
        "service_get_new_msg": 0xA6C080,
        "service_on_message_arrive": 0xA6F9A0,
        "service_send_text": 0xA77FF0,
        "message_biz_send_text": 0xA59120,
    },
    0x1F17000: {
        "version": "9.97.74N",
        "service_vtable": 0x18BDEE8,
        "secondary_vtable_1": 0x18BE050,
        "secondary_vtable_2": 0x18BE060,
        "message_biz_vtable": 0x18BBF68,
        "service_get_new_msg": 0xA77B50,
        "service_on_message_arrive": 0xA7B470,
        "service_send_text": 0xA83AF0,
        "message_biz_send_text": 0xA64BF0,
    },
}

VTABLE_ENTRIES = 12
VTABLE_MATCH_ENTRIES = 6
VTABLE_SHAPE_ENTRIES = 8
OFFSET_GET_NEW_MSG = 0x18
OFFSET_SEND_TEXT = 0x90

DELTA_SECONDARY_1 = 0x168
DELTA_SECONDARY_2 = 0x178
DELTA_MESSAGE_BIZ_VTABLE = -0x1F80


class PeImage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = path.read_bytes()
        pe_offset = struct.unpack_from("<I", self.data, 0x3C)[0]
        section_count = struct.unpack_from("<H", self.data, pe_offset + 6)[0]
        optional_size = struct.unpack_from("<H", self.data, pe_offset + 20)[0]
        optional = pe_offset + 24
        magic = struct.unpack_from("<H", self.data, optional)[0]
        image_base_offset = optional + (24 if magic == 0x20B else 28)
        image_base_format = "<Q" if magic == 0x20B else "<I"
        self.image_base = struct.unpack_from(image_base_format, self.data, image_base_offset)[0]
        self.size_of_image = struct.unpack_from("<I", self.data, optional + 56)[0]
        table = optional + optional_size
        self.sections: list[tuple[str, int, int, int, int]] = []
        for index in range(section_count):
            offset = table + index * 40
            name = self.data[offset : offset + 8].split(b"\0", 1)[0].decode("ascii", "replace")
            virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from(
                "<IIII", self.data, offset + 8
            )
            self.sections.append((name, virtual_address, virtual_size, raw_offset, raw_size))

    @property
    def version_label(self) -> str | None:
        profile = KNOWN_PROFILES.get(self.size_of_image)
        return str(profile["version"]) if profile else None

    def section(self, name: str) -> tuple[int, bytes]:
        for section_name, virtual_address, _virtual_size, raw_offset, raw_size in self.sections:
            if section_name == name:
                return virtual_address, self.data[raw_offset : raw_offset + raw_size]
        raise KeyError(name)

    def rva_to_offset(self, rva: int) -> int:
        for _name, virtual_address, virtual_size, raw_offset, raw_size in self.sections:
            if virtual_address <= rva < virtual_address + max(virtual_size, raw_size):
                return raw_offset + rva - virtual_address
        raise ValueError(f"unmapped RVA: 0x{rva:X}")

    def offset_to_rva(self, offset: int) -> int:
        for _name, virtual_address, _virtual_size, raw_offset, raw_size in self.sections:
            if raw_offset <= offset < raw_offset + raw_size:
                return virtual_address + offset - raw_offset
        raise ValueError(f"unmapped file offset: 0x{offset:X}")

    def read_pointer(self, rva: int) -> int:
        return struct.unpack_from("<Q", self.data, self.rva_to_offset(rva))[0]

    def vtable_rvas(self, vtable_rva: int, count: int = VTABLE_ENTRIES) -> list[int]:
        """Read a vtable as a list of RVAs; stop at the first non-image pointer."""
        entries: list[int] = []
        for index in range(count):
            try:
                pointer = self.read_pointer(vtable_rva + index * 8)
            except ValueError:
                break
            if not (self.image_base <= pointer < self.image_base + self.size_of_image):
                break
            entries.append(pointer - self.image_base)
        return entries

    def vtable_shape_ok(self, rva: int, entries: int = VTABLE_SHAPE_ENTRIES) -> bool:
        """A believable vtable is a run of in-image pointers."""
        for index in range(entries):
            try:
                pointer = self.read_pointer(rva + index * 8)
            except ValueError:
                return False
            if not (self.image_base <= pointer < self.image_base + self.size_of_image):
                return False
        return True


def anchor_candidates(old: PeImage, new: PeImage, old_rva: int) -> list[tuple[int, int]]:
    """Locate the new-build counterpart of an old function RVA via 16-byte runs."""
    old_offset = old.rva_to_offset(old_rva)
    new_text_rva, new_text = new.section(".text")
    votes: collections.Counter[int] = collections.Counter()
    for relative in range(-256, 768, 4):
        start = old_offset + relative
        anchor = old.data[start : start + 16]
        if len(anchor) != 16 or anchor.count(0) > 12:
            continue
        position = new_text.find(anchor)
        if position < 0 or new_text.find(anchor, position + 1) >= 0:
            continue
        votes[new_text_rva + position - relative] += 1
    return votes.most_common(8)


def pointer_hits(image: PeImage, pointer: int) -> list[int]:
    pattern = struct.pack("<Q", pointer)
    positions: list[int] = []
    start = 0
    while True:
        found = image.data.find(pattern, start)
        if found < 0:
            return positions
        positions.append(found)
        start = found + 1


def locate_vtable_by_content(image: PeImage, mapped_by_index: dict[int, int]) -> list[tuple[int, int]]:
    """Find the pointer array whose entries match a partially mapped vtable."""
    indices = sorted(mapped_by_index)[:VTABLE_MATCH_ENTRIES]
    if len(indices) < 3:
        return []
    hits: dict[int, int] = {}
    for anchor_index in indices:
        for position in pointer_hits(image, image.image_base + mapped_by_index[anchor_index]):
            try:
                rva = image.offset_to_rva(position) - anchor_index * 8
            except ValueError:
                continue
            score = 0
            for index in indices:
                try:
                    pointer = image.read_pointer(rva + index * 8)
                except ValueError:
                    break
                if pointer == image.image_base + mapped_by_index[index]:
                    score += 1
            if score > hits.get(rva, 0):
                hits[rva] = score
    return sorted(hits.items(), key=lambda item: (-item[1], item[0]))


def main() -> int:
    parser = argparse.ArgumentParser(description="Map AppBiz offsets onto a new client build")
    parser.add_argument("--old", required=True, type=Path)
    parser.add_argument("--new", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    old = PeImage(args.old)
    new = PeImage(args.new)
    source = KNOWN_PROFILES.get(old.size_of_image)
    if source is None:
        print(f"no known profile for old image size 0x{old.size_of_image:X}", file=sys.stderr)
        return 2

    print(f"old  {args.old}")
    print(f"     version={source['version']} size_of_image=0x{old.size_of_image:X} base=0x{old.image_base:X}")
    print(f"new  {args.new}")
    print(f"     size_of_image=0x{new.size_of_image:X} base=0x{new.image_base:X}")

    cache: dict[int, tuple[int | None, list[tuple[int, int]]]] = {}

    def mapped(old_rva: int) -> int | None:
        if old_rva not in cache:
            cache[old_rva] = (lambda pair: (pair[0], pair[1]))(
                (lambda candidates: (candidates[0][0] if candidates else None, candidates))(
                    anchor_candidates(old, new, old_rva)
                )
            )
        return cache[old_rva][0]

    result: dict[str, object] = {
        "source_version": source["version"],
        "source_size_of_image": hex(old.size_of_image),
        "target_size_of_image": hex(new.size_of_image),
        "functions": {},
        "vtables": {},
        "checks": {},
    }
    functions: dict[str, int] = {}

    print("\nfunction anchors")
    for key in (
        "service_get_new_msg",
        "service_on_message_arrive",
        "service_send_text",
        "message_biz_send_text",
    ):
        old_rva = int(source[key])
        new_rva = mapped(old_rva)
        candidates = cache[old_rva][1]
        result["functions"][key] = {
            "old_rva": hex(old_rva),
            "new_rva": hex(new_rva) if new_rva is not None else None,
            "candidates": [[hex(rva), votes] for rva, votes in candidates],
        }
        if new_rva is None:
            print(f"   {key:28} 0x{old_rva:X} -> (anchor failed)")
            continue
        functions[key] = new_rva
        print(f"   {key:28} 0x{old_rva:X} -> 0x{new_rva:X}  votes={candidates[0][1]}")

    print("\nvtable content match")
    content_match: dict[str, int] = {}
    for key in ("service_vtable", "secondary_vtable_1", "secondary_vtable_2", "message_biz_vtable"):
        old_rva = int(source[key])
        entries = old.vtable_rvas(old_rva)
        mapped_by_index: dict[int, int] = {}
        for index, entry in enumerate(entries):
            entry_new = mapped(entry)
            if entry_new is not None:
                mapped_by_index[index] = entry_new
        hits = locate_vtable_by_content(new, mapped_by_index)
        result["vtables"][key] = {
            "old_rva": hex(old_rva),
            "entries": len(entries),
            "mapped_entries": len(mapped_by_index),
            "candidates": [[hex(rva), score] for rva, score in hits[:4]],
        }
        if hits:
            content_match[key] = hits[0][0]
            print(
                f"   {key:28} 0x{old_rva:X} -> 0x{hits[0][0]:X}  "
                f"entries={len(entries)} mapped={len(mapped_by_index)} matched={hits[0][1]}"
            )
        else:
            print(f"   {key:28} 0x{old_rva:X} -> (no content match)")

    print("\nservice vtable confirmation")
    service_vtable = content_match.get("service_vtable")
    if service_vtable is None and "service_get_new_msg" in functions:
        # Fall back to the pointer stores that hold the GetNewMsg slot: the
        # object's vtable pointer appears in the static data of the service.
        for position in pointer_hits(new, new.image_base + functions["service_get_new_msg"]):
            try:
                rva = new.offset_to_rva(position)
            except ValueError:
                continue
            candidate = rva - OFFSET_GET_NEW_MSG
            if candidate > 0 and new.vtable_shape_ok(candidate):
                service_vtable = candidate
                break
    if service_vtable is not None:
        print(f"   service_vtable = 0x{service_vtable:X}")
        if "service_get_new_msg" in functions:
            actual = new.read_pointer(service_vtable + OFFSET_GET_NEW_MSG) - new.image_base
            ok = actual == functions["service_get_new_msg"]
            result["checks"]["service_vtable+0x18=GetNewMsg"] = ok
            print(f"   check +0x{OFFSET_GET_NEW_MSG:X} == GetNewMsg : {ok}")
        if "service_send_text" not in functions:
            derived = new.read_pointer(service_vtable + OFFSET_SEND_TEXT) - new.image_base
            if new.image_base <= derived + new.image_base < new.image_base + new.size_of_image:
                functions["service_send_text"] = derived
                result["functions"]["service_send_text"] = {
                    "old_rva": hex(int(source["service_send_text"])),
                    "new_rva": hex(derived),
                    "derived_from": "service_vtable+0x90",
                }
                print(f"   service_send_text derived from service_vtable+0x{OFFSET_SEND_TEXT:X} -> 0x{derived:X}")
        if "service_send_text" in functions:
            actual = new.read_pointer(service_vtable + OFFSET_SEND_TEXT) - new.image_base
            ok = actual == functions["service_send_text"]
            result["checks"]["service_vtable+0x90=SendText"] = ok
            print(f"   check +0x{OFFSET_SEND_TEXT:X} == SendText : {ok}")

    print("\nrelative-layout candidates (must pass shape + content checks)")
    layout: dict[str, int] = {}
    if service_vtable is not None:
        proposals = {
            "secondary_vtable_1": service_vtable + DELTA_SECONDARY_1,
            "secondary_vtable_2": service_vtable + DELTA_SECONDARY_2,
            "message_biz_vtable": service_vtable + DELTA_MESSAGE_BIZ_VTABLE,
        }
        for key, candidate in proposals.items():
            shape_ok = new.vtable_shape_ok(candidate)
            content_ok = content_match.get(key) == candidate
            layout[key] = candidate
            result["vtables"].setdefault(key, {})["layout_candidate"] = hex(candidate)
            result["vtables"][key]["layout_shape_ok"] = shape_ok  # type: ignore[index]
            result["vtables"][key]["layout_matches_content"] = content_ok  # type: ignore[index]
            print(
                f"   {key:28} 0x{candidate:X}  shape_ok={shape_ok} "
                f"content_match={'yes' if content_ok else 'no'}"
            )

    def resolve(key: str) -> int | None:
        """Prefer the content match, fall back to a shape-confirmed layout candidate."""
        if key in content_match:
            return content_match[key]
        candidate = layout.get(key)
        if candidate is not None and new.vtable_shape_ok(candidate):
            return candidate
        return None

    vtables = {key: resolve(key) for key in (
        "service_vtable",
        "secondary_vtable_1",
        "secondary_vtable_2",
        "message_biz_vtable",
    )}
    if service_vtable is not None:
        vtables["service_vtable"] = service_vtable

    def h(value: int | None) -> str | None:
        return hex(value) if value is not None else None

    profile = {
        "size_of_image": hex(new.size_of_image),
        "serviceVtable": h(vtables.get("service_vtable")),
        "secondaryVtable1": h(vtables.get("secondary_vtable_1")),
        "secondaryVtable2": h(vtables.get("secondary_vtable_2")),
        "serviceGetNewMsg": h(functions.get("service_get_new_msg")),
        "serviceOnMessageArrive": h(functions.get("service_on_message_arrive")),
        "serviceSendText": h(functions.get("service_send_text")),
        "messageBizVtable": h(vtables.get("message_biz_vtable")),
        "messageBizSendText": h(functions.get("message_biz_send_text")),
    }
    result["profile"] = profile

    missing = [key for key, value in profile.items() if value is None]
    print("\nproposed profile")
    for key, value in profile.items():
        print(f"   {key} = {value}")
    if missing:
        print(f"\nwarning: unresolved fields: {', '.join(missing)}")
    else:
        print("\nall fields resolved")

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"written: {args.output}")
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
