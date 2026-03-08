import bpy
import os
import json
import re
import math
import gpu
import gpu_extras
from gpu_extras.batch import batch_for_shader
import numpy as np
import random
from mathutils import Vector
import blf
from bpy.types import Panel, Operator, PropertyGroup, UIList
from bpy.props import StringProperty, BoolProperty, CollectionProperty, FloatProperty
import sys
import platform
import logging
from pathlib import Path
import traceback
import time
from datetime import timedelta
try:
    import cupy as cp
    HAS_CUDA = True
    print("\n=== Информация о GPU ===")
    print(f"CUDA доступна: Да")
    print(f"Текущее устройство: {cp.cuda.runtime.getDeviceProperties(0)['name'].decode()}")
    print(f"Общая память GPU: {cp.cuda.runtime.getDeviceProperties(0)['totalGlobalMem'] / 1024**3:.2f} GB")
    print("=====================\n")
except ImportError:
    HAS_CUDA = False
    print("\n=== Информация о GPU ===")
    print("CUDA недоступна. Будет использован CPU.")
    print("Для использования GPU установите библиотеку cupy:")
    print("pip install cupy-cuda11x  # для CUDA 11.x")
    print("или")
    print("pip install cupy-cuda12x  # для CUDA 12.x")
    print("=====================\n")

SCIPY_AVAILABLE = False
try:
    # Сначала пробуем импортировать scipy напрямую
    from scipy import ndimage
    SCIPY_AVAILABLE = True
    print("🔬 SciPy уже доступна для качественного ресайза")
except ImportError as e:
    print(f"⚠️ SciPy недоступна из коробки: {e}")
    print("🔧 Попытка автоматической установки...")


from bpy.props import EnumProperty, StringProperty, CollectionProperty, BoolProperty, IntProperty
from bpy.types import Operator, Panel, PropertyGroup
from gpu_extras.presets import draw_texture_2d
from pathlib import Path

bl_info = {
    "name": "AGR_baker",
    "author": "computer_invader",
    "version": (1, 1, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Baker",
    "description": "Работа с текстурными габорами АГР",
    "category": "Object",
}

def ensure_python_paths():
    """Добавляет пользовательские пути Python для доступа к установленным модулям"""
    try:
        import sys
        import site
        
        # Добавляем пользовательскую папку Python
        user_site = site.getusersitepackages()
        if user_site and user_site not in sys.path:
            sys.path.insert(0, user_site)
            print(f"📍 Добавлен пользовательский путь Python: {user_site}")
        
        # Также добавляем возможный путь для Windows
        try:
            import os
            appdata_path = os.path.join(os.environ.get('APPDATA', ''), 'Python', 'Python311', 'site-packages')
            if os.path.exists(appdata_path) and appdata_path not in sys.path:
                sys.path.insert(0, appdata_path)
                print(f"📍 Добавлен AppData путь Python: {appdata_path}")
        except Exception:
            pass
            
    except Exception as e:
        print(f"⚠️ Ошибка добавления Python путей: {e}")

def ensure_scene_properties():
    """Гарантирует, что коллекции Scene для аддона существуют (на случай частичной регистрации)."""
    # Эта функция больше не нужна, так как CollectionProperty создаются в register()
    pass

def connect_normal_map(nodes, links, tex_normal, bsdf, normal_type, location):
    """Подключает нормальную карту с учетом типа (OpenGL/DirectX)"""
    if normal_type == 'OPENGL':
        normal_map = nodes.new(type='ShaderNodeNormalMap')
        normal_map.location = location
        links.new(tex_normal.outputs['Color'], normal_map.inputs['Color'])
        links.new(normal_map.outputs['Normal'], bsdf.inputs['Normal'])
        
    elif normal_type == 'DIRECTX':
        separate_color = nodes.new(type='ShaderNodeSeparateColor')
        separate_color.location = (location[0] + 100, location[1] + 100)
        
        math_subtract = nodes.new(type='ShaderNodeMath')
        math_subtract.operation = 'SUBTRACT'
        math_subtract.location = (location[0] + 200, location[1] + 50)
        math_subtract.inputs[0].default_value = 1.0
        
        combine_color = nodes.new(type='ShaderNodeCombineColor')
        combine_color.location = (location[0] + 300, location[1])
        
        normal_map = nodes.new(type='ShaderNodeNormalMap')
        normal_map.location = (location[0] + 400, location[1])
        
        links.new(tex_normal.outputs['Color'], separate_color.inputs['Color'])
        
        links.new(separate_color.outputs['Green'], math_subtract.inputs[1])
        
        links.new(separate_color.outputs['Red'], combine_color.inputs['Red'])
        links.new(math_subtract.outputs['Value'], combine_color.inputs['Green'])
        links.new(separate_color.outputs['Blue'], combine_color.inputs['Blue'])
        
        links.new(combine_color.outputs['Color'], normal_map.inputs['Color'])
        links.new(normal_map.outputs['Normal'], bsdf.inputs['Normal'])
        

class BAKER_TextureSet(PropertyGroup):
    """Хранит информацию об одном наборе запеченных текстур"""
    name: StringProperty(
        name="Имя набора",
        description="Название набора текстур (обычно имя материала)",
        default=""
    )
    
    material_name: StringProperty(
        name="Материал",
        description="Имя материала, для которого был создан набор",
        default=""
    )
    
    object_name: StringProperty(
        name="Объект",
        description="Имя объекта, для которого был запечен набор",
        default=""
    )
    
    resolution: IntProperty(
        name="Разрешение",
        description="Разрешение текстур в наборе",
        default=1024
    )
    
    output_path: StringProperty(
        name="Путь",
        description="Путь к папке с текстурами",
        default=""
    )
    
    has_diffuse: BoolProperty(name="Диффузная", default=False)
    has_diffuse_opacity: BoolProperty(name="Диффузная+Альфа", default=False)
    has_normal: BoolProperty(name="Нормаль", default=False)
    has_normal_directx: BoolProperty(name="Нормаль DirectX", default=False)
    has_roughness: BoolProperty(name="Шероховатость", default=False)
    has_metallic: BoolProperty(name="Металлик", default=False)
    has_emit: BoolProperty(name="Эмиссия", default=False)
    has_opacity: BoolProperty(name="Прозрачность", default=False)
    has_erm: BoolProperty(name="ERM", default=False)
    
    is_selected_for_atlas: BoolProperty(
        name="Выбрано для атласа",
        description="Выбрать этот набор для включения в атлас",
        default=False
    )

class BAKER_AtlasData(PropertyGroup):
    """Хранит информацию о созданном атласе"""
    name: StringProperty(
        name="Имя атласа",
        description="Название атласа",
        default=""
    )
    
    atlas_type: EnumProperty(
        name="Тип атласа",
        description="Тип атласа (HIGH или LOW)",
        items=[
            ('HIGH', "HIGH", "Атлас с ERM и DIFFUSE_OPACITY"),
            ('LOW', "LOW", "Атлас с отдельными картами")
        ],
        default='HIGH'
    )
    
    atlas_size: IntProperty(
        name="Размер атласа",
        description="Размер атласа в пикселях",
        default=1024
    )
    
    output_path: StringProperty(
        name="Путь атласа",
        description="Путь к созданному атласу",
        default=""
    )
    
    texture_sets_count: IntProperty(
        name="Количество наборов",
        description="Количество наборов текстур в атласе",
        default=0
    )

class BAKER_UdimMaterial(PropertyGroup):
    """Хранит информацию о материале для UDIM"""
    material_name: StringProperty(
        name="Материал",
        description="Имя материала",
        default=""
    )
    
    object_name: StringProperty(
        name="Объект",
        description="Имя объекта",
        default=""
    )
    
    diffuse_path: StringProperty(
        name="Diffuse путь",
        description="Путь к диффузной текстуре",
        default=""
    )
    
    erm_path: StringProperty(
        name="ERM путь", 
        description="Путь к ERM текстуре",
        default=""
    )
    
    normal_path: StringProperty(
        name="Normal путь",
        description="Путь к нормаль текстуре", 
        default=""
    )
    
    output_path: StringProperty(
        name="Выходная папка",
        description="Папка с текстурами",
        default=""
    )

    material_index: IntProperty(
        name="Индекс материала",
        description="Индекс материала в слоте объекта",
        default=-1
    )

class BAKER_ObjectMaterialIndices(PropertyGroup):
    """Хранит информацию об индексах материалов объекта для UDIM"""
    object_name: StringProperty(
        name="Имя объекта",
        description="Имя объекта",
        default=""
    )

    material_indices: StringProperty(
        name="Индексы материалов",
        description="Список индексов материалов через запятую (материал_индекс:удим_номер)",
        default=""
    )

resolutions = [
    ('64', "64", "64 x 64 pixels"),
    ('128', "128", "128 x 128 pixels"),
    ('256', "256", "256 x 256 pixels"),
    ('512', "512", "512 x 512 pixels"),
    ('1024', "1024", "1024 x 1024 pixels"),
    ('2048', "2048", "2048 x 2048 pixels"),
    ('4096', "4096", "4096 x 4096 pixels"),
]

atlas_sizes = [
    ('128', "128", "128 x 128 pixels"),
    ('256', "256", "256 x 256 pixels"),
    ('512', "512", "512 x 512 pixels"),
    ('1024', "1024", "1024 x 1024 pixels"),
    ('2048', "2048", "2048 x 2048 pixels"),
    ('4096', "4096", "4096 x 4096 pixels"),
]

connection_modes = [
    ('HIGH', "HIGH", "Использует ERM комбинированную карту и DIFFUSE_OPACITY"),
    ('LOW', "LOW", "Использует отдельные карты DIFFUSE, METALLIC, ROUGHNESS, OPACITY, NORMAL"),
]

normal_types = [
    ('OPENGL', "OpenGL", "Стандартные нормали OpenGL"),
    ('DIRECTX', "DirectX", "Инвертированный зеленый канал для DirectX"),
]

class BAKER_OT_bake_textures(Operator):
    """Bake textures from high-poly to low-poly model"""
    bl_idname = "baker.bake_textures"
    bl_label = "Bake Textures"
    bl_options = {'REGISTER', 'UNDO'}
    
    resolution: EnumProperty(
        name="Resolution",
        description="Texture resolution",
        items=resolutions,
        default='1024'
    )
    
    output_path: StringProperty(
        name="Output Path",
        description="Path to save baked textures",
        default="",
        subtype='DIR_PATH'
    )
    
    connection_mode: EnumProperty(
        name="Connection Mode",
        description="Mode for connecting textures to material",
        items=connection_modes,
        default='HIGH'
    )
    
    normal_type: EnumProperty(
        name="Normal Type",
        description="Type of normal map to generate",
        items=normal_types,
        default='OPENGL'
    )

    max_ray_distance: FloatProperty(
        name="Max Ray Distance",
        description="Maximum ray distance for baking",
        default=0.0,
        min=0.0,
        max=100.0
    )

    extrusion: FloatProperty(
        name="Extrusion",
        description="Extrusion value for cage",
        default=0.50,
        min=0.0,
        max=10.0
    )

    simple_baking: BoolProperty(
        name="Simple Baking",
        description="Use simple baking mode (no selected to active, bake on active object only)",
        default=False
    )

    bake_with_alpha: BoolProperty(
        name="Bake with Alpha",
        description="Bake diffuse with alpha channel (opacity)",
        default=False
    )
    
    @classmethod
    def poll(cls, context):
        # В простом режиме достаточно одного активного объекта с материалом
        if context.active_object and context.active_object.type == 'MESH':
            if len(context.active_object.material_slots) > 0:
                return True
        
        # В обычном режиме нужно минимум 2 объекта (high-poly + low-poly)
        if len(context.selected_objects) < 2:
            return False
        for obj in context.selected_objects:
            if obj != context.active_object and obj.type == 'MESH' and context.active_object.type == 'MESH':
                return True
        return False
    
    def execute(self, context):
        # Сохраняем исходный режим (OBJECT, EDIT, etc.)
        original_mode = context.active_object.mode if context.active_object else 'OBJECT'
        print(f"🔄 Исходный режим: {original_mode}")
        
        # Переключаемся в OBJECT режим для запекания
        if context.active_object and context.active_object.mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
                print(f"🔄 Переключились в OBJECT режим для запекания")
            except Exception as e:
                print(f"⚠️ Не удалось переключиться в OBJECT режим: {e}")
        
        try:
            low_poly = context.active_object
            
            if self.simple_baking:
                # В простом режиме запекаем только на активном объекте
                high_poly_objects = []
                print("🔄 Режим простого запекания: запекание на активном объекте без selected_to_active")
            else:
                # В обычном режиме используем выбранные объекты как high-poly
                high_poly_objects = [obj for obj in context.selected_objects if obj != low_poly]
                if len(high_poly_objects) == 0:
                    self.report({'ERROR'}, "В обычном режиме нужно выбрать high-poly объекты")
                    return {'CANCELLED'}
                print(f"🔄 Режим обычного запекания: {len(high_poly_objects)} high-poly объектов")
            
            if len(low_poly.material_slots) == 0:
                self.report({'ERROR'}, "Low-poly object must have at least one material")
                return {'CANCELLED'}
            
            blend_file_path = bpy.path.abspath("//")
            if not blend_file_path:
                self.report({'ERROR'}, "Please save your file before baking")
                return {'CANCELLED'}
            
            main_baked_folder = os.path.join(blend_file_path, "OBJECT_BAKED")

            print(f"📁 Создаем структуру папок:")
            print(f"   Родительская папка: {main_baked_folder}")

            if not os.path.exists(main_baked_folder):
                os.makedirs(main_baked_folder)

            self.refresh_texture_sets_list(context)

            resolution = int(self.resolution)

            # Создаем отдельные папки для каждого материала
            materials_to_bake = []
            for material_slot in low_poly.material_slots:
                if material_slot.material:
                    materials_to_bake.append(material_slot.material)

            if not materials_to_bake:
                self.report({'ERROR'}, "Нет материалов для запекания")
                return {'CANCELLED'}

            print(f"   📦 Найдено материалов для запекания: {len(materials_to_bake)}")

            self.bake_all_textures(context, low_poly, high_poly_objects, resolution, main_baked_folder, materials_to_bake, self.simple_baking, self.bake_with_alpha)

            bpy.ops.baker.refresh_texture_sets()
            
            return {'FINISHED'}
        
        finally:
            # Восстанавливаем исходный режим
            if context.active_object and context.active_object.mode != original_mode:
                try:
                    bpy.ops.object.mode_set(mode=original_mode)
                    print(f"🔄 Восстановили исходный режим: {original_mode}")
                except Exception as e:
                    print(f"⚠️ Не удалось восстановить исходный режим {original_mode}: {e}")
        
    def bake_all_textures(self, context, low_poly, high_poly_objects, resolution, main_baked_folder, materials_to_bake, simple_mode=False, bake_with_alpha=False):
        original_engine = context.scene.render.engine
        original_samples = context.scene.cycles.samples
        original_denois = context.scene.cycles.use_denoising

        context.scene.render.engine = 'CYCLES'
        context.scene.cycles.samples = 1
        context.scene.cycles.use_denoising = False
        context.scene.cycles.device = 'CPU'
        
        print(f"🔧 Настройки запекания: engine=CYCLES, samples=1, device=CPU (принудительно)")

        # Проходим по всем материалам для запекания
        for material in materials_to_bake:
            low_poly_mat_name = material.name

            # Создаем отдельную папку для каждого материала
            material_output_path = os.path.join(main_baked_folder, f"{low_poly_mat_name}_baked")

            print(f"   📁 Папка материала: {material_output_path}")

            if not os.path.exists(material_output_path):
                os.makedirs(material_output_path)

            # Находим индекс материала в слотах объекта
            mat_idx = -1
            for i, slot in enumerate(low_poly.material_slots):
                if slot.material == material:
                    mat_idx = i
                    break

            if mat_idx == -1:
                print(f"⚠️ Материал {low_poly_mat_name} не найден в слотах объекта, пропускаем")
                continue

            diffuse_img = self.create_texture_image(f"T_{low_poly_mat_name}_DIFFUSE", resolution)
            roughness_img = self.create_texture_image(f"T_{low_poly_mat_name}_ROUGHNESS", resolution)
            metallic_img = self.create_texture_image(f"T_{low_poly_mat_name}_METALLIC", resolution)
            emit_img = self.create_texture_image(f"T_{low_poly_mat_name}_EMIT", resolution)
            normal_img = self.create_texture_image(f"T_{low_poly_mat_name}_NORMAL", resolution)
            opacity_img = self.create_texture_image(f"T_{low_poly_mat_name}_OPACITY", resolution)
            erm_img = self.create_texture_image(f"T_{low_poly_mat_name}_ERM", resolution)
            
            self.setup_bake_nodes(material)
            
            original_states = self.disable_metallic_for_diffuse_baking(high_poly_objects, simple_mode, low_poly, mat_idx)
            
            try:
                self.bake_texture(context, low_poly, high_poly_objects, diffuse_img, 'DIFFUSE', mat_idx, max_ray_distance=self.max_ray_distance, extrusion=self.extrusion, simple_mode=simple_mode)
                diffuse_path = os.path.join(material_output_path, f"T_{low_poly_mat_name}_DIFFUSE.png")
                self.save_texture(diffuse_img, diffuse_path)

                # Проверяем настройку запекания с альфа-каналом
                if bake_with_alpha:
                    print(f"🔄 Запекание с альфа-каналом включено")
                    diffuse_opacity_img = self.create_texture_image(f"T_{low_poly_mat_name}_DIFFUSE_OPACITY", resolution, with_alpha=True)
                    self.bake_texture(context, low_poly, high_poly_objects, diffuse_opacity_img, 'DIFFUSE', mat_idx, use_alpha=True, max_ray_distance=self.max_ray_distance, extrusion=self.extrusion, simple_mode=simple_mode)
                    
                    diffuse_opacity_path = os.path.join(material_output_path, f"T_{low_poly_mat_name}_DIFFUSE_OPACITY.png")
                    self.save_texture(diffuse_opacity_img, diffuse_opacity_path)
                    
                    # Извлекаем opacity из альфа-канала
                    self.extract_opacity(diffuse_opacity_img, opacity_img, low_poly, material_output_path)
                    self.save_texture(opacity_img, os.path.join(material_output_path, f"T_{low_poly_mat_name}_OPACITY.png"))
                    print(f"✅ Запечены DIFFUSE_OPACITY и OPACITY с альфа-каналом")
                else:
                    print(f"🔄 Запекание без альфа-канала - копируем DIFFUSE как DIFFUSE_OPACITY")
                    # Просто копируем DIFFUSE как DIFFUSE_OPACITY (без альфы)
                    diffuse_opacity_path = os.path.join(material_output_path, f"T_{low_poly_mat_name}_DIFFUSE_OPACITY.png")
                    import shutil
                    shutil.copy2(diffuse_path, diffuse_opacity_path)
                    
                    # Создаем белую OPACITY карту (полностью непрозрачная)
                    opacity_pixels = [1.0, 1.0, 1.0, 1.0] * (resolution * resolution)
                    opacity_img.pixels.foreach_set(opacity_pixels)
                    self.save_texture(opacity_img, os.path.join(material_output_path, f"T_{low_poly_mat_name}_OPACITY.png"))
                    print(f"✅ DIFFUSE скопирован как DIFFUSE_OPACITY, создана белая OPACITY")
            finally:
                self.restore_material_states(original_states)

            self.bake_texture(context, low_poly, high_poly_objects, roughness_img, 'ROUGHNESS', mat_idx, max_ray_distance=self.max_ray_distance, extrusion=self.extrusion, simple_mode=simple_mode)
            self.save_texture(roughness_img, os.path.join(material_output_path, f"T_{low_poly_mat_name}_ROUGHNESS.png"))

            self.bake_metallic(context, low_poly, high_poly_objects, metallic_img, mat_idx, simple_mode)
            self.save_texture(metallic_img, os.path.join(material_output_path, f"T_{low_poly_mat_name}_METALLIC.png"))

            self.bake_emission_strength(context, low_poly, high_poly_objects, emit_img, mat_idx, simple_mode)
            self.save_texture(emit_img, os.path.join(material_output_path, f"T_{low_poly_mat_name}_EMIT.png"))

            self.create_erm_texture(emit_img, roughness_img, metallic_img, erm_img, material_output_path)
            self.save_texture(erm_img, os.path.join(material_output_path, f"T_{low_poly_mat_name}_ERM.png"))
            
            if context.scene.baker_bake_normal_enabled:
                self.bake_texture(context, low_poly, high_poly_objects, normal_img, 'NORMAL', mat_idx, normal_type=self.normal_type, max_ray_distance=self.max_ray_distance, extrusion=self.extrusion, simple_mode=simple_mode)
                print(f"✅ Запечена нормаль с хайполи объекта для материала {low_poly_mat_name}")
            else:
                normal_img = self.create_flat_normal_image(f"T_{low_poly_mat_name}_NORMAL", resolution)
                print(f"🔄 Создана плоская нормаль (0.5, 0.5, 1) для материала {low_poly_mat_name}")
            
            if self.normal_type == 'DIRECTX':
                normal_filename = f"T_{low_poly_mat_name}_NORMAL_DIRECTX.png"
            else:
                normal_filename = f"T_{low_poly_mat_name}_NORMAL.png"
            self.save_texture(normal_img, os.path.join(material_output_path, normal_filename))

            print(f"\n📁 === ПОДКЛЮЧЕНИЕ ТЕКСТУР К МАТЕРИАЛУ ===")
            print(f"Текстуры будут загружены с диска для подключения к материалу {low_poly_mat_name}")

            self.connect_textures_to_material(material, diffuse_img, erm_img, normal_img, opacity_img, self.connection_mode, self.normal_type, material_output_path)

            self.save_texture_set_info_with_path(context, low_poly, low_poly_mat_name, resolution, material_output_path)

            print(f"Завершено запекание для материала: {low_poly_mat_name}")
            print(f"   📂 Текстуры сохранены в: {material_output_path}")

        # После завершения всех операций запекания удаляем конфликтующие текстуры
        processed_materials = [mat.name for mat in materials_to_bake]
        for material_name in processed_materials:
            self.remove_conflicting_textures(context, material_name, main_baked_folder, 'bake')

        context.scene.render.engine = original_engine
        context.scene.cycles.samples = original_samples
        context.scene.cycles.use_denoising = original_denois

        self.report({'INFO'}, f"Запекание завершено! Текстуры сохранены в {main_baked_folder}")
    
    def create_texture_image(self, name, resolution, with_alpha=False):
        if name in bpy.data.images:
            existing_image = bpy.data.images[name]
            counter = 1
            new_name = f"{name}.{counter:03d}"
            while new_name in bpy.data.images:
                counter += 1
                new_name = f"{name}.{counter:03d}"
            existing_image.name = new_name
            print(f"🔄 Переименован существующий образ: {name} → {new_name}")
        
        image = bpy.data.images.new(
            name,
            width=resolution,
            height=resolution,
            alpha=with_alpha,
            float_buffer=False
        )
        image.colorspace_settings.name = 'sRGB'
        
        if with_alpha:
            pixels = [1.0, 1.0, 1.0, 1.0] * (resolution * resolution)
        else:
            pixels = [1.0, 1.0, 1.0, 1.0] * (resolution * resolution)
        
        try:
            image.pixels.foreach_set(pixels)
        except Exception:
            image.pixels = pixels
        
        return image

    def create_flat_normal_image(self, name, resolution):
        """Создает изображение с плоской нормалью (0.5, 0.5, 1) в разрешении 256x256"""
        # Всегда используем 256 пикселей для плоской нормали
        flat_normal_resolution = 256

        if name in bpy.data.images:
            existing_image = bpy.data.images[name]
            counter = 1
            new_name = f"{name}.{counter:03d}"
            while new_name in bpy.data.images:
                counter += 1
                new_name = f"{name}.{counter:03d}"
            existing_image.name = new_name
            print(f"🔄 Переименован существующий образ: {name} → {new_name}")

        image = bpy.data.images.new(
            name,
            width=flat_normal_resolution,
            height=flat_normal_resolution,
            alpha=True,
            float_buffer=False
        )
        image.colorspace_settings.name = 'Non-Color'

        # Создаем плоскую нормаль RGBA(0.5, 0.5, 1.0, 1.0)
        pixels = [0.5, 0.5, 1.0, 1.0] * (flat_normal_resolution * flat_normal_resolution)

        try:
            image.pixels.foreach_set(pixels)
        except Exception:
            image.pixels = pixels

        return image

    def setup_bake_nodes(self, material):
        material.use_nodes = True
        nodes = material.node_tree.nodes
        
        output = None
        for node in nodes:
            if node.type == 'OUTPUT_MATERIAL':
                output = node
                break
        
        if not output:
            output = nodes.new(type='ShaderNodeOutputMaterial')
            output.location = (300, 0)
        
        bsdf = None
        for node in nodes:
            if node.type == 'BSDF_PRINCIPLED':
                bsdf = node
                break
        
        if not bsdf:
            bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
            bsdf.location = (0, 0)
            material.node_tree.links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
        
        bake_texture_node = None
        for node in nodes:
            if node.type == 'TEX_IMAGE' and node.name.startswith('BakeTexture'):
                bake_texture_node = node
                break
        
        if not bake_texture_node:
            bake_texture_node = nodes.new('ShaderNodeTexImage')
            bake_texture_node.name = 'BakeTexture'
            bake_texture_node.label = 'Bake Target'
            bake_texture_node.location = (-300, 0)
        
    
    def bake_texture(self, context, low_poly, high_poly_objects, image, bake_type, material_index, use_alpha=False, normal_type='OPENGL', max_ray_distance=0.0, extrusion=0.5, simple_mode=False):
        material = low_poly.material_slots[material_index].material
        
        nodes = material.node_tree.nodes
        texture_node = None
        
        for node in nodes:
            if node.type == 'TEX_IMAGE' and node.name.startswith('BakeTexture'):
                texture_node = node
                break
        
        if not texture_node:
            for node in nodes:
                if node.type == 'TEX_IMAGE':
                    texture_node = node
                    break
        
        if not texture_node:
            texture_node = nodes.new('ShaderNodeTexImage')
            texture_node.name = 'BakeTexture'
            texture_node.label = 'Bake Target'
            texture_node.location = (-300, 0)
        
        texture_node.image = image
        texture_node.select = True
        nodes.active = texture_node
        
        if simple_mode:
            # В простом режиме отключаем selected_to_active и другие опции
            context.scene.render.bake.use_selected_to_active = False
            context.scene.render.bake.cage_extrusion = 0.0
            context.scene.render.bake.max_ray_distance = 0.0
            print(f"🔧 Простое запекание: selected_to_active=False")
        else:
            # В обычном режиме используем настройки пользователя
            context.scene.render.bake.use_selected_to_active = True
            context.scene.render.bake.cage_extrusion = extrusion
            context.scene.render.bake.max_ray_distance = max_ray_distance
            print(f"🔧 Обычное запекание: selected_to_active=True, extrusion={extrusion}, max_ray_distance={max_ray_distance}")
        
        if bake_type == 'DIFFUSE' and use_alpha:
            context.scene.render.bake.margin = 0
        else:
            context.scene.render.bake.margin = 8
            
        context.scene.render.bake.use_clear = True  
        
        original_film_transparent = context.scene.render.film_transparent
        
        if bake_type == 'DIFFUSE':
            context.scene.cycles.bake_type = 'DIFFUSE'
            context.scene.render.bake.use_pass_direct = False   
            context.scene.render.bake.use_pass_indirect = False 
            context.scene.render.bake.use_pass_color = True     
            
            if use_alpha:
                context.scene.render.film_transparent = True
                image.colorspace_settings.name = 'sRGB'
                
        elif bake_type == 'ROUGHNESS':
            context.scene.cycles.bake_type = 'ROUGHNESS'
            image.colorspace_settings.name = 'Non-Color'  
            
        elif bake_type == 'NORMAL':
            context.scene.cycles.bake_type = 'NORMAL'
            context.scene.render.bake.normal_space = 'TANGENT'
            image.colorspace_settings.name = 'Non-Color'  
        
        bpy.ops.object.select_all(action='DESELECT')
        
        if simple_mode:
            # В простом режиме выбираем только активный объект
            low_poly.select_set(True)
            context.view_layer.objects.active = low_poly
            print(f"🎯 Простое запекание: выбран только {low_poly.name}")
        else:
            # В обычном режиме выбираем high-poly объекты + low-poly
            for obj in high_poly_objects:
                obj.select_set(True)
            low_poly.select_set(True)
            context.view_layer.objects.active = low_poly
            print(f"🎯 Обычное запекание: выбрано {len(high_poly_objects)} high-poly + {low_poly.name}")
        
        try:
            bpy.ops.object.bake(type=context.scene.cycles.bake_type)
            print(f"Успешно запечена текстура {bake_type} для материала {material.name}")
            
            if bake_type == 'NORMAL' and normal_type == 'DIRECTX':
                self.convert_normal_to_directx(image)
                print(f"✅ Конвертирована нормаль в DirectX формат (инвертирован зеленый канал)")
                
        except Exception as e:
            print(f"Ошибка при запекании {bake_type}: {e}")
            bpy.context.view_layer.update()
            try:
                bpy.ops.object.bake(type=context.scene.cycles.bake_type)
                print(f"Успешно запечена текстура {bake_type} при повторной попытке")
                
                if bake_type == 'NORMAL' and normal_type == 'DIRECTX':
                    self.convert_normal_to_directx(image)
                    print(f"✅ Конвертирована нормаль в DirectX формат при повторной попытке")
                    
            except Exception as e2:
                print(f"Повторная ошибка при запекании {bake_type}: {e2}")
        
        context.scene.render.film_transparent = original_film_transparent
    
    def bake_metallic(self, context, low_poly, high_poly_objects, metallic_img, material_index, simple_mode=False):
        """Запекает metallic, переподключив входы metallic к roughness"""
        original_states = []
        processed_materials = set()  # Отслеживаем уже обработанные материалы
        
        print(f"\n🔄 === ЗАПЕКАНИЕ METALLIC ===")
        print(f"Переподключаем входы metallic к roughness для запекания")
        
        try:
            if simple_mode:
                print("  🔄 Простой режим: работаем с активным материалом активного объекта")
                # В простом режиме работаем с активным материалом активного объекта
                low_poly_material = low_poly.material_slots[material_index].material
                if low_poly_material and low_poly_material.use_nodes:
                    processed_materials.add(low_poly_material.name)
                    print(f"  🔄 Обрабатываем активный материал: {low_poly_material.name}")
                    
                    nodes_data = []
                    for node in low_poly_material.node_tree.nodes:
                        if node.type == 'BSDF_PRINCIPLED':
                            metallic_value = node.inputs['Metallic'].default_value
                            roughness_value = node.inputs['Roughness'].default_value
                            
                            metallic_links = []
                            for link in low_poly_material.node_tree.links:
                                if link.to_socket == node.inputs['Metallic']:
                                    metallic_links.append((link.from_node, link.from_socket))
                            
                            roughness_links = []
                            for link in low_poly_material.node_tree.links:
                                if link.to_socket == node.inputs['Roughness']:
                                    roughness_links.append((link.from_node, link.from_socket))
                            
                            nodes_data.append((node, metallic_value, roughness_value, metallic_links, roughness_links))
                    
                    original_states.append((low_poly_material, nodes_data))
                    
                    for node_data in nodes_data:
                        node = node_data[0]
                        metallic_value = node_data[1]
                        metallic_links = node_data[3]
                        
                        for link in list(low_poly_material.node_tree.links):
                            if link.to_socket == node.inputs['Roughness']:
                                low_poly_material.node_tree.links.remove(link)
                        
                        for link in list(low_poly_material.node_tree.links):
                            if link.to_socket == node.inputs['Metallic']:
                                low_poly_material.node_tree.links.remove(link)
                        
                        for link_data in metallic_links:
                            from_node, from_socket = link_data
                            low_poly_material.node_tree.links.new(from_socket, node.inputs['Roughness'])
                            print(f"    Переподключил {from_node.name}:{from_socket.name} -> Roughness")
                        
                        if not metallic_links:
                            node.inputs['Roughness'].default_value = metallic_value
                            print(f"    Установил дефолтное значение metallic {metallic_value} в roughness")
                        
                        node.inputs['Metallic'].default_value = 0.0
            else:
                for high_poly in high_poly_objects:
                    for mat_slot in high_poly.material_slots:
                        if not mat_slot.material:
                            continue
                            
                        mat = mat_slot.material
                        if not mat.use_nodes:
                            continue
                        
                        # Пропускаем уже обработанные материалы
                        if mat.name in processed_materials:
                            print(f"  ⚠️ Материал {mat.name} уже обработан, пропускаем")
                            continue
                        
                        processed_materials.add(mat.name)
                        print(f"  🔄 Обрабатываем материал: {mat.name}")
                            
                        nodes_data = []
                        for node in mat.node_tree.nodes:
                            if node.type == 'BSDF_PRINCIPLED':
                                metallic_value = node.inputs['Metallic'].default_value
                                roughness_value = node.inputs['Roughness'].default_value
                                
                                metallic_links = []
                                for link in mat.node_tree.links:
                                    if link.to_socket == node.inputs['Metallic']:
                                        metallic_links.append((link.from_node, link.from_socket))
                                
                                roughness_links = []
                                for link in mat.node_tree.links:
                                    if link.to_socket == node.inputs['Roughness']:
                                        roughness_links.append((link.from_node, link.from_socket))
                                
                                nodes_data.append((node, metallic_value, roughness_value, metallic_links, roughness_links))
                        
                        original_states.append((mat, nodes_data))
                        
                        for node_data in nodes_data:
                            node = node_data[0]
                            metallic_value = node_data[1]
                            metallic_links = node_data[3]
                            
                            for link in list(mat.node_tree.links):
                                if link.to_socket == node.inputs['Roughness']:
                                    mat.node_tree.links.remove(link)
                            
                            for link in list(mat.node_tree.links):
                                if link.to_socket == node.inputs['Metallic']:
                                    mat.node_tree.links.remove(link)
                            
                            for link_data in metallic_links:
                                from_node, from_socket = link_data
                                mat.node_tree.links.new(from_socket, node.inputs['Roughness'])
                                print(f"    Переподключил {from_node.name}:{from_socket.name} -> Roughness")
                            
                            if not metallic_links:
                                node.inputs['Roughness'].default_value = metallic_value
                                print(f"    Установил дефолтное значение metallic {metallic_value} в roughness")
                            
                            node.inputs['Metallic'].default_value = 0.0
            
            self.bake_texture(context, low_poly, high_poly_objects, metallic_img, 'ROUGHNESS', material_index, max_ray_distance=self.max_ray_distance, extrusion=self.extrusion, simple_mode=simple_mode)
            print(f"✅ Metallic запечен через roughness")
        
        finally:
            for mat_data in original_states:
                mat = mat_data[0]
                nodes_data = mat_data[1]
                
                for node_data in nodes_data:
                    node = node_data[0]
                    metallic_value = node_data[1]
                    roughness_value = node_data[2]
                    metallic_links = node_data[3]
                    roughness_links = node_data[4]
                    
                    node.inputs['Metallic'].default_value = metallic_value
                    node.inputs['Roughness'].default_value = roughness_value
                    
                    for link in list(mat.node_tree.links):
                        if link.to_socket == node.inputs['Metallic'] or link.to_socket == node.inputs['Roughness']:
                            mat.node_tree.links.remove(link)
                    
                    for link_data in metallic_links:
                        from_node, from_socket = link_data
                        mat.node_tree.links.new(from_socket, node.inputs['Metallic'])
                    
                    for link_data in roughness_links:
                        from_node, from_socket = link_data
                        mat.node_tree.links.new(from_socket, node.inputs['Roughness'])
            
            print(f"✅ Исходное состояние восстановлено")
    
    def bake_emission_strength(self, context, low_poly, high_poly_objects, emit_img, material_index, simple_mode=False):
        """Запекает emission strength, переподключив входы emission strength к roughness"""
        original_states = []
        processed_materials = set()  # Отслеживаем уже обработанные материалы
        
        print(f"\n🔄 === ЗАПЕКАНИЕ EMISSION STRENGTH ===")
        print(f"Переподключаем входы emission strength к roughness для запекания")
        
        try:
            if simple_mode:
                print("  🔄 Простой режим: работаем с активным материалом активного объекта")
                # В простом режиме работаем с активным материалом активного объекта
                low_poly_material = low_poly.material_slots[material_index].material
                if low_poly_material and low_poly_material.use_nodes:
                    processed_materials.add(low_poly_material.name)
                    print(f"  🔄 Обрабатываем активный материал: {low_poly_material.name}")
                    
                    nodes_data = []
                    for node in low_poly_material.node_tree.nodes:
                        if node.type == 'BSDF_PRINCIPLED':
                            emission_strength_value = node.inputs['Emission Strength'].default_value
                            roughness_value = node.inputs['Roughness'].default_value
                            
                            emission_strength_links = []
                            for link in low_poly_material.node_tree.links:
                                if link.to_socket == node.inputs['Emission Strength']:
                                    emission_strength_links.append((link.from_node, link.from_socket))
                            
                            roughness_links = []
                            for link in low_poly_material.node_tree.links:
                                if link.to_socket == node.inputs['Roughness']:
                                    roughness_links.append((link.from_node, link.from_socket))
                            
                            nodes_data.append((node, emission_strength_value, roughness_value, emission_strength_links, roughness_links))
                    
                    original_states.append((low_poly_material, nodes_data))
                    
                    for node_data in nodes_data:
                        node = node_data[0]
                        emission_strength_value = node_data[1]
                        emission_strength_links = node_data[3]
                        
                        for link in list(low_poly_material.node_tree.links):
                            if link.to_socket == node.inputs['Roughness']:
                                low_poly_material.node_tree.links.remove(link)
                        
                        for link in list(low_poly_material.node_tree.links):
                            if link.to_socket == node.inputs['Emission Strength']:
                                low_poly_material.node_tree.links.remove(link)
                        
                        for link_data in emission_strength_links:
                            from_node, from_socket = link_data
                            low_poly_material.node_tree.links.new(from_socket, node.inputs['Roughness'])
                            print(f"    Переподключил {from_node.name}:{from_socket.name} -> Roughness")
                        
                        if not emission_strength_links:
                            node.inputs['Roughness'].default_value = emission_strength_value
                            print(f"    Установил дефолтное значение emission strength {emission_strength_value} в roughness")
                        
                        node.inputs['Emission Strength'].default_value = 0.0
            else:
                for high_poly in high_poly_objects:
                    for mat_slot in high_poly.material_slots:
                        if not mat_slot.material:
                            continue
                            
                        mat = mat_slot.material
                        if not mat.use_nodes:
                            continue
                        
                        # Пропускаем уже обработанные материалы
                        if mat.name in processed_materials:
                            print(f"  ⚠️ Материал {mat.name} уже обработан, пропускаем")
                            continue
                        
                        processed_materials.add(mat.name)
                        print(f"  🔄 Обрабатываем материал: {mat.name}")
                            
                        nodes_data = []
                        for node in mat.node_tree.nodes:
                            if node.type == 'BSDF_PRINCIPLED':
                                emission_strength_value = node.inputs['Emission Strength'].default_value
                                roughness_value = node.inputs['Roughness'].default_value
                                
                                emission_strength_links = []
                                for link in mat.node_tree.links:
                                    if link.to_socket == node.inputs['Emission Strength']:
                                        emission_strength_links.append((link.from_node, link.from_socket))
                                
                                roughness_links = []
                                for link in mat.node_tree.links:
                                    if link.to_socket == node.inputs['Roughness']:
                                        roughness_links.append((link.from_node, link.from_socket))
                                
                                nodes_data.append((node, emission_strength_value, roughness_value, emission_strength_links, roughness_links))
                        
                        original_states.append((mat, nodes_data))
                        
                        for node_data in nodes_data:
                            node = node_data[0]
                            emission_strength_value = node_data[1]
                            emission_strength_links = node_data[3]
                            
                            for link in list(mat.node_tree.links):
                                if link.to_socket == node.inputs['Roughness']:
                                    mat.node_tree.links.remove(link)
                            
                            for link in list(mat.node_tree.links):
                                if link.to_socket == node.inputs['Emission Strength']:
                                    mat.node_tree.links.remove(link)
                            
                            for link_data in emission_strength_links:
                                from_node, from_socket = link_data
                                mat.node_tree.links.new(from_socket, node.inputs['Roughness'])
                                print(f"    Переподключил {from_node.name}:{from_socket.name} -> Roughness")
                            
                            if not emission_strength_links:
                                node.inputs['Roughness'].default_value = emission_strength_value
                                print(f"    Установил дефолтное значение emission strength {emission_strength_value} в roughness")
                            
                            node.inputs['Emission Strength'].default_value = 0.0
            
            self.bake_texture(context, low_poly, high_poly_objects, emit_img, 'ROUGHNESS', material_index, max_ray_distance=self.max_ray_distance, extrusion=self.extrusion, simple_mode=simple_mode)
            print(f"✅ Emission strength запечен через roughness")
        
        finally:
            for mat_data in original_states:
                mat = mat_data[0]
                nodes_data = mat_data[1]
                
                for node_data in nodes_data:
                    node = node_data[0]
                    emission_strength_value = node_data[1]
                    roughness_value = node_data[2]
                    emission_strength_links = node_data[3]
                    roughness_links = node_data[4]
                    
                    node.inputs['Emission Strength'].default_value = emission_strength_value
                    node.inputs['Roughness'].default_value = roughness_value
                    
                    for link in list(mat.node_tree.links):
                        if link.to_socket == node.inputs['Emission Strength'] or link.to_socket == node.inputs['Roughness']:
                            mat.node_tree.links.remove(link)
                    
                    for link_data in emission_strength_links:
                        from_node, from_socket = link_data
                        mat.node_tree.links.new(from_socket, node.inputs['Emission Strength'])
                    
                    for link_data in roughness_links:
                        from_node, from_socket = link_data
                        mat.node_tree.links.new(from_socket, node.inputs['Roughness'])
            
            print(f"✅ Исходное состояние восстановлено")

    def create_erm_texture(self, emit_img, roughness_img, metallic_img, erm_img, material_output_path):
        """Создает ERM текстуру из отдельных карт Emission, Roughness и Metallic, загруженных с диска"""

        material_name = erm_img.name.replace("T_", "").replace("_ERM", "")

        emit_path = os.path.join(material_output_path, f"T_{material_name}_EMIT.png")
        roughness_path = os.path.join(material_output_path, f"T_{material_name}_ROUGHNESS.png")
        metallic_path = os.path.join(material_output_path, f"T_{material_name}_METALLIC.png")
        
        print(f"Загружаем файлы:")
        
        def load_texture_file(filepath, name):
             """Загружает текстуру с диска для обработки"""
             if os.path.exists(filepath):
                 try:
                     temp_img = bpy.data.images.load(filepath)
                     
                     temp_img.filepath = filepath
                     
                     temp_img.colorspace_settings.name = 'Non-Color'  
                     
                     temp_img.update()
                     _ = temp_img.pixels[0]  
                     
                     print(f"✅ Загружена {name}: {temp_img.size[0]}x{temp_img.size[1]}")
                     return temp_img
                 except Exception as e:
                     print(f"❌ Ошибка загрузки {name}: {e}")
                     return None
             else:
                 print(f"⚠️  Файл не найден: {filepath}")
                 return None
        
        emit_file = load_texture_file(emit_path, "EMIT")
        roughness_file = load_texture_file(roughness_path, "ROUGHNESS")
        metallic_file = load_texture_file(metallic_path, "METALLIC")
        
        if not (emit_file and roughness_file and metallic_file):
            print("❌ Не удалось загрузить все файлы для ERM")
            return None
        
        width = emit_file.size[0]
        height = emit_file.size[1]
        
        print(f"Создаем ERM текстуру {width}x{height}")
        
        try:
            emit_array = np.array(emit_file.pixels[:]).reshape(height, width, 4)
            roughness_array = np.array(roughness_file.pixels[:]).reshape(height, width, 4)
            metallic_array = np.array(metallic_file.pixels[:]).reshape(height, width, 4)
            
            
            erm_array = np.zeros((height, width, 4), dtype=np.float32)
            
            erm_array[:, :, 0] = emit_array[:, :, 0]        # R канал = Emission (красный канал)
            erm_array[:, :, 1] = roughness_array[:, :, 0]   # G канал = Roughness (красный канал)
            erm_array[:, :, 2] = metallic_array[:, :, 0]    # B канал = Metallic (красный канал)
            erm_array[:, :, 3] = 1.0                        # A канал = всегда 1.0
            
            min_r = np.min(erm_array[:, :, 0])
            max_r = np.max(erm_array[:, :, 0])
            min_g = np.min(erm_array[:, :, 1])
            max_g = np.max(erm_array[:, :, 1])
            min_b = np.min(erm_array[:, :, 2])
            max_b = np.max(erm_array[:, :, 2])
            
            erm_img.pixels = erm_array.flatten().tolist()
            erm_img.update()
            
            print(f"✅ Создана ERM текстура {width}x{height} из файлов с диска")
            
        except Exception as e:
            print(f"❌ Ошибка при создании ERM текстуры: {e}")
        
        finally:
            try:
                if emit_file and emit_file.name in bpy.data.images:
                    bpy.data.images.remove(emit_file)
                if roughness_file and roughness_file.name in bpy.data.images:
                    bpy.data.images.remove(roughness_file)
                if metallic_file and metallic_file.name in bpy.data.images:
                    bpy.data.images.remove(metallic_file)
            except:
                pass
        
        print(f"=" * 50)
        return erm_img
   
    def save_texture(self, image, filepath):
        """Сохраняет текстуру на диск"""
        image.update()
        
        scene = bpy.context.scene
        
        original_format = scene.render.image_settings.file_format
        original_color_mode = scene.render.image_settings.color_mode
        original_color_depth = scene.render.image_settings.color_depth
        original_view_settings = scene.view_settings.view_transform
        original_look = scene.view_settings.look
        original_display_device = scene.display_settings.display_device
        
        scene.render.image_settings.file_format = 'PNG'
        scene.render.image_settings.compression = 15
        
        if "DIFFUSE_OPACITY" in image.name:
            scene.render.image_settings.color_mode = 'RGBA'
            save_mode = 'RGBA'
        else:
            scene.render.image_settings.color_mode = 'RGB'
            save_mode = 'RGB'
            
        scene.render.image_settings.color_depth = '8'
        
        scene.view_settings.view_transform = 'Standard'
        scene.view_settings.look = 'None'
        scene.display_settings.display_device = 'sRGB'
        
        original_filepath = image.filepath
        image.filepath_raw = filepath
        try:
            image.save_render(filepath)
            print(f"Сохранена текстура: {filepath} (режим: {save_mode})")
        except Exception as e:
            print(f"Ошибка сохранения {filepath}: {e}")
            image.save_render(filepath, scene=scene)
            
        image.filepath = original_filepath
        
        scene.render.image_settings.file_format = original_format
        scene.render.image_settings.color_mode = original_color_mode
        scene.render.image_settings.color_depth = original_color_depth
        scene.view_settings.view_transform = original_view_settings
        scene.view_settings.look = original_look
        scene.display_settings.display_device = original_display_device
    
    def create_uv_mask(self, obj, texture_width, texture_height):
        """Создает маску пикселей, покрытых UV-развертками объекта"""
        import bmesh
        from mathutils import Vector
        
        uv_mask = np.zeros((texture_height, texture_width), dtype=bool)
        
        original_mode = obj.mode
        original_active = bpy.context.view_layer.objects.active
        original_selected = bpy.context.selected_objects[:]
        
        try:
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            
            if obj.mode != 'EDIT':
                bpy.ops.object.mode_set(mode='EDIT')
            
            bm = bmesh.from_edit_mesh(obj.data)
            
            if not bm.loops.layers.uv:
                print("⚠️ Нет UV слоя для создания маски")
                return np.ones((texture_height, texture_width), dtype=bool)  # Возвращаем полную маску
            
            uv_layer = bm.loops.layers.uv.active
            
            print(f"🗺️ Создание UV-маски для {obj.name} ({texture_width}x{texture_height})")
            
            face_count = 0
            for face in bm.faces:
                if len(face.loops) >= 3:
                    uv_coords = []
                    for loop in face.loops:
                        uv = loop[uv_layer].uv
                        pixel_x = int(uv.x * texture_width)
                        pixel_y = int((1.0 - uv.y) * texture_height)
                        uv_coords.append((pixel_x, pixel_y))
                    
                    self.rasterize_polygon(uv_mask, uv_coords, texture_width, texture_height)
                    face_count += 1
            
            covered_pixels = np.sum(uv_mask)
            total_pixels = texture_width * texture_height
            coverage_percent = (covered_pixels / total_pixels) * 100
            
            
        except Exception as e:
            print(f"❌ Ошибка создания UV-маски: {e}")
            uv_mask = np.ones((texture_height, texture_width), dtype=bool)
        
        finally:
            try:
                if obj.mode != original_mode:
                    bpy.ops.object.mode_set(mode=original_mode)
                
                bpy.ops.object.select_all(action='DESELECT')
                for sel_obj in original_selected:
                    if sel_obj:
                        sel_obj.select_set(True)
                bpy.context.view_layer.objects.active = original_active
                
            except Exception as e:
                print(f"⚠️ Ошибка восстановления состояния: {e}")
        
        return uv_mask
    
    def rasterize_polygon(self, mask, uv_coords, width, height):
        """Растеризует полигон в маску"""
        if len(uv_coords) < 3:
            return
        
        min_x = max(0, min(coord[0] for coord in uv_coords))
        max_x = min(width - 1, max(coord[0] for coord in uv_coords))
        min_y = max(0, min(coord[1] for coord in uv_coords))
        max_y = min(height - 1, max(coord[1] for coord in uv_coords))
        
        for y in range(min_y, max_y + 1):
            for x in range(min_x, max_x + 1):
                if self.point_in_polygon(x, y, uv_coords):
                    if 0 <= y < height and 0 <= x < width:
                        mask[y, x] = True
    
    def point_in_polygon(self, x, y, polygon):
        """Проверяет, находится ли точка внутри полигона (ray casting algorithm)"""
        n = len(polygon)
        inside = False
        
        p1x, p1y = polygon[0]
        for i in range(1, n + 1):
            p2x, p2y = polygon[i % n]
            if y > min(p1y, p2y):
                if y <= max(p1y, p2y):
                    if x <= max(p1x, p2x):
                        if p1y != p2y:
                            xinters = (y - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
                        if p1x == p2x or x <= xinters:
                            inside = not inside
            p1x, p1y = p2x, p2y
        
        return inside
    
    def extract_opacity(self, diffuse_opacity_img, opacity_img, low_poly_obj, material_output_path):
        """Extract opacity from alpha channel of diffuse_opacity image, загруженного с диска
        Возвращает: True если была применена автозаливка белым цветом, False иначе"""

        material_name = opacity_img.name.replace("T_", "").replace("_OPACITY", "")

        diffuse_opacity_path = os.path.join(material_output_path, f"T_{material_name}_DIFFUSE_OPACITY.png")
        
        print(f"Загружаем файл: {diffuse_opacity_path}")
        
        try:
            diffuse_opacity_file = bpy.data.images.load(diffuse_opacity_path)
            
            diffuse_opacity_file.filepath = diffuse_opacity_path
            
            diffuse_opacity_file.colorspace_settings.name = 'sRGB'  
            
            diffuse_opacity_file.update()
            _ = diffuse_opacity_file.pixels[0]
            
            width = diffuse_opacity_file.size[0]
            height = diffuse_opacity_file.size[1]
            
            
            diffuse_opacity_array = np.array(diffuse_opacity_file.pixels[:]).reshape(height, width, 4)
            
            
            alpha_channel = diffuse_opacity_array[..., 3]
            
            total_pixels = width * height
            
            alpha_mask = diffuse_opacity_array[..., 3] > 0.5
            
            opacity_array = np.zeros((height, width, 4), dtype=np.float32)
            
            opacity_value = np.where(alpha_mask, 1.0, 0.0)
            
            opacity_array[..., 0] = opacity_value  # R канал
            opacity_array[..., 1] = opacity_value  # G канал  
            opacity_array[..., 2] = opacity_value  # B канал
            opacity_array[..., 3] = 1.0            # A канал
            
            uv_mask = self.create_uv_mask(low_poly_obj, width, height)
            
            uv_opacity_values = opacity_value[uv_mask]
            uv_transparent_pixels = np.sum(uv_opacity_values == 0.0)
            uv_opaque_pixels = np.sum(uv_opacity_values == 1.0)
            uv_total_pixels = len(uv_opacity_values)
            
            total_transparent_pixels = np.sum(opacity_value == 0.0)
            total_opaque_pixels = np.sum(opacity_value == 1.0)
            
            uv_white_percentage = (uv_opaque_pixels / uv_total_pixels * 100) if uv_total_pixels > 0 else 0
            total_white_percentage = (total_opaque_pixels / total_pixels) * 100
            
            print(f"\n📋 РЕЗУЛЬТАТ ИЗВЛЕЧЕНИЯ С ДИСКА:")
            print(f"  🌍 ОБЩАЯ СТАТИСТИКА:")
            print(f"    Прозрачных пикселей (черные): {total_transparent_pixels}")
            print(f"    Непрозрачных пикселей (белые): {total_opaque_pixels}")
            print(f"    Всего пикселей: {total_pixels}")
            print(f"    Процент непрозрачности: {total_white_percentage:.1f}%")
            print(f"  🗺️  UV-ОБЛАСТЬ:")
            print(f"    Прозрачных пикселей в UV: {uv_transparent_pixels}")
            print(f"    Непрозрачных пикселей в UV: {uv_opaque_pixels}")
            print(f"    Всего пикселей в UV: {uv_total_pixels}")
            print(f"    Процент непрозрачности в UV: {uv_white_percentage:.1f}%")
            
            is_auto_filled = False
            if uv_white_percentage >= 98.0:
                print(f"\n🎯 АВТОЗАЛИВКА: {uv_white_percentage:.1f}% белых пикселей в UV-области >= 98%, заливаем карту полностью белым!")
                opacity_array[..., 0] = 1.0  # R канал
                opacity_array[..., 1] = 1.0  # G канал  
                opacity_array[..., 2] = 1.0  # B канал
                opacity_array[..., 3] = 1.0  # A канал
                print(f"✅ Карта прозрачности залита чистым белым цветом")
                is_auto_filled = True
            else:
                print(f"ℹ️  Автозаливка не применена: {uv_white_percentage:.1f}% < 98%")
            
            opacity_img.pixels = opacity_array.flatten().tolist()
            opacity_img.update()
            
            if uv_transparent_pixels == 0 and uv_white_percentage < 98.0:
                print(f"\n⚠️  ПРОБЛЕМА: Нет прозрачных пикселей в UV-области!")
            
            print(f"=" * 50)
            
            if diffuse_opacity_file.name in bpy.data.images:
                bpy.data.images.remove(diffuse_opacity_file)
            
            return is_auto_filled
                
        except Exception as e:
            print(f"❌ Ошибка при извлечении прозрачности: {e}")
            width = opacity_img.size[0]
            height = opacity_img.size[1]
            pixels = [1.0, 1.0, 1.0, 1.0] * (width * height)
            opacity_img.pixels = pixels
            opacity_img.update()
            return False

    def connect_textures_to_material(self, material, diffuse_img, erm_img, normal_img, opacity_img, connection_mode, normal_type, material_output_path):
        """Подключает созданные текстуры к материалу"""
        material_name = material.name

        textures_to_remove = [
            f"T_{material_name}_DIFFUSE",
            f"T_{material_name}_ERM",
            f"T_{material_name}_NORMAL",
            f"T_{material_name}_NORMAL_DIRECTX",
            f"T_{material_name}_OPACITY",
            f"T_{material_name}_ROUGHNESS",
            f"T_{material_name}_METALLIC",
            f"T_{material_name}_EMIT",
            f"T_{material_name}_DIFFUSE_OPACITY"
        ]

        for texture_name in textures_to_remove:
            if texture_name in bpy.data.images:
                bpy.data.images.remove(bpy.data.images[texture_name])

        for img_name in list(bpy.data.images.keys()):
            if any(tex_name in img_name for tex_name in textures_to_remove):
                bpy.data.images.remove(bpy.data.images[img_name])

        "bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)"
        print(f"✅ Очистка памяти завершена")

        material.use_nodes = True
        nodes = material.node_tree.nodes
        links = material.node_tree.links

        nodes.clear()

        output = nodes.new(type='ShaderNodeOutputMaterial')
        bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')

        output.location = (400, 0)
        bsdf.location = (100, 0)

        links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])

        def load_texture_from_disk(texture_name, label, location, colorspace='sRGB'):
             """Загружает текстуру с диска и создает узел"""
             texture_path = os.path.join(material_output_path, f"{texture_name}.png")
             
             if os.path.exists(texture_path):
                 try:
                     if texture_name in bpy.data.images:
                         bpy.data.images.remove(bpy.data.images[texture_name])
                     
                     for img_name in list(bpy.data.images.keys()):
                         if texture_name in img_name:
                             bpy.data.images.remove(bpy.data.images[img_name])
                     
                     "bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)"
                     
                     img = bpy.data.images.load(texture_path)
                     
                     img.name = texture_name
                     
                     img.filepath = texture_path
                     img.filepath_raw = texture_path
                     
                     img.colorspace_settings.name = colorspace
                     
                     img.reload()
                     img.update()
                     _ = img.pixels[0]
                     
                     if img.has_data:
                         pass
                             
                     else:
                         img.reload()
                         img.update()
                         _ = img.pixels[0]
                         if not img.has_data:
                             return None
                     
                     tex_node = nodes.new(type='ShaderNodeTexImage')
                     tex_node.image = img
                     tex_node.location = location
                     tex_node.label = label
                     
                     nodes.active = tex_node
                     
                     return tex_node
                     
                 except Exception as e:
                     print(f"❌ Ошибка загрузки текстуры {label}: {e}")
                     return None
             else:
                 print(f"⚠️  Файл текстуры не найден: {texture_path}")
                 return None
        
        if connection_mode == 'HIGH':
            print(f"🔧 Режим подключения: HIGH (ERM + DIFFUSE_OPACITY)")
            
            tex_diffuse_opacity = load_texture_from_disk(f"T_{material_name}_DIFFUSE_OPACITY", "Diffuse Opacity", (-700, 300), 'sRGB')
            if tex_diffuse_opacity:
                links.new(tex_diffuse_opacity.outputs['Color'], bsdf.inputs['Base Color'])
                links.new(tex_diffuse_opacity.outputs['Color'], bsdf.inputs['Emission Color'])
                links.new(tex_diffuse_opacity.outputs['Alpha'], bsdf.inputs['Alpha'])
            
            if normal_type == 'DIRECTX':
                normal_texture_name = f"T_{material_name}_NORMAL_DIRECTX"
            else:
                normal_texture_name = f"T_{material_name}_NORMAL"
                
            tex_normal = load_texture_from_disk(normal_texture_name, "Normal", (-700, 0), 'Non-Color')
            if tex_normal:
                connect_normal_map(nodes, links, tex_normal, bsdf, normal_type, (-400, 0))
            
            tex_erm = load_texture_from_disk(f"T_{material_name}_ERM", "ERM", (-700, -300), 'Non-Color')
            if tex_erm:
                separate_color = nodes.new(type='ShaderNodeSeparateColor')
                separate_color.location = (-400, -300)
                
                links.new(tex_erm.outputs['Color'], separate_color.inputs['Color'])

                links.new(separate_color.outputs['Red'], bsdf.inputs['Emission Strength'])
                links.new(separate_color.outputs['Green'], bsdf.inputs['Roughness'])
                links.new(separate_color.outputs['Blue'], bsdf.inputs['Metallic'])
                
        elif connection_mode == 'LOW':
            print(f"🔧 Режим подключения: LOW (отдельные карты)")
            
            tex_diffuse = load_texture_from_disk(f"T_{material_name}_DIFFUSE", "Diffuse", (-700, 400), 'sRGB')
            if tex_diffuse:
                links.new(tex_diffuse.outputs['Color'], bsdf.inputs['Base Color'])
            
            tex_metallic = load_texture_from_disk(f"T_{material_name}_METALLIC", "Metallic", (-700, 200), 'Non-Color')
            if tex_metallic:
                links.new(tex_metallic.outputs['Color'], bsdf.inputs['Metallic'])
            
            tex_roughness = load_texture_from_disk(f"T_{material_name}_ROUGHNESS", "Roughness", (-700, 0), 'Non-Color')
            if tex_roughness:
                links.new(tex_roughness.outputs['Color'], bsdf.inputs['Roughness'])
            
            tex_opacity = load_texture_from_disk(f"T_{material_name}_OPACITY", "Opacity", (-700, -200), 'Non-Color')
            if tex_opacity:
                links.new(tex_opacity.outputs['Color'], bsdf.inputs['Alpha'])
            
            if normal_type == 'DIRECTX':
                normal_texture_name = f"T_{material_name}_NORMAL_DIRECTX"
            else:
                normal_texture_name = f"T_{material_name}_NORMAL"
                
            tex_normal = load_texture_from_disk(normal_texture_name, "Normal", (-700, -400), 'Non-Color')
            if tex_normal:
                connect_normal_map(nodes, links, tex_normal, bsdf, normal_type, (-400, -400))
        
        material.blend_method = 'HASHED'
        material.shadow_method = 'HASHED'
        material.use_backface_culling = False
        
        bsdf.inputs['Base Color'].default_value = (1.0, 1.0, 1.0, 1.0)
        bsdf.inputs['Metallic'].default_value = 0.0
        bsdf.inputs['Roughness'].default_value = 0.8
        bsdf.inputs['IOR'].default_value = 1.5
        bsdf.inputs['Alpha'].default_value = 1.0
        bsdf.inputs['Emission Color'].default_value = (0.0, 0.0, 0.0, 1.0)
        bsdf.inputs['Emission Strength'].default_value = 0
        
        bpy.context.view_layer.update()
        
        for area in bpy.context.screen.areas:
            area.tag_redraw()
            
        material.node_tree.update_tag()
        
        print(f"✅ Текстуры загружены с диска и подключены к материалу {material.name} в режиме {connection_mode}")
        
        return material

    def disable_metallic_for_diffuse_baking(self, high_poly_objects, simple_mode=False, low_poly=None, material_index=0):
        """Временно отключает металлик на хайполи объектах для корректного запекания диффузных текстур"""
        original_states = []
        processed_materials = set()  # Отслеживаем уже обработанные материалы
        
        print(f"\n🔄 === ОТКЛЮЧЕНИЕ METALLIC ДЛЯ DIFFUSE ===")
        
        if simple_mode:
            print("  🔄 Простой режим: отключаем metallic для активного материала активного объекта")
            # В простом режиме работаем с активным материалом активного объекта
            if low_poly and len(low_poly.material_slots) > material_index:
                low_poly_material = low_poly.material_slots[material_index].material
                if low_poly_material and low_poly_material.use_nodes:
                    processed_materials.add(low_poly_material.name)
                    print(f"  🔄 Отключаем metallic для активного материала: {low_poly_material.name}")
                    
                    nodes_data = []
                    for node in low_poly_material.node_tree.nodes:
                        if node.type == 'BSDF_PRINCIPLED':
                            metallic_value = node.inputs['Metallic'].default_value
                            ior_value = node.inputs['IOR'].default_value
                            metallic_links = []
                            ior_links = []
                            
                            for link in low_poly_material.node_tree.links:
                                if link.to_socket == node.inputs['Metallic']:
                                    metallic_links.append((link.from_node, link.from_socket))
                                elif link.to_socket == node.inputs['IOR']:
                                    ior_links.append((link.from_node, link.from_socket))
                            
                            nodes_data.append((node, metallic_value, ior_value, metallic_links, ior_links))
                    
                    original_states.append((low_poly_material, nodes_data))
                    
                    for node_data in nodes_data:
                        node = node_data[0]
                        
                        for link in list(low_poly_material.node_tree.links):
                            if link.to_socket == node.inputs['Metallic']:
                                low_poly_material.node_tree.links.remove(link)
                            elif link.to_socket == node.inputs['IOR']:
                                low_poly_material.node_tree.links.remove(link)
                        
                        node.inputs['Metallic'].default_value = 0.0
                        node.inputs['IOR'].default_value = 1.0
                        print(f"    Отключил metallic и установил IOR=1.0 для {low_poly_material.name}")
            
            return original_states
        
        for high_poly in high_poly_objects:
            for mat_slot in high_poly.material_slots:
                if not mat_slot.material:
                    continue
                    
                mat = mat_slot.material
                if not mat.use_nodes:
                    continue
                
                # Пропускаем уже обработанные материалы
                if mat.name in processed_materials:
                    print(f"  ⚠️ Материал {mat.name} уже обработан, пропускаем")
                    continue
                
                processed_materials.add(mat.name)
                print(f"  🔄 Отключаем metallic для материала: {mat.name}")
                    
                nodes_data = []
                for node in mat.node_tree.nodes:
                    if node.type == 'BSDF_PRINCIPLED':
                        metallic_value = node.inputs['Metallic'].default_value
                        ior_value = node.inputs['IOR'].default_value
                        metallic_links = []
                        ior_links = []
                        
                        for link in mat.node_tree.links:
                            if link.to_socket == node.inputs['Metallic']:
                                metallic_links.append((link.from_node, link.from_socket))
                            elif link.to_socket == node.inputs['IOR']:
                                ior_links.append((link.from_node, link.from_socket))
                        
                        nodes_data.append((node, metallic_value, ior_value, metallic_links, ior_links))
                
                original_states.append((mat, nodes_data))
                
                for node_data in nodes_data:
                    node = node_data[0]
                    
                    for link in list(mat.node_tree.links):
                        if link.to_socket == node.inputs['Metallic']:
                            mat.node_tree.links.remove(link)
                        elif link.to_socket == node.inputs['IOR']:
                            mat.node_tree.links.remove(link)
                    
                    node.inputs['Metallic'].default_value = 0.0
                    node.inputs['IOR'].default_value = 1.0  
        
        return original_states
    
    def restore_material_states(self, original_states):
        """Восстанавливает исходное состояние материалов"""
        for mat_data in original_states:
            mat = mat_data[0]
            nodes_data = mat_data[1]
            
            for node_data in nodes_data:
                node = node_data[0]
                metallic_value = node_data[1]
                ior_value = node_data[2]
                metallic_links = node_data[3]
                ior_links = node_data[4]
                
                node.inputs['Metallic'].default_value = metallic_value
                node.inputs['IOR'].default_value = ior_value
                
                for link_data in metallic_links:
                    from_node, from_socket = link_data
                    mat.node_tree.links.new(from_socket, node.inputs['Metallic'])
                
                for link_data in ior_links:
                    from_node, from_socket = link_data
                    mat.node_tree.links.new(from_socket, node.inputs['IOR'])

    def convert_normal_to_directx(self, image):
        """Инвертирует зеленый канал в нормали DirectX"""
        try:
            width = image.size[0]
            height = image.size[1]
            
            image.update()
            _ = image.pixels[0]
            
            
            pixels = np.array(image.pixels[:]).reshape(height, width, 4)
            
            green_min = np.min(pixels[:, :, 1])
            green_max = np.max(pixels[:, :, 1])
            
            pixels[:, :, 1] = 1.0 - pixels[:, :, 1]
            
            green_min_after = np.min(pixels[:, :, 1])
            green_max_after = np.max(pixels[:, :, 1])
            
            image.pixels = pixels.flatten().tolist()
            image.update()
            print(f"✅ Зеленый канал инвертирован в текстуре {image.name}")
            
        except Exception as e:
            print(f"❌ Ошибка при конвертации нормали в DirectX: {e}")

    def save_texture_set_info(self, context, low_poly, material_name, resolution):
        """Сохраняет информацию о созданном наборе текстур в коллекцию (старый метод)"""
        self.save_texture_set_info_with_path(context, low_poly, material_name, resolution, self.output_path)
    
    def save_texture_set_info_with_path(self, context, low_poly, material_name, resolution, output_path):
        """Сохраняет информацию о созданном наборе текстур в коллекцию с указанным путем"""
        texture_sets = context.scene.baker_texture_sets
        
        existing_set = None
        for tex_set in texture_sets:
            if tex_set.name == f"T_{material_name}":
                existing_set = tex_set
                break
        
        if existing_set:
            tex_set = existing_set
        else:
            tex_set = texture_sets.add()
        
        tex_set.name = f"T_{material_name}"
        tex_set.material_name = material_name
        tex_set.object_name = low_poly.name
        tex_set.resolution = resolution
        tex_set.output_path = output_path
        
        base_path = os.path.join(output_path, f"T_{material_name}")
        tex_set.has_diffuse = os.path.exists(f"{base_path}_DIFFUSE.png")
        tex_set.has_diffuse_opacity = os.path.exists(f"{base_path}_DIFFUSE_OPACITY.png")
        tex_set.has_normal = os.path.exists(f"{base_path}_NORMAL.png")
        tex_set.has_normal_directx = os.path.exists(f"{base_path}_NORMAL_DIRECTX.png")
        tex_set.has_roughness = os.path.exists(f"{base_path}_ROUGHNESS.png")
        tex_set.has_metallic = os.path.exists(f"{base_path}_METALLIC.png")
        tex_set.has_emit = os.path.exists(f"{base_path}_EMIT.png")
        tex_set.has_opacity = os.path.exists(f"{base_path}_OPACITY.png")
        tex_set.has_erm = os.path.exists(f"{base_path}_ERM.png")
        
        print(f"💾 Сохранена информация о наборе текстур: {tex_set.name}")

    def refresh_texture_sets_list(self, context):
        """Вспомогательная функция для очистки списка наборов текстур и добавления новых из OBJECT_BAKED"""
        texture_sets = context.scene.baker_texture_sets
        indices_to_remove = []

        for i, tex_set in enumerate(texture_sets):
            if not os.path.exists(tex_set.output_path):
                indices_to_remove.append(i)
                continue

            base_path = os.path.join(tex_set.output_path, tex_set.name)
            has_any_texture = any(os.path.exists(f"{base_path}_{suffix}.png")
                                for suffix in ["DIFFUSE", "DIFFUSE_OPACITY", "NORMAL", "NORMAL_DIRECTX",
                                             "ROUGHNESS", "METALLIC", "EMIT", "OPACITY", "ERM"])

            if not has_any_texture:
                indices_to_remove.append(i)

        for i in reversed(indices_to_remove):
            texture_sets.remove(i)

        # Сканируем папку OBJECT_BAKED и добавляем новые наборы
        scan_object_baked_folder(context)

    def remove_conflicting_textures(self, context, material_name, main_baked_folder, operation_type):
        """Удаляет конфликтующие текстуры перед созданием новых

        Args:
            operation_type: 'bake' или 'generate'
        """
        print(f"🔄 Удаление конфликтующих текстур для материала '{material_name}'...")

        if operation_type == 'bake':
            # При запекании удаляем сгенерированные текстуры
            generated_path = os.path.join(main_baked_folder, f"{material_name}_generated")
            if os.path.exists(generated_path):
                try:
                    import shutil
                    shutil.rmtree(generated_path)
                    print(f"🗑️ Удалена папка сгенерированных текстур: {generated_path}")
                except Exception as e:
                    print(f"⚠️ Ошибка удаления папки {generated_path}: {e}")

            # Удаляем запись из списка текстурных наборов, если она указывает на generated
            texture_sets_to_remove = []
            for i, tex_set in enumerate(context.scene.baker_texture_sets):
                if tex_set.material_name == material_name and "_generated" in tex_set.output_path:
                    texture_sets_to_remove.append(i)
                    print(f"🗑️ Удалена запись о сгенерированных текстурах: {tex_set.name}")

            # Удаляем в обратном порядке, чтобы индексы оставались корректными
            for i in reversed(texture_sets_to_remove):
                context.scene.baker_texture_sets.remove(i)

        elif operation_type == 'generate':
            # При генерации удаляем запеченные текстуры
            baked_path = os.path.join(main_baked_folder, f"{material_name}_baked")
            if os.path.exists(baked_path):
                try:
                    import shutil
                    shutil.rmtree(baked_path)
                    print(f"🗑️ Удалена папка с запеченными текстурами: {baked_path}")
                except Exception as e:
                    print(f"⚠️ Ошибка удаления папки {baked_path}: {e}")

            # Удаляем запись из списка текстурных наборов, если она указывает на baked
            texture_sets_to_remove = []
            for i, tex_set in enumerate(context.scene.baker_texture_sets):
                if tex_set.material_name == material_name and "_baked" in tex_set.output_path:
                    texture_sets_to_remove.append(i)
                    print(f"🗑️ Удалена запись о запеченных текстурах: {tex_set.name}")

            # Удаляем в обратном порядке, чтобы индексы оставались корректными
            for i in reversed(texture_sets_to_remove):
                context.scene.baker_texture_sets.remove(i)


class BAKER_OT_create_atlas(Operator):
    """Создает атлас из выбранных наборов текстур"""
    bl_idname = "baker.create_atlas"
    bl_label = "Create Atlas"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return any(tex_set.is_selected_for_atlas for tex_set in context.scene.baker_texture_sets)
    
    def execute(self, context):
        scene = context.scene
        atlas_size = int(scene.baker_atlas_size)
        atlas_type = scene.baker_atlas_type
        
        selected_sets = [tex_set for tex_set in scene.baker_texture_sets if tex_set.is_selected_for_atlas]
        
        if not selected_sets:
            self.report({'ERROR'}, "Не выбрано ни одного набора текстур")
            return {'CANCELLED'}
        
        print(f"\n🔧 === СОЗДАНИЕ АТЛАСА ===")
        print(f"Размер: {atlas_size}x{atlas_size}")
        print(f"Тип: {atlas_type}")
        print(f"Выбранных наборов: {len(selected_sets)}")
        
        if not self.can_pack_textures(selected_sets, atlas_size):
            self.report({'ERROR'}, f"Невозможно упаковать {len(selected_sets)} наборов в атлас {atlas_size}x{atlas_size}")
            return {'CANCELLED'}
        
        atlas_data = self.create_atlas(context, selected_sets, atlas_size, atlas_type)
        
        if atlas_data:
            self.report({'INFO'}, f"Атлас создан: {atlas_data['name']}")
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, "Ошибка при создании атласа")
            return {'CANCELLED'}

    def can_pack_textures(self, texture_sets, atlas_size):
        """Проверяет, можно ли упаковать наборы текстур в атлас заданного размера"""
        total_area = sum(tex_set.resolution * tex_set.resolution for tex_set in texture_sets)
        atlas_area = atlas_size * atlas_size
        
        if total_area > atlas_area:
            return False
        
        try:
            layout = pack_atlas_rectangles(texture_sets, atlas_size)
            return layout is not None
        except:
            return False
    
    def create_atlas(self, context, texture_sets, atlas_size, atlas_type):
        """Создает атлас из выбранных наборов текстур"""
        
        if atlas_type == 'HIGH':
            texture_types = ['DIFFUSE_OPACITY', 'ERM', 'NORMAL']
        else:  # LOW
            texture_types = ['DIFFUSE', 'METALLIC', 'ROUGHNESS', 'OPACITY', 'NORMAL']
        
        # Проверяем, можно ли использовать новую логику именования для LOW атласов
        address, obj_type = try_get_active_object_info(context)
        use_new_naming = (atlas_type == 'LOW' and address is not None and obj_type is not None)
        
        if use_new_naming:
            atlas_name, material_name_new = get_atlas_names_low_format(address, obj_type, atlas_size, len(texture_sets))
            print(f"🏷️  Используется новый формат именования: {atlas_name}")
            print(f"🏷️  Активный объект: {context.active_object.name} -> Address: {address}, Type: {obj_type}")
        else:
            atlas_name = f"Atlas_{atlas_type}_{atlas_size}_{len(texture_sets)}sets"
            material_name_new = None
            if address is None:
                print(f"🏷️  Используется старый формат именования (активный объект не найден или не в формате SM_Address_Type)")
            else:
                print(f"🏷️  Используется старый формат именования (тип атласа: {atlas_type})")
        
        if texture_sets:
            base_output_path = os.path.dirname(texture_sets[0].output_path)
            
            # Ищем свободное имя папки с нумерацией
            counter = 1
            atlas_output_path = os.path.join(base_output_path, f"{atlas_name}_{counter}")
            while os.path.exists(atlas_output_path):
                counter += 1
                atlas_output_path = os.path.join(base_output_path, f"{atlas_name}_{counter}")
            
            # Обновляем имя атласа с учетом нумерации
            atlas_name = f"{atlas_name}_{counter}"
            
            if not os.path.exists(atlas_output_path):
                os.makedirs(atlas_output_path)
        else:
            self.report({'ERROR'}, "Не удалось определить путь для сохранения")
            return None
        
        layout = calculate_atlas_packing_layout(texture_sets, atlas_size)
        
        if not layout:
            self.report({'ERROR'}, "Не удалось рассчитать упаковку текстур")
            return None
        
        source_has_alpha = self.has_diffuse_opacity_alpha_channel(texture_sets, atlas_type)
        if source_has_alpha:
            print(f"🔍 Обнаружен альфаканал в исходных DIFFUSE_OPACITY картах для HIGH атласа")
        else:
            print(f"ℹ️ Альфаканал не обнаружен или тип атласа не HIGH")

        created_atlases = {}
        for texture_type in texture_types:
            atlas_image = self.create_atlas_for_type(texture_sets, texture_type, atlas_size, layout)
            
            if atlas_image:
                if use_new_naming:
                    atlas_filename = get_texture_filename_low_format(address, obj_type, texture_type)
                else:
                    atlas_filename = f"{atlas_name}_{texture_type}.png"
                
                atlas_filepath = os.path.join(atlas_output_path, atlas_filename)
                self.save_atlas_image(atlas_image, atlas_filepath, atlas_type, source_has_alpha)
                created_atlases[texture_type] = atlas_filepath
                print(f"✅ Создан атлас {texture_type}: {atlas_filename}")
        
        normal_type = context.scene.baker_normal_type
        atlas_material = self.create_atlas_material(context, atlas_name, created_atlases, atlas_type, normal_type, 
                                                   custom_material_name=material_name_new)
        
        affected_objects = self.apply_atlas_to_objects(context, texture_sets, atlas_material, layout)
        
        # Сохраняем манифест раскладки для последующего отката
        try:
            manifest = {
                'atlas_name': atlas_name,
                'atlas_type': atlas_type,
                'atlas_size': atlas_size,
                'created_atlases': created_atlases,
                'layout': [
                    {
                        'material_name': item['texture_set'].material_name,
                        'u_min': item['u_min'],
                        'v_min': item['v_min'],
                        'u_max': item['u_max'],
                        'v_max': item['v_max'],
                        'width': item['width'],
                        'height': item['height'],
                        'x': item['x'],
                        'y': item['y']
                    }
                    for item in layout
                ]
            }
            manifest_path = os.path.join(atlas_output_path, f"{atlas_name}_layout.json")
            with open(manifest_path, 'w', encoding='utf-8') as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
            print(f"💾 Сохранен манифест раскладки атласа: {manifest_path}")
        except Exception as e:
            print(f"⚠️ Не удалось сохранить манифест раскладки: {e}")

        atlas_data_entry = context.scene.baker_atlases.add()
        atlas_data_entry.name = atlas_name
        atlas_data_entry.atlas_type = atlas_type
        atlas_data_entry.atlas_size = atlas_size
        atlas_data_entry.output_path = atlas_output_path
        atlas_data_entry.texture_sets_count = len(texture_sets)
        
        return {
            'name': atlas_name,
            'atlases': created_atlases,
            'material': atlas_material,
            'affected_objects': affected_objects
        }
    
    
    def create_atlas_for_type(self, texture_sets, texture_type, atlas_size, layout):
        """Создает атлас для конкретного типа текстуры"""
        atlas_name = f"Atlas_{texture_type}_{atlas_size}"
        
        if atlas_name in bpy.data.images:
            bpy.data.images.remove(bpy.data.images[atlas_name])
        
        atlas_image = bpy.data.images.new(
            atlas_name,
            width=atlas_size,
            height=atlas_size,
            alpha=(texture_type == 'DIFFUSE_OPACITY'),
            float_buffer=False
        )
        
        if texture_type in ['DIFFUSE', 'DIFFUSE_OPACITY']:
            atlas_image.colorspace_settings.name = 'sRGB'
        else:
            atlas_image.colorspace_settings.name = 'Non-Color'
        
        if texture_type == 'DIFFUSE_OPACITY':
            fill = [0.0, 0.0, 0.0, 0.0]
        else:
            fill = [0.0, 0.0, 0.0, 1.0]
        total_px = atlas_size * atlas_size * 4
        buf = fill * (total_px // 4)
        try:
            atlas_image.pixels.foreach_set(buf)
        except Exception:
            atlas_image.pixels = buf
        
        atlas_array = np.array(atlas_image.pixels[:]).reshape(atlas_size, atlas_size, 4)
        
        for item in layout:
            texture_path = self.get_texture_path(item['texture_set'], texture_type)
            
            if texture_path and os.path.exists(texture_path):
                self.place_texture_in_atlas(atlas_array, texture_path, item)
        
        flat = atlas_array.flatten().tolist()
        try:
            atlas_image.pixels.foreach_set(flat)
        except Exception:
            atlas_image.pixels = flat
        atlas_image.update()
        
        return atlas_image
    
    def get_texture_path(self, texture_set, texture_type):
        """Получает путь к файлу текстуры заданного типа"""
        base_path = os.path.join(texture_set.output_path, texture_set.name)
        
        if texture_type == 'DIFFUSE':
            if texture_set.has_diffuse:
                return f"{base_path}_DIFFUSE.png"
        elif texture_type == 'DIFFUSE_OPACITY':
            if texture_set.has_diffuse_opacity:
                return f"{base_path}_DIFFUSE_OPACITY.png"
        elif texture_type == 'NORMAL':
            if texture_set.has_normal:
                return f"{base_path}_NORMAL.png"
            elif texture_set.has_normal_directx:
                return f"{base_path}_NORMAL_DIRECTX.png"
        elif texture_type == 'METALLIC':
            if texture_set.has_metallic:
                return f"{base_path}_METALLIC.png"
        elif texture_type == 'ROUGHNESS':
            if texture_set.has_roughness:
                return f"{base_path}_ROUGHNESS.png"
        elif texture_type == 'OPACITY':
            if texture_set.has_opacity:
                return f"{base_path}_OPACITY.png"
        elif texture_type == 'ERM':
            if texture_set.has_erm:
                return f"{base_path}_ERM.png"
        
        return None
    
    def place_texture_in_atlas(self, atlas_array, texture_path, layout_item):
        """Размещает текстуру в атласе с масштабированием при необходимости"""
        try:
            temp_img = bpy.data.images.load(texture_path)
            temp_img.update()
            _ = temp_img.pixels[0]
            
            tex_width = temp_img.size[0]
            tex_height = temp_img.size[1]
            tex_array = np.array(temp_img.pixels[:]).reshape(tex_height, tex_width, 4)
            
            cell_width = layout_item['width']
            cell_height = layout_item['height']
            
            if tex_width != cell_width or tex_height != cell_height:
                print(f"🔧 Масштабирование текстуры {os.path.basename(texture_path)}: "
                      f"{tex_width}x{tex_height} → {cell_width}x{cell_height}")
                
                # Масштабируем текстуру до размера ячейки
                if SCIPY_AVAILABLE:
                    # Используем scipy для более качественного ресайза
                    scale_x = cell_width / tex_width
                    scale_y = cell_height / tex_height
                    tex_array = ndimage.zoom(tex_array, (scale_y, scale_x, 1), order=1)
                    print(f"🔬 Качественный ресайз через SciPy")
                else:
                    # Fallback: простое ближайшее соседство через numpy
                    print("⚠️ Простое масштабирование через numpy")
                    indices_y = np.round(np.linspace(0, tex_height - 1, cell_height)).astype(int)
                    indices_x = np.round(np.linspace(0, tex_width - 1, cell_width)).astype(int)
                    tex_array = tex_array[np.ix_(indices_y, indices_x)]
                
                # Убеждаемся, что размеры точно соответствуют ячейке
                if tex_array.shape[0] != cell_height or tex_array.shape[1] != cell_width:
                    # Если размеры все еще не совпадают, обрезаем или дополняем
                    final_array = np.zeros((cell_height, cell_width, 4), dtype=tex_array.dtype)
                    min_h = min(tex_array.shape[0], cell_height)
                    min_w = min(tex_array.shape[1], cell_width)
                    final_array[:min_h, :min_w] = tex_array[:min_h, :min_w]
                    tex_array = final_array
            
            x = layout_item['x']
            y = layout_item['y']
            
            atlas_array[y:y+cell_height, x:x+cell_width, :] = tex_array
            
            if tex_width != cell_width or tex_height != cell_height:
                print(f"📍 Размещена и масштабирована текстура {os.path.basename(texture_path)} в позиции ({x}, {y})")
            else:
                print(f"📍 Размещена текстура {os.path.basename(texture_path)} ({tex_width}x{tex_height}) в позиции ({x}, {y})")
            
        except Exception as e:
            print(f"❌ Ошибка размещения текстуры {texture_path}: {e}")
        finally:
            if 'temp_img' in locals() and temp_img.name in bpy.data.images:
                bpy.data.images.remove(temp_img)
    
    def has_diffuse_opacity_alpha_channel(self, texture_sets, atlas_type):
        """Проверяет есть ли альфаканал в исходных DIFFUSE_OPACITY картах"""
        if atlas_type != 'HIGH':
            return False
            
        for texture_set in texture_sets:
            if texture_set.has_diffuse_opacity:
                diffuse_opacity_path = os.path.join(texture_set.output_path, f"T_{texture_set.material_name}_DIFFUSE_OPACITY.png")
                if os.path.exists(diffuse_opacity_path):
                    try:
                        temp_img = bpy.data.images.load(diffuse_opacity_path)
                        temp_img.update()
                        

                        width = temp_img.size[0]
                        height = temp_img.size[1]
                        channels = len(temp_img.pixels) // (width * height)
                        
                        has_useful_alpha = False
                        
                        if channels == 4:
                            import numpy as np
                            pixels_array = np.array(temp_img.pixels[:]).reshape(height, width, 4)
                            alpha_channel = pixels_array[..., 3]
                            
                            min_alpha = np.min(alpha_channel)
                            max_alpha = np.max(alpha_channel)
                            
                            if min_alpha < max_alpha and min_alpha < 0.99:
                                has_useful_alpha = True
                                print(f"🔍 Найден полезный альфаканал в DIFFUSE_OPACITY: {os.path.basename(diffuse_opacity_path)} (мин: {min_alpha:.3f}, макс: {max_alpha:.3f})")
                            else:
                                print(f"ℹ️  Альфаканал в DIFFUSE_OPACITY не используется: {os.path.basename(diffuse_opacity_path)} (мин: {min_alpha:.3f}, макс: {max_alpha:.3f})")
                        
                        bpy.data.images.remove(temp_img)
                        
                        if has_useful_alpha:
                            return True
                            
                    except Exception as e:
                        print(f"⚠️ Ошибка проверки альфаканала в {diffuse_opacity_path}: {e}")
                        
        return False

    def save_atlas_image(self, image, filepath, atlas_type='LOW', source_has_alpha=False):
        """Сохраняет изображение атласа с учетом типа и наличия альфаканала в исходниках"""
        
        scene = bpy.context.scene
        
        original_format = scene.render.image_settings.file_format
        original_color_mode = scene.render.image_settings.color_mode
        original_color_depth = scene.render.image_settings.color_depth
        original_view_settings = scene.view_settings.view_transform
        original_look = scene.view_settings.look
        original_display_device = scene.display_settings.display_device
        
        scene.render.image_settings.file_format = 'PNG'
        scene.render.image_settings.color_depth = '8'
        scene.view_settings.view_transform = 'Standard'
        scene.view_settings.look = 'None'
        scene.display_settings.display_device = 'sRGB'
        
        if (atlas_type == 'HIGH' and 
            source_has_alpha and 
            '_DIFFUSE_OPACITY.png' in filepath):
            scene.render.image_settings.color_mode = 'RGBA'
            save_mode = 'RGBA (HIGH DIFFUSE_OPACITY атлас с альфаканалом)'
        else:
            scene.render.image_settings.color_mode = 'RGB'
            save_mode = 'RGB'
        
        try:
            image.filepath_raw = filepath
            image.save_render(filepath)
            print(f"💾 Сохранен атлас: {filepath} (режим: {save_mode})")
        except Exception as e:
            print(f"❌ Ошибка сохранения атласа {filepath}: {e}")
        finally:
            scene.render.image_settings.file_format = original_format
            scene.render.image_settings.color_mode = original_color_mode
            scene.render.image_settings.color_depth = original_color_depth
            scene.view_settings.view_transform = original_view_settings
            scene.view_settings.look = original_look
            scene.display_settings.display_device = original_display_device
    
    def create_atlas_material(self, context, atlas_name, created_atlases, atlas_type, normal_type='OPENGL', custom_material_name=None):
        """Создает материал с атласом"""
        if custom_material_name:
            material_name = custom_material_name
        else:
            material_name = f"Material_{atlas_name}"
        
        if material_name in bpy.data.materials:
            material = bpy.data.materials[material_name]
        else:
            material = bpy.data.materials.new(name=material_name)
        
        material.use_nodes = True
        nodes = material.node_tree.nodes
        links = material.node_tree.links
        nodes.clear()
        
        output = nodes.new(type='ShaderNodeOutputMaterial')
        bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
        
        output.location = (400, 0)
        bsdf.location = (100, 0)
        
        links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
        
        def load_atlas_texture(texture_path, texture_name, location, colorspace='sRGB'):
            """Загружает атласную текстуру и создает узел"""
            if os.path.exists(texture_path):
                try:
                    if texture_name in bpy.data.images:
                        bpy.data.images.remove(bpy.data.images[texture_name])
                    
                    
                    img = bpy.data.images.load(texture_path)
                    img.name = texture_name
                    img.filepath = texture_path
                    img.filepath_raw = texture_path
                    img.colorspace_settings.name = colorspace
                    img.reload()
                    img.update()
                    
                    if img.has_data:
                        print(f"✅ Атласная текстура {texture_name} загружена: {img.size[0]}x{img.size[1]}")
                    else:
                        print(f"⚠️  Текстура {texture_name} загружена, но данные отсутствуют")
                        img.reload()
                        img.update()
                        _ = img.pixels[0]
                    
                    tex_node = nodes.new(type='ShaderNodeTexImage')
                    tex_node.image = img
                    tex_node.location = location
                    tex_node.label = texture_name
                    
                    return tex_node
                    
                except Exception as e:
                    print(f"❌ Ошибка загрузки атласной текстуры {texture_name}: {e}")
                    return None
            else:
                print(f"⚠️  Файл атласной текстуры не найден: {texture_path}")
                return None
        
        if atlas_type == 'HIGH':
            print(f"🔧 Подключение атласа HIGH режим")
            
            if 'DIFFUSE_OPACITY' in created_atlases:
                tex_diffuse_opacity = load_atlas_texture(
                    created_atlases['DIFFUSE_OPACITY'], 
                    f"{atlas_name}_DIFFUSE_OPACITY", 
                    (-700, 300), 
                    'sRGB'
                )
                if tex_diffuse_opacity:
                    links.new(tex_diffuse_opacity.outputs['Color'], bsdf.inputs['Base Color'])
                    links.new(tex_diffuse_opacity.outputs['Color'], bsdf.inputs['Emission Color'])
                    links.new(tex_diffuse_opacity.outputs['Alpha'], bsdf.inputs['Alpha'])
            
            if 'ERM' in created_atlases:
                tex_erm = load_atlas_texture(
                    created_atlases['ERM'], 
                    f"{atlas_name}_ERM", 
                    (-700, -100), 
                    'Non-Color'
                )
                if tex_erm:
                    separate_color = nodes.new(type='ShaderNodeSeparateColor')
                    separate_color.location = (-400, -100)
                    
                    links.new(tex_erm.outputs['Color'], separate_color.inputs['Color'])
                    links.new(separate_color.outputs['Red'], bsdf.inputs['Emission Strength'])
                    links.new(separate_color.outputs['Green'], bsdf.inputs['Roughness'])
                    links.new(separate_color.outputs['Blue'], bsdf.inputs['Metallic'])
            
            if 'NORMAL' in created_atlases:
                tex_normal = load_atlas_texture(
                    created_atlases['NORMAL'], 
                    f"{atlas_name}_NORMAL", 
                    (-700, -400), 
                    'Non-Color'
                )
                if tex_normal:
                    connect_normal_map(nodes, links, tex_normal, bsdf, normal_type, (-400, -400))
        
        elif atlas_type == 'LOW':
            print(f"🔧 Подключение атласа LOW режим")
            
            if 'DIFFUSE' in created_atlases:
                tex_diffuse = load_atlas_texture(
                    created_atlases['DIFFUSE'], 
                    f"{atlas_name}_DIFFUSE", 
                    (-700, 400), 
                    'sRGB'
                )
                if tex_diffuse:
                    links.new(tex_diffuse.outputs['Color'], bsdf.inputs['Base Color'])
            
            if 'METALLIC' in created_atlases:
                tex_metallic = load_atlas_texture(
                    created_atlases['METALLIC'], 
                    f"{atlas_name}_METALLIC", 
                    (-700, 200), 
                    'Non-Color'
                )
                if tex_metallic:
                    links.new(tex_metallic.outputs['Color'], bsdf.inputs['Metallic'])
            
            if 'ROUGHNESS' in created_atlases:
                tex_roughness = load_atlas_texture(
                    created_atlases['ROUGHNESS'], 
                    f"{atlas_name}_ROUGHNESS", 
                    (-700, 0), 
                    'Non-Color'
                )
                if tex_roughness:
                    links.new(tex_roughness.outputs['Color'], bsdf.inputs['Roughness'])
            
            if 'OPACITY' in created_atlases:
                tex_opacity = load_atlas_texture(
                    created_atlases['OPACITY'], 
                    f"{atlas_name}_OPACITY", 
                    (-700, -200), 
                    'Non-Color'
                )
                if tex_opacity:
                    links.new(tex_opacity.outputs['Color'], bsdf.inputs['Alpha'])
            
            if 'NORMAL' in created_atlases:
                tex_normal = load_atlas_texture(
                    created_atlases['NORMAL'], 
                    f"{atlas_name}_NORMAL", 
                    (-700, -400), 
                    'Non-Color'
                )
                if tex_normal:
                    connect_normal_map(nodes, links, tex_normal, bsdf, normal_type, (-400, -400))
        
        material.blend_method = 'HASHED'
        material.shadow_method = 'HASHED'
        material.use_backface_culling = False
        
        bsdf.inputs['Base Color'].default_value = (1.0, 1.0, 1.0, 1.0)
        bsdf.inputs['Metallic'].default_value = 0.0
        bsdf.inputs['Roughness'].default_value = 0.8
        bsdf.inputs['IOR'].default_value = 1.5
        bsdf.inputs['Alpha'].default_value = 1.0
        bsdf.inputs['Emission Color'].default_value = (0.0, 0.0, 0.0, 1.0)
        bsdf.inputs['Emission Strength'].default_value = 0.0
        
        bpy.context.view_layer.update()
        for area in bpy.context.screen.areas:
            area.tag_redraw()
        material.node_tree.update_tag()
        
        print(f"🎨 Создан и настроен материал атласа: {material_name}")
        return material
    
    def get_objects_with_selected_materials(self, context, selected_material_names):
        """Находит все объекты в сцене, которые используют материалы из выбранных наборов"""
        found_objects = []
        
        print(f"🔍 Поиск объектов с материалами из выбранных наборов: {selected_material_names}")
        
        # Проверяем, активен ли local view (изоляция)
        space_data = None
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                space_data = area.spaces.active
                break
        
        if space_data and space_data.local_view:
            print(f"🔍 Используется Local View - поиск только среди изолированных объектов")
            # Ищем только среди объектов в local view
            objects_to_check = [obj for obj in context.scene.objects if obj.local_view_get(space_data)]
        else:
            print(f"🔍 Local View не активен - поиск среди всех объектов сцены")
            # Ищем среди всех объектов сцены
            objects_to_check = context.scene.objects
        
        for obj in objects_to_check:
            if obj.type != 'MESH' or not obj.material_slots:
                continue
                
            for slot in obj.material_slots:
                if not slot.material:
                    continue
                    
                material = slot.material
                
                # Проверяем является ли материал одним из выбранных
                if material.name in selected_material_names:
                    found_objects.append({
                        'object': obj,
                        'material_name': material.name
                    })
                    print(f"  ✅ Найден объект: {obj.name} с выбранным материалом: {material.name}")
                    break  # Один объект добавляем только один раз
        
        return found_objects
    
    def apply_atlas_to_objects(self, context, texture_sets, atlas_material, layout):
        """Применяет атласный материал к объектам и корректирует UV"""
        affected_objects = []
        
        material_to_layout = {}
        for item in layout:
            tex_set = item['texture_set']
            material_to_layout[tex_set.material_name] = item
        
        print(f"\n🗺️ === КОРРЕКТИРОВКА UV ===")
        print(f"Будут обработаны объекты с материалами: {list(material_to_layout.keys())}")
        
        # Получаем объекты из наборов текстур (как было раньше)
        object_names = list(set(tex_set.object_name for tex_set in texture_sets))
        
        # Получаем список всех материалов из выбранных наборов
        selected_material_names = [tex_set.material_name for tex_set in texture_sets]
        
        # Ищем все объекты в сцене, которые используют эти материалы
        all_objects_with_materials = self.get_objects_with_selected_materials(context, selected_material_names)
        
        # Добавляем найденные объекты к списку для обработки
        for obj_data in all_objects_with_materials:
            obj = obj_data['object']
            if obj.name not in object_names:
                object_names.append(obj.name)
                print(f"  ➕ Добавлен объект с выбранным материалом: {obj.name}")
        
        print(f"📋 Общее количество объектов для обработки: {len(object_names)}")
        
        for obj_name in object_names:
            if obj_name in bpy.data.objects:
                obj = bpy.data.objects[obj_name]
                if obj.type == 'MESH':
                    
                    object_layout_item = None
                    original_material_name = None
                    
                    for slot in obj.material_slots:
                        if slot.material and slot.material.name in material_to_layout:
                            object_layout_item = material_to_layout[slot.material.name]
                            original_material_name = slot.material.name
                            break
                    
                    if not object_layout_item:
                        for tex_set in texture_sets:
                            if tex_set.object_name == obj_name:
                                object_layout_item = material_to_layout.get(tex_set.material_name)
                                original_material_name = tex_set.material_name
                                break
                    
                    if not object_layout_item:

                        continue
                    
                    print(f"  📍 Использую layout для материала: {original_material_name}")
                    
                    uv_corrected = self.adjust_uv_for_atlas(obj, object_layout_item, atlas_material, material_to_layout)
                    
                    if uv_corrected:
                        affected_objects.append(obj)
                        print(f"  ✅ UV развертки скорректированы")
                        
                        # Удаляем неиспользуемые слоты материалов после назначения атласа
                        try:
                            original_active = bpy.context.view_layer.objects.active
                            original_selected = [o for o in bpy.context.selected_objects]
                            
                            bpy.ops.object.select_all(action='DESELECT')
                            obj.select_set(True)
                            bpy.context.view_layer.objects.active = obj
                            
                            bpy.ops.object.material_slot_remove_unused()
                            print(f"  🧹 Удалены неиспользуемые слоты материалов с объекта {obj.name}")
                            
                            # Восстанавливаем выделение
                            bpy.ops.object.select_all(action='DESELECT')
                            if original_active:
                                bpy.context.view_layer.objects.active = original_active
                            for o in original_selected:
                                if o and o.name in bpy.data.objects:
                                    o.select_set(True)
                                    
                        except Exception as e:
                            print(f"  ⚠️  Ошибка при удалении неиспользуемых слотов: {e}")
                    else:
                        print(f"  ⚠️  UV развертки не требуют коррекции")
        
        print(f"🔧 Применен атлас к {len(affected_objects)} объектам")
        return affected_objects
    
    def adjust_uv_for_atlas(self, obj, layout_item, atlas_material, material_to_layout=None):
        """Корректирует UV развертки объекта для соответствия атласу"""
        if not obj.data.uv_layers:
            print(f"    ⚠️  Объект {obj.name} не имеет UV разверток")
            return False
        
        if not layout_item:
            print(f"    ❌ Не передан layout_item для объекта {obj.name}")
            return False
        
        original_active = bpy.context.view_layer.objects.active
        original_selected = [o for o in bpy.context.selected_objects]
        original_mode = bpy.context.mode
        
        try:
            area_3d = None
            for area in bpy.context.screen.areas:
                if area.type == 'VIEW_3D':
                    area_3d = area
                    break
            
            if not area_3d:
                print(f"    ❌ Не найдена область 3D Viewport")
                return False
            
            if bpy.context.mode != 'OBJECT':
                with bpy.context.temp_override(area=area_3d, active_object=bpy.context.active_object):
                    bpy.ops.object.mode_set(mode='OBJECT')
            
            bpy.ops.object.select_all(action='DESELECT')
            
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj
            
            bpy.context.view_layer.update()
            
            if bpy.context.view_layer.objects.active != obj:
                print(f"    ❌ Не удалось сделать объект {obj.name} активным")
                return False
            
            with bpy.context.temp_override(area=area_3d, active_object=obj, selected_objects=[obj]):
                bpy.ops.object.mode_set(mode='EDIT')
            
            import bmesh
            bm = bmesh.from_edit_mesh(obj.data)
            bm.faces.ensure_lookup_table()
            
            if not bm.loops.layers.uv:
                print(f"    ⚠️  Нет UV слоя в bmesh")
                return False
            
            uv_layer_bmesh = bm.loops.layers.uv.active
            
            faces_modified = 0
            atlas_material_index = -1
            
            atlas_material_found = False
            for i, slot in enumerate(obj.material_slots):
                if slot.material == atlas_material:
                    atlas_material_index = i
                    atlas_material_found = True
                    break
            
            if not atlas_material_found:
                obj.data.materials.append(atlas_material)
                atlas_material_index = len(obj.material_slots) - 1
                print(f"      ➕ Добавлен материал атласа в слот {atlas_material_index}")
            else:
                print(f"      ✅ Материал атласа найден в слоте {atlas_material_index}")
            
            for face in bm.faces:
                face_layout_item = layout_item
                face_should_use_atlas = False
                
                if material_to_layout and len(obj.material_slots) > face.material_index:
                    material_slot = obj.material_slots[face.material_index]
                    if material_slot.material and material_slot.material.name in material_to_layout:
                        face_layout_item = material_to_layout[material_slot.material.name]
                        face_should_use_atlas = True
                
                if face_should_use_atlas or not material_to_layout:
                    face.material_index = atlas_material_index
                    
                    for loop in face.loops:
                        uv = loop[uv_layer_bmesh].uv
                        
                        orig_u, orig_v = uv.x, uv.y
                        
                        new_u = face_layout_item['u_min'] + orig_u * (face_layout_item['u_max'] - face_layout_item['u_min'])
                        new_v = face_layout_item['v_min'] + orig_v * (face_layout_item['v_max'] - face_layout_item['v_min'])
                        
                        uv.x = new_u
                        uv.y = new_v
                    
                    faces_modified += 1
            
            bmesh.update_edit_mesh(obj.data)
            
            print(f"    📐 Скорректировано {faces_modified} граней")
            print(f"    🎨 Материал атласа назначен на {faces_modified} граней в слоте {atlas_material_index}")
            
            return faces_modified > 0
            
        except Exception as e:
            print(f"    ❌ Ошибка при корректировке UV для {obj.name}: {e}")
            return False
            
        finally:
            try:
                area_3d = None
                for area in bpy.context.screen.areas:
                    if area.type == 'VIEW_3D':
                        area_3d = area
                        break
                
                if bpy.context.mode != 'OBJECT':
                    if area_3d and bpy.context.view_layer.objects.active:
                        with bpy.context.temp_override(area=area_3d, active_object=bpy.context.view_layer.objects.active):
                            bpy.ops.object.mode_set(mode='OBJECT')
                    else:
                        try:
                            bpy.ops.object.mode_set(mode='OBJECT')
                        except:
                            pass
                
                bpy.ops.object.select_all(action='DESELECT')
                for selected_obj in original_selected:
                    if selected_obj and selected_obj.name in bpy.data.objects:
                        selected_obj.select_set(True)
                
                if original_active and original_active.name in bpy.data.objects:
                    bpy.context.view_layer.objects.active = original_active
                    
            except Exception as restore_error:
                print(f"    ⚠️  Предупреждение при восстановлении состояния: {restore_error}")

class BAKER_OT_preview_atlas_layout(Operator):
    """Быстрый предпросмотр раскладки без записи файлов"""
    bl_idname = "baker.preview_atlas_layout"
    bl_label = "Предпросмотр раскладки"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        selected_sets = [tex_set for tex_set in scene.baker_texture_sets if tex_set.is_selected_for_atlas]
        if not selected_sets:
            self.report({'ERROR'}, "Не выбрано ни одного набора текстур")
            return {'CANCELLED'}
        atlas_size = int(scene.baker_atlas_size)
        try:
            layout = calculate_atlas_packing_layout(selected_sets, atlas_size)
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка расчета раскладки: {e}")
            return {'CANCELLED'}

        # Ищем и удаляем существующий предпросмотр
        preview_name = "AGR_Atlas_Preview"
        if preview_name in bpy.data.images:
            existing_image = bpy.data.images[preview_name]
            

            
            # Удаляем изображение
            bpy.data.images.remove(existing_image)
            print(f"🗑️ Удалено существующее изображение {preview_name}")

        # Создаем временное изображение предпросмотра
        img = bpy.data.images.new(preview_name, width=atlas_size, height=atlas_size, alpha=True, float_buffer=False)
        img.colorspace_settings.name = 'sRGB'

        # Рисуем рамки яркими разными цветами поверх черного фона
        import numpy as _np
        canvas = _np.zeros((atlas_size, atlas_size, 4), dtype=_np.float32)
        border_width = 10  # Ширина границы в пикселях
        
        # Набор ярких цветов для границ
        bright_colors = [
            (1.0, 0.0, 0.0, 1.0),  # Красный
            (0.0, 1.0, 0.0, 1.0),  # Зеленый
            (0.0, 0.0, 1.0, 1.0),  # Синий
            (1.0, 1.0, 0.0, 1.0),  # Желтый
            (1.0, 0.0, 1.0, 1.0),  # Магента
            (0.0, 1.0, 1.0, 1.0),  # Циан
            (1.0, 0.5, 0.0, 1.0),  # Оранжевый
            (0.5, 0.0, 1.0, 1.0),  # Фиолетовый
            (1.0, 0.0, 0.5, 1.0),  # Розовый
            (0.5, 1.0, 0.0, 1.0),  # Лайм
        ]
        
        for i, item in enumerate(layout):
            x, y, w, h = item['x'], item['y'], item['width'], item['height']
            # Выбираем цвет по индексу с повторением
            color = bright_colors[i % len(bright_colors)]
            
            # Рисуем границы шириной border_width пикселей
            # Левая граница
            canvas[y:y+h, x:x+border_width, :] = color
            # Правая граница
            canvas[y:y+h, max(0, x+w-border_width):x+w, :] = color
            # Верхняя граница
            canvas[y:y+border_width, x:x+w, :] = color
            # Нижняя граница
            canvas[max(0, y+h-border_width):y+h, x:x+w, :] = color
        
        flat = canvas.flatten().tolist()
        try:
            img.pixels.foreach_set(flat)
        except Exception:
            img.pixels = flat
        img.update()

        # Показываем изображение в UV Editor или Image Editor
        self.show_preview_in_editor(context, img)

        self.report({'INFO'}, "Предпросмотр создан: Image 'AGR_Atlas_Preview'")
        return {'FINISHED'}

    def show_preview_in_editor(self, context, image):
        """Показывает изображение в UV Editor или Image Editor"""
        # Ищем UV Editor или Image Editor
        target_area = None
        for area in context.screen.areas:
            if area.type in ['IMAGE_EDITOR']:
                target_area = area
                break
        
        if target_area:
            # Устанавливаем изображение в найденном редакторе
            for space in target_area.spaces:
                if space.type == 'IMAGE_EDITOR':
                    space.image = image
                    # Снимаем pin с изображения
                    space.use_image_pin = False
                    break
            
            # Обновляем область
            target_area.tag_redraw()
            print(f"📷 Предпросмотр отображен в Image Editor")
        else:
            print(f"⚠️ Image Editor не найден, изображение создано в Data")

class BAKER_OT_restore_materials_from_atlas(Operator):
    """Откат материалов и UV после применения атласа (по сохраненному манифесту)"""
    bl_idname = "baker.restore_materials_from_atlas"
    bl_label = "Откат материалов из атласа"
    bl_options = {'REGISTER', 'UNDO'}

    manifest_path: StringProperty(name="Путь к манифесту", default="", subtype='FILE_PATH')

    def _is_material_properly_setup(self, material, atlas_type='HIGH'):
        """Проверяет, правильно ли настроен материал с текстурами"""
        if not material.node_tree:
            return False

        nodes = material.node_tree.nodes
        texture_nodes = [node for node in nodes if node.type == 'TEX_IMAGE']

        # Проверяем материал по критериям HIGH атласа
        high_required = ['ERM', 'DIFFUSE_OPACITY']
        high_normal = ['NORMAL', 'NORMAL_DIRECTX']
        if self._check_texture_setup(texture_nodes, high_required, high_normal):
            return True

        # Проверяем материал по критериям LOW атласа
        low_required = ['DIFFUSE', 'ROUGHNESS', 'METALLIC']
        low_normal = ['NORMAL', 'NORMAL_DIRECTX']
        if self._check_texture_setup(texture_nodes, low_required, low_normal):
            return True

        return False

    def _check_texture_setup(self, texture_nodes, required_textures, normal_textures):
        """Вспомогательная функция для проверки текстур по заданным критериям"""
        found_textures = []
        found_normal = False

        for node in texture_nodes:
            if node.image:
                image_name = node.image.name.upper()
                # Проверяем основные текстуры
                for required in required_textures:
                    if required in image_name:
                        found_textures.append(required)
                        break

                # Проверяем нормаль (NORMAL или NORMAL_DIRECTX)
                for normal_type in normal_textures:
                    if normal_type in image_name:
                        found_normal = True
                        break

        # Проверяем, что найдены все необходимые текстуры И нормаль
        has_all_required = len(set(found_textures)) >= len(required_textures)
        return has_all_required and found_normal

    def _clear_material_nodes(self, material):
        """Полностью очищает все ноды материала, оставляя только базовую структуру"""
        if not material.node_tree:
            material.use_nodes = True
            return

        nodes = material.node_tree.nodes
        links = material.node_tree.links

        # Очищаем все связи
        for link in links:
            links.remove(link)

        # Удаляем все ноды, кроме базовых
        nodes_to_remove = []
        for node in nodes:
            # Сохраняем только Principled BSDF и Material Output
            if node.type not in ['BSDF_PRINCIPLED', 'OUTPUT_MATERIAL']:
                nodes_to_remove.append(node)

        for node in nodes_to_remove:
            nodes.remove(node)

        # Создаем базовую структуру, если её нет
        principled = nodes.get('Principled BSDF')
        if not principled:
            principled = nodes.new('ShaderNodeBsdfPrincipled')
            principled.name = 'Principled BSDF'
            principled.location = (0, 0)

        output = nodes.get('Material Output')
        if not output:
            output = nodes.new('ShaderNodeOutputMaterial')
            output.name = 'Material Output'
            output.location = (300, 0)

        # Подключаем базовую связь
        if not output.inputs['Surface'].links:
            links.new(principled.outputs['BSDF'], output.inputs['Surface'])

    def setup_material_from_texture_sets(self, material, material_name, texture_sets, atlas_type='HIGH'):
        """Находит и подключает текстуры к материалу из списка наборов текстур"""
        print(f"🔍 Поиск текстур для материала '{material_name}' (тип атласа: {atlas_type})...")

        # Ищем набор текстур для этого материала
        matching_set = None
        for tex_set in texture_sets:
            # Сравниваем по имени материала или имени набора
            if tex_set.material_name == material_name or tex_set.name == material_name:
                matching_set = tex_set
                break

        if not matching_set:
            print(f"❌ Набор текстур для материала '{material_name}' не найден в списке")
            return

        print(f"✅ Найден набор текстур: {matching_set.name} (путь: {matching_set.output_path})")

        # Очищаем материал перед настройкой
        self._clear_material_nodes(material)
        print(f"🧹 Материал '{material_name}' очищен перед настройкой")

        # Создаем ноды для подключения текстур в зависимости от типа атласа
        self.create_texture_nodes_for_material(material, matching_set, atlas_type)

    def create_texture_nodes_for_material(self, material, texture_set, atlas_type='HIGH'):
        """Создает и подключает ноды текстур к материалу в зависимости от типа атласа"""
        if not material.node_tree:
            material.use_nodes = True

        nodes = material.node_tree.nodes
        links = material.node_tree.links

        # Очищаем существующие ноды текстур (кроме Principled BSDF и Material Output)
        texture_nodes_to_remove = []
        for node in nodes:
            if node.type == 'TEX_IMAGE' and node != nodes.get('Principled BSDF') and node != nodes.get('Material Output'):
                texture_nodes_to_remove.append(node)

        for node in texture_nodes_to_remove:
            nodes.remove(node)

        # Получаем или создаем Principled BSDF
        principled = nodes.get('Principled BSDF')
        if not principled:
            principled = nodes.new('ShaderNodeBsdfPrincipled')
            principled.name = 'Principled BSDF'
            principled.location = (0, 0)

        # Получаем или создаем Material Output
        output = nodes.get('Material Output')
        if not output:
            output = nodes.new('ShaderNodeOutputMaterial')
            output.name = 'Material Output'
            output.location = (300, 0)

        # Подключаем Principled к Output, если не подключено
        if not output.inputs['Surface'].links:
            links.new(principled.outputs['BSDF'], output.inputs['Surface'])

        base_path = os.path.join(texture_set.output_path, texture_set.name)
        node_x = -400
        node_y = 200

        print(f"🔧 Настройка текстур для типа атласа: {atlas_type}")

        if atlas_type == 'HIGH':
            # HIGH атлас: используем DIFFUSE_OPACITY + ERM + NORMAL
            self._setup_high_atlas_textures(nodes, links, principled, base_path, texture_set, node_x, node_y)
        else:  # atlas_type == 'LOW'
            # LOW атлас: используем отдельные карты
            self._setup_low_atlas_textures(nodes, links, principled, base_path, texture_set, node_x, node_y)

        print(f"✅ Завершено подключение текстур к материалу '{material.name}' (тип: {atlas_type})")

    def _setup_high_atlas_textures(self, nodes, links, principled, base_path, texture_set, node_x, node_y):
        """Настраивает текстуры для HIGH атласа (DIFFUSE_OPACITY + ERM + NORMAL)"""
        # Подключаем DIFFUSE_OPACITY текстуру
        if texture_set.has_diffuse_opacity:
            diffuse_opacity_path = f"{base_path}_DIFFUSE_OPACITY.png"
            if os.path.exists(diffuse_opacity_path):
                diffuse_node = nodes.new('ShaderNodeTexImage')
                diffuse_node.name = 'Diffuse Opacity Texture'
                diffuse_node.location = (node_x, node_y)
                diffuse_node.image = bpy.data.images.load(diffuse_opacity_path)
                links.new(diffuse_node.outputs['Color'], principled.inputs['Base Color'])
                links.new(diffuse_node.outputs['Alpha'], principled.inputs['Alpha'])
                print(f"  🎨 HIGH: Подключена DIFFUSE_OPACITY текстура: {os.path.basename(diffuse_opacity_path)}")
                node_y -= 300

        # Подключаем NORMAL текстуру
        if texture_set.has_normal:
            normal_path = f"{base_path}_NORMAL.png"
            if os.path.exists(normal_path):
                normal_node = nodes.new('ShaderNodeTexImage')
                normal_node.name = 'Normal Texture'
                normal_node.location = (node_x, node_y)
                normal_node.image = bpy.data.images.load(normal_path)
                normal_node.image.colorspace_settings.name = 'Non-Color'

                # Создаем Normal Map ноду
                normal_map_node = nodes.new('ShaderNodeNormalMap')
                normal_map_node.location = (node_x + 200, node_y)
                links.new(normal_node.outputs['Color'], normal_map_node.inputs['Color'])
                links.new(normal_map_node.outputs['Normal'], principled.inputs['Normal'])
                print(f"  🗺️  HIGH: Подключена NORMAL текстура: {os.path.basename(normal_path)}")
                node_y -= 300

        # Подключаем ERM текстуру (Emission Strength + Roughness + Metallic)
        if texture_set.has_erm:
            erm_path = f"{base_path}_ERM.png"
            if os.path.exists(erm_path):
                erm_node = nodes.new('ShaderNodeTexImage')
                erm_node.name = 'ERM Texture'
                erm_node.location = (node_x, node_y)
                erm_node.image = bpy.data.images.load(erm_path)
                erm_node.image.colorspace_settings.name = 'Non-Color'

                # Разделяем каналы ERM
                separate_color = nodes.new('ShaderNodeSeparateColor')
                separate_color.location = (node_x + 200, node_y)

                links.new(erm_node.outputs['Color'], separate_color.inputs['Color'])
                links.new(separate_color.outputs['Red'], principled.inputs['Emission Strength'])  # Red = Emission Strength
                links.new(separate_color.outputs['Green'], principled.inputs['Roughness'])  # Green = Roughness
                links.new(separate_color.outputs['Blue'], principled.inputs['Metallic'])  # Blue = Metallic
                print(f"  🎭 HIGH: Подключена ERM текстура (R=Emission, G=Roughness, B=Metallic): {os.path.basename(erm_path)}")

    def _setup_low_atlas_textures(self, nodes, links, principled, base_path, texture_set, node_x, node_y):
        """Настраивает текстуры для LOW атласа (отдельные карты)"""
        # Подключаем DIFFUSE текстуру
        if texture_set.has_diffuse:
            diffuse_path = f"{base_path}_DIFFUSE.png"
            if os.path.exists(diffuse_path):
                diffuse_node = nodes.new('ShaderNodeTexImage')
                diffuse_node.name = 'Diffuse Texture'
                diffuse_node.location = (node_x, node_y)
                diffuse_node.image = bpy.data.images.load(diffuse_path)
                links.new(diffuse_node.outputs['Color'], principled.inputs['Base Color'])
                print(f"  🎨 LOW: Подключена DIFFUSE текстура: {os.path.basename(diffuse_path)}")
                node_y -= 300

        # Подключаем ROUGHNESS текстуру
        if texture_set.has_roughness:
            roughness_path = f"{base_path}_ROUGHNESS.png"
            if os.path.exists(roughness_path):
                roughness_node = nodes.new('ShaderNodeTexImage')
                roughness_node.name = 'Roughness Texture'
                roughness_node.location = (node_x, node_y)
                roughness_node.image = bpy.data.images.load(roughness_path)
                roughness_node.image.colorspace_settings.name = 'Non-Color'
                links.new(roughness_node.outputs['Color'], principled.inputs['Roughness'])
                print(f"  🔶 LOW: Подключена ROUGHNESS текстура: {os.path.basename(roughness_path)}")
                node_y -= 300

        # Подключаем METALLIC текстуру
        if texture_set.has_metallic:
            metallic_path = f"{base_path}_METALLIC.png"
            if os.path.exists(metallic_path):
                metallic_node = nodes.new('ShaderNodeTexImage')
                metallic_node.name = 'Metallic Texture'
                metallic_node.location = (node_x, node_y)
                metallic_node.image = bpy.data.images.load(metallic_path)
                metallic_node.image.colorspace_settings.name = 'Non-Color'
                links.new(metallic_node.outputs['Color'], principled.inputs['Metallic'])
                print(f"  ⚙️  LOW: Подключена METALLIC текстура: {os.path.basename(metallic_path)}")
                node_y -= 300

        # Подключаем NORMAL текстуру
        if texture_set.has_normal:
            normal_path = f"{base_path}_NORMAL.png"
            if os.path.exists(normal_path):
                normal_node = nodes.new('ShaderNodeTexImage')
                normal_node.name = 'Normal Texture'
                normal_node.location = (node_x, node_y)
                normal_node.image = bpy.data.images.load(normal_path)
                normal_node.image.colorspace_settings.name = 'Non-Color'

                # Создаем Normal Map ноду
                normal_map_node = nodes.new('ShaderNodeNormalMap')
                normal_map_node.location = (node_x + 200, node_y)
                links.new(normal_node.outputs['Color'], normal_map_node.inputs['Color'])
                links.new(normal_map_node.outputs['Normal'], principled.inputs['Normal'])
                print(f"  🗺️  LOW: Подключена NORMAL текстура: {os.path.basename(normal_path)}")
                node_y -= 300

        # Подключаем OPACITY текстуру
        if texture_set.has_opacity:
            opacity_path = f"{base_path}_OPACITY.png"
            if os.path.exists(opacity_path):
                opacity_node = nodes.new('ShaderNodeTexImage')
                opacity_node.name = 'Opacity Texture'
                opacity_node.location = (node_x, node_y)
                opacity_node.image = bpy.data.images.load(opacity_path)
                opacity_node.image.colorspace_settings.name = 'Non-Color'
                links.new(opacity_node.outputs['Color'], principled.inputs['Alpha'])
                print(f"  💧 LOW: Подключена OPACITY текстура: {os.path.basename(opacity_path)}")
                node_y -= 300

        # EMIT текстура не подключается в LOW атласе

    def execute(self, context):
        path = self.manifest_path
        
        if not path:
            # Попытаемся найти манифест по активному объекту и его активному материалу
            obj = context.active_object
            if not obj or obj.type != 'MESH' or not obj.material_slots:
                self.report({'ERROR'}, "Выберите объект с материалами")
                return {'CANCELLED'}
            
            # Получаем активный материал
            active_material = obj.active_material
            if not active_material:
                self.report({'ERROR'}, "У объекта нет активного материала")
                return {'CANCELLED'}
            
            # Ищем текстуры в материале для определения пути к манифесту
            atlas_texture_path = None
            if active_material.node_tree:
                for node in active_material.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image:
                        image_path = node.image.filepath
                        if image_path and ('Atlas_' in os.path.basename(image_path) or 'atlas' in image_path.lower()):
                            atlas_texture_path = image_path
                            break
            
            if not atlas_texture_path:
                self.report({'ERROR'}, "В активном материале не найдены текстуры атласа")
                return {'CANCELLED'}
            
            # Определяем директорию и имя атласа из пути текстуры
            # Преобразуем UNC путь в полный путь, если необходимо
            if atlas_texture_path.startswith('//'):
                atlas_texture_path = bpy.path.abspath(atlas_texture_path)

            texture_dir = os.path.dirname(atlas_texture_path)
            texture_filename = os.path.basename(atlas_texture_path)
            
            # Проверяем, новый ли формат текстур (T_Address_ObjectType_d_1.png и т.д.)
            is_new_format = (texture_filename.startswith('T_') and 
                           any(suffix in texture_filename for suffix in ['_d_1.png', '_r_1.png', '_m_1.png', '_n_1.png', '_o_1.png']))
            
            if is_new_format:
                # Новый формат: берем адрес из активного объекта
                try:
                    active_obj = context.active_object
                    if active_obj:
                        address, obj_type = process_object_name(active_obj.name)
                        # Ищем json файл с этим адресом
                        import glob
                        pattern = os.path.join(texture_dir, f"*{address}*_layout.json")
                        print(f"🔍 Ищем манифест с адресом '{address}': {pattern}")
                        manifest_files = glob.glob(pattern)
                        
                        if manifest_files:
                            path = manifest_files[0]
                            print(f"✅ Найден манифест: {os.path.basename(path)}")
                        else:
                            print(f"❌ Манифест с адресом '{address}' не найден")
                            # Fallback к старой логике
                            atlas_name = os.path.splitext(texture_filename)[0]
                            path = os.path.join(texture_dir, f"{atlas_name}_layout.json")
                    else:
                        # Нет активного объекта, используем старую логику
                        atlas_name = os.path.splitext(texture_filename)[0]
                        path = os.path.join(texture_dir, f"{atlas_name}_layout.json")
                        
                except Exception as e:
                    print(f"⚠️ Ошибка при обработке нового формата: {e}")
                    # Fallback к старой логике
                    atlas_name = os.path.splitext(texture_filename)[0]
                    path = os.path.join(texture_dir, f"{atlas_name}_layout.json")
            else:
                # Старый формат: извлекаем имя атласа из имени файла
                atlas_name = texture_filename
                for suffix in ['_DIFFUSE', '_NORMAL', '_ERM', '_DIFFUSE_OPACITY', '_OPACITY', '_ROUGHNESS', '_METALLIC', '_EMIT', '_NORMAL_DIRECTX']:
                    if suffix in atlas_name:
                        atlas_name = atlas_name.split(suffix)[0]
                        break
                # Убираем расширение
                atlas_name = os.path.splitext(atlas_name)[0]
                path = os.path.join(texture_dir, f"{atlas_name}_layout.json")

        if not os.path.exists(path):
            self.report({'ERROR'}, f"Манифест не найден: {path}")
            return {'CANCELLED'}
            
        try:
            with open(path, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка чтения манифеста: {e}")
            return {'CANCELLED'}

        # Подготовим быстрый поиск по layout
        layout = manifest.get('layout', [])
        atlas_name = manifest.get('atlas_name')
        atlas_type = manifest.get('atlas_type', 'HIGH')  # Извлекаем тип атласа из манифеста
        
        # Определяем имя материала атласа
        if atlas_name and atlas_name.startswith('Atlas_LOW_'):
            # Новый формат: получаем имя материала из активного объекта
            try:
                active_obj = context.active_object
                if active_obj:
                    address, obj_type = process_object_name(active_obj.name)
                    atlas_mat_name = f"M_{address}_{obj_type}_1"
                    print(f"🏷️  Новый формат: ищем материал '{atlas_mat_name}'")
                else:
                    # Fallback к старому формату
                    atlas_mat_name = f"Material_{atlas_name}"
                    print(f"🏷️  Fallback: ищем материал '{atlas_mat_name}'")
            except:
                # Fallback к старому формату
                atlas_mat_name = f"Material_{atlas_name}"
                print(f"🏷️  Fallback (ошибка): ищем материал '{atlas_mat_name}'")
        else:
            # Старый формат
            atlas_mat_name = f"Material_{atlas_name}"
            print(f"🏷️  Старый формат: ищем материал '{atlas_mat_name}'")

        import bmesh

        processed_objects = 0
        restored_faces_total = 0
        processed_objects_list = []
        
        for obj in context.scene.objects:
            if obj.type != 'MESH' or not obj.data.uv_layers or not obj.material_slots:
                continue

            # Ищем материал атласа на объекте
            atlas_index = -1
            for i, slot in enumerate(obj.material_slots):
                if slot.material and slot.material.name == atlas_mat_name:
                    atlas_index = i
                    break
            if atlas_index == -1:
                continue

            # Гарантируем наличие исходных материалов из layout
            material_name_to_index = {}
            for item in layout:
                mat_name = item['material_name']
                mat = bpy.data.materials.get(mat_name)

                # Если материал не существует, создаем его и ищем текстуры
                if not mat:
                    mat = bpy.data.materials.new(mat_name)
                    print(f"📝 Создан новый материал: {mat_name}")
                    self.setup_material_from_texture_sets(mat, mat_name, context.scene.baker_texture_sets, atlas_type)
                else:
                    print(f"✅ Материал уже существует: {mat_name}")
                    # Проверяем, правильно ли настроен существующий материал
                    if not self._is_material_properly_setup(mat, atlas_type):
                        print(f"🔧 Материал '{mat_name}' требует настройки текстур")
                        self.setup_material_from_texture_sets(mat, mat_name, context.scene.baker_texture_sets, atlas_type)
                    else:
                        print(f"✨ Материал '{mat_name}' уже правильно настроен")

                # добавим в объект, если отсутствует
                found = None
                for i, slot in enumerate(obj.material_slots):
                    if slot.material and slot.material.name == mat_name:
                        found = i
                        break
                if found is None:
                    obj.data.materials.append(mat)
                    found = len(obj.material_slots) - 1
                material_name_to_index[mat_name] = found

            # Редактируем UV и материалы
            if bpy.context.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.mode_set(mode='EDIT')
            bm = bmesh.from_edit_mesh(obj.data)
            bm.faces.ensure_lookup_table()
            uv_layer = bm.loops.layers.uv.active

            restored_faces = 0
            for face in bm.faces:
                if face.material_index != atlas_index:
                    continue
                # средние UV для определения прямоугольника
                u_sum = 0.0
                v_sum = 0.0
                count = len(face.loops)
                if count == 0:
                    continue
                for loop in face.loops:
                    uv = loop[uv_layer].uv
                    u_sum += uv.x
                    v_sum += uv.y
                u_avg = u_sum / count
                v_avg = v_sum / count

                # Находим layout-ячейку, содержащую центр
                matched = None
                for item in layout:
                    if (u_avg >= item['u_min'] and u_avg <= item['u_max'] and
                        v_avg >= item['v_min'] and v_avg <= item['v_max']):
                        matched = item
                        break
                if matched is None:
                    continue

                # Переназначаем материал
                target_mat_idx = material_name_to_index.get(matched['material_name'], -1)
                if target_mat_idx != -1:
                    face.material_index = target_mat_idx

                # Обратное преобразование UV в 0–1
                u_min = matched['u_min']
                v_min = matched['v_min']
                u_scale = (matched['u_max'] - matched['u_min']) or 1.0
                v_scale = (matched['v_max'] - matched['v_min']) or 1.0
                for loop in face.loops:
                    uv = loop[uv_layer].uv
                    uv.x = (uv.x - u_min) / u_scale
                    uv.y = (uv.y - v_min) / v_scale

                restored_faces += 1

            bmesh.update_edit_mesh(obj.data)
            bpy.ops.object.mode_set(mode='OBJECT')

            if restored_faces > 0:
                processed_objects += 1
                restored_faces_total += restored_faces
                processed_objects_list.append(obj)

        # Удаляем неиспользуемые слоты материалов со всех обработанных объектов
        if processed_objects_list:
            original_active = bpy.context.view_layer.objects.active
            original_selected = [o for o in bpy.context.selected_objects]
            
            for obj in processed_objects_list:
                try:
                    bpy.ops.object.select_all(action='DESELECT')
                    obj.select_set(True)
                    bpy.context.view_layer.objects.active = obj
                    
                    bpy.ops.object.material_slot_remove_unused()
                    print(f"  🧹 Удалены неиспользуемые слоты материалов с объекта {obj.name}")
                    
                except Exception as e:
                    print(f"  ⚠️  Ошибка при удалении неиспользуемых слотов с объекта {obj.name}: {e}")
            
            # Восстанавливаем выделение
            try:
                bpy.ops.object.select_all(action='DESELECT')
                if original_active and original_active.name in bpy.data.objects:
                    bpy.context.view_layer.objects.active = original_active
                for o in original_selected:
                    if o and o.name in bpy.data.objects:
                        o.select_set(True)
            except Exception as e:
                print(f"  ⚠️  Ошибка при восстановлении выделения: {e}")

        self.report({'INFO'}, f"Откат выполнен. Объектов: {processed_objects}, полигонов: {restored_faces_total}")
        return {'FINISHED'}

class BAKER_OT_select_texture_sets_for_selected_objects(Operator):
    """Выбрать все наборы текстур для материалов всех выбранных объектов"""
    bl_idname = "baker.select_texture_sets_for_selected_objects"
    bl_label = "Выбрать наборы для выбранных объектов"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']

        if not selected_objects:
            self.report({'ERROR'}, "Не выбрано ни одного mesh объекта")
            return {'CANCELLED'}

        # Сначала снимаем выделение со всех наборов
        for tex_set in scene.baker_texture_sets:
            tex_set.is_selected_for_atlas = False

        # Собираем все имена материалов из выбранных объектов
        material_names = set()
        for obj in selected_objects:
            for slot in obj.material_slots:
                if slot.material:
                    material_names.add(slot.material.name)

        # Выбираем все наборы текстур для этих материалов
        selected_count = 0
        for tex_set in scene.baker_texture_sets:
            if tex_set.material_name in material_names:
                tex_set.is_selected_for_atlas = True
                selected_count += 1

        if selected_count == 0:
            self.report({'WARNING'}, "Для выбранных объектов не найдено подходящих наборов текстур")
        else:
            self.report({'INFO'}, f"Выбрано {selected_count} наборов текстур для материалов выбранных объектов")

        return {'FINISHED'}

class BAKER_OT_toggle_select_all_texture_sets(Operator):
    """Выбрать все наборы текстур или снять выделение со всех"""
    bl_idname = "baker.toggle_select_all_texture_sets"
    bl_label = "Выбрать/снять все"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene

        if not scene.baker_texture_sets:
            self.report({'WARNING'}, "Нет доступных наборов текстур")
            return {'CANCELLED'}

        # Проверяем, все ли наборы выбраны
        all_selected = all(tex_set.is_selected_for_atlas for tex_set in scene.baker_texture_sets)
        selected_count = sum(1 for tex_set in scene.baker_texture_sets if tex_set.is_selected_for_atlas)

        if all_selected:
            # Снимаем выделение со всех
            for tex_set in scene.baker_texture_sets:
                tex_set.is_selected_for_atlas = False
            self.report({'INFO'}, "Снято выделение со всех наборов текстур")
        else:
            # Выбираем все
            for tex_set in scene.baker_texture_sets:
                tex_set.is_selected_for_atlas = True
            self.report({'INFO'}, f"Выбрано {len(scene.baker_texture_sets)} наборов текстур")

        return {'FINISHED'}

class BAKER_OT_invert_texture_sets_selection(Operator):
    """Инвертировать выделение наборов текстур"""
    bl_idname = "baker.invert_texture_sets_selection"
    bl_label = "Инвертировать выделение"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene

        if not scene.baker_texture_sets:
            self.report({'WARNING'}, "Нет доступных наборов текстур")
            return {'CANCELLED'}

        # Инвертируем выделение для каждого набора
        inverted_count = 0
        for tex_set in scene.baker_texture_sets:
            tex_set.is_selected_for_atlas = not tex_set.is_selected_for_atlas
            inverted_count += 1

        selected_count = sum(1 for tex_set in scene.baker_texture_sets if tex_set.is_selected_for_atlas)
        self.report({'INFO'}, f"Инвертировано выделение. Выбрано {selected_count} из {len(scene.baker_texture_sets)} наборов")

        return {'FINISHED'}

class BAKER_OT_delete_selected_texture_sets(Operator):
    """Удалить выбранные наборы текстур и соответствующие материалы"""
    bl_idname = "baker.delete_selected_texture_sets"
    bl_label = "Удалить наборы"
    bl_options = {'REGISTER', 'UNDO'}

    confirm_delete: BoolProperty(
        name="Подтвердить удаление",
        description="Подтвердить удаление текстур с диска",
        default=False
    )

    def invoke(self, context, event):
        scene = context.scene

        # Находим выбранные наборы
        selected_sets = [tex_set for tex_set in scene.baker_texture_sets if tex_set.is_selected_for_atlas]

        if not selected_sets:
            self.report({'WARNING'}, "Не выбрано ни одного набора для удаления")
            return {'CANCELLED'}

        # Показываем диалог подтверждения
        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        # Находим выбранные наборы для отображения информации
        selected_sets = [tex_set for tex_set in scene.baker_texture_sets if tex_set.is_selected_for_atlas]
        selected_count = len(selected_sets)

        # Заголовок предупреждения
        box = layout.box()
        box.label(text="ВНИМАНИЕ: Необратимое действие!", icon='ERROR')

        # Описание действия
        col = layout.column()
        col.label(text="Это действие навсегда удалит с диска:")
        col.separator()

        # Список удаляемых наборов
        if selected_count <= 5:
            for tex_set in selected_sets:
                col.label(text=f"• Набор: {tex_set.name}", icon='FILE_FOLDER')
        else:
            for i, tex_set in enumerate(selected_sets[:3]):
                col.label(text=f"• Набор: {tex_set.name}", icon='FILE_FOLDER')
            col.label(text=f"• ... и ещё {selected_count - 3} наборов", icon='FILE_FOLDER')

        col.separator()
        col.label(text="• Все связанные текстуры (.png файлы)")
        col.label(text="• Материалы из файла Blender")

        # Предупреждение
        box = layout.box()
        box.label(text="Это действие НЕЛЬЗЯ отменить!", icon='CANCEL')

        # Чекбокс подтверждения
        layout.prop(self, "confirm_delete", text="Я понимаю последствия и подтверждаю удаление")

    def execute(self, context):
        # Проверяем подтверждение пользователя
        if not self.confirm_delete:
            self.report({'WARNING'}, "Удаление не подтверждено. Действие отменено.")
            return {'CANCELLED'}

        # Выполняем удаление
        return self._execute_deletion(context)

    def _execute_deletion(self, context):
        scene = context.scene
        deleted_count = 0
        materials_deleted = 0

        # Собираем индексы выбранных наборов для безопасного удаления
        indices_to_remove = []
        for i, tex_set in enumerate(scene.baker_texture_sets):
            if tex_set.is_selected_for_atlas:
                indices_to_remove.append(i)

        # Сортируем в обратном порядке для безопасного удаления
        indices_to_remove.sort(reverse=True)

        for index in indices_to_remove:
            tex_set = scene.baker_texture_sets[index]
            try:
                # Удаляем папку с текстурами
                if os.path.exists(tex_set.output_path):
                    import shutil
                    shutil.rmtree(tex_set.output_path)
                    print(f"🗑️ Удалена папка: {tex_set.output_path}")

                # Удаляем материал из bpy.data.materials
                if tex_set.material_name:
                    material = bpy.data.materials.get(tex_set.material_name)
                    if material:
                        bpy.data.materials.remove(material)
                        materials_deleted += 1
                        print(f"🗑️ Удален материал: {tex_set.material_name}")

                # Удаляем набор из списка по индексу
                scene.baker_texture_sets.remove(index)
                deleted_count += 1

            except Exception as e:
                self.report({'ERROR'}, f"Ошибка при удалении набора '{tex_set.name if 'tex_set' in locals() else 'неизвестный'}': {str(e)}")
                continue

        if deleted_count > 0:
            self.report({'INFO'}, f"Удалено {deleted_count} наборов текстур и {materials_deleted} материалов")
        else:
            self.report({'WARNING'}, "Не удалось удалить ни одного набора")

        return {'FINISHED'}

# ===== ATLAS HELPER FUNCTIONS =====

def calculate_atlas_packing_layout(texture_sets, atlas_size):
    """Рассчитывает расположение текстур в атласе с сохранением оригинальных размеров"""
    
    total_area = sum(tex_set.resolution * tex_set.resolution for tex_set in texture_sets)
    atlas_area = atlas_size * atlas_size
    
    if total_area > atlas_area:
        raise Exception(f"❌ Общая площадь текстур ({total_area}px²) превышает площадь атласа ({atlas_area}px²)")
    
    sorted_sets = sorted(texture_sets, key=lambda x: x.resolution * x.resolution, reverse=True)
    
    layout = pack_atlas_rectangles(sorted_sets, atlas_size)
    
    if not layout:
        raise Exception("❌ Не удалось разместить все текстуры в атласе")
    
    for item in layout:
        res = item['texture_set'].resolution
    
    return layout

def pack_atlas_rectangles(texture_sets, atlas_size):
    """Упаковывает прямоугольники (текстуры) в атлас методом Guillotine"""
    # Сортируем текстуры по убыванию размера для лучшей упаковки
    texture_sets = sorted(texture_sets, key=lambda x: x.resolution, reverse=True)

    layout = []
    # Список свободных прямоугольников: [{'x': x, 'y': y, 'width': w, 'height': h}, ...]
    free_rects = [{'x': 0, 'y': 0, 'width': atlas_size, 'height': atlas_size}]

    for tex_set in texture_sets:
        size = tex_set.resolution
        placed = False

        # Ищем наиболее подходящий свободный прямоугольник
        best_rect_idx = -1
        best_score = float('inf')
        best_fit = None

        for i, rect in enumerate(free_rects):
            if rect['width'] >= size and rect['height'] >= size:
                # Вычисляем "отходы" - насколько плохо подходит прямоугольник
                waste_width = rect['width'] - size
                waste_height = rect['height'] - size
                score = waste_width * waste_height  # Минимизируем площадь отходов

                if score < best_score:
                    best_score = score
                    best_rect_idx = i
                    best_fit = rect

        if best_rect_idx != -1:
            # Размещаем текстуру
            rect = best_fit
            x, y = rect['x'], rect['y']

            layout.append({
                'texture_set': tex_set,
                'x': x,
                'y': y,
                'width': size,
                'height': size,
                'u_min': x / atlas_size,
                'v_min': y / atlas_size,
                'u_max': (x + size) / atlas_size,
                'v_max': (y + size) / atlas_size
            })

            # Удаляем использованный прямоугольник
            del free_rects[best_rect_idx]

            # Создаем два новых свободных прямоугольника (Guillotine split)
            # Правый прямоугольник
            if rect['width'] > size:
                free_rects.append({
                    'x': x + size,
                    'y': y,
                    'width': rect['width'] - size,
                    'height': size
                })

            # Нижний прямоугольник
            if rect['height'] > size:
                free_rects.append({
                    'x': x,
                    'y': y + size,
                    'width': rect['width'],
                    'height': rect['height'] - size
                })

            placed = True

        if not placed:
            return None  # Нет подходящего места

    return layout

# ===== UDIM HELPER FUNCTIONS =====

def cleanup_unused_images_static():
    print("\n🧹 Очистка неиспользуемых изображений...")
    used_images = set()
    for material in bpy.data.materials:
        if material.use_nodes:
            for node in material.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    used_images.add(node.image)
    removed_count = 0
    for image in list(bpy.data.images):
        if image not in used_images:
            print(f"  Удаление неиспользуемой текстуры: {image.name}")
            bpy.data.images.remove(image)
            removed_count += 1
    print(f"Итого удалено: {removed_count}")

# ===== UDIM HELPER FUNCTIONS =====

def process_object_name(obj_name):
    """Обрабатывает имя объекта для получения ADDRESS и типа (Main/Flora/Ground/GroundEl)"""
    if not obj_name.startswith("SM_"):
        raise Exception("Имя объекта должно начинаться с 'SM_'")
    
    # Убираем префикс SM_
    name_parts = obj_name[3:].split('_')
    
    if len(name_parts) < 2:
        raise Exception("Неверный формат имени объекта. Ожидается: SM_ADDRESS_ObjectType")
    
    # Последняя часть должна быть Main, Flora, Ground или GroundEl
    obj_type = name_parts[-1]
    if obj_type not in ['Main', 'Flora', 'Ground', 'GroundEl']:
        raise Exception("Имя объекта должно заканчиваться на '_Main', '_Flora', '_Ground' или '_GroundEl'")
    
    # ADDRESS - это всё между SM_ и _ObjectType
    address = '_'.join(name_parts[:-1])
    
    return address, obj_type

def get_udim_texture_name(address, obj_type, tex_type, udim_number):
    """Генерирует имя UDIM текстуры в зависимости от типа объекта"""
    if obj_type == 'Main':
        return f"T_{address}_{tex_type}_1.{udim_number:04d}.png"
    else:  # Ground
        return f"T_{address}_Ground_{tex_type}_1.{udim_number:04d}.png"

def get_udim_material_name(address, obj_type):
    """Генерирует имя UDIM материала"""
    if obj_type == 'Main':
        return f"M_{address}_Main_1"
    else:  # Ground
        return f"M_{address}_Ground_1"

def find_materials_using_textures(texture_paths):
    """Находит материалы, которые используют указанные текстуры"""
    materials_to_clear = []

    for material in bpy.data.materials:
        if not material.use_nodes or not material.node_tree:
            continue

        for node in material.node_tree.nodes:
            if node.type == 'TEX_IMAGE' and node.image:
                # Проверяем, использует ли этот нод одну из переименовываемых текстур
                for texture_path in texture_paths:
                    # Преобразуем UNC пути к полным путям для корректного сравнения
                    node_filepath = node.image.filepath
                    if node_filepath.startswith('//'):
                        node_filepath = bpy.path.abspath(node_filepath)

                    if node_filepath and os.path.normpath(node_filepath) == os.path.normpath(texture_path):
                        if material not in materials_to_clear:
                            materials_to_clear.append(material)
                            # Сохраняем имя материала сразу, на случай если он будет удален позже
                            material_name = material.name
                            print(f"  🎨 Найден материал '{material_name}', использующий текстуру: {os.path.basename(texture_path)}")
                        break

    return materials_to_clear

def delete_materials_using_textures(materials, texture_paths):
    """Полностью удаляет материалы, которые используют указанные текстуры"""
    deleted_count = 0

    # Создаем копию списка, чтобы избежать проблем при итерации во время удаления
    materials_to_delete = materials.copy()

    for material in materials_to_delete:
        # Сохраняем имя материала перед удалением, так как после удаления объект станет недействительным
        material_name = material.name
        try:
            # Удаляем материал из bpy.data.materials
            bpy.data.materials.remove(material)
            deleted_count += 1
            print(f"    🗑️ Полностью удален материал: '{material_name}'")
        except Exception as e:
            print(f"    ❌ Ошибка удаления материала '{material_name}': {e}")

    print(f"✅ Удалено {deleted_count} материалов из файла")
    return deleted_count

def rename_existing_udim_files_to_temp(udim_dir, address, obj_type, max_udim_count=99):
    """Переименовывает существующие UDIM файлы в TEMP файлы и удаляет материалы"""
    print("🔄 Переименование существующих UDIM файлов в TEMP...")

    temp_files = []
    import os

    # Собираем список всех файлов, которые будут переименованы
    files_to_rename = []
    for i in range(max_udim_count):
        udim_number = 1001 + i
        for tex_type in ['Diffuse', 'ERM', 'Normal']:
            udim_filename = get_udim_texture_name(address, obj_type, tex_type, udim_number)
            udim_path = udim_dir / udim_filename
            if udim_path.exists():
                files_to_rename.append(str(udim_path))

    # Находим материалы, которые используют эти текстуры
    if files_to_rename:
        materials_to_delete = find_materials_using_textures(files_to_rename)
        if materials_to_delete:
            print(f"🗑️ Удаление {len(materials_to_delete)} материалов, использующих переименовываемые текстуры...")
            delete_materials_using_textures(materials_to_delete, files_to_rename)

    # Теперь переименовываем файлы
    for i in range(max_udim_count):
        udim_number = 1001 + i

        # Проверяем каждый тип текстуры
        for tex_type in ['Diffuse', 'ERM', 'Normal']:
            udim_filename = get_udim_texture_name(address, obj_type, tex_type, udim_number)
            udim_path = udim_dir / udim_filename

            if udim_path.exists():
                # Создаем TEMP имя файла
                temp_filename = f"{udim_filename}.TEMP"
                temp_path = udim_dir / temp_filename

                try:
                    # Переименовываем файл
                    udim_path.rename(temp_path)
                    temp_files.append(str(temp_path))
                    print(f"  📁 {udim_filename} -> {temp_filename}")
                except Exception as e:
                    print(f"  ❌ Ошибка переименования {udim_filename}: {e}")

    print(f"✅ Переименовано {len(temp_files)} файлов в TEMP")
    return temp_files

def cleanup_temp_files(temp_files):
    """Удаляет TEMP файлы"""
    print("🧹 Удаление TEMP файлов...")

    deleted_count = 0
    for temp_path in temp_files:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
                deleted_count += 1
                print(f"  🗑️ Удален: {os.path.basename(temp_path)}")
        except Exception as e:
            print(f"  ❌ Ошибка удаления {os.path.basename(temp_path)}: {e}")

    print(f"✅ Удалено {deleted_count} TEMP файлов")

def get_next_material_sequence_number():
    """Возвращает следующий свободный номер последовательности материалов"""
    existing_numbers = set()
    
    # Ищем все материалы с паттерном M_{number}_{udim}
    for mat in bpy.data.materials:
        if mat.name.startswith('M_'):
            parts = mat.name.split('_')
            if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
                existing_numbers.add(int(parts[1]))
    
    # Возвращаем первый свободный номер начиная с 1
    sequence_num = 1
    while sequence_num in existing_numbers:
        sequence_num += 1
    
    return sequence_num

def get_udim_directory_name(address, obj_type):
    """Генерирует имя папки для UDIM текстур"""
    if obj_type == 'Main':
        return f"SM_{address}"
    else:  # Ground
        return f"SM_{address}_Ground"

# ===== ATLAS NAMING FUNCTIONS =====

def try_get_active_object_info(context):
    """Пытается получить информацию об активном объекте для именования атласа
    Возвращает (address, obj_type) или (None, None) если объект не в нужном формате"""
    try:
        active_obj = context.active_object
        if not active_obj:
            return None, None
        
        address, obj_type = process_object_name(active_obj.name)
        return address, obj_type
    except:
        # Если объект не в формате SM_Address_Type, возвращаем None
        return None, None

def get_atlas_names_low_format(address, obj_type, atlas_size, texture_sets_count):
    """Генерирует имена в новом формате для LOW атласа"""
    # Базовое имя атласа: используем address и obj_type 
    atlas_base_name = f"Atlas_LOW_{address}_{obj_type}_{atlas_size}_{texture_sets_count}sets"
    
    # Имя материала: M_Address_obj_type_1
    material_name = f"M_{address}_{obj_type}_1"
    
    return atlas_base_name, material_name

def get_texture_filename_low_format(address, obj_type, texture_type):
    """Генерирует имя файла текстуры в новом формате для LOW атласа"""
    # Маппинг типов текстур на краткие обозначения
    texture_type_mapping = {
        'DIFFUSE': 'd',
        'ROUGHNESS': 'r', 
        'METALLIC': 'm',
        'NORMAL': 'n',
        'OPACITY': 'o'
    }
    
    tex_suffix = texture_type_mapping.get(texture_type, texture_type.lower())
    return f"T_{address}_{obj_type}_{tex_suffix}_1.png"

def find_texture_node_in_chain(node):
    """Рекурсивно ищет текстурный узел в цепочке нодов"""
    if not node:
        return None
    
    if node.type == 'TEX_IMAGE':
        return node
    
    # Проверяем все входы узла
    for input_socket in node.inputs:
        if input_socket.links:
            for link in input_socket.links:
                result = find_texture_node_in_chain(link.from_node)
                if result:
                    return result
    return None

def is_erm_setup(separate_node, principled):
    """Проверяет, является ли Separate нода частью ERM сетапа"""
    if not separate_node or not principled:
        return False
    
    # Проверяем тип ноды (может быть как Separate RGB, так и Separate Color)
    if separate_node.type not in ['SEPARATE_RGB', 'SEPARATE_COLOR']:
        return False
    
    # Счетчик правильных подключений
    valid_connections = 0
    
    # Проверяем выходы Separate ноды
    for output_idx, output in enumerate(separate_node.outputs):
        if not output.links:
            continue
        
        for link in output.links:
            current_node = link.to_node
            current_socket = link.to_socket
            
            # Проверяем цепочку нодов до Principled BSDF
            while current_node:
                if current_node == principled:
                    socket_name = current_socket.name.lower()
                    # Проверяем соответствие каналов
                    if ((output_idx == 0 and ('emission' in socket_name or 'strength' in socket_name)) or
                        (output_idx == 1 and 'rough' in socket_name) or
                        (output_idx == 2 and 'metal' in socket_name)):
                        valid_connections += 1
                    break
                
                # Проверяем следующую связь в цепочке
                if current_node.outputs and current_node.outputs[0].links:
                    next_link = current_node.outputs[0].links[0]
                    current_node = next_link.to_node
                    current_socket = next_link.to_socket
                else:
                    break
    
    # Возвращаем True, если найдено хотя бы 1 правильных подключения
    return valid_connections >= 1

def find_all_texture_nodes_in_material(material):
    """Находит все текстурные узлы в материале"""
    texture_nodes = {'Diffuse': None, 'Normal': None, 'ERM': None}
    
    if not material or not material.node_tree:
        return texture_nodes
    
    nodes = material.node_tree.nodes
    
    # Ищем Principled BSDF
    principled = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if not principled:
        return texture_nodes
    
    # Base Color (Diffuse)
    if principled.inputs['Base Color'].links:
        texture_nodes['Diffuse'] = find_texture_node_in_chain(principled.inputs['Base Color'].links[0].from_node)
    
    # Normal map - проверяем все варианты подключения
    # 1. Через Normal Map ноду
    normal_map = None
    for node in nodes:
        if node.type == 'NORMAL_MAP':
            normal_map = node
            if node.inputs['Color'].links:
                texture_nodes['Normal'] = find_texture_node_in_chain(node.inputs['Color'].links[0].from_node)
                break
    
    # 2. Напрямую к входу Normal в Principled BSDF (если не нашли через Normal Map)
    if not texture_nodes['Normal'] and principled.inputs['Normal'].links:
        direct_normal = find_texture_node_in_chain(principled.inputs['Normal'].links[0].from_node)
        if direct_normal:
            texture_nodes['Normal'] = direct_normal
    
    # 3. DirectX нормаль с инвертированием зеленого канала через Math ноды
    if not texture_nodes['Normal']:
        for node in nodes:
            if node.type == 'NORMAL_MAP':
                # Проверяем есть ли Math нода, подключенная к Normal Map
                if node.inputs['Color'].links:
                    from_node = node.inputs['Color'].links[0].from_node
                    # Ищем Math ноду с операцией инверсии зеленого канала
                    if from_node.type == 'SEPARATE_RGB':
                        # Проверяем подключение зеленого канала через Math subtract
                        for output in from_node.outputs:
                            if output.name == 'G' and output.links:  # Зеленый канал
                                for link in output.links:
                                    math_node = link.to_node
                                    if (math_node.type == 'MATH' and 
                                        hasattr(math_node, 'operation') and 
                                        math_node.operation == 'SUBTRACT'):
                                        # Нашли Math ноду с инверсией, ищем текстуру
                                        if from_node.inputs[0].links:
                                            texture_nodes['Normal'] = find_texture_node_in_chain(from_node.inputs[0].links[0].from_node)
                                            if texture_nodes['Normal']:
                                                break
                if texture_nodes['Normal']:
                    break
    
    # Ищем ERM текстуру через Separate Color/RGB ноду
    for node in nodes:
        if node.type in ['SEPARATE_RGB', 'SEPARATE_COLOR']:
            if is_erm_setup(node, principled):
                # Если нашли подходящую Separate ноду, ищем подключенную к ней текстуру
                if node.inputs[0].links:  # Для Separate Color или если node.inputs['Image'].links для Separate RGB
                    erm_texture = find_texture_node_in_chain(node.inputs[0].links[0].from_node)
                    if erm_texture:
                        texture_nodes['ERM'] = erm_texture
                        break
    
    return texture_nodes

def get_image_filepath_static(texture_node):
    """Получает путь к файлу изображения из текстурного узла"""
    if not texture_node or texture_node.type != 'TEX_IMAGE':
        return None
        
    image = texture_node.image
    if not image:
        return None
        
    # Получаем абсолютный путь к файлу
    filepath = bpy.path.abspath(image.filepath)
    return filepath if filepath else None

def get_object_texture_sets_for_udim(context, obj):
    """Сканирует материалы объекта и возвращает подходящие для UDIM"""
    available_sets = []
    
    print(f"🔍 Сканирование материалов объекта {obj.name}...")
    
    if not obj.material_slots:
        print("   ❌ У объекта нет материалов")
        return available_sets
    
    for slot_index, slot in enumerate(obj.material_slots):
        material = slot.material
        if not material:
            print(f"   ⚠️ Пустой слот материала {slot_index}")
            continue
            
        print(f"   🔍 Анализ материала: {material.name}")
        
        # Используем существующую функцию для поиска текстур в материале
        texture_nodes = find_all_texture_nodes_in_material(material)
        
        # Проверяем, есть ли нужные текстуры для HIGH типа
        diffuse_node = texture_nodes.get('Diffuse')
        erm_node = texture_nodes.get('ERM')
        normal_node = texture_nodes.get('Normal')
        
        print(f"      Найденные текстуры:")
        print(f"         Diffuse: {'✓' if diffuse_node else '✗'}")
        print(f"         ERM: {'✓' if erm_node else '✗'}")
        print(f"         Normal: {'✓' if normal_node else '✗'}")
        
        if not (diffuse_node and erm_node and normal_node):
            print(f"      ❌ Материал не содержит всех необходимых текстур")
            continue
        
        # Получаем пути к файлам текстур
        diffuse_path = get_image_filepath_static(diffuse_node)
        erm_path = get_image_filepath_static(erm_node)
        normal_path = get_image_filepath_static(normal_node)
        
        print(f"      Пути к файлам:")
        print(f"         Diffuse: {diffuse_path}")
        print(f"         ERM: {erm_path}")
        print(f"         Normal: {normal_path}")
        
        # Проверяем, что все файлы существуют
        if not all([diffuse_path, erm_path, normal_path]):
            print(f"      ❌ Не все текстуры имеют валидные пути")
            continue
            
        if not all([os.path.exists(diffuse_path), os.path.exists(erm_path), os.path.exists(normal_path)]):
            print(f"      ❌ Не все файлы текстур найдены на диске")
            continue
        
        # Создаем простой объект для хранения информации о материале
        material_info = {
            'name': f"Material_{material.name}",
            'material_name': material.name,
            'material_index': slot_index,
            'diffuse_path': diffuse_path,
            'erm_path': erm_path,
            'normal_path': normal_path,
            'output_path': os.path.dirname(diffuse_path)
        }
        
        available_sets.append(material_info)
        print(f"      ✅ Материал подходит для UDIM: {material.name}")
    
    print(f"Всего найдено подходящих материалов: {len(available_sets)}")
    return available_sets

def setup_udim_material_nodes(material):
    """Настраивает базовые ноды для UDIM материала"""
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    
    # Очищаем существующие ноды
    nodes.clear()
    
    # Создаем Principled BSDF
    bsdf = nodes.new('ShaderNodeBsdfPrincipled')
    bsdf.location = (0, 0)
    
    # Создаем Material Output
    output = nodes.new('ShaderNodeOutputMaterial')
    output.location = (300, 0)
    
    # Соединяем BSDF с выходом
    links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
    
    return nodes, links, bsdf

def move_uvs_to_udim_tiles(obj, udim_dir=None):
    """Перемещает UV координаты в UDIM тайлы в зависимости от материала полигона"""
    if not obj.data.uv_layers:
        print("У объекта нет UV слоев")
        return
    
    if not obj.data.polygons:
        print("У объекта нет полигонов")
        return
    
    print(f"\n📐 Перемещение UV координат в UDIM тайлы для объекта: {obj.name}")

    # Сохраняем индексы материалов объекта перед перемещением UV
    material_indices = {}
    for i, slot in enumerate(obj.material_slots):
        if slot.material:
            material_indices[i] = slot.material.name
            print(f"   📝 Сохранен материал '{slot.material.name}' с индексом {i}")

    # Сохраняем информацию об индексах в сцене
    scene = bpy.context.scene
    if hasattr(scene, 'baker_object_material_indices'):
        # Удаляем старую информацию для этого объекта
        indices_to_remove = []
        for i, mat_indices in enumerate(scene.baker_object_material_indices):
            if mat_indices.object_name == obj.name:
                indices_to_remove.append(i)

        for i in reversed(indices_to_remove):
            scene.baker_object_material_indices.remove(i)

        # Добавляем новую информацию
        obj_indices = scene.baker_object_material_indices.add()
        obj_indices.object_name = obj.name
        indices_str = ','.join([f"{idx}:{name}" for idx, name in material_indices.items()])
        obj_indices.material_indices = indices_str
        print(f"   💾 Индексы материалов сохранены: {indices_str}")

    original_active = bpy.context.view_layer.objects.active
    original_selected = [o for o in bpy.context.selected_objects]
    original_mode = bpy.context.mode

    try:
        # Настраиваем контекст
        if bpy.context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        
        # Переходим в Edit Mode
        bpy.ops.object.mode_set(mode='EDIT')
        
        import bmesh
        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        
        if not bm.loops.layers.uv:
            print("⚠️  Нет UV слоя в bmesh")
            return
        
        uv_layer_bmesh = bm.loops.layers.uv.active
        processed_faces = {}

        # Сканируем UDIM текстуры и создаем маппинг индексов материалов на UDIM номера
        material_to_udim = {}
        udim_tiles_found = set()

        if udim_dir and os.path.exists(udim_dir):
            # Сканируем файлы в UDIM папке для определения номеров тайлов
            print(f"   🔍 Сканирование UDIM папки: {udim_dir}")
            import re

            for filename in os.listdir(udim_dir):
                if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.tga', '.tiff', '.exr')):
                    # Ищем UDIM номер в имени файла (4 цифры перед расширением файла)
                    match = re.search(r'\.(\d{4})\.(png|jpg|jpeg|tga|tiff|exr)$', filename.lower())
                    if match:
                        udim_number = int(match.group(1))
                        if 1001 <= udim_number <= 1999:  # Валидный UDIM диапазон
                            udim_tiles_found.add(udim_number)
                            print(f"     Найден UDIM файл: {filename} -> UDIM {udim_number}")

            print(f"   📊 Найдено UDIM номеров: {sorted(udim_tiles_found)}")
        else:
            print(f"   ⚠️ UDIM папка не найдена или не указана: {udim_dir}")

        # Сортируем найденные UDIM номера и создаем маппинг
        sorted_udims = sorted(udim_tiles_found)
        for i, udim_num in enumerate(sorted_udims):
            if i < len(obj.material_slots):
                material_to_udim[i] = udim_num

        print(f"   🔢 Маппинг индексов материалов на UDIM номера:")
        for mat_idx, udim_num in material_to_udim.items():
            print(f"      Материал {mat_idx} -> UDIM {udim_num}")

        # Проходим по всем полигонам
        for face in bm.faces:
            material_index = face.material_index

            # Получаем UDIM номер для этого индекса материала
            if material_index in material_to_udim:
                udim_number = material_to_udim[material_index]
            else:
                # Если индекс не найден, используем fallback логику
                udim_number = 1001 + material_index
                print(f"   ⚠️ Материал с индексом {material_index} не найден в маппинге, используем UDIM {udim_number}")

            # Вычисляем смещение UDIM тайла (UDIM - 1001)
            udim_offset = udim_number - 1001
            udim_offset_u = udim_offset % 10  # Горизонтальное смещение (0-9)
            udim_offset_v = udim_offset // 10  # Вертикальное смещение
            
            # Перемещаем UV координаты для каждой вершины полигона
            for loop in face.loops:
                uv = loop[uv_layer_bmesh].uv
                uv.x += udim_offset_u
                uv.y += udim_offset_v
            
            # Ведем статистику
            if material_index not in processed_faces:
                processed_faces[material_index] = 0
            processed_faces[material_index] += 1
        
        # Обновляем меш
        bmesh.update_edit_mesh(obj.data)
        
        print("📊 Статистика перемещения UV координат:")
        for mat_index, count in processed_faces.items():
            if count > 0:
                udim_number = material_to_udim.get(mat_index, 1001 + mat_index)
                print(f"  UDIM тайл {udim_number}: перемещено {count} полигонов (материал {mat_index})")
        
        print("✅ Перемещение UV координат завершено")
        
    except Exception as e:
        print(f"❌ Ошибка при перемещении UV координат: {str(e)}")
        raise
    finally:
        try:
            # Возвращаем в Object Mode
            if bpy.context.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            
            # Восстанавливаем оригинальное состояние
            bpy.ops.object.select_all(action='DESELECT')
            for selected_obj in original_selected:
                if selected_obj and selected_obj.name in bpy.data.objects:
                    selected_obj.select_set(True)
            
            if original_active and original_active.name in bpy.data.objects:
                bpy.context.view_layer.objects.active = original_active
                
        except Exception as restore_error:
            print(f"⚠️  Предупреждение при восстановлении состояния: {restore_error}")

def save_image_with_format_udim(image, filepath):
    """Сохраняет изображение с контролем формата через настройки рендера"""
    print(f"\nСохранение изображения: {os.path.basename(filepath)}")
    print(f"Полный путь: {filepath}")
    
    if not image:
        print("ОШИБКА: Изображение не определено")
        return False
    
    # Проверяем директорию назначения
    target_dir = os.path.dirname(filepath)
    if not os.path.exists(target_dir):
        try:
            os.makedirs(target_dir, exist_ok=True)
            print(f"Создана директория: {target_dir}")
        except Exception as e:
            print(f"ОШИБКА при создании директории: {str(e)}")
            return False
    
    # Сохраняем текущие настройки рендера и изображения
    current_settings = {
        'file_format': bpy.context.scene.render.image_settings.file_format,
        'color_mode': bpy.context.scene.render.image_settings.color_mode,
        'color_depth': bpy.context.scene.render.image_settings.color_depth,
        'compression': bpy.context.scene.render.image_settings.compression,
        'view_settings': bpy.context.scene.view_settings.view_transform,
        'look': bpy.context.scene.view_settings.look,
        'exposure': bpy.context.scene.view_settings.exposure,
        'gamma': bpy.context.scene.view_settings.gamma,
        'image_colorspace': image.colorspace_settings.name if image else None
    }
    
    try:
        # Настраиваем формат PNG
        bpy.context.scene.render.image_settings.file_format = 'PNG'
        bpy.context.scene.render.image_settings.compression = 15
        
        # Определяем наличие альфа-канала
        has_alpha = image.depth == 32 and image.channels == 4
        
        # Устанавливаем цветовой режим
        bpy.context.scene.render.image_settings.color_mode = 'RGBA' if has_alpha else 'RGB'
        print(f"Установлен режим {'RGBA' if has_alpha else 'RGB'} (Alpha: {has_alpha})")
        
        # Устанавливаем глубину цвета 8 бит на канал
        bpy.context.scene.render.image_settings.color_depth = '8'
        
        # Устанавливаем sRGB для сохранения файлов на диск
        image.colorspace_settings.name = 'sRGB'
        
        # Настраиваем параметры отображения для корректного сохранения
        bpy.context.scene.view_settings.view_transform = 'Standard'
        bpy.context.scene.view_settings.look = 'None'
        bpy.context.scene.view_settings.exposure = 0
        bpy.context.scene.view_settings.gamma = 1
        
        # Сохраняем изображение
        print(f"Сохранение в цветовом пространстве: {image.colorspace_settings.name}")
        image.save_render(filepath)
        
        # Проверяем, что файл создан
        if os.path.exists(filepath):
            print(f"Файл успешно сохранен: {filepath}")
            return True
        else:
            print(f"ОШИБКА: Файл не был создан: {filepath}")
            return False
            
    except Exception as e:
        print(f"Общая ошибка при сохранении: {str(e)}")
        return False
        
    finally:
        # Восстанавливаем настройки рендера и изображения
        bpy.context.scene.render.image_settings.file_format = current_settings['file_format']
        bpy.context.scene.render.image_settings.color_mode = current_settings['color_mode']
        bpy.context.scene.render.image_settings.color_depth = current_settings['color_depth']
        bpy.context.scene.render.image_settings.compression = current_settings['compression']
        bpy.context.scene.view_settings.view_transform = current_settings['view_settings']
        bpy.context.scene.view_settings.look = current_settings['look']
        bpy.context.scene.view_settings.exposure = current_settings['exposure']
        bpy.context.scene.view_settings.gamma = current_settings['gamma']
        if current_settings['image_colorspace']:
            image.colorspace_settings.name = current_settings['image_colorspace']

# ===== UDIM VALIDATION HELPER =====
# def validate_udim_sets_static(texture_sets):
#     """Проверяет, что каждый элемент имеет пути к DIFFUSE/ERM/NORMAL и что файлы существуют."""
#     if not texture_sets:
#         return False
#     required = ('diffuse_path', 'erm_path', 'normal_path')
#     for info in texture_sets:
#         if not all(k in info and info.get(k) for k in required):
#             print(f"Материал {info.get('material_name')} не содержит всех необходимых путей")
#             return False
#         if not all(os.path.exists(info.get(k)) for k in required):
#             print(f"Файлы некоторых текстур не найдены для материала {info.get('material_name')}")
#             return False
#     return True

# ===== UDIM STATIC FALLBACKS =====
# def create_udim_directory_static(address, obj_type):
#     blend_path = bpy.data.filepath
#     if not blend_path:
#         print("Ошибка: Сначала сохраните blend файл")
#         return None
#     base_dir = Path(blend_path).parent
#     udim_dir_name = get_udim_directory_name(address, obj_type)
#     udim_dir = base_dir / udim_dir_name
#     try:
#         udim_dir.mkdir(exist_ok=True, parents=True)
#         print(f"Создана папка для UDIM: {udim_dir}")
#         return udim_dir
#     except Exception as e:
#         print(f"Ошибка при создании папки UDIM: {str(e)}")
#         return None

# def create_udim_material_and_textures_static(context, obj, texture_sets, udim_dir, address, obj_type):
# Закомментированы статические UDIM функции для тестирования основного функционала
"""
    material_name = get_udim_material_name(address, obj_type)
    udim_material = bpy.data.materials.new(name=material_name)
    nodes, links, bsdf = setup_udim_material_nodes(udim_material)
    texture_info = {'Diffuse': {'files': [], 'node': None}, 'ERM': {'files': [], 'node': None}, 'Normal': {'files': [], 'node': None}}
    for i, material_info in enumerate(texture_sets):
        udim_number = 1001 + i
        for tex_type in ['Diffuse', 'ERM', 'Normal']:
            if tex_type == 'Diffuse':
                source_path = material_info.get('diffuse_path')
            elif tex_type == 'ERM':
                source_path = material_info.get('erm_path')
            else:
                source_path = material_info.get('normal_path')
            if source_path and os.path.exists(source_path):
                udim_filename = get_udim_texture_name(address, obj_type, tex_type, udim_number)
                udim_path = udim_dir / udim_filename
                try:
                    source_image = bpy.data.images.load(source_path)
                    if save_image_with_format_udim(source_image, str(udim_path)):
                        texture_info[tex_type]['files'].append((udim_number, str(udim_path)))
                    bpy.data.images.remove(source_image)
                except Exception as e:
                    print(f"❌ Ошибка при создании UDIM {tex_type}: {str(e)}")
                    if 'source_image' in locals():
                        bpy.data.images.remove(source_image)
    for tex_type, info in texture_info.items():
        if not info['files']:
            continue
        try:
            tex_node = nodes.new('ShaderNodeTexImage')
            info['node'] = tex_node
            first_image = bpy.data.images.load(info['files'][0][1])
            width, height = first_image.size
            has_alpha = first_image.depth == 32 and first_image.channels == 4
            bpy.data.images.remove(first_image)
            if obj_type == 'Main':
                image_name = f"T_{address}_{tex_type}_UDIM"
                udim_pattern = str(udim_dir / f"T_{address}_{tex_type}_1.<UDIM>.png")
            else:
                image_name = f"T_{address}_Ground_{tex_type}_UDIM"
                udim_pattern = str(udim_dir / f"T_{address}_Ground_{tex_type}_1.<UDIM>.png")
            image = bpy.data.images.new(name=image_name, width=width, height=height, alpha=has_alpha)
            image.source = 'TILED'
            image.filepath = udim_pattern
            tex_node.image = image
            if tex_type in ['Normal', 'ERM']:
                image.colorspace_settings.name = 'Non-Color'
            else:
                image.colorspace_settings.name = 'sRGB'
            if tex_type == 'Diffuse':
                links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])
                if has_alpha and 'Alpha' in tex_node.outputs and 'Alpha' in bsdf.inputs:
                    links.new(tex_node.outputs['Alpha'], bsdf.inputs['Alpha'])
                links.new(tex_node.outputs['Color'], bsdf.inputs['Emission Color'])
            elif tex_type == 'Normal':
                normal_map = nodes.new('ShaderNodeNormalMap')
                normal_map.location = (-300, -100)
                links.new(tex_node.outputs['Color'], normal_map.inputs['Color'])
                links.new(normal_map.outputs['Normal'], bsdf.inputs['Normal'])
            elif tex_type == 'ERM':
                separate_color = nodes.new('ShaderNodeSeparateColor')
                separate_color.location = (-300, -400)
                separate_color.mode = 'RGB'
                links.new(tex_node.outputs['Color'], separate_color.inputs[0])
                links.new(separate_color.outputs[0], bsdf.inputs['Emission Strength'])
                links.new(separate_color.outputs[1], bsdf.inputs['Roughness'])
                links.new(separate_color.outputs[2], bsdf.inputs['Metallic'])
        except Exception as e:
            print(f"Ошибка при настройке текстуры {tex_type}: {str(e)}")
            continue
    return udim_material, texture_info

def validate_udim_files_static(texture_info, udim_dir):
    print("\n🔍 Проверка целостности UDIM файлов...")
    all_valid = True
    for key, info in texture_info.items():
        for udim_number, file_path in info['files']:
            if not os.path.exists(file_path):
                print(f"❌ Файл не найден: {file_path}")
                all_valid = False
    if all_valid:
        print("✅ Все UDIM файлы созданы успешно!")
    else:
        print("❌ Обнаружены проблемы с UDIM файлами")
    return all_valid

def assign_udim_material_to_object_static(obj, udim_material):
    try:
        old_materials = [slot.material for slot in obj.material_slots if slot.material]
        obj.data.materials.clear()
        obj.data.materials.append(udim_material)
        if obj.data.polygons:
            for polygon in obj.data.polygons:
                polygon.material_index = 0
        for mat in old_materials:
            try:
                if mat and mat.name != udim_material.name:
                    bpy.data.materials.remove(mat, do_unlink=True)
            except Exception as e:
                print(f"Не удалось удалить материал {mat.name}: {str(e)}")
        print(f"UDIM материал назначен объекту: {udim_material.name}")
    except Exception as e:
        print(f"Ошибка при назначении UDIM материала: {str(e)}")
        raise

def cleanup_unused_images_static():
    print("\n🧹 Очистка неиспользуемых изображений...")
    used_images = set()
    for material in bpy.data.materials:
        if material.use_nodes:
            for node in material.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    used_images.add(node.image)
    removed_count = 0
    for image in list(bpy.data.images):
        if image not in used_images:
            print(f"  Удаление неиспользуемой текстуры: {image.name}")
            bpy.data.images.remove(image)
            removed_count += 1
    print(f"Итого удалено: {removed_count}")
"""

# ===== UDIM OPERATOR =====

class BAKER_OT_create_udim(Operator):
    """Создает UDIM текстуры из материалов объекта"""
    bl_idname = "baker.create_udim"
    bl_label = "Создать UDIM сет"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            return False
        
        # Проверяем, что у объекта есть материалы
        if not obj.data.materials:
            return False
            
        # Для создания UDIM нам нужны ОБЫЧНЫЕ материалы с текстурами (не UDIM)
        has_regular_textures = False
        for slot in obj.data.materials:
            if slot and slot.use_nodes:
                for node in slot.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image:
                        # Проверяем что это НЕ UDIM текстура
                        if node.image.source != 'TILED':
                            has_regular_textures = True
                            break
            if has_regular_textures:
                break
        
        return has_regular_textures
    
    def execute(self, context):
        try:
            obj = context.active_object
            if not obj:
                self.report({'ERROR'}, "Не выбран активный объект")
                return {'CANCELLED'}
            
            print(f"\n🚀 === СОЗДАНИЕ UDIM СЕТА ===")
            print(f"Обработка объекта: {obj.name}")
            
            # Обрабатываем имя объекта
            try:
                address, obj_type = process_object_name(obj.name)
                print(f"ADDRESS: '{address}', Тип: '{obj_type}'")
            except Exception as e:
                self.report({'ERROR'}, f"Неправильное имя объекта: {str(e)}")
                return {'CANCELLED'}
            
            # Проверяем, что у объекта есть материалы с обычными текстурами
            if not obj.data.materials:
                self.report({'ERROR'}, "У объекта нет материалов")
                return {'CANCELLED'}
            
            print(f"🎨 Создание UDIM материала из обычных материалов...")
            
            # Создаем папку для UDIM 
            udim_dir = self.create_udim_directory(address, obj_type)
            if not udim_dir:
                self.report({'ERROR'}, "Не удалось создать папку для UDIM")
                return {'CANCELLED'}
            
            # Собираем информацию о материалах
            material_data = []
            for i, slot in enumerate(obj.data.materials):
                if slot and slot.use_nodes:
                    # Извлекаем пути к текстурам из материала по подключениям
                    diffuse_path = None
                    erm_path = None  
                    normal_path = None
                    
                    for node in slot.node_tree.nodes:
                        if node.type == 'TEX_IMAGE' and node.image and node.image.filepath:
                            filepath = bpy.path.abspath(node.image.filepath)
                            texture_type = self.detect_texture_type_by_connections(node)
                            
                            if texture_type == 'Diffuse':
                                diffuse_path = filepath
                                print(f"    🎨 Найдена Diffuse: {os.path.basename(filepath)}")
                            elif texture_type == 'ERM':
                                erm_path = filepath
                                print(f"    ⚙️ Найдена ERM: {os.path.basename(filepath)}")
                            elif texture_type == 'Normal':
                                normal_path = filepath
                                print(f"    🗺️ Найдена Normal: {os.path.basename(filepath)}")
                    
                    if diffuse_path or erm_path or normal_path:
                        print(f"  Материал {slot.name}:")
                        print(f"    Diffuse: {'✅' if diffuse_path else '❌'}")
                        print(f"    ERM: {'✅' if erm_path else '❌'}")
                        print(f"    Normal: {'✅' if normal_path else '❌'}")
                        
                        material_data.append({
                            'material_name': slot.name,
                            'diffuse_path': diffuse_path,
                            'erm_path': erm_path,
                            'normal_path': normal_path,
                            'output_path': str(udim_dir)
                        })
            
            if not material_data:
                self.report({'ERROR'}, "Не найдено материалов с текстурами")
                return {'CANCELLED'}
            
            print(f"📦 Найдено {len(material_data)} материалов с текстурами")

            # Проверяем, что все материалы объекта подходят для UDIM
            suitable_materials = get_object_texture_sets_for_udim(context, obj)
            total_materials = len([slot for slot in obj.data.materials if slot])

            if len(suitable_materials) != total_materials:
                unsuitable_materials = []
                for slot in obj.data.materials:
                    if slot:
                        material_suitable = any(mat['material_name'] == slot.name for mat in suitable_materials)
                        if not material_suitable:
                            unsuitable_materials.append(slot.name)

                self.report({'ERROR'},
                    f"Не все материалы подходят для UDIM. Подходящих: {len(suitable_materials)}/{total_materials}. "
                    f"Неподходящие материалы: {', '.join(unsuitable_materials)}")
                return {'CANCELLED'}

            # Создаем UDIM материал и текстуры
            udim_material, texture_info = self.create_udim_material_and_textures(
                context, obj, material_data, udim_dir, address, obj_type
            )
            
            if not udim_material:
                self.report({'ERROR'}, "Не удалось создать UDIM материал")
                return {'CANCELLED'}
            
            # Проверяем целостность созданных файлов
            self.validate_udim_files(texture_info, udim_dir)
            
            # Перемещаем UV координаты в UDIM тайлы
            move_uvs_to_udim_tiles(obj, udim_dir)
            
            # Назначаем UDIM материал объекту
            self.assign_udim_material_to_object(obj, udim_material)
            
            # Очищаем неиспользуемые изображения
            self.cleanup_unused_images()
            
            self.report({'INFO'}, f"UDIM сет успешно создан: {udim_material.name}")
            print(f"✅ UDIM сет создан успешно!")
            print(f"📁 Папка: {udim_dir}")
            print(f"🎨 Материал: {udim_material.name}")
            
            return {'FINISHED'}
            
        except Exception as e:
            print(f"❌ Ошибка при создании UDIM сета: {str(e)}")
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, f"Ошибка: {str(e)}")
            return {'CANCELLED'}

    def validate_texture_sets_for_udim(self, texture_sets):
        """Проверяет, что все материалы содержат необходимые текстуры для UDIM"""
        if not texture_sets:
            print("❌ Нет наборов текстур для валидации")
            return False
        
        print(f"🔍 Валидация {len(texture_sets)} наборов текстур для UDIM:")
        
        for i, material_info in enumerate(texture_sets, 1):
            material_name = material_info.get('material_name', 'Неизвестный')
            print(f"  #{i}: {material_name}")
            
            # Получаем пути к файлам из словаря
            diffuse_path = material_info.get('diffuse_path')
            erm_path = material_info.get('erm_path')
            normal_path = material_info.get('normal_path')
            
            print(f"    Diffuse: {diffuse_path}")
            print(f"    ERM: {erm_path}")
            print(f"    Normal: {normal_path}")
            
            if not all([diffuse_path, erm_path, normal_path]):
                print(f"❌ Материал {material_name} не содержит всех необходимых путей")
                print(f"    Diffuse: {'✅' if diffuse_path else '❌'}")
                print(f"    ERM: {'✅' if erm_path else '❌'}")
                print(f"    Normal: {'✅' if normal_path else '❌'}")
                return False
            
            # Проверяем, что файлы существуют
            diffuse_exists = os.path.exists(diffuse_path)
            erm_exists = os.path.exists(erm_path)
            normal_exists = os.path.exists(normal_path)
            
            print(f"    Файлы на диске:")
            print(f"      Diffuse: {'✅' if diffuse_exists else '❌'} {diffuse_path}")
            print(f"      ERM: {'✅' if erm_exists else '❌'} {erm_path}")
            print(f"      Normal: {'✅' if normal_exists else '❌'} {normal_path}")
            
            if not all([diffuse_exists, erm_exists, normal_exists]):
                print(f"❌ Некоторые файлы текстур для материала {material_name} не найдены")
                return False
            
            print(f"    ✅ Материал {material_name} прошел валидацию")
        
        print("✅ Все наборы текстур успешно прошли валидацию")
        return True

    def get_texture_path_from_set(self, material_info, texture_type):
        """Получает путь к текстуре определенного типа из материала"""
        if texture_type == 'diffuse':
            return material_info.get('diffuse_path')
        elif texture_type == 'erm':
            return material_info.get('erm_path')
        elif texture_type == 'normal':
            return material_info.get('normal_path')
        else:
            return None

    def create_udim_directory(self, address, obj_type):
        """Создает директорию для UDIM текстур"""
        print(f"🔧 Создание директории UDIM...")
        print(f"  Address: {address}")
        print(f"  Object Type: {obj_type}")
        
        blend_path = bpy.data.filepath
        print(f"  Blend файл: {blend_path}")
        
        if not blend_path:
            print("❌ Ошибка: Сначала сохраните blend файл")
            return None
        
        base_dir = Path(blend_path).parent
        print(f"  Базовая директория: {base_dir}")
        
        udim_dir_name = get_udim_directory_name(address, obj_type)
        print(f"  Имя UDIM директории: {udim_dir_name}")
        
        udim_dir = base_dir / udim_dir_name
        print(f"  Полный путь UDIM: {udim_dir}")
        
        try:
            udim_dir.mkdir(exist_ok=True, parents=True)
            print(f"✅ Создана папка для UDIM: {udim_dir}")
            return udim_dir
        except Exception as e:
            print(f"❌ Ошибка при создании папки UDIM: {str(e)}")
            import traceback
            traceback.print_exc()
            return None

    def create_udim_material_and_textures(self, context, obj, texture_sets, udim_dir, address, obj_type):
        """Создает UDIM материал и текстуры"""
        print(f"🎨 Создание UDIM материала и текстур...")

        # Переименовываем существующие UDIM файлы в TEMP для предотвращения конфликтов
        max_udim_count = len(texture_sets) + 10  # Добавляем запас на возможные дополнительные тайлы
        temp_files = rename_existing_udim_files_to_temp(udim_dir, address, obj_type, max_udim_count)

        # Корректируем пути к текстурам в texture_sets после переименования в TEMP
        print(f"🔧 Корректировка путей к текстурам после переименования...")
        corrected_count = 0
        for i, material_info in enumerate(texture_sets):
            for tex_type in ['diffuse_path', 'erm_path', 'normal_path']:
                original_path = material_info.get(tex_type)
                if original_path:
                    temp_path = original_path + '.TEMP'
                    if os.path.exists(temp_path):
                        material_info[tex_type] = temp_path
                        corrected_count += 1
                        print(f"  📁 {os.path.basename(original_path)} -> {os.path.basename(temp_path)}")
                    elif os.path.exists(original_path):
                        # TEMP файл не найден, но оригинальный файл существует - оставляем как есть
                        print(f"  ✅ Оригинальный файл найден: {os.path.basename(original_path)}")
                    else:
                        print(f"  ❌ Ни TEMP, ни оригинальный файл не найдены: {os.path.basename(original_path)}")

        print(f"✅ Скорректировано {corrected_count} путей к текстурам")

        # Создаем новый UDIM материал с правильным именем
        material_name = get_udim_material_name(address, obj_type)
        print(f"  Имя материала: {material_name}")
        udim_material = bpy.data.materials.new(name=material_name)
        
        # Настраиваем базовые ноды
        print(f"  Настройка нодов материала...")
        nodes, links, bsdf = setup_udim_material_nodes(udim_material)
        
        # Словари для хранения информации о текстурах
        texture_info = {
            'Diffuse': {'files': [], 'node': None},
            'ERM': {'files': [], 'node': None},
            'Normal': {'files': [], 'node': None}
        }
        
        # Первый проход: копируем и переименовываем текстуры
        print(f"  Копирование и переименование текстур...")
        for i, material_info in enumerate(texture_sets):
            udim_number = 1001 + i
            print(f"    Обработка материала {i}: {material_info.get('material_name')} -> UDIM {udim_number}")
            
            # Обрабатываем каждый тип текстуры
            for tex_type in ['Diffuse', 'ERM', 'Normal']:
                source_path = None
                
                if tex_type == 'Diffuse':
                    source_path = self.get_texture_path_from_set(material_info, 'diffuse')
                elif tex_type == 'ERM':
                    source_path = self.get_texture_path_from_set(material_info, 'erm')
                elif tex_type == 'Normal':
                    source_path = self.get_texture_path_from_set(material_info, 'normal')
                
                if source_path and os.path.exists(source_path):
                    # Создаем имя целевого файла
                    udim_filename = get_udim_texture_name(address, obj_type, tex_type, udim_number)
                    target_path = udim_dir / udim_filename
                    
                    try:
                        # Проверяем, совпадает ли исходный путь с целевым
                        if os.path.abspath(source_path) == os.path.abspath(target_path):
                            # Файл уже в целевой директории, используем как есть
                            texture_info[tex_type]['files'].append(str(target_path))
                            print(f"      {tex_type}: {os.path.basename(source_path)} (уже в UDIM папке)")
                        else:
                            # Копируем файл
                            import shutil
                            shutil.copy2(source_path, target_path)
                            texture_info[tex_type]['files'].append(str(target_path))
                            print(f"      {tex_type}: {os.path.basename(source_path)} -> {udim_filename}")
                    except Exception as e:
                        print(f"      ❌ Ошибка копирования {tex_type}: {e}")
                else:
                    print(f"      ❌ Файл {tex_type} не найден: {source_path}")
        
        # Создаем ноды текстур
        print(f"  Создание нодов текстур...")
        for tex_type, info in texture_info.items():
            if info['files']:
                # Создаем нод Image Texture
                tex_node = nodes.new(type='ShaderNodeTexImage')
                tex_node.label = f'UDIM {tex_type}'
                
                # Расстановка нод
                if tex_type == 'Diffuse':
                    tex_node.location = (-600, 200)
                elif tex_type == 'Normal':
                    tex_node.location = (-600, -100)
                elif tex_type == 'ERM':
                    tex_node.location = (-600, -400)
                
                # Загружаем первую текстуру для создания изображения
                first_texture_path = info['files'][0]
                
                # Определяем базовое имя (без UDIM номера)
                base_name = os.path.basename(first_texture_path)
                base_name = base_name.replace('1001', '<UDIM>')
                
                # Создаем изображение
                img = bpy.data.images.load(first_texture_path)
                img.name = base_name
                img.source = 'TILED'
                
                # Подключаем к ноду
                tex_node.image = img
                info['node'] = tex_node
                
                # Настройка цветового пространства
                if tex_type in ['ERM', 'Normal']:
                    img.colorspace_settings.name = 'Non-Color'
                else:
                    img.colorspace_settings.name = 'sRGB'
                
                print(f"    ✅ Создан нод для {tex_type}")
        
        # Подключаем ноды к материалу
        print(f"  Подключение нодов к материалу...")
        try:
            # Diffuse
            if texture_info['Diffuse']['node']:
                links.new(texture_info['Diffuse']['node'].outputs['Color'], bsdf.inputs['Base Color'])
                links.new(texture_info['Diffuse']['node'].outputs['Alpha'], bsdf.inputs['Alpha'])
                # Подключаем к Emission Color
                links.new(texture_info['Diffuse']['node'].outputs['Color'], bsdf.inputs['Emission Color'])
            
            # ERM (распаковка)
            if texture_info['ERM']['node']:
                separate_color = nodes.new(type='ShaderNodeSeparateColor')
                separate_color.location = (-300, -400)
                separate_color.mode = 'RGB'
                links.new(texture_info['ERM']['node'].outputs['Color'], separate_color.inputs['Color'])
                links.new(separate_color.outputs['Red'], bsdf.inputs['Emission Strength'])
                links.new(separate_color.outputs['Green'], bsdf.inputs['Roughness'])
                links.new(separate_color.outputs['Blue'], bsdf.inputs['Metallic'])
            
            # Normal
            if texture_info['Normal']['node']:
                normal_map = nodes.new(type='ShaderNodeNormalMap')
                normal_map.location = (-300, -100)
                links.new(texture_info['Normal']['node'].outputs['Color'], normal_map.inputs['Color'])
                links.new(normal_map.outputs['Normal'], bsdf.inputs['Normal'])
            
            print(f"  ✅ Ноды подключены к материалу")
        except Exception as e:
            print(f"  ❌ Ошибка подключения нодов: {e}")

        # Очищаем TEMP файлы после завершения всех операций
        cleanup_temp_files(temp_files)

        print(f"✅ UDIM материал создан: {material_name}")
        return udim_material, texture_info

    def assign_udim_material_to_object(self, obj, udim_material):
        """Назначает UDIM материал объекту"""
        print(f"🎯 Назначение UDIM материала объекту...")
        print(f"  Объект: {obj.name}")
        print(f"  Материал: {udim_material.name}")
        
        try:
            # Очищаем существующие материалы
            obj.data.materials.clear()
            
            # Добавляем UDIM материал
            obj.data.materials.append(udim_material)
            
            print(f"✅ UDIM материал назначен объекту")
        except Exception as e:
            print(f"❌ Ошибка назначения UDIM материала: {e}")
            raise

    def validate_udim_files(self, texture_info, udim_dir):
        """Проверяет целостность созданных UDIM файлов"""
        print(f"🔍 Проверка целостности UDIM файлов...")
        
        try:
            all_valid = True
            for tex_type, info in texture_info.items():
                print(f"  Проверка {tex_type}:")
                if info['files']:
                    for file_path in info['files']:
                        if os.path.exists(file_path):
                            file_size = os.path.getsize(file_path)
                            print(f"    ✅ {os.path.basename(file_path)} ({file_size} bytes)")
                        else:
                            print(f"    ❌ Файл не найден: {os.path.basename(file_path)}")
                            all_valid = False
                else:
                    print(f"    ⚠️ Нет файлов для {tex_type}")
            
            print(f"{'✅' if all_valid else '❌'} Проверка файлов завершена")
            return all_valid
        except Exception as e:
            print(f"❌ Ошибка проверки файлов: {e}")
            return False

    def cleanup_unused_images(self):
        """Очищает неиспользуемые изображения из файла"""
        print("🧹 Очистка неиспользуемых изображений...")
        
        try:
            # Собираем все используемые изображения
            used_images = set()
            for material in bpy.data.materials:
                if material.use_nodes:
                    for node in material.node_tree.nodes:
                        if node.type == 'TEX_IMAGE' and node.image:
                            used_images.add(node.image)
            
            # Удаляем неиспользуемые изображения
            removed_count = 0
            for image in list(bpy.data.images):
                if image not in used_images and image.users == 0:
                    bpy.data.images.remove(image)
                    removed_count += 1
            
            print(f"🧹 Удалено {removed_count} неиспользуемых изображений")
        except Exception as e:
            print(f"❌ Ошибка очистки изображений: {e}")

    def detect_texture_type_by_connections(self, node):
        """Определяет тип текстуры по подключениям нода"""
        if not node.type == 'TEX_IMAGE' or not node.image:
            return None
            
        # Анализируем подключения выходов нода
        kinds = set()
        
        # Проверяем все выходы Color
        for output in node.outputs:
            if output.name == 'Color' and output.is_linked:
                for link in output.links:
                    to_node = link.to_node
                    to_socket = link.to_socket
                    
                    # Normal Map
                    if to_node.type == 'NORMAL_MAP':
                        kinds.add('Normal')
                    # Principled BSDF
                    elif to_node.type == 'BSDF_PRINCIPLED':
                        if to_socket.name == 'Base Color':
                            kinds.add('Diffuse')
                        elif to_socket.name in {'Roughness', 'Metallic', 'Emission Strength'}:
                            kinds.add('ERM')
                    # Separate Color (обычно для ERM)
                    elif to_node.type == 'SEPARATE_COLOR':
                        # Проверяем что Separate Color подключен к Principled BSDF
                        for sep_output in to_node.outputs:
                            if sep_output.is_linked:
                                for sep_link in sep_output.links:
                                    if sep_link.to_node.type == 'BSDF_PRINCIPLED':
                                        if sep_link.to_socket.name in {'Roughness', 'Metallic', 'Emission Strength'}:
                                            kinds.add('ERM')
        
        # Приоритет: Normal > Diffuse > ERM
        if 'Normal' in kinds:
            return 'Normal'
        elif 'Diffuse' in kinds:
            return 'Diffuse'
        elif 'ERM' in kinds:
            return 'ERM'
        
        # Fallback: определяем по имени файла
        if node.image and node.image.filepath:
            filename = os.path.basename(node.image.filepath).lower()
            if 'normal' in filename:
                return 'Normal'
            elif 'diffuse' in filename or 'albedo' in filename or 'basecolor' in filename:
                return 'Diffuse'
            elif any(tag in filename for tag in ['erm', 'orm', 'roughness', 'metallic']):
                return 'ERM'
        
        return None

class BAKER_OT_rename_ucx(Operator):
    """Переименование выбранных объектов в UCX коллизии"""
    bl_idname = "baker.rename_ucx"
    bl_label = "Переименовать в UCX"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        # Получаем все выбранные MESH объекты
        selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if not selected_objects:
            self.report({'ERROR'}, "Выберите объекты для переименования в UCX")
            return {'CANCELLED'}
        
        # Проверяем наличие Address
        address = context.scene.agr_address
        if not address:
            self.report({'ERROR'}, "Введите Address в панели AGR_rename")
            return {'CANCELLED'}
        
        # Сохраняем количество выбранных объектов
        context.scene.agr_ucx_objects = str(len(selected_objects))
        
        # Вызываем диалог выбора типа
        bpy.ops.baker.ucx_select_type('INVOKE_DEFAULT')
        return {'FINISHED'}

class BAKER_OT_ucx_select_type(Operator):
    """Диалог выбора типа для UCX коллизий"""
    bl_idname = "baker.ucx_select_type"
    bl_label = "Выберите тип объекта для UCX"
    bl_options = {'REGISTER', 'UNDO'}
    
    object_type: EnumProperty(
        name="Тип объекта",
        items=[
            ('Main', "Main", "Основной объект с номером"),
            ('Ground', "Ground", "Земля (без номера)"),
        ],
        default='Main'
    )
    
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)
    
    def draw(self, context):
        layout = self.layout
        layout.prop(self, "object_type", text="Тип")
    
    def execute(self, context):
        # Сохраняем выбранный тип
        context.scene.agr_ucx_type = self.object_type
        
        # Если выбран Main, запрашиваем номер
        if self.object_type == 'Main':
            bpy.ops.baker.ucx_input_number('INVOKE_DEFAULT')
        else:
            # Для Ground сразу переименовываем без номера
            self.rename_ucx_objects(context, self.object_type, None)
        
        return {'FINISHED'}
    
    def rename_ucx_objects(self, context, obj_type, number):
        """Переименовывает UCX объекты"""
        address = context.scene.agr_address
        # Получаем все выбранные MESH объекты
        selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        # Формируем базовое имя
        if number and number > 0:
            base_name = f"UCX_SM_{address}_{number:03d}_{obj_type}"
        else:
            base_name = f"UCX_SM_{address}_{obj_type}"
        
        renamed_count = 0
        changed_count = 0

        # Определяем потенциальные названия для выбранных объектов
        potential_names = []
        for idx in range(1, len(selected_objects) + 1):
            potential_names.append(f"{base_name}_{idx:03d}")

        # Находим объекты в сцене, которые имеют такие же названия
        objects_to_change = []
        for obj in context.scene.objects:
            if obj.type == 'MESH' and obj not in selected_objects:
                if obj.name in potential_names:
                    objects_to_change.append(obj)

        # Переименовываем конфликтующие объекты с суффиксом _CHANGED и уникальным ID
        used_ids = set()
        for obj in objects_to_change:
            original_idx = potential_names.index(obj.name) + 1
            # Генерируем уникальный 8-значный ID
            while True:
                unique_id = random.randint(10000000, 99999999)
                if unique_id not in used_ids:
                    used_ids.add(unique_id)
                    break
            new_name = f"{base_name}_{original_idx:03d}_CHANGED_{unique_id}"
            obj.name = new_name
            changed_count += 1

        # Сначала добавляем рандомный суффикс ко всем выбранным объектам
        selected_suffixes = set()
        for obj in selected_objects:
            while True:
                suffix = random.randint(10000000, 99999999)
                if suffix not in selected_suffixes:
                    selected_suffixes.add(suffix)
                    break
            obj.name = f"{obj.name}_{suffix}"

        # Теперь переименовываем выбранные объекты в целевой формат
        for idx, obj in enumerate(selected_objects, 1):
            new_name = f"{base_name}_{idx:03d}"
            obj.name = new_name
            renamed_count += 1

        if changed_count > 0:
            self.report({'INFO'}, f"Переименовано {renamed_count} UCX объектов и {changed_count} конфликтующих")
        else:
            self.report({'INFO'}, f"Переименовано {renamed_count} UCX объектов")

class BAKER_OT_ucx_input_number(Operator):
    """Диалог ввода номера для UCX коллизий"""
    bl_idname = "baker.ucx_input_number"
    bl_label = "Введите номер объекта для UCX"
    bl_options = {'REGISTER', 'UNDO'}
    
    object_number: IntProperty(
        name="Номер объекта",
        description="Номер объекта от 0 до 999 (0 = без номера)",
        default=1,
        min=0,
        max=999
    )
    
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)
    
    def draw(self, context):
        layout = self.layout
        layout.prop(self, "object_number", text="Номер")
        layout.label(text="0 = без номера")
    
    def execute(self, context):
        obj_type = context.scene.agr_ucx_type
        
        # Переименовываем UCX объекты
        self.rename_ucx_objects(context, obj_type, self.object_number)
        
        return {'FINISHED'}
    
    def rename_ucx_objects(self, context, obj_type, number):
        """Переименовывает UCX объекты"""
        address = context.scene.agr_address
        # Получаем все выбранные MESH объекты
        selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        # Формируем базовое имя
        if number and number > 0:
            base_name = f"UCX_SM_{address}_{number:03d}_{obj_type}"
        else:
            base_name = f"UCX_SM_{address}_{obj_type}"
        
        renamed_count = 0
        changed_count = 0

        # Определяем потенциальные названия для выбранных объектов
        potential_names = []
        for idx in range(1, len(selected_objects) + 1):
            potential_names.append(f"{base_name}_{idx:03d}")

        # Находим объекты в сцене, которые имеют такие же названия
        objects_to_change = []
        for obj in context.scene.objects:
            if obj.type == 'MESH' and obj not in selected_objects:
                if obj.name in potential_names:
                    objects_to_change.append(obj)

        # Переименовываем конфликтующие объекты с суффиксом _CHANGED и уникальным ID
        used_ids = set()
        for obj in objects_to_change:
            original_idx = potential_names.index(obj.name) + 1
            # Генерируем уникальный 8-значный ID
            while True:
                unique_id = random.randint(10000000, 99999999)
                if unique_id not in used_ids:
                    used_ids.add(unique_id)
                    break
            new_name = f"{base_name}_{original_idx:03d}_CHANGED_{unique_id}"
            obj.name = new_name
            changed_count += 1

        # Сначала добавляем рандомный суффикс ко всем выбранным объектам
        selected_suffixes = set()
        for obj in selected_objects:
            while True:
                suffix = random.randint(10000000, 99999999)
                if suffix not in selected_suffixes:
                    selected_suffixes.add(suffix)
                    break
            obj.name = f"{obj.name}_{suffix}"

        # Теперь переименовываем выбранные объекты в целевой формат
        for idx, obj in enumerate(selected_objects, 1):
            new_name = f"{base_name}_{idx:03d}"
            obj.name = new_name
            renamed_count += 1

        if changed_count > 0:
            self.report({'INFO'}, f"Переименовано {renamed_count} UCX объектов и {changed_count} конфликтующих")
        else:
            self.report({'INFO'}, f"Переименовано {renamed_count} UCX объектов")

# ============= AGR Rename Operators =============

class BAKER_OT_agr_rename_main_object(Operator):
    """Переименование основного объекта с выбором типа"""
    bl_idname = "baker.agr_rename_main_object"
    bl_label = "Переименовать основной объект"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        # Проверяем наличие активного объекта
        active_obj = context.active_object
        if not active_obj or active_obj.type != 'MESH':
            self.report({'ERROR'}, "Выберите MESH объект")
            return {'CANCELLED'}
        
        # Проверяем наличие Address
        address = context.scene.agr_address
        if not address:
            self.report({'ERROR'}, "Введите Address в панели AGR_rename")
            return {'CANCELLED'}
        
        # Вызываем диалог выбора типа
        bpy.ops.baker.agr_select_type('INVOKE_DEFAULT')
        return {'FINISHED'}

class BAKER_OT_agr_select_type(Operator):
    """Диалог выбора типа объекта"""
    bl_idname = "baker.agr_select_type"
    bl_label = "Выберите тип объекта"
    bl_options = {'REGISTER', 'UNDO'}
    
    object_type: EnumProperty(
        name="Тип объекта",
        items=[
            ('Main', "Main", "Основной объект с номером"),
            ('MainGlass', "MainGlass", "Основной стеклянный объект"),
            ('Ground', "Ground", "Земля"),
            ('GroundGlass', "GroundGlass", "Земля стеклянная"),
            ('GroundEl', "GroundEl", "Земля EL"),
            ('GroundElGlass', "GroundElGlass", "Земля EL стеклянная"),
            ('Flora', "Flora", "Флора"),
        ],
        default='Main'
    )
    
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)
    
    def draw(self, context):
        layout = self.layout
        layout.prop(self, "object_type", text="Тип")
    
    def execute(self, context):
        # Сохраняем выбранный тип
        context.scene.agr_selected_type = self.object_type
        
        # Если выбран Main или MainGlass, запрашиваем номер
        if self.object_type in ['Main', 'MainGlass']:
            bpy.ops.baker.agr_input_number('INVOKE_DEFAULT')
        else:
            # Для остальных типов сразу переименовываем без номера
            self.rename_object(context, self.object_type, None)
        
        return {'FINISHED'}
    
    def rename_object(self, context, obj_type, number):
        """Переименовывает только объект (без материалов)"""
        active_obj = context.active_object
        address = context.scene.agr_address
        
        # Формируем имя объекта
        if number and number > 0:
            obj_name = f"SM_{address}_{number:03d}_{obj_type}"
        else:
            obj_name = f"SM_{address}_{obj_type}"
        
        # Переименовываем объект
        active_obj.name = obj_name
        
        self.report({'INFO'}, f"Объект переименован в {obj_name}. Используйте 'Переименовать материалы объекта' для переименования материалов")

class BAKER_OT_agr_input_number(Operator):
    """Диалог ввода номера объекта"""
    bl_idname = "baker.agr_input_number"
    bl_label = "Введите номер объекта"
    bl_options = {'REGISTER', 'UNDO'}
    
    object_number: IntProperty(
        name="Номер объекта",
        description="Номер объекта от 0 до 999 (0 = без номера)",
        default=1,
        min=0,
        max=999
    )
    
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)
    
    def draw(self, context):
        layout = self.layout
        layout.prop(self, "object_number", text="Номер")
        layout.label(text="0 = без номера")
    
    def execute(self, context):
        obj_type = context.scene.agr_selected_type
        
        # Переименовываем объект
        self.rename_object(context, obj_type, self.object_number)
        
        return {'FINISHED'}
    
    def rename_object(self, context, obj_type, number):
        """Переименовывает только объект (без материалов)"""
        active_obj = context.active_object
        address = context.scene.agr_address
        
        # Формируем имя объекта
        if number and number > 0:
            obj_name = f"SM_{address}_{number:03d}_{obj_type}"
        else:
            obj_name = f"SM_{address}_{obj_type}"
        
        # Переименовываем объект
        active_obj.name = obj_name
        
        self.report({'INFO'}, f"Объект переименован в {obj_name}. Используйте 'Переименовать материалы объекта' для переименования материалов")

class BAKER_OT_agr_rename_materials(Operator):
    """Переименование материалов объекта на основе его имени"""
    bl_idname = "baker.agr_rename_materials"
    bl_label = "Переименовать материалы объекта"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        """Проверка доступности оператора"""
        import re
        
        active_obj = context.active_object
        if not active_obj or active_obj.type != 'MESH':
            return False
        
        # Проверяем, соответствует ли имя объекта паттерну SM_
        obj_name = active_obj.name
        
        # Удаляем суффиксы .001, .002 и т.д. (от дублирования объектов)
        obj_name_clean = re.sub(r'\.\d{3}$', '', obj_name)
        
        # Паттерн: SM_Address_Number_Type или SM_Address_Type
        # Примеры: SM_PereulokTrekhprudny_D_9_001_Main, SM_PereulokTrekhprudny_D_9_Ground
        pattern = r'^SM_[A-Za-z0-9_]+_(Main|MainGlass|Ground|GroundGlass|GroundEl|GroundElGlass|Flora)$'
        pattern_with_number = r'^SM_[A-Za-z0-9_]+_\d{3}_(Main|MainGlass|Ground|GroundGlass|GroundEl|GroundElGlass|Flora)$'
        
        if re.match(pattern, obj_name_clean) or re.match(pattern_with_number, obj_name_clean):
            return True
        
        return False
    
    def execute(self, context):
        import re
        
        active_obj = context.active_object
        obj_name = active_obj.name
        
        # Удаляем суффиксы .001, .002 и т.д. (от дублирования объектов)
        obj_name_clean = re.sub(r'\.\d{3}$', '', obj_name)
        
        # Парсим имя объекта
        parts = obj_name_clean.split('_')
        
        # Находим тип объекта (последняя часть)
        valid_types = ['Main', 'MainGlass', 'Ground', 'GroundGlass', 'GroundEl', 'GroundElGlass', 'Flora']
        obj_type = None
        number = None
        
        # Проверяем последнюю часть
        if parts[-1] in valid_types:
            obj_type = parts[-1]
            
            # Проверяем, есть ли номер перед типом
            if len(parts) >= 3 and parts[-2].isdigit() and len(parts[-2]) == 3:
                number = int(parts[-2])
                # Address - это все части между SM_ и номером
                address = '_'.join(parts[1:-2])
            else:
                # Address - это все части между SM_ и типом
                address = '_'.join(parts[1:-1])
        else:
            self.report({'ERROR'}, "Не удалось распознать тип объекта")
            return {'CANCELLED'}
        
        # Сохраняем данные для диалогов
        context.scene.agr_glass_obj_type = obj_type
        context.scene.agr_glass_address = address
        context.scene.agr_glass_number = number if number else 0
        
        # Проверяем материалы
        if not active_obj.data.materials:
            self.report({'WARNING'}, "У объекта нет материалов")
            return {'CANCELLED'}
        
        # Для стеклянных типов вызываем диалоги
        if obj_type in ['MainGlass', 'GroundGlass']:
            # Спрашиваем HIGH или LOW
            bpy.ops.baker.agr_glass_select_quality('INVOKE_DEFAULT')
            return {'FINISHED'}
        elif obj_type == 'GroundElGlass':
            # Сразу спрашиваем номер для LOW стекла
            bpy.ops.baker.agr_glass_input_number('INVOKE_DEFAULT')
            return {'FINISHED'}
        
        # Для обычных типов переименовываем как обычно
        self.rename_materials_standard(context, active_obj, address, number, obj_type)
        return {'FINISHED'}
    
    def rename_materials_standard(self, context, active_obj, address, number, obj_type):
        """Стандартное переименование материалов"""
        material_count = len(active_obj.data.materials)
        if material_count > 9:
            self.report({'WARNING'}, f"У объекта {material_count} материалов, будут переименованы только первые 9")
            material_count = 9
        
        for idx, mat_slot in enumerate(active_obj.data.materials[:material_count], 1):
            if mat_slot:
                if number and number > 0:
                    mat_name = f"M_{address}_{number:03d}_{obj_type}_{idx}"
                else:
                    mat_name = f"M_{address}_{obj_type}_{idx}"
                mat_slot.name = mat_name
        
        self.report({'INFO'}, f"Материалы переименованы для объекта {active_obj.name}")

class BAKER_OT_agr_glass_select_quality(Operator):
    """Диалог выбора качества стекла HIGH или LOW"""
    bl_idname = "baker.agr_glass_select_quality"
    bl_label = "Выберите качество стекла"
    bl_options = {'REGISTER', 'UNDO'}
    
    glass_quality: EnumProperty(
        name="Качество",
        items=[
            ('HIGH', "HIGH", "Уникальное стекло с полным названием"),
            ('LOW', "LOW", "Простое стекло M_Glass_##"),
        ],
        default='HIGH'
    )
    
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)
    
    def draw(self, context):
        layout = self.layout
        layout.prop(self, "glass_quality", text="Качество")
    
    def execute(self, context):
        if self.glass_quality == 'HIGH':
            # Переименовываем как обычные материалы
            active_obj = context.active_object
            address = context.scene.agr_glass_address
            number = context.scene.agr_glass_number
            obj_type = context.scene.agr_glass_obj_type
            
            self.rename_materials_high(context, active_obj, address, number, obj_type)
        else:
            # LOW - спрашиваем номер стекла
            bpy.ops.baker.agr_glass_input_number('INVOKE_DEFAULT')
        
        return {'FINISHED'}
    
    def rename_materials_high(self, context, active_obj, address, number, obj_type):
        """Переименование материалов в HIGH качестве"""
        material_count = len(active_obj.data.materials)
        if material_count > 9:
            self.report({'WARNING'}, f"У объекта {material_count} материалов, будут переименованы только первые 9")
            material_count = 9
        
        for idx, mat_slot in enumerate(active_obj.data.materials[:material_count], 1):
            if mat_slot:
                if number and number > 0:
                    mat_name = f"M_{address}_{number:03d}_{obj_type}_{idx}"
                else:
                    mat_name = f"M_{address}_{obj_type}_{idx}"
                mat_slot.name = mat_name
        
        self.report({'INFO'}, f"Материалы переименованы в HIGH качестве")

class BAKER_OT_agr_glass_input_number(Operator):
    """Диалог ввода номера стекла LOW"""
    bl_idname = "baker.agr_glass_input_number"
    bl_label = "Введите номер стекла"
    bl_options = {'REGISTER', 'UNDO'}
    
    glass_number: IntProperty(
        name="Номер стекла",
        description="Номер стекла от 1 до 99",
        default=1,
        min=1,
        max=99
    )
    
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)
    
    def draw(self, context):
        layout = self.layout
        layout.prop(self, "glass_number", text="Номер")
        layout.label(text="Диапазон: 1-99")
    
    def execute(self, context):
        active_obj = context.active_object
        
        # Переименовываем в M_Glass_##
        material_count = len(active_obj.data.materials)
        if material_count > 9:
            self.report({'WARNING'}, f"У объекта {material_count} материалов, будут переименованы только первые 9")
            material_count = 9
        
        for idx, mat_slot in enumerate(active_obj.data.materials[:material_count], 1):
            if mat_slot:
                mat_name = f"M_Glass_{self.glass_number:02d}"
                mat_slot.name = mat_name
        
        self.report({'INFO'}, f"Материалы переименованы в M_Glass_{self.glass_number:02d}")
        return {'FINISHED'}

class BAKER_OT_agr_rename_geojson(Operator):
    """Переименование GEOJSON файла и адресов материалов внутри"""
    bl_idname = "baker.agr_rename_geojson"
    bl_label = "Переименовать GEOJSON"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        active_obj = context.active_object
        if not active_obj or active_obj.type != 'MESH':
            self.report({'ERROR'}, "Выберите MESH объект")
            return {'CANCELLED'}
        
        # Проверяем наличие Address
        new_address = context.scene.agr_address
        if not new_address:
            self.report({'ERROR'}, "Введите Address в панели AGR_rename")
            return {'CANCELLED'}
        
        # Парсим имя объекта для получения type и number (Main или Ground)
        obj_name = active_obj.name
        parsed = self.parse_object_name(obj_name)
        
        if not parsed:
            self.report({'ERROR'}, f"Объект должен быть типа Main или Ground")
            return {'CANCELLED'}
        
        current_address, number, obj_type = parsed
        
        if obj_type not in ['Main', 'Ground']:
            self.report({'ERROR'}, f"Переименование GEOJSON поддерживается только для типов Main и Ground")
            return {'CANCELLED'}
        
        # Получаем папку с текстурами из материалов
        texture_folder = self.get_texture_folder_from_material(active_obj)
        
        if not texture_folder:
            self.report({'ERROR'}, f"Не удалось найти папку с текстурами")
            return {'CANCELLED'}
        
        if not os.path.exists(texture_folder):
            self.report({'ERROR'}, f"Папка не найдена: {texture_folder}")
            return {'CANCELLED'}
        
        # Ищем GEOJSON файл в папке по маске (адрес может быть любой)
        geojson_file = None
        old_address_in_file = None
        
        if obj_type == 'Main' and number:
            # Ищем файл по маске: SM_*_001.geojson или SM_*.geojson
            import re
            # Паттерн с номером: SM_Address_001.geojson
            pattern_with_num = r'^SM_(.+?)_' + re.escape(number) + r'\.geojson$'
            # Паттерн без номера: SM_Address.geojson
            pattern_without_num = r'^SM_(.+?)\.geojson$'
            
            for filename in os.listdir(texture_folder):
                match = re.match(pattern_with_num, filename)
                if match:
                    geojson_file = filename
                    old_address_in_file = match.group(1)
                    break
                match = re.match(pattern_without_num, filename)
                if match:
                    geojson_file = filename
                    old_address_in_file = match.group(1)
                    # Продолжаем искать, приоритет у файла с номером
                    
        elif obj_type == 'Ground':
            # Ищем файл по маске: SM_*_Ground.geojson
            import re
            pattern = r'^SM_(.+?)_Ground\.geojson$'
            for filename in os.listdir(texture_folder):
                match = re.match(pattern, filename)
                if match:
                    geojson_file = filename
                    old_address_in_file = match.group(1)
                    break
        
        if not geojson_file:
            self.report({'ERROR'}, f"GEOJSON файл не найден в папке {texture_folder}")
            return {'CANCELLED'}
        
        old_geojson_path = os.path.join(texture_folder, geojson_file)
        
        # Читаем и обновляем содержимое GEOJSON
        try:
            with open(old_geojson_path, 'r', encoding='utf-8') as f:
                geojson_data = json.load(f)
            
            # Обновляем адреса в материалах стекла (если они есть)
            # Используем old_address_in_file, найденный по маске
            updated_count = self.update_glass_materials_in_geojson(geojson_data, old_address_in_file, new_address)
            
            # Сохраняем обновленный файл с новым именем
            if obj_type == 'Main' and number:
                new_geojson_name = f"SM_{new_address}_{number}.geojson"
            elif obj_type == 'Ground':
                new_geojson_name = f"SM_{new_address}_Ground.geojson"
            
            new_geojson_path = os.path.join(texture_folder, new_geojson_name)
            
            with open(new_geojson_path, 'w', encoding='utf-8') as f:
                json.dump(geojson_data, f, ensure_ascii=False, indent=2)
            
            # Удаляем старый файл если имя изменилось
            if old_geojson_path != new_geojson_path and os.path.exists(old_geojson_path):
                os.remove(old_geojson_path)
            
            # Переименовываем FBX файлы
            fbx_renamed_count = self.rename_fbx_files(texture_folder, new_address, number, obj_type)
            
            if updated_count > 0:
                self.report({'INFO'}, f"GEOJSON переименован: {new_geojson_name}, обновлено материалов стекла: {updated_count}, FBX файлов: {fbx_renamed_count}")
            else:
                self.report({'INFO'}, f"GEOJSON переименован: {new_geojson_name}, FBX файлов: {fbx_renamed_count}")
            
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка обработки GEOJSON: {str(e)}")
            return {'CANCELLED'}
        
        return {'FINISHED'}
    
    def parse_object_name(self, obj_name):
        """Парсит имя объекта и возвращает (address, number, type)"""
        import re
        
        # Паттерн для Main: SM_Address_001_Main
        pattern_main = r'^SM_(.+?)_(\d{3})_(Main|MainGlass)$'
        match = re.match(pattern_main, obj_name)
        if match:
            return match.group(1), match.group(2), 'Main'
        
        # Паттерн для Ground: SM_Address_Ground
        pattern_ground = r'^SM_(.+?)_(Ground|GroundGlass|GroundEl|GroundElGlass)$'
        match = re.match(pattern_ground, obj_name)
        if match:
            return match.group(1), None, 'Ground'
        
        return None
    
    def get_texture_folder_from_material(self, obj):
        """Получает путь к папке с текстурами из ноды материала"""
        for mat_slot in obj.data.materials:
            if mat_slot and mat_slot.use_nodes:
                for node in mat_slot.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image:
                        if node.image.source == 'TILED':
                            if node.image.filepath:
                                abs_path = bpy.path.abspath(node.image.filepath)
                                folder_path = os.path.dirname(abs_path)
                                return folder_path
        return None
    
    def update_glass_materials_in_geojson(self, geojson_data, old_address, new_address):
        """Обновляет адреса в названиях материалов стекла внутри GEOJSON"""
        updated_count = 0
        
        try:
            # Проходим по всем features
            if 'features' in geojson_data:
                for feature in geojson_data['features']:
                    # Ищем Glasses
                    if 'Glasses' in feature:
                        for glass_list in feature['Glasses']:
                            # Это может быть список или словарь
                            if isinstance(glass_list, dict):
                                # Создаем новый словарь с обновленными ключами
                                new_glass_dict = {}
                                for old_mat_name, mat_data in glass_list.items():
                                    # Заменяем адрес в имени материала
                                    # M_OldAddress_001_MainGlass_1 -> M_NewAddress_001_MainGlass_1
                                    new_mat_name = old_mat_name.replace(f"M_{old_address}_", f"M_{new_address}_")
                                    new_glass_dict[new_mat_name] = mat_data
                                    
                                    if old_mat_name != new_mat_name:
                                        updated_count += 1
                                        print(f"  {old_mat_name} -> {new_mat_name}")
                                
                                # Заменяем весь словарь
                                glass_list.clear()
                                glass_list.update(new_glass_dict)
        except Exception as e:
            print(f"Ошибка обновления материалов в GEOJSON: {e}")
        
        return updated_count
    
    def rename_fbx_files(self, folder_path, new_address, number, obj_type):
        """Переименовывает FBX файлы в папке по маске"""
        import re
        renamed_count = 0
        
        try:
            files_in_folder = os.listdir(folder_path)
            
            if obj_type == 'Main':
                # Ищем FBX по маске для Main
                if number:
                    # С номером: SM_*_001.fbx и SM_*_001_Light.fbx
                    pattern_main = r'^SM_(.+?)_' + re.escape(number) + r'\.fbx$'
                    pattern_light = r'^SM_(.+?)_' + re.escape(number) + r'_Light\.fbx$'
                    
                    new_main_name = f"SM_{new_address}_{number}.fbx"
                    new_light_name = f"SM_{new_address}_{number}_Light.fbx"
                else:
                    # Без номера: SM_*.fbx и SM_*_Light.fbx (но не SM_*_001.fbx)
                    new_main_name = f"SM_{new_address}.fbx"
                    new_light_name = f"SM_{new_address}_Light.fbx"
                
                for filename in files_in_folder:
                    if number:
                        # С номером
                        match_main = re.match(pattern_main, filename)
                        match_light = re.match(pattern_light, filename)
                        
                        if match_main:
                            old_path = os.path.join(folder_path, filename)
                            new_path = os.path.join(folder_path, new_main_name)
                            if old_path != new_path:
                                os.rename(old_path, new_path)
                                renamed_count += 1
                                print(f"  FBX: {filename} -> {new_main_name}")
                        
                        if match_light:
                            old_path = os.path.join(folder_path, filename)
                            new_path = os.path.join(folder_path, new_light_name)
                            if old_path != new_path:
                                os.rename(old_path, new_path)
                                renamed_count += 1
                                print(f"  FBX Light: {filename} -> {new_light_name}")
                    else:
                        # Без номера - проверяем что файл НЕ содержит _\d{3} перед .fbx или _Light.fbx
                        # SM_Address.fbx - подходит
                        # SM_Address_001.fbx - не подходит
                        # SM_Address_Light.fbx - подходит
                        # SM_Address_001_Light.fbx - не подходит
                        if filename.endswith('.fbx'):
                            if filename.endswith('_Light.fbx'):
                                # Проверяем Light
                                base = filename[:-10]  # убираем '_Light.fbx'
                                if base.startswith('SM_') and not re.search(r'_\d{3}$', base):
                                    old_path = os.path.join(folder_path, filename)
                                    new_path = os.path.join(folder_path, new_light_name)
                                    if old_path != new_path:
                                        os.rename(old_path, new_path)
                                        renamed_count += 1
                                        print(f"  FBX Light: {filename} -> {new_light_name}")
                            else:
                                # Проверяем обычный FBX
                                base = filename[:-4]  # убираем '.fbx'
                                if base.startswith('SM_') and not re.search(r'_\d{3}$', base) and '_Light' not in base:
                                    old_path = os.path.join(folder_path, filename)
                                    new_path = os.path.join(folder_path, new_main_name)
                                    if old_path != new_path:
                                        os.rename(old_path, new_path)
                                        renamed_count += 1
                                        print(f"  FBX: {filename} -> {new_main_name}")
            
            elif obj_type == 'Ground':
                # Ищем FBX по маске для Ground
                pattern_main = r'^SM_(.+?)_Ground\.fbx$'
                pattern_light = r'^SM_(.+?)_Ground_Light\.fbx$'
                
                new_main_name = f"SM_{new_address}_Ground.fbx"
                new_light_name = f"SM_{new_address}_Ground_Light.fbx"
                
                for filename in files_in_folder:
                    # Проверяем основной FBX
                    match = re.match(pattern_main, filename)
                    if match:
                        old_path = os.path.join(folder_path, filename)
                        new_path = os.path.join(folder_path, new_main_name)
                        if old_path != new_path:
                            os.rename(old_path, new_path)
                            renamed_count += 1
                            print(f"  FBX: {filename} -> {new_main_name}")
                    
                    # Проверяем Light FBX
                    match = re.match(pattern_light, filename)
                    if match:
                        old_path = os.path.join(folder_path, filename)
                        new_path = os.path.join(folder_path, new_light_name)
                        if old_path != new_path:
                            os.rename(old_path, new_path)
                            renamed_count += 1
                            print(f"  FBX Light: {filename} -> {new_light_name}")
        
        except Exception as e:
            print(f"Ошибка переименования FBX: {e}")
        
        return renamed_count

class BAKER_OT_agr_rename_lights(Operator):
    """Переименование Empty объекта и привязанных источников света"""
    bl_idname = "baker.agr_rename_lights"
    bl_label = "Переименовать свет"
    bl_options = {'REGISTER', 'UNDO'}
    
    light_type: EnumProperty(
        name="Тип объекта",
        items=[
            ('Main', "Main", "Основной объект с номером"),
            ('Ground', "Ground", "Земля без номера"),
        ],
        default='Main'
    )
    
    def invoke(self, context, event):
        active_obj = context.active_object
        if not active_obj or active_obj.type != 'EMPTY':
            self.report({'ERROR'}, "Выберите EMPTY объект")
            return {'CANCELLED'}
        
        # Проверяем наличие Address
        new_address = context.scene.agr_address
        if not new_address:
            self.report({'ERROR'}, "Введите Address в панели AGR_rename")
            return {'CANCELLED'}
        
        return context.window_manager.invoke_props_dialog(self)
    
    def draw(self, context):
        layout = self.layout
        layout.prop(self, "light_type", text="Тип")
    
    def execute(self, context):
        # Если выбран Main, запрашиваем номер
        if self.light_type == 'Main':
            context.scene.agr_lights_type = 'Main'
            bpy.ops.baker.agr_lights_input_number('INVOKE_DEFAULT')
        else:
            # Для Ground сразу переименовываем
            self.rename_lights(context, 'Ground', None)
        
        return {'FINISHED'}
    
    def rename_lights(self, context, obj_type, number):
        """Переименовывает Empty и привязанные источники света"""
        active_obj = context.active_object
        new_address = context.scene.agr_address
        
        # Находим все дочерние объекты типа LIGHT
        light_objects = []
        for obj in context.scene.objects:
            if obj.type == 'LIGHT' and obj.parent == active_obj:
                light_objects.append(obj)
        
        if not light_objects:
            self.report({'WARNING'}, "К Empty не привязаны источники света")
            return {'CANCELLED'}
        
        # Переименовываем Empty
        if obj_type == 'Ground':
            root_name = f"{new_address}_Ground_Root"
        elif obj_type == 'Main' and number:
            root_name = f"{new_address}_{number}_Root"
        else:  # Main без номера
            root_name = f"{new_address}_Root"
        
        active_obj.name = root_name
        
        # Группируем источники по типам и переименовываем
        spot_counter = 1
        point_counter = 1
        
        # Сначала сортируем по имени для предсказуемого порядка
        light_objects.sort(key=lambda x: x.name)
        
        for light_obj in light_objects:
            light_type = light_obj.data.type
            
            if light_type == 'SPOT':
                lighttype_name = 'Spot'
                counter = spot_counter
                spot_counter += 1
            elif light_type == 'POINT':
                lighttype_name = 'Point'
                counter = point_counter
                point_counter += 1
            else:
                continue  # Пропускаем другие типы
            
            # Формируем имя источника
            if obj_type == 'Ground':
                new_light_name = f"{new_address}_Ground_{lighttype_name}_{counter:03d}"
            elif obj_type == 'Main' and number:
                new_light_name = f"{new_address}_{number}_{lighttype_name}_{counter:03d}"
            else:  # Main без номера
                new_light_name = f"{new_address}_{lighttype_name}_{counter:03d}"
            
            light_obj.name = new_light_name
        
        total_lights = spot_counter - 1 + point_counter - 1
        self.report({'INFO'}, f"Empty переименован в {root_name}, переименовано {total_lights} источников света")

class BAKER_OT_agr_lights_input_number(Operator):
    """Ввод номера для Main типа"""
    bl_idname = "baker.agr_lights_input_number"
    bl_label = "Введите номер (0-999)"
    bl_options = {'REGISTER', 'UNDO'}
    
    number: IntProperty(
        name="Номер",
        description="Номер объекта (0-999, 0 = без номера)",
        default=1,
        min=0,
        max=999
    )
    
    def execute(self, context):
        if self.number == 0:
            number_str = None
        else:
            number_str = f"{self.number:03d}"
        
        # Выполняем переименование
        self.rename_lights(context, 'Main', number_str)
        return {'FINISHED'}
    
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)
    
    def draw(self, context):
        layout = self.layout
        layout.prop(self, "number")
    
    def rename_lights(self, context, obj_type, number):
        """Переименовывает Empty и привязанные источники света"""
        active_obj = context.active_object
        new_address = context.scene.agr_address
        
        # Находим все дочерние объекты типа LIGHT
        light_objects = []
        for obj in context.scene.objects:
            if obj.type == 'LIGHT' and obj.parent == active_obj:
                light_objects.append(obj)
        
        if not light_objects:
            self.report({'WARNING'}, "К Empty не привязаны источники света")
            return {'CANCELLED'}
        
        # Переименовываем Empty
        if obj_type == 'Ground':
            root_name = f"{new_address}_Ground_Root"
        elif obj_type == 'Main' and number:
            root_name = f"{new_address}_{number}_Root"
        else:  # Main без номера
            root_name = f"{new_address}_Root"
        
        active_obj.name = root_name
        
        # Группируем источники по типам и переименовываем
        spot_counter = 1
        point_counter = 1
        
        # Сначала сортируем по имени для предсказуемого порядка
        light_objects.sort(key=lambda x: x.name)
        
        for light_obj in light_objects:
            light_type = light_obj.data.type
            
            if light_type == 'SPOT':
                lighttype_name = 'Spot'
                counter = spot_counter
                spot_counter += 1
            elif light_type == 'POINT':
                lighttype_name = 'Point'
                counter = point_counter
                point_counter += 1
            else:
                continue  # Пропускаем другие типы
            
            # Формируем имя источника
            if obj_type == 'Ground':
                new_light_name = f"{new_address}_Ground_{lighttype_name}_{counter:03d}"
            elif obj_type == 'Main' and number:
                new_light_name = f"{new_address}_{number}_{lighttype_name}_{counter:03d}"
            else:  # Main без номера
                new_light_name = f"{new_address}_{lighttype_name}_{counter:03d}"
            
            light_obj.name = new_light_name
        
        total_lights = spot_counter - 1 + point_counter - 1
        self.report({'INFO'}, f"Empty переименован в {root_name}, переименовано {total_lights} источников света")

class BAKER_OT_agr_distribute_collections(Operator):
    """Распределение объектов с заданным Address по коллекциям"""
    bl_idname = "baker.agr_distribute_collections"
    bl_label = "Распределить по коллекциям"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        import re
        
        address = context.scene.agr_address
        if not address:
            self.report({'ERROR'}, "Введите Address в панели AGR_rename")
            return {'CANCELLED'}
        
        # Проверяем, есть ли уже сохраненный lowpoly номер
        lowpoly_number = getattr(context.scene, 'agr_lowpoly_number', "")
        
        if lowpoly_number:
            # Номер уже введен, выполняем сначала highpoly, потом lowpoly
            print(f"\n=== ЭТАП 1: Распределение highpoly объектов ===")
            result_high = self.execute_highpoly(context)
            
            print(f"\n=== ЭТАП 2: Распределение lowpoly объектов ===")
            result_low = self.execute_lowpoly(context, lowpoly_number)
            
            # Возвращаем результат lowpoly (последний этап)
            return result_low
        
        # Автоматически определяем наличие lowpoly объектов
        has_lowpoly = self.detect_lowpoly_objects(context, address)
        
        if has_lowpoly:
            # Если есть lowpoly, спрашиваем 4-значный код
            bpy.ops.baker.agr_distribute_input_lowpoly_number('INVOKE_DEFAULT')
            return {'FINISHED'}
        else:
            # Иначе запускаем только highpoly логику
            return self.execute_highpoly(context)
    
    def detect_lowpoly_objects(self, context, address):
        """Определяет наличие lowpoly объектов в сцене"""
        import re
        
        for obj in context.scene.objects:
            if obj.type != 'MESH':
                continue
            
            obj_name = obj.name
            obj_name_clean = re.sub(r'\.\d{3}$', '', obj_name)
            has_suffix = obj_name != obj_name_clean
            
            # Main/MainGlass/Ground/GroundGlass с суффиксом - это lowpoly
            if has_suffix:
                match = re.match(r'^SM_' + re.escape(address) + r'_(\d{3})_(Main|MainGlass)', obj_name)
                if match:
                    return True
                
                match = re.match(r'^SM_' + re.escape(address) + r'_(Ground|GroundGlass)', obj_name)
                if match:
                    return True
            
            # GroundEl и Flora - всегда lowpoly
            match = re.match(r'^SM_' + re.escape(address) + r'_(GroundEl|GroundElGlass|Flora)$', obj_name_clean)
            if match:
                return True
        
        return False
    
    def execute_highpoly(self, context):
        """Распределение highpoly объектов (старая логика)"""
        import re
        
        address = context.scene.agr_address
        
        # Словарь для хранения объектов по категориям
        collections_data = {}
        
        # Проходим по всем объектам в сцене
        for obj in context.scene.objects:
            obj_name = obj.name
            
            # 1. Main объекты с номером: SM_Address_001_Main, SM_Address_001_MainGlass
            match = re.match(r'^SM_' + re.escape(address) + r'_(\d{3})_(Main|MainGlass)$', obj_name)
            if match:
                number = match.group(1)
                coll_name = f"SM_{address}_{number}.fbx"
                if coll_name not in collections_data:
                    collections_data[coll_name] = []
                collections_data[coll_name].append(obj)
                continue
            
            # 2. Main UCX с номером: UCX_SM_Address_001_Main_###
            match = re.match(r'^UCX_SM_' + re.escape(address) + r'_(\d{3})_Main_\d+$', obj_name)
            if match:
                number = match.group(1)
                coll_name = f"SM_{address}_{number}.fbx"
                if coll_name not in collections_data:
                    collections_data[coll_name] = []
                collections_data[coll_name].append(obj)
                continue
            
            # 3. Main Root с номером: Address_001_Root
            match = re.match(r'^' + re.escape(address) + r'_(\d{3})_Root$', obj_name)
            if match:
                number = match.group(1)
                coll_name = f"SM_{address}_{number}_Light.fbx"
                if coll_name not in collections_data:
                    collections_data[coll_name] = []
                collections_data[coll_name].append(obj)
                # Добавляем дочерние источники света
                for child in obj.children:
                    if child.type == 'LIGHT':
                        collections_data[coll_name].append(child)
                continue
            
            # 4. Main источники света с номером: Address_001_Spot_001, Address_001_Point_001
            match = re.match(r'^' + re.escape(address) + r'_(\d{3})_(Spot|Point)_\d+$', obj_name)
            if match:
                number = match.group(1)
                coll_name = f"SM_{address}_{number}_Light.fbx"
                # Объект уже добавлен как дочерний к Root, пропускаем
                continue
            
            # 5. Main объекты БЕЗ номера: SM_Address_Main, SM_Address_MainGlass
            match = re.match(r'^SM_' + re.escape(address) + r'_(Main|MainGlass)$', obj_name)
            if match:
                coll_name = f"SM_{address}.fbx"
                if coll_name not in collections_data:
                    collections_data[coll_name] = []
                collections_data[coll_name].append(obj)
                continue
            
            # 6. Main UCX БЕЗ номера: UCX_SM_Address_Main_###
            match = re.match(r'^UCX_SM_' + re.escape(address) + r'_Main_\d+$', obj_name)
            if match:
                coll_name = f"SM_{address}.fbx"
                if coll_name not in collections_data:
                    collections_data[coll_name] = []
                collections_data[coll_name].append(obj)
                continue
            
            # 7. Main Root БЕЗ номера: Address_Root
            match = re.match(r'^' + re.escape(address) + r'_Root$', obj_name)
            if match:
                coll_name = f"SM_{address}_Light.fbx"
                if coll_name not in collections_data:
                    collections_data[coll_name] = []
                collections_data[coll_name].append(obj)
                # Добавляем дочерние источники света
                for child in obj.children:
                    if child.type == 'LIGHT':
                        collections_data[coll_name].append(child)
                continue
            
            # 8. Main источники света БЕЗ номера: Address_Spot_001, Address_Point_001
            match = re.match(r'^' + re.escape(address) + r'_(Spot|Point)_\d+$', obj_name)
            if match:
                coll_name = f"SM_{address}_Light.fbx"
                # Объект уже добавлен как дочерний к Root, пропускаем
                continue
            
            # 9. Ground объекты highpoly: SM_Address_Ground, SM_Address_GroundGlass (БЕЗ GroundEl - это lowpoly!)
            match = re.match(r'^SM_' + re.escape(address) + r'_(Ground|GroundGlass)$', obj_name)
            if match:
                coll_name = f"SM_{address}_Ground.fbx"
                if coll_name not in collections_data:
                    collections_data[coll_name] = []
                collections_data[coll_name].append(obj)
                continue
            
            # 10. Ground UCX: UCX_SM_Address_Ground_###
            match = re.match(r'^UCX_SM_' + re.escape(address) + r'_Ground_\d+$', obj_name)
            if match:
                coll_name = f"SM_{address}_Ground.fbx"
                if coll_name not in collections_data:
                    collections_data[coll_name] = []
                collections_data[coll_name].append(obj)
                continue
            
            # 11. Ground Root: Address_Ground_Root
            match = re.match(r'^' + re.escape(address) + r'_Ground_Root$', obj_name)
            if match:
                coll_name = f"SM_{address}_Ground_Light.fbx"
                if coll_name not in collections_data:
                    collections_data[coll_name] = []
                collections_data[coll_name].append(obj)
                # Добавляем дочерние источники света
                for child in obj.children:
                    if child.type == 'LIGHT':
                        collections_data[coll_name].append(child)
                continue
            
            # 12. Ground источники света: Address_Ground_Spot_001, Address_Ground_Point_001
            match = re.match(r'^' + re.escape(address) + r'_Ground_(Spot|Point)_\d+$', obj_name)
            if match:
                coll_name = f"SM_{address}_Ground_Light.fbx"
                # Объект уже добавлен как дочерний к Root, пропускаем
                continue
        
        # Создаем коллекции и перемещаем объекты
        total_objects = 0
        collections_created = 0
        
        for coll_name, objects in collections_data.items():
            if not objects:
                continue
            
            # Создаем коллекцию если её нет
            if coll_name not in bpy.data.collections:
                new_coll = bpy.data.collections.new(coll_name)
                context.scene.collection.children.link(new_coll)
                collections_created += 1
            else:
                new_coll = bpy.data.collections[coll_name]
            
            # Перемещаем объекты в коллекцию
            for obj in objects:
                # Удаляем из всех текущих коллекций
                for old_coll in obj.users_collection:
                    old_coll.objects.unlink(obj)
                
                # Добавляем в новую коллекцию
                if obj.name not in new_coll.objects:
                    new_coll.objects.link(obj)
                    total_objects += 1
        
        # Очищаем пустые коллекции
        collections_removed = self.remove_empty_collections(context)
        
        if total_objects > 0:
            msg = f"Распределено {total_objects} highpoly объектов в {len(collections_data)} коллекций (создано новых: {collections_created})"
            if collections_removed > 0:
                msg += f", удалено пустых коллекций: {collections_removed}"
            self.report({'INFO'}, msg)
        else:
            self.report({'WARNING'}, f"Не найдено highpoly объектов с адресом {address}")
        
        return {'FINISHED'}
    
    def execute_lowpoly(self, context, lowpoly_number):
        """Распределение lowpoly объектов"""
        import re
        
        address = context.scene.agr_address
        
        print(f"\n=== Распределение lowpoly объектов ===")
        print(f"Address: {address}")
        print(f"Lowpoly номер: {lowpoly_number}")
        
        # Словари для хранения объектов
        # Группа = ADDRESS + номер (все Main и MainGlass с одним address_number)
        main_groups = {}  # {address_number: [objects]}
        ground_objects = []  # Все Ground, GroundEl, Flora
        
        # Проходим по всем объектам в сцене
        for obj in context.scene.objects:
            if obj.type != 'MESH':
                continue
            
            obj_name = obj.name
            obj_name_clean = re.sub(r'\.\d{3}$', '', obj_name)  # Удаляем .001, .002
            
            is_lowpoly = obj_name != obj_name_clean  # Есть суффикс дублирования
            
            # Определяем тип
            # Main/MainGlass с номером lowpoly
            match = re.match(r'^SM_' + re.escape(address) + r'_(\d{3})_(Main|MainGlass)', obj_name)
            if match and is_lowpoly:
                number = match.group(1)
                obj_type = match.group(2)
                group_key = f"{address}_{number}"  # Группа: ADDRESS_номер
                
                if group_key not in main_groups:
                    main_groups[group_key] = []
                
                main_groups[group_key].append(obj)
                print(f"  Lowpoly {obj_type}: {obj_name} → группа {group_key}")
                continue
            
            # Ground/GroundGlass lowpoly (определяем по .001 .002)
            match = re.match(r'^SM_' + re.escape(address) + r'_(Ground|GroundGlass)', obj_name)
            if match and is_lowpoly:
                ground_objects.append(obj)
                print(f"  Lowpoly Ground/GroundGlass: {obj_name}")
                continue
            
            # GroundEl (всегда lowpoly)
            match = re.match(r'^SM_' + re.escape(address) + r'_(GroundEl|GroundElGlass)$', obj_name_clean)
            if match:
                ground_objects.append(obj)
                print(f"  Lowpoly GroundEl: {obj_name}")
                continue
            
            # Flora (всегда lowpoly)
            match = re.match(r'^SM_' + re.escape(address) + r'_Flora$', obj_name_clean)
            if match:
                ground_objects.append(obj)
                print(f"  Lowpoly Flora: {obj_name}")
                continue
        
        # Распределяем Main/MainGlass по полигонам (до 150k треугольников)
        # Важно: группа (ADDRESS_номер) включает ВСЕ Main и MainGlass с этим номером
        # Группа НЕ разделяется между коллекциями!
        collections_data = {}
        
        # Собираем все группы (address_number) с их объектами и треугольниками
        groups_to_pack = []  # [(group_key, objects, total_tris)]
        
        for group_key, objects in main_groups.items():
            # Считаем общее количество треугольников в группе
            total_tris = sum(len(obj.data.polygons) for obj in objects)
            groups_to_pack.append((group_key, objects, total_tris))
            
            # Показываем состав группы
            main_count = sum(1 for obj in objects if 'Main' in obj.name and 'MainGlass' not in obj.name)
            mainglass_count = sum(1 for obj in objects if 'MainGlass' in obj.name)
            print(f"  Группа {group_key}: {len(objects)} объектов (Main: {main_count}, MainGlass: {mainglass_count}), {total_tris} треугольников")
        
        # Упаковываем группы в коллекции до 150k треугольников
        max_tris = 150000
        current_batch = []
        current_tris = 0
        batch_index = 1
        
        for group_key, group_objects, group_tris in groups_to_pack:
            # Если добавление этой группы превысит лимит И уже есть объекты в батче
            if current_tris + group_tris > max_tris and current_batch:
                # Сохраняем текущий батч
                coll_name = f"{lowpoly_number}_{address}_{batch_index:02d}.fbx"
                collections_data[coll_name] = current_batch
                print(f"  → Коллекция {coll_name}: {len(current_batch)} объектов, {current_tris} треугольников")
                
                # Начинаем новый батч
                current_batch = group_objects.copy()
                current_tris = group_tris
                batch_index += 1
            else:
                # Добавляем группу в текущий батч (не разделяя её)
                current_batch.extend(group_objects)
                current_tris += group_tris
        
        # Сохраняем последний батч
        if current_batch:
            coll_name = f"{lowpoly_number}_{address}_{batch_index:02d}.fbx"
            collections_data[coll_name] = current_batch
            print(f"  → Коллекция {coll_name}: {len(current_batch)} объектов, {current_tris} треугольников")
        
        # Ground объекты в одну коллекцию
        if ground_objects:
            coll_name = f"{lowpoly_number}_{address}_Ground.fbx"
            collections_data[coll_name] = ground_objects
            print(f"  Коллекция {coll_name}: {len(ground_objects)} объектов")
        
        # Создаем коллекции и перемещаем объекты
        total_objects = 0
        collections_created = 0
        
        for coll_name, objects in collections_data.items():
            if not objects:
                continue
            
            # Создаем коллекцию если её нет
            if coll_name not in bpy.data.collections:
                new_coll = bpy.data.collections.new(coll_name)
                context.scene.collection.children.link(new_coll)
                collections_created += 1
            else:
                new_coll = bpy.data.collections[coll_name]
            
            # Перемещаем объекты в коллекцию
            for obj in objects:
                # Удаляем из всех текущих коллекций
                for old_coll in obj.users_collection:
                    old_coll.objects.unlink(obj)
                
                # Добавляем в новую коллекцию
                if obj.name not in new_coll.objects:
                    new_coll.objects.link(obj)
                    total_objects += 1
        
        # Очищаем пустые коллекции
        collections_removed = self.remove_empty_collections(context)
        
        # Переименовываем папку и FBX файлы для lowpoly
        self.rename_lowpoly_folder_and_fbx(context, lowpoly_number, address)
        
        if total_objects > 0:
            msg = f"Распределено {total_objects} lowpoly объектов в {len(collections_data)} коллекций (создано новых: {collections_created})"
            if collections_removed > 0:
                msg += f", удалено пустых коллекций: {collections_removed}"
            self.report({'INFO'}, msg)
        else:
            self.report({'WARNING'}, f"Не найдено lowpoly объектов с адресом {address}")
        
        return {'FINISHED'}
    
    def remove_empty_collections(self, context):
        """Удаляет пустые коллекции из сцены"""
        removed_count = 0
        
        def remove_empty_recursive(collection):
            """Рекурсивно удаляет пустые дочерние коллекции"""
            nonlocal removed_count
            
            # Сначала обрабатываем дочерние коллекции
            for child in list(collection.children):
                remove_empty_recursive(child)
            
            # Проверяем, пуста ли коллекция (нет объектов и нет дочерних коллекций)
            if len(collection.objects) == 0 and len(collection.children) == 0:
                # Не удаляем главную коллекцию сцены
                if collection != context.scene.collection:
                    # Удаляем коллекцию из всех родительских коллекций
                    for parent in bpy.data.collections:
                        if collection.name in parent.children:
                            parent.children.unlink(collection)
                    
                    # Удаляем из сцены если она там есть
                    if collection.name in context.scene.collection.children:
                        context.scene.collection.children.unlink(collection)
                    
                    # Удаляем из данных
                    bpy.data.collections.remove(collection)
                    removed_count += 1
        
        # Начинаем с главной коллекции сцены
        remove_empty_recursive(context.scene.collection)
        
        return removed_count
    
    def rename_lowpoly_folder_and_fbx(self, context, lowpoly_number, new_address):
        """Переименовывает папку lowpoly и FBX файлы внутри неё"""
        import os
        import re
        
        # Получаем путь к .blend файлу
        blend_path = bpy.data.filepath
        if not blend_path:
            print("  .blend файл не сохранен, пропускаем переименование папки/FBX")
            return
        
        blend_dir = os.path.dirname(blend_path)
        print(f"\n=== Переименование lowpoly папки и FBX файлов ===")
        print(f"Директория .blend: {blend_dir}")
        print(f"Lowpoly номер: {lowpoly_number}")
        print(f"Новый адрес: {new_address}")
        
        # Ищем папку с паттерном ####_* (любое 4-значное число + любой текст)
        old_folder = None
        old_folder_name = None
        
        for item in os.listdir(blend_dir):
            item_path = os.path.join(blend_dir, item)
            if os.path.isdir(item_path):
                # Проверяем паттерн: 4 цифры + underscore + что-то еще
                match = re.match(r'^(\d{4})_(.+)$', item)
                if match:
                    old_folder = item_path
                    old_folder_name = item
                    old_number = match.group(1)
                    old_address = match.group(2)
                    print(f"  Найдена папка: {old_folder_name}")
                    print(f"    Старый номер: {old_number}")
                    print(f"    Старый адрес: {old_address}")
                    break
        
        if not old_folder:
            print("  Папка lowpoly не найдена, создаем новую")
            new_folder_name = f"{lowpoly_number}_{new_address}"
            new_folder_path = os.path.join(blend_dir, new_folder_name)
            os.makedirs(new_folder_path, exist_ok=True)
            print(f"  Создана папка: {new_folder_name}")
            return
        
        # Переименовываем FBX файлы внутри старой папки
        fbx_renamed = 0
        for filename in os.listdir(old_folder):
            if filename.endswith('.fbx'):
                old_fbx_path = os.path.join(old_folder, filename)
                
                # Паттерн FBX: ####_Address_##.fbx или ####_Address_Ground.fbx
                # Заменяем старый адрес на новый, сохраняя номер lowpoly и суффикс
                match = re.match(r'^(\d{4})_(.+?)(_\d{2}|_Ground)(\.fbx)$', filename)
                if match:
                    old_lpnumber = match.group(1)
                    old_addr = match.group(2)
                    suffix = match.group(3)
                    extension = match.group(4)
                    
                    # Формируем новое имя: новый lowpoly номер + новый адрес + суффикс
                    new_fbx_name = f"{lowpoly_number}_{new_address}{suffix}{extension}"
                    new_fbx_path = os.path.join(old_folder, new_fbx_name)
                    
                    try:
                        os.rename(old_fbx_path, new_fbx_path)
                        print(f"  FBX переименован: {filename} → {new_fbx_name}")
                        fbx_renamed += 1
                    except Exception as e:
                        print(f"  Ошибка переименования FBX {filename}: {e}")
        
        # Переименовываем саму папку
        new_folder_name = f"{lowpoly_number}_{new_address}"
        new_folder_path = os.path.join(blend_dir, new_folder_name)
        
        if old_folder != new_folder_path:
            try:
                os.rename(old_folder, new_folder_path)
                print(f"  Папка переименована: {old_folder_name} → {new_folder_name}")
            except Exception as e:
                print(f"  Ошибка переименования папки: {e}")
        else:
            print(f"  Папка уже называется правильно: {new_folder_name}")
        
        print(f"  Итого переименовано FBX файлов: {fbx_renamed}")

class BAKER_OT_agr_distribute_input_lowpoly_number(Operator):
    """Диалог ввода 4-значного номера для lowpoly коллекций"""
    bl_idname = "baker.agr_distribute_input_lowpoly_number"
    bl_label = "Введите номер lowpoly коллекции"
    bl_options = {'REGISTER', 'UNDO'}
    
    lowpoly_number: StringProperty(
        name="Номер коллекции",
        description="4-значный номер lowpoly коллекции (0000-9999)",
        default="0000",
        maxlen=4
    )
    
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)
    
    def draw(self, context):
        layout = self.layout
        layout.prop(self, "lowpoly_number", text="Номер")
        layout.label(text="Введите ровно 4 цифры (0000-9999)")
    
    def execute(self, context):
        # Проверяем, что введено ровно 4 цифры
        if len(self.lowpoly_number) != 4 or not self.lowpoly_number.isdigit():
            self.report({'ERROR'}, "Введите ровно 4 цифры (например: 0903)")
            return {'CANCELLED'}
        
        # Сохраняем номер во временное свойство
        context.scene.agr_lowpoly_number = self.lowpoly_number
        
        # Теперь вызываем основной оператор, который увидит это свойство
        bpy.ops.baker.agr_distribute_collections('EXEC_DEFAULT')
        
        # Очищаем временное свойство
        context.scene.agr_lowpoly_number = ""
        
        return {'FINISHED'}

class BAKER_OT_agr_rename_project(Operator):
    """Переименование всего проекта с заданным Address"""
    bl_idname = "baker.agr_rename_project"
    bl_label = "Переименовать проект"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        import re
        
        new_address = context.scene.agr_address
        if not new_address:
            self.report({'ERROR'}, "Введите Address в панели AGR_rename")
            return {'CANCELLED'}
        
        # Проверяем, был ли уже введен lowpoly номер
        lowpoly_number = getattr(context.scene, 'agr_project_lowpoly_number', "")
        
        if lowpoly_number:
            # Номер уже введен, выполняем переименование
            return self.execute_rename(context, new_address, lowpoly_number)
        
        # Определяем наличие lowpoly объектов
        has_lowpoly = self.detect_lowpoly_objects(context)
        
        if has_lowpoly:
            # Запрашиваем lowpoly номер
            bpy.ops.baker.agr_rename_project_input_lowpoly_number('INVOKE_DEFAULT')
            return {'FINISHED'}
        else:
            # Нет lowpoly - выполняем обычное переименование
            return self.execute_rename(context, new_address, None)
    
    def detect_lowpoly_objects(self, context):
        """Определяет наличие lowpoly объектов"""
        import re
        
        for obj in context.scene.objects:
            if obj.type != 'MESH':
                continue
            
            obj_name = obj.name
            obj_name_clean = re.sub(r'\.\d{3}$', '', obj_name)
            has_suffix = obj_name != obj_name_clean
            
            # Проверяем наличие суффикса .001, .002 и т.д.
            if has_suffix:
                # Main, MainGlass, Ground, GroundGlass с суффиксом - lowpoly
                if re.match(r'^SM_.+?(_\d{3})?_(Main|MainGlass|Ground|GroundGlass)', obj_name):
                    return True
            
            # GroundEl и Flora всегда lowpoly
            if re.match(r'^SM_.+?_(GroundEl|GroundElGlass|Flora)$', obj_name_clean):
                return True
        
        return False
    
    def execute_rename(self, context, new_address, lowpoly_number):
        """Основная логика переименования"""
        self.report({'INFO'}, f"Начинается переименование проекта на адрес: {new_address}")
        
        # 1. Переименование highpoly объектов (Main, MainGlass, Ground без суффиксов)
        highpoly_renamed = self.rename_highpoly_objects(context, new_address)
        
        # 2. Переименование lowpoly объектов (если есть lowpoly_number)
        lowpoly_renamed = 0
        if lowpoly_number:
            lowpoly_renamed = self.rename_lowpoly_objects(context, new_address)
        
        # 3. Переименование UCX объектов
        ucx_objects_renamed = self.rename_ucx_objects(context, new_address)
        
        # 4. Переименование текстур для Main и Ground объектов
        textures_renamed = self.rename_textures_for_objects(context, new_address)
        
        # 5. Переименование GEOJSON и FBX для Main и Ground объектов
        geojson_fbx_renamed = self.rename_geojson_fbx_for_objects(context, new_address)
        
        # 6. Переименование источников света
        lights_renamed = self.rename_lights_for_roots(context, new_address)
        
        # 7. Распределение по коллекциям
        self.distribute_to_collections(context, new_address, lowpoly_number)
        
        summary = f"Проект переименован! Highpoly: {highpoly_renamed}, Lowpoly: {lowpoly_renamed}, UCX: {ucx_objects_renamed}, "
        summary += f"Текстуры: {textures_renamed}, GEOJSON/FBX: {geojson_fbx_renamed}, Свет: {lights_renamed}"
        self.report({'INFO'}, summary)
        
        return {'FINISHED'}
    
    def rename_highpoly_objects(self, context, new_address):
        """Переименовывает highpoly объекты (без суффиксов .001, .002)"""
        import re
        renamed_count = 0
        
        for obj in context.scene.objects:
            if obj.type != 'MESH':
                continue
            
            obj_name = obj.name
            
            # Пропускаем объекты с суффиксами .001, .002 (это lowpoly)
            if re.search(r'\.\d{3}$', obj_name):
                continue
            
            # Main с номером: SM_*_001_Main или SM_*_001_MainGlass
            match = re.match(r'^SM_(.+?)_(\d{3})_(Main|MainGlass)$', obj_name)
            if match:
                old_address = match.group(1)
                number = match.group(2)
                obj_type = match.group(3)
                
                new_name = f"SM_{new_address}_{number}_{obj_type}"
                obj.name = new_name
                
                # Переименовываем материалы
                self.rename_materials(obj, new_address, number, obj_type)
                renamed_count += 1
                continue
            
            # Main без номера: SM_*_Main или SM_*_MainGlass
            match = re.match(r'^SM_(.+?)_(Main|MainGlass)$', obj_name)
            if match:
                old_address = match.group(1)
                obj_type = match.group(2)
                
                new_name = f"SM_{new_address}_{obj_type}"
                obj.name = new_name
                
                # Переименовываем материалы
                self.rename_materials(obj, new_address, None, obj_type)
                renamed_count += 1
                continue
            
            # Ground БЕЗ GroundEl и Flora (они всегда lowpoly)
            match = re.match(r'^SM_(.+?)_(Ground|GroundGlass)$', obj_name)
            if match:
                old_address = match.group(1)
                obj_type = match.group(2)
                
                new_name = f"SM_{new_address}_{obj_type}"
                obj.name = new_name
                
                # Переименовываем материалы
                self.rename_materials(obj, new_address, None, obj_type)
                renamed_count += 1
                continue
        
        print(f"✓ Переименовано highpoly объектов: {renamed_count}")
        return renamed_count
    
    def rename_lowpoly_objects(self, context, new_address):
        """Переименовывает lowpoly объекты (с суффиксами .001, .002, GroundEl, Flora)"""
        import re
        renamed_count = 0
        
        for obj in context.scene.objects:
            if obj.type != 'MESH':
                continue
            
            obj_name = obj.name
            obj_name_clean = re.sub(r'(\.\d{3})$', '', obj_name)  # Сохраняем суффикс
            suffix_match = re.search(r'(\.\d{3})$', obj_name)
            suffix = suffix_match.group(1) if suffix_match else ""
            
            # Main/MainGlass с номером и суффиксом: SM_*_001_Main.001
            match = re.match(r'^SM_(.+?)_(\d{3})_(Main|MainGlass)$', obj_name_clean)
            if match and suffix:
                old_address = match.group(1)
                number = match.group(2)
                obj_type = match.group(3)
                
                new_name = f"SM_{new_address}_{number}_{obj_type}{suffix}"
                obj.name = new_name
                
                # Переименовываем материалы
                self.rename_materials(obj, new_address, number, obj_type)
                renamed_count += 1
                continue
            
            # Ground/GroundGlass с суффиксом: SM_*_Ground.001
            match = re.match(r'^SM_(.+?)_(Ground|GroundGlass)$', obj_name_clean)
            if match and suffix:
                old_address = match.group(1)
                obj_type = match.group(2)
                
                new_name = f"SM_{new_address}_{obj_type}{suffix}"
                obj.name = new_name
                
                # Переименовываем материалы
                self.rename_materials(obj, new_address, None, obj_type)
                renamed_count += 1
                continue
            
            # GroundEl и Flora (всегда lowpoly, без суффиксов)
            match = re.match(r'^SM_(.+?)_(GroundEl|GroundElGlass|Flora)$', obj_name_clean)
            if match:
                old_address = match.group(1)
                obj_type = match.group(2)
                
                new_name = f"SM_{new_address}_{obj_type}"
                obj.name = new_name
                
                # Переименовываем материалы
                self.rename_materials(obj, new_address, None, obj_type)
                renamed_count += 1
                continue
        
        print(f"✓ Переименовано lowpoly объектов: {renamed_count}")
        return renamed_count
    
    def rename_materials(self, obj, address, number, obj_type):
        """Переименовывает материалы объекта (кроме M_Glass_##)"""
        import re
        
        if obj.data.materials:
            for idx, mat_slot in enumerate(obj.data.materials, 1):
                if mat_slot:
                    # Не переименовываем M_Glass_## материалы
                    if re.match(r'^M_Glass_\d{2}$', mat_slot.name):
                        print(f"  Пропущен материал стекла: {mat_slot.name}")
                        continue
                    
                    if number:
                        mat_name = f"M_{address}_{number}_{obj_type}_{idx}"
                    else:
                        mat_name = f"M_{address}_{obj_type}_{idx}"
                    mat_slot.name = mat_name
    
    def rename_ucx_objects(self, context, new_address):
        """Переименовывает UCX объекты"""
        import re
        renamed_count = 0
        
        for obj in context.scene.objects:
            if obj.type != 'MESH':
                continue
            
            obj_name = obj.name
            
            # UCX с номером Main: UCX_SM_*_001_Main_###
            match = re.match(r'^UCX_SM_(.+?)_(\d{3})_Main_(\d+)$', obj_name)
            if match:
                old_address = match.group(1)
                number = match.group(2)
                ucx_num = match.group(3)
                
                new_name = f"UCX_SM_{new_address}_{number}_Main_{ucx_num}"
                obj.name = new_name
                renamed_count += 1
                continue
            
            # UCX без номера Main: UCX_SM_*_Main_###
            match = re.match(r'^UCX_SM_(.+?)_Main_(\d+)$', obj_name)
            if match:
                old_address = match.group(1)
                ucx_num = match.group(2)
                
                new_name = f"UCX_SM_{new_address}_Main_{ucx_num}"
                obj.name = new_name
                renamed_count += 1
                continue
            
            # UCX Ground: UCX_SM_*_Ground_###
            match = re.match(r'^UCX_SM_(.+?)_Ground_(\d+)$', obj_name)
            if match:
                old_address = match.group(1)
                ucx_num = match.group(2)
                
                new_name = f"UCX_SM_{new_address}_Ground_{ucx_num}"
                obj.name = new_name
                renamed_count += 1
                continue
        
        print(f"✓ Переименовано UCX объектов: {renamed_count}")
        return renamed_count
    
    def rename_textures_for_objects(self, context, new_address):
        """Переименовывает текстуры для всех объектов (highpoly и lowpoly)"""
        import re
        renamed_count = 0
        
        for obj in context.scene.objects:
            if obj.type != 'MESH':
                continue
            
            if not obj.data.materials:
                continue
            
            obj_name = obj.name
            obj_name_clean = re.sub(r'\.\d{3}$', '', obj_name)  # Удаляем .001, .002
            
            # Проверяем, является ли объект с новым адресом
            # Highpoly Main с номером
            if re.match(r'^SM_' + re.escape(new_address) + r'_\d{3}_Main$', obj_name_clean):
                self.rename_textures_for_object(obj, new_address)
                renamed_count += 1
                continue
            
            # Highpoly Ground
            if re.match(r'^SM_' + re.escape(new_address) + r'_(Ground|GroundGlass)$', obj_name_clean):
                self.rename_textures_for_object(obj, new_address)
                renamed_count += 1
                continue
            
            # Lowpoly Main/MainGlass с суффиксом .001, .002
            if re.match(r'^SM_' + re.escape(new_address) + r'_\d{3}_(Main|MainGlass)$', obj_name_clean):
                if re.search(r'\.\d{3}$', obj_name):  # Есть суффикс
                    self.rename_textures_for_object(obj, new_address)
                    renamed_count += 1
                    continue
            
            # Lowpoly Ground/GroundGlass с суффиксом
            if re.match(r'^SM_' + re.escape(new_address) + r'_(Ground|GroundGlass)$', obj_name_clean):
                if re.search(r'\.\d{3}$', obj_name):  # Есть суффикс
                    self.rename_textures_for_object(obj, new_address)
                    renamed_count += 1
                    continue
            
            # Lowpoly GroundEl и Flora (всегда lowpoly)
            if re.match(r'^SM_' + re.escape(new_address) + r'_(GroundEl|GroundElGlass|Flora)$', obj_name_clean):
                self.rename_textures_for_object(obj, new_address)
                renamed_count += 1
                continue
        
        print(f"✓ Переименовано текстур для объектов: {renamed_count}")
        return renamed_count
    
    def rename_textures_for_object(self, obj, new_address):
        """Переименовывает текстуры для одного объекта (вызывает существующий оператор)"""
        try:
            # Делаем объект активным
            bpy.context.view_layer.objects.active = obj
            
            # Вызываем оператор переименования текстур
            bpy.ops.baker.agr_rename_textures('EXEC_DEFAULT')
            
            print(f"  ✓ Текстуры переименованы для: {obj.name}")
        except Exception as e:
            print(f"  ✗ Ошибка переименования текстур для {obj.name}: {e}")
    
    def parse_object_name(self, obj_name):
        """Парсит имя объекта"""
        import re
        
        # Main с номером
        match = re.match(r'^SM_(.+?)_(\d{3})_(Main|MainGlass)$', obj_name)
        if match:
            return match.group(1), match.group(2), 'Main'
        
        # Ground
        match = re.match(r'^SM_(.+?)_(Ground|GroundGlass|GroundEl|GroundElGlass)$', obj_name)
        if match:
            return match.group(1), None, 'Ground'
        
        return None
    
    def get_texture_folder_from_material(self, obj):
        """Получает путь к папке с текстурами"""
        for mat_slot in obj.data.materials:
            if mat_slot and mat_slot.use_nodes:
                for node in mat_slot.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image:
                        if node.image.source == 'TILED':
                            if node.image.filepath:
                                abs_path = bpy.path.abspath(node.image.filepath)
                                folder_path = os.path.dirname(abs_path)
                                return folder_path
        return None
    
    def rename_textures(self, folder_path, new_address, number, obj_type):
        """Переименовывает текстуры в папке"""
        import re
        renamed_count = 0
        
        for filename in os.listdir(folder_path):
            if not filename.endswith('.png'):
                continue
            
            # Проверяем UDIM паттерн
            udim_match = re.search(r'\.(\d{4})\.png$', filename)
            if not udim_match:
                continue
            
            udim_number = udim_match.group(1)
            
            # Определяем тип текстуры
            texture_type = self.get_texture_type(filename)
            if not texture_type:
                continue
            
            # Проверяем соответствие маске
            should_rename = False
            material_num = None
            
            if obj_type == 'Main' and number:
                pattern = r'^T_.+?_' + re.escape(number) + r'_' + re.escape(texture_type) + r'_(\d+)\.\d{4}\.png$'
                match = re.match(pattern, filename)
                if match:
                    should_rename = True
                    material_num = match.group(1)
            elif obj_type == 'Ground':
                pattern = r'^T_.+?_Ground_' + re.escape(texture_type) + r'_(\d+)\.\d{4}\.png$'
                match = re.match(pattern, filename)
                if match:
                    should_rename = True
                    material_num = match.group(1)
            
            if not should_rename:
                continue
            
            # Формируем новое имя
            if obj_type == 'Main' and number:
                new_filename = f"T_{new_address}_{number}_{texture_type}_{material_num}.{udim_number}.png"
            elif obj_type == 'Ground':
                new_filename = f"T_{new_address}_Ground_{texture_type}_{material_num}.{udim_number}.png"
            else:
                continue
            
            old_path = os.path.join(folder_path, filename)
            new_path = os.path.join(folder_path, new_filename)
            
            try:
                if old_path != new_path:
                    os.rename(old_path, new_path)
                    renamed_count += 1
            except Exception as e:
                print(f"  Ошибка переименования текстуры {filename}: {e}")
        
        return renamed_count
    
    def get_texture_type(self, filename):
        """Определяет тип текстуры по имени файла"""
        if 'Diffuse' in filename:
            return 'Diffuse'
        elif 'Normal' in filename:
            return 'Normal'
        elif 'ERM' in filename or 'ORM' in filename:
            return 'ERM'
        return None
    
    def get_new_folder_name(self, address, number, obj_type):
        """Формирует новое имя папки"""
        if obj_type == 'Main' and number:
            return f"SM_{address}_{number}"
        elif obj_type == 'Ground':
            return f"SM_{address}_Ground"
        return None
    
    def update_material_paths(self, obj, old_folder, new_folder):
        """Переподключает текстуры в материалах"""
        # Получаем список новых текстур
        new_textures = {}
        for filename in os.listdir(new_folder):
            if filename.endswith('.1001.png'):
                if 'Diffuse' in filename:
                    new_textures['Diffuse'] = filename
                elif 'Normal' in filename:
                    new_textures['Normal'] = filename
                elif 'ERM' in filename or 'ORM' in filename:
                    new_textures['ERM'] = filename
        
        for mat_slot in obj.data.materials:
            if not mat_slot or not mat_slot.use_nodes:
                continue
            
            nodes = mat_slot.node_tree.nodes
            
            # Ищем BSDF
            bsdf = None
            for node in nodes:
                if node.type == 'BSDF_PRINCIPLED':
                    bsdf = node
                    break
            
            if not bsdf:
                continue
            
            # Diffuse
            if bsdf.inputs['Base Color'].is_linked and 'Diffuse' in new_textures:
                diffuse_link = bsdf.inputs['Base Color'].links[0]
                diffuse_node = diffuse_link.from_node
                if diffuse_node.type == 'TEX_IMAGE':
                    self.load_new_texture(new_folder, new_textures['Diffuse'], diffuse_node, 'sRGB')
            
            # ERM
            for node in nodes:
                if node.type in ['SEPRGB', 'SEPARATE_COLOR', 'SEPARATE_XYZ']:
                    is_connected = any(link.to_node == bsdf for output in node.outputs for link in output.links)
                    if is_connected and node.inputs[0].is_linked and 'ERM' in new_textures:
                        erm_node = node.inputs[0].links[0].from_node
                        if erm_node.type == 'TEX_IMAGE':
                            self.load_new_texture(new_folder, new_textures['ERM'], erm_node, 'Non-Color')
                        break
            
            # Normal
            for node in nodes:
                if node.type == 'NORMAL_MAP':
                    if node.outputs['Normal'].is_linked and node.inputs['Color'].is_linked and 'Normal' in new_textures:
                        if any(link.to_node == bsdf for link in node.outputs['Normal'].links):
                            normal_node = node.inputs['Color'].links[0].from_node
                            if normal_node.type == 'TEX_IMAGE':
                                self.load_new_texture(new_folder, new_textures['Normal'], normal_node, 'Non-Color')
                            break
    
    def load_new_texture(self, folder, filename, node, color_space='sRGB'):
        """Загружает новую UDIM текстуру в ноду"""
        try:
            base_name = filename.replace('.1001.png', '')
            udim_path = os.path.join(folder, f"{base_name}.<UDIM>.png")
            abs_path = os.path.abspath(udim_path)
            
            new_image = bpy.data.images.load(abs_path, check_existing=False)
            new_image.source = 'TILED'
            new_image.colorspace_settings.name = color_space
            node.image = new_image
            
            return bpy.path.relpath(abs_path)
        except Exception as e:
            print(f"  Ошибка загрузки текстуры {filename}: {e}")
            return None
    
    def rename_geojson_fbx_for_objects(self, context, new_address):
        """Переименовывает GEOJSON и FBX файлы"""
        import re
        renamed_count = 0
        
        processed_folders = set()
        
        for obj in context.scene.objects:
            if obj.type != 'MESH':
                continue
            
            obj_name = obj.name
            
            # Проверяем Main или Ground с новым адресом
            match_main = re.match(r'^SM_' + re.escape(new_address) + r'_(\d{3})_Main$', obj_name)
            match_ground = re.match(r'^SM_' + re.escape(new_address) + r'_Ground$', obj_name)
            
            if match_main or match_ground:
                try:
                    parsed = self.parse_object_name(obj_name)
                    if not parsed:
                        continue
                    
                    current_address, number, obj_type = parsed
                    
                    if obj_type not in ['Main', 'Ground']:
                        continue
                    
                    texture_folder = self.get_texture_folder_from_material(obj)
                    if not texture_folder or not os.path.exists(texture_folder):
                        continue
                    
                    # Избегаем повторной обработки одной папки
                    if texture_folder in processed_folders:
                        continue
                    processed_folders.add(texture_folder)
                    
                    # Переименовываем GEOJSON
                    geojson_renamed = self.rename_geojson_in_folder(texture_folder, new_address, number, obj_type)
                    
                    # Переименовываем FBX
                    fbx_renamed = self.rename_fbx_in_folder(texture_folder, new_address, number, obj_type)
                    
                    if geojson_renamed or fbx_renamed:
                        renamed_count += 1
                
                except Exception as e:
                    print(f"  Ошибка переименования GEOJSON/FBX для {obj_name}: {e}")
        
        print(f"✓ Переименовано GEOJSON/FBX для объектов: {renamed_count}")
        return renamed_count
    
    def rename_geojson_in_folder(self, folder_path, new_address, number, obj_type):
        """Переименовывает GEOJSON файл в папке"""
        import re
        
        try:
            if obj_type == 'Main' and number:
                pattern = r'^SM_(.+?)_' + re.escape(number) + r'\.geojson$'
                new_name = f"SM_{new_address}_{number}.geojson"
            elif obj_type == 'Ground':
                pattern = r'^SM_(.+?)_Ground\.geojson$'
                new_name = f"SM_{new_address}_Ground.geojson"
            else:
                return False
            
            for filename in os.listdir(folder_path):
                match = re.match(pattern, filename)
                if match:
                    old_path = os.path.join(folder_path, filename)
                    new_path = os.path.join(folder_path, new_name)
                    
                    if old_path != new_path:
                        # Читаем и обновляем содержимое
                        with open(old_path, 'r', encoding='utf-8') as f:
                            geojson_data = json.load(f)
                        
                        old_address = match.group(1)
                        self.update_glass_materials_in_geojson(geojson_data, old_address, new_address)
                        
                        # Сохраняем с новым именем
                        with open(new_path, 'w', encoding='utf-8') as f:
                            json.dump(geojson_data, f, ensure_ascii=False, indent=2)
                        
                        # Удаляем старый файл
                        if old_path != new_path:
                            os.remove(old_path)
                        
                        return True
        except Exception as e:
            print(f"    Ошибка переименования GEOJSON: {e}")
        
        return False
    
    def update_glass_materials_in_geojson(self, geojson_data, old_address, new_address):
        """Обновляет адреса материалов в GEOJSON"""
        try:
            if 'features' in geojson_data:
                for feature in geojson_data['features']:
                    if 'Glasses' in feature:
                        for glass_list in feature['Glasses']:
                            if isinstance(glass_list, dict):
                                new_glass_dict = {}
                                for old_mat_name, mat_data in glass_list.items():
                                    new_mat_name = old_mat_name.replace(f"M_{old_address}_", f"M_{new_address}_")
                                    new_glass_dict[new_mat_name] = mat_data
                                glass_list.clear()
                                glass_list.update(new_glass_dict)
        except Exception as e:
            print(f"    Ошибка обновления материалов в GEOJSON: {e}")
    
    def rename_fbx_in_folder(self, folder_path, new_address, number, obj_type):
        """Переименовывает FBX файлы в папке"""
        import re
        renamed = False
        
        try:
            if obj_type == 'Main' and number:
                # С номером
                pattern_main = r'^SM_(.+?)_' + re.escape(number) + r'\.fbx$'
                pattern_light = r'^SM_(.+?)_' + re.escape(number) + r'_Light\.fbx$'
                new_main = f"SM_{new_address}_{number}.fbx"
                new_light = f"SM_{new_address}_{number}_Light.fbx"
            elif obj_type == 'Ground':
                pattern_main = r'^SM_(.+?)_Ground\.fbx$'
                pattern_light = r'^SM_(.+?)_Ground_Light\.fbx$'
                new_main = f"SM_{new_address}_Ground.fbx"
                new_light = f"SM_{new_address}_Ground_Light.fbx"
            else:
                return False
            
            for filename in os.listdir(folder_path):
                match = re.match(pattern_main, filename)
                if match:
                    old_path = os.path.join(folder_path, filename)
                    new_path = os.path.join(folder_path, new_main)
                    if old_path != new_path:
                        os.rename(old_path, new_path)
                        renamed = True
                
                match = re.match(pattern_light, filename)
                if match:
                    old_path = os.path.join(folder_path, filename)
                    new_path = os.path.join(folder_path, new_light)
                    if old_path != new_path:
                        os.rename(old_path, new_path)
                        renamed = True
        
        except Exception as e:
            print(f"    Ошибка переименования FBX: {e}")
        
        return renamed
    
    def rename_lights_for_roots(self, context, new_address):
        """Переименовывает Empty Root объекты и их источники света"""
        import re
        renamed_count = 0
        
        for obj in context.scene.objects:
            if obj.type != 'EMPTY':
                continue
            
            obj_name = obj.name
            
            # Ground Root: *_Ground_Root (проверяем ПЕРВЫМ, т.к. он более специфичный)
            match = re.match(r'^(.+?)_Ground_Root$', obj_name)
            if match:
                old_address = match.group(1)
                
                new_root_name = f"{new_address}_Ground_Root"
                obj.name = new_root_name
                
                # Переименовываем дочерние источники света
                self.rename_child_lights(obj, new_address, None, 'Ground')
                renamed_count += 1
                continue
            
            # Main Root с номером: *_001_Root
            match = re.match(r'^(.+?)_(\d{3})_Root$', obj_name)
            if match:
                old_address = match.group(1)
                number = match.group(2)
                
                new_root_name = f"{new_address}_{number}_Root"
                obj.name = new_root_name
                
                # Переименовываем дочерние источники света
                self.rename_child_lights(obj, new_address, number, 'Main')
                renamed_count += 1
                continue
            
            # Main Root без номера: *_Root (проверяем ПОСЛЕДНИМ, т.к. самый общий)
            match = re.match(r'^(.+?)_Root$', obj_name)
            if match:
                old_address = match.group(1)
                
                new_root_name = f"{new_address}_Root"
                obj.name = new_root_name
                
                # Переименовываем дочерние источники света
                self.rename_child_lights(obj, new_address, None, 'Main')
                renamed_count += 1
                continue
        
        print(f"✓ Переименовано Root объектов со светом: {renamed_count}")
        return renamed_count
    
    def rename_child_lights(self, root_obj, address, number, obj_type):
        """Переименовывает дочерние источники света"""
        spot_counter = 1
        point_counter = 1
        
        light_objects = [child for child in root_obj.children if child.type == 'LIGHT']
        light_objects.sort(key=lambda x: x.name)
        
        for light_obj in light_objects:
            light_type = light_obj.data.type
            
            if light_type == 'SPOT':
                lighttype_name = 'Spot'
                counter = spot_counter
                spot_counter += 1
            elif light_type == 'POINT':
                lighttype_name = 'Point'
                counter = point_counter
                point_counter += 1
            else:
                continue
            
            # Формируем имя
            if obj_type == 'Ground':
                new_light_name = f"{address}_Ground_{lighttype_name}_{counter:03d}"
            elif obj_type == 'Main' and number:
                new_light_name = f"{address}_{number}_{lighttype_name}_{counter:03d}"
            else:  # Main без номера
                new_light_name = f"{address}_{lighttype_name}_{counter:03d}"
            
            light_obj.name = new_light_name
    
    def distribute_to_collections(self, context, new_address, lowpoly_number=None):
        """Распределяет объекты по коллекциям"""
        # Используем существующий оператор распределения
        # Сохраняем address
        context.scene.agr_address = new_address
        
        # Если есть lowpoly_number, сохраняем его и вызываем оператор
        if lowpoly_number:
            context.scene.agr_lowpoly_number = lowpoly_number
        
        # Вызываем оператор распределения
        bpy.ops.baker.agr_distribute_collections('EXEC_DEFAULT')
        
        # Очищаем временное свойство
        if lowpoly_number:
            context.scene.agr_lowpoly_number = ""
        
        return
    
    def distribute_to_collections_old(self, context, new_address):
        """Распределяет объекты по коллекциям (старый метод)"""
        import re
        
        # Словарь для хранения объектов по категориям
        collections_data = {}
        
        # Проходим по всем объектам в сцене
        for obj in context.scene.objects:
            obj_name = obj.name
            
            # 1. Main объекты с номером
            match = re.match(r'^SM_' + re.escape(new_address) + r'_(\d{3})_(Main|MainGlass)$', obj_name)
            if match:
                number = match.group(1)
                coll_name = f"SM_{new_address}_{number}.fbx"
                if coll_name not in collections_data:
                    collections_data[coll_name] = []
                collections_data[coll_name].append(obj)
                continue
            
            # 2. Main UCX с номером
            match = re.match(r'^UCX_SM_' + re.escape(new_address) + r'_(\d{3})_Main_\d+$', obj_name)
            if match:
                number = match.group(1)
                coll_name = f"SM_{new_address}_{number}.fbx"
                if coll_name not in collections_data:
                    collections_data[coll_name] = []
                collections_data[coll_name].append(obj)
                continue
            
            # 3. Main Root с номером
            match = re.match(r'^' + re.escape(new_address) + r'_(\d{3})_Root$', obj_name)
            if match:
                number = match.group(1)
                coll_name = f"SM_{new_address}_{number}_Light.fbx"
                if coll_name not in collections_data:
                    collections_data[coll_name] = []
                collections_data[coll_name].append(obj)
                for child in obj.children:
                    if child.type == 'LIGHT':
                        collections_data[coll_name].append(child)
                continue
            
            # 4. Main объекты БЕЗ номера
            match = re.match(r'^SM_' + re.escape(new_address) + r'_(Main|MainGlass)$', obj_name)
            if match:
                coll_name = f"SM_{new_address}.fbx"
                if coll_name not in collections_data:
                    collections_data[coll_name] = []
                collections_data[coll_name].append(obj)
                continue
            
            # 5. Main UCX БЕЗ номера
            match = re.match(r'^UCX_SM_' + re.escape(new_address) + r'_Main_\d+$', obj_name)
            if match:
                coll_name = f"SM_{new_address}.fbx"
                if coll_name not in collections_data:
                    collections_data[coll_name] = []
                collections_data[coll_name].append(obj)
                continue
            
            # 6. Main Root БЕЗ номера
            match = re.match(r'^' + re.escape(new_address) + r'_Root$', obj_name)
            if match:
                coll_name = f"SM_{new_address}_Light.fbx"
                if coll_name not in collections_data:
                    collections_data[coll_name] = []
                collections_data[coll_name].append(obj)
                for child in obj.children:
                    if child.type == 'LIGHT':
                        collections_data[coll_name].append(child)
                continue
            
            # 7. Ground объекты
            match = re.match(r'^SM_' + re.escape(new_address) + r'_(Ground|GroundGlass|GroundEl|GroundElGlass)$', obj_name)
            if match:
                coll_name = f"SM_{new_address}_Ground.fbx"
                if coll_name not in collections_data:
                    collections_data[coll_name] = []
                collections_data[coll_name].append(obj)
                continue
            
            # 8. Ground UCX
            match = re.match(r'^UCX_SM_' + re.escape(new_address) + r'_Ground_\d+$', obj_name)
            if match:
                coll_name = f"SM_{new_address}_Ground.fbx"
                if coll_name not in collections_data:
                    collections_data[coll_name] = []
                collections_data[coll_name].append(obj)
                continue
            
            # 9. Ground Root
            match = re.match(r'^' + re.escape(new_address) + r'_Ground_Root$', obj_name)
            if match:
                coll_name = f"SM_{new_address}_Ground_Light.fbx"
                if coll_name not in collections_data:
                    collections_data[coll_name] = []
                collections_data[coll_name].append(obj)
                for child in obj.children:
                    if child.type == 'LIGHT':
                        collections_data[coll_name].append(child)
                continue
        
        # Создаем коллекции и перемещаем объекты
        total_objects = 0
        collections_created = 0
        
        for coll_name, objects in collections_data.items():
            if not objects:
                continue
            
            # Создаем коллекцию если её нет
            if coll_name not in bpy.data.collections:
                new_coll = bpy.data.collections.new(coll_name)
                context.scene.collection.children.link(new_coll)
                collections_created += 1
            else:
                new_coll = bpy.data.collections[coll_name]
            
            # Перемещаем объекты в коллекцию
            for obj in objects:
                # Удаляем из всех текущих коллекций
                for old_coll in obj.users_collection:
                    old_coll.objects.unlink(obj)
                
                # Добавляем в новую коллекцию
                if obj.name not in new_coll.objects:
                    new_coll.objects.link(obj)
                    total_objects += 1
        
        # Очищаем пустые коллекции
        collections_removed = self.remove_empty_collections(context)
        
        print(f"✓ Распределено по коллекциям: {total_objects} объектов в {len(collections_data)} коллекций")
        if collections_removed > 0:
            print(f"  Удалено пустых коллекций: {collections_removed}")
    
    def remove_empty_collections(self, context):
        """Удаляет пустые коллекции из сцены"""
        removed_count = 0
        
        def remove_empty_recursive(collection):
            """Рекурсивно удаляет пустые дочерние коллекции"""
            nonlocal removed_count
            
            # Сначала обрабатываем дочерние коллекции
            for child in list(collection.children):
                remove_empty_recursive(child)
            
            # Проверяем, пуста ли коллекция
            if len(collection.objects) == 0 and len(collection.children) == 0:
                if collection != context.scene.collection:
                    # Удаляем коллекцию из всех родительских коллекций
                    for parent in bpy.data.collections:
                        if collection.name in parent.children:
                            parent.children.unlink(collection)
                    
                    # Удаляем из сцены
                    if collection.name in context.scene.collection.children:
                        context.scene.collection.children.unlink(collection)
                    
                    # Удаляем из данных
                    bpy.data.collections.remove(collection)
                    removed_count += 1
        
        # Начинаем с главной коллекции сцены
        remove_empty_recursive(context.scene.collection)
        
        return removed_count

class BAKER_OT_agr_rename_project_input_lowpoly_number(Operator):
    """Диалог ввода 4-значного номера для lowpoly при переименовании проекта"""
    bl_idname = "baker.agr_rename_project_input_lowpoly_number"
    bl_label = "Введите номер lowpoly коллекции"
    bl_options = {'REGISTER', 'UNDO'}
    
    lowpoly_number: StringProperty(
        name="Номер коллекции",
        description="4-значный номер lowpoly коллекции (0000-9999)",
        default="0000",
        maxlen=4
    )
    
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)
    
    def draw(self, context):
        layout = self.layout
        layout.prop(self, "lowpoly_number", text="Номер")
        layout.label(text="Введите ровно 4 цифры (0000-9999)")
    
    def execute(self, context):
        # Проверяем, что введено ровно 4 цифры
        if len(self.lowpoly_number) != 4 or not self.lowpoly_number.isdigit():
            self.report({'ERROR'}, "Введите ровно 4 цифры (например: 0903)")
            return {'CANCELLED'}
        
        new_address = context.scene.agr_address
        if not new_address:
            self.report({'ERROR'}, "Address не установлен")
            return {'CANCELLED'}
        
        # Сохраняем lowpoly_number во временное свойство для проекта
        context.scene.agr_project_lowpoly_number = self.lowpoly_number
        
        # Вызываем основной оператор через bpy.ops
        bpy.ops.baker.agr_rename_project('EXEC_DEFAULT')
        
        # Очищаем временное свойство
        context.scene.agr_project_lowpoly_number = ""
        
        return {'FINISHED'}

class BAKER_OT_agr_rename_textures(Operator):
    """Переименование текстур объекта (автоматически определяет UDIM или обычные)"""
    bl_idname = "baker.agr_rename_textures"
    bl_label = "Переименовать текстуры"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        active_obj = context.active_object
        if not active_obj or active_obj.type != 'MESH':
            self.report({'ERROR'}, "Выберите MESH объект")
            return {'CANCELLED'}
        
        # Проверяем наличие Address
        address = context.scene.agr_address
        if not address:
            self.report({'ERROR'}, "Введите Address в панели AGR_rename")
            return {'CANCELLED'}
        
        # Парсим имя объекта для получения type и number
        obj_name = active_obj.name
        parsed = self.parse_object_name(obj_name)
        
        if not parsed:
            self.report({'ERROR'}, f"Не удалось распознать формат имени объекта: {obj_name}")
            return {'CANCELLED'}
        
        current_address, number, obj_type = parsed
        
        if obj_type not in ['Main', 'Ground', 'GroundEl', 'Flora']:
            self.report({'ERROR'}, f"Переименование текстур поддерживается только для типов Main, Ground, GroundEl, Flora")
            return {'CANCELLED'}
        
        # Проверяем наличие материалов
        if not active_obj.data.materials:
            self.report({'ERROR'}, "У объекта нет материалов")
            return {'CANCELLED'}
        
        # Определяем тип текстур (UDIM или обычные)
        texture_type = self.detect_texture_type(active_obj)
        
        if not texture_type:
            self.report({'ERROR'}, "Не найдены текстуры в материалах объекта")
            return {'CANCELLED'}
        
        # Выбираем путь обработки в зависимости от типа текстур
        if texture_type == 'UDIM':
            # СТАРЫЙ ПУТЬ: для UDIM текстур (highpoly)
            return self.process_udim_textures(active_obj, address, number, obj_type)
        else:
            # НОВЫЙ ПУТЬ: для обычных текстур (lowpoly)
            return self.process_regular_textures(active_obj, address, number, obj_type)
    
    def detect_texture_type(self, obj):
        """Определяет тип текстур в материалах объекта: UDIM или обычные"""
        has_udim = False
        has_regular = False
        
        for mat_slot in obj.data.materials:
            if mat_slot and mat_slot.use_nodes:
                for node in mat_slot.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image:
                        if node.image.source == 'TILED':
                            has_udim = True
                        elif node.image.filepath:
                            has_regular = True
        
        # Приоритет у UDIM, если есть оба типа
        if has_udim:
            return 'UDIM'
        elif has_regular:
            return 'REGULAR'
        
        return None
    
    def process_udim_textures(self, obj, address, number, obj_type):
        """СТАРЫЙ ПУТЬ: Обработка UDIM текстур (highpoly)"""
        print(f"\n=== Обработка UDIM текстур (highpoly) ===")
        
        # Получаем путь к папке с UDIM текстурами
        texture_folder = self.get_udim_texture_folder(obj)
        
        if not texture_folder:
            self.report({'ERROR'}, "Не найдены UDIM текстуры в материалах объекта")
            return {'CANCELLED'}
        
        if not os.path.exists(texture_folder):
            self.report({'ERROR'}, f"Папка с текстурами не найдена: {texture_folder}")
            return {'CANCELLED'}
        
        # СНАЧАЛА переименовываем текстуры в текущей папке
        renamed_count = self.rename_textures(texture_folder, address, number, obj_type)
        
        if renamed_count == 0:
            self.report({'WARNING'}, "Не найдены текстуры для переименования по заданной маске")
            return {'CANCELLED'}
        
        # ПОТОМ переименовываем саму папку
        new_folder_name = self.get_new_folder_name(address, number, obj_type)
        parent_folder = os.path.dirname(texture_folder)
        new_folder_path = os.path.join(parent_folder, new_folder_name)
        
        folder_renamed = False
        try:
            if texture_folder != new_folder_path and os.path.exists(texture_folder):
                os.rename(texture_folder, new_folder_path)
                folder_renamed = True
                
                # Обновляем пути в материалах после переименования папки
                self.update_material_paths(obj, texture_folder, new_folder_path)
        except Exception as e:
            self.report({'WARNING'}, f"Переименовано {renamed_count} текстур, но не удалось переименовать папку: {str(e)}")
            return {'FINISHED'}
        
        if folder_renamed:
            self.report({'INFO'}, f"Переименовано {renamed_count} UDIM текстур и папка в {new_folder_name}")
        else:
            self.report({'INFO'}, f"Переименовано {renamed_count} UDIM текстур")
        
        return {'FINISHED'}
    
    def process_regular_textures(self, obj, address, number, obj_type):
        """НОВЫЙ ПУТЬ: Обработка обычных текстур (lowpoly)"""
        print(f"\n=== Обработка обычных текстур (lowpoly) ===")
        
        # Получаем текстуры из материалов
        textures = self.get_regular_textures(obj)
        
        if not textures:
            self.report({'ERROR'}, "Не найдены обычные текстуры в материалах объекта")
            return {'CANCELLED'}
        
        # Получаем путь к файлу blend
        blend_filepath = bpy.data.filepath
        if not blend_filepath:
            self.report({'ERROR'}, "Сохраните файл перед переименованием текстур")
            return {'CANCELLED'}
        
        # Создаем папку low_texture рядом с файлом
        blend_dir = os.path.dirname(blend_filepath)
        low_texture_folder = os.path.join(blend_dir, "low_texture")
        
        if not os.path.exists(low_texture_folder):
            os.makedirs(low_texture_folder)
        
        # Переименовываем и сохраняем текстуры
        renamed_count = self.process_textures(obj, textures, low_texture_folder, address, number, obj_type)
        
        if renamed_count == 0:
            self.report({'WARNING'}, "Не удалось обработать текстуры")
            return {'CANCELLED'}
        
        self.report({'INFO'}, f"Обработано {renamed_count} обычных текстур в папке low_texture")
        return {'FINISHED'}
    
    def parse_object_name(self, obj_name):
        """Парсит имя объекта и возвращает (address, number, type)"""
        import re
        
        # Удаляем суффиксы .001, .002 и т.д. (от дублирования объектов)
        obj_name_clean = re.sub(r'\.\d{3}$', '', obj_name)
        
        # Паттерн для Main: SM_Address_001_Main
        pattern_main = r'^SM_(.+?)_(\d{3})_(Main|MainGlass)$'
        match = re.match(pattern_main, obj_name_clean)
        if match:
            return match.group(1), match.group(2), 'Main'
        
        # Паттерн для Ground: SM_Address_Ground
        pattern_ground = r'^SM_(.+?)_(Ground|GroundGlass)$'
        match = re.match(pattern_ground, obj_name_clean)
        if match:
            return match.group(1), None, 'Ground'
        
        # Паттерн для GroundEl: SM_Address_GroundEl или SM_Address_GroundElGlass
        pattern_groundel = r'^SM_(.+?)_(GroundEl|GroundElGlass)$'
        match = re.match(pattern_groundel, obj_name_clean)
        if match:
            obj_type = match.group(2)  # Используем оригинальное написание из объекта
            return match.group(1), None, obj_type
        
        # Паттерн для Flora: SM_Address_Flora
        pattern_flora = r'^SM_(.+?)_(Flora)$'
        match = re.match(pattern_flora, obj_name_clean)
        if match:
            return match.group(1), None, 'Flora'
        
        return None
    
    def get_udim_texture_folder(self, obj):
        """Получает путь к папке с UDIM текстурами"""
        for mat_slot in obj.data.materials:
            if mat_slot and mat_slot.use_nodes:
                for node in mat_slot.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image:
                        if node.image.source == 'TILED':
                            # Это UDIM текстура - получаем путь к папке
                            if node.image.filepath:
                                abs_path = bpy.path.abspath(node.image.filepath)
                                folder_path = os.path.dirname(abs_path)
                                return folder_path
        
        return None
    
    def get_regular_textures(self, obj):
        """Получает список обычных (не UDIM) текстур из материала"""
        textures = []
        processed_images = set()  # Чтобы не добавлять дубликаты
        
        for mat_slot in obj.data.materials:
            if mat_slot and mat_slot.use_nodes:
                for node in mat_slot.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image:
                        # Ищем ОБЫЧНЫЕ текстуры (не UDIM)
                        if node.image.source != 'TILED' and node.image.filepath:
                            # Избегаем дубликатов
                            if node.image.name not in processed_images:
                                textures.append(node.image)
                                processed_images.add(node.image.name)
                    
                    # Также ищем текстуры, подключенные к Normal Map ноде
                    elif node.type == 'NORMAL_MAP':
                        # Проверяем входы Normal Map ноды
                        if 'Color' in node.inputs:
                            color_input = node.inputs['Color']
                            if color_input.is_linked:
                                # Получаем подключенную ноду
                                linked_node = color_input.links[0].from_node
                                if linked_node.type == 'TEX_IMAGE' and linked_node.image:
                                    if linked_node.image.source != 'TILED' and linked_node.image.filepath:
                                        # Избегаем дубликатов
                                        if linked_node.image.name not in processed_images:
                                            textures.append(linked_node.image)
                                            processed_images.add(linked_node.image.name)
        
        return textures if textures else None
    
    def get_texture_type_from_filename(self, filename):
        """Определяет тип lowpoly текстуры по имени файла"""
        filename_lower = filename.lower()
        
        # Ищем паттерны: _d_, _o_, _m_, _n_, _r_ (или перед расширением)
        if '_d_' in filename_lower or filename_lower.endswith('_d.png'):
            return 'd'
        elif '_o_' in filename_lower or filename_lower.endswith('_o.png'):
            return 'o'
        elif '_m_' in filename_lower or filename_lower.endswith('_m.png'):
            return 'm'
        elif '_n_' in filename_lower or filename_lower.endswith('_n.png'):
            return 'n'
        elif '_r_' in filename_lower or filename_lower.endswith('_r.png'):
            return 'r'
        
        return None
    
    def get_color_space_for_texture_type(self, tex_type):
        """Возвращает правильный color space для типа текстуры"""
        if tex_type == 'd':  # diffuse
            return 'sRGB'
        elif tex_type in ['o', 'm', 'r']:  # opacity, metallic, roughness - технические
            return 'Non-Color'
        elif tex_type == 'n':  # normal
            return 'Non-Color'
        
        return 'sRGB'  # по умолчанию
    
    def process_textures(self, obj, textures, target_folder, address, number, obj_type):
        """Обрабатывает текстуры: сохраняет, переименовывает, переподключает"""
        import shutil
        renamed_count = 0
        old_images = []  # Список старых изображений для удаления
        new_texture_paths = {}  # {тип: новый_путь}
        
        print(f"\n=== Обработка lowpoly текстур ===")
        print(f"Объект: {obj.name}")
        print(f"Address: {address}, Number: {number}, Type: {obj_type}")
        print(f"Целевая папка: {target_folder}")
        print(f"Найдено текстур: {len(textures)}")
        
        # Обрабатываем каждую текстуру
        for img in textures:
            print(f"\n  Обработка изображения: {img.name}")
            
            # Проверяем, упакована ли текстура внутри .blend файла
            is_packed = img.packed_file is not None
            print(f"    Упакована в .blend: {is_packed}")
            
            # Определяем тип текстуры по имени
            filename = img.name if not img.filepath else os.path.basename(img.filepath)
            tex_type = self.get_texture_type_from_filename(filename)
            
            if not tex_type:
                print(f"    Пропущена (не распознан тип): {filename}")
                continue
            
            print(f"    Тип текстуры: {tex_type}")
            
            # Если текстура упакована, сначала сохраняем её на диск
            if is_packed:
                print(f"    Текстура упакована, распаковываем...")
                # Создаем временный путь для распаковки
                temp_filename = f"temp_{img.name}"
                temp_path = os.path.join(target_folder, temp_filename)
                
                try:
                    # Сохраняем упакованную текстуру на диск
                    img.filepath = temp_path
                    img.save()
                    old_abs_path = temp_path
                    print(f"    Распаковано в: {temp_path}")
                except Exception as e:
                    print(f"    ✗ Ошибка распаковки: {e}")
                    continue
            else:
                # Текстура не упакована, используем внешний путь
                if not img.filepath:
                    print(f"    Пропущена (нет filepath)")
                    continue
                
                old_abs_path = bpy.path.abspath(img.filepath)
                if not os.path.exists(old_abs_path):
                    print(f"    Текстура не найдена: {old_abs_path}")
                    continue
            
            # Формируем новое имя текстуры
            if obj_type == 'Main' and number:
                new_filename = f"T_{address}_{number}_{obj_type}_{tex_type}_1.png"
            elif obj_type == 'Ground':
                new_filename = f"T_{address}_Ground_{tex_type}_1.png"
            elif obj_type.startswith('GroundE') and obj_type != 'Ground':  # GroundEl или GroundElGlass
                new_filename = f"T_{address}_{obj_type}_{tex_type}_1.png"
            elif obj_type == 'Flora':
                new_filename = f"T_{address}_Flora_{tex_type}_1.png"
            else:
                continue
            
            print(f"    Новое имя: {new_filename}")
            
            # Путь к новому файлу
            new_filepath = os.path.join(target_folder, new_filename)
            
            # Сохраняем текстуру
            try:
                # Если это была упакованная текстура и мы распаковали её во временный файл
                if is_packed:
                    # Переименовываем временный файл в финальное имя
                    if os.path.exists(old_abs_path) and old_abs_path != new_filepath:
                        import shutil
                        shutil.move(old_abs_path, new_filepath)
                        print(f"    ✓ Переименовано из временного: {new_filepath}")
                else:
                    # Копируем существующий файл
                    import shutil
                    shutil.copy2(old_abs_path, new_filepath)
                    print(f"    ✓ Скопировано: {new_filepath}")
                
                new_texture_paths[tex_type] = new_filepath
                old_images.append(img)
                renamed_count += 1
                
            except Exception as e:
                print(f"    ✗ Ошибка: {e}")
        
        # Переподключаем текстуры в материалах
        if new_texture_paths:
            self.reconnect_textures(obj, new_texture_paths)
            
            # Удаляем старые изображения из файла
            for old_img in old_images:
                try:
                    bpy.data.images.remove(old_img)
                    print(f"  Удалена старая текстура: {old_img.name}")
                except:
                    pass
        
        print(f"\n=== Обработано текстур: {renamed_count} ===\n")
        return renamed_count
    
    def reconnect_textures(self, obj, new_texture_paths):
        """Переподключает текстуры в материалах с правильными color space"""
        print(f"\n=== Переподключение текстур ===")
        
        for mat_slot in obj.data.materials:
            if not mat_slot or not mat_slot.use_nodes:
                continue
            
            print(f"\nМатериал: {mat_slot.name}")
            nodes = mat_slot.node_tree.nodes
            links = mat_slot.node_tree.links
            
            # Находим Principled BSDF
            principled = None
            for node in nodes:
                if node.type == 'BSDF_PRINCIPLED':
                    principled = node
                    break
            
            if not principled:
                print("  Principled BSDF не найден")
                continue
            
            # Удаляем старые TEX_IMAGE и NORMAL_MAP ноды
            old_tex_nodes = [node for node in nodes if node.type == 'TEX_IMAGE']
            old_normal_nodes = [node for node in nodes if node.type == 'NORMAL_MAP']
            
            for node in old_tex_nodes:
                node_name = node.name  # Сохраняем имя перед удалением
                nodes.remove(node)
                print(f"  Удалена старая текстурная нода: {node_name}")
            
            for node in old_normal_nodes:
                node_name = node.name  # Сохраняем имя перед удалением
                nodes.remove(node)
                print(f"  Удалена старая Normal Map нода: {node_name}")
            
            # Создаем новые ноды для каждой текстуры
            x_offset = -300
            y_offset = 0
            
            for tex_type, tex_path in new_texture_paths.items():
                # Создаем ноду изображения
                tex_node = nodes.new(type='ShaderNodeTexImage')
                tex_node.location = (principled.location.x + x_offset, principled.location.y + y_offset)
                
                # Загружаем изображение
                img = bpy.data.images.load(tex_path)
                tex_node.image = img
                
                # Устанавливаем правильный color space
                color_space = self.get_color_space_for_texture_type(tex_type)
                img.colorspace_settings.name = color_space
                
                print(f"  Подключена текстура {tex_type}: {os.path.basename(tex_path)} (color space: {color_space})")
                
                # Подключаем к Principled BSDF
                if tex_type == 'd':  # Diffuse -> Base Color
                    links.new(tex_node.outputs['Color'], principled.inputs['Base Color'])
                elif tex_type == 'o':  # Opacity -> Alpha
                    links.new(tex_node.outputs['Color'], principled.inputs['Alpha'])
                elif tex_type == 'm':  # Metallic
                    links.new(tex_node.outputs['Color'], principled.inputs['Metallic'])
                elif tex_type == 'n':  # Normal
                    # Создаем Normal Map ноду
                    normal_map = nodes.new(type='ShaderNodeNormalMap')
                    normal_map.location = (tex_node.location.x + 200, tex_node.location.y)
                    links.new(tex_node.outputs['Color'], normal_map.inputs['Color'])
                    links.new(normal_map.outputs['Normal'], principled.inputs['Normal'])
                elif tex_type == 'r':  # Roughness
                    links.new(tex_node.outputs['Color'], principled.inputs['Roughness'])
                
                y_offset -= 300
        
        print(f"\n=== Переподключение завершено ===")
    
    def rename_textures(self, folder_path, new_address, number, obj_type):
        """Переименовывает текстуры в папке, находя их по маске без учета адреса"""
        renamed_count = 0
        
        print(f"\n=== Начало переименования текстур ===")
        print(f"Папка: {folder_path}")
        print(f"Новый адрес: {new_address}")
        print(f"Номер: {number}")
        print(f"Тип объекта: {obj_type}")
        
        files = os.listdir(folder_path)
        print(f"Найдено файлов в папке: {len(files)}")
        
        for filename in files:
            print(f"\nПроверка файла: {filename}")
            
            if not filename.endswith('.png'):
                print(f"  Пропущен: не .png файл")
                continue
            
            # Проверяем UDIM паттерн (.1001, .1002 и т.д.)
            udim_match = re.search(r'\.(\d{4})\.png$', filename)
            if not udim_match:
                print(f"  Пропущен: не найден UDIM паттерн")
                continue
            
            udim_number = udim_match.group(1)
            print(f"  UDIM номер: {udim_number}")
            
            # Определяем тип текстуры
            texture_type = self.get_texture_type(filename)
            if not texture_type:
                print(f"  Пропущен: не определен тип текстуры")
                continue
            
            print(f"  Тип текстуры: {texture_type}")
            
            # Проверяем соответствие маске и извлекаем номер материала
            should_rename = False
            material_num = None
            
            if obj_type == 'Main' and number:
                # Паттерн для Main: T_*_001_Diffuse_1.1001.png (любой адрес, конкретный номер, номер материала)
                pattern = r'^T_.+?_' + re.escape(number) + r'_' + re.escape(texture_type) + r'_(\d+)\.\d{4}\.png$'
                print(f"  Проверка паттерна Main: {pattern}")
                match = re.match(pattern, filename)
                if match:
                    should_rename = True
                    material_num = match.group(1)
                    print(f"  Совпадение найдено! Номер материала: {material_num}")
            
            elif obj_type == 'Ground':
                # Паттерн для Ground: T_*_Ground_Diffuse_1.1001.png (любой адрес, номер материала)
                pattern = r'^T_.+?_Ground_' + re.escape(texture_type) + r'_(\d+)\.\d{4}\.png$'
                print(f"  Проверка паттерна Ground: {pattern}")
                match = re.match(pattern, filename)
                if match:
                    should_rename = True
                    material_num = match.group(1)
                    print(f"  Совпадение найдено! Номер материала: {material_num}")
            
            elif obj_type.startswith('GroundE') and obj_type != 'Ground':  # GroundEl или GroundElGlass
                # Паттерн для GroundEl: T_*_GroundEl_Diffuse_1.1001.png (любой адрес, номер материала)
                pattern = r'^T_.+?_GroundEl(?:Glass)?_' + re.escape(texture_type) + r'_(\d+)\.\d{4}\.png$'
                print(f"  Проверка паттерна GroundEl: {pattern}")
                match = re.match(pattern, filename)
                if match:
                    should_rename = True
                    material_num = match.group(1)
                    print(f"  Совпадение найдено! Номер материала: {material_num}")
            
            elif obj_type == 'Flora':
                # Паттерн для Flora: T_*_Flora_Diffuse_1.1001.png (любой адрес, номер материала)
                pattern = r'^T_.+?_Flora_' + re.escape(texture_type) + r'_(\d+)\.\d{4}\.png$'
                print(f"  Проверка паттерна Flora: {pattern}")
                match = re.match(pattern, filename)
                if match:
                    should_rename = True
                    material_num = match.group(1)
                    print(f"  Совпадение найдено! Номер материала: {material_num}")
            
            if not should_rename:
                print(f"  Пропущен: не соответствует маске")
                continue
            
            # Формируем новое имя с сохранением номера материала
            if obj_type == 'Main' and number:
                new_filename = f"T_{new_address}_{number}_{texture_type}_{material_num}.{udim_number}.png"
            elif obj_type == 'Ground':
                new_filename = f"T_{new_address}_Ground_{texture_type}_{material_num}.{udim_number}.png"
            elif obj_type.startswith('GroundE') and obj_type != 'Ground':  # GroundEl или GroundElGlass
                new_filename = f"T_{new_address}_{obj_type}_{texture_type}_{material_num}.{udim_number}.png"
            elif obj_type == 'Flora':
                new_filename = f"T_{new_address}_Flora_{texture_type}_{material_num}.{udim_number}.png"
            else:
                continue
            
            print(f"  Новое имя: {new_filename}")
            
            old_path = os.path.join(folder_path, filename)
            new_path = os.path.join(folder_path, new_filename)
            
            try:
                if old_path != new_path:
                    os.rename(old_path, new_path)
                    renamed_count += 1
                    print(f"  ✓ УСПЕШНО переименовано!")
                else:
                    print(f"  Имя не изменилось, пропущено")
            except Exception as e:
                print(f"  ✗ ОШИБКА переименования: {e}")
        
        print(f"\n=== Итого переименовано: {renamed_count} файлов ===\n")
        return renamed_count
    
    def get_texture_type(self, filename):
        """Определяет тип текстуры по имени файла (для UDIM текстур)"""
        if 'Diffuse' in filename:
            return 'Diffuse'
        elif 'Normal' in filename:
            return 'Normal'
        elif 'ERM' in filename or 'ORM' in filename:
            return 'ERM'
        return None
    
    def get_new_folder_name(self, address, number, obj_type):
        """Формирует новое имя папки"""
        if obj_type == 'Main' and number:
            # Для Main папка БЕЗ суффикса "_Main"
            return f"SM_{address}_{number}"
        elif obj_type == 'Ground':
            # Для Ground папка С суффиксом "_Ground"
            return f"SM_{address}_Ground"
        elif obj_type.startswith('GroundE') and obj_type != 'Ground':  # GroundEl или GroundElGlass
            # Для GroundEl папка с оригинальным написанием
            return f"SM_{address}_{obj_type}"
        elif obj_type == 'Flora':
            # Для Flora папка С суффиксом "_Flora"
            return f"SM_{address}_Flora"
        return None
    
    def update_material_paths(self, obj, old_folder, new_folder):
        """Находит ноды по подключениям и заменяет текстуры на новые"""
        print(f"\n=== Переподключение текстур в материалах ===")
        print(f"Новая папка: {new_folder}")
        
        # Получаем список всех новых текстур в папке
        new_textures = {}
        for filename in os.listdir(new_folder):
            if filename.endswith('.1001.png'):
                if 'Diffuse' in filename:
                    new_textures['Diffuse'] = filename
                elif 'Normal' in filename:
                    new_textures['Normal'] = filename
                elif 'ERM' in filename or 'ORM' in filename:
                    new_textures['ERM'] = filename
        
        print(f"Найдены новые текстуры: {new_textures}")
        
        for mat_slot in obj.data.materials:
            if not mat_slot or not mat_slot.use_nodes:
                continue
            
            print(f"\nМатериал: {mat_slot.name}")
            nodes = mat_slot.node_tree.nodes
            links = mat_slot.node_tree.links
            
            # Ищем BSDF ноду
            bsdf = None
            for node in nodes:
                if node.type == 'BSDF_PRINCIPLED':
                    bsdf = node
                    break
            
            if not bsdf:
                print("  Не найдена Principled BSDF нода")
                continue
            
            # 1. Diffuse - подключена к Base Color
            if bsdf.inputs['Base Color'].is_linked:
                diffuse_link = bsdf.inputs['Base Color'].links[0]
                diffuse_node = diffuse_link.from_node
                
                if diffuse_node.type == 'TEX_IMAGE' and 'Diffuse' in new_textures:
                    print(f"  Diffuse нода найдена: {diffuse_node.name}")
                    new_path = self.load_new_texture(new_folder, new_textures['Diffuse'], diffuse_node, 'sRGB')
                    if new_path:
                        print(f"    ✓ Заменена на: {new_path}")
            
            # 2. ERM - ищем через Separate RGB/XYZ ноду
            for node in nodes:
                if node.type in ['SEPRGB', 'SEPARATE_COLOR', 'SEPARATE_XYZ']:
                    # Проверяем что эта нода подключена к BSDF
                    is_connected_to_bsdf = False
                    for output in node.outputs:
                        for link in output.links:
                            if link.to_node == bsdf:
                                is_connected_to_bsdf = True
                                break
                    
                    if is_connected_to_bsdf:
                        # Ищем текстуру подключенную к Separate
                        if node.inputs[0].is_linked:
                            erm_link = node.inputs[0].links[0]
                            erm_node = erm_link.from_node
                            
                            if erm_node.type == 'TEX_IMAGE' and 'ERM' in new_textures:
                                print(f"  ERM нода найдена: {erm_node.name}")
                                new_path = self.load_new_texture(new_folder, new_textures['ERM'], erm_node, 'Non-Color')
                                if new_path:
                                    print(f"    ✓ Заменена на: {new_path} (Non-Color)")
                        break
            
            # 3. Normal - через Normal Map ноду
            for node in nodes:
                if node.type == 'NORMAL_MAP':
                    # Проверяем что Normal Map подключена к BSDF
                    if node.outputs['Normal'].is_linked:
                        for link in node.outputs['Normal'].links:
                            if link.to_node == bsdf:
                                # Ищем текстуру подключенную к Normal Map
                                if node.inputs['Color'].is_linked:
                                    normal_link = node.inputs['Color'].links[0]
                                    normal_node = normal_link.from_node
                                    
                                    if normal_node.type == 'TEX_IMAGE' and 'Normal' in new_textures:
                                        print(f"  Normal нода найдена: {normal_node.name}")
                                        new_path = self.load_new_texture(new_folder, new_textures['Normal'], normal_node, 'Non-Color')
                                        if new_path:
                                            print(f"    ✓ Заменена на: {new_path} (Non-Color)")
                                break
        
        print(f"=== Переподключение завершено ===\n")
    
    def load_new_texture(self, folder, filename, node, color_space='sRGB'):
        """Загружает новую UDIM текстуру в ноду"""
        try:
            # Формируем UDIM путь
            base_name = filename.replace('.1001.png', '')
            udim_path = os.path.join(folder, f"{base_name}.<UDIM>.png")
            
            # Загружаем новое изображение
            abs_path = os.path.abspath(udim_path)
            
            # Создаем новое изображение как UDIM
            new_image = bpy.data.images.load(abs_path, check_existing=False)
            new_image.source = 'TILED'
            
            # Устанавливаем color space
            new_image.colorspace_settings.name = color_space
            
            # Заменяем изображение в ноде
            node.image = new_image
            
            return bpy.path.relpath(abs_path)
        except Exception as e:
            print(f"    ✗ Ошибка загрузки: {e}")
            return None

class BAKER_OT_revert_udim_uvs(Operator):
    """Перенос UV обратно в 0–1 и восстановление исходных материалов (из UDIM)"""
    bl_idname = "baker.revert_udim_uvs"
    bl_label = "Откат UDIM UV (разбор)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            return False

        # Кнопка всегда активна для MESH объектов, независимо от наличия UDIM текстур
        return True

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "Выберите MESH объект")
            return {'CANCELLED'}
        if not obj.data.uv_layers:
            self.report({'ERROR'}, "У объекта нет UV")
            return {'CANCELLED'}

        # Проверяем наличие UDIM текстур в материалах
        has_udim_textures = False
        for slot in obj.data.materials:
            if slot and slot.use_nodes:
                for node in slot.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image and node.image.source == 'TILED':
                        has_udim_textures = True
                        break
                if has_udim_textures:
                    break

        if not has_udim_textures:
            self.report({'ERROR'}, "UDIM текстуры не найдены. Операция отменена.")
            return {'CANCELLED'}

        # Пытаемся восстановить материалы по сохраненным индексам
        mats_sequence = []
        restored_materials = False
        tile_to_mat_index = {}
        sorted_udims = []

        # Ищем сохраненную информацию об индексах материалов
        scene = context.scene
        if hasattr(scene, 'baker_object_material_indices'):
            for obj_indices in scene.baker_object_material_indices:
                if obj_indices.object_name == obj.name:
                    print(f"   🔍 Найдена сохраненная информация об индексах материалов для объекта {obj.name}")
                    print(f"   📋 Сохраненные индексы: {obj_indices.material_indices}")

                    # Парсим строку индексов
                    saved_indices = {}
                    for pair in obj_indices.material_indices.split(','):
                        if ':' in pair:
                            idx_str, mat_name = pair.split(':', 1)
                            try:
                                saved_indices[int(idx_str)] = mat_name
                            except ValueError:
                                continue

                    # Восстанавливаем материалы по индексам
                    obj.data.materials.clear()
                    max_index = max(saved_indices.keys()) if saved_indices else 0

                    for i in range(max_index + 1):
                        if i in saved_indices:
                            mat_name = saved_indices[i]
                            material = bpy.data.materials.get(mat_name)
                            if material:
                                obj.data.materials.append(material)
                                mats_sequence.append(material)
                                print(f"   ✅ Восстановлен материал '{mat_name}' на индекс {i}")
                            else:
                                print(f"   ⚠️ Материал '{mat_name}' не найден, будет создан новый")
                                # Создаем пустой слот
                                obj.data.materials.append(None)
                        else:
                            obj.data.materials.append(None)

                    if mats_sequence:
                        restored_materials = True
                        # Создаем маппинг для совместимости с остальным кодом
                        for i, mat in enumerate(mats_sequence):
                            if mat:
                                # Предполагаем, что индекс соответствует UDIM номеру минус 1000
                                udim_number = 1001 + i
                                tile_to_mat_index[udim_number] = i
                        sorted_udims = sorted(tile_to_mat_index.keys())
                        print(f"   🎉 Успешно восстановлено {len(mats_sequence)} материалов по индексам")
                    break

        # Если не удалось восстановить материалы, создаем новые
        if not restored_materials:
            print("   🔄 Не удалось восстановить материалы по индексам, создаем новые")

            # Определяем путь к UDIM папке
            udim_dir = None
            try:
                address, obj_type = process_object_name(obj.name)
                blend_path = bpy.data.filepath
                if blend_path:
                    from pathlib import Path
                    base_dir = Path(blend_path).parent
                    udim_dir_name = get_udim_directory_name(address, obj_type)
                    udim_dir = base_dir / udim_dir_name
                    print(f"   📁 Путь к UDIM папке: {udim_dir}")
            except Exception as e:
                print(f"   ⚠️ Не удалось определить путь к UDIM папке: {e}")

            tile_to_mat_index = self.create_materials_per_udim_tile(obj, udim_dir)
            # Собираем последовательность материалов по возрастанию UDIM
            sorted_udims = sorted(tile_to_mat_index.keys())
            mats_sequence = []
            for udim_number in sorted_udims:
                idx = tile_to_mat_index[udim_number]
                if 0 <= idx < len(obj.material_slots):
                    mats_sequence.append(obj.material_slots[idx].material)

        import bmesh
        # Переход в Edit Mode
        if bpy.context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode='EDIT')

        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        uv_layer = bm.loops.layers.uv.active

        # Убедимся, что материалы присутствуют в объекте в нужном порядке
        if mats_sequence:
            obj.data.materials.clear()
            for mat in mats_sequence:
                obj.data.materials.append(mat)
            # Перестроим маппинг UDIM → новый индекс слота
            udim_to_slot = {udim: idx for idx, udim in enumerate(sorted_udims)} if sorted_udims else tile_to_mat_index

        for face in bm.faces:
            if len(face.loops) == 0:
                continue
            # Определяем UDIM-тайл по большинству UV-петель (надежнее центра)
            tile_counts = {}
            loop_tiles = []
            for loop in face.loops:
                uv = loop[uv_layer].uv
                u_off = math.floor(uv.x)
                v_off = math.floor(uv.y)
                tile = 1001 + u_off + v_off * 10
                loop_tiles.append((loop, u_off, v_off, tile))
                tile_counts[tile] = tile_counts.get(tile, 0) + 1
            # Выбираем тайл с максимумом голосов
            udim_tile = max(tile_counts.items(), key=lambda kv: kv[1])[0]
            # Назначаем материал по тайлу
            if mats_sequence:
                idx = None
                if 'udim_to_slot' in locals() and udim_tile in udim_to_slot:
                    idx = udim_to_slot[udim_tile]
                elif udim_tile in tile_to_mat_index:
                    idx = tile_to_mat_index[udim_tile]
                if idx is not None and 0 <= idx < len(obj.data.materials):
                    face.material_index = idx
            # Откатываем UV в 0–1 для каждой петли относительно её собственного тайла
            for loop, u_off, v_off, _tile in loop_tiles:
                uv = loop[uv_layer].uv
                uv.x -= u_off
                uv.y -= v_off

        bmesh.update_edit_mesh(obj.data)
        bpy.ops.object.mode_set(mode='OBJECT')

        self.report({'INFO'}, "UV возвращены в 0–1 и материалы распределены по UDIM тайлам")
        return {'FINISHED'}

    def create_materials_per_udim_tile(self, obj, udim_dir=None):
        """Собирает UDIM тайлы из материалов объекта и создает материалы M_UDIM для каждого тайла.
        Возвращает словарь {udim_number: material_slot_index}."""
        # Найдем любой материал с UDIM текстурами и соберем tile->file для типов Diffuse/ERM/Normal
        udim_tiles = {}

        def detect_kind_for_tex_node(node):
            # Пытаемся определить тип текстуры по подключению
            kinds = set()
            for link in node.outputs.get('Color', []).links:
                to_node = link.to_node
                if to_node.type == 'NORMAL_MAP':
                    kinds.add('Normal')
                elif to_node.type == 'BSDF_PRINCIPLED':
                    if link.to_socket.name == 'Base Color':
                        kinds.add('Diffuse')
                    elif link.to_socket.name in {'Roughness', 'Metallic'}:
                        kinds.add('ERM')
            # Приоритет: Normal, Diffuse, ERM
            if 'Normal' in kinds:
                return 'Normal'
            if 'Diffuse' in kinds:
                return 'Diffuse'
            if 'ERM' in kinds:
                return 'ERM'
            # Хэвристика по имени файла
            img = getattr(node, 'image', None)
            fp = getattr(img, 'filepath', '') if img else ''
            if 'normal' in fp.lower():
                return 'Normal'
            if 'diffuse' in fp.lower() or 'albedo' in fp.lower() or 'basecolor' in fp.lower():
                return 'Diffuse'
            if any(tag in fp.lower() for tag in ['erm', 'orm', 'roughness', 'metallic']):
                return 'ERM'
            return None

        def filepath_for_tile(image, tile_number):
            # Предпочтительно берем напрямую из ImageTile
            try:
                for tile in image.tiles:
                    num = getattr(tile, 'number', getattr(tile, 'tile_number', None))
                    if num == tile_number:
                        fp = getattr(tile, 'filepath', None)
                        if fp:
                            return bpy.path.abspath(fp)
            except Exception:
                pass
            # Пытаемся заменить 4-значный UDIM в пути
            base = bpy.path.abspath(image.filepath)
            if not base:
                return None
            m = re.search(r"\.(\d{4})\.(png|jpg|jpeg|tga|tiff|exr)(?!.*\d)", base.lower())
            if m:
                return re.sub(r"(\d{4})(?!.*\d)", f"{tile_number:04d}", base)
            # Если в пути есть <UDIM>
            if '<UDIM>' in base:
                return base.replace('<UDIM>', f"{tile_number:04d}")
            return None

        # Скан материалов и узлов
        for slot in obj.material_slots:
            mat = slot.material
            if not mat or not mat.use_nodes:
                continue
            for node in mat.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and getattr(node, 'image', None):
                    img = node.image
                    if getattr(img, 'source', '') != 'TILED':
                        continue
                    kind = detect_kind_for_tex_node(node)
                    if kind is None:
                        continue
                    # Собираем по всем тайлам
                    try:
                        for tile in img.tiles:
                            tile_num = getattr(tile, 'number', getattr(tile, 'tile_number', None))
                            if tile_num is None:
                                continue
                            path = filepath_for_tile(img, tile_num)
                            if not path:
                                continue
                            info = udim_tiles.setdefault(tile_num, {})
                            info[kind] = path
                    except Exception:
                        continue

        # Если не нашли UDIM текстур в материалах, ищем UDIM файлы в папке output_path
        if not udim_tiles:
            print(f"   🔍 UDIM текстуры не найдены в материалах, ищем файлы в папке: {str(udim_dir) if udim_dir else 'не указана'}")
            if udim_dir and os.path.exists(udim_dir):
                import re
                for filename in os.listdir(udim_dir):
                    if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.tga', '.tiff', '.exr')):
                        # Ищем UDIM номер в имени файла (4 цифры перед расширением файла)
                        match = re.search(r'\.(\d{4})\.(png|jpg|jpeg|tga|tiff|exr)$', filename.lower())
                        if match:
                            tile_num = int(match.group(1))
                            if 1001 <= tile_num <= 1999:  # Валидный UDIM диапазон
                                filepath = os.path.join(udim_dir, filename)
                                # Определяем тип текстуры по имени файла
                                kind = None
                                fname_lower = filename.lower()
                                if 'normal' in fname_lower:
                                    kind = 'Normal'
                                elif 'diffuse' in fname_lower or 'albedo' in fname_lower or 'basecolor' in fname_lower:
                                    kind = 'Diffuse'
                                elif any(tag in fname_lower for tag in ['erm', 'orm', 'roughness', 'metallic']):
                                    kind = 'ERM'

                                if kind:
                                    info = udim_tiles.setdefault(tile_num, {})
                                    info[kind] = filepath
                                    print(f"      ✅ Найдена {kind} текстура для UDIM {tile_num}: {filename}")

        # Получаем следующий номер последовательности для материалов
        sequence_num = get_next_material_sequence_number()
        print(f"🔢 Используем номер последовательности: {sequence_num}")
        
        # Создаем материалы M_{sequence}_{UDIM} для каждого тайла
        tile_to_slot = {}
        for tile_num in sorted(udim_tiles.keys()):
            mat_name = f"M_{sequence_num}_{tile_num}"
            mat = bpy.data.materials.get(mat_name) or bpy.data.materials.new(mat_name)
            print(f"📦 Создан материал: {mat_name}")
            mat.use_nodes = True
            nodes = mat.node_tree.nodes
            links = mat.node_tree.links
            nodes.clear()
            out = nodes.new('ShaderNodeOutputMaterial')
            bsdf = nodes.new('ShaderNodeBsdfPrincipled')
            out.location = (500, 0)
            bsdf.location = (200, 0)
            links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])

            info = udim_tiles[tile_num]

            # Diffuse
            if 'Diffuse' in info:
                try:
                    img = bpy.data.images.load(info['Diffuse'])
                    img.colorspace_settings.name = 'sRGB'
                    tn = nodes.new('ShaderNodeTexImage')
                    tn.image = img
                    tn.location = (-200, 100)
                    links.new(tn.outputs['Color'], bsdf.inputs['Base Color'])
                    # Подключаем Alpha канал к Base Color Alpha
                    links.new(tn.outputs['Alpha'], bsdf.inputs['Alpha'])

                    # Переносим альбедо color в emission color
                    links.new(tn.outputs['Color'], bsdf.inputs['Emission Color'])
                except Exception:
                    pass

            # Normal
            if 'Normal' in info:
                try:
                    img = bpy.data.images.load(info['Normal'])
                    img.colorspace_settings.name = 'Non-Color'
                    tn = nodes.new('ShaderNodeTexImage')
                    tn.image = img
                    tn.location = (-200, -100)
                    nmap = nodes.new('ShaderNodeNormalMap')
                    nmap.location = (0, -100)
                    links.new(tn.outputs['Color'], nmap.inputs['Color'])
                    links.new(nmap.outputs['Normal'], bsdf.inputs['Normal'])
                except Exception:
                    pass

            # ERM
            if 'ERM' in info:
                try:
                    img = bpy.data.images.load(info['ERM'])
                    img.colorspace_settings.name = 'Non-Color'
                    tn = nodes.new('ShaderNodeTexImage')
                    tn.image = img
                    tn.location = (-400, -300)
                    sep = nodes.new('ShaderNodeSeparateColor')
                    sep.location = (-200, -300)
                    links.new(tn.outputs['Color'], sep.inputs['Color'])
                    # G -> Roughness, B -> Metallic, R -> Emission Strength
                    links.new(sep.outputs['Green'], bsdf.inputs['Roughness'])
                    links.new(sep.outputs['Blue'], bsdf.inputs['Metallic'])
                    links.new(sep.outputs['Red'], bsdf.inputs['Emission Strength'])

                except Exception:
                    pass

            # Добавляем материал в объект, запоминаем индекс
            obj.data.materials.append(mat)
            tile_to_slot[tile_num] = len(obj.material_slots) - 1

        return tile_to_slot

class BAKER_PT_panel(Panel):
    """Creates a Panel in the Object properties window"""
    bl_label = "AGR_baker"
    bl_idname = "BAKER_PT_main_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "AGR_baker"
    
    def draw(self, context):
        layout = self.layout
        texture_settings_box = layout.box()
        
        texture_settings_box.label(text="Разрешение текстур:")
        row = texture_settings_box.row()
        row.prop(context.scene, "baker_resolution", text="")
        
        texture_settings_box.separator()
        row = texture_settings_box.row()
        row.prop(context.scene, "baker_bake_normal_enabled", text="Запечь NORMAL")
        
        row = texture_settings_box.row()
        row.prop(context.scene, "baker_bake_with_alpha", text="Запечь с альфа-каналом")
        
        row = texture_settings_box.row()
        row.prop(context.scene, "baker_simple_baking", text="Простое запекание")
        

        layout.separator()
        bake_settings_box = layout.box()
        bake_settings_box.label(text="Настройки запекания:")

        row = bake_settings_box.row()
        row.prop(context.scene, "baker_max_ray_distance", text="Max Ray Distance")

        row = bake_settings_box.row()
        row.prop(context.scene, "baker_extrusion", text="Extrusion")

        layout.separator()

        row = layout.row()
        row.scale_y = 2.0
        op = row.operator("baker.bake_textures", text="Запечь все текстуры")
        op.resolution = context.scene.baker_resolution
        op.connection_mode = context.scene.baker_connection_mode
        op.normal_type = context.scene.baker_normal_type
        op.max_ray_distance = context.scene.baker_max_ray_distance
        op.extrusion = context.scene.baker_extrusion
        op.simple_baking = context.scene.baker_simple_baking
        op.bake_with_alpha = context.scene.baker_bake_with_alpha
        
        layout.separator()
        
        # Меню наборов текстур
        ensure_scene_properties()
        
        # Сворачиваемый заголовок с кнопками управления
        box = layout.box()
        header_row = box.row()
        header_row.prop(context.scene, "baker_main_texture_sets_collapsed", 
                       text="Доступные наборы текстур:", 
                       icon='TRIA_DOWN' if context.scene.baker_main_texture_sets_collapsed else 'TRIA_RIGHT')
        header_row.operator("baker.refresh_texture_sets", text="", icon='FILE_REFRESH')

        if context.scene.baker_main_texture_sets_collapsed:
            # Кнопки управления (скрыты в сворачиваемом меню)
            buttons_row = box.row(align=True)
            buttons_row.scale_y = 1.1
            buttons_row.operator("baker.select_texture_sets_for_selected_objects", text="Выбрать для объектов", icon='OBJECT_DATA')
            buttons_row.operator("baker.invert_texture_sets_selection", text="Инвертировать", icon='ARROW_LEFTRIGHT')
            buttons_row.operator("baker.toggle_select_all_texture_sets", text="Выбрать/снять все")

            # Кнопка удаления наборов
            delete_row = box.row()
            delete_row.scale_y = 1.0
            selected_count_for_atlas = sum(1 for tex_set in context.scene.baker_texture_sets if tex_set.is_selected_for_atlas)
            if selected_count_for_atlas > 0:
                delete_row.operator("baker.delete_selected_texture_sets",
                                  text=f"Удалить наборы ({selected_count_for_atlas})",
                                  icon='TRASH')
            else:
                delete_row.label(text="Удалить наборы", icon='TRASH')

            # Отображение наборов текстур (тоже в сворачиваемом меню)
            if len(context.scene.baker_texture_sets) == 0:
                box.label(text="Нет запеченных наборов", icon='INFO')
                box.label(text="Сначала запеките текстуры")
            else:
                # Сортируем наборы: сначала выбранные, потом остальные
                sorted_texture_sets = sorted(enumerate(context.scene.baker_texture_sets), 
                                           key=lambda x: (not x[1].is_selected_for_atlas, x[0]))
                
                for original_index, tex_set in sorted_texture_sets:
                    row = box.row()
                    row.prop(tex_set, "is_selected_for_atlas", text="")

                    col = row.column()
                    col.label(text=f"{tex_set.name} ({tex_set.resolution}px)")
                    sub = col.row()
                    sub.scale_y = 0.7

                    available_textures = []
                    if tex_set.has_diffuse: available_textures.append("D")
                    if tex_set.has_diffuse_opacity: available_textures.append("DO")
                    if tex_set.has_normal: available_textures.append("N")
                    if tex_set.has_normal_directx: available_textures.append("NDX")
                    if tex_set.has_roughness: available_textures.append("R")
                    if tex_set.has_metallic: available_textures.append("M")
                    if tex_set.has_emit: available_textures.append("E")
                    if tex_set.has_opacity: available_textures.append("O")
                    if tex_set.has_erm: available_textures.append("ERM")

                    sub.label(text=f"Файлы: {', '.join(available_textures)}")

class BAKER_PT_material_generation_panel(Panel):
    """Панель для генерации текстур из материалов"""
    bl_label = "Генерация из материалов"
    bl_idname = "BAKER_PT_material_generation_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "AGR_baker"
    bl_parent_id = "BAKER_PT_main_panel"
    bl_options = {'DEFAULT_CLOSED'}
    
    def draw(self, context):
        layout = self.layout
        scene = context.scene
        box = layout.box()
        col = box.column(align=True)
        col.prop(scene, "baker_generate_all_materials", text="Генерировать для всех материалов объектов")
        col.prop(scene, "baker_overwrite_existing", text="Перезаписать существующие текстуры")
        col.prop(scene, "resize_textures_256", text="Переразмерять текстуры 256px")
        
        row = box.row()
        row.scale_y = 1.5
        
        obj = context.active_object
        can_generate = False
        materials_with_nodes = []
        if obj and obj.type == 'MESH':
            if scene.baker_generate_all_materials:
                for slot in obj.material_slots:
                    if slot.material and slot.material.use_nodes:
                        materials_with_nodes.append(slot.material)
                can_generate = len(materials_with_nodes) > 0
            else:
                if obj.active_material and obj.active_material.use_nodes:
                    materials_with_nodes.append(obj.active_material)
                    can_generate = True
        
        if can_generate and materials_with_nodes:
            blend_file_path = bpy.path.abspath("//")
            if blend_file_path:
                main_baked_folder = os.path.join(blend_file_path, "OBJECT_BAKED")
                
                existing_count = 0
                existing_generated = 0
                existing_baked = 0
                
                for material in materials_with_nodes:
                    generated_path = os.path.join(main_baked_folder, f"{material.name}_generated")
                    has_generated = os.path.exists(generated_path)
                    
                    has_baked = any(tex_set.material_name == material.name and "_baked" in tex_set.output_path 
                                  for tex_set in scene.baker_texture_sets)
                    
                    if has_generated or has_baked:
                        existing_count += 1
                        if has_generated:
                            existing_generated += 1
                        if has_baked:
                            existing_baked += 1
                
                if existing_count > 0:
                    status_box = layout.box()
                    status_col = status_box.column(align=True)
                    status_col.label(text=f"📊 Статус материалов ({len(materials_with_nodes)} всего):")
                    
                    if existing_generated > 0:
                        status_col.label(text=f"   🎨 Сгенерированные: {existing_generated}")
                    if existing_baked > 0:
                        status_col.label(text=f"   🔥 Запеченные: {existing_baked}")
                    
                    new_count = len(materials_with_nodes) - existing_count
                    if new_count > 0:
                        status_col.label(text=f"   ✨ Новые: {new_count}")
                    
        
        row.enabled = can_generate
        
        if scene.baker_generate_all_materials:
            row = layout.row()
            row.scale_y = 2
            button_text = "Сгенерировать для всех материалов"
        else:
            row = layout.row()
            row.scale_y = 2
            button_text = "Сгенерировать из активного материала"
            
        op = row.operator("baker.generate_from_material", text=button_text)
        op.resolution = scene.baker_resolution
        op.connection_mode = scene.baker_connection_mode
        op.normal_type = scene.baker_normal_type
        op.generate_all_materials = scene.baker_generate_all_materials
        op.overwrite_existing = scene.baker_overwrite_existing
        op.resize_textures_256 = scene.resize_textures_256
        
        if not can_generate:
            if not obj:
                box.label(text="Нет активного объекта", icon='ERROR')
            elif obj.type != 'MESH':
                box.label(text="Объект должен быть типа MESH", icon='ERROR')
            elif scene.baker_generate_all_materials:
                if not any(slot.material for slot in obj.material_slots):
                    box.label(text="У объекта нет материалов", icon='ERROR')
                else:
                    box.label(text="Ни один материал не использует ноды", icon='ERROR')
            else:
                if not obj.active_material:
                    box.label(text="У объекта нет активного материала", icon='ERROR')
                elif not obj.active_material.use_nodes:
                    box.label(text="Материал должен использовать ноды", icon='ERROR')

class BAKER_PT_atlas_panel(Panel):
    """Панель для создания атласов из запеченных текстур"""
    bl_label = "Создание атласов"
    bl_idname = "BAKER_PT_atlas_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "AGR_baker"
    bl_options = {'DEFAULT_CLOSED'}
    
    def draw(self, context):
        layout = self.layout
        scene = context.scene
        ensure_scene_properties()
        
        box = layout.box()
        buttons_row = box.row(align=True)
        buttons_row.scale_y = 1.2
        buttons_row.operator("baker.select_texture_sets_for_selected_objects", text="Выбрать для объектов", icon='OBJECT_DATA')
        buttons_row.operator("baker.invert_texture_sets_selection", text="Инвертировать", icon='ARROW_LEFTRIGHT')
        buttons_row.operator("baker.toggle_select_all_texture_sets", text="Выбрать/снять все")

        # Кнопка удаления наборов
        delete_row = box.row()
        delete_row.scale_y = 1.1
        selected_count = sum(1 for tex_set in scene.baker_texture_sets if tex_set.is_selected_for_atlas)
        if selected_count > 0:
            delete_row.operator("baker.delete_selected_texture_sets",
                              text=f"Удалить наборы ({selected_count})",
                              icon='TRASH')
        else:
            delete_row.label(text="Удалить наборы", icon='TRASH')

        # Сворачиваемый заголовок с кнопками управления
        header_row = box.row()
        header_row.prop(scene, "baker_texture_sets_collapsed", text="Доступные наборы текстур:", icon='TRIA_DOWN' if scene.baker_texture_sets_collapsed else 'TRIA_RIGHT')
        header_row.operator("baker.refresh_texture_sets", text="", icon='FILE_REFRESH')


        if len(scene.baker_texture_sets) == 0:
            box.label(text="Нет запеченных наборов", icon='INFO')
            box.label(text="Сначала запеките текстуры")
        elif scene.baker_texture_sets_collapsed:
            # Сортируем наборы: сначала выбранные, потом остальные
            sorted_texture_sets = sorted(enumerate(scene.baker_texture_sets), 
                                       key=lambda x: (not x[1].is_selected_for_atlas, x[0]))
            
            for original_index, tex_set in sorted_texture_sets:
                row = box.row()
                row.prop(tex_set, "is_selected_for_atlas", text="")

                col = row.column()
                col.label(text=f"{tex_set.name} ({tex_set.resolution}px)")
                sub = col.row()
                sub.scale_y = 0.7

                available_textures = []
                if tex_set.has_diffuse: available_textures.append("D")
                if tex_set.has_diffuse_opacity: available_textures.append("DO")
                if tex_set.has_normal: available_textures.append("N")
                if tex_set.has_normal_directx: available_textures.append("NDX")
                if tex_set.has_roughness: available_textures.append("R")
                if tex_set.has_metallic: available_textures.append("M")
                if tex_set.has_emit: available_textures.append("E")
                if tex_set.has_opacity: available_textures.append("O")
                if tex_set.has_erm: available_textures.append("ERM")

                sub.label(text=f"Файлы: {', '.join(available_textures)}")
        
        layout.separator()
        
        box = layout.box()
        box.label(text="Настройки атласа:")
        row = box.row()
        row.prop(scene, "baker_atlas_type", text="Тип")
        
        row = box.row()
        row.prop(scene, "baker_atlas_size", text="Размер")
        
        box = layout.box()
        col = box.column(align=True)      
        atlas_size = int(scene.baker_atlas_size)
        col.label(text=f"Атлас {atlas_size}x{atlas_size}:")
        
        if atlas_size == 512:
            col.label(text="• 4 текстуры 256x256")
        elif atlas_size == 1024:
            col.label(text="• 1 текстура 512x512 + 12 текстур 256x256")
            col.label(text="• или 4 текстуры 512x512")
            col.label(text="• или 16 текстур 256x256")
        elif atlas_size == 2048:
            col.label(text="• 1 текстура 1024x1024 + комбинации меньших")
            col.label(text="• или 4 текстуры 1024x1024")
            col.label(text="• или 16 текстур 512x512")
        elif atlas_size == 4096:
            col.label(text="• 1 текстура 2048x2048 + комбинации меньших")
            col.label(text="• или 4 текстуры 2048x2048")
            col.label(text="• или 16 текстур 1024x1024")
        
        layout.separator()
        
        selected_count = sum(1 for tex_set in scene.baker_texture_sets if tex_set.is_selected_for_atlas)
        
        if selected_count == 0:
            layout.label(text="Выберите наборы для атласа", icon='INFO')
        else:
            row = layout.row()
            row.scale_y = 2
            op = row.operator("baker.create_atlas", text=f"Создать атлас из {selected_count} наборов")

        # Дополнительные инструменты атласа
        layout.separator()
        tools_box = layout.box()
        tools_box.label(text="Инструменты атласа:", icon='IMAGE_DATA')
        row = tools_box.row()
        row.operator("baker.preview_atlas_layout", text="Предпросмотр раскладки", icon='HIDE_OFF')
        row.operator("baker.restore_materials_from_atlas", text="Откат материалов", icon='LOOP_BACK')

class BAKER_PT_udim_panel(Panel):
    """Панель для создания UDIM сетов из наборов текстур"""
    bl_label = "Создание UDIM сетов"
    bl_idname = "BAKER_PT_udim_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "AGR_baker"
    bl_options = {'DEFAULT_CLOSED'}
    
    def draw(self, context):
        layout = self.layout
        scene = context.scene
        ensure_scene_properties()
        obj = context.active_object
        
        box = layout.box()
        box.label(text="UDIM сеты из наборов текстур", icon='UV')
        if not obj:
            box.label(text="Выберите объект", icon='ERROR')
        else:
            # Информационный блок по имени
            if not obj.name.startswith("SM_"):
                box.label(text="Имя объекта не в формате SM_Address_Main/Ground", icon='INFO')
            else:
                name_parts = obj.name[3:].split('_')
                if len(name_parts) < 2 or name_parts[-1] not in ['Main', 'Ground']:
                    box.label(text="Имя объекта не в формате SM_Address_Main/Ground", icon='INFO')
            if not obj.material_slots:
                box.label(text="У объекта нет материалов", icon='INFO')
        
        # Сворачиваемый заголовок с кнопкой проверки материалов
        materials_box = layout.box()
        header_row = materials_box.row()
        header_row.prop(context.scene, "baker_udim_materials_collapsed", text="Материалы для UDIM:", icon='TRIA_DOWN' if context.scene.baker_udim_materials_collapsed else 'TRIA_RIGHT')
        header_row.operator("baker.scan_materials_for_udim", text="", icon='VIEWZOOM')

        # Получаем сохраненные материалы для этого объекта
        obj_materials = []
        if obj and hasattr(context.scene, 'baker_udim_materials') and len(context.scene.baker_udim_materials) > 0:
            obj_materials = [mat for mat in context.scene.baker_udim_materials if mat.object_name == obj.name]

        if not hasattr(context.scene, 'baker_udim_materials') or len(context.scene.baker_udim_materials) == 0:
            materials_box.label(text="Нет данных сканирования", icon='INFO')
        elif context.scene.baker_udim_materials_collapsed:

            if obj and obj_materials:
                # Показываем найденные материалы
                materials_box.label(text=f"Найдено {len(obj_materials)} подходящих материалов:")

                for i, mat_info in enumerate(obj_materials):
                    row = materials_box.row()
                    row.label(text=f"#{i+1}: {mat_info.material_name}")

                    sub = row.row()
                    sub.scale_y = 0.7
                    sub.label(text="[Diffuse, ERM, Normal]")

                layout.separator()
        else:
            materials_box.label(text="Нет подходящих материалов", icon='INFO')

        layout.separator()

        # Показываем информацию о том, что будет создано (всегда видимо)
        if obj and obj_materials and hasattr(context.scene, 'baker_udim_materials') and len(context.scene.baker_udim_materials) > 0:
            try:
                address, obj_type = process_object_name(obj.name)
                info_box = layout.box()
                col = info_box.column(align=True)
                col.label(text="Что будет создано:")

                udim_dir_name = get_udim_directory_name(address, obj_type)
                material_name = get_udim_material_name(address, obj_type)

                col.label(text=f"• UDIM текстуры в папке {udim_dir_name}")
                col.label(text=f"• Материал {material_name}")
                col.label(text="• UV координаты будут перемещены в тайлы")
                col.label(text="• Используются типы: DIFFUSE, ERM, NORMAL")

                # Показываем примеры имен файлов
                sub = col.column()
                sub.scale_y = 0.8
                sub.label(text="Примеры файлов:")
                example_diffuse = get_udim_texture_name(address, obj_type, "Diffuse", 1001)
                example_erm = get_udim_texture_name(address, obj_type, "ERM", 1001)
                sub.label(text=f"  {example_diffuse}")
                sub.label(text=f"  {example_erm}")

            except Exception as e:
                info_box = layout.box()
                info_box.label(text="Ошибка в имени объекта", icon='ERROR')
        
        # Напоминание о сохранении (не блокируем инструменты)
        if not bpy.data.filepath:
            warning_box = layout.box()
            warning_box.label(text="Рекомендуется сохранить .blend перед операциями UDIM", icon='INFO')
        
        # Кнопка создания UDIM (доступна при корректном имени)
        if obj and obj.name.startswith("SM_") and obj_materials:
            row = layout.row()
            row.scale_y = 2
            obj_materials_count = len(obj_materials)
            row.operator("baker.create_udim", text=f"Создать UDIM сет из {obj_materials_count} материалов", icon='UV_DATA')
            
        # Инструменты UDIM — теперь всегда видимы
        layout.separator()
        tools = layout.box()
        tools.label(text="Инструменты UDIM:", icon='UV')
        row = tools.row()
        row.operator("baker.rename_ucx", text="Переименовать в UCX", icon='MESH_ICOSPHERE')
        row.operator("baker.revert_udim_uvs", text="Откат в 0–1 (разбор UDIM)", icon='LOOP_BACK')

class BAKER_OT_scan_materials_for_udim(Operator):
    """Сканирует материалы объекта для создания UDIM"""
    bl_idname = "baker.scan_materials_for_udim"
    bl_label = "Сканировать материалы для UDIM"
    bl_description = "Ищет материалы с DIFFUSE, ERM, NORMAL текстурами для создания UDIM"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        scene = context.scene
        obj = context.active_object
        
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "Выберите меш объект")
            return {'CANCELLED'}
        
        print(f"\n🔍 === СКАНИРОВАНИЕ МАТЕРИАЛОВ ДЛЯ UDIM ===")
        print(f"Объект: {obj.name}")
        
        # Очищаем старые результаты для этого объекта
        if hasattr(scene, 'baker_udim_materials'):
            indices_to_remove = []
            for i, mat_info in enumerate(scene.baker_udim_materials):
                if mat_info.object_name == obj.name:
                    indices_to_remove.append(i)
            
            for i in reversed(indices_to_remove):
                scene.baker_udim_materials.remove(i)
        
        # Сканируем материалы
        found_materials = get_object_texture_sets_for_udim(context, obj)
        
        # Сохраняем результаты
        for material_info in found_materials:
            # Проверяем, не добавлен ли уже этот материал
            existing_mat = next((mat for mat in scene.baker_udim_materials if mat.material_name == material_info['material_name'] and mat.object_name == obj.name), None)
            if existing_mat:
                print(f"⚠️ Материал {material_info['material_name']} уже существует в списке, пропускаем")
                continue

            udim_mat = scene.baker_udim_materials.add()
            udim_mat.material_name = material_info['material_name']
            udim_mat.material_index = material_info['material_index']
            udim_mat.object_name = obj.name
            udim_mat.diffuse_path = material_info['diffuse_path']
            udim_mat.erm_path = material_info['erm_path']
            udim_mat.normal_path = material_info['normal_path']
            udim_mat.output_path = material_info['output_path']
        
        print(f"✅ Сканирование завершено: найдено {len(found_materials)} подходящих материалов")
        
        if found_materials:
            self.report({'INFO'}, f"Найдено {len(found_materials)} подходящих материалов")
        else:
            self.report({'WARNING'}, "Не найдено материалов с необходимыми текстурами")
        
        return {'FINISHED'}

def connect_textures_to_material_global(material, diffuse_img, erm_img, normal_img, opacity_img, connection_mode, normal_type, material_output_path):
    """Подключает созданные текстуры к материалу (глобальная версия)"""
    material_name = material.name

    textures_to_remove = [
        f"T_{material_name}_DIFFUSE",
        f"T_{material_name}_ERM",
        f"T_{material_name}_NORMAL",
        f"T_{material_name}_NORMAL_DIRECTX",
        f"T_{material_name}_OPACITY",
        f"T_{material_name}_ROUGHNESS",
        f"T_{material_name}_METALLIC",
        f"T_{material_name}_EMIT",
        f"T_{material_name}_DIFFUSE_OPACITY"
    ]

    for texture_name in textures_to_remove:
        if texture_name in bpy.data.images:
            bpy.data.images.remove(bpy.data.images[texture_name])

    for img_name in list(bpy.data.images.keys()):
        if any(tex_name in img_name for tex_name in textures_to_remove):
            bpy.data.images.remove(bpy.data.images[img_name])

    # Убираем очистку orphans, чтобы не удалять материалы
    # bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    print(f"✅ Очистка памяти завершена")

    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links

    nodes.clear()

    output = nodes.new(type='ShaderNodeOutputMaterial')
    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')

    output.location = (400, 0)
    bsdf.location = (100, 0)

    links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])

    def load_texture_from_disk_no_cleanup(texture_name, label, location, colorspace='sRGB'):
         """Загружает текстуру с диска и создает узел (без очистки)"""
         texture_path = os.path.join(material_output_path, f"{texture_name}.png")

         if os.path.exists(texture_path):
             try:
                 # Убираем удаление существующих изображений, чтобы избежать проблем с очисткой
                 if texture_name in bpy.data.images:
                     existing_img = bpy.data.images[texture_name]
                     # Просто используем существующее изображение, если оно есть
                     img = existing_img
                 else:
                     img = bpy.data.images.load(texture_path)
                     img.name = texture_name

                 img.filepath = texture_path
                 img.filepath_raw = texture_path
                 img.colorspace_settings.name = colorspace

                 img.reload()
                 img.update()
                 _ = img.pixels[0]

                 if img.has_data:
                     pass
                 else:
                     img.reload()
                     img.update()
                     _ = img.pixels[0]
                     if not img.has_data:
                         return None

                 tex_node = nodes.new(type='ShaderNodeTexImage')
                 tex_node.image = img
                 tex_node.location = location
                 tex_node.label = label

                 nodes.active = tex_node

                 return tex_node

             except Exception as e:
                 print(f"❌ Ошибка загрузки текстуры {label}: {e}")
                 return None
         else:
             print(f"⚠️  Файл текстуры не найден: {texture_path}")
             return None

    def load_texture_from_disk(texture_name, label, location, colorspace='sRGB'):
         """Загружает текстуру с диска и создает узел"""
         texture_path = os.path.join(material_output_path, f"{texture_name}.png")

         if os.path.exists(texture_path):
             try:
                 if texture_name in bpy.data.images:
                     bpy.data.images.remove(bpy.data.images[texture_name])

                 for img_name in list(bpy.data.images.keys()):
                     if texture_name in img_name:
                         bpy.data.images.remove(bpy.data.images[img_name])

                 "bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)"

                 img = bpy.data.images.load(texture_path)

                 img.name = texture_name

                 img.filepath = texture_path
                 img.filepath_raw = texture_path

                 img.colorspace_settings.name = colorspace

                 img.reload()
                 img.update()
                 _ = img.pixels[0]

                 if img.has_data:
                     pass

                 else:
                     img.reload()
                     img.update()
                     _ = img.pixels[0]
                     if not img.has_data:
                         return None

                 tex_node = nodes.new(type='ShaderNodeTexImage')
                 tex_node.image = img
                 tex_node.location = location
                 tex_node.label = label

                 nodes.active = tex_node

                 return tex_node

             except Exception as e:
                 print(f"❌ Ошибка загрузки текстуры {label}: {e}")
                 return None
         else:
             print(f"⚠️  Файл текстуры не найден: {texture_path}")
             return None

    if connection_mode == 'HIGH':
        print(f"🔧 Режим подключения: HIGH (ERM + DIFFUSE_OPACITY)")

        tex_diffuse_opacity = load_texture_from_disk_no_cleanup(f"T_{material_name}_DIFFUSE_OPACITY", "Diffuse Opacity", (-700, 300), 'sRGB')
        if tex_diffuse_opacity:
            links.new(tex_diffuse_opacity.outputs['Color'], bsdf.inputs['Base Color'])
            links.new(tex_diffuse_opacity.outputs['Color'], bsdf.inputs['Emission Color'])
            links.new(tex_diffuse_opacity.outputs['Alpha'], bsdf.inputs['Alpha'])

        if normal_type == 'DIRECTX':
            normal_texture_name = f"T_{material_name}_NORMAL_DIRECTX"
        else:
            normal_texture_name = f"T_{material_name}_NORMAL"

        tex_normal = load_texture_from_disk_no_cleanup(normal_texture_name, "Normal", (-700, 0), 'Non-Color')
        if tex_normal:
            # Создаем простой normal map node для подключения
            normal_map = nodes.new(type='ShaderNodeNormalMap')
            normal_map.location = (-400, 0)
            links.new(tex_normal.outputs['Color'], normal_map.inputs['Color'])
            links.new(normal_map.outputs['Normal'], bsdf.inputs['Normal'])

        tex_erm = load_texture_from_disk_no_cleanup(f"T_{material_name}_ERM", "ERM", (-700, -300), 'Non-Color')
        if tex_erm:
            separate_color = nodes.new(type='ShaderNodeSeparateColor')
            separate_color.location = (-400, -300)

            links.new(tex_erm.outputs['Color'], separate_color.inputs['Color'])

            links.new(separate_color.outputs['Red'], bsdf.inputs['Emission Strength'])
            links.new(separate_color.outputs['Green'], bsdf.inputs['Roughness'])
            links.new(separate_color.outputs['Blue'], bsdf.inputs['Metallic'])

    elif connection_mode == 'LOW':
        print(f"🔧 Режим подключения: LOW (отдельные карты)")

        tex_diffuse = load_texture_from_disk_no_cleanup(f"T_{material_name}_DIFFUSE", "Diffuse", (-700, 400), 'sRGB')
        if tex_diffuse:
            links.new(tex_diffuse.outputs['Color'], bsdf.inputs['Base Color'])

        tex_metallic = load_texture_from_disk_no_cleanup(f"T_{material_name}_METALLIC", "Metallic", (-700, 200), 'Non-Color')
        if tex_metallic:
            links.new(tex_metallic.outputs['Color'], bsdf.inputs['Metallic'])

        tex_roughness = load_texture_from_disk_no_cleanup(f"T_{material_name}_ROUGHNESS", "Roughness", (-700, 0), 'Non-Color')
        if tex_roughness:
            links.new(tex_roughness.outputs['Color'], bsdf.inputs['Roughness'])

        tex_opacity = load_texture_from_disk_no_cleanup(f"T_{material_name}_OPACITY", "Opacity", (-700, -200), 'Non-Color')
        if tex_opacity:
            links.new(tex_opacity.outputs['Color'], bsdf.inputs['Alpha'])

        if normal_type == 'DIRECTX':
            normal_texture_name = f"T_{material_name}_NORMAL_DIRECTX"
        else:
            normal_texture_name = f"T_{material_name}_NORMAL"

        tex_normal = load_texture_from_disk_no_cleanup(normal_texture_name, "Normal", (-700, -400), 'Non-Color')
        if tex_normal:
            # Создаем простой normal map node для подключения
            normal_map = nodes.new(type='ShaderNodeNormalMap')
            normal_map.location = (-400, -400)
            links.new(tex_normal.outputs['Color'], normal_map.inputs['Color'])
            links.new(normal_map.outputs['Normal'], bsdf.inputs['Normal'])

    material.blend_method = 'HASHED'
    material.shadow_method = 'HASHED'
    material.use_backface_culling = False

    bsdf.inputs['Base Color'].default_value = (1.0, 1.0, 1.0, 1.0)
    bsdf.inputs['Metallic'].default_value = 0.0
    bsdf.inputs['Roughness'].default_value = 0.8
    bsdf.inputs['IOR'].default_value = 1.5
    bsdf.inputs['Alpha'].default_value = 1.0
    bsdf.inputs['Emission Color'].default_value = (0.0, 0.0, 0.0, 1.0)
    bsdf.inputs['Emission Strength'].default_value = 0

    bpy.context.view_layer.update()

    for area in bpy.context.screen.areas:
        area.tag_redraw()

    material.node_tree.update_tag()

    print(f"✅ Текстуры загружены с диска и подключены к материалу {material.name} в режиме {connection_mode}")

    return material


def create_material_from_textures(material_name, folder_path, connection_mode, normal_type, resolution):
    """Создает материал и подключает к нему текстуры из папки"""
    print(f"\n🎨 === СОЗДАНИЕ МАТЕРИАЛА '{material_name}' ===")

    # Проверяем, существует ли уже материал
    existing_material = bpy.data.materials.get(material_name)
    if existing_material:
        print(f"   ⏭️ Материал '{material_name}' уже существует")
        return existing_material

    # Создаем новый материал
    material = bpy.data.materials.new(name=material_name)
    print(f"   ✅ Создан новый материал '{material_name}'")

    # Подключаем текстуры к материалу напрямую из файлов
    try:
        connect_textures_to_material_global(material, None, None, None, None,
                                          connection_mode, normal_type, folder_path)
        print(f"   ✅ Текстуры подключены к материалу в режиме {connection_mode}")
    except Exception as e:
        print(f"   ❌ Ошибка при подключении текстур: {e}")

    print(f"   🎯 Материал '{material_name}' готов к использованию")

    return material

def find_materials_using_baked_textures():
    """Находит материалы в сцене, которые используют текстуры из папки OBJECT_BAKED"""
    materials_with_baked_textures = {}

    print(f"\n🔍 === ПОИСК МАТЕРИАЛОВ С ТЕКСТУРАМИ ИЗ OBJECT_BAKED ===")

    # Получаем путь к папке OBJECT_BAKED
    blend_file_path = bpy.path.abspath("//")
    if not blend_file_path:
        print("⚠️ Файл не сохранен, невозможно найти папку OBJECT_BAKED")
        return materials_with_baked_textures

    main_baked_folder = os.path.join(blend_file_path, "OBJECT_BAKED")
    if not os.path.exists(main_baked_folder):
        print(f"⚠️ Папка OBJECT_BAKED не существует: {main_baked_folder}")
        return materials_with_baked_textures

    # Проходим по всем материалам в сцене
    for material in bpy.data.materials:
        if not material.use_nodes:
            continue

        material_name = material.name
        texture_paths = []

        # Проверяем ноды текстур на наличие путей к OBJECT_BAKED
        for node in material.node_tree.nodes:
            if node.type == 'TEX_IMAGE' and node.image:
                image_path = node.image.filepath
                if image_path and main_baked_folder in image_path:
                    texture_paths.append(image_path)

        # Если нашли текстуры из OBJECT_BAKED, анализируем материал
        if texture_paths:
            print(f"📋 Материал '{material_name}' использует {len(texture_paths)} текстур из OBJECT_BAKED")

            # Определяем ожидаемое имя материала из путей текстур
            expected_material_name = None
            for texture_path in texture_paths:
                # Извлекаем имя материала из пути к текстуре
                # Пример пути: ".../OBJECT_BAKED/MaterialName_generated/T_MaterialName_DIFFUSE.png"
                dirname = os.path.dirname(texture_path)
                folder_name = os.path.basename(dirname)

                # Определяем имя материала из названия папки
                if "_generated" in folder_name:
                    extracted_name = folder_name.replace("_generated", "")
                elif "_baked" in folder_name:
                    extracted_name = folder_name.replace("_baked", "")
                else:
                    continue

                if expected_material_name is None:
                    expected_material_name = extracted_name
                elif expected_material_name != extracted_name:
                    print(f"⚠️ Несогласованность имен в материале '{material_name}': {expected_material_name} vs {extracted_name}")
                    break

            if expected_material_name:
                materials_with_baked_textures[material_name] = {
                    'material': material,
                    'expected_name': expected_material_name,
                    'texture_paths': texture_paths,
                    'folder_path': os.path.dirname(texture_paths[0]) if texture_paths else None
                }

                if material_name != expected_material_name:
                    print(f"   ⚠️ Несоответствие имен: материал '{material_name}' ожидается '{expected_material_name}'")
                else:
                    print(f"   ✅ Имена совпадают: '{material_name}'")

    print(f"📊 Найдено материалов с текстурами из OBJECT_BAKED: {len(materials_with_baked_textures)}")
    return materials_with_baked_textures


def rename_material_and_related_elements(material, new_name, folder_path, texture_paths):
    """Переименовывает материал и все связанные элементы (папку, текстуры, ноды)"""
    old_name = material.name

    print(f"\n🔄 === ПЕРЕИМЕНОВАНИЕ МАТЕРИАЛА '{old_name}' -> '{new_name}' ===")

    try:
        # 1. Переименовываем материал
        material.name = new_name
        print(f"   ✅ Материал переименован: '{old_name}' -> '{new_name}'")

        # 2. Переименовываем папку
        if folder_path and os.path.exists(folder_path):
            parent_dir = os.path.dirname(folder_path)
            folder_name = os.path.basename(folder_path)

            # Определяем новый имя папки
            if "_generated" in folder_name:
                new_folder_name = f"{new_name}_generated"
            elif "_baked" in folder_name:
                new_folder_name = f"{new_name}_baked"
            else:
                new_folder_name = new_name

            new_folder_path = os.path.join(parent_dir, new_folder_name)

            if folder_path != new_folder_path:
                os.rename(folder_path, new_folder_path)
                print(f"   ✅ Папка переименована: '{folder_name}' -> '{new_folder_name}'")
                folder_path = new_folder_path
            else:
                print(f"   ⏭️ Папка уже имеет правильное имя")

        # 3. Переименовываем текстуры и обновляем пути
        old_texture_prefix = f"T_{old_name}"
        new_texture_prefix = f"T_{new_name}"

        for node in material.node_tree.nodes:
            if node.type == 'TEX_IMAGE' and node.image:
                image = node.image
                old_image_name = image.name

                # Переименовываем изображение в Blender
                if old_image_name.startswith(old_texture_prefix):
                    new_image_name = old_image_name.replace(old_texture_prefix, new_texture_prefix, 1)
                    image.name = new_image_name
                    print(f"   ✅ Изображение переименовано: '{old_image_name}' -> '{new_image_name}'")

                # Обновляем путь к файлу
                if image.filepath:
                    old_filepath = image.filepath

                    # Переименовываем файл на диске
                    if os.path.exists(old_filepath):
                        new_filepath = old_filepath.replace(old_texture_prefix, new_texture_prefix)
                        if old_filepath != new_filepath:
                            try:
                                os.rename(old_filepath, new_filepath)
                                image.filepath = new_filepath
                                image.filepath_raw = new_filepath
                                print(f"   ✅ Файл текстуры переименован: '{os.path.basename(old_filepath)}' -> '{os.path.basename(new_filepath)}'")
                            except Exception as e:
                                print(f"   ❌ Ошибка переименования файла: {e}")

        # 4. Обновляем наборы текстур в сцене
        scene = bpy.context.scene
        for tex_set in scene.baker_texture_sets:
            if tex_set.material_name == old_name:
                tex_set.material_name = new_name
                if folder_path:
                    tex_set.output_path = folder_path
                print(f"   ✅ Набор текстур обновлен: материал '{old_name}' -> '{new_name}'")

        print(f"   🎯 Переименование завершено успешно")

    except Exception as e:
        print(f"   ❌ Ошибка при переименовании: {e}")

    return folder_path


def scan_object_baked_folder(context):
    """Сканирует папку OBJECT_BAKED и добавляет найденные наборы текстур в список"""
    scene = context.scene
    texture_sets = scene.baker_texture_sets

    # Получаем путь к папке OBJECT_BAKED
    blend_file_path = bpy.path.abspath("//")
    if not blend_file_path:
        print("⚠️ Файл не сохранен, невозможно найти папку OBJECT_BAKED")
        return 0

    main_baked_folder = os.path.join(blend_file_path, "OBJECT_BAKED")
    if not os.path.exists(main_baked_folder):
        print(f"⚠️ Папка OBJECT_BAKED не существует: {main_baked_folder}")
        return 0

    print(f"\n🔍 === СКАНИРОВАНИЕ ПАПКИ OBJECT_BAKED ===")
    print(f"Путь: {main_baked_folder}")

    # 1. Сначала проверяем материалы на несоответствия имен
    print(f"\n🔍 Проверяем материалы на несоответствия имен...")
    materials_info = find_materials_using_baked_textures()

    # Обрабатываем материалы с несоответствующими именами
    renamed_count = 0
    for material_name, info in materials_info.items():
        if material_name != info['expected_name']:
            print(f"\n⚠️ Найдено несоответствие: материал '{material_name}' ожидается '{info['expected_name']}'")
            print(f"   📁 Папка: {info['folder_path']}")

            # Переименовываем материал и все связанные элементы
            new_folder_path = rename_material_and_related_elements(
                info['material'],
                info['expected_name'],
                info['folder_path'],
                info['texture_paths']
            )

            # Обновляем информацию о папке
            if new_folder_path != info['folder_path']:
                info['folder_path'] = new_folder_path

            renamed_count += 1

    if renamed_count > 0:
        print(f"\n✅ Переименовано материалов: {renamed_count}")
    else:
        print(f"\n✅ Несоответствий имен материалов не найдено")

    added_count = 0

    # 2. Сканируем подпапки в OBJECT_BAKED и добавляем наборы текстур
    for folder_name in os.listdir(main_baked_folder):
        folder_path = os.path.join(main_baked_folder, folder_name)

        # Пропускаем файлы, обрабатываем только папки
        if not os.path.isdir(folder_path):
            continue

        # Проверяем, является ли это папкой с текстурами (содержит _generated или _baked)
        if not ("_generated" in folder_name or "_baked" in folder_name):
            # Проверяем, является ли это атласом (начинается с Atlas_)
            if not folder_name.startswith("Atlas_"):
                continue

        print(f"📁 Обработка папки: {folder_name}")

        # Определяем имя материала из названия папки
        if "_generated" in folder_name:
            material_name = folder_name.replace("_generated", "")
        elif "_baked" in folder_name:
            material_name = folder_name.replace("_baked", "")
        elif folder_name.startswith("Atlas_"):
            # Для атласов пропускаем, так как они не являются отдельными наборами текстур
            continue
        else:
            continue

        # Проверяем, есть ли уже такой набор в списке
        existing_set = None
        for tex_set in texture_sets:
            if tex_set.material_name == material_name and tex_set.output_path == folder_path:
                existing_set = tex_set
                break

        # Также проверяем, не был ли материал переименован ранее
        renamed_material = None
        for mat_name, info in materials_info.items():
            if info['expected_name'] == material_name and info['folder_path'] == folder_path:
                renamed_material = bpy.data.materials.get(material_name)
                break

        # Если набор существует, проверяем наличие материала
        if existing_set:
            print(f"   ⏭️ Набор '{material_name}' уже существует в списке")
            # Проверяем, существует ли материал для этого набора
            existing_material = bpy.data.materials.get(material_name)
            if existing_material:
                print(f"   ⏭️ Материал '{material_name}' уже существует, пропускаем")
                continue
            else:
                print(f"   ⚠️ Материал '{material_name}' отсутствует, создаем...")
                # Создаем материал для существующего набора
                connection_mode = scene.baker_connection_mode
                normal_type = scene.baker_normal_type
                created_material = create_material_from_textures(
                    material_name, folder_path, connection_mode, normal_type, existing_set.resolution
                )
                if created_material:
                    print(f"   ✅ Материал '{material_name}' создан для существующего набора")
                continue

        # Если материал был переименован, обновляем существующий набор
        if renamed_material:
            print(f"   🔄 Материал '{material_name}' был переименован, обновляем набор...")
            # Находим набор с старым именем и обновляем его
            old_name = None
            for mat_name, info in materials_info.items():
                if info['expected_name'] == material_name and info['material'] == renamed_material:
                    old_name = mat_name
                    break

            if old_name:
                for tex_set in texture_sets:
                    if tex_set.material_name == old_name:
                        tex_set.material_name = material_name
                        tex_set.output_path = folder_path
                        print(f"   ✅ Набор обновлен: '{old_name}' -> '{material_name}'")
                        break
            continue

        # Создаем новый набор
        new_set = texture_sets.add()
        new_set.name = f"T_{material_name}"
        new_set.material_name = material_name
        new_set.object_name = ""  # Для найденных наборов объект неизвестен
        new_set.output_path = folder_path

        # Определяем разрешение из размера файлов (проверяем первый найденный файл)
        resolution = 1024  # значение по умолчанию
        base_path = os.path.join(folder_path, f"T_{material_name}")

        # Список возможных файлов для определения разрешения
        texture_files = [
            f"{base_path}_DIFFUSE.png",
            f"{base_path}_DIFFUSE_OPACITY.png",
            f"{base_path}_NORMAL.png",
            f"{base_path}_ERM.png"
        ]

        for texture_file in texture_files:
            if os.path.exists(texture_file):
                try:
                    # Загружаем изображение для определения размера
                    temp_image = bpy.data.images.load(texture_file)
                    resolution = temp_image.size[0]  # Предполагаем квадратные текстуры
                    bpy.data.images.remove(temp_image)
                    break
                except Exception as e:
                    print(f"   ⚠️ Ошибка при определении размера {texture_file}: {e}")

        new_set.resolution = resolution

        # Проверяем наличие файлов текстур
        new_set.has_diffuse = os.path.exists(f"{base_path}_DIFFUSE.png")
        new_set.has_diffuse_opacity = os.path.exists(f"{base_path}_DIFFUSE_OPACITY.png")
        new_set.has_normal = os.path.exists(f"{base_path}_NORMAL.png")
        new_set.has_normal_directx = os.path.exists(f"{base_path}_NORMAL_DIRECTX.png")
        new_set.has_roughness = os.path.exists(f"{base_path}_ROUGHNESS.png")
        new_set.has_metallic = os.path.exists(f"{base_path}_METALLIC.png")
        new_set.has_emit = os.path.exists(f"{base_path}_EMIT.png")
        new_set.has_opacity = os.path.exists(f"{base_path}_OPACITY.png")
        new_set.has_erm = os.path.exists(f"{base_path}_ERM.png")

        # Подсчитываем количество найденных текстур
        texture_count = sum([
            new_set.has_diffuse, new_set.has_diffuse_opacity, new_set.has_normal,
            new_set.has_normal_directx, new_set.has_roughness, new_set.has_metallic,
            new_set.has_emit, new_set.has_opacity, new_set.has_erm
        ])

        if texture_count > 0:
            added_count += 1
            print(f"   ✅ Добавлен набор '{material_name}' ({texture_count} текстур, {resolution}x{resolution})")

            # Создаем материал и подключаем текстуры, если материал не существует
            connection_mode = scene.baker_connection_mode
            normal_type = scene.baker_normal_type

            print(f"   🔧 Используем настройки: режим={connection_mode}, нормали={normal_type}")

            # Вызываем функцию создания материала
            created_material = create_material_from_textures(
                material_name, folder_path, connection_mode, normal_type, resolution
            )

            if created_material:
                print(f"   🎯 Материал '{material_name}' создан и настроен")
            else:
                print(f"   ⚠️ Не удалось создать материал '{material_name}'")

        else:
            # Если нет текстур, удаляем пустой набор
            texture_sets.remove(len(texture_sets) - 1)
            print(f"   ❌ Пропущен набор '{material_name}' (нет текстур)")

    print(f"📊 Найдено и добавлено наборов: {added_count}")
    print("=" * 50)

    return added_count


class BAKER_OT_refresh_texture_sets(Operator):
    """Обновляет список доступных наборов текстур, удаляя несуществующие и добавляя новые"""
    bl_idname = "baker.refresh_texture_sets"
    bl_label = "Обновить список наборов"
    bl_description = "Сканирует файловую систему, удаляет несуществующие наборы и добавляет новые из папки OBJECT_BAKED"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        scene = context.scene
        texture_sets = scene.baker_texture_sets
        
        indices_to_remove = []
        updated_count = 0
        removed_count = 0
        
        print("\n🔄 === ОБНОВЛЕНИЕ СПИСКА НАБОРОВ ТЕКСТУР ===")
        
        # Словарь для отслеживания дубликатов по имени
        name_to_latest_index = {}
        
        # Сначала находим дубликаты и помечаем старые для удаления
        for i, tex_set in enumerate(texture_sets):
            if tex_set.name in name_to_latest_index:
                # Помечаем предыдущую запись для удаления
                old_index = name_to_latest_index[tex_set.name]
                if old_index not in indices_to_remove:
                    indices_to_remove.append(old_index)
                    print(f"🔄 Найден дубликат '{tex_set.name}', старая запись будет удалена")
            
            # Сохраняем текущий индекс как последний для этого имени
            name_to_latest_index[tex_set.name] = i
        
        # Теперь проверяем существование файлов для оставшихся записей
        for i, tex_set in enumerate(texture_sets):
            if i in indices_to_remove:
                continue  # Пропускаем уже помеченные для удаления
                
            base_path = os.path.join(tex_set.output_path, tex_set.name)
            
            if not os.path.exists(tex_set.output_path):
                print(f"❌ Папка не существует: {tex_set.output_path}")
                indices_to_remove.append(i)
                continue
            
            has_any_texture = False
            texture_files = [
                f"{base_path}_DIFFUSE.png",
                f"{base_path}_DIFFUSE_OPACITY.png", 
                f"{base_path}_NORMAL.png",
                f"{base_path}_NORMAL_DIRECTX.png",
                f"{base_path}_ROUGHNESS.png",
                f"{base_path}_METALLIC.png",
                f"{base_path}_EMIT.png",
                f"{base_path}_OPACITY.png",
                f"{base_path}_ERM.png"
            ]
            
            for texture_file in texture_files:
                if os.path.exists(texture_file):
                    has_any_texture = True
                    break
            
            if not has_any_texture:
                print(f"❌ Набор '{tex_set.name}' не имеет файлов текстур")
                indices_to_remove.append(i)
            else:
                old_diffuse = tex_set.has_diffuse
                old_normal = tex_set.has_normal
                old_roughness = tex_set.has_roughness
                
                tex_set.has_diffuse = os.path.exists(f"{base_path}_DIFFUSE.png")
                tex_set.has_diffuse_opacity = os.path.exists(f"{base_path}_DIFFUSE_OPACITY.png")
                tex_set.has_normal = os.path.exists(f"{base_path}_NORMAL.png")
                tex_set.has_normal_directx = os.path.exists(f"{base_path}_NORMAL_DIRECTX.png")
                tex_set.has_roughness = os.path.exists(f"{base_path}_ROUGHNESS.png")
                tex_set.has_metallic = os.path.exists(f"{base_path}_METALLIC.png")
                tex_set.has_emit = os.path.exists(f"{base_path}_EMIT.png")
                tex_set.has_opacity = os.path.exists(f"{base_path}_OPACITY.png")
                tex_set.has_erm = os.path.exists(f"{base_path}_ERM.png")
                
                if (old_diffuse != tex_set.has_diffuse or 
                    old_normal != tex_set.has_normal or 
                    old_roughness != tex_set.has_roughness):
                    updated_count += 1
                    print(f"🔄 Обновлен набор: {tex_set.name}")
        
        for i in reversed(indices_to_remove):
            removed_name = texture_sets[i].name
            texture_sets.remove(i)
            removed_count += 1
            print(f"🗑️ Удален набор: {removed_name}")

        # Сканируем папку OBJECT_BAKED и добавляем новые наборы
        added_count = scan_object_baked_folder(context)

        print(f"✅ Обновление завершено:")
        print(f"   📊 Всего наборов: {len(texture_sets)}")
        print(f"   🔄 Обновлено: {updated_count}")
        print(f"   🗑️ Удалено: {removed_count}")
        print(f"   ➕ Добавлено: {added_count}")
        print("=" * 50)

        if added_count > 0:
            self.report({'INFO'}, f"Добавлено {added_count} новых наборов из OBJECT_BAKED")
        elif removed_count > 0:
            self.report({'INFO'}, f"Удалено {removed_count} несуществующих наборов")
        elif updated_count > 0:
            self.report({'INFO'}, f"Обновлено {updated_count} наборов")
        else:
            self.report({'INFO'}, "Все наборы актуальны")
        
        return {'FINISHED'}

class BAKER_OT_generate_from_material(Operator):
    """Создает набор текстур из настроек материала выбранного объекта"""
    bl_idname = "baker.generate_from_material"
    bl_label = "Сгенерировать из материала"
    bl_options = {'REGISTER', 'UNDO'}
    
    resolution: EnumProperty(
        name="Resolution",
        description="Texture resolution",
        items=resolutions,
        default='1024'
    )
    
    connection_mode: EnumProperty(
        name="Connection Mode",
        description="Mode for connecting textures to material",
        items=connection_modes,
        default='HIGH'
    )
    
    normal_type: EnumProperty(
        name="Normal Type", 
        description="Type of normal map to generate",
        items=normal_types,
        default='OPENGL'
    )
    
    generate_all_materials: BoolProperty(
        name="Все материалы",
        description="Генерировать текстуры для всех материалов на объекте",
        default=False
    )
    overwrite_existing: BoolProperty(
        name="Перезаписать существующие",
        description="Перезаписать уже существующие запеченные или сгенерированные текстуры",
        default=False
    )
    
    resize_textures_256: BoolProperty(
        name="Переразмерять текстуры 256px",
        description="Переразмерять текстуры 256x256 пикселей",
        default=False
    )
    @classmethod
    def poll(cls, context):
        return (context.active_object and 
                context.active_object.type == 'MESH' and
                context.active_object.active_material)
    
    def execute(self, context):
        obj = context.active_object
        
        if not obj or obj.type != 'MESH':
            self.report({'ERROR'}, "Выберите MESH объект")
            return {'CANCELLED'}
            
        blend_file_path = bpy.path.abspath("//")
        if not blend_file_path:
            self.report({'ERROR'}, "Сохраните файл перед генерацией текстур")
            return {'CANCELLED'}
        
        main_baked_folder = os.path.join(blend_file_path, "OBJECT_BAKED")
        
        if not os.path.exists(main_baked_folder):
            os.makedirs(main_baked_folder)
        
        self.refresh_texture_sets_list(context)
        resolution = int(self.resolution)
        
        # Поддержка пакетной генерации по выделенным объектам
        target_objects = [obj]
        if context.selected_objects and len(context.selected_objects) > 1:
            target_objects = [o for o in context.selected_objects if o.type == 'MESH']

        # Собираем пары (объект, материал) для обработки
        pairs = []
        if self.generate_all_materials:
            for o in target_objects:
                for slot in o.material_slots:
                    if slot.material and slot.material.use_nodes:
                        pairs.append((o, slot.material))
            if not pairs:
                self.report({'ERROR'}, "Нет материалов с нодами у выбранных объектов")
                return {'CANCELLED'}
        else:
            for o in target_objects:
                material = o.active_material
                if material and material.use_nodes:
                    pairs.append((o, material))
            if not pairs:
                self.report({'ERROR'}, "Нет активных материалов с нодами у выбранных объектов")
                return {'CANCELLED'}
        
        # Уникальные материалы для статуса
        unique_materials = []
        seen = set()
        for _, m in pairs:
            if m.name not in seen:
                unique_materials.append(m)
                seen.add(m.name)
        status_info = self.get_materials_status(context, unique_materials, main_baked_folder)
        
        print(f"\n📊 === АНАЛИЗ МАТЕРИАЛОВ ===")
        print(f"Всего материалов для обработки: {status_info['total']}")
        print(f"Уже имеют текстуры: {status_info['existing']}")
        print(f"  - Сгенерированные: {status_info['existing_generated']}")
        print(f"  - Запеченные: {status_info['existing_baked']}")
        print(f"Требуют обработки: {len(status_info['to_process'])}")
        print(f"Режим перезаписи: {'Включен' if self.overwrite_existing else 'Выключен'}")
        
        final_materials_to_process = []
        skipped_materials = []
        
        # Если включена перезапись - обрабатываем все материалы без проверок
        if self.overwrite_existing:
            final_materials_to_process = unique_materials[:]
            print(f"🔄 Режим перезаписи включен - будут обработаны все {len(final_materials_to_process)} материалов")
        else:
            # Иначе проверяем существование текстур и пропускаем существующие
            for material in unique_materials:
                has_textures, texture_type, texture_path = self.check_material_has_textures(
                    context, material.name, main_baked_folder)
                
                if has_textures:
                    skipped_materials.append((material, texture_type, texture_path))
                    print(f"⏭️  Пропускаем '{material.name}' - уже есть {texture_type} текстуры")
                else:
                    final_materials_to_process.append(material)
        
        if skipped_materials:
            material_names = [mat[0].name for mat in skipped_materials]
            self.report({'INFO'}, f"Пропущено {len(skipped_materials)} материалов с существующими текстурами: {', '.join(material_names)}")
        
        if not final_materials_to_process:
            if skipped_materials:
                self.report({'WARNING'}, "Все материалы уже имеют текстуры. Включите 'Перезаписать существующие' для обновления.")
            else:
                self.report({'ERROR'}, "Нет материалов для обработки")
            return {'CANCELLED'}
        
        # Пары к обработке, отфильтрованные по статусу
        final_pairs = []
        final_names = {m.name for m in final_materials_to_process}
        for obj_ref, material in pairs:
            if material.name in final_names:
                final_pairs.append((obj_ref, material))

        for obj_ref, material in final_pairs:
            material_output_path = os.path.join(main_baked_folder, f"{material.name}_generated")

            if os.path.exists(material_output_path):
                print(f"📁 Используем существующую папку:")
                print(f"   Материал: {material.name}")
                print(f"   Путь: {material_output_path}")
            else:
                print(f"📁 Создаем новую папку для материала:")
                print(f"   Материал: {material.name}")
                print(f"   Путь: {material_output_path}")
                try:
                    os.makedirs(material_output_path, exist_ok=True)
                except Exception as e:
                    print(f"❌ Ошибка создания папки: {e}")
                    continue
            
            # Перед генерацией автоисправление color space входящих текстур
            try:
                self.autofix_input_textures_colorspace(material)
            except Exception as e:
                print(f"⚠️ Ошибка автофикса color space для {material.name}: {e}")

            self.generate_textures_from_material(context, obj_ref, material, resolution, material_output_path)

        # После завершения всех операций генерации удаляем конфликтующие текстуры
        processed_materials = [mat.name for _, mat in final_pairs]
        for material_name in processed_materials:
            self.remove_conflicting_textures(context, material_name, main_baked_folder, 'generate')

        processed_count = len(final_pairs)
        skipped_count = len(skipped_materials)
        self.report({'INFO'}, f"Обработано: {processed_count}, Пропущено: {skipped_count} материалов")
        
        # Обновляем список наборов текстур
        bpy.ops.baker.refresh_texture_sets()
        
        return {'FINISHED'}

    def autofix_input_textures_colorspace(self, material):
        """Автоправка color space у входящих текстур: BaseColor/Emission — sRGB, техкарты — Non-Color.
        Файлы сохраняем как sRGB — этот шаг не меняет формат файлов, только узлы."""
        if not material or not material.use_nodes:
            return
        node_tree = material.node_tree
        for node in node_tree.nodes:
            if node.type == 'TEX_IMAGE' and getattr(node, 'image', None):
                # Пытаемся определить назначение по связям
                dest_sockets = [link.to_socket for link in node.outputs['Color'].links]
                dest_names = {sock.name for sock in dest_sockets}
                # Цветовые
                if any(n in dest_names for n in ['Base Color', 'Emission', 'Subsurface Color']) or 'Color' in dest_names:
                    try:
                        node.image.colorspace_settings.name = 'sRGB'
                    except Exception:
                        pass
                # Технические
                technical_hit = any(n in dest_names for n in ['Metallic', 'Roughness', 'Specular', 'Normal', 'Alpha', 'Displacement', 'Height'])
                if technical_hit:
                    try:
                        node.image.colorspace_settings.name = 'Non-Color'
                    except Exception:
                        pass
    
    def check_material_has_textures(self, context, material_name, main_baked_folder):
        """Проверяет существование текстур для материала"""
        
        # Если включена overwrite existing - считаем что нет существующих текстур
        if context.scene.baker_overwrite_existing:
            return False, None, None
        
        generated_path = os.path.join(main_baked_folder, f"{material_name}_generated")
        if os.path.exists(generated_path):
            texture_files = [
                f"T_{material_name}_DIFFUSE.png",
                f"T_{material_name}_ERM.png",
                f"T_{material_name}_NORMAL.png",
                f"T_{material_name}_DIFFUSE_OPACITY.png"
            ]
            
            for texture_file in texture_files:
                if os.path.exists(os.path.join(generated_path, texture_file)):
                    return True, "generated", generated_path
        
        for tex_set in context.scene.baker_texture_sets:
            if tex_set.material_name == material_name:
                # Проверяем, что это действительно запеченная текстура (содержит "_baked" в пути)
                if "_baked" in tex_set.output_path:
                    return True, "baked", tex_set.output_path
        
        return False, None, None

    def remove_conflicting_textures(self, context, material_name, main_baked_folder, operation_type):
        """Удаляет конфликтующие текстуры перед созданием новых

        Args:
            operation_type: 'bake' или 'generate'
        """
        print(f"🔄 Удаление конфликтующих текстур для материала '{material_name}'...")

        if operation_type == 'bake':
            # При запекании удаляем сгенерированные текстуры
            generated_path = os.path.join(main_baked_folder, f"{material_name}_generated")
            if os.path.exists(generated_path):
                try:
                    import shutil
                    shutil.rmtree(generated_path)
                    print(f"🗑️ Удалена папка сгенерированных текстур: {generated_path}")
                except Exception as e:
                    print(f"⚠️ Ошибка удаления папки {generated_path}: {e}")

            # Удаляем запись из списка текстурных наборов, если она указывает на generated
            texture_sets_to_remove = []
            for i, tex_set in enumerate(context.scene.baker_texture_sets):
                if tex_set.material_name == material_name and "_generated" in tex_set.output_path:
                    texture_sets_to_remove.append(i)
                    print(f"🗑️ Удалена запись о сгенерированных текстурах: {tex_set.name}")

            # Удаляем в обратном порядке, чтобы индексы оставались корректными
            for i in reversed(texture_sets_to_remove):
                context.scene.baker_texture_sets.remove(i)

        elif operation_type == 'generate':
            # При генерации удаляем запеченные текстуры
            baked_path = os.path.join(main_baked_folder, f"{material_name}_baked")
            if os.path.exists(baked_path):
                try:
                    import shutil
                    shutil.rmtree(baked_path)
                    print(f"🗑️ Удалена папка с запеченными текстурами: {baked_path}")
                except Exception as e:
                    print(f"⚠️ Ошибка удаления папки {baked_path}: {e}")

            # Удаляем запись из списка текстурных наборов, если она указывает на baked
            texture_sets_to_remove = []
            for i, tex_set in enumerate(context.scene.baker_texture_sets):
                if tex_set.material_name == material_name and "_baked" in tex_set.output_path:
                    texture_sets_to_remove.append(i)
                    print(f"🗑️ Удалена запись о запеченных текстурах: {tex_set.name}")

            # Удаляем в обратном порядке, чтобы индексы оставались корректными
            for i in reversed(texture_sets_to_remove):
                context.scene.baker_texture_sets.remove(i)

    def get_materials_status(self, context, materials, main_baked_folder):
        """Получает статус существования текстур для списка материалов"""
        status_info = {
            'total': len(materials),
            'existing': 0,
            'existing_generated': 0, 
            'existing_baked': 0,
            'to_process': []
        }
        
        # Если включена overwrite existing - возвращаем пустой статус (все материалы к обработке)
        if context.scene.baker_overwrite_existing:
            status_info['to_process'] = materials[:]
            return status_info
            
        for material in materials:
            has_textures, texture_type, texture_path = self.check_material_has_textures(
                context, material.name, main_baked_folder)
            
            if has_textures:
                status_info['existing'] += 1
                if texture_type == 'generated':
                    status_info['existing_generated'] += 1
                elif texture_type == 'baked':
                    status_info['existing_baked'] += 1
            else:
                status_info['to_process'].append(material)
        
        return status_info
    
    def generate_textures_from_material(self, context, obj, material, resolution, output_path):
        """Создает набор текстур из настроек материала"""
        
        print(f"\n🎨 === ГЕНЕРАЦИЯ ТЕКСТУР ИЗ МАТЕРИАЛА ===")
        print(f"Объект: {obj.name}")
        print(f"Материал: {material.name}")
        
        principled_node = None
        for node in material.node_tree.nodes:
            if node.type == 'BSDF_PRINCIPLED':
                principled_node = node
                break
        
        if not principled_node:
            self.report({'ERROR'}, "В материале не найдена нода Principled BSDF")
            return
        
        mat_name = material.name

        # Определяем, используются ли реальные текстуры по каналам
        base_tex, _ = self.get_texture_or_value_from_slot(principled_node, 'Base Color')
        has_normal_texture = not self.is_flat_normal(principled_node)
        r_tex, _ = self.get_texture_or_value_from_slot(principled_node, 'Roughness')
        m_tex, _ = self.get_texture_or_value_from_slot(principled_node, 'Metallic')
        e_tex, _ = self.get_texture_or_value_from_slot(principled_node, 'Emission Strength')
        # Для альфы учитываем либо явную текстуру Alpha, либо альфа-канал диффузной
        a_tex, _ = self.get_texture_or_value_from_slot(principled_node, 'Alpha')
        has_diffuse_alpha = False
        if not a_tex and base_tex:
            try:
                has_diffuse_alpha = self.texture_has_alpha_channel(base_tex)
            except Exception:
                has_diffuse_alpha = False

        erm_has_texture = any([r_tex, m_tex, e_tex])

        # Пер-канальные разрешения
        # Логика разрешений при resize_textures_256:
        # - Если включен режим переразмеривания 256px текстур, то все текстуры размером 256px увеличиваются до базового разрешения
        # - Если режим выключен, то 256px текстуры остаются 256px, остальные используют базовое разрешение
        
        # Диффуз
        if base_tex:
            if max(base_tex.size) <= 256:
                diffuse_res = resolution if self.resize_textures_256 else 256
            else:
                diffuse_res = resolution
        else:
            diffuse_res = 256  # Если нет текстуры, используем минимальное разрешение
            
        # Нормаль — попробуем взять текстуру из слота Normal
        normal_src_img, _ = self.get_texture_or_value_from_slot(principled_node, 'Normal')
        if has_normal_texture or normal_src_img:
            if normal_src_img and max(normal_src_img.size) <= 256:
                normal_res = resolution if self.resize_textures_256 else 256
            else:
                normal_res = resolution
        else:
            normal_res = 256
            
        # Roughness
        if r_tex:
            if max(r_tex.size) <= 256:
                roughness_res = resolution if self.resize_textures_256 else 256
            else:
                roughness_res = resolution
        else:
            roughness_res = 256
            
        # Metallic
        if m_tex:
            if max(m_tex.size) <= 256:
                metallic_res = resolution if self.resize_textures_256 else 256
            else:
                metallic_res = resolution
        else:
            metallic_res = 256
            
        # Emission
        if e_tex:
            if max(e_tex.size) <= 256:
                emit_res = resolution if self.resize_textures_256 else 256
            else:
                emit_res = resolution
        else:
            emit_res = 256
            
        # DIFFUSE_OPACITY — учитываем размеры диффуза и альфы
        if base_tex or a_tex or has_diffuse_alpha:
            # Берем максимальный размер между диффузом и альфой
            max_source_size = 256
            if base_tex:
                max_source_size = max(max_source_size, max(base_tex.size))
            if a_tex:
                max_source_size = max(max_source_size, max(a_tex.size))
            
            if max_source_size <= 256:
                diffuse_opacity_res = resolution if self.resize_textures_256 else 256
            else:
                diffuse_opacity_res = resolution
        else:
            diffuse_opacity_res = 256
            
        # OPACITY — используем то же разрешение, что и DIFFUSE_OPACITY для корректного объединения
        opacity_res = diffuse_opacity_res
        
        # ERM — если есть хотя бы одна текстура с размером больше 256, используем базовое разрешение
        if erm_has_texture:
            max_erm_size = 256
            if r_tex:
                max_erm_size = max(max_erm_size, max(r_tex.size))
            if m_tex:
                max_erm_size = max(max_erm_size, max(m_tex.size))
            if e_tex:
                max_erm_size = max(max_erm_size, max(e_tex.size))
            
            if max_erm_size <= 256:
                erm_res = resolution if self.resize_textures_256 else 256
            else:
                erm_res = resolution
        else:
            erm_res = 256

        print(f"Размеры: DIFFUSE {diffuse_res}, ERM {erm_res}, NORMAL {normal_res}")
        
        diffuse_img = self.create_texture_image(f"T_{mat_name}_DIFFUSE", diffuse_res, with_alpha=True)  # Всегда с альфой для совместимости
        roughness_img = self.create_texture_image(f"T_{mat_name}_ROUGHNESS", roughness_res)
        metallic_img = self.create_texture_image(f"T_{mat_name}_METALLIC", metallic_res)
        emit_img = self.create_texture_image(f"T_{mat_name}_EMIT", emit_res)
        normal_img = self.create_texture_image(f"T_{mat_name}_NORMAL", normal_res)
        opacity_img = self.create_texture_image(f"T_{mat_name}_OPACITY", opacity_res)
        erm_img = self.create_texture_image(f"T_{mat_name}_ERM", erm_res)
        diffuse_opacity_img = self.create_texture_image(f"T_{mat_name}_DIFFUSE_OPACITY", diffuse_opacity_res, with_alpha=True)
        
        is_diffuse_opacity = self.create_diffuse_from_material(principled_node, diffuse_img, diffuse_res)
        
        self.create_channel_texture_from_material(principled_node, 'Roughness', roughness_img, roughness_res, default_value=0.8)
        self.create_channel_texture_from_material(principled_node, 'Metallic', metallic_img, metallic_res, default_value=0.0)
        self.create_channel_texture_from_material(principled_node, 'Emission Strength', emit_img, emit_res, default_value=0.0)
        self.create_normal_from_material(principled_node, normal_img, normal_res)
        
        if is_diffuse_opacity:
            print(f"📢 Извлекаем OPACITY из DIFFUSE_OPACITY")
            diffuse_pixels = diffuse_img.pixels[:]
            opacity_pixels = []
            for i in range(3, len(diffuse_pixels), 4):
                alpha_value = diffuse_pixels[i]
                opacity_pixels.extend([alpha_value, alpha_value, alpha_value, 1.0])  # R=G=B=alpha, A=1
            opacity_img.pixels = opacity_pixels
            
            diffuse_opacity_img.pixels = diffuse_img.pixels[:]
        else:
            self.create_alpha_from_material(principled_node, opacity_img, opacity_res)
            self.combine_diffuse_and_alpha(diffuse_img, opacity_img, diffuse_opacity_img, diffuse_opacity_res)
        
        self.create_erm_from_textures(emit_img, roughness_img, metallic_img, erm_img, erm_res)
        
        if is_diffuse_opacity:
            diffuse_opacity_path = os.path.join(output_path, f"T_{mat_name}_DIFFUSE_OPACITY.png")
            self.save_texture(diffuse_img, diffuse_opacity_path)
            print(f"💾 Сохранено как DIFFUSE_OPACITY: {diffuse_opacity_path}")
            
            # Загружаем сохраненную DIFFUSE_OPACITY с диска для извлечения DIFFUSE
            try:
                if os.path.exists(diffuse_opacity_path):
                    disk_diffuse_opacity = bpy.data.images.load(diffuse_opacity_path)
                    disk_diffuse_opacity.reload()
                    source_pixels = disk_diffuse_opacity.pixels[:]
                    bpy.data.images.remove(disk_diffuse_opacity)
                else:
                    source_pixels = diffuse_img.pixels[:]
            except Exception:
                source_pixels = diffuse_img.pixels[:]
            
            diffuse_no_alpha = self.create_texture_image(f"T_{mat_name}_DIFFUSE_NO_ALPHA", diffuse_res, with_alpha=False)
            
            # Копирование пикселей RGB с установкой альфы = 1.0
            no_alpha_pixels = []
            for i in range(0, len(source_pixels), 4):
                if i + 3 < len(source_pixels):
                    no_alpha_pixels.extend([
                        source_pixels[i],     # R
                        source_pixels[i + 1], # G
                        source_pixels[i + 2], # B
                        1.0                   # A = 1.0
                    ])
            
            diffuse_no_alpha.pixels = no_alpha_pixels
            
            diffuse_path = os.path.join(output_path, f"T_{mat_name}_DIFFUSE.png")
            self.save_texture(diffuse_no_alpha, diffuse_path)
            bpy.data.images.remove(diffuse_no_alpha)
            print(f"💾 Сохранено как DIFFUSE (без альфы): {diffuse_path}")
        else:
            diffuse_path = os.path.join(output_path, f"T_{mat_name}_DIFFUSE.png")
            self.save_texture(diffuse_img, diffuse_path)
            print(f"💾 Сохранено как DIFFUSE: {diffuse_path}")
            
            diffuse_opacity_path = os.path.join(output_path, f"T_{mat_name}_DIFFUSE_OPACITY.png")
            self.save_texture(diffuse_opacity_img, diffuse_opacity_path)
            print(f"💾 Сохранено как DIFFUSE_OPACITY: {diffuse_opacity_path}")
        
        self.save_texture(roughness_img, os.path.join(output_path, f"T_{mat_name}_ROUGHNESS.png"))
        self.save_texture(metallic_img, os.path.join(output_path, f"T_{mat_name}_METALLIC.png"))
        self.save_texture(emit_img, os.path.join(output_path, f"T_{mat_name}_EMIT.png"))
        self.save_texture(opacity_img, os.path.join(output_path, f"T_{mat_name}_OPACITY.png"))
        self.save_texture(erm_img, os.path.join(output_path, f"T_{mat_name}_ERM.png"))
        
        normal_path = os.path.join(output_path, f"T_{mat_name}_NORMAL.png")
        self.save_texture(normal_img, normal_path)
        
        normal_directx_img = self.create_texture_image(f"T_{mat_name}_NORMAL_DIRECTX", normal_res)
        
        is_flat_normal = self.is_flat_normal(principled_node)
        
        if is_flat_normal:
            print(f"🔄 Создаем плоскую DirectX нормаль напрямую вместо конвертации")
            pixels = []
            for _ in range(normal_res * normal_res):
                pixels.extend([0.5, 0.5, 1.0, 1.0])
            normal_directx_img.pixels = pixels
        else:
            # Пытаемся загрузить сохраненную обычную нормаль с диска для корректной конвертации
            normal_path = os.path.join(output_path, f"T_{mat_name}_NORMAL.png")
            if os.path.exists(normal_path):
                print(f"📂 Загружаем обычную нормаль с диска для конвертации в DirectX")
                try:
                    # Загружаем текстуру с диска
                    loaded_normal = bpy.data.images.load(normal_path)
                    loaded_normal.name = f"T_{mat_name}_NORMAL_loaded"

                    # Копируем пиксели из загруженной текстуры
                    if loaded_normal.size[0] == normal_res and loaded_normal.size[1] == normal_res:
                        normal_directx_img.pixels = loaded_normal.pixels[:]
                    else:
                        print(f"⚠️ Размер загруженной нормали не совпадает, используем текущую")
                        normal_directx_img.pixels = normal_img.pixels[:]

                    # Удаляем загруженную текстуру
                    bpy.data.images.remove(loaded_normal)

                except Exception as e:
                    print(f"❌ Ошибка загрузки нормали с диска: {e}, используем текущую")
                    normal_directx_img.pixels = normal_img.pixels[:]
            else:
                print(f"📝 Используем текущую нормаль для конвертации в DirectX")
                normal_directx_img.pixels = normal_img.pixels[:]

            # Конвертируем в DirectX формат
            self.convert_normal_to_directx(normal_directx_img)
            
        normal_directx_path = os.path.join(output_path, f"T_{mat_name}_NORMAL_DIRECTX.png")
        self.save_texture(normal_directx_img, normal_directx_path)
        
        self.connect_textures_to_material(material, diffuse_img, erm_img, normal_img, opacity_img, self.connection_mode, self.normal_type, output_path)
        
        # Определяем максимальный размер среди всех созданных текстур для записи в набор
        max_resolution = max(diffuse_res, erm_res, normal_res, diffuse_opacity_res, opacity_res)
        print(f"📏 Максимальный размер в наборе: {max_resolution}px")
        
        self.save_texture_set_info_with_path(context, obj, mat_name, max_resolution, output_path)
        
        print(f"✅ Генерация текстур завершена!")

    def get_texture_or_value_from_slot(self, node, slot_name):
        """Получает текстуру или значение из слота ноды"""
        slot = node.inputs.get(slot_name)
        if not slot:
            return None, 0.0

        if slot.links:
            input_node = slot.links[0].from_node
            # Прямое подключение текстуры
            if input_node.type == 'TEX_IMAGE' and input_node.image:
                return input_node.image, None
            # Поддержка SeparateColor/SeparateRGB → берем соответствующий вход
            if input_node.type in {'SEPRGB', 'SEPARATE_COLOR'}:
                # пытаемся найти источник у входа Color
                color_input = input_node.inputs.get('Image') or input_node.inputs.get('Color')
                if color_input and color_input.links:
                    src = color_input.links[0].from_node
                    if src.type == 'TEX_IMAGE' and src.image:
                        return src.image, None
            # Поддержка через ColorRamp/Math/VectorMath → ищем ближайшую текстуру вверх по цепочке
            walker = input_node
            visited = set()
            while walker and walker not in visited:
                visited.add(walker)
                inps = [s for s in walker.inputs if hasattr(s, 'links') and s.links]
                next_walker = None
                for s in inps:
                    src = s.links[0].from_node
                    if src.type == 'TEX_IMAGE' and getattr(src, 'image', None):
                        return src.image, None
                    if src.type in {'MIX', 'MIX_RGB', 'HUE_SAT', 'BRIGHTCONTRAST', 'RGBCURVES', 'VALTORGB', 'MATH', 'VECT_MATH', 'NORMAL_MAP', 'BUMP', 'SEPARATE_COLOR', 'SEPRGB'}:
                        next_walker = src
                        break
                walker = next_walker

        if hasattr(slot, 'default_value'):
            return None, slot.default_value
        return None, 0.0

    def get_texture_connection_type(self, node, slot_name):
        """Определяет тип подключения текстуры к слоту: 'alpha', 'color' или None"""
        slot = node.inputs.get(slot_name)
        if not slot or not slot.links:
            return None

        link = slot.links[0]
        from_node = link.from_node
        from_socket = link.from_socket

        # Проверяем, подключена ли текстура напрямую
        if from_node.type == 'TEX_IMAGE':
            # Определяем тип output'а по имени сокета
            if from_socket.name.lower() in ['alpha', 'a']:
                return 'alpha'
            elif from_socket.name.lower() in ['color', 'rgb']:
                return 'color'
            else:
                # Если имя сокета не определяет тип, проверяем по индексу
                # Обычно Alpha - это индекс 1 (после Color на индексе 0)
                if hasattr(from_socket, 'index') and from_socket.index == 1:
                    return 'alpha'
                else:
                    return 'color'

        # Для других типов нод пытаемся пройти по цепочке
        walker = from_node
        visited = set()
        while walker and walker not in visited:
            visited.add(walker)
            if walker.type == 'TEX_IMAGE':
                # Нашли текстуру, проверяем через какой output она подключена к следующей ноде
                for output in walker.outputs:
                    if output.links:
                        for out_link in output.links:
                            if out_link.to_node == from_node:
                                if output.name.lower() in ['alpha', 'a']:
                                    return 'alpha'
                                elif output.name.lower() in ['color', 'rgb']:
                                    return 'color'
                                else:
                                    # По индексу
                                    if hasattr(output, 'index') and output.index == 1:
                                        return 'alpha'
                                    else:
                                        return 'color'
            # Продолжаем искать вверх по цепочке
            inps = [s for s in walker.inputs if hasattr(s, 'links') and s.links]
            if inps:
                walker = inps[0].links[0].from_node
            else:
                break

        return None

    def is_alpha_connected_to_principled(self, principled_node):
        """Проверяет, подключена ли альфа-текстура в слот ALPHA Principled BSDF"""
        alpha_slot = principled_node.inputs.get('Alpha')
        if not alpha_slot or not alpha_slot.links:
            return False

        # Получаем подключенную ноду
        link = alpha_slot.links[0]
        from_node = link.from_node
        from_socket = link.from_socket

        # Проверяем, подключена ли текстура напрямую через альфа-канал
        if from_node.type == 'TEX_IMAGE':
            # Проверяем тип output'а по имени сокета или индексу
            if from_socket.name.lower() in ['alpha', 'a']:
                return True
            elif hasattr(from_socket, 'index') and from_socket.index == 1:
                return True

        # Для других типов нод пытаемся пройти по цепочке
        walker = from_node
        visited = set()
        while walker and walker not in visited:
            visited.add(walker)
            if walker.type == 'TEX_IMAGE':
                # Нашли текстуру, проверяем через какой output она подключена
                for output in walker.outputs:
                    if output.links:
                        for out_link in output.links:
                            if out_link.to_node == from_node:
                                if output.name.lower() in ['alpha', 'a']:
                                    return True
                                elif hasattr(output, 'index') and output.index == 1:
                                    return True

            # Продолжаем искать вверх по цепочке
            inps = [s for s in walker.inputs if hasattr(s, 'links') and s.links]
            if inps:
                walker = inps[0].links[0].from_node
            else:
                break

        return False

    def get_source_image_and_channel_for_slot(self, principled_node, slot_name):
        """Возвращает (image, channel_index) для слота Principled.
        channel_index: 0=R, 1=G, 2=B, None если не удалось определить конкретный канал.
        Поддерживает сразу кейс Separate Color/Separate RGB → Roughness/Metallic/Emission Strength.
        """
        slot = principled_node.inputs.get(slot_name)
        if not slot or not slot.links:
            return None, None
        link = slot.links[0]
        from_node = link.from_node
        from_socket = link.from_socket
        # Separate Color / Separate RGB
        if from_node.type in {'SEPARATE_COLOR', 'SEPRGB'}:
            color_input = from_node.inputs.get('Image') or from_node.inputs.get('Color')
            src_img = None
            if color_input and color_input.links:
                src_node = color_input.links[0].from_node
                if src_node.type == 'TEX_IMAGE' and getattr(src_node, 'image', None):
                    src_img = src_node.image
            ch_name = from_socket.name.lower()
            if 'red' in ch_name:
                return src_img, 0
            if 'green' in ch_name:
                return src_img, 1
            if 'blue' in ch_name:
                return src_img, 2
            return src_img, None
        # Прямой TEX_IMAGE
        if from_node.type == 'TEX_IMAGE' and getattr(from_node, 'image', None):
            return from_node.image, None
        # Попытка пройти по цепочке вверх до текстуры
        walker = from_node
        visited = set()
        while walker and walker not in visited:
            visited.add(walker)
            inps = [s for s in walker.inputs if hasattr(s, 'links') and s.links]
            next_walker = None
            for s in inps:
                src = s.links[0].from_node
                if src.type == 'TEX_IMAGE' and getattr(src, 'image', None):
                    return src.image, None
                if src.type in {'MIX', 'MIX_RGB', 'HUE_SAT', 'BRIGHTCONTRAST', 'RGBCURVES', 'VALTORGB', 'MATH', 'VECT_MATH', 'NORMAL_MAP', 'BUMP', 'SEPARATE_COLOR', 'SEPRGB'}:
                    next_walker = src
                    break
            walker = next_walker
        return None, None

    def is_solid_color_image(self, image):
        """Проверяет, является ли изображение одноцветным (заглушкой)"""
        try:
            pixels = image.pixels[:]
            if not pixels:
                return False, [0, 0, 0, 1]

            # Берем первый пиксель как эталон
            first_pixel = pixels[:4]
            tolerance = 0.001  # Допуск для сравнения

            # Проверяем все пиксели
            for i in range(0, len(pixels), 4):
                pixel = pixels[i:i+4]
                if len(pixel) < 4:
                    continue

                # Сравниваем с эталонным пикселем
                if not all(abs(a - b) <= tolerance for a, b in zip(first_pixel, pixel)):
                    return False, first_pixel

            return True, first_pixel
        except Exception:
            return False, [0, 0, 0, 1]

    def resize_texture_if_needed(self, source_image, target_resolution, force_resize_256=False):
        """Изменяет размер текстуры если нужно
        force_resize_256: игнорировать настройку resize_textures_256 и всегда ресайзить 256px текстуры
        """
        # Не изменяем размер, если исходное изображение уже target_resolution
        if source_image.size[0] == target_resolution and source_image.size[1] == target_resolution:
            return source_image

        # Проверяем, является ли изображение одноцветным
        is_solid, solid_color = self.is_solid_color_image(source_image)

        if is_solid:
            # Для одноцветных изображений просто создаем новое нужного размера
            print(f"🎨 Одноцветная текстура {source_image.name}: {solid_color} → создаем {target_resolution}x{target_resolution}")

            # Создаем новое изображение
            resized_image = bpy.data.images.new(
                name=f"{source_image.name}_resized",
                width=target_resolution,
                height=target_resolution,
                alpha=source_image.channels == 4
            )
            resized_image.colorspace_settings.name = 'sRGB'

            # Заполняем тем же цветом
            pixels = solid_color * (target_resolution * target_resolution)
            try:
                resized_image.pixels.foreach_set(pixels)
            except Exception:
                resized_image.pixels = pixels

            return resized_image

        # Проверяем настройку resize_textures_256 для текстур 256x256
        if source_image.size[0] == 256 and source_image.size[1] == 256:
            if force_resize_256:
                print(f"🔄 Принудительный ресайз текстуры 256x256: {source_image.name} (force_resize_256=True)")
            else:
                # Проверяем настройку сцены только если force_resize_256 не установлен
                if not bpy.context.scene.resize_textures_256:
                    print(f"✅ Пропускаем ресайз текстуры 256x256: {source_image.name} (настройка отключена)")
                    return source_image
                else:
                    print(f"🔄 Разрешен ресайз текстуры 256x256: {source_image.name} (настройка включена)")
        
        resized_image = bpy.data.images.new(
            name=f"{source_image.name}_resized",
            width=target_resolution,
            height=target_resolution
        )
        
        import numpy as np
        
        source_pixels = np.array(source_image.pixels[:]).reshape(source_image.size[1], source_image.size[0], 4)
        
        scale_x = target_resolution / source_image.size[0]  
        scale_y = target_resolution / source_image.size[1]
        
        try:
            if SCIPY_AVAILABLE:
                # Используем scipy для качественного ресайза
                print(f"🔬 Качественный ресайз через SciPy: {source_image.size[0]}x{source_image.size[1]} → {target_resolution}x{target_resolution}")
                target_pixels = ndimage.zoom(source_pixels, (scale_y, scale_x, 1), order=1)
            else:
                # Fallback: простой ресайз через numpy
                print(f"⚠️ Простой ресайз через numpy: {source_image.size[0]}x{source_image.size[1]} → {target_resolution}x{target_resolution}")
                target_pixels = np.zeros((target_resolution, target_resolution, 4), dtype=np.float32)
                
                for y in range(target_resolution):
                    for x in range(target_resolution):
                        src_x = int(x / scale_x)
                        src_y = int(y / scale_y)
                        src_x = min(src_x, source_image.size[0] - 1)
                        src_y = min(src_y, source_image.size[1] - 1)
                        target_pixels[y, x] = source_pixels[src_y, src_x]
            
            # Защита: проверяем что размеры массива соответствуют ожидаемым
            expected_shape = (target_resolution, target_resolution, 4)
            if target_pixels.shape != expected_shape:
                print(f"⚠️ Несоответствие формы массива пикселей: ожидается {expected_shape}, получено {target_pixels.shape}")
                # Создаём новый массив с правильными размерами
                corrected_pixels = np.zeros(expected_shape, dtype=np.float32)
                # Заполняем цветом источника (средний цвет) или белым
                avg_color = np.mean(source_pixels, axis=(0, 1))
                corrected_pixels[:, :] = avg_color
                target_pixels = corrected_pixels
            
            # Дополнительная проверка размера перед установкой пикселей
            expected_pixel_count = target_resolution * target_resolution * 4
            actual_pixel_count = target_pixels.size
            
            if actual_pixel_count != expected_pixel_count:
                print(f"⚠️ Несоответствие количества пикселей: ожидается {expected_pixel_count}, получено {actual_pixel_count}")
                # Создаём корректный массив пикселей
                flat_pixels = target_pixels.flatten()
                if len(flat_pixels) > expected_pixel_count:
                    flat_pixels = flat_pixels[:expected_pixel_count]
                elif len(flat_pixels) < expected_pixel_count:
                    # Дополняем недостающие пиксели
                    missing = expected_pixel_count - len(flat_pixels)
                    flat_pixels = np.concatenate([flat_pixels, np.ones(missing, dtype=np.float32)])
            else:
                flat_pixels = target_pixels.flatten()
            
            try:
                resized_image.pixels.foreach_set(flat_pixels.tolist())
            except Exception:
                resized_image.pixels = flat_pixels.tolist()
                
        except Exception as e:
            print(f"❌ Ошибка при ресайзе текстуры: {e}")
            # Fallback: создаём белую текстуру нужного размера
            fallback_pixels = [1.0, 1.0, 1.0, 1.0] * (target_resolution * target_resolution)
            try:
                resized_image.pixels.foreach_set(fallback_pixels)
            except Exception:
                resized_image.pixels = fallback_pixels
        
        return resized_image

    def create_diffuse_from_material(self, principled_node, target_image, resolution):
        """Создает диффузную текстуру из материала - использует подключенную текстуру или запекает значение"""
        
        print(f"🎨 Создание диффузной текстуры...")
        
        texture, value = self.get_texture_or_value_from_slot(principled_node, 'Base Color')
        
        if texture:
            print(f"✅ Найдена подключенная диффузная текстура: {texture.name}")
            
            has_alpha = self.texture_has_alpha_channel(texture)
            
            # Проверяем нужно ли изменение размера с учетом настройки resize_textures_256
            should_resize = False
            if texture.size[0] != resolution or texture.size[1] != resolution:
                if texture.size[0] == 256 and texture.size[1] == 256:
                    # Текстура 256x256 - проверяем настройку resize_textures_256
                    should_resize = self.resize_textures_256
                else:
                    # Не 256x256 - всегда изменяем размер
                    should_resize = True
            
            if should_resize:
                print(f"🔄 Изменение размера: {texture.size[0]}x{texture.size[1]} → {resolution}x{resolution}")
                temp_texture = bpy.data.images.new(
                    name=f"temp_diffuse_{resolution}", 
                    width=resolution, 
                    height=resolution,
                    alpha=has_alpha
                )
                temp_texture.colorspace_settings.name = 'sRGB'
                
                resized_texture = self.resize_texture_if_needed(texture, resolution, force_resize_256=self.resize_textures_256)
                
                # Защита от несоответствия размеров пикселей
                expected_size = temp_texture.size[0] * temp_texture.size[1] * 4
                actual_size = len(resized_texture.pixels[:])
                
                if actual_size != expected_size:
                    print(f"⚠️ Несоответствие размера пикселей диффузной: ожидается {expected_size}, получено {actual_size}")
                    print(f"   Исходное разрешение: {texture.size[0]}x{texture.size[1]}")
                    print(f"   Целевое разрешение: {temp_texture.size[0]}x{temp_texture.size[1]}")
                    
                    # Создаём дефолтную диффузную текстуру
                    default_color = value if value else [0.8, 0.8, 0.8]
                    default_diffuse_pixels = []
                    for _ in range(temp_texture.size[0] * temp_texture.size[1]):
                        default_diffuse_pixels.extend([default_color[0], default_color[1], default_color[2], 1.0])
                    
                    temp_texture.pixels = default_diffuse_pixels
                else:
                    temp_texture.pixels = resized_texture.pixels[:]
                
                if resized_texture.name.endswith('_resized'):
                    bpy.data.images.remove(resized_texture)
                
                source_texture = temp_texture
            else:
                print(f"✅ Размер подходит: {texture.size[0]}x{texture.size[1]}")
                source_texture = texture
            
            # Проверяем, подключена ли альфа в слот ALPHA
            alpha_connected = self.is_alpha_connected_to_principled(principled_node)

            if has_alpha and alpha_connected:
                print(f"📢 ВАЖНО: Альфа подключена в слот ALPHA, сохраняем как DIFFUSE_OPACITY с альфа-каналом для последующего извлечения!")
            elif has_alpha and not alpha_connected:
                print(f"📝 Альфа не подключена в слот ALPHA, сохраняем как DIFFUSE без альфа-канала")
            else:
                print(f"📝 Текстура без альфа-канала, сохраняем как DIFFUSE")

            if has_alpha and alpha_connected:
                
                # Защита от несоответствия размеров при записи в target_image
                expected_size = target_image.size[0] * target_image.size[1] * 4
                actual_size = len(source_texture.pixels[:])
                
                if actual_size != expected_size:
                    print(f"⚠️ Несоответствие размера пикселей при записи DIFFUSE_OPACITY: ожидается {expected_size}, получено {actual_size}")
                    print(f"   Исходное разрешение: {source_texture.size[0]}x{source_texture.size[1]}")
                    print(f"   Целевое разрешение: {target_image.size[0]}x{target_image.size[1]}")
                    
                    # Создаём дефолтную диффузную текстуру с альфой
                    default_color = value if value else [0.8, 0.8, 0.8]
                    default_diffuse_opacity_pixels = []
                    for _ in range(target_image.size[0] * target_image.size[1]):
                        default_diffuse_opacity_pixels.extend([default_color[0], default_color[1], default_color[2], 1.0])
                    
                    target_image.pixels = default_diffuse_opacity_pixels
                else:
                    target_image.pixels = source_texture.pixels[:]
                    
                return True
            else:
                print(f"📝 Сохраняем как DIFFUSE без альфа-канала")
                src_w, src_h = source_texture.size
                
                # Защита от несоответствия размеров при записи в target_image
                expected_size = target_image.size[0] * target_image.size[1] * 4
                actual_size = len(source_texture.pixels[:])
                
                if actual_size != expected_size and (src_w != resolution or src_h != resolution):
                    print(f"⚠️ Несоответствие размера пикселей диффузной при записи: ожидается {expected_size}, получено {actual_size}")
                    print(f"   Исходное разрешение: {src_w}x{src_h}")
                    print(f"   Целевое разрешение: {resolution}x{resolution}")
                    
                    # Создаём дефолтную диффузную текстуру
                    default_color = value if value else [0.8, 0.8, 0.8]
                    default_diffuse_pixels = []
                    for _ in range(target_image.size[0] * target_image.size[1]):
                        default_diffuse_pixels.extend([default_color[0], default_color[1], default_color[2], 1.0])
                    
                    target_image.pixels = default_diffuse_pixels
                elif src_w != resolution or src_h != resolution:
                    # Если источник меньше по размеру, чем target_image — пересэмплируем на лету в целевой размер
                    src = np.array(source_texture.pixels[:]).reshape(src_h, src_w, 4)
                    
                    if SCIPY_AVAILABLE:
                        # Используем scipy для качественного ресайза
                        print(f"🔬 Качественный ресайз диффузной через SciPy: {src_w}x{src_h} → {resolution}x{resolution}")
                        scale_x = resolution / src_w
                        scale_y = resolution / src_h
                        out = ndimage.zoom(src, (scale_y, scale_x, 1), order=1)
                        # Принудительно устанавливаем альфа=1.0 для диффузной
                        out[:, :, 3] = 1.0
                    else:
                        # Fallback: простой ресайз
                        print(f"⚠️ Простой ресайз диффузной через numpy: {src_w}x{src_h} → {resolution}x{resolution}")
                        out = np.zeros((resolution, resolution, 4), dtype=np.float32)
                        scale_x = resolution / src_w
                        scale_y = resolution / src_h
                        for y in range(resolution):
                            sy = min(int(y / scale_y), src_h - 1)
                            for x in range(resolution):
                                sx = min(int(x / scale_x), src_w - 1)
                                r, g, b, _a = src[sy, sx]
                                out[y, x] = (r, g, b, 1.0)
                    try:
                        target_image.pixels.foreach_set(out.flatten().tolist())
                    except Exception:
                        target_image.pixels = out.flatten().tolist()
                else:
                    source_pixels = source_texture.pixels[:]
                    diffuse_pixels = []
                    for i in range(0, len(source_pixels), 4):
                        diffuse_pixels.extend([
                            source_pixels[i],     # R
                            source_pixels[i + 1], # G
                            source_pixels[i + 2], # B
                            1.0                   # A = 1.0
                        ])
                    target_image.pixels = diffuse_pixels
            
            if source_texture != texture and source_texture.name.startswith('temp_diffuse_'):
                bpy.data.images.remove(source_texture)
                
            return False
            
        else:
            print(f"📝 Нет подключенной текстуры, запекаем значение цвета через рендер-пайплайн...")
            
            old_engine = bpy.context.scene.render.engine
            old_samples = bpy.context.scene.cycles.samples
            old_pixel_filter_type = bpy.context.scene.cycles.pixel_filter_type if hasattr(bpy.context.scene.cycles, 'pixel_filter_type') else 'GAUSSIAN'
            old_use_denoising = bpy.context.scene.cycles.use_denoising
            old_active_object = bpy.context.active_object
            old_selected_objects = [obj for obj in bpy.context.selected_objects]
            old_world = bpy.context.scene.world
            old_mode = bpy.context.mode  # Сохраняем текущий режим
            
            try:
                # Переключаемся в Object Mode если находимся в Edit Mode
                if bpy.context.mode == 'EDIT_MESH':
                    bpy.ops.object.mode_set(mode='OBJECT')
                    print(f"🔄 Переключились из Edit Mode в Object Mode для запекания")
                
                bpy.context.scene.render.engine = 'CYCLES'
                bpy.context.scene.cycles.samples = 1
                bpy.context.scene.cycles.device = 'CPU'
                if hasattr(bpy.context.scene.cycles, 'pixel_filter_type'):
                    bpy.context.scene.cycles.pixel_filter_type = 'BOX'
                bpy.context.scene.cycles.use_denoising = False
                
                print(f"🔧 Генерация материала: engine=CYCLES, samples=1, device=CPU (принудительно)")
                
                bake_world = bpy.data.worlds.new("temp_bake_world")
                bpy.context.scene.world = bake_world
                
                bake_world.use_nodes = True
                world_nodes = bake_world.node_tree.nodes
                world_links = bake_world.node_tree.links
                
                for node in world_nodes:
                    world_nodes.remove(node)
                
                background = world_nodes.new('ShaderNodeBackground')
                background.inputs['Color'].default_value = (1, 1, 1, 1)
                background.inputs['Strength'].default_value = 1
                
                world_output = world_nodes.new('ShaderNodeOutputWorld')
                world_links.new(background.outputs['Background'], world_output.inputs['Surface'])
                
                temp_material = bpy.data.materials.new(name="temp_diffuse_bake_material")
                temp_material.use_nodes = True
                nodes = temp_material.node_tree.nodes
                links = temp_material.node_tree.links
                
                for node in nodes:
                    nodes.remove(node)
                
                principled_bake = nodes.new('ShaderNodeBsdfPrincipled')
                principled_bake.location = (0, 0)
                
                material_output = nodes.new('ShaderNodeOutputMaterial')
                material_output.location = (300, 0)
                
                links.new(principled_bake.outputs['BSDF'], material_output.inputs['Surface'])
                
                principled_bake.inputs['Base Color'].default_value = principled_node.inputs['Base Color'].default_value
                color = principled_node.inputs['Base Color'].default_value
                print(f"🎨 Запекаем цвет: RGB({color[0]:.2f}, {color[1]:.2f}, {color[2]:.2f})")
                
                principled_bake.inputs['IOR'].default_value = 1.0
                
                temp_mesh = bpy.data.meshes.new("temp_diffuse_bake_mesh")
                temp_obj = bpy.data.objects.new("temp_diffuse_bake_obj", temp_mesh)
                bpy.context.scene.collection.objects.link(temp_obj)
                
                vertices = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
                faces = [(0, 1, 2, 3)]
                temp_mesh.from_pydata(vertices, [], faces)
                temp_mesh.update()
                
                temp_mesh.uv_layers.new(name="UVMap")
                uv_layer = temp_mesh.uv_layers.active.data
                uv_layer[0].uv = (0, 0)
                uv_layer[1].uv = (1, 0)
                uv_layer[2].uv = (1, 1)
                uv_layer[3].uv = (0, 1)
                
                temp_obj.data.materials.append(temp_material)
                
                texture_node = nodes.new('ShaderNodeTexImage')
                texture_node.image = target_image
                texture_node.location = (-300, -200)
                nodes.active = texture_node
                
                bpy.context.scene.render.bake.use_selected_to_active = False
                bpy.context.scene.render.bake.target = 'IMAGE_TEXTURES'
                bpy.context.scene.render.bake.margin = 0
                bpy.context.scene.render.bake.use_clear = True
                bpy.context.scene.render.bake.use_cage = False 
                
                bpy.ops.object.select_all(action='DESELECT')
                temp_obj.select_set(True)
                bpy.context.view_layer.objects.active = temp_obj
                
                bpy.ops.object.bake(type='DIFFUSE', pass_filter={'COLOR'})
                print(f"✅ Диффузная текстура запечена через рендер-пайплайн")
                
            except Exception as e:
                print(f"❌ Ошибка запекания диффузной текстуры: {e}")
                pixels = []
                color = principled_node.inputs['Base Color'].default_value[:3] if principled_node.inputs['Base Color'].default_value else [0.8, 0.8, 0.8]
                for _ in range(resolution * resolution):
                    pixels.extend([color[0], color[1], color[2], 1.0])
                target_image.pixels = pixels
                
            finally:
                if 'temp_diffuse_bake_obj' in bpy.data.objects:
                    bpy.data.objects.remove(bpy.data.objects['temp_diffuse_bake_obj'], do_unlink=True)
                if 'temp_diffuse_bake_mesh' in bpy.data.meshes:
                    bpy.data.meshes.remove(bpy.data.meshes['temp_diffuse_bake_mesh'])
                if 'temp_diffuse_bake_material' in bpy.data.materials:
                    bpy.data.materials.remove(bpy.data.materials['temp_diffuse_bake_material'])
                if 'temp_bake_world' in bpy.data.worlds:
                    bpy.data.worlds.remove(bpy.data.worlds['temp_bake_world'])
                
                bpy.context.scene.render.engine = old_engine
                bpy.context.scene.cycles.samples = old_samples
                if hasattr(bpy.context.scene.cycles, 'pixel_filter_type'):
                    bpy.context.scene.cycles.pixel_filter_type = old_pixel_filter_type
                bpy.context.scene.cycles.use_denoising = old_use_denoising
                bpy.context.scene.world = old_world
                
                # Восстанавливаем выделение и активный объект
                if bpy.context.mode != 'OBJECT':
                    bpy.ops.object.mode_set(mode='OBJECT')
                bpy.ops.object.select_all(action='DESELECT')
                for obj in old_selected_objects:
                    if obj.name in bpy.data.objects:
                        obj.select_set(True)
                if old_active_object and old_active_object.name in bpy.data.objects:
                    bpy.context.view_layer.objects.active = old_active_object
                
                # Возвращаемся в изначальный режим
                if old_mode == 'EDIT_MESH' and bpy.context.active_object:
                    bpy.ops.object.mode_set(mode='EDIT')
                    print(f"🔄 Вернулись в Edit Mode")
        
        return False

    def create_channel_texture_from_material(self, principled_node, channel_name, target_image, resolution, default_value=0.0):
        """Создает одноканальную текстуру из материала с правильным выбором канала (R/G/B) из ERM.
        Для Roughness/Metallic/Emission Strength берём соответствующий канал из источника.
        """
        # Пытаемся определить исходное изображение и конкретный канал
        src_img, chan_idx = self.get_source_image_and_channel_for_slot(principled_node, channel_name)
        texture = None
        value = None
        if src_img is None:
            # Фоллбек на прежнюю логику
            texture, value = self.get_texture_or_value_from_slot(principled_node, channel_name)
            if texture:
                src_img = texture
        
        if src_img:
            # Проверяем нужно ли изменение размера с учетом настройки resize_textures_256
            should_resize = False
            if src_img.size[0] != resolution or src_img.size[1] != resolution:
                if src_img.size[0] == 256 and src_img.size[1] == 256:
                    # Текстура 256x256 - проверяем настройку resize_textures_256
                    should_resize = self.resize_textures_256
                else:
                    # Не 256x256 - всегда изменяем размер
                    should_resize = True
            
            if should_resize:
                print(f"🔄 Изменение размера {channel_name}: {src_img.size[0]}x{src_img.size[1]} → {resolution}x{resolution}")
                resized_texture = self.resize_texture_if_needed(src_img, resolution, force_resize_256=self.resize_textures_256)
                source_pixels = resized_texture.pixels[:]
                actual_resolution = resolution
                if resized_texture.name.endswith('_resized'):
                    bpy.data.images.remove(resized_texture)
            else:
                print(f"✅ Размер {channel_name} подходит: {src_img.size[0]}x{src_img.size[1]}")
                source_pixels = src_img.pixels[:]
                actual_resolution = src_img.size[0]
            
            # Определяем канал по умолчанию, если не удалось
            if chan_idx is None:
                if channel_name == 'Roughness':
                    chan_idx = 1  # G
                elif channel_name == 'Metallic':
                    chan_idx = 2  # B
                elif channel_name == 'Emission Strength':
                    chan_idx = 0  # R
                else:
                    chan_idx = 0  # безопасный дефолт
            
            out = []
            # Сдвиг для нужного канала в RGBA
            offset = chan_idx
            for i in range(0, len(source_pixels), 4):
                v = source_pixels[i + offset]
                out.extend([v, v, v, 1.0])
            
            # Проверяем, что размер выходного массива соответствует целевому изображению
            expected_size = target_image.size[0] * target_image.size[1] * 4
            if len(out) != expected_size:
                print(f"⚠️ Несоответствие размера пикселей: ожидается {expected_size}, получено {len(out)}")
                print(f"   Исходное разрешение: {src_img.size[0]}x{src_img.size[1]}")
                print(f"   Целевое разрешение: {target_image.size[0]}x{target_image.size[1]}")
                print(f"   Актуальное разрешение источника: {actual_resolution}x{actual_resolution}")
                
                # Если размеры не совпадают, создаём массив с дефолтным значением
                val = default_value if value is None else value
                out = [val, val, val, 1.0] * (target_image.size[0] * target_image.size[1])
            
            try:
                target_image.pixels.foreach_set(out)
            except Exception as e:
                print(f"❌ Ошибка при установке пикселей через foreach_set: {e}")
                try:
                    target_image.pixels = out
                except Exception as e2:
                    print(f"❌ Ошибка при прямом назначении пикселей: {e2}")
                    # Последняя попытка с дефолтным значением
                    val = default_value
                    default_pixels = [val, val, val, 1.0] * (target_image.size[0] * target_image.size[1])
                    target_image.pixels = default_pixels
        else:
            val = default_value if value is None else value
            out = [val, val, val, 1.0] * (target_image.size[0] * target_image.size[1])
            try:
                target_image.pixels.foreach_set(out)
            except Exception:
                target_image.pixels = out

    def create_normal_from_material(self, principled_node, target_image, resolution):
        """Создает карту нормалей из материала"""
        normal_texture = None
        
        if principled_node.inputs['Normal'].links:
            from_node = principled_node.inputs['Normal'].links[0].from_node
            
            if from_node.type == 'NORMAL_MAP' and from_node.inputs['Color'].links:
                tex_node = from_node.inputs['Color'].links[0].from_node
                if tex_node.type == 'TEX_IMAGE' and tex_node.image:
                    normal_texture = tex_node.image
            
            elif from_node.type == 'TEX_IMAGE' and from_node.image:
                normal_texture = from_node.image
        
        if normal_texture:
            # Правильная обработка ресайза с учётом правила 256
            if normal_texture.size[0] == 256 and normal_texture.size[1] == 256 and self.resize_textures_256:
                # Нужно увеличить с 256 до целевого разрешения
                resized_texture = self.resize_texture_if_needed(normal_texture, resolution, force_resize_256=self.resize_textures_256)
                source_pixels = resized_texture.pixels[:]
                actual_resolution = resolution
                if resized_texture.name.endswith('_resized'):
                    bpy.data.images.remove(resized_texture)
            elif normal_texture.size[0] != resolution or normal_texture.size[1] != resolution:
                # Обычный ресайз для других размеров
                resized_texture = self.resize_texture_if_needed(normal_texture, resolution, force_resize_256=self.resize_textures_256)
                source_pixels = resized_texture.pixels[:]
                actual_resolution = resolution
                if resized_texture.name.endswith('_resized'):
                    bpy.data.images.remove(resized_texture)
            else:
                # Размеры уже совпадают
                source_pixels = normal_texture.pixels[:]
                actual_resolution = normal_texture.size[0]
            
            # Проверяем, что размер выходного массива соответствует целевому изображению
            expected_size = target_image.size[0] * target_image.size[1] * 4
            if len(source_pixels) != expected_size:
                print(f"⚠️ Несоответствие размера пикселей нормали: ожидается {expected_size}, получено {len(source_pixels)}")
                print(f"   Исходное разрешение: {normal_texture.size[0]}x{normal_texture.size[1]}")
                print(f"   Целевое разрешение: {target_image.size[0]}x{target_image.size[1]}")
                print(f"   Актуальное разрешение источника: {actual_resolution}x{actual_resolution}")
                
                # Если размеры не совпадают, создаём массив с дефолтными значениями нормали
                default_normal_pixels = []
                for _ in range(target_image.size[0] * target_image.size[1]):
                    default_normal_pixels.extend([0.5, 0.5, 1.0, 1.0])
                source_pixels = default_normal_pixels
            
            try:
                target_image.pixels.foreach_set(source_pixels)
            except Exception as e:
                print(f"❌ Ошибка при установке пикселей нормали через foreach_set: {e}")
                try:
                    target_image.pixels = source_pixels
                except Exception as e2:
                    print(f"❌ Ошибка при прямом назначении пикселей нормали: {e2}")
                    # Последняя попытка с дефолтными значениями нормали
                    default_normal_pixels = []
                    for _ in range(target_image.size[0] * target_image.size[1]):
                        default_normal_pixels.extend([0.5, 0.5, 1.0, 1.0])
                    target_image.pixels = default_normal_pixels
        else:
            # Создаём дефолтную карту нормалей
            pixels = []
            for _ in range(resolution * resolution):
                pixels.extend([0.5, 0.5, 1.0, 1.0])
            try:
                target_image.pixels.foreach_set(pixels)
            except Exception:
                target_image.pixels = pixels

    def create_alpha_from_material(self, principled_node, target_image, resolution):
        """Создает карту прозрачности из материала"""

        print(f"🔍 Поиск альфа-канала в материале...")

        alpha_texture, alpha_value = self.get_texture_or_value_from_slot(principled_node, 'Alpha')

        if alpha_texture:
            # Определяем тип подключения текстуры
            connection_type = self.get_texture_connection_type(principled_node, 'Alpha')
            print(f"✅ Найдена текстура в слоте Alpha: {alpha_texture.name}")
            print(f"🔗 Тип подключения: {connection_type}")

            if connection_type == 'alpha':
                # Текущая логика - извлекаем альфа-канал
                print(f"🎯 Обработка как альфа-канал")
                source_texture = alpha_texture
                use_alpha_channel = True
            elif connection_type == 'color':
                # Новая логика - используем как OPACITY для объединения с DIFFUSE
                print(f"🎯 Обработка как OPACITY текстура")
                source_texture = alpha_texture
                use_alpha_channel = False  # Не извлекаем альфа, используем как есть
            else:
                # Неизвестный тип - используем как альфа
                print(f"⚠️ Неизвестный тип подключения, обрабатываем как альфа")
                source_texture = alpha_texture
                use_alpha_channel = True
        else:
            # Проверяем, подключена ли альфа в слот ALPHA
            alpha_connected = self.is_alpha_connected_to_principled(principled_node)

            if alpha_connected:
                # Альфа подключена - ищем диффузную текстуру с альфа-каналом
                diffuse_texture, _ = self.get_texture_or_value_from_slot(principled_node, 'Base Color')

                if diffuse_texture and self.texture_has_alpha_channel(diffuse_texture):
                    print(f"✅ Альфа подключена в слот ALPHA, найден альфа-канал в диффузной текстуре: {diffuse_texture.name}")
                    source_texture = diffuse_texture
                    use_alpha_channel = True
                else:
                    source_texture = None
                    use_alpha_channel = False
                    if diffuse_texture:
                        print(f"ℹ️  Диффузная текстура без альфы: {diffuse_texture.name}")
                    else:
                        print(f"ℹ️  Диффузная текстура не найдена")
            else:
                # Альфа НЕ подключена - не извлекаем альфа из диффузной текстуры
                print(f"ℹ️  Альфа не подключена в слот ALPHA, пропускаем извлечение альфа-канала из диффузной текстуры")
                source_texture = None
                use_alpha_channel = False
        
        if source_texture and use_alpha_channel:
            # Проверяем размер и изменяем при необходимости
            if source_texture.size[0] == 256 and source_texture.size[1] == 256 and resolution != 256 and not self.resize_textures_256:
                # Специальная обработка 256px альфы без ресайза
                print(f"🎯 Специальная обработка 256px альфы: {source_texture.name}")
                source_pixels = source_texture.pixels[:]
                actual_resolution = 256
            elif source_texture.size[0] != resolution or source_texture.size[1] != resolution:
                # Обычный ресайз для других размеров
                resized_texture = self.resize_texture_if_needed(source_texture, resolution, force_resize_256=self.resize_textures_256)
                source_pixels = resized_texture.pixels[:]
                actual_resolution = resolution
                if resized_texture.name.endswith('_resized'):
                    bpy.data.images.remove(resized_texture)
            else:
                # Размеры уже совпадают
                source_pixels = source_texture.pixels[:]
                actual_resolution = source_texture.size[0]

            # Проверяем, что размер выходного массива соответствует целевому изображению
            expected_size = target_image.size[0] * target_image.size[1] * 4
            if len(source_pixels) != expected_size:
                print(f"⚠️ Несоответствие размера пикселей альфы: ожидается {expected_size}, получено {len(source_pixels)}")
                print(f"   Исходное разрешение: {source_texture.size[0]}x{source_texture.size[1]}")
                print(f"   Целевое разрешение: {target_image.size[0]}x{target_image.size[1]}")
                print(f"   Актуальное разрешение источника: {actual_resolution}x{actual_resolution}")

                # Если размеры не совпадают, заливаем дефолтным значением альфы
                alpha_val = 1.0
                default_alpha_pixels = []
                for _ in range(target_image.size[0] * target_image.size[1]):
                    default_alpha_pixels.extend([alpha_val, alpha_val, alpha_val, 1.0])

                try:
                    target_image.pixels.foreach_set(default_alpha_pixels)
                except Exception:
                    target_image.pixels = default_alpha_pixels

                print(f"📝 Заливка альфы дефолтным значением из-за несоответствия размеров: {alpha_val}")
                return

            # Извлекаем альфа-канал и создаем RGB пиксели
            pixels = []
            alpha_values = []
            for i in range(0, len(source_pixels), 4):
                alpha_value = source_pixels[i + 3]
                alpha_values.append(alpha_value)
                pixels.extend([alpha_value, alpha_value, alpha_value, 1.0])

            try:
                target_image.pixels.foreach_set(pixels)
            except Exception as e:
                print(f"❌ Ошибка при установке пикселей альфы через foreach_set: {e}")
                try:
                    target_image.pixels = pixels
                except Exception as e2:
                    print(f"❌ Ошибка при прямом назначении пикселей альфы: {e2}")
                    # Последняя попытка с дефолтными значениями альфы
                    alpha_val = 1.0
                    default_alpha_pixels = []
                    for _ in range(target_image.size[0] * target_image.size[1]):
                        default_alpha_pixels.extend([alpha_val, alpha_val, alpha_val, 1.0])
                    target_image.pixels = default_alpha_pixels

            min_alpha = min(alpha_values) if alpha_values else 1.0
            max_alpha = max(alpha_values) if alpha_values else 1.0
            print(f"📊 Извлечен альфа-канал: мин={min_alpha:.3f}, макс={max_alpha:.3f}")

        elif source_texture and not use_alpha_channel:
            # Новая логика: текстура подключена через Color output - используем как OPACITY
            print(f"🎨 Используем текстуру как OPACITY без извлечения альфа-канала")

            if source_texture.size[0] != resolution or source_texture.size[1] != resolution:
                # Ресайзим текстуру до нужного размера
                resized_texture = self.resize_texture_if_needed(source_texture, resolution, force_resize_256=self.resize_textures_256)
                source_pixels = resized_texture.pixels[:]
                if resized_texture.name.endswith('_resized'):
                    bpy.data.images.remove(resized_texture)
            else:
                source_pixels = source_texture.pixels[:]

            # Проверяем размер
            expected_size = target_image.size[0] * target_image.size[1] * 4
            if len(source_pixels) != expected_size:
                print(f"⚠️ Несоответствие размера пикселей OPACITY: ожидается {expected_size}, получено {len(source_pixels)}")
                # Заливаем дефолтным значением
                alpha_val = 1.0
                default_pixels = []
                for _ in range(target_image.size[0] * target_image.size[1]):
                    default_pixels.extend([alpha_val, alpha_val, alpha_val, 1.0])
                target_image.pixels = default_pixels
                print(f"📝 Заливка OPACITY дефолтным значением: {alpha_val}")
                return

            # Используем текстуру как есть (бинаризируем RGB в 0 или 1)
            pixels = []
            opacity_values = []
            for i in range(0, len(source_pixels), 4):
                r = source_pixels[i]
                g = source_pixels[i + 1]
                b = source_pixels[i + 2]
                # Вычисляем среднее RGB и бинаризируем: > 0.5 → 1, иначе → 0
                avg_rgb = (r + g + b) / 3.0
                opacity_value = 1.0 if avg_rgb > 0.5 else 0.0
                opacity_values.append(opacity_value)
                pixels.extend([opacity_value, opacity_value, opacity_value, 1.0])

            try:
                target_image.pixels.foreach_set(pixels)
            except Exception as e:
                print(f"❌ Ошибка при установке пикселей OPACITY через foreach_set: {e}")
                target_image.pixels = pixels

            # Для бинарных значений считаем количество пикселей с opacity 1.0
            ones_count = sum(1 for v in opacity_values if v == 1.0)
            total_count = len(opacity_values)
            opacity_ratio = ones_count / total_count if total_count > 0 else 0
            print(f"📊 Создана бинарная OPACITY из Color: {ones_count}/{total_count} пикселей с opacity=1.0 ({opacity_ratio:.1%})")

        else:
            alpha_val = alpha_value if alpha_value is not None else 1.0
            pixels = []
            for _ in range(resolution * resolution):
                pixels.extend([alpha_val, alpha_val, alpha_val, 1.0])
            try:
                target_image.pixels.foreach_set(pixels)
            except Exception:
                target_image.pixels = pixels
            print(f"📝 Заливка альфы значением: {alpha_val}")

    def texture_has_alpha_channel(self, texture):
        """Проверяет есть ли значимый альфа-канал в текстуре"""
        if not texture or not texture.has_data:
            return False
        
        try:
            pixels = texture.pixels[:]
            if len(pixels) == 0:
                return False
            
            alpha_values = [pixels[i + 3] for i in range(0, len(pixels), 4)]
            
            if not alpha_values:
                return False
            
            min_alpha = min(alpha_values)
            max_alpha = max(alpha_values)
            
            has_transparency = min_alpha < 0.99
            has_variation = (max_alpha - min_alpha) > 0.01
            
            result = has_transparency or has_variation
            
            print(f"🔍 Проверка альфы в {texture.name}: мин={min_alpha:.3f}, макс={max_alpha:.3f} → {'Есть' if result else 'Нет'}")
            
            return result
            
        except Exception as e:
            print(f"⚠️ Ошибка проверки альфы в {texture.name}: {e}")
            return False

    def combine_diffuse_and_alpha(self, diffuse_img, alpha_img, target_img, resolution):
        """Объединяет диффузную карту и альфу в DIFFUSE_OPACITY"""
        diffuse_pixels = diffuse_img.pixels[:]
        alpha_pixels = alpha_img.pixels[:]
        
        print(f"🔄 Объединяем DIFFUSE + ALPHA:")
        print(f"   Диффузная: {len(diffuse_pixels)//4} пикселей")
        print(f"   Альфа: {len(alpha_pixels)//4} пикселей")
        
        combined_pixels = []
        for i in range(0, len(diffuse_pixels), 4):
            combined_pixels.extend([
                diffuse_pixels[i],      # R
                diffuse_pixels[i + 1],  # G  
                diffuse_pixels[i + 2],  # B
                alpha_pixels[i]         # A
            ])
        
        target_img.pixels = combined_pixels
        
        final_alpha_values = [combined_pixels[i + 3] for i in range(0, len(combined_pixels), 4)]
        min_alpha = min(final_alpha_values) if final_alpha_values else 1.0
        max_alpha = max(final_alpha_values) if final_alpha_values else 1.0
        print(f"✅ DIFFUSE_OPACITY создана: альфа от {min_alpha:.3f} до {max_alpha:.3f}")

    def create_erm_from_textures(self, emit_img, roughness_img, metallic_img, target_img, resolution):
        """Создаёт ERM текстуру. Если источники отдельно — собираем из них.
        Если в материале была подключена готовая ERM через Separate Color — это уже учтено при создании карт emit/roughness/metallic.
        """
        try:
            # Сохраняем оригинальные изображения для очистки
            original_emit = emit_img
            original_roughness = roughness_img
            original_metallic = metallic_img

            # Принудительно ресайзим текстуры до нужного размера (игнорируя настройку resize_textures_256)
            if emit_img.size[0] != resolution or emit_img.size[1] != resolution:
                print(f"🔄 ERM: Ресайз emit текстуры {emit_img.size[0]}x{emit_img.size[1]} → {resolution}x{resolution}")
                emit_img = self.resize_texture_if_needed(emit_img, resolution, force_resize_256=True)

            if roughness_img.size[0] != resolution or roughness_img.size[1] != resolution:
                print(f"🔄 ERM: Ресайз roughness текстуры {roughness_img.size[0]}x{roughness_img.size[1]} → {resolution}x{resolution}")
                roughness_img = self.resize_texture_if_needed(roughness_img, resolution, force_resize_256=True)

            if metallic_img.size[0] != resolution or metallic_img.size[1] != resolution:
                print(f"🔄 ERM: Ресайз metallic текстуры {metallic_img.size[0]}x{metallic_img.size[1]} → {resolution}x{resolution}")
                metallic_img = self.resize_texture_if_needed(metallic_img, resolution, force_resize_256=True)

            emit_pixels = emit_img.pixels[:]
            roughness_pixels = roughness_img.pixels[:]
            metallic_pixels = metallic_img.pixels[:]

            # Очищаем временные изображения
            def cleanup_temp_image(img, original_img):
                if img != original_img and img.name.endswith('_resized'):
                    print(f"🧹 ERM: Очищаем временное изображение: {img.name}")
                    bpy.data.images.remove(img)

            cleanup_temp_image(emit_img, original_emit)
            cleanup_temp_image(roughness_img, original_roughness)
            cleanup_temp_image(metallic_img, original_metallic)
            erm_pixels = []
            for i in range(0, len(emit_pixels), 4):
                erm_pixels.extend([
                    emit_pixels[i],      # R = Emission Strength
                    roughness_pixels[i], # G = Roughness
                    metallic_pixels[i],  # B = Metallic
                    1.0
                ])
            target_img.pixels = erm_pixels
        except Exception:
            # безопасный fallback
            target_img.pixels = [0.0, 0.0, 0.0, 1.0] * (resolution * resolution)

    def refresh_texture_sets_list(self, context):
        """Обновляет список наборов текстур"""
        scene = context.scene
        texture_sets = scene.baker_texture_sets
        
        indices_to_remove = []
        
        for i, tex_set in enumerate(texture_sets):
            if not os.path.exists(tex_set.output_path):
                indices_to_remove.append(i)
                continue
                
            base_path = os.path.join(tex_set.output_path, tex_set.name)
            has_any_texture = any(os.path.exists(f"{base_path}_{suffix}.png") 
                                for suffix in ["DIFFUSE", "DIFFUSE_OPACITY", "NORMAL", "NORMAL_DIRECTX", 
                                             "ROUGHNESS", "METALLIC", "EMIT", "OPACITY", "ERM"])
            
            if not has_any_texture:
                indices_to_remove.append(i)
        
        for i in reversed(indices_to_remove):
            texture_sets.remove(i)
    
    def create_texture_image(self, name, resolution, with_alpha=False):
        """Создает новое изображение для текстуры"""
        if name in bpy.data.images:
            existing_image = bpy.data.images[name]
            counter = 1
            new_name = f"{name}.{counter:03d}"
            while new_name in bpy.data.images:
                counter += 1
                new_name = f"{name}.{counter:03d}"
            existing_image.name = new_name
            print(f"🔄 Переименован существующий образ: {name} → {new_name}")
        
        image = bpy.data.images.new(
            name,
            width=resolution,
            height=resolution,
            alpha=with_alpha,
            float_buffer=False
        )
        image.colorspace_settings.name = 'sRGB'
        
        total = resolution * resolution
        fill = [1.0, 1.0, 1.0, 1.0]
        buf = fill * total
        try:
            image.pixels.foreach_set(buf)
        except Exception:
            image.pixels = buf
        _ = image.pixels[0]
        
        return image
    
    def save_texture(self, image, filepath):
        """Сохраняет текстуру на диск (используя тот же метод что и при запекании)"""
        image.update()
        
        scene = bpy.context.scene
        
        original_format = scene.render.image_settings.file_format
        original_color_mode = scene.render.image_settings.color_mode
        original_color_depth = scene.render.image_settings.color_depth
        original_view_settings = scene.view_settings.view_transform
        original_look = scene.view_settings.look
        original_display_device = scene.display_settings.display_device
        original_filepath = image.filepath
        
        scene.render.image_settings.file_format = 'PNG'
        scene.render.image_settings.compression = 15
        scene.render.image_settings.color_depth = '8'
        
        should_save_alpha = self.should_save_with_alpha(image, filepath)
        
        if should_save_alpha:
            scene.render.image_settings.color_mode = 'RGBA'
            save_mode = 'RGBA'
            print(f"📝 Сохраняем с альфа-каналом: {os.path.basename(filepath)}")
        else:
            scene.render.image_settings.color_mode = 'RGB'
            save_mode = 'RGB'
            print(f"📝 Сохраняем без альфа-канала: {os.path.basename(filepath)}")
        
        scene.view_settings.view_transform = 'Standard'
        scene.view_settings.look = 'None'
        scene.display_settings.display_device = 'sRGB'
        
        image.filepath_raw = filepath
        
        try:
            image.save_render(filepath)
            print(f"✅ Сохранена текстура: {filepath} (режим: {save_mode})")
        except Exception as e:
            print(f"❌ Ошибка сохранения {filepath}: {e}")
            try:
                image.save_render(filepath, scene=scene)
                print(f"✅ Сохранена текстура (повторная попытка): {filepath}")
            except Exception as e2:
                print(f"❌ Критическая ошибка сохранения {filepath}: {e2}")
        finally:
            image.filepath = original_filepath
            scene.render.image_settings.file_format = original_format
            scene.render.image_settings.color_mode = original_color_mode
            scene.render.image_settings.color_depth = original_color_depth
            scene.view_settings.view_transform = original_view_settings
            scene.view_settings.look = original_look
            scene.display_settings.display_device = original_display_device
    
    def should_save_with_alpha(self, image, filepath):
        """Определяет нужно ли сохранять изображение с альфа-каналом"""
        
        if "DIFFUSE_OPACITY" in os.path.basename(filepath):
            return self.has_meaningful_alpha_channel(image)
        
        return False
    
    def has_meaningful_alpha_channel(self, image):
        """Проверяет есть ли значимый альфа-канал в изображении"""
        if not image.has_data:
            return False
        
        pixels = image.pixels[:]
        if len(pixels) == 0:
            return False
        
        alpha_values = [pixels[i + 3] for i in range(0, len(pixels), 4)]
        
        if not alpha_values:
            return False
        
        min_alpha = min(alpha_values)
        max_alpha = max(alpha_values)
        
        has_transparency = min_alpha < 0.99
        
        has_variation = (max_alpha - min_alpha) > 0.01
        
        is_meaningful = has_transparency or has_variation
        
        print(f"🔍 Анализ альфа-канала {image.name}:")
        print(f"   Мин: {min_alpha:.3f}, Макс: {max_alpha:.3f}")
        print(f"   Прозрачность: {has_transparency}, Вариация: {has_variation}")
        print(f"   Результат: {'Значимый' if is_meaningful else 'Не значимый'}")
        
        return is_meaningful
    
    def convert_normal_to_directx(self, image):
        """Конвертирует нормаль из OpenGL в DirectX (инвертирует зеленый канал)"""
        pixels = list(image.pixels)
        
        for i in range(0, len(pixels), 4):
            if i + 1 < len(pixels):
                pixels[i + 1] = 1.0 - pixels[i + 1]
        
        image.pixels = pixels
    
    def connect_textures_to_material(self, material, diffuse_img, erm_img, normal_img, opacity_img, connection_mode, normal_type, output_path):
        """Подключает созданные текстуры к материалу (загружает с диска как в основном коде)"""
        material_name = material.name
        
        textures_to_remove = [
            f"T_{material_name}_DIFFUSE",
            f"T_{material_name}_ERM", 
            f"T_{material_name}_NORMAL",
            f"T_{material_name}_NORMAL_DIRECTX",
            f"T_{material_name}_OPACITY",
            f"T_{material_name}_ROUGHNESS",
            f"T_{material_name}_METALLIC", 
            f"T_{material_name}_EMIT",
            f"T_{material_name}_DIFFUSE_OPACITY"
        ]
        for texture_name in textures_to_remove:
            if texture_name in bpy.data.images:
                bpy.data.images.remove(bpy.data.images[texture_name])
        
        for img_name in list(bpy.data.images.keys()):
            if any(tex_name in img_name for tex_name in textures_to_remove):
                bpy.data.images.remove(bpy.data.images[img_name])
        
        "bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)"
        print(f"✅ Очистка памяти завершена")
        
        material.use_nodes = True
        nodes = material.node_tree.nodes
        links = material.node_tree.links
        
        nodes.clear()
        
        output = nodes.new(type='ShaderNodeOutputMaterial')
        bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
        
        output.location = (400, 0)
        bsdf.location = (100, 0)
        
        links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])

        def load_texture_from_disk(texture_name, label, location, colorspace='sRGB'):
            """Загружает текстуру с диска и создает узел"""
            texture_path = os.path.join(output_path, f"{texture_name}.png")
            if os.path.exists(texture_path):
                try:
                    if texture_name in bpy.data.images:
                        bpy.data.images.remove(bpy.data.images[texture_name])
                    
                    for img_name in list(bpy.data.images.keys()):
                        if texture_name in img_name:
                            bpy.data.images.remove(bpy.data.images[img_name])
                    
                    # Убираем очистку orphans, чтобы не удалять материалы
                # bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
                    
                    img = bpy.data.images.load(texture_path)
                    
                    img.name = texture_name
                    
                    img.filepath = texture_path
                    img.filepath_raw = texture_path
                    
                    img.colorspace_settings.name = colorspace
                    
                    img.reload()
                    img.update()
                    _ = img.pixels[0]
                    
                    if img.has_data:
                        pass
                    else:
                        img.reload()
                        img.update()
                        _ = img.pixels[0]
                        if not img.has_data:
                            return None
                    
                    tex_node = nodes.new(type='ShaderNodeTexImage')
                    tex_node.image = img
                    tex_node.location = location
                    tex_node.label = label
                    
                    nodes.active = tex_node
                    
                    return tex_node
                    
                except Exception as e:
                    print(f"❌ Ошибка загрузки текстуры {label}: {e}")
                    return None
            else:
                print(f"⚠️  Файл текстуры не найден: {texture_path}")
                return None
        
        if connection_mode == 'HIGH':
            print(f"🔧 Режим подключения: HIGH (ERM + DIFFUSE_OPACITY)")
            
            tex_diffuse_opacity = load_texture_from_disk(f"T_{material_name}_DIFFUSE_OPACITY", "Diffuse Opacity", (-700, 300), 'sRGB')
            if tex_diffuse_opacity:
                links.new(tex_diffuse_opacity.outputs['Color'], bsdf.inputs['Base Color'])
                links.new(tex_diffuse_opacity.outputs['Color'], bsdf.inputs['Emission Color'])
                links.new(tex_diffuse_opacity.outputs['Alpha'], bsdf.inputs['Alpha'])
            
            if normal_type == 'DIRECTX':
                normal_texture_name = f"T_{material_name}_NORMAL_DIRECTX"
            else:
                normal_texture_name = f"T_{material_name}_NORMAL"
                
            tex_normal = load_texture_from_disk(normal_texture_name, "Normal", (-700, 0), 'Non-Color')
            if tex_normal:
                connect_normal_map(nodes, links, tex_normal, bsdf, normal_type, (-400, 0))
            
            tex_erm = load_texture_from_disk(f"T_{material_name}_ERM", "ERM", (-700, -300), 'Non-Color')
            if tex_erm:
                separate_color = nodes.new(type='ShaderNodeSeparateColor')
                separate_color.location = (-400, -300)
                
                links.new(tex_erm.outputs['Color'], separate_color.inputs['Color'])

                links.new(separate_color.outputs['Red'], bsdf.inputs['Emission Strength'])
                links.new(separate_color.outputs['Green'], bsdf.inputs['Roughness'])
                links.new(separate_color.outputs['Blue'], bsdf.inputs['Metallic'])
                
        elif connection_mode == 'LOW':
            print(f"🔧 Режим подключения: LOW (отдельные карты)")
            
            tex_diffuse = load_texture_from_disk(f"T_{material_name}_DIFFUSE", "Diffuse", (-700, 400), 'sRGB')
            if tex_diffuse:
                links.new(tex_diffuse.outputs['Color'], bsdf.inputs['Base Color'])
            
            tex_metallic = load_texture_from_disk(f"T_{material_name}_METALLIC", "Metallic", (-700, 200), 'Non-Color')
            if tex_metallic:
                links.new(tex_metallic.outputs['Color'], bsdf.inputs['Metallic'])
            
            tex_roughness = load_texture_from_disk(f"T_{material_name}_ROUGHNESS", "Roughness", (-700, 0), 'Non-Color')
            if tex_roughness:
                links.new(tex_roughness.outputs['Color'], bsdf.inputs['Roughness'])
            
            tex_opacity = load_texture_from_disk(f"T_{material_name}_OPACITY", "Opacity", (-700, -200), 'Non-Color')
            if tex_opacity:
                links.new(tex_opacity.outputs['Color'], bsdf.inputs['Alpha'])
            
            if normal_type == 'DIRECTX':
                normal_texture_name = f"T_{material_name}_NORMAL_DIRECTX"
            else:
                normal_texture_name = f"T_{material_name}_NORMAL"
                
            tex_normal = load_texture_from_disk(normal_texture_name, "Normal", (-700, -400), 'Non-Color')
            if tex_normal:
                connect_normal_map(nodes, links, tex_normal, bsdf, normal_type, (-400, -400))
        
        material.blend_method = 'HASHED'
        material.shadow_method = 'HASHED'
        material.use_backface_culling = False
        
        bsdf.inputs['Base Color'].default_value = (1.0, 1.0, 1.0, 1.0)
        bsdf.inputs['Metallic'].default_value = 0.0
        bsdf.inputs['Roughness'].default_value = 0.8
        bsdf.inputs['IOR'].default_value = 1.5
        bsdf.inputs['Alpha'].default_value = 1.0
        bsdf.inputs['Emission Color'].default_value = (0.0, 0.0, 0.0, 1.0)
        bsdf.inputs['Emission Strength'].default_value = 0
        
        bpy.context.view_layer.update()
        
        for area in bpy.context.screen.areas:
            area.tag_redraw()
            
        material.node_tree.update_tag()
        
        print(f"✅ Текстуры загружены с диска и подключены к материалу {material.name} в режиме {connection_mode}")
        
        return material
    
    def save_texture_set_info_with_path(self, context, obj, material_name, resolution, output_path):
        """Сохраняет информацию о созданном наборе текстур"""
        scene = context.scene
        
        # Проверяем, есть ли уже такой набор
        existing_set = None
        for tex_set in scene.baker_texture_sets:
            if tex_set.material_name == material_name and tex_set.object_name == obj.name:
                existing_set = tex_set
                break
        
        if not existing_set:
            existing_set = scene.baker_texture_sets.add()
        
        existing_set.name = f"T_{material_name}"
        existing_set.material_name = material_name
        existing_set.object_name = obj.name
        existing_set.resolution = resolution
        existing_set.output_path = output_path
        
        # Проверяем какие файлы существуют
        base_path = os.path.join(output_path, f"T_{material_name}")
        
        existing_set.has_diffuse = os.path.exists(f"{base_path}_DIFFUSE.png")
        existing_set.has_diffuse_opacity = os.path.exists(f"{base_path}_DIFFUSE_OPACITY.png")
        existing_set.has_normal = os.path.exists(f"{base_path}_NORMAL.png")
        existing_set.has_normal_directx = os.path.exists(f"{base_path}_NORMAL_DIRECTX.png")
        existing_set.has_roughness = os.path.exists(f"{base_path}_ROUGHNESS.png")
        existing_set.has_metallic = os.path.exists(f"{base_path}_METALLIC.png")
        existing_set.has_emit = os.path.exists(f"{base_path}_EMIT.png")
        existing_set.has_opacity = os.path.exists(f"{base_path}_OPACITY.png")
        existing_set.has_erm = os.path.exists(f"{base_path}_ERM.png")
        
        print(f"📝 Сохранена информация о наборе: {material_name}")
        print(f"   Разрешение: {resolution}x{resolution}")
        print(f"   Путь: {output_path}")

    def is_flat_normal(self, principled_node):
        """Проверяет, является ли нормаль плоской (не подключена текстура)"""
        normal_texture = None
        
        if principled_node.inputs['Normal'].links:
            from_node = principled_node.inputs['Normal'].links[0].from_node
            
            if from_node.type == 'NORMAL_MAP' and from_node.inputs['Color'].links:
                tex_node = from_node.inputs['Color'].links[0].from_node
                if tex_node.type == 'TEX_IMAGE' and tex_node.image:
                    normal_texture = tex_node.image
            
            elif from_node.type == 'TEX_IMAGE' and from_node.image:
                normal_texture = from_node.image
        
        return normal_texture is None



#FLORA_CREATOR
def resize_image_array(src_array, target_height, target_width):
    """Простое масштабирование изображения до размера ячейки"""
    src_height, src_width = src_array.shape[:2]
    
    # Создаём массив для результата
    result = np.zeros((target_height, target_width, 4), dtype=np.float32)
    
    # Вычисляем координаты
    x_coords = np.linspace(0, src_width - 1, target_width)
    y_coords = np.linspace(0, src_height - 1, target_height)
    
    # Округляем координаты до ближайших пикселей
    x_floor = np.floor(x_coords).astype(int)
    x_ceil = np.minimum(x_floor + 1, src_width - 1)
    x_frac = x_coords - x_floor
    
    y_floor = np.floor(y_coords).astype(int)
    y_ceil = np.minimum(y_floor + 1, src_height - 1)
    y_frac = y_coords - y_floor
    
    # Масштабируем каждый канал отдельно
    for channel in range(4):
        # Билинейная интерполяция для каждой строки
        for y in range(target_height):
            # Получаем значения для текущей и следующей строки
            top_row = src_array[y_floor[y], :, channel]
            bottom_row = src_array[y_ceil[y], :, channel]
            
            # Интерполируем между строками
            for x in range(target_width):
                # Интерполяция по x
                top = (top_row[x_floor[x]] * (1 - x_frac[x]) + 
                      top_row[x_ceil[x]] * x_frac[x])
                bottom = (bottom_row[x_floor[x]] * (1 - x_frac[x]) + 
                        bottom_row[x_ceil[x]] * x_frac[x])
                
                # Интерполяция по y
                result[y, x, channel] = top * (1 - y_frac[y]) + bottom * y_frac[y]
    
    return result

def scale_cell_pixels(cell_pixels, scale_factor):
    """Масштабирование текстуры в ячейке"""
    height, width = cell_pixels.shape[:2]
    
    # Вычисляем новые размеры
    new_width = int(width * scale_factor)
    new_height = int(height * scale_factor)
    
    # Создаём массив для результата
    result = np.zeros_like(cell_pixels)
    
    if scale_factor < 1:
        # Если уменьшаем, сначала масштабируем, потом центрируем
        scaled = resize_image_array(cell_pixels, new_height, new_width)
        
        # Вычисляем отступы для центрирования
        offset_x = (width - new_width) // 2
        offset_y = (height - new_height) // 2
        
        # Помещаем масштабированное изображение в центр
        result[offset_y:offset_y + new_height, offset_x:offset_x + new_width] = scaled
    else:
        # Если увеличиваем, обрезаем центральную часть
        start_x = (width - int(width / scale_factor)) // 2
        start_y = (height - int(height / scale_factor)) // 2
        end_x = start_x + int(width / scale_factor)
        end_y = start_y + int(height / scale_factor)
        
        # Берём центральную часть и масштабируем до полного размера
        cropped = cell_pixels[start_y:end_y, start_x:end_x]
        result = resize_image_array(cropped, height, width)
    
    return result

def calculate_grid_size(num_textures):
    """Вычисляет оптимальный размер сетки для текстур"""
    if num_textures <= 1:
        return (1, 1)
    elif num_textures <= 2:
        return (1, 2)  # Одна строка, два столбца
    elif num_textures <= 4:
        return (2, 2)  # 2x2 сетка
    elif num_textures <= 6:
        return (2, 3)  # 2x3 сетка
    elif num_textures <= 8:
        return (2, 4)  # 2x4 сетка
    else:
        # Для большего количества используем квадратную сетку
        grid = int(np.ceil(np.sqrt(num_textures)))
        return (grid, grid)

def get_uv_bounds(uv_coords):
    """Вычисляет границы UV-развертки"""
    if not uv_coords:
        return (0, 0, 1, 1)
    
    min_u = min(uv.x for uv in uv_coords)
    min_v = min(uv.y for uv in uv_coords)
    max_u = max(uv.x for uv in uv_coords)
    max_v = max(uv.y for uv in uv_coords)
    
    # Проверяем, что размеры не нулевые
    if max_u - min_u < 0.001:
        max_u = min_u + 1.0
    if max_v - min_v < 0.001:
        max_v = min_v + 1.0
    
    return (min_u, min_v, max_u, max_v)

def normalize_uv_coords(uv_coords):
    """Нормализует UV-координаты в диапазон [0, 1]"""
    min_u, min_v, max_u, max_v = get_uv_bounds(uv_coords)
    
    # Нормализуем координаты
    for uv in uv_coords:
        uv.x = (uv.x - min_u) / (max_u - min_u)
        uv.y = (uv.y - min_v) / (max_v - min_v)
    
    return uv_coords

def get_uv_aspect_ratio(uv_coords):
    """Вычисляет соотношение сторон UV-развертки"""
    min_u, min_v, max_u, max_v = get_uv_bounds(uv_coords)
    width = max_u - min_u
    height = max_v - min_v
    return width / height if height > 0 else 1.0

def calculate_optimal_scale(cell_uvs, cell_size_u, cell_size_v, grid_x, grid_y):
    """Вычисляет оптимальный масштаб для UV в ячейке"""
    # Находим границы UV в локальных координатах ячейки
    cell_min_u = min(uv.x for uv in cell_uvs) - grid_x * cell_size_u
    cell_min_v = min(uv.y for uv in cell_uvs) - grid_y * cell_size_v
    cell_max_u = max(uv.x for uv in cell_uvs) - grid_x * cell_size_u
    cell_max_v = max(uv.y for uv in cell_uvs) - grid_y * cell_size_v
    
    # Вычисляем размеры UV в ячейке
    uv_width = cell_max_u - cell_min_u
    uv_height = cell_max_v - cell_min_v
    
    # Вычисляем масштаб (оставляем 5% отступа от краёв)
    if uv_width > 0 and uv_height > 0:
        scale_u = (cell_size_u * 0.95) / uv_width
        scale_v = (cell_size_v * 0.95) / uv_height
        return min(scale_u, scale_v)
    return 1.0

def expand_colors(channel, alpha_mask, size, iterations=3):
    """Расширяет цвета от краёв непрозрачной области"""
    result = channel.copy()
    current_mask = alpha_mask.copy()
    
    for _ in range(iterations):
        # Создаём маску для пикселей, которые нужно заполнить в этой итерации
        next_mask = np.zeros_like(current_mask)
        
        for i in range(1, size-1):
            for j in range(1, size-1):
                if not current_mask[i, j]:  # Если пиксель прозрачный
                    # Проверяем соседей
                    neighbors_values = []
                    neighbors_weights = []
                    
                    for di in [-1, 0, 1]:
                        for dj in [-1, 0, 1]:
                            if current_mask[i+di, j+dj]:
                                # Вычисляем вес на основе расстояния (диагональные соседи имеют меньший вес)
                                weight = 1.0 if di == 0 or dj == 0 else 0.707  # sqrt(2)/2
                                neighbors_values.append(result[i+di, j+dj])
                                neighbors_weights.append(weight)
                    
                    if neighbors_values:
                        # Вычисляем взвешенное среднее
                        result[i, j] = np.average(neighbors_values, weights=neighbors_weights)
                        next_mask[i, j] = True
        
        # Обновляем маску для следующей итерации
        current_mask = current_mask | next_mask
    
    return result

def save_texture_maps(atlas_pixels, texture_dir, size):
    """Сохраняет диффузную и opacity карты"""
    # Создаём массивы для обеих текстур (без альфа-канала)
    diffuse_pixels = np.zeros((size, size, 4), dtype=np.float32)
    opacity_pixels = np.zeros((size, size, 4), dtype=np.float32)
    
    # Получаем маску прозрачности (1 где альфа > 0.5, 0 где <= 0.5)
    alpha_mask = atlas_pixels[..., 3] > 0.5
    
    # Обрабатываем opacity карту (чистый белый и чёрный)
    opacity_value = np.where(alpha_mask, 1.0, 0.0)
    # Устанавливаем все каналы в чистый белый или чёрный
    opacity_pixels[..., 0] = opacity_value
    opacity_pixels[..., 1] = opacity_value
    opacity_pixels[..., 2] = opacity_value
    opacity_pixels[..., 3] = 1.0
    
    # Обрабатываем диффузную карту
    # Расширяем цвета для каждого канала RGB
    for c in range(3):
        diffuse_pixels[..., c] = expand_colors(atlas_pixels[..., c], alpha_mask, size)
    diffuse_pixels[..., 3] = 1.0
    
    # Настраиваем рендер для сохранения
    scene = bpy.context.scene
    settings = scene.render.image_settings
    
    # Сохраняем текущие настройки
    original_format = settings.file_format
    original_color_mode = settings.color_mode
    original_color_depth = settings.color_depth
    original_view_settings = scene.view_settings.view_transform
    original_look = scene.view_settings.look
    original_display_device = scene.display_settings.display_device
    
    # Устанавливаем настройки для корректного сохранения PNG
    settings.file_format = 'PNG'
    settings.color_mode = 'RGB'  # Используем RGB, так как SRGB недоступен здесь
    settings.color_depth = '8'
    
    # Настраиваем цветовое пространство
    scene.view_settings.view_transform = 'Standard'
    scene.view_settings.look = 'None'
    scene.display_settings.display_device = 'sRGB'
    
    # Создаём и сохраняем диффузную карту
    diffuse_tex = bpy.data.images.new("flora_d", size, size, alpha=True)
    diffuse_tex.colorspace_settings.name = 'sRGB'
    diffuse_tex.pixels = diffuse_pixels.flatten()
    diffuse_path = os.path.join(texture_dir, "flora_d.png")
    
    # Сохраняем с правильными настройками цвета
    diffuse_tex.save_render(diffuse_path)
    
    # Создаём и сохраняем карту opacity
    opacity_tex = bpy.data.images.new("flora_o", size, size, alpha=True)
    opacity_tex.colorspace_settings.name = 'sRGB'
    opacity_tex.pixels = opacity_pixels.flatten()
    opacity_path = os.path.join(texture_dir, "flora_o.png")
    opacity_tex.save_render(opacity_path)
    
    # Восстанавливаем оригинальные настройки
    settings.file_format = original_format
    settings.color_mode = original_color_mode
    settings.color_depth = original_color_depth
    scene.view_settings.view_transform = original_view_settings
    scene.view_settings.look = original_look
    scene.display_settings.display_device = original_display_device
    
    # Очищаем временные изображения
    bpy.data.images.remove(diffuse_tex)
    bpy.data.images.remove(opacity_tex)
    
    print(f"Сохранены текстуры:")
    print(f"- Диффузная карта: {diffuse_path}")
    print(f"- Карта opacity: {opacity_path}")
    
    return diffuse_path, opacity_path

def create_tree_atlas():
    # Получаем выделенные объекты
    selected_objects = bpy.context.selected_objects
    
    # Проверяем, что выбран только один объект
    if len(selected_objects) != 1:
        print("Ошибка: Должен быть выбран ровно один объект!")
        return {'CANCELLED'}
    
    # Получаем выделенный объект
    obj = selected_objects[0]
    
    # Проверяем, что это меш
    if obj.type != 'MESH':
        print("Ошибка: Выбранный объект должен быть мешем!")
        return {'CANCELLED'}
    
    # Проверяем наличие материалов
    if len(obj.material_slots) == 0:
        print("Ошибка: У объекта нет материалов!")
        return {'CANCELLED'}
    
    # Проверяем, что у каждого материала есть только одна текстура Base Color
    print(f"Проверка материалов объекта: {obj.name}")
    valid_materials = 0
    
    for mat_slot in obj.material_slots:
        if not mat_slot.material:
            continue
            
        mat = mat_slot.material
        if not mat.use_nodes:
            print(f"Материал {mat.name}: не использует ноды - пропускаем")
            continue
        
        # Ищем Principled BSDF или другие материальные ноды
        bsdf_node = None
        for node in mat.node_tree.nodes:
            if node.type == 'BSDF_PRINCIPLED':
                bsdf_node = node
                break
        
        if not bsdf_node:
            print(f"Материал {mat.name}: не найден Principled BSDF - пропускаем")
            continue
        
        # Проверяем подключение к Base Color
        base_color_input = bsdf_node.inputs['Base Color']
        if not base_color_input.is_linked:
            print(f"Материал {mat.name}: Base Color не подключен - пропускаем")
            continue
        
        # Проверяем, что подключена именно текстура
        linked_node = base_color_input.links[0].from_node
        if linked_node.type != 'TEX_IMAGE':
            print(f"Материал {mat.name}: к Base Color подключен не текстурный нод - пропускаем")
            continue
        
        if not linked_node.image:
            print(f"Материал {mat.name}: текстурный нод не содержит изображения - пропускаем")
            continue
        
        # Проверяем, что это единственная текстура в материале
        texture_nodes = [node for node in mat.node_tree.nodes if node.type == 'TEX_IMAGE' and node.image]
        if len(texture_nodes) > 1:
            print(f"Материал {mat.name}: найдено {len(texture_nodes)} текстур, должна быть только одна - пропускаем")
            continue
        
        print(f"Материал {mat.name}: валидный - найдена одна текстура Base Color: {linked_node.image.name}")
        valid_materials += 1
    
    if valid_materials == 0:
        print("Ошибка: Не найдено подходящих материалов!")
        print("Требования: материал с нодами, Principled BSDF с одной текстурой на Base Color")
        return {'CANCELLED'}
    
    print(f"Найдено {valid_materials} подходящих материалов")
    
    # Создаём директорию для сохранения текстур
    blend_file_path = bpy.data.filepath
    if not blend_file_path:
        print("Ошибка: Сохраните файл .blend перед созданием атласа!")
        return {'CANCELLED'}
        
    directory = os.path.dirname(blend_file_path)
    texture_dir = os.path.join(directory, "Flora_texture")
    os.makedirs(texture_dir, exist_ok=True)
    
    # Создаём новый материал для атласа
    atlas_mat = bpy.data.materials.new(name="TreeAtlas")
    atlas_mat.use_nodes = True
    nodes = atlas_mat.node_tree.nodes
    
    # Создаём текстуру атласа
    atlas_tex = bpy.data.images.new("TreeAtlas", 2048, 2048, alpha=True)
    # Инициализируем пиксели атласа прозрачным цветом
    pixels = [0.0] * (2048 * 2048 * 4)
    atlas_tex.pixels = pixels
    
    # Словарь для хранения информации о текстурах
    texture_info = {}
    
    # Собираем все текстуры и UV-координаты из материалов
    if not obj.data.uv_layers:
        print("Ошибка: У объекта нет UV-слоёв!")
        return {'CANCELLED'}
        
    uv_layer = obj.data.uv_layers.active
    if not uv_layer:
        print("Ошибка: Нет активного UV-слоя!")
        return {'CANCELLED'}
    
    print("\nПоиск текстур в материалах...")
    for mat_slot in obj.material_slots:
        if not mat_slot.material:
            continue
            
        mat = mat_slot.material
        if not mat.use_nodes:
            continue
            
        print(f"- Материал: {mat.name}")
        # Ищем текстурные ноды и собираем UV-координаты
        for node in mat.node_tree.nodes:
            if node.type == 'TEX_IMAGE' and node.image:
                uv_coords = []
                # Собираем UV-координаты для этого материала
                for poly in obj.data.polygons:
                    if obj.material_slots[poly.material_index].material.name == mat.name:
                        for loop_idx in poly.loop_indices:
                            uv_coords.append(uv_layer.data[loop_idx].uv.copy())
                
                if uv_coords:  # Проверяем, что у материала есть UV-координаты
                    # Нормализуем UV-координаты
                    normalized_uvs = normalize_uv_coords(uv_coords)
                    texture_info[mat.name] = {
                        'image': node.image,
                        'uv_coords': normalized_uvs
                    }
                    print(f"  - Найдена текстура: {node.image.name}")
    
    if not texture_info:
        print("Не найдено текстур для создания атласа!")
        return {'CANCELLED'}
    
    print(f"\nНайдено текстур: {len(texture_info)}")
    
    # Вычисляем оптимальный размер сетки
    rows, cols = calculate_grid_size(len(texture_info))
    print(f"Размер сетки: {rows}x{cols}")
    
    # Создаём массив для атласа
    atlas_pixels = np.zeros((2048, 2048, 4), dtype=np.float32)
    
    # Размер ячейки в пикселях
    cell_pixels_width = int(2048 / cols)
    cell_pixels_height = int(2048 / rows)
    
    # Размер ячейки в UV-координатах
    cell_size_u = 1.0 / cols
    cell_size_v = 1.0 / rows
    
    for idx, (mat_name, tex_data) in enumerate(texture_info.items()):
        print(f"\nОбработка материала {mat_name}")
        grid_x = idx % cols
        grid_y = idx // cols
        
        # Получаем исходную текстуру
        src_image = tex_data['image']
        if not src_image.has_data:
            src_image.reload()
        
        print(f"- Размер исходной текстуры: {src_image.size[0]}x{src_image.size[1]}")
        
        # Сначала помещаем UV в ячейку
        cell_uvs = []
        for poly in obj.data.polygons:
            if obj.material_slots[poly.material_index].material.name == mat_name:
                for loop_idx in poly.loop_indices:
                    uv = uv_layer.data[loop_idx].uv
                    # Перемещаем UV в соответствующую ячейку
                    uv.x = uv.x * cell_size_u + (grid_x * cell_size_u)
                    uv.y = uv.y * cell_size_v + (grid_y * cell_size_v)
                    cell_uvs.append(uv)
        
        # Сначала помещаем текстуру в ячейку
        src_pixels = np.array(src_image.pixels[:]).reshape((src_image.size[1], src_image.size[0], 4))
        cell_pixels = resize_image_array(src_pixels, cell_pixels_height, cell_pixels_width)
        
        # Вычисляем оптимальный масштаб для UV в ячейке
        scale_factor = calculate_optimal_scale(cell_uvs, cell_size_u, cell_size_v, grid_x, grid_y)
        print(f"- Масштаб UV в ячейке: {scale_factor:.2f}")
        
        # Масштабируем UV в ячейке
        for uv in cell_uvs:
            # Центрируем относительно ячейки
            local_u = uv.x - (grid_x * cell_size_u) - (cell_size_u / 2)
            local_v = uv.y - (grid_y * cell_size_v) - (cell_size_v / 2)
            
            # Масштабируем и возвращаем обратно
            uv.x = local_u * scale_factor + (grid_x * cell_size_u) + (cell_size_u / 2)
            uv.y = local_v * scale_factor + (grid_y * cell_size_v) + (cell_size_v / 2)
        
        # Масштабируем текстуру в ячейке с тем же коэффициентом
        scaled_pixels = scale_cell_pixels(cell_pixels, scale_factor)
        
        # Копируем текстуру в атлас
        atlas_x = grid_x * cell_pixels_width
        atlas_y = grid_y * cell_pixels_height
        atlas_pixels[atlas_y:atlas_y + cell_pixels_height,
                    atlas_x:atlas_x + cell_pixels_width] = scaled_pixels
    
    # Обновляем пиксели атласа
    atlas_tex.pixels = atlas_pixels.flatten()
    
    # Сохраняем диффузную и opacity карты
    diffuse_path, opacity_path = save_texture_maps(atlas_pixels, texture_dir, 2048)
    
    # Создаём новый материал с атласом
    atlas_mat = bpy.data.materials.new(name="TreeAtlas")
    atlas_mat.use_nodes = True
    nodes = atlas_mat.node_tree.nodes
    links = atlas_mat.node_tree.links
    
    # Очищаем существующие ноды
    nodes.clear()
    
    # Создаём и настраиваем ноды
    principled_bsdf = nodes.new('ShaderNodeBsdfPrincipled')
    principled_bsdf.location = (300, 0)
    
    output = nodes.new('ShaderNodeOutputMaterial')
    output.location = (500, 0)
    
    # Загружаем и подключаем диффузную текстуру
    diffuse_tex_node = nodes.new('ShaderNodeTexImage')
    diffuse_tex_node.location = (0, 200)
    diffuse_tex_node.image = bpy.data.images.load(diffuse_path)
    diffuse_tex_node.image.colorspace_settings.name = 'sRGB'
    
    # Загружаем и подключаем opacity текстуру
    opacity_tex_node = nodes.new('ShaderNodeTexImage')
    opacity_tex_node.location = (0, -200)
    opacity_tex_node.image = bpy.data.images.load(opacity_path)
    opacity_tex_node.image.colorspace_settings.name = 'sRGB'
    
    # Создаём связи между нодами
    links.new(diffuse_tex_node.outputs['Color'], principled_bsdf.inputs['Base Color'])
    links.new(opacity_tex_node.outputs['Color'], principled_bsdf.inputs['Alpha'])
    links.new(principled_bsdf.outputs['BSDF'], output.inputs['Surface'])
    
    # Настраиваем прозрачность материала
    atlas_mat.blend_method = 'HASHED'
    atlas_mat.shadow_method = 'HASHED'
    principled_bsdf.inputs['Alpha'].default_value = 1.0
    
    # Применяем материал к объекту
    obj.data.materials.clear()
    obj.data.materials.append(atlas_mat)
    
    print(f"\nАтлас создан успешно!")
    print(f"Диффузная карта сохранена и подключена: {diffuse_path}")
    print(f"Карта opacity сохранена и подключена: {opacity_path}")
    print(f"Размер сетки: {rows}x{cols}, размер ячейки: {cell_pixels_width}x{cell_pixels_height} пикселей")
    
    return {'FINISHED'}


class BAKER_OT_create_atlas_flora(Operator):
    """Создать атлас текстур для растительности"""
    bl_idname = "flora.create_atlas"
    bl_label = "Создать атлас Flora"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        try:
            result = create_tree_atlas()
            if result == {'CANCELLED'}:
                self.report({'ERROR'}, "Не удалось создать атлас. Проверьте консоль для деталей.")
                return {'CANCELLED'}
            else:
                self.report({'INFO'}, "Атлас Flora успешно создан!")
                return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка при создании атласа: {str(e)}")
            print(f"Подробная ошибка: {e}")
            return {'CANCELLED'}


class BAKER_PT_atlas_panel_flora(Panel):
    """Панель для создания атласа Flora"""
    bl_label = "Создание атласа Flora"
    bl_idname = "BAKER_PT_atlas_panel_flora"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "AGR_baker"
    bl_options = {'DEFAULT_CLOSED'}
    
    def draw(self, context):
        layout = self.layout
        
        # Информация о выбранном объекте
        obj = context.active_object
        if obj and obj.type == 'MESH':
            box = layout.box()
            box.label(text=f"Выбранный объект: {obj.name}", icon='OBJECT_DATA')
            
            # Проверяем материалы
            if obj.material_slots:
                box.label(text=f"Материалов: {len(obj.material_slots)}", icon='MATERIAL')
                
                # Показываем материалы с текстурами
                texture_count = 0
                for mat_slot in obj.material_slots:
                    if mat_slot.material and mat_slot.material.use_nodes:
                        for node in mat_slot.material.node_tree.nodes:
                            if node.type == 'TEX_IMAGE' and node.image:
                                texture_count += 1
                                break
                
                if texture_count > 0:
                    box.label(text=f"Текстур найдено: {texture_count}", icon='TEXTURE')
                else:
                    box.label(text="Текстуры не найдены", icon='ERROR')
            else:
                box.label(text="Нет материалов", icon='ERROR')
                
            # Проверяем UV
            if obj.data.uv_layers:
                box.label(text=f"UV слоёв: {len(obj.data.uv_layers)}", icon='UV')
            else:
                box.label(text="Нет UV слоёв", icon='ERROR')
        else:
            layout.label(text="Выберите меш объект", icon='INFO')
        
        layout.separator()
        
        # Инструкции
        box = layout.box()
        box.label(text="Инструкция:", icon='HELP')
        box.label(text="1. Выберите меш с материалами")
        box.label(text="2. Убедитесь, что есть UV развёртка")
        box.label(text="3. Убедитесь, что все материалы имеют только одну текстуру Base Color")
        box.label(text="4. Сохраните .blend файл")
        box.label(text="5. Нажмите кнопку создания атласа")
        
        layout.separator()
        
        # Основная кнопка
        row = layout.row(align=True)
        row.scale_y = 2.0
        row.operator("flora.create_atlas", icon='TEXTURE')
        
        # Дополнительная информация
        layout.separator()
        box = layout.box()
        box.label(text="Результат:", icon='FILE_FOLDER')
        box.label(text="• flora_d.png - диффузная карта")
        box.label(text="• flora_o.png - карта прозрачности")
        box.label(text="• скейл текстур под 0.90625")
        box.label(text="• Папка: Flora_texture/")

#--------------------------------
#Bound_creator
# Настройка логирования
def log_message(message):
    """Функция для гарантированного вывода в консоль Blender"""
    print(message, flush=True)  # Добавляем flush=True для немедленного вывода
    # Также можно добавить вывод в системный лог Blender
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'CONSOLE':
                override = {'window': window, 'screen': window.screen, 'area': area}
                bpy.ops.console.scrollback_append(override, text=message, type="INFO")

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Добавляем вывод в консоль Blender
class BlenderConsoleHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            print(msg, flush=True)  # Вывод в консоль Blender
        except Exception:
            self.handleError(record)

# Настраиваем форматтер
formatter = logging.Formatter('%(message)s')
console_handler = BlenderConsoleHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# Константы
TARGET_SIZE = 4096
SCALE_FACTOR = 0.90625
BORDER_SIZE = 192  # Размер рамки в пикселях
MAX_IMAGE_SIZE = 16384  # Максимальный размер изображения
MIN_IMAGE_SIZE = 64     # Минимальный размер изображения
SUPPORTED_FORMATS = {'.png', '.jpg', '.jpeg', '.tga', '.tiff'}
REQUIRED_BLENDER_VERSION = (3, 0, 0)

# Проверка версии Blender
def check_blender_version():
    if bpy.app.version < REQUIRED_BLENDER_VERSION:
        raise RuntimeError(f"Требуется Blender версии {'.'.join(map(str, REQUIRED_BLENDER_VERSION))} или выше")

# Проверка GPU и CUDA
def check_gpu():
    """Проверяет наличие GPU и его характеристики"""
    try:
        device = cp.cuda.runtime.getDeviceProperties(0)
        memory = device['totalGlobalMem'] / 1024**3  # в ГБ
        log_message(f"GPU: {device['name'].decode()}")
        log_message(f"Память GPU: {memory:.2f} GB")
        return True, device, memory
    except ImportError:
        log_message("Cupy не установлен. Будет использован CPU.")
        return False, None, 0
    except Exception as e:
        log_message(f"Ошибка при проверке GPU: {str(e)}")
        return False, None, 0

# Проверка безопасности пути
def is_safe_path(path):
    try:
        path = Path(path).resolve()
        return str(path).startswith(str(Path(bpy.data.filepath).parent.resolve()))
    except:
        return False

# Проверка формата файла
def is_supported_format(filepath):
    return Path(filepath).suffix.lower() in SUPPORTED_FORMATS

# Проверка размера изображения
def check_image_size(image):
    if image.size[0] > MAX_IMAGE_SIZE or image.size[1] > MAX_IMAGE_SIZE:
        raise ValueError(f"Изображение слишком большое: {image.size[0]}x{image.size[1]}")
    if image.size[0] < MIN_IMAGE_SIZE or image.size[1] < MIN_IMAGE_SIZE:
        raise ValueError(f"Изображение слишком маленькое: {image.size[0]}x{image.size[1]}")

# Проверка UV-развертки
def check_uv_unwrap(obj):
    if not obj.data.uv_layers:
        raise ValueError("Объект не имеет UV-развертки")
    return obj.data.uv_layers.active

# Проверка материала
def check_material(obj):
    if not obj.data.materials:
        raise ValueError("Объект не имеет материала")
    return obj.data.materials[0]

# Проверка сохраненности файла
def check_saved_file():
    if not bpy.data.filepath:
        raise ValueError("Файл .blend не сохранен")

# Создание безопасной директории
def create_safe_directory(path):
    try:
        path = Path(path)
        if not is_safe_path(path):
            raise ValueError("Небезопасный путь")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    except Exception as e:
        raise ValueError(f"Ошибка при создании директории: {str(e)}")

# Очистка памяти GPU
def cleanup_gpu():
    try:
        cp.get_default_memory_pool().free_all_blocks()
    except:
        pass

# Контекстный менеджер для GPU операций
class GPUContext:
    def __init__(self):
        self.stream = None
        self.use_gpu = False
        
    def __enter__(self):
        try:
            self.use_gpu = True
            self.stream = cp.cuda.Stream()
            return self
        except:
            self.use_gpu = False
            return self
            
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.use_gpu:
            cleanup_gpu()

# Класс для хранения информации о текстуре
class TextureItem(PropertyGroup):
    name: StringProperty(name="Name")
    filepath: StringProperty(name="Filepath")
    selected: BoolProperty(name="Selected", default=False)

def resize_and_scale_image(image):
    """Изменяет размер изображения до 4096x4096 и применяет масштабирование с рамкой 192px"""
    try:
        # Проверка размера изображения
        check_image_size(image)
        
        # Проверяем настройку использования GPU
        use_gpu = bpy.context.scene.use_gpu and HAS_CUDA
        
        # Создаем новое изображение 4096x4096
        result_image = bpy.data.images.new(
            name="temp_result",
            width=TARGET_SIZE,
            height=TARGET_SIZE
        )
        
        # Заполняем белым цветом
        result_image.pixels = [1.0] * (TARGET_SIZE * TARGET_SIZE * 4)
        result_image.update()
        
        # Вычисляем размеры для масштабирования
        scaled_size = int(TARGET_SIZE * SCALE_FACTOR)
        
        with GPUContext() as gpu:
            if gpu.use_gpu and use_gpu:
                try:
                    with gpu.stream:
                        # Переносим данные на GPU с использованием pinned memory
                        source_pixels = cp.asarray(image.pixels[:], dtype=cp.float32).reshape(image.size[0], image.size[1], -1)
                        
                        # Создаем сетку координат для масштабирования
                        y_coords, x_coords = cp.meshgrid(cp.arange(scaled_size), cp.arange(scaled_size), indexing='ij')
                        
                        # Вычисляем координаты исходного изображения
                        src_x = (x_coords * image.size[0] / scaled_size).astype(cp.int32)
                        src_y = (y_coords * image.size[1] / scaled_size).astype(cp.int32)
                        
                        # Масштабируем изображение на GPU
                        scaled_pixels = source_pixels[src_y, src_x]
                        
                        # Вычисляем смещение для центрирования
                        offset = BORDER_SIZE
                        
                        # Создаем белое изображение на GPU
                        result_pixels = cp.ones((TARGET_SIZE, TARGET_SIZE, 4), dtype=cp.float32)
                        
                        # Копируем масштабированное изображение в центр
                        result_pixels[offset:offset+scaled_size, offset:offset+scaled_size] = scaled_pixels
                        
                        # Переносим результат обратно на CPU
                        result_image.pixels = cp.asnumpy(result_pixels).flatten()
                        
                except Exception as e:
                    use_gpu = False
            
            if not use_gpu:
                # CPU версия
                source_pixels = np.array(image.pixels[:]).reshape(image.size[0], image.size[1], -1)
                scaled_pixels = np.zeros((scaled_size, scaled_size, 4), dtype=np.float32)
                
                for y in range(scaled_size):
                    for x in range(scaled_size):
                        src_x = int(x * image.size[0] / scaled_size)
                        src_y = int(y * image.size[1] / scaled_size)
                        scaled_pixels[y, x] = source_pixels[src_y, src_x]
                
                offset = BORDER_SIZE
                result_pixels = np.array(result_image.pixels[:]).reshape(TARGET_SIZE, TARGET_SIZE, -1)
                result_pixels[offset:offset+scaled_size, offset:offset+scaled_size] = scaled_pixels
                result_image.pixels = result_pixels.flatten()
        
        result_image.update()
        
        # Копируем результат в исходное изображение
        image.pixels = result_image.pixels[:]
        image.update()
        
        # Очищаем временные данные
        bpy.data.images.remove(result_image)
        
    except Exception as e:
        raise

def format_time(seconds):
    """Форматирует время в удобный для чтения формат"""
    return str(timedelta(seconds=round(seconds)))

def apply_overlay(base_image, overlay_image):
    """Накладывает изображение с альфа-каналом на базовое изображение"""
    try:
        if not base_image or not overlay_image:
            raise ValueError("Отсутствует базовое или накладываемое изображение")
            
        # Проверяем размеры изображений
        base_size = (base_image.size[0], base_image.size[1])
        overlay_size = (overlay_image.size[0], overlay_image.size[1])
        
        if base_size != overlay_size:
            # Изменяем размер оверлея под размер базового изображения
            temp_overlay = bpy.data.images.new(
                name="temp_overlay",
                width=base_size[0],
                height=base_size[1]
            )
            
            # Копируем данные оверлея во временное изображение
            temp_overlay.pixels = overlay_image.pixels[:]
            
            # Масштабируем временное изображение
            temp_overlay.scale(base_size[0], base_size[1])
            
            # Используем масштабированный оверлей
            overlay_image = temp_overlay
            
        # Проверяем наличие альфа-канала
        if len(base_image.pixels) % 4 != 0 or len(overlay_image.pixels) % 4 != 0:
            raise ValueError("Изображения должны иметь альфа-канал")
        
        # Проверяем настройку использования GPU
        use_gpu = bpy.context.scene.use_gpu and HAS_CUDA
        
        with GPUContext() as gpu:
            if gpu.use_gpu and use_gpu:
                try:
                    with gpu.stream:
                        # Переносим данные на GPU с использованием pinned memory
                        base_pixels = cp.asarray(base_image.pixels[:], dtype=cp.float32).reshape(base_image.size[0], base_image.size[1], -1)
                        overlay_pixels = cp.asarray(overlay_image.pixels[:], dtype=cp.float32).reshape(overlay_image.size[0], overlay_image.size[1], -1)
                        
                        # Накладываем с учетом альфа-канала на GPU
                        alpha = overlay_pixels[:, :, 3:4]
                        result = base_pixels * (1 - alpha) + overlay_pixels * alpha
                        
                        # Переносим результат обратно на CPU
                        base_image.pixels = cp.asnumpy(result).flatten()
                        
                except Exception as e:
                    use_gpu = False
            
            if not use_gpu:
                # CPU версия
                base_pixels = np.array(base_image.pixels[:]).reshape(base_image.size[0], base_image.size[1], -1)
                overlay_pixels = np.array(overlay_image.pixels[:]).reshape(overlay_image.size[0], overlay_image.size[1], -1)
                
                alpha = overlay_pixels[:, :, 3:4]
                result = base_pixels * (1 - alpha) + overlay_pixels * alpha
                
                base_image.pixels = result.flatten()
        
        base_image.update()
        
        # Очищаем временное изображение, если оно было создано
        if 'temp_overlay' in locals():
            bpy.data.images.remove(temp_overlay)
        
    except Exception as e:
        raise

def scale_uv_and_bake(self, context):
    """Масштабирует UV-развертку на временном объекте и запекает текстуры"""
    start_time = time.time()
    print(f"Начало работы скрипта: {time.strftime('%H:%M:%S', time.localtime(start_time))}")
    
    try:
        # Получаем выбранные текстуры
        selected_textures = [item for item in context.scene.texture_list if item.selected]
        if not selected_textures:
            self.report({'ERROR'}, "Не выбрано ни одной текстуры для запекания")
            return {'CANCELLED'}
        
        # Проверяем сохраненность файла
        check_saved_file()
        
        # Проверяем GPU
        has_gpu, device, memory = check_gpu()
        if has_gpu and memory < 4:  # Если меньше 4GB памяти GPU
            self.report({'WARNING'}, "Недостаточно памяти GPU. Рекомендуется использовать CPU.")
        
        # Создаем временный объект
        try:
            # Снимаем выделение со всех объектов
            bpy.ops.object.select_all(action='DESELECT')
            
            # Создаем временный объект (plane) для запекания
            temp_mesh = bpy.data.meshes.new("temp_bake_mesh")
            temp_obj = bpy.data.objects.new("temp_bake_obj", temp_mesh)
            bpy.context.scene.collection.objects.link(temp_obj)
            
            # Делаем временный объект активным и выбранным
            bpy.context.view_layer.objects.active = temp_obj
            temp_obj.select_set(True)
            
            # Создаем простой UV-меш (plane)
            vertices = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
            faces = [(0, 1, 2, 3)]
            temp_mesh.from_pydata(vertices, [], faces)
            temp_mesh.update()
            
            # Создаем UV-развертку
            temp_mesh.uv_layers.new(name="UVMap")
            uv_layer = temp_mesh.uv_layers.active.data
            
            # Создаем уменьшенную UV-развертку (0.90624)
            scale = 0.90624
            offset = (1 - scale) / 2  # Центрируем уменьшенную развертку
            
            uv_layer[0].uv = (offset, offset)  # Нижний левый
            uv_layer[1].uv = (scale + offset, offset)  # Нижний правый
            uv_layer[2].uv = (scale + offset, scale + offset)  # Верхний правый
            uv_layer[3].uv = (offset, scale + offset)  # Верхний левый
            
            # Переходим в режим редактирования
            bpy.ops.object.mode_set(mode='EDIT')
            
            # Выбираем все UV-точки
            bpy.ops.mesh.select_all(action='SELECT')
            
            # Возвращаемся в объектный режим
            bpy.ops.object.mode_set(mode='OBJECT')
            
            # Сохраняем текущие настройки рендера
            old_engine = bpy.context.scene.render.engine
            old_samples = bpy.context.scene.cycles.samples
            old_film_exposure = bpy.context.scene.view_settings.exposure
            old_film_gamma = bpy.context.scene.view_settings.gamma
            
            # Настраиваем Cycles для запекания
            bpy.context.scene.render.engine = 'CYCLES'
            bpy.context.scene.cycles.samples = 1
            bpy.context.scene.cycles.device = 'CPU'
            bpy.context.scene.cycles.pixel_filter_type = 'BOX'
            bpy.context.scene.cycles.use_denoising = False
            
            # Настраиваем мир
            if bpy.context.scene.world is None:
                bpy.context.scene.world = bpy.data.worlds.new("Bake_World")
            
            bpy.context.scene.world.use_nodes = True
            world_nodes = bpy.context.scene.world.node_tree.nodes
            world_links = bpy.context.scene.world.node_tree.links
            
            for node in world_nodes:
                world_nodes.remove(node)
            
            background = world_nodes.new('ShaderNodeBackground')
            background.inputs['Color'].default_value = (1, 1, 1, 1)
            background.inputs['Strength'].default_value = 1
            
            world_output = world_nodes.new('ShaderNodeOutputWorld')
            world_links.new(background.outputs['Background'], world_output.inputs['Surface'])
            
            # Настраиваем запекание
            bpy.context.scene.render.bake.use_selected_to_active = False
            bpy.context.scene.render.bake.target = 'IMAGE_TEXTURES'
            bpy.context.scene.render.bake.margin = 512
            bpy.context.scene.render.bake.use_clear = True
            bpy.context.scene.render.bake.use_cage = False
            
            # Создаем директорию для сохранения результатов
            blend_name = os.path.splitext(os.path.basename(bpy.data.filepath))[0]
            folder_name = f"{blend_name}_Baked"
            save_path = create_safe_directory(os.path.join(os.path.dirname(bpy.data.filepath), folder_name))
            
            # Запекаем каждую выбранную текстуру
            for i, texture_item in enumerate(selected_textures, 1):
                try:
                    # Проверяем формат файла
                    if not is_supported_format(texture_item.filepath):
                        continue
                    
                    # Создаем новое изображение для запекания
                    image_name = os.path.splitext(os.path.basename(texture_item.filepath))[0]
                    
                    # Удаляем существующее изображение, если оно есть
                    if image_name in bpy.data.images:
                        bpy.data.images.remove(bpy.data.images[image_name])
                    
                    # Загружаем исходную текстуру
                    source_image = bpy.data.images.load(texture_item.filepath)
                    
                    # Проверяем размер изображения
                    check_image_size(source_image)
                    
                    # Создаем новое изображение того же размера
                    bake_image = bpy.data.images.new(
                        name=image_name,
                        width=source_image.size[0],
                        height=source_image.size[1]
                    )
                    
                    # Создаем материал
                    if "temp_bake_material" in bpy.data.materials:
                        bpy.data.materials.remove(bpy.data.materials["temp_bake_material"])
                    bake_material = bpy.data.materials.new(name="temp_bake_material")
                    bake_material.use_nodes = True
                    temp_obj.data.materials.clear()
                    temp_obj.data.materials.append(bake_material)
                    
                    # Очищаем ноды материала
                    nodes = bake_material.node_tree.nodes
                    links = bake_material.node_tree.links
                    nodes.clear()
                    
                    # Создаем ноды для запекания
                    principled = nodes.new('ShaderNodeBsdfPrincipled')
                    output = nodes.new('ShaderNodeOutputMaterial')
                    tex_image = nodes.new('ShaderNodeTexImage')
                    bake_image_node = nodes.new('ShaderNodeTexImage')
                    
                    # Настраиваем ноды
                    tex_image.image = source_image
                    bake_image_node.image = bake_image
                    bake_image_node.select = True
                    nodes.active = bake_image_node
                    
                    # Соединяем ноды
                    links.new(tex_image.outputs['Color'], principled.inputs['Base Color'])
                    links.new(principled.outputs['BSDF'], output.inputs['Surface'])
                    
                    # Запекаем
                    bpy.ops.object.bake(type='DIFFUSE', pass_filter={'COLOR'})
                    
                    # Накладываем оверлей
                    apply_overlay(bake_image, source_image)
                    
                    # Сохраняем результат
                    if not bpy.data.filepath:
                        raise ValueError("Файл .blend не сохранен")
                    
                    blend_name = os.path.splitext(os.path.basename(bpy.data.filepath))[0]
                    folder_name = f"{blend_name}_Baked"
                    image_path = f"//{folder_name}/{image_name}.png"
                    abs_path = bpy.path.abspath(image_path)
                    
                    # Создаем папку, если её нет
                    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                    
                    # Сохраняем текущие настройки рендера
                    old_format = bpy.context.scene.render.image_settings.file_format
                    old_compression = bpy.context.scene.render.image_settings.compression
                    old_color_mode = bpy.context.scene.render.image_settings.color_mode
                    old_color_depth = bpy.context.scene.render.image_settings.color_depth
                    
                    try:
                        # Настраиваем параметры сохранения
                        bpy.context.scene.render.image_settings.file_format = 'PNG'
                        bpy.context.scene.render.image_settings.compression = 90  # Максимальное сжатие PNG
                        bpy.context.scene.render.image_settings.color_mode = 'RGB'  # Сохраняем без альфа-канала
                        bpy.context.scene.render.image_settings.color_depth = '8'  # 8 бит на канал
                        
                        # Создаем временное изображение без альфа-канала
                        temp_image = bpy.data.images.new(
                            name="temp_rgb",
                            width=bake_image.size[0],
                            height=bake_image.size[1]
                        )
                        temp_image.colorspace_settings.name = 'sRGB'
                        
                        # Копируем только RGB каналы
                        pixels = list(bake_image.pixels)
                        new_pixels = []
                        for i in range(0, len(pixels), 4):
                            new_pixels.extend(pixels[i:i+3])  # Копируем только RGB
                            new_pixels.append(1.0)  # Добавляем непрозрачный альфа-канал
                        temp_image.pixels = new_pixels
                        
                        # Сохраняем изображение
                        temp_image.filepath_raw = abs_path
                        temp_image.save_render(abs_path)
                        
                        # Удаляем временное изображение
                        bpy.data.images.remove(temp_image)
                        
                    finally:
                        # Восстанавливаем настройки рендера
                        bpy.context.scene.render.image_settings.file_format = old_format
                        bpy.context.scene.render.image_settings.compression = old_compression
                        bpy.context.scene.render.image_settings.color_mode = old_color_mode
                        bpy.context.scene.render.image_settings.color_depth = old_color_depth
                    
                    # Удаляем временные данные
                    bpy.data.images.remove(source_image)
                    bpy.data.images.remove(bake_image)
                    
                except Exception as e:
                    continue
            
            self.report({'INFO'}, f"Обработано {len(selected_textures)} текстур")
            return {'FINISHED'}
            
        except Exception as e:
            self.report({'ERROR'}, f"Критическая ошибка: {str(e)}")
            return {'CANCELLED'}
            
        finally:
            # Восстанавливаем настройки рендера
            bpy.context.scene.render.engine = old_engine
            bpy.context.scene.cycles.samples = old_samples
            bpy.context.scene.view_settings.exposure = old_film_exposure
            bpy.context.scene.view_settings.gamma = old_film_gamma
            
            # Удаляем временные объекты
            if 'temp_obj' in locals():
                bpy.data.objects.remove(temp_obj, do_unlink=True)
            if 'temp_mesh' in locals():
                bpy.data.meshes.remove(temp_mesh)
            if 'bake_material' in bpy.data.materials:
                bpy.data.materials.remove(bpy.data.materials["temp_bake_material"])
                
    except Exception as e:
        self.report({'ERROR'}, f"Критическая ошибка: {str(e)}")
        return {'CANCELLED'}
    
    finally:
        end_time = time.time()
        print(f"Конец работы скрипта: {time.strftime('%H:%M:%S', time.localtime(end_time))}")
        print(f"Общее время работы: {format_time(end_time - start_time)}")

class TEXTURE_UL_list(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            layout.prop(item, "selected", text="")
            layout.label(text=os.path.basename(item.filepath))

class BAKER_OT_ScaleAndBake(Operator):
    bl_idname = "uv.scale_and_bake"
    bl_label = "Scale UV and Bake Textures"
    bl_description = "Scale UV to 0.90624 and bake selected textures"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        return scale_uv_and_bake(self, context)

class BAKER_OT_LoadTextures(Operator):
    bl_idname = "uv.load_textures"
    bl_label = "Load Textures"
    bl_description = "Load textures from directory"
    
    directory: StringProperty(
        name="Directory",
        subtype='DIR_PATH'
    )
    
    def execute(self, context):
        # Очищаем старый список
        context.scene.texture_list.clear()
        
        loaded_count = 0
        skipped_count = 0
        
        # Ищем все текстуры в директории
        for filename in os.listdir(self.directory):
            if filename.lower().endswith(tuple(SUPPORTED_FORMATS)):
                filepath = os.path.join(self.directory, filename)
                
                try:
                    # Загружаем изображение для проверки размера
                    temp_image = bpy.data.images.load(filepath)
                    
                    # Проверяем размер - должно быть точно 4096x4096
                    if temp_image.size[0] == 4096 and temp_image.size[1] == 4096:
                        item = context.scene.texture_list.add()
                        item.name = filename
                        item.filepath = filepath
                        loaded_count += 1
                    else:
                        skipped_count += 1
                    
                    # Удаляем временное изображение
                    bpy.data.images.remove(temp_image)
                    
                except Exception as e:
                    print(f"Ошибка при загрузке {filename}: {e}")
                    skipped_count += 1
        
        if loaded_count > 0:
            self.report({'INFO'}, f"Загружено {loaded_count} текстур размером 4096x4096")
        else:
            self.report({'WARNING'}, "Не найдено текстур размером 4096x4096")
            
        if skipped_count > 0:
            self.report({'INFO'}, f"Пропущено {skipped_count} текстур (неподходящий размер)")
        
        return {'FINISHED'}
    
    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

class BAKER_OT_ResizeAndScale(Operator):
    bl_idname = "uv.resize_and_scale"
    bl_label = "Resize and Scale Textures"
    bl_description = "Resize textures to 4096x4096 and scale to 0.90625"
    
    def execute(self, context):
        selected_textures = [item for item in context.scene.texture_list if item.selected]
        if not selected_textures:
            self.report({'ERROR'}, "Не выбрано ни одной текстуры")
            return {'CANCELLED'}
        
        try:
            for texture_item in selected_textures:
                image = bpy.data.images.load(texture_item.filepath)
                resize_and_scale_image(image)
                
                # Сохраняем результат
                blend_name = os.path.splitext(os.path.basename(bpy.data.filepath))[0]
                folder_name = f"{blend_name}_Resized"
                image_path = f"//{folder_name}/{os.path.basename(texture_item.filepath)}"
                abs_path = bpy.path.abspath(image_path)
                
                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                image.filepath_raw = abs_path
                image.save()
                
                bpy.data.images.remove(image)
            
            self.report({'INFO'}, f"Обработано {len(selected_textures)} текстур")
            return {'FINISHED'}
            
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

class BAKER_OT_ResizeScaleAndBake(Operator):
    bl_idname = "uv.resize_scale_and_bake"
    bl_label = "Resize, Scale and Bake Textures"
    bl_description = "Resize textures to 4096x4096, scale to 0.90625 and bake them"
    bl_options = {'REGISTER', 'UNDO'}
    
    overlay_filepath: StringProperty(
        name="Overlay Texture",
        subtype='FILE_PATH',
        description="Выберите текстуру для наложения"
    )
    
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)
    
    def execute(self, context):
        start_time = time.time()
        print(f"Начало обработки текстур: {time.strftime('%H:%M:%S', time.localtime(start_time))}")
        
        selected_textures = [item for item in context.scene.texture_list if item.selected]
        if not selected_textures:
            self.report({'ERROR'}, "Не выбрано ни одной текстуры")
            return {'CANCELLED'}
        
        if not self.overlay_filepath:
            self.report({'ERROR'}, "Не выбрана текстура для наложения")
            return {'CANCELLED'}
        
        try:
            # Загружаем текстуру оверлея
            overlay_image = bpy.data.images.load(self.overlay_filepath)
            
            # Снимаем выделение со всех объектов
            bpy.ops.object.select_all(action='DESELECT')
            
            # Создаем временный объект (plane) для запекания
            temp_mesh = bpy.data.meshes.new("temp_bake_mesh")
            temp_obj = bpy.data.objects.new("temp_bake_obj", temp_mesh)
            bpy.context.scene.collection.objects.link(temp_obj)
            
            # Делаем временный объект активным и выбранным
            bpy.context.view_layer.objects.active = temp_obj
            temp_obj.select_set(True)
            
            # Создаем простой UV-меш (plane)
            vertices = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
            faces = [(0, 1, 2, 3)]
            temp_mesh.from_pydata(vertices, [], faces)
            temp_mesh.update()
            
            # Создаем UV-развертку
            temp_mesh.uv_layers.new(name="UVMap")
            uv_layer = temp_mesh.uv_layers.active.data
            
            # Создаем уменьшенную UV-развертку (0.90624)
            scale = 0.90624
            offset = (1 - scale) / 2  # Центрируем уменьшенную развертку
            
            uv_layer[0].uv = (offset, offset)  # Нижний левый
            uv_layer[1].uv = (scale + offset, offset)  # Нижний правый
            uv_layer[2].uv = (scale + offset, scale + offset)  # Верхний правый
            uv_layer[3].uv = (offset, scale + offset)  # Верхний левый
            
            # Переходим в режим редактирования
            bpy.ops.object.mode_set(mode='EDIT')
            
            # Выбираем все UV-точки
            bpy.ops.mesh.select_all(action='SELECT')
            
            # Возвращаемся в объектный режим
            bpy.ops.object.mode_set(mode='OBJECT')
            
            # Сохраняем текущие настройки рендера
            old_engine = bpy.context.scene.render.engine
            old_samples = bpy.context.scene.cycles.samples
            old_film_exposure = bpy.context.scene.view_settings.exposure
            old_film_gamma = bpy.context.scene.view_settings.gamma
            
            # Настраиваем Cycles для запекания
            bpy.context.scene.render.engine = 'CYCLES'
            bpy.context.scene.cycles.samples = 1
            bpy.context.scene.cycles.device = 'CPU'
            bpy.context.scene.cycles.pixel_filter_type = 'BOX'
            bpy.context.scene.cycles.use_denoising = False
            
            # Настраиваем мир
            if bpy.context.scene.world is None:
                bpy.context.scene.world = bpy.data.worlds.new("Bake_World")
            
            bpy.context.scene.world.use_nodes = True
            world_nodes = bpy.context.scene.world.node_tree.nodes
            world_links = bpy.context.scene.world.node_tree.links
            
            for node in world_nodes:
                world_nodes.remove(node)
            
            background = world_nodes.new('ShaderNodeBackground')
            background.inputs['Color'].default_value = (1, 1, 1, 1)
            background.inputs['Strength'].default_value = 1
            
            world_output = world_nodes.new('ShaderNodeOutputWorld')
            world_links.new(background.outputs['Background'], world_output.inputs['Surface'])
            
            # Настраиваем запекание
            bpy.context.scene.render.bake.use_selected_to_active = False
            bpy.context.scene.render.bake.target = 'IMAGE_TEXTURES'
            bpy.context.scene.render.bake.margin = 512
            bpy.context.scene.render.bake.use_clear = True
            bpy.context.scene.render.bake.use_cage = False
            
            # Обрабатываем каждую выбранную текстуру
            for texture_item in selected_textures:
                # Загружаем исходную текстуру
                source_image = bpy.data.images.load(texture_item.filepath)
                
                # Создаем временное изображение для resize and scale
                temp_image = bpy.data.images.new(
                    name="temp_resized",
                    width=source_image.size[0],
                    height=source_image.size[1]
                )
                temp_image.pixels = source_image.pixels[:]
                
                # Применяем resize and scale к временному изображению
                resize_and_scale_image(temp_image)
                
                # Создаем новое изображение для запекания
                image_name = os.path.splitext(os.path.basename(texture_item.filepath))[0]
                
                # Удаляем существующее изображение, если оно есть
                if image_name in bpy.data.images:
                    bpy.data.images.remove(bpy.data.images[image_name])
                
                # Создаем новое изображение того же размера
                bake_image = bpy.data.images.new(
                    name=image_name,
                    width=temp_image.size[0],
                    height=temp_image.size[1]
                )
                
                # Создаем базовый материал для запекания
                if "temp_bake_material" in bpy.data.materials:
                    bpy.data.materials.remove(bpy.data.materials["temp_bake_material"])
                bake_material = bpy.data.materials.new(name="temp_bake_material")
                bake_material.use_nodes = True
                temp_obj.data.materials.clear()
                temp_obj.data.materials.append(bake_material)
                
                # Очищаем ноды материала
                nodes = bake_material.node_tree.nodes
                links = bake_material.node_tree.links
                nodes.clear()
                
                # Создаем ноды для запекания
                principled = nodes.new('ShaderNodeBsdfPrincipled')
                output = nodes.new('ShaderNodeOutputMaterial')
                tex_image = nodes.new('ShaderNodeTexImage')
                bake_image_node = nodes.new('ShaderNodeTexImage')
                
                # Настраиваем ноды
                tex_image.image = temp_image  # Используем обработанное изображение
                bake_image_node.image = bake_image
                bake_image_node.select = True
                nodes.active = bake_image_node
                
                # Соединяем ноды
                links.new(tex_image.outputs['Color'], principled.inputs['Base Color'])
                links.new(principled.outputs['BSDF'], output.inputs['Surface'])
                
                # Запекаем
                bpy.ops.object.bake(type='DIFFUSE', pass_filter={'COLOR'})
                
                # Накладываем оверлей
                apply_overlay(bake_image, overlay_image)
                
                # Сохраняем результат
                if not bpy.data.filepath:
                    raise ValueError("Файл .blend не сохранен")
                
                blend_name = os.path.splitext(os.path.basename(bpy.data.filepath))[0]
                folder_name = f"{blend_name}_Baked"
                image_path = f"//{folder_name}/{image_name}.png"
                abs_path = bpy.path.abspath(image_path)
                
                # Создаем папку, если её нет
                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                
                # Сохраняем изображение
                bake_image.filepath_raw = abs_path
                bake_image.file_format = 'PNG'
                bake_image.save()
                
                # Удаляем временные данные
                bpy.data.images.remove(source_image)
                bpy.data.images.remove(temp_image)
                bpy.data.images.remove(bake_image)
                
            # Удаляем текстуру оверлея
            bpy.data.images.remove(overlay_image)
            
            self.report({'INFO'}, f"Обработано {len(selected_textures)} текстур")
            return {'FINISHED'}
            
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
            
        finally:
            # Восстанавливаем настройки рендера
            bpy.context.scene.render.engine = old_engine
            bpy.context.scene.cycles.samples = old_samples
            bpy.context.scene.view_settings.exposure = old_film_exposure
            bpy.context.scene.view_settings.gamma = old_film_gamma
            
            # Удаляем временные объекты
            if 'temp_obj' in locals():
                bpy.data.objects.remove(temp_obj, do_unlink=True)
            if 'temp_mesh' in locals():
                bpy.data.meshes.remove(temp_mesh)
            if 'bake_material' in bpy.data.materials:
                bpy.data.materials.remove(bpy.data.materials["temp_bake_material"])
                
            end_time = time.time()
            print(f"Завершение обработки текстур: {time.strftime('%H:%M:%S', time.localtime(end_time))}")
            print(f"Общее время обработки: {format_time(end_time - start_time)}")

class BAKER_PT_TextureList(Panel):
    bl_label = "Запечь рамку 192px на текстуры"
    bl_idname = "BAKER_PT_texture_list"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "AGR_baker"
    bl_options = {'DEFAULT_CLOSED'}
    
    def draw(self, context):
        layout = self.layout
        
        # Кнопка загрузки текстур
        row = layout.row()
        row.operator("uv.load_textures", text="Загрузить текстуры")
        
        # Список текстур
        row = layout.row()
        row.template_list("TEXTURE_UL_list", "", context.scene, "texture_list", context.scene, "texture_list_index")
        
        # Переключатель CPU/GPU
        row = layout.row()
        row.prop(context.scene, "use_gpu", text="Использовать GPU (если доступно)")
        
        # Кнопки для обработки текстур
        row = layout.row()
        row.operator("uv.resize_scale_and_bake", text="Запечь рамку")
#--------------------------------
class BAKER_OT_ReplaceWithLight(bpy.types.Operator):
    """Replaces selected objects with lights"""
    bl_idname = "object.replace_with_light"
    bl_label = "Replace with Light"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        selected_objects = context.selected_objects.copy()  # Создаем копию списка
        if not selected_objects:
            self.report({'WARNING'}, "Нет выбранных объектов")
            return {'CANCELLED'}
        
        scene = context.scene
        light_type = scene.light_replacer_type
        light_power = scene.light_replacer_power
        light_color = scene.light_replacer_color
        spot_size = scene.light_replacer_spot_size
        spot_blend = scene.light_replacer_spot_blend
        area_size = scene.light_replacer_area_size
        area_shape = scene.light_replacer_area_shape
        point_radius = scene.light_replacer_point_radius
        
        for obj in selected_objects:
            # Создаем данные для источника света
            light_data = bpy.data.lights.new(name=f"{obj.name}_Light", type=light_type)
            light_data.energy = light_power
            light_data.color = light_color
            
            # Настройки для разных типов света
            if light_type == 'SPOT':
                light_data.spot_size = spot_size
                light_data.spot_blend = spot_blend
            elif light_type == 'AREA':
                light_data.shape = area_shape
                light_data.size = area_size
                if area_shape == 'RECTANGLE':
                    light_data.size_y = area_size  # Для прямоугольника
            elif light_type == 'POINT':
                light_data.shadow_soft_size = point_radius
            
            # Создаем объект света
            light_object = bpy.data.objects.new(name=f"{obj.name}_Light", object_data=light_data)
            context.collection.objects.link(light_object)
            
            # Копируем трансформации
            light_object.location = obj.location
            light_object.rotation_euler = obj.rotation_euler
            light_object.scale = obj.scale
            
            # Удаляем исходный объект
            bpy.data.objects.remove(obj, do_unlink=True)
        
        self.report({'INFO'}, f"Заменено {len(selected_objects)} объектов на источники света")
        return {'FINISHED'}

class BAKER_PT_light_replacer_panel(bpy.types.Panel):
    """Creates a panel in the 3D Viewport sidebar"""
    bl_label = "Замена объектов на свет"
    bl_idname = "BAKER_PT_Light_replacer_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "AGR_baker"
    bl_options = {'DEFAULT_CLOSED'}
    
    def draw(self, context):
        layout = self.layout
        scene = context.scene
        
        box = layout.box()
        box.label(text="Настройки источника света", icon='LIGHT')
        
        col = box.column(align=True)
        col.prop(scene, "light_replacer_type", text="Тип")
        col.prop(scene, "light_replacer_power", text="Мощность (Вт)")
        col.prop(scene, "light_replacer_color", text="Цвет")
        
        # Специфичные настройки для каждого типа света
        if scene.light_replacer_type == 'SPOT':
            spot_box = layout.box()
            spot_box.label(text="Настройки прожектора", icon='LIGHT_SPOT')
            spot_col = spot_box.column(align=True)
            spot_col.prop(scene, "light_replacer_spot_size", text="Размер конуса")
            spot_col.prop(scene, "light_replacer_spot_blend", text="Размытие")
        elif scene.light_replacer_type == 'AREA':
            area_box = layout.box()
            area_box.label(text="Настройки площадного света", icon='LIGHT_AREA')
            area_col = area_box.column(align=True)
            area_col.prop(scene, "light_replacer_area_shape", text="Форма")
            area_col.prop(scene, "light_replacer_area_size", text="Размер")
        elif scene.light_replacer_type == 'POINT':
            point_box = layout.box()
            point_box.label(text="Настройки точечного света", icon='LIGHT_POINT')
            point_col = point_box.column(align=True)
            point_col.prop(scene, "light_replacer_point_radius", text="Радиус")
        
        layout.separator()
        
        # Информация о выбранных объектах
        selected_count = len(context.selected_objects)
        if selected_count > 0:
            info_box = layout.box()
            info_box.label(text=f"Выбрано объектов: {selected_count}", icon='INFO')
        
        # Кнопка замены
        row = layout.row()
        row.scale_y = 2.0
        if selected_count > 0:
            row.operator("object.replace_with_light", text=f"Заменить {selected_count} объектов")
        else:
            row.enabled = False
            row.operator("object.replace_with_light", text="Выберите объекты для замены")

# ============= AGR Rename Panel =============

class BAKER_PT_agr_rename_panel(Panel):
    """Панель для переименования объектов AGR"""
    bl_label = "AGR_rename"
    bl_idname = "BAKER_PT_agr_rename_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "AGR_baker"
    bl_options = {'DEFAULT_CLOSED'}
    
    def draw(self, context):
        layout = self.layout
        scene = context.scene
        
        # Поле для ввода Address
        box = layout.box()
        box.label(text="Настройки", icon='SETTINGS')
        box.prop(scene, "agr_address", text="Address")
        
        layout.separator()
        
        # Кнопка для переименования основного объекта
        row = layout.row()
        row.scale_y = 1.5
        active_obj = context.active_object
        if active_obj and active_obj.type == 'MESH' and scene.agr_address:
            row.operator("baker.agr_rename_main_object", text="Переименовать основной объект", icon='OBJECT_DATA')
        else:
            row.enabled = False
            if not scene.agr_address:
                row.operator("baker.agr_rename_main_object", text="Введите Address")
            else:
                row.operator("baker.agr_rename_main_object", text="Выберите MESH объект")
        
        layout.separator()
        
        # Кнопка для переименования материалов объекта
        row = layout.row()
        row.scale_y = 1.5
        # Используем poll метод оператора для проверки
        if BAKER_OT_agr_rename_materials.poll(context):
            row.operator("baker.agr_rename_materials", text="Переименовать материалы объекта", icon='MATERIAL')
        else:
            row.enabled = False
            if not active_obj or active_obj.type != 'MESH':
                row.operator("baker.agr_rename_materials", text="Выберите MESH объект")
            else:
                row.operator("baker.agr_rename_materials", text="Объект не соответствует формату")
        
        layout.separator()
        
        # Кнопка для переименования в UCX
        row = layout.row()
        row.scale_y = 1.5
        # Получаем количество выбранных MESH объектов
        selected_count = len([obj for obj in context.selected_objects if obj.type == 'MESH'])
        
        if scene.agr_address and selected_count > 0:
            row.operator("baker.rename_ucx", text=f"Переименовать в UCX ({selected_count})", icon='MESH_CUBE')
        else:
            row.enabled = False
            if not scene.agr_address:
                row.operator("baker.rename_ucx", text="Введите Address")
            else:
                row.operator("baker.rename_ucx", text="Выберите объекты для UCX")
        
        layout.separator()
        
        # Кнопка для переименования текстур
        row = layout.row()
        row.scale_y = 1.5
        if active_obj and active_obj.type == 'MESH' and scene.agr_address:
            # Проверяем, является ли объект Main, Ground, GroundEl или Flora
            obj_name = active_obj.name
            is_valid_type = False
            if obj_name.startswith("SM_"):
                if "_Main" in obj_name or "_Ground" in obj_name or "_Flora" in obj_name:
                    is_valid_type = True
            
            if is_valid_type:
                row.operator("baker.agr_rename_textures", text="Переименовать текстуры", icon='TEXTURE')
            else:
                row.enabled = False
                row.operator("baker.agr_rename_textures", text="Только для Main/Ground/GroundEl/Flora")
        else:
            row.enabled = False
            if not scene.agr_address:
                row.operator("baker.agr_rename_textures", text="Введите Address")
            else:
                row.operator("baker.agr_rename_textures", text="Выберите объект Main/Ground/GroundEl/Flora")
        
        layout.separator()
        
        # Кнопка для переименования GEOJSON
        row = layout.row()
        row.scale_y = 1.5
        if active_obj and active_obj.type == 'MESH' and scene.agr_address:
            # Проверяем, является ли объект Main или Ground
            obj_name = active_obj.name
            is_valid_type = False
            if obj_name.startswith("SM_"):
                if "_Main" in obj_name or "_Ground" in obj_name:
                    is_valid_type = True
            
            if is_valid_type:
                row.operator("baker.agr_rename_geojson", text="Переименовать GEOJSON", icon='FILE_TEXT')
            else:
                row.enabled = False
                row.operator("baker.agr_rename_geojson", text="Только для Main/Ground")
        else:
            row.enabled = False
            if not scene.agr_address:
                row.operator("baker.agr_rename_geojson", text="Введите Address")
            else:
                row.operator("baker.agr_rename_geojson", text="Выберите объект Main/Ground")
        
        layout.separator()
        
        # Кнопка для переименования света
        row = layout.row()
        row.scale_y = 1.5
        if active_obj and active_obj.type == 'EMPTY' and scene.agr_address:
            # Проверяем, есть ли дочерние источники света
            has_lights = False
            for obj in context.scene.objects:
                if obj.type == 'LIGHT' and obj.parent == active_obj:
                    has_lights = True
                    break
            
            if has_lights:
                row.operator("baker.agr_rename_lights", text="Переименовать свет", icon='LIGHT')
            else:
                row.enabled = False
                row.operator("baker.agr_rename_lights", text="Empty без источников света")
        else:
            row.enabled = False
            if not scene.agr_address:
                row.operator("baker.agr_rename_lights", text="Введите Address")
            else:
                row.operator("baker.agr_rename_lights", text="Выберите Empty со светом")
        
        layout.separator()
        
        # Кнопка для распределения по коллекциям
        row = layout.row()
        row.scale_y = 1.5
        if scene.agr_address:
            row.operator("baker.agr_distribute_collections", text="Распределить по коллекциям", icon='GROUP')
        else:
            row.enabled = False
            row.operator("baker.agr_distribute_collections", text="Введите Address")
        
        layout.separator()
        layout.separator()
        
        # Кнопка для переименования всего проекта
        box = layout.box()
        box.label(text="Переименование проекта", icon='FILE_FOLDER')
        row = box.row()
        row.scale_y = 2.0
        if scene.agr_address:
            row.operator("baker.agr_rename_project", text="Переименовать ВЕСЬ ПРОЕКТ", icon='ERROR')
        else:
            row.enabled = False
            row.operator("baker.agr_rename_project", text="Введите Address")

# Глобальная переменная для отслеживания активного экземпляра быстрого режима
_active_quick_mode_instance = None

# Глобальная функция для draw handler
def draw_viewport_hints_callback(operator, context):
    """Callback функция для draw handler"""
    operator.draw_viewport_hints(operator, context)

# Быстрый режим AGR Baker - модальный оператор для горячих клавиш
class BAKER_OT_quick_mode(Operator):
    """Быстрый режим AGR Baker с горячими клавишами и подсказками в viewport"""
    bl_idname = "baker.quick_mode"
    bl_label = "AGR Baker Quick Mode"
    bl_description = "Быстрый режим: колесико мыши - разрешение, Q - запечь, E - сгенерировать"
    bl_options = {'REGISTER'}
    
    def __init__(self):
        self._handle = None
        self._timer = None
        self._is_finished = False  # Флаг для предотвращения повторных вызовов
    
    def modal(self, context, event):
        # Если экземпляр уже завершен, не обрабатываем события
        if self._is_finished:
            return {'FINISHED'}
        
        # Перерисовываем viewport для обновления подсказок
        context.area.tag_redraw()
        
        # Колесико мыши - изменение разрешения
        if event.type == 'WHEELUPMOUSE':
            if self.change_resolution(context, 1):
                pass  # Подсказки покажут новое разрешение
            return {'RUNNING_MODAL'}
        elif event.type == 'WHEELDOWNMOUSE':
            if self.change_resolution(context, -1):
                pass  # Подсказки покажут новое разрешение
            return {'RUNNING_MODAL'}
        
        # Q - быстрое запекание с модификаторами
        elif event.type == 'Q' and event.value == 'PRESS':
            # Проверяем модификаторы
            disable_normal = event.alt  # Alt - отключить запекание нормалей
            simple_baking = event.ctrl  # Ctrl - простое запекание
            bake_with_alpha = event.shift  # Shift - запечь с альфа-каналом
            
            if self.quick_bake(context, disable_normal, simple_baking, bake_with_alpha):
                self.finish_modal(context)
                return {'FINISHED'}  # Завершаем режим после запекания
            return {'RUNNING_MODAL'}
        
        # E - быстрая генерация с модификаторами
        elif event.type == 'E' and event.value == 'PRESS':
            # Проверяем модификаторы
            generate_all = event.shift  # Shift - все материалы
            overwrite = event.ctrl      # Ctrl - перезаписать существующие
            resize_256 = event.alt      # Alt - переразмерять до 256px
            
            if self.quick_generate(context, generate_all, overwrite, resize_256):
                self.finish_modal(context)
                return {'FINISHED'}  # Завершаем режим после генерации
            return {'RUNNING_MODAL'}
        
        # WASD - управление параметрами запекания
        elif event.type == 'W' and event.value == 'PRESS':
            # W - увеличить max ray distance на 0.005
            self.change_ray_distance(context, 0.01)
            return {'RUNNING_MODAL'}
        elif event.type == 'S' and event.value == 'PRESS':
            # S - уменьшить max ray distance на 0.005
            self.change_ray_distance(context, -0.01)
            return {'RUNNING_MODAL'}
        elif event.type == 'A' and event.value == 'PRESS':
            # A - уменьшить extrusion на 0.005
            self.change_extrusion(context, -0.01)
            return {'RUNNING_MODAL'}
        elif event.type == 'D' and event.value == 'PRESS':
            # D - увеличить extrusion на 0.005
            self.change_extrusion(context, 0.01)
            return {'RUNNING_MODAL'}
        
        # ESC, правый клик или левый клик - выход из режима
        elif event.type in {'ESC', 'RIGHTMOUSE'}:
            self.finish_modal(context)
            self.report({'INFO'}, "Быстрый режим AGR Baker отключен")
            return {'CANCELLED'}
        elif event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            self.finish_modal(context)
            self.report({'INFO'}, "Быстрый режим AGR Baker отключен")
            return {'CANCELLED'}
        
        # Все остальные события передаем дальше
        return {'PASS_THROUGH'}
    
    def invoke(self, context, event):
        global _active_quick_mode_instance
        
        # Закрываем предыдущий экземпляр, если он активен
        if _active_quick_mode_instance is not None:
            try:
                # Сначала помечаем как завершенный
                _active_quick_mode_instance._is_finished = True
                # Затем очищаем обработчики
                _active_quick_mode_instance.finish_modal(context)
                self.report({'INFO'}, "Предыдущий быстрый режим закрыт")
            except Exception as e:
                print(f"Ошибка при закрытии предыдущего быстрого режима: {e}")
                # Принудительно очищаем ссылку
                _active_quick_mode_instance = None
        
        if context.area.type == 'VIEW_3D':
            # Устанавливаем себя как активный экземпляр
            _active_quick_mode_instance = self
            
            # Добавляем обработчик рисования подсказок
            args = (self, context)
            self._handle = bpy.types.SpaceView3D.draw_handler_add(
                draw_viewport_hints_callback, args, 'WINDOW', 'POST_PIXEL')
            
            # Добавляем таймер для обновления
            self._timer = context.window_manager.event_timer_add(0.1, window=context.window)
            
            context.window_manager.modal_handler_add(self)
            self.report({'INFO'}, f"Быстрый режим AGR Baker активен | Разрешение: {context.scene.baker_resolution}px")
            return {'RUNNING_MODAL'}
        else:
            self.report({'WARNING'}, "Быстрый режим работает только в 3D Viewport")
            return {'CANCELLED'}
    
    def finish_modal(self, context):
        """Очищает обработчики при завершении режима"""
        global _active_quick_mode_instance
        
        # Устанавливаем флаг завершения, чтобы modal больше не обрабатывал события
        self._is_finished = True
        
        if self._handle:
            bpy.types.SpaceView3D.draw_handler_remove(self._handle, 'WINDOW')
            self._handle = None
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        
        # Очищаем глобальную ссылку, если это мы
        if _active_quick_mode_instance == self:
            _active_quick_mode_instance = None
    
    def set_blf_size(self, font_id, font_size):
        """Устанавливает размер шрифта с совместимостью для разных версий Blender"""
        try:
            # Для Blender 4.2+ (новый API)
            blf.size(font_id, font_size)
        except TypeError:
            # Для старых версий Blender (старый API)
            blf.size(font_id, font_size, 72)
    
    def draw_viewport_hints(self, operator, context):
        """Рисует подсказки в правом нижнем углу viewport"""
        # Если экземпляр завершен, не рисуем подсказки
        if self._is_finished:
            return
        
        # Получаем размеры области
        region = context.region
        width = region.width
        height = region.height
        
        # Настройки шрифта
        font_id = 0
        font_size = 18  # Увеличили с 14 до 18
        line_height = 24  # Увеличили с 18 до 24 для лучшего интервала
        
        # Позиция для текста (правый нижний угол с отступами)
        x_offset = width - 400  # Увеличили с 350 до 400 для большего шрифта
        y_offset = 50           # Увеличили отступ снизу с 40 до 50
        
        # Цвета
        bg_color = (0.0, 0.0, 0.0, 0.8)  # Полупрозрачный черный фон
        text_color = (1.0, 1.0, 1.0, 1.0)  # Белый текст
        accent_color = (0.3, 0.8, 1.0, 1.0)  # Синий акцент
        
        # Подготавливаем текст подсказок
        scene = context.scene
        current_res = scene.baker_resolution
        current_ray_distance = scene.baker_max_ray_distance
        current_extrusion = scene.baker_extrusion
        
        hints = [
            ("AGR Baker", accent_color),
            ("", text_color),
            (f"Разрешение: {current_res}px", text_color),
            (f"Max Ray Distance: {current_ray_distance:.3f}", text_color),
            (f"Extrusion: {current_extrusion:.3f}", text_color),
            ("", text_color),
            ("Управление:", accent_color),
            ("", text_color),
            ("Колесико - разрешение", text_color),
            ("W/S - Ray Distance ±0.01", text_color),
            ("A/D - Extrusion ±0.01", text_color),
            ("", text_color),
            ("Q - запечь с выбранных", text_color),
            ("Shift+Q - запечь с альфой", text_color),
            ("Alt+Q - запечь без нормалей", text_color),
            ("Ctrl+Q - запечь из материала", text_color),
            ("", text_color),
            ("E - генерация из материала", text_color),
            ("Shift+E - все материалы", text_color),
            ("Ctrl+E - перезапись", text_color),
            ("Alt+E - переразмерить 256px", text_color),
            ("", text_color),
            ("ESC/ПКМ/ЛКМ - выход", text_color),
        ]
        
        # Настраиваем размер шрифта перед вычислением размеров
        self.set_blf_size(font_id, font_size)
        
        # Вычисляем размер фона
        max_width = max([blf.dimensions(font_id, text)[0] for text, _ in hints if text])
        bg_width = max_width + 30  # Увеличили отступы с 20 до 30
        bg_height = len(hints) * line_height + 30  # Увеличили отступы с 20 до 30
        
        # Корректируем позицию, чтобы фон не выходил за границы
        if x_offset + bg_width > width:
            x_offset = width - bg_width - 10
        if y_offset + bg_height > height:
            y_offset = height - bg_height - 10
        
        # Пробуем нарисовать полупрозрачный фон
        try:
            self.draw_background_rect(x_offset - 15, y_offset - 15, bg_width, bg_height, bg_color)
        except:
            # Если не получается нарисовать фон, просто продолжаем без него
            pass
        
        # Рисуем текст
        self.set_blf_size(font_id, font_size)
        
        current_y = y_offset + bg_height - 40  # Увеличили отступ сверху с 30 до 40
        for text, color in hints:
            if text:  # Пропускаем пустые строки для отступов
                blf.color(font_id, *color)
                blf.position(font_id, x_offset, current_y, 0)
                blf.draw(font_id, text)
            current_y -= line_height
    
    def draw_background_rect(self, x, y, width, height, color):
        """Рисует простой прямоугольник для фона подсказок"""
        try:
            # Создаем вершины прямоугольника
            vertices = [
                (x, y),
                (x + width, y),
                (x + width, y + height),
                (x, y + height)
            ]
            
            indices = [(0, 1, 2), (2, 3, 0)]
            
            # Пробуем разные названия шейдеров для совместимости
            shader = None
            shader_names = ['UNIFORM_COLOR', 'FLAT_COLOR', '2D_UNIFORM_COLOR']
            
            for shader_name in shader_names:
                try:
                    shader = gpu.shader.from_builtin(shader_name)
                    break
                except ValueError:
                    continue
            
            if shader is None:
                # Если не удалось получить шейдер, просто пропускаем отрисовку фона
                return
            
            batch = batch_for_shader(shader, 'TRIS', {"pos": vertices}, indices=indices)
            
            # Включаем альфа-блендинг
            gpu.state.blend_set('ALPHA')
            
            shader.bind()
            shader.uniform_float("color", color)
            batch.draw(shader)
            
            # Отключаем блендинг
            gpu.state.blend_set('NONE')
            
        except Exception as e:
            # В случае любой ошибки просто пропускаем отрисовку фона
            # Текст все равно будет виден
            pass
    
    def change_resolution(self, context, direction):
        """Изменяет разрешение в зависимости от направления прокрутки"""
        current_res = context.scene.baker_resolution
        resolution_values = [item[0] for item in resolutions]
        
        try:
            current_index = resolution_values.index(current_res)
            new_index = current_index + direction
            
            # Ограничиваем индекс в пределах списка
            new_index = max(0, min(len(resolution_values) - 1, new_index))
            
            if new_index != current_index:
                context.scene.baker_resolution = resolution_values[new_index]
                return True
            
        except ValueError:
            context.scene.baker_resolution = '2048'
            return True
        
        return False
    
    def change_ray_distance(self, context, delta):
        """Изменяет max ray distance на указанное значение"""
        scene = context.scene
        current_value = scene.baker_max_ray_distance
        new_value = max(0.0, current_value + delta)  # Не даем уйти в минус
        scene.baker_max_ray_distance = new_value
        return True
    
    def change_extrusion(self, context, delta):
        """Изменяет extrusion на указанное значение"""
        scene = context.scene
        current_value = scene.baker_extrusion
        new_value = max(0.0, current_value + delta)  # Не даем уйти в минус
        scene.baker_extrusion = new_value
        return True
    
    def quick_bake(self, context, disable_normal=False, simple_baking=False, bake_with_alpha=False):
        """Быстрое запекание с принудительным управлением нормалями и режимом"""
        scene = context.scene
        
        # Проверяем возможность запекания
        if not context.active_object:
            self.report({'ERROR'}, "Нет активного объекта")
            return False
        
        # В простом режиме достаточно одного активного объекта
        if simple_baking:
            if not context.active_object.material_slots:
                self.report({'ERROR'}, "У активного объекта нет материалов")
                return False
        else:
            if len(context.selected_objects) < 2:
                self.report({'ERROR'}, "Выберите объекты для запекания")
                return False
        
        # Сохраняем текущее состояние настройки нормалей и альфы
        original_bake_normal = scene.baker_bake_normal_enabled
        original_bake_with_alpha = scene.baker_bake_with_alpha
        
        # Принудительно устанавливаем нужное состояние нормалей
        if disable_normal:
            scene.baker_bake_normal_enabled = False  # Alt+Q - без нормалей
        else:
            scene.baker_bake_normal_enabled = True   # Q - всегда с нормалями
        
        # Принудительно устанавливаем состояние альфы (игнорируем настройки сцены)
        if bake_with_alpha:
            scene.baker_bake_with_alpha = True   # Shift+Q - с альфой
        else:
            scene.baker_bake_with_alpha = False  # Q - без альфы
        
        try:
            # Запускаем запекание с настройками
            bpy.ops.baker.bake_textures(
                resolution=scene.baker_resolution,
                connection_mode=scene.baker_connection_mode,
                normal_type=scene.baker_normal_type,
                max_ray_distance=scene.baker_max_ray_distance,
                extrusion=scene.baker_extrusion,
                simple_baking=simple_baking,  # Используем параметр из горячей клавиши, а не настройки
                bake_with_alpha=scene.baker_bake_with_alpha  # Используем настройку из сцены
            )
            
            # Формируем информативное сообщение
            options = []
            if disable_normal:
                options.append("без нормалей")
            if simple_baking:
                options.append("простое запекание")
            if bake_with_alpha:
                options.append("с альфой")
            
            options_text = f" ({', '.join(options)})" if options else " (с нормалями)"
            self.report({'INFO'}, f"Запекание запущено с разрешением {scene.baker_resolution}px{options_text}")
            return True
            
        finally:
            # Восстанавливаем исходное состояние настройки нормалей и альфы
            scene.baker_bake_normal_enabled = original_bake_normal
            scene.baker_bake_with_alpha = original_bake_with_alpha
    
    def quick_generate(self, context, generate_all=None, overwrite=None, resize_256=None):
        """Быстрая генерация с опциональными модификаторами"""
        scene = context.scene
        obj = context.active_object
        
        # Проверяем возможность генерации
        if not obj:
            self.report({'ERROR'}, "Нет активного объекта")
            return False
        
        if obj.type != 'MESH':
            self.report({'ERROR'}, "Объект должен быть типа MESH")
            return False
        
        # Определяем настройки генерации
        # Если параметры не переданы явно, используем базовые настройки (False)
        if generate_all is None:
            generate_all = False  # По умолчанию только активный материал
        if overwrite is None:
            overwrite = False     # По умолчанию не перезаписывать
        if resize_256 is None:
            resize_256 = False    # По умолчанию не переразмеривать
        
        # Проверяем наличие материалов с нодами
        has_materials = False
        if generate_all:
            for slot in obj.material_slots:
                if slot.material and slot.material.use_nodes:
                    has_materials = True
                    break
        else:
            if obj.active_material and obj.active_material.use_nodes:
                has_materials = True
        
        if not has_materials:
            self.report({'ERROR'}, "Нет подходящих материалов с нодами")
            return False
        
        # Запускаем генерацию с настройками
        bpy.ops.baker.generate_from_material(
            resolution=scene.baker_resolution,
            connection_mode=scene.baker_connection_mode,
            normal_type=scene.baker_normal_type,
            generate_all_materials=generate_all,
            overwrite_existing=overwrite,
            resize_textures_256=resize_256
        )
        
        # Формируем информативное сообщение
        options = []
        if generate_all:
            options.append("все материалы")
        if overwrite:
            options.append("перезапись")
        if resize_256:
            options.append("256px")
        
        options_text = f" ({', '.join(options)})" if options else ""
        self.report({'INFO'}, f"Генерация запущена с разрешением {scene.baker_resolution}px{options_text}")
        return True


#--------------------------------
def register():
    # Сначала убеждаемся, что Python пути настроены
    ensure_python_paths()
    
    # Пытаемся переимпортировать scipy после настройки путей
    global SCIPY_AVAILABLE, ndimage
    if not SCIPY_AVAILABLE:
        try:
            # Принудительно очищаем кэш модулей для повторного импорта
            import sys
            modules_to_reload = [mod for mod in sys.modules.keys() if mod.startswith('scipy') or mod.startswith('numpy')]
            for mod in modules_to_reload:
                if mod in sys.modules:
                    del sys.modules[mod]
            
            # Пытаемся импортировать заново
            from scipy import ndimage
            SCIPY_AVAILABLE = True
            print("🔬 SciPy теперь доступна после настройки путей!")
        except ImportError as e:
            print(f"📝 SciPy все еще недоступна: {e}")
            print("💡 Аддон будет работать без SciPy (с базовым ресайзом текстур)")
        except Exception as e:
            print(f"⚠️ Ошибка при попытке загрузки SciPy: {e}")
    global HAS_CUDA, cupy, cp
    if not HAS_CUDA:
        try:
            # Принудительно очищаем кэш модулей для повторного импорта
            import sys
            modules_to_reload = [mod for mod in sys.modules.keys() if mod.startswith('cupy') or mod.startswith('cp')]
            for mod in modules_to_reload:
                if mod in sys.modules:
                    del sys.modules[mod]
            
            # Пытаемся импортировать заново
            import cupy as cp
            HAS_CUDA = True
            print("🔬 CUDA теперь доступна после настройки путей!")
        except ImportError as e:
            print(f"📝 CUDA все еще недоступна: {e}")
            print("💡 Аддон будет работать без SciPy (с базовым ресайзом текстур)")
        except Exception as e:
            print(f"⚠️ Ошибка при попытке загрузки SciPy: {e}")

    
    # ВАЖНО: Сначала регистрируем PropertyGroup классы
    try:
        bpy.utils.register_class(BAKER_TextureSet)
    except Exception as e:
        print(f"Ошибка регистрации BAKER_TextureSet: {e}")
    
    try:
        bpy.utils.register_class(BAKER_AtlasData)
    except Exception as e:
        print(f"Ошибка регистрации BAKER_AtlasData: {e}")
    
    try:
        bpy.utils.register_class(BAKER_UdimMaterial)
    except Exception as e:
        print(f"Ошибка регистрации BAKER_UdimMaterial: {e}")
    try:
        bpy.utils.register_class(BAKER_ObjectMaterialIndices)
    except Exception as e:
        print(f"Ошибка регистрации BAKER_ObjectMaterialIndices: {e}")
    try:
        bpy.utils.register_class(TextureItem)
    except Exception as e:
        print(f"Ошибка регистрации TextureItem: {e}")
    try:
        bpy.utils.register_class(TEXTURE_UL_list)
    except Exception as e:
        print(f"Ошибка регистрации TEXTURE_UL_list: {e}")
    
    # Теперь регистрируем операторы
    operators = [
        BAKER_OT_bake_textures,
        BAKER_OT_create_atlas,
        BAKER_OT_create_udim,
        BAKER_OT_scan_materials_for_udim,
        BAKER_OT_refresh_texture_sets,
        BAKER_OT_generate_from_material,
        BAKER_OT_preview_atlas_layout,
        BAKER_OT_restore_materials_from_atlas,
        BAKER_OT_rename_ucx,
        BAKER_OT_ucx_select_type,
        BAKER_OT_ucx_input_number,
        BAKER_OT_agr_rename_main_object,
        BAKER_OT_agr_select_type,
        BAKER_OT_agr_input_number,
        BAKER_OT_agr_rename_materials,
        BAKER_OT_agr_glass_select_quality,
        BAKER_OT_agr_glass_input_number,
        BAKER_OT_agr_rename_geojson,
        BAKER_OT_agr_rename_textures,
        BAKER_OT_agr_rename_lights,
        BAKER_OT_agr_lights_input_number,
        BAKER_OT_agr_distribute_collections,
        BAKER_OT_agr_distribute_input_lowpoly_number,
        BAKER_OT_agr_rename_project,
        BAKER_OT_agr_rename_project_input_lowpoly_number,
        BAKER_OT_revert_udim_uvs,
        BAKER_OT_select_texture_sets_for_selected_objects,
        BAKER_OT_invert_texture_sets_selection,
        BAKER_OT_toggle_select_all_texture_sets,
        BAKER_OT_delete_selected_texture_sets,
        BAKER_OT_create_atlas_flora,
        BAKER_OT_ScaleAndBake,
        BAKER_OT_LoadTextures,
        BAKER_OT_ResizeAndScale,
        BAKER_OT_ResizeScaleAndBake,
        BAKER_OT_ReplaceWithLight,
        BAKER_OT_quick_mode
    ]
    
    for op in operators:
        try:
            bpy.utils.register_class(op)
        except Exception as e:
            print(f"Ошибка регистрации {op.__name__}: {e}")
    
    # Регистрируем панели и меню
    panels = [
        BAKER_PT_panel,
        BAKER_PT_material_generation_panel,
        BAKER_PT_atlas_panel,
        BAKER_PT_udim_panel,
        BAKER_PT_atlas_panel_flora,
        BAKER_PT_agr_rename_panel,
        BAKER_PT_TextureList,
        BAKER_PT_light_replacer_panel
    ]
    
    for panel in panels:
        try:
            bpy.utils.register_class(panel)
        except Exception as e:
            print(f"Ошибка регистрации {panel.__name__}: {e}")
    
    # Добавляем привязку клавиш Alt+2 для быстрого режима
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc:
        # Привязка для Object Mode
        km_object = kc.keymaps.new(name='Object Mode', space_type='EMPTY')
        kmi_object = km_object.keymap_items.new('baker.quick_mode', 'TWO', 'PRESS', alt=True)
        
        # Привязка для Edit Mode
        km_edit = kc.keymaps.new(name='Mesh', space_type='EMPTY')
        kmi_edit = km_edit.keymap_items.new('baker.quick_mode', 'TWO', 'PRESS', alt=True)
    
    bpy.types.Scene.baker_resolution = EnumProperty(
        name="Resolution",
        description="Texture resolution",
        items=resolutions,
        default='2048'
    )
    bpy.types.Scene.baker_connection_mode = EnumProperty(
        name="Connection Mode",
        description="Mode for connecting textures to material",
        items=connection_modes,
        default='HIGH'
    )
    bpy.types.Scene.baker_normal_type = EnumProperty(
        name="Normal Type",
        description="Type of normal map to generate",
        items=normal_types,
        default='OPENGL'
    )
    bpy.types.Scene.baker_bake_normal_enabled = BoolProperty(
        name="Bake Normal",
        description="Bake normals from high-poly object or create flat normal",
        default=True
    )
    bpy.types.Scene.baker_bake_with_alpha = BoolProperty(
        name="Bake with Alpha",
        description="Bake diffuse with alpha channel (opacity)",
        default=False
    )
    bpy.types.Scene.baker_simple_baking = BoolProperty(
        name="Simple Baking",
        description="Use simple baking mode (no selected to active, bake on active object only)",
        default=False
    )
    bpy.types.Scene.baker_max_ray_distance = FloatProperty(
        name="Max Ray Distance",
        description="Maximum ray distance for baking",
        default=0.0,
        min=0.0,
        max=100.0
    )
    bpy.types.Scene.baker_extrusion = FloatProperty(
        name="Extrusion",
        description="Extrusion value for cage",
        default=0.5,
        min=0.0,
        max=10.0
    )
    
    bpy.types.Scene.baker_atlas_type = EnumProperty(
        name="Atlas Type",
        description="Type of atlas to create",
        items=[
            ('HIGH', "HIGH", "Атлас с ERM и DIFFUSE_OPACITY картами"),
            ('LOW', "LOW", "Атлас с отдельными картами DIFFUSE, METALLIC, ROUGHNESS, OPACITY, NORMAL")
        ],
        default='HIGH'
    )
    bpy.types.Scene.baker_atlas_size = EnumProperty(
        name="Atlas Size",
        description="Size of the atlas texture",
        items=atlas_sizes,
        default='2048'
    )
    
    bpy.types.Scene.baker_generate_all_materials = BoolProperty(
        name="Все материалы",
        description="Генерировать текстуры для всех материалов на объекте",
        default=False
    )
    bpy.types.Scene.light_replacer_type = bpy.props.EnumProperty(
        name="Тип света",
        description="Выберите тип источника света",
        items=[
            ('POINT', "Точечный", "Точечный источник света"),
            ('SUN', "Солнце", "Солнечный свет"),
            ('SPOT', "Прожектор", "Направленный прожектор"),
            ('AREA', "Площадный", "Площадный источник света")
        ],
        default='POINT'
    )
    bpy.types.Scene.light_replacer_power = bpy.props.FloatProperty(
        name="Мощность (Вт)",
        description="Установите мощность света в ваттах",
        default=100.0,
        min=0.0,
        soft_max=10000.0
    )
    bpy.types.Scene.light_replacer_color = bpy.props.FloatVectorProperty(
        name="Цвет",
        description="Установите цвет света",
        subtype='COLOR',
        default=(1.0, 1.0, 1.0),
        min=0.0,
        max=1.0
    )
    bpy.types.Scene.light_replacer_spot_size = bpy.props.FloatProperty(
        subtype='ANGLE',
        name="Размер конуса",
        description="Установите размер конуса прожектора",
        default=0.785398,  # 45 градусов в радианах
        min=0.017453,      # 1 градус
        max=3.141593       # 180 градусов
    )
    bpy.types.Scene.light_replacer_spot_blend = bpy.props.FloatProperty(
        name="Размытие",
        description="Установите размытие краев прожектора",
        default=0.15,
        min=0.0,
        max=1.0
    )
    bpy.types.Scene.light_replacer_area_size = bpy.props.FloatProperty(
        name="Размер",
        description="Установите размер площадного света",
        default=1.0,
        min=0.01,
        max=100.0
    )
    bpy.types.Scene.light_replacer_area_shape = bpy.props.EnumProperty(
        name="Форма",
        description="Выберите форму площадного света",
        items=[
            ('SQUARE', "Квадрат", "Квадратный площадный свет"), 
            ('RECTANGLE', "Прямоугольник", "Прямоугольный площадный свет"),
            ('DISK', "Диск", "Круглый площадный свет"),
            ('ELLIPSE', "Эллипс", "Эллиптический площадный свет")
        ],
        default='SQUARE'
    )
    bpy.types.Scene.light_replacer_point_radius = bpy.props.FloatProperty(
        name="Радиус",
        description="Установите радиус точечного света для мягких теней",
        default=0.1,
        min=0.0,
        max=10.0
    )
    
    # AGR Rename properties
    bpy.types.Scene.agr_address = StringProperty(
        name="Address",
        description="Адрес для переименования объектов",
        default=""
    )
    bpy.types.Scene.agr_selected_type = StringProperty(
        name="Selected Type",
        description="Выбранный тип объекта (внутреннее использование)",
        default=""
    )
    bpy.types.Scene.agr_ucx_type = StringProperty(
        name="UCX Type",
        description="Выбранный тип для UCX коллизий (внутреннее использование)",
        default=""
    )
    bpy.types.Scene.agr_ucx_objects = StringProperty(
        name="UCX Objects",
        description="Количество UCX объектов (внутреннее использование)",
        default=""
    )
    bpy.types.Scene.agr_lights_type = StringProperty(
        name="Lights Type",
        description="Тип для переименования света (внутреннее использование)",
        default=""
    )
    bpy.types.Scene.agr_glass_obj_type = StringProperty(
        name="Glass Object Type",
        description="Тип стеклянного объекта (внутреннее использование)",
        default=""
    )
    bpy.types.Scene.agr_glass_address = StringProperty(
        name="Glass Address",
        description="Адрес для стеклянного объекта (внутреннее использование)",
        default=""
    )
    bpy.types.Scene.agr_glass_number = IntProperty(
        name="Glass Number",
        description="Номер для стеклянного объекта (внутреннее использование)",
        default=0
    )
    bpy.types.Scene.agr_lowpoly_number = StringProperty(
        name="Lowpoly Number",
        description="4-значный номер для lowpoly коллекций (внутреннее использование)",
        default=""
    )
    bpy.types.Scene.agr_project_lowpoly_number = StringProperty(
        name="Project Lowpoly Number",
        description="4-значный номер для lowpoly при переименовании проекта (внутреннее использование)",
        default=""
    )
    
    bpy.types.Scene.baker_overwrite_existing = BoolProperty(
        name="Перезаписать существующие",
        description="Перезаписать уже существующие запеченные или сгенерированные текстуры",
        default=False
    )
    
    bpy.types.Scene.resize_textures_256 = BoolProperty(
        name="Переразмерять текстуры 256px",
        description="Переразмерять текстуры 256x256 пикселей",
        default=False
    )

    bpy.types.Scene.baker_texture_sets_collapsed = BoolProperty(
        name="Показать наборы текстур",
        description="Показать/скрыть список доступных наборов текстур",
        default=True
    )

    bpy.types.Scene.baker_main_texture_sets_collapsed = BoolProperty(
        name="Показать наборы текстур в основной панели",
        description="Показать/скрыть список доступных наборов текстур в основной панели",
        default=False
    )

    bpy.types.Scene.baker_udim_materials_collapsed = BoolProperty(
        name="Показать материалы UDIM",
        description="Показать/скрыть список материалов для UDIM",
        default=True
    )

    has_gpu, device, memory = check_gpu()
    bpy.types.Scene.texture_list = CollectionProperty(type=TextureItem)
    bpy.types.Scene.texture_list_index = bpy.props.IntProperty()
    bpy.types.Scene.use_gpu = bpy.props.BoolProperty(
        name="Use GPU",
        description="Use GPU for texture processing if available",
        default=has_gpu
    )
    # ВАЖНО: CollectionProperty создаем в самом конце, после регистрации всех PropertyGroup классов
    print("📝 Создание CollectionProperty...")
    try:
        bpy.types.Scene.baker_texture_sets = CollectionProperty(type=BAKER_TextureSet)
        print("✅ baker_texture_sets создан")
    except Exception as e:
        print(f"❌ Ошибка создания baker_texture_sets: {e}")
    
    try:
        bpy.types.Scene.baker_atlases = CollectionProperty(type=BAKER_AtlasData)
        print("✅ baker_atlases создан")
    except Exception as e:
        print(f"❌ Ошибка создания baker_atlases: {e}")
    
    try:
        bpy.types.Scene.baker_udim_materials = CollectionProperty(type=BAKER_UdimMaterial)
        print("✅ baker_udim_materials создан")
    except Exception as e:
        print(f"❌ Ошибка создания baker_udim_materials: {e}")

    try:
        bpy.types.Scene.baker_object_material_indices = CollectionProperty(type=BAKER_ObjectMaterialIndices)
        print("✅ baker_object_material_indices создан")
    except Exception as e:
        print(f"❌ Ошибка создания baker_object_material_indices: {e}")
    
    print("🎉 Регистрация аддона завершена!")

def unregister():
    global _active_quick_mode_instance
    
    # Закрываем активный быстрый режим, если он есть
    if _active_quick_mode_instance is not None:
        try:
            # Помечаем как завершенный
            _active_quick_mode_instance._is_finished = True
            # Очищаем обработчики
            _active_quick_mode_instance.finish_modal(bpy.context)
        except Exception as e:
            print(f"Ошибка при закрытии быстрого режима в unregister: {e}")
        finally:
            # В любом случае очищаем ссылку
            _active_quick_mode_instance = None
    
    # Удаляем привязки клавиш
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc:
        # Удаляем привязки для Object Mode
        km_object = kc.keymaps.get('Object Mode')
        if km_object:
            for kmi in km_object.keymap_items:
                if kmi.idname == 'baker.quick_mode':
                    km_object.keymap_items.remove(kmi)
        
        # Удаляем привязки для Edit Mode
        km_edit = kc.keymaps.get('Mesh')
        if km_edit:
            for kmi in km_edit.keymap_items:
                if kmi.idname == 'baker.quick_mode':
                    km_edit.keymap_items.remove(kmi)
    
    del bpy.types.Scene.baker_resolution
    del bpy.types.Scene.baker_connection_mode
    del bpy.types.Scene.baker_normal_type
    del bpy.types.Scene.baker_bake_normal_enabled
    del bpy.types.Scene.baker_bake_with_alpha
    del bpy.types.Scene.baker_simple_baking
    del bpy.types.Scene.baker_max_ray_distance
    del bpy.types.Scene.baker_extrusion
    del bpy.types.Scene.baker_atlas_type
    del bpy.types.Scene.baker_atlas_size
    del bpy.types.Scene.baker_generate_all_materials
    del bpy.types.Scene.baker_overwrite_existing
    del bpy.types.Scene.resize_textures_256
    del bpy.types.Scene.baker_texture_sets_collapsed
    del bpy.types.Scene.baker_main_texture_sets_collapsed
    del bpy.types.Scene.baker_udim_materials_collapsed
    del bpy.types.Scene.baker_texture_sets
    del bpy.types.Scene.baker_atlases
    del bpy.types.Scene.baker_udim_materials
    del bpy.types.Scene.baker_object_material_indices
    del bpy.types.Scene.texture_list_index
    del bpy.types.Scene.texture_list
    del bpy.types.Scene.use_gpu
    del bpy.types.Scene.light_replacer_type
    del bpy.types.Scene.light_replacer_power
    del bpy.types.Scene.light_replacer_color
    del bpy.types.Scene.light_replacer_spot_size
    del bpy.types.Scene.light_replacer_spot_blend
    del bpy.types.Scene.light_replacer_area_size
    del bpy.types.Scene.light_replacer_area_shape
    del bpy.types.Scene.light_replacer_point_radius
    
    # AGR Rename properties
    del bpy.types.Scene.agr_address
    del bpy.types.Scene.agr_selected_type
    del bpy.types.Scene.agr_ucx_type
    del bpy.types.Scene.agr_ucx_objects
    del bpy.types.Scene.agr_lights_type
    del bpy.types.Scene.agr_glass_obj_type
    del bpy.types.Scene.agr_glass_address
    del bpy.types.Scene.agr_glass_number
    del bpy.types.Scene.agr_lowpoly_number
    del bpy.types.Scene.agr_project_lowpoly_number
    
    bpy.utils.unregister_class(BAKER_PT_panel)
    bpy.utils.unregister_class(BAKER_PT_material_generation_panel)
    bpy.utils.unregister_class(BAKER_PT_atlas_panel)
    bpy.utils.unregister_class(BAKER_PT_udim_panel)
    bpy.utils.unregister_class(BAKER_PT_TextureList)
    bpy.utils.unregister_class(BAKER_PT_light_replacer_panel)
    bpy.utils.unregister_class(BAKER_PT_agr_rename_panel)
    
    bpy.utils.unregister_class(BAKER_OT_bake_textures)
    bpy.utils.unregister_class(BAKER_OT_create_atlas)
    bpy.utils.unregister_class(BAKER_OT_create_udim)
    bpy.utils.unregister_class(BAKER_OT_scan_materials_for_udim)
    bpy.utils.unregister_class(BAKER_OT_refresh_texture_sets)
    bpy.utils.unregister_class(BAKER_OT_generate_from_material)
    bpy.utils.unregister_class(BAKER_OT_preview_atlas_layout)
    bpy.utils.unregister_class(BAKER_OT_restore_materials_from_atlas)
    bpy.utils.unregister_class(BAKER_OT_rename_ucx)
    bpy.utils.unregister_class(BAKER_OT_ucx_select_type)
    bpy.utils.unregister_class(BAKER_OT_ucx_input_number)
    bpy.utils.unregister_class(BAKER_OT_agr_rename_main_object)
    bpy.utils.unregister_class(BAKER_OT_agr_select_type)
    bpy.utils.unregister_class(BAKER_OT_agr_input_number)
    bpy.utils.unregister_class(BAKER_OT_agr_rename_materials)
    bpy.utils.unregister_class(BAKER_OT_agr_glass_select_quality)
    bpy.utils.unregister_class(BAKER_OT_agr_glass_input_number)
    bpy.utils.unregister_class(BAKER_OT_agr_rename_geojson)
    bpy.utils.unregister_class(BAKER_OT_agr_rename_textures)
    bpy.utils.unregister_class(BAKER_OT_agr_rename_lights)
    bpy.utils.unregister_class(BAKER_OT_agr_lights_input_number)
    bpy.utils.unregister_class(BAKER_OT_agr_distribute_collections)
    bpy.utils.unregister_class(BAKER_OT_agr_rename_project)
    bpy.utils.unregister_class(BAKER_OT_agr_rename_project_input_lowpoly_number)
    bpy.utils.unregister_class(BAKER_OT_revert_udim_uvs)
    bpy.utils.unregister_class(BAKER_OT_select_texture_sets_for_selected_objects)
    bpy.utils.unregister_class(BAKER_OT_invert_texture_sets_selection)
    bpy.utils.unregister_class(BAKER_OT_toggle_select_all_texture_sets)
    bpy.utils.unregister_class(BAKER_OT_delete_selected_texture_sets)
    bpy.utils.unregister_class(BAKER_OT_create_atlas_flora)
    bpy.utils.unregister_class(BAKER_PT_atlas_panel_flora)
    bpy.utils.unregister_class(BAKER_OT_ScaleAndBake)
    bpy.utils.unregister_class(BAKER_OT_LoadTextures)
    bpy.utils.unregister_class(BAKER_OT_ResizeAndScale)
    bpy.utils.unregister_class(BAKER_OT_ResizeScaleAndBake)
    bpy.utils.unregister_class(BAKER_OT_ReplaceWithLight)
    bpy.utils.unregister_class(BAKER_OT_quick_mode)

    
    bpy.utils.unregister_class(BAKER_TextureSet)
    bpy.utils.unregister_class(BAKER_AtlasData)
    bpy.utils.unregister_class(BAKER_UdimMaterial)
    bpy.utils.unregister_class(BAKER_ObjectMaterialIndices)
    bpy.utils.unregister_class(TEXTURE_UL_list)
    bpy.utils.unregister_class(TextureItem)

if __name__ == "__main__":
    register() 
