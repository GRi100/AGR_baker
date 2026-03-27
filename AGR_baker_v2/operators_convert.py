"""
Material to Texture Set conversion operators
"""

import bpy
from bpy.types import Operator
from bpy.props import BoolProperty
import os
from pathlib import Path
from .core.materials import connect_texture_set_to_material
from .core import baking as baking_core


def _linear_to_srgb(c):
    """Convert a single linear float channel (0.0-1.0) to sRGB (0-255 int).
    Blender stores Base Color in linear space; PNG files are interpreted as sRGB.
    """
    c = max(0.0, min(1.0, c))
    if c <= 0.0031308:
        encoded = c * 12.92
    else:
        encoded = 1.055 * (c ** (1.0 / 2.4)) - 0.055
    return int(encoded * 255 + 0.5)


class AGR_OT_ConvertMaterialsToSets(Operator):
    """Convert object materials to texture sets by extracting and splitting textures"""
    bl_idname = "agr.convert_materials_to_sets"
    bl_label = "Convert Materials to Sets"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            return False

        if not obj.material_slots:
            return False

        return True

    def execute(self, context):
        try:
            try:
                from PIL import Image
            except ImportError:
                self.report({'ERROR'}, "Pillow not installed. Install it first.")
                return {'CANCELLED'}

            obj = context.active_object

            blend_path = bpy.data.filepath
            if not blend_path:
                self.report({'ERROR'}, "Save blend file first")
                return {'CANCELLED'}

            self.base_dir = Path(blend_path).parent
            agr_bake_dir = self.base_dir / "AGR_BAKE"

            if not agr_bake_dir.exists():
                agr_bake_dir.mkdir(parents=True)

            print(f"\n🔄 === CONVERTING MATERIALS TO SETS ===")
            print(f"Object: {obj.name}")

            converted_count = 0

            for slot in obj.material_slots:
                if not slot.material:
                    continue

                material = slot.material

                if not material.use_nodes:
                    print(f"⚠️ Material {material.name}: No nodes, skipping")
                    continue

                print(f"\n📦 Processing material: {material.name}")

                textures, bsdf = self.find_material_textures(material)

                set_name = f"S_{material.name}"
                set_folder = agr_bake_dir / set_name

                if not set_folder.exists():
                    set_folder.mkdir(parents=True)
                    print(f"  📁 Created folder: {set_folder}")

                success = self.process_material_textures(context, material, textures, bsdf, set_folder)

                if success:
                    connect_texture_set_to_material(material, str(set_folder), material.name)
                    converted_count += 1
                    print(f"  ✅ Material converted and reconnected successfully")

            bpy.ops.agr.refresh_texture_sets()

            self.report({'INFO'}, f"Converted {converted_count} materials to texture sets")
            print(f"\n✅ Conversion complete: {converted_count} materials")

            return {'FINISHED'}

        except Exception as e:
            print(f"❌ Error converting materials: {str(e)}")
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, f"Error: {str(e)}")
            return {'CANCELLED'}

    def find_material_textures(self, material):
        """Find diffuse/opacity, ERM, and Normal texture nodes in material.

        Supports:
        - Packed RGBA DiffuseOpacity texture
        - Separate Diffuse + Opacity textures
        - Packed ERM via Separate Color node
        - Separate E/R/M textures connected individually

        Returns (textures dict, bsdf node or None).
        """
        textures = {
            'diffuse_opacity': None,   # single packed RGBA texture
            'diffuse': None,           # separate diffuse texture
            'opacity': None,           # separate opacity texture
            'erm': None,               # single packed ERM texture
            'emit': None,              # separate Emit texture
            'roughness': None,         # separate Roughness texture
            'metallic': None,          # separate Metallic texture
            'normal': None,
        }

        nodes = material.node_tree.nodes

        # Find BSDF node
        bsdf = None
        for node in nodes:
            if node.type == 'BSDF_PRINCIPLED':
                bsdf = node
                break

        if not bsdf:
            print("  ⚠️ No Principled BSDF found")
            return textures, bsdf

        # ── Diffuse / Opacity detection ──────────────────────────────────────
        # Detect candidate nodes on Base Color and Alpha separately, then compare
        candidate_diffuse = None
        candidate_opacity = None

        if bsdf.inputs['Base Color'].is_linked:
            link = bsdf.inputs['Base Color'].links[0]
            tex_node = self.find_texture_node(link.from_node)
            if tex_node and tex_node.image:
                candidate_diffuse = tex_node

        if bsdf.inputs['Alpha'].is_linked:
            link = bsdf.inputs['Alpha'].links[0]
            tex_node = self.find_texture_node(link.from_node)
            if tex_node and tex_node.image:
                candidate_opacity = tex_node

        if candidate_diffuse and candidate_opacity:
            if candidate_diffuse == candidate_opacity:
                # Same node — packed RGBA texture
                textures['diffuse_opacity'] = candidate_diffuse.image
                print(f"  ✅ Found packed DiffuseOpacity: {candidate_diffuse.image.name}")
            else:
                # Different nodes — separate diffuse and opacity
                textures['diffuse'] = candidate_diffuse.image
                textures['opacity'] = candidate_opacity.image
                print(f"  ✅ Found separate Diffuse: {candidate_diffuse.image.name}")
                print(f"  ✅ Found separate Opacity: {candidate_opacity.image.name}")
        elif candidate_diffuse:
            # Only Base Color connected, no Alpha — treat as diffuse_opacity (may be RGBA or RGB)
            textures['diffuse_opacity'] = candidate_diffuse.image
            print(f"  ✅ Found Diffuse/Opacity texture: {candidate_diffuse.image.name}")

        # ── ERM detection ─────────────────────────────────────────────────────
        # First try packed ERM via Separate Color node
        for input_name in ['Emission Strength', 'Roughness', 'Metallic']:
            if bsdf.inputs[input_name].is_linked:
                link = bsdf.inputs[input_name].links[0]
                from_node = link.from_node

                if from_node.type in ('SEPARATE_COLOR', 'SEPRGB'):
                    if from_node.inputs[0].is_linked:
                        tex_link = from_node.inputs[0].links[0]
                        tex_node = self.find_texture_node(tex_link.from_node)
                        if tex_node and tex_node.image:
                            textures['erm'] = tex_node.image
                            print(f"  ✅ Found packed ERM texture: {tex_node.image.name}")
                            break

        # If no packed ERM, check for individually connected E/R/M textures
        if not textures['erm']:
            erm_map = {
                'Emission Strength': 'emit',
                'Roughness': 'roughness',
                'Metallic': 'metallic',
            }
            for input_name, key in erm_map.items():
                if bsdf.inputs[input_name].is_linked:
                    link = bsdf.inputs[input_name].links[0]
                    from_node = link.from_node
                    # Direct texture connection (not through Separate Color)
                    if from_node.type not in ('SEPARATE_COLOR', 'SEPRGB'):
                        tex_node = self.find_texture_node(from_node)
                        if tex_node and tex_node.image:
                            textures[key] = tex_node.image
                            print(f"  ✅ Found separate {key}: {tex_node.image.name}")

        # ── Normal detection ──────────────────────────────────────────────────
        if bsdf.inputs['Normal'].is_linked:
            link = bsdf.inputs['Normal'].links[0]
            from_node = link.from_node

            if from_node.type == 'NORMAL_MAP':
                if from_node.inputs['Color'].is_linked:
                    tex_link = from_node.inputs['Color'].links[0]
                    tex_node = self.find_texture_node(tex_link.from_node)
                    if tex_node and tex_node.image:
                        textures['normal'] = tex_node.image
                        print(f"  ✅ Found Normal texture: {tex_node.image.name}")

        return textures, bsdf

    def find_texture_node(self, node):
        """Recursively find texture image node"""
        if node.type == 'TEX_IMAGE':
            return node

        for inp in node.inputs:
            if inp.is_linked:
                link = inp.links[0]
                result = self.find_texture_node(link.from_node)
                if result:
                    return result

        return None

    def resolve_image_path(self, img):
        """Resolve a bpy image to a file path with three fallback levels:
        1. Standard bpy.path.abspath check
        2. Search by filename in the project directory tree
        3. Save packed image to a temporary file

        Returns absolute path string or None.
        """
        base_dir = self.base_dir

        # Step 1: standard path resolution
        if img.filepath:
            path = bpy.path.abspath(img.filepath)
            if path and os.path.exists(path):
                return path

        # Step 2: search by filename in project directory
        raw_name = img.filepath_raw if img.filepath_raw else ""
        filename = os.path.basename(raw_name) if raw_name else ""
        if not filename:
            filename = img.name
            if not filename.lower().endswith('.png'):
                filename = filename + '.png'

        if filename:
            for pattern in (filename, f"*/{filename}", f"*/*/{filename}", f"*/*/*/{filename}", f"*/*/*/*/{filename}", f"*/*/*/*/*/{filename}", f"*/*/*/*/*/*/{filename}"):
                for found in base_dir.glob(pattern):
                    print(f"  🔍 Found by name search: {found}")
                    return str(found)

        # Step 3: packed image — save to temp file
        if img.packed_file:
            agr_bake_dir = base_dir / "AGR_BAKE"
            agr_bake_dir.mkdir(parents=True, exist_ok=True)

            safe_name = img.name.replace('/', '_').replace('\\', '_').replace(':', '_')
            if not safe_name.lower().endswith('.png'):
                safe_name = safe_name + '.png'
            temp_path = agr_bake_dir / f"_packed_temp_{safe_name}"

            original_filepath_raw = img.filepath_raw
            original_file_format = img.file_format
            try:
                img.file_format = 'PNG'
                img.filepath_raw = str(temp_path)
                img.save()
                print(f"  📦 Extracted packed image to temp: {temp_path.name}")
                return str(temp_path)
            except Exception as e:
                print(f"  ⚠️ Failed to save packed image {img.name}: {e}")
            finally:
                img.filepath_raw = original_filepath_raw
                img.file_format = original_file_format

        print(f"  ⚠️ Could not resolve path for image: {img.name}")
        return None

    def load_pil_image(self, img):
        """Load a bpy image as PIL Image.
        Falls back to reading pixel data directly if file is not on disk.
        Returns PIL Image or None.
        """
        from PIL import Image

        path = self.resolve_image_path(img)
        if path and os.path.exists(path):
            pil = Image.open(path)
            pil.load()  # force full read into memory before potential temp cleanup
            if os.path.basename(path).startswith('_packed_temp_'):
                try:
                    os.remove(path)
                except Exception:
                    pass
            return pil

        # Last resort: use bpy pixel buffer (works for any loaded/packed image)
        if img.size[0] > 0 and img.size[1] > 0:
            try:
                import numpy as np
                pixels = np.array(img.pixels[:]).reshape(img.size[1], img.size[0], 4)
                # Flip vertically: Blender pixels are bottom-to-top, Pillow expects top-to-bottom
                arr = (np.flipud(pixels) * 255).astype('uint8')
                return Image.fromarray(arr, 'RGBA')
            except Exception as e:
                print(f"  ⚠️ Failed to load image via pixel buffer {img.name}: {e}")

        return None

    def _get_erm_channel_connections(self, bsdf, erm_image):
        """Check which ERM channels are actually routed through Separate Color to the BSDF.
        Returns dict {'emit': bool, 'roughness': bool, 'metallic': bool}.
        A False value means that BSDF input is NOT connected through the ERM texture,
        so its default_value should be used instead of the corresponding texture channel.
        """
        channels = {'emit': False, 'roughness': False, 'metallic': False}
        mapping = [
            ('Emission Strength', 'emit'),
            ('Roughness', 'roughness'),
            ('Metallic', 'metallic'),
        ]
        for input_name, key in mapping:
            if bsdf.inputs[input_name].is_linked:
                link = bsdf.inputs[input_name].links[0]
                from_node = link.from_node
                if from_node.type in ('SEPARATE_COLOR', 'SEPRGB'):
                    if from_node.inputs[0].is_linked:
                        tex_node = self.find_texture_node(from_node.inputs[0].links[0].from_node)
                        if tex_node and tex_node.image == erm_image:
                            channels[key] = True
        return channels

    def process_material_textures(self, context, material, textures, bsdf, set_folder):
        """Process and save textures to set folder"""
        from PIL import Image
        import numpy as np

        material_name = material.name
        diffuse_ok = False
        erm_ok = False
        normal_ok = False

        # ── Diffuse / Opacity ────────────────────────────────────────────────

        if textures['diffuse'] and textures['opacity']:
            # Separate diffuse and opacity textures
            diffuse_ok = True
            try:
                pil_diffuse = self.load_pil_image(textures['diffuse'])
                pil_opacity = self.load_pil_image(textures['opacity'])

                if pil_diffuse and pil_opacity:
                    rgb = pil_diffuse.convert('RGB')
                    opacity_l = pil_opacity.convert('L').resize(rgb.size)

                    # Check if opacity is fully white (no transparency)
                    opacity_arr = np.array(opacity_l)
                    opacity_is_white = bool(np.all(opacity_arr == 255))

                    diffuse_path = set_folder / f"T_{material_name}_Diffuse.png"
                    rgb.save(str(diffuse_path))
                    print(f"  💾 Saved Diffuse: {diffuse_path.name}")

                    opacity_path = set_folder / f"T_{material_name}_Opacity.png"
                    opacity_l.save(str(opacity_path))
                    print(f"  💾 Saved Opacity: {opacity_path.name}")

                    do_path = set_folder / f"T_{material_name}_DiffuseOpacity.png"
                    if opacity_is_white:
                        # Fully opaque — save as RGB (no alpha channel needed)
                        rgb.save(str(do_path))
                        print(f"  💾 Saved DiffuseOpacity (RGB, opacity fully white): {do_path.name}")
                    else:
                        # Has transparency — merge as RGBA
                        rgba = rgb.copy()
                        rgba.putalpha(opacity_l)
                        rgba.save(str(do_path))
                        print(f"  💾 Saved DiffuseOpacity (RGBA): {do_path.name}")

            except Exception as e:
                print(f"  ❌ Error processing separate Diffuse/Opacity: {e}")

        elif textures['diffuse']:
            # Diffuse only — no Alpha connection
            diffuse_ok = True
            try:
                pil_img = self.load_pil_image(textures['diffuse'])
                if pil_img:
                    rgb = pil_img.convert('RGB')

                    diffuse_path = set_folder / f"T_{material_name}_Diffuse.png"
                    rgb.save(str(diffuse_path))
                    print(f"  💾 Saved Diffuse: {diffuse_path.name}")

                    white_opacity = Image.new('L', rgb.size, 255)
                    opacity_path = set_folder / f"T_{material_name}_Opacity.png"
                    white_opacity.save(str(opacity_path))
                    print(f"  💾 Saved Opacity (white placeholder): {opacity_path.name}")

                    do_path = set_folder / f"T_{material_name}_DiffuseOpacity.png"
                    rgb.save(str(do_path))
                    print(f"  💾 Saved DiffuseOpacity (RGB, no alpha): {do_path.name}")

            except Exception as e:
                print(f"  ❌ Error processing Diffuse: {e}")

        elif textures['diffuse_opacity']:
            # Packed RGBA or RGB texture
            img = textures['diffuse_opacity']
            diffuse_ok = True
            try:
                pil_img = self.load_pil_image(img)
                if pil_img:
                    if pil_img.mode in ('RGBA', 'LA'):
                        rgb = pil_img.convert('RGB')
                        alpha = pil_img.split()[-1]

                        diffuse_path = set_folder / f"T_{material_name}_Diffuse.png"
                        rgb.save(str(diffuse_path))
                        print(f"  💾 Saved Diffuse: {diffuse_path.name}")

                        alpha_is_white = min(alpha.getdata()) == 255
                        if alpha_is_white:
                            # Alpha fully white — no real transparency, save as RGB
                            print(f"  ℹ️ Alpha channel is fully white — treating as opaque")
                            white_opacity = Image.new('L', rgb.size, 255)
                            opacity_path = set_folder / f"T_{material_name}_Opacity.png"
                            white_opacity.save(str(opacity_path))
                            print(f"  💾 Saved Opacity (white placeholder): {opacity_path.name}")

                            do_path = set_folder / f"T_{material_name}_DiffuseOpacity.png"
                            rgb.save(str(do_path))
                            print(f"  💾 Saved DiffuseOpacity (RGB, alpha was white): {do_path.name}")
                        else:
                            opacity_path = set_folder / f"T_{material_name}_Opacity.png"
                            alpha.save(str(opacity_path))
                            print(f"  💾 Saved Opacity: {opacity_path.name}")

                            do_path = set_folder / f"T_{material_name}_DiffuseOpacity.png"
                            pil_img.save(str(do_path))
                            print(f"  💾 Saved DiffuseOpacity (RGBA): {do_path.name}")
                    else:
                        rgb = pil_img.convert('RGB')

                        diffuse_path = set_folder / f"T_{material_name}_Diffuse.png"
                        rgb.save(str(diffuse_path))
                        print(f"  💾 Saved Diffuse: {diffuse_path.name}")

                        white_opacity = Image.new('L', rgb.size, 255)
                        opacity_path = set_folder / f"T_{material_name}_Opacity.png"
                        white_opacity.save(str(opacity_path))
                        print(f"  💾 Saved Opacity (white placeholder): {opacity_path.name}")

                        do_path = set_folder / f"T_{material_name}_DiffuseOpacity.png"
                        rgb.save(str(do_path))
                        print(f"  💾 Saved DiffuseOpacity (RGB, no alpha): {do_path.name}")

            except Exception as e:
                print(f"  ❌ Error processing Diffuse/Opacity: {e}")

        else:
            # No diffuse texture — create flat color from BSDF Base Color
            print(f"  ⚠️ No Diffuse texture found — creating from BSDF Base Color")
            try:
                res = 256
                if bsdf:
                    bc = bsdf.inputs['Base Color'].default_value
                    r, g, b = (_linear_to_srgb(bc[i]) for i in range(3))
                else:
                    r, g, b = 204, 204, 204

                flat = Image.new('RGB', (res, res), (r, g, b))

                flat.save(str(set_folder / f"T_{material_name}_Diffuse.png"))
                Image.new('L', (res, res), 255).save(str(set_folder / f"T_{material_name}_Opacity.png"))
                flat.save(str(set_folder / f"T_{material_name}_DiffuseOpacity.png"))

                diffuse_ok = True
            except Exception as e:
                print(f"  ❌ Error creating flat Diffuse: {e}")

        # ── ERM ──────────────────────────────────────────────────────────────

        # Read BSDF fallback scalar values (used when textures are missing)
        emit_val = 0.0
        rough_val = 0.5
        metal_val = 0.0
        if bsdf:
            emit_val = float(bsdf.inputs['Emission Strength'].default_value)
            rough_val = float(bsdf.inputs['Roughness'].default_value)
            metal_val = float(bsdf.inputs['Metallic'].default_value)

        if textures['erm']:
            # Packed ERM texture — check which channels are actually wired to the BSDF
            erm_ok = True
            try:
                pil_img = self.load_pil_image(textures['erm'])
                if pil_img:
                    pil_img = pil_img.convert('RGB')
                    r, g, b = pil_img.split()

                    # Determine which outputs of the Separate Color are connected to BSDF
                    erm_conn = self._get_erm_channel_connections(bsdf, textures['erm']) if bsdf else {}

                    # Replace disconnected channels with flat fill from BSDF default values
                    if not erm_conn.get('emit', True):
                        fill = int(min(max(emit_val, 0.0), 1.0) * 255)
                        r = Image.new('L', pil_img.size, fill)
                        print(f"  ⚠️ Emit not connected to BSDF — using BSDF value ({fill})")

                    if not erm_conn.get('roughness', True):
                        fill = int(min(max(rough_val, 0.0), 1.0) * 255)
                        g = Image.new('L', pil_img.size, fill)
                        print(f"  ⚠️ Roughness not connected to BSDF — using BSDF value ({fill})")

                    if not erm_conn.get('metallic', True):
                        fill = int(min(max(metal_val, 0.0), 1.0) * 255)
                        b = Image.new('L', pil_img.size, fill)
                        print(f"  ⚠️ Metallic not connected to BSDF — using BSDF value ({fill})")

                    # Rebuild ERM from (possibly patched) channels
                    erm_final = Image.merge('RGB', (r, g, b))

                    erm_path = set_folder / f"T_{material_name}_ERM.png"
                    erm_final.save(str(erm_path))
                    print(f"  💾 Saved ERM: {erm_path.name}")

                    emit_path = set_folder / f"T_{material_name}_Emit.png"
                    r.save(str(emit_path))
                    print(f"  💾 Saved Emit: {emit_path.name}")

                    roughness_path = set_folder / f"T_{material_name}_Roughness.png"
                    g.save(str(roughness_path))
                    print(f"  💾 Saved Roughness: {roughness_path.name}")

                    metallic_path = set_folder / f"T_{material_name}_Metallic.png"
                    b.save(str(metallic_path))
                    print(f"  💾 Saved Metallic: {metallic_path.name}")

            except Exception as e:
                print(f"  ❌ Error processing packed ERM: {e}")

        else:
            has_any_erm_tex = any(textures[k] for k in ('emit', 'roughness', 'metallic'))

            if has_any_erm_tex:
                # Build ERM from separately connected textures;
                # missing channels are filled with BSDF scalar values
                erm_ok = True
                try:
                    # Determine output resolution from found textures
                    width, height = 256, 256
                    for key in ('emit', 'roughness', 'metallic'):
                        if textures[key]:
                            pil_test = self.load_pil_image(textures[key])
                            if pil_test:
                                w, h = pil_test.size
                                if w * h > width * height:
                                    width, height = w, h

                    def load_channel(img_key, fallback_val):
                        """Return grayscale channel image or solid fill from BSDF value."""
                        if textures[img_key]:
                            pil = self.load_pil_image(textures[img_key])
                            if pil:
                                return pil.convert('L').resize((width, height))
                        fill = int(min(max(fallback_val, 0.0), 1.0) * 255)
                        return Image.new('L', (width, height), fill)

                    r_ch = load_channel('emit', emit_val)
                    g_ch = load_channel('roughness', rough_val)
                    b_ch = load_channel('metallic', metal_val)

                    erm_img = Image.merge('RGB', (r_ch, g_ch, b_ch))
                    erm_path = set_folder / f"T_{material_name}_ERM.png"
                    erm_img.save(str(erm_path))
                    print(f"  💾 Saved ERM (assembled from separate channels): {erm_path.name}")

                    emit_path = set_folder / f"T_{material_name}_Emit.png"
                    r_ch.save(str(emit_path))
                    roughness_path = set_folder / f"T_{material_name}_Roughness.png"
                    g_ch.save(str(roughness_path))
                    metallic_path = set_folder / f"T_{material_name}_Metallic.png"
                    b_ch.save(str(metallic_path))
                    print(f"  💾 Saved individual ERM channels")

                except Exception as e:
                    print(f"  ❌ Error assembling ERM from separate channels: {e}")

            else:
                # No ERM textures at all — create flat 256x256 from BSDF values
                erm_ok = True
                try:
                    r_val = int(min(max(emit_val, 0.0), 1.0) * 255)
                    g_val = int(min(max(rough_val, 0.0), 1.0) * 255)
                    b_val = int(min(max(metal_val, 0.0), 1.0) * 255)

                    flat_erm = Image.new('RGB', (256, 256), (r_val, g_val, b_val))
                    erm_path = set_folder / f"T_{material_name}_ERM.png"
                    flat_erm.save(str(erm_path))
                    print(f"  💾 Saved ERM (flat from BSDF E={r_val} R={g_val} M={b_val}): {erm_path.name}")

                    Image.new('L', (256, 256), r_val).save(str(set_folder / f"T_{material_name}_Emit.png"))
                    Image.new('L', (256, 256), g_val).save(str(set_folder / f"T_{material_name}_Roughness.png"))
                    Image.new('L', (256, 256), b_val).save(str(set_folder / f"T_{material_name}_Metallic.png"))
                    print(f"  💾 Saved flat individual ERM channels")

                except Exception as e:
                    print(f"  ❌ Error creating flat ERM: {e}")

        # ── Normal ───────────────────────────────────────────────────────────

        if textures['normal']:
            normal_ok = True
            try:
                pil_img = self.load_pil_image(textures['normal'])
                if pil_img:
                    normal_path = set_folder / f"T_{material_name}_Normal.png"
                    pil_img.convert('RGB').save(str(normal_path))
                    print(f"  💾 Saved Normal: {normal_path.name}")

            except Exception as e:
                print(f"  ❌ Error processing Normal: {e}")

        else:
            # No normal texture — create flat tangent-space normal (128, 128, 255)
            try:
                flat_normal = Image.new('RGB', (256, 256), (128, 128, 255))
                normal_path = set_folder / f"T_{material_name}_Normal.png"
                flat_normal.save(str(normal_path))
                print(f"  💾 Saved Normal (flat tangent-space 128,128,255): {normal_path.name}")
                normal_ok = True
            except Exception as e:
                print(f"  ❌ Error creating flat Normal: {e}")

        # Diffuse is mandatory; ERM and Normal are generated as flat fallbacks,
        # so they should always succeed unless an exception occurred.
        return diffuse_ok


class AGR_OT_ConvertActiveMaterialToSet(Operator):
    """Convert only the active material of the active object to a texture set"""
    bl_idname = "agr.convert_active_material_to_set"
    bl_label = "Convert Active Material to Set"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (obj and obj.type == 'MESH' and obj.active_material
                and obj.active_material.use_nodes)

    def execute(self, context):
        try:
            try:
                from PIL import Image
            except ImportError:
                self.report({'ERROR'}, "Pillow not installed. Install it first.")
                return {'CANCELLED'}

            obj = context.active_object

            blend_path = bpy.data.filepath
            if not blend_path:
                self.report({'ERROR'}, "Save blend file first")
                return {'CANCELLED'}

            self.base_dir = Path(blend_path).parent
            agr_bake_dir = self.base_dir / "AGR_BAKE"

            if not agr_bake_dir.exists():
                agr_bake_dir.mkdir(parents=True)

            material = obj.active_material

            print(f"\n🔄 === CONVERTING ACTIVE MATERIAL TO SET ===")
            print(f"Object: {obj.name}, Material: {material.name}")

            textures, bsdf = self.find_material_textures(material)

            set_name = f"S_{material.name}"
            set_folder = agr_bake_dir / set_name

            if not set_folder.exists():
                set_folder.mkdir(parents=True)
                print(f"  📁 Created folder: {set_folder}")

            success = self.process_material_textures(context, material, textures, bsdf, set_folder)

            if success:
                connect_texture_set_to_material(material, str(set_folder), material.name)
                print(f"  ✅ Material converted and reconnected successfully")

            bpy.ops.agr.refresh_texture_sets()

            if success:
                self.report({'INFO'}, f"Converted active material: {material.name}")
            else:
                self.report({'WARNING'}, f"Conversion incomplete for: {material.name}")

            print(f"\n✅ Active material conversion complete")
            return {'FINISHED'}

        except Exception as e:
            print(f"❌ Error converting active material: {str(e)}")
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, f"Error: {str(e)}")
            return {'CANCELLED'}


# Copy helper methods from AGR_OT_ConvertMaterialsToSets so that
# AGR_OT_ConvertActiveMaterialToSet can use self.method() without inheriting
# from a registered Blender operator class (which causes RNA struct conflicts).
_SHARED_METHODS = (
    'find_material_textures',
    'find_texture_node',
    'resolve_image_path',
    'load_pil_image',
    '_get_erm_channel_connections',
    'process_material_textures',
)
for _m in _SHARED_METHODS:
    setattr(AGR_OT_ConvertActiveMaterialToSet, _m,
            getattr(AGR_OT_ConvertMaterialsToSets, _m))


classes = (
    AGR_OT_ConvertMaterialsToSets,
    AGR_OT_ConvertActiveMaterialToSet,
)


def register():
    """Register conversion operators"""
    for cls in classes:
        bpy.utils.register_class(cls)
    print("✅ Conversion operators registered")


def unregister():
    """Unregister conversion operators"""
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    print("Conversion operators unregistered")
