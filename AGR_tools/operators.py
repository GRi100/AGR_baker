"""
Operators package for AGR Tools
"""

from . import operators_bake
from . import operators_sets
from . import operators_utils
from . import operators_udim
from . import operators_uv
from . import operators_uv_extrude
from . import operators_link
from . import operators_convert
from . import operators_atlas
from . import operators_frame
from . import operators_rename
from . import operators_rename_project
from . import operators_quick
from . import operators_json
from . import operators_lights
from . import operators_sync
from . import operators_easteregg
from . import operators_library
from . import operators_share

# Registration order; unregistration walks it backwards (keep mirrored)
_MODULES = (
    operators_bake,
    operators_sets,
    operators_utils,
    operators_udim,
    operators_uv,
    operators_uv_extrude,  # after operators_uv: its panel is a child of AGR_PT_uv_panel
    operators_link,
    operators_convert,
    operators_atlas,
    operators_frame,
    operators_rename,
    operators_rename_project,
    operators_quick,
    operators_json,
    operators_lights,
    operators_sync,
    operators_easteregg,
    operators_library,
    operators_share,
)


def register():
    """Register every operator module, rolling back on failure so a broken
    module never leaves the addon half-enabled (see __init__.register).

    The failing module is unwound as well — it is the one most likely to hold
    half of its classes registered."""
    done = []
    try:
        for module in _MODULES:
            module.register()
            done.append(module)
    except Exception:
        failed = list(_MODULES[len(done):len(done) + 1])
        for module in reversed(done + failed):
            try:
                module.unregister()
            except Exception as rollback_error:
                print(f"⚠️ AGR operators: rollback of {module.__name__} failed: {rollback_error}")
        raise

def unregister():
    """Unregister in reverse.  Each module is isolated: an exception in one
    (a property already gone, a keymap Blender freed) must not strand every
    module below it registered until Blender restarts."""
    errors = []
    for module in reversed(_MODULES):
        try:
            module.unregister()
        except Exception as e:
            errors.append(f"{module.__name__}: {e}")

    if errors:
        print("⚠️ AGR operators unregistered with errors:\n  " + "\n  ".join(errors))
