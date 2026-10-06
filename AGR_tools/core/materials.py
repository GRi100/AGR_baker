"""
Material utilities for AGR Tools
"""

import bpy
import os
import re


_BSDF_DEFAULTS = {
    'Base Color': (1.0, 1.0, 1.0, 1.0),
    'Metallic': 0.0,
    'Roughness': 0.8,
    'IOR': 1.5,
    'Alpha': 1.0,
    'Emission Color': (1.0, 1.0, 1.0, 1.0),
    'Emission Strength': 0.0,
}


def capture_bsdf_values(material):
    """Capture Principled BSDF default_values before node cleanup.
    Returns dict or None if no BSDF found."""
    if not material.use_nodes or not material.node_tree:
        return None
    for node in material.node_tree.nodes:
        if node.type == 'BSDF_PRINCIPLED':
            values = {}
            for key in _BSDF_DEFAULTS:
                if key in node.inputs:
                    val = node.inputs[key].default_value
                    if hasattr(val, '__len__'):
                        values[key] = tuple(val)
                    else:
                        values[key] = float(val)
            return values
    return None


def connect_normal_map(nodes, links, tex_normal, bsdf, location):
    """Connect normal map (OpenGL only)"""
    normal_map = nodes.new(type='ShaderNodeNormalMap')
    normal_map.location = location
    links.new(tex_normal.outputs['Color'], normal_map.inputs['Color'])
    links.new(normal_map.outputs['Normal'], bsdf.inputs['Normal'])


def _resolved_path(path):
    """Absolute, case-normalised path for comparing image filepaths."""
    try:
        return os.path.normcase(os.path.abspath(bpy.path.abspath(path)))
    except Exception:
        return None


def _find_image_by_path(texture_path):
    """Return the datablock already pointing at texture_path, or None."""
    target = _resolved_path(texture_path)
    if not target:
        return None
    for img in bpy.data.images:
        # Only FILE-backed datablocks may be reused: reload() on a GENERATED
        # image frees its buffers and regenerates a BLANK one, and the caller
        # then wires that black texture into the material under a green report.
        # Packed images keep source == 'FILE' (reload re-packs from disk, which
        # is exactly what a re-baked set should do); TILED (UDIM) images keep a
        # <UDIM> token that never resolves to a concrete file.
        if img.source != 'FILE' or not img.filepath_raw:
            continue
        if _resolved_path(img.filepath_raw) == target:
            return img
    return None


def _drop_unused_namesakes(texture_name, keep):
    """Free the canonical image name from LEFTOVER datablocks only.

    Deleting every namesake (the old behaviour) blanked the texture nodes of
    EVERY other material pointing at the same image — twin materials from
    duplication/Append/AGR Share went to export without textures. A datablock
    with users belongs to somebody and is never removed here.
    """
    dup_pattern = re.compile(re.escape(texture_name) + r'(\.\d{3})?')
    for img in list(bpy.data.images):
        if img is keep or not dup_pattern.fullmatch(img.name):
            continue
        if img.users == 0:
            bpy.data.images.remove(img)


def _image_has_pixels(image):
    """A truncated/zero-byte PNG loads WITHOUT raising and reports size (0, 0);
    such a datablock must never be treated as a usable texture."""
    return bool(image) and image.size[0] > 0 and image.size[1] > 0


def acquire_image(texture_path, texture_name, colorspace='sRGB'):
    """Load or reuse the image datablock for texture_path.

    Returns (image, created) — (None, False) when the file is missing or its
    pixels cannot be read. An existing datablock for the SAME resolved path is
    reused and reloaded instead of being deleted by name: shared sets keep
    working for every material that references them, and reload() is exactly
    what a re-baked set should do to all of its users.
    """
    if not os.path.exists(texture_path):
        print(f"⚠️ Texture not found: {texture_path}")
        return None, False

    existing = _find_image_by_path(texture_path)
    if existing is not None:
        try:
            existing.colorspace_settings.name = colorspace
            existing.reload()
            existing.update()
        except Exception as e:
            print(f"❌ Error reloading texture {texture_name}: {e}")
            return None, False
        if not _image_has_pixels(existing):
            print(f"❌ Texture has no pixel data: {texture_path}")
            return None, False
        # reload() can flip the source (a datablock whose file vanished and
        # came back, a script re-generating it): only a FILE image is the
        # texture on disk we were asked for
        if existing.source != 'FILE':
            print(f"❌ Datablock stopped being a file image: {texture_path}")
            return None, False
        return existing, False

    try:
        img = bpy.data.images.load(texture_path)
    except Exception as e:
        print(f"❌ Error loading texture {texture_name}: {e}")
        return None, False

    try:
        img.filepath = texture_path
        img.filepath_raw = texture_path
        img.colorspace_settings.name = colorspace
        img.reload()
        img.update()
    except Exception as e:
        print(f"❌ Error preparing texture {texture_name}: {e}")
        bpy.data.images.remove(img)
        return None, False

    if not _image_has_pixels(img):
        print(f"❌ Texture has no pixel data: {texture_path}")
        bpy.data.images.remove(img)
        return None, False

    _drop_unused_namesakes(texture_name, img)
    img.name = texture_name
    return img, True


def make_texture_node(nodes, image, label, location):
    """Create a TEX_IMAGE node for an already loaded image."""
    tex_node = nodes.new(type='ShaderNodeTexImage')
    tex_node.image = image
    tex_node.location = location
    tex_node.label = label
    return tex_node


def load_texture_from_disk(nodes, texture_path, texture_name, label, location, colorspace='sRGB'):
    """Load texture from disk and create image texture node"""
    img, _created = acquire_image(texture_path, texture_name, colorspace)
    if img is None:
        return None
    return make_texture_node(nodes, img, label, location)


def _preload(texture_path, texture_name, colorspace, created):
    """acquire_image + bookkeeping of datablocks WE created (see _discard)."""
    img, was_created = acquire_image(texture_path, texture_name, colorspace)
    if was_created and img is not None:
        created.append(img)
    return img


def _discard(created):
    """Drop datablocks created by a preload that ended up unused.
    A failed connect must not leave (0, 0) leftovers in bpy.data."""
    for img in created:
        try:
            if img.users == 0:
                bpy.data.images.remove(img)
        except Exception:
            pass


def _setup_material_nodes(material):
    """Clear material nodes and create base BSDF setup.
    Returns (nodes, links, bsdf, saved_bsdf_values)."""
    saved_values = capture_bsdf_values(material)

    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links

    nodes.clear()

    output = nodes.new(type='ShaderNodeOutputMaterial')
    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')

    output.location = (400, 0)
    bsdf.location = (100, 0)

    links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])

    return nodes, links, bsdf, saved_values


def _finalize_material(material, bsdf, saved_values=None):
    """Apply BSDF values (saved or default), material settings, and update viewport."""
    # blend_method is deprecated since 4.2 — guard for 5.x where it may be gone
    if hasattr(material, 'blend_method'):
        material.blend_method = 'HASHED'
    material.use_backface_culling = False

    for key, default_val in _BSDF_DEFAULTS.items():
        val = saved_values.get(key, default_val) if saved_values else default_val
        bsdf.inputs[key].default_value = val

    bpy.context.view_layer.update()
    material.node_tree.update_tag()


def validate_high_mode(texture_set_path, material_name):
    """Check if ALL HIGH mode textures exist. Returns list of missing texture names."""
    tex_types = ["DiffuseOpacity", "ERM", "Normal"]
    missing = [t for t in tex_types if not os.path.exists(os.path.join(texture_set_path, f"T_{material_name}_{t}.png"))]
    return missing


def validate_regular_mode(texture_set_path, material_name):
    """Check if at least one regular texture exists. Returns list of missing texture names.

    Emit is deliberately NOT required: it is connected when present, but a
    non-emissive set is a complete LOW set without it.
    """
    tex_types = ["Diffuse", "Roughness", "Metallic", "Opacity", "Normal"]
    missing = [t for t in tex_types if not os.path.exists(os.path.join(texture_set_path, f"T_{material_name}_{t}.png"))]
    return missing


def validate_all_high_mode(selected_sets):
    """Validate ALL sets have complete HIGH mode textures.
    Returns (is_valid, error_message). If is_valid=False, error_message lists missing textures."""
    errors = {}
    for tex_set in selected_sets:
        missing = validate_high_mode(tex_set.folder_path, tex_set.material_name)
        if missing:
            errors[tex_set.material_name] = missing

    if errors:
        names = ', '.join(f"{name} (no {', '.join(m)})" for name, m in errors.items())
        return False, f"Missing HIGH textures: {names}"
    return True, ""


def connect_texture_set_to_material(material, texture_set_path, material_name):
    """
    Connect texture set to material (HIGH mode: ERM + DiffuseOpacity).
    Returns None if required HIGH mode textures are missing or unreadable.
    """
    diffuse_opacity_path = os.path.join(texture_set_path, f"T_{material_name}_DiffuseOpacity.png")
    erm_path = os.path.join(texture_set_path, f"T_{material_name}_ERM.png")
    normal_path = os.path.join(texture_set_path, f"T_{material_name}_Normal.png")

    if not (os.path.exists(erm_path) and os.path.exists(diffuse_opacity_path)):
        print(f"❌ HIGH mode textures not found for {material_name} (need ERM + DiffuseOpacity)")
        return None

    print(f"🔧 Connecting in HIGH mode (ERM + DiffuseOpacity)")

    # Preload EVERYTHING before the graph is touched. _setup_material_nodes
    # clears the node tree by design, so a load failure after it (truncated PNG,
    # zero-byte file from a full disk, cloud-sync placeholder) left a blank white
    # material, a falsely successful report and dead (0,0) datablocks — and the
    # HIGH→regular fallback of connect_best never got its chance.
    created = []
    img_diffuse_opacity = _preload(diffuse_opacity_path, f"T_{material_name}_DiffuseOpacity", 'sRGB', created)
    img_erm = _preload(erm_path, f"T_{material_name}_ERM", 'Non-Color', created)
    # Normal is optional: a set without it still connects (matches the old
    # behaviour, where only ERM + DiffuseOpacity were required)
    img_normal = _preload(normal_path, f"T_{material_name}_Normal", 'Non-Color', created)

    if img_diffuse_opacity is None or img_erm is None:
        print(f"❌ HIGH mode textures unreadable for {material_name} — material left untouched")
        _discard(created)
        return None

    nodes, links, bsdf, saved_values = _setup_material_nodes(material)

    # DiffuseOpacity
    tex_diffuse_opacity = make_texture_node(
        nodes, img_diffuse_opacity, "Diffuse Opacity", (-700, 300))
    links.new(tex_diffuse_opacity.outputs['Color'], bsdf.inputs['Base Color'])
    links.new(tex_diffuse_opacity.outputs['Color'], bsdf.inputs['Emission Color'])
    links.new(tex_diffuse_opacity.outputs['Alpha'], bsdf.inputs['Alpha'])

    # Normal
    if img_normal is not None:
        tex_normal = make_texture_node(nodes, img_normal, "Normal", (-700, 0))
        connect_normal_map(nodes, links, tex_normal, bsdf, (-400, 0))

    # ERM
    tex_erm = make_texture_node(nodes, img_erm, "ERM", (-700, -300))
    separate_color = nodes.new(type='ShaderNodeSeparateColor')
    separate_color.location = (-400, -300)

    links.new(tex_erm.outputs['Color'], separate_color.inputs['Color'])
    links.new(separate_color.outputs['Red'], bsdf.inputs['Emission Strength'])
    links.new(separate_color.outputs['Green'], bsdf.inputs['Roughness'])
    links.new(separate_color.outputs['Blue'], bsdf.inputs['Metallic'])

    _finalize_material(material, bsdf, saved_values)

    print(f"✅ Texture set connected to material: {material.name}")
    return material


def connect_regular_texture_set_to_material(material, texture_set_path, material_name):
    """
    Connect regular (separate) textures to material.
    Uses individual Diffuse, Roughness, Metallic, Opacity, Emit, Normal files.
    Returns None if required regular textures are missing or unreadable.
    """
    # Validate BEFORE clearing nodes — ALL textures must exist
    missing = validate_regular_mode(texture_set_path, material_name)
    if missing:
        print(f"❌ Missing regular textures for {material_name}: {missing}")
        return None

    print(f"🔧 Connecting regular textures for {material_name}")

    # Same contract as HIGH mode: every image is read BEFORE nodes.clear()
    created = []

    def path_of(tex_type):
        return os.path.join(texture_set_path, f"T_{material_name}_{tex_type}.png")

    img_diffuse = _preload(path_of('Diffuse'), f"T_{material_name}_Diffuse", 'sRGB', created)
    img_metallic = _preload(path_of('Metallic'), f"T_{material_name}_Metallic", 'Non-Color', created)
    img_roughness = _preload(path_of('Roughness'), f"T_{material_name}_Roughness", 'Non-Color', created)
    img_opacity = _preload(path_of('Opacity'), f"T_{material_name}_Opacity", 'Non-Color', created)
    img_normal = _preload(path_of('Normal'), f"T_{material_name}_Normal", 'Non-Color', created)
    # Emit is optional — see validate_regular_mode
    emit_path = path_of('Emit')
    img_emit = (_preload(emit_path, f"T_{material_name}_Emit", 'Non-Color', created)
                if os.path.exists(emit_path) else None)

    required = (img_diffuse, img_metallic, img_roughness, img_opacity, img_normal)
    if any(img is None for img in required):
        print(f"❌ Regular textures unreadable for {material_name} — material left untouched")
        _discard(created)
        return None

    nodes, links, bsdf, saved_values = _setup_material_nodes(material)

    # Diffuse -> Base Color
    tex_diffuse = make_texture_node(nodes, img_diffuse, "Diffuse", (-700, 400))
    links.new(tex_diffuse.outputs['Color'], bsdf.inputs['Base Color'])

    # Metallic
    tex_metallic = make_texture_node(nodes, img_metallic, "Metallic", (-700, 200))
    links.new(tex_metallic.outputs['Color'], bsdf.inputs['Metallic'])

    # Roughness
    tex_roughness = make_texture_node(nodes, img_roughness, "Roughness", (-700, 0))
    links.new(tex_roughness.outputs['Color'], bsdf.inputs['Roughness'])

    # Opacity
    tex_opacity = make_texture_node(nodes, img_opacity, "Opacity", (-700, -200))
    links.new(tex_opacity.outputs['Color'], bsdf.inputs['Alpha'])

    # Emit: the baker writes T_*_Emit.png and HIGH mode drives emission from
    # ERM.R — LOW mode used to drop it, so emissive surfaces went dark
    if img_emit is not None:
        tex_emit = make_texture_node(nodes, img_emit, "Emit", (-700, -400))
        links.new(tex_emit.outputs['Color'], bsdf.inputs['Emission Strength'])
        links.new(tex_diffuse.outputs['Color'], bsdf.inputs['Emission Color'])

    # Normal
    tex_normal = make_texture_node(nodes, img_normal, "Normal", (-700, -600))
    connect_normal_map(nodes, links, tex_normal, bsdf, (-400, -600))

    _finalize_material(material, bsdf, saved_values)

    print(f"✅ Regular textures connected to material: {material.name}")
    return material


def connect_best_texture_set_to_material(material, texture_set_path, material_name):
    """Try HIGH mode first, fallback to regular (separate) textures."""
    result = connect_texture_set_to_material(material, texture_set_path, material_name)
    if result is None:
        result = connect_regular_texture_set_to_material(material, texture_set_path, material_name)
    return result
