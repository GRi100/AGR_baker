"""
AGR Lights - replace selected objects with light sources.

Ported from the standalone "Light Replacer" addon into AGR Tools.
Settings live in a PropertyGroup on the Scene (agr_light_settings) so they
are saved with the .blend file, mirroring the AGR_BakerSettings pattern.
"""

import bpy
from bpy.props import (
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    PointerProperty,
)
from bpy.types import Operator, Panel, PropertyGroup


class AGR_LightReplacerSettings(PropertyGroup):
    """Settings for the Replace-with-Light operator"""

    light_type: EnumProperty(
        name="Тип света",
        description="Тип создаваемого источника света",
        items=[
            ('POINT', "Точечный", "Точечный источник света"),
            ('SUN', "Солнце", "Солнечный свет"),
            ('SPOT', "Прожектор", "Направленный прожектор"),
            ('AREA', "Площадный", "Площадный источник света"),
        ],
        default='POINT',
    )

    power: FloatProperty(
        name="Мощность (Вт)",
        description="Мощность света в ваттах",
        default=100.0,
        min=0.0,
        soft_max=10000.0,
    )

    color: FloatVectorProperty(
        name="Цвет",
        description="Цвет света",
        subtype='COLOR',
        default=(1.0, 1.0, 1.0),
        min=0.0,
        max=1.0,
    )

    spot_size: FloatProperty(
        subtype='ANGLE',
        name="Размер конуса",
        description="Размер конуса прожектора",
        default=0.785398,  # 45 degrees
        min=0.017453,      # 1 degree
        max=3.141593,      # 180 degrees
    )

    spot_blend: FloatProperty(
        name="Размытие",
        description="Размытие краёв прожектора",
        default=0.15,
        min=0.0,
        max=1.0,
    )

    area_size: FloatProperty(
        name="Размер",
        description="Размер площадного света",
        default=1.0,
        min=0.01,
        max=100.0,
    )

    area_shape: EnumProperty(
        name="Форма",
        description="Форма площадного света",
        items=[
            ('SQUARE', "Квадрат", "Квадратный площадный свет"),
            ('RECTANGLE', "Прямоугольник", "Прямоугольный площадный свет"),
            ('DISK', "Диск", "Круглый площадный свет"),
            ('ELLIPSE', "Эллипс", "Эллиптический площадный свет"),
        ],
        default='SQUARE',
    )

    point_radius: FloatProperty(
        name="Радиус",
        description="Радиус точечного света для мягких теней",
        default=0.1,
        min=0.0,
        max=10.0,
    )


class AGR_OT_replace_with_light(Operator):
    """Replace every selected object with a light source"""
    bl_idname = "agr.replace_with_light"
    bl_label = "Replace with Light"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        # Removing the object being edited is unsupported - Object Mode only
        return context.mode == 'OBJECT'

    def execute(self, context):
        selected_objects = context.selected_objects.copy()
        if not selected_objects:
            self.report({'WARNING'}, "Нет выбранных объектов")
            return {'CANCELLED'}

        s = context.scene.agr_light_settings

        for obj in selected_objects:
            light_data = bpy.data.lights.new(name=f"{obj.name}_Light", type=s.light_type)
            light_data.energy = s.power
            light_data.color = s.color

            if s.light_type == 'SPOT':
                light_data.spot_size = s.spot_size
                light_data.spot_blend = s.spot_blend
            elif s.light_type == 'AREA':
                light_data.shape = s.area_shape
                light_data.size = s.area_size
                if s.area_shape in {'RECTANGLE', 'ELLIPSE'}:
                    light_data.size_y = s.area_size
            elif s.light_type == 'POINT':
                light_data.shadow_soft_size = s.point_radius

            light_object = bpy.data.objects.new(name=f"{obj.name}_Light", object_data=light_data)

            # Keep the replacement in the same collections as the original
            collections = list(obj.users_collection) or [context.collection]
            for coll in collections:
                coll.objects.link(light_object)

            light_object.matrix_world = obj.matrix_world.copy()

            bpy.data.objects.remove(obj, do_unlink=True)

        self.report({'INFO'}, f"Заменено {len(selected_objects)} объектов на источники света")
        print(f"✅ AGR Lights: replaced {len(selected_objects)} objects with {s.light_type} lights")
        return {'FINISHED'}


class AGR_PT_LightsPanel(Panel):
    """AGR Lights panel in the AGR Tools sidebar"""
    bl_label = "AGR Lights"
    bl_idname = "AGR_PT_lights_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 20  # after ui.py panels (0), before AGR Share (100)

    def draw(self, context):
        layout = self.layout
        s = context.scene.agr_light_settings

        col = layout.column(align=True)
        col.label(text="Настройки источника света", icon='LIGHT')
        col.prop(s, "light_type", text="Тип")
        col.prop(s, "power", text="Мощность (Вт)")
        col.prop(s, "color", text="Цвет")

        if s.light_type == 'SPOT':
            spot_col = layout.column(align=True)
            spot_col.label(text="Прожектор", icon='LIGHT_SPOT')
            spot_col.prop(s, "spot_size", text="Размер конуса")
            spot_col.prop(s, "spot_blend", text="Размытие")
        elif s.light_type == 'AREA':
            area_col = layout.column(align=True)
            area_col.label(text="Площадной свет", icon='LIGHT_AREA')
            area_col.prop(s, "area_shape", text="Форма")
            area_col.prop(s, "area_size", text="Размер")
        elif s.light_type == 'POINT':
            point_col = layout.column(align=True)
            point_col.label(text="Точечный свет", icon='LIGHT_POINT')
            point_col.prop(s, "point_radius", text="Радиус")

        layout.separator()

        selected_count = len(context.selected_objects)
        if selected_count > 0:
            layout.label(text=f"Выбрано объектов: {selected_count}", icon='INFO')

        row = layout.row()
        row.scale_y = 2.0
        if selected_count > 0:
            row.operator("agr.replace_with_light", text=f"Заменить {selected_count} объектов")
        else:
            row.enabled = False
            row.operator("agr.replace_with_light", text="Выберите объекты для замены")


classes = (
    AGR_LightReplacerSettings,
    AGR_OT_replace_with_light,
    AGR_PT_LightsPanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.agr_light_settings = PointerProperty(type=AGR_LightReplacerSettings)

    print("✅ AGR Lights operators registered")


def unregister():
    del bpy.types.Scene.agr_light_settings

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
