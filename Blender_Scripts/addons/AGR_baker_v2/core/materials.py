"""
Material utilities for AGR Baker v2
"""

import bpy
import os


def connect_normal_map(nodes, links, tex_normal, bsdf, normal_type, location):
    """Connect normal map with OpenGL/DirectX support"""
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


def connect_texture_set_to_material(material, texture_set_path, material_name, normal_type='OPENGL'):
    """
    Connect texture set to material
    
    Args:
        material: Blender material
        texture_set_path: Path to texture set folder (S_material_name)
        material_name: Material name for texture naming
        normal_type: 'OPENGL' or 'DIRECTX'
    """
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    
    # Clear existing nodes
    nodes.clear()
    
    # Create output and BSDF
    output = nodes.new(type='ShaderNodeOutputMaterial')
    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    
    output.location = (400, 0)
    bsdf.location = (100, 0)
    
    links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
    
    # Check which textures exist
    diffuse_opacity_path = os.path.join(texture_set_path, f"T_{material_name}_DiffuseOpacity.png")
    diffuse_path = os.path.join(texture_set_path, f"T_{material_name}_Diffuse.png")
    erm_path = os.path.join(texture_set_path, f"T_{material_name}_ERM.png")
    normal_path = os.path.join(texture_set_path, f"T_{material_name}_Normal.png")
    roughness_path = os.path.join(texture_set_path, f"T_{material_name}_Roughness.png")
    metallic_path = os.path.join(texture_set_path, f"T_{material_name}_Metallic.png")
    opacity_path = os.path.join(texture_set_path, f"T_{material_name}_Opacity.png")
    
    has_erm = os.path.exists(erm_path)
    has_diffuse_opacity = os.path.exists(diffuse_opacity_path)
    
    # HIGH mode: ERM + DiffuseOpacity
    if has_erm and has_diffuse_opacity:
        print(f"🔧 Connecting in HIGH mode (ERM + DiffuseOpacity)")
        
        # Diffuse + Opacity
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
            connect_normal_map(nodes, links, tex_normal, bsdf, normal_type, (-400, 0))
        
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
    
    # LOW mode: separate textures
    else:
        print(f"🔧 Connecting in LOW mode (separate textures)")
        
        # Diffuse
        if os.path.exists(diffuse_path):
            tex_diffuse = load_texture_from_disk(
                nodes, diffuse_path,
                f"T_{material_name}_Diffuse",
                "Diffuse", (-700, 400), 'sRGB'
            )
            if tex_diffuse:
                links.new(tex_diffuse.outputs['Color'], bsdf.inputs['Base Color'])
        
        # Metallic
        if os.path.exists(metallic_path):
            tex_metallic = load_texture_from_disk(
                nodes, metallic_path,
                f"T_{material_name}_Metallic",
                "Metallic", (-700, 200), 'Non-Color'
            )
            if tex_metallic:
                links.new(tex_metallic.outputs['Color'], bsdf.inputs['Metallic'])
        
        # Roughness
        if os.path.exists(roughness_path):
            tex_roughness = load_texture_from_disk(
                nodes, roughness_path,
                f"T_{material_name}_Roughness",
                "Roughness", (-700, 0), 'Non-Color'
            )
            if tex_roughness:
                links.new(tex_roughness.outputs['Color'], bsdf.inputs['Roughness'])
        
        # Opacity
        if os.path.exists(opacity_path):
            tex_opacity = load_texture_from_disk(
                nodes, opacity_path,
                f"T_{material_name}_Opacity",
                "Opacity", (-700, -200), 'Non-Color'
            )
            if tex_opacity:
                links.new(tex_opacity.outputs['Color'], bsdf.inputs['Alpha'])
        
        # Normal
        if os.path.exists(normal_path):
            tex_normal = load_texture_from_disk(
                nodes, normal_path,
                f"T_{material_name}_Normal",
                "Normal", (-700, -400), 'Non-Color'
            )
            if tex_normal:
                connect_normal_map(nodes, links, tex_normal, bsdf, normal_type, (-400, -400))
    
    # Configure material settings
    material.blend_method = 'HASHED'
    material.shadow_method = 'HASHED'
    material.use_backface_culling = False
    
    # Set default BSDF values
    bsdf.inputs['Base Color'].default_value = (1.0, 1.0, 1.0, 1.0)
    bsdf.inputs['Metallic'].default_value = 0.0
    bsdf.inputs['Roughness'].default_value = 0.8
    bsdf.inputs['IOR'].default_value = 1.5
    bsdf.inputs['Alpha'].default_value = 1.0
    bsdf.inputs['Emission Color'].default_value = (0.0, 0.0, 0.0, 1.0)
    bsdf.inputs['Emission Strength'].default_value = 0.0
    
    # Update viewport
    bpy.context.view_layer.update()
    material.node_tree.update_tag()
    
    print(f"✅ Texture set connected to material: {material.name}")
    
    return material
