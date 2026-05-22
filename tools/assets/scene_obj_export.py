#!/usr/bin/env python3
"""
Scene OBJ exporter — for a `<map>/` directory containing:
  - `<map>.nif`            (terrain mesh)
  - `<map>.luo`            (scene script)
  - `<map>_Objects.luo`    (object placements)
  - other map LUOs

This walks the directory, converts each NIF to OBJ (cached), and then
emits a single combined `<map>_scene.obj` that includes:
  - the terrain
  - every NIF referenced anywhere in the directory tree, placed at
    coordinates parsed from the `Objects.luo` if available, otherwise
    at the origin

The output is a single ASCII OBJ file plus a sibling MTL with all the
texture references.

Usage:
    python Res\\scene_obj_export.py \\
        --scene Res\\extracted_named_v4\\World\\Map\\ch\\chage_berserker_Devil \\
        --out   Res\\scene_obj\\chage_berserker_Devil
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qqxj_asset_pipeline as pipe
import luo_decompiler as ld


# Very rough placement extractor — looks for `NiPoint3(x, y, z)` literals in
# the decompiled scene script and pairs them with the most recently-seen
# NIF filename string. Not perfect (the Lua semantics are richer than this)
# but enough to start.

NIF_REF_RE = re.compile(r'"([^"]+\.nif)"', re.IGNORECASE)
POS3_RE = re.compile(
    r"NiPoint3\s*\(\s*(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*,"
    r"\s*(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*,"
    r"\s*(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*\)"
)


def detect_resource_root(scene_dir: Path, explicit_root: str | None) -> Path:
    if explicit_root:
        return Path(explicit_root).resolve()
    names = {"extracted_named", "extracted_named_v4", "tw_extracted_named_v4"}
    cur = scene_dir.resolve()
    while cur != cur.parent:
        if cur.name in names:
            return cur
        if (cur / "Character" / "PartsDB.fdb").exists() and (cur / "World" / "Map").exists():
            return cur
        cur = cur.parent
    return scene_dir.parent.resolve()


def build_nif_index(root: Path) -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    if not root.exists():
        return index
    for nif in root.rglob("*.nif"):
        index.setdefault(nif.name.lower(), []).append(nif)
    for matches in index.values():
        matches.sort(key=lambda p: (len(p.parts), str(p).lower()))
    return index


def _path_parts_lower(path: Path) -> tuple[str, ...]:
    return tuple(part.lower() for part in path.parts if part not in ("", "."))


def resolve_nif_reference(
    nif_ref: str,
    scene_dir: Path,
    root: Path,
    scene_index: dict[str, list[Path]],
    root_index: dict[str, list[Path]],
) -> Path | None:
    clean_ref = nif_ref.strip().replace("\\", "/")
    ref_path = Path(clean_ref)
    direct_candidates = []
    if ref_path.is_absolute():
        direct_candidates.append(ref_path)
    else:
        direct_candidates.extend([scene_dir / ref_path, root / ref_path])
    for cand in direct_candidates:
        if cand.exists():
            return cand.resolve()

    wanted_suffix = _path_parts_lower(ref_path)
    basename = ref_path.name.lower()
    matches = scene_index.get(basename, []) + root_index.get(basename, [])
    if wanted_suffix and len(wanted_suffix) > 1:
        suffix_matches = [
            p for p in matches
            if _path_parts_lower(p)[-len(wanted_suffix):] == wanted_suffix
        ]
        if suffix_matches:
            return suffix_matches[0]
    return matches[0] if matches else None


def collect_placements(scene_dir: Path) -> list[dict]:
    """Walk every *.luo in scene_dir, decompile, and pair NIF refs with
    their nearest NiPoint3 (best-effort).
    """
    placements: list[dict] = []
    for luo in sorted(scene_dir.glob("*.luo")):
        src = ld.decompile_luo(luo)
        if not src:
            continue
        # Find every NIF mention plus the next NiPoint3 after it (within
        # 400 chars).
        for m in NIF_REF_RE.finditer(src):
            nif_name = m.group(1)
            tail = src[m.end():m.end() + 400]
            pos_m = POS3_RE.search(tail)
            if pos_m:
                pos = (float(pos_m.group(1)), float(pos_m.group(2)),
                       float(pos_m.group(3)))
            else:
                pos = (0.0, 0.0, 0.0)
            placements.append({
                "luo": luo.name, "nif": nif_name, "position": pos,
            })
    return placements


def merge_obj(src_obj: Path, dst_obj_handle, dst_mtl_handle, group_name: str,
              vertex_offset: int, uv_offset: int, position: tuple[float, float, float],
              mtl_path: Path | None,
              bounds_sink: list[str] | None = None) -> tuple[int, int]:
    """Append src_obj to dst_obj_handle, preserving normal mesh objects.

    Collision/bounding-box debug materials are redirected into a single
    scene-wide object named ``__BOUNDING_BOXES``. Normal meshes keep their
    source OBJ object names, prefixed by the placement group so names remain
    unique in the combined scene. Returns (new_vertex_offset, new_uv_offset).
    """
    if not src_obj.exists():
        return vertex_offset, uv_offset
    px, py, pz = position
    n_verts = 0
    n_uvs = 0
    # Track materials used by this OBJ so the combined MTL stays in sync.
    seen_materials: list[str] = []
    current_object = group_name
    current_material = ""
    current_is_bounds = False
    last_written_object = None
    bounds_lines: list[str] = []
    last_bounds_material = ""

    def write_object(name: str) -> None:
        nonlocal last_written_object
        if last_written_object != name:
            dst_obj_handle.write(f"\no {name}\n")
            last_written_object = name

    for raw_line in src_obj.read_text(errors="ignore").splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        if line.startswith("v "):
            parts = line.split()
            try:
                x, y, z = float(parts[1]), float(parts[2]), float(parts[3])
                dst_obj_handle.write(f"v {x + px} {y + py} {z + pz}\n")
                n_verts += 1
            except ValueError:
                continue
        elif line.startswith("vt "):
            dst_obj_handle.write(line + "\n")
            n_uvs += 1
        elif line.startswith("vn "):
            dst_obj_handle.write(line + "\n")
        elif line.startswith("f "):
            tokens = line.split()
            out_tokens = ["f"]
            for tok in tokens[1:]:
                # OBJ face token: v/vt/vn or v//vn or v/vt or v
                bits = tok.split("/")
                v_idx = int(bits[0]) + vertex_offset
                new_tok = str(v_idx)
                if len(bits) > 1 and bits[1]:
                    new_tok += "/" + str(int(bits[1]) + uv_offset)
                elif len(bits) > 1:
                    new_tok += "/"
                if len(bits) > 2 and bits[2]:
                    new_tok += "/" + str(int(bits[2]) + vertex_offset)
                out_tokens.append(new_tok)
            out_face = " ".join(out_tokens)
            if current_is_bounds:
                if current_material and current_material != last_bounds_material:
                    bounds_lines.append(f"usemtl {current_material}")
                    last_bounds_material = current_material
                bounds_lines.append(out_face)
            else:
                write_object(f"{group_name}__{pipe.safe_name(current_object)}")
                dst_obj_handle.write(out_face + "\n")
        elif line.startswith("usemtl "):
            mat = line[7:].strip()
            unique = f"{group_name}__{mat}"
            current_material = unique
            current_is_bounds = "collision_debug" in mat.lower()
            seen_materials.append((mat, unique))
            if not current_is_bounds:
                write_object(f"{group_name}__{pipe.safe_name(current_object)}")
                dst_obj_handle.write(f"usemtl {unique}\n")
        elif line.startswith("o ") or line.startswith("g "):
            name = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else group_name
            current_object = name or group_name
            continue
        elif line.startswith("mtllib"):
            continue
        else:
            dst_obj_handle.write(line + "\n")
    if bounds_lines and bounds_sink is not None:
        bounds_sink.extend(bounds_lines)
    elif bounds_lines:
        dst_obj_handle.write("\no __BOUNDING_BOXES\n")
        dst_obj_handle.write("\n".join(bounds_lines) + "\n")
    # Append uniquely-renamed materials to the combined MTL.
    if mtl_path and mtl_path.exists():
        in_block = False
        block_name = None
        block_lines: list[str] = []
        for raw_line in mtl_path.read_text(errors="ignore").splitlines():
            line = raw_line.rstrip()
            if line.lower().startswith("newmtl "):
                if in_block and block_name:
                    # flush
                    unique = next(
                        (u for orig, u in seen_materials if orig == block_name),
                        None,
                    )
                    if unique:
                        dst_mtl_handle.write(f"\nnewmtl {unique}\n")
                        dst_mtl_handle.write("\n".join(block_lines) + "\n")
                block_name = line[7:].strip()
                in_block = True
                block_lines = []
            elif in_block:
                block_lines.append(line)
        if in_block and block_name:
            unique = next(
                (u for orig, u in seen_materials if orig == block_name),
                None,
            )
            if unique:
                dst_mtl_handle.write(f"\nnewmtl {unique}\n")
                dst_mtl_handle.write("\n".join(block_lines) + "\n")
    return vertex_offset + n_verts, uv_offset + n_uvs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", required=True, help="map directory (e.g. World/Map/ch/<map>)")
    ap.add_argument("--out", required=True, help="output dir for the combined OBJ + textures")
    ap.add_argument("--root", default=None, help="extracted root for texture lookups (default: parent of scene)")
    ap.add_argument(
        "--include-objects",
        action="store_true",
        help="follow NIF references in *_Objects.luo (default: only the main map nif)",
    )
    args = ap.parse_args()

    scene_dir = Path(args.scene).resolve()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    root = detect_resource_root(scene_dir, args.root)

    map_name = scene_dir.name
    main_nif = scene_dir / f"{map_name}.nif"
    out_obj = out_dir / f"{map_name}_scene.obj"
    out_mtl = out_dir / f"{map_name}_scene.mtl"

    placements: list[dict] = []
    # Always include the main map NIF
    if main_nif.exists():
        placements.append({"luo": "(root)", "nif": main_nif.name, "position": (0, 0, 0)})
    # Optionally include objects from LUOs
    if args.include_objects:
        placements.extend(collect_placements(scene_dir))

    print(f"[+] scene: {map_name}")
    print(f"[+] {len(placements)} placement(s) to merge")
    print(f"[+] resource root: {root}")
    print("[+] indexing NIFs...")
    scene_index = build_nif_index(scene_dir)
    root_index = build_nif_index(root) if root != scene_dir else scene_index
    print(f"[+] indexed {sum(len(v) for v in root_index.values())} NIF(s)")

    with out_obj.open("w") as obj_h, out_mtl.open("w") as mtl_h:
        obj_h.write(f"# Combined scene OBJ for {map_name}\n")
        obj_h.write(f"mtllib {out_mtl.name}\n")
        mtl_h.write(f"# Materials for {map_name}\n")
        v_off = 0
        uv_off = 0
        scene_bounds_lines: list[str] = []
        alias_rows: list[dict[str, str]] = []
        for i, p in enumerate(placements):
            nif_name = p["nif"]
            nif_path = resolve_nif_reference(
                nif_name,
                scene_dir=scene_dir,
                root=root,
                scene_index=scene_index,
                root_index=root_index,
            )
            if not nif_path:
                print(f"  [{i}] missing: {nif_name}")
                continue
            cache_root = out_dir / ".obj_cache"
            cache_root.mkdir(parents=True, exist_ok=True)
            export_dir = cache_root / f"{i:03d}_{nif_path.stem}"
            result = pipe.export_nif_to_obj(
                nif_path,
                out_dir=export_dir,
                resource_root=root,
                convert_to_png=True,
            )
            if not result.get("ok"):
                print(f"  [{i}] convert failed: {nif_path}")
                continue
            obj = Path(result["obj"])
            # Copy any texture files into out_dir
            for tex in obj.parent.glob("*.png"):
                dst = out_dir / tex.name
                if not dst.exists():
                    shutil.copy2(tex, dst)
            for tex in obj.parent.glob("*.dds"):
                dst = out_dir / tex.name
                if not dst.exists():
                    shutil.copy2(tex, dst)
            mtl_src = obj.with_suffix(".mtl")
            group = f"{i:03d}_{nif_path.stem}"
            alias_csv = export_dir / "texture_aliases.csv"
            if alias_csv.exists():
                with alias_csv.open("r", newline="", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        alias_rows.append({
                            "group": group,
                            "nif": str(nif_path),
                            "texture_ref": row.get("texture_ref", ""),
                            "new_ref": row.get("new_ref", ""),
                            "source": row.get("source", ""),
                            "copied_to": str(out_dir / row.get("new_ref", "")) if row.get("new_ref") else "",
                        })
            v_off, uv_off = merge_obj(obj, obj_h, mtl_h, group, v_off, uv_off,
                                      p["position"], mtl_src, scene_bounds_lines)
            print(f"  [{i}] +{nif_path.stem}  @{p['position']}  vtotal={v_off}")
        if scene_bounds_lines:
            obj_h.write("\no __BOUNDING_BOXES\n")
            obj_h.write("\n".join(scene_bounds_lines) + "\n")
    alias_out = out_dir / "texture_aliases.csv"
    with alias_out.open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["group", "nif", "texture_ref", "new_ref", "source", "copied_to"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(alias_rows)
    print(f"[+] wrote {out_obj}  ({out_obj.stat().st_size} bytes)")
    print(f"[+] wrote {out_mtl}")
    print(f"[+] wrote {alias_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
