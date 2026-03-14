# Исправления системы атласов AGR Baker v2

## Выполненные исправления

### 1. Проверка альфа-канала
- Функция `check_sets_have_alpha()` теперь реально проверяет файлы через PIL
- Проверяет DiffuseOpacity и Diffuse файлы на наличие RGBA/LA режимов
- Fallback на флаги texture set если PIL недоступна

### 2. Нейминг файлов

**HIGH атлас:**
- `T_AtlasName_Diffuse.png` - диффузная карта
- `T_AtlasName_DiffuseOpacity.png` - диффузная с альфой (RGBA если есть альфа, RGB если нет)
- `T_AtlasName_Opacity.png` - отдельная альфа-карта (если есть)
- `T_AtlasName_Emit.png` - эмиссия
- `T_AtlasName_Roughness.png` - шероховатость
- `T_AtlasName_Metallic.png` - металличность
- `T_AtlasName_Normal.png` - нормали

**LOW атлас:**
- `T_AtlasName_d.png` - диффузная карта
- `T_AtlasName_do.png` - диффузная с альфой (RGBA если есть альфа, RGB если нет)
- `T_AtlasName_o.png` - отдельная альфа-карта (если есть)
- `T_AtlasName_erm.png` - объединенная ERM карта
- `T_AtlasName_r.png` - отдельная шероховатость
- `T_AtlasName_m.png` - отдельная металличность
- `T_AtlasName_n.png` - нормали

### 3. Подключение текстур к материалу

**HIGH атлас:**
- DiffuseOpacity → Base Color + Alpha (если есть альфа)
- ERM создается на лету из E, R, M → Separate Color → Emission/Roughness/Metallic
- Normal → Normal Map → Normal

**LOW атлас:**
- do → Base Color + Alpha (если есть альфа)
- r → Roughness
- m → Metallic
- n → Normal Map → Normal
- Без Emission для LOW

### 4. UV раскладка
- Исправлена функция `apply_atlas_to_object()`
- Теперь сохраняет маппинг face_index → material_name ДО очистки материалов
- Применяет UV координаты в режиме OBJECT
- Только после UV раскладки заменяет материалы

### 5. Три функции создания атласа

**AGR_OT_CreateAtlasOnly:**
- Создает только текстуры атласа из выбранных сетов
- Не применяет материал и не раскладывает UV
- Не работает логика LOW по спец маскам

**AGR_OT_CreateAtlasFromObject:**
- Создает атлас из материалов объекта
- Сверяется с наличием сетов для материалов
- Создает материал и назначает на активный объект
- Раскладывает UV
- Определяет LOW/HIGH по имени объекта (SM_Address_ObjectType)

**AGR_OT_ApplyAtlasToObject:**
- Применяет существующий атлас к объекту
- Сверяет материалы с JSON выбранного атласа
- Создает материал и назначает его
- Раскладывает UV для активного объекта
- Логика LOW не работает (только HIGH)

### 6. Предпросмотр раскладки
- Новый оператор `AGR_OT_PreviewAtlasLayout`
- Показывает предпросмотр раскладки для выбранных сетов
- Отображается в Image Editor
- Работает ДО создания атласа

## Технические детали

### Обработка альфа-канала
- Если в исходных сетах НЕТ альфа-канала, DO создается без альфа (RGB)
- Если есть альфа, DO создается с альфа (RGBA)
- Отдельная Opacity карта создается всегда если есть альфа

### HIGH vs LOW
- HIGH: разделяет DO/ERM/N на отдельные карты (Diffuse, DiffuseOpacity, Emit, Roughness, Metallic, Normal)
- LOW: объединяет в ERM, дублирует D как DO, создает отдельные r/m/n

### UV маппинг
- Использует JSON маппинг из atlas_mapping.json
- Сохраняет оригинальные UV координаты полигонов
- Масштабирует их в регион атласа по формуле: new_uv = uv_min + orig_uv * (uv_max - uv_min)
