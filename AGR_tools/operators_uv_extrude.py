"""AGR UV — «Экструд с развёрткой»: extrude whose side faces keep the texel
of the face they grow from.

Stock extrude leaves the new side faces with UVs collapsed onto the hinge
edge, and "Correct Face Attributes" cannot fix that: it re-interpolates UVs
INSIDE the plane of the neighbouring faces, so the part of the move along
their normal is lost — a 90° extrusion (window reveal, parapet, cornice)
stays a zero-width line in UV.

Here every side face is UNFOLDED around its hinge edge into the plane of
the face it grew from (the wall; the moved cap only when the hinge has no
wall — a region on the mesh border) and mapped through that face's own UV
gradient:

  * the hinge corners copy the wall UVs exactly — no seam at the hinge;
  * the UV step along the hinge is exact, the step across it is the wall's
    least-squares gradient, so anisotropic, rotated, sheared and mirrored
    layouts continue correctly (scaling by the hinge length alone silently
    assumes a uniform, unmirrored map);
  * the unfold is an isometry, so the texel density equals the wall's for
    ANY extrusion angle, direction, shear or scale.

«Держать в квадрате» (on confirm only — it changes topology, the live
preview stays a pure unfold): every side face is bisected along each UV
tile border its unfolded UVs cross — u = k / v = k are straight lines in 3D
because the map is affine on a face — and each piece moves by whole tiles
into the wall's tile.  The tiles follow one another like on a tiled
texture, and the UDIM tile / atlas 0..1 contract holds.

Free corners shared by side faces of one straight wall are welded
bit-exactly; where the hinge chain bends the unfold honestly opens a seam —
a strip around a corner cannot be continuous with the wall, continuous with
itself AND undistorted at the same time.

All geometry is measured in WORLD space (object scale counts toward the
texel) from vectors RELATIVE to the hinge vertex, so city-scale offsets do
not eat float32 precision.

Entry points
  * Ctrl+Alt+E in mesh Edit Mode, the bottom of the Alt+E extrude menu and
    the «Экструд с развёрткой» sub-panel → `agr.extrude_uv`: a thin wrapper
    that starts the `agr.extrude_uv_move` macro exactly like the stock E
    wrapper (faces: along the normal; edges: free).  Macro = extrude_region
    → arm → translate → unfold: ONE undo step, every step in the redo panel.
    While the translate is modal a depsgraph handler re-unfolds the armed
    faces on every update, so the texture follows the mouse.
  * `agr.uv_unfold_selected` — repair for faces extruded earlier with the
    stock E: the SELECTED faces are unfolded from their unselected
    neighbours, chains propagate outward from the biggest surface.
"""

import heapq
from itertools import count
from math import floor
from time import perf_counter

import bpy
import bmesh
from bpy.app.handlers import persistent
from bpy.props import BoolProperty
from bpy.types import Macro, Operator, Panel
from mathutils import Vector

from .log import agr_report, drop_stale_handlers, unregister_classes
from .operators_uv import (
    _AGR_UVGridPollMixin,
    _edit_mesh_objects,
    _interior_lines,
    _world_normal,
)

# A hinge shorter than this (world metres) carries no direction
_EPS_LEN = 1e-9
# Hinge UV length below this: the reference has no texel information
_EPS_UV = 1e-9
# |det(g_a, g_b)| / |g_a|^2 below this: the reference UV is collapsed ACROSS
# the hinge (a face broken by an earlier stock extrude) — unusable
_DET_REL = 1e-6
# Free corners closer than this fraction of the face's lateral UV length snap
# onto the corner already written for that vertex: float noise of a straight
# wall welds, a real bend opens a far wider gap and stays a seam
_WELD_REL = 0.01
_WELD_ABS = 1e-6
# UV distance stepped from the hinge INTO the wall to pick its tile: an AGR
# grid face spans exactly 0..1, so its hinge sits ON a tile border
_TILE_PROBE = 1e-4
# Tile borders per side face above this are not cut — a 64-tile-long
# extrusion is a typo, and every border is one more chained bisect
_MAX_TILE_LINES = 64
# bisect_plane weld distance in WORLD metres (divided by the object scale,
# same convention as the grid cut in operators_uv)
_CUT_WELD = 1e-5

_KEEP_TILE_DESC = ("После подтверждения разрезать выдавленные грани по границам "
                   "UV-квадратов текстуры и сдвинуть каждый кусок в квадрат грани-"
                   "основания: квадраты идут один за другим, как на тайловой "
                   "текстуре, а UDIM-тайл и диапазон 0..1 атласа сохраняются")


# ============================================================
# Unfold math
# ============================================================

def _hinge_side(verts, va, vb):
    """+1 when the corner sequence `verts` walks va→vb, -1 when it walks
    vb→va, 0 when va–vb is not one of its sides."""
    n = len(verts)
    for i, v in enumerate(verts):
        if v == va:
            if verts[(i + 1) % n] == vb:
                return 1
            if verts[i - 1] == vb:
                return -1
            return 0
    return 0


def _interior_dir(side, pts, a):
    """Unit vector ⟂ `a` in the plane of the polygon `pts`, pointing from the
    hinge INTO it (`side` from _hinge_side); None while it has no area yet.

    Winding, not "towards the centroid": a polygon's interior lies LEFT of
    each of its directed sides seen from its normal — true for a concave
    wall ngon too, where the centroid can sit across the hinge."""
    if not side:
        return None
    n = _world_normal(pts)
    if n.length_squared < 1e-24:
        return None
    d = n.normalized().cross(a)
    if d.length_squared < 1e-12:
        return None
    return d.normalized() * side


def _reference_frame(lin, ref, va, vb, uv):
    """Linear UV model of `ref` around its side va–vb, or None if unusable.

        UV(va + x·a + w·b) ≈ uva + x·g_a + w·g_b

    `a` runs along the hinge, `b` points into `ref`.  g_a is EXACT (the hinge
    corners must stay welded to the reference); g_b is the least-squares
    gradient across the hinge over ref's other corners — exact for any
    affine layout, a best fit for a trapezoid mapped onto a rectangle.

    Points are world vectors RELATIVE to va through the 3x3 part only: a
    city-scale translation would cost millimetres of float32 precision
    before the subtraction.

    Returns (a, b, uva, uvb, g_a, g_b, tile); `tile` is the reference's UV
    tile probed a hair inside it next to the hinge (see _cut_to_tiles)."""
    loops = ref.loops[:]
    verts = [loop.vert for loop in loops]
    ia = ib = -1
    for i, v in enumerate(verts):
        if v == va:
            ia = i
        elif v == vb:
            ib = i
    if ia < 0 or ib < 0:
        return None
    o = va.co
    pts = [lin @ (v.co - o) for v in verts]
    hinge = pts[ib]
    length = hinge.length
    if length < _EPS_LEN:
        return None
    a = hinge / length
    b = _interior_dir(_hinge_side(verts, va, vb), pts, a)
    if b is None:
        return None
    uva = loops[ia][uv].uv.copy()
    uvb = loops[ib][uv].uv.copy()
    if (uvb - uva).length < _EPS_UV:
        return None
    g_a = (uvb - uva) / length
    num = Vector((0.0, 0.0))
    den = 0.0
    for i, loop in enumerate(loops):
        if i == ia or i == ib:
            continue
        w = pts[i].dot(b)
        num += (loop[uv].uv - uva - g_a * pts[i].dot(a)) * w
        den += w * w
    if den <= (length * 1e-6) ** 2:
        return None  # sliver: every other corner sits on the hinge line
    g_b = num / den
    det = g_a.x * g_b.y - g_a.y * g_b.x
    if abs(det) < _DET_REL * g_a.length_squared:
        return None
    # an AGR grid face spans exactly 0..1, so its hinge sits ON a tile
    # border and its centroid may belong to the far end of a long wall —
    # step a hair from the hinge midpoint INTO the reference instead
    probe = (uva + uvb) * 0.5 + g_b.normalized() * _TILE_PROBE
    return a, b, uva, uvb, g_a, g_b, (floor(probe.x), floor(probe.y))


def _face_corners(lin, face, va):
    """(loops, verts, world vectors from va) of `face` in loop order."""
    loops = face.loops[:]
    verts = [loop.vert for loop in loops]
    o = va.co
    return loops, verts, [lin @ (v.co - o) for v in verts]


def _unfold_uvs(lin, face, va, vb, frame):
    """The face rotated about the hinge va–vb onto the reference plane, on
    the side AWAY from the reference, through the reference UV model.

    Returns (loops, verts, uvs, free): `free` marks the unfolded corners —
    the hinge corners copy the reference UVs exactly."""
    a, _b, uva, uvb, g_a, g_b, _tile = frame
    loops, verts, pts = _face_corners(lin, face, va)
    b_t = _interior_dir(_hinge_side(verts, va, vb), pts, a)
    uvs = []
    free = []
    for v, d in zip(verts, pts):
        if v == va:
            uvs.append(uva.copy())
            free.append(False)
        elif v == vb:
            uvs.append(uvb.copy())
            free.append(False)
        else:
            x = d.dot(a)
            q = d - a * x
            # distance from the hinge measured inside the face's own plane
            # (a warped quad drops its out-of-plane part); a face without
            # area yet (live preview at zero offset) has no plane at all
            y = q.dot(b_t) if b_t is not None else q.length
            uvs.append(uva + g_a * x - g_b * y)
            free.append(True)
    return loops, verts, uvs, free


def _unfold_cost(lin, face, va, vb, frame):
    """0 when `face` already continues the reference flat (180° dihedral),
    2 when it is folded back onto it.  Picks the natural one of several walls
    sharing a hinge (a fin extruded from an interior edge unfolds over the
    side it leans to)."""
    _loops, verts, pts = _face_corners(lin, face, va)
    b_t = _interior_dir(_hinge_side(verts, va, vb), pts, frame[0])
    return 0.0 if b_t is None else 1.0 + b_t.dot(frame[1])


def _write_face(uv, loops, verts, uvs, free, corners):
    """Write `uvs` into `loops`.  A free (unfolded) corner snaps onto a free
    corner already written for the same vertex when it is within the weld
    tolerance: side faces of a straight wall then share BIT-IDENTICAL UVs —
    exporters split a UV vertex on any difference at all."""
    for loop, vert, val, is_free in zip(loops, verts, uvs, free):
        if is_free:
            known = corners.get(vert)
            if known is None:
                corners[vert] = [val]
            else:
                # tolerance scales with this corner's lateral reach in UV
                reach = min((val - p).length for p, f in zip(uvs, free) if not f)
                tol = max(_WELD_ABS, _WELD_REL * reach)
                for prev in known:
                    if (prev - val).length <= tol:
                        val = prev
                        break
                else:
                    known.append(val)
        loop[uv].uv = val


# ============================================================
# «Держать в квадрате»: cut along the UV tile borders, shift the pieces
# ============================================================

def _uv_affine(face, uv):
    """Least-squares affine map LOCAL position → UV over the corners of
    `face`: (center, uv_center, (grad_u, grad_v)) with both gradients lying
    in the face plane, or None for a face without area.  Exact on an
    unfolded face (the unfold is affine), so every UV iso-line on it is a
    straight line — one bisect plane."""
    loops = face.loops[:]
    pts = [loop.vert.co.copy() for loop in loops]
    n = _world_normal(pts)  # Newell works in any space
    if n.length_squared < 1e-24:
        return None
    n.normalize()
    k = len(pts)
    c = sum(pts, Vector()) / k
    e1 = max((p - c for p in pts), key=lambda d: d.length_squared)
    e1 = e1 - n * e1.dot(n)
    if e1.length_squared < 1e-24:
        return None
    e1.normalize()
    e2 = n.cross(e1)
    uvs = [loop[uv].uv.copy() for loop in loops]
    uc = sum(uvs, Vector((0.0, 0.0))) / k
    sss = stt = sst = 0.0
    rs = Vector((0.0, 0.0))
    rt = Vector((0.0, 0.0))
    for p, w in zip(pts, uvs):
        d = p - c
        s = d.dot(e1)
        t = d.dot(e2)
        r = w - uc
        sss += s * s
        stt += t * t
        sst += s * t
        rs += r * s
        rt += r * t
    det = sss * stt - sst * sst
    if det <= 1e-12 * (sss + stt) ** 2:
        return None
    alpha = (rs * stt - rt * sst) / det
    beta = (rt * sss - rs * sst) / det
    return c, uc, (e1 * alpha.x + e2 * beta.x, e1 * alpha.y + e2 * beta.y)


def _reselect_split_verts(res):
    """bisect_plane leaves a vertex it inserts into a SELECTED edge
    unselected (and in vertex select mode the two halves too) — the cap /
    top edge must stay selected for the next extrusion, so a split between
    two selected corners is selected again."""
    cut = set(res['geom_cut'])
    for v in res['geom_cut']:
        if not isinstance(v, bmesh.types.BMVert) or v.select:
            continue
        halves = [e for e in v.link_edges if e not in cut]
        if len(halves) == 2 and all(e.other_vert(v).select for e in halves):
            v.select = True
            for e in halves:
                e.select = True


def _shift_into_tile(face, uv, tile):
    """Move `face` by whole tiles so its UV centroid lands in `tile`."""
    loops = face.loops[:]
    n = len(loops)
    du = tile[0] - floor(sum(loop[uv].uv.x for loop in loops) / n)
    dv = tile[1] - floor(sum(loop[uv].uv.y for loop in loops) / n)
    if not du and not dv:
        return False
    for loop in loops:
        w = loop[uv].uv
        loop[uv].uv = (w.x + du, w.y + dv)
    return True


def _cut_to_tiles(obj, bm, uv, jobs):
    """Bisect every (face, tile) of `jobs` along the UV tile borders it
    crosses, then move every piece by whole tiles into `tile`.

    A border crossing an edge shared with a neighbouring side face splits it
    for both: the neighbour's own cut later passes through that vertex (the
    weld distance reuses it), so the strip stays watertight.  Border lines
    within _interior_lines' epsilon of the face's own UV extent are not
    cut — no slivers.  Returns counts."""
    scale = max(abs(s) for s in obj.matrix_world.to_scale())
    dist = _CUT_WELD / max(scale, 1e-9)
    stats = {'cut': 0, 'pieces': 0, 'shifted': 0, 'too_many': 0}
    for face, tile in jobs:
        if not face.is_valid:
            continue
        planes = []
        fit = _uv_affine(face, uv)
        if fit is not None:
            c, uc, grads = fit
            uvs = [loop[uv].uv.copy() for loop in face.loops]
            for axis in (0, 1):
                g = grads[axis]
                g2 = g.length_squared
                if g2 < 1e-24:
                    continue
                lo = min(w[axis] for w in uvs)
                hi = max(w[axis] for w in uvs)
                for k in _interior_lines(lo, hi):
                    planes.append((c + g * ((k - uc[axis]) / g2), g))
        if len(planes) > _MAX_TILE_LINES:
            stats['too_many'] += 1
            planes = []
        pieces = [face]
        if planes:
            geom = [face, *face.edges, *face.verts]
            for co, no in planes:
                res = bmesh.ops.bisect_plane(bm, geom=geom, dist=dist,
                                             plane_co=co, plane_no=no)
                # survivors + everything new: chaining keeps every next
                # border cutting the pieces of the previous ones
                geom = res['geom']
                _reselect_split_verts(res)
            pieces = [g for g in geom if isinstance(g, bmesh.types.BMFace) and g.is_valid]
            stats['cut'] += 1
        stats['pieces'] += len(pieces)
        for piece in pieces:
            if _shift_into_tile(piece, uv, tile):
                stats['shifted'] += 1
    return stats


# ============================================================
# Extrusion side faces (macro + live preview)
# ============================================================

def _extrusion_targets(bm):
    """Side faces of the extrusion that just ran: an UNselected quad hanging
    off a SELECTED edge whose opposite edge is fully unselected.

    Holds in every select mode right after extrude_region (+ translate): the
    new geometry is selected, the source boundary is not, and interior cap
    edges have no unselected face.  Returns [(face, hinge, top)]."""
    items = []
    seen = set()
    for edge in bm.edges:
        if not edge.select:
            continue
        for face in edge.link_faces:
            if face.select or face in seen or len(face.loops) != 4:
                continue
            for loop in face.loops:
                if loop.edge == edge:
                    break
            else:
                continue
            hinge = loop.link_loop_next.link_loop_next.edge
            if hinge.verts[0].select or hinge.verts[1].select:
                continue
            seen.add(face)
            items.append((face, hinge, edge))
    return items


def _plan_extrusion(lin, items, uv):
    """Resolve the references of every side face: [(face, va, vb, kind,
    frames)], kind None when nothing usable exists.

    WALL — faces across the hinge (the surface the extrusion grew from);
    CAP — faces across the top edge (the moved region), only when the hinge
    has no usable wall: a face region on the mesh border loses its original
    faces, so its border hinges have no neighbour at all.  Several walls on
    one hinge (a fin on an interior edge) are all kept — the cheapest unfold
    is picked at apply time, because it depends on where the face is now.

    Frames do not depend on the side face, so a plan stays valid while the
    translate runs: the walls do not move and the cap moves RIGIDLY (frames
    are relative to the hinge vertex) — that is what lets the live preview
    resolve once and only apply per mouse move."""
    skip = {face for face, _hinge, _top in items}
    plans = []
    for face, hinge, top in items:
        for kind, edge in (('WALL', hinge), ('CAP', top)):
            va, vb = edge.verts
            frames = []
            for ref in edge.link_faces:
                if ref != face and ref not in skip:
                    frame = _reference_frame(lin, ref, va, vb, uv)
                    if frame is not None:
                        frames.append(frame)
            if frames:
                plans.append((face, va, vb, kind, frames))
                break
        else:
            plans.append((face, None, None, None, None))
    return plans


def _apply_plans(lin, uv, plans):
    """Unfold every planned face (continuous UVs, no tile handling).
    Returns (counts, [(face, reference tile)]) — the jobs of _cut_to_tiles."""
    corners = {}
    stats = {'WALL': 0, 'CAP': 0, 'no_ref': 0}
    jobs = []
    for face, va, vb, kind, frames in plans:
        if kind is None:
            stats['no_ref'] += 1
            continue
        if len(frames) == 1:
            frame = frames[0]
        else:
            frame = min(frames, key=lambda fr: _unfold_cost(lin, face, va, vb, fr))
        loops, verts, uvs, free = _unfold_uvs(lin, face, va, vb, frame)
        _write_face(uv, loops, verts, uvs, free, corners)
        stats[kind] += 1
        jobs.append((face, frame[6]))
    return stats, jobs


def _unfold_extrusion(obj, bm, uv, items):
    """Unfold every (face, hinge, top) of `items` -> (counts, cut jobs)."""
    lin = obj.matrix_world.to_3x3()
    return _apply_plans(lin, uv, _plan_extrusion(lin, items, uv))


# ============================================================
# Selected faces (repair after a stock extrude)
# ============================================================

def _unfold_selected(obj, bm, uv, targets):
    """Unfold every face of `targets` from its unselected neighbours ->
    (counts, cut jobs).

    Priority propagation ("widest path"): a reference's priority is the area
    of the unselected surface it belongs to, and an unfolded face hands its
    priority on to its selected neighbours.  The wall around a window (one
    big surface) therefore beats the window cap (an island enclosed by the
    selected reveals), and a stepped reveal unfolds ring by ring from the
    wall instead of its inner ring from the cap.  The tile a chain is kept
    in is the tile of the wall it started from — the rings in between are
    still continuous (uncut) while the chain grows."""
    lin = obj.matrix_world.to_3x3()
    tset = set(targets)
    comp = {}
    areas = []

    def surface_area(face):
        cid = comp.get(face)
        if cid is None:
            cid = len(areas)
            comp[face] = cid
            total = 0.0
            stack = [face]
            while stack:
                f = stack.pop()
                total += f.calc_area()
                for edge in f.edges:
                    for g in edge.link_faces:
                        if g not in comp and g not in tset:
                            comp[g] = cid
                            stack.append(g)
            areas.append(total)
        return areas[cid]

    heap = []
    seq = count()

    def offer(face, ref, edge, priority, tile=None):
        va, vb = edge.verts
        frame = _reference_frame(lin, ref, va, vb, uv)
        if frame is not None:
            cost = _unfold_cost(lin, face, va, vb, frame)
            heapq.heappush(heap, (-priority, cost, next(seq), face, va, vb, frame,
                                  frame[6] if tile is None else tile))

    for face in targets:
        for edge in face.edges:
            for ref in edge.link_faces:
                if ref != face and ref not in tset:
                    offer(face, ref, edge, surface_area(ref))

    done = set()
    corners = {}
    jobs = []
    while heap:
        neg_priority, _cost, _n, face, va, vb, frame, tile = heapq.heappop(heap)
        if face in done:
            continue
        loops, verts, uvs, free = _unfold_uvs(lin, face, va, vb, frame)
        _write_face(uv, loops, verts, uvs, free, corners)
        done.add(face)
        jobs.append((face, tile))
        for edge in face.edges:
            for nxt in edge.link_faces:
                if nxt in tset and nxt not in done:
                    offer(nxt, face, edge, -neg_priority, tile)
    return {'done': len(done), 'no_ref': len(targets) - len(done)}, jobs


# ============================================================
# Live preview while the macro's translate is modal
# ============================================================

# obj.session_uid -> {"name", "counts", "items", "plans"} — element INDICES
# only: a BMesh wrapper kept across operators can dangle once the edit-BMesh
# is rebuilt (undo), an index is checked by the counts
_LIVE = {}
# seen: the macro was modal at least once since arming; deadline: arm expiry
# while it was not seen yet; next: perf_counter() before which steps skip
_LIVE_STATE = {"seen": False, "deadline": 0.0, "next": 0.0}
# The translate evaluates the depsgraph while it INITIALISES (snapping,
# evaluated cage) — before its modal handler exists.  Measured in the GUI:
# that call came first and, read as "not modal", killed the preview before
# the first mouse move.  Until the macro was seen modal, only this timeout
# ends the arm.
_LIVE_GRACE = 2.0
# A preview step slower than this (~1300 side faces at ~19 µs each) throttles
# the next ones so the viewport stays responsive; the final unfold still runs
# on confirm
_LIVE_SLOW = 0.025
# Window.modal_operators reports the MACRO for a macro's modal step (the
# handler gets the mother operator) — checked in the GUI with simulated
# events.  Not the translate: an unrelated later G must never match.
_LIVE_MODAL_IDS = frozenset(("AGR_OT_extrude_uv_move", "agr.extrude_uv_move"))


def _macro_modal():
    try:
        for win in bpy.context.window_manager.windows:
            for op in win.modal_operators:
                if op.bl_idname in _LIVE_MODAL_IDS:
                    return True
    except Exception:
        pass
    return False


def _arm_live(context):
    """Remember the fresh side faces and their resolved references by
    element index (topology is frozen while the translate runs; the counts
    guard against anything else)."""
    _LIVE.clear()
    _LIVE_STATE.update(seen=False, deadline=perf_counter() + _LIVE_GRACE, next=0.0)
    for obj in _edit_mesh_objects(context):
        bm = bmesh.from_edit_mesh(obj.data)
        uv = bm.loops.layers.uv.active
        if uv is None:
            continue
        items = _extrusion_targets(bm)
        if not items:
            continue
        bm.verts.index_update()
        bm.edges.index_update()
        bm.faces.index_update()
        plans = _plan_extrusion(obj.matrix_world.to_3x3(), items, uv)
        _LIVE[obj.session_uid] = {
            "name": obj.name,
            "counts": (len(bm.verts), len(bm.edges), len(bm.faces)),
            "items": [(f.index, h.index, t.index) for f, h, t in items],
            # frames are plain data; a CAP frame goes stale only if the user
            # switches the modal to rotate/scale — the confirm re-plans anyway
            "plans": [(f.index, va.index, vb.index, kind, frames) if kind is not None
                      else (f.index, -1, -1, None, None)
                      for f, va, vb, kind, frames in plans],
        }


def _live_items(obj, state):
    """Armed items resolved on the CURRENT edit-BMesh — (bm, items), or
    (None, None) once the topology moved under them (auto-merge, undo)."""
    bm = bmesh.from_edit_mesh(obj.data)
    if (len(bm.verts), len(bm.edges), len(bm.faces)) != state["counts"]:
        return None, None
    bm.faces.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    items = [(bm.faces[fi], bm.edges[hi], bm.edges[ti]) for fi, hi, ti in state["items"]]
    return bm, items


def _live_update(obj, state):
    """Apply the armed plans to the current corner positions (no
    re-planning: measured ~19 µs per side face instead of ~49)."""
    bm = bmesh.from_edit_mesh(obj.data)
    if (len(bm.verts), len(bm.edges), len(bm.faces)) != state["counts"]:
        return False
    uv = bm.loops.layers.uv.active
    if uv is None:
        return False
    bm.verts.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    verts, faces = bm.verts, bm.faces
    plans = [(faces[fi], verts[ai], verts[bi], kind, frames) if kind is not None
             else (faces[fi], None, None, None, None)
             for fi, ai, bi, kind, frames in state["plans"]]
    _apply_plans(obj.matrix_world.to_3x3(), uv, plans)
    return True


@persistent
def _uv_extrude_live_update(_scene, _depsgraph):
    """depsgraph_update_PRE: re-unfold the armed side faces on every step of
    the modal translate.  PRE is the slot between "the transform moved the
    vertices" and "the depsgraph evaluates + the viewport draws" — the UVs
    written here reach the same frame without a tag of our own (a tag from
    POST costs a second evaluation per mouse move).  Fails SAFE: anything
    unexpected disarms; the final unfold still runs as the macro's last
    step."""
    if not _LIVE:
        return
    start = perf_counter()
    if not _macro_modal():
        if _LIVE_STATE["seen"] or start > _LIVE_STATE["deadline"]:
            _LIVE.clear()  # confirmed / cancelled — or never became modal
        return
    _LIVE_STATE["seen"] = True
    if start < _LIVE_STATE["next"]:
        return
    for uid, state in list(_LIVE.items()):
        obj = bpy.data.objects.get(state["name"])
        try:
            ok = (obj is not None and obj.session_uid == uid
                  and obj.type == 'MESH' and obj.mode == 'EDIT'
                  and _live_update(obj, state))
        except Exception:
            ok = False  # a preview glitch must never escape into the depsgraph loop
        if not ok:
            _LIVE.pop(uid, None)
    spent = perf_counter() - start
    # a slow step buys itself a pause of twice its cost: <= 1/3 of the time
    _LIVE_STATE["next"] = start + 3.0 * spent if spent > _LIVE_SLOW else 0.0


@persistent
def _uv_extrude_on_load_pre(_dummy):
    _LIVE.clear()


# ============================================================
# Operators
# ============================================================

def _cut_report(cut_totals):
    parts = []
    if cut_totals['cut']:
        parts.append(f"нарезано по квадратам граней: {cut_totals['cut']} "
                     f"(кусков {cut_totals['pieces']})")
    if cut_totals['shifted']:
        parts.append(f"сдвинуто в квадрат основания: {cut_totals['shifted']}")
    return parts


def _cut_warning(cut_totals):
    if cut_totals['too_many']:
        return [f"не разрезано (больше {_MAX_TILE_LINES} границ квадратов на грань): "
                f"{cut_totals['too_many']}"]
    return []


class AGR_OT_ExtrudeUVArm(Operator):
    """Macro step: remember the fresh side faces for the live preview"""
    bl_idname = "agr.extrude_uv_arm"
    bl_label = "Подготовка развёртки"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        try:
            _arm_live(context)
        except Exception:
            _LIVE.clear()  # the preview is cosmetic — it must never stop the extrude
        # FINISHED regardless: a cancelled step would end the macro here
        return {'FINISHED'}


class AGR_OT_ExtrudeUVUnfold(_AGR_UVGridPollMixin, Operator):
    """Macro step: unfold the side faces of the extrusion that just ran"""
    bl_idname = "agr.extrude_uv_unfold"
    bl_label = "Развёртка от грани"
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}

    keep_tile: BoolProperty(
        name="Держать в квадрате",
        description=_KEEP_TILE_DESC,
        default=True,
    )

    def _execute(self, context):
        armed = dict(_LIVE)
        _LIVE.clear()
        totals = {'WALL': 0, 'CAP': 0, 'no_ref': 0}
        cut_totals = {'cut': 0, 'pieces': 0, 'shifted': 0, 'too_many': 0}
        no_uv = []
        atlas = []
        for obj in _edit_mesh_objects(context):
            bm = bmesh.from_edit_mesh(obj.data)
            items = None
            state = armed.get(obj.session_uid)
            if state is not None and state["name"] == obj.name:
                # the arm's scan, unless the topology moved (auto-merge)
                _bm, items = _live_items(obj, state)
            if items is None:
                items = _extrusion_targets(bm)
            if not items:
                continue
            uv = bm.loops.layers.uv.active
            if uv is None:
                no_uv.append(obj.name)  # never invent a layer: nothing to continue
                continue
            stats, jobs = _unfold_extrusion(obj, bm, uv, items)
            for key in totals:
                totals[key] += stats[key]
            if not jobs:
                continue
            self._mutated = True
            cut_any = False
            if self.keep_tile:
                cut = _cut_to_tiles(obj, bm, uv, jobs)
                for key in cut_totals:
                    cut_totals[key] += cut[key]
                cut_any = bool(cut['cut'])
            bmesh.update_edit_mesh(obj.data, loop_triangles=cut_any, destructive=cut_any)
            if obj.get('agr_atlas_applied'):
                atlas.append(obj.name)

        done = totals['WALL'] + totals['CAP']
        if not done and not totals['no_ref'] and not no_uv:
            return {'FINISHED'}  # vertex extrude: no side faces, nothing to say

        parts = []
        if done:
            parts.append(f"Развёртка продолжена, граней: {done}")
            parts += _cut_report(cut_totals)
            if totals['CAP']:
                parts.append(f"у шарнира нет стены — продолжено от крышки: {totals['CAP']}")
        warn = _cut_warning(cut_totals)
        if totals['no_ref']:
            warn.append(f"без опорной грани с развёрткой: {totals['no_ref']} "
                        f"(ребро без соседей или опора со схлопнутой UV)")
        if no_uv:
            warn.append(f"нет UV-слоя: {', '.join(no_uv)}")
        if atlas:
            warn.append(f"на атласе ({', '.join(atlas)}) проверьте, что новые "
                        f"грани не вылезли за ячейку материала")
        agr_report(self, 'WARNING' if warn else 'INFO', "; ".join(parts + warn))
        return {'FINISHED'}


class AGR_OT_ExtrudeUVMove(Macro):
    """Extrude, move and unfold the UVs of the new side faces"""
    bl_idname = "agr.extrude_uv_move"
    bl_label = "Экструд с развёрткой"
    # INTERNAL: F3 should offer the wrapper below (it sets the stock E
    # constraints), not this raw macro
    bl_options = {'REGISTER', 'UNDO', 'INTERNAL'}


class AGR_OT_ExtrudeUV(Operator):
    """Extrude like E — the new side faces get UVs unfolded from the face they grow from"""
    bl_idname = "agr.extrude_uv"
    bl_label = "Экструд с развёрткой"
    bl_description = ("Экструд как E (грани — по нормали, рёбра — свободно), но новые "
                      "боковые грани сразу получают развёртку, развёрнутую от грани-"
                      "основания с тем же текселем — под любым углом и в любую сторону. "
                      "Горячая клавиша Ctrl+Alt+E")
    # no 'UNDO': the macro pushes the undo step and owns the redo panel —
    # exactly like the stock view3d.edit_mesh_extrude_move_normal

    @classmethod
    def poll(cls, context):
        obj = context.object
        if context.mode != 'EDIT_MESH' or obj is None or obj.type != 'MESH':
            cls.poll_message_set("Работает в режиме редактирования меша")
            return False
        return True

    def invoke(self, context, _event):
        return self.execute(context)

    def execute(self, context):
        from bpy_extras.object_utils import object_report_if_active_shape_key_is_locked

        obj = context.object
        if object_report_if_active_shape_key_is_locked(obj, self):
            return {'CANCELLED'}
        mesh = obj.data
        # the stock E wrapper's translate settings, case for case
        if mesh.total_face_sel:
            move = {"orient_type": 'NORMAL', "constraint_axis": (False, False, True),
                    "release_confirm": False}
        elif mesh.total_edge_sel == 1:
            move = {"constraint_axis": (False, False, False), "release_confirm": False}
        else:
            move = {"release_confirm": False}
        bpy.ops.agr.extrude_uv_move('INVOKE_REGION_WIN', TRANSFORM_OT_translate=move)
        # the macro returns RUNNING_MODAL — ignored like the stock wrapper does
        # (returning it from here would keep this operator alive, #24671)
        return {'FINISHED'}


class AGR_OT_UVUnfoldSelected(_AGR_UVGridPollMixin, Operator):
    """Unfold the selected faces' UVs from their unselected neighbours"""
    bl_idname = "agr.uv_unfold_selected"
    bl_label = "Развернуть от соседей"
    bl_description = ("Выделенные грани (например, откосы, выдавленные обычным E) "
                      "получают развёртку, развёрнутую от соседней невыделенной грани "
                      "с тем же текселем. Ступенчатые откосы продолжаются кольцо за "
                      "кольцом; между стеной и крышкой побеждает большая поверхность")
    bl_options = {'REGISTER', 'UNDO'}

    keep_tile: BoolProperty(
        name="Держать в квадрате",
        description=_KEEP_TILE_DESC,
        default=True,
    )

    def _execute(self, context):
        n_selected = 0
        totals = {'done': 0, 'no_ref': 0}
        cut_totals = {'cut': 0, 'pieces': 0, 'shifted': 0, 'too_many': 0}
        no_uv = []
        for obj in _edit_mesh_objects(context):
            bm = bmesh.from_edit_mesh(obj.data)
            targets = [f for f in bm.faces if f.select]
            if not targets:
                continue
            n_selected += len(targets)
            uv = bm.loops.layers.uv.active
            if uv is None:
                no_uv.append(obj.name)
                continue
            stats, jobs = _unfold_selected(obj, bm, uv, targets)
            for key in totals:
                totals[key] += stats[key]
            if not jobs:
                continue
            self._mutated = True
            cut_any = False
            if self.keep_tile:
                cut = _cut_to_tiles(obj, bm, uv, jobs)
                for key in cut_totals:
                    cut_totals[key] += cut[key]
                cut_any = bool(cut['cut'])
            bmesh.update_edit_mesh(obj.data, loop_triangles=cut_any, destructive=cut_any)

        if not n_selected:
            agr_report(self, 'ERROR', "Выделите грани, которым нужна развёртка (например, откосы)")
            return {'CANCELLED'}
        no_ref_note = (f"без опоры: {totals['no_ref']} — у них нет невыделенного соседа "
                       f"с рабочей развёрткой (выделите сами боковые грани, а не крышку)")
        if not totals['done']:
            msg = [no_ref_note] if totals['no_ref'] else []
            if no_uv:
                msg.append(f"нет UV-слоя: {', '.join(no_uv)}")
            agr_report(self, 'WARNING', "Ничего не развёрнуто: " + "; ".join(msg))
            return {'CANCELLED'}

        parts = [f"Развёрнуто от соседей, граней: {totals['done']}"] + _cut_report(cut_totals)
        warn = _cut_warning(cut_totals)
        if totals['no_ref']:
            warn.append(no_ref_note)
        if no_uv:
            warn.append(f"нет UV-слоя: {', '.join(no_uv)}")
        agr_report(self, 'WARNING' if warn else 'INFO', "; ".join(parts + warn))
        return {'FINISHED'}


class AGR_PT_UVExtrudePanel(Panel):
    """Extrude with UV continuation (sub-panel of AGR UV)"""
    bl_label = "Экструд с развёрткой"
    bl_idname = "AGR_PT_uv_extrude_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_parent_id = "AGR_PT_uv_panel"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.scale_y = 1.3
        # the translate must run in the viewport, not in this sidebar
        col.operator_context = 'INVOKE_REGION_WIN'
        col.operator(AGR_OT_ExtrudeUV.bl_idname, icon='MOD_SOLIDIFY')

        col = layout.column(align=True)
        col.operator(AGR_OT_UVUnfoldSelected.bl_idname, icon='UV_FACESEL')
        col.label(text="Ctrl+Alt+E или внизу меню Alt+E", icon='INFO')
        col.label(text="«Держать в квадрате» — в панели операции", icon='INFO')
        if context.mode != 'EDIT_MESH':
            col.label(text="Работает в Edit Mode", icon='INFO')


# ============================================================
# Registration
# ============================================================

classes = (
    AGR_OT_ExtrudeUVArm,
    AGR_OT_ExtrudeUVUnfold,
    AGR_OT_ExtrudeUVMove,
    AGR_OT_ExtrudeUV,
    AGR_OT_UVUnfoldSelected,
    AGR_PT_UVExtrudePanel,
)

_addon_keymaps = []  # [(keymap, keymap_item)] added by register()


def _draw_extrude_menu(self, _context):
    layout = self.layout
    layout.separator()
    layout.operator_context = 'INVOKE_REGION_WIN'
    layout.operator(AGR_OT_ExtrudeUV.bl_idname, icon='MOD_SOLIDIFY')


def _drop_menu_entries(menu, func_name):
    """Remove every draw function named `func_name` from `menu` — after a dev
    reload the previous module's function object is a different object, so
    Menu.remove() (identity) would leave it drawing a second entry."""
    try:
        funcs = menu._dyn_ui_initialize()
    except Exception:
        return
    for fn in list(funcs):
        if getattr(fn, "__name__", None) == func_name:
            funcs.remove(fn)


def _drop_live_handlers():
    # PRE since the GUI check; the first version sat in POST — a dev reload
    # from it must not leave that copy behind
    drop_stale_handlers(bpy.app.handlers.depsgraph_update_pre, "_uv_extrude_live_update")
    drop_stale_handlers(bpy.app.handlers.depsgraph_update_post, "_uv_extrude_live_update")
    drop_stale_handlers(bpy.app.handlers.load_pre, "_uv_extrude_on_load_pre")


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    # the steps are defined on the freshly registered class every time — a
    # dev reload re-registers it, so the list never doubles
    AGR_OT_ExtrudeUVMove.define("MESH_OT_extrude_region")
    AGR_OT_ExtrudeUVMove.define("AGR_OT_extrude_uv_arm")
    move = AGR_OT_ExtrudeUVMove.define("TRANSFORM_OT_translate")
    # same overrides as the stock MESH_OT_extrude_region_move macro: the
    # walls must not move (they are the unfold reference)
    move.properties.use_proportional_edit = False
    move.properties.mirror = False
    AGR_OT_ExtrudeUVMove.define("AGR_OT_extrude_uv_unfold")

    _drop_live_handlers()
    bpy.app.handlers.depsgraph_update_pre.append(_uv_extrude_live_update)
    bpy.app.handlers.load_pre.append(_uv_extrude_on_load_pre)

    menu = getattr(bpy.types, "VIEW3D_MT_edit_mesh_extrude", None)
    if menu is not None:
        _drop_menu_entries(menu, _draw_extrude_menu.__name__)
        menu.append(_draw_extrude_menu)

    # Ctrl+Alt+E is bound nowhere in the 5.x default keymap
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon if wm is not None else None
    if kc is not None:
        km = kc.keymaps.new(name="Mesh", space_type='EMPTY')
        for kmi in list(km.keymap_items):
            if kmi.idname == AGR_OT_ExtrudeUV.bl_idname:
                km.keymap_items.remove(kmi)  # stale item of an unbalanced dev reload
        kmi = km.keymap_items.new(AGR_OT_ExtrudeUV.bl_idname, 'E', 'PRESS',
                                  ctrl=True, alt=True)
        _addon_keymaps.append((km, kmi))
    print("✅ AGR UV extrude registered (Ctrl+Alt+E, Alt+E menu)")


def unregister():
    for km, kmi in _addon_keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass  # keymap already gone (Blender shutdown order)
    _addon_keymaps.clear()

    menu = getattr(bpy.types, "VIEW3D_MT_edit_mesh_extrude", None)
    if menu is not None:
        _drop_menu_entries(menu, _draw_extrude_menu.__name__)

    _drop_live_handlers()
    _LIVE.clear()

    unregister_classes(classes)  # idempotent: survives a half-registered module
