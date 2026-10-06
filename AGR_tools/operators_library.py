"""
AGR Library — texture set preview picker in the UDIM-tiles HUD style.

Built on the same AGR_UDIMGridHUD mixin as the UDIM tile tools, so it looks
and behaves identically: a grid anchored at the bottom-left of the viewport,
preview tiles with badge strips, the resize triangle at the grid's top-right
corner, wheel/MMB passed through for viewport navigation. Click a preview to
toggle the set's selection (thick blue frame), drag to paint-select several,
Esc/RMB/Enter closes.
"""

import bpy
import numpy as np
import os
from bpy.types import Operator
from bpy.app.handlers import persistent

from .log import drop_stale_handlers, unregister_classes
from .operators_udim import AGR_UDIMGridHUD

# path → (mtime, size) of the datablock the preview was built from.  Loading
# a full-size PNG per set cost 58.8 ms per 4K texture (≈6 s for 100 sets on
# EVERY open); the images now live in bpy.data between opens and are reused
# when the file on disk has not changed.
_PREVIEW_IMAGE_NAME = "__agr_library_preview"
_preview_cache = {}

# Singleton bookkeeping so load_pre/unregister can clean up a live library
_active_library = None

# Ownership token: every open stamps a fresh token into WindowManager.
# A modal whose token no longer matches (addon reloaded → property reset to
# 0, or a newer library opened) closes itself on its next event — without
# this, reloadOnSave dev loops leak zombie draw handlers.
_token_counter = 0


def _cleanup_active():
    global _active_library
    if _active_library is not None:
        try:
            _active_library._hud_finish(bpy.context)
        except Exception:
            pass
        _active_library = None
    try:
        bpy.context.window_manager.agr_library_open = False
    except Exception:
        pass


@persistent
def _library_on_load_pre(_dummy):
    # Draw handlers and temp datablocks must not survive a file switch
    _cleanup_active()
    drop_preview_cache()


def _preview_path(tex_set):
    """Representative PNG of the set, or None. Pure filesystem lookup —
    kept separate from the loading so it can be tested headlessly."""
    for name in (f"T_{tex_set.material_name}_DiffuseOpacity.png",
                 f"T_{tex_set.material_name}_Diffuse.png"):
        candidate = os.path.join(tex_set.folder_path, name)
        if os.path.exists(candidate):
            return candidate
    try:
        for fname in sorted(os.listdir(tex_set.folder_path)):
            if fname.lower().endswith('.png'):
                return os.path.join(tex_set.folder_path, fname)
    except OSError:
        pass
    return None


def _preview_image(path):
    """128px preview datablock for `path`, loaded at most once per session.
    The mtime is part of the key so a re-baked texture still refreshes.

    The datablock is GENERATED and packed — never a loaded FILE image.  A
    loaded one keeps `filepath` pointing at the real 4K texture, and
    `img.scale()` marks it dirty; because these previews now outlive the HUD
    (the whole point of the cache), a single Image → Save All Images, or the
    Save button of "Save changes before closing?", rewrote every browsed
    texture on disk as a 128×128 file.  A generated image has no filepath,
    so those paths skip it entirely.
    """
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        stamp = 0.0

    cached = _preview_cache.get(path)
    if cached is not None:
        name, cached_stamp = cached
        img = bpy.data.images.get(name)
        # The source stamp must match too: undo can free a datablock and the
        # next load may take its exact ".NNN" name for a DIFFERENT set, which
        # would show someone else's thumbnail
        if (img is not None and cached_stamp == stamp
                and img.get('agr_preview_src') == path):
            return img
        _preview_cache.pop(path, None)
        if img is not None:
            try:
                bpy.data.images.remove(img)
            except Exception:
                pass

    src = bpy.data.images.load(path, check_existing=False)
    try:
        src.scale(128, 128)   # full-size pixels are dropped right here
        buf = np.empty(128 * 128 * 4, dtype=np.float32)
        src.pixels.foreach_get(buf)
    finally:
        bpy.data.images.remove(src)

    img = bpy.data.images.new(_PREVIEW_IMAGE_NAME, 128, 128, alpha=True)
    img.pixels.foreach_set(buf)
    img['agr_preview_src'] = path
    try:
        img.pack()   # keeps the buffer alive across a depsgraph flush
    except Exception:
        pass
    _preview_cache[path] = (img.name, stamp)
    return img


def drop_preview_cache():
    """Release every cached preview datablock (file switch / unregister)."""
    for name, _stamp in list(_preview_cache.values()):
        img = bpy.data.images.get(name)
        if img is not None:
            try:
                bpy.data.images.remove(img)
            except Exception:
                pass
    _preview_cache.clear()


def _resolution_badge(resolution):
    if resolution >= 1024 and resolution % 1024 == 0:
        return f"{resolution // 1024}K"
    return f"{resolution}px"


class AGR_OT_LibraryToggle(Operator, AGR_UDIMGridHUD):
    """Texture set preview library (UDIM-tiles style): click a preview to select the set, drag to select many; Esc/Enter closes"""
    bl_idname = "agr.library_toggle"
    bl_label = "Sets Library"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        if not context.scene.agr_texture_sets:
            cls.poll_message_set("Список сетов пуст — нажмите Refresh Sets")
            return False
        return True

    # ---- lifecycle ----

    def invoke(self, context, event):
        global _active_library, _token_counter
        wm = context.window_manager

        if wm.agr_library_open:
            # Second press = close (also covers a stale flag after errors)
            wm.agr_library_open = False
            wm.agr_library_token = 0
            _cleanup_active()
            return {'FINISHED'}

        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'ERROR'}, "Запустите из 3D View")
            return {'CANCELLED'}

        sets = context.scene.agr_texture_sets
        count = len(sets)

        # UDIM-style grid state (the mixin's geometry/drawing works off
        # these attributes; keys are set indices instead of tile numbers)
        self._COLS = max(4, min(10, int(round(count ** 0.5)) + 2))
        self._cell = 96
        self._slots = {i: (i % self._COLS, i // self._COLS) for i in range(count)}
        self._labels = {i: sets[i].name for i in range(count)}
        self._rows = (count + self._COLS - 1) // self._COLS + 1
        self._mouse = (0, 0)
        self._hover_cell = None
        self._resizing = False
        self._painting = False
        self._paint_value = True
        self._status = "Клик/протяжка по превью — выбор; Esc/Enter — закрыть"
        self._load_set_previews(context)

        self._handle = bpy.types.SpaceView3D.draw_handler_add(
            self._draw_hud, (context,), 'WINDOW', 'POST_PIXEL')
        context.window_manager.modal_handler_add(self)
        _token_counter += 1
        self._token = _token_counter
        wm.agr_library_token = self._token
        wm.agr_library_open = True
        _active_library = self
        context.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def _load_set_previews(self, context):
        """GPU textures for every set, taken from the session preview cache.

        `self._preview_images` stays EMPTY on purpose: the mixin's
        `_hud_finish` deletes whatever is listed there, and these datablocks
        must outlive one open — re-decoding a 4K PNG per set is what made the
        library freeze Blender for seconds on every press."""
        import gpu
        self._gpu_textures = {}
        self._preview_images = []

        sets = context.scene.agr_texture_sets
        wm = context.window_manager
        show_progress = len(sets) > 8
        if show_progress:
            try:
                wm.progress_begin(0, len(sets))
            except Exception:
                show_progress = False

        try:
            for i, tex_set in enumerate(sets):
                if show_progress:
                    try:
                        wm.progress_update(i)
                    except Exception:
                        pass
                path = _preview_path(tex_set)
                if not path:
                    continue
                try:
                    img = _preview_image(path)
                    if img is not None:
                        self._gpu_textures[i] = gpu.texture.from_image(img)
                except Exception as e:
                    print(f"⚠️ Library: preview failed for {tex_set.name}: {e}")
        finally:
            if show_progress:
                try:
                    wm.progress_end()
                except Exception:
                    pass

    def _close(self, context):
        global _active_library
        self._hud_finish(context)
        if _active_library is self:
            _active_library = None
        try:
            context.window_manager.agr_library_open = False
            context.window_manager.agr_library_token = 0
        except Exception:
            pass

    # ---- modal (UDIM-picker pattern + paint-select) ----

    def modal(self, context, event):
        wm = context.window_manager

        # Ownership check: a reload reset the token to 0, or a newer
        # library instance took over — this instance is a zombie, clean
        # up its handler WITHOUT touching the shared flags.
        if getattr(wm, 'agr_library_token', 0) != getattr(self, '_token', -1):
            global _active_library
            self._hud_finish(context)
            if _active_library is self:
                _active_library = None
            return {'CANCELLED'}

        if not wm.agr_library_open:
            self._close(context)
            return {'FINISHED'}

        sets = context.scene.agr_texture_sets

        common = self._handle_common(context, event)
        if common:
            # Extend the paint stroke over the card under the cursor
            if self._painting and event.type == 'MOUSEMOVE' and self._hover_cell:
                idx = self._tile_at_cell(self._hover_cell)
                if idx is not None and idx < len(sets):
                    sets[idx].is_selected = self._paint_value
            return common

        if event.type == 'LEFTMOUSE':
            if event.value == 'PRESS':
                cell = self._cell_at(event.mouse_region_x, event.mouse_region_y)
                idx = self._tile_at_cell(cell) if cell else None
                if idx is not None and idx < len(sets):
                    sets[idx].is_selected = not sets[idx].is_selected
                    self._painting = True
                    self._paint_value = sets[idx].is_selected
                return {'RUNNING_MODAL'}
            if event.value == 'RELEASE':
                self._painting = False
                return {'RUNNING_MODAL'}
            return {'RUNNING_MODAL'}

        if event.type in {'ESC', 'RIGHTMOUSE', 'RET', 'NUMPAD_ENTER'} and event.value == 'PRESS':
            self._close(context)
            count = sum(1 for ts in sets if ts.is_selected)
            self.report({'INFO'}, f"Выбрано сетов: {count}")
            return {'FINISHED'}

        return {'RUNNING_MODAL'}

    # ---- drawing (mixin grid; per-tile look overridden below) ----

    def _draw_hud(self, context):
        sets = context.scene.agr_texture_sets
        count_sel = sum(1 for ts in sets if ts.is_selected)
        self._draw_hud_base(f"Библиотека сетов: {len(sets)}  |  выбрано: {count_sel}")

    def _px_tile(self, x0, y0, x1, y1, tile):
        """UDIM-tile visual language, with selection state on top:
        thick blue frame = selected, light frame = hovered; badge strip
        shows the resolution, bottom strip the set name."""
        import blf

        sets = bpy.context.scene.agr_texture_sets
        tex_set = sets[tile] if tile < len(sets) else None

        if tex_set is not None and tex_set.is_selected:
            self._px_rect(x0 - 3, y0 - 3, x1 + 3, y1 + 3, (0.15, 0.45, 1.0, 0.95))
        elif self._hover_cell is not None and self._slots.get(tile) == self._hover_cell:
            self._px_rect(x0 - 2, y0 - 2, x1 + 2, y1 + 2, (0.85, 0.85, 0.85, 0.5))

        texture = self._gpu_textures.get(tile)
        if texture is not None:
            self._px_rect(x0 - 1, y0 - 1, x1 + 1, y1 + 1, (0.05, 0.05, 0.05, 0.9))
            self._px_image(x0, y0, x1, y1, texture)
        else:
            self._px_rect(x0, y0, x1, y1, (0.28, 0.28, 0.3, 0.9))

        if tex_set is None:
            return

        # Resolution badge (top-left), same strip style as UDIM tile numbers
        self._px_rect(x0, y1 - 18, x0 + 36, y1, (0.0, 0.0, 0.0, 0.6))
        blf.size(0, 12)
        blf.color(0, 1.0, 1.0, 1.0, 1.0)
        blf.position(0, x0 + 4, y1 - 15, 0)
        blf.draw(0, _resolution_badge(tex_set.resolution))

        # Name strip at the bottom
        if (x1 - x0) >= 48:
            self._px_rect(x0, y0, x1, y0 + 14, (0.0, 0.0, 0.0, 0.6))
            blf.size(0, 10)
            blf.position(0, x0 + 4, y0 + 3, 0)
            max_chars = max(4, int((x1 - x0) / 6))
            blf.draw(0, tex_set.name[:max_chars])


classes = (
    AGR_OT_LibraryToggle,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.agr_library_open = bpy.props.BoolProperty(
        name="AGR Library Open", default=False)
    bpy.types.WindowManager.agr_library_token = bpy.props.IntProperty(
        name="AGR Library Token", default=0)
    # Dedup by NAME: a dev reload leaves the previous module's handler in
    # the list, and only the previous module could remove it by identity
    drop_stale_handlers(bpy.app.handlers.load_pre, "_library_on_load_pre")
    bpy.app.handlers.load_pre.append(_library_on_load_pre)
    print("✅ Library operator registered")


def unregister():
    _cleanup_active()
    drop_preview_cache()
    drop_stale_handlers(bpy.app.handlers.load_pre, "_library_on_load_pre")
    if hasattr(bpy.types.WindowManager, 'agr_library_open'):
        del bpy.types.WindowManager.agr_library_open
    if hasattr(bpy.types.WindowManager, 'agr_library_token'):
        del bpy.types.WindowManager.agr_library_token
    # Idempotent: a rollback may run on a module whose register() died
    # part-way, leaving only some of these classes live
    unregister_classes(classes)
