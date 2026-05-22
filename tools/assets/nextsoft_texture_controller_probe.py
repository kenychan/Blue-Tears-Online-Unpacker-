#!/usr/bin/env python3
"""
Probe animated texture controllers in Nextsoft/Gamebryo NIF/KF files.

This is deliberately metadata-only. OBJ can store static UVs, but it cannot
store time-varying texture transforms or flipbook texture swaps. The viewer and
future animation export can use this output to evaluate those controllers.

The layout is backed by two sources:
  - TW NClient_unpacked `NiTextureTransformController::LoadBinary/GetCtlrID`
  - local nifly `NiTextureTransformController::Sync`

For 10.2.0.5, the controller payload follows NiFloatInterpController:
  NiTimeController:
    i32 nextControllerRef
    u16 flags
    f32 frequency, phase, startTime, stopTime
    i32 targetRef
  NiSingleInterpController:
    i32 interpolatorRef
  NiTextureTransformController:
    u8  shaderMap
    u32 textureSlot
    u32 operation
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


TT_OPERATION_NAMES = {
    0: "TT_TRANSLATE_U",
    1: "TT_TRANSLATE_V",
    2: "TT_ROTATE",
    3: "TT_SCALE_U",
    4: "TT_SCALE_V",
}

TEXTURE_SLOT_NAMES = {
    0: "BASE_MAP",
    1: "DARK_MAP",
    2: "DETAIL_MAP",
    3: "GLOSS_MAP",
    4: "GLOW_MAP",
    5: "BUMP_MAP",
    6: "NORMAL_MAP",
    7: "PARALLAX_MAP",
    8: "DECAL_0_MAP",
    9: "DECAL_1_MAP",
    10: "DECAL_2_MAP",
    11: "DECAL_3_MAP",
}


@dataclass
class TextureTransformController:
    block_index: int
    offset: int
    next_controller_ref: int
    flags: int
    frequency: float
    phase: float
    start_time: float
    stop_time: float
    target_ref: int
    interpolator_ref: int
    shader_map: bool
    texture_slot: int
    texture_slot_name: str
    operation: int
    operation_name: str
    score: float


@dataclass
class FlipController:
    block_index: int
    offset: int
    next_controller_ref: int
    flags: int
    frequency: float
    phase: float
    start_time: float
    stop_time: float
    target_ref: int
    interpolator_ref: int
    texture_slot: int
    texture_slot_name: str
    source_refs: list[int]
    score: float


@dataclass
class FloatKey:
    time: float
    value: float
    forward: float | None = None
    backward: float | None = None
    tension: float | None = None
    bias: float | None = None
    continuity: float | None = None


@dataclass
class FloatData:
    block_index: int
    offset: int
    interpolation: int
    interpolation_name: str
    num_keys: int
    keys: list[FloatKey]
    byte_size: int
    score: float


KEY_TYPE_NAMES = {
    0: "NO_INTERP",
    1: "LINEAR_KEY",
    2: "QUADRATIC_KEY",
    3: "TBC_KEY",
    4: "XYZ_ROTATION_KEY",
    5: "CONST_KEY",
}


def _i32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<i", buf, off)[0]


def _u16(buf: bytes, off: int) -> int:
    return struct.unpack_from("<H", buf, off)[0]


def _u32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def _f32(buf: bytes, off: int) -> float:
    return struct.unpack_from("<f", buf, off)[0]


def _valid_ref(value: int, num_blocks: int) -> bool:
    return value == -1 or 0 <= value < num_blocks


def _valid_time(value: float) -> bool:
    # Gamebryo often uses large sentinel-ish values. Reject NaN/Inf and only
    # truly absurd magnitudes.
    return math.isfinite(value) and abs(value) < 1.0e20


def _base_controller_at(buf: bytes, off: int, num_blocks: int) -> tuple[dict, float] | None:
    if off < 0 or off + 30 > len(buf):
        return None
    try:
        next_ref = _i32(buf, off)
        flags = _u16(buf, off + 4)
        frequency = _f32(buf, off + 6)
        phase = _f32(buf, off + 10)
        start_time = _f32(buf, off + 14)
        stop_time = _f32(buf, off + 18)
        target_ref = _i32(buf, off + 22)
        interpolator_ref = _i32(buf, off + 26)
    except struct.error:
        return None
    if not (_valid_ref(next_ref, num_blocks) and _valid_ref(target_ref, num_blocks)):
        return None
    if not _valid_ref(interpolator_ref, num_blocks):
        return None
    if flags > 0x7FFF:
        return None
    if not all(_valid_time(v) for v in (frequency, phase, start_time, stop_time)):
        return None
    score = 0.0
    if abs(frequency - 1.0) < 1e-4:
        score += 3.0
    if abs(phase) < 1e-4:
        score += 1.0
    if start_time <= stop_time:
        score += 1.0
    if target_ref != -1:
        score += 1.0
    if interpolator_ref != -1:
        score += 1.0
    return {
        "next_controller_ref": next_ref,
        "flags": flags,
        "frequency": frequency,
        "phase": phase,
        "start_time": start_time,
        "stop_time": stop_time,
        "target_ref": target_ref,
        "interpolator_ref": interpolator_ref,
    }, score


def _texture_transform_at(
    buf: bytes,
    off: int,
    block_index: int,
    num_blocks: int,
) -> TextureTransformController | None:
    base = _base_controller_at(buf, off, num_blocks)
    if base is None or off + 39 > len(buf):
        return None
    fields, score = base
    shader_map = buf[off + 30]
    texture_slot = _u32(buf, off + 31)
    operation = _u32(buf, off + 35)
    if shader_map not in (0, 1):
        return None
    if texture_slot not in TEXTURE_SLOT_NAMES:
        return None
    if operation not in TT_OPERATION_NAMES:
        return None
    score += 10.0
    if texture_slot == 0:
        score += 1.0
    return TextureTransformController(
        block_index=block_index,
        offset=off,
        shader_map=bool(shader_map),
        texture_slot=texture_slot,
        texture_slot_name=TEXTURE_SLOT_NAMES[texture_slot],
        operation=operation,
        operation_name=TT_OPERATION_NAMES[operation],
        score=score,
        **fields,
    )


def _flip_controller_at(
    buf: bytes,
    off: int,
    block_index: int,
    num_blocks: int,
) -> FlipController | None:
    base = _base_controller_at(buf, off, num_blocks)
    if base is None or off + 38 > len(buf):
        return None
    fields, score = base
    texture_slot = _u32(buf, off + 30)
    count = _u32(buf, off + 34)
    if texture_slot not in TEXTURE_SLOT_NAMES:
        return None
    if count > 256 or off + 38 + count * 4 > len(buf):
        return None
    refs = [_i32(buf, off + 38 + i * 4) for i in range(count)]
    if any(not _valid_ref(ref, num_blocks) for ref in refs):
        return None
    score += 8.0 + min(count, 8)
    return FlipController(
        block_index=block_index,
        offset=off,
        texture_slot=texture_slot,
        texture_slot_name=TEXTURE_SLOT_NAMES[texture_slot],
        source_refs=refs,
        score=score,
        **fields,
    )


def _float_data_at(buf: bytes, off: int, block_index: int) -> FloatData | None:
    if off < 0 or off + 4 > len(buf):
        return None
    try:
        num_keys = _u32(buf, off)
    except struct.error:
        return None
    if num_keys == 0:
        return FloatData(
            block_index=block_index,
            offset=off,
            interpolation=0,
            interpolation_name="NO_KEYS",
            num_keys=0,
            keys=[],
            byte_size=4,
            score=1.0,
        )
    if num_keys > 4096 or off + 8 > len(buf):
        return None
    interpolation = _u32(buf, off + 4)
    if interpolation not in KEY_TYPE_NAMES or interpolation == 4:
        return None
    pos = off + 8
    keys: list[FloatKey] = []
    last_time = -math.inf
    score = 4.0 + min(num_keys, 16) * 0.25
    if interpolation == 0:
        # Real animated curves in these KF files are normally LINEAR or
        # QUADRATIC. NO_INTERP scans with multiple keys are often accidental
        # float-looking byte runs inside larger blocks.
        score -= 5.0 + max(0, num_keys - 1) * 1.0
    for _ in range(num_keys):
        if pos + 8 > len(buf):
            return None
        time = _f32(buf, pos)
        value = _f32(buf, pos + 4)
        pos += 8
        if not (_valid_time(time) and math.isfinite(value) and abs(value) < 1.0e10):
            return None
        key = FloatKey(time=time, value=value)
        if interpolation == 2:
            if pos + 8 > len(buf):
                return None
            key.forward = _f32(buf, pos)
            key.backward = _f32(buf, pos + 4)
            pos += 8
            if not all(math.isfinite(v) and abs(v) < 1.0e10 for v in (key.forward, key.backward)):
                return None
        elif interpolation == 3:
            if pos + 12 > len(buf):
                return None
            key.tension = _f32(buf, pos)
            key.bias = _f32(buf, pos + 4)
            key.continuity = _f32(buf, pos + 8)
            pos += 12
            if not all(
                math.isfinite(v) and abs(v) < 1.0e10
                for v in (key.tension, key.bias, key.continuity)
            ):
                return None
        if time >= last_time:
            score += 0.5
        else:
            score -= 4.0
        last_time = time
        keys.append(key)
    if keys:
        if abs(keys[0].time) < 1e-4:
            score += 2.0
        if keys[-1].time > keys[0].time:
            score += 1.0
        if abs(keys[-1].time - 3.0) < 1e-4:
            score += 2.0
        unique_times = len({round(k.time, 5) for k in keys})
        unique_values = len({round(k.value, 5) for k in keys})
        if num_keys > 4 and unique_times <= 1:
            score -= min(num_keys, 64) * 1.0
        if num_keys > 4 and unique_values <= 1:
            score -= min(num_keys, 64) * 0.5
    return FloatData(
        block_index=block_index,
        offset=off,
        interpolation=interpolation,
        interpolation_name=KEY_TYPE_NAMES[interpolation],
        num_keys=num_keys,
        keys=keys,
        byte_size=pos - off,
        score=score,
    )


def _block_indices(info: nif_parser.NifInfo, type_name: str) -> list[int]:
    return [
        i for i, ti in enumerate(info.block_type_indices)
        if 0 <= ti < len(info.block_types) and info.block_types[ti] == type_name
    ]


def _choose_non_overlapping(candidates: list, wanted: int, size: int) -> list:
    chosen = []
    occupied: list[tuple[int, int]] = []
    for cand in sorted(candidates, key=lambda c: (-c.score, c.offset)):
        byte_size = int(getattr(cand, "byte_size", size) or size)
        span = (cand.offset, cand.offset + byte_size)
        if any(span[0] < hi and span[1] > lo for lo, hi in occupied):
            continue
        chosen.append(cand)
        occupied.append(span)
        if len(chosen) >= wanted:
            break
    return sorted(chosen, key=lambda c: c.offset)


def _source_texture_anchors(buf: bytes, info: nif_parser.NifInfo) -> dict[int, int]:
    source_blocks = _block_indices(info, "NiSourceTexture")
    refs: list[int] = []
    for match in re.finditer(rb"[-A-Za-z0-9_./\\: ]+\.(?:dds|png|tga|bmp|jpg|jpeg)", buf, re.I):
        raw = match.group(0).split(b"\x00")[-1]
        if raw:
            # The block body usually starts at the sized string length.
            refs.append(max(0, match.start() - 4))
    return {
        block_index: refs[i]
        for i, block_index in enumerate(source_blocks)
        if i < len(refs)
    }


def _choose_float_data(
    candidates: list[FloatData],
    float_data_blocks: list[int],
    anchors: dict[int, int],
) -> list[FloatData]:
    chosen: list[FloatData] = []
    occupied: list[tuple[int, int]] = []
    for block_index in float_data_blocks:
        lower = max((off for bi, off in anchors.items() if bi < block_index), default=0)
        upper = min((off for bi, off in anchors.items() if bi > block_index), default=10**18)
        block_candidates = [
            cand for cand in candidates
            if lower <= cand.offset < upper
            and not any(cand.offset < hi and cand.offset + cand.byte_size > lo for lo, hi in occupied)
        ]
        if not block_candidates:
            block_candidates = [
                cand for cand in candidates
                if not any(cand.offset < hi and cand.offset + cand.byte_size > lo for lo, hi in occupied)
            ]
        if not block_candidates:
            continue
        # Within the anchored range, prefer the earliest high-quality candidate;
        # this matches physical NIF block order and avoids later mesh-data float
        # runs that accidentally look like animation curves.
        best = max(block_candidates, key=lambda c: (c.score, -abs(c.offset - lower), -c.offset))
        best.block_index = block_index
        chosen.append(best)
        occupied.append((best.offset, best.offset + best.byte_size))
    return sorted(chosen, key=lambda c: c.offset)


def find_texture_controllers_from_bytes(buf: bytes) -> dict:
    info, header_end = nif_parser.parse_header(buf)
    tt_blocks = _block_indices(info, "NiTextureTransformController")
    flip_blocks = _block_indices(info, "NiFlipController")
    float_data_blocks = _block_indices(info, "NiFloatData")

    tt_candidates: list[TextureTransformController] = []
    flip_candidates: list[FlipController] = []
    float_candidates: list[FloatData] = []
    for off in range(header_end, max(header_end, len(buf) - 38)):
        if tt_blocks:
            cand = _texture_transform_at(buf, off, -1, info.num_blocks)
            if cand is not None:
                tt_candidates.append(cand)
        if flip_blocks:
            cand = _flip_controller_at(buf, off, -1, info.num_blocks)
            if cand is not None:
                flip_candidates.append(cand)
        if float_data_blocks:
            cand = _float_data_at(buf, off, -1)
            if cand is not None:
                float_candidates.append(cand)

    tt = _choose_non_overlapping(tt_candidates, len(tt_blocks), 39)
    for block_index, cand in zip(tt_blocks, tt):
        cand.block_index = block_index

    flips = _choose_non_overlapping(flip_candidates, len(flip_blocks), 38)
    for block_index, cand in zip(flip_blocks, flips):
        cand.block_index = block_index

    anchors: dict[int, int] = {cand.block_index: cand.offset for cand in tt}
    anchors.update({cand.block_index: cand.offset for cand in flips})
    anchors.update(_source_texture_anchors(buf, info))
    floats = _choose_float_data(float_candidates, float_data_blocks, anchors)

    return {
        "nif_version": info.version_str,
        "num_blocks": info.num_blocks,
        "expected_texture_transform_controllers": len(tt_blocks),
        "texture_transform_controllers": [asdict(c) for c in tt],
        "expected_flip_controllers": len(flip_blocks),
        "flip_controllers": [asdict(c) for c in flips],
        "expected_float_data": len(float_data_blocks),
        "float_data": [asdict(c) for c in floats],
        "probe_notes": {
            "complete_texture_transform_controller_scan": len(tt) == len(tt_blocks),
            "complete_flip_controller_scan": len(flips) == len(flip_blocks),
            "complete_float_data_scan": len(floats) == len(float_data_blocks),
        },
    }


def find_texture_controllers(path: Path) -> dict:
    return find_texture_controllers_from_bytes(path.read_bytes()) | {"path": str(path)}


def bind_texture_animation(nif_path: Path) -> dict:
    """Best-effort pairing of NIF texture controllers with sibling KF curves."""
    nif_meta = find_texture_controllers(nif_path)
    controllers = nif_meta.get("texture_transform_controllers", [])
    if not controllers:
        return {
            "ok": 1,
            "nif": str(nif_path),
            "controllers": [],
            "kf": "",
            "bindings": [],
        }

    inline_curves = nif_meta.get("float_data", [])
    if inline_curves:
        curve_by_block = {int(curve.get("block_index", -999)): curve for curve in inline_curves}
        bindings = []
        max_duration = 0.0
        for i, controller in enumerate(sorted(controllers, key=lambda c: c.get("offset", 0))):
            data_ref = int(controller.get("interpolator_ref", -999)) + 1
            curve = curve_by_block.get(data_ref)
            if curve is None and i < len(inline_curves):
                curve = inline_curves[i]
            if curve:
                curve = dict(curve)
                curve["operation_name"] = controller.get("operation_name", "")
                for key in curve.get("keys", []):
                    max_duration = max(max_duration, float(key.get("time", 0.0)))
            bindings.append({
                "controller": controller,
                "sequence_link": {},
                "guessed_data_ref": data_ref if data_ref != -999 else None,
                "curve": curve,
                "mesh_group_target_ref": controller.get("target_ref"),
                "binding_method": "inline_interpolator_ref_plus_one",
            })
        return {
            "ok": 1,
            "nif": str(nif_path),
            "kf": "",
            "duration": max_duration or 3.0,
            "controller_count": len(controllers),
            "curve_count": len(inline_curves),
            "bindings": bindings,
            "note": "bound inline NiFloatData curves from the NIF",
        }

    best_kf = None
    best_meta = None
    best_score = -1
    for kf in sorted(nif_path.parent.glob("*.kf")):
        try:
            meta = find_texture_controllers(kf)
        except Exception:
            continue
        curves = meta.get("float_data", [])
        score = min(len(curves), len(controllers))
        if score > best_score:
            best_score = score
            best_kf = kf
            best_meta = meta
    if best_kf is None or best_meta is None or best_score <= 0:
        return {
            "ok": 1,
            "nif": str(nif_path),
            "controllers": controllers,
            "kf": "",
            "bindings": [],
            "note": "no sibling KF with usable NiFloatData curves",
        }

    curves = best_meta.get("float_data", [])
    curve_by_block = {int(curve.get("block_index", -999)): curve for curve in curves}
    sequence_links = []
    try:
        import nextsoft_kf_transform_probe as transform_probe
        seq_meta = transform_probe.probe_transform_tracks(best_kf)
        sequence_links = [
            cb for cb in seq_meta.get("controller_sequence", {}).get("controlled_blocks", [])
            if cb.get("controller_type") == "NiTextureTransformController"
        ]
    except Exception:
        sequence_links = []

    bindings = []
    max_duration = 0.0
    sorted_controllers = sorted(controllers, key=lambda c: c.get("offset", 0))
    for i, controller in enumerate(sorted_controllers):
        link = sequence_links[i] if i < len(sequence_links) else {}
        data_ref = int(link.get("interpolator_ref", -999)) + 1 if link else -999
        curve = curve_by_block.get(data_ref)
        if curve is None and i < len(curves):
            curve = curves[i]
        if curve:
            curve = dict(curve)
            curve["operation_name"] = controller.get("operation_name", "") or _operation_from_controller_id(
                str(link.get("controller_id", ""))
            )
            for key in curve.get("keys", []):
                max_duration = max(max_duration, float(key.get("time", 0.0)))
        bindings.append({
            "controller": controller,
            "sequence_link": link,
            "guessed_data_ref": data_ref if data_ref != -999 else None,
            "curve": curve,
            "mesh_group_target_ref": controller.get("target_ref"),
            "binding_method": "sequence_interpolator_ref_plus_one" if link else "controller_order",
        })
    return {
        "ok": 1,
        "nif": str(nif_path),
        "kf": str(best_kf),
        "duration": max_duration or 3.0,
        "controller_count": len(controllers),
        "curve_count": len(curves),
        "bindings": bindings,
    }


def _operation_from_controller_id(controller_id: str) -> str:
    for name in TT_OPERATION_NAMES.values():
        if name in controller_id:
            return name
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", type=Path, help="NIF or KF file")
    ap.add_argument("--json", dest="json_out", type=Path, help="optional JSON output")
    ap.add_argument("--csv", dest="csv_out", type=Path, help="optional CSV output for texture transform controllers")
    ap.add_argument("--bind-sibling-kf", action="store_true", help="for a NIF, also pair controllers with sibling KF curves")
    args = ap.parse_args()

    data = bind_texture_animation(args.path) if args.bind_sibling_kf else find_texture_controllers(args.path)
    print(json.dumps(data, indent=2))
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(data, indent=2), encoding="utf-8")
    if args.csv_out:
        args.csv_out.parent.mkdir(parents=True, exist_ok=True)
        rows = data["texture_transform_controllers"]
        fieldnames = list(rows[0].keys()) if rows else [
            "block_index", "offset", "operation", "operation_name", "texture_slot", "texture_slot_name",
        ]
        with args.csv_out.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
