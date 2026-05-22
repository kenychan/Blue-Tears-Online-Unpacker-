# Blue Tears Online Unpacker

Extraction and asset-conversion tools for the
Nextsoft/Gamebryo resource format used by Blue Tears Online / QQ Xian Jing / Punch Monster.

This is an ongoing documentation of a hobby project and this repository is for educational purpose only. 
I do it out of childhood nostalgia and love for this early-deceased game. Please refrain from any commerical use. 
It contains resource extraction, asset viewing scripts and the development diary.

## Current Status

This is the first practical version:

- `*.SFAi` archive extraction with named folder reconstruction works for the
  TW 2012 Client (DragonLegend_120911) resource set and the related CN 2009 Client (QQXJ_Alpha_V0.2.3) layout when the matching keys
  and indices are available.
- NIF to OBJ export works for static meshes, scene/map NIFs, embedded or
  external textures, texture aliases, alpha hookup metadata, collision/debug
  geometry, and many QQXJ-specific Gamebryo 10.2.0.5 block variants.
- LUO decompilation and object graph extraction works well enough to group map
  scene data into game objects instead of raw file lists.
- `Character/PartsDB.fdb` decoding works for TW character parts and texture
  variants.
- KFM/KF export now resolves base models and transform-track bindings into
  JSON/CSV. Full animated playback/export is still in progress.


Known limits:

- Some scenes still have missing textures or placement details; these are now
  treated as parser/object-graph gaps rather than archive extraction failures.
- Viewer playback is not yet a real skeletal/scene animation player.
- Very large scene exports are currently CPU/Python heavy. The next milestone
  is continued parser work and better object-graph scene assembly. The current
  exporter already uses NumPy transforms, reusable export caches, fast texture
  lookup, and optional CuPy/CUDA transforms for large vertex batches.


## Setup

Use Python 3.10+ or a Conda environment.

```powershell
pip install -r requirements.txt
```

Optional tools:

- Blender, for opening exported OBJ/MTL packages and generated import scripts.
- Ghidra + JDK, for reproducing the TW client reverse-engineering exports.
- Java, for LUO decompilation through `unluac.jar`.



## Extract Resources

```powershell
.\tools\sfa\extract_resources.ps1 `
  -ArchiveRoot input\TW\Res `
  -RuntimeImage input\TW.NClient.runtime_image.bin `
  -Out data\TW_extracted_named
```

The important output is a named tree such as:

```text
data/TW_extracted_named/
  World/
  Character/
  Effects/
  Item/
  Sound/
```

## Export A NIF With Textures

```powershell
python tools\assets\qqxj_asset_pipeline.py export-nif `
  data\TW_extracted_named\World\Object\cw\block_cw_theatre.nif `
  --resource-root data\TW_extracted_named `
  --out exports\obj\block_cw_theatre
```

Outputs include:

- `*.obj` and `*.mtl`
- copied texture files with Blender-safe names
- `texture_resolution.csv`
- `texture_aliases.csv`
- `open_in_blender_with_textures.py`

Texture-controller metadata is slower and is opt-in for static OBJ export:

```powershell
python tools\assets\qqxj_asset_pipeline.py export-nif `
  data\TW_extracted_named\World\Object\cw\block_cw_theatre.nif `
  --resource-root data\TW_extracted_named `
  --out exports\obj\block_cw_theatre `
  --with-controller-metadata
```

## Export A Map Scene

```powershell
python tools\assets\scene_obj_export.py `
  --scene data\TW_extracted_named\World\Map\ch\ch_bldg_bedroom1 `
  --root data\TW_extracted_named `
  --out exports\scenes\ch_bldg_bedroom1
```

Use `--include-objects` to follow object references from LUO files when the
scene needs placed object NIFs beyond the base map NIF.

## Export Character Parts

```powershell
python tools\assets\partsdb_probe.py `
  data\TW_extracted_named\Character\PartsDB.fdb `
  --out exports\partsdb_probe

python tools\assets\qqxj_asset_pipeline.py export-part `
  back_T15MBack-L_mantle `
  --resource-root data\TW_extracted_named `
  --out exports\parts\back_T15MBack-L_mantle
```

`export-part` uses `PartsDB.fdb` to select the correct mesh and texture
variant instead of relying on same-basename texture guessing.

## Export KFM/KF Animation Metadata

```powershell
python tools\assets\qqxj_asset_pipeline.py export-anim `
  data\TW_extracted_named\Effects\Projection\FX_MonsterDynamiteDirection.kfm `
  --resource-root data\TW_extracted_named `
  --out exports\anim\FX_MonsterDynamiteDirection
```

Outputs include:

- `<name>_animation.json`, with resolved base NIF, resolved KF files, and
  per-animation transform bindings.
- `<name>_transform_tracks.csv`, with node names, controller types, and key
  counts for quick inspection.

## LUO Object Graphs

```powershell
python tools\assets\luo_object_graph.py `
  data\TW_extracted_named\World\Map\vf\vf_upper_cloud1\vf_upper_cloud1.luo `
  --root data\TW_extracted_named `
  --out work\vf_upper_cloud1.graph.json
```

This is the data model intended for the next viewer generation: maps, rooms,
object placements, portals, triggers, monsters, NPCs, sounds, and scene
components represented as game-data nodes instead of plain folders.

## GUI Viewer

```powershell
python tools\viewer\qqxj_viewer.py --root data\TW_extracted_named
```

The viewer is useful for exploration, but it is still slower than the command
line exporters on first load of large scenes. Repeated NIF loads use cached OBJ
packages and cached parsed mesh arrays. The Performance menu can enable
optional GPU transforms if CuPy/CUDA are installed; otherwise the parser uses
NumPy on CPU.
