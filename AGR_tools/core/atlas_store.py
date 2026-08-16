"""
Per-object storage of atlas layout records (AGR_BAKE atlas workflows).

Replaces the on-disk atlas_mapping.json sidecar for objects: every object
an atlas is applied to carries the FULL layout of its atlas(es) as an
idprop + FBX-proof color mirror (see core/attr_store.py).  Legacy JSON
files are still READ as a fallback (and by the object-less Create Atlas
Only path, which keeps writing them - there is no object to carry the
record there).

Record schema:
{"version": 1,
 "atlases": [{"atlas_name": "A_X_Main_1", "atlas_type": "HIGH"|"LOW",
              "atlas_size": 2048, "material_name": "M_X_Main_1", "bin": 0,
              "folder": "//AGR_BAKE/A_X_Main_1",   # blend-relative when possible
              "created_atlases": {"ERM": "T_..._ERM.png", ...},  # basenames
              "layout": [{"set_name", "material_name", "x", "y", "width",
                          "height", "u_min", "v_min", "u_max", "v_max"}]}]}

Multi-atlas objects store one entry per bin - Unpack can rebuild ALL bins
(the legacy JSON path only ever saw the one bin found via Base Color).
"""

import json
import os

import bpy

from .attr_store import ColorBlobStore

ATLAS_STORE = ColorBlobStore(
    prefix="AGR_Atlas_T",
    magic=b"AGRA",
    prop_key="agr_atlas_data",
    validator=lambda d: isinstance(d.get("atlases"), list),
)


def _relpath(path):
    """Blend-relative ('//...') when possible, absolute otherwise."""
    path = str(path)
    try:
        return bpy.path.relpath(path)
    except ValueError:
        return path


def entry_folder_abs(entry):
    """Absolute atlas folder of a record entry ('' when unset)."""
    folder = entry.get("folder", "")
    return bpy.path.abspath(folder) if folder else ""


def serialize_layout(layout):
    """In-memory packing layout (items hold 'texture_set' PropertyGroup
    refs) -> plain JSON-able dicts, same shape as atlas_mapping.json."""
    out = []
    for item in layout:
        ts = item.get('texture_set')
        out.append({
            'set_name': ts.name if ts else item.get('set_name', ''),
            'material_name': ts.material_name if ts else item.get('material_name', ''),
            'x': item['x'], 'y': item['y'],
            'width': item['width'], 'height': item['height'],
            'u_min': item['u_min'], 'v_min': item['v_min'],
            'u_max': item['u_max'], 'v_max': item['v_max'],
        })
    return out


def make_atlas_entry(atlas_name, atlas_type, atlas_size, material_name,
                     folder, created_atlases, layout, bin_index=0):
    """Normalised record entry; `layout` must already be JSON-able
    (serialize_layout for in-memory layouts, legacy JSON layout as is)."""
    return {
        "atlas_name": atlas_name,
        "atlas_type": atlas_type,
        "atlas_size": int(atlas_size) if atlas_size else 0,
        "material_name": material_name or "",
        "bin": int(bin_index),
        "folder": _relpath(folder),
        "created_atlases": {k: os.path.basename(v)
                            for k, v in (created_atlases or {}).items() if v},
        "layout": [dict(item) for item in layout],
    }


def record_from_legacy(mapping, folder):
    """Legacy atlas_mapping.json dict -> record entry."""
    return make_atlas_entry(
        mapping.get("atlas_name") or os.path.basename(str(folder)),
        mapping.get("atlas_type", "HIGH"),
        mapping.get("atlas_size", 0),
        mapping.get("material_name", ""),
        folder,
        mapping.get("created_atlases") or {},
        mapping.get("layout") or [],
    )


def read_atlas_record(obj):
    """Record from the object (idprop first, color mirror fallback) or None."""
    if obj is None:
        return None
    return ATLAS_STORE.read(obj)


def write_atlas_record(obj, atlases):
    """Store the atlas entries (one per bin) on the object."""
    if obj is None:
        return False
    return ATLAS_STORE.write(obj, {"version": 1, "atlases": list(atlases)})


def strip_atlas_record(obj):
    """Remove the record completely (after Unpack)."""
    if obj is not None:
        ATLAS_STORE.strip(obj)


def iter_atlas_entries(peek=True):
    """Yield (obj, entry) over every mesh carrying an atlas record.
    peek=True is poll()/draw()-safe: cached reads, no ID mutations.  The
    O(1) gate keeps the scan cheap on record-less objects."""
    store = ATLAS_STORE
    for obj in bpy.data.objects:
        if obj.type != 'MESH':
            continue
        if obj.get(store.prop_key) is None:
            data = getattr(obj, "data", None)
            if data is None or data.attributes.get(store.prefix + "0") is None:
                continue
        record = store.peek(obj) if peek else store.read(obj)
        if not record:
            continue
        for entry in record.get("atlases", []):
            if isinstance(entry, dict):
                yield obj, entry


def find_atlas_entry(atlas_name, peek=True):
    """First (obj, entry) whose atlas_name matches, else (None, None)."""
    for obj, entry in iter_atlas_entries(peek=peek):
        if entry.get("atlas_name") == atlas_name:
            return obj, entry
    return None, None


def atlas_type_for(atlas_name, default='HIGH'):
    """Atlas type from any per-object record in the file (scan fallback
    for A_* folders that have no legacy JSON)."""
    _obj, entry = find_atlas_entry(atlas_name, peek=True)
    if entry:
        return entry.get("atlas_type", default)
    return default


def load_legacy_atlas_json(folder):
    """Read atlas_mapping.json from an atlas folder (legacy source)."""
    path = os.path.join(str(folder), "atlas_mapping.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as exc:
        print(f"⚠️ Could not read atlas_mapping.json in {folder}: {exc}")
        return None


def save_legacy_atlas_json(folder, entry):
    """Write atlas_mapping.json from a record entry — the on-disk safety
    copy that keeps an atlas folder re-appliable when its on-object record
    is stripped (Unpack) or the carrier object dies.  created_atlases stay
    basenames: readers rebase them onto the folder anyway."""
    try:
        mapping = {
            'atlas_name': entry.get('atlas_name', ''),
            'atlas_type': entry.get('atlas_type', 'HIGH'),
            'atlas_size': entry.get('atlas_size', 0),
            'material_name': entry.get('material_name', ''),
            'created_atlases': dict(entry.get('created_atlases') or {}),
            'layout': [dict(item) for item in entry.get('layout', [])],
        }
        path = os.path.join(str(folder), 'atlas_mapping.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(mapping, f, ensure_ascii=False, indent=2)
        print(f"💾 atlas_mapping.json сохранён: {path}")
        return True
    except Exception as exc:
        print(f"⚠️ Не удалось сохранить atlas_mapping.json: {exc}")
        return False
