#!/usr/bin/env python3
"""
Animation inventory/probe for Nextsoft/Gamebryo NIF files.

This is not a full KF/animation exporter yet. It identifies animation-oriented
NIFs and records trustworthy metadata: block types, embedded strings, and
candidate keyframe/time values. Use it to prioritize the files that need the
next parser pass.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inspect_nifs  # noqa: E402


ANIM_TYPES = {
    "NiControllerSequence",
    "NiTransformInterpolator",
    "NiTransformData",
    "NiTextKeyExtraData",
    "NiBoolInterpolator",
    "NiBoolData",
    "NiFloatInterpolator",
    "NiFloatData",
    "NiVisController",
}


def iter_nifs(path: Path):
    if path.is_file():
        yield path
        return
    for root, _, files in os.walk(path):
        for name in files:
            if name.lower().endswith(".nif"):
                yield Path(root) / name


def candidate_times(buf: bytes) -> list[float]:
    vals = []
    for off in range(0, len(buf) - 4, 4):
        f = struct.unpack_from("<f", buf, off)[0]
        if 0.0 <= f <= 10_000.0:
            # Filter the constant identity-transform soup a bit.
            if f not in (0.0, 1.0):
                vals.append(round(f, 6))
    out = []
    seen = set()
    for f in vals:
        if f not in seen:
            out.append(f)
            seen.add(f)
        if len(out) >= 32:
            break
    return out


def probe(path: Path) -> dict[str, str | int]:
    data = path.read_bytes()
    try:
        s = inspect_nifs.summarize_file(str(path))
    except Exception as exc:
        return {
            "path": str(path),
            "is_anim": 0,
            "num_blocks": 0,
            "anim_types": "",
            "strings": "",
            "candidate_times": "",
            "error": str(exc),
        }

    if "error" in s:
        return {
            "path": str(path),
            "is_anim": 0,
            "num_blocks": 0,
            "anim_types": "",
            "strings": "",
            "candidate_times": "",
            "error": s["error"],
        }

    types = [t for t in s["type_names"] if t in ANIM_TYPES]
    strings = [x for x in s["all_strings"] if re.search(r"[A-Za-z_]", x)]
    return {
        "path": str(path),
        "is_anim": int(bool(types)),
        "num_blocks": s["num_blocks"],
        "anim_types": ";".join(types),
        "strings": ";".join(strings[:20]),
        "candidate_times": ";".join(str(x) for x in candidate_times(data)),
        "error": "",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="NIF file or directory")
    ap.add_argument("output_csv", help="CSV report")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    files = list(iter_nifs(Path(args.input)))
    if args.limit:
        files = files[:args.limit]

    rows = [probe(path) for path in files]
    out = Path(args.output_csv)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "path",
                "is_anim",
                "num_blocks",
                "anim_types",
                "strings",
                "candidate_times",
                "error",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    anim = sum(int(r["is_anim"]) for r in rows)
    print(f"[+] animation_like={anim}/{len(rows)} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
