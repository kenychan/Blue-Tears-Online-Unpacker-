#!/usr/bin/env python3
"""
Parse Gamebryo Keyframe Manager (.kfm) and Keyframe (.kf) files.

KFM format (version 1.2.4b — observed in QQxj/Punch Monster):
  - ASCII line: ";Gamebryo KFM File Version 1.2.4b\\n"
  - u32 nif_name_len  + ASCII nif_name        (the base model)
  - u32 base_anim_len + ASCII base_anim_id    (often same as model stem)
  - u32 unknown1                              (often 1)
  - u32 unknown2                              (often 0)
  - float fade_in_time
  - float fade_out_time
  - u32 num_animations
  - u32 unknown3                              (often 0)
  - per animation:
      u32 anim_name_len  + ASCII anim_name
      u32 kf_file_len    + ASCII kf_file_name
      u32[5] trailer                          (probably loop / priority / blend)

KF files are regular Gamebryo NIFs containing animation controllers:
  - NiSequenceStreamHelper, NiKeyframeController, NiKeyframeData blocks
This module only reports the basic stats; full animation decoding is future work.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path


KFM_MAGIC_PREFIX = b";Gamebryo KFM File Version"


@dataclass
class KfmAnim:
    name: str
    kf_file: str
    raw_extra: list[int] = field(default_factory=list)


@dataclass
class Kfm:
    version_line: str
    nif_name: str
    base_anim_id: str
    fade_in: float
    fade_out: float
    animations: list[KfmAnim]
    extras: dict


def _read_sized_string(buf: bytes, pos: int) -> tuple[str, int]:
    n, = struct.unpack_from("<I", buf, pos)
    pos += 4
    if n == 0:
        return "", pos
    if pos + n > len(buf):
        raise ValueError(f"sized string overflows at {pos:#x} (len={n})")
    s = buf[pos:pos + n].decode("latin1", errors="replace")
    return s, pos + n


def parse_kfm(buf: bytes) -> Kfm:
    if not buf.startswith(KFM_MAGIC_PREFIX):
        raise ValueError("not a KFM file (missing magic)")
    nl = buf.find(b"\n")
    if nl < 0:
        raise ValueError("malformed header")
    version_line = buf[:nl].decode("latin1", errors="replace")
    pos = nl + 1
    nif_name, pos = _read_sized_string(buf, pos)
    base_anim_id, pos = _read_sized_string(buf, pos)
    unknown1, unknown2 = struct.unpack_from("<II", buf, pos); pos += 8
    fade_in, fade_out = struct.unpack_from("<ff", buf, pos); pos += 8
    num_anims, = struct.unpack_from("<I", buf, pos); pos += 4
    unknown3, = struct.unpack_from("<I", buf, pos); pos += 4
    anims: list[KfmAnim] = []
    for _ in range(num_anims):
        anim_name, pos = _read_sized_string(buf, pos)
        kf_file, pos = _read_sized_string(buf, pos)
        # TW 1.2.4b files use five trailer u32s after each animation pair.
        # Older notes assumed a leading pad + three trailers, which misaligns
        # immediately on DummyPlayerM.kfm.
        if pos + 20 <= len(buf):
            extra = list(struct.unpack_from("<5I", buf, pos))
            pos += 20
        else:
            extra = []
        anims.append(KfmAnim(name=anim_name, kf_file=kf_file, raw_extra=extra))
    return Kfm(
        version_line=version_line,
        nif_name=nif_name,
        base_anim_id=base_anim_id,
        fade_in=fade_in,
        fade_out=fade_out,
        animations=anims,
        extras=dict(unknown1=unknown1, unknown2=unknown2, unknown3=unknown3),
    )


def summarize_kfm(kfm: Kfm) -> str:
    out = [f"# {kfm.version_line}"]
    out.append(f"base NIF: {kfm.nif_name}")
    out.append(f"base anim id: {kfm.base_anim_id}")
    out.append(f"fade-in: {kfm.fade_in}s  fade-out: {kfm.fade_out}s")
    out.append(f"animations: {len(kfm.animations)}")
    for a in kfm.animations:
        out.append(f"  - {a.name!r} -> {a.kf_file}")
        if a.raw_extra:
            out.append(f"    extra u32s: {a.raw_extra}")
    return "\n".join(out)


def summarize_kf(buf: bytes) -> str:
    """KF is a Gamebryo NIF. Quick summary: version, num blocks, list block types."""
    nl = buf.find(b"\n")
    if nl < 0:
        return "(not a KF/NIF)"
    header = buf[:nl].decode("latin1", errors="replace")
    pos = nl + 1
    ver, = struct.unpack_from("<I", buf, pos); pos += 4
    pos += 4  # unknown/user_version
    num_blocks, = struct.unpack_from("<I", buf, pos); pos += 4
    num_types, = struct.unpack_from("<H", buf, pos); pos += 2
    types = []
    for _ in range(num_types):
        n, = struct.unpack_from("<I", buf, pos); pos += 4
        types.append(buf[pos:pos + n].decode("ascii", errors="replace"))
        pos += n
    out = [f"# {header}"]
    out.append(f"version: {hex(ver)}  blocks: {num_blocks}  unique types: {num_types}")
    out.append("block types:")
    for t in types:
        out.append(f"  - {t}")
    return "\n".join(out)


if __name__ == "__main__":
    import sys
    for arg in sys.argv[1:]:
        p = Path(arg)
        if p.suffix.lower() == ".kfm":
            kfm = parse_kfm(p.read_bytes())
            print(summarize_kfm(kfm))
        elif p.suffix.lower() == ".kf":
            print(summarize_kf(p.read_bytes()))
        else:
            print(f"unknown extension: {p}")
