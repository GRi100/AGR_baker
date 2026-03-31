"""
Operators package for AGR Baker v2
"""

from . import operators_bake
from . import operators_sets
from . import operators_utils
from . import operators_udim
from . import operators_convert
from . import operators_atlas
from . import operators_frame
from . import operators_rename
from . import operators_rename_project
from . import operators_quick
from . import operators_json

def register():
    operators_bake.register()
    operators_sets.register()
    operators_utils.register()
    operators_udim.register()
    operators_convert.register()
    operators_atlas.register()
    operators_frame.register()
    operators_rename.register()
    operators_rename_project.register()
    operators_quick.register()
    operators_json.register()

def unregister():
    operators_json.unregister()
    operators_quick.unregister()
    operators_rename_project.unregister()
    operators_rename.unregister()
    operators_frame.unregister()
    operators_atlas.unregister()
    operators_convert.unregister()
    operators_udim.unregister()
    operators_utils.unregister()
    operators_sets.unregister()
    operators_bake.unregister()
