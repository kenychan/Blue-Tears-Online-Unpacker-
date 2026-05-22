#!/usr/bin/env python3
"""
Normalize Nextsoft/Gamebryo NIF headers for standard NIF tools.

The extracted QQxj/Nextsoft NIFs are valid enough for the local inspector, but
some standard readers reject the exact 10.2.0.5 version. This script writes
compatibility copies, optionally patching the version and/or widening the block
type count field for experiments.

Original files are never modified.
"""

from __future__ import annotations

import argparse
import os
import struct
from pathlib import Path


HEADER_MAGIC = b"Gamebryo File Format"
NEXTSOFT_VERSION_FAMILY = 0x0A020000


class NormalizeError(Exception):
    pass


def parse_version(text: str) -> int:
    parts = text.split(".")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("version must look like 10.2.0.0")
    try:
        a, b, c, d = [int(x, 10) for x in parts]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("version components must be decimal") from exc
    if not all(0 <= x <= 255 for x in (a, b, c, d)):
        raise argparse.ArgumentTypeError("version components must fit in one byte")
    return (a << 24) | (b << 16) | (c << 8) | d


def version_text(version: int) -> str:
    return ".".join(str((version >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def normalize(
    buf: bytes,
    patch_version: int | None = None,
    widen_block_types: bool = False,
) -> bytes:
    nl = buf.find(b"\n")
    if nl < 0 or not buf.startswith(HEADER_MAGIC):
        raise NormalizeError("not a Gamebryo NIF header")

    off = nl + 1
    if off + 14 > len(buf):
        raise NormalizeError("truncated NIF header")

    version = struct.unpack_from("<I", buf, off)[0]
    if (version & 0xFFFFFF00) != NEXTSOFT_VERSION_FAMILY:
        raise NormalizeError(f"unexpected NIF version 0x{version:08x}")

    # Header after text:
    #   u32 version
    #   u32 user_version
    #   u32 num_blocks
    #   u16 num_block_types       <- Nextsoft variant
    ntypes_off = off + 12
    ntypes = struct.unpack_from("<H", buf, ntypes_off)[0]
    if ntypes == 0 or ntypes > 4096:
        raise NormalizeError(f"implausible num_block_types={ntypes}")

    out = bytearray()
    if patch_version is None:
        out += buf[:ntypes_off]
    else:
        header_line = (
            f"Gamebryo File Format, Version {version_text(patch_version)}"
            .encode("ascii")
        )
        out += header_line + b"\n"
        out += struct.pack("<I", patch_version)
        out += buf[off + 4:ntypes_off]

    out += struct.pack("<I" if widen_block_types else "<H", ntypes)
    out += buf[ntypes_off + 2:]
    return bytes(out)


def looks_like_standard_header(buf: bytes, widened: bool) -> bool:
    """Light validation after changing the header."""
    try:
        nl = buf.find(b"\n")
        off = nl + 1
        _version, _user_version, nblocks = struct.unpack_from("<III", buf, off)
        off += 12
        if widened:
            ntypes = struct.unpack_from("<I", buf, off)[0]
            off += 4
        else:
            ntypes = struct.unpack_from("<H", buf, off)[0]
            off += 2
        if nblocks <= 0 or nblocks > 1_000_000 or ntypes <= 0 or ntypes > 4096:
            return False
        for _ in range(ntypes):
            slen = struct.unpack_from("<I", buf, off)[0]
            off += 4
            if slen <= 0 or slen > 200 or off + slen > len(buf):
                return False
            name = buf[off:off + slen]
            if not all(0x20 <= b < 0x7F for b in name):
                return False
            off += slen
        # Enough validation to prove the type table is aligned.
        return True
    except Exception:
        return False


def iter_nifs(path: Path):
    if path.is_file():
        yield path
        return
    for root, _, files in os.walk(path):
        for name in files:
            if name.lower().endswith(".nif"):
                yield Path(root) / name


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="NIF file or directory")
    ap.add_argument("output", help="output file or directory")
    ap.add_argument(
        "--patch-version",
        type=parse_version,
        default=None,
        help="also rewrite NIF binary/text version, e.g. 10.2.0.0",
    )
    ap.add_argument(
        "--widen-block-types",
        action="store_true",
        help="experimental: change num_block_types from uint16 to uint32",
    )
    ap.add_argument("--limit", type=int, default=0, help="stop after N files")
    args = ap.parse_args()

    src = Path(args.input)
    dst = Path(args.output)
    files = list(iter_nifs(src))
    if args.limit:
        files = files[:args.limit]

    ok = failed = 0
    for path in files:
        try:
            out = normalize(
                path.read_bytes(),
                args.patch_version,
                widen_block_types=args.widen_block_types,
            )
            if not looks_like_standard_header(out, args.widen_block_types):
                raise NormalizeError("normalized header did not validate")

            out_path = dst if src.is_file() else dst / path.relative_to(src)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(out)
            ok += 1
        except Exception as exc:
            failed += 1
            print(f"[!] {path}: {exc}")

    print(f"[+] normalized={ok} failed={failed} -> {dst}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
