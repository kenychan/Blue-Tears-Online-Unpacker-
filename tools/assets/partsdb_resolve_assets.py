#!/usr/bin/env python3
"""
Resolve decoded PartsDB records to actual files under an extracted Character tree.

Input:
    Character/PartsDB.fdb
    extracted root, e.g. Res/TW/extracted_named

Output:
    partsdb_assets.json/csv where each row says:
      - part name/id/parent
      - mesh NIF path, inherited from parent for texture-override records
      - shape -> texture path mappings
      - KF animation paths

This is the bridge between PartsDB.fdb and the viewer/exporter.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from partsdb_probe import PartRecord, ShapeTextures, decode


def norm_rel(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def as_posix_or_empty(path: Path | None) -> str:
    return path.as_posix() if path else ""


def build_path_index(character_dir: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for path in character_dir.rglob("*"):
        if path.is_file():
            out[path.relative_to(character_dir).as_posix().lower()] = path
    return out


def resolve_under_character(
    character_dir: Path,
    path_index: dict[str, Path],
    base_dir: str,
    filename: str,
) -> Path | None:
    if not filename:
        return None
    rel = (Path(*norm_rel(base_dir).split("/")) / Path(*norm_rel(filename).split("/"))).as_posix()
    return path_index.get(rel.lower())


def texture_candidates(record: PartRecord, inherited_base_dir: str = "") -> list[dict]:
    base_dir = record.base_dir or inherited_base_dir
    out = []
    for item in record.texture_shapes:
        for tex in item.textures:
            out.append({"shape": item.shape, "base_dir": base_dir, "texture": tex})
    return out


def build_assets(root: Path, records: list[PartRecord], full_animations: bool = False) -> list[dict]:
    character_dir = root / "Character"
    path_index = build_path_index(character_dir)
    by_id = {rec.part_id: rec for rec in records}
    animation_cache: dict[int, tuple[int, int, list[dict]]] = {}
    resolved: list[dict] = []

    def resolve_animations(source: PartRecord | None) -> tuple[int, int, list[dict]]:
        if source is None:
            return 0, 0, []
        cached = animation_cache.get(source.part_id)
        if cached is not None:
            return cached
        rows = []
        existing = 0
        names = source.animations if full_animations else source.animations[:20]
        for name in names:
            kf_path = resolve_under_character(
                character_dir, path_index, source.base_dir, name
            )
            rows.append({
                "name": name,
                "path": as_posix_or_empty(kf_path),
                "exists": kf_path is not None,
            })
            if kf_path is not None:
                existing += 1
        if not full_animations:
            # Avoid duplicating hundreds of KF filenames into every inherited
            # child record. The extracted TW PartsDB uses the same base dir
            # convention consistently, so a sampled existence check is enough
            # for the compact manifest.
            existing = len(source.animations) if existing == len(rows) else existing
        result = (len(source.animations), existing, rows)
        animation_cache[source.part_id] = result
        return result

    for rec in records:
        parent = by_id.get(rec.parent_id)
        mesh_source = rec if rec.mesh_file.lower().endswith(".nif") else parent
        anim_source = rec if rec.animations else parent
        texture_base = rec.base_dir
        if rec.kind == "T" and parent is not None:
            texture_base = parent.base_dir

        mesh_path = None
        if mesh_source is not None:
            mesh_path = resolve_under_character(
                character_dir, path_index, mesh_source.base_dir, mesh_source.mesh_file
            )

        texture_rows = []
        for tex in texture_candidates(rec, texture_base):
            tex_path = resolve_under_character(
                character_dir, path_index, tex["base_dir"], tex["texture"]
            )
            texture_rows.append({
                "shape": tex["shape"],
                "texture": tex["texture"],
                "path": as_posix_or_empty(tex_path),
                "exists": tex_path is not None,
            })

        animation_count, animations_existing, animation_rows = resolve_animations(anim_source)

        resolved.append({
            "kind": rec.kind,
            "index": rec.index,
            "part_id": rec.part_id,
            "parent_id": rec.parent_id,
            "parent_name": parent.name if parent else "",
            "name": rec.name,
            "mesh_attach_point": rec.mesh_attach_point or (parent.mesh_attach_point if parent else ""),
            "base_dir": rec.base_dir,
            "mesh_file": mesh_source.mesh_file if mesh_source else "",
            "mesh_path": as_posix_or_empty(mesh_path),
            "mesh_exists": mesh_path is not None,
            "textures": texture_rows,
            "texture_count": len(texture_rows),
            "textures_existing": sum(1 for row in texture_rows if row["exists"]),
            "animations": animation_rows,
            "animation_source_part_id": anim_source.part_id if anim_source else -1,
            "animation_count": animation_count,
            "animations_existing": animations_existing,
        })
    return resolved


def write_outputs(assets: list[dict], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "partsdb_assets.json").write_text(
        json.dumps(assets, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with (out_dir / "partsdb_assets.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "kind", "part_id", "parent_id", "name", "parent_name",
                "mesh_attach_point", "base_dir", "mesh_file", "mesh_path",
                "mesh_exists", "texture_count", "textures_existing",
                "animation_source_part_id", "animation_count", "animations_existing",
            ],
        )
        writer.writeheader()
        for row in assets:
            writer.writerow({k: row.get(k, "") for k in writer.fieldnames})
    summary = {
        "records": len(assets),
        "mesh_existing": sum(1 for row in assets if row["mesh_exists"]),
        "mesh_missing": sum(1 for row in assets if not row["mesh_exists"]),
        "textures": sum(row["texture_count"] for row in assets),
        "textures_existing": sum(row["textures_existing"] for row in assets),
        "animations": sum(row["animation_count"] for row in assets),
        "animations_existing": sum(row["animations_existing"] for row in assets),
    }
    (out_dir / "partsdb_asset_summary.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("fdb", help="Path to Character/PartsDB.fdb")
    ap.add_argument("--root", required=True, help="extracted root containing Character/")
    ap.add_argument("--out", default="partsdb_assets", help="output directory")
    ap.add_argument(
        "--full-animations",
        action="store_true",
        help="embed every resolved KF path in JSON; default stores counts plus a sample",
    )
    args = ap.parse_args()

    _, records = decode(Path(args.fdb), include_t=True)
    assets = build_assets(Path(args.root), records, full_animations=args.full_animations)
    write_outputs(assets, Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
