"""AGR UV — grid-based planar UV mapping driven by a reference grid.

Two grid sources:
  * EDGES — the user picks two edges of ONE grid cell ("Запомнить сетку");
    they define the U/V axes, the cell sizes and the grid origin in WORLD
    space, so the stored grid works across every object in the scene.
  * WORLD — no edges needed: axes are derived from the mean normal of the
    target faces (floor -> U=+X/V=+Y, wall -> V=up/U=along the wall), the
    cell size is typed in manually, an optional angle rotates the grid
    around the normal, and the origin snaps to the selection corner or the
    world origin.

"Развернуть по сетке" maps each target face into the 0..1 UV square of its
own grid cell (faces spanning several cells exceed 0..1 — use "Разрезать по
сетке" first to bisect the mesh along the grid lines).  Auto-orientation
makes the result deterministic: the basis handedness follows the mean face
normal so the texture is never mirrored, and V points "up" (world +Z for
walls, +Y for floors); manual swap/flip toggles apply on top of it.

NOTE: UVs are written into the 0..1 square on purpose — the integer part of
UV is reserved by the UDIM tools (tile number) and the atlas operators
require UVs inside the unit square.

Standalone origin of the algorithm: scripts/mesh_grid_uv_fix.py.
"""

import time
import traceback

import bpy
import bmesh
import gpu
from gpu_extras.batch import batch_for_shader
from math import ceil, floor
from mathutils import Matrix, Vector, geometry

from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    PointerProperty,
)
from bpy.types import Operator, Panel, PropertyGroup

from .log import agr_report, logger
from .operators_udim import object_has_udim

# Safety cap for the cut operator: max grid lines per object (both axes)
_MAX_CUT_LINES = 2048

# Faces whose UVs exceed the unit square by more than this are counted as
# "bigger than one cell" (the warning suggests cutting first)
_UNIT_EPS = 1e-3

# Grid overlay limits
_OVERLAY_MAX_SEGMENTS = 20000   # cut-preview segments across all objects
_OVERLAY_MAX_LATTICE = 512      # faint lattice lines
_OVERLAY_MAX_FACES = 200000     # refuse to preview above this face count
_OVERLAY_LINE_BUDGET = 60000    # per-rebuild face×line iteration budget
_OVERLAY_THROTTLE = 0.15        # seconds between fingerprint re-checks


def _tag_redraw_view3d(_self=None, _context=None):
    """Ask every 3D viewport to redraw (used as a property update callback)."""
    wm = getattr(bpy.context, "window_manager", None)
    if wm is None:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


# ============================================================
# Settings
# ============================================================

class AGR_UVGridSettings(PropertyGroup):
    """Stored reference grid + options for the AGR UV grid tools"""

    # --- stored grid (captured from two edges, world space) ---
    has_grid: BoolProperty(
        name="Сетка задана",
        description="Опорная сетка запомнена по двум рёбрам",
        default=False,
    )
    origin: FloatVectorProperty(name="Начало сетки", size=3, subtype='TRANSLATION')
    u_dir: FloatVectorProperty(name="Ось U", size=3, subtype='XYZ')
    v_dir: FloatVectorProperty(name="Ось V", size=3, subtype='XYZ')
    cell_u: FloatProperty(name="Ячейка U", subtype='DISTANCE', default=0.0)
    cell_v: FloatProperty(name="Ячейка V", subtype='DISTANCE', default=0.0)

    # --- grid source ---
    grid_source: EnumProperty(
        name="Сетка",
        description="Откуда берутся оси и размер ячейки сетки",
        items=[
            ('EDGES', "По рёбрам", "Сетка, запомненная по двум рёбрам одной ячейки"),
            ('WORLD', "Мировая", "Оси из нормали фейсов и мировых осей, размер ячейки задаётся вручную"),
        ],
        default='EDGES',
        update=_tag_redraw_view3d,
    )

    # --- WORLD grid parameters ---
    world_cell_u: FloatProperty(
        name="Ячейка U",
        description="Размер ячейки мировой сетки по горизонтали",
        subtype='DISTANCE',
        default=1.0, min=0.001, soft_max=100.0,
        update=_tag_redraw_view3d,
    )
    world_cell_v: FloatProperty(
        name="Ячейка V",
        description="Размер ячейки мировой сетки по вертикали",
        subtype='DISTANCE',
        default=1.0, min=0.001, soft_max=100.0,
        update=_tag_redraw_view3d,
    )
    world_angle: FloatProperty(
        name="Поворот",
        description="Поворот сетки вокруг нормали фейсов (направление нарезки)",
        subtype='ANGLE',
        default=0.0, soft_min=-3.14159, soft_max=3.14159,
        update=_tag_redraw_view3d,
    )
    origin_mode: EnumProperty(
        name="Начало",
        description="Откуда начинается мировая сетка",
        items=[
            ('SELECTION', "Угол выделения", "Сетка начинается от угла обрабатываемых фейсов"),
            ('WORLD', "Начало мира", "Сетка привязана к мировым координатам (0,0,0)"),
        ],
        default='SELECTION',
        update=_tag_redraw_view3d,
    )

    # --- common options ---
    projection: EnumProperty(
        name="Проекция",
        description="Как сетка ложится на поверхность",
        items=[
            ('PLANAR', "Плоская",
             "Одна плоскость проекции на всё выделение"),
            ('SURFACE', "По поверхности",
             "U накапливается по дуге вдоль поверхности (ячейки не сжимаются "
             "на скруглениях), V — по высоте; линии реза следуют за фейсами"),
        ],
        default='PLANAR',
        update=_tag_redraw_view3d,
    )
    selection_mode: EnumProperty(
        name="Область",
        description="Какие фейсы обрабатывать",
        items=[
            ('SELECTED', "Выделение", "Только выделенные фейсы"),
            ('ALL', "Весь меш", "Все фейсы редактируемых мешей"),
        ],
        default='SELECTED',
        update=_tag_redraw_view3d,
    )
    auto_orient: BoolProperty(
        name="Авто-ориентация",
        description="Развернуть оси так, чтобы текстура не была зеркальной "
                    "(по нормалям фейсов), а V смотрел вверх",
        default=True,
        update=_tag_redraw_view3d,
    )
    swap_axes: BoolProperty(
        name="U↔V",
        description="Поменять оси U и V местами",
        default=False,
        update=_tag_redraw_view3d,
    )
    flip_u: BoolProperty(
        name="Флип U",
        description="Отразить направление U",
        default=False,
        update=_tag_redraw_view3d,
    )
    flip_v: BoolProperty(
        name="Флип V",
        description="Отразить направление V",
        default=False,
        update=_tag_redraw_view3d,
    )
    offset_u: FloatProperty(
        name="Сдвиг U",
        description="Сдвиг всей сетки вдоль оси U (в метрах)",
        subtype='DISTANCE',
        default=0.0, soft_min=-10.0, soft_max=10.0,
        update=_tag_redraw_view3d,
    )
    offset_v: FloatProperty(
        name="Сдвиг V",
        description="Сдвиг всей сетки вдоль оси V (в метрах)",
        subtype='DISTANCE',
        default=0.0, soft_min=-10.0, soft_max=10.0,
        update=_tag_redraw_view3d,
    )
    snap_tolerance: FloatProperty(
        name="Прилипание",
        description="Доля ячейки: вершина ближе этого к линии сетки прижимается к ней точно",
        default=0.005, min=0.0, max=0.45, precision=3,
    )


# ============================================================
# Helpers
# ============================================================

def _get_settings(context):
    return getattr(context.scene, "agr_uv_settings", None)


def _edit_mesh_objects(context):
    """Mesh objects currently in Edit Mode (multi-object edit aware)."""
    objs = list(getattr(context, "objects_in_mode_unique_data", None) or [])
    if not objs and context.edit_object is not None:
        objs = [context.edit_object]
    return [ob for ob in objs if ob.type == 'MESH' and ob.mode == 'EDIT']


def _selected_edges(context):
    """Selected edges across edit-mode objects as (world_a, world_b) pairs.

    Endpoint coordinates are copied out IMMEDIATELY: BMElem references must
    never survive this function — an undo step rebuilds the edit-mesh BMesh
    and a later dereference dies with «BMesh data has been removed»."""
    picked = []
    for obj in _edit_mesh_objects(context):
        bm = bmesh.from_edit_mesh(obj.data)
        mat = obj.matrix_world
        for e in bm.edges:
            if e.select:
                picked.append((mat @ e.verts[0].co, mat @ e.verts[1].co))
                if len(picked) > 2:
                    return picked  # enough to know it's "more than two"
    return picked


def _collect_targets(context, settings):
    """[(obj, bm, faces)] to process, honoring the selection mode."""
    targets = []
    for obj in _edit_mesh_objects(context):
        bm = bmesh.from_edit_mesh(obj.data)
        if settings.selection_mode == 'SELECTED':
            faces = [f for f in bm.faces if f.select]
        else:
            faces = [f for f in bm.faces if not f.hide]
        if faces:
            targets.append((obj, bm, faces))
    return targets


def _orientation_targets(targets):
    """Prefer the LIVE face selection as the orientation gesture: the grid
    keeps rotating under the selection even in «Весь меш» mode, where the
    huge roof/floor faces would otherwise drag the mean normal vertical and
    lock the grid to world XY."""
    picked = []
    for obj, bm, _faces in targets:
        sel = [f for f in bm.faces if f.select]
        if sel:
            picked.append((obj, bm, sel))
    return picked or targets


def _mean_world_normal(targets):
    """Area-weighted mean world-space normal of the target faces.

    Newell's formula over the WORLD-space verts: the fan cross-product sum
    is the face area vector, so weighting and normal transform come out
    correct under any object transform (incl. non-uniform/negative scale).
    """
    n = Vector((0.0, 0.0, 0.0))
    for obj, _bm, faces in targets:
        mat = obj.matrix_world
        for f in faces:
            ws = [mat @ v.co for v in f.verts]
            for i in range(1, len(ws) - 1):
                n += (ws[i] - ws[0]).cross(ws[i + 1] - ws[0])
    return n


def _capture_grid(op, context, settings, picked=None):
    """Store the reference grid from two selected edges of one cell."""
    if picked is None:
        picked = _selected_edges(context)
    if len(picked) != 2:
        count = "больше двух" if len(picked) > 2 else str(len(picked))
        agr_report(op, 'ERROR',
                   f"Нужно ровно 2 ребра одной ячейки сетки (выделено: {count})")
        return False

    (a1, b1), (a2, b2) = picked
    d1, d2 = b1 - a1, b2 - a2
    len1, len2 = d1.length, d2.length
    if len1 < 1e-9 or len2 < 1e-9:
        agr_report(op, 'ERROR', "Одно из рёбер имеет нулевую длину")
        return False
    n1, n2 = d1 / len1, d2 / len2

    normal = n1.cross(n2)
    if normal.length < 1e-2:
        agr_report(op, 'ERROR', "Рёбра почти параллельны — выделите два соседних ребра одной ячейки")
        return False
    normal.normalize()

    # U = the more horizontal edge; tie-break by the larger |X| component
    if abs(abs(n1.z) - abs(n2.z)) > 0.1:
        first_is_u = abs(n1.z) < abs(n2.z)
    else:
        first_is_u = abs(n1.x) >= abs(n2.x)
    if first_is_u:
        (ua, ub, ud, ul), (va, vb, vd, vl) = (a1, b1, n1, len1), (a2, b2, n2, len2)
    else:
        (ua, ub, ud, ul), (va, vb, vd, vl) = (a2, b2, n2, len2), (a1, b1, n1, len1)

    # Grid corner = intersection of the two edge LINES (handles a shared
    # vertex and edges that merely point at a common corner)
    hit = geometry.intersect_line_line(ua, ub, va, vb)
    if hit is None:  # parallel — already excluded above, just in case
        origin = ua.copy()
    else:
        origin = (hit[0] + hit[1]) * 0.5

    # Axes point from the origin along their edges (deterministic cell 0..1)
    if (ua - origin).length_squared > (ub - origin).length_squared:
        ud = -ud
    if (va - origin).length_squared > (vb - origin).length_squared:
        vd = -vd

    # Orthonormalize: U as picked, V = in-plane perpendicular toward the V edge
    x_dir = ud.normalized()
    y_dir = normal.cross(x_dir).normalized()
    if y_dir.dot(vd) < 0:
        y_dir = -y_dir

    # cell_v is a PROJECTION onto the orthonormal V axis: if the second
    # edge is a triangulation diagonal of the cell, its projection onto V
    # is exactly the cell height.  cell_u is the raw U edge length — that
    # edge DEFINES the U axis, so it must be a real cell edge (a diagonal
    # picked as U corrupts both cell sizes; the non-perpendicular warning
    # below is the guard for that input)
    cell_u = ul
    cell_v = vl * abs(vd.dot(y_dir))
    if cell_v < 1e-9:
        agr_report(op, 'ERROR', "Второе ребро лежит вдоль первого — ячейка вырождена")
        return False

    settings.origin = origin
    settings.u_dir = x_dir
    settings.v_dir = y_dir
    settings.cell_u = cell_u
    settings.cell_v = cell_v
    settings.has_grid = True
    _tag_redraw_view3d()  # the grid overlay must repaint with the new grid

    if abs(n1.dot(n2)) > 0.3:  # edges far from perpendicular (>~17°)
        agr_report(op, 'WARNING',
                   f"Сетка запомнена: ячейка {cell_u:.3g} × {cell_v:.3g} м, но рёбра "
                   "не перпендикулярны — диагональ допустима только как ВТОРОЕ "
                   "(вертикальное) ребро; горизонтальное ребро оси U должно быть "
                   "настоящим ребром ячейки")
    else:
        agr_report(op, 'INFO', f"Сетка запомнена: ячейка {cell_u:.3g} × {cell_v:.3g} м")
    return True


def _ensure_grid(op, context, settings):
    """EDGES source: make sure a grid exists, auto-capturing from a
    2-edge selection for the original one-shot script workflow.

    Returns 'HAD' (grid already available), 'CAPTURED' (just captured from
    the selected edges) or None (no grid — error already reported).
    """
    if settings.grid_source != 'EDGES' or settings.has_grid:
        return 'HAD'
    picked = _selected_edges(context)
    if len(picked) == 2:
        return 'CAPTURED' if _capture_grid(op, context, settings, picked) else None
    agr_report(op, 'ERROR',
               "Сетка не задана — выделите 2 ребра ячейки и нажмите «Запомнить сетку», "
               "или переключитесь на мировую сетку")
    return None


def _capture_only_finish(op, context, settings, grid_state):
    """After an on-the-fly capture in SELECTED mode there are no selected
    faces by construction (2 selected edges can never form a selected
    face) — finish with the capture instead of a confusing ERROR."""
    if grid_state != 'CAPTURED' or settings.selection_mode != 'SELECTED':
        return False
    for obj in _edit_mesh_objects(context):
        bm = bmesh.from_edit_mesh(obj.data)
        if any(f.select for f in bm.faces):
            return False
    agr_report(op, 'INFO',
               "Сетка запомнена — теперь выделите фейсы и запустите оператор снова")
    return True


def _resolve_basis(op, settings, targets, quiet=False):
    """Final (origin, x_dir, y_dir, cell_u, cell_v) in world space, or None.

    Applies grid source, axis swap, auto-orientation (no mirroring, V up)
    and the manual flips; for the WORLD source optionally snaps the origin
    to the corner of the processed faces.  quiet=True (overlay preview)
    suppresses the error reports.
    """
    n_hint = _mean_world_normal(_orientation_targets(targets))

    if settings.grid_source == 'EDGES':
        origin = Vector(settings.origin)
        x_dir = Vector(settings.u_dir)
        y_dir = Vector(settings.v_dir)
        cell_u, cell_v = settings.cell_u, settings.cell_v
        if (not settings.has_grid or x_dir.length < 1e-9 or y_dir.length < 1e-9
                or cell_u < 1e-9 or cell_v < 1e-9):
            if not quiet:
                agr_report(op, 'ERROR', "Сетка повреждена — запомните её заново")
            return None
        x_dir.normalize()
        y_dir.normalize()
    else:  # WORLD
        if n_hint.length < 1e-6:
            if not quiet:
                agr_report(op, 'ERROR',
                           "Не удалось определить нормаль фейсов (фейсы смотрят в разные "
                           "стороны) — обрабатывайте стены по отдельности")
            return None
        n = n_hint.normalized()
        if abs(n.z) > 0.7:  # floor / ceiling
            x_dir = Vector((1.0, 0.0, 0.0))
            y_dir = Vector((0.0, 1.0, 0.0))
        else:  # wall: U horizontal along the wall, V up the wall
            x_dir = Vector((0.0, 0.0, 1.0)).cross(n).normalized()
            y_dir = n.cross(x_dir).normalized()
        if settings.world_angle != 0.0:
            rot = Matrix.Rotation(settings.world_angle, 3, n)
            x_dir = (rot @ x_dir).normalized()
            y_dir = (rot @ y_dir).normalized()
        origin = Vector((0.0, 0.0, 0.0))
        cell_u, cell_v = settings.world_cell_u, settings.world_cell_v

    if settings.swap_axes:
        x_dir, y_dir = y_dir, x_dir
        cell_u, cell_v = cell_v, cell_u

    if settings.auto_orient and n_hint.length > 1e-6:
        n = n_hint.normalized()
        up = Vector((0.0, 0.0, 1.0)) if abs(n.z) < 0.7 else Vector((0.0, 1.0, 0.0))
        # V points "up" (skip when V is perpendicular to the up reference)
        if abs(y_dir.dot(up)) > 1e-3 and y_dir.dot(up) < 0:
            y_dir = -y_dir
        # right-handed w.r.t. the face normal -> texture is not mirrored
        if x_dir.cross(y_dir).dot(n) < 0:
            x_dir = -x_dir

    if settings.flip_u:
        x_dir = -x_dir
    if settings.flip_v:
        y_dir = -y_dir

    # WORLD grid starting at the selection corner: shift the origin AFTER
    # the final axis orientation is known (the "corner" depends on it)
    if settings.grid_source == 'WORLD' and settings.origin_mode == 'SELECTION':
        min_u = min_v = None
        for obj, _bm, faces in targets:
            mat = obj.matrix_world
            for f in faces:
                for v in f.verts:
                    d = (mat @ v.co) - origin
                    gu, gv = d.dot(x_dir), d.dot(y_dir)
                    min_u = gu if min_u is None else min(min_u, gu)
                    min_v = gv if min_v is None else min(min_v, gv)
        if min_u is not None:
            origin = origin + x_dir * min_u + y_dir * min_v

    # user grid offset: move the whole grid along its axes (SURFACE arc-U
    # gets the U part separately — through the phase anchor, see
    # _surface_u_frames — because the arc coordinate ignores the origin)
    origin = origin + x_dir * settings.offset_u + y_dir * settings.offset_v

    return origin, x_dir, y_dir, cell_u, cell_v


def _snap(value, tol):
    """Snap a grid coordinate to the nearest grid line within tolerance."""
    r = round(value)
    return float(r) if abs(value - r) <= tol else value


# Grid lines closer than this (in cell units) to a span border are skipped.
# The ONE constant shared by the cut planner and the overlay preview — the
# preview must promise exactly the cuts the operator will make.
_LINE_EPS = 1e-4


def _interior_lines(lo, hi, eps=_LINE_EPS):
    """Integer grid lines strictly inside (lo, hi), border lines excluded."""
    return [k for k in range(floor(lo) + 1, ceil(hi)) if lo + eps < k < hi - eps]


def _surface_u_frames(targets, basis, anchor_point=None, u_sign=1.0, u_offset=0.0):
    """Per-face arc-length U frames for the SURFACE projection.

    Returns {face: (h_dir, anchor_co, u0)} with u(p) = u0 + (p - anchor)·h_dir
    in METERS, world space.  h_dir is the face's own horizontal tangent
    (y_dir × normal), and u0 offsets are stitched across shared edges via
    BFS so U accumulates the true arc length along a curved facade instead
    of collapsing onto one projection plane.

    Phase anchoring makes the coordinate REPRODUCIBLE across topology
    changes — the cut and the following unwrap MUST agree on where the grid
    lines are: every connected component is shifted so its smallest arc-U
    sits exactly on a grid line (0); with anchor_point (EDGES source: the
    captured grid origin) the component closest to that point puts U=0
    there instead, so the grid phase stays at the captured cell corner.

    u_sign must be (flip_u sign) × (flip_v sign): h is derived from the
    possibly-flipped y_dir, so the flip_v negation has to be cancelled out
    of U while flip_u must actually flip it.  Operator code must not call
    this directly — go through _make_frames, which derives the arguments.
    """
    from collections import deque

    origin, x_dir, y_dir, _cell_u, _cell_v = basis
    frames = {}
    components = []  # (comp_faces, comp_min_u, dist²_to_anchor, u_at_nearest)
    for obj, _bm, faces in targets:
        mat = obj.matrix_world
        face_set = set(faces)
        h_raw = {}
        face_ws = {}
        for f in faces:
            ws = [mat @ v.co for v in f.verts]
            face_ws[f] = ws
            n = Vector((0.0, 0.0, 0.0))
            for i in range(1, len(ws) - 1):
                n += (ws[i] - ws[0]).cross(ws[i + 1] - ws[0])
            # U = V×n per face: right-handed w.r.t. the face's OWN normal —
            # never mirrored by construction (do NOT inherit the sign from
            # a neighbour: at corners ≥90° that dot is a coin flip and it
            # mirrored whole downstream walls)
            h = y_dir.cross(n)
            if h.length < 1e-6 * max(n.length, 1e-12):
                h = x_dir.copy()  # face ⊥ V axis (floor-ish) — fall back to planar U
            else:
                h.normalize()
                h *= u_sign
            h_raw[f] = h

        def planar_u(p):
            return (p - origin).dot(x_dir)

        # deterministic seeds: components start at their planar-leftmost face
        ordered = sorted(faces, key=lambda f: min(planar_u(p) for p in face_ws[f]))
        visited = set()
        for seed in ordered:
            if seed in visited:
                continue
            sh = h_raw[seed]
            anchor = face_ws[seed][0]
            frames[seed] = (sh, anchor, planar_u(anchor))
            visited.add(seed)
            comp = [seed]
            queue = deque([seed])
            while queue:
                f = queue.popleft()
                fh, fanchor, fu0 = frames[f]
                for e in f.edges:
                    for g in e.link_faces:
                        if g not in face_set or g in visited:
                            continue
                        gh = h_raw[g]
                        shared = [mat @ v.co for v in e.verts]
                        # stitch: both faces give the shared verts the same mean U
                        mean_f = sum(fu0 + (p - fanchor).dot(fh) for p in shared) / len(shared)
                        ganchor = face_ws[g][0]
                        mean_rel = sum((p - ganchor).dot(gh) for p in shared) / len(shared)
                        frames[g] = (gh, ganchor, mean_f - mean_rel)
                        visited.add(g)
                        comp.append(g)
                        queue.append(g)

            comp_min = best_d = best_u = None
            for f in comp:
                fh, fanchor, fu0 = frames[f]
                for p in face_ws[f]:
                    u = fu0 + (p - fanchor).dot(fh)
                    comp_min = u if comp_min is None else min(comp_min, u)
                    if anchor_point is not None:
                        d = (p - anchor_point).length_squared
                        if best_d is None or d < best_d:
                            best_d, best_u = d, u
            components.append((comp, comp_min, best_d, best_u))

    # the component nearest to the captured origin anchors its phase there;
    # every other component starts its grid at its own edge
    anchored = None
    if anchor_point is not None and components:
        anchored = min(range(len(components)),
                       key=lambda i: (components[i][2]
                                      if components[i][2] is not None else 1e30))
    for idx, (comp, comp_min, _d, u_at) in enumerate(components):
        # + user grid offset THROUGH the anchor (the anchor re-zeroes the
        # phase, so an origin shift alone would be cancelled out)
        shift = (u_at if (idx == anchored and u_at is not None) else comp_min)
        shift = (shift or 0.0) + u_offset
        if shift:
            for f in comp:
                h, a, u0 = frames[f]
                frames[f] = (h, a, u0 - shift)
    return frames


def _make_frames(settings, targets, basis):
    """SURFACE frames with the anchor/sign/offset derived in ONE place.

    Cut, unwrap and the overlay preview MUST agree on the grid phase (see
    _surface_u_frames); routing every caller through here is what makes
    the agreement structural instead of a copy-paste convention.  Returns
    None for the PLANAR projection.
    """
    if settings.projection != 'SURFACE':
        return None
    anchor = Vector(settings.origin) if settings.grid_source == 'EDGES' else None
    # flip_v negates y_dir, which would drag the per-face U (= y_dir × n)
    # along with it — cancel that; flip_u genuinely flips U
    u_sign = ((-1.0 if settings.flip_u else 1.0)
              * (-1.0 if settings.flip_v else 1.0))
    return _surface_u_frames(targets, basis, anchor, u_sign, settings.offset_u)


def _report_no_targets(op, settings):
    if settings.selection_mode == 'SELECTED':
        agr_report(op, 'ERROR',
                   "Нет выделенных фейсов — выделите фейсы или включите режим «Весь меш»")
    else:
        agr_report(op, 'ERROR', "Нет фейсов для обработки")


def _note_uv_overwrite(obj, bm, faces, atlas_objects, udim_objects):
    """Track objects whose special UV state gets overwritten by a rewrite."""
    if obj.get('agr_atlas_applied'):
        if len(faces) == len(bm.faces):
            # every atlas UV gets rewritten -> the re-apply guard would
            # only block a now-perfectly-valid atlas application
            del obj['agr_atlas_applied']
        atlas_objects.append(obj.name)
    if object_has_udim(obj):
        # integer UV part IS the tile number for the UDIM tools
        udim_objects.append(obj.name)


# ============================================================
# Core: unwrap
# ============================================================

def _do_unwrap(op, context, settings):
    targets = _collect_targets(context, settings)
    if not targets:
        _report_no_targets(op, settings)
        return False
    basis = _resolve_basis(op, settings, targets)
    if basis is None:
        return False
    origin, x_dir, y_dir, cell_u, cell_v = basis
    tol = settings.snap_tolerance
    surface = settings.projection == 'SURFACE'
    frames = _make_frames(settings, targets, basis)

    total = 0
    oversize = 0
    atlas_objects = []
    udim_objects = []
    for obj, bm, faces in targets:
        mat = obj.matrix_world
        _note_uv_overwrite(obj, bm, faces, atlas_objects, udim_objects)
        uv_layer = bm.loops.layers.uv.verify()

        # STRICT position-faithful mapping (user's final choice — the
        # smart placement tiers were each tried and rejected): UV = grid
        # coordinate minus the cell index of the face center, per-vert
        # snap on top.  Pieces of one cell assemble the tile at their true
        # places; anything misaligned pokes out of 0..1 honestly and the
        # oversize counter suggests cutting.
        for f in faces:
            pts = [mat @ v.co for v in f.verts]
            if surface:
                fh, fanchor, fu0 = frames[f]
                gus = [(fu0 + (p - fanchor).dot(fh)) / cell_u for p in pts]
            else:
                gus = [(p - origin).dot(x_dir) / cell_u for p in pts]
            gvs = [(p - origin).dot(y_dir) / cell_v for p in pts]

            cell_x = floor(sum(gus) / len(gus))
            cell_y = floor(sum(gvs) / len(gvs))
            face_out = False
            for i, loop in enumerate(f.loops):
                u = _snap(gus[i], tol) - cell_x
                v = _snap(gvs[i], tol) - cell_y
                loop[uv_layer].uv = (u, v)
                if (u < -_UNIT_EPS or u > 1.0 + _UNIT_EPS
                        or v < -_UNIT_EPS or v > 1.0 + _UNIT_EPS):
                    face_out = True
            if face_out:
                oversize += 1
        total += len(faces)
        bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)

    msg = f"Развёрнуто фейсов: {total} (ячейка {cell_u:.3g} × {cell_v:.3g} м)"
    if udim_objects:
        msg += f"; ВНИМАНИЕ: раскладка UDIM-тайлов потеряна у: {', '.join(udim_objects)}"
    if atlas_objects:
        msg += f"; перезаписаны UV атласа: {', '.join(atlas_objects)}"
    if oversize:
        msg += (f"; {oversize} фейс(ов) больше одной ячейки — "
                "примените «Разрезать по сетке»")
    if udim_objects or atlas_objects or oversize:
        agr_report(op, 'WARNING', msg)
    else:
        agr_report(op, 'INFO', msg)
    return True


# ============================================================
# Core: cut along grid lines
# ============================================================

def _do_cut(op, context, settings):
    targets = _collect_targets(context, settings)
    if not targets:
        _report_no_targets(op, settings)
        return False
    basis = _resolve_basis(op, settings, targets)
    if basis is None:
        return False
    origin, x_dir, y_dir, cell_u, cell_v = basis
    surface = settings.projection == 'SURFACE'
    frames = _make_frames(settings, targets, basis)

    # ---- phase 0: plan EVERY object before ANY bisect.  The span/limit
    # guards must fire while all meshes are still untouched: a cancelled
    # operator pushes no undo step, so cutting object A and then failing
    # on object B would fuse A's cuts into the previous undo entry ----
    plans = []
    skipped = []
    for obj, bm, faces in targets:
        mat = obj.matrix_world
        verts = {v for f in faces for v in f.verts}
        us, vs = [], []
        for v in verts:
            d = (mat @ v.co) - origin
            if not surface:
                us.append(d.dot(x_dir) / cell_u)
            vs.append(d.dot(y_dir) / cell_v)
        min_v, max_v = min(vs), max(vs)
        span = max_v - min_v
        min_u = max_u = None
        if not surface:
            min_u, max_u = min(us), max(us)
            span += max_u - min_u
        if span > _MAX_CUT_LINES:
            skipped.append(f"{obj.name} (~{int(span)} линий)")
            continue

        u_plan = []
        if surface:
            # per-face U lines: every face carries its own cut planes
            u_total = 0
            for f in faces:
                fh, fanchor, fu0 = frames[f]
                us_f = [(fu0 + ((mat @ v.co) - fanchor).dot(fh)) / cell_u
                        for v in f.verts]
                ks = _interior_lines(min(us_f), max(us_f))
                u_total += len(ks)
                if u_total > _MAX_CUT_LINES:
                    break
                if ks:
                    u_plan.append((f, fh, fanchor, fu0, ks))
            if u_total > _MAX_CUT_LINES:
                skipped.append(f"{obj.name} (>{_MAX_CUT_LINES} линий)")
                continue

        plans.append((obj, bm, faces, u_plan, min_u, max_u, min_v, max_v))

    if skipped:
        # nothing has been cut yet — abort the WHOLE operator (the caller
        # must not run a follow-up unwrap that would bury this ERROR)
        agr_report(op, 'ERROR',
                   "Слишком много линий разреза: " + ", ".join(skipped) +
                   f" (лимит {_MAX_CUT_LINES}) — проверьте размер ячейки")
        return False

    total_cuts = 0
    total_new = 0
    for obj, bm, faces, u_plan, min_u, max_u, min_v, max_v in plans:
        mat = obj.matrix_world
        mat_inv = mat.inverted_safe()
        # world normal -> local plane normal for bisect_plane (transpose,
        # NOT inverse: correct under non-uniform scale)
        nrm_to_local = mat.to_3x3().transposed()
        # dist is in LOCAL units — divide by the object scale so the weld
        # tolerance stays ~1e-5 m in world space (FBX imports scale 100+)
        obj_scale = max(abs(c) for c in mat.to_scale())
        weld_dist = 1e-5 / max(obj_scale, 1e-9)
        before = len(faces)

        # ---- phase 1 (SURFACE only): per-face U cuts ----
        work_faces = list(faces)
        if surface:
            result_faces = set(faces)
            for f, fh, fanchor, fu0, ks in u_plan:
                result_faces.discard(f)
                geom = list(f.verts) + list(f.edges) + [f]
                plane_no = (nrm_to_local @ fh).normalized()
                for k in ks:
                    res = bmesh.ops.bisect_plane(
                        bm, geom=geom,
                        plane_co=mat_inv @ (fanchor + fh * (k * cell_u - fu0)),
                        plane_no=plane_no,
                        dist=weld_dist,
                    )
                    geom = res['geom']
                    total_cuts += 1
                result_faces.update(g for g in geom
                                    if isinstance(g, bmesh.types.BMFace))
            work_faces = [f for f in result_faces if f.is_valid]

        # ---- phase 2: global planes — V lines (+ U lines in PLANAR) ----
        axes = [(y_dir, cell_v, _interior_lines(min_v, max_v))]
        if not surface:
            axes.insert(0, (x_dir, cell_u, _interior_lines(min_u, max_u)))

        verts2 = {v for f in work_faces for v in f.verts}
        edges2 = {e for f in work_faces for e in f.edges}
        geom = list(verts2) + list(edges2) + list(work_faces)
        for dir_w, cell, lines in axes:
            if not lines:
                continue
            plane_no = (nrm_to_local @ dir_w).normalized()
            for k in lines:
                res = bmesh.ops.bisect_plane(
                    bm, geom=geom,
                    plane_co=mat_inv @ (origin + dir_w * (k * cell)),
                    plane_no=plane_no,
                    dist=weld_dist,
                )
                # res['geom'] = survivors + everything newly created (incl.
                # new faces) — chaining it keeps every next cut complete
                geom = res['geom']
                total_cuts += 1

        new_faces = [g for g in geom
                     if isinstance(g, bmesh.types.BMFace) and g.is_valid]
        if settings.selection_mode == 'SELECTED':
            # bisect_plane creates faces with select=False — reselect them
            # (the assignment flushes down to verts/edges on its own; do
            # NOT select_flush upward — it would grab enclosed faces the
            # user deliberately deselected, e.g. a window in a wall)
            for f in new_faces:
                f.select = True
        total_new += len(new_faces) - before
        bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=True)

    agr_report(op, 'INFO', f"Разрезов: {total_cuts}, новых фейсов: +{total_new}")
    return True


# ============================================================
# Grid overlay: live preview of the grid and the cut lines
# ============================================================

_uv_handle_3d = None
_uv_draw_error_logged = False
_uv_geo_version = 0
_uv_overlay_cache = {"fp": None, "data": None, "next_check": 0.0}
_uv_last_stats = None  # feeds the panel while the overlay is on

_COLOR_CUT = (1.0, 0.6, 0.1, 0.9)        # orange: actual bisect segments
_COLOR_LATTICE = (0.5, 0.7, 1.0, 0.22)   # faint blue: full lattice
_COLOR_CELL = (1.0, 1.0, 1.0, 0.75)      # white: one-cell outline
_COLOR_AXIS_U = (0.95, 0.25, 0.2, 1.0)   # red: U arrow
_COLOR_AXIS_V = (0.3, 0.85, 0.3, 1.0)    # green: V arrow


def _uv_overlay_fingerprint(context, settings):
    """Cheap state hash: recompute the preview only when this changes."""
    s = settings
    fp = [
        s.grid_source, s.projection, s.selection_mode, s.auto_orient, s.swap_axes,
        s.flip_u, s.flip_v, round(s.world_cell_u, 6), round(s.world_cell_v, 6),
        round(s.world_angle, 6), s.origin_mode, s.has_grid,
        round(s.offset_u, 6), round(s.offset_v, 6),
        tuple(round(c, 6) for c in s.origin),
        tuple(round(c, 6) for c in s.u_dir),
        tuple(round(c, 6) for c in s.v_dir),
        round(s.cell_u, 6), round(s.cell_v, 6),
        _uv_geo_version,
    ]
    for obj in _edit_mesh_objects(context):
        bm = bmesh.from_edit_mesh(obj.data)
        # selection is ALWAYS part of the state: the grid orientation follows
        # the live selection even in «Весь меш» mode
        sel_count = sel_hash = 0
        for f in bm.faces:
            if f.select:
                sel_count += 1
                sel_hash = (sel_hash * 31 + f.index) & 0x7FFFFFFF
        fp.append((obj.name, len(bm.verts), len(bm.faces), sel_count, sel_hash,
                   tuple(round(v, 5) for row in obj.matrix_world for v in row)))
    return tuple(fp)


def _uv_overlay_build(context, settings):
    """Preview geometry: cut segments on the faces + lattice + U/V axes."""
    targets = _collect_targets(context, settings)
    if not targets:
        return None
    total_faces = sum(len(faces) for _o, _b, faces in targets)
    if total_faces > _OVERLAY_MAX_FACES:
        return {"error": f"слишком много фейсов для превью ({total_faces})"}
    basis = _resolve_basis(None, settings, targets, quiet=True)
    if basis is None:
        return {"error": "сетка не определена (см. настройки)"}
    origin, x_dir, y_dir, cell_u, cell_v = basis
    surface = settings.projection == 'SURFACE'
    frames = _make_frames(settings, targets, basis)

    cut_pts = []
    truncated = False
    budget = _OVERLAY_LINE_BUDGET
    gmin_u = gmax_u = gmin_v = gmax_v = None
    centroid = Vector((0.0, 0.0, 0.0))
    centroid_n = 0
    normal_sum = Vector((0.0, 0.0, 0.0))
    lift_len = 0.004 * min(cell_u, cell_v)
    axis_seed = None  # (min_u, corner_point, h_dir, face_normal) for SURFACE axes

    for obj, _bm, faces in targets:
        mat = obj.matrix_world
        for f in faces:
            ws = [mat @ v.co for v in f.verts]
            if surface:
                fh, fanchor, fu0 = frames[f]
                gus = [(fu0 + (p - fanchor).dot(fh)) / cell_u for p in ws]
            else:
                gus = [(p - origin).dot(x_dir) / cell_u for p in ws]
            gvs = [(p - origin).dot(y_dir) / cell_v for p in ws]

            fmin_u, fmax_u = min(gus), max(gus)
            fmin_v, fmax_v = min(gvs), max(gvs)
            gmin_u = fmin_u if gmin_u is None else min(gmin_u, fmin_u)
            gmax_u = fmax_u if gmax_u is None else max(gmax_u, fmax_u)
            gmin_v = fmin_v if gmin_v is None else min(gmin_v, fmin_v)
            gmax_v = fmax_v if gmax_v is None else max(gmax_v, fmax_v)

            fn = Vector((0.0, 0.0, 0.0))
            for i in range(1, len(ws) - 1):
                fn += (ws[i] - ws[0]).cross(ws[i + 1] - ws[0])
            normal_sum += fn
            for p in ws:
                centroid += p
            centroid_n += len(ws)

            if surface:
                i_min = min(range(len(gus)), key=lambda i: gus[i])
                if axis_seed is None or gus[i_min] < axis_seed[0]:
                    axis_seed = (gus[i_min], ws[i_min].copy(), fh.copy(),
                                 fn.normalized() if fn.length > 1e-12 else Vector((0, 0, 1)))

            if truncated:
                continue
            lift = (fn.normalized() if fn.length > 1e-12 else Vector((0, 0, 1))) * lift_len

            # intersect the face polygon with every grid line crossing it
            # (_interior_lines keeps the preview in lockstep with _do_cut:
            # a line the cut skips must never be drawn)
            for vals, sort_dir in ((gus, y_dir), (gvs, x_dir)):
                for k in _interior_lines(min(vals), max(vals)):
                    budget -= 1
                    if budget <= 0 or len(cut_pts) >= _OVERLAY_MAX_SEGMENTS * 2:
                        truncated = True
                        break
                    # vertex-exactly-on-line degeneracy is nudged to one
                    # side so crossings always come in PAIRS — the pairing
                    # below would otherwise draw segments outside the face
                    # on concave polygons
                    n = len(ws)
                    sides = [(v - k) if abs(v - k) > 1e-9 else 1e-12
                             for v in vals]
                    pts = []
                    for i in range(n):
                        sa, sb = sides[i], sides[(i + 1) % n]
                        if sa * sb < 0.0:
                            t = sa / (sa - sb)
                            pts.append(ws[i].lerp(ws[(i + 1) % n], t))
                    if len(pts) < 2:
                        continue
                    pts.sort(key=lambda p: p.dot(sort_dir))
                    for j in range(0, len(pts) - 1, 2):
                        cut_pts.append(tuple(pts[j] + lift))
                        cut_pts.append(tuple(pts[j + 1] + lift))
                if truncated:
                    break

    lattice_pts, cell_pts, axis_u_pts, axis_v_pts = [], [], [], []
    if surface and axis_seed is not None:
        # SURFACE: no flat lattice (the grid follows the faces) — draw one
        # cell outline + axis arrows on the seed face instead
        _su, corner, h_dir, seed_n = axis_seed
        base = Vector(corner) + seed_n * lift_len
        cu_v, cv_v = h_dir * cell_u, y_dir * cell_v
        c00, c10 = tuple(base), tuple(base + cu_v)
        c11, c01 = tuple(base + cu_v + cv_v), tuple(base + cv_v)
        cell_pts = [c00, c10, c10, c11, c11, c01, c01, c00]
        head = 0.15 * min(cell_u, cell_v)

        def arrow_s(target, to_v, side_dir):
            frm, to = base, base + to_v
            back = (frm - to).normalized() * head
            side = side_dir * (head * 0.6)
            target += [tuple(frm), tuple(to),
                       tuple(to), tuple(to + back + side),
                       tuple(to), tuple(to + back - side)]

        arrow_s(axis_u_pts, cu_v, y_dir)
        arrow_s(axis_v_pts, cv_v, h_dir)
    elif centroid_n and normal_sum.length > 1e-9 and gmin_u is not None:
        mean_n = normal_sum.normalized()
        centroid /= centroid_n
        # draw the lattice in the dominant surface plane (the origin may sit
        # off that plane, e.g. WORLD origin (0,0,0) for a wall at y=5)
        o_draw = origin + mean_n * ((centroid - origin).dot(mean_n) + lift_len)

        def gp(u, v):
            return tuple(o_draw + x_dir * (u * cell_u) + y_dir * (v * cell_v))

        ku0, ku1 = floor(gmin_u), ceil(gmax_u)
        kv0, kv1 = floor(gmin_v), ceil(gmax_v)
        if (ku1 - ku0) + (kv1 - kv0) <= _OVERLAY_MAX_LATTICE:
            for k in range(ku0, ku1 + 1):
                lattice_pts += [gp(k, kv0), gp(k, kv1)]
            for k in range(kv0, kv1 + 1):
                lattice_pts += [gp(ku0, k), gp(ku1, k)]

        # one-cell outline + axis arrows at the selection corner cell
        cu, cv = floor(gmin_u + 1e-6), floor(gmin_v + 1e-6)
        c00, c10, c11, c01 = gp(cu, cv), gp(cu + 1, cv), gp(cu + 1, cv + 1), gp(cu, cv + 1)
        cell_pts = [c00, c10, c10, c11, c11, c01, c01, c00]

        head = 0.15 * min(cell_u, cell_v)

        def arrow(target, tip_uv, side_dir):
            frm, to = Vector(c00), Vector(gp(*tip_uv))
            back = (frm - to).normalized() * head
            side = side_dir * (head * 0.6)
            target += [tuple(frm), tuple(to),
                       tuple(to), tuple(to + back + side),
                       tuple(to), tuple(to + back - side)]

        arrow(axis_u_pts, (cu + 1, cv), y_dir)
        arrow(axis_v_pts, (cu, cv + 1), x_dir)

    return {
        "cut_pts": cut_pts,
        "lattice_pts": lattice_pts,
        "cell_pts": cell_pts,
        "axis_u_pts": axis_u_pts,
        "axis_v_pts": axis_v_pts,
        "cell_u": cell_u,
        "cell_v": cell_v,
        "truncated": truncated,
        "batches": {},  # built lazily inside the draw callback
    }


def _uv_get_overlay_data(context):
    """Throttled, fingerprint-cached preview data (None = draw nothing)."""
    global _uv_last_stats
    settings = _get_settings(context)
    if settings is None or context.mode != 'EDIT_MESH':
        _uv_last_stats = None
        return None
    cache = _uv_overlay_cache
    now = time.monotonic()
    # fp None = "no valid cache" (initial state / ReferenceError recovery)
    # and bypasses the throttle; a legitimate empty build keeps fp set, so
    # it does NOT degrade into an O(faces) fingerprint on every redraw
    if now >= cache["next_check"] or cache["fp"] is None:
        cache["next_check"] = now + _OVERLAY_THROTTLE
        try:
            fp = _uv_overlay_fingerprint(context, settings)
            if cache["fp"] != fp or cache["data"] is None:
                # drop the old geometry BEFORE the rebuild and stamp fp
                # only AFTER it: if the build throws, stale geometry must
                # not survive under the new fingerprint (it would be drawn
                # forever), and the throttled retry stays armed
                cache["data"] = None
                cache["data"] = _uv_overlay_build(context, settings)
                cache["fp"] = fp
        except ReferenceError:
            # stale edit-mesh BMesh (undo mid-frame) — retry next redraw
            cache["fp"] = None
            cache["data"] = None
    data = cache["data"]
    if data is None:
        _uv_last_stats = None
        return None
    if "error" in data:
        _uv_last_stats = {"error": data["error"]}
        return None
    _uv_last_stats = {
        "cuts": len(data["cut_pts"]) // 2,
        "truncated": data["truncated"],
        "cell_u": data["cell_u"],
        "cell_v": data["cell_v"],
    }
    return data


def _uv_overlay_enabled():
    """Handlers survive a .blend load while the WindowManager is replaced —
    stale handlers must draw nothing until the load_post sync runs."""
    wm = getattr(bpy.context, "window_manager", None)
    return wm is not None and getattr(wm, "agr_uv_grid_show", False)


def _uv_draw_overlay_3d():
    global _uv_draw_error_logged
    try:
        if not _uv_overlay_enabled():
            return
        data = _uv_get_overlay_data(bpy.context)
        if data is None:
            return

        shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
        prev_blend = gpu.state.blend_get()
        prev_depth = gpu.state.depth_test_get()
        gpu.state.blend_set('ALPHA')
        # depth-tested on purpose (user preference): occluded lines make the
        # grid read as engraved INTO the faces, not floating over them
        gpu.state.depth_test_set('LESS_EQUAL')
        try:
            viewport = gpu.state.viewport_get()
            shader.bind()
            shader.uniform_float("viewportSize", (viewport[2], viewport[3]))
            passes = (
                ("lattice_pts", _COLOR_LATTICE, 1.0),
                ("cell_pts", _COLOR_CELL, 2.0),
                ("cut_pts", _COLOR_CUT, 2.5),
                ("axis_u_pts", _COLOR_AXIS_U, 3.0),
                ("axis_v_pts", _COLOR_AXIS_V, 3.0),
            )
            for key, color, width in passes:
                coords = data[key]
                if not coords:
                    continue
                batch = data["batches"].get(key)
                if batch is None:
                    batch = batch_for_shader(shader, 'LINES', {"pos": coords})
                    data["batches"][key] = batch
                shader.uniform_float("lineWidth", width)
                shader.uniform_float("color", color)
                batch.draw(shader)
        finally:
            gpu.state.blend_set(prev_blend)
            gpu.state.depth_test_set(prev_depth)
        # a clean pass re-arms "log once": the NEXT error streak gets its
        # own traceback instead of being silenced by a long-gone transient
        _uv_draw_error_logged = False
    except Exception:
        if not _uv_draw_error_logged:
            _uv_draw_error_logged = True
            logger.error("❌ AGR UV: grid overlay draw failed:\n%s",
                         traceback.format_exc())


@bpy.app.handlers.persistent
def _uv_overlay_depsgraph(_scene, depsgraph):
    """Bump the geometry version so vertex edits refresh the preview."""
    global _uv_geo_version
    for u in depsgraph.updates:
        if u.is_updated_geometry:
            _uv_geo_version += 1
            break


def _uv_add_handlers():
    global _uv_handle_3d, _uv_draw_error_logged
    _uv_draw_error_logged = False
    _uv_overlay_cache["fp"] = None
    _uv_overlay_cache["next_check"] = 0.0
    if _uv_handle_3d is None:
        _uv_handle_3d = bpy.types.SpaceView3D.draw_handler_add(
            _uv_draw_overlay_3d, (), 'WINDOW', 'POST_VIEW')
    if _uv_overlay_depsgraph not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_uv_overlay_depsgraph)


def _uv_remove_handlers():
    global _uv_handle_3d, _uv_last_stats
    if _uv_handle_3d is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_uv_handle_3d, 'WINDOW')
        except Exception:
            pass
        _uv_handle_3d = None
    if _uv_overlay_depsgraph in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_uv_overlay_depsgraph)
    _uv_overlay_cache["fp"] = None
    _uv_overlay_cache["data"] = None
    _uv_last_stats = None


def _uv_grid_toggle(_self, context):
    if context.window_manager.agr_uv_grid_show:
        _uv_add_handlers()
    else:
        _uv_remove_handlers()
    _tag_redraw_view3d()


@bpy.app.handlers.persistent
def _uv_sync_handlers_on_load(_dummy):
    """Re-align handler state with the toggle visible after a file load."""
    if _uv_overlay_enabled():
        _uv_add_handlers()
    else:
        _uv_remove_handlers()


# ============================================================
# Operators
# ============================================================

class _AGR_UVGridPollMixin:
    @classmethod
    def poll(cls, context):
        if context.mode != 'EDIT_MESH':
            cls.poll_message_set("Работает в режиме редактирования меша")
            return False
        return True

    def execute(self, context):
        # Right after an undo the edit-mesh BMesh handed out by
        # bmesh.from_edit_mesh can still expose dead elements — turn the
        # crash into a friendly "run it again" instead of a traceback
        try:
            return self._execute(context)
        except ReferenceError:
            agr_report(self, 'ERROR',
                       "Данные меша устарели (например, после Undo) — "
                       "запустите оператор ещё раз")
            return {'CANCELLED'}


class AGR_OT_UVGridCapture(_AGR_UVGridPollMixin, Operator):
    """Store the reference grid from two selected edges of one grid cell"""
    bl_idname = "agr.uv_grid_capture"
    bl_label = "Запомнить сетку"
    bl_description = ("Запомнить опорную сетку по двум выделенным рёбрам одной "
                      "ячейки: оси U/V, размер ячейки и начало — в мировых "
                      "координатах, работает для всех объектов сцены")
    bl_options = {'REGISTER', 'UNDO'}

    def _execute(self, context):
        settings = _get_settings(context)
        if settings is None or not _capture_grid(self, context, settings):
            return {'CANCELLED'}
        return {'FINISHED'}


class AGR_OT_UVGridClear(Operator):
    """Forget the stored reference grid"""
    bl_idname = "agr.uv_grid_clear"
    bl_label = "Сбросить сетку"
    bl_description = "Забыть запомненную опорную сетку"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        settings = _get_settings(context)
        if settings is None or not settings.has_grid:
            cls.poll_message_set("Сетка не запомнена")
            return False
        return True

    def execute(self, context):
        _get_settings(context).has_grid = False
        _tag_redraw_view3d()  # the overlay must swap to its "no grid" state
        agr_report(self, 'INFO', "Опорная сетка сброшена")
        return {'FINISHED'}


class AGR_OT_UVGridUnwrap(_AGR_UVGridPollMixin, Operator):
    """Map every target face into the 0..1 UV square of its grid cell"""
    bl_idname = "agr.uv_grid_unwrap"
    bl_label = "Развернуть по сетке"
    bl_description = ("Развернуть фейсы по опорной сетке: каждый фейс попадает "
                      "в UV-квадрат 0..1 своей ячейки. Если сетка не запомнена, "
                      "но выделено ровно 2 ребра — сетка запомнится автоматически")
    bl_options = {'REGISTER', 'UNDO'}

    def _execute(self, context):
        settings = _get_settings(context)
        if settings is None:
            return {'CANCELLED'}
        grid_state = _ensure_grid(self, context, settings)
        if grid_state is None:
            return {'CANCELLED'}
        if _capture_only_finish(self, context, settings, grid_state):
            return {'FINISHED'}
        if not _do_unwrap(self, context, settings):
            return {'CANCELLED'}
        return {'FINISHED'}


class AGR_OT_UVGridCut(_AGR_UVGridPollMixin, Operator):
    """Bisect the target faces along the grid lines"""
    bl_idname = "agr.uv_grid_cut"
    bl_label = "Разрезать по сетке"
    bl_description = ("Разрезать фейсы по линиям опорной сетки — после нарезки "
                      "каждый фейс помещается ровно в одну ячейку")
    bl_options = {'REGISTER', 'UNDO'}

    def _execute(self, context):
        settings = _get_settings(context)
        if settings is None:
            return {'CANCELLED'}
        grid_state = _ensure_grid(self, context, settings)
        if grid_state is None:
            return {'CANCELLED'}
        if _capture_only_finish(self, context, settings, grid_state):
            return {'FINISHED'}
        if not _do_cut(self, context, settings):
            return {'CANCELLED'}
        return {'FINISHED'}


class AGR_OT_UVGridCutUnwrap(_AGR_UVGridPollMixin, Operator):
    """Bisect along the grid lines, then unwrap each face into its cell"""
    bl_idname = "agr.uv_grid_cut_unwrap"
    bl_label = "Разрезать и развернуть"
    bl_description = ("Разрезать фейсы по линиям сетки и сразу развернуть: "
                      "каждый получившийся фейс займёт UV-квадрат 0..1 своей ячейки")
    bl_options = {'REGISTER', 'UNDO'}

    def _execute(self, context):
        settings = _get_settings(context)
        if settings is None:
            return {'CANCELLED'}
        grid_state = _ensure_grid(self, context, settings)
        if grid_state is None:
            return {'CANCELLED'}
        if _capture_only_finish(self, context, settings, grid_state):
            return {'FINISHED'}
        # any skipped object aborts before unwrap so the cut ERROR survives
        if not _do_cut(self, context, settings):
            return {'CANCELLED'}
        if not _do_unwrap(self, context, settings):
            return {'CANCELLED'}
        return {'FINISHED'}


# ============================================================
# Panel
# ============================================================

class AGR_PT_UVPanel(Panel):
    """AGR UV panel in the AGR Tools sidebar"""
    bl_label = "AGR UV"
    bl_idname = "AGR_PT_uv_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 40  # after AGR Sync (30), before AGR Share (100)

    def draw(self, context):
        layout = self.layout
        s = _get_settings(context)
        if s is None:
            return

        col = layout.column(align=True)
        row = col.row(align=True)
        row.prop(s, "grid_source", expand=True)

        if s.grid_source == 'EDGES':
            if s.has_grid:
                col.label(text=f"Ячейка: {s.cell_u:.3g} × {s.cell_v:.3g} м", icon='GRID')
            else:
                col.label(text="Сетка не задана", icon='GRID')
            row = col.row(align=True)
            row.operator("agr.uv_grid_capture", text="Запомнить сетку", icon='EYEDROPPER')
            sub = row.row(align=True)
            sub.enabled = s.has_grid
            sub.operator("agr.uv_grid_clear", text="", icon='X')
        else:
            row = col.row(align=True)
            row.prop(s, "world_cell_u", text="U")
            row.prop(s, "world_cell_v", text="V")
            col.prop(s, "world_angle", text="Поворот")
            row = col.row(align=True)
            row.prop(s, "origin_mode", expand=True)

        if context.mode != 'EDIT_MESH':
            layout.label(text="Инструменты работают в Edit Mode", icon='INFO')

        wm = context.window_manager
        row = layout.row()
        row.scale_y = 1.3
        icon = 'HIDE_OFF' if wm.agr_uv_grid_show else 'HIDE_ON'
        row.prop(wm, "agr_uv_grid_show", text="Показать сетку", icon=icon, toggle=True)
        if wm.agr_uv_grid_show and _uv_last_stats is not None:
            if "error" in _uv_last_stats:
                layout.label(text=_uv_last_stats["error"], icon='ERROR')
            else:
                layout.label(text=f"Линий реза: {_uv_last_stats['cuts']}", icon='INFO')
                if _uv_last_stats["truncated"]:
                    layout.label(text="Превью обрезано (слишком много линий)", icon='ERROR')

        layout.separator()
        col = layout.column(align=True)
        row = col.row(align=True)
        row.prop(s, "projection", expand=True)
        row = col.row(align=True)
        row.prop(s, "selection_mode", expand=True)
        col.prop(s, "auto_orient")
        row = col.row(align=True)
        row.prop(s, "swap_axes", toggle=True)
        row.prop(s, "flip_u", toggle=True)
        row.prop(s, "flip_v", toggle=True)
        row = col.row(align=True)
        row.prop(s, "offset_u", text="Сдвиг U")
        row.prop(s, "offset_v", text="Сдвиг V")
        col.prop(s, "snap_tolerance", text="Прилипание")

        layout.separator()
        col = layout.column(align=True)
        row = col.row(align=True)
        row.scale_y = 1.5
        row.operator("agr.uv_grid_unwrap", text="Развернуть по сетке", icon='UV')
        col.operator("agr.uv_grid_cut", text="Разрезать по сетке", icon='MESH_GRID')
        col.operator("agr.uv_grid_cut_unwrap", text="Разрезать и развернуть", icon='MOD_UVPROJECT')


# ============================================================
# Registration
# ============================================================

classes = (
    AGR_UVGridSettings,
    AGR_OT_UVGridCapture,
    AGR_OT_UVGridClear,
    AGR_OT_UVGridUnwrap,
    AGR_OT_UVGridCut,
    AGR_OT_UVGridCutUnwrap,
    AGR_PT_UVPanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.agr_uv_settings = PointerProperty(type=AGR_UVGridSettings)

    bpy.types.WindowManager.agr_uv_grid_show = BoolProperty(
        name="Показать сетку",
        description="Оверлей опорной сетки в реальном времени: оранжевые линии — "
                    "будущие разрезы на фейсах, решётка и оси U (красная) / "
                    "V (зелёная); обновляется при смене выделения и настроек",
        default=False,
        update=_uv_grid_toggle,
    )

    if _uv_sync_handlers_on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_uv_sync_handlers_on_load)

    # reloadOnSave: the WindowManager value survives re-registration while
    # the draw handlers do not — re-add them if the overlay was left on
    try:
        wm = bpy.context.window_manager
        if wm is not None and getattr(wm, "agr_uv_grid_show", False):
            _uv_add_handlers()
    except Exception:
        pass  # restricted context during Blender startup

    print("✅ AGR UV operators registered")


def unregister():
    if _uv_sync_handlers_on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_uv_sync_handlers_on_load)

    _uv_remove_handlers()

    if hasattr(bpy.types.WindowManager, "agr_uv_grid_show"):
        del bpy.types.WindowManager.agr_uv_grid_show

    # guarded like the WM prop: after a partially failed register() an
    # unguarded del would abort the WHOLE operators.py unregister chain
    if hasattr(bpy.types.Scene, "agr_uv_settings"):
        del bpy.types.Scene.agr_uv_settings

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
