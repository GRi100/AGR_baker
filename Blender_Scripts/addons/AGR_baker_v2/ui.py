"""
UI panels for AGR Baker v2
"""

import bpy
from bpy.types import Panel, UIList


class AGR_UL_TextureSetsList(UIList):
    """UI List for texture sets"""
    
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        tex_set = item
        
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            
            # Set name with icon
            if tex_set.is_assigned:
                row.label(text=tex_set.name, icon='CHECKMARK')
            else:
                row.label(text=tex_set.name, icon='TEXTURE')
            
            # Resolution info
            row.label(text=f"{tex_set.resolution}px")
            
            # Texture flags
            sub = row.row(align=True)
            sub.scale_x = 0.5
            if tex_set.has_diffuse_opacity:
                sub.label(text="DO", icon='IMAGE_RGB_ALPHA')
            if tex_set.has_erm:
                sub.label(text="ERM", icon='NODE_COMPOSITING')
            if tex_set.has_normal:
                sub.label(text="N", icon='NORMALS_FACE')
        
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="", icon='TEXTURE')


class AGR_PT_MainPanel(Panel):
    """Main AGR Baker panel"""
    bl_label = "AGR Baker v2"
    bl_idname = "AGR_PT_main_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Baker'
    
    def draw(self, context):
        layout = self.layout
        settings = context.scene.agr_baker_settings
        
        # Header
        box = layout.box()
        box.label(text="Texture Baking", icon='RENDER_STILL')
        
        # Baking settings
        col = box.column(align=True)
        col.prop(settings, "resolution")
        col.prop(settings, "bake_with_alpha")
        col.prop(settings, "bake_normal_enabled")
        
        row = col.row(align=True)
        row.prop(settings, "max_ray_distance")
        row.prop(settings, "extrusion")
        
        # Bake button
        box.separator()
        row = box.row()
        row.scale_y = 1.5
        row.operator("agr.bake_textures", text="Bake Texture Set", icon='RENDER_STILL')
        
        # Info
        if context.active_object:
            box.label(text=f"Active: {context.active_object.name}", icon='OBJECT_DATA')
            if len(context.selected_objects) > 1:
                box.label(text=f"Sources: {len(context.selected_objects) - 1} objects", icon='OUTLINER_OB_MESH')


class AGR_PT_TextureSetsPanel(Panel):
    """Texture sets management panel"""
    bl_label = "Texture Sets"
    bl_idname = "AGR_PT_texture_sets_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Baker'
    bl_options = {'DEFAULT_CLOSED'}
    
    def draw(self, context):
        layout = self.layout
        
        # Refresh button
        row = layout.row()
        row.operator("agr.refresh_texture_sets", text="Refresh Sets", icon='FILE_REFRESH')
        row.operator("agr.load_sets_from_folder", text="Load All", icon='IMPORT')
        
        # Texture sets list
        box = layout.box()
        box.label(text=f"Available Sets ({len(context.scene.agr_texture_sets)}):", icon='TEXTURE')
        
        if len(context.scene.agr_texture_sets) > 0:
            box.template_list(
                "AGR_UL_TextureSetsList", "",
                context.scene, "agr_texture_sets",
                context.scene, "agr_texture_sets_index",
                rows=5
            )
            
            # Selected set operations
            if context.scene.agr_texture_sets_index >= 0 and context.scene.agr_texture_sets_index < len(context.scene.agr_texture_sets):
                tex_set = context.scene.agr_texture_sets[context.scene.agr_texture_sets_index]
                
                col = box.column(align=True)
                col.label(text=f"Set: S_{tex_set.material_name}", icon='INFO')
                col.label(text=f"Path: .../{tex_set.folder_path.split('/')[-1]}")
                
                # Texture info
                row = col.row(align=True)
                if tex_set.has_diffuse:
                    row.label(text="Diffuse", icon='CHECKMARK')
                if tex_set.has_diffuse_opacity:
                    row.label(text="DO", icon='CHECKMARK')
                if tex_set.has_erm:
                    row.label(text="ERM", icon='CHECKMARK')
                if tex_set.has_normal:
                    row.label(text="Normal", icon='CHECKMARK')
                
                # Operations
                col.separator()
                op_row = col.row(align=True)
                op = op_row.operator("agr.connect_set_to_material", text="Connect to Material", icon='LINKED')
                op.set_index = context.scene.agr_texture_sets_index
                
                op = op_row.operator("agr.assign_set_to_active", text="Assign to Active", icon='OBJECT_DATA')
                op.set_index = context.scene.agr_texture_sets_index
        else:
            box.label(text="No texture sets found", icon='INFO')
            box.label(text="Bake textures or refresh list")


class AGR_PT_PhotoshopPanel(Panel):
    """Photoshop integration panel"""
    bl_label = "Photoshop Integration"
    bl_idname = "AGR_PT_photoshop_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Baker'
    bl_options = {'DEFAULT_CLOSED'}
    
    def draw(self, context):
        layout = self.layout
        settings = context.scene.agr_baker_settings
        
        box = layout.box()
        box.prop(settings, "photoshop_enabled", text="Enable Photoshop")
        
        if settings.photoshop_enabled:
            box.prop(settings, "photoshop_path", text="")
            
            box.separator()
            box.label(text="Features:", icon='INFO')
            col = box.column(align=True)
            col.label(text="• Texture resizing")
            col.label(text="• Batch processing")
            col.label(text="• Advanced filters")
            
            box.separator()
            box.operator("agr.open_photoshop_settings", text="Settings", icon='PREFERENCES')
        else:
            box.label(text="Photoshop integration disabled", icon='INFO')


class AGR_PT_SettingsPanel(Panel):
    """Settings panel"""
    bl_label = "Settings"
    bl_idname = "AGR_PT_settings_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Baker'
    bl_options = {'DEFAULT_CLOSED'}
    
    def draw(self, context):
        layout = self.layout
        settings = context.scene.agr_baker_settings
        
        box = layout.box()
        box.label(text="Output Settings", icon='FILE_FOLDER')
        box.prop(settings, "output_folder", text="Folder Name")
        
        # Show full path
        if bpy.path.abspath("//"):
            import os
            full_path = os.path.join(bpy.path.abspath("//"), settings.output_folder)
            box.label(text=f"Path: {full_path}", icon='INFO')
        else:
            box.label(text="Save blend file to see path", icon='ERROR')


classes = (
    AGR_UL_TextureSetsList,
    AGR_PT_MainPanel,
    AGR_PT_TextureSetsPanel,
    AGR_PT_PhotoshopPanel,
    AGR_PT_SettingsPanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    
    # Register index property for UI list
    bpy.types.Scene.agr_texture_sets_index = bpy.props.IntProperty(default=0)
    
    print("✅ UI registered")


def unregister():
    del bpy.types.Scene.agr_texture_sets_index
    
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
