# Исправления багов в системе атласов

## Исправленные проблемы

### 1. ✅ Apply Atlas to Object - выбор атласа
**Проблема:** Непонятно какой атлас назначается, нет возможности выбрать

**Решение:**
- Добавлен `EnumProperty` для выбора атласа из списка
- Показывается диалог с dropdown списком всех доступных атласов
- Отображается имя и разрешение каждого атласа

**Код:**
```python
selected_atlas: EnumProperty(
    name="Atlas",
    description="Select atlas to apply",
    items=lambda self, context: self.get_atlas_items(context)
)

def get_atlas_items(self, context):
    items = []
    for ts in context.scene.agr_texture_sets:
        if ts.is_atlas:
            items.append((
                ts.folder_path,
                ts.name,
                f"Atlas: {ts.name} ({ts.resolution}x{ts.resolution})"
            ))
    return items
```

### 2. ✅ Apply Atlas to Object - подключение материала
**Проблема:** Подключение материала должно быть как в create_atlas_from_object по HIGH методу через 3 карты

**Решение:**
- Изменена логика `create_atlas_material_from_textures()`
- Теперь ВСЕГДА использует 3-карточный метод: DO (или D), ERM, N
- Работает для обоих типов атласов (HIGH и LOW)

**Подключение:**
1. **DiffuseOpacity** (или Diffuse) → Base Color + Alpha
2. **ERM** → Separate Color → E→Emission, R→Roughness, M→Metallic
3. **Normal** → Normal Map → Normal

### 3. ✅ Create Atlas from Object - ошибка create_erm_atlas
**Проблема:** 
```
AttributeError: 'AGR_OT_CreateAtlasFromObject' object has no attribute 'create_erm_atlas'
```

**Решение:**
- Добавлен метод `create_erm_atlas()` в класс `AGR_OT_CreateAtlasFromObject`
- Метод объединяет E, R, M каналы в RGB текстуру
- Использует PIL для загрузки и масштабирования каналов

**Код:**
```python
def create_erm_atlas(self, texture_sets, atlas_size, layout):
    """Создает ERM атлас (объединяет E, R, M в RGB каналы)"""
    # Создает Blender image
    # Загружает E, R, M через PIL
    # Объединяет в RGB каналы
    # Размещает в атласе
    return atlas_image
```

## Технические детали

### Структура подключения материала (3-карточный метод)

```
DiffuseOpacity (RGBA или RGB)
    ├─ Color → Base Color
    └─ Alpha → Alpha

ERM (RGB)
    └─ Color → Separate Color
        ├─ Red → Emission Strength
        ├─ Green → Roughness
        └─ Blue → Metallic

Normal (RGB)
    └─ Color → Normal Map → Normal
```

### Выбор атласа в Apply Atlas to Object

1. Оператор вызывается через `invoke()`
2. Показывается диалог с `EnumProperty`
3. Пользователь выбирает атлас из списка
4. `execute()` применяет выбранный атлас

### Создание ERM атласа

1. Загружает Emit, Roughness, Metallic текстуры через PIL
2. Конвертирует в grayscale (L)
3. Масштабирует до нужного размера (LANCZOS)
4. Объединяет в RGB каналы numpy массива
5. Размещает в атласе по layout координатам

## Проверка работоспособности

### Тест 1: Apply Atlas to Object
1. Создать несколько атласов
2. Выбрать объект
3. Вызвать "Apply Atlas to Object"
4. Должен показаться диалог со списком атласов
5. Выбрать атлас и применить
6. Материал должен подключиться через 3 карты (DO/ERM/N)

### Тест 2: Create Atlas from Object (LOW)
1. Создать объект с именем `SM_TestAddress_Main`
2. Назначить материалы с texture sets
3. Вызвать "Create Atlas from Object"
4. Должен создаться атлас без ошибки `create_erm_atlas`
5. ERM текстура должна быть создана

### Тест 3: Подключение материала
1. Применить любой атлас к объекту
2. Проверить Shader Editor
3. Должны быть подключены:
   - DiffuseOpacity → Base Color + Alpha
   - ERM → Separate Color → E/R/M
   - Normal → Normal Map → Normal

## Файлы изменены
- `operators_atlas.py` - все исправления

## Совместимость
- Blender 4.2+
- Требуется Pillow (PIL)
- Требуется numpy
