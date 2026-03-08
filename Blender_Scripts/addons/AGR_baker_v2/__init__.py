"""
AGR Baker v2.0 - Texture Baking and Management Addon for Blender 5.0
Author: computer_invader
"""

bl_info = {
    "name": "AGR Baker v2",
    "author": "computer_invader",
    "version": (2, 0, 0),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > AGR Baker",
    "description": "Advanced texture baking and set management with Photoshop integration",
    "category": "Object",
}

import bpy
from . import properties
from . import operators
from . import ui

modules = [
    properties,
    operators,
    ui,
]

def register():
    """Register all addon classes and properties"""
    for module in modules:
        module.register()
    
    print("✅ AGR Baker v2.0 registered successfully")

def unregister():
    """Unregister all addon classes and properties"""
    for module in reversed(modules):
        module.unregister()
    
    print("AGR Baker v2.0 unregistered")

if __name__ == "__main__":
    register()
