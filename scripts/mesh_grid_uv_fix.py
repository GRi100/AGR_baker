import bpy, bmesh
from mathutils import Vector
from math import floor

# --- settings ---
SWAP_XY  = False
FLIP_U   = True     # both flips worked for the original test wall
FLIP_V   = True
SNAP_TOL = 0.005     # cell fraction: verts closer than this to a grid line snap to it
UV_NAME  = "UVMap"
ONLY_SELECTED_FACES = False
# -----------------

obj = bpy.context.active_object
assert obj.mode == 'EDIT', "Edit Mode + 2 выделенных ребра"
M  = obj.matrix_world
me = obj.data
bm = bmesh.from_edit_mesh(me)

edges = [e for e in bm.edges if e.select]
assert len(edges) == 2, f"нужно ровно 2 ребра, выделено {len(edges)}"

def wdir(e):
    a = M @ e.verts[0].co; b = M @ e.verts[1].co
    return (b - a), (b - a).length, a

d1, L1, o1 = wdir(edges[0])
d2, L2, o2 = wdir(edges[1])
n1, n2 = d1.normalized(), d2.normalized()

normal = n1.cross(n2)
assert normal.length > 1e-6, "рёбра почти параллельны"
normal.normalize()

if abs(n1.x) >= abs(n2.x):
    (Xd, Lu, O), (Yraw, Lv) = (n1, L1, o1), (n2, L2)
else:
    (Xd, Lu, O), (Yraw, Lv) = (n2, L2, o2), (n1, L1)

if SWAP_XY:
    (Xd, Lu, O), (Yraw, Lv) = (Yraw, Lv, O), (Xd, Lu)

Xd = Xd.normalized()
Yd = normal.cross(Xd).normalized()
if Yd.dot(Yraw) < 0: Yd = -Yd
if FLIP_U: Xd = -Xd
if FLIP_V: Yd = -Yd

uv = bm.loops.layers.uv.verify()

def gc(p):
    d = p - O
    return Vector((d.dot(Xd)/Lu, d.dot(Yd)/Lv))

def snap(v):
    r = round(v)
    return r if abs(v - r) <= SNAP_TOL else v

faces = [f for f in bm.faces if (f.select or not ONLY_SELECTED_FACES)]
for f in faces:
    c = gc(M @ f.calc_center_median())
    cu, cv = floor(c.x), floor(c.y)
    for loop in f.loops:
        p = gc(M @ loop.vert.co)
        loop[uv].uv = (snap(p.x) - cu, snap(p.y) - cv)

bmesh.update_edit_mesh(me)
print(f"U-cell={Lu:.4f} V-cell={Lv:.4f} faces={len(faces)} tol={SNAP_TOL}")