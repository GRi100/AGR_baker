"""
Operators package for AGR Baker v2
"""

from . import operators_bake
from . import operators_sets
from . import operators_utils
from . import operators_udim
from . import operators_convert
from . import operators_atlas

def register():
    operators_bake.register()
    operators_sets.register()
    operators_utils.register()
    operators_udim.register()
    operators_convert.register()
    operators_atlas.register()

def unregister():
    operators_atlas.unregister()
    operators_convert.unregister()
    operators_udim.unregister()
    operators_utils.unregister()
    operators_sets.unregister()
    operators_bake.unregister()
