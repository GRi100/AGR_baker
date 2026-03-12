"""
Utility operators for AGR Baker v2
"""

import bpy
from bpy.types import Operator
import sys
import subprocess


class AGR_OT_InstallPillow(Operator):
    """Install PIL/Pillow library for texture resizing"""
    bl_idname = "agr.install_pillow"
    bl_label = "Install Pillow"
    bl_options = {'REGISTER'}
    
    def execute(self, context):
        try:
            # Get Python executable from Blender
            python_exe = sys.executable
            
            self.report({'INFO'}, "Installing Pillow... This may take a moment")
            print("🔧 Installing Pillow...")
            print(f"   Python executable: {python_exe}")
            
            # Install Pillow using pip
            subprocess.check_call([python_exe, "-m", "pip", "install", "Pillow"])
            
            self.report({'INFO'}, "Pillow installed successfully! Please restart Blender")
            print("✅ Pillow installed successfully!")
            print("⚠️ Please restart Blender to use Pillow features")
            
            return {'FINISHED'}
        
        except subprocess.CalledProcessError as e:
            self.report({'ERROR'}, f"Failed to install Pillow: {e}")
            print(f"❌ Failed to install Pillow: {e}")
            return {'CANCELLED'}
        
        except Exception as e:
            self.report({'ERROR'}, f"Error: {e}")
            print(f"❌ Error installing Pillow: {e}")
            return {'CANCELLED'}
    
    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)
    
    def draw(self, context):
        layout = self.layout
        layout.label(text="Install PIL/Pillow for texture resizing?", icon='QUESTION')
        layout.separator()
        layout.label(text="This will run: pip install Pillow")
        layout.label(text="Restart Blender after installation")


classes = (
    AGR_OT_InstallPillow,
)


def register():
    """Register utility operators"""
    for cls in classes:
        bpy.utils.register_class(cls)
    print("✅ Utility operators registered")


def unregister():
    """Unregister utility operators"""
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    print("Utility operators unregistered")
