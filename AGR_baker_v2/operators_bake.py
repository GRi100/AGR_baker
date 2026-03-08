"""
Baking operators for AGR Baker v2
"""

import bpy
from bpy.types import Operator
from bpy.props import EnumProperty, BoolProperty
import os
import numpy as np

from .core import baking, materials, texture_sets


class AGR_OT_BakeTextures(Operator):
    """Bake texture set from selected objects to active"""
    bl_idname = "agr.bake_textures"
    bl_label = "Bake Texture Set"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return (context.active_object and 
                context.active_object.type == 'MESH' and
                len(context.active_object.material_slots) > 0)
    
    def execute(self, context):
        settings = context.scene.agr_baker_settings
        target_obj = context.active_object
        
        # Get source objects (all selected except active)
        source_objects = [obj for obj in context.selected_objects 
                         if obj != target_obj and obj.type == 'MESH']
        
        # Check if blend file is saved
        if not bpy.path.abspath("//"):
            self.report({'ERROR'}, "Save blend file first")
            return {'CANCELLED'}
        
        # Ensure AGR_BAKE folder exists
        agr_bake_path = texture_sets.ensure_agr_bake_folder(context)
        if not agr_bake_path:
            self.report({'ERROR'}, "Cannot create AGR_BAKE folder")
            return {'CANCELLED'}
        
        # Setup render engine
        original_engine = context.scene.render.engine
        original_samples = context.scene.cycles.samples
        original_denoise = context.scene.cycles.use_denoising
        
        context.scene.render.engine = 'CYCLES'
        context.scene.cycles.samples = 1
        context.scene.cycles.use_denoising = False
        context.scene.cycles.device = 'CPU'
        
        resolution = int(settings.resolution)
        
        try:
            # Bake each material
            for mat_slot in target_obj.material_slots:
                if not mat_slot.material:
                    continue
                
                material = mat_slot.material
                material_name = material.name
                
                print(f"\n🔥 Baking texture set for: {material_name}")
                
                # Create texture set folder
                set_folder = texture_sets.ensure_texture_set_folder(context, material_name)
                if not set_folder:
                    continue
                
                # Find material index
                mat_idx = -1
                for i, slot in enumerate(target_obj.material_slots):
                    if slot.material == material:
                        mat_idx = i
                        break
                
                if mat_idx == -1:
                    continue
                
                # Bake textures
                self.bake_material_textures(
                    context, target_obj, source_objects,
                    material, mat_idx, material_name,
                    set_folder, resolution, settings
                )
                
                # Save texture set info
                texture_sets.save_texture_set_info(
                    context, material_name, resolution, set_folder
                )
            
            # Refresh texture sets list
            texture_sets.refresh_texture_sets_list(context)
            
            self.report({'INFO'}, f"Baking complete! Textures in {agr_bake_path}")
        
        finally:
            # Restore render settings
            context.scene.render.engine = original_engine
            context.scene.cycles.samples = original_samples
            context.scene.cycles.use_denoising = original_denoise
        
        return {'FINISHED'}
    
    def bake_material_textures(self, context, target_obj, source_objects,
                               material, mat_idx, material_name,
                               set_folder, resolution, settings):
        """Bake all textures for a material"""
        
        # Create images
        img_diffuse = baking.create_texture_image(
            f"T_{material_name}_Diffuse", resolution
        )
        img_roughness = baking.create_texture_image(
            f"T_{material_name}_Roughness", resolution
        )
        img_metallic = baking.create_texture_image(
            f"T_{material_name}_Metallic", resolution
        )
        img_emit = baking.create_texture_image(
            f"T_{material_name}_Emit", resolution
        )
        img_normal = baking.create_texture_image(
            f"T_{material_name}_Normal", resolution
        )
        img_opacity = baking.create_texture_image(
            f"T_{material_name}_Opacity", resolution
        )
        
        # Setup bake node
        baking.setup_bake_node(material)
        
        # Bake Diffuse
        print("  📸 Baking Diffuse...")
        baking.bake_texture(
            context, target_obj, source_objects, img_diffuse,
            'DIFFUSE', mat_idx,
            max_ray_distance=settings.max_ray_distance,
            extrusion=settings.extrusion
        )
        baking.save_texture(
            img_diffuse,
            os.path.join(set_folder, f"T_{material_name}_Diffuse.png")
        )
        
        # Bake DiffuseOpacity if needed
        if settings.bake_with_alpha:
            print("  📸 Baking DiffuseOpacity...")
            img_diffuse_opacity = baking.create_texture_image(
                f"T_{material_name}_DiffuseOpacity", resolution, with_alpha=True
            )
            baking.bake_texture(
                context, target_obj, source_objects, img_diffuse_opacity,
                'DIFFUSE', mat_idx, use_alpha=True,
                max_ray_distance=settings.max_ray_distance,
                extrusion=settings.extrusion
            )
            baking.save_texture(
                img_diffuse_opacity,
                os.path.join(set_folder, f"T_{material_name}_DiffuseOpacity.png")
            )
            
            # Extract opacity from alpha
            self.extract_opacity_from_alpha(
                img_diffuse_opacity, img_opacity, resolution
            )
        else:
            # Create white opacity
            pixels = [1.0, 1.0, 1.0, 1.0] * (resolution * resolution)
            img_opacity.pixels.foreach_set(pixels)
        
        baking.save_texture(
            img_opacity,
            os.path.join(set_folder, f"T_{material_name}_Opacity.png")
        )
        
        # Bake Roughness
        print("  📸 Baking Roughness...")
        baking.bake_texture(
            context, target_obj, source_objects, img_roughness,
            'ROUGHNESS', mat_idx,
            max_ray_distance=settings.max_ray_distance,
            extrusion=settings.extrusion
        )
        baking.save_texture(
            img_roughness,
            os.path.join(set_folder, f"T_{material_name}_Roughness.png")
        )
        
        # Bake Metallic (via roughness channel)
        print("  📸 Baking Metallic...")
        self.bake_metallic_via_roughness(
            context, target_obj, source_objects,
            img_metallic, mat_idx, settings
        )
        baking.save_texture(
            img_metallic,
            os.path.join(set_folder, f"T_{material_name}_Metallic.png")
        )
        
        # Bake Emit (via roughness channel)
        print("  📸 Baking Emit...")
        self.bake_emit_via_roughness(
            context, target_obj, source_objects,
            img_emit, mat_idx, settings
        )
        baking.save_texture(
            img_emit,
            os.path.join(set_folder, f"T_{material_name}_Emit.png")
        )
        
        # Bake Normal
        print("  📸 Baking Normal...")
        img_normal = baking.create_flat_normal_image(
            f"T_{material_name}_Normal", 256
        )
        if len(source_objects) > 0:
            baking.bake_texture(
                context, target_obj, source_objects, img_normal,
                'NORMAL', mat_idx,
                normal_type=settings.normal_type,
                max_ray_distance=settings.max_ray_distance,
                extrusion=settings.extrusion
            )
        baking.save_texture(
            img_normal,
            os.path.join(set_folder, f"T_{material_name}_Normal.png")
        )
        
        # Create ERM texture
        print("  🎨 Creating ERM...")
        img_erm = baking.create_texture_image(
            f"T_{material_name}_ERM", resolution
        )
        self.create_erm_from_images(
            img_emit, img_roughness, img_metallic, img_erm
        )
        baking.save_texture(
            img_erm,
            os.path.join(set_folder, f"T_{material_name}_ERM.png")
        )
        
        print(f"✅ Baked texture set: S_{material_name}")
    
    def extract_opacity_from_alpha(self, diffuse_opacity_img, opacity_img, resolution):
        """Extract opacity from alpha channel"""
        try:
            width, height = diffuse_opacity_img.size
            pixels = np.array(diffuse_opacity_img.pixels[:]).reshape(height, width, 4)
            
            # Extract alpha channel
            alpha = pixels[:, :, 3]
            
            # Create opacity image (white where opaque, black where transparent)
            opacity_array = np.zeros((height, width, 4), dtype=np.float32)
            opacity_value = np.where(alpha > 0.5, 1.0, 0.0)
            
            opacity_array[:, :, 0] = opacity_value
            opacity_array[:, :, 1] = opacity_value
            opacity_array[:, :, 2] = opacity_value
            opacity_array[:, :, 3] = 1.0
            
            opacity_img.pixels = opacity_array.flatten().tolist()
            opacity_img.update()
            
            print(f"  ✅ Extracted opacity from alpha")
        
        except Exception as e:
            print(f"  ❌ Opacity extraction error: {e}")
    
    def create_erm_from_images(self, emit_img, roughness_img, metallic_img, erm_img):
        """Create ERM texture from E, R, M images"""
        try:
            width, height = emit_img.size
            
            emit_array = np.array(emit_img.pixels[:]).reshape(height, width, 4)
            roughness_array = np.array(roughness_img.pixels[:]).reshape(height, width, 4)
            metallic_array = np.array(metallic_img.pixels[:]).reshape(height, width, 4)
            
            erm_array = np.zeros((height, width, 4), dtype=np.float32)
            erm_array[:, :, 0] = emit_array[:, :, 0]       # R = Emit
            erm_array[:, :, 1] = roughness_array[:, :, 0]  # G = Roughness
            erm_array[:, :, 2] = metallic_array[:, :, 0]   # B = Metallic
            erm_array[:, :, 3] = 1.0
            
            erm_img.pixels = erm_array.flatten().tolist()
            erm_img.update()
            
            print(f"  ✅ Created ERM texture")
        
        except Exception as e:
            print(f"  ❌ ERM creation error: {e}")
    
    def bake_metallic_via_roughness(self, context, target_obj, source_objects,
                                    metallic_img, mat_idx, settings):
        """Bake metallic by routing it through roughness input"""
        material = target_obj.material_slots[mat_idx].material
        original_states = []
        
        if material.use_nodes:
            for node in material.node_tree.nodes:
                if node.type == 'BSDF_PRINCIPLED':
                    metallic_value = node.inputs['Metallic'].default_value
                    roughness_value = node.inputs['Roughness'].default_value
                    
                    metallic_links = []
                    for link in material.node_tree.links:
                        if link.to_socket == node.inputs['Metallic']:
                            metallic_links.append((link.from_node, link.from_socket))
                    
                    original_states.append((node, metallic_value, roughness_value, metallic_links))
                    
                    for link in list(material.node_tree.links):
                        if link.to_socket == node.inputs['Roughness']:
                            material.node_tree.links.remove(link)
                    
                    for from_node, from_socket in metallic_links:
                        material.node_tree.links.new(from_socket, node.inputs['Roughness'])
                    
                    if not metallic_links:
                        node.inputs['Roughness'].default_value = metallic_value
                    
                    node.inputs['Metallic'].default_value = 0.0
        
        baking.bake_texture(
            context, target_obj, source_objects, metallic_img,
            'ROUGHNESS', mat_idx,
            max_ray_distance=settings.max_ray_distance,
            extrusion=settings.extrusion
        )
        
        for node, metallic_value, roughness_value, metallic_links in original_states:
            node.inputs['Metallic'].default_value = metallic_value
            node.inputs['Roughness'].default_value = roughness_value
            
            for link in list(material.node_tree.links):
                if link.to_socket == node.inputs['Roughness']:
                    material.node_tree.links.remove(link)
            
            for from_node, from_socket in metallic_links:
                material.node_tree.links.new(from_socket, node.inputs['Metallic'])
    
    def bake_emit_via_roughness(self, context, target_obj, source_objects,
                                emit_img, mat_idx, settings):
        """Bake emission strength by routing it through roughness input"""
        material = target_obj.material_slots[mat_idx].material
        original_states = []
        
        if material.use_nodes:
            for node in material.node_tree.nodes:
                if node.type == 'BSDF_PRINCIPLED':
                    emit_value = node.inputs['Emission Strength'].default_value
                    roughness_value = node.inputs['Roughness'].default_value
                    
                    emit_links = []
                    for link in material.node_tree.links:
                        if link.to_socket == node.inputs['Emission Strength']:
                            emit_links.append((link.from_node, link.from_socket))
                    
                    original_states.append((node, emit_value, roughness_value, emit_links))
                    
                    for link in list(material.node_tree.links):
                        if link.to_socket == node.inputs['Roughness']:
                            material.node_tree.links.remove(link)
                    
                    for from_node, from_socket in emit_links:
                        material.node_tree.links.new(from_socket, node.inputs['Roughness'])
                    
                    if not emit_links:
                        node.inputs['Roughness'].default_value = emit_value
                    
                    node.inputs['Emission Strength'].default_value = 0.0
        
        baking.bake_texture(
            context, target_obj, source_objects, emit_img,
            'ROUGHNESS', mat_idx,
            max_ray_distance=settings.max_ray_distance,
            extrusion=settings.extrusion
        )
        
        for node, emit_value, roughness_value, emit_links in original_states:
            node.inputs['Emission Strength'].default_value = emit_value
            node.inputs['Roughness'].default_value = roughness_value
            
            for link in list(material.node_tree.links):
                if link.to_socket == node.inputs['Roughness']:
                    material.node_tree.links.remove(link)
            
            for from_node, from_socket in emit_links:
                material.node_tree.links.new(from_socket, node.inputs['Emission Strength'])


classes = (
    AGR_OT_BakeTextures,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
