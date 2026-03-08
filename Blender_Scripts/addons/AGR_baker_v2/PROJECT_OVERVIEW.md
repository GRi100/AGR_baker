# AGR Baker v2.0 - Обзор проекта

## 📋 Выполненные задачи

✅ **Структура проекта** - Создана модульная архитектура с разделением на компоненты
✅ **Core модуль** - Реализованы базовые функции запекания текстур
✅ **Система сетов** - Управление текстурными сетами S_material_name
✅ **Подключение сетов** - Автоматическое подключение текстур к материалам
✅ **Интеграция Photoshop** - Настройки и подготовка для работы с Photoshop
✅ **UI панели** - Полнофункциональный интерфейс пользователя
✅ **Загрузка сетов** - Автоматическое сканирование папки AGR_BAKE
✅ **Документация** - README и QUICKSTART руководства

## 🏗️ Архитектура проекта

### Модульная структура

```
AGR_baker_v2/
│
├── __init__.py                 # Точка входа, регистрация модулей
│
├── properties.py               # PropertyGroup классы
│   ├── AGR_TextureSet         # Данные текстурного сета
│   └── AGR_BakerSettings      # Настройки аддона
│
├── operators.py                # Пакет операторов
├── operators_bake.py           # Операторы запекания
│   └── AGR_OT_BakeTextures    # Главный оператор запекания
│
├── operators_sets.py           # Операторы работы с сетами
│   ├── AGR_OT_RefreshTextureSets
│   ├── AGR_OT_ConnectSetToMaterial
│   ├── AGR_OT_AssignSetToActiveObject
│   ├── AGR_OT_LoadSetsFromFolder
│   └── AGR_OT_OpenPhotoshopSettings
│
├── ui.py                       # UI панели
│   ├── AGR_UL_TextureSetsList # UIList для сетов
│   ├── AGR_PT_MainPanel       # Главная панель
│   ├── AGR_PT_TextureSetsPanel
│   ├── AGR_PT_PhotoshopPanel
│   └── AGR_PT_SettingsPanel
│
└── core/                       # Основные модули
    ├── __init__.py
    ├── baking.py              # Функции запекания
    │   ├── create_texture_image()
    │   ├── create_flat_normal_image()
    │   ├── setup_bake_node()
    │   ├── bake_texture()
    │   ├── convert_normal_to_directx()
    │   └── save_texture()
    │
    ├── materials.py           # Работа с материалами
    │   ├── connect_normal_map()
    │   ├── load_texture_from_disk()
    │   └── connect_texture_set_to_material()
    │
    └── texture_sets.py        # Управление сетами
        ├── get_agr_bake_folder()
        ├── ensure_texture_set_folder()
        ├── scan_texture_sets()
        ├── refresh_texture_sets_list()
        └── save_texture_set_info()
```

## 🎯 Ключевые особенности

### 1. Система текстурных сетов

**Концепция**: Каждый материал получает свою папку с полным набором текстур

```
AGR_BAKE/
└── S_MaterialName/
    ├── T_MaterialName_Diffuse.png
    ├── T_MaterialName_DiffuseOpacity.png
    ├── T_MaterialName_Emit.png
    ├── T_MaterialName_Roughness.png
    ├── T_MaterialName_Opacity.png
    ├── T_MaterialName_Normal.png
    ├── T_MaterialName_ERM.png
    └── T_MaterialName_Metallic.png
```

### 2. Автоматическое запекание

**Процесс**:
1. Выбор high-poly объектов (источники)
2. Выбор low-poly объекта (цель, активный)
3. Автоматическое создание всех 8 текстур
4. Сохранение в структурированные папки
5. Подключение к материалу

**Технические детали**:
- Metallic и Emit запекаются через Roughness канал
- Opacity извлекается из альфа-канала DiffuseOpacity
- ERM создается комбинацией E+R+M в RGB каналы
- Поддержка OpenGL и DirectX нормалей

### 3. Умное подключение текстур

**HIGH режим** (оптимизированный):
- DiffuseOpacity (RGBA) → Base Color + Alpha
- ERM (RGB) → Emission Strength + Roughness + Metallic
- Normal → Normal Map

**LOW режим** (раздельный):
- Diffuse → Base Color
- Metallic → Metallic
- Roughness → Roughness
- Opacity → Alpha
- Normal → Normal Map

### 4. Интеграция с Photoshop

**Возможности**:
- Настройка пути к Photoshop
- Подготовка для ресайза текстур
- Пакетная обработка
- Применение фильтров

**Реализация**:
- Панель настроек в UI
- Свойства в AGR_BakerSettings
- Оператор для открытия настроек

## 🔄 Workflow

### Базовый workflow

```
1. Подготовка сцены
   ↓
2. Настройка параметров (разрешение, альфа, нормали)
   ↓
3. Запекание (Bake Texture Set)
   ↓
4. Автоматическое создание папки S_MaterialName
   ↓
5. Сохранение 8 текстур
   ↓
6. Подключение к материалу
   ↓
7. Обновление списка сетов
```

### Работа с существующими сетами

```
1. Открыть панель Texture Sets
   ↓
2. Refresh Sets (сканирование AGR_BAKE)
   ↓
3. Выбор действия:
   - Load All (загрузить все сеты)
   - Connect to Material (подключить к материалу)
   - Assign to Active (назначить активному объекту)
```

## 📊 Сравнение с оригиналом

| Аспект | AGR_baker.py (v1) | AGR_baker_v2 |
|--------|-------------------|--------------|
| **Архитектура** | Монолитный файл 14859 строк | Модульная структура |
| **Файлов** | 1 файл | 11 файлов |
| **Папка вывода** | OBJECT_BAKED | AGR_BAKE |
| **Именование сетов** | T_MaterialName | S_MaterialName |
| **Загрузка сетов** | Ручная | Автоматическая |
| **UI** | Базовый | Расширенный с UIList |
| **Photoshop** | Нет | Есть интеграция |
| **Blender** | 4.2 LTS | 5.0 |
| **Поддержка** | Сложная | Легкая (модули) |

## 🛠️ Технические детали

### Используемые технологии

- **Blender API**: bpy, bpy.types, bpy.props
- **NumPy**: Обработка массивов пикселей
- **Python**: 3.11+ (Blender 5.0)

### Оптимизации

- CPU запекание для стабильности
- Samples = 1 для скорости
- Denoising отключен
- Переиспользование функций через модули

### Форматы

- **Текстуры**: PNG, 8-bit
- **Colorspace**: sRGB (цвет), Non-Color (данные)
- **Разрешения**: 512, 1024, 2048, 4096

## 📝 Следующие шаги (опционально)

### Возможные улучшения

1. **Photoshop автоматизация**
   - Скрипты для автоматического ресайза
   - Пакетная обработка через JSX
   - Применение фильтров

2. **UDIM поддержка**
   - Запекание UDIM тайлов
   - Управление UDIM сетами

3. **Атласы текстур**
   - Создание атласов из сетов
   - Упаковка текстур

4. **Экспорт**
   - Экспорт в игровые движки
   - Генерация метаданных

## 📚 Документация

- **README.md** - Полная документация
- **QUICKSTART.md** - Краткое руководство
- **PROJECT_OVERVIEW.md** - Этот файл

## ✅ Результат

Создан полнофункциональный аддон для Blender 5.0 с:
- ✅ Модульной архитектурой
- ✅ Системой текстурных сетов
- ✅ Автоматическим запеканием 8 типов текстур
- ✅ Умным подключением к материалам
- ✅ Интеграцией с Photoshop
- ✅ Интуитивным UI
- ✅ Полной документацией

**Оригинальный файл AGR_baker.py остается без изменений для справки.**

---

**Автор**: computer_invader  
**Версия**: 2.0.0  
**Дата**: 2026-03-08  
**Blender**: 5.0+
