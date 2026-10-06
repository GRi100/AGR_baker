# SPDX-License-Identifier: GPL-3.0-or-later
"""
AGR Rename Project operator for AGR Tools
Based on rename_project.py - handles full project renaming
"""

import os
import json
import re

import bpy
from bpy.types import Operator
from bpy.props import StringProperty

from .log import agr_report, unregister_classes
from . import rename_shared
from .rename_shared import (
    RENAME_ALLOWED_TYPES,
    parse_sm_name,
    get_texture_type_from_filename,
    get_color_space_for_texture_type,
    udim_aware_material_name,
)


def _agr_baker_get_new_address(scene):
    """Get address from scene properties"""
    address = getattr(scene, "agr_rename_address", "")
    if address:
        return address.strip()
    return ""


def _agr_baker_get_project_root():
    """Get project root from blend file location"""
    blend_path = bpy.data.filepath
    if not blend_path:
        return ""
    return os.path.dirname(blend_path)


def _agr_baker_is_lowpoly_collection(coll):
    """Check if collection is lowpoly (starts with 4 digits)"""
    try:
        return bool(re.match(r'^\d{4}', coll.name))
    except Exception:
        return False


def _agr_baker_obj_in_lowpoly_collection(obj):
    """Check if object is in lowpoly collection"""
    try:
        return any(_agr_baker_is_lowpoly_collection(coll) for coll in obj.users_collection)
    except Exception:
        return False


# ============= Scene properties =============

def register_scene_properties():
    """Registers addon scene properties for project renaming."""
    bpy.types.Scene.agr_rp_project_lowpoly_number = StringProperty(
        name="Project Lowpoly Number",
        description="4-значный номер lowpoly при переименовании проекта",
        default="",
    )


def unregister_scene_properties():
    if hasattr(bpy.types.Scene, "agr_rp_project_lowpoly_number"):
        del bpy.types.Scene.agr_rp_project_lowpoly_number


# ============= Operator =============

class AGR_RP_OT_rename_project(Operator):
    """Переименование всего проекта с заданным Address"""
    bl_idname = "agr.rename_project"
    bl_label = "Переименовать проект"
    # NO 'UNDO': the operator renames files, folders and geojson on disk, and
    # Ctrl+Z would restore only the names inside the .blend — leaving the scene
    # pointing at paths that no longer exist. An honest REGISTER-only operator
    # plus an explicit confirmation is the lesser evil (RENAME-6).
    bl_options = {'REGISTER'}

    # Set in execute(); class-level defaults keep the helpers callable when a
    # test or another operator drives them directly.
    _old_addresses = frozenset()
    _emptied_collections = frozenset()

    def _plan_summary(self, context):
        """Rough scale of the damage for the confirmation popup — counted
        BEFORE anything is touched."""
        objects = 0
        roots = 0
        for obj in context.scene.objects:
            if obj.type == 'MESH' and obj.name.startswith(('SM_', 'UCX_SM_')):
                objects += 1
            elif obj.type == 'EMPTY' and obj.name.endswith('_Root'):
                roots += 1

        root_dir = _agr_baker_get_project_root()
        folders = 0
        files = 0
        if root_dir and os.path.isdir(root_dir):
            try:
                for entry in os.listdir(root_dir):
                    path = os.path.join(root_dir, entry)
                    if os.path.isdir(path):
                        if re.match(r'^\d{4}_', entry) or entry.startswith('SM_'):
                            folders += 1
                    elif entry.lower().endswith(('.fbx', '.geojson', '.png')):
                        files += 1
            except OSError:
                pass
        return objects, roots, folders, files

    def invoke(self, context, event):
        objects, roots, folders, files = self._plan_summary(context)
        message = (
            f"Объектов SM_/UCX_: {objects}, Root света: {roots}, "
            f"папок: {folders}, файлов рядом с .blend: {files}. "
            "Будут переименованы файлы и папки на диске — отмена невозможна."
        )
        try:
            return context.window_manager.invoke_confirm(
                self, event,
                title="Переименовать ВЕСЬ ПРОЕКТ?",
                message=message,
                confirm_text="Переименовать",
            )
        except TypeError:
            # Older Blender builds: invoke_confirm without title/message
            agr_report(self, 'WARNING', message)
            return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        new_address = _agr_baker_get_new_address(context.scene)
        if not new_address:
            agr_report(self, 'ERROR', "Введите Address в панели AGR_rename")
            return {'CANCELLED'}

        lowpoly_number = getattr(context.scene, "agr_rp_project_lowpoly_number", "").strip()
        if lowpoly_number and (len(lowpoly_number) != 4 or not lowpoly_number.isdigit()):
            agr_report(self, 'ERROR', "Введите ровно 4 цифры для номера lowpoly (например: 0903)")
            return {'CANCELLED'}

        has_lowpoly = self.detect_lowpoly_objects(context)
        if has_lowpoly and not lowpoly_number:
            agr_report(self, 'ERROR',
                       "Найдены lowpoly коллекции — укажите 4-значный номер во второй строке")
            return {'CANCELLED'}

        # Addresses replaced during this run — the geojson/FBX pass may only
        # touch files that carry one of them (RENAME-5).
        self._old_addresses = set()
        # Collections THIS operator emptied — the only ones it may delete
        # afterwards (RENAME-6).
        self._emptied_collections = set()

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

    def _rename_sm_object(self, obj, new_address, keep_suffix):
        """Rename ONE SM_* object + its materials through the shared parser.
        Returns True when the name matched the convention.

        Both branches used to carry their own regex ladders: the highpoly one
        knew four types out of seven and skipped every `.001` duplicate, so
        Flora/GroundEl kept the OLD address and nothing said so (RENAME-8)."""
        parsed = parse_sm_name(obj.name)
        if not parsed:
            return False
        _address, number, obj_type = parsed
        if obj_type not in RENAME_ALLOWED_TYPES['materials']:
            return False

        suffix = ""
        if keep_suffix:
            suffix_match = re.search(r'(\.\d{3})$', obj.name)
            suffix = suffix_match.group(1) if suffix_match else ""

        if _address:
            if not isinstance(self._old_addresses, set):
                self._old_addresses = set(self._old_addresses)
            self._old_addresses.add(_address)
        if number:
            obj.name = f"SM_{new_address}_{number}_{obj_type}{suffix}"
        else:
            obj.name = f"SM_{new_address}_{obj_type}{suffix}"
        self.rename_materials(obj, new_address, number, obj_type)
        return True

    def _report_unmatched(self, unmatched):
        if unmatched:
            agr_report(self, 'WARNING',
                       "Не подошли под шаблон SM_Адрес[_NNN]_Тип и оставлены как есть: "
                       + ", ".join(sorted(unmatched)))

    def rename_highpoly_objects(self, context, new_address):
        renamed_count = 0
        unmatched = []
        for obj in context.scene.objects:
            if obj.type != 'MESH':
                continue
            if _agr_baker_obj_in_lowpoly_collection(obj):
                continue
            if not obj.name.startswith('SM_'):
                continue
            if self._rename_sm_object(obj, new_address, keep_suffix=True):
                renamed_count += 1
            else:
                unmatched.append(obj.name)
        self._report_unmatched(unmatched)
        return renamed_count

    def rename_lowpoly_objects(self, context, new_address):
        renamed_count = 0
        unmatched = []
        for obj in context.scene.objects:
            if obj.type != 'MESH':
                continue
            if not _agr_baker_obj_in_lowpoly_collection(obj):
                continue
            if not obj.name.startswith('SM_'):
                continue
            if self._rename_sm_object(obj, new_address, keep_suffix=True):
                renamed_count += 1
            else:
                unmatched.append(obj.name)
        self._report_unmatched(unmatched)
        return renamed_count

    def rename_materials(self, obj, address, number, obj_type):
        if obj.data.materials:
            for idx, mat_slot in enumerate(obj.data.materials, 1):
                # udim_aware_material_name keeps the shared Ground material of
                # GroundEl/Flora siblings on its documented Ground name and
                # skips glass materials.
                mat_name = udim_aware_material_name(mat_slot, address, number,
                                                    obj_type, idx)
                if not mat_name or mat_slot.name == mat_name:
                    continue
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
        return get_texture_type_from_filename(filename)

    def get_color_space_for_texture_type(self, tex_type):
        return get_color_space_for_texture_type(tex_type)

    def process_udim_textures(self, obj, address, number, obj_type):
        texture_folder = self.get_udim_texture_folder(obj)
        if not texture_folder:
            agr_report(self, 'ERROR', "Не найдена папка с UDIM текстурами")
            return False
        if not os.path.exists(texture_folder):
            agr_report(self, 'ERROR', f"Папка с текстурами не найдена: {texture_folder}")
            return False

        renamed_count, _folder_renamed = rename_shared.process_udim_textures(
            self, obj, texture_folder, address, number, obj_type)
        return renamed_count > 0

    def get_udim_texture_folder(self, obj):
        for mat_slot in obj.data.materials:
            if mat_slot and mat_slot.use_nodes:
                for node in mat_slot.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image:
                        if node.image.source == 'TILED' and node.image.filepath:
                            abs_path = bpy.path.abspath(node.image.filepath)
                            return os.path.dirname(abs_path)
        return None

    def get_texture_type(self, filename):
        return rename_shared.udim_texture_type(filename)

    def get_new_folder_name(self, address, number, obj_type):
        return rename_shared.get_new_udim_folder_name(address, number, obj_type)

    def update_material_paths(self, obj, old_folder, new_folder):
        return rename_shared.update_udim_material_paths(self, obj, new_folder)

    def process_regular_textures(self, obj, address, number, obj_type):
        """Process regular (non-UDIM) textures through the shared pipeline."""
        project_root = _agr_baker_get_project_root()
        if project_root and os.path.exists(project_root):
            target_root = project_root
        else:
            blend_filepath = bpy.data.filepath
            if not blend_filepath:
                agr_report(self, 'ERROR', "Сохраните .blend файл")
                return False
            target_root = os.path.dirname(blend_filepath)

        low_texture_folder = os.path.join(target_root, "low_texture")
        if not os.path.exists(low_texture_folder):
            os.makedirs(low_texture_folder)

        renamed_count, _warnings = rename_shared.process_object_textures(
            self, obj, low_texture_folder, address, number, obj_type)
        return renamed_count > 0

    def reconnect_textures(self, obj, new_texture_paths):
        loaded, _applied = rename_shared.reconnect_textures(obj, new_texture_paths)
        return loaded


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

    def rename_geojson_fbx_for_objects(self, context, new_address):
        renamed_count = 0
        processed_keys = set()
        project_root = _agr_baker_get_project_root()
        # The file's own address must be one we actually renamed away from (or
        # the new one, for an idempotent re-run) — otherwise a stray delivery
        # of another building would be adopted.
        allowed = set(self._old_addresses) | {new_address}

        for obj in context.scene.objects:
            if obj.type != 'MESH':
                continue
            parsed = parse_sm_name(obj.name)
            if not parsed:
                continue
            address, number, obj_type = parsed
            if address != new_address:
                continue
            if obj_type in ('MainGlass',):
                obj_type = 'Main'
            if obj_type in ('GroundGlass',):
                obj_type = 'Ground'
            if obj_type not in ('Main', 'Ground'):
                continue
            try:
                folders = rename_shared.geojson_search_dirs(project_root, allowed, number, obj_type)
                if not folders:
                    folder = self.get_texture_folder_from_material(obj)
                    folders = [folder] if folder and os.path.isdir(folder) else []
                if not folders:
                    continue
                key = (tuple(folders), obj_type, number)
                if key in processed_keys:
                    continue
                processed_keys.add(key)

                geojson_renamed = False
                fbx_renamed = False
                for folder in folders:
                    if not geojson_renamed:
                        geojson_renamed = self.rename_geojson_in_folder(
                            folder, new_address, number, obj_type, allowed)
                    if self.rename_fbx_in_folder(folder, new_address, number, obj_type, allowed):
                        fbx_renamed = True
                if geojson_renamed or fbx_renamed:
                    renamed_count += 1
            except Exception as e:
                agr_report(self, 'WARNING',
                           f"Ошибка переименования GEOJSON/FBX для {obj.name}: {e}")
        return renamed_count

    def get_texture_folder_from_material(self, obj):
        for mat_slot in obj.data.materials:
            if mat_slot and mat_slot.use_nodes:
                for node in mat_slot.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image:
                        if node.image.source == 'TILED' and node.image.filepath:
                            abs_path = bpy.path.abspath(node.image.filepath)
                            folder_path = os.path.dirname(abs_path)
                            return folder_path
        project_root = _agr_baker_get_project_root()
        if project_root and os.path.exists(project_root):
            return project_root
        return None

    def rename_geojson_in_folder(self, folder_path, new_address, number, obj_type, allowed=None):
        """Rename the delivery geojson inside ONE folder (no recursion).

        os.walk() over the project root used to rename the first geojson it met
        anywhere, backup folders of past deliveries included (RENAME-5)."""
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

            for filename in sorted(os.listdir(folder_path)):
                if obj_type == 'Main' and not number:
                    if filename.endswith('_Ground.geojson'):
                        continue
                match = re.match(pattern, filename)
                if not match:
                    continue
                old_address = match.group(1)
                if allowed is not None and old_address not in allowed:
                    continue
                old_path = os.path.join(folder_path, filename)
                new_path = os.path.join(folder_path, new_name)
                if old_path != new_path and os.path.exists(new_path):
                    agr_report(self, 'WARNING',
                               f"{new_name} уже существует — {filename} не переименован")
                    continue
                with open(old_path, 'r', encoding='utf-8-sig') as f:
                    geojson_data = json.load(f)
                self.update_glass_materials_in_geojson(geojson_data, old_address, new_address)
                with open(new_path, 'w', encoding='utf-8') as f:
                    json.dump(geojson_data, f, ensure_ascii=False, indent=2)
                if old_path != new_path and os.path.exists(old_path):
                    os.remove(old_path)
                return True
        except Exception as e:
            agr_report(self, 'WARNING', f"Ошибка переименования GEOJSON: {e}")
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

    def rename_fbx_in_folder(self, folder_path, new_address, number, obj_type, allowed=None):
        """Rename the delivery FBX files inside ONE folder (no recursion) —
        the recursive version renamed every match in every subfolder, archives
        of past deliveries included (RENAME-5)."""
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

            def _rename(filename, new_filename):
                old_path = os.path.join(folder_path, filename)
                new_path = os.path.join(folder_path, new_filename)
                if old_path == new_path:
                    return False
                if os.path.exists(new_path):
                    agr_report(self, 'WARNING',
                               f"{new_filename} уже существует — {filename} не переименован")
                    return False
                os.rename(old_path, new_path)
                return True

            for filename in sorted(os.listdir(folder_path)):
                if obj_type == 'Main' and not number:
                    if filename.endswith('_Ground.fbx') or filename.endswith('_Ground_Light.fbx'):
                        continue
                skip_main_match = obj_type == 'Main' and not number and filename.endswith('_Light.fbx')
                if not skip_main_match:
                    match = re.match(pattern_main, filename)
                    if match and (allowed is None or match.group(1) in allowed):
                        renamed = _rename(filename, new_main) or renamed
                        continue
                match = re.match(pattern_light, filename)
                if match and (allowed is None or match.group(1) in allowed):
                    renamed = _rename(filename, new_light) or renamed
        except Exception as e:
            agr_report(self, 'WARNING', f"Ошибка переименования FBX: {e}")
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
        """One implementation, shared with operators_rename (RENAME-10):
        names are claimed through temp names and collisions are reported."""
        renamed, _conflicts = rename_shared.rename_child_lights(
            self, root_obj, address, number, obj_type)
        return renamed


    def distribute_to_collections(self, context, new_address, lowpoly_number=None):
        if lowpoly_number:
            self._distribute_highpoly(context, new_address)
            self._distribute_lowpoly(context, new_address, lowpoly_number)
        else:
            self._distribute_highpoly(context, new_address)

    def _distribute_highpoly(self, context, address):
        collections_data = {}

        for obj in context.scene.objects:
            if _agr_baker_obj_in_lowpoly_collection(obj):
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

            # 'Point' kept for legacy scenes renamed before the Omni convention
            match = re.match(r'^' + re.escape(address) + r'_(\d{3})_(Spot|Omni|Point)_\d+$', obj_name)
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

            match = re.match(r'^' + re.escape(address) + r'_(Spot|Omni|Point)_\d+$', obj_name)
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

            match = re.match(r'^' + re.escape(address) + r'_Ground_(Spot|Omni|Point)_\d+$', obj_name)
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
                    self._note_emptied(old_coll)
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
            if not _agr_baker_obj_in_lowpoly_collection(obj):
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
                    self._note_emptied(old_coll)
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

    def _note_emptied(self, collection):
        """Remember a collection THIS operator took objects out of."""
        try:
            if not isinstance(self._emptied_collections, set):
                self._emptied_collections = set(self._emptied_collections)
            self._emptied_collections.add(collection.name)
        except Exception:
            pass

    def _remove_empty_collections(self, context):
        """Delete ONLY the collections this run emptied.

        The old sweep removed every empty collection in the file — a user's
        pre-made empty "Refs" went with it, and Ctrl+Z is not offered by this
        operator any more (RENAME-6)."""
        removed_count = 0
        for name in sorted(self._emptied_collections):
            collection = bpy.data.collections.get(name)
            if collection is None:
                continue
            if collection == context.scene.collection:
                continue
            if len(collection.objects) or len(collection.children):
                continue
            for parent in bpy.data.collections:
                if collection.name in parent.children:
                    parent.children.unlink(collection)
            if collection.name in context.scene.collection.children:
                context.scene.collection.children.unlink(collection)
            bpy.data.collections.remove(collection)
            removed_count += 1
        return removed_count

    def _lowpoly_folder_candidates(self, root_dir, lowpoly_number):
        r"""Directories NNNN_<something> next to the .blend that really hold a
        lowpoly delivery — proven by an FBX named NNNN_Addr_01.fbx /
        NNNN_Addr_Ground.fbx inside.

        The old scan took the FIRST `^\d{4}_` directory os.listdir returned and
        happily renamed `0001_Архив` into the project (RENAME-3)."""
        candidates = []
        try:
            entries = sorted(os.listdir(root_dir))
        except OSError as exc:
            agr_report(self, 'WARNING', f"Не удалось прочитать корень проекта: {exc}")
            return candidates

        for item in entries:
            item_path = os.path.join(root_dir, item)
            if not os.path.isdir(item_path):
                continue
            match = re.match(r'^(\d{4})_(.+)$', item)
            if not match:
                continue
            try:
                inner = os.listdir(item_path)
            except OSError:
                continue
            if not any(rename_shared.LOWPOLY_FBX_RE.match(f) for f in inner):
                continue
            candidates.append((match.group(1), item_path, item))
        return candidates

    def _rename_lowpoly_folder_and_fbx(self, lowpoly_number, new_address):
        root_dir = _agr_baker_get_project_root()
        if not root_dir:
            blend_path = bpy.data.filepath
            if not blend_path:
                return
            root_dir = os.path.dirname(blend_path)

        new_folder_name = f"{lowpoly_number}_{new_address}"
        new_folder_path = os.path.join(root_dir, new_folder_name)

        candidates = self._lowpoly_folder_candidates(root_dir, lowpoly_number)
        # A folder already carrying the target number is the obvious one
        numbered = [c for c in candidates if c[0] == lowpoly_number]
        if numbered:
            candidates = numbered

        if not candidates:
            os.makedirs(new_folder_path, exist_ok=True)
            return

        if len(candidates) > 1:
            names = ", ".join(c[2] for c in candidates)
            # WARNING, not ERROR: the rest of the rename already ran, and an
            # ERROR report makes bpy.ops raise for every script caller.
            agr_report(self, 'WARNING',
                       "Не удалось определить папку lowpoly — подходят несколько: "
                       f"{names}. Переименование папки и FBX ОТМЕНЕНО")
            return

        _number, old_folder, old_folder_name = candidates[0]

        for filename in sorted(os.listdir(old_folder)):
            match = rename_shared.LOWPOLY_FBX_RE.match(filename)
            if not match:
                continue
            new_fbx_name = f"{lowpoly_number}_{new_address}{match.group(3)}.fbx"
            old_fbx_path = os.path.join(old_folder, filename)
            new_fbx_path = os.path.join(old_folder, new_fbx_name)
            if old_fbx_path == new_fbx_path:
                continue
            if os.path.exists(new_fbx_path):
                agr_report(self, 'WARNING',
                           f"{new_fbx_name} уже существует — {filename} не переименован")
                continue
            try:
                os.rename(old_fbx_path, new_fbx_path)
            except Exception as exc:
                agr_report(self, 'WARNING', f"Не удалось переименовать {filename}: {exc}")

        if old_folder == new_folder_path:
            return
        if os.path.exists(new_folder_path):
            agr_report(self, 'WARNING',
                       f"Папка {new_folder_name} уже существует — {old_folder_name} не переименована")
            return
        try:
            os.rename(old_folder, new_folder_path)
        except Exception as exc:
            agr_report(self, 'WARNING',
                       f"Не удалось переименовать папку {old_folder_name}: {exc}")



# ============= Register =============

classes = (
    AGR_RP_OT_rename_project,
)


def register():
    register_scene_properties()
    
    for cls in classes:
        bpy.utils.register_class(cls)
    
    print("✅ Rename Project operators registered")


def unregister():
    unregister_classes(classes)  # idempotent: survives a half-registered module (R-glue-4)
    
    unregister_scene_properties()
    
    print("Rename Project operators unregistered")
