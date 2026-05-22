#!/usr/bin/env python3
"""
Probe/decode QQXJ Character/PartsDB.fdb.

The TW client's decompiled CFPartsDB::vLoadFile shows this file starts with:

    u32 num_part_l
    u32 num_part_t
    u32 string_blob_size

It then loads num_part_l "large" CFPart records followed by num_part_t
"texture/replacement" records. The first record class has the fields that
matter most for reconstruction: name, parent id, mesh attach point, mesh file,
base directory, shape -> texture lists, text keys, and animation filenames.

This script is intentionally conservative: it decodes the record layout that is
supported by the TW client and emits the raw offsets/unknown bytes so we can
tighten the model without losing data.
"""

from __future__ import annotations

import argparse
import csv
import json
import struct
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class ShapeTextures:
    shape: str
    textures: list[str]


@dataclass
class TextKeyGroup:
    part: str
    keys: list[dict]


@dataclass
class PartRecord:
    kind: str
    index: int
    offset: int
    end_offset: int
    part_id: int
    parent_id: int
    name: str
    mesh_attach_point: str
    mesh_file: str
    base_dir: str
    texture_shapes: list[ShapeTextures] = field(default_factory=list)
    animations: list[str] = field(default_factory=list)
    text_keys: list[TextKeyGroup] = field(default_factory=list)
    num_texture_shapes: int = 0
    num_text_key_groups: int = 0
    num_animations: int = 0
    record_flag: int = 0
    trailing_u16: int = 0


class FdbReader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def tell(self) -> int:
        return self.pos

    def read(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise EOFError(f"read past EOF at 0x{self.pos:x}, need {n}")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def u8(self) -> int:
        return self.read(1)[0]

    def u16(self) -> int:
        value, = struct.unpack("<H", self.read(2))
        return value

    def u32(self) -> int:
        value, = struct.unpack("<I", self.read(4))
        return value

    def i32(self) -> int:
        value, = struct.unpack("<i", self.read(4))
        return value

    def f32(self) -> float:
        value, = struct.unpack("<f", self.read(4))
        return value

    def sized_string(self, size: int | None = None) -> str:
        if size is None:
            size = self.u8()
        raw = self.read(size)
        if raw.endswith(b"\x00"):
            raw = raw[:-1]
        return raw.decode("cp949", errors="replace")


def read_shape_textures(r: FdbReader, count: int) -> list[ShapeTextures]:
    out: list[ShapeTextures] = []
    for _ in range(count):
        shape = r.sized_string()
        tex_count = r.u8()
        textures = [r.sized_string() for _ in range(tex_count)]
        out.append(ShapeTextures(shape=shape, textures=textures))
    return out


def read_text_key_groups(r: FdbReader, count: int) -> list[TextKeyGroup]:
    groups: list[TextKeyGroup] = []
    for _ in range(count):
        part = r.sized_string()
        key_count = r.u8()
        keys = []
        for _ in range(key_count):
            time = r.f32()
            text = r.sized_string()
            keys.append({"time": time, "text": text})
        groups.append(TextKeyGroup(part=part, keys=keys))
    return groups


def read_part_l(r: FdbReader, index: int) -> PartRecord:
    start = r.tell()
    part_id = r.u32()
    parent_id = r.i32()
    name_len = r.u8()
    attach_len = r.u8()
    mesh_len = r.u8()
    base_dir_len = r.u8()
    num_texture_shapes = r.u8()
    record_flag = r.u8()
    num_text_key_groups = r.u16()
    num_animations = r.u16()
    trailing_u16 = r.u16()

    name = r.sized_string(name_len)
    mesh_attach_point = r.sized_string(attach_len)
    mesh_file = r.sized_string(mesh_len)
    base_dir = r.sized_string(base_dir_len)
    texture_shapes = read_shape_textures(r, num_texture_shapes)
    text_keys = read_text_key_groups(r, num_text_key_groups)
    animations = [r.sized_string() for _ in range(num_animations)]

    return PartRecord(
        kind="L",
        index=index,
        offset=start,
        end_offset=r.tell(),
        part_id=part_id,
        parent_id=parent_id,
        name=name,
        mesh_attach_point=mesh_attach_point,
        mesh_file=mesh_file,
        base_dir=base_dir,
        texture_shapes=texture_shapes,
        animations=animations,
        text_keys=text_keys,
        num_texture_shapes=num_texture_shapes,
        num_text_key_groups=num_text_key_groups,
        num_animations=num_animations,
        record_flag=record_flag,
        trailing_u16=trailing_u16,
    )


def read_part_t(r: FdbReader, index: int) -> PartRecord:
    """Read the smaller CFPartT-style record.

    The decompiled class is 0x28 bytes in memory and omits the animation list.
    It keeps the same string/header prefix and the same shape texture/text key
    tails, which is enough for texture replacement tables.
    """
    start = r.tell()
    part_id = r.u32()
    parent_id = r.i32()
    name_len = r.u8()
    base_dir_len = r.u8()
    variant_dir_len = r.u8()
    num_texture_shapes = r.u8()
    num_text_key_groups = r.u16()
    trailing_u16 = r.u16()

    name = r.sized_string(name_len)
    base_dir = r.sized_string(base_dir_len)
    variant_dir = r.sized_string(variant_dir_len)
    texture_shapes = read_shape_textures(r, num_texture_shapes)
    text_keys = read_text_key_groups(r, num_text_key_groups)

    return PartRecord(
        kind="T",
        index=index,
        offset=start,
        end_offset=r.tell(),
        part_id=part_id,
        parent_id=parent_id,
        name=name,
        mesh_attach_point="",
        mesh_file=variant_dir,
        base_dir=base_dir,
        texture_shapes=texture_shapes,
        text_keys=text_keys,
        num_texture_shapes=num_texture_shapes,
        num_text_key_groups=num_text_key_groups,
        trailing_u16=trailing_u16,
    )


def decode(path: Path, include_t: bool = True) -> tuple[dict, list[PartRecord]]:
    r = FdbReader(path.read_bytes())
    count_l = r.u32()
    count_t = r.u32()
    blob_size = r.u32()
    records: list[PartRecord] = []
    for i in range(count_l):
        records.append(read_part_l(r, i))
    if include_t:
        for i in range(count_t):
            records.append(read_part_t(r, i))
    header = {
        "path": str(path),
        "file_size": len(r.data),
        "count_l": count_l,
        "count_t": count_t,
        "blob_size": blob_size,
        "decoded_records": len(records),
        "end_offset": r.tell(),
        "remaining_bytes": len(r.data) - r.tell(),
    }
    return header, records


def write_outputs(header: dict, records: list[PartRecord], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "partsdb_records.json"
    csv_path = out_dir / "partsdb_records.csv"
    summary_path = out_dir / "partsdb_summary.json"

    summary_path.write_text(json.dumps(header, indent=2), encoding="utf-8")
    json_path.write_text(
        json.dumps([asdict(r) for r in records], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "kind", "index", "offset", "end_offset", "part_id",
                "parent_id", "name", "mesh_attach_point", "mesh_file",
                "base_dir", "num_texture_shapes", "num_animations",
                "num_text_key_groups", "textures", "animations",
            ],
        )
        writer.writeheader()
        for rec in records:
            texture_summary = "; ".join(
                f"{st.shape}=>{','.join(st.textures)}"
                for st in rec.texture_shapes
            )
            writer.writerow({
                "kind": rec.kind,
                "index": rec.index,
                "offset": f"0x{rec.offset:x}",
                "end_offset": f"0x{rec.end_offset:x}",
                "part_id": rec.part_id,
                "parent_id": rec.parent_id,
                "name": rec.name,
                "mesh_attach_point": rec.mesh_attach_point,
                "mesh_file": rec.mesh_file,
                "base_dir": rec.base_dir,
                "num_texture_shapes": rec.num_texture_shapes,
                "num_animations": rec.num_animations,
                "num_text_key_groups": rec.num_text_key_groups,
                "textures": texture_summary,
                "animations": ";".join(rec.animations[:20]),
            })


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("fdb", help="Path to Character/PartsDB.fdb")
    ap.add_argument("--out", default="partsdb_probe", help="output directory")
    ap.add_argument("--skip-t", action="store_true", help="only decode PartL records")
    args = ap.parse_args()

    header, records = decode(Path(args.fdb), include_t=not args.skip_t)
    write_outputs(header, records, Path(args.out))
    print(json.dumps(header, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
