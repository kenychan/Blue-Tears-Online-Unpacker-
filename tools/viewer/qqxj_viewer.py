#!/usr/bin/env python3
"""
qqxj_viewer.py — single-window viewer for the QQxj / Punch Monster asset
tree produced by sfa_named_extract_v4.py.

Tabs:
  - Image:    DDS / TGA / PNG / JPG / BMP previews.
  - Lua:      `unluac`-decompiled Lua source for .luo + .lua text for .lua.
  - 3D Model: NIF mesh with textures applied (textures resolved via the
              sibling LUO file's `m_Texture` table — that's where the real
              texture-per-shape mapping lives).
  - Animation: KFM metadata + KF block-list, plus the base NIF that the
              animation drives.
  - Scenes:   independent tree of every World/Map/<map>/<map>.nif so you
              can browse maps without hunting through the file system.
  - Audio:    WAV / OGG playback.
  - Info:     raw metadata fallback.

NIF/OBJ outputs are cached under Res/Temp, and explicit exports go under
Res/Export_Obj so Blender can find OBJ, MTL, and textures together.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import pickle
import struct
import subprocess
import sys
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _module_dir in (HERE, HERE.parent / "assets"):
    if _module_dir.exists() and str(_module_dir) not in sys.path:
        sys.path.insert(0, str(_module_dir))

# Anaconda environments often have PyQt5 installed alongside PySide6.
# Their Qt5 plugin paths fight, and the OS picks the wrong one, producing
# "Could not load the Qt platform plugin 'windows'" at startup. Point Qt
# explicitly at PySide6's plugin directory before any Qt import.
try:
    import PySide6 as _pyside6
    _plugins = Path(_pyside6.__file__).resolve().parent / "plugins"
    if _plugins.exists():
        os.environ["QT_PLUGIN_PATH"] = str(_plugins)
        os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(_plugins / "platforms")
    # Defensively scrub any environment variable a conda PyQt5 install
    # might have set.
    for _k in list(os.environ.keys()):
        if _k.startswith("QT_") and "PyQt5" in os.environ.get(_k, ""):
            del os.environ[_k]
except Exception:
    pass

import numpy as np
from PIL import Image
from PySide6 import QtCore, QtGui, QtWidgets
try:
    from PySide6 import QtMultimedia
    HAS_QT_MULTIMEDIA = True
except ImportError:
    QtMultimedia = None  # type: ignore
    HAS_QT_MULTIMEDIA = False


IMAGE_EXTS = {".dds", ".tga", ".png", ".jpg", ".jpeg", ".bmp", ".gif"}
TEXT_EXTS = {".lua", ".txt", ".xml", ".ini", ".csv", ".json", ".html", ".log", ".md"}
AUDIO_EXTS = {".wav", ".ogg", ".mp3"}
MESH_EXTS = {".nif"}
ANIM_EXTS = {".kf", ".kfm"}
LUO_EXTS = {".luo"}

# Encoding-fallback order: covers GB2312 (gb2312), GBK (cp936), Big5 (cp950),
# Korean (cp949), and finally latin1 as a never-fail catch.
TEXT_ENCODINGS = ("utf-8", "gb18030", "gb2312", "cp936", "cp950", "big5", "cp949", "latin1")


def file_kind(path: Path) -> str:
    e = path.suffix.lower()
    if e in IMAGE_EXTS:
        return "image"
    if e in LUO_EXTS:
        return "luo"
    if e in TEXT_EXTS:
        return "text"
    if e in AUDIO_EXTS:
        return "audio"
    if e in MESH_EXTS:
        return "mesh"
    if e in ANIM_EXTS:
        return "anim"
    return "other"


def decode_text(data: bytes) -> tuple[str, str]:
    for enc in TEXT_ENCODINGS:
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("latin1", errors="replace"), "latin1(replace)"


# ---------------------------------------------------------------------------
# Image conversion
# ---------------------------------------------------------------------------


def load_image_qpixmap(path: Path) -> tuple[QtGui.QPixmap, str]:
    try:
        im = Image.open(path)
        im.load()
        info = f"{im.format} {im.mode} {im.size[0]}x{im.size[1]} ({path.stat().st_size} bytes)"
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA")
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        pix = QtGui.QPixmap()
        pix.loadFromData(buf.getvalue(), "PNG")
        return pix, info
    except Exception:
        pix = QtGui.QPixmap(str(path))
        return pix, f"(Qt fallback) {pix.width()}x{pix.height()}"


# ---------------------------------------------------------------------------
# NIF → OBJ pipeline with caching
# ---------------------------------------------------------------------------


CACHE_SCHEMA_VERSION = "v6-2026-05-21-partsdb"  # bump when parser/pipeline changes invalidate old OBJs
MESH_CACHE_SCHEMA_VERSION = "mesh-v1-2026-05-22"


def _cache_key(nif_path: Path) -> str:
    st = nif_path.stat()
    here = Path(__file__).resolve().parent
    parser_mtime = (here / "nextsoft_nif_to_obj.py").stat().st_mtime
    pipeline_mtime = (here / "qqxj_asset_pipeline.py").stat().st_mtime
    block_parser = here.parent / "assets" / "nextsoft_nif_parser.py"
    block_parser_mtime = block_parser.stat().st_mtime if block_parser.exists() else 0
    key_input = (
        f"{CACHE_SCHEMA_VERSION}|{nif_path}|{st.st_mtime}|{st.st_size}"
        f"|parser={parser_mtime}|block_parser={block_parser_mtime}|pipeline={pipeline_mtime}"
    )
    return hashlib.md5(key_input.encode()).hexdigest()[:16]


def get_obj_for_nif(nif_path: Path, cache_root: Path) -> Path | None:
    """Convert NIF→OBJ once per (path, mtime); reuse the cached output."""
    key = _cache_key(nif_path)
    work = cache_root / key
    obj = work / nif_path.with_suffix(".obj").name
    if obj.exists() and (work / ".done").exists():
        return obj
    work.mkdir(parents=True, exist_ok=True)
    here = Path(__file__).resolve().parent
    py = sys.executable
    try:
        subprocess.run(
            [py, str(here / "nextsoft_nif_to_obj.py"), str(nif_path), str(obj)],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    if not obj.exists():
        return None
    (work / ".done").write_text("ok")
    return obj


def rewrite_mtl_with_luo_textures(obj_path: Path, nif_path: Path,
                                  texture_search_roots: list[Path]) -> int:
    """Rewrite the MTL next to obj_path so each `mat_NNN` block uses the
    DDS named by the part LUO for the NNN-th shape. Returns count of
    materials that got a texture assigned.

    Also copies the DDS (or PNG-converted) into the OBJ dir so it can be
    opened in Blender or another viewer without scavenging.
    """
    try:
        import luo_decompiler as ld
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import luo_decompiler as ld
    mtl = obj_path.with_suffix(".mtl")
    if not mtl.exists():
        return 0
    luo = ld.find_part_luo_for_nif(nif_path)
    if not luo:
        return 0
    src = ld.decompile_luo(luo)
    if not src:
        return 0
    tex_map = ld.extract_texture_map(src)
    if not tex_map:
        return 0
    shape_names = shape_names_in_nif(nif_path)
    # Read original MTL, locate newmtl blocks in order, assign texture.
    lines = mtl.read_text(errors="ignore").splitlines()
    out_lines: list[str] = []
    mat_idx = -1
    count = 0
    for line in lines:
        ls = line.strip()
        if ls.lower().startswith("newmtl "):
            mat_idx += 1
            out_lines.append(line)
            continue
        if ls.lower().startswith("map_kd "):
            # Skip the original; we'll write a fresh one once per material.
            continue
        out_lines.append(line)
        # If we hit blank line or end of block, drop in our map_Kd
        if ls == "" and mat_idx >= 0 and mat_idx < len(shape_names):
            shape = shape_names[mat_idx]
            tex_files = tex_map.get(shape, [])
            for tf in tex_files:
                tex_path: Path | None = None
                for root in [nif_path.parent] + texture_search_roots:
                    cand = root / tf
                    if cand.exists():
                        tex_path = cand
                        break
                    for found in root.rglob(tf):
                        tex_path = found
                        break
                    if tex_path:
                        break
                if tex_path:
                    # Copy / convert into OBJ dir
                    target_name = tex_path.name
                    target = obj_path.parent / target_name
                    if not target.exists():
                        try:
                            if tex_path.suffix.lower() == ".dds":
                                Image.open(tex_path).save(target.with_suffix(".png"))
                                target_name = target.with_suffix(".png").name
                                target = target.with_suffix(".png")
                            else:
                                import shutil as _sh
                                _sh.copy2(tex_path, target)
                        except Exception:
                            continue
                    out_lines.insert(len(out_lines) - 1, f"map_Kd {target_name}")
                    count += 1
                    break
    mtl.write_text("\n".join(out_lines) + "\n")
    return count


def shape_names_in_nif(nif_path: Path) -> list[str]:
    """Return the NiTriStrips shape names in file order. Names are extracted
    heuristically by finding length-prefixed strings ending in 'Shape'."""
    import re
    data = nif_path.read_bytes()
    out: list[str] = []
    for m in re.finditer(rb"[A-Za-z0-9_]+Shape", data):
        off = m.start()
        name_len = m.end() - m.start()
        if off >= 4:
            prefix_len, = struct.unpack_from("<I", data, off - 4)
            if prefix_len == name_len:
                out.append(m.group().decode("ascii", errors="replace"))
    return out


def _resource_root_for(path: Path) -> Path:
    here = Path(__file__).resolve().parent
    for parent in [path, *path.parents]:
        if parent.name.lower() in {"extracted_named", "extracted_named_v4", "tw_extracted_named_v4"}:
            return parent
        if (parent / "Character" / "PartsDB.fdb").exists():
            return parent
        if (parent / "World" / "Map").exists() and (parent / "Character").exists():
            return parent
    return here / "extracted_named_v4"


def get_obj_for_nif(nif_path: Path, cache_root: Path) -> Path | None:
    """Convert NIF to OBJ once per cache key; reuse the cached output."""
    import qqxj_asset_pipeline as pipe

    key = _cache_key(nif_path)
    work = cache_root / key
    obj = work / nif_path.with_suffix(".obj").name
    if obj.exists() and obj.with_suffix(".mtl").exists() and (work / ".done").exists():
        return obj
    work.mkdir(parents=True, exist_ok=True)
    result = pipe.export_nif_to_obj(
        nif_path,
        work,
        resource_root=_resource_root_for(nif_path),
        convert_to_png=True,
    )
    if not result.get("ok") or not obj.exists():
        return None
    (work / ".done").write_text("ok")
    return obj


def rewrite_mtl_with_luo_textures(obj_path: Path, nif_path: Path,
                                  texture_search_roots: list[Path]) -> int:
    import qqxj_asset_pipeline as pipe

    resource_root = texture_search_roots[-1] if texture_search_roots else _resource_root_for(nif_path)
    result = pipe.rewrite_mtl_with_real_textures(obj_path, nif_path, resource_root, convert_to_png=True)
    return int(result.get("resolved", 0))


# ---------------------------------------------------------------------------
# 3D mesh view
# ---------------------------------------------------------------------------


import pyqtgraph as pg  # noqa: E402
import pyqtgraph.opengl as gl  # noqa: E402


class MeshView(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.gl_view = gl.GLViewWidget()
        self.gl_view.setCameraPosition(distance=20, elevation=20, azimuth=45)
        self.gl_view.setBackgroundColor((40, 40, 50))
        layout.addWidget(self.gl_view)
        grid = gl.GLGridItem()
        grid.setSize(20, 20)
        grid.setSpacing(1, 1)
        self.gl_view.addItem(grid)
        ax = gl.GLAxisItem()
        ax.setSize(5, 5, 5)
        self.gl_view.addItem(ax)
        self._meshes: list = []
        self._animated: list[dict] = []
        self._anim_time = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick_animation)

    def clear(self) -> None:
        self._timer.stop()
        self._animated.clear()
        for m in self._meshes:
            try:
                self.gl_view.removeItem(m)
            except Exception:
                pass
        self._meshes.clear()

    @staticmethod
    def _sample_texture_colors(tex_array: np.ndarray, uvs: np.ndarray) -> np.ndarray:
        h, w = tex_array.shape[:2]
        u = (uvs[:, 0] % 1.0) * (w - 1)
        v = (1.0 - (uvs[:, 1] % 1.0)) * (h - 1)
        ix = np.clip(u.astype(np.int32), 0, w - 1)
        iy = np.clip(v.astype(np.int32), 0, h - 1)
        rgb = tex_array[iy, ix]
        if rgb.max() < 0.02:
            avg = tex_array.reshape(-1, 3).mean(0)
            rgb = np.tile(avg, (uvs.shape[0], 1))
        alpha = np.ones((rgb.shape[0], 1), dtype=np.float32)
        return np.concatenate([rgb, alpha], axis=1).astype(np.float32)

    @staticmethod
    def _curve_value(curve: dict, t: float) -> float:
        keys = curve.get("keys") or []
        if not keys:
            return 0.0
        if t <= float(keys[0].get("time", 0.0)):
            return float(keys[0].get("value", 0.0))
        for a, b in zip(keys, keys[1:]):
            ta = float(a.get("time", 0.0))
            tb = float(b.get("time", ta))
            if ta <= t <= tb:
                va = float(a.get("value", 0.0))
                vb = float(b.get("value", va))
                if abs(tb - ta) < 1e-6:
                    return vb
                f = (t - ta) / (tb - ta)
                return va + (vb - va) * f
        return float(keys[-1].get("value", 0.0))

    def _animated_uvs(self, base_uvs: np.ndarray, curves: list[dict], t: float) -> np.ndarray:
        uvs = base_uvs.copy()
        for curve in curves:
            op = curve.get("operation_name", "")
            value = self._curve_value(curve, t)
            if op == "TT_TRANSLATE_U":
                uvs[:, 0] += value
            elif op == "TT_TRANSLATE_V":
                uvs[:, 1] += value
            elif op == "TT_SCALE_U":
                uvs[:, 0] *= value
            elif op == "TT_SCALE_V":
                uvs[:, 1] *= value
            elif op == "TT_ROTATE":
                c = np.cos(value)
                s = np.sin(value)
                centered = uvs - 0.5
                x = centered[:, 0].copy()
                y = centered[:, 1].copy()
                uvs[:, 0] = c * x - s * y + 0.5
                uvs[:, 1] = s * x + c * y + 0.5
        return uvs

    def _tick_animation(self) -> None:
        if not self._animated:
            self._timer.stop()
            return
        self._anim_time += self._timer.interval() / 1000.0
        for entry in self._animated:
            duration = max(0.001, float(entry.get("duration", 3.0)))
            t = self._anim_time % duration
            uvs = self._animated_uvs(entry["uvs"], entry["curves"], t)
            colors = self._sample_texture_colors(entry["tex_array"], uvs)
            mesh_data = entry["mesh_data"]
            mesh_data.setVertexColors(colors)
            entry["item"].meshDataChanged()

    def show_meshes(self, mesh_data_list: list[dict], info_cb=None) -> None:
        """mesh_data_list entries:
            { 'verts': Nx3, 'faces': Mx3, 'uvs'?: Nx2, 'tex_path'?: Path,
              'name': str }
        """
        self.clear()
        if not mesh_data_list:
            if info_cb:
                info_cb("(no meshes)")
            return
        big = np.concatenate([m["verts"] for m in mesh_data_list], axis=0)
        center = big.mean(axis=0)
        span = max(1e-3, float(np.linalg.norm(big.max(0) - big.min(0))))

        info: list[str] = [f"meshes: {len(mesh_data_list)}", f"bbox span: {span:.2f}"]
        for i, m in enumerate(mesh_data_list):
            verts = (m["verts"].astype(np.float32) - center)
            faces = m["faces"].astype(np.int32)
            colors = None
            tex_path = m.get("tex_path")
            uvs = m.get("uvs")
            tex_array = None
            if tex_path is not None and uvs is not None and uvs.shape[0] == verts.shape[0]:
                try:
                    # Use RGB only — alpha at transparent corners would zero everything
                    img = Image.open(tex_path).convert("RGB")
                    tex_array = np.asarray(img, dtype=np.float32) / 255.0
                    colors = self._sample_texture_colors(tex_array, uvs)
                except Exception as e:
                    info.append(f"  mesh {i} tex bake failed: {e}")
            mesh_data = gl.MeshData(vertexes=verts, faces=faces)
            if colors is not None:
                mesh_data.setVertexColors(colors)
                # CRITICAL: drop the 'shaded' uniform-color shader so vertex
                # colors are actually used.
                item = gl.GLMeshItem(
                    meshdata=mesh_data, smooth=False, drawFaces=True,
                    drawEdges=False, shader=None,
                )
                tex_label = tex_path.name if tex_path else "?"
                info.append(
                    f"  {i:2d} {m.get('name', '?'):<28} verts={len(verts):5d}"
                    f" faces={len(faces):5d}  tex={tex_label}"
                )
                curves = m.get("texture_anim_curves") or []
                if curves and tex_array is not None:
                    self._animated.append({
                        "item": item,
                        "mesh_data": mesh_data,
                        "uvs": uvs.astype(np.float32),
                        "tex_array": tex_array,
                        "curves": curves,
                        "duration": m.get("texture_anim_duration", 3.0),
                    })
            else:
                color = m.get("color") or self._mesh_color(i)
                item = gl.GLMeshItem(
                    meshdata=mesh_data, smooth=True, drawFaces=True,
                    drawEdges=False, color=color, shader="shaded",
                )
                info.append(
                    f"  {i:2d} {m.get('name', '?'):<28} verts={len(verts):5d}"
                    f" faces={len(faces):5d}  (no texture, flat color)"
                )
            item.setGLOptions("opaque")
            self.gl_view.addItem(item)
            self._meshes.append(item)
        self.gl_view.setCameraPosition(distance=span * 1.5)
        if self._animated:
            info.append(f"animated texture previews: {len(self._animated)} mesh(es)")
            self._anim_time = 0.0
            self._timer.start()
        if info_cb:
            info_cb("\n".join(info))

    def _mesh_color(self, i: int):
        palette = [
            (0.9, 0.7, 0.7, 1.0), (0.7, 0.9, 0.7, 1.0),
            (0.7, 0.7, 0.9, 1.0), (0.9, 0.9, 0.7, 1.0),
            (0.9, 0.7, 0.9, 1.0), (0.7, 0.9, 0.9, 1.0),
        ]
        return palette[i % len(palette)]


def load_obj_meshes_with_textures(obj_path: Path, shape_names: list[str],
                                  texture_map: dict[str, list[str]],
                                  texture_search_roots: list[Path]) -> list[dict]:
    """Fast deterministic OBJ+MTL reader for viewer preview.

    Trimesh is flexible but it can reorder geometry/materials and it is slow
    on these many-object map NIF exports. This reader follows the OBJ stream
    directly: `o` starts a mesh, `usemtl` selects the material, and `map_Kd`
    in the sibling MTL supplies the texture path.
    """
    mtl_path = obj_path.with_suffix(".mtl")
    cache_path = obj_path.with_suffix(".meshcache.pkl")
    try:
        meta = {
            "version": MESH_CACHE_SCHEMA_VERSION,
            "obj_mtime": obj_path.stat().st_mtime,
            "mtl_mtime": mtl_path.stat().st_mtime if mtl_path.exists() else 0,
            "obj_size": obj_path.stat().st_size,
            "mtl_size": mtl_path.stat().st_size if mtl_path.exists() else 0,
        }
        if cache_path.exists():
            with cache_path.open("rb") as f:
                cached = pickle.load(f)
            if cached.get("meta") == meta:
                return cached.get("chunks", [])
    except Exception:
        meta = {}

    material_textures: dict[str, Path] = {}
    material_colors: dict[str, tuple[float, float, float, float]] = {}
    if mtl_path.exists():
        current = ""
        for line in mtl_path.read_text(encoding="ascii", errors="ignore").splitlines():
            parts = line.strip().split(None, 1)
            if not parts:
                continue
            key = parts[0].lower()
            if key == "newmtl" and len(parts) == 2:
                current = parts[1]
            elif key == "map_kd" and current and len(parts) == 2:
                tex = (obj_path.parent / parts[1].strip()).resolve()
                if tex.exists():
                    material_textures[current] = tex
            elif key == "kd" and current and len(parts) == 2:
                vals = parts[1].split()
                if len(vals) >= 3:
                    try:
                        material_colors[current] = (
                            float(vals[0]), float(vals[1]), float(vals[2]), 1.0
                        )
                    except ValueError:
                        pass

    global_v: list[tuple[float, float, float]] = []
    global_vt: list[tuple[float, float]] = []
    chunks: list[dict] = []
    current_name = "mesh_000"
    current_mat = ""
    local_verts: list[tuple[float, float, float]] = []
    local_uvs: list[tuple[float, float]] = []
    local_faces: list[tuple[int, int, int]] = []

    def flush() -> None:
        nonlocal local_verts, local_uvs, local_faces
        if not local_faces:
            local_verts = []
            local_uvs = []
            return
        verts = np.asarray(local_verts, dtype=np.float32)
        faces = np.asarray(local_faces, dtype=np.int32)
        uvs = np.asarray(local_uvs, dtype=np.float32) if len(local_uvs) == len(local_verts) else None
        tex_path = material_textures.get(current_mat) if uvs is not None else None
        chunks.append({
            "name": current_name,
            "verts": verts,
            "faces": faces,
            "uvs": uvs,
            "tex_path": tex_path,
            "color": material_colors.get(current_mat),
        })
        local_verts = []
        local_uvs = []
        local_faces = []

    for raw in obj_path.read_text(encoding="ascii", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        tag = parts[0]
        if tag == "v" and len(parts) >= 4:
            global_v.append((float(parts[1]), float(parts[2]), float(parts[3])))
        elif tag == "vt" and len(parts) >= 3:
            global_vt.append((float(parts[1]), float(parts[2])))
        elif tag in {"o", "g"}:
            flush()
            current_name = parts[1] if len(parts) > 1 else f"mesh_{len(chunks):03d}"
            current_mat = ""
        elif tag == "usemtl":
            if local_faces:
                flush()
            current_mat = parts[1] if len(parts) > 1 else ""
        elif tag == "f" and len(parts) >= 4:
            face_indices: list[int] = []
            for tok in parts[1:]:
                bits = tok.split("/")
                try:
                    vi = int(bits[0])
                except ValueError:
                    continue
                if vi < 0:
                    vi = len(global_v) + vi + 1
                if not (1 <= vi <= len(global_v)):
                    continue
                vt = None
                if len(bits) > 1 and bits[1]:
                    ti = int(bits[1])
                    if ti < 0:
                        ti = len(global_vt) + ti + 1
                    if 1 <= ti <= len(global_vt):
                        vt = global_vt[ti - 1]
                face_indices.append(len(local_verts))
                local_verts.append(global_v[vi - 1])
                if vt is not None:
                    local_uvs.append(vt)
            if len(face_indices) >= 3:
                for i in range(1, len(face_indices) - 1):
                    local_faces.append((face_indices[0], face_indices[i], face_indices[i + 1]))
    flush()
    if meta:
        try:
            with cache_path.open("wb") as f:
                pickle.dump({"meta": meta, "chunks": chunks}, f, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception:
            pass
    return chunks


# ---------------------------------------------------------------------------
# Scene browser model — lists every <map>/<map>.nif under World/Map.
# ---------------------------------------------------------------------------


def attach_texture_animation_curves(meshes: list[dict], nif_path: Path) -> dict:
    """Attach best-effort texture animation curves to viewer mesh chunks."""
    try:
        import nextsoft_texture_controller_probe as controller_probe
    except Exception as exc:
        return {"ok": 0, "error": str(exc)}

    try:
        binding_meta = controller_probe.bind_texture_animation(nif_path)
    except Exception as exc:
        return {"ok": 0, "error": str(exc)}
    bindings = [b for b in binding_meta.get("bindings", []) if b.get("curve")]
    if not bindings:
        return {"ok": 1, "controllers": 0, "animated_meshes": 0}

    grouped: list[list[dict]] = []
    for binding in bindings:
        link = binding.get("sequence_link") or {}
        node = link.get("node_name", "")
        if not grouped or (grouped[-1][0].get("sequence_link") or {}).get("node_name", "") != node:
            grouped.append([])
        grouped[-1].append(binding)
    textured_meshes = [m for m in meshes if m.get("tex_path") is not None and m.get("uvs") is not None]
    animated = 0
    max_duration = 0.0
    for mesh, group in zip(textured_meshes, grouped):
        mesh_curves = []
        for binding in group:
            curve = dict(binding["curve"])
            ctrl = binding.get("controller") or {}
            link = binding.get("sequence_link") or {}
            curve["operation_name"] = curve.get("operation_name") or ctrl.get("operation_name", "")
            curve["controller_block_index"] = ctrl.get("block_index")
            curve["target_ref"] = ctrl.get("target_ref")
            curve["node_name"] = link.get("node_name", "")
            mesh_curves.append(curve)
            for key in curve.get("keys", []):
                max_duration = max(max_duration, float(key.get("time", 0.0)))
        if mesh_curves:
            mesh["texture_anim_curves"] = mesh_curves
            mesh["texture_anim_duration"] = max_duration or 3.0
            animated += 1
    return {
        "ok": 1,
        "controllers": binding_meta.get("controller_count", len(bindings)),
        "kf": binding_meta.get("kf", ""),
        "float_curves": binding_meta.get("curve_count", len(bindings)),
        "animated_meshes": animated,
        "duration": max_duration or 3.0,
    }


class SceneBrowserModel(QtCore.QAbstractItemModel):
    def __init__(self, root: Path, parent=None):
        super().__init__(parent)
        self.root = root
        self.scenes: list[dict] = []
        self._scan()

    def _scan(self):
        maps_dir = self.root / "World" / "Map"
        if not maps_dir.exists():
            return
        for region in sorted(maps_dir.iterdir()):
            if not region.is_dir():
                continue
            for scene_dir in sorted(region.iterdir()):
                if not scene_dir.is_dir():
                    continue
                main_nif = scene_dir / f"{scene_dir.name}.nif"
                main_luo = scene_dir / f"{scene_dir.name}.luo"
                objects = scene_dir / f"{scene_dir.name}_Objects.luo"
                monsters = scene_dir / f"{scene_dir.name}_Monsters.luo"
                map_tool = scene_dir / f"{scene_dir.name}_MapToolData.luo"
                navmesh = scene_dir / f"{scene_dir.name}_NavMeshPE.xml"
                self.scenes.append({
                    "name": scene_dir.name,
                    "region": region.name,
                    "dir": scene_dir,
                    "nif": main_nif if main_nif.exists() else None,
                    "scene_luo": main_luo if main_luo.exists() else None,
                    "objects": objects if objects.exists() else None,
                    "monsters": monsters if monsters.exists() else None,
                    "map_tool": map_tool if map_tool.exists() else None,
                    "navmesh": navmesh if navmesh.exists() else None,
                })

    def rowCount(self, parent=QtCore.QModelIndex()):
        if not parent.isValid():
            return len(self.scenes)
        return 0

    def columnCount(self, parent=QtCore.QModelIndex()):
        return 3

    def headerData(self, section, orientation, role=QtCore.Qt.DisplayRole):
        if orientation != QtCore.Qt.Horizontal or role != QtCore.Qt.DisplayRole:
            return None
        return ["Map", "Region", "Files"][section]

    def index(self, row, column, parent=QtCore.QModelIndex()):
        if not self.hasIndex(row, column, parent):
            return QtCore.QModelIndex()
        return self.createIndex(row, column, self.scenes[row])

    def parent(self, index):
        return QtCore.QModelIndex()

    def data(self, index, role=QtCore.Qt.DisplayRole):
        if not index.isValid() or role != QtCore.Qt.DisplayRole:
            return None
        scene = index.internalPointer()
        if index.column() == 0:
            return scene["name"]
        if index.column() == 1:
            return scene["region"]
        if index.column() == 2:
            parts = []
            for key in ("nif", "scene_luo", "objects", "monsters", "navmesh"):
                if scene[key]:
                    parts.append(key)
            return ", ".join(parts)
        return None


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, root: Path):
        super().__init__()
        self.setWindowTitle("QQxj asset viewer")
        self.resize(1500, 950)
        self.root = root
        self.cache_dir = Path(__file__).resolve().parent / "Temp" / "viewer_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.use_gpu_transforms = os.environ.get("QQXJ_USE_GPU_TRANSFORMS") == "1"

        # File tree
        self.model = QtWidgets.QFileSystemModel()
        self.model.setRootPath(str(root))
        self.tree = QtWidgets.QTreeView()
        self.tree.setModel(self.model)
        self.tree.setRootIndex(self.model.index(str(root)))
        self.tree.setColumnWidth(0, 320)
        self.tree.hideColumn(1)
        self.tree.hideColumn(2)
        self.tree.hideColumn(3)
        self.tree.clicked.connect(self.on_select)

        # Right tabs
        self.tabs = QtWidgets.QTabWidget()
        self._build_tabs()

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.addWidget(self.tree)
        splitter.addWidget(self.tabs)
        splitter.setSizes([350, 1150])
        self.setCentralWidget(splitter)
        self.statusBar().showMessage(f"root: {root}")

        # Menu
        menu = self.menuBar()
        file_menu = menu.addMenu("&File")
        act = QtGui.QAction("Choose &root...", self)
        act.triggered.connect(self._choose_root)
        file_menu.addAction(act)
        file_menu.addSeparator()
        act = QtGui.QAction("Reveal in &Explorer", self)
        act.triggered.connect(self._reveal_selected)
        file_menu.addAction(act)
        act = QtGui.QAction("Open OBJ in &Blender", self)
        act.triggered.connect(self._open_in_blender)
        file_menu.addAction(act)
        act = QtGui.QAction("&Export OBJ package", self)
        act.triggered.connect(self._export_obj_package)
        file_menu.addAction(act)
        act = QtGui.QAction("Export selected &scene OBJ", self)
        act.triggered.connect(self._export_scene_package)
        file_menu.addAction(act)

        view_menu = menu.addMenu("&Performance")
        self.gpu_action = QtGui.QAction("Use optional GPU transforms", self)
        self.gpu_action.setCheckable(True)
        self.gpu_action.setChecked(self.use_gpu_transforms)
        self.gpu_action.setToolTip("Uses CuPy for large vertex transform batches when CuPy/CUDA are installed.")
        self.gpu_action.toggled.connect(self._toggle_gpu_transforms)
        view_menu.addAction(self.gpu_action)
        clear_cache = QtGui.QAction("Clear viewer mesh cache", self)
        clear_cache.triggered.connect(self._clear_viewer_cache)
        view_menu.addAction(clear_cache)

    def _toggle_gpu_transforms(self, enabled: bool):
        self.use_gpu_transforms = enabled
        if enabled:
            os.environ["QQXJ_USE_GPU_TRANSFORMS"] = "1"
            self.statusBar().showMessage("optional GPU transforms enabled; requires CuPy/CUDA")
        else:
            os.environ.pop("QQXJ_USE_GPU_TRANSFORMS", None)
            self.statusBar().showMessage("optional GPU transforms disabled; using NumPy/CPU")

    def _clear_viewer_cache(self):
        removed = 0
        for path in self.cache_dir.rglob("*.meshcache.pkl"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        self.statusBar().showMessage(f"cleared {removed} cached mesh parse file(s)")

    def _build_tabs(self):
        font = QtGui.QFont("Consolas")
        font.setStyleHint(QtGui.QFont.Monospace)

        # Image
        self.image_label = QtWidgets.QLabel("(select an asset)")
        self.image_label.setAlignment(QtCore.Qt.AlignCenter)
        self.image_label.setMinimumSize(400, 400)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.image_label)
        self.tabs.addTab(scroll, "Image")

        # Lua
        self.lua_edit = QtWidgets.QPlainTextEdit()
        self.lua_edit.setReadOnly(True)
        self.lua_edit.setFont(font)
        self.tabs.addTab(self.lua_edit, "Lua")

        # 3D
        self.mesh_view = MeshView()
        self.tabs.addTab(self.mesh_view, "3D Model")

        # Animation
        self.anim_text = QtWidgets.QPlainTextEdit()
        self.anim_text.setReadOnly(True)
        self.anim_text.setFont(font)
        self.tabs.addTab(self.anim_text, "Animation")

        # Scenes
        self.scenes_view = QtWidgets.QWidget()
        sv_layout = QtWidgets.QHBoxLayout(self.scenes_view)
        self.scenes_tree = QtWidgets.QTreeView()
        self.scenes_tree.setRootIsDecorated(False)
        self.scenes_tree.setAlternatingRowColors(True)
        self.scenes_tree.clicked.connect(self._scene_selected)
        self.scenes_mesh_view = MeshView()
        sv_layout.addWidget(self.scenes_tree, 1)
        sv_layout.addWidget(self.scenes_mesh_view, 2)
        self.tabs.addTab(self.scenes_view, "Scenes")

        # Resource graph
        self.graph_view = QtWidgets.QWidget()
        graph_layout = QtWidgets.QVBoxLayout(self.graph_view)
        graph_bar = QtWidgets.QHBoxLayout()
        self.graph_refresh_btn = QtWidgets.QPushButton("Build graph")
        self.graph_status = QtWidgets.QLabel("(not built)")
        self.graph_status.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        graph_bar.addWidget(self.graph_refresh_btn)
        graph_bar.addWidget(self.graph_status, 1)
        self.graph_tree = QtWidgets.QTreeWidget()
        self.graph_tree.setHeaderLabels(["Node", "Type", "Path"])
        self.graph_tree.setColumnWidth(0, 320)
        self.graph_tree.setColumnWidth(1, 130)
        self.graph_tree.setAlternatingRowColors(True)
        self.graph_tree.itemClicked.connect(self._resource_graph_selected)
        self.graph_refresh_btn.clicked.connect(self._build_resource_graph)
        graph_layout.addLayout(graph_bar)
        graph_layout.addWidget(self.graph_tree, 1)
        self.tabs.addTab(self.graph_view, "Resource Graph")

        # Audio
        audio_panel = QtWidgets.QWidget()
        ap_layout = QtWidgets.QVBoxLayout(audio_panel)
        if HAS_QT_MULTIMEDIA:
            self.audio_label = QtWidgets.QLabel("(no audio loaded)")
            self.audio_play_btn = QtWidgets.QPushButton("Play / Pause")
            self.audio_stop_btn = QtWidgets.QPushButton("Stop")
            ap_layout.addWidget(self.audio_label)
            ap_layout.addWidget(self.audio_play_btn)
            ap_layout.addWidget(self.audio_stop_btn)
            ap_layout.addStretch()
            self.audio_player = QtMultimedia.QMediaPlayer()
            self.audio_output = QtMultimedia.QAudioOutput()
            self.audio_player.setAudioOutput(self.audio_output)
            self.audio_play_btn.clicked.connect(self._toggle_audio)
            self.audio_stop_btn.clicked.connect(self.audio_player.stop)
        else:
            self.audio_label = QtWidgets.QLabel(
                "Audio playback unavailable — PySide6.QtMultimedia not installed.\n\n"
                "To enable: pip install PySide6-Addons\n"
                "  (or in conda: pip install --upgrade PySide6)\n\n"
                "Until then, you can still browse audio files; the file path is\n"
                "shown in the Info tab so you can open them in another player."
            )
            self.audio_label.setWordWrap(True)
            ap_layout.addWidget(self.audio_label)
            ap_layout.addStretch()
            self.audio_player = None
            self.audio_output = None
        self.tabs.addTab(audio_panel, "Audio")

        # Info
        self.info_edit = QtWidgets.QPlainTextEdit()
        self.info_edit.setReadOnly(True)
        self.info_edit.setFont(font)
        self.tabs.addTab(self.info_edit, "Info")

        # Text fallback panel (for non-Lua text files)
        self.text_edit = QtWidgets.QPlainTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setFont(font)
        self.tabs.addTab(self.text_edit, "Text")

    # -----------------------------------------------------------------------

    def _set_scene_model(self):
        sm = SceneBrowserModel(self.root, self)
        self.scenes_tree.setModel(sm)
        self.scenes_tree.setColumnWidth(0, 250)
        self.scenes_tree.setColumnWidth(1, 90)

    def _add_graph_file_item(self, parent: QtWidgets.QTreeWidgetItem, label: str,
                             kind: str, path: Path | str | None) -> QtWidgets.QTreeWidgetItem:
        path_text = str(path) if path else ""
        item = QtWidgets.QTreeWidgetItem([label, kind, path_text])
        if path:
            item.setData(0, QtCore.Qt.UserRole, str(path))
        parent.addChild(item)
        return item

    def _build_resource_graph(self):
        self.graph_tree.clear()
        self.graph_status.setText("building...")
        QtWidgets.QApplication.processEvents()
        total = 0

        maps_root = QtWidgets.QTreeWidgetItem(["World maps", "group", str(self.root / "World" / "Map")])
        self.graph_tree.addTopLevelItem(maps_root)
        scene_model = SceneBrowserModel(self.root, self)
        for scene in scene_model.scenes:
            scene_item = QtWidgets.QTreeWidgetItem([scene["name"], "map", str(scene["dir"])])
            maps_root.addChild(scene_item)
            for label, key in [
                ("scene NIF", "nif"),
                ("scene LUO", "scene_luo"),
                ("objects LUO", "objects"),
                ("monsters LUO", "monsters"),
                ("map tool LUO", "map_tool"),
                ("navmesh", "navmesh"),
            ]:
                if scene.get(key):
                    self._add_graph_file_item(scene_item, label, key, scene[key])
            total += 1

        character_root = QtWidgets.QTreeWidgetItem(["Character parts", "group", str(self.root / "Character")])
        self.graph_tree.addTopLevelItem(character_root)
        fdb = self.root / "Character" / "PartsDB.fdb"
        if fdb.exists():
            try:
                sys.path.insert(0, str(Path(__file__).resolve().parent))
                import partsdb_probe
                import partsdb_resolve_assets

                _, records = partsdb_probe.decode(fdb, include_t=True)
                assets = partsdb_resolve_assets.build_assets(self.root, records, full_animations=False)
                for part in assets:
                    label = part.get("name") or f"part {part.get('part_id')}"
                    part_item = QtWidgets.QTreeWidgetItem([
                        label,
                        f"part {part.get('kind', '')}",
                        part.get("base_dir", ""),
                    ])
                    character_root.addChild(part_item)
                    mesh_path = part.get("mesh_path")
                    if mesh_path:
                        self._add_graph_file_item(part_item, "mesh", "nif", Path(mesh_path))
                    tex_parent = QtWidgets.QTreeWidgetItem(["textures", "group", ""])
                    part_item.addChild(tex_parent)
                    for tex in part.get("textures", [])[:20]:
                        path = tex.get("path")
                        name = tex.get("shape") or tex.get("texture") or "texture"
                        self._add_graph_file_item(tex_parent, name, "texture", Path(path) if path else None)
                    anim_parent = QtWidgets.QTreeWidgetItem(["animations", "group", ""])
                    part_item.addChild(anim_parent)
                    for anim in part.get("animations", [])[:20]:
                        path = anim.get("path")
                        self._add_graph_file_item(anim_parent, anim.get("name", "animation"), "kf", Path(path) if path else None)
                    total += 1
            except Exception as exc:
                self._add_graph_file_item(character_root, f"PartsDB parse failed: {exc}", "error", None)
        else:
            self._add_graph_file_item(character_root, "PartsDB.fdb not found", "missing", None)

        maps_root.setExpanded(True)
        character_root.setExpanded(True)
        self.graph_status.setText(f"{total} grouped nodes")

    def _resource_graph_selected(self, item: QtWidgets.QTreeWidgetItem, column: int):
        raw = item.data(0, QtCore.Qt.UserRole)
        if not raw:
            return
        path = Path(raw)
        if path.is_file():
            self._open_path_in_viewer(path)

    # -----------------------------------------------------------------------

    def _choose_root(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose extracted root", str(self.root))
        if not d:
            return
        self.root = Path(d)
        self.model.setRootPath(d)
        self.tree.setRootIndex(self.model.index(d))
        self.statusBar().showMessage(f"root: {self.root}")
        self._set_scene_model()

    def _reveal_selected(self):
        p = self._selected_path()
        if p:
            subprocess.Popen(["explorer", "/select,", str(p)])

    def _open_in_blender(self):
        p = self._selected_path()
        if not p or p.suffix.lower() != ".nif":
            QtWidgets.QMessageBox.information(self, "Open in Blender", "Select a .nif first.")
            return
        obj = get_obj_for_nif(p, self.cache_dir)
        if obj is None:
            QtWidgets.QMessageBox.warning(self, "Open in Blender", "NIF→OBJ conversion failed.")
            return
        for cand in [
            Path("C:/Program Files/Blender Foundation/Blender 5.1/blender.exe"),
            Path("C:/Program Files/Blender Foundation/Blender 4.5/blender.exe"),
            Path("C:/Program Files/Blender Foundation/Blender 4.3/blender.exe"),
        ]:
            if cand.exists():
                expr = textwrap.dedent(f"""
                    import bpy
                    from pathlib import Path

                    obj_path = Path({str(obj)!r})
                    bpy.ops.object.select_all(action='SELECT')
                    bpy.ops.object.delete()
                    bpy.ops.wm.obj_import(filepath=str(obj_path))

                    for mat in bpy.data.materials:
                        mat.use_nodes = True
                        mat.blend_method = 'BLEND'
                        mat.use_screen_refraction = True
                        bsdf = mat.node_tree.nodes.get('Principled BSDF')
                        if not bsdf:
                            continue
                        for node in mat.node_tree.nodes:
                            if node.type == 'TEX_IMAGE' and node.image:
                                mat.node_tree.links.new(node.outputs['Color'], bsdf.inputs['Base Color'])
                                if 'Alpha' in node.outputs and 'Alpha' in bsdf.inputs:
                                    mat.node_tree.links.new(node.outputs['Alpha'], bsdf.inputs['Alpha'])
                                break
                    bpy.ops.wm.save_as_mainfile(filepath=str(obj_path.with_suffix('.blend')))
                """)
                subprocess.Popen([str(cand), "--python-expr", expr])
                return
        QtWidgets.QMessageBox.warning(self, "Open in Blender", "Blender not found.")

    def _export_obj_package(self):
        p = self._selected_path()
        if not p or p.suffix.lower() != ".nif":
            QtWidgets.QMessageBox.information(self, "Export OBJ", "Select a .nif first.")
            return
        import qqxj_asset_pipeline as pipe

        result = pipe.export_nif_to_obj(p, resource_root=_resource_root_for(p), convert_to_png=True)
        if not result.get("ok"):
            QtWidgets.QMessageBox.warning(self, "Export OBJ", result.get("error", "Export failed."))
            return
        blend, blend_log = pipe.create_blend_for_obj(Path(result["obj"]), timeout=120)
        self.info_edit.setPlainText(
            "Exported OBJ package\n\n"
            f"OBJ: {result['obj']}\n"
            f"MTL: {result['mtl']}\n"
            f"BLEND: {blend if blend else '(not created)'}\n"
            f"textures resolved: {result['textures_resolved']}/{result['materials']}\n"
            f"report: {result['texture_report']}\n"
            f"helper: {Path(result['obj']).parent / 'open_in_blender_with_textures.py'}\n\n"
            f"{blend_log[-2000:] if blend_log else ''}"
        )

    def _selected_scene_dir(self) -> Path | None:
        idx = self.scenes_tree.currentIndex()
        if idx.isValid():
            scene = idx.internalPointer()
            if isinstance(scene, dict) and scene.get("dir"):
                return Path(scene["dir"])
        p = self._selected_path()
        if p:
            base = p if p.is_dir() else p.parent
            while base != base.parent:
                if (base / f"{base.name}.nif").exists():
                    return base
                base = base.parent
        return None

    def _export_scene_package(self):
        scene_dir = self._selected_scene_dir()
        if scene_dir is None:
            QtWidgets.QMessageBox.information(
                self, "Export Scene", "Select a scene row or a file inside a World/Map scene folder."
            )
            return
        out_dir = Path(__file__).resolve().parent / "Export_Obj" / "Scenes" / scene_dir.parent.name / scene_dir.name
        cmd = [
            sys.executable,
            str(Path(__file__).resolve().parent / "scene_obj_export.py"),
            "--scene",
            str(scene_dir),
            "--out",
            str(out_dir),
            "--root",
            str(self.root),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            QtWidgets.QMessageBox.warning(
                self, "Export Scene", proc.stderr or proc.stdout or "Scene export failed."
            )
            return
        scene_obj = out_dir / f"{scene_dir.name}_scene.obj"
        self.info_edit.setPlainText(
            "Exported scene OBJ package\n\n"
            f"scene: {scene_dir}\n"
            f"OBJ: {scene_obj}\n"
            f"MTL: {scene_obj.with_suffix('.mtl')}\n\n"
            f"{proc.stdout}"
        )

    def _selected_path(self) -> Path | None:
        idx = self.tree.currentIndex()
        if not idx.isValid():
            return None
        return Path(self.model.filePath(idx))

    def _toggle_audio(self):
        if not HAS_QT_MULTIMEDIA or self.audio_player is None:
            return
        if self.audio_player.playbackState() == QtMultimedia.QMediaPlayer.PlayingState:
            self.audio_player.pause()
        else:
            self.audio_player.play()

    # -----------------------------------------------------------------------

    def _open_path_in_viewer(self, path: Path):
        kind = file_kind(path)
        self.statusBar().showMessage(f"{kind}: {path}")
        if kind == "image":
            self._show_image(path)
            self.tabs.setCurrentWidget(self.tabs.widget(0))
        elif kind == "luo":
            self._show_luo(path)
            self.tabs.setCurrentWidget(self.lua_edit)
        elif kind == "text" or path.suffix.lower() == ".xml":
            self._show_text(path)
            self.tabs.setCurrentWidget(self.text_edit)
        elif kind == "mesh":
            self._show_mesh(path)
            self.tabs.setCurrentWidget(self.mesh_view)
        elif kind == "anim":
            self._show_anim(path)
            self.tabs.setCurrentWidget(self.anim_text)
        elif kind == "audio":
            self._show_audio(path)
            for i in range(self.tabs.count()):
                if self.tabs.tabText(i) == "Audio":
                    self.tabs.setCurrentIndex(i)
                    break
        else:
            self.info_edit.setPlainText(f"Unknown type: {path}")

    def on_select(self, index: QtCore.QModelIndex):
        path = Path(self.model.filePath(index))
        if not path.is_file():
            return
        self._open_path_in_viewer(path)

    def _show_image(self, path: Path):
        try:
            pix, info = load_image_qpixmap(path)
        except Exception as e:
            self.image_label.setText(f"Failed to load: {e}")
            return
        if pix.isNull():
            self.image_label.setText(f"Could not decode {path.name}")
            return
        scaled = pix.scaled(max(800, self.image_label.width()), 1200,
                            QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
        self.image_label.setPixmap(scaled)
        self.info_edit.setPlainText(f"{path}\n\n{info}")

    def _show_text(self, path: Path):
        try:
            data = path.read_bytes()
        except Exception as e:
            self.text_edit.setPlainText(f"Failed to read: {e}")
            return
        text, enc = decode_text(data)
        self.text_edit.setPlainText(text)
        self.info_edit.setPlainText(f"{path}\n\ndecoded as: {enc}")

    def _show_luo(self, path: Path):
        try:
            import luo_decompiler as ld
        except ImportError:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import luo_decompiler as ld
        src = ld.decompile_luo(path)
        if src:
            text, _ = decode_text(src.encode("latin1", errors="replace"))
            self.lua_edit.setPlainText(text)
            meta = ld.extract_part_meta(src)
            info_lines = [f"path: {path}"]
            if meta.get("mesh_file"):
                info_lines.append(f"mesh_file: {meta['mesh_file']}")
            if meta.get("attach_point"):
                info_lines.append(f"attach_point: {meta['attach_point']}")
            if meta.get("textures"):
                info_lines.append("texture map:")
                for shape, files in meta["textures"].items():
                    info_lines.append(f"  {shape}: {files}")
            self.info_edit.setPlainText("\n".join(info_lines))
            return
        # Fallback to partial summary
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from qqxj_viewer import parse_lua51_bytecode, format_luo_summary
        raw = path.read_bytes()
        parsed = parse_lua51_bytecode(raw)
        summary = format_luo_summary(parsed, raw)
        summary = (
            "## unluac decompilation unavailable; showing partial summary.\n"
            "## Install Java + place unluac.jar next to Res/ to get real Lua source.\n\n"
            + summary
        )
        self.lua_edit.setPlainText(summary)
        self.info_edit.setPlainText(f"{path}\n(Lua 5.1 bytecode; partial decode)")

    def _show_mesh(self, path: Path):
        obj = get_obj_for_nif(path, self.cache_dir)
        if obj is None:
            self.info_edit.setPlainText(f"NIF→OBJ failed for {path}")
            return
        # Rewrite MTL using LUO textures so Blender can also open the OBJ correctly.
        try:
            mtl_count = rewrite_mtl_with_luo_textures(obj, path, [self.root])
        except Exception as e:
            mtl_count = -1
            print(f"[viewer] MTL rewrite failed: {e}")
        # Resolve textures from LUO (preferred) or fall back to MTL/path search.
        try:
            import luo_decompiler as ld
        except ImportError:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import luo_decompiler as ld
        texture_map: dict[str, list[str]] = {}
        luo = ld.find_part_luo_for_nif(path)
        if luo is not None:
            src = ld.decompile_luo(luo)
            if src:
                texture_map = ld.extract_texture_map(src)
        # Search texture roots: same dir as NIF first, then archive root, then full tree.
        archive_root = path
        while archive_root.parent != self.root and archive_root.parent.parent != archive_root:
            archive_root = archive_root.parent
        search_roots = [path.parent, archive_root, self.root]
        shape_names = shape_names_in_nif(path)
        meshes = load_obj_meshes_with_textures(obj, shape_names, texture_map, search_roots)
        texture_anim_meta = attach_texture_animation_curves(meshes, path)
        info_extra = []
        info_extra.append(f"NIF: {path}")
        info_extra.append(f"cached OBJ: {obj}")
        export_info = obj.parent / "export_info.json"
        if export_info.exists():
            try:
                import json
                info = json.loads(export_info.read_text(encoding="utf-8"))
                source = info.get("texture_map_source") or "mtl/path"
                info_extra.append(f"texture map source: {source}")
                ttc = int(info.get("texture_transform_controllers", 0) or 0)
                flips = int(info.get("flip_controllers", 0) or 0)
                if ttc or flips:
                    info_extra.append(
                        f"texture animation controllers: transform={ttc}, flip={flips}"
                    )
                    if info.get("texture_controller_report"):
                        info_extra.append(f"texture controller report: {info['texture_controller_report']}")
                if info.get("partsdb"):
                    pdb = info["partsdb"]
                    info_extra.append(
                        f"PartsDB: {pdb.get('part_name')} "
                        f"(id={pdb.get('part_id')}, kind={pdb.get('part_kind')})"
                    )
            except Exception:
                pass
        if luo:
            info_extra.append(f"part LUO: {luo.name}")
        if texture_anim_meta.get("controllers"):
            info_extra.append(
                "texture animation preview: "
                f"{texture_anim_meta.get('animated_meshes', 0)} mesh(es), "
                f"{texture_anim_meta.get('float_curves', 0)} curve(s)"
            )
            if texture_anim_meta.get("kf"):
                info_extra.append(f"texture animation KF: {texture_anim_meta['kf']}")
        info_extra.append(f"shape names in NIF: {shape_names}")
        info_extra.append(f"texture map from LUO: {texture_map}")
        sibling_kfs = sorted(path.parent.glob("*.kf"))
        sibling_kfms = sorted(path.parent.glob("*.kfm"))
        if sibling_kfms:
            info_extra.append("KFM files:")
            info_extra.extend(f"  - {p.name}" for p in sibling_kfms[:20])
        if sibling_kfs:
            info_extra.append(f"KF animations in same folder: {len(sibling_kfs)}")
            info_extra.extend(f"  - {p.name}" for p in sibling_kfs[:30])
            if len(sibling_kfs) > 30:
                info_extra.append(f"  ... {len(sibling_kfs) - 30} more")
        def info_cb(txt):
            self.info_edit.setPlainText("\n".join(info_extra) + "\n\n" + txt)
        self.mesh_view.show_meshes(meshes, info_cb=info_cb)

    def _show_anim(self, path: Path):
        try:
            import nextsoft_kfm_parser as kfm_mod
        except ImportError:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import nextsoft_kfm_parser as kfm_mod
        raw = path.read_bytes()
        ext = path.suffix.lower()
        sibling_nifs = [p.name for p in path.parent.glob("*.nif")][:10]
        try:
            if ext == ".kfm":
                kfm = kfm_mod.parse_kfm(raw)
                summary = kfm_mod.summarize_kfm(kfm)
                texture_summary = ""
                transform_summary = ""
            else:
                summary = kfm_mod.summarize_kf(raw)
                try:
                    import nextsoft_texture_controller_probe as controller_probe
                    tex_anim = controller_probe.find_texture_controllers(path)
                    float_rows = tex_anim.get("float_data", [])
                    texture_lines = [
                        "## texture animation curves",
                        f"float curves: {len(float_rows)}/{tex_anim.get('expected_float_data', 0)}",
                    ]
                    for row in float_rows[:12]:
                        keys = row.get("keys", [])
                        if keys:
                            first = keys[0]
                            last = keys[-1]
                            texture_lines.append(
                                f"  - block {row.get('block_index')}: "
                                f"{row.get('interpolation_name')} "
                                f"{first.get('time')}:{first.get('value')} -> "
                                f"{last.get('time')}:{last.get('value')}"
                            )
                    texture_summary = "\n".join(texture_lines) + "\n\n"
                except Exception as e:
                    texture_summary = f"## texture animation curves\nprobe failed: {e}\n\n"
                try:
                    import nextsoft_kf_transform_probe as transform_probe
                    tracks = transform_probe.probe_transform_tracks(path)
                    rows = tracks.get("transform_tracks", [])
                    sequence = tracks.get("controller_sequence", {})
                    controlled = sequence.get("controlled_blocks", [])
                    bound = tracks.get("bound_transform_tracks", [])
                    transform_lines = [
                        "## transform tracks",
                        f"sequence: {sequence.get('sequence_name', '')}",
                        f"controlled nodes: {len(controlled)}",
                        f"tracks: {len(rows)}/{tracks.get('expected_transform_data', 0)}",
                    ]
                    if controlled:
                        transform_lines.append("")
                        transform_lines.append("controller sequence:")
                        for cb in controlled[:16]:
                            node = cb.get("node_name") or f"offset {cb.get('node_name_offset')}"
                            ctrl = cb.get("controller_type") or "controller"
                            prop = cb.get("property_type")
                            cid = cb.get("controller_id")
                            suffix = ""
                            if prop:
                                suffix += f" property={prop}"
                            if cid:
                                suffix += f" id={cid}"
                            transform_lines.append(f"  - {node}: {ctrl}{suffix}")
                        if len(controlled) > 16:
                            transform_lines.append(f"  ... {len(controlled) - 16} more")
                    if bound:
                        transform_lines.append("")
                        transform_lines.append("bound transform tracks:")
                        for item in bound[:16]:
                            cb = item.get("controlled_block", {})
                            row = item.get("track") or {}
                            node = cb.get("node_name") or f"offset {cb.get('node_name_offset')}"
                            trans = row.get("translation_keys", [])
                            rot = row.get("rotation_keys", [])
                            scale = row.get("scale_keys", [])
                            parts = []
                            if trans:
                                parts.append(f"T={len(trans)}")
                            if rot:
                                parts.append(f"R={len(rot)}")
                            if scale:
                                parts.append(f"S={len(scale)}")
                            if not row:
                                parts.append("no data block found")
                            transform_lines.append(
                                f"  - {node}: block {row.get('block_index', item.get('guessed_data_ref'))} "
                                + (", ".join(parts) if parts else "empty")
                            )
                        if len(bound) > 16:
                            transform_lines.append(f"  ... {len(bound) - 16} more")
                    elif rows:
                        transform_lines.append("")
                        transform_lines.append("raw transform data blocks:")
                        for row in rows[:12]:
                            trans = row.get("translation_keys", [])
                            rot = row.get("rotation_keys", [])
                            scale = row.get("scale_keys", [])
                            parts = []
                            if trans:
                                parts.append(f"T={len(trans)}")
                            if rot:
                                parts.append(f"R={len(rot)}")
                            if scale:
                                parts.append(f"S={len(scale)}")
                            transform_lines.append(
                                f"  - block {row.get('block_index')}: "
                                + (", ".join(parts) if parts else "empty")
                            )
                    transform_summary = "\n".join(transform_lines) + "\n\n"
                except Exception as e:
                    transform_summary = f"## transform tracks\nprobe failed: {e}\n\n"
        except Exception as e:
            summary = f"parse failed: {e}\n\nFirst 64 bytes: {raw[:64].hex()}"
            texture_summary = ""
            transform_summary = ""
        body = (
            f"## {path.name}\n"
            f"path: {path}\n"
            f"size: {path.stat().st_size} bytes\n\n"
            f"## sibling NIFs (likely targets)\n"
            + "\n".join(f"  - {n}" for n in sibling_nifs)
            + f"\n\n## parsed contents\n{summary}\n\n"
            + texture_summary
            + transform_summary
            + f"## roadmap\n"
            f"  - KFM listing: done.\n"
            f"  - KF controller sequence and transform tracks: parsed.\n"
            f"  - Skinned-mesh playback: not yet — needs full NIF parser to bind\n"
            f"    the skeleton and apply NiKeyframeData transforms over time.\n"
        )
        self.anim_text.setPlainText(body)

    def _show_audio(self, path: Path):
        if HAS_QT_MULTIMEDIA and self.audio_player is not None:
            url = QtCore.QUrl.fromLocalFile(str(path))
            self.audio_player.setSource(url)
            self.audio_label.setText(f"loaded: {path.name} ({path.stat().st_size} bytes)")
            self.info_edit.setPlainText(f"{path}\n\nPress Play to listen.")
        else:
            self.info_edit.setPlainText(
                f"{path}\n\nAudio playback unavailable — install PySide6-Addons.\n"
                "You can open this file in an external player (it's a standard "
                f"{path.suffix.lstrip('.').upper()} file)."
            )

    def _scene_selected(self, index: QtCore.QModelIndex):
        if not index.isValid():
            return
        scene = index.internalPointer()
        if scene.get("nif"):
            self._show_mesh(scene["nif"])
            # also surface in scenes' own mesh view
            try:
                obj = get_obj_for_nif(scene["nif"], self.cache_dir)
                if obj is not None:
                    sys.path.insert(0, str(Path(__file__).resolve().parent))
                    import luo_decompiler as ld
                    luo = ld.find_part_luo_for_nif(scene["nif"])
                    texture_map = ld.extract_texture_map(ld.decompile_luo(luo) or "") if luo else {}
                    shape_names = shape_names_in_nif(scene["nif"])
                    meshes = load_obj_meshes_with_textures(
                        obj, shape_names, texture_map,
                        [scene["nif"].parent, self.root],
                    )
                    self.scenes_mesh_view.show_meshes(meshes)
            except Exception as e:
                self.info_edit.appendPlainText(f"scene mesh load failed: {e}")
        # Also dump composition info
        lines = [f"Map: {scene['name']}", f"Region: {scene['region']}", ""]
        for key in ("nif", "scene_luo", "objects", "monsters", "map_tool", "navmesh"):
            v = scene[key]
            lines.append(f"  {key}: {v if v else '(missing)'}")
        if scene.get("scene_luo") or scene.get("objects"):
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import luo_decompiler as ld
            for k in ("scene_luo", "objects", "monsters", "map_tool"):
                v = scene.get(k)
                if not v:
                    continue
                src = ld.decompile_luo(v)
                if src:
                    lines.append(f"\n## {v.name}\n{src[:2000]}")
                    if len(src) > 2000:
                        lines.append(f"\n... ({len(src)} bytes truncated)")
        self.info_edit.setPlainText("\n".join(lines))


# ---------------------------------------------------------------------------
# Backwards-compat LUO summarizer (used by _show_luo fallback path).
# ---------------------------------------------------------------------------


def parse_lua51_bytecode(data: bytes) -> dict:
    if data[:4] != b"\x1bLua" or data[4] != 0x51:
        return {"error": "not Lua 5.1"}
    fmt, endian, int_sz, sz_sz, inst_sz, num_sz, integral = data[5:12]
    pos = 12

    def U32():
        nonlocal pos
        v, = struct.unpack_from("<I", data, pos)
        pos += 4
        return v

    def U8():
        nonlocal pos
        v = data[pos]
        pos += 1
        return v

    def DOUBLE():
        nonlocal pos
        v, = struct.unpack_from("<d", data, pos)
        pos += 8
        return v

    def STRING():
        nonlocal pos
        n = U32()
        if n == 0:
            return ""
        s = data[pos:pos + n - 1].decode("latin1", errors="replace")
        pos += n
        return s
    try:
        source = STRING()
        line_def = U32()
        last_line = U32()
        nups = U8()
        params = U8()
        vararg = U8()
        max_stack = U8()
        ncode = U32()
        pos += ncode * inst_sz
        sizek = U32()
        strings: list[str] = []
        numbers: list[float] = []
        for _ in range(sizek):
            tt = U8()
            if tt == 0:
                pass
            elif tt == 1:
                _ = U8()
            elif tt == 3:
                numbers.append(DOUBLE())
            elif tt == 4:
                strings.append(STRING())
            else:
                return {"error": f"unknown const type {tt}", "strings": strings}
        return dict(source=source, line_defined=line_def, last_line=last_line,
                    num_upvalues=nups, num_params=params, is_vararg=vararg,
                    max_stack=max_stack, num_instructions=ncode,
                    constants=dict(strings=strings, numbers=numbers))
    except Exception as e:
        return {"error": str(e)}


def format_luo_summary(parsed: dict, raw: bytes) -> str:
    out = [f"# Lua 5.1 bytecode ({len(raw)} bytes)"]
    if "error" in parsed:
        out.append(f"# parser bailed: {parsed['error']}")
    if "source" in parsed:
        out.append(f"# source: {parsed['source']}")
        out.append(f"# lines {parsed.get('line_defined', 0)}..{parsed.get('last_line', 0)}")
    c = parsed.get("constants") or {}
    if c.get("strings"):
        out.append("\n## String constants")
        for s in c["strings"]:
            out.append(f"  {s!r}")
    if c.get("numbers"):
        out.append("\n## Number constants")
        for n in c["numbers"]:
            out.append(f"  {n}")
    return "\n".join(out)


# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser()
    here = Path(__file__).resolve().parent
    default_root = here / "extracted_named_v4"
    ap.add_argument("--root", default=str(default_root))
    args = ap.parse_args()
    root = Path(args.root)
    if not root.exists():
        print(f"[!] root does not exist: {root}")
        return 1
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    w = MainWindow(root)
    w._set_scene_model()
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
