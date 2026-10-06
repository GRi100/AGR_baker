"""
AGR Tools - Texture Baking and Management Addon for Blender 5.0
Author: computer_invader
"""

bl_info = {
    "name": "AGR Tools",
    "author": "computer_invader",
    "version": (2, 9, 0),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > AGR Tools",
    "description": "Texture baking, atlas creation, UDIM workflows (per-object records), grid UV mapping, extrude with UV continuation, stub unwrap, linked-object join/restore, asset renaming, lights, sync and team tools",
    "category": "Object",
}

import bpy
import sys
import subprocess
import site
import os

# Make a user-installed Pillow importable.  APPEND, never insert(0): Blender
# deliberately disables user-site because its bundled numpy/requests/cython
# are built for this exact build — putting the user's site-packages FIRST let
# any `pip install --user` package silently shadow the bundle for the whole
# process (baking and atlases included).  At the end of the path Pillow is
# still found and the bundle keeps priority.
try:
    user_site = site.getusersitepackages()
    if user_site and os.path.exists(user_site) and user_site not in sys.path:
        sys.path.append(user_site)
        print(f"📍 Added user site-packages to path: {user_site}")

    # Also try AppData path for Windows
    if sys.platform == 'win32':
        appdata_path = os.path.join(os.environ.get('APPDATA', ''), 'Python', f'Python{sys.version_info.major}{sys.version_info.minor}', 'site-packages')
        if os.path.exists(appdata_path) and appdata_path not in sys.path:
            sys.path.append(appdata_path)
            print(f"📍 Added AppData Python path: {appdata_path}")
except Exception as e:
    print(f"⚠️ Error adding Python paths: {e}")

from . import log

# Single source of truth for the optional Pillow dependency (log.py); the
# probe here only prints the familiar startup line
if log.pillow_available():
    print("✅ PIL/Pillow is available")
else:
    print("⚠️ PIL/Pillow not available - texture resizing will be limited")
    print("   Install with: pip install Pillow")

from . import properties
from . import operators
from . import ui

# log first: WindowManager status props must exist before ui draws them
modules = [
    log,
    properties,
    operators,
    ui,
]

def register():
    """Register all addon classes and properties.

    A failure half-way through (a broken .py after an edit, an API that moved
    in a new Blender) used to leave the process with live classes, Scene
    properties, keymaps, handlers and the Share thread while Blender showed
    the addon as disabled — the next enable then died with "already
    registered".  Roll back what succeeded, in reverse order, and re-raise so
    Blender still reports the real error.

    The module that FAILED is unwound too: the typical failure (an API that
    moved) fires after some of its own classes are already registered, and
    skipping it left exactly the "already registered" state this rollback
    exists to prevent.  Its unregister() is safe to run because
    log.unregister_classes skips classes Blender no longer holds.
    """
    done = []
    try:
        for module in modules:
            module.register()
            done.append(module)
    except Exception:
        failed = modules[len(done):len(done) + 1]
        for module in reversed(done + failed):
            try:
                module.unregister()
            except Exception as rollback_error:
                # keep unwinding: one bad module must not strand the rest
                print(f"⚠️ AGR Tools: rollback of {module.__name__} failed: {rollback_error}")
        print("❌ AGR Tools: registration failed, partial state rolled back")
        raise

    print("✅ AGR Tools registered successfully")

def unregister():
    """Unregister all addon classes and properties"""
    errors = []
    for module in reversed(modules):
        try:
            module.unregister()
        except Exception as e:
            # An exception here used to abort the whole chain, leaving
            # everything below it registered forever
            errors.append(f"{module.__name__}: {e}")

    if errors:
        print("⚠️ AGR Tools unregistered with errors:\n  " + "\n  ".join(errors))
    else:
        print("AGR Tools unregistered")

if __name__ == "__main__":
    register()
