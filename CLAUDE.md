# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AGR Baker v2 is a Blender 5.0+ addon for professional texture baking, atlas creation, UDIM workflows, and asset renaming. It lives entirely in `Blender_Scripts/addons/AGR_baker_v2/`.

## Development Setup

- **No build system** — this is a pure Blender Python addon. Deploy by copying `AGR_baker_v2/` to Blender's addons directory and enabling it.
- **No test suite** — test manually in Blender's system console (Window → Toggle System Console).
- **Dependencies**: `bpy` (Blender API), optional `Pillow` (resizing, frame overlays). The addon auto-detects missing Pillow and offers installation via `AGR_OT_InstallPillow`.

## Architecture

### Module Layout

| Module | Purpose |
|---|---|
| `__init__.py` | Addon registration, Pillow dependency check, submodule imports |
| `properties.py` | All `PropertyGroup` definitions stored on `bpy.types.Scene` |
| `ui.py` | UI panels, UILists, and layout code |
| `core/baking.py` | Image creation, bake execution, resolution detection from node trees |
| `core/materials.py` | Material/shader node setup, texture loading, BSDF connections |
| `core/texture_sets.py` | Texture set scanning, folder management, PNG alpha detection |
| `operators_bake.py` | Selected-to-active baking and simple bake operators |
| `operators_atlas.py` | Atlas creation, preview, application, and unpacking (largest module) |
| `operators_udim.py` | UDIM tiling workflows |
| `operators_sets.py` | Texture set CRUD, resize, connect, assign, batch operations |
| `operators_rename.py` | Object/material/texture renaming with dialog UIs |
| `operators_rename_project.py` | Bulk project-wide renaming across all assets |
| `operators_convert.py` | Material-to-TextureSet extraction |
| `operators_frame.py` | Border/frame overlay on textures using Pillow |
| `operators_utils.py` | Utility operators (Pillow install) |

### Registration Pattern

Each operator module exposes `register()` / `unregister()` functions. `__init__.py` calls them all during addon lifecycle.

### Dual-Mode Material System

The addon supports two texture set modes, both stored under `AGR_BAKE/`:
- **HIGH mode** (`S_*` folders): Packed textures — ERM (Emission/Roughness/Metallic combined) + DiffuseOpacity (RGBA)
- **LOW mode** (`S_*` folders): Separate textures — individual Diffuse, Normal, Roughness, Metallic, Opacity, Emit PNGs

Atlas folders use the `A_*` prefix with the same dual-mode distinction.

### Naming Conventions

- Objects: `SM_Address_Type` (e.g., `SM_Broadway10_Main`)
- Materials: `M_name` (material), `S_name` (texture set folder), `A_name` (atlas folder)
- Textures: `T_materialName_TextureType.png` (e.g., `T_Wall_Diffuse.png`)
- LOW atlas short suffixes: `_d` (diffuse), `_do` (diffuseOpacity), `_r`, `_m`, `_o`, `_n`, `_e`, `_erm`

### Key Technical Details

- **Alpha detection**: Reads PNG IHDR chunk byte 9 (color_type) directly — avoids loading full image data.
- **Resolution detection**: Traces shader node connections recursively to find texture nodes and extract resolution.
- **Node tree manipulation**: Clears and rebuilds Principled BSDF connections; supports both legacy Separate RGB and modern Separate Color nodes.
- **File structure**: `AGR_BAKE/` folder sits next to the `.blend` file. `AGR_BAKE_UDIM/` for UDIM workflows.
- **Logging**: Extensive `print()` statements with emoji prefixes throughout — check Blender's system console for debug output.
