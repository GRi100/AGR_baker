"""
AGR Share — shared clipboard over the internet via Yandex.Disk.

Team members can copy objects (Ctrl+C) and share them instantly.
Colleagues see all shared items in the addon panel without manual link exchange.
Files and index are stored on Yandex.Disk.

To disable this feature: comment out the operators_share import in operators.py.
"""

import bpy
import os
import sys
import json
import subprocess
import threading
import tempfile
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from bpy.types import Operator, Panel, PropertyGroup, UIList
from bpy.props import (
    StringProperty,
    IntProperty,
    BoolProperty,
    CollectionProperty,
)

from .operators_bake import sanitize_material_name


_ALL_PROJECTS = "All"

_cached_items = []


def _utcnow_naive():
    """Naive UTC now. Index timestamps must stay tz-naive so they compare
    cleanly with entries written by older builds (datetime.utcnow is
    deprecated since Python 3.12, hence this replacement)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _apply_project_filter(scene):
    active = scene.agr_share_active_project
    show_all = (active == _ALL_PROJECTS)
    scene.agr_share_items.clear()
    for entry in _cached_items:
        if not show_all and entry.get("project", "") != active:
            continue
        item = scene.agr_share_items.add()
        item.sender = entry.get("sender", "?")
        item.timestamp = entry.get("timestamp", "")
        item.url = entry.get("disk_path", entry.get("url", ""))
        item.description = entry.get("description", "")
        item.objects_count = entry.get("objects_count", 0)
        item.project = entry.get("project", "")
    count = len(scene.agr_share_items)
    label = _ALL_PROJECTS if show_all else active
    scene.agr_share_status = f"{count} item(s) in '{label}'"


def _on_active_project_changed(self, context):
    """Called when user changes project selection — re-filter locally."""
    _apply_project_filter(context.scene)


# ---------------------------------------------------------------------------
# Config helpers — stored in ~/.agr_baker_share.json (never in .blend)
# ---------------------------------------------------------------------------

_CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".agr_baker_share.json")

_config_cache = None


def _load_config() -> dict:
    """Read config from disk. Cached across UI redraws; invalidated by _save_config."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            _config_cache = json.load(f)
    except FileNotFoundError:
        _config_cache = {}
    except (OSError, ValueError) as e:
        print(f"⚠️ AGR Share: failed to read config: {e}")
        _config_cache = {}
    return _config_cache


def _save_config(data: dict):
    global _config_cache
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    _config_cache = dict(data)


_config_lock = threading.Lock()


def _update_config(**kwargs):
    """Atomic read-modify-write on the config file.

    Watcher thread persists `last_seen_ts` while the main thread persists
    `yandex_token`, `sender_name`, etc. Without a merge step they would
    clobber each other. The lock additionally protects against two writers
    racing the same file.
    """
    with _config_lock:
        cfg = dict(_load_config())
        cfg.update(kwargs)
        _save_config(cfg)


# ---------------------------------------------------------------------------
# Network helpers (called from background threads — no bpy access!)
# ---------------------------------------------------------------------------

_YADISK_API = "https://cloud-api.yandex.net/v1/disk/resources"
_YADISK_FOLDER = "app:/AGR_Share"
_YADISK_INDEX = "app:/AGR_Share/agr_clipboard.json"
_USER_AGENT = "AGR-Baker-Addon"
_TIMEOUT = 60


# Datablock categories shown in the Share dialog summary.
# Each tuple: (data_from attribute, display label, Blender icon).
# Order here = display order in the dialog.
_CLIPBOARD_CATEGORIES = (
    ("objects", "Objects", "OBJECT_DATA"),
    ("collections", "Collections", "OUTLINER_COLLECTION"),
    ("meshes", "Meshes", "MESH_DATA"),
    ("curves", "Curves", "CURVE_DATA"),
    ("armatures", "Armatures", "ARMATURE_DATA"),
    ("lights", "Lights", "LIGHT_DATA"),
    ("cameras", "Cameras", "CAMERA_DATA"),
    ("materials", "Materials", "MATERIAL"),
    ("images", "Images", "IMAGE_DATA"),
    ("textures", "Textures", "TEXTURE"),
    ("node_groups", "Node Groups", "NODETREE"),
)


def _get_clipboard_path():
    """Return path to Blender clipboard .blend file, or None.

    Blender writes copybuffer.blend to the *base* temp directory
    (parent of the per-session bpy.app.tempdir), not the session dir.
    """
    search_dirs = [
        os.path.dirname(bpy.app.tempdir.rstrip(os.sep)),  # base temp (parent)
        bpy.app.tempdir,                                    # session temp
        tempfile.gettempdir(),                               # system temp
    ]
    for d in search_dirs:
        for name in ("copybuffer.blend", "clipboard.blend"):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
    return None


def _yadisk_mkdir(ya_token: str, path: str):
    """Create a folder on Yandex.Disk. Idempotent — treats 409 as success."""
    encoded = urllib.parse.quote(path)
    req = urllib.request.Request(f"{_YADISK_API}?path={encoded}", method="PUT")
    req.add_header("Authorization", f"OAuth {ya_token}")
    try:
        urllib.request.urlopen(req, timeout=_TIMEOUT)
    except urllib.error.HTTPError as e:
        if e.code != 409:
            raise


def _yadisk_delete(ya_token: str, path: str):
    """Delete a resource on Yandex.Disk permanently. Raises on HTTP/URL errors."""
    encoded = urllib.parse.quote(path)
    req = urllib.request.Request(
        f"{_YADISK_API}?path={encoded}&permanently=true",
        method="DELETE",
    )
    req.add_header("Authorization", f"OAuth {ya_token}")
    urllib.request.urlopen(req, timeout=_TIMEOUT)


def _upload_file(file_bytes: bytes, ya_token: str, filename: str,
                 project: str) -> str:
    """Upload file to Yandex.Disk AGR_Share/project/ folder. Returns disk path."""
    _yadisk_mkdir(ya_token, _YADISK_FOLDER)
    _yadisk_mkdir(ya_token, f"{_YADISK_FOLDER}/{project}")

    disk_path = f"{_YADISK_FOLDER}/{project}/{filename}"
    encoded_path = urllib.parse.quote(disk_path)
    req = urllib.request.Request(
        f"{_YADISK_API}/upload?path={encoded_path}&overwrite=true",
    )
    req.add_header("Authorization", f"OAuth {ya_token}")
    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    upload_url = json.loads(resp.read().decode("utf-8"))["href"]

    put_req = urllib.request.Request(upload_url, data=file_bytes, method="PUT")
    put_req.add_header("Content-Type", "application/octet-stream")
    urllib.request.urlopen(put_req, timeout=_TIMEOUT)

    return disk_path


def _read_index(ya_token: str) -> dict:
    """Read agr_clipboard.json from Yandex.Disk. Returns dict with 'items' and 'projects'."""
    encoded = urllib.parse.quote(_YADISK_INDEX)
    req = urllib.request.Request(f"{_YADISK_API}/download?path={encoded}")
    req.add_header("Authorization", f"OAuth {ya_token}")
    try:
        resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
        dl_url = json.loads(resp.read().decode("utf-8"))["href"]
        dl_req = urllib.request.Request(dl_url)
        with urllib.request.urlopen(dl_req, timeout=_TIMEOUT) as dl_resp:
            data = json.loads(dl_resp.read().decode("utf-8"))
        if "projects" not in data:
            data["projects"] = []
        if "items" not in data:
            data["items"] = []
        return data
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"items": [], "projects": []}
        raise


def _write_index(ya_token: str, index_data: dict):
    """Write agr_clipboard.json to Yandex.Disk (overwrite)."""
    _yadisk_mkdir(ya_token, _YADISK_FOLDER)
    encoded = urllib.parse.quote(_YADISK_INDEX)
    req = urllib.request.Request(f"{_YADISK_API}/upload?path={encoded}&overwrite=true")
    req.add_header("Authorization", f"OAuth {ya_token}")
    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    upload_url = json.loads(resp.read().decode("utf-8"))["href"]

    payload = json.dumps(index_data, ensure_ascii=False, indent=2).encode("utf-8")
    put_req = urllib.request.Request(upload_url, data=payload, method="PUT")
    put_req.add_header("Content-Type", "application/json")
    urllib.request.urlopen(put_req, timeout=_TIMEOUT)


def _yadisk_list_folders(ya_token: str):
    """List subfolder names inside AGR_Share on Yandex.Disk.
    Returns None when the listing FAILED (e.g. 429/503) — callers must not
    treat a transient error as "no projects" or they would wipe the shared
    index for the whole team."""
    encoded = urllib.parse.quote(_YADISK_FOLDER)
    req = urllib.request.Request(
        f"{_YADISK_API}?path={encoded}&fields=_embedded.items.name,_embedded.items.type&limit=100",
    )
    req.add_header("Authorization", f"OAuth {ya_token}")
    try:
        resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
        data = json.loads(resp.read().decode("utf-8"))
        items = data.get("_embedded", {}).get("items", [])
        return [i["name"] for i in items if i.get("type") == "dir"]
    except urllib.error.HTTPError:
        return None


def _read_items(ya_token: str) -> list:
    """Read items list from index on Yandex.Disk."""
    return _read_index(ya_token).get("items", [])


def _write_items(ya_token: str, items: list):
    """Write items to index, preserving projects list."""
    try:
        index_data = _read_index(ya_token)
    except Exception:
        index_data = {"projects": []}
    index_data["items"] = items
    _write_index(ya_token, index_data)


def _download_file(disk_path: str, local_path: str, ya_token: str):
    """Download a file from Yandex.Disk to local_path.

    Two-step process:
    1. GET download URL from Yandex.Disk API
    2. GET file bytes from that URL
    """
    encoded_path = urllib.parse.quote(disk_path)
    req = urllib.request.Request(
        f"{_YADISK_API}/download?path={encoded_path}",
    )
    req.add_header("Authorization", f"OAuth {ya_token}")
    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    download_url = json.loads(resp.read().decode("utf-8"))["href"]

    dl_req = urllib.request.Request(download_url)
    with urllib.request.urlopen(dl_req, timeout=_TIMEOUT) as dl_resp:
        with open(local_path, "wb") as f:
            f.write(dl_resp.read())


def _prune_old_items(items: list, max_days: int = 30) -> list:
    """Remove items older than max_days."""
    cutoff = _utcnow_naive() - timedelta(days=max_days)
    result = []
    for item in items:
        try:
            ts = datetime.fromisoformat(item.get("timestamp", ""))
            if ts >= cutoff:
                result.append(item)
        except (ValueError, TypeError):
            result.append(item)
    return result


def _format_relative_time(iso_str: str) -> str:
    """Convert ISO timestamp to a short relative string like '5m ago'."""
    try:
        ts = datetime.fromisoformat(iso_str)
        delta = _utcnow_naive() - ts
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return f"{seconds}s"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}m"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h"
        days = hours // 24
        return f"{days}d"
    except (ValueError, TypeError):
        return "?"


# ---------------------------------------------------------------------------
# Notification helpers — Windows toast via PowerShell + WinRT (no pip deps)
# ---------------------------------------------------------------------------

def _show_windows_toast(title: str, body: str):
    """Fire a Windows 10/11 toast via PowerShell + WinRT.

    No-op on non-Windows. Uses CREATE_NO_WINDOW so no console flashes.
    Title/body are wrapped in single-quoted PowerShell strings; ' is escaped
    by doubling (standard PS rule). No variable interpolation happens inside
    single-quoted strings, so this is safe against PS injection.
    """
    if sys.platform != "win32":
        return

    title_esc = title.replace("'", "''")
    body_esc = body.replace("'", "''")

    ps_script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]|Out-Null;"
        "[Windows.UI.Notifications.ToastNotification,Windows.UI.Notifications,ContentType=WindowsRuntime]|Out-Null;"
        "[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime]|Out-Null;"
        "$tpl=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$texts=$tpl.GetElementsByTagName('text');"
        f"$texts.Item(0).AppendChild($tpl.CreateTextNode('{title_esc}'))|Out-Null;"
        f"$texts.Item(1).AppendChild($tpl.CreateTextNode('{body_esc}'))|Out-Null;"
        "$toast=[Windows.UI.Notifications.ToastNotification]::new($tpl);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('AGR Tools').Show($toast)"
    )

    try:
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-WindowStyle", "Hidden", "-Command", ps_script],
            creationflags=0x08000000,  # CREATE_NO_WINDOW
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"⚠️ AGR Share toast: {e}")


def _diff_new_items(items, last_seen_iso, my_sender):
    """Return (new_items, latest_ts_iso) given the index and baseline.

    `latest_ts_iso` is the max timestamp across all items (used to advance
    the baseline). `new_items` is empty on first run (`last_seen_iso=None`)
    — that prevents 30+ toasts on initial enable.

    Items with no/invalid timestamp are skipped from both new and latest.
    """
    def parse(iso_str):
        try:
            return datetime.fromisoformat(iso_str)
        except (ValueError, TypeError):
            return None

    parsed = []
    for it in items or []:
        ts = parse(it.get("timestamp", ""))
        if ts is not None:
            parsed.append((ts, it))

    if not parsed:
        return [], last_seen_iso or ""

    latest_dt = max(ts for ts, _ in parsed)
    latest_iso = latest_dt.isoformat()

    last_seen_dt = parse(last_seen_iso) if last_seen_iso else None
    if last_seen_dt is None:
        return [], latest_iso

    new = []
    for ts, it in parsed:
        if ts <= last_seen_dt:
            continue
        # Skip own shares unless sender_name is unset (then we can't tell).
        if my_sender and it.get("sender", "") == my_sender:
            continue
        new.append(it)
    return new, latest_iso


def _persist_last_seen(ts_iso: str):
    """Save last_seen_ts via the merge-aware updater."""
    try:
        _update_config(last_seen_ts=ts_iso)
    except OSError as e:
        print(f"⚠️ AGR Share: failed to persist last_seen_ts: {e}")


def _on_new_items_main_thread(items):
    """Main-thread callback scheduled via bpy.app.timers.register.

    Refreshes module cache, re-applies the project filter into every scene
    that owns share props, and forces a redraw of the AGR panel.
    Returns None so the timer is one-shot.
    """
    global _cached_items
    _cached_items = list(items)
    for scene in bpy.data.scenes:
        if hasattr(scene, "agr_share_items"):
            try:
                _apply_project_filter(scene)
            except Exception as e:
                print(f"⚠️ AGR Share refresh: {e}")
    wm = bpy.context.window_manager
    if wm:
        for window in wm.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    for region in area.regions:
                        if region.type == 'UI':
                            region.tag_redraw()
    return None


# ---------------------------------------------------------------------------
# Background watcher — polls the index and fires Windows toast notifications
# ---------------------------------------------------------------------------

class _ShareWatcher:
    """Singleton background thread that polls the shared index.

    Lifecycle: started in register() (if a token is configured and
    notifications are enabled), stopped in unregister() and restarted on
    Settings save (token / interval / enabled may have changed).

    The thread NEVER touches bpy.* directly — UI updates are bridged via
    bpy.app.timers.register() which is one of the few thread-safe bpy APIs.
    """

    _thread = None
    _stop_event = None
    _lock = threading.Lock()

    @classmethod
    def start(cls):
        with cls._lock:
            if cls._thread is not None and cls._thread.is_alive():
                return
            cfg = _load_config()
            if not cfg.get("yandex_token"):
                return
            if not cfg.get("notifications_enabled", True):
                return
            if sys.platform != "win32":
                # Toasts are Windows-only; on other platforms the watcher
                # has no useful side-effect, so we don't burn API quota.
                return
            cls._stop_event = threading.Event()
            cls._thread = threading.Thread(
                target=cls._loop,
                args=(cls._stop_event,),
                daemon=True,
                name="AGR-Share-Watcher",
            )
            cls._thread.start()
            print("✅ AGR Share watcher started")

    @classmethod
    def stop(cls):
        with cls._lock:
            if cls._stop_event is not None:
                cls._stop_event.set()
            thread = cls._thread
        if thread is not None:
            thread.join(timeout=5.0)
        with cls._lock:
            cls._thread = None
            cls._stop_event = None

    @classmethod
    def restart(cls):
        cls.stop()
        cls.start()

    @staticmethod
    def _loop(stop_event):
        while not stop_event.is_set():
            # cfg is read after the try for the poll interval — keep it bound
            # even when an exception fires before _load_config() returns
            cfg = None
            try:
                cfg = _load_config()
                token = cfg.get("yandex_token", "")
                if not token:
                    break
                if not cfg.get("notifications_enabled", True):
                    break
                my_sender = cfg.get("sender_name", "")
                last_seen = cfg.get("last_seen_ts")

                items = _read_items(token)
                new_items, latest_ts = _diff_new_items(items, last_seen, my_sender)

                if last_seen is None and latest_ts:
                    # Baseline on first run — record, but don't notify.
                    _persist_last_seen(latest_ts)
                elif new_items:
                    _persist_last_seen(latest_ts)
                    for entry in new_items:
                        title = f"AGR Share: {entry.get('sender', '?')}"
                        desc = entry.get("description") or ""
                        body = desc if desc and desc != "No description" \
                            else f"{entry.get('objects_count', 0)} objects shared"
                        _show_windows_toast(title, body)
                    bpy.app.timers.register(
                        lambda items=items: _on_new_items_main_thread(items),
                        first_interval=0.1,
                    )
            except Exception as e:
                print(f"⚠️ AGR Share watcher: {e}")

            interval = max(10, int(cfg.get("poll_interval", 30)) if cfg else 30)
            if stop_event.wait(interval):
                break
        print("AGR Share watcher stopped")


# ---------------------------------------------------------------------------
# PropertyGroup — one shared item in the pool
# ---------------------------------------------------------------------------

class AGR_ShareProject(PropertyGroup):
    """A project folder in the shared clipboard."""
    name: StringProperty(name="Name", default="")


class AGR_ShareItem(PropertyGroup):
    """Single entry in the shared clipboard pool."""
    sender: StringProperty(name="Sender", default="")
    timestamp: StringProperty(name="Time", default="")
    # `url` stores the Yandex.Disk resource path (e.g. "app:/AGR_Share/P/file.blend"),
    # not an HTTP URL — kept for backwards-compatibility with existing index data.
    url: StringProperty(name="URL", default="")
    description: StringProperty(name="Description", default="")
    objects_count: IntProperty(name="Objects", default=0)
    project: StringProperty(name="Project", default="")


# ---------------------------------------------------------------------------
# UIList
# ---------------------------------------------------------------------------

class AGR_UL_ShareItemsList(UIList):
    """Draws one row in the shared items list."""
    bl_idname = "AGR_UL_share_items_list"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            cfg = _load_config()
            sender_name = cfg.get("sender_name", "")
            is_own = (item.sender == sender_name) and sender_name != ""
            item_icon = 'EXPORT' if is_own else 'IMPORT'

            row = layout.row(align=True)
            split = row.split(factor=0.25)
            split.label(text=item.sender, icon=item_icon)

            mid = split.split(factor=0.55)
            mid.label(text=item.description if item.description else "—")

            right = mid.split(factor=0.5)
            right.label(text=f"{item.objects_count} obj")
            right.label(text=_format_relative_time(item.timestamp))

        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text=item.description or item.sender, icon='IMPORT')


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class AGR_OT_SaveShareConfig(Operator):
    """Open settings dialog for the shared clipboard"""
    bl_idname = "agr.save_share_config"
    bl_label = "Share Settings"
    bl_options = {'REGISTER'}

    yandex_token: StringProperty(name="Yandex.Disk Token", default="", subtype='PASSWORD')
    sender_name: StringProperty(name="Your Name", default="")
    notifications_enabled: BoolProperty(
        name="Windows Notifications",
        description="Show a Windows toast when teammates share new items",
        default=True,
    )
    poll_interval: IntProperty(
        name="Poll Interval (sec)",
        description="How often to check Yandex.Disk for new items",
        default=30,
        min=10,
        max=600,
    )

    def invoke(self, context, event):
        cfg = _load_config()
        self.yandex_token = cfg.get("yandex_token", "")
        self.sender_name = cfg.get("sender_name", "")
        self.notifications_enabled = cfg.get("notifications_enabled", True)
        self.poll_interval = int(cfg.get("poll_interval", 30))
        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "yandex_token")
        layout.prop(self, "sender_name")
        layout.separator()
        layout.prop(self, "notifications_enabled")
        row = layout.row()
        row.enabled = self.notifications_enabled
        row.prop(self, "poll_interval")
        if sys.platform != "win32":
            info = layout.box()
            info.label(
                text="Toast notifications are Windows-only.",
                icon='INFO',
            )

    def execute(self, context):
        _update_config(
            yandex_token=self.yandex_token.strip(),
            sender_name=self.sender_name.strip(),
            notifications_enabled=bool(self.notifications_enabled),
            poll_interval=int(self.poll_interval),
        )
        _ShareWatcher.restart()
        self.report({'INFO'}, "Share settings saved")
        print("✅ AGR Share config saved")
        return {'FINISHED'}


# -- Create Project ---------------------------------------------------------

class AGR_OT_CreateShareProject(Operator):
    """Create a new project folder for shared clipboard"""
    bl_idname = "agr.create_share_project"
    bl_label = "New Project"
    bl_options = {'REGISTER'}

    project_name: StringProperty(
        name="Project Name",
        description="Name of the new project folder",
        default="",
    )

    _timer = None
    _thread = None
    _state = None

    @classmethod
    def poll(cls, context):
        return not context.scene.agr_share_is_busy

    def invoke(self, context, event):
        self.project_name = ""
        return context.window_manager.invoke_props_dialog(self, width=300)

    def draw(self, context):
        self.layout.prop(self, "project_name")

    def execute(self, context):
        name = self.project_name.strip()
        if not name:
            self.report({'ERROR'}, "Enter a project name")
            return {'CANCELLED'}

        cfg = _load_config()
        ya_token = cfg.get("yandex_token", "")

        if not ya_token:
            self.report({'ERROR'}, "Configure Yandex.Disk token first")
            return {'CANCELLED'}

        self._state = {"error": None}
        self._thread = threading.Thread(
            target=self._do_create,
            args=(self._state, ya_token, name),
            daemon=True,
        )
        self._thread.start()

        context.scene.agr_share_is_busy = True
        context.scene.agr_share_status = f"Creating project '{name}'..."
        self._timer = context.window_manager.event_timer_add(0.2, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}
        if self._thread and self._thread.is_alive():
            return {'PASS_THROUGH'}

        context.window_manager.event_timer_remove(self._timer)
        scene = context.scene
        scene.agr_share_is_busy = False

        error = self._state.get("error")
        if error:
            scene.agr_share_status = f"Error: {error}"
            self.report({'ERROR'}, error)
            return {'CANCELLED'}

        name = self.project_name.strip()
        scene.agr_share_active_project = name

        existing = [p.name for p in scene.agr_share_projects]
        if name not in existing:
            p = scene.agr_share_projects.add()
            p.name = name

        scene.agr_share_status = f"Project '{name}' created"
        self.report({'INFO'}, f"Project '{name}' created")
        return {'FINISHED'}

    @staticmethod
    def _do_create(state, ya_token, name):
        try:
            _yadisk_mkdir(ya_token, _YADISK_FOLDER)
            _yadisk_mkdir(ya_token, f"{_YADISK_FOLDER}/{name}")

            index_data = _read_index(ya_token)
            projects = index_data.get("projects", [])
            if name not in projects:
                projects.append(name)
            index_data["projects"] = projects
            _write_index(ya_token, index_data)
        except urllib.error.HTTPError as e:
            state["error"] = f"HTTP error {e.code}: {e.reason}"
        except urllib.error.URLError as e:
            state["error"] = f"Network error: {e.reason}"
        except Exception as e:
            state["error"] = str(e)


# -- Share Clipboard --------------------------------------------------------

class AGR_OT_ShareClipboard(Operator):
    """Upload copied objects to the shared clipboard"""
    bl_idname = "agr.share_clipboard"
    bl_label = "Share Clipboard"
    bl_options = {'REGISTER'}

    description_text: StringProperty(
        name="Description",
        description="Short description of what you are sharing",
        default="",
    )

    _timer = None
    _thread = None
    _state = None

    @classmethod
    def poll(cls, context):
        return not context.scene.agr_share_is_busy

    def invoke(self, context, event):
        self._clipboard_contents = self._read_clipboard_contents()
        return context.window_manager.invoke_props_dialog(self, width=420)

    @staticmethod
    def _read_clipboard_contents():
        """Peek into copybuffer.blend and return a dict of datablock names.

        Returns None when no clipboard file exists, {} when it exists but
        cannot be read. Uses libraries.load() in metadata-only mode — does
        not import any datablocks into the current scene.

        The returned dict maps each attribute from _CLIPBOARD_CATEGORIES
        to a list of names (possibly empty).
        """
        clip_path = _get_clipboard_path()
        if not clip_path:
            return None
        try:
            with bpy.data.libraries.load(clip_path, link=False) as (data_from, data_to):
                return {
                    attr: list(getattr(data_from, attr, []) or [])
                    for attr, _, _ in _CLIPBOARD_CATEGORIES
                }
        except Exception as e:
            print(f"⚠️ AGR Share: failed to peek clipboard: {e}")
            return {}

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "description_text", text="Description")

        contents = getattr(self, "_clipboard_contents", None)
        box = layout.box()
        if contents is None:
            box.label(text="Clipboard is empty (Ctrl+C first)", icon='ERROR')
            return
        if not contents or not any(contents.values()):
            box.label(text="Could not read clipboard contents", icon='ERROR')
            return

        total = sum(len(v) for v in contents.values())
        box.label(
            text=f"Clipboard contents — {total} datablock(s):",
            icon='PACKAGE',
        )

        per_section_max = 6
        for attr, label, icon in _CLIPBOARD_CATEGORIES:
            names = contents.get(attr, [])
            if not names:
                continue
            section = box.column(align=True)
            section.label(text=f"{label} ({len(names)})", icon=icon)
            for name in names[:per_section_max]:
                section.label(text=f"    {name}")
            remaining = len(names) - per_section_max
            if remaining > 0:
                section.label(text=f"    ... and {remaining} more")

    def execute(self, context):
        scene = context.scene
        cfg = _load_config()
        sender = cfg.get("sender_name", "") or "Anonymous"
        ya_token = cfg.get("yandex_token", "")

        if not ya_token:
            self.report({'ERROR'}, "Configure Yandex.Disk token first")
            return {'CANCELLED'}

        clip_path = _get_clipboard_path()
        if not clip_path:
            tmpdir = bpy.app.tempdir
            files = os.listdir(tmpdir) if os.path.isdir(tmpdir) else []
            blend_files = [f for f in files if f.endswith(".blend")]
            print(f"⚠️ AGR Share: tempdir={tmpdir}")
            print(f"⚠️ AGR Share: .blend files in tempdir: {blend_files}")
            self.report({'ERROR'}, f"Clipboard not found in {tmpdir}. Copy objects first (Ctrl+C)")
            return {'CANCELLED'}

        with open(clip_path, "rb") as f:
            file_bytes = f.read()

        contents = getattr(self, "_clipboard_contents", None)
        if contents is None:
            contents = self._read_clipboard_contents() or {}
        obj_count = len(contents.get("objects", []))
        desc = self.description_text.strip() or "No description"
        project = scene.agr_share_active_project
        if not project or project == _ALL_PROJECTS:
            self.report({'ERROR'}, f"Select a project folder first (not '{_ALL_PROJECTS}')")
            return {'CANCELLED'}
        timestamp = _utcnow_naive().strftime("%Y%m%d_%H%M%S")
        filename = f"clip_{sanitize_material_name(sender)}_{timestamp}.blend"

        self._state = {"result": None, "error": None}
        self._thread = threading.Thread(
            target=self._do_upload,
            args=(self._state, file_bytes, ya_token,
                  sender, desc, obj_count, filename, project),
            daemon=True,
        )
        self._thread.start()

        scene.agr_share_is_busy = True
        scene.agr_share_status = "Uploading..."
        self._timer = context.window_manager.event_timer_add(0.2, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}
        if self._thread and self._thread.is_alive():
            return {'PASS_THROUGH'}

        context.window_manager.event_timer_remove(self._timer)
        context.scene.agr_share_is_busy = False

        error = self._state.get("error")
        if error:
            context.scene.agr_share_status = f"Error: {error}"
            self.report({'ERROR'}, error)
            return {'CANCELLED'}

        url = self._state.get("result")
        context.scene.agr_share_status = "Shared successfully"
        self.report({'INFO'}, "Objects shared successfully")
        print(f"✅ AGR Share: uploaded to {url}")
        return {'FINISHED'}

    @staticmethod
    def _do_upload(state, file_bytes, ya_token,
                   sender, desc, obj_count, filename, project):
        """Background thread: upload to Yandex.Disk then update index."""
        try:
            disk_path = _upload_file(file_bytes, ya_token, filename, project)
            items = _read_items(ya_token)
            items = _prune_old_items(items)
            items.append({
                "sender": sender,
                "timestamp": _utcnow_naive().isoformat(),
                "disk_path": disk_path,
                "description": desc,
                "objects_count": obj_count,
                "project": project,
            })
            _write_items(ya_token, items)
            state["result"] = disk_path
        except urllib.error.HTTPError as e:
            if e.code == 401:
                state["error"] = "Invalid Yandex.Disk token"
            elif e.code == 404:
                state["error"] = "Index not found on Yandex.Disk"
            else:
                state["error"] = f"HTTP error {e.code}: {e.reason}"
        except urllib.error.URLError as e:
            state["error"] = f"Network error: {e.reason}"
        except Exception as e:
            state["error"] = str(e)


# -- Refresh ----------------------------------------------------------------

class AGR_OT_RefreshShareList(Operator):
    """Refresh the shared items list from Yandex.Disk"""
    bl_idname = "agr.refresh_share_list"
    bl_label = "Refresh Share List"
    bl_options = {'REGISTER'}

    _timer = None
    _thread = None
    _state = None

    @classmethod
    def poll(cls, context):
        return not context.scene.agr_share_is_busy

    def execute(self, context):
        cfg = _load_config()
        ya_token = cfg.get("yandex_token", "")
        if not ya_token:
            self.report({'ERROR'}, "Configure Yandex.Disk token first")
            return {'CANCELLED'}

        self._state = {"items": None, "projects": None, "error": None}
        self._thread = threading.Thread(
            target=self._do_refresh,
            args=(self._state, ya_token),
            daemon=True,
        )
        self._thread.start()

        context.scene.agr_share_is_busy = True
        context.scene.agr_share_status = "Refreshing..."
        self._timer = context.window_manager.event_timer_add(0.2, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}
        if self._thread and self._thread.is_alive():
            return {'PASS_THROUGH'}

        context.window_manager.event_timer_remove(self._timer)
        scene = context.scene
        scene.agr_share_is_busy = False

        error = self._state.get("error")
        if error:
            scene.agr_share_status = f"Error: {error}"
            self.report({'ERROR'}, error)
            return {'CANCELLED'}

        global _cached_items
        _cached_items = self._state.get("items") or []

        scene.agr_share_projects.clear()
        p = scene.agr_share_projects.add()
        p.name = _ALL_PROJECTS
        projects = self._state.get("projects") or []
        for pname in projects:
            p = scene.agr_share_projects.add()
            p.name = pname

        if not scene.agr_share_active_project:
            scene.agr_share_active_project = _ALL_PROJECTS

        _apply_project_filter(scene)

        count = len(scene.agr_share_items)
        self.report({'INFO'}, f"Loaded {count} shared item(s)")
        return {'FINISHED'}

    @staticmethod
    def _do_refresh(state, ya_token):
        try:
            index_data = _read_index(ya_token)

            # Scan Yandex.Disk folders as source of truth
            listed = _yadisk_list_folders(ya_token)

            if listed is None:
                # Listing failed (e.g. 429/503) — show the index as-is and
                # do NOT reconcile: writing here would wipe the shared
                # index based on a transient error
                state["items"] = index_data.get("items", [])
                state["projects"] = sorted(index_data.get("projects", []))
                return

            disk_folders = sorted(listed)

            # Sync: disk folders are the canonical project list. A read
            # operation only writes when disk state genuinely diverged;
            # NOTE: the index has no etag/locking, so concurrent writers can
            # still race — kept minimal deliberately (small team, short window).
            idx_projects = index_data.get("projects", [])
            if set(disk_folders) != set(idx_projects):
                index_data["projects"] = disk_folders
                valid = set(disk_folders)
                index_data["items"] = [
                    i for i in index_data.get("items", [])
                    if i.get("project", "") in valid or not i.get("project")
                ]
                _write_index(ya_token, index_data)

            state["items"] = index_data.get("items", [])
            state["projects"] = disk_folders
        except urllib.error.HTTPError as e:
            if e.code == 404:
                state["error"] = "Index not found on Yandex.Disk"
            else:
                state["error"] = f"HTTP error {e.code}: {e.reason}"
        except urllib.error.URLError as e:
            state["error"] = f"Network error: {e.reason}"
        except Exception as e:
            state["error"] = str(e)


# -- Receive ----------------------------------------------------------------

class AGR_OT_ReceiveShared(Operator):
    """Download and append shared objects into the current scene"""
    bl_idname = "agr.receive_shared"
    bl_label = "Receive Shared"
    bl_options = {'REGISTER', 'UNDO'}

    _timer = None
    _thread = None
    _state = None
    _tmp_path = None

    @classmethod
    def poll(cls, context):
        scene = context.scene
        if scene.agr_share_is_busy:
            return False
        if len(scene.agr_share_items) == 0:
            return False
        idx = scene.agr_share_items_index
        return 0 <= idx < len(scene.agr_share_items)

    def execute(self, context):
        scene = context.scene
        item = scene.agr_share_items[scene.agr_share_items_index]
        disk_path = item.url
        if not disk_path:
            self.report({'ERROR'}, "No path for this item")
            return {'CANCELLED'}

        cfg = _load_config()
        ya_token = cfg.get("yandex_token", "")
        if not ya_token:
            self.report({'ERROR'}, "Configure Yandex.Disk token first")
            return {'CANCELLED'}

        fd, self._tmp_path = tempfile.mkstemp(suffix=".blend")
        os.close(fd)

        self._state = {"error": None}
        self._thread = threading.Thread(
            target=self._do_download,
            args=(self._state, disk_path, self._tmp_path, ya_token),
            daemon=True,
        )
        self._thread.start()

        scene.agr_share_is_busy = True
        scene.agr_share_status = "Downloading..."
        self._timer = context.window_manager.event_timer_add(0.2, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}
        if self._thread and self._thread.is_alive():
            return {'PASS_THROUGH'}

        context.window_manager.event_timer_remove(self._timer)
        scene = context.scene
        scene.agr_share_is_busy = False

        error = self._state.get("error")
        if error:
            scene.agr_share_status = f"Error: {error}"
            self.report({'ERROR'}, error)
            self._cleanup_tmp()
            return {'CANCELLED'}

        # Append objects on main thread
        appended = []
        try:
            with bpy.data.libraries.load(self._tmp_path, link=False) as (data_from, data_to):
                data_to.objects = data_from.objects

            collection = context.collection or context.scene.collection
            for obj in data_to.objects:
                if obj is not None:
                    collection.objects.link(obj)
                    appended.append(obj.name)
        except Exception as e:
            self.report({'ERROR'}, f"Failed to append objects: {e}")
            self._cleanup_tmp()
            return {'CANCELLED'}

        self._cleanup_tmp()
        count = len(appended)
        scene.agr_share_status = f"Received {count} object(s)"
        self.report({'INFO'}, f"Received {count} object(s)")
        print(f"✅ AGR Share: appended {count} objects: {appended}")
        return {'FINISHED'}

    @staticmethod
    def _do_download(state, disk_path, tmp_path, ya_token):
        try:
            _download_file(disk_path, tmp_path, ya_token)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                state["error"] = "File not found on Yandex.Disk"
            elif e.code == 401:
                state["error"] = "Invalid Yandex.Disk token"
            else:
                state["error"] = f"HTTP error {e.code}: {e.reason}"
        except urllib.error.URLError as e:
            state["error"] = f"Network error: {e.reason}"
        except Exception as e:
            state["error"] = str(e)

    def _cleanup_tmp(self):
        if not self._tmp_path:
            return
        try:
            os.remove(self._tmp_path)
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"⚠️ AGR Share: failed to remove tmp file: {e}")


# -- Delete own item --------------------------------------------------------

class AGR_OT_DeleteShareProject(Operator):
    """Delete the active project folder and all its items"""
    bl_idname = "agr.delete_share_project"
    bl_label = "Delete Project"
    bl_options = {'REGISTER'}

    _timer = None
    _thread = None
    _state = None

    @classmethod
    def poll(cls, context):
        scene = context.scene
        if scene.agr_share_is_busy:
            return False
        proj = scene.agr_share_active_project
        return proj and proj != _ALL_PROJECTS

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        scene = context.scene
        project = scene.agr_share_active_project

        cfg = _load_config()
        ya_token = cfg.get("yandex_token", "")
        if not ya_token:
            self.report({'ERROR'}, "Configure Yandex.Disk token first")
            return {'CANCELLED'}

        self._state = {"error": None}
        self._thread = threading.Thread(
            target=self._do_delete_project,
            args=(self._state, ya_token, project),
            daemon=True,
        )
        self._thread.start()

        scene.agr_share_is_busy = True
        scene.agr_share_status = f"Deleting project '{project}'..."
        self._timer = context.window_manager.event_timer_add(0.2, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}
        if self._thread and self._thread.is_alive():
            return {'PASS_THROUGH'}

        context.window_manager.event_timer_remove(self._timer)
        scene = context.scene
        scene.agr_share_is_busy = False

        error = self._state.get("error")
        if error:
            scene.agr_share_status = f"Error: {error}"
            self.report({'ERROR'}, error)
            return {'CANCELLED'}

        scene.agr_share_active_project = _ALL_PROJECTS
        scene.agr_share_status = "Project deleted. Refreshing..."
        bpy.ops.agr.refresh_share_list()
        return {'FINISHED'}

    @staticmethod
    def _do_delete_project(state, ya_token, project):
        try:
            try:
                _yadisk_delete(ya_token, f"{_YADISK_FOLDER}/{project}")
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    print(f"⚠️ AGR Share: delete folder '{project}' failed: HTTP {e.code}")

            index_data = _read_index(ya_token)
            projects = index_data.get("projects", [])
            index_data["projects"] = [p for p in projects if p != project]
            items = index_data.get("items", [])
            index_data["items"] = [i for i in items if i.get("project") != project]
            _write_index(ya_token, index_data)
        except urllib.error.HTTPError as e:
            state["error"] = f"HTTP error {e.code}: {e.reason}"
        except urllib.error.URLError as e:
            state["error"] = f"Network error: {e.reason}"
        except Exception as e:
            state["error"] = str(e)


class AGR_OT_DeleteShared(Operator):
    """Delete a shared item from the pool"""
    bl_idname = "agr.delete_shared"
    bl_label = "Delete Shared"
    bl_options = {'REGISTER'}

    _timer = None
    _thread = None
    _state = None

    @classmethod
    def poll(cls, context):
        scene = context.scene
        if scene.agr_share_is_busy:
            return False
        if len(scene.agr_share_items) == 0:
            return False
        idx = scene.agr_share_items_index
        return 0 <= idx < len(scene.agr_share_items)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        scene = context.scene
        layout = self.layout
        if not (0 <= scene.agr_share_items_index < len(scene.agr_share_items)):
            layout.label(text="No item selected", icon='ERROR')
            return

        item = scene.agr_share_items[scene.agr_share_items_index]
        cfg = _load_config()
        sender_name = cfg.get("sender_name", "")
        is_own = bool(sender_name) and item.sender == sender_name

        if is_own:
            layout.label(text="Delete your shared item?", icon='TRASH')
        else:
            layout.label(text="Delete SOMEONE ELSE'S shared item?", icon='ERROR')

        box = layout.box()
        row = box.row()
        row.label(text="Sender:")
        row.label(text=item.sender or "?",
                  icon='USER' if is_own else 'COMMUNITY')

        row = box.row()
        row.label(text="Description:")
        row.label(text=item.description if item.description else "—")

        row = box.row()
        row.label(text="Shared:")
        row.label(text=f"{_format_relative_time(item.timestamp)} ago")

        row = box.row()
        row.label(text="Project:")
        row.label(text=item.project or "—")

        row = box.row()
        row.label(text="Objects:")
        row.label(text=str(item.objects_count))

        if not is_own:
            layout.separator()
            warn = layout.box()
            warn.alert = True
            warn.label(
                text=f"This file belongs to {item.sender or 'someone else'}. Confirm deletion.",
                icon='ERROR',
            )

    def execute(self, context):
        scene = context.scene
        item = scene.agr_share_items[scene.agr_share_items_index]
        disk_path = item.url

        cfg = _load_config()
        ya_token = cfg.get("yandex_token", "")
        if not ya_token:
            self.report({'ERROR'}, "Configure Yandex.Disk token first")
            return {'CANCELLED'}

        self._state = {"error": None}
        self._thread = threading.Thread(
            target=self._do_delete,
            args=(self._state, ya_token, disk_path),
            daemon=True,
        )
        self._thread.start()

        scene.agr_share_is_busy = True
        scene.agr_share_status = "Deleting..."
        self._timer = context.window_manager.event_timer_add(0.2, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}
        if self._thread and self._thread.is_alive():
            return {'PASS_THROUGH'}

        context.window_manager.event_timer_remove(self._timer)
        scene = context.scene
        scene.agr_share_is_busy = False

        error = self._state.get("error")
        if error:
            scene.agr_share_status = f"Error: {error}"
            self.report({'ERROR'}, error)
            return {'CANCELLED'}

        scene.agr_share_status = "Deleted. Refreshing..."
        bpy.ops.agr.refresh_share_list()
        return {'FINISHED'}

    @staticmethod
    def _do_delete(state, ya_token, disk_path):
        try:
            if disk_path:
                try:
                    _yadisk_delete(ya_token, disk_path)
                except urllib.error.HTTPError as e:
                    if e.code != 404:
                        print(f"⚠️ AGR Share: delete file failed: HTTP {e.code}")

            items = _read_items(ya_token)
            items = [i for i in items if i.get("disk_path") != disk_path]
            _write_items(ya_token, items)
        except urllib.error.HTTPError as e:
            state["error"] = f"HTTP error {e.code}: {e.reason}"
        except urllib.error.URLError as e:
            state["error"] = f"Network error: {e.reason}"
        except Exception as e:
            state["error"] = str(e)


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class AGR_PT_SharePanel(Panel):
    bl_label = "AGR Share"
    bl_idname = "AGR_PT_share_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 100

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        cfg = _load_config()
        is_configured = bool(cfg.get("yandex_token"))
        is_busy = scene.agr_share_is_busy

        # --- Config section ---
        if not is_configured:
            warn = layout.column(align=True)
            warn.alert = True
            warn.label(text="Configure connection", icon='ERROR')
            warn.operator("agr.save_share_config", text="Open Settings", icon='PREFERENCES')
            return

        # --- Project selector ---
        row = layout.row(align=True)
        row.prop_search(
            scene, "agr_share_active_project",
            scene, "agr_share_projects",
            text="", icon='FILE_FOLDER',
        )
        sub = row.row(align=True)
        sub.enabled = not is_busy
        sub.operator("agr.create_share_project", text="", icon='ADD')
        sub = row.row(align=True)
        sub.enabled = not is_busy and scene.agr_share_active_project not in (_ALL_PROJECTS, "")
        sub.operator("agr.delete_share_project", text="", icon='REMOVE')

        # --- Action buttons ---
        row = layout.row(align=True)
        sub = row.row(align=True)
        sub.enabled = not is_busy
        sub.operator("agr.share_clipboard", text="Share", icon='EXPORT')

        sub = row.row(align=True)
        sub.enabled = not is_busy
        sub.operator("agr.refresh_share_list", text="Refresh", icon='FILE_REFRESH')

        # --- Items list ---
        items_count = len(scene.agr_share_items)
        if items_count > 0:
            layout.template_list(
                "AGR_UL_share_items_list", "",
                scene, "agr_share_items",
                scene, "agr_share_items_index",
                rows=4,
            )

            row = layout.row(align=True)
            sub = row.row(align=True)
            sub.enabled = not is_busy
            sub.operator("agr.receive_shared", text="Receive", icon='IMPORT')

            sub = row.row(align=True)
            sub.enabled = not is_busy
            sub.operator("agr.delete_shared", text="Delete", icon='TRASH')
        else:
            layout.label(text="No shared items. Click Refresh.", icon='INFO')

        # --- Status ---
        if scene.agr_share_status:
            layout.label(text=scene.agr_share_status, icon='TIME' if is_busy else 'BLANK1')

        # --- Settings gear ---
        layout.separator()
        row = layout.row()
        row.alignment = 'RIGHT'
        sender = cfg.get("sender_name", "")
        row.label(text=sender if sender else "Anonymous", icon='USER')
        row.operator("agr.save_share_config", text="", icon='PREFERENCES')


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (
    AGR_ShareProject,
    AGR_ShareItem,
    AGR_UL_ShareItemsList,
    AGR_OT_SaveShareConfig,
    AGR_OT_CreateShareProject,
    AGR_OT_ShareClipboard,
    AGR_OT_RefreshShareList,
    AGR_OT_ReceiveShared,
    AGR_OT_DeleteShareProject,
    AGR_OT_DeleteShared,
    AGR_PT_SharePanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.agr_share_projects = CollectionProperty(type=AGR_ShareProject)
    bpy.types.Scene.agr_share_active_project = StringProperty(
        name="Project",
        description="Active project folder (All = show everything)",
        default=_ALL_PROJECTS,
        update=_on_active_project_changed,
    )
    bpy.types.Scene.agr_share_items = CollectionProperty(type=AGR_ShareItem)
    bpy.types.Scene.agr_share_items_index = IntProperty(default=0)
    bpy.types.Scene.agr_share_status = StringProperty(default="")
    bpy.types.Scene.agr_share_is_busy = BoolProperty(default=False)

    print("✅ AGR Share registered")
    _ShareWatcher.start()


def unregister():
    _ShareWatcher.stop()

    global _cached_items, _config_cache
    _cached_items.clear()
    _config_cache = None

    props = [
        "agr_share_is_busy",
        "agr_share_status",
        "agr_share_items_index",
        "agr_share_items",
        "agr_share_active_project",
        "agr_share_projects",
    ]
    for p in props:
        if hasattr(bpy.types.Scene, p):
            delattr(bpy.types.Scene, p)

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    print("AGR Share unregistered")
