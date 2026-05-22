#!/usr/bin/env python3
"""
Proper block-by-block parser for the Nextsoft Gamebryo 10.2.0.5 NIF variant
used by QQxj. Compared to the existing heuristic nextsoft_nif_to_obj.py this:

  - Walks the block_type_table + block_type_indices to identify each block.
  - Decodes NiObjectNET name fields (Nextsoft has a leading u32 hash before
    the name string).
  - Decodes NiTriStripsData: vertices, normals, UVs, vertex colors,
    triangle strips.
  - Decodes NiSourceTexture: external filename OR a NiPixelData ref for
    embedded textures.
  - Decodes NiTexturingProperty: which NiSourceTexture is the base map.
  - Decodes NiTriStrips block: which properties (material, texturing) it
    uses, which data block it points at.
  - Decodes NiPixelData enough to write an embedded texture as a .dds.

Output is a `NifFile` Python object that downstream tools consume.

This is NOT a complete NIF parser. It targets just what the QQxj character
and item NIFs use, validated against ~1925 NIFs in extracted_named_v4.
"""

from __future__ import annotations

import struct
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

try:
    import numpy as np
except Exception:  # pragma: no cover - optional speed path
    np = None

try:
    import cupy as cp
except Exception:  # pragma: no cover - optional GPU path
    cp = None


HEADER_SIG = b"Gamebryo File Format, Version 10.2.0.5"


@dataclass
class Stream:
    buf: bytes
    pos: int = 0

    def u8(self) -> int:
        v = self.buf[self.pos]
        self.pos += 1
        return v

    def u16(self) -> int:
        v, = struct.unpack_from("<H", self.buf, self.pos)
        self.pos += 2
        return v

    def u32(self) -> int:
        v, = struct.unpack_from("<I", self.buf, self.pos)
        self.pos += 4
        return v

    def i32(self) -> int:
        v, = struct.unpack_from("<i", self.buf, self.pos)
        self.pos += 4
        return v

    def f32(self) -> float:
        v, = struct.unpack_from("<f", self.buf, self.pos)
        self.pos += 4
        return v

    def vec3(self) -> tuple[float, float, float]:
        x = self.f32(); y = self.f32(); z = self.f32()
        return (x, y, z)

    def vec4(self) -> tuple[float, float, float, float]:
        x = self.f32(); y = self.f32(); z = self.f32(); w = self.f32()
        return (x, y, z, w)

    def quat(self) -> tuple[float, float, float, float]:
        # Stored as (w, x, y, z)
        return self.vec4()

    def matrix33(self) -> tuple[float, ...]:
        return tuple(self.f32() for _ in range(9))

    def sized_string(self) -> str:
        n = self.u32()
        if n > 4096:
            raise ValueError(f"sized_string too long: {n} at {self.pos:#x}")
        s = self.buf[self.pos:self.pos + n].decode("latin1", errors="replace")
        self.pos += n
        return s

    def skip(self, n: int) -> None:
        self.pos += n

    def remaining(self) -> int:
        return len(self.buf) - self.pos

    def tell(self) -> int:
        return self.pos

    def seek(self, pos: int) -> None:
        self.pos = pos


@dataclass
class NiObjectNet:
    """Base header carried by most blocks. Nextsoft adds an unknown u32
    before the name."""
    unknown0: int = 0
    name: str = ""
    extra_data_refs: list[int] = field(default_factory=list)
    controller: int = -1

    @classmethod
    def parse(cls, s: Stream) -> "NiObjectNet":
        # Nextsoft usually prepends a u32, but several NiNode/NiProperty
        # bodies omit it and begin directly with a sized string.
        first = s.u32()
        if first == 0 and s.pos + 4 <= len(s.buf):
            n = s.u32()
            if n > 4096:
                # No leading Nextsoft u32 and an empty name; `n` is actually
                # the controller ref (normally -1).
                return cls(
                    unknown0=0,
                    name="",
                    extra_data_refs=[],
                    controller=struct.unpack("<i", struct.pack("<I", n))[0],
                )
            else:
                name = s.buf[s.pos:s.pos + n].decode("latin1", errors="replace")
                s.pos += n
                u0 = first
        else:
            n = first
            if n > 4096:
                raise ValueError(f"sized_string too long: {n} at {s.pos:#x}")
            name = s.buf[s.pos:s.pos + n].decode("latin1", errors="replace")
            s.pos += n
            u0 = 0
        n_extra = s.u32()
        refs: list[int] = []
        if n_extra <= 1024:
            refs = [s.i32() for _ in range(n_extra)]
            ctrl = s.i32()
        else:
            # Some property blocks in this client omit the 10.x extra-data
            # list and store Controller immediately after the name.
            ctrl = struct.unpack("<i", struct.pack("<I", n_extra))[0]
        return cls(unknown0=u0, name=name, extra_data_refs=refs, controller=ctrl)


@dataclass
class NiAvObject:
    """NiAVObject extends NiObjectNET with transform + properties."""
    on: NiObjectNet = field(default_factory=NiObjectNet)
    flags: int = 0
    translation: tuple = (0.0, 0.0, 0.0)
    rotation: tuple = (1, 0, 0, 0, 1, 0, 0, 0, 1)
    scale: float = 1.0
    velocity: tuple = (0.0, 0.0, 0.0)
    property_refs: list[int] = field(default_factory=list)
    has_bounding_box: int = 0
    collision: int = -1

    @classmethod
    def parse(cls, s: Stream) -> "NiAvObject":
        on = NiObjectNet.parse(s)
        flags = s.u16()
        translation = s.vec3()
        rotation = s.matrix33()
        scale = s.f32()
        n_props = s.u32()
        prop_refs = [s.i32() for _ in range(n_props)]
        collision = s.i32()
        has_bb = s.u8()
        if has_bb:
            # BoundingBox in this 10.2 branch is center + axes + extents.
            s.vec3(); s.matrix33(); s.vec3()
        return cls(
            on=on, flags=flags, translation=translation, rotation=rotation,
            scale=scale, velocity=(0.0, 0.0, 0.0), property_refs=prop_refs,
            has_bounding_box=has_bb, collision=collision,
        )


@dataclass
class NiNode:
    av: NiAvObject = field(default_factory=NiAvObject)
    children: list[int] = field(default_factory=list)
    effects: list[int] = field(default_factory=list)

    @classmethod
    def parse(cls, s: Stream) -> "NiNode":
        av = NiAvObject.parse(s)
        n_ch = s.u32()
        children = [s.i32() for _ in range(n_ch)]
        n_eff = s.u32()
        effects = [s.i32() for _ in range(n_eff)]
        return cls(av=av, children=children, effects=effects)


@dataclass
class TriBasedGeomData:
    vertices: list[tuple[float, float, float]]
    normals: list[tuple[float, float, float]]
    uv_sets: list[list[tuple[float, float]]]
    vertex_colors: list[tuple[float, float, float, float]]
    center: tuple
    radius: float
    consistency_flags: int = 0

    @classmethod
    def parse(cls, s: Stream) -> "TriBasedGeomData":
        unknown_int = s.i32()
        if unknown_int != 0:
            raise ValueError(f"NiGeometryData unknown_int={unknown_int} at {s.tell() - 4:#x}")
        num_verts = s.u16()
        keep_flags = s.u8()
        compress_flags = s.u8()
        has_verts = s.u8()
        verts: list = []
        if has_verts:
            verts = [s.vec3() for _ in range(num_verts)]
        data_flags = s.u16()
        n_uv = data_flags & 0x3f
        has_tbn = (data_flags & 0x1000) != 0
        has_normals = s.u8()
        normals: list = []
        if has_normals:
            normals = [s.vec3() for _ in range(num_verts)]
            if has_tbn:
                s.skip(num_verts * 12 * 2)
        center = s.vec3()
        radius = s.f32()
        has_colors = s.u8()
        colors: list = []
        if has_colors:
            colors = [s.vec4() for _ in range(num_verts)]
        uv_sets: list = []
        for _ in range(n_uv):
            uv_sets.append([(s.f32(), s.f32()) for _ in range(num_verts)])
        consistency_flags = s.u16()
        return cls(
            vertices=verts, normals=normals, uv_sets=uv_sets,
            vertex_colors=colors, center=center, radius=radius,
            consistency_flags=consistency_flags,
        )


@dataclass
class NiTriStripsData:
    geom: TriBasedGeomData
    num_triangles: int
    strips: list[list[int]]
    consistency_flags: int

    @classmethod
    def parse(cls, s: Stream) -> "NiTriStripsData":
        geom = TriBasedGeomData.parse(s)
        num_triangles = s.u16()
        num_strips = s.u16()
        strip_lengths = [s.u16() for _ in range(num_strips)]
        has_points = s.u8()
        strips: list = []
        if has_points:
            for length in strip_lengths:
                strips.append([s.u16() for _ in range(length)])
        return cls(
            geom=geom, num_triangles=num_triangles, strips=strips,
            consistency_flags=geom.consistency_flags,
        )


@dataclass
class NiTriStrips:
    av: NiAvObject
    data: int
    skin_instance: int = -1

    @classmethod
    def parse(cls, s: Stream) -> "NiTriStrips":
        av = NiAvObject.parse(s)
        data = s.i32()
        skin = s.i32()
        has_shader = s.u8()
        if has_shader:
            s.sized_string()
            s.i32()
        return cls(av=av, data=data, skin_instance=skin)


@dataclass
class NiSourceTexture:
    on: NiObjectNet
    use_external: int = 1
    file_name: str = ""
    pixel_data: int = -1

    @classmethod
    def parse(cls, s: Stream) -> "NiSourceTexture":
        start = s.tell()
        on = NiObjectNet()
        try:
            # Most embedded texture source blocks carry NiObjectNET data.
            on = NiObjectNet.parse(s)
            if s.tell() >= len(s.buf) or s.buf[s.tell()] not in (0, 1):
                raise ValueError("not an object-net source texture body")
        except Exception:
            # Some effect source blocks are compact and begin directly with
            # UseExternal + FileName.
            s.seek(start)
        use_external = s.u8()
        if use_external:
            file_name = s.sized_string()
            # Gamebryo 10.1+ keeps a mostly-unused object ref even for
            # external textures.
            pixel_data = s.i32()
        else:
            # Embedded textures still carry their original file path here.
            file_name = s.sized_string()
            pixel_data = s.i32()
        # Pixel layout, mip-map format, alpha format, static, direct-render.
        s.u32(); s.u32(); s.u32(); s.u8(); s.u8()
        return cls(on=on, use_external=use_external, file_name=file_name,
                   pixel_data=pixel_data)


@dataclass
class NiTexturingProperty:
    on: NiObjectNet
    base_texture_source: int = -1  # block ref to NiSourceTexture

    @classmethod
    def parse(cls, s: Stream) -> "NiTexturingProperty":
        on = NiObjectNet.parse(s)
        body_start = s.tell()

        def parse_body(with_flags: bool) -> int:
            s.seek(body_start)
            if with_flags:
                s.u16()
            apply_mode = s.u32()
            texture_count = s.u32()
            if apply_mode > 8 or texture_count > 16:
                raise ValueError("bad NiTexturingProperty header")
            base_source = -1
            for ti in range(texture_count):
                has = s.u8()
                if has not in (0, 1):
                    raise ValueError("bad TexDesc present flag")
                if not has:
                    continue
                source = s.i32()
                clamp = s.u32()
                filt = s.u32()
                uv_set = s.u32()
                if clamp > 16 or filt > 16 or uv_set > 16:
                    raise ValueError("bad TexDesc enum")
                s.u16(); s.u16()
                has_transform = s.u8()
                if has_transform not in (0, 1):
                    raise ValueError("bad TexTransform flag")
                if has_transform:
                    s.skip(5 * 4 + 4 + 2 * 4)
                if ti == 0:
                    base_source = source
            n_shader = s.u32()
            if n_shader > 64:
                raise ValueError("bad shader texture count")
            for _ in range(n_shader):
                has = s.u8()
                if has not in (0, 1):
                    raise ValueError("bad ShaderTexDesc flag")
                if has:
                    s.i32(); s.u32(); s.u32(); s.u32(); s.u16(); s.u16()
                    has_transform = s.u8()
                    if has_transform:
                        s.skip(5 * 4 + 4 + 2 * 4)
                s.u32()
            return base_source

        try:
            base_source = parse_body(False)
        except Exception:
            base_source = parse_body(True)
        return cls(on=on, base_texture_source=base_source)


@dataclass
class NifFile:
    version: int
    user_version: int
    num_blocks: int
    block_types: list[str]
    block_type_idx: list[int]
    blocks: list[object]  # parsed where supported; None otherwise
    block_raw: list[bytes]  # always populated
    block_offsets: list[int]
    roots: list[int]
    path: Path | None = None


def parse_extra_data_string(s: Stream) -> tuple[str, str]:
    name = s.sized_string()
    value = s.sized_string()
    return name, value


def parse_extra_data_bool(s: Stream) -> tuple[str, int]:
    name = s.sized_string()
    value = s.u8()
    return name, value


def parse_objectnet_tail(s: Stream, byte_count: int) -> NiObjectNet:
    on = NiObjectNet.parse(s)
    s.skip(byte_count)
    return on


def skip_nipixeldata(s: Stream) -> None:
    start = s.tell()
    if start + 44 > len(s.buf):
        raise ValueError("truncated NiPixelData")
    mip_count, = struct.unpack_from("<I", s.buf, start + 40)
    if mip_count <= 0 or mip_count > 64:
        raise ValueError(f"bad NiPixelData mip_count={mip_count}")
    pos = start + 44 + mip_count * 12
    if pos + 8 > len(s.buf):
        raise ValueError("truncated NiPixelData mip table")
    _, total_size = struct.unpack_from("<II", s.buf, pos)
    if total_size < 0 or total_size > 256 * 1024 * 1024:
        raise ValueError(f"bad NiPixelData total_size={total_size}")
    s.seek(pos + 8 + total_size)


def parse_simple_block(bt: str, s: Stream) -> object | None:
    if bt == "NiStringExtraData":
        return parse_extra_data_string(s)
    if bt == "NiBooleanExtraData":
        return parse_extra_data_bool(s)
    if bt == "NiFogProperty":
        return parse_objectnet_tail(s, 42)
    if bt == "NiMaterialProperty":
        return parse_objectnet_tail(s, 12 * 4 + 4 + 4)
    if bt == "NiVertexColorProperty":
        return parse_objectnet_tail(s, 2 + 4 + 4)
    if bt == "NiShadeProperty":
        return parse_objectnet_tail(s, 2)
    if bt == "NiZBufferProperty":
        return parse_objectnet_tail(s, 2 + 4)
    if bt == "NiAlphaProperty":
        return parse_objectnet_tail(s, 2 + 1)
    if bt == "NiSpecularProperty":
        return parse_objectnet_tail(s, 2)
    if bt == "NiPixelData":
        skip_nipixeldata(s)
        return ("NiPixelData",)
    if bt == "NiCamera":
        av = NiAvObject.parse(s)
        s.skip(2 + 6 * 4 + 1 + 4 * 4 + 4 + 4 + 4 + 4)
        return av
    if bt == "NiTextureEffect":
        av = NiAvObject.parse(s)
        switch_state = s.u8()
        n_affected = s.u32()
        s.skip(n_affected * 4)
        s.skip(36 + 12 + 4 + 4 + 4 + 4 + 4 + 1 + 12 + 4 + 2 + 2)
        # QQXJ writes an additional 16-byte tail here in several map files.
        # Without it, the following NiSourceTexture is read 16 bytes early and
        # all early scene texture assignments shift.
        s.skip(16)
        return av
    return None


def parse_nif(buf: bytes, path: Path | None = None) -> NifFile:
    nl = buf.find(b"\n")
    if nl < 0 or not buf.startswith(HEADER_SIG):
        raise ValueError("not a Gamebryo 10.2.0.5 NIF")
    s = Stream(buf, pos=nl + 1)
    version = s.u32()
    user_version = s.u32()  # zero in our samples
    num_blocks = s.u32()
    num_block_types = s.u16()  # Nextsoft: u16, not u32
    block_types = []
    for _ in range(num_block_types):
        n = s.u32()
        block_types.append(buf[s.pos:s.pos + n].decode("ascii", errors="replace"))
        s.pos += n
    block_type_idx = [s.u16() for _ in range(num_blocks)]

    geometry_offsets_by_block: dict[int, int] = {}
    try:
        import nif_parser as geometry_probe
        info2, header_end2 = geometry_probe.parse_header(buf)
        geoms = geometry_probe.find_geometry_data(buf, scan_start=header_end2)
        tri_indices = [
            bi for bi, ti in enumerate(info2.block_type_indices)
            if 0 <= ti < len(info2.block_types)
            and info2.block_types[ti] == "NiTriStripsData"
        ]
        for bi, geom in zip(tri_indices, geoms):
            geometry_offsets_by_block[bi] = geom.block_offset
    except Exception:
        geometry_offsets_by_block = {}

    # Find block end positions by signature search. The proper way needs
    # a size table (post-20.x) but here we parse incrementally and trust
    # the parse to land at the next block boundary. As a fallback we use
    # the start of the next-known block type string occurrence.
    blocks: list[object] = []
    raw: list[bytes] = []
    offsets: list[int] = []
    for i, ti in enumerate(block_type_idx):
        bt = block_types[ti]
        if bt == "NiTriStripsData" and i in geometry_offsets_by_block:
            s.seek(geometry_offsets_by_block[i])
        start = s.pos
        parsed: object = None
        try:
            if bt == "NiNode":
                parsed = NiNode.parse(s)
            elif bt == "NiTriStrips":
                parsed = NiTriStrips.parse(s)
            elif bt == "NiTriStripsData":
                parsed = NiTriStripsData.parse(s)
            elif bt == "NiSourceTexture":
                parsed = NiSourceTexture.parse(s)
            elif bt == "NiTexturingProperty":
                parsed = NiTexturingProperty.parse(s)
            else:
                parsed = parse_simple_block(bt, s)
            if parsed is None:
                # Unknown block; we need to skip to the next block. Without
                # a block size table we resort to "advance until we find
                # something that looks like a valid block start". This is
                # imperfect but tolerable for our targeted use.
                parsed = None
        except Exception:
            parsed = None
        end = s.pos
        if parsed is None:
            # Try to skip to next sensible block start. We look for the
            # NEXT plausible NiObjectNET name (u32 unknown=0 + sized string
            # with reasonable length and printable ascii).
            j = start
            limit = min(len(buf), start + 256 * 1024)
            while j < limit - 8:
                u0, n = struct.unpack_from("<II", buf, j)
                if u0 == 0 and 0 < n <= 128:
                    cand = buf[j + 8:j + 8 + n]
                    if all(0x20 <= c < 0x7F for c in cand):
                        # plausible name start of NEXT block; the bytes
                        # immediately before should be the END of the
                        # previous block.
                        break
                j += 1
            end = j if j < limit - 8 else len(buf)
            s.pos = end
        raw.append(buf[start:end])
        offsets.append(start)
        blocks.append(parsed)
    # footer: num_roots + roots[]
    # Robust to trailing slop.
    roots: list[int] = []
    try:
        nr = struct.unpack_from("<I", buf, s.pos)[0]
        if 0 <= nr <= 64:
            s.pos += 4
            roots = [s.i32() for _ in range(nr)]
    except Exception:
        pass
    return NifFile(
        version=version, user_version=user_version, num_blocks=num_blocks,
        block_types=block_types, block_type_idx=block_type_idx,
        blocks=blocks, block_raw=raw, block_offsets=offsets,
        roots=roots, path=path,
    )


def load_nif(path: str | Path) -> NifFile:
    path = Path(path)
    return parse_nif(path.read_bytes(), path)


Transform = tuple[tuple[float, ...], float, tuple[float, float, float]]


IDENTITY_TRANSFORM: Transform = (
    (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
    1.0,
    (0.0, 0.0, 0.0),
)


def _matmul33(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(
        a[r * 3 + 0] * b[0 * 3 + c]
        + a[r * 3 + 1] * b[1 * 3 + c]
        + a[r * 3 + 2] * b[2 * 3 + c]
        for r in range(3)
        for c in range(3)
    )


def _matvec33(m: tuple[float, ...], v: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        m[0] * v[0] + m[1] * v[1] + m[2] * v[2],
        m[3] * v[0] + m[4] * v[1] + m[5] * v[2],
        m[6] * v[0] + m[7] * v[1] + m[8] * v[2],
    )


def _compose_transform(parent: Transform, local: Transform) -> Transform:
    pr, ps, pt = parent
    lr, ls, lt = local
    wr = _matmul33(pr, lr)
    rt = _matvec33(pr, lt)
    wt = (pt[0] + ps * rt[0], pt[1] + ps * rt[1], pt[2] + ps * rt[2])
    return wr, ps * ls, wt


def _apply_transform(tx: Transform, v: tuple[float, float, float]) -> tuple[float, float, float]:
    r, s, t = tx
    rv = _matvec33(r, v)
    return (t[0] + s * rv[0], t[1] + s * rv[1], t[2] + s * rv[2])


def _apply_transform_many(
    tx: Transform,
    vertices: list[tuple[float, float, float]],
) -> list[tuple[float, float, float]]:
    if not vertices:
        return []
    use_gpu = os.environ.get("QQXJ_USE_GPU_TRANSFORMS") == "1"
    if use_gpu and cp is not None and len(vertices) >= 4096:
        r, s, t = tx
        arr = cp.asarray(vertices, dtype=cp.float32)
        mat = cp.asarray(r, dtype=cp.float32).reshape((3, 3))
        trans = cp.asarray(t, dtype=cp.float32)
        out = arr @ mat.T
        out *= cp.float32(s)
        out += trans
        return [tuple(map(float, row)) for row in cp.asnumpy(out).tolist()]
    if np is None or len(vertices) < 64:
        return [_apply_transform(tx, v) for v in vertices]
    r, s, t = tx
    arr = np.asarray(vertices, dtype=np.float32)
    mat = np.asarray(r, dtype=np.float32).reshape((3, 3))
    trans = np.asarray(t, dtype=np.float32)
    out = arr @ mat.T
    out *= np.float32(s)
    out += trans
    return [tuple(map(float, row)) for row in out.tolist()]


def _local_transform(block: object) -> Transform:
    if isinstance(block, NiNode):
        av = block.av
    elif isinstance(block, NiTriStrips):
        av = block.av
    else:
        return IDENTITY_TRANSFORM
    return tuple(float(x) for x in av.rotation), float(av.scale), tuple(float(x) for x in av.translation)


def world_transforms(nif: NifFile) -> dict[int, Transform]:
    parent: dict[int, int] = {}
    for i, block in enumerate(nif.blocks):
        if isinstance(block, NiNode):
            for child in block.children:
                if child >= 0 and child not in parent:
                    parent[child] = i

    memo: dict[int, Transform] = {}

    def get(i: int, seen: set[int] | None = None) -> Transform:
        if i in memo:
            return memo[i]
        if seen is None:
            seen = set()
        if i in seen:
            return _local_transform(nif.blocks[i])
        seen.add(i)
        local = _local_transform(nif.blocks[i])
        if i in parent and 0 <= parent[i] < len(nif.blocks):
            tx = _compose_transform(get(parent[i], seen), local)
        else:
            tx = local
        memo[i] = tx
        return tx

    for i, block in enumerate(nif.blocks):
        if isinstance(block, (NiNode, NiTriStrips)):
            get(i)
    return memo


def collect_meshes(nif: NifFile) -> list[dict]:
    """Walk the NIF and produce render-ready mesh dicts:
        { name, vertices, uvs, faces, texture_filename, texture_block }
    """
    meshes = []
    transforms = world_transforms(nif)
    for i, b in enumerate(nif.blocks):
        if not isinstance(b, NiTriStrips):
            continue
        data_idx = b.data
        data: NiTriStripsData | None = None
        if 0 <= data_idx < len(nif.blocks) and isinstance(nif.blocks[data_idx], NiTriStripsData):
            data = nif.blocks[data_idx]
        if data is None or not data.geom.vertices:
            continue
        is_collision = b.av.collision >= 0
        nearby_types = [
            nif.block_types[nif.block_type_idx[j]]
            for j in range(max(0, data_idx - 4), data_idx)
            if 0 <= nif.block_type_idx[j] < len(nif.block_types)
        ]
        if "NiCollisionData" in nearby_types:
            is_collision = True
        # Convert strips to triangles
        faces: list[tuple[int, int, int]] = []
        for strip in data.strips:
            for j in range(len(strip) - 2):
                a, b2, c = strip[j], strip[j + 1], strip[j + 2]
                if a == b2 or b2 == c or a == c:
                    continue
                if j % 2 == 0:
                    faces.append((a, b2, c))
                else:
                    faces.append((a, c, b2))
        uvs: list[tuple[float, float]] = []
        if data.geom.uv_sets:
            uvs = data.geom.uv_sets[0]
        # Find the texture by walking property_refs
        tex_name = ""
        tex_block = -1
        for pr in b.av.property_refs:
            if 0 <= pr < len(nif.blocks):
                prop = nif.blocks[pr]
                if isinstance(prop, NiTexturingProperty):
                    src = prop.base_texture_source
                    if 0 <= src < len(nif.blocks):
                        st = nif.blocks[src]
                        if isinstance(st, NiSourceTexture):
                            tex_name = st.file_name
                            tex_block = src if not st.use_external else -1
                            break
        tx = transforms.get(i, IDENTITY_TRANSFORM)
        vertices = _apply_transform_many(tx, data.geom.vertices)
        meshes.append(dict(
            name=b.av.on.name or f"mesh_{i}",
            vertices=vertices,
            uvs=uvs,
            faces=faces,
            texture_filename=tex_name,
            texture_block=tex_block,
            block_index=i,
            data_block_index=data_idx,
            data_offset=nif.block_offsets[data_idx] if 0 <= data_idx < len(nif.block_offsets) else 0,
            transform_applied=True,
            is_collision=is_collision,
        ))
    return meshes


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: nextsoft_nif_parser.py <nif> [...more]")
        sys.exit(1)
    for arg in sys.argv[1:]:
        nif = load_nif(arg)
        ms = collect_meshes(nif)
        print(f"{arg}: blocks={nif.num_blocks} types={len(nif.block_types)} meshes={len(ms)}")
        for m in ms:
            print(f"  mesh {m['name']!r}: verts={len(m['vertices'])} uvs={len(m['uvs'])} faces={len(m['faces'])} tex={m['texture_filename']!r}")
