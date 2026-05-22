#!/usr/bin/env python3
"""
Heuristic OBJ exporter for QQxj/Nextsoft Gamebryo NIF files.

This intentionally avoids NifSkope/PyFFI. The client NIFs are a Nextsoft
variant, and NifSkope rejects some block bodies. This tool scans for the
NiTriStripsData-style payloads we can already recognize:

    u16 vertex_count
    00 00 01
    vertex_count * Vector3<float32>

It then searches nearby for a plausible uint16 triangle-strip index run and
writes an OBJ. This is a mesh recovery tool, not a perfect NIF parser yet.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MIN_VERTS = 3
MAX_VERTS = 200_000


@dataclass
class Mesh:
    source_offset: int
    vertices: list[tuple[float, float, float]]
    indices: list[int]
    index_offset: int
    uvs: list[tuple[float, float]]
    score: float
    texture: str = ""
    triangles: list[tuple[int, int, int]] | None = None
    name: str = ""
    is_collision: bool = False
    uv_transform: Any = None


@dataclass
class IndexRun:
    offset: int
    indices: list[int]


def f32(buf: bytes, off: int) -> float:
    return struct.unpack_from("<f", buf, off)[0]


def u16(buf: bytes, off: int) -> int:
    return struct.unpack_from("<H", buf, off)[0]


def is_reasonable_vertex(v: tuple[float, float, float]) -> bool:
    return all(math.isfinite(x) and abs(x) < 1_000_000 for x in v)


def bbox(vertices: list[tuple[float, float, float]]):
    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    zs = [v[2] for v in vertices]
    return (
        (min(xs), min(ys), min(zs)),
        (max(xs), max(ys), max(zs)),
    )


def bbox_score(vertices: list[tuple[float, float, float]]) -> float:
    lo, hi = bbox(vertices)
    spans = [hi[i] - lo[i] for i in range(3)]
    if max(spans) <= 1e-6:
        return -100.0
    if max(spans) > 500_000:
        return -100.0
    nonzero_axes = sum(1 for s in spans if s > 1e-5)
    return nonzero_axes + min(len(vertices), 5000) / 5000.0


def read_vertices(buf: bytes, count_off: int) -> list[tuple[float, float, float]] | None:
    count = u16(buf, count_off)
    if count < MIN_VERTS or count > MAX_VERTS:
        return None
    # Nextsoft's Gamebryo files place the Has Vertices flag at +4 in
    # the NiGeometryData body. The older strict three-byte check rejected
    # any vertex count >= 256 because the high byte lives at +3.
    if count_off + 5 > len(buf) or buf[count_off + 4] != 0x01:
        return None

    start = count_off + 5
    end = start + count * 12
    if end > len(buf):
        return None

    vertices = []
    for i in range(count):
        v = (f32(buf, start + i * 12), f32(buf, start + i * 12 + 4), f32(buf, start + i * 12 + 8))
        if not is_reasonable_vertex(v):
            return None
        vertices.append(v)

    if bbox_score(vertices) < 0:
        return None
    return vertices


def strip_to_faces(indices: list[int]) -> list[tuple[int, int, int]]:
    faces = []
    seen = set()
    for i in range(len(indices) - 2):
        a, b, c = indices[i], indices[i + 1], indices[i + 2]
        if a == b or b == c or a == c:
            continue
        key = tuple(sorted((a, b, c)))
        if key in seen:
            continue
        seen.add(key)
        if i % 2:
            faces.append((b, a, c))
        else:
            faces.append((a, b, c))
    return faces


def find_index_run(buf: bytes, search_start: int, vertex_count: int) -> IndexRun:
    best = IndexRun(0, [])
    best_score = -1.0
    search_end = min(len(buf) - 2, search_start + 1200)

    for off in range(search_start, search_end):
        vals = []
        pos = off
        while pos + 2 <= len(buf):
            value = u16(buf, pos)
            if value >= vertex_count:
                break
            vals.append(value)
            pos += 2
            if len(vals) >= min(vertex_count * 4, 65535):
                break

        if len(vals) >= 3 and len(set(vals)) >= 3:
            faces = strip_to_faces(vals)
            if not faces:
                continue
            # The true strip usually follows normals/colors/UVs soon after the
            # vertex array. Later accidental runs can be valid uint16 values
            # from the next block, so distance matters more than raw length.
            unique = len(set(vals))
            distance = off - search_start
            score = unique * 100.0 + min(len(vals), vertex_count * 2) + min(len(faces), 32) * 2.0
            score -= distance / 12.0
            if score > best_score:
                best = IndexRun(off, vals)
                best_score = score

    return best


def uv_candidate_score(values: list[float]) -> float:
    if not values or any(not math.isfinite(x) for x in values):
        return -1.0
    if any(x < -0.25 or x > 1.25 for x in values):
        return -1.0

    pairs = list(zip(values[0::2], values[1::2]))
    us = [p[0] for p in pairs]
    vs = [p[1] for p in pairs]
    if max(us) - min(us) < 0.2 or max(vs) - min(vs) < 0.2:
        return -1.0
    if len({(round(u, 4), round(v, 4)) for u, v in pairs}) < min(3, len(pairs)):
        return -1.0

    in_unit = sum(1 for x in values if 0.0 <= x <= 1.0)
    not_tiny = sum(1 for x in values if abs(x) > 0.01)
    near_binary = sum(1 for x in values if abs(x) < 0.001 or abs(x - 1.0) < 0.001)
    return in_unit + not_tiny * 0.5 + near_binary * 0.25


def find_uvs(
    buf: bytes,
    vertex_end: int,
    vertex_count: int,
    index_offset: int = 0,
) -> list[tuple[float, float]]:
    need = vertex_count * 2
    if vertex_count < MIN_VERTS or need * 4 > 65536:
        return []

    search_end = index_offset if index_offset > vertex_end else min(len(buf), vertex_end + 700)
    search_end = min(search_end, len(buf) - need * 4)
    if search_end <= vertex_end:
        return []

    best_score = -1.0
    best_values: list[float] = []
    best_off = 0
    for off in range(vertex_end, search_end + 1):
        values = [f32(buf, off + i * 4) for i in range(need)]
        score = uv_candidate_score(values)
        if score < 0:
            continue
        # In these files the useful UV table usually sits shortly before the
        # strip index payload. Prefer later equally-good candidates so earlier
        # float noise near normals/colors does not steal the match.
        if score > best_score or (abs(score - best_score) < 0.001 and off > best_off):
            best_score = score
            best_values = values
            best_off = off

    if best_score < 0:
        return []
    return list(zip(best_values[0::2], best_values[1::2]))


def find_meshes(buf: bytes) -> list[Mesh]:
    structured = find_meshes_structured(buf)
    if structured and sum(len(m.triangles or []) for m in structured) > 0:
        return structured

    meshes: list[Mesh] = []
    occupied: list[tuple[int, int]] = []

    for off in range(0, len(buf) - 7):
        vertices = read_vertices(buf, off)
        if not vertices:
            continue

        v_start = off + 5
        v_end = v_start + len(vertices) * 12
        if any(not (v_end <= a or v_start >= b) for a, b in occupied):
            continue

        index_run = find_index_run(buf, v_end, len(vertices))
        indices = index_run.indices
        uvs = find_uvs(buf, v_end, len(vertices), index_run.offset)
        score = bbox_score(vertices) + len(set(indices)) / max(1, len(vertices))
        if uvs:
            score += 0.25
        if indices and len(strip_to_faces(indices)) >= 1:
            meshes.append(Mesh(off, vertices, indices, index_run.offset, uvs, score))
            occupied.append((v_start, v_end))

    # Remove low-value duplicate-ish scans. Highest score first, then stable offset.
    meshes.sort(key=lambda m: (-m.score, m.source_offset))
    accepted: list[Mesh] = []
    for mesh in meshes:
        lo, hi = bbox(mesh.vertices)
        duplicate = False
        for other in accepted:
            olo, ohi = bbox(other.vertices)
            if len(mesh.vertices) == len(other.vertices) and lo == olo and hi == ohi:
                duplicate = True
                break
        if not duplicate:
            accepted.append(mesh)
    accepted.sort(key=lambda m: m.source_offset)
    return accepted


def find_meshes_structured(buf: bytes) -> list[Mesh]:
    try:
        import nextsoft_nif_parser as scene_parser
        nif = scene_parser.parse_nif(buf)
        collected = scene_parser.collect_meshes(nif)
        meshes_from_scene: list[Mesh] = []
        for item in collected:
            if not item.get("faces"):
                continue
            meshes_from_scene.append(Mesh(
                source_offset=int(item.get("data_offset") or 0),
                vertices=item["vertices"],
                indices=[],
                index_offset=0,
                uvs=item.get("uvs") or [],
                score=2000.0 + len(item["faces"]),
                texture=item.get("texture_filename") or "",
                triangles=item["faces"],
                name=item.get("name") or f"mesh_{item.get('block_index', 0)}",
                is_collision=bool(item.get("is_collision")),
            ))
        if meshes_from_scene:
            return meshes_from_scene
    except Exception:
        pass

    try:
        import nif_parser
    except Exception:
        return []

    try:
        info, header_end = nif_parser.parse_header(buf)
        parsed = nif_parser.find_geometry_data(buf, scan_start=header_end)
    except Exception:
        return []
    tri_data_indices = [
        i for i, ti in enumerate(info.block_type_indices)
        if 0 <= ti < len(info.block_types)
        and info.block_types[ti] == "NiTriStripsData"
    ]
    shape_names = nif_parser.names_in_file(buf)
    meshes: list[Mesh] = []
    for i, parsed_mesh in enumerate(parsed):
        if not parsed_mesh.triangles:
            continue
        is_collision = False
        if i < len(tri_data_indices):
            block_index = tri_data_indices[i]
            nearby = [
                info.block_types[info.block_type_indices[j]]
                for j in range(max(0, block_index - 4), block_index)
                if 0 <= info.block_type_indices[j] < len(info.block_types)
            ]
            is_collision = "NiCollisionData" in nearby
        name = shape_names[i] if i < len(shape_names) else f"mesh_{i:03d}"
        meshes.append(Mesh(
            source_offset=parsed_mesh.block_offset,
            vertices=parsed_mesh.vertices,
            indices=[],
            index_offset=0,
            uvs=parsed_mesh.uvs or [],
            score=1000.0 + len(parsed_mesh.triangles),
            triangles=parsed_mesh.triangles,
            name=name,
            is_collision=is_collision,
        ))
    return meshes


def extract_texture_names(buf: bytes) -> list[str]:
    text = buf.decode("latin1", errors="ignore")
    names = []
    seen = set()
    for match in re.finditer(r"[-A-Za-z0-9_./\\: ]+\.(?:dds|png|tga|bmp|jpg|jpeg)", text, re.I):
        name = match.group(0).split("\x00")[-1]
        name = name.replace("\\", "/").strip()
        if name and name.lower() not in seen:
            names.append(name)
            seen.add(name.lower())
    return names


def iter_texture_refs_with_offsets(buf: bytes) -> list[tuple[int, str]]:
    refs: list[tuple[int, str]] = []
    seen_at: set[tuple[int, str]] = set()
    for match in re.finditer(rb"[-A-Za-z0-9_./\\: ]+\.(?:dds|png|tga|bmp|jpg|jpeg)", buf, re.I):
        raw = match.group(0).split(b"\x00")[-1]
        name = raw.decode("latin1", errors="ignore").replace("\\", "/").strip()
        key = (match.start(), name.lower())
        if name and key not in seen_at:
            refs.append((match.start(), name))
            seen_at.add(key)
    return refs


def attach_textures(meshes: list[Mesh], textures: list[str]) -> list[Mesh]:
    if not textures:
        return meshes
    for i, mesh in enumerate(meshes):
        mesh.texture = textures[min(i, len(textures) - 1)]
    return meshes


def attach_textures_by_mesh_segments(buf: bytes, meshes: list[Mesh]) -> list[Mesh]:
    refs = iter_texture_refs_with_offsets(buf)
    if not refs:
        return meshes
    meshes = sorted(meshes, key=lambda m: m.source_offset)
    ref_index = 0
    active_texture = ""
    prev_off = 0
    for mesh in meshes:
        if mesh.texture:
            prev_off = mesh.source_offset
            continue
        if mesh.is_collision:
            mesh.texture = ""
            prev_off = mesh.source_offset
            continue
        if not mesh.uvs:
            mesh.texture = ""
            prev_off = mesh.source_offset
            continue
        segment_refs: list[str] = []
        while ref_index < len(refs) and refs[ref_index][0] < mesh.source_offset:
            off, name = refs[ref_index]
            if off >= prev_off:
                segment_refs.append(name)
            ref_index += 1
        if segment_refs:
            # In QQXJ's 10.2.0.5 files the relevant NiSourceTexture record is
            # usually the last texture string before the geometry data. If the
            # following mesh omits a texture ref, keep this one active because
            # repeated effect/prop meshes commonly share one source texture.
            active_texture = segment_refs[-1]
        mesh.texture = active_texture
        prev_off = mesh.source_offset
    return meshes


def attach_uv_transforms(buf: bytes, meshes: list[Mesh]) -> list[Mesh]:
    try:
        import nextsoft_texture_transform_probe as tx_probe
    except Exception:
        return meshes
    try:
        transforms = tx_probe.find_transforms_from_bytes(buf)
    except Exception:
        return meshes
    if not transforms:
        return meshes
    for mesh in meshes:
        if not mesh.texture or not mesh.uvs:
            continue
        tex_norm = mesh.texture.replace("\\", "/").lower()
        tex_base = Path(tex_norm).name
        candidates = [
            tx for tx in transforms
            if tx.has_transform
            and not tx.is_identity
            and tx.texdesc_offset <= mesh.source_offset
            and (
                tx.texture.lower() == tex_norm
                or Path(tx.texture.lower()).name == tex_base
            )
        ]
        if not candidates:
            continue
        mesh.uv_transform = max(candidates, key=lambda tx: tx.texdesc_offset)
    return meshes


def transform_uv(u: float, v: float, tx) -> tuple[float, float]:
    # NIF TexTransform operates on the original UVs. OBJ's V inversion happens
    # later when writing vt lines. This is the direct matrix form from the TW
    # client `NiTextureTransform::UpdateMatrix` decompile:
    #   Res/TW/ghidra_exports/decompiled/0055de00_FUN_0055de00.c
    tu = float(tx.translate_u)
    tv = float(tx.translate_v)
    angle = float(tx.rotation)
    su = float(tx.scale_u)
    sv = float(tx.scale_v)
    cu = float(tx.center_u)
    cv = float(tx.center_v)
    c = math.cos(angle)
    s = math.sin(angle)
    method = int(tx.method)
    if method == 1:
        m00 = c * su
        m01 = s * su
        m10 = -s * sv
        m11 = c * sv
        mt0 = ((-cu - tu) * c + (tv - cv) * s) * su + cu
        mt1 = (c * (tv - cv) + s * (cu + tu)) * sv + cv
    else:
        m00 = c * su
        m01 = s * su
        m10 = -s * sv
        m11 = c * sv
        mt0 = (tv - cv) * -s + c * (tu - cu) + cu
        mt1 = cv + s * (tu - cu) + c * (tv - cv)
    return m00 * u + m10 * v + mt0, m01 * u + m11 * v + mt1


def write_obj(meshes: list[Mesh], out_path: Path) -> dict[str, int]:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    mtl_path = out_path.with_suffix(".mtl")
    vertex_base = 1
    uv_base = 1
    total_faces = 0
    total_uvs = 0
    materials = []
    for mi, mesh in enumerate(meshes):
        if mesh.is_collision:
            materials.append((f"mat_{mi:03d}_collision_debug", ""))
        elif mesh.texture:
            materials.append((f"mat_{mi:03d}", mesh.texture))

    if materials:
        with mtl_path.open("w", encoding="ascii", newline="\n", errors="ignore") as mtl:
            mtl.write("# Exported by nextsoft_nif_to_obj.py\n")
            for mat_name, tex in materials:
                mtl.write(f"newmtl {mat_name}\n")
                if mat_name.endswith("_collision_debug"):
                    mtl.write("Ka 1 0 0\nKd 1 0 0\nKs 0 0 0\nd 1\nillum 1\n\n")
                else:
                    mtl.write("Kd 1 1 1\n")
                    mtl.write(f"map_Kd {tex}\n\n")

    with out_path.open("w", encoding="ascii", newline="\n") as f:
        f.write("# Exported by nextsoft_nif_to_obj.py\n")
        if materials:
            f.write(f"mtllib {mtl_path.name}\n")
        for mi, mesh in enumerate(meshes):
            obj_name = mesh.name or f"mesh_{mi:03d}_off_{mesh.source_offset}"
            f.write(f"o {obj_name}\n")
            if mesh.is_collision:
                f.write(f"usemtl mat_{mi:03d}_collision_debug\n")
            elif mesh.texture:
                f.write(f"usemtl mat_{mi:03d}\n")
            for x, y, z in mesh.vertices:
                f.write(f"v {x:.8g} {y:.8g} {z:.8g}\n")
            for u, v in mesh.uvs:
                if mesh.uv_transform is not None:
                    u, v = transform_uv(u, v, mesh.uv_transform)
                f.write(f"vt {u:.8g} {1.0 - v:.8g}\n")
            faces = mesh.triangles if mesh.triangles is not None else strip_to_faces(mesh.indices)
            for a, b, c in faces:
                if mesh.uvs and max(a, b, c) < len(mesh.uvs):
                    f.write(
                        f"f {vertex_base + a}/{uv_base + a} "
                        f"{vertex_base + b}/{uv_base + b} "
                        f"{vertex_base + c}/{uv_base + c}\n"
                    )
                else:
                    f.write(f"f {vertex_base + a} {vertex_base + b} {vertex_base + c}\n")
            vertex_base += len(mesh.vertices)
            uv_base += len(mesh.uvs)
            total_faces += len(faces)
            total_uvs += len(mesh.uvs)
    return {
        "meshes": len(meshes),
        "vertices": vertex_base - 1,
        "uvs": total_uvs,
        "faces": total_faces,
        "uv_transforms": sum(1 for mesh in meshes if mesh.uv_transform is not None),
    }


def iter_nifs(path: Path):
    if path.is_file():
        yield path
        return
    for root, _, files in os.walk(path):
        for name in files:
            if name.lower().endswith(".nif"):
                yield Path(root) / name


def convert_one(src: Path, dst: Path) -> dict[str, str | int]:
    data = src.read_bytes()
    meshes = find_meshes(data)
    if any(m.triangles is not None for m in meshes):
        meshes = attach_textures_by_mesh_segments(data, meshes)
    else:
        meshes = attach_textures(meshes, extract_texture_names(data))
    meshes = attach_uv_transforms(data, meshes)
    if meshes:
        stats = write_obj(meshes, dst)
    else:
        stats = {"meshes": 0, "vertices": 0, "uvs": 0, "faces": 0}
    return {
        "source": str(src),
        "output": str(dst),
        "textures": ";".join(extract_texture_names(data)[:16]),
        **stats,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="NIF file or directory")
    ap.add_argument("output", help="OBJ file or output directory")
    ap.add_argument("--limit", type=int, default=0, help="stop after N files")
    ap.add_argument("--manifest", default="", help="optional CSV report path")
    args = ap.parse_args()

    src = Path(args.input)
    dst = Path(args.output)
    files = list(iter_nifs(src))
    if not files:
        print(f"[!] no .nif files found at input path: {src.resolve()}")
        print("[!] If you are inside the Res folder, use .\\extracted_stage1\\World\\nif")
        return 2
    if args.limit:
        files = files[:args.limit]

    rows = []
    ok = 0
    for path in files:
        out_path = dst if src.is_file() else dst / path.relative_to(src).with_suffix(".obj")
        row = convert_one(path, out_path)
        rows.append(row)
        if int(row["faces"]) > 0:
            ok += 1

    if args.manifest:
        mpath = Path(args.manifest)
        mpath.parent.mkdir(parents=True, exist_ok=True)
        with mpath.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["source", "output", "textures", "meshes", "vertices", "uvs", "faces", "uv_transforms"])
            writer.writeheader()
            writer.writerows(rows)

    print(f"[+] converted_with_faces={ok}/{len(rows)} -> {dst}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
