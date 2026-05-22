#!/usr/bin/env python3
"""
Targeted parser for Nextsoft / Gamebryo NIF version 10.2.0.5
(`0x0A020005`).

Strategy: instead of parsing the entire block graph (which would require
implementing dozens of block layouts), we scan for NiGeometryData-style
blocks by their unique header signature, then parse them in full
according to the NifTools wiki layout. This is enough to extract mesh
vertices, normals, UVs, vertex colors, and triangle strips — which is
all the OBJ exporter needs.

Block layout (10.2.0.5, derived from nifxml/nif.xml):

  NiGeometryData (abstract):
    +0   i32   Group ID            (always 0)
    +4   u16   Num Vertices
    +6   u8    Keep Flags
    +7   u8    Compress Flags
    +8   u8    Has Vertices         (must be 1 to embed verts)
    +9   Vector3[N]  Vertices
    +9+12N u16 Data Flags
                low 6 bits = num UV sets; bit 12 = has tangents+bitangents
    +11+12N u8 Has Normals
    [if Has Normals: Vector3[N] Normals;
       optionally Vector3[N] Tangents + Vector3[N] Bitangents]
    Vector3 Center
    f32     Radius
    u8      Has Vertex Colors
    [if HasVC: Color4[N]]
    TexCoord[num_uv_sets][N]
  NiTriBasedGeomData:
    u16 Num Triangles
  NiTriStripsData:
    u16 Num Strips
    u16 StripLengths[Num Strips]
    u8  Has Points
    u16 Points[sum(StripLengths)]
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class NifMesh:
    name: str
    vertices: list[tuple[float, float, float]]
    normals: list[tuple[float, float, float]] | None
    uvs: list[tuple[float, float]] | None
    vertex_colors: list[tuple[float, float, float, float]] | None
    triangles: list[tuple[int, int, int]]
    block_offset: int
    block_index: int = -1
    is_collision: bool = False
    num_strips: int = 0


@dataclass
class NifInfo:
    version_str: str
    version_int: int
    num_blocks: int
    block_types: list[str]
    block_type_indices: list[int]  # block_index -> index into block_types


def parse_header(buf: bytes) -> tuple[NifInfo, int]:
    """Parse the NIF header. Returns (info, offset_after_header)."""
    nl = buf.find(b"\n")
    if nl < 0:
        raise ValueError("no header newline")
    version_str = buf[:nl].decode("latin1", errors="replace")
    pos = nl + 1
    ver, = struct.unpack_from("<I", buf, pos); pos += 4
    # Endian byte (since 20.0.0.4) + user version (since 10.0.1.8). For
    # 10.2.0.5 there's no endian byte but there IS a user_version.
    user_version, = struct.unpack_from("<I", buf, pos); pos += 4
    num_blocks, = struct.unpack_from("<I", buf, pos); pos += 4
    # Block types table: u16 num_types, then sized strings.
    num_types, = struct.unpack_from("<H", buf, pos); pos += 2
    types: list[str] = []
    for _ in range(num_types):
        n, = struct.unpack_from("<I", buf, pos); pos += 4
        types.append(buf[pos:pos + n].decode("ascii", errors="replace"))
        pos += n
    # Block type index table: u16 per block.
    block_type_indices: list[int] = list(
        struct.unpack_from(f"<{num_blocks}H", buf, pos)
    )
    pos += num_blocks * 2
    return NifInfo(version_str, ver, num_blocks, types, block_type_indices), pos


def _read_vec3(buf: bytes, off: int) -> tuple[float, float, float]:
    x, y, z = struct.unpack_from("<3f", buf, off)
    return x, y, z


def _read_color4(buf: bytes, off: int) -> tuple[float, float, float, float]:
    return struct.unpack_from("<4f", buf, off)


def _read_uv(buf: bytes, off: int) -> tuple[float, float]:
    return struct.unpack_from("<2f", buf, off)


def _vec_is_reasonable(v) -> bool:
    return all(math.isfinite(c) and abs(c) < 1e6 for c in v)


def parse_geometry_data_at(buf: bytes, start: int) -> tuple[NifMesh, int] | None:
    """Try to parse a NiTriStripsData (or compatible NiGeometryData
    descendant with strip data) starting at `start`. Returns (mesh,
    bytes_consumed) on success.
    """
    if start + 9 > len(buf):
        return None
    group_id, = struct.unpack_from("<i", buf, start)
    if group_id != 0:
        return None
    num_vertices, = struct.unpack_from("<H", buf, start + 4)
    if num_vertices < 3 or num_vertices > 200_000:
        return None
    keep_flags = buf[start + 6]
    compress_flags = buf[start + 7]
    has_vertices = buf[start + 8]
    if has_vertices != 1:
        return None
    if keep_flags > 0x3F or compress_flags > 0x1F:
        return None
    pos = start + 9
    # Vertices
    if pos + num_vertices * 12 > len(buf):
        return None
    verts: list[tuple[float, float, float]] = []
    for i in range(num_vertices):
        v = _read_vec3(buf, pos + i * 12)
        if not _vec_is_reasonable(v):
            return None
        verts.append(v)
    pos += num_vertices * 12
    if pos + 3 > len(buf):
        return None
    data_flags, = struct.unpack_from("<H", buf, pos); pos += 2
    num_uv_sets = data_flags & 0x3F
    has_tbn = (data_flags & 0x1000) != 0
    if num_uv_sets > 8:
        return None
    has_normals = buf[pos]; pos += 1
    normals: list | None = None
    if has_normals == 1:
        if pos + num_vertices * 12 > len(buf):
            return None
        normals = [_read_vec3(buf, pos + i * 12) for i in range(num_vertices)]
        if not all(_vec_is_reasonable(n) for n in normals):
            return None
        pos += num_vertices * 12
        if has_tbn:
            need = 2 * num_vertices * 12
            if pos + need > len(buf):
                return None
            pos += need
    # Bounding sphere
    if pos + 16 > len(buf):
        return None
    cx, cy, cz, radius = struct.unpack_from("<4f", buf, pos); pos += 16
    if not all(math.isfinite(c) and abs(c) < 1e6 for c in (cx, cy, cz, radius)):
        return None
    # Vertex colors
    if pos + 1 > len(buf):
        return None
    has_vc = buf[pos]; pos += 1
    vcols: list | None = None
    if has_vc == 1:
        if pos + num_vertices * 16 > len(buf):
            return None
        vcols = [_read_color4(buf, pos + i * 16) for i in range(num_vertices)]
        pos += num_vertices * 16
    # UV sets
    uvs: list[tuple[float, float]] | None = None
    if num_uv_sets > 0:
        for s in range(num_uv_sets):
            if pos + num_vertices * 8 > len(buf):
                return None
            set_uvs = [_read_uv(buf, pos + i * 8) for i in range(num_vertices)]
            if s == 0:
                uvs = set_uvs
            pos += num_vertices * 8
    # NiGeometryData always ends with a u16 consistency flag. Missing this
    # field shifts NiTriBasedGeomData by two bytes and makes large strip lists
    # look like degenerate garbage.
    if pos + 2 > len(buf):
        return None
    consistency_flags, = struct.unpack_from("<H", buf, pos)
    pos += 2
    if consistency_flags > 0x8000:
        return None
    # NiTriBasedGeomData: u16 num_triangles
    if pos + 2 > len(buf):
        return None
    num_triangles, = struct.unpack_from("<H", buf, pos); pos += 2
    # NiTriStripsData
    if pos + 2 > len(buf):
        return None
    num_strips, = struct.unpack_from("<H", buf, pos); pos += 2
    if num_strips > 1024:
        return None
    if pos + num_strips * 2 > len(buf):
        return None
    strip_lengths = list(struct.unpack_from(f"<{num_strips}H", buf, pos))
    pos += num_strips * 2
    if pos + 1 > len(buf):
        return None
    has_points = buf[pos]; pos += 1
    triangles: list[tuple[int, int, int]] = []
    if has_points == 1:
        total_pts = sum(strip_lengths)
        if pos + total_pts * 2 > len(buf):
            return None
        ptr = pos
        for strip_len in strip_lengths:
            strip = list(struct.unpack_from(f"<{strip_len}H", buf, ptr))
            ptr += strip_len * 2
            # Validate all indices are within range
            if any(i >= num_vertices for i in strip):
                return None
            # Convert strip to triangles
            for i in range(len(strip) - 2):
                a, b, c = strip[i], strip[i + 1], strip[i + 2]
                if a == b or b == c or a == c:
                    continue
                if i & 1:
                    triangles.append((b, a, c))
                else:
                    triangles.append((a, b, c))
        pos = ptr
    return (
        NifMesh(
            name="", vertices=verts, normals=normals, uvs=uvs,
            vertex_colors=vcols, triangles=triangles,
            block_offset=start, num_strips=num_strips,
        ),
        pos - start,
    )


def find_geometry_data(buf: bytes, scan_start: int = 0) -> list[NifMesh]:
    """Scan the entire buffer for valid NiTriStripsData blocks."""
    meshes: list[NifMesh] = []
    occupied_ends: list[int] = []
    pos = scan_start
    while pos + 9 < len(buf):
        result = parse_geometry_data_at(buf, pos)
        if result is None:
            pos += 1
            continue
        mesh, consumed = result
        end = pos + consumed
        # Skip if it overlaps a previously accepted mesh.
        if any(pos < e and end > meshes[i].block_offset for i, e in enumerate(occupied_ends)):
            pos += 1
            continue
        # Heuristic quality gate: require either at least 1 triangle OR
        # at least 32 vertices (raw point clouds).
        if mesh.triangles or len(mesh.vertices) >= 32:
            meshes.append(mesh)
            occupied_ends.append(end)
            pos = end
        else:
            pos += 1
    return meshes


def names_in_file(buf: bytes) -> list[str]:
    """Return all (length-prefixed) names ending in 'Shape' in file order."""
    import re
    out: list[str] = []
    for m in re.finditer(rb"[A-Za-z0-9_]+Shape", buf):
        off = m.start()
        name_len = m.end() - m.start()
        if off >= 4:
            prefix_len, = struct.unpack_from("<I", buf, off - 4)
            if prefix_len == name_len:
                out.append(m.group().decode("ascii", errors="replace"))
    return out


def parse_nif(path: Path) -> tuple[NifInfo, list[NifMesh]]:
    buf = path.read_bytes()
    info, header_end = parse_header(buf)
    meshes = find_geometry_data(buf, scan_start=header_end)
    tri_data_indices = [
        i for i, ti in enumerate(info.block_type_indices)
        if 0 <= ti < len(info.block_types)
        and info.block_types[ti] == "NiTriStripsData"
    ]
    # Pair mesh data with NiTriStrips block names heuristically (file
    # order).
    shape_names = names_in_file(buf)
    for i, m in enumerate(meshes):
        if i < len(tri_data_indices):
            block_index = tri_data_indices[i]
            m.block_index = block_index
            lo = max(0, block_index - 4)
            nearby = [
                info.block_types[info.block_type_indices[j]]
                for j in range(lo, block_index)
                if 0 <= info.block_type_indices[j] < len(info.block_types)
            ]
            m.is_collision = "NiCollisionData" in nearby
        if i < len(shape_names):
            m.name = shape_names[i]
        else:
            m.name = f"mesh_{i:03d}"
    return info, meshes


if __name__ == "__main__":
    import sys
    for arg in sys.argv[1:]:
        info, meshes = parse_nif(Path(arg))
        print(f"=== {arg} ===")
        print(f"  version: {info.version_str} (0x{info.version_int:08x})")
        print(f"  num_blocks: {info.num_blocks}  unique_types: {len(info.block_types)}")
        print(f"  meshes found: {len(meshes)}")
        for m in meshes:
            print(
                f"    {m.name:<28} verts={len(m.vertices):5d}"
                f" tris={len(m.triangles):5d}"
                f" uvs={'y' if m.uvs else 'n'}"
                f" normals={'y' if m.normals else 'n'}"
                f" colors={'y' if m.vertex_colors else 'n'}"
            )
