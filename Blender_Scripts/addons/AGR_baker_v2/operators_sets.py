"""
Additional operators for texture set management
"""

import bpy
from bpy.types import Operator
import os

from .core import texture_sets, materials


class AGR_OT_RefreshTextureSets(Operator):
    """Refresh texture sets list from AGR_BAKE folder"""
    bl_idname = "agr.refresh_texture_sets"
    bl_label = "Refresh Texture Sets"
    bl_options = {'REGISTER'}
    
    def execute(self, context):
        count = texture_sets.refresh_texture_sets_list(context)
        self.report({'INFO'}, f"Found {count} texture sets")
        return {'FINISHED'}


class AGR_OT_ConnectSetToMaterial(Operator):
    """Connect texture set to material"""
    bl_idname = "agr.connect_set_to_material"
    bl_label = "Connect Set to Material"
    bl_options = {'REGISTER', 'UNDO'}
    
    set_index: bpy.props.IntProperty()
    
    def execute(self, context):
        texture_sets_list = context.scene.agr_texture_sets
        
        if self.set_index < 0 or self.set_index >= len(texture_sets_list):
            self.report({'ERROR'}, "Invalid texture set index")
            return {'CANCELLED'}
        
        tex_set = texture_sets_list[self.set_index]
        material_name = tex_set.material_name
        
        # Find or create material
        if material_name in bpy.data.materials:
            material = bpy.data.materials[material_name]
        else:
            material = bpy.data.materials.new(name=material_name)
        
        # Connect texture set
        materials.connect_texture_set_to_material(
            material,
            tex_set.folder_path,
            material_name
        )
        
        # Update assignment flag
        tex_set.is_assigned = True
        
        self.report({'INFO'}, f"Connected S_{material_name} to material")
        return {'FINISHED'}


class AGR_OT_AssignSetToActiveObject(Operator):
    """Assign texture set to active object's material"""
    bl_idname = "agr.assign_set_to_active"
    bl_label = "Assign to Active Object"
    bl_options = {'REGISTER', 'UNDO'}
    
    set_index: bpy.props.IntProperty()
    
    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'
    
    def execute(self, context):
        texture_sets_list = context.scene.agr_texture_sets
        
        if self.set_index < 0 or self.set_index >= len(texture_sets_list):
            self.report({'ERROR'}, "Invalid texture set index")
            return {'CANCELLED'}
        
        tex_set = texture_sets_list[self.set_index]
        material_name = tex_set.material_name
        obj = context.active_object
        
        # Find or create material
        if material_name in bpy.data.materials:
            material = bpy.data.materials[material_name]
        else:
            material = bpy.data.materials.new(name=material_name)
            
        # Connect texture set
        materials.connect_texture_set_to_material(
            material,
            tex_set.folder_path,
            material_name
        )
        
        # Assign to object
        if len(obj.material_slots) == 0:
            obj.data.materials.append(material)
        else:
            obj.material_slots[0].material = material
        
        tex_set.is_assigned = True
        
        self.report({'INFO'}, f"Assigned S_{material_name} to {obj.name}")
        return {'FINISHED'}


class AGR_OT_LoadSetsFromFolder(Operator):
    """Load all texture sets from AGR_BAKE folder and connect to materials"""
    bl_idname = "agr.load_sets_from_folder"
    bl_label = "Load Sets from Folder"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        # Refresh sets list
        count = texture_sets.refresh_texture_sets_list(context)
        
        if count == 0:
            self.report({'WARNING'}, "No texture sets found in AGR_BAKE")
            return {'CANCELLED'}
        
        settings = context.scene.agr_baker_settings
        connected_count = 0
        
        # Connect each set to its material
        for tex_set in context.scene.agr_texture_sets:
            material_name = tex_set.material_name
            
            # Check if material exists
            if material_name in bpy.data.materials:
                material = bpy.data.materials[material_name]
                
                # Connect texture set
                materials.connect_texture_set_to_material(
                    material,
                    tex_set.folder_path,
                    material_name
                )
                
                tex_set.is_assigned = True
                connected_count += 1
                print(f"✅ Connected S_{material_name} to existing material")
        
        self.report({'INFO'}, f"Loaded {count} sets, connected {connected_count} to materials")
        return {'FINISHED'}


class AGR_OT_OpenPhotoshopSettings(Operator):
    """Open Photoshop integration settings"""
    bl_idname = "agr.open_photoshop_settings"
    bl_label = "Photoshop Settings"
    bl_options = {'REGISTER'}
    
    def execute(self, context):
        return context.window_manager.invoke_props_dialog(self, width=400)
    
    def draw(self, context):
        layout = self.layout
        settings = context.scene.agr_baker_settings
        
        layout.label(text="Photoshop Integration", icon='TEXTURE')
        layout.separator()
        
        layout.prop(settings, "photoshop_enabled")
        layout.prop(settings, "photoshop_path")
        
        layout.separator()
        layout.label(text="Photoshop will be used for:", icon='INFO')
        box = layout.box()
        box.label(text="• Texture resizing")
        box.label(text="• Advanced texture processing")
        box.label(text="• Batch operations")
    
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=400)


classes = (
    AGR_OT_RefreshTextureSets,
    AGR_OT_ConnectSetToMaterial,
    AGR_OT_AssignSetToActiveObject,
    AGR_OT_LoadSetsFromFolder,
    AGR_OT_OpenPhotoshopSettings,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
