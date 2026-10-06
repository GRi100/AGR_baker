"""AGR UV — grid-based planar UV mapping driven by a reference grid.

Three grid sources:
  * EDGES — the user picks two edges of ONE grid cell ("Запомнить сетку");
    they define the U/V axes, the cell sizes and the grid origin in WORLD
    space, so the stored grid works across every object in the scene.
  * WORLD — no edges needed: axes are derived from the mean normal of the
    target faces (floor -> U=+X/V=+Y, wall -> V=up/U=along the wall), the
    cell size is typed in manually, an optional angle rotates the grid
    around the normal, and the origin snaps to the selection corner or the
    world origin.
  * TOPZ — plan view, top-down: U=+X, V=+Y (rotated by world_angle around
    world +Z), origin at world (0,0,0) + the manual offsets, cut planes
    vertical.  It NEVER looks at the geometry: the mean normal is not even
    computed, auto-orientation is skipped and SURFACE degrades to PLANAR,
    so the grid cannot drift with the selection.  The price is deliberate:
    a downward-facing face comes out MIRRORED (auto-orient is what used to
    fix that) and a vertical face collapses in V — both are what a plan
    projection means, and the operator reports the second one.

Besides the grid there is one tool for the opposite kind of geometry —
"Органика" (see the organic section below): no plane describes an organic
mesh, so it is cut by a world 3D grid into pieces and every piece gets its
own UV square, projected along its own normal.

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
from math import atan2, ceil, cos, degrees, floor, hypot, pi, radians, sin
from mathutils import Matrix, Vector, geometry

from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
)
from bpy.types import Operator, Panel, PropertyGroup

from .log import agr_report, logger, unregister_classes
from .core.texture_sets import material_texture_entry
from .core.udim_tiles import face_tile_number, uv_to_udim_number
from .operators_udim import object_has_udim

# Safety cap for the cut operator: max grid lines per object (both axes).
# Measured on flat grids in 5.2 (the same chained-bisect cost the organic
# cut pays): 98 lines 0.02 s, 198 — 0.14 s, 398 — 0.92 s, 598 — 6.75 s
# (1.5x the lines = 7x the time).  The old 2048 was NOT a guard: a typo in
# the cell size on a 20x20 m facade (0.02 m instead of 0.2 m) passed it and
# froze the UI for minutes with no progress bar and no way to cancel.
_MAX_CUT_LINES = 640

# Scratch face int layer that carries the selection across bisect_plane in
# «Весь меш» mode (see _cut_object).  Created and dropped inside one call —
# it must never reach the mesh: the delivery checker forbids stray attrs.
_SEL_KEEP_LAYER = "agr_uv_sel_tmp"

# Faces whose UVs exceed the unit square by more than this are counted as
# "bigger than one cell" (the warning suggests cutting first)
_UNIT_EPS = 1e-3

# TOPZ (plan view): |cos| between the face normal and the grid plane normal
# below this means the face is (nearly) vertical, so the top-down projection
# squashes its UVs by 20x or more — reported, never silently accepted
_TOPZ_FLAT_EPS = 0.05

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
            ('TOPZ', "Сверху", "Плановая сетка вида сверху: U = мировой +X, V = мировой +Y, "
                               "начало в (0,0,0), разрезы — вертикальными плоскостями. "
                               "НЕ зависит от выделенной геометрии (нормали не смотрятся, "
                               "авто-ориентация и проекция «по поверхности» не применяются)"),
        ],
        default='EDGES',
        update=_tag_redraw_view3d,
    )

    # --- WORLD grid parameters ---
    world_cell_u: FloatProperty(
        name="Ячейка U",
        description="Размер ячейки мировой сетки по оси U (в режиме «Сверху» — "
                    "по мировому X)",
        subtype='DISTANCE',
        default=1.0, min=0.001, soft_max=100.0,
        update=_tag_redraw_view3d,
    )
    world_cell_v: FloatProperty(
        name="Ячейка V",
        description="Размер ячейки мировой сетки по оси V (в режиме «Сверху» — "
                    "по мировому Y)",
        subtype='DISTANCE',
        default=1.0, min=0.001, soft_max=100.0,
        update=_tag_redraw_view3d,
    )
    world_angle: FloatProperty(
        name="Поворот",
        description="Поворот сетки вокруг нормали фейсов (направление нарезки); "
                    "в режиме «Сверху» — вокруг мировой оси Z",
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

    # --- stub unwrap ---
    stub_threshold: IntProperty(
        name="Порог заглушки",
        description="Материал/тайл считается заглушкой, когда его наибольшая "
                    "текстура не превышает этот размер в пикселях",
        default=256, min=1, max=8192, subtype='PIXEL',
    )
    stub_margin: FloatProperty(
        name="Отступ",
        description="Масштаб фейса внутри UV-квадрата (0.9 = 5% отступ от "
                    "каждого края, чтобы развёртка не касалась границ)",
        default=0.9, min=0.1, max=1.0,
    )

    # --- organic (voxel-piece) unwrap ---
    organic_cell: FloatProperty(
        name="Кусок",
        description="Размер ячейки мировой 3D-сетки: меш режется на куски "
                    "примерно этого размера, и КАЖДЫЙ кусок разворачивается "
                    "на свой UV-квадрат",
        subtype='DISTANCE',
        default=0.25, min=0.001, soft_max=10.0,
    )
    organic_cut: BoolProperty(
        name="Резать меш",
        description="Разрезать геометрию по плоскостям 3D-сетки (X/Y/Z), чтобы "
                    "кусок не свисал в соседнюю ячейку. Выключено — только UV, "
                    "геометрия не меняется",
        default=True,
    )
    organic_angle: FloatProperty(
        name="Разброс нормалей",
        description="Максимальный угол между нормалью грани и средней нормалью "
                    "куска: кусок, который загибается сильнее, делится дальше. "
                    "Это защита от наложения UV на сгибах (уши, ноздри)",
        subtype='ANGLE',
        default=radians(60.0), min=radians(5.0), max=radians(89.0),
    )
    organic_merge: FloatProperty(
        name="Слить мелкие",
        description="Куски площадью меньше этой доли квадрата ячейки прилипают "
                    "к соседу с самой длинной общей границей (0 — не сливать; "
                    "иначе тонкие обрезки у линий реза получают по целому "
                    "квадрату текстуры)",
        default=0.15, min=0.0, max=1.0,
    )
    organic_margin: FloatProperty(
        name="Отступ",
        description="Масштаб куска внутри UV-квадрата (1.0 — вплотную к краям, "
                    "0.9 — 5% отступ с каждой стороны)",
        default=1.0, min=0.1, max=1.0,
    )
    organic_fill: EnumProperty(
        name="Заполнение",
        description="Как кусок ложится в свой UV-квадрат",
        items=[
            ('SCALE', "Один масштаб",
             "Один UV-квадрат = два размера куска в метрах (кусок может лежать "
             "по диагонали ячейки, поэтому с запасом): клетки текстуры "
             "одинаковы по всей модели, каждый кусок занимает свою часть "
             "квадрата. Самые «чёткие квадратики». Итоговый метр на квадрат "
             "пишется в отчёте"),
            ('STRETCH', "На весь квадрат",
             "Растянуть кусок на весь квадрат неравномерно — квадрат заполнен "
             "целиком, но мелкий кусок показывает текстуру крупнее соседей"),
            ('FIT', "Вписать",
             "Вписать кусок в квадрат с сохранением пропорций — пропорции "
             "текстуры целы, масштаб всё равно свой у каждого куска"),
        ],
        default='SCALE',
    )
    organic_align: EnumProperty(
        name="Поворот",
        description="Как ориентирован кусок внутри UV-квадрата",
        items=[
            ('WORLD', "По вертикали",
             "V куска смотрит вверх (мировой +Z) — текстура ориентирована "
             "одинаково на всей модели"),
            ('PCA', "По форме",
             "Длинную сторону куска положить в U — минимум растяжения"),
            ('NONE', "Без поворота",
             "Базис от первого ребра куска (как у развёртки заглушек)"),
        ],
        default='WORLD',
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


def _world_base_axes(n):
    """Unrotated WORLD-grid axes for the normalized mean normal `n`.

    The ONE derivation shared by _resolve_basis and the angle-from-edge
    pick (they MUST agree on what world_angle=0 means): floor/ceiling ->
    U=+X/V=+Y, wall -> U horizontal along the wall, V up the wall.
    world_angle then rotates these around `n`.
    """
    if abs(n.z) > 0.7:  # floor / ceiling
        return Vector((1.0, 0.0, 0.0)), Vector((0.0, 1.0, 0.0))
    x_dir = Vector((0.0, 0.0, 1.0)).cross(n).normalized()
    return x_dir, n.cross(x_dir).normalized()


# Pipettes, WORLD source: max spread between the link-face normals of the
# picked element.  Beyond this the element sits on a crease (wall∩roof,
# wall∩ground) and the area-weighted average normal is meaningless — the
# review repro showed a silently diagonal grid up to 90° off, so the honest
# move is refusing.  20° still tolerates curved facades (cylinder segments).
_PICK_CREASE_COS = cos(radians(20.0))


def _face_world_area_vector(mat, f):
    """Newell area vector of one face in WORLD space (length = 2·area)."""
    ws = [mat @ v.co for v in f.verts]
    n = Vector((0.0, 0.0, 0.0))
    for i in range(1, len(ws) - 1):
        n += (ws[i] - ws[0]).cross(ws[i + 1] - ws[0])
    return n


def _coplanar_normals(area_vectors, min_cos=_PICK_CREASE_COS):
    """True when every pair of link-face normals agrees within the limit."""
    dirs = [v.normalized() for v in area_vectors if v.length > 1e-12]
    return all(a.dot(b) >= min_cos
               for i, a in enumerate(dirs) for b in dirs[i + 1:])


def _pick_single_edge(context):
    """The ONE selected edge as (world_a, world_b, [face_area_vectors]).

    Returns None unless exactly one edge is selected across the edit-mode
    objects.  Everything is copied out immediately (no BMElem retention —
    see _selected_edges).  The per-face WORLD area vectors of the edge's
    link faces are the ONLY normal source the WORLD pipette can use: a
    selected face flushes ≥3 selected edges, so a live face selection can
    never coexist with this pick — deriving the hint from the selection
    was an unreachable branch (and the whole-mesh mean in «Весь меш» is
    zero on closed volumes), which the review confirmed empirically.
    """
    found = None
    for obj in _edit_mesh_objects(context):
        bm = bmesh.from_edit_mesh(obj.data)
        mat = obj.matrix_world
        for e in bm.edges:
            if not e.select:
                continue
            if found is not None:
                return None  # more than one selected edge
            vecs = [_face_world_area_vector(mat, f) for f in e.link_faces]
            found = (mat @ e.verts[0].co, mat @ e.verts[1].co, vecs)
    return found


def _pick_single_vert(context):
    """The ONE selected vertex as (world_co, [(obj, bm, link_faces)],
    [face_area_vectors]).

    Returns None unless exactly one vertex is selected: an edge selects
    both of its verts and a face all of them, so a single selected vertex
    can only come from a direct vertex-mode click — no ambiguity.  The
    link faces are the WORLD-source basis targets (live within THIS
    operator call only — never store them past an undo); the area vectors
    feed the same crease guard as the edge pick.
    """
    found = None
    for obj in _edit_mesh_objects(context):
        bm = bmesh.from_edit_mesh(obj.data)
        mat = obj.matrix_world
        for v in bm.verts:
            if not v.select:
                continue
            if found is not None:
                return None  # more than one selected vertex
            link = list(v.link_faces)
            vecs = [_face_world_area_vector(mat, f) for f in link]
            found = (mat @ v.co, [(obj, bm, link)] if link else [], vecs)
    return found


def _capture_grid(op, context, settings, picked=None):
    """Store the reference grid from the selected edges.

    Exactly 2 edges — precise capture from one cell (axes, sizes, origin).
    3+ edges — statistical autofit over the whole selection (median cell
    sizes, diagonals filtered out; see _capture_grid_autofit).
    """
    if picked is None:
        picked = _selected_edges(context)
    if len(picked) > 2:
        return _capture_grid_autofit(op, context, settings)
    if len(picked) != 2:
        agr_report(op, 'ERROR',
                   "Выделите 2 ребра одной ячейки (точный захват) или несколько "
                   f"рёбер сетки (автофит) — сейчас выделено: {len(picked)}")
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


# Autofit: edges farther than this from a family axis are discarded as
# diagonals/garbage.  15° keeps a solid margin both to noisy grid edges
# (millimeter noise is < 1°) and to the closest real diagonal (26.6° for
# a 2:1 cell, 45° for a square one).  Diagonals of longer cells (18.4° for
# 3:1, 14° for 4:1) need the median re-centering and the length signature
# in _autofit_solve on top — the window alone cannot separate them.
_AUTOFIT_ANGLE_TOL = pi / 12.0
# Coplanarity: edges leaving the grid plane by more than 30° are not grid
# edges (|d·n|/|d| = sine of the tilt; window returns/reveals sit at ~90°
# and would otherwise vote for angle 0 with full 3D weight — atan2(0,0)==0)
_AUTOFIT_PLANE_TOL = 0.5
# True grid edges sit sub-degree from their family axis (mm noise); an edge
# farther off AND matching the cell-diagonal length is a triangulation
# diagonal even when it fits the ±15° window
_AUTOFIT_TIGHT_TOL = pi / 90.0        # 2°
_AUTOFIT_DIAG_LEN_TOL = 0.1           # ±10% around hypot(cell_u, cell_v)
# Angle re-centering: the weighted-median shift converges in 2-3 passes
_AUTOFIT_MAX_PASSES = 6
_AUTOFIT_SHIFT_EPS = pi / 3600.0      # 0.05° — considered converged
# If the two families keep less than this fraction of the in-plane edge
# length, the "grid" explains a minority of the selection — refuse instead
# of committing a garbage basis (uniform direction mess keeps ~33%)
_AUTOFIT_MIN_KEPT_FRAC = 0.4
# Rerun the fit with the families' own plane normal when the face-area
# normal was tilted >2° by attached out-of-plane geometry (one-sided
# window returns drag it and shrink every projected length)
_AUTOFIT_REFIT_DOT = cos(pi / 90.0)


def _median_sorted(vals):
    """Median of an already sorted non-empty sequence."""
    k = len(vals)
    return vals[k // 2] if k % 2 else 0.5 * (vals[k // 2 - 1] + vals[k // 2])


def _weighted_median(pairs):
    """Weighted median of [(value, weight)] — the value where the running
    weight crosses half of the total.  Majority-robust: unlike the mean it
    ignores a coherent minority cluster (triangulation diagonals) entirely."""
    pairs = sorted(pairs)
    half = 0.5 * sum(w for _v, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= half:
            return v
    return pairs[-1][0]


def _collect_autofit_edges(context):
    """ALL selected edges + the area normal of their linked faces.

    Everything is copied out immediately (BMElem lifetime rule — see
    _selected_edges).  Returns ([(world_a, world_b, is_boundary)], normal).
    """
    edges = []
    normal = Vector((0.0, 0.0, 0.0))
    for obj in _edit_mesh_objects(context):
        bm = bmesh.from_edit_mesh(obj.data)
        mat = obj.matrix_world
        seen = set()
        for e in bm.edges:
            if not e.select:
                continue
            edges.append((mat @ e.verts[0].co, mat @ e.verts[1].co,
                          e.is_boundary))
            for f in e.link_faces:
                if f in seen:
                    continue
                seen.add(f)
                ws = [mat @ v.co for v in f.verts]
                for i in range(1, len(ws) - 1):
                    normal += (ws[i] - ws[0]).cross(ws[i + 1] - ws[0])
    return edges, normal


def _autofit_solve(edges, n):
    """One autofit pass against a fixed grid-plane normal `n`.

    Returns a dict: {'err': message} on failure, otherwise the fitted
    axes/cells/origin plus statistics for the report and `n_fit` — the
    plane normal implied by the surviving families — for the optional
    refit pass (see _capture_grid_autofit).
    """
    # Coplanarity filter: an edge leaving the grid plane (window return,
    # reveal) projects to ~nothing in-plane, so atan2(0, 0) == 0 made it
    # vote for angle 0 with its FULL 3D length and its 3D length poisoned
    # the family medians.  Drop such edges entirely and measure everything
    # that stays IN THE PLANE.
    data = []   # (a, b, in-plane unit dir, in-plane length, boundary, 3D unit dir)
    oop = 0     # out-of-plane rejects
    for a, b, boundary in edges:
        d = b - a
        l3 = d.length
        if l3 < 1e-9:
            continue
        if abs(d.dot(n)) > _AUTOFIT_PLANE_TOL * l3:
            oop += 1
            continue
        dp = d - n * d.dot(n)
        lp = dp.length
        if lp < 1e-9:
            oop += 1
            continue
        data.append((a, b, dp / lp, lp, boundary, d / l3))
    if len(data) < 3:
        msg = "Для автофита выделите минимум 3 ребра сетки"
        if oop:
            msg += f" (вне плоскости отброшено: {oop})"
        return {'err': msg}

    # reference frame: same convention as the WORLD grid source
    if abs(n.z) > 0.7:  # floor / ceiling
        u_ref = Vector((1.0, 0.0, 0.0))
        v_ref = Vector((0.0, 1.0, 0.0))
    else:  # wall
        u_ref = Vector((0.0, 0.0, 1.0)).cross(n).normalized()
        v_ref = n.cross(u_ref).normalized()

    # initial dominant angle: length-weighted circular mean of directions
    # folded to a 90° period (both families collapse onto one point)
    sc = cc = 0.0
    angles = []
    for _a, _b, du, lp, _bd, _d3 in data:
        phi = atan2(du.dot(v_ref), du.dot(u_ref)) % pi  # sign-free direction
        angles.append(phi)
        sc += lp * sin(4.0 * phi)
        cc += lp * cos(4.0 * phi)
    if abs(sc) < 1e-9 and abs(cc) < 1e-9:
        return {'err': "Направления рёбер не образуют выраженной сетки"}
    alpha = (atan2(sc, cc) / 4.0) % (pi / 2.0)

    # Iterative re-centering: the circular MEAN above is dragged by
    # diagonal votes (~8° on a triangulated 3:1 wall) and the ±15° window
    # anchored on the dragged angle keeps those diagonals in the family.
    # Re-anchor on the weighted MEDIAN of the family deviations instead —
    # the majority cluster (true grid edges) wins, and on the next pass
    # the window, now centered on the true axis, expels the diagonals.
    fam_a = fam_b = None
    dropped = 0
    for _ in range(_AUTOFIT_MAX_PASSES):
        fam_a, fam_b, devs = [], [], []  # edges at alpha / alpha+90°
        dropped = 0
        for entry, phi in zip(data, angles):
            delta = (phi - alpha) % pi
            dev_a = delta if delta < pi / 2.0 else delta - pi
            dev_b = delta - pi / 2.0
            if abs(dev_a) <= _AUTOFIT_ANGLE_TOL:
                fam_a.append((entry, dev_a))
                devs.append((dev_a, entry[3]))
            elif abs(dev_b) <= _AUTOFIT_ANGLE_TOL:
                fam_b.append((entry, dev_b))
                devs.append((dev_b, entry[3]))
            else:
                dropped += 1
        if not fam_a or not fam_b:
            return {'err': "Не нашлось двух перпендикулярных семейств рёбер "
                           "(в выделении одни диагонали?) — выделите рёбра "
                           "вдоль обеих осей сетки"}
        shift = _weighted_median(devs)
        if abs(shift) <= _AUTOFIT_SHIFT_EPS:
            break
        alpha = (alpha + shift) % (pi / 2.0)

    # U = the more horizontal family (same convention as the 2-edge capture)
    if alpha <= pi / 4.0:
        u_fam, v_fam, u_ang = fam_a, fam_b, alpha
    else:
        u_fam, v_fam, u_ang = fam_b, fam_a, alpha - pi / 2.0

    # Diagonal length signature: a 4:1 cell diagonal is only 14° off the
    # U axis — INSIDE the angle window, unreachable for any angle filter.
    # Estimate the cells from the tight (sub-2°) members and expel every
    # off-axis edge whose length matches hypot(cell_u, cell_v).
    tight_u = sorted(e[3] for e, dev in u_fam if abs(dev) <= _AUTOFIT_TIGHT_TOL)
    tight_v = sorted(e[3] for e, dev in v_fam if abs(dev) <= _AUTOFIT_TIGHT_TOL)
    if tight_u and tight_v:
        diag = hypot(_median_sorted(tight_u), _median_sorted(tight_v))
        band = _AUTOFIT_DIAG_LEN_TOL * diag

        def _purge(fam):
            return [(e, dev) for e, dev in fam
                    if abs(dev) <= _AUTOFIT_TIGHT_TOL
                    or abs(e[3] - diag) > band]

        u_kept, v_kept = _purge(u_fam), _purge(v_fam)
        dropped += (len(u_fam) - len(u_kept)) + (len(v_fam) - len(v_kept))
        u_fam, v_fam = u_kept, v_kept

    # Refuse when the fitted grid explains only a minority of the
    # selection — committing a basis built from 3 edges out of 200 is
    # worse than an honest error
    total_w = sum(e[3] for e in data)
    kept_w = (sum(e[3] for e, _dev in u_fam)
              + sum(e[3] for e, _dev in v_fam))
    if len(u_fam) + len(v_fam) < 3 or kept_w < _AUTOFIT_MIN_KEPT_FRAC * total_w:
        return {'err': "Рёбра сетки — меньшинство выделения (отброшено "
                       f"{dropped + oop} из {len(data) + oop} рёбер): похоже, "
                       "выделение не лежит на регулярной сетке"}

    def fam_axis(fam, ref):
        """Length-weighted mean of the family's in-plane directions,
        sign-aligned to ref so opposite edges don't cancel out."""
        acc = Vector((0.0, 0.0, 0.0))
        for (_a, _b, du, lp, _bd, _d3), _dev in fam:
            acc += (-du if du.dot(ref) < 0.0 else du) * lp
        return acc

    x_acc = fam_axis(u_fam, u_ref * cos(u_ang) + v_ref * sin(u_ang))
    if x_acc.length < 1e-9:
        return {'err': "Семейство U вырождено — сетка не определяется"}
    x_dir = x_acc.normalized()
    y_dir = n.cross(x_dir).normalized()
    y_raw = fam_axis(v_fam, y_dir)
    if y_raw.length > 1e-9 and y_dir.dot(y_raw.normalized()) < 0:
        y_dir = -y_dir

    def fam_cell(fam):
        """Median in-plane length; interior edges only when there are
        enough of them (partial cells live on mesh borders), spread flag
        on top."""
        interior = [e[3] for e, _dev in fam if not e[4]]
        pool = interior if len(interior) >= 3 else [e[3] for e, _dev in fam]
        pool = sorted(pool)
        med = _median_sorted(pool)
        rough = sum(1 for lp in pool if abs(lp - med) > 0.2 * med)
        return med, rough > 0.25 * len(pool)

    cell_u, warn_u = fam_cell(u_fam)
    cell_v, warn_v = fam_cell(v_fam)
    if cell_u < 1e-9 or cell_v < 1e-9:
        return {'err': "Медианная длина рёбер нулевая — ячейка вырождена"}

    # origin = bounding corner of the selection (incl. diagonal endpoints —
    # their verts are grid corners too; out-of-plane edges are excluded,
    # their far ends are NOT grid corners)
    p0 = data[0][0]
    min_u = min_v = None
    for a, b, _du, _lp, _bd, _d3 in data:
        for p in (a, b):
            rel = p - p0
            gu, gv = rel.dot(x_dir), rel.dot(y_dir)
            min_u = gu if min_u is None else min(min_u, gu)
            min_v = gv if min_v is None else min(min_v, gv)
    origin = p0 + x_dir * min_u + y_dir * min_v

    # Plane normal implied by the surviving families' RAW 3D directions —
    # when the face-area normal was tilted by attached out-of-plane faces
    # (a one-sided window return), this one is clean and drives the refit
    u3 = Vector((0.0, 0.0, 0.0))
    v3 = Vector((0.0, 0.0, 0.0))
    for (_a, _b, _du, lp, _bd, d3), _dev in u_fam:
        u3 += (-d3 if d3.dot(x_dir) < 0.0 else d3) * lp
    for (_a, _b, _du, lp, _bd, d3), _dev in v_fam:
        v3 += (-d3 if d3.dot(y_dir) < 0.0 else d3) * lp
    n_fit = u3.cross(v3)
    if n_fit.length > 1e-6:
        n_fit.normalize()
        if n_fit.dot(n) < 0.0:
            n_fit = -n_fit
    else:
        n_fit = n

    return {
        'err': None, 'n': n, 'n_fit': n_fit,
        'x_dir': x_dir, 'y_dir': y_dir,
        'cell_u': cell_u, 'cell_v': cell_v, 'origin': origin,
        'warn': warn_u or warn_v,
        'n_u': len(u_fam), 'n_v': len(v_fam),
        'dropped': dropped, 'oop': oop,
    }


def _capture_grid_autofit(op, context, settings):
    """Fit the reference grid statistically from 3+ selected edges.

    A 2-edge capture trusts exactly those two edges — millimeter mesh noise
    in them shifts the WHOLE grid.  The autofit averages it out instead
    (the pipeline itself lives in _autofit_solve):

      1. coplanarity filter: edges leaving the grid plane by >30° (window
         returns, reveals) are dropped, and every direction/length below
         is measured IN THE PLANE — the 3D length of a tilted edge is not
         a grid pitch;
      2. the dominant in-plane grid angle starts as a length-weighted
         circular mean of edge directions folded to a 90° period, then is
         re-anchored on the weighted MEDIAN of family deviations until it
         converges (the mean alone is dragged by triangulation diagonals);
      3. edges within _AUTOFIT_ANGLE_TOL of the two perpendicular family
         axes are kept; off-axis edges whose length matches the cell
         diagonal hypot(cell_u, cell_v) are expelled on top;
      4. if the surviving families explain < _AUTOFIT_MIN_KEPT_FRAC of the
         in-plane edge length, the capture is REFUSED — 3 edges out of 200
         is not a fit;
      5. cell sizes = MEDIAN in-plane edge length per family — robust to
         trimmed border cells and noise while outliers stay under 50%;
         interior (non-boundary) edges are preferred when there are at
         least 3 of them, so partial cells on mesh borders don't vote;
      6. axes = length-weighted mean of each family's sign-aligned
         directions, orthonormalized like the 2-edge capture (auto-orient
         at use time handles handedness/V-up on top);
      7. origin = bounding corner of the selection in grid axes;
      8. the whole fit reruns once against the plane normal implied by the
         fitted families when the face-area normal was tilted >2° by
         attached out-of-plane geometry (one-sided window returns shrink
         every projected length otherwise).
    """
    edges, n_sum = _collect_autofit_edges(context)
    if len(edges) < 3:
        agr_report(op, 'ERROR',
                   "Для автофита выделите минимум 3 ребра сетки")
        return False
    if n_sum.length < 1e-6:
        agr_report(op, 'ERROR',
                   "Не удалось определить нормаль поверхности у выделенных рёбер")
        return False

    fit = _autofit_solve(edges, n_sum.normalized())
    if fit['err'] is None and fit['n_fit'].dot(fit['n']) < _AUTOFIT_REFIT_DOT:
        refit = _autofit_solve(edges, fit['n_fit'])
        if refit['err'] is None:
            fit = refit  # keep the tilted-plane fit when the refit fails
    if fit['err'] is not None:
        agr_report(op, 'ERROR', fit['err'])
        return False

    settings.origin = fit['origin']
    settings.u_dir = fit['x_dir']
    settings.v_dir = fit['y_dir']
    settings.cell_u = fit['cell_u']
    settings.cell_v = fit['cell_v']
    settings.has_grid = True
    _tag_redraw_view3d()  # the grid overlay must repaint with the new grid

    msg = (f"Сетка (автофит): ячейка {fit['cell_u']:.3g} × {fit['cell_v']:.3g} м "
           f"по рёбрам U:{fit['n_u']} V:{fit['n_v']}")
    if fit['dropped']:
        msg += f", отброшено (диагонали и пр.): {fit['dropped']}"
    if fit['oop']:
        msg += f", вне плоскости сетки: {fit['oop']}"
    if fit['warn']:
        agr_report(op, 'WARNING',
                   msg + " — длины рёбер сильно разбросаны, проверьте выделение")
    else:
        agr_report(op, 'INFO', msg)
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
               "Сетка не задана — выделите 2 ребра ячейки (или несколько рёбер — "
               "автофит) и нажмите «Запомнить сетку», или переключитесь на "
               "мировую сетку / «Сверху»")
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

    TOPZ is the deliberate exception to all of that: the basis is nailed to
    the world axes (U=+X, V=+Y, origin 0) and the face normals are NEVER
    consulted — the mean normal is not even computed, so the plan-view grid
    cannot drift with the selection.
    """
    topz = settings.grid_source == 'TOPZ'
    # structural guarantee, not just a skipped branch: TOPZ never reads the
    # geometry orientation (and saves the O(faces) Newell pass)
    n_hint = (Vector((0.0, 0.0, 0.0)) if topz
              else _mean_world_normal(_orientation_targets(targets)))

    if topz:
        # plan view, top-down: U along world +X, V along world +Y, rotated
        # around world +Z by the manual angle
        x_dir = Vector((1.0, 0.0, 0.0))
        y_dir = Vector((0.0, 1.0, 0.0))
        if settings.world_angle != 0.0:
            rot = Matrix.Rotation(settings.world_angle, 3, Vector((0.0, 0.0, 1.0)))
            x_dir = (rot @ x_dir).normalized()
            y_dir = (rot @ y_dir).normalized()
        origin = Vector((0.0, 0.0, 0.0))
        cell_u, cell_v = settings.world_cell_u, settings.world_cell_v
    elif settings.grid_source == 'EDGES':
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
        x_dir, y_dir = _world_base_axes(n)
        if settings.world_angle != 0.0:
            rot = Matrix.Rotation(settings.world_angle, 3, n)
            x_dir = (rot @ x_dir).normalized()
            y_dir = (rot @ y_dir).normalized()
        origin = Vector((0.0, 0.0, 0.0))
        cell_u, cell_v = settings.world_cell_u, settings.world_cell_v

    # TOPZ ignores the swap: it flips the basis handedness, and the
    # auto-orient that would compensate is structurally OFF here (n_hint is
    # zero), so the whole unwrap came out MIRRORED - the shoelace UV area
    # flipped sign on every face with nothing in the report.  The sanctioned
    # rotation for the plan grid is world_angle (90° = swapped axes).
    if settings.swap_axes and settings.grid_source != 'TOPZ':
        x_dir, y_dir = y_dir, x_dir
        cell_u, cell_v = cell_v, cell_u

    # TOPZ falls out here on its own: n_hint is the zero vector by
    # construction, so auto-orientation can never rotate the plan grid
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
    # the final axis orientation is known (the "corner" depends on it).
    # The test is deliberately `== 'WORLD'`, NOT `!= 'EDGES'`: merging the
    # two non-EDGES sources here would make TOPZ read the selection again
    # (this scan visits every target vertex) and kill its whole premise.
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


def _surface_mode(settings):
    """True when the arc-length SURFACE projection is actually in effect.

    The ONE place that decides it: TOPZ is a plan-view projection by
    definition (its axes are horizontal and its U must stay a world X
    coordinate), so the per-face arc-U machinery is forced off there — cut,
    unwrap and the overlay preview all ask this function instead of reading
    settings.projection directly, which is what keeps them in lockstep.
    """
    return settings.projection == 'SURFACE' and settings.grid_source != 'TOPZ'


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
    if not _surface_mode(settings):
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


def _note_uv_overwrite_counts(obj, n_total, n_touched, atlas_objects, udim_objects):
    """Track objects whose special UV state gets overwritten by a rewrite."""
    if obj.get('agr_atlas_applied'):
        if n_touched == n_total:
            # every atlas UV gets rewritten -> the re-apply guard would
            # only block a now-perfectly-valid atlas application
            del obj['agr_atlas_applied']
        atlas_objects.append(obj.name)
    if object_has_udim(obj):
        # integer UV part IS the tile number for the UDIM tools
        udim_objects.append(obj.name)


def _note_uv_overwrite(obj, bm, faces, atlas_objects, udim_objects):
    _note_uv_overwrite_counts(obj, len(bm.faces), len(faces),
                              atlas_objects, udim_objects)


# ============================================================
# Stub unwrap: every face of a stub material/tile fills its unit square
# ============================================================

_STUB_EPS = 1e-9


def _world_normal(pts):
    """Newell normal of a polygon given by its WORLD points.

    The same construction as `_face_world_area_vector`: derived from the
    world coordinates, it already carries the flip a mirrored instance
    (negative matrix determinant) introduces, so the basis built on it is
    right-handed IN THE WORLD, not merely in object space.
    """
    n = Vector((0.0, 0.0, 0.0))
    for i in range(1, len(pts) - 1):
        n += (pts[i] - pts[0]).cross(pts[i + 1] - pts[0])
    return n


def _stub_face_uvs(pts, normal, tile_uv=(0.0, 0.0), margin=0.9):
    """Project ONE face onto its own plane and stretch it to fill the unit
    square (user decision: faces are laid on top of each other), then scale
    by `margin` around (0.5, 0.5) and shift into its UDIM tile.

    `pts` and `normal` must be in the SAME space, and the callers pass
    WORLD space: with local coordinates a mirrored instance (negative
    determinant) unwraps mirrored relative to its twin.

    Basis: x = first edge projected into the plane (rotation-stable),
    y = n × x, so (x × y)·n = +1 and the mapping is never mirrored.
    Returns [(u, v)] in loop order, or None for a degenerate face."""
    n = normal.normalized()
    if n.length < 0.5:
        return None
    x = pts[1] - pts[0]
    x = x - n * x.dot(n)
    if x.length < _STUB_EPS:
        up = Vector((0.0, 0.0, 1.0)) if abs(n.z) < 0.9 else Vector((1.0, 0.0, 0.0))
        x = n.cross(up)
        if x.length < _STUB_EPS:
            return None
    x.normalize()
    y = n.cross(x)
    flat = [((p - pts[0]).dot(x), (p - pts[0]).dot(y)) for p in pts]
    min_x = min(p[0] for p in flat)
    max_x = max(p[0] for p in flat)
    min_y = min(p[1] for p in flat)
    max_y = max(p[1] for p in flat)
    width = max_x - min_x
    height = max_y - min_y
    if width < _STUB_EPS or height < _STUB_EPS:
        return None
    out = []
    for px, py in flat:
        u = (px - min_x) / width    # NON-UNIFORM stretch fills both axes
        v = (py - min_y) / height
        u = 0.5 + (u - 0.5) * margin  # margin keeps UVs off the square edges
        v = 0.5 + (v - 0.5) * margin
        out.append((u + tile_uv[0], v + tile_uv[1]))
    return out


# Tile arithmetic lives in core/udim_tiles so that this module (producer)
# and operators_udim (consumer of the very same numbers) cannot drift apart
# on the cell borders again.  The two thin wrappers keep the old private
# names alive for the existing headless tests.
_uv_to_udim_number = uv_to_udim_number
_face_tile_number = face_tile_number


def _tile_offset(num):
    """UDIM number -> integer (u, v) offset of its unit square."""
    return float((num - 1001) % 10), float((num - 1001) // 10)


# ============================================================
# Organic unwrap: cut a complex mesh with a world 3D grid and give
# every resulting piece its own clean UV square
# ============================================================
#
# The grid tools above assume a plane (a facade, a floor).  Organic shapes
# — a sculpt, a tree, Suzanne — have no such plane, and every projection
# onto ONE plane smears the texture where the surface turns away.  The
# approach here is the opposite: chop the surface into pieces small enough
# to be nearly flat, then project EACH piece along ITS OWN normal into its
# own 0..1 square.  The texel density is then set by the piece size, so a
# checker stays a checker on curvature instead of stretching into streaks.
#
# piece = edge-connected region inside ONE cell of the world voxel grid,
#         with the face normals kept inside a cone (no fold-over),
#         after tiny off-cuts have been absorbed by their neighbours.
#
# All pieces share the same unit square (they overlap in UV) exactly like
# the stub unwrap does per face: with a tiling/checker texture every piece
# shows one clean tile.  On a UDIM object a piece stays in the tile its
# faces already vote for, so the integer UV part keeps its tile meaning.

_ORGANIC_EPS = 1e-9
_ORGANIC_MERGE_PASSES = 8

# Organic gets its OWN, much tighter plane budget than the grid tools.  The
# grid cutter spends _MAX_CUT_LINES over a planar selection in two axes;
# organic chains every plane against the whole growing geometry in THREE, and
# the cost is superlinear — measured on flat grids in 5.2: 198 planes 0.47 s,
# 398 planes 2.5 s, 798 planes 29.4 s (2x the planes, 12x the time).  At 2048
# that is ten minutes of frozen UI with no progress bar and no way to cancel,
# which is not a usable outcome for anyone; 512 keeps the worst case in the
# seconds range and the operator says plainly to enlarge the piece instead.
_ORGANIC_MAX_CUT_LINES = 512

# SCALE mode: how many cells one UV square is worth.  A piece never spans
# more than one voxel, but it can lie across it DIAGONALLY, so its extent in
# its own tangent frame reaches ~1.4 cells (measured on Suzanne subdiv 0..3,
# cells 0.12..0.5: median 1.05, p90 1.40, max ~2.0 once sliver merging joins
# two voxels).  Anchoring the square at 2 cells is what makes the texel size
# come out IDENTICAL on ~every piece instead of "identical except for the
# two thirds that had to be shrunk to fit" — and it stays a CONSTANT, so two
# objects unwrapped in separate runs still share one texture scale.
_ORGANIC_SCALE_SLACK = 2.0
_ORGANIC_FOLD_EPS = -1e-12
_ORGANIC_AXES = (Vector((1.0, 0.0, 0.0)),
                 Vector((0.0, 1.0, 0.0)),
                 Vector((0.0, 0.0, 1.0)))


def _matrix_close(a, b, tol=1e-6):
    """Same world transform to within `tol` (per element)."""
    return all(abs(a[r][c] - b[r][c]) <= tol
               for r in range(4) for c in range(4))


def _organic_params(settings):
    """Operator-independent snapshot of the organic settings."""
    cell = max(float(settings.organic_cell), 1e-4)
    return {
        'cell': cell,
        # metres per UV square in SCALE mode (see _ORGANIC_SCALE_SLACK)
        'tile': cell * _ORGANIC_SCALE_SLACK,
        'cut': bool(settings.organic_cut),
        'cos_half': cos(max(min(float(settings.organic_angle), pi / 2 - 1e-6), 1e-6)),
        'merge': float(settings.organic_merge),
        'margin': float(settings.organic_margin),
        'fill': settings.organic_fill,
        'align': settings.organic_align,
    }


def _organic_stats():
    # 'dirty' is not a statistic: it says the BMesh was actually mutated.
    # In Object mode that BMesh is a throw-away copy and the flag is
    # ignored; in Edit Mode it IS the mesh, and then the operator must end
    # with {'FINISHED'} even having unwrapped nothing (see _organic_report)
    return {'patches': 0, 'faces': 0, 'cuts': 0,
            'degenerate': 0, 'out_of_tiles': 0,
            'folds': 0, 'merged': 0, 'crossed_tiles': 0,
            'rescaled': 0, 'dirty': False,
            'atlas_objects': [], 'udim_objects': [], 'modifier_objects': [],
            'shared_matrix_objects': []}


def _organic_world_table(mat, faces):
    """{BMVert: world coordinate} for every corner of `faces`, built ONCE.

    Without it a shared vertex is transformed once per incident face in
    _organic_face_data (~4x on a quad mesh) and then AGAIN per piece in
    _organic_patch_uvs — five matrix multiplications where one is enough.
    The expression is the same `mat @ v.co`, so the numbers are bit-identical
    to the per-face version; only the count changes.

    The entries are shared mutable Vectors: read them, never write into one
    in place (`a - b`, `p.dot(x)` are fine, `p -= n` is not).
    """
    world = {}
    for f in faces:
        for v in f.verts:
            if v not in world:
                world[v] = mat @ v.co
    return world


def _organic_face_data(world, faces):
    """World centroid, area-vector normal and area per face (Newell).

    The area vector (length == 2 x area) doubles as the weight: summing it
    over a set of faces gives the area-weighted mean normal for free, and
    that works under any object transform including negative scale.

    `world` is the shared per-vertex table from _organic_world_table.
    """
    centroids, normals, areas = {}, {}, {}
    for f in faces:
        ws = [world[v] for v in f.verts]
        c = Vector((0.0, 0.0, 0.0))
        for p in ws:
            c += p
        centroids[f] = c / len(ws)
        n = Vector((0.0, 0.0, 0.0))
        for i in range(1, len(ws) - 1):
            n += (ws[i] - ws[0]).cross(ws[i + 1] - ws[0])
        normals[f] = n
        areas[f] = n.length * 0.5
    return centroids, normals, areas


def _organic_plan_cut(mat, faces, cell):
    """Interior world-grid planes crossing the faces, per world axis.

    Returns [(axis, [k, ...]), ...], or None when the total exceeds
    _ORGANIC_MAX_CUT_LINES — the caller must then abort BEFORE mutating
    anything (a cancelled operator pushes no undo step, so a partial cut
    would be fused into the previous undo entry).
    """
    verts = {v for f in faces for v in f.verts}
    if not verts:
        return []
    world = [mat @ v.co for v in verts]
    plan = []
    total = 0
    for ax in _ORGANIC_AXES:
        ds = [p.dot(ax) / cell for p in world]
        lo, hi = min(ds), max(ds)
        # count arithmetically BEFORE materialising the list: a
        # kilometres-long mesh at a millimetre cell used to build ~10^7
        # ints (hundreds of MB, ~1 s) only for the cap below to throw them
        # away (_do_cut pre-checks its span the same way).  The formula is
        # exactly _interior_lines' predicate — integers k with
        # lo + eps < k < hi - eps.
        n_ax = max(0, ceil(hi - _LINE_EPS) - floor(lo + _LINE_EPS) - 1)
        total += n_ax
        if total > _ORGANIC_MAX_CUT_LINES:
            return None
        plan.append((ax, _interior_lines(lo, hi)))
    return plan


def _organic_cut(bm, mat, faces, cell, plan, reselect=False):
    """Bisect `faces` along the planned world planes.

    Returns (surviving + new faces, number of bisects).  Same chaining as
    _do_cut: res['geom'] carries survivors AND everything new, so every
    following plane sees the complete geometry.

    reselect=True restores the face selection afterwards (Edit Mode): bisect
    creates its faces with select=False AND replaces the originals, so the
    user's selection comes out EMPTY otherwise - the next selection-based run
    would then find nothing to work on.  Like _do_cut, the assignment is left
    to flush DOWN to verts/edges on its own and never flushed upward.
    """
    mat_inv = mat.inverted_safe()
    # world normal -> local plane normal (transpose, NOT inverse: correct
    # under non-uniform scale)
    nrm_to_local = mat.to_3x3().transposed()
    obj_scale = max(abs(c) for c in mat.to_scale())
    weld = 1e-5 / max(obj_scale, 1e-9)
    verts = {v for f in faces for v in f.verts}
    edges = {e for f in faces for e in f.edges}
    geom = list(verts) + list(edges) + list(faces)
    n_cuts = 0
    for ax, ks in plan:
        if not ks:
            continue
        plane_no = (nrm_to_local @ ax).normalized()
        for k in ks:
            res = bmesh.ops.bisect_plane(
                bm, geom=geom,
                plane_co=mat_inv @ (ax * (k * cell)),
                plane_no=plane_no,
                dist=weld,
            )
            geom = res['geom']
            n_cuts += 1
    out = [g for g in geom
           if isinstance(g, bmesh.types.BMFace) and g.is_valid]
    if reselect and n_cuts:
        for f in out:
            f.select = True
    return out, n_cuts


def _organic_in_cone(normal, axis, cos_half):
    """True when `normal` lies inside the cone around `axis`.

    A zero-length normal (degenerate face) is a wildcard: it carries no
    orientation of its own and must not be able to break a piece apart.
    """
    if normal.length < _ORGANIC_EPS or axis.length < _ORGANIC_EPS:
        return True
    return normal.dot(axis) >= cos_half * normal.length * axis.length


def _organic_patch_axis(patch, normals):
    """Area-weighted mean normal of a piece (sum of the area vectors)."""
    n = Vector((0.0, 0.0, 0.0))
    for f in patch:
        n += normals[f]
    return n


def _organic_split_cone(pool, normals, cos_half):
    """Partition `pool` into connected pieces whose faces ALL lie inside the
    cone around the piece's FINAL mean normal.

    Greedy growth on its own is not enough, and that is the subtle part:
    the running mean drifts while the BFS walks across curvature, so the
    faces a piece started from can end up far outside the cone of the
    finished piece — past 90 deg that is a projection FOLD, i.e. UVs of two
    faces laid on top of each other.  Hence every grown piece is validated
    against its own final mean; a piece with failing faces is split into the
    passing and failing halves and BOTH are partitioned again.  Every
    recursion works on a strictly smaller set, so this terminates, and the
    invariant it leaves behind ("every face is within cos_half of its
    piece's mean") is what makes the flattening fold-free by construction.
    """
    from collections import deque

    result = []
    work = [sorted(pool, key=lambda f: f.index)]
    while work:
        pending = work.pop()
        if not pending:
            continue
        pset = set(pending)
        # --- growth: edge-connected + running-mean cone ---
        taken = set()
        grown = []
        for seed in pending:
            if seed in taken:
                continue
            axis = normals[seed].copy()
            patch = [seed]
            taken.add(seed)
            queue = deque((seed,))
            while queue:
                f = queue.popleft()
                for e in f.edges:
                    if len(e.link_faces) != 2:
                        # a non-manifold edge (3+ sheets) has no meaningful
                        # "other side" — growing across it would flatten two
                        # sheets onto one plane.  Boundary edges (1 face)
                        # fall out here too, at no cost
                        continue
                    for g in e.link_faces:
                        if g in taken or g not in pset:
                            continue
                        if not _organic_in_cone(normals[g], axis, cos_half):
                            continue          # would fold the projection
                        axis = axis + normals[g]
                        taken.add(g)
                        patch.append(g)
                        queue.append(g)
            grown.append(patch)
        # --- validation against the final mean ---
        for patch in grown:
            if len(patch) == 1:
                result.append(patch)
                continue
            mean = _organic_patch_axis(patch, normals)
            good, bad = [], []
            for f in patch:
                (good if _organic_in_cone(normals[f], mean, cos_half)
                 else bad).append(f)
            if not bad or not good:
                result.append(patch)
            else:
                work.append(good)
                work.append(bad)
    return result


def _organic_build_patches(mat, faces, cell, cos_half, world=None):
    """Split the faces into pieces: one piece = an edge-connected region
    inside ONE voxel of the world grid whose normals stay inside a cone.

    Three constraints, each doing one job:
      * the voxel key LOCALISES a piece — no piece is bigger than the cell,
        and the pieces tile the model in world space (so the texture scale
        is predictable and independent of the object's own orientation);
      * edge connectivity keeps a piece in ONE part (two lumps of surface
        crossing the same cell must not share a square);
      * the normal cone is what keeps the later planar projection
        fold-free: a region wrapping around a ridge (an ear, a nostril)
        is split instead of being flattened on top of itself.

    `world` is the shared per-vertex world table (_organic_world_table);
    None means "build it here" — the path used by callers that only hold
    the matrix, e.g. the unit checks in scripts/test_uv_organic.py.

    Returns (patches, centroids, normals, areas).
    """
    if world is None:
        world = _organic_world_table(mat, faces)
    centroids, normals, areas = _organic_face_data(world, faces)
    groups = {}
    for f in faces:
        c = centroids[f]
        key = (floor(c.x / cell), floor(c.y / cell), floor(c.z / cell))
        groups.setdefault(key, []).append(f)

    patches = []
    for key in sorted(groups):          # deterministic voxel order
        patches.extend(_organic_split_cone(groups[key], normals, cos_half))
    return patches, centroids, normals, areas


def _organic_merge_slivers(patches, normals, areas, cell, frac, cos_half):
    """Absorb tiny pieces into the neighbour with the longest shared border.

    Bisecting along a grid leaves thin off-cuts against every cut line.
    Left alone each of them claims a WHOLE texture square, so a 2 cm sliver
    would show the tile at 10x the density of its neighbours — the single
    most visible artefact of the whole approach.  A sliver is only absorbed
    by a neighbour inside the same normal cone: merging across a ridge
    would re-introduce exactly the fold the cone split prevented.

    Returns the new patch list (order deterministic).
    """
    if frac <= 0.0 or not patches:
        return patches, 0
    limit = frac * cell * cell
    members = {i: list(p) for i, p in enumerate(patches)}
    owner = {}
    for i, p in members.items():
        for f in p:
            owner[f] = i
    area = {i: sum(areas[f] for f in p) for i, p in members.items()}
    axis = {}
    for i, p in members.items():
        n = Vector((0.0, 0.0, 0.0))
        for f in p:
            n += normals[f]
        axis[i] = n

    merged_total = 0
    for _pass in range(_ORGANIC_MERGE_PASSES):
        small = sorted((i for i in members if area[i] < limit),
                       key=lambda i: (area[i], i))
        if not small:
            break
        merged = 0
        for i in small:
            if i not in members:
                continue                      # absorbed earlier this pass
            if area[i] >= limit:
                # it ALREADY absorbed a sliver this pass and is no longer
                # small: without this re-check the merge chains (A into B,
                # then the fattened B into C), and the ascending sort makes
                # that the normal case rather than the exception — six 0.9x
                # neighbours collapsed into one 5.4x piece.  An oversized
                # piece then trips the SCALE rescale and shows the texture
                # coarser than everything around it, which is the very
                # artefact this function exists to remove.
                continue
            share = {}
            for f in members[i]:
                for e in f.edges:
                    for g in e.link_faces:
                        j = owner.get(g)
                        if j is None or j == i:
                            continue
                        share[j] = share.get(j, 0.0) + e.calc_length()
            best, best_len = None, 0.0
            ai = axis[i]
            for j in sorted(share):
                if share[j] <= best_len:
                    continue
                # cheap reject on the two means, then the real test: EVERY
                # face of the sliver must survive inside the cone of the
                # MERGED mean — otherwise the merge would re-introduce the
                # very fold the cone split just prevented (the target's own
                # faces are safe: a sliver under the area limit barely moves
                # an area-weighted mean)
                if not _organic_in_cone(ai, axis[j], cos_half):
                    continue
                merged_axis = axis[j] + ai
                if not all(_organic_in_cone(normals[f], merged_axis, cos_half)
                           for f in members[i]):
                    continue
                best, best_len = j, share[j]
            if best is None:
                continue                      # no compatible neighbour
            for f in members[i]:
                owner[f] = best
            members[best].extend(members[i])
            area[best] += area[i]
            axis[best] = axis[best] + ai
            del members[i], area[i], axis[i]
            merged += 1
        merged_total += merged
        if not merged:
            break
    return [members[i] for i in sorted(members)], merged_total


def _organic_patch_uvs(patch, world, normals, params):
    """({BMVert: (u, v)}, rescaled) — ONE piece mapped onto the 0..1 square.

    `world` is the shared per-vertex world table (_organic_world_table): the
    piece builder already transformed these vertices, so re-transforming
    them here would be the second of five passes over the same matrix.

    Projection goes along the piece's OWN mean normal (that is the whole
    point of the voxel pieces: the texel density follows the surface, not
    one global plane), and the basis is right-handed w.r.t. that normal, so
    the mapping is never mirrored.  Returns None for a degenerate piece
    (no area, or a piece that collapses onto a line).

    Three fill modes, and the difference is what the texture DOES:
      * SCALE   — 1 UV square == `cell` x _ORGANIC_SCALE_SLACK metres, piece
                  centred.  The texel size is then the same on every piece
                  of every object, so a checker reads as one crisp grid over
                  the whole model.  This is the default; the rare piece that
                  still does not fit is uniformly shrunk and reported
                  (`rescaled`).
      * STRETCH — the piece bbox fills the square (non-uniform).  Every
                  piece shows one whole tile, like the stub unwrap, so a
                  small piece shows the texture bigger than its neighbours.
      * FIT     — uniform version of STRETCH: aspect kept, square not full.
    """
    align, fill, margin = params['align'], params['fill'], params['margin']
    n = Vector((0.0, 0.0, 0.0))
    for f in patch:
        n += normals[f]
    if n.length < _ORGANIC_EPS:
        for f in patch:               # fully folded piece: fall back to a face
            if normals[f].length > _ORGANIC_EPS:
                n = normals[f].copy()
                break
    if n.length < _ORGANIC_EPS:
        return None
    n.normalize()

    x = y = None
    if align == 'NONE':
        vs = list(patch[0].verts)
        if len(vs) > 1:
            e = world[vs[1]] - world[vs[0]]   # fresh Vector: safe to mutate
            e -= n * e.dot(n)
            if e.length > _ORGANIC_EPS:
                x = e.normalized()
                y = n.cross(x)
    if x is None:
        # V "up": world +Z projected into the piece plane (world +Y as the
        # fallback for pieces facing straight up or down)
        up = Vector((0.0, 0.0, 1.0))
        cand = up - n * up.dot(n)
        if cand.length < 1e-4:
            up = Vector((0.0, 1.0, 0.0))
            cand = up - n * up.dot(n)
        if cand.length < _ORGANIC_EPS:
            return None
        y = cand.normalized()
        x = y.cross(n)                # (x x y)·n == +1  ->  no mirroring

    verts, seen = [], set()
    for f in patch:
        for v in f.verts:
            if v not in seen:
                seen.add(v)
                verts.append(v)
    pts = [world[v] for v in verts]   # read-only view into the shared table
    flat = [(p.dot(x), p.dot(y)) for p in pts]

    if align == 'PCA' and len(flat) > 2:
        cx = sum(p[0] for p in flat) / len(flat)
        cy = sum(p[1] for p in flat) / len(flat)
        sxx = syy = sxy = 0.0
        for px, py in flat:
            dx, dy = px - cx, py - cy
            sxx += dx * dx
            syy += dy * dy
            sxy += dx * dy
        if abs(sxy) > _ORGANIC_EPS or abs(sxx - syy) > _ORGANIC_EPS:
            # principal-axis angle in the (x, y) frame; rotating the FRAME
            # (not the points) keeps the map affine and the handedness safe
            ang = 0.5 * atan2(2.0 * sxy, sxx - syy)
            ca, sa = cos(ang), sin(ang)
            x, y = (x * ca + y * sa), (y * ca - x * sa)
            flat = [(p.dot(x), p.dot(y)) for p in pts]

    min_x = min(p[0] for p in flat)
    max_x = max(p[0] for p in flat)
    min_y = min(p[1] for p in flat)
    max_y = max(p[1] for p in flat)
    w, h = max_x - min_x, max_y - min_y
    out = {}
    rescaled = False
    if fill == 'STRETCH':
        if w < _ORGANIC_EPS or h < _ORGANIC_EPS:
            return None               # a line cannot be stretched onto a square
        for v, (px, py) in zip(verts, flat):
            u = (px - min_x) / w      # NON-UNIFORM stretch fills both axes
            vv = (py - min_y) / h
            out[v] = (0.5 + (u - 0.5) * margin, 0.5 + (vv - 0.5) * margin)
        return out, rescaled

    if w < _ORGANIC_EPS and h < _ORGANIC_EPS:
        return None
    if fill == 'SCALE':
        # absolute: `tile` metres map to the whole square, so the texel size
        # is identical on every piece.  An unusually long piece is shrunk to
        # fit instead of letting UVs leave 0..1 (the integer UV part belongs
        # to the UDIM tools)
        scale = 1.0 / params['tile']
        if max(w, h) * scale > 1.0:
            scale = 1.0 / max(w, h)
            rescaled = True
    else:                             # FIT: uniform, the piece fills the square
        scale = 1.0 / max(w, h)
    cx, cy = (min_x + max_x) * 0.5, (min_y + max_y) * 0.5
    for v, (px, py) in zip(verts, flat):
        u = 0.5 + (px - cx) * scale
        vv = 0.5 + (py - cy) * scale
        out[v] = (0.5 + (u - 0.5) * margin, 0.5 + (vv - 0.5) * margin)
    return out, rescaled


def _organic_apply(obj, bm, faces, plan, params, stats, reselect=False):
    """Cut (when planned), build the pieces and write their UVs.

    `plan` must already come from _organic_plan_cut for THIS object, so the
    line-count guard has fired before any mutation.  Returns the number of
    faces actually unwrapped — 0 means the caller must NOT write the bmesh
    back: a CANCELLED operator pushes no undo step, so committing the cut of
    a run that unwrapped nothing would fuse it into the previous undo entry.

    `reselect` is handed to _organic_cut (Edit Mode + «Выделенные фейсы»).
    """
    mat = obj.matrix_world
    n_cuts = 0
    if plan:
        faces, n_cuts = _organic_cut(bm, mat, faces, params['cell'], plan,
                                     reselect=reselect)
        if n_cuts:
            stats['dirty'] = True     # geometry changed — see _organic_stats
        # bisect leaves the index tables dirty; the patch builder sorts by
        # index for determinism, and normals are read through the area
        # vectors, so only the indices need refreshing
        bm.verts.index_update()
        bm.edges.index_update()
        bm.faces.index_update()
    faces = [f for f in faces if f.is_valid and not f.hide]
    stats['cuts'] += n_cuts
    if not faces:
        return 0
    uv_layer = bm.loops.layers.uv.active
    if uv_layer is None:
        # verify() ADDS the layer, and on a live edit-BMesh that is a real
        # mutation: it outlives Edit Mode whether or not update_edit_mesh is
        # ever called, so it has to raise the dirty flag on its own
        uv_layer = bm.loops.layers.uv.verify()
        stats['dirty'] = True

    # one world transform per vertex for the whole object: the patch builder
    # and every per-piece projection below read this table instead of
    # multiplying `mat` again (same expression, same numbers, ~5x fewer ops)
    world = _organic_world_table(mat, faces)
    patches, _centroids, normals, areas = _organic_build_patches(
        mat, faces, params['cell'], params['cos_half'], world=world)
    patches, merged = _organic_merge_slivers(
        patches, normals, areas, params['cell'], params['merge'],
        params['cos_half'])
    stats['merged'] += merged
    # final enforcement of the cone invariant: the merge phase is allowed to
    # be optimistic (it only checks the sliver's own faces), so anything that
    # still violates the cone is re-split here.  A fold is worse than a
    # sliver, and this pass is what lets the fold counter in the report mean
    # "genuinely folded", not "we did not look".
    checked = []
    for patch in patches:
        axis = _organic_patch_axis(patch, normals)
        if all(_organic_in_cone(normals[f], axis, params['cos_half'])
               for f in patch):
            checked.append(patch)
        else:
            checked.extend(_organic_split_cone(patch, normals,
                                               params['cos_half']))
    patches = checked

    # integer UV part IS the tile number for the UDIM tools — a piece stays
    # in the tile its own loops vote for
    keep_tiles = object_has_udim(obj)
    n_touched = 0
    for patch in patches:
        tile_uv = (0.0, 0.0)
        if keep_tiles:
            uvs = [tuple(loop[uv_layer].uv) for f in patch for loop in f.loops]
            num = _face_tile_number(uvs)
            if num is None:
                # count FACES, like every other skip counter in this report:
                # "12" next to "вырожденных пропущено 400" read as faces and
                # let a user ship 400 untextured polygons believing 12
                stats['out_of_tiles'] += len(patch)
                continue
            # the piece is placed in ONE tile (documented design), but the
            # world voxel grid knows nothing about the UV tile layout, so a
            # piece can straddle a border and drag its minority faces onto a
            # foreign tile's texture.  That used to happen in total silence.
            if any(_face_tile_number([tuple(loop[uv_layer].uv)
                                      for loop in f.loops]) not in (None, num)
                   for f in patch):
                stats['crossed_tiles'] += 1
            tile_uv = _tile_offset(num)
        mapped = _organic_patch_uvs(patch, world, normals, params)
        if mapped is None:
            stats['degenerate'] += len(patch)
            continue
        table, rescaled = mapped
        if rescaled:
            stats['rescaled'] += 1
        for f in patch:
            face_uvs = []
            for loop in f.loops:
                u, v = table[loop.vert]
                loop[uv_layer].uv = (u + tile_uv[0], v + tile_uv[1])
                face_uvs.append((u, v))
            # counted per FAN TRIANGLE, not per face: an n-gon (and the cut
            # makes plenty of them) keeps a consistent whole-face winding
            # while one of its fan triangles is already flipped — and the
            # triangles are what gets rendered, baked and exported.  The
            # piece map is right-handed w.r.t. the piece normal, so negative
            # area == UV overlap inside the piece (report it, never hide it)
            for i in range(1, len(face_uvs) - 1):
                (x0, y0), (x1, y1) = face_uvs[0], face_uvs[i]
                x2, y2 = face_uvs[i + 1]
                if ((x1 - x0) * (y2 - y0)
                        - (x2 - x0) * (y1 - y0)) < _ORGANIC_FOLD_EPS:
                    stats['folds'] += 1
        stats['patches'] += 1
        n_touched += len(patch)

    stats['faces'] += n_touched
    if n_touched:
        stats['dirty'] = True
        _note_uv_overwrite_counts(obj, len(bm.faces), n_touched,
                                  stats['atlas_objects'], stats['udim_objects'])
    return n_touched


def _organic_report(op, stats, params, blocked=(), skipped_no_faces=0,
                    committed=False, linked=(), failed=()):
    """One report for both organic operators.

    Returns True when the operator must end with {'FINISHED'}.

    `committed` says the caller works on the LIVE edit-BMesh, i.e. its
    changes are already in the mesh and cannot be taken back by the
    operator.  Then a run that unwrapped nothing STILL has to finish:
    a {'CANCELLED'} return pushes no undo step, so the cut would be welded
    into the PREVIOUS undo entry and Ctrl+Z could never take it back.
    """
    if stats['faces'] == 0:
        msg = "Развернуть нечего: нет подходящих фейсов"
        # the accumulated skip counters ARE the answer to "why?" — without
        # them a fully parked UDIM object reported only the bare phrase and
        # left the user guessing (reproduced: 500 faces in out_of_tiles)
        reasons = []
        if stats['out_of_tiles']:
            reasons.append(f"вне валидной UDIM-зоны фейсов {stats['out_of_tiles']}")
        if stats['degenerate']:
            reasons.append(f"вырожденных фейсов {stats['degenerate']}")
        if skipped_no_faces:
            reasons.append(f"объектов без фейсов {skipped_no_faces}")
        if stats['shared_matrix_objects']:
            reasons.append("общий меш с разными трансформами: "
                           + ", ".join(stats['shared_matrix_objects']))
        if reasons:
            msg += " — " + ", ".join(reasons)
        if blocked:
            msg += f" (пропущены объекты с shape keys: {', '.join(blocked)})"
        if linked:
            msg += f" (данные из библиотеки: {', '.join(linked)})"
        if failed:
            msg += f" (сбой: {'; '.join(failed)})"
        if committed and stats['dirty']:
            if stats['cuts']:
                msg += f" — но меш уже нарезан ({stats['cuts']} плоскостей)"
            msg += ", отменить можно через Ctrl+Z"
            agr_report(op, 'WARNING', msg)
            return True
        agr_report(op, 'ERROR', msg)
        return False

    msg = (f"✅ Органика: кусков {stats['patches']}, фейсов {stats['faces']}, "
           f"кусок {params['cell']:.3g} м")
    if params['fill'] == 'SCALE':
        msg += ", 1 квадрат = {0:.3g} м".format(params['tile'])
    if stats['cuts']:
        msg += f", плоскостей реза {stats['cuts']}"
    if stats['merged']:
        msg += f", слито мелких {stats['merged']}"
    level = 'INFO'
    if stats['degenerate']:
        # skipped faces keep their OLD UVs — the status line must never be
        # green over them (that is the whole point of counting per face)
        msg += f", вырожденных фейсов пропущено {stats['degenerate']}"
        level = 'WARNING'
    if stats['out_of_tiles']:
        msg += f", вне валидной UDIM-зоны фейсов {stats['out_of_tiles']}"
        level = 'WARNING'
    if stats['rescaled']:
        msg += (f", кусков крупнее ячейки (масштаб уменьшен) "
                f"{stats['rescaled']}")
    if stats['crossed_tiles']:
        msg += (f" | ⚠️ кусков через границу UDIM-тайла {stats['crossed_tiles']} — "
                f"их фейсы съехали в один тайл (уменьшите размер куска)")
        level = 'WARNING'
    if stats['shared_matrix_objects']:
        # the whole pipeline is world-space (voxel keys, cut planes, the +Z
        # projection basis), so one shared mesh can only be cut for ONE
        # transform - the other users get a grid offset by their own delta
        msg += (f" | ⚠️ общий меш с разными трансформами, сетка взята по одному "
                f"объекту: {', '.join(stats['shared_matrix_objects'])}")
        level = 'WARNING'
    if skipped_no_faces:
        msg += f", без фейсов пропущено {skipped_no_faces}"
    if stats['folds']:
        msg += (f" | ⚠️ перевёрнутых треугольников {stats['folds']} — "
                f"уменьшите «Разброс нормалей» или размер куска")
        level = 'WARNING'
    if blocked:
        msg += (f" | ⚠️ пропущены объекты с shape keys (резать нельзя): "
                f"{', '.join(blocked)}")
        level = 'WARNING'
    if linked:
        msg += (f" | ⚠️ пропущены данные из библиотеки (только чтение): "
                f"{', '.join(linked)}")
        level = 'WARNING'
    if failed:
        msg += f" | ❌ сбой на объектах: {'; '.join(failed)}"
        level = 'WARNING'
    if stats['modifier_objects']:
        msg += (f" | развёртка по базовому мешу, модификаторы не применены: "
                f"{', '.join(stats['modifier_objects'])}")
    if stats['atlas_objects']:
        msg += f" | ⚠️ перезаписаны UV атласа: {', '.join(stats['atlas_objects'])}"
        level = 'WARNING'
    if stats['udim_objects']:
        msg += (f" | ⚠️ перезаписана развёртка UDIM-объектов: "
                f"{', '.join(stats['udim_objects'])}")
        level = 'WARNING'
    # said out loud on EVERY run: overlapping UVs are legal here (that is the
    # point — one tile per piece), but they are inside 0..1, so the atlas and
    # bake guards cannot see them and would happily produce garbage
    msg += " | куски лежат внахлёст: для запекания и атласа не годится"
    agr_report(op, level, msg)
    return True


def _organic_cap_error(op, over):
    agr_report(op, 'ERROR',
               "Слишком мелкий кусок для: " + ", ".join(over) +
               f" (> {_ORGANIC_MAX_CUT_LINES} плоскостей реза) — увеличьте "
               "размер куска: рез каждой плоскостью идёт по всей геометрии, "
               "и время растёт быстрее их числа")


# ============================================================
# Core: unwrap
# ============================================================

def _cut_committed_warn(op, reason):
    """The cut is already in the LIVE edit-BMesh and the unwrap refused.

    Returning {'CANCELLED'} now would push no undo step and weld the cut
    into the PREVIOUS undo entry, out of Ctrl+Z's reach — the very rule the
    organic path states as `_organic_report(committed=True)`.  So the
    operator finishes and says what happened instead.
    """
    agr_report(op, 'WARNING',
               f"Меш нарезан, развёртка не выполнена: {reason}; "
               "отменить можно через Ctrl+Z")
    return True


def _do_unwrap(op, context, settings, basis=None, committed=False):
    """Map every target face into the 0..1 square of its grid cell.

    `basis` — resolve the grid ONCE per user action: "Разрезать и
    развернуть" hands over the basis the CUT used.  Re-resolving it here
    would read a different input, because the bisect drops the live face
    selection and `_orientation_targets` then falls back to the whole mesh
    (a building's roof drags the mean normal vertical and the wall grid
    turns into a floor grid).  The SURFACE frames are still rebuilt from
    the new topology — phase anchoring makes them reproducible.

    `committed` — the caller has already mutated the live edit-BMesh, so a
    refusal must finish with a WARNING (see `_cut_committed_warn`).
    """
    targets = _collect_targets(context, settings)
    if not targets:
        if committed:
            return _cut_committed_warn(op, "не осталось подходящих фейсов")
        _report_no_targets(op, settings)
        return False
    if basis is None:
        basis = _resolve_basis(op, settings, targets, quiet=committed)
        if basis is None:
            if committed:
                return _cut_committed_warn(op, "сетка не определена")
            return False
    origin, x_dir, y_dir, cell_u, cell_v = basis
    tol = settings.snap_tolerance
    surface = _surface_mode(settings)
    frames = _make_frames(settings, targets, basis)
    # plan view: watch for faces the projection collapses (see _TOPZ_FLAT_EPS)
    plan_normal = x_dir.cross(y_dir) if settings.grid_source == 'TOPZ' else None

    total = 0
    oversize = 0
    vertical = 0
    atlas_objects = []
    udim_objects = []
    failed = []
    for obj, bm, faces in targets:
        # per-object isolation, same as the organic path: this is the LIVE
        # edit-BMesh, so once ANY object has been written the operator must
        # still finish — an uncaught exception on the second object of a
        # multi-object Edit Mode skips FINISHED, pushes no undo step and
        # welds the first object's writes into the PREVIOUS undo entry
        try:
            n, n_over, n_vert = _unwrap_object(
                op, obj, bm, faces, basis, frames, surface, plan_normal,
                tol, atlas_objects, udim_objects)
            total += n
            oversize += n_over
            vertical += n_vert
        except ReferenceError:
            raise   # the mixin turns a stale BMesh into a friendly report
        except Exception as exc:
            failed.append(f"{obj.name}: {exc}")

    msg = f"Развёрнуто фейсов: {total} (ячейка {cell_u:.3g} × {cell_v:.3g} м)"
    if udim_objects:
        msg += f"; ВНИМАНИЕ: раскладка UDIM-тайлов потеряна у: {', '.join(udim_objects)}"
    if atlas_objects:
        msg += f"; перезаписаны UV атласа: {', '.join(atlas_objects)}"
    if oversize:
        msg += (f"; {oversize} фейс(ов) больше одной ячейки — "
                "примените «Разрезать по сетке»")
    if vertical:
        msg += (f"; {vertical} фейс(ов) почти вертикальны — вид сверху "
                "вырождает их UV (для стен нужна мировая сетка)")
    if failed:
        msg += f"; ❌ сбой на объектах: {'; '.join(failed)}"
    if udim_objects or atlas_objects or oversize or vertical or failed:
        agr_report(op, 'WARNING', msg)
    else:
        agr_report(op, 'INFO', msg)
    return True


def _unwrap_object(op, obj, bm, faces, basis, frames, surface, plan_normal,
                   tol, atlas_objects, udim_objects):
    """Write the grid UVs of ONE object -> (faces, oversize, vertical)."""
    origin, x_dir, y_dir, cell_u, cell_v = basis
    op._mutated = True   # live edit-BMesh: writes start here (see mixin)
    mat = obj.matrix_world
    _note_uv_overwrite(obj, bm, faces, atlas_objects, udim_objects)
    uv_layer = bm.loops.layers.uv.verify()
    oversize = 0
    vertical = 0

    # STRICT position-faithful mapping (user's final choice — the smart
    # placement tiers were each tried and rejected): UV = grid coordinate
    # minus the cell index of the face center, per-vert snap on top.
    # Pieces of one cell assemble the tile at their true places; anything
    # misaligned pokes out of 0..1 honestly and the oversize counter
    # suggests cutting.
    for f in faces:
        pts = [mat @ v.co for v in f.verts]
        if plan_normal is not None:
            fn = Vector((0.0, 0.0, 0.0))
            for i in range(1, len(pts) - 1):
                fn += (pts[i] - pts[0]).cross(pts[i + 1] - pts[0])
            if (fn.length > 1e-12
                    and abs(fn.dot(plan_normal)) < _TOPZ_FLAT_EPS * fn.length):
                vertical += 1
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
    bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
    return len(faces), oversize, vertical


# ============================================================
# Core: cut along grid lines
# ============================================================

def _stamp_selection(context, settings):
    """Carry the live face selection across the bisect in «Весь меш» mode.

    There the selection is not the target list — it is the grid ORIENTATION
    gesture (`_orientation_targets`), and `bisect_plane` destroys it: new
    faces come out unselected and in VERT/EDGE select mode the flush drops
    the parent face too, so a follow-up unwrap resolved a different grid
    (a wall turned into a floor on a building).  A face int layer is the
    only carrier that survives: measured on a 4-way bisect the layer value
    reaches all 4 pieces, `f.tag` only 2 and `f.select` none.

    Runs BEFORE `_collect_targets` on purpose — adding a custom data layer
    invalidates every existing Python reference to the BMesh elements.
    Returns the objects that carry the scratch layer.
    """
    if settings.selection_mode != 'ALL':
        return []
    stamped = []
    for obj in _edit_mesh_objects(context):
        bm = bmesh.from_edit_mesh(obj.data)
        lay = bm.faces.layers.int.get(_SEL_KEEP_LAYER)
        if lay is None:
            lay = bm.faces.layers.int.new(_SEL_KEEP_LAYER)
        for f in bm.faces:
            f[lay] = 1 if f.select else 0
        stamped.append(obj)
    return stamped


def _restore_selection(obj, bm, restore=True):
    """Re-select the stamped faces and drop the scratch layer.

    The scratch layer must never reach the mesh — the delivery checker
    forbids stray attributes — so it is dropped even when the cut threw.
    """
    lay = bm.faces.layers.int.get(_SEL_KEEP_LAYER)
    if lay is None:
        return
    if restore:
        for f in bm.faces:
            if f[lay]:
                f.select = True   # flushes DOWN only, never select_flush up
    bm.faces.layers.int.remove(lay)


def _do_cut(op, context, settings):
    """Bisect the target faces along the grid lines.

    Returns the resolved basis (never None on success) so the caller can
    hand THE SAME grid to the follow-up unwrap; None means nothing was cut
    and the operator must cancel.
    """
    stamped = _stamp_selection(context, settings)
    try:
        return _do_cut_inner(op, context, settings, stamped)
    finally:
        # objects that were skipped or threw still carry the scratch layer
        for obj in stamped:
            try:
                _restore_selection(obj, bmesh.from_edit_mesh(obj.data))
            except Exception:
                pass


def _do_cut_inner(op, context, settings, stamped):
    targets = _collect_targets(context, settings)
    if not targets:
        _report_no_targets(op, settings)
        return None
    basis = _resolve_basis(op, settings, targets)
    if basis is None:
        return None
    origin, x_dir, y_dir, cell_u, cell_v = basis
    surface = _surface_mode(settings)
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
                   f" (лимит {_MAX_CUT_LINES}) — проверьте размер ячейки: "
                   "рез каждой линией идёт по всей геометрии, и время "
                   "растёт быстрее числа линий")
        return None

    total_cuts = 0
    total_new = 0
    failed = []
    for obj, bm, faces, u_plan, min_u, max_u, min_v, max_v in plans:
        # per-object isolation, same as the organic path: the bisects below
        # go into the LIVE edit-BMesh, so an uncaught exception on the
        # second object of a multi-object Edit Mode would skip FINISHED,
        # push no undo step and weld the first object's cut into the
        # PREVIOUS undo entry
        try:
            n_cuts, n_new = _cut_object(op, settings, obj, bm, faces, u_plan,
                                        min_u, max_u, min_v, max_v,
                                        basis, surface)
            total_cuts += n_cuts
            total_new += n_new
        except ReferenceError:
            raise   # the mixin turns a stale BMesh into a friendly report
        except Exception as exc:
            failed.append(f"{obj.name}: {exc}")

    msg = f"Разрезов: {total_cuts}, новых фейсов: +{total_new}"
    if failed:
        agr_report(op, 'WARNING',
                   msg + f"; ❌ сбой на объектах: {'; '.join(failed)}")
    else:
        agr_report(op, 'INFO', msg)
    return basis


def _cut_object(op, settings, obj, bm, faces, u_plan, min_u, max_u,
                min_v, max_v, basis, surface):
    """Bisect ONE object along the grid lines -> (cuts, new faces)."""
    origin, x_dir, y_dir, cell_u, cell_v = basis
    op._mutated = True   # live edit-BMesh: bisects start here (see mixin)
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

    try:
        total_cuts = 0
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
        total_new = len(new_faces) - before
    finally:
        # restore the «Весь меш» orientation gesture and drop the scratch
        # layer (removing it invalidates the element refs above, so it must
        # be the LAST thing done with this BMesh)
        _restore_selection(obj, bm)
    bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=True)
    return total_cuts, total_new


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


# Verdict cache for "too many faces to preview": the build already refused
# for THIS cheap key, so the fingerprint must not pay an O(faces) selection
# scan just to produce the same refusal string again (measured: 82 ms per
# 0.15 s tick on 490k faces — over half the tick budget spent on a preview
# that draws nothing).  The key deliberately holds no selection data.
_uv_overlay_refused = {"key": None}


def _uv_overlay_cheap_key(context, settings):
    """Key for the refusal cache: object identity + face/vert counts, plus the
    O(1) selected-face count in SELECTED mode.

    `_uv_geo_version` is in it so that hiding faces (which changes the ALL
    target count without changing len(bm.faces)) re-opens the question.

    In SELECTED mode the target count IS the selected-face count, so without
    `total_face_sel` the refusal stuck: select-all on a city mesh refused, and
    narrowing the selection to ten wall faces kept the same key (a pure
    selection change does not bump `_uv_geo_version` — the depsgraph reports
    ID_RECALC_SELECT, not GEOMETRY), so the preview stayed dead until the
    geometry was edited.  `mesh.total_face_sel` is O(1) and live in Edit Mode;
    in ALL mode it is deliberately left out so the cache ignores selection.
    """
    key = [settings.selection_mode, _uv_geo_version]
    for obj in _edit_mesh_objects(context):
        bm = bmesh.from_edit_mesh(obj.data)
        key.append((obj.name, len(bm.verts), len(bm.faces),
                    obj.data.total_face_sel if settings.selection_mode == 'SELECTED' else -1))
    return tuple(key)


def _uv_overlay_fingerprint(context, settings):
    """Cheap state hash: recompute the preview only when this changes."""
    cheap = _uv_overlay_cheap_key(context, settings)
    if _uv_overlay_refused["key"] == cheap:
        return ("refused", cheap)
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
        # the live selection even in «Весь меш» mode.
        # `mesh.total_face_sel` is O(1) and LIVE in Edit Mode (verified on
        # 5.2), while mesh.polygons.foreach_get("select") returns the
        # PRE-edit snapshot there — so numpy is not an option and the two
        # degenerate cases (nothing / everything selected) are short-cut
        # instead: their hash is fully determined by the counts.
        n_faces = len(bm.faces)
        sel_count = obj.data.total_face_sel
        sel_hash = 0
        if 0 < sel_count < n_faces:
            sel_count = 0
            for f in bm.faces:
                if f.select:
                    sel_count += 1
                    sel_hash = (sel_hash * 31 + f.index) & 0x7FFFFFFF
        fp.append((obj.name, len(bm.verts), n_faces, sel_count, sel_hash,
                   tuple(round(v, 5) for row in obj.matrix_world for v in row)))
    return tuple(fp)


def _uv_overlay_build(context, settings):
    """Preview geometry: cut segments on the faces + lattice + U/V axes."""
    targets = _collect_targets(context, settings)
    if not targets:
        return None
    total_faces = sum(len(faces) for _o, _b, faces in targets)
    if total_faces > _OVERLAY_MAX_FACES:
        # remember the refusal so the next ticks skip the selection scan
        _uv_overlay_refused["key"] = _uv_overlay_cheap_key(context, settings)
        return {"error": f"слишком много фейсов для превью ({total_faces})"}
    _uv_overlay_refused["key"] = None
    basis = _resolve_basis(None, settings, targets, quiet=True)
    if basis is None:
        return {"error": "сетка не определена (см. настройки)"}
    origin, x_dir, y_dir, cell_u, cell_v = basis
    surface = _surface_mode(settings)
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
    elif centroid_n and gmin_u is not None \
            and (normal_sum.length > 1e-9 or settings.grid_source == 'TOPZ'):
        # TOPZ needs no mean normal at all — its plane is horizontal by
        # definition — so it must NOT be gated on one.  The area vectors of a
        # closed mesh (a cube, a building shell) cancel exactly, and that
        # used to suppress the entire plan-view preview: lattice, cell
        # outline and axis arrows, leaving only the orange cut segments.
        centroid /= centroid_n
        if settings.grid_source == 'TOPZ':
            # plan view: the lattice lives in a HORIZONTAL plane through the
            # selection (following the face normal would park a top-down
            # grid beside a wall instead of cutting through it)
            mean_n = Vector((0.0, 0.0, 1.0))
        else:
            mean_n = normal_sum.normalized()
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


_UV_DRAW_HANDLE_KEY = "agr_uv_overlay_handle"   # survives a module reload


def _uv_drop_stale_handlers(seq, name):
    """Remove handlers by __name__, not identity: a dev reload builds NEW
    function objects, so the identity check never matches the old module's
    handler and it stays registered forever (same fix as operators_link)."""
    for h in [h for h in seq if getattr(h, "__name__", "") == name]:
        try:
            seq.remove(h)
        except ValueError:
            pass


def _uv_add_handlers():
    global _uv_handle_3d, _uv_draw_error_logged
    _uv_draw_error_logged = False
    _uv_overlay_cache["fp"] = None
    _uv_overlay_cache["next_check"] = 0.0
    _uv_overlay_refused["key"] = None
    if _uv_handle_3d is None:
        # a draw handler cannot be enumerated, so the previous module's
        # handle is parked in the driver namespace, which reload survives
        _uv_remove_stale_draw_handler()
        _uv_handle_3d = bpy.types.SpaceView3D.draw_handler_add(
            _uv_draw_overlay_3d, (), 'WINDOW', 'POST_VIEW')
        bpy.app.driver_namespace[_UV_DRAW_HANDLE_KEY] = _uv_handle_3d
    _uv_drop_stale_handlers(bpy.app.handlers.depsgraph_update_post,
                            "_uv_overlay_depsgraph")
    bpy.app.handlers.depsgraph_update_post.append(_uv_overlay_depsgraph)


def _uv_remove_stale_draw_handler():
    stale = bpy.app.driver_namespace.pop(_UV_DRAW_HANDLE_KEY, None)
    if stale is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(stale, 'WINDOW')
        except Exception:
            pass


def _uv_remove_handlers():
    global _uv_handle_3d, _uv_last_stats
    if _uv_handle_3d is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_uv_handle_3d, 'WINDOW')
        except Exception:
            pass
        _uv_handle_3d = None
        bpy.app.driver_namespace.pop(_UV_DRAW_HANDLE_KEY, None)
    else:
        _uv_remove_stale_draw_handler()
    _uv_drop_stale_handlers(bpy.app.handlers.depsgraph_update_post,
                            "_uv_overlay_depsgraph")
    _uv_overlay_cache["fp"] = None
    _uv_overlay_cache["data"] = None
    _uv_overlay_refused["key"] = None
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
        # crash into a friendly "run it again" instead of a traceback.
        # `_mutated` is raised by the worker functions at their FIRST write
        # into the live edit-BMesh: after that point {'CANCELLED'} would
        # push no undo step and weld those writes into the PREVIOUS undo
        # entry, so a late ReferenceError must finish instead.
        self._mutated = False
        try:
            return self._execute(context)
        except ReferenceError:
            if getattr(self, "_mutated", False):
                agr_report(self, 'WARNING',
                           "Данные меша устарели посреди операции — часть "
                           "изменений уже в меше, отменить их можно через Ctrl+Z")
                return {'FINISHED'}
            agr_report(self, 'ERROR',
                       "Данные меша устарели (например, после Undo) — "
                       "запустите оператор ещё раз")
            return {'CANCELLED'}


class AGR_OT_UVGridCapture(_AGR_UVGridPollMixin, Operator):
    """Store the reference grid from two selected edges of one grid cell"""
    bl_idname = "agr.uv_grid_capture"
    bl_label = "Запомнить сетку"
    bl_description = ("Запомнить опорную сетку. 2 ребра одной ячейки — точный "
                      "захват (оси U/V, размер ячейки, начало); несколько рёбер — "
                      "автофит: медианные длины двух перпендикулярных семейств, "
                      "диагонали и обрезки отсеиваются. Мировые координаты, "
                      "работает для всех объектов сцены")
    # no 'UNDO': only scene properties are written, and the poll restricts
    # this to Edit Mode, where an undo push produces a MESH step that cannot
    # carry them — Ctrl+Z would silently do nothing and then eat the
    # previous real edit (an empty push also costs ~50 ms on city-scale
    # meshes).  Same reasoning as the angle/offset pipettes below.
    bl_options = {'REGISTER'}

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
    # no 'UNDO' — same reasoning as the capture above: this button lives in
    # the Edit-Mode grid workflow, where the pushed mesh step cannot restore
    # the scene-level flag it flips
    bl_options = {'REGISTER'}

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


class AGR_OT_UVGridAngleFromEdge(_AGR_UVGridPollMixin, Operator):
    """Set the grid rotation so U runs along the selected edge"""
    bl_idname = "agr.uv_grid_angle_from_edge"
    bl_label = "Поворот по ребру"
    bl_description = ("Повернуть мировую сетку / сетку «Сверху» по выделенному "
                      "ребру: ось U ложится вдоль ребра (в проекции на плоскость "
                      "сетки). Выделите ровно одно ребро — лежащее в плоскости "
                      "нужной поверхности, не на изломе")
    # no 'UNDO': the pipette only writes scene properties, and in Edit Mode
    # an undo push produces a MESH step that cannot carry them — the user's
    # Ctrl+Z would silently do nothing and then eat their real edit (review
    # finding; ~50 ms per push on city-scale meshes for an empty step)
    bl_options = {'REGISTER'}

    def _execute(self, context):
        settings = _get_settings(context)
        if settings is None:
            return {'CANCELLED'}
        if settings.grid_source == 'EDGES':
            agr_report(self, 'ERROR',
                       "Поворот по ребру работает для мировой сетки и «Сверху» — "
                       "сетка по рёбрам берёт оси из «Запомнить сетку»")
            return {'CANCELLED'}
        picked = _pick_single_edge(context)
        if picked is None:
            agr_report(self, 'ERROR',
                       "Выделите ровно одно ребро — оно задаст направление сетки")
            return {'CANCELLED'}
        a, b, face_vecs = picked

        if settings.grid_source == 'TOPZ':
            # plan view: the rotation plane is horizontal by definition
            axis = Vector((0.0, 0.0, 1.0))
            base_y = Vector((0.0, 1.0, 0.0))
        else:
            # WORLD: the edge's OWN surface defines the frame — the same
            # normal the unwrap will get from the selected wall faces.  A
            # crease edge (wall∩roof/ground) averages two unrelated planes
            # and the stored angle would be read in a different frame later
            # (review repro: silent 45–90° diagonal grid) — refuse instead.
            edge_n = Vector((0.0, 0.0, 0.0))
            for v in face_vecs:
                edge_n += v
            if edge_n.length < 1e-6:
                agr_report(self, 'ERROR',
                           "Ребро не принадлежит ни одному фейсу — нормаль "
                           "поверхности не определить")
                return {'CANCELLED'}
            if not _coplanar_normals(face_vecs):
                agr_report(self, 'ERROR',
                           "Ребро лежит на изломе (нормали его фейсов "
                           "расходятся) — кликните ребро в плоскости той "
                           "поверхности, которую будете разворачивать")
                return {'CANCELLED'}
            axis = edge_n.normalized()
            _base_x, base_y = _world_base_axes(axis)

        edge_dir = b - a
        proj = edge_dir - axis * edge_dir.dot(axis)
        if edge_dir.length < 1e-9 or proj.length < edge_dir.length * 1e-3:
            agr_report(self, 'ERROR',
                       "Ребро перпендикулярно плоскости сетки — направление "
                       "по нему не определить")
            return {'CANCELLED'}

        # Solve edge·y_dir(θ) = 0 — V constant along the edge, so the grid
        # LINES follow it.  For the wall/TOPZ frames (base axes ⟂ axis)
        # this equals the naive "rotate base_x onto the edge" mod 180°, but
        # on the floor branch the world X/Y base is NOT ⟂ a tilted normal
        # and the naive atan2 left U up to 19.5° off the edge (review
        # repro: hip roof of a rotated building).  With p = proj (p·axis=0)
        # the Rodrigues expansion gives p·R(θ)·base_y = A·cosθ + B·sinθ,
        # A = p·base_y, B = p·(axis×base_y)  →  θ = atan2(−A, B).
        angle = atan2(-proj.dot(base_y), proj.dot(axis.cross(base_y)))
        # an edge has no direction: reduce mod 180° to the representative
        # closest to zero — both solutions zero the projected edge·y_dir
        while angle > pi / 2.0:
            angle -= pi
        while angle <= -pi / 2.0:
            angle += pi
        settings.world_angle = angle
        _tag_redraw_view3d()
        agr_report(self, 'INFO', f"Поворот сетки по ребру: {degrees(angle):.2f}°")
        return {'FINISHED'}


class AGR_OT_UVGridOffsetFromPoint(_AGR_UVGridPollMixin, Operator):
    """Shift the grid so a lattice intersection lands at the selected vertex"""
    bl_idname = "agr.uv_grid_offset_from_point"
    bl_label = "Сдвиг к точке"
    bl_description = ("Сдвинуть сетку к выделенной вершине: пересечение линий "
                      "сетки (угол ячеек) попадает точно в неё. Работает для "
                      "всех источников сетки. Выделите ровно одну вершину — "
                      "в плоскости нужной поверхности, не на углу/изломе")
    # no 'UNDO' — same reasoning as the angle pipette above
    bl_options = {'REGISTER'}

    def _execute(self, context):
        settings = _get_settings(context)
        if settings is None:
            return {'CANCELLED'}
        if settings.grid_source == 'EDGES' and not settings.has_grid:
            agr_report(self, 'ERROR', "Сетка не задана — сначала запомните её")
            return {'CANCELLED'}
        picked = _pick_single_vert(context)
        if picked is None:
            agr_report(self, 'ERROR',
                       "Выделите ровно одну вершину — в неё встанет "
                       "пересечение линий сетки")
            return {'CANCELLED'}
        point, vert_targets, face_vecs = picked

        targets = _collect_targets(context, settings)
        basis_targets = targets
        if settings.grid_source == 'WORLD':
            # A selected face flushes all of its verts, so a single selected
            # vertex NEVER comes with a face selection — the vertex's own
            # link faces are the only reachable normal source (and the mean
            # over «Весь меш» is zero on closed volumes).  A corner vertex
            # (facade∩ground) averages unrelated planes and the offsets
            # would be measured along axes the unwrap will never use
            # (review repro: 0.3 м silently off) — refuse it.
            if not vert_targets:
                agr_report(self, 'ERROR',
                           "Вершина не принадлежит ни одному фейсу — нормаль "
                           "поверхности не определить")
                return {'CANCELLED'}
            if not _coplanar_normals(face_vecs):
                agr_report(self, 'ERROR',
                           "Вершина на углу/изломе (нормали её фейсов "
                           "расходятся) — кликните вершину в плоскости той "
                           "поверхности, которую будете разворачивать, или "
                           "используйте источник «Сверху»")
                return {'CANCELLED'}
            basis_targets = vert_targets
        basis = _resolve_basis(self, settings, basis_targets)
        if basis is None:
            return {'CANCELLED'}
        origin, x_dir, y_dir, cell_u, cell_v = basis
        if cell_u < 1e-9 or cell_v < 1e-9:
            agr_report(self, 'ERROR', "Размер ячейки нулевой")
            return {'CANCELLED'}

        # fractional residual to the NEAREST lattice corner, in meters; the
        # resolved basis already carries the current offsets, so this is a
        # pure correction on top of them.  V is planar under every
        # projection; U under SURFACE is the anchored arc coordinate, so
        # its residual must come from the SAME frames the unwrap will use
        # (U depends on offset_u with slope −1 — one step converges).
        d = point - origin
        du = None
        if _surface_mode(settings):
            frames = _make_frames(settings, targets, basis) if targets else None
            frame = None
            if frames:
                for _obj, _bm, link in vert_targets:
                    frame = next((frames[f] for f in link if f in frames), None)
                    if frame is not None:
                        break
            if frame is not None:
                fh, fanchor, fu0 = frame
                gu = (fu0 + (point - fanchor).dot(fh)) / cell_u
                du = (gu - round(gu)) * cell_u
        else:
            gu = d.dot(x_dir) / cell_u
            du = (gu - round(gu)) * cell_u
        gv = d.dot(y_dir) / cell_v
        dv = (gv - round(gv)) * cell_v

        if du is not None:
            new_u = settings.offset_u + du
            # keep the stored offsets small: the lattice repeats every cell
            new_u -= round(new_u / cell_u) * cell_u
            settings.offset_u = new_u
        new_v = settings.offset_v + dv
        new_v -= round(new_v / cell_v) * cell_v
        settings.offset_v = new_v
        _tag_redraw_view3d()

        # one accumulated report (agr_report overwrites the status line, so
        # separate calls could not all be shown) — numbers ALWAYS included
        u_txt = f"{du:+.3f} м" if du is not None else "не скорректирован"
        msg = f"Сетка сдвинута к точке: поправка U {u_txt}, V {dv:+.3f} м"
        caveats = []
        if du is None:
            caveats.append("U по дуге считается по обрабатываемым фейсам — "
                           "выделите фейсы с этой вершиной (или режим «Весь "
                           "меш») и повторите")
        if settings.grid_source == 'WORLD' and settings.origin_mode == 'SELECTION':
            caveats.append("начало «Угол выделения» плывёт вместе с выделением "
                           "— для постоянной привязки переключите начало на "
                           "«Начало мира»")
        if caveats:
            agr_report(self, 'WARNING', msg + "; ВНИМАНИЕ: " + "; ".join(caveats))
        else:
            agr_report(self, 'INFO', msg)
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
        if _do_cut(self, context, settings) is None:
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
        basis = _do_cut(self, context, settings)
        if basis is None:
            return {'CANCELLED'}
        # ONE grid per user action: the cut resolved it from the live face
        # selection (the documented orientation gesture) and its own bisect
        # then destroyed that selection — re-resolving here would silently
        # unwrap against a different grid.  committed=True: whatever happens
        # now, the cut is already in the mesh and the operator must FINISH.
        _do_unwrap(self, context, settings, basis=basis, committed=True)
        return {'FINISHED'}


# ============================================================
# Panel
# ============================================================

class AGR_OT_UVUnwrapStub(Operator):
    """Unwrap every polygon of stub (small-texture) materials and tiles"""
    bl_idname = "agr.uv_unwrap_stub"
    bl_label = "Развернуть заглушки"
    bl_description = ("Найти материалы и UDIM-тайлы, чья наибольшая текстура "
                      "не превышает порог (заглушки), и развернуть ВСЕ их "
                      "полигоны: каждый растягивается на весь UV-квадрат "
                      "своего тайла внахлёст, с отступом от краёв")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if context.mode != 'OBJECT':
            cls.poll_message_set("Работает в объектном режиме")
            return False
        if any(o.type == 'MESH' for o in context.selected_objects):
            return True
        if context.active_object is not None and context.active_object.type == 'MESH':
            return True
        cls.poll_message_set("Выберите MESH-объекты")
        return False

    def execute(self, context):
        settings = _get_settings(context)
        threshold = settings.stub_threshold if settings else 256
        margin = settings.stub_margin if settings else 0.9

        objs = [o for o in context.selected_objects if o.type == 'MESH']
        if not objs and context.active_object is not None \
                and context.active_object.type == 'MESH':
            objs = [context.active_object]
        # one pass per MESH datablock: linked duplicates share the UV layer,
        # a second pass would double-count and (with OBJECT-linked slot
        # overrides) flatten another object's faces
        seen_data = set()
        unique_objs = []
        for o in objs:
            if o.data in seen_data:
                continue
            seen_data.add(o.data)
            unique_objs.append(o)

        stats = {'faces': 0, 'materials': 0, 'tiles': 0,
                 'degenerate': 0, 'out_of_tiles': 0, 'override_slots': 0,
                 'found_objects': 0, 'uv_created': [],
                 'atlas_objects': [], 'udim_objects': []}
        for obj in unique_objs:
            self._process_object(obj, threshold, margin, stats)

        if stats['faces'] == 0:
            # the skipped-slot counters belong in BOTH branches: with every
            # slot an OBJECT override the user used to be told "заглушки не
            # найдены" while the operator had found them and skipped them
            tail = ""
            if stats['override_slots']:
                tail = (f" (пропущено OBJECT-слотов: {stats['override_slots']}"
                        f" — оверрайд объекта не правит общий меш)")
            elif stats['found_objects']:
                tail = f" (объектов со стаб-материалами: {stats['found_objects']})"
            if stats['found_objects']:
                agr_report(self, 'WARNING',
                           "⚠️ Стаб-материалы найдены, но ни один фейс не "
                           "затронут (фейсы на других слотах/тайлах)" + tail)
            else:
                agr_report(self, 'WARNING',
                           f"⚠️ Заглушки (≤{threshold}px) не найдены в материалах "
                           f"выбранных объектов" + tail)
            if stats['uv_created']:
                # a created UV layer is a mesh mutation: {'CANCELLED'} would
                # push no undo step and leave it behind for good
                agr_report(self, 'WARNING',
                           "⚠️ Развернуть нечего, но UV-слой был создан у: "
                           + ", ".join(stats['uv_created'])
                           + " — отменить можно через Ctrl+Z")
                return {'FINISHED'}
            return {'CANCELLED'}

        msg = (f"✅ Stub-развёртка: фейсов {stats['faces']}, "
               f"материалов {stats['materials']}, тайлов {stats['tiles']}")
        level = 'INFO'
        if stats['degenerate']:
            msg += f", вырожденных пропущено {stats['degenerate']}"
        if stats['out_of_tiles']:
            msg += f", вне известных тайлов {stats['out_of_tiles']}"
        if stats['override_slots']:
            msg += (f", пропущено OBJECT-слотов {stats['override_slots']} "
                    f"(меш общий, оверрайд объекта не правит всех)")
        if stats['atlas_objects']:
            msg += f" | ⚠️ перезаписаны UV атласа: {', '.join(stats['atlas_objects'])}"
            level = 'WARNING'
        if stats['udim_objects']:
            msg += (f" | ⚠️ перезаписана развёртка UDIM-объектов: "
                    f"{', '.join(stats['udim_objects'])}")
            level = 'WARNING'
        agr_report(self, level, msg)
        return {'FINISHED'}

    @staticmethod
    def _process_object(obj, threshold, margin, stats):
        mesh = obj.data
        stub_fixed_slots = set()
        tile_maps = {}  # slot index -> (all tiles, stub tiles)
        for idx, slot in enumerate(obj.material_slots):
            if slot.link == 'OBJECT':
                # the UV write goes into the SHARED mesh; a per-object
                # material override must not reshape every user of it
                if material_texture_entry(slot.material) is not None:
                    stats['override_slots'] += 1
                continue
            entry = material_texture_entry(slot.material)
            if entry is None:
                continue  # no textures (or unreadable size) -> not a stub
            kind, res = entry
            if kind == 'fixed':
                if res <= threshold:
                    stub_fixed_slots.add(idx)
            else:
                stub_tiles = {num: side for num, side in res.items()
                              if side <= threshold}
                if stub_tiles:
                    tile_maps[idx] = (res, stub_tiles)
        if not stub_fixed_slots and not tile_maps:
            return
        stats['found_objects'] += 1

        # the UV layer is created LAZILY, right before the first real write:
        # creating it up front and then returning {'CANCELLED'} left a stray
        # layer in the mesh outside any undo step, against the "1 UV per
        # object" delivery rule
        uv_layer = mesh.uv_layers.active
        uv_data = uv_layer.data if uv_layer is not None else None
        mat_world = obj.matrix_world
        # a fixed-stub slot on a UDIM object must keep each face in its
        # OWN tile - flattening into 0..1 would silently re-texture it
        keep_tiles = object_has_udim(obj)

        def _poly_uvs(poly):
            # no UV layer yet -> every loop reads as (0, 0), i.e. tile 1001,
            # exactly what the freshly created layer used to give
            if uv_data is None:
                return [(0.0, 0.0)] * poly.loop_total
            return [tuple(uv_data[li].uv) for li in poly.loop_indices]

        n_touched = 0
        touched_slots = set()
        touched_tiles = set()
        for poly in mesh.polygons:
            idx = poly.material_index
            tile_uv = (0.0, 0.0)
            if idx in stub_fixed_slots:
                if keep_tiles:
                    num = _face_tile_number(_poly_uvs(poly))
                    if num is None:
                        stats['out_of_tiles'] += 1
                        continue
                    tile_uv = _tile_offset(num)
            elif idx in tile_maps:
                all_tiles, stub_tiles = tile_maps[idx]
                num = _face_tile_number(_poly_uvs(poly))
                if num not in stub_tiles:
                    if num not in all_tiles:
                        stats['out_of_tiles'] += 1
                    continue
                tile_uv = _tile_offset(num)
            else:
                continue
            # WORLD space: a mirrored instance (negative matrix determinant)
            # turns the right-handed local basis into a left-handed world
            # one, i.e. a mirrored unwrap — the docstring promises otherwise
            pts = [mat_world @ mesh.vertices[mesh.loops[li].vertex_index].co
                   for li in poly.loop_indices]
            new_uvs = _stub_face_uvs(pts, _world_normal(pts), tile_uv, margin)
            if new_uvs is None:
                stats['degenerate'] += 1
                continue
            if uv_data is None:
                uv_data = mesh.uv_layers.new(name="UVMap").data
                stats['uv_created'].append(obj.name)
            for li, uv in zip(poly.loop_indices, new_uvs):
                uv_data[li].uv = uv
            n_touched += 1
            if idx in stub_fixed_slots:
                touched_slots.add(idx)
            else:
                touched_tiles.add(num)

        if n_touched:
            stats['materials'] += len(touched_slots)
            stats['tiles'] += len(touched_tiles)
            stats['faces'] += n_touched
            _note_uv_overwrite_counts(obj, len(mesh.polygons), n_touched,
                                      stats['atlas_objects'],
                                      stats['udim_objects'])
            mesh.update()


class AGR_OT_UVUnwrapStubSelected(_AGR_UVGridPollMixin, Operator):
    """Unwrap the SELECTED polygons into overlapping unit squares"""
    bl_idname = "agr.uv_unwrap_stub_selected"
    bl_label = "Развернуть выделенные фейсы"
    bl_description = ("Развернуть ВЫДЕЛЕННЫЕ полигоны внахлёст: каждый "
                      "растягивается на весь UV-квадрат с отступом; фейсы "
                      "UDIM-материалов остаются в своём тайле. Порог "
                      "разрешения не проверяется")
    bl_options = {'REGISTER', 'UNDO'}

    def _execute(self, context):
        settings = _get_settings(context)
        margin = settings.stub_margin if settings else 0.9

        total = 0
        degenerate = 0
        out_of_zone = 0
        selected_any = False
        uv_created = []
        atlas_objects = []
        udim_objects = []

        for obj in _edit_mesh_objects(context):
            bm = bmesh.from_edit_mesh(obj.data)
            faces = [f for f in bm.faces if f.select]
            if not faces:
                continue
            selected_any = True
            # the UV layer is created LAZILY, at the first real write: it is
            # a mutation of the LIVE edit-BMesh that survives leaving Edit
            # Mode, so creating it before knowing whether anything gets
            # unwrapped left a stray layer behind a {'CANCELLED'}
            uv_layer = bm.loops.layers.uv.active
            mat_world = obj.matrix_world
            keep_tiles = object_has_udim(obj)
            n_done = 0
            for f in faces:
                tile_uv = (0.0, 0.0)
                if keep_tiles:
                    uvs = ([tuple(loop[uv_layer].uv) for loop in f.loops]
                           if uv_layer is not None
                           else [(0.0, 0.0)] * len(f.loops))
                    num = _face_tile_number(uvs)
                    if num is None:
                        # negative parking zone / past the row end - a
                        # clamped tile would teleport the face onto 1001
                        out_of_zone += 1
                        continue
                    tile_uv = _tile_offset(num)
                # WORLD space (see _stub_face_uvs): on a mirrored instance a
                # local basis comes out left-handed in the world, i.e. the
                # unwrap is mirrored against the non-mirrored twin
                pts = [mat_world @ loop.vert.co for loop in f.loops]
                new_uvs = _stub_face_uvs(pts, _world_normal(pts),
                                         tile_uv, margin)
                if new_uvs is None:
                    degenerate += 1
                    continue
                self._mutated = True   # live edit-BMesh write starts here
                if uv_layer is None:
                    uv_layer = bm.loops.layers.uv.new("UVMap")
                    uv_created.append(obj.name)
                for loop, uv in zip(f.loops, new_uvs):
                    loop[uv_layer].uv = uv
                n_done += 1
            if n_done:
                total += n_done
                _note_uv_overwrite_counts(obj, len(bm.faces), n_done,
                                          atlas_objects, udim_objects)
                bmesh.update_edit_mesh(obj.data, loop_triangles=False,
                                       destructive=False)

        if not selected_any:
            agr_report(self, 'ERROR',
                       "Нет выделенных фейсов — выделите полигоны в Edit Mode")
            return {'CANCELLED'}
        if total == 0:
            reason = ("Развернуть нечего: фейсы вырождены или вне валидной "
                      "UDIM-зоны")
            if uv_created:
                # a layer in the LIVE edit-BMesh survives leaving Edit Mode;
                # {'CANCELLED'} pushes no undo step and would leave it there
                agr_report(self, 'WARNING',
                           reason + "; UV-слой создан у: "
                           + ", ".join(uv_created)
                           + " — отменить можно через Ctrl+Z")
                return {'FINISHED'}
            agr_report(self, 'ERROR', reason)
            return {'CANCELLED'}

        msg = f"✅ Развёрнуто фейсов внахлёст: {total}"
        level = 'INFO'
        if degenerate:
            msg += f", вырожденных пропущено: {degenerate}"
        if out_of_zone:
            msg += f", вне валидной UDIM-зоны пропущено: {out_of_zone}"
        if atlas_objects:
            msg += f" | ⚠️ перезаписаны UV атласа: {', '.join(atlas_objects)}"
            level = 'WARNING'
        if udim_objects:
            msg += (f" | ⚠️ перезаписана развёртка UDIM-объектов: "
                    f"{', '.join(udim_objects)}")
            level = 'WARNING'
        agr_report(self, level, msg)
        return {'FINISHED'}


class AGR_OT_UVOrganicUnwrap(Operator):
    """Cut organic meshes by the world 3D grid, one UV square per piece"""
    bl_idname = "agr.uv_organic_unwrap"
    bl_label = "Нарезать органику"
    bl_description = ("Нарезать выбранные меши мировой 3D-сеткой и развернуть "
                      "КАЖДЫЙ кусок на свой UV-квадрат 0..1: кусок проецируется "
                      "по своей нормали, поэтому квадраты остаются квадратами и "
                      "на сложной органике (скульпт, дерево, Сузанна). Куски "
                      "лежат в одном квадрате внахлёст — тайловая текстура "
                      "показывает по одному чистому тайлу на кусок; для "
                      "запекания и атласа такая развёртка НЕ годится")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if context.mode != 'OBJECT':
            cls.poll_message_set("Работает в объектном режиме")
            return False
        if any(o.type == 'MESH' for o in context.selected_objects):
            return True
        if context.active_object is not None and context.active_object.type == 'MESH':
            return True
        cls.poll_message_set("Выберите MESH-объекты")
        return False

    def execute(self, context):
        settings = _get_settings(context)
        if settings is None:
            return {'CANCELLED'}
        params = _organic_params(settings)

        objs = [o for o in context.selected_objects if o.type == 'MESH']
        if not objs and context.active_object is not None \
                and context.active_object.type == 'MESH':
            objs = [context.active_object]

        stats = _organic_stats()
        # library data is READ-ONLY by contract, but bmesh writes it anyway:
        # bm.to_mesh() on a linked datablock does not raise, it mutates the
        # library mesh in place (verified on 5.2) — an edit nobody can save
        # that hits every user of that library
        linked = [o.name for o in objs
                  if o.library is not None or o.data.library is not None]
        objs = [o for o in objs
                if o.library is None and o.data.library is None]

        # one pass per MESH datablock: linked duplicates share the mesh, a
        # second pass would cut the same geometry twice.  The catch is that
        # this pipeline is WORLD-space, so the pass runs with ONE object's
        # matrix - a shared mesh whose users sit at different transforms
        # cannot be right for all of them, and that must be said out loud
        # instead of silently favouring whoever came first in the selection.
        seen_data, unique = {}, []
        for o in objs:
            first = seen_data.get(o.data)
            if first is not None:
                if not _matrix_close(first.matrix_world, o.matrix_world):
                    stats['shared_matrix_objects'].append(o.name)
                continue
            seen_data[o.data] = o
            unique.append(o)

        prepared, blocked, over, failed = [], [], [], []
        skipped_no_faces = 0
        try:
            for obj in unique:
                mesh = obj.data
                if mesh.shape_keys is not None:
                    # bmesh.from_mesh() does not read shape keys, so writing
                    # the mesh back would drop them — and that holds even
                    # with the cut switched off.  Edit Mode («Нарезать
                    # выделенное») works on the live edit-BMesh and carries
                    # them correctly, so send the user there instead.
                    blocked.append(obj.name)
                    continue
                bm = bmesh.new()
                bm.from_mesh(mesh)
                faces = [f for f in bm.faces if not f.hide]
                if not faces:
                    bm.free()
                    skipped_no_faces += 1
                    continue
                plan = (_organic_plan_cut(obj.matrix_world, faces, params['cell'])
                        if params['cut'] else [])
                if plan is None:
                    over.append(obj.name)
                    bm.free()
                    continue
                if obj.modifiers:
                    stats['modifier_objects'].append(obj.name)
                prepared.append((obj, bm, faces, plan))

            if over:
                # nothing has been written yet — abort the WHOLE operator
                _organic_cap_error(self, over)
                return {'CANCELLED'}
            if not prepared:
                msg = "Нет мешей для нарезки"
                if blocked:
                    msg += f" (пропущены объекты с shape keys: {', '.join(blocked)})"
                if linked:
                    msg += f" (данные из библиотеки: {', '.join(linked)})"
                agr_report(self, 'ERROR', msg)
                return {'CANCELLED'}

            for obj, bm, faces, plan in prepared:
                # per-object isolation: once ANY mesh has been written the
                # operator must still finish, because a {'CANCELLED'} return
                # pushes no undo step and would weld that write into the
                # PREVIOUS undo entry, out of Ctrl+Z's reach
                try:
                    if _organic_apply(obj, bm, faces, plan, params, stats):
                        bm.to_mesh(obj.data)
                        obj.data.update()
                except Exception as exc:
                    failed.append(f"{obj.name}: {exc}")
        finally:
            for _obj, bm, _faces, _plan in prepared:
                bm.free()

        if not _organic_report(self, stats, params, blocked, skipped_no_faces,
                               linked=linked, failed=failed):
            return {'CANCELLED'}
        return {'FINISHED'}


class AGR_OT_UVOrganicUnwrapSelected(_AGR_UVGridPollMixin, Operator):
    """Cut the target faces by the world 3D grid, one UV square per piece"""
    bl_idname = "agr.uv_organic_unwrap_selected"
    bl_label = "Нарезать выделенное"
    bl_description = ("То же самое в режиме редактирования: нарезать и "
                      "развернуть выделенные фейсы (или весь меш — по "
                      "переключателю «Область»). Единственный путь для мешей "
                      "с shape keys")
    bl_options = {'REGISTER', 'UNDO'}

    def _execute(self, context):
        settings = _get_settings(context)
        if settings is None:
            return {'CANCELLED'}
        params = _organic_params(settings)
        targets = _collect_targets(context, settings)
        if not targets:
            _report_no_targets(self, settings)
            return {'CANCELLED'}

        stats = _organic_stats()
        plans, over = [], []
        for obj, bm, faces in targets:
            if obj.modifiers:
                # same warning as the Object path: the unwrap runs on the
                # BASE mesh, so two buttons of one panel must not disagree
                stats['modifier_objects'].append(obj.name)
            plan = (_organic_plan_cut(obj.matrix_world, faces, params['cell'])
                    if params['cut'] else [])
            if plan is None:
                over.append(obj.name)
                continue
            plans.append((obj, bm, faces, plan))
        if over:
            # plan EVERY object before ANY bisect: a cancelled operator
            # pushes no undo step, so a partial cut would be unrecoverable
            _organic_cap_error(self, over)
            return {'CANCELLED'}

        reselect = settings.selection_mode == 'SELECTED'
        failed = []
        for obj, bm, faces, plan in plans:
            # per-object isolation, same as the Object path: this is the
            # LIVE edit-BMesh, so once ANY object has been cut the operator
            # must still finish — an uncaught exception here would skip
            # FINISHED, push no undo step and weld the cut into the
            # PREVIOUS undo entry, out of Ctrl+Z's reach (reproduced with
            # two objects in multi-object Edit Mode)
            self._mutated = True
            try:
                _organic_apply(obj, bm, faces, plan, params, stats,
                               reselect=reselect)
                bmesh.update_edit_mesh(obj.data, loop_triangles=True,
                                       destructive=bool(plan))
            except Exception as exc:
                failed.append(f"{obj.name}: {exc}")

        # committed=True: bmesh.from_edit_mesh hands out THE edit BMesh, not
        # a copy, so the bisect above is already in the mesh — it survives
        # leaving Edit Mode even with update_edit_mesh never called (that
        # call only refreshes the tessellation).  There is therefore no
        # "do not commit" option here, and cancelling would leave the cut
        # un-undoable; _organic_report finishes with a WARNING instead
        if not _organic_report(self, stats, params, committed=True,
                               failed=failed):
            return {'CANCELLED'}
        return {'FINISHED'}


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
            topz_src = s.grid_source == 'TOPZ'
            row = col.row(align=True)
            row.prop(s, "world_cell_u", text="X" if topz_src else "U")
            row.prop(s, "world_cell_v", text="Y" if topz_src else "V")
            row = col.row(align=True)
            row.prop(s, "world_angle", text="Поворот")
            row.operator("agr.uv_grid_angle_from_edge", text="", icon='EYEDROPPER')
            if s.grid_source == 'TOPZ':
                col.label(text="Вид сверху: U=+X, V=+Y, начало (0,0,0)", icon='AXIS_TOP')
            else:
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
        topz = s.grid_source == 'TOPZ'
        # plan view ignores both of these — grey them out instead of hiding
        # them so the panel does not jump when the source changes
        row = col.row(align=True)
        row.enabled = not topz
        row.prop(s, "projection", expand=True)
        row = col.row(align=True)
        row.prop(s, "selection_mode", expand=True)
        sub = col.row(align=True)
        sub.enabled = not topz
        sub.prop(s, "auto_orient")
        row = col.row(align=True)
        # swap flips handedness and TOPZ has no auto-orient to compensate -
        # _resolve_basis ignores it there, so grey it out (flips stay: an
        # explicit mirror is intentional under every source)
        sub = row.row(align=True)
        sub.enabled = not topz
        sub.prop(s, "swap_axes", toggle=True)
        row.prop(s, "flip_u", toggle=True)
        row.prop(s, "flip_v", toggle=True)
        row = col.row(align=True)
        row.prop(s, "offset_u", text="Сдвиг U")
        row.prop(s, "offset_v", text="Сдвиг V")
        row.operator("agr.uv_grid_offset_from_point", text="", icon='EYEDROPPER')
        col.prop(s, "snap_tolerance", text="Прилипание")

        layout.separator()
        col = layout.column(align=True)
        row = col.row(align=True)
        row.scale_y = 1.5
        row.operator("agr.uv_grid_unwrap", text="Развернуть по сетке", icon='UV')
        col.operator("agr.uv_grid_cut", text="Разрезать по сетке", icon='MESH_GRID')
        col.operator("agr.uv_grid_cut_unwrap", text="Разрезать и развернуть", icon='MOD_UVPROJECT')


class AGR_PT_UVStubPanel(Panel):
    """Stub unwrap tools (sub-panel of AGR UV)"""
    bl_label = "Заглушки"
    bl_idname = "AGR_PT_uv_stub_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_parent_id = "AGR_PT_uv_panel"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        s = _get_settings(context)
        if s is None:
            return
        row = layout.row(align=True)
        row.prop(s, "stub_threshold", text="Порог")
        row.prop(s, "stub_margin", text="Отступ")
        col = layout.column(align=True)
        col.operator("agr.uv_unwrap_stub", icon='SHADING_BBOX')
        col.operator("agr.uv_unwrap_stub_selected", icon='UV_FACESEL')
        if context.mode == 'OBJECT':
            col.label(text="«Выделенные фейсы» — в Edit Mode", icon='INFO')


class AGR_PT_UVOrganicPanel(Panel):
    """Organic (voxel-piece) unwrap tools (sub-panel of AGR UV)"""
    bl_label = "Органика"
    bl_idname = "AGR_PT_uv_organic_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_parent_id = "AGR_PT_uv_panel"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        s = _get_settings(context)
        if s is None:
            return
        col = layout.column(align=True)
        col.prop(s, "organic_cell", text="Кусок")
        col.prop(s, "organic_cut", text="Резать меш")
        col.prop(s, "organic_angle", text="Разброс нормалей")
        col.prop(s, "organic_merge", text="Слить мелкие")
        col.prop(s, "organic_margin", text="Отступ")
        row = layout.row(align=True)
        row.prop(s, "organic_fill", expand=True)
        row = layout.row(align=True)
        row.prop(s, "organic_align", expand=True)
        # scale_y belongs to the LAYOUT, not to the item added after it —
        # re-assigning it mid-column would resize the whole column (both
        # buttons AND the labels below).  Emphasis therefore needs its own
        # column, exactly like the stacked groups in ui.py:293/297/302
        col = layout.column(align=True)
        col.scale_y = 1.3
        col.operator("agr.uv_organic_unwrap", icon='MOD_REMESH')

        col = layout.column(align=True)
        col.operator("agr.uv_organic_unwrap_selected", icon='UV_FACESEL')
        col.label(text="Куски внахлёст: не для запекания", icon='INFO')
        if context.mode == 'EDIT_MESH':
            col.label(text="«Область» берётся из настроек выше", icon='INFO')


# ============================================================
# Registration
# ============================================================

classes = (
    AGR_UVGridSettings,
    AGR_OT_UVGridCapture,
    AGR_OT_UVGridClear,
    AGR_OT_UVGridAngleFromEdge,
    AGR_OT_UVGridOffsetFromPoint,
    AGR_OT_UVGridUnwrap,
    AGR_OT_UVGridCut,
    AGR_OT_UVGridCutUnwrap,
    AGR_OT_UVUnwrapStub,
    AGR_OT_UVUnwrapStubSelected,
    AGR_OT_UVOrganicUnwrap,
    AGR_OT_UVOrganicUnwrapSelected,
    AGR_PT_UVPanel,
    AGR_PT_UVStubPanel,
    AGR_PT_UVOrganicPanel,
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

    # by __name__, not identity: a dev reload builds a NEW function object,
    # so the identity check would leave the old module's handler registered
    _uv_drop_stale_handlers(bpy.app.handlers.load_post,
                            "_uv_sync_handlers_on_load")
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
    _uv_drop_stale_handlers(bpy.app.handlers.load_post,
                            "_uv_sync_handlers_on_load")

    _uv_remove_handlers()

    if hasattr(bpy.types.WindowManager, "agr_uv_grid_show"):
        del bpy.types.WindowManager.agr_uv_grid_show

    # guarded like the WM prop: after a partially failed register() an
    # unguarded del would abort the WHOLE operators.py unregister chain
    if hasattr(bpy.types.Scene, "agr_uv_settings"):
        del bpy.types.Scene.agr_uv_settings

    unregister_classes(classes)  # idempotent: survives a half-registered module (R-glue-4)
