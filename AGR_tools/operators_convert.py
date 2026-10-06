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
from .core.texture_sets import ensure_agr_bake_folder
from .log import agr_report, unregister_classes


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


def _group_output_socket(group_node, socket):
    """Input of the group's active NodeGroupOutput matching `socket` of the
    outer group node (index first, name as fallback)."""
    tree = group_node.node_tree
    outputs = [n for n in tree.nodes if n.type == 'GROUP_OUTPUT']
    if not outputs:
        return None
    out_node = next((n for n in outputs if getattr(n, 'is_active_output', False)),
                    outputs[0])
    if socket is None:
        # No socket context: only unambiguous when the group has one link
        linked = [s for s in out_node.inputs if s.is_linked]
        return linked[0] if len(linked) == 1 else None
    try:
        idx = list(group_node.outputs).index(socket)
    except ValueError:
        idx = -1
    if 0 <= idx < len(out_node.inputs):
        return out_node.inputs[idx]
    return out_node.inputs.get(socket.name)


class AGR_OT_ConvertMaterialsToSets(Operator):
    """Convert object materials to texture sets by extracting and splitting textures"""
    bl_idname = "agr.convert_materials_to_sets"
    bl_label = "Convert Materials to Sets"
    bl_options = {'REGISTER', 'UNDO'}

    # Per-material failure notes filled by process_material_textures()
    last_errors = []

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
            # Output folder name is a user setting: writing a hardcoded
            # AGR_BAKE put the sets outside the folder the list scans
            agr_bake_dir = Path(ensure_agr_bake_folder(context))
            self.agr_bake_dir = agr_bake_dir

            print(f"\n🔄 === CONVERTING MATERIALS TO SETS ===")
            print(f"Object: {obj.name}")

            converted_count = 0
            problems = []

            for slot in obj.material_slots:
                if not slot.material:
                    continue

                material = slot.material

                if not material.use_nodes:
                    print(f"⚠️ Material {material.name}: No nodes, skipping")
                    problems.append(f"{material.name}: без нод")
                    continue

                print(f"\n📦 Processing material: {material.name}")

                textures, bsdf = self.find_material_textures(material)

                set_name = f"S_{material.name}"
                set_folder = agr_bake_dir / set_name

                # Every refusal happens BEFORE the folder is created and
                # before a single file is written
                ok, reason = self.preflight_material(material, textures, bsdf, set_folder)
                if not ok:
                    print(f"  ⛔ {reason}")
                    problems.append(reason)
                    continue

                if not set_folder.exists():
                    set_folder.mkdir(parents=True)
                    print(f"  📁 Created folder: {set_folder}")

                success = self.process_material_textures(context, material, textures, bsdf, set_folder)

                if success:
                    if connect_texture_set_to_material(material, str(set_folder), material.name) is None:
                        problems.append(f"{material.name}: текстуры записаны, но не подключились "
                                        f"(нечитаемый файл в {set_folder})")
                    converted_count += 1
                    print(f"  ✅ Material converted and reconnected successfully")
                else:
                    problems.append(f"{material.name}: "
                                    + (self.last_errors[0] if self.last_errors
                                       else "текстуры не записаны"))

            bpy.ops.agr.refresh_texture_sets(skip_alpha_strip=True)

            if problems:
                agr_report(self, 'WARNING',
                           f"Сконвертировано материалов: {converted_count}; "
                           f"пропущено: {'; '.join(problems)}")
            else:
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
            tex_node = self.find_texture_node(link.from_node, link.from_socket)
            if tex_node and tex_node.image:
                candidate_diffuse = tex_node

        if bsdf.inputs['Alpha'].is_linked:
            link = bsdf.inputs['Alpha'].links[0]
            tex_node = self.find_texture_node(link.from_node, link.from_socket)
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
                        tex_node = self.find_texture_node(tex_link.from_node, tex_link.from_socket)
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
                        tex_node = self.find_texture_node(from_node, link.from_socket)
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
                    tex_node = self.find_texture_node(tex_link.from_node, tex_link.from_socket)
                    if tex_node and tex_node.image:
                        textures['normal'] = tex_node.image
                        print(f"  ✅ Found Normal texture: {tex_node.image.name}")

        return textures, bsdf

    def find_texture_node(self, node, socket=None, visited=None):
        """Trace a link back to the TEX_IMAGE that feeds it.

        Socket-oriented on purpose: entering a GROUP jumps to that group's
        NodeGroupOutput and continues from the input matching the socket we
        arrived through — walking the group's first input instead would
        happily return a texture belonging to another output.  Without the
        descent the converter saw no texture at all and wrote a flat colour
        stub over an existing set (core/baking.py and material_images have
        descended into groups for releases).
        """
        if node is None:
            return None
        if visited is None:
            visited = set()
        key = (node.as_pointer(), socket.identifier if socket is not None else "")
        if key in visited:
            return None
        visited.add(key)

        if node.type == 'TEX_IMAGE':
            return node

        if node.type == 'GROUP' and node.node_tree is not None:
            inner = _group_output_socket(node, socket)
            if inner is not None and inner.is_linked:
                link = inner.links[0]
                return self.find_texture_node(link.from_node, link.from_socket, visited)
            return None

        # A texture living OUTSIDE the group, fed in through NodeGroupInput,
        # cannot be resolved without the outer node — treated as "not found"
        if node.type == 'GROUP_INPUT':
            return None

        for inp in node.inputs:
            if inp.is_linked:
                link = inp.links[0]
                result = self.find_texture_node(link.from_node, link.from_socket, visited)
                if result:
                    return result

        return None

    def preflight_material(self, material, textures, bsdf, set_folder):
        """Decide whether this material may be converted — BEFORE the set
        folder is created and before a single file is written.
        Returns (ok, reason)."""
        if bsdf is None:
            return False, (f"{material.name}: нет Principled BSDF "
                           "(стекло/эмиссия) — конвертация невозможна")

        if textures.get('diffuse') or textures.get('diffuse_opacity'):
            return True, ""

        # No diffuse source. A flat Base Color fill is the DOCUMENTED
        # behaviour for a bare-colour material — but only then.
        if bsdf.inputs['Base Color'].is_linked:
            return False, (f"{material.name}: Base Color подключён, но текстуру "
                           "проследить не удалось — заглушка не записана")

        # CONV-1 protects REAL textures from being replaced by a flat stub.
        # A stub written by this very converter is always 256×256 (the flat
        # branch hardcodes res = 256), so refusing to overwrite it only made
        # the user delete the folder by hand after tweaking the Base Color.
        do_path = set_folder / f"T_{material.name}_DiffuseOpacity.png"
        if do_path.exists():
            from .core.texture_sets import read_png_ihdr
            width, height, color_type = read_png_ihdr(str(do_path))
            # color_type < 0 = unreadable: never overwrite what cannot be
            # identified as our own stub.
            if color_type < 0 or max(width, height) > 256:
                return False, (f"{material.name}: голый цвет, но сет S_{material.name} "
                               "уже существует — перезапись заглушкой отменена")

        return True, ""

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
            # Same output folder the sets go into (a user setting)
            agr_bake_dir = Path(getattr(self, 'agr_bake_dir', base_dir / "AGR_BAKE"))
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
                pixels = np.empty(img.size[0] * img.size[1] * 4, dtype=np.float32)
                img.pixels.foreach_get(pixels)
                pixels = pixels.reshape(img.size[1], img.size[0], 4)
                # Flip vertically: Blender pixels are bottom-to-top, Pillow expects top-to-bottom
                pixels = np.flipud(pixels)
                # img.pixels is SCENE-LINEAR float: writing it straight into
                # a PNG made mid-tones about twice as dark (0.5 -> 54 instead
                # of 128), and an HDR value > 1.0 wrapped around modulo 256
                # into colour noise. Encode colour, clamp everything, leave
                # alpha linear.
                rgb = np.clip(pixels[:, :, :3], 0.0, 1.0)
                low = rgb <= 0.0031308
                rgb = np.where(low, rgb * 12.92,
                               1.055 * np.power(np.clip(rgb, 1e-8, 1.0), 1.0 / 2.4) - 0.055)
                alpha = np.clip(pixels[:, :, 3:4], 0.0, 1.0)
                arr = np.rint(np.concatenate((rgb, alpha), axis=2) * 255.0)
                arr = np.clip(arr, 0, 255).astype('uint8')
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
                        _in_link = from_node.inputs[0].links[0]
                        tex_node = self.find_texture_node(_in_link.from_node, _in_link.from_socket)
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
        # Every branch used to raise its flag BEFORE the try, so a failed
        # load/save still reported success and the set ended up a mix of
        # the previous DiffuseOpacity and fresh flat ERM/Normal stubs.
        self.last_errors = []

        # ── Diffuse / Opacity ────────────────────────────────────────────────

        if textures['diffuse'] and textures['opacity']:
            # Separate diffuse and opacity textures
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

                    diffuse_ok = True
                else:
                    self.last_errors.append("не удалось прочитать Diffuse/Opacity")

            except Exception as e:
                print(f"  ❌ Error processing separate Diffuse/Opacity: {e}")
                self.last_errors.append(f"Diffuse/Opacity: {e}")

        elif textures['diffuse']:
            # Diffuse only — no Alpha connection
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

                    diffuse_ok = True
                else:
                    self.last_errors.append("не удалось прочитать Diffuse")

            except Exception as e:
                print(f"  ❌ Error processing Diffuse: {e}")
                self.last_errors.append(f"Diffuse: {e}")

        elif textures['diffuse_opacity']:
            # Packed RGBA or RGB texture
            img = textures['diffuse_opacity']
            try:
                pil_img = self.load_pil_image(img)
                if pil_img:
                    # A palette PNG with tRNS (the usual foliage cut-out
                    # export) reports mode 'P' — converting it to RGB threw
                    # the alpha away silently, so promote it to RGBA first.
                    if pil_img.mode in ('P', 'PA') or 'transparency' in pil_img.info:
                        pil_img = pil_img.convert('RGBA')
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

                    diffuse_ok = True
                else:
                    self.last_errors.append("не удалось прочитать DiffuseOpacity")

            except Exception as e:
                print(f"  ❌ Error processing Diffuse/Opacity: {e}")
                self.last_errors.append(f"DiffuseOpacity: {e}")

        else:
            # No diffuse texture — flat colour from BSDF Base Color.  This
            # branch is only ever reached for a genuinely bare-colour
            # material: preflight_material() already refused the "Base Color
            # linked but untraceable" and "set already on disk" cases.
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
                self.last_errors.append(f"плоский Diffuse: {e}")

        if not diffuse_ok:
            # Writing ERM/Normal now would leave the folder as a mix of the
            # PREVIOUS diffuse and fresh stubs — worse than not touching it
            print("  ⛔ Diffuse part failed — ERM/Normal not written")
            return False

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

                    erm_ok = True
                else:
                    self.last_errors.append("не удалось прочитать ERM")

            except Exception as e:
                print(f"  ❌ Error processing packed ERM: {e}")
                self.last_errors.append(f"ERM: {e}")

        else:
            has_any_erm_tex = any(textures[k] for k in ('emit', 'roughness', 'metallic'))

            if has_any_erm_tex:
                # Build ERM from separately connected textures;
                # missing channels are filled with BSDF scalar values
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
                    erm_ok = True

                except Exception as e:
                    print(f"  ❌ Error assembling ERM from separate channels: {e}")
                    self.last_errors.append(f"ERM: {e}")

            else:
                # No ERM textures at all — create flat 256x256 from BSDF values
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
                    erm_ok = True

                except Exception as e:
                    print(f"  ❌ Error creating flat ERM: {e}")
                    self.last_errors.append(f"плоский ERM: {e}")

        # ── Normal ───────────────────────────────────────────────────────────

        if textures['normal']:
            try:
                pil_img = self.load_pil_image(textures['normal'])
                if pil_img:
                    normal_path = set_folder / f"T_{material_name}_Normal.png"
                    pil_img.convert('RGB').save(str(normal_path))
                    print(f"  💾 Saved Normal: {normal_path.name}")
                    normal_ok = True
                else:
                    self.last_errors.append("не удалось прочитать Normal")

            except Exception as e:
                print(f"  ❌ Error processing Normal: {e}")
                self.last_errors.append(f"Normal: {e}")

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
                self.last_errors.append(f"плоский Normal: {e}")

        # Diffuse is mandatory; ERM and Normal are generated as flat fallbacks,
        # so they should always succeed unless an exception occurred.
        return diffuse_ok


class AGR_OT_ConvertActiveMaterialToSet(Operator):
    """Convert only the active material of the active object to a texture set"""
    bl_idname = "agr.convert_active_material_to_set"
    bl_label = "Convert Active Material to Set"
    bl_options = {'REGISTER', 'UNDO'}

    # Per-material failure notes filled by process_material_textures()
    last_errors = []

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
            agr_bake_dir = Path(ensure_agr_bake_folder(context))
            self.agr_bake_dir = agr_bake_dir

            material = obj.active_material

            print(f"\n🔄 === CONVERTING ACTIVE MATERIAL TO SET ===")
            print(f"Object: {obj.name}, Material: {material.name}")

            textures, bsdf = self.find_material_textures(material)

            set_name = f"S_{material.name}"
            set_folder = agr_bake_dir / set_name

            # Refuse before the folder exists and before any file is written
            ok, reason = self.preflight_material(material, textures, bsdf, set_folder)
            if not ok:
                agr_report(self, 'ERROR', reason)
                return {'CANCELLED'}

            if not set_folder.exists():
                set_folder.mkdir(parents=True)
                print(f"  📁 Created folder: {set_folder}")

            success = self.process_material_textures(context, material, textures, bsdf, set_folder)

            if success:
                if connect_texture_set_to_material(material, str(set_folder), material.name) is None:
                    agr_report(self, 'WARNING',
                               f"{material.name}: текстуры записаны, но не подключились "
                               f"(нечитаемый файл в {set_folder})")
                print(f"  ✅ Material converted and reconnected successfully")

            bpy.ops.agr.refresh_texture_sets(skip_alpha_strip=True)

            if success:
                self.report({'INFO'}, f"Converted active material: {material.name}")
            else:
                agr_report(self, 'WARNING',
                           f"Конвертация не завершена: {material.name} — "
                           + ("; ".join(self.last_errors) or "текстуры не записаны"))

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
    'preflight_material',
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
    unregister_classes(classes)  # idempotent: survives a half-registered module (R-glue-4)
    print("Conversion operators unregistered")
