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
            
            # Selection checkbox
            row.prop(tex_set, "is_selected", text="")
            
            # Set name with icon
            if tex_set.is_assigned:
                row.label(text=tex_set.name, icon='CHECKMARK')
            else:
                row.label(text=tex_set.name, icon='TEXTURE')
            
            # Resolution info
            row.label(text=f"{tex_set.resolution}px")
            
            # Alpha channel indicator
            if tex_set.has_alpha:
                row.label(text="", icon='IMAGE_ALPHA')
        
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
        
        # Render settings
        col.separator()
        col.label(text="Render Settings:", icon='RENDER_STILL')
        row = col.row(align=True)
        row.prop(settings, "bake_samples")
        row.prop(settings, "bake_device", text="")
        col.prop(settings, "bake_use_denoising")
        
        # Bake buttons
        box.separator()
        
        # Regular bake (selected to active)
        row = box.row()
        row.scale_y = 1.5
        row.operator("agr.bake_textures", text="Bake from High-Poly", icon='RENDER_STILL')
        
        # Simple bake (from material)
        row = box.row()
        row.scale_y = 1.3
        row.operator("agr.simple_bake", text="Simple Bake Active Material", icon='MATERIAL')
        
        # Simple bake all materials
        row = box.row()
        row.scale_y = 1.3
        row.operator("agr.simple_bake_all", text="Simple Bake All Materials", icon='MATERIAL_DATA')
        
        # Pillow installation check
        try:
            from PIL import Image
            pillow_available = True
        except ImportError:
            pillow_available = False
        
        if not pillow_available:
            box.separator()
            warning_box = box.box()
            warning_box.alert = True
            warning_box.label(text="⚠️ Pillow not installed", icon='ERROR')
            warning_box.label(text="Texture resizing unavailable")
            warning_box.operator("agr.install_pillow", text="Install Pillow", icon='IMPORT')
        
        # Info
        if context.active_object:
            box.label(text=f"Active: {context.active_object.name}", icon='OBJECT_DATA')
            if context.active_object.active_material:
                box.label(text=f"Material: {context.active_object.active_material.name}", icon='MATERIAL')
            mat_count = len([slot for slot in context.active_object.material_slots if slot.material])
            if mat_count > 0:
                box.label(text=f"Materials: {mat_count}", icon='MATERIAL_DATA')
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
        settings = context.scene.agr_baker_settings
        
        # Refresh button
        layout.operator("agr.refresh_texture_sets", text="Refresh Sets", icon='FILE_REFRESH')
        
        # Sort and selection controls
        sort_box = layout.box()
        sort_box.label(text="Sort & Select:", icon='SORTSIZE')
        
        # Sort buttons
        sort_row = sort_box.row(align=True)
        sort_row.label(text="Sort:")
        op = sort_row.operator("agr.sort_sets_by_name", text="Name", depress=(settings.sets_sort_mode == 'NAME'))
        op = sort_row.operator("agr.sort_sets_by_resolution", text="Res", depress=(settings.sets_sort_mode == 'RESOLUTION'))
        op = sort_row.operator("agr.sort_sets_by_alpha", text="Alpha", depress=(settings.sets_sort_mode == 'ALPHA'))
        
        # Selection buttons
        sel_row = sort_box.row(align=True)
        sel_row.label(text="Select:")
        op = sel_row.operator("agr.select_all_sets", text="All")
        op.action = 'SELECT'
        op = sel_row.operator("agr.select_all_sets", text="None")
        op.action = 'DESELECT'
        
        sel_row2 = sort_box.row(align=True)
        sel_row2.operator("agr.select_sets_with_alpha", text="With Alpha", icon='IMAGE_ALPHA')
        sel_row2.operator("agr.select_sets_by_resolution", text="By Res", icon='TEXTURE')
        
        sel_row3 = sort_box.row(align=True)
        sel_row3.operator("agr.select_sets_for_object", text="For Active", icon='OBJECT_DATA')
        sel_row3.operator("agr.select_set_for_active_material", text="For Active Mat", icon='MATERIAL')
        
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
            
            # Count selected
            selected_count = sum(1 for ts in context.scene.agr_texture_sets if ts.is_selected)
            if selected_count > 0:
                box.label(text=f"Selected: {selected_count}", icon='CHECKBOX_HLT')
            
            # Batch operations (always visible)
            batch_box = box.box()
            batch_box.label(text="Batch Operations:", icon='MODIFIER')
            
            # Resize and blur operations
            batch_box.operator("agr.resize_texture_set", text="Resize Selected Sets", icon='IMAGE_DATA')
            batch_box.operator("agr.gaussian_blur_set", text="Gaussian Blur on Selected", icon='BRUSH_DATA')
            
            batch_box.separator()
            
            # Delete texture types
            batch_row = batch_box.row(align=True)
            op = batch_row.operator("agr.delete_textures_from_selected", text="Del DO", icon='X')
            op.texture_type = 'DO'
            op = batch_row.operator("agr.delete_textures_from_selected", text="Del ERM", icon='X')
            op.texture_type = 'ERM'
            op = batch_row.operator("agr.delete_textures_from_selected", text="Del Normal", icon='X')
            op.texture_type = 'NORMAL'
            
            batch_box.separator()
            
            # Connect and assign operations
            batch_box.operator("agr.connect_set_to_material", text="Connect to Materials", icon='LINKED')
            
            if context.active_object and context.active_object.type == 'MESH':
                batch_box.operator("agr.assign_set_to_active", text="Assign to Active Object", icon='OBJECT_DATA')
            
            batch_box.separator()
            batch_box.operator("agr.delete_selected_sets", text="Delete Selected Sets", icon='TRASH')
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
