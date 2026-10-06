# Headless test for AGR_tools/operators_bake.py + core/baking.py.
# Section 1 does a REAL 64px Cycles CPU bake (seconds in background);
# the pipeline sections stub bake_texture out — what they check is the
# contract around the bake call, not Cycles itself.
# Run: blender --background --factory-startup --python scripts/test_bake.py
import os
import shutil
import sys

import bpy

# repo root = parent of scripts/ — works from any checkout location
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import AGR_tools.log as agr_log
import AGR_tools.properties as props
import AGR_tools.operators_bake as bake_mod
import AGR_tools.operators_sets as sets_mod
import AGR_tools.ui as ui
from AGR_tools.core import baking

agr_log.register()
props.register()
bake_mod.register()
sets_mod.register()
ui.register()

FAILS = []


def check(name, cond, extra=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def expect_cancel(callop):
    """True only for a clean CANCELLED / report-ERROR outcome (no traceback)."""
    try:
        return callop() == {'CANCELLED'}
    except RuntimeError as exc:
        return "Traceback" not in str(exc)


# --------------------------------------------------------------- workspace
ROOT = os.path.join(bpy.app.tempdir, "agr_bake_test")
if os.path.isdir(ROOT):
    shutil.rmtree(ROOT, ignore_errors=True)
os.makedirs(ROOT)
bpy.ops.wm.save_as_mainfile(filepath=os.path.join(ROOT, "scene.blend"))
AGR_BAKE = os.path.join(ROOT, "AGR_BAKE")

SETTINGS = bpy.context.scene.agr_baker_settings
SETTINGS.resolution = '64'
SETTINGS.bake_samples = 1
SETTINGS.bake_device = 'CPU'
SETTINGS.bake_use_denoising = False
SETTINGS.bake_normal_enabled = True


def reset_scene():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for mat in list(bpy.data.materials):
        bpy.data.materials.remove(mat)
    for img in list(bpy.data.images):
        bpy.data.images.remove(img)


def add_plane(name):
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata([(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)], [], [(0, 1, 2, 3)])
    mesh.validate()
    mesh.uv_layers.new(name="UVMap")
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def principled_material(name, color=(0.2, 0.4, 0.8, 1.0)):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = next(n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
    bsdf.inputs['Base Color'].default_value = color
    return mat


def emission_material(name):
    """Emission drives the output, NO Principled anywhere (the BAKE-3 case)."""
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    for node in list(nt.nodes):
        if node.type == 'BSDF_PRINCIPLED':
            nt.nodes.remove(node)
    out = next(n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL')
    emission = nt.nodes.new('ShaderNodeEmission')
    emission.name = 'MARKER_EMISSION'
    emission.inputs['Color'].default_value = (0.8, 0.05, 0.05, 1.0)
    nt.links.new(emission.outputs['Emission'], out.inputs['Surface'])
    return mat


def select_only(obj):
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def node_types(mat):
    return sorted(n.type for n in mat.node_tree.nodes)


print("== 1. Simple Bake refuses a material without Principled BSDF (BAKE-3) ==")
reset_scene()
obj1 = add_plane("Sign")
mat_emit = emission_material("M_Sign_1")
obj1.data.materials.append(mat_emit)
select_only(obj1)
before_nodes = node_types(mat_emit)
# a set that already exists must survive the refusal
existing_set = os.path.join(AGR_BAKE, "S_M_Sign_1")
os.makedirs(existing_set, exist_ok=True)
marker_file = os.path.join(existing_set, "T_M_Sign_1_Diffuse.png")
with open(marker_file, "wb") as f:
    f.write(b"KEEPME")

check("simple_bake CANCELLED", expect_cancel(lambda: bpy.ops.agr.simple_bake()))
check("emission node survived", mat_emit.node_tree.nodes.get('MARKER_EMISSION') is not None)
check("no Principled invented", node_types(mat_emit) == before_nodes,
      f"{before_nodes} -> {node_types(mat_emit)}")
check("existing set file untouched", open(marker_file, "rb").read() == b"KEEPME")
check("no bake plane left behind",
      not any(o.name.startswith("BakePlane") for o in bpy.data.objects))

print("== 2. Simple Bake still works on a Principled material (real 64px Cycles bake) ==")
reset_scene()
obj2 = add_plane("Wall")
mat2 = principled_material("M_Wall_1", (1.0, 0.0, 0.0, 1.0))
obj2.data.materials.append(mat2)
select_only(obj2)
res2 = bpy.ops.agr.simple_bake()
check("simple_bake FINISHED", res2 == {'FINISHED'}, str(res2))
set_folder = os.path.join(AGR_BAKE, "S_M_Wall_1")
for tex_type in ("Diffuse", "DiffuseOpacity", "ERM", "Normal"):
    check(f"{tex_type} written",
          os.path.exists(os.path.join(set_folder, f"T_M_Wall_1_{tex_type}.png")))
check("material rebuilt on the baked set",
      any(n.type == 'TEX_IMAGE' for n in mat2.node_tree.nodes))
check("no leftover T_*.NNN datablocks",
      not any('.' in i.name for i in bpy.data.images), str(list(bpy.data.images.keys())))

print("== 3. Simple Bake All skips the non-Principled slot and bakes the rest ==")
reset_scene()
obj3 = add_plane("Mixed")
good = principled_material("M_Mixed_1", (0.0, 1.0, 0.0, 1.0))
bad = emission_material("M_Mixed_2")
obj3.data.materials.append(good)
obj3.data.materials.append(bad)
select_only(obj3)
res3 = bpy.ops.agr.simple_bake_all()
check("simple_bake_all FINISHED", res3 == {'FINISHED'}, str(res3))
check("Principled slot baked",
      os.path.exists(os.path.join(AGR_BAKE, "S_M_Mixed_1", "T_M_Mixed_1_ERM.png")))
check("emission slot skipped (no folder)",
      not os.path.isdir(os.path.join(AGR_BAKE, "S_M_Mixed_2")))
check("emission material intact", bad.node_tree.nodes.get('MARKER_EMISSION') is not None)

print("== 4. bake_texture restores the Bake panel when it fails early (BAKE-5) ==")
reset_scene()
bake = bpy.context.scene.render.bake
bake.margin = 42
bake.use_selected_to_active = False
bpy.context.scene.cycles.bake_type = 'COMBINED'
orphan_mesh = bpy.data.meshes.new("OrphanMesh")
orphan_mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0)], [], [(0, 1, 2)])
orphan_mesh.validate()
orphan = bpy.data.objects.new("Orphan", orphan_mesh)   # never linked to the scene
orphan.data.materials.append(principled_material("M_Orphan"))
img4 = baking.create_texture_image("T_Orphan_Diffuse", 64)
raised = False
try:
    baking.bake_texture(bpy.context, orphan, [], img4, 'DIFFUSE', 0)
except Exception:
    raised = True
check("bake_texture raised on an object outside the view layer", raised)
check("margin restored", bake.margin == 42, str(bake.margin))
check("use_selected_to_active restored", bake.use_selected_to_active is False)
check("cycles.bake_type restored", bpy.context.scene.cycles.bake_type == 'COMBINED',
      bpy.context.scene.cycles.bake_type)
bake.margin = 8

print("== 5. alpha helpers: dedup by material, exact restore (BAKE-11) ==")
reset_scene()
o5a = add_plane("HighA")
o5b = add_plane("HighB")
shared = principled_material("M_Shared")
bsdf5 = next(n for n in shared.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
value = shared.node_tree.nodes.new('ShaderNodeValue')
value.outputs[0].default_value = 0.25
shared.node_tree.links.new(value.outputs[0], bsdf5.inputs['Alpha'])
o5a.data.materials.append(shared)
o5b.data.materials.append(shared)          # same material on both objects
states = bake_mod.disable_alpha_on_objects([o5a, o5b])
check("shared material processed once", len(states) == 1, str(len(states)))
check("alpha link removed while baking", not bsdf5.inputs['Alpha'].links)
check("alpha forced opaque", abs(bsdf5.inputs['Alpha'].default_value - 1.0) < 1e-6)
bake_mod.restore_alpha_on_objects(states)
check("alpha link restored", bool(bsdf5.inputs['Alpha'].links))
check("alpha source restored",
      bsdf5.inputs['Alpha'].links[0].from_node == value)

print("== 6. selected-to-active disables alpha on the PBR passes (BAKE-11) ==")
reset_scene()
low = add_plane("Low")
low.data.materials.append(principled_material("M_Low_1"))
high = add_plane("High")
high.location = (0, 0, 0.1)
high_mat = principled_material("M_High")
high_bsdf = next(n for n in high_mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
high_value = high_mat.node_tree.nodes.new('ShaderNodeValue')
high_value.outputs[0].default_value = 0.3
high_mat.node_tree.links.new(high_value.outputs[0], high_bsdf.inputs['Alpha'])
high.data.materials.append(high_mat)

seen = {}
real_bake_texture = baking.bake_texture


def recording_bake(context, target_obj, source_objects, image, bake_type, *a, **kw):
    """Stub: record the high-poly Alpha state per pass instead of running Cycles
    (what is under test is the wrapping contract, not the renderer)."""
    seen[bake_type] = {
        'linked': bool(high_bsdf.inputs['Alpha'].links),
        'value': float(high_bsdf.inputs['Alpha'].default_value),
    }


baking.bake_texture = recording_bake
try:
    bpy.ops.object.select_all(action='DESELECT')
    high.select_set(True)
    low.select_set(True)
    bpy.context.view_layer.objects.active = low
    res6 = bpy.ops.agr.bake_textures()
finally:
    baking.bake_texture = real_bake_texture

check("bake_textures FINISHED", res6 == {'FINISHED'}, str(res6))
check("DIFFUSE pass saw the live alpha", seen.get('DIFFUSE', {}).get('linked') is True,
      str(seen.get('DIFFUSE')))
check("ROUGHNESS pass ran with alpha disabled",
      seen.get('ROUGHNESS', {}).get('linked') is False, str(seen.get('ROUGHNESS')))
check("NORMAL pass ran with alpha disabled",
      seen.get('NORMAL', {}).get('linked') is False, str(seen.get('NORMAL')))
check("high-poly alpha link restored afterwards", bool(high_bsdf.inputs['Alpha'].links))
check("high-poly alpha source restored",
      high_bsdf.inputs['Alpha'].links[0].from_node == high_value)

print("== 7. Bake from High-Poly leaves no renamed image datablocks (BAKE-10) ==")
baking.bake_texture = recording_bake
try:
    bpy.ops.object.select_all(action='DESELECT')
    high.select_set(True)
    low.select_set(True)
    bpy.context.view_layer.objects.active = low
    res7 = bpy.ops.agr.bake_textures()          # second run over the same material
finally:
    baking.bake_texture = real_bake_texture
check("second run FINISHED", res7 == {'FINISHED'}, str(res7))
leftovers = [i.name for i in bpy.data.images if i.name[-4:-3] == '.' and i.name[-3:].isdigit()]
check("no T_*.NNN leftovers after a repeated bake", not leftovers, str(leftovers))

print("== 8. ERM is composed from the uint8 file buffers (BAKE-8) ==")
reset_scene()
try:
    from PIL import Image
except ImportError:
    Image = None

if Image is None:
    print("  [SKIP] Pillow not available")
else:
    erm_dir = os.path.join(ROOT, "erm")
    os.makedirs(erm_dir, exist_ok=True)
    # Emit: top half 255 / bottom half 0 — catches a vertical flip
    emit = Image.new('RGB', (8, 8), (0, 0, 0))
    for x in range(8):
        for y in range(4):
            emit.putpixel((x, y), (255, 255, 255))
    emit.save(os.path.join(erm_dir, "T_Probe_Emit.png"), 'PNG')
    Image.new('RGB', (8, 8), (128, 128, 128)).save(
        os.path.join(erm_dir, "T_Probe_Roughness.png"), 'PNG')
    Image.new('RGB', (8, 8), (32, 32, 32)).save(
        os.path.join(erm_dir, "T_Probe_Metallic.png"), 'PNG')

    erm_img = baking.create_texture_image("T_Probe_ERM", 8)
    erm_img.colorspace_settings.name = 'Non-Color'
    check("fast ERM path used",
          bake_mod.compose_erm_from_files(erm_dir, "Probe", erm_img) is True)
    px = list(erm_img.pixels)
    bottom = px[0:4]              # Blender's first row is the BOTTOM one
    top = px[(7 * 8) * 4:(7 * 8) * 4 + 4]
    check("G = Roughness (128/255)", abs(bottom[1] - 128 / 255.0) < 1e-4, str(bottom))
    check("B = Metallic (32/255)", abs(bottom[2] - 32 / 255.0) < 1e-4, str(bottom))
    check("alpha is opaque", abs(bottom[3] - 1.0) < 1e-6)
    check("R = Emit, bottom row black", bottom[0] < 1e-4, str(bottom))
    check("R = Emit, top row white (no vertical flip)", top[0] > 0.999, str(top))
    # a mismatching target falls back instead of writing garbage
    small = baking.create_texture_image("T_Probe_ERM_small", 4)
    check("size mismatch falls back",
          bake_mod.compose_erm_from_files(erm_dir, "Probe", small) is False)

print("== 9. R-6: a Principled hidden in a node group gets its OWN advice (BAKE-3) ==")
reset_scene()


def grouped_principled_material(name):
    """Output driven by a node GROUP that carries the Principled inside."""
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    for node in list(nt.nodes):
        if node.type == 'BSDF_PRINCIPLED':
            nt.nodes.remove(node)
    group_tree = bpy.data.node_groups.new(f"{name}_GRP", 'ShaderNodeTree')
    group_tree.interface.new_socket("Shader", in_out='OUTPUT', socket_type='NodeSocketShader')
    inner_bsdf = group_tree.nodes.new('ShaderNodeBsdfPrincipled')
    group_out = group_tree.nodes.new('NodeGroupOutput')
    group_tree.links.new(inner_bsdf.outputs['BSDF'], group_out.inputs[0])
    grp = nt.nodes.new('ShaderNodeGroup')
    grp.node_tree = group_tree
    out = next(n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL')
    nt.links.new(grp.outputs[0], out.inputs['Surface'])
    return mat


def op_error_text(callop):
    try:
        callop()
        return ""
    except RuntimeError as exc:
        return str(exc)


mat_grp = grouped_principled_material("M_Grouped_1")
mat_emit9 = emission_material("M_Grouped_2")
check("R-6: node-group Principled is not visible at the top level",
      baking.find_principled_bsdf(mat_grp) is None)
check("R-6: such a material is still refused",
      baking.material_lacks_principled(mat_grp) is True)
check("R-6: the group case is recognised",
      baking.principled_hidden_in_group(mat_grp) is True)
check("R-6: a plain Emission material is NOT the group case",
      baking.principled_hidden_in_group(mat_emit9) is False)
check("R-6: a healthy Principled material is not the group case",
      baking.principled_hidden_in_group(principled_material("M_Grouped_3")) is False)

obj9 = add_plane("Grouped")
obj9.data.materials.append(mat_grp)
select_only(obj9)
msg9 = op_error_text(lambda: bpy.ops.agr.simple_bake())
check("R-6: simple_bake still refuses", msg9 != "" and 'Traceback' not in msg9, msg9[:120])
check("R-6: the message points at the node group",
      "нод-групп" in msg9, msg9[:160])

reset_scene()
obj9b = add_plane("Grouped2")
obj9b.data.materials.append(emission_material("M_Grouped2_1"))
select_only(obj9b)
msg9b = op_error_text(lambda: bpy.ops.agr.simple_bake())
check("R-6: the Emission material keeps the old advice",
      "эмиссию/стекло" in msg9b and "нод-групп" not in msg9b, msg9b[:160])

print("=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} CHECKS FAILED:")
    for name in FAILS:
        print("  -", name)
    sys.exit(1)
print("✅ ALL CHECKS PASSED")
