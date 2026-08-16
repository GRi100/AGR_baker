"""
AGR Outliner Sync - keep the active Outliner object framed in the 3D View.

Ported from the standalone "Outliner View Sync" addon into AGR Tools.
Uses bpy.msgbus to react to active-object changes; only reacts when the
change originated from the Outliner (checked via recent operator history).

Properties live on WindowManager (not saved in .blend) and are prefixed
with agr_ to avoid clashing with the old standalone addon if both are
installed.
"""

import bpy
from bpy.props import BoolProperty, FloatProperty
from bpy.types import Operator, Panel
from math import cos, radians, sin
from mathutils import Vector

_last_active_name = None
_in_handler = False
_MSGBUS_OWNER = object()


def _find_view3d_context():
    ctx = bpy.context
    try:
        if ctx.area and ctx.area.type == "VIEW_3D":
            area = ctx.area
            region = None
            if ctx.region and ctx.region.type == "WINDOW":
                region = ctx.region
            else:
                for r in area.regions:
                    if r.type == "WINDOW":
                        region = r
                        break
            if region:
                return ctx.window, ctx.screen, area, region
    except Exception:
        pass

    wm = bpy.context.window_manager
    for window in wm.windows:
        screen = window.screen
        for area in screen.areas:
            if area.type == "VIEW_3D":
                for region in area.regions:
                    if region.type == "WINDOW":
                        return window, screen, area, region
    return None


def _object_axes(obj):
    """Normalised world axes of the object (scale/shear stripped); falls
    back to the world axes for degenerate (zero-scale) matrices."""
    m = obj.matrix_world.to_3x3()
    defaults = (Vector((1, 0, 0)), Vector((0, 1, 0)), Vector((0, 0, 1)))
    axes = []
    for i, default in enumerate(defaults):
        col = Vector(m.col[i])
        axes.append(col.normalized() if col.length > 1e-9 else default.copy())
    return axes


def _apply_three_quarter_view(area, obj, wm):
    """Rotate the view to an elevated front-corner (3/4) angle derived from
    the object's OWN axes: azimuth away from the local front (-Y),
    elevation above the local horizon.  Runs BEFORE view3d.view_selected,
    which then does the (smooth-view) center+zoom fit."""
    space = area.spaces.active
    r3d = getattr(space, "region_3d", None)
    if r3d is None or r3d.view_perspective == 'CAMERA':
        return  # never fight the user's camera view
    azimuth = float(getattr(wm, "agr_sync_azimuth", radians(45.0)))
    elevation = float(getattr(wm, "agr_sync_elevation", radians(25.0)))
    ax, ay, az = _object_axes(obj)
    # eye direction (object -> viewer): the local front is -Y, up is +Z
    eye = (ax * (sin(azimuth) * cos(elevation))
           - ay * (cos(azimuth) * cos(elevation))
           + az * sin(elevation))
    if eye.length < 1e-9:
        return
    # the view looks along its local -Z, so +Z must point AT the viewer
    r3d.view_rotation = eye.normalized().to_track_quat('Z', 'Y')


# --- occlusion-aware focus -------------------------------------------------
# The user clicks lights in the Outliner; walls often stand between the
# fitted camera and the light.  After the fit we ray-cast the scene from the
# object towards candidate view directions (preferred 3/4 first) and keep
# the first unblocked one; when EVERY direction is blocked (a light inside a
# room) the camera parks IN FRONT of the nearest occluder - the object is
# always visible.

_SCAN_AZIMUTH_OFFSETS = (0.0, 45.0, -45.0, 90.0, -90.0, 135.0, -135.0, 180.0)
_SCAN_ELEVATIONS = (None, 45.0, 70.0)   # None = the preferred elevation
_OCCLUSION_SELF_SKIPS = 8               # max self-hit step-throughs per ray
_OCCLUSION_NEAR_LIMIT = 0.25            # never park closer than this (m)


def _focus_center(obj):
    """World-space point to look at: bbox center for meshes, origin for
    lights/empties (their bound_box is degenerate)."""
    if obj.type == 'MESH' and obj.bound_box:
        local = Vector((0.0, 0.0, 0.0))
        for corner in obj.bound_box:
            local += Vector(corner)
        return obj.matrix_world @ (local / 8.0)
    return obj.matrix_world.translation.copy()


def _free_distance(scene, depsgraph, obj, origin, direction, max_dist):
    """Distance from origin along direction until the first FOREIGN surface;
    hits on the focused object itself are stepped through.  Returns max_dist
    when the ray stays clear, and None when the self-skip budget runs out
    with ONLY self-hits — a joined container crosses dozens of its own
    shells, and reporting that distance as an occluder would park the
    camera INSIDE the object."""
    travelled = 0.0
    start = origin.copy()
    for _ in range(_OCCLUSION_SELF_SKIPS):
        remaining = max_dist - travelled
        if remaining <= 0.0:
            return max_dist
        hit, loc, _n, _idx, hit_obj, _mat = scene.ray_cast(
            depsgraph, start, direction, distance=remaining)
        if not hit:
            return max_dist
        travelled += (loc - start).length
        if hit_obj is not None and getattr(hit_obj, "original", hit_obj) == obj:
            start = loc + direction * 1e-4
            travelled += 1e-4
            continue
        return travelled
    return None  # exhausted inside the object's own shells - no verdict


def _view_candidates(obj, wm):
    """Eye directions to try, in preference order (preferred 3/4 first,
    then azimuth sweeps at the preferred / 45deg / 70deg elevations)."""
    az0 = float(getattr(wm, "agr_sync_azimuth", radians(45.0)))
    el0 = float(getattr(wm, "agr_sync_elevation", radians(25.0)))
    ax, ay, az_axis = _object_axes(obj)

    def eye_dir(azimuth, elevation):
        d = (ax * (sin(azimuth) * cos(elevation))
             - ay * (cos(azimuth) * cos(elevation))
             + az_axis * sin(elevation))
        return d.normalized() if d.length > 1e-9 else None

    dirs = []
    for el_deg in _SCAN_ELEVATIONS:
        el = el0 if el_deg is None else radians(el_deg)
        for off_deg in _SCAN_AZIMUTH_OFFSETS:
            d = eye_dir(az0 + radians(off_deg), el)
            if d is not None:
                dirs.append(d)
    return dirs


def _pick_clear_view(depsgraph, obj, wm, needed, current_dir=None):
    """(direction, free_distance): the first candidate that stays clear for
    `needed` meters, else the least blocked one.  current_dir is tried
    first so an already-clear view is never changed."""
    scene = bpy.context.scene
    center = _focus_center(obj)
    candidates = _view_candidates(obj, wm)
    if current_dir is not None and current_dir.length > 1e-9:
        candidates.insert(0, current_dir.normalized())
    best_dir = None
    best_free = -1.0
    for d in candidates:
        free = _free_distance(scene, depsgraph, obj, center, d, needed)
        if free is None:
            continue  # ray never left the object's own shells - no verdict
        if free >= needed - 1e-6:
            return d, free
        if free > best_free:
            best_dir, best_free = d, free
    return best_dir, best_free


def _avoid_occlusion(area, obj, wm):
    """Post-fit pass: re-aim the fitted view so geometry does not hide the
    focused object; clamp the distance in front of the occluder when every
    direction is blocked."""
    space = area.spaces.active
    r3d = getattr(space, "region_3d", None)
    if r3d is None or r3d.view_perspective == 'CAMERA':
        return
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        needed = max(float(r3d.view_distance), _OCCLUSION_NEAR_LIMIT)
        current = r3d.view_rotation @ Vector((0.0, 0.0, 1.0))
        best_dir, best_free = _pick_clear_view(depsgraph, obj, wm, needed, current)
    except Exception as exc:
        # msgbus notify path - a ray_cast/depsgraph hiccup must never
        # traceback out of the handler
        print(f"⚠️ AGR Sync: обзор без препятствий пропущен: {exc}")
        return
    if best_dir is None:
        # every candidate died inside the object's own shells (bbox center
        # of a joined container) - keep the view_selected fit untouched
        return
    r3d.view_rotation = best_dir.to_track_quat('Z', 'Y')
    if best_free < needed - 1e-6:
        # fully enclosed object - park between the occluder and the object
        r3d.view_distance = max(_OCCLUSION_NEAR_LIMIT, best_free * 0.9)


def _sync_now():
    global _last_active_name, _in_handler

    if _in_handler:
        return

    wm = bpy.context.window_manager
    if not getattr(wm, "agr_sync_outliner_view", False):
        return

    view_layer = bpy.context.view_layer
    if not view_layer:
        return

    obj = view_layer.objects.active
    if obj is None:
        _last_active_name = None
        return

    if obj.name == _last_active_name:
        return

    _last_active_name = obj.name

    # view_selected uses selection; ensure active is selected.
    if not obj.select_get():
        try:
            obj.select_set(True)
        except Exception:
            return

    ctx_info = _find_view3d_context()
    if not ctx_info:
        return

    window, screen, area, region = ctx_info

    _in_handler = True
    try:
        override = {
            "window": window,
            "screen": screen,
            "area": area,
            "region": region,
            "scene": bpy.context.scene,
            "view_layer": view_layer,
        }
        with bpy.context.temp_override(**override):
            if getattr(wm, "agr_sync_auto_rotate", True):
                _apply_three_quarter_view(area, obj, wm)
            bpy.ops.view3d.view_selected(use_all_regions=False)
            factor = max(0.01, float(getattr(wm, "agr_sync_outliner_distance", 1.0)))
            if factor != 1.0:
                space = area.spaces.active
                if space and hasattr(space, "region_3d"):
                    r3d = space.region_3d
                    r3d.view_distance = max(0.001, r3d.view_distance * factor)
            # AFTER the fit and the distance factor: re-aim / step closer so
            # the focused object is never hidden behind geometry
            if getattr(wm, "agr_sync_avoid_occlusion", True):
                _avoid_occlusion(area, obj, wm)
    finally:
        _in_handler = False


def _recent_ops_info():
    wm = bpy.context.window_manager
    try:
        ops = list(wm.operators)
    except Exception:
        return "unknown", []

    recent = [getattr(op, "bl_idname", "") for op in ops[-8:]]

    # Determine origin by the most recent relevant operator.
    for op_id in reversed(recent):
        if op_id.startswith(("OUTLINER_OT_", "outliner.")):
            return "outliner", recent
        if op_id.startswith(("VIEW3D_OT_", "view3d.")):
            return "view3d", recent

    return "unknown", recent


def _on_active_object_change():
    wm = bpy.context.window_manager
    if not getattr(wm, "agr_sync_outliner_view", False):
        return

    origin, _ = _recent_ops_info()
    if origin != "outliner":
        return

    _sync_now()


def _subscribe_msgbus():
    if not hasattr(bpy, "msgbus"):
        return
    if not hasattr(bpy.types, "LayerObjects"):
        return
    bpy.msgbus.clear_by_owner(_MSGBUS_OWNER)
    bpy.msgbus.subscribe_rna(
        key=(bpy.types.LayerObjects, "active"),
        owner=_MSGBUS_OWNER,
        args=(),
        notify=_on_active_object_change,
    )


def _ensure_handlers():
    _subscribe_msgbus()


def _remove_handlers():
    if hasattr(bpy, "msgbus"):
        bpy.msgbus.clear_by_owner(_MSGBUS_OWNER)


def _sync_toggle(self, context):
    global _last_active_name
    if context.window_manager.agr_sync_outliner_view:
        _ensure_handlers()
        _last_active_name = None
    else:
        _remove_handlers()


@bpy.app.handlers.persistent
def _resubscribe_on_load(_dummy):
    """Blender wipes all msgbus subscribers on file load - re-subscribe
    when sync is still enabled on the surviving WindowManager."""
    global _last_active_name
    wm = bpy.context.window_manager
    if wm and getattr(wm, "agr_sync_outliner_view", False):
        _last_active_name = None
        _ensure_handlers()


class AGR_OT_sync_outliner_toggle(Operator):
    """Enable/disable framing the active Outliner object in the 3D View"""
    bl_idname = "agr.sync_outliner_toggle"
    bl_label = "Toggle Outliner Sync"

    def execute(self, context):
        wm = context.window_manager
        wm.agr_sync_outliner_view = not wm.agr_sync_outliner_view
        return {'FINISHED'}


class AGR_PT_SyncPanel(Panel):
    """AGR Outliner Sync panel in the AGR Tools sidebar"""
    bl_label = "AGR Sync"
    bl_idname = "AGR_PT_sync_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 30  # after AGR Lights (20), before AGR Share (100)

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager
        layout.prop(wm, "agr_sync_outliner_view", text="Sync Active to View")
        layout.prop(wm, "agr_sync_outliner_distance", text="Distance Factor")
        layout.prop(wm, "agr_sync_auto_rotate", text="Разворот 3/4")
        if wm.agr_sync_auto_rotate:
            row = layout.row(align=True)
            row.prop(wm, "agr_sync_azimuth", text="Азимут")
            row.prop(wm, "agr_sync_elevation", text="Наклон")
        layout.prop(wm, "agr_sync_avoid_occlusion", text="Не загораживать")
        layout.label(text="Фокус на активном объекте из Outliner", icon='INFO')


def _draw_outliner_header(self, context):
    layout = self.layout
    wm = context.window_manager
    icon = "RESTRICT_SELECT_OFF" if wm.agr_sync_outliner_view else "RESTRICT_SELECT_ON"
    layout.separator_spacer()
    layout.prop(wm, "agr_sync_outliner_view", text="", icon=icon, toggle=True)


classes = (
    AGR_OT_sync_outliner_toggle,
    AGR_PT_SyncPanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.WindowManager.agr_sync_outliner_view = BoolProperty(
        name="Sync Outliner to View",
        description="Frame the active Outliner object in the 3D View",
        default=False,
        update=_sync_toggle,
    )
    bpy.types.WindowManager.agr_sync_outliner_distance = FloatProperty(
        name="Distance Factor",
        description="Multiplier for view distance after framing (1.0 = default)",
        default=1.0,
        min=0.1,
        soft_max=5.0,
    )
    bpy.types.WindowManager.agr_sync_auto_rotate = BoolProperty(
        name="Разворот 3/4",
        description="Перед фокусом развернуть вид на 3/4-ракурс к объекту "
                    "(сверху-спереди, по его собственным осям)",
        default=True,
    )
    bpy.types.WindowManager.agr_sync_azimuth = FloatProperty(
        name="Азимут",
        description="Отклонение взгляда от фасада объекта (его локальной -Y) по горизонтали",
        subtype='ANGLE',
        default=radians(45.0),
        min=-3.14159265,
        max=3.14159265,
    )
    bpy.types.WindowManager.agr_sync_elevation = FloatProperty(
        name="Наклон",
        description="Подъём взгляда над горизонтом объекта",
        subtype='ANGLE',
        default=radians(25.0),
        min=0.0,
        max=radians(85.0),
    )
    bpy.types.WindowManager.agr_sync_avoid_occlusion = BoolProperty(
        name="Не загораживать",
        description="Рейкастом найти ракурс, с которого объект не перекрыт "
                    "геометрией; если перекрыт со всех сторон (свет внутри "
                    "помещения) — камера встаёт ПЕРЕД ближайшей стеной",
        default=True,
    )

    if hasattr(bpy.types, "OUTLINER_HT_header"):
        bpy.types.OUTLINER_HT_header.append(_draw_outliner_header)

    if _resubscribe_on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_resubscribe_on_load)

    if bpy.context.window_manager and getattr(bpy.context.window_manager, "agr_sync_outliner_view", False):
        _ensure_handlers()

    print("✅ AGR Sync operators registered")


def unregister():
    if _resubscribe_on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_resubscribe_on_load)

    _remove_handlers()

    if hasattr(bpy.types, "OUTLINER_HT_header"):
        bpy.types.OUTLINER_HT_header.remove(_draw_outliner_header)

    if hasattr(bpy.types.WindowManager, "agr_sync_outliner_view"):
        del bpy.types.WindowManager.agr_sync_outliner_view
    if hasattr(bpy.types.WindowManager, "agr_sync_outliner_distance"):
        del bpy.types.WindowManager.agr_sync_outliner_distance
    if hasattr(bpy.types.WindowManager, "agr_sync_auto_rotate"):
        del bpy.types.WindowManager.agr_sync_auto_rotate
    if hasattr(bpy.types.WindowManager, "agr_sync_azimuth"):
        del bpy.types.WindowManager.agr_sync_azimuth
    if hasattr(bpy.types.WindowManager, "agr_sync_elevation"):
        del bpy.types.WindowManager.agr_sync_elevation
    if hasattr(bpy.types.WindowManager, "agr_sync_avoid_occlusion"):
        del bpy.types.WindowManager.agr_sync_avoid_occlusion

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
