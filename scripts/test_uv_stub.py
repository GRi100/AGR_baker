# Headless test for AGR UV "Развернуть stub" (operators_uv.py stub unwrap +
# core/texture_sets.py material_texture_entry detection).
# Run: blender --background --factory-startup --python scripts/test_uv_stub.py
import os
import sys

import bpy
import bmesh
from mathutils import Vector

# repo root = parent of scripts/ — works from any checkout location
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import AGR_tools.log as agr_log
import AGR_tools.operators_uv as uvmod
from AGR_tools.core.texture_sets import material_texture_entry

agr_log.register()
uvmod.register()

FAILS = []


def check(name, cond, extra=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def expect_cancel(callop):
    try:
        return callop() == {'CANCELLED'}
    except RuntimeError as exc:
        return "Traceback" not in str(exc)


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
        if mat.users == 0:
            bpy.data.materials.remove(mat)
    for img in list(bpy.data.images):
        if img.users == 0:
            bpy.data.images.remove(img)


def make_grid(name, nx, ny, cell=1.0, with_uv=True):
    verts = []
    for j in range(ny + 1):
        for i in range(nx + 1):
            verts.append((i * cell, j * cell, 0.0))
    faces = []
    for j in range(ny):
        for i in range(nx):
            a = j * (nx + 1) + i
            faces.append((a, a + 1, a + nx + 2, a + nx + 1))
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata(verts, [], faces)
    mesh.validate()
    if with_uv:
        mesh.uv_layers.new(name="UVMap")
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def set_face_uvs(obj, poly_index, uvs):
    data = obj.data.uv_layers.active.data
    for li, val in zip(obj.data.polygons[poly_index].loop_indices, uvs):
        data[li].uv = val


def face_uvs(obj, poly_index):
    data = obj.data.uv_layers.active.data
    return [tuple(data[li].uv) for li in obj.data.polygons[poly_index].loop_indices]


def uv_bbox(obj, poly_index):
    pts = face_uvs(obj, poly_index)
    us = [p[0] for p in pts]
    vs = [p[1] for p in pts]
    return (min(us), min(vs), max(us), max(vs))


def bbox_close(bb, ref, tol=1e-5):
    return all(abs(a - b) <= tol for a, b in zip(bb, ref))


def uvs_close(a, b, tol=1e-6):
    return len(a) == len(b) and all(
        abs(pa[0] - pb[0]) <= tol and abs(pa[1] - pb[1]) <= tol
        for pa, pb in zip(a, b))


def make_image(name, size):
    return bpy.data.images.new(name, width=size, height=size)


def make_mat(name, *images):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    for img in images:
        node = mat.node_tree.nodes.new('ShaderNodeTexImage')
        node.image = img
    return mat


def make_tiled_image(name, sizes_by_tile, tmpdir):
    """Real PNG files on disk + a TILED datablock over them (tile sizes may
    stay (0,0) until loaded — exercises the read_png_ihdr disk fallback)."""
    paths = {}
    for tile, size in sizes_by_tile.items():
        img = bpy.data.images.new(f"__tmp_{name}_{tile}", width=size, height=size)
        path = os.path.join(tmpdir, f"T_{name}.{tile}.png")
        img.filepath_raw = path
        img.file_format = 'PNG'
        img.save()
        bpy.data.images.remove(img)
        paths[tile] = path
    tiled = bpy.data.images.load(paths[1001])
    tiled.source = 'TILED'
    for tile in sorted(sizes_by_tile):
        if tile != 1001:
            tiled.tiles.new(tile_number=tile)
    return tiled


def select_only(*objs):
    bpy.ops.object.select_all(action='DESELECT')
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]


MARGIN_BBOX = (0.05, 0.05, 0.95, 0.95)


print("== 1. fixed 256px material: every face fills its margin square ==")
reset_scene()
o1 = make_grid("StubA", 2, 1)
o1.data.materials.append(make_mat("M_StubA", make_image("iA", 256)))
set_face_uvs(o1, 0, [(0.31, 0.30), (0.32, 0.30), (0.32, 0.31), (0.31, 0.31)])
set_face_uvs(o1, 1, [(0.70, 0.70), (0.80, 0.70), (0.80, 0.80), (0.70, 0.80)])
o1['agr_atlas_applied'] = 'M_FakeAtlas'
select_only(o1)
check("op FINISHED", bpy.ops.agr.uv_unwrap_stub() == {'FINISHED'})
check("face 0 bbox = margin square", bbox_close(uv_bbox(o1, 0), MARGIN_BBOX),
      str(uv_bbox(o1, 0)))
check("face 1 bbox = margin square (stacked on face 0)",
      bbox_close(uv_bbox(o1, 1), MARGIN_BBOX))
check("full coverage clears agr_atlas_applied", o1.get('agr_atlas_applied') is None)

print("== 2. non-square face still fills BOTH axes (non-uniform stretch) ==")
reset_scene()
o2 = make_grid("StubRect", 1, 1, cell=1.0)
# stretch the mesh 2x along X: the face becomes 2x1
o2.data.vertices[1].co.x = 2.0
o2.data.vertices[3].co.x = 2.0
o2.data.materials.append(make_mat("M_StubRect", make_image("iR", 128)))
select_only(o2)
check("op FINISHED", bpy.ops.agr.uv_unwrap_stub() == {'FINISHED'})
check("2x1 face bbox = margin square", bbox_close(uv_bbox(o2, 0), MARGIN_BBOX),
      str(uv_bbox(o2, 0)))

print("== 3. big material untouched, op cancels cleanly ==")
reset_scene()
o3 = make_grid("BigB", 1, 1)
o3.data.materials.append(make_mat("M_BigB", make_image("iB", 1024)))
orig = [(0.2, 0.2), (0.4, 0.2), (0.4, 0.4), (0.2, 0.4)]
set_face_uvs(o3, 0, orig)
select_only(o3)
check("no stubs -> CANCELLED", expect_cancel(lambda: bpy.ops.agr.uv_unwrap_stub()))
check("UVs untouched", uvs_close(face_uvs(o3, 0), orig))

print("== 4. mixed slots: only the stub slot is unwrapped ==")
reset_scene()
o4 = make_grid("Mixed", 2, 1)
o4.data.materials.append(make_mat("M_MixSmall", make_image("iS", 256)))
o4.data.materials.append(make_mat("M_MixBig", make_image("iG", 2048)))
o4.data.polygons[1].material_index = 1
set_face_uvs(o4, 0, [(0.1, 0.1), (0.2, 0.1), (0.2, 0.2), (0.1, 0.2)])
big_orig = [(0.5, 0.5), (0.6, 0.5), (0.6, 0.6), (0.5, 0.6)]
set_face_uvs(o4, 1, big_orig)
o4['agr_atlas_applied'] = 'M_FakeAtlas'
select_only(o4)
check("op FINISHED", bpy.ops.agr.uv_unwrap_stub() == {'FINISHED'})
check("stub face unwrapped", bbox_close(uv_bbox(o4, 0), MARGIN_BBOX))
check("big face untouched", uvs_close(face_uvs(o4, 1), big_orig))
check("partial coverage keeps agr_atlas_applied",
      o4.get('agr_atlas_applied') == 'M_FakeAtlas')

print("== 5. UDIM: stub tile unwraps into ITS tile, big tile untouched ==")
reset_scene()
tmpdir = os.path.join(bpy.app.tempdir, "agr_stub_udim")
os.makedirs(tmpdir, exist_ok=True)
tiled = make_tiled_image("Udim", {1001: 512, 1002: 256}, tmpdir)
mat_udim = make_mat("M_Udim", tiled)
entry = material_texture_entry(mat_udim)
check("detection: per-tile sizes resolved (disk fallback ok)",
      entry == ("tiles", {1001: 512, 1002: 256}), str(entry))
o5 = make_grid("UdimObj", 2, 1)
o5.data.materials.append(mat_udim)
tile1_orig = [(0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9)]
set_face_uvs(o5, 0, tile1_orig)                       # tile 1001 (512px)
set_face_uvs(o5, 1, [(1.1, 0.1), (1.9, 0.1), (1.9, 0.9), (1.1, 0.9)])  # 1002
select_only(o5)
check("op FINISHED", bpy.ops.agr.uv_unwrap_stub() == {'FINISHED'})
check("512px tile untouched", uvs_close(face_uvs(o5, 0), tile1_orig))
check("256px tile fills ITS square",
      bbox_close(uv_bbox(o5, 1), (1.05, 0.05, 1.95, 0.95)), str(uv_bbox(o5, 1)))

print("== 6. unit checks: degenerate faces and tile math ==")
sq = [Vector((0, 0, 0)), Vector((1, 0, 0)), Vector((1, 1, 0)), Vector((0, 1, 0))]
tri = uvmod._stub_face_uvs([Vector((0, 0, 0)), Vector((1, 0, 0)), Vector((0, 1, 0))],
                           Vector((0, 0, 1)))
check("triangle fills the square",
      tri is not None and bbox_close((min(u for u, v in tri), min(v for u, v in tri),
                                      max(u for u, v in tri), max(v for u, v in tri)),
                                     MARGIN_BBOX))
check("collinear face -> None",
      uvmod._stub_face_uvs([Vector((0, 0, 0)), Vector((1, 0, 0)), Vector((2, 0, 0))],
                           Vector((0, 0, 1))) is None)
check("zero normal -> None",
      uvmod._stub_face_uvs(sq, Vector((0, 0, 0))) is None)
check("tile number (0.5, 0.5) -> 1001", uvmod._uv_to_udim_number(0.5, 0.5) == 1001)
check("tile number (1.5, 0.5) -> 1002", uvmod._uv_to_udim_number(1.5, 0.5) == 1002)
check("tile number (0.5, 1.5) -> 1011", uvmod._uv_to_udim_number(0.5, 1.5) == 1011)
check("tile offset 1013 -> (2, 1)", uvmod._tile_offset(1013) == (2.0, 1.0))
check("straddling face falls back to mean UV",
      uvmod._face_tile_number([(0.9, 0.1), (1.4, 0.1), (1.4, 0.4), (0.9, 0.4)]) == 1002)
check("negative parking zone -> None (no teleport to 1001)",
      uvmod._uv_to_udim_number(-0.5, 0.5) is None
      and uvmod._uv_to_udim_number(0.5, -0.5) is None)
check("past the 10-column row end -> None (no row wrap)",
      uvmod._uv_to_udim_number(10.5, 0.5) is None)
check("face fully in the negative zone votes None",
      uvmod._face_tile_number([(-0.9, 0.1), (-0.4, 0.1),
                               (-0.4, 0.4), (-0.9, 0.4)]) is None)

print("== 7. edit mode: only the SELECTED faces, no threshold ==")
reset_scene()
o7 = make_grid("EditSel", 2, 2)
o7.data.materials.append(make_mat("M_Big7", make_image("i7", 4096)))
for i in range(4):
    set_face_uvs(o7, i, [(0.2, 0.2), (0.3, 0.2), (0.3, 0.3), (0.2, 0.3)])
select_only(o7)
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.select_mode(type='FACE')
bpy.ops.mesh.select_all(action='DESELECT')
bm = bmesh.from_edit_mesh(o7.data)
bm.faces.ensure_lookup_table()
bm.faces[0].select = True
bm.faces[1].select = True
bmesh.update_edit_mesh(o7.data)
check("selected op FINISHED", bpy.ops.agr.uv_unwrap_stub_selected() == {'FINISHED'})
bpy.ops.object.mode_set(mode='OBJECT')
check("selected face 0 unwrapped", bbox_close(uv_bbox(o7, 0), MARGIN_BBOX))
check("selected face 1 unwrapped", bbox_close(uv_bbox(o7, 1), MARGIN_BBOX))
check("unselected face 2 untouched",
      bbox_close(uv_bbox(o7, 2), (0.2, 0.2, 0.3, 0.3)))
check("unselected face 3 untouched",
      bbox_close(uv_bbox(o7, 3), (0.2, 0.2, 0.3, 0.3)))

print("== 8. edit mode: nothing selected cancels cleanly ==")
select_only(o7)
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.select_all(action='DESELECT')
check("no selection -> CANCELLED",
      expect_cancel(lambda: bpy.ops.agr.uv_unwrap_stub_selected()))
bpy.ops.object.mode_set(mode='OBJECT')

print("== 9. multi-object edit mode ==")
reset_scene()
o9a = make_grid("MultiA", 1, 1)
o9a.data.materials.append(make_mat("M_M9A", make_image("i9a", 1024)))
o9b = make_grid("MultiB", 1, 1)
o9b.data.materials.append(make_mat("M_M9B", make_image("i9b", 1024)))
set_face_uvs(o9a, 0, [(0.4, 0.4), (0.5, 0.4), (0.5, 0.5), (0.4, 0.5)])
set_face_uvs(o9b, 0, [(0.4, 0.4), (0.5, 0.4), (0.5, 0.5), (0.4, 0.5)])
select_only(o9a, o9b)
bpy.ops.object.mode_set(mode='EDIT')  # multi-object edit
bpy.ops.mesh.select_mode(type='FACE')
bpy.ops.mesh.select_all(action='SELECT')
check("multi-object selected op FINISHED",
      bpy.ops.agr.uv_unwrap_stub_selected() == {'FINISHED'})
bpy.ops.object.mode_set(mode='OBJECT')
check("object A unwrapped", bbox_close(uv_bbox(o9a, 0), MARGIN_BBOX))
check("object B unwrapped", bbox_close(uv_bbox(o9b, 0), MARGIN_BBOX))

print("== 10. missing UV layer is auto-created ==")
reset_scene()
o10 = make_grid("NoUV", 1, 1, with_uv=False)
o10.data.materials.append(make_mat("M_NoUV", make_image("i10", 256)))
check("mesh starts without UV layer", o10.data.uv_layers.active is None)
select_only(o10)
check("op FINISHED", bpy.ops.agr.uv_unwrap_stub() == {'FINISHED'})
check("UV layer created", o10.data.uv_layers.active is not None)
check("face unwrapped", bbox_close(uv_bbox(o10, 0), MARGIN_BBOX))

print("== 11. custom threshold and margin from settings ==")
reset_scene()
s = bpy.context.scene.agr_uv_settings
s.stub_threshold = 1024
s.stub_margin = 0.5
o11 = make_grid("Custom", 1, 1)
o11.data.materials.append(make_mat("M_C11", make_image("i11", 1024)))
select_only(o11)
check("1024 counts as stub at threshold 1024",
      bpy.ops.agr.uv_unwrap_stub() == {'FINISHED'})
check("margin 0.5 -> bbox [0.25..0.75]",
      bbox_close(uv_bbox(o11, 0), (0.25, 0.25, 0.75, 0.75)), str(uv_bbox(o11, 0)))
s.stub_threshold = 256
s.stub_margin = 0.9

print("=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} CHECKS FAILED:")
    for name in FAILS:
        print("  -", name)
    sys.exit(1)
print("✅ ALL CHECKS PASSED")
