# Headless test for AGR Convert (operators_convert.py: material -> texture set).
# Run: blender --background --factory-startup --python scripts/test_convert.py
import os
import sys
import tempfile

import bpy

# repo root = parent of scripts/ — works from any checkout location
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import AGR_tools.log as agr_log
import AGR_tools.properties as props
import AGR_tools.operators_convert as convmod
import AGR_tools.operators_sets as setsmod

agr_log.register()
props.register()
convmod.register()
setsmod.register()

from PIL import Image

FAILS = []


def check(name, cond, extra=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def run_op(callop, **kwargs):
    """Operator result; an ERROR report raises RuntimeError from Python."""
    try:
        return callop(**kwargs)
    except RuntimeError as exc:
        if "Traceback" in str(exc):
            raise
        return {'CANCELLED'}


TMP = tempfile.mkdtemp(prefix="agr_conv_test_")
bpy.ops.wm.save_as_mainfile(filepath=os.path.join(TMP, "scene.blend"))
BAKE = os.path.join(TMP, "AGR_BAKE")


def png(path, size, color):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new('RGB', (size, size), color).save(path)
    return path


def load_img(path):
    return bpy.data.images.load(path, check_existing=False)


def new_object(name, material):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0)], [], [(0, 1, 2)])
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    mesh.materials.append(material)
    for o in bpy.data.objects:
        o.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    return obj


def principled_material(name):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = next(n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
    return mat, bsdf


def set_files(mat_name):
    folder = os.path.join(BAKE, f"S_{mat_name}")
    if not os.path.isdir(folder):
        return {}
    return {f: os.path.join(folder, f) for f in os.listdir(folder)}


print("== 1. texture behind a node group is found (CONV-1) ==")
src = png(os.path.join(TMP, "src", "brick.png"), 64, (10, 200, 30))
mat, bsdf = principled_material("M_Group")

group = bpy.data.node_groups.new("TexGroup", 'ShaderNodeTree')
group.interface.new_socket("Fac", in_out='OUTPUT', socket_type='NodeSocketFloat')
group.interface.new_socket("Color", in_out='OUTPUT', socket_type='NodeSocketColor')
g_out = group.nodes.new('NodeGroupOutput')
g_tex = group.nodes.new('ShaderNodeTexImage')
g_tex.image = load_img(src)
g_val = group.nodes.new('ShaderNodeValue')
# Output 0 (Fac) is a plain value, output 1 (Color) is the texture: a
# non-socket-aware descent would return the Value branch and find nothing
group.links.new(g_val.outputs[0], g_out.inputs[0])
group.links.new(g_tex.outputs['Color'], g_out.inputs[1])

g_node = mat.node_tree.nodes.new('ShaderNodeGroup')
g_node.node_tree = group
mat.node_tree.links.new(g_node.outputs[1], bsdf.inputs['Base Color'])

new_object("SM_Grp_Ground", mat)
check("convert FINISHED", run_op(bpy.ops.agr.convert_materials_to_sets) == {'FINISHED'})
files = set_files("M_Group")
check("DiffuseOpacity written", "T_M_Group_DiffuseOpacity.png" in files, str(sorted(files)))
if "T_M_Group_DiffuseOpacity.png" in files:
    with Image.open(files["T_M_Group_DiffuseOpacity.png"]) as im:
        check("kept the source resolution (not a 256px stub)", im.size == (64, 64), str(im.size))
        check("kept the source colour", im.convert('RGB').getpixel((5, 5)) == (10, 200, 30),
              str(im.convert('RGB').getpixel((5, 5))))

print("== 2. material without Principled BSDF is refused (CONV-1) ==")
glass = bpy.data.materials.new("M_Glass")
glass.use_nodes = True
nodes = glass.node_tree.nodes
nodes.clear()
out = nodes.new('ShaderNodeOutputMaterial')
gbsdf = nodes.new('ShaderNodeBsdfGlass')
glass.node_tree.links.new(gbsdf.outputs[0], out.inputs['Surface'])

# an existing, good set must survive the refusal
good_folder = os.path.join(BAKE, "S_M_Glass")
png(os.path.join(good_folder, "T_M_Glass_DiffuseOpacity.png"), 512, (7, 8, 9))
png(os.path.join(good_folder, "T_M_Glass_ERM.png"), 512, (0, 204, 0))

new_object("SM_Glass_Ground", glass)
res = run_op(bpy.ops.agr.convert_materials_to_sets)
check("operator still FINISHED (reports the skip)", res == {'FINISHED'}, str(res))
with Image.open(os.path.join(good_folder, "T_M_Glass_DiffuseOpacity.png")) as im:
    check("existing 512px set NOT overwritten", im.size == (512, 512), str(im.size))
check("Glass node graph untouched",
      [n.type for n in glass.node_tree.nodes].count('BSDF_GLASS') == 1,
      str([n.type for n in glass.node_tree.nodes]))
check("no Principled injected",
      not any(n.type == 'BSDF_PRINCIPLED' for n in glass.node_tree.nodes))

print("== 3. active-material operator refuses the same way ==")
res = run_op(bpy.ops.agr.convert_active_material_to_set)
check("CANCELLED for a Glass-only material", res == {'CANCELLED'}, str(res))

print("== 4. bare colour writes a stub, but never over an existing set ==")
bare, bare_bsdf = principled_material("M_Bare")
bare_bsdf.inputs['Base Color'].default_value = (1.0, 0.0, 0.0, 1.0)
new_object("SM_Bare_Ground", bare)
check("first conversion FINISHED",
      run_op(bpy.ops.agr.convert_materials_to_sets) == {'FINISHED'})
stub = os.path.join(BAKE, "S_M_Bare", "T_M_Bare_DiffuseOpacity.png")
check("flat stub written for a bare colour", os.path.exists(stub))
with Image.open(stub) as im:
    check("stub is 256px", im.size == (256, 256), str(im.size))

# now put a real 512px set in its place and repeat
png(os.path.join(BAKE, "S_M_Bare", "T_M_Bare_DiffuseOpacity.png"), 512, (1, 2, 3))
run_op(bpy.ops.agr.convert_materials_to_sets)
with Image.open(stub) as im:
    check("existing set NOT overwritten by the stub", im.size == (512, 512), str(im.size))

print("== 5. linked Base Color without a traceable texture is refused ==")
linked, linked_bsdf = principled_material("M_Linked")
rgb = linked.node_tree.nodes.new('ShaderNodeRGB')
linked.node_tree.links.new(rgb.outputs[0], linked_bsdf.inputs['Base Color'])
new_object("SM_Linked_Ground", linked)
run_op(bpy.ops.agr.convert_materials_to_sets)
check("no set folder created for an untraceable Base Color",
      not os.path.isdir(os.path.join(BAKE, "S_M_Linked")))

print("== 6. output_folder setting is honoured (CONV-4) ==")
bpy.context.scene.agr_baker_settings.output_folder = "BAKE_2026"
custom, custom_bsdf = principled_material("M_Custom")
custom_bsdf.inputs['Base Color'].default_value = (0.0, 0.0, 1.0, 1.0)
new_object("SM_Custom_Ground", custom)
check("convert FINISHED",
      run_op(bpy.ops.agr.convert_materials_to_sets) == {'FINISHED'})
check("set written into the configured folder",
      os.path.exists(os.path.join(TMP, "BAKE_2026", "S_M_Custom",
                                  "T_M_Custom_DiffuseOpacity.png")))
check("nothing written into the hardcoded AGR_BAKE",
      not os.path.isdir(os.path.join(BAKE, "S_M_Custom")))
bpy.context.scene.agr_baker_settings.output_folder = "AGR_BAKE"

print("== 7. palette PNG with tRNS keeps its alpha (CONV-3b) ==")
pal_path = os.path.join(TMP, "src", "leaf.png")
pal = Image.new('P', (32, 32), 0)
palette = [0, 0, 0] + [200, 40, 40] + [0] * (256 * 3 - 6)
pal.putpalette(palette)
for x in range(16):
    for y in range(32):
        pal.putpixel((x, y), 1)
pal.save(pal_path, transparency=0)
with Image.open(pal_path) as probe:
    check("source is a palette PNG with transparency",
          probe.mode == 'P' and 'transparency' in probe.info, probe.mode)

leaf, leaf_bsdf = principled_material("M_Leaf")
tex = leaf.node_tree.nodes.new('ShaderNodeTexImage')
tex.image = load_img(pal_path)
leaf.node_tree.links.new(tex.outputs['Color'], leaf_bsdf.inputs['Base Color'])
leaf.node_tree.links.new(tex.outputs['Alpha'], leaf_bsdf.inputs['Alpha'])
new_object("SM_Leaf_Ground", leaf)
run_op(bpy.ops.agr.convert_materials_to_sets)
op_path = os.path.join(BAKE, "S_M_Leaf", "T_M_Leaf_Opacity.png")
check("Opacity written", os.path.exists(op_path))
if os.path.exists(op_path):
    with Image.open(op_path) as im:
        check("Opacity is NOT fully white (alpha survived)",
              min(im.convert('L').getdata()) == 0,
              str(min(im.convert('L').getdata())))
    with Image.open(os.path.join(BAKE, "S_M_Leaf", "T_M_Leaf_DiffuseOpacity.png")) as im:
        check("DiffuseOpacity saved as RGBA", im.mode == 'RGBA', im.mode)

print("== 8. pixel-buffer fallback encodes sRGB (CONV-3a) ==")
gen = bpy.data.images.new("generated_only", 8, 8)
gen.pixels.foreach_set([0.5, 0.5, 0.5, 1.0] * 64)   # scene-linear mid grey
genmat, gen_bsdf = principled_material("M_Gen")
gtex = genmat.node_tree.nodes.new('ShaderNodeTexImage')
gtex.image = gen
genmat.node_tree.links.new(gtex.outputs['Color'], gen_bsdf.inputs['Base Color'])
new_object("SM_Gen_Ground", genmat)
run_op(bpy.ops.agr.convert_materials_to_sets)
gen_do = os.path.join(BAKE, "S_M_Gen", "T_M_Gen_DiffuseOpacity.png")
check("set written from the pixel buffer", os.path.exists(gen_do))
if os.path.exists(gen_do):
    with Image.open(gen_do) as im:
        value = im.convert('RGB').getpixel((2, 2))[0]
    # linear 0.5 -> sRGB 0.7354 -> 188 (the old code wrote 127)
    check("linear 0.5 encoded as sRGB ~188", 185 <= value <= 191, str(value))

hdr = bpy.data.images.new("generated_hdr", 8, 8, float_buffer=True)
hdr.pixels.foreach_set([4.0, 4.0, 4.0, 1.0] * 64)   # values above 1.0
hdrmat, hdr_bsdf = principled_material("M_Hdr")
htex = hdrmat.node_tree.nodes.new('ShaderNodeTexImage')
htex.image = hdr
hdrmat.node_tree.links.new(htex.outputs['Color'], hdr_bsdf.inputs['Base Color'])
new_object("SM_Hdr_Ground", hdrmat)
run_op(bpy.ops.agr.convert_materials_to_sets)
hdr_do = os.path.join(BAKE, "S_M_Hdr", "T_M_Hdr_DiffuseOpacity.png")
if os.path.exists(hdr_do):
    with Image.open(hdr_do) as im:
        hvalue = im.convert('RGB').getpixel((2, 2))[0]
    # without the clamp the uint8 cast wrapped 1020 around modulo 256
    check("values above 1.0 clamp to white, not noise", hvalue == 255, str(hvalue))

print("== 9. a failed diffuse blocks the ERM/Normal stubs (CONV-2) ==")
broken, broken_bsdf = principled_material("M_Broken")
btex = broken.node_tree.nodes.new('ShaderNodeTexImage')
btex.image = load_img(src)
broken.node_tree.links.new(btex.outputs['Color'], broken_bsdf.inputs['Base Color'])
new_object("SM_Broken_Ground", broken)

# Simulate an unreadable source (truncated PNG / cloud placeholder): the
# flags used to be raised BEFORE the try, so the set ended up as the old
# diffuse plus fresh flat ERM/Normal stubs and the report said "converted"
_real_loader = convmod.AGR_OT_ConvertMaterialsToSets.load_pil_image
convmod.AGR_OT_ConvertMaterialsToSets.load_pil_image = lambda self, img: None
try:
    run_op(bpy.ops.agr.convert_materials_to_sets)
finally:
    convmod.AGR_OT_ConvertMaterialsToSets.load_pil_image = _real_loader

broken_files = set_files("M_Broken")
check("no DiffuseOpacity written", "T_M_Broken_DiffuseOpacity.png" not in broken_files,
      str(sorted(broken_files)))
check("no ERM stub written for a failed material",
      "T_M_Broken_ERM.png" not in broken_files, str(sorted(broken_files)))
check("no Normal stub written for a failed material",
      "T_M_Broken_Normal.png" not in broken_files, str(sorted(broken_files)))
check("material not reconnected to a half-written set",
      not any(n.type == 'TEX_IMAGE' and n.image and 'T_M_Broken' in n.image.name
              for n in broken.node_tree.nodes),
      str([n.image.name for n in broken.node_tree.nodes
           if n.type == 'TEX_IMAGE' and n.image]))

print("== 10. a flat-colour material may overwrite its OWN 256px stub (R-7) ==")
flat, flat_bsdf = principled_material("M_Flat")
flat_bsdf.inputs['Base Color'].default_value = (1.0, 0.0, 0.0, 1.0)
new_object("SM_Flat_Ground", flat)
check("first conversion of a bare-colour material",
      run_op(bpy.ops.agr.convert_materials_to_sets) == {'FINISHED'})
flat_do = os.path.join(BAKE, "S_M_Flat", "T_M_Flat_DiffuseOpacity.png")
check("a 256px stub was written", os.path.exists(flat_do))
with Image.open(flat_do) as im:
    check("the stub really is 256px", max(im.size) == 256, str(im.size))

# the user tweaks the colour and re-runs: the stub must be refreshed, not refused
for node in list(flat.node_tree.nodes):
    if node.type == 'TEX_IMAGE':
        flat.node_tree.nodes.remove(node)
flat_bsdf = next(n for n in flat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
flat_bsdf.inputs['Base Color'].default_value = (0.0, 0.0, 1.0, 1.0)
for o in bpy.data.objects:
    o.select_set(False)
flat_obj = bpy.data.objects["SM_Flat_Ground"]
flat_obj.select_set(True)
bpy.context.view_layer.objects.active = flat_obj
check("re-converting over the own stub is allowed",
      run_op(bpy.ops.agr.convert_materials_to_sets) == {'FINISHED'})
with Image.open(flat_do) as im:
    rgb = im.convert('RGB').getpixel((8, 8))
check("the stub carries the NEW colour", rgb[2] > rgb[0], str(rgb))

# a REAL texture of the same name is still protected (CONV-1 intact)
real, real_bsdf = principled_material("M_Real")
real_bsdf.inputs['Base Color'].default_value = (0.0, 1.0, 0.0, 1.0)
png(os.path.join(BAKE, "S_M_Real", "T_M_Real_DiffuseOpacity.png"), 1024, (7, 7, 7))
new_object("SM_Real_Ground", real)
check("a 1024px set is NOT overwritten by a stub",
      run_op(bpy.ops.agr.convert_active_material_to_set) == {'CANCELLED'})
with Image.open(os.path.join(BAKE, "S_M_Real", "T_M_Real_DiffuseOpacity.png")) as im:
    check("the real texture is untouched on disk", max(im.size) == 1024, str(im.size))

# preflight unit check: an unreadable existing file is never overwritten
from pathlib import Path as _PathC
pre = convmod.AGR_OT_ConvertMaterialsToSets.preflight_material
junk_folder = os.path.join(BAKE, "S_M_Junk")
os.makedirs(junk_folder, exist_ok=True)
with open(os.path.join(junk_folder, "T_M_Junk_DiffuseOpacity.png"), "wb") as fh:
    fh.write(b"not a png")
junk, junk_bsdf = principled_material("M_Junk")
ok, reason = pre(None, junk, {}, junk_bsdf, _PathC(junk_folder))
check("an unreadable existing DiffuseOpacity blocks the stub", not ok, str(reason))


print("=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} CHECKS FAILED:")
    for name in FAILS:
        print("  -", name)
    sys.exit(1)
print("✅ ALL CHECKS PASSED")
