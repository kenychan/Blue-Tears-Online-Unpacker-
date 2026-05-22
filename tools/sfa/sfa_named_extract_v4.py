#!/usr/bin/env python3
"""
Named extractor for NextSoft SFA archives.

This parser is based on the SFAi routine recovered from the TW NClient runtime:
header/data/file records are decrypted with a rolling subtractive key, then each
file record gives an exact offset, size, and numbered data chunk.
"""

from __future__ import annotations

import argparse
import csv
import os
from dataclasses import dataclass
from pathlib import Path
import struct
import sys
from typing import Iterable


IMAGE_BASE = 0x400000
KEY_POINTER_VA = 0x16F4398
HEADER_KEY_POS = 0x340
RECORDS_KEY_POS = 0x354


@dataclass
class DataRecord:
    index: int
    size: int
    extra: int


@dataclass
class FileRecord:
    index: int
    name: str
    offset: int
    size: int
    unknown0: int
    data_index: int
    payload_encoded: bool
    flags_or_hash0: int
    tail_hex: str


@dataclass
class ArchiveIndex:
    archive: str
    total_size: int
    record_size: int
    data_records: list[DataRecord]
    file_records: list[FileRecord]


def load_runtime_key(runtime_image: Path | None, key_file: Path | None) -> bytes:
    if key_file:
        key = key_file.read_bytes()
        if len(key) < 150:
            raise ValueError(f"key file is too short: {key_file}")
        return key[:300]

    if not runtime_image:
        raise ValueError("pass --runtime-image or --key-file")

    image = runtime_image.read_bytes()
    ptr_off = KEY_POINTER_VA - IMAGE_BASE
    if ptr_off < 0 or ptr_off + 4 > len(image):
        raise ValueError(f"runtime image does not contain key pointer VA {KEY_POINTER_VA:#x}")

    key_va = struct.unpack_from("<I", image, ptr_off)[0]
    key_off = key_va - IMAGE_BASE
    if key_off < 0 or key_off + 150 > len(image):
        raise ValueError(f"runtime image key pointer {key_va:#x} is outside the dumped image")
    return image[key_off:key_off + 300]


def decrypt_record(raw: bytes, key: bytes, key_pos: int) -> bytes:
    out = bytearray(raw)
    for i, value in enumerate(out):
        pos = key_pos + i
        out[i] = (value - ((key[pos % 150] + key[pos % 100]) & 0xFF)) & 0xFF
    return bytes(out)


def decode_name(raw: bytes) -> str:
    raw = raw.split(b"\0", 1)[0]
    for encoding in ("utf-8", "cp950", "cp936", "cp949", "latin1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    return raw.decode("latin1", "replace")


def safe_parts(name: str) -> list[str]:
    cleaned = name.replace("/", "\\").replace("\0", "")
    parts: list[str] = []
    for part in cleaned.split("\\"):
        if not part or part in (".", ".."):
            continue
        part = part.rstrip(" .")
        if not part:
            continue
        parts.append(part)
    return parts


def safe_output_path(out_root: Path, archive: str, name: str) -> Path:
    base = (out_root / archive).resolve()
    path = base.joinpath(*safe_parts(name)).resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"unsafe archive path: {name!r}") from exc
    return path


def parse_index(index_path: Path, key: bytes) -> ArchiveIndex:
    raw = index_path.read_bytes()
    if len(raw) < 20:
        raise ValueError(f"{index_path} is too small to be an SFAi index")

    header = decrypt_record(raw[:20], key, HEADER_KEY_POS)
    magic, version, total_size, data_count, file_count = struct.unpack("<5I", header)
    if magic != 0x7E414653:
        raise ValueError(f"{index_path} has invalid decrypted magic {magic:#x}")
    if version != 1:
        raise ValueError(f"{index_path} has unsupported SFA version {version}")

    file_base = 20 + data_count * 12
    remaining = len(raw) - file_base
    if file_count == 0 or remaining <= 0 or remaining % file_count:
        raise ValueError(
            f"{index_path} has inconsistent sizes: len={len(raw)}, "
            f"data_count={data_count}, file_count={file_count}, remaining={remaining}"
        )
    record_size = remaining // file_count
    if record_size < 276 or record_size % 4:
        raise ValueError(f"{index_path} has unexpected file record size {record_size}")

    data_records: list[DataRecord] = []
    for i in range(data_count):
        off = 20 + i * 12
        record = decrypt_record(raw[off:off + 12], key, RECORDS_KEY_POS + i * 12)
        data_records.append(DataRecord(*struct.unpack("<3I", record)))

    file_records: list[FileRecord] = []
    for i in range(file_count):
        off = file_base + i * record_size
        record = decrypt_record(raw[off:off + record_size], key, RECORDS_KEY_POS + i * record_size)
        name = decode_name(record[:256])
        fields = struct.unpack("<" + "I" * ((record_size - 256) // 4), record[256:])
        if len(fields) < 5:
            raise ValueError(f"{index_path} record {i} is too short")
        file_records.append(
            FileRecord(
                index=i,
                name=name,
                offset=fields[0],
                size=fields[1],
                unknown0=fields[2],
                data_index=fields[3],
                payload_encoded=bool(record[273]) if len(record) > 273 else False,
                flags_or_hash0=fields[4],
                tail_hex=record[256:].hex(),
            )
        )

    archive = index_path.name[:-5] if index_path.name.lower().endswith(".sfai") else index_path.stem
    return ArchiveIndex(archive, total_size, record_size, data_records, file_records)


def discover_archives(root: Path, names: Iterable[str] | None) -> list[str]:
    if names:
        return list(names)
    archives = []
    for path in sorted(root.glob("*.SFAi")):
        archives.append(path.name[:-5])
    return archives


def read_slice(chunk_path: Path, offset: int, size: int) -> bytes:
    with chunk_path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read(size)
    if len(data) != size:
        raise IOError(f"short read from {chunk_path}: wanted {size}, got {len(data)}")
    return data


def signature_label(data: bytes) -> str:
    if data.startswith(b"DDS "):
        return "DDS"
    if data.startswith(b"Gamebryo File Format"):
        return "NIF/KF/KFM"
    if data.startswith(b"OggS"):
        return "OGG"
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "WAV"
    if data.startswith(b"<?xml") or data.lstrip().startswith(b"<"):
        return "XML/TEXT"
    if data.startswith(b"\x1bLua") or data[:4] in (b"LuaQ", b"LUO\0"):
        return "LUA/LUO"
    return data[:8].hex()


def extract_archive(
    root: Path,
    out_root: Path,
    archive_index: ArchiveIndex,
    key: bytes,
    decode_payloads: bool,
    dry_run: bool,
    limit: int | None,
    manifest_writer: csv.DictWriter,
) -> tuple[int, int]:
    chunks: dict[int, Path] = {}
    for data_record in archive_index.data_records:
        chunk_path = root / f"{archive_index.archive}.{data_record.index:03d}"
        chunks[data_record.index] = chunk_path

    extracted = 0
    failed = 0
    for file_record in archive_index.file_records:
        if limit is not None and extracted >= limit:
            break

        chunk_path = chunks.get(file_record.data_index)
        output_path = safe_output_path(out_root, archive_index.archive, file_record.name)
        status = "ok"
        sig = ""
        error = ""
        try:
            if not chunk_path or not chunk_path.exists():
                raise FileNotFoundError(f"missing chunk for data index {file_record.data_index}")
            chunk_size = chunk_path.stat().st_size
            if file_record.offset + file_record.size > chunk_size:
                raise IOError(
                    f"entry exceeds chunk: off={file_record.offset}, "
                    f"size={file_record.size}, chunk_size={chunk_size}"
                )
            data = read_slice(chunk_path, file_record.offset, file_record.size)
            if decode_payloads and file_record.payload_encoded:
                data = decrypt_record(data, key, 1)
            sig = signature_label(data)
            if not dry_run:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(data)
            extracted += 1
        except Exception as exc:  # Keep going so one damaged record does not stop the archive.
            status = "failed"
            error = str(exc)
            failed += 1

        manifest_writer.writerow(
            {
                "archive": archive_index.archive,
                "index": file_record.index,
                "path": file_record.name,
                "chunk": file_record.data_index,
                "offset": file_record.offset,
                "size": file_record.size,
                "record_size": archive_index.record_size,
                "unknown0": file_record.unknown0,
                "flags_or_hash0": file_record.flags_or_hash0,
                "payload_encoded": int(file_record.payload_encoded),
                "signature": sig,
                "status": status,
                "error": error,
                "tail_hex": file_record.tail_hex,
                "output_path": str(output_path),
            }
        )
    return extracted, failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract named files from NextSoft SFA archives.")
    parser.add_argument("--root", required=True, type=Path, help="Directory containing *.SFAi and *.000 chunks")
    parser.add_argument("--out", required=True, type=Path, help="Output folder for extracted files")
    parser.add_argument("--runtime-image", type=Path, help="TW NClient runtime image used to recover the SFA key")
    parser.add_argument("--key-file", type=Path, help="Raw key bytes, alternative to --runtime-image")
    parser.add_argument("--archives", nargs="*", help="Archive base names, e.g. World Character UI")
    parser.add_argument("--manifest", type=Path, help="CSV manifest path")
    parser.add_argument("--raw-payloads", action="store_true", help="Do not decode per-file encoded payloads")
    parser.add_argument("--dry-run", action="store_true", help="Parse and write manifest without extracting data")
    parser.add_argument("--limit", type=int, help="Maximum files to extract per archive")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    out = args.out.resolve()
    script_dir = Path(__file__).resolve().parent
    default_manifest = out / "sfa_named_manifest.csv"
    manifest_path = (args.manifest or default_manifest).resolve()

    repo_root = script_dir.parent.resolve()
    for write_path in (out, manifest_path.parent):
        try:
            write_path.relative_to(repo_root)
        except ValueError:
            raise SystemExit(f"Refusing to write outside workspace: {write_path}")

    key = load_runtime_key(args.runtime_image, args.key_file)
    archives = discover_archives(root, args.archives)
    if not archives:
        raise SystemExit(f"No SFAi archives found under {root}")

    if not args.dry_run:
        out.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    fields = [
        "archive",
        "index",
        "path",
        "chunk",
        "offset",
        "size",
        "record_size",
        "unknown0",
        "flags_or_hash0",
        "payload_encoded",
        "signature",
        "status",
        "error",
        "tail_hex",
        "output_path",
    ]
    total_extracted = 0
    total_failed = 0
    with manifest_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for archive in archives:
            index_path = root / f"{archive}.SFAi"
            if not index_path.exists():
                print(f"[skip] missing {index_path}")
                continue
            parsed = parse_index(index_path, key)
            data_total = sum(record.size for record in parsed.data_records)
            status = "OK" if data_total == parsed.total_size else f"data sum {data_total} != header {parsed.total_size}"
            print(
                f"[index] {archive}: files={len(parsed.file_records)} "
                f"chunks={len(parsed.data_records)} recsize={parsed.record_size} {status}"
            )
            extracted, failed = extract_archive(
                root,
                out,
                parsed,
                key,
                not args.raw_payloads,
                args.dry_run,
                args.limit,
                writer,
            )
            total_extracted += extracted
            total_failed += failed
            print(f"[extract] {archive}: extracted={extracted} failed={failed}")

    mode = "dry-run" if args.dry_run else "extracted"
    print(f"[+] {mode} files={total_extracted} failed={total_failed} manifest={manifest_path}")
    return 0 if total_failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
