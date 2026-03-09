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
        # Need at least 2 selected objects for selected-to-active baking
        if not context.active_object or context.active_object.type != 'MESH':
            return False
        if len(context.active_object.material_slots) == 0:
            return False
        # Check if we have at least one other mesh object selected
        other_meshes = [obj for obj in context.selected_objects
                       if obj != context.active_object and obj.type == 'MESH']
        return len(other_meshes) > 0
    
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
                
                # Connect textures to material
                print(f"🔗 Connecting textures to material {material_name}...")
                materials.connect_texture_set_to_material(
                    material,
                    set_folder,
                    material_name
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
        
        # Disable metallic for diffuse baking (metallic ruins diffuse baking)
        print("  🔄 Disabling metallic for diffuse baking...")
        original_metallic_states = self.disable_metallic_for_diffuse(source_objects)
        
        try:
            # Bake Diffuse
            print("  📸 Baking Diffuse...")
            baking.bake_texture(
                context, target_obj, source_objects, img_diffuse,
                'DIFFUSE', mat_idx,
                max_ray_distance=settings.max_ray_distance,
                extrusion=settings.extrusion
            )
            diffuse_path = os.path.join(set_folder, f"T_{material_name}_Diffuse.png")
            baking.save_texture(img_diffuse, diffuse_path)
            
            # Bake DiffuseOpacity if needed
            if settings.bake_with_alpha:
                print("  📸 Baking DiffuseOpacity with alpha...")
                img_diffuse_opacity = baking.create_texture_image(
                    f"T_{material_name}_DiffuseOpacity", resolution, with_alpha=True
                )
                baking.bake_texture(
                    context, target_obj, source_objects, img_diffuse_opacity,
                    'DIFFUSE', mat_idx, use_alpha=True,
                    max_ray_distance=settings.max_ray_distance,
                    extrusion=settings.extrusion
                )
                diffuse_opacity_path = os.path.join(set_folder, f"T_{material_name}_DiffuseOpacity.png")
                baking.save_texture(img_diffuse_opacity, diffuse_opacity_path)
                
                # Extract opacity from alpha
                self.extract_opacity_from_alpha(
                    img_diffuse_opacity, img_opacity, resolution
                )
                baking.save_texture(
                    img_opacity,
                    os.path.join(set_folder, f"T_{material_name}_Opacity.png")
                )
                print(f"✅ Baked DIFFUSE_OPACITY and OPACITY with alpha channel")
            else:
                print("  🔄 No alpha - copying Diffuse as DiffuseOpacity...")
                # Copy Diffuse as DiffuseOpacity (without alpha)
                import shutil
                diffuse_opacity_path = os.path.join(set_folder, f"T_{material_name}_DiffuseOpacity.png")
                shutil.copy2(diffuse_path, diffuse_opacity_path)
                
                # Create white opacity map (fully opaque)
                pixels = [1.0, 1.0, 1.0, 1.0] * (resolution * resolution)
                img_opacity.pixels.foreach_set(pixels)
                baking.save_texture(
                    img_opacity,
                    os.path.join(set_folder, f"T_{material_name}_Opacity.png")
                )
                print(f"✅ Diffuse copied as DiffuseOpacity, created white Opacity")
                
        finally:
            # Restore metallic
            self.restore_metallic_states(original_metallic_states)
            print("  ✅ Restored metallic values")
        
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
        if len(source_objects) > 0:
            # Bake from high-poly with correct resolution
            img_normal = baking.create_texture_image(
                f"T_{material_name}_Normal", resolution
            )
            img_normal.colorspace_settings.name = 'Non-Color'
            baking.bake_texture(
                context, target_obj, source_objects, img_normal,
                'NORMAL', mat_idx,
                max_ray_distance=settings.max_ray_distance,
                extrusion=settings.extrusion
            )
            print(f"  ✅ Baked normal from high-poly at {resolution}px")
        else:
            # Create flat normal stub only if no high-poly objects
            img_normal = baking.create_flat_normal_image(
                f"T_{material_name}_Normal", resolution
            )
            print(f"  🔄 Created flat normal stub at {resolution}px (no high-poly)")
        
        baking.save_texture(
            img_normal,
            os.path.join(set_folder, f"T_{material_name}_Normal.png")
        )
        
        # Create ERM texture from saved files
        print("  🎨 Creating ERM from saved files...")
        img_erm = baking.create_texture_image(
            f"T_{material_name}_ERM", resolution
        )
        self.create_erm_from_files(
            set_folder, material_name, img_erm
        )
        baking.save_texture(
            img_erm,
            os.path.join(set_folder, f"T_{material_name}_ERM.png")
        )
        
        print(f"✅ Baked texture set: S_{material_name}")
    
    def disable_metallic_for_diffuse(self, source_objects):
        """Disable metallic on all source objects for correct diffuse baking"""
        original_states = []
        processed_materials = set()
        
        print(f"  🔄 Disabling metallic for {len(source_objects)} source objects...")
        
        for source_obj in source_objects:
            for mat_slot in source_obj.material_slots:
                if not mat_slot.material or not mat_slot.material.use_nodes:
                    continue
                
                mat = mat_slot.material
                
                # Skip already processed materials
                if mat.name in processed_materials:
                    continue
                
                processed_materials.add(mat.name)
                nodes_data = []
                
                for node in mat.node_tree.nodes:
                    if node.type == 'BSDF_PRINCIPLED':
                        metallic_value = node.inputs['Metallic'].default_value
                        ior_value = node.inputs['IOR'].default_value
                        
                        metallic_links = []
                        ior_links = []
                        
                        for link in mat.node_tree.links:
                            if link.to_socket == node.inputs['Metallic']:
                                metallic_links.append((link.from_node, link.from_socket))
                            elif link.to_socket == node.inputs['IOR']:
                                ior_links.append((link.from_node, link.from_socket))
                        
                        nodes_data.append((node, metallic_value, ior_value, metallic_links, ior_links))
                
                if nodes_data:
                    original_states.append((mat, nodes_data))
                    
                    # Disable metallic and set IOR to 1.0
                    for node, _, _, metallic_links, ior_links in nodes_data:
                        # Remove metallic connections
                        for link in list(mat.node_tree.links):
                            if link.to_socket == node.inputs['Metallic']:
                                mat.node_tree.links.remove(link)
                            elif link.to_socket == node.inputs['IOR']:
                                mat.node_tree.links.remove(link)
                        
                        node.inputs['Metallic'].default_value = 0.0
                        node.inputs['IOR'].default_value = 1.0
        
        print(f"  ✅ Disabled metallic for {len(processed_materials)} materials")
        return original_states
    
    def restore_metallic_states(self, original_states):
        """Restore metallic values after diffuse baking"""
        for mat, nodes_data in original_states:
            for node, metallic_value, ior_value, metallic_links, ior_links in nodes_data:
                node.inputs['Metallic'].default_value = metallic_value
                node.inputs['IOR'].default_value = ior_value
                
                # Restore connections
                for from_node, from_socket in metallic_links:
                    mat.node_tree.links.new(from_socket, node.inputs['Metallic'])
                
                for from_node, from_socket in ior_links:
                    mat.node_tree.links.new(from_socket, node.inputs['IOR'])
    
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
    
    def create_erm_from_files(self, set_folder, material_name, erm_img):
        """Create ERM texture from saved E, R, M files"""
        try:
            emit_path = os.path.join(set_folder, f"T_{material_name}_Emit.png")
            roughness_path = os.path.join(set_folder, f"T_{material_name}_Roughness.png")
            metallic_path = os.path.join(set_folder, f"T_{material_name}_Metallic.png")
            
            # Load files
            emit_file = bpy.data.images.load(emit_path)
            roughness_file = bpy.data.images.load(roughness_path)
            metallic_file = bpy.data.images.load(metallic_path)
            
            emit_file.colorspace_settings.name = 'Non-Color'
            roughness_file.colorspace_settings.name = 'Non-Color'
            metallic_file.colorspace_settings.name = 'Non-Color'
            
            width, height = emit_file.size
            
            # Convert to arrays
            emit_array = np.array(emit_file.pixels[:]).reshape(height, width, 4)
            roughness_array = np.array(roughness_file.pixels[:]).reshape(height, width, 4)
            metallic_array = np.array(metallic_file.pixels[:]).reshape(height, width, 4)
            
            # Create ERM
            erm_array = np.zeros((height, width, 4), dtype=np.float32)
            erm_array[:, :, 0] = emit_array[:, :, 0]       # R = Emit
            erm_array[:, :, 1] = roughness_array[:, :, 0]  # G = Roughness
            erm_array[:, :, 2] = metallic_array[:, :, 0]   # B = Metallic
            erm_array[:, :, 3] = 1.0
            
            erm_img.pixels = erm_array.flatten().tolist()
            erm_img.update()
            
            # Cleanup loaded files
            bpy.data.images.remove(emit_file)
            bpy.data.images.remove(roughness_file)
            bpy.data.images.remove(metallic_file)
            
            print(f"  ✅ Created ERM texture from saved files")
        
        except Exception as e:
            print(f"  ❌ ERM creation error: {e}")
    
    def bake_metallic_via_roughness(self, context, target_obj, source_objects,
                                    metallic_img, mat_idx, settings):
        """Bake metallic by routing it through roughness input for ALL objects"""
        original_states = []
        
        # Process ALL source objects (high-poly)
        for source_obj in source_objects:
            for mat_slot in source_obj.material_slots:
                if not mat_slot.material or not mat_slot.material.use_nodes:
                    continue
                
                mat = mat_slot.material
                nodes_data = []
                
                for node in mat.node_tree.nodes:
                    if node.type == 'BSDF_PRINCIPLED':
                        metallic_value = node.inputs['Metallic'].default_value
                        roughness_value = node.inputs['Roughness'].default_value
                        
                        metallic_links = []
                        roughness_links = []
                        for link in mat.node_tree.links:
                            if link.to_socket == node.inputs['Metallic']:
                                metallic_links.append((link.from_node, link.from_socket))
                            elif link.to_socket == node.inputs['Roughness']:
                                roughness_links.append((link.from_node, link.from_socket))
                        
                        nodes_data.append((node, metallic_value, roughness_value, metallic_links, roughness_links))
                        
                        # Disconnect roughness
                        for link in list(mat.node_tree.links):
                            if link.to_socket == node.inputs['Roughness']:
                                mat.node_tree.links.remove(link)
                        
                        # Connect metallic to roughness
                        for from_node, from_socket in metallic_links:
                            mat.node_tree.links.new(from_socket, node.inputs['Roughness'])
                        
                        if not metallic_links:
                            node.inputs['Roughness'].default_value = metallic_value
                        
                        node.inputs['Metallic'].default_value = 0.0
                
                if nodes_data:
                    original_states.append((mat, nodes_data))
        
        # Bake
        baking.bake_texture(
            context, target_obj, source_objects, metallic_img,
            'ROUGHNESS', mat_idx,
            max_ray_distance=settings.max_ray_distance,
            extrusion=settings.extrusion
        )
        
        # Restore all materials
        for mat, nodes_data in original_states:
            for node, metallic_value, roughness_value, metallic_links, roughness_links in nodes_data:
                node.inputs['Metallic'].default_value = metallic_value
                node.inputs['Roughness'].default_value = roughness_value
                
                # Remove temporary connections
                for link in list(mat.node_tree.links):
                    if link.to_socket == node.inputs['Roughness']:
                        mat.node_tree.links.remove(link)
                
                # Restore original connections
                for from_node, from_socket in roughness_links:
                    mat.node_tree.links.new(from_socket, node.inputs['Roughness'])
    
    def bake_emit_via_roughness(self, context, target_obj, source_objects,
                                emit_img, mat_idx, settings):
        """Bake emission strength by routing it through roughness input for ALL objects"""
        original_states = []
        
        # Process ALL source objects (high-poly)
        for source_obj in source_objects:
            for mat_slot in source_obj.material_slots:
                if not mat_slot.material or not mat_slot.material.use_nodes:
                    continue
                
                mat = mat_slot.material
                nodes_data = []
                
                for node in mat.node_tree.nodes:
                    if node.type == 'BSDF_PRINCIPLED':
                        emit_value = node.inputs['Emission Strength'].default_value
                        roughness_value = node.inputs['Roughness'].default_value
                        
                        emit_links = []
                        roughness_links = []
                        for link in mat.node_tree.links:
                            if link.to_socket == node.inputs['Emission Strength']:
                                emit_links.append((link.from_node, link.from_socket))
                            elif link.to_socket == node.inputs['Roughness']:
                                roughness_links.append((link.from_node, link.from_socket))
                        
                        nodes_data.append((node, emit_value, roughness_value, emit_links, roughness_links))
                        
                        # Disconnect roughness
                        for link in list(mat.node_tree.links):
                            if link.to_socket == node.inputs['Roughness']:
                                mat.node_tree.links.remove(link)
                        
                        # Connect emit to roughness
                        for from_node, from_socket in emit_links:
                            mat.node_tree.links.new(from_socket, node.inputs['Roughness'])
                        
                        if not emit_links:
                            node.inputs['Roughness'].default_value = emit_value
                        
                        node.inputs['Emission Strength'].default_value = 0.0
                
                if nodes_data:
                    original_states.append((mat, nodes_data))
        
        # Bake
        baking.bake_texture(
            context, target_obj, source_objects, emit_img,
            'ROUGHNESS', mat_idx,
            max_ray_distance=settings.max_ray_distance,
            extrusion=settings.extrusion
        )
        
        # Restore all materials
        for mat, nodes_data in original_states:
            for node, emit_value, roughness_value, emit_links, roughness_links in nodes_data:
                node.inputs['Emission Strength'].default_value = emit_value
                node.inputs['Roughness'].default_value = roughness_value
                
                # Remove temporary connections
                for link in list(mat.node_tree.links):
                    if link.to_socket == node.inputs['Roughness']:
                        mat.node_tree.links.remove(link)
                
                # Restore original connections
                for from_node, from_socket in roughness_links:
                    mat.node_tree.links.new(from_socket, node.inputs['Roughness'])


classes = (
    AGR_OT_BakeTextures,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
