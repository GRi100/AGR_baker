"""
Material to Texture Set conversion operators
"""

import bpy
from bpy.types import Operator
from bpy.props import BoolProperty
import os
from pathlib import Path
from .core.materials import connect_texture_set_to_material


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
        
        # Check if object has materials
        if not obj.material_slots:
            return False
        
        return True
    
    def execute(self, context):
        try:
            # Check for PIL
            try:
                from PIL import Image
            except ImportError:
                self.report({'ERROR'}, "Pillow not installed. Install it first.")
                return {'CANCELLED'}
            
            obj = context.active_object
            
            # Get blend file path
            blend_path = bpy.data.filepath
            if not blend_path:
                self.report({'ERROR'}, "Save blend file first")
                return {'CANCELLED'}
            
            base_dir = Path(blend_path).parent
            agr_bake_dir = base_dir / "AGR_BAKE"
            
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
                
                # Find texture nodes
                textures = self.find_material_textures(material)
                
                if not textures:
                    print(f"  ⚠️ No textures found in material")
                    continue
                
                # Create set folder
                set_name = f"S_{material.name}"
                set_folder = agr_bake_dir / set_name
                
                if not set_folder.exists():
                    set_folder.mkdir(parents=True)
                    print(f"  📁 Created folder: {set_folder}")
                
                # Process textures
                success = self.process_material_textures(material, textures, set_folder)
                
                if success:
                    # Reconnect textures to material using existing function
                    connect_texture_set_to_material(material, str(set_folder), material.name)
                    converted_count += 1
                    print(f"  ✅ Material converted and reconnected successfully")
            
            # Refresh texture sets list
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
        """Find DO, ERM, and Normal texture nodes in material"""
        textures = {
            'diffuse_opacity': None,
            'erm': None,
            'normal': None
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
            return textures
        
        # Find Diffuse/Opacity texture (connected to Base Color or Alpha)
        for input_name in ['Base Color', 'Alpha']:
            if bsdf.inputs[input_name].is_linked:
                link = bsdf.inputs[input_name].links[0]
                from_node = link.from_node
                
                # Trace back to texture node
                tex_node = self.find_texture_node(from_node)
                if tex_node and tex_node.image:
                    textures['diffuse_opacity'] = tex_node.image
                    print(f"  ✅ Found Diffuse/Opacity texture: {tex_node.image.name}")
                    break
        
        # Find ERM texture (connected to Emission/Roughness/Metallic through Separate Color)
        for input_name in ['Emission Strength', 'Roughness', 'Metallic']:
            if bsdf.inputs[input_name].is_linked:
                link = bsdf.inputs[input_name].links[0]
                from_node = link.from_node
                
                # Check if it's from Separate Color
                if from_node.type == 'SEPARATE_COLOR' or from_node.type == 'SEPRGB':
                    # Trace back to texture
                    if from_node.inputs[0].is_linked:
                        tex_link = from_node.inputs[0].links[0]
                        tex_node = self.find_texture_node(tex_link.from_node)
                        if tex_node and tex_node.image:
                            textures['erm'] = tex_node.image
                            print(f"  ✅ Found ERM texture: {tex_node.image.name}")
                            break
        
        # Find Normal texture (connected to Normal through Normal Map)
        if bsdf.inputs['Normal'].is_linked:
            link = bsdf.inputs['Normal'].links[0]
            from_node = link.from_node
            
            # Check if it's from Normal Map
            if from_node.type == 'NORMAL_MAP':
                if from_node.inputs['Color'].is_linked:
                    tex_link = from_node.inputs['Color'].links[0]
                    tex_node = self.find_texture_node(tex_link.from_node)
                    if tex_node and tex_node.image:
                        textures['normal'] = tex_node.image
                        print(f"  ✅ Found Normal texture: {tex_node.image.name}")
        
        return textures
    
    def find_texture_node(self, node):
        """Recursively find texture image node"""
        if node.type == 'TEX_IMAGE':
            return node
        
        # Check inputs for connected texture nodes
        for input in node.inputs:
            if input.is_linked:
                link = input.links[0]
                result = self.find_texture_node(link.from_node)
                if result:
                    return result
        
        return None
    
    def process_material_textures(self, material, textures, set_folder):
        """Process and save textures to set folder"""
        from PIL import Image
        import numpy as np
        
        material_name = material.name
        success = False
        
        # Process Diffuse/Opacity - ALWAYS create DO, Diffuse, and Opacity
        if textures['diffuse_opacity']:
            img = textures['diffuse_opacity']
            success = True
            
            try:
                # Get image filepath
                img_path = bpy.path.abspath(img.filepath)
                
                if not os.path.exists(img_path):
                    print(f"  ⚠️ Image file not found: {img_path}")
                else:
                    pil_img = Image.open(img_path)
                    
                    # Check if has alpha
                    if pil_img.mode in ('RGBA', 'LA'):
                        # Has alpha - split into components
                        rgb = pil_img.convert('RGB')
                        alpha = pil_img.split()[-1]
                        
                        # Save Diffuse
                        diffuse_path = set_folder / f"T_{material_name}_Diffuse.png"
                        rgb.save(str(diffuse_path))
                        print(f"  💾 Saved Diffuse: {diffuse_path.name}")
                        
                        # Save Opacity
                        opacity_path = set_folder / f"T_{material_name}_Opacity.png"
                        alpha.save(str(opacity_path))
                        print(f"  💾 Saved Opacity: {opacity_path.name}")
                        
                        # Save DiffuseOpacity (RGBA with original alpha)
                        do_path = set_folder / f"T_{material_name}_DiffuseOpacity.png"
                        pil_img.save(str(do_path))
                        print(f"  💾 Saved DiffuseOpacity (RGBA): {do_path.name}")
                    else:
                        # No alpha - save RGB without alpha channel
                        rgb = pil_img.convert('RGB')
                        
                        # Save Diffuse
                        diffuse_path = set_folder / f"T_{material_name}_Diffuse.png"
                        rgb.save(str(diffuse_path))
                        print(f"  💾 Saved Diffuse: {diffuse_path.name}")
                        
                        # Create white 256x256 Opacity
                        white_opacity = Image.new('L', (256, 256), 255)
                        opacity_path = set_folder / f"T_{material_name}_Opacity.png"
                        white_opacity.save(str(opacity_path))
                        print(f"  💾 Saved Opacity (white placeholder): {opacity_path.name}")
                        
                        # Save DiffuseOpacity as RGB (no alpha channel, just duplicate of Diffuse)
                        do_path = set_folder / f"T_{material_name}_DiffuseOpacity.png"
                        rgb.save(str(do_path))
                        print(f"  💾 Saved DiffuseOpacity (RGB, no alpha): {do_path.name}")
            
            except Exception as e:
                print(f"  ❌ Error processing Diffuse/Opacity: {e}")
        
        # Process ERM
        if textures['erm']:
            img = textures['erm']
            success = True
            
            try:
                img_path = bpy.path.abspath(img.filepath)
                
                if not os.path.exists(img_path):
                    print(f"  ⚠️ Image file not found: {img_path}")
                else:
                    pil_img = Image.open(img_path).convert('RGB')
                    r, g, b = pil_img.split()
                    
                    # Save ERM
                    erm_path = set_folder / f"T_{material_name}_ERM.png"
                    pil_img.save(str(erm_path))
                    print(f"  💾 Saved ERM: {erm_path.name}")
                    
                    # Save individual channels
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
                print(f"  ❌ Error processing ERM: {e}")
        
        # Process Normal
        if textures['normal']:
            img = textures['normal']
            success = True
            
            try:
                img_path = bpy.path.abspath(img.filepath)
                
                if not os.path.exists(img_path):
                    print(f"  ⚠️ Image file not found: {img_path}")
                else:
                    pil_img = Image.open(img_path)
                    
                    # Save Normal
                    normal_path = set_folder / f"T_{material_name}_Normal.png"
                    pil_img.save(str(normal_path))
                    print(f"  💾 Saved Normal: {normal_path.name}")
            
            except Exception as e:
                print(f"  ❌ Error processing Normal: {e}")
        
        return success


classes = (
    AGR_OT_ConvertMaterialsToSets,
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
