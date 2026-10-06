# Headless regression suite for AGR Rename (operators_rename.py,
# operators_rename_project.py and the shared rename_shared.py).
# Run: blender --background --factory-startup --python scripts/test_rename.py
import os
import re
import sys
import shutil
import tempfile

import bpy

# repo root = parent of scripts/ — works from any checkout location
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import AGR_tools.log as agr_log
import AGR_tools.rename_shared as rs
import AGR_tools.operators_rename as rn
import AGR_tools.operators_rename_project as rnp

agr_log.register()
rn.register()
rnp.register()

FAILS = []
TMP_ROOTS = []


def check(name, cond, extra=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


# ─────────────────────────── helpers ───────────────────────────

def reset_scene():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for me in list(bpy.data.meshes):
        bpy.data.meshes.remove(me)
    for mat in list(bpy.data.materials):
        bpy.data.materials.remove(mat)
    for img in list(bpy.data.images):
        if img.name not in ('Render Result', 'Viewer Node'):
            bpy.data.images.remove(img)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)


def tmp_root(prefix):
    root = tempfile.mkdtemp(prefix=prefix)
    TMP_ROOTS.append(root)
    reset_scene()
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(root, "proj.blend"))
    return root


def make_png(path, color=(1, 0, 0, 1)):
    img = bpy.data.images.new("__tmp__", 8, 8, alpha=True)
    img.pixels = list(color) * 64
    img.filepath_raw = path
    img.file_format = 'PNG'
    img.save()
    bpy.data.images.remove(img)


def make_quad(name):
    me = bpy.data.meshes.new(name + "Mesh")
    me.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [[0, 1, 2, 3]])
    me.validate()
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    return ob


def new_mat(name):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = next(n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED')
    return mat, nt, bsdf


def tex_node(nt, path, colorspace='sRGB'):
    n = nt.nodes.new('ShaderNodeTexImage')
    n.image = bpy.data.images.load(path)
    n.image.colorspace_settings.name = colorspace
    return n


def img_names():
    return sorted(i.name for i in bpy.data.images if i.name not in ('Render Result', 'Viewer Node'))


def tex_images(nt):
    return [(n.image.name if n.image else None) for n in nt.nodes if n.type == 'TEX_IMAGE']


def px(img):
    return tuple(round(v, 2) for v in img.pixels[0:4])


class Capture:
    """Collect agr_report() messages emitted by the rename modules."""

    def __init__(self, *modules):
        self.modules = modules
        self.messages = []

    def __enter__(self):
        self._orig = {}
        for mod in self.modules:
            self._orig[mod] = mod.agr_report

            def hook(op, level, msg, _o=mod.agr_report, _self=self):
                _self.messages.append((level, msg))
                _o(op, level, msg)
            mod.agr_report = hook
        return self

    def __exit__(self, *exc):
        for mod, orig in self._orig.items():
            mod.agr_report = orig
        return False

    def has(self, level, needle):
        return any(lv == level and needle in msg for lv, msg in self.messages)


# ═══════════════ 1. RENAME-1 / X1: material graph survives ═══════════════
print("== 1. RENAME-1: HIGH set survives 'Переименовать ВЕСЬ ПРОЕКТ' ==")
root = tmp_root("agr_rn1_")
do_p = os.path.join(root, "T_Old_Ground_do_1.png")
erm_p = os.path.join(root, "T_Old_Ground_erm_1.png")
n_p = os.path.join(root, "T_Old_Ground_n_1.png")
make_png(do_p, (1, 0, 0, 0.5))
make_png(erm_p, (0, 0.5, 0, 1))
make_png(n_p, (0.5, 0.5, 1, 1))

ob = make_quad("SM_Old_Ground")
mat, nt, bsdf = new_mat("M_Old_Ground_1")
do_n = tex_node(nt, do_p, 'sRGB')
nt.links.new(do_n.outputs['Color'], bsdf.inputs['Base Color'])
nt.links.new(do_n.outputs['Alpha'], bsdf.inputs['Alpha'])
erm_n = tex_node(nt, erm_p, 'Non-Color')
sep = nt.nodes.new('ShaderNodeSeparateColor')
nt.links.new(erm_n.outputs['Color'], sep.inputs[0])
nt.links.new(sep.outputs['Green'], bsdf.inputs['Roughness'])
nt.links.new(sep.outputs['Blue'], bsdf.inputs['Metallic'])
nrm_n = tex_node(nt, n_p, 'Non-Color')
nmap = nt.nodes.new('ShaderNodeNormalMap')
nt.links.new(nrm_n.outputs['Color'], nmap.inputs['Color'])
nt.links.new(nmap.outputs['Normal'], bsdf.inputs['Normal'])
ob.data.materials.append(mat)

bpy.context.scene.agr_rename_address = "New"
check("op FINISHED", bpy.ops.agr.rename_project() == {'FINISHED'})
check("object renamed", ob.name == "SM_New_Ground", ob.name)
check("Base Color still linked", bsdf.inputs['Base Color'].is_linked)
check("Alpha still linked", bsdf.inputs['Alpha'].is_linked)
check("Separate Color input still linked", sep.inputs[0].is_linked)
check("Normal still linked", bsdf.inputs['Normal'].is_linked)
new_do = bsdf.inputs['Base Color'].links[0].from_node.image
new_erm = sep.inputs[0].links[0].from_node.image
new_n = nmap.inputs['Color'].links[0].from_node.image
check("DO renamed", new_do and new_do.name == "T_New_Ground_do_1.png",
      new_do.name if new_do else None)
check("ERM renamed", new_erm and new_erm.name == "T_New_Ground_erm_1.png",
      new_erm.name if new_erm else None)
check("Normal renamed", new_n and new_n.name == "T_New_Ground_n_1.png",
      new_n.name if new_n else None)
check("ERM colorspace Non-Color", new_erm.colorspace_settings.name == 'Non-Color',
      new_erm.colorspace_settings.name)
check("DO colorspace sRGB", new_do.colorspace_settings.name == 'sRGB',
      new_do.colorspace_settings.name)
check("Normal colorspace Non-Color", new_n.colorspace_settings.name == 'Non-Color')
check("all three packed", all(i.packed_file for i in (new_do, new_erm, new_n)))

print("== 1b. RENAME-1: LOW set (d/o/r/m/n/e) ==")
root = tmp_root("agr_rn1b_")
paths = {}
for code, col in (('d', (1, 0, 0, 1)), ('o', (1, 1, 1, 1)), ('r', (0.3, 0.3, 0.3, 1)),
                  ('m', (0.1, 0.1, 0.1, 1)), ('n', (0.5, 0.5, 1, 1)), ('e', (0, 1, 0, 1))):
    paths[code] = os.path.join(root, f"T_Old_Ground_{code}_1.png")
    make_png(paths[code], col)
ob = make_quad("SM_Old_Ground")
mat, nt, bsdf = new_mat("M_Old_Ground_1")
sockets = {'d': 'Base Color', 'o': 'Alpha', 'r': 'Roughness',
           'm': 'Metallic', 'e': 'Emission Color'}
nodes_by_code = {}
for code, socket in sockets.items():
    node = tex_node(nt, paths[code], 'sRGB' if code in ('d', 'e') else 'Non-Color')
    nt.links.new(node.outputs['Color'], bsdf.inputs[socket])
    nodes_by_code[code] = node
nrm_n = tex_node(nt, paths['n'], 'Non-Color')
nmap = nt.nodes.new('ShaderNodeNormalMap')
nt.links.new(nrm_n.outputs['Color'], nmap.inputs['Color'])
nt.links.new(nmap.outputs['Normal'], bsdf.inputs['Normal'])
ob.data.materials.append(mat)
bpy.context.scene.agr_rename_address = "New"
check("op FINISHED", bpy.ops.agr.rename_project() == {'FINISHED'})
for code, socket in sockets.items():
    linked = bsdf.inputs[socket].is_linked
    image = bsdf.inputs[socket].links[0].from_node.image if linked else None
    check(f"LOW '{code}' reconnected to {socket}",
          linked and image is not None and image.name == f"T_New_Ground_{code}_1.png",
          image.name if image else None)
check("LOW 'n' reconnected", nmap.inputs['Color'].is_linked
      and nmap.inputs['Color'].links[0].from_node.image.name == "T_New_Ground_n_1.png")
check("colorspace: 'r' Non-Color",
      bsdf.inputs['Roughness'].links[0].from_node.image.colorspace_settings.name == 'Non-Color')
check("colorspace: 'e' sRGB",
      bsdf.inputs['Emission Color'].links[0].from_node.image.colorspace_settings.name == 'sRGB')

print("== 1c. RENAME-X1: unrecognized texture stays connected ==")
root = tmp_root("agr_rn1c_")
d_p = os.path.join(root, "T_Old_Ground_d_1.png")
full = os.path.join(root, "T_Wall_DiffuseOpacity.png")
make_png(d_p, (1, 0, 0, 1))
make_png(full, (0, 1, 0, 1))
ob = make_quad("SM_Old_Ground")
mat, nt, bsdf = new_mat("M_Old_Ground_1")
dn = tex_node(nt, d_p)
nt.links.new(dn.outputs['Color'], bsdf.inputs['Base Color'])
nf = tex_node(nt, full)
nt.links.new(nf.outputs['Color'], bsdf.inputs['Emission Color'])
ob.data.materials.append(mat)
bpy.context.scene.agr_rename_address = "New"
bpy.ops.agr.rename_project()
check("unrecognized node still in the material",
      "T_Wall_DiffuseOpacity.png" in tex_images(nt), str(tex_images(nt)))
check("Emission Color still linked", bsdf.inputs['Emission Color'].is_linked)
check("its datablock survives", bpy.data.images.get("T_Wall_DiffuseOpacity.png") is not None)


# ═══════════════ 2. RENAME-4: per-material files ═══════════════
print("== 2. RENAME-4: two materials keep two textures ==")
root = tmp_root("agr_rn4_")
d1 = os.path.join(root, "T_Old_Ground_d_1.png")
d2 = os.path.join(root, "T_Old_Ground_d_2.png")
make_png(d1, (1, 0, 0, 1))
make_png(d2, (0, 0, 1, 1))
ob = make_quad("SM_Old_Ground")
m1, nt1, b1 = new_mat("M_Old_Ground_1")
n1 = tex_node(nt1, d1)
nt1.links.new(n1.outputs['Color'], b1.inputs['Base Color'])
m2, nt2, b2 = new_mat("M_Old_Ground_2")
n2 = tex_node(nt2, d2)
nt2.links.new(n2.outputs['Color'], b2.inputs['Base Color'])
ob.data.materials.append(m1)
ob.data.materials.append(m2)
bpy.context.scene.agr_rename_address = "New"
bpy.ops.agr.rename_project()
i1 = b1.inputs['Base Color'].links[0].from_node.image
i2 = b2.inputs['Base Color'].links[0].from_node.image
check("two distinct datablocks", i1 is not None and i2 is not None and i1 != i2,
      f"{i1.name if i1 else None} / {i2.name if i2 else None}")
check("names carry the material index",
      {i1.name, i2.name} == {"T_New_Ground_d_1.png", "T_New_Ground_d_2.png"},
      f"{i1.name} / {i2.name}")
check("M1 keeps its red pixels", px(i1)[0] > 0.9 and px(i1)[2] < 0.1, str(px(i1)))
check("M2 keeps its blue pixels", px(i2)[2] > 0.9 and px(i2)[0] < 0.1, str(px(i2)))

print("== 2b. RENAME-4: packed-only M1 + unpacked M2 ==")
root = tmp_root("agr_rn4b_")
d1 = os.path.join(root, "T_Old_Ground_d_1.png")
d2 = os.path.join(root, "T_Old_Ground_d_2.png")
make_png(d1, (1, 0, 0, 1))
make_png(d2, (0, 0, 1, 1))
ob = make_quad("SM_Old_Ground")
m1, nt1, b1 = new_mat("M_Old_Ground_1")
n1 = tex_node(nt1, d1)
nt1.links.new(n1.outputs['Color'], b1.inputs['Base Color'])
n1.image.pack()
os.remove(d1)  # packed-only: the .blend is the ONLY copy
m2, nt2, b2 = new_mat("M_Old_Ground_2")
n2 = tex_node(nt2, d2)
nt2.links.new(n2.outputs['Color'], b2.inputs['Base Color'])
ob.data.materials.append(m1)
ob.data.materials.append(m2)
bpy.context.scene.agr_rename_address = "New"
bpy.ops.agr.rename_project()
i1 = b1.inputs['Base Color'].links[0].from_node.image
i2 = b2.inputs['Base Color'].links[0].from_node.image
check("packed-only M1 data survives (red)", i1 is not None and px(i1)[0] > 0.9, str(px(i1) if i1 else None))
check("M2 stays blue", i2 is not None and px(i2)[2] > 0.9, str(px(i2) if i2 else None))
check("still two datablocks", i1 != i2)


# ═══════════════ 3. RENAME-2: datablocks are not destroyed ═══════════════
print("== 3. RENAME-2: roughness through a ColorRamp (reconnect misses it) ==")
root = tmp_root("agr_rn2a_")
d_p = os.path.join(root, "T_Old_Ground_d_1.png")
r_p = os.path.join(root, "T_Old_Ground_r_1.png")
make_png(d_p, (1, 0, 0, 1))
make_png(r_p, (0.5, 0.5, 0.5, 1))
ob = make_quad("SM_Old_Ground")
mat, nt, bsdf = new_mat("M_Old_Ground_1")
dn = tex_node(nt, d_p)
nt.links.new(dn.outputs['Color'], bsdf.inputs['Base Color'])
rnode = tex_node(nt, r_p, 'Non-Color')
ramp = nt.nodes.new('ShaderNodeValToRGB')
nt.links.new(rnode.outputs['Color'], ramp.inputs['Fac'])
nt.links.new(ramp.outputs['Color'], bsdf.inputs['Roughness'])
ob.data.materials.append(mat)
bpy.context.scene.agr_rename_address = "New"
bpy.context.view_layer.objects.active = ob
ob.select_set(True)
with Capture(rs) as cap:
    check("op FINISHED", bpy.ops.agr.rename_textures() == {'FINISHED'})
check("roughness node keeps an image", rnode.image is not None,
      rnode.image.name if rnode.image else None)
check("original file still on disk", os.path.exists(r_p))
check("WARNING about the unconnected type", cap.has('WARNING', 'не подключены'),
      str([m for _l, m in cap.messages]))
check("diffuse reconnected and packed",
      bsdf.inputs['Base Color'].is_linked
      and bsdf.inputs['Base Color'].links[0].from_node.image.packed_file is not None)

print("== 3b. RENAME-2: packed-only map the reconnect cannot reach ==")
root = tmp_root("agr_rn2b_")
d_p = os.path.join(root, "T_Old_Ground_d_1.png")
r_p = os.path.join(root, "T_Old_Ground_r_1.png")
make_png(d_p, (1, 0, 0, 1))
make_png(r_p, (0.5, 0.5, 0.5, 1))
ob = make_quad("SM_Old_Ground")
mat, nt, bsdf = new_mat("M_Old_Ground_1")
dn = tex_node(nt, d_p)
nt.links.new(dn.outputs['Color'], bsdf.inputs['Base Color'])
rnode = tex_node(nt, r_p, 'Non-Color')
rnode.image.pack()
os.remove(r_p)
ramp = nt.nodes.new('ShaderNodeValToRGB')
nt.links.new(rnode.outputs['Color'], ramp.inputs['Fac'])
nt.links.new(ramp.outputs['Color'], bsdf.inputs['Roughness'])
ob.data.materials.append(mat)
bpy.context.scene.agr_rename_address = "New"
bpy.context.view_layer.objects.active = ob
ob.select_set(True)
bpy.ops.agr.rename_textures()
check("packed roughness datablock survives", rnode.image is not None)
check("its packed data survives", rnode.image is not None and rnode.image.packed_file is not None)
found_r = []
for dirpath, _dirs, files in os.walk(root):
    found_r += [f for f in files if '_r_' in f]
check("a roughness file is left on disk as well", bool(found_r), str(found_r))

print("== 3c. RENAME-2: image datablock shared by two objects ==")
root = tmp_root("agr_rn2c_")
ds = os.path.join(root, "T_Old_Shared_d_1.png")
make_png(ds)
mA, ntA, bA = new_mat("M_Old_Ground_1")
nA = tex_node(ntA, ds)
ntA.links.new(nA.outputs['Color'], bA.inputs['Base Color'])
obA = make_quad("SM_Old_Ground")
obA.data.materials.append(mA)
mB, ntB, bB = new_mat("M_Old_Main_1")
nB = ntB.nodes.new('ShaderNodeTexImage')
nB.image = nA.image
ntB.links.new(nB.outputs['Color'], bB.inputs['Base Color'])
obB = make_quad("SM_Old_Main")
obB.data.materials.append(mB)
bpy.context.scene.agr_rename_address = "New"
bpy.ops.agr.rename_project()
check("object A keeps a texture", None not in tex_images(ntA), str(tex_images(ntA)))
check("object B keeps a texture", None not in tex_images(ntB), str(tex_images(ntB)))
check("original file survives", os.path.exists(ds))

print("== 3d. RENAME-2: material without Principled BSDF ==")
root = tmp_root("agr_rn2d_")
d_p = os.path.join(root, "T_Old_Ground_d_1.png")
make_png(d_p, (1, 0, 0, 1))
ob = make_quad("SM_Old_Ground")
mat = bpy.data.materials.new("M_Old_Ground_1")
mat.use_nodes = True
nt = mat.node_tree
for node in list(nt.nodes):
    if node.type == 'BSDF_PRINCIPLED':
        nt.nodes.remove(node)
out = next(n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL')
em = nt.nodes.new('ShaderNodeEmission')
nt.links.new(em.outputs['Emission'], out.inputs['Surface'])
tn = tex_node(nt, d_p)
nt.links.new(tn.outputs['Color'], em.inputs['Color'])
ob.data.materials.append(mat)
bpy.context.scene.agr_rename_address = "New"
bpy.ops.agr.rename_project()
check("emission texture node keeps its image", tn.image is not None,
      tn.image.name if tn.image else None)
check("original file survives", os.path.exists(d_p))


# ═══════════════ 4. RENAME-5: no recursion into archives ═══════════════
print("== 4. RENAME-5: geojson/FBX in a backup subfolder stay untouched ==")
root = tmp_root("agr_rn5_")
with open(os.path.join(root, "SM_Old_Ground.geojson"), 'w', encoding='utf-8') as f:
    f.write('{"features": [{"properties": {}, "geometry": {"coordinates": [0, 0]}}]}')
with open(os.path.join(root, "SM_Old_Ground.fbx"), 'w') as f:
    f.write("x")
deep = os.path.join(root, "backup_2024", "deep")
os.makedirs(deep)
with open(os.path.join(deep, "SM_Old_Ground.geojson"), 'w', encoding='utf-8') as f:
    f.write('{"features": [{"properties": {}}]}')
with open(os.path.join(deep, "SM_Old_Ground.fbx"), 'w') as f:
    f.write("x")
make_quad("SM_Old_Ground")
bpy.context.scene.agr_rename_address = "New"
bpy.ops.agr.rename_project()
check("root geojson renamed", os.path.exists(os.path.join(root, "SM_New_Ground.geojson")))
check("root FBX renamed", os.path.exists(os.path.join(root, "SM_New_Ground.fbx")))
check("archive geojson untouched", os.path.exists(os.path.join(deep, "SM_Old_Ground.geojson")))
check("archive FBX untouched", os.path.exists(os.path.join(deep, "SM_Old_Ground.fbx")))
check("nothing new appeared in the archive",
      not os.path.exists(os.path.join(deep, "SM_New_Ground.fbx")))

print("== 4b. RENAME-5: a foreign address is not adopted ==")
root = tmp_root("agr_rn5b_")
with open(os.path.join(root, "SM_Foreign_Ground.geojson"), 'w', encoding='utf-8') as f:
    f.write('{"features": [{"properties": {}}]}')
make_quad("SM_Old_Ground")
bpy.context.scene.agr_rename_address = "New"
bpy.ops.agr.rename_project()
check("foreign geojson kept its name",
      os.path.exists(os.path.join(root, "SM_Foreign_Ground.geojson")))
check("no SM_New_Ground.geojson invented",
      not os.path.exists(os.path.join(root, "SM_New_Ground.geojson")))


# ═══════════════ 5. RENAME-3: lowpoly folder choice ═══════════════
print("== 5. RENAME-3: only the real lowpoly folder is renamed ==")
root = tmp_root("agr_rn3_")
archive = os.path.join(root, "0001_ArchiveBackup")
real = os.path.join(root, "0903_Old")
os.makedirs(archive)
os.makedirs(real)
with open(os.path.join(archive, "0001_Whatever_Ground.fbx"), 'w') as f:
    f.write("x")
with open(os.path.join(real, "0903_Old_Ground.fbx"), 'w') as f:
    f.write("x")
coll = bpy.data.collections.new("0903_Old_Ground.fbx")
bpy.context.scene.collection.children.link(coll)
ob = make_quad("SM_Old_Ground")
bpy.context.scene.collection.objects.unlink(ob)
coll.objects.link(ob)
bpy.context.scene.agr_rename_address = "New"
bpy.context.scene.agr_rp_project_lowpoly_number = "0903"
bpy.ops.agr.rename_project()
bpy.context.scene.agr_rp_project_lowpoly_number = ""
dirs = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))
check("real lowpoly folder renamed", "0903_New" in dirs, str(dirs))
check("archive folder untouched", "0001_ArchiveBackup" in dirs, str(dirs))
check("FBX inside renamed",
      os.path.exists(os.path.join(root, "0903_New", "0903_New_Ground.fbx")),
      str(os.listdir(os.path.join(root, "0903_New"))) if os.path.isdir(os.path.join(root, "0903_New")) else "-")
check("archive FBX untouched",
      os.path.exists(os.path.join(archive, "0001_Whatever_Ground.fbx")))

print("== 5b. RENAME-3: ambiguity is reported, not silently guessed ==")
root = tmp_root("agr_rn3b_")
for name in ("0903_A", "0903_B"):
    os.makedirs(os.path.join(root, name))
    with open(os.path.join(root, name, f"0903_{name[5:]}_Ground.fbx"), 'w') as f:
        f.write("x")
coll = bpy.data.collections.new("0903_Old_Ground.fbx")
bpy.context.scene.collection.children.link(coll)
ob = make_quad("SM_Old_Ground")
bpy.context.scene.collection.objects.unlink(ob)
coll.objects.link(ob)
bpy.context.scene.agr_rename_address = "New"
bpy.context.scene.agr_rp_project_lowpoly_number = "0903"
with Capture(rnp) as cap:
    bpy.ops.agr.rename_project()
bpy.context.scene.agr_rp_project_lowpoly_number = ""
dirs = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))
check("no folder renamed on ambiguity", "0903_A" in dirs and "0903_B" in dirs, str(dirs))
check("ambiguity reported", cap.has('WARNING', 'несколько'), str([m for _l, m in cap.messages]))

print("== 5c. RENAME-3: occupied target name warns instead of staying silent ==")
root = tmp_root("agr_rn3c_")
os.makedirs(os.path.join(root, "0903_Old"))
os.makedirs(os.path.join(root, "0903_New"))
with open(os.path.join(root, "0903_Old", "0903_Old_Ground.fbx"), 'w') as f:
    f.write("x")
coll = bpy.data.collections.new("0903_Old_Ground.fbx")
bpy.context.scene.collection.children.link(coll)
ob = make_quad("SM_Old_Ground")
bpy.context.scene.collection.objects.unlink(ob)
coll.objects.link(ob)
bpy.context.scene.agr_rename_address = "New"
bpy.context.scene.agr_rp_project_lowpoly_number = "0903"
with Capture(rnp) as cap:
    bpy.ops.agr.rename_project()
bpy.context.scene.agr_rp_project_lowpoly_number = ""
check("WARNING about the occupied folder", cap.has('WARNING', 'уже существует'),
      str([m for _l, m in cap.messages]))


# ═══════════════ 6. RENAME-6: confirmation, no UNDO, no collection sweep ═══════════════
print("== 6. RENAME-6: honest options and a narrow collection cleanup ==")
check("no misleading 'UNDO' flag", 'UNDO' not in rnp.AGR_RP_OT_rename_project.bl_options,
      str(rnp.AGR_RP_OT_rename_project.bl_options))
check("operator has invoke() for the confirmation",
      hasattr(rnp.AGR_RP_OT_rename_project, 'invoke'))
root = tmp_root("agr_rn6_")
refs = bpy.data.collections.new("Refs")          # user's own EMPTY collection
bpy.context.scene.collection.children.link(refs)
src = bpy.data.collections.new("Import")
bpy.context.scene.collection.children.link(src)
ob = make_quad("SM_Old_Ground")
bpy.context.scene.collection.objects.unlink(ob)
src.objects.link(ob)
bpy.context.scene.agr_rename_address = "New"
bpy.ops.agr.rename_project()
check("user's empty collection survives", bpy.data.collections.get("Refs") is not None)
check("collection the operator emptied is gone", bpy.data.collections.get("Import") is None)


# ═══════════════ 7. RENAME-7: UDIM per material + guard ═══════════════
print("== 7. RENAME-7: UDIM tiles map per material ==")
root = tmp_root("agr_rn7_")
udim_dir = os.path.join(root, "SM_Old_Ground")
os.makedirs(udim_dir)
for idx in (1, 2):
    for tex in ('Diffuse', 'Normal', 'ERM'):
        make_png(os.path.join(udim_dir, f"T_Old_Ground_{tex}_{idx}.1001.png"))
table = rs.scan_udim_folder(udim_dir)
check("scan finds both material indices",
      ('Diffuse', 1) in table and ('Diffuse', 2) in table, str(sorted(table)))

ob = make_quad("SM_Old_Ground")
loaded_calls = []
for idx in (1, 2):
    mat, nt, bsdf = new_mat(f"M_Old_Ground_{idx}")
    node = tex_node(nt, os.path.join(udim_dir, f"T_Old_Ground_Diffuse_{idx}.1001.png"))
    nt.links.new(node.outputs['Color'], bsdf.inputs['Base Color'])
    ob.data.materials.append(mat)

_orig_load = rs._load_udim_texture


def _fake_load(folder, filename, node, color_space):
    owner = None
    for material in ob.data.materials:
        if not material or not material.node_tree:
            continue
        twin = material.node_tree.nodes.get(node.name)
        if twin is not None and twin == node:
            owner = material.name
            break
    loaded_calls.append((owner, filename, color_space))
    return True


rs._load_udim_texture = _fake_load
try:
    rs.update_udim_material_paths(None, ob, udim_dir)
finally:
    rs._load_udim_texture = _orig_load
by_mat = {mat: fn for mat, fn, _cs in loaded_calls}
check("material 1 gets tile _1",
      by_mat.get("M_Old_Ground_1") == "T_Old_Ground_Diffuse_1.1001.png", str(by_mat))
check("material 2 gets tile _2",
      by_mat.get("M_Old_Ground_2") == "T_Old_Ground_Diffuse_2.1001.png", str(by_mat))

print("== 7b. RENAME-7: occupied target folder refuses BEFORE renaming tiles ==")
os.makedirs(os.path.join(root, "SM_New_Ground"))
before = sorted(os.listdir(udim_dir))
with Capture(rs) as cap:
    renamed, folder_renamed = rs.process_udim_textures(None, ob, udim_dir, "New", None, 'Ground')
check("refused", renamed == 0 and not folder_renamed, f"{renamed}/{folder_renamed}")
check("tiles untouched", sorted(os.listdir(udim_dir)) == before)
check("refusal reported", cap.has('WARNING', 'уже существует'), str([m for _l, m in cap.messages]))


# ═══════════════ 8. RENAME-8: all 7 types + .001 ═══════════════
print("== 8. RENAME-8: Flora / GroundEl / .001 duplicates are renamed too ==")
root = tmp_root("agr_rn8_")
flora = make_quad("SM_Old_Flora")
grel = make_quad("SM_Old_GroundEl")
dup = make_quad("SM_Old_001_Main")
dup.name = "SM_Old_001_Main.001"
junk = make_quad("SM_Garbage")
bpy.context.scene.agr_rename_address = "New"
with Capture(rnp) as cap:
    bpy.ops.agr.rename_project()
check("Flora renamed", flora.name == "SM_New_Flora", flora.name)
check("GroundEl renamed", grel.name == "SM_New_GroundEl", grel.name)
check(".001 duplicate renamed and keeps its suffix",
      dup.name == "SM_New_001_Main.001", dup.name)
check("non-convention SM_ object untouched", junk.name == "SM_Garbage", junk.name)
check("WARNING lists the skipped object", cap.has('WARNING', 'SM_Garbage'),
      str([m for _l, m in cap.messages]))


# ═══════════════ 9. RENAME-9: geojson button on a plain lowpoly ═══════════════
print("== 9. RENAME-9: 'Переименовать GEOJSON' without a UDIM node ==")
root = tmp_root("agr_rn9_")
sm_dir = os.path.join(root, "SM_Old_Ground")
os.makedirs(sm_dir)
with open(os.path.join(sm_dir, "SM_Old_Ground.geojson"), 'w', encoding='utf-8') as f:
    f.write('{"features": [{"properties": {}, "Glasses": [{"M_Old_Glass_01": {}}]}]}')
d_p = os.path.join(root, "T_Old_Ground_d_1.png")
make_png(d_p)
ob = make_quad("SM_Old_Ground")
mat, nt, bsdf = new_mat("M_Old_Ground_1")
dn = tex_node(nt, d_p)
nt.links.new(dn.outputs['Color'], bsdf.inputs['Base Color'])
ob.data.materials.append(mat)
bpy.context.view_layer.objects.active = ob
ob.select_set(True)
bpy.context.scene.agr_rename_address = "New"
check("poll accepts a Ground object", bpy.ops.agr.rename_geojson.poll())
check("op FINISHED", bpy.ops.agr.rename_geojson() == {'FINISHED'})
check("geojson renamed in the object's own folder",
      os.path.exists(os.path.join(sm_dir, "SM_New_Ground.geojson")),
      str(os.listdir(sm_dir)))

print("== 9b. RENAME-9: poll no longer accepts GroundEl as 'Ground' ==")
grel = make_quad("SM_Old_GroundEl")
bpy.context.view_layer.objects.active = grel
check("poll rejects GroundEl", not bpy.ops.agr.rename_geojson.poll())


# ═══════════════ 10. RENAME-10: light name collisions ═══════════════
print("== 10. RENAME-10: two unnumbered Roots do not silently collide ==")
root = tmp_root("agr_rn10_")


def make_root(name, light_name):
    empty = bpy.data.objects.new(name, None)
    bpy.context.scene.collection.objects.link(empty)
    light_data = bpy.data.lights.new(light_name, type='POINT')
    light = bpy.data.objects.new(light_name, light_data)
    bpy.context.scene.collection.objects.link(light)
    light.parent = empty
    return empty, light


r1, l1 = make_root("Old_Root", "L_one")
r2, l2 = make_root("Old2_Root", "L_two")
bpy.context.scene.agr_rename_address = "New"
with Capture(rs) as cap:
    bpy.ops.agr.rename_project()
names = sorted([l1.name, l2.name])
check("exactly one light took the canonical name",
      sum(1 for n in names if n == "New_Omni_001") == 1, str(names))
check("the loser is NOT left on a temp name",
      not any(n.startswith("__agr_light_tmp_") for n in names), str(names))
check("no silent .001 for the loser", "New_Omni_001.001" not in names, str(names))
check("conflict reported", cap.has('WARNING', 'заняты'), str([m for _l, m in cap.messages]))

print("== 10b. renumbering inside one Root does not self-collide ==")
reset_scene()
empty = bpy.data.objects.new("Old_Root", None)
bpy.context.scene.collection.objects.link(empty)
lights = []
for i in range(3):
    ld = bpy.data.lights.new(f"z_{i}", type='SPOT')
    lo = bpy.data.objects.new(f"z_{i}", ld)
    bpy.context.scene.collection.objects.link(lo)
    lo.parent = empty
    lights.append(lo)
renamed, conflicts = rs.rename_child_lights(None, empty, "New", None, 'Main')
check("all three renamed", renamed == 3, str(renamed))
check("names are the expected sequence",
      sorted(o.name for o in lights) == ["New_Spot_001", "New_Spot_002", "New_Spot_003"],
      str(sorted(o.name for o in lights)))
check("no conflicts", not conflicts, str(conflicts))


# ═══════════════ 11. load_post handler dedup ═══════════════
print("== 11. load_post handler is deduped by __name__ ==")


def _autofill_address_on_load(_dummy):  # a stale copy from a dev reload
    pass


bpy.app.handlers.load_post.append(_autofill_address_on_load)
rn._drop_stale_load_handlers()
bpy.app.handlers.load_post.append(rn._autofill_address_on_load)
same_name = [h for h in bpy.app.handlers.load_post
             if getattr(h, "__name__", "") == "_autofill_address_on_load"]
check("exactly one handler with that name", len(same_name) == 1, str(len(same_name)))


# ═══════════════ 13. R-3: one file per (material, type), no index collision ═══════════════
print("== 13. RENAME-4: index allocation never collides ==")

# (a) a material name index and a material with no index anywhere
root = tmp_root("agr_rn13a_")
pa = os.path.join(root, "T_Wall_d.png")      # no trailing index in the file
pb = os.path.join(root, "T_Roof_d.png")      # no trailing index in the file
make_png(pa, (1, 0, 0, 1))
make_png(pb, (0, 0, 1, 1))
ob = make_quad("SM_Old_Ground")
m1, nt1, b1 = new_mat("M_Old_Ground_1")      # index 1 from the material name
n1 = tex_node(nt1, pa)
nt1.links.new(n1.outputs['Color'], b1.inputs['Base Color'])
m2, nt2, b2 = new_mat("M_Roof")              # no index at all
n2 = tex_node(nt2, pb)
nt2.links.new(n2.outputs['Color'], b2.inputs['Base Color'])
ob.data.materials.append(m1)
ob.data.materials.append(m2)
low = os.path.join(root, "low_texture")
os.makedirs(low, exist_ok=True)
with Capture(rs) as cap:
    count, warns = rs.process_object_textures(None, ob, low, "New", None, "Ground")
i1 = b1.inputs['Base Color'].links[0].from_node.image
i2 = b2.inputs['Base Color'].links[0].from_node.image
check("13a: both materials renamed", count == 2, f"count={count} warns={warns}")
check("13a: no 'имя занято' warning", not any("занято" in w for w in warns), str(warns))
check("13a: indices are _1 and _2",
      {i1.name, i2.name} == {"T_New_Ground_d_1.png", "T_New_Ground_d_2.png"},
      f"{i1.name} / {i2.name}")

# (b) ONE datablock shared by two materials -> two own files
root = tmp_root("agr_rn13b_")
ps = os.path.join(root, "T_Old_Ground_d_1.png")
make_png(ps, (1, 0, 0, 1))
ob = make_quad("SM_Old_Ground")
m1, nt1, b1 = new_mat("M_Old_Ground_1")
n1 = tex_node(nt1, ps)
nt1.links.new(n1.outputs['Color'], b1.inputs['Base Color'])
m2, nt2, b2 = new_mat("M_Old_Ground_2")
n2 = nt2.nodes.new('ShaderNodeTexImage')
n2.image = n1.image                          # SAME datablock
nt2.links.new(n2.outputs['Color'], b2.inputs['Base Color'])
ob.data.materials.append(m1)
ob.data.materials.append(m2)
low = os.path.join(root, "low_texture")
os.makedirs(low, exist_ok=True)
count, warns = rs.process_object_textures(None, ob, low, "New", None, "Ground")
i1 = b1.inputs['Base Color'].links[0].from_node.image
i2 = b2.inputs['Base Color'].links[0].from_node.image
check("13b: shared datablock split into two files", count == 2, f"count={count} warns={warns}")
check("13b: M1 -> _1", i1.name == "T_New_Ground_d_1.png", i1.name)
check("13b: M2 -> _2", i2.name == "T_New_Ground_d_2.png", i2.name)
check("13b: the old shared datablock is gone exactly once",
      bpy.data.images.get("T_Old_Ground_d_1.png") is None, str(img_names()))

# (c) Blender's '.001' duplicate of a convention name
root = tmp_root("agr_rn13c_")
pa = os.path.join(root, "T_A_d.png")
pb = os.path.join(root, "T_B_d.png")
make_png(pa, (1, 0, 0, 1))
make_png(pb, (0, 0, 1, 1))
ob = make_quad("SM_Old_Ground")
m1, nt1, b1 = new_mat("M_Old_Ground_1")
n1 = tex_node(nt1, pa)
nt1.links.new(n1.outputs['Color'], b1.inputs['Base Color'])
m2, nt2, b2 = new_mat("M_Old_Ground_1")      # Blender makes it M_Old_Ground_1.001
n2 = tex_node(nt2, pb)
nt2.links.new(n2.outputs['Color'], b2.inputs['Base Color'])
ob.data.materials.append(m1)
ob.data.materials.append(m2)
low = os.path.join(root, "low_texture")
os.makedirs(low, exist_ok=True)
count, warns = rs.process_object_textures(None, ob, low, "New", None, "Ground")
i2 = b2.inputs['Base Color'].links[0].from_node.image
check("13c: '.001' duplicate gets its own file", count == 2, f"count={count} warns={warns}")
check("13c: it lands on the next free index", i2.name == "T_New_Ground_d_2.png", i2.name)

# (d) a stray file index must not beat the material-name index
root = tmp_root("agr_rn13d_")
pa = os.path.join(root, "T_Old_Ground_d_1.png")
pb = os.path.join(root, "T_Other_d_1.png")   # file says _1, material says _2
make_png(pa, (1, 0, 0, 1))
make_png(pb, (0, 0, 1, 1))
ob = make_quad("SM_Old_Ground")
m1, nt1, b1 = new_mat("M_Old_Ground_1")
n1 = tex_node(nt1, pa)
nt1.links.new(n1.outputs['Color'], b1.inputs['Base Color'])
m2, nt2, b2 = new_mat("M_Old_Ground_2")
n2 = tex_node(nt2, pb)
nt2.links.new(n2.outputs['Color'], b2.inputs['Base Color'])
ob.data.materials.append(m1)
ob.data.materials.append(m2)
low = os.path.join(root, "low_texture")
os.makedirs(low, exist_ok=True)
count, warns = rs.process_object_textures(None, ob, low, "New", None, "Ground")
i2 = b2.inputs['Base Color'].links[0].from_node.image
check("13d: material index wins over the file index",
      count == 2 and i2.name == "T_New_Ground_d_2.png", f"count={count} i2={i2.name}")


# ═══════════════ 14. R-2: the shared UDIM material keeps its Ground name ═══════════════
print("== 14. UDIM material shared by Ground + GroundEl ==")


def udim_mat(name, tile_path=None):
    """Material driving a TILED image — the shared UDIM material of an
    address, the one Ground/GroundEl/Flora all point at."""
    mat, nt, bsdf = new_mat(name)
    node = nt.nodes.new('ShaderNodeTexImage')
    if tile_path:
        img = bpy.data.images.load(tile_path)
    else:
        img = bpy.data.images.new(name + "_tiles", 8, 8)
    img.source = 'TILED'
    node.image = img
    nt.links.new(node.outputs['Color'], bsdf.inputs['Base Color'])
    return mat


root = tmp_root("agr_rn14_")
udim_dir = os.path.join(root, "SM_Old_Ground")
os.makedirs(udim_dir)
tile_png = os.path.join(udim_dir, "T_Old_Ground_Diffuse_1.1001.png")
make_png(tile_png)
obg = make_quad("SM_Old_Ground")
obe = make_quad("SM_Old_GroundEl")
shared = udim_mat("M_Old_Ground_1", tile_png)
obg.data.materials.append(shared)
obe.data.materials.append(shared)
bpy.context.scene.agr_rename_address = "New"
check("14: rename_project FINISHED", bpy.ops.agr.rename_project() == {'FINISHED'})
check("14: shared UDIM material keeps the Ground token",
      shared.name == "M_New_Ground_1", shared.name)

# per-object rename on the sibling must not touch it either
root = tmp_root("agr_rn14b_")
obg = make_quad("SM_Addr_Ground")
obe = make_quad("SM_Addr_GroundEl")
shared = udim_mat("M_Addr_Ground_1")
obg.data.materials.append(shared)
obe.data.materials.append(shared)
bpy.context.scene.agr_rename_address = "Addr"
for o in bpy.data.objects:
    o.select_set(False)
obe.select_set(True)
bpy.context.view_layer.objects.active = obe
check("14b: agr.rename_materials on the sibling FINISHED",
      bpy.ops.agr.rename_materials() == {'FINISHED'})
check("14b: canonical Ground name survives the sibling rename",
      shared.name == "M_Addr_Ground_1", shared.name)

# a plain (non-UDIM) material of a GroundEl is still renamed as before
root = tmp_root("agr_rn14c_")
obe = make_quad("SM_Addr_GroundEl")
plain, _nt, _b = new_mat("Whatever")
obe.data.materials.append(plain)
bpy.context.scene.agr_rename_address = "Addr"
for o in bpy.data.objects:
    o.select_set(False)
obe.select_set(True)
bpy.context.view_layer.objects.active = obe
bpy.ops.agr.rename_materials()
check("14c: a non-UDIM material still gets the object's own type",
      plain.name == "M_Addr_GroundEl_1", plain.name)


# ═══════════════ 12. shared helpers ═══════════════
print("== 12. rename_shared unit checks ==")
check("erm → Non-Color", rs.get_color_space_for_texture_type('erm') == 'Non-Color')
check("do → sRGB", rs.get_color_space_for_texture_type('do') == 'sRGB')
check("e → sRGB", rs.get_color_space_for_texture_type('e') == 'sRGB')
check("unknown type is NOT sRGB by default",
      rs.get_color_space_for_texture_type('zzz') != 'sRGB')
check("type from filename: erm before r",
      rs.get_texture_type_from_filename("T_A_Ground_erm_1.png") == 'erm')
check("type from filename: address tail is not a type",
      rs.get_texture_type_from_filename("T_Volkhonka_D_5_Ground_d_2.png") == 'd')
check("index from filename", rs.texture_index_from_filename("T_A_Ground_d_2.png") == 2)
check("index from material name", rs.material_index_from_name("M_A_Ground_3") == 3)
check("index survives Blender's '.001' duplicate suffix",
      rs.material_index_from_name("M_A_Ground_3.001") == 3,
      str(rs.material_index_from_name("M_A_Ground_3.001")))
check("parse_sm_name knows GroundElGlass",
      rs.parse_sm_name("SM_Addr_GroundElGlass.001") == ("Addr", None, "GroundElGlass"))


# ─────────────────────────── teardown ───────────────────────────
for path in TMP_ROOTS:
    shutil.rmtree(path, ignore_errors=True)

print("=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} CHECKS FAILED:")
    for name in FAILS:
        print("  -", name)
    sys.exit(1)
print("✅ ALL CHECKS PASSED")
