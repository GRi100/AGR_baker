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
print("\n=== 20. Plain Ctrl+J of two containers -> absorbed, full restore ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
mesh_b = make_cube_mesh("MeshB")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
b1 = add_obj("B1", mesh_b, TRS((0, 5, 0)))
b2 = add_obj("B2", mesh_b, TRS((3, 5, 0)))
orig20 = {o.name: o.matrix_world.copy() for o in (a1, a2, b1, b2)}
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
c_a = bpy.data.objects.get("A1")
select_only([b1, b2], b1)
bpy.ops.agr.link_join()
c_b = bpy.data.objects.get("B1")
select_only([c_a, c_b], c_a)
bpy.ops.object.join()  # PLAIN join outside the addon - absorbed via windows
tbl20, extra20 = linkmod._peek_merged(c_a)
check("foreign: peek sees the merged view", tbl20 is not None
      and len(tbl20["instances"]) == 4, str(extra20))
check("foreign: one foreign window reported", extra20 == 1)
select_only([c_a], c_a)
check("foreign: separate FINISHED", bpy.ops.agr.link_separate_all() == {'FINISHED'})
check("foreign: all 4 restored", {o.name for o in bpy.data.objects}
      == {"A1", "A2", "B1", "B2"}, str(sorted(o.name for o in bpy.data.objects)))
check("foreign: positions exact",
      all(mat_close(bpy.data.objects[n].matrix_world, orig20[n]) for n in orig20))
check("foreign: A group linked",
      bpy.data.objects["A1"].data == bpy.data.objects["A2"].data)
check("foreign: B group linked",
      bpy.data.objects["B1"].data == bpy.data.objects["B2"].data)
check("foreign: groups distinct",
      bpy.data.objects["A1"].data != bpy.data.objects["B1"].data)
check("foreign: local coords intact",
      all(local_coords_match(bpy.data.objects[n]) for n in orig20))

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
print("\n=== 35. Plain Ctrl+J: container INTO a plain object (plain active) ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0), rot=(0, 0, 45)))
d1 = add_obj("D1", make_cube_mesh("MeshD"), TRS((0, 5, 0), scale=(1, 2, 1)))
orig = {o.name: o.matrix_world.copy() for o in (a1, a2, d1)}
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects.get("A1")
select_only([cont, d1], d1)          # PLAIN object is the ACTIVE one
bpy.ops.object.join()                # the old idprop dies with the container
merged_obj = bpy.data.objects.get("D1")
check("absorb: idprop is gone", merged_obj.get(KEY) is None)
check("absorb: still a container (colors)", linkmod.is_container(merged_obj))
tbl, extra = linkmod._peek_merged(merged_obj)
check("absorb: view has 3 instances (A1, A2, D1)",
      tbl is not None and len(tbl["instances"]) == 3,
      str(len(tbl["instances"])) if tbl else "-")
check("absorb: D1 registered as an instance",
      tbl is not None and any(i["name"] == "D1" for i in tbl["instances"].values()))
select_only([merged_obj], merged_obj)
check("absorb: separate FINISHED", bpy.ops.agr.link_separate_all() == {'FINISHED'})
names = {o.name for o in bpy.data.objects}
check("absorb: all three restored (no _leftover)", names == {"A1", "A2", "D1"}, str(names))
check("absorb: positions exact",
      all(mat_close(bpy.data.objects[n].matrix_world, orig[n]) for n in orig))
check("absorb: A group linked again",
      bpy.data.objects["A1"].data == bpy.data.objects["A2"].data)
check("absorb: local coords intact",
      all(local_coords_match(bpy.data.objects[n]) for n in ("A1", "A2", "D1")))

# ---------------------------------------------------------------------------
print("\n=== 36. Plain Ctrl+J merges SHARED link groups (same datablock) ===")
reset_scene()
mesh_x = make_cube_mesh("MeshX")
x1 = add_obj("X1", mesh_x, TRS((0, 0, 0)))
x2 = add_obj("X2", mesh_x, TRS((3, 0, 0)))
x3 = add_obj("X3", mesh_x, TRS((0, 5, 0)))
x4 = add_obj("X4", mesh_x, TRS((3, 5, 0)))
orig = {o.name: o.matrix_world.copy() for o in (x1, x2, x3, x4)}
select_only([x1, x2], x1)
bpy.ops.agr.link_join()
c1 = bpy.data.objects.get("X1")
select_only([x3, x4], x3)
bpy.ops.agr.link_join()
c2 = bpy.data.objects.get("X3")
select_only([c1, c2], c1)
bpy.ops.object.join()
select_only([c1], c1)
check("groupmerge: separate FINISHED", bpy.ops.agr.link_separate_all() == {'FINISHED'})
restored = {o.name: o for o in bpy.data.objects}
check("groupmerge: all four restored", set(restored) == {"X1", "X2", "X3", "X4"})
check("groupmerge: ONE shared datablock for all four",
      len({restored[n].data for n in restored}) == 1)
check("groupmerge: positions exact",
      all(mat_close(restored[n].matrix_world, orig[n]) for n in orig))

# ---------------------------------------------------------------------------
print("\n=== 37. Chained plain Ctrl+J: three containers, three windows ===")
reset_scene()
conts = []
orig = {}
for tag, y in (("A", 0), ("B", 5), ("C", 10)):
    mesh_t = make_cube_mesh(f"Mesh{tag}")
    o1 = add_obj(f"{tag}1", mesh_t, TRS((0, y, 0)))
    o2 = add_obj(f"{tag}2", mesh_t, TRS((3, y, 0)))
    orig[o1.name] = o1.matrix_world.copy()
    orig[o2.name] = o2.matrix_world.copy()
    select_only([o1, o2], o1)
    bpy.ops.agr.link_join()
    conts.append(bpy.data.objects.get(f"{tag}1"))
select_only([conts[0], conts[1]], conts[0])
bpy.ops.object.join()                       # A absorbs B's window
select_only([conts[0], conts[2]], conts[0])
bpy.ops.object.join()                       # then C's window on top
tbl, extra = linkmod._peek_merged(conts[0])
check("chain: both foreign windows visible", extra == 2, str(extra))
check("chain: six instances in the view", tbl is not None and len(tbl["instances"]) == 6)
select_only([conts[0]], conts[0])
check("chain: separate FINISHED", bpy.ops.agr.link_separate_all() == {'FINISHED'})
restored = {o.name: o for o in bpy.data.objects}
check("chain: all six restored", set(restored) == set(orig), str(set(restored)))
check("chain: positions exact",
      all(mat_close(restored[n].matrix_world, orig[n]) for n in orig))
for tag in ("A", "B", "C"):
    check(f"chain: {tag} group linked",
          restored[f"{tag}1"].data == restored[f"{tag}2"].data)

# ---------------------------------------------------------------------------
print("\n=== 38. link_join right after a plain Ctrl+J absorbs the windows ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
mesh_b = make_cube_mesh("MeshB")
b1 = add_obj("B1", mesh_b, TRS((0, 5, 0)))
b2 = add_obj("B2", mesh_b, TRS((3, 5, 0)))
e1 = add_obj("E1", make_cube_mesh("MeshE"), TRS((0, 10, 0)))
orig = {o.name: o.matrix_world.copy() for o in (a1, a2, b1, b2, e1)}
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
c_a = bpy.data.objects.get("A1")
select_only([b1, b2], b1)
bpy.ops.agr.link_join()
c_b = bpy.data.objects.get("B1")
select_only([c_a, c_b], c_a)
bpy.ops.object.join()                       # plain join first
select_only([bpy.data.objects["A1"], e1], bpy.data.objects["A1"])
check("joinafter: link_join FINISHED", bpy.ops.agr.link_join() == {'FINISHED'})
cont = next(o for o in bpy.data.objects if linkmod.is_container(o))
table = linkmod.read_table(cont)
check("joinafter: five instances tracked", len(table["instances"]) == 5,
      str(len(table["instances"])))
check("joinafter: no stale matrices left",
      not any(i.get("matrix_stale") for i in table["instances"].values()))
select_only([cont], cont)
check("joinafter: separate FINISHED", bpy.ops.agr.link_separate_all() == {'FINISHED'})
restored = {o.name: o for o in bpy.data.objects}
check("joinafter: all five restored", set(restored) == set(orig), str(set(restored)))
check("joinafter: positions exact",
      all(mat_close(restored[n].matrix_world, orig[n]) for n in orig))
check("joinafter: A linked", restored["A1"].data == restored["A2"].data)
check("joinafter: B linked", restored["B1"].data == restored["B2"].data)

# ---------------------------------------------------------------------------
print("\n=== 39. FBX roundtrip AFTER a plain Ctrl+J (windows survive export) ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0), rot=(0, 0, 30)))
d1 = add_obj("D1", make_cube_mesh("MeshD"), TRS((0, 5, 0)))
orig = {o.name: o.matrix_world.copy() for o in (a1, a2, d1)}
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects.get("A1")
select_only([cont, d1], d1)
bpy.ops.object.join()                       # plain join, NO reconcile before export
merged_obj = bpy.data.objects.get("D1")
fbx5 = os.path.join(bpy.app.tempdir, "agr_link_absorb.fbx")
select_only([merged_obj], merged_obj)
bpy.ops.export_scene.fbx(filepath=fbx5, use_selection=True)
reset_scene()
bpy.ops.import_scene.fbx(filepath=fbx5)
cont2 = next((o for o in bpy.data.objects
              if o.type == 'MESH' and linkmod.is_container(o)), None)
check("absorb-fbx: container recognised after import", cont2 is not None)
tbl, _extra = linkmod._peek_merged(cont2)
check("absorb-fbx: three instances in the view",
      tbl is not None and len(tbl["instances"]) == 3)
select_only([cont2], cont2)
check("absorb-fbx: separate FINISHED", bpy.ops.agr.link_separate_all() == {'FINISHED'})
restored = {o.name: o for o in bpy.data.objects if o.type == 'MESH'}
check("absorb-fbx: all three restored", set(restored) == {"A1", "A2", "D1"},
      str(set(restored)))
check("absorb-fbx: positions restored",
      all(mat_close(restored[n].matrix_world, orig[n], tol=1e-3) for n in restored))
check("absorb-fbx: A group linked", restored["A1"].data == restored["A2"].data)
check("absorb-fbx: coords BIT-EXACT (per-window precise layer)",
      all(local_coords_match(restored[n], tol=1e-7) for n in ("A1", "A2", "D1")))

# ---------------------------------------------------------------------------
print("\n=== 40. Absorption safety: idempotence + Alt+D twin of the merged mesh ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
d1 = add_obj("D1", make_cube_mesh("MeshD"), TRS((0, 5, 0)))
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects.get("A1")
select_only([cont, d1], d1)
bpy.ops.object.join()
merged_obj = bpy.data.objects.get("D1")
# Alt+D twin BEFORE any AGR operator touches the merged mesh
twin = merged_obj.copy()                      # linked duplicate (shares mesh)
bpy.context.scene.collection.objects.link(twin)
twin_mesh = twin.data
st1 = linkmod._reconcile_container(bpy.context, merged_obj)
check("idem: first reconcile materialised", st1 is not None)
check("idem: reconcile made the mesh single-user (twin protected)",
      merged_obj.data is not twin_mesh and twin.data is twin_mesh)
st2 = linkmod._reconcile_container(bpy.context, merged_obj)
check("idem: second reconcile is a no-op", st2 is None)
tblm = linkmod.read_table(merged_obj)
check("idem: materialised table has 3 instances",
      isinstance(merged_obj.get(KEY), str) and tblm is not None
      and len(tblm["instances"]) == 3)
check("idem: twin still a container on its own",
      linkmod.is_container(twin))
select_only([merged_obj], merged_obj)
check("idem: separate FINISHED", bpy.ops.agr.link_separate_all() == {'FINISHED'})
check("idem: twin survived the disassembly",
      bpy.data.objects.get(twin.name) is not None
      and len(twin.data.vertices) == len(twin_mesh.vertices))

# ---------------------------------------------------------------------------
print("\n=== 41. Destroyed foreign window degrades honestly (edit after Ctrl+J) ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
mesh_b = make_cube_mesh("MeshB")
b1 = add_obj("B1", mesh_b, TRS((0, 5, 0)))
b2 = add_obj("B2", mesh_b, TRS((3, 5, 0)))
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
c_a = bpy.data.objects.get("A1")
select_only([b1, b2], b1)
bpy.ops.agr.link_join()
c_b = bpy.data.objects.get("B1")
select_only([c_a, c_b], c_a)
bpy.ops.object.join()
# delete one face inside B's block: loops shift, B's window CRC dies
import bmesh as _bmesh
me = c_a.data
bm = _bmesh.new()
bm.from_mesh(me)
bm.faces.ensure_lookup_table()
_bmesh.ops.delete(bm, geom=[bm.faces[-1]], context='FACES')
bm.to_mesh(me)
bm.free()
select_only([c_a], c_a)
check("degrade: separate still FINISHED", bpy.ops.agr.link_separate_all() == {'FINISHED'})
wm = bpy.context.window_manager
check("degrade: warning fired", wm.agr_last_status_level == 'WARNING',
      wm.agr_last_status)

# ---------------------------------------------------------------------------
print("\n=== 42. Plain tail AFTER a merged container (multi-layer window cut exactly) ===")
# The v1 framing lost B's table here: its blob spans several layers and the
# plain block after it gave no magic candidate, so the window overshot and
# failed CRC.  v2 frames carry the carrier loop count - the cut is exact.
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
mesh_b = make_cube_mesh("MeshB")
b1 = add_obj("B1", mesh_b, TRS((0, 5, 0)))
b2 = add_obj("B2", mesh_b, TRS((3, 5, 0), rot=(0, 0, 30)))
p1 = add_obj("P1", make_cube_mesh("MeshP"), TRS((0, 10, 0)))
orig = {o.name: o.matrix_world.copy() for o in (a1, a2, b1, b2, p1)}
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
c_a = bpy.data.objects.get("A1")
select_only([b1, b2], b1)
bpy.ops.agr.link_join()
c_b = bpy.data.objects.get("B1")
select_only([c_a, c_b, p1], c_a)  # ONE plain Ctrl+J: [A][B][P] block order
bpy.ops.object.join()
tbl42, extra42 = linkmod._peek_merged(c_a)
check("tail42: B's window survives the plain tail", extra42 == 1, str(extra42))
check("tail42: all four instances in the view",
      tbl42 is not None and len(tbl42["instances"]) == 4,
      str(len(tbl42["instances"])) if tbl42 else "-")
select_only([c_a], c_a)
check("tail42: separate FINISHED", bpy.ops.agr.link_separate_all() == {'FINISHED'})
names42 = {o.name for o in bpy.data.objects}
check("tail42: A and B fully restored",
      {"A1", "A2", "B1", "B2"} <= names42, str(sorted(names42)))
for n in ("A1", "A2", "B1", "B2"):
    o = bpy.data.objects.get(n)
    check(f"tail42: {n} matrix exact", o is not None and mat_close(o.matrix_world, orig[n]))
    check(f"tail42: {n} geometry clean (8 verts)",
          o is not None and len(o.data.vertices) == 8)
check("tail42: B group linked",
      bpy.data.objects["B1"].data == bpy.data.objects["B2"].data)
check("tail42: foreign P kept as leftover (idprop semantics)",
      any(n.endswith("_leftover") for n in names42), str(sorted(names42)))

# ---------------------------------------------------------------------------
print("\n=== 43. link_join on a SINGLE container = refresh memory ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
mesh_b = make_cube_mesh("MeshB")
b1 = add_obj("B1", mesh_b, TRS((0, 5, 0)))
b2 = add_obj("B2", mesh_b, TRS((3, 5, 0)))
orig = {o.name: o.matrix_world.copy() for o in (a1, a2, b1, b2)}
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
c_a = bpy.data.objects.get("A1")
select_only([b1, b2], b1)
bpy.ops.agr.link_join()
c_b = bpy.data.objects.get("B1")
select_only([c_a, c_b], c_a)
bpy.ops.object.join()                      # plain Ctrl+J: window not absorbed yet
select_only([c_a], c_a)
check("refresh: single-container join FINISHED",
      bpy.ops.agr.link_join() == {'FINISHED'})
tbl43 = linkmod._parse_table(c_a.get(KEY))
check("refresh: merged table materialised in the idprop",
      tbl43 is not None and len(tbl43["instances"]) == 4,
      str(len(tbl43["instances"])) if tbl43 else "-")
check("refresh: no foreign windows left",
      linkmod._merged_view(c_a)[1] == 0)
check("refresh: second click is a clean no-op",
      bpy.ops.agr.link_join() == {'FINISHED'}
      and len(linkmod.read_table(c_a)["instances"]) == 4)
select_only([c_a], c_a)
check("refresh: separate after refresh FINISHED",
      bpy.ops.agr.link_separate_all() == {'FINISHED'})
check("refresh: all restored at exact positions",
      all(bpy.data.objects.get(n) is not None
          and mat_close(bpy.data.objects[n].matrix_world, orig[n]) for n in orig))

print("--- 43b. single PLAIN mesh still refuses ---")
reset_scene()
lone = add_obj("Lone", make_cube_mesh("MeshL"))
select_only([lone], lone)
check("refresh: plain single mesh -> CANCELLED",
      expect_cancel(lambda: bpy.ops.agr.link_join()))

# ---------------------------------------------------------------------------
# Forced restore (agr.link_restore) helpers
# ---------------------------------------------------------------------------
import bmesh as _bmesh  # noqa: E402  (idempotent re-import, also done in section 22)


def purge_orphan_meshes():
    """Donor datablocks survive the join with users==0 and the alive-adoption
    branch happily picks them up (see test 1).  Restore scenarios MUST kill
    them, or the reference would come from the orphan and the majority vote
    would never be exercised.  A .blend save/reload does exactly this."""
    for me in list(bpy.data.meshes):
        if me.users == 0:
            bpy.data.meshes.remove(me)


def inst_id(cont, name):
    table = linkmod.read_table(cont)
    return next(int(i) for i, inst in table["instances"].items()
                if inst["name"] == name)


def gid_of(cont, data_name):
    table = linkmod.read_table(cont)
    return next(int(g) for g, info in table["groups"].items()
                if info["data_name"] == data_name)


def nudge_vert(cont, iid, dx=0.3):
    """Shift the first vertex of the instance's first face (a local edit)."""
    attr = cont.data.attributes[ATTR]
    for poly, pa in zip(cont.data.polygons, attr.data):
        if pa.value == iid:
            cont.data.vertices[poly.vertices[0]].co.x += dx
            return
    raise AssertionError(f"no faces with id {iid}")


def paint_faces(cont, iid, slot):
    attr = cont.data.attributes[ATTR]
    for poly, pa in zip(cont.data.polygons, attr.data):
        if pa.value == iid:
            poly.material_index = slot


def drop_faces(cont, iid, n):
    """bmesh-delete n faces of the instance (recipe from test 22)."""
    bm = _bmesh.new()
    bm.from_mesh(cont.data)
    layer = bm.faces.layers.int.get(ATTR)
    doomed = [f for f in bm.faces if f[layer] == iid][:n]
    _bmesh.ops.delete(bm, geom=doomed, context='FACES')
    bm.to_mesh(cont.data)
    bm.free()


def status():
    return bpy.context.window_manager.agr_last_status


def status_level():
    return bpy.context.window_manager.agr_last_status_level


# ---------------------------------------------------------------------------
print("\n=== 44. RESTORE SOFT: vertex shift discarded, link recovered ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0), rot=(0, 0, 15)))
a3 = add_obj("A3", mesh_a, TRS((6, 0, 0)))
orig = {o.name: o.matrix_world.copy() for o in (a1, a2, a3)}
select_only([a1, a2, a3], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
purge_orphan_meshes()
nudge_vert(cont, inst_id(cont, "A2"), 0.3)
select_only([cont], cont)
check("soft: restore ran", bpy.ops.agr.link_restore(mode='SOFT') == {'FINISHED'})
restored = {o.name: o for o in bpy.data.objects}
check("soft: 3 restored", len(restored) == 3, str(sorted(restored)))
check("soft: all linked",
      restored["A1"].data == restored["A2"].data == restored["A3"].data)
check("soft: A2 coords back to original", local_coords_match(restored["A2"]))
for n in ("A1", "A2", "A3"):
    check(f"soft: {n} matrix", mat_close(restored[n].matrix_world, orig[n], tol=1e-3))
check("soft: datablock keeps group name", restored["A1"].data.name == "MeshA")
check("soft: INFO level", status_level() == 'INFO', status())
check("soft: shifts reported", "сдвиги" in status(), status())

# ---------------------------------------------------------------------------
print("\n=== 45. RESTORE SOFT: repaint discarded (majority reference) ===")
reset_scene()
mat_red = bpy.data.materials.new("M_Red")
mat_blue = bpy.data.materials.new("M_Blue")
mesh_a = make_cube_mesh("MeshA")
mesh_a.materials.append(mat_red)
mesh_a.materials.append(mat_blue)  # slot exists, but all faces are red
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
a3 = add_obj("A3", mesh_a, TRS((6, 0, 0)))
select_only([a1, a2, a3], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
purge_orphan_meshes()
paint_faces(cont, inst_id(cont, "A2"), 1)  # repaint one copy blue
select_only([cont], cont)
check("repaint: restore ran", bpy.ops.agr.link_restore(mode='SOFT') == {'FINISHED'})
restored = {o.name: o for o in bpy.data.objects}
check("repaint: all linked",
      restored["A1"].data == restored["A2"].data == restored["A3"].data)
check("repaint: slot is majority red",
      [m.name for m in restored["A2"].data.materials] == ["M_Red"],
      str([m.name for m in restored["A2"].data.materials]))
check("repaint: faces on slot 0",
      all(p.material_index == 0 for p in restored["A2"].data.polygons))
check("repaint: reported", "перекраска" in status(), status())

print("--- 45b. Majority repaint WINS (2 of 3 painted blue) ---")
reset_scene()
mat_red = bpy.data.materials.new("M_Red")
mat_blue = bpy.data.materials.new("M_Blue")
mesh_a = make_cube_mesh("MeshA")
mesh_a.materials.append(mat_red)
mesh_a.materials.append(mat_blue)
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
a3 = add_obj("A3", mesh_a, TRS((6, 0, 0)))
select_only([a1, a2, a3], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
purge_orphan_meshes()
paint_faces(cont, inst_id(cont, "A2"), 1)
paint_faces(cont, inst_id(cont, "A3"), 1)
select_only([cont], cont)
check("majority: restore ran", bpy.ops.agr.link_restore(mode='SOFT') == {'FINISHED'})
restored = {o.name: o for o in bpy.data.objects}
check("majority: all linked",
      restored["A1"].data == restored["A2"].data == restored["A3"].data)
check("majority: blue wins (2 votes)",
      [m.name for m in restored["A1"].data.materials] == ["M_Blue"],
      str([m.name for m in restored["A1"].data.materials]))

# ---------------------------------------------------------------------------
print("\n=== 46. RESTORE SOFT does NOT touch broken topology ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
a3 = add_obj("A3", mesh_a, TRS((6, 0, 0)))
select_only([a1, a2, a3], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
purge_orphan_meshes()
drop_faces(cont, inst_id(cont, "A2"), 1)
select_only([cont], cont)
check("soft-topo: restore ran", bpy.ops.agr.link_restore(mode='SOFT') == {'FINISHED'})
restored = {o.name: o for o in bpy.data.objects}
check("soft-topo: A1+A3 linked", restored["A1"].data == restored["A3"].data)
check("soft-topo: A2 stays unique", restored["A2"].data != restored["A1"].data)
check("soft-topo: A2 keeps 5 faces", len(restored["A2"].data.polygons) == 5)
check("soft-topo: WARNING level", status_level() == 'WARNING', status())
check("soft-topo: hard-mode hint", "нужен жёсткий режим" in status(), status())

# ---------------------------------------------------------------------------
print("\n=== 47. RESTORE HARD: broken chunk rebuilt from the reference ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0), rot=(0, 0, 40)))
a3 = add_obj("A3", mesh_a, TRS((6, 0, 0)))
orig = {o.name: o.matrix_world.copy() for o in (a1, a2, a3)}
select_only([a1, a2, a3], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
purge_orphan_meshes()
a2_id = inst_id(cont, "A2")
drop_faces(cont, a2_id, 3)
nudge_vert(cont, a2_id, 0.2)
select_only([cont], cont)
check("hard: restore ran", bpy.ops.agr.link_restore(mode='HARD') == {'FINISHED'})
restored = {o.name: o for o in bpy.data.objects}
check("hard: 3 restored", len(restored) == 3, str(sorted(restored)))
check("hard: all linked",
      restored["A1"].data == restored["A2"].data == restored["A3"].data)
check("hard: full 6 faces back", len(restored["A2"].data.polygons) == 6)
check("hard: coords original", local_coords_match(restored["A2"]))
check("hard: A2 position by fit", mat_close(restored["A2"].matrix_world, orig["A2"], tol=1e-3))
check("hard: rebuilt reported", "перестроено по эталону" in status(), status())
check("hard: fit converged (no approx warning)",
      "позиция может быть неточной" not in status(), status())
check("hard: INFO level", status_level() == 'INFO', status())

# ---------------------------------------------------------------------------
print("\n=== 48. RESTORE HARD after a plain Ctrl+J of a foreign cube ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
a3 = add_obj("A3", mesh_a, TRS((6, 0, 0)))
select_only([a1, a2, a3], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
foreign = add_obj("Foreign", make_cube_mesh("MeshF"), T(0, -5, 0))
select_only([cont, foreign], cont)
bpy.ops.object.join()  # plain Ctrl+J: foreign faces get id 0
purge_orphan_meshes()
drop_faces(cont, inst_id(cont, "A2"), 2)
select_only([cont], cont)
check("hard-foreign: restore ran", bpy.ops.agr.link_restore(mode='HARD') == {'FINISHED'})
objs = {o.name: o for o in bpy.data.objects}
check("hard-foreign: A* restored linked",
      {"A1", "A2", "A3"} <= set(objs)
      and objs["A1"].data == objs["A2"].data == objs["A3"].data,
      str(sorted(objs)))
check("hard-foreign: A2 coords original", local_coords_match(objs["A2"]))
leftover = next((o for o in bpy.data.objects if o.name.endswith("_leftover")), None)
check("hard-foreign: foreign kept as leftover", leftover is not None)
check("hard-foreign: leftover holds the foreign 6 faces",
      leftover is not None and len(leftover.data.polygons) == 6)

print("--- 48b. duplicated face inherits id+orig: counts save the day ---")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
purge_orphan_meshes()
a2_id = inst_id(cont, "A2")
bm = _bmesh.new()
bm.from_mesh(cont.data)
layer = bm.faces.layers.int.get(ATTR)
src = next(f for f in bm.faces if f[layer] == a2_id)
res = _bmesh.ops.duplicate(bm, geom=[src])
new_verts = [g for g in res["geom"] if isinstance(g, _bmesh.types.BMVert)]
_bmesh.ops.translate(bm, verts=new_verts, vec=(0.0, 0.0, 0.7))
bm.to_mesh(cont.data)
bm.free()
select_only([cont], cont)
check("dup: restore ran", bpy.ops.agr.link_restore(mode='HARD') == {'FINISHED'})
restored = {o.name: o for o in bpy.data.objects}
check("dup: linked again", restored["A1"].data == restored["A2"].data)
check("dup: clean 6 faces", len(restored["A2"].data.polygons) == 6)
check("dup: position close",
      mat_close(restored["A2"].matrix_world, TRS((3, 0, 0)), tol=1e-2))

# ---------------------------------------------------------------------------
print("\n=== 49. RESTORE HARD with NO reference: honest give-up ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
purge_orphan_meshes()  # kills the orphan MeshA donors too
drop_faces(cont, inst_id(cont, "A1"), 1)
drop_faces(cont, inst_id(cont, "A2"), 2)
select_only([cont], cont)
check("noref: restore ran", bpy.ops.agr.link_restore(mode='HARD') == {'FINISHED'})
restored = {o.name: o for o in bpy.data.objects}
check("noref: both unique", restored["A1"].data != restored["A2"].data)
check("noref: edits preserved (nothing mangled)",
      len(restored["A1"].data.polygons) == 5
      and len(restored["A2"].data.polygons) == 4)
check("noref: WARNING level", status_level() == 'WARNING', status())
check("noref: no-reference reported", "групп без эталона" in status(), status())

# ---------------------------------------------------------------------------
print("\n=== 50. RESTORE HARD: reference from the alive scene datablock ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
a3 = add_obj("A3", mesh_a, TRS((6, 0, 0)))  # never joined: keeps MeshA alive
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
purge_orphan_meshes()  # A3 keeps MeshA alive (users == 1)
drop_faces(cont, inst_id(cont, "A1"), 1)
drop_faces(cont, inst_id(cont, "A2"), 2)
select_only([cont], cont)
check("aliveref: restore ran", bpy.ops.agr.link_restore(mode='HARD') == {'FINISHED'})
restored = {o.name: o for o in bpy.data.objects}
check("aliveref: all three linked on MeshA",
      restored["A1"].data == restored["A2"].data == restored["A3"].data == mesh_a)
check("aliveref: full cube back", len(restored["A1"].data.polygons) == 6)
check("aliveref: reported", "эталон взят из сцены" in status(), status())
check("aliveref: no tracking attr on the reference",
      mesh_a.attributes.get(ATTR) is None)

# ---------------------------------------------------------------------------
print("\n=== 51. RESTORE: faceless instance stays skipped (user decision) ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
a3 = add_obj("A3", mesh_a, TRS((6, 0, 0)))
select_only([a1, a2, a3], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
purge_orphan_meshes()
drop_faces(cont, inst_id(cont, "A2"), 6)  # ALL faces of A2 gone
select_only([cont], cont)
check("faceless: restore ran", bpy.ops.agr.link_restore(mode='HARD') == {'FINISHED'})
check("faceless: A2 not recreated", bpy.data.objects.get("A2") is None)
cont2 = next((o for o in bpy.data.objects if linkmod.read_table(o) is not None), None)
check("faceless: container survives with the record", cont2 is not None)
names_left = ({i["name"] for i in linkmod.read_table(cont2)["instances"].values()}
              if cont2 else set())
check("faceless: A2 entry preserved", "A2" in names_left, str(names_left))
check("faceless: skip reported", "без граней" in status(), status())
restored = {o.name: o for o in bpy.data.objects if o.name in ("A1", "A3")}
check("faceless: A1+A3 linked",
      len(restored) == 2 and restored["A1"].data == restored["A3"].data)

# ---------------------------------------------------------------------------
print("\n=== 52. RESTORE after an FBX roundtrip (bit-exact reference) ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0), rot=(0, 0, 10)))
a2 = add_obj("A2", mesh_a, TRS((4, 0, 0), rot=(0, 0, 30)))
a3 = add_obj("A3", mesh_a, TRS((8, 1, 0)))
orig = {o.name: o.matrix_world.copy() for o in (a1, a2, a3)}
select_only([a1, a2, a3], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
fbx52 = os.path.join(bpy.app.tempdir, "agr_link_restore.fbx")
select_only([cont], cont)
bpy.ops.export_scene.fbx(filepath=fbx52, use_selection=True)
reset_scene()
bpy.ops.import_scene.fbx(filepath=fbx52)
cont2 = next(o for o in bpy.data.objects
             if o.type == 'MESH' and linkmod.is_container(o))
select_only([cont2], cont2)
bpy.ops.agr.link_join()  # refresh: materialise the attrs + idprop
purge_orphan_meshes()
nudge_vert(cont2, inst_id(cont2, "A2"), 0.3)
drop_faces(cont2, inst_id(cont2, "A3"), 2)
select_only([cont2], cont2)
check("fbx-restore: ran", bpy.ops.agr.link_restore(mode='HARD') == {'FINISHED'})
restored = {o.name: o for o in bpy.data.objects}
check("fbx-restore: all linked",
      restored["A1"].data == restored["A2"].data == restored["A3"].data)
check("fbx-restore: coords bit-exact",
      all(local_coords_match(restored[n], tol=1e-6) for n in ("A1", "A2", "A3")))
for n in ("A1", "A2", "A3"):
    check(f"fbx-restore: {n} matrix",
          mat_close(restored[n].matrix_world, orig[n], tol=1e-3))

# ---------------------------------------------------------------------------
print("\n=== 53. RESTORE a single group (group_id) ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
mesh_b = make_cube_mesh("MeshB")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((2, 0, 0)))
b1 = add_obj("B1", mesh_b, TRS((0, 5, 0)))
b2 = add_obj("B2", mesh_b, TRS((2, 5, 0)))
select_only([a1, a2, b1, b2], b1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
purge_orphan_meshes()
nudge_vert(cont, inst_id(cont, "A2"), 0.4)
gid_a = gid_of(cont, "MeshA")
select_only([cont], cont)
check("group-restore: ran",
      bpy.ops.agr.link_restore(mode='SOFT', group_id=gid_a) == {'FINISHED'})
objs = {o.name: o for o in bpy.data.objects}
check("group-restore: A* out and linked",
      {"A1", "A2"} <= set(objs) and objs["A1"].data == objs["A2"].data,
      str(sorted(objs)))
check("group-restore: A2 coords restored", local_coords_match(objs["A2"]))
cont2 = objs.get("B1")
check("group-restore: container keeps the B group",
      cont2 is not None and linkmod.read_table(cont2) is not None
      and len(linkmod.read_table(cont2)["instances"]) == 2)
check("group-restore: container faces = 12",
      cont2 is not None and len(cont2.data.polygons) == 12)

# ---------------------------------------------------------------------------
print("\n=== 54. RESTORE SOFT on a standalone (single-instance) group ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
u1 = add_obj("U1", make_cube_mesh("MeshU"), TRS((0, 5, 0), rot=(0, 0, 25)))
orig_u = u1.matrix_world.copy()
select_only([a1, a2, u1], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
purge_orphan_meshes()
nudge_vert(cont, inst_id(cont, "U1"), 0.3)
select_only([cont], cont)
check("standalone: restore ran", bpy.ops.agr.link_restore(mode='SOFT') == {'FINISHED'})
restored = {o.name: o for o in bpy.data.objects}
check("standalone: U1 coords restored", local_coords_match(restored["U1"]))
check("standalone: U1 matrix", mat_close(restored["U1"].matrix_world, orig_u, tol=1e-3))
check("standalone: INFO level", status_level() == 'INFO', status())

# ---------------------------------------------------------------------------
# Negative restore scenarios: what restore must NOT pretend to fix
# ---------------------------------------------------------------------------

def chunk_verts(cont, iid):
    attr = cont.data.attributes[ATTR]
    vs = set()
    for poly, pa in zip(cont.data.polygons, attr.data):
        if pa.value == iid:
            vs.update(poly.vertices)
    return vs


def flatten_chunk(cont, iid):
    """Scale-to-zero damage: the affine fit degenerates (det ~ 0)."""
    for vi in chunk_verts(cont, iid):
        cont.data.vertices[vi].co.z = 0.0


def flip_chunk(cont, iid):
    bm = _bmesh.new()
    bm.from_mesh(cont.data)
    layer = bm.faces.layers.int.get(ATTR)
    _bmesh.ops.reverse_faces(bm, faces=[f for f in bm.faces if f[layer] == iid])
    bm.to_mesh(cont.data)
    bm.free()


# ---------------------------------------------------------------------------
print("\n=== 55. RESTORE: failed fit is NOT sold as success; HARD uses the healthy ref ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
a3 = add_obj("A3", mesh_a, TRS((6, 0, 0)))
select_only([a1, a2, a3], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
purge_orphan_meshes()
flatten_chunk(cont, inst_id(cont, "A2"))
flatten_chunk(cont, inst_id(cont, "A3"))
select_only([cont], cont)
check("failfit-soft: ran", bpy.ops.agr.link_restore(mode='SOFT') == {'FINISHED'})
restored = {o.name: o for o in bpy.data.objects}
check("failfit-soft: A1 pristine", local_coords_match(restored["A1"]))
check("failfit-soft: flattened stay unique and flat",
      restored["A2"].data != restored["A1"].data
      and not local_coords_match(restored["A2"]))
check("failfit-soft: NOT a clean success", status_level() == 'WARNING', status())
check("failfit-soft: hard-mode hint", "нужен жёсткий режим" in status(), status())

print("--- 55b. HARD rebuilds the flattened chunks from the HEALTHY reference ---")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
a3 = add_obj("A3", mesh_a, TRS((6, 0, 0)))
orig = {o.name: o.matrix_world.copy() for o in (a1, a2, a3)}
select_only([a1, a2, a3], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
purge_orphan_meshes()
flatten_chunk(cont, inst_id(cont, "A2"))
flatten_chunk(cont, inst_id(cont, "A3"))
select_only([cont], cont)
check("failfit-hard: ran", bpy.ops.agr.link_restore(mode='HARD') == {'FINISHED'})
restored = {o.name: o for o in bpy.data.objects}
check("failfit-hard: all linked on the healthy chunk",
      restored["A1"].data == restored["A2"].data == restored["A3"].data)
check("failfit-hard: geometry is the HEALTHY cube", local_coords_match(restored["A2"]))
check("failfit-hard: rebuilt reported", "перестроено по эталону" in status(), status())
check("failfit-hard: approximate position honestly flagged",
      "позиция может быть неточной" in status(), status())
for n in ("A1", "A2", "A3"):
    check(f"failfit-hard: {n} matrix (fresh m_rel fallback)",
          mat_close(restored[n].matrix_world, orig[n], tol=1e-3))

# ---------------------------------------------------------------------------
print("\n=== 56. RESTORE: a flipped lowest-id copy cannot hijack the group ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
ref_normals56 = [p.normal.copy() for p in mesh_a.polygons]
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
a3 = add_obj("A3", mesh_a, TRS((6, 0, 0)))
select_only([a1, a2, a3], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
purge_orphan_meshes()
flip_chunk(cont, inst_id(cont, "A1"))  # vandalise the FIRST (lowest-id) copy
select_only([cont], cont)
check("flip-soft: ran", bpy.ops.agr.link_restore(mode='SOFT') == {'FINISHED'})
restored = {o.name: o for o in bpy.data.objects}
check("flip-soft: healthy majority linked", restored["A2"].data == restored["A3"].data)
check("flip-soft: flipped copy stays unique",
      restored["A1"].data != restored["A2"].data)
check("flip-soft: majority keeps TRUE normals",
      all((p.normal - r).length < 1e-3
          for p, r in zip(restored["A2"].data.polygons, ref_normals56)))

print("--- 56b. HARD rebuilds the flipped copy from the healthy majority ---")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
ref_normals56 = [p.normal.copy() for p in mesh_a.polygons]
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
a3 = add_obj("A3", mesh_a, TRS((6, 0, 0)))
select_only([a1, a2, a3], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
purge_orphan_meshes()
flip_chunk(cont, inst_id(cont, "A1"))
select_only([cont], cont)
check("flip-hard: ran", bpy.ops.agr.link_restore(mode='HARD') == {'FINISHED'})
restored = {o.name: o for o in bpy.data.objects}
check("flip-hard: all linked", restored["A1"].data == restored["A2"].data == restored["A3"].data)
check("flip-hard: TRUE normals won (flip discarded)",
      all((p.normal - r).length < 1e-3
          for p, r in zip(restored["A1"].data.polygons, ref_normals56)))
check("flip-hard: rebuilt reported", "перестроено по эталону" in status(), status())

# ---------------------------------------------------------------------------
print("\n=== 57. RESTORE SOFT never enters the last-resort alive branch ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
a3 = add_obj("A3", mesh_a, TRS((6, 0, 0)))  # never joined: keeps MeshA alive
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
purge_orphan_meshes()
drop_faces(cont, inst_id(cont, "A1"), 1)
drop_faces(cont, inst_id(cont, "A2"), 2)
select_only([cont], cont)
check("soft-lastresort: ran", bpy.ops.agr.link_restore(mode='SOFT') == {'FINISHED'})
restored = {o.name: o for o in bpy.data.objects}
check("soft-lastresort: NO false alive-adoption report",
      "эталон взят из сцены" not in status(), status())
check("soft-lastresort: chunks stay off the alive datablock",
      restored["A1"].data != mesh_a and restored["A2"].data != mesh_a)
check("soft-lastresort: honest hard-mode hint",
      "нужен жёсткий режим" in status(), status())

# ---------------------------------------------------------------------------
print("\n=== 58. RESTORE: never-intact group (loose vert) - no reference, no repaint theft ===")


def make_loose_cube(name):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(CUBE_VERTS + [(0.0, 0.0, 2.0)], [], CUBE_FACES)
    mesh.validate()
    return mesh


reset_scene()
mat_red = bpy.data.materials.new("M_Red")
mat_blue = bpy.data.materials.new("M_Blue")
mesh_a = make_loose_cube("MeshA")
mesh_a.materials.append(mat_red)
mesh_a.materials.append(mat_blue)
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
a3 = add_obj("A3", mesh_a, TRS((6, 0, 0)))
select_only([a1, a2, a3], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
purge_orphan_meshes()
paint_faces(cont, inst_id(cont, "A1"), 1)  # repaint the FIRST copy blue
select_only([cont], cont)
check("loose-soft: ran", bpy.ops.agr.link_restore(mode='SOFT') == {'FINISHED'})
restored = {o.name: o for o in bpy.data.objects if o.name in ("A1", "A2", "A3")}
check("loose-soft: repaint NOT stolen by the unvetted first member",
      [m.name for m in restored["A2"].data.materials] == ["M_Red"]
      and [m.name for m in restored["A1"].data.materials] == ["M_Blue"],
      f'A1={[m.name for m in restored["A1"].data.materials]} '
      f'A2={[m.name for m in restored["A2"].data.materials]}')
check("loose-soft: WARNING level", status_level() == 'WARNING', status())

print("--- 58b. HARD on the same group honestly gives up ---")
reset_scene()
mat_red = bpy.data.materials.new("M_Red")
mat_blue = bpy.data.materials.new("M_Blue")
mesh_a = make_loose_cube("MeshA")
mesh_a.materials.append(mat_red)
mesh_a.materials.append(mat_blue)
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
a3 = add_obj("A3", mesh_a, TRS((6, 0, 0)))
select_only([a1, a2, a3], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
purge_orphan_meshes()
paint_faces(cont, inst_id(cont, "A1"), 1)
select_only([cont], cont)
check("loose-hard: ran", bpy.ops.agr.link_restore(mode='HARD') == {'FINISHED'})
restored = {o.name: o for o in bpy.data.objects if o.name in ("A1", "A2", "A3")}
check("loose-hard: paint preserved (nothing rebuilt onto member[0])",
      [m.name for m in restored["A1"].data.materials] == ["M_Blue"]
      and [m.name for m in restored["A2"].data.materials] == ["M_Red"])
check("loose-hard: no-reference reported", "групп без эталона" in status(), status())

# ---------------------------------------------------------------------------
print("\n=== 59. RESTORE HARD: same-name same-counts stranger is rejected ===")
reset_scene()
mesh_a = make_cube_mesh("MeshA")
a1 = add_obj("A1", mesh_a, TRS((0, 0, 0)))
a2 = add_obj("A2", mesh_a, TRS((3, 0, 0)))
select_only([a1, a2], a1)
bpy.ops.agr.link_join()
cont = bpy.data.objects[0]
purge_orphan_meshes()
cont.data.name = "MeshContainer"  # free the group name for the stranger
stranger_mesh = bpy.data.meshes.new("MeshA")  # same name, same counts...
stranger_mesh.from_pydata([(x, y, z * 4.0) for x, y, z in CUBE_VERTS],
                          [], CUBE_FACES)     # ...alien geometry (4x tall)
stranger_mesh.validate()
add_obj("Stranger", stranger_mesh, T(0, -8, 0))
drop_faces(cont, inst_id(cont, "A1"), 1)
drop_faces(cont, inst_id(cont, "A2"), 2)
select_only([cont], cont)
check("stranger: ran", bpy.ops.agr.link_restore(mode='HARD') == {'FINISHED'})
restored = {o.name: o for o in bpy.data.objects if o.name in ("A1", "A2")}
check("stranger: NOT adopted as reference",
      restored["A1"].data != stranger_mesh and restored["A2"].data != stranger_mesh)
check("stranger: no alive-adoption report",
      "эталон взят из сцены" not in status(), status())
check("stranger: honest give-up", "групп без эталона" in status(), status())
z_span = max(v.co.z for v in restored["A1"].data.vertices) \
    - min(v.co.z for v in restored["A1"].data.vertices)
check("stranger: restored geometry is NOT the 4x-tall alien", z_span < 1.5, str(z_span))
check("stranger: stranger itself untouched",
      len(stranger_mesh.polygons) == 6
      and abs(max(v.co.z for v in stranger_mesh.vertices) - 2.0) < 1e-5)

# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} FAILED:")
    for name in FAILS:
        print(f"   - {name}")
    sys.exit(1)
print("✅ ALL CHECKS PASSED")
