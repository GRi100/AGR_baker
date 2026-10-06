# Headless test for AGR UDIM (operators_udim.py + core/udim_tiles.py).
# Run: blender --background --factory-startup --python scripts/test_udim.py
import os
import sys
import tempfile

import bpy

# repo root = parent of scripts/ — works from any checkout location
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import AGR_tools.log as agr_log
import AGR_tools.properties as props
import AGR_tools.operators_udim as udimmod
from AGR_tools.core.udim_store import read_udim_record
from AGR_tools.core.udim_tiles import uv_to_udim_number, face_tile_number

agr_log.register()
props.register()
udimmod.register()

FAILS = []


def check(name, cond, extra=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def detached(cls, *names):
    """Instance carrying the operator's plain methods: an Operator subclass
    is a bpy_struct and cannot be instantiated from Python."""
    return type("Detached_" + cls.__name__, (object,),
                {n: getattr(cls, n) for n in names})()


def run_op(callop):
    """Operator result; an ERROR report raises RuntimeError from Python."""
    try:
        return callop()
    except RuntimeError as exc:
        if "Traceback" in str(exc):
            raise
        return {'CANCELLED'}


TMP = tempfile.mkdtemp(prefix="agr_udim_test_")
BLEND = os.path.join(TMP, "scene.blend")
bpy.ops.wm.save_as_mainfile(filepath=BLEND)
BAKE = os.path.join(TMP, "AGR_BAKE")


def write_png(path, rgb):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img = bpy.data.images.new(os.path.basename(path), 4, 4, alpha=True)
    px = []
    for _ in range(16):
        px.extend([rgb[0], rgb[1], rgb[2], 1.0])
    img.pixels.foreach_set(px)
    img.filepath_raw = path
    img.file_format = 'PNG'
    img.save()
    bpy.data.images.remove(img)


def make_set(mat_name, rgb, types=("DiffuseOpacity", "ERM", "Normal")):
    folder = os.path.join(BAKE, f"S_{mat_name}")
    os.makedirs(folder, exist_ok=True)
    for t in types:
        write_png(os.path.join(folder, f"T_{mat_name}_{t}.png"), rgb)
    return folder


def make_obj(obj_name, mat_names):
    mesh = bpy.data.meshes.new(obj_name)
    verts = []
    faces = []
    for i in range(len(mat_names)):
        base = len(verts)
        verts += [(i, 0, 0), (i + 1, 0, 0), (i + 1, 1, 0), (i, 1, 0)]
        faces.append((base, base + 1, base + 2, base + 3))
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    mesh.uv_layers.new(name="UVMap")
    uv = mesh.uv_layers[0].data
    for li, loop in enumerate(mesh.loops):
        corner = li % 4
        uv[li].uv = [(0, 0), (1, 0), (1, 1), (0, 1)][corner]
    obj = bpy.data.objects.new(obj_name, mesh)
    bpy.context.collection.objects.link(obj)
    for idx, mat_name in enumerate(mat_names):
        mat = bpy.data.materials.get(mat_name) or bpy.data.materials.new(mat_name)
        mat.use_nodes = True
        mesh.materials.append(mat)
        mesh.polygons[idx].material_index = idx
    return obj


def activate(obj):
    for o in bpy.data.objects:
        o.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def tile_files(udim_dir, number):
    return sorted(f for f in os.listdir(udim_dir) if f.endswith(f".{number}.png"))


print("== 1. tile-number helper (UDIM-2 / UV-6) ==")
check("u=0,v=0 -> 1001", uv_to_udim_number(0.0, 0.0) == 1001)
check("u=1.0 closes column 0", uv_to_udim_number(1.0, 0.5) == 1001,
      str(uv_to_udim_number(1.0, 0.5)))
check("u=1.5 -> column 1", uv_to_udim_number(1.5, 0.0) == 1002)
check("u=0.0,v=1.0 -> 1001 (right-closed row)", uv_to_udim_number(0.0, 1.0) == 1001,
      str(uv_to_udim_number(0.0, 1.0)))
check("u=0.5,v=1.5 -> 1011", uv_to_udim_number(0.5, 1.5) == 1011)
check("negative u -> None", uv_to_udim_number(-0.5, 0.5) is None)
check("negative v -> None", uv_to_udim_number(0.5, -0.5) is None)
check("u beyond 10 -> None", uv_to_udim_number(10.5, 0.5) is None)
check("u exactly 10.0 stays in column 9", uv_to_udim_number(10.0, 0.0) == 1010)
check("float32 noise on the border keeps the tile",
      uv_to_udim_number(1.0000001, 0.9999999) == 1001,
      str(uv_to_udim_number(1.0000001, 0.9999999)))

square = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
tiles_by_rotation = {face_tile_number(square[i:] + square[:i]) for i in range(4)}
check("face filling a tile exactly votes 1001 in ANY loop order",
      tiles_by_rotation == {1001}, str(tiles_by_rotation))
shifted = [(1.0, 1.0), (2.0, 1.0), (2.0, 2.0), (1.0, 2.0)]
check("face filling tile 1012 exactly", face_tile_number(shifted) == 1012,
      str(face_tile_number(shifted)))
check("empty uv list -> None", face_tile_number([]) is None)
check("face in the negative zone -> None",
      face_tile_number([(-1.0, 0.0), (0.0, 0.0), (0.0, 1.0)]) is None)

print("== 2. tiles_with_uv uses the same rule ==")
mesh = bpy.data.meshes.new("voteplane")
# loop order deliberately starts at the far (1,1) corner
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(2, 3, 0, 1)])
mesh.update()
mesh.uv_layers.new(name="UVMap")
uvs = {2: (1.0, 1.0), 3: (0.0, 1.0), 0: (0.0, 0.0), 1: (1.0, 0.0)}
for loop in mesh.loops:
    mesh.uv_layers[0].data[loop.index].uv = uvs[loop.vertex_index]
vobj = bpy.data.objects.new("voteplane", mesh)
bpy.context.collection.objects.link(vobj)
check("tiles_with_uv returns {1001} regardless of loop order",
      udimmod.tiles_with_uv(vobj) == {1001}, str(udimmod.tiles_with_uv(vobj)))
bpy.data.objects.remove(vobj, do_unlink=True)

print("== 3. Create UDIM on a sibling of the same address (UDIM-1) ==")
make_set("MatA", (1.0, 0.0, 0.0))
make_set("MatB", (0.0, 0.0, 1.0))

ground = make_obj("SM_Addr_Ground", ["MatA"])
activate(ground)
check("create on Ground FINISHED", bpy.ops.agr.create_udim() == {'FINISHED'})

udim_dir = os.path.join(BAKE, "SM_Addr_Ground")
tile_1001 = os.path.join(udim_dir, "T_Addr_Ground_Diffuse_1.1001.png")
bytes_before = open(tile_1001, 'rb').read()
rec_ground = read_udim_record(ground)
check("Ground record has tile 1001 for MatA",
      rec_ground and rec_ground['udim_tiles'][0]['material_name'] == 'MatA')
ground_mat = ground.data.materials[0].name
check("UDIM material got the canonical name (UDIM-X1)",
      ground_mat == "M_Addr_Ground_1", ground_mat)

el = make_obj("SM_Addr_GroundEl", ["MatB"])
activate(el)
check("create on the sibling FINISHED", bpy.ops.agr.create_udim() == {'FINISHED'})
check("tile 1001 on disk NOT overwritten",
      open(tile_1001, 'rb').read() == bytes_before)
check("sibling tiles start at 1002", bool(tile_files(udim_dir, 1002)),
      str(sorted(os.listdir(udim_dir))))
rec_ground2 = read_udim_record(ground)
rec_el = read_udim_record(el)
check("no second record forked on the sibling", rec_el is None)
names = [t['material_name'] for t in rec_ground2['udim_tiles']]
check("one record holds both materials", names == ['MatA', 'MatB'], str(names))
check("sibling reuses the SAME UDIM material datablock",
      el.data.materials[0] is ground.data.materials[0],
      el.data.materials[0].name)
check("no .001 material was created",
      bpy.data.materials.get("M_Addr_Ground_1.001") is None)
uv_el = [tuple(round(c, 3) for c in d.uv) for d in el.data.uv_layers[0].data]
check("sibling UVs moved into tile 1002", all(u >= 1.0 for u, _v in uv_el), str(uv_el))

print("== 4. Create UDIM refuses materials without a full set (UDIM-3) ==")
make_set("MatFull", (0.0, 1.0, 0.0))
make_set("MatPartial", (0.5, 0.5, 0.5), types=("DiffuseOpacity",))
part = make_obj("SM_Part_Ground", ["MatFull", "MatPartial"])
activate(part)
res = run_op(bpy.ops.agr.create_udim)
check("operator CANCELLED", res == {'CANCELLED'}, str(res))
check("no mutation: both slots kept",
      [s.material.name for s in part.material_slots] == ["MatFull", "MatPartial"])
check("no mutation: UVs untouched",
      all(c <= 1.0 for d in part.data.uv_layers[0].data for c in d.uv))
check("no mutation: folder not created",
      not os.path.isdir(os.path.join(BAKE, "SM_Part_Ground")))

print("== 5. Add Sets to UDIM records only what was copied (UDIM-4) ==")
mapping = read_udim_record(ground)
before = len(mapping['udim_tiles'])
ghost = [{
    'material_index': 0,
    'material_name': 'GhostMat',
    'diffuse_path': os.path.join(TMP, 'nope', 'T_GhostMat_DiffuseOpacity.png'),
    'erm_path': os.path.join(TMP, 'nope', 'T_GhostMat_ERM.png'),
    'normal_path': os.path.join(TMP, 'nope', 'T_GhostMat_Normal.png'),
}]
from pathlib import Path as _P
added, failed = udimmod.AGR_OT_AddToUDIM.add_sets_to_udim(
    None, ground, ghost, _P(udim_dir), "Addr", "Ground", 1090, mapping)
after = read_udim_record(ground)
check("added_count is 0", added == 0)
check("failed list names the set", failed == ['GhostMat'], str(failed))
check("no phantom tile in the record", len(after['udim_tiles']) == before,
      f"{before} -> {len(after['udim_tiles'])}")
check("no files for tile 1090", not tile_files(udim_dir, 1090))

good = [{
    'material_index': 0,
    'material_name': 'MatA',
    'diffuse_path': os.path.join(BAKE, "S_MatA", "T_MatA_DiffuseOpacity.png"),
    'erm_path': os.path.join(BAKE, "S_MatA", "T_MatA_ERM.png"),
    'normal_path': os.path.join(BAKE, "S_MatA", "T_MatA_Normal.png"),
}]
added2, failed2 = udimmod.AGR_OT_AddToUDIM.add_sets_to_udim(
    None, ground, good, _P(udim_dir), "Addr", "Ground", 1091, read_udim_record(ground))
check("a real set is added", added2 == 1 and not failed2)
check("record grew by exactly one tile",
      len(read_udim_record(ground)['udim_tiles']) == before + 1)

print("== 6. Add Sets poll accepts a sibling of the carrier ==")
lonely = make_obj("SM_Addr_Flora", ["MatA"])
activate(lonely)
check("sibling carrier found", udimmod.find_sibling_udim_carrier(lonely) is not None)
check("agr.add_to_udim poll passes for the sibling",
      bpy.ops.agr.add_to_udim.poll())
bpy.data.objects.remove(lonely, do_unlink=True)

print("== 7. Two-phase tile rename rolls back (UDIM-5) ==")


ren_dir = os.path.join(TMP, "rename_test")
os.makedirs(ren_dir, exist_ok=True)
for number in (1001, 1002):
    write_png(os.path.join(ren_dir, f"T_X_Diffuse_1.{number}.png"), (0.1, 0.2, 0.3))

editor = detached(udimmod.AGR_OT_UDIMLayoutEditor, '_rename_tile_files', '_adopt_orphan_tmp')
editor._udim_dir = ren_dir

# happy path: swap two tiles
editor._rename_tile_files({1001: 1002, 1002: 1001})
check("swap renamed both tiles",
      sorted(os.listdir(ren_dir)) == ["T_X_Diffuse_1.1001.png", "T_X_Diffuse_1.1002.png"],
      str(sorted(os.listdir(ren_dir))))
check("no .agrtmp left after a successful swap",
      not [f for f in os.listdir(ren_dir) if f.endswith('.agrtmp')])

# failing second phase: the destination is held by a stub that raises
real_rename = os.rename
state = {"failed": False}


def flaky_rename(src, dst):
    # Fail ONCE, on the first phase-2 move — the rollback moves that follow
    # must be allowed through, otherwise the test would be testing itself
    if dst.endswith(".1002.png") and not state["failed"]:
        state["failed"] = True
        raise OSError("simulated lock")
    return real_rename(src, dst)


before_files = sorted(os.listdir(ren_dir))
os.rename = flaky_rename
try:
    raised = False
    try:
        editor._rename_tile_files({1001: 1002, 1002: 1003})
    except RuntimeError:
        raised = True
finally:
    os.rename = real_rename
check("failure in phase 2 raises", raised)
check("rollback restored the original names",
      sorted(os.listdir(ren_dir)) == before_files, str(sorted(os.listdir(ren_dir))))
check("no orphaned .agrtmp after rollback",
      not [f for f in os.listdir(ren_dir) if f.endswith('.agrtmp')])

# orphan adoption
orphan = os.path.join(ren_dir, "T_X_Diffuse_1.1004.png")
write_png(orphan, (0.4, 0.4, 0.4))
real_rename(orphan, orphan + ".agrtmp")
editor._rename_tile_files({})
check("orphaned .agrtmp restored on the next run", os.path.exists(orphan))

# an existing destination is refused before anything moves
write_png(os.path.join(ren_dir, "T_X_Diffuse_1.1005.png"), (0.6, 0.6, 0.6))
blocked = False
snapshot = sorted(os.listdir(ren_dir))
try:
    editor._rename_tile_files({1004: 1005})
except RuntimeError:
    blocked = True
check("rename onto an existing tile refused", blocked)
check("nothing moved after the refusal", sorted(os.listdir(ren_dir)) == snapshot)

print("== 8. convert_tile_to_set does not migrate a legacy JSON (UDIM-6) ==")
import json
legacy_dir = os.path.join(BAKE, "SM_Leg_Ground")
os.makedirs(legacy_dir, exist_ok=True)
write_png(os.path.join(legacy_dir, "T_Leg_Ground_Diffuse_1.1001.png"), (0.2, 0.9, 0.2))
with open(os.path.join(legacy_dir, "udim_mapping.json"), 'w', encoding='utf-8') as f:
    json.dump({"object_name": "SM_Leg_Ground", "address": "Leg", "obj_type": "Ground",
               "udim_tiles": [{"udim_number": 1001, "material_index": 0,
                               "material_name": "MatA", "set_name": "S_MatA"}]}, f)
legacy_obj = make_obj("SM_Leg_Ground", ["MatLegacy"])
conv = detached(udimmod.AGR_OT_ConvertTileToSet, '_convert_tile')
conv._udim_dir = legacy_dir
conv._obj_name = legacy_obj.name
conv._address = "Leg"
conv._agr_bake_dir = BAKE
conv.report = lambda *_a, **_k: None
conv._convert_tile(bpy.context, 1001)
check("no UDIM mirror attributes stamped on the mesh",
      not [a.name for a in legacy_obj.data.attributes if a.name.startswith('AGR_UDIM_T')],
      str([a.name for a in legacy_obj.data.attributes]))
check("no idprop record written", legacy_obj.get('agr_udim_data') is None)
check("set created from the legacy name",
      os.path.isdir(os.path.join(BAKE, "S_MatA")))

print("== 9. Replace Tile prefers DiffuseOpacity (UDIM-7) ==")
rep = detached(udimmod.AGR_OT_ReplaceUDIMTile, '_find_source_for_type', '_compose_diffuse_opacity')
rep._set_folder = os.path.join(BAKE, "S_MatRep")
rep._set_material = "MatRep"
os.makedirs(rep._set_folder, exist_ok=True)
write_png(os.path.join(rep._set_folder, "T_MatRep_Diffuse.png"), (1.0, 1.0, 0.0))
write_png(os.path.join(rep._set_folder, "T_MatRep_DiffuseOpacity.png"), (0.0, 1.0, 1.0))
rep._alpha_warning = None
src = rep._find_source_for_type('Diffuse', os.path.join(TMP, "target.png"))
check("DiffuseOpacity wins over Diffuse", src.endswith("DiffuseOpacity.png"), str(src))
check("no alpha warning when DO exists", rep._alpha_warning is None)

# only Diffuse + Opacity -> compose RGBA
rep2 = detached(udimmod.AGR_OT_ReplaceUDIMTile, '_find_source_for_type', '_compose_diffuse_opacity')
rep2._set_folder = os.path.join(BAKE, "S_MatSplit")
rep2._set_material = "MatSplit"
os.makedirs(rep2._set_folder, exist_ok=True)
write_png(os.path.join(rep2._set_folder, "T_MatSplit_Diffuse.png"), (1.0, 0.0, 1.0))
write_png(os.path.join(rep2._set_folder, "T_MatSplit_Opacity.png"), (0.0, 0.0, 0.0))
rep2._alpha_warning = None
composed_target = os.path.join(TMP, "composed.png")
src2 = rep2._find_source_for_type('Diffuse', composed_target)
try:
    from PIL import Image as _PIL
except ImportError:
    _PIL = None
if _PIL is not None:
    check("RGBA composed from Diffuse + Opacity", src2 == 'COMPOSED', str(src2))
    check("composed file has an alpha channel",
          _PIL.open(composed_target).mode == 'RGBA')
    check("no alpha warning when composing", rep2._alpha_warning is None)
else:
    check("no Pillow -> explicit alpha warning", rep2._alpha_warning is not None)

# no alpha source at all -> explicit warning
rep3 = detached(udimmod.AGR_OT_ReplaceUDIMTile, '_find_source_for_type', '_compose_diffuse_opacity')
rep3._set_folder = os.path.join(BAKE, "S_MatFlat")
rep3._set_material = "MatFlat"
os.makedirs(rep3._set_folder, exist_ok=True)
write_png(os.path.join(rep3._set_folder, "T_MatFlat_Diffuse.png"), (0.3, 0.3, 0.3))
rep3._alpha_warning = None
src3 = rep3._find_source_for_type('Diffuse', os.path.join(TMP, "t3.png"))
check("plain Diffuse still used as a fallback", src3.endswith("T_MatFlat_Diffuse.png"))
check("alpha loss reported", rep3._alpha_warning is not None, str(rep3._alpha_warning))

print("== 10. object_has_udim cache (UDIM-10) ==")
plain = make_obj("SM_Cache_Ground", ["MatCache"])
check("plain object has no UDIM", udimmod.object_has_udim(plain) is False)
img = bpy.data.images.new("cache_img", 8, 8)
node = plain.data.materials[0].node_tree.nodes.new('ShaderNodeTexImage')
node.image = img
check("still no UDIM with a FILE image", udimmod.object_has_udim(plain) is False)
img.source = 'TILED'
check("flipping image.source is seen without an explicit invalidate",
      udimmod.object_has_udim(plain) is True)
check("cache is keyed by session_uid",
      plain.session_uid in udimmod._has_udim_cache)

print("== 11. HUD zombie protection (UDIM-8) ==")
check("WindowManager token registered",
      hasattr(bpy.types.WindowManager, 'agr_udim_hud_token'))
check("load_pre handler installed",
      any(getattr(h, '__name__', '') == '_on_load_pre_hud'
          for h in bpy.app.handlers.load_pre))
check("load_post cache reset installed",
      any(getattr(h, '__name__', '') == '_invalidate_udim_cache_on_load'
          for h in bpy.app.handlers.load_post))


class _Zombie(udimmod.AGR_UDIMGridHUD):
    def __init__(self):
        self._udim_token = 12345
        self._handle = None
        self._gpu_textures = {}
        self._preview_images = []
        self._finished = False

    def _hud_finish(self, context):
        self._finished = True


zombie = _Zombie()
bpy.context.window_manager.agr_udim_hud_token = 999
result = zombie._handle_common(bpy.context, type('E', (), {'type': 'MOUSEMOVE', 'value': 'PRESS'})())
check("stale-token modal self-cancels", result == {'CANCELLED'}, str(result))
check("stale-token modal cleaned itself up", zombie._finished)

print("== 2b. tiles_with_uv is vectorised and keeps the per-face rule (R-9) ==")
vmesh = bpy.data.meshes.new("multitile")
vverts = []
vfaces = []
for i in range(3):
    b = len(vverts)
    vverts += [(i, 0, 0), (i + 1, 0, 0), (i + 1, 1, 0), (i, 1, 0)]
    vfaces.append((b, b + 1, b + 2, b + 3))
vmesh.from_pydata(vverts, [], vfaces)
vmesh.update()
vmesh.uv_layers.new(name="UVMap")
# face 0 -> tile 1001, face 1 -> tile 1012, face 2 -> the negative (invalid) zone
corners = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
offsets = [(0.0, 0.0), (1.0, 1.0), (-3.0, 0.0)]
for fi, poly in enumerate(vmesh.polygons):
    du, dv = offsets[fi]
    for ci, li in enumerate(poly.loop_indices):
        vmesh.uv_layers[0].data[li].uv = (corners[ci][0] + du, corners[ci][1] + dv)
mobj = bpy.data.objects.new("multitile", vmesh)
bpy.context.collection.objects.link(mobj)
check("multi-tile mesh reports exactly its two valid tiles",
      udimmod.tiles_with_uv(mobj) == {1001, 1012},
      str(sorted(udimmod.tiles_with_uv(mobj))))
bpy.data.objects.remove(mobj, do_unlink=True)

empty_mesh = bpy.data.meshes.new("nofaces")
empty_mesh.uv_layers.new(name="UVMap")
eobj = bpy.data.objects.new("nofaces", empty_mesh)
bpy.context.collection.objects.link(eobj)
check("mesh without polygons returns an empty set",
      udimmod.tiles_with_uv(eobj) == set(), str(udimmod.tiles_with_uv(eobj)))
bpy.data.objects.remove(eobj, do_unlink=True)


print("== 12. Shared address folder: pickers, layout, revert (R-1/R-4/R-5) ==")
make_set("MatX", (1.0, 0.5, 0.0))
make_set("MatY", (0.0, 0.5, 1.0))
make_set("MatZ", (0.5, 0.0, 0.5))

shr_g = make_obj("SM_Shr_Ground", ["MatX", "MatY"])
activate(shr_g)
check("12: create on the carrier FINISHED", run_op(bpy.ops.agr.create_udim) == {'FINISHED'})
shr_e = make_obj("SM_Shr_GroundEl", ["MatZ"])
activate(shr_e)
check("12: create on the sibling FINISHED", run_op(bpy.ops.agr.create_udim) == {'FINISHED'})
shr_dir = os.path.join(BAKE, "SM_Shr_Ground")


class _SilentOp:
    def report(self, *_a, **_k):
        pass


# R-5: the sibling has no record of its own — the picker must read the carrier's
resolved = udimmod._resolve_udim_context(_SilentOp(), bpy.context)
labels = resolved[2] if resolved else {}
check("12: picker labels on the sibling come from the carrier record",
      labels.get(1001) == 'MatX' and labels.get(1003) == 'MatZ', str(labels))

# R-4: every guard has to see BOTH objects of the shared folder
users = udimmod.objects_using_udim_dir(shr_dir)
check("12: both siblings are found as users of the shared folder",
      {o.name for o in users} == {"SM_Shr_Ground", "SM_Shr_GroundEl"},
      str(sorted(o.name for o in users)))
guard = set()
for user in users:
    guard |= udimmod.tiles_with_uv(user)
check("12: the delete guard covers the sibling's tile too",
      {1001, 1002, 1003} <= guard, str(sorted(guard)))

# R-4: dragging the sibling's tile from the carrier moves ITS UVs as well
editor = detached(udimmod.AGR_OT_UDIMLayoutEditor, '_apply', '_rename_tile_files',
                  '_adopt_orphan_tmp', '_shift_uvs', '_update_mapping', '_reload_images')
editor._udim_dir = shr_dir
editor._obj_name = shr_g.name          # dragged from the CARRIER
editor._slots = {1001: (0, 0), 1002: (1, 0), 1003: (3, 0)}   # 1003 -> 1004
editor.report = lambda *_a, **_k: None
uv_before = [tuple(d.uv) for d in shr_e.data.uv_layers[0].data]
res = editor._apply(bpy.context)
uv_after = [tuple(d.uv) for d in shr_e.data.uv_layers[0].data]
check("12: layout apply FINISHED", res == {'FINISHED'}, str(res))
check("12: tile files renamed to 1004", bool(tile_files(shr_dir, 1004)),
      str(sorted(os.listdir(shr_dir))))
check("12: the sibling's UVs followed its dragged tile",
      all(round(a[0] - b[0], 3) == 1.0 and round(a[1] - b[1], 3) == 0.0
          for a, b in zip(uv_after, uv_before)),
      f"{uv_before} -> {uv_after}")
rec_shr = read_udim_record(shr_g)
check("12: the carrier record was renumbered",
      {t['udim_number'] for t in rec_shr['udim_tiles']} == {1001, 1002, 1004},
      str(sorted(t['udim_number'] for t in rec_shr['udim_tiles'])))

# R-1: the revert scope of a sibling is its OWN tile only
actual = udimmod.scan_udim_tiles_in_dir(shr_dir)
borrowed = udimmod.borrow_carrier_mapping(shr_e, shr_dir)
tiles, skipped, subset, own, others = udimmod.revert_tile_scope(
    shr_e, shr_dir, actual, borrowed)
check("12: revert scope of the sibling is its own tile", tiles == {1004}, str(sorted(tiles)))
check("12: the carrier's tiles are left alone", skipped == {1001, 1002}, str(sorted(skipped)))
check("12: nothing is 'missing from the mapping' -> no scary dialog",
      not (tiles - {t['udim_number'] for t in subset['udim_tiles']}),
      str(subset['udim_tiles']))

activate(shr_e)
check("12: revert on the sibling FINISHED", run_op(bpy.ops.agr.revert_udim) == {'FINISHED'})
check("12: the sibling got exactly its own material back",
      [m.name for m in shr_e.data.materials] == ['MatZ'],
      str([m.name for m in shr_e.data.materials]))
check("12: no generic M_#_#### materials were created",
      not [m.name for m in bpy.data.materials if m.name.startswith("M_1_")],
      str([m.name for m in bpy.data.materials if m.name.startswith("M_1_")]))
rec_shr2 = read_udim_record(shr_g)
check("12: the carrier record dropped only the reverted tile",
      {t['udim_number'] for t in rec_shr2['udim_tiles']} == {1001, 1002},
      str(sorted(t['udim_number'] for t in rec_shr2['udim_tiles'])))
check("12: the reverted tile files stay on disk (shared folder)",
      bool(tile_files(shr_dir, 1004)))
check("12: the carrier keeps its UDIM material",
      udimmod.object_has_udim(shr_g))

print("== 12b. Carrier reverts first: the record survives on the sibling (R-1) ==")
make_set("MatP", (0.2, 0.2, 0.2))
make_set("MatQ", (0.8, 0.8, 0.8))
hnd_g = make_obj("SM_Hnd_Ground", ["MatP"])
activate(hnd_g)
check("12b: create on the carrier FINISHED", run_op(bpy.ops.agr.create_udim) == {'FINISHED'})
hnd_e = make_obj("SM_Hnd_GroundEl", ["MatQ"])
activate(hnd_e)
check("12b: create on the sibling FINISHED", run_op(bpy.ops.agr.create_udim) == {'FINISHED'})
hnd_dir = os.path.join(BAKE, "SM_Hnd_Ground")

activate(hnd_g)
check("12b: revert on the carrier FINISHED", run_op(bpy.ops.agr.revert_udim) == {'FINISHED'})
check("12b: the carrier got only its own material back",
      [m.name for m in hnd_g.data.materials] == ['MatP'],
      str([m.name for m in hnd_g.data.materials]))
carrier, cmap = udimmod.find_udim_record_carrier(hnd_dir)
check("12b: the record was handed over to the sibling",
      carrier is not None and carrier.name == "SM_Hnd_GroundEl",
      carrier.name if carrier else None)
check("12b: it still holds the sibling's tile",
      bool(cmap) and {t['udim_number'] for t in cmap['udim_tiles']} == {1002},
      str(cmap['udim_tiles'] if cmap else None))
activate(hnd_e)
check("12b: revert on the sibling FINISHED", run_op(bpy.ops.agr.revert_udim) == {'FINISHED'})
check("12b: the sibling got its original material, not M_#_####",
      [m.name for m in hnd_e.data.materials] == ['MatQ'],
      str([m.name for m in hnd_e.data.materials]))

print("== 12c. Create UDIM ignores a slot without faces (R-6) ==")
lo = make_obj("SM_Leftover_Ground", ["MatP"])
lo.data.materials.append(bpy.data.materials.new("LeftoverMat"))   # no S_ folder, no faces
activate(lo)
check("12c: a leftover empty slot does not block Create",
      run_op(bpy.ops.agr.create_udim) == {'FINISHED'})
check("12c: the empty slot is gone with the rest",
      [m.name for m in lo.data.materials] == ["M_Leftover_Ground_1"],
      str([m.name for m in lo.data.materials]))
# a slot WITH faces and without a set is still refused (UDIM-3 stays intact)
make_set("MatR", (0.4, 0.4, 0.4))
strict = make_obj("SM_Strict_Ground", ["MatR", "NoSetMat"])
activate(strict)
check("12c: a used slot without a set still blocks Create",
      run_op(bpy.ops.agr.create_udim) == {'CANCELLED'})


print("=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} CHECKS FAILED:")
    for name in FAILS:
        print("  -", name)
    sys.exit(1)
print("✅ ALL CHECKS PASSED")
