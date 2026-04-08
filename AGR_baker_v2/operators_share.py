"""
AGR Share — shared clipboard over the internet via Yandex.Disk.

Team members can copy objects (Ctrl+C) and share them instantly.
Colleagues see all shared items in the addon panel without manual link exchange.
Files and index are stored on Yandex.Disk.

To disable this feature: comment out the operators_share import in operators.py.
"""

import bpy
import os
import json
import threading
import tempfile
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta
from bpy.types import Operator, Panel, PropertyGroup, UIList
from bpy.props import (
    StringProperty,
    IntProperty,
    BoolProperty,
    CollectionProperty,
)


# ---------------------------------------------------------------------------
# Module-level cache for all items (used to re-filter without network call)
# ---------------------------------------------------------------------------

_cached_items = []  # list of dicts from index, set by Refresh


def _apply_project_filter(scene):
    """Re-populate scene.agr_share_items from cache based on active project."""
    active = scene.agr_share_active_project
    show_all = (active == "All")
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
    label = "All" if show_all else active
    scene.agr_share_status = f"{count} item(s) in '{label}'"


def _on_active_project_changed(self, context):
    """Called when user changes project selection — re-filter locally."""
    _apply_project_filter(context.scene)


# ---------------------------------------------------------------------------
# Config helpers — stored in ~/.agr_baker_share.json (never in .blend)
# ---------------------------------------------------------------------------

_CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".agr_baker_share.json")


def _load_config() -> dict:
    """Read config from disk. Returns empty dict on any error."""
    if os.path.isfile(_CONFIG_PATH):
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_config(data: dict):
    """Write config to disk."""
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Network helpers (called from background threads — no bpy access!)
# ---------------------------------------------------------------------------

_YADISK_API = "https://cloud-api.yandex.net/v1/disk/resources"
_YADISK_FOLDER = "app:/AGR_Share"
_YADISK_INDEX = "app:/AGR_Share/agr_clipboard.json"
_USER_AGENT = "AGR-Baker-Addon"
_TIMEOUT = 60


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


def _yadisk_ensure_folder(ya_token: str):
    """Create AGR_Share folder on Yandex.Disk if it doesn't exist."""
    path = urllib.parse.quote(_YADISK_FOLDER)
    req = urllib.request.Request(
        f"{_YADISK_API}?path={path}",
        method="PUT",
    )
    req.add_header("Authorization", f"OAuth {ya_token}")
    try:
        urllib.request.urlopen(req, timeout=_TIMEOUT)
    except urllib.error.HTTPError as e:
        if e.code == 409:  # folder already exists
            pass
        else:
            raise


def _yadisk_ensure_subfolder(ya_token: str, project: str):
    """Create project subfolder under AGR_Share if it doesn't exist."""
    folder_path = f"{_YADISK_FOLDER}/{project}"
    encoded = urllib.parse.quote(folder_path)
    req = urllib.request.Request(
        f"{_YADISK_API}?path={encoded}",
        method="PUT",
    )
    req.add_header("Authorization", f"OAuth {ya_token}")
    try:
        urllib.request.urlopen(req, timeout=_TIMEOUT)
    except urllib.error.HTTPError as e:
        if e.code == 409:
            pass
        else:
            raise


def _upload_file(file_bytes: bytes, ya_token: str, filename: str,
                 project: str = "") -> str:
    """Upload file to Yandex.Disk AGR_Share/project/ folder. Returns disk path."""
    _yadisk_ensure_folder(ya_token)
    _yadisk_ensure_subfolder(ya_token, project)

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
    _yadisk_ensure_folder(ya_token)
    encoded = urllib.parse.quote(_YADISK_INDEX)
    req = urllib.request.Request(f"{_YADISK_API}/upload?path={encoded}&overwrite=true")
    req.add_header("Authorization", f"OAuth {ya_token}")
    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    upload_url = json.loads(resp.read().decode("utf-8"))["href"]

    payload = json.dumps(index_data, ensure_ascii=False, indent=2).encode("utf-8")
    put_req = urllib.request.Request(upload_url, data=payload, method="PUT")
    put_req.add_header("Content-Type", "application/json")
    urllib.request.urlopen(put_req, timeout=_TIMEOUT)


def _yadisk_list_folders(ya_token: str) -> list:
    """List subfolder names inside AGR_Share on Yandex.Disk."""
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
        return []


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
    cutoff = datetime.utcnow() - timedelta(days=max_days)
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
        delta = datetime.utcnow() - ts
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
# PropertyGroup — one shared item in the pool
# ---------------------------------------------------------------------------

class AGR_ShareProject(PropertyGroup):
    """A project folder in the shared clipboard."""
    name: StringProperty(name="Name", default="")


class AGR_ShareItem(PropertyGroup):
    """Single entry in the shared clipboard pool."""
    sender: StringProperty(name="Sender", default="")
    timestamp: StringProperty(name="Time", default="")
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

    def invoke(self, context, event):
        cfg = _load_config()
        self.yandex_token = cfg.get("yandex_token", "")
        self.sender_name = cfg.get("sender_name", "")
        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "yandex_token")
        layout.prop(self, "sender_name")

    def execute(self, context):
        _save_config({
            "yandex_token": self.yandex_token.strip(),
            "sender_name": self.sender_name.strip(),
        })
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

        # Update local projects list
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
            # Create subfolder on Yandex.Disk
            _yadisk_ensure_folder(ya_token)
            folder_path = f"{_YADISK_FOLDER}/{name}"
            encoded = urllib.parse.quote(folder_path)
            req = urllib.request.Request(
                f"{_YADISK_API}?path={encoded}",
                method="PUT",
            )
            req.add_header("Authorization", f"OAuth {ya_token}")
            try:
                urllib.request.urlopen(req, timeout=_TIMEOUT)
            except urllib.error.HTTPError as e:
                if e.code != 409:
                    raise

            # Add project to index
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
    _state = None  # shared dict: {"result": ..., "error": ...}

    @classmethod
    def poll(cls, context):
        return not context.scene.agr_share_is_busy

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=350)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "description_text", text="Description")

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

        obj_count = len(context.selected_objects)
        desc = self.description_text.strip() or "No description"
        project = scene.agr_share_active_project
        if not project or project == "All":
            self.report({'ERROR'}, "Select a project folder first (not 'All')")
            return {'CANCELLED'}
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filename = f"clip_{sender}_{timestamp}.blend"

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
                "timestamp": datetime.utcnow().isoformat(),
                "disk_path": disk_path,
                "description": desc,
                "objects_count": obj_count,
                "project": project,
            })
            _write_items(ya_token, items)
            state["result"] = disk_path
        except urllib.error.HTTPError as e:
            if e.code == 401:
                state["error"] = "Invalid token (GitHub or Yandex)"
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

        # Update module cache
        global _cached_items
        _cached_items = self._state.get("items") or []

        # Populate projects list with "All" virtual folder first
        scene.agr_share_projects.clear()
        p = scene.agr_share_projects.add()
        p.name = "All"
        projects = self._state.get("projects") or []
        for pname in projects:
            p = scene.agr_share_projects.add()
            p.name = pname

        # Set active project if empty
        if not scene.agr_share_active_project:
            scene.agr_share_active_project = "All"

        # Filter and populate items from cache
        _apply_project_filter(scene)

        count = len(scene.agr_share_items)
        self.report({'INFO'}, f"Loaded {count} shared item(s)")
        return {'FINISHED'}

    @staticmethod
    def _do_refresh(state, ya_token):
        try:
            index_data = _read_index(ya_token)

            # Scan Yandex.Disk folders as source of truth
            disk_folders = sorted(_yadisk_list_folders(ya_token))

            # Sync: disk folders are the canonical project list
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
        disk_path = item.url  # stores Yandex.Disk path
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
        try:
            if self._tmp_path and os.path.isfile(self._tmp_path):
                os.remove(self._tmp_path)
        except Exception:
            pass


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
        return proj and proj != "All"

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

        scene.agr_share_active_project = "All"
        scene.agr_share_status = "Project deleted. Refreshing..."
        bpy.ops.agr.refresh_share_list()
        return {'FINISHED'}

    @staticmethod
    def _do_delete_project(state, ya_token, project):
        try:
            # Delete folder from Yandex.Disk (with all files)
            try:
                folder_path = f"{_YADISK_FOLDER}/{project}"
                encoded = urllib.parse.quote(folder_path)
                req = urllib.request.Request(
                    f"{_YADISK_API}?path={encoded}&permanently=true",
                    method="DELETE",
                )
                req.add_header("Authorization", f"OAuth {ya_token}")
                urllib.request.urlopen(req, timeout=_TIMEOUT)
            except urllib.error.HTTPError:
                pass  # folder may not exist

            # Remove project and its items from index
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
    """Delete your own shared item from the pool"""
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
        if not (0 <= idx < len(scene.agr_share_items)):
            return False
        cfg = _load_config()
        sender = cfg.get("sender_name", "")
        return sender != "" and scene.agr_share_items[idx].sender == sender

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        scene = context.scene
        item = scene.agr_share_items[scene.agr_share_items_index]
        disk_path = item.url  # stores Yandex.Disk path

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

        # Trigger a refresh
        scene.agr_share_status = "Deleted. Refreshing..."
        bpy.ops.agr.refresh_share_list()
        return {'FINISHED'}

    @staticmethod
    def _do_delete(state, ya_token, disk_path):
        try:
            # Remove file from Yandex.Disk (best-effort)
            if disk_path:
                try:
                    encoded = urllib.parse.quote(disk_path)
                    req = urllib.request.Request(
                        f"{_YADISK_API}?path={encoded}&permanently=true",
                        method="DELETE",
                    )
                    req.add_header("Authorization", f"OAuth {ya_token}")
                    urllib.request.urlopen(req, timeout=_TIMEOUT)
                except Exception:
                    pass  # file may already be gone

            # Remove entry from index
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
    bl_category = 'AGR Baker'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        cfg = _load_config()
        is_configured = bool(cfg.get("yandex_token"))
        is_busy = scene.agr_share_is_busy

        # --- Config section ---
        if not is_configured:
            box = layout.box()
            box.label(text="Configure connection", icon='ERROR')
            box.operator("agr.save_share_config", text="Open Settings", icon='PREFERENCES')
            return

        # --- Project selector ---
        box = layout.box()
        row = box.row(align=True)
        row.prop_search(
            scene, "agr_share_active_project",
            scene, "agr_share_projects",
            text="", icon='FILE_FOLDER',
        )
        sub = row.row(align=True)
        sub.enabled = not is_busy
        sub.operator("agr.create_share_project", text="", icon='ADD')
        sub = row.row(align=True)
        sub.enabled = not is_busy and scene.agr_share_active_project not in ("All", "")
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
        default="All",
        update=_on_active_project_changed,
    )
    bpy.types.Scene.agr_share_items = CollectionProperty(type=AGR_ShareItem)
    bpy.types.Scene.agr_share_items_index = IntProperty(default=0)
    bpy.types.Scene.agr_share_status = StringProperty(default="")
    bpy.types.Scene.agr_share_is_busy = BoolProperty(default=False)

    print("✅ AGR Share registered")


def unregister():
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
