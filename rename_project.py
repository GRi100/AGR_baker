# SPDX-License-Identifier: GPL-3.0-or-later

import os
import json
import re

import bpy
from bpy.types import Operator, Panel
from bpy.props import StringProperty

from . import os_utils
from . import utills
from . import check_highpoly_lowpoly


def _get_new_address(scene):
    address = getattr(scene, "agr_rp_address", "")
    if address:
        return address.strip()
    props = getattr(scene, "agr_scene_properties", None)
    if props:
        address = getattr(props, "Address", "")
        if address:
            return address.strip()
    return ""


def _get_project_root():
    path = os_utils._get_project_path()
    if not path:
        return ""
    return os.path.abspath(path)

def _is_lowpoly_collection(coll):
    try:
        return bool(re.match(r'^\d{4}', coll.name))
    except Exception:
        return False

def _obj_in_lowpoly_collection(obj):
    try:
        return any(_is_lowpoly_collection(coll) for coll in obj.users_collection)
    except Exception:
        return False

# ============= Scene properties (own, to avoid conflicts) =============

def register_scene_properties():
    """Registers addon scene properties (agr_rp_*)."""
    bpy.types.Scene.agr_rp_address = StringProperty(
        name="Address",
        description="Адрес для переименования проекта (если пусто — берется из SINTEZ AGR Checker)",
        default="",
    )
    bpy.types.Scene.agr_rp_project_lowpoly_number = StringProperty(
        name="Project Lowpoly Number",
        description="4-значный номер lowpoly при переименовании проекта",
        default="",
    )


def unregister_scene_properties():
    if hasattr(bpy.types.Scene, "agr_rp_address"):
        del bpy.types.Scene.agr_rp_address
    if hasattr(bpy.types.Scene, "agr_rp_project_lowpoly_number"):
        del bpy.types.Scene.agr_rp_project_lowpoly_number


# ============= Operators =============

class AGR_RP_OT_rename_project(Operator):
    """Переименование всего проекта с заданным Address"""
    bl_idname = "agr.rename_project"
    bl_label = "Переименовать проект"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        new_address = _get_new_address(context.scene)
        if not new_address:
            self.report({'ERROR'}, "Введите Address в панели переименования или в настройках SINTEZ AGR Checker")
            return {'CANCELLED'}

        lowpoly_number = getattr(context.scene, "agr_rp_project_lowpoly_number", "").strip()
        if lowpoly_number and (len(lowpoly_number) != 4 or not lowpoly_number.isdigit()):
            self.report({'ERROR'}, "Введите ровно 4 цифры для номера lowpoly (например: 0903)")
            return {'CANCELLED'}

        has_lowpoly = self.detect_lowpoly_objects(context)
        if has_lowpoly and not lowpoly_number:
            self.report({'ERROR'}, "Найдены lowpoly коллекции — укажите 4-значный номер во второй строке")
            return {'CANCELLED'}

        return self.execute_rename(context, new_address, lowpoly_number if has_lowpoly else None)

    def detect_lowpoly_objects(self, context):
        for obj in context.scene.objects:
            for coll in obj.users_collection:
                if re.match(r'^\d{4}', coll.name):
                    return True
        return False

    def execute_rename(self, context, new_address, lowpoly_number):
        self.report({'INFO'}, f"Начинается переименование проекта на адрес: {new_address}")

        highpoly_renamed = self.rename_highpoly_objects(context, new_address)
        lowpoly_renamed = 0
        if lowpoly_number:
            lowpoly_renamed = self.rename_lowpoly_objects(context, new_address)
        ucx_objects_renamed = self.rename_ucx_objects(context, new_address)
        textures_renamed = self.rename_textures_for_objects(context, new_address)
        geojson_fbx_renamed = self.rename_geojson_fbx_for_objects(context, new_address)
        lights_renamed = self.rename_lights_for_roots(context, new_address)
        self.distribute_to_collections(context, new_address, lowpoly_number)

        summary = (
            f"Проект переименован! Highpoly: {highpoly_renamed}, Lowpoly: {lowpoly_renamed}, "
            f"UCX: {ucx_objects_renamed}, Текстуры: {textures_renamed}, "
            f"GEOJSON/FBX: {geojson_fbx_renamed}, Свет: {lights_renamed}"
        )
        self.report({'INFO'}, summary)
        return {'FINISHED'}

    def rename_highpoly_objects(self, context, new_address):
        renamed_count = 0
        for obj in context.scene.objects:
            if obj.type != 'MESH':
                continue
            if _obj_in_lowpoly_collection(obj):
                continue
            obj_name = obj.name
            if re.search(r'\.\d{3}$', obj_name):
                continue
            match = re.match(r'^SM_(.+?)_(\d{3})_(Main|MainGlass)$', obj_name)
            if match:
                number = match.group(2)
                obj_type = match.group(3)
                obj.name = f"SM_{new_address}_{number}_{obj_type}"
                self.rename_materials(obj, new_address, number, obj_type)
                renamed_count += 1
                continue
            match = re.match(r'^SM_(.+?)_(Main|MainGlass)$', obj_name)
            if match:
                obj_type = match.group(2)
                obj.name = f"SM_{new_address}_{obj_type}"
                self.rename_materials(obj, new_address, None, obj_type)
                renamed_count += 1
                continue
            match = re.match(r'^SM_(.+?)_(Ground|GroundGlass)$', obj_name)
            if match:
                obj_type = match.group(2)
                obj.name = f"SM_{new_address}_{obj_type}"
                self.rename_materials(obj, new_address, None, obj_type)
                renamed_count += 1
                continue
        return renamed_count

    def rename_lowpoly_objects(self, context, new_address):
        renamed_count = 0
        for obj in context.scene.objects:
            if obj.type != 'MESH':
                continue
            if not _obj_in_lowpoly_collection(obj):
                continue
            obj_name = obj.name
            obj_name_clean = re.sub(r'(\.\d{3})$', '', obj_name)
            suffix_match = re.search(r'(\.\d{3})$', obj_name)
            suffix = suffix_match.group(1) if suffix_match else ""
            match = re.match(r'^SM_(.+?)_(\d{3})_(Main|MainGlass)$', obj_name_clean)
            if match:
                number = match.group(2)
                obj_type = match.group(3)
                obj.name = f"SM_{new_address}_{number}_{obj_type}{suffix}"
                self.rename_materials(obj, new_address, number, obj_type)
                renamed_count += 1
                continue
            match = re.match(r'^SM_(.+?)_(Ground|GroundGlass)$', obj_name_clean)
            if match:
                obj_type = match.group(2)
                obj.name = f"SM_{new_address}_{obj_type}{suffix}"
                self.rename_materials(obj, new_address, None, obj_type)
                renamed_count += 1
                continue
            match = re.match(r'^SM_(.+?)_(GroundEl|GroundElGlass|Flora)$', obj_name_clean)
            if match:
                obj_type = match.group(2)
                obj.name = f"SM_{new_address}_{obj_type}"
                self.rename_materials(obj, new_address, None, obj_type)
                renamed_count += 1
                continue
        return renamed_count

    def rename_materials(self, obj, address, number, obj_type):
        if obj.data.materials:
            for idx, mat_slot in enumerate(obj.data.materials, 1):
                if mat_slot:
                    if re.match(r'^M_Glass_\d{2}$', mat_slot.name):
                        continue
                    if number:
                        mat_name = f"M_{address}_{number}_{obj_type}_{idx}"
                    else:
                        mat_name = f"M_{address}_{obj_type}_{idx}"
                    mat_slot.name = mat_name

    def rename_ucx_objects(self, context, new_address):
        renamed_count = 0
        for obj in context.scene.objects:
            if obj.type != 'MESH':
                continue
            obj_name = obj.name
            match = re.match(r'^UCX_SM_(.+?)_(\d{3})_Main_(\d+)$', obj_name)
            if match:
                number = match.group(2)
                ucx_num = match.group(3)
                obj.name = f"UCX_SM_{new_address}_{number}_Main_{ucx_num}"
                renamed_count += 1
                continue
            match = re.match(r'^UCX_SM_(.+?)_Main_(\d+)$', obj_name)
            if match:
                ucx_num = match.group(2)
                obj.name = f"UCX_SM_{new_address}_Main_{ucx_num}"
                renamed_count += 1
                continue
            match = re.match(r'^UCX_SM_(.+?)_Ground_(\d+)$', obj_name)
            if match:
                ucx_num = match.group(2)
                obj.name = f"UCX_SM_{new_address}_Ground_{ucx_num}"
                renamed_count += 1
                continue
        return renamed_count

    def rename_textures_for_objects(self, context, new_address):
        renamed_count = 0
        for obj in context.scene.objects:
            if obj.type != 'MESH':
                continue
            if not obj.data.materials:
                continue
            obj_name = obj.name
            obj_name_clean = re.sub(r'\.\d{3}$', '', obj_name)
            if re.match(r'^SM_' + re.escape(new_address) + r'(_\d{3})?_Main$', obj_name_clean):
                if self.rename_textures_for_object(obj, new_address):
                    renamed_count += 1
                continue
            if re.match(r'^SM_' + re.escape(new_address) + r'(_\d{3})?_(Ground|GroundEl|GroundElGlass|Flora)$', obj_name_clean):
                if self.rename_textures_for_object(obj, new_address):
                    renamed_count += 1
                continue
        return renamed_count

    def rename_textures_for_object(self, obj, new_address):
        parsed = self.parse_object_name(obj.name)
        if not parsed:
            return False
        _, number, obj_type = parsed
        if obj_type not in ['Main', 'Ground', 'GroundEl', 'GroundElGlass', 'Flora']:
            return False

        texture_type = self.detect_texture_type(obj)
        if not texture_type:
            return False

        if texture_type == 'UDIM':
            return self.process_udim_textures(obj, new_address, number, obj_type)
        # Если все текстуры запакованы — переименовываем без распаковки
        if self._all_textures_packed(obj):
            return self._rename_packed_textures_in_place(obj, new_address, number, obj_type)
        return self.process_regular_textures(obj, new_address, number, obj_type)

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
        """Проверяет, что все regular-текстуры объекта запакованы."""
        for mat_slot in obj.data.materials:
            if not mat_slot or not mat_slot.use_nodes:
                continue
            for node in mat_slot.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    if node.image.source == 'TILED':
                        continue
                    if node.image.filepath and not node.image.packed_file:
                        return False
                elif node.type == 'NORMAL_MAP' and 'Color' in node.inputs:
                    color_input = node.inputs['Color']
                    if color_input.is_linked:
                        linked_node = color_input.links[0].from_node
                        if linked_node.type == 'TEX_IMAGE' and linked_node.image:
                            if linked_node.image.source != 'TILED':
                                if linked_node.image.filepath and not linked_node.image.packed_file:
                                    return False
        return True

    def _rename_packed_textures_in_place(self, obj, address, number, obj_type):
        """Переименовывает Name и File Name у запакованных текстур прямо в blend.
        Без распаковки, без создания файлов — только метаданные."""
        textures = self.get_regular_textures(obj)
        if not textures:
            return False

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

        return renamed_count > 0

    def process_udim_textures(self, obj, address, number, obj_type):
        texture_folder = self.get_udim_texture_folder(obj)
        if not texture_folder:
            self.report({'ERROR'}, "Не найдена папка с UDIM текстурами")
            return False
        if not os.path.exists(texture_folder):
            self.report({'ERROR'}, f"Папка с текстурами не найдена: {texture_folder}")
            return False
        renamed_count = self.rename_udim_textures(texture_folder, address, number, obj_type)
        if renamed_count == 0:
            return False

        new_folder_name = self.get_new_folder_name(address, number, obj_type)
        if not new_folder_name:
            return True
        parent_folder = os.path.dirname(texture_folder)
        new_folder_path = os.path.join(parent_folder, new_folder_name)
        try:
            if texture_folder != new_folder_path and os.path.exists(texture_folder):
                os.rename(texture_folder, new_folder_path)
                self.update_material_paths(obj, texture_folder, new_folder_path)
        except Exception as e:
            self.report({'WARNING'}, f"Текстуры переименованы, но папка не переименована: {e}")
        return True

    def get_udim_texture_folder(self, obj):
        for mat_slot in obj.data.materials:
            if mat_slot and mat_slot.use_nodes:
                for node in mat_slot.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image:
                        if node.image.source == 'TILED' and node.image.filepath:
                            abs_path = bpy.path.abspath(node.image.filepath)
                            folder_path = os.path.dirname(abs_path)
                            return folder_path
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
                if any(tag in filename for tag in ['_Ground_', '_GroundEl', '_Flora_']):
                    continue
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
                print(f"  Ошибка переименования текстуры {filename}: {e}")
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
            if not mat_slot:
                continue
            mat_slot.use_nodes = True
            tree = utills.get_material_node_tree(mat_slot, ensure=True)
            if not tree:
                continue
            nodes = tree.nodes
            bsdf = check_highpoly_lowpoly.CheckUtils.get_bsdf(mat_slot)
            if not bsdf:
                continue
            if bsdf.inputs['Base Color'].is_linked and 'Diffuse' in new_textures:
                diffuse_link = bsdf.inputs['Base Color'].links[0]
                diffuse_node = diffuse_link.from_node
                if diffuse_node.type == 'TEX_IMAGE':
                    self.load_new_texture(new_folder, new_textures['Diffuse'], diffuse_node, 'sRGB')
            for node in nodes:
                if node.type in ['SEPRGB', 'SEPARATE_COLOR', 'SEPARATE_XYZ']:
                    is_connected = any(link.to_node == bsdf for output in node.outputs for link in output.links)
                    if is_connected and node.inputs[0].is_linked and 'ERM' in new_textures:
                        erm_node = node.inputs[0].links[0].from_node
                        if erm_node.type == 'TEX_IMAGE':
                            self.load_new_texture(new_folder, new_textures['ERM'], erm_node, 'Non-Color')
                        break
            for node in nodes:
                if node.type == 'NORMAL_MAP':
                    if node.outputs['Normal'].is_linked and node.inputs['Color'].is_linked and 'Normal' in new_textures:
                        if any(link.to_node == bsdf for link in node.outputs['Normal'].links):
                            normal_node = node.inputs['Color'].links[0].from_node
                            if normal_node.type == 'TEX_IMAGE':
                                self.load_new_texture(new_folder, new_textures['Normal'], normal_node, 'Non-Color')
                            break

    def load_new_texture(self, folder, filename, node, color_space='sRGB'):
        try:
            base_name = filename.replace('.1001.png', '')
            udim_path = os.path.join(folder, f"{base_name}.<UDIM>.png")
            abs_path = os.path.abspath(udim_path)
            new_image = bpy.data.images.load(abs_path, check_existing=True)
            new_image.source = 'TILED'
            new_image.colorspace_settings.name = color_space
            node.image = new_image
            return bpy.path.relpath(abs_path)
        except Exception as e:
            print(f"  Ошибка загрузки текстуры {filename}: {e}")
            return None

    def process_regular_textures(self, obj, address, number, obj_type):
        textures = self.get_regular_textures(obj)
        if not textures:
            return False

        project_root = _get_project_root()
        if project_root and os.path.exists(project_root):
            target_root = project_root
        else:
            blend_filepath = bpy.data.filepath
            if not blend_filepath:
                self.report({'ERROR'}, "Сохраните .blend файл или укажите путь к проекту")
                return False
            target_root = os.path.dirname(blend_filepath)

        low_texture_folder = os.path.join(target_root, "low_texture")
        if not os.path.exists(low_texture_folder):
            os.makedirs(low_texture_folder)

        renamed_count, new_texture_paths, loaded_images = self.process_textures(
            obj, textures, low_texture_folder, address, number, obj_type
        )
        if renamed_count > 0 and new_texture_paths:
            self._pack_textures_and_cleanup(low_texture_folder, loaded_images)
            return True
        return False

    def get_regular_textures(self, obj):
        textures = []
        processed_images = set()
        has_regular = lambda im: im.source != 'TILED' and (im.filepath or im.packed_file)
        for mat_slot in obj.data.materials:
            if mat_slot and mat_slot.use_nodes:
                for node in mat_slot.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image:
                        if has_regular(node.image):
                            if node.image.name not in processed_images:
                                textures.append(node.image)
                                processed_images.add(node.image.name)
                    elif node.type == 'NORMAL_MAP':
                        if 'Color' in node.inputs:
                            color_input = node.inputs['Color']
                            if color_input.is_linked:
                                linked_node = color_input.links[0].from_node
                                if linked_node.type == 'TEX_IMAGE' and linked_node.image:
                                    if has_regular(linked_node.image):
                                        if linked_node.image.name not in processed_images:
                                            textures.append(linked_node.image)
                                            processed_images.add(linked_node.image.name)
        return textures if textures else None

    def get_texture_type_from_filename(self, filename):
        filename_lower = filename.lower()
        if '_d_' in filename_lower or filename_lower.endswith('_d.png'):
            return 'd'
        if '_o_' in filename_lower or filename_lower.endswith('_o.png'):
            return 'o'
        if '_m_' in filename_lower or filename_lower.endswith('_m.png'):
            return 'm'
        if '_n_' in filename_lower or filename_lower.endswith('_n.png'):
            return 'n'
        if '_r_' in filename_lower or filename_lower.endswith('_r.png'):
            return 'r'
        return None

    def get_color_space_for_texture_type(self, tex_type):
        if tex_type == 'd':
            return 'sRGB'
        if tex_type in ['o', 'm', 'r']:
            return 'Non-Color'
        if tex_type == 'n':
            return 'Non-Color'
        return 'sRGB'

    def process_textures(self, obj, textures, target_folder, address, number, obj_type):
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
            except Exception:
                pass

        if new_texture_paths:
            loaded_images = self.reconnect_textures(obj, new_texture_paths)
            for old_img in old_images:
                try:
                    bpy.data.images.remove(old_img)
                except Exception:
                    pass

        return renamed_count, new_texture_paths, loaded_images

    def _pack_textures_and_cleanup(self, folder_path, images):
        packed_any = False
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
                    packed_any = True
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
            os.rmdir(folder_path)
        except Exception:
            pass

        if packed_any:
            self.report({'INFO'}, "Lowpoly текстуры упакованы в .blend и папка удалена")

    def reconnect_textures(self, obj, new_texture_paths):
        loaded_images = []
        for mat_slot in obj.data.materials:
            if not mat_slot or not mat_slot.use_nodes:
                continue
            nodes = mat_slot.node_tree.nodes
            links = mat_slot.node_tree.links
            principled = None
            for node in nodes:
                if node.type == 'BSDF_PRINCIPLED':
                    principled = node
                    break
            if not principled:
                continue

            old_tex_nodes = [node for node in nodes if node.type == 'TEX_IMAGE']
            old_normal_nodes = [node for node in nodes if node.type == 'NORMAL_MAP']
            for node in old_tex_nodes:
                nodes.remove(node)
            for node in old_normal_nodes:
                nodes.remove(node)

            x_offset = -300
            y_offset = 0
            for tex_type, tex_path in new_texture_paths.items():
                abs_path = os.path.abspath(tex_path)
                if not os.path.exists(abs_path):
                    continue
                tex_node = nodes.new(type='ShaderNodeTexImage')
                tex_node.location = (principled.location.x + x_offset, principled.location.y + y_offset)
                img = bpy.data.images.load(abs_path, check_existing=True)
                tex_node.image = img
                tex_node.image.colorspace_settings.name = self.get_color_space_for_texture_type(tex_type)
                loaded_images.append(img)
                if tex_type == 'd':
                    links.new(tex_node.outputs['Color'], principled.inputs['Base Color'])
                elif tex_type == 'o':
                    links.new(tex_node.outputs['Color'], principled.inputs['Alpha'])
                elif tex_type == 'm':
                    links.new(tex_node.outputs['Color'], principled.inputs['Metallic'])
                elif tex_type == 'n':
                    normal_map = nodes.new(type='ShaderNodeNormalMap')
                    normal_map.location = (principled.location.x + x_offset + 200, principled.location.y + y_offset)
                    links.new(tex_node.outputs['Color'], normal_map.inputs['Color'])
                    links.new(normal_map.outputs['Normal'], principled.inputs['Normal'])
                elif tex_type == 'r':
                    links.new(tex_node.outputs['Color'], principled.inputs['Roughness'])
        return loaded_images

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

    def get_texture_folder_from_material(self, obj):
        for mat_slot in obj.data.materials:
            if mat_slot and mat_slot.use_nodes:
                for node in mat_slot.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image:
                        if node.image.source == 'TILED' and node.image.filepath:
                            abs_path = bpy.path.abspath(node.image.filepath)
                            folder_path = os.path.dirname(abs_path)
                            return folder_path
        project_root = _get_project_root()
        if project_root and os.path.exists(project_root):
            return project_root
        return None

    def rename_geojson_fbx_for_objects(self, context, new_address):
        renamed_count = 0
        processed_keys = set()
        project_root = _get_project_root()
        for obj in context.scene.objects:
            if obj.type != 'MESH':
                continue
            obj_name = obj.name
            obj_name_clean = re.sub(r'\.\d{3}$', '', obj_name)
            match_main = re.match(r'^SM_' + re.escape(new_address) + r'(_\d{3})?_(Main|MainGlass)$', obj_name_clean)
            match_ground = re.match(r'^SM_' + re.escape(new_address) + r'_(Ground|GroundGlass)$', obj_name_clean)
            if match_main or match_ground:
                try:
                    parsed = self.parse_object_name(obj_name_clean)
                    if not parsed:
                        continue
                    current_address, number, obj_type = parsed
                    if obj_type not in ['Main', 'Ground']:
                        continue
                    if project_root and os.path.exists(project_root):
                        texture_folder = project_root
                    else:
                        texture_folder = self.get_texture_folder_from_material(obj)
                    if not texture_folder or not os.path.exists(texture_folder):
                        continue
                    key = (texture_folder, obj_type, number)
                    if key in processed_keys:
                        continue
                    processed_keys.add(key)
                    geojson_renamed = self.rename_geojson_in_folder(texture_folder, new_address, number, obj_type)
                    fbx_renamed = self.rename_fbx_in_folder(texture_folder, new_address, number, obj_type)
                    if geojson_renamed or fbx_renamed:
                        renamed_count += 1
                except Exception as e:
                    print(f"  Ошибка переименования GEOJSON/FBX для {obj_name}: {e}")
        return renamed_count

    def rename_geojson_in_folder(self, folder_path, new_address, number, obj_type):
        try:
            if obj_type == 'Main' and number:
                pattern = r'^SM_(.+?)_' + re.escape(number) + r'\.geojson$'
                new_name = f"SM_{new_address}_{number}.geojson"
            elif obj_type == 'Main' and not number:
                pattern = r'^SM_(.+?)\.geojson$'
                new_name = f"SM_{new_address}.geojson"
            elif obj_type == 'Ground':
                pattern = r'^SM_(.+?)_Ground\.geojson$'
                new_name = f"SM_{new_address}_Ground.geojson"
            else:
                return False
            for root, _, files in os.walk(folder_path):
                for filename in files:
                    if obj_type == 'Main' and not number:
                        if filename.endswith('_Ground.geojson'):
                            continue
                    match = re.match(pattern, filename)
                    if match:
                        old_path = os.path.join(root, filename)
                        new_path = os.path.join(root, new_name)
                        with open(old_path, 'r', encoding='utf-8') as f:
                            geojson_data = json.load(f)
                        old_address = match.group(1)
                        self.update_glass_materials_in_geojson(geojson_data, old_address, new_address)
                        with open(new_path, 'w', encoding='utf-8') as f:
                            json.dump(geojson_data, f, ensure_ascii=False, indent=2)
                        if old_path != new_path and os.path.exists(old_path):
                            os.remove(old_path)
                        return True
        except Exception as e:
            print(f"    Ошибка переименования GEOJSON: {e}")
        return False

    def update_glass_materials_in_geojson(self, geojson_data, old_address, new_address):
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
        renamed = False
        try:
            if obj_type == 'Main' and number:
                pattern_main = r'^SM_(.+?)_' + re.escape(number) + r'\.fbx$'
                pattern_light = r'^SM_(.+?)_' + re.escape(number) + r'_Light\.fbx$'
                new_main = f"SM_{new_address}_{number}.fbx"
                new_light = f"SM_{new_address}_{number}_Light.fbx"
            elif obj_type == 'Main' and not number:
                pattern_main = r'^SM_(.+?)\.fbx$'
                pattern_light = r'^SM_(.+?)_Light\.fbx$'
                new_main = f"SM_{new_address}.fbx"
                new_light = f"SM_{new_address}_Light.fbx"
            elif obj_type == 'Ground':
                pattern_main = r'^SM_(.+?)_Ground\.fbx$'
                pattern_light = r'^SM_(.+?)_Ground_Light\.fbx$'
                new_main = f"SM_{new_address}_Ground.fbx"
                new_light = f"SM_{new_address}_Ground_Light.fbx"
            else:
                return False
            for root, _, files in os.walk(folder_path):
                for filename in files:
                    if obj_type == 'Main' and not number:
                        if filename.endswith('_Ground.fbx') or filename.endswith('_Ground_Light.fbx'):
                            continue
                    skip_main_match = obj_type == 'Main' and not number and filename.endswith('_Light.fbx')
                    if not skip_main_match:
                        match = re.match(pattern_main, filename)
                        if match:
                            old_path = os.path.join(root, filename)
                            new_path = os.path.join(root, new_main)
                            if old_path != new_path:
                                os.rename(old_path, new_path)
                                renamed = True
                    match = re.match(pattern_light, filename)
                    if match:
                        old_path = os.path.join(root, filename)
                        new_path = os.path.join(root, new_light)
                        if old_path != new_path:
                            os.rename(old_path, new_path)
                            renamed = True
        except Exception as e:
            print(f"    Ошибка переименования FBX: {e}")
        return renamed

    def rename_lights_for_roots(self, context, new_address):
        renamed_count = 0
        for obj in context.scene.objects:
            if obj.type != 'EMPTY':
                continue
            obj_name = obj.name
            match = re.match(r'^(.+?)_Ground_Root$', obj_name)
            if match:
                obj.name = f"{new_address}_Ground_Root"
                self.rename_child_lights(obj, new_address, None, 'Ground')
                renamed_count += 1
                continue
            match = re.match(r'^(.+?)_(\d{3})_Root$', obj_name)
            if match:
                number = match.group(2)
                obj.name = f"{new_address}_{number}_Root"
                self.rename_child_lights(obj, new_address, number, 'Main')
                renamed_count += 1
                continue
            match = re.match(r'^(.+?)_Root$', obj_name)
            if match:
                obj.name = f"{new_address}_Root"
                self.rename_child_lights(obj, new_address, None, 'Main')
                renamed_count += 1
                continue
        return renamed_count

    def rename_child_lights(self, root_obj, address, number, obj_type):
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
            if obj_type == 'Ground':
                new_light_name = f"{address}_Ground_{lighttype_name}_{counter:03d}"
            elif obj_type == 'Main' and number:
                new_light_name = f"{address}_{number}_{lighttype_name}_{counter:03d}"
            else:
                new_light_name = f"{address}_{lighttype_name}_{counter:03d}"
            light_obj.name = new_light_name

    def distribute_to_collections(self, context, new_address, lowpoly_number=None):
        if lowpoly_number:
            self._distribute_highpoly(context, new_address)
            self._distribute_lowpoly(context, new_address, lowpoly_number)
        else:
            self._distribute_highpoly(context, new_address)

    def _distribute_highpoly(self, context, address):
        collections_data = {}

        for obj in context.scene.objects:
            if _obj_in_lowpoly_collection(obj):
                continue
            obj_name = obj.name

            match = re.match(r'^SM_' + re.escape(address) + r'_(\d{3})_(Main|MainGlass)$', obj_name)
            if match:
                number = match.group(1)
                coll_name = f"SM_{address}_{number}.fbx"
                collections_data.setdefault(coll_name, []).append(obj)
                continue

            match = re.match(r'^UCX_SM_' + re.escape(address) + r'_(\d{3})_Main_\d+$', obj_name)
            if match:
                number = match.group(1)
                coll_name = f"SM_{address}_{number}.fbx"
                collections_data.setdefault(coll_name, []).append(obj)
                continue

            match = re.match(r'^' + re.escape(address) + r'_(\d{3})_Root$', obj_name)
            if match:
                number = match.group(1)
                coll_name = f"SM_{address}_{number}_Light.fbx"
                collections_data.setdefault(coll_name, []).append(obj)
                for child in obj.children:
                    if child.type == 'LIGHT':
                        collections_data[coll_name].append(child)
                continue

            match = re.match(r'^' + re.escape(address) + r'_(\d{3})_(Spot|Point)_\d+$', obj_name)
            if match:
                continue

            match = re.match(r'^SM_' + re.escape(address) + r'_(Main|MainGlass)$', obj_name)
            if match:
                coll_name = f"SM_{address}.fbx"
                collections_data.setdefault(coll_name, []).append(obj)
                continue

            match = re.match(r'^UCX_SM_' + re.escape(address) + r'_Main_\d+$', obj_name)
            if match:
                coll_name = f"SM_{address}.fbx"
                collections_data.setdefault(coll_name, []).append(obj)
                continue

            match = re.match(r'^' + re.escape(address) + r'_Root$', obj_name)
            if match:
                coll_name = f"SM_{address}_Light.fbx"
                collections_data.setdefault(coll_name, []).append(obj)
                for child in obj.children:
                    if child.type == 'LIGHT':
                        collections_data[coll_name].append(child)
                continue

            match = re.match(r'^' + re.escape(address) + r'_(Spot|Point)_\d+$', obj_name)
            if match:
                continue

            match = re.match(r'^SM_' + re.escape(address) + r'_(Ground|GroundGlass)$', obj_name)
            if match:
                coll_name = f"SM_{address}_Ground.fbx"
                collections_data.setdefault(coll_name, []).append(obj)
                continue

            match = re.match(r'^UCX_SM_' + re.escape(address) + r'_Ground_\d+$', obj_name)
            if match:
                coll_name = f"SM_{address}_Ground.fbx"
                collections_data.setdefault(coll_name, []).append(obj)
                continue

            match = re.match(r'^' + re.escape(address) + r'_Ground_Root$', obj_name)
            if match:
                coll_name = f"SM_{address}_Ground_Light.fbx"
                collections_data.setdefault(coll_name, []).append(obj)
                for child in obj.children:
                    if child.type == 'LIGHT':
                        collections_data[coll_name].append(child)
                continue

            match = re.match(r'^' + re.escape(address) + r'_Ground_(Spot|Point)_\d+$', obj_name)
            if match:
                continue

        total_objects = 0
        collections_created = 0
        for coll_name, objects in collections_data.items():
            if not objects:
                continue
            if coll_name not in bpy.data.collections:
                new_coll = bpy.data.collections.new(coll_name)
                context.scene.collection.children.link(new_coll)
                collections_created += 1
            else:
                new_coll = bpy.data.collections[coll_name]
            for obj in objects:
                for old_coll in obj.users_collection:
                    old_coll.objects.unlink(obj)
                if obj.name not in new_coll.objects:
                    new_coll.objects.link(obj)
                    total_objects += 1

        collections_removed = self._remove_empty_collections(context)

        if total_objects > 0:
            msg = f"Распределено {total_objects} highpoly объектов в {len(collections_data)} коллекций (создано новых: {collections_created})"
            if collections_removed > 0:
                msg += f", удалено пустых коллекций: {collections_removed}"
            self.report({'INFO'}, msg)
        else:
            self.report({'WARNING'}, f"Не найдено highpoly объектов с адресом {address}")

    def _distribute_lowpoly(self, context, address, lowpoly_number):
        main_groups = {}
        ground_objects = []

        for obj in context.scene.objects:
            if obj.type != 'MESH':
                continue
            if not _obj_in_lowpoly_collection(obj):
                continue
            obj_name = obj.name
            obj_name_clean = re.sub(r'\.\d{3}$', '', obj_name)

            match = re.match(r'^SM_' + re.escape(address) + r'_(\d{3})_(Main|MainGlass)', obj_name)
            if match:
                number = match.group(1)
                group_key = f"{address}_{number}"
                main_groups.setdefault(group_key, []).append(obj)
                continue

            match = re.match(r'^SM_' + re.escape(address) + r'_(Ground|GroundGlass)', obj_name)
            if match:
                ground_objects.append(obj)
                continue

            match = re.match(r'^SM_' + re.escape(address) + r'_(GroundEl|GroundElGlass)$', obj_name_clean)
            if match:
                ground_objects.append(obj)
                continue

            match = re.match(r'^SM_' + re.escape(address) + r'_Flora$', obj_name_clean)
            if match:
                ground_objects.append(obj)
                continue

        collections_data = {}
        groups_to_pack = []
        for group_key, objects in main_groups.items():
            total_tris = sum(len(obj.data.polygons) for obj in objects if obj.type == 'MESH' and obj.data)
            groups_to_pack.append((group_key, objects, total_tris))

        max_tris = 150000
        current_batch = []
        current_tris = 0
        batch_index = 1

        for _, group_objects, group_tris in groups_to_pack:
            if current_tris + group_tris > max_tris and current_batch:
                coll_name = f"{lowpoly_number}_{address}_{batch_index:02d}.fbx"
                collections_data[coll_name] = current_batch
                current_batch = group_objects.copy()
                current_tris = group_tris
                batch_index += 1
            else:
                current_batch.extend(group_objects)
                current_tris += group_tris

        if current_batch:
            coll_name = f"{lowpoly_number}_{address}_{batch_index:02d}.fbx"
            collections_data[coll_name] = current_batch

        if ground_objects:
            coll_name = f"{lowpoly_number}_{address}_Ground.fbx"
            collections_data[coll_name] = ground_objects

        total_objects = 0
        collections_created = 0
        for coll_name, objects in collections_data.items():
            if not objects:
                continue
            if coll_name not in bpy.data.collections:
                new_coll = bpy.data.collections.new(coll_name)
                context.scene.collection.children.link(new_coll)
                collections_created += 1
            else:
                new_coll = bpy.data.collections[coll_name]

            for obj in objects:
                for old_coll in obj.users_collection:
                    old_coll.objects.unlink(obj)
                if obj.name not in new_coll.objects:
                    new_coll.objects.link(obj)
                    total_objects += 1

        collections_removed = self._remove_empty_collections(context)
        self._rename_lowpoly_folder_and_fbx(lowpoly_number, address)

        if total_objects > 0:
            msg = f"Распределено {total_objects} lowpoly объектов в {len(collections_data)} коллекций (создано новых: {collections_created})"
            if collections_removed > 0:
                msg += f", удалено пустых коллекций: {collections_removed}"
            self.report({'INFO'}, msg)
        else:
            self.report({'WARNING'}, f"Не найдено lowpoly объектов с адресом {address}")

    def _remove_empty_collections(self, context):
        removed_count = 0

        def remove_empty_recursive(collection):
            nonlocal removed_count
            for child in list(collection.children):
                remove_empty_recursive(child)
            if len(collection.objects) == 0 and len(collection.children) == 0:
                if collection != context.scene.collection:
                    for parent in bpy.data.collections:
                        if collection.name in parent.children:
                            parent.children.unlink(collection)
                    if collection.name in context.scene.collection.children:
                        context.scene.collection.children.unlink(collection)
                    bpy.data.collections.remove(collection)
                    removed_count += 1

        remove_empty_recursive(context.scene.collection)
        return removed_count

    def _rename_lowpoly_folder_and_fbx(self, lowpoly_number, new_address):
        root_dir = _get_project_root()
        if not root_dir:
            blend_path = bpy.data.filepath
            if not blend_path:
                return
            root_dir = os.path.dirname(blend_path)

        old_folder = None
        old_folder_name = None
        for item in os.listdir(root_dir):
            item_path = os.path.join(root_dir, item)
            if os.path.isdir(item_path):
                match = re.match(r'^(\d{4})_(.+)$', item)
                if match:
                    old_folder = item_path
                    old_folder_name = item
                    break

        if not old_folder:
            new_folder_name = f"{lowpoly_number}_{new_address}"
            new_folder_path = os.path.join(root_dir, new_folder_name)
            os.makedirs(new_folder_path, exist_ok=True)
            return

        for filename in os.listdir(old_folder):
            if filename.endswith('.fbx'):
                old_fbx_path = os.path.join(old_folder, filename)
                match = re.match(r'^(\d{4})_(.+?)(_\d{2}|_Ground)(\.fbx)$', filename)
                if match:
                    suffix = match.group(3)
                    extension = match.group(4)
                    new_fbx_name = f"{lowpoly_number}_{new_address}{suffix}{extension}"
                    new_fbx_path = os.path.join(old_folder, new_fbx_name)
                    try:
                        os.rename(old_fbx_path, new_fbx_path)
                    except Exception:
                        pass

        new_folder_name = f"{lowpoly_number}_{new_address}"
        new_folder_path = os.path.join(root_dir, new_folder_name)
        if old_folder != new_folder_path:
            try:
                os.rename(old_folder, new_folder_path)
            except Exception:
                pass


class AGR_RP_OT_rename_project_input_lowpoly_number(Operator):
    """Диалог ввода 4-значного номера для lowpoly при переименовании проекта"""
    bl_idname = "agr.rename_project_input_lowpoly_number"
    bl_label = "Введите номер lowpoly коллекции"
    bl_options = {'REGISTER', 'UNDO'}

    lowpoly_number: StringProperty(
        name="Номер коллекции",
        description="4-значный номер lowpoly коллекции (0000-9999)",
        default="0000",
        maxlen=4,
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "lowpoly_number", text="Номер")
        layout.label(text="Введите ровно 4 цифры (0000-9999)")

    def execute(self, context):
        if len(self.lowpoly_number) != 4 or not self.lowpoly_number.isdigit():
            self.report({'ERROR'}, "Введите ровно 4 цифры (например: 0903)")
            return {'CANCELLED'}
        new_address = _get_new_address(context.scene)
        if not new_address:
            self.report({'ERROR'}, "Address не установлен")
            return {'CANCELLED'}
        context.scene.agr_rp_project_lowpoly_number = self.lowpoly_number
        bpy.ops.agr.rename_project('EXEC_DEFAULT')
        context.scene.agr_rp_project_lowpoly_number = ""
        return {'FINISHED'}


# ============= Panel =============

class AGR_RP_PT_main_panel(Panel):
    """Панель переименования всего проекта"""
    bl_label = "Переименование проекта"
    bl_idname = "AGR_RP_PT_main_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "SINTEZ AGR"
    bl_parent_id = "VIEW3D_PT_Main"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        box = layout.box()
        box.label(text="Переименование проекта", icon='FILE_FOLDER')
        box.prop(scene, "agr_rp_address", text="Address")
        box.prop(scene, "agr_rp_project_lowpoly_number", text="Lowpoly номер")
        if not _get_project_root():
            box.label(text="Укажите путь к проекту в основном разделе", icon='INFO')
        row = box.row()
        row.scale_y = 2.0
        if _get_new_address(scene):
            row.operator("agr.rename_project", text="Переименовать весь проект", icon='ERROR')
        else:
            row.enabled = False
            row.operator("agr.rename_project", text="Введите Address")


classes = (
    AGR_RP_OT_rename_project,
    AGR_RP_OT_rename_project_input_lowpoly_number,
    AGR_RP_PT_main_panel,
)
