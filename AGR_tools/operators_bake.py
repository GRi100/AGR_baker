"""
Baking operators for AGR Tools
"""

import bpy
from .log import unregister_classes
from bpy.types import Operator
from bpy.props import EnumProperty, BoolProperty
import os
import re
import shutil
import numpy as np

from .core import baking, materials, texture_sets


def sanitize_material_name(name):
    """Replace filesystem-unsafe characters in material name with underscores"""
    return re.sub(r'[/\\:*?"<>|]', '_', name)


def compose_erm_from_files(set_folder, material_name, erm_img):
    """Fill erm_img with R=Emit, G=Roughness, B=Metallic read from the saved PNGs.

    Returns False when the fast path does not apply (no Pillow, exotic bit depth,
    sizes that disagree) so the caller can fall back to the datablock path.

    Why: the datablock path loads three full float32 images — 268 MB each at 4K,
    ~1 GB transient per material — to take ONE channel from each of them.
    """
    try:
        from PIL import Image
    except ImportError:
        return False

    paths = [os.path.join(set_folder, f"T_{material_name}_{t}.png")
             for t in ("Emit", "Roughness", "Metallic")]
    if not all(os.path.exists(path) for path in paths):
        return False

    try:
        planes = []
        for path in paths:
            with Image.open(path) as img:
                # 16-bit / float modes quantise differently — leave them to the
                # legacy path rather than guessing
                if img.mode not in ('L', 'LA', 'P', 'RGB', 'RGBA'):
                    return False
                planes.append(np.asarray(img.convert('RGB').getchannel(0)))

        if planes[0].shape != planes[1].shape or planes[0].shape != planes[2].shape:
            return False
        height, width = planes[0].shape
        if (width, height) != tuple(erm_img.size):
            return False

        erm_array = np.empty((height, width, 4), dtype=np.float32)
        # PIL rows run top-down, Blender's pixel buffer bottom-up
        for channel, plane in enumerate(planes):
            erm_array[:, :, channel] = np.flipud(plane).astype(np.float32) / 255.0
        erm_array[:, :, 3] = 1.0

        erm_img.pixels.foreach_set(erm_array.ravel())
        erm_img.update()
        print(f"  ✅ Created ERM texture from saved files (uint8 path)")
        return True

    except Exception as e:
        print(f"  ⚠️ Fast ERM path failed ({e}), falling back")
        return False


def disable_alpha_on_material(material):
    """Unlink Alpha and force it to 1.0 on every Principled BSDF of a material.
    Returns the saved state for restore_alpha_on_material."""
    saved_state = []

    if not material or not material.use_nodes or not material.node_tree:
        return saved_state

    for node in material.node_tree.nodes:
        if node.type != 'BSDF_PRINCIPLED':
            continue

        alpha_value = node.inputs['Alpha'].default_value
        alpha_links = [(link.from_node, link.from_socket)
                       for link in material.node_tree.links
                       if link.to_socket == node.inputs['Alpha']]

        saved_state.append((node, alpha_value, alpha_links))

        for link in list(material.node_tree.links):
            if link.to_socket == node.inputs['Alpha']:
                material.node_tree.links.remove(link)

        node.inputs['Alpha'].default_value = 1.0

    return saved_state


def restore_alpha_on_material(material, saved_state):
    """Restore what disable_alpha_on_material saved."""
    if not material or not material.node_tree:
        return
    for node, alpha_value, alpha_links in saved_state:
        node.inputs['Alpha'].default_value = alpha_value
        for _from_node, from_socket in alpha_links:
            material.node_tree.links.new(from_socket, node.inputs['Alpha'])


def disable_alpha_on_objects(objects):
    """Disable Alpha on every material of every object (deduped by material).

    Simple Bake bakes Roughness/Metallic/Emit/Normal with alpha off; the
    selected-to-active path used to leave the high-poly cutout alive, so rays
    passed through leaves/grids and the two pipelines disagreed on the same
    material.
    """
    states = []
    processed_materials = set()

    for obj in objects:
        for mat_slot in obj.material_slots:
            mat = mat_slot.material
            if not mat or not mat.use_nodes:
                continue
            if mat.name in processed_materials:
                continue
            processed_materials.add(mat.name)
            saved = disable_alpha_on_material(mat)
            if saved:
                states.append((mat, saved))

    return states


def restore_alpha_on_objects(states):
    """Restore what disable_alpha_on_objects saved."""
    for mat, saved in states:
        restore_alpha_on_material(mat, saved)


def _image_pixels_to_np(image):
    """Read image pixels into a float32 array shaped (height, width, 4).
    foreach_get is orders of magnitude faster than slicing image.pixels[:]."""
    width, height = image.size
    buf = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(buf)
    return buf.reshape(height, width, 4)


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
        # Active object must have exactly 1 material for baking
        if len(context.active_object.material_slots) != 1:
            return False
        # Check if we have at least one other mesh object selected
        other_meshes = [obj for obj in context.selected_objects
                       if obj != context.active_object and obj.type == 'MESH']
        return len(other_meshes) > 0
    
    def execute(self, context):
        # Save selection context and mode
        original_selection = list(context.selected_objects)
        original_active = context.active_object
        original_mode = original_active.mode if original_active else 'OBJECT'
        print(f"💾 Saved context: {len(original_selection)} objects, active: {original_active.name if original_active else 'None'}, mode: {original_mode}")
        
        # Switch to OBJECT mode if needed
        if original_active and original_mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
                print(f"🔄 Switched from {original_mode} to OBJECT mode")
            except Exception as e:
                print(f"⚠️ Could not switch to OBJECT mode: {e}")
        
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
        
        # Setup render engine (match old baker settings)
        original_engine = context.scene.render.engine
        original_samples = context.scene.cycles.samples
        original_denoise = context.scene.cycles.use_denoising
        original_device = context.scene.cycles.device

        context.scene.render.engine = 'CYCLES'
        context.scene.cycles.samples = settings.bake_samples
        context.scene.cycles.use_denoising = settings.bake_use_denoising
        context.scene.cycles.device = settings.bake_device

        print(f"🔧 Bake settings: engine=CYCLES, samples={settings.bake_samples}, device={settings.bake_device}, denoising={settings.bake_use_denoising}")
        
        resolution = int(settings.resolution)
        failed_connects = []

        try:
            # Bake each material
            for mat_slot in target_obj.material_slots:
                if not mat_slot.material:
                    continue
                
                material = mat_slot.material
                material_name = sanitize_material_name(material.name)
                
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
                connected = materials.connect_texture_set_to_material(
                    material,
                    set_folder,
                    material_name
                )
                if connected is None:
                    # Textures are on disk but unreadable: the material graph is
                    # left untouched, so say so instead of reporting success
                    failed_connects.append(material_name)

                # Drop the T_*.001 leftovers this run renamed away — every
                # iterative re-bake used to add five generated datablocks
                AGR_OT_SimpleBake.cleanup_renamed_images(material_name)
            
            # Refresh texture sets list
            texture_sets.refresh_texture_sets_list(context)

            if failed_connects:
                self.report({'WARNING'},
                            "Запечено, но текстуры не подключились (файлы не читаются): "
                            + ", ".join(failed_connects))
            else:
                self.report({'INFO'}, f"Baking complete! Textures in {agr_bake_path}")
        
        finally:
            # Restore render settings
            context.scene.render.engine = original_engine
            context.scene.cycles.samples = original_samples
            context.scene.cycles.use_denoising = original_denoise
            context.scene.cycles.device = original_device

            # Restore selection context
            bpy.ops.object.select_all(action='DESELECT')
            for obj in original_selection:
                if obj.name in bpy.data.objects:
                    bpy.data.objects[obj.name].select_set(True)
            if original_active and original_active.name in bpy.data.objects:
                context.view_layer.objects.active = bpy.data.objects[original_active.name]

                # Restore original mode
                if original_mode != 'OBJECT':
                    try:
                        bpy.ops.object.mode_set(mode=original_mode)
                        print(f"🔄 Restored mode: {original_mode}")
                    except Exception as e:
                        print(f"⚠️ Could not restore mode {original_mode}: {e}")

            print(f"🔄 Restored selection: {len(original_selection)} objects")

        return {'FINISHED'}

    def bake_material_textures(self, context, target_obj, source_objects,
                               material, mat_idx, material_name,
                               set_folder, resolution, settings):
        """Bake all textures for a material"""

        # Validate single Principled BSDF
        if material.use_nodes:
            bsdf_count = sum(1 for n in material.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
            if bsdf_count > 1:
                print(f"⚠️ Material '{material.name}' has {bsdf_count} Principled BSDF nodes — only one is expected")

        # Create images (opacity created conditionally later based on alpha baking)
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
        # img_normal is created later in the Normal section: both branches
        # there create their own image, an early one would only leak as .001

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
                
                # Extract opacity from alpha - load from saved file
                print("  🔄 Extracting opacity from alpha channel...")
                img_opacity = baking.create_texture_image(
                    f"T_{material_name}_Opacity", resolution
                )
                self.extract_opacity_from_saved_file(
                    diffuse_opacity_path, img_opacity
                )
                baking.save_texture(
                    img_opacity,
                    os.path.join(set_folder, f"T_{material_name}_Opacity.png")
                )
                print(f"✅ Baked DIFFUSE_OPACITY and extracted OPACITY from alpha channel")
            else:
                print("  🔄 No alpha - copying Diffuse as DiffuseOpacity...")
                # Copy Diffuse as DiffuseOpacity (without alpha)
                diffuse_opacity_path = os.path.join(set_folder, f"T_{material_name}_DiffuseOpacity.png")
                shutil.copy2(diffuse_path, diffuse_opacity_path)
                
                # Create white opacity stub 256px (fully opaque)
                img_opacity = baking.create_texture_image(
                    f"T_{material_name}_Opacity", 256
                )
                pixels = [1.0, 1.0, 1.0, 1.0] * (256 * 256)
                img_opacity.pixels.foreach_set(pixels)
                baking.save_texture(
                    img_opacity,
                    os.path.join(set_folder, f"T_{material_name}_Opacity.png")
                )
                print(f"✅ Diffuse copied as DiffuseOpacity, created white Opacity stub 256px")
                
        finally:
            # Restore metallic
            self.restore_metallic_states(original_metallic_states)
            print("  ✅ Restored metallic values")
        
        # Disable alpha for the PBR/Normal passes on the HIGH-POLY sources —
        # the same treatment Simple Bake gives its material, otherwise a cutout
        # high-poly leaves holes in Roughness/Metallic/Emit/Normal
        print("  🔄 Disabling alpha for PBR baking...")
        alpha_states = disable_alpha_on_objects(source_objects)

        try:
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
            if settings.bake_normal_enabled and len(source_objects) > 0:
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
                # Create flat normal stub 256px when baking is disabled or no high-poly
                img_normal = baking.create_flat_normal_image(
                    f"T_{material_name}_Normal", 256
                )
                if not settings.bake_normal_enabled:
                    print(f"  🔄 Created flat normal stub 256px (baking disabled)")
                else:
                    print(f"  🔄 Created flat normal stub 256px (no high-poly)")

            baking.save_texture(
                img_normal,
                os.path.join(set_folder, f"T_{material_name}_Normal.png")
            )

        finally:
            restore_alpha_on_objects(alpha_states)
            print("  ✅ Restored alpha values")

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
    
    def extract_opacity_from_saved_file(self, diffuse_opacity_path, opacity_img):
        """Extract opacity from alpha channel of saved DiffuseOpacity file"""
        diffuse_opacity_file = None
        try:
            # Load the saved DiffuseOpacity file
            diffuse_opacity_file = bpy.data.images.load(diffuse_opacity_path)
            diffuse_opacity_file.colorspace_settings.name = 'sRGB'

            pixels = _image_pixels_to_np(diffuse_opacity_file)
            height, width = pixels.shape[0], pixels.shape[1]

            # Extract alpha channel
            alpha = pixels[:, :, 3]

            # Create opacity image (grayscale from alpha)
            opacity_array = np.zeros((height, width, 4), dtype=np.float32)
            opacity_array[:, :, 0] = alpha
            opacity_array[:, :, 1] = alpha
            opacity_array[:, :, 2] = alpha
            opacity_array[:, :, 3] = 1.0

            opacity_img.pixels.foreach_set(opacity_array.ravel())
            opacity_img.update()

            print(f"  ✅ Extracted opacity from saved DiffuseOpacity file")

        except Exception as e:
            print(f"  ❌ Opacity extraction error: {e}")
            import traceback
            traceback.print_exc()

        finally:
            if diffuse_opacity_file is not None:
                bpy.data.images.remove(diffuse_opacity_file)
    
    def create_erm_from_files(self, set_folder, material_name, erm_img):
        """Create ERM texture from saved E, R, M files"""
        if compose_erm_from_files(set_folder, material_name, erm_img):
            return

        emit_file = roughness_file = metallic_file = None
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
            emit_array = _image_pixels_to_np(emit_file)
            roughness_array = _image_pixels_to_np(roughness_file)
            metallic_array = _image_pixels_to_np(metallic_file)

            # Create ERM
            erm_array = np.zeros((height, width, 4), dtype=np.float32)
            erm_array[:, :, 0] = emit_array[:, :, 0]       # R = Emit
            erm_array[:, :, 1] = roughness_array[:, :, 0]  # G = Roughness
            erm_array[:, :, 2] = metallic_array[:, :, 0]   # B = Metallic
            erm_array[:, :, 3] = 1.0

            erm_img.pixels.foreach_set(erm_array.ravel())
            erm_img.update()

            print(f"  ✅ Created ERM texture from saved files")

        except Exception as e:
            print(f"  ❌ ERM creation error: {e}")

        finally:
            for img in (emit_file, roughness_file, metallic_file):
                if img is not None:
                    bpy.data.images.remove(img)
    
    def bake_metallic_via_roughness(self, context, target_obj, source_objects,
                                    metallic_img, mat_idx, settings):
        """Bake metallic by routing it through roughness input for ALL objects"""
        original_states = []
        processed_materials = set()

        # Process ALL source objects (high-poly)
        for source_obj in source_objects:
            for mat_slot in source_obj.material_slots:
                if not mat_slot.material or not mat_slot.material.use_nodes:
                    continue

                mat = mat_slot.material
                # A shared material must be rewired/snapshotted only once —
                # a second pass would snapshot the already-modified state and
                # its restore would destroy the original wiring.
                if mat.name in processed_materials:
                    continue
                processed_materials.add(mat.name)
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
        
        # Bake — wrapped in try/finally so materials are always restored
        try:
            baking.bake_texture(
                context, target_obj, source_objects, metallic_img,
                'ROUGHNESS', mat_idx,
                max_ray_distance=settings.max_ray_distance,
                extrusion=settings.extrusion
            )
        finally:
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
        processed_materials = set()

        # Process ALL source objects (high-poly)
        for source_obj in source_objects:
            for mat_slot in source_obj.material_slots:
                if not mat_slot.material or not mat_slot.material.use_nodes:
                    continue

                mat = mat_slot.material
                # Same dedup as bake_metallic_via_roughness — see comment there.
                if mat.name in processed_materials:
                    continue
                processed_materials.add(mat.name)
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
        
        # Bake — wrapped in try/finally so materials are always restored
        try:
            baking.bake_texture(
                context, target_obj, source_objects, emit_img,
                'ROUGHNESS', mat_idx,
                max_ray_distance=settings.max_ray_distance,
                extrusion=settings.extrusion
            )
        finally:
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


class AGR_OT_SimpleBake(Operator):
    """Simple bake from material to textures using a plane"""
    bl_idname = "agr.simple_bake"
    bl_label = "Simple Bake from Material"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        # Need active object with at least one material
        if not context.active_object or context.active_object.type != 'MESH':
            return False
        if len(context.active_object.material_slots) == 0:
            return False
        # Need active material
        active_mat = context.active_object.active_material
        return active_mat is not None
    
    def execute(self, context):
        # Save selection context and mode
        original_selection = list(context.selected_objects)
        original_active = context.active_object
        original_mode = original_active.mode if original_active else 'OBJECT'
        print(f"💾 Saved context: {len(original_selection)} objects, active: {original_active.name if original_active else 'None'}, mode: {original_mode}")
        
        # Ensure we're in OBJECT mode before baking
        if original_active and original_mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
                print(f"🔄 Switched from {original_mode} to OBJECT mode")
            except Exception as e:
                self.report({'ERROR'}, f"Cannot switch to OBJECT mode: {e}")
                return {'CANCELLED'}
        
        settings = context.scene.agr_baker_settings
        active_obj = context.active_object
        active_material = active_obj.active_material
        
        if not active_material:
            self.report({'ERROR'}, "No active material")
            return {'CANCELLED'}

        # Guard BEFORE any mutation: setup_bake_node would otherwise hang a new
        # empty Principled on an Emission/Glass material, bake its grey default
        # over the existing set and rebuild the material from those greys
        if baking.material_lacks_principled(active_material):
            # Principled внутри нод-группы виден только тут: сам отказ верный,
            # но совет «переведите эмиссию/стекло» такому материалу бесполезен
            if baking.principled_hidden_in_group(active_material):
                hint = "вынесите Principled из нод-группы на верхний уровень материала"
            else:
                hint = "переведите эмиссию/стекло на Principled"
            self.report({'ERROR'},
                        f"Материал '{active_material.name}' без Principled BSDF "
                        f"на верхнем уровне — {hint}")
            return {'CANCELLED'}

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
        original_device = context.scene.cycles.device

        context.scene.render.engine = 'CYCLES'
        context.scene.cycles.samples = settings.bake_samples
        context.scene.cycles.use_denoising = settings.bake_use_denoising
        context.scene.cycles.device = settings.bake_device

        print(f"🔧 Bake settings: engine=CYCLES, samples={settings.bake_samples}, device={settings.bake_device}, denoising={settings.bake_use_denoising}")

        user_resolution = int(settings.resolution)
        material_name = sanitize_material_name(active_material.name)

        print(f"\n🔥 Simple Bake for material: {material_name}")
        print(f"📏 User selected resolution: {user_resolution}px")
        
        # Auto-detect if we should bake with alpha
        bake_with_alpha = baking.should_bake_with_alpha(active_material)
        print(f"🔍 Auto-detected alpha baking: {bake_with_alpha}")
        
        # Determine resolutions based on connections
        diffuse_res = baking.determine_bake_resolution(active_material, 'Base Color', user_resolution)
        
        # If we're baking with alpha, use user resolution for diffuse
        # (even if Base Color is not connected - we need proper resolution for DiffuseOpacity)
        if bake_with_alpha:
            diffuse_res = user_resolution
            print(f"  📏 Diffuse: Baking with alpha → {user_resolution}px (user)")
        
        pbr_res = baking.determine_pbr_group_resolution(active_material, user_resolution)
        
        # Check if Normal Map exists without texture connected to Color input
        if baking.check_normal_map_without_texture(active_material):
            normal_res = 256
        else:
            normal_res = baking.determine_bake_resolution(active_material, 'Normal', user_resolution)
        
        
        # Create bake plane
        bake_plane = None
        
        try:
            # Create texture set folder
            set_folder = texture_sets.ensure_texture_set_folder(context, material_name)
            if not set_folder:
                self.report({'ERROR'}, "Cannot create texture set folder")
                return {'CANCELLED'}
            
            # Create plane with standard UV
            bake_plane = baking.create_bake_plane(f"BakePlane_{material_name}")
            
            # Assign material to plane
            if len(bake_plane.material_slots) == 0:
                bake_plane.data.materials.append(active_material)
            else:
                bake_plane.material_slots[0].material = active_material
            
            print(f"  ✅ Material '{material_name}' assigned to bake plane")
            
            # Bake textures from plane (no source objects = simple mode)
            self.bake_material_simple(
                context, bake_plane, active_material, material_name,
                set_folder, diffuse_res, pbr_res, normal_res, bake_with_alpha
            )
            
            # Save texture set info
            texture_sets.save_texture_set_info(
                context, material_name, user_resolution, set_folder
            )
            
            # Connect textures to original material
            print(f"🔗 Connecting textures to material {material_name}...")
            connected = materials.connect_texture_set_to_material(
                active_material,
                set_folder,
                material_name
            )
            
            # Refresh texture sets list
            texture_sets.refresh_texture_sets_list(context)
            
            # Cleanup renamed images (T_*.001, T_*.010, etc)
            AGR_OT_SimpleBake.cleanup_renamed_images(material_name)

            if connected is None:
                self.report({'WARNING'},
                            f"Запечено в {set_folder}, но текстуры не подключились "
                            "(файлы не читаются)")
            else:
                self.report({'INFO'}, f"Simple bake complete! Textures in {set_folder}")
        
        finally:
            # Delete bake plane
            if bake_plane:
                bpy.data.objects.remove(bake_plane, do_unlink=True)
                print(f"  🗑️ Removed bake plane")
            
            # Restore render settings
            context.scene.render.engine = original_engine
            context.scene.cycles.samples = original_samples
            context.scene.cycles.use_denoising = original_denoise
            context.scene.cycles.device = original_device

            # Restore selection context
            bpy.ops.object.select_all(action='DESELECT')
            for obj in original_selection:
                if obj.name in bpy.data.objects:
                    bpy.data.objects[obj.name].select_set(True)
            if original_active and original_active.name in bpy.data.objects:
                context.view_layer.objects.active = bpy.data.objects[original_active.name]

                # Restore original mode
                if original_mode != 'OBJECT':
                    try:
                        bpy.ops.object.mode_set(mode=original_mode)
                        print(f"🔄 Restored mode: {original_mode}")
                    except Exception as e:
                        print(f"⚠️ Could not restore mode {original_mode}: {e}")

            print(f"🔄 Restored selection: {len(original_selection)} objects")

        return {'FINISHED'}

    @staticmethod
    def bake_material_simple(context, bake_plane, material, material_name,
                            set_folder, diffuse_res, pbr_res, normal_res, bake_with_alpha):
        """Bake all textures from material on plane"""

        # Operators guard this before mutating anything; a script caller gets an
        # honest error instead of a grey bake over the author's shader (BAKE-3)
        if baking.material_lacks_principled(material):
            raise RuntimeError(
                f"Material '{material.name}' has no Principled BSDF — simple bake refused")

        # Validate single Principled BSDF
        if material.use_nodes:
            bsdf_count = sum(1 for n in material.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
            if bsdf_count > 1:
                print(f"⚠️ Material '{material.name}' has {bsdf_count} Principled BSDF nodes — only one is expected")

        # Create images with determined resolutions
        img_diffuse = baking.create_texture_image(
            f"T_{material_name}_Diffuse", diffuse_res
        )
        img_roughness = baking.create_texture_image(
            f"T_{material_name}_Roughness", pbr_res
        )
        img_metallic = baking.create_texture_image(
            f"T_{material_name}_Metallic", pbr_res
        )
        img_emit = baking.create_texture_image(
            f"T_{material_name}_Emit", pbr_res
        )
        
        # Setup bake node (create_missing=False: the material is already known
        # to carry a Principled BSDF, and inventing one here is exactly the bug)
        baking.setup_bake_node(material, create_missing=False)

        # Disable metallic for diffuse baking
        print(f"  🔄 Disabling metallic for Diffuse baking...")
        metallic_state = AGR_OT_SimpleBake.disable_metallic_simple(material)
        
        try:
            # Bake Diffuse
            print(f"  📸 Baking Diffuse at {diffuse_res}px...")
            baking.bake_texture(
                context, bake_plane, [], img_diffuse,
                'DIFFUSE', 0
            )
            diffuse_path = os.path.join(set_folder, f"T_{material_name}_Diffuse.png")
            baking.save_texture(img_diffuse, diffuse_path)
            
            # Bake DiffuseOpacity if needed (auto-detected)
            if bake_with_alpha:
                print(f"  📸 Baking DiffuseOpacity with alpha at {diffuse_res}px...")
                img_diffuse_opacity = baking.create_texture_image(
                    f"T_{material_name}_DiffuseOpacity", diffuse_res, with_alpha=True
                )
                baking.bake_texture(
                    context, bake_plane, [], img_diffuse_opacity,
                    'DIFFUSE', 0, use_alpha=True
                )
                diffuse_opacity_path = os.path.join(set_folder, f"T_{material_name}_DiffuseOpacity.png")
                baking.save_texture(img_diffuse_opacity, diffuse_opacity_path)
                
                # Extract opacity from alpha
                print("  🔄 Extracting opacity from alpha channel...")
                img_opacity = baking.create_texture_image(
                    f"T_{material_name}_Opacity", diffuse_res
                )
                AGR_OT_SimpleBake.extract_opacity_from_saved_file(diffuse_opacity_path, img_opacity)
                baking.save_texture(
                    img_opacity,
                    os.path.join(set_folder, f"T_{material_name}_Opacity.png")
                )
                print(f"✅ Baked DIFFUSE_OPACITY and extracted OPACITY")
            else:
                print(f"  🔄 No alpha - copying Diffuse as DiffuseOpacity...")
                diffuse_opacity_path = os.path.join(set_folder, f"T_{material_name}_DiffuseOpacity.png")
                shutil.copy2(diffuse_path, diffuse_opacity_path)
                
                # Create white opacity stub with appropriate resolution
                # If diffuse_res >= 256, use 256px; otherwise use diffuse_res
                opacity_res = 256 if diffuse_res >= 256 else diffuse_res
                img_opacity = baking.create_texture_image(
                    f"T_{material_name}_Opacity", opacity_res
                )
                pixels = [1.0, 1.0, 1.0, 1.0] * (opacity_res * opacity_res)
                img_opacity.pixels.foreach_set(pixels)
                baking.save_texture(
                    img_opacity,
                    os.path.join(set_folder, f"T_{material_name}_Opacity.png")
                )
                print(f"✅ Diffuse copied as DiffuseOpacity, created white Opacity stub {opacity_res}px")
        
        finally:
            # Restore metallic
            AGR_OT_SimpleBake.restore_metallic_simple(material, metallic_state)
            print(f"  ✅ Restored metallic values")
        
        # Disable alpha for PBR baking (Roughness, Metallic, Emit, Normal)
        print(f"  🔄 Disabling alpha for PBR baking...")
        alpha_state = AGR_OT_SimpleBake.disable_alpha_simple(material)
        
        try:
            # Bake Roughness
            print(f"  📸 Baking Roughness at {pbr_res}px...")
            baking.bake_texture(
                context, bake_plane, [], img_roughness,
                'ROUGHNESS', 0
            )
            baking.save_texture(
                img_roughness,
                os.path.join(set_folder, f"T_{material_name}_Roughness.png")
            )
            
            # Bake Metallic (via roughness channel)
            print(f"  📸 Baking Metallic at {pbr_res}px...")
            AGR_OT_SimpleBake.bake_metallic_simple(context, bake_plane, material, img_metallic)
            baking.save_texture(
                img_metallic,
                os.path.join(set_folder, f"T_{material_name}_Metallic.png")
            )
            
            # Bake Emit (via roughness channel)
            print(f"  📸 Baking Emit at {pbr_res}px...")
            AGR_OT_SimpleBake.bake_emit_simple(context, bake_plane, material, img_emit)
            baking.save_texture(
                img_emit,
                os.path.join(set_folder, f"T_{material_name}_Emit.png")
            )
            
            # Bake Normal (determined by material, not by bake_normal_enabled setting)
            print(f"  📸 Baking Normal at {normal_res}px...")
            if baking.check_normal_map_without_texture(material):
                # Create 256px stub when Normal Map has no texture
                img_normal = baking.create_flat_normal_image(
                    f"T_{material_name}_Normal", 256
                )
                print(f"  🔄 Created flat normal stub 256px (Normal Map without texture)")
            else:
                # Bake normally when texture exists or no Normal Map
                img_normal = baking.create_texture_image(
                    f"T_{material_name}_Normal", normal_res
                )
                img_normal.colorspace_settings.name = 'Non-Color'
                baking.bake_texture(
                    context, bake_plane, [], img_normal,
                    'NORMAL', 0
                )
                print(f"  ✅ Baked normal at {normal_res}px")
            
            baking.save_texture(
                img_normal,
                os.path.join(set_folder, f"T_{material_name}_Normal.png")
            )
        
        finally:
            # Restore alpha
            AGR_OT_SimpleBake.restore_alpha_simple(material, alpha_state)
            print(f"  ✅ Restored alpha values")
        
        # Create ERM texture from saved files
        print(f"  🎨 Creating ERM from saved files at {pbr_res}px...")
        img_erm = baking.create_texture_image(
            f"T_{material_name}_ERM", pbr_res
        )
        AGR_OT_SimpleBake.create_erm_from_files(set_folder, material_name, img_erm)
        baking.save_texture(
            img_erm,
            os.path.join(set_folder, f"T_{material_name}_ERM.png")
        )
        
        print(f"✅ Simple baked texture set: S_{material_name}")
    
    @staticmethod
    def disable_metallic_simple(material):
        """Disable metallic on material for diffuse baking"""
        saved_state = []
        
        if not material.use_nodes:
            return saved_state
        
        for node in material.node_tree.nodes:
            if node.type == 'BSDF_PRINCIPLED':
                metallic_value = node.inputs['Metallic'].default_value
                ior_value = node.inputs['IOR'].default_value
                
                metallic_links = []
                ior_links = []
                
                for link in material.node_tree.links:
                    if link.to_socket == node.inputs['Metallic']:
                        metallic_links.append((link.from_node, link.from_socket))
                    elif link.to_socket == node.inputs['IOR']:
                        ior_links.append((link.from_node, link.from_socket))
                
                saved_state.append((node, metallic_value, ior_value, metallic_links, ior_links))
                
                # Remove metallic and IOR connections
                for link in list(material.node_tree.links):
                    if link.to_socket == node.inputs['Metallic']:
                        material.node_tree.links.remove(link)
                    elif link.to_socket == node.inputs['IOR']:
                        material.node_tree.links.remove(link)
                
                node.inputs['Metallic'].default_value = 0.0
                node.inputs['IOR'].default_value = 1.0
        
        return saved_state
    
    @staticmethod
    def restore_metallic_simple(material, saved_state):
        """Restore metallic values after diffuse baking"""
        for node, metallic_value, ior_value, metallic_links, ior_links in saved_state:
            node.inputs['Metallic'].default_value = metallic_value
            node.inputs['IOR'].default_value = ior_value
            
            # Restore connections
            for from_node, from_socket in metallic_links:
                material.node_tree.links.new(from_socket, node.inputs['Metallic'])
            
            for from_node, from_socket in ior_links:
                material.node_tree.links.new(from_socket, node.inputs['IOR'])
    
    @staticmethod
    def disable_alpha_simple(material):
        """Disable alpha on material for PBR baking (Roughness, Metallic, Emit, Normal).
        Thin wrapper: the selected-to-active path uses the same helper."""
        return disable_alpha_on_material(material)

    @staticmethod
    def restore_alpha_simple(material, saved_state):
        """Restore alpha values after PBR baking"""
        restore_alpha_on_material(material, saved_state)

    @staticmethod
    def cleanup_renamed_images(material_name):
        """Remove LEFTOVER renamed images like T_materialname_*.001.

        Only datablocks nobody references are dropped: a same-named .001 image
        can belong to a twin material (duplicate / Append / AGR Share copy), and
        deleting it blanked that material's texture nodes file-wide.
        """
        import re
        pattern = re.compile(rf"^T_{re.escape(material_name)}_\w+\.\d{{3}}$")

        removed_count = 0
        for img in list(bpy.data.images):
            if pattern.match(img.name) and img.users == 0:
                print(f"  🗑️ Removing renamed image: {img.name}")
                bpy.data.images.remove(img)
                removed_count += 1

        # Give the surviving texture its canonical name back. The connect step
        # loads the file BEFORE clearing the graph (so a broken PNG cannot wipe
        # it), and at that moment the generated bake image still holds the name
        # — the loaded one lands as T_*_Type.001 and the generated one is orphaned
        # a moment later.
        for img in list(bpy.data.images):
            if not pattern.match(img.name):
                continue
            base = img.name.rsplit('.', 1)[0]
            holder = bpy.data.images.get(base)
            # Only when the canonical name is held by an ORPHAN: a free name means
            # nothing conflicts, and a .001 nobody collides with stays as it is
            if holder is not None and holder is not img and holder.users == 0:
                print(f"  🗑️ Removing orphaned image: {holder.name}")
                bpy.data.images.remove(holder)
                removed_count += 1
                img.name = base

        if removed_count > 0:
            print(f"✅ Cleaned up {removed_count} renamed images")

    @staticmethod
    def bake_metallic_simple(context, bake_plane, material, metallic_img):
        """Bake metallic by routing through roughness"""
        nodes = material.node_tree.nodes
        
        for node in nodes:
            if node.type == 'BSDF_PRINCIPLED':
                metallic_value = node.inputs['Metallic'].default_value
                roughness_value = node.inputs['Roughness'].default_value
                
                metallic_links = []
                roughness_links = []
                for link in material.node_tree.links:
                    if link.to_socket == node.inputs['Metallic']:
                        metallic_links.append((link.from_node, link.from_socket))
                    elif link.to_socket == node.inputs['Roughness']:
                        roughness_links.append((link.from_node, link.from_socket))
                
                # Disconnect roughness
                for link in list(material.node_tree.links):
                    if link.to_socket == node.inputs['Roughness']:
                        material.node_tree.links.remove(link)
                
                # Connect metallic to roughness
                for from_node, from_socket in metallic_links:
                    material.node_tree.links.new(from_socket, node.inputs['Roughness'])

                if not metallic_links:
                    node.inputs['Roughness'].default_value = metallic_value

                node.inputs['Metallic'].default_value = 0.0

                # Bake — wrapped in try/finally so node state is always restored
                try:
                    baking.bake_texture(
                        context, bake_plane, [], metallic_img,
                        'ROUGHNESS', 0
                    )
                finally:
                    # Restore
                    node.inputs['Metallic'].default_value = metallic_value
                    node.inputs['Roughness'].default_value = roughness_value

                    for link in list(material.node_tree.links):
                        if link.to_socket == node.inputs['Roughness']:
                            material.node_tree.links.remove(link)

                    for from_node, from_socket in roughness_links:
                        material.node_tree.links.new(from_socket, node.inputs['Roughness'])

                break
    
    @staticmethod
    def bake_emit_simple(context, bake_plane, material, emit_img):
        """Bake emission by routing through roughness"""
        nodes = material.node_tree.nodes
        
        for node in nodes:
            if node.type == 'BSDF_PRINCIPLED':
                emit_value = node.inputs['Emission Strength'].default_value
                roughness_value = node.inputs['Roughness'].default_value
                
                emit_links = []
                roughness_links = []
                for link in material.node_tree.links:
                    if link.to_socket == node.inputs['Emission Strength']:
                        emit_links.append((link.from_node, link.from_socket))
                    elif link.to_socket == node.inputs['Roughness']:
                        roughness_links.append((link.from_node, link.from_socket))
                
                # Disconnect roughness
                for link in list(material.node_tree.links):
                    if link.to_socket == node.inputs['Roughness']:
                        material.node_tree.links.remove(link)
                
                # Connect emit to roughness
                for from_node, from_socket in emit_links:
                    material.node_tree.links.new(from_socket, node.inputs['Roughness'])

                if not emit_links:
                    node.inputs['Roughness'].default_value = emit_value

                node.inputs['Emission Strength'].default_value = 0.0

                # Bake — wrapped in try/finally so node state is always restored
                try:
                    baking.bake_texture(
                        context, bake_plane, [], emit_img,
                        'ROUGHNESS', 0
                    )
                finally:
                    # Restore
                    node.inputs['Emission Strength'].default_value = emit_value
                    node.inputs['Roughness'].default_value = roughness_value

                    for link in list(material.node_tree.links):
                        if link.to_socket == node.inputs['Roughness']:
                            material.node_tree.links.remove(link)

                    for from_node, from_socket in roughness_links:
                        material.node_tree.links.new(from_socket, node.inputs['Roughness'])

                break
    
    @staticmethod
    def extract_opacity_from_saved_file(diffuse_opacity_path, opacity_img):
        """Extract opacity from alpha channel of saved DiffuseOpacity file"""
        diffuse_opacity_file = None
        try:
            diffuse_opacity_file = bpy.data.images.load(diffuse_opacity_path)
            diffuse_opacity_file.colorspace_settings.name = 'sRGB'

            pixels = _image_pixels_to_np(diffuse_opacity_file)
            height, width = pixels.shape[0], pixels.shape[1]

            alpha = pixels[:, :, 3]

            opacity_array = np.zeros((height, width, 4), dtype=np.float32)
            opacity_array[:, :, 0] = alpha
            opacity_array[:, :, 1] = alpha
            opacity_array[:, :, 2] = alpha
            opacity_array[:, :, 3] = 1.0

            opacity_img.pixels.foreach_set(opacity_array.ravel())
            opacity_img.update()

            print(f"  ✅ Extracted opacity from saved DiffuseOpacity file")

        except Exception as e:
            print(f"  ❌ Opacity extraction error: {e}")

        finally:
            if diffuse_opacity_file is not None:
                bpy.data.images.remove(diffuse_opacity_file)
    
    @staticmethod
    def create_erm_from_files(set_folder, material_name, erm_img):
        """Create ERM texture from saved E, R, M files"""
        if compose_erm_from_files(set_folder, material_name, erm_img):
            return

        emit_file = roughness_file = metallic_file = None
        try:
            emit_path = os.path.join(set_folder, f"T_{material_name}_Emit.png")
            roughness_path = os.path.join(set_folder, f"T_{material_name}_Roughness.png")
            metallic_path = os.path.join(set_folder, f"T_{material_name}_Metallic.png")

            emit_file = bpy.data.images.load(emit_path)
            roughness_file = bpy.data.images.load(roughness_path)
            metallic_file = bpy.data.images.load(metallic_path)

            emit_file.colorspace_settings.name = 'Non-Color'
            roughness_file.colorspace_settings.name = 'Non-Color'
            metallic_file.colorspace_settings.name = 'Non-Color'

            width, height = emit_file.size

            emit_array = _image_pixels_to_np(emit_file)
            roughness_array = _image_pixels_to_np(roughness_file)
            metallic_array = _image_pixels_to_np(metallic_file)

            erm_array = np.zeros((height, width, 4), dtype=np.float32)
            erm_array[:, :, 0] = emit_array[:, :, 0]
            erm_array[:, :, 1] = roughness_array[:, :, 0]
            erm_array[:, :, 2] = metallic_array[:, :, 0]
            erm_array[:, :, 3] = 1.0

            erm_img.pixels.foreach_set(erm_array.ravel())
            erm_img.update()

            print(f"  ✅ Created ERM texture from saved files")

        except Exception as e:
            print(f"  ❌ ERM creation error: {e}")

        finally:
            for img in (emit_file, roughness_file, metallic_file):
                if img is not None:
                    bpy.data.images.remove(img)


class AGR_OT_SimpleBakeAll(Operator):
    """Simple bake all materials on object to textures"""
    bl_idname = "agr.simple_bake_all"
    bl_label = "Simple Bake All Materials"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        # Need active object with at least one material
        if not context.active_object or context.active_object.type != 'MESH':
            return False
        if len(context.active_object.material_slots) == 0:
            return False
        return True
    
    def execute(self, context):
        # Save selection context
        original_selection = list(context.selected_objects)
        original_active = context.active_object
        original_mode = original_active.mode if original_active else 'OBJECT'
        print(f"💾 Saved selection: {len(original_selection)} objects, active: {original_active.name if original_active else 'None'}, mode: {original_mode}")

        # Ensure we're in OBJECT mode before baking
        if context.active_object and context.active_object.mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
                print(f"🔄 Switched to OBJECT mode for baking")
            except Exception as e:
                self.report({'ERROR'}, f"Cannot switch to OBJECT mode: {e}")
                return {'CANCELLED'}
        
        settings = context.scene.agr_baker_settings
        active_obj = context.active_object
        
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
        original_device = context.scene.cycles.device

        context.scene.render.engine = 'CYCLES'
        context.scene.cycles.samples = settings.bake_samples
        context.scene.cycles.use_denoising = settings.bake_use_denoising
        context.scene.cycles.device = settings.bake_device

        user_resolution = int(settings.resolution)
        
        print(f"\n🔥 Simple Bake All Materials for object: {active_obj.name}")
        print(f"🔧 Bake settings: samples={settings.bake_samples}, device={settings.bake_device}, denoising={settings.bake_use_denoising}")
        print(f"📏 User selected resolution: {user_resolution}px")
        print(f"📦 Materials to bake: {len(active_obj.material_slots)}")
        
        baked_count = 0
        skipped_materials = []
        grouped_materials = []   # Principled спрятан в нод-группе — другой совет
        failed_connects = []

        try:
            # Bake each material
            for mat_slot in active_obj.material_slots:
                if not mat_slot.material:
                    print(f"⚠️ Skipping empty material slot")
                    continue
                
                material = mat_slot.material
                material_name = sanitize_material_name(material.name)

                # Same guard as Simple Bake, but a whole-object run skips the
                # slot instead of aborting the batch
                if baking.material_lacks_principled(material):
                    print(f"⚠️ Skipping '{material.name}': no Principled BSDF")
                    # Principled внутри нод-группы отмечается отдельно: совет
                    # «перевести на Principled» такому материалу не подходит
                    if baking.principled_hidden_in_group(material):
                        grouped_materials.append(material.name)
                    else:
                        skipped_materials.append(material.name)
                    continue

                print(f"\n📦 Processing material: {material_name}")
                
                # Auto-detect if we should bake with alpha
                bake_with_alpha = baking.should_bake_with_alpha(material)
                print(f"🔍 Auto-detected alpha baking: {bake_with_alpha}")
                
                # Determine resolutions based on connections
                diffuse_res = baking.determine_bake_resolution(material, 'Base Color', user_resolution)
                
                # If we're baking with alpha, use user resolution for diffuse
                # (even if Base Color is not connected - we need proper resolution for DiffuseOpacity)
                if bake_with_alpha:
                    diffuse_res = user_resolution
                    print(f"  📏 Diffuse: Baking with alpha → {user_resolution}px (user)")
                
                pbr_res = baking.determine_pbr_group_resolution(material, user_resolution)
                
                # Check if Normal Map exists without texture connected to Color input
                if baking.check_normal_map_without_texture(material):
                    normal_res = 256
                else:
                    normal_res = baking.determine_bake_resolution(material, 'Normal', user_resolution)
                
                # Create bake plane
                bake_plane = None
                
                try:
                    # Create texture set folder
                    set_folder = texture_sets.ensure_texture_set_folder(context, material_name)
                    if not set_folder:
                        print(f"❌ Cannot create texture set folder for {material_name}")
                        continue
                    
                    # Create plane with standard UV
                    bake_plane = baking.create_bake_plane(f"BakePlane_{material_name}")
                    
                    # Assign material to plane
                    if len(bake_plane.material_slots) == 0:
                        bake_plane.data.materials.append(material)
                    else:
                        bake_plane.material_slots[0].material = material
                    
                    print(f"  ✅ Material '{material_name}' assigned to bake plane")
                    
                    # Bake textures from plane - call static method directly
                    AGR_OT_SimpleBake.bake_material_simple(
                        context, bake_plane, material, material_name,
                        set_folder, diffuse_res, pbr_res, normal_res, bake_with_alpha
                    )
                    
                    # Save texture set info
                    texture_sets.save_texture_set_info(
                        context, material_name, user_resolution, set_folder
                    )
                    
                    # Connect textures to material
                    print(f"🔗 Connecting textures to material {material_name}...")
                    connected = materials.connect_texture_set_to_material(
                        material,
                        set_folder,
                        material_name
                    )
                    if connected is None:
                        failed_connects.append(material_name)
                    
                    # Cleanup renamed images (T_*.001, T_*.010, etc)
                    AGR_OT_SimpleBake.cleanup_renamed_images(material_name)
                    
                    baked_count += 1
                    print(f"✅ Completed baking for material: {material_name}")
                
                finally:
                    # Delete bake plane
                    if bake_plane:
                        bpy.data.objects.remove(bake_plane, do_unlink=True)
                        print(f"  🗑️ Removed bake plane")
            
            # Refresh texture sets list
            texture_sets.refresh_texture_sets_list(context)

            notes = []
            if skipped_materials:
                notes.append("без Principled BSDF пропущены: " + ", ".join(skipped_materials))
            if grouped_materials:
                notes.append("Principled внутри нод-группы (вынесите на верхний "
                             "уровень материала) пропущены: " + ", ".join(grouped_materials))
            if failed_connects:
                notes.append("текстуры не подключились: " + ", ".join(failed_connects))
            if notes:
                self.report({'WARNING'},
                            f"Запечено материалов: {baked_count}; " + "; ".join(notes))
            else:
                self.report({'INFO'}, f"Simple bake complete! Baked {baked_count} materials")
        
        finally:
            # Restore render settings
            context.scene.render.engine = original_engine
            context.scene.cycles.samples = original_samples
            context.scene.cycles.use_denoising = original_denoise
            context.scene.cycles.device = original_device

            # Restore selection context
            bpy.ops.object.select_all(action='DESELECT')
            for obj in original_selection:
                if obj.name in bpy.data.objects:
                    bpy.data.objects[obj.name].select_set(True)
            if original_active and original_active.name in bpy.data.objects:
                context.view_layer.objects.active = bpy.data.objects[original_active.name]

                if original_mode != 'OBJECT':
                    try:
                        bpy.ops.object.mode_set(mode=original_mode)
                        print(f"🔄 Restored mode: {original_mode}")
                    except Exception as e:
                        print(f"⚠️ Could not restore mode {original_mode}: {e}")

            print(f"🔄 Restored selection: {len(original_selection)} objects")

        return {'FINISHED'}


classes = (
    AGR_OT_BakeTextures,
    AGR_OT_SimpleBake,
    AGR_OT_SimpleBakeAll,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    unregister_classes(classes)  # idempotent: survives a half-registered module (R-glue-4)

