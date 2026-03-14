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
            
            # Set name with icon - different for atlases
            if tex_set.is_atlas:
                # Atlas icon
                row.label(text=tex_set.name, icon='IMAGE_PLANE')
            elif tex_set.is_assigned:
                row.label(text=tex_set.name, icon='CHECKMARK')
            else:
                row.label(text=tex_set.name, icon='TEXTURE')
            
            # Resolution info
            row.label(text=f"{tex_set.resolution}px")
            
            # Atlas type indicator
            if tex_set.is_atlas:
                row.label(text=tex_set.atlas_type, icon='UV')
            
            # Alpha channel indicator
            if tex_set.has_alpha:
                row.label(text="", icon='IMAGE_ALPHA')
        
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            if item.is_atlas:
                layout.label(text="", icon='IMAGE_PLANE')
            else:
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
        
        # Convert materials to sets
        box.separator()
        row = box.row()
        row.scale_y = 1.2
        row.operator("agr.convert_materials_to_sets", text="Convert Materials to Sets", icon='IMPORT')
        
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
        
        # Atlas Operations
        layout.separator()
        atlas_box = layout.box()
        atlas_box.label(text="Atlas Operations:", icon='IMAGE_PLANE')
        
        settings = context.scene.agr_baker_settings
        
        # Atlas settings
        atlas_box.prop(settings, "atlas_size", text="Atlas Size")
        
        # Count selected non-atlas sets
        selected_non_atlas = sum(1 for ts in context.scene.agr_texture_sets if ts.is_selected and not ts.is_atlas)
        
        if selected_non_atlas > 0:
            atlas_box.label(text=f"Selected: {selected_non_atlas} sets", icon='CHECKBOX_HLT')
            
            # Preview atlas layout
            atlas_box.operator("agr.preview_atlas_layout", text="Preview Atlas Layout", icon='HIDE_OFF')
            
            # Create atlas from selected sets (only textures, no UV, no material)
            op = atlas_box.operator("agr.create_atlas_only", text="Create Atlas Only", icon='IMAGE_PLANE')
            op.atlas_type = 'AUTO'
            
            # Info about active object
            if context.active_object:
                try:
                    from .operators_atlas import process_object_name
                    address, obj_type = process_object_name(context.active_object.name)
                    atlas_box.label(text=f"Active: {obj_type} ({address})", icon='OBJECT_DATA')
                except:
                    atlas_box.label(text=f"Active: {context.active_object.name}", icon='OBJECT_DATA')
        else:
            atlas_box.label(text="Select texture sets to create atlas", icon='INFO')
        
        # Create atlas from active object materials
        atlas_box.separator()
        if context.active_object and context.active_object.type == 'MESH' and len(context.active_object.material_slots) > 0:
            atlas_box.operator("agr.create_atlas_from_object", text="Create Atlas from Object", icon='OBJECT_DATA')
        else:
            row = atlas_box.row()
            row.enabled = False
            row.operator("agr.create_atlas_from_object", text="Create Atlas from Object", icon='OBJECT_DATA')
        
        # Apply atlas to object and preview
        atlas_box.separator()
        atlas_sets = [ts for ts in context.scene.agr_texture_sets if ts.is_atlas]
        if atlas_sets:
            atlas_box.label(text=f"Available Atlases: {len(atlas_sets)}", icon='IMAGE_DATA')
            
            if context.active_object and context.active_object.type == 'MESH':
                atlas_box.operator("agr.apply_atlas_to_object", text="Apply Atlas to Object", icon='UV')
            
            # Preview buttons for each atlas
            preview_box = atlas_box.box()
            preview_box.label(text="Preview:", icon='HIDE_OFF')
            for atlas in atlas_sets[:5]:  # Show max 5 atlases
                op = preview_box.operator("agr.preview_atlas", text=atlas.name, icon='IMAGE_PLANE')
                op.atlas_name = atlas.name
            
            if len(atlas_sets) > 5:
                preview_box.label(text=f"... and {len(atlas_sets) - 5} more", icon='THREE_DOTS')
        
        # UDIM Operations (always visible)
        layout.separator()
        udim_box = layout.box()
        udim_box.label(text="UDIM Operations:", icon='UV')
        
        # UDIM settings
        settings = context.scene.agr_baker_settings
        udim_box.prop(settings, "udim_use_main_directory", text="Use Main Directory")
        
        udim_box.separator()
        
        obj = context.active_object
        if obj and obj.type == 'MESH' and obj.name.startswith("SM_"):
            # Check if has UDIM
            has_udim = False
            for slot in obj.material_slots:
                if slot.material and slot.material.use_nodes:
                    for node in slot.material.node_tree.nodes:
                        if node.type == 'TEX_IMAGE' and node.image:
                            if node.image.source == 'TILED':
                                has_udim = True
                                break
                if has_udim:
                    break
            
            if has_udim:
                udim_box.operator("agr.revert_udim", text="Disassemble UDIM", icon='LOOP_BACK')
            else:
                udim_box.operator("agr.create_udim", text="Create UDIM Set", icon='UV_DATA')
        else:
            udim_box.label(text="Select SM_* object for UDIM", icon='INFO')
            # Show disabled buttons
            row = udim_box.row()
            row.enabled = False
            row.operator("agr.create_udim", text="Create UDIM Set", icon='UV_DATA')
            row = udim_box.row()
            row.enabled = False
            row.operator("agr.revert_udim", text="Disassemble UDIM", icon='LOOP_BACK')
        
        if len(context.scene.agr_texture_sets) == 0:
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
