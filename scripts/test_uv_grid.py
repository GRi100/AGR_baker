# Headless test for AGR_tools/operators_uv.py
# Run: blender --background --factory-startup --python scripts/test_uv_grid.py
import os
import sys
import traceback

import bpy
import bmesh
from math import cos, degrees, pi, radians, sin
from mathutils import Euler, Matrix, Vector

# repo root = parent of scripts/ — works from any checkout location
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import AGR_tools.log as agr_log
import AGR_tools.operators_uv as uvmod

agr_log.register()
uvmod.register()

FAILS = []


def check(name, cond, extra=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def expect_cancel(callop):
    """True only for a clean CANCELLED / report-ERROR outcome.

    bpy.ops wraps ANY uncaught exception from execute() in RuntimeError,
    so a bare `except RuntimeError: return True` would count a genuine
    crash as the expected cancellation.  A clean op.report({'ERROR'})
    message never contains a traceback; a crash does.
    """
    try:
        return callop() == {'CANCELLED'}
    except RuntimeError as exc:
        return "Traceback" not in str(exc)


def make_grid_object(name, nx, ny, cell=1.0, matrix=None, plane="XY"):
    """Explicit (nx x ny)-cell quad grid with spacing `cell`."""
    mesh = bpy.data.meshes.new(name)
    verts = []
    for j in range(ny + 1):
        for i in range(nx + 1):
            if plane == "XY":
                verts.append((i * cell, j * cell, 0.0))
            else:  # XZ wall (normal along -Y for this winding... checked below)
                verts.append((i * cell, 0.0, j * cell))
    faces = []
    for j in range(ny):
        for i in range(nx):
            k = j * (nx + 1) + i
            faces.append((k, k + 1, k + nx + 2, k + nx + 1))
    mesh.from_pydata(verts, [], faces)
    mesh.validate()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    if matrix is not None:
        obj.matrix_world = matrix
    return obj


def enter_edit(obj):
    bpy.ops.object.mode_set(mode='OBJECT')
    for o in bpy.context.selected_objects:
        o.select_set(False)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    return bmesh.from_edit_mesh(obj.data)


def deselect_all(bm):
    for v in bm.verts:
        v.select = False
    for e in bm.edges:
        e.select = False
    for f in bm.faces:
        f.select = False


def select_edge_between(bm, obj, wa, wb):
    """Select the edge whose WORLD endpoints match wa..wb."""
    inv = obj.matrix_world
    for e in bm.edges:
        pa, pb = inv @ e.verts[0].co, inv @ e.verts[1].co
        if ((pa - Vector(wa)).length < 1e-5 and (pb - Vector(wb)).length < 1e-5) or \
           ((pa - Vector(wb)).length < 1e-5 and (pb - Vector(wa)).length < 1e-5):
            e.select = True
            for v in e.verts:
                v.select = True
            return True
    return False


def select_vert_at(bm, obj, wco):
    """Select the single vertex whose WORLD position matches wco."""
    mat = obj.matrix_world
    for v in bm.verts:
        if (mat @ v.co - Vector(wco)).length < 1e-5:
            v.select = True
            return True
    return False


def uv_of_vert(bm, uv_layer, face, local_co):
    for loop in face.loops:
        if (loop.vert.co - Vector(local_co)).length < 1e-5:
            return Vector(loop[uv_layer].uv)
    return None


def shoelace(face, uv_layer):
    """Signed UV area of the face loops in winding order (>0 = not mirrored)."""
    s = 0.0
    loops = face.loops[:]
    for i, loop in enumerate(loops):
        a = loop[uv_layer].uv
        b = loops[(i + 1) % len(loops)][uv_layer].uv
        s += a.x * b.y - b.x * a.y
    return s


def all_uvs_in_unit(bm, uv_layer, eps=1e-3):
    for f in bm.faces:
        for loop in f.loops:
            u, v = loop[uv_layer].uv
            if u < -eps or u > 1 + eps or v < -eps or v > 1 + eps:
                return False
    return True


def settings():
    return bpy.context.scene.agr_uv_settings


def reset_settings():
    s = settings()
    s.has_grid = False
    s.grid_source = 'EDGES'
    s.projection = 'PLANAR'
    s.selection_mode = 'ALL'
    s.auto_orient = True
    s.swap_axes = s.flip_u = s.flip_v = False
    s.snap_tolerance = 0.005
    s.world_cell_u = s.world_cell_v = 1.0
    s.world_angle = 0.0
    s.origin_mode = 'SELECTION'
    s.offset_u = s.offset_v = 0.0
    return s


try:
    print("=" * 60)
    print("TEST 1: capture from 2 edges on a floor grid + unwrap ALL")
    obj = make_grid_object("Floor", 4, 4, cell=1.0)
    bm = enter_edit(obj)
    deselect_all(bm)
    ok1 = select_edge_between(bm, obj, (0, 0, 0), (1, 0, 0))
    ok2 = select_edge_between(bm, obj, (0, 0, 0), (0, 1, 0))
    check("test edges found", ok1 and ok2)
    reset_settings()

    r = bpy.ops.agr.uv_grid_capture()
    s = settings()
    check("capture FINISHED", r == {'FINISHED'})
    check("has_grid", s.has_grid)
    check("cell 1x1", abs(s.cell_u - 1.0) < 1e-5 and abs(s.cell_v - 1.0) < 1e-5,
          f"cell={s.cell_u:.4f}x{s.cell_v:.4f}")

    r = bpy.ops.agr.uv_grid_unwrap()
    check("unwrap FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(obj.data)
    uv_layer = bm.loops.layers.uv.verify()
    check("all UVs in 0..1", all_uvs_in_unit(bm, uv_layer))
    # face (0,0)..(1,1): vert (1,0) must land at uv (1,0) — U=+X, V=+Y
    f0 = next(f for f in bm.faces
              if (f.calc_center_median() - Vector((0.5, 0.5, 0))).length < 1e-5)
    uv10 = uv_of_vert(bm, uv_layer, f0, (1, 0, 0))
    uv01 = uv_of_vert(bm, uv_layer, f0, (0, 1, 0))
    check("U along +X", uv10 is not None and (uv10 - Vector((1, 0))).length < 1e-4,
          f"uv={tuple(uv10) if uv10 else None}")
    check("V along +Y", uv01 is not None and (uv01 - Vector((0, 1))).length < 1e-4,
          f"uv={tuple(uv01) if uv01 else None}")
    check("not mirrored (shoelace>0)", all(shoelace(f, uv_layer) > 0 for f in bm.faces))

    print("=" * 60)
    print("TEST 2: auto-orient fixes a flipped stored basis")
    s.u_dir = (-1.0, 0.0, 0.0)   # simulate a capture with reversed U
    s.v_dir = (0.0, -1.0, 0.0)   # ... and reversed V
    r = bpy.ops.agr.uv_grid_unwrap()
    check("unwrap FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(obj.data)
    uv_layer = bm.loops.layers.uv.verify()
    f0 = next(f for f in bm.faces
              if (f.calc_center_median() - Vector((0.5, 0.5, 0))).length < 1e-5)
    uv10 = uv_of_vert(bm, uv_layer, f0, (1, 0, 0))
    check("auto-orient restored +X/+Y", uv10 is not None and (uv10 - Vector((1, 0))).length < 1e-4,
          f"uv={tuple(uv10) if uv10 else None}")
    check("not mirrored after flip fix", all(shoelace(f, uv_layer) > 0 for f in bm.faces))
    # left-handed basis (swapped axes) must also become right-handed
    s.u_dir = (0.0, 1.0, 0.0)
    s.v_dir = (1.0, 0.0, 0.0)
    bpy.ops.agr.uv_grid_unwrap()
    bm = bmesh.from_edit_mesh(obj.data)
    uv_layer = bm.loops.layers.uv.verify()
    check("left-handed basis fixed", all(shoelace(f, uv_layer) > 0 for f in bm.faces))
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 3: wall (XZ plane) — V must go up (world +Z)")
    wall = make_grid_object("Wall", 4, 3, cell=1.0, plane="XZ")
    bm = enter_edit(wall)
    deselect_all(bm)
    select_edge_between(bm, wall, (0, 0, 0), (1, 0, 0))
    select_edge_between(bm, wall, (0, 0, 0), (0, 0, 1))
    reset_settings()
    r = bpy.ops.agr.uv_grid_capture()
    check("wall capture FINISHED", r == {'FINISHED'})
    r = bpy.ops.agr.uv_grid_unwrap()
    check("wall unwrap FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(wall.data)
    uv_layer = bm.loops.layers.uv.verify()
    fw = next(f for f in bm.faces
              if (f.calc_center_median() - Vector((0.5, 0, 0.5))).length < 1e-5)
    uv_low = uv_of_vert(bm, uv_layer, fw, (0, 0, 0))
    uv_high = uv_of_vert(bm, uv_layer, fw, (0, 0, 1))
    check("V grows with world Z", uv_low is not None and uv_high is not None
          and uv_high.y > uv_low.y + 0.5,
          f"low={tuple(uv_low) if uv_low else None} high={tuple(uv_high) if uv_high else None}")
    check("wall not mirrored", all(shoelace(f, uv_layer) > 0 for f in bm.faces))
    check("wall UVs in 0..1", all_uvs_in_unit(bm, uv_layer))
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 4: cut a single big quad 4x4 into 16 cells (stored world grid)")
    big = make_grid_object("Big", 1, 1, cell=4.0)
    bm = enter_edit(big)
    reset_settings()
    s = settings()
    s.has_grid = True
    s.origin = (0, 0, 0)
    s.u_dir = (1, 0, 0)
    s.v_dir = (0, 1, 0)
    s.cell_u = s.cell_v = 1.0
    r = bpy.ops.agr.uv_grid_cut()
    check("cut FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(big.data)
    check("16 faces after cut", len(bm.faces) == 16, f"faces={len(bm.faces)}")
    r = bpy.ops.agr.uv_grid_unwrap()
    check("unwrap after cut FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(big.data)
    uv_layer = bm.loops.layers.uv.verify()
    check("all cell UVs in 0..1", all_uvs_in_unit(bm, uv_layer))
    check("cells not mirrored", all(shoelace(f, uv_layer) > 0 for f in bm.faces))
    # every UV corner must sit exactly on 0/1 (snap + exact bisect)
    exact = all(abs(c - round(c)) < 1e-4
                for f in bm.faces for loop in f.loops for c in loop[uv_layer].uv)
    check("UV corners exactly 0/1", exact)
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 5: rotated + non-uniformly scaled object, WORLD-space cut")
    mat = (Matrix.Translation((7, -3, 2))
           @ Euler((0, 0, radians(30))).to_matrix().to_4x4()
           @ Matrix.Diagonal((2, 1, 1, 1)))
    rot = make_grid_object("RotScaled", 1, 1, cell=4.0, matrix=mat)
    bm = enter_edit(rot)
    reset_settings()
    s = settings()
    s.has_grid = True
    m3 = mat.to_3x3()
    ux = m3 @ Vector((1, 0, 0))   # local X in world: length 2
    vy = m3 @ Vector((0, 1, 0))   # local Y in world: length 1
    s.origin = mat @ Vector((0, 0, 0))
    s.u_dir = ux.normalized()
    s.v_dir = vy.normalized()
    s.cell_u = ux.length          # world cell = one local meter of X
    s.cell_v = vy.length
    r = bpy.ops.agr.uv_grid_cut()
    check("cut FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(rot.data)
    check("16 faces after transform cut", len(bm.faces) == 16, f"faces={len(bm.faces)}")
    r = bpy.ops.agr.uv_grid_unwrap()
    bm = bmesh.from_edit_mesh(rot.data)
    uv_layer = bm.loops.layers.uv.verify()
    check("transformed UVs in 0..1", all_uvs_in_unit(bm, uv_layer))
    check("transformed not mirrored", all(shoelace(f, uv_layer) > 0 for f in bm.faces))
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 6: WORLD grid source (no edges at all) + selection corner origin")
    wobj = make_grid_object("WorldSrc", 1, 1, cell=3.0,
                            matrix=Matrix.Translation((10.37, 5.21, 0)))
    bm = enter_edit(wobj)
    reset_settings()
    s = settings()
    s.grid_source = 'WORLD'
    s.world_cell_u = s.world_cell_v = 1.0
    s.origin_mode = 'SELECTION'
    r = bpy.ops.agr.uv_grid_cut_unwrap()
    check("world cut+unwrap FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(wobj.data)
    uv_layer = bm.loops.layers.uv.verify()
    check("9 faces (3x3)", len(bm.faces) == 9, f"faces={len(bm.faces)}")
    check("world-grid UVs in 0..1", all_uvs_in_unit(bm, uv_layer))
    check("world-grid not mirrored", all(shoelace(f, uv_layer) > 0 for f in bm.faces))
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 7: WORLD grid at 45° — diagonal cut")
    dobj = make_grid_object("Diag", 1, 1, cell=2.0)
    bm = enter_edit(dobj)
    reset_settings()
    s = settings()
    s.grid_source = 'WORLD'
    s.world_cell_u = s.world_cell_v = 1.0
    s.world_angle = radians(45)
    s.origin_mode = 'SELECTION'
    r = bpy.ops.agr.uv_grid_cut()
    check("diagonal cut FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(dobj.data)
    check("diagonal cut created faces", len(bm.faces) > 4, f"faces={len(bm.faces)}")
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 8: SELECTED faces mode + oversize warning + snap")
    sel = make_grid_object("SelGrid", 4, 4, cell=1.0)
    # nudge one vert by 2mm to exercise snapping (tol 0.005 of 1m = 5mm)
    sel.data.vertices[6].co.x += 0.002  # vert (1,1) of the 5x5 lattice
    bm = enter_edit(sel)
    deselect_all(bm)
    for f in bm.faces:
        c = f.calc_center_median()
        if c.x < 2.0 and c.y < 2.0:   # 2x2 face block
            f.select = True
    bm.select_flush(True)
    reset_settings()
    s = settings()
    s.selection_mode = 'SELECTED'
    s.has_grid = True
    s.origin = (0, 0, 0)
    s.u_dir = (1, 0, 0)
    s.v_dir = (0, 1, 0)
    s.cell_u = s.cell_v = 1.0
    r = bpy.ops.agr.uv_grid_unwrap()
    check("selected unwrap FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(sel.data)
    uv_layer = bm.loops.layers.uv.verify()
    sel_faces = [f for f in bm.faces if f.select]
    check("only 4 faces processed", len(sel_faces) == 4)
    # scope check: the fresh uv layer starts at (0,0) — unselected faces
    # must STAY there, otherwise SELECTED mode silently processed them
    untouched = all(loop[uv_layer].uv.length < 1e-9
                    for f in bm.faces if not f.select for loop in f.loops)
    check("unselected faces untouched (uv stays 0,0)", untouched)
    snapped = all(abs(c - round(c)) < 1e-5
                  for f in sel_faces for loop in f.loops for c in loop[uv_layer].uv)
    check("2mm deviation snapped to grid", snapped)
    # oversize warning path: 2x2m face vs 1m grid
    bpy.ops.object.mode_set(mode='OBJECT')
    over = make_grid_object("Oversize", 2, 2, cell=2.0)
    bm = enter_edit(over)
    reset_settings()
    s = settings()
    s.has_grid = True
    s.origin = (0, 0, 0)
    s.u_dir = (1, 0, 0)
    s.v_dir = (0, 1, 0)
    s.cell_u = s.cell_v = 1.0
    r = bpy.ops.agr.uv_grid_unwrap()
    check("oversize unwrap still FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(over.data)
    uv_layer = bm.loops.layers.uv.verify()
    check("oversize UVs exceed unit (warning case)", not all_uvs_in_unit(bm, uv_layer))
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 9: error paths")
    err = make_grid_object("Err", 2, 2, cell=1.0)
    bm = enter_edit(err)
    deselect_all(bm)
    reset_settings()
    check("capture with 0 edges CANCELLED",
          expect_cancel(bpy.ops.agr.uv_grid_capture))
    check("unwrap without grid CANCELLED",
          expect_cancel(bpy.ops.agr.uv_grid_unwrap))
    # parallel edges
    deselect_all(bm)
    select_edge_between(bm, err, (0, 0, 0), (1, 0, 0))
    select_edge_between(bm, err, (0, 1, 0), (1, 1, 0))
    check("parallel edges CANCELLED",
          expect_cancel(bpy.ops.agr.uv_grid_capture))
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 10: diagonal edge capture -> cell_v is the projection")
    dia = make_grid_object("DiaGrid", 3, 3, cell=1.0)
    bm = enter_edit(dia)
    deselect_all(bm)
    select_edge_between(bm, dia, (0, 0, 0), (1, 0, 0))     # bottom edge
    # simulate a triangulation diagonal of the first cell
    v_a = next(v for v in bm.verts if (v.co - Vector((0, 0, 0))).length < 1e-6)
    v_b = next(v for v in bm.verts if (v.co - Vector((1, 1, 0))).length < 1e-6)
    diag = bm.edges.new((v_a, v_b))
    diag.select = True
    reset_settings()
    r = bpy.ops.agr.uv_grid_capture()
    s = settings()
    check("diagonal capture ran", r == {'FINISHED'})
    check("cell_v = projection (1.0, not 1.414)", abs(s.cell_v - 1.0) < 1e-4,
          f"cell_v={s.cell_v:.4f}")
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 11: agr_atlas_applied cleared on FULL unwrap, kept on partial")
    at = make_grid_object("AtlasObj", 2, 2, cell=1.0)
    at['agr_atlas_applied'] = True
    bm = enter_edit(at)
    deselect_all(bm)
    # partial selection first: flag must survive
    for f in bm.faces:
        if f.calc_center_median().x < 1.0 and f.calc_center_median().y < 1.0:
            f.select = True
            break
    bm.select_flush(True)
    reset_settings()
    s = settings()
    s.selection_mode = 'SELECTED'
    s.has_grid = True
    s.origin = (0, 0, 0)
    s.u_dir = (1, 0, 0)
    s.v_dir = (0, 1, 0)
    s.cell_u = s.cell_v = 1.0
    bpy.ops.agr.uv_grid_unwrap()
    check("flag kept on partial unwrap", at.get('agr_atlas_applied') is not None)
    s.selection_mode = 'ALL'
    bpy.ops.agr.uv_grid_unwrap()
    check("flag cleared on full unwrap", at.get('agr_atlas_applied') is None)
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 12: auto-capture with SELECTED mode finishes with capture only")
    ac = make_grid_object("AutoCap", 3, 3, cell=1.0)
    bm = enter_edit(ac)
    deselect_all(bm)
    select_edge_between(bm, ac, (0, 0, 0), (1, 0, 0))
    select_edge_between(bm, ac, (0, 0, 0), (0, 1, 0))
    reset_settings()
    s = settings()
    s.selection_mode = 'SELECTED'   # default UX path from the tooltip
    r = bpy.ops.agr.uv_grid_unwrap()
    check("auto-capture returns FINISHED (not ERROR)", r == {'FINISHED'})
    check("grid captured on the fly", s.has_grid and abs(s.cell_u - 1.0) < 1e-5)
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 13: tiny cell typo -> fast skip, CANCELLED, no hang")
    import time
    ty = make_grid_object("TinyCell", 1, 1, cell=4.0)
    bm = enter_edit(ty)
    reset_settings()
    s = settings()
    s.grid_source = 'WORLD'
    s.world_cell_u = s.world_cell_v = 0.001   # 4000 lines per axis
    t0 = time.time()
    cancelled = expect_cancel(bpy.ops.agr.uv_grid_cut)
    dt = time.time() - t0
    check("oversized line count CANCELLED", cancelled)
    check("skip is fast (<2s)", dt < 2.0, f"dt={dt:.2f}s")
    bm = bmesh.from_edit_mesh(ty.data)
    check("mesh untouched", len(bm.faces) == 1)
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 14: UDIM object detected for the warning path")
    ud = make_grid_object("UdimObj", 2, 2, cell=1.0)
    mtl = bpy.data.materials.new("M_UdimTest")
    mtl.use_nodes = True
    img = bpy.data.images.new("T_UdimTest", 64, 64, tiled=True)
    tex = mtl.node_tree.nodes.new('ShaderNodeTexImage')
    tex.image = img
    ud.data.materials.append(mtl)
    from AGR_tools.operators_udim import object_has_udim, invalidate_udim_cache
    invalidate_udim_cache()
    check("object_has_udim sees the tiled image", object_has_udim(ud))
    bm = enter_edit(ud)
    reset_settings()
    s = settings()
    s.has_grid = True
    s.origin = (0, 0, 0)
    s.u_dir = (1, 0, 0)
    s.v_dir = (0, 1, 0)
    s.cell_u = s.cell_v = 1.0
    r = bpy.ops.agr.uv_grid_unwrap()   # must WARN (not fail) about UDIM
    check("unwrap on UDIM object still FINISHED", r == {'FINISHED'})
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 15: rerun after undo must not crash with ReferenceError")
    un = make_grid_object("UndoObj", 3, 3, cell=1.0)
    bm = enter_edit(un)
    deselect_all(bm)
    select_edge_between(bm, un, (0, 0, 0), (1, 0, 0))
    select_edge_between(bm, un, (0, 0, 0), (0, 1, 0))
    reset_settings()
    s = settings()
    s.selection_mode = 'ALL'
    r = bpy.ops.agr.uv_grid_cut_unwrap()   # auto-capture + destructive cut
    check("first cut_unwrap FINISHED", r == {'FINISHED'})
    undo_ok = True
    try:
        bpy.ops.ed.undo_push(message="test")
        bpy.ops.ed.undo()
    except RuntimeError as exc:
        undo_ok = False
        print(f"  [SKIP] undo unavailable in background: {exc}")
    if undo_ok:
        # settings may be restored by undo -> force the capture path again
        s = settings()
        s.has_grid = False
        try:
            r = bpy.ops.agr.uv_grid_cut_unwrap()
            crashed = False
        except RuntimeError as exc:
            # a clean CANCELLED+report is fine, an embedded traceback is not
            crashed = "Traceback" in str(exc)
        except ReferenceError:
            crashed = True
        check("no ReferenceError crash after undo", not crashed)

    print("=" * 60)
    print("TEST 16: grid overlay compute + handler lifecycle")
    ov = make_grid_object("OverlayObj", 1, 1, cell=4.0)
    bm = enter_edit(ov)
    reset_settings()
    s = settings()
    s.selection_mode = 'ALL'
    s.has_grid = True
    s.origin = (0, 0, 0)
    s.u_dir = (1, 0, 0)
    s.v_dir = (0, 1, 0)
    s.cell_u = s.cell_v = 1.0

    wm = bpy.context.window_manager
    wm.agr_uv_grid_show = True
    check("3D handler added", uvmod._uv_handle_3d is not None)
    check("depsgraph handler added",
          uvmod._uv_overlay_depsgraph in bpy.app.handlers.depsgraph_update_post)

    data = uvmod._uv_get_overlay_data(bpy.context)
    check("overlay data built", data is not None)
    if data:
        # 4x4 quad on a 1m grid: 3 u-lines + 3 v-lines, one segment each
        check("6 cut segments", len(data["cut_pts"]) == 12,
              f"pts={len(data['cut_pts'])}")
        check("lattice present", len(data["lattice_pts"]) > 0)
        check("axes present", len(data["axis_u_pts"]) == 6 and len(data["axis_v_pts"]) == 6)
        # cut segments must be lifted off the surface (z > 0 for a floor)
        check("segments lifted off the face", all(p[2] > 0 for p in data["cut_pts"]))
        check("stats exposed", uvmod._uv_last_stats is not None
              and uvmod._uv_last_stats.get("cuts") == 6)

    fp1 = uvmod._uv_overlay_fingerprint(bpy.context, s)
    s.world_angle = 0.5
    fp2 = uvmod._uv_overlay_fingerprint(bpy.context, s)
    check("fingerprint reacts to angle", fp1 != fp2)
    s.world_angle = 0.0
    # selection change must alter the fingerprint in SELECTED mode
    s.selection_mode = 'SELECTED'
    bm = bmesh.from_edit_mesh(ov.data)
    deselect_all(bm)
    fp3 = uvmod._uv_overlay_fingerprint(bpy.context, s)
    for f in bm.faces:
        f.select = True
    fp4 = uvmod._uv_overlay_fingerprint(bpy.context, s)
    check("fingerprint reacts to selection", fp3 != fp4)

    wm.agr_uv_grid_show = False
    check("3D handler removed", uvmod._uv_handle_3d is None)
    check("depsgraph handler removed",
          uvmod._uv_overlay_depsgraph not in bpy.app.handlers.depsgraph_update_post)
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 17: SURFACE projection on a curved facade (quarter cylinder)")
    from math import pi, sin, cos

    def make_arc_wall(name, segments=8, radius=2.0, height=1.0, arc=pi / 2):
        mesh = bpy.data.meshes.new(name)
        verts = []
        for i in range(segments + 1):
            a = arc * i / segments
            x, y = radius * cos(a), radius * sin(a)
            verts += [(x, y, 0.0), (x, y, height)]
        faces = [(i * 2, i * 2 + 2, i * 2 + 3, i * 2 + 1) for i in range(segments)]
        mesh.from_pydata(verts, [], faces)
        mesh.validate()
        obj = bpy.data.objects.new(name, mesh)
        bpy.context.collection.objects.link(obj)
        return obj

    SEG, R, H = 8, 2.0, 1.0
    chord = 2 * R * sin((pi / 2) / SEG / 2)   # flat-quad width along the arc
    arcw = make_arc_wall("ArcWall", SEG, R, H)
    bm = enter_edit(arcw)
    reset_settings()
    s = settings()
    s.selection_mode = 'ALL'
    s.grid_source = 'WORLD'
    s.origin_mode = 'SELECTION'
    s.world_cell_u = chord
    s.world_cell_v = H

    # PLANAR baseline: one projection plane compresses the far quads —
    # position-faithful mapping honestly shows that (use SURFACE for arcs)
    s.projection = 'PLANAR'
    bpy.ops.agr.uv_grid_unwrap()
    bm = bmesh.from_edit_mesh(arcw.data)
    uv_layer = bm.loops.layers.uv.verify()
    def all_uv_integers(eps=1e-3):
        return all(abs(c - round(c)) < eps
                   for f in bm.faces for loop in f.loops for c in loop[uv_layer].uv)
    check("PLANAR compresses cells on the arc (не 0/1)", not all_uv_integers())

    # SURFACE: arc-length U -> every quad is exactly one cell
    s.projection = 'SURFACE'
    r = bpy.ops.agr.uv_grid_unwrap()
    check("surface unwrap FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(arcw.data)
    uv_layer = bm.loops.layers.uv.verify()
    check("SURFACE: every quad = exactly one cell (uv corners 0/1)",
          all_uv_integers(), )
    check("SURFACE: all UVs in 0..1", all_uvs_in_unit(bm, uv_layer))
    # V must map height 0..1
    fw = list(bm.faces)[0]
    vs_uv = sorted(loop[uv_layer].uv.y for loop in fw.loops)
    check("SURFACE: V spans 0..1 per quad",
          abs(vs_uv[0]) < 1e-3 and abs(vs_uv[-1] - 1.0) < 1e-3)

    # SURFACE cut: half-cell -> each quad bisected along its own plane
    s.world_cell_u = chord / 2
    r = bpy.ops.agr.uv_grid_cut()
    check("surface cut FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(arcw.data)
    check("each quad split in two", len(bm.faces) == SEG * 2,
          f"faces={len(bm.faces)}")
    r = bpy.ops.agr.uv_grid_unwrap()
    bm = bmesh.from_edit_mesh(arcw.data)
    uv_layer = bm.loops.layers.uv.verify()
    check("after surface cut: all UVs in 0..1", all_uvs_in_unit(bm, uv_layer))
    check("after surface cut: uv corners 0/1", all_uv_integers())

    # overlay in SURFACE mode: per-face frames, no flat lattice
    wm = bpy.context.window_manager
    # quarter-chord cells: one interior U line inside every half-quad
    # (full-chord lines would coincide with the existing edges -> 0 segments)
    s.world_cell_u = chord / 4
    wm.agr_uv_grid_show = True
    uvmod._uv_overlay_cache["fp"] = None
    uvmod._uv_overlay_cache["next_check"] = 0.0
    data = uvmod._uv_get_overlay_data(bpy.context)
    check("surface overlay data built", data is not None)
    if data:
        check("surface overlay: no flat lattice", len(data["lattice_pts"]) == 0)
        check("surface overlay: axes drawn", len(data["axis_u_pts"]) == 6)
        check("surface overlay: cut segments exist", len(data["cut_pts"]) > 0,
              f"segments={len(data['cut_pts']) // 2}")
    wm.agr_uv_grid_show = False
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 18: EDGES + SURFACE cut→unwrap phase stays anchored on the arc")
    an = make_arc_wall("ArcAnchored", SEG, R, H)
    bm = enter_edit(an)
    reset_settings()
    s = settings()
    s.selection_mode = 'ALL'
    s.grid_source = 'EDGES'
    s.projection = 'SURFACE'
    s.has_grid = True
    # stored grid captured at the FIRST arc corner: origin on the vert,
    # U along the first chord, half-chord cells force real cuts
    a1 = (pi / 2) / SEG
    first_dir = Vector((R * cos(a1) - R, R * sin(a1), 0.0)).normalized()
    s.origin = (R, 0.0, 0.0)
    s.u_dir = first_dir
    s.v_dir = (0.0, 0.0, 1.0)
    s.cell_u = chord / 2
    s.cell_v = H
    r = bpy.ops.agr.uv_grid_cut_unwrap()
    check("anchored cut+unwrap FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(an.data)
    uv_layer = bm.loops.layers.uv.verify()
    check("16 half-cells", len(bm.faces) == SEG * 2, f"faces={len(bm.faces)}")
    # the frames are REBUILT between cut and unwrap — anchoring must keep
    # the phase, so every cut piece lands exactly in 0..1
    check("no out-of-unit UVs after rebuild (фаза не уплыла)",
          all_uvs_in_unit(bm, uv_layer))
    check("cut pieces land on 0/1 corners", all_uv_integers(1e-3))
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 19: pre-existing edge splits a cell — pieces keep their position")
    pt = make_grid_object("SplitCell", 1, 1, cell=1.0, plane="XZ")
    bm = enter_edit(pt)
    # split the single 1x1 cell by a pre-existing vertical edge at x=0.3
    bmesh.ops.bisect_plane(bm, geom=list(bm.verts) + list(bm.edges) + list(bm.faces),
                           plane_co=Vector((0.3, 0, 0)), plane_no=Vector((1, 0, 0)),
                           dist=1e-6)
    bmesh.update_edit_mesh(pt.data, loop_triangles=True, destructive=True)
    bm = bmesh.from_edit_mesh(pt.data)
    reset_settings()
    s = settings()
    s.selection_mode = 'ALL'
    s.has_grid = True
    s.origin = (0, 0, 0)
    s.u_dir = (1, 0, 0)
    s.v_dir = (0, 0, 1)
    s.cell_u = s.cell_v = 1.0
    bpy.ops.agr.uv_grid_unwrap()
    bm = bmesh.from_edit_mesh(pt.data)
    uv_layer = bm.loops.layers.uv.verify()
    us_all = sorted({round(loop[uv_layer].uv.x, 4) for f in bm.faces for loop in f.loops})
    check("pieces assemble the tile at their true positions (0, 0.3, 1)",
          us_all == [0.0, 0.3, 1.0], f"us={us_all}")
    # continuity: the shared border carries the same u from both pieces
    border_us = {round(loop[uv_layer].uv.x, 4)
                 for f in bm.faces for loop in f.loops
                 if abs(loop.vert.co.x - 0.3) < 1e-5}
    check("shared border is continuous (u=0.3 с обеих сторон)",
          border_us == {0.3}, f"border={border_us}")
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 20: orientation follows the live selection in «Весь меш» mode")
    ro = bpy.data.meshes.new("RoofWall")
    ro_verts = [(0, 0, 2), (10, 0, 2), (10, 10, 2), (0, 10, 2),   # huge roof
                (0, 0, 0), (2, 0, 0), (2, 0, 2), (0, 0, 2)]       # small wall (XZ)
    ro.from_pydata(ro_verts, [], [(0, 1, 2, 3), (4, 5, 6, 7)])
    ro.validate()
    row = bpy.data.objects.new("RoofWall", ro)
    bpy.context.collection.objects.link(row)
    bm = enter_edit(row)
    deselect_all(bm)
    wall_face = next(f for f in bm.faces if abs(f.calc_center_median().z - 1.0) < 0.1)
    wall_face.select = True
    bm.select_flush(True)
    reset_settings()
    s = settings()
    s.selection_mode = 'ALL'        # processes everything...
    s.grid_source = 'WORLD'
    s.world_cell_u = 2.0
    s.world_cell_v = 2.0
    r = bpy.ops.agr.uv_grid_unwrap()
    check("ALL-mode unwrap FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(row.data)
    uv_layer = bm.loops.layers.uv.verify()
    wall_face = next(f for f in bm.faces if abs(f.calc_center_median().z - 1.0) < 0.1)
    # ...but the WALL selection must set the orientation: V follows world Z
    uv_low = uv_of_vert(bm, uv_layer, wall_face, (0, 0, 0))
    uv_high = uv_of_vert(bm, uv_layer, wall_face, (0, 0, 2))
    check("wall selection drives orientation (V grows with Z)",
          uv_low is not None and uv_high is not None
          and uv_high.y > uv_low.y + 0.5,
          f"low={tuple(uv_low) if uv_low else None} high={tuple(uv_high) if uv_high else None}")
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 21: strict position — off-phase panel keeps its exact place")
    op_wall = make_grid_object("OffPhase", 1, 1, cell=1.0, plane="XZ")
    bm = enter_edit(op_wall)
    reset_settings()
    s = settings()
    s.selection_mode = 'ALL'
    s.has_grid = True
    # grid shifted by 0.4: the 1m panel spans grid coords 0.4..1.4
    s.origin = (-0.4, 0.0, 0.0)
    s.u_dir = (1, 0, 0)
    s.v_dir = (0, 0, 1)
    s.cell_u = s.cell_v = 1.0
    r = bpy.ops.agr.uv_grid_unwrap()
    check("off-phase unwrap FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(op_wall.data)
    uv_layer = bm.loops.layers.uv.verify()
    us_all = sorted({round(loop[uv_layer].uv.x, 4) for f in bm.faces for loop in f.loops})
    check("panel keeps its true grid position 0.4..1.4 (честно торчит)",
          us_all == [0.4, 1.4], f"us={us_all}")
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 22: imperfect panel keeps its exact position (strict, no stretch)")
    im = bpy.data.meshes.new("Imperfect")
    # panels 0.94 / 1.06 / 1.0 wide on a 1m grid: slightly off-size on purpose
    xs = [0.0, 0.94, 2.0, 3.0]
    iv = []
    for x in xs:
        iv += [(x, 0.0, 0.0), (x, 0.0, 1.0)]
    ifaces = [(i * 2, i * 2 + 2, i * 2 + 3, i * 2 + 1) for i in range(3)]
    im.from_pydata(iv, [], ifaces)
    im.validate()
    imo = bpy.data.objects.new("Imperfect", im)
    bpy.context.collection.objects.link(imo)
    bm = enter_edit(imo)
    reset_settings()
    s = settings()
    s.selection_mode = 'ALL'
    s.has_grid = True
    s.origin = (0, 0, 0)
    s.u_dir = (1, 0, 0)
    s.v_dir = (0, 0, 1)
    s.cell_u = s.cell_v = 1.0
    r = bpy.ops.agr.uv_grid_unwrap()
    check("imperfect unwrap FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(imo.data)
    uv_layer = bm.loops.layers.uv.verify()
    # strict position: the 0.94 panel maps to 0..0.94 exactly (no stretch)
    f094 = next(f for f in bm.faces
                if abs(f.calc_center_median().x - 0.47) < 1e-3)
    us_f = sorted({round(loop[uv_layer].uv.x, 4) for loop in f094.loops})
    check("0.94 panel keeps exact position 0..0.94", us_f == [0.0, 0.94],
          f"us={us_f}")
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 23: no mirroring across a >90° corner (SURFACE)")
    from math import radians as rad
    co = bpy.data.meshes.new("Corner")
    p0 = Vector((0, 0, 0))
    p1 = Vector((2, 0, 0))
    d2 = Vector((cos(rad(100)), sin(rad(100)), 0.0))   # 100° turn
    p2 = p1 + d2 * 2.0
    cv = []
    for p in (p0, p1, p2):
        cv += [(p.x, p.y, 0.0), (p.x, p.y, 2.0)]
    cfaces = [(0, 2, 3, 1), (2, 4, 5, 3)]
    co.from_pydata(cv, [], cfaces)
    co.validate()
    cobj = bpy.data.objects.new("Corner", co)
    bpy.context.collection.objects.link(cobj)
    bm = enter_edit(cobj)
    reset_settings()
    s = settings()
    s.selection_mode = 'ALL'
    s.grid_source = 'WORLD'
    s.projection = 'SURFACE'
    s.world_cell_u = 2.0
    s.world_cell_v = 2.0
    r = bpy.ops.agr.uv_grid_unwrap()
    check("corner unwrap FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(cobj.data)
    uv_layer = bm.loops.layers.uv.verify()
    # old sign-inheritance mirrored the wall past the corner (dot<0 coin flip)
    check("both walls not mirrored (shoelace>0)",
          all(shoelace(f, uv_layer) > 0 for f in bm.faces),
          f"shoelaces={[round(shoelace(f, uv_layer), 3) for f in bm.faces]}")
    check("corner walls fill tiles", all_uvs_in_unit(bm, uv_layer))
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 24: drifted cell fragments still assemble (не рвёт тайл на 2 части)")
    dfm = bpy.data.meshes.new("DriftFrag")
    # one 1m cell split into 0.6 + 0.4 fragments; grid drifted by 0.03
    # (bigger than snap 0.005, well within _STRETCH_TOL): the right
    # fragment now "crosses" the drifted line 1.0 by 0.03
    dv = []
    for x in (0.0, 0.6, 1.0):
        dv += [(x, 0.0, 0.0), (x, 0.0, 1.0)]
    dfaces = [(0, 2, 3, 1), (2, 4, 5, 3)]
    dfm.from_pydata(dv, [], dfaces)
    dfm.validate()
    dfo = bpy.data.objects.new("DriftFrag", dfm)
    bpy.context.collection.objects.link(dfo)
    bm = enter_edit(dfo)
    reset_settings()
    s = settings()
    s.selection_mode = 'ALL'
    s.has_grid = True
    s.origin = (0.03, 0.0, 0.03)   # drifted grid
    s.u_dir = (1, 0, 0)
    s.v_dir = (0, 0, 1)
    s.cell_u = s.cell_v = 1.0
    r = bpy.ops.agr.uv_grid_unwrap()
    check("drifted fragments unwrap FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(dfo.data)
    uv_layer = bm.loops.layers.uv.verify()
    # strict position: fragments keep exact grid coords (-0.03/0.57/0.97)
    # and, crucially, the shared border carries ONE value from both sides
    us_all = sorted({round(loop[uv_layer].uv.x, 3) for f in bm.faces for loop in f.loops})
    check("fragments keep exact positions (-0.03 / 0.57 / 0.97)",
          us_all == [-0.03, 0.57, 0.97], f"us={us_all}")
    border = {round(loop[uv_layer].uv.x, 4)
              for f in bm.faces for loop in f.loops
              if abs(loop.vert.co.x - 0.6) < 1e-5}
    check("shared border single-valued", len(border) == 1, f"border={border}")
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 25: closed building loop — no giant stretched seam faces")
    lm = bpy.data.meshes.new("LoopWall")
    corners = [Vector((0, 0, 0)), Vector((2, 0, 0)), Vector((2, 2, 0)), Vector((0, 2, 0))]
    ring = []
    for ci in range(4):
        a, b = corners[ci], corners[(ci + 1) % 4]
        n = int(round((b - a).length))          # 1m quads
        for k in range(n):
            ring.append(a + (b - a) * (k / n))
    # two rows + a doorway (one bottom quad removed): the mean normal is
    # non-zero (WORLD basis resolves) while the UPPER row stays a closed
    # ring -> the BFS walk wraps around and meets itself at a seam
    lv = []
    for p in ring:
        lv += [(p.x, p.y, 0.0), (p.x, p.y, 1.0), (p.x, p.y, 2.0)]
    m = len(ring)
    lfaces = []
    for i in range(m):
        j = (i + 1) % m
        for row in range(2):
            if i == 0 and row == 0:
                continue   # doorway
            lfaces.append((i * 3 + row, j * 3 + row,
                           j * 3 + row + 1, i * 3 + row + 1))
    lm.from_pydata(lv, [], lfaces)
    lm.validate()
    lobj = bpy.data.objects.new("LoopWall", lm)
    bpy.context.collection.objects.link(lobj)
    bm = enter_edit(lobj)
    reset_settings()
    s = settings()
    s.selection_mode = 'ALL'
    s.grid_source = 'WORLD'
    s.projection = 'SURFACE'
    s.world_cell_u = 1.0
    s.world_cell_v = 1.0
    r = bpy.ops.agr.uv_grid_unwrap()
    check("loop unwrap FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(lobj.data)
    uv_layer = bm.loops.layers.uv.verify()
    # old vertex-mean averaged u≈0 with u≈perimeter at the seam verts ->
    # bowtie faces stretched across many units
    max_span = max(
        max(loop[uv_layer].uv.x for loop in f.loops)
        - min(loop[uv_layer].uv.x for loop in f.loops)
        for f in bm.faces)
    check("no face spans more than ~1 tile", max_span < 1.05,
          f"max_span={max_span:.2f}")
    check("loop UVs inside the unit square", all_uvs_in_unit(bm, uv_layer, eps=1e-2))
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 26: grid offsets shift the grid in both projections + cut")
    for proj in ('PLANAR', 'SURFACE'):
        ofw = make_grid_object(f"Offset{proj}", 1, 1, cell=1.0, plane="XZ")
        bm = enter_edit(ofw)
        reset_settings()
        s = settings()
        s.selection_mode = 'ALL'
        s.projection = proj
        s.has_grid = True
        s.origin = (0, 0, 0)
        s.u_dir = (1, 0, 0)
        s.v_dir = (0, 0, 1)
        s.cell_u = s.cell_v = 1.0
        s.offset_u = 0.25
        r = bpy.ops.agr.uv_grid_unwrap()
        check(f"{proj}: offset unwrap FINISHED", r == {'FINISHED'})
        bm = bmesh.from_edit_mesh(ofw.data)
        uv_layer = bm.loops.layers.uv.verify()
        us_all = sorted({round(loop[uv_layer].uv.x, 4)
                         for f in bm.faces for loop in f.loops})
        check(f"{proj}: grid shifted by 0.25 (uv -0.25..0.75)",
              us_all == [-0.25, 0.75], f"us={us_all}")
        bpy.ops.object.mode_set(mode='OBJECT')
    # cut respects the offset: line lands at x = 0.5 with offset 0.5
    ofc = make_grid_object("OffsetCut", 1, 1, cell=1.0, plane="XZ")
    bm = enter_edit(ofc)
    reset_settings()
    s = settings()
    s.selection_mode = 'ALL'
    s.has_grid = True
    s.origin = (0, 0, 0)
    s.u_dir = (1, 0, 0)
    s.v_dir = (0, 0, 1)
    s.cell_u = s.cell_v = 1.0
    s.offset_u = 0.5
    r = bpy.ops.agr.uv_grid_cut()
    check("offset cut FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(ofc.data)
    check("cut line moved to x=0.5 (2 faces)", len(bm.faces) == 2
          and any(abs(v.co.x - 0.5) < 1e-5 for v in bm.verts),
          f"faces={len(bm.faces)}")
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 27: SURFACE flip_v must NOT flip U (and flip_u must)")
    fw = make_grid_object("FlipWall", 1, 1, cell=1.0, plane="XZ")
    bm = enter_edit(fw)
    reset_settings()
    s = settings()
    s.selection_mode = 'ALL'
    s.projection = 'SURFACE'
    s.has_grid = True
    s.origin = (0, 0, 0)
    s.u_dir = (1, 0, 0)
    s.v_dir = (0, 0, 1)
    s.cell_u = s.cell_v = 1.0
    s.flip_v = True
    r = bpy.ops.agr.uv_grid_unwrap()
    check("flip_v surface unwrap FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(fw.data)
    uv_layer = bm.loops.layers.uv.verify()
    f27 = list(bm.faces)[0]
    uv00 = uv_of_vert(bm, uv_layer, f27, (0, 0, 0))
    uv10 = uv_of_vert(bm, uv_layer, f27, (1, 0, 0))
    uv01 = uv_of_vert(bm, uv_layer, f27, (0, 0, 1))
    check("flip_v alone leaves U unchanged (u=0@x0, u=1@x1)",
          uv00 is not None and uv10 is not None
          and abs(uv00.x) < 1e-4 and abs(uv10.x - 1.0) < 1e-4,
          f"uv00={tuple(uv00) if uv00 else None} uv10={tuple(uv10) if uv10 else None}")
    check("flip_v mirrors V (v=1@z0, v=0@z1)",
          uv01 is not None and abs(uv00.y - 1.0) < 1e-4 and abs(uv01.y) < 1e-4,
          f"uv00={tuple(uv00) if uv00 else None} uv01={tuple(uv01) if uv01 else None}")
    s.flip_u = True   # both flips: U must now actually mirror
    bpy.ops.agr.uv_grid_unwrap()
    bm = bmesh.from_edit_mesh(fw.data)
    uv_layer = bm.loops.layers.uv.verify()
    f27 = list(bm.faces)[0]
    uv00 = uv_of_vert(bm, uv_layer, f27, (0, 0, 0))
    uv10 = uv_of_vert(bm, uv_layer, f27, (1, 0, 0))
    check("flip_u+flip_v mirrors U (u=1@x0, u=0@x1)",
          uv00 is not None and uv10 is not None
          and abs(uv00.x - 1.0) < 1e-4 and abs(uv10.x) < 1e-4,
          f"uv00={tuple(uv00) if uv00 else None} uv10={tuple(uv10) if uv10 else None}")
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 28: cut must not expand selection onto an enclosed window")
    en = make_grid_object("Enclosed", 3, 3, cell=1.0, plane="XZ")
    bm = enter_edit(en)
    for f in bm.faces:
        f.select = True
    win = next(f for f in bm.faces
               if (f.calc_center_median() - Vector((1.5, 0, 1.5))).length < 1e-5)
    win.select = False   # the "window" the user excluded
    # restore the vert/edge state Blender's own flush leaves after a click:
    # the window's verts/edges stay selected via the surrounding faces
    for f in bm.faces:
        if f.select:
            for v in f.verts:
                v.select = True
            for e in f.edges:
                e.select = True
    reset_settings()
    s = settings()
    s.selection_mode = 'SELECTED'
    s.has_grid = True
    s.origin = (0, 0, 0)
    s.u_dir = (1, 0, 0)
    s.v_dir = (0, 0, 1)
    s.cell_u = s.cell_v = 0.5
    r = bpy.ops.agr.uv_grid_cut()
    check("enclosed-window cut FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(en.data)
    win = next(f for f in bm.faces
               if (f.calc_center_median() - Vector((1.5, 0, 1.5))).length < 1e-5)
    check("window face still deselected after cut", not win.select)
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 29: multi-object cut cancels BEFORE mutating any mesh")
    oka = make_grid_object("PreScanA", 1, 1, cell=2.0)
    bad = make_grid_object("PreScanB", 1, 1, cell=2.0,
                           matrix=(Matrix.Translation((100, 0, 0))
                                   @ Matrix.Diagonal((3000, 3000, 1, 1))))
    bpy.ops.object.mode_set(mode='OBJECT')
    for o in bpy.context.selected_objects:
        o.select_set(False)
    oka.select_set(True)
    bad.select_set(True)
    bpy.context.view_layer.objects.active = oka
    bpy.ops.object.mode_set(mode='EDIT')   # multi-object edit
    reset_settings()
    s = settings()
    s.selection_mode = 'ALL'
    s.grid_source = 'WORLD'
    s.origin_mode = 'WORLD'
    s.world_cell_u = s.world_cell_v = 1.0   # B spans ~12000 lines -> guard
    check("multi-object guard CANCELLED", expect_cancel(bpy.ops.agr.uv_grid_cut))
    bm_a = bmesh.from_edit_mesh(oka.data)
    bm_b = bmesh.from_edit_mesh(bad.data)
    check("no object was mutated before the guard",
          len(bm_a.faces) == 1 and len(bm_b.faces) == 1,
          f"A={len(bm_a.faces)} B={len(bm_b.faces)}")
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 30: autofit capture — triangulated wall, noise, diagonals, trims")

    def make_tri_wall(name, nx, nz, cell=1.0, noise=0.0004, cell_z=None):
        """Triangulated XZ wall with deterministic ~1mm vertex noise.
        cell = X pitch, cell_z = Z pitch (defaults to cell — square cells)."""
        cz = cell if cell_z is None else cell_z
        mesh = bpy.data.meshes.new(name)
        verts = []
        for j in range(nz + 1):
            for i in range(nx + 1):
                dx = (((i * 37 + j * 17) % 7) - 3) * noise
                dz = (((i * 23 + j * 41) % 7) - 3) * noise
                verts.append((i * cell + dx, 0.0, j * cz + dz))
        faces = []
        for j in range(nz):
            for i in range(nx):
                k = j * (nx + 1) + i
                faces.append((k, k + 1, k + nx + 2))
                faces.append((k, k + nx + 2, k + nx + 1))
        mesh.from_pydata(verts, [], faces)
        mesh.validate()
        obj = bpy.data.objects.new(name, mesh)
        bpy.context.collection.objects.link(obj)
        return obj

    tw = make_tri_wall("AutoFit", 4, 3, cell=1.0)
    bm = enter_edit(tw)
    for e in bm.edges:
        e.select = True
    reset_settings()
    s = settings()
    r = bpy.ops.agr.uv_grid_capture()
    check("autofit capture FINISHED", r == {'FINISHED'})
    check("autofit cell ~1x1 despite noise and diagonals",
          abs(s.cell_u - 1.0) < 0.005 and abs(s.cell_v - 1.0) < 0.005,
          f"cell={s.cell_u:.4f}x{s.cell_v:.4f}")
    ud, vd = Vector(s.u_dir), Vector(s.v_dir)
    check("autofit axes along X and Z",
          abs(abs(ud.x) - 1.0) < 0.01 and abs(abs(vd.z) - 1.0) < 0.01,
          f"u={tuple(round(c, 3) for c in ud)} v={tuple(round(c, 3) for c in vd)}")
    check("autofit origin near the wall corner",
          (Vector(s.origin) - Vector((0, 0, 0))).length < 0.01,
          f"origin={tuple(round(c, 4) for c in s.origin)}")
    # end-to-end: snap (5mm default) absorbs the ~1mm noise -> exact tiles
    s.selection_mode = 'ALL'
    r = bpy.ops.agr.uv_grid_unwrap()
    check("autofit unwrap FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(tw.data)
    uv_layer = bm.loops.layers.uv.verify()
    check("autofit UVs in unit square (snap ate the noise)",
          all_uvs_in_unit(bm, uv_layer))
    # only diagonals selected -> no perpendicular families -> clean cancel
    bm = bmesh.from_edit_mesh(tw.data)
    deselect_all(bm)
    ndiag = 0
    for e in bm.edges:
        d = e.verts[1].co - e.verts[0].co
        if abs(d.x) > 0.5 and abs(d.z) > 0.5:  # cell diagonals
            e.select = True
            ndiag += 1
    check("selected only diagonals (3+)", ndiag >= 3, f"n={ndiag}")
    check("autofit with only diagonals CANCELLED",
          expect_cancel(bpy.ops.agr.uv_grid_capture))
    bpy.ops.object.mode_set(mode='OBJECT')
    # trimmed border cells: the median must ignore the 0.4m leftovers
    tm = bpy.data.meshes.new("TrimStrip")
    xs = [0.0, 1.0, 2.0, 3.0, 3.4]
    tv = []
    for x in xs:
        tv += [(x, 0.0, 0.0), (x, 0.0, 1.0)]
    tf = [(i * 2, i * 2 + 2, i * 2 + 3, i * 2 + 1) for i in range(4)]
    tm.from_pydata(tv, [], tf)
    tm.validate()
    tmo = bpy.data.objects.new("TrimStrip", tm)
    bpy.context.collection.objects.link(tmo)
    bm = enter_edit(tmo)
    for e in bm.edges:
        e.select = True
    reset_settings()
    s = settings()
    r = bpy.ops.agr.uv_grid_capture()
    check("trimmed-strip autofit FINISHED", r == {'FINISHED'})
    check("median ignores the 0.4m trims (cell = 1x1)",
          abs(s.cell_u - 1.0) < 1e-5 and abs(s.cell_v - 1.0) < 1e-5,
          f"cell={s.cell_u:.4f}x{s.cell_v:.4f}")
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 31: autofit angle re-centering — triangulated 3:1 cells")
    # Diagonals (18.4°) drag a plain circular-mean angle by ~8°; the ±15°
    # window anchored there keeps them, and with border edges excluded the
    # interior median lands on the diagonal: cell_u = √10 ≈ 3.162.  The
    # weighted-median re-centering must snap the axis back to the true 0°
    # so the window expels the diagonals on the next pass.
    t31 = make_tri_wall("AutoFit31", 3, 3, cell=3.0, cell_z=1.0)
    bm = enter_edit(t31)
    for e in bm.edges:
        e.select = True
    reset_settings()
    s = settings()
    r = bpy.ops.agr.uv_grid_capture()
    check("3:1 autofit capture FINISHED", r == {'FINISHED'})
    check("3:1 cell is 3x1 (diagonal √10 rejected)",
          abs(s.cell_u - 3.0) < 0.01 and abs(s.cell_v - 1.0) < 0.005,
          f"cell={s.cell_u:.4f}x{s.cell_v:.4f}")
    ud, vd = Vector(s.u_dir), Vector(s.v_dir)
    check("3:1 axes not tilted by diagonal votes",
          abs(abs(ud.x) - 1.0) < 0.001 and abs(abs(vd.z) - 1.0) < 0.001,
          f"u={tuple(round(c, 4) for c in ud)} v={tuple(round(c, 4) for c in vd)}")
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 32: autofit diagonal length signature — 4:1 cells")
    # A 4:1 diagonal is only 14° off the U axis — INSIDE the ±15° window,
    # no angle filter can reject it; only the hypot(cell_u, cell_v) length
    # test can (old result: cell_u = √17 ≈ 4.123, axis tilted ~6°)
    t32 = make_tri_wall("AutoFit32", 3, 3, cell=4.0, cell_z=1.0)
    bm = enter_edit(t32)
    for e in bm.edges:
        e.select = True
    reset_settings()
    s = settings()
    r = bpy.ops.agr.uv_grid_capture()
    check("4:1 autofit capture FINISHED", r == {'FINISHED'})
    check("4:1 cell is 4x1 (diagonal √17 rejected by length)",
          abs(s.cell_u - 4.0) < 0.01 and abs(s.cell_v - 1.0) < 0.005,
          f"cell={s.cell_u:.4f}x{s.cell_v:.4f}")
    ud, vd = Vector(s.u_dir), Vector(s.v_dir)
    check("4:1 axes not tilted by diagonal votes",
          abs(abs(ud.x) - 1.0) < 0.001 and abs(abs(vd.z) - 1.0) < 0.001,
          f"u={tuple(round(c, 4) for c in ud)} v={tuple(round(c, 4) for c in vd)}")
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 33: autofit coplanarity — wall with perpendicular returns")

    def make_finned_wall(name, nfins, depth, alternate):
        """3x3-cell XZ wall + perpendicular return strips (window reveals)
        at the first nfins vertical grid lines.  alternate=True flips every
        other fin so their area normals cancel (mean normal stays clean);
        False leaves one-sided fins that TILT the mean normal (refit path).
        Perpendicular depth edges are 0.625 m — with enough of them the old
        3D-length median voted cell_u = 0.625 instead of 1.0."""
        verts, faces, idx = [], [], {}

        def vid(p):
            if p not in idx:
                idx[p] = len(verts)
                verts.append(p)
            return idx[p]

        for j in range(3):
            for i in range(3):
                faces.append((vid((i, 0.0, j)), vid((i + 1, 0.0, j)),
                              vid((i + 1, 0.0, j + 1)), vid((i, 0.0, j + 1))))
        for f in range(nfins):
            flip = alternate and f % 2
            for j in range(3):
                quad = (vid((f, 0.0, j)), vid((f, -depth, j)),
                        vid((f, -depth, j + 1)), vid((f, 0.0, j + 1)))
                faces.append(tuple(reversed(quad)) if flip else quad)
        mesh = bpy.data.meshes.new(name)
        mesh.from_pydata(verts, [], faces)
        mesh.validate()
        obj = bpy.data.objects.new(name, mesh)
        bpy.context.collection.objects.link(obj)
        return obj

    fw33 = make_finned_wall("AutoFit33", 4, 0.625, alternate=True)
    bm = enter_edit(fw33)
    for e in bm.edges:
        e.select = True
    reset_settings()
    s = settings()
    r = bpy.ops.agr.uv_grid_capture()
    check("finned-wall autofit capture FINISHED", r == {'FINISHED'})
    check("perpendicular 0.625m edges dropped (cell = 1x1)",
          abs(s.cell_u - 1.0) < 0.005 and abs(s.cell_v - 1.0) < 0.005,
          f"cell={s.cell_u:.4f}x{s.cell_v:.4f}")
    ud, vd = Vector(s.u_dir), Vector(s.v_dir)
    check("finned-wall axes along X and Z",
          abs(abs(ud.x) - 1.0) < 0.001 and abs(abs(vd.z) - 1.0) < 0.001,
          f"u={tuple(round(c, 4) for c in ud)} v={tuple(round(c, 4) for c in vd)}")
    og = Vector(s.origin)
    check("finned-wall origin at the wall corner (in-plane)",
          abs(og.x) < 0.01 and abs(og.z) < 0.01,
          f"origin={tuple(round(c, 4) for c in og)}")
    bpy.ops.object.mode_set(mode='OBJECT')
    # one-sided return: the fin's face area TILTS the mean normal ~12°,
    # shrinking every projected length by ~2% — the refit against the
    # families' own plane normal must restore the exact 1.0 cell
    fw33b = make_finned_wall("AutoFit33b", 1, 0.625, alternate=False)
    bm = enter_edit(fw33b)
    for e in bm.edges:
        e.select = True
    reset_settings()
    s = settings()
    r = bpy.ops.agr.uv_grid_capture()
    check("one-sided return capture FINISHED", r == {'FINISHED'})
    check("refit restores exact cell despite tilted mean normal",
          abs(s.cell_u - 1.0) < 0.005 and abs(s.cell_v - 1.0) < 0.005,
          f"cell={s.cell_u:.4f}x{s.cell_v:.4f}")
    ud, vd = Vector(s.u_dir), Vector(s.v_dir)
    check("refit axes along X and Z",
          abs(abs(ud.x) - 1.0) < 0.001 and abs(abs(vd.z) - 1.0) < 0.001,
          f"u={tuple(round(c, 4) for c in ud)} v={tuple(round(c, 4) for c in vd)}")
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 34: autofit refuses when grid edges are a minority")
    # 26 unit spokes at 7° steps (each backed by a thin triangle for the
    # normal): directions are near-uniform in the plane, the two ±15°
    # windows can keep at most 10 of 26 edges (~38% of the weight) —
    # the capture must refuse instead of committing a garbage basis
    mm = bpy.data.meshes.new("AutoFitMess")
    mv, mf = [], []
    for k in range(26):
        ang = k * 7.0 * pi / 180.0
        c = Vector((k * 3.0, 0.0, 0.0))
        d = Vector((cos(ang), 0.0, sin(ang)))
        p = Vector((-d.z * 0.05, 0.0, d.x * 0.05))
        base = len(mv)
        mv += [tuple(c), tuple(c + d), tuple(c + p)]
        mf.append((base, base + 1, base + 2))
    mm.from_pydata(mv, [], mf)
    mm.validate()
    mo = bpy.data.objects.new("AutoFitMess", mm)
    bpy.context.collection.objects.link(mo)
    bm = enter_edit(mo)
    deselect_all(bm)
    nspokes = 0
    for e in bm.edges:
        if abs((e.verts[1].co - e.verts[0].co).length - 1.0) < 1e-4:
            e.select = True   # only the unit spokes, not the triangle backs
            nspokes += 1
    check("mess: 26 spokes selected", nspokes == 26, f"n={nspokes}")
    reset_settings()
    check("minority-grid autofit CANCELLED",
          expect_cancel(bpy.ops.agr.uv_grid_capture))
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 35: TOPZ (plan view) basis is nailed to world X/Y")
    # floor grid tilted 60 deg about X: |n.z| = 0.5 -> the WORLD source would
    # pick its WALL branch (V up the slope), so the two sources must disagree
    roof = make_grid_object("RoofTilt", 3, 3, cell=1.0,
                            matrix=Matrix.Rotation(radians(60), 4, 'X'))
    bm = enter_edit(roof)
    for f in bm.faces:
        f.select = True
    reset_settings()
    s = settings()
    s.selection_mode = 'ALL'
    s.grid_source = 'TOPZ'
    s.world_cell_u = s.world_cell_v = 1.0
    r = bpy.ops.agr.uv_grid_unwrap()
    check("TOPZ unwrap FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(roof.data)
    uv_layer = bm.loops.layers.uv.verify()
    check("TOPZ: all UVs in 0..1", all_uvs_in_unit(bm, uv_layer))
    f0 = next(f for f in bm.faces
              if (f.calc_center_median() - Vector((0.5, 0.5, 0))).length < 1e-5)
    uv10 = uv_of_vert(bm, uv_layer, f0, (1, 0, 0))
    uv01 = uv_of_vert(bm, uv_layer, f0, (0, 1, 0))
    check("TOPZ: U = world +X", uv10 is not None and (uv10 - Vector((1, 0))).length < 1e-4,
          f"uv={tuple(uv10) if uv10 else None}")
    # world Y of local (0,1,0) is cos(60) = 0.5 -> the slope is FORESHORTENED,
    # which is exactly what a top-down projection must do
    check("TOPZ: V = world +Y (slope foreshortened to 0.5)",
          uv01 is not None and (uv01 - Vector((0, 0.5))).length < 1e-4,
          f"uv={tuple(uv01) if uv01 else None}")
    # same geometry through the WORLD source measures along the slope -> 1.0
    s.grid_source = 'WORLD'
    s.origin_mode = 'WORLD'
    bpy.ops.agr.uv_grid_unwrap()
    bm = bmesh.from_edit_mesh(roof.data)
    uv_layer = bm.loops.layers.uv.verify()
    f0 = next(f for f in bm.faces
              if (f.calc_center_median() - Vector((0.5, 0.5, 0))).length < 1e-5)
    w01 = uv_of_vert(bm, uv_layer, f0, (0, 1, 0))
    check("WORLD source measures along the slope (V=1) - sources differ",
          w01 is not None and abs(w01.y - 1.0) < 1e-4,
          f"uv={tuple(w01) if w01 else None}")

    print("=" * 60)
    print("TEST 36: TOPZ ignores selection, auto-orient and origin_mode")
    s.grid_source = 'TOPZ'
    s.selection_mode = 'SELECTED'
    s.origin_mode = 'SELECTION'   # must be ignored: TOPZ anchors at world 0
    s.auto_orient = True          # must be ignored: normals are never read
    bm = bmesh.from_edit_mesh(roof.data)
    deselect_all(bm)
    # ONE far face, deliberately OFF the grid lines in V: local x 2..3,
    # y 1..2 -> world y 0.5..1.0.  The fractional corner is what makes the
    # check discriminating: with the origin nailed at world 0 the face lands
    # at v = 0.5, while snapping the origin to the selection corner (what
    # `!= 'EDGES'` instead of `== 'WORLD'` in _resolve_basis would do) would
    # zero it.  On an integer-aligned face BOTH branches give the same UVs,
    # so the guard _resolve_basis documents went untested.
    far = next(f for f in bm.faces
               if (f.calc_center_median() - Vector((2.5, 1.5, 0))).length < 1e-5)
    far.select = True
    r = bpy.ops.agr.uv_grid_unwrap()
    check("TOPZ single-face unwrap FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(roof.data)
    uv_layer = bm.loops.layers.uv.verify()
    far = next(f for f in bm.faces
               if (f.calc_center_median() - Vector((2.5, 1.5, 0))).length < 1e-5)
    uv21 = uv_of_vert(bm, uv_layer, far, (2, 1, 0))
    uv32 = uv_of_vert(bm, uv_layer, far, (3, 2, 0))
    # world (2, 0.5): u = 2 - floor(2.5) = 0, v = 0.5 - floor(0.75) = 0.5
    check("TOPZ: origin stays at world 0 despite origin_mode=SELECTION",
          uv21 is not None and (uv21 - Vector((0, 0.5))).length < 1e-4,
          f"uv={tuple(uv21) if uv21 else None}")
    # world (3, 1.0) -> (1, 1.0): the slope stays foreshortened by cos(60)
    check("TOPZ: far face keeps plan-view scale",
          uv32 is not None and (uv32 - Vector((1, 1.0))).length < 1e-4,
          f"uv={tuple(uv32) if uv32 else None}")
    bpy.ops.object.mode_set(mode='OBJECT')

    # normals pointing DOWN: auto-orient would mirror U for the other
    # sources; TOPZ must keep U = +X and honestly report the mirroring
    flip = make_grid_object("FlipFloor", 2, 2, cell=1.0)
    bm = enter_edit(flip)
    for f in bm.faces:
        f.select = True
    bpy.ops.mesh.flip_normals()
    reset_settings()
    s = settings()
    s.selection_mode = 'ALL'
    s.grid_source = 'TOPZ'
    bpy.ops.agr.uv_grid_unwrap()
    bm = bmesh.from_edit_mesh(flip.data)
    uv_layer = bm.loops.layers.uv.verify()
    f0 = next(f for f in bm.faces
              if (f.calc_center_median() - Vector((0.5, 0.5, 0))).length < 1e-5)
    uv10 = uv_of_vert(bm, uv_layer, f0, (1, 0, 0))
    check("TOPZ: inverted normals do NOT rotate the plan grid",
          uv10 is not None and (uv10 - Vector((1, 0))).length < 1e-4,
          f"uv={tuple(uv10) if uv10 else None}")
    check("TOPZ: mirroring is reported honestly, not compensated",
          shoelace(f0, uv_layer) < 0, f"shoelace={shoelace(f0, uv_layer):.4f}")
    bpy.ops.object.mode_set(mode='OBJECT')

    # swap_axes flips the basis handedness, and under TOPZ the auto-orient
    # that compensates it is structurally OFF - the whole unwrap silently
    # came out MIRRORED (shoelace flipped sign on every face).  TOPZ must
    # ignore the swap: the sanctioned plan-grid rotation is world_angle.
    upfloor = make_grid_object("SwapFloor", 2, 2, cell=1.0)
    bm = enter_edit(upfloor)
    for f in bm.faces:
        f.select = True
    reset_settings()
    s = settings()
    s.selection_mode = 'ALL'
    s.grid_source = 'TOPZ'
    s.swap_axes = True
    bpy.ops.agr.uv_grid_unwrap()
    bm = bmesh.from_edit_mesh(upfloor.data)
    uv_layer = bm.loops.layers.uv.verify()
    check("TOPZ: swap_axes is ignored - nothing is mirrored",
          all(shoelace(f, uv_layer) > 0 for f in bm.faces),
          f"shoelaces={[round(shoelace(f, uv_layer), 3) for f in bm.faces]}")
    f0 = next(f for f in bm.faces
              if (f.calc_center_median() - Vector((0.5, 0.5, 0))).length < 1e-5)
    uv10 = uv_of_vert(bm, uv_layer, f0, (1, 0, 0))
    check("TOPZ: with swap requested U still equals world +X",
          uv10 is not None and (uv10 - Vector((1, 0))).length < 1e-4,
          f"uv={tuple(uv10) if uv10 else None}")
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 37: TOPZ world_angle + offsets, SURFACE forced off, cut")
    floor2 = make_grid_object("PlanFloor", 2, 2, cell=1.0)
    bm = enter_edit(floor2)
    for f in bm.faces:
        f.select = True
    reset_settings()
    s = settings()
    s.selection_mode = 'ALL'
    s.grid_source = 'TOPZ'
    s.world_angle = radians(90)   # U -> +Y, V -> -X
    bpy.ops.agr.uv_grid_unwrap()
    bm = bmesh.from_edit_mesh(floor2.data)
    uv_layer = bm.loops.layers.uv.verify()
    f0 = next(f for f in bm.faces
              if (f.calc_center_median() - Vector((0.5, 0.5, 0))).length < 1e-5)
    a10 = uv_of_vert(bm, uv_layer, f0, (1, 0, 0))
    a01 = uv_of_vert(bm, uv_layer, f0, (0, 1, 0))
    check("TOPZ +90 deg: U follows world +Y",
          a01 is not None and (a01 - Vector((1, 1))).length < 1e-4,
          f"uv(0,1)={tuple(a01) if a01 else None}")
    check("TOPZ +90 deg: V follows world -X",
          a10 is not None and (a10 - Vector((0, 0))).length < 1e-4,
          f"uv(1,0)={tuple(a10) if a10 else None}")
    s.world_angle = 0.0
    s.offset_u = 0.25
    bpy.ops.agr.uv_grid_unwrap()
    bm = bmesh.from_edit_mesh(floor2.data)
    uv_layer = bm.loops.layers.uv.verify()
    f0 = next(f for f in bm.faces
              if (f.calc_center_median() - Vector((0.5, 0.5, 0))).length < 1e-5)
    o10 = uv_of_vert(bm, uv_layer, f0, (1, 0, 0))
    check("TOPZ: offset_u shifts the plan grid",
          o10 is not None and abs(o10.x - 0.75) < 1e-4,
          f"uv={tuple(o10) if o10 else None}")
    s.offset_u = 0.0

    # the overlay preview must survive the new source (it draws a HORIZONTAL
    # lattice there, not one parked beside the faces)
    data = uvmod._uv_overlay_build(bpy.context, s)
    check("TOPZ overlay builds", isinstance(data, dict) and "error" not in data,
          str(data if not isinstance(data, dict) else data.get("error", "ok")))
    if isinstance(data, dict) and "error" not in data:
        zs = [p[2] for p in data["lattice_pts"]]
        check("TOPZ overlay lattice is horizontal",
              bool(zs) and max(zs) - min(zs) < 1e-5,
              f"dz={max(zs) - min(zs):.2e}" if zs else "no lattice")
    bpy.ops.object.mode_set(mode='OBJECT')

    # SURFACE must be a no-op under TOPZ: the plan-view cut and unwrap have
    # to come out bit-identical to the PLANAR run
    def topz_cut_unwrap(name, projection):
        ob = make_grid_object(name, 3, 3, cell=1.0,
                              matrix=Matrix.Rotation(radians(60), 4, 'X'))
        b = enter_edit(ob)
        for f in b.faces:
            f.select = True
        reset_settings()
        st = settings()
        st.selection_mode = 'ALL'
        st.grid_source = 'TOPZ'
        st.projection = projection
        st.world_cell_u = st.world_cell_v = 0.4
        res = bpy.ops.agr.uv_grid_cut_unwrap()
        b = bmesh.from_edit_mesh(ob.data)
        uvl = b.loops.layers.uv.verify()
        uvs = sorted((round(l[uvl].uv.x, 5), round(l[uvl].uv.y, 5),
                      round(l.vert.co.x, 5), round(l.vert.co.y, 5))
                     for f in b.faces for l in f.loops)
        inside = all_uvs_in_unit(b, uvl)
        nf = len(b.faces)
        bpy.ops.object.mode_set(mode='OBJECT')
        return res, nf, uvs, inside

    r_pl, nf_pl, uv_pl, in_pl = topz_cut_unwrap("TopzPlanar", 'PLANAR')
    r_sf, nf_sf, uv_sf, in_sf = topz_cut_unwrap("TopzSurface", 'SURFACE')
    check("TOPZ cut+unwrap FINISHED", r_pl == {'FINISHED'} and r_sf == {'FINISHED'})
    check("TOPZ cut: every face lands inside its cell", in_pl and in_sf)
    check("TOPZ cut: mesh really got cut", nf_pl > 9, f"faces={nf_pl}")
    check("TOPZ: SURFACE projection is forced off (identical result)",
          nf_pl == nf_sf and uv_pl == uv_sf,
          f"faces {nf_pl} vs {nf_sf}, uv equal={uv_pl == uv_sf}")

    print("=" * 60)
    print("TEST 38: angle from edge — TOPZ plan grid")
    rot30 = Matrix.Rotation(radians(30), 4, 'Z')
    plan = make_grid_object("AnglePlan", 4, 4, cell=1.0, matrix=rot30)
    bm = enter_edit(plan)
    deselect_all(bm)
    wa, wb = rot30 @ Vector((0, 0, 0)), rot30 @ Vector((1, 0, 0))
    check("edge for angle pick found", select_edge_between(bm, plan, wa, wb))
    reset_settings()
    s = settings()
    s.grid_source = 'TOPZ'
    r = bpy.ops.agr.uv_grid_angle_from_edge()
    check("TOPZ angle pick FINISHED", r == {'FINISHED'})
    check("TOPZ angle = 30 deg", abs(degrees(s.world_angle) - 30.0) < 0.01,
          f"angle={degrees(s.world_angle):.3f}")
    # end-to-end: with the grid rotated to match, the rotated floor unwraps
    # into exact 0..1 cells (a wrong angle would leave cells straddling lines)
    r = bpy.ops.agr.uv_grid_unwrap()
    check("TOPZ unwrap after angle pick FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(plan.data)
    uv_layer = bm.loops.layers.uv.verify()
    check("TOPZ rotated floor lands in 0..1", all_uvs_in_unit(bm, uv_layer))
    exact = all(abs(c - round(c)) < 1e-4
                for f in bm.faces for loop in f.loops for c in loop[uv_layer].uv)
    check("TOPZ rotated floor corners exact", exact)
    bpy.ops.object.mode_set(mode='OBJECT')

    # an edge has no direction: 105 deg must come back as -75 (mod 180,
    # representative closest to zero)
    rot105 = Matrix.Rotation(radians(105), 4, 'Z')
    plan2 = make_grid_object("AnglePlan2", 1, 1, cell=1.0, matrix=rot105)
    bm = enter_edit(plan2)
    deselect_all(bm)
    check("105 deg edge found", select_edge_between(
        bm, plan2, rot105 @ Vector((0, 0, 0)), rot105 @ Vector((1, 0, 0))))
    reset_settings()
    s = settings()
    s.grid_source = 'TOPZ'
    bpy.ops.agr.uv_grid_angle_from_edge()
    check("angle reduced mod 180 to (-90, 90]",
          abs(degrees(s.world_angle) + 75.0) < 0.01,
          f"angle={degrees(s.world_angle):.3f}")
    bpy.ops.object.mode_set(mode='OBJECT')

    # error paths: vertical edge under TOPZ, zero / two edges, EDGES source
    wallv = make_grid_object("AngleWallV", 1, 1, cell=1.0, plane="XZ")
    bm = enter_edit(wallv)
    deselect_all(bm)
    check("vertical edge found", select_edge_between(bm, wallv, (0, 0, 0), (0, 0, 1)))
    reset_settings()
    s = settings()
    s.grid_source = 'TOPZ'
    check("vertical edge under TOPZ is refused",
          expect_cancel(lambda: bpy.ops.agr.uv_grid_angle_from_edge()))
    bm = bmesh.from_edit_mesh(wallv.data)
    deselect_all(bm)
    check("no edge is refused",
          expect_cancel(lambda: bpy.ops.agr.uv_grid_angle_from_edge()))
    select_edge_between(bm, wallv, (0, 0, 0), (1, 0, 0))
    select_edge_between(bm, wallv, (0, 0, 0), (0, 0, 1))
    check("two edges are refused",
          expect_cancel(lambda: bpy.ops.agr.uv_grid_angle_from_edge()))
    s.grid_source = 'EDGES'
    bm = bmesh.from_edit_mesh(wallv.data)
    deselect_all(bm)
    select_edge_between(bm, wallv, (0, 0, 0), (1, 0, 0))
    check("EDGES source is refused",
          expect_cancel(lambda: bpy.ops.agr.uv_grid_angle_from_edge()))
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 39: angle from edge — WORLD wall, bare edge click")
    # rotation about Y spins the XZ wall in its own plane: the wall normal
    # stays +-Y while every "horizontal" edge now slopes at 25 deg
    tilt = Matrix.Rotation(radians(25), 4, 'Y')
    wall9 = make_grid_object("AngleWall", 4, 3, cell=1.0, plane="XZ", matrix=tilt)
    bm = enter_edit(wall9)
    deselect_all(bm)
    check("sloped wall edge found", select_edge_between(
        bm, wall9, tilt @ Vector((0, 0, 0)), tilt @ Vector((1, 0, 0))))
    reset_settings()
    s = settings()
    s.grid_source = 'WORLD'
    s.selection_mode = 'SELECTED'   # nothing but the edge is selected:
    r = bpy.ops.agr.uv_grid_angle_from_edge()   # normal comes from its faces
    check("WORLD angle pick FINISHED (normal from edge faces)", r == {'FINISHED'})
    check("WORLD angle magnitude = 25 deg",
          abs(abs(degrees(s.world_angle)) - 25.0) < 0.01,
          f"angle={degrees(s.world_angle):.3f}")
    bm = bmesh.from_edit_mesh(wall9.data)
    for f in bm.faces:
        f.select = True
    r = bpy.ops.agr.uv_grid_unwrap()
    check("tilted wall unwrap FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(wall9.data)
    uv_layer = bm.loops.layers.uv.verify()
    check("tilted wall lands in 0..1", all_uvs_in_unit(bm, uv_layer))
    exact = all(abs(c - round(c)) < 1e-4
                for f in bm.faces for loop in f.loops for c in loop[uv_layer].uv)
    check("tilted wall corners exact", exact)
    check("tilted wall not mirrored", all(shoelace(f, uv_layer) > 0 for f in bm.faces))
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 40: offset from point — intersection lands at the vertex")
    off = Matrix.Translation((0.3, 0.6, 0.0))
    ofl = make_grid_object("OffsetFloor", 2, 2, cell=1.0, matrix=off)
    bm = enter_edit(ofl)
    deselect_all(bm)
    check("anchor vertex found", select_vert_at(bm, ofl, (0.3, 0.6, 0.0)))
    reset_settings()
    s = settings()
    s.grid_source = 'TOPZ'
    s.selection_mode = 'ALL'
    r = bpy.ops.agr.uv_grid_offset_from_point()
    check("TOPZ offset pick FINISHED", r == {'FINISHED'})
    # NEAREST corner: 0.6 -> -0.4, the equivalent small representative
    check("offsets = 0.3 / -0.4",
          abs(s.offset_u - 0.3) < 1e-5 and abs(s.offset_v + 0.4) < 1e-5,
          f"offsets=({s.offset_u:.4f}, {s.offset_v:.4f})")
    # idempotence: any lattice-mate vertex keeps the offsets unchanged
    bm = bmesh.from_edit_mesh(ofl.data)
    deselect_all(bm)
    select_vert_at(bm, ofl, (1.3, 1.6, 0.0))
    bpy.ops.agr.uv_grid_offset_from_point()
    check("lattice-mate vertex is a no-op",
          abs(s.offset_u - 0.3) < 1e-5 and abs(s.offset_v + 0.4) < 1e-5,
          f"offsets=({s.offset_u:.4f}, {s.offset_v:.4f})")
    # a huge pre-existing offset wraps back to the small representative
    s.offset_u = 0.9
    bm = bmesh.from_edit_mesh(ofl.data)
    deselect_all(bm)
    select_vert_at(bm, ofl, (0.3, 0.6, 0.0))
    bpy.ops.agr.uv_grid_offset_from_point()
    check("offset wraps to the small representative",
          abs(s.offset_u - 0.3) < 1e-5, f"offset_u={s.offset_u:.4f}")
    # end-to-end: the shifted floor now unwraps into exact 0..1 cells
    r = bpy.ops.agr.uv_grid_unwrap()
    check("unwrap after offset FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(ofl.data)
    uv_layer = bm.loops.layers.uv.verify()
    exact = all(abs(c - round(c)) < 1e-4
                for f in bm.faces for loop in f.loops for c in loop[uv_layer].uv)
    check("shifted floor corners exact", exact and all_uvs_in_unit(bm, uv_layer))
    # direct assertion: the picked point sits ON a lattice intersection of
    # the RESOLVED basis (offsets included)
    tg = uvmod._collect_targets(bpy.context, s)
    basis = uvmod._resolve_basis(None, s, tg, quiet=True)
    check("basis resolves after offset pick", basis is not None)
    if basis is not None:
        o, xd, yd, cu, cv = basis
        dd = Vector((0.3, 0.6, 0.0)) - o
        gu, gv = dd.dot(xd) / cu, dd.dot(yd) / cv
        check("point on lattice intersection",
              abs(gu - round(gu)) < 1e-6 and abs(gv - round(gv)) < 1e-6,
              f"g=({gu:.6f}, {gv:.6f})")

    # EDGES source: the stored grid shifts through the same offsets
    reset_settings()
    s = settings()
    s.has_grid = True
    s.origin = (0, 0, 0)
    s.u_dir = (1, 0, 0)
    s.v_dir = (0, 1, 0)
    s.cell_u = s.cell_v = 1.0
    s.selection_mode = 'ALL'
    bm = bmesh.from_edit_mesh(ofl.data)
    deselect_all(bm)
    select_vert_at(bm, ofl, (1.3, 0.6, 0.0))
    r = bpy.ops.agr.uv_grid_offset_from_point()
    check("EDGES offset pick FINISHED", r == {'FINISHED'})
    check("EDGES offsets = 0.3 / -0.4",
          abs(s.offset_u - 0.3) < 1e-5 and abs(s.offset_v + 0.4) < 1e-5,
          f"offsets=({s.offset_u:.4f}, {s.offset_v:.4f})")
    bpy.ops.object.mode_set(mode='OBJECT')

    # WORLD source, bare vertex click: axes resolve from the vertex's faces
    woff = Matrix.Translation((0.2, 0.0, 0.7))
    wall10 = make_grid_object("OffsetWall", 2, 2, cell=1.0, plane="XZ", matrix=woff)
    bm = enter_edit(wall10)
    deselect_all(bm)
    check("wall vertex found", select_vert_at(bm, wall10, (1.2, 0.0, 1.7)))
    reset_settings()
    s = settings()
    s.grid_source = 'WORLD'
    s.origin_mode = 'WORLD'
    s.selection_mode = 'SELECTED'   # nothing but the vertex is selected
    r = bpy.ops.agr.uv_grid_offset_from_point()
    check("WORLD bare-vertex pick FINISHED", r == {'FINISHED'})
    check("WORLD offsets snap the wall grid",
          abs(abs(s.offset_u) - 0.2) < 1e-5 and abs(abs(s.offset_v) - 0.3) < 1e-5,
          f"offsets=({s.offset_u:.4f}, {s.offset_v:.4f})")
    bm = bmesh.from_edit_mesh(wall10.data)
    for f in bm.faces:
        f.select = True
    r = bpy.ops.agr.uv_grid_unwrap()
    check("wall unwrap after offset FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(wall10.data)
    uv_layer = bm.loops.layers.uv.verify()
    exact = all(abs(c - round(c)) < 1e-4
                for f in bm.faces for loop in f.loops for c in loop[uv_layer].uv)
    check("offset wall corners exact", exact and all_uvs_in_unit(bm, uv_layer))

    # error paths: no vertex / two verts (an edge) / EDGES without a grid
    bm = bmesh.from_edit_mesh(wall10.data)
    deselect_all(bm)
    check("no vertex is refused",
          expect_cancel(lambda: bpy.ops.agr.uv_grid_offset_from_point()))
    select_edge_between(bm, wall10, (0.2, 0, 0.7), (1.2, 0, 0.7))
    check("two verts (an edge) are refused",
          expect_cancel(lambda: bpy.ops.agr.uv_grid_offset_from_point()))
    s.grid_source = 'EDGES'
    s.has_grid = False
    bm = bmesh.from_edit_mesh(wall10.data)
    deselect_all(bm)
    select_vert_at(bm, wall10, (0.2, 0.0, 0.7))
    check("EDGES without a grid is refused",
          expect_cancel(lambda: bpy.ops.agr.uv_grid_offset_from_point()))
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 41: angle from edge — tilted hip roof (floor branch, exact solve)")
    # plane with normal n=(0.4, 0.4, 0.8246): |n.z|>0.7 puts _world_base_axes
    # into the floor branch where base X/Y are NOT perpendicular to n — the
    # naive "rotate base_x onto the edge" formula left U up to 19.5° off
    n = Vector((0.4, 0.4, 0.0))
    n.z = (1.0 - n.length_squared) ** 0.5
    ex = Vector((1.0, 0.0, 0.0))
    e_dir = (ex - n * ex.dot(n)).normalized()   # in-plane edge direction
    w_dir = n.cross(e_dir)
    roof_mesh = bpy.data.meshes.new("HipRoof")
    p0 = Vector((0.0, 0.0, 0.0))
    roof_mesh.from_pydata(
        [tuple(p0), tuple(p0 + 2 * e_dir), tuple(p0 + 2 * e_dir + w_dir),
         tuple(p0 + w_dir)], [], [(0, 1, 2, 3)])
    roof_mesh.validate()
    roof = bpy.data.objects.new("HipRoof", roof_mesh)
    bpy.context.collection.objects.link(roof)
    bm = enter_edit(roof)
    deselect_all(bm)
    check("roof edge found", select_edge_between(bm, roof, tuple(p0),
                                                 tuple(p0 + 2 * e_dir)))
    reset_settings()
    s = settings()
    s.grid_source = 'WORLD'
    s.origin_mode = 'WORLD'
    r = bpy.ops.agr.uv_grid_angle_from_edge()
    check("hip-roof angle pick FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(roof.data)
    for f in bm.faces:
        f.select = True
    tg = uvmod._collect_targets(bpy.context, s)
    basis = uvmod._resolve_basis(None, s, tg, quiet=True)
    check("hip-roof basis resolves", basis is not None)
    if basis is not None:
        _o, xd, yd, _cu, _cv = basis
        check("hip roof: V constant along the edge (grid lines follow it)",
              abs(e_dir.dot(yd)) < 1e-5,
              f"e·y={e_dir.dot(yd):.6f}, e·x={e_dir.dot(xd):.6f}")
        check("hip roof: U really runs along the edge",
              abs(e_dir.dot(xd)) > 0.5, f"e·x={e_dir.dot(xd):.6f}")
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 42: crease guards — edge/vertex on facade∩ground are refused")
    crease_mesh = bpy.data.meshes.new("CreaseMesh")
    crease_mesh.from_pydata(
        [(0, 0, 0), (2, 0, 0), (2, 0, 2), (0, 0, 2),   # facade, XZ plane
         (0, 4, 0), (2, 4, 0)],                        # ground extends +Y
        [], [(0, 1, 2, 3), (0, 4, 5, 1)])
    crease_mesh.validate()
    crease = bpy.data.objects.new("CreaseMesh", crease_mesh)
    bpy.context.collection.objects.link(crease)
    bm = enter_edit(crease)
    deselect_all(bm)
    check("crease edge found", select_edge_between(bm, crease, (0, 0, 0), (2, 0, 0)))
    reset_settings()
    s = settings()
    s.grid_source = 'WORLD'
    s.origin_mode = 'WORLD'
    check("angle pick on a crease edge is refused",
          expect_cancel(lambda: bpy.ops.agr.uv_grid_angle_from_edge()))
    # top edge belongs to the facade alone — the guard must let it through
    bm = bmesh.from_edit_mesh(crease.data)
    deselect_all(bm)
    select_edge_between(bm, crease, (0, 0, 2), (2, 0, 2))
    r = bpy.ops.agr.uv_grid_angle_from_edge()
    check("angle pick on a facade-only edge passes", r == {'FINISHED'})
    # corner vertex mixes facade and ground normals — offsets would be
    # measured along axes the unwrap never uses
    bm = bmesh.from_edit_mesh(crease.data)
    deselect_all(bm)
    check("corner vertex found", select_vert_at(bm, crease, (0, 0, 0)))
    check("offset pick on a corner vertex is refused",
          expect_cancel(lambda: bpy.ops.agr.uv_grid_offset_from_point()))
    bm = bmesh.from_edit_mesh(crease.data)
    deselect_all(bm)
    select_vert_at(bm, crease, (0, 0, 2))   # facade-only vertex
    r = bpy.ops.agr.uv_grid_offset_from_point()
    check("offset pick on a facade-only vertex passes", r == {'FINISHED'})
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 43: offset from point under SURFACE — arc-U residual via frames")
    swall = make_grid_object("SurfWall", 4, 3, cell=1.0, plane="XZ",
                             matrix=Matrix.Translation((2.37, 0.0, 0.4)))
    bm = enter_edit(swall)
    deselect_all(bm)
    check("surf vertex found", select_vert_at(bm, swall, (3.37, 0.0, 1.4)))
    reset_settings()
    s = settings()
    s.grid_source = 'WORLD'
    s.origin_mode = 'WORLD'
    s.projection = 'SURFACE'
    s.selection_mode = 'ALL'    # targets exist -> frames available
    s.world_cell_u = 0.8
    s.world_cell_v = 1.0
    r = bpy.ops.agr.uv_grid_offset_from_point()
    check("SURFACE offset pick FINISHED", r == {'FINISHED'})
    # picked vertex sits 1.0 m of arc from the component minimum: residual
    # to the nearest 0.8-line is 0.2 m (sign depends on the arc direction)
    check("SURFACE offsets: |U|=0.2 (arc), V=0.4",
          abs(abs(s.offset_u) - 0.2) < 1e-5 and abs(s.offset_v - 0.4) < 1e-5,
          f"offsets=({s.offset_u:.4f}, {s.offset_v:.4f})")
    r = bpy.ops.agr.uv_grid_unwrap()
    check("SURFACE unwrap after offset FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(swall.data)
    uv_layer = bm.loops.layers.uv.verify()
    inv = swall.matrix_world.inverted()
    target_local = inv @ Vector((3.37, 0.0, 1.4))
    picked_uvs = [Vector(loop[uv_layer].uv) for f in bm.faces for loop in f.loops
                  if (loop.vert.co - target_local).length < 1e-5]
    check("picked vertex lands ON the arc grid line",
          bool(picked_uvs) and all(abs(uv.x - round(uv.x)) < 1e-4
                                   and abs(uv.y - round(uv.y)) < 1e-4
                                   for uv in picked_uvs),
          f"uvs={[tuple(round(c, 4) for c in uv) for uv in picked_uvs]}")
    # frames unavailable (SELECTED mode, nothing but the vertex selected):
    # U must stay untouched, V still applies
    s.offset_u = s.offset_v = 0.0
    s.selection_mode = 'SELECTED'
    bm = bmesh.from_edit_mesh(swall.data)
    deselect_all(bm)
    select_vert_at(bm, swall, (3.37, 0.0, 1.4))
    r = bpy.ops.agr.uv_grid_offset_from_point()
    check("SURFACE offset without frames FINISHED (V-only)", r == {'FINISHED'})
    check("U untouched, V applied",
          abs(s.offset_u) < 1e-9 and abs(s.offset_v - 0.4) < 1e-5,
          f"offsets=({s.offset_u:.4f}, {s.offset_v:.4f})")
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 44: «Весь меш» — the cut keeps the orientation gesture (UV-1)")
    # The documented gesture: in «Весь меш» the LIVE face selection only
    # orients the grid.  bisect_plane used to destroy it (new faces come out
    # unselected and the VERT/EDGE flush drops the parent too), so the
    # follow-up unwrap resolved a DIFFERENT grid — a wall turned into a
    # floor on a building and pieces landed outside 0..1.

    def set_select_mode(kind):
        bpy.context.tool_settings.mesh_select_mode = {
            'VERT': (True, False, False),
            'EDGE': (False, True, False),
            'FACE': (False, False, True)}[kind]

    def make_roof_wall(name):
        """10x10 roof at z=2 plus a 4x2 wall — the mean normal of the WHOLE
        mesh is vertical (floor grid), of the wall alone horizontal."""
        me = bpy.data.meshes.new(name)
        me.from_pydata([(0, 0, 2), (10, 0, 2), (10, 10, 2), (0, 10, 2),
                        (0, 0, 0), (4, 0, 0), (4, 0, 2), (0, 0, 2)],
                       [], [(0, 1, 2, 3), (4, 5, 6, 7)])
        me.validate()
        ob = bpy.data.objects.new(name, me)
        bpy.context.collection.objects.link(ob)
        return ob

    def select_wall(obj):
        bm = bmesh.from_edit_mesh(obj.data)
        deselect_all(bm)
        wall = next(f for f in bm.faces
                    if abs(f.calc_center_median().z - 1.0) < 0.1)
        wall.select = True
        bm.select_flush(True)
        bmesh.update_edit_mesh(obj.data)
        return bm

    def world_grid_settings():
        s = reset_settings()
        s.grid_source = 'WORLD'
        s.origin_mode = 'WORLD'
        s.selection_mode = 'ALL'
        s.world_cell_u = s.world_cell_v = 1.0
        return s

    def faces_out_of_unit(obj):
        bm = bmesh.from_edit_mesh(obj.data)
        uvl = bm.loops.layers.uv.verify()
        out = 0
        for f in bm.faces:
            for lo in f.loops:
                u, v = lo[uvl].uv
                if u < -1e-3 or u > 1 + 1e-3 or v < -1e-3 or v > 1 + 1e-3:
                    out += 1
                    break
        return out, len(bm.faces)

    for mode in ('VERT', 'EDGE'):
        rw = make_roof_wall(f"RoofWall_{mode}")
        set_select_mode(mode)
        enter_edit(rw)
        select_wall(rw)
        s = world_grid_settings()
        b0 = uvmod._resolve_basis(None, s, uvmod._collect_targets(bpy.context, s),
                                  quiet=True)
        r = bpy.ops.agr.uv_grid_cut()
        check(f"{mode}: cut FINISHED", r == {'FINISHED'})
        bm = bmesh.from_edit_mesh(rw.data)
        nsel = sum(1 for f in bm.faces if f.select)
        check(f"{mode}: selection survives the cut", nsel > 0, f"selected={nsel}")
        b1 = uvmod._resolve_basis(None, s, uvmod._collect_targets(bpy.context, s),
                                  quiet=True)
        check(f"{mode}: basis unchanged between cut and unwrap",
              all(abs(a - b) < 1e-6 for a, b in
                  zip(list(b0[1]) + list(b0[2]), list(b1[1]) + list(b1[2]))),
              f"y {tuple(round(c, 2) for c in b0[2])} -> "
              f"{tuple(round(c, 2) for c in b1[2])}")
        check(f"{mode}: no scratch attribute left in the mesh",
              uvmod._SEL_KEEP_LAYER not in
              [a.name for a in rw.data.attributes],
              str([a.name for a in rw.data.attributes]))
        bpy.ops.object.mode_set(mode='OBJECT')

        rw2 = make_roof_wall(f"RoofWallOne_{mode}")
        set_select_mode(mode)
        enter_edit(rw2)
        select_wall(rw2)
        world_grid_settings()
        r = bpy.ops.agr.uv_grid_cut_unwrap()
        check(f"{mode}: one-click cut+unwrap FINISHED", r == {'FINISHED'})
        out, n = faces_out_of_unit(rw2)
        check(f"{mode}: every face inside 0..1 after one click", out == 0,
              f"out={out} of {n}")
        bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 45: closed shell — cut_unwrap never CANCELS after cutting (UV-2)")
    set_select_mode('VERT')
    bpy.ops.mesh.primitive_cube_add(size=2.0)
    cube = bpy.context.active_object
    bm = enter_edit(cube)
    deselect_all(bm)
    px = next(f for f in bm.faces if f.calc_center_median().x > 0.9)
    px.select = True
    bm.select_flush(True)
    bmesh.update_edit_mesh(cube.data)
    s = reset_settings()
    s.grid_source = 'WORLD'
    s.origin_mode = 'WORLD'
    s.world_cell_u = s.world_cell_v = 0.5
    before = len(bm.faces)
    r = bpy.ops.agr.uv_grid_cut_unwrap()
    bm = bmesh.from_edit_mesh(cube.data)
    check("closed cube: FINISHED (a CANCELLED here welds the cut into the "
          "previous undo step)", r == {'FINISHED'})
    check("closed cube: the mesh really got cut", len(bm.faces) > before,
          f"{before} -> {len(bm.faces)}")
    bpy.ops.object.mode_set(mode='OBJECT')

    # the committed-refusal path itself, without needing a mesh that fails
    class _FakeOp:
        def __init__(self):
            self.reports = []

        def report(self, kinds, msg):
            self.reports.append((set(kinds), msg))

    fake = _FakeOp()
    _orig_collect = uvmod._collect_targets
    uvmod._collect_targets = lambda ctx, st: []
    try:
        committed_ok = uvmod._do_unwrap(fake, bpy.context, settings(),
                                        committed=True)
        plain_ok = uvmod._do_unwrap(_FakeOp(), bpy.context, settings())
    finally:
        uvmod._collect_targets = _orig_collect
    check("committed refusal returns True (operator must FINISH)",
          committed_ok is True)
    check("committed refusal reports a WARNING with the Ctrl+Z hint",
          any('WARNING' in kinds and "Ctrl+Z" in msg
              for kinds, msg in fake.reports), str(fake.reports))
    check("plain refusal still returns False", plain_ok is False)

    print("=" * 60)
    print("TEST 46: per-object isolation of the cut loop (UV-7)")
    isoa = make_grid_object("IsoCutA", 2, 2, cell=1.0)
    isob = make_grid_object("IsoCutB", 2, 2, cell=1.0,
                            matrix=Matrix.Translation(Vector((10, 0, 0))))
    bpy.ops.object.mode_set(mode='OBJECT')
    for o in bpy.context.selected_objects:
        o.select_set(False)
    isoa.select_set(True)
    isob.select_set(True)
    bpy.context.view_layer.objects.active = isoa
    bpy.ops.object.mode_set(mode='EDIT')
    s = reset_settings()
    s.grid_source = 'WORLD'
    s.origin_mode = 'WORLD'
    s.world_cell_u = s.world_cell_v = 0.5
    _orig_cut_object = uvmod._cut_object

    def _boom_cut(op, settings_, obj, *args, **kwargs):
        if obj.name == "IsoCutB":
            raise RuntimeError("boom")
        return _orig_cut_object(op, settings_, obj, *args, **kwargs)

    uvmod._cut_object = _boom_cut
    try:
        r = bpy.ops.agr.uv_grid_cut()
    finally:
        uvmod._cut_object = _orig_cut_object
    status = bpy.context.window_manager.agr_last_status
    check("failing second object still FINISHES (undo step is pushed)",
          r == {'FINISHED'}, str(r))
    check("the failure is named in the report", "IsoCutB" in status, status)
    bm_a = bmesh.from_edit_mesh(isoa.data)
    check("the first object is really cut", len(bm_a.faces) > 4,
          f"faces={len(bm_a.faces)}")
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 47: cut cap lowered to the measured range (UV-4)")
    check("cap inside the measured 512..800 band",
          512 <= uvmod._MAX_CUT_LINES <= 800, f"cap={uvmod._MAX_CUT_LINES}")
    import time as _time
    big = make_grid_object("CutCapBig", 1, 1, cell=500.0)   # span ~1000 lines
    bm = enter_edit(big)
    s = reset_settings()
    s.grid_source = 'WORLD'
    s.origin_mode = 'WORLD'
    s.world_cell_u = s.world_cell_v = 1.0
    t0 = _time.time()
    cancelled = expect_cancel(bpy.ops.agr.uv_grid_cut)
    dt = _time.time() - t0
    check("~1000 lines refused (the old 2048 cap let it through)", cancelled)
    check("the refusal is instant", dt < 2.0, f"dt={dt:.2f}s")
    check("the error names the measured cause",
          "время растёт быстрее" in bpy.context.window_manager.agr_last_status,
          bpy.context.window_manager.agr_last_status)
    bm = bmesh.from_edit_mesh(big.data)
    check("mesh untouched by the refusal", len(bm.faces) == 1)
    bpy.ops.object.mode_set(mode='OBJECT')
    check("a refused cut leaves no scratch attribute either",
          uvmod._SEL_KEEP_LAYER not in [a.name for a in big.data.attributes],
          str([a.name for a in big.data.attributes]))

    print("=" * 60)
    print("TEST 48: overlay fingerprint skips the selection scan when the "
          "preview already refused (UV-5)")
    ov = make_grid_object("OverlayCap", 3, 3, cell=1.0)
    bm = enter_edit(ov)
    bm.faces.ensure_lookup_table()
    for f in bm.faces:
        f.select = False
    bm.faces[0].select = True
    bm.select_flush(True)
    bmesh.update_edit_mesh(ov.data)
    s = reset_settings()
    s.grid_source = 'WORLD'
    s.origin_mode = 'WORLD'
    uvmod._uv_overlay_refused["key"] = None
    fp_sel = uvmod._uv_overlay_fingerprint(bpy.context, s)
    check("normal fingerprint is not the refusal marker", fp_sel[0] != "refused")
    _old_max = uvmod._OVERLAY_MAX_FACES
    uvmod._OVERLAY_MAX_FACES = 1
    try:
        data = uvmod._uv_overlay_build(bpy.context, s)
        check("build refuses above the face cap",
              isinstance(data, dict) and "error" in data, str(data))
        check("the refusal is remembered",
              uvmod._uv_overlay_refused["key"] is not None)
        fp1 = uvmod._uv_overlay_fingerprint(bpy.context, s)
        check("fingerprint short-circuits to the refusal marker",
              fp1[0] == "refused", str(fp1)[:80])
        bm = bmesh.from_edit_mesh(ov.data)
        bm.faces.ensure_lookup_table()
        bm.faces[1].select = True
        bmesh.update_edit_mesh(ov.data)
        fp2 = uvmod._uv_overlay_fingerprint(bpy.context, s)
        check("a selection change costs nothing while refused", fp1 == fp2)
    finally:
        uvmod._OVERLAY_MAX_FACES = _old_max
    data = uvmod._uv_overlay_build(bpy.context, s)
    check("the refusal is dropped once the preview builds again",
          uvmod._uv_overlay_refused["key"] is None)
    fp3 = uvmod._uv_overlay_fingerprint(bpy.context, s)
    bm = bmesh.from_edit_mesh(ov.data)
    bm.faces.ensure_lookup_table()
    bm.faces[2].select = True
    bmesh.update_edit_mesh(ov.data)
    fp4 = uvmod._uv_overlay_fingerprint(bpy.context, s)
    check("selection is back in the fingerprint when the preview draws",
          fp3 != fp4)
    # the two O(1) short-cuts: nothing / everything selected
    bm = bmesh.from_edit_mesh(ov.data)
    for f in bm.faces:
        f.select = False
    bmesh.update_edit_mesh(ov.data)
    fp_none = uvmod._uv_overlay_fingerprint(bpy.context, s)
    for f in bm.faces:
        f.select = True
    bm.select_flush(True)
    bmesh.update_edit_mesh(ov.data)
    fp_all = uvmod._uv_overlay_fingerprint(bpy.context, s)
    check("none-selected and all-selected differ", fp_none != fp_all)

    # In SELECTED mode the target count IS the selected-face count, so the
    # refusal cache MUST notice a narrower selection: select-all on a city
    # mesh refused and the preview stayed dead until the geometry changed
    # (a pure selection change does not bump _uv_geo_version).
    s.selection_mode = 'SELECTED'
    uvmod._uv_overlay_refused["key"] = None
    bm = bmesh.from_edit_mesh(ov.data)
    bm.faces.ensure_lookup_table()
    for f in bm.faces:
        f.select = True
    bm.select_flush(True)
    bmesh.update_edit_mesh(ov.data)
    uvmod._OVERLAY_MAX_FACES = 4
    try:
        data = uvmod._uv_overlay_build(bpy.context, s)
        check("SELECTED: build refuses when the whole mesh is selected",
              isinstance(data, dict) and "error" in data, str(data))
        fp_wide = uvmod._uv_overlay_fingerprint(bpy.context, s)
        check("SELECTED: the wide selection short-circuits to the marker",
              fp_wide[0] == "refused", str(fp_wide)[:80])
        for f in bm.faces:
            f.select = False
        bm.faces[0].select = True
        bm.select_flush(True)
        bmesh.update_edit_mesh(ov.data)
        fp_narrow = uvmod._uv_overlay_fingerprint(bpy.context, s)
        check("SELECTED: narrowing the selection leaves the refusal marker",
              fp_narrow[0] != "refused", str(fp_narrow)[:80])
        data = uvmod._uv_overlay_build(bpy.context, s)
        check("SELECTED: the preview comes back without touching geometry",
              isinstance(data, dict) and "error" not in data, str(data)[:120])
    finally:
        uvmod._OVERLAY_MAX_FACES = _old_max
        uvmod._uv_overlay_refused["key"] = None
        s.selection_mode = 'ALL'
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 49: overlay handlers dedupe by __name__, not identity (UV-9)")

    def _uv_overlay_depsgraph(_scene, _dg):
        """Stand-in for the handler object of a previous module reload."""

    bpy.app.handlers.depsgraph_update_post.append(_uv_overlay_depsgraph)
    uvmod._uv_add_handlers()
    n_dg = sum(1 for h in bpy.app.handlers.depsgraph_update_post
               if getattr(h, "__name__", "") == "_uv_overlay_depsgraph")
    check("stale twin dropped by name on add", n_dg == 1, f"n={n_dg}")
    uvmod._uv_remove_handlers()
    n_dg = sum(1 for h in bpy.app.handlers.depsgraph_update_post
               if getattr(h, "__name__", "") == "_uv_overlay_depsgraph")
    check("remove clears every twin", n_dg == 0, f"n={n_dg}")

    def _uv_sync_handlers_on_load(_dummy):
        """Stand-in for the load_post handler of a previous module reload."""

    bpy.app.handlers.load_post.append(_uv_sync_handlers_on_load)
    uvmod.unregister()
    uvmod.register()   # a dev reload does exactly this
    n_lp = sum(1 for h in bpy.app.handlers.load_post
               if getattr(h, "__name__", "") == "_uv_sync_handlers_on_load")
    check("stale load_post twin dropped on re-register", n_lp == 1, f"n={n_lp}")

except Exception:
    traceback.print_exc()
    FAILS.append("EXCEPTION")

print("=" * 60)
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILED -> " + "; ".join(FAILS))
    sys.exit(1)
print("RESULT: ALL TESTS PASSED")
