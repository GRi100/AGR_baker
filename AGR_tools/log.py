"""
Central logging for AGR Tools.

Console keeps the familiar emoji-print style; a rotating file log lives in
the user's home directory (~/.agr_tools.log) so debugging a failed AGR
submission does not require console screenshots. The last reported message
is mirrored into WindowManager.agr_last_status and shown in the main panel.

Migration note: modules move from bare print() to agr_report()/logger
incrementally — new code should use these helpers.
"""

import logging
import logging.handlers
import os

import bpy

LOG_PATH = os.path.join(os.path.expanduser("~"), ".agr_tools.log")

logger = logging.getLogger("agr_tools")


def _ensure_handlers():
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    logger.propagate = False

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=512 * 1024, backupCount=2, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(file_handler)
    except OSError as e:
        print(f"⚠️ AGR log file unavailable ({e}) — console logging only")


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
