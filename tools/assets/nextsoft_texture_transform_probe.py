#!/usr/bin/env python3
"""
Probe static TexDesc/NiTextureTransform payloads in Nextsoft Gamebryo NIFs.

This does not need full NIF block boundaries. It uses the header block-type
order to identify NiSourceTexture block indices, then scans before each texture
filename for a TexDesc whose sourceRef points at that block.

Gamebryo 10.x TexDesc layout used here:
    i32 sourceRef
    u32 clampMode
    u32 filterMode
    u32 uvSet
    i16 ps2_l
    i16 ps2_k
    u8  hasTexTransform
    if has:
        f32 translate_u, translate_v
        f32 scale_u, scale_v
        f32 rotation
        u32 method
        f32 center_u, center_v
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import struct
from dataclasses import asdict, dataclass
from pathlib import Path

import nif_parser


@dataclass
class TexTransform:
    texture_index: int
    source_block: int
    texture: str
    texture_offset: int
    texdesc_offset: int
    clamp_mode: int
    filter_mode: int
    uv_set: int
    ps2_l: int
    ps2_k: int
    has_transform: bool
    translate_u: float = 0.0
    translate_v: float = 0.0
    scale_u: float = 1.0
    scale_v: float = 1.0
    rotation: float = 0.0
    method: int = 0
    center_u: float = 0.0
    center_v: float = 0.0

    @property
    def is_identity(self) -> bool:
        return (
            abs(self.translate_u) < 1e-6
            and abs(self.translate_v) < 1e-6
            and abs(self.scale_u - 1.0) < 1e-6
            and abs(self.scale_v - 1.0) < 1e-6
            and abs(self.rotation) < 1e-6
        )


def _f32(buf: bytes, off: int) -> float:
    return struct.unpack_from("<f", buf, off)[0]


def texture_refs(buf: bytes) -> list[tuple[int, str]]:
    refs: list[tuple[int, str]] = []
    for match in re.finditer(rb"[-A-Za-z0-9_./\\: ]+\.(?:dds|png|tga|bmp|jpg|jpeg)", buf, re.I):
        raw = match.group(0).split(b"\x00")[-1]
        name = raw.decode("latin1", errors="ignore").replace("\\", "/").strip()
        if name:
            refs.append((match.start(), name))
    return refs


def parse_texdesc_at(buf: bytes, off: int, source_block: int) -> TexTransform | None:
    if off < 0 or off + 21 > len(buf):
        return None
    try:
        src, clamp, filt, uv_set, ps2_l, ps2_k = struct.unpack_from("<iIIIhh", buf, off)
    except struct.error:
        return None
    if src != source_block:
        return None
    if clamp > 8 or filt > 8 or uv_set > 16:
        return None
    has = buf[off + 20]
    if has not in (0, 1):
        return None
    tx = TexTransform(
        texture_index=-1,
        source_block=source_block,
        texture="",
        texture_offset=-1,
        texdesc_offset=off,
        clamp_mode=clamp,
        filter_mode=filt,
        uv_set=uv_set,
        ps2_l=ps2_l,
        ps2_k=ps2_k,
        has_transform=bool(has),
    )
    if has:
        if off + 21 + 32 > len(buf):
            return None
        vals = struct.unpack_from("<5fI2f", buf, off + 21)
        floats = [*vals[:5], *vals[6:]]
        if any(not math.isfinite(v) or abs(v) > 10000 for v in floats):
            return None
        tx.translate_u = vals[0]
        tx.translate_v = vals[1]
        tx.scale_u = vals[2]
        tx.scale_v = vals[3]
        tx.rotation = vals[4]
        tx.method = vals[5]
        tx.center_u = vals[6]
        tx.center_v = vals[7]
    return tx


def find_transforms_from_bytes(buf: bytes) -> list[TexTransform]:
    info, _ = nif_parser.parse_header(buf)
    source_blocks = [
        i for i, ti in enumerate(info.block_type_indices)
        if 0 <= ti < len(info.block_types)
        and info.block_types[ti] == "NiSourceTexture"
    ]
    refs = texture_refs(buf)
    out: list[TexTransform] = []
    for i, (tex_off, tex_name) in enumerate(refs):
        if i >= len(source_blocks):
            break
        source_block = source_blocks[i]
        needle = struct.pack("<i", source_block)
        search_start = max(0, tex_off - 8192)
        hits = [
            search_start + m.start()
            for m in re.finditer(re.escape(needle), buf[search_start:tex_off])
        ]
        parsed = None
        for hit in reversed(hits):
            cand = parse_texdesc_at(buf, hit, source_block)
            if cand is not None:
                parsed = cand
                break
        if parsed is None:
            continue
        parsed.texture_index = i
        parsed.texture = tex_name
        parsed.texture_offset = tex_off
        out.append(parsed)
    return out


def find_transforms(path: Path) -> list[TexTransform]:
    return find_transforms_from_bytes(path.read_bytes())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("nif", type=Path)
    ap.add_argument("--out", type=Path, help="optional CSV output")
    args = ap.parse_args()

    rows = find_transforms(args.nif)
    print(json.dumps([asdict(r) | {"is_identity": r.is_identity} for r in rows], indent=2))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", newline="", encoding="utf-8") as f:
            fieldnames = list(asdict(rows[0]).keys()) + ["is_identity"] if rows else [
                "texture_index", "source_block", "texture", "texture_offset", "texdesc_offset",
                "clamp_mode", "filter_mode", "uv_set", "ps2_l", "ps2_k", "has_transform",
                "translate_u", "translate_v", "scale_u", "scale_v", "rotation", "method",
                "center_u", "center_v", "is_identity",
            ]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(asdict(row) | {"is_identity": row.is_identity})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
