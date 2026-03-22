"""
Frame creation operators for texture sets
"""

import bpy
from bpy.types import Operator
from bpy.props import EnumProperty, FloatProperty, StringProperty, CollectionProperty
import os
import shutil
from pathlib import Path

from .core import texture_sets


class AGR_OT_CreateFrameOnSets(Operator):
    """Create frame on selected texture sets"""
    bl_idname = "agr.create_frame_on_sets"
    bl_label = "Create Frame on Sets"
    bl_options = {'REGISTER', 'UNDO'}
    
    scale_factor: FloatProperty(
        name="Scale Factor",
        description="Scale factor for texture (0.90625 = 192px border on 4K)",
        default=0.90625,
        min=0.1,
        max=1.0
    )
    
    frame_overlay: EnumProperty(
        name="Frame Overlay",
        description="Choose frame overlay to apply",
        items=[
            ('NONE', "None", "No overlay, just create border"),
            ('COMPLEX', "Complex", "Apply complex frame overlay"),
            ('MINIMAL', "Minimal", "Apply minimal frame overlay"),
        ],
        default='NONE'
    )
    
    def execute(self, context):
        try:
            from PIL import Image
        except ImportError:
            self.report({'ERROR'}, "PIL/Pillow not available. Install with: pip install Pillow")
            return {'CANCELLED'}
        
        texture_sets_list = context.scene.agr_texture_sets
        
        # Get selected sets
        selected_sets = [tex_set for tex_set in texture_sets_list if tex_set.is_selected]
        
        if len(selected_sets) == 0:
            self.report({'WARNING'}, "No texture sets selected")
            return {'CANCELLED'}
        
        # Get overlay image path if needed
        overlay_path = None
        if self.frame_overlay != 'NONE':
            addon_dir = os.path.dirname(os.path.abspath(__file__))
            resources_dir = os.path.join(addon_dir, "resources")
            
            if self.frame_overlay == 'COMPLEX':
                overlay_path = os.path.join(resources_dir, "texture_4k_complex_V2.png")
            elif self.frame_overlay == 'MINIMAL':
                overlay_path = os.path.join(resources_dir, "texture_4k_minimalizm_V2.png")
            
            if not os.path.exists(overlay_path):
                self.report({'ERROR'}, f"Overlay image not found: {overlay_path}")
                return {'CANCELLED'}
        
        processed_count = 0
        skipped_count = 0
        error_count = 0
        
        for tex_set in selected_sets:
            material_name = tex_set.material_name
            folder_path = tex_set.folder_path
            
            # Create new folder with _Frame suffix
            parent_folder = os.path.dirname(folder_path)
            new_folder_name = f"S_{material_name}_Frame"
            new_folder_path = os.path.join(parent_folder, new_folder_name)
            
            if not os.path.exists(new_folder_path):
                os.makedirs(new_folder_path)
            
            print(f"\n🖼️ Processing texture set: S_{material_name}")
            
            # List of texture types to process
            texture_files = {
                'Diffuse': f"T_{material_name}_Diffuse.png",
                'DiffuseOpacity': f"T_{material_name}_DiffuseOpacity.png",
                'Emit': f"T_{material_name}_Emit.png",
                'Roughness': f"T_{material_name}_Roughness.png",
                'Metallic': f"T_{material_name}_Metallic.png",
                'Opacity': f"T_{material_name}_Opacity.png",
                'Normal': f"T_{material_name}_Normal.png",
                'ERM': f"T_{material_name}_ERM.png",
            }
            
            set_processed = False
            
            for tex_type, filename in texture_files.items():
                filepath = os.path.join(folder_path, filename)
                
                if not os.path.exists(filepath):
                    continue
                
                try:
                    # Load image
                    img = Image.open(filepath)
                    
                    # Skip if resolution <= 256
                    if img.width <= 256 or img.height <= 256:
                        print(f"  ⏭️ {filename}: skipped (resolution {img.width}x{img.height} <= 256px)")
                        # Still copy the file to new folder with new name
                        new_filename = f"T_{material_name}_Frame_{tex_type}.png"
                        output_path = os.path.join(new_folder_path, new_filename)
                        shutil.copy2(filepath, output_path)
                        skipped_count += 1
                        img.close()
                        continue
                    
                    # Process the texture
                    result_img = self.process_texture(
                        img, 
                        tex_type, 
                        overlay_path, 
                        self.scale_factor,
                        material_name,
                        new_folder_path
                    )
                    
                    if result_img:
                        # Save to new folder with new name
                        new_filename = f"T_{material_name}_Frame_{tex_type}.png"
                        output_path = os.path.join(new_folder_path, new_filename)
                        result_img.save(output_path, 'PNG')
                        print(f"  ✅ {filename}: processed and saved as {new_filename}")
                        set_processed = True
                    
                    img.close()
                    if result_img:
                        result_img.close()
                    
                except Exception as e:
                    print(f"  ❌ Error processing {filename}: {e}")
                    error_count += 1
            
            if set_processed:
                processed_count += 1
        
        # Refresh texture sets list to include new sets
        texture_sets.refresh_texture_sets_list(context)
        
        if error_count > 0:
            self.report({'WARNING'}, f"Processed {processed_count} sets, skipped {skipped_count} textures, {error_count} errors")
        else:
            self.report({'INFO'}, f"Processed {processed_count} sets, skipped {skipped_count} textures (<=256px)")
        
        return {'FINISHED'}
    
    def process_texture(self, img, tex_type, overlay_path, scale_factor, material_name, output_folder):
        """Process a single texture with frame creation"""
        from PIL import Image
        import numpy as np
        
        original_size = img.size
        original_mode = img.mode
        has_alpha = original_mode in ('RGBA', 'LA', 'PA')
        
        # Calculate scaled size
        scaled_width = int(original_size[0] * scale_factor)
        scaled_height = int(original_size[1] * scale_factor)
        
        # Scale image using LANCZOS
        scaled_img = img.resize((scaled_width, scaled_height), Image.LANCZOS)
        
        # Create new image with original size
        # Background: black transparent for images with alpha, black opaque for others
        if has_alpha:
            result_img = Image.new('RGBA', original_size, (0, 0, 0, 0))
        else:
            result_img = Image.new('RGBA', original_size, (0, 0, 0, 255))
        
        # Calculate offset to center the scaled image (creates equal margins)
        offset_x = (original_size[0] - scaled_width) // 2
        offset_y = (original_size[1] - scaled_height) // 2
        
        # Paste scaled image in center
        if scaled_img.mode == 'RGBA':
            result_img.paste(scaled_img, (offset_x, offset_y), scaled_img)
        else:
            # Convert to RGBA for pasting
            scaled_rgba = scaled_img.convert('RGBA')
            result_img.paste(scaled_rgba, (offset_x, offset_y))
            scaled_rgba.close()
        
        # Fill margins by extending edge pixels (bleed effect)
        result_array = np.array(result_img)
        
        # Top margin - extend top edge downward
        if offset_y > 0:
            edge_row = result_array[offset_y:offset_y+1, offset_x:offset_x+scaled_width]
            for y in range(offset_y):
                result_array[y, offset_x:offset_x+scaled_width] = edge_row
        
        # Bottom margin - extend bottom edge upward
        if offset_y + scaled_height < original_size[1]:
            edge_row = result_array[offset_y+scaled_height-1:offset_y+scaled_height, offset_x:offset_x+scaled_width]
            for y in range(offset_y + scaled_height, original_size[1]):
                result_array[y, offset_x:offset_x+scaled_width] = edge_row
        
        # Left margin - extend left edge leftward
        if offset_x > 0:
            edge_col = result_array[:, offset_x:offset_x+1]
            for x in range(offset_x):
                result_array[:, x] = edge_col[:, 0]
        
        # Right margin - extend right edge rightward
        if offset_x + scaled_width < original_size[0]:
            edge_col = result_array[:, offset_x+scaled_width-1:offset_x+scaled_width]
            for x in range(offset_x + scaled_width, original_size[0]):
                result_array[:, x] = edge_col[:, 0]
        
        # Convert back to PIL Image
        result_img = Image.fromarray(result_array)
        
        # Apply overlay only on DiffuseOpacity textures
        if tex_type == 'DiffuseOpacity' and overlay_path:
            try:
                overlay_img = Image.open(overlay_path)
                
                # Resize overlay to match texture size if needed
                if overlay_img.size != original_size:
                    overlay_img = overlay_img.resize(original_size, Image.LANCZOS)
                
                # Composite overlay on top
                if overlay_img.mode == 'RGBA':
                    result_img = Image.alpha_composite(result_img.convert('RGBA'), overlay_img)
                
                overlay_img.close()
                
                # Extract alpha channel and save as Opacity
                if result_img.mode == 'RGBA':
                    alpha_channel = result_img.split()[3]
                    new_opacity_filename = f"T_{material_name}_Frame_Opacity.png"
                    opacity_path = os.path.join(output_folder, new_opacity_filename)
                    
                    # Save opacity as grayscale
                    opacity_img = Image.new('L', original_size)
                    opacity_img.paste(alpha_channel)
                    opacity_img.save(opacity_path, 'PNG')
                    opacity_img.close()
                    print(f"  💾 Extracted alpha to {new_opacity_filename}")
                
                # Save DiffuseOpacity as Diffuse (always RGB, no alpha)
                new_diffuse_filename = f"T_{material_name}_Frame_Diffuse.png"
                diffuse_path = os.path.join(output_folder, new_diffuse_filename)
                
                # Always convert to RGB (no alpha channel)
                diffuse_img = result_img.convert('RGB')
                diffuse_img.save(diffuse_path, 'PNG')
                diffuse_img.close()
                
                print(f"  💾 Saved as {new_diffuse_filename}")
                
            except Exception as e:
                print(f"  ⚠️ Error applying overlay: {e}")
        
        # Convert back to original mode if needed
        if not has_alpha and result_img.mode == 'RGBA':
            result_img = result_img.convert('RGB')
        
        scaled_img.close()
        
        return result_img
    
    def invoke(self, context, event):
        selected_count = sum(1 for tex_set in context.scene.agr_texture_sets if tex_set.is_selected)
        if selected_count == 0:
            self.report({'WARNING'}, "No sets selected")
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self)
    
    def draw(self, context):
        layout = self.layout
        selected_count = sum(1 for tex_set in context.scene.agr_texture_sets if tex_set.is_selected)
        
        layout.label(text=f"Create frame on {selected_count} texture set(s)", icon='IMAGE_DATA')
        layout.separator()
        layout.prop(self, "scale_factor")
        layout.prop(self, "frame_overlay")
        layout.separator()
        layout.label(text="Textures <= 256px will be copied as-is", icon='INFO')
        layout.label(text="New sets saved to *_Frame folders", icon='FILE_FOLDER')
        layout.label(text="Textures renamed to T_Name_Frame_Type", icon='FILE')


class AGR_OT_CreateFrameOnFiles(Operator):
    """Create frame on manually selected texture files"""
    bl_idname = "agr.create_frame_on_files"
    bl_label = "Create Frame on Files"
    bl_options = {'REGISTER', 'UNDO'}
    
    files: CollectionProperty(
        type=bpy.types.OperatorFileListElement,
        options={'HIDDEN', 'SKIP_SAVE'}
    )
    
    directory: StringProperty(
        subtype='DIR_PATH',
        options={'HIDDEN', 'SKIP_SAVE'}
    )
    
    filter_glob: StringProperty(
        default="*.png;*.jpg;*.jpeg;*.tga;*.bmp;*.tiff",
        options={'HIDDEN'}
    )
    
    scale_factor: FloatProperty(
        name="Scale Factor",
        description="Scale factor for texture (0.90625 = 192px border on 4K)",
        default=0.90625,
        min=0.1,
        max=1.0
    )
    
    frame_overlay: EnumProperty(
        name="Frame Overlay",
        description="Choose frame overlay to apply",
        items=[
            ('NONE', "None", "No overlay, just create border"),
            ('COMPLEX', "Complex", "Apply complex frame overlay"),
            ('MINIMAL', "Minimal", "Apply minimal frame overlay"),
        ],
        default='NONE'
    )
    
    def execute(self, context):
        try:
            from PIL import Image
        except ImportError:
            self.report({'ERROR'}, "PIL/Pillow not available. Install with: pip install Pillow")
            return {'CANCELLED'}
        
        if not self.files:
            self.report({'WARNING'}, "No files selected")
            return {'CANCELLED'}
        
        # Get overlay image path if needed
        overlay_path = None
        if self.frame_overlay != 'NONE':
            addon_dir = os.path.dirname(os.path.abspath(__file__))
            resources_dir = os.path.join(addon_dir, "resources")
            
            if self.frame_overlay == 'COMPLEX':
                overlay_path = os.path.join(resources_dir, "texture_4k_complex_V2.png")
            elif self.frame_overlay == 'MINIMAL':
                overlay_path = os.path.join(resources_dir, "texture_4k_minimalizm_V2.png")
            
            if not os.path.exists(overlay_path):
                self.report({'ERROR'}, f"Overlay image not found: {overlay_path}")
                return {'CANCELLED'}
        
        processed_count = 0
        skipped_count = 0
        error_count = 0
        
        for file_elem in self.files:
            filepath = os.path.join(self.directory, file_elem.name)
            
            if not os.path.exists(filepath):
                continue
            
            try:
                # Load image
                img = Image.open(filepath)
                
                # Skip if resolution <= 256
                if img.width <= 256 or img.height <= 256:
                    print(f"⏭️ {file_elem.name}: skipped (resolution {img.width}x{img.height} <= 256px)")
                    skipped_count += 1
                    img.close()
                    continue
                
                original_size = img.size
                original_mode = img.mode
                has_alpha = original_mode in ('RGBA', 'LA', 'PA')
                
                # Calculate scaled size
                scaled_width = int(original_size[0] * self.scale_factor)
                scaled_height = int(original_size[1] * self.scale_factor)
                
                # Scale image using LANCZOS
                scaled_img = img.resize((scaled_width, scaled_height), Image.LANCZOS)
                
                # Create new image with original size
                # Background: black transparent for images with alpha, black opaque for others
                if has_alpha:
                    result_img = Image.new('RGBA', original_size, (0, 0, 0, 0))
                else:
                    result_img = Image.new('RGBA', original_size, (0, 0, 0, 255))
                
                # Calculate offset to center the scaled image (creates equal margins)
                offset_x = (original_size[0] - scaled_width) // 2
                offset_y = (original_size[1] - scaled_height) // 2
                
                # Paste scaled image in center
                if scaled_img.mode == 'RGBA':
                    result_img.paste(scaled_img, (offset_x, offset_y), scaled_img)
                else:
                    # Convert to RGBA for pasting
                    scaled_rgba = scaled_img.convert('RGBA')
                    result_img.paste(scaled_rgba, (offset_x, offset_y))
                    scaled_rgba.close()
                
                # Fill margins by extending edge pixels (bleed effect)
                import numpy as np
                result_array = np.array(result_img)
                
                # Top margin - extend top edge downward
                if offset_y > 0:
                    edge_row = result_array[offset_y:offset_y+1, offset_x:offset_x+scaled_width]
                    for y in range(offset_y):
                        result_array[y, offset_x:offset_x+scaled_width] = edge_row
                
                # Bottom margin - extend bottom edge upward
                if offset_y + scaled_height < original_size[1]:
                    edge_row = result_array[offset_y+scaled_height-1:offset_y+scaled_height, offset_x:offset_x+scaled_width]
                    for y in range(offset_y + scaled_height, original_size[1]):
                        result_array[y, offset_x:offset_x+scaled_width] = edge_row
                
                # Left margin - extend left edge leftward
                if offset_x > 0:
                    edge_col = result_array[:, offset_x:offset_x+1]
                    for x in range(offset_x):
                        result_array[:, x] = edge_col[:, 0]
                
                # Right margin - extend right edge rightward
                if offset_x + scaled_width < original_size[0]:
                    edge_col = result_array[:, offset_x+scaled_width-1:offset_x+scaled_width]
                    for x in range(offset_x + scaled_width, original_size[0]):
                        result_array[:, x] = edge_col[:, 0]
                
                # Convert back to PIL Image
                result_img = Image.fromarray(result_array)
                
                # Apply overlay if specified
                if overlay_path:
                    try:
                        overlay_img = Image.open(overlay_path)
                        
                        # Resize overlay to match texture size if needed
                        if overlay_img.size != original_size:
                            overlay_img = overlay_img.resize(original_size, Image.LANCZOS)
                        
                        # Composite overlay on top
                        if overlay_img.mode == 'RGBA':
                            result_img = Image.alpha_composite(result_img.convert('RGBA'), overlay_img)
                        
                        overlay_img.close()
                        
                    except Exception as e:
                        print(f"⚠️ Error applying overlay to {file_elem.name}: {e}")
                
                # Convert back to original mode if needed
                if not has_alpha and result_img.mode == 'RGBA':
                    result_img = result_img.convert('RGB')
                
                # Save back to same file
                result_img.save(filepath, 'PNG')
                print(f"✅ {file_elem.name}: processed and saved")
                
                processed_count += 1
                
                img.close()
                scaled_img.close()
                result_img.close()
                
            except Exception as e:
                print(f"❌ Error processing {file_elem.name}: {e}")
                error_count += 1
        
        if error_count > 0:
            self.report({'WARNING'}, f"Processed {processed_count} files, skipped {skipped_count}, {error_count} errors")
        else:
            self.report({'INFO'}, f"Processed {processed_count} files, skipped {skipped_count} (<=256px)")
        
        return {'FINISHED'}
    
    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


classes = (
    AGR_OT_CreateFrameOnSets,
    AGR_OT_CreateFrameOnFiles,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    
    print("✅ Frame operators registered")


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
