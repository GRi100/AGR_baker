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
    """Texture set data (S_material_name)"""
    
    name: StringProperty(
        name="Set Name",
        description="Texture set name (S_material_name)",
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
    
    is_assigned: BoolProperty(
        name="Assigned",
        description="Set is assigned to a material",
        default=False
    )


class AGR_BakerSettings(PropertyGroup):
    """Main addon settings"""
    
    # Baking settings
    resolution: EnumProperty(
        name="Resolution",
        description="Texture resolution for baking",
        items=[
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
