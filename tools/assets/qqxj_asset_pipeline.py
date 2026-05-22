#!/usr/bin/env python3
"""
Shared QQXJ asset pipeline.

This module is deliberately standalone: the GUI viewer and command-line export
use the same functions so texture resolution behaves the same everywhere.
Writes are kept under the project tree by default.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import subprocess
import sys
import textwrap
from dataclasses import asdict
from pathlib import Path

try:
    from PIL import Image
except Exception:  # pragma: no cover - optional
    Image = None


SCRIPT_DIR = Path(__file__).resolve().parent
if SCRIPT_DIR.name.lower() == "assets" and SCRIPT_DIR.parent.name.lower() == "tools":
    PROJECT_ROOT = SCRIPT_DIR.parent.parent
    DEFAULT_RESOURCE_ROOT = PROJECT_ROOT / "data" / "extracted_named_v4"
    DEFAULT_EXPORT_ROOT = PROJECT_ROOT / "exports" / "obj"
else:
    PROJECT_ROOT = SCRIPT_DIR.parent
    DEFAULT_RESOURCE_ROOT = SCRIPT_DIR / "extracted_named_v4"
    DEFAULT_EXPORT_ROOT = SCRIPT_DIR / "Export_Obj"
TEXTURE_EXTS = {".dds", ".tga", ".png", ".bmp", ".jpg", ".jpeg"}
DDS_FOURCC_BY_PIXEL_FORMAT = {4: b"DXT1", 5: b"DXT5", 6: b"DXT5"}
_TEXTURE_INDEX_CACHE: dict[str, tuple[dict[str, list[Path]], dict[str, list[Path]]]] = {}
_PARTSDB_CACHE: dict[str, list[object]] = {}
_IMAGE_ALPHA_CACHE: dict[str, bool] = {}
MAX_TEXTURE_BASENAME = 64


def assert_inside_project(path: Path) -> None:
    resolved = path.resolve()
    try:
        resolved.relative_to(PROJECT_ROOT.resolve())
    except ValueError as exc:
        raise ValueError(f"refusing to write outside project: {resolved}") from exc


def safe_name(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._")
    return text or "asset"


def default_export_dir(asset_path: Path, resource_root: Path = DEFAULT_RESOURCE_ROOT) -> Path:
    try:
        rel = asset_path.resolve().relative_to(resource_root.resolve())
        parent = DEFAULT_EXPORT_ROOT / rel.parent / safe_name(asset_path.stem)
    except ValueError:
        parent = DEFAULT_EXPORT_ROOT / safe_name(asset_path.stem)
    return parent


def decode_text(data: bytes) -> tuple[str, str]:
    for enc in ("utf-8", "gb18030", "cp936", "cp950", "cp949", "latin1"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            pass
    return data.decode("latin1", errors="replace"), "latin1-replace"


def shape_names_in_nif(nif_path: Path) -> list[str]:
    data = nif_path.read_bytes()
    out: list[str] = []
    seen = set()
    for match in re.finditer(rb"[A-Za-z0-9_]+Shape", data):
        off = match.start()
        name_len = match.end() - match.start()
        if off >= 4:
            prefix_len = int.from_bytes(data[off - 4:off], "little", signed=False)
            if prefix_len != name_len:
                continue
        name = match.group().decode("ascii", errors="replace")
        key = name.lower()
        if key not in seen:
            out.append(name)
            seen.add(key)
    return out


def archive_root_for(path: Path, resource_root: Path) -> Path:
    try:
        rel = path.resolve().relative_to(resource_root.resolve())
        first = rel.parts[0]
        return resource_root / first
    except Exception:
        return path.parent


def iter_textures(root: Path):
    if not root.exists():
        return
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in TEXTURE_EXTS:
            yield path


def texture_indices(resource_root: Path) -> tuple[dict[str, list[Path]], dict[str, list[Path]]]:
    cache_key = str(resource_root.resolve()).lower()
    if cache_key in _TEXTURE_INDEX_CACHE:
        return _TEXTURE_INDEX_CACHE[cache_key]
    by_base: dict[str, list[Path]] = {}
    by_rel: dict[str, list[Path]] = {}
    for tex in iter_textures(resource_root):
        by_base.setdefault(tex.name.lower(), []).append(tex)
        try:
            rel_to_root = tex.relative_to(resource_root)
            if len(rel_to_root.parts) > 1:
                rel = Path(*rel_to_root.parts[1:]).as_posix().lower()
                by_rel.setdefault(rel, []).append(tex)
        except Exception:
            pass
    for values in by_base.values():
        values.sort(key=lambda p: (len(p.parts), str(p).lower()))
    for values in by_rel.values():
        values.sort(key=lambda p: (len(p.parts), str(p).lower()))
    _TEXTURE_INDEX_CACHE[cache_key] = (by_base, by_rel)
    return by_base, by_rel


def image_has_alpha(path: Path) -> bool:
    if Image is None or not path.exists():
        return False
    cache_key = f"{path.resolve()}|{path.stat().st_mtime}|{path.stat().st_size}"
    if cache_key in _IMAGE_ALPHA_CACHE:
        return _IMAGE_ALPHA_CACHE[cache_key]
    try:
        img = Image.open(path)
        if img.mode in {"RGBA", "LA"}:
            alpha = img.getchannel("A")
            result = alpha.getextrema()[0] < 255
            _IMAGE_ALPHA_CACHE[cache_key] = result
            return result
        if img.mode == "P" and "transparency" in img.info:
            _IMAGE_ALPHA_CACHE[cache_key] = True
            return True
    except Exception:
        return False
    _IMAGE_ALPHA_CACHE[cache_key] = False
    return False


def texture_has_alpha_fast(path: Path) -> bool:
    if not path.exists():
        return False
    suffix = path.suffix.lower()
    try:
        if suffix == ".dds":
            with path.open("rb") as f:
                head = f.read(128)
            if len(head) >= 88 and head[:4] == b"DDS ":
                fourcc = head[84:88]
                if fourcc in {b"DXT3", b"DXT5", b"ATI2"}:
                    return True
                pf_flags = int.from_bytes(head[80:84], "little", signed=False)
                rgb_alpha_bit_mask = int.from_bytes(head[108:112], "little", signed=False)
                return bool((pf_flags & 0x1) or rgb_alpha_bit_mask)
        if suffix == ".tga":
            with path.open("rb") as f:
                head = f.read(18)
            return len(head) >= 18 and (head[17] & 0x0F) > 0
        if suffix == ".png":
            with path.open("rb") as f:
                head = f.read(33)
            if len(head) >= 33 and head.startswith(b"\x89PNG\r\n\x1a\n"):
                color_type = head[25]
                return color_type in {4, 6}
    except Exception:
        return False
    return image_has_alpha(path)


def exported_texture_has_alpha(copied_to: str, source: Path | None) -> bool:
    if source is not None and texture_has_alpha_fast(source):
        return True
    if copied_to:
        return texture_has_alpha_fast(Path(copied_to))
    return False


def resolve_texture(
    texture_ref: str,
    nif_path: Path,
    resource_root: Path,
    by_base: dict[str, list[Path]] | None = None,
    by_rel: dict[str, list[Path]] | None = None,
    allow_index: bool = True,
) -> tuple[Path | None, str]:
    ref = texture_ref.replace("\\", "/").strip()
    if not ref:
        return None, ""
    ref_path = Path(ref)
    candidates = [
        nif_path.parent / ref_path,
        archive_root_for(nif_path, resource_root) / ref_path,
        resource_root / ref_path,
    ]
    for cand in candidates:
        if cand.exists():
            return cand, "direct"
    for cand in candidates:
        for ext in TEXTURE_EXTS:
            alt = cand.with_suffix(ext)
            if alt.exists():
                return alt, "direct_alt_ext"

    if not allow_index:
        return None, ""
    if by_base is None or by_rel is None:
        by_base, by_rel = texture_indices(resource_root)

    ref_norm = ref.lower()
    if ref_norm in by_rel:
        return by_rel[ref_norm][0], "relative"

    suffix_matches = [
        path
        for rel, paths in by_rel.items()
        if rel.endswith("/" + ref_norm)
        for path in paths
    ]
    if suffix_matches:
        suffix_matches.sort(key=lambda p: (len(p.parts), str(p).lower()))
        return suffix_matches[0], "suffix"

    base = ref_path.name.lower()
    if base in by_base:
        # Prefer a texture near the NIF first. This avoids picking a random
        # same-basename texture from another costume/map folder.
        near = [p for p in by_base[base] if nif_path.parent in p.parents or p.parent == nif_path.parent]
        if near:
            near.sort(key=lambda p: len(p.parts))
            return near[0], "near_basename"
        return by_base[base][0], "basename"

    # The original NIFs often reference .tga while the extracted payload is
    # actually a DDS with the same stem.
    stem_matches = [
        path
        for candidate_base, paths in by_base.items()
        if Path(candidate_base).stem == ref_path.stem.lower()
        for path in paths
    ]
    if stem_matches:
        near = [p for p in stem_matches if nif_path.parent in p.parents or p.parent == nif_path.parent]
        chosen = near or stem_matches
        chosen.sort(key=lambda p: (len(p.parts), str(p).lower()))
        return chosen[0], "stem_any_ext"
    return None, ""


def unique_target_name(out_dir: Path, preferred: str, used: set[str]) -> str:
    path = Path(preferred)
    stem = safe_name(path.stem)
    suffix = path.suffix or ".png"
    if len(f"{stem}{suffix}") > MAX_TEXTURE_BASENAME:
        digest = hashlib.sha1(preferred.replace("\\", "/").encode("utf-8", errors="ignore")).hexdigest()[:10]
        keep = max(8, MAX_TEXTURE_BASENAME - len(suffix) - len(digest) - 1)
        stem = f"{stem[:keep]}_{digest}"
    name = f"{stem}{suffix}"
    i = 2
    while name.lower() in used:
        name = f"{stem}_{i}{suffix}"
        i += 1
    used.add(name.lower())
    return name


def dds_header(width: int, height: int, fourcc: bytes, mip_count: int, linear_size: int) -> bytes:
    import struct

    flags = 0x0002100F  # caps | height | width | pitch/linear | pixel format | mipmap count
    caps = 0x1000
    if mip_count > 1:
        caps |= 0x400008  # complex | mipmap
    reserved = [0] * 11
    pf_flags = 0x00000004  # DDPF_FOURCC
    return b"DDS " + struct.pack(
        "<7I 11I 2I 4s 10I",
        124,
        flags,
        height,
        width,
        linear_size,
        0,
        mip_count,
        *reserved,
        32,
        pf_flags,
        fourcc,
        0,
        0,
        0,
        0,
        0,
        caps,
        0,
        0,
        0,
        0,
    )


def dxt_linear_size(width: int, height: int, fourcc: bytes) -> int:
    block = 8 if fourcc == b"DXT1" else 16
    return max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * block


def parse_embedded_texture(buf: bytes, texture_ref: str) -> dict | None:
    import re
    import struct

    ref = Path(texture_ref.replace("\\", "/")).name.encode("latin1", errors="ignore")
    if not ref:
        return None
    for match in re.finditer(re.escape(ref), buf, re.I):
        name_start = match.start()
        if name_start < 4:
            continue
        try:
            name_len, = struct.unpack_from("<I", buf, name_start - 4)
        except Exception:
            continue
        if name_len <= 0 or name_len > 512 or name_start + name_len > len(buf):
            continue
        full_name = buf[name_start:name_start + name_len].decode("latin1", errors="ignore")
        if Path(texture_ref).name.lower() not in full_name.lower():
            continue

        source_tail = name_start + name_len
        if source_tail + 18 + 44 > len(buf):
            continue
        try:
            data_ref, pixel_layout, mipmap_format, alpha_format = struct.unpack_from("<IIII", buf, source_tail)
            is_static = buf[source_tail + 16]
            direct_render = buf[source_tail + 17]
            pixel_data = source_tail + 18
            pixel_format, = struct.unpack_from("<I", buf, pixel_data)
            mip_count, = struct.unpack_from("<I", buf, pixel_data + 40)
        except Exception:
            continue
        if pixel_layout != 6 or mip_count <= 0 or mip_count > 16:
            continue
        fourcc = None
        upper_name = full_name.upper()
        for candidate in (b"DXT1", b"DXT3", b"DXT5"):
            if candidate.decode("ascii") in upper_name:
                fourcc = candidate
                break
        if fourcc is None:
            fourcc = DDS_FOURCC_BY_PIXEL_FORMAT.get(pixel_format)
        if fourcc is None:
            continue

        pos = pixel_data + 44
        mips = []
        ok = True
        for _ in range(mip_count):
            if pos + 12 > len(buf):
                ok = False
                break
            offset, width, height = struct.unpack_from("<III", buf, pos)
            pos += 12
            if width <= 0 or height <= 0 or width > 8192 or height > 8192:
                ok = False
                break
            mips.append((offset, width, height))
        if not ok or pos + 8 > len(buf):
            continue
        total_a, total_size = struct.unpack_from("<II", buf, pos)
        data_start = pos + 8
        data_end = data_start + total_size
        if total_size <= 0 or total_size > 256 * 1024 * 1024 or data_end > len(buf):
            continue
        width = mips[0][1]
        height = mips[0][2]
        header = dds_header(width, height, fourcc, mip_count, dxt_linear_size(width, height, fourcc))
        return {
            "name": full_name,
            "data_ref": data_ref,
            "pixel_format": pixel_format,
            "pixel_layout": pixel_layout,
            "mipmap_format": mipmap_format,
            "alpha_format": alpha_format,
            "is_static": is_static,
            "direct_render": direct_render,
            "width": width,
            "height": height,
            "mip_count": mip_count,
            "total_size": total_size,
            "dds": header + buf[data_start:data_end],
        }
    return None


def extract_embedded_texture_for_obj(
    nif_path: Path,
    texture_ref: str,
    out_dir: Path,
    used: set[str],
    convert_to_png: bool = True,
) -> tuple[str, Path, str] | None:
    embedded = parse_embedded_texture(nif_path.read_bytes(), texture_ref)
    if not embedded:
        return None
    name = unique_target_name(out_dir, Path(texture_ref).with_suffix(".dds").name, used)
    dds_path = out_dir / name
    if not dds_path.exists():
        dds_path.write_bytes(embedded["dds"])

    if convert_to_png and Image is not None:
        try:
            png_name = unique_target_name(out_dir, dds_path.with_suffix(".png").name, used)
            png_path = out_dir / png_name
            if not png_path.exists():
                img = Image.open(dds_path)
                img.save(png_path)
            return png_name, png_path, "embedded_nipixeldata_png"
        except Exception:
            pass
    return name, dds_path, "embedded_nipixeldata_dds"


def copy_texture_for_obj(source: Path, out_dir: Path, used: set[str], convert_to_png: bool = True) -> tuple[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    if convert_to_png and source.suffix.lower() in {".dds", ".tga", ".bmp", ".jpg", ".jpeg"} and Image is not None:
        name = unique_target_name(out_dir, source.with_suffix(".png").name, used)
        target = out_dir / name
        if not target.exists() or target.stat().st_mtime < source.stat().st_mtime:
            img = Image.open(source)
            img.save(target)
        return name, target

    name = unique_target_name(out_dir, source.name, used)
    target = out_dir / name
    if not target.exists() or target.stat().st_mtime < source.stat().st_mtime:
        shutil.copy2(source, target)
    return name, target


def read_mtl_materials(mtl_path: Path) -> list[dict[str, str]]:
    mats: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    if not mtl_path.exists():
        return mats
    for line in mtl_path.read_text(encoding="ascii", errors="ignore").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("newmtl "):
            current = {"name": stripped.split(None, 1)[1], "map_kd": "", "kd": ""}
            mats.append(current)
        elif current is not None and stripped.lower().startswith("map_kd "):
            current["map_kd"] = stripped.split(None, 1)[1]
        elif current is not None and stripped.lower().startswith("kd "):
            current["kd"] = stripped.split(None, 1)[1]
    return mats


def obj_object_names(obj_path: Path) -> list[str]:
    names: list[str] = []
    if not obj_path.exists():
        return names
    for line in obj_path.read_text(encoding="ascii", errors="ignore").splitlines():
        stripped = line.strip()
        if stripped.startswith("o ") or stripped.startswith("g "):
            name = stripped.split(None, 1)[1] if len(stripped.split(None, 1)) > 1 else ""
            if name:
                names.append(name)
    return names


def obj_material_shapes(obj_path: Path) -> dict[str, str]:
    """Return material name -> first OBJ object/group that uses it.

    OBJ exporters can legitimately skip materials for untextured/collision
    meshes. Pairing MTL rows with object rows by ordinal drifts after that, so
    texture rewrite must use the actual `usemtl` records.
    """
    out: dict[str, str] = {}
    if not obj_path.exists():
        return out
    current = ""
    for line in obj_path.read_text(encoding="ascii", errors="ignore").splitlines():
        stripped = line.strip()
        if stripped.startswith("o ") or stripped.startswith("g "):
            parts = stripped.split(None, 1)
            current = parts[1] if len(parts) > 1 else ""
        elif stripped.startswith("usemtl "):
            parts = stripped.split(None, 1)
            mat = parts[1] if len(parts) > 1 else ""
            if mat and current and mat not in out:
                out[mat] = current
    return out


def luo_texture_map_for_nif(nif_path: Path) -> tuple[Path | None, dict[str, list[str]], str]:
    import luo_decompiler as ld

    luo = ld.find_part_luo_for_nif(nif_path)
    if not luo:
        return None, {}, ""
    src = ld.decompile_luo(luo)
    if not src:
        return luo, {}, ""
    return luo, ld.extract_texture_map(src), src


def _partsdb_records(resource_root: Path) -> list[object]:
    cache_key = str(resource_root.resolve()).lower()
    if cache_key in _PARTSDB_CACHE:
        return _PARTSDB_CACHE[cache_key]
    fdb = resource_root / "Character" / "PartsDB.fdb"
    if not fdb.exists():
        _PARTSDB_CACHE[cache_key] = []
        return []
    try:
        import partsdb_probe

        _, records = partsdb_probe.decode(fdb, include_t=True)
    except Exception:
        records = []
    _PARTSDB_CACHE[cache_key] = records
    return records


def _record_mesh_rel(record, by_id: dict[int, object]) -> str:
    mesh_source = record if str(record.mesh_file).lower().endswith(".nif") else by_id.get(record.parent_id)
    if mesh_source is None:
        return ""
    return (Path(mesh_source.base_dir.replace("\\", "/")) / mesh_source.mesh_file).as_posix().lower()


def partsdb_texture_map_for_nif(
    nif_path: Path,
    resource_root: Path,
    part_name: str = "",
) -> tuple[dict[str, list[str]], dict]:
    """Return shape -> texture refs from Character/PartsDB.fdb.

    Texture refs are relative to the Character archive root, matching how
    resolve_texture() searches from archive_root_for(nif_path, resource_root).
    """
    records = _partsdb_records(resource_root)
    if not records:
        return {}, {}
    by_id = {r.part_id: r for r in records}
    try:
        rel = nif_path.resolve().relative_to((resource_root / "Character").resolve()).as_posix().lower()
    except ValueError:
        return {}, {}

    if part_name:
        wanted = part_name.lower()
        named = [r for r in records if r.name.lower() == wanted or str(r.part_id) == wanted]
        same_file = [
            r for r in named
            if Path(_record_mesh_rel(r, by_id)).name == nif_path.name.lower()
        ]
        candidates = same_file or named
    else:
        candidates = [r for r in records if _record_mesh_rel(r, by_id) == rel]
        # For automatic export, prefer the base L record that physically owns
        # this mesh. T records are texture replacement variants and are better
        # selected explicitly with --part.
        base = [
            r for r in candidates
            if r.kind == "L"
            and (Path(r.base_dir.replace("\\", "/")) / r.mesh_file).as_posix().lower() == rel
        ]
        if base:
            candidates = base
    if not candidates:
        return {}, {"candidates": []}

    selected = candidates[0]
    parent = by_id.get(selected.parent_id)
    texture_base_dir = selected.base_dir
    if selected.kind == "T" and parent is not None:
        texture_base_dir = parent.base_dir

    tex_map: dict[str, list[str]] = {}
    for item in selected.texture_shapes:
        refs = []
        for tex in item.textures:
            refs.append((Path(texture_base_dir.replace("\\", "/")) / tex.replace("\\", "/")).as_posix())
        tex_map[item.shape] = refs

    meta = {
        "part_id": selected.part_id,
        "part_name": selected.name,
        "part_kind": selected.kind,
        "parent_id": selected.parent_id,
        "parent_name": parent.name if parent else "",
        "candidate_count": len(candidates),
        "candidates": [
            {"part_id": r.part_id, "name": r.name, "kind": r.kind, "parent_id": r.parent_id}
            for r in candidates[:50]
        ],
    }
    return tex_map, meta


def find_partsdb_record(resource_root: Path, part_name: str):
    records = _partsdb_records(resource_root)
    wanted = part_name.lower()
    for record in records:
        if record.name.lower() == wanted or str(record.part_id) == wanted:
            return record
    return None


def mesh_path_for_partsdb_record(resource_root: Path, record) -> Path | None:
    records = _partsdb_records(resource_root)
    by_id = {r.part_id: r for r in records}
    mesh_source = record if str(record.mesh_file).lower().endswith(".nif") else by_id.get(record.parent_id)
    if mesh_source is None:
        return None
    rel = Path(mesh_source.base_dir.replace("\\", "/")) / mesh_source.mesh_file
    path = resource_root / "Character" / rel
    return path if path.exists() else None


def rewrite_mtl_with_real_textures(
    obj_path: Path,
    nif_path: Path,
    resource_root: Path = DEFAULT_RESOURCE_ROOT,
    convert_to_png: bool = True,
    part_name: str = "",
) -> dict:
    mtl_path = obj_path.with_suffix(".mtl")
    mats = read_mtl_materials(mtl_path)
    if not mats:
        return {"materials": 0, "resolved": 0, "rows": [], "luo": "", "texture_map": {}}

    shape_names = obj_object_names(obj_path) or shape_names_in_nif(nif_path)
    material_shapes = obj_material_shapes(obj_path)
    luo, tex_map, _ = luo_texture_map_for_nif(nif_path)
    partsdb_meta: dict = {}
    texture_map_source = "luo" if tex_map else ""
    if not tex_map:
        tex_map, partsdb_meta = partsdb_texture_map_for_nif(nif_path, resource_root, part_name)
        if tex_map:
            texture_map_source = "partsdb"
    tex_map_ci = {k.lower(): v for k, v in tex_map.items()}
    by_base, by_rel = texture_indices(resource_root)
    used: set[str] = set()
    copied_cache: dict[str, tuple[str, str, str, str]] = {}
    rows = []
    resolved_count = 0

    with mtl_path.open("w", encoding="ascii", newline="\n", errors="ignore") as handle:
        handle.write("# Rewritten by qqxj_asset_pipeline.py\n")
        for i, mat in enumerate(mats):
            mat_name = mat["name"]
            is_debug_collision = "collision_debug" in mat_name.lower()
            shape = material_shapes.get(mat_name, shape_names[i] if i < len(shape_names) else "")
            refs: list[str] = []
            method = ""
            if is_debug_collision:
                method = "debug_collision"
            elif shape and shape.lower() in tex_map_ci:
                refs = tex_map_ci[shape.lower()]
                method = f"{texture_map_source}_shape"
            elif mat.get("map_kd"):
                refs = [mat["map_kd"]]
                method = "nif_mtl"

            chosen_ref = ""
            attempted_ref = refs[0] if refs else ""
            source = None
            source_method = ""
            new_ref = ""
            copied_to = ""
            for ref in refs:
                cache_key = ref.replace("\\", "/").lower()
                if cache_key in copied_cache:
                    chosen_ref = ref
                    new_ref, copied_to, source_method, source_text = copied_cache[cache_key]
                    source = Path(source_text) if source_text else None
                    resolved_count += 1
                    break
                source, source_method = resolve_texture(
                    ref, nif_path, resource_root, by_base, by_rel, allow_index=False
                )
                if source:
                    chosen_ref = ref
                    new_ref, target = copy_texture_for_obj(source, obj_path.parent, used, convert_to_png)
                    copied_to = str(target)
                    copied_cache[cache_key] = (new_ref, copied_to, source_method, str(source))
                    resolved_count += 1
                    break
                embedded = extract_embedded_texture_for_obj(nif_path, ref, obj_path.parent, used, convert_to_png)
                if embedded:
                    chosen_ref = ref
                    new_ref, target, source_method = embedded
                    copied_to = str(target)
                    source = nif_path
                    copied_cache[cache_key] = (new_ref, copied_to, source_method, str(source))
                    resolved_count += 1
                    break
                source, source_method = resolve_texture(
                    ref, nif_path, resource_root, by_base, by_rel, allow_index=True
                )
                if source:
                    chosen_ref = ref
                    new_ref, target = copy_texture_for_obj(source, obj_path.parent, used, convert_to_png)
                    copied_to = str(target)
                    copied_cache[cache_key] = (new_ref, copied_to, source_method, str(source))
                    resolved_count += 1
                    break

            handle.write(f"newmtl {mat_name}\n")
            if is_debug_collision:
                handle.write("Ka 1 0 0\nKd 1 0 0\nKs 0 0 0\nd 1\nillum 1\n")
            else:
                handle.write("Ka 1 1 1\nKd 1 1 1\nKs 0 0 0\nd 1\nillum 2\n")
            if new_ref:
                handle.write(f"map_Kd {new_ref}\n")
                if exported_texture_has_alpha(copied_to, source):
                    handle.write(f"map_d {new_ref}\n")
            handle.write("\n")
            rows.append({
                "material_index": i,
                "material": mat_name,
                "shape": shape,
                "method": method,
                "texture_ref": chosen_ref or attempted_ref,
                "source_method": source_method,
                "source": str(source) if source else "",
                "new_ref": new_ref,
                "copied_to": copied_to,
                "resolved": int(bool(source)),
            })

    return {
        "materials": len(mats),
        "resolved": resolved_count,
        "rows": rows,
        "luo": str(luo) if luo else "",
        "texture_map": tex_map,
        "texture_map_source": texture_map_source,
        "partsdb": partsdb_meta,
        "shape_names": shape_names,
    }


def _read_export_info(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _export_cache_valid(
    info_path: Path,
    nif_path: Path,
    resource_root: Path,
    convert_to_png: bool,
    part_name: str,
    include_controller_metadata: bool,
) -> dict | None:
    info = _read_export_info(info_path)
    if not info or not info.get("ok"):
        return None
    cache = info.get("cache", {})
    if cache.get("nif") != str(nif_path):
        return None
    if cache.get("resource_root") != str(resource_root):
        return None
    if bool(cache.get("convert_to_png", True)) != bool(convert_to_png):
        return None
    if cache.get("part_name", "") != part_name:
        return None
    if bool(cache.get("include_controller_metadata", False)) != bool(include_controller_metadata):
        return None
    if float(cache.get("nif_mtime", -1)) != nif_path.stat().st_mtime:
        return None
    required = [
        info.get("obj", ""),
        info.get("mtl", ""),
        info.get("texture_report", ""),
        info.get("texture_aliases", ""),
    ]
    if not all(p and Path(p).exists() for p in required):
        return None
    info["cached"] = 1
    return info


def export_nif_to_obj(
    nif_path: Path,
    out_dir: Path | None = None,
    resource_root: Path = DEFAULT_RESOURCE_ROOT,
    convert_to_png: bool = True,
    part_name: str = "",
    use_cache: bool = True,
    include_controller_metadata: bool = False,
) -> dict:
    nif_path = nif_path.resolve()
    resource_root = resource_root.resolve()
    out_dir = (out_dir or default_export_dir(nif_path, resource_root)).resolve()
    assert_inside_project(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    obj_path = out_dir / f"{safe_name(nif_path.stem)}.obj"
    manifest = out_dir / "obj_export_manifest.csv"
    info_path = out_dir / "export_info.json"

    if use_cache:
        cached = _export_cache_valid(
            info_path, nif_path, resource_root, convert_to_png, part_name, include_controller_metadata
        )
        if cached is not None:
            return cached

    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "nextsoft_nif_to_obj.py"),
        str(nif_path),
        str(obj_path),
        "--manifest",
        str(manifest),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0 or not obj_path.exists():
        return {
            "ok": 0,
            "obj": str(obj_path),
            "error": result.stderr or result.stdout or "OBJ export failed",
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    obj_stats: dict[str, str] = {}
    if manifest.exists():
        try:
            with manifest.open("r", newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
                if rows:
                    obj_stats = rows[0]
        except Exception:
            obj_stats = {}

    tex_result = rewrite_mtl_with_real_textures(obj_path, nif_path, resource_root, convert_to_png, part_name)
    controller_meta = {"skipped": not include_controller_metadata}
    texture_binding = {"skipped": not include_controller_metadata}
    if include_controller_metadata:
        try:
            import nextsoft_texture_controller_probe as controller_probe
            controller_meta = controller_probe.find_texture_controllers(nif_path)
            (out_dir / "texture_controllers.json").write_text(
                json.dumps(controller_meta, indent=2),
                encoding="utf-8",
            )
            texture_binding = controller_probe.bind_texture_animation(nif_path)
            (out_dir / "texture_animation_binding.json").write_text(
                json.dumps(texture_binding, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            controller_meta = {"error": str(exc)}
            texture_binding = {"error": str(exc)}
    texture_report = out_dir / "texture_resolution.csv"
    with texture_report.open("w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "material_index",
            "material",
            "shape",
            "method",
            "texture_ref",
            "source_method",
            "source",
            "new_ref",
            "copied_to",
            "resolved",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(tex_result["rows"])
    alias_report = out_dir / "texture_aliases.csv"
    with alias_report.open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["texture_ref", "new_ref", "source", "copied_to"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        seen_aliases: set[tuple[str, str]] = set()
        for row in tex_result["rows"]:
            key = (row.get("texture_ref", ""), row.get("new_ref", ""))
            if not key[0] or not key[1] or key in seen_aliases:
                continue
            seen_aliases.add(key)
            writer.writerow({
                "texture_ref": key[0],
                "new_ref": key[1],
                "source": row.get("source", ""),
                "copied_to": row.get("copied_to", ""),
            })

    info = {
        "ok": 1,
        "nif": str(nif_path),
        "obj": str(obj_path),
        "mtl": str(obj_path.with_suffix(".mtl")),
        "texture_report": str(texture_report),
        "texture_aliases": str(alias_report),
        "materials": tex_result["materials"],
        "textures_resolved": tex_result["resolved"],
        "obj_stats": obj_stats,
        "uv_transforms": int(obj_stats.get("uv_transforms", 0) or 0),
        "texture_transform_controllers": len(controller_meta.get("texture_transform_controllers", [])),
        "flip_controllers": len(controller_meta.get("flip_controllers", [])),
        "texture_controller_report": str(out_dir / "texture_controllers.json"),
        "texture_animation_binding": str(out_dir / "texture_animation_binding.json"),
        "texture_animation_kf": texture_binding.get("kf", ""),
        "luo": tex_result["luo"],
        "texture_map_source": tex_result.get("texture_map_source", ""),
        "partsdb": tex_result.get("partsdb", {}),
        "shape_names": tex_result.get("shape_names", []),
        "cached": 0,
        "cache": {
            "nif": str(nif_path),
            "resource_root": str(resource_root),
            "convert_to_png": bool(convert_to_png),
            "part_name": part_name,
            "include_controller_metadata": bool(include_controller_metadata),
            "nif_mtime": nif_path.stat().st_mtime,
        },
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    info_path.write_text(json.dumps(info, indent=2), encoding="utf-8")
    write_blender_import_script(obj_path)
    return info


def export_part_to_obj(
    part_name: str,
    out_dir: Path | None = None,
    resource_root: Path = DEFAULT_RESOURCE_ROOT,
    convert_to_png: bool = True,
    use_cache: bool = True,
    include_controller_metadata: bool = False,
) -> dict:
    resource_root = resource_root.resolve()
    record = find_partsdb_record(resource_root, part_name)
    if record is None:
        return {"ok": 0, "error": f"PartsDB part not found: {part_name}"}
    mesh = mesh_path_for_partsdb_record(resource_root, record)
    if mesh is None:
        return {
            "ok": 0,
            "error": f"mesh not found for PartsDB part: {record.name}",
            "part_id": record.part_id,
            "mesh_file": record.mesh_file,
            "base_dir": record.base_dir,
        }
    if out_dir is None:
        out_dir = DEFAULT_EXPORT_ROOT / "parts" / safe_name(record.name)
    result = export_nif_to_obj(
        mesh,
        out_dir=out_dir,
        resource_root=resource_root,
        convert_to_png=convert_to_png,
        part_name=record.name,
        use_cache=use_cache,
        include_controller_metadata=include_controller_metadata,
    )
    result["part"] = {
        "part_id": record.part_id,
        "name": record.name,
        "kind": record.kind,
        "parent_id": record.parent_id,
        "mesh": str(mesh),
    }
    return result


def write_blender_import_script(obj_path: Path) -> Path:
    obj_path = obj_path.resolve()
    script_path = obj_path.parent / "open_in_blender_with_textures.py"
    script_path.write_text(textwrap.dedent(f"""
        import bpy
        import os
        from pathlib import Path

        obj_path = Path({str(obj_path)!r})
        os.chdir(obj_path.parent)
        bpy.ops.object.select_all(action='SELECT')
        bpy.ops.object.delete()
        bpy.ops.wm.obj_import(filepath=str(obj_path))

        material_images = {{}}
        current = ''
        mtl_path = obj_path.with_suffix('.mtl')
        if mtl_path.exists():
            for raw in mtl_path.read_text(errors='ignore').splitlines():
                parts = raw.strip().split(None, 1)
                if not parts:
                    continue
                key = parts[0].lower()
                if key == 'newmtl' and len(parts) == 2:
                    current = parts[1]
                elif key == 'map_kd' and current and len(parts) == 2:
                    image_path = (obj_path.parent / parts[1].strip()).resolve()
                    if image_path.exists():
                        material_images[current] = image_path

        for mat in bpy.data.materials:
            mat.use_nodes = True
            mat.blend_method = 'BLEND'
            mat.show_transparent_back = True
            bsdf = mat.node_tree.nodes.get('Principled BSDF')
            if not bsdf:
                continue
            tex = None
            for node in mat.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    tex = node
                    break
            if tex is None and mat.name in material_images:
                tex = mat.node_tree.nodes.new('ShaderNodeTexImage')
                tex.image = bpy.data.images.load(str(material_images[mat.name]), check_existing=True)
            if tex is None:
                continue
            if not any(link.to_node == bsdf and link.to_socket == bsdf.inputs['Base Color']
                       for link in mat.node_tree.links):
                mat.node_tree.links.new(tex.outputs['Color'], bsdf.inputs['Base Color'])
            if 'Alpha' in tex.outputs and 'Alpha' in bsdf.inputs:
                if not any(link.to_node == bsdf and link.to_socket == bsdf.inputs['Alpha']
                           for link in mat.node_tree.links):
                    mat.node_tree.links.new(tex.outputs['Alpha'], bsdf.inputs['Alpha'])

        bpy.ops.wm.save_as_mainfile(filepath=str(obj_path.with_suffix('.blend')))
    """).strip() + "\n", encoding="utf-8")
    return script_path


def find_blender() -> Path | None:
    for cand in [
        Path("C:/Program Files/Blender Foundation/Blender 4.5/blender.exe"),
        Path("C:/Program Files/Blender Foundation/Blender 4.3/blender.exe"),
        Path("C:/Program Files/Blender Foundation/Blender 5.1/blender.exe"),
    ]:
        if cand.exists():
            return cand
    return None


def create_blend_for_obj(obj_path: Path, timeout: int = 120) -> tuple[Path | None, str]:
    obj_path = obj_path.resolve()
    blender = find_blender()
    if blender is None:
        return None, "Blender not found"
    script = write_blender_import_script(obj_path)
    proc = subprocess.run(
        [str(blender), "--background", "--python", str(script)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    blend = obj_path.with_suffix(".blend")
    if proc.returncode == 0 and blend.exists():
        return blend, proc.stdout
    return None, proc.stderr or proc.stdout or "Blender import failed"


def resolve_animation_asset(anchor: Path, ref: str, resource_root: Path) -> Path | None:
    ref = ref.replace("\\", "/").strip()
    if not ref:
        return None
    rel = Path(ref)
    candidates = [
        anchor.parent / rel,
        archive_root_for(anchor, resource_root) / rel,
        resource_root / rel,
        resource_root / anchor.parent.name / rel,
    ]
    for cand in candidates:
        if cand.exists():
            return cand.resolve()

    basename = rel.name.lower()
    suffix = rel.suffix.lower()
    if suffix:
        local = sorted(anchor.parent.rglob(rel.name), key=lambda p: (len(p.parts), str(p).lower()))
        if local:
            return local[0].resolve()
        matches = [
            p for p in resource_root.rglob(f"*{suffix}")
            if p.name.lower() == basename
        ]
        if matches:
            matches.sort(key=lambda p: (0 if anchor.parent in p.parents else 1, len(p.parts), str(p).lower()))
            return matches[0].resolve()
    return None


def export_luo(luo_path: Path, out_dir: Path | None = None) -> dict:
    import luo_decompiler as ld

    out_dir = (out_dir or default_export_dir(luo_path, DEFAULT_RESOURCE_ROOT)).resolve()
    assert_inside_project(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    src = ld.decompile_luo(luo_path)
    if src:
        lua_path = out_dir / f"{safe_name(luo_path.stem)}.lua"
        lua_path.write_text(src, encoding="utf-8", errors="replace")
        meta = ld.extract_part_meta(src)
    else:
        lua_path = out_dir / f"{safe_name(luo_path.stem)}_summary.txt"
        text, enc = decode_text(luo_path.read_bytes())
        lua_path.write_text(text, encoding="utf-8", errors="replace")
        meta = {"decompiled": False, "encoding": enc}
    (out_dir / "luo_meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": 1, "output": str(lua_path), "meta": meta}


def export_kfm(kfm_path: Path, out_dir: Path | None = None, resource_root: Path = DEFAULT_RESOURCE_ROOT) -> dict:
    import nextsoft_kfm_parser as kfm_mod

    out_dir = (out_dir or default_export_dir(kfm_path, resource_root)).resolve()
    assert_inside_project(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = kfm_path.read_bytes()
    if kfm_path.suffix.lower() == ".kfm":
        parsed = kfm_mod.parse_kfm(raw)
        summary = kfm_mod.summarize_kfm(parsed)
        base_nif = resolve_animation_asset(kfm_path, parsed.nif_name, resource_root)
        animation_bindings = []
        track_rows = []
        try:
            import nextsoft_kf_transform_probe as transform_probe
        except Exception:
            transform_probe = None
        for anim in parsed.animations:
            kf_path = resolve_animation_asset(kfm_path, anim.kf_file, resource_root)
            binding = {
                "name": anim.name,
                "kf_file": anim.kf_file,
                "resolved_kf": str(kf_path) if kf_path else "",
                "raw_extra": anim.raw_extra,
                "transform_binding": {},
            }
            if kf_path and transform_probe is not None:
                try:
                    binding["transform_binding"] = transform_probe.probe_transform_tracks(kf_path)
                except Exception as exc:
                    binding["transform_binding"] = {"error": str(exc)}
            animation_bindings.append(binding)
            for item in binding.get("transform_binding", {}).get("bound_transform_tracks", []):
                cb = item.get("controlled_block", {})
                track = item.get("track") or {}
                track_rows.append({
                    "animation": anim.name,
                    "kf_file": str(kf_path) if kf_path else "",
                    "node": cb.get("node_name", ""),
                    "controller_type": cb.get("controller_type", ""),
                    "interpolator_ref": cb.get("interpolator_ref", ""),
                    "data_ref": item.get("guessed_data_ref", ""),
                    "binding_method": item.get("binding_method", ""),
                    "has_track": int(bool(track)),
                    "rotation_keys": len(track.get("rotation_keys", [])) if track else 0,
                    "translation_keys": len(track.get("translation_keys", [])) if track else 0,
                    "scale_keys": len(track.get("scale_keys", [])) if track else 0,
                })
        data = {
            "type": "kfm",
            "path": str(kfm_path),
            "summary": summary,
            "kfm": {
                "version_line": parsed.version_line,
                "nif_name": parsed.nif_name,
                "resolved_nif": str(base_nif) if base_nif else "",
                "base_anim_id": parsed.base_anim_id,
                "fade_in": parsed.fade_in,
                "fade_out": parsed.fade_out,
                "extras": parsed.extras,
                "animations": [asdict(anim) for anim in parsed.animations],
            },
            "animation_bindings": animation_bindings,
        }
        rows = [{"name": a.name, "kf_file": a.kf_file, "raw_extra": ";".join(map(str, a.raw_extra))} for a in parsed.animations]
    else:
        summary = kfm_mod.summarize_kf(raw)
        texture_animation = {}
        try:
            import nextsoft_texture_controller_probe as controller_probe
            texture_animation = controller_probe.find_texture_controllers(kfm_path)
        except Exception as exc:
            texture_animation = {"error": str(exc)}
        try:
            import nextsoft_kf_transform_probe as transform_probe
            transform_tracks = transform_probe.probe_transform_tracks(kfm_path)
        except Exception as exc:
            transform_tracks = {"error": str(exc)}
        data = {
            "type": "kf",
            "path": str(kfm_path),
            "summary": summary,
            "texture_animation": texture_animation,
            "transform_tracks": transform_tracks,
        }
        rows = []

    (out_dir / f"{safe_name(kfm_path.stem)}_animation_summary.txt").write_text(summary, encoding="utf-8")
    (out_dir / f"{safe_name(kfm_path.stem)}_animation.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    if rows:
        with (out_dir / f"{safe_name(kfm_path.stem)}_animations.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["name", "kf_file", "raw_extra"])
            writer.writeheader()
            writer.writerows(rows)
    if kfm_path.suffix.lower() == ".kfm":
        with (out_dir / f"{safe_name(kfm_path.stem)}_transform_tracks.csv").open("w", newline="", encoding="utf-8") as f:
            fieldnames = [
                "animation",
                "kf_file",
                "node",
                "controller_type",
                "interpolator_ref",
                "data_ref",
                "binding_method",
                "has_track",
                "rotation_keys",
                "translation_keys",
                "scale_keys",
            ]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(track_rows)
    return {"ok": 1, "output": str(out_dir), "summary": summary}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="QQXJ asset export pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)

    nif = sub.add_parser("export-nif", help="export NIF to OBJ with copied textures")
    nif.add_argument("nif", type=Path)
    nif.add_argument("--out", type=Path)
    nif.add_argument("--resource-root", type=Path, default=DEFAULT_RESOURCE_ROOT)
    nif.add_argument("--keep-original-texture-format", action="store_true")
    nif.add_argument("--part", default="", help="optional PartsDB part name or id for Character texture variants")
    nif.add_argument("--force", action="store_true", help="ignore export_info.json cache and rebuild")
    nif.add_argument(
        "--with-controller-metadata",
        action="store_true",
        help="also scan slow texture-controller metadata for animation research",
    )

    part = sub.add_parser("export-part", help="export a Character PartsDB part by name/id")
    part.add_argument("part", help="PartsDB part name or numeric id")
    part.add_argument("--out", type=Path)
    part.add_argument("--resource-root", type=Path, default=DEFAULT_RESOURCE_ROOT)
    part.add_argument("--keep-original-texture-format", action="store_true")
    part.add_argument("--force", action="store_true", help="ignore export_info.json cache and rebuild")
    part.add_argument(
        "--with-controller-metadata",
        action="store_true",
        help="also scan slow texture-controller metadata for animation research",
    )

    luo = sub.add_parser("export-luo", help="decompile/summarize LUO")
    luo.add_argument("luo", type=Path)
    luo.add_argument("--out", type=Path)

    anim = sub.add_parser("export-anim", help="export KFM/KF metadata")
    anim.add_argument("path", type=Path)
    anim.add_argument("--out", type=Path)
    anim.add_argument("--resource-root", type=Path, default=DEFAULT_RESOURCE_ROOT)

    args = ap.parse_args(argv)
    if args.cmd == "export-nif":
        result = export_nif_to_obj(
            args.nif,
            args.out,
            args.resource_root,
            convert_to_png=not args.keep_original_texture_format,
            part_name=args.part,
            use_cache=not args.force,
            include_controller_metadata=args.with_controller_metadata,
        )
    elif args.cmd == "export-luo":
        result = export_luo(args.luo, args.out)
    elif args.cmd == "export-part":
        result = export_part_to_obj(
            args.part,
            args.out,
            args.resource_root,
            convert_to_png=not args.keep_original_texture_format,
            use_cache=not args.force,
            include_controller_metadata=args.with_controller_metadata,
        )
    else:
        result = export_kfm(args.path, args.out, args.resource_root)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
