#!/usr/bin/env python3
"""Probe NiTransformData tracks in QQXJ/Gamebryo KF files."""

from __future__ import annotations

import argparse
import json
import math
import struct
from dataclasses import asdict, dataclass
from pathlib import Path

import nif_parser


KEY_TYPE_NAMES = {
    0: "NO_INTERP",
    1: "LINEAR_KEY",
    2: "QUADRATIC_KEY",
    3: "TBC_KEY",
    4: "XYZ_ROTATION_KEY",
    5: "CONST_KEY",
}


@dataclass
class ScalarKey:
    time: float
    value: float


@dataclass
class Vec3Key:
    time: float
    value: tuple[float, float, float]


@dataclass
class QuatKey:
    time: float
    value: tuple[float, float, float, float]


@dataclass
class TransformData:
    block_index: int
    offset: int
    rotation_type: int
    rotation_type_name: str
    rotation_keys: list[QuatKey]
    translation_type: int
    translation_type_name: str
    translation_keys: list[Vec3Key]
    scale_type: int
    scale_type_name: str
    scale_keys: list[ScalarKey]
    byte_size: int
    score: float


@dataclass
class ControlledBlock:
    index: int
    interpolator_ref: int
    controller_ref: int
    string_palette_ref: int
    node_name_offset: int
    property_type_offset: int
    controller_type_offset: int
    controller_id_offset: int
    interpolator_id_offset: int
    node_name: str
    property_type: str
    controller_type: str
    controller_id: str
    interpolator_id: str


def _u32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def _f32(buf: bytes, off: int) -> float:
    return struct.unpack_from("<f", buf, off)[0]


def _finite(value: float, limit: float = 1.0e7) -> bool:
    return math.isfinite(value) and abs(value) < limit


def _read_scalar_group(buf: bytes, off: int):
    if off + 4 > len(buf):
        return None
    n = _u32(buf, off)
    pos = off + 4
    if n == 0:
        return 0, "NO_KEYS", [], pos
    if n > 4096 or pos + 4 > len(buf):
        return None
    key_type = _u32(buf, pos)
    pos += 4
    if key_type not in KEY_TYPE_NAMES or key_type == 4:
        return None
    keys: list[ScalarKey] = []
    last_time = -math.inf
    for _ in range(n):
        if pos + 8 > len(buf):
            return None
        t = _f32(buf, pos)
        v = _f32(buf, pos + 4)
        pos += 8
        if not (_finite(t, 1.0e20) and _finite(v)):
            return None
        if t < last_time:
            return None
        last_time = t
        keys.append(ScalarKey(t, v))
        if key_type == 2:
            pos += 8
        elif key_type == 3:
            pos += 12
        if pos > len(buf):
            return None
    return key_type, KEY_TYPE_NAMES[key_type], keys, pos


def _read_vec3_group(buf: bytes, off: int):
    if off + 4 > len(buf):
        return None
    n = _u32(buf, off)
    pos = off + 4
    if n == 0:
        return 0, "NO_KEYS", [], pos
    if n > 4096 or pos + 4 > len(buf):
        return None
    key_type = _u32(buf, pos)
    pos += 4
    if key_type not in KEY_TYPE_NAMES or key_type == 4:
        return None
    keys: list[Vec3Key] = []
    last_time = -math.inf
    for _ in range(n):
        if pos + 16 > len(buf):
            return None
        t = _f32(buf, pos)
        value = struct.unpack_from("<3f", buf, pos + 4)
        pos += 16
        if not (_finite(t, 1.0e20) and all(_finite(v) for v in value)):
            return None
        if t < last_time:
            return None
        last_time = t
        keys.append(Vec3Key(t, value))
        if key_type == 2:
            pos += 24
        elif key_type == 3:
            pos += 12
        if pos > len(buf):
            return None
    return key_type, KEY_TYPE_NAMES[key_type], keys, pos


def _read_rotation_keys(buf: bytes, off: int):
    if off + 4 > len(buf):
        return None
    n = _u32(buf, off)
    pos = off + 4
    if n == 0:
        return 0, "NO_KEYS", [], pos
    if n > 4096 or pos + 4 > len(buf):
        return None
    key_type = _u32(buf, pos)
    pos += 4
    if key_type not in KEY_TYPE_NAMES or key_type == 4:
        return None
    keys: list[QuatKey] = []
    last_time = -math.inf
    for _ in range(n):
        if pos + 20 > len(buf):
            return None
        t = _f32(buf, pos)
        q = struct.unpack_from("<4f", buf, pos + 4)
        pos += 20
        if not (_finite(t, 1.0e20) and all(_finite(v, 10.0) for v in q)):
            return None
        if t < last_time:
            return None
        last_time = t
        keys.append(QuatKey(t, q))
        if key_type == 2:
            pos += 32
        elif key_type == 3:
            pos += 12
        if pos > len(buf):
            return None
    return key_type, KEY_TYPE_NAMES[key_type], keys, pos


def _transform_data_at(buf: bytes, off: int, block_index: int) -> TransformData | None:
    rot = _read_rotation_keys(buf, off)
    if rot is None:
        return None
    rotation_type, rotation_type_name, rotation_keys, pos = rot
    trans = _read_vec3_group(buf, pos)
    if trans is None:
        return None
    translation_type, translation_type_name, translation_keys, pos = trans
    scale = _read_scalar_group(buf, pos)
    if scale is None:
        return None
    scale_type, scale_type_name, scale_keys, pos = scale
    key_count = len(rotation_keys) + len(translation_keys) + len(scale_keys)
    if key_count == 0:
        return None
    score = 10.0 + key_count
    if translation_keys:
        score += 4.0
    if rotation_keys:
        score += 3.0
    if scale_keys:
        score += 1.0
    return TransformData(
        block_index=block_index,
        offset=off,
        rotation_type=rotation_type,
        rotation_type_name=rotation_type_name,
        rotation_keys=rotation_keys,
        translation_type=translation_type,
        translation_type_name=translation_type_name,
        translation_keys=translation_keys,
        scale_type=scale_type,
        scale_type_name=scale_type_name,
        scale_keys=scale_keys,
        byte_size=pos - off,
        score=score,
    )


def _block_indices(info: nif_parser.NifInfo, type_name: str) -> list[int]:
    return [
        i for i, ti in enumerate(info.block_type_indices)
        if 0 <= ti < len(info.block_types) and info.block_types[ti] == type_name
    ]


def _read_sized_string(buf: bytes, off: int) -> tuple[str, int] | None:
    if off + 4 > len(buf):
        return None
    n = _u32(buf, off)
    pos = off + 4
    if n > 4096 or pos + n > len(buf):
        return None
    raw = buf[pos:pos + n]
    try:
        text = raw.decode("latin1", errors="replace")
    except Exception:
        return None
    return text.rstrip("\x00"), pos + n


def _parse_string_palettes(buf: bytes) -> list[dict]:
    palettes: list[dict] = []
    for off in range(0, max(0, len(buf) - 12)):
        n = _u32(buf, off)
        if n < 16 or n > 65536 or off + 4 + n + 4 > len(buf):
            continue
        data = buf[off + 4:off + 4 + n]
        trailing_len = _u32(buf, off + 4 + n)
        if trailing_len not in (n, n - 1, 0):
            continue
        printable = sum(1 for b in data if b == 0 or 32 <= b < 127)
        if printable / max(1, len(data)) < 0.95 or b"\x00" not in data:
            continue
        strings: dict[int, str] = {}
        pos = 0
        for chunk in data.split(b"\x00"):
            if chunk:
                strings[pos] = chunk.decode("latin1", errors="replace")
            pos += len(chunk) + 1
        if strings:
            palettes.append({"offset": off, "length": n, "strings": strings})
    palettes.sort(key=lambda p: (-len(p["strings"]), p["offset"]))
    return palettes


def _palette_lookup(palette: dict | None, offset: int) -> str:
    if palette is None or offset == 0xFFFFFFFF or offset < 0:
        return ""
    strings = palette.get("strings", {})
    if offset in strings:
        return strings[offset]
    return ""


def _parse_controller_sequence(buf: bytes, info: nif_parser.NifInfo, header_end: int) -> dict:
    seq_blocks = _block_indices(info, "NiControllerSequence")
    if not seq_blocks:
        return {"controlled_blocks": [], "sequence_name": "", "error": "no NiControllerSequence"}
    pos = header_end
    # QQXJ 10.2.0.5 KF starts with an object name string, then the sequence
    # name string. The first is commonly empty.
    object_name = ""
    first = _read_sized_string(buf, pos)
    if first is None:
        return {"controlled_blocks": [], "sequence_name": "", "error": "sequence string parse failed"}
    object_name, pos = first
    second = _read_sized_string(buf, pos)
    if second is None:
        return {"controlled_blocks": [], "sequence_name": "", "error": "sequence name parse failed"}
    sequence_name, pos = second
    if pos + 8 > len(buf):
        return {"controlled_blocks": [], "sequence_name": sequence_name, "error": "sequence too short"}
    count = _u32(buf, pos)
    array_grow_by = _u32(buf, pos + 4)
    pos += 8
    if count > 4096 or pos + count * 32 > len(buf):
        return {
            "controlled_blocks": [],
            "sequence_name": sequence_name,
            "error": f"implausible controlled block count: {count}",
        }
    palettes = _parse_string_palettes(buf)
    palette = palettes[0] if palettes else None
    blocks: list[ControlledBlock] = []
    for i in range(count):
        vals = struct.unpack_from("<8i", buf, pos)
        pos += 32
        (
            interpolator_ref,
            controller_ref,
            string_palette_ref,
            node_off,
            prop_off,
            ctrl_type_off,
            ctrl_id_off,
            interp_id_off,
        ) = vals
        blocks.append(ControlledBlock(
            index=i,
            interpolator_ref=interpolator_ref,
            controller_ref=controller_ref,
            string_palette_ref=string_palette_ref,
            node_name_offset=node_off,
            property_type_offset=prop_off,
            controller_type_offset=ctrl_type_off,
            controller_id_offset=ctrl_id_off,
            interpolator_id_offset=interp_id_off,
            node_name=_palette_lookup(palette, node_off),
            property_type=_palette_lookup(palette, prop_off),
            controller_type=_palette_lookup(palette, ctrl_type_off),
            controller_id=_palette_lookup(palette, ctrl_id_off),
            interpolator_id=_palette_lookup(palette, interp_id_off),
        ))
    return {
        "object_name": object_name,
        "sequence_name": sequence_name,
        "controlled_block_count": count,
        "array_grow_by": array_grow_by,
        "palette_count": len(palettes),
        "palette_offset": palette.get("offset") if palette else None,
        "controlled_blocks": [asdict(b) for b in blocks],
    }


def _choose(candidates: list[TransformData], wanted: int) -> list[TransformData]:
    chosen: list[TransformData] = []
    occupied: list[tuple[int, int]] = []
    for cand in sorted(candidates, key=lambda c: (-c.score, c.offset)):
        span = (cand.offset, cand.offset + cand.byte_size)
        if any(span[0] < hi and span[1] > lo for lo, hi in occupied):
            continue
        chosen.append(cand)
        occupied.append(span)
        if len(chosen) >= wanted:
            break
    return sorted(chosen, key=lambda c: c.offset)


def probe_transform_tracks_from_bytes(buf: bytes) -> dict:
    info, header_end = nif_parser.parse_header(buf)
    blocks = _block_indices(info, "NiTransformData") + _block_indices(info, "NiKeyframeData")
    candidates: list[TransformData] = []
    for off in range(header_end, max(header_end, len(buf) - 12)):
        cand = _transform_data_at(buf, off, -1)
        if cand is not None:
            candidates.append(cand)
    tracks = _choose(candidates, len(blocks))
    for block_index, track in zip(blocks, tracks):
        track.block_index = block_index
    controller_sequence = _parse_controller_sequence(buf, info, header_end)
    track_by_block = {track.block_index: asdict(track) for track in tracks}
    bound_transform_tracks = []
    for cb in controller_sequence.get("controlled_blocks", []):
        if cb.get("controller_type") != "NiTransformController":
            continue
        interp_ref = int(cb.get("interpolator_ref", -1))
        # In these QQXJ 10.2.0.5 KFs, TransformInterpolator blocks are paired
        # directly with the following NiTransformData block. The sequence's
        # controlled block points at the interpolator; the data block carries
        # the actual key group.
        data_ref = interp_ref + 1
        bound_transform_tracks.append({
            "controlled_block": cb,
            "guessed_data_ref": data_ref,
            "track": track_by_block.get(data_ref),
            "binding_method": "interpolator_ref_plus_one",
        })
    return {
        "nif_version": info.version_str,
        "num_blocks": info.num_blocks,
        "controller_sequence": controller_sequence,
        "bound_transform_tracks": bound_transform_tracks,
        "expected_transform_data": len(blocks),
        "transform_tracks": [asdict(t) for t in tracks],
        "probe_notes": {
            "complete_transform_data_scan": len(tracks) == len(blocks),
        },
    }


def probe_transform_tracks(path: Path) -> dict:
    return probe_transform_tracks_from_bytes(path.read_bytes()) | {"path": str(path)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("kf", type=Path)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()
    data = probe_transform_tracks(args.kf)
    print(json.dumps(data, indent=2))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
