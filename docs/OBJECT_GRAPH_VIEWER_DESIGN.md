# Object Graph Viewer Design

Goal: replace "folder browsing with previews" as the main workflow. The file
tree should remain a forensic/debug tool. The main viewer should show game
objects assembled from the same data the client uses: LUO, PartsDB, KFM/KF,
NIF, texture files, embedded NiPixelData, sounds, and map metadata.

## Principle

An object node is not a directory. It is a resolved bundle:

- identity: stable name/id from LUO, PartsDB, map metadata, or filename fallback
- model: one or more NIFs
- materials: per-shape texture bindings, static TexDesc UV transforms, alpha
- animation: KFM/KF list, current sequence, controller targets
- placement: transform, cell/map membership, attachment point
- dependencies: external texture/sound/script paths, embedded NiPixelData
- export actions: static OBJ package, scene OBJ package, later glTF/Blender
  action package

## Top-Level Graph

```text
Root
  Maps
    <map id/name>
      Scene root
        static map NIF
        LUO scene metadata
        objects from *_Objects.luo
        monsters from *_Monsters.luo
        sounds from map sound data
        navmesh/collision/debug layers
  Characters
    <part/category>
      <part object>
        mesh NIF
        PartsDB texture variants
        KFM/KF animation set
        attach point / parent chain
  Effects
    <effect object>
      model/planes
      texture-scroll controllers
      flip controllers
      sibling KF curves
  Raw Files
    folder explorer fallback
```

## Node Preview Contract

Clicking one object node should load a composed preview, not a raw file:

- map node: assembled static scene plus object placements when supported
- map object node: selected object model, transform, textures, LUO metadata
- character part node: mesh with correct PartsDB variant textures; animation
  selector shows resolved KFs
- animation node: target model context plus sequence/controller tree
- texture node: image plus references that use it

If a dependency is unresolved, the node should show a red/yellow diagnostic row
inside its dependency panel rather than silently falling back to folder order.

## Export Contract

Every object node should support:

- `Export static OBJ package`: geometry/materials/textures at selected frame
- `Export dependency manifest`: JSON containing all source files and resolver
  decisions
- `Export scene OBJ package`: for map nodes; includes placement transforms and
  one grouped debug/collision mesh
- later `Export glTF/Blender package`: skeleton, skin, KFs as actions, animated
  UV curves as material animation where possible

OBJ is allowed only for static snapshots. It cannot carry skeletal animation or
time-varying UV transforms faithfully.

## Data Sources

Current implemented sources:

- `PartsDB.fdb`: character part composition, inherited mesh/texture/animation
  lookups
- LUO decompile: `m_Texture` shape-to-texture mapping and script metadata
- NIF parser: geometry, UVs, materials, external texture refs, embedded
  NiPixelData, static texture transforms, texture controllers
- KF parser: `NiControllerSequence`, target node names, transform tracks, float
  curves for texture controllers
- KFM parser: model filename and animation list



## Speed Design

The object graph should be backed by indexes, not repeated recursive searches:

- build one path index per resource root
- cache PartsDB decode by file mtime/size
- cache LUO decompile/meta by file mtime/size
- cache NIF lightweight block graph separately from heavy mesh extraction
- cache texture image conversion by source mtime/size
- lazy-load meshes only when node preview opens

This lets the node tree open instantly while expensive geometry/texture work
happens only for the selected node.
