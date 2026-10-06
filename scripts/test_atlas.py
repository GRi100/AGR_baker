# Headless regression suite for AGR atlases (operators_atlas.py).
# Run: blender --background --factory-startup --python scripts/test_atlas.py
#
# Covers the 2026-09 audit findings ATLAS-1..14: material hijack, alpha loss in
# Create Atlas Only, the FBX-blind double-apply guard, uncovered-face guards,
# save/place error reporting, name parsing, enum caching, stale bins and the
# create -> unpack roundtrip.
import os
import shutil
import sys
import tempfile

import bpy

# repo root = parent of scripts/ — works from any checkout location
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import AGR_tools.log as agr_log
import AGR_tools.properties as props
import AGR_tools.operators_sets as opsets
import AGR_tools.operators_atlas as atl

agr_log.register()
props.register()
opsets.register()
atl.register()

from PIL import Image

FAILS = []
ROOT = tempfile.mkdtemp(prefix="agr_atlas_")
BAKE = os.path.join(ROOT, "AGR_BAKE")
os.makedirs(BAKE, exist_ok=True)
BLEND = os.path.join(ROOT, "scene.blend")


def check(name, cond, extra=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def run_op(callop):
    """bpy.ops raises RuntimeError when the operator reports an ERROR —
    normalise that to {'CANCELLED'} so guards can be asserted."""
    try:
        return callop()
    except RuntimeError as exc:
        if "Traceback" in str(exc):
            raise
        return {'CANCELLED'}


class FakeOp:
    """Minimal operator stand-in for the module-level guards."""

    def __init__(self):
        self.reports = []

    def report(self, level, msg):
        self.reports.append((tuple(level)[0], msg))


class FakeCompositor(atl.AtlasCompositingMixin, FakeOp):
    """Mixin under test without a registered Operator (bpy forbids direct
    instantiation of operator classes)."""


def reset_scene():
    try:
        bpy.ops.object.mode_set(mode='OBJECT')
    except RuntimeError:
        pass
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for me in list(bpy.data.meshes):
        if me.users == 0:
            bpy.data.meshes.remove(me)
    for mat in list(bpy.data.materials):
        bpy.data.materials.remove(mat)
    for img in list(bpy.data.images):
        if img.users == 0 and not img.name.startswith('Render Result'):
            bpy.data.images.remove(img)
    bpy.context.scene.agr_texture_sets.clear()
    atl._atlas_enum_fingerprint = None


def make_set(mat_name, size=64, alpha_left=255, color=(200, 30, 30),
             erm=(0, 128, 0), with_normal=True):
    """S_<mat> folder with a HIGH complement (DO + ERM + Normal).
    The left half of DO gets `alpha_left` so alpha loss is measurable."""
    folder = os.path.join(BAKE, f"S_{mat_name}")
    os.makedirs(folder, exist_ok=True)
    do = Image.new('RGBA', (size, size), color + (255,))
    if alpha_left != 255:
        for y in range(size):
            for x in range(size // 2):
                do.putpixel((x, y), color + (alpha_left,))
    do.save(os.path.join(folder, f"T_{mat_name}_DiffuseOpacity.png"))
    Image.new('RGB', (size, size), erm).save(os.path.join(folder, f"T_{mat_name}_ERM.png"))
    if with_normal:
        Image.new('RGB', (size, size), (128, 128, 255)).save(
            os.path.join(folder, f"T_{mat_name}_Normal.png"))
    return folder


def add_scene_set(mat_name, folder, res=64):
    ts = bpy.context.scene.agr_texture_sets.add()
    ts.name = f"S_{mat_name}"
    ts.material_name = mat_name
    ts.folder_path = folder
    ts.resolution = res
    ts.is_selected = True
    ts.is_atlas = False
    ts.has_diffuse_opacity = True
    return ts


def make_quad_object(name, n_faces=2):
    """Plane subdivided into n_faces quads, UVs filling 0..1 per face."""
    bpy.ops.mesh.primitive_plane_add()
    obj = bpy.context.active_object
    obj.name = name
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.subdivide(number_cuts=1)
    bpy.ops.object.mode_set(mode='OBJECT')
    uv = obj.data.uv_layers.active.data
    for poly in obj.data.polygons:
        for k, li in enumerate(poly.loop_indices):
            uv[li].uv = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))[k % 4]
    return obj


def select_only(obj):
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def material_image_dirs(material):
    dirs = set()
    if material and material.use_nodes and material.node_tree:
        for node in material.node_tree.nodes:
            if node.type == 'TEX_IMAGE' and node.image:
                dirs.add(os.path.normcase(os.path.dirname(
                    os.path.abspath(bpy.path.abspath(node.image.filepath)))))
    return dirs


bpy.ops.wm.save_as_mainfile(filepath=BLEND)
bpy.context.scene.agr_baker_settings.atlas_size = '512'
print("ROOT:", ROOT)

# ===================================================================== 1
print("\n== 1. guillotine packer invariants ==")


class FakeSet:
    def __init__(self, name, res):
        self.name = name
        self.resolution = res


def rects_overlap(a, b):
    return not (a['x'] + a['width'] <= b['x'] or b['x'] + b['width'] <= a['x'] or
                a['y'] + a['height'] <= b['y'] or b['y'] + b['height'] <= a['y'])


sets = [FakeSet(f"s{i}", r) for i, r in enumerate([512, 256, 256, 256, 128, 128, 64])]
layout = atl.calculate_atlas_packing_layout(sets, 1024)
check("packer placed every set", len(layout) == len(sets))
check("cells stay inside the atlas",
      all(0 <= it['x'] and 0 <= it['y'] and it['x'] + it['width'] <= 1024
          and it['y'] + it['height'] <= 1024 for it in layout))
check("no two cells overlap",
      all(not rects_overlap(layout[i], layout[j])
          for i in range(len(layout)) for j in range(i + 1, len(layout))))
check("UV region matches the cell in pixels",
      all(abs(it['u_min'] - it['x'] / 1024) < 1e-9 and
          abs(it['u_max'] - (it['x'] + it['width']) / 1024) < 1e-9 for it in layout))
layout2 = atl.calculate_atlas_packing_layout(sets, 1024)
check("packing is deterministic",
      [(it['x'], it['y'], it['width']) for it in layout] ==
      [(it['x'], it['y'], it['width']) for it in layout2])

bins = atl.calculate_multi_atlas_packing([FakeSet(f"m{i}", 256) for i in range(5)], 512)
check("multi packing splits into 2 bins", len(bins) == 2, f"{[len(b) for b in bins]}")
check("multi packing keeps every set",
      sum(len(b) for b in bins) == 5)

# ===================================================================== 2
print("\n== 2. ATLAS-6: SM name parsing via parse_sm_name ==")
check("MainGlass parses", atl.process_object_name("SM_Addr_MainGlass") == ("Addr", "MainGlass"))
check("GroundGlass parses", atl.process_object_name("SM_Addr_GroundGlass") == ("Addr", "GroundGlass"))
check(".001 suffix stripped", atl.process_object_name("SM_Addr_Main.001") == ("Addr", "Main"))
check("number stays part of the address",
      atl.process_object_name("SM_Addr_001_Main") == ("Addr_001", "Main"))
try:
    atl.process_object_name("Cube")
    parsed_cube = True
except Exception:
    parsed_cube = False
check("non-SM name raises", not parsed_cube)

reset_scene()
cube = make_quad_object("Cube")
atlas_type, use_low, addr, otype, warn = atl.resolve_atlas_naming(cube)
check("off-convention name -> HIGH + warning", atlas_type == 'HIGH' and not use_low and bool(warn))
glass = make_quad_object("SM_Addr_MainGlass")
atlas_type, use_low, addr, otype, warn = atl.resolve_atlas_naming(glass)
check("MainGlass -> LOW naming, no warning",
      atlas_type == 'LOW' and use_low and addr == "Addr" and otype == "MainGlass" and warn is None)

# ===================================================================== 3
print("\n== 3. ATLAS-7: face->material and tiled-UV scan via foreach_get ==")
reset_scene()
obj = make_quad_object("FaceMats")
for name in ("MA", "MB"):
    obj.data.materials.append(bpy.data.materials.new(name))
obj.data.polygons[0].material_index = 0
for poly in list(obj.data.polygons)[1:]:
    poly.material_index = 1
names = atl.build_face_material_names(obj)
check("face materials by index", names[0] == "MA" and set(names[1:]) == {"MB"})
obj.data.materials.append(None)  # empty slot
obj.data.polygons[1].material_index = 2
names = atl.build_face_material_names(obj)
check("empty slot yields None", names[1] is None)
obj.data.polygons[1].material_index = 99  # out of range
names = atl.build_face_material_names(obj)
check("out-of-range material_index yields None", names[1] is None)
uncovered = atl.faces_outside_layout(obj, {"MA", "MB"})
check("uncovered faces counted", uncovered == {None: 1}, str(uncovered))

check("unit UVs are not tiled", atl.count_faces_with_uvs_outside_unit(obj) == 0)
uv = obj.data.uv_layers.active.data
for li in obj.data.polygons[2].loop_indices:
    uv[li].uv = (uv[li].uv[0] + 2.0, uv[li].uv[1])
check("one tiled face detected", atl.count_faces_with_uvs_outside_unit(obj) == 1)

# ===================================================================== 4
print("\n== 4. ATLAS-2: Create Atlas Only keeps alpha (HIGH and LOW) ==")
for mode in ('HIGH', 'LOW'):
    reset_scene()
    folder = make_set("Leaf", alpha_left=0)
    add_scene_set("Leaf", folder)
    res = bpy.ops.agr.create_atlas_only(atlas_type=mode)
    check(f"{mode}: operator FINISHED", res == {'FINISHED'}, str(res))
    a_dirs = sorted(d for d in os.listdir(BAKE) if d.startswith('A_'))
    a_dir = os.path.join(BAKE, a_dirs[-1])
    suffix = {'HIGH': ('DiffuseOpacity', 'Opacity', 'Diffuse', 'ERM', 'Roughness'),
              'LOW': ('do', 'o', 'd', 'erm', 'r')}[mode]
    do_path = os.path.join(a_dir, f"T_{a_dirs[-1]}_{suffix[0]}.png")
    check(f"{mode}: DO written", os.path.exists(do_path), do_path)
    do = Image.open(do_path)
    W, H = do.size
    transparent = do.getpixel((10, H - 10))   # source alpha 0 (cell sits bottom-left)
    opaque = do.getpixel((50, H - 10))
    check(f"{mode}: DO is RGBA", do.mode == 'RGBA', do.mode)
    check(f"{mode}: transparent texel keeps colour and alpha 0",
          transparent[3] == 0 and transparent[0] > 150, str(transparent))
    check(f"{mode}: opaque texel stays opaque", opaque[3] == 255, str(opaque))
    do.close()
    op_path = os.path.join(a_dir, f"T_{a_dirs[-1]}_{suffix[1]}.png")
    op_img = Image.open(op_path).convert('L')
    check(f"{mode}: Opacity 0 in the transparent half", op_img.getpixel((10, H - 10)) == 0,
          str(op_img.getpixel((10, H - 10))))
    op_img.close()
    d_img = Image.open(os.path.join(a_dir, f"T_{a_dirs[-1]}_{suffix[2]}.png"))
    check(f"{mode}: Diffuse keeps the colour of transparent texels",
          d_img.convert('RGB').getpixel((10, H - 10))[0] > 150,
          str(d_img.convert('RGB').getpixel((10, H - 10))))
    d_img.close()
    # ERM fallback: the set ships only packed ERM, R must not be black
    erm = Image.open(os.path.join(a_dir, f"T_{a_dirs[-1]}_{suffix[3]}.png")).convert('RGB')
    check(f"{mode}: ERM green from the packed source", erm.getpixel((10, H - 10))[1] > 100,
          str(erm.getpixel((10, H - 10))))
    erm.close()
    r_img = Image.open(os.path.join(a_dir, f"T_{a_dirs[-1]}_{suffix[4]}.png")).convert('L')
    check(f"{mode}: split Roughness follows ERM (no black region)",
          r_img.getpixel((10, H - 10)) > 100, str(r_img.getpixel((10, H - 10))))
    r_img.close()
    shutil.rmtree(a_dir, ignore_errors=True)

# ===================================================================== 5
print("\n== 5. ATLAS-8: save failures are reported, settings restored ==")
reset_scene()
img = bpy.data.images.new("probe", 8, 8)
op = FakeCompositor()
op.reset_compositing_notes()
scene = bpy.context.scene
scene.render.image_settings.compression = 42
# an existing DIRECTORY as the target: save_render always fails on it
bad_path = os.path.join(ROOT, "locked_dir")
os.makedirs(bad_path, exist_ok=True)
ok = op.save_atlas_image(img, bad_path, 'DIFFUSE')
check("save_atlas_image reports failure", ok is False)
check("failure recorded for the report", len(op.compositing_notes()) == 1,
      str(op.compositing_notes()))
check("compression restored", scene.render.image_settings.compression == 42,
      str(scene.render.image_settings.compression))
good_path = os.path.join(ROOT, "probe.png")
check("save_atlas_image reports success", op.save_atlas_image(img, good_path, 'DIFFUSE') is True)
bpy.data.images.remove(img)

print("\n== 5b. temp Atlas_* datablocks are always freed ==")
reset_scene()
folder = make_set("Tmp")
ts = add_scene_set("Tmp", folder)
layout = atl.calculate_atlas_packing_layout([ts], 512)


class FailingCompositor(FakeCompositor):
    """Запись всегда проваливается — контракт _write_atlas_map: путь не
    возвращается, временный датаблок всё равно освобождается."""

    def save_atlas_image(self, image, filepath, texture_type):
        self._save_errors = getattr(self, '_save_errors', [])
        self._save_errors.append(f"{os.path.basename(filepath)}: forced")
        return False


op = FailingCompositor()
op.reset_compositing_notes()
before = len(bpy.data.images)
written = op._write_atlas_map([ts], 'NORMAL', 512, layout, ROOT,
                              atl.atlas_filename_fn("A_tmp"))
check("failed save yields no path", written is None)
check("failure recorded", len(op.compositing_notes()) == 1)
check("no Atlas_* datablock leaks after a failed save",
      len(bpy.data.images) == before and "Atlas_NORMAL_512" not in bpy.data.images)

op_ok = FakeCompositor()
op_ok.reset_compositing_notes()
written_ok = op_ok._write_atlas_map([ts], 'NORMAL', 512, layout, ROOT,
                                    atl.atlas_filename_fn("A_tmp"))
check("successful save returns the path", bool(written_ok) and os.path.exists(written_ok))
check("no Atlas_* datablock leaks after a successful save",
      len(bpy.data.images) == before and "Atlas_NORMAL_512" not in bpy.data.images)

print("\n== 5c. ATLAS-9: missing source maps reach the report ==")
reset_scene()
folder = make_set("NoNormal", with_normal=False)
ts = add_scene_set("NoNormal", folder)
layout = atl.calculate_atlas_packing_layout([ts], 512)
op = FakeCompositor()
op.reset_compositing_notes()
out_dir = os.path.join(ROOT, "nonormal")
os.makedirs(out_dir, exist_ok=True)
created = op.build_atlas_textures([ts], 512, layout, out_dir,
                                  atl.atlas_filename_fn("A_nn"), False)
notes = op.compositing_notes()
check("missing Normal is reported", any("NORMAL" in n for n in notes), str(notes))
check("missing map still yields a written atlas", 'NORMAL' in created)
normal_img = Image.open(created['NORMAL']).convert('RGB')
check("missing Normal filled flat, not black",
      normal_img.getpixel((10, normal_img.size[1] - 10)) == (128, 128, 255),
      str(normal_img.getpixel((10, normal_img.size[1] - 10))))
normal_img.close()

# ===================================================================== 6
print("\n== 6. ATLAS-1/4/13: material hijack and unpack (multi-atlas roundtrip) ==")
reset_scene()
for i in (1, 2):
    mat_name = f"M_Test_Ground_{i}"
    add_scene_set(mat_name, make_set(mat_name, alpha_left=255,
                                     color=(10 * i, 60, 200), erm=(0, 100 + 10 * i, 0)))

obj = make_quad_object("SM_Test_Ground")
for i in (1, 2):
    mat = bpy.data.materials.new(f"M_Test_Ground_{i}")
    mat.use_nodes = True
    mat['agr_probe'] = f"source-{i}"
    mat.node_tree.nodes.new('ShaderNodeValue').name = "SOURCE_MARKER"
    obj.data.materials.append(mat)
for k, poly in enumerate(obj.data.polygons):
    poly.material_index = 0 if k < 2 else 1

# a second object sharing the FIRST source material
shared = make_quad_object("SM_Test_GroundEl")
shared.data.materials.append(bpy.data.materials["M_Test_Ground_1"])

select_only(obj)
res = bpy.ops.agr.create_multi_atlas_from_object()
check("multi-atlas FINISHED", res == {'FINISHED'}, str(res))

atlas_mat = obj.data.materials[0]
check("atlas material carries the canonical bin name", atlas_mat.name == "M_Test_Ground_1",
      atlas_mat.name)
check("atlas material is tagged", bool(atlas_mat.get(atl.ATLAS_MAT_TAG)))
source1 = atl.find_source_material("M_Test_Ground_1")
check("source material survived under .src", source1 is not None,
      source1.name if source1 else "MISSING")
check("source keeps its idprop", source1 and source1.get('agr_probe') == "source-1")
check("source keeps its marker node",
      source1 and "SOURCE_MARKER" in [n.name for n in source1.node_tree.nodes])
check("second object still uses the source, not the atlas",
      shared.data.materials[0] is source1)

atlas_dirs = sorted(d for d in os.listdir(BAKE) if d.startswith('A_'))
atlas_folder = os.path.normcase(os.path.abspath(os.path.join(BAKE, atlas_dirs[0])))
check("shared object shows no atlas textures",
      atlas_folder not in material_image_dirs(shared.data.materials[0]))

uv_after = [tuple(obj.data.uv_layers.active.data[i].uv) for i in range(4)]
check("UVs squeezed into a sub-region", max(max(u) for u in uv_after) < 1.0, str(uv_after[0]))

# --- ATLAS-3: the double-apply guard must survive a default FBX roundtrip
print("\n== 7. ATLAS-3: double-apply guard survives FBX ==")
fbx = os.path.join(ROOT, "delivery.fbx")
select_only(obj)
bpy.ops.export_scene.fbx(filepath=fbx, use_selection=True)
saved_names = {o.name for o in bpy.data.objects}
bpy.ops.import_scene.fbx(filepath=fbx)
imported = [o for o in bpy.data.objects if o.name not in saved_names][0]
check("re-imported object lost the idprop flag", imported.get('agr_atlas_applied') is None)
check("atlas record survived FBX", bool(atl.atlas_record_names(imported)),
      str(atl.atlas_record_names(imported)))
fake = FakeOp()
check("guard now refuses the second remap",
      atl.check_atlas_uv_preconditions(fake, imported) is False)
check("refusal is reported as ERROR",
      fake.reports and fake.reports[0][0] == 'ERROR', str(fake.reports))
bpy.data.objects.remove(imported, do_unlink=True)

# --- unpack back
print("\n== 8. unpack roundtrip (ATLAS-4/13) ==")
select_only(obj)
res = bpy.ops.agr.unpack_atlas_to_materials()
check("unpack FINISHED", res == {'FINISHED'}, str(res))
check("only the used materials got slots", len(obj.data.materials) == 2,
      str([m.name for m in obj.data.materials]))
for slot_mat in obj.data.materials:
    base = slot_mat.name.replace('.atlas', '')
    own = os.path.normcase(os.path.abspath(os.path.join(BAKE, f"S_{base}")))
    dirs = material_image_dirs(slot_mat)
    check(f"{slot_mat.name}: wired to its own set, not the atlas",
          dirs == {own}, f"{dirs}")
uvs = [tuple(obj.data.uv_layers.active.data[i].uv) for i in range(len(obj.data.loops))]
check("UVs restored into 0..1 (1e-6)",
      all(-1e-6 <= u <= 1 + 1e-6 and -1e-6 <= v <= 1 + 1e-6 for u, v in uvs))
check("UV corners restored exactly",
      all(min(abs(c), abs(c - 1.0)) < 1e-6 for uv in uvs for c in uv), str(uvs[:2]))
check("atlas record stripped", not atl.atlas_record_names(obj))
check("applied flag cleared", obj.get('agr_atlas_applied') is None)
check("second object untouched by unpack", shared.data.materials[0] is source1)

# ===================================================================== 9
print("\n== 9. ATLAS-5: uncovered faces block apply before any mutation ==")
reset_scene()
for i in (1, 2):
    mat_name = f"M_Guard_Ground_{i}"
    add_scene_set(mat_name, make_set(mat_name))
obj = make_quad_object("SM_Guard_Ground")
for i in (1, 2):
    mat = bpy.data.materials.new(f"M_Guard_Ground_{i}")
    mat.use_nodes = True
    obj.data.materials.append(mat)
obj.data.materials.append(None)          # empty slot — the classic Ctrl+J leftover
obj.data.polygons[0].material_index = 0
obj.data.polygons[1].material_index = 1
obj.data.polygons[2].material_index = 2  # faces of the empty slot
obj.data.polygons[3].material_index = 1
before_uv = [tuple(uv.uv) for uv in obj.data.uv_layers.active.data]
before_mats = [m.name if m else None for m in obj.data.materials]
select_only(obj)
res = run_op(bpy.ops.agr.create_multi_atlas_from_object)
check("multi-atlas CANCELLED on uncovered faces", res == {'CANCELLED'}, str(res))
check("materials untouched", [m.name if m else None for m in obj.data.materials] == before_mats)
check("UVs untouched", [tuple(uv.uv) for uv in obj.data.uv_layers.active.data] == before_uv)
check("no atlas record written", not atl.atlas_record_names(obj))

print("\n== 9b. object without a UV layer is refused ==")
reset_scene()
add_scene_set("M_NoUV_Ground_1", make_set("M_NoUV_Ground_1"))
obj = make_quad_object("SM_NoUV_Ground")
obj.data.uv_layers.remove(obj.data.uv_layers[0])
mat = bpy.data.materials.new("M_NoUV_Ground_1")
obj.data.materials.append(mat)
select_only(obj)
res = run_op(bpy.ops.agr.create_multi_atlas_from_object)
check("no UV layer -> CANCELLED", res == {'CANCELLED'}, str(res))
check("UV layer not created behind the user's back", len(obj.data.uv_layers) == 0)

# ===================================================================== 10
print("\n== 10. ATLAS-12: orphan bins drop out of the applicable list ==")
stale_root = os.path.join(ROOT, "stale")
for i in (1, 2, 3):
    folder = os.path.join(stale_root, f"A_X_Ground_{i}")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, 'atlas_mapping.json'), 'w', encoding='utf-8') as f:
        f.write('{}')
marked = atl.mark_stale_atlas_bins(stale_root, "A_X_Ground_", 1)
check("bins 2 and 3 marked stale", marked == ["A_X_Ground_2", "A_X_Ground_3"], str(marked))
check("kept bin keeps its mapping",
      os.path.exists(os.path.join(stale_root, "A_X_Ground_1", 'atlas_mapping.json')))
check("stale bin loses atlas_mapping.json",
      not os.path.exists(os.path.join(stale_root, "A_X_Ground_2", 'atlas_mapping.json')) and
      os.path.exists(os.path.join(stale_root, "A_X_Ground_2", 'atlas_mapping.stale.json')))

# ===================================================================== 11
print("\n== 11. ATLAS-11: enum callback is cached by fingerprint ==")
reset_scene()
atl._atlas_enum_cache = []
atl._atlas_enum_fingerprint = None
first = atl.get_available_atlases(None, bpy.context)
second = atl.get_available_atlases(None, bpy.context)
check("second call returns the cached list object", first is second)
os.makedirs(os.path.join(BAKE, "A_CacheProbe"), exist_ok=True)
with open(os.path.join(BAKE, "A_CacheProbe", 'atlas_mapping.json'), 'w', encoding='utf-8') as f:
    f.write('{"atlas_size": 512}')
os.utime(BAKE, None)
third = atl.get_available_atlases(None, bpy.context)
check("a new folder invalidates the cache",
      any(item[1] == "A_CacheProbe" for item in third), str([i[1] for i in third]))

# ===================================================================== 12
print("\n== 12. multi-bin roundtrip: bins keep their own regions ==")
reset_scene()
N_SETS = 5
for i in range(1, N_SETS + 1):
    mat_name = f"M_Multi_Ground_{i}"
    add_scene_set(mat_name, make_set(mat_name, size=256, color=(20 * i, 40, 90)), res=256)

obj = make_quad_object("SM_Multi_Ground")
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.subdivide(number_cuts=1)   # 4 -> 16 faces, enough for 5 materials
bpy.ops.object.mode_set(mode='OBJECT')
uv = obj.data.uv_layers.active.data
for poly in obj.data.polygons:
    for k, li in enumerate(poly.loop_indices):
        uv[li].uv = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))[k % 4]
for i in range(1, N_SETS + 1):
    mat = bpy.data.materials.new(f"M_Multi_Ground_{i}")
    mat.use_nodes = True
    obj.data.materials.append(mat)
for k, poly in enumerate(obj.data.polygons):
    poly.material_index = k % N_SETS

select_only(obj)
res = run_op(bpy.ops.agr.create_multi_atlas_from_object)
check("multi-bin atlas FINISHED", res == {'FINISHED'}, str(res))
check("two atlas materials on the object", len(obj.data.materials) == 2,
      str([m.name for m in obj.data.materials]))
check("every atlas material is tagged",
      all(m.get(atl.ATLAS_MAT_TAG) for m in obj.data.materials))
record = atl.atlas_record_names(obj)
check("record carries both bins", len(record) == 2, str(record))

select_only(obj)
res = run_op(bpy.ops.agr.unpack_atlas_to_materials)
check("multi-bin unpack FINISHED", res == {'FINISHED'}, str(res))
check("all five materials restored", len(obj.data.materials) == N_SETS,
      str([m.name for m in obj.data.materials]))
for slot_mat in obj.data.materials:
    own = os.path.normcase(os.path.abspath(os.path.join(BAKE, f"S_{slot_mat.name}")))
    check(f"{slot_mat.name}: wired to its own set", material_image_dirs(slot_mat) == {own},
          str(material_image_dirs(slot_mat)))
uvs = [tuple(d.uv) for d in obj.data.uv_layers.active.data]
check("multi-bin UVs restored to the unit square (1e-6)",
      all(min(abs(c), abs(c - 1.0)) < 1e-6 for uv_pair in uvs for c in uv_pair), str(uvs[:2]))

# ===================================================================== 13
print("\n== 13. Apply existing atlas: guards and the applied flag ==")
reset_scene()
for i in (1, 2):
    add_scene_set(f"M_ApplyT_Ground_{i}", make_set(f"M_ApplyT_Ground_{i}"))
before_dirs = {d for d in os.listdir(BAKE) if d.startswith('A_')}
check("atlas for the apply test created",
      run_op(lambda: bpy.ops.agr.create_atlas_only(atlas_type='HIGH')) == {'FINISHED'})
new_dirs = {d for d in os.listdir(BAKE) if d.startswith('A_')} - before_dirs
check("exactly one new atlas folder", len(new_dirs) == 1, str(new_dirs))
apply_folder = os.path.join(BAKE, sorted(new_dirs)[0])

obj = make_quad_object("SM_ApplyT_Ground")
for i in (1, 2):
    obj.data.materials.append(bpy.data.materials.new(f"M_ApplyT_Ground_{i}"))
obj.data.materials.append(None)           # empty slot again
obj.data.polygons[0].material_index = 0
obj.data.polygons[1].material_index = 1
obj.data.polygons[2].material_index = 2   # empty slot
obj.data.polygons[3].material_index = 1
before_uv = [tuple(d.uv) for d in obj.data.uv_layers.active.data]
select_only(obj)
res = run_op(lambda: bpy.ops.agr.apply_atlas_to_object(selected_atlas=apply_folder))
check("apply CANCELLED on uncovered faces", res == {'CANCELLED'}, str(res))
check("apply left UVs untouched",
      [tuple(d.uv) for d in obj.data.uv_layers.active.data] == before_uv)
check("apply left the slots untouched", len(obj.data.materials) == 3)
check("no applied flag after the refusal", obj.get('agr_atlas_applied') is None)

obj.data.polygons[2].material_index = 0
obj.data.materials.pop(index=2)
select_only(obj)
res = run_op(lambda: bpy.ops.agr.apply_atlas_to_object(selected_atlas=apply_folder))
check("apply FINISHED once every face is covered", res == {'FINISHED'}, str(res))
check("applied flag set", bool(obj.get('agr_atlas_applied')))
check("single atlas material slot", len(obj.data.materials) == 1,
      str([m.name for m in obj.data.materials]))
check("atlas material tagged", bool(obj.data.materials[0].get(atl.ATLAS_MAT_TAG)))
check("source material moved aside, not hijacked",
      atl.find_source_material("M_ApplyT_Ground_1") is not None)
check("atlas material shows atlas textures",
      material_image_dirs(obj.data.materials[0]) ==
      {os.path.normcase(os.path.abspath(apply_folder))},
      str(material_image_dirs(obj.data.materials[0])))
check("record written onto the object", bool(atl.atlas_record_names(obj)))
uvs = [tuple(d.uv) for d in obj.data.uv_layers.active.data]
check("UVs squeezed into sub-regions", max(max(p) for p in uvs) < 1.0, str(uvs[:2]))
res = run_op(lambda: bpy.ops.agr.apply_atlas_to_object(selected_atlas=apply_folder))
check("second apply refused by the guard", res == {'CANCELLED'}, str(res))

# ===================================================================== 14
print("\n== 14. R-1: '<имя>.src' must not break the neighbours that share the source ==")
check("source_material_name strips .src", atl.source_material_name("M_A_Main_1.src") == "M_A_Main_1")
check("source_material_name strips .src.001",
      atl.source_material_name("M_A_Main_1.src.001") == "M_A_Main_1")
check("source_material_name leaves a canonical name alone",
      atl.source_material_name("M_A_Main_1") == "M_A_Main_1")
check("source_material_name leaves a plain .001 alone",
      atl.source_material_name("M_A_Main_1.001") == "M_A_Main_1.001")
check("source_material_name tolerates None", atl.source_material_name(None) is None)

reset_scene()
for i in (1, 2, 3):
    mat_name = f"M_Shared_Main_{i}"
    add_scene_set(mat_name, make_set(mat_name, color=(20 * i, 60, 200)))
    mat = bpy.data.materials.new(mat_name)
    mat.use_nodes = True

first = make_quad_object("SM_Shared_Main")
first.data.materials.append(bpy.data.materials["M_Shared_Main_1"])
first.data.materials.append(bpy.data.materials["M_Shared_Main_2"])
for k, poly in enumerate(first.data.polygons):
    poly.material_index = 0 if k < 2 else 1

# neighbour sharing material #1 with the object about to be atlased
second = make_quad_object("SM_Shared_GroundEl")
second.data.materials.append(bpy.data.materials["M_Shared_Main_1"])
second.data.materials.append(bpy.data.materials["M_Shared_Main_3"])
for k, poly in enumerate(second.data.polygons):
    poly.material_index = 0 if k < 2 else 1

select_only(first)
check("first object atlased", run_op(bpy.ops.agr.create_multi_atlas_from_object) == {'FINISHED'})
check("neighbour now holds the pushed-aside source",
      second.data.materials[0].name == "M_Shared_Main_1.src",
      str([m.name for m in second.data.materials]))

select_only(second)
res = run_op(bpy.ops.agr.create_multi_atlas_from_object)
check("R-1: neighbour with a '.src' slot still finds its sets", res == {'FINISHED'}, str(res))
check("neighbour got its own atlas material", len(second.data.materials) == 1,
      str([m.name for m in second.data.materials]))

# ... and Apply of an existing layout onto a third sharing object
third = make_quad_object("SM_Shared_Main_2")
third.data.materials.append(bpy.data.materials["M_Shared_Main_1.src"])
third.data.materials.append(bpy.data.materials["M_Shared_Main_2"])
for k, poly in enumerate(third.data.polygons):
    poly.material_index = 0 if k < 2 else 1
first_atlas = sorted(d for d in os.listdir(BAKE) if d.startswith('A_Shared_Main_'))[0]
select_only(third)
res = run_op(lambda: bpy.ops.agr.apply_atlas_to_object(
    selected_atlas=os.path.join(BAKE, first_atlas)))
check("R-1: Apply matches a '.src' slot against the layout", res == {'FINISHED'}, str(res))
uvs = [tuple(d.uv) for d in third.data.uv_layers.active.data]
check("R-1: Apply actually remapped the UVs", max(max(p) for p in uvs) < 1.0, str(uvs[:2]))

# ===================================================================== 15
print("\n== 15. R-2: an UNMARKED atlas material (FBX lost the tag) is reused, not pushed aside ==")
reset_scene()
for i in (1, 2):
    add_scene_set(f"M_Tagless_Ground_{i}", make_set(f"M_Tagless_Ground_{i}"))
before_dirs = {d for d in os.listdir(BAKE) if d.startswith('A_')}
check("atlas for the tagless test created",
      run_op(lambda: bpy.ops.agr.create_atlas_only(atlas_type='HIGH')) == {'FINISHED'})
tagless_folder = os.path.join(
    BAKE, sorted({d for d in os.listdir(BAKE) if d.startswith('A_')} - before_dirs)[0])

owner = make_quad_object("SM_Tagless_Ground")
for i in (1, 2):
    owner.data.materials.append(bpy.data.materials.new(f"M_Tagless_Ground_{i}"))
owner.data.polygons[0].material_index = 0
owner.data.polygons[1].material_index = 0
owner.data.polygons[2].material_index = 1
owner.data.polygons[3].material_index = 1
select_only(owner)
check("apply onto the owner FINISHED",
      run_op(lambda: bpy.ops.agr.apply_atlas_to_object(selected_atlas=tagless_folder)) == {'FINISHED'})
atlas_mat = owner.data.materials[0]
atlas_mat_name = atlas_mat.name
# default FBX delivery carries no Custom Properties — the tag is gone
del atlas_mat[atl.ATLAS_MAT_TAG]
check("tag really removed", atlas_mat.get(atl.ATLAS_MAT_TAG) is None)

neighbour = make_quad_object("SM_Tagless2_Ground")
# only bin #1 collided with a source name, #2 was never pushed aside
neighbour.data.materials.append(bpy.data.materials["M_Tagless_Ground_1.src"])
neighbour.data.materials.append(bpy.data.materials["M_Tagless_Ground_2"])
neighbour.data.polygons[0].material_index = 0
neighbour.data.polygons[1].material_index = 0
neighbour.data.polygons[2].material_index = 1
neighbour.data.polygons[3].material_index = 1
select_only(neighbour)
res = run_op(lambda: bpy.ops.agr.apply_atlas_to_object(selected_atlas=tagless_folder))
check("apply onto the neighbour FINISHED", res == {'FINISHED'}, str(res))
check("R-2: the tagless atlas material kept its canonical name",
      atlas_mat.name == atlas_mat_name, atlas_mat.name)
check("R-2: the tag was restored, not duplicated",
      atlas_mat.get(atl.ATLAS_MAT_TAG) is not None)
check("R-2: both objects share the one atlas material",
      neighbour.data.materials[0] is atlas_mat,
      f"{neighbour.data.materials[0].name} vs {atlas_mat.name}")
check("R-2: no '.src.001' twin was created",
      not any(m.name.endswith('.src.001') for m in bpy.data.materials),
      str([m.name for m in bpy.data.materials]))
check("owner still carries the atlas material", owner.data.materials[0] is atlas_mat)

# ===================================================================== 16
print("\n== 16. R-5: delivery format — E/R/M and Opacity stay 3-channel RGB PNG ==")
reset_scene()
ts_rgb = add_scene_set("M_Fmt_Ground_1", make_set("M_Fmt_Ground_1", alpha_left=90))
layout = atl.calculate_atlas_packing_layout([ts_rgb], 512)
op = FakeCompositor()
op.reset_compositing_notes()
fmt_dir = os.path.join(ROOT, "fmt")
os.makedirs(fmt_dir, exist_ok=True)
created = op.build_atlas_textures([ts_rgb], 512, layout, fmt_dir,
                                  atl.atlas_filename_fn("A_Fmt"), True)
for key in ('EMIT', 'ROUGHNESS', 'METALLIC', 'OPACITY'):
    check(f"{key} written", key in created, str(sorted(created)))
    if key in created:
        with Image.open(created[key]) as im:
            check(f"R-5: {key} is RGB, not grayscale 'L'", im.mode == 'RGB', im.mode)
with Image.open(created['DIFFUSE_OPACITY']) as im:
    check("DO stays RGBA", im.mode == 'RGBA', im.mode)

# R-7: the resolver returns the closure, nothing dead behind it
name_for = atl.atlas_filename_fn("A_Fmt")
check("R-7: atlas_filename_fn returns a callable", callable(name_for))
check("R-7: the resolver still names files", name_for('EMIT') == "T_A_Fmt_Emit.png",
      name_for('EMIT'))

print("=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} CHECKS FAILED:")
    for name in FAILS:
        print("  -", name)
    sys.exit(1)
print("✅ ALL CHECKS PASSED")
