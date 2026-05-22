"""
Blender background validator for OBJ exports.

Run with:
  blender --background --python blender_validate_objs.py -- <obj-dir> <report.csv>
"""

from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

import bpy


def args_after_double_dash() -> list[str]:
    if "--" not in sys.argv:
        return []
    return sys.argv[sys.argv.index("--") + 1:]


def clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()


def material_image_status(objects) -> tuple[int, int, str]:
    images = set()
    existing = set()
    missing = []
    for obj in objects:
        for slot in obj.material_slots:
            mat = slot.material
            if not mat or not mat.use_nodes or not mat.node_tree:
                continue
            for node in mat.node_tree.nodes:
                if node.type == "TEX_IMAGE" and node.image:
                    images.add(node.image.name)
                    filepath = bpy.path.abspath(node.image.filepath or "", library=node.image.library)
                    if filepath and os.path.exists(filepath):
                        existing.add(filepath)
                    else:
                        missing.append(node.image.filepath or node.image.name)
    return len(images), len(existing), ";".join(sorted(set(missing)))


def import_obj(path: Path) -> tuple[int, int, int, int, int, int, str, str]:
    clear_scene()
    try:
        path = path.resolve()
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=str(path))
        else:
            bpy.ops.import_scene.obj(filepath=str(path))
    except Exception as exc:
        return 0, 0, 0, 0, 0, 0, "", f"import failed: {exc}"

    verts = faces = uv_layers = material_slots = 0
    mesh_objects = []
    for obj in bpy.context.scene.objects:
        if obj.type == "MESH":
            mesh_objects.append(obj)
            verts += len(obj.data.vertices)
            faces += len(obj.data.polygons)
            uv_layers += len(obj.data.uv_layers)
            material_slots += len(obj.material_slots)
    images, existing_images, missing_images = material_image_status(mesh_objects)
    return verts, faces, uv_layers, material_slots, images, existing_images, missing_images, ""


def main() -> int:
    args = args_after_double_dash()
    if len(args) != 2:
        print("usage: blender --background --python blender_validate_objs.py -- <obj-dir> <report.csv>")
        return 2

    obj_root = Path(args[0])
    report = Path(args[1])
    rows = []
    for path in obj_root.rglob("*.obj"):
        verts, faces, uv_layers, material_slots, images, existing_images, missing_images, error = import_obj(path)
        rows.append({
            "obj": str(path),
            "vertices": verts,
            "faces": faces,
            "uv_layers": uv_layers,
            "material_slots": material_slots,
            "images": images,
            "existing_image_files": existing_images,
            "missing_image_files": missing_images,
            "ok": int(not error and verts > 0 and faces > 0),
            "textured_ok": int(not error and verts > 0 and faces > 0 and uv_layers > 0 and existing_images > 0 and not missing_images),
            "error": error,
        })

    report.parent.mkdir(parents=True, exist_ok=True)
    with report.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "obj",
                "vertices",
                "faces",
                "uv_layers",
                "material_slots",
                "images",
                "existing_image_files",
                "missing_image_files",
                "ok",
                "textured_ok",
                "error",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    ok = sum(int(r["ok"]) for r in rows)
    textured = sum(int(r["textured_ok"]) for r in rows)
    print(f"validated {ok}/{len(rows)} OBJ files, textured={textured}/{len(rows)} -> {report}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
