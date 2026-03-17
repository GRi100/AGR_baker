"""
AGR Rename operators for AGR Baker v2
Based on rename_project.py with proper popup dialogs
"""

import bpy
import os
import json
import re
import random
from bpy.types import Operator
from bpy.props import StringProperty, IntProperty, EnumProperty


# ============= Helper Functions =============

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
                if mat_slot:
                    if re.match(r'^M_Glass_\d{2}$', mat_slot.name):
                        continue
                    
                    if number:
                        mat_slot.name = f"M_{address}_{number}_{obj_type}_{idx}"
                    else:
                        mat_slot.name = f"M_{address}_{obj_type}_{idx}"
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
            self.report({'ERROR'}, "Не найдена папка с UDIM текстурами")
            return {'CANCELLED'}
        
        renamed_count = self.rename_udim_textures(texture_folder, address, number, obj_type)
        if renamed_count == 0:
            self.report({'WARNING'}, "Не найдены текстуры для переименования")
            return {'CANCELLED'}
        
        new_folder_name = self.get_new_folder_name(address, number, obj_type)
        if new_folder_name:
            parent_folder = os.path.dirname(texture_folder)
            new_folder_path = os.path.join(parent_folder, new_folder_name)
            try:
                if texture_folder != new_folder_path and os.path.exists(texture_folder):
                    os.rename(texture_folder, new_folder_path)
                    self.update_material_paths(obj, texture_folder, new_folder_path)
            except Exception as e:
                self.report({'WARNING'}, f"Текстуры переименованы, но папка не переименована: {e}")
        
        self.report({'INFO'}, f"Переименовано UDIM текстур: {renamed_count}")
        return {'FINISHED'}
    
    def process_regular_textures(self, obj, address, number, obj_type):
        textures = self.get_regular_textures(obj)
        if not textures:
            self.report({'ERROR'}, "Не найдены обычные текстуры")
            return {'CANCELLED'}
        
        blend_filepath = bpy.data.filepath
        if not blend_filepath:
            self.report({'ERROR'}, "Сохраните файл перед переименованием текстур")
            return {'CANCELLED'}
        
        target_root = os.path.dirname(blend_filepath)
        low_texture_folder = os.path.join(target_root, "low_texture")
        
        if not os.path.exists(low_texture_folder):
            os.makedirs(low_texture_folder)
        
        renamed_count, new_texture_paths, loaded_images = self.process_textures_batch(
            obj, textures, low_texture_folder, address, number, obj_type
        )
        
        if renamed_count > 0 and new_texture_paths:
            self._pack_textures_and_cleanup(low_texture_folder, loaded_images)
            self.report({'INFO'}, f"Обработано текстур: {renamed_count}")
            return {'FINISHED'}
        
        self.report({'WARNING'}, "Не удалось обработать текстуры")
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
        filename_lower = filename.lower()
        if 'diffuse' in filename_lower or '_d.' in filename_lower or '_d_' in filename_lower:
            return 'Diffuse'
        if 'normal' in filename_lower or '_n.' in filename_lower or '_n_' in filename_lower:
            return 'Normal'
        if 'erm' in filename_lower or 'orm' in filename_lower:
            return 'ERM'
        if 'roughness' in filename_lower or '_r.' in filename_lower or '_r_' in filename_lower:
            return 'Roughness'
        if 'metallic' in filename_lower or '_m.' in filename_lower or '_m_' in filename_lower:
            return 'Metallic'
        if 'opacity' in filename_lower or '_o.' in filename_lower or '_o_' in filename_lower:
            return 'Opacity'
        return None
    
    def rename_udim_textures(self, folder_path, new_address, number, obj_type):
        renamed_count = 0
        for filename in os.listdir(folder_path):
            if not filename.endswith('.png'):
                continue
            
            udim_match = re.search(r'\.(\d{4})\.png$', filename)
            if not udim_match:
                continue
            
            udim_number = udim_match.group(1)
            texture_type = self.get_texture_type(filename)
            if not texture_type:
                continue
            
            should_rename = False
            material_num = None
            
            if obj_type == 'Main' and number:
                pattern = r'^T_.+?_' + re.escape(number) + r'_' + re.escape(texture_type) + r'_(\d+)\.\d{4}\.png$'
                match = re.match(pattern, filename)
                if match:
                    should_rename = True
                    material_num = match.group(1)
            elif obj_type == 'Main' and not number:
                if not any(tag in filename for tag in ['_Ground_', '_GroundEl', '_Flora_']):
                    pattern = r'^T_.+?_' + re.escape(texture_type) + r'_(\d+)\.\d{4}\.png$'
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
            
            if obj_type == 'Main' and number:
                new_filename = f"T_{new_address}_{number}_{texture_type}_{material_num}.{udim_number}.png"
            elif obj_type == 'Main' and not number:
                new_filename = f"T_{new_address}_{texture_type}_{material_num}.{udim_number}.png"
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
                print(f"Error renaming {filename}: {e}")
        
        return renamed_count
    
    def get_texture_type(self, filename):
        if 'Diffuse' in filename:
            return 'Diffuse'
        if 'Normal' in filename:
            return 'Normal'
        if 'ERM' in filename or 'ORM' in filename:
            return 'ERM'
        return None
    
    def get_new_folder_name(self, address, number, obj_type):
        if obj_type == 'Main' and number:
            return f"SM_{address}_{number}"
        if obj_type == 'Main' and not number:
            return f"SM_{address}"
        if obj_type == 'Ground':
            return f"SM_{address}_Ground"
        return None
    
    def update_material_paths(self, obj, old_folder, new_folder):
        """Update material texture paths after folder rename"""
        new_textures = {}
        try:
            for filename in os.listdir(new_folder):
                if filename.endswith('.1001.png'):
                    if 'Diffuse' in filename:
                        new_textures['Diffuse'] = filename
                    elif 'Normal' in filename:
                        new_textures['Normal'] = filename
                    elif 'ERM' in filename or 'ORM' in filename:
                        new_textures['ERM'] = filename
        except OSError:
            return

        if not hasattr(obj.data, 'materials'):
            return

        for mat_slot in obj.data.materials:
            if not mat_slot or not mat_slot.use_nodes:
                continue
            tree = mat_slot.node_tree
            if not tree:
                continue
            nodes = tree.nodes

            bsdf = None
            for node in nodes:
                if node.type == 'BSDF_PRINCIPLED':
                    bsdf = node
                    break
            if not bsdf:
                continue

            # Update Diffuse texture
            if bsdf.inputs['Base Color'].is_linked and 'Diffuse' in new_textures:
                diffuse_link = bsdf.inputs['Base Color'].links[0]
                diffuse_node = diffuse_link.from_node
                if diffuse_node.type == 'TEX_IMAGE':
                    self.load_udim_texture(new_folder, new_textures['Diffuse'], diffuse_node, 'sRGB')

            # Update ERM texture
            for node in nodes:
                if node.type in ['SEPRGB', 'SEPARATE_COLOR']:
                    is_connected = any(link.to_node == bsdf for output in node.outputs for link in output.links)
                    if is_connected and node.inputs[0].is_linked and 'ERM' in new_textures:
                        erm_node = node.inputs[0].links[0].from_node
                        if erm_node.type == 'TEX_IMAGE':
                            self.load_udim_texture(new_folder, new_textures['ERM'], erm_node, 'Non-Color')
                        break

            # Update Normal texture
            for node in nodes:
                if node.type == 'NORMAL_MAP':
                    if node.outputs['Normal'].is_linked and node.inputs['Color'].is_linked and 'Normal' in new_textures:
                        if any(link.to_node == bsdf for link in node.outputs['Normal'].links):
                            normal_node = node.inputs['Color'].links[0].from_node
                            if normal_node.type == 'TEX_IMAGE':
                                self.load_udim_texture(new_folder, new_textures['Normal'], normal_node, 'Non-Color')
                            break

    def load_udim_texture(self, folder, filename, node, color_space='sRGB'):
        """Load new UDIM texture into node"""
        try:
            base_name = filename.replace('.1001.png', '')
            udim_path = os.path.join(folder, f"{base_name}.<UDIM>.png")
            abs_path = os.path.abspath(udim_path)
            new_image = bpy.data.images.load(abs_path, check_existing=True)
            new_image.source = 'TILED'
            new_image.colorspace_settings.name = color_space
            node.image = new_image
        except Exception as e:
            print(f"  ⚠️ Error loading UDIM texture {filename}: {e}")
    
    def process_textures_batch(self, obj, textures, target_folder, address, number, obj_type):
        renamed_count = 0
        old_images = []
        new_texture_paths = {}
        loaded_images = []
        
        for img in textures:
            is_packed = img.packed_file is not None
            filename = img.name if not img.filepath else os.path.basename(img.filepath)
            tex_type = self.get_texture_type_from_filename(filename)
            if not tex_type:
                continue
            
            if is_packed:
                temp_filename = f"temp_{img.name}"
                temp_path = os.path.join(target_folder, temp_filename)
                try:
                    img.filepath = temp_path
                    img.save()
                    old_abs_path = temp_path
                except Exception:
                    continue
            else:
                if not img.filepath:
                    continue
                old_abs_path = bpy.path.abspath(img.filepath)
                if not os.path.exists(old_abs_path):
                    continue
            
            if obj_type == 'Main' and number:
                new_filename = f"T_{address}_{number}_{obj_type}_{tex_type}_1.png"
            elif obj_type == 'Main' and not number:
                new_filename = f"T_{address}_{obj_type}_{tex_type}_1.png"
            elif obj_type == 'Ground':
                new_filename = f"T_{address}_Ground_{tex_type}_1.png"
            elif obj_type.startswith('GroundE') and obj_type != 'Ground':
                new_filename = f"T_{address}_{obj_type}_{tex_type}_1.png"
            elif obj_type == 'Flora':
                new_filename = f"T_{address}_Flora_{tex_type}_1.png"
            else:
                continue
            
            new_filepath = os.path.join(target_folder, new_filename)
            
            try:
                if is_packed:
                    if os.path.exists(old_abs_path) and old_abs_path != new_filepath:
                        import shutil
                        shutil.move(old_abs_path, new_filepath)
                else:
                    import shutil
                    shutil.copy2(old_abs_path, new_filepath)
                
                new_texture_paths[tex_type] = new_filepath
                old_images.append(img)
                renamed_count += 1
            except Exception as e:
                print(f"Error processing texture: {e}")
        
        if new_texture_paths:
            loaded_images = self.reconnect_textures(obj, new_texture_paths)
            for old_img in old_images:
                try:
                    bpy.data.images.remove(old_img)
                except Exception:
                    pass
        
        return renamed_count, new_texture_paths, loaded_images
    
    def reconnect_textures(self, obj, new_texture_paths):
        loaded_images = []

        for mat_slot in obj.data.materials:
            if not mat_slot or not mat_slot.use_nodes:
                continue

            nodes = mat_slot.node_tree.nodes
            bsdf = None
            for node in nodes:
                if node.type == 'BSDF_PRINCIPLED':
                    bsdf = node
                    break

            if not bsdf:
                continue

            # Diffuse
            if 'd' in new_texture_paths and bsdf.inputs['Base Color'].is_linked:
                diffuse_link = bsdf.inputs['Base Color'].links[0]
                diffuse_node = diffuse_link.from_node
                if diffuse_node.type == 'TEX_IMAGE':
                    new_img = self.load_texture(new_texture_paths['d'], 'sRGB')
                    if new_img:
                        diffuse_node.image = new_img
                        loaded_images.append(new_img)

            # Normal
            if 'n' in new_texture_paths:
                for node in nodes:
                    if node.type == 'NORMAL_MAP':
                        if node.outputs['Normal'].is_linked and node.inputs['Color'].is_linked:
                            if any(link.to_node == bsdf for link in node.outputs['Normal'].links):
                                normal_node = node.inputs['Color'].links[0].from_node
                                if normal_node.type == 'TEX_IMAGE':
                                    new_img = self.load_texture(new_texture_paths['n'], 'Non-Color')
                                    if new_img:
                                        normal_node.image = new_img
                                        loaded_images.append(new_img)
                                break

            # ERM (through Separate Color / Separate RGB node)
            if 'erm' in new_texture_paths:
                for node in nodes:
                    if node.type in ['SEPRGB', 'SEPARATE_COLOR']:
                        is_connected = any(link.to_node == bsdf for output in node.outputs for link in output.links)
                        if is_connected and node.inputs[0].is_linked:
                            erm_node = node.inputs[0].links[0].from_node
                            if erm_node.type == 'TEX_IMAGE':
                                new_img = self.load_texture(new_texture_paths['erm'], 'Non-Color')
                                if new_img:
                                    erm_node.image = new_img
                                    loaded_images.append(new_img)
                            break

            # Roughness (direct connection to BSDF)
            if 'r' in new_texture_paths and bsdf.inputs['Roughness'].is_linked:
                roughness_link = bsdf.inputs['Roughness'].links[0]
                roughness_node = roughness_link.from_node
                if roughness_node.type == 'TEX_IMAGE':
                    new_img = self.load_texture(new_texture_paths['r'], 'Non-Color')
                    if new_img:
                        roughness_node.image = new_img
                        loaded_images.append(new_img)

            # Metallic (direct connection to BSDF)
            if 'm' in new_texture_paths and bsdf.inputs['Metallic'].is_linked:
                metallic_link = bsdf.inputs['Metallic'].links[0]
                metallic_node = metallic_link.from_node
                if metallic_node.type == 'TEX_IMAGE':
                    new_img = self.load_texture(new_texture_paths['m'], 'Non-Color')
                    if new_img:
                        metallic_node.image = new_img
                        loaded_images.append(new_img)

            # Emit
            if 'e' in new_texture_paths and bsdf.inputs['Emission Color'].is_linked:
                emit_link = bsdf.inputs['Emission Color'].links[0]
                emit_node = emit_link.from_node
                if emit_node.type == 'TEX_IMAGE':
                    new_img = self.load_texture(new_texture_paths['e'], 'sRGB')
                    if new_img:
                        emit_node.image = new_img
                        loaded_images.append(new_img)

            # Opacity (Alpha on BSDF)
            if 'o' in new_texture_paths and bsdf.inputs['Alpha'].is_linked:
                alpha_link = bsdf.inputs['Alpha'].links[0]
                alpha_node = alpha_link.from_node
                if alpha_node.type == 'TEX_IMAGE':
                    new_img = self.load_texture(new_texture_paths['o'], 'Non-Color')
                    if new_img:
                        alpha_node.image = new_img
                        loaded_images.append(new_img)

        return loaded_images
    
    def load_texture(self, filepath, color_space='sRGB'):
        try:
            abs_path = os.path.abspath(filepath)
            new_image = bpy.data.images.load(abs_path, check_existing=True)
            new_image.colorspace_settings.name = color_space
            return new_image
        except Exception as e:
            print(f"Error loading texture {filepath}: {e}")
            return None
    
    def _pack_textures_and_cleanup(self, folder_path, images):
        seen_paths = set()
        
        for img in (images or []):
            if not img:
                continue
            try:
                abs_path = bpy.path.abspath(img.filepath)
            except Exception:
                continue
            
            if not abs_path or abs_path in seen_paths:
                continue
            seen_paths.add(abs_path)
            
            if not os.path.exists(abs_path):
                continue
            
            try:
                if not img.packed_file:
                    img.pack()
            except Exception:
                continue
        
        try:
            for root, dirs, files in os.walk(folder_path, topdown=False):
                for filename in files:
                    try:
                        os.remove(os.path.join(root, filename))
                    except Exception:
                        pass
                for dirname in dirs:
                    try:
                        os.rmdir(os.path.join(root, dirname))
                    except Exception:
                        pass
            
            if os.path.exists(folder_path):
                try:
                    os.rmdir(folder_path)
                except Exception:
                    pass
        except Exception:
            pass


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
        obj_name = re.sub(r'\.\d{3}$', '', obj.name)
        return bool(re.match(r'^SM_.+?(_\d{3})?_(Main|Ground)', obj_name))
    
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


# ============= Rename Lights =============

class AGR_OT_rename_lights(Operator):
    """Переименование Empty объекта и привязанных источников света - вызывает диалог"""
    bl_idname = "agr.rename_lights"
    bl_label = "Переименовать свет"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj or obj.type != 'EMPTY':
            return False
        for child in context.scene.objects:
            if child.type == 'LIGHT' and child.parent == obj:
                return True
        return False
    
    def execute(self, context):
        new_address = _get_new_address(context.scene)
        if not new_address:
            self.report({'ERROR'}, "Введите Address в панели AGR_rename")
            return {'CANCELLED'}
        
        bpy.ops.agr.rename_lights_dialog('INVOKE_DEFAULT')
        return {'FINISHED'}


class AGR_OT_rename_lights_dialog(Operator):
    """Диалог выбора типа для света"""
    bl_idname = "agr.rename_lights_dialog"
    bl_label = "Выберите тип объекта"
    bl_options = {'REGISTER', 'UNDO'}
    
    light_type: EnumProperty(
        name="Тип объекта",
        items=[
            ('Main', "Main", "Основной объект с номером"),
            ('Ground', "Ground", "Земля без номера"),
        ],
        default='Main'
    )
    
    light_number: IntProperty(
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
        layout.prop(self, "light_type", text="Тип")
        if self.light_type == 'Main':
            layout.prop(self, "light_number", text="Номер")
            layout.label(text="0 = без номера")
    
    def execute(self, context):
        obj = context.active_object
        new_address = _get_new_address(context.scene)
        
        if self.light_type == 'Main' and self.light_number > 0:
            obj.name = f"SM_{new_address}_{self.light_number:03d}_Light"
        else:
            obj.name = f"SM_{new_address}_{self.light_type}_Light"
        
        renamed_lights = 0
        for child in context.scene.objects:
            if child.type == 'LIGHT' and child.parent == obj:
                if self.light_type == 'Main' and self.light_number > 0:
                    child.name = f"Light_{new_address}_{self.light_number:03d}"
                else:
                    child.name = f"Light_{new_address}_{self.light_type}"
                renamed_lights += 1
        
        self.report({'INFO'}, f"Empty переименован, источников света: {renamed_lights}")
        return {'FINISHED'}


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
    AGR_OT_rename_lights,
    AGR_OT_rename_lights_dialog,
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
    
    print("✅ Rename operators registered")


def unregister():
    if hasattr(bpy.types.Scene, "agr_rename_address"):
        del bpy.types.Scene.agr_rename_address
    if hasattr(bpy.types.Scene, "agr_glass_address"):
        del bpy.types.Scene.agr_glass_address
    if hasattr(bpy.types.Scene, "agr_glass_number"):
        del bpy.types.Scene.agr_glass_number
    if hasattr(bpy.types.Scene, "agr_glass_obj_type"):
        del bpy.types.Scene.agr_glass_obj_type
    
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

