# AGR Baker v2.0 - Краткое руководство

## Быстрая установка

1. Скопируйте папку `AGR_baker_v2` в папку аддонов Blender 5.0
2. Активируйте аддон в Preferences → Add-ons
3. Панель появится в 3D Viewport (N) → AGR Baker

## Основной workflow

### 1. Запекание текстур

```
Выбрать high-poly объекты → Выбрать low-poly (активный) → Bake Texture Set
```

**Результат**: Папка `AGR_BAKE/S_MaterialName/` с 8 текстурами

### 2. Загрузка сетов

```
Texture Sets → Refresh Sets → Load All
```

**Результат**: Все сеты подключены к соответствующим материалам

### 3. Ручное подключение

```
Texture Sets → Выбрать сет → Connect to Material / Assign to Active
```

## Структура текстур

Каждый материал получает:

- ✅ **T_Name_Diffuse.png** - Цвет RGB
- ✅ **T_Name_DiffuseOpacity.png** - Цвет + Альфа RGBA
- ✅ **T_Name_Emit.png** - Emission Strength
- ✅ **T_Name_Roughness.png** - Шероховатость
- ✅ **T_Name_Opacity.png** - Прозрачность (извлечена из альфы)
- ✅ **T_Name_Normal.png** - Карта нормалей
- ✅ **T_Name_ERM.png** - Комбинированная (E+R+M в RGB)
- ✅ **T_Name_Metallic.png** - Металличность

## Настройки

### Запекание
- **Resolution**: 512-4096px
- **Bake with Alpha**: Включить для прозрачности
- **Normal Type**: OpenGL (стандарт) или DirectX
- **Max Ray Distance**: 0.0 (авто) или больше
- **Extrusion**: 0.5 (стандарт)

### Photoshop
- Включить в панели "Photoshop Integration"
- Указать путь к Photoshop.exe
- Использовать для ресайза и обработки

## Отличия от v1

| Функция | v1 (AGR_baker.py) | v2 (AGR_baker_v2) |
|---------|-------------------|-------------------|
| Структура | Монолитный файл | Модульная |
| Папка | OBJECT_BAKED | AGR_BAKE |
| Сеты | T_MaterialName | S_MaterialName |
| Загрузка | Ручная | Автоматическая |
| Photoshop | Нет | Есть |
| Blender | 4.2 | 5.0 |

## Решение проблем

**Аддон не виден**: Проверьте имя папки `AGR_baker_v2`

**Ошибка запекания**: Сохраните blend файл

**Нет текстур**: Нажмите "Refresh Sets"

## Файлы проекта

```
AGR_baker_v2/
├── __init__.py           # Регистрация аддона
├── properties.py         # Свойства и данные
├── operators.py          # Пакет операторов
├── operators_bake.py     # Запекание
├── operators_sets.py     # Работа с сетами
├── ui.py                 # Интерфейс
├── core/
│   ├── baking.py        # Функции запекания
│   ├── materials.py     # Работа с материалами
│   └── texture_sets.py  # Управление сетами
└── README.md            # Полная документация
```

## Поддержка

Автор: computer_invader
Версия: 2.0.0
Blender: 5.0+

---

**Важно**: Оригинальный AGR_baker.py остается без изменений для справки.
