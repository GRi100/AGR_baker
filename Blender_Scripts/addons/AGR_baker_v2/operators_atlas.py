"""
Atlas creation operators for AGR Baker v2
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
    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False


# ===== HELPER FUNCTIONS =====

def process_object_name(obj_name):
    """
    Обрабатывает имя объекта для получения ADDRESS и типа (Main/Flora/Ground/GroundEl)
    Returns: (address, obj_type) or raises Exception
    """
    if not obj_name.startswith("SM_"):
        raise Exception("Имя объекта должно начинаться с 'SM_'")
    
    # Убираем префикс SM_
    name_parts = obj_name[3:].split('_')
    
    if len(name_parts) < 2:
        raise Exception("Неверный формат имени объекта. Ожидается: SM_ADDRESS_ObjectType")
    
    # Последняя часть должна быть Main, Flora, Ground или GroundEl
    obj_type = name_parts[-1]
    if obj_type not in ['Main', 'Flora', 'Ground', 'GroundEl']:
        raise Exception("Имя объекта должно заканчиваться на '_Main', '_Flora', '_Ground' или '_GroundEl'")
    
    # ADDRESS - это всё между SM_ и _ObjectType
    address = '_'.join(name_parts[:-1])
    
    return address, obj_type


def get_atlas_naming(context, atlas_type, atlas_size, texture_sets_count):
    """
    Определяет именование атласа на основе активного объекта
    Returns: (atlas_name, material_name, use_low_naming)
    """
    active_obj = context.active_object
    
    # Пытаемся определить тип объекта для LOW атласов
    if atlas_type == 'LOW' and active_obj:
        try:
            address, obj_type = process_object_name(active_obj.name)
            # LOW атлас с специальным именованием
            atlas_name = f"A_{address}_{obj_type}"
            material_name = f"M_{address}_{obj_type}_1"
            return atlas_name, material_name, True
        except:
            pass
    
    # HIGH атлас или объект не подходит под LOW схему
    if active_obj:
        atlas_name = f"A_{active_obj.name}"
    else:
        atlas_name = f"A_Atlas_{atlas_type}_{atlas_size}"
    
    material_name = f"M_{atlas_name}"
    return atlas_name, material_name, False


def get_texture_filename(atlas_name, texture_type, use_low_naming, address=None, obj_type=None):
    """
    Генерирует имя файла текстуры для атласа
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
        return f"T_{address}_{obj_type}_{suffix}.png"
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
                    img = Image.open(do_path)
                    if img.mode in ('RGBA', 'LA'):
                        print(f"  ✓ Найден альфа-канал в {tex_set.name}")
                        return True
                except:
                    pass
            
            # Проверяем Diffuse файл
            d_path = os.path.join(tex_set.folder_path, f"T_{tex_set.material_name}_Diffuse.png")
            if os.path.exists(d_path):
                try:
                    img = Image.open(d_path)
                    if img.mode in ('RGBA', 'LA'):
                        print(f"  ✓ Найден альфа-канал в {tex_set.name}")
                        return True
                except:
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


def create_erm_atlas_combined(texture_sets, atlas_size, layout):
    """Создает ERM атлас (объединяет E, R, M в RGB каналы) - общая функция"""
    from PIL import Image
    
    atlas_name = f"Atlas_ERM_{atlas_size}"
    
    # Удаляем старое изображение если есть
    if atlas_name in bpy.data.images:
        bpy.data.images.remove(bpy.data.images[atlas_name])
    
    # Создаем новое изображение
    atlas_image = bpy.data.images.new(
        atlas_name,
        width=atlas_size,
        height=atlas_size,
        alpha=False,
        float_buffer=False
    )
    
    atlas_image.colorspace_settings.name = 'Non-Color'
    
    # Заполняем черным цветом
    fill = [0.0, 0.0, 0.0, 1.0]
    total_px = atlas_size * atlas_size * 4
    buf = fill * (total_px // 4)
    atlas_image.pixels = buf
    
    # Создаем numpy массив для работы
    atlas_array = np.array(atlas_image.pixels[:]).reshape(atlas_size, atlas_size, 4)
    
    # Размещаем текстуры в атласе
    for item in layout:
        # Получаем пути к E, R, M текстурам
        material_name = item['texture_set'].material_name
        folder_path = item['texture_set'].folder_path
        
        emit_path = os.path.join(folder_path, f"T_{material_name}_Emit.png")
        roughness_path = os.path.join(folder_path, f"T_{material_name}_Roughness.png")
        metallic_path = os.path.join(folder_path, f"T_{material_name}_Metallic.png")
        
        cell_width = item['width']
        cell_height = item['height']
        x = item['x']
        y = item['y']
        
        # Загружаем каналы через PIL
        e_channel = None
        r_channel = None
        m_channel = None
        
        if os.path.exists(emit_path):
            e_img = Image.open(emit_path).convert('L')
            if e_img.size != (cell_width, cell_height):
                e_img = e_img.resize((cell_width, cell_height), Image.Resampling.LANCZOS)
            e_channel = np.array(e_img, dtype=np.float32) / 255.0
        
        if os.path.exists(roughness_path):
            r_img = Image.open(roughness_path).convert('L')
            if r_img.size != (cell_width, cell_height):
                r_img = r_img.resize((cell_width, cell_height), Image.Resampling.LANCZOS)
            r_channel = np.array(r_img, dtype=np.float32) / 255.0
        
        if os.path.exists(metallic_path):
            m_img = Image.open(metallic_path).convert('L')
            if m_img.size != (cell_width, cell_height):
                m_img = m_img.resize((cell_width, cell_height), Image.Resampling.LANCZOS)
            m_channel = np.array(m_img, dtype=np.float32) / 255.0
        
        # Создаем ERM текстуру
        if e_channel is None:
            e_channel = np.zeros((cell_height, cell_width), dtype=np.float32)
        if r_channel is None:
            r_channel = np.ones((cell_height, cell_width), dtype=np.float32) * 0.5
        if m_channel is None:
            m_channel = np.zeros((cell_height, cell_width), dtype=np.float32)
        
        # Размещаем в атласе
        atlas_array[y:y+cell_height, x:x+cell_width, 0] = e_channel
        atlas_array[y:y+cell_height, x:x+cell_width, 1] = r_channel
        atlas_array[y:y+cell_height, x:x+cell_width, 2] = m_channel
        atlas_array[y:y+cell_height, x:x+cell_width, 3] = 1.0
        
        print(f"  📍 Размещена ERM: {item['texture_set'].name} в ({x}, {y})")
    
    # Записываем массив обратно в изображение
    flat = atlas_array.flatten().tolist()
    atlas_image.pixels = flat
    atlas_image.update()
    
    return atlas_image


# ===== PREVIEW ATLAS LAYOUT OPERATOR =====

class AGR_OT_PreviewAtlasLayout(Operator):
    """Preview atlas packing layout for selected texture sets"""
    bl_idname = "agr.preview_atlas_layout"
    bl_label = "Preview Atlas Layout"
    bl_options = {'REGISTER'}
    
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
        
        # Заполняем черным цветом
        pixels = [0.0, 0.0, 0.0, 1.0] * (atlas_size * atlas_size)
        preview_image.pixels = pixels
        
        # Создаем numpy массив для работы
        preview_array = np.array(preview_image.pixels[:]).reshape(atlas_size, atlas_size, 4)
        
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
        
        # Записываем массив обратно в изображение
        flat = preview_array.flatten().tolist()
        preview_image.pixels = flat
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


# ===== CREATE ATLAS ONLY OPERATOR =====

class AGR_OT_CreateAtlasOnly(Operator):
    """Create texture atlas from selected texture sets (no UV layout, no material assignment)"""
    bl_idname = "agr.create_atlas_only"
    bl_label = "Create Atlas Only"
    bl_options = {'REGISTER', 'UNDO'}
    
    atlas_type: EnumProperty(
        name="Atlas Type",
        description="Type of atlas to create",
        items=[
            ('AUTO', "Auto", "Automatically determine based on active object"),
            ('HIGH', "HIGH", "HIGH atlas with DO/ERM/N textures"),
            ('LOW', "LOW", "LOW atlas with d/r/m/o/n separate textures"),
        ],
        default='AUTO'
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
        
        # Определяем тип атласа
        if self.atlas_type == 'AUTO':
            active_obj = context.active_object
            if active_obj:
                try:
                    address, obj_type = process_object_name(active_obj.name)
                    if obj_type in ['Main', 'Flora', 'Ground', 'GroundEl']:
                        final_atlas_type = 'LOW'
                    else:
                        final_atlas_type = 'HIGH'
                except:
                    final_atlas_type = 'HIGH'
            else:
                final_atlas_type = 'HIGH'
        else:
            final_atlas_type = self.atlas_type
        
        print(f"\n{'='*60}")
        print(f"🎨 СОЗДАНИЕ АТЛАСА (ТОЛЬКО ТЕКСТУРЫ)")
        print(f"{'='*60}")
        print(f"Тип атласа: {final_atlas_type}")
        print(f"Размер атласа: {atlas_size}x{atlas_size}")
        print(f"Количество наборов: {len(selected_sets)}")
        
        try:
            # Создаем атлас БЕЗ применения к объекту
            result = self.create_atlas_textures_only(context, selected_sets, atlas_size, final_atlas_type)
            
            if result:
                self.report({'INFO'}, f"Атлас создан: {result['atlas_name']}")
                
                # Обновляем список сетов
                bpy.ops.agr.refresh_texture_sets()
                
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
    
    def generate_procedural_atlas_name(self, context):
        """Генерирует процедурное имя атласа A_001, A_002, etc."""
        settings = context.scene.agr_baker_settings
        
        # Получаем базовую папку
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
        
        # Определяем типы текстур для атласа
        if atlas_type == 'HIGH':
            # HIGH: разделяем DO, E, R, M, N
            texture_types = ['DIFFUSE', 'EMIT', 'ROUGHNESS', 'METALLIC', 'NORMAL']
            if has_alpha:
                texture_types.insert(0, 'OPACITY')
        else:  # LOW
            # LOW: объединяем в ERM, дублируем D как DO
            texture_types = ['DIFFUSE', 'ERM', 'NORMAL']
            if has_alpha:
                texture_types.insert(0, 'OPACITY')
        
        # Получаем именование - процедурное A_001, A_002, etc.
        atlas_name = self.generate_procedural_atlas_name(context)
        
        print(f"📝 Имя атласа: {atlas_name}")
        print(f"📝 Типы текстур: {texture_types}")
        print(f"📝 Альфа-канал: {'Да' if has_alpha else 'Нет'}")
        
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
        """Создает текстуры для HIGH атласа (разделенные Diffuse, DiffuseOpacity, Emit, Roughness, Metallic, Normal)"""
        from PIL import Image
        created_atlases = {}
        
        # Diffuse
        print(f"\n🖼️ Создание Diffuse атласа")
        diffuse_atlas = self.create_atlas_for_type(texture_sets, 'DIFFUSE', atlas_size, layout, False)
        if diffuse_atlas:
            filename = f"T_{atlas_name}_Diffuse.png"
            filepath = os.path.join(output_path, filename)
            self.save_atlas_image(diffuse_atlas, filepath, 'DIFFUSE')
            created_atlases['DIFFUSE'] = filepath
            print(f"  ✅ Создан: {filename}")
            
            # Создаем DiffuseOpacity
            if has_alpha:
                # С альфа-каналом из Opacity
                print(f"  🔧 Создание DiffuseOpacity с альфа-каналом")
                opacity_atlas = self.create_atlas_for_type(texture_sets, 'OPACITY', atlas_size, layout, False)
                
                # Объединяем D + O в DO через PIL
                d_img = Image.open(filepath).convert('RGB')
                o_array = np.array(opacity_atlas.pixels[:]).reshape(atlas_size, atlas_size, 4)
                o_channel = (o_array[:, :, 0] * 255).astype(np.uint8)
                o_pil = Image.fromarray(o_channel, mode='L')
                
                # Создаем RGBA
                do_img = Image.new('RGBA', (atlas_size, atlas_size))
                do_img.paste(d_img, (0, 0))
                do_img.putalpha(o_pil)
                
                do_filename = f"T_{atlas_name}_DiffuseOpacity.png"
                do_filepath = os.path.join(output_path, do_filename)
                do_img.save(do_filepath)
                created_atlases['DIFFUSE_OPACITY'] = do_filepath
                print(f"  ✅ Создан DiffuseOpacity с альфа: {do_filename}")
                
                # Сохраняем Opacity отдельно
                opacity_filename = f"T_{atlas_name}_Opacity.png"
                opacity_filepath = os.path.join(output_path, opacity_filename)
                self.save_atlas_image(opacity_atlas, opacity_filepath, 'OPACITY')
                created_atlases['OPACITY'] = opacity_filepath
                print(f"  ✅ Создан Opacity: {opacity_filename}")
                
                bpy.data.images.remove(opacity_atlas)
            else:
                # Без альфа - просто копируем Diffuse как DiffuseOpacity (RGB)
                print(f"  🔧 Дублирование Diffuse как DiffuseOpacity (без альфа)")
                do_filename = f"T_{atlas_name}_DiffuseOpacity.png"
                do_filepath = os.path.join(output_path, do_filename)
                d_img = Image.open(filepath).convert('RGB')
                d_img.save(do_filepath)
                created_atlases['DIFFUSE_OPACITY'] = do_filepath
                print(f"  ✅ Создан DiffuseOpacity без альфа: {do_filename}")
            
            bpy.data.images.remove(diffuse_atlas)
        
        # Emit
        print(f"\n🖼️ Создание Emit атласа")
        emit_atlas = self.create_atlas_for_type(texture_sets, 'EMIT', atlas_size, layout, False)
        emit_filepath = None
        if emit_atlas:
            filename = f"T_{atlas_name}_Emit.png"
            filepath = os.path.join(output_path, filename)
            self.save_atlas_image(emit_atlas, filepath, 'EMIT')
            created_atlases['EMIT'] = filepath
            emit_filepath = filepath
            print(f"  ✅ Создан: {filename}")
            bpy.data.images.remove(emit_atlas)
        
        # Roughness
        print(f"\n🖼️ Создание Roughness атласа")
        roughness_atlas = self.create_atlas_for_type(texture_sets, 'ROUGHNESS', atlas_size, layout, False)
        roughness_filepath = None
        if roughness_atlas:
            filename = f"T_{atlas_name}_Roughness.png"
            filepath = os.path.join(output_path, filename)
            self.save_atlas_image(roughness_atlas, filepath, 'ROUGHNESS')
            created_atlases['ROUGHNESS'] = filepath
            roughness_filepath = filepath
            print(f"  ✅ Создан: {filename}")
            bpy.data.images.remove(roughness_atlas)
        
        # Metallic
        print(f"\n🖼️ Создание Metallic атласа")
        metallic_atlas = self.create_atlas_for_type(texture_sets, 'METALLIC', atlas_size, layout, False)
        metallic_filepath = None
        if metallic_atlas:
            filename = f"T_{atlas_name}_Metallic.png"
            filepath = os.path.join(output_path, filename)
            self.save_atlas_image(metallic_atlas, filepath, 'METALLIC')
            created_atlases['METALLIC'] = filepath
            metallic_filepath = filepath
            print(f"  ✅ Создан: {filename}")
            bpy.data.images.remove(metallic_atlas)
        
        # Создаем объединенную ERM текстуру из E, R, M
        if emit_filepath and roughness_filepath and metallic_filepath:
            print(f"\n🖼️ Создание объединенной ERM текстуры")
            e_img = Image.open(emit_filepath).convert('L')
            r_img = Image.open(roughness_filepath).convert('L')
            m_img = Image.open(metallic_filepath).convert('L')
            
            erm_img = Image.merge('RGB', (e_img, r_img, m_img))
            
            erm_filename = f"T_{atlas_name}_ERM.png"
            erm_filepath = os.path.join(output_path, erm_filename)
            erm_img.save(erm_filepath)
            created_atlases['ERM'] = erm_filepath
            print(f"  ✅ Создан ERM: {erm_filename}")
        
        # Opacity (всегда создаем из исходных сетов)
        print(f"\n🖼️ Создание Opacity атласа")
        opacity_atlas = self.create_atlas_for_type(texture_sets, 'OPACITY', atlas_size, layout, False)
        if opacity_atlas:
            filename = f"T_{atlas_name}_Opacity.png"
            filepath = os.path.join(output_path, filename)
            self.save_atlas_image(opacity_atlas, filepath, 'OPACITY')
            created_atlases['OPACITY'] = filepath
            print(f"  ✅ Создан: {filename}")
            bpy.data.images.remove(opacity_atlas)
        
        # Normal
        print(f"\n🖼️ Создание Normal атласа")
        normal_atlas = self.create_atlas_for_type(texture_sets, 'NORMAL', atlas_size, layout, False)
        if normal_atlas:
            filename = f"T_{atlas_name}_Normal.png"
            filepath = os.path.join(output_path, filename)
            self.save_atlas_image(normal_atlas, filepath, 'NORMAL')
            created_atlases['NORMAL'] = filepath
            print(f"  ✅ Создан: {filename}")
            bpy.data.images.remove(normal_atlas)
        
        return created_atlases
    
    def create_low_atlas_textures(self, texture_sets, atlas_size, layout, output_path, atlas_name, has_alpha):
        """Создает текстуры для LOW атласа (ERM объединенная, D дублируется как DO)"""
        from PIL import Image
        created_atlases = {}
        
        # Diffuse
        print(f"\n🖼️ Создание Diffuse атласа")
        diffuse_atlas = self.create_atlas_for_type(texture_sets, 'DIFFUSE', atlas_size, layout, False)
        if diffuse_atlas:
            filename = f"T_{atlas_name}_d.png"
            filepath = os.path.join(output_path, filename)
            self.save_atlas_image(diffuse_atlas, filepath, 'DIFFUSE')
            created_atlases['DIFFUSE'] = filepath
            print(f"  ✅ Создан: {filename}")
            
            # Дублируем D как DO
            do_filename = f"T_{atlas_name}_do.png"
            do_filepath = os.path.join(output_path, do_filename)
            
            if has_alpha:
                # Если есть альфа, создаем DO с альфа-каналом из Opacity
                print(f"  🔧 Создание DO с альфа-каналом")
                
                # Создаем Opacity atlas
                opacity_atlas = self.create_atlas_for_type(texture_sets, 'OPACITY', atlas_size, layout, False)
                
                # Объединяем D + O в DO через PIL
                d_img = Image.open(filepath).convert('RGB')
                
                # Конвертируем Blender image в PIL
                o_array = np.array(opacity_atlas.pixels[:]).reshape(atlas_size, atlas_size, 4)
                o_channel = (o_array[:, :, 0] * 255).astype(np.uint8)
                o_pil = Image.fromarray(o_channel, mode='L')
                
                # Создаем RGBA
                do_img = Image.new('RGBA', (atlas_size, atlas_size))
                do_img.paste(d_img, (0, 0))
                do_img.putalpha(o_pil)
                do_img.save(do_filepath)
                
                created_atlases['DIFFUSE_OPACITY'] = do_filepath
                print(f"  ✅ Создан DO с альфа: {do_filename}")
                
                # Сохраняем Opacity отдельно
                opacity_filename = f"T_{atlas_name}_o.png"
                opacity_filepath = os.path.join(output_path, opacity_filename)
                self.save_atlas_image(opacity_atlas, opacity_filepath, 'OPACITY')
                created_atlases['OPACITY'] = opacity_filepath
                print(f"  ✅ Создан Opacity: {opacity_filename}")
                
                bpy.data.images.remove(opacity_atlas)
            else:
                # Если нет альфа, просто копируем D как DO (RGB без альфа)
                print(f"  🔧 Дублирование D как DO (без альфа)")
                d_img = Image.open(filepath).convert('RGB')
                d_img.save(do_filepath)
                created_atlases['DIFFUSE_OPACITY'] = do_filepath
                print(f"  ✅ Создан DO без альфа: {do_filename}")
            
            bpy.data.images.remove(diffuse_atlas)
        
        # ERM (объединяем E, R, M в один файл)
        print(f"\n🖼️ Создание ERM атласа")
        erm_atlas = self.create_erm_atlas(texture_sets, atlas_size, layout)
        if erm_atlas:
            filename = f"T_{atlas_name}_erm.png"
            filepath = os.path.join(output_path, filename)
            self.save_atlas_image(erm_atlas, filepath, 'ERM')
            created_atlases['ERM'] = filepath
            print(f"  ✅ Создан: {filename}")
            bpy.data.images.remove(erm_atlas)
        
        # Сохраняем отдельные каналы для LOW (r, m)
        print(f"\n🖼️ Создание отдельных каналов")
        
        # Roughness
        roughness_atlas = self.create_atlas_for_type(texture_sets, 'ROUGHNESS', atlas_size, layout, False)
        if roughness_atlas:
            filename = f"T_{atlas_name}_r.png"
            filepath = os.path.join(output_path, filename)
            self.save_atlas_image(roughness_atlas, filepath, 'ROUGHNESS')
            created_atlases['ROUGHNESS'] = filepath
            print(f"  ✅ Создан: {filename}")
            bpy.data.images.remove(roughness_atlas)
        
        # Metallic
        metallic_atlas = self.create_atlas_for_type(texture_sets, 'METALLIC', atlas_size, layout, False)
        if metallic_atlas:
            filename = f"T_{atlas_name}_m.png"
            filepath = os.path.join(output_path, filename)
            self.save_atlas_image(metallic_atlas, filepath, 'METALLIC')
            created_atlases['METALLIC'] = filepath
            print(f"  ✅ Создан: {filename}")
            bpy.data.images.remove(metallic_atlas)
        
        # Normal
        print(f"\n🖼️ Создание Normal атласа")
        normal_atlas = self.create_atlas_for_type(texture_sets, 'NORMAL', atlas_size, layout, False)
        if normal_atlas:
            filename = f"T_{atlas_name}_n.png"
            filepath = os.path.join(output_path, filename)
            self.save_atlas_image(normal_atlas, filepath, 'NORMAL')
            created_atlases['NORMAL'] = filepath
            print(f"  ✅ Создан: {filename}")
            bpy.data.images.remove(normal_atlas)
        
        return created_atlases
    
    def create_erm_atlas(self, texture_sets, atlas_size, layout):
        """Создает ERM атлас (объединяет E, R, M в RGB каналы)"""
        from PIL import Image
        
        atlas_name = f"Atlas_ERM_{atlas_size}"
        
        # Удаляем старое изображение если есть
        if atlas_name in bpy.data.images:
            bpy.data.images.remove(bpy.data.images[atlas_name])
        
        # Создаем новое изображение
        atlas_image = bpy.data.images.new(
            atlas_name,
            width=atlas_size,
            height=atlas_size,
            alpha=False,
            float_buffer=False
        )
        
        atlas_image.colorspace_settings.name = 'Non-Color'
        
        # Заполняем черным цветом
        fill = [0.0, 0.0, 0.0, 1.0]
        total_px = atlas_size * atlas_size * 4
        buf = fill * (total_px // 4)
        atlas_image.pixels = buf
        
        # Создаем numpy массив для работы
        atlas_array = np.array(atlas_image.pixels[:]).reshape(atlas_size, atlas_size, 4)
        
        # Размещаем текстуры в атласе
        for item in layout:
            # Получаем пути к E, R, M текстурам
            emit_path = self.get_texture_path(item['texture_set'], 'EMIT')
            roughness_path = self.get_texture_path(item['texture_set'], 'ROUGHNESS')
            metallic_path = self.get_texture_path(item['texture_set'], 'METALLIC')
            
            cell_width = item['width']
            cell_height = item['height']
            x = item['x']
            y = item['y']
            
            # Загружаем каналы через PIL
            e_channel = None
            r_channel = None
            m_channel = None
            
            if emit_path and os.path.exists(emit_path):
                e_img = Image.open(emit_path).convert('L')
                if e_img.size != (cell_width, cell_height):
                    e_img = e_img.resize((cell_width, cell_height), Image.Resampling.LANCZOS)
                e_channel = np.array(e_img, dtype=np.float32) / 255.0
            
            if roughness_path and os.path.exists(roughness_path):
                r_img = Image.open(roughness_path).convert('L')
                if r_img.size != (cell_width, cell_height):
                    r_img = r_img.resize((cell_width, cell_height), Image.Resampling.LANCZOS)
                r_channel = np.array(r_img, dtype=np.float32) / 255.0
            
            if metallic_path and os.path.exists(metallic_path):
                m_img = Image.open(metallic_path).convert('L')
                if m_img.size != (cell_width, cell_height):
                    m_img = m_img.resize((cell_width, cell_height), Image.Resampling.LANCZOS)
                m_channel = np.array(m_img, dtype=np.float32) / 255.0
            
            # Создаем ERM текстуру
            if e_channel is None:
                e_channel = np.zeros((cell_height, cell_width), dtype=np.float32)
            if r_channel is None:
                r_channel = np.ones((cell_height, cell_width), dtype=np.float32) * 0.5
            if m_channel is None:
                m_channel = np.zeros((cell_height, cell_width), dtype=np.float32)
            
            # Размещаем в атласе
            atlas_array[y:y+cell_height, x:x+cell_width, 0] = e_channel
            atlas_array[y:y+cell_height, x:x+cell_width, 1] = r_channel
            atlas_array[y:y+cell_height, x:x+cell_width, 2] = m_channel
            atlas_array[y:y+cell_height, x:x+cell_width, 3] = 1.0
            
            print(f"  📍 Размещена ERM: {item['texture_set'].name} в ({x}, {y})")
        
        # Записываем массив обратно в изображение
        flat = atlas_array.flatten().tolist()
        atlas_image.pixels = flat
        atlas_image.update()
        
        return atlas_image
    
    def create_atlas_for_type(self, texture_sets, texture_type, atlas_size, layout, with_alpha=False):
        """Создает атлас для конкретного типа текстуры"""
        atlas_name = f"Atlas_{texture_type}_{atlas_size}"
        
        # Удаляем старое изображение если есть
        if atlas_name in bpy.data.images:
            bpy.data.images.remove(bpy.data.images[atlas_name])
        
        # Создаем новое изображение
        atlas_image = bpy.data.images.new(
            atlas_name,
            width=atlas_size,
            height=atlas_size,
            alpha=with_alpha,
            float_buffer=False
        )
        
        # Устанавливаем цветовое пространство
        if texture_type in ['DIFFUSE', 'DIFFUSE_OPACITY']:
            atlas_image.colorspace_settings.name = 'sRGB'
        else:
            atlas_image.colorspace_settings.name = 'Non-Color'
        
        # Заполняем черным цветом
        if with_alpha:
            fill = [0.0, 0.0, 0.0, 0.0]
        else:
            fill = [0.0, 0.0, 0.0, 1.0]
        
        total_px = atlas_size * atlas_size * 4
        buf = fill * (total_px // 4)
        atlas_image.pixels = buf
        
        # Создаем numpy массив для работы
        atlas_array = np.array(atlas_image.pixels[:]).reshape(atlas_size, atlas_size, 4)
        
        # Размещаем текстуры в атласе
        for item in layout:
            texture_path = self.get_texture_path(item['texture_set'], texture_type)
            
            if texture_path and os.path.exists(texture_path):
                self.place_texture_in_atlas(atlas_array, texture_path, item)
            else:
                print(f"  ⚠️ Текстура не найдена: {texture_type} для {item['texture_set'].name}")
        
        # Записываем массив обратно в изображение
        flat = atlas_array.flatten().tolist()
        atlas_image.pixels = flat
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
        
        return None
    
    def place_texture_in_atlas(self, atlas_array, texture_path, layout_item):
        """Размещает текстуру в атласе с масштабированием при необходимости"""
        try:
            cell_width = layout_item['width']
            cell_height = layout_item['height']
            
            # Используем Pillow для качественного масштабирования
            if PILLOW_AVAILABLE:
                from PIL import Image
                # Загружаем через Pillow
                pil_img = Image.open(texture_path)
                
                # Масштабируем если нужно с использованием LANCZOS
                if pil_img.size != (cell_width, cell_height):
                    pil_img = pil_img.resize((cell_width, cell_height), Image.Resampling.LANCZOS)
                
                # Конвертируем в RGBA если нужно
                if pil_img.mode != 'RGBA':
                    pil_img = pil_img.convert('RGBA')
                
                # Конвертируем в numpy массив (0-255) и нормализуем в 0-1
                tex_array = np.array(pil_img, dtype=np.float32) / 255.0
                
            else:
                # Fallback: загружаем через Blender
                temp_img = bpy.data.images.load(texture_path)
                temp_img.update()
                _ = temp_img.pixels[0]
                
                tex_width = temp_img.size[0]
                tex_height = temp_img.size[1]
                tex_array = np.array(temp_img.pixels[:]).reshape(tex_height, tex_width, 4)
                
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
            print(f"  ❌ Ошибка размещения {texture_path}: {e}")
    
    def save_atlas_image(self, image, filepath, texture_type):
        """Сохраняет изображение атласа"""
        scene = bpy.context.scene
        
        # Сохраняем оригинальные настройки
        original_format = scene.render.image_settings.file_format
        original_color_mode = scene.render.image_settings.color_mode
        original_color_depth = scene.render.image_settings.color_depth
        original_view_settings = scene.view_settings.view_transform
        original_look = scene.view_settings.look
        original_display_device = scene.display_settings.display_device
        
        # Устанавливаем настройки для сохранения
        scene.render.image_settings.file_format = 'PNG'
        scene.render.image_settings.color_depth = '8'
        scene.render.image_settings.compression = 15
        scene.view_settings.view_transform = 'Standard'
        scene.view_settings.look = 'None'
        scene.display_settings.display_device = 'sRGB'
        
        # Определяем режим цвета
        if texture_type in ['DIFFUSE_OPACITY', 'OPACITY']:
            scene.render.image_settings.color_mode = 'RGBA'
        else:
            scene.render.image_settings.color_mode = 'RGB'
        
        try:
            image.filepath_raw = filepath
            image.save_render(filepath)
            print(f"  💾 Сохранен: {os.path.basename(filepath)}")
        except Exception as e:
            print(f"  ❌ Ошибка сохранения {filepath}: {e}")
        finally:
            # Восстанавливаем настройки
            scene.render.image_settings.file_format = original_format
            scene.render.image_settings.color_mode = original_color_mode
            scene.render.image_settings.color_depth = original_color_depth
            scene.view_settings.view_transform = original_view_settings
            scene.view_settings.look = original_look
            scene.display_settings.display_device = original_display_device
    
    def save_atlas_mapping(self, output_path, atlas_name, atlas_type, atlas_size, layout, created_atlases):
        """Сохраняет информацию о раскладке атласа в JSON"""
        try:
            mapping = {
                'atlas_name': atlas_name,
                'atlas_type': atlas_type,
                'atlas_size': atlas_size,
                'created_atlases': created_atlases,
                'layout': [
                    {
                        'set_name': item['texture_set'].name,
                        'material_name': item['texture_set'].material_name,
                        'x': item['x'],
                        'y': item['y'],
                        'width': item['width'],
                        'height': item['height'],
                        'u_min': item['u_min'],
                        'v_min': item['v_min'],
                        'u_max': item['u_max'],
                        'v_max': item['v_max']
                    }
                    for item in layout
                ]
            }
            
            mapping_path = os.path.join(output_path, 'atlas_mapping.json')
            with open(mapping_path, 'w', encoding='utf-8') as f:
                json.dump(mapping, f, ensure_ascii=False, indent=2)
            
            print(f"💾 Сохранен atlas_mapping.json")
            
        except Exception as e:
            print(f"⚠️ Не удалось сохранить atlas_mapping.json: {e}")


# ===== CREATE ATLAS FROM OBJECT OPERATOR =====

class AGR_OT_CreateAtlasFromObject(Operator):
    """Create atlas from object materials, assign material and layout UVs"""
    bl_idname = "agr.create_atlas_from_object"
    bl_label = "Create Atlas from Object"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == 'MESH' and len(obj.material_slots) > 0
    
    def execute(self, context):
        obj = context.active_object
        settings = context.scene.agr_baker_settings
        texture_sets_list = context.scene.agr_texture_sets
        
        # Собираем все материалы объекта
        material_names = []
        for slot in obj.material_slots:
            if slot.material:
                material_names.append(slot.material.name)
        
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
        
        atlas_size = int(settings.atlas_size)
        
        # Проверяем, можно ли упаковать
        total_area = sum(s.resolution * s.resolution for s in object_sets)
        if total_area > atlas_size * atlas_size:
            self.report({'ERROR'}, f"Текстуры не помещаются в атлас {atlas_size}x{atlas_size}")
            return {'CANCELLED'}
        
        # Определяем тип атласа на основе имени объекта
        try:
            address, obj_type = process_object_name(obj.name)
            if obj_type in ['Main', 'Flora', 'Ground', 'GroundEl']:
                atlas_type = 'LOW'
                use_low_naming = True
            else:
                atlas_type = 'HIGH'
                use_low_naming = False
        except:
            atlas_type = 'HIGH'
            use_low_naming = False
        
        print(f"\n{'='*60}")
        print(f"🎨 СОЗДАНИЕ АТЛАСА ИЗ МАТЕРИАЛОВ ОБЪЕКТА")
        print(f"{'='*60}")
        print(f"Объект: {obj.name}")
        print(f"Тип атласа: {atlas_type}")
        print(f"Размер атласа: {atlas_size}x{atlas_size}")
        print(f"Количество материалов: {len(object_sets)}")
        
        try:
            # Создаем атлас
            result = self.create_and_apply_atlas(context, obj, object_sets, atlas_size, atlas_type, use_low_naming)
            
            if result:
                self.report({'INFO'}, f"Атлас создан и применен: {result['atlas_name']}")
                
                # Обновляем список сетов
                bpy.ops.agr.refresh_texture_sets()
                
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
        
        # Проверяем наличие альфа-канала
        has_alpha = check_sets_have_alpha(texture_sets)
        
        # Получаем именование
        if use_low_naming:
            try:
                address, obj_type = process_object_name(obj.name)
                atlas_name = f"A_{address}_{obj_type}"
                material_name = f"M_{address}_{obj_type}_1"
            except:
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
        
        # Сохраняем atlas_mapping.json
        self.save_atlas_mapping(atlas_output_path, atlas_name, atlas_type, atlas_size, layout, created_atlases)
        
        # Создаем материал
        atlas_material = self.create_atlas_material(
            context, atlas_name, material_name, created_atlases, atlas_type
        )
        
        # Применяем к объекту
        self.apply_atlas_to_object(context, obj, atlas_material, layout)
        
        print(f"\n✅ Атлас создан и применен!")
        print(f"{'='*60}\n")
        
        return {
            'atlas_name': atlas_name,
            'material_name': material_name,
            'output_path': atlas_output_path,
            'atlases': created_atlases,
            'material': atlas_material
        }
    
    def create_high_atlas_textures(self, texture_sets, atlas_size, layout, output_path, atlas_name, has_alpha, use_low_naming):
        """Создает текстуры для HIGH атласа (сначала DO, потом разделяем на D и O)"""
        from PIL import Image
        created_atlases = {}
        
        # Получаем address и obj_type если LOW naming
        address = None
        obj_type = None
        if use_low_naming:
            try:
                address, obj_type = process_object_name(bpy.context.active_object.name)
            except:
                pass
        
        # Создаем DO из DiffuseOpacity текстур
        print(f"\n🖼️ Создание DO атласа")
        do_atlas = self.create_atlas_for_type(texture_sets, 'DIFFUSE_OPACITY', atlas_size, layout, has_alpha)
        
        if do_atlas:
            # Сохраняем DO
            do_filename = get_texture_filename(atlas_name, 'DIFFUSE_OPACITY', use_low_naming, address, obj_type)
            do_filepath = os.path.join(output_path, do_filename)
            
            if has_alpha:
                # DO с альфа-каналом - сохраняем как RGBA
                self.save_atlas_image(do_atlas, do_filepath, 'DIFFUSE_OPACITY')
                created_atlases['DIFFUSE_OPACITY'] = do_filepath
                print(f"  ✅ Создан DO с альфа: {do_filename}")
                
                # Разделяем DO на D и O через PIL
                print(f"  🔧 Разделение DO на D и O")
                do_img = Image.open(do_filepath)
                
                # D - RGB часть
                d_img = do_img.convert('RGB')
                d_filename = get_texture_filename(atlas_name, 'DIFFUSE', use_low_naming, address, obj_type)
                if not d_filename:
                    d_filename = f"T_{atlas_name}_Diffuse.png"
                d_filepath = os.path.join(output_path, d_filename)
                d_img.save(d_filepath)
                created_atlases['DIFFUSE'] = d_filepath
                print(f"  ✅ Создан Diffuse: {d_filename}")
                
                # O - Alpha канал
                if do_img.mode in ('RGBA', 'LA'):
                    o_channel = do_img.split()[-1]
                    o_filename = get_texture_filename(atlas_name, 'OPACITY', use_low_naming, address, obj_type)
                    if not o_filename:
                        o_filename = f"T_{atlas_name}_Opacity.png"
                    o_filepath = os.path.join(output_path, o_filename)
                    o_channel.save(o_filepath)
                    created_atlases['OPACITY'] = o_filepath
                    print(f"  ✅ Создан Opacity: {o_filename}")
            else:
                # DO без альфа - сохраняем как RGB
                self.save_atlas_image(do_atlas, do_filepath, 'DIFFUSE')
                created_atlases['DIFFUSE_OPACITY'] = do_filepath
                print(f"  ✅ Создан DiffuseOpacity без альфа: {do_filename}")
                
                # D - просто копия DO (без альфа)
                do_img = Image.open(do_filepath).convert('RGB')
                d_filename = get_texture_filename(atlas_name, 'DIFFUSE', use_low_naming, address, obj_type)
                if not d_filename:
                    d_filename = f"T_{atlas_name}_Diffuse.png"
                d_filepath = os.path.join(output_path, d_filename)
                do_img.save(d_filepath)
                created_atlases['DIFFUSE'] = d_filepath
                print(f"  ✅ Создан Diffuse: {d_filename}")
            
            bpy.data.images.remove(do_atlas)
        
        # ERM (разделяем на отдельные карты для HIGH + создаем объединенную ERM)
        print(f"\n🖼️ Создание E, R, M атласов")
        
        # Emit
        emit_atlas = self.create_atlas_for_type(texture_sets, 'EMIT', atlas_size, layout, False)
        emit_filepath = None
        if emit_atlas:
            filename = get_texture_filename(atlas_name, 'EMIT', use_low_naming, address, obj_type)
            if not filename:
                filename = f"T_{atlas_name}_Emit.png"
            filepath = os.path.join(output_path, filename)
            self.save_atlas_image(emit_atlas, filepath, 'EMIT')
            created_atlases['EMIT'] = filepath
            emit_filepath = filepath
            print(f"  ✅ Создан Emit: {filename}")
            bpy.data.images.remove(emit_atlas)
        
        # Roughness
        roughness_atlas = self.create_atlas_for_type(texture_sets, 'ROUGHNESS', atlas_size, layout, False)
        roughness_filepath = None
        if roughness_atlas:
            filename = get_texture_filename(atlas_name, 'ROUGHNESS', use_low_naming, address, obj_type)
            if not filename:
                filename = f"T_{atlas_name}_Roughness.png"
            filepath = os.path.join(output_path, filename)
            self.save_atlas_image(roughness_atlas, filepath, 'ROUGHNESS')
            created_atlases['ROUGHNESS'] = filepath
            roughness_filepath = filepath
            print(f"  ✅ Создан Roughness: {filename}")
            bpy.data.images.remove(roughness_atlas)
        
        # Metallic
        metallic_atlas = self.create_atlas_for_type(texture_sets, 'METALLIC', atlas_size, layout, False)
        metallic_filepath = None
        if metallic_atlas:
            filename = get_texture_filename(atlas_name, 'METALLIC', use_low_naming, address, obj_type)
            if not filename:
                filename = f"T_{atlas_name}_Metallic.png"
            filepath = os.path.join(output_path, filename)
            self.save_atlas_image(metallic_atlas, filepath, 'METALLIC')
            created_atlases['METALLIC'] = filepath
            metallic_filepath = filepath
            print(f"  ✅ Создан Metallic: {filename}")
            bpy.data.images.remove(metallic_atlas)
        
        # Создаем объединенную ERM текстуру из E, R, M
        if emit_filepath and roughness_filepath and metallic_filepath:
            print(f"\n🖼️ Создание объединенной ERM текстуры")
            e_img = Image.open(emit_filepath).convert('L')
            r_img = Image.open(roughness_filepath).convert('L')
            m_img = Image.open(metallic_filepath).convert('L')
            
            erm_img = Image.merge('RGB', (e_img, r_img, m_img))
            
            erm_filename = get_texture_filename(atlas_name, 'ERM', use_low_naming, address, obj_type)
            if not erm_filename:
                erm_filename = f"T_{atlas_name}_ERM.png"
            erm_filepath = os.path.join(output_path, erm_filename)
            erm_img.save(erm_filepath)
            created_atlases['ERM'] = erm_filepath
            print(f"  ✅ Создан ERM: {erm_filename}")
        
        # Opacity (всегда создаем из исходных сетов)
        print(f"\n🖼️ Создание Opacity атласа")
        opacity_atlas = self.create_atlas_for_type(texture_sets, 'OPACITY', atlas_size, layout, False)
        if opacity_atlas:
            filename = get_texture_filename(atlas_name, 'OPACITY', use_low_naming, address, obj_type)
            if not filename:
                filename = f"T_{atlas_name}_Opacity.png"
            filepath = os.path.join(output_path, filename)
            self.save_atlas_image(opacity_atlas, filepath, 'OPACITY')
            created_atlases['OPACITY'] = filepath
            print(f"  ✅ Создан Opacity: {filename}")
            bpy.data.images.remove(opacity_atlas)
        
        # Normal
        print(f"\n🖼️ Создание Normal атласа")
        normal_atlas = self.create_atlas_for_type(texture_sets, 'NORMAL', atlas_size, layout, False)
        if normal_atlas:
            filename = get_texture_filename(atlas_name, 'NORMAL', use_low_naming, address, obj_type)
            if not filename:
                filename = f"T_{atlas_name}_Normal.png"
            filepath = os.path.join(output_path, filename)
            self.save_atlas_image(normal_atlas, filepath, 'NORMAL')
            created_atlases['NORMAL'] = filepath
            print(f"  ✅ Создан Normal: {filename}")
            bpy.data.images.remove(normal_atlas)
        
        return created_atlases
    
    def create_low_atlas_textures(self, texture_sets, atlas_size, layout, output_path, atlas_name, has_alpha, use_low_naming):
        """Создает текстуры для LOW атласа (сначала DO из DiffuseOpacity, потом ERM объединенная)"""
        from PIL import Image
        created_atlases = {}
        
        # Получаем address и obj_type
        address = None
        obj_type = None
        if use_low_naming:
            try:
                address, obj_type = process_object_name(bpy.context.active_object.name)
            except:
                pass
        
        # Создаем DO из DiffuseOpacity текстур (или Diffuse + Opacity)
        print(f"\n🖼️ Создание DO атласа")
        
        # Пробуем создать из DiffuseOpacity
        do_atlas = self.create_atlas_for_type(texture_sets, 'DIFFUSE_OPACITY', atlas_size, layout, has_alpha)
        
        if do_atlas:
            # Сохраняем DO
            do_filename = get_texture_filename(atlas_name, 'DIFFUSE_OPACITY', use_low_naming, address, obj_type)
            if not do_filename:
                do_filename = f"T_{atlas_name}_DO.png"
            do_filepath = os.path.join(output_path, do_filename)
            
            if has_alpha:
                # DO с альфа-каналом
                self.save_atlas_image(do_atlas, do_filepath, 'DIFFUSE_OPACITY')
                created_atlases['DIFFUSE_OPACITY'] = do_filepath
                print(f"  ✅ Создан DO с альфа: {do_filename}")
                
                # Разделяем DO на D (для совместимости)
                print(f"  🔧 Извлечение D из DO")
                do_img = Image.open(do_filepath)
                d_img = do_img.convert('RGB')
                d_filename = get_texture_filename(atlas_name, 'DIFFUSE', use_low_naming, address, obj_type)
                if not d_filename:
                    d_filename = f"T_{atlas_name}_d.png"
                d_filepath = os.path.join(output_path, d_filename)
                d_img.save(d_filepath)
                created_atlases['DIFFUSE'] = d_filepath
                print(f"  ✅ Создан D: {d_filename}")
            else:
                # DO без альфа
                self.save_atlas_image(do_atlas, do_filepath, 'DIFFUSE')
                created_atlases['DIFFUSE_OPACITY'] = do_filepath
                print(f"  ✅ Создан DO без альфа: {do_filename}")
                
                # D - копия DO
                do_img = Image.open(do_filepath).convert('RGB')
                d_filename = get_texture_filename(atlas_name, 'DIFFUSE', use_low_naming, address, obj_type)
                if not d_filename:
                    d_filename = f"T_{atlas_name}_d.png"
                d_filepath = os.path.join(output_path, d_filename)
                do_img.save(d_filepath)
                created_atlases['DIFFUSE'] = d_filepath
                print(f"  ✅ Создан D: {d_filename}")
            
            bpy.data.images.remove(do_atlas)
        else:
            # Fallback: создаем из отдельных Diffuse и Opacity
            print(f"  ⚠️ DiffuseOpacity не найдена, создаем из Diffuse + Opacity")
            diffuse_atlas = self.create_atlas_for_type(texture_sets, 'DIFFUSE', atlas_size, layout, False)
            
            if diffuse_atlas:
                d_filename = get_texture_filename(atlas_name, 'DIFFUSE', use_low_naming, address, obj_type)
                if not d_filename:
                    d_filename = f"T_{atlas_name}_d.png"
                d_filepath = os.path.join(output_path, d_filename)
                self.save_atlas_image(diffuse_atlas, d_filepath, 'DIFFUSE')
                created_atlases['DIFFUSE'] = d_filepath
                
                # Создаем DO
                do_filename = get_texture_filename(atlas_name, 'DIFFUSE_OPACITY', use_low_naming, address, obj_type)
                if not do_filename:
                    do_filename = f"T_{atlas_name}_DO.png"
                do_filepath = os.path.join(output_path, do_filename)
                
                if has_alpha:
                    # Объединяем D + O в DO
                    opacity_atlas = self.create_atlas_for_type(texture_sets, 'OPACITY', atlas_size, layout, False)
                    
                    d_img = Image.open(d_filepath).convert('RGB')
                    o_array = np.array(opacity_atlas.pixels[:]).reshape(atlas_size, atlas_size, 4)
                    o_channel = (o_array[:, :, 0] * 255).astype(np.uint8)
                    o_pil = Image.fromarray(o_channel, mode='L')
                    
                    do_img = Image.new('RGBA', (atlas_size, atlas_size))
                    do_img.paste(d_img, (0, 0))
                    do_img.putalpha(o_pil)
                    do_img.save(do_filepath)
                    
                    created_atlases['DIFFUSE_OPACITY'] = do_filepath
                    print(f"  ✅ Создан DO с альфа: {do_filename}")
                    
                    bpy.data.images.remove(opacity_atlas)
                else:
                    # DO без альфа - просто копия D
                    d_img = Image.open(d_filepath).convert('RGB')
                    d_img.save(do_filepath)
                    created_atlases['DIFFUSE_OPACITY'] = do_filepath
                    print(f"  ✅ Создан DO без альфа: {do_filename}")
                
                bpy.data.images.remove(diffuse_atlas)
        
        # ERM (объединяем E, R, M в один файл) + отдельные каналы для LOW
        print(f"\n🖼️ Создание ERM атласа")
        erm_atlas = self.create_erm_atlas(texture_sets, atlas_size, layout)
        if erm_atlas:
            filename = get_texture_filename(atlas_name, 'ERM', use_low_naming, address, obj_type)
            if not filename:
                filename = f"T_{atlas_name}_erm.png"
            filepath = os.path.join(output_path, filename)
            self.save_atlas_image(erm_atlas, filepath, 'ERM')
            created_atlases['ERM'] = filepath
            print(f"  ✅ Создан: {os.path.basename(filename)}")
            bpy.data.images.remove(erm_atlas)
        
        # Создаем отдельные каналы r, m для LOW
        print(f"\n🖼️ Создание отдельных каналов")
        
        # Roughness
        roughness_atlas = self.create_atlas_for_type(texture_sets, 'ROUGHNESS', atlas_size, layout, False)
        if roughness_atlas:
            filename = get_texture_filename(atlas_name, 'ROUGHNESS', use_low_naming, address, obj_type)
            if not filename:
                filename = f"T_{atlas_name}_r.png"
            filepath = os.path.join(output_path, filename)
            self.save_atlas_image(roughness_atlas, filepath, 'ROUGHNESS')
            created_atlases['ROUGHNESS'] = filepath
            print(f"  ✅ Создан: {os.path.basename(filename)}")
            bpy.data.images.remove(roughness_atlas)
        
        # Metallic
        metallic_atlas = self.create_atlas_for_type(texture_sets, 'METALLIC', atlas_size, layout, False)
        if metallic_atlas:
            filename = get_texture_filename(atlas_name, 'METALLIC', use_low_naming, address, obj_type)
            if not filename:
                filename = f"T_{atlas_name}_m.png"
            filepath = os.path.join(output_path, filename)
            self.save_atlas_image(metallic_atlas, filepath, 'METALLIC')
            created_atlases['METALLIC'] = filepath
            print(f"  ✅ Создан: {os.path.basename(filename)}")
            bpy.data.images.remove(metallic_atlas)
        
        # Emit
        emit_atlas = self.create_atlas_for_type(texture_sets, 'EMIT', atlas_size, layout, False)
        if emit_atlas:
            filename = get_texture_filename(atlas_name, 'EMIT', use_low_naming, address, obj_type)
            if not filename:
                filename = f"T_{atlas_name}_e.png"
            filepath = os.path.join(output_path, filename)
            self.save_atlas_image(emit_atlas, filepath, 'EMIT')
            created_atlases['EMIT'] = filepath
            print(f"  ✅ Создан: {os.path.basename(filename)}")
            bpy.data.images.remove(emit_atlas)
        
        # Opacity (всегда создаем из исходных сетов)
        print(f"\n🖼️ Создание Opacity атласа")
        opacity_atlas = self.create_atlas_for_type(texture_sets, 'OPACITY', atlas_size, layout, False)
        if opacity_atlas:
            filename = get_texture_filename(atlas_name, 'OPACITY', use_low_naming, address, obj_type)
            if not filename:
                filename = f"T_{atlas_name}_o.png"
            filepath = os.path.join(output_path, filename)
            self.save_atlas_image(opacity_atlas, filepath, 'OPACITY')
            created_atlases['OPACITY'] = filepath
            print(f"  ✅ Создан: {os.path.basename(filename)}")
            bpy.data.images.remove(opacity_atlas)
        
        # Normal
        print(f"\n🖼️ Создание Normal атласа")
        normal_atlas = self.create_atlas_for_type(texture_sets, 'NORMAL', atlas_size, layout, False)
        if normal_atlas:
            filename = get_texture_filename(atlas_name, 'NORMAL', use_low_naming, address, obj_type)
            if not filename:
                filename = f"T_{atlas_name}_n.png"
            filepath = os.path.join(output_path, filename)
            self.save_atlas_image(normal_atlas, filepath, 'NORMAL')
            created_atlases['NORMAL'] = filepath
            bpy.data.images.remove(normal_atlas)
        
        return created_atlases
    
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
        
        fill = [0.0, 0.0, 0.0, 1.0]
        total_px = atlas_size * atlas_size * 4
        buf = fill * (total_px // 4)
        atlas_image.pixels = buf
        
        atlas_array = np.array(atlas_image.pixels[:]).reshape(atlas_size, atlas_size, 4)
        
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
                e_img = Image.open(emit_path).convert('L')
                if e_img.size != (cell_width, cell_height):
                    e_img = e_img.resize((cell_width, cell_height), Image.Resampling.LANCZOS)
                e_channel = np.array(e_img, dtype=np.float32) / 255.0
            
            if roughness_path and os.path.exists(roughness_path):
                r_img = Image.open(roughness_path).convert('L')
                if r_img.size != (cell_width, cell_height):
                    r_img = r_img.resize((cell_width, cell_height), Image.Resampling.LANCZOS)
                r_channel = np.array(r_img, dtype=np.float32) / 255.0
            
            if metallic_path and os.path.exists(metallic_path):
                m_img = Image.open(metallic_path).convert('L')
                if m_img.size != (cell_width, cell_height):
                    m_img = m_img.resize((cell_width, cell_height), Image.Resampling.LANCZOS)
                m_channel = np.array(m_img, dtype=np.float32) / 255.0
            
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
        
        flat = atlas_array.flatten().tolist()
        atlas_image.pixels = flat
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
        
        if with_alpha:
            fill = [0.0, 0.0, 0.0, 0.0]
        else:
            fill = [0.0, 0.0, 0.0, 1.0]
        
        total_px = atlas_size * atlas_size * 4
        buf = fill * (total_px // 4)
        atlas_image.pixels = buf
        
        atlas_array = np.array(atlas_image.pixels[:]).reshape(atlas_size, atlas_size, 4)
        
        for item in layout:
            texture_path = self.get_texture_path(item['texture_set'], texture_type)
            
            if texture_path and os.path.exists(texture_path):
                self.place_texture_in_atlas(atlas_array, texture_path, item)
            else:
                print(f"  ⚠️ Текстура не найдена: {texture_type} для {item['texture_set'].name}")
        
        flat = atlas_array.flatten().tolist()
        atlas_image.pixels = flat
        atlas_image.update()
        
        return atlas_image
    
    def get_texture_path(self, texture_set, texture_type):
        """Получает путь к файлу текстуры заданного типа"""
        material_name = texture_set.material_name
        folder_path = texture_set.folder_path
        
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
        
        return None
    
    def place_texture_in_atlas(self, atlas_array, texture_path, layout_item):
        """Размещает текстуру в атласе с масштабированием при необходимости"""
        try:
            cell_width = layout_item['width']
            cell_height = layout_item['height']
            
            if PILLOW_AVAILABLE:
                from PIL import Image
                pil_img = Image.open(texture_path)
                
                if pil_img.size != (cell_width, cell_height):
                    pil_img = pil_img.resize((cell_width, cell_height), Image.Resampling.LANCZOS)
                
                if pil_img.mode != 'RGBA':
                    pil_img = pil_img.convert('RGBA')
                
                tex_array = np.array(pil_img, dtype=np.float32) / 255.0
                
            else:
                temp_img = bpy.data.images.load(texture_path)
                temp_img.update()
                _ = temp_img.pixels[0]
                
                tex_width = temp_img.size[0]
                tex_height = temp_img.size[1]
                tex_array = np.array(temp_img.pixels[:]).reshape(tex_height, tex_width, 4)
                
                if tex_width != cell_width or tex_height != cell_height:
                    indices_y = np.round(np.linspace(0, tex_height - 1, cell_height)).astype(int)
                    indices_x = np.round(np.linspace(0, tex_width - 1, cell_width)).astype(int)
                    tex_array = tex_array[np.ix_(indices_y, indices_x)]
                
                if temp_img.name in bpy.data.images:
                    bpy.data.images.remove(temp_img)
            
            x = layout_item['x']
            y = layout_item['y']
            atlas_array[y:y+cell_height, x:x+cell_width, :] = tex_array
            
        except Exception as e:
            print(f"  ❌ Ошибка размещения {texture_path}: {e}")
    
    def save_atlas_image(self, image, filepath, texture_type):
        """Сохраняет изображение атласа"""
        scene = bpy.context.scene
        
        original_format = scene.render.image_settings.file_format
        original_color_mode = scene.render.image_settings.color_mode
        original_color_depth = scene.render.image_settings.color_depth
        original_view_settings = scene.view_settings.view_transform
        original_look = scene.view_settings.look
        original_display_device = scene.display_settings.display_device
        
        scene.render.image_settings.file_format = 'PNG'
        scene.render.image_settings.color_depth = '8'
        scene.render.image_settings.compression = 15
        scene.view_settings.view_transform = 'Standard'
        scene.view_settings.look = 'None'
        scene.display_settings.display_device = 'sRGB'
        
        if texture_type in ['DIFFUSE_OPACITY', 'OPACITY']:
            scene.render.image_settings.color_mode = 'RGBA'
        else:
            scene.render.image_settings.color_mode = 'RGB'
        
        try:
            image.filepath_raw = filepath
            image.save_render(filepath)
            print(f"  💾 Сохранен: {os.path.basename(filepath)}")
        except Exception as e:
            print(f"  ❌ Ошибка сохранения {filepath}: {e}")
        finally:
            scene.render.image_settings.file_format = original_format
            scene.render.image_settings.color_mode = original_color_mode
            scene.render.image_settings.color_depth = original_color_depth
            scene.view_settings.view_transform = original_view_settings
            scene.view_settings.look = original_look
            scene.display_settings.display_device = original_display_device
    
    def save_atlas_mapping(self, output_path, atlas_name, atlas_type, atlas_size, layout, created_atlases):
        """Сохраняет информацию о раскладке атласа в JSON"""
        try:
            mapping = {
                'atlas_name': atlas_name,
                'atlas_type': atlas_type,
                'atlas_size': atlas_size,
                'created_atlases': created_atlases,
                'layout': [
                    {
                        'set_name': item['texture_set'].name,
                        'material_name': item['texture_set'].material_name,
                        'x': item['x'],
                        'y': item['y'],
                        'width': item['width'],
                        'height': item['height'],
                        'u_min': item['u_min'],
                        'v_min': item['v_min'],
                        'u_max': item['u_max'],
                        'v_max': item['v_max']
                    }
                    for item in layout
                ]
            }
            
            mapping_path = os.path.join(output_path, 'atlas_mapping.json')
            with open(mapping_path, 'w', encoding='utf-8') as f:
                json.dump(mapping, f, ensure_ascii=False, indent=2)
            
            print(f"💾 Сохранен atlas_mapping.json")
            
        except Exception as e:
            print(f"⚠️ Не удалось сохранить atlas_mapping.json: {e}")
    
    def create_atlas_material(self, context, atlas_name, material_name, created_atlases, atlas_type):
        """Создает материал с атласными текстурами"""
        if material_name in bpy.data.materials:
            material = bpy.data.materials[material_name]
        else:
            material = bpy.data.materials.new(name=material_name)
        
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
                    if 'OPACITY' in created_atlases:
                        # Есть альфа-канал
                        links.new(tex_do.outputs['Alpha'], bsdf.inputs['Alpha'])
                        material.blend_method = 'HASHED'
            
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
    
    def apply_atlas_to_object(self, context, obj, atlas_material, layout):
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
        
        # Сохраняем маппинг face_index -> material_name ДО очистки материалов
        face_to_material = {}
        for i, slot in enumerate(obj.material_slots):
            if slot.material:
                mat_name = slot.material.name
                # Проходим по всем полигонам и сохраняем их материал
                for poly in obj.data.polygons:
                    if poly.material_index == i:
                        face_to_material[poly.index] = mat_name
        
        print(f"  📊 Сохранено {len(face_to_material)} полигонов с материалами")
        
        # Теперь применяем UV координаты используя сохраненный маппинг
        if obj.data.uv_layers.active is None:
            obj.data.uv_layers.new(name="UVMap")
        
        uv_layer = obj.data.uv_layers.active.data
        
        processed_faces = 0
        for poly in obj.data.polygons:
            if poly.index in face_to_material:
                mat_name = face_to_material[poly.index]
                
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
        
        print(f"✅ UV раскладка применена, материал назначен")


# ===== APPLY EXISTING ATLAS TO OBJECT OPERATOR =====

def get_available_atlases(self, context):
    """Получает список доступных атласов для EnumProperty"""
    items = []
    texture_sets_list = context.scene.agr_texture_sets
    
    for i, ts in enumerate(texture_sets_list):
        if ts.is_atlas:
            items.append((
                ts.folder_path,
                ts.name,
                f"Atlas: {ts.name} ({ts.resolution}x{ts.resolution})"
            ))
    
    if not items:
        items.append(('NONE', "No atlases", "No atlases available"))
    
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
            return False
        
        # Проверяем наличие атласов
        texture_sets_list = context.scene.agr_texture_sets
        atlases = [ts for ts in texture_sets_list if ts.is_atlas]
        return len(atlases) > 0
    
    def invoke(self, context, event):
        # Показываем диалог выбора атласа
        texture_sets_list = context.scene.agr_texture_sets
        atlases = [ts for ts in texture_sets_list if ts.is_atlas]
        
        if not atlases:
            self.report({'WARNING'}, "Нет доступных атласов")
            return {'CANCELLED'}
        
        # Показываем диалог выбора
        return context.window_manager.invoke_props_dialog(self)
    
    def draw(self, context):
        layout = self.layout
        layout.prop(self, "selected_atlas")
    
    def execute(self, context):
        obj = context.active_object
        texture_sets_list = context.scene.agr_texture_sets
        
        if self.selected_atlas == 'NONE':
            self.report({'ERROR'}, "Не выбран атлас")
            return {'CANCELLED'}
        
        # Находим атлас по folder_path
        atlas_set = None
        for ts in texture_sets_list:
            if ts.is_atlas and ts.folder_path == self.selected_atlas:
                atlas_set = ts
                break
        
        if not atlas_set:
            self.report({'ERROR'}, "Атлас не найден")
            return {'CANCELLED'}
        
        # Загружаем atlas_mapping.json
        mapping_path = os.path.join(atlas_set.folder_path, 'atlas_mapping.json')
        if not os.path.exists(mapping_path):
            self.report({'ERROR'}, f"Не найден atlas_mapping.json для атласа")
            return {'CANCELLED'}
        
        try:
            with open(mapping_path, 'r', encoding='utf-8') as f:
                mapping = json.load(f)
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка чтения atlas_mapping.json: {e}")
            return {'CANCELLED'}
        
        # Проверяем, что все материалы объекта есть в атласе
        obj_materials = [slot.material.name for slot in obj.material_slots if slot.material]
        atlas_materials = [item['material_name'] for item in mapping['layout']]
        
        missing = [m for m in obj_materials if m not in atlas_materials]
        if missing:
            self.report({'ERROR'}, f"Материалы не найдены в атласе: {', '.join(missing)}. Операция отменена.")
            return {'CANCELLED'}
        
        print(f"\n{'='*60}")
        print(f"📐 ПРИМЕНЕНИЕ АТЛАСА К ОБЪЕКТУ")
        print(f"{'='*60}")
        print(f"Объект: {obj.name}")
        print(f"Атлас: {atlas_set.name}")
        
        try:
            # Применяем атлас
            self.apply_atlas_uv(context, obj, atlas_set, mapping)
            
            self.report({'INFO'}, f"Атлас применен к объекту")
            return {'FINISHED'}
            
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка применения атласа: {str(e)}")
            print(f"❌ Ошибка: {e}")
            import traceback
            traceback.print_exc()
            return {'CANCELLED'}
    
    def apply_atlas_uv(self, context, obj, atlas_set, mapping):
        """Применяет UV раскладку атласа к объекту (ИСПРАВЛЕНО: сохраняет маппинг ДО очистки материалов)"""
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
        face_to_material = {}
        for i, slot in enumerate(obj.material_slots):
            if slot.material:
                mat_name = slot.material.name
                # Находим все полигоны с этим материалом
                for poly in obj.data.polygons:
                    if poly.material_index == i:
                        face_to_material[poly.index] = mat_name
        
        print(f"💾 Сохранено {len(face_to_material)} полигонов с материалами")
        
        # Получаем или создаем атласный материал
        atlas_material_name = f"M_{atlas_set.name}"
        if atlas_material_name not in bpy.data.materials:
            # Пытаемся создать материал из текстур атласа
            self.create_atlas_material_from_textures(atlas_set, mapping)
        
        if atlas_material_name not in bpy.data.materials:
            self.report({'WARNING'}, f"Материал атласа '{atlas_material_name}' не найден")
            return
        
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
            face_index = face.index
            
            if face_index in face_to_material:
                mat_name = face_to_material[face_index]
                
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
        
        print(f"✅ UV раскладка применена: обработано {processed_faces} полигонов по JSON маппингу")
    
    def create_atlas_material_from_textures(self, atlas_set, mapping):
        """Создает материал атласа из текстур"""
        material_name = f"M_{atlas_set.name}"
        atlas_type = mapping.get('atlas_type', 'HIGH')
        created_atlases = mapping.get('created_atlases', {})
        
        if material_name in bpy.data.materials:
            material = bpy.data.materials[material_name]
        else:
            material = bpy.data.materials.new(name=material_name)
        
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
        
        material.blend_method = 'HASHED'
        
        print(f"🎨 Материал создан: {material_name}")


# ===== PREVIEW EXISTING ATLAS OPERATOR =====

class AGR_OT_PreviewAtlas(Operator):
    """Preview existing atlas in Image Editor"""
    bl_idname = "agr.preview_atlas"
    bl_label = "Preview Atlas"
    bl_options = {'REGISTER'}
    
    atlas_name: bpy.props.StringProperty(
        name="Atlas Name",
        description="Name of the atlas to preview"
    )
    
    def execute(self, context):
        texture_sets_list = context.scene.agr_texture_sets
        
        # Находим атлас
        atlas_set = None
        for ts in texture_sets_list:
            if ts.is_atlas and ts.name == self.atlas_name:
                atlas_set = ts
                break
        
        if not atlas_set:
            self.report({'ERROR'}, f"Атлас '{self.atlas_name}' не найден")
            return {'CANCELLED'}
        
        # Ищем изображение атласа (DO или d текстуру)
        atlas_folder = atlas_set.folder_path
        atlas_image_path = None
        
        # Пробуем найти основную текстуру
        for filename in os.listdir(atlas_folder):
            if filename.endswith('.png'):
                if '_DO.png' in filename or '_d.png' in filename or '_Diffuse.png' in filename:
                    atlas_image_path = os.path.join(atlas_folder, filename)
                    break
        
        if not atlas_image_path:
            # Берем первую PNG
            for filename in os.listdir(atlas_folder):
                if filename.endswith('.png'):
                    atlas_image_path = os.path.join(atlas_folder, filename)
                    break
        
        if not atlas_image_path or not os.path.exists(atlas_image_path):
            self.report({'ERROR'}, "Не найдено изображение атласа")
            return {'CANCELLED'}
        
        # Загружаем изображение
        image_name = f"Preview_{atlas_set.name}"
        if image_name in bpy.data.images:
            bpy.data.images.remove(bpy.data.images[image_name])
        
        try:
            image = bpy.data.images.load(atlas_image_path)
            image.name = image_name
            
            # Показываем в Image Editor
            self.show_preview_in_editor(context, image)
            
            self.report({'INFO'}, f"Предпросмотр атласа: {atlas_set.name}")
            return {'FINISHED'}
            
        except Exception as e:
            self.report({'ERROR'}, f"Ошибка загрузки изображения: {e}")
            return {'CANCELLED'}
    
    def show_preview_in_editor(self, context, image):
        """Показывает изображение в Image Editor"""
        for area in context.screen.areas:
            if area.type == 'IMAGE_EDITOR':
                for space in area.spaces:
                    if space.type == 'IMAGE_EDITOR':
                        space.image = image
                        space.use_image_pin = False
                        break
                area.tag_redraw()
                print(f"📷 Предпросмотр отображен в Image Editor")
                return
        
        print(f"⚠️ Image Editor не найден, изображение загружено в Data")


# ===== REGISTRATION =====

classes = (
    AGR_OT_PreviewAtlasLayout,
    AGR_OT_CreateAtlasOnly,
    AGR_OT_CreateAtlasFromObject,
    AGR_OT_ApplyAtlasToObject,
    AGR_OT_PreviewAtlas,
)


def register():
    """Register atlas operators"""
    for cls in classes:
        bpy.utils.register_class(cls)
    
    print("✅ Atlas operators registered")


def unregister():
    """Unregister atlas operators"""
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    
    print("Atlas operators unregistered")
