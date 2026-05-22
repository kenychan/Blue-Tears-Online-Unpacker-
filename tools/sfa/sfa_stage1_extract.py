#!/usr/bin/env python3
"""
Stage-1 resource extractor for the QQxj/Nextsoft SFA resource volumes.

This deliberately does not require the SFAi index to be fully understood yet.
It works directly from the numbered data volumes:

  - carves directly visible WAV, OGG, DDS, NIF, and KFM assets
  - splits SFA-entry payloads that start with the known 16-byte entry header
  - prints a compact archive report in --dry-run mode

The split SFA-entry payloads are not decoded by this script. They are useful
inputs for the next pass, where the per-entry compression/encryption wrapper
needs to be solved.
"""

from __future__ import annotations

import argparse
import os
import struct
from dataclasses import dataclass
from pathlib import Path


SFA_ENTRY_PREFIX = bytes.fromhex("f3 74 1b 0b fd 16 e9 4c 4c 1a 94 e8")

SIGNATURES = {
    "nif": b"Gamebryo File Format",
    "kfm": b";Gamebryo KFM File Version",
    "dds": b"DDS ",
    "wav": b"RIFF",
    "ogg": b"OggS",
    "sfa": SFA_ENTRY_PREFIX,
}


@dataclass(frozen=True)
class Hit:
    kind: str
    offset: int


def read_volumes(root: Path, archive: str) -> bytes:
    parts = sorted(root.glob(f"{archive}.[0-9][0-9][0-9]"))
    if not parts:
        raise FileNotFoundError(f"no numbered volumes found for {archive!r} in {root}")
    return b"".join(p.read_bytes() for p in parts)


def find_all(buf: bytes, needle: bytes) -> list[int]:
    out: list[int] = []
    pos = 0
    while True:
        pos = buf.find(needle, pos)
        if pos < 0:
            return out
        out.append(pos)
        pos += 1


def all_hits(buf: bytes) -> list[Hit]:
    hits: list[Hit] = []
    for kind, sig in SIGNATURES.items():
        for off in find_all(buf, sig):
            hits.append(Hit(kind, off))
    return sorted(hits, key=lambda h: (h.offset, h.kind))


def next_boundary(hits: list[Hit], start_index: int, end: int) -> int:
    cur = hits[start_index].offset
    for hit in hits[start_index + 1:]:
        if hit.offset > cur:
            return hit.offset
    return end


def riff_length(buf: bytes, off: int) -> int | None:
    if off + 12 > len(buf) or buf[off:off + 4] != b"RIFF":
        return None
    size = struct.unpack_from("<I", buf, off + 4)[0] + 8
    if size < 12 or off + size > len(buf):
        return None
    if buf[off + 8:off + 12] != b"WAVE":
        return None
    return size


def dds_length(buf: bytes, off: int) -> int | None:
    if off + 128 > len(buf) or buf[off:off + 4] != b"DDS ":
        return None
    if struct.unpack_from("<I", buf, off + 4)[0] != 124:
        return None

    height = struct.unpack_from("<I", buf, off + 12)[0]
    width = struct.unpack_from("<I", buf, off + 16)[0]
    mipmaps = struct.unpack_from("<I", buf, off + 28)[0] or 1
    fourcc = buf[off + 84:off + 88]
    rgb_bits = struct.unpack_from("<I", buf, off + 88)[0]

    if not height or not width or height > 16384 or width > 16384:
        return None

    block_bytes = {b"DXT1": 8, b"DXT3": 16, b"DXT5": 16}.get(fourcc)
    total = 128
    w, h = width, height
    for _ in range(mipmaps):
        if block_bytes:
            total += max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * block_bytes
        elif rgb_bits:
            total += max(1, w) * max(1, h) * max(1, rgb_bits // 8)
        else:
            return None
        w = max(1, w // 2)
        h = max(1, h // 2)

    if off + total > len(buf):
        return None
    return total


def ogg_streams(buf: bytes) -> list[tuple[int, int]]:
    streams: list[tuple[int, int]] = []
    active: dict[int, int] = {}
    pos = 0

    while True:
        off = buf.find(b"OggS", pos)
        if off < 0:
            break
        if off + 27 > len(buf):
            break

        header_type = buf[off + 5]
        serial = struct.unpack_from("<I", buf, off + 14)[0]
        seg_count = buf[off + 26]
        page_end = off + 27 + seg_count
        if page_end > len(buf):
            pos = off + 4
            continue
        page_end += sum(buf[off + 27:off + 27 + seg_count])
        if page_end > len(buf):
            pos = off + 4
            continue

        if header_type & 0x02:
            active[serial] = off
        if header_type & 0x04 and serial in active:
            streams.append((active.pop(serial), page_end))
        pos = page_end

    return sorted(set(streams))


def safe_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def extract_archive(root: Path, archive: str, out_root: Path, dry_run: bool) -> None:
    buf = read_volumes(root, archive)
    hits = all_hits(buf)
    counts: dict[str, int] = {}
    for hit in hits:
        counts[hit.kind] = counts.get(hit.kind, 0) + 1

    print(f"{archive}: {len(buf):,} bytes")
    print("  signatures:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none")
    if dry_run:
        for hit in hits[:12]:
            print(f"  first {hit.kind:3s} at 0x{hit.offset:08x}")
        return

    archive_out = out_root / archive

    # OGG needs page-aware carving; a raw signature count is page count, not file count.
    for idx, (start, end) in enumerate(ogg_streams(buf)):
        safe_write(archive_out / "ogg" / f"{archive}_{idx:05d}.ogg", buf[start:end])

    for i, hit in enumerate(hits):
        start = hit.offset
        end = next_boundary(hits, i, len(buf))

        if hit.kind == "ogg":
            continue
        if hit.kind == "wav":
            size = riff_length(buf, start)
            if size is None:
                continue
            end = start + size
        elif hit.kind == "dds":
            size = dds_length(buf, start)
            if size is not None:
                end = start + size
        elif hit.kind == "sfa":
            # Require the full 16-byte entry marker. Byte 12 varies, the rest is fixed.
            if start + 16 > len(buf):
                continue
            marker = buf[start:start + 16]
            if marker[:12] != SFA_ENTRY_PREFIX or marker[13:16] != bytes.fromhex("ac c8 16"):
                continue

        ext = "bin" if hit.kind == "sfa" else hit.kind
        safe_write(archive_out / hit.kind / f"{archive}_{hit.offset:010d}.{ext}", buf[start:end])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("archives", nargs="*", help="archive base names, e.g. Sound World UI")
    ap.add_argument("--root", default=".", help="directory containing .SFAi and numbered volumes")
    ap.add_argument("--out", default="extracted_stage1", help="output directory")
    ap.add_argument("--dry-run", action="store_true", help="report only; do not write files")
    args = ap.parse_args()

    root = Path(args.root)
    archives = args.archives
    if not archives:
        archives = sorted({p.stem for p in root.glob("*.SFAi")})

    for archive in archives:
        try:
            extract_archive(root, archive, Path(args.out), args.dry_run)
        except FileNotFoundError as exc:
            print(f"{archive}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
