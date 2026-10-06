# SPDX-License-Identifier: GPL-3.0-or-later
"""
Shared rename helpers for AGR Tools.

`operators_rename.py` (per-object buttons) and `operators_rename_project.py`
(the whole-project button) used to carry ~800 lines of verbatim copies of the
same texture pipeline.  Every fix landed in one copy only — the HIGH-material
graph destruction (do/erm/e never reconnected) lived in the project twin for
three releases after the per-object twin was fixed.  This module owns the ONE
implementation both operators call.

Nothing here touches the operator classes: functions take an optional
`operator` and report through `agr_report`, so both callers keep their own
wording for the summary line.
"""

import os
import re
import shutil

import bpy

from .log import agr_report


# ============= SM_ names =============

# Longest alternatives first so backtracking never settles on a short type
SM_NAME_RE = re.compile(
    r'^SM_(?P<address>.+?)(?:_(?P<number>\d{3}))?_'
    r'(?P<type>GroundElGlass|GroundGlass|MainGlass|GroundEl|Ground|Main|Flora)'
    r'(?:\.\d{3})?$'
)

# Which object types each rename operation accepts — the single source of
# truth shared by ui.py draw() and the operators (lists used to diverge
# between 6 regex copies in the panel)
RENAME_ALLOWED_TYPES = {
    'materials': {'Main', 'MainGlass', 'Ground', 'GroundGlass', 'GroundEl', 'GroundElGlass', 'Flora'},
    'glass_materials': {'MainGlass', 'GroundGlass', 'GroundElGlass'},
    'textures': {'Main', 'Ground', 'GroundEl', 'Flora'},
    'geojson': {'Main', 'Ground'},
}


def parse_sm_name(name):
    """Parse 'SM_Address[_NNN]_Type[.001]' → (address, number|None, obj_type),
    or None when the name does not follow the SM_ convention."""
    match = SM_NAME_RE.match(name)
    if not match:
        return None
    return match.group('address'), match.group('number'), match.group('type')


def strip_dup_suffix(name):
    """Blender's '.001' duplicate suffix."""
    return re.sub(r'\.\d{3}$', '', name)


# The lowpoly collection is named like the FBX it exports (see
# _distribute_lowpoly): "0109_BolshaiaPirogovskaia_ZU_51_1_Ground.fbx" — the
# 4-digit number AND the _Ground tail are BOTH required, that is what tells it
# apart from every other NNNN_* collection in the scene.
_LOWPOLY_COLL_RE = re.compile(r'^(\d{4})_(.+?)_Ground(?:\.[Ff][Bb][Xx])?$')

# FBX files that prove a folder really is the lowpoly delivery folder
LOWPOLY_FBX_RE = re.compile(r'^(\d{4})_(.+?)(_\d{2}|_Ground)\.fbx$', re.IGNORECASE)


def find_lowpoly_collections(scene):
    """Collections named NNNN_<address>_Ground[.fbx] in the scene tree, in
    tree order.  Returns [(number, address, collection_name)]; a collection
    linked in several places is visited once."""
    found = []
    seen = set()

    def walk(coll):
        if coll.as_pointer() in seen:
            return
        seen.add(coll.as_pointer())
        match = _LOWPOLY_COLL_RE.match(strip_dup_suffix(coll.name))
        if match:
            found.append((match.group(1), match.group(2), coll.name))
        for child in coll.children:
            walk(child)

    if scene is not None:
        for child in scene.collection.children:
            walk(child)
    return found


# ============= Texture type codes =============

# Regular (non-UDIM) lowpoly textures use short suffix codes — the SAME codes
# go into output filenames and into the reconnect keys (project convention:
# lowpoly = short codes, UDIM highpoly = full names).  Longer codes first, so
# 'erm' never matches as 'r' and 'do' never as 'd'/'o'.
TEXTURE_TYPE_CODES = ('erm', 'do', 'd', 'o', 'm', 'n', 'r', 'e')

# Only the three color maps are sRGB.  An UNKNOWN type must NOT default to
# sRGB: a data map silently gamma-decoded is a wrong bake, while a color map
# read as data is visible immediately.
_COLOR_SPACE = {
    'd': 'sRGB',
    'do': 'sRGB',
    'e': 'sRGB',
    'erm': 'Non-Color',
    'n': 'Non-Color',
    'o': 'Non-Color',
    'r': 'Non-Color',
    'm': 'Non-Color',
}


def get_texture_type_from_filename(filename):
    """Short texture code from a lowpoly texture filename, or None.
    Anchored at the end so an address part like Volkhonka_D_5 never matches."""
    for tex_type in TEXTURE_TYPE_CODES:
        if re.search(rf'_{tex_type}_\d+\.png$', filename) or filename.endswith(f'_{tex_type}.png'):
            return tex_type
    return None


def get_color_space_for_texture_type(tex_type):
    """Color space for a short texture code; unknown types stay Non-Color."""
    return _COLOR_SPACE.get(tex_type, 'Non-Color')


def texture_index_from_filename(filename):
    """Trailing material index of a lowpoly texture: T_..._d_2.png → 2.
    None when the file carries no index (legacy single-material sets)."""
    match = re.search(r'_(\d+)\.png$', filename)
    if match:
        return int(match.group(1))
    return None


def material_index_from_name(mat_name):
    """Index of a convention material name: M_Addr_Ground_2 → 2, else None.
    Blender's '.001' duplicate suffix is stripped first — a duplicated
    `M_Addr_Ground_1.001` still carries index 1, and the bare regex used to
    see no index at all and drop the material into the counter fallback."""
    match = re.search(r'_(\d+)$', strip_dup_suffix(mat_name or ""))
    if match:
        return int(match.group(1))
    return None


def material_is_udim(material):
    """True when the material drives a TILED (UDIM) image."""
    if material is None or not material.use_nodes or not material.node_tree:
        return False
    for node in material.node_tree.nodes:
        if node.type == 'TEX_IMAGE' and node.image and node.image.source == 'TILED':
            return True
    return False


def udim_aware_material_name(material, address, number, obj_type, idx):
    """Delivery name for one material slot, or None when the material must be
    left alone.

    A UDIM material is SHARED by every non-Main object of one address —
    Ground/GroundEl/Flora all collapse into the same SM_<addr>_Ground folder
    and, since the shared-record contract, into the same material datablock.
    Two rules follow:
      * the name always uses the Ground token (CLAUDE.md UDIM convention),
        otherwise the sibling stamps `M_<addr>_GroundEl_1` onto the Ground
        material and the delivery name check fails;
      * once the material already carries the canonical Ground name for this
        address it is NOT renamed again — GroundEl sorts after Ground and
        would otherwise overwrite the carrier's name with its own slot index.
    """
    if material is None:
        return None
    if re.match(r'^M_Glass_\d{2}$', material.name):
        return None
    if obj_type not in ('Main', 'MainGlass') and material_is_udim(material):
        if re.match(rf'^M_{re.escape(address)}_Ground_\d+$', material.name):
            return None
        type_token = 'Ground'
    else:
        type_token = obj_type
    if number:
        return f"M_{address}_{number}_{type_token}_{idx}"
    return f"M_{address}_{type_token}_{idx}"


def build_texture_filename(address, number, obj_type, tex_type, index):
    """Delivery filename for one lowpoly texture, or None for an unsupported
    object type."""
    if obj_type == 'Main' and number:
        return f"T_{address}_{number}_{obj_type}_{tex_type}_{index}.png"
    if obj_type == 'Main':
        return f"T_{address}_{obj_type}_{tex_type}_{index}.png"
    if obj_type == 'Ground':
        return f"T_{address}_Ground_{tex_type}_{index}.png"
    if obj_type.startswith('GroundE'):
        return f"T_{address}_{obj_type}_{tex_type}_{index}.png"
    if obj_type == 'Flora':
        return f"T_{address}_Flora_{tex_type}_{index}.png"
    return None


# ============= Reconnect =============

def load_texture(filepath, color_space='sRGB'):
    """Load an image datablock and stamp its color space; None on failure."""
    try:
        abs_path = os.path.abspath(filepath)
        new_image = bpy.data.images.load(abs_path, check_existing=True)
        new_image.colorspace_settings.name = color_space
        return new_image
    except Exception as exc:
        agr_report(None, 'WARNING', f"⚠️ Не удалось загрузить текстуру {filepath}: {exc}")
        return None


def _principled(nodes):
    for node in nodes:
        if node.type == 'BSDF_PRINCIPLED':
            return node
    return None


def reconnect_material_textures(mat, paths_by_type):
    """Swap `node.image` IN PLACE for every texture type present in
    `paths_by_type` ({short code: filepath}).

    Nodes are NEVER deleted: the project twin used to wipe every TEX_IMAGE and
    NORMAL_MAP node and rebuild only d/o/m/n/r, which unlinked do/erm/e and
    dropped image datablocks the operator had not even processed.

    Returns (loaded_images, applied_types)."""
    loaded = []
    applied = {}

    if not mat or not mat.use_nodes or not mat.node_tree:
        return loaded, applied

    nodes = mat.node_tree.nodes
    bsdf = _principled(nodes)
    if not bsdf:
        return loaded, applied

    def swap(node, tex_type):
        new_img = load_texture(paths_by_type[tex_type], get_color_space_for_texture_type(tex_type))
        if not new_img:
            return
        node.image = new_img
        loaded.append(new_img)
        applied[tex_type] = new_img

    # Diffuse
    if 'd' in paths_by_type and bsdf.inputs['Base Color'].is_linked:
        node = bsdf.inputs['Base Color'].links[0].from_node
        if node.type == 'TEX_IMAGE':
            swap(node, 'd')

    # DiffuseOpacity (LOW-atlas '_do' files). Regular low sets use separate
    # _d/_o maps, so this fires only when an atlas material actually
    # references a _do image — replace it wherever it is used (Base Color and
    # Alpha share the same TEX_IMAGE node), otherwise the renamed file would
    # be lost by cleanup.
    if 'do' in paths_by_type:
        for node in nodes:
            if node.type == 'TEX_IMAGE' and node.image:
                old_name = os.path.basename(node.image.filepath) if node.image.filepath else node.image.name
                if re.search(r'_do(_\d+)?(\.png)?(\.\d{3})?$', old_name):
                    swap(node, 'do')

    # Normal (through the Normal Map node)
    if 'n' in paths_by_type:
        for node in nodes:
            if node.type == 'NORMAL_MAP':
                if node.outputs['Normal'].is_linked and node.inputs['Color'].is_linked:
                    if any(link.to_node == bsdf for link in node.outputs['Normal'].links):
                        normal_node = node.inputs['Color'].links[0].from_node
                        if normal_node.type == 'TEX_IMAGE':
                            swap(normal_node, 'n')
                        break

    # ERM (through Separate Color / Separate RGB)
    if 'erm' in paths_by_type:
        for node in nodes:
            if node.type in ('SEPRGB', 'SEPARATE_COLOR'):
                is_connected = any(link.to_node == bsdf for output in node.outputs for link in output.links)
                if is_connected and node.inputs[0].is_linked:
                    erm_node = node.inputs[0].links[0].from_node
                    if erm_node.type == 'TEX_IMAGE':
                        swap(erm_node, 'erm')
                    break

    # Direct BSDF inputs
    for tex_type, socket in (('r', 'Roughness'), ('m', 'Metallic'),
                             ('e', 'Emission Color'), ('o', 'Alpha')):
        if tex_type in paths_by_type and bsdf.inputs[socket].is_linked:
            node = bsdf.inputs[socket].links[0].from_node
            if node.type == 'TEX_IMAGE':
                swap(node, tex_type)

    return loaded, applied


def reconnect_textures(obj, new_texture_paths):
    """Rebind every material of `obj`.

    `new_texture_paths` is keyed either by (material_name, tex_type) — the
    per-material form that keeps two materials of one object from sharing a
    single `_1` file — or by plain tex_type (legacy flat form applied to every
    material).

    Returns (loaded_images, applied) where `applied` maps the ORIGINAL key to
    the image that actually landed in the graph."""
    loaded = []
    applied = {}

    keyed_by_material = any(isinstance(k, tuple) for k in new_texture_paths)

    for mat in obj.data.materials:
        if not mat:
            continue
        if keyed_by_material:
            paths = {k[1]: v for k, v in new_texture_paths.items() if k[0] == mat.name}
        else:
            paths = dict(new_texture_paths)
        if not paths:
            continue
        mat_loaded, mat_applied = reconnect_material_textures(mat, paths)
        loaded.extend(mat_loaded)
        for tex_type, img in mat_applied.items():
            applied[(mat.name, tex_type) if keyed_by_material else tex_type] = img

    return loaded, applied


# ============= Regular (non-UDIM) texture pipeline =============

def collect_material_textures(obj):
    """[(material, image)] for every non-UDIM texture of the object, unique per
    (material, image).  The same datablock used by two materials is returned
    twice on purpose — each material gets its OWN renamed file."""
    out = []
    seen = set()
    for mat in obj.data.materials:
        if not mat or not mat.use_nodes or not mat.node_tree:
            continue
        for node in mat.node_tree.nodes:
            if node.type != 'TEX_IMAGE' or not node.image:
                continue
            img = node.image
            if img.source == 'TILED':
                continue
            if not (img.filepath or img.packed_file):
                continue
            key = (mat.name, img.name)
            if key in seen:
                continue
            seen.add(key)
            out.append((mat, img))
    return out


def _norm(path):
    return os.path.normcase(os.path.abspath(path))


def process_object_textures(operator, obj, target_folder, address, number, obj_type):
    """Copy/move every regular texture of `obj` into `target_folder` under the
    delivery name, rebind the materials in place and pack the results.

    Returns (renamed_count, warnings).  Warnings are also reported one by one —
    the caller only has to add its own summary line.

    Data-safety rules encoded here (RENAME-2/RENAME-4):
      * one file per (material, texture type), index from the source filename
        or the material name, so two materials never collide on `_1`;
      * an existing target file is NEVER overwritten;
      * the old datablock is removed only when it has no users left AND the
        replacement is confirmed (packed or present on disk);
      * a created file is deleted only when its image is really packed.
    """
    pairs = collect_material_textures(obj)
    if not pairs:
        return 0, []

    warnings = []

    def warn(msg):
        warnings.append(msg)
        agr_report(operator, 'WARNING', msg)

    new_paths = {}        # (mat_name, tex_type) -> new filepath
    sources = {}          # (mat_name, tex_type) -> old image datablock
    old_filepaths = {}    # (mat_name, tex_type) -> filepath before we touched it
    planned = {}          # normalised target path -> key
    allocated = {}        # tex_type -> set of indices already handed out
    # image pointer -> filepath BEFORE we touched it.  A datablock shared by
    # two materials is exported twice, and the second pass must be able to
    # restore the ORIGINAL path, not the temp one the first pass left behind.
    original_fp = {}
    renamed_count = 0

    for mat, img in pairs:
        filename = img.name if not img.filepath else os.path.basename(img.filepath)
        tex_type = get_texture_type_from_filename(filename)
        if not tex_type:
            continue

        key = (mat.name, tex_type)
        if key in new_paths:
            warn(f"У материала {mat.name} несколько текстур типа '{tex_type}' — "
                 f"{filename} пропущена")
            continue

        # RENAME-4 promises ONE file per (material, texture type).  The index
        # of the material NAME comes first — it IS the delivery bin number,
        # while a filename index only records where the texture came from and
        # routinely disagrees (a `M_..._2` fed by `T_Other_d_1.png`).  Any
        # index already handed out for this type is skipped: two materials
        # colliding on `_1` used to leave the second one unrenamed with a
        # "имя занято" warning.
        used = allocated.setdefault(tex_type, set())
        index = material_index_from_name(mat.name)
        if index is None:
            index = texture_index_from_filename(filename)
        if index is None or index in used:
            index = 1
            while index in used:
                index += 1
        used.add(index)

        new_filename = build_texture_filename(address, number, obj_type, tex_type, index)
        if not new_filename:
            continue
        new_filepath = os.path.join(target_folder, new_filename)

        if _norm(new_filepath) in planned:
            warn(f"Имя {new_filename} уже занято другой текстурой этого объекта — "
                 f"{filename} пропущена")
            continue
        if os.path.exists(new_filepath):
            warn(f"Файл {new_filename} уже существует — {filename} не перезаписана")
            continue

        is_packed = img.packed_file is not None
        try:
            old_fp = original_fp.setdefault(img.as_pointer(), img.filepath)
        except Exception:
            old_fp = img.filepath
        if is_packed:
            temp_path = os.path.join(target_folder, f"temp_{img.name}")
            try:
                img.filepath = temp_path
                img.save()
            except Exception as exc:
                img.filepath = old_fp
                warn(f"Не удалось выгрузить упакованную текстуру {img.name}: {exc}")
                continue
            # img.save() logs "Unable to pack file" to the console and returns
            # WITHOUT raising — the file on disk is the only honest check.
            if not os.path.exists(temp_path):
                img.filepath = old_fp
                warn(f"Упакованная текстура {img.name} не сохранилась на диск — пропущена")
                continue
            source_path = temp_path
        else:
            if not img.filepath:
                continue
            source_path = bpy.path.abspath(img.filepath)
            if not os.path.exists(source_path):
                warn(f"Файл текстуры не найден: {source_path}")
                continue

        try:
            if is_packed:
                shutil.move(source_path, new_filepath)
            else:
                shutil.copy2(source_path, new_filepath)
        except Exception as exc:
            if is_packed:
                img.filepath = old_fp
            warn(f"Не удалось подготовить {new_filename}: {exc}")
            continue

        new_paths[key] = new_filepath
        planned[_norm(new_filepath)] = key
        sources[key] = img
        old_filepaths[key] = old_fp
        renamed_count += 1

    if not new_paths:
        return 0, warnings

    loaded_images, applied = reconnect_textures(obj, new_paths)

    # Pack the new images.  A created file may be deleted ONLY when its image
    # really ended up inside the .blend — otherwise the file IS the data.
    packed_paths = set()
    for img in loaded_images:
        try:
            abs_path = bpy.path.abspath(img.filepath)
        except Exception:
            continue
        if not abs_path or not os.path.exists(abs_path):
            continue
        try:
            if not img.packed_file:
                img.pack()
        except Exception as exc:
            warn(f"Не удалось упаковать {os.path.basename(abs_path)}: {exc}")
            continue
        if img.packed_file:
            packed_paths.add(_norm(abs_path))
        else:
            warn(f"{os.path.basename(abs_path)} не упакована — файл оставлен на диске")

    # Retire the originals.  Anything the reconnect did not reach keeps BOTH
    # its datablock and its freshly written file: the alternative is the
    # RENAME-2 hole where a ColorRamp-wired or shared map silently vanished.
    unmatched = []
    # ONE datablock can back several materials (each of them now gets its own
    # renamed file), so the same image appears under several keys.  The first
    # key removes it; every later touch of that freed StructRNA — even
    # `img.name` inside a warning — raises ReferenceError, so identities are
    # captured up front and the removed ones are skipped.
    source_ptr = {}
    source_name = {}
    for key, img in sources.items():
        try:
            source_ptr[key] = img.as_pointer()
            source_name[key] = img.name
        except Exception:
            source_ptr[key] = None
            source_name[key] = "?"
    dead_sources = set()

    for key, img in sources.items():
        mat_name, tex_type = key
        ptr = source_ptr.get(key)
        if ptr is not None and ptr in dead_sources:
            continue
        img_name = source_name.get(key, "?")
        new_img = applied.get(key)
        if new_img is None:
            unmatched.append(f"{mat_name}:{tex_type}")
            try:
                img.filepath = old_filepaths.get(key, img.filepath)
            except Exception:
                pass
            continue

        try:
            new_abs = bpy.path.abspath(new_img.filepath)
        except Exception:
            new_abs = ""
        replacement_safe = bool(new_img.packed_file) or (new_abs and os.path.exists(new_abs))
        if not replacement_safe:
            warn(f"Новая текстура для {mat_name}:{tex_type} не подтверждена — "
                 f"старая {img_name} оставлена")
            continue

        try:
            if img.users == 0:
                bpy.data.images.remove(img)
                if ptr is not None:
                    dead_sources.add(ptr)
            else:
                warn(f"Текстура {img_name} используется ещё в {img.users} мест(ах) — не удалена")
        except Exception as exc:
            warn(f"Не удалось удалить старую текстуру {img_name}: {exc}")

    if unmatched:
        warn("Текстуры переименованы, но не подключены (нестандартная проводка "
             "или нет Principled BSDF): " + ", ".join(sorted(unmatched)))

    for path in new_paths.values():
        if _norm(path) not in packed_paths:
            continue
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    # rmdir succeeds only when the folder is empty — foreign user files and
    # the ones we deliberately kept stay put.
    try:
        os.rmdir(target_folder)
    except OSError:
        pass

    return renamed_count, warnings


# ============= UDIM =============

_UDIM_TILE_RE = re.compile(r'^T_.+?_(Diffuse|Normal|ERM|ORM)(?:_(\d+))?\.1001\.png$')

_UDIM_COLOR_SPACE = {'Diffuse': 'sRGB', 'Normal': 'Non-Color', 'ERM': 'Non-Color'}


def udim_texture_type(filename):
    """Full UDIM texture type from a filename ('ORM' normalised to 'ERM')."""
    if 'Diffuse' in filename:
        return 'Diffuse'
    if 'Normal' in filename:
        return 'Normal'
    if 'ERM' in filename or 'ORM' in filename:
        return 'ERM'
    return None


def scan_udim_folder(folder):
    """{(tex_type, material_index|None): filename} for the *.1001.png tiles."""
    table = {}
    try:
        names = os.listdir(folder)
    except OSError:
        return table
    for filename in names:
        match = _UDIM_TILE_RE.match(filename)
        if not match:
            continue
        tex_type = udim_texture_type(filename)
        if not tex_type:
            continue
        index = int(match.group(2)) if match.group(2) else None
        table[(tex_type, index)] = filename
    return table


def _load_udim_texture(folder, filename, node, color_space):
    try:
        base_name = filename.replace('.1001.png', '')
        udim_path = os.path.join(folder, f"{base_name}.<UDIM>.png")
        abs_path = os.path.abspath(udim_path)
        new_image = bpy.data.images.load(abs_path, check_existing=True)
        new_image.source = 'TILED'
        new_image.colorspace_settings.name = color_space
        node.image = new_image
        return True
    except Exception as exc:
        agr_report(None, 'WARNING', f"⚠️ Не удалось загрузить UDIM текстуру {filename}: {exc}")
        return False


def update_udim_material_paths(operator, obj, folder):
    """Point every material of `obj` at ITS OWN tile set inside `folder`.

    The old implementation built one {type: filename} dict from os.listdir and
    handed it to every material, so a two-material object showed the tiles of
    whichever file the OS listed last (RENAME-7)."""
    table = scan_udim_folder(folder)
    if not table:
        return 0

    if not hasattr(obj.data, 'materials'):
        return 0

    updated = 0
    for slot_index, mat in enumerate(obj.data.materials, 1):
        if not mat or not mat.use_nodes or not mat.node_tree:
            continue

        files = {t: fn for (t, num), fn in table.items() if num == slot_index}
        if not files:
            # Legacy sets without an index, and single-material objects whose
            # tiles carry someone else's number — accept only when the type is
            # unambiguous in the whole folder.
            for tex_type in ('Diffuse', 'Normal', 'ERM'):
                candidates = [fn for (t, _num), fn in table.items() if t == tex_type]
                if len(candidates) == 1:
                    files[tex_type] = candidates[0]
        if not files:
            agr_report(operator, 'WARNING',
                       f"Для материала {mat.name} не найдены UDIM-тайлы с индексом {slot_index}")
            continue

        nodes = mat.node_tree.nodes
        bsdf = _principled(nodes)
        if not bsdf:
            continue

        if 'Diffuse' in files and bsdf.inputs['Base Color'].is_linked:
            node = bsdf.inputs['Base Color'].links[0].from_node
            if node.type == 'TEX_IMAGE':
                if _load_udim_texture(folder, files['Diffuse'], node, _UDIM_COLOR_SPACE['Diffuse']):
                    updated += 1

        if 'ERM' in files:
            for node in nodes:
                if node.type in ('SEPRGB', 'SEPARATE_COLOR', 'SEPARATE_XYZ'):
                    is_connected = any(link.to_node == bsdf for output in node.outputs for link in output.links)
                    if is_connected and node.inputs[0].is_linked:
                        erm_node = node.inputs[0].links[0].from_node
                        if erm_node.type == 'TEX_IMAGE':
                            if _load_udim_texture(folder, files['ERM'], erm_node, _UDIM_COLOR_SPACE['ERM']):
                                updated += 1
                        break

        if 'Normal' in files:
            for node in nodes:
                if node.type == 'NORMAL_MAP':
                    if node.outputs['Normal'].is_linked and node.inputs['Color'].is_linked:
                        if any(link.to_node == bsdf for link in node.outputs['Normal'].links):
                            normal_node = node.inputs['Color'].links[0].from_node
                            if normal_node.type == 'TEX_IMAGE':
                                if _load_udim_texture(folder, files['Normal'], normal_node,
                                                      _UDIM_COLOR_SPACE['Normal']):
                                    updated += 1
                            break

    return updated


def get_new_udim_folder_name(address, number, obj_type):
    if obj_type == 'Main' and number:
        return f"SM_{address}_{number}"
    if obj_type == 'Main' and not number:
        return f"SM_{address}"
    if obj_type == 'Ground':
        return f"SM_{address}_Ground"
    return None


def rename_udim_textures(operator, folder_path, new_address, number, obj_type):
    """Rename the *.<UDIM>.png tiles inside `folder_path`.  Never overwrites an
    existing target."""
    renamed_count = 0
    try:
        names = os.listdir(folder_path)
    except OSError as exc:
        # WARNING, not ERROR: these helpers run in the middle of "Переименовать
        # ВЕСЬ ПРОЕКТ", and an ERROR report makes bpy.ops raise for every
        # script caller even though the operator itself keeps going.
        agr_report(operator, 'WARNING', f"Не удалось прочитать папку тайлов: {exc}")
        return 0

    for filename in names:
        if not filename.endswith('.png'):
            continue
        udim_match = re.search(r'\.(\d{4})\.png$', filename)
        if not udim_match:
            continue
        udim_number = udim_match.group(1)
        texture_type = udim_texture_type(filename)
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
            if any(tag in filename for tag in ('_Ground_', '_GroundEl', '_Flora_')):
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
        else:
            new_filename = f"T_{new_address}_Ground_{texture_type}_{material_num}.{udim_number}.png"

        old_path = os.path.join(folder_path, filename)
        new_path = os.path.join(folder_path, new_filename)
        if old_path == new_path:
            continue
        if os.path.exists(new_path):
            agr_report(operator, 'WARNING', f"Тайл {new_filename} уже существует — {filename} не тронут")
            continue
        try:
            os.rename(old_path, new_path)
            renamed_count += 1
        except Exception as exc:
            agr_report(operator, 'WARNING', f"Не удалось переименовать {filename}: {exc}")

    return renamed_count


def process_udim_textures(operator, obj, texture_folder, address, number, obj_type):
    """Rename tiles + folder and rebind every material.  Returns
    (renamed_count, folder_renamed)."""
    new_folder_name = get_new_udim_folder_name(address, number, obj_type)
    new_folder_path = None
    if new_folder_name:
        parent_folder = os.path.dirname(texture_folder)
        new_folder_path = os.path.join(parent_folder, new_folder_name)
        # Refuse BEFORE touching the tiles: renaming them first and then
        # failing on the folder leaves every material pointing at a path that
        # no longer exists (pink object, RENAME-7 scenario B).
        if new_folder_path != texture_folder and os.path.exists(new_folder_path):
            agr_report(operator, 'WARNING',
                       f"Папка {new_folder_name} уже существует — переименование UDIM отменено")
            return 0, False

    renamed_count = rename_udim_textures(operator, texture_folder, address, number, obj_type)
    if renamed_count == 0:
        return 0, False

    folder_renamed = False
    current_folder = texture_folder
    if new_folder_path and new_folder_path != texture_folder:
        try:
            os.rename(texture_folder, new_folder_path)
            current_folder = new_folder_path
            folder_renamed = True
        except Exception as exc:
            agr_report(operator, 'WARNING', f"Текстуры переименованы, но папка не переименована: {exc}")

    # ALWAYS rebind — after a failed folder rename the tiles inside the OLD
    # folder already carry the new names.
    update_udim_material_paths(operator, obj, current_folder)
    return renamed_count, folder_renamed


# ============= Lights =============

def rename_child_lights(operator, root_obj, address, number, obj_type):
    """Rename LIGHT children of a Root EMPTY using the project convention.

    Names are claimed through a temporary unique name first, exactly like
    rename_ucx_objects: without it a second unnumbered Root produced
    `Addr_Spot_001.001`, and the FBX distribution regexes (anchored `$`) drop
    such an object from the delivery collection silently.
    Returns (renamed, conflicts) — conflicts are names held by FOREIGN objects."""
    spot_counter = 1
    point_counter = 1
    renamed = 0
    conflicts = []

    light_objects = [child for child in root_obj.children if child.type == 'LIGHT']
    light_objects.sort(key=lambda x: x.name)

    targets = []
    for light_obj in light_objects:
        light_type = light_obj.data.type
        if light_type == 'SPOT':
            lighttype_name = 'Spot'
            counter = spot_counter
            spot_counter += 1
        elif light_type == 'POINT':
            # Project convention: point lights are named "Omni" (3ds Max style)
            lighttype_name = 'Omni'
            counter = point_counter
            point_counter += 1
        else:
            continue
        if obj_type == 'Ground':
            new_name = f"{address}_Ground_{lighttype_name}_{counter:03d}"
        elif obj_type == 'Main' and number:
            new_name = f"{address}_{number}_{lighttype_name}_{counter:03d}"
        else:
            new_name = f"{address}_{lighttype_name}_{counter:03d}"
        targets.append((light_obj, new_name))

    if not targets:
        return 0, conflicts

    ours = {obj for obj, _ in targets}
    original_names = {}
    # Park our own lights on unique temp names so a swap inside the group
    # (Spot_002 → Spot_001) does not collide with itself.
    for idx, (light_obj, _new_name) in enumerate(targets):
        original_names[idx] = light_obj.name
        light_obj.name = f"__agr_light_tmp_{id(root_obj)}_{idx}"

    for idx, (light_obj, new_name) in enumerate(targets):
        holder = bpy.data.objects.get(new_name)
        if holder is not None and holder not in ours:
            conflicts.append(new_name)
            light_obj.name = original_names[idx]
            continue
        light_obj.name = new_name
        if light_obj.name != new_name:
            # Blender appended .001 anyway — the name is taken by something we
            # cannot see (another scene, a library override).
            conflicts.append(new_name)
            light_obj.name = original_names[idx]
            continue
        renamed += 1

    if conflicts and operator is not None:
        agr_report(operator, 'WARNING',
                   "Имена источников света заняты другими объектами: " + ", ".join(conflicts))
    return renamed, conflicts


# ============= geojson / FBX on disk =============

def geojson_search_dirs(root, addresses, number, obj_type):
    """One-level directories that may hold the delivery geojson/FBX of one
    object: the project root plus the object's OWN SM_* folders.

    The twin used to os.walk() the WHOLE project root and renamed the first
    geojson it met anywhere — including copies inside backup folders of past
    deliveries (RENAME-5)."""
    if not root or not os.path.isdir(root):
        return []

    dirs = []
    expected = set()
    for address in addresses:
        if not address:
            continue
        if obj_type == 'Main' and number:
            expected.add(f"SM_{address}_{number}")
        expected.add(f"SM_{address}")
        if obj_type == 'Ground':
            expected.add(f"SM_{address}_Ground")

    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return [root]

    # The object's OWN folder wins over the project root — a root-level
    # SM_X.geojson left over from an earlier layout must not shadow it.
    for entry in entries:
        if entry not in expected:
            continue
        path = os.path.join(root, entry)
        if os.path.isdir(path):
            dirs.append(path)
    dirs.append(root)
    return dirs
