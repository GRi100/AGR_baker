# AGR Link — наглядная визуализация памяти контейнера.
#
# Запуск: выделить контейнер, открыть Scripting → New → вставить → Run Script.
#
# Создаёт ТРИ слоя цвета (переключать кликом в Object Data Properties →
# Color Attributes, смотреть в Solid mode: Viewport Shading → Color → Attribute):
#
#   AGR_Demo_GROUP — цвет по ЛИНК-ГРУППЕ: все копии одного меша одного тона,
#                    яркость слегка меняется от инстанса к инстансу, поэтому
#                    видны и «кто с кем слинкован», и границы отдельных копий;
#   AGR_Demo_ID    — свой цвет на КАЖДЫЙ инстанс;
#   AGR_Demo_ORIG  — белый = вершина была при джойне, красный = дорисована.
#
# Плюс печатает в консоль легенду групп и расшифровку сырых байтов
# AGR_Link_ID / AGR_Link_CO (доказательство раскладки каналов).
#
# Служебные слои демо НЕ удаляются кнопкой «Удалить память (сдача)» —
# снесите их вручную перед отдачей файла.

import colorsys
import importlib
import json

import bpy
import numpy as np

MODE = 'GROUP'            # какой слой сделать активным: 'GROUP' | 'ID' | 'ORIG'
ASSIGN_MATERIAL = False   # True — заменить материалы объекта на демо-материал
DUMP_FACES = 8            # сколько граней распечатать в консоль (0 — не печатать)

ATTR_ID = "agr_link_id"      # INT, FACE  — номер инстанса
ATTR_ORIG = "agr_link_orig"  # BOOLEAN, POINT — «существовала при джойне»
COL_CO = "AGR_Link_CO"       # RGBA = x_hi, x_lo, y_hi, y_lo
COL_ID = "AGR_Link_ID"       # RGBA = z_hi, z_lo, flag<<7|id_hi, id_lo
DEMO_GROUP = "AGR_Demo_GROUP"
DEMO_ID = "AGR_Demo_ID"
DEMO_ORIG = "AGR_Demo_ORIG"
GREY = (0.15, 0.15, 0.15, 1.0)   # непомеченная / чужая геометрия


# ---------------------------------------------------------------- helpers

def link_module():
    """Модуль аддона, если он установлен — тогда таблица читается его же
    кодом (виртуальный merged-вид, фолбэк на цветовое зеркало)."""
    for name in ("AGR_tools.operators_link",
                 "bl_ext.user_default.AGR_tools.operators_link",
                 "bl_ext.vscode_development.AGR_tools.operators_link"):
        try:
            return importlib.import_module(name)
        except Exception:
            continue
    return None


def read_table(obj):
    mod = link_module()
    if mod is not None:
        try:
            return mod.read_table(obj)
        except Exception:
            pass
    raw = obj.get("agr_link_data")
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def srgb_bytes(attr):
    """Слой цвета как байты на sRGB-сетке b/255 — ровно то, что пишет аддон."""
    arr = np.zeros(len(attr.data) * 4, dtype=np.float32)
    attr.data.foreach_get("color_srgb", arr)
    return np.clip(np.rint(arr.astype(np.float64) * 255.0), 0, 255).astype(np.uint8)


def face_ids(mesh):
    attr = mesh.attributes.get(ATTR_ID)
    if attr is None or attr.domain != 'FACE':
        return None
    arr = np.zeros(len(mesh.polygons), dtype=np.intc)
    attr.data.foreach_get("value", arr)
    return arr


def orig_flags(mesh):
    attr = mesh.attributes.get(ATTR_ORIG)
    if attr is None or attr.domain != 'POINT':
        return None
    arr = np.zeros(len(mesh.vertices), dtype=bool)
    attr.data.foreach_get("value", arr)
    return arr


def hue_color(ordinal, sat=0.8, val=1.0):
    """Разнесённые тона: золотое сечение по кругу цветов."""
    r, g, b = colorsys.hsv_to_rgb((ordinal * 0.61803398875) % 1.0, sat, val)
    return (r, g, b, 1.0)


def new_corner_color(mesh, name):
    old = mesh.attributes.get(name)
    if old is not None:
        mesh.attributes.remove(old)
    return mesh.color_attributes.new(name=name, type='FLOAT_COLOR', domain='CORNER')


# ---------------------------------------------------------------- dump

def dump_packed(obj, limit):
    """Расшифровка сырых байтов — доказательство раскладки каналов."""
    if limit <= 0:
        return
    mesh = obj.data
    col_id = mesh.attributes.get(COL_ID)
    col_co = mesh.attributes.get(COL_CO)
    if col_id is None or col_co is None:
        print("ℹ️  Цветового зеркала нет — печать байтов пропущена")
        return
    table = read_table(obj) or {}
    co_min = np.asarray(table.get("co_min", [0.0, 0.0, 0.0]), dtype=np.float64)
    co_size = np.asarray(table.get("co_size", [1.0, 1.0, 1.0]), dtype=np.float64)

    b_id = srgb_bytes(col_id).reshape(-1, 4).astype(np.int64)
    b_co = srgb_bytes(col_co).reshape(-1, 4).astype(np.int64)
    n_polys = len(mesh.polygons)
    loop_start = np.zeros(n_polys, dtype=np.intc)
    mesh.polygons.foreach_get("loop_start", loop_start)

    print(f"\n{'='*76}\nAGR_Link_ID / AGR_Link_CO — первые {min(limit, n_polys)} граней")
    print(f"co_min={co_min.round(3).tolist()}  co_size={co_size.round(3).tolist()}")
    print(f"{'грань':>6} {'R,G,B,A (ID)':>18} {'id':>5} {'orig':>5} {'координата вершины':>30}")
    for i in range(min(limit, n_polys)):
        ls = int(loop_start[i])
        r, g, b, a = b_id[ls]
        iid = ((b & 127) << 8) | a       # id: 7 бит из B + весь A
        orig = b >= 128                   # бит 7 канала B
        z16 = (r << 8) | g                # Z: R — старший байт
        x16 = (b_co[ls][0] << 8) | b_co[ls][1]
        y16 = (b_co[ls][2] << 8) | b_co[ls][3]
        co = np.array([x16, y16, z16], dtype=np.float64) / 65535.0 * co_size + co_min
        print(f"{i:>6} {str([int(r), int(g), int(b), int(a)]):>18} {iid:>5} "
              f"{str(bool(orig)):>5} {np.round(co, 4).tolist()!s:>30}")
    print("=" * 76)


# ---------------------------------------------------------------- layers

def build_layers(obj):
    mesh = obj.data
    n_loops = len(mesh.loops)
    n_polys = len(mesh.polygons)
    if n_loops == 0:
        print("❌ У меша нет граней")
        return False

    ids = face_ids(mesh)
    if ids is None:
        print("❌ Нет атрибута agr_link_id.\n"
              "   Нажмите «Джоин с памятью линков» на выделенном контейнере "
              "(один объект = обновить память) и запустите скрипт снова.")
        return False

    table = read_table(obj)
    if table is None:
        print("⚠️  Таблица контейнера не читается — группы недоступны, "
              "будет только раскраска по инстансам")
        instances, groups = {}, {}
    else:
        instances = table.get("instances", {})
        groups = table.get("groups", {})

    # id инстанса -> id группы; порядковый номер группы задаёт её тон
    id_to_group = {int(k): int(v.get("group", 0)) for k, v in instances.items()}
    group_ids = sorted({g for g in id_to_group.values()})
    group_ordinal = {gid: i for i, gid in enumerate(group_ids)}
    # порядковый номер инстанса ВНУТРИ группы задаёт градацию яркости
    per_group_members = {}
    for iid in sorted(id_to_group):
        per_group_members.setdefault(id_to_group[iid], []).append(iid)

    loop_total = np.zeros(n_polys, dtype=np.intc)
    mesh.polygons.foreach_get("loop_total", loop_total)
    ids_per_loop = np.repeat(ids, loop_total)
    uniq_ids = [int(v) for v in np.unique(ids_per_loop)]

    # --- слой по ГРУППАМ: тон = группа, яркость = номер копии в группе
    data_g = np.empty((n_loops, 4), dtype=np.float32)
    for iid in uniq_ids:
        gid = id_to_group.get(iid)
        if iid <= 0 or gid is None:
            data_g[ids_per_loop == iid] = GREY
            continue
        members = per_group_members.get(gid, [iid])
        pos = members.index(iid) if iid in members else 0
        # 1.0, 0.85, 0.7, 1.0, ... — копии различимы, тон группы сохраняется
        val = 1.0 - 0.15 * (pos % 3)
        data_g[ids_per_loop == iid] = hue_color(group_ordinal.get(gid, 0), 0.85, val)
    new_corner_color(mesh, DEMO_GROUP).data.foreach_set("color_srgb", data_g.ravel())

    # --- слой по ИНСТАНСАМ: свой тон на каждую копию
    data_i = np.empty((n_loops, 4), dtype=np.float32)
    for iid in uniq_ids:
        data_i[ids_per_loop == iid] = GREY if iid <= 0 else hue_color(iid)
    new_corner_color(mesh, DEMO_ID).data.foreach_set("color_srgb", data_i.ravel())

    # --- слой «была при джойне»
    flags = orig_flags(mesh)
    if flags is not None:
        vidx = np.zeros(n_loops, dtype=np.intc)
        mesh.loops.foreach_get("vertex_index", vidx)
        f_loop = flags[vidx]
        data_o = np.empty((n_loops, 4), dtype=np.float32)
        data_o[f_loop] = (0.9, 0.9, 0.9, 1.0)
        data_o[~f_loop] = (1.0, 0.05, 0.05, 1.0)
        new_corner_color(mesh, DEMO_ORIG).data.foreach_set("color_srgb", data_o.ravel())

    active_name = {'GROUP': DEMO_GROUP, 'ID': DEMO_ID, 'ORIG': DEMO_ORIG}.get(MODE, DEMO_GROUP)
    layer = mesh.color_attributes.get(active_name) or mesh.color_attributes.get(DEMO_GROUP)
    mesh.color_attributes.active_color = layer
    mesh.attributes.default_color_name = layer.name

    # --- легенда групп
    print(f"\n📊 Легенда групп ({len(group_ids)} групп, "
          f"{len(id_to_group)} инстансов в памяти):")
    for gid in group_ids:
        info = groups.get(str(gid), {})
        members = per_group_members.get(gid, [])
        present = [i for i in members if i in uniq_ids]
        r, g, b, _a = hue_color(group_ordinal[gid], 0.85, 1.0)
        hexcol = "#%02X%02X%02X" % (int(r * 255), int(g * 255), int(b * 255))
        print(f"   {hexcol}  группа {gid:>3} · {info.get('data_name', '?'):<24} "
              f"копий в памяти: {len(members):>3}, с гранями в меше: {len(present):>3}")
    stray = [i for i in uniq_ids if i <= 0 or i not in id_to_group]
    if stray:
        print(f"   #262626  серым — непомеченные грани (id {stray})")
    if flags is not None:
        print(f"   Дорисованных после джойна вершин: {int((~flags).sum())} из {len(flags)}")
    print(f"\n✅ Слои: {DEMO_GROUP} / {DEMO_ID}"
          + (f" / {DEMO_ORIG}" if flags is not None else "")
          + f"; активен {layer.name}")
    print("👉 Solid mode → Viewport Shading (стрелка вниз) → Color → Attribute")
    print("👉 Переключать: Object Data Properties → Color Attributes → клик по слою")
    return True


def build_material():
    mat = bpy.data.materials.get("M_AGR_LinkDemo")
    if mat is None:
        mat = bpy.data.materials.new("M_AGR_LinkDemo")
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (300, 0)
    emit = nt.nodes.new("ShaderNodeEmission")    # плоский цвет без освещения
    emit.location = (100, 0)
    col = nt.nodes.new("ShaderNodeVertexColor")  # это и есть Color Attribute
    col.layer_name = {'GROUP': DEMO_GROUP, 'ID': DEMO_ID, 'ORIG': DEMO_ORIG}.get(MODE, DEMO_GROUP)
    col.location = (-150, 0)
    nt.links.new(col.outputs["Color"], emit.inputs["Color"])
    nt.links.new(emit.outputs["Emission"], out.inputs["Surface"])
    print(f"✅ Материал {mat.name}: Color Attribute({col.layer_name}) → Emission")
    return mat


def main():
    obj = bpy.context.active_object
    if obj is None or obj.type != 'MESH':
        print("❌ Выделите меш-контейнер AGR Link")
        return
    print(f"\n🔍 Объект: {obj.name}  (граней {len(obj.data.polygons)}, "
          f"вершин {len(obj.data.vertices)})")
    dump_packed(obj, DUMP_FACES)
    if not build_layers(obj):
        return
    mat = build_material()
    if ASSIGN_MATERIAL:
        obj.data.materials.clear()
        obj.data.materials.append(mat)
        print("✅ Демо-материал назначен (исходные слоты объекта заменены)")
    else:
        print("ℹ️  ASSIGN_MATERIAL = False — материал создан, но не назначен")


main()
