# Headless test for AGR UV "Органика" — voxel-piece unwrap of organic meshes
# (operators_uv.py: _organic_* helpers + agr.uv_organic_unwrap[_selected]).
# Run: blender --background --factory-startup --python scripts/test_uv_organic.py
import io
import os
import sys
import traceback

import bpy
import bmesh
from math import cos, floor, radians
from mathutils import Vector

# repo root = parent of scripts/ — works from any checkout location
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import AGR_tools.log as agr_log
import AGR_tools.operators_uv as uvmod
from AGR_tools.operators_udim import invalidate_udim_cache

agr_log.register()
uvmod.register()

FAILS = []


def check(name, cond, extra=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def expect_cancel(callop):
    """True only for a clean CANCELLED / report-ERROR outcome — a crash
    wrapped in RuntimeError carries a traceback and must not pass."""
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


def settings():
    return bpy.context.scene.agr_uv_settings


def reset_settings(cell=0.25, cut=True, fill='SCALE', align='WORLD',
                   merge=0.15, margin=1.0, angle=60.0, selection='ALL'):
    s = settings()
    s.organic_cell = cell
    s.organic_cut = cut
    s.organic_fill = fill
    s.organic_align = align
    s.organic_merge = merge
    s.organic_margin = margin
    s.organic_angle = radians(angle)
    s.selection_mode = selection
    return s


def suzanne(name, levels=0, size=2.0):
    bpy.ops.object.select_all(action='DESELECT')
    bpy.ops.mesh.primitive_monkey_add(size=size, location=(0, 0, 0))
    obj = bpy.context.active_object
    obj.name = name
    if levels:
        m = obj.modifiers.new("Subsurf", 'SUBSURF')
        m.levels = m.render_levels = levels
        bpy.ops.object.modifier_apply(modifier=m.name)
    return obj


def select_only(*objs):
    bpy.ops.object.select_all(action='DESELECT')
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]


def all_uvs(obj):
    uvl = obj.data.uv_layers.active
    return [tuple(d.uv) for d in uvl.data]


def uvs_in_unit(obj, eps=1e-4, lo=0.0, hi=1.0):
    for u, v in all_uvs(obj):
        if u < lo - eps or u > hi + eps or v < lo - eps or v > hi + eps:
            return False
    return True


def uv_bounds(obj):
    pts = all_uvs(obj)
    return (min(p[0] for p in pts), min(p[1] for p in pts),
            max(p[0] for p in pts), max(p[1] for p in pts))


def _shoelace(pts):
    s = 0.0
    for i, (ax, ay) in enumerate(pts):
        bx, by = pts[(i + 1) % len(pts)]
        s += ax * by - bx * ay
    return s * 0.5


def count_folds(obj):
    """FAN TRIANGLES whose UV winding runs against their piece — the
    projection folded and two triangles share texels.

    Per triangle, not per face, on purpose: the cut emits n-gons, and an
    n-gon keeps a consistent whole-face shoelace while one of its fan
    triangles is already flipped (measured: the per-face test undercounts
    by ~2x).  The triangles are what gets rendered and exported, so they
    are what the cone invariant has to protect."""
    me = obj.data
    uvl = me.uv_layers.active
    folds = 0
    for poly in me.polygons:
        pts = [tuple(uvl.data[li].uv) for li in poly.loop_indices]
        for i in range(1, len(pts) - 1):
            (x0, y0), (x1, y1), (x2, y2) = pts[0], pts[i], pts[i + 1]
            if (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0) < -1e-12:
                folds += 1
    return folds


def densities(obj):
    """UV area per 3D area for every face (texel density, sorted)."""
    me = obj.data
    uvl = me.uv_layers.active
    out = []
    for poly in me.polygons:
        if poly.area < 1e-12:
            continue
        pts = [tuple(uvl.data[li].uv) for li in poly.loop_indices]
        out.append(abs(_shoelace(pts)) / poly.area)
    out.sort()
    return out


def pct(vals, q):
    if not vals:
        return 0.0
    return vals[min(len(vals) - 1, max(0, int(q * (len(vals) - 1))))]


def max_cell_span(obj, axis):
    """Largest world extent of a single face along a world axis."""
    mat = obj.matrix_world
    me = obj.data
    worst = 0.0
    for poly in me.polygons:
        vals = [(mat @ me.vertices[me.loops[li].vertex_index].co)[axis]
                for li in poly.loop_indices]
        worst = max(worst, max(vals) - min(vals))
    return worst


try:
    print("=" * 60)
    print("TEST 1: Suzanne, subdiv 0/1/2/3 — runs, stays in 0..1, no folds")
    for levels in (0, 1, 2, 3):
        reset_scene()
        obj = suzanne(f"Suz{levels}", levels)
        before = len(obj.data.polygons)
        reset_settings(cell=0.25)
        select_only(obj)
        r = bpy.ops.agr.uv_organic_unwrap()
        after = len(obj.data.polygons)
        folds = count_folds(obj)
        check(f"subdiv {levels}: FINISHED", r == {'FINISHED'})
        check(f"subdiv {levels}: mesh got cut", after > before,
              f"{before} -> {after}")
        check(f"subdiv {levels}: all UVs inside 0..1", uvs_in_unit(obj),
              str(tuple(round(c, 4) for c in uv_bounds(obj))))
        # subdiv 0 is the one case with a tolerance: its raw quads are
        # strongly non-planar and comparable to the cell, so a fan triangle
        # can point outside the cone its own face satisfies (2 of 2852
        # measured).  Everything denser must be exactly 0.
        limit = 5 if levels == 0 else 0
        check(f"subdiv {levels}: no folded triangles", folds <= limit,
              f"folds={folds} limit={limit}")

    print("=" * 60)
    print("TEST 2: the voxel cut really bounds a face to one cell")
    reset_scene()
    obj = suzanne("SuzCut", 2)
    reset_settings(cell=0.25)
    select_only(obj)
    bpy.ops.agr.uv_organic_unwrap()
    spans = [max_cell_span(obj, a) for a in (0, 1, 2)]
    check("no face spans more than one cell in X/Y/Z",
          all(sp <= 0.25 + 1e-4 for sp in spans),
          f"max spans={[round(sp, 5) for sp in spans]}")

    print("=" * 60)
    print("TEST 3: SCALE keeps ONE texel density; the cone bounds the rest")
    def run_density(name, levels, cell, fill, angle):
        reset_scene()
        obj = suzanne(name, levels)
        reset_settings(cell=cell, fill=fill, angle=angle)
        select_only(obj)
        bpy.ops.agr.uv_organic_unwrap()
        d = densities(obj)
        return d, pct(d, 0.95) / max(pct(d, 0.05), 1e-9)

    d_scale, spread_scale = run_density("SuzScale", 2, 0.25, 'SCALE', 60.0)
    d_tight, spread_tight = run_density("SuzTight", 2, 0.25, 'SCALE', 25.0)
    d_stretch, spread_stretch = run_density("SuzStretch", 2, 0.25, 'STRETCH', 60.0)
    # 1 UV square == cell * _ORGANIC_SCALE_SLACK metres  ->  density ~
    # 1/tile**2 (4.0 at cell 0.25), never ABOVE it (a projection can only
    # shrink a footprint, never grow it)
    tile = 0.25 * uvmod._ORGANIC_SCALE_SLACK
    target = 1.0 / (tile * tile)
    check("SCALE: density sits at 1/tile^2",
          abs(pct(d_scale, 0.5) - target) < 0.1 * target,
          f"median={pct(d_scale, 0.5):.3f} target={target:.3f}")
    check("SCALE: no face denser than 1/tile^2",
          pct(d_scale, 1.0) <= target * 1.02, f"max={pct(d_scale, 1.0):.3f}")
    # the only thing left spreading the density is foreshortening INSIDE a
    # piece, and the cone caps it at 1/cos(half-angle) — that is the whole
    # deal this design makes, so assert the bound instead of a magic number
    check("SCALE: spread stays under the cone bound 1/cos(60 deg)",
          spread_scale < 1.0 / cos(radians(60.0)) + 0.05,
          f"p95/p5={spread_scale:.3f} bound={1.0 / cos(radians(60.0)):.2f}")
    check("SCALE: a tighter cone (25 deg) tightens the density",
          spread_tight < 1.0 / cos(radians(25.0)) + 0.05
          and spread_tight < spread_scale,
          f"25deg={spread_tight:.3f} vs 60deg={spread_scale:.3f}")
    check("STRETCH spreads density far wider (documents the trade-off)",
          spread_stretch > 4.0 and spread_stretch > 3.0 * spread_scale,
          f"stretch={spread_stretch:.1f} vs scale={spread_scale:.3f}")

    print("=" * 60)
    print("TEST 4: deterministic — same mesh, same UVs")
    reset_scene()
    o1 = suzanne("Det1", 1)
    reset_settings(cell=0.3)
    select_only(o1)
    bpy.ops.agr.uv_organic_unwrap()
    uv1 = all_uvs(o1)
    o2 = suzanne("Det2", 1)
    reset_settings(cell=0.3)
    select_only(o2)
    bpy.ops.agr.uv_organic_unwrap()
    uv2 = all_uvs(o2)
    same = len(uv1) == len(uv2) and all(
        abs(x[0] - y[0]) < 1e-6 and abs(x[1] - y[1]) < 1e-6
        for x, y in zip(uv1, uv2))
    check("two identical runs produce identical UVs", same,
          f"loops {len(uv1)} vs {len(uv2)}")

    print("=" * 60)
    print("TEST 5: cut-line cap cancels BEFORE mutating the mesh")
    reset_scene()
    obj = suzanne("SuzCap", 1)
    before = len(obj.data.polygons)
    reset_settings(cell=0.001)
    select_only(obj)
    check("tiny cell CANCELLED", expect_cancel(bpy.ops.agr.uv_organic_unwrap))
    check("mesh untouched after the cancel",
          len(obj.data.polygons) == before,
          f"{before} -> {len(obj.data.polygons)}")

    print("=" * 60)
    print("TEST 6: «Резать меш» off — UV only, geometry untouched")
    reset_scene()
    obj = suzanne("SuzNoCut", 1)
    before = len(obj.data.polygons)
    reset_settings(cell=0.25, cut=False)
    select_only(obj)
    r = bpy.ops.agr.uv_organic_unwrap()
    check("no-cut FINISHED", r == {'FINISHED'})
    check("face count unchanged", len(obj.data.polygons) == before,
          f"{before} -> {len(obj.data.polygons)}")
    check("UVs still inside 0..1", uvs_in_unit(obj))

    print("=" * 60)
    print("TEST 7: Edit mode touches ONLY the selected faces")
    reset_scene()
    obj = suzanne("SuzSel", 1)
    obj.data.uv_layers.new(name="UVMap")
    for d in obj.data.uv_layers.active.data:
        d.uv = (5.0, 5.0)                 # sentinel: must survive untouched
    sel_polys = [p.index for p in obj.data.polygons
                 if p.center.x > 0.2]
    reset_settings(cell=0.25, cut=False, selection='SELECTED')
    select_only(obj)
    bpy.ops.object.mode_set(mode='EDIT')
    bm = bmesh.from_edit_mesh(obj.data)
    for f in bm.faces:
        f.select = f.index in set(sel_polys)
    bmesh.update_edit_mesh(obj.data)
    r = bpy.ops.agr.uv_organic_unwrap_selected()
    bpy.ops.object.mode_set(mode='OBJECT')
    check("edit-mode op FINISHED", r == {'FINISHED'})
    uvl = obj.data.uv_layers.active
    touched_ok = untouched_ok = True
    for poly in obj.data.polygons:
        pts = [tuple(uvl.data[li].uv) for li in poly.loop_indices]
        if poly.index in set(sel_polys):
            if any(u < -1e-4 or u > 1.0001 or v < -1e-4 or v > 1.0001
                   for u, v in pts):
                touched_ok = False
        else:
            if any(abs(u - 5.0) > 1e-6 or abs(v - 5.0) > 1e-6 for u, v in pts):
                untouched_ok = False
    check("selected faces unwrapped into 0..1", touched_ok)
    check("unselected faces keep their old UVs", untouched_ok)

    print("=" * 60)
    print("TEST 7b: the cut must not shred the user's face selection")
    # bisect_plane creates its faces deselected AND replaces the originals,
    # so without an explicit reselect the selection comes out EMPTY - the
    # first run looks fine (the new faces ride in res['geom']), and the
    # SECOND run finds nothing to work on.  _do_cut has always reselected;
    # the organic cutter has to agree with it.
    reset_scene()
    obj = suzanne("SuzReselect", 1)
    reset_settings(cell=0.3, cut=True, selection='SELECTED')
    select_only(obj)
    bpy.ops.object.mode_set(mode='EDIT')
    bm = bmesh.from_edit_mesh(obj.data)
    for f in bm.faces:
        f.select = f.calc_center_median().x > 0.0
    bmesh.update_edit_mesh(obj.data)
    sel_before = sum(1 for f in bm.faces if f.select)
    r = bpy.ops.agr.uv_organic_unwrap_selected()
    check("reselect: first run FINISHED", r == {'FINISHED'})
    bm = bmesh.from_edit_mesh(obj.data)
    sel_after = sum(1 for f in bm.faces if f.select)
    check("reselect: the selection survived the cut", sel_after > 0,
          f"{sel_before} -> {sel_after}")
    # the real point: a SECOND run still has something to work on
    r2 = bpy.ops.agr.uv_organic_unwrap_selected()
    check("reselect: a second run still finds its faces", r2 == {'FINISHED'},
          str(r2))
    bpy.ops.object.mode_set(mode='OBJECT')

    print("=" * 60)
    print("TEST 8: UDIM object — every piece stays in its own tile")
    reset_scene()
    tmpdir = bpy.app.tempdir
    img = bpy.data.images.new("__udim_src", width=64, height=64)
    path = os.path.join(tmpdir, "T_organic.1001.png")
    img.filepath_raw = path
    img.file_format = 'PNG'
    img.save()
    bpy.data.images.remove(img)
    tiled = bpy.data.images.load(path)
    tiled.source = 'TILED'
    tiled.tiles.new(tile_number=1002)
    mat = bpy.data.materials.new("M_Udim")
    mat.use_nodes = True
    node = mat.node_tree.nodes.new('ShaderNodeTexImage')
    node.image = tiled
    obj = suzanne("SuzUdim", 1)
    obj.data.materials.append(mat)
    obj.data.uv_layers.new(name="UVMap")
    for d in obj.data.uv_layers.active.data:
        d.uv = (1.5, 0.5)                 # everything lives in tile 1002
    invalidate_udim_cache()   # the material was just built: drop the cache
    reset_settings(cell=0.25)
    select_only(obj)
    r = bpy.ops.agr.uv_organic_unwrap()
    check("UDIM object FINISHED", r == {'FINISHED'})
    lo_u, lo_v, hi_u, hi_v = uv_bounds(obj)
    check("UVs stayed inside tile 1002 (u in 1..2, v in 0..1)",
          lo_u > 1.0 - 1e-3 and hi_u < 2.0 + 1e-3
          and lo_v > -1e-3 and hi_v < 1.0 + 1e-3,
          f"bounds={tuple(round(c, 4) for c in (lo_u, lo_v, hi_u, hi_v))}")

    print("=" * 60)
    print("TEST 9: shape keys — blocked in Object mode, carried in Edit mode")
    reset_scene()
    obj = suzanne("SuzShape", 0)
    select_only(obj)
    bpy.ops.object.shape_key_add(from_mix=False)
    bpy.ops.object.shape_key_add(from_mix=False)
    before = len(obj.data.polygons)
    reset_settings(cell=0.3)
    check("object mode refuses a shape-keyed mesh",
          expect_cancel(bpy.ops.agr.uv_organic_unwrap))
    check("shape-keyed mesh untouched", len(obj.data.polygons) == before)
    # ... and also with the cut off: bm.to_mesh() drops the keys either way
    reset_settings(cell=0.3, cut=False)
    check("object mode refuses it with «Резать меш» off too",
          expect_cancel(bpy.ops.agr.uv_organic_unwrap))
    check("shape keys still there", obj.data.shape_keys is not None)
    reset_settings(cell=0.3)
    bpy.ops.object.mode_set(mode='EDIT')
    reset_settings(cell=0.3, selection='ALL')
    r = bpy.ops.agr.uv_organic_unwrap_selected()
    bpy.ops.object.mode_set(mode='OBJECT')
    check("edit mode handles it", r == {'FINISHED'})
    check("mesh got cut in edit mode", len(obj.data.polygons) > before,
          f"{before} -> {len(obj.data.polygons)}")
    keys = obj.data.shape_keys
    check("shape keys survived the cut", keys is not None and len(keys.key_blocks) == 2)
    check("shape key data matches the new vertex count",
          keys is not None
          and len(keys.key_blocks[0].data) == len(obj.data.vertices),
          f"{len(keys.key_blocks[0].data) if keys else -1} vs {len(obj.data.vertices)}")

    print("=" * 60)
    print("TEST 10: margin, atlas flag, degenerate face")
    reset_scene()
    me = bpy.data.meshes.new("FlatMesh")
    me.from_pydata([(0, 0, 0), (0.2, 0, 0), (0.2, 0.2, 0), (0, 0.2, 0)],
                   [], [(0, 1, 2, 3)])
    me.validate()
    flat = bpy.data.objects.new("Flat", me)
    bpy.context.scene.collection.objects.link(flat)
    flat['agr_atlas_applied'] = 'M_FakeAtlas'
    reset_settings(cell=0.25, margin=0.8, cut=False)
    select_only(flat)
    r = bpy.ops.agr.uv_organic_unwrap()
    check("flat quad FINISHED", r == {'FINISHED'})
    # 0.2 m piece, 1 square = 0.25 * slack m -> the piece covers 0.2/tile of
    # it, centred, then x0.8 margin around (0.5, 0.5)
    tile = 0.25 * uvmod._ORGANIC_SCALE_SLACK
    half = 0.5 * (0.2 / tile) * 0.8
    bb = uv_bounds(flat)
    check("SCALE + margin lands exactly where the maths says",
          all(abs(a - b) < 1e-5 for a, b in
              zip(bb, (0.5 - half, 0.5 - half, 0.5 + half, 0.5 + half))),
          f"{tuple(round(c, 5) for c in bb)} expected half={half:.5f}")
    check("full rewrite clears agr_atlas_applied",
          flat.get('agr_atlas_applied') is None)

    reset_scene()
    me = bpy.data.meshes.new("DegMesh")
    # a healthy quad + a zero-area triangle (two coincident verts)
    me.from_pydata([(0, 0, 0), (0.2, 0, 0), (0.2, 0.2, 0), (0, 0.2, 0),
                    (0.5, 0, 0), (0.6, 0, 0), (0.6, 0, 0)],
                   [], [(0, 1, 2, 3), (4, 5, 6)])
    me.validate()
    deg = bpy.data.objects.new("Deg", me)
    bpy.context.scene.collection.objects.link(deg)
    reset_settings(cell=0.25, cut=False)
    select_only(deg)
    r = bpy.ops.agr.uv_organic_unwrap()
    check("degenerate face does not crash the operator", r == {'FINISHED'})
    check("healthy face still unwrapped",
          uvs_in_unit(deg) and len(deg.data.polygons) == 2)

    print("=" * 60)
    print("TEST 11: linked duplicates are cut ONCE")
    reset_scene()
    ref = suzanne("SuzRef", 1)
    reset_settings(cell=0.25)
    select_only(ref)
    bpy.ops.agr.uv_organic_unwrap()
    expected = len(ref.data.polygons)
    reset_scene()
    base = suzanne("SuzShared", 1)
    twin = bpy.data.objects.new("SuzTwin", base.data)   # SAME mesh datablock
    bpy.context.scene.collection.objects.link(twin)
    twin.location = (5, 0, 0)
    reset_settings(cell=0.25)
    select_only(base, twin)
    r = bpy.ops.agr.uv_organic_unwrap()
    check("linked pair FINISHED", r == {'FINISHED'})
    check("shared mesh cut exactly once",
          len(base.data.polygons) == expected,
          f"{len(base.data.polygons)} vs {expected}")

    print("=" * 60)
    print("TEST 12: internals — cone invariant, voxel locality, sliver merge")
    reset_scene()
    obj = suzanne("SuzInt", 2)
    cell, half = 0.25, radians(30.0)
    cos_half = cos(half)
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.index_update()
    faces = list(bm.faces)
    patches, cents, normals, areas = uvmod._organic_build_patches(
        obj.matrix_world, faces, cell, cos_half)
    check("patches cover every face exactly once",
          sum(len(p) for p in patches) == len(faces)
          and len({f for p in patches for f in p}) == len(faces),
          f"{sum(len(p) for p in patches)} vs {len(faces)}")
    bad_cone = 0
    for p in patches:
        mean = Vector((0.0, 0.0, 0.0))
        for f in p:
            mean += normals[f]
        for f in p:
            n = normals[f]
            if n.length > 1e-9 and mean.length > 1e-9 and \
                    n.dot(mean) < cos_half * n.length * mean.length:
                bad_cone += 1
    check("every face lies inside the cone of ITS piece mean (30 deg)",
          bad_cone == 0, f"violations={bad_cone}")
    bad_voxel = 0
    for p in patches:
        keys = {(floor(cents[f].x / cell), floor(cents[f].y / cell),
                 floor(cents[f].z / cell)) for f in p}
        if len(keys) != 1:
            bad_voxel += 1
    check("every piece sits in ONE voxel before merging", bad_voxel == 0,
          f"multi-voxel pieces={bad_voxel}")
    conn_bad = 0
    for p in patches:
        pset = set(p)
        seen = {p[0]}
        stack = [p[0]]
        while stack:
            f = stack.pop()
            for e in f.edges:
                for g in e.link_faces:
                    if g in pset and g not in seen:
                        seen.add(g)
                        stack.append(g)
        if len(seen) != len(p):
            conn_bad += 1
    check("every piece is edge-connected", conn_bad == 0,
          f"disconnected pieces={conn_bad}")

    merged_patches, merged_n = uvmod._organic_merge_slivers(
        patches, normals, areas, cell, 0.15, cos_half)
    check("sliver merge absorbs pieces", merged_n > 0, f"merged={merged_n}")
    check("merge lowers the piece count by exactly that much",
          len(merged_patches) == len(patches) - merged_n,
          f"{len(patches)} -> {len(merged_patches)}, merged={merged_n}")
    check("merge keeps every face",
          sum(len(p) for p in merged_patches) == len(faces))
    none_merged, zero = uvmod._organic_merge_slivers(
        patches, normals, areas, cell, 0.0, cos_half)
    check("merge=0 is a no-op", zero == 0 and len(none_merged) == len(patches))

    # A piece that already absorbed a sliver is no longer a sliver.  Without
    # that re-check the merge CHAINS (A into B, then the fattened B into C),
    # and the ascending-area order makes chaining the rule rather than the
    # exception - six 0.9x neighbours collapsed into a single 5.4x piece,
    # which then trips the SCALE rescale and shows the texture coarser than
    # everything around it.  A pure chain of slivers must stay under
    # limit + one sliver, i.e. 2x limit.
    limit = 0.15 * cell * cell
    src_area = {}
    for p in patches:
        a = sum(areas[f] for f in p)
        for f in p:
            src_area[f] = a
    chains = [sum(areas[f] for f in p) for p in merged_patches
              if len(p) > 1 and all(src_area[f] < limit for f in p)]
    check("a chain of slivers cannot grow past 2x the area limit",
          all(a <= 2.0 * limit + 1e-12 for a in chains),
          f"limit={limit:.4g}, worst chain={max(chains, default=0.0):.4g}, "
          f"chains={len(chains)}")
    bm.free()

    print("=" * 60)
    print("TEST 13: cone angle actually controls the piece count")
    reset_scene()
    counts = {}
    for angle in (15.0, 75.0):
        obj = suzanne(f"SuzAng{int(angle)}", 1)
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        bm.faces.index_update()
        patches, _c, _n, _a = uvmod._organic_build_patches(
            obj.matrix_world, list(bm.faces), 0.5, cos(radians(angle)))
        counts[angle] = len(patches)
        bm.free()
    check("a tighter cone yields more pieces", counts[15.0] > counts[75.0],
          f"15deg={counts[15.0]} 75deg={counts[75.0]}")

    print("=" * 60)
    print("TEST 14: FIT fills the square, poll refuses the wrong mode")
    reset_scene()
    obj = suzanne("SuzFit", 1)
    reset_settings(cell=0.25, fill='FIT', margin=1.0)
    select_only(obj)
    r = bpy.ops.agr.uv_organic_unwrap()
    bb = uv_bounds(obj)
    check("FIT FINISHED", r == {'FINISHED'})
    check("FIT reaches both square borders",
          bb[0] < 1e-4 and bb[1] < 1e-4 and bb[2] > 1.0 - 1e-4
          and bb[3] > 1.0 - 1e-4, str(tuple(round(c, 5) for c in bb)))
    reset_scene()
    empty = bpy.data.objects.new("Empty", None)
    bpy.context.scene.collection.objects.link(empty)
    select_only(empty)
    check("poll refuses a non-mesh selection",
          not bpy.ops.agr.uv_organic_unwrap.poll())

    print("=" * 60)
    print("TEST 15: every prop name the panels draw really exists")
    import re
    src = io.open(uvmod.__file__, encoding='utf-8').read()         if hasattr(uvmod, "__file__") else ""
    known = {pr.identifier for pr in uvmod.AGR_UVGridSettings.bl_rna.properties}
    # names drawn from the settings PropertyGroup (row/col/sub.prop(s, "..."))
    drawn = set(re.findall(r'\.prop\(\s*s\s*,\s*"([a-z_]+)"', src))
    missing = sorted(n for n in drawn if n not in known)
    check("panels reference only existing settings props", not missing,
          f"drawn={len(drawn)} missing={missing}")
    check("the organic props are all wired",
          {"organic_cell", "organic_cut", "organic_angle", "organic_merge",
           "organic_margin", "organic_fill", "organic_align"} <= known)
    check("TOPZ is the third grid source",
          [i.identifier for i in
           uvmod.AGR_UVGridSettings.bl_rna.properties["grid_source"].enum_items]
          == ['EDGES', 'WORLD', 'TOPZ'])
    # Blender names the RNA types after bl_idname, not the Python class
    check("both organic operators + the panel are registered",
          hasattr(bpy.types, "AGR_OT_uv_organic_unwrap")
          and hasattr(bpy.types, "AGR_OT_uv_organic_unwrap_selected")
          and hasattr(bpy.types, "AGR_PT_uv_organic_panel"))

    # -------------------------------------------------------------------
    print("\n=== TEST 16: a cut that unwraps nothing must stay UNDOABLE ===")
    # bmesh.from_edit_mesh hands out THE edit BMesh, so the bisect is in the
    # mesh the moment it runs.  Returning CANCELLED then pushes no undo step
    # and the cut is welded into the PREVIOUS undo entry - Ctrl+Z can never
    # take it back.  The Object-mode path has no such problem: its BMesh is
    # a copy that is only written when something was actually unwrapped.
    reset_scene()
    obj = suzanne("UndoCut", levels=0)
    reset_settings(cell=0.5, cut=True, selection='ALL')
    select_only(obj)
    faces_before = len(obj.data.polygons)

    # force "nothing unwrapped" while leaving the cut in place: every piece
    # reports as degenerate, exactly like a UDIM object parked outside any
    # valid tile would
    real_patch_uvs = uvmod._organic_patch_uvs
    uvmod._organic_patch_uvs = lambda *a, **k: None
    try:
        bpy.ops.object.mode_set(mode='EDIT')
        bm = bmesh.from_edit_mesh(obj.data)
        for f in bm.faces:
            f.select = True
        bmesh.update_edit_mesh(obj.data)
        r_edit = bpy.ops.agr.uv_organic_unwrap_selected()
        bpy.ops.object.mode_set(mode='OBJECT')
    finally:
        uvmod._organic_patch_uvs = real_patch_uvs

    faces_after = len(obj.data.polygons)
    check("undo-cut: the mesh really was cut", faces_after > faces_before,
          f"{faces_before} -> {faces_after}")
    check("undo-cut: edit-mode op FINISHED (so an undo step exists)",
          r_edit == {'FINISHED'}, str(r_edit))

    # and the Object-mode twin, where cancelling IS correct: its BMesh is a
    # throw-away copy, so nothing reached the mesh and no undo step is owed
    reset_scene()
    obj2 = suzanne("UndoCutObj", levels=0)
    reset_settings(cell=0.5, cut=True, selection='ALL')
    select_only(obj2)
    before2 = len(obj2.data.polygons)
    uvmod._organic_patch_uvs = lambda *a, **k: None
    try:
        r_obj = expect_cancel(lambda: bpy.ops.agr.uv_organic_unwrap())
    finally:
        uvmod._organic_patch_uvs = real_patch_uvs
    check("undo-cut: object-mode op still CANCELS", r_obj)
    check("undo-cut: object-mode left the mesh untouched",
          len(obj2.data.polygons) == before2,
          f"{before2} -> {len(obj2.data.polygons)}")

    # the flag means "this BMesh was written to", nothing looser
    st = uvmod._organic_stats()
    check("undo-cut: a fresh stats dict is not dirty", st['dirty'] is False)

    # -------------------------------------------------------------------
    print("\n=== TEST 17: skip reasons reach the user, level is honest ===")
    # (a) a fully parked UDIM object used to fail with the bare "Развернуть
    # нечего" - the accumulated out_of_tiles counter IS the answer and must
    # be in the message; (b) faces skipped in an otherwise successful run
    # keep their OLD UVs, so the status line must be WARNING, never a green
    # INFO over hundreds of untextured polygons.
    reset_scene()
    wm = bpy.context.window_manager
    img = bpy.data.images.new("__udim_src2", width=64, height=64)
    path2 = os.path.join(bpy.app.tempdir, "T_organic2.1001.png")
    img.filepath_raw = path2
    img.file_format = 'PNG'
    img.save()
    bpy.data.images.remove(img)
    tiled2 = bpy.data.images.load(path2)
    tiled2.source = 'TILED'
    mat2 = bpy.data.materials.new("M_Udim2")
    mat2.use_nodes = True
    mat2.node_tree.nodes.new('ShaderNodeTexImage').image = tiled2
    obj = suzanne("SuzParked", 0)
    obj.data.materials.append(mat2)
    obj.data.uv_layers.new(name="UVMap")
    for d in obj.data.uv_layers.active.data:
        d.uv = (-3.0, -3.0)               # parked outside every valid tile
    invalidate_udim_cache()
    reset_settings(cell=0.25, cut=False)
    select_only(obj)
    check("parked UDIM object CANCELLED",
          expect_cancel(bpy.ops.agr.uv_organic_unwrap))
    check("the reason (out_of_tiles) is in the message",
          "вне валидной UDIM-зоны" in wm.agr_last_status, wm.agr_last_status)

    reset_scene()
    obj2 = suzanne("SuzLevel", 0)
    reset_settings(cell=0.5, cut=False, selection='ALL')
    select_only(obj2)
    real_uvs = uvmod._organic_patch_uvs
    calls = {"n": 0}

    def _first_none(*a, **k):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_uvs(*a, **k)

    uvmod._organic_patch_uvs = _first_none
    try:
        r = bpy.ops.agr.uv_organic_unwrap()
    finally:
        uvmod._organic_patch_uvs = real_uvs
    check("mixed run FINISHED", r == {'FINISHED'}, str(r))
    check("skipped faces raise the level to WARNING",
          wm.agr_last_status_level == 'WARNING', wm.agr_last_status_level)
    check("...and are named in the message",
          "вырожденных" in wm.agr_last_status, wm.agr_last_status)

    # -------------------------------------------------------------------
    print("\n=== TEST 18: edit mode - crash on object 2 must not eat object 1's undo ===")
    # The Object path always isolated objects with a per-object try; the
    # Edit path did not, so an exception on the second object ended the
    # operator without FINISHED - no undo step, and the LIVE edit-BMesh cut
    # of the first object welded into the previous undo entry (reproduced).
    reset_scene()
    o1 = suzanne("IsoA", 0)
    o2 = suzanne("IsoB", 0)
    o2.location.x = 5.0
    reset_settings(cell=0.5, cut=True, selection='ALL')
    select_only(o1, o2)
    f1_before = len(o1.data.polygons)
    real_apply = uvmod._organic_apply

    def _boom(obj, *a, **k):
        if obj.name == "IsoB":
            raise RuntimeError("boom")
        return real_apply(obj, *a, **k)

    uvmod._organic_apply = _boom
    try:
        bpy.ops.object.mode_set(mode='EDIT')
        for ob in (o1, o2):
            bm = bmesh.from_edit_mesh(ob.data)
            for f in bm.faces:
                f.select = True
            bmesh.update_edit_mesh(ob.data)
        r = bpy.ops.agr.uv_organic_unwrap_selected()
        bpy.ops.object.mode_set(mode='OBJECT')
    finally:
        uvmod._organic_apply = real_apply
    check("isolation: operator FINISHED despite the crash (undo step exists)",
          r == {'FINISHED'}, str(r))
    check("isolation: the failure is reported",
          "сбой" in wm.agr_last_status, wm.agr_last_status)
    check("isolation: object 1 was still cut",
          len(o1.data.polygons) > f1_before,
          f"{f1_before} -> {len(o1.data.polygons)}")

except Exception:
    traceback.print_exc()
    FAILS.append("EXCEPTION")

print("=" * 60)
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILED -> " + "; ".join(FAILS))
    sys.exit(1)
print("RESULT: ALL TESTS PASSED")
