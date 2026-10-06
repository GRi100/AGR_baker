# Headless regression suite for AGR JSON (operators_json.py).
# Run: blender --background --factory-startup --python scripts/test_json.py
import io
import json
import os
import shutil
import sys
import tempfile

import bpy

# repo root = parent of scripts/ — works from any checkout location
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import AGR_tools.log as agr_log
import AGR_tools.operators_json as gj

agr_log.register()
gj.register()

FAILS = []
TMP_ROOTS = []


def check(name, cond, extra=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def tmp_root(prefix):
    root = tempfile.mkdtemp(prefix=prefix)
    TMP_ROOTS.append(root)
    bpy.ops.wm.read_homefile(use_empty=True)
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(root, "proj.blend"))
    return root


def read(path):
    with open(path, 'r', encoding='utf-8-sig') as f:
        return json.load(f)


def image_of(path):
    return (read(path).get('features', [{}])[0].get('properties', {}) or {}).get('imageBase64')


def write_raw(path, text, encoding='utf-8'):
    with io.open(path, 'w', encoding=encoding, newline='') as f:
        f.write(text)


def make_png(path):
    img = bpy.data.images.new("__pic__", 16, 16)
    img.pixels = [0.2, 0.4, 0.6, 1.0] * 256
    img.filepath_raw = path
    img.file_format = 'PNG'
    img.save()
    bpy.data.images.remove(img)


class Capture:
    def __init__(self, module):
        self.module = module
        self.messages = []

    def __enter__(self):
        self._orig = self.module.agr_report

        def hook(op, level, msg, _o=self._orig, _self=self):
            _self.messages.append((level, msg))
            _o(op, level, msg)
        self.module.agr_report = hook
        return self

    def __exit__(self, *exc):
        self.module.agr_report = self._orig
        return False

    def has(self, level, needle):
        return any(lv == level and needle in msg for lv, msg in self.messages)


MINIMAL = '{"features": [{"properties": {"address": "A", "imageBase64": ""}, ' \
          '"geometry": {"coordinates": [0, 0]}, "Glasses": []}]}'


# ═══════════════ 1. JSON-1: malformed files do not abort the batch ═══════════════
print("== 1. JSON-1: features: [] does not crash 'Загрузить' ==")
root = tmp_root("agr_js1_")
os.makedirs(os.path.join(root, "SM_A"))
os.makedirs(os.path.join(root, "SM_B"))
write_raw(os.path.join(root, "SM_A", "SM_A.geojson"), '{"features": []}')
write_raw(os.path.join(root, "SM_B", "SM_B.geojson"), MINIMAL)
with Capture(gj) as cap:
    check("load FINISHED", bpy.ops.agr.load_all_geojson() == {'FINISHED'})
check("report counts only the parsed file", cap.has('INFO', 'загружено 1 JSON'),
      str([m for _l, m in cap.messages]))
check("broken file listed in a WARNING", cap.has('WARNING', 'SM_A'),
      str([m for _l, m in cap.messages]))

print("== 1b. JSON-1: 'Сохранить' skips the broken file and finishes the batch ==")
props = bpy.context.scene.agr_geojson_props
props.address = "CHANGED"
with Capture(gj) as cap:
    check("save FINISHED", bpy.ops.agr.save_all_geojson() == {'FINISHED'})
check("healthy file written",
      read(os.path.join(root, "SM_B", "SM_B.geojson"))['features'][0]['properties']['address'] == "CHANGED")
check("broken file left as it was",
      read(os.path.join(root, "SM_A", "SM_A.geojson")) == {"features": []})
check("skipped file reported", cap.has('WARNING', 'SM_A'), str([m for _l, m in cap.messages]))

print("== 1c. JSON-1: no 'features' key at all ==")
root = tmp_root("agr_js1c_")
os.makedirs(os.path.join(root, "SM_A"))
os.makedirs(os.path.join(root, "SM_B"))
os.makedirs(os.path.join(root, "SM_C"))
write_raw(os.path.join(root, "SM_A", "SM_A.geojson"), MINIMAL)
write_raw(os.path.join(root, "SM_B", "SM_B.geojson"), '{"type": "FeatureCollection"}')
write_raw(os.path.join(root, "SM_C", "SM_C.geojson"), MINIMAL)
check("load FINISHED", bpy.ops.agr.load_all_geojson() == {'FINISHED'})
bpy.context.scene.agr_geojson_props.address = "Z"
check("save FINISHED", bpy.ops.agr.save_all_geojson() == {'FINISHED'})
check("file AFTER the broken one is still saved",
      read(os.path.join(root, "SM_C", "SM_C.geojson"))['features'][0]['properties']['address'] == "Z")


# ═══════════════ 2. JSON-2: the image is never wiped by an empty value ═══════════════
print("== 2. JSON-2: add_image → new folder → load → save keeps the picture ==")
root = tmp_root("agr_js2_")
os.makedirs(os.path.join(root, "SM_Addr_002"))
os.makedirs(os.path.join(root, "SM_Addr_Ground"))
bpy.ops.agr.load_all_geojson()
bpy.ops.agr.create_all_geojson()
pic = os.path.join(root, "pic.png")
make_png(pic)
check("add_image FINISHED", bpy.ops.agr.add_image_to_geojson(filepath=pic) == {'FINISHED'})
p002 = os.path.join(root, "SM_Addr_002", "SM_Addr_002.geojson")
pgnd = os.path.join(root, "SM_Addr_Ground", "SM_Addr_Ground.geojson")
check("image written to 002", bool(image_of(p002)))
check("image written to Ground", bool(image_of(pgnd)))

# a newer building folder appears and gets an image-less geojson from the template
os.makedirs(os.path.join(root, "SM_Addr_001"))
bpy.ops.agr.load_all_geojson()
folders = bpy.context.scene.agr_geojson_folders
idx = next(i for i, f in enumerate(folders) if f.name == "SM_Addr_001")
bpy.context.scene.agr_geojson_folders_index = idx
bpy.ops.agr.create_geojson()
bpy.ops.agr.load_all_geojson()
check("load picked the image up from a file that HAS one",
      bpy.context.scene.agr_geojson_props.has_image)
bpy.ops.agr.save_all_geojson()
p001 = os.path.join(root, "SM_Addr_001", "SM_Addr_001.geojson")
check("002 keeps its image", bool(image_of(p002)))
check("Ground keeps its image", bool(image_of(pgnd)))
check("the new building got the image too", bool(image_of(p001)))

print("== 2b. JSON-2: image only in the Ground file ==")
root = tmp_root("agr_js2b_")
os.makedirs(os.path.join(root, "SM_Addr"))
os.makedirs(os.path.join(root, "SM_Addr_Ground"))
bpy.ops.agr.load_all_geojson()
bpy.ops.agr.create_all_geojson()
pgnd = os.path.join(root, "SM_Addr_Ground", "SM_Addr_Ground.geojson")
data = read(pgnd)
data['features'][0]['properties']['imageBase64'] = "VEVTVF9JTUFHRQ=="
write_raw(pgnd, json.dumps(data, ensure_ascii=False))
bpy.ops.agr.load_all_geojson()
check("shared prop picked the Ground image up",
      bpy.context.scene.agr_geojson_props.imageBase64 == "VEVTVF9JTUFHRQ==",
      bpy.context.scene.agr_geojson_props.imageBase64[:20])
bpy.ops.agr.save_all_geojson()
check("Ground image survives the save", image_of(pgnd) == "VEVTVF9JTUFHRQ==", repr(image_of(pgnd)))

print("== 2c. JSON-2: an empty shared value never clears an existing picture ==")
bpy.context.scene.agr_geojson_props.imageBase64 = ""
bpy.context.scene.agr_geojson_props.has_image = False
bpy.ops.agr.save_all_geojson()
check("picture still there after an image-less save", image_of(pgnd) == "VEVTVF9JTUFHRQ==",
      repr(image_of(pgnd)))

print("== 2d. deliberate removal has its own operator ==")
check("operator registered", hasattr(bpy.ops.agr, "remove_image_from_geojson"))
check("remove FINISHED", bpy.ops.agr.remove_image_from_geojson() == {'FINISHED'})
check("image cleared everywhere", image_of(pgnd) == "",
      repr(image_of(pgnd)))
check("shared prop cleared", not bpy.context.scene.agr_geojson_props.has_image)


# ═══════════════ 3. JSON-4: BOM ═══════════════
print("== 3. JSON-4: a geojson with a UTF-8 BOM is read and saved ==")
root = tmp_root("agr_js4_")
os.makedirs(os.path.join(root, "SM_Bom"))
bom_path = os.path.join(root, "SM_Bom", "SM_Bom.geojson")
write_raw(bom_path, MINIMAL, encoding='utf-8-sig')
with Capture(gj) as cap:
    bpy.ops.agr.load_all_geojson()
check("report says 1 parsed", cap.has('INFO', 'загружено 1 JSON'),
      str([m for _l, m in cap.messages]))
check("no 'unreadable' warning", not cap.has('WARNING', 'SM_Bom'),
      str([m for _l, m in cap.messages]))
bpy.context.scene.agr_geojson_props.address = "BomAddr"
bpy.ops.agr.save_all_geojson()
check("value really written", read(bom_path)['features'][0]['properties']['address'] == "BomAddr")

print("== 3b. JSON-4: a truly unparseable file is counted as skipped ==")
root = tmp_root("agr_js4b_")
os.makedirs(os.path.join(root, "SM_Bad"))
os.makedirs(os.path.join(root, "SM_Good"))
write_raw(os.path.join(root, "SM_Bad", "SM_Bad.geojson"), "{ not json at all")
write_raw(os.path.join(root, "SM_Good", "SM_Good.geojson"), MINIMAL)
with Capture(gj) as cap:
    bpy.ops.agr.load_all_geojson()
check("only the good one counted", cap.has('INFO', 'загружено 1 JSON'),
      str([m for _l, m in cap.messages]))
check("bad one named in the WARNING", cap.has('WARNING', 'SM_Bad'),
      str([m for _l, m in cap.messages]))
with Capture(gj) as cap:
    bpy.ops.agr.save_all_geojson()
check("save reports 1 written", cap.has('INFO', 'Сохранено 1 файлов'),
      str([m for _l, m in cap.messages]))
check("save lists the skipped file", cap.has('WARNING', 'SM_Bad'),
      str([m for _l, m in cap.messages]))


# ═══════════════ 4. JSON-5: atomic write ═══════════════
print("== 4. JSON-5: _save_geojson writes through a temp file ==")
root = tmp_root("agr_js5_")
target = os.path.join(root, "atomic.geojson")
write_raw(target, MINIMAL)
before = read(target)
gj._save_geojson(target, {"features": [{"properties": {"x": 1}}]})
check("content replaced", read(target)['features'][0]['properties']['x'] == 1)
check("no temp leftovers", not [f for f in os.listdir(root) if f.startswith('.agr_geojson_')],
      str(os.listdir(root)))


class _Unserialisable:
    pass


try:
    gj._save_geojson(target, {"features": [{"properties": {"x": _Unserialisable()}}]})
    raised = False
except Exception:
    raised = True
check("a failing dump raises", raised)
check("the original file is intact after a failed write",
      read(target)['features'][0]['properties']['x'] == 1)
check("failed write leaves no temp file",
      not [f for f in os.listdir(root) if f.startswith('.agr_geojson_')],
      str(os.listdir(root)))


# ═══════════════ 5. guard helpers ═══════════════
print("== 5. _feature / _feature_props guards ==")
check("None for a non-dict", gj._feature([1, 2]) is None)
check("None for empty features", gj._feature({"features": []}) is None)
check("None when features is not a list", gj._feature({"features": {}}) is None)
check("None when features[0] is not a dict", gj._feature({"features": ["x"]}) is None)
check("dict for a healthy file", isinstance(gj._feature(json.loads(MINIMAL)), dict))
check("_feature_props None without properties",
      gj._feature_props({"features": [{}]}) is None)
check("_feature_props dict for a healthy file",
      isinstance(gj._feature_props(json.loads(MINIMAL)), dict))


print("== R-8. atomic save keeps the file mode instead of mkstemp's 0600 ==")
import stat as _stat

mode_dir = tempfile.mkdtemp(prefix="agr_json_mode_")
mode_path = os.path.join(mode_dir, "SM_Mode.geojson")
payload = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"address": "X"},
     "geometry": {"type": "Point", "coordinates": [1, 2]}, "Glasses": []}]}
with open(mode_path, "w", encoding="utf-8") as fh:
    json.dump(payload, fh)
os.chmod(mode_path, 0o664)
mode_before = _stat.S_IMODE(os.stat(mode_path).st_mode)
gj._save_geojson(mode_path, payload)
mode_after = _stat.S_IMODE(os.stat(mode_path).st_mode)
check("existing file keeps its permission bits", mode_before == mode_after,
      f"{oct(mode_before)} -> {oct(mode_after)}")
check("no .agr_geojson_ temp file left behind",
      not [f for f in os.listdir(mode_dir) if f.startswith(".agr_geojson_")],
      str(os.listdir(mode_dir)))
check("content still parses after the atomic save",
      json.load(open(mode_path, encoding="utf-8"))["features"][0]["properties"]["address"] == "X")

# a brand-new file must not inherit mkstemp's owner-only 0600
fresh_path = os.path.join(mode_dir, "SM_Fresh.geojson")
gj._save_geojson(fresh_path, payload)
fresh_mode = _stat.S_IMODE(os.stat(fresh_path).st_mode)
check("a newly created geojson is group/world readable",
      fresh_mode & 0o044 or os.name == 'nt', oct(fresh_mode))
shutil.rmtree(mode_dir, ignore_errors=True)


for path in TMP_ROOTS:
    shutil.rmtree(path, ignore_errors=True)

print("=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} CHECKS FAILED:")
    for name in FAILS:
        print("  -", name)
    sys.exit(1)
print("✅ ALL CHECKS PASSED")
