#!/usr/bin/env python3
"""
Probe decrypted Nextsoft .SFAi records against numbered data volumes.

This does not solve the encoded filename field yet. It answers the next useful
question: which records have plausible volume-local archive offsets, what
visible payload is at that offset, and what magic-aware length can be recovered
without trusting the still-unclear name field.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import struct
from pathlib import Path

from sfa_stage1_extract import SFA_ENTRY_PREFIX, dds_length, riff_length


RECORD_SIZE = 276


def u32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def read_volumes(root: Path, archive: str) -> bytes:
    parts = sorted(root.glob(f"{archive}.[0-9][0-9][0-9]"))
    if not parts:
        raise FileNotFoundError(f"no numbered volumes found for {archive!r} in {root}")
    return b"".join(p.read_bytes() for p in parts)


def volume_table(root: Path, archive: str) -> list[tuple[int, int, Path]]:
    parts = sorted(root.glob(f"{archive}.[0-9][0-9][0-9]"))
    if not parts:
        raise FileNotFoundError(f"no numbered volumes found for {archive!r} in {root}")
    table = []
    cursor = 0
    for path in parts:
        size = path.stat().st_size
        table.append((cursor, cursor + size, path))
        cursor += size
    return table


def volume_paths(root: Path, archive: str) -> list[Path]:
    parts = sorted(root.glob(f"{archive}.[0-9][0-9][0-9]"))
    if not parts:
        raise FileNotFoundError(f"no numbered volumes found for {archive!r} in {root}")
    return parts


def table_size(table: list[tuple[int, int, Path]]) -> int:
    return table[-1][1] if table else 0


def read_at(table: list[tuple[int, int, Path]], offset: int, size: int) -> bytes:
    out = bytearray()
    remaining = size
    pos = offset
    for start, end, path in table:
        if pos >= end:
            continue
        if pos < start:
            break
        take = min(remaining, end - pos)
        with path.open("rb") as f:
            f.seek(pos - start)
            out += f.read(take)
        remaining -= take
        pos += take
        if remaining <= 0:
            break
    return bytes(out)


def read_volume_at(volumes: list[Path], volume_index: int, offset: int, size: int) -> bytes:
    if volume_index < 0 or volume_index >= len(volumes):
        return b""
    path = volumes[volume_index]
    vol_size = path.stat().st_size
    if offset < 0 or offset >= vol_size:
        return b""
    with path.open("rb") as f:
        f.seek(offset)
        return f.read(min(size, vol_size - offset))


def iter_records(dec_body: bytes):
    prelude = len(dec_body) % RECORD_SIZE
    count = (len(dec_body) - prelude) // RECORD_SIZE
    for index in range(count):
        start = prelude + index * RECORD_SIZE
        rec = dec_body[start:start + RECORD_SIZE]
        name_raw = rec[:256].rstrip(b"\x00")
        yield {
            "index": index,
            "encoded_name_len": len(name_raw),
            "encoded_name_hex": name_raw.hex(),
            "field_256": u32(rec, 256),
            "field_260": u32(rec, 260),
            "field_264": u32(rec, 264),
            "field_268": u32(rec, 268),
            "field_272": u32(rec, 272),
        }


def find_all(buf: bytes, needle: bytes) -> list[int]:
    out: list[int] = []
    pos = 0
    while True:
        pos = buf.find(needle, pos)
        if pos < 0:
            return out
        out.append(pos)
        pos += 1


def next_after(offsets: list[int], off: int, fallback: int) -> int:
    pos = bisect.bisect_right(offsets, off)
    return offsets[pos] if pos < len(offsets) else fallback


def build_magic_index(buf: bytes) -> dict[bytes, list[int]]:
    return {
        b"Gamebryo File Format": find_all(buf, b"Gamebryo File Format"),
        b";Gamebryo KFM File Version": find_all(buf, b";Gamebryo KFM File Version"),
    }


def magic_at(buf: bytes, off: int, magic_index: dict[bytes, list[int]] | None = None) -> tuple[str, int]:
    if off < 0 or off >= len(buf):
        return "", 0
    tail = buf[off:]
    if tail.startswith(b"Gamebryo File Format"):
        if magic_index is None:
            return ".nif", 0
        nxt = next_after(magic_index[b"Gamebryo File Format"], off, len(buf))
        return ".nif", nxt - off
    if tail.startswith(b";Gamebryo KFM File Version"):
        if magic_index is None:
            return ".kfm", 0
        nxt = next_after(magic_index[b";Gamebryo KFM File Version"], off, len(buf))
        return ".kfm", nxt - off
    if tail.startswith(b"DDS "):
        return ".dds", dds_length(buf, off) or 0
    if tail.startswith(b"RIFF") and tail[8:12] == b"WAVE":
        return ".wav", riff_length(buf, off) or 0
    if tail.startswith(b"OggS"):
        return ".ogg", 0
    if tail.startswith(SFA_ENTRY_PREFIX):
        return ".sfaentry", 0
    return "", 0


def magic_from_prefix(prefix: bytes) -> str:
    if prefix.startswith(b"Gamebryo File Format"):
        return ".nif"
    if prefix.startswith(b";Gamebryo KFM File Version"):
        return ".kfm"
    if prefix.startswith(b"DDS "):
        return ".dds"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WAVE":
        return ".wav"
    if prefix.startswith(b"OggS"):
        return ".ogg"
    if prefix.startswith(SFA_ENTRY_PREFIX):
        return ".sfaentry"
    return ""


def probe_archive(root: Path, archive: str, out_dir: Path, limit: int = 0, fast: bool = False) -> None:
    dec_path = root / f"{archive}.SFAi.dec"
    if not dec_path.exists():
        print(f"[!] {archive}: missing {dec_path}")
        return

    volume_files = volume_paths(root, archive)
    volume_sizes = [p.stat().st_size for p in volume_files]
    if fast:
        concat_table = volume_table(root, archive)
        data_size = table_size(concat_table)
        data = b""
        magic_index = None
    else:
        data = read_volumes(root, archive)
        data_size = len(data)
        concat_table = []
        magic_index = build_magic_index(data)
    rows = []
    for row in iter_records(dec_path.read_bytes()):
        if limit and len(rows) >= limit:
            break
        offset = int(row["field_256"])
        volume_index = int(row["field_268"])
        size = int(row["field_260"])
        volume_in_range = (
            0 <= volume_index < len(volume_files)
            and 0 <= offset < volume_sizes[volume_index]
        )
        sized_in_range = bool(volume_in_range and size and offset + size <= volume_sizes[volume_index])
        global_offset = sum(volume_sizes[:volume_index]) + offset if volume_in_range else -1
        global_in_range = 0 <= global_offset < data_size
        if volume_in_range:
            ext = magic_from_prefix(read_volume_at(volume_files, volume_index, offset, 32))
            magic_len = 0
        else:
            ext, magic_len = ("", 0)
        if not fast and volume_in_range and not ext:
            ext, magic_len = magic_at(data, global_offset, magic_index) if global_in_range else ("", 0)
        rows.append({
            "archive": archive,
            **row,
            "offset": offset,
            "volume_index": volume_index,
            "global_offset": global_offset,
            "in_range": int(volume_in_range),
            "sized_in_range": int(sized_in_range),
            "magic_ext": ext,
            "magic_len": magic_len,
        })

    out_dir.mkdir(parents=True, exist_ok=True)
    out_csv = out_dir / f"{archive}_offset_probe.csv"
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "archive",
                "index",
                "volume_index",
                "offset",
                "global_offset",
                "in_range",
                "sized_in_range",
                "magic_ext",
                "magic_len",
                "field_256",
                "field_260",
                "field_264",
                "field_268",
                "field_272",
                "encoded_name_len",
                "encoded_name_hex",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    by_ext: dict[str, int] = {}
    for row in rows:
        key = row["magic_ext"] or "none"
        by_ext[key] = by_ext.get(key, 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(by_ext.items()))
    print(f"[+] {archive}: records={len(rows)} {summary} -> {out_csv}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("archives", nargs="*", help="archive base names, e.g. World Character UI")
    ap.add_argument("--root", default=".", help="directory with .SFAi.dec and numbered volumes")
    ap.add_argument("--out", default="sfa_offset_probe", help="CSV output directory")
    ap.add_argument("--limit", type=int, default=0, help="probe only the first N records per archive")
    ap.add_argument("--fast", action="store_true", help="do not pre-scan large archives for NIF/KFM lengths")
    args = ap.parse_args()

    root = Path(args.root)
    archives = args.archives or sorted(
        p.name[:-len(".SFAi.dec")] for p in root.glob("*.SFAi.dec")
    )
    for archive in archives:
        try:
            probe_archive(root, archive, Path(args.out), args.limit, args.fast)
        except FileNotFoundError as exc:
            print(f"[!] {archive}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
