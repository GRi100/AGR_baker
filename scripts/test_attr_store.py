# Headless test for AGR_tools/core/attr_store.py (generic idprop+color-mirror
# transport shared by AGR Link / UDIM / atlas records).
# Run: blender --background --factory-startup --python scripts/test_attr_store.py
import json
import os
import sys

import bpy
import numpy as np

# repo root = parent of scripts/ — works from any checkout location
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from AGR_tools.core.attr_store import (ColorBlobStore, preserve_active_color,
                                       read_srgb_bytes)

FAILS = []


def check(name, cond, extra=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def reset_scene():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for me in list(bpy.data.meshes):
        if me.users == 0:
            bpy.data.meshes.remove(me)


CUBE_VERTS = [(-0.5, -0.5, -0.5), (0.5, -0.5, -0.5), (0.5, 0.5, -0.5), (-0.5, 0.5, -0.5),
              (-0.5, -0.5, 0.5), (0.5, -0.5, 0.5), (0.5, 0.5, 0.5), (-0.5, 0.5, 0.5)]
CUBE_FACES = [(0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1),
              (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0)]
QUAD_VERTS = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
QUAD_FACES = [(0, 1, 2, 3)]


def make_obj(name, verts=CUBE_VERTS, faces=CUBE_FACES):
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata(verts, [], [list(f) for f in faces])
    mesh.validate()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


STORE_A = ColorBlobStore(prefix="AGR_TstA_T", magic=b"AGTA", prop_key="agr_test_a",
                         validator=lambda d: "marker" in d,
                         idprop_exclude=("secret_",))
STORE_B = ColorBlobStore(prefix="AGR_TstB_T", magic=b"AGTB", prop_key="agr_test_b")

RECORD = {
    "marker": 1,
    "название": "Ленинский 71, корпус 2 — тест",
    "layout": [{"set": f"S_{i}", "u": i * 0.125, "n": i} for i in range(8)],
    "flags": [True, False, None],
    "secret_payload": "X" * 300,
}
LEAN = {k: v for k, v in RECORD.items() if not k.startswith("secret_")}


print("== 1. basic roundtrip ==")
reset_scene()
o1 = make_obj("StoreBasic")
check("write returns True (mirror written)", STORE_A.write(o1, RECORD) is True)
raw = o1.get("agr_test_a")
check("idprop present", isinstance(raw, str))
check("idprop_exclude keeps secret_* out of idprop", "secret_payload" not in raw)
check("idprop carries the lean record", json.loads(raw) == LEAN)
t0 = o1.data.attributes.get("AGR_TstA_T0")
check("mirror attr exists (CORNER FLOAT_COLOR)",
      t0 is not None and t0.domain == 'CORNER' and t0.data_type == 'FLOAT_COLOR')
check("read prefers the idprop (lean)", STORE_A.read(o1) == LEAN)
o1.pop("agr_test_a", None)
check("read falls back to colors (FULL record)", STORE_A.read(o1) == RECORD)

print("== 2. peek cache ==")
STORE_A.write_idprop(o1, RECORD)
check("peek == lean record", STORE_A.peek(o1) == LEAN)
check("peek returns the cached object", STORE_A.peek(o1) is STORE_A.peek(o1))
o1["agr_test_a"] = json.dumps({"marker": 2})
check("peek revalidates against the raw string", STORE_A.peek(o1) == {"marker": 2})
o2 = make_obj("StoreColorsOnly")
STORE_A.write(o2, RECORD)
o2.pop("agr_test_a", None)
check("peek colors-only path decodes", STORE_A.peek(o2) == RECORD)
check("peek colors-only path is cached", STORE_A.peek(o2) is STORE_A.peek(o2))
o3 = make_obj("StoreForeign")
check("peek on a non-carrier is None", STORE_A.peek(o3) is None)

print("== 3. strip ==")
STORE_A.strip(o1)
check("strip removes the idprop", o1.get("agr_test_a") is None)
check("strip removes the mirror", not STORE_A.color_names(o1.data))
check("peek after strip is None", STORE_A.peek(o1) is None)

print("== 4. corruption is detected, never guessed ==")
o4 = make_obj("StoreCorrupt")
STORE_A.write(o4, RECORD)
attr = o4.data.attributes.get("AGR_TstA_T0")
arr = np.zeros(len(attr.data) * 4, dtype=np.float32)
attr.data.foreach_get("color_srgb", arr)
arr[20] = 1.0 if arr[20] < 0.5 else 0.0  # flip one payload byte
attr.data.foreach_set("color_srgb", arr)
check("flipped byte -> decode None (CRC)", STORE_A.decode_colors(o4.data) is None)
o5 = make_obj("StoreMagic")
STORE_A.write(o5, RECORD)
alien = ColorBlobStore(prefix="AGR_TstA_T", magic=b"XXXX", prop_key="agr_alien")
check("foreign magic -> decode None", alien.decode_colors(o5.data) is None)
o6 = make_obj("StoreSchema")
check("pack does not validate (writer's duty)",
      STORE_A.pack_colors(o6.data, {"foo": 1}) is True)
check("validator rejects a foreign schema on decode",
      STORE_A.decode_colors(o6.data) is None)

print("== 5. truncation ==")
o7 = make_obj("StoreTrunc", QUAD_VERTS, QUAD_FACES)  # 4 loops -> 16 bytes/attr
big = {"marker": 1, "data": "".join(f"{i:04d}" for i in range(500))}
check("multi-attr write ok", STORE_A.write(o7, big) is True)
names = STORE_A.color_names(o7.data)
check("blob spans several attrs", len(names) > 1, f"attrs={len(names)}")
o7.data.attributes.remove(o7.data.attributes.get(names[-1]))
check("missing chunk -> decode None", STORE_A.decode_colors(o7.data) is None)

print("== 6. capacity guard ==")
# 3 attrs x 16 bytes = 48 bytes: fits the minimal v2 frame (18-byte
# header), still refuses the big record
tiny = ColorBlobStore(prefix="AGR_TstC_T", magic=b"AGTC", prop_key="agr_test_c",
                      max_attrs=3)
o8 = make_obj("StoreTiny", QUAD_VERTS, QUAD_FACES)
check("small record fits", tiny.pack_colors(o8.data, {"m": 1}) is True)
old_names = tiny.color_names(o8.data)
check("over-capacity pack refused", tiny.pack_colors(o8.data, big) is False)
check("old mirror survives a refused pack (checked BEFORE removal)",
      tiny.color_names(o8.data) == old_names)
check("write() removes the stale mirror on failure",
      tiny.write(o8, big) is False and not tiny.color_names(o8.data))
check("...but the idprop still carries the record",
      json.loads(o8.get("agr_test_c"))["marker"] == 1)
o9 = make_obj("StoreNoLoops", QUAD_VERTS, [])  # vertices only, 0 loops
check("0 loops -> pack refused", STORE_A.pack_colors(o9.data, RECORD) is False)
check("0 loops -> write False, idprop still set",
      STORE_A.write(o9, RECORD) is False and o9.get("agr_test_a") is not None)

print("== 7. active/render color preserved ==")
o10 = make_obj("StoreActive")
user_col = o10.data.color_attributes.new(name="Col", type='BYTE_COLOR', domain='CORNER')
o10.data.color_attributes.active_color = user_col
o10.data.color_attributes.render_color_index = 0
STORE_A.write(o10, RECORD)
check("active color stays the user's layer",
      o10.data.color_attributes.active_color is not None
      and o10.data.color_attributes.active_color.name == "Col")
ridx = o10.data.color_attributes.render_color_index
check("render color stays the user's layer",
      0 <= ridx < len(o10.data.color_attributes)
      and o10.data.color_attributes[ridx].name == "Col")
with preserve_active_color(o10.data):
    o10.data.color_attributes.new(name="Scratch", type='FLOAT_COLOR', domain='CORNER')
check("contextmanager restores active after new()",
      o10.data.color_attributes.active_color.name == "Col")

print("== 8. namespaces coexist ==")
o11 = make_obj("StoreCoexist")
STORE_A.write(o11, RECORD)
STORE_B.write(o11, {"kind": "B", "n": 7})
check("A readable next to B", STORE_A.read(o11) == LEAN)
check("B readable next to A", STORE_B.read(o11) == {"kind": "B", "n": 7})
STORE_A.strip(o11)
check("strip(A) leaves B's idprop", o11.get("agr_test_b") is not None)
check("strip(A) leaves B's mirror", o11.data.attributes.get("AGR_TstB_T0") is not None)
check("B still decodes after strip(A)", STORE_B.read(o11) == {"kind": "B", "n": 7})

print("== 9. FBX roundtrip (default settings, BYTE_COLOR quantisation) ==")
reset_scene()
o12 = make_obj("StoreFbx")
STORE_A.write(o12, RECORD)
for obj in bpy.data.objects:
    obj.select_set(obj is o12)
bpy.context.view_layer.objects.active = o12
fbx_path = os.path.join(bpy.app.tempdir, "agr_attr_store_rt.fbx")
bpy.ops.export_scene.fbx(filepath=fbx_path, use_selection=True)  # NO custom props
reset_scene()
bpy.ops.import_scene.fbx(filepath=fbx_path)
carrier = next((o for o in bpy.data.objects
                if o.type == 'MESH' and o.data.attributes.get("AGR_TstA_T0")), None)
check("fbx: carrier found via mirror", carrier is not None)
check("fbx: no idprop after import", carrier is not None and carrier.get("agr_test_a") is None)
check("fbx: FULL record decoded bit-exact",
      carrier is not None and STORE_A.read(carrier) == RECORD)

print("== 10. scan_windows: records survive a PLAIN Blender join ==")
reset_scene()
w1 = make_obj("WinCarrier1")
w2 = make_obj("WinCarrier2")
plain = make_obj("WinPlain")  # no record - becomes the zero-filled block
rec1 = {"marker": 1, "who": "first", "pad": list(range(40))}
rec2 = {"marker": 1, "who": "second"}
STORE_A.write(w1, rec1)
STORE_A.write(w2, rec2)
n_loops_plain = len(plain.data.loops)
n_loops_w1 = len(w1.data.loops)
for obj in bpy.data.objects:
    obj.select_set(True)
bpy.context.view_layer.objects.active = plain  # plain object ACTIVE: idprops die
bpy.ops.object.join()
joined = plain.data
wins = STORE_A.scan_windows(joined)
check("scan: two windows found", len(wins) == 2, str([(s, c) for s, c, _r in wins]))
if len(wins) == 2:
    check("scan: first window starts after the plain block",
          wins[0][0] == n_loops_plain, str(wins[0][0]))
    check("scan: second window at its own block offset",
          wins[1][0] == n_loops_plain + n_loops_w1, str(wins[1][0]))
    check("scan: first record intact", wins[0][2] == rec1)
    check("scan: second record intact", wins[1][2] == rec2)
check("scan: whole-mesh decode fails (magic not at loop 0)",
      STORE_A.decode_colors(joined) is None)
check("scan: read() falls back to the first window (UDIM/atlas survival)",
      STORE_A.read(plain) == rec1)
check("scan: no windows on a clean mesh",
      STORE_A.scan_windows(make_obj("WinClean").data) == [])

print("== 10b. exact v2 windows: multi-layer blob + plain TAIL after it ==")
reset_scene()
carrier = make_obj("TailCarrier")
tail = make_obj("TailPlain")
# big record -> blob spans SEVERAL layers on a 24-loop cube (v1 windows
# could not reconstruct it when a non-carrier block follows)
rec_big = {"marker": 1, "blob": "x" * 700, "nums": list(range(120))}
STORE_A.write(carrier, rec_big)
n_layers = len([a for a in carrier.data.attributes if a.name.startswith("AGR_TstA_T")])
check("tail: blob spans >= 2 layers", n_layers >= 2, str(n_layers))
n_carrier_loops = len(carrier.data.loops)
for obj in bpy.data.objects:
    obj.select_set(True)
bpy.context.view_layer.objects.active = carrier  # carrier FIRST, tail after
bpy.ops.object.join()
wins = STORE_A.scan_windows(carrier.data)
check("tail: window found despite the foreign tail", len(wins) == 1,
      str([(s, c) for s, c, _r in wins]))
if len(wins) == 1:
    check("tail: exact loop_count (no tail swallowed)",
          wins[0][0] == 0 and wins[0][1] == n_carrier_loops,
          str((wins[0][0], wins[0][1])))
    check("tail: record intact", wins[0][2] == rec_big)

print("== 10c. legacy v1 frames still decode ==")
reset_scene()
STORE_V1 = ColorBlobStore(prefix="AGR_TstA_T", magic=b"AGTA", prop_key="agr_test_a",
                          validator=lambda d: "marker" in d, version=1)
o_v1 = make_obj("V1Carrier")
rec_v1 = {"marker": 1, "legacy": True}
check("v1: write ok", STORE_V1.write(o_v1, rec_v1) is True)
check("v1: v2-reader decodes whole-mesh", STORE_A.read(o_v1) == rec_v1)
o_v1.pop("agr_test_a", None)
wins_v1 = STORE_A.scan_windows(o_v1.data)
check("v1: v2-scanner finds the v1 window",
      len(wins_v1) == 1 and wins_v1[0][2] == rec_v1)

print("== 10d. service layer never becomes active/default color ==")
reset_scene()
o_clean = make_obj("NoColors")   # mesh WITHOUT any color attributes
STORE_A.write(o_clean, RECORD)
check("cleanmesh: active color stays unset",
      o_clean.data.color_attributes.active_color is None,
      str(o_clean.data.color_attributes.active_color))
check("cleanmesh: default/render color stays unset",
      o_clean.data.color_attributes.render_color_index == -1,
      str(o_clean.data.color_attributes.render_color_index))

print("=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} CHECKS FAILED:")
    for name in FAILS:
        print("  -", name)
    sys.exit(1)
print("✅ ALL CHECKS PASSED")
