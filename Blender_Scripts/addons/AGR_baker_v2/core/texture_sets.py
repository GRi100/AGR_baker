"""
Texture set management for AGR Baker v2
"""

import bpy
import os
from pathlib import Path


def get_agr_bake_folder(context):
    """Get AGR_BAKE folder path next to blend file"""
    blend_path = bpy.path.abspath("//")
    if not blend_path:
        return None
    
    settings = context.scene.agr_baker_settings
    agr_bake_path = os.path.join(blend_path, settings.output_folder)
    
    return agr_bake_path


def ensure_agr_bake_folder(context):
    """Ensure AGR_BAKE folder exists"""
    agr_bake_path = get_agr_bake_folder(context)
    if not agr_bake_path:
        return None
    
    if not os.path.exists(agr_bake_path):
        os.makedirs(agr_bake_path)
        print(f"📁 Created AGR_BAKE folder: {agr_bake_path}")
    
    return agr_bake_path


def get_texture_set_folder(context, material_name):
    """Get texture set folder path (S_material_name)"""
    agr_bake_path = ensure_agr_bake_folder(context)
    if not agr_bake_path:
        return None
    
    set_folder = os.path.join(agr_bake_path, f"S_{material_name}")
    return set_folder


def ensure_texture_set_folder(context, material_name):
    """Ensure texture set folder exists"""
    set_folder = get_texture_set_folder(context, material_name)
    if not set_folder:
        return None
    
    if not os.path.exists(set_folder):
        os.makedirs(set_folder)
        print(f"📁 Created texture set folder: {set_folder}")
    
    return set_folder


def scan_texture_sets(context):
    """
    Scan AGR_BAKE folder for texture sets (S_material_name folders)
    Returns list of found texture sets
    """
    agr_bake_path = get_agr_bake_folder(context)
    if not agr_bake_path or not os.path.exists(agr_bake_path):
        print("⚠️ AGR_BAKE folder not found")
        return []
    
    texture_sets = []
    
    # Scan for S_* folders (regular texture sets)
    for item in os.listdir(agr_bake_path):
        item_path = os.path.join(agr_bake_path, item)
        
        if os.path.isdir(item_path) and item.startswith("S_"):
            material_name = item[2:]  # Remove "S_" prefix
            
            # Check for textures in folder
            texture_info = scan_texture_set_folder(item_path, material_name)
            
            if texture_info['has_any']:
                texture_sets.append({
                    'name': item,
                    'material_name': material_name,
                    'folder_path': item_path,
                    'textures': texture_info,
                    'is_atlas': False
                })
    
    print(f"🔍 Found {len(texture_sets)} texture sets in AGR_BAKE")
    return texture_sets


def scan_texture_set_folder(folder_path, material_name):
    """
    Scan texture set folder for available textures
    Returns dict with texture availability flags
    """
    texture_info = {
        'has_diffuse': False,
        'has_diffuse_opacity': False,
        'has_emit': False,
        'has_roughness': False,
        'has_opacity': False,
        'has_normal': False,
        'has_erm': False,
        'has_metallic': False,
        'has_any': False,
        'resolution': 1024
    }
    
    # Check for each texture type
    texture_types = {
        'has_diffuse': f"T_{material_name}_Diffuse.png",
        'has_diffuse_opacity': f"T_{material_name}_DiffuseOpacity.png",
        'has_emit': f"T_{material_name}_Emit.png",
        'has_roughness': f"T_{material_name}_Roughness.png",
        'has_opacity': f"T_{material_name}_Opacity.png",
        'has_normal': f"T_{material_name}_Normal.png",
        'has_erm': f"T_{material_name}_ERM.png",
        'has_metallic': f"T_{material_name}_Metallic.png",
    }
    
    max_resolution = 0
    
    for key, filename in texture_types.items():
        filepath = os.path.join(folder_path, filename)
        if os.path.exists(filepath):
            texture_info[key] = True
            texture_info['has_any'] = True
            
            # Get resolution from each texture and find maximum
            try:
                img = bpy.data.images.load(filepath)
                current_resolution = max(img.size[0], img.size[1])  # Use max of width/height
                if current_resolution > max_resolution:
                    max_resolution = current_resolution
                    print(f"  📏 {filename}: {current_resolution}px (new max)")
                bpy.data.images.remove(img)
            except Exception as e:
                print(f"  ⚠️ Could not read resolution from {filename}: {e}")
    
    # Set resolution to maximum found, or keep default 1024
    if max_resolution > 0:
        texture_info['resolution'] = max_resolution
        print(f"  ✅ Set resolution determined: {max_resolution}px")
    else:
        print(f"  ⚠️ No textures found, using default 1024px")
    
    return texture_info


def refresh_texture_sets_list(context):
    """Refresh texture sets list in scene properties"""
    texture_sets_collection = context.scene.agr_texture_sets
    settings = context.scene.agr_baker_settings
    
    # Clear existing
    texture_sets_collection.clear()
    
    # Scan and add
    found_sets = scan_texture_sets(context)
    
    # Check alpha on all sets BEFORE sorting (to preserve alpha info)
    import struct
    for set_data in found_sets:
        set_data['textures']['has_alpha'] = False
        
        if set_data['textures']['has_diffuse_opacity']:
            material_name = set_data['material_name']
            folder_path = set_data['folder_path']
            do_path = os.path.join(folder_path, f"T_{material_name}_DiffuseOpacity.png")
            
            if os.path.exists(do_path):
                try:
                    with open(do_path, 'rb') as f:
                        signature = f.read(8)
                        if signature == b'\x89PNG\r\n\x1a\n':
                            chunk_length_bytes = f.read(4)
                            if len(chunk_length_bytes) == 4:
                                chunk_type = f.read(4)
                                if chunk_type == b'IHDR':
                                    ihdr_data = f.read(13)
                                    if len(ihdr_data) == 13:
                                        # IHDR structure: width(4) + height(4) + bit_depth(1) + color_type(1) + ...
                                        color_type = ihdr_data[9]  # Color type is at byte 9, not 8
                                        print(f"  🔍 {material_name}: PNG color_type = {color_type}")
                                        if color_type in (4, 6):  # Grayscale+Alpha or RGBA
                                            set_data['textures']['has_alpha'] = True
                                            print(f"  ✅ {material_name}: Has alpha channel!")
                                        else:
                                            print(f"  ⚠️ {material_name}: No alpha (color_type {color_type})")
                except Exception as e:
                    print(f"  ❌ {material_name}: Error checking alpha: {e}")
    
    # Sort based on settings
    if settings.sets_sort_mode == 'NAME':
        found_sets.sort(key=lambda x: x['name'])
        print(f"  📋 Sorted by name (alphabetically)")
    elif settings.sets_sort_mode == 'RESOLUTION':
        found_sets.sort(key=lambda x: x['textures']['resolution'], reverse=True)
        print(f"  📋 Sorted by resolution (high to low)")
    elif settings.sets_sort_mode == 'ALPHA':
        # Sort by alpha presence (with alpha first), then by name
        found_sets.sort(key=lambda x: (not x['textures'].get('has_alpha', False), x['name']))
        print(f"  📋 Sorted by alpha presence (with alpha first)")
    
    for set_data in found_sets:
        tex_set = texture_sets_collection.add()
        tex_set.name = set_data['name']
        tex_set.material_name = set_data['material_name']
        tex_set.folder_path = set_data['folder_path']
        
        # Copy texture flags
        textures = set_data['textures']
        tex_set.has_diffuse = textures['has_diffuse']
        tex_set.has_diffuse_opacity = textures['has_diffuse_opacity']
        tex_set.has_emit = textures['has_emit']
        tex_set.has_roughness = textures['has_roughness']
        tex_set.has_opacity = textures['has_opacity']
        tex_set.has_normal = textures['has_normal']
        tex_set.has_erm = textures['has_erm']
        tex_set.has_metallic = textures['has_metallic']
        tex_set.resolution = textures['resolution']
        
        # Copy alpha flag (already checked above)
        tex_set.has_alpha = textures.get('has_alpha', False)
        tex_set.is_selected = False
        
        # Atlas-specific properties
        tex_set.is_atlas = set_data.get('is_atlas', False)
        if tex_set.is_atlas:
            tex_set.atlas_type = set_data.get('atlas_type', 'HIGH')
            tex_set.object_name = set_data.get('object_name', '')
        
        # Check if assigned to material
        tex_set.is_assigned = check_if_set_assigned(context, set_data['material_name'])
    
    print(f"✅ Refreshed texture sets list: {len(found_sets)} sets")
    return len(found_sets)


def sort_texture_sets_in_scene(context, sort_mode: str):
    """Sort existing `Scene.agr_texture_sets` collection in-place.

    This is a fast path used by the Sort UI buttons.
    It MUST NOT rescan folders / load images / recompute alpha/resolution.
    All required fields (`name`, `resolution`, `has_alpha`) are expected to be
    already populated during earlier refresh/scan stages.
    """

    texture_sets_collection = context.scene.agr_texture_sets
    if not texture_sets_collection or len(texture_sets_collection) < 2:
        return

    # Build desired order (store original indices to keep sort stable)
    indexed = list(enumerate(texture_sets_collection))

    if sort_mode == 'NAME':
        indexed.sort(key=lambda it: (it[1].name.casefold(), it[0]))
    elif sort_mode == 'RESOLUTION':
        indexed.sort(key=lambda it: (-int(it[1].resolution), it[1].name.casefold(), it[0]))
    elif sort_mode == 'ALPHA':
        indexed.sort(key=lambda it: (not bool(getattr(it[1], 'has_alpha', False)), it[1].name.casefold(), it[0]))
    else:
        return

    # Reorder collection using `move`.
    target_names = [item.name for _, item in indexed]
    name_to_current_index = {texture_sets_collection[i].name: i for i in range(len(texture_sets_collection))}

    for target_index, name in enumerate(target_names):
        current_index = name_to_current_index.get(name)
        if current_index is None or current_index == target_index:
            continue

        texture_sets_collection.move(current_index, target_index)

        # Update mapping for indices affected by the move.
        if current_index > target_index:
            for n, idx in list(name_to_current_index.items()):
                if target_index <= idx < current_index:
                    name_to_current_index[n] = idx + 1
        else:
            for n, idx in list(name_to_current_index.items()):
                if current_index < idx <= target_index:
                    name_to_current_index[n] = idx - 1

        name_to_current_index[name] = target_index


def check_if_set_assigned(context, material_name):
    """Check if texture set is assigned to a material"""
    # Check if material exists with this name
    if material_name in bpy.data.materials:
        material = bpy.data.materials[material_name]
        
        # Check if material has texture nodes
        if material.use_nodes:
            for node in material.node_tree.nodes:
                if node.type == 'TEX_IMAGE' and node.image:
                    # Check if image name matches texture set
                    if f"T_{material_name}_" in node.image.name:
                        return True
    
    return False


def save_texture_set_info(context, material_name, resolution, folder_path):
    """Save texture set info to scene collection"""
    texture_sets = context.scene.agr_texture_sets
    
    # Check if set already exists
    existing_set = None
    for tex_set in texture_sets:
        if tex_set.material_name == material_name:
            existing_set = tex_set
            break
    
    if existing_set:
        tex_set = existing_set
    else:
        tex_set = texture_sets.add()
    
    tex_set.name = f"S_{material_name}"
    tex_set.material_name = material_name
    tex_set.folder_path = folder_path
    
    # Scan folder for available textures and get actual resolution
    texture_info = scan_texture_set_folder(folder_path, material_name)
    tex_set.has_diffuse = texture_info['has_diffuse']
    tex_set.has_diffuse_opacity = texture_info['has_diffuse_opacity']
    tex_set.has_emit = texture_info['has_emit']
    tex_set.has_roughness = texture_info['has_roughness']
    tex_set.has_opacity = texture_info['has_opacity']
    tex_set.has_normal = texture_info['has_normal']
    tex_set.has_erm = texture_info['has_erm']
    tex_set.has_metallic = texture_info['has_metallic']
    
    # Use scanned resolution (max from all textures) instead of parameter
    tex_set.resolution = texture_info['resolution']
    
    print(f"💾 Saved texture set info: S_{material_name}")
    
    return tex_set


def get_texture_paths(folder_path, material_name):
    """Get all texture paths for a material"""
    return {
        'diffuse': os.path.join(folder_path, f"T_{material_name}_Diffuse.png"),
        'diffuse_opacity': os.path.join(folder_path, f"T_{material_name}_DiffuseOpacity.png"),
        'emit': os.path.join(folder_path, f"T_{material_name}_Emit.png"),
        'roughness': os.path.join(folder_path, f"T_{material_name}_Roughness.png"),
        'opacity': os.path.join(folder_path, f"T_{material_name}_Opacity.png"),
        'normal': os.path.join(folder_path, f"T_{material_name}_Normal.png"),
        'erm': os.path.join(folder_path, f"T_{material_name}_ERM.png"),
        'metallic': os.path.join(folder_path, f"T_{material_name}_Metallic.png"),
    }
