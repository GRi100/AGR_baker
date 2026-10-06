"""
AGR Rename operators for AGR Tools
Based on rename_project.py with proper popup dialogs
"""

import bpy
import os
import json
import re
import random
from bpy.types import Operator
from bpy.props import StringProperty, IntProperty, EnumProperty

from .log import agr_report, unregister_classes
from . import rename_shared
from .rename_shared import (  # noqa: F401 — re-exported for ui.py and the twin
    SM_NAME_RE,
    RENAME_ALLOWED_TYPES,
    parse_sm_name,
    get_texture_type_from_filename,
    get_color_space_for_texture_type,
    find_lowpoly_collections,
    udim_aware_material_name,
)


# ============= Helper Functions =============
#
# SM_NAME_RE / RENAME_ALLOWED_TYPES / parse_sm_name / find_lowpoly_collections
# now live in rename_shared.py (one copy for both rename operators) and are
# re-exported above — ui.py keeps importing them from here.


def _get_new_address(scene):
    """Get address from scene properties"""
    address = getattr(scene, "agr_rename_address", "")
    if address:
        return address.strip()
    return ""


def _is_lowpoly_collection(coll):
    """Check if collection is lowpoly (starts with 4 digits)"""
    try:
        return bool(re.match(r'^\d{4}', coll.name))
    except Exception:
        return False


def _obj_in_lowpoly_collection(obj):
    """Check if object is in lowpoly collection"""
    try:
        return any(_is_lowpoly_collection(coll) for coll in obj.users_collection)
    except Exception:
        return False


# ============= Rename Main Object =============

class AGR_OT_rename_main_object(Operator):
    """Переименование основного объекта - вызывает диалог выбора типа"""
    bl_idname = "agr.rename_main_object"
    bl_label = "Переименовать основной объект"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'
    
    def execute(self, context):
        address = _get_new_address(context.scene)
        if not address:
            self.report({'ERROR'}, "Введите Address в панели AGR_rename")
            return {'CANCELLED'}
        
        # Call dialog operator
        bpy.ops.agr.rename_main_object_dialog('INVOKE_DEFAULT')
        return {'FINISHED'}


class AGR_OT_rename_main_object_dialog(Operator):
    """Диалог выбора типа объекта"""
    bl_idname = "agr.rename_main_object_dialog"
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
        layout.prop(self, "object_type", text="Тип")
        if self.object_type in ['Main', 'MainGlass']:
            layout.prop(self, "object_number", text="Номер")
            layout.label(text="0 = без номера")
    
    def execute(self, context):
        address = _get_new_address(context.scene)
        obj = context.active_object
        
        # Format name
        if self.object_type in ['Main', 'MainGlass'] and self.object_number > 0:
            obj.name = f"SM_{address}_{self.object_number:03d}_{self.object_type}"
        else:
            obj.name = f"SM_{address}_{self.object_type}"
        
        self.report({'INFO'}, f"Объект переименован в {obj.name}")
        return {'FINISHED'}


# ============= Rename Materials =============

class AGR_OT_rename_materials(Operator):
    """Переименование материалов объекта на основе его имени"""
    bl_idname = "agr.rename_materials"
    bl_label = "Переименовать материалы объекта"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            return False
        
        obj_name = obj.name
        obj_name_clean = re.sub(r'\.\d{3}$', '', obj_name)
        
        patterns = [
            r'^SM_.+?_\d{3}_(Main|MainGlass)$',
            r'^SM_.+?_(Main|MainGlass|Ground|GroundGlass|GroundEl|GroundElGlass|Flora)$'
        ]
        
        for pattern in patterns:
            if re.match(pattern, obj_name_clean):
                return True
        return False
    
    def execute(self, context):
        obj = context.active_object
        obj_name = obj.name
        obj_name_clean = re.sub(r'\.\d{3}$', '', obj_name)
        
        parsed = self.parse_object_name(obj_name_clean)
        if not parsed:
            self.report({'ERROR'}, "Не удалось распознать формат имени объекта")
            return {'CANCELLED'}
        
        address, number, obj_type = parsed
        
        renamed_count = 0
        if obj.data.materials:
            for idx, mat_slot in enumerate(obj.data.materials, 1):
                # udim_aware_material_name keeps the shared Ground material of
                # GroundEl/Flora siblings on its documented Ground name and
                # skips glass materials.
                mat_name = udim_aware_material_name(mat_slot, address, number,
                                                    obj_type, idx)
                if not mat_name:
                    continue
                if mat_slot.name != mat_name:
                    mat_slot.name = mat_name
                renamed_count += 1
        
        self.report({'INFO'}, f"Переименовано материалов: {renamed_count}")
        return {'FINISHED'}
    
    def parse_object_name(self, obj_name):
        match = re.match(r'^SM_(.+?)_(\d{3})_(Main|MainGlass)$', obj_name)
        if match:
            return match.group(1), match.group(2), match.group(3)
        
        match = re.match(r'^SM_(.+?)_(Main|MainGlass|Ground|GroundGlass|GroundEl|GroundElGlass|Flora)$', obj_name)
        if match:
            return match.group(1), None, match.group(2)
        
        return None


# ============= Rename Glass Materials =============

class AGR_OT_rename_glass_materials(Operator):
    """Переименование материалов стекла - вызывает диалог выбора качества"""
    bl_idname = "agr.rename_glass_materials"
    bl_label = "Переименовать материалы стекла"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            return False
        
        obj_name = obj.name
        obj_name_clean = re.sub(r'\.\d{3}$', '', obj_name)
        
        # Check if object is Glass type
        patterns = [
            r'^SM_.+?_\d{3}_MainGlass$',
            r'^SM_.+?_MainGlass$',
            r'^SM_.+?_GroundGlass$',
            r'^SM_.+?_GroundElGlass$',
        ]
        
        for pattern in patterns:
            if re.match(pattern, obj_name_clean):
                return True
        return False
    
    def execute(self, context):
        address = _get_new_address(context.scene)
        if not address:
            self.report({'ERROR'}, "Введите Address в панели AGR_rename")
            return {'CANCELLED'}
        
        # Parse object name to get info
        obj = context.active_object
        parsed = self.parse_object_name(obj.name)
        if not parsed:
            self.report({'ERROR'}, "Не удалось распознать формат имени объекта")
            return {'CANCELLED'}
        
        address_from_obj, number, obj_type = parsed
        
        # Store info in scene for dialog operators
        context.scene.agr_glass_address = address
        context.scene.agr_glass_number = int(number) if number else 0
        context.scene.agr_glass_obj_type = obj_type
        
        # Call quality selection dialog
        bpy.ops.agr.rename_glass_quality_dialog('INVOKE_DEFAULT')
        return {'FINISHED'}
    
    def parse_object_name(self, obj_name):
        obj_name_clean = re.sub(r'\.\d{3}$', '', obj_name)
        
        match = re.match(r'^SM_(.+?)_(\d{3})_MainGlass$', obj_name_clean)
        if match:
            return match.group(1), match.group(2), 'MainGlass'
        
        match = re.match(r'^SM_(.+?)_MainGlass$', obj_name_clean)
        if match:
            return match.group(1), None, 'MainGlass'
        
        match = re.match(r'^SM_(.+?)_GroundGlass$', obj_name_clean)
        if match:
            return match.group(1), None, 'GroundGlass'
        
        match = re.match(r'^SM_(.+?)_GroundElGlass$', obj_name_clean)
        if match:
            return match.group(1), None, 'GroundElGlass'
        
        return None


class AGR_OT_rename_glass_quality_dialog(Operator):
    """Диалог выбора качества стекла HIGH или LOW"""
    bl_idname = "agr.rename_glass_quality_dialog"
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
            # Rename as regular materials with full naming
            active_obj = context.active_object
            address = context.scene.agr_glass_address
            number = context.scene.agr_glass_number
            obj_type = context.scene.agr_glass_obj_type
            
            self.rename_materials_high(context, active_obj, address, number, obj_type)
        else:
            # LOW - ask for glass number
            bpy.ops.agr.rename_glass_number_dialog('INVOKE_DEFAULT')
        
        return {'FINISHED'}
    
    def rename_materials_high(self, context, active_obj, address, number, obj_type):
        """Rename materials in HIGH quality"""
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


class AGR_OT_rename_glass_number_dialog(Operator):
    """Диалог ввода номера стекла LOW"""
    bl_idname = "agr.rename_glass_number_dialog"
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
        
        # Rename to M_Glass_##
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


# ============= Rename UCX =============

class AGR_OT_rename_ucx(Operator):
    """Переименование выбранных объектов в UCX коллизии - вызывает диалог"""
    bl_idname = "agr.rename_ucx"
    bl_label = "Переименовать в UCX"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return len([obj for obj in context.selected_objects if obj.type == 'MESH']) > 0
    
    def execute(self, context):
        address = _get_new_address(context.scene)
        if not address:
            self.report({'ERROR'}, "Введите Address в панели AGR_rename")
            return {'CANCELLED'}
        
        bpy.ops.agr.rename_ucx_dialog('INVOKE_DEFAULT')
        return {'FINISHED'}


class AGR_OT_rename_ucx_dialog(Operator):
    """Диалог выбора типа для UCX коллизий"""
    bl_idname = "agr.rename_ucx_dialog"
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
        layout.prop(self, "object_type", text="Тип")
        if self.object_type == 'Main':
            layout.prop(self, "object_number", text="Номер")
            layout.label(text="0 = без номера")
    
    def execute(self, context):
        address = _get_new_address(context.scene)
        selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        if self.object_type == 'Main' and self.object_number > 0:
            base_name = f"UCX_SM_{address}_{self.object_number:03d}_{self.object_type}"
        else:
            base_name = f"UCX_SM_{address}_{self.object_type}"
        
        renamed_count = self.rename_ucx_objects(context, selected_objects, base_name)
        
        self.report({'INFO'}, f"Переименовано UCX объектов: {renamed_count}")
        return {'FINISHED'}
    
    def rename_ucx_objects(self, context, selected_objects, base_name):
        renamed_count = 0
        potential_names = [f"{base_name}_{idx:03d}" for idx in range(1, len(selected_objects) + 1)]
        
        objects_to_change = []
        for obj in context.scene.objects:
            if obj.type == 'MESH' and obj not in selected_objects:
                if obj.name in potential_names:
                    objects_to_change.append(obj)
        
        used_ids = set()
        for obj in objects_to_change:
            original_idx = potential_names.index(obj.name) + 1
            while True:
                unique_id = random.randint(10000000, 99999999)
                if unique_id not in used_ids:
                    used_ids.add(unique_id)
                    break
            obj.name = f"{base_name}_{original_idx:03d}_CHANGED_{unique_id}"
        
        selected_suffixes = set()
        for obj in selected_objects:
            while True:
                suffix = random.randint(10000000, 99999999)
                if suffix not in selected_suffixes:
                    selected_suffixes.add(suffix)
                    break
            obj.name = f"{obj.name}_{suffix}"
        
        for idx, obj in enumerate(selected_objects, 1):
            obj.name = f"{base_name}_{idx:03d}"
            renamed_count += 1
        
        return renamed_count


# ============= Rename Textures =============

class AGR_OT_rename_textures(Operator):
    """Переименование текстур объекта"""
    bl_idname = "agr.rename_textures"
    bl_label = "Переименовать текстуры"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            return False
        if not obj.data.materials:
            return False
        
        obj_name = re.sub(r'\.\d{3}$', '', obj.name)
        return bool(re.match(r'^SM_.+?(_\d{3})?_(Main|Ground|GroundEl|GroundElGlass|Flora)', obj_name))
    
    def execute(self, context):
        obj = context.active_object
        address = _get_new_address(context.scene)
        if not address:
            self.report({'ERROR'}, "Введите Address в панели AGR_rename")
            return {'CANCELLED'}
        
        parsed = self.parse_object_name(obj.name)
        if not parsed:
            self.report({'ERROR'}, "Не удалось распознать формат имени объекта")
            return {'CANCELLED'}
        
        current_address, number, obj_type = parsed
        
        texture_type = self.detect_texture_type(obj)
        if not texture_type:
            self.report({'ERROR'}, "Не найдены текстуры в материалах")
            return {'CANCELLED'}
        
        if texture_type == 'UDIM':
            return self.process_udim_textures(obj, address, number, obj_type)
        
        if self._all_textures_packed(obj):
            return self._rename_packed_textures_in_place(obj, address, number, obj_type)
        
        return self.process_regular_textures(obj, address, number, obj_type)
    
    def parse_object_name(self, obj_name):
        obj_name_clean = re.sub(r'\.\d{3}$', '', obj_name)
        
        match = re.match(r'^SM_(.+?)_(\d{3})_(Main|MainGlass)$', obj_name_clean)
        if match:
            return match.group(1), match.group(2), 'Main'

        match = re.match(r'^SM_(.+?)_(Main|MainGlass)$', obj_name_clean)
        if match:
            return match.group(1), None, 'Main'

        match = re.match(r'^SM_(.+?)_(Ground|GroundGlass)$', obj_name_clean)
        if match:
            return match.group(1), None, 'Ground'
        
        match = re.match(r'^SM_(.+?)_(GroundEl|GroundElGlass)$', obj_name_clean)
        if match:
            return match.group(1), None, match.group(2)
        
        match = re.match(r'^SM_(.+?)_(Flora)$', obj_name_clean)
        if match:
            return match.group(1), None, 'Flora'
        
        return None
    
    def detect_texture_type(self, obj):
        has_udim = False
        has_regular = False
        
        for mat_slot in obj.data.materials:
            if mat_slot and mat_slot.use_nodes:
                for node in mat_slot.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image:
                        if node.image.source == 'TILED':
                            has_udim = True
                        elif node.image.filepath or node.image.packed_file:
                            has_regular = True
        
        if has_udim:
            return 'UDIM'
        if has_regular:
            return 'REGULAR'
        return None
    
    def _all_textures_packed(self, obj):
        for mat_slot in obj.data.materials:
            if not mat_slot or not mat_slot.use_nodes:
                continue
            for node in mat_slot.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    if node.image.source == 'TILED':
                        continue
                    if node.image.filepath and not node.image.packed_file:
                        return False
        return True
    
    def _rename_packed_textures_in_place(self, obj, address, number, obj_type):
        textures = self.get_regular_textures(obj)
        if not textures:
            return {'CANCELLED'}
        
        tex_type_counters = {}
        renamed_count = 0
        
        for img in textures:
            if not img.packed_file:
                continue
            
            filename = img.name if not img.filepath else os.path.basename(img.filepath)
            tex_type = self.get_texture_type_from_filename(filename)
            if not tex_type:
                continue
            
            tex_type_counters[tex_type] = tex_type_counters.get(tex_type, 0) + 1
            idx = tex_type_counters[tex_type]
            
            if obj_type == 'Main' and number:
                new_filename = f"T_{address}_{number}_{obj_type}_{tex_type}_{idx}.png"
            elif obj_type == 'Main' and not number:
                new_filename = f"T_{address}_{obj_type}_{tex_type}_{idx}.png"
            elif obj_type == 'Ground':
                new_filename = f"T_{address}_Ground_{tex_type}_{idx}.png"
            elif obj_type.startswith('GroundE') and obj_type != 'Ground':
                new_filename = f"T_{address}_{obj_type}_{tex_type}_{idx}.png"
            elif obj_type == 'Flora':
                new_filename = f"T_{address}_Flora_{tex_type}_{idx}.png"
            else:
                continue
            
            new_name = new_filename.replace('.png', '')
            old_name = img.name
            
            if old_name == new_name and (not img.filepath or os.path.basename(img.filepath) == new_filename):
                continue
            
            try:
                img.name = new_name
                img.filepath = "//" + new_filename
                renamed_count += 1
            except Exception:
                pass
        
        if renamed_count > 0:
            self.report({'INFO'}, f"Переименовано запакованных текстур: {renamed_count}")
            return {'FINISHED'}
        return {'CANCELLED'}
    
    def process_udim_textures(self, obj, address, number, obj_type):
        texture_folder = self.get_udim_texture_folder(obj)
        if not texture_folder or not os.path.exists(texture_folder):
            agr_report(self, 'ERROR', "Не найдена папка с UDIM текстурами")
            return {'CANCELLED'}

        renamed_count, _folder_renamed = rename_shared.process_udim_textures(
            self, obj, texture_folder, address, number, obj_type)
        if renamed_count == 0:
            agr_report(self, 'WARNING', "Не найдены текстуры для переименования")
            return {'CANCELLED'}

        agr_report(self, 'INFO', f"Переименовано UDIM текстур: {renamed_count}")
        return {'FINISHED'}

    def process_regular_textures(self, obj, address, number, obj_type):
        blend_filepath = bpy.data.filepath
        if not blend_filepath:
            agr_report(self, 'ERROR', "Сохраните файл перед переименованием текстур")
            return {'CANCELLED'}

        target_root = os.path.dirname(blend_filepath)
        low_texture_folder = os.path.join(target_root, "low_texture")
        if not os.path.exists(low_texture_folder):
            os.makedirs(low_texture_folder)

        renamed_count, _warnings = rename_shared.process_object_textures(
            self, obj, low_texture_folder, address, number, obj_type)

        if renamed_count > 0:
            agr_report(self, 'INFO', f"Обработано текстур: {renamed_count}")
            return {'FINISHED'}

        agr_report(self, 'WARNING', "Не удалось обработать текстуры")
        return {'CANCELLED'}

    def get_udim_texture_folder(self, obj):
        for mat_slot in obj.data.materials:
            if mat_slot and mat_slot.use_nodes:
                for node in mat_slot.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image:
                        if node.image.source == 'TILED' and node.image.filepath:
                            abs_path = bpy.path.abspath(node.image.filepath)
                            return os.path.dirname(abs_path)
        return None

    def get_regular_textures(self, obj):
        textures = []
        processed_images = set()

        for mat_slot in obj.data.materials:
            if mat_slot and mat_slot.use_nodes:
                for node in mat_slot.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image:
                        if node.image.source != 'TILED' and node.image.name not in processed_images:
                            textures.append(node.image)
                            processed_images.add(node.image.name)

        return textures if textures else None

    def get_texture_type_from_filename(self, filename):
        return get_texture_type_from_filename(filename)



# ============= Rename GEOJSON =============

class AGR_OT_rename_geojson(Operator):
    """Переименование GEOJSON файла и адресов материалов внутри"""
    bl_idname = "agr.rename_geojson"
    bl_label = "Переименовать GEOJSON"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            return False
        # Same parser and same type set the panel uses — the old unanchored
        # regex let SM_X_GroundEl through as "Ground" and the button then
        # errored out.
        parsed = parse_sm_name(obj.name)
        if not parsed or parsed[2] not in RENAME_ALLOWED_TYPES['geojson']:
            cls.poll_message_set("Активный объект должен быть SM_*_Main или SM_*_Ground")
            return False
        return True

    def execute(self, context):
        obj = context.active_object
        new_address = _get_new_address(context.scene)
        if not new_address:
            self.report({'ERROR'}, "Введите Address в панели AGR_rename")
            return {'CANCELLED'}
        
        parsed = self.parse_object_name(obj.name)
        if not parsed:
            self.report({'ERROR'}, "Объект должен быть типа Main или Ground")
            return {'CANCELLED'}
        
        current_address, number, obj_type = parsed
        if obj_type not in ['Main', 'Ground']:
            self.report({'ERROR'}, "Переименование GEOJSON только для Main и Ground")
            return {'CANCELLED'}
        
        texture_folder = self.get_texture_folder_from_material(obj)
        if not texture_folder or not os.path.exists(texture_folder):
            self.report({'ERROR'}, "Не удалось найти папку с текстурами")
            return {'CANCELLED'}
        
        geojson_file, old_address_in_file = self.find_geojson_file(texture_folder, obj_type, number)
        if not geojson_file:
            self.report({'ERROR'}, f"GEOJSON файл не найден в папке {texture_folder}")
            return {'CANCELLED'}
        
        old_geojson_path = os.path.join(texture_folder, geojson_file)
        
        try:
            with open(old_geojson_path, 'r', encoding='utf-8') as f:
                geojson_data = json.load(f)
            
            updated_count = self.update_glass_materials_in_geojson(geojson_data, old_address_in_file, new_address)
            
            if obj_type == 'Main' and number:
                new_geojson_name = f"SM_{new_address}_{number}.geojson"
            else:
                new_geojson_name = f"SM_{new_address}_{obj_type}.geojson"
            
            new_geojson_path = os.path.join(texture_folder, new_geojson_name)
            
            with open(new_geojson_path, 'w', encoding='utf-8') as f:
                json.dump(geojson_data, f, ensure_ascii=False, indent=2)
            
            if old_geojson_path != new_geojson_path and os.path.exists(old_geojson_path):
                os.remove(old_geojson_path)
            
            fbx_renamed_count = self.rename_fbx_files(texture_folder, new_address, number, obj_type)
            
            self.report({'INFO'}, f"GEOJSON переименован, материалов стекла: {updated_count}, FBX: {fbx_renamed_count}")
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка обработки GEOJSON: {str(e)}")
            return {'CANCELLED'}
        
        return {'FINISHED'}
    
    def parse_object_name(self, obj_name):
        obj_name_clean = re.sub(r'\.\d{3}$', '', obj_name)
        
        match = re.match(r'^SM_(.+?)_(\d{3})_(Main|MainGlass)$', obj_name_clean)
        if match:
            return match.group(1), match.group(2), 'Main'

        match = re.match(r'^SM_(.+?)_(Main|MainGlass)$', obj_name_clean)
        if match:
            return match.group(1), None, 'Main'

        match = re.match(r'^SM_(.+?)_(Ground|GroundGlass|GroundEl|GroundElGlass)$', obj_name_clean)
        if match:
            return match.group(1), None, 'Ground'

        return None

    def get_texture_folder_from_material(self, obj):
        for mat_slot in obj.data.materials:
            if mat_slot and mat_slot.use_nodes:
                for node in mat_slot.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image:
                        if node.image.source == 'TILED' and node.image.filepath:
                            abs_path = bpy.path.abspath(node.image.filepath)
                            return os.path.dirname(abs_path)
        # A UDIM node is not the only place a delivery geojson can live: for a
        # plain (packed) lowpoly object it sits in the object's own SM_* folder
        # next to the .blend, or in the .blend folder itself. The project twin
        # already fell back this way — the per-object button used to hard-fail.
        blend_path = bpy.data.filepath
        if not blend_path:
            return None
        root = os.path.dirname(blend_path)
        parsed = parse_sm_name(obj.name)
        if parsed:
            address, number, obj_type = parsed
            for candidate in rename_shared.geojson_search_dirs(root, [address], number, obj_type):
                try:
                    if any(f.endswith('.geojson') for f in os.listdir(candidate)):
                        return candidate
                except OSError:
                    continue
        if os.path.isdir(root):
            return root
        return None


    def find_geojson_file(self, folder, obj_type, number):
        geojson_file = None
        old_address = None
        
        if obj_type == 'Main' and number:
            pattern_with_num = r'^SM_(.+?)_' + re.escape(number) + r'\.geojson$'
            pattern_without_num = r'^SM_(.+?)\.geojson$'
            
            for filename in os.listdir(folder):
                match = re.match(pattern_with_num, filename)
                if match:
                    geojson_file = filename
                    old_address = match.group(1)
                    break
                match = re.match(pattern_without_num, filename)
                if match:
                    geojson_file = filename
                    old_address = match.group(1)
        elif obj_type == 'Ground':
            pattern = r'^SM_(.+?)_Ground\.geojson$'
            for filename in os.listdir(folder):
                match = re.match(pattern, filename)
                if match:
                    geojson_file = filename
                    old_address = match.group(1)
                    break
        
        return geojson_file, old_address
    
    def update_glass_materials_in_geojson(self, geojson_data, old_address, new_address):
        updated_count = 0
        
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
                                    if old_mat_name != new_mat_name:
                                        updated_count += 1
                                glass_list.clear()
                                glass_list.update(new_glass_dict)
        except Exception as e:
            print(f"Error updating materials in GEOJSON: {e}")
        
        return updated_count
    
    def rename_fbx_files(self, folder_path, new_address, number, obj_type):
        renamed_count = 0
        
        try:
            files_in_folder = os.listdir(folder_path)
            
            if obj_type == 'Main':
                if number:
                    pattern_main = r'^SM_(.+?)_' + re.escape(number) + r'\.fbx$'
                    pattern_light = r'^SM_(.+?)_' + re.escape(number) + r'_Light\.fbx$'
                    new_main_name = f"SM_{new_address}_{number}.fbx"
                    new_light_name = f"SM_{new_address}_{number}_Light.fbx"
                else:
                    new_main_name = f"SM_{new_address}.fbx"
                    new_light_name = f"SM_{new_address}_Light.fbx"
                
                for filename in files_in_folder:
                    if number:
                        match_main = re.match(pattern_main, filename)
                        match_light = re.match(pattern_light, filename)
                        
                        if match_main:
                            old_path = os.path.join(folder_path, filename)
                            new_path = os.path.join(folder_path, new_main_name)
                            if old_path != new_path:
                                os.rename(old_path, new_path)
                                renamed_count += 1
                        
                        if match_light:
                            old_path = os.path.join(folder_path, filename)
                            new_path = os.path.join(folder_path, new_light_name)
                            if old_path != new_path:
                                os.rename(old_path, new_path)
                                renamed_count += 1
            
            elif obj_type == 'Ground':
                pattern_main = r'^SM_(.+?)_Ground\.fbx$'
                pattern_light = r'^SM_(.+?)_Ground_Light\.fbx$'
                new_main_name = f"SM_{new_address}_Ground.fbx"
                new_light_name = f"SM_{new_address}_Ground_Light.fbx"
                
                for filename in files_in_folder:
                    match = re.match(pattern_main, filename)
                    if match:
                        old_path = os.path.join(folder_path, filename)
                        new_path = os.path.join(folder_path, new_main_name)
                        if old_path != new_path:
                            os.rename(old_path, new_path)
                            renamed_count += 1
                    
                    match = re.match(pattern_light, filename)
                    if match:
                        old_path = os.path.join(folder_path, filename)
                        new_path = os.path.join(folder_path, new_light_name)
                        if old_path != new_path:
                            os.rename(old_path, new_path)
                            renamed_count += 1
        
        except Exception as e:
            print(f"Error renaming FBX: {e}")
        
        return renamed_count


# ============= Rename Lights Root =============

def _rename_child_lights_root(root_obj, address, number, obj_type, operator=None):
    """Rename LIGHT children of a Root EMPTY (one implementation, shared with
    the project twin — collision handling used to exist only for UCX)."""
    renamed, _conflicts = rename_shared.rename_child_lights(
        operator, root_obj, address, number, obj_type)
    return renamed


class AGR_OT_rename_lights_root(Operator):
    """Переименование Root Empty и дочерних источников света по проектному соглашению"""
    bl_idname = "agr.rename_lights_root"
    bl_label = "Переименовать Root свет"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj or obj.type != 'EMPTY':
            return False
        # Name is not checked: the operator renames the empty INTO *_Root
        return any(child.type == 'LIGHT' for child in obj.children)

    def execute(self, context):
        new_address = _get_new_address(context.scene)
        if not new_address:
            self.report({'ERROR'}, "Введите Address в панели AGR_rename")
            return {'CANCELLED'}
        bpy.ops.agr.rename_lights_root_dialog('INVOKE_DEFAULT')
        return {'FINISHED'}


class AGR_OT_rename_lights_root_dialog(Operator):
    """Диалог выбора типа для Root-света"""
    bl_idname = "agr.rename_lights_root_dialog"
    bl_label = "Тип Root объекта"
    bl_options = {'REGISTER', 'UNDO'}

    light_type: EnumProperty(
        name="Тип объекта",
        items=[
            ('Main', "Main", "Основной объект (с номером или без)"),
            ('Ground', "Ground", "Земля без номера"),
        ],
        default='Main'
    )

    light_number: IntProperty(
        name="Номер объекта",
        description="Номер 001-999 (0 = без номера)",
        default=1,
        min=0,
        max=999
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "light_type", text="Тип")
        if self.light_type == 'Main':
            layout.prop(self, "light_number", text="Номер")
            layout.label(text="0 = без номера")

    def execute(self, context):
        obj = context.active_object
        new_address = _get_new_address(context.scene)

        if self.light_type == 'Ground':
            obj.name = f"{new_address}_Ground_Root"
            _rename_child_lights_root(obj, new_address, None, 'Ground')
        elif self.light_type == 'Main' and self.light_number > 0:
            number_str = f"{self.light_number:03d}"
            obj.name = f"{new_address}_{number_str}_Root"
            _rename_child_lights_root(obj, new_address, number_str, 'Main')
        else:
            obj.name = f"{new_address}_Root"
            _rename_child_lights_root(obj, new_address, None, 'Main')

        light_count = sum(1 for c in obj.children if c.type == 'LIGHT')
        self.report({'INFO'}, f"Root переименован, источников света: {light_count}")
        return {'FINISHED'}


# ============= Autofill address from the scene =============

# Collection scanning lives in rename_shared.find_lowpoly_collections
# (re-exported at the top of this module).


class AGR_OT_rename_autofill_address(Operator):
    """Найти lowpoly-коллекцию вида 0109_Адрес_Ground(.fbx) и подставить
номер lowpoly и адрес в поля панели"""
    bl_idname = "agr.rename_autofill_address"
    bl_label = "Заполнить из сцены"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        matches = find_lowpoly_collections(scene)
        if not matches:
            self.report({'ERROR'},
                        "Не найдена lowpoly-коллекция вида NNNN_Адрес_Ground "
                        "(например 0109_BolshaiaPirogovskaia_ZU_51_1_Ground.fbx)")
            return {'CANCELLED'}
        number, address, coll_name = matches[0]
        scene.agr_rp_project_lowpoly_number = number
        scene.agr_rename_address = address
        if len(matches) > 1:
            others = ", ".join(m[2] for m in matches[1:])
            self.report({'WARNING'},
                        f"Взята первая коллекция: {coll_name}; ещё найдены: {others}")
        else:
            self.report({'INFO'}, f"Адрес заполнен из коллекции: {coll_name}")
        print(f"📍 AGR Rename: адрес из коллекции — {number}_{address}")
        return {'FINISHED'}


@bpy.app.handlers.persistent
def _autofill_address_on_load(_dummy):
    """Fill BOTH address props from the lowpoly collection name on file
    load — ONLY when both are empty (a partially filled state is the
    user's own choice).  Exceptions are swallowed: a handler error would
    spam EVERY file open."""
    try:
        scene = bpy.context.scene
        if scene is None:
            return
        if getattr(scene, 'agr_rename_address', '') or \
                getattr(scene, 'agr_rp_project_lowpoly_number', ''):
            return
        matches = find_lowpoly_collections(scene)
        if not matches:
            return
        number, address, coll_name = matches[0]
        scene.agr_rp_project_lowpoly_number = number
        scene.agr_rename_address = address
        print(f"📍 AGR Rename: адрес заполнен из коллекции {coll_name} — {number}_{address}")
    except Exception as exc:
        print(f"⚠️ AGR Rename autofill on load: {exc}")


def _drop_stale_load_handlers():
    """Dev reload (reloadOnSave) builds a NEW function object every time, so an
    identity check never spots the previous copy and the handler stacks up —
    dedupe by __name__ instead (same fix as operators_link's save_pre)."""
    for handler in list(bpy.app.handlers.load_post):
        if getattr(handler, "__name__", "") == _autofill_address_on_load.__name__:
            bpy.app.handlers.load_post.remove(handler)


# ============= Register =============











# ============= Register =============

classes = (
    AGR_OT_rename_main_object,
    AGR_OT_rename_main_object_dialog,
    AGR_OT_rename_materials,
    AGR_OT_rename_glass_materials,
    AGR_OT_rename_glass_quality_dialog,
    AGR_OT_rename_glass_number_dialog,
    AGR_OT_rename_ucx,
    AGR_OT_rename_ucx_dialog,
    AGR_OT_rename_textures,
    AGR_OT_rename_geojson,
    AGR_OT_rename_lights_root,
    AGR_OT_rename_lights_root_dialog,
    AGR_OT_rename_autofill_address,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    
    bpy.types.Scene.agr_rename_address = StringProperty(
        name="Address",
        description="Адрес для переименования",
        default="",
    )
    
    # Glass material properties
    bpy.types.Scene.agr_glass_address = StringProperty(
        name="Glass Address",
        description="Адрес для стекла",
        default="",
    )
    
    bpy.types.Scene.agr_glass_number = IntProperty(
        name="Glass Number",
        description="Номер для стекла",
        default=0,
    )
    
    bpy.types.Scene.agr_glass_obj_type = StringProperty(
        name="Glass Object Type",
        description="Тип объекта стекла",
        default="",
    )

    _drop_stale_load_handlers()
    bpy.app.handlers.load_post.append(_autofill_address_on_load)

    print("✅ Rename operators registered")


def unregister():
    _drop_stale_load_handlers()

    if hasattr(bpy.types.Scene, "agr_rename_address"):
        del bpy.types.Scene.agr_rename_address
    if hasattr(bpy.types.Scene, "agr_glass_address"):
        del bpy.types.Scene.agr_glass_address
    if hasattr(bpy.types.Scene, "agr_glass_number"):
        del bpy.types.Scene.agr_glass_number
    if hasattr(bpy.types.Scene, "agr_glass_obj_type"):
        del bpy.types.Scene.agr_glass_obj_type
    
    unregister_classes(classes)  # idempotent: survives a half-registered module (R-glue-4)

