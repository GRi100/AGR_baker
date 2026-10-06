# Headless test for AGR_tools/operators_lights.py
# Run: blender --background --factory-startup --python scripts/test_lights.py
import os
import sys

import bpy
from mathutils import Vector

# repo root = parent of scripts/ — works from any checkout location
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import AGR_tools.log as agr_log
import AGR_tools.operators_lights as lights

agr_log.register()
lights.register()

FAILS = []


def check(name, cond, extra=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def close(a, b, tol=1e-5):
    return (Vector(a) - Vector(b)).length < tol


def reset_scene():
    bpy.ops.object.select_all(action='DESELECT')
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for me in list(bpy.data.meshes):
        if me.users == 0:
            bpy.data.meshes.remove(me)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)


def tri(name):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def select_only(obj):
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


# ===================================================================
print("\n=== TEST 1: replace_with_light keeps the placeholder's parent ===")
reset_scene()

root = bpy.data.objects.new("Addr_Root", None)          # EMPTY parent
bpy.context.scene.collection.objects.link(root)
root.location = (5.0, 1.0, 0.0)

placeholder = tri("Placeholder")
placeholder.parent = root
placeholder.location = (3.0, 0.0, 2.0)

child = tri("ChildOfPlaceholder")
child.parent = placeholder
child.location = (0.0, 0.0, 1.0)

bpy.context.view_layer.update()
ph_world = placeholder.matrix_world.translation.copy()
child_world_before = child.matrix_world.translation.copy()
placeholder["agr_marker"] = "keep me"

select_only(placeholder)
res = bpy.ops.agr.replace_with_light()
bpy.context.view_layer.update()

light = bpy.data.objects.get("Placeholder_Light")
check("1.1 operator finished", res == {'FINISHED'}, str(res))
check("1.2 light created", light is not None)
check("1.3 light inherits the parent",
      light is not None and light.parent is root,
      str(None if light is None or light.parent is None else light.parent.name))
check("1.4 light stands exactly where the placeholder stood",
      light is not None and close(light.matrix_world.translation, ph_world),
      f"{tuple(light.matrix_world.translation)} vs {tuple(ph_world)}")
check("1.5 the Root empty sees a LIGHT child (rename Root button stays on)",
      any(c.type == 'LIGHT' for c in root.children))

child_after = bpy.data.objects.get("ChildOfPlaceholder")
check("1.6 the placeholder's child survives and is re-parented to the light",
      child_after is not None and child_after.parent is light,
      str(None if child_after is None or child_after.parent is None
          else child_after.parent.name))
check("1.7 the child did NOT move in world space",
      child_after is not None
      and close(child_after.matrix_world.translation, child_world_before),
      f"{tuple(child_after.matrix_world.translation)} vs {tuple(child_world_before)}")
check("1.8 custom properties travel to the light",
      light is not None and light.get("agr_marker") == "keep me",
      str(dict(light.items()) if light else {}))
check("1.9 the placeholder itself is gone",
      bpy.data.objects.get("Placeholder") is None)


# ===================================================================
print("\n=== TEST 2: unparented placeholders behave exactly as before ===")
reset_scene()

lonely = tri("SM_Addr_Ground")
lonely.location = (1.0, 2.0, 3.0)
bpy.context.view_layer.update()
world_before = lonely.matrix_world.translation.copy()

select_only(lonely)
bpy.ops.agr.replace_with_light()
bpy.context.view_layer.update()

light = bpy.data.objects.get("SM_Addr_Ground_Light")
check("2.1 light created without a parent",
      light is not None and light.parent is None)
check("2.2 world transform preserved",
      light is not None and close(light.matrix_world.translation, world_before))
check("2.3 collections preserved",
      light is not None and light.users_collection[0] == bpy.context.scene.collection)


# ===================================================================
print("\n=== TEST 3: several placeholders under one Root ===")
reset_scene()

root = bpy.data.objects.new("Addr_Root", None)
bpy.context.scene.collection.objects.link(root)
placeholders = []
for i in range(3):
    p = tri(f"P{i}")
    p.parent = root
    p.location = (float(i), 0.0, 0.0)
    placeholders.append(p)
bpy.context.view_layer.update()
worlds = [p.matrix_world.translation.copy() for p in placeholders]

bpy.ops.object.select_all(action='DESELECT')
for p in placeholders:
    p.select_set(True)
bpy.context.view_layer.objects.active = placeholders[0]
bpy.ops.agr.replace_with_light()
bpy.context.view_layer.update()

lights_under_root = [c for c in root.children if c.type == 'LIGHT']
check("3.1 all three lights hang under the Root", len(lights_under_root) == 3,
      str(len(lights_under_root)))
placed = all(
    any(close(c.matrix_world.translation, w) for c in lights_under_root)
    for w in worlds)
check("3.2 every light landed on its placeholder's world position", placed)


# ===================================================================
print("\n=== TEST 4: distance overlay is computed once per change ===")
reset_scene()

coll = bpy.data.collections.new("Lights")
bpy.context.scene.collection.children.link(coll)
for i in range(12):
    data = bpy.data.lights.new(name=f"Omni{i}", type='POINT')
    obj = bpy.data.objects.new(f"Omni{i}", data)
    coll.objects.link(obj)
    obj.location = (i * 1.0, 0.0, 0.0)   # 1 m apart => every pair violates

settings = bpy.context.scene.agr_light_settings
settings.dist_collection = coll
settings.dist_show_spheres = True
bpy.context.view_layer.update()

calls = {"n": 0}
real_build = lights._build_overlay_data


def counting_build(lights_, settings_):
    calls["n"] += 1
    return real_build(lights_, settings_)


lights._build_overlay_data = counting_build
try:
    lights._overlay_cache["fp"] = None
    lights._overlay_cache["data"] = None

    data = lights._get_overlay_data(bpy.context.scene)
    check("4.1 first call builds the overlay", calls["n"] == 1 and data is not None)
    check("4.2 violations found on a 1 m grid", data["violation_count"] > 0,
          str(data["violation_count"]))
    check("4.3 spheres built (3 rings × 32 segments × 2 pts per light)",
          len(data["red_spheres"]) + len(data["green_spheres"]) == 12 * 3 * 32 * 2,
          str(len(data["red_spheres"]) + len(data["green_spheres"])))

    # both draw callbacks plus a dozen viewport redraws
    for _ in range(20):
        lights._get_overlay_data(bpy.context.scene)
    check("4.4 twenty redraws without a change rebuild nothing", calls["n"] == 1,
          str(calls["n"]))

    # moving a light bumps the depsgraph counter -> exactly one rebuild
    lights._bump_geo_version(bpy.context.scene)
    lights._get_overlay_data(bpy.context.scene)
    lights._get_overlay_data(bpy.context.scene)
    check("4.5 a depsgraph update triggers exactly one rebuild", calls["n"] == 2,
          str(calls["n"]))

    # a settings change must be seen even though the depsgraph did not move
    settings.dist_omni_radius = 3.0
    lights._get_overlay_data(bpy.context.scene)
    check("4.6 a settings change invalidates the cache", calls["n"] == 3,
          str(calls["n"]))

    # SUN/AREA lights are counted, and the count survives a cache hit
    sun_data = bpy.data.lights.new(name="Sun", type='SUN')
    sun = bpy.data.objects.new("Sun", sun_data)
    coll.objects.link(sun)
    lights._bump_geo_version(bpy.context.scene)
    data = lights._get_overlay_data(bpy.context.scene)
    check("4.7 forbidden light types are reported", data["bad_types"] == 1,
          str(data["bad_types"]))
    data = lights._get_overlay_data(bpy.context.scene)
    check("4.8 the counter survives a cache hit", data["bad_types"] == 1)
finally:
    lights._build_overlay_data = real_build


# ===================================================================
print("\n=== TEST 5: sphere geometry matches the old Python build ===")

positions = [Vector((0.0, 0.0, 0.0)), Vector((10.0, 0.0, 0.0))]
radii = [5.0, 1.5]
expected = []
for pos, radius in zip(positions, radii):
    for ring in lights._UNIT_RINGS:
        for k in range(len(ring) - 1):
            for pt in (ring[k], ring[k + 1]):
                expected.append((pos.x + pt[0] * radius,
                                 pos.y + pt[1] * radius,
                                 pos.z + pt[2] * radius))

reset_scene()
coll = bpy.data.collections.new("Pair")
bpy.context.scene.collection.children.link(coll)
for i, (pos, radius) in enumerate(zip(positions, radii)):
    data = bpy.data.lights.new(name=f"L{i}", type='POINT' if i == 0 else 'SPOT')
    obj = bpy.data.objects.new(f"L{i}", data)
    coll.objects.link(obj)
    obj.location = pos
settings = bpy.context.scene.agr_light_settings
settings.dist_collection = coll
settings.dist_omni_radius = 5.0
settings.dist_spot_radius = 1.5
settings.dist_show_spheres = True
bpy.context.view_layer.update()
lights._bump_geo_version(bpy.context.scene)
data = lights._get_overlay_data(bpy.context.scene)

built = data["green_spheres"] + data["red_spheres"]
check("5.1 same number of sphere vertices as the reference build",
      len(built) == len(expected), f"{len(built)} vs {len(expected)}")
# compare as multisets of quantised points: the float32 build can order two
# coincident vertices differently, which says nothing about correctness
def quant(points):
    return sorted(tuple(round(c, 4) for c in p) for p in points)


check("5.2 the same point cloud as the reference build",
      quant(built) == quant(expected),
      f"{len(built)} pts, first mismatch "
      f"{next((f'{a} vs {b}' for a, b in zip(quant(built), quant(expected)) if a != b), 'none')}")

# ===================================================================
print("\n=== TEST 6: the overlay counter ignores unrelated depsgraph traffic ===")
reset_scene()

coll6 = bpy.data.collections.new("Lights6")
bpy.context.scene.collection.children.link(coll6)
for i in range(4):
    data = bpy.data.lights.new(name=f"Om{i}", type='POINT')
    lo = bpy.data.objects.new(f"Om{i}", data)
    coll6.objects.link(lo)
    lo.location = (i * 1.0, 0.0, 0.0)
prop = tri("UnrelatedBuilding")          # a plain mesh, nothing to do with light
bpy.context.scene.agr_light_settings.dist_collection = coll6
bpy.context.view_layer.update()


def bumped(fn):
    """Run fn, then let the depsgraph handler fire, and report the delta."""
    bpy.context.view_layer.update()
    before = lights._geo_version
    fn()
    bpy.context.view_layer.update()
    return lights._geo_version != before


check("6.1 moving a light still invalidates the overlay",
      bumped(lambda: setattr(bpy.data.objects["Om0"], "location", (7.0, 0.0, 0.0))))
check("6.2 changing a light's data type still invalidates",
      bumped(lambda: setattr(bpy.data.lights["Om1"], "type", 'SPOT')))
check("6.3 linking a light into another collection still invalidates",
      bumped(lambda: bpy.data.collections.new("Extra6").objects.link(
          bpy.data.objects["Om2"])))
# THE regression: a modal transform of an unrelated building used to rebuild
# the whole overlay on every tick (57-65 ms at 2000 lights per tick)
check("6.4 moving an unrelated mesh does NOT invalidate the overlay",
      not bumped(lambda: setattr(prop, "location", (12.0, 3.0, 0.0))))
check("6.5 editing an unrelated mesh's geometry does NOT invalidate",
      not bumped(lambda: setattr(prop.data.vertices[0], "co", (0.4, 0.0, 0.0))))
check("6.6 a manual call without a depsgraph still bumps (tests / old API)",
      bumped(lambda: lights._bump_geo_version(bpy.context.scene)))


# ===================================================================
print("\n=== TEST 7: replace_with_light hierarchy details ===")
reset_scene()

# --- 7a: VERTEX parenting keeps its vertex indices -------------------
host = tri("VertexHost")
host.location = (2.0, 0.0, 0.0)
ph_v = tri("VertPlaceholder")
ph_v.parent = host
ph_v.parent_type = 'VERTEX_3'
ph_v.parent_vertices = (0, 1, 2)
ph_v.location = (0.0, 0.5, 1.0)
bpy.context.view_layer.update()
world_before = ph_v.matrix_world.translation.copy()

select_only(ph_v)
res = bpy.ops.agr.replace_with_light()
bpy.context.view_layer.update()
vlight = bpy.data.objects.get("VertPlaceholder_Light")
check("7.1 vertex-parented placeholder replaced", res == {'FINISHED'} and vlight is not None)
check("7.2 parent_type preserved",
      vlight is not None and vlight.parent_type == 'VERTEX_3',
      str(vlight.parent_type if vlight else None))
check("7.3 parent_vertices preserved (was silently reset to vertex 0)",
      vlight is not None and tuple(vlight.parent_vertices) == (0, 1, 2),
      str(tuple(vlight.parent_vertices) if vlight else None))
check("7.4 world position unchanged",
      vlight is not None and close(vlight.matrix_world.translation, world_before),
      f"{tuple(vlight.matrix_world.translation)} vs {tuple(world_before)}")

# --- 7b: a chain of placeholders (the children map is precomputed) ----
reset_scene()
outer = tri("Outer")
outer.location = (1.0, 0.0, 0.0)
inner = tri("Inner")
inner.parent = outer
inner.location = (0.0, 2.0, 0.0)
leaf = tri("Leaf")
leaf.parent = inner
leaf.location = (0.0, 0.0, 3.0)
bpy.context.view_layer.update()
leaf_world = leaf.matrix_world.translation.copy()
inner_world = inner.matrix_world.translation.copy()

bpy.ops.object.select_all(action='DESELECT')
outer.select_set(True)
inner.select_set(True)
bpy.context.view_layer.objects.active = outer
res = bpy.ops.agr.replace_with_light()
bpy.context.view_layer.update()

outer_light = bpy.data.objects.get("Outer_Light")
inner_light = bpy.data.objects.get("Inner_Light")
leaf_after = bpy.data.objects.get("Leaf")
check("7.5 both placeholders of the chain became lights",
      res == {'FINISHED'} and outer_light is not None and inner_light is not None)
check("7.6 the inner light hangs under the outer light, not under a dead object",
      inner_light is not None and inner_light.parent is outer_light,
      str(None if inner_light is None or inner_light.parent is None
          else inner_light.parent.name))
check("7.7 the inner light kept its world position",
      inner_light is not None and close(inner_light.matrix_world.translation, inner_world),
      f"{tuple(inner_light.matrix_world.translation)} vs {tuple(inner_world)}")
check("7.8 the leaf survived, re-parented onto the inner light, in place",
      leaf_after is not None and leaf_after.parent is inner_light
      and close(leaf_after.matrix_world.translation, leaf_world),
      str(None if leaf_after is None or leaf_after.parent is None
          else leaf_after.parent.name))
check("7.9 no placeholder left behind",
      bpy.data.objects.get("Outer") is None and bpy.data.objects.get("Inner") is None)


# ===================================================================
print("\n" + "=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} CHECK(S) FAILED:")
    for name in FAILS:
        print("   -", name)
else:
    print("✅ ALL CHECKS PASSED")
print("=" * 60)
