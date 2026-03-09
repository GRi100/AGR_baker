"""
Operators package for AGR Baker v2
"""

from . import operators_bake
from . import operators_sets

def register():
    operators_bake.register()
    operators_sets.register()

def unregister():
    operators_sets.unregister()
    operators_bake.unregister()
