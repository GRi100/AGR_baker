"""
UI panels for AGR Tools.

Style rule: every collapsible section is a REAL Blender sub-panel
(bl_parent_id + DEFAULT_CLOSED) — native arrows, state stored by Blender.
No layout.box() in panels (boxes are allowed only inside operator dialogs).
"""

import bpy
import os
from bpy.types import Panel, UIList
from bpy.props import BoolProperty

from .log import LOG_PATH, STATUS_ICONS
from .operators_atlas import calculate_multi_atlas_packing
from .operators_rename import parse_sm_name, RENAME_ALLOWED_TYPES
from .operators_udim import object_has_udim

# Detected once at import: Python does NOT cache failed imports, so probing
# PIL inside draw() would rescan sys.path on every panel redraw.
# agr.install_pillow flips this flag after a successful install.
try:
    from PIL import Image as _PILImage  # noqa: F401
    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False


# ---- Texture set thumbnails (lazy bpy.utils.previews) ----

_previews = None
_thumb_misses = set()


def _get_preview_collection():
    global _previews
    if _previews is None:
        import bpy.utils.previews
        _previews = bpy.utils.previews.new()
    return _previews


def get_set_thumbnail_icon(tex_set):
    """icon_value of the set's representative texture, 0 when none.
    Loaded lazily on first draw of the row; misses are memoized so draw()
    does not re-probe the filesystem every redraw."""
    key = tex_set.folder_path
    if key in _thumb_misses:
        return 0
    pcoll = _get_preview_collection()
    if key in pcoll:
        return pcoll[key].icon_id

    candidates = [
        os.path.join(key, f"T_{tex_set.material_name}_DiffuseOpacity.png"),
        os.path.join(key, f"T_{tex_set.material_name}_Diffuse.png"),
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return pcoll.load(key, path, 'IMAGE').icon_id
            except Exception:
                break
    else:
        # LOW atlases use short-suffix names — take any PNG in the folder
        try:
            for fname in sorted(os.listdir(key)):
                if fname.lower().endswith('.png'):
                    return pcoll.load(key, os.path.join(key, fname), 'IMAGE').icon_id
        except OSError:
            pass

    _thumb_misses.add(key)
    return 0


def invalidate_set_thumbnails():
    """Drop all cached thumbnails (called by Refresh Sets and unregister)."""
    global _previews
    _thumb_misses.clear()
    if _previews is not None:
        import bpy.utils.previews
        bpy.utils.previews.remove(_previews)
        _previews = None


# ---- AGR_BAKE disk state indicator (cached by folder mtime) ----

_disk_state_cache = {"path": None, "mtime": None, "count": None}


def get_disk_sets_count(agr_bake_path):
    """Number of S_*/A_* folders on disk, or None when unavailable."""
    try:
        mtime = os.path.getmtime(agr_bake_path)
    except OSError:
        return None
    if _disk_state_cache["path"] == agr_bake_path and _disk_state_cache["mtime"] == mtime:
        return _disk_state_cache["count"]
    try:
        count = sum(
            1 for name in os.listdir(agr_bake_path)
            if name.startswith(("S_", "A_")) and os.path.isdir(os.path.join(agr_bake_path, name))
        )
    except OSError:
        return None
    _disk_state_cache.update(path=agr_bake_path, mtime=mtime, count=count)
    return count


def get_agr_bake_path(context):
    blend_dir = bpy.path.abspath("//")
    if not blend_dir:
        return None
    return os.path.join(blend_dir, context.scene.agr_baker_settings.output_folder)


# ===== TEXTURE SETS UILIST =====

class AGR_UL_TextureSetsList(UIList):
    """UI List for texture sets"""

    filter_show_sets: BoolProperty(
        name="Sets", description="Show texture sets (S_*)", default=True)
    filter_show_atlases: BoolProperty(
        name="Atlases", description="Show atlases (A_*)", default=True)
    filter_only_unassigned: BoolProperty(
        name="Unassigned only", description="Show only sets not connected to a material", default=False)

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        tex_set = item

        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)

            # Flat click-to-select row: no checkbox, no button chrome —
            # the name itself (emboss NONE) toggles is_selected, state is
            # the leading dot. Drag across rows sweeps natively.
            split = row.split(factor=0.5)

            name_row = split.row(align=True)
            name_row.emboss = 'NONE'
            name_row.prop(tex_set, "is_selected", text=tex_set.name,
                          icon='RADIOBUT_ON' if tex_set.is_selected else 'RADIOBUT_OFF')

            thumb = get_set_thumbnail_icon(tex_set)
            if thumb:
                name_row.label(text="", icon_value=thumb)

            # Middle: resolution
            mid_split = split.split(factor=0.5)
            mid_split.alignment = 'CENTER'
            mid_split.label(text=_resolution_badge(tex_set.resolution))

            # Right: indicators
            right = mid_split.row(align=True)
            right.alignment = 'RIGHT'
            if tex_set.is_atlas:
                right.label(text=tex_set.atlas_type, icon='UV')
            elif tex_set.is_assigned:
                right.label(text="", icon='LINKED')
            if tex_set.has_alpha:
                right.label(text="", icon='IMAGE_ALPHA')
            if not tex_set.is_atlas and not tex_set.has_alpha:
                right.label(text="")

        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            if item.is_atlas:
                layout.label(text="", icon='IMAGE_PLANE')
            else:
                layout.label(text="", icon='TEXTURE')

    def draw_filter(self, context, layout):
        row = layout.row(align=True)
        row.prop(self, "filter_name", text="", icon='VIEWZOOM')
        row.prop(self, "filter_show_sets", text="", icon='TEXTURE', toggle=True)
        row.prop(self, "filter_show_atlases", text="", icon='IMAGE_PLANE', toggle=True)
        row.prop(self, "filter_only_unassigned", text="", icon='UNLINKED', toggle=True)

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        helper = bpy.types.UI_UL_list

        if self.filter_name:
            flt_flags = helper.filter_items_by_name(
                self.filter_name, self.bitflag_filter_item, items, "name")
        else:
            flt_flags = [self.bitflag_filter_item] * len(items)

        # Display-only filtering: is_selected batch flags keep working on
        # hidden rows — batch operators iterate the full collection
        for i, item in enumerate(items):
            if item.is_atlas and not self.filter_show_atlases:
                flt_flags[i] = 0
            elif not item.is_atlas and not self.filter_show_sets:
                flt_flags[i] = 0
            elif self.filter_only_unassigned and item.is_assigned:
                flt_flags[i] = 0

        return flt_flags, []


# ===== AGR BAKER =====

class AGR_PT_MainPanel(Panel):
    """Main AGR Baker panel"""
    bl_label = "AGR Baker"
    bl_idname = "AGR_PT_main_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 0

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager

        # Status line: last operation + open-log button
        last_status = getattr(wm, 'agr_last_status', '')
        if last_status:
            status_row = layout.row(align=True)
            level = getattr(wm, 'agr_last_status_level', 'INFO')
            status_row.label(text=last_status, icon=STATUS_ICONS.get(level, 'INFO'))
            status_row.operator("wm.path_open", text="", icon='TEXT').filepath = LOG_PATH

        # Pillow installation check
        if not PILLOW_AVAILABLE:
            warning = layout.column(align=True)
            warning.alert = True
            warning.label(text="Pillow not installed", icon='ERROR')
            warning.operator("agr.install_pillow", text="Install Pillow", icon='IMPORT')

        # Context info (compact)
        obj = context.active_object
        if obj:
            info = layout.column(align=True)
            info.label(text=f"Active: {obj.name}", icon='OBJECT_DATA')
            mat_count = len([slot for slot in obj.material_slots if slot.material])
            if obj.active_material:
                info.label(text=f"Material: {obj.active_material.name} ({mat_count})", icon='MATERIAL')
            if len(context.selected_objects) > 1:
                info.label(text=f"Sources: {len(context.selected_objects) - 1} objects", icon='OUTLINER_OB_MESH')


class AGR_PT_BakeSettingsPanel(Panel):
    """Bake settings sub-panel"""
    bl_label = "Bake Settings"
    bl_idname = "AGR_PT_bake_settings_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_parent_id = "AGR_PT_main_panel"
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 0

    def draw(self, context):
        layout = self.layout
        settings = context.scene.agr_baker_settings

        col = layout.column(align=True)
        col.prop(settings, "resolution")
        row = col.row(align=True)
        row.prop(settings, "bake_with_alpha", toggle=True)
        row.prop(settings, "bake_normal_enabled", toggle=True)

        row = col.row(align=True)
        row.prop(settings, "max_ray_distance")
        row.prop(settings, "extrusion")

        col.separator()
        row = col.row(align=True)
        row.prop(settings, "bake_samples")
        row.prop(settings, "bake_device", text="")
        row.prop(settings, "bake_use_denoising", text="", icon='SHADERFX', toggle=True)


class AGR_PT_BakeConvertPanel(Panel):
    """Bake and convert buttons sub-panel"""
    bl_label = "Bake & Convert"
    bl_idname = "AGR_PT_bake_convert_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_parent_id = "AGR_PT_main_panel"
    bl_order = 1

    def draw(self, context):
        layout = self.layout

        row = layout.row()
        row.scale_y = 1.5
        row.operator("agr.bake_textures", text="Bake from High-Poly", icon='RENDER_STILL')

        col = layout.column(align=True)
        col.scale_y = 1.2
        col.operator("agr.simple_bake", text="Simple Bake Material", icon='MATERIAL')
        col.operator("agr.simple_bake_all", text="Simple Bake All Materials", icon='MATERIAL')

        col = layout.column(align=True)
        col.scale_y = 1.1
        col.operator("agr.convert_active_material_to_set", text="Convert Active Material", icon='EXPORT')
        col.operator("agr.convert_materials_to_sets", text="Convert All Materials", icon='EXPORT')


# ===== TEXTURE SETS =====

class AGR_PT_TextureSetsPanel(Panel):
    """Texture sets management panel (top-level tab; children keep pointing
    at AGR_PT_texture_sets_panel, so the whole subtree moves with it)"""
    bl_label = "AGR Texture Sets"
    bl_idname = "AGR_PT_texture_sets_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 5  # after AGR Baker (0), before AGR Rename (10)

    def draw(self, context):
        layout = self.layout

        # Refresh + open AGR_BAKE folder
        top_row = layout.row(align=True)
        top_row.operator("agr.refresh_texture_sets", text="Refresh Sets", icon='FILE_REFRESH')
        agr_bake_path = get_agr_bake_path(context)
        if agr_bake_path and os.path.isdir(agr_bake_path):
            top_row.operator("wm.path_open", text="", icon='FILE_FOLDER').filepath = agr_bake_path

        # Disk-state indicator: the scene list can lag behind the folder
        if agr_bake_path:
            disk_count = get_disk_sets_count(agr_bake_path)
            if disk_count is None:
                layout.label(text="Папка AGR_BAKE не найдена", icon='ERROR')
            elif disk_count != len(context.scene.agr_texture_sets):
                layout.label(text=f"На диске {disk_count} папок — список устарел, нажмите Refresh", icon='ERROR')


def _resolution_badge(resolution):
    """Short library-style resolution tag: 1024 -> 1K, 4096 -> 4K."""
    if resolution >= 1024 and resolution % 1024 == 0:
        return f"{resolution // 1024}K"
    return f"{resolution}px"


# template_icon scale per gallery column count (fills the N-panel width)
_GALLERY_SCALE = {1: 14.0, 2: 8.0, 3: 5.5, 4: 4.0}


class AGR_PT_SetsListPanel(Panel):
    """The texture sets list itself"""
    bl_label = "Sets"
    bl_idname = "AGR_PT_sets_list_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_parent_id = "AGR_PT_texture_sets_panel"
    bl_order = 0

    def draw_header(self, context):
        texture_sets = context.scene.agr_texture_sets
        selected = sum(1 for ts in texture_sets if ts.is_selected)
        text = f"({len(texture_sets)})"
        if selected:
            text += f"  выбрано: {selected}"
        self.layout.label(text=text)

    def draw(self, context):
        layout = self.layout
        settings = context.scene.agr_baker_settings
        texture_sets = context.scene.agr_texture_sets

        if len(texture_sets) == 0:
            layout.label(text="No texture sets found", icon='INFO')
            return

        # Custom viewport library (operators_library.py): persistent
        # overlay panel with big CLICKABLE previews — click toggles
        # selection (blue frame), drag paint-selects, wheel scrolls,
        # everything outside the panel passes through to Blender.
        wm = context.window_manager
        top = layout.row(align=True)
        top.operator("agr.library_toggle", text="Библиотека превью",
                     icon='IMGDISPLAY', depress=getattr(wm, 'agr_library_open', False))

        # View mode switch + gallery density
        view_row = layout.row(align=True)
        view_row.prop(settings, "sets_view_mode", expand=True)
        if settings.sets_view_mode == 'GALLERY':
            view_row.prop(settings, "gallery_columns", text="")

        if settings.sets_view_mode == 'LIST':
            # The active-row concept is meaningless here (selection is the
            # dot state) — a read-only dummy index keeps every row free of
            # the native blue active highlight
            layout.template_list(
                "AGR_UL_TextureSetsList", "",
                context.scene, "agr_texture_sets",
                context.scene, "agr_sets_no_active_index",
                rows=5
            )
        else:
            self._draw_gallery(layout, settings, texture_sets)

    def _draw_gallery(self, layout, settings, texture_sets):
        """Library-style cards: big preview, flat click-to-select name
        underneath (dot = state), badge row. For CLICKABLE previews use
        «Библиотека превью» — template_icon in panels is display-only."""
        columns = settings.gallery_columns
        scale = _GALLERY_SCALE.get(columns, 8.0)

        grid = layout.grid_flow(row_major=True, columns=columns,
                                even_columns=True, even_rows=False, align=False)

        for tex_set in texture_sets:
            card = grid.column(align=True)

            thumb = get_set_thumbnail_icon(tex_set)
            if thumb:
                card.template_icon(icon_value=thumb, scale=scale)
            else:
                card.label(text="(нет превью)", icon='TEXTURE')

            name_row = card.row(align=True)
            name_row.emboss = 'NONE'
            name_row.prop(tex_set, "is_selected", text=tex_set.name,
                          icon='RADIOBUT_ON' if tex_set.is_selected else 'RADIOBUT_OFF')

            tags = card.row(align=True)
            tags.alignment = 'LEFT'
            tags.label(text=_resolution_badge(tex_set.resolution))
            if tex_set.is_atlas:
                tags.label(text=tex_set.atlas_type, icon='UV')
            if tex_set.has_alpha:
                tags.label(text="", icon='IMAGE_ALPHA')
            if tex_set.is_assigned:
                tags.label(text="", icon='LINKED')

            card.separator()


class AGR_PT_SortSelectPanel(Panel):
    """Sort and selection helpers"""
    bl_label = "Sort & Select"
    bl_idname = "AGR_PT_sort_select_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_parent_id = "AGR_PT_texture_sets_panel"
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 1

    @classmethod
    def poll(cls, context):
        return len(context.scene.agr_texture_sets) > 0

    def draw(self, context):
        layout = self.layout
        settings = context.scene.agr_baker_settings

        sort_row = layout.row(align=True)
        sort_row.label(text="Sort:")
        sort_row.operator("agr.sort_sets_by_name", text="Name", depress=(settings.sets_sort_mode == 'NAME'))
        sort_row.operator("agr.sort_sets_by_resolution", text="Res", depress=(settings.sets_sort_mode == 'RESOLUTION'))
        sort_row.operator("agr.sort_sets_by_alpha", text="Alpha", depress=(settings.sets_sort_mode == 'ALPHA'))

        sel_row = layout.row(align=True)
        sel_row.label(text="Select:")
        op = sel_row.operator("agr.select_all_sets", text="All")
        op.action = 'SELECT'
        op = sel_row.operator("agr.select_all_sets", text="None")
        op.action = 'DESELECT'

        sel_row2 = layout.row(align=True)
        sel_row2.operator("agr.select_sets_with_alpha", text="With Alpha", icon='IMAGE_ALPHA')
        sel_row2.operator("agr.select_sets_by_resolution", text="By Res", icon='TEXTURE')
        sel_row2.operator("agr.select_sets_with_frame", text="Frame", icon='IMAGE_PLANE')

        sel_row3 = layout.row(align=True)
        sel_row3.operator("agr.select_sets_for_object", text="For Active", icon='OBJECT_DATA')
        sel_row3.operator("agr.select_set_for_active_material", text="For Active Mat", icon='MATERIAL')
        sel_row3.operator("agr.select_mirrored_sets", text="Mirrored", icon='MOD_MIRROR')
        sel_row3.operator("agr.select_stub_sets", text="Stubs", icon='COLOR')


class AGR_PT_BatchOpsPanel(Panel):
    """Batch operations over checked sets"""
    bl_label = "Batch Operations"
    bl_idname = "AGR_PT_batch_ops_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_parent_id = "AGR_PT_texture_sets_panel"
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 2

    @classmethod
    def poll(cls, context):
        return len(context.scene.agr_texture_sets) > 0

    def draw(self, context):
        layout = self.layout

        # Group 1: connect / assign (scene-side, most frequent)
        col = layout.column(align=True)
        col.label(text="Connect:", icon='LINKED')
        row = col.row(align=True)
        row.operator("agr.connect_set_to_material", text="HIGH")
        row.operator("agr.connect_regular_set_to_material", text="Regular")
        if context.active_object and context.active_object.type == 'MESH':
            col.operator("agr.assign_set_to_active", text="Assign to Active", icon='OBJECT_DATA')
        col.operator("agr.swap_sets_on_object", text="Swap on Object", icon='ARROW_LEFTRIGHT')

        # Group 2: file operations (rewrite PNGs on disk)
        col = layout.column(align=True)
        col.label(text="Files (irreversible):", icon='DISK_DRIVE')
        row = col.row(align=True)
        row.operator("agr.resize_texture_set", text="Resize", icon='IMAGE_DATA')
        row.operator("agr.gaussian_blur_set", text="Blur", icon='BRUSH_DATA')
        row = col.row(align=True)
        row.operator("agr.mirror_texture_set", text="Mirror", icon='MOD_MIRROR')
        row.operator("agr.strip_useless_alpha", text="Strip Alpha", icon='IMAGE_ALPHA')
        row = col.row(align=True)
        row.operator("agr.tile_texture_set", text="Tile ×N", icon='MOD_ARRAY')
        row.operator("agr.create_stub_set", text="Stub Set", icon='COLOR')

        # Group 3: frames / overlays
        col = layout.column(align=True)
        col.label(text="Frame:", icon='IMAGE_PLANE')
        row = col.row(align=True)
        row.operator("agr.create_frame_on_sets", text="On Sets")
        row.operator("agr.create_frame_on_files", text="On Files...")
        row = col.row(align=True)
        row.operator("agr.apply_overlay_to_files", text="Overlay...", icon='NODE_COMPOSITING')
        row.operator("agr.add_frame_overlay", text="Import...", icon='IMPORT')


class AGR_PT_DeleteOpsPanel(Panel):
    """Destructive operations, nested under Batch"""
    bl_label = "Delete Operations"
    bl_idname = "AGR_PT_delete_ops_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_parent_id = "AGR_PT_batch_ops_panel"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        row = col.row(align=True)
        op = row.operator("agr.delete_textures_from_selected", text="Del DO", icon='X')
        op.texture_type = 'DO'
        op = row.operator("agr.delete_textures_from_selected", text="Del ERM", icon='X')
        op.texture_type = 'ERM'
        op = row.operator("agr.delete_textures_from_selected", text="Del Normal", icon='X')
        op.texture_type = 'NORMAL'
        col.separator()
        col.operator("agr.delete_selected_sets", text="Delete Selected Sets", icon='TRASH')


def _ru_plural(n, one, few, many):
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return few
    return many


# Forecast for the Atlas Ops panel: how many atlases Create Multi-Atlas
# would produce for the active object.  draw() runs on every redraw, so the
# packing is cached by a cheap fingerprint (object, atlas size, matched sets).
_ATLAS_FORECAST_CACHE = {}


def _multi_atlas_forecast(context):
    """(bins, n_sets, missing, oversize) for the active object, or None."""
    obj = context.active_object
    if obj is None or obj.type != 'MESH' or not obj.material_slots:
        return None
    mats = []
    for slot in obj.material_slots:
        if slot.material and slot.material.name not in mats:
            mats.append(slot.material.name)
    if not mats:
        return None
    atlas_size = int(context.scene.agr_baker_settings.atlas_size)
    sets = []
    fp = []
    missing = 0
    for name in mats:
        ts = next((t for t in context.scene.agr_texture_sets
                   if t.material_name == name and not t.is_atlas), None)
        if ts is None:
            missing += 1
        else:
            sets.append(ts)
            fp.append((ts.name, ts.resolution))
    fingerprint = (obj.name, atlas_size, tuple(fp), missing)
    cached = _ATLAS_FORECAST_CACHE.get("forecast")
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    oversize = sum(1 for ts in sets if ts.resolution > atlas_size)
    bins = 0
    if sets and not oversize:
        try:
            bins = len(calculate_multi_atlas_packing(sets, atlas_size))
        except Exception:
            bins = 0  # draw() must never traceback
    info = (bins, len(sets), missing, oversize)
    _ATLAS_FORECAST_CACHE["forecast"] = (fingerprint, info)
    return info


class AGR_PT_AtlasOpsPanel(Panel):
    """Atlas creation / application"""
    bl_label = "Atlas Operations"
    bl_idname = "AGR_PT_atlas_ops_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_parent_id = "AGR_PT_texture_sets_panel"
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 3

    def draw(self, context):
        layout = self.layout
        settings = context.scene.agr_baker_settings

        layout.prop(settings, "atlas_size", text="Atlas Size")

        # Forecast: how many atlases the (multi) create operator will produce
        forecast = _multi_atlas_forecast(context)
        if forecast is not None:
            bins, n_sets, missing, oversize = forecast
            if oversize:
                layout.label(text=f"Сетов крупнее атласа: {oversize}", icon='ERROR')
            elif bins:
                word = _ru_plural(bins, "атлас", "атласа", "атласов")
                layout.label(text=f"Получится {bins} {word} (сетов: {n_sets})", icon='INFO')
            if missing:
                layout.label(text=f"Материалов без texture set: {missing}", icon='ERROR')

        # Poll() greys unavailable buttons and puts the reason in the tooltip
        col = layout.column(align=True)
        col.operator("agr.preview_atlas_layout_from_object", text="Preview Atlas from Object", icon='HIDE_OFF')
        col.operator("agr.create_multi_atlas_from_object", text="Create Multi-Atlas from Object", icon='IMGDISPLAY')

        col = layout.column(align=True)
        col.operator("agr.apply_atlas_to_object", text="Apply Atlas to Object", icon='UV')
        col.operator("agr.unpack_atlas_to_materials", text="Unpack Atlas to Materials", icon='LOOP_BACK')


class AGR_PT_AtlasFromSelectedPanel(Panel):
    """Atlas from checked sets, nested under Atlas Operations"""
    bl_label = "Atlas from Selected Sets"
    bl_idname = "AGR_PT_atlas_from_selected_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_parent_id = "AGR_PT_atlas_ops_panel"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        texture_sets = context.scene.agr_texture_sets

        selected_non_atlas = sum(1 for ts in texture_sets if ts.is_selected and not ts.is_atlas)
        layout.label(text=f"Selected: {selected_non_atlas} sets",
                     icon='CHECKBOX_HLT' if selected_non_atlas else 'CHECKBOX_DEHLT')

        col = layout.column(align=True)
        col.operator("agr.preview_atlas_layout", text="Preview Atlas Layout", icon='HIDE_OFF')
        col.operator("agr.create_atlas_only", text="Create Atlas Only", icon='IMAGE_PLANE')


class AGR_PT_UdimOpsPanel(Panel):
    """UDIM workflows"""
    bl_label = "UDIM Operations"
    bl_idname = "AGR_PT_udim_ops_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_parent_id = "AGR_PT_texture_sets_panel"
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 4

    def draw(self, context):
        layout = self.layout
        settings = context.scene.agr_baker_settings

        layout.prop(settings, "udim_use_main_directory", text="Use Main Directory")

        # object_has_udim is cached (fingerprint by material names/node
        # counts) — no per-redraw node-tree walk here
        obj = context.active_object
        if obj and obj.type == 'MESH' and obj.name.startswith("SM_") and object_has_udim(obj):
            col = layout.column(align=True)
            col.operator("agr.udim_layout_editor", text="Edit UDIM Layout", icon='MESH_GRID')
            col.operator("agr.replace_udim_tile", text="Replace Tile with Set", icon='ARROW_LEFTRIGHT')
            col.operator("agr.convert_tile_to_set", text="Convert Tile to Set", icon='EXPORT')
            col.operator("agr.delete_udim_tile", text="Delete Tile", icon='X')
            col.separator()
            col.operator("agr.add_to_udim", text="Add Sets to UDIM", icon='ADD')
            col.operator("agr.revert_udim", text="Disassemble UDIM", icon='LOOP_BACK')
        else:
            # Poll() greys the button with the reason in its tooltip
            layout.operator("agr.create_udim", text="Create UDIM Set", icon='UV_DATA')


# ===== SETTINGS =====

class AGR_PT_SettingsPanel(Panel):
    """Settings panel (sub-panel of AGR Baker)"""
    bl_label = "Settings"
    bl_idname = "AGR_PT_settings_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_parent_id = "AGR_PT_main_panel"
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 3

    def draw(self, context):
        layout = self.layout
        settings = context.scene.agr_baker_settings

        col = layout.column(align=True)
        col.label(text="Output Settings", icon='FILE_FOLDER')
        col.prop(settings, "output_folder", text="Folder Name")

        # Show full path + open-in-explorer
        full_path = get_agr_bake_path(context)
        if full_path:
            row = col.row(align=True)
            row.label(text=f"Path: {full_path}", icon='INFO')
            if os.path.isdir(full_path):
                row.operator("wm.path_open", text="", icon='FILE_FOLDER').filepath = full_path
        else:
            col.label(text="Save blend file to see path", icon='ERROR')


# ===== AGR RENAME =====

class AGR_PT_RenamePanel(Panel):
    """AGR Rename panel - with popup dialogs"""
    bl_label = "AGR Rename"
    bl_idname = "AGR_PT_rename_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 10

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        # Address input (+ autofill from the NNNN_Address lowpoly collection)
        col = layout.column(align=True)
        col.label(text="Settings", icon='SETTINGS')
        row = col.row(align=True)
        row.prop(scene, "agr_rename_address", text="Address")
        row.operator("agr.rename_autofill_address", text="", icon='EYEDROPPER')
        col.prop(scene, "agr_rp_project_lowpoly_number", text="Lowpoly Number")

        layout.separator()

        # Rename operations in compact column. One parse of the active
        # object's name drives every button — the per-button regex copies
        # used to diverge (see parse_sm_name / RENAME_ALLOWED_TYPES).
        col = layout.column(align=True)

        active_obj = context.active_object
        is_mesh = bool(active_obj and active_obj.type == 'MESH')
        parsed = parse_sm_name(active_obj.name) if is_mesh else None
        obj_type = parsed[2] if parsed else None
        has_address = bool(scene.agr_rename_address)

        def rename_row(idname, ok, ok_text, fail_text, icon):
            row = col.row()
            row.scale_y = 1.4
            if ok:
                row.operator(idname, text=ok_text, icon=icon)
            else:
                row.enabled = False
                row.operator(idname, text=fail_text)

        def fail_reason(need_type_text):
            if not has_address:
                return "Введите Address"
            if not is_mesh:
                return "Выберите MESH объект"
            return need_type_text

        # Rename Main Object (any mesh, address required)
        rename_row("agr.rename_main_object",
                   is_mesh and has_address,
                   "Переименовать основной объект",
                   "Введите Address" if not has_address else "Выберите MESH объект",
                   'OBJECT_DATA')

        # Rename Materials (address not required — object name carries it)
        rename_row("agr.rename_materials",
                   is_mesh and obj_type in RENAME_ALLOWED_TYPES['materials'],
                   "Переименовать материалы объекта",
                   "Выберите MESH объект" if not is_mesh else "Объект не соответствует формату",
                   'MATERIAL')

        # Rename Glass Materials
        rename_row("agr.rename_glass_materials",
                   is_mesh and has_address and obj_type in RENAME_ALLOWED_TYPES['glass_materials'],
                   "Переименовать материалы стекла",
                   fail_reason("Только для Glass объектов"),
                   'MATERIAL')

        # Rename UCX (works on selection, not the active object's type)
        selected_count = len([o for o in context.selected_objects if o.type == 'MESH'])
        rename_row("agr.rename_ucx",
                   has_address and selected_count > 0,
                   f"Переименовать в UCX ({selected_count})",
                   "Введите Address" if not has_address else "Выберите объекты для UCX",
                   'MESH_CUBE')

        # Rename Textures
        rename_row("agr.rename_textures",
                   is_mesh and has_address and obj_type in RENAME_ALLOWED_TYPES['textures'],
                   "Переименовать текстуры",
                   fail_reason("Только для Main/Ground/GroundEl/Flora"),
                   'TEXTURE')

        # Rename GEOJSON
        rename_row("agr.rename_geojson",
                   is_mesh and has_address and obj_type in RENAME_ALLOWED_TYPES['geojson'],
                   "Переименовать GEOJSON",
                   fail_reason("Только для Main/Ground"),
                   'FILE_TEXT')

        # Rename Lights Root
        row = col.row()
        row.scale_y = 1.4
        # Any EMPTY with LIGHT children qualifies — the operator's job is to
        # rename it INTO the *_Root convention, so don't require the suffix
        is_light_root = active_obj and active_obj.type == 'EMPTY' and scene.agr_rename_address
        if is_light_root:
            has_lights = any(child.type == 'LIGHT' for child in active_obj.children)
            if has_lights:
                row.operator("agr.rename_lights_root", text="Переименовать Root свет", icon='LIGHT')
            else:
                row.enabled = False
                row.operator("agr.rename_lights_root", text="Empty без источников света")
        else:
            row.enabled = False
            if not scene.agr_rename_address:
                row.operator("agr.rename_lights_root", text="Введите Address")
            else:
                row.operator("agr.rename_lights_root", text="Выберите Empty со светом")

        layout.separator()

        # Rename Project
        col = layout.column(align=True)
        col.label(text="Переименование проекта", icon='FILE_FOLDER')

        row = col.row()
        row.scale_y = 2.0
        if scene.agr_rename_address:
            row.operator("agr.rename_project", text="Переименовать ВЕСЬ ПРОЕКТ", icon='ERROR')
        else:
            row.enabled = False
            row.operator("agr.rename_project", text="Введите Address")


# ===== AGR JSON =====

class AGR_PT_JsonPanel(Panel):
    """GeoJSON management panel"""
    bl_label = "AGR JSON"
    bl_idname = "AGR_PT_json_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 15

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        folders = scene.agr_geojson_folders

        # Load + save
        row = layout.row(align=True)
        row.operator("agr.load_all_geojson", icon='FILE_REFRESH')
        if len(folders) > 0:
            row.operator("agr.save_all_geojson", text="Сохранить", icon='FILE_TICK')

        # Folder list (only shown after scan)
        if len(folders) > 0:
            layout.template_list(
                "AGR_UL_geojson_folder_list", "",
                scene, "agr_geojson_folders",
                scene, "agr_geojson_folders_index",
                rows=5,
            )

            # Create buttons
            row = layout.row(align=True)
            active_folder = None
            idx = scene.agr_geojson_folders_index
            if 0 <= idx < len(folders):
                active_folder = folders[idx]

            sub = row.row(align=True)
            sub.enabled = active_folder is not None and not active_folder.has_geojson
            sub.operator("agr.create_geojson", text="Создать GeoJSON", icon='FILE_NEW')

            sub = row.row(align=True)
            sub.enabled = any(not f.has_geojson for f in folders)
            sub.operator("agr.create_all_geojson", text="Создать все", icon='DOCUMENTS')


class AGR_PT_JsonFieldsPanel(Panel):
    """Shared GeoJSON fields"""
    bl_label = "Общие поля"
    bl_idname = "AGR_PT_json_fields_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_parent_id = "AGR_PT_json_panel"
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 0

    def draw(self, context):
        props = context.scene.agr_geojson_props
        col = self.layout.column(align=True)
        col.prop(props, "address")
        col.prop(props, "okrug")
        col.prop(props, "rajon")
        col.prop(props, "name")
        col.prop(props, "name_ground")
        col.separator()
        col.prop(props, "developer")
        col.prop(props, "designer")
        col.prop(props, "cadNum")
        col.separator()
        col.prop(props, "ZU_area")
        col.prop(props, "h_otn")
        col.prop(props, "h_abs")
        col.separator()
        col.prop(props, "s_obsh")
        col.prop(props, "s_naz")
        col.prop(props, "s_podz")
        col.prop(props, "spp_gns")
        col.separator()
        col.prop(props, "act_AGR")
        col.prop(props, "other")


class AGR_PT_JsonImagePanel(Panel):
    """Embedded image preview"""
    bl_label = "Изображение"
    bl_idname = "AGR_PT_json_image_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_parent_id = "AGR_PT_json_panel"
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 1

    def draw(self, context):
        props = context.scene.agr_geojson_props
        col = self.layout.column(align=True)

        # Preview image via template_icon
        preview_name = "__agr_geojson_preview__"
        if preview_name in bpy.data.images:
            img = bpy.data.images[preview_name]
            icon_id = img.preview.icon_id if img.preview else 0
            if icon_id:
                col.template_icon(icon_value=icon_id, scale=8.0)
                col.label(text="Изображение загружено", icon='CHECKMARK')
            else:
                # preview not ready yet, force it
                col.label(text="Превью обновляется...", icon='INFO')
                col.operator("agr.refresh_image_preview", icon='FILE_REFRESH')
        else:
            if props.has_image:
                col.label(text="Превью не загружено", icon='INFO')
                col.operator("agr.refresh_image_preview", icon='FILE_REFRESH')
            else:
                col.label(text="Нет изображения", icon='ERROR')

        col.separator()
        col.operator("agr.add_image_to_geojson", icon='IMAGE_DATA')


class AGR_PT_JsonFilePropsPanel(Panel):
    """Per-folder FNO fields"""
    bl_label = "Индивидуальные поля"
    bl_idname = "AGR_PT_json_file_props_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_parent_id = "AGR_PT_json_panel"
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 2

    @classmethod
    def poll(cls, context):
        return len(context.scene.agr_geojson_folders) > 0

    def draw(self, context):
        folders = context.scene.agr_geojson_folders
        col = self.layout.column(align=True)
        col.label(text="Код ФНО:", icon='TEXT')
        for folder in folders:
            suffix = folder.label_suffix
            label = f"Код ФНО {suffix}" if suffix else "Код ФНО"
            col.prop(folder, "FNO_code", text=label)

        col.separator()

        col.label(text="Название ФНО:", icon='TEXT')
        for folder in folders:
            suffix = folder.label_suffix
            label = f"Название ФНО {suffix}" if suffix else "Название ФНО"
            col.prop(folder, "FNO_name", text=label)


class AGR_PT_JsonCoordsPanel(Panel):
    """Per-folder coordinates"""
    bl_label = "Координаты"
    bl_idname = "AGR_PT_json_coords_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_parent_id = "AGR_PT_json_panel"
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 3

    @classmethod
    def poll(cls, context):
        return len(context.scene.agr_geojson_folders) > 0

    def draw(self, context):
        folders = context.scene.agr_geojson_folders
        col = self.layout.column(align=True)
        for folder in folders:
            suffix = folder.label_suffix
            label = suffix if suffix else folder.name
            row = col.row(align=True)
            row.label(text=label, icon='PIVOT_CURSOR')
            row.prop(folder, "coord_x", text="X")
            row.prop(folder, "coord_y", text="Y")
            row.prop(folder, "h_relief", text="H")


class AGR_PT_JsonGlassesPanel(Panel):
    """Per-folder glass materials"""
    bl_label = "Стёкла"
    bl_idname = "AGR_PT_json_glasses_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_parent_id = "AGR_PT_json_panel"
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 4

    @classmethod
    def poll(cls, context):
        return len(context.scene.agr_geojson_folders) > 0

    def draw(self, context):
        layout = self.layout
        folders = context.scene.agr_geojson_folders

        for fi, folder in enumerate(folders):
            suffix = folder.label_suffix
            folder_label = suffix if suffix else folder.name

            # Folder header with add button
            header_row = layout.row(align=True)
            header_row.label(text=folder_label, icon='NODE_MATERIAL')
            op_add = header_row.operator("agr.add_glass_entry", text="", icon='ADD')
            op_add.folder_index = fi

            # Compact glass entries
            for gi, glass in enumerate(folder.glasses):
                # Material name + delete on one row
                row = layout.row(align=True)
                row.prop(glass, "mat_name", text="")
                op_del = row.operator("agr.remove_glass_entry", text="", icon='X')
                op_del.folder_index = fi
                op_del.glass_index = gi

                # RGB + properties in two compact rows
                row = layout.row(align=True)
                row.prop(glass, "color_r", text="R")
                row.prop(glass, "color_g", text="G")
                row.prop(glass, "color_b", text="B")
                row.prop(glass, "transparency", text="T")

                row = layout.row(align=True)
                row.prop(glass, "refraction", text="IOR")
                row.prop(glass, "roughness", text="Rough")
                row.prop(glass, "metallicity", text="Met")


class AGR_PT_JsonUtilsPanel(Panel):
    """GeoJSON utilities"""
    bl_label = "Утилиты"
    bl_idname = "AGR_PT_json_utils_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_parent_id = "AGR_PT_json_panel"
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 5

    def draw(self, context):
        col = self.layout.column(align=True)
        col.operator("agr.add_glass_to_geojson", icon='NODE_MATERIAL')
        col.operator("agr.add_coords_to_geojson", icon='PIVOT_CURSOR')


# ===== REGISTRATION =====

# Parents must register before their sub-panels
classes = (
    AGR_UL_TextureSetsList,
    AGR_PT_MainPanel,
    AGR_PT_BakeSettingsPanel,
    AGR_PT_BakeConvertPanel,
    AGR_PT_TextureSetsPanel,
    AGR_PT_SetsListPanel,
    AGR_PT_SortSelectPanel,
    AGR_PT_BatchOpsPanel,
    AGR_PT_DeleteOpsPanel,
    AGR_PT_AtlasOpsPanel,
    AGR_PT_AtlasFromSelectedPanel,
    AGR_PT_UdimOpsPanel,
    AGR_PT_SettingsPanel,
    AGR_PT_RenamePanel,
    AGR_PT_JsonPanel,
    AGR_PT_JsonFieldsPanel,
    AGR_PT_JsonImagePanel,
    AGR_PT_JsonFilePropsPanel,
    AGR_PT_JsonCoordsPanel,
    AGR_PT_JsonGlassesPanel,
    AGR_PT_JsonUtilsPanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    # Register index property for UI list
    bpy.types.Scene.agr_texture_sets_index = bpy.props.IntProperty(default=0)
    # Read-only always -1: feeding this to template_list suppresses the
    # native blue active-row highlight (selection dots are the only state)
    bpy.types.Scene.agr_sets_no_active_index = bpy.props.IntProperty(
        default=-1,
        get=lambda self: -1,
        set=lambda self, value: None,
    )

    print("✅ UI registered")


def unregister():
    invalidate_set_thumbnails()

    del bpy.types.Scene.agr_texture_sets_index
    del bpy.types.Scene.agr_sets_no_active_index

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
