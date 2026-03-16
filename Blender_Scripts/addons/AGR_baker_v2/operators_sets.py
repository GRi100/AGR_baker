"""
Additional operators for texture set management
"""

import bpy
from bpy.types import Operator
from bpy.props import EnumProperty, IntProperty
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


class AGR_OT_ResizeTextureSet(Operator):
    """Resize all textures in selected sets using LANCZOS algorithm"""
    bl_idname = "agr.resize_texture_set"
    bl_label = "Resize Texture Set"
    bl_options = {'REGISTER', 'UNDO'}
    
    target_resolution: EnumProperty(
        name="Target Resolution",
        description="Resolution to resize textures to",
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
    
    def execute(self, context):
        texture_sets_list = context.scene.agr_texture_sets
        target_res = int(self.target_resolution)
        
        # Get selected sets
        selected_sets = [tex_set for tex_set in texture_sets_list if tex_set.is_selected]
        
        if len(selected_sets) == 0:
            self.report({'WARNING'}, "No texture sets selected")
            return {'CANCELLED'}
        
        try:
            from PIL import Image
            has_pil = True
        except ImportError:
            self.report({'ERROR'}, "PIL/Pillow not available. Install with: pip install Pillow")
            return {'CANCELLED'}
        
        resized_count = 0
        error_count = 0
        
        for tex_set in selected_sets:
            material_name = tex_set.material_name
            folder_path = tex_set.folder_path
            
            print(f"\n🔄 Resizing texture set: S_{material_name} to {target_res}px")
            
            # List of texture types to resize
            texture_files = [
                f"T_{material_name}_Diffuse.png",
                f"T_{material_name}_DiffuseOpacity.png",
                f"T_{material_name}_Emit.png",
                f"T_{material_name}_Roughness.png",
                f"T_{material_name}_Metallic.png",
                f"T_{material_name}_Opacity.png",
                f"T_{material_name}_Normal.png",
                f"T_{material_name}_ERM.png",
            ]
            
            for filename in texture_files:
                filepath = os.path.join(folder_path, filename)
                
                if not os.path.exists(filepath):
                    continue
                
                try:
                    # Load image
                    img = Image.open(filepath)
                    original_size = img.size
                    
                    # Skip if already at target resolution
                    if img.width == target_res and img.height == target_res:
                        print(f"  ⏭️ {filename}: already {target_res}px")
                        continue
                    
                    # Resize using LANCZOS
                    img_resized = img.resize((target_res, target_res), Image.LANCZOS)
                    
                    # Save back to same file
                    img_resized.save(filepath, 'PNG')
                    
                    print(f"  ✅ {filename}: {original_size[0]}px → {target_res}px")
                    resized_count += 1
                    
                except Exception as e:
                    print(f"  ❌ Error resizing {filename}: {e}")
                    error_count += 1
            
            # Update resolution in texture set
            tex_set.resolution = target_res
            
            # Reconnect textures to material
            material_name = tex_set.material_name
            if material_name in bpy.data.materials:
                material = bpy.data.materials[material_name]
                print(f"🔗 Reconnecting textures to material {material_name}...")
                materials.connect_texture_set_to_material(
                    material,
                    folder_path,
                    material_name
                )
        
        # Refresh texture sets list
        texture_sets.refresh_texture_sets_list(context)
        
        if error_count > 0:
            self.report({'WARNING'}, f"Resized {resized_count} textures, {error_count} errors")
        else:
            self.report({'INFO'}, f"Resized {resized_count} textures to {target_res}px and reconnected")
        
        return {'FINISHED'}
    
    def invoke(self, context, event):
        selected_count = sum(1 for tex_set in context.scene.agr_texture_sets if tex_set.is_selected)
        if selected_count == 0:
            self.report({'WARNING'}, "No sets selected")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self)
    
    def draw(self, context):
        layout = self.layout
        selected_count = sum(1 for tex_set in context.scene.agr_texture_sets if tex_set.is_selected)
        
        layout.label(text=f"Resize {selected_count} texture set(s)", icon='IMAGE_DATA')
        layout.separator()
        layout.prop(self, "target_resolution")
        layout.separator()
        layout.label(text="All textures will be resized using LANCZOS", icon='INFO')


class AGR_OT_ConnectSetToMaterial(Operator):
    """Connect selected texture sets to materials"""
    bl_idname = "agr.connect_set_to_material"
    bl_label = "Connect Selected to Materials"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        texture_sets_list = context.scene.agr_texture_sets
        
        # Get all selected sets
        selected_sets = [tex_set for tex_set in texture_sets_list if tex_set.is_selected]
        
        if len(selected_sets) == 0:
            self.report({'WARNING'}, "No texture sets selected")
            return {'CANCELLED'}
        
        connected_count = 0
        
        for tex_set in selected_sets:
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
            connected_count += 1
        
        self.report({'INFO'}, f"Connected {connected_count} sets to materials")
        return {'FINISHED'}


class AGR_OT_AssignSetToActiveObject(Operator):
    """Assign selected texture sets to active object's materials"""
    bl_idname = "agr.assign_set_to_active"
    bl_label = "Assign Selected to Active"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'
    
    def execute(self, context):
        texture_sets_list = context.scene.agr_texture_sets
        obj = context.active_object
        
        # Get all selected sets
        selected_sets = [tex_set for tex_set in texture_sets_list if tex_set.is_selected]
        
        if len(selected_sets) == 0:
            self.report({'WARNING'}, "No texture sets selected")
            return {'CANCELLED'}
        
        assigned_count = 0
        skipped_count = 0
        
        for tex_set in selected_sets:
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
            
            # Check if material already on object
            already_assigned = False
            for slot in obj.material_slots:
                if slot.material == material:
                    already_assigned = True
                    break
            
            if not already_assigned:
                # Assign to object - append as new material slot
                obj.data.materials.append(material)
                tex_set.is_assigned = True
                assigned_count += 1
            else:
                skipped_count += 1
                print(f"⏭️ Skipped {material_name} - already on object")
        
        if skipped_count > 0:
            self.report({'INFO'}, f"Assigned {assigned_count}, skipped {skipped_count} (already on object)")
        else:
            self.report({'INFO'}, f"Assigned {assigned_count} materials to {obj.name}")
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


class AGR_OT_DeleteSelectedSets(Operator):
    """Delete selected texture sets (remove materials and slots, keep files)"""
    bl_idname = "agr.delete_selected_sets"
    bl_label = "Delete Selected Sets"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        texture_sets_list = context.scene.agr_texture_sets
        
        # Collect sets to delete
        sets_to_delete = []
        for tex_set in texture_sets_list:
            if tex_set.is_selected:
                sets_to_delete.append(tex_set)
        
        if len(sets_to_delete) == 0:
            self.report({'WARNING'}, "No sets selected for deletion")
            return {'CANCELLED'}
        
        materials_deleted = 0
        slots_removed = 0
        
        for tex_set in sets_to_delete:
            material_name = tex_set.material_name
            
            # Remove material slots from all objects
            if material_name in bpy.data.materials:
                material = bpy.data.materials[material_name]
                
                # Find all objects using this material
                for obj in bpy.data.objects:
                    if obj.type == 'MESH':
                        slots_to_remove = []
                        for i, slot in enumerate(obj.material_slots):
                            if slot.material == material:
                                slots_to_remove.append(i)
                        
                        # Remove slots in reverse order using context override
                        for slot_idx in reversed(slots_to_remove):
                            obj.active_material_index = slot_idx
                            with context.temp_override(object=obj):
                                bpy.ops.object.material_slot_remove()
                            slots_removed += 1
                            print(f"🗑️ Removed material slot from {obj.name}")
                
                # Delete material from scene
                bpy.data.materials.remove(material)
                materials_deleted += 1
                print(f"🗑️ Deleted material: {material_name}")
        
        # Refresh list
        texture_sets.refresh_texture_sets_list(context)
        
        self.report({'INFO'}, f"Removed {materials_deleted} materials and {slots_removed} slots (files kept)")
        return {'FINISHED'}
    
    def invoke(self, context, event):
        # Count selected
        selected_count = sum(1 for tex_set in context.scene.agr_texture_sets if tex_set.is_selected)
        if selected_count == 0:
            self.report({'WARNING'}, "No sets selected")
            return {'CANCELLED'}
        return context.window_manager.invoke_confirm(self, event)


class AGR_OT_ToggleSetSelection(Operator):
    """Toggle texture set selection"""
    bl_idname = "agr.toggle_set_selection"
    bl_label = "Toggle Selection"
    bl_options = {'REGISTER'}
    
    set_index: bpy.props.IntProperty()
    
    def execute(self, context):
        texture_sets_list = context.scene.agr_texture_sets
        
        if self.set_index < 0 or self.set_index >= len(texture_sets_list):
            return {'CANCELLED'}
        
        tex_set = texture_sets_list[self.set_index]
        tex_set.is_selected = not tex_set.is_selected
        
        return {'FINISHED'}


class AGR_OT_SelectAllSets(Operator):
    """Select or deselect all texture sets"""
    bl_idname = "agr.select_all_sets"
    bl_label = "Select All"
    bl_options = {'REGISTER'}
    
    action: bpy.props.EnumProperty(
        items=[
            ('SELECT', "Select", "Select all"),
            ('DESELECT', "Deselect", "Deselect all"),
            ('TOGGLE', "Toggle", "Toggle selection"),
        ],
        default='TOGGLE'
    )
    
    def execute(self, context):
        texture_sets_list = context.scene.agr_texture_sets
        
        if self.action == 'SELECT':
            for tex_set in texture_sets_list:
                tex_set.is_selected = True
        elif self.action == 'DESELECT':
            for tex_set in texture_sets_list:
                tex_set.is_selected = False
        elif self.action == 'TOGGLE':
            # If any selected, deselect all; otherwise select all
            any_selected = any(tex_set.is_selected for tex_set in texture_sets_list)
            for tex_set in texture_sets_list:
                tex_set.is_selected = not any_selected
        
        return {'FINISHED'}


class AGR_OT_DeleteTexturesFromSelected(Operator):
    """Remove texture nodes from selected sets (files remain on disk)"""
    bl_idname = "agr.delete_textures_from_selected"
    bl_label = "Remove Texture Nodes"
    bl_options = {'REGISTER', 'UNDO'}
    
    texture_type: bpy.props.EnumProperty(
        items=[
            ('DO', "DiffuseOpacity", "Remove DiffuseOpacity nodes"),
            ('ERM', "ERM", "Remove ERM nodes"),
            ('NORMAL', "Normal", "Remove Normal nodes"),
        ]
    )
    
    def execute(self, context):
        texture_sets_list = context.scene.agr_texture_sets
        
        # Collect selected sets
        selected_sets = [tex_set for tex_set in texture_sets_list if tex_set.is_selected]
        
        if len(selected_sets) == 0:
            self.report({'WARNING'}, "No sets selected")
            return {'CANCELLED'}
        
        nodes_removed = 0
        
        for tex_set in selected_sets:
            material_name = tex_set.material_name
            
            # Remove nodes from material
            if material_name in bpy.data.materials:
                material = bpy.data.materials[material_name]
                if material.use_nodes:
                    nodes_to_remove = []
                    
                    if self.texture_type == 'DO':
                        # Remove DiffuseOpacity texture node
                        for node in material.node_tree.nodes:
                            if node.type == 'TEX_IMAGE' and node.image:
                                if f"T_{material_name}_DiffuseOpacity" in node.image.name:
                                    nodes_to_remove.append(node)
                    
                    elif self.texture_type == 'ERM':
                        # Remove ERM texture node and SeparateColor node
                        for node in material.node_tree.nodes:
                            if node.type == 'TEX_IMAGE' and node.image:
                                if f"T_{material_name}_ERM" in node.image.name:
                                    nodes_to_remove.append(node)
                            elif node.type == 'SEPARATE_COLOR':
                                # Check if connected to ERM texture
                                for link in material.node_tree.links:
                                    if link.to_node == node:
                                        if link.from_node.type == 'TEX_IMAGE' and link.from_node.image:
                                            if f"T_{material_name}_ERM" in link.from_node.image.name:
                                                nodes_to_remove.append(node)
                                                break
                    
                    elif self.texture_type == 'NORMAL':
                        # Remove Normal texture node and NormalMap node
                        for node in material.node_tree.nodes:
                            if node.type == 'TEX_IMAGE' and node.image:
                                if f"T_{material_name}_Normal" in node.image.name:
                                    nodes_to_remove.append(node)
                            elif node.type == 'NORMAL_MAP':
                                # Check if connected to Normal texture
                                for link in material.node_tree.links:
                                    if link.to_node == node:
                                        if link.from_node.type == 'TEX_IMAGE' and link.from_node.image:
                                            if f"T_{material_name}_Normal" in link.from_node.image.name:
                                                nodes_to_remove.append(node)
                                                break
                    
                    for node in nodes_to_remove:
                        node_name = node.name  # Save name before removal
                        material.node_tree.nodes.remove(node)
                        nodes_removed += 1
                        print(f"🗑️ Removed node {node_name} from material {material_name}")
        
        self.report({'INFO'}, f"Removed {nodes_removed} nodes (files kept on disk)")
        return {'FINISHED'}
    
    def invoke(self, context, event):
        selected_count = sum(1 for tex_set in context.scene.agr_texture_sets if tex_set.is_selected)
        if selected_count == 0:
            self.report({'WARNING'}, "No sets selected")
            return {'CANCELLED'}
        return context.window_manager.invoke_confirm(self, event)


class AGR_OT_CheckAlphaOnAllSets(Operator):
    """Check alpha channel on all texture sets"""
    bl_idname = "agr.check_alpha_on_all_sets"
    bl_label = "Check Alpha on All Sets"
    bl_options = {'REGISTER'}
    
    def execute(self, context):
        import struct
        texture_sets_list = context.scene.agr_texture_sets
        
        checked_count = 0
        alpha_count = 0
        
        for tex_set in texture_sets_list:
            # Default to False
            tex_set.has_alpha = False
            
            if tex_set.has_diffuse_opacity:
                material_name = tex_set.material_name
                folder_path = tex_set.folder_path
                do_path = os.path.join(folder_path, f"T_{material_name}_DiffuseOpacity.png")
                
                if os.path.exists(do_path):
                    try:
                        # Check PNG format by reading file header
                        with open(do_path, 'rb') as f:
                            # Read PNG signature
                            signature = f.read(8)
                            if signature != b'\x89PNG\r\n\x1a\n':
                                print(f"⚠️ {material_name}: Not a valid PNG file")
                                continue
                            
                            # Read IHDR chunk (should be first chunk)
                            chunk_length_bytes = f.read(4)
                            if len(chunk_length_bytes) < 4:
                                print(f"⚠️ {material_name}: Cannot read chunk length")
                                continue
                            
                            chunk_type = f.read(4)
                            if chunk_type != b'IHDR':
                                print(f"⚠️ {material_name}: IHDR chunk not found")
                                continue
                            
                            # Read IHDR data (13 bytes)
                            ihdr_data = f.read(13)
                            if len(ihdr_data) < 13:
                                print(f"⚠️ {material_name}: Cannot read IHDR data")
                                continue
                            
                            color_type = ihdr_data[9]
                            
                            # Color types with alpha: 4 (grayscale+alpha) or 6 (RGBA)
                            has_alpha = color_type in (4, 6)
                            tex_set.has_alpha = has_alpha
                            checked_count += 1
                            if has_alpha:
                                alpha_count += 1
                            print(f"✅ Checked {material_name}: alpha={has_alpha}, color_type={color_type}")
                        
                    except Exception as e:
                        print(f"❌ Error checking {do_path}: {e}")
                        import traceback
                        traceback.print_exc()
        
        self.report({'INFO'}, f"Checked {checked_count} sets, {alpha_count} have alpha")
        return {'FINISHED'}


class AGR_OT_SelectSetsWithAlpha(Operator):
    """Select all texture sets with alpha channel"""
    bl_idname = "agr.select_sets_with_alpha"
    bl_label = "Select Sets with Alpha"
    bl_options = {'REGISTER'}
    
    def execute(self, context):
        texture_sets_list = context.scene.agr_texture_sets
        selected_count = 0
        
        for tex_set in texture_sets_list:
            if tex_set.has_alpha:
                tex_set.is_selected = True
                selected_count += 1
            else:
                tex_set.is_selected = False
        
        self.report({'INFO'}, f"Selected {selected_count} sets with alpha")
        return {'FINISHED'}


class AGR_OT_SelectSetsWithFrame(Operator):
    """Select all texture sets with _Frame suffix"""
    bl_idname = "agr.select_sets_with_frame"
    bl_label = "Select Sets with Frame"
    bl_options = {'REGISTER'}

    def execute(self, context):
        texture_sets_list = context.scene.agr_texture_sets
        selected_count = 0

        for tex_set in texture_sets_list:
            if tex_set.name.endswith("_Frame"):
                tex_set.is_selected = True
                selected_count += 1
            else:
                tex_set.is_selected = False

        self.report({'INFO'}, f"Selected {selected_count} sets with _Frame suffix")
        return {'FINISHED'}


class AGR_OT_SelectSetsForObject(Operator):
    """Select texture sets matching active object's materials"""
    bl_idname = "agr.select_sets_for_object"
    bl_label = "Select Sets for Object"
    bl_options = {'REGISTER'}
    
    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'
    
    def execute(self, context):
        texture_sets_list = context.scene.agr_texture_sets
        obj = context.active_object
        
        # Get material names from object
        material_names = set()
        for slot in obj.material_slots:
            if slot.material:
                material_names.add(slot.material.name)
        
        if len(material_names) == 0:
            self.report({'WARNING'}, "Active object has no materials")
            return {'CANCELLED'}
        
        selected_count = 0
        
        for tex_set in texture_sets_list:
            if tex_set.material_name in material_names:
                tex_set.is_selected = True
                selected_count += 1
            else:
                tex_set.is_selected = False
        
        self.report({'INFO'}, f"Selected {selected_count} sets for {obj.name}")
        return {'FINISHED'}


class AGR_OT_SelectSetForActiveMaterial(Operator):
    """Select texture set matching active material on active object"""
    bl_idname = "agr.select_set_for_active_material"
    bl_label = "Select Set for Active Material"
    bl_options = {'REGISTER'}
    
    @classmethod
    def poll(cls, context):
        return (context.active_object and
                context.active_object.type == 'MESH' and
                context.active_object.active_material)
    
    def execute(self, context):
        texture_sets_list = context.scene.agr_texture_sets
        obj = context.active_object
        active_mat = obj.active_material
        
        if not active_mat:
            self.report({'WARNING'}, "No active material")
            return {'CANCELLED'}
        
        selected_count = 0
        
        for tex_set in texture_sets_list:
            if tex_set.material_name == active_mat.name:
                tex_set.is_selected = True
                selected_count += 1
            else:
                tex_set.is_selected = False
        
        if selected_count > 0:
            self.report({'INFO'}, f"Selected set for material: {active_mat.name}")
        else:
            self.report({'WARNING'}, f"No set found for material: {active_mat.name}")
        
        return {'FINISHED'}


class AGR_OT_SelectSetsByResolution(Operator):
    """Select texture sets by resolution"""
    bl_idname = "agr.select_sets_by_resolution"
    bl_label = "Select Sets by Resolution"
    bl_options = {'REGISTER'}
    
    resolution: EnumProperty(
        name="Resolution",
        description="Select sets with this resolution",
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
    
    def execute(self, context):
        texture_sets_list = context.scene.agr_texture_sets
        target_res = int(self.resolution)
        
        selected_count = 0
        
        for tex_set in texture_sets_list:
            if tex_set.resolution == target_res:
                tex_set.is_selected = True
                selected_count += 1
            else:
                tex_set.is_selected = False
        
        self.report({'INFO'}, f"Selected {selected_count} sets with {target_res}px resolution")
        return {'FINISHED'}
    
    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)
    
    def draw(self, context):
        layout = self.layout
        layout.prop(self, "resolution")


class AGR_OT_SortSetsByName(Operator):
    """Sort texture sets by name (alphabetically)"""
    bl_idname = "agr.sort_sets_by_name"
    bl_label = "Sort by Name"
    bl_options = {'REGISTER'}
    
    def execute(self, context):
        settings = context.scene.agr_baker_settings
        settings.sets_sort_mode = 'NAME'
        # Fast in-place sort: do NOT rescan/recompute anything here.
        texture_sets.sort_texture_sets_in_scene(context, 'NAME')
        return {'FINISHED'}


class AGR_OT_SortSetsByResolution(Operator):
    """Sort texture sets by resolution (high to low)"""
    bl_idname = "agr.sort_sets_by_resolution"
    bl_label = "Sort by Resolution"
    bl_options = {'REGISTER'}
    
    def execute(self, context):
        settings = context.scene.agr_baker_settings
        settings.sets_sort_mode = 'RESOLUTION'
        # Fast in-place sort: do NOT rescan/recompute anything here.
        texture_sets.sort_texture_sets_in_scene(context, 'RESOLUTION')
        return {'FINISHED'}


class AGR_OT_SortSetsByAlpha(Operator):
    """Sort texture sets by alpha presence (with alpha first)"""
    bl_idname = "agr.sort_sets_by_alpha"
    bl_label = "Sort by Alpha"
    bl_options = {'REGISTER'}
    
    def execute(self, context):
        settings = context.scene.agr_baker_settings
        settings.sets_sort_mode = 'ALPHA'
        # Fast in-place sort: do NOT rescan/recompute anything here.
        texture_sets.sort_texture_sets_in_scene(context, 'ALPHA')
        return {'FINISHED'}


class AGR_OT_GaussianBlurSet(Operator):
    """Apply Gaussian blur to all textures in selected sets"""
    bl_idname = "agr.gaussian_blur_set"
    bl_label = "Gaussian Blur on Selected Sets"
    bl_options = {'REGISTER', 'UNDO'}
    
    blur_radius: bpy.props.FloatProperty(
        name="Blur Radius (px)",
        description="Gaussian blur radius in pixels (like Photoshop)",
        default=2.0,
        min=0.1,
        max=100.0
    )
    
    def execute(self, context):
        texture_sets_list = context.scene.agr_texture_sets
        
        # Get selected sets
        selected_sets = [tex_set for tex_set in texture_sets_list if tex_set.is_selected]
        
        if len(selected_sets) == 0:
            self.report({'WARNING'}, "No texture sets selected")
            return {'CANCELLED'}
        
        try:
            from PIL import Image, ImageFilter
            has_pil = True
        except ImportError:
            self.report({'ERROR'}, "PIL/Pillow not available. Install with: pip install Pillow")
            return {'CANCELLED'}
        
        processed_count = 0
        error_count = 0
        
        for tex_set in selected_sets:
            material_name = tex_set.material_name
            folder_path = tex_set.folder_path
            
            try:
                print(f"\n🔄 Processing {material_name}...")
                
                # Define texture types to blur (exclude Normal - blurring normals causes issues)
                texture_types = [
                    ('Diffuse', f"T_{material_name}_Diffuse.png"),
                    ('DiffuseOpacity', f"T_{material_name}_DiffuseOpacity.png"),
                    ('Roughness', f"T_{material_name}_Roughness.png"),
                    ('Metallic', f"T_{material_name}_Metallic.png"),
                    ('Emit', f"T_{material_name}_Emit.png"),
                    ('Opacity', f"T_{material_name}_Opacity.png"),
                    ('ERM', f"T_{material_name}_ERM.png"),
                ]
                
                blurred_count = 0
                
                for tex_type, filename in texture_types:
                    tex_path = os.path.join(folder_path, filename)
                    
                    if os.path.exists(tex_path):
                        try:
                            # Load texture
                            img = Image.open(tex_path)
                            
                            # Apply Gaussian blur
                            img_blurred = img.filter(ImageFilter.GaussianBlur(radius=self.blur_radius))
                            
                            # Save blurred texture
                            img_blurred.save(tex_path, 'PNG')
                            print(f"  🌀 Blurred {tex_type}")
                            blurred_count += 1
                            
                        except Exception as e:
                            print(f"  ⚠️ Error blurring {tex_type}: {e}")
                
                if blurred_count > 0:
                    print(f"  ✅ Blurred {blurred_count} textures")
                    
                    # Reconnect textures to material if it exists
                    if material_name in bpy.data.materials:
                        material = bpy.data.materials[material_name]
                        print(f"  🔗 Reconnecting textures to material...")
                        
                        materials.connect_texture_set_to_material(
                            material,
                            folder_path,
                            material_name
                        )
                        print(f"  ✅ Reconnected textures to material")
                    
                    processed_count += 1
                else:
                    print(f"  ⚠️ No textures found to blur")
                
            except Exception as e:
                print(f"  ❌ Error processing {material_name}: {e}")
                error_count += 1
        
        # Refresh texture sets list
        texture_sets.refresh_texture_sets_list(context)
        
        if error_count > 0:
            self.report({'WARNING'}, f"Processed {processed_count} sets, {error_count} errors")
        else:
            self.report({'INFO'}, f"Applied Gaussian blur to {processed_count} sets")
        
        return {'FINISHED'}
    
    def invoke(self, context, event):
        selected_count = sum(1 for tex_set in context.scene.agr_texture_sets if tex_set.is_selected)
        if selected_count == 0:
            self.report({'WARNING'}, "No sets selected")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self)
    
    def draw(self, context):
        layout = self.layout
        selected_count = sum(1 for tex_set in context.scene.agr_texture_sets if tex_set.is_selected)
        
        layout.label(text=f"Blur {selected_count} texture set(s)", icon='BRUSH_DATA')
        layout.separator()
        layout.prop(self, "blur_radius")
        layout.separator()
        layout.label(text="• Blurs all textures (except Normal)", icon='INFO')
        layout.label(text="• Reconnects textures to material")


# List of all operator classes for registration
classes = (
    AGR_OT_RefreshTextureSets,
    AGR_OT_ResizeTextureSet,
    AGR_OT_ConnectSetToMaterial,
    AGR_OT_AssignSetToActiveObject,
    AGR_OT_LoadSetsFromFolder,
    AGR_OT_DeleteSelectedSets,
    AGR_OT_ToggleSetSelection,
    AGR_OT_SelectAllSets,
    AGR_OT_DeleteTexturesFromSelected,
    AGR_OT_CheckAlphaOnAllSets,
    AGR_OT_SelectSetsWithAlpha,
    AGR_OT_SelectSetsWithFrame,
    AGR_OT_SelectSetsForObject,
    AGR_OT_SelectSetForActiveMaterial,
    AGR_OT_SelectSetsByResolution,
    AGR_OT_SortSetsByName,
    AGR_OT_SortSetsByResolution,
    AGR_OT_SortSetsByAlpha,
    AGR_OT_GaussianBlurSet,
)


def register():
    """Register operator classes"""
    for cls in classes:
        bpy.utils.register_class(cls)
    print("✅ Texture set operators registered")


def unregister():
    """Unregister operator classes"""
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    print("Texture set operators unregistered")
