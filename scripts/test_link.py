# Headless test for AGR_tools/operators_link.py
# Run: blender --background --factory-startup --python scripts/test_link.py
import os
import sys

import bpy
from math import radians
from mathutils import Euler, Matrix, Vector

# repo root = parent of scripts/ — works from any checkout location
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import AGR_tools.log as agr_log
import AGR_tools.operators_link as linkmod

agr_log.register()
linkmod.register()

ATTR = linkmod.ATTR_NAME
KEY = linkmod.PROP_KEY

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


def reset_scene():
    bpy.ops.object.select_all(action='DESELECT')
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for me in list(bpy.data.meshes):
        if me.users == 0:
            bpy.data.meshes.remove(me)
    for mat in list(bpy.data.materials):
        if mat.users == 0:
            bpy.data.materials.remove(mat)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)


CUBE_VERTS = [(-0.5, -0.5, -0.5), (0.5, -0.5, -0.5), (0.5, 0.5, -0.5), (-0.5, 0.5, -0.5),
              (-0.5, -0.5, 0.5), (0.5, -0.5, 0.5), (0.5, 0.5, 0.5), (-0.5, 0.5, 0.5)]
CUBE_FACES = [(0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1),
              (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0)]


def make_cube_mesh(name):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(CUBE_VERTS, [], CUBE_FACES)
    mesh.validate()
    return mesh


def add_obj(name, mesh, matrix=None, coll=None):
    obj = bpy.data.objects.new(name, mesh)
    (coll or bpy.context.scene.collection).objects.link(obj)
    if matrix is not None:
        obj.matrix_world = matrix
    return obj


def select_only(objs, active):
    bpy.ops.object.select_all(action='DESELECT')
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = active


def mat_close(a, b, tol=1e-4):
    return all(abs(a[i][j] - b[i][j]) < tol for i in range(4) for j in range(4))


def local_coords_match(obj, tol=1e-4):
    if len(obj.data.vertices) != len(CUBE_VERTS):
        return False
    for v, ref in zip(obj.data.vertices, CUBE_VERTS):
        if (v.co - Vector(ref)).length > tol:
            return False
    return True


def T(x, y, z):
    return Matrix.Translation((x, y, z))


def TRS(loc, rot=(0, 0, 0), scale=(1, 1, 1)):
    m = Matrix.LocRotScale(Vector(loc), Euler([radians(a) for a in rot]), Vector(scale))
    return m


# ---------------------------------------------------------------------------
print("\n=== 1. Basic: 3 linked copies + 1 unique, join, separate all ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0), rot=(0, 0, 45)))
a3 = add_obj("A3", mesh_a, TRS((6, 1, 2), rot=(15, 0, 90), scale=(2, 2, 2)))
mesh_u = make_cube_mesh("MeshU")
u1 = add_obj("U1", mesh_u, TRS((0, 5, 0), scale=(1, 3, 1)))
orig = {o.name: o.matrix_world.copy() for o in (a1, a2, a3, u1)}

select_only([a1, a2, a3, u1], a1)
check("join FINISHED", bpy.ops.agr.link_join() == {'FINISHED'})
check("only container left", len(bpy.data.objects) == 1)
cont = bpy.data.objects[0]
check("container is ex-active", cont.name == "A1")
check("container has table", cont.get(KEY) is not None)
check("container has attribute", cont.data.attributes.get(ATTR) is not None)
check("container faces = 24", len(cont.data.polygons) == 24)
table = linkmod.read_table(cont)
check("table: 4 instances", len(table["instances"]) == 4)
check("table: 2 groups", len(table["groups"]) == 2)
ids_in_attr = set()
for poly_attr in cont.data.attributes[ATTR].data:
    ids_in_attr.add(poly_attr.value)
check("attribute ids match table", ids_in_attr == {int(i) for i in table["instances"]})

select_only([cont], cont)
check("separate FINISHED", bpy.ops.agr.link_separate_all() == {'FINISHED'})
check("container gone", bpy.data.objects.get("A1.__agr_link_tmp") is None)
check("4 objects restored", len(bpy.data.objects) == 4)
restored = {o.name: o for o in bpy.data.objects}
check("names restored", set(restored) == {"A1", "A2", "A3", "U1"})
for name in ("A1", "A2", "A3", "U1"):
    o = restored.get(name)
    check(f"{name} matrix restored", o is not None and mat_close(o.matrix_world, orig[name]))
    check(f"{name} local coords intact", o is not None and local_coords_match(o))
    check(f"{name} no attr leftover", o is not None and o.data.attributes.get(ATTR) is None)
    check(f"{name} no table prop", o is not None and o.get(KEY) is None)
a_datas = {restored["A1"].data, restored["A2"].data, restored["A3"].data}
check("A1/A2/A3 share one mesh", len(a_datas) == 1)
check("shared mesh named MeshA", restored["A1"].data.name == "MeshA")
check("U1 mesh unique", restored["U1"].data not in a_datas)
check("restored objects selected", all(restored[n].select_get() for n in restored))

# ---------------------------------------------------------------------------
print("\n=== 2. Move container after join → delta applies to all ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
orig = {o.name: o.matrix_world.copy() for o in (a1, a2)}
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
delta = TRS((10, -2, 1), rot=(0, 0, 30))
cont.matrix_world = delta @ cont.matrix_world
select_only([cont], cont)
bpy.ops.agr.link_separate_all()
restored = {o.name: o for o in bpy.data.objects}
for name in ("A1", "A2"):
    check(f"{name} got container delta",
          mat_close(restored[name].matrix_world, delta @ orig[name]))
check("moved: still linked", restored["A1"].data == restored["A2"].data)

# ---------------------------------------------------------------------------
print("\n=== 3. Nested joins + moves between joins ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
mesh_b = make_cube_mesh("MeshB")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((2, 0, 0)))
b1 = add_obj("B1", mesh_b, TRS((0, 10, 0), rot=(0, 0, 10)))
b2 = add_obj("B2", mesh_b, TRS((2, 10, 0), rot=(0, 30, 0)))
u1 = add_obj("U1", make_cube_mesh("MeshU"), TRS((-5, -5, 0)))
orig = {o.name: o.matrix_world.copy() for o in (a1, a2, b1, b2, u1)}

select_only([a1, a2], a1)
bpy.ops.agr.link_join()
c1 = bpy.data.objects.get("A1")
select_only([b1, b2], b1)
bpy.ops.agr.link_join()
c2 = bpy.data.objects.get("B1")
check("two containers made", c1 is not None and c2 is not None)

move_b = T(0, 5, 0)
c2.matrix_world = move_b @ c2.matrix_world

select_only([c1, c2, u1], c1)
check("nested join FINISHED", bpy.ops.agr.link_join() == {'FINISHED'})
check("single container", len(bpy.data.objects) == 1)
cont = bpy.data.objects[0]
table = linkmod.read_table(cont)
check("nested: 5 instances", len(table["instances"]) == 5)
check("nested: 3 groups", len(table["groups"]) == 3)

move_all = T(100, 0, 0)
cont.matrix_world = move_all @ cont.matrix_world
select_only([cont], cont)
bpy.ops.agr.link_separate_all()
restored = {o.name: o for o in bpy.data.objects}
check("nested: 5 restored", len(restored) == 5)
for name in ("A1", "A2", "U1"):
    check(f"nested {name} matrix", mat_close(restored[name].matrix_world, move_all @ orig[name]))
for name in ("B1", "B2"):
    check(f"nested {name} matrix (both moves)",
          mat_close(restored[name].matrix_world, move_all @ move_b @ orig[name]))
check("nested A linked", restored["A1"].data == restored["A2"].data)
check("nested B linked", restored["B1"].data == restored["B2"].data)
check("nested A vs B distinct", restored["A1"].data != restored["B1"].data)

# ---------------------------------------------------------------------------
print("\n=== 4. Edited instance stays unique, others re-link ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
a3 = add_obj("A3", mesh_a, TRS((6, 0, 0)))
select_only([a1, a2, a3], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
table = linkmod.read_table(cont)
a2_id = next(int(i) for i, inst in table["instances"].items() if inst["name"] == "A2")
attr = cont.data.attributes[ATTR]
edited_vert = None
for poly, pa in zip(cont.data.polygons, attr.data):
    if pa.value == a2_id:
        edited_vert = poly.vertices[0]
        break
cont.data.vertices[edited_vert].co.x += 0.3
select_only([cont], cont)
result = bpy.ops.agr.link_separate_all()
check("edited: separate ran", result == {'FINISHED'})
restored = {o.name: o for o in bpy.data.objects}
check("edited: 3 restored", len(restored) == 3)
check("edited: A1+A3 linked", restored["A1"].data == restored["A3"].data)
check("edited: A2 unique", restored["A2"].data != restored["A1"].data)
check("edited: A1 coords pristine", local_coords_match(restored["A1"]))
check("edited: A2 keeps the edit", not local_coords_match(restored["A2"]))

# ---------------------------------------------------------------------------
print("\n=== 5. Mirrored instance (negative scale) ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
ref_normals = [poly.normal.copy() for poly in mesh_a.polygons]


def normals_match_ref(mesh, tol=1e-3):
    if len(mesh.polygons) != len(ref_normals):
        return False
    return all((poly.normal - ref).length < tol
               for poly, ref in zip(mesh.polygons, ref_normals))


a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((4, 0, 0), scale=(-1, 1, 1)))
orig = {o.name: o.matrix_world.copy() for o in (a1, a2)}
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
select_only([cont], cont)
bpy.ops.agr.link_separate_all()
restored = {o.name: o for o in bpy.data.objects}
check("mirror: A2 matrix restored", mat_close(restored["A2"].matrix_world, orig["A2"]))
check("mirror: coords intact", local_coords_match(restored["A2"]))
check("mirror: linked again", restored["A1"].data == restored["A2"].data)
check("mirror: orientation as original", normals_match_ref(restored["A1"].data))

print("--- 5b. Mirrored object is the ACTIVE one (mesh rebuilt from it) ---")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0), scale=(-1, 1, 1)))  # mirrored active
a2 = add_obj("A2", mesh_a, TRS((4, 0, 0)))
orig = {o.name: o.matrix_world.copy() for o in (a1, a2)}
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
select_only([cont], cont)
bpy.ops.agr.link_separate_all()
restored = {o.name: o for o in bpy.data.objects}
check("mirror-active: A1 matrix restored", mat_close(restored["A1"].matrix_world, orig["A1"]))
check("mirror-active: A2 matrix restored", mat_close(restored["A2"].matrix_world, orig["A2"]))
check("mirror-active: coords intact", local_coords_match(restored["A1"]))
check("mirror-active: linked again", restored["A1"].data == restored["A2"].data)
check("mirror-active: orientation as original", normals_match_ref(restored["A1"].data))

# ---------------------------------------------------------------------------
print("\n=== 6. Materials: slots + face assignment restored ===")
reset_scene()
mat_red = bpy.data.materials.new("M_Red")
mat_blue = bpy.data.materials.new("M_Blue")
mat_green = bpy.data.materials.new("M_Green")
mesh_a = make_cube_mesh("MeshA")
mesh_a.materials.append(mat_red)
mesh_a.materials.append(mat_blue)
mesh_a.polygons[1].material_index = 1  # top face blue
mesh_u = make_cube_mesh("MeshU")
mesh_u.materials.append(mat_green)
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
u1 = add_obj("U1", mesh_u, TRS((0, 5, 0)))
select_only([a1, a2, u1], u1)  # active = the green one, slot orders will merge
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
select_only([cont], cont)
bpy.ops.agr.link_separate_all()
restored = {o.name: o for o in bpy.data.objects}
a_mats = [m.name if m else "" for m in restored["A1"].data.materials]
u_mats = [m.name if m else "" for m in restored["U1"].data.materials]
check("mats: A slots restored", a_mats == ["M_Red", "M_Blue"], str(a_mats))
check("mats: U slots restored", u_mats == ["M_Green"], str(u_mats))
check("mats: A top face blue", restored["A1"].data.polygons[1].material_index == 1)
check("mats: A other faces red", restored["A1"].data.polygons[0].material_index == 0)
check("mats: A linked again", restored["A1"].data == restored["A2"].data)

# ---------------------------------------------------------------------------
print("\n=== 7. Partial extraction by group ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
mesh_b = make_cube_mesh("MeshB")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((2, 0, 0)))
b1 = add_obj("B1", mesh_b, TRS((0, 5, 0)))
b2 = add_obj("B2", mesh_b, TRS((2, 5, 0)))
orig = {o.name: o.matrix_world.copy() for o in (a1, a2, b1, b2)}
select_only([a1, a2, b1, b2], b1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
table = linkmod.read_table(cont)
gid_a = next(int(g) for g, info in table["groups"].items() if info["data_name"] == "MeshA")
select_only([cont], cont)
check("partial extract FINISHED",
      bpy.ops.agr.link_extract_group(group_id=gid_a) == {'FINISHED'})
check("partial: 3 objects now", len(bpy.data.objects) == 3)
check("partial: container kept name", bpy.data.objects.get("B1") is not None)
cont = bpy.data.objects["B1"]
table = linkmod.read_table(cont)
check("partial: container keeps 2 instances", len(table["instances"]) == 2)
check("partial: container faces = 12", len(cont.data.polygons) == 12)
restored = {o.name: o for o in bpy.data.objects if o != cont}
check("partial: A restored + linked",
      set(restored) == {"A1", "A2"} and restored["A1"].data == restored["A2"].data)
check("partial: A1 matrix", mat_close(restored["A1"].matrix_world, orig["A1"]))
select_only([cont], cont)
bpy.ops.agr.link_separate_all()
final = {o.name: o for o in bpy.data.objects}
check("partial→full: all 4 back", set(final) == {"A1", "A2", "B1", "B2"})
check("partial→full: B linked", final["B1"].data == final["B2"].data)
check("partial→full: B2 matrix", mat_close(final["B2"].matrix_world, orig["B2"]))

# ---------------------------------------------------------------------------
print("\n=== 8. Modifiers block the join ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
a2.modifiers.new("Sub", 'SUBSURF')
select_only([a1, a2], a1)
check("modifiers: join CANCELLED", expect_cancel(lambda: bpy.ops.agr.link_join()))
check("modifiers: nothing joined", len(bpy.data.objects) == 2)
check("modifiers: still linked", a1.data == a2.data and a1.data.users == 2)
check("modifiers: no table written", a1.get(KEY) is None)

# ---------------------------------------------------------------------------
print("\n=== 9. Custom props survive the roundtrip ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
a2["agr_atlas_applied"] = 1
a2["my_note"] = "hello"
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
select_only([cont], cont)
bpy.ops.agr.link_separate_all()
restored = {o.name: o for o in bpy.data.objects}
check("props: int restored", restored["A2"].get("agr_atlas_applied") == 1)
check("props: str restored", restored["A2"].get("my_note") == "hello")
check("props: A1 untouched", restored["A1"].get("my_note") is None)

# ---------------------------------------------------------------------------
print("\n=== 10. Re-attach to a datablock still alive in the file ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
a3 = add_obj("A3", mesh_a, TRS((6, 0, 0)))  # stays in the scene
select_only([a1, a2], a1)  # join only two of three copies
bpy.ops.agr.link_join()
cont = next(o for o in bpy.data.objects if o.get(KEY))
check("alive: A3 untouched", a3.data == mesh_a and len(mesh_a.polygons) == 6)
select_only([cont], cont)
bpy.ops.agr.link_separate_all()
restored = {o.name: o for o in bpy.data.objects}
check("alive: 3 objects", len(restored) == 3)
check("alive: restored share A3's mesh",
      restored["A1"].data == restored["A3"].data == restored["A2"].data)
check("alive: no stray attribute", restored["A3"].data.attributes.get(ATTR) is None)

# ---------------------------------------------------------------------------
print("\n=== 11. Collections + parent restored ===")
reset_scene()
coll = bpy.data.collections.new("Lowpoly")
bpy.context.scene.collection.children.link(coll)
root = bpy.data.objects.new("Root", None)
bpy.context.scene.collection.objects.link(root)
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)), coll=coll)
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
a2.parent = root
a2.matrix_world = TRS((3, 0, 0))
orig_a2 = a2.matrix_world.copy()
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
cont = next(o for o in bpy.data.objects if o.get(KEY))
select_only([cont], cont)
bpy.ops.agr.link_separate_all()
restored = {o.name: o for o in bpy.data.objects if o.type == 'MESH'}
check("coll: A1 back in Lowpoly", coll in restored["A1"].users_collection)
check("coll: A2 in scene root",
      bpy.context.scene.collection in restored["A2"].users_collection)
check("parent: A2 parented to Root", restored["A2"].parent == root)
check("parent: A2 world matrix kept", mat_close(restored["A2"].matrix_world, orig_a2))

# ---------------------------------------------------------------------------
print("\n=== 12. Error paths ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
select_only([a1], a1)
check("one object: join CANCELLED", expect_cancel(lambda: bpy.ops.agr.link_join()))
check("plain object: separate poll False", not bpy.ops.agr.link_separate_all.poll())
check("plain object: extract poll False", not bpy.ops.agr.link_extract_group.poll())

# ---------------------------------------------------------------------------
print("\n=== 13. Metadata survives .blend save/reload ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0), rot=(0, 0, 30)))
orig = {o.name: o.matrix_world.copy() for o in (a1, a2)}
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
blend_path = os.path.join(bpy.app.tempdir, "agr_link_roundtrip.blend")
bpy.ops.wm.save_as_mainfile(filepath=blend_path)
bpy.ops.wm.open_mainfile(filepath=blend_path)
cont = next((o for o in bpy.data.objects if o.get(KEY)), None)
check("reload: container found", cont is not None)
check("reload: attribute survived", cont.data.attributes.get(ATTR) is not None)
select_only([cont], cont)
check("reload: separate FINISHED", bpy.ops.agr.link_separate_all() == {'FINISHED'})
restored = {o.name: o for o in bpy.data.objects}
check("reload: both restored", set(restored) == {"A1", "A2"})
check("reload: linked again", restored["A1"].data == restored["A2"].data)
check("reload: A2 matrix", mat_close(restored["A2"].matrix_world, orig["A2"]))

# ---------------------------------------------------------------------------
print("\n=== 14. Parent+child joined together: order-independent restore ===")
reset_scene()
par = add_obj("Par", make_cube_mesh("MeshP"), TRS((5, 5, 0), rot=(0, 0, 30)))
chi = add_obj("Chi", make_cube_mesh("MeshC"))
chi.parent = par
chi.matrix_world = TRS((8, 5, 0))
orig = {o.name: o.matrix_world.copy() for o in (par, chi)}
select_only([par, chi], chi)  # CHILD active => child gets the LOWER instance id
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
select_only([cont], cont)
bpy.ops.agr.link_separate_all()
restored = {o.name: o for o in bpy.data.objects}
check("pc: parent matrix", mat_close(restored["Par"].matrix_world, orig["Par"]))
check("pc: child matrix (child was active)",
      mat_close(restored["Chi"].matrix_world, orig["Chi"]))
check("pc: parenting restored", restored["Chi"].parent == restored["Par"])

print("--- 14b. 3-level chain, MIDDLE object active ---")
reset_scene()
gp = add_obj("GP", make_cube_mesh("MeshGP"), T(2, 0, 0))
p = add_obj("P", make_cube_mesh("MeshPP"))
p.parent = gp
p.matrix_world = TRS((5, 5, 0), rot=(0, 0, 30))
c = add_obj("C", make_cube_mesh("MeshCC"))
c.parent = p
c.matrix_world = T(11, 5, 0)
orig = {o.name: o.matrix_world.copy() for o in (gp, p, c)}
select_only([gp, p, c], p)  # middle of the chain active
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
select_only([cont], cont)
bpy.ops.agr.link_separate_all()
restored = {o.name: o for o in bpy.data.objects}
for name in ("GP", "P", "C"):
    check(f"chain: {name} matrix", mat_close(restored[name].matrix_world, orig[name]))
check("chain: C->P->GP parents",
      restored["C"].parent == restored["P"] and restored["P"].parent == restored["GP"])

# ---------------------------------------------------------------------------
print("\n=== 15. City-scale offsets: re-link survives float32 noise ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0), rot=(0, 0, 7)))
a2 = add_obj("A2", mesh_a, TRS((300, 120, 1), rot=(0, 0, 33)))
a3 = add_obj("A3", mesh_a, TRS((520, -210, 2), rot=(0, 15, 90)))
orig = {o.name: o.matrix_world.copy() for o in (a1, a2, a3)}
select_only([a1, a2, a3], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
select_only([cont], cont)
bpy.ops.agr.link_separate_all()
restored = {o.name: o for o in bpy.data.objects}
check("far: all three linked",
      len({restored[n].data for n in ("A1", "A2", "A3")}) == 1)
check("far: no false 'edited' warning",
      bpy.context.window_manager.agr_last_status_level == 'INFO')
for name in ("A1", "A2", "A3"):
    check(f"far: {name} matrix", mat_close(restored[name].matrix_world, orig[name], tol=1e-3))
check("far: coords sane", local_coords_match(restored["A1"], tol=2e-3))

# ---------------------------------------------------------------------------
print("\n=== 16. Alt+D duplicate of the container survives disassembly ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
dup = cont.copy()  # Alt+D: object copy, mesh shared, idprops copied
bpy.context.scene.collection.objects.link(dup)
check("dup: shares container mesh", dup.data == cont.data)
select_only([cont], cont)
bpy.ops.agr.link_separate_all()
check("dup: geometry intact", len(dup.data.polygons) == 12)
dup_table = linkmod.read_table(dup)
check("dup: still a container", dup_table is not None and len(dup_table["instances"]) == 2)
restored = {o.name: o for o in bpy.data.objects if o != dup}
check("dup: originals restored linked",
      set(restored) == {"A1", "A2"} and restored["A1"].data == restored["A2"].data)
select_only([dup], dup)
check("dup: its own separate works", bpy.ops.agr.link_separate_all() == {'FINISHED'})
mesh_objs = [o for o in bpy.data.objects if o.type == 'MESH']
check("dup: 4 objects, one shared mesh",
      len(mesh_objs) == 4 and len({o.data for o in mesh_objs}) == 1)

# ---------------------------------------------------------------------------
print("\n=== 17. Pre-existing loose verts survive in the leftover husk ===")
reset_scene()
mesh_l = bpy.data.meshes.new("MeshL")
mesh_l.from_pydata(CUBE_VERTS + [(3, 3, 3), (4, 4, 4), (5, 5, 5)], [], CUBE_FACES)
mesh_l.validate()
l1 = add_obj("L1", mesh_l)
b1 = add_obj("B1", make_cube_mesh("MeshB"), T(3, 0, 0))
select_only([l1, b1], l1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
check("loose: container carries them", len(cont.data.vertices) == 19)
select_only([cont], cont)
bpy.ops.agr.link_separate_all()
names = {o.name for o in bpy.data.objects}
check("loose: husk kept", names == {"L1", "B1", "L1_leftover"}, str(names))
husk = bpy.data.objects.get("L1_leftover")
check("loose: husk holds the 3 verts", husk is not None and len(husk.data.vertices) == 3)
check("loose: husk is not a container", husk is not None and husk.get(KEY) is None)
total = sum(len(o.data.vertices) for o in bpy.data.objects)
check("loose: no verts lost", total == 19, f"total={total}")

# ---------------------------------------------------------------------------
print("\n=== 18. Zero-scale participants are blocked cleanly ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0), scale=(1, 1, 0)))
select_only([a1, a2], a1)
check("zero-scale participant: CANCELLED", expect_cancel(lambda: bpy.ops.agr.link_join()))
check("zero-scale: nothing mutated", a1.data == a2.data and a1.data.users == 2)
select_only([a1, a2], a2)  # zero-scaled object as the ACTIVE one
check("zero-scale active: CANCELLED", expect_cancel(lambda: bpy.ops.agr.link_join()))
check("zero-scale: still intact", len(bpy.data.objects) == 2 and a1.get(KEY) is None)

# ---------------------------------------------------------------------------
print("\n=== 19. Incremental join into an existing container ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
a3 = add_obj("A3", mesh_a, TRS((6, 0, 0), rot=(0, 0, 45)))
orig = {o.name: o.matrix_world.copy() for o in (a1, a2, a3)}
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects.get("A1")
select_only([cont, a3], cont)
check("incr: second join FINISHED", bpy.ops.agr.link_join() == {'FINISHED'})
table = linkmod.read_table(cont)
check("incr: 3 instances, 1 group",
      len(table["instances"]) == 3 and len(table["groups"]) == 1)
select_only([cont], cont)
bpy.ops.agr.link_separate_all()
restored = {o.name: o for o in bpy.data.objects}
check("incr: all restored linked",
      set(restored) == {"A1", "A2", "A3"}
      and len({restored[n].data for n in restored}) == 1)
check("incr: A3 matrix", mat_close(restored["A3"].matrix_world, orig["A3"]))

# ---------------------------------------------------------------------------
print("\n=== 20. Foreign plain Ctrl+J of two containers -> honest warning ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
mesh_b = make_cube_mesh("MeshB")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
b1 = add_obj("B1", mesh_b, TRS((0, 5, 0)))
b2 = add_obj("B2", mesh_b, TRS((3, 5, 0)))
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
c_a = bpy.data.objects.get("A1")
select_only([b1, b2], b1)
bpy.ops.agr.link_join()
c_b = bpy.data.objects.get("B1")
select_only([c_a, c_b], c_a)
bpy.ops.object.join()  # PLAIN join outside the addon - ids collide
select_only([c_a], c_a)
check("foreign: separate still FINISHED", bpy.ops.agr.link_separate_all() == {'FINISHED'})
wm = bpy.context.window_manager
check("foreign: warning fired", wm.agr_last_status_level == 'WARNING')
check("foreign: face-count mismatch named", "число граней" in wm.agr_last_status,
      wm.agr_last_status)

# ---------------------------------------------------------------------------
print("\n=== 21. Renamed material: re-link still works, no phantom slot ===")
reset_scene()
mat_red = bpy.data.materials.new("M_Red")
mesh_a = make_cube_mesh("MeshA")
mesh_a.materials.append(mat_red)
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
mat_red.name = "M_Red_v2"  # rename AFTER the join
cont = bpy.data.objects[0]
select_only([cont], cont)
bpy.ops.agr.link_separate_all()
restored = {o.name: o for o in bpy.data.objects}
check("rename-mat: linked again", restored["A1"].data == restored["A2"].data)
slot_names = [m.name if m else "" for m in restored["A1"].data.materials]
check("rename-mat: single clean slot", slot_names == ["M_Red_v2"], str(slot_names))
check("rename-mat: faces on slot 0",
      all(p.material_index == 0 for p in restored["A1"].data.polygons))

# ---------------------------------------------------------------------------
print("\n=== 22. Missing-faces instance keeps its table entry; rename reported ===")
reset_scene()
import bmesh as _bmesh
mesh_a = make_cube_mesh("MeshA")
mesh_b = make_cube_mesh("MeshB")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
b1 = add_obj("B1", mesh_b, TRS((0, 5, 0)))
select_only([a1, a2, b1], a1)  # container will be named "A1"
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
table = linkmod.read_table(cont)
a2_id = next(int(i) for i, inst in table["instances"].items() if inst["name"] == "A2")
gid_a = next(int(i) for i, inst in table["instances"].items()
             if inst["name"] == "A1")
gid_a = table["instances"][str(gid_a)]["group"]
bm = _bmesh.new()
bm.from_mesh(cont.data)
layer = bm.faces.layers.int.get(ATTR)
doomed = [f for f in bm.faces if f[layer] == a2_id]
_bmesh.ops.delete(bm, geom=doomed, context='FACES')
bm.to_mesh(cont.data)
bm.free()
select_only([cont], cont)
check("missing: extract group FINISHED",
      bpy.ops.agr.link_extract_group(group_id=gid_a) == {'FINISHED'})
cont2 = next((o for o in bpy.data.objects if o.get(KEY)), None)
check("missing: container survives", cont2 is not None)
table2 = linkmod.read_table(cont2)
names_left = {inst["name"] for inst in table2["instances"].values()}
check("missing: A2 entry preserved", "A2" in names_left, str(names_left))
check("missing: container renamed honestly", cont2.name == "A1.001", cont2.name)
check("missing: rename in warning", "переименован" in bpy.context.window_manager.agr_last_status)
check("missing: A1 restored with clean name", bpy.data.objects.get("A1") is not None)

# ---------------------------------------------------------------------------
print("\n=== 23. Apply All Transforms on the container ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((4, 0, 0), rot=(0, 0, 30)))
a3 = add_obj("A3", mesh_a, TRS((8, 1, 0), scale=(1, 2, 1)))
orig = {o.name: o.matrix_world.copy() for o in (a1, a2, a3)}
select_only([a1, a2, a3], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
move = T(10, -3, 2) @ TRS((0, 0, 0), rot=(0, 0, 25))
cont.matrix_world = move @ cont.matrix_world
select_only([cont], cont)
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
select_only([cont], cont)
bpy.ops.agr.link_separate_all()
restored = {o.name: o for o in bpy.data.objects}
for name in ("A1", "A2", "A3"):
    check(f"apply: {name} origin restored",
          mat_close(restored[name].matrix_world, move @ orig[name], tol=1e-3))
check("apply: linked", len({restored[n].data for n in restored}) == 1)
check("apply: no false warning",
      bpy.context.window_manager.agr_last_status_level == 'INFO')

# ---------------------------------------------------------------------------
print("\n=== 24. Set Origin on the container ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((4, 0, 0), rot=(0, 0, 30)))
orig = {o.name: o.matrix_world.copy() for o in (a1, a2)}
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
bpy.context.scene.cursor.location = (5, 5, 5)
select_only([cont], cont)
bpy.ops.object.origin_set(type='ORIGIN_CURSOR')
select_only([cont], cont)
bpy.ops.agr.link_separate_all()
restored = {o.name: o for o in bpy.data.objects}
for name in ("A1", "A2"):
    check(f"origin-set: {name} origin restored",
          mat_close(restored[name].matrix_world, orig[name], tol=1e-3))
check("origin-set: linked", restored["A1"].data == restored["A2"].data)

# ---------------------------------------------------------------------------
print("\n=== 25. Edit-mode move of a whole piece: origin follows, link kept ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((4, 0, 0), rot=(0, 0, 30)))
a3 = add_obj("A3", mesh_a, TRS((8, 0, 0)))
orig = {o.name: o.matrix_world.copy() for o in (a1, a2, a3)}
select_only([a1, a2, a3], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
table = linkmod.read_table(cont)
a2_id = next(int(i) for i, inst in table["instances"].items() if inst["name"] == "A2")
ids = linkmod._read_face_ids(cont.data)
move_verts = set()
for poly, iid in zip(cont.data.polygons, ids):
    if iid == a2_id:
        move_verts.update(poly.vertices)
for vi in move_verts:
    cont.data.vertices[vi].co.x += 3.0
select_only([cont], cont)
bpy.ops.agr.link_separate_all()
restored = {o.name: o for o in bpy.data.objects}
check("piece-move: origin follows the piece",
      mat_close(restored["A2"].matrix_world, T(3, 0, 0) @ orig["A2"], tol=1e-3))
check("piece-move: still linked (all 3)",
      len({restored[n].data for n in ("A1", "A2", "A3")}) == 1)
check("piece-move: others in place",
      mat_close(restored["A1"].matrix_world, orig["A1"])
      and mat_close(restored["A3"].matrix_world, orig["A3"]))

# ---------------------------------------------------------------------------
print("\n=== 26. Legacy container (no coord attributes) falls back ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((4, 0, 0), rot=(0, 0, 30)))
orig = {o.name: o.matrix_world.copy() for o in (a1, a2)}
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
for name in (linkmod.CO_ATTR, linkmod.ORIG_ATTR):
    attr = cont.data.attributes.get(name)
    if attr is not None:
        cont.data.attributes.remove(attr)
select_only([cont], cont)
bpy.ops.agr.link_separate_all()
restored = {o.name: o for o in bpy.data.objects}
check("legacy: matrices via fallback",
      all(mat_close(restored[n].matrix_world, orig[n]) for n in ("A1", "A2")))
check("legacy: linked", restored["A1"].data == restored["A2"].data)

# ---------------------------------------------------------------------------
print("\n=== 27. FBX round-trip through the STANDARD exporter ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0), rot=(0, 0, 10)))
a2 = add_obj("A2", mesh_a, TRS((4, 0, 0), rot=(0, 0, 30), scale=(1, 2, 1)))
a3 = add_obj("A3", mesh_a, TRS((8, 1, 2), rot=(15, 0, 90)))
orig = {o.name: o.matrix_world.copy() for o in (a1, a2, a3)}
select_only([a1, a2, a3], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
check("fbx: color mirror present after join",
      cont.data.attributes.get(linkmod.COL_CO) is not None
      and cont.data.attributes.get(linkmod.COL_ID) is not None)
check("fbx: no UV channels used", len(cont.data.uv_layers) == 0)
fbx_path = os.path.join(bpy.app.tempdir, "agr_link_rt.fbx")
select_only([cont], cont)
bpy.ops.export_scene.fbx(filepath=fbx_path, use_selection=True, use_custom_props=True)
reset_scene()
bpy.ops.import_scene.fbx(filepath=fbx_path, use_custom_props=True)
cont2 = next((o for o in bpy.data.objects if o.get(KEY)), None)
check("fbx: container recognised after import", cont2 is not None)
check("fbx: color mirror survived FBX",
      cont2 is not None and cont2.data.attributes.get(linkmod.COL_CO) is not None)
select_only([cont2], cont2)
check("fbx: separate FINISHED", bpy.ops.agr.link_separate_all() == {'FINISHED'})
restored = {o.name: o for o in bpy.data.objects if o.type == 'MESH'}
check("fbx: all three restored", set(restored) == {"A1", "A2", "A3"}, str(set(restored)))
for name in ("A1", "A2", "A3"):
    check(f"fbx: {name} matrix restored",
          name in restored and mat_close(restored[name].matrix_world, orig[name], tol=1e-3))
check("fbx: linked again",
      len({restored[n].data for n in restored}) == 1 if len(restored) == 3 else False)
check("fbx: restored objects carry no service data",
      all(restored[n].data.attributes.get(linkmod.COL_CO) is None
          and restored[n].data.attributes.get(linkmod.ATTR_NAME) is None
          and restored[n].get(KEY) is None for n in restored))

# ---------------------------------------------------------------------------
print("\n=== 28. Strip memory for clean delivery ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
select_only([cont], cont)
check("strip: FINISHED", bpy.ops.agr.link_strip() == {'FINISHED'})
check("strip: table gone", cont.get(KEY) is None)
check("strip: attributes gone",
      all(cont.data.attributes.get(n) is None
          for n in (linkmod.ATTR_NAME, linkmod.CO_ATTR, linkmod.ORIG_ATTR,
                    linkmod.COL_CO, linkmod.COL_ID)))
check("strip: separate no longer possible", not bpy.ops.agr.link_separate_all.poll())

# ---------------------------------------------------------------------------
print("\n=== 29. FBX with DEFAULT settings (no Custom Properties needed) ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0), rot=(0, 0, 10)))
a2 = add_obj("A2", mesh_a, TRS((4, 0, 0), rot=(0, 0, 30)))
a3 = add_obj("A3", mesh_a, TRS((8, 1, 0), scale=(1, 2, 1)))
u1 = add_obj("U1", make_cube_mesh("MeshU"), TRS((0, 5, 0)))
orig = {o.name: o.matrix_world.copy() for o in (a1, a2, a3, u1)}
select_only([a1, a2, a3, u1], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
check("defaults: table encoded in colors",
      cont.data.attributes.get(linkmod.TABLE_COL_PREFIX + "0") is not None)
fbx2 = os.path.join(bpy.app.tempdir, "agr_link_defaults.fbx")
select_only([cont], cont)
bpy.ops.export_scene.fbx(filepath=fbx2, use_selection=True)  # NO custom props
reset_scene()
bpy.ops.import_scene.fbx(filepath=fbx2)  # plain defaults
cont2 = next((o for o in bpy.data.objects
              if o.type == 'MESH' and linkmod.is_container(o)), None)
check("defaults: container recognised via colors only", cont2 is not None)
check("defaults: no idprop after import", cont2 is not None and cont2.get(KEY) is None)
table2 = linkmod.read_table(cont2)
check("defaults: table decoded", table2 is not None and len(table2["instances"]) == 4)
gid_a = next(int(g) for g, info in table2["groups"].items()
             if info["data_name"] == "MeshA")
select_only([cont2], cont2)
check("defaults: partial extract works",
      bpy.ops.agr.link_extract_group(group_id=gid_a) == {'FINISHED'})
cont3 = next((o for o in bpy.data.objects
              if o.type == 'MESH' and linkmod.read_table(o) is not None
              and len(linkmod.read_table(o)["instances"]) == 1), None)
check("defaults: container survives with U1", cont3 is not None)
check("defaults: idprop materialised on touch",
      cont3 is not None and isinstance(cont3.get(KEY), str))
restored = {o.name: o for o in bpy.data.objects
            if o.name.startswith("A") and linkmod.read_table(o) is None}
check("defaults: group A restored linked",
      set(restored) == {"A1", "A2", "A3"}
      and len({restored[n].data for n in restored}) == 1)
for name in ("A1", "A2", "A3"):
    check(f"defaults: {name} matrix", mat_close(restored[name].matrix_world, orig[name], tol=1e-3))
select_only([cont3], cont3)
bpy.ops.agr.link_separate_all()
u_restored = bpy.data.objects.get("U1")
check("defaults: U1 restored",
      u_restored is not None and mat_close(u_restored.matrix_world, orig["U1"], tol=1e-3))
check("defaults: coords BIT-EXACT after FBX",
      all(local_coords_match(restored[n], tol=1e-7) for n in ("A1", "A2", "A3"))
      and local_coords_match(u_restored, tol=1e-7))

# ---------------------------------------------------------------------------
print("\n=== 30. Join a freshly imported container without disassembly ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((4, 0, 0), rot=(0, 0, 30)))
orig = {o.name: o.matrix_world.copy() for o in (a1, a2)}
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
fbx3 = os.path.join(bpy.app.tempdir, "agr_link_joinback.fbx")
select_only([cont], cont)
bpy.ops.export_scene.fbx(filepath=fbx3, use_selection=True)
reset_scene()
bpy.ops.import_scene.fbx(filepath=fbx3)
cont2 = next((o for o in bpy.data.objects
              if o.type == 'MESH' and linkmod.is_container(o)), None)
check("joinback: container recognised", cont2 is not None)
b1 = add_obj("B1", make_cube_mesh("MeshB"), TRS((0, 5, 0)))
orig["B1"] = b1.matrix_world.copy()
select_only([cont2, b1], cont2)
check("joinback: join FINISHED", bpy.ops.agr.link_join() == {'FINISHED'})
cont3 = next(o for o in bpy.data.objects if linkmod.is_container(o))
table3 = linkmod.read_table(cont3)
check("joinback: 3 instances tracked", len(table3["instances"]) == 3)
select_only([cont3], cont3)
check("joinback: separate FINISHED", bpy.ops.agr.link_separate_all() == {'FINISHED'})
restored = {o.name: o for o in bpy.data.objects if o.type == 'MESH'}
check("joinback: all restored", set(restored) == {"A1", "A2", "B1"}, str(set(restored)))
check("joinback: A linked", restored["A1"].data == restored["A2"].data)
for name in ("A1", "A2", "B1"):
    check(f"joinback: {name} matrix",
          mat_close(restored[name].matrix_world, orig[name], tol=1e-3))

# ---------------------------------------------------------------------------
print("\n=== 31. Material replaced on container: chunks take CONTAINER material ===")
reset_scene()
mat_old = bpy.data.materials.new("M_Old")
mat_udim = bpy.data.materials.new("M_UDIM")
mesh_a = make_cube_mesh("MeshA")
mesh_a.materials.append(mat_old)
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
a3 = add_obj("A3", mesh_a, TRS((6, 0, 0)))  # stays alive with M_Old
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
cont = next(o for o in bpy.data.objects if o.get(KEY))
cont.data.materials[0] = mat_udim  # the UDIM swap on the container
select_only([cont], cont)
bpy.ops.agr.link_separate_all()
restored = {o.name: o for o in bpy.data.objects}
check("udim: A1+A2 linked to each other", restored["A1"].data == restored["A2"].data)
check("udim: kept separate from old-material copy", restored["A1"].data != a3.data)
check("udim: container material won",
      [m.name for m in restored["A1"].data.materials] == ["M_UDIM"])
check("udim: no false 'edited' warning",
      bpy.context.window_manager.agr_last_status_level == 'INFO')

# ---------------------------------------------------------------------------
print("\n=== 32. UV re-unwrapped on container: container UV wins, link kept ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
mesh_a.uv_layers.new(name="UVMap", do_init=False)
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
cont = next(o for o in bpy.data.objects if o.get(KEY))
for d in cont.data.uv_layers[0].data:  # "re-unwrap": shift all UVs
    d.uv.x += 0.25
select_only([cont], cont)
bpy.ops.agr.link_separate_all()
restored = {o.name: o for o in bpy.data.objects}
check("uv: linked again", restored["A1"].data == restored["A2"].data)
check("uv: single UV layer", len(restored["A1"].data.uv_layers) == 1)
check("uv: container unwrap won",
      abs(restored["A1"].data.uv_layers[0].data[0].uv.x - 0.25) < 1e-6)

print("--- 32b. Mismatched UV layer names warn at join ---")
reset_scene()
m1 = make_cube_mesh("MeshC1")
m1.uv_layers.new(name="UVMap", do_init=False)
m2 = make_cube_mesh("MeshC2")
m2.uv_layers.new(name="UVChannel_1", do_init=False)
c1 = add_obj("C1", m1, TRS((0, 0, 0)))
c2 = add_obj("C2", m2, TRS((3, 0, 0)))
select_only([c1, c2], c1)
bpy.ops.agr.link_join()
cont_m = next(o for o in bpy.data.objects if o.get(KEY))
check("uv-mismatch: join unions the layers (the warned-about hazard)",
      len(cont_m.data.uv_layers) == 2)

# ---------------------------------------------------------------------------
print("\n=== 33. Strip on FBX-imported container + foreign T-name safety ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
fbx4 = os.path.join(bpy.app.tempdir, "agr_link_strip.fbx")
select_only([cont], cont)
bpy.ops.export_scene.fbx(filepath=fbx4, use_selection=True)
reset_scene()
bpy.ops.import_scene.fbx(filepath=fbx4)
cont2 = next((o for o in bpy.data.objects
              if o.type == 'MESH' and linkmod.is_container(o)), None)
check("strip-fbx: container recognised", cont2 is not None)
cont2.data.color_attributes.new(name="AGR_Link_T0.001", type='FLOAT_COLOR', domain='CORNER')
check("strip-fbx: foreign T-name does not break recognition",
      linkmod.is_container(cont2))
select_only([cont2], cont2)
check("strip-fbx: strip FINISHED (no idprop present)",
      bpy.ops.agr.link_strip() == {'FINISHED'})
check("strip-fbx: no longer a container", not linkmod.is_container(cont2))

# ---------------------------------------------------------------------------
print("\n=== 34. Parent is still inside the container during partial extract ===")
reset_scene()
par = add_obj("Par", make_cube_mesh("MeshP"), TRS((5, 5, 0), rot=(0, 0, 30)))
chi = add_obj("Chi", make_cube_mesh("MeshC"))
chi.parent = par
chi.matrix_world = TRS((8, 5, 0))
orig = {o.name: o.matrix_world.copy() for o in (par, chi)}
select_only([par, chi], par)  # container keeps the PARENT's name
bpy.ops.agr.link_join()
cont = next(o for o in bpy.data.objects if o.get(KEY))
table = linkmod.read_table(cont)
gid_chi = next(inst["group"] for inst in table["instances"].values()
               if inst["name"] == "Chi")
select_only([cont], cont)
bpy.ops.agr.link_extract_group(group_id=gid_chi)
chi_r = bpy.data.objects.get("Chi")
cont = next(o for o in bpy.data.objects if o.get(KEY))
check("pwin: Chi parented to surviving container", chi_r is not None and chi_r.parent == cont)
check("pwin: Chi world matrix correct", mat_close(chi_r.matrix_world, orig["Chi"]))
select_only([cont], cont)
bpy.ops.agr.link_separate_all()
par_r = bpy.data.objects.get("Par")
check("pwin: parent handed over to restored Par",
      chi_r.parent == par_r and par_r is not None)
check("pwin: Chi world kept through handover", mat_close(chi_r.matrix_world, orig["Chi"]))
check("pwin: Par matrix restored", mat_close(par_r.matrix_world, orig["Par"]))

# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} FAILED:")
    for name in FAILS:
        print(f"   - {name}")
    sys.exit(1)
print("✅ ALL CHECKS PASSED")
