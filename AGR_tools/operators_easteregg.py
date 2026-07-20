"""
AGR Easter Egg - "Katya's confirmation" for Separate > By Loose Parts.

Separate By Loose Parts on a heavy mesh freezes Blender for a long time,
and it sits right under "By Material" in the P menu, so it gets clicked
by accident all the time. Katya asked for a confirmation dialog here -
this module is dedicated to her.

Implementation: VIEW3D_MT_edit_mesh_separate.draw is replaced at register
time with a version where "By Loose Parts" calls agr.separate_loose_confirm
(a dialog) instead of mesh.separate directly. The original draw function
is restored at unregister.
"""

import random

import bpy
from bpy.types import Operator

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

_original_separate_draw = None


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
        col.separator()
        sub = col.column(align=True)
        sub.enabled = False
        sub.label(text="Функционал добавлен по просьбе Кати. Мы помним.")

    def execute(self, context):
        try:
            result = bpy.ops.mesh.separate(type='LOOSE')
        except RuntimeError as e:
            self.report({'ERROR'}, f"Separate failed: {e}")
            return {'CANCELLED'}

        if 'FINISHED' in result:
            self.report({'INFO'}, "Разделено по Loose Parts. Катя гордится вами.")
        return result


def _agr_separate_menu_draw(self, context):
    """Replacement draw for VIEW3D_MT_edit_mesh_separate.

    Mirrors the stock menu (Selection / By Material / By Loose Parts) but
    routes By Loose Parts through Katya's confirmation dialog.
    """
    layout = self.layout
    layout.operator_context = 'INVOKE_DEFAULT'
    layout.operator("mesh.separate", text="Selection").type = 'SELECTED'
    layout.operator("mesh.separate", text="By Material").type = 'MATERIAL'
    layout.operator("agr.separate_loose_confirm", text="By Loose Parts")


# Marker so a re-register (addon reload) never mistakes the patched draw
# for the stock one and loses the original
_agr_separate_menu_draw._agr_separate_patch = True


classes = (
    AGR_OT_separate_loose_confirm,
)


def register():
    global _original_separate_draw

    for cls in classes:
        bpy.utils.register_class(cls)

    menu = getattr(bpy.types, "VIEW3D_MT_edit_mesh_separate", None)
    if menu is not None:
        if getattr(menu.draw, "_agr_separate_patch", False):
            # Unbalanced reload: the menu is still patched by a previous
            # module instance — recover the stock draw from the attribute
            # stashed on the patched function, then re-patch with ours.
            _original_separate_draw = getattr(menu.draw, "_agr_original_draw", None)
        else:
            _original_separate_draw = menu.draw
        # Stash the original ON the patched function so any future module
        # instance can restore it even without this module's global
        _agr_separate_menu_draw._agr_original_draw = _original_separate_draw
        menu.draw = _agr_separate_menu_draw
        print("✅ AGR Easter Egg: Katya now guards By Loose Parts")


def unregister():
    global _original_separate_draw

    menu = getattr(bpy.types, "VIEW3D_MT_edit_mesh_separate", None)
    if menu is not None:
        original = _original_separate_draw or getattr(menu.draw, "_agr_original_draw", None)
        if original is not None:
            menu.draw = original
        _original_separate_draw = None

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
