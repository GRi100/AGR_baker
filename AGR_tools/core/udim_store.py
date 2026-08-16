"""
Per-object storage of the UDIM tile mapping (AGR_BAKE UDIM workflows).

Replaces the on-disk udim_mapping.json sidecar: the record lives on the
object that carries the UDIM material (idprop + FBX-proof color mirror,
see core/attr_store.py).  Legacy JSON files are still READ as a fallback
and migrated onto the object on first use - never written anymore.

Record schema (kept 1:1 with the old udim_mapping.json so every consumer
keeps reading the same dict):
{"object_name": ..., "address": ..., "obj_type": ...,
 "udim_tiles": [{"udim_number": 1001, "material_index": 0,
                 "material_name": "Wall", "set_name": "S_Wall"}, ...]}
"""

from .attr_store import ColorBlobStore

UDIM_STORE = ColorBlobStore(
    prefix="AGR_UDIM_T",
    magic=b"AGRU",
    prop_key="agr_udim_data",
    validator=lambda d: isinstance(d.get("udim_tiles"), list),
)


def read_udim_record(obj):
    """Record from the object (idprop first, color mirror fallback) or None."""
    if obj is None:
        return None
    return UDIM_STORE.read(obj)


def write_udim_record(obj, mapping):
    """Write the mapping onto the object (idprop + color mirror)."""
    if obj is None:
        return False
    return UDIM_STORE.write(obj, mapping)


def strip_udim_record(obj):
    """Remove the record completely (after a full revert)."""
    if obj is not None:
        UDIM_STORE.strip(obj)
