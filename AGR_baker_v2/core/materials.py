"""
Material utilities for AGR Baker v2
"""

import bpy
import os


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


def load_texture_from_disk(nodes, texture_path, texture_name, label, location, colorspace='sRGB'):
    """Load texture from disk and create image texture node"""
    if not os.path.exists(texture_path):
        print(f"⚠️ Texture not found: {texture_path}")
        return None

    try:
        # Remove existing images with same name
        if texture_name in bpy.data.images:
            bpy.data.images.remove(bpy.data.images[texture_name])

        for img_name in list(bpy.data.images.keys()):
            if texture_name in img_name:
                bpy.data.images.remove(bpy.data.images[img_name])

        # Load image
        img = bpy.data.images.load(texture_path)
        img.name = texture_name
        img.filepath = texture_path
        img.filepath_raw = texture_path
        img.colorspace_settings.name = colorspace
        img.reload()
        img.update()

        # Create texture node
        tex_node = nodes.new(type='ShaderNodeTexImage')
        tex_node.image = img
        tex_node.location = location
        tex_node.label = label

        return tex_node

    except Exception as e:
        print(f"❌ Error loading texture {label}: {e}")
        return None


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
    """Check if at least one regular texture exists. Returns list of missing texture names."""
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
    Returns None if required HIGH mode textures are missing.
    """
    diffuse_opacity_path = os.path.join(texture_set_path, f"T_{material_name}_DiffuseOpacity.png")
    erm_path = os.path.join(texture_set_path, f"T_{material_name}_ERM.png")
    normal_path = os.path.join(texture_set_path, f"T_{material_name}_Normal.png")

    if not (os.path.exists(erm_path) and os.path.exists(diffuse_opacity_path)):
        print(f"❌ HIGH mode textures not found for {material_name} (need ERM + DiffuseOpacity)")
        return None

    print(f"🔧 Connecting in HIGH mode (ERM + DiffuseOpacity)")

    nodes, links, bsdf, saved_values = _setup_material_nodes(material)

    # DiffuseOpacity
    tex_diffuse_opacity = load_texture_from_disk(
        nodes, diffuse_opacity_path,
        f"T_{material_name}_DiffuseOpacity",
        "Diffuse Opacity", (-700, 300), 'sRGB'
    )
    if tex_diffuse_opacity:
        links.new(tex_diffuse_opacity.outputs['Color'], bsdf.inputs['Base Color'])
        links.new(tex_diffuse_opacity.outputs['Color'], bsdf.inputs['Emission Color'])
        links.new(tex_diffuse_opacity.outputs['Alpha'], bsdf.inputs['Alpha'])

    # Normal
    tex_normal = load_texture_from_disk(
        nodes, normal_path,
        f"T_{material_name}_Normal",
        "Normal", (-700, 0), 'Non-Color'
    )
    if tex_normal:
        connect_normal_map(nodes, links, tex_normal, bsdf, (-400, 0))

    # ERM
    tex_erm = load_texture_from_disk(
        nodes, erm_path,
        f"T_{material_name}_ERM",
        "ERM", (-700, -300), 'Non-Color'
    )
    if tex_erm:
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
    Uses individual Diffuse, Roughness, Metallic, Opacity, Normal files.
    Returns None if no regular textures found.
    """
    # Validate BEFORE clearing nodes — ALL textures must exist
    missing = validate_regular_mode(texture_set_path, material_name)
    if missing:
        print(f"❌ Missing regular textures for {material_name}: {missing}")
        return None

    print(f"🔧 Connecting regular textures for {material_name}")

    nodes, links, bsdf, saved_values = _setup_material_nodes(material)

    # Diffuse -> Base Color only (no Emission Color)
    tex_diffuse = load_texture_from_disk(
        nodes, os.path.join(texture_set_path, f"T_{material_name}_Diffuse.png"),
        f"T_{material_name}_Diffuse",
        "Diffuse", (-700, 400), 'sRGB'
    )
    if tex_diffuse:
        links.new(tex_diffuse.outputs['Color'], bsdf.inputs['Base Color'])

    # Metallic
    tex_metallic = load_texture_from_disk(
        nodes, os.path.join(texture_set_path, f"T_{material_name}_Metallic.png"),
        f"T_{material_name}_Metallic",
        "Metallic", (-700, 200), 'Non-Color'
    )
    if tex_metallic:
        links.new(tex_metallic.outputs['Color'], bsdf.inputs['Metallic'])

    # Roughness
    tex_roughness = load_texture_from_disk(
        nodes, os.path.join(texture_set_path, f"T_{material_name}_Roughness.png"),
        f"T_{material_name}_Roughness",
        "Roughness", (-700, 0), 'Non-Color'
    )
    if tex_roughness:
        links.new(tex_roughness.outputs['Color'], bsdf.inputs['Roughness'])

    # Opacity
    tex_opacity = load_texture_from_disk(
        nodes, os.path.join(texture_set_path, f"T_{material_name}_Opacity.png"),
        f"T_{material_name}_Opacity",
        "Opacity", (-700, -200), 'Non-Color'
    )
    if tex_opacity:
        links.new(tex_opacity.outputs['Color'], bsdf.inputs['Alpha'])

    # Normal
    tex_normal = load_texture_from_disk(
        nodes, os.path.join(texture_set_path, f"T_{material_name}_Normal.png"),
        f"T_{material_name}_Normal",
        "Normal", (-700, -600), 'Non-Color'
    )
    if tex_normal:
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
