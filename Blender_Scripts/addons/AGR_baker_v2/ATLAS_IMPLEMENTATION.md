# Atlas Implementation - AGR Baker v2

## Обзор

Полностью реализована система создания и работы с атласами текстур в AGR Baker v2.

## Основные возможности

### 1. Создание атласов

#### Автоматическое определение типа
- **HIGH атлас** (DO/ERM/N) - для обычных объектов
- **LOW атлас** (d/r/m/o/n) - для объектов SM_Address_Main/Ground/Flora/GroundEl

#### Три способа создания:

**A. Из выбранных texture sets**
- Выберите несколько texture sets в списке
- Нажмите "Create Atlas from Selected"
- Тип атласа определяется автоматически по активному объекту

**B. Из материалов активного объекта**
- Выберите объект с материалами
- Нажмите "Create Atlas from Object"
- Автоматически находит все texture sets для материалов объекта
- Создает атлас и применяет его к объекту с раскладкой UV

**C. Применение существующего атласа**
- Выберите объект
- Нажмите "Apply Atlas to Object"
- Выберите атлас из списка
- UV координаты автоматически раскладываются по атласу

### 2. Именование

#### HIGH атлас (обычные объекты)
```
Папка: A_ObjectName/
Материал: M_A_ObjectName
Текстуры:
  - T_A_ObjectName_DO.png (Diffuse + Opacity)
  - T_A_ObjectName_ERM.png (Emission + Roughness + Metallic)
  - T_A_ObjectName_N.png (Normal)
```

#### LOW атлас (SM_Address_Type объекты)
```
Папка: A_Address_Type/
Материал: M_Address_Type_1
Текстуры:
  - T_Address_Type_d.png (Diffuse)
  - T_Address_Type_r.png (Roughness)
  - T_Address_Type_m.png (Metallic)
  - T_Address_Type_o.png (Opacity)
  - T_Address_Type_n.png (Normal)
```

Поддерживаемые типы: Main, Ground, Flora, GroundEl

### 3. Atlas Mapping JSON

Каждый атлас сохраняет `atlas_mapping.json` с информацией о раскладке:

```json
{
  "atlas_name": "A_ObjectName",
  "atlas_type": "HIGH",
  "atlas_size": 2048,
  "created_atlases": {
    "DIFFUSE_OPACITY": "path/to/T_A_ObjectName_DO.png",
    "ERM": "path/to/T_A_ObjectName_ERM.png",
    "NORMAL": "path/to/T_A_ObjectName_N.png"
  },
  "layout": [
    {
      "set_name": "MaterialName_baked",
      "material_name": "MaterialName",
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

### 4. Предпросмотр атласа

- Кнопки предпросмотра для каждого атласа в UI
- Открывает изображение в Image Editor
- Показывает основную текстуру (DO или d)

### 5. Интеграция в список сетов

- Атласы отображаются в списке texture sets с префиксом `A_`
- Помечены специальной иконкой `IMAGE_PLANE`
- Можно удалять как обычные сеты
- Автоматически обновляются при создании

## Технические детали

### Упаковка текстур

Используется алгоритм **Guillotine** для оптимальной упаковки:
- Сортировка по размеру (большие текстуры первыми)
- Минимизация отходов пространства
- Проверка на переполнение атласа

### Масштабирование текстур

**Приоритет 1: Pillow (PIL)**
- Качественное масштабирование через LANCZOS
- Быстрая обработка

**Fallback: NumPy**
- Простое nearest-neighbor масштабирование
- Работает без дополнительных библиотек

### Подключение к материалу

#### HIGH атлас:
```
DIFFUSE_OPACITY -> Base Color + Alpha
ERM (Separate Color):
  - Red -> Emission Strength
  - Green -> Roughness
  - Blue -> Metallic
NORMAL -> Normal Map -> Normal
```

#### LOW атлас:
```
DIFFUSE -> Base Color
ROUGHNESS -> Roughness
METALLIC -> Metallic
OPACITY -> Alpha
NORMAL -> Normal Map -> Normal
```

### UV раскладка

При применении атласа к объекту:
1. Сохраняются индексы материалов полигонов
2. Все материалы заменяются на атласный
3. UV координаты масштабируются и смещаются в соответствующие области атласа
4. Каждый полигон получает UV координаты своего материала из atlas_mapping.json

## UI элементы

### Atlas Operations панель

```
Atlas Operations:
├── Atlas Size: [dropdown]
├── Selected: X sets
├── Create Atlas from Selected [AUTO]
├── Active: Type (Address)
├── ───────────────────────
├── Create Atlas from Object
├── ───────────────────────
├── Available Atlases: X
├── Apply Atlas to Object
└── Preview:
    ├── [Atlas1]
    ├── [Atlas2]
    └── ...
```

## Примеры использования

### Пример 1: Создание HIGH атласа
```
1. Запеките несколько материалов
2. Выберите их texture sets в списке
3. Установите размер атласа (например, 2048)
4. Нажмите "Create Atlas from Selected"
5. Атлас создан с именем A_ObjectName
```

### Пример 2: Создание LOW атласа для SM_Address_Main
```
1. Выберите объект SM_PereulokTrekhprudny_001_Main
2. Убедитесь, что все материалы запечены
3. Нажмите "Create Atlas from Object"
4. Атлас создан: A_PereulokTrekhprudny_Main
5. Текстуры: T_PereulokTrekhprudny_Main_d/r/m/o/n.png
6. UV автоматически разложены
```

### Пример 3: Применение существующего атласа
```
1. Выберите объект с несколькими материалами
2. Нажмите "Apply Atlas to Object"
3. Выберите атлас из списка
4. UV раскладываются автоматически
5. Все материалы заменены на атласный
```

## Файловая структура

```
OBJECT_BAKED/
├── MaterialName1_baked/
│   ├── T_MaterialName1_Diffuse.png
│   ├── T_MaterialName1_ERM.png
│   └── T_MaterialName1_Normal.png
├── MaterialName2_baked/
│   └── ...
└── A_ObjectName/
    ├── T_A_ObjectName_DO.png
    ├── T_A_ObjectName_ERM.png
    ├── T_A_ObjectName_N.png
    └── atlas_mapping.json
```

## Ограничения

1. Максимальный размер атласа: 4096x4096
2. Все текстуры должны быть квадратными
3. Общая площадь текстур не должна превышать площадь атласа
4. Для LOW атласов объект должен иметь имя формата SM_Address_Type

## Улучшения по сравнению со старой версией

1. ✅ Автоматическое определение типа атласа
2. ✅ Единая кнопка создания вместо двух (HIGH/LOW)
3. ✅ Создание атласа из материалов объекта
4. ✅ Автоматическая раскладка UV при создании
5. ✅ Применение существующего атласа к объекту
6. ✅ Предпросмотр атласов в Image Editor
7. ✅ Качественное масштабирование через Pillow
8. ✅ Только OpenGL normal maps (упрощение)
9. ✅ Сохранение atlas_mapping.json для всех атласов
10. ✅ Интеграция в список texture sets

## Совместимость

- Blender 4.2+
- Python 3.11+
- Опционально: Pillow (для качественного масштабирования)
- NumPy (встроен в Blender)
