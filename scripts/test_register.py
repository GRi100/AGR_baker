# Headless test for the addon lifecycle: AGR_tools/__init__.py, operators.py,
# properties.py, ui.py, log.py + the pure helpers of operators_share /
# operators_library / ui that must not need a network or a GPU.
# Run: blender --background --factory-startup --python scripts/test_register.py
import importlib
import os
import sys

import bpy

# repo root = parent of scripts/ — works from any checkout location
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import AGR_tools
import AGR_tools.log as agr_log
import AGR_tools.operators as agr_operators
import AGR_tools.operators_share as share
import AGR_tools.operators_library as library
import AGR_tools.operators_lights as lights
import AGR_tools.operators_sync as sync
import AGR_tools.operators_quick as quick
import AGR_tools.ui as agr_ui

FAILS = []


def check(name, cond, extra=""):
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def handler_names(handler_list):
    return [getattr(h, "__name__", "?") for h in handler_list]


def addon_handler_names(handler_list):
    """Only the handlers this addon installs (Blender ships its own)."""
    ours = {
        "_sync_handlers_on_load", "_bump_geo_version", "_resubscribe_on_load",
        "_library_on_load_pre", "_quick_on_load_pre", "_autofill_address_on_load",
        "_uv_overlay_depsgraph", "_uv_sync_handlers_on_load",
    }
    return [n for n in handler_names(handler_list) if n in ours]


ADDON_SCENE_PROPS = (
    "agr_texture_sets", "agr_baker_settings", "agr_texture_sets_index",
    "agr_sets_no_active_index", "agr_light_settings", "agr_uv_settings",
    "agr_geojson_folders", "agr_share_items", "agr_share_projects",
    "agr_link_autosync",
)
ADDON_WM_PROPS = (
    "agr_last_status", "agr_last_status_level", "agr_light_distance_show",
    "agr_sync_outliner_view", "agr_library_open", "agr_uv_grid_show",
)


def live_props():
    scene = [p for p in ADDON_SCENE_PROPS if hasattr(bpy.types.Scene, p)]
    wm = [p for p in ADDON_WM_PROPS if hasattr(bpy.types.WindowManager, p)]
    return scene, wm


# ===================================================================
print("\n=== TEST 1: register / unregister / register twice ===")

AGR_tools.register()
scene_props, wm_props = live_props()
check("1.1 register creates the addon properties",
      len(scene_props) >= 8 and len(wm_props) >= 5,
      f"scene={len(scene_props)} wm={len(wm_props)}")
check("1.2 operator registered", hasattr(bpy.types, "AGR_OT_replace_with_light"))

AGR_tools.unregister()
scene_props, wm_props = live_props()
check("1.3 unregister removes every Scene/WindowManager property",
      not scene_props and not wm_props, f"left: {scene_props} {wm_props}")
check("1.4 operator class gone", not hasattr(bpy.types, "AGR_OT_replace_with_light"))
check("1.5 no addon handlers left in load_post/load_pre/depsgraph_update_post/save_pre",
      not addon_handler_names(bpy.app.handlers.load_post)
      and not addon_handler_names(bpy.app.handlers.load_pre)
      and not addon_handler_names(bpy.app.handlers.depsgraph_update_post),
      f"post={addon_handler_names(bpy.app.handlers.load_post)} "
      f"pre={addon_handler_names(bpy.app.handlers.load_pre)}")

# second full cycle — "already registered" used to fire here
try:
    AGR_tools.register()
    second_ok, err = True, ""
except Exception as e:
    second_ok, err = False, str(e)
check("1.6 second register() succeeds", second_ok, err)

# ===================================================================
print("\n=== TEST 2: handlers are deduplicated by name, not identity ===")

# reloadOnSave scenario: modules are reloaded while their handlers stay in
# the lists, then register() runs again on the FRESH module objects
importlib.reload(lights)
importlib.reload(sync)
importlib.reload(library)
importlib.reload(quick)
lights.register()
sync.register()
library.register()
quick.register()

names_post = addon_handler_names(bpy.app.handlers.load_post)
names_pre = addon_handler_names(bpy.app.handlers.load_pre)
names_deps = addon_handler_names(bpy.app.handlers.depsgraph_update_post)
check("2.1 no duplicate load_post handlers", len(names_post) == len(set(names_post)), str(names_post))
check("2.2 no duplicate load_pre handlers", len(names_pre) == len(set(names_pre)), str(names_pre))
check("2.3 no duplicate depsgraph handlers", len(names_deps) == len(set(names_deps)), str(names_deps))
check("2.4 lights installed its depsgraph counter", "_bump_geo_version" in names_deps)
check("2.5 quick mode installed a load_pre guard", "_quick_on_load_pre" in names_pre)

# the reloaded modules must be able to remove the OLD modules' handlers
lights.unregister()
sync.unregister()
library.unregister()
quick.unregister()
check("2.6 reloaded modules removed every stale copy",
      "_sync_handlers_on_load" not in addon_handler_names(bpy.app.handlers.load_post)
      and "_library_on_load_pre" not in addon_handler_names(bpy.app.handlers.load_pre)
      and "_bump_geo_version" not in addon_handler_names(bpy.app.handlers.depsgraph_update_post))

# put the addon back into a consistent state for the rest of the run
lights.register()
sync.register()
library.register()
quick.register()

# ===================================================================
print("\n=== TEST 3: drop_stale_handlers helper ===")


def _fake_handler(_dummy):
    pass


bucket = [_fake_handler, _fake_handler, lambda x: None]
removed = agr_log.drop_stale_handlers(bucket, "_fake_handler")
check("3.1 removes every same-named handler", removed == 2 and len(bucket) == 1)
check("3.2 no-op for an unknown name",
      agr_log.drop_stale_handlers(bucket, "_nope") == 0)

# ===================================================================
print("\n=== TEST 4: a failing module rolls the chain back ===")

AGR_tools.unregister()
scene_props, wm_props = live_props()
check("4.0 clean slate before the rollback test", not scene_props and not wm_props)

original_register = agr_ui.register


def _boom():
    raise RuntimeError("simulated failure")


agr_ui.register = _boom
raised = False
try:
    AGR_tools.register()
except RuntimeError:
    raised = True
finally:
    agr_ui.register = original_register

check("4.1 the exception is propagated (Blender must see the error)", raised)
scene_props, wm_props = live_props()
check("4.2 rollback removed every property registered before the failure",
      not scene_props and not wm_props, f"left: {scene_props} {wm_props}")
check("4.3 rollback unregistered the operator classes",
      not hasattr(bpy.types, "AGR_OT_replace_with_light"))
check("4.4 rollback left no addon handlers",
      not addon_handler_names(bpy.app.handlers.load_post)
      and not addon_handler_names(bpy.app.handlers.load_pre))

try:
    AGR_tools.register()
    recovered, err = True, ""
except Exception as e:
    recovered, err = False, str(e)
check("4.5 register() after a rolled-back failure works", recovered, err)

# ===================================================================
print("\n=== TEST 5: unregister isolates a broken module ===")

failing = agr_operators._MODULES[0]
original = failing.unregister


def _bad_unregister():
    raise RuntimeError("simulated unregister failure")


failing.unregister = _bad_unregister
try:
    AGR_tools.unregister()
    survived = True
except Exception as e:
    survived = False
    print("   unregister raised:", e)
finally:
    failing.unregister = original

check("5.1 a broken unregister does not abort the chain", survived)
scene_props, wm_props = live_props()
# the broken module owns no Scene/WM props, so everything else must be gone
check("5.2 every other module still unregistered",
      not scene_props and not wm_props, f"left: {scene_props} {wm_props}")

# the sabotaged module is still registered — clean it up by hand, otherwise
# the next register() legitimately fails with "already registered"
try:
    failing.unregister()
except Exception:
    pass
AGR_tools.register()

# ===================================================================
print("\n=== TEST 6: Pillow flag lives in exactly one place ===")

before = agr_log.pillow_available()
agr_log.set_pillow_available(True)
check("6.1 installer flips the shared flag", agr_log.pillow_available() is True)
agr_log.set_pillow_available(False)
check("6.2 flag is readable as False too", agr_log.pillow_available() is False)
agr_log.set_pillow_available(before)
check("6.3 ui reads the shared flag, keeps no copy of its own",
      not hasattr(agr_ui, "PILLOW_AVAILABLE"))

# ===================================================================
print("\n=== TEST 7: per-process log file ===")

check("7.1 log path carries the pid", str(os.getpid()) in agr_log.LOG_PATH,
      agr_log.LOG_PATH)
agr_log.agr_report(None, 'INFO', "test_register: log line")
check("7.2 log file written", os.path.exists(agr_log.LOG_PATH))
file_handlers = [h for h in agr_log.logger.handlers
                 if h.__class__.__name__.endswith("FileHandler")]
check("7.3 no RotatingFileHandler (breaks on a second Blender under Windows)",
      all(h.__class__.__name__ == "FileHandler" for h in file_handlers),
      str([h.__class__.__name__ for h in file_handlers]))

# ===================================================================
print("\n=== TEST 8: Share pure helpers (no network) ===")

items = [
    {"sender": "Ann", "timestamp": "2026-01-01T10:00:00", "disk_path": "app:/AGR_Share/P/a.blend"},
    {"sender": "Bob", "timestamp": "2026-01-01T12:00:00", "url": "app:/AGR_Share/P/b.blend"},
    {"sender": "Ann", "timestamp": "2026-01-01T14:00:00", "disk_path": "app:/AGR_Share/P/c.blend"},
]

new, latest = share._diff_new_items(items, None, "Ann")
check("8.1 first run notifies nothing and records the baseline",
      new == [] and latest == "2026-01-01T14:00:00", f"{len(new)} {latest}")

new, latest = share._diff_new_items(items, "2026-01-01T10:00:00", "Ann")
check("8.2 own shares are skipped, foreign ones are new",
      [i["sender"] for i in new] == ["Bob"], str([i["sender"] for i in new]))

new, _ = share._diff_new_items(items, "2026-01-01T23:00:00", "Ann")
check("8.3 nothing new past the baseline", new == [])

check("8.4 _entry_path reads disk_path",
      share._entry_path(items[0]) == "app:/AGR_Share/P/a.blend")
check("8.5 _entry_path falls back to the legacy url key",
      share._entry_path(items[1]) == "app:/AGR_Share/P/b.blend")
check("8.6 _entry_path on a record without a path", share._entry_path({}) == "")

# the legacy-keyed entry must be removable — this is the GLUE-12 regression
legacy_path = share._entry_path(items[1])
kept = [i for i in items if share._entry_path(i) != legacy_path]
check("8.7 legacy entry disappears when filtered through _entry_path",
      len(kept) == 2 and all(share._entry_path(i) != legacy_path for i in kept))

fresh = {"sender": "X", "timestamp": share._utcnow_naive().isoformat()}
old = {"sender": "X", "timestamp": "2020-01-01T00:00:00"}
pruned = share._prune_old_items([fresh, old])
check("8.8 _prune_old_items drops items past the cutoff", pruned == [fresh])
check("8.9 _prune_old_items keeps records with a broken timestamp",
      share._prune_old_items([{"sender": "X"}]) == [{"sender": "X"}])

scene = bpy.context.scene
share._cached_items = list(items)
for entry in share._cached_items:
    entry["project"] = "P"
scene.agr_share_active_project = "P"
share._apply_project_filter(scene)
check("8.10 _apply_project_filter fills the UI list from the cache",
      len(scene.agr_share_items) == 3, str(len(scene.agr_share_items)))
check("8.11 legacy `url` entries get their path into the row",
      scene.agr_share_items[1].url == "app:/AGR_Share/P/b.blend")
scene.agr_share_active_project = "Other"
share._apply_project_filter(scene)
check("8.12 filter honours the active project", len(scene.agr_share_items) == 0)
share._cached_items = []

check("8.13 watcher refuses to start in background Blender",
      bpy.app.background and share._ShareWatcher._thread is None)

check("8.14 every threaded Share operator implements cancel()",
      all(callable(getattr(cls, "cancel", None)) for cls in (
          share.AGR_OT_RefreshShareList, share.AGR_OT_ShareClipboard,
          share.AGR_OT_ReceiveShared, share.AGR_OT_DeleteShared,
          share.AGR_OT_CreateShareProject, share.AGR_OT_DeleteShareProject)))

check("8.15 Quick Mode implements cancel()",
      callable(getattr(quick.AGR_OT_QuickMode, "cancel", None)))

# ---- project listing must page, and must refuse to guess ----------------
import json as _json
import urllib.error as _urlerror


class _FakeResp:
    def __init__(self, payload):
        self._payload = _json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload


def _install_fake_listing(total_folders, report_total=True, fail=False):
    """urlopen stub serving `total_folders` dirs in pages of _LIST_PAGE."""
    state = {"requests": 0}

    def fake_urlopen(req, timeout=None):
        state["requests"] += 1
        if fail:
            raise _urlerror.HTTPError(req.full_url, 503, "busy", None, None)
        url = req.full_url
        offset = int(url.split("offset=")[1].split("&")[0])
        page = [{"name": f"P{i:04d}", "type": "dir"}
                for i in range(offset, min(offset + share._LIST_PAGE, total_folders))]
        embedded = {"items": page}
        if report_total:
            embedded["total"] = total_folders
        return _FakeResp({"_embedded": embedded})

    share.urllib.request.urlopen = fake_urlopen
    return state


real_urlopen = share.urllib.request.urlopen
try:
    state = _install_fake_listing(250)
    listed = share._yadisk_list_folders("token")
    check("8.16 a 250-project folder is listed in full (was truncated at 100)",
          listed is not None and len(listed) == 250, str(len(listed or [])))
    check("8.17 three pages were fetched", state["requests"] == 3, str(state["requests"]))

    _install_fake_listing(37)
    listed = share._yadisk_list_folders("token")
    check("8.18 a single short page ends the listing",
          listed is not None and len(listed) == 37, str(len(listed or [])))

    _install_fake_listing(250, report_total=False)
    listed = share._yadisk_list_folders("token")
    check("8.19 without a reported total, a full page keeps paging",
          listed is not None and len(listed) == 250, str(len(listed or [])))

    _install_fake_listing(0, fail=True)
    check("8.20 a transient HTTP error is still 'no verdict' (None)",
          share._yadisk_list_folders("token") is None)

    _install_fake_listing(share._LIST_PAGE * (share._LIST_MAX_PAGES + 2),
                          report_total=False)
    check("8.21 an oversized listing is reported as incomplete, not as truth",
          share._yadisk_list_folders("token") is None)
finally:
    share.urllib.request.urlopen = real_urlopen

# ===================================================================
print("\n=== TEST 9: atlas forecast is cheap in draw() ===")

fp = (2048, (1024, 1024, 512), 0, 0)
info = agr_ui.compute_multi_atlas_forecast(fp, [1024, 1024, 512], 0)
check("9.1 three sets fit one 2048 atlas", info == (1, 3, 0, 0), str(info))

fp_big = (512, (1024,), 0, 0)
info_big = agr_ui.compute_multi_atlas_forecast(fp_big, [1024], 0)
check("9.2 a set larger than the atlas is reported, packing skipped",
      info_big == (0, 1, 0, 1), str(info_big))

fp_many = (1024, tuple([1024] * 4), 1, 0)
info_many = agr_ui.compute_multi_atlas_forecast(fp_many, [1024] * 4, 1)
check("9.3 four full-size sets need four atlases, missing count kept",
      info_many == (4, 4, 1, 0), str(info_many))

# the fingerprint must not touch the packer and must be stable per redraw
sets = scene.agr_texture_sets
sets.clear()
for i, res in enumerate((1024, 512)):
    ts = sets.add()
    ts.name = f"S_mat{i}"
    ts.material_name = f"mat{i}"
    ts.resolution = res

mesh = bpy.data.meshes.new("fc")
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
obj = bpy.data.objects.new("ForecastObj", mesh)
scene.collection.objects.link(obj)
for i in range(2):
    mat = bpy.data.materials.new(f"mat{i}")
    obj.data.materials.append(mat)
bpy.context.view_layer.objects.active = obj

inputs = agr_ui._forecast_inputs(bpy.context)
check("9.4 forecast inputs resolve both materials through the map",
      inputs is not None and sorted(inputs[1]) == [512, 1024] and inputs[2] == 0,
      str(inputs))
check("9.5 fingerprint is stable across redraws",
      agr_ui._forecast_inputs(bpy.context)[0] == inputs[0])

mapping = agr_ui._sets_by_material(scene)
check("9.6 material→set map is built once per generation",
      agr_ui._sets_by_material(scene) is mapping)
agr_ui.bump_sets_generation()
check("9.7 bump_sets_generation rebuilds the map",
      agr_ui._sets_by_material(scene) is not mapping)

# _multi_atlas_forecast must NEVER run the packing inline in draw()
agr_ui.bump_sets_generation()
calls = {"n": 0}
real_packer = agr_ui.calculate_multi_atlas_packing


def _counting_packer(sets_, size):
    calls["n"] += 1
    return real_packer(sets_, size)


agr_ui.calculate_multi_atlas_packing = _counting_packer
try:
    for _ in range(5):
        agr_ui._multi_atlas_forecast(bpy.context)
    check("9.8 five redraws run the packer zero times", calls["n"] == 0, str(calls["n"]))

    fp_live = agr_ui._forecast_inputs(bpy.context)[0]
    agr_ui._ATLAS_FORECAST_CACHE[fp_live] = agr_ui.compute_multi_atlas_forecast(
        fp_live, [1024, 512], 0)
    check("9.9 once the deferred pass filled the cache, draw() shows it",
          agr_ui._multi_atlas_forecast(bpy.context) == (1, 2, 0, 0),
          str(agr_ui._multi_atlas_forecast(bpy.context)))
finally:
    agr_ui.calculate_multi_atlas_packing = real_packer
    agr_ui._ATLAS_FORECAST_PENDING.clear()

# ===================================================================
print("\n=== TEST 10: library previews are cached across opens ===")

import tempfile

tmpdir = tempfile.mkdtemp(prefix="agr_lib_")
img = bpy.data.images.new("src", 64, 64)
src_path = os.path.join(tmpdir, "T_mat0_DiffuseOpacity.png")
img.filepath_raw = src_path
img.file_format = 'PNG'
img.save()
bpy.data.images.remove(img)

sets[0].folder_path = tmpdir
check("10.1 preview path finds the DiffuseOpacity file",
      library._preview_path(sets[0]) == src_path, str(library._preview_path(sets[0])))

n_before = len(bpy.data.images)
first_img = library._preview_image(src_path)
check("10.2 preview is downscaled to 128px",
      tuple(first_img.size) == (128, 128), str(tuple(first_img.size)))
second_img = library._preview_image(src_path)
check("10.3 second open reuses the datablock (no re-decode)",
      second_img is first_img and len(bpy.data.images) == n_before + 1,
      f"{len(bpy.data.images)} vs {n_before + 1}")
library.drop_preview_cache()
check("10.4 drop_preview_cache releases the datablocks",
      len(bpy.data.images) == n_before and not library._preview_cache)

sets.clear()
bpy.data.objects.remove(obj, do_unlink=True)

# ===================================================================
print("\n=== TEST 11: sys.path / msgbus owner hygiene ===")

check("11.1 msgbus owner is a stable string across reloads",
      isinstance(sync._MSGBUS_OWNER, str))

check("11.2 bl_info version matches the documented 2.8 line",
      AGR_tools.bl_info["version"][:2] == (2, 8), str(AGR_tools.bl_info["version"]))

# ===================================================================
print("\n=== TEST 12: library previews never carry a texture filepath ===")

# Regression: the session preview cache used to keep LOADED images whose
# filepath pointed at the real 4K texture and which img.scale() left dirty.
# One "Image → Save All Images" (or the Save button of "Save changes before
# closing?") then rewrote every browsed set's PNG as a 128x128 file.
lib_dir = tempfile.mkdtemp(prefix="agr_lib2_")
big_path = os.path.join(lib_dir, "T_matA_DiffuseOpacity.png")
seed = bpy.data.images.new("seed", 512, 512, alpha=True)
seed.filepath_raw = big_path
seed.file_format = 'PNG'
seed.save()
bpy.data.images.remove(seed)

from AGR_tools.core.texture_sets import read_png_ihdr

ihdr_before = read_png_ihdr(big_path)
preview = library._preview_image(big_path)
check("12.1 preview built at 128px", tuple(preview.size) == (128, 128),
      str(tuple(preview.size)))
check("12.2 preview keeps no filepath of the source texture",
      preview.filepath == "" and preview.filepath_raw == "",
      f"{preview.filepath!r} {preview.filepath_raw!r}")
check("12.3 preview is not dirty (Save All Images skips it)",
      not preview.is_dirty)
check("12.4 no cached image points at a real texture file",
      not any(i.is_dirty and i.filepath for i in bpy.data.images),
      str([i.name for i in bpy.data.images if i.is_dirty and i.filepath]))

try:
    bpy.ops.image.save_all_modified()
except RuntimeError:
    pass  # poll() fails when nothing is savable — exactly the wanted state
check("12.5 the source PNG on disk is untouched",
      read_png_ihdr(big_path) == ihdr_before,
      f"{read_png_ihdr(big_path)} vs {ihdr_before}")
check("12.6 the cache remembers which set the datablock belongs to",
      preview.get('agr_preview_src') == big_path)

# a datablock whose name was reused by a different set must not be served
preview['agr_preview_src'] = "some/other/set.png"
other = library._preview_image(big_path)
check("12.7 a name collision is detected and the preview rebuilt",
      other is not preview or other.get('agr_preview_src') == big_path)
library.drop_preview_cache()
check("12.8 drop_preview_cache still releases everything",
      not library._preview_cache)

# ===================================================================
print("\n=== TEST 13: Share toast bookkeeping moves the baseline ===")

check("13.1 _show_windows_toast returns a bool, never None",
      isinstance(share._show_windows_toast("t", "b"), bool))

watch_items = [
    {"sender": "Bob", "timestamp": "2026-02-01T10:00:00",
     "disk_path": "app:/AGR_Share/P/b.blend", "description": "one"},
    {"sender": "Bob", "timestamp": "2026-02-01T11:00:00",
     "disk_path": "app:/AGR_Share/P/c.blend", "description": "two"},
]
real_read, real_toast, real_persist = share._read_items, share._show_windows_toast, share._persist_last_seen
persisted = []
try:
    share._read_items = lambda token: watch_items
    share._persist_last_seen = lambda ts: persisted.append(ts)
    cfg = {"yandex_token": "T", "sender_name": "Ann", "last_seen_ts": None}

    share._show_windows_toast = lambda *a: True
    check("13.2 the first turn records the baseline silently",
          share._ShareWatcher._poll_once(cfg) is True
          and persisted == ["2026-02-01T11:00:00"], str(persisted))

    persisted.clear()
    cfg["last_seen_ts"] = "2026-02-01T09:00:00"
    share._ShareWatcher._poll_once(cfg)
    # THE regression: _show_windows_toast returned None, so `shown` stayed 0,
    # the baseline never moved and every toast replayed on each 30 s poll
    check("13.3 shown toasts advance last_seen",
          persisted == ["2026-02-01T11:00:00"], str(persisted))

    persisted.clear()
    share._show_windows_toast = lambda *a: False
    share._ShareWatcher._poll_once(cfg)
    check("13.4 a failed toast keeps the baseline where it was (nothing lost)",
          persisted == [], str(persisted))

    check("13.5 the watcher stops when notifications are switched off",
          share._ShareWatcher._poll_once(
              {"yandex_token": "T", "notifications_enabled": False}) is False)
    check("13.6 the watcher stops without a token",
          share._ShareWatcher._poll_once({"yandex_token": ""}) is False)
finally:
    share._read_items, share._show_windows_toast = real_read, real_toast
    share._persist_last_seen = real_persist

# ===================================================================
print("\n=== TEST 14: config writes survive a locked target file ===")

cfg_dir = tempfile.mkdtemp(prefix="agr_cfg_")
cfg_path = os.path.join(cfg_dir, ".agr_baker_share.json")
real_cfg_path = share._CONFIG_PATH
try:
    share._CONFIG_PATH = cfg_path
    share._config_cache = None
    share._save_config({"yandex_token": "first"})
    check("14.1 plain write lands", os.path.exists(cfg_path))

    holder = open(cfg_path, "r", encoding="utf-8")   # a second Blender reading
    try:
        share._save_config({"yandex_token": "second"})
        wrote, err = True, ""
    except Exception as e:
        wrote, err = False, f"{type(e).__name__}: {e}"
    finally:
        holder.close()
    check("14.2 os.replace onto an open handle no longer raises", wrote, err)

    import json as _json_check
    with open(cfg_path, encoding="utf-8") as f:
        written = _json_check.load(f)
    check("14.3 the new token really reached the file",
          written.get("yandex_token") == "second", str(written))
    leftovers = [n for n in os.listdir(cfg_dir) if ".tmp" in n]
    check("14.4 no .tmp<pid> file is left in the home directory",
          not leftovers, str(leftovers))
finally:
    share._CONFIG_PATH = real_cfg_path
    share._config_cache = None

# ===================================================================
print("\n=== TEST 15: a module that dies MID-register is rolled back too ===")

AGR_tools.unregister()
real_pointer_property = lights.PointerProperty


def _moved_api(*_a, **_k):
    # fires AFTER lights.register() has registered its classes — the real
    # "API moved" shape, and the one the old rollback could not undo
    raise RuntimeError("simulated: API moved")


lights.PointerProperty = _moved_api
try:
    AGR_tools.register()
    raised_mid = False
except Exception:
    raised_mid = True
finally:
    lights.PointerProperty = real_pointer_property

check("15.1 the failure is propagated", raised_mid)
check("15.2 the FAILING module's own classes were unregistered too",
      not hasattr(bpy.types, "AGR_OT_replace_with_light")
      and not hasattr(bpy.types, "AGR_LightReplacerSettings"))
try:
    AGR_tools.register()
    reenabled, err = True, ""
except Exception as e:
    reenabled, err = False, f"{type(e).__name__}: {str(e)[:120]}"
check("15.3 re-enabling the addon works (was 'already registered')", reenabled, err)

check("15.4 unregister_classes skips classes Blender no longer holds",
      agr_log.unregister_classes(()) is None)

# ===================================================================
print("\n=== TEST 16: every rebuild of the sets list invalidates the caches ===")

import AGR_tools.core.texture_sets as core_sets

check("16.1 ui subscribed its invalidator to the core hook",
      any(getattr(cb, "__name__", "") == "bump_sets_generation"
          for cb in core_sets.LIST_REBUILT_CALLBACKS),
      str([getattr(cb, "__name__", "?") for cb in core_sets.LIST_REBUILT_CALLBACKS]))

fired = {"n": 0}
core_sets.LIST_REBUILT_CALLBACKS.append(lambda: fired.__setitem__("n", fired["n"] + 1))
try:
    core_sets.refresh_texture_sets_list(bpy.context)
    check("16.2 the core helper fires the hook (the 11 direct callers are covered)",
          fired["n"] == 1, str(fired["n"]))
finally:
    core_sets.LIST_REBUILT_CALLBACKS.pop()

sets = scene.agr_texture_sets
sets.clear()
for i in range(250):
    ts = sets.add()
    ts.name = f"S_M{i}"
    ts.material_name = f"M{i}"
    ts.resolution = 1024
agr_ui.bump_sets_generation()
by_mat = agr_ui._sets_by_material(scene)
check("16.3 the map stores plain resolutions, not PropertyGroup references",
      by_mat.get("M10") == 1024 and isinstance(by_mat.get("M10"), int),
      repr(by_mat.get("M10")))

# same-length rebuild — the exact shape that used to leave freed IDProperty
# memory behind the cached references
sets.clear()
for i in range(250):
    ts = sets.add()
    ts.name = f"S_M{i}"
    ts.material_name = f"M{i}"
    ts.resolution = 2048 if i == 10 else 512
for cb in list(core_sets.LIST_REBUILT_CALLBACKS):
    cb()
check("16.4 a same-length rebuild is seen (the key used to collide)",
      agr_ui._sets_by_material(scene).get("M10") == 2048,
      str(agr_ui._sets_by_material(scene).get("M10")))
sets.clear()
agr_ui.bump_sets_generation()

# ===================================================================
print("\n=== TEST 17: the UDIM sibling lookup is not run per redraw ===")

real_finder = agr_ui.find_sibling_udim_carrier
finder_calls = {"n": 0}


def _counting_finder(obj):
    finder_calls["n"] += 1
    return real_finder(obj)


sib_mesh = bpy.data.meshes.new("sib")
sib_mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
sib_obj = bpy.data.objects.new("SM_Addr_Ground", sib_mesh)
scene.collection.objects.link(sib_obj)

agr_ui.find_sibling_udim_carrier = _counting_finder
agr_ui._SIBLING_CACHE["key"] = None
try:
    for _ in range(20):
        agr_ui._sibling_udim_carrier(sib_obj)
    check("17.1 twenty redraws scan bpy.data.objects once", finder_calls["n"] == 1,
          str(finder_calls["n"]))
    extra_obj = bpy.data.objects.new("Filler", None)
    scene.collection.objects.link(extra_obj)
    agr_ui._sibling_udim_carrier(sib_obj)
    check("17.2 a new object invalidates the cached answer", finder_calls["n"] == 2,
          str(finder_calls["n"]))
    bpy.data.objects.remove(extra_obj, do_unlink=True)
finally:
    agr_ui.find_sibling_udim_carrier = real_finder
bpy.data.objects.remove(sib_obj, do_unlink=True)

# ===================================================================
print("\n=== TEST 18: an unreachable log file degrades to console-only ===")

real_log_path = agr_log.LOG_PATH
real_handlers = list(agr_log.logger.handlers)
try:
    agr_log.LOG_PATH = os.path.join(tempfile.mkdtemp(), "no_such_dir",
                                    ".agr_tools.test.log")
    agr_log.logger.handlers.clear()
    try:
        agr_log.register()
        agr_log.agr_report(None, 'INFO', "unreachable log file test")
        survived, err = True, ""
    except Exception as e:
        survived, err = False, f"{type(e).__name__}: {e}"
    # delay=True moved the open into FileHandler.emit(), which has no
    # try/except — the first logger.info() inside register() then took the
    # WHOLE addon down instead of falling back to the console
    check("18.1 register() and agr_report() survive an unreachable home",
          survived, err)
    check("18.2 no file handler was installed for the broken path",
          not [h for h in agr_log.logger.handlers
               if h.__class__.__name__.endswith("FileHandler")],
          str([h.__class__.__name__ for h in agr_log.logger.handlers]))
finally:
    agr_log.LOG_PATH = real_log_path
    agr_log.logger.handlers.clear()
    agr_log.logger.handlers.extend(real_handlers)

# ===================================================================
print("\n=== TEST 19: msgbus owner identity survives a module reload ===")

check("19.1 the owner string is interned (msgbus compares by identity)",
      sys.intern(sync._MSGBUS_OWNER) is sync._MSGBUS_OWNER, sync._MSGBUS_OWNER)
owner_before = sync._MSGBUS_OWNER
importlib.reload(sync)
check("19.2 the reloaded module yields the SAME owner object",
      sync._MSGBUS_OWNER is owner_before)
sync.register()   # the reload dropped the live module's registration state


# ===================================================================
print("\n" + "=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} CHECK(S) FAILED:")
    for name in FAILS:
        print("   -", name)
else:
    print("✅ ALL CHECKS PASSED")
print("=" * 60)
