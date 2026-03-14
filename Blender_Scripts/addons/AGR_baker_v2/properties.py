"""
Property definitions for AGR Baker v2
"""

import bpy
from bpy.props import (
    StringProperty, 
    BoolProperty, 
    IntProperty, 
    EnumProperty,
    CollectionProperty,
    PointerProperty
)
from bpy.types import PropertyGroup


class AGR_TextureSet(PropertyGroup):
    """Texture set data (S_material_name or A_atlas_name)"""
    
    name: StringProperty(
        name="Set Name",
        description="Texture set name (S_material_name or A_atlas_name)",
        default=""
    )
    
    material_name: StringProperty(
        name="Material Name",
        description="Associated material name",
        default=""
    )
    
    folder_path: StringProperty(
        name="Folder Path",
        description="Path to texture set folder",
        default="",
        subtype='DIR_PATH'
    )
    
    resolution: IntProperty(
        name="Resolution",
        description="Texture resolution",
        default=1024,
        min=64,
        max=8192
    )
    
    # Texture availability flags
    has_diffuse: BoolProperty(name="Diffuse", default=False)
    has_diffuse_opacity: BoolProperty(name="Diffuse+Opacity", default=False)
    has_emit: BoolProperty(name="Emit", default=False)
    has_roughness: BoolProperty(name="Roughness", default=False)
    has_opacity: BoolProperty(name="Opacity", default=False)
    has_normal: BoolProperty(name="Normal", default=False)
    has_erm: BoolProperty(name="ERM", default=False)
    has_metallic: BoolProperty(name="Metallic", default=False)
    
    has_alpha: BoolProperty(
        name="Has Alpha",
        description="DiffuseOpacity texture has alpha channel",
        default=False
    )
    
    is_assigned: BoolProperty(
        name="Assigned",
        description="Set is assigned to a material",
        default=False
    )
    
    is_selected: BoolProperty(
        name="Selected",
        description="Set is selected for batch operations",
        default=False
    )
    
    # Atlas-specific properties
    is_atlas: BoolProperty(
        name="Is Atlas",
        description="This set is an atlas (prefix A_)",
        default=False
    )
    
    atlas_type: EnumProperty(
        name="Atlas Type",
        description="Type of atlas (HIGH or LOW)",
        items=[
            ('HIGH', "HIGH", "Atlas with DO/ERM/N textures"),
            ('LOW', "LOW", "Atlas with d/r/m/o/n separate textures")
        ],
        default='HIGH'
    )
    
    object_name: StringProperty(
        name="Object Name",
        description="Name of the object this set was baked from",
        default=""
    )


class AGR_BakerSettings(PropertyGroup):
    """Main addon settings"""
    
    # Baking settings
    resolution: EnumProperty(
        name="Resolution",
        description="Texture resolution for baking",
        items=[
            ('64', "64", "64x64"),
            ('128', "128", "128x128"),
            ('256', "256", "256x256"),
            ('512', "512", "512x512"),
            ('1024', "1024", "1024x1024"),
            ('2048', "2048", "2048x2048"),
            ('4096', "4096", "4096x4096"),
        ],
        default='1024'
    )
    
    bake_with_alpha: BoolProperty(
        name="Bake with Alpha",
        description="Bake diffuse with alpha channel",
        default=False
    )
    
    bake_normal_enabled: BoolProperty(
        name="Bake Normal from High-Poly",
        description="Bake normal map from high-poly objects (if disabled, creates flat normal)",
        default=True
    )
    
    
    max_ray_distance: bpy.props.FloatProperty(
        name="Max Ray Distance",
        description="Maximum ray distance for baking",
        default=0.0,
        min=0.0,
        max=100.0
    )
    
    extrusion: bpy.props.FloatProperty(
        name="Extrusion",
        description="Cage extrusion",
        default=0.5,
        min=0.0,
        max=10.0
    )
    
    # Render settings for baking
    bake_samples: IntProperty(
        name="Samples",
        description="Number of samples for baking",
        default=1,
        min=1,
        max=4096
    )
    
    bake_device: EnumProperty(
        name="Device",
        description="Device to use for baking",
        items=[
            ('CPU', "CPU", "Use CPU for baking"),
            ('GPU', "GPU", "Use GPU for baking (if available)"),
        ],
        default='CPU'
    )
    
    bake_use_denoising: BoolProperty(
        name="Denoise",
        description="Use denoising during baking",
        default=False
    )
    
    # Photoshop integration
    photoshop_path: StringProperty(
        name="Photoshop Path",
        description="Path to Photoshop executable",
        default="",
        subtype='FILE_PATH'
    )
    
    photoshop_enabled: BoolProperty(
        name="Enable Photoshop",
        description="Enable Photoshop integration for texture processing",
        default=False
    )
    
    # Output settings
    output_folder: StringProperty(
        name="Output Folder",
        description="Base output folder (AGR_BAKE)",
        default="AGR_BAKE"
    )
    
    # Texture sets list settings
    sets_sort_mode: EnumProperty(
        name="Sort By",
        description="Sort texture sets list",
        items=[
            ('NAME', "Name", "Sort alphabetically by name"),
            ('RESOLUTION', "Resolution", "Sort by resolution (high to low)"),
            ('ALPHA', "Alpha", "Sort by alpha presence (with alpha first)"),
        ],
        default='NAME'
    )
    
    # UDIM settings
    udim_use_main_directory: BoolProperty(
        name="Use Main Directory",
        description="Create and search for UDIM textures in project root instead of AGR_BAKE folder",
        default=False
    )
    
    # Atlas settings
    atlas_size: EnumProperty(
        name="Atlas Size",
        description="Size of the atlas texture",
        items=[
            ('512', "512", "512x512"),
            ('1024', "1024", "1024x1024"),
            ('2048', "2048", "2048x2048"),
            ('4096', "4096", "4096x4096"),
            ('8192', "8192", "8192x8192"),
        ],
        default='2048'
    )


classes = (
    AGR_TextureSet,
    AGR_BakerSettings,
)


def register():
    """Register property classes"""
    for cls in classes:
        bpy.utils.register_class(cls)
    
    # Register collections and pointers
    bpy.types.Scene.agr_texture_sets = CollectionProperty(type=AGR_TextureSet)
    bpy.types.Scene.agr_baker_settings = PointerProperty(type=AGR_BakerSettings)
    
    print("✅ Properties registered")


def unregister():
    """Unregister property classes"""
    # Remove collections and pointers
    del bpy.types.Scene.agr_texture_sets
    del bpy.types.Scene.agr_baker_settings
    
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    
    print("Properties unregistered")
