#!/usr/bin/env python3
"""
Find ASCII/UTF-16 strings in a PE image and scan x86 code for direct references.

This is intentionally small and dependency-light except for capstone. It works
on normal PE files and on dumped in-memory PE images whose section table still
maps raw data to RVAs.
"""

from __future__ import annotations

import argparse
import re
import struct
from dataclasses import dataclass
from pathlib import Path

from capstone import Cs, CS_ARCH_X86, CS_MODE_32
from capstone.x86_const import X86_OP_IMM, X86_OP_MEM


@dataclass(frozen=True)
class Section:
    name: str
    va: int
    vsize: int
    raw: int
    raw_size: int
    chars: int


@dataclass(frozen=True)
class PEInfo:
    image_base: int
    sections: list[Section]


def u16(buf: bytes, off: int) -> int:
    return struct.unpack_from("<H", buf, off)[0]


def u32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def parse_pe(buf: bytes) -> PEInfo:
    if buf[:2] != b"MZ":
        raise ValueError("missing MZ header")
    pe_off = u32(buf, 0x3C)
    if buf[pe_off:pe_off + 4] != b"PE\0\0":
        raise ValueError("missing PE header")

    file_header = pe_off + 4
    section_count = u16(buf, file_header + 2)
    opt_size = u16(buf, file_header + 16)
    opt = file_header + 20
    magic = u16(buf, opt)
    if magic != 0x10B:
        raise ValueError(f"expected PE32 optional header, got 0x{magic:x}")
    image_base = u32(buf, opt + 28)

    sec_off = opt + opt_size
    sections: list[Section] = []
    for i in range(section_count):
        off = sec_off + i * 40
        name = buf[off:off + 8].rstrip(b"\0").decode("ascii", "replace")
        vsize = u32(buf, off + 8)
        va = u32(buf, off + 12)
        raw_size = u32(buf, off + 16)
        raw = u32(buf, off + 20)
        chars = u32(buf, off + 36)
        sections.append(Section(name, va, vsize, raw, raw_size, chars))
    return PEInfo(image_base, sections)


def rva_to_file(pe: PEInfo, rva: int) -> int | None:
    for sec in pe.sections:
        span = max(sec.vsize, sec.raw_size)
        if sec.va <= rva < sec.va + span:
            delta = rva - sec.va
            if delta < sec.raw_size:
                return sec.raw + delta
    return rva if rva < 0x1000 else None


def rva_to_memory_file(pe: PEInfo, rva: int) -> int | None:
    for sec in pe.sections:
        span = max(sec.vsize, sec.raw_size)
        if sec.va <= rva < sec.va + span:
            return rva
    return rva if rva < 0x1000 else None


def file_to_rva(pe: PEInfo, file_off: int) -> int | None:
    for sec in pe.sections:
        if sec.raw <= file_off < sec.raw + sec.raw_size:
            return sec.va + (file_off - sec.raw)
    return file_off if file_off < 0x1000 else None


def memory_file_to_rva(pe: PEInfo, file_off: int) -> int | None:
    for sec in pe.sections:
        span = max(sec.vsize, sec.raw_size)
        if sec.va <= file_off < sec.va + span:
            return file_off
    return file_off if file_off < 0x1000 else None


def section_bytes(buf: bytes, sec: Section) -> bytes:
    return buf[sec.raw:sec.raw + sec.raw_size]


def section_memory_bytes(buf: bytes, sec: Section) -> bytes:
    size = max(sec.vsize, sec.raw_size)
    return buf[sec.va:sec.va + size]


def is_exec(sec: Section) -> bool:
    return bool(sec.chars & 0x20000000)


def find_string_offsets(buf: bytes, text: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    needles = [("ascii", text.encode("ascii", "ignore"))]
    wide = text.encode("utf-16le")
    needles.append(("utf16", wide))
    for kind, needle in needles:
        if not needle:
            continue
        pos = 0
        while True:
            pos = buf.find(needle, pos)
            if pos < 0:
                break
            out.append((kind, pos))
            pos += 1
    return out


def find_related_strings(buf: bytes, center: int, radius: int = 512) -> list[str]:
    start = max(0, center - radius)
    end = min(len(buf), center + radius)
    chunk = buf[start:end]
    vals = []
    for m in re.finditer(rb"[ -~]{4,}", chunk):
        s = m.group(0).decode("ascii", "replace")
        if s not in vals:
            vals.append(s)
    return vals


def direct_xrefs(buf: bytes, pe: PEInfo, target_va: int, memory_layout: bool) -> list[tuple[int, str, str]]:
    md = Cs(CS_ARCH_X86, CS_MODE_32)
    md.detail = True
    refs: list[tuple[int, str, str]] = []
    for sec in pe.sections:
        if not is_exec(sec):
            continue
        code = section_memory_bytes(buf, sec) if memory_layout else section_bytes(buf, sec)
        base = pe.image_base + sec.va
        for ins in md.disasm(code, base):
            for op in ins.operands:
                if op.type == X86_OP_IMM and op.imm == target_va:
                    refs.append((ins.address, ins.mnemonic, ins.op_str))
                elif op.type == X86_OP_MEM and op.mem.disp == target_va:
                    refs.append((ins.address, ins.mnemonic, ins.op_str))
    return refs


def scan_image(path: Path, strings: list[str], memory_layout: bool) -> int:
    buf = path.read_bytes()
    pe = parse_pe(buf)
    print(f"{path}")
    print(f"  layout={'memory' if memory_layout else 'file'}")
    print(f"  image_base=0x{pe.image_base:08x}")
    print("  sections:")
    for sec in pe.sections:
        flag = "X" if is_exec(sec) else "-"
        print(
            f"    {sec.name:8s} {flag} va=0x{sec.va:08x} raw=0x{sec.raw:08x} "
            f"vsize=0x{sec.vsize:08x} raw_size=0x{sec.raw_size:08x}"
        )

    for text in strings:
        print(f"\n[string] {text!r}")
        found = find_string_offsets(buf, text)
        if not found:
            print("  not found")
            continue
        for kind, off in found:
            rva = memory_file_to_rva(pe, off) if memory_layout else file_to_rva(pe, off)
            va = pe.image_base + rva if rva is not None else None
            print(f"  {kind} file=0x{off:08x} rva={rva if rva is not None else None!s} va={va if va is not None else None!s}")
            nearby = find_related_strings(buf, off)
            for s in nearby[:20]:
                print(f"    near: {s}")
            if va is not None:
                refs = direct_xrefs(buf, pe, va, memory_layout)
                if refs:
                    for addr, mnem, op_str in refs[:50]:
                        print(f"    xref 0x{addr:08x}: {mnem} {op_str}")
                else:
                    print("    xref: no direct x86 immediate refs")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image", type=Path)
    ap.add_argument("strings", nargs="+")
    ap.add_argument("--memory-layout", action="store_true", help="input is a dumped PE image laid out by RVA")
    args = ap.parse_args()
    return scan_image(args.image, args.strings, args.memory_layout)


if __name__ == "__main__":
    raise SystemExit(main())
