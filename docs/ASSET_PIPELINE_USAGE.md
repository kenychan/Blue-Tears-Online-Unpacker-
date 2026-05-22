# QQXJ asset pipeline usage

## Export a NIF to Blender-ready OBJ

```powershell
cd <repo-or-working-copy>
python .\Res\qqxj_asset_pipeline.py export-nif .\Res\extracted_named_v4\Character\back\T80WBack\G_W_Mantle.nif --resource-root .\Res\extracted_named_v4
```

Output goes under `Res\Export_Obj\...` unless `--out` is passed.

The exported folder contains:

- `.obj`
- `.mtl`
- copied/converted `.png` textures beside the OBJ
- `texture_resolution.csv`
- `export_info.json`

`texture_resolution.csv` records exactly which LUO shape or NIF/MTL texture reference resolved to which real texture file.

## Validate in Blender

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.1\blender.exe" --background --python .\Res\blender_validate_objs.py -- .\Res\Export_Obj\Character\back\T80WBack\G_W_Mantle .\Res\Export_Obj\Character\back\T80WBack\G_W_Mantle\blender_validation.csv
```

The validator now checks that image files actually exist on disk. `textured_ok=1` means Blender imported mesh data, UVs, materials, and found real texture files.

Important: some large World NIFs contain `NiPixelData` blocks with embedded DXT texture payloads. The exporter now reconstructs those payloads as DDS, converts them to PNG when Pillow can read them, and points the MTL at the generated image beside the OBJ. In the smoke test `chage_berserker_Devil.nif`, all 32 material slots resolved to existing image files.

## Export/decompile LUO

```powershell
python .\Res\qqxj_asset_pipeline.py export-luo .\Res\extracted_named_v4\Character\back\T80WBack\back_T80WBack_mantle.luo
```

If Java and `Res\unluac.jar` are available, this writes decompiled Lua plus `luo_meta.json`.

## Build a map object graph from LUO

```powershell
python .\tools\assets\luo_object_graph.py .\data\extracted_named\World\Map\vf\vf_upper_cloud1\vf_upper_cloud1.luo --root .\data\extracted_named --out .\work\vf_upper_cloud1.graph.json
```

This decompiles the map LUO, resolves `include(...)`, and auto-loads sibling scene scripts such as `*_Objects.luo`, `*_Monsters.luo`, and `*_MapToolData.luo`. The output is the first viewer-ready object graph: map scene resource, rooms, volumes, positions, portals, NPC/object spawns, trigger records, and their source locations. When `--root` is provided, object nodes also get `resolved_refs` pointing at likely class/data scripts such as `Lua/Object/Main/Do/<ObjectName>.luo`, trigger LUOs, NPC data tables, and NPC text records.

If you have already generated `partsdb_assets.csv`, pass it to enrich class/data LUOs that contain `CharacterDescription(...)` with real mesh/animation candidates. This works for direct `TDo...` class LUOs and for NPC records embedded inside combined NPC data tables:

```powershell
python .\tools\assets\partsdb_resolve_assets.py .\data\extracted_named\Character\PartsDB.fdb --root .\data\extracted_named --out .\work\partsdb_assets
python .\tools\assets\luo_object_graph.py .\data\extracted_named\World\Map\vf\vf_upper_cloud1\vf_upper_cloud1.luo --root .\data\extracted_named --partsdb-assets .\work\partsdb_assets\partsdb_assets.csv --out .\work\vf_upper_cloud1.graph.json
```

For a compact list of only object nodes:

```powershell
python .\tools\assets\luo_object_graph.py .\data\extracted_named\World\Map\vf\vf_upper_cloud1\vf_upper_cloud1.luo --root .\data\extracted_named --flat-objects --out .\work\vf_upper_cloud1.objects.json
```

## Export KFM/KF metadata

```powershell
python .\Res\qqxj_asset_pipeline.py export-anim .\Res\extracted_named_v4\Character\effect\Common-ProjectionTest\ProjectionTest.kfm
```

This writes a readable summary, JSON, and CSV animation list for KFM files. Full skinned animation playback/export is not implemented yet; KF files are currently parsed at metadata/block-list level.

For `.kf` files the JSON now also includes:

- `controller_sequence`: resolved `NiControllerSequence` target node names, controller types, and texture-controller IDs.
- `bound_transform_tracks`: best-effort binding from each `NiTransformController` node to its `NiTransformData` keys.
- `texture_animation`: `NiTextureTransformController`/`NiFloatData` curve data for texture scrolling and flip-style effects.

This works on the TW 2012 client samples and the CN 2009 samples tested so far. OBJ export remains static; OBJ cannot carry skeletal animation. Animation export will need glTF/FBX/DAE or a Blender Python importer that creates armatures/actions.

## GUI viewer

```powershell
cd <repo-or-working-copy>
python .\Res\qqxj_viewer.py --root .\Res\extracted_named_v4
```

The viewer supports:

- NIF preview/export via the shared pipeline
- LUO decompile/metadata
- KFM/KF summaries with controller-sequence node names and transform-track counts
- DDS/TGA/PNG/JPG/BMP images
- WAV/OGG audio when QtMultimedia is available
- world scene browsing
- resource graph browsing for map bundles and PartsDB character part bundles

Viewer cache is under `Res\Temp\viewer_cache`, not Windows temp.


