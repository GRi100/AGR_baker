# Итоговые изменения в системе атласов AGR Baker v2

## Выполненные задачи

### 1. Предпросмотр раскладки атласа
- ✅ Создан оператор `AGR_OT_PreviewAtlasLayout` для предпросмотра раскладки выбранных сетов
- ✅ Отдельная кнопка в UI для просмотра заполнения атласа ДО создания
- ✅ Визуализация через GPU с цветовой кодировкой текстур

### 2. Три функции создания атласов

#### 2.1 Create Atlas Only (`AGR_OT_CreateAtlasOnly`)
**Назначение:** Создание атласа из выбранных сетов без раскладки UV и без назначения материала

**Особенности:**
- Не раскладывает UV
- Не накидывает материал на активный объект
- Логика для создания LOW по спец маскам НЕ работает
- Процедурное именование: `A_001`, `A_002`, `A_003` и т.д.

**HIGH атлас:**
- Создает: `Diffuse`, `DiffuseOpacity`, `Emit`, `Roughness`, `Metallic`, `Normal`, `ERM`
- ERM создается объединением E+R+M каналов
- Если нет альфа-канала: DO создается как RGB (без альфа)

**LOW атлас:**
- Создает: `d`, `do`, `erm`, `r`, `m`, `n`, `o` (если есть альфа)
- ERM объединяет E+R+M
- DO дублируется из D (если нет альфа)

#### 2.2 Create Atlas from Object (`AGR_OT_CreateAtlasFromObject`)
**Назначение:** Создание атласа из материалов объекта с назначением материала и раскладкой UV

**Особенности:**
- Сверяется с наличием сетов для материалов
- Создает материал и назначает на активный объект
- Раскладывает UV по атласу
- Именование через `A_objectname` или по спец маске если LOW активный объект

**Логика LOW активного объекта:**
- Если имя объекта: `SM_ADDRESS_Main/Flora/Ground/GroundEl`
- Атлас: `A_ADDRESS_ObjectType`
- Материал: `M_ADDRESS_ObjectType_1`
- Текстуры: `T_ADDRESS_ObjectType_d/r/m/o/n.png`

**HIGH атлас:**
- Создает: `Diffuse`, `DiffuseOpacity`, `Emit`, `Roughness`, `Metallic`, `Normal`, `ERM`
- ERM создается объединением E+R+M
- Если нет альфа: DO = RGB копия Diffuse

**LOW атлас:**
- Создает: `d`, `do`, `erm`, `r`, `m`, `n`, `o`
- DO создается из DiffuseOpacity или Diffuse+Opacity
- ERM объединяет E+R+M

#### 2.3 Apply Atlas to Object (`AGR_OT_ApplyAtlasToObject`)
**Назначение:** Применение существующего атласа к активному объекту

**Особенности:**
- Сверяет материалы с JSON выбранного атласа
- Создает материал и назначает его
- Раскладывает UV для активного объекта
- Логика LOW активного объекта НЕ работает
- **ИСПРАВЛЕНО:** Сохраняет маппинг полигонов на материалы ДО очистки материалов

### 3. Разделение текстур HIGH атласа
- ✅ HIGH атлас разделяет DO, ERM, N на отдельные карты:
  - `Diffuse` (RGB)
  - `DiffuseOpacity` (RGBA или RGB)
  - `Emit` (L)
  - `Roughness` (L)
  - `Metallic` (L)
  - `ERM` (RGB объединение E+R+M)
  - `Normal` (RGB)

### 4. Объединение текстур LOW атласа
- ✅ LOW атлас складывает ERM:
  - Создает `erm` из объединения `e`, `r`, `m`
- ✅ Дублирует D как DO:
  - Если нет альфа: `do` = копия `d` (RGB)
  - Если есть альфа: `do` = `d` + `o` (RGBA)

### 5. Проверка альфа-канала
- ✅ Реальная проверка файлов через PIL
- ✅ Функция `check_sets_have_alpha()` проверяет DiffuseOpacity текстуры
- ✅ Если хотя бы один сет имеет альфа → атлас создается с альфа
- ✅ DO без альфа создается как RGB (не RGBA)

### 6. Нейминг файлов

#### HIGH атлас (стандартный или A_ObjectName):
```
T_AtlasName_Diffuse.png
T_AtlasName_DiffuseOpacity.png
T_AtlasName_Emit.png
T_AtlasName_Roughness.png
T_AtlasName_Metallic.png
T_AtlasName_ERM.png
T_AtlasName_Normal.png
T_AtlasName_Opacity.png (если есть альфа)
```

#### LOW атлас (A_ADDRESS_ObjectType):
```
T_ADDRESS_ObjectType_d.png
T_ADDRESS_ObjectType_do.png
T_ADDRESS_ObjectType_erm.png
T_ADDRESS_ObjectType_r.png
T_ADDRESS_ObjectType_m.png
T_ADDRESS_ObjectType_n.png
T_ADDRESS_ObjectType_o.png (если есть альфа)
```

### 7. Подключение текстур к материалу

#### HIGH материал:
- Base Color ← DiffuseOpacity (Color)
- Alpha ← DiffuseOpacity (Alpha)
- Emission Strength ← Emit (Color)
- Roughness ← Roughness (Color)
- Metallic ← Metallic (Color)
- Normal ← Normal (через Normal Map node)

#### LOW материал:
- Base Color ← do (Color)
- Alpha ← do (Alpha)
- Emission Strength ← erm (Red)
- Roughness ← erm (Green) или r
- Metallic ← erm (Blue) или m
- Normal ← n (через Normal Map node)

### 8. UV раскладка
- ✅ Сохранение маппинга полигонов на материалы ДО очистки материалов
- ✅ Использование `face_to_material` словаря для корректной раскладки
- ✅ Раскладка по JSON маппингу из `atlas_mapping.json`
- ✅ Трансформация UV координат в область атласа

## Технические детали

### Структура atlas_mapping.json
```json
{
  "atlas_name": "A_001",
  "atlas_type": "HIGH",
  "atlas_size": 2048,
  "created_atlases": {
    "DIFFUSE": "path/to/T_A_001_Diffuse.png",
    "DIFFUSE_OPACITY": "path/to/T_A_001_DiffuseOpacity.png",
    "EMIT": "path/to/T_A_001_Emit.png",
    "ROUGHNESS": "path/to/T_A_001_Roughness.png",
    "METALLIC": "path/to/T_A_001_Metallic.png",
    "ERM": "path/to/T_A_001_ERM.png",
    "NORMAL": "path/to/T_A_001_Normal.png"
  },
  "layout": [
    {
      "set_name": "S_Material1",
      "material_name": "Material1",
      "x": 0,
      "y": 0,
      "width": 1024,
      "height": 1024,
      "u_min": 0.0,
      "v_min": 0.0,
      "u_max": 0.5,
      "v_max": 0.5
    }
  ]
}
```

### Процедурное именование атласов
- Сканирует существующие папки `A_###`
- Находит максимальный номер
- Создает следующий: `A_001`, `A_002`, `A_003`...

### Алгоритм упаковки
- Guillotine rectangle packing
- Сортировка по убыванию размера
- Минимизация отходов пространства

## Исправленные баги

1. ✅ Альфа-канал проверяется реально (через PIL), а не по наличию файла
2. ✅ DO без альфа создается как RGB (не RGBA с белым альфа)
3. ✅ Нейминг HIGH атласа использует правильный регистр (Diffuse, не DIFFUSE)
4. ✅ ERM создается для HIGH атласа (объединение E+R+M)
5. ✅ UV раскладка сохраняет маппинг ДО очистки материалов
6. ✅ Процедурное именование для Create Atlas Only

## Файлы изменены
- `operators_atlas.py` - основные изменения
- `core/materials.py` - подключение текстур
- `properties.py` - свойства атласов
- `ui.py` - UI кнопки

## Совместимость
- Blender 4.2+
- Требуется Pillow (PIL)
- Требуется numpy
