"""
Core baking utilities for AGR Baker v2
"""

import bpy
import os
import numpy as np


def create_texture_image(name, resolution, with_alpha=False):
    """Create a new texture image"""
    # Remove existing image with same name
    if name in bpy.data.images:
        existing = bpy.data.images[name]
        counter = 1
        new_name = f"{name}.{counter:03d}"
        while new_name in bpy.data.images:
            counter += 1
            new_name = f"{name}.{counter:03d}"
        existing.name = new_name
        print(f"🔄 Renamed existing: {name} → {new_name}")
    
    image = bpy.data.images.new(
        name,
        width=resolution,
        height=resolution,
        alpha=with_alpha,
        float_buffer=False
    )
    image.colorspace_settings.name = 'sRGB'
    
    # Initialize with white
    pixels = [1.0, 1.0, 1.0, 1.0] * (resolution * resolution)
    image.pixels.foreach_set(pixels)
    
    return image


def create_flat_normal_image(name, resolution=256):
    """Create flat normal map (0.5, 0.5, 1.0)"""
    if name in bpy.data.images:
        existing = bpy.data.images[name]
        counter = 1
        new_name = f"{name}.{counter:03d}"
        while new_name in bpy.data.images:
            counter += 1
            new_name = f"{name}.{counter:03d}"
        existing.name = new_name
    
    image = bpy.data.images.new(
        name,
        width=resolution,
        height=resolution,
        alpha=True,
        float_buffer=False
    )
    image.colorspace_settings.name = 'Non-Color'
    
    # Flat normal: RGB(0.5, 0.5, 1.0)
    pixels = [0.5, 0.5, 1.0, 1.0] * (resolution * resolution)
    image.pixels.foreach_set(pixels)
    
    return image


def setup_bake_node(material):
    """Setup material nodes for baking"""
    material.use_nodes = True
    nodes = material.node_tree.nodes
    
    # Ensure output node exists
    output = None
    for node in nodes:
        if node.type == 'OUTPUT_MATERIAL':
            output = node
            break
    
    if not output:
        output = nodes.new(type='ShaderNodeOutputMaterial')
        output.location = (300, 0)
    
    # Ensure BSDF exists
    bsdf = None
    for node in nodes:
        if node.type == 'BSDF_PRINCIPLED':
            bsdf = node
            break
    
    if not bsdf:
        bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
        bsdf.location = (0, 0)
        material.node_tree.links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
    
    # Create or find bake texture node
    bake_node = None
    for node in nodes:
        if node.type == 'TEX_IMAGE' and node.name.startswith('BakeTexture'):
            bake_node = node
            break
    
    if not bake_node:
        bake_node = nodes.new('ShaderNodeTexImage')
        bake_node.name = 'BakeTexture'
        bake_node.label = 'Bake Target'
        bake_node.location = (-300, 0)
    
    return bake_node


def bake_texture(context, target_obj, source_objects, image, bake_type, 
                 material_index, use_alpha=False, normal_type='OPENGL',
                 max_ray_distance=0.0, extrusion=0.5):
    """
    Bake texture from source objects to target object
    
    Args:
        context: Blender context
        target_obj: Target low-poly object
        source_objects: List of high-poly source objects (empty for simple bake)
        image: Target image to bake into
        bake_type: 'DIFFUSE', 'ROUGHNESS', 'NORMAL'
        material_index: Material slot index
        use_alpha: Bake with alpha channel
        normal_type: 'OPENGL' or 'DIRECTX'
        max_ray_distance: Max ray distance for baking
        extrusion: Cage extrusion value
    """
    material = target_obj.material_slots[material_index].material
    nodes = material.node_tree.nodes
    
    # Find or create texture node
    texture_node = None
    for node in nodes:
        if node.type == 'TEX_IMAGE' and node.name.startswith('BakeTexture'):
            texture_node = node
            break
    
    if not texture_node:
        texture_node = setup_bake_node(material)
    
    texture_node.image = image
    texture_node.select = True
    nodes.active = texture_node
    
    # Configure baking settings
    simple_mode = len(source_objects) == 0
    
    if simple_mode:
        context.scene.render.bake.use_selected_to_active = False
        context.scene.render.bake.cage_extrusion = 0.0
        context.scene.render.bake.max_ray_distance = 0.0
    else:
        context.scene.render.bake.use_selected_to_active = True
        context.scene.render.bake.cage_extrusion = extrusion
        context.scene.render.bake.max_ray_distance = max_ray_distance
    
    context.scene.render.bake.margin = 0 if (bake_type == 'DIFFUSE' and use_alpha) else 8
    context.scene.render.bake.use_clear = True
    
    original_film_transparent = context.scene.render.film_transparent
    
    # Configure bake type
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
    
    # Select objects for baking
    bpy.ops.object.select_all(action='DESELECT')
    
    if simple_mode:
        target_obj.select_set(True)
        context.view_layer.objects.active = target_obj
    else:
        for obj in source_objects:
            obj.select_set(True)
        target_obj.select_set(True)
        context.view_layer.objects.active = target_obj
    
    # Perform baking
    try:
        bpy.ops.object.bake(type=context.scene.cycles.bake_type)
        print(f"✅ Baked {bake_type} for {material.name}")
        
        if bake_type == 'NORMAL' and normal_type == 'DIRECTX':
            convert_normal_to_directx(image)
            print(f"✅ Converted to DirectX normal")
    
    except Exception as e:
        print(f"❌ Baking error {bake_type}: {e}")
        context.view_layer.update()
        try:
            bpy.ops.object.bake(type=context.scene.cycles.bake_type)
            print(f"✅ Baked {bake_type} on retry")
            
            if bake_type == 'NORMAL' and normal_type == 'DIRECTX':
                convert_normal_to_directx(image)
        except Exception as e2:
            print(f"❌ Retry failed {bake_type}: {e2}")
    
    context.scene.render.film_transparent = original_film_transparent


def convert_normal_to_directx(image):
    """Invert green channel for DirectX normal map"""
    try:
        width, height = image.size
        image.update()
        
        pixels = np.array(image.pixels[:]).reshape(height, width, 4)
        pixels[:, :, 1] = 1.0 - pixels[:, :, 1]  # Invert green
        
        image.pixels = pixels.flatten().tolist()
        image.update()
        print(f"✅ Green channel inverted for {image.name}")
    
    except Exception as e:
        print(f"❌ DirectX conversion error: {e}")


def save_texture(image, filepath):
    """Save texture to disk"""
    image.update()
    
    scene = bpy.context.scene
    
    # Store original settings
    original_format = scene.render.image_settings.file_format
    original_color_mode = scene.render.image_settings.color_mode
    original_color_depth = scene.render.image_settings.color_depth
    original_view = scene.view_settings.view_transform
    original_look = scene.view_settings.look
    original_display = scene.display_settings.display_device
    
    # Configure for PNG export
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.compression = 15
    
    if "DIFFUSE_OPACITY" in image.name:
        scene.render.image_settings.color_mode = 'RGBA'
    else:
        scene.render.image_settings.color_mode = 'RGB'
    
    scene.render.image_settings.color_depth = '8'
    scene.view_settings.view_transform = 'Standard'
    scene.view_settings.look = 'None'
    scene.display_settings.display_device = 'sRGB'
    
    # Save
    original_filepath = image.filepath
    image.filepath_raw = filepath
    
    try:
        image.save_render(filepath)
        print(f"💾 Saved: {filepath}")
    except Exception as e:
        print(f"❌ Save error {filepath}: {e}")
        image.save_render(filepath, scene=scene)
    
    image.filepath = original_filepath
    
    # Restore settings
    scene.render.image_settings.file_format = original_format
    scene.render.image_settings.color_mode = original_color_mode
    scene.render.image_settings.color_depth = original_color_depth
    scene.view_settings.view_transform = original_view
    scene.view_settings.look = original_look
    scene.display_settings.display_device = original_display
