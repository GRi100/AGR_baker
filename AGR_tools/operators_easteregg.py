"""
AGR Easter Egg - "Katya's confirmation" for Separate > By Loose Parts.

Separate By Loose Parts on a heavy mesh freezes Blender for a long time,
and it sits right under "By Material" in the P menu, so it gets clicked
by accident all the time. Katya asked for a confirmation dialog here -
this module is dedicated to her.

Implementation (Blender 5.x): there is NO VIEW3D_MT_edit_mesh_separate
menu class anymore — the P key invokes mesh.separate directly and its enum
popup is drawn C-side, so there is nothing to monkeypatch.  Instead an
ADDON KEYMAP item shadows P in the "Mesh" keymap and opens our own menu
(AGR_MT_separate_confirm), which mirrors the stock popup but routes
"By Loose Parts" through the agr.separate_loose_confirm dialog.

Known limitation: the Mesh menu and the right-click context menu build
their Separate submenu inline via operator_menu_enum — those stay stock
(guarding them would mean re-drawing whole stock menus).  The accident
vector Katya asked about is the P popup, and that one is guarded.
"""

import random

import bpy
from bpy.types import Menu, Operator

# Vertex count above which the dialog switches to full panic mode
_DANGER_VERTS = 100_000

_KATYA_QUOTES = [
    "«Ты уверен? Я вот однажды не была...»",
    "«Подумай ещё раз. Blender не подумает.»",
    "«Это не кнопка, это лотерея.»",
    "«Сначала сохранись. Потом ещё раз сохранись.»",
    "«Loose Parts — это навсегда. Ну, почти.»",
    "«Я предупреждала. Теперь предупреждает Blender.»",
]

_addon_keymaps = []  # [(keymap, keymap_item)] added by register()


def _count_edit_verts(context):
    """Total vertex count across all meshes currently in edit mode"""
    total = 0
    try:
        for obj in context.objects_in_mode:
            if obj.type == 'MESH' and obj.data:
                # Mesh data is stale while in Edit Mode - flush the edit-mesh
                # first, or the count reflects the moment Edit Mode was entered
                obj.update_from_editmode()
                total += len(obj.data.vertices)
    except Exception:
        obj = context.active_object
        if obj and obj.type == 'MESH' and obj.data:
            try:
                obj.update_from_editmode()
            except Exception:
                pass
            total = len(obj.data.vertices)
    return total


class AGR_OT_separate_loose_confirm(Operator):
    """Separate by loose parts - but Katya asks first"""
    bl_idname = "agr.separate_loose_confirm"
    bl_label = "Катя спрашивает"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def invoke(self, context, event):
        self._verts = _count_edit_verts(context)
        self._quote = random.choice(_KATYA_QUOTES)
        width = 420 if self._verts >= _DANGER_VERTS else 360
        return context.window_manager.invoke_props_dialog(self, width=width)

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)

        col.label(text="Вы нажали By Loose Parts.", icon='ERROR')
        col.label(text=f"В меше {self._verts:,} вершин.".replace(",", " "))
        col.separator()

        if self._verts >= _DANGER_VERTS:
            alert = col.column(align=True)
            alert.alert = True
            alert.label(text="Это МНОГО. Blender может уйти", icon='SORTTIME')
            alert.label(text="в глубокую медитацию. Возможно, навсегда.")
            col.separator()

        col.label(text=f"Катя: {self._quote}", icon='COMMUNITY')

    def execute(self, context):
        try:
            result = bpy.ops.mesh.separate(type='LOOSE')
        except RuntimeError as e:
            self.report({'ERROR'}, f"Separate failed: {e}")
            return {'CANCELLED'}

        if 'FINISHED' in result:
            self.report({'INFO'}, "Разделено по Loose Parts. Катя гордится вами.")
        return result


class AGR_MT_separate_confirm(Menu):
    """Replacement for the stock P / Separate enum popup in mesh edit mode.

    Mirrors it item for item (Selection / By Material / By Loose Parts)
    but routes By Loose Parts through Katya's confirmation dialog.
    """
    bl_idname = "AGR_MT_separate_confirm"
    bl_label = "Separate"

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def draw(self, context):
        layout = self.layout
        layout.operator_context = 'INVOKE_DEFAULT'
        layout.operator("mesh.separate", text="Selection").type = 'SELECTED'
        layout.operator("mesh.separate", text="By Material").type = 'MATERIAL'
        layout.operator("agr.separate_loose_confirm", text="By Loose Parts")


classes = (
    AGR_OT_separate_loose_confirm,
    AGR_MT_separate_confirm,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    # Shadow the stock P binding ("mesh.separate" in the Mesh keymap) with
    # an addon keymap item opening our menu: addon keymaps take precedence
    # over the default keyconfig, user customizations still win over both.
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon if wm is not None else None
    if kc is None:
        print("⚠️ AGR Easter Egg: no addon keyconfig — Katya's P hook skipped")
        return
    km = kc.keymaps.new(name="Mesh", space_type='EMPTY')
    # dedupe after an unbalanced dev reload: a stale item would open the
    # menu twice (or keep working after a failed unregister)
    for kmi in list(km.keymap_items):
        if (kmi.idname == "wm.call_menu"
                and getattr(kmi.properties, "name", "") == AGR_MT_separate_confirm.bl_idname):
            km.keymap_items.remove(kmi)
    kmi = km.keymap_items.new("wm.call_menu", 'P', 'PRESS')
    kmi.properties.name = AGR_MT_separate_confirm.bl_idname
    _addon_keymaps.append((km, kmi))
    print("✅ AGR Easter Egg: Katya now guards By Loose Parts (P menu)")


def unregister():
    for km, kmi in _addon_keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass  # keymap already gone (e.g. Blender shutdown order)
    _addon_keymaps.clear()

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
