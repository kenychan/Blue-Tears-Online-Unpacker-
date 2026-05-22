#!/usr/bin/env python3
"""
LUO decompiler + texture-mapping extractor.

Tries unluac.jar first (a real Lua 5.1 decompiler producing readable
source). Falls back to the partial constant-table summary built into
qqxj_viewer.parse_lua51_bytecode.

Also exposes a helper that given a decompiled LUO returns the
`m_Texture` table: `{ shape_name: [dds_files...] }`. This is what lets us
correctly map textures to NIF mesh shapes.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

DEFAULT_UNLUAC = Path(__file__).resolve().parent / "unluac.jar"


@lru_cache(maxsize=1)
def find_java() -> str | None:
    p = shutil.which("java")
    return p


def decompile_luo(path: Path, unluac_jar: Path = DEFAULT_UNLUAC,
                  timeout: float = 15.0) -> str | None:
    """Run unluac on the file; return the decompiled Lua source as a
    string, or None if not possible."""
    java = find_java()
    if not java or not unluac_jar.exists():
        return None
    try:
        result = subprocess.run(
            [java, "-jar", str(unluac_jar), str(path)],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        if result.returncode != 0 and not result.stdout:
            return None
        return result.stdout
    except Exception:
        return None


def _balanced_block(text: str, start: int) -> tuple[int, int] | None:
    """Return (open_idx, close_idx_exclusive) for the brace-balanced block
    starting at the first `{` at or after `start`."""
    open_idx = text.find("{", start)
    if open_idx < 0:
        return None
    depth = 0
    i = open_idx
    in_string = False
    while i < len(text):
        c = text[i]
        if in_string:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_string = False
        else:
            if c == '"':
                in_string = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return open_idx, i + 1
        i += 1
    return None


_SHAPE_ENTRY_RE = re.compile(r"([A-Za-z_]\w*)\s*=\s*\{")


def extract_texture_map(decompiled_lua: str) -> dict[str, list[str]]:
    """Parse `m_Texture = { Shape = { "a.dds", "b.dds" }, ... }` properly."""
    if not decompiled_lua:
        return {}
    out: dict[str, list[str]] = {}
    anchor = decompiled_lua.find("m_Texture")
    if anchor < 0:
        return out
    block = _balanced_block(decompiled_lua, anchor)
    if not block:
        return out
    body = decompiled_lua[block[0] + 1:block[1] - 1]
    # iterate shape entries inside body
    i = 0
    while i < len(body):
        m = _SHAPE_ENTRY_RE.search(body, i)
        if not m:
            break
        shape = m.group(1)
        inner = _balanced_block(body, m.end() - 1)
        if not inner:
            break
        textures = re.findall(r"\"([^\"]+)\"", body[inner[0] + 1:inner[1] - 1])
        out[shape] = textures
        i = inner[1]
    return out


_MESH_FILE_RE = re.compile(r'm_strMeshFile\s*=\s*"([^"]+)"')
_ATTACH_POINT_RE = re.compile(r'm_strMeshAttachPoint\s*=\s*"([^"]+)"')


def extract_part_meta(decompiled_lua: str) -> dict:
    """Best-effort extraction of m_strMeshFile + m_strMeshAttachPoint."""
    if not decompiled_lua:
        return {}
    out: dict = {}
    m = _MESH_FILE_RE.search(decompiled_lua)
    if m:
        out["mesh_file"] = m.group(1)
    a = _ATTACH_POINT_RE.search(decompiled_lua)
    if a:
        out["attach_point"] = a.group(1)
    out["textures"] = extract_texture_map(decompiled_lua)
    return out


def find_part_luo_for_nif(nif_path: Path) -> Path | None:
    """For a given NIF, find the sibling/parent LUO that describes it.

    Most LUOs are named `<archive>_<dir>_<part>.luo` and sit next to or
    one level above the NIF they describe.
    """
    stem = nif_path.stem  # e.g. "G_W_Mantle"
    # 1) any .luo in same dir whose decompile references this NIF by name
    for cand in nif_path.parent.glob("*.luo"):
        try:
            text = cand.read_bytes()
            if stem.encode() in text or nif_path.name.encode() in text:
                return cand
        except Exception:
            continue
    # 2) walk up to two parents
    for parent in [nif_path.parent.parent, nif_path.parent.parent.parent]:
        if not parent.exists():
            continue
        for cand in parent.rglob("*.luo"):
            try:
                text = cand.read_bytes()
                if nif_path.name.encode() in text:
                    return cand
            except Exception:
                continue
    return None


if __name__ == "__main__":
    import sys
    for arg in sys.argv[1:]:
        p = Path(arg)
        src = decompile_luo(p)
        if src is None:
            print(f"# {p}: could not decompile (java/unluac missing or parse failed)")
            continue
        meta = extract_part_meta(src)
        print(f"# {p}")
        for k, v in meta.items():
            print(f"  {k}: {v}")
        print()
        print(src)
