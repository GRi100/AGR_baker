"""
AGR Link - join linked (instanced) objects with full memory of what was
joined, and disassemble the result back into the original linked objects
at any time.

How it works:
- Every face of every participant is stamped with an INT face attribute
  ``agr_link_id`` (instance number) before the join.
- The joined object ("container") carries a JSON table in the object
  custom property ``agr_link_data``: per instance - original name,
  matrix RELATIVE to the container, collections, material slots, parent,
  custom props, face count; per link group - the shared mesh datablock
  name + counts.
- Storing matrices relative to the container makes the math invariant:
  moving/rotating the container after the join transfers to every
  restored object automatically, and nested joins only need a single
  matrix conversion for the merged-in container's entries.
- Disassembly cuts the CURRENT geometry by the attribute (edits made
  after the join survive), transforms each piece back into its original
  local space and re-links identical pieces of one group to a single
  mesh datablock.  Pieces whose geometry no longer matches stay unique
  (reported as a warning).  If the original datablock is still alive in
  the file (some copies were never joined), restored objects re-attach
  to it and become linked with the survivors again.

Origin recovery does NOT rely on the container matrix: at join time every
vertex stores its original LOCAL coordinate (attributes agr_link_co +
agr_link_orig), and disassembly fits the affine frame "stored -> current"
per instance.  This survives Apply Transform / Set Origin on the container,
whole-piece moves in Edit Mode (the origin follows the piece and linking is
kept), and FBX matrix rebuilds.  Containers created by older versions fall
back to the matrix_rel path, where Apply Transform still breaks.

FBX transport: containers are FULLY self-contained - a plain DEFAULT
File->Export/Import FBX carries everything, no checkboxes needed.  Generic
mesh attributes do not survive FBX and the importer quantises colors to
BYTE_COLOR on the sRGB grid, so ALL color-carried data is byte-robust:
written/read through color_srgb (the exact b/255 grid the importer rounds
to), coordinates/ids split hi/lo = 16 bit per value (AGR_Link_CO /
AGR_Link_ID), and the JSON table itself zlib+CRC32-encoded into
AGR_Link_T0..Tn at 1 byte per channel.  Restoration is BIT-EXACT: the full
float32 originals ride inside the byte-exact table blob and each vertex
finds its precise record through its quantised 16-bit key
(order-independent), the quantised channels doubling as correspondence key
and fallback.  After import the idprop table is rebuilt from the colors on
first touch.  UV channels are never used - the
delivered file keeps exactly the user's single UV channel.  "Удалить
память (сдача)" strips everything for a fully clean delivery.

Known limitations (documented, not bugs):
- Material slot overrides with link='OBJECT' are baked into mesh data
  by Blender's join; such instances come back with the override as a
  data material.
- Disassembly copies the container mesh once per instance (quadratic);
  fine for typical containers, slow above several hundred instances.

Modifiers: Blender's join silently DROPS modifiers of all non-active
objects, so the join here is blocked while any participant has
modifiers (modifier support is a possible future step).
"""

import base64
import json

import bpy
import bmesh
import numpy as np
from bpy.props import IntProperty
from bpy.types import Operator, Panel
from mathutils import Matrix

from .log import agr_report
from .core.attr_store import ColorBlobStore, preserve_active_color, read_srgb_bytes
from .core.atlas_store import ATLAS_STORE
from .core.udim_store import UDIM_STORE

ATTR_NAME = "agr_link_id"
CO_ATTR = "agr_link_co"      # per-vertex ORIGINAL local coordinates
ORIG_ATTR = "agr_link_orig"  # per-vertex "existed at join" flag
# FBX cannot carry generic mesh attributes, but vertex colors travel through
# the STANDARD exporter by default - the tracking data is mirrored into two
# permanent color attributes on the container.  Coordinates are normalised to
# [0,1] (bounds stored in the JSON table) to stay safe under the exporter's
# sRGB handling; binary flags ride in alpha, which color management never
# touches.  UV channels are never used (city requirement: 1 UV per object).
# The FBX importer quantises colors to BYTE_COLOR (8 bit/channel on the
# sRGB grid), so ALL color-carried data is byte-robust: written and read
# through "color_srgb" (the exact b/255 grid the importer rounds to).
# Coordinates and ids are split hi/lo into two channels = 16 bit/value.
COL_CO = "AGR_Link_CO"   # RGBA = (x_hi, x_lo, y_hi, y_lo)
COL_ID = "AGR_Link_ID"   # RGBA = (z_hi, z_lo, flag<<7|id_hi, id_lo)
# The JSON table itself is ALSO encoded into color attributes
# (AGR_Link_T0..Tn): zlib-compressed bytes, 1 byte per channel, with a
# CRC32-guarded header.  A container is therefore fully self-contained -
# a plain default FBX export carries EVERYTHING.
TABLE_COL_PREFIX = "AGR_Link_T"
TABLE_MAGIC = b"AGRL"
PROP_KEY = "agr_link_data"
TABLE_VERSION = 1
# Marker for the scene master collection (it has no entry in bpy.data.collections)
SCENE_ROOT = "*SCENE_ROOT*"


# ----------------------------------------------------------------------------
# Metadata table helpers
# ----------------------------------------------------------------------------

def _matrix_to_list(m):
    return [list(row) for row in m]


def _new_table():
    return {
        "version": TABLE_VERSION,
        "groups": {},      # str(gid) -> {data_name, verts, faces}
        "instances": {},   # str(iid) -> instance entry
        "next_instance": 1,
        "next_group": 1,
    }


# The generic idprop+color-mirror transport lives in core/attr_store.py
# (extracted from this module).  Link keeps its old function names as thin
# delegates so the ~35 internal call sites and the test suite stay put.
_LINK_STORE = ColorBlobStore(
    prefix=TABLE_COL_PREFIX,
    magic=TABLE_MAGIC,
    prop_key=PROP_KEY,
    validator=lambda table: "instances" in table,
    idprop_exclude=("precise_",),
)
# Link's own poll/draw cache is _MERGED_CACHE (below); the store-level
# cache is only populated if future code calls _LINK_STORE.peek/read
# directly.  Both are cleared together (strip/reconcile/unregister), and
# the alias must keep pointing at the store's own dict (never reassigned).
_TABLE_CACHE = _LINK_STORE.cache


def _parse_table(raw):
    """The ONE parse/validate step shared by read_table and _peek_table."""
    return _LINK_STORE.parse_idprop(raw)


def read_table(obj):
    """Fresh, mutation-safe parse of the container table (or None).
    Falls back to the color-encoded table (fresh FBX import with default
    settings - no idprop yet).  When the mesh carries foreign plain-Ctrl+J
    table windows the result is a VIRTUAL merged view - operators that
    stamp or write must run _reconcile_container(context, obj) first (it
    materialises exactly the same ids).  poll/draw must use the cached
    _peek_table."""
    table, _extras = _merged_view(obj)
    return table


def write_table(obj, table):
    # precise_* blobs live ONLY in the color encoding - keeping megabytes of
    # base64 out of the idprop (the .blend and the FBX user property)
    _LINK_STORE.write_idprop(obj, table)


def _peek_table(obj):
    """Cached, poll()/draw()-safe read (merged view - see _peek_merged)."""
    return _peek_merged(obj)[0]


def is_container(obj):
    return obj is not None and obj.type == 'MESH' and _peek_table(obj) is not None


def _match_or_add_group(table, ginfo):
    """Reuse an existing group when the datablock identity matches
    (name + vert/face counts), otherwise register a new one.  This is what
    lets 'join more copies into an existing container later' land in the
    same link group."""
    for gid, existing in table["groups"].items():
        if (existing["data_name"] == ginfo["data_name"]
                and existing["verts"] == ginfo["verts"]
                and existing["faces"] == ginfo["faces"]):
            return int(gid)
    gid = table["next_group"]
    table["next_group"] += 1
    table["groups"][str(gid)] = dict(ginfo)
    return gid


def _capture_collections(obj, context):
    names = []
    master = context.scene.collection
    for coll in obj.users_collection:
        names.append(SCENE_ROOT if coll == master else coll.name)
    return names


def _capture_props(obj):
    """Shallow, JSON-safe snapshot of the object's custom properties."""
    props = {}
    for key in obj.keys():
        if key == PROP_KEY:
            continue
        value = obj[key]
        if hasattr(value, "to_dict"):
            value = value.to_dict()
        elif hasattr(value, "to_list"):
            value = value.to_list()
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            continue  # datablock pointers etc. - not restorable from JSON
        props[key] = value
    return props


def _capture_instance(obj, inv_container, context):
    return {
        "name": obj.name,
        "matrix_rel": _matrix_to_list(inv_container @ obj.matrix_world),
        "faces": len(obj.data.polygons),
        "collections": _capture_collections(obj, context),
        "materials": [ms.material.name if ms.material else "" for ms in obj.material_slots],
        "parent": obj.parent.name if obj.parent else None,
        "parent_type": obj.parent_type,
        "parent_bone": obj.parent_bone,
        "parent_vertices": list(obj.parent_vertices) if obj.parent_type in {'VERTEX', 'VERTEX_3'} else [],
        "matrix_parent_inverse": _matrix_to_list(obj.matrix_parent_inverse),
        "props": _capture_props(obj),
    }


# ----------------------------------------------------------------------------
# Face attribute helpers (OBJECT mode only)
# ----------------------------------------------------------------------------

def _ensure_attr(mesh):
    attr = mesh.attributes.get(ATTR_NAME)
    if attr is not None and (attr.domain != 'FACE' or attr.data_type != 'INT'):
        mesh.attributes.remove(attr)
        attr = None
    if attr is None:
        attr = mesh.attributes.new(ATTR_NAME, 'INT', 'FACE')
    return attr


def _stamp_fill(mesh, iid):
    attr = _ensure_attr(mesh)
    attr.data.foreach_set("value", np.full(len(mesh.polygons), iid, dtype=np.intc))


def _stamp_remap(mesh, id_map, loop_range=None):
    """Remap existing attribute values old->new; unknown values become 0.
    With ``loop_range=(lo, hi)`` only faces whose loop_start falls in the
    range are touched — used to remap ONE absorbed window of a plain
    Ctrl+J while the ids of the other blocks stay intact."""
    attr = _ensure_attr(mesh)
    n = len(mesh.polygons)
    arr = np.zeros(n, dtype=np.intc)
    attr.data.foreach_get("value", arr)
    max_old = max(id_map.keys(), default=0)
    lut = np.zeros(max_old + 1, dtype=np.intc)
    for old, new in id_map.items():
        lut[old] = new
    # ids outside the map (e.g. faces added by a foreign plain Ctrl+J) -> 0
    clipped = np.where((arr < 0) | (arr > max_old), 0, arr)
    remapped = lut[clipped]
    if loop_range is not None:
        lo, hi = loop_range
        starts = np.zeros(n, dtype=np.intc)
        mesh.polygons.foreach_get("loop_start", starts)
        seg = (starts >= lo) & (starts < hi)
        remapped = np.where(seg, remapped, arr)
    attr.data.foreach_set("value", remapped)


def _stamp_original_coords(mesh):
    """Store each vertex's current local coordinate + an "existed at join"
    flag as POINT attributes.  This makes every instance self-describing:
    disassembly recovers the origin by fitting stored->current coordinates,
    so Apply Transform / Set Origin / whole-piece edit-mode moves on the
    container no longer break origins or linking."""
    co = mesh.attributes.get(CO_ATTR)
    if co is not None and (co.domain != 'POINT' or co.data_type != 'FLOAT_VECTOR'):
        mesh.attributes.remove(co)
        co = None
    if co is None:
        co = mesh.attributes.new(CO_ATTR, 'FLOAT_VECTOR', 'POINT')
    n = len(mesh.vertices)
    arr = np.zeros(n * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", arr)
    co.data.foreach_set("vector", arr)

    flag = mesh.attributes.get(ORIG_ATTR)
    if flag is not None and (flag.domain != 'POINT' or flag.data_type != 'BOOLEAN'):
        mesh.attributes.remove(flag)
        flag = None
    if flag is None:
        flag = mesh.attributes.new(ORIG_ATTR, 'BOOLEAN', 'POINT')
    flag.data.foreach_set("value", np.ones(n, dtype=bool))


def _remove_tracking_attrs(mesh):
    doomed = [a.name for a in mesh.attributes
              if a.name in (ATTR_NAME, CO_ATTR, ORIG_ATTR, COL_CO, COL_ID)
              or a.name.startswith(TABLE_COL_PREFIX)]
    for name in doomed:
        attr = mesh.attributes.get(name)
        if attr is not None:
            mesh.attributes.remove(attr)


def _read_face_ids(mesh):
    attr = mesh.attributes.get(ATTR_NAME)
    if attr is None or attr.domain != 'FACE' or attr.data_type != 'INT':
        return None
    arr = np.zeros(len(mesh.polygons), dtype=np.intc)
    attr.data.foreach_get("value", arr)
    return arr


def _read_attr_values(mesh):
    attr = mesh.attributes.get(ATTR_NAME)
    if attr is None:
        return None
    arr = np.zeros(len(mesh.polygons), dtype=np.intc)
    attr.data.foreach_get("value", arr)
    return arr


def _fit_affine_core(p, q, mask, extra_tol=0.0):
    """Least-squares affine fit p[mask] -> q[mask] with trimmed-outlier
    refit and SVD normal-completion for planar clouds.  Pure computation
    (no mesh access).  Returns (a, t, res, tol) or None when degenerate;
    ``res`` are residuals over the masked points, ``tol`` the accept
    threshold used for snapping decisions."""
    if int(mask.sum()) < 3:
        return None

    def fit(sel):
        pm, qm = p[sel].mean(axis=0), q[sel].mean(axis=0)
        pc, qc = p[sel] - pm, q[sel] - qm
        s_vals = np.linalg.svd(pc, compute_uv=False)
        if s_vals[1] < 1e-6 * max(s_vals[0], 1e-9):
            return None  # collinear/degenerate point cloud
        x, *_ = np.linalg.lstsq(pc, qc, rcond=None)
        a = x.T
        if s_vals[2] < 1e-6 * s_vals[0]:
            # planar piece: the normal direction is unconstrained by the fit -
            # complete it so the frame stays invertible and orientation-true
            _u, _s, vt = np.linalg.svd(pc, full_matrices=False)
            u1, u2 = vt[0], vt[1]
            n_src = np.cross(u1, u2)
            v1, v2 = a @ u1, a @ u2
            n_img = np.cross(v1, v2)
            ln = float(np.linalg.norm(n_img))
            if ln < 1e-12:
                return None
            scale = np.sqrt(np.linalg.norm(v1) * np.linalg.norm(v2))
            a = a + np.outer(n_img / ln * scale, n_src)
        t = qm - a @ pm
        return a, t

    result = fit(mask)
    if result is None:
        return None
    a, t = result
    res = np.linalg.norm(q[mask] - (p[mask] @ a.T + t), axis=1)
    diag = float(np.linalg.norm(p[mask].max(axis=0) - p[mask].min(axis=0)))
    tol = max(1e-4, diag * 1e-4, float(np.linalg.norm(t)) * 1e-6, extra_tol)
    if res.max() > tol:
        # part of the piece was edited - refit on the rigid majority
        thr = max(tol, 3.0 * float(np.median(res)))
        inliers = np.zeros(len(mask), dtype=bool)
        inliers[np.flatnonzero(mask)[res <= thr]] = True
        if int(inliers.sum()) >= 3:
            refit = fit(inliers)
            if refit is not None:
                a, t = refit
                res = np.linalg.norm(q[mask] - (p[mask] @ a.T + t), axis=1)
                tol = max(1e-4, diag * 1e-4, float(np.linalg.norm(t)) * 1e-6, extra_tol)

    if abs(np.linalg.det(a)) < 1e-12:
        return None
    return a, t, res, tol


def _affine_to_matrix(a, t):
    return Matrix((
        (a[0][0], a[0][1], a[0][2], t[0]),
        (a[1][0], a[1][1], a[1][2], t[1]),
        (a[2][0], a[2][1], a[2][2], t[2]),
        (0.0, 0.0, 0.0, 1.0),
    ))


def _solve_instance_frame(mesh, extra_tol=0.0):
    """Recover the instance frame from the stored per-vertex original
    coordinates: least-squares affine fit original-local -> current
    container-local.  Immune to container Apply Transform / Set Origin /
    whole-piece edit-mode moves (the frame follows the piece) and to FBX
    matrix rebuilds - vertex order is irrelevant, the pairing rides with
    each vertex.  Rewrites the mesh vertices back into original local space
    (snapping unedited verts to their exact stored coords) and returns the
    frame Matrix, or None when the attributes are absent or the point cloud
    is degenerate (legacy containers fall back to the matrix path)."""
    co_attr = mesh.attributes.get(CO_ATTR)
    flag_attr = mesh.attributes.get(ORIG_ATTR)
    if (co_attr is None or co_attr.domain != 'POINT' or co_attr.data_type != 'FLOAT_VECTOR'
            or flag_attr is None or flag_attr.domain != 'POINT' or flag_attr.data_type != 'BOOLEAN'):
        return None
    n = len(mesh.vertices)
    if n == 0:
        return None
    p32 = np.zeros(n * 3, dtype=np.float32)
    co_attr.data.foreach_get("vector", p32)
    p = p32.reshape(-1, 3).astype(np.float64)
    mask = np.zeros(n, dtype=bool)
    flag_attr.data.foreach_get("value", mask)
    q32 = np.zeros(n * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", q32)
    q = q32.reshape(-1, 3).astype(np.float64)

    core = _fit_affine_core(p, q, mask, extra_tol)
    if core is None:
        return None
    a, t, res, tol = core

    # final vertex coords: exact stored originals for unedited verts (kills
    # float32 container-space noise), frame-inverse for edited/new verts
    a_inv = np.linalg.inv(a)
    final = (q - t) @ a_inv.T
    snap = np.zeros(n, dtype=bool)
    snap[np.flatnonzero(mask)[res <= tol]] = True
    final[snap] = p[snap]
    mesh.vertices.foreach_set("co", final.astype(np.float32).ravel())

    return _affine_to_matrix(a, t)


def _has_loose_geometry(mesh):
    if len(mesh.polygons) == 0:
        return len(mesh.vertices) > 0
    used = np.zeros(len(mesh.loops), dtype=np.intc)
    mesh.loops.foreach_get("vertex_index", used)
    return len(np.unique(used)) < len(mesh.vertices)


# ----------------------------------------------------------------------------
# FBX transport: permanent color-attribute mirror of the tracking data.
# The STANDARD FBX exporter carries vertex colors by default, so a plain
# File→Export/Import moves the memory with no special operators.
# ----------------------------------------------------------------------------

def _remove_color_mirror(mesh):
    """Remove ONLY the color-mirror attributes (keep the internal tracking
    attrs) - used when the mirror could not be (re)written, so a stale
    mirror never contradicts the idprop table."""
    doomed = [a.name for a in mesh.attributes
              if a.name in (COL_CO, COL_ID) or a.name.startswith(TABLE_COL_PREFIX)]
    for name in doomed:
        attr = mesh.attributes.get(name)
        if attr is not None:
            mesh.attributes.remove(attr)


def _pack_table_to_colors(mesh, table):
    """Encode the JSON table into AGR_Link_T* color attributes (zlib bytes
    on the sRGB b/255 grid, CRC-guarded — see core/attr_store.py).  Returns
    False when the mesh cannot hold it; capacity is checked BEFORE the old
    mirror is removed."""
    return _LINK_STORE.pack_colors(mesh, table)


def _read_srgb_bytes(attr):
    return read_srgb_bytes(attr)


def _decode_table_from_colors(mesh):
    """Decode the JSON table from AGR_Link_T* color attributes (after an
    FBX round trip with default settings).  CRC-guarded: returns None on
    any corruption instead of a plausible-but-wrong table."""
    return _LINK_STORE.decode_colors(mesh)


def _pack_tracking_to_colors(mesh, table):
    """Mirror the tracking attributes into the two color attributes and
    store the normalisation bounds in the table.  Overwrites any previous
    mirror (called after every join).  Finishes by encoding the table
    itself into AGR_Link_T* - the container becomes fully self-contained
    for a plain default FBX export."""
    attr = mesh.attributes.get(ATTR_NAME)
    co_attr = mesh.attributes.get(CO_ATTR)
    flag_attr = mesh.attributes.get(ORIG_ATTR)
    if attr is None or co_attr is None or flag_attr is None:
        _remove_color_mirror(mesh)  # never leave a stale mirror behind
        return False
    n_verts = len(mesh.vertices)
    n_loops = len(mesh.loops)
    n_polys = len(mesh.polygons)
    if n_verts == 0 or n_loops == 0:
        _remove_color_mirror(mesh)
        return False

    co = np.zeros(n_verts * 3, dtype=np.float32)
    co_attr.data.foreach_get("vector", co)
    co32 = co.reshape(-1, 3)
    co = co32.astype(np.float64)
    flags = np.zeros(n_verts, dtype=bool)
    flag_attr.data.foreach_get("value", flags)
    ids = np.zeros(n_polys, dtype=np.intc)
    attr.data.foreach_get("value", ids)

    co_min = co.min(axis=0)
    co_size = np.maximum(co.max(axis=0) - co_min, 1e-6)
    table["co_min"] = [float(v) for v in co_min]
    table["co_size"] = [float(v) for v in co_size]

    vidx = np.zeros(n_loops, dtype=np.intc)
    mesh.loops.foreach_get("vertex_index", vidx)
    loop_total = np.zeros(n_polys, dtype=np.intc)
    mesh.polygons.foreach_get("loop_total", loop_total)
    ids_per_loop = np.repeat(ids.astype(np.float64), loop_total)

    # never leave an AGR service layer as the mesh's active/render color:
    # a material with a blank-name Color Attribute node would render the
    # packed bytes as vertex-colour noise (the guard spans COL_CO/COL_ID
    # creation too - pack_colors below only covers the table layers)
    with preserve_active_color(mesh):
        for name in (COL_CO, COL_ID):
            old = mesh.attributes.get(name)
            if old is not None:
                mesh.attributes.remove(old)
        col_co = mesh.color_attributes.new(name=COL_CO, type='FLOAT_COLOR', domain='CORNER')
        col_id = mesh.color_attributes.new(name=COL_ID, type='FLOAT_COLOR', domain='CORNER')
        # re-fetch: the second new() may reallocate the CustomData layer
        # array, leaving the first reference dangling
        col_co = mesh.attributes.get(COL_CO)

        # 16-bit hi/lo per value on the byte-robust sRGB grid
        v16 = np.clip(np.rint((co - co_min) / co_size * 65535.0), 0, 65535).astype(np.uint32)
        v16_loop = v16[vidx]
        ids_loop = np.clip(ids_per_loop, 0, 32767).astype(np.uint32)
        flag_loop = flags[vidx].astype(np.uint32)
        data_co = np.empty((n_loops, 4), dtype=np.float32)
        data_co[:, 0] = (v16_loop[:, 0] >> 8) / 255.0
        data_co[:, 1] = (v16_loop[:, 0] & 255) / 255.0
        data_co[:, 2] = (v16_loop[:, 1] >> 8) / 255.0
        data_co[:, 3] = (v16_loop[:, 1] & 255) / 255.0
        data_id = np.empty((n_loops, 4), dtype=np.float32)
        data_id[:, 0] = (v16_loop[:, 2] >> 8) / 255.0
        data_id[:, 1] = (v16_loop[:, 2] & 255) / 255.0
        data_id[:, 2] = ((flag_loop << 7) | (ids_loop >> 8)) / 255.0
        data_id[:, 3] = (ids_loop & 255) / 255.0
        col_co.data.foreach_set("color_srgb", data_co.ravel())
        col_id.data.foreach_set("color_srgb", data_id.ravel())

        # bit-exact layer: full float32 originals ride inside the byte-exact
        # table blob; the quantised channels above serve as the per-vertex
        # correspondence key (order-independent) and as a fallback
        table["precise_n"] = int(n_verts)
        table["precise_co"] = base64.b64encode(co32.astype("<f4").tobytes()).decode("ascii")
        table_ok = _pack_table_to_colors(mesh, table)

    if not table_ok:
        _remove_color_mirror(mesh)
        return False
    return True


def _unpack_tracking_from_colors(mesh, table):
    """Rebuild the internal tracking attributes from the color mirror after
    an FBX round trip.  Tolerates the importer changing the domain
    (CORNER/POINT) and BYTE_COLOR degradation."""
    col_co = mesh.attributes.get(COL_CO)
    col_id = mesh.attributes.get(COL_ID)
    co_min = table.get("co_min")
    co_size = table.get("co_size")
    if col_co is None or col_id is None or co_min is None or co_size is None:
        return False
    n_verts = len(mesh.vertices)
    n_loops = len(mesh.loops)
    n_polys = len(mesh.polygons)
    if n_verts == 0 or n_loops == 0:
        return False
    vidx = np.zeros(n_loops, dtype=np.intc)
    mesh.loops.foreach_get("vertex_index", vidx)
    loop_start = np.zeros(n_polys, dtype=np.intc)
    mesh.polygons.foreach_get("loop_start", loop_start)

    def per_vertex_bytes(attr_):
        b = _read_srgb_bytes(attr_).reshape(-1, 4).astype(np.uint32)
        if attr_.domain == 'CORNER' and len(b) == n_loops:
            out = np.zeros((n_verts, 4), dtype=np.uint32)
            out[vidx] = b
            return out, b
        if attr_.domain == 'POINT' and len(b) == n_verts:
            return b, b[vidx]
        return None, None

    b_co_v, _b_co_l = per_vertex_bytes(col_co)
    b_id_v, b_id_l = per_vertex_bytes(col_id)
    if b_co_v is None or b_id_v is None:
        return False

    v16 = np.empty((n_verts, 3), dtype=np.int64)
    v16[:, 0] = b_co_v[:, 0] * 256 + b_co_v[:, 1]
    v16[:, 1] = b_co_v[:, 2] * 256 + b_co_v[:, 3]
    v16[:, 2] = b_id_v[:, 0] * 256 + b_id_v[:, 1]
    co = v16.astype(np.float64) / 65535.0 * np.asarray(co_size) + np.asarray(co_min)
    flags = b_id_v[:, 2] >= 128

    id_hi = b_id_l[loop_start, 2]
    id_lo = b_id_l[loop_start, 3]
    face_ids = ((id_hi & 127) * 256 + id_lo).astype(np.intc)

    # bit-exact overlay: the color-encoded table blob carries the full
    # float32 originals; each vertex finds its precise record through the
    # quantised 16-bit key (order-independent, collisions only within one
    # quantum where any candidate is equally right)
    full_precision = False
    blob_table = _decode_table_from_colors(mesh)
    if blob_table is not None and "precise_co" in blob_table and "precise_n" in blob_table:
        try:
            raw = base64.b64decode(blob_table["precise_co"])
            precise = np.frombuffer(raw, dtype="<f4")
        except (ValueError, TypeError):
            precise = None
        if precise is not None and len(precise) == int(blob_table["precise_n"]) * 3:
            precise = precise.reshape(-1, 3)
            b_min = np.asarray(blob_table.get("co_min", co_min))
            b_size = np.asarray(blob_table.get("co_size", co_size))
            k16 = np.clip(np.rint((precise.astype(np.float64) - b_min) / b_size * 65535.0),
                          0, 65535).astype(np.int64)
            keys_pack = (k16[:, 0] << 32) | (k16[:, 1] << 16) | k16[:, 2]
            keys_mesh = (v16[:, 0] << 32) | (v16[:, 1] << 16) | v16[:, 2]
            order = np.argsort(keys_pack, kind="stable")
            sorted_keys = keys_pack[order]
            pos = np.clip(np.searchsorted(sorted_keys, keys_mesh), 0, len(sorted_keys) - 1)
            hit = sorted_keys[pos] == keys_mesh
            co[hit] = precise[order[pos[hit]]]
            # loop-less (loose) verts carry no CORNER data by construction -
            # they can never key-match and must not veto full precision
            covered = np.zeros(n_verts, dtype=bool)
            covered[vidx] = True
            full_precision = bool(hit[covered].all()) if covered.any() else False

    attr = _ensure_attr(mesh)
    attr.data.foreach_set("value", face_ids)
    co_attr = mesh.attributes.get(CO_ATTR)
    if co_attr is not None and (co_attr.domain != 'POINT' or co_attr.data_type != 'FLOAT_VECTOR'):
        mesh.attributes.remove(co_attr)
        co_attr = None
    if co_attr is None:
        co_attr = mesh.attributes.new(CO_ATTR, 'FLOAT_VECTOR', 'POINT')
    co_attr.data.foreach_set("vector", co.astype(np.float32).ravel())
    flag_attr = mesh.attributes.get(ORIG_ATTR)
    if flag_attr is not None and (flag_attr.domain != 'POINT' or flag_attr.data_type != 'BOOLEAN'):
        mesh.attributes.remove(flag_attr)
        flag_attr = None
    if flag_attr is None:
        flag_attr = mesh.attributes.new(ORIG_ATTR, 'BOOLEAN', 'POINT')
    flag_attr.data.foreach_set("value", flags)
    # remember the 16-bit quantisation step UNLESS every vertex got its
    # bit-exact original back: the fit/snap/relink tolerances must absorb
    # the quantum or copies diverge by a hair after the FBX roundtrip
    if full_precision:
        table["co_quant"] = 0.0
    else:
        table["co_quant"] = float(np.max(np.asarray(co_size)) / 65535.0 * 2.0)
    return True


# ----------------------------------------------------------------------------
# Plain-Ctrl+J absorption.
#
# A plain Blender join keeps every source mesh's loops as ONE contiguous
# block, merges same-named attributes (missing layers zero-filled) and
# keeps the ACTIVE object's idprops.  So after a plain Ctrl+J:
#   - the table of every merged-in container survives in the color layers
#     as a "window" at its own loop offset (found by magic scan + CRC);
#   - agr_link_id/agr_link_co/agr_link_orig survive with correct values in
#     each block (foreign faces get id=0 / orig=False by zero-fill).
# Absorption merges those window tables into one canonical container, so
# containers survive ANY join - the AGR button is no longer mandatory.
# ----------------------------------------------------------------------------

def _window_matches_table(win_tbl, table):
    """Same instance set (name + face count) — identifies the container's
    OWN mirror window among the scanned ones (robust even if json key
    order or precise_* payloads differ)."""
    def sig(t):
        return sorted((str(i.get("name", "")), int(i.get("faces", 0) or 0))
                      for i in t.get("instances", {}).values())
    return sig(win_tbl) == sig(table)


def _compose_merged(idprop_tbl, windows, zero_faces=0, container_name="", mesh_name=""):
    """Pure merge of the idprop table with FOREIGN table windows (no mesh
    access, no mutation of the inputs).  Base = the idprop table (its own
    mirror window is dropped), or the first window when there is no idprop
    (the ex-active was a plain object or a fresh import).  Every other
    window's instances are appended under fresh ids with matrix_stale=True
    (their matrix_rel is relative to the OLD container; the reconcile step
    refits it).  With zero_faces > 0 and no idprop, the untracked ex-active
    geometry becomes a regular instance named after the container.
    Returns (merged, extras, zero_iid) where extras = [(loop_start,
    loop_count, id_map)] — deterministic, so the poll/draw VIEW and the
    materialisation produce identical ids."""
    windows = list(windows)
    if idprop_tbl is not None:
        base_src = idprop_tbl
        for i, (_s, _c, wt) in enumerate(windows):
            if _window_matches_table(wt, idprop_tbl):
                windows.pop(i)  # the container's own mirror
                break
    else:
        if not windows:
            return None, [], None
        base_src = {k: v for k, v in windows.pop(0)[2].items()
                    if not k.startswith("precise_")}
    merged = json.loads(json.dumps(base_src))
    merged.setdefault("groups", {})
    merged.setdefault("instances", {})
    merged.setdefault("next_instance", 1)
    merged.setdefault("next_group", 1)

    extras = []
    for s, cnt, wt in windows:
        group_map = {}
        for gid_str, ginfo in wt.get("groups", {}).items():
            group_map[int(gid_str)] = _match_or_add_group(merged, ginfo)
        id_map = {}
        for iid_str, inst in wt.get("instances", {}).items():
            nid = merged["next_instance"]
            merged["next_instance"] += 1
            entry = dict(inst)
            entry["group"] = group_map.get(entry.get("group", 0), 0)
            entry["matrix_stale"] = True
            merged["instances"][str(nid)] = entry
            id_map[int(iid_str)] = nid
        q = float(wt.get("co_quant", 0.0) or 0.0)
        if q > float(merged.get("co_quant", 0.0) or 0.0):
            merged["co_quant"] = q
        extras.append((s, cnt, id_map))

    zero_iid = None
    if idprop_tbl is None and zero_faces > 0:
        zero_iid = _add_zero_instance(merged, zero_faces, container_name, mesh_name)
    return merged, extras, zero_iid


def _add_zero_instance(merged, zero_faces, container_name, mesh_name):
    """Register the untracked (id=0) geometry as a regular instance of the
    container itself: identity rel-matrix, its own fresh group (never
    matched into an existing one — the concatenated geometry is not an
    instance of anything).  Keeps the ex-active object recoverable."""
    gid = merged["next_group"]
    merged["next_group"] += 1
    merged["groups"][str(gid)] = {"data_name": mesh_name or container_name,
                                  "verts": 0, "faces": int(zero_faces)}
    zero_iid = merged["next_instance"]
    merged["next_instance"] += 1
    merged["instances"][str(zero_iid)] = {
        "name": container_name,
        "matrix_rel": _matrix_to_list(Matrix.Identity(4)),
        "faces": int(zero_faces),
        "collections": [],
        "materials": [],
        "parent": None,
        "parent_type": 'OBJECT',
        "parent_bone": "",
        "parent_vertices": [],
        "matrix_parent_inverse": _matrix_to_list(Matrix.Identity(4)),
        "props": {},
        "group": gid,
    }
    return zero_iid


def _merged_view(obj):
    """(table, extra_windows_count) WITHOUT mutating anything.  When the
    mesh carries foreign table windows the returned table is a VIRTUAL
    merge — operators must run _reconcile_container() before stamping or
    writing (it materialises exactly the same ids)."""
    idp = _parse_table(obj.get(PROP_KEY))
    data = getattr(obj, "data", None)
    if getattr(obj, "type", None) != 'MESH' or data is None:
        return idp, 0
    if data.attributes.get(TABLE_COL_PREFIX + "0") is None:
        return idp, 0
    # canonical container: exactly one magic hit = its own mirror.  The
    # cheap first-layer count avoids decompressing the whole (multi-MB
    # with precise_*) blob from poll()/draw() just to conclude "nothing
    # to absorb".
    if idp is not None and _LINK_STORE.count_window_candidates(data) <= 1:
        return idp, 0
    windows = _LINK_STORE.scan_windows(data)
    if not windows:
        return idp, 0
    if idp is not None:
        own = any(_window_matches_table(wt, idp) for _s, _c, wt in windows)
        if len(windows) - (1 if own else 0) <= 0:
            return idp, 0
    zero = 0
    if idp is None:
        ids = _read_face_ids(data)
        if ids is not None:
            zero = int((ids == 0).sum())
        else:  # FBX path: no attrs yet - estimate from the window metadata
            total = sum(int(i.get("faces", 0) or 0)
                        for _s, _c, wt in windows
                        for i in wt.get("instances", {}).values())
            zero = max(0, len(data.polygons) - total)
    merged, extras, _z = _compose_merged(idp, windows, zero, obj.name, data.name)
    if merged is None:
        return idp, 0
    return merged, len(extras)


# poll/draw cache for the merged view: obj.name -> (fingerprint, result)
_MERGED_CACHE = {}


def _peek_merged(obj):
    """Cached, poll()/draw()-safe merged view — see _merged_view."""
    raw = obj.get(PROP_KEY)
    raw_key = raw if isinstance(raw, str) else None
    data = getattr(obj, "data", None)
    if getattr(obj, "type", None) != 'MESH' or data is None:
        return (_parse_table(raw_key), 0)
    if raw_key is None and data.attributes.get(TABLE_COL_PREFIX + "0") is None:
        return (None, 0)  # plain mesh - answer without polluting the cache
    fp = (raw_key, data.name, len(data.loops))
    hit = _MERGED_CACHE.get(obj.name)
    if hit is not None and hit[0] == fp:
        return hit[1]
    result = _merged_view(obj)
    _MERGED_CACHE[obj.name] = (fp, result)
    return result


def _unpack_tracking_windows(mesh, windows, merged):
    """FBX path of the plain-Ctrl+J absorb: rebuild the internal tracking
    attributes when the color mirror holds SEVERAL table windows.  Each
    window's vertices are denormalised with ITS OWN co_min/co_size and
    bit-exact precise_* records; verts outside every window (plain objects
    merged without memory) keep their CURRENT coords with orig=False —
    the zero-instance step stamps them afterwards.  Updates
    merged["co_quant"] with the worst window quantum.  Returns True when
    the mirror was usable."""
    col_co = mesh.attributes.get(COL_CO)
    col_id = mesh.attributes.get(COL_ID)
    if col_co is None or col_id is None:
        return False
    n_verts = len(mesh.vertices)
    n_loops = len(mesh.loops)
    n_polys = len(mesh.polygons)
    if n_verts == 0 or n_loops == 0:
        return False
    vidx = np.zeros(n_loops, dtype=np.intc)
    mesh.loops.foreach_get("vertex_index", vidx)
    loop_start = np.zeros(n_polys, dtype=np.intc)
    mesh.polygons.foreach_get("loop_start", loop_start)

    def per_vertex_bytes(attr_):
        b = _read_srgb_bytes(attr_).reshape(-1, 4).astype(np.uint32)
        if attr_.domain == 'CORNER' and len(b) == n_loops:
            out = np.zeros((n_verts, 4), dtype=np.uint32)
            out[vidx] = b
            return out, b
        if attr_.domain == 'POINT' and len(b) == n_verts:
            return b, b[vidx]
        return None, None

    b_co_v, _b_co_l = per_vertex_bytes(col_co)
    b_id_v, b_id_l = per_vertex_bytes(col_id)
    if b_co_v is None or b_id_v is None:
        return False

    v16 = np.empty((n_verts, 3), dtype=np.int64)
    v16[:, 0] = b_co_v[:, 0] * 256 + b_co_v[:, 1]
    v16[:, 1] = b_co_v[:, 2] * 256 + b_co_v[:, 3]
    v16[:, 2] = b_id_v[:, 0] * 256 + b_id_v[:, 1]
    flags = b_id_v[:, 2] >= 128

    id_hi = b_id_l[loop_start, 2]
    id_lo = b_id_l[loop_start, 3]
    face_ids = ((id_hi & 127) * 256 + id_lo).astype(np.intc)

    # default: untracked current geometry (verts outside every window)
    cur = np.zeros(n_verts * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", cur)
    co = cur.reshape(-1, 3).astype(np.float64)
    quant_worst = float(merged.get("co_quant", 0.0) or 0.0)

    for s, cnt, wtbl in windows:
        w_verts = np.unique(vidx[s:s + cnt])
        if len(w_verts) == 0:
            continue
        co_min = np.asarray(wtbl.get("co_min", [0.0, 0.0, 0.0]), dtype=np.float64)
        co_size = np.asarray(wtbl.get("co_size", [1.0, 1.0, 1.0]), dtype=np.float64)
        co[w_verts] = v16[w_verts].astype(np.float64) / 65535.0 * co_size + co_min
        full_precision = False
        if "precise_co" in wtbl and "precise_n" in wtbl:
            try:
                raw = base64.b64decode(wtbl["precise_co"])
                precise = np.frombuffer(raw, dtype="<f4")
            except (ValueError, TypeError):
                precise = None
            if precise is not None and len(precise) == int(wtbl["precise_n"]) * 3:
                precise = precise.reshape(-1, 3)
                k16 = np.clip(np.rint((precise.astype(np.float64) - co_min) / co_size * 65535.0),
                              0, 65535).astype(np.int64)
                keys_pack = (k16[:, 0] << 32) | (k16[:, 1] << 16) | k16[:, 2]
                keys_mesh = (v16[w_verts, 0] << 32) | (v16[w_verts, 1] << 16) | v16[w_verts, 2]
                order = np.argsort(keys_pack, kind="stable")
                sorted_keys = keys_pack[order]
                pos = np.clip(np.searchsorted(sorted_keys, keys_mesh), 0, len(sorted_keys) - 1)
                hit = sorted_keys[pos] == keys_mesh
                co[w_verts[hit]] = precise[order[pos[hit]]]
                full_precision = bool(hit.all())
        if not full_precision:
            quant_worst = max(quant_worst, float(np.max(co_size) / 65535.0 * 2.0))

    attr = _ensure_attr(mesh)
    attr.data.foreach_set("value", face_ids)
    co_attr = mesh.attributes.get(CO_ATTR)
    if co_attr is not None and (co_attr.domain != 'POINT' or co_attr.data_type != 'FLOAT_VECTOR'):
        mesh.attributes.remove(co_attr)
        co_attr = None
    if co_attr is None:
        co_attr = mesh.attributes.new(CO_ATTR, 'FLOAT_VECTOR', 'POINT')
    co_attr.data.foreach_set("vector", co.astype(np.float32).ravel())
    flag_attr = mesh.attributes.get(ORIG_ATTR)
    if flag_attr is not None and (flag_attr.domain != 'POINT' or flag_attr.data_type != 'BOOLEAN'):
        mesh.attributes.remove(flag_attr)
        flag_attr = None
    if flag_attr is None:
        flag_attr = mesh.attributes.new(ORIG_ATTR, 'BOOLEAN', 'POINT')
    flag_attr.data.foreach_set("value", flags)
    merged["co_quant"] = quant_worst
    return True


def _reconcile_container(context, obj):
    """Absorb containers merged in by a PLAIN Blender Ctrl+J: merge all
    foreign window tables into the idprop table (id remap per loop
    segment), register the untracked ex-active geometry as a regular
    instance, refit matrix_rel of absorbed instances against the new
    container, and repack ONE fresh color mirror.  Idempotent — after the
    repack no foreign window remains.  Returns a stats dict, or None when
    there was nothing to absorb.  Mutates mesh/idprop: operators only,
    never from poll()/draw()."""
    if obj is None or getattr(obj, "type", None) != 'MESH':
        return None
    mesh = obj.data
    if mesh.attributes.get(TABLE_COL_PREFIX + "0") is None:
        return None
    idp = _parse_table(obj.get(PROP_KEY))
    windows = _LINK_STORE.scan_windows(mesh)
    if not windows:
        return None
    if idp is not None:
        own = any(_window_matches_table(wt, idp) for _s, _c, wt in windows)
        if len(windows) - (1 if own else 0) <= 0:
            return None  # canonical container - its own mirror only
    elif len(windows) == 1 and windows[0][0] == 0:
        # single window from loop 0: classic fresh FBX import UNLESS the
        # mesh has an untracked tail (plain objects joined in afterwards)
        total = sum(int(i.get("faces", 0) or 0)
                    for i in windows[0][2].get("instances", {}).values())
        if total >= len(mesh.polygons):
            return None  # existing fresh-import path handles it

    # Alt+D twin shares this datablock - never mutate the shared copy.
    # The zero-instance group must carry the ORIGINAL datablock name (the
    # copy gets a ".001" suffix, and the panel view already showed the
    # original - diverging names would split link groups on later joins).
    orig_mesh_name = mesh.name
    real_users = mesh.users - (1 if mesh.use_fake_user else 0)
    if real_users > 1:
        mesh = mesh.copy()
        obj.data = mesh

    quant_unpacked = None
    if mesh.attributes.get(ATTR_NAME) is None:
        stub = {"co_quant": float((idp or {}).get("co_quant", 0.0) or 0.0)}
        if not _unpack_tracking_windows(mesh, windows, stub):
            return None  # no attrs and no usable mirror - cannot absorb
        quant_unpacked = stub["co_quant"]

    face_ids = _read_face_ids(mesh)
    if face_ids is None:
        return None
    zero_pre = int((face_ids == 0).sum()) if idp is None else 0

    merged, extras, zero_iid = _compose_merged(idp, windows, zero_pre, obj.name, orig_mesh_name)
    if merged is None:
        return None
    if quant_unpacked is not None and quant_unpacked > float(merged.get("co_quant", 0.0) or 0.0):
        merged["co_quant"] = quant_unpacked

    for s, cnt, id_map in extras:
        _stamp_remap(mesh, id_map, loop_range=(s, s + cnt))

    n_loops = len(mesh.loops)
    n_polys = len(mesh.polygons)
    vidx = np.zeros(n_loops, dtype=np.intc)
    mesh.loops.foreach_get("vertex_index", vidx)
    loop_total = np.zeros(n_polys, dtype=np.intc)
    mesh.polygons.foreach_get("loop_total", loop_total)
    face_ids = _read_face_ids(mesh)

    # untracked geometry -> instance of the container itself (idprop absent
    # means the ex-active carried no memory; with an idprop the untracked
    # faces keep today's foreign semantics)
    zero_instance = False
    if idp is None:
        # ids OUTSIDE every window belong to no readable table (e.g. a
        # merged-in container whose mirror was never written or died) -
        # they would collide with the fresh merged numbering, so they are
        # folded into the zero-instance instead of scrambling extraction
        loop_start_arr = np.zeros(n_polys, dtype=np.intc)
        mesh.polygons.foreach_get("loop_start", loop_start_arr)
        in_win = np.zeros(n_polys, dtype=bool)
        for w_s, w_cnt, _wt in windows:
            in_win |= (loop_start_arr >= w_s) & (loop_start_arr < w_s + w_cnt)
        stray = (~in_win) & (face_ids != 0)
        if stray.any():
            face_ids = np.where(stray, 0, face_ids).astype(np.intc)
            _ensure_attr(mesh).data.foreach_set("value", face_ids)
        zero_mask = face_ids == 0
        zero_fact = int(zero_mask.sum())
        if zero_fact:
            if zero_iid is None:
                zero_iid = _add_zero_instance(merged, zero_fact, obj.name, orig_mesh_name)
            entry = merged["instances"][str(zero_iid)]
            entry["faces"] = zero_fact
            entry["collections"] = _capture_collections(obj, context)
            entry["materials"] = [ms.material.name if ms.material else ""
                                  for ms in obj.material_slots]
            entry["parent"] = obj.parent.name if obj.parent else None
            entry["parent_type"] = obj.parent_type
            entry["parent_bone"] = obj.parent_bone
            entry["parent_vertices"] = (list(obj.parent_vertices)
                                        if obj.parent_type in {'VERTEX', 'VERTEX_3'} else [])
            entry["matrix_parent_inverse"] = _matrix_to_list(obj.matrix_parent_inverse)
            entry["props"] = _capture_props(obj)
            zero_loops = np.repeat(zero_mask, loop_total)
            zero_verts = np.unique(vidx[zero_loops])
            merged["groups"][str(entry["group"])]["verts"] = int(len(zero_verts))
            face_ids = np.where(zero_mask, zero_iid, face_ids).astype(np.intc)
            _ensure_attr(mesh).data.foreach_set("value", face_ids)
            # their original local coords ARE the current ones (the active
            # object is never transformed by a join)
            flag_attr = mesh.attributes.get(ORIG_ATTR)
            co_attr = mesh.attributes.get(CO_ATTR)
            if flag_attr is not None and co_attr is not None:
                n_verts = len(mesh.vertices)
                fl = np.zeros(n_verts, dtype=bool)
                flag_attr.data.foreach_get("value", fl)
                need = np.zeros(n_verts, dtype=bool)
                need[zero_verts] = True
                need &= ~fl
                if need.any():
                    cur = np.zeros(n_verts * 3, dtype=np.float32)
                    mesh.vertices.foreach_get("co", cur)
                    stored = np.zeros(n_verts * 3, dtype=np.float32)
                    co_attr.data.foreach_get("vector", stored)
                    stored.reshape(-1, 3)[need] = cur.reshape(-1, 3)[need]
                    co_attr.data.foreach_set("vector", stored)
                    flag_attr.data.foreach_set("value", fl | need)
            zero_instance = True

    # refit matrix_rel of absorbed instances against THIS container (the
    # stored one is relative to the OLD container and would fling pieces)
    stale = 0
    if extras:
        co_attr = mesh.attributes.get(CO_ATTR)
        flag_attr = mesh.attributes.get(ORIG_ATTR)
        if co_attr is not None and flag_attr is not None:
            n_verts = len(mesh.vertices)
            p32 = np.zeros(n_verts * 3, dtype=np.float32)
            co_attr.data.foreach_get("vector", p32)
            p = p32.reshape(-1, 3).astype(np.float64)
            om = np.zeros(n_verts, dtype=bool)
            flag_attr.data.foreach_get("value", om)
            q32 = np.zeros(n_verts * 3, dtype=np.float32)
            mesh.vertices.foreach_get("co", q32)
            q = q32.reshape(-1, 3).astype(np.float64)
            ids_per_loop = np.repeat(face_ids, loop_total)
            extra_tol = float(merged.get("co_quant", 0.0) or 0.0)
            for _s, _c, id_map in extras:
                for nid in id_map.values():
                    vmask = np.zeros(n_verts, dtype=bool)
                    vmask[vidx[ids_per_loop == nid]] = True
                    vmask &= om
                    core = _fit_affine_core(p, q, vmask, extra_tol)
                    entry = merged["instances"][str(nid)]
                    if core is not None:
                        a, t, _res, _tol = core
                        entry["matrix_rel"] = _matrix_to_list(_affine_to_matrix(a, t))
                        entry.pop("matrix_stale", None)
                    else:
                        stale += 1
        else:
            stale = sum(len(m) for _s, _c, m in extras)

    # idprop FIRST (the table must survive even if the repack fails), then
    # ONE fresh mirror over the whole mesh - absorption is now permanent
    write_table(obj, merged)
    try:
        mirror_ok = _pack_tracking_to_colors(mesh, merged)
    except Exception:
        _remove_color_mirror(mesh)
        mirror_ok = False
    if mirror_ok:
        write_table(obj, merged)
    _TABLE_CACHE.pop(obj.name, None)
    _MERGED_CACHE.pop(obj.name, None)
    return {"absorbed": len(extras), "zero_instance": zero_instance,
            "stale": stale, "mirror_ok": mirror_ok}


# ----------------------------------------------------------------------------
# Join
# ----------------------------------------------------------------------------

def _rollback_join(mutated):
    """Undo pre-join mutations after a failed join: restore replaced
    datablocks and stamped attribute values."""
    for obj, orig_data, saved_ids in reversed(mutated):
        try:
            if orig_data is not None:
                copy = obj.data
                obj.data = orig_data
                if copy.users == 0:
                    bpy.data.meshes.remove(copy)
            elif saved_ids is not None:
                attr = obj.data.attributes.get(ATTR_NAME)
                if attr is not None:
                    attr.data.foreach_set("value", saved_ids)
            else:
                _remove_tracking_attrs(obj.data)
        except (ReferenceError, RuntimeError):
            pass


class AGR_OT_link_join(Operator):
    """Заджоинить выбранные меши в один объект с памятью линков:
контейнер можно в любой момент разобрать обратно на исходные
линкованные объекты (панель AGR Link). Клик по ОДНОМУ контейнеру
обновляет его память: поглощает обычные Ctrl+J и перепаковывает
зеркало (закрепление перед правками/передачей в другой пакет)"""
    bl_idname = "agr.link_join"
    bl_label = "Джоин с памятью линков"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'OBJECT'
                and context.active_object is not None
                and context.active_object.type == 'MESH')

    def execute(self, context):
        active = context.active_object
        participants = [o for o in context.selected_objects if o.type == 'MESH']
        skipped = [o for o in context.selected_objects if o.type != 'MESH']

        if active not in participants:
            agr_report(self, 'ERROR', "❌ AGR Link: активный объект должен быть выделенным мешем")
            return {'CANCELLED'}
        if len(participants) < 2:
            # single CONTAINER: refresh its memory instead of blocking -
            # absorb plain-Ctrl+J windows and repack a fresh mirror
            if (len(participants) == 1 and participants[0] == active
                    and read_table(active) is not None):
                return self._refresh_single(context, active)
            agr_report(self, 'ERROR', "❌ AGR Link: выделите минимум 2 меш-объекта")
            return {'CANCELLED'}

        # --- blockers (all BEFORE any mutation) -----------------------------
        from_library = [o.name for o in participants
                        if o.library is not None or o.data.library is not None]
        if from_library:
            names = ", ".join(from_library[:5]) + ("…" if len(from_library) > 5 else "")
            agr_report(self, 'ERROR',
                       f"❌ AGR Link: объекты из линкованной библиотеки нельзя джоинить: {names}")
            return {'CANCELLED'}

        with_modifiers = [o.name for o in participants if o.modifiers]
        if with_modifiers:
            names = ", ".join(with_modifiers[:5]) + ("…" if len(with_modifiers) > 5 else "")
            agr_report(self, 'ERROR',
                       f"❌ AGR Link: у объектов есть модификаторы (пропадут при джоине) — "
                       f"примените или удалите их: {names}")
            return {'CANCELLED'}
        with_keys = [o.name for o in participants if o.data.shape_keys]
        if with_keys:
            names = ", ".join(with_keys[:5]) + ("…" if len(with_keys) > 5 else "")
            agr_report(self, 'ERROR', f"❌ AGR Link: у объектов есть shape keys: {names}")
            return {'CANCELLED'}

        # Zero-scale matrices are not invertible - the restore math needs M⁻¹
        singular = [o.name for o in participants
                    if abs(o.matrix_world.determinant()) < 1e-12]
        if singular:
            names = ", ".join(singular[:5]) + ("…" if len(singular) > 5 else "")
            agr_report(self, 'ERROR',
                       f"❌ AGR Link: нулевой масштаб (матрица необратима), исправьте scale: {names}")
            return {'CANCELLED'}

        if not bpy.ops.object.join.poll():
            agr_report(self, 'ERROR', "❌ AGR Link: join невозможен в текущем контексте")
            return {'CANCELLED'}

        # Zero-face participants cannot be tracked by a face attribute
        no_faces = [o for o in participants if len(o.data.polygons) == 0]
        if no_faces:
            for o in no_faces:
                o.select_set(False)
            participants = [o for o in participants if o not in no_faces]
            agr_report(self, 'WARNING',
                       f"⚠️ AGR Link: без граней, исключены из джоина: "
                       + ", ".join(o.name for o in no_faces[:5]))
            if active not in participants or len(participants) < 2:
                agr_report(self, 'ERROR', "❌ AGR Link: после исключения объектов джоинить нечего")
                return {'CANCELLED'}

        loose = [o.name for o in participants if _has_loose_geometry(o.data)]
        if loose:
            agr_report(self, 'WARNING',
                       "⚠️ AGR Link: свободные вершины/рёбра не отслеживаются атрибутом и при "
                       "разборке останутся в контейнере: " + ", ".join(loose[:5]))

        # join unions UV layers BY NAME and zero-fills the missing ones -
        # mismatched layer names silently break the 1-UV delivery rule
        uv_sets = {tuple(sorted(l.name for l in o.data.uv_layers)) for o in participants}
        if len(uv_sets) > 1:
            agr_report(self, 'WARNING',
                       "⚠️ AGR Link: у объектов РАЗНЫЕ наборы UV-слоёв — join объединит их в "
                       "несколько каналов, грани без слоя получат нулевые UV")

        # plain-Ctrl+J leftovers: absorb foreign table windows first, so every
        # participant is a canonical container before the tables are merged
        # (a standalone improvement - deliberately NOT rolled back on failure)
        absorbed_tables = 0
        for obj in participants:
            recon = _reconcile_container(context, obj)
            if recon:
                absorbed_tables += recon.get("absorbed", 0)

        a_mat = active.matrix_world.copy()
        inv_active = a_mat.inverted()

        # --- build the merged table (pure computation, no scene mutation yet)
        active_table = read_table(active)
        if active_table is not None:
            table = active_table  # own ids stay valid, matrices stay verbatim
            # a freshly FBX-imported container has no internal attributes yet -
            # rebuild them from the color mirror before anything is stamped;
            # stamping over MISSING attributes would zero-fill every face id
            if active.data.attributes.get(ATTR_NAME) is None:
                if not _unpack_tracking_from_colors(active.data, table):
                    agr_report(self, 'ERROR',
                               "❌ AGR Link: у контейнера нет разметки (ни атрибутов, ни "
                               "цветового зеркала) — джоин отменён, разметка была бы потеряна")
                    return {'CANCELLED'}
        else:
            table = _new_table()

        others = [o for o in participants if o != active]

        # (obj, iid, needs_fill, id_map) - stamping plan, applied after all
        # entries are computed
        stamp_plan = []
        # Same live datablock => same link group (this is what "linked" means)
        plain_group_by_data = {}

        if active_table is None:
            # active itself becomes instance #1 with identity rel-matrix
            ginfo = {"data_name": active.data.name,
                     "verts": len(active.data.vertices),
                     "faces": len(active.data.polygons)}
            gid = _match_or_add_group(table, ginfo)
            plain_group_by_data[active.data] = gid
            iid = table["next_instance"]
            table["next_instance"] += 1
            entry = _capture_instance(active, inv_active, context)
            entry["group"] = gid
            table["instances"][str(iid)] = entry
            stamp_plan.append((active, iid, True, None))

        merged_containers = 0
        for obj in others:
            sub = read_table(obj)
            if sub is not None:
                if obj.data.attributes.get(ATTR_NAME) is None:
                    # freshly imported container joined without a prior
                    # disassembly - restore its attributes from colors first
                    if not _unpack_tracking_from_colors(obj.data, sub):
                        agr_report(self, 'ERROR',
                                   f"❌ AGR Link: у контейнера '{obj.name}' нет разметки — "
                                   f"джоин отменён, его геометрия стала бы неразборной")
                        return {'CANCELLED'}
                # the merged-in container's quantisation budget must survive
                # the merge or its copies won't re-link at disassembly
                sub_quant = float(sub.get("co_quant", 0.0) or 0.0)
                if sub_quant > float(table.get("co_quant", 0.0) or 0.0):
                    table["co_quant"] = sub_quant
                # merge a nested container: one matrix conversion for all entries
                conv = inv_active @ obj.matrix_world
                group_map = {}
                for gid_str, ginfo in sub.get("groups", {}).items():
                    group_map[int(gid_str)] = _match_or_add_group(table, ginfo)
                id_map = {}
                for iid_str, inst in sub.get("instances", {}).items():
                    nid = table["next_instance"]
                    table["next_instance"] += 1
                    inst = dict(inst)
                    inst["group"] = group_map.get(inst.get("group", 0), 0)
                    inst["matrix_rel"] = _matrix_to_list(conv @ Matrix(inst["matrix_rel"]))
                    table["instances"][str(nid)] = inst
                    id_map[int(iid_str)] = nid
                stamp_plan.append((obj, None, False, id_map))
                merged_containers += 1
            else:
                data = obj.data
                gid = plain_group_by_data.get(data)
                if gid is None:
                    ginfo = {"data_name": data.name,
                             "verts": len(data.vertices),
                             "faces": len(data.polygons)}
                    gid = _match_or_add_group(table, ginfo)
                    plain_group_by_data[data] = gid
                iid = table["next_instance"]
                table["next_instance"] += 1
                entry = _capture_instance(obj, inv_active, context)
                entry["group"] = gid
                table["instances"][str(iid)] = entry
                stamp_plan.append((obj, iid, True, None))

        # --- mutations: single-user data, then stamp the face attribute.
        # Linked copies share one mesh datablock, so each participant must own
        # its data before per-object ids can be written into it.  The join
        # bakes geometry anyway, so nothing is lost by the copy.  Every
        # mutation is recorded so a failed join can roll back.
        mutated = []  # (obj, replaced_original_data | None, saved_attr_ids | None)
        try:
            for obj, iid, fill, id_map in stamp_plan:
                orig_data = None
                saved_ids = None
                if obj.data.users > 1:
                    orig_data = obj.data
                    obj.data = obj.data.copy()
                elif id_map:
                    saved_ids = _read_attr_values(obj.data)
                mutated.append((obj, orig_data, saved_ids))
                if fill:
                    _stamp_fill(obj.data, iid)
                    _stamp_original_coords(obj.data)
                elif id_map:
                    _stamp_remap(obj.data, id_map)
            # Active container with no id conflicts: attribute is already correct.
            if active.data.users > 1:
                orig_data = active.data
                active.data = active.data.copy()
                mutated.append((active, orig_data, None))
        except Exception as exc:
            _rollback_join(mutated)
            agr_report(self, 'ERROR', f"❌ AGR Link: сбой подготовки, изменения откачены: {exc}")
            return {'CANCELLED'}

        # --- join
        for o in skipped:
            o.select_set(False)
        context.view_layer.objects.active = active
        try:
            ret = bpy.ops.object.join()
        except RuntimeError as exc:
            _rollback_join(mutated)
            agr_report(self, 'ERROR', f"❌ AGR Link: join не удался, изменения откачены: {exc}")
            return {'CANCELLED'}
        if 'FINISHED' not in ret:
            _rollback_join(mutated)
            agr_report(self, 'ERROR', "❌ AGR Link: join не выполнился, изменения откачены")
            return {'CANCELLED'}

        # idprop FIRST: even if the color mirror fails, the table must exist -
        # a container with stamped geometry and no table is unrecoverable
        write_table(active, table)
        try:
            mirror_ok = _pack_tracking_to_colors(active.data, table)
        except Exception:
            _remove_color_mirror(active.data)
            mirror_ok = False
        if mirror_ok:
            write_table(active, table)  # now includes the co_min/co_size bounds
        else:
            agr_report(self, 'WARNING',
                       "⚠️ AGR Link: цветовое зеркало не записано — контейнер не переживёт "
                       "FBX-перенос (в .blend разборка работает)")

        n_inst = len(table["instances"])
        n_groups = len({inst["group"] for inst in table["instances"].values()})
        msg = (f"✅ AGR Link: заджоинено {len(participants)} объектов → '{active.name}' "
               f"(в памяти {n_inst} объектов, {n_groups} групп)")
        if merged_containers:
            msg += f", влито контейнеров: {merged_containers}"
        if absorbed_tables:
            msg += f", поглощено таблиц обычного Ctrl+J: {absorbed_tables}"
        agr_report(self, 'INFO', msg)
        return {'FINISHED'}

    def _refresh_single(self, context, active):
        """Single selected container: "закрепить память" — absorb any
        plain-Ctrl+J windows (materialise the merged table exactly as the
        panel shows it) and repack ONE fresh mirror over the current mesh.
        After this the container is canonical: safe to edit topology and
        safe to hand to another DCC."""
        if active.library is not None or active.data.library is not None:
            agr_report(self, 'ERROR',
                       "❌ AGR Link: объект из линкованной библиотеки нельзя изменять")
            return {'CANCELLED'}

        recon = _reconcile_container(context, active)
        table = read_table(active)
        if table is None:
            agr_report(self, 'ERROR',
                       "❌ AGR Link: таблица контейнера не читается (данные повреждены)")
            return {'CANCELLED'}

        if recon is not None:
            mirror_ok = recon.get("mirror_ok", True)
        else:
            # nothing to absorb - still refresh idprop + mirror so the
            # memory matches the CURRENT mesh exactly (e.g. after edits or
            # a fresh FBX import that was never materialised)
            has_attrs = active.data.attributes.get(ATTR_NAME) is not None
            if not has_attrs:
                has_attrs = _unpack_tracking_from_colors(active.data, table)
            if not has_attrs:
                # inherited broken state (idprop without attrs or a usable
                # mirror) - repacking would destroy the surviving mirror
                agr_report(self, 'WARNING',
                           "⚠️ AGR Link: у контейнера нет разметки для перепаковки — "
                           "память оставлена как есть")
                return {'FINISHED'}
            write_table(active, table)
            try:
                mirror_ok = _pack_tracking_to_colors(active.data, table)
            except Exception:
                _remove_color_mirror(active.data)
                mirror_ok = False
            if mirror_ok:
                write_table(active, table)
            _TABLE_CACHE.pop(active.name, None)
            _MERGED_CACHE.pop(active.name, None)

        n_inst = len(table.get("instances", {}))
        n_groups = len({inst.get("group", 0) for inst in table.get("instances", {}).values()})
        msg = (f"✅ AGR Link: память '{active.name}' обновлена "
               f"({n_inst} объектов, {n_groups} групп)")
        if recon and recon.get("absorbed"):
            msg += f", поглощено таблиц обычного Ctrl+J: {recon['absorbed']}"
        if recon and recon.get("zero_instance"):
            msg += ", непомеченная геометрия оформлена объектом"
        level = 'INFO'
        if not mirror_ok:
            msg += " | ⚠️ цветовое зеркало не записано (FBX-перенос недоступен)"
            level = 'WARNING'
        agr_report(self, level, msg)
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# Disassembly
# ----------------------------------------------------------------------------

def _prune_mesh_to_faces(mesh, keep_mask_fn, keep_preexisting_loose=False):
    """Delete every face for which keep_mask_fn(attr_value) is False, plus
    loose geometry ORPHANED by that deletion.  With keep_preexisting_loose,
    verts that were already loose before (untracked by the face attribute)
    survive - the container prune must honour the join-time promise that
    loose geometry stays in the container.  Returns (faces_left, verts_left)."""
    bm = bmesh.new()
    bm.from_mesh(mesh)
    layer = bm.faces.layers.int.get(ATTR_NAME)
    if layer is None:
        bm.free()
        return len(mesh.polygons), len(mesh.vertices)
    pre_loose = {v for v in bm.verts if not v.link_faces} if keep_preexisting_loose else set()
    doomed = [f for f in bm.faces if not keep_mask_fn(f[layer])]
    if doomed:
        bmesh.ops.delete(bm, geom=doomed, context='FACES')
    loose = [v for v in bm.verts if not v.link_faces and v not in pre_loose]
    if loose:
        bmesh.ops.delete(bm, geom=loose, context='VERTS')
    faces_left, verts_left = len(bm.faces), len(bm.verts)
    bm.to_mesh(mesh)
    bm.free()
    return faces_left, verts_left


def _restore_materials(mesh):
    """Compact the container's material slots down to the ones this chunk's
    faces actually use, keeping the container's slot order.  CONTAINER
    materials and UV win by design (user decision): re-assigning a shared
    UDIM material or re-unwrapping on the container must survive disassembly
    with linking intact, so the stored per-instance material names are kept
    only as table metadata and never forced back."""
    n = len(mesh.polygons)
    idx = np.zeros(n, dtype=np.intc)
    if n:
        mesh.polygons.foreach_get("material_index", idx)
    old_mats = list(mesh.materials)

    final = []
    remap = {}
    for oi in (sorted(set(idx.tolist())) if n else []):
        mat = old_mats[oi] if 0 <= oi < len(old_mats) else None
        if mat is None and not old_mats:
            # container has no materials at all - no phantom empty slot
            remap[oi] = 0
            continue
        pos = None
        for i, m in enumerate(final):  # collapse duplicate slots of one material
            if m is mat:
                pos = i
                break
        if pos is None:
            final.append(mat)
            pos = len(final) - 1
        remap[oi] = pos

    mesh.materials.clear()
    for mat in final:
        mesh.materials.append(mat)
    if n and remap:
        lut_size = max(remap.keys()) + 1
        lut = np.zeros(lut_size, dtype=np.intc)
        for oi, pos in remap.items():
            lut[oi] = pos
        safe_idx = np.where((idx < 0) | (idx >= lut_size), 0, idx)
        mesh.polygons.foreach_set("material_index", lut[safe_idx])


def _mesh_coords(mesh):
    arr = np.zeros(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", arr)
    return arr


def _mesh_mat_indices(mesh):
    arr = np.zeros(len(mesh.polygons), dtype=np.intc)
    if len(mesh.polygons):
        mesh.polygons.foreach_get("material_index", arr)
    return arr


def _mesh_poly_normals(mesh):
    # mesh.polygon_normals (unlike polygons.foreach_get("normal")) forces a
    # recompute of the lazy normal cache after transform()/flip_normals()
    arr = np.zeros(len(mesh.polygons) * 3, dtype=np.float32)
    if len(mesh.polygons):
        mesh.polygon_normals.foreach_get("vector", arr)
    return arr


def _geometry_matches(mesh_a, mesh_b, check_materials=True, extra_atol=0.0):
    """extra_atol absorbs float32 roundtrip noise that grows with the
    instance's CONTAINER-relative offset (city-scale scenes): the joined
    verts are stored as float32 at container magnitudes, so the M⁻¹ trip
    leaves ~2.4e-7 error per metre of offset regardless of mesh size."""
    if (len(mesh_a.vertices) != len(mesh_b.vertices)
            or len(mesh_a.polygons) != len(mesh_b.polygons)
            or len(mesh_a.loops) != len(mesh_b.loops)):
        return False
    ca, cb = _mesh_coords(mesh_a), _mesh_coords(mesh_b)
    scale = float(max(np.max(np.abs(ca), initial=1.0), 1.0))
    atol = max(1e-5, scale * 1e-5, extra_atol)
    if not np.allclose(ca, cb, atol=atol):
        return False
    # same coords but flipped winding is NOT the same mesh - a silent link
    # would discard the flip (matters for mirrored instances).  0.1 catches
    # orientation flips (delta ~2.0) while tolerating normal noise from
    # quantised coords on small faces - genuine edits are caught by coords
    if not np.allclose(_mesh_poly_normals(mesh_a), _mesh_poly_normals(mesh_b), atol=0.1):
        return False
    if check_materials:
        if not np.array_equal(_mesh_mat_indices(mesh_a), _mesh_mat_indices(mesh_b)):
            return False
        names_a = [m.name if m else "" for m in mesh_a.materials]
        names_b = [m.name if m else "" for m in mesh_b.materials]
        if names_a != names_b:
            return False
    return True


def _uv_matches(mesh_a, mesh_b, atol=1e-5):
    """Same UV layer names and values.  Used ONLY for adopting an alive
    datablock: re-attaching to a mesh with a different (old) unwrap would
    resurrect it; between chunks of one container UV is never compared -
    they link and the container's unwrap wins by design."""
    names_a = [l.name for l in mesh_a.uv_layers]
    names_b = [l.name for l in mesh_b.uv_layers]
    if names_a != names_b:
        return False
    for la, lb in zip(mesh_a.uv_layers, mesh_b.uv_layers):
        na, nb = len(la.data), len(lb.data)
        if na != nb:
            return False
        if na == 0:
            continue
        ua = np.zeros(na * 2, dtype=np.float32)
        la.data.foreach_get("uv", ua)
        ub = np.zeros(nb * 2, dtype=np.float32)
        lb.data.foreach_get("uv", ub)
        if not np.allclose(ua, ub, atol=atol):
            return False
    return True


def _link_to_collections(obj, names, context, fallback_collections):
    linked = False
    for name in names:
        coll = context.scene.collection if name == SCENE_ROOT else bpy.data.collections.get(name)
        if coll is not None:
            try:
                coll.objects.link(obj)
                linked = True
            except RuntimeError:
                pass  # already linked
    if not linked:
        for coll in fallback_collections:
            try:
                coll.objects.link(obj)
                linked = True
            except RuntimeError:
                pass
        if not linked:
            context.scene.collection.objects.link(obj)


def _extract_instances(op, context, container, target_ids):
    """Core disassembly: pull the given instance ids out of the container.
    Returns the list of created objects or None on error."""
    # blockers FIRST (read_table is a mutation-free view): a CANCELLED
    # outcome must not leave a reconcile mutation stranded outside undo
    if read_table(container) is None:
        agr_report(op, 'ERROR', "❌ AGR Link: активный объект — не контейнер AGR Link")
        return None
    if container.modifiers:
        agr_report(op, 'ERROR',
                   "❌ AGR Link: на контейнере есть модификаторы — примените или удалите "
                   "их перед разборкой")
        return None
    if container.data.shape_keys:
        agr_report(op, 'ERROR', "❌ AGR Link: на контейнере есть shape keys — разборка невозможна")
        return None
    # absorb plain-Ctrl+J leftovers: materialises the same ids the panel
    # showed (deterministic merge), so target_ids stay valid
    recon = _reconcile_container(context, container)
    table = read_table(container)
    if table is None:
        agr_report(op, 'ERROR', "❌ AGR Link: таблица контейнера не читается (данные повреждены)")
        return None

    mesh = container.data
    # A linked duplicate of the container (Alt+D) shares this datablock;
    # pruning it in place would silently empty the twin - mirror the join
    # path's single-user policy.
    real_users = mesh.users - (1 if mesh.use_fake_user else 0)
    if real_users > 1:
        mesh = mesh.copy()
        container.data = mesh

    # container came through FBX: generic attributes are gone, but the color
    # mirror survived - rebuild the internal attributes from it, and
    # materialise the idprop table when it was decoded from colors
    if mesh.attributes.get(ATTR_NAME) is None:
        _unpack_tracking_from_colors(mesh, table)
    if not isinstance(container.get(PROP_KEY), str):
        write_table(container, table)

    face_ids = _read_face_ids(mesh)
    if face_ids is None:
        agr_report(op, 'ERROR', f"❌ AGR Link: на контейнере нет атрибута {ATTR_NAME}")
        return None

    target_ids = {int(i) for i in target_ids if str(i) in table["instances"]}
    if not target_ids:
        agr_report(op, 'ERROR', "❌ AGR Link: нечего разбирать (экземпляры не найдены в таблице)")
        return None

    available = set(np.unique(face_ids).tolist())
    missing = sorted(target_ids - available)
    extract = sorted(target_ids & available)

    c_mat = container.matrix_world.copy()
    fallback_colls = list(container.users_collection)

    # Free the container's name so a restored instance with the same name
    # does not get a ".001" suffix while the soon-to-die husk still holds it.
    original_container_name = container.name
    container.name = original_container_name + ".__agr_link_tmp"
    container_deleted = False
    container_final_name = original_container_name

    mirror_failed = False
    try:
        created = []  # (obj, entry, matrix_rel, group_id)
        done_ids = []
        skipped_singular = 0
        skipped_stale = 0
        face_count_changed = 0
        for iid in extract:
            entry = table["instances"][str(iid)]
            m_rel = Matrix(entry["matrix_rel"])
            stored_faces = entry.get("faces")
            if stored_faces is not None:
                if int(np.count_nonzero(face_ids == iid)) != stored_faces:
                    face_count_changed += 1

            new_mesh = mesh.copy()
            _prune_mesh_to_faces(new_mesh, lambda v, _iid=iid: v == _iid)

            # Primary path: recover the frame from the stored per-vertex
            # original coords (robust to container Apply Transform / Set
            # Origin / whole-piece edit-mode moves / FBX matrix rebuilds).
            # Legacy containers fall back to the matrix path.
            frame = _solve_instance_frame(new_mesh,
                                          extra_tol=float(table.get("co_quant", 0.0)))
            _remove_tracking_attrs(new_mesh)
            if frame is None:
                if entry.get("matrix_stale"):
                    # absorbed from a plain Ctrl+J and the coordinate fit
                    # failed: the stored matrix belongs to the OLD container,
                    # transforming by it would fling the piece - keep the
                    # faces and the table entry instead
                    bpy.data.meshes.remove(new_mesh)
                    skipped_stale += 1
                    continue
                if abs(m_rel.determinant()) < 1e-12:
                    # non-invertible stored matrix and no coord attributes -
                    # keep the instance's faces and table entry
                    bpy.data.meshes.remove(new_mesh)
                    skipped_singular += 1
                    continue
                frame = m_rel
                # Self-inverse also for mirrored (negative determinant)
                # instances: join bakes M without flipping the winding, M⁻¹
                # restores the original orientation exactly (verified on 5.2)
                new_mesh.transform(m_rel.inverted())
            _restore_materials(new_mesh)

            obj = bpy.data.objects.new(entry["name"], new_mesh)
            _link_to_collections(obj, entry.get("collections", []), context, fallback_colls)
            for key, value in entry.get("props", {}).items():
                obj[key] = value
            created.append((obj, entry, frame, entry.get("group", 0)))
            done_ids.append(iid)

        # Second pass: parents.  Resolution order matters: (1) the batch
        # itself, by ORIGINAL entry name; (2) the surviving container when
        # the parent name is the container's real name (it is parked under
        # .__agr_link_tmp right now, so a bare name lookup would miss it);
        # (3) the scene by name.
        created_by_name = {}
        for obj, entry, m_rel, gid in created:
            created_by_name.setdefault(entry["name"], obj)
        container_survives = len(table["instances"]) > len(done_ids)
        for obj, entry, m_rel, gid in created:
            parent_name = entry.get("parent")
            if parent_name:
                parent = created_by_name.get(parent_name)
                if (parent is None and container_survives
                        and parent_name == original_container_name):
                    parent = container
                if parent is None:
                    parent = bpy.data.objects.get(parent_name)
                if parent is not None:
                    obj.parent = parent
                    obj.parent_type = entry.get("parent_type", 'OBJECT')
                    if obj.parent_type == 'BONE':
                        obj.parent_bone = entry.get("parent_bone", "")
                    elif obj.parent_type in {'VERTEX', 'VERTEX_3'}:
                        pv = entry.get("parent_vertices") or []
                        for i, v in enumerate(pv[:3]):
                            obj.parent_vertices[i] = v
                    obj.matrix_parent_inverse = Matrix(entry["matrix_parent_inverse"])

        # Third pass: world matrices, PARENTS FIRST.  matrix_world assignment
        # solves the local basis against the parent's CURRENT transform, so a
        # child placed before its in-batch parent would end up at
        # parent_world @ target (verified on 5.2).
        context.view_layer.update()
        unplaced = {obj: m_rel for obj, entry, m_rel, gid in created}
        while unplaced:
            progressed = False
            for obj in list(unplaced.keys()):
                parent = obj.parent
                if parent is not None and parent in unplaced:
                    continue
                obj.matrix_world = c_mat @ unplaced.pop(obj)
                progressed = True
            if not progressed:  # parent cycle - place the rest as-is
                for obj in list(unplaced.keys()):
                    obj.matrix_world = c_mat @ unplaced.pop(obj)

        # Fourth pass: re-link identical geometry of each group to one datablock
        unlinked_edited = 0
        by_group = {}
        for obj, entry, m_rel, gid in created:
            by_group.setdefault(gid, []).append((obj, m_rel))
        new_meshes = {obj.data for obj, *_ in created}
        for gid, members in by_group.items():
            ginfo = table["groups"].get(str(gid), {})
            member_objs = [o for o, _m in members]
            data_name = ginfo.get("data_name", member_objs[0].data.name)
            # float32 noise budget grows with container-relative offset;
            # after an FBX roundtrip the 16-bit quantisation step dominates
            extra_atol = max(max(m.translation.length for _o, m in members) * 5e-6,
                             float(table.get("co_quant", 0.0)))

            target = None
            alive = bpy.data.meshes.get(data_name)
            alive_ok = (alive is not None and alive is not mesh
                        and alive not in new_meshes)
            if alive_ok and alive.attributes.get(ATTR_NAME) is not None and alive.users > 0:
                # a LIVE container's mesh elsewhere (e.g. the twin after
                # Alt+D) - adopting it would strip its tracking attribute
                alive_ok = False
            # FULL test including materials AND UV: chunks carry the
            # CONTAINER's materials/unwrap, so an alive datablock with
            # different slots or an old unwrap must not be adopted (the
            # group then links onto itself instead)
            if alive_ok and _geometry_matches(member_objs[0].data, alive,
                                              check_materials=True,
                                              extra_atol=extra_atol) \
                    and _uv_matches(member_objs[0].data, alive):
                # copies of this group still live in the file - re-attach
                target = alive
                _remove_tracking_attrs(target)
            if target is None:
                target = member_objs[0].data
                target.name = data_name

            for member in member_objs:
                if member.data == target:
                    continue
                if _geometry_matches(member.data, target, extra_atol=extra_atol):
                    old = member.data
                    member.data = target
                    bpy.data.meshes.remove(old)
                else:
                    unlinked_edited += 1

        # --- shrink the container (keep untracked loose geometry, as the
        # join-time warning promises)
        done_set = set(done_ids)
        faces_left, verts_left = _prune_mesh_to_faces(
            mesh, lambda v: v not in done_set, keep_preexisting_loose=True)

        for iid in done_ids:
            table["instances"].pop(str(iid), None)
        used_groups = {inst.get("group", 0) for inst in table["instances"].values()}
        for gid in list(table["groups"].keys()):
            if int(gid) not in used_groups:
                del table["groups"][gid]

        container_renamed = False
        if table["instances"]:
            # idprop first, then refresh the color mirror (it went stale)
            write_table(container, table)
            try:
                mirror_failed = not _pack_tracking_to_colors(mesh, table)
            except Exception:
                _remove_color_mirror(mesh)
                mirror_failed = True
            if not mirror_failed:
                write_table(container, table)
            container.name = original_container_name
            container_final_name = container.name
            container_renamed = container.name != original_container_name
        else:
            if verts_left == 0:
                husk_mesh = container.data
                # hand the container's children over to the restored namesake
                # instance (or unparent), preserving world transforms - else
                # deleting the container would snap them to wrong positions
                replacement = created_by_name.get(original_container_name)
                for child in list(container.children):
                    world = child.matrix_world.copy()
                    child.parent = replacement
                    child.matrix_world = world
                bpy.data.objects.remove(container, do_unlink=True)
                if husk_mesh.users == 0:
                    bpy.data.meshes.remove(husk_mesh)
                container_deleted = True
            else:
                # untracked leftovers (loose geometry / foreign Ctrl+J faces):
                # never delete silently
                container.name = original_container_name + "_leftover"
                container_final_name = container.name
                del container[PROP_KEY]
                _remove_tracking_attrs(mesh)
                agr_report(op, 'WARNING',
                           f"⚠️ AGR Link: в '{container.name}' остались непомеченные "
                           f"вершины ({verts_left}) — контейнер сохранён")
    except Exception as exc:
        if not container_deleted:
            try:
                container.name = original_container_name
            except ReferenceError:
                pass
        agr_report(op, 'ERROR', f"❌ AGR Link: сбой разборки: {exc}")
        return None

    # selection: restored objects selected, first reachable one active
    for o in context.selected_objects:
        o.select_set(False)
    for obj, *_ in created:
        try:
            obj.select_set(True)
        except RuntimeError:
            pass  # restored into a collection excluded from this view layer
    for obj, *_ in created:
        try:
            context.view_layer.objects.active = obj
            break
        except RuntimeError:
            continue

    renamed = [obj.name for obj, entry, *_ in created if obj.name != entry["name"]]
    warn_bits = []
    if recon and recon.get("absorbed"):
        warn_bits.append(f"поглощены таблицы обычного Ctrl+J: {recon['absorbed']}")
    if missing:
        warn_bits.append(f"без граней (пропущены, записи сохранены): {len(missing)}")
    if skipped_singular:
        warn_bits.append(f"необратимая матрица (пропущены): {skipped_singular}")
    if skipped_stale:
        warn_bits.append(f"позиция не восстановима после Ctrl+J (пропущены): {skipped_stale}")
    if face_count_changed:
        warn_bits.append(f"изменилось число граней (правки или сторонний Ctrl+J): {face_count_changed}")
    if unlinked_edited:
        warn_bits.append(f"правленых копий оставлено уникальными: {unlinked_edited}")
    if renamed:
        warn_bits.append("имена заняты, переименованы: " + ", ".join(renamed[:3]))
    if mirror_failed:
        warn_bits.append("цветовое зеркало контейнера не обновлено (FBX-перенос недоступен)")
    if not container_deleted and container_final_name != original_container_name \
            and not container_final_name.endswith("_leftover"):
        warn_bits.append(f"контейнер переименован: {container_final_name}")
    level = 'WARNING' if warn_bits else 'INFO'
    icon = "⚠️" if warn_bits else "✅"
    msg = f"{icon} AGR Link: восстановлено {len(created)} объектов"
    if warn_bits:
        msg += " (" + "; ".join(warn_bits) + ")"
    agr_report(op, level, msg)
    return [obj for obj, *_ in created]


class AGR_OT_link_extract_group(Operator):
    """Разобрать из контейнера одну группу линков (или один объект):
восстановить исходные объекты с их именами, позициями и линкованностью"""
    bl_idname = "agr.link_extract_group"
    bl_label = "Разобрать группу"
    bl_options = {'REGISTER', 'UNDO'}

    group_id: IntProperty(name="Group ID", default=0)

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and is_container(context.active_object)

    def execute(self, context):
        container = context.active_object
        table = read_table(container)
        if table is None:
            agr_report(self, 'ERROR', "❌ AGR Link: таблица контейнера не читается (данные повреждены)")
            return {'CANCELLED'}
        ids = [int(iid) for iid, inst in table["instances"].items()
               if inst.get("group", 0) == self.group_id]
        if not ids:
            agr_report(self, 'ERROR', "❌ AGR Link: группа не найдена в контейнере")
            return {'CANCELLED'}
        result = _extract_instances(self, context, container, ids)
        return {'FINISHED'} if result is not None else {'CANCELLED'}


class AGR_OT_link_separate_all(Operator):
    """Разобрать контейнер полностью: восстановить ВСЕ исходные объекты
с их именами, позициями, коллекциями и линкованностью"""
    bl_idname = "agr.link_separate_all"
    bl_label = "Разобрать всё"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and is_container(context.active_object)

    def execute(self, context):
        container = context.active_object
        table = read_table(container)
        if table is None:
            agr_report(self, 'ERROR', "❌ AGR Link: таблица контейнера не читается (данные повреждены)")
            return {'CANCELLED'}
        ids = [int(iid) for iid in table["instances"].keys()]
        result = _extract_instances(self, context, container, ids)
        return {'FINISHED'} if result is not None else {'CANCELLED'}


# ----------------------------------------------------------------------------
# Strip memory (clean delivery)
# ----------------------------------------------------------------------------

class AGR_OT_link_strip(Operator):
    """Удалить память AGR с выделенных объектов (для полностью чистой
сдачи): таблица AGR Link, служебные атрибуты и color attributes, а также
записи атласов/UDIM.  Разборка/распаковка станет НЕВОЗМОЖНА"""
    bl_idname = "agr.link_strip"
    bl_label = "Удалить память (сдача)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'OBJECT'
                and any(o.type == 'MESH'
                        and (is_container(o)
                             or ATLAS_STORE.peek(o) is not None
                             or UDIM_STORE.peek(o) is not None)
                        for o in context.selected_objects))

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        count = 0
        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue
            had = False
            if read_table(obj) is not None:
                # colors-only container (fresh FBX import) has no idprop - pop, not del
                obj.pop(PROP_KEY, None)
                _remove_tracking_attrs(obj.data)
                _TABLE_CACHE.pop(obj.name, None)
                _MERGED_CACHE.pop(obj.name, None)
                had = True
            # atlas/UDIM records are AGR service data too - the delivery
            # file must not carry any of the color mirrors
            if ATLAS_STORE.peek(obj) is not None:
                ATLAS_STORE.strip(obj)
                had = True
            if 'agr_atlas_applied' in obj:
                # stripping the record while leaving this guard set would
                # block BOTH Apply (flag) and Unpack (no record) forever
                del obj['agr_atlas_applied']
                had = True
            if UDIM_STORE.peek(obj) is not None:
                UDIM_STORE.strip(obj)
                had = True
            if had:
                count += 1
        agr_report(self, 'INFO', f"✅ AGR Link: память удалена у {count} объектов — разборка невозможна")
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# Panel
# ----------------------------------------------------------------------------

class AGR_PT_LinkPanel(Panel):
    bl_label = "AGR Link"
    bl_idname = "AGR_PT_link_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 50  # after AGR UV (40), before AGR Share (100)

    def draw(self, context):
        layout = self.layout

        meshes = [o for o in context.selected_objects if o.type == 'MESH']
        groups = {o.data for o in meshes}
        col = layout.column(align=True)
        col.operator("agr.link_join", icon='OBJECT_DATAMODE')
        if meshes:
            col.label(text=f"Выбрано: {len(meshes)} мешей, {len(groups)} групп данных")

        obj = context.active_object
        if obj is None or obj.type != 'MESH':
            return
        table, extra_windows = _peek_merged(obj)
        if table is None:
            layout.label(text="Активный объект — не контейнер", icon='INFO')
            return

        instances = table.get("instances", {})
        by_group = {}
        for inst in instances.values():
            by_group.setdefault(inst.get("group", 0), []).append(inst)
        n_groups = len(by_group)

        layout.separator()
        layout.label(text=f"Контейнер: {len(instances)} объектов, {n_groups} групп",
                     icon='PACKAGE')
        if extra_windows:
            layout.label(text=f"Обычный Ctrl+J: влито контейнеров: {extra_windows}",
                         icon='INFO')
            layout.label(text="Память объединится при разборке или джойне")

        def group_label(gid, members):
            if len(members) > 1:
                data_name = table.get("groups", {}).get(str(gid), {}).get("data_name", "?")
                return f"{data_name} · {len(members)} шт.", 'LINKED'
            return members[0].get("name", "?"), 'OBJECT_DATA'

        box = layout.column(align=True)
        for gid in sorted(by_group.keys(), key=lambda g: group_label(g, by_group[g])[0]):
            members = by_group[gid]
            text, icon = group_label(gid, members)
            row = box.row(align=True)
            row.label(text=text, icon=icon)
            op = row.operator("agr.link_extract_group", text="", icon='EXPORT')
            op.group_id = gid

        layout.operator("agr.link_separate_all", icon='OUTLINER_OB_GROUP_INSTANCE')
        layout.operator("agr.link_strip", icon='TRASH')


# ----------------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------------

classes = (
    AGR_OT_link_join,
    AGR_OT_link_extract_group,
    AGR_OT_link_separate_all,
    AGR_OT_link_strip,
    AGR_PT_LinkPanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    print("✅ AGR Link operators registered")


def unregister():
    _TABLE_CACHE.clear()
    _MERGED_CACHE.clear()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
