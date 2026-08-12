# Headless test for AGR_tools/operators_uv.py
# Run: blender --background --factory-startup --python scripts/test_uv_grid.py
import os
import sys
import traceback

import bpy
import bmesh
from math import radians
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

except Exception:
    traceback.print_exc()
    FAILS.append("EXCEPTION")

print("=" * 60)
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILED -> " + "; ".join(FAILS))
    sys.exit(1)
print("RESULT: ALL TESTS PASSED")
