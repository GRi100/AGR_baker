"""
Additional operators for texture set management
"""

import bpy
from bpy.types import Operator
from bpy.props import EnumProperty, IntProperty
import os
import re

from .core import texture_sets, materials
from .log import agr_report


def strip_useless_alpha_in_folders(folders):
    """Convert every RGBA PNG whose alpha channel is fully white to RGB.
    Returns the number of converted files. Requires Pillow (caller checks)."""
    from PIL import Image
    from .core.texture_sets import png_has_alpha

    converted = 0
    for folder in folders:
        if not folder or not os.path.isdir(folder):
            continue
        for fname in os.listdir(folder):
            if not fname.lower().endswith('.png'):
                continue
            path = os.path.join(folder, fname)
            if not png_has_alpha(path):
                continue
            try:
                with Image.open(path) as img:
                    if 'A' not in img.getbands():
                        continue
                    # getextrema is a C pass — no python-list materialization
                    if img.getchannel('A').getextrema()[0] < 254:
                        continue
                    rgb = img.convert('RGB')
                try:
                    rgb.save(path, 'PNG')
                    converted += 1
                    print(f"  🧹 {fname}: useless white alpha stripped (RGBA → RGB)")
                finally:
                    rgb.close()
            except Exception as e:
                print(f"  ⚠️ strip alpha failed for {fname}: {e}")
    return converted


class AGR_OT_RefreshTextureSets(Operator):
    """Refresh texture sets list from AGR_BAKE folder"""
    bl_idname = "agr.refresh_texture_sets"
    bl_label = "Refresh Texture Sets"
    bl_options = {'REGISTER'}

    def execute(self, context):
        # Thumbnails may reference replaced/deleted files after a rescan
        from . import ui
        ui.invalidate_set_thumbnails()

        count = texture_sets.refresh_texture_sets_list(context)

        # Optional auto-cleanup: RGBA files with a fully white alpha come
        # from external/legacy sources and only skew alpha detection
        settings = context.scene.agr_baker_settings
        if getattr(settings, 'auto_strip_alpha', False):
            try:
                from PIL import Image  # noqa: F401
            except ImportError:
                pass
            else:
                folders = [ts.folder_path for ts in context.scene.agr_texture_sets if ts.has_alpha]
                converted = strip_useless_alpha_in_folders(folders)
                if converted:
                    bpy.ops.agr.check_alpha_on_all_sets()
                    agr_report(self, 'INFO', f"Found {count} sets, stripped useless alpha in {converted} file(s)")
                    return {'FINISHED'}

        agr_report(self, 'INFO', f"Found {count} texture sets")
        return {'FINISHED'}


class AGR_OT_StripUselessAlpha(Operator):
    """Convert RGBA textures with a fully white alpha channel to RGB (smaller files, honest alpha detection)"""
    bl_idname = "agr.strip_useless_alpha"
    bl_label = "Strip Useless Alpha"
    # No UNDO: rewrites PNG files in place, Ctrl+Z cannot revert them
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        if not any(ts.is_selected for ts in context.scene.agr_texture_sets):
            cls.poll_message_set("Выберите текстурные сеты кликом по строкам списка")
            return False
        return True

    def execute(self, context):
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            self.report({'ERROR'}, "PIL/Pillow not available. Install with: pip install Pillow")
            return {'CANCELLED'}

        folders = [ts.folder_path for ts in context.scene.agr_texture_sets if ts.is_selected]
        converted = strip_useless_alpha_in_folders(folders)
        bpy.ops.agr.check_alpha_on_all_sets()
        agr_report(self, 'INFO', f"Converted {converted} file(s) RGBA → RGB")
        return {'FINISHED'}


class AGR_OT_ResizeTextureSet(Operator):
    """Create resized copies of selected texture sets (new S_*_<res>px folders, LANCZOS); originals stay untouched"""
    bl_idname = "agr.resize_texture_set"
    bl_label = "Resize Selected Sets"
    # No UNDO: writes new PNG folders on disk, Ctrl+Z cannot revert them
    bl_options = {'REGISTER'}

    target_resolution: EnumProperty(
        name="Target Resolution",
        description="Resolution of the resized copy",
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

        # Get selected sets (atlases excluded - resize only regular sets)
        selected_sets = [tex_set for tex_set in texture_sets_list
                         if tex_set.is_selected and not tex_set.is_atlas]

        if len(selected_sets) == 0:
            self.report({'WARNING'}, "No texture sets selected")
            return {'CANCELLED'}

        try:
            from PIL import Image
        except ImportError:
            self.report({'ERROR'}, "PIL/Pillow not available. Install with: pip install Pillow")
            return {'CANCELLED'}

        suffix = f"{target_res}px"

        processed_count = 0
        error_count = 0

        for tex_set in selected_sets:
            material_name = tex_set.material_name
            folder_path = tex_set.folder_path

            try:
                print(f"\n🔄 Resizing {material_name} → {target_res}px...")

                parent_folder = os.path.dirname(folder_path)
                new_folder_name = f"S_{material_name}_{suffix}"
                new_folder_path = os.path.join(parent_folder, new_folder_name)
                if not os.path.exists(new_folder_path):
                    os.makedirs(new_folder_path)

                texture_types = [
                    ('Diffuse', f"T_{material_name}_Diffuse.png"),
                    ('DiffuseOpacity', f"T_{material_name}_DiffuseOpacity.png"),
                    ('Roughness', f"T_{material_name}_Roughness.png"),
                    ('Metallic', f"T_{material_name}_Metallic.png"),
                    ('Emit', f"T_{material_name}_Emit.png"),
                    ('Opacity', f"T_{material_name}_Opacity.png"),
                    ('ERM', f"T_{material_name}_ERM.png"),
                    ('Normal', f"T_{material_name}_Normal.png"),
                ]

                resized_count = 0
                failed_count = 0

                for tex_type, filename in texture_types:
                    tex_path = os.path.join(folder_path, filename)

                    if os.path.exists(tex_path):
                        try:
                            with Image.open(tex_path) as img:
                                original_size = img.size
                                if img.size == (target_res, target_res):
                                    # Already at target - plain copy into the new set
                                    img_resized = img.copy()
                                else:
                                    img_resized = img.resize((target_res, target_res), Image.LANCZOS)

                            new_filename = f"T_{material_name}_{suffix}_{tex_type}.png"
                            output_path = os.path.join(new_folder_path, new_filename)
                            img_resized.save(output_path, 'PNG')
                            img_resized.close()
                            print(f"  📐 {tex_type}: {original_size[0]}px → {target_res}px")
                            resized_count += 1

                        except Exception as e:
                            print(f"  ⚠️ Error resizing {tex_type}: {e}")
                            failed_count += 1

                if failed_count > 0:
                    error_count += 1

                if resized_count > 0:
                    print(f"  ✅ Created {new_folder_name} with {resized_count} textures")
                    processed_count += 1
                else:
                    print(f"  ⚠️ No textures found to resize")
                    # Don't leave an empty S_*_<res>px folder behind
                    try:
                        os.rmdir(new_folder_path)
                    except OSError:
                        pass

            except Exception as e:
                print(f"  ❌ Error processing {material_name}: {e}")
                error_count += 1

        # Refresh texture sets list so new _<res>px sets appear
        texture_sets.refresh_texture_sets_list(context)

        if error_count > 0:
            self.report({'WARNING'}, f"Resized {processed_count} sets, {error_count} errors")
        else:
            self.report({'INFO'}, f"Created {processed_count} resized set(s) (_{suffix})")

        return {'FINISHED'}

    def invoke(self, context, event):
        selected_count = sum(1 for tex_set in context.scene.agr_texture_sets
                             if tex_set.is_selected and not tex_set.is_atlas)
        if selected_count == 0:
            self.report({'WARNING'}, "No sets selected")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        selected_count = sum(1 for tex_set in context.scene.agr_texture_sets
                             if tex_set.is_selected and not tex_set.is_atlas)

        layout.label(text=f"Resize {selected_count} texture set(s)", icon='IMAGE_DATA')
        layout.separator()
        layout.prop(self, "target_resolution")
        layout.separator()
        layout.label(text=f"• Creates new S_*_{self.target_resolution}px sets", icon='INFO')
        layout.label(text="• Originals are not modified")

class AGR_OT_ConnectSetToMaterial(Operator):
    """Connect selected texture sets to materials"""
    bl_idname = "agr.connect_set_to_material"
    bl_label = "Connect Selected to Materials"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        texture_sets_list = context.scene.agr_texture_sets
        selected_sets = [ts for ts in texture_sets_list if ts.is_selected]

        if not selected_sets:
            self.report({'WARNING'}, "No texture sets selected")
            return {'CANCELLED'}

        # Validate ALL sets have HIGH textures BEFORE any changes
        is_valid, error_msg = materials.validate_all_high_mode(selected_sets)
        if not is_valid:
            self.report({'ERROR'}, error_msg)
            return {'CANCELLED'}

        # All validated — now connect
        for tex_set in selected_sets:
            material_name = tex_set.material_name
            if material_name in bpy.data.materials:
                material = bpy.data.materials[material_name]
            else:
                material = bpy.data.materials.new(name=material_name)

            materials.connect_texture_set_to_material(material, tex_set.folder_path, material_name)
            tex_set.is_assigned = True

        self.report({'INFO'}, f"Connected {len(selected_sets)} sets to materials")
        return {'FINISHED'}


class AGR_OT_ConnectRegularSetToMaterial(Operator):
    """Connect selected texture sets to materials using regular (separate) textures"""
    bl_idname = "agr.connect_regular_set_to_material"
    bl_label = "Connect Regular Textures to Materials"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        texture_sets_list = context.scene.agr_texture_sets
        selected_sets = [ts for ts in texture_sets_list if ts.is_selected]

        if not selected_sets:
            self.report({'WARNING'}, "No texture sets selected")
            return {'CANCELLED'}

        # Validate ALL sets before making any changes
        errors = {}
        for tex_set in selected_sets:
            missing = materials.validate_regular_mode(tex_set.folder_path, tex_set.material_name)
            if missing:
                errors[tex_set.material_name] = missing

        if errors:
            names = ', '.join(f"{name} (no {', '.join(m)})" for name, m in errors.items())
            self.report({'ERROR'}, f"Missing regular textures: {names}")
            return {'CANCELLED'}

        # All validated — now connect
        for tex_set in selected_sets:
            material_name = tex_set.material_name
            if material_name in bpy.data.materials:
                material = bpy.data.materials[material_name]
            else:
                material = bpy.data.materials.new(name=material_name)

            materials.connect_regular_texture_set_to_material(material, tex_set.folder_path, material_name)
            tex_set.is_assigned = True

        self.report({'INFO'}, f"Connected {len(selected_sets)} sets with regular textures")
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

        # Validate ALL sets have HIGH textures BEFORE any changes
        is_valid, error_msg = materials.validate_all_high_mode(selected_sets)
        if not is_valid:
            self.report({'ERROR'}, error_msg)
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

            # Connect texture set (HIGH mode)
            materials.connect_texture_set_to_material(material, tex_set.folder_path, material_name)

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

        # Validate ALL sets with existing materials have HIGH textures
        sets_with_materials = [ts for ts in context.scene.agr_texture_sets
                               if ts.material_name in bpy.data.materials]
        if sets_with_materials:
            is_valid, error_msg = materials.validate_all_high_mode(sets_with_materials)
            if not is_valid:
                self.report({'ERROR'}, error_msg)
                return {'CANCELLED'}

        connected_count = 0

        # Connect each set to its material
        for tex_set in sets_with_materials:
            material_name = tex_set.material_name
            material = bpy.data.materials[material_name]

            # Connect texture set (HIGH mode)
            materials.connect_texture_set_to_material(material, tex_set.folder_path, material_name)

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
    """Toggle texture set selection (click on a row/card; selected entries are highlighted blue)"""
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
        # The clicked entry also becomes the active one
        context.scene.agr_texture_sets_index = self.set_index

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
        from .core.texture_sets import read_png_ihdr
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
                    _, _, color_type = read_png_ihdr(do_path)
                    if color_type < 0:
                        print(f"⚠️ {material_name}: Not a readable PNG file")
                        continue

                    # Color types with alpha: 4 (grayscale+alpha) or 6 (RGBA)
                    has_alpha = color_type in (4, 6)
                    tex_set.has_alpha = has_alpha
                    checked_count += 1
                    if has_alpha:
                        alpha_count += 1
                    print(f"✅ Checked {material_name}: alpha={has_alpha}, color_type={color_type}")

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
    # No UNDO: rewrites PNG files in place, Ctrl+Z cannot revert them
    bl_options = {'REGISTER'}
    
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
        
        # Validate ALL sets have HIGH textures BEFORE any blur
        is_valid, error_msg = materials.validate_all_high_mode(selected_sets)
        if not is_valid:
            self.report({'ERROR'}, error_msg)
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
                            with Image.open(tex_path) as img:
                                # Apply Gaussian blur
                                img_blurred = img.filter(ImageFilter.GaussianBlur(radius=self.blur_radius))

                            # Save blurred texture
                            img_blurred.save(tex_path, 'PNG')
                            img_blurred.close()
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
                        materials.connect_texture_set_to_material(material, folder_path, material_name)
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


class AGR_OT_MirrorTextureSet(Operator):
    """Create mirrored copies of selected texture sets (new S_*_mirrorX/_mirrorY folders)"""
    bl_idname = "agr.mirror_texture_set"
    bl_label = "Mirror Selected Sets"
    # No UNDO: writes new PNG folders on disk, Ctrl+Z cannot revert them
    bl_options = {'REGISTER'}

    mirror_axis: bpy.props.EnumProperty(
        name="Mirror Axis",
        description="Axis to mirror textures across",
        items=[
            ('X', "X (horizontal)", "Flip left-right, creates _mirrorX set"),
            ('Y', "Y (vertical)", "Flip top-bottom, creates _mirrorY set"),
        ],
        default='X'
    )

    def execute(self, context):
        texture_sets_list = context.scene.agr_texture_sets

        # Get selected sets (atlases excluded - mirror only regular sets)
        selected_sets = [tex_set for tex_set in texture_sets_list
                         if tex_set.is_selected and not tex_set.is_atlas]

        if len(selected_sets) == 0:
            self.report({'WARNING'}, "No texture sets selected")
            return {'CANCELLED'}

        try:
            from PIL import Image
        except ImportError:
            self.report({'ERROR'}, "PIL/Pillow not available. Install with: pip install Pillow")
            return {'CANCELLED'}

        suffix = f"mirror{self.mirror_axis}"
        flip_method = Image.FLIP_LEFT_RIGHT if self.mirror_axis == 'X' else Image.FLIP_TOP_BOTTOM
        # Flipping a normal map inverts one normal component:
        # X flip -> invert R channel, Y flip -> invert G channel (OpenGL)
        invert_channel = 0 if self.mirror_axis == 'X' else 1

        processed_count = 0
        error_count = 0

        for tex_set in selected_sets:
            material_name = tex_set.material_name
            folder_path = tex_set.folder_path

            try:
                print(f"\n🔄 Mirroring {material_name} ({self.mirror_axis})...")

                parent_folder = os.path.dirname(folder_path)
                new_folder_name = f"S_{material_name}_{suffix}"
                new_folder_path = os.path.join(parent_folder, new_folder_name)
                if not os.path.exists(new_folder_path):
                    os.makedirs(new_folder_path)

                texture_types = [
                    ('Diffuse', f"T_{material_name}_Diffuse.png"),
                    ('DiffuseOpacity', f"T_{material_name}_DiffuseOpacity.png"),
                    ('Roughness', f"T_{material_name}_Roughness.png"),
                    ('Metallic', f"T_{material_name}_Metallic.png"),
                    ('Emit', f"T_{material_name}_Emit.png"),
                    ('Opacity', f"T_{material_name}_Opacity.png"),
                    ('ERM', f"T_{material_name}_ERM.png"),
                    ('Normal', f"T_{material_name}_Normal.png"),
                ]

                mirrored_count = 0
                failed_count = 0

                for tex_type, filename in texture_types:
                    tex_path = os.path.join(folder_path, filename)

                    if os.path.exists(tex_path):
                        try:
                            with Image.open(tex_path) as img:
                                img_mirrored = img.transpose(flip_method)

                            if tex_type == 'Normal':
                                if img_mirrored.mode not in ('RGB', 'RGBA'):
                                    img_mirrored = img_mirrored.convert('RGB')
                                channels = list(img_mirrored.split())
                                channels[invert_channel] = channels[invert_channel].point(lambda v: 255 - v)
                                img_mirrored = Image.merge(img_mirrored.mode, channels)

                            new_filename = f"T_{material_name}_{suffix}_{tex_type}.png"
                            output_path = os.path.join(new_folder_path, new_filename)
                            img_mirrored.save(output_path, 'PNG')
                            img_mirrored.close()
                            print(f"  🪞 Mirrored {tex_type}")
                            mirrored_count += 1

                        except Exception as e:
                            print(f"  ⚠️ Error mirroring {tex_type}: {e}")
                            failed_count += 1

                if failed_count > 0:
                    error_count += 1

                if mirrored_count > 0:
                    print(f"  ✅ Created {new_folder_name} with {mirrored_count} textures")
                    processed_count += 1
                else:
                    print(f"  ⚠️ No textures found to mirror")
                    # Don't leave an empty S_*_mirror* folder behind
                    try:
                        os.rmdir(new_folder_path)
                    except OSError:
                        pass

            except Exception as e:
                print(f"  ❌ Error processing {material_name}: {e}")
                error_count += 1

        # Refresh texture sets list so new _mirror* sets appear
        texture_sets.refresh_texture_sets_list(context)

        if error_count > 0:
            self.report({'WARNING'}, f"Mirrored {processed_count} sets, {error_count} errors")
        else:
            self.report({'INFO'}, f"Created {processed_count} mirrored set(s) (_{suffix})")

        return {'FINISHED'}

    def invoke(self, context, event):
        selected_count = sum(1 for tex_set in context.scene.agr_texture_sets
                             if tex_set.is_selected and not tex_set.is_atlas)
        if selected_count == 0:
            self.report({'WARNING'}, "No sets selected")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        selected_count = sum(1 for tex_set in context.scene.agr_texture_sets
                             if tex_set.is_selected and not tex_set.is_atlas)

        layout.label(text=f"Mirror {selected_count} texture set(s)", icon='MOD_MIRROR')
        layout.separator()
        layout.prop(self, "mirror_axis", expand=True)
        layout.separator()
        layout.label(text="• Creates new S_*_mirrorX/_mirrorY sets", icon='INFO')
        layout.label(text="• Normal maps get channel inversion")


class AGR_OT_TileTextureSet(Operator):
    """Enlarge selected texture sets by tiling: repeat the texture N x N times into a new S_*_tileNx set (resolution grows N times)"""
    bl_idname = "agr.tile_texture_set"
    bl_label = "Tile Selected Sets"
    # No UNDO: writes new PNG folders on disk, Ctrl+Z cannot revert them
    bl_options = {'REGISTER'}

    # Blender's practical texture ceiling; tiling past it produces unusable PNGs
    MAX_RESOLUTION = 16384

    tile_factor: bpy.props.IntProperty(
        name="Multiplier",
        description="How many times to enlarge: N=2 places 2x2 copies (4 quadrants), N=3 places 3x3, etc.",
        default=2,
        min=2,
        max=8
    )

    def execute(self, context):
        texture_sets_list = context.scene.agr_texture_sets

        # Get selected sets (atlases excluded - tile only regular sets)
        selected_sets = [tex_set for tex_set in texture_sets_list
                         if tex_set.is_selected and not tex_set.is_atlas]

        if len(selected_sets) == 0:
            self.report({'WARNING'}, "No texture sets selected")
            return {'CANCELLED'}

        try:
            from PIL import Image
        except ImportError:
            self.report({'ERROR'}, "PIL/Pillow not available. Install with: pip install Pillow")
            return {'CANCELLED'}

        n = self.tile_factor
        suffix = f"tile{n}x"

        processed_count = 0
        error_count = 0
        skipped_big = 0

        for tex_set in selected_sets:
            material_name = tex_set.material_name
            folder_path = tex_set.folder_path

            try:
                print(f"\n🔄 Tiling {material_name} ({n}x{n})...")

                parent_folder = os.path.dirname(folder_path)
                new_folder_name = f"S_{material_name}_{suffix}"
                new_folder_path = os.path.join(parent_folder, new_folder_name)
                if not os.path.exists(new_folder_path):
                    os.makedirs(new_folder_path)

                texture_types = [
                    ('Diffuse', f"T_{material_name}_Diffuse.png"),
                    ('DiffuseOpacity', f"T_{material_name}_DiffuseOpacity.png"),
                    ('Roughness', f"T_{material_name}_Roughness.png"),
                    ('Metallic', f"T_{material_name}_Metallic.png"),
                    ('Emit', f"T_{material_name}_Emit.png"),
                    ('Opacity', f"T_{material_name}_Opacity.png"),
                    ('ERM', f"T_{material_name}_ERM.png"),
                    ('Normal', f"T_{material_name}_Normal.png"),
                ]

                tiled_count = 0
                failed_count = 0

                for tex_type, filename in texture_types:
                    tex_path = os.path.join(folder_path, filename)

                    if os.path.exists(tex_path):
                        try:
                            with Image.open(tex_path) as img:
                                # Indexed/exotic modes lose their palette when
                                # pasted into a fresh canvas - normalize first
                                if img.mode not in ('RGB', 'RGBA', 'L', 'LA'):
                                    img = img.convert('RGBA' if 'transparency' in img.info else 'RGB')

                                w, h = img.size
                                if w * n > self.MAX_RESOLUTION or h * n > self.MAX_RESOLUTION:
                                    print(f"  ⚠️ Skipped {tex_type}: {w * n}px exceeds {self.MAX_RESOLUTION}px limit")
                                    skipped_big += 1
                                    continue

                                img_tiled = Image.new(img.mode, (w * n, h * n))
                                for ty in range(n):
                                    for tx in range(n):
                                        img_tiled.paste(img, (tx * w, ty * h))

                            new_filename = f"T_{material_name}_{suffix}_{tex_type}.png"
                            output_path = os.path.join(new_folder_path, new_filename)
                            img_tiled.save(output_path, 'PNG')
                            img_tiled.close()
                            print(f"  🧩 Tiled {tex_type} ({w}px → {w * n}px)")
                            tiled_count += 1

                        except Exception as e:
                            print(f"  ⚠️ Error tiling {tex_type}: {e}")
                            failed_count += 1

                if failed_count > 0:
                    error_count += 1

                if tiled_count > 0:
                    print(f"  ✅ Created {new_folder_name} with {tiled_count} textures")
                    processed_count += 1
                else:
                    print(f"  ⚠️ No textures found to tile")
                    # Don't leave an empty S_*_tile* folder behind
                    try:
                        os.rmdir(new_folder_path)
                    except OSError:
                        pass

            except Exception as e:
                print(f"  ❌ Error processing {material_name}: {e}")
                error_count += 1

        # Refresh texture sets list so new _tile* sets appear
        texture_sets.refresh_texture_sets_list(context)

        if error_count > 0:
            self.report({'WARNING'}, f"Tiled {processed_count} sets, {error_count} errors")
        elif skipped_big > 0:
            self.report({'WARNING'}, f"Created {processed_count} tiled set(s), {skipped_big} textures over {self.MAX_RESOLUTION}px skipped")
        else:
            self.report({'INFO'}, f"Created {processed_count} tiled set(s) (_{suffix})")

        return {'FINISHED'}

    def invoke(self, context, event):
        selected_count = sum(1 for tex_set in context.scene.agr_texture_sets
                             if tex_set.is_selected and not tex_set.is_atlas)
        if selected_count == 0:
            self.report({'WARNING'}, "No sets selected")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        selected = [tex_set for tex_set in context.scene.agr_texture_sets
                    if tex_set.is_selected and not tex_set.is_atlas]

        layout.label(text=f"Tile {len(selected)} texture set(s)", icon='MOD_ARRAY')
        layout.separator()
        layout.prop(self, "tile_factor", slider=True)

        n = self.tile_factor
        layout.separator()
        layout.label(text=f"• Places {n}x{n} copies into new S_*_tile{n}x sets", icon='INFO')
        max_res = max((tex_set.resolution for tex_set in selected), default=0)
        if max_res:
            result = max_res * n
            if result > self.MAX_RESOLUTION:
                layout.label(text=f"• Largest set: {max_res}px → {result}px (over limit, will skip!)", icon='ERROR')
            else:
                layout.label(text=f"• Largest set: {max_res}px → {result}px")


def _average_color_of_image(path):
    """Alpha-weighted average RGB of an image file -> (r, g, b) ints, or None
    if the image is fully transparent / unreadable."""
    from PIL import Image
    import numpy as np

    try:
        with Image.open(path) as img:
            has_alpha = 'A' in img.getbands()
            img = img.convert('RGBA' if has_alpha else 'RGB')
            arr = np.asarray(img)
    except Exception as e:
        print(f"  ⚠️ Cannot read {os.path.basename(path)}: {e}")
        return None

    if has_alpha:
        # Weight RGB by alpha so transparent regions don't skew the color
        alpha = arr[..., 3].astype(np.float32) * (1.0 / 255.0)
        alpha_sum = alpha.sum(dtype=np.float64)
        if alpha_sum < 1e-6:
            return None
        return tuple(int(round((arr[..., c] * alpha).sum(dtype=np.float64) / alpha_sum))
                     for c in range(3))
    return tuple(int(round(arr[..., c].mean(dtype=np.float64))) for c in range(3))


class AGR_OT_CreateStubSet(Operator):
    """For each selected set create a 256px stub set S_*_stub: solid-color textures filled with the set's average DiffuseOpacity color"""
    bl_idname = "agr.create_stub_set"
    bl_label = "Create Stub Sets"
    # No UNDO: writes new PNG folders on disk, Ctrl+Z cannot revert them
    bl_options = {'REGISTER'}

    STUB_RESOLUTION = 256
    # Same neutral PBR defaults as UDIM tile replacement: E=0, R=204, M=0
    DEFAULT_ERM = (0, 204, 0)

    def execute(self, context):
        texture_sets_list = context.scene.agr_texture_sets

        # Get selected sets (atlases excluded - stub only regular sets)
        selected_sets = [tex_set for tex_set in texture_sets_list
                         if tex_set.is_selected and not tex_set.is_atlas]

        if len(selected_sets) == 0:
            self.report({'WARNING'}, "No texture sets selected")
            return {'CANCELLED'}

        try:
            from PIL import Image
        except ImportError:
            self.report({'ERROR'}, "PIL/Pillow not available. Install with: pip install Pillow")
            return {'CANCELLED'}

        res = (self.STUB_RESOLUTION, self.STUB_RESOLUTION)
        processed_count = 0
        error_count = 0

        for tex_set in selected_sets:
            material_name = tex_set.material_name
            folder_path = tex_set.folder_path

            try:
                print(f"\n🔄 Stub for {material_name}...")

                # Average DiffuseOpacity color of THIS set (alpha-weighted)
                color_path = None
                for fname in (f"T_{material_name}_DiffuseOpacity.png",
                              f"T_{material_name}_Diffuse.png"):
                    candidate = os.path.join(folder_path, fname)
                    if os.path.exists(candidate):
                        color_path = candidate
                        break
                if not color_path:
                    print(f"  ⚠️ No DiffuseOpacity/Diffuse found, skipped")
                    error_count += 1
                    continue

                diffuse_color = _average_color_of_image(color_path)
                if not diffuse_color:
                    print(f"  ⚠️ No usable pixels (fully transparent?), skipped")
                    error_count += 1
                    continue
                print(f"  🎨 Average color RGB{diffuse_color}")

                erm_color = None
                erm_path = os.path.join(folder_path, f"T_{material_name}_ERM.png")
                if os.path.exists(erm_path):
                    erm_color = _average_color_of_image(erm_path)
                if not erm_color:
                    erm_color = self.DEFAULT_ERM
                emit, rough, metal = erm_color

                new_name = f"{material_name}_stub"
                parent_folder = os.path.dirname(folder_path)
                new_folder_path = os.path.join(parent_folder, f"S_{new_name}")
                if not os.path.exists(new_folder_path):
                    os.makedirs(new_folder_path)

                # Full HIGH + LOW texture complement so the stub connects in both modes
                stub_textures = [
                    ('DiffuseOpacity', 'RGBA', diffuse_color + (255,)),
                    ('Diffuse', 'RGB', diffuse_color),
                    ('Opacity', 'RGB', (255, 255, 255)),
                    ('ERM', 'RGB', erm_color),
                    ('Emit', 'RGB', (emit, emit, emit)),
                    ('Roughness', 'RGB', (rough, rough, rough)),
                    ('Metallic', 'RGB', (metal, metal, metal)),
                    ('Normal', 'RGB', (128, 128, 255)),
                ]

                for tex_type, mode, color in stub_textures:
                    img = Image.new(mode, res, color)
                    img.save(os.path.join(new_folder_path, f"T_{new_name}_{tex_type}.png"), 'PNG')
                    img.close()

                print(f"  ✅ Created S_{new_name} ({len(stub_textures)} textures)")
                processed_count += 1

            except Exception as e:
                print(f"  ❌ Error processing {material_name}: {e}")
                error_count += 1

        # Refresh texture sets list so new _stub sets appear
        texture_sets.refresh_texture_sets_list(context)

        if error_count > 0:
            self.report({'WARNING'}, f"Created {processed_count} stub set(s), {error_count} skipped/errors")
        else:
            self.report({'INFO'}, f"Created {processed_count} stub set(s) (_stub)")

        return {'FINISHED'}


class AGR_OT_SelectMirroredSets(Operator):
    """Select all texture sets with _mirrorX/_mirrorY suffix"""
    bl_idname = "agr.select_mirrored_sets"
    bl_label = "Select Mirrored Sets"
    bl_options = {'REGISTER'}

    def execute(self, context):
        texture_sets_list = context.scene.agr_texture_sets
        selected_count = 0

        for tex_set in texture_sets_list:
            if tex_set.name.endswith("_mirrorX") or tex_set.name.endswith("_mirrorY"):
                tex_set.is_selected = True
                selected_count += 1
            else:
                tex_set.is_selected = False

        self.report({'INFO'}, f"Selected {selected_count} mirrored sets")
        return {'FINISHED'}


class AGR_OT_SelectStubSets(Operator):
    """Select all texture sets with _stub suffix"""
    bl_idname = "agr.select_stub_sets"
    bl_label = "Select Stub Sets"
    bl_options = {'REGISTER'}

    def execute(self, context):
        texture_sets_list = context.scene.agr_texture_sets
        selected_count = 0

        for tex_set in texture_sets_list:
            if tex_set.name.endswith("_stub"):
                tex_set.is_selected = True
                selected_count += 1
            else:
                tex_set.is_selected = False

        self.report({'INFO'}, f"Selected {selected_count} stub sets")
        return {'FINISHED'}


# Suffixes produced by the copying batch ops (Mirror / Tile / Stub / Resize).
# group(1) = base material name, group(2) = the derived suffix itself.
DERIVED_SET_SUFFIX_RE = re.compile(r'^(.+)_(mirror[XY]|tile\d+x|stub|\d+px)$')


class AGR_OT_SwapSetsOnObject(Operator):
    """Swap object materials to the checked derived sets (_mirrorX/_tileNx/_stub/_<res>px): a new material is built from each derived set and replaces its base material in the object's slots"""
    bl_idname = "agr.swap_sets_on_object"
    bl_label = "Swap Sets on Object"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        targets = [o for o in context.selected_objects if o.type == 'MESH']
        if not targets and not (context.active_object and context.active_object.type == 'MESH'):
            cls.poll_message_set("Выберите MESH-объект(ы)")
            return False
        return True

    def execute(self, context):
        texture_sets_list = context.scene.agr_texture_sets

        selected_sets = [tex_set for tex_set in texture_sets_list
                         if tex_set.is_selected and not tex_set.is_atlas]

        if len(selected_sets) == 0:
            self.report({'WARNING'}, "No texture sets selected")
            return {'CANCELLED'}

        targets = [o for o in context.selected_objects if o.type == 'MESH']
        if not targets and context.active_object and context.active_object.type == 'MESH':
            targets = [context.active_object]
        if not targets:
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}

        swapped_count = 0
        no_match_count = 0
        skipped_sets = []

        for tex_set in selected_sets:
            derived_name = tex_set.material_name

            m = DERIVED_SET_SUFFIX_RE.match(derived_name)
            if not m:
                skipped_sets.append(tex_set.name)
                print(f"⏭️ {tex_set.name}: not a derived set (_mirror*/_tile*/_stub/_*px), skipped")
                continue
            base_name = m.group(1)

            derived_material = None  # built lazily, only if a matching slot exists
            set_swapped = 0

            for obj in targets:
                for slot in obj.material_slots:
                    slot_mat = slot.material
                    if slot_mat is None:
                        continue
                    # Match base material exactly, tolerating Blender's .001 copies
                    slot_name = slot_mat.name
                    if slot_name != base_name and not (
                            slot_name.startswith(base_name + '.')
                            and slot_name[len(base_name) + 1:].isdigit()):
                        continue

                    if derived_material is None:
                        if derived_name in bpy.data.materials:
                            derived_material = bpy.data.materials[derived_name]
                        else:
                            # Copy keeps blend method / culling / extra nodes of the base
                            derived_material = slot_mat.copy()
                            derived_material.name = derived_name
                        materials.connect_best_texture_set_to_material(
                            derived_material, tex_set.folder_path, derived_name)

                    slot.material = derived_material
                    set_swapped += 1
                    print(f"🔁 {obj.name}: {slot_name} → {derived_name}")

            if set_swapped:
                tex_set.is_assigned = True
                swapped_count += set_swapped
            else:
                no_match_count += 1
                print(f"⚠️ {tex_set.name}: base material '{base_name}' not found on target object(s)")

        parts = [f"Swapped {swapped_count} slot(s)"]
        if no_match_count:
            parts.append(f"{no_match_count} set(s) had no matching material")
        if skipped_sets:
            parts.append(f"{len(skipped_sets)} not derived")
        level = 'INFO' if swapped_count else 'WARNING'
        self.report({level}, ", ".join(parts))
        return {'FINISHED'}


# List of all operator classes for registration
classes = (
    AGR_OT_RefreshTextureSets,
    AGR_OT_ResizeTextureSet,
    AGR_OT_ConnectSetToMaterial,
    AGR_OT_ConnectRegularSetToMaterial,
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
    AGR_OT_MirrorTextureSet,
    AGR_OT_TileTextureSet,
    AGR_OT_CreateStubSet,
    AGR_OT_SelectMirroredSets,
    AGR_OT_SelectStubSets,
    AGR_OT_SwapSetsOnObject,
    AGR_OT_StripUselessAlpha,
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
