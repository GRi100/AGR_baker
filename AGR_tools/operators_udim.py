"""
UDIM operators for AGR Tools
"""

import bpy
from bpy.types import Operator
from bpy.props import StringProperty
from bpy.app.handlers import persistent
import os
import json
import math
import re
import shutil
from pathlib import Path

from .core.udim_store import (UDIM_STORE, read_udim_record, write_udim_record,
                              strip_udim_record)
from .core.udim_tiles import EPS, uv_to_udim_number, face_tile_number
from .log import agr_report, unregister_classes
from .rename_shared import parse_sm_name


def process_object_name(obj_name):
    """Extract (address, obj_type) from 'SM_Address[_NNN]_Type[.001]'.

    Delegates to the shared SM_ parser (rename_shared.parse_sm_name) so all
    7 object types and Blender's '.001' suffix are handled the same way as in
    AGR Rename / AGR Atlas; the optional 3-digit number stays part of the
    address, exactly as the old split-by-underscore code produced it.
    """
    parsed = parse_sm_name(obj_name)
    if parsed is None:
        raise ValueError("Object name must be in format SM_Address[_NNN]_Type")
    address, number, obj_type = parsed
    if number:
        address = f"{address}_{number}"
    return address, obj_type


def get_udim_directory_name(address, obj_type):
    """Generate UDIM folder name based on object type"""
    if obj_type == 'Main':
        return f"SM_{address}"
    else:  # Ground or other types
        return f"SM_{address}_Ground"


def get_udim_texture_name(address, obj_type, tex_type, udim_number):
    """Generate UDIM texture name based on object type"""
    if obj_type == 'Main':
        return f"T_{address}_{tex_type}_1.{udim_number:04d}.png"
    else:  # Ground
        return f"T_{address}_Ground_{tex_type}_1.{udim_number:04d}.png"


def get_udim_material_name(address, obj_type):
    """Generate UDIM material name based on object type"""
    if obj_type == 'Main':
        return f"M_{address}_Main_1"
    else:  # Ground
        return f"M_{address}_Ground_1"


def _get_output_folder():
    """Get output folder name from settings, default AGR_BAKE"""
    try:
        return bpy.context.scene.agr_baker_settings.output_folder
    except Exception:
        return "AGR_BAKE"


def setup_udim_material_nodes(material):
    """Setup basic nodes for UDIM material"""
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    
    nodes.clear()
    
    output = nodes.new(type='ShaderNodeOutputMaterial')
    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    
    output.location = (400, 0)
    bsdf.location = (100, 0)
    
    links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
    
    return nodes, links, bsdf


def save_udim_mapping_json(udim_dir, material_mapping):
    """Save material mapping to JSON file"""
    json_path = os.path.join(udim_dir, "udim_mapping.json")
    
    try:
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(material_mapping, f, indent=2, ensure_ascii=False)
        print(f"✅ Saved UDIM mapping to: {json_path}")
        return True
    except Exception as e:
        print(f"❌ Error saving UDIM mapping: {e}")
        return False


def load_udim_mapping_json(udim_dir):
    """Load material mapping from JSON file"""
    json_path = os.path.join(udim_dir, "udim_mapping.json")
    
    if not os.path.exists(json_path):
        return None
    
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            mapping = json.load(f)
        print(f"✅ Loaded UDIM mapping from: {json_path}")
        return mapping
    except Exception as e:
        print(f"❌ Error loading UDIM mapping: {e}")
        return None


def load_udim_mapping(obj, udim_dir, migrate=True):
    """UDIM mapping for the object: per-object record first, the legacy
    on-disk udim_mapping.json as a fallback (migrated onto the object on
    first use).  NEVER call with migrate=True from draw()/poll()."""
    record = read_udim_record(obj)
    if record is not None:
        return record
    legacy = load_udim_mapping_json(str(udim_dir))
    if legacy is not None and obj is not None and migrate:
        if write_udim_record(obj, legacy):
            print(f"📍 UDIM: legacy JSON mapping migrated onto '{obj.name}'")
    return legacy


def save_udim_mapping(obj, udim_dir, mapping):
    """Store the mapping ON THE OBJECT (idprop + color mirror).  The
    on-disk udim_mapping.json stays legacy: read as a fallback, never
    written anymore (udim_dir is kept in the signature for symmetry)."""
    return write_udim_record(obj, mapping)


def find_udim_record_carrier(udim_dir, exclude=None, peek=False):
    """Object of this .blend carrying a UDIM record for the given tile
    folder.  The folder is SHARED by every non-Main type of one address
    (Ground/GroundEl/Flora all collapse into SM_<addr>_Ground), but the
    record lives on whichever object created the UDIM — operators running
    from a sibling object must find and update THAT record instead of
    forking a second, diverging one.  Returns (obj, mapping) or (None,
    None).

    peek=True uses the cached, never-mutating read — the only variant
    allowed from poll()/draw()."""
    want = os.path.basename(str(udim_dir))
    for other in bpy.data.objects:
        if other is exclude or other.type != 'MESH':
            continue
        # O(1) gate: only true record carriers are read
        if other.get(UDIM_STORE.prop_key) is None:
            data = getattr(other, "data", None)
            if data is None or data.attributes.get(UDIM_STORE.prefix + "0") is None:
                continue
        rec = UDIM_STORE.peek(other) if peek else read_udim_record(other)
        if not rec:
            continue
        addr = rec.get("address")
        otype = rec.get("obj_type", "Ground")
        if addr and get_udim_directory_name(addr, otype) == want:
            return other, rec
    return None, None


def find_udim_directory(address, obj_type, base_dir, use_main_dir=False):
    """Find UDIM directory in AGR_BAKE or root directory
    
    Args:
        address: Object address from name
        obj_type: Object type (Main, Ground, etc.)
        base_dir: Base directory (blend file parent)
        use_main_dir: If True, prioritize root directory over AGR_BAKE
    
    Search order depends on use_main_dir:
    - If True: 1. base_dir/SM_address, 2. base_dir/AGR_BAKE/SM_address
    - If False: 1. base_dir/AGR_BAKE/SM_address, 2. base_dir/SM_address
    
    Returns Path object if found, None otherwise
    """
    udim_dir_name = get_udim_directory_name(address, obj_type)
    
    if use_main_dir:
        # Prioritize root directory
        udim_dir_root = base_dir / udim_dir_name
        if udim_dir_root.exists():
            print(f"✅ Found UDIM directory in root: {udim_dir_root}")
            return udim_dir_root
        
        # Fallback to AGR_BAKE
        agr_bake_dir = base_dir / _get_output_folder()
        udim_dir_agr = agr_bake_dir / udim_dir_name
        if udim_dir_agr.exists():
            print(f"✅ Found UDIM directory in AGR_BAKE: {udim_dir_agr}")
            return udim_dir_agr
    else:
        # Prioritize AGR_BAKE (default)
        agr_bake_dir = base_dir / _get_output_folder()
        udim_dir_agr = agr_bake_dir / udim_dir_name
        if udim_dir_agr.exists():
            print(f"✅ Found UDIM directory in AGR_BAKE: {udim_dir_agr}")
            return udim_dir_agr
        
        # Fallback to root directory
        udim_dir_root = base_dir / udim_dir_name
        if udim_dir_root.exists():
            print(f"✅ Found UDIM directory in root: {udim_dir_root}")
            return udim_dir_root
    
    print(f"❌ UDIM directory not found: {udim_dir_name}")
    return None


def scan_udim_tiles_in_dir(udim_dir):
    """Scan directory for UDIM tile numbers (1001-1999).  A missing folder
    is simply an empty set — callers use this as an "is the shared address
    folder already occupied?" probe before creating anything."""
    tiles = set()

    if not os.path.isdir(str(udim_dir)):
        return tiles

    for filename in os.listdir(udim_dir):
        if not filename.lower().endswith('.png'):
            continue

        match = re.search(r'\.(\d{4})\.png$', filename)
        if not match:
            continue

        udim_number = int(match.group(1))
        if 1001 <= udim_number <= 1999:
            tiles.add(udim_number)

    return tiles


def find_tile_textures_in_dir(udim_dir, udim_number):
    """Find texture files for a specific UDIM tile, typed by name substring"""
    tile_textures = {}

    for filename in os.listdir(udim_dir):
        if not filename.lower().endswith('.png'):
            continue

        match = re.search(r'\.(\d{4})\.png$', filename)
        if not match:
            continue

        file_udim = int(match.group(1))
        if file_udim != udim_number:
            continue

        if 'Diffuse' in filename:
            tile_textures['Diffuse'] = os.path.join(udim_dir, filename)
        elif 'ERM' in filename:
            tile_textures['ERM'] = os.path.join(udim_dir, filename)
        elif 'Normal' in filename:
            tile_textures['Normal'] = os.path.join(udim_dir, filename)

    return tile_textures


def _png_has_alpha(filepath):
    """Fast alpha check via PNG IHDR — delegates to the shared parser."""
    from .core.texture_sets import png_has_alpha
    return png_has_alpha(filepath)


# poll() and panel draw() run on every redraw — scanning every node tree each
# time lags the UI on heavy scenes. Fingerprint (material names + node counts)
# is O(slots) instead of O(nodes); UDIM operators clear the cache explicitly.
# Keyed by session_uid, NOT by name: names are reused across files and after a
# rename, and the consumer in operators_uv decides from this value whether to
# flatten a face into 0..1 — a stale True/False there rewrites UVs.
_has_udim_cache = {}


def invalidate_udim_cache():
    _has_udim_cache.clear()


@persistent
def _invalidate_udim_cache_on_load(_dummy):
    # A different .blend brings different objects; session_uid is unique per
    # session but the cache must not outlive the file it was built from.
    _has_udim_cache.clear()


def _tiled_images_signature():
    """Cheap global signal that the set of TILED images changed.  Flipping
    image.source in the shader editor (or swapping the image inside an
    existing node) leaves material names and node counts untouched, so the
    per-object fingerprint alone cannot see it.  bpy.data.images is dozens
    of entries — orders of magnitude cheaper than walking every node tree."""
    count = 0
    acc = 0
    for img in bpy.data.images:
        if img.source == 'TILED':
            count += 1
            acc ^= img.as_pointer()
    return count, acc


def object_has_udim(obj):
    """True when any material of obj uses a TILED (UDIM) image. Cached."""
    if not obj or obj.type != 'MESH':
        return False
    try:
        fingerprint = (
            tuple(
                (slot.material.name, len(slot.material.node_tree.nodes))
                for slot in obj.material_slots
                if slot.material and slot.material.use_nodes
            ),
            _tiled_images_signature(),
        )
    except Exception:
        fingerprint = None

    cached = _has_udim_cache.get(obj.session_uid)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]

    has_udim = False
    for slot in obj.material_slots:
        if slot.material and slot.material.use_nodes:
            for node in slot.material.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image and node.image.source == 'TILED':
                    has_udim = True
                    break
        if has_udim:
            break

    _has_udim_cache[obj.session_uid] = (fingerprint, has_udim)
    return has_udim


def tiles_with_uv(obj):
    """Set of UDIM tiles actually occupied by the object's UV faces
    (per-face centroid, the shared rule of core/udim_tiles)."""
    mesh = obj.data
    if not mesh.uv_layers.active:
        return set()

    # Per-loop Python access costs seconds on a 500k-poly Ground and this
    # runs in invoke(), before the picker even opens — pull the whole UV
    # layer in one foreach_get and average per face with numpy.
    try:
        import numpy as np
        n_loops = len(mesh.loops)
        uvs = np.empty(n_loops * 2, dtype=np.float32)
        mesh.uv_layers.active.data.foreach_get("uv", uvs)
        uvs = uvs.reshape(n_loops, 2)
        n_polys = len(mesh.polygons)
        if n_polys == 0 or n_loops == 0:
            return set()
        starts = np.empty(n_polys, dtype=np.int32)
        totals = np.empty(n_polys, dtype=np.int32)
        mesh.polygons.foreach_get("loop_start", starts)
        mesh.polygons.foreach_get("loop_total", totals)
        # Per-face Python loop cost 2.6 s on 200k polys and this now runs
        # once per sibling in the revert / picker guards.  loop_start is
        # monotonic and loop_total > 0 for every real polygon, so the whole
        # centroid pass is one reduceat.  Arithmetic is 1:1 with
        # core.udim_tiles.uv_to_udim_number (right-closed columns/rows,
        # column clamped to 9, invalid zone dropped).
        starts64 = starts.astype(np.int64)
        totals64 = np.maximum(totals.astype(np.int64), 1)
        cu = np.add.reduceat(uvs[:, 0].astype(np.float64), starts64) / totals64
        cv = np.add.reduceat(uvs[:, 1].astype(np.float64), starts64) / totals64
        valid = ((cu >= -EPS) & (cv >= -EPS) &
                 (cu <= 10 + EPS) & (cv <= 10 + EPS) & (totals > 0))
        col = np.minimum(np.maximum(np.floor(cu - EPS).astype(np.int64), 0), 9)
        row = np.maximum(np.floor(cv - EPS).astype(np.int64), 0)
        return set((1001 + col + row * 10)[valid].tolist())
    except Exception as exc:
        print(f"⚠️ tiles_with_uv: numpy path failed ({exc}), falling back")

    uv_data = mesh.uv_layers.active.data
    used = set()
    for poly in mesh.polygons:
        tile = face_tile_number([uv_data[li].uv for li in poly.loop_indices])
        if tile is not None:
            used.add(tile)
    return used


# Zombie protection for the tile-picker modals, mirroring operators_library:
# a modal killed WITHOUT its modal() running again (File→Open, addon
# reloadOnSave — the standard dev loop of this project) leaves a live draw
# handler bound to a dead instance plus its __agr_udim_preview_* datablocks.
_active_hud = None
_hud_token_counter = 0

# Tiles wider than this are not decoded for the 128px HUD preview
_PREVIEW_MAX_SIDE = 8192


def _cleanup_active_hud():
    global _active_hud
    if _active_hud is not None:
        try:
            _active_hud._hud_finish(bpy.context)
        except Exception:
            pass
        _active_hud = None


def _drop_stale_handlers(handler_list, name):
    """Remove handlers left by a previous addon reload (same __name__,
    different function object)."""
    for handler in list(handler_list):
        if getattr(handler, "__name__", None) == name:
            try:
                handler_list.remove(handler)
            except ValueError:
                pass


@persistent
def _on_load_pre_hud(_dummy):
    # Draw handlers and temp datablocks must not survive a file switch
    _cleanup_active_hud()


class AGR_UDIMGridHUD:
    """Shared modal HUD for UDIM tile tools: grid geometry with a resizable
    corner (drag the top-right handle), lazy tile previews, viewport
    navigation pass-through (wheel/MMB). Subclasses call _hud_start() in
    invoke, _handle_common() first in modal, _hud_finish() on exit, and
    draw via _draw_hud_base() (optionally adding on top)."""

    _GAP = 6
    _ORIGIN = (60, 60)
    _COLS = 10

    # ---- lifecycle ----

    def _hud_start(self, context, udim_dir, tiles, tile_to_material, status):
        global _active_hud, _hud_token_counter
        # Only one picker at a time; a previous one (or a zombie from an
        # earlier reload) is closed before this one paints anything.
        _cleanup_active_hud()
        self._udim_dir = str(udim_dir)
        self._cell = 64
        self._slots = {t: ((t - 1001) % 10, (t - 1001) // 10) for t in tiles}
        self._labels = {t: tile_to_material.get(t, '') for t in tiles}
        self._rows = max(r for _, r in self._slots.values()) + 2
        self._mouse = (0, 0)
        self._hover_cell = None
        self._resizing = False
        self._status = status
        self._load_tile_previews(tiles)
        self._handle = bpy.types.SpaceView3D.draw_handler_add(
            self._draw_hud, (context,), 'WINDOW', 'POST_PIXEL')
        context.window_manager.modal_handler_add(self)
        # Ownership token: an addon reload resets the WM property to 0, so
        # the surviving instance sees a mismatch on its next event and
        # cleans itself up instead of drawing forever.
        _hud_token_counter += 1
        self._udim_token = _hud_token_counter
        try:
            context.window_manager.agr_udim_hud_token = self._udim_token
        except Exception:
            self._udim_token = None
        _active_hud = self
        context.area.tag_redraw()

    def _load_tile_previews(self, tiles):
        """Small GPU textures of each tile's Diffuse for the HUD grid.
        Private temp datablocks, downscaled in-place, removed in _hud_finish
        — never touches the scene's TILED images."""
        import gpu
        from .core.texture_sets import read_png_ihdr
        self._gpu_textures = {}
        self._preview_images = []
        for tile in tiles:
            tex_files = find_tile_textures_in_dir(self._udim_dir, tile)
            path = tex_files.get('Diffuse') or next(iter(tex_files.values()), None)
            if not path:
                continue
            # Each preview shrinks to 128px right after loading, so the
            # RETAINED cost is tiny — but the full-resolution decode is
            # not, and an oversized tile would freeze the click that opens
            # the picker. Header-only probe, then a plain colour cell.
            try:
                ihdr = read_png_ihdr(path)
                if ihdr and max(ihdr[0], ihdr[1]) > _PREVIEW_MAX_SIDE:
                    print(f"⚠️ UDIM HUD: tile {tile} is {ihdr[0]}x{ihdr[1]} — preview skipped")
                    continue
            except Exception:
                pass
            try:
                img = bpy.data.images.load(path, check_existing=False)
                img.name = f"__agr_udim_preview_{tile}"
                img.scale(128, 128)
                self._gpu_textures[tile] = gpu.texture.from_image(img)
                self._preview_images.append(img.name)
            except Exception as e:
                print(f"⚠️ UDIM HUD: preview failed for tile {tile}: {e}")

    def _hud_finish(self, context):
        global _active_hud
        if _active_hud is self:
            _active_hud = None
            try:
                bpy.context.window_manager.agr_udim_hud_token = 0
            except Exception:
                pass
        if getattr(self, '_handle', None):
            bpy.types.SpaceView3D.draw_handler_remove(self._handle, 'WINDOW')
            self._handle = None
        # Release GPU textures BEFORE removing their source datablocks
        self._gpu_textures = {}
        for name in getattr(self, '_preview_images', []):
            img = bpy.data.images.get(name)
            if img:
                bpy.data.images.remove(img)
        self._preview_images = []
        if context.area:
            context.area.tag_redraw()

    # ---- grid geometry ----

    def _cell_rect(self, col, row):
        step = self._cell + self._GAP
        x = self._ORIGIN[0] + col * step
        y = self._ORIGIN[1] + row * step
        return x, y, x + self._cell, y + self._cell

    def _grid_bounds(self):
        step = self._cell + self._GAP
        x0, y0 = self._ORIGIN
        return x0, y0, x0 + self._COLS * step - self._GAP, y0 + self._rows * step - self._GAP

    def _corner_rect(self):
        """Resize handle at the top-right corner of the grid."""
        _, _, x1, y1 = self._grid_bounds()
        return x1 - 8, y1 - 8, x1 + 14, y1 + 14

    def _cell_at(self, mx, my):
        step = self._cell + self._GAP
        col = (mx - self._ORIGIN[0]) // step
        row = (my - self._ORIGIN[1]) // step
        if col < 0 or col >= self._COLS or row < 0 or row >= self._rows:
            return None
        x0, y0, x1, y1 = self._cell_rect(col, row)
        if mx > x1 or my > y1:  # landed in the gap between cells
            return None
        return int(col), int(row)

    def _tile_at_cell(self, cell):
        for tile, slot in self._slots.items():
            if slot == cell:
                return tile
        return None

    # ---- common events ----

    def _handle_common(self, context, event):
        """Shared event handling. Returns a modal result set or None when
        the subclass should process the event itself."""
        # Ownership check first: a reload zeroed the token, or a newer
        # picker took over — this instance is a zombie.  Only instances
        # started through _hud_start() carry a token (operators_library
        # reuses this mixin with its OWN token and must not be touched).
        token = getattr(self, '_udim_token', None)
        if token is not None:
            wm_token = getattr(context.window_manager, 'agr_udim_hud_token', 0)
            if wm_token != token:
                self._udim_token = None   # do not zero the newer owner's token
                self._hud_finish(context)
                return {'CANCELLED'}

        if context.area:
            context.area.tag_redraw()

        # Keep viewport navigation alive under the HUD
        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'MIDDLEMOUSE',
                          'TRACKPADPAN', 'TRACKPADZOOM'}:
            return {'PASS_THROUGH'}

        if event.type == 'MOUSEMOVE':
            self._mouse = (event.mouse_region_x, event.mouse_region_y)
            if self._resizing:
                # The corner follows the cursor: cell size derives from width
                step = (event.mouse_region_x - self._ORIGIN[0]) / self._COLS
                self._cell = int(max(38, min(200, step - self._GAP)))
                self._hover_cell = None
            else:
                self._hover_cell = self._cell_at(*self._mouse)
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE':
            mx, my = event.mouse_region_x, event.mouse_region_y
            cx0, cy0, cx1, cy1 = self._corner_rect()
            if event.value == 'PRESS' and cx0 <= mx <= cx1 and cy0 <= my <= cy1:
                self._resizing = True
                return {'RUNNING_MODAL'}
            if event.value == 'RELEASE' and self._resizing:
                self._resizing = False
                return {'RUNNING_MODAL'}

        return None

    # ---- drawing ----

    def _px_rect(self, x0, y0, x1, y1, color):
        import gpu
        from gpu_extras.batch import batch_for_shader
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        batch = batch_for_shader(shader, 'TRIS', {
            "pos": [(x0, y0), (x1, y0), (x1, y1), (x0, y0), (x1, y1), (x0, y1)]})
        shader.bind()
        shader.uniform_float("color", color)
        batch.draw(shader)

    def _px_image(self, x0, y0, x1, y1, texture):
        import gpu
        from gpu_extras.batch import batch_for_shader
        shader = gpu.shader.from_builtin('IMAGE')
        batch = batch_for_shader(shader, 'TRIS', {
            "pos": [(x0, y0), (x1, y0), (x1, y1), (x0, y0), (x1, y1), (x0, y1)],
            "texCoord": [(0, 0), (1, 0), (1, 1), (0, 0), (1, 1), (0, 1)],
        })
        shader.bind()
        shader.uniform_sampler("image", texture)
        batch.draw(shader)

    def _px_tile(self, x0, y0, x1, y1, tile):
        import blf
        texture = self._gpu_textures.get(tile)
        if texture is not None:
            self._px_rect(x0 - 1, y0 - 1, x1 + 1, y1 + 1, (0.05, 0.05, 0.05, 0.9))
            self._px_image(x0, y0, x1, y1, texture)
        else:
            self._px_rect(x0, y0, x1, y1, (0.25, 0.5, 0.9, 0.75))
        # Number badge readable on any texture
        self._px_rect(x0, y1 - 18, x0 + 40, y1, (0.0, 0.0, 0.0, 0.6))
        blf.size(0, 13)
        blf.color(0, 1.0, 1.0, 1.0, 1.0)
        blf.position(0, x0 + 4, y1 - 15, 0)
        blf.draw(0, str(tile))
        label = self._labels.get(tile, '')
        if label and (x1 - x0) >= 48:
            self._px_rect(x0, y0, x1, y0 + 14, (0.0, 0.0, 0.0, 0.6))
            blf.size(0, 10)
            blf.position(0, x0 + 4, y0 + 3, 0)
            blf.draw(0, label[:14])

    def _draw_hud_base(self, title, skip_tile=None):
        import gpu
        import blf
        gpu.state.blend_set('ALPHA')

        # Empty grid cells (hovered one highlighted)
        for row in range(self._rows):
            for col in range(self._COLS):
                x0, y0, x1, y1 = self._cell_rect(col, row)
                hovered = self._hover_cell == (col, row)
                self._px_rect(x0, y0, x1, y1, (1.0, 1.0, 1.0, 0.16 if hovered else 0.07))

        # Occupied tiles
        for tile, (col, row) in self._slots.items():
            if tile == skip_tile:
                continue
            x0, y0, x1, y1 = self._cell_rect(col, row)
            self._px_tile(x0, y0, x1, y1, tile)

        # Resize handle: triangle at the top-right grid corner
        _, _, gx1, gy1 = self._grid_bounds()
        active = self._resizing
        import gpu as _gpu
        from gpu_extras.batch import batch_for_shader as _bfs
        shader = _gpu.shader.from_builtin('UNIFORM_COLOR')
        batch = _bfs(shader, 'TRIS', {
            "pos": [(gx1 + 12, gy1 - 8), (gx1 + 12, gy1 + 12), (gx1 - 8, gy1 + 12)]})
        shader.bind()
        shader.uniform_float("color", (1.0, 0.75, 0.2, 0.95) if active else (0.85, 0.85, 0.85, 0.7))
        batch.draw(shader)

        # Header + status line above the grid
        blf.size(0, 13)
        blf.color(0, 1.0, 1.0, 1.0, 1.0)
        top = gy1 + 22
        blf.position(0, self._ORIGIN[0], top, 0)
        blf.draw(0, title)
        if self._status:
            blf.position(0, self._ORIGIN[0], top + 18, 0)
            blf.color(0, 1.0, 0.85, 0.4, 1.0)
            blf.draw(0, self._status)

        gpu.state.blend_set('NONE')


def scan_texture_sets_for_udim(context, obj):
    """Scan AGR_BAKE folder for texture sets matching object materials.

    Returns (texture_sets, skipped): the caller MUST look at `skipped` —
    silently dropping a slot used to leave its faces in tile 1001 wearing
    another material's texture while its own material was unlinked."""
    texture_sets = []
    skipped = []

    # Get AGR_BAKE folder
    blend_path = bpy.data.filepath
    if not blend_path:
        print("⚠️ Blend file not saved")
        return texture_sets, skipped

    from pathlib import Path
    base_dir = Path(blend_path).parent
    agr_bake_dir = base_dir / _get_output_folder()

    if not agr_bake_dir.exists():
        print(f"⚠️ AGR_BAKE folder not found: {agr_bake_dir}")
        skipped = [f"{slot.material.name} (нет папки {_get_output_folder()})"
                   for slot in obj.material_slots if slot.material]
        return texture_sets, skipped

    print(f"🔍 Scanning AGR_BAKE for texture sets...")

    # Slots WITHOUT faces carry no geometry into the UDIM, so a missing
    # texture set for them cannot leave anything wearing the wrong tile —
    # they are dropped by materials.clear() anyway.  Blocking Create UDIM on
    # such a leftover slot (Ctrl+J, deleted geometry, an old material) forced
    # the user to clean slots by hand for no gain.
    used_slots = set()
    try:
        import numpy as np
        idx = np.empty(len(obj.data.polygons), dtype=np.int32)
        obj.data.polygons.foreach_get('material_index', idx)
        used_slots = set(np.unique(idx).tolist())
    except Exception as exc:
        print(f"⚠️ UDIM: material_index scan failed ({exc}) — every slot is kept")
        used_slots = set(range(len(obj.material_slots)))

    for mat_idx, slot in enumerate(obj.material_slots):
        if not slot.material:
            continue
        if mat_idx not in used_slots:
            print(f"  ⏭️ Material {slot.material.name}: slot has no faces — dropped")
            continue

        material = slot.material
        material_name = material.name
        
        # Look for S_material_name folder
        set_folder = agr_bake_dir / f"S_{material_name}"
        
        if not set_folder.exists():
            print(f"  ⚠️ Material {material_name}: No texture set folder found (S_{material_name})")
            skipped.append(f"{material_name} (нет сета S_{material_name})")
            continue

        # Check for required textures: DiffuseOpacity (or Diffuse), ERM, Normal
        diffuse_opacity_path = set_folder / f"T_{material_name}_DiffuseOpacity.png"
        diffuse_path = set_folder / f"T_{material_name}_Diffuse.png"
        erm_path = set_folder / f"T_{material_name}_ERM.png"
        normal_path = set_folder / f"T_{material_name}_Normal.png"
        
        # Use DiffuseOpacity if exists, otherwise Diffuse
        if diffuse_opacity_path.exists():
            final_diffuse_path = str(diffuse_opacity_path)
        elif diffuse_path.exists():
            final_diffuse_path = str(diffuse_path)
        else:
            final_diffuse_path = None
        
        # Check if we have all required textures
        has_diffuse = final_diffuse_path is not None
        has_erm = erm_path.exists()
        has_normal = normal_path.exists()
        
        if has_diffuse and has_erm and has_normal:
            texture_sets.append({
                'material_index': mat_idx,
                'material_name': material_name,
                'diffuse_path': final_diffuse_path,
                'erm_path': str(erm_path),
                'normal_path': str(normal_path)
            })
            print(f"  ✅ Material {material_name}: Found complete texture set")
        else:
            missing = []
            if not has_diffuse:
                missing.append("Diffuse/DiffuseOpacity")
            if not has_erm:
                missing.append("ERM")
            if not has_normal:
                missing.append("Normal")
            print(f"  ⚠️ Material {material_name}: Missing textures: {', '.join(missing)}")
            skipped.append(f"{material_name} (нет: {', '.join(missing)})")

    print(f"✅ Found {len(texture_sets)} complete texture sets")
    return texture_sets, skipped


def material_has_tiled_image(material):
    """True when the material drives a TILED (UDIM) image."""
    if not material or not material.use_nodes:
        return False
    for node in material.node_tree.nodes:
        if node.type == 'TEX_IMAGE' and node.image and node.image.source == 'TILED':
            return True
    return False


def find_udim_material_for_dir(obj, udim_dir):
    """The object's material whose TILED image lives in `udim_dir`.  Used
    to hand a sibling of the same address the EXISTING UDIM material
    instead of a second `M_<addr>_Ground_1.001` datablock."""
    if obj is None:
        return None
    want = os.path.normcase(os.path.abspath(str(udim_dir)))
    for slot in obj.material_slots:
        mat = slot.material
        if not mat or not mat.use_nodes:
            continue
        for node in mat.node_tree.nodes:
            if node.type != 'TEX_IMAGE' or not node.image:
                continue
            if node.image.source != 'TILED':
                continue
            path = bpy.path.abspath(node.image.filepath)
            if not path:
                continue
            if os.path.normcase(os.path.dirname(os.path.abspath(path))) == want:
                return mat
    return None


def objects_using_udim_dir(udim_dir, exclude=None):
    """Every mesh of this .blend whose material drives a TILED image from
    `udim_dir`.  The folder is SHARED by all non-Main types of one address,
    so any operator that renames, deletes or re-numbers tiles has to know
    the siblings — otherwise it moves the files out from under them."""
    out = []
    for other in bpy.data.objects:
        if other is exclude or other.type != 'MESH':
            continue
        if find_udim_material_for_dir(other, udim_dir):
            out.append(other)
    return out


def revert_tile_scope(obj, udim_dir, actual_tiles, mapping):
    """Split the tiles found on disk into "this object's" and "not its
    business" for a revert.

    The folder is SHARED by every non-Main type of one address, so:
      * a tile whose UV faces belong to a SIBLING is skipped — disassembling
        it built generic M_#_#### materials and empty slots on the wrong
        object and pulled the tile out of the sibling's record;
      * a tile that is neither in the mapping nor occupied by this object's
        own UVs is skipped too — those are the leftover files of a sibling
        that already reverted (revert never deletes tile files), and they
        used to resurface as junk materials on the next object.

    Returns (tiles_to_revert, skipped_tiles, mapping_subset, own_tiles,
    sibling_objects); `mapping` is returned trimmed to the tiles in scope.
    """
    own = tiles_with_uv(obj)
    others = objects_using_udim_dir(udim_dir, exclude=obj)
    skipped = set()
    for other in others:
        skipped |= tiles_with_uv(other)
    skipped -= own

    json_tiles = set()
    if mapping:
        json_tiles = {t.get('udim_number') for t in mapping.get('udim_tiles', [])}
        skipped |= (set(actual_tiles) - json_tiles - own)

    tiles = set(actual_tiles) - skipped
    if mapping and skipped:
        mapping = dict(mapping)
        mapping['udim_tiles'] = [t for t in mapping.get('udim_tiles', [])
                                 if t.get('udim_number') not in skipped]
    return tiles, skipped, mapping, own, others


def claim_canonical_material_name(material, canonical):
    """Give `material` the canonical UDIM name even when an ORPHANED
    same-named datablock still squats it.  The object's own materials are
    unlinked right before the UDIM material is assigned, so the source
    `M_<addr>_Ground_1` sits at 0 users and would otherwise force the new
    material into `...1.001` — a name that fails the city's FBX naming
    check.  A live or fake-user datablock is never touched."""
    if material.name == canonical:
        return True
    squatter = bpy.data.materials.get(canonical)
    if squatter is None:
        material.name = canonical
        return True
    if squatter is material:
        return True
    if squatter.users == 0 and not material_has_tiled_image(squatter):
        squatter.name = canonical + ".src"
        material.name = canonical
        print(f"📍 UDIM: freed the canonical name from an orphaned material -> {squatter.name}")
        return True
    print(f"⚠️ UDIM: name '{canonical}' is taken by a material in use — keeping '{material.name}'")
    return False


def borrow_carrier_mapping(obj, udim_dir):
    """Mapping for an object that has no record of its own: the SHARED
    address folder may be owned by a sibling.  Only the tiles this object's
    UVs actually occupy are kept — the sibling's tiles are none of its
    business and would otherwise become empty material slots."""
    carrier, mapping = find_udim_record_carrier(udim_dir, exclude=obj)
    if carrier is None or not mapping:
        return None
    own_tiles = tiles_with_uv(obj)
    tiles = [t for t in mapping.get('udim_tiles', [])
             if t.get('udim_number') in own_tiles]
    if not tiles:
        return None
    print(f"📍 UDIM: using the record of sibling '{carrier.name}' "
          f"for {len(tiles)} tiles")
    subset = dict(mapping)
    subset['udim_tiles'] = tiles
    return subset


def find_sibling_udim_carrier(obj, peek=True):
    """Carrier of the address' SHARED UDIM record for an object that has no
    UDIM material of its own.  Name parsing only — no filesystem access, so
    it is safe to call from poll()/draw()."""
    if not obj or obj.type != 'MESH' or not obj.name.startswith("SM_"):
        return None
    try:
        address, obj_type = process_object_name(obj.name)
    except Exception:
        return None
    folder = get_udim_directory_name(address, obj_type)
    carrier, _mapping = find_udim_record_carrier(folder, exclude=obj, peek=peek)
    return carrier


def reload_tiled_images(obj, new_tiles=None):
    """Reload every TILED image of the object so new tiles show up.
    `new_tiles` are registered in the image's tile collection first —
    reload() re-reads the tiles an image already knows about, it does not
    discover tile files that appeared on disk afterwards."""
    if obj is None:
        return
    for slot in obj.material_slots:
        if not slot.material or not slot.material.use_nodes:
            continue
        for node in slot.material.node_tree.nodes:
            if node.type == 'TEX_IMAGE' and node.image and node.image.source == 'TILED':
                img = node.image
                if new_tiles:
                    try:
                        known = {t.number for t in img.tiles}
                        for number in sorted(new_tiles):
                            if number not in known:
                                img.tiles.new(tile_number=number)
                    except Exception as exc:
                        print(f"  ⚠️ Could not register tiles on {img.name}: {exc}")
                try:
                    img.reload()
                except Exception as exc:
                    print(f"  ⚠️ Error reloading {img.name}: {exc}")


class AGR_OT_CreateUDIM(Operator):
    """Create UDIM texture set from object materials"""
    bl_idname = "agr.create_udim"
    bl_label = "Create UDIM Set"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH' or not obj.name.startswith("SM_"):
            cls.poll_message_set("Выберите MESH-объект с именем SM_*")
            return False
        if not obj.material_slots:
            cls.poll_message_set("У объекта нет материалов")
            return False
        # Can only create UDIM if doesn't already have UDIM textures
        if object_has_udim(obj):
            cls.poll_message_set("У объекта уже есть UDIM-текстуры")
            return False
        return True
    
    def execute(self, context):
        invalidate_udim_cache()
        try:
            obj = context.active_object

            print(f"\n🚀 === CREATING UDIM SET ===")
            print(f"Object: {obj.name}")
            
            # Parse object name
            try:
                address, obj_type = process_object_name(obj.name)
                print(f"Address: {address}, Type: {obj_type}")
            except Exception as e:
                self.report({'ERROR'}, f"Invalid object name: {str(e)}")
                return {'CANCELLED'}
            
            # Scan materials
            texture_sets, skipped = scan_texture_sets_for_udim(context, obj)

            # Refuse BEFORE any mutation (same contract as the atlas
            # operator): a skipped slot used to be unlinked anyway and its
            # faces stayed in tile 1001 wearing another material's texture.
            if skipped:
                agr_report(self, 'ERROR',
                           "UDIM не создан — у этих материалов нет полного сета "
                           "(Diffuse/DiffuseOpacity + ERM + Normal): "
                           + "; ".join(skipped))
                return {'CANCELLED'}

            if not texture_sets:
                self.report({'ERROR'}, "No suitable materials found (need Diffuse, ERM, Normal)")
                return {'CANCELLED'}

            print(f"Found {len(texture_sets)} suitable materials")

            # Get use_main_dir setting
            use_main_dir = context.scene.agr_baker_settings.udim_use_main_directory

            # The tile folder is SHARED by every non-Main type of one
            # address, so it may already hold a sibling's UDIM.  Continue
            # its numbering instead of overwriting tiles 1001+ on disk.
            planned_dir = self.udim_directory_path(address, obj_type, use_main_dir)
            if planned_dir is None:
                self.report({'ERROR'}, "Save blend file first")
                return {'CANCELLED'}
            existing_tiles = scan_udim_tiles_in_dir(planned_dir)
            carrier, carrier_mapping = find_udim_record_carrier(planned_dir, exclude=obj)
            start_udim = (max(existing_tiles) + 1) if existing_tiles else 1001
            if existing_tiles:
                print(f"📍 UDIM folder already holds tiles {sorted(existing_tiles)} — "
                      f"continuing from {start_udim}")

            # Create UDIM directory
            udim_dir = self.create_udim_directory(address, obj_type, use_main_dir)
            if not udim_dir:
                self.report({'ERROR'}, "Failed to create UDIM directory")
                return {'CANCELLED'}

            # Create UDIM material and textures
            udim_material = self.create_udim_material_and_textures(
                context, obj, texture_sets, udim_dir, address, obj_type,
                start_udim=start_udim, carrier=carrier, carrier_mapping=carrier_mapping
            )

            if not udim_material:
                self.report({'ERROR'}, "Failed to create UDIM material")
                return {'CANCELLED'}

            # Move UVs to UDIM tiles. Map original material slot → tile:
            # slots skipped during scanning (no texture set) must not shift
            # the numbering of the remaining tiles.
            slot_to_udim = {
                mat_info['material_index']: start_udim + i
                for i, mat_info in enumerate(texture_sets)
            }
            self.move_uvs_to_udim_tiles(obj, slot_to_udim)

            # Assign UDIM material to object
            obj.data.materials.clear()
            # Slots are empty now, so an orphaned same-named source
            # material can hand over the canonical name (see UDIM-X1)
            claim_canonical_material_name(
                udim_material, get_udim_material_name(address, obj_type))
            obj.data.materials.append(udim_material)

            # Set all polygons to use material 0
            for poly in obj.data.polygons:
                poly.material_index = 0

            # New tiles must appear on the sibling that already shows this
            # folder as well
            new_numbers = [start_udim + i for i in range(len(texture_sets))]
            reload_tiled_images(obj, new_numbers)
            if carrier is not None and carrier is not obj:
                reload_tiled_images(carrier, new_numbers)

            if carrier is not None:
                agr_report(self, 'INFO',
                           f"UDIM дополнен: {udim_material.name}, тайлы "
                           f"{start_udim}–{start_udim + len(texture_sets) - 1} "
                           f"(запись на объекте {carrier.name})")
            else:
                self.report({'INFO'}, f"UDIM set created: {udim_material.name}")
            print(f"✅ UDIM set created successfully!")

            return {'FINISHED'}

        except Exception as e:
            print(f"❌ Error creating UDIM: {str(e)}")
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, f"Error: {str(e)}")
            return {'CANCELLED'}
    
    def udim_directory_path(self, address, obj_type, use_main_dir=False):
        """Where the UDIM folder WOULD be — resolved without creating it,
        so occupancy can be checked before any mkdir/copy."""
        blend_path = bpy.data.filepath
        if not blend_path:
            return None
        base_dir = Path(blend_path).parent
        udim_dir_name = get_udim_directory_name(address, obj_type)
        if use_main_dir:
            return base_dir / udim_dir_name
        return base_dir / _get_output_folder() / udim_dir_name

    def create_udim_directory(self, address, obj_type, use_main_dir=False):
        """Create directory for UDIM textures

        Args:
            address: Object address
            obj_type: Object type (Main, Ground, etc.)
            use_main_dir: If True, create in root directory instead of AGR_BAKE
        """
        blend_path = bpy.data.filepath
        if not blend_path:
            print("❌ Save blend file first")
            return None
        
        base_dir = Path(blend_path).parent
        udim_dir_name = get_udim_directory_name(address, obj_type)
        
        if use_main_dir:
            # Create in root directory
            udim_dir = base_dir / udim_dir_name
            print(f"📁 Creating UDIM folder in root directory")
        else:
            # Create in AGR_BAKE (default)
            agr_bake_dir = base_dir / _get_output_folder()
            if not agr_bake_dir.exists():
                agr_bake_dir.mkdir(parents=True)
            udim_dir = agr_bake_dir / udim_dir_name
            print(f"📁 Creating UDIM folder in AGR_BAKE")
        
        try:
            udim_dir.mkdir(exist_ok=True, parents=True)
            print(f"✅ Created UDIM folder: {udim_dir}")
            return udim_dir
        except Exception as e:
            print(f"❌ Error creating UDIM folder: {e}")
            return None
    
    def create_udim_material_and_textures(self, context, obj, texture_sets, udim_dir,
                                          address, obj_type, start_udim=1001,
                                          carrier=None, carrier_mapping=None):
        """Create UDIM material and textures, recording the tiles.

        When a sibling of the same address already owns this SHARED folder
        (`carrier`), its tiles are extended and its material reused — one
        Ground UDIM per address, no forked record, no `.001` material."""
        print(f"🎨 Creating UDIM material and textures...")

        reuse_material = find_udim_material_for_dir(carrier, udim_dir) if carrier else None

        # Record: extend the carrier's one, or start our own
        if carrier is not None and carrier_mapping is not None:
            material_mapping = carrier_mapping
            record_obj = carrier
            material_mapping.setdefault('udim_tiles', [])
        else:
            material_mapping = {
                'object_name': obj.name,
                'address': address,
                'obj_type': obj_type,
                'udim_tiles': []
            }
            record_obj = obj

        # Create UDIM material
        material_name = get_udim_material_name(address, obj_type)
        if reuse_material is not None:
            udim_material = reuse_material
            print(f"♻️ Reusing the address' UDIM material: {udim_material.name}")
        else:
            udim_material = bpy.data.materials.new(name=material_name)

        # Texture info storage
        texture_info = {
            'Diffuse': {'files': [], 'node': None},
            'ERM': {'files': [], 'node': None},
            'Normal': {'files': [], 'node': None}
        }

        # Nothing may be overwritten: the numbering starts past the tiles
        # already in the folder, so an existing target means the plan and
        # the disk disagree — refuse before the first copy2.
        plan = []
        for i, mat_info in enumerate(texture_sets):
            udim_number = start_udim + i
            for tex_type in ['Diffuse', 'ERM', 'Normal']:
                source_path = mat_info.get(f"{tex_type.lower()}_path")
                if not source_path or not os.path.exists(source_path):
                    continue
                udim_filename = get_udim_texture_name(address, obj_type, tex_type, udim_number)
                target_path = udim_dir / udim_filename
                if target_path.exists():
                    agr_report(self, 'ERROR',
                               f"UDIM не создан: файл тайла уже существует — {udim_filename}")
                    return None
                plan.append((i, udim_number, tex_type, source_path, target_path, udim_filename))

        new_tiles = []
        for i, mat_info in enumerate(texture_sets):
            udim_number = start_udim + i
            print(f"  Processing material {i}: {mat_info['material_name']} -> UDIM {udim_number}")
            new_tiles.append({
                'udim_number': udim_number,
                'material_index': mat_info['material_index'],
                'material_name': mat_info['material_name'],
                'set_name': f"S_{mat_info['material_name']}"
            })

        for _i, udim_number, tex_type, source_path, target_path, udim_filename in plan:
            try:
                shutil.copy2(source_path, target_path)
                texture_info[tex_type]['files'].append(str(target_path))
                print(f"    {tex_type}: {os.path.basename(source_path)} -> {udim_filename}")
            except Exception as e:
                print(f"    ❌ Error copying {tex_type}: {e}")

        material_mapping['udim_tiles'].extend(new_tiles)

        # Save the mapping on its carrier (idprop + color mirror)
        save_udim_mapping(record_obj, udim_dir, material_mapping)

        if reuse_material is not None:
            # The reused material already points at this tiled set; a
            # reload picks the new tiles up (done by the caller).
            print(f"✅ UDIM tiles appended to material: {udim_material.name}")
            return udim_material

        nodes, links, bsdf = setup_udim_material_nodes(udim_material)

        # Create texture nodes
        for tex_type, info in texture_info.items():
            if info['files']:
                tex_node = nodes.new(type='ShaderNodeTexImage')
                tex_node.label = f'UDIM {tex_type}'
                
                if tex_type == 'Diffuse':
                    tex_node.location = (-600, 200)
                elif tex_type == 'Normal':
                    tex_node.location = (-600, -100)
                elif tex_type == 'ERM':
                    tex_node.location = (-600, -400)
                
                # Load first texture and set as TILED
                first_texture = info['files'][0]
                img = bpy.data.images.load(first_texture)
                img.source = 'TILED'
                tex_node.image = img
                info['node'] = tex_node
                
                # Set colorspace
                if tex_type in ['ERM', 'Normal']:
                    img.colorspace_settings.name = 'Non-Color'
                else:
                    img.colorspace_settings.name = 'sRGB'
        
        # Connect nodes
        if texture_info['Diffuse']['node']:
            links.new(texture_info['Diffuse']['node'].outputs['Color'], bsdf.inputs['Base Color'])
            links.new(texture_info['Diffuse']['node'].outputs['Alpha'], bsdf.inputs['Alpha'])
            links.new(texture_info['Diffuse']['node'].outputs['Color'], bsdf.inputs['Emission Color'])
        
        if texture_info['ERM']['node']:
            separate_color = nodes.new(type='ShaderNodeSeparateColor')
            separate_color.location = (-300, -400)
            links.new(texture_info['ERM']['node'].outputs['Color'], separate_color.inputs['Color'])
            links.new(separate_color.outputs['Red'], bsdf.inputs['Emission Strength'])
            links.new(separate_color.outputs['Green'], bsdf.inputs['Roughness'])
            links.new(separate_color.outputs['Blue'], bsdf.inputs['Metallic'])
        
        if texture_info['Normal']['node']:
            normal_map = nodes.new(type='ShaderNodeNormalMap')
            normal_map.location = (-300, -100)
            links.new(texture_info['Normal']['node'].outputs['Color'], normal_map.inputs['Color'])
            links.new(normal_map.outputs['Normal'], bsdf.inputs['Normal'])
        
        print(f"✅ UDIM material created: {material_name}")
        return udim_material
    
    def move_uvs_to_udim_tiles(self, obj, slot_to_udim):
        """Move UV coordinates to UDIM tiles using a material-slot → tile map"""
        if not obj.data.uv_layers:
            print("⚠️ No UV layers found")
            return
        
        import bmesh
        
        bm = bmesh.new()
        bm.from_mesh(obj.data)

        if not bm.loops.layers.uv:
            print("⚠️ No UV layer in bmesh")
            bm.free()
            return

        uv_layer = bm.loops.layers.uv.active

        print(f"📐 Moving UVs to UDIM tiles...")

        # Count polygons per material
        material_counts = {}

        try:
            for face in bm.faces:
                udim_number = slot_to_udim.get(face.material_index)
                if udim_number is None:
                    continue

                # Calculate UDIM offset
                udim_offset = udim_number - 1001
                udim_offset_u = udim_offset % 10
                udim_offset_v = udim_offset // 10

                # Move UVs
                for loop in face.loops:
                    uv = loop[uv_layer].uv
                    uv.x += udim_offset_u
                    uv.y += udim_offset_v

                material_counts[udim_number] = material_counts.get(udim_number, 0) + 1

            # Update mesh
            bm.to_mesh(obj.data)
            obj.data.update()
        finally:
            bm.free()

        # Print statistics
        for udim_number, count in sorted(material_counts.items()):
            print(f"  UDIM {udim_number}: {count} polygons moved")

        print(f"✅ UV coordinates moved to UDIM tiles")


class AGR_OT_AddToUDIM(Operator):
    """Add selected texture sets to existing UDIM"""
    bl_idname = "agr.add_to_udim"
    bl_label = "Add Sets to UDIM"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH' or not obj.name.startswith("SM_"):
            cls.poll_message_set("Выберите MESH-объект с именем SM_*")
            return False
        # Either the object itself carries the UDIM, or a sibling of the
        # same address owns the SHARED tile folder — adding sets to that
        # folder is exactly what this operator does.
        if not object_has_udim(obj) and find_sibling_udim_carrier(obj) is None:
            cls.poll_message_set("У объекта нет UDIM-текстур")
            return False
        return True
    
    def execute(self, context):
        invalidate_udim_cache()
        try:
            obj = context.active_object

            print(f"\n➕ === ADDING SETS TO UDIM ===")
            print(f"Object: {obj.name}")
            
            # Parse object name
            try:
                address, obj_type = process_object_name(obj.name)
                print(f"Address: {address}, Type: {obj_type}")
            except Exception as e:
                self.report({'ERROR'}, f"Invalid object name: {str(e)}")
                return {'CANCELLED'}
            
            # Find UDIM directory
            blend_path = bpy.data.filepath
            if not blend_path:
                self.report({'ERROR'}, "Save blend file first")
                return {'CANCELLED'}
            
            base_dir = Path(blend_path).parent
            use_main_dir = context.scene.agr_baker_settings.udim_use_main_directory
            udim_dir = find_udim_directory(address, obj_type, base_dir, use_main_dir)
            
            if not udim_dir:
                self.report({'ERROR'}, f"UDIM directory not found for {address}")
                return {'CANCELLED'}
            
            # Load existing mapping (object record, legacy JSON fallback)
            mapping = load_udim_mapping(obj, udim_dir)
            record_carrier = obj

            if not mapping:
                # per-object records + a SHARED Ground folder: the record
                # may live on a sibling object of the same address - update
                # that one instead of forking a diverging copy
                carrier, mapping = find_udim_record_carrier(udim_dir, exclude=obj)
                if carrier is not None:
                    record_carrier = carrier
                    print(f"📍 UDIM mapping found on sibling object '{carrier.name}'")

            if not mapping:
                self.report({'WARNING'}, "No UDIM mapping found - sets will be added without recording")
                print("⚠️ No UDIM mapping found - sets will be added without recording")
            
            # Get existing UDIM tiles
            existing_tiles = self.scan_existing_udim_tiles(udim_dir, mapping)
            max_udim = max(existing_tiles) if existing_tiles else 1000
            
            print(f"Existing UDIM tiles: {sorted(existing_tiles)}")
            print(f"Next available UDIM: {max_udim + 1}")
            
            # Get selected texture sets from the list
            selected_sets = [ts for ts in context.scene.agr_texture_sets if ts.is_selected]
            
            if not selected_sets:
                self.report({'ERROR'}, "No texture sets selected")
                return {'CANCELLED'}
            
            print(f"Selected texture sets: {len(selected_sets)}")
            
            # Convert selected sets to texture info format
            texture_sets = self.prepare_texture_sets(selected_sets, base_dir)
            
            if not texture_sets:
                self.report({'ERROR'}, "No suitable texture sets found (need Diffuse, ERM, Normal)")
                return {'CANCELLED'}
            
            # Filter out sets that are already in UDIM
            existing_set_names = set()
            if mapping:
                for tile in mapping.get('udim_tiles', []):
                    existing_set_names.add(tile.get('set_name', ''))
            
            new_sets = []
            for tex_set in texture_sets:
                set_name = f"S_{tex_set['material_name']}"
                if set_name not in existing_set_names:
                    new_sets.append(tex_set)
                else:
                    print(f"  ⚠️ Skipping {set_name} - already in UDIM")
            
            if not new_sets:
                self.report({'INFO'}, "All selected texture sets are already in UDIM")
                return {'CANCELLED'}
            
            print(f"Found {len(new_sets)} new texture sets to add")
            
            # Add new sets to UDIM (the record goes to its actual carrier)
            added_count, failed = self.add_sets_to_udim(
                record_carrier, new_sets, udim_dir, address, obj_type,
                max_udim + 1, mapping
            )

            if added_count == 0:
                msg = "Не удалось добавить сеты в UDIM"
                if failed:
                    msg += ": " + ", ".join(failed)
                agr_report(self, 'ERROR', msg)
                return {'CANCELLED'}

            # Reload UDIM images
            self.reload_udim_images(obj)

            if failed:
                agr_report(self, 'WARNING',
                           f"Добавлено сетов в UDIM: {added_count}; не скопированы: "
                           + ", ".join(failed))
            else:
                self.report({'INFO'}, f"Added {added_count} texture sets to UDIM")
            print(f"✅ Successfully added {added_count} sets to UDIM")

            return {'FINISHED'}
            
        except Exception as e:
            print(f"❌ Error adding to UDIM: {str(e)}")
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, f"Error: {str(e)}")
            return {'CANCELLED'}
    
    def scan_existing_udim_tiles(self, udim_dir, mapping):
        """Scan for existing UDIM tile numbers"""
        tiles = set()
        
        # First, check JSON mapping
        if mapping:
            for tile in mapping.get('udim_tiles', []):
                tiles.add(tile['udim_number'])
        
        # Also scan directory for actual files
        for filename in os.listdir(udim_dir):
            if not filename.lower().endswith('.png'):
                continue
            
            match = re.search(r'\.(\d{4})\.png$', filename)
            if match:
                udim_number = int(match.group(1))
                if 1001 <= udim_number <= 1999:
                    tiles.add(udim_number)
        
        return tiles
    
    def prepare_texture_sets(self, selected_sets, base_dir):
        """Convert selected texture sets to format needed for UDIM creation"""
        texture_sets = []
        agr_bake_dir = base_dir / _get_output_folder()
        
        if not agr_bake_dir.exists():
            print(f"⚠️ AGR_BAKE folder not found: {agr_bake_dir}")
            return texture_sets
        
        for idx, tex_set in enumerate(selected_sets):
            # Skip atlas sets
            if tex_set.is_atlas:
                print(f"  ⚠️ Skipping atlas set: {tex_set.name}")
                continue
            
            set_name = tex_set.name
            set_folder = agr_bake_dir / set_name
            
            if not set_folder.exists():
                print(f"  ⚠️ Set folder not found: {set_name}")
                continue
            
            # Extract material name from set name (remove S_ prefix)
            if set_name.startswith("S_"):
                material_name = set_name[2:]
            else:
                material_name = set_name
            
            # Check for required textures
            diffuse_opacity_path = set_folder / f"T_{material_name}_DiffuseOpacity.png"
            diffuse_path = set_folder / f"T_{material_name}_Diffuse.png"
            erm_path = set_folder / f"T_{material_name}_ERM.png"
            normal_path = set_folder / f"T_{material_name}_Normal.png"
            
            # Use DiffuseOpacity if exists, otherwise Diffuse
            if diffuse_opacity_path.exists():
                final_diffuse_path = str(diffuse_opacity_path)
            elif diffuse_path.exists():
                final_diffuse_path = str(diffuse_path)
            else:
                final_diffuse_path = None
            
            # Check if we have all required textures
            has_diffuse = final_diffuse_path is not None
            has_erm = erm_path.exists()
            has_normal = normal_path.exists()
            
            if has_diffuse and has_erm and has_normal:
                texture_sets.append({
                    'material_index': idx,
                    'material_name': material_name,
                    'diffuse_path': final_diffuse_path,
                    'erm_path': str(erm_path),
                    'normal_path': str(normal_path)
                })
                print(f"  ✅ {set_name}: Found complete texture set")
            else:
                missing = []
                if not has_diffuse:
                    missing.append("Diffuse/DiffuseOpacity")
                if not has_erm:
                    missing.append("ERM")
                if not has_normal:
                    missing.append("Normal")
                print(f"  ⚠️ {set_name}: Missing textures: {', '.join(missing)}")
        
        return texture_sets

    
    def add_sets_to_udim(self, obj, texture_sets, udim_dir, address, obj_type, start_udim, mapping):
        """Add texture sets to UDIM folder and update the object record.
        Returns (added_count, failed_names)."""
        added_count = 0
        new_tiles = []
        failed = []

        for i, mat_info in enumerate(texture_sets):
            udim_number = start_udim + i
            print(f"  Adding material {mat_info['material_name']} -> UDIM {udim_number}")

            # Prepare tile info for the record — appended ONLY after the
            # files actually landed: a phantom tile survives the operator's
            # own CANCELLED (the record is written outside the undo step)
            # and later makes revert build a material for missing files.
            tile_info = {
                'udim_number': udim_number,
                'material_index': mat_info['material_index'],
                'material_name': mat_info['material_name'],
                'set_name': f"S_{mat_info['material_name']}"
            }

            # Copy each texture type
            success = True
            for tex_type in ['Diffuse', 'ERM', 'Normal']:
                source_path = mat_info.get(f"{tex_type.lower()}_path")
                
                if source_path and os.path.exists(source_path):
                    udim_filename = get_udim_texture_name(address, obj_type, tex_type, udim_number)
                    target_path = udim_dir / udim_filename

                    try:
                        shutil.copy2(source_path, target_path)
                        print(f"    {tex_type}: {os.path.basename(source_path)} -> {udim_filename}")
                    except Exception as e:
                        print(f"    ❌ Error copying {tex_type}: {e}")
                        success = False
                else:
                    print(f"    ⚠️ Missing {tex_type} texture for UDIM {udim_number}")
                    success = False
            
            if success:
                added_count += 1
                new_tiles.append(tile_info)
            else:
                failed.append(mat_info['material_name'])

        # Update the object record if a mapping exists
        if mapping and new_tiles:
            mapping['udim_tiles'].extend(new_tiles)
            save_udim_mapping(obj, udim_dir, mapping)
            print(f"✅ Updated UDIM mapping with {len(new_tiles)} new tiles")
        elif new_tiles:
            print(f"⚠️ No UDIM mapping to update (added {len(new_tiles)} tiles unrecorded)")

        return added_count, failed
    
    def reload_udim_images(self, obj):
        """Reload UDIM images to show new tiles"""
        print("🔄 Reloading UDIM images...")
        
        for slot in obj.material_slots:
            if not slot.material or not slot.material.use_nodes:
                continue
            
            for node in slot.material.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    if node.image.source == 'TILED':
                        try:
                            node.image.reload()
                            print(f"  Reloaded: {node.image.name}")
                        except Exception as e:
                            print(f"  ⚠️ Error reloading {node.image.name}: {e}")
        
        print("✅ UDIM images reloaded")


class AGR_OT_RevertUDIM(Operator):
    """Revert UDIM UVs back to 0-1 and restore original materials"""
    bl_idname = "agr.revert_udim"
    bl_label = "Revert UDIM (Disassemble)"
    bl_options = {'REGISTER', 'UNDO'}
    
    # Property to store warning message
    warning_message: StringProperty(default="")
    
    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            cls.poll_message_set("Нужен активный MESH-объект")
            return False
        if not object_has_udim(obj):
            cls.poll_message_set("У объекта нет UDIM-текстур")
            return False
        return True
    
    def invoke(self, context, event):
        """Check for JSON and show warning if needed"""
        try:
            obj = context.active_object
            
            # Parse object name
            try:
                address, obj_type = process_object_name(obj.name)
            except Exception as e:
                self.report({'ERROR'}, f"Invalid object name: {str(e)}")
                return {'CANCELLED'}
            
            # Find UDIM directory
            blend_path = bpy.data.filepath
            if not blend_path:
                self.report({'ERROR'}, "Save blend file first")
                return {'CANCELLED'}
            
            base_dir = Path(blend_path).parent
            use_main_dir = context.scene.agr_baker_settings.udim_use_main_directory
            udim_dir = find_udim_directory(address, obj_type, base_dir, use_main_dir)
            
            if not udim_dir:
                self.report({'ERROR'}, f"UDIM directory not found for {address}")
                return {'CANCELLED'}
            
            # Scan for actual UDIM tiles
            actual_tiles = self.scan_udim_tiles(udim_dir)
            
            if not actual_tiles:
                self.report({'ERROR'}, "No UDIM tiles found in directory")
                return {'CANCELLED'}
            
            # Load mapping (object record, legacy JSON fallback, then the
            # sibling that owns this address' SHARED folder)
            mapping = load_udim_mapping(obj, udim_dir)
            if not mapping:
                mapping = borrow_carrier_mapping(obj, udim_dir)

            # The folder is SHARED by every non-Main type of one address:
            # tiles owned by a sibling are none of this object's business and
            # must not be counted as "missing from the mapping" — otherwise
            # the dialog scares the user and the hybrid path builds generic
            # M_#_#### materials for the sibling's tiles.
            actual_tiles, _skip, mapping, _own, _others = revert_tile_scope(
                obj, udim_dir, actual_tiles, mapping)
            if not actual_tiles:
                self.report({'ERROR'}, "Все тайлы папки принадлежат другим "
                                       "объектам этого адреса")
                return {'CANCELLED'}

            # Check if we need to show warning
            show_warning = False
            warning_lines = []
            
            if not mapping:
                show_warning = True
                warning_lines.append("⚠️ JSON mapping file not found!")
                warning_lines.append("")
                warning_lines.append("UDIM will be disassembled to generic materials:")
                warning_lines.append("M_#_1001, M_#_1002, etc.")
                warning_lines.append("")
                warning_lines.append(f"Found {len(actual_tiles)} UDIM tiles")
            else:
                # Check if JSON covers all tiles
                json_tiles = set(tile['udim_number'] for tile in mapping.get('udim_tiles', []))
                missing_tiles = actual_tiles - json_tiles
                
                if missing_tiles:
                    show_warning = True
                    warning_lines.append("⚠️ JSON mapping incomplete!")
                    warning_lines.append("")
                    warning_lines.append(f"JSON has info for {len(json_tiles)} tiles")
                    warning_lines.append(f"But found {len(actual_tiles)} tiles in folder")
                    warning_lines.append("")
                    warning_lines.append(f"Missing tiles: {sorted(missing_tiles)}")
                    warning_lines.append("")
                    warning_lines.append("Missing tiles will use generic materials M_#_####")
            
            if show_warning:
                self.warning_message = "\n".join(warning_lines)
                return context.window_manager.invoke_props_dialog(self, width=400)
            else:
                # No warning needed, proceed directly
                return self.execute(context)
                
        except Exception as e:
            print(f"❌ Error in invoke: {str(e)}")
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, f"Error: {str(e)}")
            return {'CANCELLED'}
    
    def draw(self, context):
        """Draw warning dialog"""
        layout = self.layout
        
        # Split message by lines and draw each
        for line in self.warning_message.split('\n'):
            if line.strip():
                layout.label(text=line)
            else:
                layout.separator()
        
        layout.separator()
        layout.label(text="Continue with disassembly?")
    
    def scan_udim_tiles(self, udim_dir):
        """Scan directory for UDIM tile numbers"""
        return scan_udim_tiles_in_dir(udim_dir)
    
    def execute(self, context):
        invalidate_udim_cache()
        try:
            obj = context.active_object

            print(f"\n🔄 === REVERTING UDIM ===")
            print(f"Object: {obj.name}")
            
            # Store old UDIM material for cleanup
            old_udim_material = None
            for slot in obj.material_slots:
                if slot.material and slot.material.use_nodes:
                    for node in slot.material.node_tree.nodes:
                        if node.type == 'TEX_IMAGE' and node.image:
                            if node.image.source == 'TILED':
                                old_udim_material = slot.material
                                break
                if old_udim_material:
                    break
            
            # Parse object name to get address
            try:
                address, obj_type = process_object_name(obj.name)
            except Exception as e:
                self.report({'ERROR'}, f"Invalid object name: {str(e)}")
                return {'CANCELLED'}
            
            # Find UDIM directory
            blend_path = bpy.data.filepath
            if not blend_path:
                self.report({'ERROR'}, "Save blend file first")
                return {'CANCELLED'}
            
            base_dir = Path(blend_path).parent
            use_main_dir = context.scene.agr_baker_settings.udim_use_main_directory
            udim_dir = find_udim_directory(address, obj_type, base_dir, use_main_dir)
            
            if not udim_dir:
                self.report({'ERROR'}, f"UDIM directory not found for {address}")
                return {'CANCELLED'}
            
            # Scan for actual UDIM tiles
            actual_tiles = self.scan_udim_tiles(udim_dir)
            
            if not actual_tiles:
                self.report({'ERROR'}, "No UDIM tiles found")
                return {'CANCELLED'}
            
            # Load mapping (object record, legacy JSON fallback, then the
            # sibling that owns this address' SHARED folder)
            mapping = load_udim_mapping(obj, udim_dir)
            if not mapping:
                mapping = borrow_carrier_mapping(obj, udim_dir)

            # Shared-folder guard, same as in invoke(): only the tiles this
            # object actually occupies are disassembled.  full_mapping keeps
            # the sibling entries so the record can be handed over below.
            full_mapping = mapping
            actual_tiles, skip_tiles, mapping, own_tiles, others = revert_tile_scope(
                obj, udim_dir, actual_tiles, mapping)
            if not actual_tiles:
                self.report({'ERROR'}, "Все тайлы папки принадлежат другим "
                                       "объектам этого адреса")
                return {'CANCELLED'}
            if skip_tiles:
                print(f"📍 UDIM revert: tiles left to the siblings of the shared "
                      f"folder: {sorted(skip_tiles)}")

            result = {'CANCELLED'}

            if not mapping:
                # No JSON - use fallback for all tiles
                print("⚠️ No JSON mapping found, using fallback method for all tiles")
                result = self.revert_without_json(obj, udim_dir, actual_tiles)
            else:
                # Check if JSON covers all tiles
                json_tiles = {tile['udim_number']: tile for tile in mapping.get('udim_tiles', [])}
                json_tile_numbers = set(json_tiles.keys())
                missing_tiles = actual_tiles - json_tile_numbers
                
                if missing_tiles:
                    # Partial JSON - use JSON for covered tiles, fallback for missing
                    print(f"⚠️ JSON incomplete: {len(json_tile_numbers)} tiles in JSON, {len(missing_tiles)} missing")
                    result = self.revert_with_partial_json(obj, mapping, udim_dir, actual_tiles, missing_tiles)
                else:
                    # Complete JSON - use it for all tiles
                    print(f"✅ Complete JSON mapping found for all {len(actual_tiles)} tiles")
                    result = self.revert_with_json(obj, mapping, udim_dir)
            
            # Clean up old UDIM material if revert was successful
            if result == {'FINISHED'}:
                if old_udim_material:
                    self.cleanup_udim_material(old_udim_material)
                self._hand_over_record(obj, udim_dir, full_mapping,
                                       own_tiles, skip_tiles, others)

            return result
            
        except Exception as e:
            print(f"❌ Error reverting UDIM: {str(e)}")
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, f"Error: {str(e)}")
            return {'CANCELLED'}
    
    def _hand_over_record(self, obj, udim_dir, full_mapping, own_tiles,
                          skip_tiles, others):
        """Keep the SHARED record alive after one sibling reverted.

        The tile folder belongs to the address, not to the object: wiping the
        carrier's record outright left every other Ground* object of the
        address with no memory at all (its revert then produced generic
        M_#_#### materials).  Two cases:
          * this object owned the record — strip it, but move the entries of
            the tiles that are still in use onto an object that still has the
            UDIM material;
          * the record was borrowed from a sibling — trim only this object's
            tiles out of the carrier's record.
        """
        live_others = [o for o in others if o.name in bpy.data.objects]
        try:
            if read_udim_record(obj) is not None:
                rest = [t for t in (full_mapping or {}).get('udim_tiles', [])
                        if t.get('udim_number') in skip_tiles]
                strip_udim_record(obj)
                if not rest:
                    return
                heir = live_others[0] if live_others else None
                if heir is None:
                    agr_report(self, 'WARNING',
                               "Запись UDIM удалена, но тайлы соседних объектов "
                               "остались без памяти — некому передать запись")
                    return
                heir_map = read_udim_record(heir)
                if heir_map:
                    known = {t.get('udim_number') for t in heir_map.get('udim_tiles', [])}
                    heir_map = dict(heir_map)
                    heir_map['udim_tiles'] = list(heir_map.get('udim_tiles', [])) + [
                        t for t in rest if t.get('udim_number') not in known]
                else:
                    heir_map = dict(full_mapping)
                    heir_map['udim_tiles'] = rest
                    heir_map['object_name'] = heir.name
                save_udim_mapping(heir, udim_dir, heir_map)
                print(f"📍 UDIM: record of {len(rest)} shared tiles handed over "
                      f"to '{heir.name}'")
                return

            carrier, cmap = find_udim_record_carrier(udim_dir, exclude=obj)
            if carrier is None or not cmap:
                return
            tiles = cmap.get('udim_tiles', [])
            kept = [t for t in tiles if t.get('udim_number') not in own_tiles]
            if len(kept) == len(tiles):
                return
            cmap = dict(cmap)
            cmap['udim_tiles'] = kept
            save_udim_mapping(carrier, udim_dir, cmap)
            print(f"📍 UDIM: {len(tiles) - len(kept)} reverted tiles dropped "
                  f"from the record of '{carrier.name}'")
        except Exception as exc:
            print(f"⚠️ UDIM: record hand-over failed: {exc}")

    def revert_with_partial_json(self, obj, mapping, udim_dir, actual_tiles, missing_tiles):
        """Revert UDIM with partial JSON mapping - use JSON for covered tiles, fallback for missing"""
        print(f"🔀 Using hybrid method: JSON for {len(actual_tiles) - len(missing_tiles)} tiles, fallback for {len(missing_tiles)} tiles")
        
        # Create materials from JSON mapping
        udim_to_material = {}
        json_tiles = {tile['udim_number']: tile for tile in mapping.get('udim_tiles', [])}
        
        # Process tiles covered by JSON
        for udim_number, tile_info in json_tiles.items():
            if udim_number not in actual_tiles:
                continue  # Skip if tile doesn't exist in folder
            
            set_name = tile_info['set_name']
            material_name = tile_info['material_name']
            
            print(f"  Creating material from JSON for UDIM {udim_number}: {material_name}")
            
            # Create or get material
            mat = bpy.data.materials.get(material_name)
            if not mat:
                mat = bpy.data.materials.new(name=material_name)
            
            # Setup material nodes
            mat.use_nodes = True
            nodes = mat.node_tree.nodes
            links = mat.node_tree.links
            nodes.clear()
            
            output = nodes.new(type='ShaderNodeOutputMaterial')
            bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
            output.location = (400, 0)
            bsdf.location = (100, 0)
            links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
            
            # Load textures from set folder - always in AGR_BAKE
            blend_path = bpy.data.filepath
            base_dir = Path(blend_path).parent
            agr_bake_dir = base_dir / _get_output_folder()
            set_folder = agr_bake_dir / set_name
            
            if set_folder.exists():
                self.load_textures_to_material(mat, set_folder, material_name, nodes, links, bsdf)
            else:
                print(f"  ⚠️ Set folder not found: {set_folder}")
            
            udim_to_material[udim_number] = mat
        
        # Process missing tiles with fallback method
        sequence_num = self.get_next_sequence_number(bpy.context)
        
        for udim_number in sorted(missing_tiles):
            mat_name = f"M_{sequence_num}_{udim_number}"
            mat = bpy.data.materials.get(mat_name)
            if not mat:
                mat = bpy.data.materials.new(name=mat_name)
            
            print(f"  Creating generic material for UDIM {udim_number}: {mat_name}")
            
            # Setup material
            mat.use_nodes = True
            nodes = mat.node_tree.nodes
            links = mat.node_tree.links
            nodes.clear()
            
            output = nodes.new(type='ShaderNodeOutputMaterial')
            bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
            output.location = (400, 0)
            bsdf.location = (100, 0)
            links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
            
            # Find and load textures for this tile
            tile_textures = self.find_tile_textures(udim_dir, udim_number)
            
            if 'Diffuse' in tile_textures:
                img = bpy.data.images.load(tile_textures['Diffuse'])
                img.colorspace_settings.name = 'sRGB'
                tex_node = nodes.new(type='ShaderNodeTexImage')
                tex_node.image = img
                tex_node.location = (-300, 200)
                links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])
                links.new(tex_node.outputs['Alpha'], bsdf.inputs['Alpha'])
            
            if 'ERM' in tile_textures:
                img = bpy.data.images.load(tile_textures['ERM'])
                img.colorspace_settings.name = 'Non-Color'
                tex_node = nodes.new(type='ShaderNodeTexImage')
                tex_node.image = img
                tex_node.location = (-300, -100)
                
                separate = nodes.new(type='ShaderNodeSeparateColor')
                separate.location = (0, -100)
                links.new(tex_node.outputs['Color'], separate.inputs['Color'])
                links.new(separate.outputs['Red'], bsdf.inputs['Emission Strength'])
                links.new(separate.outputs['Green'], bsdf.inputs['Roughness'])
                links.new(separate.outputs['Blue'], bsdf.inputs['Metallic'])
            
            if 'Normal' in tile_textures:
                img = bpy.data.images.load(tile_textures['Normal'])
                img.colorspace_settings.name = 'Non-Color'
                tex_node = nodes.new(type='ShaderNodeTexImage')
                tex_node.image = img
                tex_node.location = (-300, -400)
                
                normal_map = nodes.new(type='ShaderNodeNormalMap')
                normal_map.location = (0, -400)
                links.new(tex_node.outputs['Color'], normal_map.inputs['Color'])
                links.new(normal_map.outputs['Normal'], bsdf.inputs['Normal'])
            
            udim_to_material[udim_number] = mat
        
        # Move UVs back and assign materials
        self.move_uvs_back_and_assign_materials(obj, udim_to_material)
        
        json_count = len(actual_tiles) - len(missing_tiles)
        self.report({'INFO'}, f"UDIM reverted: {json_count} from JSON, {len(missing_tiles)} generic materials")
        return {'FINISHED'}
    
    def find_tile_textures(self, udim_dir, udim_number):
        """Find textures for a specific UDIM tile"""
        return find_tile_textures_in_dir(udim_dir, udim_number)
    
    def revert_with_json(self, obj, mapping, udim_dir):
        """Revert UDIM using JSON mapping to restore original materials"""
        print(f"✅ Using JSON mapping for revert")
        
        # Create materials from mapping
        udim_to_material = {}
        
        udim_tiles = mapping.get('udim_tiles', [])
        if not udim_tiles:
            print("⚠️ JSON mapping has no 'udim_tiles' key or it is empty")
            return {'CANCELLED'}
        for tile_info in udim_tiles:
            udim_number = tile_info['udim_number']
            set_name = tile_info['set_name']
            material_name = tile_info['material_name']
            
            print(f"  Creating material for UDIM {udim_number}: {set_name}")
            
            # Create or get material
            mat = bpy.data.materials.get(material_name)
            if not mat:
                mat = bpy.data.materials.new(name=material_name)
            
            # Setup material nodes
            mat.use_nodes = True
            nodes = mat.node_tree.nodes
            links = mat.node_tree.links
            nodes.clear()
            
            output = nodes.new(type='ShaderNodeOutputMaterial')
            bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
            output.location = (400, 0)
            bsdf.location = (100, 0)
            links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
            
            # Load textures from set folder - always in AGR_BAKE
            blend_path = bpy.data.filepath
            base_dir = Path(blend_path).parent
            agr_bake_dir = base_dir / _get_output_folder()
            set_folder = agr_bake_dir / set_name
            
            if set_folder.exists():
                self.load_textures_to_material(mat, set_folder, material_name, nodes, links, bsdf)
            else:
                print(f"  ⚠️ Set folder not found: {set_folder}")
            
            udim_to_material[udim_number] = mat
        
        # Move UVs back to 0-1 and assign materials
        self.move_uvs_back_and_assign_materials(obj, udim_to_material)
        
        self.report({'INFO'}, f"UDIM reverted using JSON mapping: {len(udim_to_material)} materials restored")
        return {'FINISHED'}
    
    def revert_without_json(self, obj, udim_dir, actual_tiles):
        """Revert UDIM without JSON mapping (fallback)"""
        print(f"⚠️ Using fallback method without JSON for {len(actual_tiles)} tiles")
        
        # Create generic materials M_#_####
        udim_to_material = {}
        sequence_num = self.get_next_sequence_number(bpy.context)
        
        for udim_number in sorted(actual_tiles):
            mat_name = f"M_{sequence_num}_{udim_number}"
            mat = bpy.data.materials.get(mat_name)
            if not mat:
                mat = bpy.data.materials.new(name=mat_name)
            
            print(f"  Creating generic material: {mat_name}")
            
            # Setup material
            mat.use_nodes = True
            nodes = mat.node_tree.nodes
            links = mat.node_tree.links
            nodes.clear()
            
            output = nodes.new(type='ShaderNodeOutputMaterial')
            bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
            output.location = (400, 0)
            bsdf.location = (100, 0)
            links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
            
            # Find and load textures for this tile
            tile_textures = self.find_tile_textures(udim_dir, udim_number)
            
            if 'Diffuse' in tile_textures:
                img = bpy.data.images.load(tile_textures['Diffuse'])
                img.colorspace_settings.name = 'sRGB'
                tex_node = nodes.new(type='ShaderNodeTexImage')
                tex_node.image = img
                tex_node.location = (-300, 200)
                links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])
                links.new(tex_node.outputs['Alpha'], bsdf.inputs['Alpha'])
            
            if 'ERM' in tile_textures:
                img = bpy.data.images.load(tile_textures['ERM'])
                img.colorspace_settings.name = 'Non-Color'
                tex_node = nodes.new(type='ShaderNodeTexImage')
                tex_node.image = img
                tex_node.location = (-300, -100)
                
                separate = nodes.new(type='ShaderNodeSeparateColor')
                separate.location = (0, -100)
                links.new(tex_node.outputs['Color'], separate.inputs['Color'])
                links.new(separate.outputs['Red'], bsdf.inputs['Emission Strength'])
                links.new(separate.outputs['Green'], bsdf.inputs['Roughness'])
                links.new(separate.outputs['Blue'], bsdf.inputs['Metallic'])
            
            if 'Normal' in tile_textures:
                img = bpy.data.images.load(tile_textures['Normal'])
                img.colorspace_settings.name = 'Non-Color'
                tex_node = nodes.new(type='ShaderNodeTexImage')
                tex_node.image = img
                tex_node.location = (-300, -400)
                
                normal_map = nodes.new(type='ShaderNodeNormalMap')
                normal_map.location = (0, -400)
                links.new(tex_node.outputs['Color'], normal_map.inputs['Color'])
                links.new(normal_map.outputs['Normal'], bsdf.inputs['Normal'])
            
            udim_to_material[udim_number] = mat
        
        # Move UVs back and assign materials
        self.move_uvs_back_and_assign_materials(obj, udim_to_material)
        
        self.report({'INFO'}, f"UDIM reverted (fallback): {len(udim_to_material)} generic materials created")
        return {'FINISHED'}
    
    def load_textures_to_material(self, mat, set_folder, material_name, nodes, links, bsdf):
        """Load textures from set folder to material"""
        # Check for DiffuseOpacity or Diffuse
        diffuse_opacity_path = set_folder / f"T_{material_name}_DiffuseOpacity.png"
        diffuse_path = set_folder / f"T_{material_name}_Diffuse.png"
        erm_path = set_folder / f"T_{material_name}_ERM.png"
        normal_path = set_folder / f"T_{material_name}_Normal.png"
        
        # Load Diffuse/DiffuseOpacity
        if diffuse_opacity_path.exists():
            img = bpy.data.images.load(str(diffuse_opacity_path))
            img.colorspace_settings.name = 'sRGB'
            tex_node = nodes.new(type='ShaderNodeTexImage')
            tex_node.image = img
            tex_node.location = (-300, 200)
            links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])
            links.new(tex_node.outputs['Alpha'], bsdf.inputs['Alpha'])
            links.new(tex_node.outputs['Color'], bsdf.inputs['Emission Color'])
        elif diffuse_path.exists():
            img = bpy.data.images.load(str(diffuse_path))
            img.colorspace_settings.name = 'sRGB'
            tex_node = nodes.new(type='ShaderNodeTexImage')
            tex_node.image = img
            tex_node.location = (-300, 200)
            links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])
        
        # Load ERM
        if erm_path.exists():
            img = bpy.data.images.load(str(erm_path))
            img.colorspace_settings.name = 'Non-Color'
            tex_node = nodes.new(type='ShaderNodeTexImage')
            tex_node.image = img
            tex_node.location = (-300, -100)
            
            separate = nodes.new(type='ShaderNodeSeparateColor')
            separate.location = (0, -100)
            links.new(tex_node.outputs['Color'], separate.inputs['Color'])
            links.new(separate.outputs['Red'], bsdf.inputs['Emission Strength'])
            links.new(separate.outputs['Green'], bsdf.inputs['Roughness'])
            links.new(separate.outputs['Blue'], bsdf.inputs['Metallic'])
        
        # Load Normal
        if normal_path.exists():
            img = bpy.data.images.load(str(normal_path))
            img.colorspace_settings.name = 'Non-Color'
            tex_node = nodes.new(type='ShaderNodeTexImage')
            tex_node.image = img
            tex_node.location = (-300, -400)
            
            normal_map = nodes.new(type='ShaderNodeNormalMap')
            normal_map.location = (0, -400)
            links.new(tex_node.outputs['Color'], normal_map.inputs['Color'])
            links.new(normal_map.outputs['Normal'], bsdf.inputs['Normal'])
    
    def move_uvs_back_and_assign_materials(self, obj, udim_to_material):
        """Move UVs back to 0-1 and assign materials per UDIM tile"""
        import bmesh
        
        # Clear existing materials and add new ones
        obj.data.materials.clear()
        
        udim_to_slot = {}
        for udim_number in sorted(udim_to_material.keys()):
            mat = udim_to_material[udim_number]
            obj.data.materials.append(mat)
            udim_to_slot[udim_number] = len(obj.data.materials) - 1
        
        # Move UVs and assign materials
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        
        if not bm.loops.layers.uv:
            bm.free()
            return
        
        uv_layer = bm.loops.layers.uv.active
        skipped_faces = 0

        for face in bm.faces:
            # Determine UDIM tile from UV coordinates (shared centroid rule
            # of core/udim_tiles — the per-loop vote made a face that fills
            # a tile exactly depend on its loop order)
            udim_tile = face_tile_number([loop[uv_layer].uv for loop in face.loops])

            if udim_tile is not None:
                # Shift UVs ONLY for tiles that belong to this UDIM.
                # Faces in the negative zone (unwrap overshoot below 0 gives
                # "tile 1000" and a bogus (-9,+1) shift via Python modulo)
                # or in unknown tiles must be left untouched.
                if udim_tile not in udim_to_slot:
                    skipped_faces += 1
                    continue

                # Assign material
                face.material_index = udim_to_slot[udim_tile]

                # Move UVs back to 0-1
                udim_offset = udim_tile - 1001
                offset_u = udim_offset % 10
                offset_v = udim_offset // 10

                for loop in face.loops:
                    uv = loop[uv_layer].uv
                    uv.x -= offset_u
                    uv.y -= offset_v

        bm.to_mesh(obj.data)
        bm.free()
        obj.data.update()

        if skipped_faces:
            print(f"⚠️ {skipped_faces} faces outside known UDIM tiles — UVs left untouched")
        print(f"✅ UVs moved back to 0-1 and materials assigned")
    
    def get_next_sequence_number(self, context):
        """Get next available sequence number for generic materials
        Checks both scene materials and texture sets to avoid conflicts
        """
        max_num = 0
        
        # Check materials in scene
        for mat in bpy.data.materials:
            match = re.match(r'M_(\d+)_\d{4}', mat.name)
            if match:
                num = int(match.group(1))
                if num > max_num:
                    max_num = num
        
        # Check texture sets
        for tex_set in context.scene.agr_texture_sets:
            # Texture sets have format S_M_#_#### 
            set_name = tex_set.name
            if set_name.startswith("S_"):
                material_name = set_name[2:]  # Remove S_ prefix
                match = re.match(r'M_(\d+)_\d{4}', material_name)
                if match:
                    num = int(match.group(1))
                    if num > max_num:
                        max_num = num
        
        print(f"  Next sequence number: {max_num + 1}")
        return max_num + 1
    
    def cleanup_udim_material(self, udim_material):
        """Remove UDIM material and its images from the scene"""
        print(f"🧹 Cleaning up UDIM material: {udim_material.name}")
        
        # Collect UDIM images used by this material
        udim_images = []
        if udim_material.use_nodes:
            for node in udim_material.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    if node.image.source == 'TILED':
                        udim_images.append(node.image)
        
        # Remove the material only if no other users remain
        mat_name = udim_material.name
        if udim_material.users > 0:
            print(f"  ⏭️ Skipping material removal: still used by {udim_material.users} other(s): {mat_name}")
        else:
            try:
                bpy.data.materials.remove(udim_material)
                print(f"  ✅ Removed material: {mat_name}")
            except Exception as e:
                print(f"  ⚠️ Error removing material: {e}")

        # Remove UDIM images only if no other users remain
        for img in udim_images:
            img_name = img.name
            if img.users > 0:
                print(f"  ⏭️ Skipping image removal: still used by {img.users} other(s): {img_name}")
                continue
            try:
                bpy.data.images.remove(img)
                print(f"  ✅ Removed UDIM image: {img_name}")
            except Exception as e:
                print(f"  ⚠️ Error removing image: {e}")
        
        print(f"✅ UDIM material cleanup complete")


# Module-level cache for the dynamic tile enum: Blender requires the returned
# item strings to stay referenced, and the items callback must be cheap.
def _resolve_udim_context(op, context):
    """Shared invoke prologue for the tile-picker operators: resolve the
    UDIM directory, tiles and tile→material labels of the active object.
    Returns (udim_dir, tiles, tile_to_material, address, base_dir) or None
    (already reported)."""
    obj = context.active_object

    blend_path = bpy.data.filepath
    if not blend_path:
        op.report({'ERROR'}, "Save blend file first")
        return None

    try:
        address, obj_type = process_object_name(obj.name)
    except Exception as e:
        op.report({'ERROR'}, f"Invalid object name: {str(e)}")
        return None

    base_dir = Path(blend_path).parent
    use_main_dir = context.scene.agr_baker_settings.udim_use_main_directory
    udim_dir = find_udim_directory(address, obj_type, base_dir, use_main_dir)

    if not udim_dir:
        op.report({'ERROR'}, f"UDIM directory not found for {address}")
        return None

    tiles = sorted(scan_udim_tiles_in_dir(str(udim_dir)))
    if not tiles:
        op.report({'ERROR'}, "No UDIM tiles found on disk")
        return None

    # migrate=False: this runs from the HUD operators' invoke() (some are
    # REGISTER-only, no UNDO) - merely OPENING a tile picker must not
    # permanently write a record onto the mesh outside the undo stack
    mapping = load_udim_mapping(obj, udim_dir, migrate=False)
    if not mapping:
        # Shared address folder: a sibling (GroundEl/Flora) has no record of
        # its own, the carrier holds ALL tiles.  Without this the picker shows
        # unlabelled tiles and Convert Tile invents S_<addr>_tileNNNN names.
        _carrier, mapping = find_udim_record_carrier(udim_dir, exclude=obj)
    tile_to_material = {}
    if mapping:
        for tile_info in mapping.get('udim_tiles', []):
            tile_to_material[tile_info.get('udim_number')] = tile_info.get('material_name', '')

    return udim_dir, tiles, tile_to_material, address, base_dir


class AGR_OT_ConvertTileToSet(Operator, AGR_UDIMGridHUD):
    """Create a texture set (S_*) from one UDIM tile — click the tile on the viewport grid"""
    bl_idname = "agr.convert_tile_to_set"
    bl_label = "Convert Tile to Set"
    # No UNDO: the operator only writes files on disk
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH' or not obj.name.startswith("SM_"):
            cls.poll_message_set("Выберите MESH-объект с именем SM_*")
            return False
        if not object_has_udim(obj):
            cls.poll_message_set("У объекта нет UDIM-текстур")
            return False
        return True

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'ERROR'}, "Запустите из 3D View")
            return {'CANCELLED'}

        resolved = _resolve_udim_context(self, context)
        if not resolved:
            return {'CANCELLED'}
        udim_dir, tiles, tile_to_material, address, base_dir = resolved

        self._address = address
        self._agr_bake_dir = str(base_dir / _get_output_folder())
        self._obj_name = context.active_object.name
        self._hud_start(context, udim_dir, tiles, tile_to_material,
                        "Кликните тайл — он будет извлечён в текстурный сет; Esc — выход")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        common = self._handle_common(context, event)
        if common:
            return common

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            cell = self._cell_at(event.mouse_region_x, event.mouse_region_y)
            tile = self._tile_at_cell(cell) if cell else None
            if tile is not None:
                self._hud_finish(context)
                return self._convert_tile(context, tile)
            return {'RUNNING_MODAL'}

        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            self._hud_finish(context)
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    def _draw_hud(self, context):
        self._draw_hud_base("Convert Tile to Set")

    def _convert_tile(self, context, udim_number):
        try:
            tile_textures = find_tile_textures_in_dir(self._udim_dir, udim_number)
            if not tile_textures:
                self.report({'ERROR'}, f"No textures found for tile {udim_number}")
                return {'CANCELLED'}

            # Original set/material name from mapping, fallback to a generic
            # one. migrate=False: this operator is REGISTER-only (files on
            # disk), so migrating a legacy JSON here would stamp the mirror
            # attributes onto the mesh outside the undo stack.
            own = bpy.data.objects.get(self._obj_name)
            mapping = load_udim_mapping(own, self._udim_dir, migrate=False)
            if not mapping:
                # Sibling of a shared address folder — the record lives on the
                # carrier; without it the set is named S_<addr>_tileNNNN and
                # loses the link to its source material.
                _carrier, mapping = find_udim_record_carrier(self._udim_dir,
                                                             exclude=own)
            material_name = None
            if mapping:
                for tile_info in mapping.get('udim_tiles', []):
                    if tile_info.get('udim_number') == udim_number:
                        material_name = tile_info.get('material_name')
                        break
            if not material_name:
                material_name = f"{self._address}_tile{udim_number}"
                print(f"⚠️ No mapping entry for tile {udim_number}, using name: {material_name}")

            set_folder = os.path.join(self._agr_bake_dir, f"S_{material_name}")
            os.makedirs(set_folder, exist_ok=True)
            print(f"📁 Creating texture set: {set_folder}")

            copied_count = 0
            pil_image = None
            try:
                from PIL import Image as pil_image
            except ImportError:
                print("⚠️ Pillow not available — ERM split / Opacity extraction skipped, plain copies only")

            for tex_type, source_path in tile_textures.items():
                try:
                    if tex_type == 'Diffuse':
                        # Tile Diffuse may carry alpha — a set must always
                        # contain BOTH DiffuseOpacity (as-is) and plain RGB
                        # Diffuse, plus an Opacity map (alpha or white).
                        do_path = os.path.join(set_folder, f"T_{material_name}_DiffuseOpacity.png")
                        shutil.copy2(source_path, do_path)
                        copied_count += 1
                        print(f"  ✅ {os.path.basename(source_path)} -> T_{material_name}_DiffuseOpacity.png")

                        d_path = os.path.join(set_folder, f"T_{material_name}_Diffuse.png")
                        o_path = os.path.join(set_folder, f"T_{material_name}_Opacity.png")
                        has_alpha = _png_has_alpha(source_path)
                        if pil_image:
                            with pil_image.open(source_path) as img:
                                img.convert('RGB').save(d_path, 'PNG')
                                if has_alpha:
                                    img.split()[-1].save(o_path, 'PNG')
                                else:
                                    pil_image.new('L', img.size, 255).save(o_path, 'PNG')
                            copied_count += 2
                            print(f"  ✅ Diffuse + Opacity extracted from tile")
                        else:
                            shutil.copy2(source_path, d_path)
                            copied_count += 1
                    elif tex_type == 'ERM':
                        erm_path = os.path.join(set_folder, f"T_{material_name}_ERM.png")
                        shutil.copy2(source_path, erm_path)
                        copied_count += 1
                        print(f"  ✅ {os.path.basename(source_path)} -> T_{material_name}_ERM.png")

                        # Unpack packed channels into separate maps so the set
                        # is usable by LOW connect and atlas assembly
                        if pil_image:
                            with pil_image.open(source_path) as erm_img:
                                channels = erm_img.convert('RGB').split()
                            for channel, channel_type in zip(channels, ('Emit', 'Roughness', 'Metallic')):
                                channel.save(os.path.join(set_folder, f"T_{material_name}_{channel_type}.png"), 'PNG')
                                copied_count += 1
                            print(f"  ✅ ERM split into Emit / Roughness / Metallic")
                    else:
                        target_path = os.path.join(set_folder, f"T_{material_name}_{tex_type}.png")
                        shutil.copy2(source_path, target_path)
                        copied_count += 1
                        print(f"  ✅ {os.path.basename(source_path)} -> T_{material_name}_{tex_type}.png")
                except Exception as e:
                    print(f"  ❌ Error copying {tex_type}: {e}")

            if copied_count == 0:
                self.report({'ERROR'}, "Failed to copy tile textures")
                return {'CANCELLED'}

            # Refresh the sets list so the new set appears
            try:
                bpy.ops.agr.refresh_texture_sets(skip_alpha_strip=True)
            except Exception as e:
                print(f"⚠️ Could not refresh texture sets: {e}")

            self.report({'INFO'}, f"Tile {udim_number} -> S_{material_name} ({copied_count} textures)")
            return {'FINISHED'}

        except Exception as e:
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, f"Convert tile failed: {str(e)}")
            return {'CANCELLED'}


# ===== INTERACTIVE UDIM LAYOUT EDITOR =====

class AGR_OT_UDIMLayoutEditor(Operator, AGR_UDIMGridHUD):
    """Interactively rearrange UDIM tiles: drag tiles on the HUD grid, then Enter/S renames tile files, shifts UVs and updates udim_mapping.json"""
    bl_idname = "agr.udim_layout_editor"
    bl_label = "Edit UDIM Layout"
    # No UNDO: renames files on disk; cancel with Esc before saving instead
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH' or not obj.name.startswith("SM_"):
            cls.poll_message_set("Выберите MESH-объект с именем SM_*")
            return False
        if not object_has_udim(obj):
            cls.poll_message_set("У объекта нет UDIM-текстур")
            return False
        return True

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'ERROR'}, "Запустите из 3D View")
            return {'CANCELLED'}

        resolved = _resolve_udim_context(self, context)
        if not resolved:
            return {'CANCELLED'}
        udim_dir, tiles, tile_to_material, _address, _base_dir = resolved

        self._obj_name = context.active_object.name
        self._dragging = None
        self._hud_start(context, udim_dir, tiles, tile_to_material,
                        "ЛКМ тащить тайл, Enter/S сохранить, Esc отмена")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        common = self._handle_common(context, event)
        if common:
            return common

        if event.type == 'LEFTMOUSE':
            if event.value == 'PRESS':
                cell = self._cell_at(event.mouse_region_x, event.mouse_region_y)
                if cell:
                    tile = self._tile_at_cell(cell)
                    if tile is not None:
                        self._dragging = tile
            elif event.value == 'RELEASE' and self._dragging is not None:
                target = self._cell_at(event.mouse_region_x, event.mouse_region_y)
                if target:
                    occupant = self._tile_at_cell(target)
                    source_cell = self._slots[self._dragging]
                    if occupant is None:
                        self._slots[self._dragging] = target
                    elif occupant != self._dragging:
                        # Drop onto an occupied cell = swap the two tiles
                        self._slots[occupant] = source_cell
                        self._slots[self._dragging] = target
                self._dragging = None
            return {'RUNNING_MODAL'}

        if event.type in {'RET', 'NUMPAD_ENTER', 'S'} and event.value == 'PRESS':
            self._hud_finish(context)
            return self._apply(context)

        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            self._hud_finish(context)
            self.report({'INFO'}, "UDIM layout: отменено")
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    # ---- apply ----

    def _apply(self, context):
        obj = bpy.data.objects.get(self._obj_name)
        if obj is None:
            self.report({'ERROR'}, "Объект не найден")
            return {'CANCELLED'}

        changes = {}
        for tile, (col, row) in self._slots.items():
            new_tile = 1001 + col + row * 10
            if new_tile != tile:
                changes[tile] = new_tile

        if not changes:
            self.report({'INFO'}, "UDIM layout: изменений нет")
            return {'FINISHED'}

        try:
            self._rename_tile_files(changes)
            # The grid holds every tile of the SHARED address folder: dragging
            # a sibling's tile renames ITS files, so its UVs and its images
            # have to follow too, or its faces stay on the old number (= on
            # another material's texture).
            users = objects_using_udim_dir(self._udim_dir)
            if obj not in users:
                users.append(obj)
            for user in users:
                self._shift_uvs(user, changes)
            self._update_mapping(changes)
            for user in users:
                self._reload_images(user)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, f"UDIM layout: ошибка применения: {e}")
            return {'CANCELLED'}

        invalidate_udim_cache()
        self.report({'INFO'}, f"UDIM layout: перемещено тайлов: {len(changes)}")
        return {'FINISHED'}

    def _adopt_orphan_tmp(self, udim_dir):
        """Restore `*.agrtmp` left behind by an interrupted earlier run.
        Nothing scans for that extension, so an orphan is a tile that has
        silently vanished from the set — put it back before planning."""
        restored = 0
        for fname in os.listdir(udim_dir):
            if not fname.endswith('.agrtmp'):
                continue
            src = fname[:-len('.agrtmp')]
            if not re.search(r'\.(\d{4})\.png$', src):
                continue
            if os.path.exists(os.path.join(udim_dir, src)):
                continue   # the real file made it — the temp copy is stale
            try:
                os.rename(os.path.join(udim_dir, fname), os.path.join(udim_dir, src))
                restored += 1
            except OSError as exc:
                print(f"⚠️ UDIM layout: could not restore orphan {fname}: {exc}")
        if restored:
            print(f"♻️ UDIM layout: restored {restored} orphaned .agrtmp files")

    def _rename_tile_files(self, changes):
        udim_dir = self._udim_dir
        self._adopt_orphan_tmp(udim_dir)

        plan = []
        for fname in os.listdir(udim_dir):
            m = re.search(r'\.(\d{4})\.png$', fname)
            if not m:
                continue
            old = int(m.group(1))
            if old not in changes:
                continue
            dst = f"{fname[:m.start()]}.{changes[old]}.png"
            plan.append((fname, fname + '.agrtmp', dst))

        # No destination may already exist: renaming onto a live file would
        # destroy a tile that is not part of this move at all.
        sources = {src for src, _, _ in plan}
        for _src, _tmp, dst in plan:
            if dst not in sources and os.path.exists(os.path.join(udim_dir, dst)):
                raise RuntimeError(f"целевой файл уже существует: {dst}")

        # Two-phase rename so swapped tiles never collide on disk. Both
        # phases roll back: a failure in phase 2 (file locked by a viewer)
        # used to leave *.agrtmp that no scanner sees — the tiles simply
        # disappeared from the set.
        done_tmp = []
        done_dst = []
        try:
            for src, tmp, _dst in plan:
                os.rename(os.path.join(udim_dir, src), os.path.join(udim_dir, tmp))
                done_tmp.append((src, tmp))
            for _src, tmp, dst in plan:
                os.rename(os.path.join(udim_dir, tmp), os.path.join(udim_dir, dst))
                done_dst.append((tmp, dst))
        except OSError as exc:
            for tmp, dst in reversed(done_dst):
                try:
                    os.rename(os.path.join(udim_dir, dst), os.path.join(udim_dir, tmp))
                except OSError as back_exc:
                    print(f"❌ UDIM layout: rollback failed for {dst}: {back_exc}")
            for src, tmp in reversed(done_tmp):
                try:
                    os.rename(os.path.join(udim_dir, tmp), os.path.join(udim_dir, src))
                except OSError as back_exc:
                    print(f"❌ UDIM layout: rollback failed for {tmp}: {back_exc}")
            raise RuntimeError(f"переименование отменено и откатено: {exc}")

        print(f"📁 UDIM layout: renamed {len(plan)} tile files")

    def _shift_uvs(self, obj, changes):
        import bmesh
        if obj.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        bm = bmesh.new()
        bm.from_mesh(obj.data)
        uv_layer = bm.loops.layers.uv.active
        if not uv_layer:
            bm.free()
            return

        moved = 0
        for face in bm.faces:
            # Shared centroid rule (core/udim_tiles): a per-loop vote on a
            # face that exactly fills its tile picked the tile of whichever
            # corner the loop order started at, and shifted it by another
            # tile's delta.
            tile = face_tile_number([loop[uv_layer].uv for loop in face.loops])
            if tile is None:
                continue
            new_tile = changes.get(tile)
            if new_tile is None:
                continue
            old_col, old_row = (tile - 1001) % 10, (tile - 1001) // 10
            new_col, new_row = (new_tile - 1001) % 10, (new_tile - 1001) // 10
            du, dv = new_col - old_col, new_row - old_row
            for loop in face.loops:
                loop[uv_layer].uv.x += du
                loop[uv_layer].uv.y += dv
            moved += 1

        bm.to_mesh(obj.data)
        bm.free()
        obj.data.update()
        print(f"📐 UDIM layout: shifted UVs of {moved} faces")

    def _update_mapping(self, changes):
        obj = bpy.data.objects.get(self._obj_name)
        mapping = load_udim_mapping(obj, self._udim_dir)
        owner = obj
        if not mapping:
            # Sibling of a shared address folder: the record lives on the
            # carrier and MUST be re-numbered there — otherwise the renamed
            # files no longer match any entry.
            owner, mapping = find_udim_record_carrier(self._udim_dir, exclude=obj)
        if not mapping or owner is None:
            return
        for tile_info in mapping.get('udim_tiles', []):
            old = tile_info.get('udim_number')
            if old in changes:
                tile_info['udim_number'] = changes[old]
        save_udim_mapping(owner, self._udim_dir, mapping)

    def _reload_images(self, obj):
        for slot in obj.material_slots:
            if slot.material and slot.material.use_nodes:
                for node in slot.material.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image and node.image.source == 'TILED':
                        try:
                            node.image.reload()
                        except Exception as e:
                            print(f"⚠️ UDIM layout: reload failed for {node.image.name}: {e}")

    # ---- HUD ----

    def _draw_hud(self, context):
        import gpu
        self._draw_hud_base(f"UDIM Layout: {self._obj_name}", skip_tile=self._dragging)
        # Dragged tile follows the cursor on top of the base grid
        if self._dragging is not None:
            gpu.state.blend_set('ALPHA')
            mx, my = self._mouse
            half = self._cell // 2
            self._px_tile(mx - half, my - half, mx + half, my + half, self._dragging)
            gpu.state.blend_set('NONE')


# ===== REPLACE TILE WITH TEXTURE SET =====

class AGR_OT_ReplaceUDIMTile(Operator, AGR_UDIMGridHUD):
    """Overwrite ONE UDIM tile's textures with the checked texture set — click the tile on the viewport grid"""
    bl_idname = "agr.replace_udim_tile"
    bl_label = "Replace Tile with Set"
    # No UNDO: overwrites files on disk
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH' or not obj.name.startswith("SM_"):
            cls.poll_message_set("Выберите MESH-объект с именем SM_*")
            return False
        if not object_has_udim(obj):
            cls.poll_message_set("У объекта нет UDIM-текстур")
            return False
        selected = [ts for ts in context.scene.agr_texture_sets
                    if ts.is_selected and not ts.is_atlas]
        if len(selected) != 1:
            cls.poll_message_set("Отметьте РОВНО ОДИН текстурный сет галочкой в списке")
            return False
        return True

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'ERROR'}, "Запустите из 3D View")
            return {'CANCELLED'}

        resolved = _resolve_udim_context(self, context)
        if not resolved:
            return {'CANCELLED'}
        udim_dir, tiles, tile_to_material, _address, _base_dir = resolved

        source = next(ts for ts in context.scene.agr_texture_sets
                      if ts.is_selected and not ts.is_atlas)
        self._obj_name = context.active_object.name
        self._set_folder = source.folder_path
        self._set_material = source.material_name

        self._hud_start(context, udim_dir, tiles, tile_to_material,
                        f"Кликните тайл — он будет ПЕРЕЗАПИСАН сетом S_{self._set_material}; Esc — выход")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        common = self._handle_common(context, event)
        if common:
            return common

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            cell = self._cell_at(event.mouse_region_x, event.mouse_region_y)
            tile = self._tile_at_cell(cell) if cell else None
            if tile is not None:
                self._hud_finish(context)
                return self._replace_tile(context, tile)
            return {'RUNNING_MODAL'}

        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            self._hud_finish(context)
            return {'CANCELLED'}

        return {'RUNNING_MODAL'}

    def _draw_hud(self, context):
        self._draw_hud_base(f"Replace Tile: S_{self._set_material}")

    def _replace_tile(self, context, tile):
        tile_textures = find_tile_textures_in_dir(self._udim_dir, tile)
        if not tile_textures:
            self.report({'ERROR'}, f"No textures found for tile {tile}")
            return {'CANCELLED'}

        replaced = 0
        skipped = []
        self._alpha_warning = None
        for tex_type, target_path in tile_textures.items():
            src = self._find_source_for_type(tex_type, target_path)
            if src == 'COMPOSED':
                replaced += 1
                continue
            if not src:
                skipped.append(tex_type)
                continue
            try:
                shutil.copy2(src, target_path)
                replaced += 1
                print(f"  ✅ {tex_type}: {os.path.basename(src)} -> {os.path.basename(target_path)}")
            except Exception as e:
                print(f"  ❌ Error replacing {tex_type}: {e}")
                skipped.append(tex_type)

        if replaced == 0:
            self.report({'ERROR'}, "Не удалось заменить ни одной текстуры (нет подходящих файлов в сете)")
            return {'CANCELLED'}

        # The tile now shows another material — record it in the mapping.
        # The tile files are ALREADY replaced on disk at this point, so a
        # lost record must be reported, never dropped silently.
        obj_rec = bpy.data.objects.get(self._obj_name)
        mapping = load_udim_mapping(obj_rec, self._udim_dir) if obj_rec else None
        if not mapping:
            obj_rec, mapping = find_udim_record_carrier(self._udim_dir)
        if mapping is not None and obj_rec is not None:
            for tile_info in mapping.get('udim_tiles', []):
                if tile_info.get('udim_number') == tile:
                    tile_info['material_name'] = self._set_material
            if not save_udim_mapping(obj_rec, self._udim_dir, mapping):
                agr_report(self, 'WARNING',
                           "⚠️ UDIM: тайл заменён, но запись не сохранилась — "
                           "маппинг разойдётся с файлами")
        else:
            agr_report(self, 'WARNING',
                       "⚠️ UDIM: тайл заменён, но запись не найдена (объект "
                       "переименован/удалён?) — маппинг не обновлён")

        # Refresh viewport
        obj = bpy.data.objects.get(self._obj_name)
        if obj:
            for slot in obj.material_slots:
                if slot.material and slot.material.use_nodes:
                    for node in slot.material.node_tree.nodes:
                        if node.type == 'TEX_IMAGE' and node.image and node.image.source == 'TILED':
                            try:
                                node.image.reload()
                            except Exception:
                                pass
        invalidate_udim_cache()

        msg = f"Тайл {tile} заменён сетом S_{self._set_material} ({replaced} текстур)"
        if skipped:
            msg += f", пропущено: {', '.join(skipped)}"
        if self._alpha_warning:
            msg += f"; ⚠️ {self._alpha_warning}"
        if skipped or self._alpha_warning:
            agr_report(self, 'WARNING', msg)
        else:
            self.report({'INFO'}, msg)
        return {'FINISHED'}

    def _find_source_for_type(self, tex_type, target_path):
        """Path of the set's file for a tile texture type; Diffuse falls back
        to DiffuseOpacity; a missing ERM is composed from separate E/R/M.
        Returns path, 'COMPOSED' (already written) or None."""
        folder, mat = self._set_folder, self._set_material

        if tex_type == 'Diffuse':
            # DiffuseOpacity FIRST — create/add build the tile from it and
            # the UDIM material links its Alpha into the BSDF, so taking
            # the flat RGB Diffuse silently dropped the tile's alpha.
            do_path = os.path.join(folder, f"T_{mat}_DiffuseOpacity.png")
            if os.path.exists(do_path):
                return do_path
            d_path = os.path.join(folder, f"T_{mat}_Diffuse.png")
            if not os.path.exists(d_path):
                return None
            o_path = os.path.join(folder, f"T_{mat}_Opacity.png")
            if os.path.exists(o_path):
                if self._compose_diffuse_opacity(d_path, o_path, target_path):
                    print("  ✅ DiffuseOpacity composed from Diffuse + Opacity")
                    return 'COMPOSED'
                self._alpha_warning = ("нет DiffuseOpacity, а собрать RGBA "
                                       "не удалось (нужен Pillow) — альфа тайла потеряна")
            else:
                self._alpha_warning = ("в сете нет DiffuseOpacity/Opacity — "
                                       "альфа тайла потеряна")
            return d_path

        if tex_type == 'ERM':
            path = os.path.join(folder, f"T_{mat}_ERM.png")
            if os.path.exists(path):
                return path
            if self._compose_erm(target_path):
                print(f"  ✅ ERM composed from separate Emit/Roughness/Metallic")
                return 'COMPOSED'
            return None

        path = os.path.join(folder, f"T_{mat}_{tex_type}.png")
        return path if os.path.exists(path) else None

    def _compose_diffuse_opacity(self, diffuse_path, opacity_path, target_path):
        """Merge the set's separate Diffuse + Opacity into an RGBA file at
        target_path (the tile keeps its alpha channel)."""
        try:
            from PIL import Image
        except ImportError:
            return False
        try:
            with Image.open(diffuse_path) as d_img, Image.open(opacity_path) as o_img:
                rgb = d_img.convert('RGB')
                alpha = o_img.convert('L')
                if alpha.size != rgb.size:
                    alpha = alpha.resize(rgb.size, Image.LANCZOS)
                rgba = rgb.copy()
                rgba.putalpha(alpha)
                rgba.save(target_path, 'PNG')
            return True
        except Exception as exc:
            print(f"  ❌ Could not compose DiffuseOpacity: {exc}")
            return False

    def _compose_erm(self, target_path):
        """Pack the set's separate Emit/Roughness/Metallic into an ERM file
        directly at target_path. Missing channels get BSDF defaults."""
        try:
            from PIL import Image
        except ImportError:
            return False

        folder, mat = self._set_folder, self._set_material
        loaded = {}
        for key, tname in (('E', 'Emit'), ('R', 'Roughness'), ('M', 'Metallic')):
            path = os.path.join(folder, f"T_{mat}_{tname}.png")
            if os.path.exists(path):
                try:
                    with Image.open(path) as img:
                        loaded[key] = img.convert('L')
                except Exception:
                    pass
        if not loaded:
            return False

        size = next(iter(loaded.values())).size
        defaults = {'E': 0, 'R': 204, 'M': 0}  # match BSDF defaults (R=0.8)
        bands = []
        for key in ('E', 'R', 'M'):
            band = loaded.get(key)
            if band is None:
                band = Image.new('L', size, defaults[key])
            elif band.size != size:
                band = band.resize(size, Image.LANCZOS)
            bands.append(band)

        merged = Image.merge('RGB', tuple(bands))
        try:
            merged.save(target_path, 'PNG')
        finally:
            merged.close()
            for band in bands:
                band.close()
        return True


# ===== DELETE UDIM TILE =====

class AGR_OT_DeleteUDIMTile(Operator, AGR_UDIMGridHUD):
    """Delete UDIM tiles — click a tile on the viewport grid to remove its files (tiles with UV faces require a second confirming click)"""
    bl_idname = "agr.delete_udim_tile"
    bl_label = "Delete Tile"
    # No UNDO: deletes files on disk
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH' or not obj.name.startswith("SM_"):
            cls.poll_message_set("Выберите MESH-объект с именем SM_*")
            return False
        if not object_has_udim(obj):
            cls.poll_message_set("У объекта нет UDIM-текстур")
            return False
        return True

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'ERROR'}, "Запустите из 3D View")
            return {'CANCELLED'}

        resolved = _resolve_udim_context(self, context)
        if not resolved:
            return {'CANCELLED'}
        udim_dir, tiles, tile_to_material, _address, _base_dir = resolved

        obj = context.active_object
        self._obj_name = obj.name
        # The picker shows every tile of the SHARED address folder, so the
        # "contains UV faces" guard has to look at every object using it —
        # otherwise a sibling's tile is deleted with a single unwarned click.
        self._tiles_with_uv = set()
        for user in objects_using_udim_dir(udim_dir):
            self._tiles_with_uv |= tiles_with_uv(user)
        self._tiles_with_uv |= tiles_with_uv(obj)
        self._pending_confirm = None
        self._deleted_count = 0

        self._hud_start(context, udim_dir, tiles, tile_to_material,
                        "Клик — удалить тайл (можно несколько); Esc/Enter — выход")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        common = self._handle_common(context, event)
        if common:
            return common

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            cell = self._cell_at(event.mouse_region_x, event.mouse_region_y)
            tile = self._tile_at_cell(cell) if cell else None
            if tile is None:
                return {'RUNNING_MODAL'}

            # A tile carrying UV faces needs a second confirming click
            if tile in self._tiles_with_uv and self._pending_confirm != tile:
                self._pending_confirm = tile
                self._status = f"⚠ Тайл {tile} содержит развёртку! Кликните ещё раз для удаления"
                return {'RUNNING_MODAL'}

            self._delete_tile(tile)
            return {'RUNNING_MODAL'}

        if event.type in {'ESC', 'RIGHTMOUSE', 'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            self._hud_finish(context)
            return self._finish_report(context)

        return {'RUNNING_MODAL'}

    def _draw_hud(self, context):
        self._draw_hud_base("Delete UDIM Tiles")

    def _delete_tile(self, tile):
        removed_files = 0
        for fname in os.listdir(self._udim_dir):
            m = re.search(r'\.(\d{4})\.png$', fname)
            if m and int(m.group(1)) == tile:
                try:
                    os.remove(os.path.join(self._udim_dir, fname))
                    removed_files += 1
                except OSError as e:
                    print(f"  ❌ Error deleting {fname}: {e}")

        # Drop the tile from the mapping as well.  The tile files are
        # ALREADY deleted on disk - a lost record must be reported.
        obj_rec = bpy.data.objects.get(self._obj_name)
        mapping = load_udim_mapping(obj_rec, self._udim_dir) if obj_rec else None
        if not mapping:
            obj_rec, mapping = find_udim_record_carrier(self._udim_dir)
        if mapping is not None and obj_rec is not None:
            tiles_list = mapping.get('udim_tiles', [])
            mapping['udim_tiles'] = [t for t in tiles_list if t.get('udim_number') != tile]
            if not save_udim_mapping(obj_rec, self._udim_dir, mapping):
                agr_report(self, 'WARNING',
                           "⚠️ UDIM: тайл удалён, но запись не сохранилась — "
                           "маппинг разойдётся с файлами")
        else:
            agr_report(self, 'WARNING',
                       "⚠️ UDIM: тайл удалён с диска, но запись не найдена "
                       "(объект переименован/удалён?) — маппинг не обновлён")

        # Remove from the HUD state (stay open for further deletions)
        self._slots.pop(tile, None)
        self._labels.pop(tile, None)
        self._gpu_textures.pop(tile, None)
        self._pending_confirm = None
        self._deleted_count += 1
        self._status = f"Тайл {tile} удалён ({removed_files} файлов). Клик — удалить ещё; Esc — выход"
        print(f"🗑️ UDIM: deleted tile {tile} ({removed_files} files)")

    def _finish_report(self, context):
        if not self._deleted_count:
            self.report({'INFO'}, "Ничего не удалено")
            return {'CANCELLED'}

        obj = bpy.data.objects.get(self._obj_name)
        if obj:
            for slot in obj.material_slots:
                if slot.material and slot.material.use_nodes:
                    for node in slot.material.node_tree.nodes:
                        if node.type == 'TEX_IMAGE' and node.image and node.image.source == 'TILED':
                            try:
                                node.image.reload()
                            except Exception:
                                pass
        invalidate_udim_cache()
        self.report({'INFO'}, f"Удалено тайлов: {self._deleted_count}")
        return {'FINISHED'}


classes = (
    AGR_OT_CreateUDIM,
    AGR_OT_AddToUDIM,
    AGR_OT_RevertUDIM,
    AGR_OT_ConvertTileToSet,
    AGR_OT_UDIMLayoutEditor,
    AGR_OT_ReplaceUDIMTile,
    AGR_OT_DeleteUDIMTile,
)


def register():
    """Register UDIM operators"""
    for cls in classes:
        bpy.utils.register_class(cls)
    # Ownership token for the tile-picker HUDs: a reload recreates the
    # property at 0, so any surviving modal self-destructs on its next
    # event instead of drawing over the new session.
    bpy.types.WindowManager.agr_udim_hud_token = bpy.props.IntProperty(default=0)
    # dev-reload makes NEW function objects, so identity checks never see
    # the previous registration — dedup by name, as operators_link does
    _drop_stale_handlers(bpy.app.handlers.load_pre, _on_load_pre_hud.__name__)
    _drop_stale_handlers(bpy.app.handlers.load_post, _invalidate_udim_cache_on_load.__name__)
    bpy.app.handlers.load_pre.append(_on_load_pre_hud)
    bpy.app.handlers.load_post.append(_invalidate_udim_cache_on_load)
    print("✅ UDIM operators registered")


def unregister():
    """Unregister UDIM operators"""
    _cleanup_active_hud()
    if _on_load_pre_hud in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_on_load_pre_hud)
    if _invalidate_udim_cache_on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_invalidate_udim_cache_on_load)
    _has_udim_cache.clear()
    if hasattr(bpy.types.WindowManager, 'agr_udim_hud_token'):
        del bpy.types.WindowManager.agr_udim_hud_token
    unregister_classes(classes)  # idempotent: survives a half-registered module (R-glue-4)
    print("UDIM operators unregistered")

