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
    
    # Set alpha mode for proper alpha channel handling
    if with_alpha:
        image.alpha_mode = 'STRAIGHT'
        print(f"  🔧 Created image with alpha channel: {name}")
    
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
                 material_index, use_alpha=False,
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
        image.colorspace_settings.name = 'sRGB'

        if use_alpha:
            context.scene.render.film_transparent = True
    
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
    
    except Exception as e:
        print(f"❌ Baking error {bake_type}: {e}")
        context.view_layer.update()
        try:
            bpy.ops.object.bake(type=context.scene.cycles.bake_type)
            print(f"✅ Baked {bake_type} on retry")
        except Exception as e2:
            print(f"❌ Retry failed {bake_type}: {e2}")
            raise RuntimeError(f"Bake failed for {bake_type} after retry: {e2}") from e2
    
    context.scene.render.film_transparent = original_film_transparent


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
    
    # Check for DiffuseOpacity in any case variation
    if "DIFFUSE_OPACITY" in image.name.upper() or "DIFFUSEOPACITY" in image.name.upper():
        scene.render.image_settings.color_mode = 'RGBA'
    else:
        scene.render.image_settings.color_mode = 'RGB'
    
    scene.render.image_settings.color_depth = '8'
    scene.view_settings.view_transform = 'Standard'
    scene.view_settings.look = 'None'
    scene.display_settings.display_device = 'sRGB'
    
    # Save
    original_filepath = image.filepath
    original_filepath_raw = image.filepath_raw
    image.filepath_raw = filepath

    try:
        image.save_render(filepath)
        print(f"💾 Saved: {filepath}")
    except Exception as e:
        print(f"❌ Save error {filepath}: {e}")
        image.save_render(filepath, scene=scene)

    image.filepath = original_filepath
    image.filepath_raw = original_filepath_raw
    
    # Restore settings
    scene.render.image_settings.file_format = original_format
    scene.render.image_settings.color_mode = original_color_mode
    scene.render.image_settings.color_depth = original_color_depth
    scene.view_settings.view_transform = original_view
    scene.view_settings.look = original_look
    scene.display_settings.display_device = original_display


def get_texture_resolution_from_node(node):
    """Get resolution from texture node if it's an image texture"""
    if node.type == 'TEX_IMAGE' and node.image:
        width, height = node.image.size
        return max(width, height)
    return None


def check_socket_connection(material, socket_name):
    """
    Check if a Principled BSDF socket has connections and determine resolution
    
    Returns:
        tuple: (has_connection, resolution, is_texture_node)
        - has_connection: bool - True if socket has any connection
        - resolution: int or None - texture resolution if connected to texture
        - is_texture_node: bool - True if directly connected to texture node
    """
    if not material or not material.use_nodes:
        return False, None, False
    
    for node in material.node_tree.nodes:
        if node.type == 'BSDF_PRINCIPLED':
            if socket_name not in node.inputs:
                continue
            
            socket = node.inputs[socket_name]
            
            # Check if socket has links
            if not socket.links:
                return False, None, False
            
            # Get the connected node
            from_node = socket.links[0].from_node
            
            # Check if it's a texture node
            if from_node.type == 'TEX_IMAGE':
                resolution = get_texture_resolution_from_node(from_node)
                return True, resolution, True
            
            # Check if connected through other nodes (like ColorRamp, Separate, etc)
            # Trace back to find texture nodes
            resolution = trace_to_texture_node(from_node, visited=set())
            if resolution:
                return True, resolution, False
            
            # Has connection but not to texture
            return True, None, False
    
    return False, None, False


def trace_to_texture_node(node, visited=None, depth=0, max_depth=10):
    """
    Recursively trace node connections to find texture nodes
    
    Returns:
        int or None: texture resolution if found, None otherwise
    """
    if visited is None:
        visited = set()
    
    if depth > max_depth or node in visited:
        return None
    
    visited.add(node)
    
    # Check if current node is texture
    if node.type == 'TEX_IMAGE':
        return get_texture_resolution_from_node(node)
    
    # Trace through input connections
    for input_socket in node.inputs:
        if input_socket.links:
            from_node = input_socket.links[0].from_node
            resolution = trace_to_texture_node(from_node, visited, depth + 1, max_depth)
            if resolution:
                return resolution
    
    return None


def determine_bake_resolution(material, socket_name, user_resolution):
    """
    Determine appropriate baking resolution based on socket connections
    
    Args:
        material: Material to check
        socket_name: Name of Principled BSDF socket ('Base Color', 'Roughness', etc)
        user_resolution: User-selected resolution
    
    Returns:
        int: Resolution to use for baking (256 or user_resolution)
    """
    # If user selected 256 or less, always use user resolution (ignore stub rules)
    if user_resolution <= 256:
        print(f"  📏 {socket_name}: User resolution <= 256 → {user_resolution}px (forced)")
        return user_resolution
    
    has_connection, texture_res, is_texture = check_socket_connection(material, socket_name)
    
    # No connection → 256px stub
    if not has_connection:
        print(f"  📏 {socket_name}: No connection → 256px stub")
        return 256
    
    # Connected to 256px texture → 256px
    if texture_res == 256:
        print(f"  📏 {socket_name}: Connected to 256px texture → 256px")
        return 256
    
    # Connected to low-res texture (64 or 128) and user wants high quality (>256) → use 256px
    if texture_res and texture_res < 256 and user_resolution > 256:
        print(f"  📏 {socket_name}: Connected to {texture_res}px texture, user wants {user_resolution}px → 256px (middle ground)")
        return 256
    
    # Connected to texture with different resolution → user resolution
    if texture_res and texture_res != 256:
        print(f"  📏 {socket_name}: Connected to {texture_res}px texture → {user_resolution}px (user)")
        return user_resolution
    
    # Connected to non-texture nodes → user resolution
    print(f"  📏 {socket_name}: Connected to procedural → {user_resolution}px (user)")
    return user_resolution


def determine_alpha_resolution(material, user_resolution):
    """
    Determine resolution for alpha-related baking
    If Alpha socket has connection, use user resolution, otherwise 256px
    
    Returns:
        int: Resolution to use
    """
    has_connection, texture_res, _ = check_socket_connection(material, 'Alpha')
    
    if has_connection:
        print(f"  📏 Alpha: Connected → {user_resolution}px (user)")
        return user_resolution
    else:
        print(f"  📏 Alpha: No connection → 256px stub")
        return 256


def check_normal_is_only_normal_map(material):
    """
    Check if Normal socket is connected only through a Normal Map node
    (no other processing nodes)
    
    Returns:
        bool: True if only Normal Map node is connected
    """
    if not material or not material.use_nodes:
        return False
    
    for node in material.node_tree.nodes:
        if node.type == 'BSDF_PRINCIPLED':
            if 'Normal' not in node.inputs:
                return False
            
            socket = node.inputs['Normal']
            
            # Check if socket has links
            if not socket.links:
                return False
            
            # Get the connected node
            from_node = socket.links[0].from_node
            
            # Check if it's a Normal Map node
            if from_node.type == 'NORMAL_MAP':
                # Check if Normal Map is connected to a texture
                color_input = from_node.inputs.get('Color')
                if color_input and color_input.links:
                    texture_node = color_input.links[0].from_node
                    if texture_node.type == 'TEX_IMAGE':
                        print(f"  🔍 Normal: Only Normal Map node detected → 256px")
                        return True
            
            return False
    
    return False


def check_normal_map_without_texture(material):
    """
    Check if Normal socket is connected to Normal Map node WITHOUT anything connected to Color input
    
    Returns:
        bool: True if Normal Map node exists but Color input has no connections
    """
    if not material or not material.use_nodes:
        print(f"  🔍 Normal check: Material has no nodes")
        return False
    
    for node in material.node_tree.nodes:
        if node.type == 'BSDF_PRINCIPLED':
            if 'Normal' not in node.inputs:
                print(f"  🔍 Normal check: BSDF has no Normal input")
                return False
            
            socket = node.inputs['Normal']
            
            # Check if socket has links
            if not socket.links:
                print(f"  🔍 Normal check: Normal socket has no links")
                return False
            
            # Get the connected node
            from_node = socket.links[0].from_node
            print(f"  🔍 Normal check: Connected node type = '{from_node.type}', name = '{from_node.name}'")
            
            # Check if it's a Normal Map node
            if from_node.type == 'NORMAL_MAP':
                print(f"  🔍 Normal check: Found Normal Map node")
                # Check if Normal Map's Color input has anything connected
                color_input = from_node.inputs.get('Color')
                if not color_input:
                    print(f"  🔍 Normal check: Normal Map has no Color input")
                    return False
                
                if not color_input.links:
                    # Nothing connected to Color input → use 256px stub
                    print(f"  🔍 Normal: Normal Map with empty Color input → 256px stub")
                    return True
                
                # Something is connected to Color input → bake normally
                print(f"  🔍 Normal: Normal Map with Color input connected → bake normally")
                return False
            
            # Not a Normal Map node → bake normally
            print(f"  🔍 Normal check: Not a Normal Map node, returning False")
            return False
    
    print(f"  🔍 Normal check: No BSDF_PRINCIPLED found")
    return False


def is_image_fully_white(image, check_alpha=False):
    """
    Check if image is fully white (all pixels are 1.0)
    
    Args:
        image: Blender image
        check_alpha: If True, check alpha channel; if False, check RGB
    
    Returns:
        bool: True if fully white
    """
    if not image:
        return False
    
    try:
        import numpy as np
        
        # Get pixels as numpy array
        width, height = image.size
        pixels = np.array(image.pixels[:]).reshape(height, width, 4)
        
        if check_alpha:
            # Check only alpha channel
            alpha_channel = pixels[:, :, 3]
            is_white = np.all(alpha_channel > 0.99)
            print(f"  🔍 Alpha channel check: min={alpha_channel.min():.3f}, max={alpha_channel.max():.3f}, fully_white={is_white}")
            return is_white
        else:
            # Check RGB channels
            rgb_channels = pixels[:, :, :3]
            is_white = np.all(rgb_channels > 0.99)
            print(f"  🔍 RGB check: min={rgb_channels.min():.3f}, max={rgb_channels.max():.3f}, fully_white={is_white}")
            return is_white
    
    except Exception as e:
        print(f"  ⚠️ Error checking if image is white: {e}")
        return False


def should_bake_with_alpha(material):
    """
    Auto-detect if we should bake with alpha channel
    
    Rules:
    1. If Alpha connected to texture Alpha output → check if alpha channel is fully white
    2. If Alpha connected to texture Color output → check if texture is fully white
    3. If fully white → don't bake alpha
    
    Returns:
        bool: True if should bake with alpha
    """
    if not material or not material.use_nodes:
        return False
    
    for node in material.node_tree.nodes:
        if node.type == 'BSDF_PRINCIPLED':
            if 'Alpha' not in node.inputs:
                return False
            
            socket = node.inputs['Alpha']
            
            # Check if socket has links
            if not socket.links:
                print(f"  🔍 Alpha: No connection → bake without alpha")
                return False
            
            # Get the connected node and socket
            link = socket.links[0]
            from_node = link.from_node
            from_socket = link.from_socket
            
            # Check if it's a texture node
            if from_node.type == 'TEX_IMAGE':
                if not from_node.image:
                    print(f"  🔍 Alpha: Texture node has no image → bake without alpha")
                    return False
                
                # Check if connected through Color output
                if from_socket.name == 'Color':
                    print(f"  🔍 Alpha: Connected to texture Color output")
                    # Check if texture is fully white
                    if is_image_fully_white(from_node.image, check_alpha=False):
                        print(f"  🔍 Alpha: Texture is fully white → bake without alpha")
                        return False
                    else:
                        print(f"  🔍 Alpha: Texture has color variation → bake with alpha")
                        return True
                
                # Check if connected through Alpha output
                elif from_socket.name == 'Alpha':
                    print(f"  🔍 Alpha: Connected to texture Alpha output")
                    # Check if texture has alpha channel
                    has_alpha = from_node.image.alpha_mode != 'NONE' and from_node.image.depth in (32, 64)
                    if not has_alpha:
                        print(f"  🔍 Alpha: Texture has no alpha channel → bake without alpha")
                        return False
                    
                    # Check if alpha channel is fully white
                    if is_image_fully_white(from_node.image, check_alpha=True):
                        print(f"  🔍 Alpha: Alpha channel is fully white → bake without alpha")
                        return False
                    else:
                        print(f"  🔍 Alpha: Alpha channel has variation → bake with alpha")
                        return True
            
            # Connected to non-texture node (procedural, etc)
            print(f"  🔍 Alpha: Connected to procedural → bake with alpha")
            return True
    
    return False


def determine_pbr_group_resolution(material, user_resolution):
    """
    Determine resolution for PBR group (Roughness, Metallic, Emission)
    
    Rules:
    - If user_resolution <= 256: always use user_resolution
    - If any socket has texture < 256px and user wants > 256px: use 256px (middle ground)
    - If any socket has texture > 256px or procedural: use user_resolution
    - If all sockets have 256px textures or no connections: use 256px
    
    Returns:
        int: Resolution to use
    """
    # Rule 1: If user selected 256 or less, always use it
    if user_resolution <= 256:
        print(f"  📏 PBR Group: User resolution <= 256 → {user_resolution}px (forced)")
        return user_resolution
    
    sockets = ['Roughness', 'Metallic', 'Emission Strength']
    
    has_any_connection = False
    has_low_res_texture = False
    has_high_res_or_procedural = False
    
    for socket_name in sockets:
        has_connection, texture_res, _ = check_socket_connection(material, socket_name)
        if has_connection:
            has_any_connection = True
            
            # Check if texture is low-res (< 256px)
            if texture_res and texture_res < 256:
                has_low_res_texture = True
                print(f"  📏 PBR Group: {socket_name} has {texture_res}px texture (< 256px)")
            
            # Check if texture is high-res (> 256px) or procedural (texture_res is None)
            elif texture_res is None or texture_res > 256:
                has_high_res_or_procedural = True
                if texture_res:
                    print(f"  📏 PBR Group: {socket_name} has {texture_res}px texture (> 256px) → {user_resolution}px (user)")
                else:
                    print(f"  📏 PBR Group: {socket_name} has procedural connection → {user_resolution}px (user)")
                return user_resolution
    
    # If any socket has low-res texture (< 256px) and user wants high quality
    if has_low_res_texture and user_resolution > 256:
        print(f"  📏 PBR Group: Has low-res textures, user wants {user_resolution}px → 256px (middle ground)")
        return 256
    
    # If any connection exists but all are 256px textures
    if has_any_connection:
        print(f"  📏 PBR Group: All connections are 256px textures → 256px")
        return 256
    
    # No connections → 256px stub
    print(f"  📏 PBR Group: No connections → 256px stub")
    return 256


def create_bake_plane(name="BakePlane"):
    """
    Create a plane with standard UV mapping for simple baking
    
    Returns:
        bpy.types.Object: Created plane object
    """
    # Create plane mesh
    bpy.ops.mesh.primitive_plane_add(size=2, location=(0, 0, 0))
    plane = bpy.context.active_object
    plane.name = name
    
    # Ensure UV map exists
    if not plane.data.uv_layers:
        plane.data.uv_layers.new(name="UVMap")
    
    # Explicitly set UV coordinates to ensure full 0-1 coverage
    # Blender plane has 4 vertices forming 1 quad face with 4 loops
    mesh = plane.data
    uv_layer = mesh.uv_layers.active.data
    
    # Get the face (should be only one quad)
    if len(mesh.polygons) > 0:
        face = mesh.polygons[0]
        
        # Get vertex positions to determine UV mapping
        # We need to map based on actual vertex positions
        vertices = [mesh.vertices[i] for i in face.vertices]
        
        # Map each loop to correct UV based on vertex position
        for loop_idx in face.loop_indices:
            loop = mesh.loops[loop_idx]
            vert = mesh.vertices[loop.vertex_index]
            
            # Map vertex position to UV (0-1 range)
            # Assuming plane is centered at origin with size 2 (-1 to 1 in world space)
            u = (vert.co.x + 1.0) / 2.0  # Map -1..1 to 0..1
            v = (vert.co.y + 1.0) / 2.0  # Map -1..1 to 0..1
            
            uv_layer[loop_idx].uv = (u, v)
    
    print(f"  ✅ Created bake plane: {name} with explicit UV mapping (0-1)")
    return plane
