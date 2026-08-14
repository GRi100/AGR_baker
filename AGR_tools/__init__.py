"""
AGR Tools - Texture Baking and Management Addon for Blender 5.0
Author: computer_invader
"""

bl_info = {
    "name": "AGR Tools",
    "author": "computer_invader",
    "version": (2, 5, 0),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > AGR Tools",
    "description": "Texture baking, atlas creation, UDIM workflows, grid UV mapping, linked-object join/restore, asset renaming, lights, sync and team tools",
    "category": "Object",
}

import bpy
import sys
import subprocess
import site
import os

# Add user site-packages to sys.path for Pillow
try:
    user_site = site.getusersitepackages()
    if user_site and os.path.exists(user_site) and user_site not in sys.path:
        sys.path.insert(0, user_site)
        print(f"📍 Added user site-packages to path: {user_site}")
    
    # Also try AppData path for Windows
    if sys.platform == 'win32':
        appdata_path = os.path.join(os.environ.get('APPDATA', ''), 'Python', f'Python{sys.version_info.major}{sys.version_info.minor}', 'site-packages')
        if os.path.exists(appdata_path) and appdata_path not in sys.path:
            sys.path.insert(0, appdata_path)
            print(f"📍 Added AppData Python path: {appdata_path}")
except Exception as e:
    print(f"⚠️ Error adding Python paths: {e}")

# Check for Pillow availability
PILLOW_AVAILABLE = False
try:
    from PIL import Image
    PILLOW_AVAILABLE = True
    print("✅ PIL/Pillow is available")
except ImportError:
    print("⚠️ PIL/Pillow not available - texture resizing will be limited")
    print("   Install with: pip install Pillow")

from . import log
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
    """Register all addon classes and properties"""
    for module in modules:
        module.register()
    
    print("✅ AGR Tools registered successfully")

def unregister():
    """Unregister all addon classes and properties"""
    for module in reversed(modules):
        module.unregister()

    print("AGR Tools unregistered")

if __name__ == "__main__":
    register()
