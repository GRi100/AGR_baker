"""
Quick Mode modal operator for AGR Baker v2
Provides keyboard-driven baking/convert workflow with viewport HUD overlay.
Activated via Alt+2 in Object or Edit mode.
"""

import bpy
from bpy.types import Operator
import gpu
import blf
from gpu_extras.batch import batch_for_shader


# Singleton instance to prevent multiple quick mode overlays
_active_quick_mode_instance = None

# Registered keymaps for cleanup
addon_keymaps = []

# Resolution enum values matching AGR_BakerSettings.resolution
RESOLUTION_VALUES = ['64', '128', '256', '512', '1024', '2048', '4096']


def _draw_viewport_hints_callback(operator, context):
    """Callback for SpaceView3D draw handler — delegates to operator method."""
    operator.draw_viewport_hints(context)


class AGR_OT_QuickMode(Operator):
    """AGR Baker Quick Mode — keyboard-driven baking and conversion"""
    bl_idname = "agr.quick_mode"
    bl_label = "AGR Quick Mode"
    bl_options = {'REGISTER'}

    def invoke(self, context, event):
        global _active_quick_mode_instance

        # Close previous instance if any
        if _active_quick_mode_instance is not None:
            try:
                _active_quick_mode_instance._is_finished = True
                _active_quick_mode_instance.finish_modal(context)
            except Exception:
                pass
            _active_quick_mode_instance = None

        # Only works in 3D Viewport
        if context.area.type != 'VIEW_3D':
            self.report({'WARNING'}, "Quick Mode works only in 3D Viewport")
            return {'CANCELLED'}

        # Init instance attributes (no __init__ in Blender operators)
        self._handle = None
        self._timer = None
        self._is_finished = False

        _active_quick_mode_instance = self

        # Add viewport draw handler for HUD
        args = (self, context)
        self._handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw_viewport_hints_callback, args, 'WINDOW', 'POST_PIXEL'
        )

        # Timer for periodic redraws
        self._timer = context.window_manager.event_timer_add(0.1, window=context.window)

        context.window_manager.modal_handler_add(self)
        self.report({'INFO'}, "AGR Quick Mode activated")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if self._is_finished:
            return {'FINISHED'}

        # Redraw viewport for HUD updates
        context.area.tag_redraw()

        # --- Mouse wheel: resolution ---
        if event.type == 'WHEELUPMOUSE':
            self.change_resolution(context, 1)
            return {'RUNNING_MODAL'}
        elif event.type == 'WHEELDOWNMOUSE':
            self.change_resolution(context, -1)
            return {'RUNNING_MODAL'}

        # --- Q: Bake from high poly ---
        elif event.type == 'Q' and event.value == 'PRESS':
            if self.quick_bake(context, event):
                self.finish_modal(context)
                return {'FINISHED'}
            return {'RUNNING_MODAL'}

        # --- E: Convert material ---
        elif event.type == 'E' and event.value == 'PRESS':
            if self.quick_convert(context, event):
                self.finish_modal(context)
                return {'FINISHED'}
            return {'RUNNING_MODAL'}

        # --- R: Simple bake ---
        elif event.type == 'R' and event.value == 'PRESS':
            if self.quick_simple_bake(context, event):
                self.finish_modal(context)
                return {'FINISHED'}
            return {'RUNNING_MODAL'}

        # --- WASD: Parameter tuning ---
        elif event.type == 'W' and event.value == 'PRESS':
            self.change_ray_distance(context, 0.01)
            return {'RUNNING_MODAL'}
        elif event.type == 'S' and event.value == 'PRESS':
            self.change_ray_distance(context, -0.01)
            return {'RUNNING_MODAL'}
        elif event.type == 'A' and event.value == 'PRESS':
            self.change_extrusion(context, -0.01)
            return {'RUNNING_MODAL'}
        elif event.type == 'D' and event.value == 'PRESS':
            self.change_extrusion(context, 0.01)
            return {'RUNNING_MODAL'}

        # --- Exit ---
        elif event.type in {'ESC', 'RIGHTMOUSE'}:
            self.finish_modal(context)
            self.report({'INFO'}, "AGR Quick Mode deactivated")
            return {'CANCELLED'}
        elif event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            self.finish_modal(context)
            self.report({'INFO'}, "AGR Quick Mode deactivated")
            return {'CANCELLED'}

        return {'PASS_THROUGH'}

    def finish_modal(self, context):
        """Remove draw handler, timer, and clear global ref."""
        global _active_quick_mode_instance

        self._is_finished = True

        if self._handle:
            bpy.types.SpaceView3D.draw_handler_remove(self._handle, 'WINDOW')
            self._handle = None
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None

        if _active_quick_mode_instance == self:
            _active_quick_mode_instance = None

    # ── Helper methods ──────────────────────────────────────────

    def change_resolution(self, context, direction):
        """Cycle resolution through enum values."""
        settings = context.scene.agr_baker_settings
        current_res = settings.resolution

        try:
            idx = RESOLUTION_VALUES.index(current_res)
        except ValueError:
            idx = RESOLUTION_VALUES.index('1024')

        new_idx = max(0, min(len(RESOLUTION_VALUES) - 1, idx + direction))
        if new_idx != idx:
            settings.resolution = RESOLUTION_VALUES[new_idx]

    def change_ray_distance(self, context, delta):
        """Adjust max_ray_distance by delta (clamped to >= 0)."""
        settings = context.scene.agr_baker_settings
        settings.max_ray_distance = max(0.0, settings.max_ray_distance + delta)

    def change_extrusion(self, context, delta):
        """Adjust extrusion by delta (clamped to >= 0)."""
        settings = context.scene.agr_baker_settings
        settings.extrusion = max(0.0, settings.extrusion + delta)

    # ── Action methods ──────────────────────────────────────────

    def quick_bake(self, context, event):
        """Bake from high poly with modifier-driven normal/alpha settings.

        Default: no normals, no alpha.
        Alt: enable normals.
        Ctrl: enable alpha.
        Modifiers combine (Ctrl+Alt = both).
        """
        settings = context.scene.agr_baker_settings

        # Validate selection for selected-to-active
        if not context.active_object or context.active_object.type != 'MESH':
            self.report({'ERROR'}, "No active mesh object")
            return False
        if len(context.active_object.material_slots) != 1:
            self.report({'ERROR'}, "Active object must have exactly 1 material")
            return False
        other_meshes = [o for o in context.selected_objects
                        if o != context.active_object and o.type == 'MESH']
        if not other_meshes:
            self.report({'ERROR'}, "Select high-poly source objects")
            return False

        # Save original settings
        orig_normal = settings.bake_normal_enabled
        orig_alpha = settings.bake_with_alpha

        # Apply modifier overrides
        settings.bake_normal_enabled = event.alt   # Alt = add normals
        settings.bake_with_alpha = event.ctrl      # Ctrl = add alpha

        try:
            result = bpy.ops.agr.bake_textures()
            success = result != {'CANCELLED'}
        except Exception as e:
            self.report({'ERROR'}, str(e))
            success = False
        finally:
            settings.bake_normal_enabled = orig_normal
            settings.bake_with_alpha = orig_alpha

        if success:
            options = []
            if event.alt:
                options.append("normals")
            if event.ctrl:
                options.append("alpha")
            opt_text = f" + {', '.join(options)}" if options else ""
            self.report({'INFO'}, f"Bake {settings.resolution}px{opt_text}")

        return success

    def quick_convert(self, context, event):
        """Convert material(s) to texture set(s).

        Default: active material only.
        Shift: all materials.
        """
        if not context.active_object or context.active_object.type != 'MESH':
            self.report({'ERROR'}, "No active mesh object")
            return False

        try:
            if event.shift:
                result = bpy.ops.agr.convert_materials_to_sets()
            else:
                result = bpy.ops.agr.convert_active_material_to_set()
            return result != {'CANCELLED'}
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return False

    def quick_simple_bake(self, context, event):
        """Simple bake from material.

        Default: active material only.
        Shift: all materials.
        """
        if not context.active_object or context.active_object.type != 'MESH':
            self.report({'ERROR'}, "No active mesh object")
            return False

        try:
            if event.shift:
                result = bpy.ops.agr.simple_bake_all()
            else:
                result = bpy.ops.agr.simple_bake()
            return result != {'CANCELLED'}
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return False

    # ── HUD rendering ───────────────────────────────────────────

    def set_blf_size(self, font_id, font_size):
        """Blender version-compatible blf.size wrapper."""
        try:
            blf.size(font_id, font_size)
        except TypeError:
            blf.size(font_id, font_size, 72)

    def draw_viewport_hints(self, context):
        """Draw HUD overlay in the bottom-right corner of the viewport."""
        if self._is_finished:
            return

        region = context.region
        width = region.width
        height = region.height

        font_id = 0
        font_size = 18
        line_height = 24

        # Position: bottom-right with padding
        x_offset = width - 400
        y_offset = 50

        # Colors
        bg_color = (0.0, 0.0, 0.0, 0.8)
        text_color = (1.0, 1.0, 1.0, 1.0)
        accent_color = (0.3, 0.8, 1.0, 1.0)

        # Current parameter values
        settings = context.scene.agr_baker_settings

        hints = [
            ("AGR Baker Quick Mode", accent_color),
            ("", text_color),
            (f"Resolution: {settings.resolution}px", text_color),
            (f"Max Ray Distance: {settings.max_ray_distance:.3f}", text_color),
            (f"Extrusion: {settings.extrusion:.3f}", text_color),
            ("", text_color),
            ("Controls:", accent_color),
            ("", text_color),
            ("Scroll - Resolution", text_color),
            ("W/S - Ray Distance ±0.01", text_color),
            ("A/D - Extrusion ±0.01", text_color),
            ("", text_color),
            ("Q - Bake (no normal, no alpha)", text_color),
            ("Alt+Q - Bake + Normal", text_color),
            ("Ctrl+Q - Bake + Alpha", text_color),
            ("Ctrl+Alt+Q - Bake + Both", text_color),
            ("", text_color),
            ("E - Convert active material", text_color),
            ("Shift+E - Convert ALL materials", text_color),
            ("", text_color),
            ("R - Simple bake active", text_color),
            ("Shift+R - Simple bake ALL", text_color),
            ("", text_color),
            ("ESC/RMB/LMB - Exit", text_color),
        ]

        # Compute background dimensions
        self.set_blf_size(font_id, font_size)
        max_text_width = max(blf.dimensions(font_id, text)[0] for text, _ in hints if text)
        bg_width = max_text_width + 30
        bg_height = len(hints) * line_height + 30

        # Clamp position so the HUD stays within the viewport
        if x_offset + bg_width > width:
            x_offset = width - bg_width - 10
        if y_offset + bg_height > height:
            y_offset = height - bg_height - 10

        # Draw background
        try:
            self.draw_background_rect(
                x_offset - 15, y_offset - 15, bg_width, bg_height, bg_color
            )
        except Exception:
            pass

        # Draw text lines
        self.set_blf_size(font_id, font_size)
        current_y = y_offset + bg_height - 40

        for text, color in hints:
            if text:
                blf.color(font_id, *color)
                blf.position(font_id, x_offset, current_y, 0)
                blf.draw(font_id, text)
            current_y -= line_height

    def draw_background_rect(self, x, y, width, height, color):
        """Draw a semi-transparent rectangle behind HUD text."""
        try:
            vertices = [
                (x, y),
                (x + width, y),
                (x + width, y + height),
                (x, y + height),
            ]
            indices = [(0, 1, 2), (2, 3, 0)]

            # Try multiple shader names for Blender version compatibility
            shader = None
            for name in ('UNIFORM_COLOR', 'FLAT_COLOR', '2D_UNIFORM_COLOR'):
                try:
                    shader = gpu.shader.from_builtin(name)
                    break
                except ValueError:
                    continue

            if shader is None:
                return

            batch = batch_for_shader(shader, 'TRIS', {"pos": vertices}, indices=indices)

            gpu.state.blend_set('ALPHA')
            shader.bind()
            shader.uniform_float("color", color)
            batch.draw(shader)
            gpu.state.blend_set('NONE')

        except Exception:
            pass


# ── Registration ────────────────────────────────────────────────

classes = (AGR_OT_QuickMode,)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    # Register keymaps
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc:
        # Object Mode
        km = kc.keymaps.new(name='Object Mode', space_type='EMPTY')
        kmi = km.keymap_items.new('agr.quick_mode', 'TWO', 'PRESS', alt=True)
        addon_keymaps.append((km, kmi))

        # Edit Mode (Mesh)
        km = kc.keymaps.new(name='Mesh', space_type='EMPTY')
        kmi = km.keymap_items.new('agr.quick_mode', 'TWO', 'PRESS', alt=True)
        addon_keymaps.append((km, kmi))


def unregister():
    global _active_quick_mode_instance

    # Close active instance if running
    if _active_quick_mode_instance is not None:
        try:
            _active_quick_mode_instance._is_finished = True
            _active_quick_mode_instance.finish_modal(bpy.context)
        except Exception:
            pass
        _active_quick_mode_instance = None

    # Remove keymaps
    for km, kmi in addon_keymaps:
        km.keymap_items.remove(kmi)
    addon_keymaps.clear()

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
