"""
AGR Lights - replace selected objects with light sources.

Ported from the standalone "Light Replacer" addon into AGR Tools.
Settings live in a PropertyGroup on the Scene (agr_light_settings) so they
are saved with the .blend file, mirroring the AGR_BakerSettings pattern.

Distance-check overlay (city requirement #5): the minimum distance between
neighbouring light sources is 5 m around an omnidirectional (POINT) light
and 1.5 m around a cone (SPOT) light.  A pair violates the rule when the
distance is below the LARGER of the two exclusion radii.  The overlay draws
exclusion-radius wire spheres, lines with distance labels between lights and
highlights violators in red, live in the 3D viewport.  Toggled via
WindowManager.agr_light_distance_show (same pattern as AGR Sync); draw
handlers are global to SpaceView3D and removed in unregister() so the
reloadOnSave dev loop does not leak zombie handlers.
"""

import math
import traceback

import bpy
import blf
import gpu
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    PointerProperty,
)
from bpy.types import Operator, Panel, PropertyGroup
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader


# ────────────────────────────────────────────────────────────────────
# Distance overlay: module state
# ────────────────────────────────────────────────────────────────────

_handle_3d = None
_handle_2d = None

# fingerprint → prebuilt draw data; rebuilt only when lights move or
# settings change, so per-redraw cost stays O(n) instead of O(n²)
_overlay_cache = {"fp": None, "data": None}

# (lights, violations, bad_types) for the panel; filled by _get_overlay_data
_last_stats = None

_draw_error_logged = False

_CIRCLE_SEGMENTS = 32
_MAX_LABELS = 150  # label clutter cap; lines are still drawn for every pair


def _build_unit_rings():
    """Three axis-aligned unit circles (XY/XZ/YZ) approximating a sphere."""
    rings = []
    for plane in range(3):
        ring = []
        for i in range(_CIRCLE_SEGMENTS + 1):
            a = 2.0 * math.pi * i / _CIRCLE_SEGMENTS
            c, s = math.cos(a), math.sin(a)
            if plane == 0:
                ring.append((c, s, 0.0))
            elif plane == 1:
                ring.append((c, 0.0, s))
            else:
                ring.append((0.0, c, s))
        rings.append(ring)
    return rings


_UNIT_RINGS = _build_unit_rings()


def _tag_redraw_view3d(_self=None, _context=None):
    """Redraw every 3D viewport (also used as a property update callback)."""
    wm = bpy.context.window_manager
    if wm is None:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


class AGR_LightReplacerSettings(PropertyGroup):
    """Settings for the Replace-with-Light operator"""

    light_type: EnumProperty(
        name="Тип света",
        description="Тип создаваемого источника света",
        items=[
            ('POINT', "Точечный", "Точечный источник света"),
            ('SUN', "Солнце", "Солнечный свет"),
            ('SPOT', "Прожектор", "Направленный прожектор"),
            ('AREA', "Площадный", "Площадный источник света"),
        ],
        default='POINT',
    )

    power: FloatProperty(
        name="Мощность (Вт)",
        description="Мощность света в ваттах",
        default=100.0,
        min=0.0,
        soft_max=10000.0,
    )

    color: FloatVectorProperty(
        name="Цвет",
        description="Цвет света",
        subtype='COLOR',
        default=(1.0, 1.0, 1.0),
        min=0.0,
        max=1.0,
    )

    spot_size: FloatProperty(
        subtype='ANGLE',
        name="Размер конуса",
        description="Размер конуса прожектора",
        default=0.785398,  # 45 degrees
        min=0.017453,      # 1 degree
        max=3.141593,      # 180 degrees
    )

    spot_blend: FloatProperty(
        name="Размытие",
        description="Размытие краёв прожектора",
        default=0.15,
        min=0.0,
        max=1.0,
    )

    area_size: FloatProperty(
        name="Размер",
        description="Размер площадного света",
        default=1.0,
        min=0.01,
        max=100.0,
    )

    area_shape: EnumProperty(
        name="Форма",
        description="Форма площадного света",
        items=[
            ('SQUARE', "Квадрат", "Квадратный площадный свет"),
            ('RECTANGLE', "Прямоугольник", "Прямоугольный площадный свет"),
            ('DISK', "Диск", "Круглый площадный свет"),
            ('ELLIPSE', "Эллипс", "Эллиптический площадный свет"),
        ],
        default='SQUARE',
    )

    point_radius: FloatProperty(
        name="Радиус",
        description="Радиус точечного света для мягких теней",
        default=0.1,
        min=0.0,
        max=10.0,
    )

    # ---- distance-check overlay (city requirement #5) ----

    dist_collection: PointerProperty(
        name="Коллекция светильников",
        description="Коллекция с источниками освещения для проверки расстояний "
                    "(включая вложенные коллекции)",
        type=bpy.types.Collection,
        update=_tag_redraw_view3d,
    )

    dist_omni_radius: FloatProperty(
        name="Радиус Omni",
        description="Минимальное расстояние от всенаправленного (POINT) источника "
                    "до любого другого светильника (требование: 5 м)",
        default=5.0,
        min=0.0,
        soft_max=50.0,
        subtype='DISTANCE',
        update=_tag_redraw_view3d,
    )

    dist_spot_radius: FloatProperty(
        name="Радиус Spot",
        description="Минимальное расстояние от конического (SPOT) прожектора "
                    "до любого другого светильника (требование: 1.5 м)",
        default=1.5,
        min=0.0,
        soft_max=50.0,
        subtype='DISTANCE',
        update=_tag_redraw_view3d,
    )

    dist_show_spheres: BoolProperty(
        name="Сферы радиусов",
        description="Рисовать запретную зону каждого светильника проволочной сферой "
                    "(зелёная — нарушений нет, красная — есть)",
        default=True,
        update=_tag_redraw_view3d,
    )

    dist_show_nearest: BoolProperty(
        name="Дистанции до соседей",
        description="Показывать линию и расстояние до ближайшего соседа для каждого "
                    "светильника в радиусе поиска ~8 радиусов зоны "
                    "(нарушения подсвечиваются всегда)",
        default=False,
        update=_tag_redraw_view3d,
    )


class AGR_OT_replace_with_light(Operator):
    """Replace every selected object with a light source"""
    bl_idname = "agr.replace_with_light"
    bl_label = "Replace with Light"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        # Removing the object being edited is unsupported - Object Mode only
        return context.mode == 'OBJECT'

    def execute(self, context):
        selected_objects = context.selected_objects.copy()
        if not selected_objects:
            self.report({'WARNING'}, "Нет выбранных объектов")
            return {'CANCELLED'}

        s = context.scene.agr_light_settings

        for obj in selected_objects:
            light_data = bpy.data.lights.new(name=f"{obj.name}_Light", type=s.light_type)
            light_data.energy = s.power
            light_data.color = s.color

            if s.light_type == 'SPOT':
                light_data.spot_size = s.spot_size
                light_data.spot_blend = s.spot_blend
            elif s.light_type == 'AREA':
                light_data.shape = s.area_shape
                light_data.size = s.area_size
                if s.area_shape in {'RECTANGLE', 'ELLIPSE'}:
                    light_data.size_y = s.area_size
            elif s.light_type == 'POINT':
                light_data.shadow_soft_size = s.point_radius

            light_object = bpy.data.objects.new(name=f"{obj.name}_Light", object_data=light_data)

            # Keep the replacement in the same collections as the original
            collections = list(obj.users_collection) or [context.collection]
            for coll in collections:
                coll.objects.link(light_object)

            light_object.matrix_world = obj.matrix_world.copy()

            bpy.data.objects.remove(obj, do_unlink=True)

        self.report({'INFO'}, f"Заменено {len(selected_objects)} объектов на источники света")
        print(f"✅ AGR Lights: replaced {len(selected_objects)} objects with {s.light_type} lights")
        return {'FINISHED'}


# ────────────────────────────────────────────────────────────────────
# Distance overlay: computation
# ────────────────────────────────────────────────────────────────────

def _collect_lights(settings):
    """(name, world position, light type, exclusion radius) for every
    POINT/SPOT light in the chosen collection; SUN/AREA are only counted —
    the city requirements do not allow them at all (requirement #4)."""
    lights = []
    bad_types = 0
    for obj in settings.dist_collection.all_objects:
        if obj.type != 'LIGHT' or obj.data is None:
            continue
        light_type = obj.data.type
        if light_type == 'POINT':
            lights.append((obj.name, obj.matrix_world.translation.copy(),
                           light_type, settings.dist_omni_radius))
        elif light_type == 'SPOT':
            lights.append((obj.name, obj.matrix_world.translation.copy(),
                           light_type, settings.dist_spot_radius))
        else:
            bad_types += 1
    return lights, bad_types


def _fingerprint(lights, settings):
    return (
        settings.dist_collection.name if settings.dist_collection else "",
        round(settings.dist_omni_radius, 3),
        round(settings.dist_spot_radius, 3),
        settings.dist_show_spheres,
        settings.dist_show_nearest,
        tuple((name, light_type, round(pos.x, 4), round(pos.y, 4), round(pos.z, 4))
              for name, pos, light_type, _radius in lights),
    )


_NEAR_MAX_RINGS = 8  # nearest-neighbour search horizon, in grid cells


def _build_spatial_grid(positions, cell_size):
    """Uniform hash grid: cell key → list of light indices.  Returns the
    grid plus each light's own cell key (aligned with positions)."""
    inv = 1.0 / cell_size
    grid = {}
    cells = []
    for idx, pos in enumerate(positions):
        key = (int(math.floor(pos.x * inv)),
               int(math.floor(pos.y * inv)),
               int(math.floor(pos.z * inv)))
        grid.setdefault(key, []).append(idx)
        cells.append(key)
    return grid, cells


def _shell_keys(cx, cy, cz, ring):
    """Cell keys on the cube shell at Chebyshev distance `ring`."""
    if ring == 0:
        yield (cx, cy, cz)
        return
    for dx in range(-ring, ring + 1):
        for dy in range(-ring, ring + 1):
            for dz in range(-ring, ring + 1):
                if max(abs(dx), abs(dy), abs(dz)) == ring:
                    yield (cx + dx, cy + dy, cz + dz)


def _nearest_in_grid(i, positions, grid, cells, cell_size):
    """Expanding-shell nearest-neighbour search.  A cell at Chebyshev
    distance k is at least (k-1)*cell_size away, so once the best found
    distance is <= ring*cell_size no farther shell can beat it.  Search
    stops after _NEAR_MAX_RINGS shells — a light with no neighbour inside
    that horizon simply gets no distance line."""
    cx, cy, cz = cells[i]
    pos = positions[i]
    best_j = None
    best_d = None
    for ring in range(_NEAR_MAX_RINGS + 1):
        if best_d is not None and best_d <= (ring - 1) * cell_size:
            break
        for key in _shell_keys(cx, cy, cz, ring):
            bucket = grid.get(key)
            if not bucket:
                continue
            for j in bucket:
                if j == i:
                    continue
                d = (pos - positions[j]).length
                if best_d is None or d < best_d:
                    best_j, best_d = j, d
    # a candidate found in a far shell's corner may still be farther than
    # unscanned cells of the next shell — beyond the horizon it is not
    # guaranteed to be the true nearest, so report nothing instead
    if best_j is None or best_d > _NEAR_MAX_RINGS * cell_size:
        return None
    return (best_j, best_d)


def _build_overlay_data(lights, settings):
    """Pair scan via a spatial hash grid → line coords, sphere coords and
    labels for drawing.  A pair violates when its distance is below
    max(radius_a, radius_b): each light owns an exclusion zone and no other
    light may enter it.  Any violating pair is closer than the largest
    exclusion radius, so with cell_size = max radius both lights always sit
    in adjacent cells — checking the 27 surrounding cells is exhaustive and
    distant light groups are never compared against each other."""
    n = len(lights)
    positions = [entry[1] for entry in lights]
    radii = [entry[3] for entry in lights]

    violating = [False] * n
    violation_pairs = []      # (i, j, distance, required)

    cell_size = max(settings.dist_omni_radius, settings.dist_spot_radius, 0.001)
    grid, cells = _build_spatial_grid(positions, cell_size)

    for i in range(n):
        pos_i = positions[i]
        cx, cy, cz = cells[i]
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    bucket = grid.get((cx + dx, cy + dy, cz + dz))
                    if not bucket:
                        continue
                    for j in bucket:
                        if j <= i:
                            continue  # each unordered pair once
                        dist = (pos_i - positions[j]).length
                        required = max(radii[i], radii[j])
                        if dist < required - 1e-6:
                            violation_pairs.append((i, j, dist, required))
                            violating[i] = True
                            violating[j] = True

    nearest = [None] * n      # (j, distance) within the search horizon
    if settings.dist_show_nearest:
        for i in range(n):
            nearest[i] = _nearest_in_grid(i, positions, grid, cells, cell_size)

    red_lines = []
    near_lines = []
    labels = []  # (world midpoint, text, is_violation) — violations first

    for i, j, dist, required in violation_pairs:
        red_lines.append(tuple(positions[i]))
        red_lines.append(tuple(positions[j]))
        mid = (positions[i] + positions[j]) * 0.5
        labels.append((mid, f"{dist:.2f} м  (мин. {required:g} м)", True))

    if settings.dist_show_nearest:
        labeled = {(min(i, j), max(i, j)) for i, j, _d, _r in violation_pairs}
        for i in range(n):
            if nearest[i] is None:
                continue
            j, dist = nearest[i]
            key = (min(i, j), max(i, j))
            if key in labeled:
                continue
            labeled.add(key)
            near_lines.append(tuple(positions[i]))
            near_lines.append(tuple(positions[j]))
            mid = (positions[i] + positions[j]) * 0.5
            labels.append((mid, f"{dist:.2f} м", False))

    red_spheres = []
    green_spheres = []
    if settings.dist_show_spheres:
        for idx in range(n):
            pos, radius = positions[idx], radii[idx]
            target = red_spheres if violating[idx] else green_spheres
            for ring in _UNIT_RINGS:
                for k in range(len(ring) - 1):
                    a, b = ring[k], ring[k + 1]
                    target.append((pos.x + a[0] * radius,
                                   pos.y + a[1] * radius,
                                   pos.z + a[2] * radius))
                    target.append((pos.x + b[0] * radius,
                                   pos.y + b[1] * radius,
                                   pos.z + b[2] * radius))

    return {
        "red_lines": red_lines,
        "near_lines": near_lines,
        "red_spheres": red_spheres,
        "green_spheres": green_spheres,
        "labels": labels,
        "violating_names": [lights[i][0] for i in range(n) if violating[i]],
        "violation_count": len(violation_pairs),
        "light_count": n,
        "batches": {},  # built lazily inside the 3D draw callback
    }


def _get_overlay_data(scene):
    """Cached overlay data; recomputed only when the fingerprint changes."""
    global _last_stats
    settings = getattr(scene, "agr_light_settings", None)
    if settings is None or settings.dist_collection is None:
        _last_stats = None
        return None

    lights, bad_types = _collect_lights(settings)
    fp = _fingerprint(lights, settings)
    if _overlay_cache["fp"] != fp:
        _overlay_cache["fp"] = fp
        _overlay_cache["data"] = _build_overlay_data(lights, settings)

    data = _overlay_cache["data"]
    # SUN/AREA lights are not part of the fingerprint (they carry no
    # exclusion zone) — refresh their counter even on a cache hit
    data["bad_types"] = bad_types
    _last_stats = {
        "lights": data["light_count"],
        "violations": data["violation_count"],
        "bad_types": data["bad_types"],
    }
    return data


# ────────────────────────────────────────────────────────────────────
# Distance overlay: drawing
# ────────────────────────────────────────────────────────────────────

_COLOR_SPHERE_OK = (0.25, 0.85, 0.35, 0.35)
_COLOR_SPHERE_BAD = (1.0, 0.2, 0.15, 0.65)
_COLOR_LINE_BAD = (1.0, 0.15, 0.1, 0.95)
_COLOR_LINE_NEAR = (0.8, 0.8, 0.8, 0.5)
_COLOR_TEXT_BAD = (1.0, 0.35, 0.3, 1.0)
_COLOR_TEXT_NEAR = (0.9, 0.9, 0.9, 1.0)
_COLOR_TEXT_OK = (0.45, 0.9, 0.5, 1.0)
_COLOR_TEXT_WARN = (1.0, 0.75, 0.25, 1.0)


def _overlay_enabled():
    """Draw handlers survive a .blend load while the WindowManager (and its
    agr_light_distance_show value) is replaced — stale handlers must draw
    nothing until _sync_handlers_on_load reconciles them."""
    wm = getattr(bpy.context, "window_manager", None)
    return wm is not None and getattr(wm, "agr_light_distance_show", False)


def _draw_overlay_3d():
    global _draw_error_logged
    try:
        if not _overlay_enabled():
            return
        data = _get_overlay_data(bpy.context.scene)
        if data is None:
            return

        shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
        prev_blend = gpu.state.blend_get()
        prev_depth = gpu.state.depth_test_get()
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('NONE')
        try:
            viewport = gpu.state.viewport_get()
            shader.bind()
            shader.uniform_float("viewportSize", (viewport[2], viewport[3]))

            # draw order: OK spheres under red spheres under lines
            passes = (
                ("green_spheres", _COLOR_SPHERE_OK, 1.0),
                ("red_spheres", _COLOR_SPHERE_BAD, 2.0),
                ("near_lines", _COLOR_LINE_NEAR, 1.5),
                ("red_lines", _COLOR_LINE_BAD, 3.0),
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
    except Exception:
        if not _draw_error_logged:
            _draw_error_logged = True
            print(f"❌ AGR Lights: distance overlay 3D draw failed:\n{traceback.format_exc()}")


def _blf_text(x, y, text, color, size=12):
    blf.size(0, size)
    blf.color(0, *color)
    blf.enable(0, blf.SHADOW)
    blf.shadow(0, 3, 0.0, 0.0, 0.0, 0.9)
    blf.shadow_offset(0, 1, -1)
    blf.position(0, x, y, 0)
    blf.draw(0, text)
    blf.disable(0, blf.SHADOW)


def _draw_overlay_2d():
    global _draw_error_logged
    try:
        if not _overlay_enabled():
            return
        context = bpy.context
        region = context.region
        rv3d = context.region_data
        if region is None or rv3d is None:
            return

        settings = getattr(context.scene, "agr_light_settings", None)
        if settings is None:
            return
        if settings.dist_collection is None:
            _blf_text(12, 28, "AGR Свет: коллекция не выбрана", _COLOR_TEXT_WARN)
            return

        data = _get_overlay_data(context.scene)
        if data is None:
            return

        # distance labels at pair midpoints (violations first in the list)
        skipped = 0
        for idx, (mid, text, is_violation) in enumerate(data["labels"]):
            if idx >= _MAX_LABELS:
                skipped = len(data["labels"]) - _MAX_LABELS
                break
            co = view3d_utils.location_3d_to_region_2d(region, rv3d, mid)
            if co is None:
                continue
            color = _COLOR_TEXT_BAD if is_violation else _COLOR_TEXT_NEAR
            _blf_text(co.x + 5, co.y + 5, text, color, size=13 if is_violation else 11)

        # status block, bottom-left
        y = 28
        _blf_text(12, y, f"AGR Свет: {data['light_count']} "
                         f"(Omni ≥ {settings.dist_omni_radius:g} м, "
                         f"Spot ≥ {settings.dist_spot_radius:g} м)",
                  _COLOR_TEXT_NEAR)
        y += 18
        if data["violation_count"]:
            _blf_text(12, y, f"Нарушений: {data['violation_count']}", _COLOR_TEXT_BAD, size=14)
        else:
            _blf_text(12, y, "Нарушений нет", _COLOR_TEXT_OK, size=14)
        y += 18
        if data["bad_types"]:
            _blf_text(12, y, f"Недопустимые типы (не Omni/Spot): {data['bad_types']}",
                      _COLOR_TEXT_WARN)
            y += 18
        if skipped:
            _blf_text(12, y, f"Подписей скрыто: {skipped}", _COLOR_TEXT_WARN)
    except Exception:
        if not _draw_error_logged:
            _draw_error_logged = True
            print(f"❌ AGR Lights: distance overlay 2D draw failed:\n{traceback.format_exc()}")


# ────────────────────────────────────────────────────────────────────
# Distance overlay: handler lifecycle
# ────────────────────────────────────────────────────────────────────

def _add_handlers():
    global _handle_3d, _handle_2d, _draw_error_logged
    _draw_error_logged = False
    _overlay_cache["fp"] = None  # force fresh compute on enable
    if _handle_3d is None:
        _handle_3d = bpy.types.SpaceView3D.draw_handler_add(
            _draw_overlay_3d, (), 'WINDOW', 'POST_VIEW')
    if _handle_2d is None:
        _handle_2d = bpy.types.SpaceView3D.draw_handler_add(
            _draw_overlay_2d, (), 'WINDOW', 'POST_PIXEL')


def _remove_handlers():
    global _handle_3d, _handle_2d
    if _handle_3d is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_handle_3d, 'WINDOW')
        except Exception:
            pass
        _handle_3d = None
    if _handle_2d is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_handle_2d, 'WINDOW')
        except Exception:
            pass
        _handle_2d = None
    _overlay_cache["fp"] = None
    _overlay_cache["data"] = None


def _distance_toggle(_self, context):
    if context.window_manager.agr_light_distance_show:
        _add_handlers()
    else:
        _remove_handlers()
    _tag_redraw_view3d()


@bpy.app.handlers.persistent
def _sync_handlers_on_load(_dummy):
    """A file load replaces the WindowManager (the toggle value resets)
    while SpaceView3D draw handlers survive it — re-align handler state
    with the toggle actually visible after the load."""
    if _overlay_enabled():
        _add_handlers()  # also resets the fingerprint cache
    else:
        _remove_handlers()


class AGR_OT_light_distance_select_violations(Operator):
    """Select every light that violates the minimum-distance requirement"""
    bl_idname = "agr.light_distance_select_violations"
    bl_label = "Выделить нарушителей"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "agr_light_settings", None)
        if settings is None or settings.dist_collection is None:
            cls.poll_message_set("Выберите коллекцию со светильниками")
            return False
        return context.mode == 'OBJECT'

    def execute(self, context):
        data = _get_overlay_data(context.scene)
        if data is None:
            self.report({'WARNING'}, "Нет данных для проверки — выберите коллекцию")
            return {'CANCELLED'}

        names = data["violating_names"]
        if not names:
            self.report({'INFO'}, "Нарушений минимальных расстояний нет ✅")
            return {'FINISHED'}

        bpy.ops.object.select_all(action='DESELECT')
        selected = 0
        for name in names:
            obj = context.view_layer.objects.get(name)
            if obj is None:
                continue  # light is not in the current view layer
            try:
                obj.select_set(True)
                context.view_layer.objects.active = obj
                selected += 1
            except RuntimeError:
                pass  # hidden from the viewport — selection unsupported

        self.report({'WARNING'},
                    f"Выделено нарушителей: {selected} из {len(names)} "
                    f"(пар с нарушением: {data['violation_count']})")
        return {'FINISHED'}


class AGR_PT_LightsPanel(Panel):
    """AGR Lights panel in the AGR Tools sidebar"""
    bl_label = "AGR Lights"
    bl_idname = "AGR_PT_lights_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 20  # after ui.py panels (0), before AGR Share (100)

    def draw(self, context):
        layout = self.layout
        s = context.scene.agr_light_settings

        col = layout.column(align=True)
        col.label(text="Настройки источника света", icon='LIGHT')
        col.prop(s, "light_type", text="Тип")
        col.prop(s, "power", text="Мощность (Вт)")
        col.prop(s, "color", text="Цвет")

        if s.light_type == 'SPOT':
            spot_col = layout.column(align=True)
            spot_col.label(text="Прожектор", icon='LIGHT_SPOT')
            spot_col.prop(s, "spot_size", text="Размер конуса")
            spot_col.prop(s, "spot_blend", text="Размытие")
        elif s.light_type == 'AREA':
            area_col = layout.column(align=True)
            area_col.label(text="Площадной свет", icon='LIGHT_AREA')
            area_col.prop(s, "area_shape", text="Форма")
            area_col.prop(s, "area_size", text="Размер")
        elif s.light_type == 'POINT':
            point_col = layout.column(align=True)
            point_col.label(text="Точечный свет", icon='LIGHT_POINT')
            point_col.prop(s, "point_radius", text="Радиус")

        layout.separator()

        selected_count = len(context.selected_objects)
        if selected_count > 0:
            layout.label(text=f"Выбрано объектов: {selected_count}", icon='INFO')

        row = layout.row()
        row.scale_y = 2.0
        if selected_count > 0:
            row.operator("agr.replace_with_light", text=f"Заменить {selected_count} объектов")
        else:
            row.enabled = False
            row.operator("agr.replace_with_light", text="Выберите объекты для замены")


class AGR_PT_LightsDistancePanel(Panel):
    """Distance-check overlay controls (city requirement #5)"""
    bl_label = "Проверка расстояний"
    bl_idname = "AGR_PT_lights_distance_panel"
    bl_parent_id = "AGR_PT_lights_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        s = context.scene.agr_light_settings
        wm = context.window_manager

        layout.prop(s, "dist_collection", text="Коллекция")

        col = layout.column(align=True)
        col.prop(s, "dist_omni_radius", text="Радиус Omni")
        col.prop(s, "dist_spot_radius", text="Радиус Spot")

        col = layout.column(align=True)
        col.prop(s, "dist_show_spheres", text="Сферы радиусов")
        col.prop(s, "dist_show_nearest", text="Дистанции до соседей")

        row = layout.row()
        row.scale_y = 1.6
        # allow switching OFF even when the collection got deleted meanwhile
        row.enabled = s.dist_collection is not None or wm.agr_light_distance_show
        icon = 'HIDE_OFF' if wm.agr_light_distance_show else 'HIDE_ON'
        row.prop(wm, "agr_light_distance_show", text="Проверка расстояний",
                 icon=icon, toggle=True)

        if s.dist_collection is None:
            layout.label(text="Выберите коллекцию со светильниками", icon='INFO')
            return

        if wm.agr_light_distance_show and _last_stats is not None:
            layout.label(text=f"Светильников: {_last_stats['lights']}", icon='LIGHT')
            if _last_stats["violations"]:
                layout.label(text=f"Пар с нарушением: {_last_stats['violations']}",
                             icon='ERROR')
            else:
                layout.label(text="Нарушений нет", icon='CHECKMARK')
            if _last_stats["bad_types"]:
                layout.label(text=f"Недопустимые типы (не Omni/Spot): "
                                  f"{_last_stats['bad_types']}", icon='CANCEL')
            layout.operator("agr.light_distance_select_violations",
                            icon='RESTRICT_SELECT_OFF')


classes = (
    AGR_LightReplacerSettings,
    AGR_OT_replace_with_light,
    AGR_OT_light_distance_select_violations,
    AGR_PT_LightsPanel,
    AGR_PT_LightsDistancePanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.agr_light_settings = PointerProperty(type=AGR_LightReplacerSettings)

    bpy.types.WindowManager.agr_light_distance_show = BoolProperty(
        name="Проверка расстояний",
        description="Интерактивная проверка минимальных расстояний между источниками "
                    "света (Omni ≥ 5 м, Spot ≥ 1.5 м): сферы зон, линии и подписи "
                    "дистанций, красная подсветка нарушений",
        default=False,
        update=_distance_toggle,
    )

    if _sync_handlers_on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_sync_handlers_on_load)

    # reloadOnSave: the WindowManager value survives re-registration while
    # the draw handlers do not — re-add them if the overlay was left on
    try:
        wm = bpy.context.window_manager
        if wm is not None and getattr(wm, "agr_light_distance_show", False):
            _add_handlers()
    except Exception:
        pass  # restricted context during Blender startup

    print("✅ AGR Lights operators registered")


def unregister():
    if _sync_handlers_on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_sync_handlers_on_load)

    _remove_handlers()

    if hasattr(bpy.types.WindowManager, "agr_light_distance_show"):
        del bpy.types.WindowManager.agr_light_distance_show

    del bpy.types.Scene.agr_light_settings

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
