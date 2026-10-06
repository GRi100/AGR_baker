# Headless test for AGR_tools/core/materials.py + operators_sets.py +
# operators_frame.py (no baking — see scripts/test_bake.py for the Cycles part).
# Run: blender --background --factory-startup --python scripts/test_materials.py
import os
import shutil
import sys

import bpy

# repo root = parent of scripts/ — works from any checkout location
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import AGR_tools.log as agr_log
import AGR_tools.properties as props
import AGR_tools.operators_sets as sets_mod
import AGR_tools.operators_frame as frame_mod
import AGR_tools.ui as ui
from AGR_tools.core import materials

agr_log.register()
props.register()
sets_mod.register()
frame_mod.register()
ui.register()

try:
    from PIL import Image
except ImportError:
    print("❌ Pillow is required for this test (Blender's Python: pip install Pillow)")
    sys.exit(1)

FAILS = []


def expect_cancel(callop):
    """True only for a clean CANCELLED / report-ERROR outcome (no traceback)."""
    try:
        return callop() == {'CANCELLED'}
    except RuntimeError as exc:
        return 'Traceback' not in str(exc)


def check(name, cond, extra=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


# --------------------------------------------------------------- workspace
ROOT = os.path.join(bpy.app.tempdir, "agr_materials_test")
if os.path.isdir(ROOT):
    shutil.rmtree(ROOT, ignore_errors=True)
os.makedirs(ROOT)
BLEND = os.path.join(ROOT, "scene.blend")
bpy.ops.wm.save_as_mainfile(filepath=BLEND)
AGR_BAKE = os.path.join(ROOT, "AGR_BAKE")
os.makedirs(AGR_BAKE, exist_ok=True)

HIGH_TYPES = ("DiffuseOpacity", "ERM", "Normal")
LOW_TYPES = ("Diffuse", "Roughness", "Metallic", "Opacity", "Normal")


def png(path, color, size=(64, 64), mode='RGB'):
    Image.new(mode, size, color).save(path, 'PNG')


def make_set(material_name, types=HIGH_TYPES, color=(200, 10, 10), size=(64, 64)):
    """Create S_<material_name> with the given texture types on disk."""
    folder = os.path.join(AGR_BAKE, f"S_{material_name}")
    os.makedirs(folder, exist_ok=True)
    for tex_type in types:
        png(os.path.join(folder, f"T_{material_name}_{tex_type}.png"), color, size)
    return folder


def clear_data():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for mat in list(bpy.data.materials):
        bpy.data.materials.remove(mat)
    for img in list(bpy.data.images):
        bpy.data.images.remove(img)


def new_material(name, marker=True):
    """Material with a recognisable graph: an Emission driving the output."""
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    if marker:
        nt = mat.node_tree
        emission = nt.nodes.new('ShaderNodeEmission')
        emission.name = 'MARKER'
        out = next(n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL')
        nt.links.new(emission.outputs['Emission'], out.inputs['Surface'])
    return mat


def node_types(mat):
    return sorted(n.type for n in mat.node_tree.nodes)


def linked_from(bsdf, socket_name):
    socket = bsdf.inputs[socket_name]
    return socket.links[0].from_node if socket.links else None


def bsdf_of(mat):
    return next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)


def select_only_sets(names):
    found = 0
    for ts in bpy.context.scene.agr_texture_sets:
        ts.is_selected = ts.material_name in names
        found += 1 if ts.is_selected else 0
    return found


def refresh():
    bpy.ops.agr.refresh_texture_sets(skip_alpha_strip=True)


print("== 1. acquire_image reuses the datablock of the same file (BAKE-4) ==")
clear_data()
folder1 = make_set("Reuse")
path1 = os.path.join(folder1, "T_Reuse_ERM.png")
img_a, created_a = materials.acquire_image(path1, "T_Reuse_ERM", 'Non-Color')
img_b, created_b = materials.acquire_image(path1, "T_Reuse_ERM", 'Non-Color')
check("first acquire creates a datablock", created_a is True and img_a is not None)
check("second acquire reuses it", created_b is False and img_b is img_a)
check("no .001 duplicate spawned", len(bpy.data.images) == 1, str(list(bpy.data.images.keys())))

print("== 2. a zero-byte PNG never becomes a usable image ==")
clear_data()
folder2 = make_set("Broken")
open(os.path.join(folder2, "T_Broken_DiffuseOpacity.png"), "wb").close()
img_bad, created_bad = materials.acquire_image(
    os.path.join(folder2, "T_Broken_DiffuseOpacity.png"), "T_Broken_DiffuseOpacity")
check("broken PNG -> None", img_bad is None and created_bad is False)
check("no (0,0) leftovers in bpy.data",
      all(i.size[0] > 0 for i in bpy.data.images), str([(i.name, tuple(i.size)) for i in bpy.data.images]))

print("== 3. HIGH connect refuses BEFORE clearing the graph (BAKE-1) ==")
clear_data()
mat3 = new_material("M_Broken")
before = node_types(mat3)
result = materials.connect_texture_set_to_material(mat3, folder2, "Broken")
check("connect returns None", result is None)
check("node graph untouched", node_types(mat3) == before, f"{before} -> {node_types(mat3)}")
check("marker node alive", mat3.node_tree.nodes.get('MARKER') is not None)
check("no image datablocks left", len(bpy.data.images) == 0,
      str([(i.name, tuple(i.size)) for i in bpy.data.images]))

print("== 4. connect_best falls back to LOW when HIGH is unreadable ==")
clear_data()
folder4 = make_set("Fallback", types=HIGH_TYPES + LOW_TYPES)
open(os.path.join(folder4, "T_Fallback_DiffuseOpacity.png"), "wb").close()
mat4 = new_material("M_Fallback")
res4 = materials.connect_best_texture_set_to_material(mat4, folder4, "Fallback")
check("connect_best succeeded via LOW", res4 is mat4)
b4 = bsdf_of(mat4)
check("Base Color linked to Diffuse",
      linked_from(b4, 'Base Color') is not None
      and linked_from(b4, 'Base Color').image.name == "T_Fallback_Diffuse")

print("== 5. LOW mode connects Emit (BAKE-6) ==")
clear_data()
folder5 = make_set("Emissive", types=LOW_TYPES + ("Emit",))
check("validate_regular_mode does not require Emit",
      materials.validate_regular_mode(folder5, "Emissive") == [])
mat5 = new_material("M_Emissive")
res5 = materials.connect_regular_texture_set_to_material(mat5, folder5, "Emissive")
check("LOW connect succeeded", res5 is mat5)
b5 = bsdf_of(mat5)
emit_node = linked_from(b5, 'Emission Strength')
check("Emission Strength <- T_*_Emit",
      emit_node is not None and emit_node.image.name == "T_Emissive_Emit")
check("Emit image is Non-Color",
      emit_node is not None and emit_node.image.colorspace_settings.name == 'Non-Color')
check("Emission Color <- Diffuse",
      linked_from(b5, 'Emission Color') is not None
      and linked_from(b5, 'Emission Color').image.name == "T_Emissive_Diffuse")

print("== 5b. a set without Emit still connects in LOW mode ==")
clear_data()
folder5b = make_set("NoEmit", types=LOW_TYPES)
mat5b = new_material("M_NoEmit")
check("LOW connect without Emit",
      materials.connect_regular_texture_set_to_material(mat5b, folder5b, "NoEmit") is mat5b)
check("Emission Strength stays unlinked",
      linked_from(bsdf_of(mat5b), 'Emission Strength') is None)

print("== 6. twin material keeps its textures (BAKE-4, end-to-end) ==")
clear_data()
folder6 = make_set("Twin")
mat6 = bpy.data.materials.new("Twin")
materials.connect_texture_set_to_material(mat6, folder6, "Twin")
twin = mat6.copy()          # duplicate / Append / AGR Share copy
twin.name = "Twin.001"
twin_images = [n.image for n in twin.node_tree.nodes if n.type == 'TEX_IMAGE']
check("twin starts with 3 images", len(twin_images) == 3)
materials.connect_texture_set_to_material(mat6, folder6, "Twin")   # re-connect
still = [n.image for n in twin.node_tree.nodes if n.type == 'TEX_IMAGE']
check("twin images survive the re-connect", all(i is not None for i in still),
      str([getattr(i, 'name', None) for i in still]))
check("no .001 image duplicates",
      not any('.' in i.name for i in bpy.data.images), str(list(bpy.data.images.keys())))

print("== 7. cleanup_renamed_images spares referenced datablocks ==")
clear_data()
import AGR_tools.operators_bake as bake_mod
used = bpy.data.images.new("T_Clean_Diffuse.001", 8, 8)
orphan = bpy.data.images.new("T_Clean_Roughness.001", 8, 8)
holder = bpy.data.materials.new("Holder")
holder.use_nodes = True
node = holder.node_tree.nodes.new('ShaderNodeTexImage')
node.image = used
bake_mod.AGR_OT_SimpleBake.cleanup_renamed_images("Clean")
check("orphan .001 removed", "T_Clean_Roughness.001" not in bpy.data.images)
check("referenced .001 kept", "T_Clean_Diffuse.001" in bpy.data.images)
check("twin node still has its image", node.image is not None)

print("== 8. Connect / Assign reject atlas rows (BAKE-13) ==")
clear_data()
make_set("Regular")
atlas_dir = os.path.join(AGR_BAKE, "A_Test_Main_1024_3")
os.makedirs(atlas_dir, exist_ok=True)
for tex_type in HIGH_TYPES:
    png(os.path.join(atlas_dir, f"T_Test_Main_1024_3_{tex_type}.png"), (5, 5, 5))
refresh()
atlas_rows = [ts for ts in bpy.context.scene.agr_texture_sets if ts.is_atlas]
check("atlas row scanned", len(atlas_rows) == 1, str([ts.name for ts in bpy.context.scene.agr_texture_sets]))
select_only_sets({atlas_rows[0].material_name})
check("connect poll False on atlas-only selection",
      bpy.ops.agr.connect_set_to_material.poll() is False)
mesh_obj = bpy.data.objects.new("Obj", bpy.data.meshes.new("M"))
bpy.context.scene.collection.objects.link(mesh_obj)
bpy.context.view_layer.objects.active = mesh_obj
check("assign poll False on atlas-only selection",
      bpy.ops.agr.assign_set_to_active.poll() is False)
select_only_sets({"Regular", atlas_rows[0].material_name})
check("mixed selection: connect FINISHED", bpy.ops.agr.connect_set_to_material() == {'FINISHED'})
check("only the regular material was created",
      "Regular" in bpy.data.materials and atlas_rows[0].material_name not in bpy.data.materials,
      str(list(bpy.data.materials.keys())))

print("== 8b. Assign does not hang a blank material when the set is unreadable ==")
clear_data()
folder8b = make_set("Unreadable")
open(os.path.join(folder8b, "T_Unreadable_ERM.png"), "wb").close()
refresh()
select_only_sets({"Unreadable"})
obj8b = bpy.data.objects.new("Target", bpy.data.meshes.new("TargetMesh"))
bpy.context.scene.collection.objects.link(obj8b)
bpy.context.view_layer.objects.active = obj8b
check("assign FINISHED (reports the failure)", bpy.ops.agr.assign_set_to_active() == {'FINISHED'})
check("no material slot added", len(obj8b.material_slots) == 0)
check("no blank material left in the file", "Unreadable" not in bpy.data.materials,
      str(list(bpy.data.materials.keys())))

print("== 9. strip alpha touches only this set's own textures (BAKE-9) ==")
clear_data()
folder9 = make_set("Scoped")
own = os.path.join(folder9, "T_Scoped_DiffuseOpacity.png")
png(own, (10, 20, 30, 255), mode='RGBA')
foreign = os.path.join(folder9, "source_reference.png")
png(foreign, (10, 20, 30, 255), mode='RGBA')
converted = sets_mod.strip_useless_alpha_in_sets([(folder9, "Scoped")])
check("own RGBA with white alpha converted", converted == 1, f"converted={converted}")
with Image.open(own) as im:
    check("own file is RGB now", im.mode == 'RGB', im.mode)
with Image.open(foreign) as im:
    check("foreign PNG left alone", im.mode == 'RGBA', im.mode)

print("== 10. Resize keeps aspect and counts upscales (BAKE-12) ==")
check("fit_to_long_side 2048x1024 -> 1024x512",
      sets_mod.fit_to_long_side((2048, 1024), 1024) == (1024, 512))
check("fit_to_long_side square stays square",
      sets_mod.fit_to_long_side((512, 512), 256) == (256, 256))
check("fit_to_long_side no-op at target",
      sets_mod.fit_to_long_side((1024, 512), 1024) == (1024, 512))
clear_data()
folder10 = make_set("Aspect", types=("Diffuse",), size=(128, 64))
png(os.path.join(folder10, "T_Aspect_Normal.png"), (128, 128, 255), (32, 32))
refresh()
select_only_sets({"Aspect"})
check("resize FINISHED", bpy.ops.agr.resize_texture_set(target_resolution='256') == {'FINISHED'})
out10 = os.path.join(AGR_BAKE, "S_Aspect_256px")
with Image.open(os.path.join(out10, "T_Aspect_256px_Diffuse.png")) as im:
    check("non-square resized proportionally", im.size == (256, 128), str(im.size))
with Image.open(os.path.join(out10, "T_Aspect_256px_Normal.png")) as im:
    check("32px stub upscaled to 256 (counted, not silent)", im.size == (256, 256), str(im.size))

print("== 11. Frame on files: collisions rejected before any write (BAKE-2) ==")
files_dir = os.path.join(ROOT, "frame_files")
os.makedirs(files_dir, exist_ok=True)
jpg_path = os.path.join(files_dir, "Tex.jpg")
png_path = os.path.join(files_dir, "Tex.png")
Image.new('RGB', (512, 512), (0, 255, 0)).save(jpg_path, 'JPEG')
Image.new('RGB', (512, 512), (255, 0, 0)).save(png_path, 'PNG')
before_bytes = open(png_path, 'rb').read()
check("frame on jpg alone FINISHED",
      bpy.ops.agr.create_frame_on_files(
          directory=files_dir, files=[{"name": "Tex.jpg"}]) == {'FINISHED'})
check("existing Tex.png untouched", open(png_path, 'rb').read() == before_bytes)
# same root inside ONE selection: the .jpg is rejected, the .png frames itself
check("frame on jpg+png FINISHED",
      bpy.ops.agr.create_frame_on_files(
          directory=files_dir,
          files=[{"name": "Tex.jpg"}, {"name": "Tex.png"}]) == {'FINISHED'})
with Image.open(png_path) as im:
    center = im.convert('RGB').getpixel((256, 256))
check("Tex.png framed from ITSELF, not from the jpg", center[0] > 200 and center[1] < 60, str(center))
# a lone non-png source still produces its png sibling
tga_dir = os.path.join(ROOT, "frame_files2")
os.makedirs(tga_dir, exist_ok=True)
Image.new('RGB', (512, 512), (0, 0, 255)).save(os.path.join(tga_dir, "Solo.jpg"), 'JPEG')
check("frame on lone jpg FINISHED",
      bpy.ops.agr.create_frame_on_files(
          directory=tga_dir, files=[{"name": "Solo.jpg"}]) == {'FINISHED'})
check("png sibling created", os.path.exists(os.path.join(tga_dir, "Solo.png")))

print("== 12. Frame overlays: user directory, bundled read-only (BAKE-7) ==")
user_dir = frame_mod.get_frames_dir()
bundled_dir = frame_mod.get_bundled_frames_dir()
check("user dir is outside the addon",
      str(bundled_dir).lower() not in str(user_dir).lower(), f"{user_dir} vs {bundled_dir}")
bundled_names = [f.name for f in bundled_dir.iterdir()
                 if f.is_file() and f.suffix.lower() == '.png']
check("bundled templates found", len(bundled_names) > 0, str(bundled_names))
check("bundled templates are listed", set(bundled_names) <= set(frame_mod.list_frame_overlays()))
check("bundled template is flagged read-only", frame_mod.is_bundled_overlay(bundled_names[0]))
check("delete refuses a bundled template",
      expect_cancel(lambda: bpy.ops.agr.delete_frame_overlay(frame_name=bundled_names[0])))
check("bundled template still on disk", (bundled_dir / bundled_names[0]).exists())
# imported overlay lands in the user directory and IS deletable
imported = os.path.join(ROOT, "AGR_TEST_Overlay.png")
png(imported, (0, 0, 0, 0), (64, 64), mode='RGBA')
check("import FINISHED",
      bpy.ops.agr.add_frame_overlay(directory=ROOT, files=[{"name": "AGR_TEST_Overlay.png"}]) == {'FINISHED'})
check("imported file is in the user dir", (user_dir / "AGR_TEST_Overlay.png").exists())
check("imported file is NOT in the addon dir", not (bundled_dir / "AGR_TEST_Overlay.png").exists())
check("imported overlay is not read-only", not frame_mod.is_bundled_overlay("AGR_TEST_Overlay.png"))
check("resolve finds the imported overlay",
      frame_mod.resolve_overlay_path("AGR_TEST_Overlay.png") == str(user_dir / "AGR_TEST_Overlay.png"))
check("delete removes the imported overlay",
      bpy.ops.agr.delete_frame_overlay(frame_name="AGR_TEST_Overlay.png") == {'FINISHED'})
check("imported file gone", not (user_dir / "AGR_TEST_Overlay.png").exists())

print("== 13. colour round-trip through connect (regression) ==")
clear_data()
folder13 = make_set("Colour", color=(255, 0, 0))
mat13 = bpy.data.materials.new("Colour")
check("connect FINISHED",
      materials.connect_texture_set_to_material(mat13, folder13, "Colour") is mat13)
img13 = bpy.data.images["T_Colour_DiffuseOpacity"]
check("image loaded at the file size", tuple(img13.size) == (64, 64), str(tuple(img13.size)))
px = list(img13.pixels[0:4])
check("red pixel round-trips (sRGB)", px[0] > 0.99 and px[1] < 0.01 and px[2] < 0.01, str(px))

print("== 14. R-4: acquire_image never reuses a non-FILE datablock ==")
clear_data()
folder14 = make_set("Reuse", color=(255, 0, 0))
tex14 = os.path.join(folder14, "T_Reuse_DiffuseOpacity.png")

# a GENERATED datablock whose filepath_raw happens to point at the set file:
# reload() would free its buffers and regenerate a BLANK image, and the caller
# would wire that black texture into the material under a green report
gen = bpy.data.images.new("T_Reuse_DiffuseOpacity_gen", 64, 64)
gen.pixels.foreach_set([0.2, 0.4, 0.6, 1.0] * (64 * 64))
gen.filepath_raw = tex14
gen.use_fake_user = True          # keep it alive for the assertions below
check("probe datablock is GENERATED", gen.source == 'GENERATED', gen.source)

img14, created14 = materials.acquire_image(tex14, "T_Reuse_DiffuseOpacity")
check("R-4: a fresh FILE datablock is created", created14 is True)
check("R-4: the generated datablock is not reused", img14 is not gen)
check("R-4: the returned image is FILE-backed", img14 and img14.source == 'FILE',
      img14.source if img14 else "None")
px14 = list(img14.pixels[0:4]) if img14 else []
check("R-4: pixels come from disk (red), not a blank buffer",
      px14 and px14[0] > 0.99 and px14[1] < 0.01, str(px14))
check("R-4: the generated datablock kept its own pixels",
      abs(list(gen.pixels[0:4])[2] - 0.6) < 0.02, str(list(gen.pixels[0:4])))

# the normal case still reuses: a second call finds the FILE datablock
img14b, created14b = materials.acquire_image(tex14, "T_Reuse_DiffuseOpacity")
check("R-4: FILE datablocks are still reused", img14b is img14 and created14b is False)
gen.use_fake_user = False

print("=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} CHECKS FAILED:")
    for name in FAILS:
        print("  -", name)
    sys.exit(1)
print("✅ ALL CHECKS PASSED")
