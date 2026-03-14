# Atlas System Changes - Summary

## Выполненные изменения

### 1. Предпросмотр раскладки атласа (Preview Atlas Layout)
- **Оператор**: `AGR_OT_PreviewAtlasLayout` (`agr.preview_atlas_layout`)
- **Функция**: Показывает предпросмотр раскладки выбранных текстурных сетов в атласе
- **Особенности**:
  - Создает цветное превью с границами между текстурами
  - Работает ДО создания атласа
  - Показывает результат в Image Editor
  - Помогает оценить заполнение атласа

### 2. Создание атласа без применения (Create Atlas Only)
- **Оператор**: `AGR_OT_CreateAtlasOnly` (`agr.create_atlas_only`)
- **Функция**: Создает только текстуры атласа без раскладки UV и назначения материала
- **Особенности**:
  - НЕ раскладывает UV
  - НЕ назначает материал на объект
  - НЕ работает логика LOW по спец маскам
  - Создает atlas_mapping.json для последующего использования

### 3. Создание атласа из материалов объекта (Create Atlas from Object)
- **Оператор**: `AGR_OT_CreateAtlasFromObject` (`agr.create_atlas_from_object`)
- **Функция**: Создает атлас из материалов активного объекта, назначает материал и раскладывает UV
- **Особенности**:
  - Сверяется с наличием texture sets для материалов
  - Создает материал атласа
  - Назначает материал на активный объект
  - Раскладывает UV по JSON маппингу
  - **Работает логика LOW naming**: если активный объект `SM_*_Main/Flora/Ground/GroundEl`, создается LOW атлас с именованием `A_Address_ObjectType` и материалом `M_Address_ObjectType_1`

### 4. Применение существующего атласа (Apply Atlas to Object)
- **Оператор**: `AGR_OT_ApplyAtlasToObject` (`agr.apply_atlas_to_object`)
- **Функция**: Применяет существующий атлас к активному объекту
- **Особенности**:
  - Сверяет материалы объекта с JSON маппингом атласа
  - Создает материал атласа (если не существует)
  - Назначает материал на объект
  - Раскладывает UV по JSON маппингу
  - **НЕ работает логика LOW naming** (используется существующий атлас)

## Изменения в создании HIGH атласа

### Было:
- Создавались текстуры: DO (Diffuse+Opacity), ERM (Emit+Roughness+Metallic), N (Normal)

### Стало:
- Создаются **отдельные** текстуры:
  - **D** (Diffuse) - RGB
  - **O** (Opacity) - Grayscale (если есть альфа-канал)
  - **E** (Emit) - Grayscale
  - **R** (Roughness) - Grayscale
  - **M** (Metallic) - Grayscale
  - **N** (Normal) - RGB
- **DO создается** путем объединения D + O:
  - Если есть альфа: DO = RGBA (D как RGB + O как Alpha)
  - Если нет альфа: DO = RGB (просто D без альфа-канала)

## Изменения в создании LOW атласа

### Было:
- Создавались отдельные текстуры: d, r, m, o, n

### Стало:
- **ERM создается объединенной** (E в Red, R в Green, M в Blue)
- **D дублируется как DO**:
  - Если есть альфа: DO = RGBA (D + O)
  - Если нет альфа: DO = RGB (просто D)
- Создаются текстуры:
  - **d** (Diffuse)
  - **DO** (DiffuseOpacity) - дубликат D или D+O
  - **ERM** (Emit+Roughness+Metallic объединенные)
  - **n** (Normal)

## Проверка альфа-канала

- **Функция**: `check_sets_have_alpha(texture_sets)`
- **Логика**: Проверяет наличие `has_diffuse_opacity` или `has_opacity` в исходных сетах
- **Применение**:
  - Если альфа НЕТ: DO создается без альфа-канала (RGB)
  - Если альфа ЕСТЬ: DO создается с альфа-каналом (RGBA)

## Исправление раскладки UV

### Было:
- UV раскладывались в первый тайл для всех материалов

### Стало:
- UV раскладываются **по JSON маппингу** из `atlas_mapping.json`
- Каждый материал получает свою область в атласе согласно layout
- Используется маппинг `material_name -> {u_min, v_min, u_max, v_max}`

## Подключение материалов

### HIGH атлас:
```
DO -> Base Color + Alpha
E -> Emission Strength
R -> Roughness
M -> Metallic
N -> Normal (через Normal Map node)
```

### LOW атлас:
```
DO -> Base Color + Alpha
ERM (через Separate Color):
  - Red -> Emission Strength
  - Green -> Roughness
  - Blue -> Metallic
N -> Normal (через Normal Map node)
```

## UI изменения

### Добавлены кнопки:
1. **Preview Atlas Layout** - предпросмотр раскладки
2. **Create Atlas Only** - создание только текстур атласа
3. **Create Atlas from Object** - создание атласа из материалов объекта (с применением)
4. **Apply Atlas to Object** - применение существующего атласа

### Удалена кнопка:
- **Create Atlas from Selected** - заменена на "Create Atlas Only"

## Технические детали

### Общая функция для ERM:
- `create_erm_atlas_combined()` - создает ERM атлас из отдельных E, R, M текстур
- Используется как в `AGR_OT_CreateAtlasOnly`, так и в `AGR_OT_CreateAtlasFromObject`

### Именование файлов:
- **HIGH**: `T_AtlasName_D.png`, `T_AtlasName_O.png`, `T_AtlasName_E.png`, etc.
- **LOW с LOW naming**: `T_Address_ObjectType_d.png`, `T_Address_ObjectType_o.png`, etc.
- **LOW без LOW naming**: `T_AtlasName_d.png`, `T_AtlasName_o.png`, etc.

## Совместимость

- Все изменения обратно совместимы
- Старые атласы продолжат работать
- Новые атласы используют улучшенную структуру
