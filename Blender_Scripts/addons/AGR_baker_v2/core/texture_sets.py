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
    
    # Scan for S_* folders
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
                    'textures': texture_info
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
    
    for key, filename in texture_types.items():
        filepath = os.path.join(folder_path, filename)
        if os.path.exists(filepath):
            texture_info[key] = True
            texture_info['has_any'] = True
            
            # Try to get resolution from first found texture
            if texture_info['resolution'] == 1024:
                try:
                    img = bpy.data.images.load(filepath)
                    texture_info['resolution'] = img.size[0]
                    bpy.data.images.remove(img)
                except:
                    pass
    
    return texture_info


def refresh_texture_sets_list(context):
    """Refresh texture sets list in scene properties"""
    texture_sets_collection = context.scene.agr_texture_sets
    
    # Clear existing
    texture_sets_collection.clear()
    
    # Scan and add
    found_sets = scan_texture_sets(context)
    
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
        
        # Check if assigned to material
        tex_set.is_assigned = check_if_set_assigned(context, set_data['material_name'])
    
    print(f"✅ Refreshed texture sets list: {len(found_sets)} sets")
    return len(found_sets)


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
    tex_set.resolution = resolution
    
    # Scan folder for available textures
    texture_info = scan_texture_set_folder(folder_path, material_name)
    tex_set.has_diffuse = texture_info['has_diffuse']
    tex_set.has_diffuse_opacity = texture_info['has_diffuse_opacity']
    tex_set.has_emit = texture_info['has_emit']
    tex_set.has_roughness = texture_info['has_roughness']
    tex_set.has_opacity = texture_info['has_opacity']
    tex_set.has_normal = texture_info['has_normal']
    tex_set.has_erm = texture_info['has_erm']
    tex_set.has_metallic = texture_info['has_metallic']
    
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
