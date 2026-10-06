"""
Atlas creation operators for AGR Tools
"""

import bpy
from bpy.types import Operator
from bpy.props import EnumProperty, StringProperty
import os
import json
import numpy as np
import bmesh

try:
    from PIL import Image
except ImportError:  # the single addon-wide flag lives in log.pillow_available()
    Image = None

# Import material utilities
from .core.materials import connect_texture_set_to_material, connect_regular_texture_set_to_material, validate_all_high_mode
import re

from .core.atlas_store import (
    entry_folder_abs, find_atlas_entry, iter_atlas_entries,
    load_legacy_atlas_json, make_atlas_entry, read_atlas_record,
    record_from_legacy, save_legacy_atlas_json, serialize_layout,
    strip_atlas_record, write_atlas_record,
)
from .log import agr_report, pillow_available, unregister_classes
# Single source of truth for SM_ name parsing (all seven delivery types,
# .NNN suffix stripped); operators_rename never imports this module back
from .operators_rename import parse_sm_name, RENAME_ALLOWED_TYPES


# ===== HELPER FUNCTIONS =====

def process_object_name(obj_name):
    """
    Обрабатывает имя объекта для получения ADDRESS и типа
    Returns: (address, obj_type) or raises Exception

    Разбор делегирован единому источнику правды в operators_rename
    (все семь типов сдачи + срез блендеровского суффикса .NNN): локальный
    список из четырёх типов ронял SM_X_MainGlass и SM_X_Main.001, а
    вызывающий код молча уезжал в HIGH-схему с именами вне конвенции.
    """
    parsed = parse_sm_name(obj_name)
    if not parsed:
        allowed = ', '.join(sorted(RENAME_ALLOWED_TYPES['materials']))
        raise Exception(
            f"Имя '{obj_name}' вне конвенции SM_Адрес[_NNN]_Тип "
            f"(типы: {allowed})")
    address, number, obj_type = parsed
    # Номер — часть адреса во всех производных именах
    # (AGR Rename пишет M_{address}_{NNN}_{Type}_{idx})
    if number:
        address = f"{address}_{number}"
    return address, obj_type


def resolve_atlas_naming(obj):
    """(atlas_type, use_low_naming, address, obj_type, warning|None).

    Имя вне конвенции больше не уходит в молчаливый HIGH-фолбэк: он давал
    папки вида A_SM_X_Main.001_1 и материал M_A_SM_X_Main.001_1, которые не
    проходят проверку имён на сдаче."""
    try:
        address, obj_type = process_object_name(obj.name)
    except Exception as exc:
        return 'HIGH', False, None, None, (
            f"{exc} — атлас назван по объекту (A_{obj.name}), имена вне конвенции сдачи")
    return 'LOW', True, address, obj_type, None


def count_faces_with_uvs_outside_unit(obj, tolerance=0.001):
    """Count faces whose UVs leave the 0..1 square — tiled UVs cannot be
    linearly remapped into an atlas cell without bleeding into neighbours.

    foreach_get + numpy: the per-loop Python walk allocated a Vector per loop
    (~3.2M on a city Ground object) before every atlas apply."""
    mesh = obj.data
    if not mesh.uv_layers.active:
        return 0
    n_loops = len(mesh.loops)
    n_polys = len(mesh.polygons)
    if not n_loops or not n_polys:
        return 0

    uvs = np.empty(n_loops * 2, dtype=np.float32)
    mesh.uv_layers.active.data.foreach_get('uv', uvs)
    uvs = uvs.reshape(n_loops, 2)
    lo, hi = -tolerance, 1.0 + tolerance
    bad_loops = np.any((uvs < lo) | (uvs > hi), axis=1)
    if not bad_loops.any():
        return 0

    starts = np.empty(n_polys, dtype=np.int64)
    totals = np.empty(n_polys, dtype=np.int64)
    mesh.polygons.foreach_get('loop_start', starts)
    mesh.polygons.foreach_get('loop_total', totals)
    # per-face OR over its loop range = difference of a prefix sum
    cum = np.concatenate(([0], np.cumsum(bad_loops, dtype=np.int64)))
    per_face = cum[starts + totals] - cum[starts]
    return int(np.count_nonzero(per_face))


def atlas_record_names(obj):
    """Имена атласов из записи на объекте ('' если записи нет).
    Только чтение — мутаций ID-данных не делает."""
    try:
        record = read_atlas_record(obj)
    except Exception:
        return []
    if not record:
        return []
    names = []
    for entry in record.get('atlases', []):
        if isinstance(entry, dict) and entry.get('atlas_name'):
            names.append(entry['atlas_name'])
    return names


def check_atlas_uv_preconditions(op, obj):
    """Shared guard for all atlas-apply operators: no double apply (the
    linear UV remap is not idempotent) and no tiled UVs. Returns True when
    the object is safe to remap; reports the reason and returns False otherwise.

    EXECUTE-time only (reads the record without mutating) — draw/poll must
    stay on ATLAS_STORE.peek."""
    applied = obj.get('agr_atlas_applied')
    if not applied:
        # Второй источник правды: idprop не переживает дефолтный экспорт FBX,
        # а запись атласа переживает (цветовое зеркало) — реимпортированный
        # объект иначе проходил гард и сжимал UV второй раз
        names = atlas_record_names(obj)
        if names:
            applied = ", ".join(names)
    if applied:
        op.report({'ERROR'}, f"Атлас уже применён к объекту ({applied}) — повторный ремап исказит UV. Сначала Unpack Atlas.")
        return False

    if not obj.data.uv_layers.active:
        op.report({'ERROR'}, "У объекта нет UV-слоя — атлас ремапит существующую развёртку, разверните объект в 0..1")
        return False

    bad_faces = count_faces_with_uvs_outside_unit(obj)
    if bad_faces:
        op.report({'ERROR'}, f"UV выходят за пределы 0..1 у {bad_faces} полигонов (тайлинг) — сначала приведите развёртку в 0..1.")
        return False

    return True


def build_face_material_names(obj):
    """[имя материала | None] на каждый полигон — один foreach_get вместо
    двойного цикла O(слоты × полигоны) (48 млн итераций Python на городском
    меше с 60 материалами)."""
    mesh = obj.data
    n = len(mesh.polygons)
    if not n:
        return []
    idx = np.empty(n, dtype=np.int64)
    mesh.polygons.foreach_get('material_index', idx)
    # имя нормализуется: сосед по отодвинутому исходнику ('<имя>.src') должен
    # сопоставляться с тем же регионом раскладки, что и до переименования
    slot_names = [source_material_name(s.material.name) if s.material else None
                  for s in obj.material_slots]
    # хвостовой None ловит material_index за пределами списка слотов
    names = np.array(slot_names + [None], dtype=object)
    np.clip(idx, 0, len(slot_names), out=idx)
    return list(names[idx])


def uncovered_from_names(face_material_names, covered_materials):
    """{имя материала (None — пустой слот): число граней}, которые раскладка
    атласа НЕ покрывает.  Apply ремапил только сопоставленные грани, но
    переводил на атласный материал ВСЕ — остальные оставались с UV 0..1 и
    сэмплили весь атлас."""
    counts = {}
    for name in face_material_names:
        if name not in covered_materials:
            counts[name] = counts.get(name, 0) + 1
    return counts


def faces_outside_layout(obj, covered_materials):
    """uncovered_from_names по текущим слотам объекта."""
    return uncovered_from_names(build_face_material_names(obj), covered_materials)


def describe_uncovered(counts):
    """Человекочитаемый список несопоставленных материалов для отчёта."""
    parts = []
    for name in sorted(counts, key=lambda n: (n is not None, n or '')):
        parts.append(f"{name or 'пустой слот'} ({counts[name]})")
    return ", ".join(parts)


# Метка атласного материала: имя бина M_{addr}_{Type}_{i} буквально совпадает
# с именем, которое AGR Rename даёт ПЕРВОМУ материалу самого объекта, поэтому
# по имени отличить свой атласный датаблок от чужого исходника невозможно
ATLAS_MAT_TAG = 'agr_atlas'
SOURCE_MAT_SUFFIX = '.src'
# '.src' и блендеровский дубль-суффикс за ним ('.src.001'): claim_atlas_material
# отодвигает исходник, а Blender добивает имя при коллизии
_SRC_RE = re.compile(re.escape(SOURCE_MAT_SUFFIX) + r'(\.\d{3})?$')


def source_material_name(name):
    """Каноническое имя материала: '<имя>.src[.NNN]' → '<имя>'.

    Все поиски по имени (сет под материал слота, регион раскладки атласа)
    обязаны идти через этот хелпер: отодвинутый исходник остаётся стоять на
    ДРУГИХ объектах, и без нормализации сосед по материалу («GroundEl делит
    материал с Main», Shift+D-двойник, объект под Apply той же раскладки)
    получал отказ «Не найдены texture sets для материалов: …src»."""
    return _SRC_RE.sub('', name) if name else name


def _images_all_in_folder(material, folder_path):
    """True, когда ВСЕ TEX_IMAGE-ноды материала указывают в folder_path.

    Без проверки метки атласа — так на неё опирается и material_wired_to_set
    (исходник ↔ его сет), и claim_atlas_material (непомеченный датаблок ↔
    папка атласа: после дефолтного FBX-экспорта метка теряется вместе с
    custom properties)."""
    if not (material.use_nodes and material.node_tree):
        return False
    images = [n.image for n in material.node_tree.nodes
              if n.type == 'TEX_IMAGE' and n.image]
    if not images:
        return False
    target = os.path.normcase(os.path.abspath(folder_path))
    for img in images:
        path = bpy.path.abspath(img.filepath)
        if not path:
            return False
        if os.path.normcase(os.path.dirname(os.path.abspath(path))) != target:
            return False
    return True


def claim_atlas_material(material_name, atlas_name, atlas_folder=None):
    """(датаблок под атласные карты, имя отодвинутого исходника|None).

    Переиспользовать чужой датаблок с nodes.clear() нельзя: исходный материал
    стоял ещё на других объектах и молча получал атласные карты при UV 0..1,
    а Unpack его не восстанавливал.  Каноническое имя остаётся у АТЛАСНОГО
    материала (решение по сдаче), исходник уезжает в '<имя>.src'.

    atlas_folder — папка карт этого атласа.  Метка ATLAS_MAT_TAG живёт только
    в .blend (дефолтная сдача — FBX без Custom Properties — её теряет, как и
    любой файл до 2.8), поэтому непомеченный датаблок, все картинки которого
    лежат в папке АТЛАСА, — это наш же атласный материал: он переиспользуется
    и помечается заново, а не уезжает в '.src.001' вместе с объектом-носителем.
    """
    existing = bpy.data.materials.get(material_name)
    renamed = None
    if existing is not None:
        if existing.get(ATLAS_MAT_TAG):
            # наш же атласный материал с прошлого прогона — честно переиспользуем
            existing[ATLAS_MAT_TAG] = atlas_name
            return existing, None
        if atlas_folder and _images_all_in_folder(existing, atlas_folder):
            # метка потерялась (FBX/легаси), но карты — из папки этого атласа
            existing[ATLAS_MAT_TAG] = atlas_name
            return existing, None
        existing.name = material_name + SOURCE_MAT_SUFFIX
        renamed = existing.name
    material = bpy.data.materials.new(name=material_name)
    material[ATLAS_MAT_TAG] = atlas_name
    return material, renamed


def find_source_material(material_name):
    """Исходный (не атласный) датаблок, отодвинутый claim_atlas_material."""
    prefix = material_name + SOURCE_MAT_SUFFIX
    for mat in bpy.data.materials:
        if mat.name.startswith(prefix) and not mat.get(ATLAS_MAT_TAG):
            return mat
    return None


def material_wired_to_set(material, folder_path):
    """True, когда материал уже подключён именно к ЭТОМУ сету.

    Прежняя эвристика «есть хоть одна TEX_IMAGE-нода ⇒ настроен» оставляла на
    материале атласные карты (или карты чужого/устаревшего сета) при
    восстановленных UV 0..1 — грани сэмплили весь атлас."""
    if material.get(ATLAS_MAT_TAG):
        return False
    return _images_all_in_folder(material, folder_path)


def mark_stale_atlas_bins(base_path, name_prefix, kept_bins):
    """Пометить осиротевшие бины (i > kept_bins) устаревшими.

    Их atlas_mapping.json уезжает в atlas_mapping.stale.json: файлы никто не
    удаляет (могли уже уйти заказчику), но папка перестаёт предлагаться в
    «Apply Atlas» — применение СТАРОЙ раскладки растянуло бы UV по
    несуществующим регионам.  Возвращает имена помеченных папок."""
    stale = []
    idx = kept_bins + 1
    while True:
        folder = os.path.join(base_path, f"{name_prefix}{idx}")
        if not os.path.isdir(folder):
            break
        mapping = os.path.join(folder, 'atlas_mapping.json')
        if os.path.exists(mapping):
            try:
                os.replace(mapping, os.path.join(folder, 'atlas_mapping.stale.json'))
            except OSError as exc:
                print(f"  ⚠️ Не удалось пометить устаревшим {folder}: {exc}")
        stale.append(os.path.basename(folder))
        idx += 1
    return stale


# Короткие суффиксы LOW-схемы (без адреса объекта — для атласа из сетов)
_LOW_SHORT_SUFFIX = {
    'DIFFUSE': 'd',
    'DIFFUSE_OPACITY': 'do',
    'ROUGHNESS': 'r',
    'METALLIC': 'm',
    'OPACITY': 'o',
    'NORMAL': 'n',
    'EMIT': 'e',
    'ERM': 'erm',
}


def atlas_filename_fn(atlas_name, use_low_naming=False, address=None, obj_type=None,
                      index=1, low_short=False):
    """Резолвер имени файла карты — единственное, чем отличаются два пути
    композитинга (атлас из сетов и атлас из объекта)."""
    def name_for(texture_type):
        if low_short:
            return f"T_{atlas_name}_{_LOW_SHORT_SUFFIX[texture_type]}.png"
        return get_texture_filename(atlas_name, texture_type, use_low_naming,
                                    address, obj_type, index)
    return name_for


def get_texture_filename(atlas_name, texture_type, use_low_naming, address=None, obj_type=None, index=1):
    """
    Генерирует имя файла текстуры для атласа.
    index - номер атласа/материала в LOW схеме (T_..._d_1.png, T_..._d_2.png)
    """
    if use_low_naming and address and obj_type:
        # LOW naming: T_Address_ObjectType_d/r/m/o/n/e.png
        type_map = {
            'DIFFUSE': 'd',
            'DIFFUSE_OPACITY': 'do',
            'ROUGHNESS': 'r',
            'METALLIC': 'm',
            'OPACITY': 'o',
            'NORMAL': 'n',
            'EMIT': 'e',
            'ERM': 'erm'
        }
        suffix = type_map.get(texture_type, texture_type.lower())
        return f"T_{address}_{obj_type}_{suffix}_{index}.png"
    else:
        # HIGH naming: T_AtlasName_Diffuse/DiffuseOpacity/Emit/Roughness/Metallic/ERM/Normal.png
        type_map = {
            'DIFFUSE': 'Diffuse',
            'DIFFUSE_OPACITY': 'DiffuseOpacity',
            'EMIT': 'Emit',
            'ROUGHNESS': 'Roughness',
            'METALLIC': 'Metallic',
            'OPACITY': 'Opacity',
            'ERM': 'ERM',
            'NORMAL': 'Normal'
        }
        suffix = type_map.get(texture_type, texture_type)
        return f"T_{atlas_name}_{suffix}.png"


def check_sets_have_alpha(texture_sets):
    """
    Проверяет, есть ли альфа-канал в исходных текстурах сетов
    Returns: True если хотя бы один сет имеет альфа-канал
    """
    try:
        from PIL import Image
        
        for tex_set in texture_sets:
            # Проверяем DiffuseOpacity файл
            do_path = os.path.join(tex_set.folder_path, f"T_{tex_set.material_name}_DiffuseOpacity.png")
            if os.path.exists(do_path):
                try:
                    with Image.open(do_path) as img:
                        if img.mode in ('RGBA', 'LA'):
                            print(f"  ✓ Найден альфа-канал в {tex_set.name}")
                            return True
                except Exception:
                    pass

            # Проверяем Diffuse файл
            d_path = os.path.join(tex_set.folder_path, f"T_{tex_set.material_name}_Diffuse.png")
            if os.path.exists(d_path):
                try:
                    with Image.open(d_path) as img:
                        if img.mode in ('RGBA', 'LA'):
                            print(f"  ✓ Найден альфа-канал в {tex_set.name}")
                            return True
                except Exception:
                    pass
        
        print(f"  ℹ️ Альфа-канал не найден ни в одном сете")
        return False
        
    except ImportError:
        # Fallback: используем флаги из texture set
        for tex_set in texture_sets:
            if tex_set.has_diffuse_opacity or tex_set.has_opacity:
                return True
        return False


def pack_atlas_rectangles(texture_sets, atlas_size):
    """
    Упаковывает прямоугольники (текстуры) в атлас методом Guillotine
    """
    # Сортируем текстуры по убыванию размера для лучшей упаковки
    texture_sets = sorted(texture_sets, key=lambda x: x.resolution, reverse=True)

    layout = []
    # Список свободных прямоугольников
    free_rects = [{'x': 0, 'y': 0, 'width': atlas_size, 'height': atlas_size}]

    for tex_set in texture_sets:
        size = tex_set.resolution
        placed = False

        # Ищем наиболее подходящий свободный прямоугольник
        best_rect_idx = -1
        best_score = float('inf')
        best_fit = None

        for i, rect in enumerate(free_rects):
            if rect['width'] >= size and rect['height'] >= size:
                # Вычисляем "отходы"
                waste_width = rect['width'] - size
                waste_height = rect['height'] - size
                score = waste_width * waste_height

                if score < best_score:
                    best_score = score
                    best_rect_idx = i
                    best_fit = rect

        if best_rect_idx != -1:
            # Размещаем текстуру
            rect = best_fit
            x, y = rect['x'], rect['y']

            layout.append({
                'texture_set': tex_set,
                'x': x,
                'y': y,
                'width': size,
                'height': size,
                'u_min': x / atlas_size,
                'v_min': y / atlas_size,
                'u_max': (x + size) / atlas_size,
                'v_max': (y + size) / atlas_size
            })

            # Удаляем использованный прямоугольник
            del free_rects[best_rect_idx]

            # Создаем два новых свободных прямоугольника (Guillotine split)
            if rect['width'] > size:
                free_rects.append({
                    'x': x + size,
                    'y': y,
                    'width': rect['width'] - size,
                    'height': size
                })

            if rect['height'] > size:
                free_rects.append({
                    'x': x,
                    'y': y + size,
                    'width': rect['width'],
                    'height': rect['height'] - size
                })

            placed = True

        if not placed:
            return None  # Нет подходящего места

    return layout


def calculate_atlas_packing_layout(texture_sets, atlas_size):
    """
    Рассчитывает расположение текстур в атласе
    """
    total_area = sum(tex_set.resolution * tex_set.resolution for tex_set in texture_sets)
    atlas_area = atlas_size * atlas_size
    
    if total_area > atlas_area:
        raise Exception(f"Общая площадь текстур ({total_area}px²) превышает площадь атласа ({atlas_area}px²)")
    
    sorted_sets = sorted(texture_sets, key=lambda x: x.resolution * x.resolution, reverse=True)

    layout = pack_atlas_rectangles(sorted_sets, atlas_size)

    if not layout:
        raise Exception("Не удалось разместить все текстуры в атласе")

    return layout


def calculate_multi_atlas_packing(texture_sets, atlas_size):
    """
    Упаковывает сеты в НЕСКОЛЬКО атласов заданного размера (First-Fit-Decreasing).
    Returns: list of layouts (один layout на атлас)
    """
    for tex_set in texture_sets:
        if tex_set.resolution > atlas_size:
            raise Exception(
                f"Текстура {tex_set.name} ({tex_set.resolution}px) больше атласа {atlas_size}px"
            )

    remaining = sorted(texture_sets, key=lambda x: x.resolution, reverse=True)
    bin_layouts = []

    while remaining:
        # Greedily grow the current bin: keep the trial layout of the largest
        # subset that still packs into one atlas
        placed_sets = []
        layout = None
        for tex_set in remaining:
            trial_layout = pack_atlas_rectangles(placed_sets + [tex_set], atlas_size)
            if trial_layout is not None:
                placed_sets.append(tex_set)
                layout = trial_layout

        if not placed_sets:
            raise Exception("Не удалось разместить текстуры в атласе")

        bin_layouts.append(layout)
        placed_ids = {id(ts) for ts in placed_sets}
        remaining = [ts for ts in remaining if id(ts) not in placed_ids]

    return bin_layouts



# ===== PREVIEW ATLAS LAYOUT OPERATOR =====

class AGR_OT_PreviewAtlasLayout(Operator):
    """Preview atlas packing layout for selected texture sets"""
    bl_idname = "agr.preview_atlas_layout"
    bl_label = "Preview Atlas Layout"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        if not any(ts.is_selected and not ts.is_atlas for ts in context.scene.agr_texture_sets):
            cls.poll_message_set("Отметьте текстурные сеты галочками в списке")
            return False
        return True

    def execute(self, context):
        settings = context.scene.agr_baker_settings
        texture_sets_list = context.scene.agr_texture_sets
        
        # Получаем выбранные сеты
        selected_sets = [tex_set for tex_set in texture_sets_list if tex_set.is_selected and not tex_set.is_atlas]
        
        if len(selected_sets) == 0:
            self.report({'WARNING'}, "Не выбрано ни одного набора текстур")
            return {'CANCELLED'}
        
        atlas_size = int(settings.atlas_size)
        
        # Проверяем, можно ли упаковать
        total_area = sum(s.resolution * s.resolution for s in selected_sets)
        if total_area > atlas_size * atlas_size:
            self.report({'ERROR'}, f"Текстуры не помещаются в атлас {atlas_size}x{atlas_size}")
            return {'CANCELLED'}
        
        try:
            # Рассчитываем упаковку
            layout = calculate_atlas_packing_layout(selected_sets, atlas_size)
            
            if not layout:
                self.report({'ERROR'}, "Не удалось рассчитать упаковку")
                return {'CANCELLED'}
            
            # Создаем превью изображение
            preview_image = self.create_preview_image(layout, atlas_size)
            
            if preview_image:
                # Показываем в Image Editor
                self.show_preview_in_editor(context, preview_image)
                self.report({'INFO'}, f"Предпросмотр: {len(layout)} текстур в атласе {atlas_size}x{atlas_size}")
                return {'FINISHED'}
            else:
                self.report({'ERROR'}, "Не удалось создать превью")
                return {'CANCELLED'}
                
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка: {str(e)}")
            print(f"❌ Ошибка предпросмотра: {e}")
            import traceback
            traceback.print_exc()
            return {'CANCELLED'}
    
    def create_preview_image(self, layout, atlas_size):
        """Создает превью изображение с раскладкой"""
        import random
        
        preview_name = "Atlas_Preview"
        
        # Удаляем старое превью если есть
        if preview_name in bpy.data.images:
            bpy.data.images.remove(bpy.data.images[preview_name])
        
        # Создаем новое изображение
        preview_image = bpy.data.images.new(
            preview_name,
            width=atlas_size,
            height=atlas_size,
            alpha=False,
            float_buffer=False
        )
        
        # Создаем numpy массив напрямую (без GPU roundtrip)
        preview_array = np.zeros((atlas_size, atlas_size, 4), dtype=np.float32)
        preview_array[:, :, 3] = 1.0  # alpha = 1.0

        # Рисуем прямоугольники для каждой текстуры
        for item in layout:
            x = item['x']
            y = item['y']
            w = item['width']
            h = item['height']

            # Генерируем случайный цвет для каждой текстуры
            color = [random.random(), random.random(), random.random(), 1.0]

            # Заполняем область
            preview_array[y:y+h, x:x+w, :] = color

            # Рисуем границу (белая рамка 2px)
            border_width = max(2, atlas_size // 512)
            preview_array[y:y+border_width, x:x+w, :] = [1.0, 1.0, 1.0, 1.0]  # Верх
            preview_array[y+h-border_width:y+h, x:x+w, :] = [1.0, 1.0, 1.0, 1.0]  # Низ
            preview_array[y:y+h, x:x+border_width, :] = [1.0, 1.0, 1.0, 1.0]  # Лево
            preview_array[y:y+h, x+w-border_width:x+w, :] = [1.0, 1.0, 1.0, 1.0]  # Право

        # Записываем массив в изображение
        preview_image.pixels.foreach_set(preview_array.ravel())
        preview_image.update()

        print(f"✅ Создано превью: {len(layout)} текстур")
        
        return preview_image
    
    def show_preview_in_editor(self, context, image):
        """Показывает изображение в Image Editor"""
        for area in context.screen.areas:
            if area.type == 'IMAGE_EDITOR':
                for space in area.spaces:
                    if space.type == 'IMAGE_EDITOR':
                        space.image = image
                        space.use_image_pin = True
                        break
                area.tag_redraw()
                print(f"📷 Предпросмотр отображен в Image Editor")
                return
        
        print(f"⚠️ Image Editor не найден, изображение загружено в Data")


# ===== PREVIEW ATLAS LAYOUT FROM OBJECT OPERATOR =====

class AGR_OT_PreviewAtlasLayoutFromObject(Operator):
    """Preview atlas packing layout for active object materials"""
    bl_idname = "agr.preview_atlas_layout_from_object"
    bl_label = "Preview Atlas Layout from Object"
    bl_options = {'REGISTER'}
    
    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not (obj and obj.type == 'MESH' and len(obj.material_slots) > 0):
            cls.poll_message_set("Нужен активный MESH-объект с материалами")
            return False
        return True
    
    def execute(self, context):
        settings = context.scene.agr_baker_settings
        texture_sets_list = context.scene.agr_texture_sets
        obj = context.active_object
        
        # Собираем все материалы объекта
        # Без дублей: один материал может занимать несколько слотов,
        # а в атласе ему нужна ровно одна ячейка
        material_names = []
        for slot in obj.material_slots:
            if not slot.material:
                continue
            # сет ищется по КАНОНИЧЕСКОМУ имени: материал мог быть отодвинут
            # в '<имя>.src' атласом соседнего объекта
            name = source_material_name(slot.material.name)
            if name not in material_names:
                material_names.append(name)
        
        if not material_names:
            self.report({'WARNING'}, "У объекта нет материалов")
            return {'CANCELLED'}
        
        # Ищем соответствующие texture sets
        object_sets = []
        missing_materials = []
        
        for mat_name in material_names:
            found = False
            for tex_set in texture_sets_list:
                if tex_set.material_name == mat_name and not tex_set.is_atlas:
                    object_sets.append(tex_set)
                    found = True
                    break
            
            if not found:
                missing_materials.append(mat_name)
        
        if missing_materials:
            self.report({'WARNING'}, f"Не найдены texture sets для материалов: {', '.join(missing_materials)}")
        
        if not object_sets:
            self.report({'WARNING'}, "Не найдено ни одного texture set для материалов объекта")
            return {'CANCELLED'}
        
        atlas_size = int(settings.atlas_size)
        
        # Проверяем, можно ли упаковать
        total_area = sum(s.resolution * s.resolution for s in object_sets)
        if total_area > atlas_size * atlas_size:
            self.report({'ERROR'}, f"Текстуры не помещаются в атлас {atlas_size}x{atlas_size}")
            return {'CANCELLED'}
        
        try:
            # Рассчитываем упаковку
            layout = calculate_atlas_packing_layout(object_sets, atlas_size)
            
            if not layout:
                self.report({'ERROR'}, "Не удалось рассчитать упаковку")
                return {'CANCELLED'}
            
            # Создаем превью изображение
            preview_image = self.create_preview_image(layout, atlas_size, obj.name)
            
            if preview_image:
                # Показываем в Image Editor
                self.show_preview_in_editor(context, preview_image)
                self.report({'INFO'}, f"Предпросмотр для {obj.name}: {len(layout)} текстур в атласе {atlas_size}x{atlas_size}")
                return {'FINISHED'}
            else:
                self.report({'ERROR'}, "Не удалось создать превью")
                return {'CANCELLED'}
                
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка: {str(e)}")
            print(f"❌ Ошибка предпросмотра: {e}")
            import traceback
            traceback.print_exc()
            return {'CANCELLED'}
    
    def create_preview_image(self, layout, atlas_size, obj_name):
        """Создает превью изображение с раскладкой"""
        import random
        
        preview_name = f"Atlas_Preview_{obj_name}"
        
        # Удаляем старое превью если есть
        if preview_name in bpy.data.images:
            bpy.data.images.remove(bpy.data.images[preview_name])
        
        # Создаем новое изображение
        preview_image = bpy.data.images.new(
            preview_name,
            width=atlas_size,
            height=atlas_size,
            alpha=False,
            float_buffer=False
        )
        
        # Создаем numpy массив напрямую (без GPU roundtrip)
        preview_array = np.zeros((atlas_size, atlas_size, 4), dtype=np.float32)
        preview_array[:, :, 3] = 1.0  # alpha = 1.0

        # Рисуем прямоугольники для каждой текстуры
        for item in layout:
            x = item['x']
            y = item['y']
            w = item['width']
            h = item['height']

            # Генерируем случайный цвет для каждой текстуры
            color = [random.random(), random.random(), random.random(), 1.0]

            # Заполняем область
            preview_array[y:y+h, x:x+w, :] = color

            # Рисуем границу (белая рамка 2px)
            border_width = max(2, atlas_size // 512)
            preview_array[y:y+border_width, x:x+w, :] = [1.0, 1.0, 1.0, 1.0]  # Верх
            preview_array[y+h-border_width:y+h, x:x+w, :] = [1.0, 1.0, 1.0, 1.0]  # Низ
            preview_array[y:y+h, x:x+border_width, :] = [1.0, 1.0, 1.0, 1.0]  # Лево
            preview_array[y:y+h, x+w-border_width:x+w, :] = [1.0, 1.0, 1.0, 1.0]  # Право

        # Записываем массив в изображение
        preview_image.pixels.foreach_set(preview_array.ravel())
        preview_image.update()

        print(f"✅ Создано превью для {obj_name}: {len(layout)} текстур")
        
        return preview_image
    
    def show_preview_in_editor(self, context, image):
        """Показывает изображение в Image Editor"""
        for area in context.screen.areas:
            if area.type == 'IMAGE_EDITOR':
                for space in area.spaces:
                    if space.type == 'IMAGE_EDITOR':
                        space.image = image
                        space.use_image_pin = True
                        break
                area.tag_redraw()
                print(f"📷 Предпросмотр отображен в Image Editor")
                return
        
        print(f"⚠️ Image Editor не найден, изображение загружено в Data")


# ===== CREATE ATLAS ONLY OPERATOR =====

# ===== SHARED ATLAS COMPOSITING =====

class AtlasCompositingMixin:
    """Единственная реализация композитинга атласа.

    Раньше эти методы жили ДВУМЯ дословными копиями (атлас из сетов и атлас
    из объекта), и фиксы приземлялись в одну: DO-first ветка попала только в
    объектную, из-за чего «Create Atlas Only» терял альфу и чернил цвет
    прозрачных текселей.  Пути отличаются ровно резолвером имён файлов
    (`name_for`), он и передаётся аргументом."""

    def _note_missing_map(self, texture_set_name, texture_type):
        """Отсутствующий файл карты — не только строка в консоли: чёрная
        ячейка в атласе выглядела как успешная сборка."""
        self._missing_maps = getattr(self, '_missing_maps', [])
        note = f"{texture_set_name}: {texture_type}"
        if note not in self._missing_maps:
            self._missing_maps.append(note)

    def create_erm_atlas(self, texture_sets, atlas_size, layout):
        """Создает ERM атлас (объединяет E, R, M в RGB каналы)"""
        from PIL import Image

        atlas_name = f"Atlas_ERM_{atlas_size}"

        if atlas_name in bpy.data.images:
            bpy.data.images.remove(bpy.data.images[atlas_name])

        atlas_image = bpy.data.images.new(
            atlas_name,
            width=atlas_size,
            height=atlas_size,
            alpha=False,
            float_buffer=False
        )
        atlas_image.colorspace_settings.name = 'Non-Color'

        # Создаем numpy массив напрямую (без GPU roundtrip)
        atlas_array = np.zeros((atlas_size, atlas_size, 4), dtype=np.float32)
        atlas_array[:, :, 3] = 1.0

        for item in layout:
            emit_path = self.get_texture_path(item['texture_set'], 'EMIT')
            roughness_path = self.get_texture_path(item['texture_set'], 'ROUGHNESS')
            metallic_path = self.get_texture_path(item['texture_set'], 'METALLIC')

            cell_width = item['width']
            cell_height = item['height']
            x = item['x']
            y = item['y']

            e_channel = None
            r_channel = None
            m_channel = None

            if emit_path and os.path.exists(emit_path):
                with Image.open(emit_path) as raw:
                    e_img = raw.convert('L')
                    if e_img.size != (cell_width, cell_height):
                        e_img = e_img.resize((cell_width, cell_height), Image.Resampling.LANCZOS)
                    # Flip vertically: Pillow is top-to-bottom, Blender pixels are bottom-to-top
                    e_channel = np.flipud(np.array(e_img, dtype=np.float32) / 255.0)

            if roughness_path and os.path.exists(roughness_path):
                with Image.open(roughness_path) as raw:
                    r_img = raw.convert('L')
                    if r_img.size != (cell_width, cell_height):
                        r_img = r_img.resize((cell_width, cell_height), Image.Resampling.LANCZOS)
                    r_channel = np.flipud(np.array(r_img, dtype=np.float32) / 255.0)

            if metallic_path and os.path.exists(metallic_path):
                with Image.open(metallic_path) as raw:
                    m_img = raw.convert('L')
                    if m_img.size != (cell_width, cell_height):
                        m_img = m_img.resize((cell_width, cell_height), Image.Resampling.LANCZOS)
                    m_channel = np.flipud(np.array(m_img, dtype=np.float32) / 255.0)

            # Fallback: unpack missing channels from the packed ERM file —
            # standard HIGH sets ship only DiffuseOpacity+ERM+Normal, and
            # without this the atlas silently loses Emission/Metallic.
            if e_channel is None or r_channel is None or m_channel is None:
                erm_path = self.get_texture_path(item['texture_set'], 'ERM')
                if erm_path and os.path.exists(erm_path):
                    with Image.open(erm_path) as raw:
                        erm_img = raw.convert('RGB')
                        if erm_img.size != (cell_width, cell_height):
                            erm_img = erm_img.resize((cell_width, cell_height), Image.Resampling.LANCZOS)
                        erm_arr = np.flipud(np.array(erm_img, dtype=np.float32) / 255.0)
                    if e_channel is None:
                        e_channel = erm_arr[:, :, 0]
                    if r_channel is None:
                        r_channel = erm_arr[:, :, 1]
                    if m_channel is None:
                        m_channel = erm_arr[:, :, 2]

            if e_channel is None:
                e_channel = np.zeros((cell_height, cell_width), dtype=np.float32)
            if r_channel is None:
                r_channel = np.ones((cell_height, cell_width), dtype=np.float32) * 0.5
            if m_channel is None:
                m_channel = np.zeros((cell_height, cell_width), dtype=np.float32)

            atlas_array[y:y+cell_height, x:x+cell_width, 0] = e_channel
            atlas_array[y:y+cell_height, x:x+cell_width, 1] = r_channel
            atlas_array[y:y+cell_height, x:x+cell_width, 2] = m_channel
            atlas_array[y:y+cell_height, x:x+cell_width, 3] = 1.0

            print(f"  📍 Размещена ERM: {item['texture_set'].name} в ({x}, {y})")

        atlas_image.pixels.foreach_set(atlas_array.ravel())
        atlas_image.update()

        return atlas_image

    def create_atlas_for_type(self, texture_sets, texture_type, atlas_size, layout, with_alpha=False):
        """Создает атлас для конкретного типа текстуры"""
        atlas_name = f"Atlas_{texture_type}_{atlas_size}"

        if atlas_name in bpy.data.images:
            bpy.data.images.remove(bpy.data.images[atlas_name])

        atlas_image = bpy.data.images.new(
            atlas_name,
            width=atlas_size,
            height=atlas_size,
            alpha=with_alpha,
            float_buffer=False
        )

        if texture_type in ['DIFFUSE', 'DIFFUSE_OPACITY']:
            atlas_image.colorspace_settings.name = 'sRGB'
        else:
            atlas_image.colorspace_settings.name = 'Non-Color'

        # Создаем numpy массив напрямую (без GPU roundtrip)
        atlas_array = np.zeros((atlas_size, atlas_size, 4), dtype=np.float32)
        if not with_alpha:
            atlas_array[:, :, 3] = 1.0

        # Размещаем текстуры в атласе
        for item in layout:
            texture_path = self.get_texture_path(item['texture_set'], texture_type)

            if texture_path and os.path.exists(texture_path):
                self.place_texture_in_atlas(atlas_array, texture_path, item)
            else:
                x, y = item['x'], item['y']
                if texture_type == 'OPACITY':
                    # Missing Opacity means fully opaque — a black region
                    # would turn the whole set transparent in the DO atlas
                    atlas_array[y:y+item['height'], x:x+item['width'], 0:3] = 1.0
                elif texture_type == 'NORMAL':
                    # Чёрный регион — невалидная нормаль; плоская (128,128,255)
                    atlas_array[y:y+item['height'], x:x+item['width'], 0] = 0.5
                    atlas_array[y:y+item['height'], x:x+item['width'], 1] = 0.5
                    atlas_array[y:y+item['height'], x:x+item['width'], 2] = 1.0
                    self._note_missing_map(item['texture_set'].name, texture_type)
                elif texture_type in ('DIFFUSE', 'DIFFUSE_OPACITY'):
                    # Цветовая карта отсутствует — ячейка будет чёрной,
                    # это обязано попасть в отчёт оператора
                    self._note_missing_map(item['texture_set'].name, texture_type)
                print(f"  ⚠️ Текстура не найдена: {texture_type} для {item['texture_set'].name}")

        atlas_image.pixels.foreach_set(atlas_array.ravel())
        atlas_image.update()

        return atlas_image

    def get_texture_path(self, texture_set, texture_type):
        """Получает путь к файлу текстуры заданного типа"""
        material_name = texture_set.material_name
        folder_path = texture_set.folder_path

        # Маппинг типов текстур на имена файлов
        texture_file_map = {
            'DIFFUSE': f"T_{material_name}_Diffuse.png",
            'DIFFUSE_OPACITY': f"T_{material_name}_DiffuseOpacity.png",
            'NORMAL': f"T_{material_name}_Normal.png",
            'METALLIC': f"T_{material_name}_Metallic.png",
            'ROUGHNESS': f"T_{material_name}_Roughness.png",
            'OPACITY': f"T_{material_name}_Opacity.png",
            'ERM': f"T_{material_name}_ERM.png",
            'EMIT': f"T_{material_name}_Emit.png",
        }

        filename = texture_file_map.get(texture_type)
        if filename:
            filepath = os.path.join(folder_path, filename)
            if os.path.exists(filepath):
                return filepath

        # Diffuse and DiffuseOpacity are interchangeable colour sources:
        # HIGH sets may ship only DiffuseOpacity, LOW sets only Diffuse —
        # never fall through to a black region when the paired map exists.
        paired = {'DIFFUSE': 'DIFFUSE_OPACITY', 'DIFFUSE_OPACITY': 'DIFFUSE'}.get(texture_type)
        if paired:
            filepath = os.path.join(folder_path, texture_file_map[paired])
            if os.path.exists(filepath):
                return filepath

        return None

    def place_texture_in_atlas(self, atlas_array, texture_path, layout_item):
        """Размещает текстуру в атласе с масштабированием при необходимости"""
        try:
            cell_width = layout_item['width']
            cell_height = layout_item['height']

            # Используем Pillow для качественного масштабирования
            if pillow_available():
                from PIL import Image
                with Image.open(texture_path) as raw_img:
                    if raw_img.size != (cell_width, cell_height):
                        pil_img = raw_img.resize((cell_width, cell_height), Image.Resampling.LANCZOS)
                    else:
                        pil_img = raw_img
                    if pil_img.mode != 'RGBA':
                        pil_img = pil_img.convert('RGBA')
                    # Flip vertically: Pillow is top-to-bottom, Blender pixels are bottom-to-top
                    tex_array = np.flipud(np.array(pil_img, dtype=np.float32) / 255.0)

            else:
                # Fallback: загружаем через Blender (already bottom-to-top)
                temp_img = bpy.data.images.load(texture_path)
                temp_img.update()
                _ = temp_img.pixels[0]

                tex_width = temp_img.size[0]
                tex_height = temp_img.size[1]
                tex_array = np.empty(tex_width * tex_height * 4, dtype=np.float32)
                temp_img.pixels.foreach_get(tex_array)
                tex_array = tex_array.reshape(tex_height, tex_width, 4)

                # Простое масштабирование через numpy
                if tex_width != cell_width or tex_height != cell_height:
                    indices_y = np.round(np.linspace(0, tex_height - 1, cell_height)).astype(int)
                    indices_x = np.round(np.linspace(0, tex_width - 1, cell_width)).astype(int)
                    tex_array = tex_array[np.ix_(indices_y, indices_x)]

                # Удаляем временное изображение
                if temp_img.name in bpy.data.images:
                    bpy.data.images.remove(temp_img)

            # Размещаем в атласе
            x = layout_item['x']
            y = layout_item['y']
            atlas_array[y:y+cell_height, x:x+cell_width, :] = tex_array

        except Exception as e:
            # Accumulate for the operator's final report — a swallowed error
            # here means a black hole in the atlas on a "successful" run
            self._place_errors = getattr(self, '_place_errors', [])
            self._place_errors.append(f"{os.path.basename(texture_path)}: {e}")
            print(f"  ❌ Ошибка размещения {texture_path}: {e}")

    def save_atlas_image(self, image, filepath, texture_type):
        """Сохраняет изображение атласа.  Returns True при успехе.

        Провал записи (сетевая папка, файл занят) обязан быть виден: путь к
        ненаписанному файлу раньше всё равно попадал в created_atlases, и
        запись атласа/JSON ссылались на несуществующий файл."""
        scene = bpy.context.scene
        img_settings = scene.render.image_settings

        # Сохраняем оригинальные настройки
        original_format = img_settings.file_format
        original_color_mode = img_settings.color_mode
        original_color_depth = img_settings.color_depth
        original_compression = img_settings.compression
        original_view_settings = scene.view_settings.view_transform
        original_look = scene.view_settings.look
        original_display_device = scene.display_settings.display_device

        # Устанавливаем настройки для сохранения
        img_settings.file_format = 'PNG'
        img_settings.color_depth = '8'
        img_settings.compression = 15
        scene.view_settings.view_transform = 'Standard'
        scene.view_settings.look = 'None'
        scene.display_settings.display_device = 'sRGB'

        # Определяем режим цвета
        if texture_type == 'DIFFUSE_OPACITY':
            img_settings.color_mode = 'RGBA'
        else:
            img_settings.color_mode = 'RGB'

        try:
            image.filepath_raw = filepath
            image.save_render(filepath)
            print(f"  💾 Сохранен: {os.path.basename(filepath)}")
            return True
        except Exception as e:
            self._save_errors = getattr(self, '_save_errors', [])
            self._save_errors.append(f"{os.path.basename(filepath)}: {e}")
            print(f"  ❌ Ошибка сохранения {filepath}: {e}")
            return False
        finally:
            # Восстанавливаем настройки (compression тоже — он оставался 15)
            img_settings.file_format = original_format
            img_settings.color_mode = original_color_mode
            img_settings.color_depth = original_color_depth
            img_settings.compression = original_compression
            scene.view_settings.view_transform = original_view_settings
            scene.view_settings.look = original_look
            scene.display_settings.display_device = original_display_device

    # ----- высокоуровневая сборка карт -----

    def _write_atlas_map(self, texture_sets, texture_type, atlas_size, layout,
                         output_path, name_for, save_as=None, with_alpha=False):
        """Собрать → сохранить → ВСЕГДА освободить временный Atlas_*-датаблок
        (16 МБ на 2K каждый; раньше remove вызывался только в успешной ветке).
        Возвращает путь только при реально записанном файле."""
        image = self.create_atlas_for_type(texture_sets, texture_type, atlas_size,
                                           layout, with_alpha)
        if image is None:
            return None
        filepath = os.path.join(output_path, name_for(texture_type))
        try:
            saved = self.save_atlas_image(image, filepath, save_as or texture_type)
        finally:
            bpy.data.images.remove(image)
        return filepath if saved else None

    def _write_erm_maps(self, texture_sets, atlas_size, layout, output_path,
                        name_for, created):
        """ERM + разложенные E/R/M из ОДНОЙ сборки.

        Отдельные атласы по типам читают только T_*_Emit/Roughness/Metallic.png
        и оставляли ЧЁРНЫЙ регион для стандартного HIGH-сета, который несёт
        лишь упакованный ERM — create_erm_atlas этот фолбэк уже умеет."""
        from PIL import Image

        image = self.create_erm_atlas(texture_sets, atlas_size, layout)
        if image is None:
            return
        erm_path = os.path.join(output_path, name_for('ERM'))
        try:
            saved = self.save_atlas_image(image, erm_path, 'ERM')
        finally:
            bpy.data.images.remove(image)
        if not saved:
            return
        created['ERM'] = erm_path

        with Image.open(erm_path) as raw:
            erm_img = raw.convert('RGB')
        try:
            for key, channel in (('EMIT', 0), ('ROUGHNESS', 1), ('METALLIC', 2)):
                path = os.path.join(output_path, name_for(key))
                # RGB, а не одноканальный 'L': остальные T_*-карты сдачи —
                # трёхканальные PNG, и менять формат файлов без прогона
                # чекером сдачи нельзя (getchannel отдаёт режим 'L')
                ch = erm_img.getchannel(channel).convert('RGB')
                try:
                    ch.save(path)
                finally:
                    ch.close()
                created[key] = path
                print(f"  ✅ Создан: {os.path.basename(path)}")
        finally:
            erm_img.close()

    def _write_do_maps(self, texture_sets, atlas_size, layout, output_path,
                       name_for, has_alpha, created):
        """DO-first: DiffuseOpacity — ГЛАВНАЯ цветовая карта, D и O выводятся
        из неё.  Сборка D с последующей вклейкой Opacity-атласа обнуляла цвет
        прозрачных текселей (RGB-сохранение сбрасывает альфу) и теряла альфу
        целиком у сетов, которые несут её только внутри DiffuseOpacity."""
        from PIL import Image

        do_filepath = self._write_atlas_map(
            texture_sets, 'DIFFUSE_OPACITY', atlas_size, layout, output_path, name_for,
            save_as='DIFFUSE_OPACITY' if has_alpha else 'DIFFUSE', with_alpha=has_alpha)
        if not do_filepath:
            return
        created['DIFFUSE_OPACITY'] = do_filepath
        print(f"  ✅ Создан DO{' с альфа' if has_alpha else ' без альфа'}: {os.path.basename(do_filepath)}")

        with Image.open(do_filepath) as do_img:
            d_filepath = os.path.join(output_path, name_for('DIFFUSE'))
            d_img = do_img.convert('RGB')
            try:
                d_img.save(d_filepath)
            finally:
                d_img.close()
            created['DIFFUSE'] = d_filepath
            print(f"  ✅ Создан Diffuse: {os.path.basename(d_filepath)}")

            if has_alpha and do_img.mode in ('RGBA', 'LA'):
                o_filepath = os.path.join(output_path, name_for('OPACITY'))
                # тот же контракт формата, что у E/R/M: RGB PNG (см. выше)
                o_channel = do_img.split()[-1].convert('RGB')
                try:
                    o_channel.save(o_filepath)
                finally:
                    o_channel.close()
                created['OPACITY'] = o_filepath
                print(f"  ✅ Создан Opacity: {os.path.basename(o_filepath)}")

    def build_atlas_textures(self, texture_sets, atlas_size, layout, output_path,
                             name_for, has_alpha):
        """Все карты атласа одним проходом; HIGH и LOW отличаются только
        именами файлов (`name_for`) и подключением материала."""
        created_atlases = {}

        print(f"\n🖼️ Создание DO/D/O атласов")
        self._write_do_maps(texture_sets, atlas_size, layout, output_path,
                            name_for, has_alpha, created_atlases)

        print(f"\n🖼️ Создание ERM и каналов E/R/M")
        self._write_erm_maps(texture_sets, atlas_size, layout, output_path,
                             name_for, created_atlases)

        if 'OPACITY' not in created_atlases:
            print(f"\n🖼️ Создание Opacity атласа")
            path = self._write_atlas_map(texture_sets, 'OPACITY', atlas_size, layout,
                                         output_path, name_for)
            if path:
                created_atlases['OPACITY'] = path

        print(f"\n🖼️ Создание Normal атласа")
        path = self._write_atlas_map(texture_sets, 'NORMAL', atlas_size, layout,
                                     output_path, name_for)
        if path:
            created_atlases['NORMAL'] = path

        return created_atlases

    def compositing_notes(self):
        """Строки для отчёта оператора: битые/отсутствующие карты и провалы
        записи — иначе чёрная ячейка уезжает в сдачу под зелёным INFO."""
        notes = []
        place_errors = getattr(self, '_place_errors', None)
        if place_errors:
            notes.append(f"не разместились текстуры ({len(place_errors)}): {'; '.join(place_errors[:3])}")
        save_errors = getattr(self, '_save_errors', None)
        if save_errors:
            notes.append(f"не записаны файлы ({len(save_errors)}): {'; '.join(save_errors[:3])}")
        missing = getattr(self, '_missing_maps', None)
        if missing:
            notes.append(f"нет исходных карт ({len(missing)}): {'; '.join(missing[:3])}")
        return notes

    def reset_compositing_notes(self):
        self._place_errors = []
        self._save_errors = []
        self._missing_maps = []


# ===== CREATE ATLAS ONLY OPERATOR =====

class AGR_OT_CreateAtlasOnly(AtlasCompositingMixin, Operator):
    """Create texture atlas from selected texture sets (no UV layout, no material assignment)"""
    bl_idname = "agr.create_atlas_only"
    bl_label = "Create Atlas Only"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not any(ts.is_selected and not ts.is_atlas for ts in context.scene.agr_texture_sets):
            cls.poll_message_set("Отметьте текстурные сеты галочками в списке")
            return False
        return True

    atlas_type: EnumProperty(
        name="Atlas Type",
        description="Type of atlas to create",
        items=[
            ('HIGH', "HIGH", "HIGH atlas with DO/ERM/N textures"),
            ('LOW', "LOW", "LOW atlas with d/r/m/o/n separate textures"),
        ],
        default='HIGH'
    )
    
    def execute(self, context):
        settings = context.scene.agr_baker_settings
        texture_sets_list = context.scene.agr_texture_sets
        
        # Получаем выбранные сеты
        selected_sets = [tex_set for tex_set in texture_sets_list if tex_set.is_selected and not tex_set.is_atlas]
        
        if len(selected_sets) == 0:
            self.report({'WARNING'}, "Не выбрано ни одного набора текстур")
            return {'CANCELLED'}
        
        atlas_size = int(settings.atlas_size)
        
        # Проверяем, можно ли упаковать
        total_area = sum(s.resolution * s.resolution for s in selected_sets)
        if total_area > atlas_size * atlas_size:
            self.report({'ERROR'}, f"Текстуры не помещаются в атлас {atlas_size}x{atlas_size}")
            return {'CANCELLED'}
        
        # Используем выбранный тип атласа
        final_atlas_type = self.atlas_type
        
        print(f"\n{'='*60}")
        print(f"🎨 СОЗДАНИЕ АТЛАСА (ТОЛЬКО ТЕКСТУРЫ)")
        print(f"{'='*60}")
        print(f"Тип атласа: {final_atlas_type}")
        print(f"Размер атласа: {atlas_size}x{atlas_size}")
        print(f"Количество наборов: {len(selected_sets)}")
        
        try:
            # Создаем атлас БЕЗ применения к объекту
            self.reset_compositing_notes()
            result = self.create_atlas_textures_only(context, selected_sets, atlas_size, final_atlas_type)

            if result:
                notes = self.compositing_notes()
                if notes:
                    agr_report(self, 'WARNING',
                               f"Атлас создан: {result['atlas_name']}, но " + "; ".join(notes))
                else:
                    agr_report(self, 'INFO', f"Атлас создан: {result['atlas_name']}")

                # Обновляем список сетов
                bpy.ops.agr.refresh_texture_sets(skip_alpha_strip=True)
                
                return {'FINISHED'}
            else:
                self.report({'ERROR'}, "Не удалось создать атлас")
                return {'CANCELLED'}
                
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка создания атласа: {str(e)}")
            print(f"❌ Ошибка: {e}")
            import traceback
            traceback.print_exc()
            return {'CANCELLED'}
    
    def generate_procedural_atlas_name(self, context, base_output_path=None):
        """Генерирует процедурное имя атласа A_001, A_002, etc.
        base_output_path — папка, куда будет записан атлас (в ней же ищется
        свободный номер); по умолчанию — папка первого сета сцены."""
        settings = context.scene.agr_baker_settings

        if not base_output_path:
            if context.scene.agr_texture_sets:
                first_set = context.scene.agr_texture_sets[0]
                base_output_path = os.path.dirname(first_set.folder_path)
            else:
                blend_file_path = bpy.path.abspath("//")
                base_output_path = os.path.join(blend_file_path, settings.output_folder)

        # Ищем существующие атласы с именами A_###
        existing_numbers = []
        if os.path.exists(base_output_path):
            for folder_name in os.listdir(base_output_path):
                folder_path = os.path.join(base_output_path, folder_name)
                if os.path.isdir(folder_path) and folder_name.startswith('A_'):
                    # Пытаемся извлечь номер
                    suffix = folder_name[2:]  # Убираем "A_"
                    if suffix.isdigit():
                        existing_numbers.append(int(suffix))
        
        # Находим следующий доступный номер
        if existing_numbers:
            next_number = max(existing_numbers) + 1
        else:
            next_number = 1
        
        # Форматируем с ведущими нулями (001, 002, etc.)
        atlas_name = f"A_{next_number:03d}"
        
        return atlas_name
    
    def create_atlas_textures_only(self, context, texture_sets, atlas_size, atlas_type):
        """Создает только текстуры атласа без применения к объекту"""
        settings = context.scene.agr_baker_settings
        
        # Проверяем наличие альфа-канала в исходных сетах
        has_alpha = check_sets_have_alpha(texture_sets)

        # Определяем путь для сохранения — та же папка, в которой ищется
        # свободный номер A_### (иначе возможна коллизия имён)
        if texture_sets:
            base_output_path = os.path.dirname(texture_sets[0].folder_path)
        else:
            blend_file_path = bpy.path.abspath("//")
            base_output_path = os.path.join(blend_file_path, settings.output_folder)

        # Получаем именование - процедурное A_001, A_002, etc.
        atlas_name = self.generate_procedural_atlas_name(context, base_output_path)

        print(f"📝 Имя атласа: {atlas_name}")
        print(f"📝 Альфа-канал: {'Да' if has_alpha else 'Нет'}")
        
        # Создаем папку для атласа
        atlas_output_path = os.path.join(base_output_path, atlas_name)
        if not os.path.exists(atlas_output_path):
            os.makedirs(atlas_output_path)
            print(f"📁 Создана папка: {atlas_output_path}")
        
        # Рассчитываем упаковку
        layout = calculate_atlas_packing_layout(texture_sets, atlas_size)
        
        if not layout:
            raise Exception("Не удалось рассчитать упаковку текстур")
        
        print(f"✅ Упаковка рассчитана: {len(layout)} текстур")
        
        # Создаем атласы для каждого типа текстуры
        created_atlases = {}
        
        if atlas_type == 'HIGH':
            # HIGH: создаем отдельные карты
            created_atlases = self.create_high_atlas_textures(
                texture_sets, atlas_size, layout, atlas_output_path, atlas_name, has_alpha
            )
        else:  # LOW
            # LOW: создаем ERM и дублируем D как DO
            created_atlases = self.create_low_atlas_textures(
                texture_sets, atlas_size, layout, atlas_output_path, atlas_name, has_alpha
            )
        
        # Сохраняем atlas_mapping.json
        self.save_atlas_mapping(atlas_output_path, atlas_name, atlas_type, atlas_size, layout, created_atlases)
        
        print(f"\n✅ Атлас успешно создан!")
        print(f"{'='*60}\n")
        
        return {
            'atlas_name': atlas_name,
            'output_path': atlas_output_path,
            'atlases': created_atlases
        }
    
    def create_high_atlas_textures(self, texture_sets, atlas_size, layout, output_path, atlas_name, has_alpha):
        """HIGH-карты атласа из сетов (T_{atlas}_DiffuseOpacity.png и т.д.)"""
        name_for = atlas_filename_fn(atlas_name)
        return self.build_atlas_textures(texture_sets, atlas_size, layout,
                                         output_path, name_for, has_alpha)

    def create_low_atlas_textures(self, texture_sets, atlas_size, layout, output_path, atlas_name, has_alpha):
        """LOW-карты атласа из сетов: короткие суффиксы T_{atlas}_d.png —
        адреса объекта на этом пути нет, индекс бина не нужен."""
        name_for = atlas_filename_fn(atlas_name, low_short=True)
        return self.build_atlas_textures(texture_sets, atlas_size, layout,
                                         output_path, name_for, has_alpha)

    def save_atlas_mapping(self, output_path, atlas_name, atlas_type, atlas_size, layout, created_atlases):
        """Legacy JSON writer (kept: this object-less path has no carrier
        for a record) — one serializer with the record path."""
        entry = make_atlas_entry(atlas_name, atlas_type, atlas_size, "",
                                 output_path, created_atlases,
                                 serialize_layout(layout))
        save_legacy_atlas_json(output_path, entry)


# ===== CREATE ATLAS FROM OBJECT OPERATOR =====

class AGR_OT_CreateAtlasFromObject(AtlasCompositingMixin, Operator):
    """Create atlas from object materials, assign material and layout UVs.

    NOT REGISTERED since 2.6.0 (user decision): the multi-atlas operator
    covers this case exactly (one bin == one atlas).  The class stays as
    the base carrying all compositing/apply helpers for the multi op."""
    bl_idname = "agr.create_atlas_from_object"
    bl_label = "Create Atlas from Object"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not (obj and obj.type == 'MESH' and len(obj.material_slots) > 0):
            cls.poll_message_set("Нужен активный MESH-объект с материалами")
            return False
        return True
    
    def execute(self, context):
        obj = context.active_object
        settings = context.scene.agr_baker_settings
        texture_sets_list = context.scene.agr_texture_sets
        
        # Собираем все материалы объекта
        # Без дублей: один материал может занимать несколько слотов,
        # а в атласе ему нужна ровно одна ячейка
        material_names = []
        for slot in obj.material_slots:
            if not slot.material:
                continue
            # сет ищется по КАНОНИЧЕСКОМУ имени: материал мог быть отодвинут
            # в '<имя>.src' атласом соседнего объекта
            name = source_material_name(slot.material.name)
            if name not in material_names:
                material_names.append(name)
        
        if not material_names:
            self.report({'WARNING'}, "У объекта нет материалов")
            return {'CANCELLED'}
        
        # Ищем соответствующие texture sets
        object_sets = []
        missing_materials = []
        
        for mat_name in material_names:
            found = False
            for tex_set in texture_sets_list:
                if tex_set.material_name == mat_name and not tex_set.is_atlas:
                    object_sets.append(tex_set)
                    found = True
                    break
            
            if not found:
                missing_materials.append(mat_name)
        
        if missing_materials:
            self.report({'ERROR'}, f"Не найдены texture sets для материалов: {', '.join(missing_materials)}")
            return {'CANCELLED'}
        
        if not object_sets:
            self.report({'WARNING'}, "Не найдено ни одного texture set для материалов объекта")
            return {'CANCELLED'}
        
        # Повторный ремап и тайлящиеся UV необратимо портят развёртку —
        # проверяем ДО любых изменений
        if not check_atlas_uv_preconditions(self, obj):
            return {'CANCELLED'}

        atlas_size = int(settings.atlas_size)

        # Проверяем, можно ли упаковать
        total_area = sum(s.resolution * s.resolution for s in object_sets)
        if total_area > atlas_size * atlas_size:
            self.report({'ERROR'}, f"Текстуры не помещаются в атлас {atlas_size}x{atlas_size}")
            return {'CANCELLED'}

        # Грани, которых нет в раскладке (пустой слот, material_index за
        # пределами слотов), получили бы атласный материал без ремапа UV
        uncovered = faces_outside_layout(obj, {ts.material_name for ts in object_sets})
        if uncovered:
            self.report({'ERROR'},
                        f"Не все грани покрыты раскладкой атласа: {describe_uncovered(uncovered)}")
            return {'CANCELLED'}

        # Определяем тип атласа на основе имени объекта
        atlas_type, use_low_naming, address, obj_type, name_warning = resolve_atlas_naming(obj)
        self._atlas_address, self._atlas_obj_type = address, obj_type

        print(f"\n{'='*60}")
        print(f"🎨 СОЗДАНИЕ АТЛАСА ИЗ МАТЕРИАЛОВ ОБЪЕКТА")
        print(f"{'='*60}")
        print(f"Объект: {obj.name}")
        print(f"Тип атласа: {atlas_type}")
        print(f"Размер атласа: {atlas_size}x{atlas_size}")
        print(f"Количество материалов: {len(object_sets)}")

        try:
            # Создаем атлас
            self.reset_compositing_notes()
            result = self.create_and_apply_atlas(context, obj, object_sets, atlas_size, atlas_type, use_low_naming)

            if result:
                notes = self.compositing_notes()
                if name_warning:
                    notes.insert(0, name_warning)
                if notes:
                    agr_report(self, 'WARNING',
                               f"Атлас создан и применён: {result['atlas_name']}; " + "; ".join(notes))
                else:
                    agr_report(self, 'INFO', f"Атлас создан и применен: {result['atlas_name']}")

                # Обновляем список сетов
                bpy.ops.agr.refresh_texture_sets(skip_alpha_strip=True)
                
                return {'FINISHED'}
            else:
                self.report({'ERROR'}, "Не удалось создать атлас")
                return {'CANCELLED'}
                
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка создания атласа: {str(e)}")
            print(f"❌ Ошибка: {e}")
            import traceback
            traceback.print_exc()
            return {'CANCELLED'}
    
    def create_and_apply_atlas(self, context, obj, texture_sets, atlas_size, atlas_type, use_low_naming):
        """Создает атлас и применяет его к объекту"""
        settings = context.scene.agr_baker_settings

        # Снимок «грань → материал» ДО создания атласных материалов:
        # claim_atlas_material отодвигает одноимённый исходник в '<имя>.src',
        # и слоты объекта уезжают за переименованием
        face_to_material = build_face_material_names(obj)

        # Проверяем наличие альфа-канала
        has_alpha = check_sets_have_alpha(texture_sets)
        
        # Получаем именование
        if use_low_naming:
            address, obj_type = self._naming_parts(True)
            if address and obj_type:
                atlas_name = f"A_{address}_{obj_type}"
                material_name = f"M_{address}_{obj_type}_1"
            else:
                atlas_name = f"A_{obj.name}"
                material_name = f"M_{atlas_name}"
                use_low_naming = False
        else:
            atlas_name = f"A_{obj.name}"
            material_name = f"M_{atlas_name}"
        
        print(f"📝 Имя атласа: {atlas_name}")
        print(f"📝 Имя материала: {material_name}")
        print(f"📝 Схема именования: {'LOW' if use_low_naming else 'HIGH'}")
        
        # Определяем путь для сохранения
        if texture_sets:
            base_output_path = os.path.dirname(texture_sets[0].folder_path)
        else:
            blend_file_path = bpy.path.abspath("//")
            base_output_path = os.path.join(blend_file_path, settings.output_folder)
        
        # Создаем папку для атласа
        atlas_output_path = os.path.join(base_output_path, atlas_name)
        if not os.path.exists(atlas_output_path):
            os.makedirs(atlas_output_path)
            print(f"📁 Создана папка: {atlas_output_path}")
        
        # Рассчитываем упаковку
        layout = calculate_atlas_packing_layout(texture_sets, atlas_size)
        
        if not layout:
            raise Exception("Не удалось рассчитать упаковку текстур")
        
        print(f"✅ Упаковка рассчитана: {len(layout)} текстур")
        
        # Создаем текстуры атласа
        created_atlases = {}
        
        if atlas_type == 'HIGH':
            created_atlases = self.create_high_atlas_textures(
                texture_sets, atlas_size, layout, atlas_output_path, atlas_name, has_alpha, use_low_naming
            )
        else:  # LOW
            created_atlases = self.create_low_atlas_textures(
                texture_sets, atlas_size, layout, atlas_output_path, atlas_name, has_alpha, use_low_naming
            )
        
        # Создаем материал
        atlas_material = self.create_atlas_material(
            context, atlas_name, material_name, created_atlases, atlas_type
        )

        # Применяем к объекту (покрытие граней уже проверено в execute ДО
        # записи файлов — здесь это последний рубеж)
        if not self.apply_atlas_to_object(context, obj, atlas_material, layout,
                                          face_to_material=face_to_material):
            return None

        # Per-object atlas record (idprop + color mirror, survives FBX);
        # atlas_mapping.json is legacy and no longer written on this path
        write_atlas_record(obj, [make_atlas_entry(
            atlas_name, atlas_type, atlas_size, atlas_material.name,
            atlas_output_path, created_atlases, serialize_layout(layout))])
        
        print(f"\n✅ Атлас создан и применен!")
        print(f"{'='*60}\n")
        
        return {
            'atlas_name': atlas_name,
            'material_name': material_name,
            'output_path': atlas_output_path,
            'atlases': created_atlases,
            'material': atlas_material
        }
    
    def _naming_parts(self, use_low_naming):
        """address/obj_type для LOW-имён файлов (T_addr_Type_d_i.png)."""
        if not use_low_naming:
            return None, None
        address = getattr(self, '_atlas_address', None)
        obj_type = getattr(self, '_atlas_obj_type', None)
        if address and obj_type:
            return address, obj_type
        obj = bpy.context.active_object
        if obj is None:
            return None, None
        try:
            return process_object_name(obj.name)
        except Exception:
            return None, None

    def create_high_atlas_textures(self, texture_sets, atlas_size, layout, output_path,
                                   atlas_name, has_alpha, use_low_naming):
        """HIGH-карты атласа из объекта (имена по схеме сдачи)"""
        address, obj_type = self._naming_parts(use_low_naming)
        # Multi-atlas sets this per bin; single atlas keeps the default 1
        name_for = atlas_filename_fn(atlas_name, use_low_naming, address, obj_type,
                                     getattr(self, '_atlas_file_index', 1))
        return self.build_atlas_textures(texture_sets, atlas_size, layout,
                                         output_path, name_for, has_alpha)

    def create_low_atlas_textures(self, texture_sets, atlas_size, layout, output_path,
                                  atlas_name, has_alpha, use_low_naming):
        """LOW-карты атласа из объекта (T_addr_Type_d_i.png)"""
        address, obj_type = self._naming_parts(use_low_naming)
        name_for = atlas_filename_fn(atlas_name, use_low_naming, address, obj_type,
                                     getattr(self, '_atlas_file_index', 1))
        return self.build_atlas_textures(texture_sets, atlas_size, layout,
                                         output_path, name_for, has_alpha)
    
    def create_atlas_material(self, context, atlas_name, material_name, created_atlases, atlas_type):
        """Создает материал с атласными текстурами"""
        # Каноническое имя бина совпадает с именем ИСХОДНОГО материала объекта:
        # прежний nodes.clear() по имени стирал чужой датаблок вместе со всеми
        # его пользователями (см. claim_atlas_material).  Папка карт нужна,
        # чтобы узнать СВОЙ же атласный материал с потерянной меткой (FBX)
        atlas_folder = os.path.dirname(next(iter(created_atlases.values()), '') or '')
        material, renamed = claim_atlas_material(material_name, atlas_name,
                                                 atlas_folder=atlas_folder or None)
        if renamed:
            self._renamed_sources = getattr(self, '_renamed_sources', [])
            self._renamed_sources.append(renamed)
            print(f"  ♻️ Исходный материал отодвинут: {material_name} → {renamed}")

        material.use_nodes = True
        nodes = material.node_tree.nodes
        links = material.node_tree.links
        nodes.clear()
        
        output = nodes.new(type='ShaderNodeOutputMaterial')
        bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
        
        output.location = (400, 0)
        bsdf.location = (100, 0)
        
        links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
        
        def load_texture(texture_path, texture_name, location, colorspace='sRGB'):
            if os.path.exists(texture_path):
                try:
                    if texture_name in bpy.data.images:
                        bpy.data.images.remove(bpy.data.images[texture_name])
                    
                    img = bpy.data.images.load(texture_path)
                    img.name = texture_name
                    img.filepath = texture_path
                    img.colorspace_settings.name = colorspace
                    img.reload()
                    img.update()
                    
                    tex_node = nodes.new(type='ShaderNodeTexImage')
                    tex_node.image = img
                    tex_node.location = location
                    tex_node.label = texture_name
                    
                    return tex_node
                except Exception as e:
                    print(f"  ❌ Ошибка загрузки текстуры {texture_name}: {e}")
                    return None
            return None
        
        if atlas_type == 'HIGH':
            # HIGH: DiffuseOpacity + ERM (combined) + Normal
            if 'DIFFUSE_OPACITY' in created_atlases:
                tex_do = load_texture(
                    created_atlases['DIFFUSE_OPACITY'],
                    os.path.basename(created_atlases['DIFFUSE_OPACITY']),
                    (-700, 300),
                    'sRGB'
                )
                if tex_do:
                    links.new(tex_do.outputs['Color'], bsdf.inputs['Base Color'])
                    links.new(tex_do.outputs['Color'], bsdf.inputs['Emission Color'])
                    links.new(tex_do.outputs['Alpha'], bsdf.inputs['Alpha'])
            
            # ERM - используем уже созданную объединенную текстуру
            if 'ERM' in created_atlases:
                tex_erm = load_texture(created_atlases['ERM'], os.path.basename(created_atlases['ERM']), (-700, -100), 'Non-Color')
                if tex_erm:
                    separate = nodes.new(type='ShaderNodeSeparateColor')
                    separate.location = (-400, -100)
                    
                    links.new(tex_erm.outputs['Color'], separate.inputs['Color'])
                    links.new(separate.outputs['Red'], bsdf.inputs['Emission Strength'])
                    links.new(separate.outputs['Green'], bsdf.inputs['Roughness'])
                    links.new(separate.outputs['Blue'], bsdf.inputs['Metallic'])
            
            if 'NORMAL' in created_atlases:
                tex_n = load_texture(created_atlases['NORMAL'], os.path.basename(created_atlases['NORMAL']), (-700, -400), 'Non-Color')
                if tex_n:
                    normal_map = nodes.new(type='ShaderNodeNormalMap')
                    normal_map.location = (-400, -400)
                    links.new(tex_n.outputs['Color'], normal_map.inputs['Color'])
                    links.new(normal_map.outputs['Normal'], bsdf.inputs['Normal'])
        
        else:  # LOW
            # LOW: d + o + erm + n (отдельные каналы d и o, не do)
            # Diffuse (d)
            if 'DIFFUSE' in created_atlases:
                tex_d = load_texture(created_atlases['DIFFUSE'], os.path.basename(created_atlases['DIFFUSE']), (-700, 300), 'sRGB')
                if tex_d:
                    links.new(tex_d.outputs['Color'], bsdf.inputs['Base Color'])
                    print(f"  ✅ Подключен Diffuse (d)")
            
            # Opacity (o) - отдельно
            if 'OPACITY' in created_atlases:
                tex_o = load_texture(created_atlases['OPACITY'], os.path.basename(created_atlases['OPACITY']), (-700, 150), 'Non-Color')
                if tex_o:
                    links.new(tex_o.outputs['Color'], bsdf.inputs['Alpha'])
                    # blend_method is deprecated since 4.2 — guard for 5.x
                    if hasattr(material, 'blend_method'):
                        material.blend_method = 'HASHED'
                    print(f"  ✅ Подключен Opacity (o)")
            
            # LOW: подключаем отдельные карты R и M (не ERM)
            y_offset = -100
            
            if 'ROUGHNESS' in created_atlases:
                tex_r = load_texture(created_atlases['ROUGHNESS'], os.path.basename(created_atlases['ROUGHNESS']), (-700, y_offset), 'Non-Color')
                if tex_r:
                    links.new(tex_r.outputs['Color'], bsdf.inputs['Roughness'])
                    print(f"  ✅ Подключен Roughness (r)")
                y_offset -= 150
            
            if 'METALLIC' in created_atlases:
                tex_m = load_texture(created_atlases['METALLIC'], os.path.basename(created_atlases['METALLIC']), (-700, y_offset), 'Non-Color')
                if tex_m:
                    links.new(tex_m.outputs['Color'], bsdf.inputs['Metallic'])
                    print(f"  ✅ Подключен Metallic (m)")
            
            # Normal (n)
            if 'NORMAL' in created_atlases:
                tex_n = load_texture(created_atlases['NORMAL'], os.path.basename(created_atlases['NORMAL']), (-700, -400), 'Non-Color')
                if tex_n:
                    normal_map = nodes.new(type='ShaderNodeNormalMap')
                    normal_map.location = (-400, -400)
                    links.new(tex_n.outputs['Color'], normal_map.inputs['Color'])
                    links.new(normal_map.outputs['Normal'], bsdf.inputs['Normal'])
                    print(f"  ✅ Подключен Normal (n)")
        
        print(f"🎨 Материал создан: {material_name}")
        
        return material
    
    def apply_atlas_to_object(self, context, obj, atlas_material, layout,
                              face_to_material=None):
        """Применяет атлас к объекту с раскладкой UV (ИСПРАВЛЕНО: сохраняет маппинг материалов ДО очистки)"""
        print(f"\n📐 Применение атласа к объекту {obj.name}")
        
        # Создаем маппинг материал -> UV координаты из layout
        material_to_uv = {}
        for item in layout:
            mat_name = item['texture_set'].material_name
            material_to_uv[mat_name] = {
                'u_min': item['u_min'],
                'v_min': item['v_min'],
                'u_max': item['u_max'],
                'v_max': item['v_max']
            }
        
        print(f"  📋 Маппинг материалов -> UV:")
        for mat_name, coords in material_to_uv.items():
            print(f"    {mat_name}: UV ({coords['u_min']:.3f}, {coords['v_min']:.3f}) -> ({coords['u_max']:.3f}, {coords['v_max']:.3f})")
        
        # Работаем с UV В РЕЖИМЕ OBJECT (до изменения материалов)
        bpy.ops.object.mode_set(mode='OBJECT')

        # Маппинг face_index -> material_name снят вызывающим ДО переименования
        # исходников; иначе слоты уже показывают '<имя>.src'
        if face_to_material is None:
            face_to_material = build_face_material_names(obj)

        # Гвард ДО мутаций: атласный материал получат ВСЕ грани, а ремап —
        # только сопоставленные; остальные сэмплили бы весь атлас
        uncovered = uncovered_from_names(face_to_material, set(material_to_uv))
        if uncovered:
            self.report({'ERROR'},
                        f"Не все грани покрыты раскладкой атласа: {describe_uncovered(uncovered)} — применение отменено")
            return False

        if obj.data.uv_layers.active is None:
            obj.data.uv_layers.new(name="UVMap")

        uv_layer = obj.data.uv_layers.active.data

        processed_faces = 0
        for poly in obj.data.polygons:
            mat_name = face_to_material[poly.index]
            if mat_name is not None:
                if mat_name in material_to_uv:
                    uv_coords = material_to_uv[mat_name]
                    
                    # Сохраняем оригинальные UV координаты полигона
                    orig_uvs = []
                    for loop_idx in poly.loop_indices:
                        uv = uv_layer[loop_idx].uv
                        orig_uvs.append((uv.x, uv.y))
                    
                    # Применяем новые UV координаты (масштабируем в регион атласа)
                    for i, loop_idx in enumerate(poly.loop_indices):
                        orig_u, orig_v = orig_uvs[i]
                        new_u = uv_coords['u_min'] + orig_u * (uv_coords['u_max'] - uv_coords['u_min'])
                        new_v = uv_coords['v_min'] + orig_v * (uv_coords['v_max'] - uv_coords['v_min'])
                        uv_layer[loop_idx].uv = (new_u, new_v)
                    
                    processed_faces += 1
        
        print(f"  ✅ Обработано {processed_faces} полигонов")
        
        # ТЕПЕРЬ заменяем материалы (после UV раскладки)
        obj.data.materials.clear()
        obj.data.materials.append(atlas_material)
        
        # Устанавливаем все полигоны на материал 0
        for poly in obj.data.polygons:
            poly.material_index = 0

        # Guard against a second (non-idempotent) UV remap; cleared by Unpack
        obj['agr_atlas_applied'] = atlas_material.name

        print(f"✅ UV раскладка применена, материал назначен")
        return True


class AGR_OT_CreateMultiAtlasFromObject(AGR_OT_CreateAtlasFromObject):
    """Pack ALL object materials into as many atlases of the chosen size as needed"""
    bl_idname = "agr.create_multi_atlas_from_object"
    bl_label = "Create Multi-Atlas from Object"
    bl_options = {'REGISTER', 'UNDO'}

    def _collect_object_sets(self, context, obj):
        """Сеты объекта в порядке слотов (без дублей) или None."""
        material_names = []
        for slot in obj.material_slots:
            if not slot.material:
                continue
            # сет ищется по КАНОНИЧЕСКОМУ имени: материал мог быть отодвинут
            # в '<имя>.src' атласом соседнего объекта
            name = source_material_name(slot.material.name)
            if name not in material_names:
                material_names.append(name)
        sets_by_material = {ts.material_name: ts for ts in context.scene.agr_texture_sets
                            if not ts.is_atlas}
        object_sets = [sets_by_material[name] for name in material_names
                       if name in sets_by_material]
        if len(object_sets) != len(material_names):
            return None
        return object_sets

    def _existing_bin_folders(self, context):
        """Папки A_*, которые будут перезаписаны этим прогоном.

        Повторная сборка молча затирала текстуры уже сданного атласа —
        спрашиваем подтверждение ДО любой записи."""
        obj = context.active_object
        try:
            object_sets = self._collect_object_sets(context, obj)
            if not object_sets:
                return []
            atlas_size = int(context.scene.agr_baker_settings.atlas_size)
            bins = len(calculate_multi_atlas_packing(object_sets, atlas_size))
            _atlas_type, use_low, address, obj_type, _warn = resolve_atlas_naming(obj)
            base = os.path.dirname(object_sets[0].folder_path)
            prefix = f"A_{address}_{obj_type}_" if use_low else f"A_{obj.name}_"
            return [f"{prefix}{i}" for i in range(1, bins + 1)
                    if os.path.isdir(os.path.join(base, f"{prefix}{i}"))]
        except Exception:
            # Прогноз не должен мешать запуску — все настоящие проверки в execute
            return []

    def invoke(self, context, event):
        folders = self._existing_bin_folders(context)
        if not folders:
            return self.execute(context)
        message = "Будут перезаписаны папки: " + ", ".join(folders)
        try:
            return context.window_manager.invoke_confirm(self, event, message=message)
        except TypeError:
            # Blender < 4.1: invoke_confirm без параметра message
            self.report({'WARNING'}, message)
            return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        obj = context.active_object
        settings = context.scene.agr_baker_settings
        texture_sets_list = context.scene.agr_texture_sets

        # Собираем все материалы объекта
        # Без дублей: один материал может занимать несколько слотов,
        # а в атласе ему нужна ровно одна ячейка
        material_names = []
        for slot in obj.material_slots:
            if not slot.material:
                continue
            # сет ищется по КАНОНИЧЕСКОМУ имени: материал мог быть отодвинут
            # в '<имя>.src' атласом соседнего объекта
            name = source_material_name(slot.material.name)
            if name not in material_names:
                material_names.append(name)

        if not material_names:
            self.report({'WARNING'}, "У объекта нет материалов")
            return {'CANCELLED'}

        # Ищем соответствующие texture sets
        object_sets = []
        missing_materials = []

        for mat_name in material_names:
            found = False
            for tex_set in texture_sets_list:
                if tex_set.material_name == mat_name and not tex_set.is_atlas:
                    object_sets.append(tex_set)
                    found = True
                    break

            if not found:
                missing_materials.append(mat_name)

        if missing_materials:
            self.report({'ERROR'}, f"Не найдены texture sets для материалов: {', '.join(missing_materials)}")
            return {'CANCELLED'}

        if not object_sets:
            self.report({'WARNING'}, "Не найдено ни одного texture set для материалов объекта")
            return {'CANCELLED'}

        # Повторный ремап и тайлящиеся UV необратимо портят развёртку —
        # проверяем ДО любых изменений
        if not check_atlas_uv_preconditions(self, obj):
            return {'CANCELLED'}

        atlas_size = int(settings.atlas_size)

        # В отличие от одиночного атласа, площадь не ограничиваем — лишь бы
        # каждый сет по отдельности влезал в атлас
        too_big = [s.name for s in object_sets if s.resolution > atlas_size]
        if too_big:
            self.report({'ERROR'}, f"Сеты больше атласа {atlas_size}px: {', '.join(too_big)}")
            return {'CANCELLED'}

        # Грани вне раскладки (пустой слот, material_index за пределами
        # слотов) получили бы атласный материал без ремапа UV
        uncovered = faces_outside_layout(obj, {ts.material_name for ts in object_sets})
        if uncovered:
            self.report({'ERROR'},
                        f"Не все грани покрыты раскладкой атласа: {describe_uncovered(uncovered)}")
            return {'CANCELLED'}

        # Определяем тип атласа на основе имени объекта
        atlas_type, use_low_naming, address, obj_type, name_warning = resolve_atlas_naming(obj)
        self._atlas_address, self._atlas_obj_type = address, obj_type

        print(f"\n{'='*60}")
        print(f"🎨 СОЗДАНИЕ МУЛЬТИ-АТЛАСА ИЗ МАТЕРИАЛОВ ОБЪЕКТА")
        print(f"{'='*60}")
        print(f"Объект: {obj.name}")
        print(f"Тип атласа: {atlas_type}")
        print(f"Размер атласа: {atlas_size}x{atlas_size}")
        print(f"Количество материалов: {len(object_sets)}")

        try:
            self.reset_compositing_notes()
            result = self.create_and_apply_multi_atlas(
                context, obj, object_sets, atlas_size, atlas_type, use_low_naming
            )

            if result:
                # Битые/отсутствующие карты, отодвинутые исходники и
                # осиротевшие бины обязаны попасть в отчёт: раньше их видела
                # только системная консоль, а оператор рапортовал INFO
                notes = self.compositing_notes()
                if name_warning:
                    notes.insert(0, name_warning)
                renamed = getattr(self, '_renamed_sources', None)
                if renamed:
                    notes.append(f"исходные материалы переименованы: {', '.join(renamed)}")
                stale = getattr(self, '_stale_bins', None)
                if stale:
                    notes.append(f"устаревшие бины помечены: {', '.join(stale)}")
                summary = f"Создано атласов: {result['atlas_count']} ({atlas_size}px), материалов: {result['atlas_count']}"
                if notes:
                    agr_report(self, 'WARNING', summary + "; " + "; ".join(notes))
                else:
                    agr_report(self, 'INFO', summary)
                bpy.ops.agr.refresh_texture_sets(skip_alpha_strip=True)
                return {'FINISHED'}
            else:
                self.report({'ERROR'}, "Не удалось создать мульти-атлас")
                return {'CANCELLED'}

        except Exception as e:
            self.report({'ERROR'}, f"Ошибка создания мульти-атласа: {str(e)}")
            print(f"❌ Ошибка: {e}")
            import traceback
            traceback.print_exc()
            return {'CANCELLED'}

    def create_and_apply_multi_atlas(self, context, obj, texture_sets, atlas_size, atlas_type, use_low_naming):
        """Создает несколько атласов и применяет их к объекту"""
        settings = context.scene.agr_baker_settings

        # Снимок «грань → материал» ДО создания атласных материалов (см. выше)
        face_to_material = build_face_material_names(obj)

        has_alpha = check_sets_have_alpha(texture_sets)

        # Базовое именование (индекс бина добавляется в цикле)
        address, obj_type = self._naming_parts(use_low_naming)
        if use_low_naming and not (address and obj_type):
            use_low_naming = False

        # Определяем путь для сохранения
        if texture_sets:
            base_output_path = os.path.dirname(texture_sets[0].folder_path)
        else:
            blend_file_path = bpy.path.abspath("//")
            base_output_path = os.path.join(blend_file_path, settings.output_folder)

        # Рассчитываем упаковку по бинам
        bin_layouts = calculate_multi_atlas_packing(texture_sets, atlas_size)
        print(f"✅ Упаковка рассчитана: {len(bin_layouts)} атлас(ов)")

        atlas_materials = []
        atlas_names = []
        atlas_entries = []

        for bin_idx, layout in enumerate(bin_layouts, start=1):
            bin_sets = [item['texture_set'] for item in layout]

            if use_low_naming:
                atlas_name = f"A_{address}_{obj_type}_{bin_idx}"
                material_name = f"M_{address}_{obj_type}_{bin_idx}"
            else:
                atlas_name = f"A_{obj.name}_{bin_idx}"
                material_name = f"M_{atlas_name}"

            print(f"\n📦 Атлас {bin_idx}/{len(bin_layouts)}: {atlas_name} ({len(bin_sets)} сетов)")

            atlas_output_path = os.path.join(base_output_path, atlas_name)
            if not os.path.exists(atlas_output_path):
                os.makedirs(atlas_output_path)
                print(f"📁 Создана папка: {atlas_output_path}")

            # LOW filenames get the bin index (T_addr_Main_d_2.png for bin 2)
            self._atlas_file_index = bin_idx
            try:
                if atlas_type == 'HIGH':
                    created_atlases = self.create_high_atlas_textures(
                        bin_sets, atlas_size, layout, atlas_output_path, atlas_name, has_alpha, use_low_naming
                    )
                else:  # LOW
                    created_atlases = self.create_low_atlas_textures(
                        bin_sets, atlas_size, layout, atlas_output_path, atlas_name, has_alpha, use_low_naming
                    )
            finally:
                self._atlas_file_index = 1

            atlas_material = self.create_atlas_material(
                context, atlas_name, material_name, created_atlases, atlas_type
            )
            atlas_materials.append(atlas_material)
            atlas_names.append(atlas_name)
            atlas_entries.append(make_atlas_entry(
                atlas_name, atlas_type, atlas_size, atlas_material.name,
                atlas_output_path, created_atlases, serialize_layout(layout),
                bin_index=bin_idx - 1))
            # on-disk safety copy: keeps the folder re-appliable even after
            # Unpack strips the on-object record or the object is deleted
            save_legacy_atlas_json(atlas_output_path, atlas_entries[-1])

        # Применяем все атласы к объекту (покрытие граней проверено в execute
        # ДО записи файлов — здесь последний рубеж)
        if not self.apply_multi_atlas_to_object(context, obj, atlas_materials, bin_layouts,
                                               face_to_material=face_to_material):
            return None

        # ONE per-object record with every bin - Unpack rebuilds them all
        # (the legacy per-bin atlas_mapping.json only ever exposed one bin)
        write_atlas_record(obj, atlas_entries)

        # Бины сверх нового числа остались от прошлой сборки с ДРУГОЙ
        # раскладкой и продолжали предлагаться в «Apply Atlas» — уводим их
        # atlas_mapping.json, чтобы папка выпала из списка применимых
        bin_prefix = f"A_{address}_{obj_type}_" if use_low_naming else f"A_{obj.name}_"
        self._stale_bins = mark_stale_atlas_bins(base_output_path, bin_prefix, len(bin_layouts))

        print(f"\n✅ Мульти-атлас создан и применен: {len(bin_layouts)} атлас(ов)")
        print(f"{'='*60}\n")

        return {
            'atlas_count': len(bin_layouts),
            'atlas_names': atlas_names,
            'materials': atlas_materials,
        }

    def apply_multi_atlas_to_object(self, context, obj, atlas_materials, bin_layouts,
                                    face_to_material=None):
        """Применяет несколько атласов: UV ремап + материал на полигон по бину"""
        print(f"\n📐 Применение мульти-атласа к объекту {obj.name}")

        # Маппинг материал -> (индекс бина, UV регион)
        material_to_target = {}
        for bin_idx, layout in enumerate(bin_layouts):
            for item in layout:
                mat_name = item['texture_set'].material_name
                material_to_target[mat_name] = {
                    'bin': bin_idx,
                    'u_min': item['u_min'],
                    'v_min': item['v_min'],
                    'u_max': item['u_max'],
                    'v_max': item['v_max'],
                }

        print(f"  📋 Маппинг материалов -> атлас/UV:")
        for mat_name, t in material_to_target.items():
            print(f"    {mat_name}: атлас {t['bin'] + 1}, UV ({t['u_min']:.3f}, {t['v_min']:.3f}) -> ({t['u_max']:.3f}, {t['v_max']:.3f})")

        bpy.ops.object.mode_set(mode='OBJECT')

        # Маппинг face_index -> material_name снят вызывающим ДО переименования
        # исходников; иначе слоты уже показывают '<имя>.src'
        if face_to_material is None:
            face_to_material = build_face_material_names(obj)

        # Гвард ДО мутаций: атласный материал получат ВСЕ грани, а ремап —
        # только сопоставленные
        uncovered = uncovered_from_names(face_to_material, set(material_to_target))
        if uncovered:
            self.report({'ERROR'},
                        f"Не все грани покрыты раскладкой атласа: {describe_uncovered(uncovered)} — применение отменено")
            return False

        if obj.data.uv_layers.active is None:
            obj.data.uv_layers.new(name="UVMap")

        uv_layer = obj.data.uv_layers.active.data

        processed_faces = 0
        for poly in obj.data.polygons:
            mat_name = face_to_material[poly.index]
            if mat_name is not None:
                if mat_name in material_to_target:
                    target = material_to_target[mat_name]

                    orig_uvs = []
                    for loop_idx in poly.loop_indices:
                        uv = uv_layer[loop_idx].uv
                        orig_uvs.append((uv.x, uv.y))

                    for i, loop_idx in enumerate(poly.loop_indices):
                        orig_u, orig_v = orig_uvs[i]
                        new_u = target['u_min'] + orig_u * (target['u_max'] - target['u_min'])
                        new_v = target['v_min'] + orig_v * (target['v_max'] - target['v_min'])
                        uv_layer[loop_idx].uv = (new_u, new_v)

                    processed_faces += 1

        print(f"  ✅ Обработано {processed_faces} полигонов")

        # Заменяем материалы: один слот на каждый атлас
        obj.data.materials.clear()
        for atlas_material in atlas_materials:
            obj.data.materials.append(atlas_material)

        # Полигон получает материал своего бина
        for poly in obj.data.polygons:
            target = material_to_target.get(face_to_material[poly.index])
            poly.material_index = target['bin'] if target else 0

        # Guard against a second (non-idempotent) UV remap; cleared by Unpack
        obj['agr_atlas_applied'] = ", ".join(m.name for m in atlas_materials)

        print(f"✅ UV раскладка применена, {len(atlas_materials)} материал(ов) назначено")
        return True


# ===== APPLY EXISTING ATLAS TO OBJECT OPERATOR =====

# Module-level cache: Blender's EnumProperty callback stores only pointers to
# the identifier/name/description strings. Without keeping a Python reference
# alive, the GC frees them and reading the property yields garbage bytes
# (UnicodeDecodeError on 0x90 / 0xf0).
_atlas_enum_cache = []
_atlas_enum_fingerprint = None


def _atlas_enum_fp(context, agr_bake_path):
    """Дешёвый фингерпринт списка атласов: колбэк EnumProperty зовётся на
    КАЖДУЮ перерисовку диалога, а полный обход делал listdir + json.load по
    каждой папке A_* (на сетевом диске — лаг на каждое движение мыши)."""
    try:
        mtime = os.path.getmtime(agr_bake_path) if agr_bake_path and os.path.isdir(agr_bake_path) else 0.0
    except OSError:
        mtime = 0.0
    n_records = sum(1 for _ in iter_atlas_entries(peek=True))
    return (bpy.data.filepath, agr_bake_path, mtime, n_records, len(bpy.data.objects))


def get_available_atlases(self, context):
    """Список атласов для EnumProperty: записи на объектах файла + legacy
    atlas_mapping.json на диске.  Колбэк зовётся из draw() диалога —
    только peek-чтения, никаких мутаций ID-данных."""
    global _atlas_enum_cache, _atlas_enum_fingerprint

    settings = context.scene.agr_baker_settings
    blend_path = bpy.path.abspath("//")
    agr_bake_path = os.path.join(blend_path, settings.output_folder) if blend_path else ''
    fingerprint = _atlas_enum_fp(context, agr_bake_path)
    if fingerprint == _atlas_enum_fingerprint and _atlas_enum_cache:
        return _atlas_enum_cache

    items = []
    seen = set()

    # 1) Per-object records (any mesh in the file carrying agr_atlas_data)
    for _obj, entry in iter_atlas_entries(peek=True):
        name = entry.get('atlas_name', '')
        folder = entry_folder_abs(entry)
        if not name or name in seen or not folder or not os.path.isdir(folder):
            continue
        seen.add(name)
        size = entry.get('atlas_size', '?')
        items.append((folder, name, f"Atlas: {name} ({size}x{size})"))

    # 2) Legacy disk scan: A_* folders with atlas_mapping.json
    if agr_bake_path:
        if os.path.exists(agr_bake_path):
            for item in os.listdir(agr_bake_path):
                if item in seen:
                    continue
                item_path = os.path.join(agr_bake_path, item)
                if os.path.isdir(item_path) and item.startswith("A_"):
                    mapping_path = os.path.join(item_path, 'atlas_mapping.json')
                    if os.path.exists(mapping_path):
                        try:
                            with open(mapping_path, 'r', encoding='utf-8') as f:
                                mapping = json.load(f)
                                atlas_size = mapping.get('atlas_size', 'Unknown')
                                items.append((
                                    item_path,
                                    item,
                                    f"Atlas: {item} ({atlas_size}x{atlas_size})"
                                ))
                        except Exception:
                            # Если не удалось прочитать mapping, всё равно добавляем
                            items.append((
                                item_path,
                                item,
                                f"Atlas: {item}"
                            ))

    if not items:
        items.append(('NONE', "No atlases", "No atlases available"))

    _atlas_enum_cache = items
    _atlas_enum_fingerprint = fingerprint
    return items


class AGR_OT_ApplyAtlasToObject(Operator):
    """Apply existing atlas to active object with UV layout"""
    bl_idname = "agr.apply_atlas_to_object"
    bl_label = "Apply Atlas to Object"
    bl_options = {'REGISTER', 'UNDO'}
    
    selected_atlas: EnumProperty(
        name="Atlas",
        description="Select atlas to apply",
        items=get_available_atlases
    )
    
    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            cls.poll_message_set("Нужен активный MESH-объект")
            return False

        # Check for atlases via texture sets collection (no filesystem I/O in poll)
        for tex_set in context.scene.agr_texture_sets:
            if getattr(tex_set, 'is_atlas', False):
                return True

        # Fallback: poll runs on every redraw — no filesystem checks here.
        # invoke() reports gracefully when no atlases are actually found.
        if not bpy.path.abspath("//"):
            cls.poll_message_set("Сохраните .blend файл — атласы ищутся рядом с ним")
            return False
        return True
    
    def invoke(self, context, event):
        # Показываем диалог выбора атласа
        # Проверяем наличие атласов через get_available_atlases
        available = get_available_atlases(self, context)
        
        if not available or (len(available) == 1 and available[0][0] == 'NONE'):
            self.report({'WARNING'}, "Нет доступных атласов")
            return {'CANCELLED'}
        
        # Показываем диалог выбора
        return context.window_manager.invoke_props_dialog(self)
    
    def draw(self, context):
        layout = self.layout
        layout.prop(self, "selected_atlas")
    
    def execute(self, context):
        obj = context.active_object
        
        if self.selected_atlas == 'NONE':
            self.report({'ERROR'}, "Не выбран атлас")
            return {'CANCELLED'}
        
        # selected_atlas содержит путь к папке атласа
        atlas_folder_path = self.selected_atlas
        atlas_name = os.path.basename(atlas_folder_path)
        
        if not os.path.exists(atlas_folder_path):
            self.report({'ERROR'}, "Папка атласа не найдена")
            return {'CANCELLED'}
        
        # Источник раскладки: запись на любом объекте файла, затем legacy
        # atlas_mapping.json рядом с текстурами (execute — мутации разрешены)
        _src_obj, mapping = find_atlas_entry(atlas_name, peek=False)
        if mapping is None:
            legacy = load_legacy_atlas_json(atlas_folder_path)
            if legacy is None:
                self.report({'ERROR'}, "Нет записи атласа на объектах и не найден atlas_mapping.json")
                return {'CANCELLED'}
            mapping = record_from_legacy(legacy, atlas_folder_path)
        
        # Проверяем, что все материалы объекта есть в атласе
        # каноническое имя: материал, отодвинутый в '<имя>.src' атласом соседа,
        # обязан по-прежнему находиться в раскладке
        obj_materials = [source_material_name(slot.material.name)
                         for slot in obj.material_slots if slot.material]
        atlas_materials = [item['material_name'] for item in mapping['layout']]
        
        missing = [m for m in obj_materials if m not in atlas_materials]
        if missing:
            self.report({'ERROR'}, f"Материалы не найдены в атласе: {', '.join(missing)}. Операция отменена.")
            return {'CANCELLED'}

        # Пустой слот / material_index за пределами слотов имени не даёт, но
        # грани такого слота получили бы атласный материал без ремапа UV
        uncovered = faces_outside_layout(obj, set(atlas_materials))
        if uncovered:
            self.report({'ERROR'},
                        f"Не все грани покрыты раскладкой атласа: {describe_uncovered(uncovered)}. Операция отменена.")
            return {'CANCELLED'}

        # Повторный ремап и тайлящиеся UV необратимо портят развёртку
        if not check_atlas_uv_preconditions(self, obj):
            return {'CANCELLED'}

        print(f"\n{'='*60}")
        print(f"📐 ПРИМЕНЕНИЕ АТЛАСА К ОБЪЕКТУ")
        print(f"{'='*60}")
        print(f"Объект: {obj.name}")
        print(f"Атлас: {atlas_name}")
        
        try:
            # Применяем атлас
            if not self.apply_atlas_uv(context, obj, atlas_folder_path, atlas_name, mapping):
                return {'CANCELLED'}

            agr_report(self, 'INFO', "Атлас применён к объекту")
            return {'FINISHED'}

        except Exception as e:
            self.report({'ERROR'}, f"Ошибка применения атласа: {str(e)}")
            print(f"❌ Ошибка: {e}")
            import traceback
            traceback.print_exc()
            return {'CANCELLED'}
    
    def apply_atlas_uv(self, context, obj, atlas_folder_path, atlas_name, mapping):
        """Применяет UV раскладку атласа к объекту.  Returns True при успехе —
        ранний выход раньше маскировался отчётом «Атлас применен»."""
        # Создаем маппинг материал -> UV координаты из JSON
        material_to_uv = {}
        for item in mapping['layout']:
            mat_name = item['material_name']
            material_to_uv[mat_name] = {
                'u_min': item['u_min'],
                'v_min': item['v_min'],
                'u_max': item['u_max'],
                'v_max': item['v_max']
            }
        
        print(f"📋 Маппинг материалов из JSON:")
        for mat_name, coords in material_to_uv.items():
            print(f"  {mat_name}: UV ({coords['u_min']:.3f}, {coords['v_min']:.3f}) - ({coords['u_max']:.3f}, {coords['v_max']:.3f})")
        
        # Переключаемся в object mode
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.object.select_all(action='DESELECT')
        obj.select_set(True)
        context.view_layer.objects.active = obj
        
        # Сохраняем маппинг face index -> material name ДО очистки материалов
        face_to_material = build_face_material_names(obj)

        print(f"💾 Сохранено {len(face_to_material)} полигонов с материалами")

        # Имя материала БИНА из записи (у мульти-атласа каждый бин несёт
        # своё M_addr_Type_i — конвенция с хардкодом _1 подсовывала бину 2
        # материал бина 1); фолбэк на конвенцию для legacy JSON без имени
        atlas_material_name = (mapping.get('material_name') or '').strip()
        if not atlas_material_name:
            try:
                address, obj_type = process_object_name(obj.name)
                atlas_material_name = f"M_{address}_{obj_type}_1"
            except Exception:
                atlas_material_name = f"M_{atlas_name}"

        # Материал добывается ДО очистки слотов: провал не должен оставлять
        # объект без материалов вообще.  Одноимённый датаблок БЕЗ метки
        # атласа — это исходный материал объекта (имя бина совпадает с
        # конвенцией AGR Rename): подсунуть его как атласный значит натянуть
        # исходную текстуру на UV, сжатые в ячейку атласа
        existing = bpy.data.materials.get(atlas_material_name)
        if existing is None or not existing.get(ATLAS_MAT_TAG):
            # Пытаемся создать материал из текстур атласа
            self.create_atlas_material_from_textures(atlas_folder_path, atlas_name, mapping, atlas_material_name)

        if atlas_material_name not in bpy.data.materials:
            self.report({'ERROR'}, f"Материал атласа '{atlas_material_name}' не найден — применение отменено")
            return False

        atlas_material = bpy.data.materials[atlas_material_name]

        # Заменяем материалы
        obj.data.materials.clear()
        obj.data.materials.append(atlas_material)
        
        # Переключаемся в edit mode для работы с UV
        bpy.ops.object.mode_set(mode='EDIT')
        bm = bmesh.from_edit_mesh(obj.data)
        
        if not bm.loops.layers.uv:
            bm.loops.layers.uv.new("UVMap")
        
        uv_layer = bm.loops.layers.uv.active
        
        # Раскладываем UV по JSON маппингу используя сохраненный face_to_material
        processed_faces = 0
        for face in bm.faces:
            mat_name = face_to_material[face.index]

            if mat_name is not None:
                if mat_name in material_to_uv:
                    uv_coords = material_to_uv[mat_name]
                    
                    # Сохраняем оригинальные UV координаты
                    face_uvs = []
                    for loop in face.loops:
                        uv = loop[uv_layer].uv
                        face_uvs.append((uv.x, uv.y))
                    
                    # Применяем трансформацию UV в область атласа
                    for i, loop in enumerate(face.loops):
                        orig_u, orig_v = face_uvs[i]
                        new_u = uv_coords['u_min'] + orig_u * (uv_coords['u_max'] - uv_coords['u_min'])
                        new_v = uv_coords['v_min'] + orig_v * (uv_coords['v_max'] - uv_coords['v_min'])
                        loop[uv_layer].uv = (new_u, new_v)
                    
                    processed_faces += 1
                else:
                    print(f"  ⚠️ Материал {mat_name} не найден в JSON маппинге")
            
            # Устанавливаем индекс материала на 0 (атласный материал)
            face.material_index = 0
        
        bmesh.update_edit_mesh(obj.data)
        bpy.ops.object.mode_set(mode='OBJECT')

        # Guard against a second (non-idempotent) UV remap; cleared by Unpack
        obj['agr_atlas_applied'] = atlas_material_name

        # Copy/migrate the atlas record onto THIS object — Unpack then works
        # from the object alone (no folder JSON needed, survives FBX)
        entry = dict(mapping)
        entry['material_name'] = atlas_material_name
        write_atlas_record(obj, [entry])

        print(f"✅ UV раскладка применена: обработано {processed_faces} полигонов")
        return True

    def create_atlas_material_from_textures(self, atlas_folder_path, atlas_name, mapping, material_name=None):
        """Создает материал атласа из текстур"""
        if not material_name:
            material_name = f"M_{atlas_name}"
        atlas_type = mapping.get('atlas_type', 'HIGH')
        created_atlases = mapping.get('created_atlases', {})

        # atlas_mapping.json хранит абсолютные пути — при переносе проекта
        # перебазируем на текущую папку атласа по имени файла
        def resolve_atlas_path(path):
            if path and os.path.exists(path):
                return path
            if path:
                local = os.path.join(atlas_folder_path, os.path.basename(path))
                if os.path.exists(local):
                    return local
            return path
        created_atlases = {k: resolve_atlas_path(v) for k, v in created_atlases.items()}

        # То же, что при создании атласа: имя бина совпадает с именем
        # ИСХОДНОГО материала объекта, и nodes.clear() по имени стирал его
        material, renamed = claim_atlas_material(material_name, atlas_name,
                                                 atlas_folder=atlas_folder_path)
        if renamed:
            self.report({'WARNING'}, f"Исходный материал отодвинут: {material_name} → {renamed}")
            print(f"  ♻️ Исходный материал отодвинут: {material_name} → {renamed}")

        material.use_nodes = True
        nodes = material.node_tree.nodes
        links = material.node_tree.links
        nodes.clear()
        
        output = nodes.new(type='ShaderNodeOutputMaterial')
        bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
        
        output.location = (400, 0)
        bsdf.location = (100, 0)
        
        links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
        
        def load_texture(texture_path, texture_name, location, colorspace='sRGB'):
            if os.path.exists(texture_path):
                try:
                    if texture_name in bpy.data.images:
                        bpy.data.images.remove(bpy.data.images[texture_name])
                    
                    img = bpy.data.images.load(texture_path)
                    img.name = texture_name
                    img.filepath = texture_path
                    img.colorspace_settings.name = colorspace
                    img.reload()
                    img.update()
                    
                    tex_node = nodes.new(type='ShaderNodeTexImage')
                    tex_node.image = img
                    tex_node.location = location
                    tex_node.label = texture_name
                    
                    return tex_node
                except Exception as e:
                    print(f"  ❌ Ошибка загрузки текстуры {texture_name}: {e}")
                    return None
            return None
        
        # Всегда используем 3-карточный метод: DO (или D), ERM, N
        # Это работает для обоих типов атласов (HIGH и LOW)
        
        # 1. Diffuse/Opacity
        if 'DIFFUSE_OPACITY' in created_atlases:
            tex_do = load_texture(created_atlases['DIFFUSE_OPACITY'], os.path.basename(created_atlases['DIFFUSE_OPACITY']), (-700, 300), 'sRGB')
            if tex_do:
                links.new(tex_do.outputs['Color'], bsdf.inputs['Base Color'])
                links.new(tex_do.outputs['Alpha'], bsdf.inputs['Alpha'])
                print(f"  ✅ Подключен DiffuseOpacity")
        elif 'DIFFUSE' in created_atlases:
            tex_d = load_texture(created_atlases['DIFFUSE'], os.path.basename(created_atlases['DIFFUSE']), (-700, 300), 'sRGB')
            if tex_d:
                links.new(tex_d.outputs['Color'], bsdf.inputs['Base Color'])
                print(f"  ✅ Подключен Diffuse")
        
        # 2. ERM (объединенная текстура E+R+M)
        if 'ERM' in created_atlases:
            tex_erm = load_texture(created_atlases['ERM'], os.path.basename(created_atlases['ERM']), (-700, -100), 'Non-Color')
            if tex_erm:
                separate = nodes.new(type='ShaderNodeSeparateColor')
                separate.location = (-400, -100)
                
                links.new(tex_erm.outputs['Color'], separate.inputs['Color'])
                links.new(separate.outputs['Red'], bsdf.inputs['Emission Strength'])
                links.new(separate.outputs['Green'], bsdf.inputs['Roughness'])
                links.new(separate.outputs['Blue'], bsdf.inputs['Metallic'])
                print(f"  ✅ Подключен ERM (E→Emission, R→Roughness, M→Metallic)")
        
        # 3. Normal
        if 'NORMAL' in created_atlases:
            tex_n = load_texture(created_atlases['NORMAL'], os.path.basename(created_atlases['NORMAL']), (-700, -400), 'Non-Color')
            if tex_n:
                normal_map = nodes.new(type='ShaderNodeNormalMap')
                normal_map.location = (-400, -400)
                links.new(tex_n.outputs['Color'], normal_map.inputs['Color'])
                links.new(normal_map.outputs['Normal'], bsdf.inputs['Normal'])
                print(f"  ✅ Подключен Normal")

        # blend_method is deprecated since 4.2 — guard for 5.x
        if hasattr(material, 'blend_method'):
            material.blend_method = 'HASHED'

        print(f"🎨 Материал создан: {material_name}")


# ===== UNPACK ATLAS TO MATERIALS OPERATOR =====

class AGR_OT_UnpackAtlasToMaterials(Operator):
    """Unpack atlas back to individual materials and restore UV to 0-1 range"""
    bl_idname = "agr.unpack_atlas_to_materials"
    bl_label = "Unpack Atlas to Materials"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        obj = context.active_object
        if not obj or obj.type != 'MESH':
            cls.poll_message_set("Нужен активный MESH-объект")
            return False
        if not obj.active_material:
            cls.poll_message_set("У объекта должен быть активный материал атласа")
            return False
        return True
    
    def execute(self, context):
        obj = context.active_object
        active_material = obj.active_material
        
        print(f"\n{'='*60}")
        print(f"📦 РАСПАКОВКА АТЛАСА В МАТЕРИАЛЫ")
        print(f"{'='*60}")
        print(f"Объект: {obj.name}")
        print(f"Активный материал: {active_material.name}")
        
        try:
            # 1-2. Источник раскладки: запись на объекте (ВСЕ бины), затем
            # legacy atlas_mapping.json через текстуру Base Color
            record = read_atlas_record(obj)
            if record and record.get('atlases'):
                entries = [e for e in record['atlases'] if isinstance(e, dict)]
                print(f"📄 Запись атласа на объекте: {len(entries)} атлас(ов)")
            else:
                atlas_folder = self.find_atlas_folder_from_material(active_material)
                if not atlas_folder:
                    self.report({'ERROR'}, "Нет записи атласа на объекте и не найдена папка через Base Color")
                    return {'CANCELLED'}
                print(f"📁 Найдена папка атласа: {atlas_folder}")
                legacy = load_legacy_atlas_json(atlas_folder)
                if legacy is None:
                    self.report({'ERROR'}, f"Не найден atlas_mapping.json в {atlas_folder}")
                    return {'CANCELLED'}
                entries = [record_from_legacy(legacy, atlas_folder)]
                # no record migration here: unpack works from `entries`
                # directly, and a CANCELLED precondition further down must
                # not leave a fresh record outside the undo stack
                print(f"📄 Загружен atlas_mapping.json")

            mapping = {
                'atlas_name': ", ".join(e.get('atlas_name', '') for e in entries),
                'atlas_type': entries[0].get('atlas_type', 'HIGH'),
                'atlas_size': entries[0].get('atlas_size', 0),
                'layout': [item for e in entries for item in e.get('layout', [])],
            }
            print(f"  Атлас(ы): {mapping['atlas_name']}")
            print(f"  Тип: {mapping['atlas_type']}")
            print(f"  Материалов в атласе: {len(mapping['layout'])}")
            
            # 3. Проверяем наличие всех материалов в texture sets
            texture_sets_list = context.scene.agr_texture_sets
            missing_materials = self.check_materials_availability(mapping, texture_sets_list)
            
            if missing_materials:
                self.report({'WARNING'}, f"Материалы не найдены в texture sets: {', '.join(missing_materials)}")
                print(f"⚠️ Отсутствующие материалы: {', '.join(missing_materials)}")

            # 3b. Validate HIGH mode textures for all found texture sets
            found_sets = [ts for ts in texture_sets_list
                          if not ts.is_atlas and any(
                              item['material_name'] == ts.material_name
                              for item in mapping['layout'])]
            if found_sets:
                is_valid, error_msg = validate_all_high_mode(found_sets)
                if not is_valid:
                    self.report({'ERROR'}, error_msg)
                    return {'CANCELLED'}

            # 4. Распаковываем атлас
            result = self.unpack_atlas(context, obj, entries, texture_sets_list)
            
            if result:
                self.report({'INFO'}, f"Атлас распакован: {result['materials_count']} материалов, {result['faces_processed']} полигонов")
                return {'FINISHED'}
            else:
                self.report({'ERROR'}, "Не удалось распаковать атлас")
                return {'CANCELLED'}
                
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка распаковки атласа: {str(e)}")
            print(f"❌ Ошибка: {e}")
            import traceback
            traceback.print_exc()
            return {'CANCELLED'}
    
    def find_atlas_folder_from_material(self, material):
        """Находит папку атласа через текстуру подключенную к Base Color"""
        if not material.use_nodes:
            return None
        
        nodes = material.node_tree.nodes
        
        # Ищем Principled BSDF
        bsdf = None
        for node in nodes:
            if node.type == 'BSDF_PRINCIPLED':
                bsdf = node
                break
        
        if not bsdf:
            return None
        
        # Ищем текстуру подключенную к Base Color
        base_color_input = bsdf.inputs['Base Color']
        if not base_color_input.is_linked:
            return None
        
        # Получаем ноду текстуры
        texture_node = base_color_input.links[0].from_node
        if texture_node.type != 'TEX_IMAGE':
            return None
        
        # Получаем изображение
        if not texture_node.image:
            return None
        
        # Получаем путь к файлу
        texture_path = bpy.path.abspath(texture_node.image.filepath)
        if not os.path.exists(texture_path):
            return None
        
        # Папка атласа - это директория где лежит текстура
        atlas_folder = os.path.dirname(texture_path)
        
        return atlas_folder
    
    def check_materials_availability(self, mapping, texture_sets_list):
        """Проверяет наличие всех материалов из атласа в texture sets"""
        missing_materials = []
        
        for item in mapping['layout']:
            mat_name = item['material_name']
            
            # Ищем в texture sets
            found = False
            for ts in texture_sets_list:
                if ts.material_name == mat_name and not ts.is_atlas:
                    found = True
                    break
            
            if not found:
                missing_materials.append(mat_name)
        
        return missing_materials
    
    def _resolve_unpacked_material(self, mat_name, mat_texture_set):
        """Датаблок для распакованного материала.

        Прежняя эвристика «есть TEX_IMAGE ⇒ настроен» оставляла на материале
        АТЛАСНЫЕ карты (имя бина совпадает с именем исходника) при UV,
        восстановленных в 0..1 — грани сэмплили весь атлас.  Теперь материал
        принимается только если он не помечен атласным И его картинки лежат в
        папке ЕГО сета; иначе берётся отодвинутый исходник '<имя>.src' или
        создаётся новый датаблок с переподключением."""
        existing = bpy.data.materials.get(mat_name)

        if existing is not None and existing.get(ATLAS_MAT_TAG):
            # Каноническое имя занято нашим же атласным материалом
            source = find_source_material(mat_name)
            if existing.users == 0:
                # Объект уже отпустил атласный материал — возвращаем имя исходнику
                existing.name = f"{mat_name}.atlas"
                if source is not None:
                    source.name = mat_name
            if source is not None:
                self.report({'INFO'}, f"Восстановлен исходный материал: {source.name}")
                existing = source
            else:
                existing = None

        if existing is not None:
            if mat_texture_set and not material_wired_to_set(existing, mat_texture_set.folder_path):
                # connect_* returns None when a texture file is unreadable — the
                # material graph is then left untouched, so say so instead of
                # reporting a successful unpack (BAKE-1 contract)
                if connect_texture_set_to_material(existing, mat_texture_set.folder_path,
                                                   mat_texture_set.material_name) is None:
                    self.report({'WARNING'},
                                f"Материал {existing.name}: не удалось подключить сет "
                                f"{mat_texture_set.name} (нечитаемый файл текстуры)")
                print(f"  🔄 Обновлен материал: {existing.name}")
            else:
                print(f"  ♻️ Используется существующий материал: {existing.name}")
            return existing

        material = bpy.data.materials.new(name=mat_name)
        if material.name != mat_name:
            self.report({'WARNING'},
                        f"Имя '{mat_name}' занято атласным материалом — создан {material.name}")
        if mat_texture_set:
            if connect_texture_set_to_material(material, mat_texture_set.folder_path,
                                               mat_texture_set.material_name) is None:
                self.report({'WARNING'},
                            f"Материал {material.name}: не удалось подключить сет "
                            f"{mat_texture_set.name} (нечитаемый файл текстуры)")
            print(f"  ✅ Создан материал: {material.name}")
        else:
            print(f"  ⚠️ Создан пустой материал: {material.name} (texture set не найден)")
        return material

    def unpack_atlas(self, context, obj, entries, texture_sets_list):
        """Распаковывает атлас(ы) обратно в отдельные материалы.

        entries — список записей атласа (один элемент на бин).  Регионы
        разных бинов ПЕРЕКРЫВАЮТСЯ в UV 0..1, поэтому фейс ищет свой регион
        только в раскладке СВОЕГО бина (по материалу его слота)."""
        # Регионы по бинам + сводный словарь для создания материалов
        bin_regions = []
        uv_region_to_material = {}
        for e in entries:
            regions = {}
            for item in e.get('layout', []):
                coords = {
                    'u_min': item['u_min'],
                    'v_min': item['v_min'],
                    'u_max': item['u_max'],
                    'v_max': item['v_max']
                }
                regions[item['material_name']] = coords
                uv_region_to_material[item['material_name']] = coords
            bin_regions.append(regions)

        # Бин фейса определяется материалом его слота (атласным материалом)
        mat_to_bin = {}
        for i, e in enumerate(entries):
            if e.get('material_name'):
                mat_to_bin[e['material_name']] = i

        print(f"\n📋 UV регионы атласа:")
        for mat_name, coords in uv_region_to_material.items():
            print(f"  {mat_name}: ({coords['u_min']:.3f}, {coords['v_min']:.3f}) - ({coords['u_max']:.3f}, {coords['v_max']:.3f})")
        
        # Переключаемся в object mode
        bpy.ops.object.mode_set(mode='OBJECT')
        
        # Получаем UV layer
        if not obj.data.uv_layers.active:
            self.report({'ERROR'}, "У объекта нет UV слоя")
            return None
        
        uv_layer = obj.data.uv_layers.active.data
        
        # Анализируем полигоны и определяем их материалы по UV координатам
        face_to_material = {}
        face_to_original_uvs = {}
        
        for poly in obj.data.polygons:
            # Получаем UV координаты полигона
            poly_uvs = []
            for loop_idx in poly.loop_indices:
                uv = uv_layer[loop_idx].uv
                poly_uvs.append((uv.x, uv.y))
            
            # Вычисляем центр полигона в UV пространстве
            center_u = sum(uv[0] for uv in poly_uvs) / len(poly_uvs)
            center_v = sum(uv[1] for uv in poly_uvs) / len(poly_uvs)
            
            # Регионы только СВОЕГО бина (мульти-атлас): регионы бинов
            # перекрываются в UV 0..1, объединение молча сматчило бы фейс
            # с чужим регионом — неизвестный слот честно уходит в unmatched
            regions = uv_region_to_material
            if len(bin_regions) > 1:
                slot = (obj.material_slots[poly.material_index]
                        if poly.material_index < len(obj.material_slots) else None)
                slot_mat = slot.material.name if slot and slot.material else None
                bin_idx = mat_to_bin.get(slot_mat)
                if bin_idx is None and slot_mat:
                    # FBX/append collisions rename the material to *.001
                    bin_idx = mat_to_bin.get(re.sub(r'\.\d{3}$', '', slot_mat))
                if bin_idx is None:
                    continue  # counted by the unmatched guard below
                regions = bin_regions[bin_idx]

            # Определяем к какому региону атласа принадлежит полигон
            matched_material = None
            for mat_name, coords in regions.items():
                if (coords['u_min'] <= center_u <= coords['u_max'] and
                    coords['v_min'] <= center_v <= coords['v_max']):
                    matched_material = mat_name
                    break
            
            if matched_material:
                face_to_material[poly.index] = matched_material
                
                # Сохраняем оригинальные UV и вычисляем обратную трансформацию
                coords = regions[matched_material]
                original_uvs = []
                u_range = coords['u_max'] - coords['u_min']
                v_range = coords['v_max'] - coords['v_min']
                for uv_u, uv_v in poly_uvs:
                    # Обратная трансформация: из atlas space в 0-1 space
                    orig_u = (uv_u - coords['u_min']) / u_range if u_range > 0 else 0.0
                    orig_v = (uv_v - coords['v_min']) / v_range if v_range > 0 else 0.0
                    original_uvs.append((orig_u, orig_v))
                
                face_to_original_uvs[poly.index] = original_uvs
        
        print(f"\n🔍 Анализ полигонов:")
        print(f"  Всего полигонов: {len(obj.data.polygons)}")
        print(f"  Определено материалов: {len(face_to_material)}")

        # Все фейсы должны попасть в регионы раскладки ДО каких-либо
        # изменений — иначе несопоставленные фейсы остались бы с висячим
        # material_index и атласными UV
        unmatched = len(obj.data.polygons) - len(face_to_material)
        if unmatched:
            self.report({'ERROR'}, f"{unmatched} полигонов не попадают в регионы атласа — распаковка отменена (UV сдвинуты или объект содержит не-атласные материалы)")
            return None

        # Очищаем материалы объекта ДО разбора датаблоков: атласный материал
        # теряет пользователя, и каноническое имя можно вернуть исходнику
        obj.data.materials.clear()

        # Создаем/получаем материалы ТОЛЬКО для реально встреченных регионов:
        # объект, использующий часть мульти-атласа, получал слот на каждый
        # материал раскладки (60 слотов при 12 нужных)
        material_objects = {}
        material_indices = {}
        needed_materials = sorted(set(face_to_material.values()))
        sets_by_material = {ts.material_name: ts for ts in texture_sets_list if not ts.is_atlas}

        for mat_name in needed_materials:
            mat_texture_set = sets_by_material.get(mat_name)
            material = self._resolve_unpacked_material(mat_name, mat_texture_set)
            material_objects[mat_name] = material

        for mat_name in sorted(material_objects.keys()):
            obj.data.materials.append(material_objects[mat_name])
            material_indices[mat_name] = len(obj.data.materials) - 1
            print(f"  📌 Добавлен материал: {mat_name} (index {material_indices[mat_name]})")
        
        # Применяем материалы и восстанавливаем UV
        faces_processed = 0
        for poly in obj.data.polygons:
            if poly.index in face_to_material:
                mat_name = face_to_material[poly.index]
                poly.material_index = material_indices[mat_name]
                
                # Восстанавливаем UV координаты в 0-1 диапазон
                original_uvs = face_to_original_uvs[poly.index]
                for i, loop_idx in enumerate(poly.loop_indices):
                    uv_layer[loop_idx].uv = original_uvs[i]
                
                faces_processed += 1
        
        # On-disk safety copy BEFORE the record is stripped: for atlases
        # created from an object the record was the ONLY layout copy - the
        # folder must stay re-appliable after Unpack
        for e in entries:
            folder = entry_folder_abs(e)
            if folder and os.path.isdir(folder):
                save_legacy_atlas_json(folder, e)

        # Object is back to individual materials — allow atlas apply again
        if 'agr_atlas_applied' in obj:
            del obj['agr_atlas_applied']
        strip_atlas_record(obj)

        print(f"\n✅ Распаковка завершена:")
        print(f"  Материалов: {len(material_objects)}")
        print(f"  Полигонов обработано: {faces_processed}")
        print(f"{'='*60}\n")

        return {
            'materials_count': len(material_objects),
            'faces_processed': faces_processed
        }


# ===== REGISTRATION =====

classes = (
    AGR_OT_PreviewAtlasLayout,
    AGR_OT_PreviewAtlasLayoutFromObject,
    AGR_OT_CreateAtlasOnly,
    # AGR_OT_CreateAtlasFromObject is NOT registered (superseded by the
    # multi-atlas operator; kept in code as its base class)
    AGR_OT_CreateMultiAtlasFromObject,
    AGR_OT_ApplyAtlasToObject,
    AGR_OT_UnpackAtlasToMaterials,
)


def register():
    """Register atlas operators"""
    for cls in classes:
        bpy.utils.register_class(cls)
    
    print("✅ Atlas operators registered")


def unregister():
    """Unregister atlas operators"""
    unregister_classes(classes)  # idempotent: survives a half-registered module (R-glue-4)
    
    print("Atlas operators unregistered")
