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
            bpy.ops.view3d.view_selected(use_all_regions=False)
            factor = max(0.01, float(getattr(wm, "agr_sync_outliner_distance", 1.0)))
            if factor != 1.0:
                space = area.spaces.active
                if space and hasattr(space, "region_3d"):
                    r3d = space.region_3d
                    r3d.view_distance = max(0.001, r3d.view_distance * factor)
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

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
