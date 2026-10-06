"""
Central logging and small process-wide helpers for AGR Tools.

Console keeps the familiar emoji-print style; a file log lives in the user's
home directory (~/.agr_tools.<pid>.log) so debugging a failed AGR submission
does not require console screenshots. The last reported message is mirrored
into WindowManager.agr_last_status and shown in the main panel.

The file is per PROCESS on purpose: the user routinely keeps two Blender
versions open (5.0 + 5.2) and headless test runs write at the same time.
A single shared file with RotatingFileHandler cannot rotate on Windows while
another process holds it open — every record past the size cap then died with
a PermissionError traceback in the console AND was lost.  Per-process files
never rotate; stale ones are swept on startup.

This module is also the home of two process-wide utilities that must not
depend on any operator module (they are imported by nearly all of them):
`drop_stale_handlers` (dev-reload-proof handler dedup) and `pillow_available`
(the single source of truth for the optional Pillow dependency).

Migration note: modules move from bare print() to agr_report()/logger
incrementally — new code should use these helpers.
"""

import glob
import logging
import os
import time

import bpy

LOG_DIR = os.path.expanduser("~")
LOG_PREFIX = ".agr_tools"
# Per-process file: see the module docstring for why this is not shared
LOG_PATH = os.path.join(LOG_DIR, f"{LOG_PREFIX}.{os.getpid()}.log")

_LOG_KEEP_DAYS = 7

logger = logging.getLogger("agr_tools")


def _sweep_old_logs():
    """Delete per-process logs older than _LOG_KEEP_DAYS (nobody rotates
    them, so the sweep is the only thing keeping the home dir tidy)."""
    cutoff = time.time() - _LOG_KEEP_DAYS * 86400
    try:
        # Two patterns: the per-process files (.agr_tools.<pid>.log) AND the
        # pre-2.8 shared log with its rotation backups (.agr_tools.log,
        # .agr_tools.log.1/.2) — the old glob matched none of the latter, so
        # a rotated 512 KB leftover sat in the home dir forever
        stale = (glob.glob(os.path.join(LOG_DIR, f"{LOG_PREFIX}.*.log"))
                 + glob.glob(os.path.join(LOG_DIR, f"{LOG_PREFIX}.log*")))
    except OSError:
        return
    for path in stale:
        if path == LOG_PATH:
            continue
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass  # held by a live Blender or not ours to delete


def _ensure_handlers():
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    logger.propagate = False

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)

    _sweep_old_logs()
    try:
        # Truncate on start instead of rotating: one file per process means
        # the only writer is us, and a fresh session wants a fresh log.
        # (A dev reload never reaches this branch — `logger` is global to the
        # process and `_ensure_handlers` returns early on `logger.handlers`.)
        #
        # delay=False on purpose: with delay=True the open moves into
        # FileHandler.emit, which has NO try/except, so an unreachable home
        # (offline roaming profile, read-only share) raised out of the very
        # first logger.info() inside register() and took the whole addon down
        # instead of degrading to console-only.
        file_handler = logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        # A broken file handler must never spam the system console on every
        # record — that noise is what made the old rotation failure so bad
        file_handler.handleError = lambda record: None
        logger.addHandler(file_handler)
    except OSError as e:
        print(f"⚠️ AGR log file unavailable ({e}) — console logging only")


# ---------------------------------------------------------------------------
# Handler bookkeeping (shared by every module that installs app handlers)
# ---------------------------------------------------------------------------

def drop_stale_handlers(handler_list, func_name):
    """Remove every handler named `func_name` from `handler_list`.

    Identity checks (`if f not in handlers`) only catch the SAME function
    object: after a dev reload (reloadOnSave) or a failed unregister the list
    keeps copies from previous module instances, and the live module can no
    longer remove them — their own draw handlers/subscriptions then leak with
    no way back short of restarting Blender.  Matching by __name__ catches
    those zombies too.  Returns the number of handlers removed.
    """
    removed = 0
    for handler in list(handler_list):
        if getattr(handler, "__name__", None) == func_name:
            try:
                handler_list.remove(handler)
                removed += 1
            except ValueError:
                pass
    return removed


def unregister_classes(classes):
    """Unregister `classes` in reverse, skipping the ones Blender does not
    hold any more.

    A register() that dies half-way (a moved API, a broken edit) leaves the
    first N classes of that module live.  The rollback then calls the
    module's unregister(), whose bare loop hit the FIRST never-registered
    class and raised — so the classes that WERE registered stayed, and the
    next enable died with "already registered".  `is_registered` makes the
    unwind idempotent; the default True keeps a stub class without the
    attribute behaving exactly as before.
    """
    for cls in reversed(classes):
        if not getattr(cls, "is_registered", True):
            continue
        try:
            bpy.utils.unregister_class(cls)
        except Exception as e:
            print(f"⚠️ AGR Tools: unregister_class({cls.__name__}) failed: {e}")


# ---------------------------------------------------------------------------
# Pillow availability — ONE flag for the whole addon
# ---------------------------------------------------------------------------

_pillow_available = None


def pillow_available():
    """True when PIL/Pillow can be imported.  Probed once and cached, so the
    per-call cost stays zero in draw(); `set_pillow_available` re-arms it
    after the in-addon installer runs (otherwise the panel warning vanished
    while atlas/frame code kept taking the degraded no-Pillow branch until
    Blender was restarted)."""
    global _pillow_available
    if _pillow_available is None:
        try:
            from PIL import Image  # noqa: F401
            _pillow_available = True
        except ImportError:
            _pillow_available = False
    return _pillow_available


def set_pillow_available(value):
    """Called by the installer operator after a successful pip install."""
    global _pillow_available
    _pillow_available = bool(value)


# Icon per level for the panel status row (ui.py reads this)
STATUS_ICONS = {'INFO': 'CHECKMARK', 'WARNING': 'ERROR', 'ERROR': 'CANCEL'}


def agr_report(operator, level, message):
    """One call = console+file log, operator.report() and the panel status
    line. level: 'INFO' | 'WARNING' | 'ERROR'."""
    _ensure_handlers()
    log_fn = {'INFO': logger.info, 'WARNING': logger.warning, 'ERROR': logger.error}
    log_fn.get(level, logger.info)(message)

    if operator is not None:
        try:
            operator.report({level}, message)
        except Exception:
            pass

    try:
        wm = bpy.context.window_manager
        wm.agr_last_status = message
        wm.agr_last_status_level = level if level in STATUS_ICONS else 'INFO'
    except Exception:
        # No window manager in headless/background runs — log only
        pass


def register():
    _ensure_handlers()
    bpy.types.WindowManager.agr_last_status = bpy.props.StringProperty(
        name="AGR Last Status", default="")
    bpy.types.WindowManager.agr_last_status_level = bpy.props.StringProperty(
        name="AGR Last Status Level", default='INFO')
    logger.info("AGR Tools logging initialised")


def unregister():
    for attr in ("agr_last_status", "agr_last_status_level"):
        if hasattr(bpy.types.WindowManager, attr):
            delattr(bpy.types.WindowManager, attr)
