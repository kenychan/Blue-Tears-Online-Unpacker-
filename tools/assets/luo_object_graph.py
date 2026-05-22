#!/usr/bin/env python3
"""
Build RE-backed object graphs from QQXJ/Blue Tears LUO map scripts.

This is intentionally a small source parser over unluac output rather than a
full Lua interpreter. The de-shelled TW client confirms that MapInit,
MapExports, MapToolDataExport, and MapDataObject-style fields are the real
scene/object records the client consumes. This module turns those records into
JSON that the viewer/exporter can use as an assembled object tree.
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import re
from pathlib import Path
from typing import Any

try:
    from luo_decompiler import DEFAULT_UNLUAC, decompile_luo
except ImportError:  # pragma: no cover - package import fallback
    DEFAULT_UNLUAC = Path(__file__).resolve().parent / "unluac.jar"
    decompile_luo = None


SCRIPT_DIR = Path(__file__).resolve().parent

_INCLUDE_RE = re.compile(r'\binclude\s*\(\s*"([^"]+)"\s*\)')
_MAP_DEF_RE = re.compile(r'\b(MapInit|MapExports|MapToolDataExport)\s*\(')
_ENTRY_RE = re.compile(r'\b(ServerRoom|Volume|Position|Resurrection_Position|SingleObject)\s*\(')
_SCENE_ASSIGN_RE = re.compile(r'\bscene\.(m_[A-Za-z_]\w*)\s*=')
_POINT_RE = re.compile(
    r'^NiPoint3\s*\(\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*\)$'
)
_CHAR_DESC_RE = re.compile(r'\bCharacterDescription\s*\(')

_OBJECT_REF_CACHE: dict[tuple[str, str], list[dict[str, str]]] = {}
_CLASS_META_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


def _line_col(text: str, index: int) -> dict[str, int]:
    line = text.count("\n", 0, index) + 1
    prev = text.rfind("\n", 0, index)
    return {"line": line, "column": index + 1 if prev < 0 else index - prev}


def _balanced_pair(text: str, open_idx: int, open_ch: str, close_ch: str) -> tuple[int, int] | None:
    if open_idx < 0 or open_idx >= len(text) or text[open_idx] != open_ch:
        return None
    depth = 0
    i = open_idx
    in_string = False
    string_ch = ""
    while i < len(text):
        c = text[i]
        if in_string:
            if c == "\\":
                i += 2
                continue
            if c == string_ch:
                in_string = False
        else:
            if c in ('"', "'"):
                in_string = True
                string_ch = c
            elif c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    return open_idx, i + 1
        i += 1
    return None


def _split_top_level(text: str, delimiter: str = ",") -> list[str]:
    out: list[str] = []
    start = 0
    brace = paren = bracket = 0
    in_string = False
    string_ch = ""
    i = 0
    while i < len(text):
        c = text[i]
        if in_string:
            if c == "\\":
                i += 2
                continue
            if c == string_ch:
                in_string = False
        else:
            if c in ('"', "'"):
                in_string = True
                string_ch = c
            elif c == "{":
                brace += 1
            elif c == "}":
                brace -= 1
            elif c == "(":
                paren += 1
            elif c == ")":
                paren -= 1
            elif c == "[":
                bracket += 1
            elif c == "]":
                bracket -= 1
            elif c == delimiter and brace == 0 and paren == 0 and bracket == 0:
                out.append(text[start:i].strip())
                start = i + 1
        i += 1
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


def _find_top_level_equal(text: str) -> int:
    brace = paren = bracket = 0
    in_string = False
    string_ch = ""
    i = 0
    while i < len(text):
        c = text[i]
        if in_string:
            if c == "\\":
                i += 2
                continue
            if c == string_ch:
                in_string = False
        else:
            if c in ('"', "'"):
                in_string = True
                string_ch = c
            elif c == "{":
                brace += 1
            elif c == "}":
                brace -= 1
            elif c == "(":
                paren += 1
            elif c == ")":
                paren -= 1
            elif c == "[":
                bracket += 1
            elif c == "]":
                bracket -= 1
            elif c == "=" and brace == 0 and paren == 0 and bracket == 0:
                return i
        i += 1
    return -1


def _string_value(src: str) -> str:
    try:
        return ast.literal_eval(src)
    except Exception:
        return src.strip('"')


def _parse_scalar(src: str) -> Any:
    src = src.strip().rstrip(",")
    point = _POINT_RE.match(src)
    if point:
        return [float(point.group(1)), float(point.group(2)), float(point.group(3))]
    if src.startswith('"') and src.endswith('"'):
        return _string_value(src)
    if src in {"true", "false"}:
        return src == "true"
    if src == "nil":
        return None
    if re.fullmatch(r"[-+]?\d+", src):
        try:
            return int(src)
        except ValueError:
            pass
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", src):
        try:
            return float(src)
        except ValueError:
            pass
    if src.startswith("{") and src.endswith("}"):
        body = src[1:-1].strip()
        if not body:
            return []
        items = _split_top_level(body)
        parsed = [_parse_scalar(item) for item in items]
        if all(not isinstance(item, dict) or set(item) != {"raw"} for item in parsed):
            return parsed
    return {"raw": src}


def _parse_field_table(src: str) -> dict[str, Any]:
    body = src.strip()
    if body.startswith("{") and body.endswith("}"):
        body = body[1:-1]
    fields: dict[str, Any] = {}
    for item in _split_top_level(body):
        eq = _find_top_level_equal(item)
        if eq < 0:
            continue
        key = item[:eq].strip()
        if key.startswith("[") and key.endswith("]"):
            key = key[1:-1].strip()
        key = key.strip('"')
        fields[key] = _parse_scalar(item[eq + 1:])
    return fields


def _parse_string_table(src: str) -> list[str]:
    src = src.strip()
    if not (src.startswith("{") and src.endswith("}")):
        return []
    return [item for item in (_parse_scalar(x) for x in _split_top_level(src[1:-1])) if isinstance(item, str)]


def _call_args(text: str, call_match: re.Match[str]) -> tuple[list[Any], int] | None:
    open_idx = text.find("(", call_match.start())
    span = _balanced_pair(text, open_idx, "(", ")")
    if not span:
        return None
    args_src = text[span[0] + 1:span[1] - 1]
    return [_parse_scalar(arg) for arg in _split_top_level(args_src)], span[1]


def _set_block_after(text: str, start: int) -> tuple[str, int] | None:
    set_idx = text.find(":Set", start, start + 120)
    if set_idx < 0:
        return None
    open_idx = text.find("{", set_idx)
    span = _balanced_pair(text, open_idx, "{", "}")
    if not span:
        return None
    return text[span[0]:span[1]], span[1]


def _def_block_after(text: str, start: int) -> tuple[str, int] | None:
    def_idx = text.find(":Def", start, start + 160)
    if def_idx < 0:
        return None
    open_idx = text.find("{", def_idx)
    span = _balanced_pair(text, open_idx, "{", "}")
    if not span:
        return None
    return text[span[0]:span[1]], span[1]


def _parse_scene_fields(block_src: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for match in _SCENE_ASSIGN_RE.finditer(block_src):
        key = match.group(1)
        value_start = match.end()
        line_end = block_src.find("\n", value_start)
        if line_end < 0:
            line_end = len(block_src)
        fields[key] = _parse_scalar(block_src[value_start:line_end].strip())
    return fields


def parse_luo_source(source: str, source_path: Path | None = None) -> dict[str, Any]:
    graph: dict[str, Any] = {
        "source": str(source_path) if source_path else None,
        "includes": _INCLUDE_RE.findall(source),
        "map_init": [],
        "exports": [],
        "counts": {},
    }

    for match in _MAP_DEF_RE.finditer(source):
        kind = match.group(1)
        args_info = _call_args(source, match)
        if not args_info:
            continue
        args, args_end = args_info
        def_info = _def_block_after(source, args_end)
        if not def_info:
            continue
        block_src, block_end = def_info
        record: dict[str, Any] = {
            "kind": kind,
            "name": args[0] if args else None,
            "args": args,
            "location": _line_col(source, match.start()),
        }
        if kind == "MapInit":
            record["scene"] = _parse_scene_fields(block_src)
            graph["map_init"].append(record)
            continue
        entries: list[dict[str, Any]] = []
        for entry_match in _ENTRY_RE.finditer(block_src):
            entry_kind = entry_match.group(1)
            entry_args_info = _call_args(block_src, entry_match)
            if not entry_args_info:
                continue
            entry_args, entry_args_end = entry_args_info
            entry: dict[str, Any] = {
                "kind": entry_kind,
                "name": entry_args[0] if entry_args else None,
                "args": entry_args,
                "location": _line_col(source, match.start() + entry_match.start()),
            }
            set_info = _set_block_after(block_src, entry_args_end)
            if set_info:
                fields_src, _ = set_info
                fields = _parse_field_table(fields_src)
                entry["fields"] = fields
                if "ObjectName" in fields:
                    entry["object_name"] = fields["ObjectName"]
                if "Position" in fields:
                    entry["position"] = fields["Position"]
                if "OrientAxis" in fields:
                    entry["orient_axis"] = fields["OrientAxis"]
                if "OrientAngle" in fields:
                    entry["orient_angle"] = fields["OrientAngle"]
                if "Extend" in fields:
                    entry["extend"] = fields["Extend"]
            elif entry_kind in {"Position", "Resurrection_Position"} and len(entry_args) > 1:
                entry["position"] = entry_args[1]
            entries.append(entry)
        record["entries"] = entries
        graph["exports"].append(record)
        graph["counts"][kind] = graph["counts"].get(kind, 0) + 1
        for entry in entries:
            key = entry["kind"]
            graph["counts"][key] = graph["counts"].get(key, 0) + 1
        if block_end <= match.end():
            break
    graph["counts"]["include"] = len(graph["includes"])
    graph["counts"]["MapInit"] = len(graph["map_init"])
    return graph


def _candidate_include_paths(include_name: str, owner: Path, root: Path | None) -> list[Path]:
    rel = Path(include_name.replace("\\", "/"))
    if rel.suffix.lower() != ".luo":
        rel = rel.with_suffix(".luo")
    candidates = [
        owner.parent / rel,
        owner.parent / "zh_tw" / rel.name,
        owner.parent.parent / rel.name,
    ]
    if root:
        candidates.append(root / rel)
        candidates.extend(root.rglob(rel.name))
    seen: set[Path] = set()
    out: list[Path] = []
    for cand in candidates:
        try:
            resolved = cand.resolve()
        except Exception:
            resolved = cand
        if resolved in seen:
            continue
        seen.add(resolved)
        if cand.exists():
            out.append(cand)
    return out


def decompile_or_read(path: Path, unluac_jar: Path | None = None) -> str | None:
    if path.suffix.lower() == ".lua":
        return path.read_text(encoding="utf-8", errors="replace")
    if decompile_luo is None:
        return None
    return decompile_luo(path, unluac_jar or DEFAULT_UNLUAC)


def _sibling_scene_luos(path: Path) -> list[Path]:
    stem = path.stem
    candidates = [
        path.parent / f"{stem}_Monsters.luo",
        path.parent / "zh_tw" / f"{stem}_Objects.luo",
        path.parent / "zh_tw" / f"{stem}_MapToolData.luo",
        path.parent / "zh_tw" / f"{stem}_Monsters.luo",
    ]
    return [p for p in candidates if p.exists()]


def _file_contains(path: Path, needle: bytes) -> bool:
    try:
        return needle in path.read_bytes()
    except Exception:
        return False


def resolve_object_refs(object_name: str, root: Path) -> list[dict[str, str]]:
    cache_key = (str(root.resolve()), object_name)
    cached = _OBJECT_REF_CACHE.get(cache_key)
    if cached is not None:
        return cached

    refs: list[dict[str, str]] = []
    seen: set[Path] = set()

    object_main = root / "Lua" / "Object" / "Main"
    if object_main.exists():
        for category in object_main.iterdir():
            if not category.is_dir():
                continue
            for cand in [category / f"{object_name}.luo", category / "zh_tw" / f"{object_name}.luo"]:
                if cand.exists():
                    resolved = cand.resolve()
                    if resolved not in seen:
                        seen.add(resolved)
                        refs.append({"role": f"object_class:{category.name}", "path": str(cand)})

    needle = object_name.encode("utf-8", errors="ignore")
    container_roots = [
        root / "Lua" / "Object" / "Main" / "Npc",
        root / "Lua" / "Class" / "Client" / "Effect" / "SoundEffects",
        root / "Npc",
        root / "Text" / "zh_tw" / "Npc",
    ]
    for container_root in container_roots:
        if not container_root.exists():
            continue
        for cand in container_root.rglob("*"):
            if not cand.is_file() or cand.suffix.lower() not in {".luo", ".lua", ".txt"}:
                continue
            resolved = cand.resolve()
            if resolved in seen:
                continue
            if _file_contains(cand, needle):
                seen.add(resolved)
                refs.append({"role": "object_data_ref", "path": str(cand)})
                if len(refs) >= 12:
                    _OBJECT_REF_CACHE[cache_key] = refs
                    return refs
    _OBJECT_REF_CACHE[cache_key] = refs
    return refs


def load_partsdb_asset_index(path: Path | None) -> dict[str, list[dict[str, Any]]]:
    if not path or not path.exists():
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get("name")
            if not name:
                continue
            compact = {
                "kind": row.get("kind", ""),
                "part_id": _parse_scalar(row.get("part_id", "")),
                "parent_id": _parse_scalar(row.get("parent_id", "")),
                "parent_name": row.get("parent_name", ""),
                "base_dir": row.get("base_dir", ""),
                "mesh_file": row.get("mesh_file", ""),
                "mesh_path": row.get("mesh_path", ""),
                "mesh_exists": row.get("mesh_exists", "").lower() == "true",
                "texture_count": _parse_scalar(row.get("texture_count", "0")),
                "textures_existing": _parse_scalar(row.get("textures_existing", "0")),
                "animation_count": _parse_scalar(row.get("animation_count", "0")),
                "animations_existing": _parse_scalar(row.get("animations_existing", "0")),
            }
            out.setdefault(name, []).append(compact)
    return out


def extract_character_descriptions(source: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for match in _CHAR_DESC_RE.finditer(source):
        open_idx = source.find("(", match.start())
        call_span = _balanced_pair(source, open_idx, "(", ")")
        if not call_span:
            continue
        call_body = source[call_span[0] + 1:call_span[1] - 1]
        args = _split_top_level(call_body)
        if not args:
            continue
        parts = _parse_string_table(args[0])
        types = _parse_string_table(args[1]) if len(args) > 1 else []
        out.append({"parts": parts, "types": types})
    return out


def _template_block_for(source: str, object_name: str) -> str:
    pattern = re.compile(r'\bTemplate\s*\(\s*"' + re.escape(object_name) + r'"')
    match = pattern.search(source)
    if not match:
        return source
    args_info = _call_args(source, match)
    if not args_info:
        return source
    _, args_end = args_info
    def_info = _def_block_after(source, args_end)
    if not def_info:
        return source[match.start():match.start() + 2000]
    return def_info[0]


def class_meta(path: Path, unluac_jar: Path | None,
               object_name: str | None,
               partsdb_index: dict[str, list[dict[str, Any]]] | None) -> dict[str, Any]:
    cache_key = (str(path.resolve()), str(unluac_jar or ""), object_name or "")
    cached = _CLASS_META_CACHE.get(cache_key)
    if cached is None:
        src = decompile_or_read(path, unluac_jar)
        target_src = _template_block_for(src or "", object_name) if object_name else (src or "")
        desc = extract_character_descriptions(target_src)
        cached = {"character_descriptions": desc}
        _CLASS_META_CACHE[cache_key] = cached
    meta = dict(cached)
    if partsdb_index:
        assets: list[dict[str, Any]] = []
        for desc in meta.get("character_descriptions", []):
            for part in desc.get("parts", []):
                for asset in partsdb_index.get(part, []):
                    assets.append({"part": part, **asset})
        if assets:
            meta["partsdb_assets"] = assets
    return meta


def _annotate_object_refs(src_graph: dict[str, Any], root: Path,
                          unluac_jar: Path | None = None,
                          partsdb_index: dict[str, list[dict[str, Any]]] | None = None) -> None:
    cache: dict[str, list[dict[str, str]]] = {}
    for export in src_graph.get("exports", []):
        for entry in export.get("entries", []):
            object_name = entry.get("object_name")
            if not isinstance(object_name, str):
                continue
            if object_name not in cache:
                cache[object_name] = resolve_object_refs(object_name, root)
            if cache[object_name]:
                refs: list[dict[str, Any]] = []
                for ref in cache[object_name]:
                    ref_out: dict[str, Any] = dict(ref)
                    if ref["role"].startswith("object_class:") or "Lua\\Object\\Main\\Npc" in ref["path"] or "Lua/Object/Main/Npc" in ref["path"]:
                        meta = class_meta(Path(ref["path"]), unluac_jar, object_name, partsdb_index)
                        if meta.get("character_descriptions"):
                            ref_out["class_meta"] = meta
                    refs.append(ref_out)
                entry["resolved_refs"] = refs


def build_object_graph(path: Path, root: Path | None = None,
                       unluac_jar: Path | None = None,
                       resolve_includes: bool = True,
                       resolve_siblings: bool = True,
                       partsdb_assets: Path | None = None) -> dict[str, Any]:
    source = decompile_or_read(path, unluac_jar)
    if source is None:
        raise RuntimeError(f"could not decompile {path}")
    graph = parse_luo_source(source, path)
    partsdb_index = load_partsdb_asset_index(partsdb_assets)
    if partsdb_assets:
        graph["partsdb_assets"] = str(partsdb_assets)
    graph["files"] = [{"path": str(path), "role": "entry"}]
    if not resolve_includes and not resolve_siblings:
        return graph

    resolved: list[dict[str, Any]] = []
    seen_paths = {path.resolve()}
    for inc in graph["includes"]:
        paths = _candidate_include_paths(inc, path, root)
        item: dict[str, Any] = {"include": inc, "candidates": [str(p) for p in paths]}
        if paths:
            inc_path = paths[0]
            seen_paths.add(inc_path.resolve())
            inc_source = decompile_or_read(inc_path, unluac_jar)
            if inc_source:
                item["path"] = str(inc_path)
                item["graph"] = parse_luo_source(inc_source, inc_path)
                if root:
                    _annotate_object_refs(item["graph"], root, unluac_jar, partsdb_index)
                graph["files"].append({"path": str(inc_path), "role": "include", "include": inc})
        resolved.append(item)
    graph["resolved_includes"] = resolved

    related: list[dict[str, Any]] = []
    if resolve_siblings:
        for sibling in _sibling_scene_luos(path):
            resolved_path = sibling.resolve()
            if resolved_path in seen_paths:
                continue
            seen_paths.add(resolved_path)
            sibling_source = decompile_or_read(sibling, unluac_jar)
            item: dict[str, Any] = {"path": str(sibling), "role": "scene_sibling"}
            if sibling_source:
                item["graph"] = parse_luo_source(sibling_source, sibling)
                if root:
                    _annotate_object_refs(item["graph"], root, unluac_jar, partsdb_index)
                graph["files"].append({"path": str(sibling), "role": "scene_sibling"})
            related.append(item)
    graph["related_files"] = related
    if root:
        graph["resource_root"] = str(root)
        _annotate_object_refs(graph, root, unluac_jar, partsdb_index)
    return graph


def flatten_object_nodes(graph: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []

    def collect(src_graph: dict[str, Any], source_hint: str | None = None) -> None:
        for export in src_graph.get("exports", []):
            for entry in export.get("entries", []):
                if entry.get("kind") != "SingleObject":
                    continue
                fields = entry.get("fields", {})
                nodes.append({
                    "node_type": "object",
                    "name": entry.get("name"),
                    "object_name": entry.get("object_name"),
                    "position": entry.get("position"),
                    "orient_axis": entry.get("orient_axis"),
                    "orient_angle": entry.get("orient_angle"),
                    "extend": entry.get("extend"),
                    "fields": fields,
                    "resolved_refs": entry.get("resolved_refs", []),
                    "source": source_hint or src_graph.get("source"),
                    "export": export.get("name"),
                    "location": entry.get("location"),
                })

    collect(graph)
    for inc in graph.get("resolved_includes", []):
        if "graph" in inc:
            collect(inc["graph"], inc.get("path"))
    for related in graph.get("related_files", []):
        if "graph" in related:
            collect(related["graph"], related.get("path"))
    return nodes


def summarize_graph(graph: dict[str, Any]) -> dict[str, Any]:
    aggregate_counts: dict[str, int] = {}

    def add_counts(src_graph: dict[str, Any]) -> None:
        for key, value in src_graph.get("counts", {}).items():
            if isinstance(value, int):
                aggregate_counts[key] = aggregate_counts.get(key, 0) + value

    scene_resources: list[str] = []
    add_counts(graph)
    for init in graph.get("map_init", []):
        scene = init.get("scene", {})
        value = scene.get("m_strResourceName")
        if isinstance(value, str):
            scene_resources.append(value)
    for inc in graph.get("resolved_includes", []):
        inc_graph = inc.get("graph")
        if not inc_graph:
            continue
        add_counts(inc_graph)
        for init in inc_graph.get("map_init", []):
            value = init.get("scene", {}).get("m_strResourceName")
            if isinstance(value, str):
                scene_resources.append(value)
    for related in graph.get("related_files", []):
        related_graph = related.get("graph")
        if not related_graph:
            continue
        add_counts(related_graph)
        for init in related_graph.get("map_init", []):
            value = init.get("scene", {}).get("m_strResourceName")
            if isinstance(value, str):
                scene_resources.append(value)
    nodes = flatten_object_nodes(graph)
    object_names = sorted({n.get("object_name") for n in nodes if isinstance(n.get("object_name"), str)})
    return {
        "source": graph.get("source"),
        "scene_resources": scene_resources,
        "files": graph.get("files", []),
        "counts": aggregate_counts,
        "object_node_count": len(nodes),
        "unique_object_names": object_names,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Build a LUO map/object graph JSON file.")
    ap.add_argument("luo", type=Path, help="Map .luo/.lua file to parse")
    ap.add_argument("--root", type=Path, default=None, help="Resource root used to resolve includes")
    ap.add_argument("--unluac", type=Path, default=DEFAULT_UNLUAC, help="Path to unluac.jar")
    ap.add_argument("--partsdb-assets", type=Path, default=None, help="Optional partsdb_assets.csv from partsdb_resolve_assets.py")
    ap.add_argument("--out", type=Path, default=None, help="Output JSON path")
    ap.add_argument("--no-includes", action="store_true", help="Do not resolve/decompile include() files")
    ap.add_argument("--no-siblings", action="store_true", help="Do not auto-load sibling *_Objects/MapToolData LUOs")
    ap.add_argument("--flat-objects", action="store_true", help="Write flattened SingleObject nodes instead")
    args = ap.parse_args()

    graph = build_object_graph(
        args.luo, args.root, args.unluac,
        not args.no_includes, not args.no_siblings,
        args.partsdb_assets,
    )
    payload: Any = flatten_object_nodes(graph) if args.flat_objects else graph
    if isinstance(payload, dict):
        payload["summary"] = summarize_graph(graph)

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"[+] wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
