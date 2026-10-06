# Headless test for AGR UV "Экструд с развёрткой" (operators_uv_extrude.py):
# the extrude macro (extrude_region -> arm -> translate -> unfold), the
# live-preview arm/update, and the "Развернуть от соседей" repair operator.
# Run: blender --background --factory-startup --python scripts/test_uv_extrude.py
import math
import os
import sys

import bpy
import bmesh
from mathutils import Matrix, Vector

# repo root = parent of scripts/ — works from any checkout location
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import AGR_tools.log as agr_log
import AGR_tools.operators_uv as uvmod
import AGR_tools.operators_uv_extrude as exmod
from AGR_tools.core.udim_tiles import face_tile_number

agr_log.register()
uvmod.register()
exmod.register()

FAILS = []
COUNT = [0]


def check(name, cond, extra=""):
    COUNT[0] += 1
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


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
    bpy.context.scene.tool_settings.use_transform_correct_face_attributes = False


def make_obj(name, verts, faces, uv_fn=None, with_uv=True, location=(0, 0, 0),
             rotation=(0, 0, 0), scale=(1, 1, 1), edges=()):
    """uv_fn(poly_index, vert_index, co) -> (u, v); default: zeros."""
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata(verts, list(edges), faces)
    mesh.validate()
    if with_uv:
        layer = mesh.uv_layers.new(name="UVMap")
        if uv_fn is not None:
            for poly in mesh.polygons:
                for li in poly.loop_indices:
                    vi = mesh.loops[li].vertex_index
                    layer.data[li].uv = uv_fn(poly.index, vi, Vector(verts[vi]))
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = location
    obj.rotation_euler = rotation
    obj.scale = scale
    bpy.context.view_layer.update()
    return obj


def affine_uv(fn):
    return lambda _pi, _vi, co: fn(co)


def quad_wall(name, uv=lambda co: (co.x, co.y), **kw):
    """One quad 0..1 x 0..1 in local XY; top edge at y = 1."""
    return make_obj(name, [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [(0, 1, 2, 3)],
                    affine_uv(uv), **kw)


def grid(name, nx, ny, uv=None, cell=1.0, **kw):
    verts = [(i * cell, j * cell, 0.0) for j in range(ny + 1) for i in range(nx + 1)]
    faces = []
    for j in range(ny):
        for i in range(nx):
            a = j * (nx + 1) + i
            faces.append((a, a + 1, a + nx + 2, a + nx + 1))
    return make_obj(name, verts, faces, affine_uv(uv) if uv else None, **kw)


def edit(objs, active=None):
    try:
        bpy.ops.object.mode_set(mode='OBJECT')
    except RuntimeError:
        pass
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = active or objs[0]
    bpy.ops.object.mode_set(mode='EDIT')


def select_only(obj, mode, pred):
    """Select (in `mode`) exactly the elements of `obj` matching pred(elem)."""
    bpy.ops.mesh.select_mode(type=mode)
    bm = bmesh.from_edit_mesh(obj.data)
    bm.select_mode = {mode}
    for seq in (bm.faces, bm.edges, bm.verts):
        for el in seq:
            el.select_set(False)
    seq = {'VERT': bm.verts, 'EDGE': bm.edges, 'FACE': bm.faces}[mode]
    for el in seq:
        if pred(el):
            el.select_set(True)
    bm.select_flush_mode()
    bmesh.update_edit_mesh(obj.data)


def edge_on(pred):
    return lambda e: pred(e.verts[0].co) and pred(e.verts[1].co)


def face_center(f):
    return sum((v.co for v in f.verts), Vector()) / len(f.verts)


def run_macro(value, **unfold):
    kw = {"TRANSFORM_OT_translate": {"value": value}}
    if unfold:
        kw["AGR_OT_extrude_uv_unfold"] = unfold
    return bpy.ops.agr.extrude_uv_move(**kw)


def bm_uv(obj):
    bm = bmesh.from_edit_mesh(obj.data)
    return bm, bm.loops.layers.uv.active


def side_items(obj):
    bm, uvl = bm_uv(obj)
    return bm, uvl, exmod._extrusion_targets(bm)


def check_uvs(name, faces, uvl, expected_fn, tol=1e-5):
    """expected_fn(face, local_co) -> (u, v) or None to skip that corner."""
    worst = 0.0
    n = 0
    for face in faces:
        for loop in face.loops:
            exp = expected_fn(face, loop.vert.co.copy())
            if exp is None:
                continue
            n += 1
            worst = max(worst, (loop[uvl].uv - Vector(exp)).length)
    check(name, n > 0 and worst < tol, f"{n} corners, max err {worst:.2e}")


def uv_area(face, uvl):
    pts = [loop[uvl].uv for loop in face.loops]
    s = 0.0
    for i in range(len(pts)):
        a, b = pts[i], pts[(i + 1) % len(pts)]
        s += a.x * b.y - b.x * a.y
    return abs(s) * 0.5


def world_area(obj, face):
    m = obj.matrix_world
    pts = [m @ v.co for v in face.verts]
    n = Vector()
    for i in range(1, len(pts) - 1):
        n += (pts[i] - pts[0]).cross(pts[i + 1] - pts[0])
    return n.length * 0.5


def corner_uvs(obj):
    """Every corner's exact UV keyed by geometry — extrude_region's face
    ORDER differs between runs (pointer-hashed internals), the faces don't."""
    bm, uvl = bm_uv(obj)
    out = []
    for f in bm.faces:
        key = tuple(sorted(tuple(round(c, 6) for c in v.co) for v in f.verts))
        corners = tuple(sorted((tuple(round(c, 6) for c in loop.vert.co), tuple(loop[uvl].uv))
                               for loop in f.loops))
        out.append((key, corners))
    return sorted(out)


def expect_cancel(callop):
    """CANCELLED, or the RuntimeError an ERROR report raises in background."""
    try:
        return callop() == {'CANCELLED'}
    except RuntimeError as exc:
        return "Traceback" not in str(exc)


# ============================================================
print("\n=== TEST 1: edge up 90°, weld, texel density, keep_tile ===")
reset_scene()
w = grid("Wall", 2, 1, uv=lambda co: (co.x, co.y))
edit([w])
select_only(w, 'EDGE', edge_on(lambda co: abs(co.y - 1) < 1e-6))
res = run_macro((0, 0, 0.5), keep_tile=False)
check("macro FINISHED", res == {'FINISHED'}, str(res))
bm, uvl, items = side_items(w)
check("two side faces", len(items) == 2, str(len(items)))
faces = [f for f, _h, _t in items]
check_uvs("hinge corners copy the wall, free corners continue +0.5",
          faces, uvl, lambda f, co: (co.x, 1.0) if abs(co.z) < 1e-6 else (co.x, 1.5))
shared = [loop[uvl].uv.copy() for f in faces for loop in f.loops
          if abs(loop.vert.co.x - 1) < 1e-6 and abs(loop.vert.co.z - 0.5) < 1e-6]
check("free corner shared by both side faces is bit-identical",
      len(shared) == 2 and tuple(shared[0]) == tuple(shared[1]), str(shared))
ratios = [uv_area(f, uvl) / world_area(w, f) for f in faces]
check("texel density equals the wall's (UV area / world area = 1)",
      all(abs(r - 1.0) < 1e-5 for r in ratios), str(ratios))
check("live arm consumed by the final step", not exmod._LIVE)

reset_scene()
w = grid("WallTile", 2, 1, uv=lambda co: (co.x, co.y))
edit([w])
select_only(w, 'EDGE', edge_on(lambda co: abs(co.y - 1) < 1e-6))
run_macro((0, 0, 0.5))  # keep_tile defaults to True
bm, uvl, items = side_items(w)
check_uvs("keep_tile: side faces wholly in tile row 1 move into row 0",
          [f for f, _h, _t in items], uvl,
          lambda f, co: (co.x, 0.0) if abs(co.z) < 1e-6 else (co.x, 0.5))

# ============================================================
print("\n=== TEST 2: any direction — angle sweep, fold-back, shear ===")
for deg in (0, 45, 90, 135, 180, -45, -90):
    reset_scene()
    w = quad_wall("Wall")
    edit([w])
    select_only(w, 'EDGE', edge_on(lambda co: abs(co.y - 1) < 1e-6))
    th = math.radians(deg)
    run_macro((0, 0.7 * math.cos(th), 0.7 * math.sin(th)), keep_tile=False)
    bm, uvl, items = side_items(w)
    check_uvs(f"{deg:+4d}°: free corners 0.7 past the hinge in V",
              [f for f, _h, _t in items], uvl,
              lambda f, co: (co.x, 1.0) if abs(co.y - 1) < 1e-6 and abs(co.z) < 1e-6
              else (co.x, 1.7))

reset_scene()
w = quad_wall("Wall")
edit([w])
select_only(w, 'EDGE', edge_on(lambda co: abs(co.y - 1) < 1e-6))
run_macro((0.3, 0, 0.7), keep_tile=False)
bm, uvl, items = side_items(w)
check_uvs("sheared extrusion: parallelogram kept (U slides +0.3)",
          [f for f, _h, _t in items], uvl,
          lambda f, co: (co.x, 1.0) if abs(co.z) < 1e-6 else (co.x, 1.7))

# ============================================================
print("\n=== TEST 3: anisotropic / rotated / mirrored wall layouts ===")
c30, s30 = math.cos(math.radians(30)), math.sin(math.radians(30))
LAYOUTS = {
    "shear+anisotropic": (Matrix(((2.0, 0.5), (0.0, 1.0))), Vector((0.25, 0.1))),
    "rotated+scaled": (Matrix(((0.5 * c30, -0.5 * s30), (0.5 * s30, 0.5 * c30))), Vector((0.3, 0.2))),
    "mirrored U": (Matrix(((-1.0, 0.0), (0.0, 1.0))), Vector((1.0, 0.0))),
    "mirrored V": (Matrix(((1.0, 0.0), (0.0, -1.0))), Vector((0.0, 1.0))),
}
for label, (M, t) in LAYOUTS.items():
    reset_scene()
    w = quad_wall("Wall", uv=lambda co, M=M, t=t: tuple(M @ Vector((co.x, co.y)) + t))
    edit([w])
    select_only(w, 'EDGE', edge_on(lambda co: abs(co.y - 1) < 1e-6))
    run_macro((0, 0, 0.6), keep_tile=False)
    bm, uvl, items = side_items(w)
    # unfolded plane coordinates of a side-face corner: (x, 1 + height)
    check_uvs(f"{label}: continues the wall's affine map exactly",
              [f for f, _h, _t in items], uvl,
              lambda f, co, M=M, t=t: tuple(M @ Vector((co.x, 1.0 + co.z)) + t))

# ============================================================
print("\n=== TEST 4: window reveal (face region inward) — WALL reference ===")


def reveal_expected(depth):
    """Corner (x, y, z) of a reveal of the 3x3 grid's centre opening, wall
    UV = (x/3, y/3).  Hinge corners (z == 0) keep the wall UV; a corner at
    depth unfolds `depth` metres perpendicular to its side's hinge, towards
    the opening centre."""
    def fn(face, co):
        if abs(co.z) < 1e-6:
            return (co.x / 3, co.y / 3)
        anchor = [v.co for v in face.verts if abs(v.co.z) < 1e-6]
        x, y = co.x, co.y
        if abs(anchor[0].y - anchor[1].y) < 1e-6:  # hinge runs along X
            return (x / 3, (y + math.copysign(depth, 1.5 - y)) / 3)
        return ((x + math.copysign(depth, 1.5 - x)) / 3, y / 3)
    return fn


reset_scene()
w = grid("Facade", 3, 3, uv=lambda co: (co.x / 3, co.y / 3))
edit([w])
select_only(w, 'FACE', lambda f: (face_center(f) - Vector((1.5, 1.5, 0))).length < 1e-6)
run_macro((0, 0, -0.3), keep_tile=False)
bm, uvl, items = side_items(w)
check("four reveal faces found", len(items) == 4, str(len(items)))
faces = [f for f, _h, _t in items]
check_uvs("reveals unfold from the wall into the opening", faces, uvl,
          reveal_expected(0.3))
check("reveal density equals the wall's (1/9)",
      all(abs(uv_area(f, uvl) / world_area(w, f) - 1 / 9) < 1e-6 for f in faces))
cap = [f for f in bm.faces if f.select]
check_uvs("cap keeps its UV", cap, uvl, lambda f, co: (co.x / 3, co.y / 3))
before = corner_uvs(w)

reset_scene()
w = grid("Facade", 3, 3, uv=lambda co: (co.x / 3, co.y / 3))
edit([w])
select_only(w, 'FACE', lambda f: (face_center(f) - Vector((1.5, 1.5, 0))).length < 1e-6)
run_macro((0, 0, -0.3))  # keep_tile on: everything is inside 0..1 already
check("keep_tile is a no-op inside the tile", corner_uvs(w) == before)

# ============================================================
print("\n=== TEST 5: hinge without a wall falls back to the cap ===")
# extruding ONE face of a 2x1 grid deletes the original face: its three
# border hinges have no neighbour left, only the inner hinge keeps a wall
reset_scene()
w = grid("Strip", 2, 1, uv=lambda co: (co.x / 2, co.y))
edit([w])
select_only(w, 'FACE', lambda f: face_center(f).x < 1)
run_macro((0, 0, 0.4), keep_tile=False)
bm, uvl, items = side_items(w)
check("four side faces", len(items) == 4, str(len(items)))
inner = [f for f, h, _t in items if all(abs(v.co.x - 1) < 1e-6 for v in h.verts)]
border = [f for f, h, _t in items if f not in inner]
check("inner hinge keeps its wall, three border hinges have none",
      len(inner) == 1 and len(border) == 3
      and all(len(h.link_faces) == 1 for f, h, _t in items if f in border))
check_uvs("inner side face: from the wall on the right", inner, uvl,
          lambda f, co: (co.x / 2, co.y) if abs(co.z) < 1e-6 else ((co.x - 0.4) / 2, co.y))


def cap_expected(face, co):
    """Pinned to the cap (top, z=0.4), unfolded 0.4 m AWAY from the cap."""
    if abs(co.z - 0.4) < 1e-6:
        return (co.x / 2, co.y)
    if abs(co.x) < 1e-6 and all(abs(v.co.x) < 1e-6 for v in face.verts):
        return ((co.x - 0.4) / 2, co.y)  # left side: away = -x
    if all(abs(v.co.y) < 1e-6 for v in face.verts):
        return (co.x / 2, co.y - 0.4)  # front side: away = -y
    return (co.x / 2, co.y + 0.4)  # back side: away = +y


check_uvs("border side faces: pinned to the cap, unfolded away from it", border, uvl,
          cap_expected)
stats, jobs = exmod._unfold_extrusion(w, bm, uvl, items)
check("stats: 1 from the wall, 3 from the cap", stats == {'WALL': 1, 'CAP': 3, 'no_ref': 0},
      str(stats))

# ============================================================
print("\n=== TEST 6: lone plane — sides unfold around the kept bottom ===")
reset_scene()
bpy.ops.mesh.primitive_plane_add(size=2)
p = bpy.context.active_object
bpy.ops.object.mode_set(mode='EDIT')
select_only(p, 'FACE', lambda f: True)
run_macro((0, 0, 1.0), keep_tile=False)
bm, uvl, items = side_items(p)
check("four side faces", len(items) == 4, str(len(items)))


def lone_expected(face, co):
    if abs(co.z) < 1e-6:
        return ((co.x + 1) / 2, (co.y + 1) / 2)
    base = [v.co for v in face.verts if abs(v.co.z) < 1e-6]
    if abs(base[0].y - base[1].y) < 1e-6:   # side along X: outward in Y
        return ((co.x + 1) / 2, (co.y + math.copysign(1, co.y) + 1) / 2)
    return ((co.x + math.copysign(1, co.x) + 1) / 2, (co.y + 1) / 2)


check_uvs("box sides laid around the bottom's UV (cross layout)",
          [f for f, _h, _t in items], uvl, lone_expected)

# ============================================================
print("\n=== TEST 7: strip continuity on a tube, honest seams at corners ===")
reset_scene()
N = 12
ring = [(math.cos(2 * math.pi * i / N), math.sin(2 * math.pi * i / N)) for i in range(N)]
verts = [(x, y, 0.0) for x, y in ring] + [(x, y, 1.0) for x, y in ring]
faces = [(i, (i + 1) % N, N + (i + 1) % N, N + i) for i in range(N)]
seg = 2 * math.sin(math.pi / N)  # chord length = 3D edge length


def tube_uv(pi, vi, co):
    i = vi % N
    k = i if i >= pi else N  # the last face closes the ring at u = N*seg (UV seam)
    return (k * seg, co.z)


tube = make_obj("Tube", verts, faces, tube_uv)
edit([tube])
select_only(tube, 'EDGE', edge_on(lambda co: abs(co.z - 1) < 1e-6))
run_macro((0, 0, 0.5), keep_tile=False)
bm, uvl, items = side_items(tube)
check("12 strip faces", len(items) == N, str(len(items)))
free = {}
for f, _h, _t in items:
    for loop in f.loops:
        if loop.vert.co.z > 1.25:
            free.setdefault(loop.vert.index, []).append(tuple(loop[uvl].uv))
vs = [uv[1] for lst in free.values() for uv in lst]
check("strip continues V to 1.5 everywhere", all(abs(v - 1.5) < 1e-5 for v in vs))
groups = [lst for lst in free.values() if len(lst) == 2]
same = sum(1 for a, b in groups if a == b)
check("straight neighbours share bit-identical corners; the UV seam stays",
      len(groups) == N and same == N - 1, f"{same}/{len(groups)} identical")

reset_scene()
floor_ = make_obj("Floor", [(0, 0, 0), (2, 0, 0), (2, 2, 0), (0, 2, 0)], [(0, 1, 2, 3)],
                  affine_uv(lambda co: (co.x, co.y)))
edit([floor_])
select_only(floor_, 'EDGE', lambda e: True)
run_macro((0, 0, 0.5), keep_tile=False)
bm, uvl, items = side_items(floor_)
check("four walls from the footprint", len(items) == 4, str(len(items)))


def floor_expected(face, co):
    if abs(co.z) < 1e-6:
        return (co.x, co.y)
    base = [v.co for v in face.verts if abs(v.co.z) < 1e-6]
    if abs(base[0].y - base[1].y) < 1e-6:
        return (co.x, co.y + (0.5 if co.y > 1 else -0.5))
    return (co.x + (0.5 if co.x > 1 else -0.5), co.y)


check_uvs("each wall unfolds outward around its own hinge",
          [f for f, _h, _t in items], uvl, floor_expected)
corner = [tuple(loop[uvl].uv) for f, _h, _t in items for loop in f.loops
          if (loop.vert.co - Vector((2, 0, 0.5))).length < 1e-6]
check("90° corner keeps a seam (two different UVs)", len(set(corner)) == 2, str(corner))

# ============================================================
print("\n=== TEST 8: fin from an interior edge picks the wall it leans away from ===")
for lean, sign in ((0.3, 1), (-0.3, -1)):
    reset_scene()
    w = grid("Fin", 2, 1, uv=lambda co: (co.x, co.y))
    edit([w])
    select_only(w, 'EDGE', edge_on(lambda co: abs(co.x - 1) < 1e-6))
    run_macro((lean, 0, 1.0), keep_tile=False)
    bm, uvl, items = side_items(w)
    reach = math.hypot(lean, 1.0)
    check_uvs(f"lean {lean:+.1f}: unfolds over the leaned-to side",
              [f for f, _h, _t in items], uvl,
              lambda f, co, s=sign, r=reach: (1.0, co.y) if abs(co.z) < 1e-6
              else (1.0 + s * r, co.y))

# ============================================================
print("\n=== TEST 9: wire edge — no reference, nothing written ===")
reset_scene()
wire = make_obj("Wire", [(0, 0, 0), (1, 0, 0)], [], edges=[(0, 1)])
edit([wire])
select_only(wire, 'EDGE', lambda e: True)
res = run_macro((0, 0, 1.0))
bm, uvl, items = side_items(wire)
check("wire extrude: FINISHED with one face", res == {'FINISHED'} and len(items) == 1)
check("its UVs stay untouched", all(tuple(l[uvl].uv) == (0.0, 0.0)
                                    for f, _h, _t in items for l in f.loops))
stats, jobs = exmod._unfold_extrusion(wire, bm, uvl, items)
check("stats report no_ref, nothing to cut", stats['no_ref'] == 1 and not stats['WALL']
      and not jobs, str(stats))

# ============================================================
print("\n=== TEST 10: world space — scale, rotation + city offset, mirror ===")
reset_scene()
w = quad_wall("Scaled", scale=(1, 1, 2))
edit([w])
select_only(w, 'EDGE', edge_on(lambda co: abs(co.y - 1) < 1e-6))
run_macro((0, 0, 1.0), keep_tile=False)  # world 1.0 = local 0.5 under scale z=2
bm, uvl, items = side_items(w)
check_uvs("non-uniform scale: texel measured in world metres",
          [f for f, _h, _t in items], uvl,
          lambda f, co: (co.x, 1.0) if abs(co.z) < 1e-6 else (co.x, 2.0))

reset_scene()
w = quad_wall("Rotated", location=(5000.0, -7000.0, 300.0),
              rotation=(math.radians(90), 0, 0))
edit([w])
select_only(w, 'EDGE', edge_on(lambda co: abs(co.y - 1) < 1e-6))
run_macro((0, -0.8, 0), keep_tile=False)  # local +Z is world -Y after R_x(90°)
bm, uvl, items = side_items(w)
check_uvs("rotated object 5 km from the origin: same UVs",
          [f for f, _h, _t in items], uvl,
          lambda f, co: (co.x, 1.0) if abs(co.z) < 1e-6 else (co.x, 1.8), tol=2e-5)

reset_scene()
w = quad_wall("Mirrored", scale=(-1, 1, 1))
edit([w])
select_only(w, 'EDGE', edge_on(lambda co: abs(co.y - 1) < 1e-6))
run_macro((0, 0, 0.5), keep_tile=False)
bm, uvl, items = side_items(w)
check_uvs("negative scale: unfold still lands on the far side of the hinge",
          [f for f, _h, _t in items], uvl,
          lambda f, co: (co.x, 1.0) if abs(co.z) < 1e-6 else (co.x, 1.5))

# ============================================================
print("\n=== TEST 11: multi-object edit mode ===")
reset_scene()
a = quad_wall("A")
b = quad_wall("B", location=(3, 0, 0), uv=lambda co: (co.x * 2, co.y * 2))
edit([a, b], active=a)
for o in (a, b):
    select_only(o, 'EDGE', edge_on(lambda co: abs(co.y - 1) < 1e-6))
run_macro((0, 0, 0.5), keep_tile=False)
bm, uvl, items = side_items(a)
check_uvs("object A", [f for f, _h, _t in items], uvl,
          lambda f, co: (co.x, 1.0) if abs(co.z) < 1e-6 else (co.x, 1.5))
bm, uvl, items = side_items(b)
check_uvs("object B (its own density 2/m)", [f for f, _h, _t in items], uvl,
          lambda f, co: (co.x * 2, 2.0) if abs(co.z) < 1e-6 else (co.x * 2, 3.0))

# ============================================================
print("\n=== TEST 12: mesh without a UV layer ===")
reset_scene()
w = quad_wall("NoUV", with_uv=False)
edit([w])
select_only(w, 'EDGE', edge_on(lambda co: abs(co.y - 1) < 1e-6))
res = run_macro((0, 0, 0.5))
bm = bmesh.from_edit_mesh(w.data)
check("extrude still happens", res == {'FINISHED'} and len(bm.faces) == 2)
check("no UV layer invented", len(bm.loops.layers.uv) == 0)

# ============================================================
print("\n=== TEST 13: live preview arm/update/disarm ===")
reset_scene()
w = quad_wall("Live")
edit([w])
select_only(w, 'EDGE', edge_on(lambda co: abs(co.y - 1) < 1e-6))
bpy.ops.mesh.extrude_region()  # the macro's first step, no move yet
exmod._arm_live(bpy.context)
state = exmod._LIVE.get(w.session_uid)
check("armed one side face", state is not None and len(state["items"]) == 1)
bm = bmesh.from_edit_mesh(w.data)
for v in bm.verts:
    if v.select:
        v.co.z += 0.7  # what the modal translate does step by step
check("live update re-unfolds", exmod._live_update(w, state))
bm, uvl, items = side_items(w)
check_uvs("preview follows the moved corners (no tile shift while dragging)",
          [f for f, _h, _t in items], uvl,
          lambda f, co: (co.x, 1.0) if abs(co.z) < 1e-6 else (co.x, 1.7))
check("preview never cuts", len(bm.faces) == 2)
bm.verts.new((5, 5, 5))  # topology moved under the arm (auto-merge, undo...)
check("topology change disarms", not exmod._live_update(w, state))

# the handler itself — a real modal needs a GUI (scripts/… has none), so the
# modal probe is faked; the GUI run with simulated events is what showed the
# translate evaluating the depsgraph BEFORE its modal handler exists
from time import perf_counter

real_modal = exmod._macro_modal
modal = [False]
exmod._macro_modal = lambda: modal[0]
try:
    reset_scene()
    w = quad_wall("LiveHandler")
    edit([w])
    select_only(w, 'EDGE', edge_on(lambda co: abs(co.y - 1) < 1e-6))
    bpy.ops.mesh.extrude_region()
    exmod._arm_live(bpy.context)

    def lift(dz):
        for v in bmesh.from_edit_mesh(w.data).verts:
            if v.select:
                v.co.z += dz

    def top_v():
        bm, uvl, items = side_items(w)
        return {round(l[uvl].uv.y, 6) for f, _h, _t in items for l in f.loops if l.vert.select}

    lift(0.4)
    exmod._uv_extrude_live_update(None, None)
    check("translate still initialising (not modal yet): arm survives, nothing written",
          bool(exmod._LIVE) and top_v() == {1.0}, str(top_v()))
    modal[0] = True
    exmod._uv_extrude_live_update(None, None)
    check("handler step unfolds once the macro is modal", top_v() == {1.4}, str(top_v()))
    exmod._LIVE_STATE["next"] = perf_counter() + 100.0
    lift(0.3)
    exmod._uv_extrude_live_update(None, None)
    check("throttled step is skipped", top_v() == {1.4}, str(top_v()))
    exmod._LIVE_STATE["next"] = 0.0
    exmod._uv_extrude_live_update(None, None)
    check("next step catches up", top_v() == {1.7}, str(top_v()))
    modal[0] = False
    lift(0.3)
    exmod._uv_extrude_live_update(None, None)
    check("modal seen and gone (confirm/cancel): disarmed without writing",
          not exmod._LIVE and top_v() == {1.7}, str(top_v()))

    exmod._arm_live(bpy.context)
    exmod._LIVE_STATE["deadline"] = perf_counter() - 1.0
    exmod._uv_extrude_live_update(None, None)
    check("never became modal within the grace time: disarmed", not exmod._LIVE)
finally:
    exmod._macro_modal = real_modal
    exmod._LIVE.clear()
    exmod._LIVE_STATE.update(seen=False, deadline=0.0, next=0.0)

reset_scene()
w = quad_wall("LiveOps")
edit([w])
select_only(w, 'EDGE', edge_on(lambda co: abs(co.y - 1) < 1e-6))
run_macro((0, 0, 0.5))
check("macro leaves nothing armed", not exmod._LIVE)

# ============================================================
print("\n=== TEST 14: «Развернуть от соседей» after a stock extrude ===")


def stock_window(depths):
    reset_scene()
    w = grid("Facade", 3, 3, uv=lambda co: (co.x / 3, co.y / 3))
    edit([w])
    select_only(w, 'FACE', lambda f: (face_center(f) - Vector((1.5, 1.5, 0))).length < 1e-6)
    for d in depths:
        bpy.ops.mesh.extrude_region_move(TRANSFORM_OT_translate={"value": (0, 0, -d)})
    return w


w = stock_window([0.3])
bm, uvl = bm_uv(w)
reveals = [f for f in bm.faces if len({round(v.co.z, 6) for v in f.verts}) == 2]
check("stock E leaves collapsed reveals", len(reveals) == 4
      and all(uv_area(f, uvl) < 1e-12 for f in reveals))
select_only(w, 'FACE', lambda f: len({round(v.co.z, 6) for v in f.verts}) == 2)
res = bpy.ops.agr.uv_unfold_selected(keep_tile=False)
bm, uvl = bm_uv(w)
check("repair FINISHED", res == {'FINISHED'}, str(res))
check_uvs("wall wins over the cap: same result as the macro",
          [f for f in bm.faces if f.select], uvl, reveal_expected(0.3))

w = stock_window([0.3, 0.2])
select_only(w, 'FACE', lambda f: len({round(v.co.z, 6) for v in f.verts}) == 2)
bpy.ops.agr.uv_unfold_selected(keep_tile=False)
bm, uvl = bm_uv(w)
sel = [f for f in bm.faces if f.select]
check("stepped reveal: 8 faces selected", len(sel) == 8, str(len(sel)))


def ring_expected(face, co):
    depth = -co.z
    if depth < 1e-6:
        return (co.x / 3, co.y / 3)
    # side of the opening this reveal belongs to: its corners share x or y
    if all(abs(v.co.x - face.verts[0].co.x) < 1e-6 for v in face.verts):
        return ((co.x + math.copysign(depth, 1.5 - co.x)) / 3, co.y / 3)
    return (co.x / 3, (co.y + math.copysign(depth, 1.5 - co.y)) / 3)


check_uvs("inner ring continues from the outer ring, not from the cap", sel, uvl,
          ring_expected)

w = stock_window([0.3])
select_only(w, 'FACE', lambda f: (face_center(f) - Vector((1.5, 1.5, -0.3))).length < 1e-6)
cap_before = corner_uvs(w)
res = bpy.ops.agr.uv_unfold_selected()
check("cap-only selection: CANCELLED (its neighbours are collapsed)",
      res == {'CANCELLED'}, str(res))
check("...and nothing changed", corner_uvs(w) == cap_before)
select_only(w, 'FACE', lambda f: False)
check("empty selection: CANCELLED", expect_cancel(bpy.ops.agr.uv_unfold_selected))

# ============================================================
print("\n=== TEST 15: «Держать в квадрате» — cut along UV tile borders, shift pieces ===")


def in_tile(faces, uvl, tile=(0, 0), eps=1e-5):
    vals = [l[uvl].uv for f in faces for l in f.loops]
    return all(tile[0] - eps <= w.x <= tile[0] + 1 + eps and tile[1] - eps <= w.y <= tile[1] + 1 + eps
               for w in vals)


def zs(bm):
    return sorted({round(v.co.z, 5) for v in bm.verts})


reset_scene()
w = quad_wall("Straddle", uv=lambda co: (co.x, 0.25 + 0.7 * co.y))
edit([w])
select_only(w, 'EDGE', edge_on(lambda co: abs(co.y - 1) < 1e-6))
run_macro((0, 0, 0.2))
bm, uvl = bm_uv(w)
sides = [f for f in bm.faces if any(v.co.z > 1e-6 for v in f.verts)]
check("straddling face cut at the border: 2 pieces", len(sides) == 2, str(len(sides)))
check("cut sits where V crosses 1.0 (z = 0.05/0.7)", round(0.05 / 0.7, 5) in zs(bm), str(zs(bm)))
check("both pieces inside the wall's square", in_tile(sides, uvl))
low = [f for f in sides if min(v.co.z for v in f.verts) < 1e-6]
check_uvs("lower piece keeps the hinge welded to the wall", low, uvl,
          lambda f, co: (co.x, 0.95) if abs(co.z) < 1e-6 else None)

reset_scene()
w = quad_wall("Tall")
edit([w])
select_only(w, 'EDGE', edge_on(lambda co: abs(co.y - 1) < 1e-6))
run_macro((0, 0, 2.3))
bm, uvl = bm_uv(w)
sides = [f for f in bm.faces if any(v.co.z > 1e-6 for v in f.verts)]
check("2.3 tiles tall: cut into 3 pieces at z = 1 and z = 2",
      len(sides) == 3 and zs(bm) == [0.0, 1.0, 2.0, 2.3], f"{len(sides)} {zs(bm)}")
check("every piece inside the square", in_tile(sides, uvl))
consistent = True
for f in sides:
    offs = {round(l[uvl].uv.y - (1.0 + l.vert.co.z), 5) for l in f.loops}
    offs |= {round(l[uvl].uv.x - l.vert.co.x, 5) + 100 for l in f.loops}
    consistent &= len(offs) == 2 and all(abs(o - round(o)) < 1e-5 for o in offs)
check("each piece = the continuous unfold shifted by whole tiles", consistent)

reset_scene()
w = quad_wall("TallNoCut")
edit([w])
select_only(w, 'EDGE', edge_on(lambda co: abs(co.y - 1) < 1e-6))
run_macro((0, 0, 2.3), keep_tile=False)
bm, uvl, items = side_items(w)
check("keep_tile off: no cut, continuous V up to 3.3",
      len(bm.faces) == 2 and abs(max(l[uvl].uv.y for f, _h, _t in items for l in f.loops) - 3.3) < 1e-5)

for mode in ('VERT', 'EDGE'):
    reset_scene()
    w = quad_wall("Shear" + mode)
    edit([w])
    if mode == 'VERT':
        select_only(w, 'VERT', lambda v: abs(v.co.y - 1) < 1e-6)
    else:
        select_only(w, 'EDGE', edge_on(lambda co: abs(co.y - 1) < 1e-6))
    run_macro((0.6, 0, 0.5))  # sheared: U runs 0..1.6 -> border u = 1 crosses the TOP edge
    bm, uvl = bm_uv(w)
    sides = [f for f in bm.faces if any(v.co.z > 1e-6 for v in f.verts)]
    check(f"{mode}: sheared face cut along u = 1 into 2 pieces, both in the square",
          len(sides) == 2 and in_tile(sides, uvl), str(len(sides)))
    top = [v for v in bm.verts if abs(v.co.z - 0.5) < 1e-6]
    top_e = [e for e in bm.edges if all(abs(v.co.z - 0.5) < 1e-6 for v in e.verts)]
    check(f"{mode}: split top edge stays fully selected (3 verts, 2 halves)",
          len(top) == 3 and all(v.select for v in top) and len(top_e) == 2
          and all(e.select for e in top_e)
          and not any(v.select for v in bm.verts if abs(v.co.z) < 1e-6))
    res = run_macro((0, 0, 0.5))
    bm, uvl, items = side_items(w)
    check(f"{mode}: chained extrusion from the cut top: 2 new side faces in the square",
          res == {'FINISHED'} and len(items) == 2 and in_tile([f for f, _h, _t in items], uvl),
          str(len(items)))

reset_scene()
w = grid("DeepWindow", 3, 3, uv=lambda co: (co.x, co.y))
edit([w])
select_only(w, 'FACE', lambda f: (face_center(f) - Vector((1.5, 1.5, 0))).length < 1e-6)
run_macro((0, 0, -1.5))  # reveals 1.5 deep: they cross a border of the 1 m squares
bm, uvl = bm_uv(w)
reveal_pieces = [f for f in bm.faces if not f.select and any(v.co.z < -1e-6 for v in f.verts)]
cap = [f for f in bm.faces if f.select]
check("deep window: reveals cut (8 pieces), cap still selected",
      len(reveal_pieces) == 8 and len(cap) == 1 and all(v.select for v in cap[0].verts),
      f"{len(reveal_pieces)} pieces, cap {len(cap)}")


def piece_tile(f, uvl):
    """The one UV square a piece lies in, None when it straddles a border."""
    us = [l[uvl].uv.x for l in f.loops]
    vs = [l[uvl].uv.y for l in f.loops]
    tu = {math.floor(min(us) + 1e-5), math.floor(max(us) - 1e-5)}
    tv = {math.floor(min(vs) + 1e-5), math.floor(max(vs) - 1e-5)}
    return (tu.pop(), tv.pop()) if len(tu) == 1 and len(tv) == 1 else None


def wall_square(f):
    """Square of the wall a reveal piece grew from (wall UV = (x, y))."""
    if all(abs(v.co.y - 1) < 1e-6 for v in f.verts):
        return (1, 0)   # bottom reveal: wall below the opening
    if all(abs(v.co.y - 2) < 1e-6 for v in f.verts):
        return (1, 2)   # top reveal
    if all(abs(v.co.x - 1) < 1e-6 for v in f.verts):
        return (0, 1)   # left reveal
    return (2, 1)       # right reveal


bad = [(piece_tile(f, uvl), wall_square(f)) for f in reveal_pieces
       if piece_tile(f, uvl) != wall_square(f)]
check("deep window: every reveal piece sits in the square of ITS wall", not bad, str(bad))
check_uvs("deep window: cap UV untouched", cap, uvl, lambda f, co: (co.x, co.y))

reset_scene()
w = quad_wall("Repair")
edit([w])
select_only(w, 'EDGE', edge_on(lambda co: abs(co.y - 1) < 1e-6))
bpy.ops.mesh.extrude_region_move(TRANSFORM_OT_translate={"value": (0, 0, 1.5)})  # stock
select_only(w, 'FACE', lambda f: any(v.co.z > 1e-6 for v in f.verts))
res = bpy.ops.agr.uv_unfold_selected()
bm, uvl = bm_uv(w)
sides = [f for f in bm.faces if any(v.co.z > 1e-6 for v in f.verts)]
check("repair button cuts too: 2 pieces in the square",
      res == {'FINISHED'} and len(sides) == 2 and in_tile(sides, uvl), f"{res} {len(sides)}")

reset_scene()
w = quad_wall("Cap", uv=lambda co: (co.x * 100, co.y * 100))
edit([w])
select_only(w, 'EDGE', edge_on(lambda co: abs(co.y - 1) < 1e-6))
run_macro((0, 0, 1.0), keep_tile=False)  # 100 tile borders across one face
bm, uvl, items = side_items(w)
stats, jobs = exmod._unfold_extrusion(w, bm, uvl, items)
cut = exmod._cut_to_tiles(w, bm, uvl, jobs)
check("more than 64 borders on a face: not cut, reported",
      cut['too_many'] == 1 and cut['cut'] == 0 and len(bm.faces) == 2, str(cut))

reset_scene()
w = quad_wall("Udim1002", uv=lambda co: (1.0 + co.x, co.y))
edit([w])
select_only(w, 'EDGE', edge_on(lambda co: abs(co.y - 1) < 1e-6))
run_macro((0, 0, 0.5))
bm, uvl, items = side_items(w)
tiles = {face_tile_number([tuple(l[uvl].uv) for l in f.loops]) for f, _h, _t in items}
check("UDIM: the extension stays in the wall's tile 1002", tiles == {1002}, str(tiles))

reset_scene()
w = quad_wall("LeftBorder")
edit([w])
select_only(w, 'EDGE', edge_on(lambda co: abs(co.x) < 1e-6))
run_macro((-0.4, 0, 0))
bm, uvl, items = side_items(w)
check_uvs("hinge ON the u=0 border: probe looks inside the wall, face moves +1",
          [f for f, _h, _t in items], uvl,
          lambda f, co: (1.0, co.y) if abs(co.x) < 1e-6 else (0.6, co.y))

# ============================================================
print("\n=== TEST 16: concave wall ngon — interior side from the winding ===")
reset_scene()
u_verts = [(0, 0, 0), (3, 0, 0), (3, 2, 0), (2, 2, 0), (2, 0.5, 0), (1, 0.5, 0),
           (1, 2, 0), (0, 2, 0)]
uw = make_obj("UWall", u_verts, [tuple(range(8))], affine_uv(lambda co: (co.x / 3, co.y / 3)))
edit([uw])
select_only(uw, 'EDGE', edge_on(lambda co: abs(co.y - 0.5) < 1e-6 and 1 - 1e-6 < co.x < 2 + 1e-6))
run_macro((0, 0, 0.4), keep_tile=False)
bm, uvl, items = side_items(uw)
check_uvs("notch floor (centroid ABOVE it) still unfolds away from the material",
          [f for f, _h, _t in items], uvl,
          lambda f, co: (co.x / 3, 0.5 / 3) if abs(co.z) < 1e-6 else (co.x / 3, 0.9 / 3))

# ============================================================
print("\n=== TEST 17: Correct Face Attributes ON does not change the result ===")
reset_scene()
bpy.context.scene.tool_settings.use_transform_correct_face_attributes = True
w = grid("Wall", 2, 1, uv=lambda co: (co.x, co.y))
edit([w])
select_only(w, 'EDGE', edge_on(lambda co: abs(co.y - 1) < 1e-6))
run_macro((0, 0.3, 0.5), keep_tile=False)
bm, uvl, items = side_items(w)
reach = math.hypot(0.3, 0.5)
check_uvs("side faces still unfolded exactly", [f for f, _h, _t in items], uvl,
          lambda f, co: (co.x, 1.0) if abs(co.z) < 1e-6 else (co.x, 1.0 + reach))
bpy.context.scene.tool_settings.use_transform_correct_face_attributes = False

# ============================================================
print("\n=== TEST 18: collapsed reference is refused, determinism ===")
reset_scene()
w = quad_wall("Chain")
edit([w])
select_only(w, 'EDGE', edge_on(lambda co: abs(co.y - 1) < 1e-6))
bpy.ops.mesh.extrude_region_move(TRANSFORM_OT_translate={"value": (0, 0, 0.5)})  # stock: collapsed
run_macro((0, 0, 0.5))
bm, uvl, items = side_items(w)
stats, _jobs = exmod._unfold_extrusion(w, bm, uvl, items)
check("wall with collapsed UV is not a reference", stats['no_ref'] == 1, str(stats))

runs = []
for _ in range(2):
    reset_scene()
    w = grid("Det", 3, 3, uv=lambda co: (co.x / 3, co.y / 3))
    edit([w])
    select_only(w, 'FACE', lambda f: (face_center(f) - Vector((1.5, 1.5, 0))).length < 1e-6)
    run_macro((0.1, 0.05, -0.3))
    runs.append(corner_uvs(w))
check("same input, bit-identical output", runs[0] == runs[1])

# ============================================================
print("\n=== TEST 19: registration lifecycle ===")
reset_scene()
menu = bpy.types.VIEW3D_MT_edit_mesh_extrude


def menu_count():
    return sum(1 for fn in menu._dyn_ui_initialize()
               if getattr(fn, "__name__", "") == "_draw_extrude_menu")


def handler_count(lst=None):
    lists = [lst] if lst is not None else [bpy.app.handlers.depsgraph_update_pre,
                                           bpy.app.handlers.depsgraph_update_post]
    return sum(1 for hl in lists for h in hl
               if getattr(h, "__name__", "") == "_uv_extrude_live_update")


check("one menu entry, one handler — in depsgraph_update_PRE",
      menu_count() == 1 and handler_count() == 1
      and handler_count(bpy.app.handlers.depsgraph_update_pre) == 1,
      f"menu {menu_count()} handler {handler_count()}")
macro_rna = bpy.ops.agr.extrude_uv_move.get_rna_type()
steps = [p.identifier for p in macro_rna.properties if p.identifier != "rna_type"]
check("macro steps in order", steps == ["MESH_OT_extrude_region", "AGR_OT_extrude_uv_arm",
                                        "TRANSFORM_OT_translate", "AGR_OT_extrude_uv_unfold"],
      str(steps))


def op_registered(name):
    try:
        getattr(bpy.ops.agr, name).get_rna_type()
        return True
    except Exception:
        return False


# a dev reload leaves the PREVIOUS module instance's function objects behind
# (identity-based remove() cannot see them) — plant such zombies by name
def _draw_extrude_menu(self, context):
    pass


def _uv_extrude_live_update(scene, depsgraph):
    pass


exmod.unregister()
menu.append(_draw_extrude_menu)
# the first version sat in POST, the current one in PRE — zombies of both
bpy.app.handlers.depsgraph_update_post.append(_uv_extrude_live_update)
bpy.app.handlers.depsgraph_update_pre.append(_uv_extrude_live_update)
exmod.register()
check("zombie menu entry / handlers (PRE and legacy POST) of a previous instance replaced",
      menu_count() == 1 and handler_count() == 1, f"menu {menu_count()} handler {handler_count()}")
kc = bpy.context.window_manager.keyconfigs.addon
if kc is not None:
    km = kc.keymaps.get("Mesh")
    ours = [k for k in km.keymap_items if k.idname == "agr.extrude_uv"] if km else []
    check("Ctrl+Alt+E bound once in the Mesh keymap (no Alt+I any more)",
          len(ours) == 1 and ours[0].type == 'E' and ours[0].ctrl and ours[0].alt
          and not ours[0].shift, str([(k.type, k.ctrl, k.alt) for k in ours]))
else:
    print("  (no addon keyconfig in background — keymap check skipped)")
exmod.unregister()
check("unregister clears menu and handler", menu_count() == 0 and handler_count() == 0)
check("operators gone", not op_registered("extrude_uv_move") and not op_registered("extrude_uv"))
exmod.unregister()  # idempotent
exmod.register()
check("re-register works", op_registered("extrude_uv_move") and menu_count() == 1)

# ============================================================
print(f"\n{COUNT[0]} checks, {len(FAILS)} failed")
if FAILS:
    print("FAILED:")
    for name in FAILS:
        print("  -", name)
    sys.exit(1)
print("ALL CHECKS PASSED")
