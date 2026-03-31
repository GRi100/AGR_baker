"""
GeoJSON management operators for AGR Baker v2.
Handles scanning, creating, editing, and enriching GeoJSON files
associated with SM_* folders next to the blend file.
"""

import bpy
import os
import re
import json
import base64
import shutil
import tempfile
from bpy.types import Operator, PropertyGroup, UIList
from bpy.props import (
    StringProperty, BoolProperty, FloatProperty,
    IntProperty, CollectionProperty, PointerProperty,
)


# ────────────────────────────────────────────
# PropertyGroups
# ────────────────────────────────────────────

class AGR_GeoJsonFolder(PropertyGroup):
    """Entry in the GeoJSON folder list, including per-file individual fields"""
    name: StringProperty(name="Folder Name", default="")
    folder_path: StringProperty(name="Folder Path", default="")
    has_geojson: BoolProperty(name="Has GeoJSON", default=False)
    geojson_filename: StringProperty(name="GeoJSON Filename", default="")
    is_ground: BoolProperty(name="Is Ground", default=False)
    label_suffix: StringProperty(name="Label Suffix", default="")

    # Individual fields per folder/file
    FNO_code: StringProperty(name="Код ФНО", default="")
    FNO_name: StringProperty(name="Название ФНО", default="")
    h_relief: FloatProperty(name="Высота рельефа", default=0.0, step=1, precision=6)


class AGR_GeoJsonProperties(PropertyGroup):
    """Shared fields written to all GeoJSON files"""
    address: StringProperty(name="Адрес", default="")
    okrug: StringProperty(name="Округ", default="")
    rajon: StringProperty(name="Район", default="")
    name: StringProperty(name="Название (Main)", default="")
    name_ground: StringProperty(name="Название (Ground)", default="")
    developer: StringProperty(name="Застройщик", default="")
    designer: StringProperty(name="Проектировщик", default="")
    cadNum: StringProperty(name="Кадастровый номер", default="")
    ZU_area: FloatProperty(name="Площадь ЗУ", default=0.0, step=1, precision=6)
    h_otn: FloatProperty(name="Высота отн.", default=0.0, step=1, precision=6)
    h_abs: FloatProperty(name="Высота абс.", default=0.0, step=1, precision=6)
    s_obsh: FloatProperty(name="Площадь общая", default=0.0, step=1, precision=6)
    s_naz: FloatProperty(name="Площадь наземная", default=0.0, step=1, precision=6)
    s_podz: FloatProperty(name="Площадь подземная", default=0.0, step=1, precision=6)
    spp_gns: FloatProperty(name="СПП ГНС", default=0.0, step=1, precision=6)
    act_AGR: StringProperty(name="Акт АГР", default="")
    other: StringProperty(name="Прочее", default="")


# ────────────────────────────────────────────
# UIList
# ────────────────────────────────────────────

class AGR_UL_GeoJsonFolderList(UIList):
    """UIList for GeoJSON folders"""
    bl_idname = "AGR_UL_geojson_folder_list"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            if item.has_geojson:
                row.label(text=item.name, icon='CHECKMARK')
            else:
                row.label(text=item.name, icon='ERROR')
            if item.is_ground:
                row.label(text="Ground", icon='MESH_PLANE')
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text=item.name, icon='FILE')


# ────────────────────────────────────────────
# Helper functions
# ────────────────────────────────────────────

def _get_blend_dir():
    """Return the directory containing the current blend file, or None."""
    blend_path = bpy.data.filepath
    if not blend_path:
        return None
    return os.path.dirname(blend_path)


def _find_geojson_in_folder(folder_path, folder_name):
    """Find a .geojson file in the given folder."""
    expected = folder_name + ".geojson"
    expected_path = os.path.join(folder_path, expected)
    if os.path.isfile(expected_path):
        return expected
    try:
        for f in os.listdir(folder_path):
            if f.lower().endswith('.geojson'):
                return f
    except OSError:
        pass
    return ""


def _is_ground_folder(folder_name):
    """Check if folder name ends with _Ground"""
    return folder_name.endswith("_Ground")


def _get_folder_suffix(folder_name):
    """Extract display suffix from folder name.
    SM_Address_001 -> '001'
    SM_Address_Ground -> 'Ground'
    SM_Address -> ''
    """
    if folder_name.endswith("_Ground"):
        return "Ground"
    match = re.search(r'_(\d{3})$', folder_name)
    if match:
        return match.group(1)
    return ""


def _get_template_path(is_ground):
    """Get path to the GeoJSON template file"""
    resources_dir = os.path.join(os.path.dirname(__file__), "resources")
    if is_ground:
        return os.path.join(resources_dir, "SM_Address_Ground.geojson")
    return os.path.join(resources_dir, "SM_Address.geojson")


def _load_geojson(filepath):
    """Load and parse a GeoJSON file"""
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)


def _save_geojson(filepath, data):
    """Save GeoJSON data to file"""
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _get_active_folder(context):
    """Get the currently selected folder item from the list"""
    scene = context.scene
    folders = scene.agr_geojson_folders
    idx = scene.agr_geojson_folders_index
    if 0 <= idx < len(folders):
        return folders[idx]
    return None


def _get_geojson_path(folder_item):
    """Get full path to the geojson file for a folder item"""
    if folder_item and folder_item.has_geojson and folder_item.geojson_filename:
        return os.path.join(folder_item.folder_path, folder_item.geojson_filename)
    return None


def _is_in_low_collection(obj):
    """Check if object is in a collection with 4-digit number prefix (LOW).
    These are lowpoly reference collections like '0903_ProezdPolesskiy...'
    """
    for coll in obj.users_collection:
        if re.match(r'^\d{4}_', coll.name):
            return True
    return False


def _linear_to_srgb(c):
    """Convert a single linear color channel to sRGB 0-255"""
    if c <= 0.0031308:
        srgb = c * 12.92
    else:
        srgb = 1.055 * (c ** (1.0 / 2.4)) - 0.055
    return max(0, min(255, int(round(srgb * 255))))


# Ground-specific fields that are written as empty strings
GROUND_EMPTY_FIELDS = {'h_otn', 'h_abs', 's_obsh', 's_naz', 's_podz', 'spp_gns', 'FNO_code'}

# Shared fields mapping: property name -> geojson key
SHARED_FIELDS = [
    'address', 'okrug', 'rajon', 'developer', 'designer',
    'cadNum', 'ZU_area', 'h_otn', 'h_abs', 's_obsh',
    's_naz', 's_podz', 'spp_gns', 'act_AGR', 'other',
]

# Float shared fields (need numeric handling)
SHARED_FLOAT_FIELDS = {
    'ZU_area', 'h_otn', 'h_abs', 's_obsh', 's_naz', 's_podz', 'spp_gns',
}


# ────────────────────────────────────────────
# Operators
# ────────────────────────────────────────────

class AGR_OT_load_all_geojson(Operator):
    """Scan SM_* folders, find GeoJSON files, and load all data"""
    bl_idname = "agr.load_all_geojson"
    bl_label = "Загрузить JSON"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        blend_dir = _get_blend_dir()
        if not blend_dir:
            self.report({'ERROR'}, "Сохраните blend файл перед загрузкой")
            return {'CANCELLED'}

        scene = context.scene
        folders = scene.agr_geojson_folders
        props = scene.agr_geojson_props
        folders.clear()

        # ── Step 1: Scan folders ──
        try:
            entries = sorted(os.listdir(blend_dir))
        except OSError as e:
            self.report({'ERROR'}, f"Ошибка чтения директории: {e}")
            return {'CANCELLED'}

        for entry in entries:
            full_path = os.path.join(blend_dir, entry)
            if not os.path.isdir(full_path):
                continue
            if not entry.startswith("SM_"):
                continue

            item = folders.add()
            item.name = entry
            item.folder_path = full_path
            item.is_ground = _is_ground_folder(entry)
            item.label_suffix = _get_folder_suffix(entry)

            geojson_name = _find_geojson_in_folder(full_path, entry)
            item.has_geojson = bool(geojson_name)
            item.geojson_filename = geojson_name

        scene.agr_geojson_folders_index = 0
        count = len(folders)
        print(f"📁 GeoJSON scan: found {count} SM_* folders in {blend_dir}")

        # ── Step 2: Load shared fields from first Main geojson ──
        source_item = None
        ground_item = None
        for folder in folders:
            if not folder.has_geojson:
                continue
            if folder.is_ground:
                if not ground_item:
                    ground_item = folder
            else:
                if not source_item:
                    source_item = folder

        load_item = source_item or ground_item
        if load_item:
            filepath = _get_geojson_path(load_item)
            if filepath:
                try:
                    data = _load_geojson(filepath)
                    feature_props = data.get('features', [{}])[0].get('properties', {})

                    for field in SHARED_FIELDS:
                        val = feature_props.get(field, "")
                        if field in SHARED_FLOAT_FIELDS:
                            if isinstance(val, str):
                                setattr(props, field, 0.0)
                            else:
                                setattr(props, field, float(val) if val else 0.0)
                        else:
                            setattr(props, field, str(val) if val is not None else "")

                    props.name = str(feature_props.get('name', ''))
                    print(f"📄 Loaded shared props from {filepath}")
                except (json.JSONDecodeError, OSError) as e:
                    print(f"⚠️ Error loading shared props: {e}")

        # Load Ground name separately
        if ground_item:
            ground_path = _get_geojson_path(ground_item)
            if ground_path:
                try:
                    ground_data = _load_geojson(ground_path)
                    ground_fprops = ground_data.get('features', [{}])[0].get('properties', {})
                    props.name_ground = str(ground_fprops.get('name', ''))
                except (json.JSONDecodeError, OSError):
                    pass

        # ── Step 3: Load individual fields per folder ──
        for folder in folders:
            if not folder.has_geojson:
                continue
            filepath = _get_geojson_path(folder)
            if not filepath:
                continue
            try:
                data = _load_geojson(filepath)
                feature_props = data.get('features', [{}])[0].get('properties', {})

                folder.FNO_code = str(feature_props.get('FNO_code', ''))
                folder.FNO_name = str(feature_props.get('FNO_name', ''))

                h_relief = feature_props.get('h_relief', 0)
                folder.h_relief = float(h_relief) if not isinstance(h_relief, str) or h_relief else 0.0

                print(f"📄 Loaded individual props from {folder.name}")
            except (json.JSONDecodeError, OSError) as e:
                print(f"⚠️ Error loading individual props for {folder.name}: {e}")

        has_geojson = sum(1 for f in folders if f.has_geojson)
        self.report({'INFO'}, f"Найдено {count} папок, загружено {has_geojson} JSON")
        return {'FINISHED'}


class AGR_OT_save_all_geojson(Operator):
    """Save shared + individual properties to ALL GeoJSON files"""
    bl_idname = "agr.save_all_geojson"
    bl_label = "Сохранить JSON"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        folders = context.scene.agr_geojson_folders
        return any(f.has_geojson for f in folders)

    def execute(self, context):
        scene = context.scene
        props = scene.agr_geojson_props
        saved_count = 0

        for folder in scene.agr_geojson_folders:
            if not folder.has_geojson:
                continue

            filepath = _get_geojson_path(folder)
            if not filepath:
                continue

            try:
                data = _load_geojson(filepath)
            except (json.JSONDecodeError, OSError) as e:
                print(f"⚠️ Error reading {filepath}: {e}")
                continue

            feature_props = data['features'][0]['properties']
            is_ground = folder.is_ground

            # Write shared fields
            for field in SHARED_FIELDS:
                if is_ground and field in GROUND_EMPTY_FIELDS:
                    feature_props[field] = ""
                    continue

                val = getattr(props, field)
                if field in SHARED_FLOAT_FIELDS:
                    feature_props[field] = val
                else:
                    feature_props[field] = val

            # Write name field
            if is_ground:
                feature_props['name'] = props.name_ground
            else:
                feature_props['name'] = props.name

            # Write individual fields from folder item
            if is_ground:
                feature_props['FNO_code'] = ""
            else:
                feature_props['FNO_code'] = folder.FNO_code

            feature_props['FNO_name'] = folder.FNO_name
            feature_props['h_relief'] = folder.h_relief

            try:
                _save_geojson(filepath, data)
                saved_count += 1
            except OSError as e:
                print(f"⚠️ Error writing {filepath}: {e}")

        self.report({'INFO'}, f"Сохранено {saved_count} файлов JSON")
        print(f"💾 Saved all props to {saved_count} GeoJSON files")
        return {'FINISHED'}


class AGR_OT_create_geojson(Operator):
    """Create GeoJSON from template for the selected folder"""
    bl_idname = "agr.create_geojson"
    bl_label = "Создать GeoJSON"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        folder = _get_active_folder(context)
        return folder is not None and not folder.has_geojson

    def execute(self, context):
        folder = _get_active_folder(context)
        if not folder:
            self.report({'ERROR'}, "Папка не выбрана")
            return {'CANCELLED'}

        template_path = _get_template_path(folder.is_ground)
        if not os.path.isfile(template_path):
            self.report({'ERROR'}, f"Шаблон не найден: {template_path}")
            return {'CANCELLED'}

        target_filename = folder.name + ".geojson"
        target_path = os.path.join(folder.folder_path, target_filename)

        try:
            shutil.copy2(template_path, target_path)
        except OSError as e:
            self.report({'ERROR'}, f"Ошибка копирования: {e}")
            return {'CANCELLED'}

        folder.has_geojson = True
        folder.geojson_filename = target_filename

        self.report({'INFO'}, f"Создан {target_filename}")
        print(f"📄 Created GeoJSON: {target_path}")
        return {'FINISHED'}


class AGR_OT_create_all_geojson(Operator):
    """Create GeoJSON for all folders that don't have one"""
    bl_idname = "agr.create_all_geojson"
    bl_label = "Создать для всех"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        folders = context.scene.agr_geojson_folders
        return any(not f.has_geojson for f in folders)

    def execute(self, context):
        created = 0
        for folder in context.scene.agr_geojson_folders:
            if folder.has_geojson:
                continue

            template_path = _get_template_path(folder.is_ground)
            if not os.path.isfile(template_path):
                continue

            target_filename = folder.name + ".geojson"
            target_path = os.path.join(folder.folder_path, target_filename)

            try:
                shutil.copy2(template_path, target_path)
                folder.has_geojson = True
                folder.geojson_filename = target_filename
                created += 1
            except OSError as e:
                print(f"⚠️ Error creating {target_path}: {e}")

        self.report({'INFO'}, f"Создано {created} файлов GeoJSON")
        print(f"📄 Created {created} GeoJSON files")
        return {'FINISHED'}


class AGR_OT_add_glass_to_geojson(Operator):
    """Scan Glass objects and write material data to GeoJSON Glasses array"""
    bl_idname = "agr.add_glass_to_geojson"
    bl_label = "Добавить стёкла"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        folders = context.scene.agr_geojson_folders
        return any(f.has_geojson for f in folders)

    def execute(self, context):
        scene = context.scene
        folders = scene.agr_geojson_folders

        # Build a mapping: folder_name -> geojson filepath
        folder_map = {}
        for folder in folders:
            if folder.has_geojson:
                filepath = _get_geojson_path(folder)
                if filepath:
                    folder_map[folder.name] = {
                        'path': filepath,
                        'is_ground': folder.is_ground,
                        'glasses': [],
                    }

        if not folder_map:
            self.report({'WARNING'}, "Нет GeoJSON файлов для записи стёкол")
            return {'CANCELLED'}

        glass_pattern = re.compile(
            r'^SM_(.+?)_(MainGlass|GroundGlass)(?:_(\d+))?(?:\.\d{3})?$'
        )

        total_glasses = 0

        for obj in bpy.data.objects:
            if obj.type != 'MESH':
                continue

            # Skip objects in LOW collections (4-digit prefix)
            if _is_in_low_collection(obj):
                continue

            match = glass_pattern.match(obj.name)
            if not match:
                continue

            base_address = match.group(1)
            glass_type = match.group(2)

            # Determine which folder this glass belongs to
            target_folder_name = None
            if glass_type == "GroundGlass":
                candidate = f"SM_{base_address}"
                if candidate in folder_map:
                    target_folder_name = candidate
                else:
                    addr_no_ground = re.sub(r'_Ground$', '', base_address)
                    candidate = f"SM_{addr_no_ground}_Ground"
                    if candidate in folder_map:
                        target_folder_name = candidate
            else:
                candidate = f"SM_{base_address}"
                if candidate in folder_map:
                    target_folder_name = candidate

            if not target_folder_name:
                print(f"⚠️ No matching folder for glass object: {obj.name}")
                continue

            # Extract material properties from each material slot
            for slot in obj.material_slots:
                mat = slot.material
                if not mat or not mat.use_nodes:
                    continue

                bsdf = None
                for node in mat.node_tree.nodes:
                    if node.type == 'BSDF_PRINCIPLED':
                        bsdf = node
                        break

                if not bsdf:
                    continue

                base_color = bsdf.inputs['Base Color'].default_value
                alpha = bsdf.inputs['Alpha'].default_value
                ior = bsdf.inputs['IOR'].default_value
                roughness = bsdf.inputs['Roughness'].default_value
                metallic = bsdf.inputs['Metallic'].default_value

                r = _linear_to_srgb(base_color[0])
                g = _linear_to_srgb(base_color[1])
                b = _linear_to_srgb(base_color[2])

                mat_key = mat.name

                glass_entry = {
                    mat_key: {
                        "color_RGB": {
                            "Red": r,
                            "Green": g,
                            "Blue": b,
                        },
                        "transparency": round(1.0 - alpha, 3),
                        "refraction": round(ior, 3),
                        "roughness": round(roughness, 3),
                        "metallicity": round(metallic, 3),
                    }
                }

                folder_map[target_folder_name]['glasses'].append(glass_entry)
                total_glasses += 1
                print(f"🪟 Glass: {mat_key} -> {target_folder_name}")

        # Write glasses to GeoJSON files
        written = 0
        for folder_name, info in folder_map.items():
            if not info['glasses']:
                continue

            try:
                data = _load_geojson(info['path'])
                data['features'][0]['Glasses'] = info['glasses']
                _save_geojson(info['path'], data)
                written += 1
            except (json.JSONDecodeError, OSError) as e:
                print(f"⚠️ Error writing glasses to {info['path']}: {e}")

        self.report({'INFO'}, f"Добавлено {total_glasses} стёкол в {written} файлов")
        print(f"🪟 Added {total_glasses} glass entries to {written} GeoJSON files")
        return {'FINISHED'}


class AGR_OT_add_coords_to_geojson(Operator):
    """Write object world coordinates to GeoJSON geometry"""
    bl_idname = "agr.add_coords_to_geojson"
    bl_label = "Добавить координаты"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        folders = context.scene.agr_geojson_folders
        return any(f.has_geojson for f in folders)

    def execute(self, context):
        scene = context.scene
        folders = scene.agr_geojson_folders

        folder_map = {}
        for folder in folders:
            if folder.has_geojson:
                filepath = _get_geojson_path(folder)
                if filepath:
                    folder_map[folder.name] = filepath

        obj_pattern = re.compile(
            r'^SM_(.+?)_(Main|Ground)(?:\.\d{3})?$'
        )

        updated = 0
        for obj in bpy.data.objects:
            if obj.type != 'MESH':
                continue

            # Skip objects in LOW collections (4-digit prefix)
            if _is_in_low_collection(obj):
                continue

            match = obj_pattern.match(obj.name)
            if not match:
                continue

            address = match.group(1)
            obj_type = match.group(2)

            if obj_type == "Ground":
                folder_name = f"SM_{address}_Ground"
                if folder_name not in folder_map:
                    folder_name = f"SM_{address}"
            else:
                folder_name = f"SM_{address}"

            if folder_name not in folder_map:
                print(f"⚠️ No matching folder for object: {obj.name} (tried {folder_name})")
                continue

            filepath = folder_map[folder_name]

            loc = obj.matrix_world.translation
            coords = [round(loc.x, 3), round(loc.y, 3)]

            try:
                data = _load_geojson(filepath)
                data['features'][0]['geometry']['coordinates'] = coords
                _save_geojson(filepath, data)
                updated += 1
                print(f"📍 Coords {coords} -> {folder_name}")
            except (json.JSONDecodeError, OSError, KeyError) as e:
                print(f"⚠️ Error writing coords to {filepath}: {e}")

        self.report({'INFO'}, f"Координаты записаны в {updated} файлов")
        print(f"📍 Updated coordinates in {updated} GeoJSON files")
        return {'FINISHED'}


class AGR_OT_add_image_to_geojson(Operator):
    """Select a JPG file, resize to 256px, encode as base64, write to all GeoJSON"""
    bl_idname = "agr.add_image_to_geojson"
    bl_label = "Добавить изображение"
    bl_options = {'REGISTER', 'UNDO'}

    filepath: StringProperty(
        name="Image File",
        subtype='FILE_PATH',
    )

    filter_glob: StringProperty(
        default="*.jpg;*.jpeg;*.png",
        options={'HIDDEN'},
    )

    @classmethod
    def poll(cls, context):
        folders = context.scene.agr_geojson_folders
        return any(f.has_geojson for f in folders)

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if not self.filepath or not os.path.isfile(self.filepath):
            self.report({'ERROR'}, "Файл не выбран или не существует")
            return {'CANCELLED'}

        try:
            from PIL import Image
            has_pillow = True
        except ImportError:
            has_pillow = False

        tmp_path = None

        try:
            if has_pillow:
                img = Image.open(self.filepath)
                img = img.convert('RGB')
                img = img.resize((256, 256), Image.LANCZOS)

                tmp_fd, tmp_path = tempfile.mkstemp(suffix='.jpg')
                os.close(tmp_fd)
                img.save(tmp_path, 'JPEG', quality=85)
                img.close()

                with open(tmp_path, 'rb') as f:
                    image_bytes = f.read()
            else:
                img_name = "__agr_temp_image__"
                if img_name in bpy.data.images:
                    bpy.data.images.remove(bpy.data.images[img_name])

                bpy_img = bpy.data.images.load(self.filepath)
                bpy_img.name = img_name
                bpy_img.scale(256, 256)

                tmp_fd, tmp_path = tempfile.mkstemp(suffix='.jpg')
                os.close(tmp_fd)

                scene = context.scene
                old_format = scene.render.image_settings.file_format
                old_quality = scene.render.image_settings.quality
                scene.render.image_settings.file_format = 'JPEG'
                scene.render.image_settings.quality = 85

                bpy_img.save_render(tmp_path, scene=scene)

                scene.render.image_settings.file_format = old_format
                scene.render.image_settings.quality = old_quality
                bpy.data.images.remove(bpy_img)

                with open(tmp_path, 'rb') as f:
                    image_bytes = f.read()

            b64_string = base64.b64encode(image_bytes).decode('ascii')

            written = 0
            for folder in context.scene.agr_geojson_folders:
                if not folder.has_geojson:
                    continue

                filepath = _get_geojson_path(folder)
                if not filepath:
                    continue

                try:
                    data = _load_geojson(filepath)
                    data['features'][0]['properties']['imageBase64'] = b64_string
                    _save_geojson(filepath, data)
                    written += 1
                except (json.JSONDecodeError, OSError) as e:
                    print(f"⚠️ Error writing image to {filepath}: {e}")

            self.report({'INFO'}, f"Изображение записано в {written} файлов")
            print(f"🖼️ Image base64 written to {written} GeoJSON files")

        except Exception as e:
            self.report({'ERROR'}, f"Ошибка обработки изображения: {e}")
            return {'CANCELLED'}

        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

        return {'FINISHED'}


# ────────────────────────────────────────────
# Registration
# ────────────────────────────────────────────

classes = (
    AGR_GeoJsonFolder,
    AGR_GeoJsonProperties,
    AGR_UL_GeoJsonFolderList,
    AGR_OT_load_all_geojson,
    AGR_OT_save_all_geojson,
    AGR_OT_create_geojson,
    AGR_OT_create_all_geojson,
    AGR_OT_add_glass_to_geojson,
    AGR_OT_add_coords_to_geojson,
    AGR_OT_add_image_to_geojson,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.agr_geojson_folders = CollectionProperty(type=AGR_GeoJsonFolder)
    bpy.types.Scene.agr_geojson_folders_index = IntProperty(default=0)
    bpy.types.Scene.agr_geojson_props = PointerProperty(type=AGR_GeoJsonProperties)

    print("✅ GeoJSON operators registered")


def unregister():
    del bpy.types.Scene.agr_geojson_props
    del bpy.types.Scene.agr_geojson_folders_index
    del bpy.types.Scene.agr_geojson_folders

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    print("GeoJSON operators unregistered")
