#!/usr/bin/env python3
"""
Build a manifest from decrypted Nextsoft .SFAi index files.

Findings encoded here:
  - .SFAi body XOR period is 300 bytes
  - index records are 276 bytes, after a small prelude
  - prelude length is len(decrypted_body) % 276
  - record bytes 0..255 hold an encoded path/name key
  - u32 fields at 256, 260, 268, and 272 are useful metadata
  - field_256 is the offset inside one numbered volume
  - field_260 is the stored entry span/size
  - field_268 is usually the numbered volume index

The encoded names are not decoded yet, so this emits stable synthetic names.
"""

from __future__ import annotations

import argparse
import csv
import os
import struct
from pathlib import Path


RECORD_SIZE = 276


def u32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def volume_paths(root: Path, archive: str) -> list[Path]:
    return sorted(root.glob(f"{archive}.[0-9][0-9][0-9]"))


def read_entry(volumes: list[Path], volume_index: int, offset: int, size: int) -> bytes:
    if volume_index < 0 or volume_index >= len(volumes):
        return b""
    path = volumes[volume_index]
    vol_size = path.stat().st_size
    if offset < 0 or size < 0 or offset > vol_size:
        return b""
    take = min(size, vol_size - offset)
    with path.open("rb") as f:
        f.seek(offset)
        return f.read(take)


def encoded_name_hex(record: bytes) -> str:
    raw = record[:256].rstrip(b"\x00")
    return raw.hex()


def iter_records(dec_body: bytes):
    prelude = len(dec_body) % RECORD_SIZE
    count = (len(dec_body) - prelude) // RECORD_SIZE
    for index in range(count):
        start = prelude + index * RECORD_SIZE
        rec = dec_body[start:start + RECORD_SIZE]
        yield {
            "index": index,
            "encoded_name_hex": encoded_name_hex(rec),
            "field_256": u32(rec, 256),
            "field_260": u32(rec, 260),
            "field_264": u32(rec, 264),
            "field_268": u32(rec, 268),
            "field_272": u32(rec, 272),
        }


def ext_from_magic(data: bytes) -> str:
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return ".wav"
    if data.startswith(b"OggS"):
        return ".ogg"
    if data.startswith(b"DDS "):
        return ".dds"
    if data.startswith(b"Gamebryo File Format"):
        return ".nif"
    if data.startswith(b";Gamebryo KFM File Version"):
        return ".kfm"
    if data.startswith(bytes.fromhex("f3 74 1b 0b fd 16 e9 4c 4c 1a 94 e8")):
        return ".sfaentry"
    return ".bin"


def build_manifest(root: Path, archive: str, out_dir: Path, split: bool) -> None:
    dec_path = root / f"{archive}.SFAi.dec"
    if not dec_path.exists():
        print(f"[!] {archive}: missing {dec_path}")
        return

    body = dec_path.read_bytes()
    rows = list(iter_records(body))
    volumes = volume_paths(root, archive)
    volume_sizes = [p.stat().st_size for p in volumes]

    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{archive}_manifest.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "archive",
                "index",
                "field_256",
                "field_260",
                "field_264",
                "volume_index",
                "field_268",
                "field_272",
                "offset_guess",
                "global_offset",
                "size_guess",
                "in_range",
                "magic_ext",
                "encoded_name_hex",
            ],
        )
        writer.writeheader()
        for row in rows:
            offset = row["field_256"]
            size = row["field_260"]
            volume_index = row["field_268"]
            global_offset = sum(volume_sizes[:volume_index]) + offset if 0 <= volume_index < len(volume_sizes) else -1
            in_range = bool(
                volumes
                and 0 <= volume_index < len(volumes)
                and size
                and offset + size <= volume_sizes[volume_index]
            )
            chunk = read_entry(volumes, volume_index, offset, min(size, 32)) if in_range else b""
            ext = ext_from_magic(chunk) if chunk else ""
            writer.writerow({
                "archive": archive,
                **row,
                "volume_index": volume_index,
                "offset_guess": offset,
                "global_offset": global_offset,
                "size_guess": size,
                "in_range": int(in_range),
                "magic_ext": ext,
            })

            if split and in_range:
                split_dir = out_dir / archive / "by_index"
                split_dir.mkdir(parents=True, exist_ok=True)
                out_name = f"{archive}_{row['index']:05d}_{volume_index:03d}_{offset:010d}{ext or '.bin'}"
                (split_dir / out_name).write_bytes(read_entry(volumes, volume_index, offset, size))

    in_range_count = sum(
        1
        for row in rows
        if volumes
        and 0 <= row["field_268"] < len(volumes)
        and row["field_260"]
        and row["field_256"] + row["field_260"] <= volume_sizes[row["field_268"]]
    )
    print(
        f"[+] {archive}: records={len(rows)} prelude={len(body) % RECORD_SIZE} "
        f"in_range={in_range_count}/{len(rows)} -> {csv_path}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("archives", nargs="*", help="archive base names")
    ap.add_argument("--root", default=".", help="directory with .SFAi.dec and volumes")
    ap.add_argument("--out", default="index_manifests", help="manifest/split output directory")
    ap.add_argument("--split", action="store_true", help="also write chunks using field_256/field_260")
    args = ap.parse_args()

    root = Path(args.root)
    archives = args.archives or sorted(
        p.name[:-len(".SFAi.dec")] for p in root.glob("*.SFAi.dec")
    )
    for archive in archives:
        build_manifest(root, archive, Path(args.out), args.split)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
