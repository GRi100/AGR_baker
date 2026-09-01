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
- Forced restore (agr.link_restore) disassembles while ROLLING BACK
  edits: SOFT snaps every original vertex of an INTACT chunk (counts
  match the group AND all verts carry the orig flag AND the frame fit
  succeeded - a failed fit demotes the chunk) back to its stored
  coordinate and links it to the group reference even when repainted;
  HARD additionally throws away chunks whose topology no longer matches
  and attaches the reference datablock, placing the object by the frame
  fitted from the surviving original vertices (a non-converged fit or
  the stored-matrix fallback is reported as an approximate position).
  The reference is the majority vote by ballot signature (materials +
  geometry digest) among intact chunks - a repainted/flipped copy cannot
  hijack the group; repaints are discarded ONLY onto a vetted reference.
  With no intact chunk left, HARD may take a still-alive datablock that
  matches the group's name, counts AND the chunks' surviving original
  vertices (its materials/UV win - reported).  A legacy container
  (no per-vertex originals, no color mirror) is refused up front.
  Faceless and matrix_stale instances are skipped exactly as in a plain
  disassembly.  UV policy is unchanged: the container's unwrap wins.

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

Triangulating exports (FBX "Triangulate Faces" - the delivery setting):
the T*-blob is a POSITIONAL byte stream over loops, and triangulation
re-orders/duplicates loops, which used to scramble it beyond CRC repair
while the per-corner CO/ID VALUES survived.  Every pack therefore writes
the shared AGR_LoopIdx layer (original loop index + 1 per loop, 0 =
untracked; core/attr_store.py) and the readers invert the permutation
when the raw parse fails.  Reproduced and fixed on the real Salarevo
delivery file.

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
import hashlib
import json
import os
import re
import time

import bpy
import bmesh
import numpy as np
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty,
                       IntProperty, PointerProperty, StringProperty)
from bpy.types import Operator, Panel, PropertyGroup, UIList
from mathutils import Matrix

from .log import agr_report
from .core.attr_store import (ColorBlobStore, preserve_active_color,
                              read_srgb_bytes, loop_index_array,
                              loop_index_is_canonical, drop_orphan_loop_index,
                              HEADER_V1, HEADER_V2)
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


def _stamp_remap(mesh, id_map, loop_range=None, face_pos=None):
    """Remap existing attribute values old->new; unknown values become 0.
    With ``loop_range=(lo, hi)`` only faces whose loop_start falls in the
    range are touched — used to remap ONE absorbed window of a plain
    Ctrl+J while the ids of the other blocks stay intact.  ``face_pos``
    overrides the per-face position tested against the range: the rescue
    path of a permuted mirror hands original loop offsets here, because
    the windows live in original loop space."""
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
        if face_pos is None:
            face_pos = np.zeros(n, dtype=np.intc)
            mesh.polygons.foreach_get("loop_start", face_pos)
        seg = (face_pos >= lo) & (face_pos < hi)
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
    # the shared loop-index layer goes when no namespace mirrors remain
    # (delivery files must carry no AGR service color attributes at all)
    drop_orphan_loop_index(mesh)


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


def _read_orig_mask(mesh):
    """POINT-domain agr_link_orig flag as a bool array, or None on legacy
    meshes that never stored it."""
    flag = mesh.attributes.get(ORIG_ATTR)
    if flag is None or flag.domain != 'POINT' or flag.data_type != 'BOOLEAN':
        return None
    arr = np.zeros(len(mesh.vertices), dtype=bool)
    flag.data.foreach_get("value", arr)
    return arr


def _fit_affine_core(p, q, mask, extra_tol=0.0):
    """Least-squares affine fit p[mask] -> q[mask] with conflict-multiplet
    exclusion, iterative trimmed-outlier refit and SVD normal-completion
    for planar clouds.  Pure computation (no mesh access).
    Returns (a, t, res, tol, converged) or None when degenerate; ``res``
    are residuals over ALL masked points from the FINAL frame, ``tol`` the
    accept threshold used for snapping decisions.  ``converged`` is False
    when the frame could not be anchored on a trustworthy rigid majority
    (trimming gave up, or so few points survived that the 12-DOF affine
    problem turns interpolative and "fits" outliers too) - callers must
    treat the placement as approximate and say so.  Known limit: on tiny
    clouds (<= ~5 verts) an edit is absorbed by the frame with zero
    residual and stays undetectable."""
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
    diag = float(np.linalg.norm(p[mask].max(axis=0) - p[mask].min(axis=0)))

    def residuals(a, t):
        return np.linalg.norm(q[mask] - (p[mask] @ a.T + t), axis=1)

    def accept_tol(t):
        return max(1e-4, diag * 1e-4, float(np.linalg.norm(t)) * 1e-6, extra_tol)

    res = residuals(a, t)
    tol = accept_tol(t)
    converged = True
    if res.max() > tol:
        m_idx = np.flatnonzero(mask)
        work = np.ones(len(m_idx), dtype=bool)    # inlier flags over m_idx

        # Points sharing one stored original (duplicated geometry inherits
        # the attributes verbatim) but pulled apart in the container CANNOT
        # be told apart by residuals: the fit would flip a coin between the
        # prototype and the duplicate, or bridge them with a fake scale.
        # Exclude every conflicted multiplet from the fit up front.  The
        # scan is LAZY - a pulled-apart multiplet always pushes a residual
        # beyond tol, so a clean first fit skips it entirely - and its
        # threshold lives in CONTAINER space with the same offset-scaled
        # noise budget as every other tolerance in this file.
        qm_all = q[m_idx]
        conflict_tol = max(
            1e-4,
            float(np.linalg.norm(qm_all.max(axis=0) - qm_all.min(axis=0))) * 1e-4,
            float(np.linalg.norm(qm_all.mean(axis=0))) * 5e-6,
            extra_tol)
        _u, inv, counts = np.unique(p[m_idx], axis=0,
                                    return_inverse=True, return_counts=True)
        if (counts > 1).any():
            for g in np.flatnonzero(counts > 1):
                sel = inv == g
                qg = qm_all[sel]
                spread = float(np.linalg.norm(qg - qg.mean(axis=0), axis=1).max())
                if spread > conflict_tol:
                    work[sel] = False
        if not work.all():
            if int(work.sum()) >= 3:
                sel = np.zeros(len(mask), dtype=bool)
                sel[m_idx[work]] = True
                refit = fit(sel)
                if refit is not None:
                    a, t = refit
                    res = residuals(a, t)
                    tol = accept_tol(t)
            else:
                work[:] = True  # conflict-free remnant degenerate - keep the full fit

        # part of the piece was edited - refit on the rigid majority.  The
        # affine LSQ SMEARS an outlier over the other points (one nudged
        # cube vertex ends up only 2x above the median residual), so a
        # single median-threshold pass cannot isolate it: iterate, dropping
        # outlier batches - and at least the single worst point per round -
        # while the inlier fit still exceeds tol and a majority remains.
        start = int(work.sum())
        min_keep = max(3, (start + 1) // 2)       # the majority must survive
        for _ in range(64):
            res_in = res[work]
            if res_in.max() <= tol or int(work.sum()) <= min_keep:
                break
            thr = max(tol, 3.0 * float(np.median(res_in)))
            drop = work & (res > thr)
            if not drop.any():
                worst = int(np.argmax(np.where(work, res, -1.0)))
                drop = np.zeros_like(work)
                drop[worst] = True
            if int(work.sum() - drop.sum()) < min_keep:
                break
            work &= ~drop
            sel = np.zeros(len(mask), dtype=bool)
            sel[m_idx[work]] = True
            refit = fit(sel)
            if refit is None:
                break
            a, t = refit
            res = residuals(a, t)
            tol = accept_tol(t)
        # honesty flag: trustworthy only when the surviving inliers still
        # fit AND enough of them survived to over-determine the affine
        # problem - an interpolative remnant "fits" anything, outliers
        # included (the vacuum-convergence trap)
        kept = int(work.sum())
        trimmed = start - kept
        converged = bool(res[work].max() <= tol
                         and (trimmed == 0
                              or (kept >= 5 and kept * 3 >= start * 2)))

    if abs(np.linalg.det(a)) < 1e-12:
        return None
    return a, t, res, tol, converged


def _affine_to_matrix(a, t):
    return Matrix((
        (a[0][0], a[0][1], a[0][2], t[0]),
        (a[1][0], a[1][1], a[1][2], t[1]),
        (a[2][0], a[2][1], a[2][2], t[2]),
        (0.0, 0.0, 0.0, 1.0),
    ))


def _solve_instance_frame(mesh, extra_tol=0.0, snap_all=False):
    """Recover the instance frame from the stored per-vertex original
    coordinates: least-squares affine fit original-local -> current
    container-local.  Immune to container Apply Transform / Set Origin /
    whole-piece edit-mode moves (the frame follows the piece) and to FBX
    matrix rebuilds - vertex order is irrelevant, the pairing rides with
    each vertex.  Rewrites the mesh vertices back into original local space
    (snapping unedited verts to their exact stored coords; snap_all=True
    forces EVERY original vertex back - the forced-restore modes discard
    local edits on purpose).  Returns (frame Matrix, edited vertex count,
    fit converged), or (None, 0, False) when the attributes are absent or
    the point cloud is degenerate (legacy containers fall back to the
    matrix path).  ``converged`` False = the frame could not be anchored
    on a trustworthy rigid majority - the caller must report the placement
    as approximate."""
    co_attr = mesh.attributes.get(CO_ATTR)
    flag_attr = mesh.attributes.get(ORIG_ATTR)
    if (co_attr is None or co_attr.domain != 'POINT' or co_attr.data_type != 'FLOAT_VECTOR'
            or flag_attr is None or flag_attr.domain != 'POINT' or flag_attr.data_type != 'BOOLEAN'):
        return None, 0, False
    n = len(mesh.vertices)
    if n == 0:
        return None, 0, False
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
        return None, 0, False
    a, t, res, tol, converged = core

    # final vertex coords: exact stored originals for unedited verts (kills
    # float32 container-space noise), frame-inverse for edited/new verts
    a_inv = np.linalg.inv(a)
    final = (q - t) @ a_inv.T
    if snap_all:
        snap = mask.copy()
    else:
        snap = np.zeros(n, dtype=bool)
        snap[np.flatnonzero(mask)[res <= tol]] = True
    final[snap] = p[snap]
    mesh.vertices.foreach_set("co", final.astype(np.float32).ravel())

    return _affine_to_matrix(a, t), int((res > tol).sum()), converged


def _stored_point_sample(mesh, limit=32):
    """Up to ``limit`` stored original coordinates of the chunk's orig
    verts.  The last-resort alive gate checks that a claimed reference
    actually CONTAINS these points among its vertices (a chunk is a subset
    of its original), rejecting a same-name same-counts stranger."""
    co_attr = mesh.attributes.get(CO_ATTR)
    om = _read_orig_mask(mesh)
    if (co_attr is None or co_attr.domain != 'POINT'
            or co_attr.data_type != 'FLOAT_VECTOR'
            or om is None or not om.any()):
        return None
    arr = np.zeros(len(mesh.vertices) * 3, dtype=np.float32)
    co_attr.data.foreach_get("vector", arr)
    pts = arr.reshape(-1, 3)[om]
    if len(pts) > limit:
        step = max(1, len(pts) // limit)
        pts = pts[::step][:limit]
    return pts.copy()


def _chunk_is_intact(mesh, ginfo):
    """Chunk is "intact as a unit": exactly the group datablock's counts
    AND every vertex existed at join time.  Only such a chunk has a
    per-vertex pairing with the reference - its edits can be rolled back.
    Originals with loose vertices never qualify: the chunk prune drops
    loose geometry (see _prune_mesh_to_faces) and the counts diverge."""
    verts, faces = ginfo.get("verts"), ginfo.get("faces")
    if verts is None or faces is None or not len(mesh.polygons):
        return False
    if len(mesh.vertices) != int(verts) or len(mesh.polygons) != int(faces):
        return False
    mask = _read_orig_mask(mesh)
    return mask is not None and len(mask) == len(mesh.vertices) and bool(mask.all())


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
    # "cannot read this mesh right now" is NOT "the mirror is stale", and
    # only the second one justifies deleting the mirror.  While an edit
    # BMesh is open every attribute's .data array is EMPTY - the counts
    # refresh, the arrays do not, and update_from_editmode() does not help -
    # so foreach_get would raise and the caller's except would wipe a
    # mirror that may be perfectly healthy.  Bail out BEFORE touching
    # anything; the save handler retries once the mesh is flushed.
    if mesh.is_editmode:
        return False
    attr = mesh.attributes.get(ATTR_NAME)
    co_attr = mesh.attributes.get(CO_ATTR)
    flag_attr = mesh.attributes.get(ORIG_ATTR)
    if attr is None or co_attr is None or flag_attr is None:
        # do NOT remove the mirror here: with the tracking attrs gone the
        # mirror may be the ONLY surviving carrier of the original coords,
        # and deleting it would destroy the container's memory for good
        # (callers must rebuild the attrs from the mirror first)
        return False
    n_verts = len(mesh.vertices)
    n_loops = len(mesh.loops)
    n_polys = len(mesh.polygons)
    if n_verts == 0 or n_loops == 0:
        _remove_color_mirror(mesh)
        return False
    if (len(co_attr.data) != n_verts or len(flag_attr.data) != n_verts
            or len(attr.data) != n_polys):
        return False   # arrays not materialised - same "cannot read" case

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


def _untracked_faces(mesh, windows=None):
    """Faces that no id stamp / readable table window accounts for — the
    tail a plain Ctrl+J into this container left behind.  With tracking
    attrs the count is exact; on the pure-FBX path (no attrs yet) it is
    estimated from the window metadata — the same arithmetic _merged_view
    always used."""
    ids = _read_face_ids(mesh)
    if ids is not None:
        return int((ids == 0).sum())
    # loop-index layer: exact even when an exporter re-ordered/duplicated
    # the loops (a triangulated mesh has MORE polygons than the table
    # promises, and the subtraction below would invent phantom untracked
    # faces).  idx -1 = the zero-fill a plain join gave foreign loops.
    idx = loop_index_array(mesh)
    if idx is not None:
        loop_start = np.zeros(len(mesh.polygons), dtype=np.intc)
        mesh.polygons.foreach_get("loop_start", loop_start)
        return int((idx[loop_start] < 0).sum())
    if windows is None:
        windows = _LINK_STORE.scan_windows(mesh)
    total = sum(int(i.get("faces", 0) or 0)
                for _s, _c, wt in windows
                for i in wt.get("instances", {}).values())
    return max(0, len(mesh.polygons) - total)


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
    # _ex: the descramble rescue reads a mirror whose loops an FBX
    # "Triangulate Faces" export permuted (raw scan finds nothing there)
    windows, _rescue_idx = _LINK_STORE.scan_windows_ex(data)
    if not windows:
        return idp, 0
    if idp is not None:
        own = any(_window_matches_table(wt, idp) for _s, _c, wt in windows)
        if len(windows) - (1 if own else 0) <= 0:
            return idp, 0
    zero = _untracked_faces(data, windows) if idp is None else 0
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


# ----------------------------------------------------------------------------
# Mirror integrity ("is this container ready for FBX?")
# ----------------------------------------------------------------------------
# The memory has TWO carriers: the idprop (lives in the .blend, immune to
# mesh edits) and the color mirror (the ONLY one a default FBX export
# carries).  They drift apart silently: the blob is written as one
# contiguous run starting at loop 0, so deleting the faces that happen to
# sit at the head of the mesh takes the frame header - magic, length,
# CRC32 - with it.  The .blend keeps working off the idprop while the
# exported FBX ships a container nobody can disassemble.
MIRROR_OK = 'OK'          # header matches the current mesh
MIRROR_STALE = 'STALE'    # mesh was edited after the mirror was packed
MIRROR_BROKEN = 'BROKEN'  # no readable header (edits cut the frame off)
MIRROR_NONE = 'NONE'      # no mirror at all
MIRROR_UNKNOWN = 'UNKNOWN'  # cannot be read right now (open edit BMesh)
MIRROR_WINDOWS = 'WINDOWS'  # memory rides in plain-Ctrl+J windows, not at loop 0

# containers the save-time autosync must leave to the explicit button:
# obj.name -> (fingerprint, reason).  Three reasons, all "not repackable as
# it stands": foreign plain-Ctrl+J windows in the mirror, a blob that does
# not fit the mesh, a repack that raised.  Without this every save would
# pay the full repack (0.5 s on a 1.3M-loop container) just to fail again;
# the fingerprint means the very next mesh edit retries it.
_NO_AUTOSYNC = {}


def _mirror_fingerprint(obj):
    """Identity of "this object with this mesh and this table" - the key
    behind the autosync skip list.  session_uid is part of it on purpose:
    a DELETED container whose name is later reused by a new object (or by
    the NEXT opened file) must not inherit the old entry - names alone
    repeat constantly in this pipeline."""
    mesh = obj.data
    raw = obj.get(PROP_KEY)
    return (obj.session_uid, mesh.name, len(mesh.loops), len(mesh.polygons),
            len(mesh.vertices), len(raw) if isinstance(raw, str) else 0)


def _has_link_data(obj):
    """Cheap "might be a container" test for batch ops and the depsgraph
    handler: idprop or mirror present, WITHOUT parsing the table (the parse
    costs 76 ms on a 1.3M-loop mesh - far too much per depsgraph tick)."""
    if obj is None or getattr(obj, "type", None) != 'MESH' or obj.data is None:
        return False
    if obj.get(PROP_KEY) is not None:
        return True
    return obj.data.attributes.get(TABLE_COL_PREFIX + "0") is not None


def _store_mirror_state(obj, store):
    """Verdict core shared by the link container check and the auxiliary
    UDIM/atlas record namespaces (same dual-carrier design in
    core/attr_store.py, same failure modes).  Callers guard object type
    and edit mode."""
    mesh = obj.data
    if mesh.attributes.get(store.prefix + "0") is None:
        return MIRROR_NONE
    head = store.peek_frame_header(mesh)
    if head is None:
        # No frame at loop 0 - two very different situations, told apart in
        # O(1) by the idprop.  A container whose OWN frame was decapitated by
        # an edit still carries its idprop (that carrier is immune to mesh
        # edits), and for it BROKEN is the truth.  A container that was merged
        # INTO a plain mesh by a plain Ctrl+J has no idprop at all - the plain
        # mesh had none - and its table survives as a WINDOW at a non-zero
        # loop offset, which scan_windows reads and a default FBX export
        # carries verbatim (scripts/test_link.py test 39).  Calling that one
        # "разрушено" was a false alarm printed right above the panel's own
        # "влито контейнеров" line.  The reader is safe: this branch is only
        # reached when a table was decoded, i.e. the windows DO parse.
        return MIRROR_BROKEN if obj.get(store.prop_key) is not None else MIRROR_WINDOWS
    version, length, src_loops = head
    if src_loops and src_loops != len(mesh.loops):
        # the mesh was edited after the pack: STALE regardless of what the
        # layers can hold now (the repack fixes both, and this verdict is
        # the more informative of the two)
        return MIRROR_STALE
    # Loop count agrees (or a legacy v1 frame carries none), so the frame
    # LOOKS healthy - but the header promises `length` payload bytes and the
    # layers must be able to HOLD them.  Without this a mirror that lost a
    # layer (deleted by hand in the Color Attributes list, or dropped by an
    # exporter with a vertex-color cap) keeps a valid header and an unchanged
    # loop count, so the verdict was OK while decode_colors could only ever
    # fail: the panel stayed silent and the FBX shipped unreadable memory.
    # frame_capacity counts ONLY layers decode can actually read: a foreign
    # FLOAT attribute that merely borrowed the "<prefix>N" name used to
    # inflate a name-only count and re-create the very false-OK this gate
    # was written to kill.
    capacity = store.frame_capacity(mesh)
    if capacity is None or (HEADER_V2 if version >= 2 else HEADER_V1) + length > capacity:
        return MIRROR_BROKEN
    return MIRROR_OK


def _mirror_state(obj):
    """poll()/draw()-safe answer to "will an FBX export carry this
    container's memory?".  Deliberately UNCACHED: the verdict depends on
    the mirror BYTES, and every fingerprint cheap enough to cache on -
    mesh name, loop count, idprop length, layer count - stays IDENTICAL
    when only the mirror changes (undo of "Закрепить память", File >
    Revert, a strip done by another script), so the panel would keep
    reporting OK over a dead mirror.  A fingerprint that IS sensitive has
    to read those bytes, which is the whole computation.  And that
    computation is tiny and near-flat: ~12 us from 512 to 2.6M loops
    against ~1 us for a cache hit, next to the 1.4 us - 1.1 ms
    _peek_merged spends in the SAME draw."""
    if obj is None or getattr(obj, "type", None) != 'MESH' or obj.data is None:
        return MIRROR_NONE
    mesh = obj.data
    if mesh.is_editmode:
        # while an edit BMesh is open every attribute's data array is
        # EMPTY (the counts refresh, the arrays do not), so the mirror
        # cannot be read at all - saying BROKEN here would raise a false
        # alarm on every container the user opens in Edit Mode
        return MIRROR_UNKNOWN
    state = _store_mirror_state(obj, _LINK_STORE)
    if state == MIRROR_OK:
        # The FBX transport is only whole with BOTH tracking layers: their
        # names (AGR_Link_CO / AGR_Link_ID) do not match the "<prefix>N"
        # filter, so the capacity gate above never sees them - deleting one
        # in the Color Attributes list kept the verdict green while the
        # import path could only ever fail ("нет атрибута agr_link_id").
        # A repack rebuilds both from the live agr_link_* attributes.
        for name in (COL_CO, COL_ID):
            if not _LINK_STORE.layer_ok(mesh, name):
                return MIRROR_BROKEN
    return state


def _invalidate_caches(name):
    """Drop every poll/draw cache entry for one object plus its autosync
    skip mark - callers reach here right after rewriting the data, and an
    explicit user action is exactly the moment an earlier "cannot repack
    this one" verdict has earned another chance."""
    _TABLE_CACHE.pop(name, None)
    _MERGED_CACHE.pop(name, None)
    _NO_AUTOSYNC.pop(name, None)


def _has_foreign_windows(mesh):
    """True when the mirror still carries table windows that are NOT this
    container's own - the leftovers of a plain Blender Ctrl+J.  A repack
    writes ONE fresh blob from loop 0 over the whole mesh, so a foreign
    window that was not absorbed FIRST is gone, and with it the only copy
    of the merged container's table (the idprop holds this container's own
    table alone).  Absorbing means _reconcile_container: id remap, possibly
    a new zero-instance, possibly obj.data swapped for an Alt+D twin - a
    structural, non-undoable change that belongs behind an explicit click,
    never behind a save-time checkbox.

    A canonical carrier contributes exactly one magic hit, at loop 0: no
    hit at all = an edit decapitated our own frame and nothing else lives
    in the layer (safe to repack); exactly one hit that peek_frame_header
    can parse = that hit IS at loop 0, i.e. ours (safe).  Anything else goes
    on to the CRC-checked scan: the raw magic count is a byte pattern search
    over the compressed payload too, so a chance b"AGRL" on a loop boundary
    used to invent a foreign window and pin the container in _NO_AUTOSYNC
    for good ("нажмите «Закрепить память»" with nothing to absorb).  Only
    scan_windows can tell a real frame from a coincidence, and it is paid
    exactly where the cheap test was already ambiguous."""
    hits = _LINK_STORE.count_window_candidates(mesh)
    if hits == 0:
        return False
    if hits == 1 and _LINK_STORE.peek_frame_header(mesh) is not None:
        return False
    windows = _LINK_STORE.scan_windows(mesh)
    if not windows:
        return False              # every candidate was a payload coincidence
    # exactly one REAL frame, and it starts at loop 0 = our own canonical
    # mirror with a coincidence somewhere in its payload: nothing to absorb
    return not (len(windows) == 1 and windows[0][0] == 0)


def _unpack_tracking_windows(mesh, windows, merged, loop_idx=None):
    """FBX path of the plain-Ctrl+J absorb: rebuild the internal tracking
    attributes when the color mirror holds SEVERAL table windows.  Each
    window's vertices are denormalised with ITS OWN co_min/co_size and
    bit-exact precise_* records; verts outside every window (plain objects
    merged without memory) keep their CURRENT coords with orig=False —
    the zero-instance step stamps them afterwards.  Updates
    merged["co_quant"] with the worst window quantum.  Returns True when
    the mirror was usable.

    loop_idx (from scan_windows_ex's rescue path) maps CURRENT loops to
    ORIGINAL loop offsets: the windows then live in original space and a
    plain [s:s+cnt] slice of the current mesh would pick an arbitrary
    subset (a triangulating exporter re-ordered the loops)."""
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
        if loop_idx is None:
            w_loops = vidx[s:s + cnt]
        else:
            w_loops = vidx[(loop_idx >= s) & (loop_idx < s + cnt)]
        w_verts = np.unique(w_loops)
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
    # rescue path: loop_idx maps current loops to ORIGINAL offsets when an
    # FBX triangulation permuted the mirror (windows then live in original
    # loop space and every loop-range test below must go through the map)
    windows, loop_idx = _LINK_STORE.scan_windows_ex(mesh)
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
    if any(mesh.attributes.get(name) is None
           for name in (ATTR_NAME, CO_ATTR, ORIG_ATTR)):
        # ALL THREE attrs, not just the face ids: with co/orig deleted but
        # the ids alive, the old one-attribute check skipped this unpack,
        # the idprop commit below went through, and _pack_tracking_to_colors
        # then refused (it must not wipe the mirror - the last carrier of
        # the coords).  That left the OLD mirror behind the NEW idprop, the
        # next reconcile no longer matched its own window and re-absorbed
        # every window under fresh ids - instances duplicated on every run.
        stub = {"co_quant": float((idp or {}).get("co_quant", 0.0) or 0.0)}
        if not _unpack_tracking_windows(mesh, windows, stub, loop_idx):
            return None  # attrs missing and no usable mirror - cannot absorb
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

    if extras and loop_idx is not None:
        starts = np.zeros(len(mesh.polygons), dtype=np.intc)
        mesh.polygons.foreach_get("loop_start", starts)
        remap_pos = loop_idx[starts]
    else:
        remap_pos = None
    for s, cnt, id_map in extras:
        _stamp_remap(mesh, id_map, loop_range=(s, s + cnt), face_pos=remap_pos)

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
        # a face lives in a window when its first loop does; on the rescue
        # path that test runs in ORIGINAL loop space (idx -1 = untracked,
        # matching no window - exactly the foreign zero-fill semantics)
        face_pos = (loop_start_arr if loop_idx is None
                    else loop_idx[loop_start_arr])
        in_win = np.zeros(n_polys, dtype=bool)
        for w_s, w_cnt, _wt in windows:
            in_win |= (face_pos >= w_s) & (face_pos < w_s + w_cnt)
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
                    if core is not None and core[4]:
                        # persist ONLY a converged fit - a frame anchored on
                        # a dubious remnant must not overwrite matrix_rel
                        # and clear the stale marker (extract would then
                        # fly the piece with no warning)
                        a, t = core[0], core[1]
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
    _invalidate_caches(obj.name)
    return {"absorbed": len(extras), "zero_instance": zero_instance,
            "stale": stale, "mirror_ok": mirror_ok}


# ----------------------------------------------------------------------------
# Memory refresh ("закрепить память")
# ----------------------------------------------------------------------------

def _refresh_container(context, obj, absorb=True):
    """Absorb any plain-Ctrl+J windows and repack ONE fresh mirror over the
    CURRENT mesh, making the container canonical again: safe to edit and
    safe to hand to another DCC.  Shared by the join operator (one selected
    container), agr.link_refresh and the save-time autosync.  Returns
    {"ok", "reason", "instances", "groups", "absorbed", "zero_instance",
    "mirror_ok"}; ok=False carries a reason string instead of reporting -
    the callers word their own message through _refresh_message.

    absorb=False is the save-time contract: repack the mirror and NOTHING
    else.  Absorbing rewrites the table and can swap the datablock, which
    is a structural change nobody asked for by ticking a checkbox - so a
    container with foreign windows is refused ("windows") instead, and the
    panel's «Закрепить память» button (absorb=True) stays the single place
    where absorption happens."""
    if obj is None or getattr(obj, "type", None) != 'MESH':
        return {"ok": False, "reason": "not_mesh"}
    if obj.library is not None or obj.data.library is not None:
        return {"ok": False, "reason": "library"}
    if obj.data.is_editmode:
        # obj.data is the pre-edit snapshot and its attribute arrays are
        # empty, so the whole repack would be written into a mesh that the
        # edit BMesh is about to overwrite - refuse here, so no caller can
        # lose a repack by accident (see _pack_tracking_to_colors)
        return {"ok": False, "reason": "editmode"}
    if not absorb:
        if _has_foreign_windows(obj.data):
            return {"ok": False, "reason": "windows"}
        if obj.get(PROP_KEY) is None and _untracked_faces(obj.data) > 0:
            # read_table below would hand back a merged view holding a
            # SYNTHESISED zero-instance for the untracked tail — and only
            # _reconcile_container can stamp its faces.  Persisting that
            # view here would register geometry no face carries (it comes
            # out of extraction only as a _leftover husk), silently, inside
            # save_pre, outside undo — absorption territory, refuse exactly
            # like "windows".
            return {"ok": False, "reason": "unmarked"}

    recon = _reconcile_container(context, obj) if absorb else None
    table = read_table(obj)
    if table is None:
        return {"ok": False, "reason": "unreadable"}

    if recon is not None:
        mirror_ok = recon.get("mirror_ok", True)
    else:
        # nothing to absorb - still refresh idprop + mirror so the memory
        # matches the CURRENT mesh exactly (e.g. after edits or a fresh FBX
        # import that was never materialised).  The test is ALL THREE attrs,
        # not just the face ids: _pack_tracking_to_colors needs the per-vertex
        # co/orig too, and a container carrying only agr_link_id would sail
        # past a one-attribute check straight into a failed pack - while the
        # mirror it was about to overwrite may be the last copy of those very
        # coordinates.  Missing any of them means "unpack from the mirror
        # FIRST", which is exactly what the branch below does.
        has_attrs = all(obj.data.attributes.get(name) is not None
                        for name in (ATTR_NAME, CO_ATTR, ORIG_ATTR))
        if not has_attrs:
            has_attrs = _unpack_tracking_from_colors(obj.data, table)
        if not has_attrs:
            # inherited broken state (idprop without attrs or a usable
            # mirror) - repacking would destroy the surviving mirror
            return {"ok": False, "reason": "no_tracking"}
        write_table(obj, table)
        try:
            mirror_ok = _pack_tracking_to_colors(obj.data, table)
        except Exception:
            _remove_color_mirror(obj.data)
            mirror_ok = False
        if mirror_ok:
            write_table(obj, table)

    _invalidate_caches(obj.name)
    instances = table.get("instances", {})
    return {"ok": True, "reason": None,
            "instances": len(instances),
            "groups": len({inst.get("group", 0) for inst in instances.values()}),
            "absorbed": (recon or {}).get("absorbed", 0),
            "zero_instance": bool((recon or {}).get("zero_instance")),
            "mirror_ok": bool(mirror_ok)}


def _refresh_message(obj, stats):
    """(level, message) for one _refresh_container result."""
    if not stats.get("ok"):
        reason = stats.get("reason")
        if reason == "library":
            return 'ERROR', "❌ AGR Link: объект из линкованной библиотеки нельзя изменять"
        if reason == "editmode":
            return 'WARNING', ("⚠️ AGR Link: контейнер в режиме редактирования — "
                               "память обновится после выхода в Object Mode")
        if reason == "windows":
            return 'WARNING', ("⚠️ AGR Link: в зеркале лежат таблицы обычного Ctrl+J — "
                               "нажмите «Закрепить память», чтобы их поглотить")
        if reason == "unmarked":
            return 'WARNING', ("⚠️ AGR Link: в контейнер влита непомеченная геометрия "
                               "(обычный Ctrl+J) — нажмите «Закрепить память», чтобы "
                               "оформить её объектом")
        if reason == "unreadable":
            return 'ERROR', "❌ AGR Link: таблица контейнера не читается (данные повреждены)"
        if reason == "no_tracking":
            return 'WARNING', ("⚠️ AGR Link: у контейнера нет разметки для перепаковки — "
                               "память оставлена как есть")
        return 'ERROR', "❌ AGR Link: активный объект — не контейнер"
    msg = (f"✅ AGR Link: память '{obj.name}' обновлена "
           f"({stats['instances']} объектов, {stats['groups']} групп)")
    if stats.get("absorbed"):
        msg += f", поглощено таблиц обычного Ctrl+J: {stats['absorbed']}"
    if stats.get("zero_instance"):
        msg += ", непомеченная геометрия оформлена объектом"
    if not stats.get("mirror_ok"):
        return 'WARNING', msg + " | ⚠️ цветовое зеркало не записано (FBX-перенос недоступен)"
    return 'INFO', msg


def _refresh_result(stats):
    """Operator return value matching a _refresh_container result: a
    container with no tracking left is reported, not treated as a failure
    (nothing was mutated, the surviving mirror stays)."""
    if stats.get("ok") or stats.get("reason") == "no_tracking":
        return {'FINISHED'}
    return {'CANCELLED'}


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
        """Single selected container: "закрепить память" — the shared
        _refresh_container does the work (absorb plain-Ctrl+J windows,
        repack ONE fresh mirror over the current mesh)."""
        stats = _refresh_container(context, active)
        level, msg = _refresh_message(active, stats)
        agr_report(self, level, msg)
        return _refresh_result(stats)


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


def _materials_match(mesh_a, mesh_b):
    """Same slot names in the same order AND the same material_index on
    every face.  Split out of _geometry_matches so the restore modes can
    tell a repaint apart from a real geometry edit."""
    if not np.array_equal(_mesh_mat_indices(mesh_a), _mesh_mat_indices(mesh_b)):
        return False
    return ([m.name if m else "" for m in mesh_a.materials]
            == [m.name if m else "" for m in mesh_b.materials])


def _ballot_key(mesh):
    """Exact-INTEGER pre-key for the restore reference vote (taken AFTER
    _restore_materials compacted the slots): material signature, element
    counts and the loop->vertex map.  Deliberately contains NO float
    bytes.  Hashing the coords used to look safe because intact chunks are
    snapped onto their stored originals - but the stored originals come
    back QUANTISED whenever table["co_quant"] > 0 (FBX roundtrip without
    the precise_co records), and each plain-Ctrl+J window is dequantised
    against its OWN co_min/co_size, so two honest copies of one group
    differ by up to co_quant/2 and landed in separate buckets.  The vote
    then degenerated to "first intact chunk wins" and a repainted minority
    could take the group.  Quantising the coords before hashing does not
    help either: any grid splits a bucket as soon as one coordinate sits
    near a cell edge, and that risk grows with the vertex count.  So the
    coordinate comparison leaves the key entirely and moves into
    _vote_reference's tolerant pairwise pass.  Flip/re-bridge detection
    does not need it: reverse_faces / flip_normals rewrite each polygon's
    loop order, so a flipped chunk still lands in another pre-bucket, and
    a re-bridged one changes the counts or the map."""
    h = hashlib.blake2b(digest_size=16)
    loops = np.zeros(len(mesh.loops), dtype=np.intc)
    if len(mesh.loops):
        mesh.loops.foreach_get("vertex_index", loops)
    h.update(loops.tobytes())
    h.update(_mesh_mat_indices(mesh).tobytes())
    names = tuple(m.name if m else "" for m in mesh.materials)
    return (names, len(mesh.vertices), len(mesh.polygons), len(mesh.loops),
            h.digest())


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
    if check_materials and not _materials_match(mesh_a, mesh_b):
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


def _vote_reference(members, extra_atol=0.0):
    """Group reference for the restore modes: majority vote among INTACT
    chunks.  A repainted, flipped or otherwise deviant copy must not
    become the new "original" - the healthy majority wins.  On a tie the
    first bucket wins (insertion order is preserved and max() returns the
    first maximum), so the outcome stays deterministic.  Returns the
    representative object or None when no chunk is intact.

    Buckets are built by greedy clustering, NOT by hashing: the exact
    integer _ballot_key only PRE-sorts, and the "same chunk" decision is
    the very predicate the adoption loop below uses - _geometry_matches
    with the SAME extra_atol budget (max(offset*5e-6, table["co_quant"])).
    That is the whole point: a bucket now means exactly "these chunks
    would adopt each other", so copies whose stored coords came back
    through different quantisation windows can no longer be split apart,
    while a repaint (materials compared exactly, and the material names
    and per-face indices sit in the pre-key already) or a flip (loop map
    in the pre-key, polygon normals at atol 0.1 inside _geometry_matches)
    still separates.  Only buckets carrying the IDENTICAL pre-key are ever
    compared, so the healthy case costs one comparison per member and the
    scan can never fan out across unrelated shapes."""
    buckets = []      # [{"rep": obj, "objs": [...]}], insertion order kept
    by_key = {}       # pre-key -> indices of the buckets sharing that key
    for obj, _m, info in members:
        if not info["intact"]:
            continue
        slot = by_key.setdefault(_ballot_key(obj.data), [])
        for i in slot:
            if _geometry_matches(buckets[i]["rep"].data, obj.data,
                                 check_materials=True, extra_atol=extra_atol):
                buckets[i]["objs"].append(obj)
                break
        else:
            slot.append(len(buckets))
            buckets.append({"rep": obj, "objs": [obj]})
    if not buckets:
        return None
    return max(buckets, key=lambda b: len(b["objs"]))["rep"]


def _adopt_target(member, target, new_meshes):
    """Switch the object onto the reference datablock and drop its own
    chunk mesh.  The chunk mesh was created by mesh.copy() for this object
    alone, so after the reassignment it has zero users and remove() is
    safe; the discard keeps the alive-adoption gate of the NEXT groups
    from probing a dead pointer."""
    old = member.data
    member.data = target
    new_meshes.discard(old)
    bpy.data.meshes.remove(old)


def _alive_contains_stored_points(alive, members, extra_atol, limit=64):
    """Vertex-subset gate for the last-resort reference: every SAMPLED
    stored original point of the group's chunks must exist among the
    candidate's vertices (a chunk is a subset of its original) - a
    same-name same-counts stranger fails here.

    The sample is spread ACROSS the members, not sliced off the head of
    one concatenated array: with a single budget for the whole group the
    first chunk alone filled all 64 slots (its own sample is 32 points),
    so in a multi-member group every chunk but the first went unchecked
    and a stranger that merely contained the FIRST chunk's points walked
    straight in.

    No stored points at all => REFUSE.  This branch is HARD-only, it
    throws the chunks' geometry away and hands the objects a datablock
    whose materials and UV then beat the container's; a name plus two
    integers is not enough to authorise that, and "pts is None" marks
    exactly the cases where nothing can vouch for identity (no stored
    coords at all, or not one original vertex left).  A group that loses
    here is reported honestly as "групп без эталона" and comes out as it
    would from a normal disassembly - nothing is destroyed and the user
    can still re-link by hand."""
    pts = [i["pts"] for _o, _m, i in members
           if i.get("pts") is not None and len(i["pts"])]
    if not pts:
        return False
    if len(pts) > limit:
        # evenly spread indices over the FULL member list (linspace, both
        # ends included).  The old stride `pts[::len(pts) // limit][:limit]`
        # degenerated to the LEADING `limit` members whenever
        # len(pts) // limit == 1 (65..127 members) - the tail went entirely
        # unchecked, which is exactly what this subset must never allow.
        idx = np.unique(np.linspace(0, len(pts) - 1, num=limit).round().astype(int))
        pts = [pts[i] for i in idx]
    share = max(1, limit // len(pts))
    sample = np.concatenate([p[:share] for p in pts],
                            axis=0)[:limit].astype(np.float64)
    if not len(alive.vertices):
        # nothing can contain a stored point; without this the blocked
        # nearest-vertex search below reduces over a zero-size axis and
        # raises, taking the whole disassembly down mid-flight
        return False
    verts = np.zeros(len(alive.vertices) * 3, dtype=np.float32)
    alive.vertices.foreach_get("co", verts)
    verts = verts.reshape(-1, 3).astype(np.float64)
    scale = float(max(np.max(np.abs(verts), initial=1.0), 1.0))
    atol = max(1e-4, scale * 1e-5, extra_atol)
    # vectorised nearest-vertex search, blocked so the (block, V, 3)
    # temporary stays ~32 MB even on a million-vertex candidate (there it
    # degrades to exactly the old point-at-a-time loop)
    block = max(1, 4_000_000 // (3 * max(len(verts), 1)))
    for i in range(0, len(sample), block):
        d2 = ((sample[i:i + block, None, :] - verts[None, :, :]) ** 2).sum(axis=2)
        if float(np.sqrt(d2.min(axis=1)).max()) > atol:
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


def _extract_instances(op, context, container, target_ids, restore='OFF'):
    """Core disassembly: pull the given instance ids out of the container.
    restore: 'OFF' - honest disassembly (edited chunks stay unique);
    'SOFT' | 'HARD' - forced restore, see AGR_OT_link_restore.
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

    # legacy container (no per-vertex co/orig): there is nothing to restore
    # from - HARD can only lean on an alive datablock from the file
    restore_blind = (restore != 'OFF'
                     and (mesh.attributes.get(CO_ATTR) is None
                          or mesh.attributes.get(ORIG_ATTR) is None))

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
        created = []  # (obj, entry, frame, group_id, info)
        done_ids = []
        skipped_singular = 0
        skipped_stale = 0
        face_changed_ids = set()
        soft_snapped = 0    # chunks whose vertex shifts were discarded
        soft_repaint = 0    # linked despite a repaint
        hard_rebuilt = 0    # chunk geometry thrown away, reference taken
        hard_alive_ref = 0  # reference had to come from the scene
        hard_no_ref = 0     # groups HARD could not restore (no reference)
        # instance ids whose placement is not anchored on a converged fit.
        # A SET, not a counter: one chunk can be flagged twice - once as an
        # intact chunk with a dubious frame, once again when HARD rebuilds
        # it from the reference - and the report must not count it twice
        # (same reason hard_ids / face_changed_ids are sets)
        pos_approx_ids = set()
        hard_ids = set()
        for iid in extract:
            entry = table["instances"][str(iid)]
            m_rel = Matrix(entry["matrix_rel"])
            gid = entry.get("group", 0)
            stored_faces = entry.get("faces")
            if stored_faces is not None:
                if int(np.count_nonzero(face_ids == iid)) != stored_faces:
                    face_changed_ids.add(iid)

            new_mesh = mesh.copy()
            _prune_mesh_to_faces(new_mesh, lambda v, _iid=iid: v == _iid)

            # intactness (restore only) must be read BEFORE the tracking
            # attrs are removed; the stored-point sample feeds the
            # last-resort alive gate of the relink phase
            intact = (restore != 'OFF'
                      and _chunk_is_intact(new_mesh, table["groups"].get(str(gid), {})))
            stored_pts = _stored_point_sample(new_mesh) if restore == 'HARD' else None
            # Primary path: recover the frame from the stored per-vertex
            # original coords (robust to container Apply Transform / Set
            # Origin / whole-piece edit-mode moves / FBX matrix rebuilds).
            # Legacy containers fall back to the matrix path.
            frame, edited_verts, fit_converged = _solve_instance_frame(
                new_mesh, extra_tol=float(table.get("co_quant", 0.0)),
                snap_all=intact)
            _remove_tracking_attrs(new_mesh)
            fitted = frame is not None
            # a failed fit means the snap never ran: the chunk must not
            # pass as restored, vote, or serve as the group reference
            intact = intact and fitted
            if intact and edited_verts:
                soft_snapped += 1
            if intact and not fit_converged:
                # coords are snapped, but the FRAME itself is dubious -
                # the object may stand off its true place
                pos_approx_ids.add(iid)
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
            created.append((obj, entry, frame, gid,
                            {"iid": iid, "intact": intact, "fitted": fitted,
                             "converged": fit_converged, "pts": stored_pts}))
            done_ids.append(iid)

        # Second pass: parents.  Resolution order matters: (1) the batch
        # itself, by ORIGINAL entry name; (2) the surviving container when
        # the parent name is the container's real name (it is parked under
        # .__agr_link_tmp right now, so a bare name lookup would miss it);
        # (3) the scene by name.
        created_by_name = {}
        for obj, entry, m_rel, gid, _info in created:
            created_by_name.setdefault(entry["name"], obj)
        container_survives = len(table["instances"]) > len(done_ids)
        for obj, entry, m_rel, gid, _info in created:
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
        unplaced = {obj: m_rel for obj, entry, m_rel, gid, _info in created}
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
        for obj, entry, m_rel, gid, info in created:
            by_group.setdefault(gid, []).append((obj, m_rel, info))
        new_meshes = {obj.data for obj, *_ in created}
        forced = restore != 'OFF'
        for gid, members in by_group.items():
            ginfo = table["groups"].get(str(gid), {})
            data_name = ginfo.get("data_name", members[0][0].data.name)
            # float32 noise budget grows with container-relative offset;
            # after an FBX roundtrip the 16-bit quantisation step dominates
            extra_atol = max(max(m.translation.length for _o, m, _i in members) * 5e-6,
                             float(table.get("co_quant", 0.0)))

            # the group reference: under restore ONLY a majority-vote winner
            # among intact chunks (or a vetted alive datablock) may serve -
            # an arbitrary, never-validated first member must not own the
            # group and eat the other copies' paint or geometry
            # the vote gets the SAME noise budget as the adoption loop
            # below - otherwise honest copies that differ only by the
            # quantisation step vote in separate buckets
            rep = _vote_reference(members, extra_atol) if forced else None
            probe = rep if rep is not None else members[0][0]

            target = None
            ref_from_alive = False
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
            # group then links onto itself instead).  Under restore the
            # probe must be the VOTED representative - matching against an
            # arbitrary (possibly vandalised) first member would promote an
            # unvetted reference for the whole group.
            if alive_ok and (not forced or rep is not None) \
                    and _geometry_matches(probe.data, alive,
                                          check_materials=True,
                                          extra_atol=extra_atol) \
                    and _uv_matches(probe.data, alive):
                # copies of this group still live in the file - re-attach
                target = alive
                _remove_tracking_attrs(target)
                ref_from_alive = True
            elif (alive_ok and restore == 'HARD' and rep is None
                  and ginfo.get("verts") is not None
                  and ginfo.get("faces") is not None
                  and len(alive.vertices) == int(ginfo["verts"])
                  and len(alive.polygons) == int(ginfo["faces"])
                  and _alive_contains_stored_points(alive, members, extra_atol)):
                # LAST resort (HARD only): not a single intact chunk
                # survived, but a datablock carrying the group's name, the
                # exact counts AND the chunks' surviving original vertices
                # still lives in the file.  Its materials/UV win over the
                # container's - reported separately
                target = alive
                _remove_tracking_attrs(target)
                ref_from_alive = True
                hard_alive_ref += 1
            if target is None:
                target = probe.data
                target.name = data_name
            ref_ok = rep is not None or ref_from_alive

            group_gave_up = False
            for member, _m, info in members:
                if member.data == target:
                    continue
                geo_ok = _geometry_matches(member.data, target,
                                           check_materials=False,
                                           extra_atol=extra_atol)
                if geo_ok and _materials_match(member.data, target):
                    _adopt_target(member, target, new_meshes)
                elif geo_ok and forced and ref_ok \
                        and (info["intact"] or restore == 'HARD'):
                    # intact chunk, different paint: restore means restore -
                    # the repaint goes to the bin, but ONLY onto a vetted
                    # reference (user decision).  The intactness test is the
                    # module contract for SOFT ("celye kuski"): a chunk whose
                    # topology was rebuilt never ran through snap_all, so its
                    # coords merely LANDING within the tolerance is not the
                    # vetted per-vertex pairing this branch claims - SOFT
                    # leaves it unique and says "нужен жёсткий режим", HARD
                    # may still discard it deliberately
                    _adopt_target(member, target, new_meshes)
                    soft_repaint += 1
                elif restore == 'HARD' and ref_ok:
                    # broken topology: the chunk's geometry is thrown away;
                    # the object keeps the frame fitted from its surviving
                    # original vertices (or the stored-matrix fallback)
                    _adopt_target(member, target, new_meshes)
                    hard_rebuilt += 1
                    hard_ids.add(info["iid"])
                    if not (info["fitted"] and info["converged"]):
                        pos_approx_ids.add(info["iid"])
                else:
                    unlinked_edited += 1
                    if restore == 'HARD' and not ref_ok:
                        group_gave_up = True
            if group_gave_up:
                # only groups that actually LEFT members unrestored count -
                # a group that linked fine without a vote is not a failure
                hard_no_ref += 1

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
    info_bits = []
    if recon and recon.get("absorbed"):
        warn_bits.append(f"поглощены таблицы обычного Ctrl+J: {recon['absorbed']}")
    if missing:
        warn_bits.append(f"без граней (пропущены, записи сохранены): {len(missing)}")
    if skipped_singular:
        warn_bits.append(f"необратимая матрица (пропущены): {skipped_singular}")
    if skipped_stale:
        warn_bits.append(f"позиция не восстановима после Ctrl+J (пропущены): {skipped_stale}")
    # instances HARD just rebuilt are not "suspicious" anymore
    stale_faces = face_changed_ids - hard_ids
    if stale_faces:
        warn_bits.append(f"изменилось число граней (правки или сторонний Ctrl+J): {len(stale_faces)}")
    if unlinked_edited:
        tail = " — нужен жёсткий режим" if restore == 'SOFT' else ""
        warn_bits.append(f"правленых копий оставлено уникальными{tail}: {unlinked_edited}")
    if renamed:
        warn_bits.append("имена заняты, переименованы: " + ", ".join(renamed[:3]))
    if mirror_failed:
        warn_bits.append("цветовое зеркало контейнера не обновлено (FBX-перенос недоступен)")
    if not container_deleted and container_final_name != original_container_name \
            and not container_final_name.endswith("_leftover"):
        warn_bits.append(f"контейнер переименован: {container_final_name}")
    if restore != 'OFF':
        if restore_blind:
            warn_bits.append("контейнер старого формата — восстанавливать не по чему")
        if soft_snapped:
            info_bits.append(f"сдвиги вершин отброшены: {soft_snapped}")
        if soft_repaint:
            info_bits.append(f"перекраска отброшена: {soft_repaint}")
        if hard_rebuilt:
            info_bits.append(f"перестроено по эталону: {hard_rebuilt}")
        if hard_alive_ref:
            warn_bits.append(f"эталон взят из сцены (его материалы/UV победили): {hard_alive_ref}")
        if hard_no_ref:
            warn_bits.append(f"групп без эталона (не восстановлены): {hard_no_ref}")
        if pos_approx_ids:
            warn_bits.append("позиция может быть неточной (фит не сошёлся): "
                             f"{len(pos_approx_ids)}")
    level = 'WARNING' if warn_bits else 'INFO'
    icon = "⚠️" if warn_bits else ("♻️" if info_bits else "✅")
    head = {'SOFT': "мягкое восстановление", 'HARD': "жёсткое восстановление"}.get(restore)
    msg = (f"{icon} AGR Link: {head} — {len(created)} объектов" if head
           else f"{icon} AGR Link: восстановлено {len(created)} объектов")
    if warn_bits or info_bits:
        msg += " (" + "; ".join(info_bits + warn_bits) + ")"
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
    """Разобрать контейнер: при выборе в списке — только выбранные группы,
без выбора — восстановить ВСЕ исходные объекты"""
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
        sel = set(_selected_group_ids(context))
        if sel:
            ids = [int(iid) for iid, inst in table["instances"].items()
                   if inst.get("group", 0) in sel]
            if not ids:
                agr_report(self, 'ERROR',
                           "❌ AGR Link: выбранные группы не найдены в контейнере")
                return {'CANCELLED'}
        else:
            ids = [int(iid) for iid in table["instances"].keys()]
        result = _extract_instances(self, context, container, ids)
        return {'FINISHED'} if result is not None else {'CANCELLED'}


class AGR_OT_link_restore(Operator):
    """Разобрать контейнер с ПРИНУДИТЕЛЬНЫМ восстановлением инстансов:
правки откатываются к эталону группы, копии снова становятся линкованными"""
    bl_idname = "agr.link_restore"
    bl_label = "Восстановить"
    bl_options = {'REGISTER', 'UNDO'}

    # HIDDEN keeps both out of the F9 "Adjust Last Operation" panel, which
    # calls execute() DIRECTLY: without it a soft restore could be turned into
    # a hard one by flipping the enum there, and the invoke_confirm below -
    # the user's only warning about discarded geometry - would never run
    mode: EnumProperty(
        name="Режим",
        items=[('SOFT', "Мягко",
                "Только целые куски: сдвиги вершин и перекраска отбрасываются"),
               ('HARD', "Жёстко",
                "Плюс куски с разрушенной топологией: их геометрия выбрасывается")],
        default='SOFT',
        options={'HIDDEN', 'SKIP_SAVE'})
    group_id: IntProperty(name="Group ID", default=-1,   # -1 = the whole container
                          options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and is_container(context.active_object)

    @classmethod
    def description(cls, context, properties):
        if properties.mode == 'HARD':
            return ("Жёсткое восстановление: как мягкое, ПЛЮС куски с разрушенной "
                    "топологией (примержи, удалённые грани) — их геометрия "
                    "ВЫБРАСЫВАЕТСЯ и заменяется эталоном группы, позиция берётся "
                    "по уцелевшим исходным вершинам. Куски без граней и с "
                    "непересчитанной матрицей пропускаются")
        return ("Мягкое восстановление: куски, целые как единица, возвращаются к "
                "исходным координатам и линкуются к эталону группы даже при другой "
                "покраске. Куски с изменённой топологией не трогаются")

    def invoke(self, context, event):
        if self.mode == 'HARD':
            # the per-group trigger is an icon-only button - this popup is
            # the user's only warning about what HARD is going to discard
            return context.window_manager.invoke_confirm(
                self, event, title="Жёсткое восстановление",
                message="Правки геометрии кусков будут ВЫБРОШЕНЫ и заменены "
                        "эталоном группы. Продолжить?",
                confirm_text="Восстановить", icon='WARNING')
        return self.execute(context)

    def execute(self, context):
        container = context.active_object
        table = read_table(container)
        if table is None:
            agr_report(self, 'ERROR',
                       "❌ AGR Link: таблица контейнера не читается (данные повреждены)")
            return {'CANCELLED'}
        mesh = container.data
        if (mesh.attributes.get(CO_ATTR) is None
                and mesh.attributes.get(COL_CO) is None):
            # legacy container: no per-vertex originals and no color mirror
            # to rebuild them from - there is NOTHING to restore, and a
            # silent plain disassembly here would be an irreversible surprise
            agr_report(self, 'ERROR',
                       "❌ AGR Link: контейнер старого формата (нет исходных "
                       "координат) — восстанавливать не по чему, используйте "
                       "обычную разборку")
            return {'CANCELLED'}
        if self.group_id < 0:
            # panel buttons: honour the list selection, else the whole container
            sel = set(_selected_group_ids(context))
            if sel:
                ids = [int(iid) for iid, inst in table["instances"].items()
                       if inst.get("group", 0) in sel]
                if not ids:
                    agr_report(self, 'ERROR',
                               "❌ AGR Link: выбранные группы не найдены в контейнере")
                    return {'CANCELLED'}
            else:
                ids = [int(iid) for iid in table["instances"].keys()]
        else:
            ids = [int(iid) for iid, inst in table["instances"].items()
                   if inst.get("group", 0) == self.group_id]
            if not ids:
                agr_report(self, 'ERROR', "❌ AGR Link: группа не найдена в контейнере")
                return {'CANCELLED'}
        result = _extract_instances(self, context, container, ids, restore=self.mode)
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
                _invalidate_caches(obj.name)
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
# Panel list mirror (Scene.agr_link_groups)
# ----------------------------------------------------------------------------
# The panel list is a real CollectionProperty synced from the ACTIVE
# container's merged table: editable rows (rename on double click) and
# texture-sets-style selection dots need ID properties, which draw() may
# not create.  A throttled depsgraph handler keeps the mirror fresh;
# operators resync explicitly before trusting the selection.

_LIST_SYNCING = False    # suppress the name-update callback during rebuilds


def _strip_copy_suffix(name):
    """Drop Blender's ``.NNN`` copy suffix and our ``_NNN`` numbering."""
    base = re.sub(r"\.\d+$", "", name)
    base = re.sub(r"_\d+$", "", base)
    return base


def _group_base_name(table, gid, members):
    """Display/base name of one link group - from OBJECT names, not the
    datablock (user decision): an explicit stored name wins, then the
    common base of the member names, then the data name as last resort."""
    ginfo = table.get("groups", {}).get(str(gid), {})
    explicit = ginfo.get("name")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    names = sorted(str(m.get("name", "")) for m in members if m.get("name"))
    if not names:
        return ginfo.get("data_name") or "?"
    if len(names) == 1:
        return names[0]
    bases = [_strip_copy_suffix(n) for n in names]
    if bases[0] and all(b == bases[0] for b in bases):
        return bases[0]
    prefix = os.path.commonprefix(bases).rstrip("._- ")
    if len(prefix) >= 2:
        return prefix
    return ginfo.get("data_name") or names[0]


def _group_rows(obj):
    """[(gid, base_name, member_count)] of the active container (sorted),
    or [] when the object is not a container.  Reads the cached merged
    view - cheap enough for the depsgraph tick."""
    if obj is None or getattr(obj, "type", None) != 'MESH':
        return []
    table, _extras = _peek_merged(obj)
    if table is None:
        return []
    by_group = {}
    for inst in table.get("instances", {}).values():
        by_group.setdefault(inst.get("group", 0), []).append(inst)
    rows = [(gid, _group_base_name(table, gid, members), len(members))
            for gid, members in by_group.items()]
    rows.sort(key=lambda r: (r[1].lower(), r[0]))
    return rows


def _sync_group_list(scene, obj):
    """Rebuild Scene.agr_link_groups when it no longer matches the active
    container.  Selection survives by gid - but only within the SAME
    container (gids of different containers are unrelated)."""
    global _LIST_SYNCING
    rows = _group_rows(obj)
    owner = obj.name if (obj is not None and rows) else ""
    coll = scene.agr_link_groups
    if (scene.agr_link_groups_owner == owner and len(coll) == len(rows)
            and all(it.gid == g and it.name == b and it.count == c
                    for it, (g, b, c) in zip(coll, rows))):
        return False
    keep = ({it.gid: it.is_selected for it in coll}
            if scene.agr_link_groups_owner == owner else {})
    _LIST_SYNCING = True
    try:
        coll.clear()
        for g, b, c in rows:
            it = coll.add()
            it.gid = g
            it.name = b
            it.count = c
            it.is_selected = keep.get(g, False)
        scene.agr_link_groups_owner = owner
        if scene.agr_link_groups_index >= len(rows):
            scene.agr_link_groups_index = max(0, len(rows) - 1)
    finally:
        _LIST_SYNCING = False
    return True


def _selected_group_ids(context):
    """gids ticked in the panel list, resynced first so a stale list can
    never aim an operator at the wrong container's groups."""
    scene = context.scene
    _sync_group_list(scene, context.active_object)
    return [it.gid for it in scene.agr_link_groups if it.is_selected]


def _on_group_item_renamed(self, context):
    """Editing the name in the list renames the whole group - through an
    operator, so the change lands in the undo stack."""
    if _LIST_SYNCING:
        return
    try:
        bpy.ops.agr.link_rename_group('EXEC_DEFAULT', True,
                                      group_id=self.gid, new_name=self.name)
    except Exception:
        # the operator reverts the field via resync on its own failures;
        # this guard only covers a broken context
        pass


class AGR_LinkGroupItem(PropertyGroup):
    gid: IntProperty()
    name: StringProperty(name="Имя", update=_on_group_item_renamed)
    count: IntProperty()
    is_selected: BoolProperty(name="Выбрано", default=False)


class AGR_UL_LinkGroupsList(UIList):
    """Groups of the active container: the dot toggles selection (click or
    drag across rows), double click on the name renames the group."""

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        row = layout.row(align=True)
        row.prop(item, "is_selected", text="", emboss=False,
                 icon='RADIOBUT_ON' if item.is_selected else 'RADIOBUT_OFF')
        row.prop(item, "name", text="", emboss=False,
                 icon='LINKED' if item.count > 1 else 'OBJECT_DATA')
        if item.count > 1:
            sub = row.row(align=True)
            sub.alignment = 'RIGHT'
            sub.label(text=f"{item.count} шт.")


class AGR_OT_link_rename_group(Operator):
    """Переименовать группу инстансов контейнера: участники получают имена
Имя_001, Имя_002…, одиночный объект — просто Имя"""
    bl_idname = "agr.link_rename_group"
    bl_label = "Переименовать группу"
    bl_options = {'REGISTER', 'UNDO'}

    group_id: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})
    new_name: StringProperty(name="Имя")

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and is_container(context.active_object)

    def execute(self, context):
        obj = context.active_object
        scene = context.scene

        def bail(msg):
            agr_report(self, 'ERROR', msg)
            _sync_group_list(scene, obj)   # revert the edited field
            return {'CANCELLED'}

        base = self.new_name.strip()
        if not base:
            return bail("❌ AGR Link: пустое имя группы")
        _tbl, extras = _peek_merged(obj)
        if extras:
            return bail("❌ AGR Link: в контейнер влиты чужие таблицы — сначала "
                        "нажмите «Закрепить память»")
        table = read_table(obj)
        if table is None:
            return bail("❌ AGR Link: таблица контейнера не читается")
        members = sorted(((int(iid), inst)
                          for iid, inst in table["instances"].items()
                          if inst.get("group", 0) == self.group_id),
                         key=lambda p: str(p[1].get("name", "")))
        if not members:
            return bail("❌ AGR Link: группа не найдена в контейнере")
        if len(members) == 1:
            members[0][1]["name"] = base
        else:
            for i, (_iid, inst) in enumerate(members, 1):
                inst["name"] = f"{base}_{i:03d}"
        if str(self.group_id) in table.get("groups", {}):
            table["groups"][str(self.group_id)]["name"] = base

        write_table(obj, table)
        mirror_ok = False
        try:
            mirror_ok = _pack_tracking_to_colors(obj.data, table)
        except Exception:
            _remove_color_mirror(obj.data)
        if mirror_ok:
            write_table(obj, table)
        _invalidate_caches(obj.name)
        _sync_group_list(scene, obj)
        msg = f"✅ AGR Link: группа переименована — «{base}» ({len(members)} шт.)"
        if mirror_ok:
            agr_report(self, 'INFO', msg)
        else:
            agr_report(self, 'WARNING',
                       msg + " | ⚠️ зеркало не обновлено (FBX уедет со старыми именами)")
        return {'FINISHED'}


class AGR_OT_link_groups_select_all(Operator):
    """Выбрать все группы в списке (или снять выбор, если что-то выбрано)"""
    bl_idname = "agr.link_groups_select_all"
    bl_label = "Все / ничего"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return len(context.scene.agr_link_groups) > 0

    def execute(self, context):
        items = context.scene.agr_link_groups
        value = not any(it.is_selected for it in items)
        for it in items:
            it.is_selected = value
        return {'FINISHED'}


class AGR_OT_link_select_by_faces(Operator):
    """Выбрать в списке группы, которым принадлежат выделенные фейсы
контейнера (Edit Mode)"""
    bl_idname = "agr.link_select_by_faces"
    bl_label = "Выбрать группы по фейсам"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return (context.mode == 'EDIT_MESH'
                and is_container(context.edit_object))

    def execute(self, context):
        obj = context.edit_object
        table = _peek_table(obj)
        if table is None:
            agr_report(self, 'ERROR', "❌ AGR Link: таблица контейнера не читается")
            return {'CANCELLED'}
        bm = bmesh.from_edit_mesh(obj.data)
        layer = bm.faces.layers.int.get(ATTR_NAME)
        if layer is None:
            agr_report(self, 'ERROR',
                       f"❌ AGR Link: на контейнере нет атрибута {ATTR_NAME} "
                       "(свежий импорт? выйдите в Object Mode и зайдите снова)")
            return {'CANCELLED'}
        ids = {f[layer] for f in bm.faces if f.select}
        ids.discard(0)
        if not ids:
            agr_report(self, 'WARNING',
                       "⚠️ AGR Link: среди выделенных фейсов нет размеченных")
            return {'CANCELLED'}
        gids = {inst.get("group", 0)
                for iid, inst in table.get("instances", {}).items()
                if int(iid) in ids}
        scene = context.scene
        _sync_group_list(scene, obj)
        for it in scene.agr_link_groups:
            it.is_selected = it.gid in gids
        agr_report(self, 'INFO',
                   f"✅ AGR Link: по фейсам выбрано групп: {len(gids)}")
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# Instance watcher: auto-name linked duplicates in chosen collections
# ----------------------------------------------------------------------------
# Live-scene counterpart of the container list: the user picks collections,
# and every group of objects sharing one mesh datablock inside them is kept
# named ``Base_001..N`` (its own numbering per group).  The base comes from
# the object names (or the editable override stored on the MESH - shared by
# all instances for free); existing valid numbers are kept, newcomers take
# the lowest free ones, so adding a copy never renumbers the whole group.

WATCH_BASE_KEY = "agr_instance_base"   # per-mesh idprop: user-chosen base

_WATCH_LAST_FP = None


def _watch_members(scene):
    """{mesh: [objects]} across the watched collections - local mesh
    objects whose datablock is genuinely shared (users >= 2)."""
    seen_names = set()
    groups = {}
    for item in scene.agr_link_watch_colls:
        coll = item.collection
        if coll is None:
            continue
        for o in coll.all_objects:
            if (o.type != 'MESH' or o.data is None or o.library is not None
                    or o.data.library is not None or o.name in seen_names):
                continue
            seen_names.add(o.name)
            me = o.data
            if me.users - (1 if me.use_fake_user else 0) < 2:
                continue
            groups.setdefault(me, []).append(o)
    return groups


def _watch_base(mesh, members):
    """Base name for one instance group: the stored override, else the
    common base of the member OBJECT names, else the first name cleaned."""
    explicit = mesh.get(WATCH_BASE_KEY)
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    names = sorted(o.name for o in members)
    bases = [_strip_copy_suffix(n) for n in names]
    if bases and bases[0] and all(b == bases[0] for b in bases):
        return bases[0]
    prefix = os.path.commonprefix(bases).rstrip("._- ")
    if len(prefix) >= 2:
        return prefix
    return _strip_copy_suffix(names[0]) or names[0]


def _watch_apply(scene):
    """Enforce ``Base_###`` on every watched group.  Returns
    (renamed_count, conflict_names).  Objects already carrying a valid
    unique number keep it; the rest get the lowest free numbers in name
    order.  Renames go through a temp pass so in-group swaps cannot
    collide; a name held by a FOREIGN object is reported, Blender's own
    dedup suffix stays on that member."""
    renamed, conflicts = 0, []
    for mesh, members in _watch_members(scene).items():
        base = _watch_base(mesh, members)
        pat = re.compile(re.escape(base) + r"_(\d{3,})$")
        by_num = {}
        rest = []
        for o in sorted(members, key=lambda ob: ob.name):
            m = pat.fullmatch(o.name)
            num = int(m.group(1)) if m else 0
            if num > 0 and num not in by_num:
                by_num[num] = o
            else:
                rest.append(o)
        desired = {o: f"{base}_{n:03d}" for n, o in by_num.items()}
        free = 1
        for o in rest:
            while free in by_num:
                free += 1
            desired[o] = f"{base}_{free:03d}"
            by_num[free] = o
        pending = [(o, want) for o, want in desired.items() if o.name != want]
        if not pending:
            continue
        for o, _want in pending:
            o.name = o.name + ".__agr_wtmp"   # free the targets first
        for o, want in pending:
            o.name = want
            if o.name != want:
                conflicts.append(want)
            renamed += 1
    return renamed, conflicts


def _watch_fp(scene):
    """Cheap fingerprint of the watched state - names, bases, membership."""
    parts = []
    for mesh, members in _watch_members(scene).items():
        parts.append((mesh.name, str(mesh.get(WATCH_BASE_KEY, "")),
                      tuple(sorted(o.name for o in members))))
    return tuple(sorted(parts))


def _watch_tick(scene):
    """Depsgraph-side auto-apply: only when the watched state changed."""
    global _WATCH_LAST_FP
    fp = _watch_fp(scene)
    if fp == _WATCH_LAST_FP:
        return
    renamed, conflicts = _watch_apply(scene)
    _WATCH_LAST_FP = _watch_fp(scene)
    if conflicts:
        agr_report(None, 'WARNING',
                   "⚠️ AGR Link: имена заняты другими объектами: "
                   + ", ".join(conflicts[:5]) + ("…" if len(conflicts) > 5 else ""))


def _sync_watch_groups(scene):
    """Mirror the watched instance groups into Scene.agr_link_watch_groups
    (editable base name + count)."""
    global _LIST_SYNCING
    rows = [(mesh.name, _watch_base(mesh, members), len(members))
            for mesh, members in _watch_members(scene).items()]
    rows.sort(key=lambda r: (r[1].lower(), r[0]))
    coll = scene.agr_link_watch_groups
    if (len(coll) == len(rows)
            and all(it.key == k and it.name == b and it.count == c
                    for it, (k, b, c) in zip(coll, rows))):
        return False
    _LIST_SYNCING = True
    try:
        coll.clear()
        for k, b, c in rows:
            it = coll.add()
            it.key = k
            it.name = b
            it.count = c
        if scene.agr_link_watch_groups_index >= len(rows):
            scene.agr_link_watch_groups_index = max(0, len(rows) - 1)
    finally:
        _LIST_SYNCING = False
    return True


def _on_watch_group_renamed(self, context):
    if _LIST_SYNCING:
        return
    try:
        bpy.ops.agr.link_watch_rename('EXEC_DEFAULT', True,
                                      mesh_name=self.key, new_name=self.name)
    except Exception:
        pass


class AGR_LinkWatchColl(PropertyGroup):
    collection: PointerProperty(type=bpy.types.Collection, name="Коллекция")


class AGR_LinkWatchGroup(PropertyGroup):
    key: StringProperty()      # mesh datablock name (group identity)
    name: StringProperty(name="Имя группы", update=_on_watch_group_renamed)
    count: IntProperty()


class AGR_UL_LinkWatchColls(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        coll = item.collection
        layout.label(text=coll.name if coll else "(коллекция удалена)",
                     icon='OUTLINER_COLLECTION')


class AGR_UL_LinkWatchGroups(UIList):
    """Watched instance groups: double click the name to rename the whole
    group (members become Имя_001, Имя_002, …)."""

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        row = layout.row(align=True)
        row.prop(item, "name", text="", emboss=False, icon='LINKED')
        sub = row.row(align=True)
        sub.alignment = 'RIGHT'
        sub.label(text=f"{item.count} шт.")


_WATCH_ENUM_ITEMS = []   # EnumProperty items must outlive the callback


def _watch_enum_items(self, context):
    global _WATCH_ENUM_ITEMS
    scene = context.scene
    watched = {it.collection for it in scene.agr_link_watch_colls if it.collection}
    items = [(c.name, c.name, "") for c in bpy.data.collections
             if c.library is None and c not in watched]
    if not items:
        items = [("__none__", "(нет коллекций)", "")]
    _WATCH_ENUM_ITEMS = items
    return items


class AGR_OT_link_watch_add(Operator):
    """Добавить коллекцию под слежку за инстансами"""
    bl_idname = "agr.link_watch_add"
    bl_label = "Добавить коллекцию"
    bl_options = {'REGISTER', 'UNDO'}
    bl_property = "collection"

    collection: EnumProperty(name="Коллекция", items=_watch_enum_items)

    def invoke(self, context, event):
        context.window_manager.invoke_search_popup(self)
        return {'FINISHED'}

    def execute(self, context):
        global _WATCH_LAST_FP
        if self.collection == "__none__":
            return {'CANCELLED'}
        coll = bpy.data.collections.get(self.collection)
        if coll is None:
            return {'CANCELLED'}
        scene = context.scene
        if any(it.collection == coll for it in scene.agr_link_watch_colls):
            return {'CANCELLED'}
        item = scene.agr_link_watch_colls.add()
        item.collection = coll
        _WATCH_LAST_FP = None      # let the next tick re-apply
        _sync_watch_groups(scene)
        return {'FINISHED'}


class AGR_OT_link_watch_remove(Operator):
    """Убрать активную коллекцию из-под слежки"""
    bl_idname = "agr.link_watch_remove"
    bl_label = "Убрать коллекцию"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return len(context.scene.agr_link_watch_colls) > 0

    def execute(self, context):
        global _WATCH_LAST_FP
        scene = context.scene
        idx = scene.agr_link_watch_colls_index
        if not (0 <= idx < len(scene.agr_link_watch_colls)):
            return {'CANCELLED'}
        scene.agr_link_watch_colls.remove(idx)
        scene.agr_link_watch_colls_index = min(
            idx, len(scene.agr_link_watch_colls) - 1)
        _WATCH_LAST_FP = None
        _sync_watch_groups(scene)
        return {'FINISHED'}


class AGR_OT_link_watch_apply(Operator):
    """Прогнать схему имён по наблюдаемым коллекциям прямо сейчас:
каждая группа инстансов получает имена Имя_001, Имя_002, …"""
    bl_idname = "agr.link_watch_apply"
    bl_label = "Применить схему имён"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(it.collection is not None
                   for it in context.scene.agr_link_watch_colls)

    def execute(self, context):
        global _WATCH_LAST_FP
        scene = context.scene
        renamed, conflicts = _watch_apply(scene)
        _WATCH_LAST_FP = _watch_fp(scene)
        _sync_watch_groups(scene)
        msg = (f"✅ AGR Link: переименовано объектов: {renamed}" if renamed
               else "✅ AGR Link: все инстансы уже названы по схеме")
        if conflicts:
            agr_report(self, 'WARNING', msg + " | ⚠️ имена заняты: "
                       + ", ".join(conflicts[:5])
                       + ("…" if len(conflicts) > 5 else ""))
        else:
            agr_report(self, 'INFO', msg)
        return {'FINISHED'}


class AGR_OT_link_watch_rename(Operator):
    """Переименовать группу инстансов: имя запоминается на датаблоке меша
и применяется ко всем участникам как Имя_001, Имя_002, …"""
    bl_idname = "agr.link_watch_rename"
    bl_label = "Переименовать группу инстансов"
    bl_options = {'REGISTER', 'UNDO'}

    mesh_name: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    new_name: StringProperty(name="Имя")

    def execute(self, context):
        global _WATCH_LAST_FP
        scene = context.scene
        mesh = bpy.data.meshes.get(self.mesh_name)
        base = self.new_name.strip()
        if mesh is None or not base:
            _sync_watch_groups(scene)   # revert the edited field
            return {'CANCELLED'}
        mesh[WATCH_BASE_KEY] = base
        renamed, conflicts = _watch_apply(scene)
        _WATCH_LAST_FP = _watch_fp(scene)
        _sync_watch_groups(scene)
        msg = f"✅ AGR Link: группа инстансов → «{base}» (переименовано: {renamed})"
        if conflicts:
            agr_report(self, 'WARNING', msg + " | ⚠️ имена заняты: "
                       + ", ".join(conflicts[:5]))
        else:
            agr_report(self, 'INFO', msg)
        return {'FINISHED'}


# ----------------------------------------------------------------------------
# UI sync handler (panel list + watcher)
# ----------------------------------------------------------------------------

_UI_SYNC_LAST = 0.0


@bpy.app.handlers.persistent
def _link_ui_sync(scene, depsgraph=None):
    """Throttled depsgraph tick: keep the panel list mirroring the active
    container and (when enabled) enforce the instance naming scheme.
    Writes only on actual change, so the tick it triggers itself finds
    nothing to do and the loop stops."""
    global _UI_SYNC_LAST
    now = time.monotonic()
    if now - _UI_SYNC_LAST < 0.2:
        return
    _UI_SYNC_LAST = now
    try:
        ctx = bpy.context
        scn = getattr(ctx, "scene", None)
        if scn is None or not hasattr(scn, "agr_link_groups"):
            return
        view_layer = getattr(ctx, "view_layer", None)
        active = view_layer.objects.active if view_layer else None
        _sync_group_list(scn, active)
        if len(scn.agr_link_watch_colls):
            if scn.agr_link_watch_enabled:
                _watch_tick(scn)
            _sync_watch_groups(scn)
    except Exception:
        pass   # a broken tick must never take the depsgraph down


# ----------------------------------------------------------------------------
# Panel
# ----------------------------------------------------------------------------

class AGR_OT_link_refresh(Operator):
    """Перепаковать цветовое зеркало контейнера по ТЕКУЩЕЙ геометрии"""
    bl_idname = "agr.link_refresh"
    bl_label = "Обновить память"
    bl_options = {'REGISTER', 'UNDO'}

    # HIDDEN: flipping this in the F9 redo panel would re-run an absorb +
    # repack across EVERY container in the scene - a structural change the
    # button the user actually pressed never offered
    scope: EnumProperty(
        name="Область",
        items=[('ACTIVE', "Активный контейнер", "Только активный объект"),
               ('ALL', "Все контейнеры сцены", "Каждый контейнер сцены")],
        default='ACTIVE',
        options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    @classmethod
    def description(cls, context, properties):
        if properties.scope == 'ALL':
            return ("Закрепить память ВСЕХ контейнеров сцены: зеркало каждого "
                    "перепаковывается по текущей геометрии. Без этого правка меша "
                    "может срезать заголовок зеркала, и FBX уедет к получателю "
                    "без памяти — .blend при этом выглядит здоровым")
        return ("Закрепить память активного контейнера: зеркало перепаковывается "
                "по текущей геометрии, контейнер снова готов к экспорту в FBX")

    def execute(self, context):
        if self.scope == 'ACTIVE':
            obj = context.active_object
            if not _has_link_data(obj):
                agr_report(self, 'ERROR', "❌ AGR Link: активный объект — не контейнер")
                return {'CANCELLED'}
            targets = [obj]
            aux_pool = [obj]
        else:
            targets = [o for o in context.scene.objects if _has_link_data(o)]
            aux_pool = [o for o in context.scene.objects
                        if o.type == 'MESH' and o.data is not None
                        and o.library is None and o.data.library is None
                        and not o.data.is_editmode]
            if not targets:
                agr_report(self, 'WARNING', "⚠️ AGR Link: в сцене нет контейнеров")
                return {'CANCELLED'}

        # the UDIM/atlas records ride on the same objects and break the
        # same way - "закрепить память" that left them dead reported a
        # success the FBX then contradicted
        aux_fixed, aux_failed = [], []
        for o in aux_pool:
            try:
                af, al = _sync_aux_records(o)
            except Exception as exc:
                af, al = [], [f"{o.name} ({exc})"]
            aux_fixed.extend(af)
            aux_failed.extend(al)

        def aux_suffix(level):
            msg = ""
            if aux_fixed:
                msg += f" | записи UDIM/атласов обновлены: {len(aux_fixed)}"
            if aux_failed:
                msg += f" | ⚠️ записи UDIM/атласов не перепакованы: {len(aux_failed)}"
                level = 'WARNING' if level == 'INFO' else level
            return msg, level

        if len(targets) == 1:
            stats = _refresh_container(context, targets[0])
            level, msg = _refresh_message(targets[0], stats)
            extra, level = aux_suffix(level)
            agr_report(self, level, msg + extra)
            result = _refresh_result(stats)
            if aux_fixed and result == {'CANCELLED'}:
                return {'FINISHED'}   # aux mirrors were rewritten - undo step needed
            return result

        done = no_mirror = 0
        skipped = []
        for obj in targets:
            stats = _refresh_container(context, obj)
            if stats["ok"]:
                done += 1
                if not stats["mirror_ok"]:
                    no_mirror += 1
            else:
                skipped.append(f"{obj.name} ({stats['reason']})")
        level = 'INFO' if done else 'WARNING'
        msg = (f"{'✅' if done else '⚠️'} AGR Link: память обновлена у {done} "
               f"из {len(targets)} контейнеров")
        if no_mirror:
            msg += f" | ⚠️ зеркало не записано: {no_mirror}"
            level = 'WARNING'
        if skipped:
            msg += (" | пропущено: " + ", ".join(skipped[:5])
                    + ("…" if len(skipped) > 5 else ""))
            level = 'WARNING'
        extra, level = aux_suffix(level)
        agr_report(self, level, msg + extra)
        # nothing repacked = nothing mutated: report it the way the
        # single-target path does instead of pushing an empty undo step
        return {'FINISHED'} if (done or aux_fixed) else {'CANCELLED'}


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
        table, extra_windows = None, 0
        if obj is not None and obj.type == 'MESH':
            table, extra_windows = _peek_merged(obj)

        if table is None:
            layout.label(text="Активный объект — не контейнер", icon='INFO')
        else:
            instances = table.get("instances", {})
            by_group = {}
            for inst in instances.values():
                by_group.setdefault(inst.get("group", 0), []).append(inst)
            n_groups = len(by_group)

            layout.separator()
            layout.label(text=f"Контейнер: {len(instances)} объектов, {n_groups} групп",
                         icon='PACKAGE')
            self._draw_mirror_state(layout, obj)
            if extra_windows:
                layout.label(text=f"Обычный Ctrl+J: влито контейнеров: {extra_windows}",
                             icon='INFO')
                layout.label(text="Память объединится при разборке или джойне")

            # the list itself is a Scene mirror kept fresh by the depsgraph
            # handler: dot = selection (click/drag), double click = rename
            scn = context.scene
            items = scn.agr_link_groups
            n_sel = sum(1 for it in items if it.is_selected)
            toolbar = layout.row(align=True)
            toolbar.operator("agr.link_groups_select_all", text="",
                             icon='CHECKBOX_DEHLT' if n_sel else 'CHECKBOX_HLT')
            toolbar.operator("agr.link_select_by_faces", text="", icon='FACESEL')
            toolbar.label(text=(f"Выбрано групп: {n_sel}" if n_sel
                                else "Клик по точке — выбор, двойной по имени — переименовать"))
            layout.template_list("AGR_UL_LinkGroupsList", "", scn, "agr_link_groups",
                                 scn, "agr_link_groups_index",
                                 rows=min(max(len(items), 3), 8))

            suffix = f"выбранное ({n_sel})" if n_sel else "всё"
            layout.operator("agr.link_separate_all", text=f"Разобрать {suffix}",
                            icon='OUTLINER_OB_GROUP_INSTANCE')
            col = layout.column(align=True)
            op = col.operator("agr.link_restore",
                              text=f"Восстановить {suffix} (мягко)", icon='LOOP_BACK')
            op.mode = 'SOFT'
            op.group_id = -1
            op = col.operator("agr.link_restore",
                              text=f"Восстановить {suffix} (жёстко)", icon='FILE_REFRESH')
            op.mode = 'HARD'
            op.group_id = -1
            layout.operator("agr.link_strip", icon='TRASH')

        # Maintenance stays visible even when the active object is not a
        # container - its whole point is catching the ones you forgot.
        layout.separator()
        col = layout.column(align=True)
        op = col.operator("agr.link_refresh", text="Обновить память всех контейнеров",
                          icon='FILE_REFRESH')
        op.scope = 'ALL'
        col.prop(context.scene, "agr_link_autosync")

    @staticmethod
    def _draw_mirror_state(layout, obj):
        """Warn when the memory will NOT survive an FBX export.  The rest
        of the panel reads the idprop, which mesh edits never touch — so
        without this line a decapitated mirror stays invisible until the
        RECEIVER imports the file and finds a container with no memory."""
        state = _mirror_state(obj)
        if state in (MIRROR_OK, MIRROR_UNKNOWN):
            # UNKNOWN = an edit BMesh is open, so the mirror simply cannot
            # be read; a red alert there would fire on every container the
            # user tabs into and would be pure noise
            return
        col = layout.column(align=True)
        row = col.row()
        if state == MIRROR_BROKEN:
            row.alert = True
            row.label(text="Зеркало разрушено — FBX уедет без памяти", icon='ERROR')
        elif state == MIRROR_STALE:
            row.alert = True
            row.label(text="Память устарела — меш правился после сборки", icon='ERROR')
        elif state == MIRROR_WINDOWS:
            # NOT an alert: the memory is still readable (merged windows of
            # a plain Ctrl+J, or a mirror an FBX triangulation permuted -
            # the loop-index rescue reads it) — the container is simply not
            # canonical yet, and the button below makes it one
            row.label(text="Память не закреплена (Ctrl+J / пересборка FBX)", icon='INFO')
        else:
            row.label(text="Зеркала нет — FBX не перенесёт память", icon='INFO')
        op = col.operator("agr.link_refresh", text="Закрепить память", icon='FILE_REFRESH')
        op.scope = 'ACTIVE'


class AGR_PT_LinkWatchPanel(Panel):
    """Слежка за инстансами: выбранные коллекции сканируются, и каждая
    группа линкованных копий держится в именах Имя_001, Имя_002, …"""
    bl_label = "Слежка за инстансами"
    bl_idname = "AGR_PT_link_watch_panel"
    bl_parent_id = "AGR_PT_link_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'AGR Tools'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        scn = context.scene

        layout.prop(scn, "agr_link_watch_enabled")

        row = layout.row()
        row.template_list("AGR_UL_LinkWatchColls", "", scn, "agr_link_watch_colls",
                          scn, "agr_link_watch_colls_index",
                          rows=min(max(len(scn.agr_link_watch_colls), 2), 4))
        side = row.column(align=True)
        side.operator("agr.link_watch_add", text="", icon='ADD')
        side.operator("agr.link_watch_remove", text="", icon='REMOVE')

        if scn.agr_link_watch_colls:
            if scn.agr_link_watch_groups:
                layout.label(text="Группы инстансов (двойной клик — переименовать):")
                layout.template_list("AGR_UL_LinkWatchGroups", "", scn,
                                     "agr_link_watch_groups",
                                     scn, "agr_link_watch_groups_index",
                                     rows=min(max(len(scn.agr_link_watch_groups), 3), 8))
            else:
                layout.label(text="Инстансов в коллекциях не найдено", icon='INFO')
            layout.operator("agr.link_watch_apply", icon='SORTALPHA')
        else:
            layout.label(text="Добавьте коллекции для слежки", icon='INFO')


# ----------------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------------

# ----------------------------------------------------------------------------
# Autosync: repack stale mirrors on save
# ----------------------------------------------------------------------------
# A repack costs ~0.4 µs per loop (0.5 s on a 1.3M-loop container), so it
# can NOT run per depsgraph tick - the work happens once in save_pre, the
# moment that is already slow and the one right before an export.  WHICH
# containers need it is NOT bookkept: it is read straight off the data (the
# mirror's own loop count against the mesh, ~12 µs per container), so an
# undo, a dev reload, a handler that was never installed or an edit made in
# a previous session cannot leave the autosync blind - and the repack
# cannot mark itself dirty either.  The scan costs ~10 ms on a 3000-object
# scene, all of it in the cheap _has_link_data filter.


def _drop_stale_handlers(handler_list, func_name):
    """Remove copies left by a dev reload: the reloaded module gets NEW
    function objects, so the usual identity check misses the old ones and
    the handler would fire twice per event."""
    for h in list(handler_list):
        if getattr(h, "__name__", None) == func_name:
            handler_list.remove(h)


# UDIM/atlas records share the dual-carrier design (idprop + color mirror,
# core/attr_store.py) and BREAK the same way: a mesh edit decapitates the
# mirror while the idprop keeps the .blend working - and the FBX ships
# without the record.  The link machinery above only watched its own
# namespace; these two used to go out dead in total silence (reproduced:
# delete the first face of a UDIM/atlas carrier -> both records survive in
# the .blend and are gone after a default FBX round trip).
_AUX_STORES = (("UDIM", UDIM_STORE), ("атлас", ATLAS_STORE))


def _sync_aux_records(obj):
    """Repack the UDIM/atlas record mirrors of one object when they no
    longer match the mesh.  Unlike the link container there is nothing to
    absorb or stamp - the record does not describe geometry - so a repack
    from the idprop (or, for a colors-only carrier, from an unambiguous
    mirror) is always the whole fix.  Buried windows of a merged-in carrier
    are left alone: one full-mesh repack would overwrite them, and
    scan_windows still reads them as they are.  Returns (fixed, failed)
    label lists.  Callers guard type/library/edit mode."""
    fixed, failed = [], []
    mesh = obj.data
    for label, store in _AUX_STORES:
        raw = obj.get(store.prop_key)
        if raw is None and mesh.attributes.get(store.prefix + "0") is None:
            continue
        state = _store_mirror_state(obj, store)
        if (state == MIRROR_OK and store.verify_frame(mesh) is not False
                and loop_index_is_canonical(mesh)):
            # same loop-index gate as the link autosync: upgrade pre-layer
            # records and re-canonicalise a permuted import on save
            continue
        if state == MIRROR_WINDOWS:
            # buried windows of a merged-in carrier stay untouched (one
            # full-mesh repack would overwrite them, scan_windows reads
            # them as they are) - but a single window that only reads
            # through the loop-index rescue is this mesh's OWN record
            # permuted by a triangulating FBX pipeline: repack THAT one.
            wins, ridx = store.scan_windows_ex(mesh)
            if ridx is None or len(wins) != 1 or wins[0][0] != 0:
                continue
            if store.write(obj, wins[0][2]):
                fixed.append(f"{obj.name} ({label})")
            else:
                failed.append(f"{obj.name} ({label})")
            continue
        record = store.parse_idprop(raw)
        if record is None:
            if store.count_window_candidates(mesh) > 1:
                failed.append(f"{obj.name} ({label})")
                continue
            record = store.read(obj)
        if record is None or not store.write(obj, record):
            failed.append(f"{obj.name} ({label})")
            continue
        fixed.append(f"{obj.name} ({label})")
    return fixed, failed


@bpy.app.handlers.persistent
def _link_save_pre(_dummy):
    """Repack the mirrors of stale containers right before the file is
    written.  The .blend never needed this - the idprop survives any edit -
    but the color mirror is the ONLY memory an FBX export carries, and a
    mesh edit can decapitate it.  Save is already the slow moment, so the
    repack hides there.  Deliberately NOT limited to objects edited in this
    session: a file that was already broken when it was opened comes out of
    the next save exportable."""
    scene = getattr(bpy.context, "scene", None)
    if scene is not None and not getattr(scene, "agr_link_autosync", True):
        return   # nothing is remembered: switching it back on catches up

    done = 0
    left, failed = [], []
    aux_done, aux_failed = 0, []
    for obj in bpy.data.objects:
        if obj.type != 'MESH' or obj.library is not None:
            continue
        mesh = obj.data
        if mesh is None or mesh.library is not None:
            continue
        if mesh.is_editmode:
            continue   # pre-edit snapshot - see _refresh_container
        # UDIM/atlas records ride on ANY mesh, container or not - sync them
        # before the link-only filter below can skip the object
        try:
            af, al = _sync_aux_records(obj)
            aux_done += len(af)
            aux_failed.extend(al)
        except Exception as exc:
            aux_failed.append(f"{obj.name} ({exc})")
        if not _has_link_data(obj):
            continue
        skip = _NO_AUTOSYNC.get(obj.name)
        if skip is not None and skip[0] == _mirror_fingerprint(obj):
            continue   # already established: not repackable as it stands
        state = _mirror_state(obj)
        if state == MIRROR_UNKNOWN:
            continue
        if (state == MIRROR_OK and _LINK_STORE.verify_frame(mesh) is not False
                and loop_index_is_canonical(mesh)):
            # the header probe cannot see payload corruption that keeps the
            # loop count intact (Sort Elements, delete a quad + build
            # another used to sail through as OK over a CRC-dead mirror),
            # and a legacy v1 frame carries no loop count at all - the deep
            # byte check runs HERE, once per save, never in poll()/draw().
            # The loop-index gate upgrades pre-2.8 mirrors (no layer yet -
            # they would not survive a triangulating FBX export) and
            # re-canonicalises a mesh that came in permuted from an import.
            continue
        if state == MIRROR_NONE and mesh.attributes.get(ATTR_NAME) is None:
            continue   # legacy container: idprop only, nothing to pack FROM
        try:
            stats = _refresh_container(bpy.context, obj, absorb=False)
            reason = (None if stats.get("ok") and stats.get("mirror_ok")
                      else stats.get("reason") or "mirror")
        except Exception as exc:
            agr_report(None, 'WARNING',
                       f"⚠️ AGR Link: не удалось обновить память '{obj.name}': {exc}")
            reason = "error"
        if reason is None:
            done += 1
            continue
        # _refresh_container drops the caches on its way out, so the mark
        # goes in AFTER it - keyed on the CURRENT fingerprint, i.e. the next
        # mesh edit retries this container all by itself
        _NO_AUTOSYNC[obj.name] = (_mirror_fingerprint(obj), reason)
        if reason in ("windows", "unmarked"):
            left.append(obj.name)
        else:
            failed.append(f"{obj.name} ({reason})")
    # agr_report, not print: this is the one failure the user MUST see - the
    # .blend keeps working off the idprop while the FBX the receiver opens
    # has no memory at all, and the system console is closed by default on
    # Windows.  The names go with it; a bare count leaves nothing to act on.
    if done:
        agr_report(None, 'INFO',
                   f"✅ AGR Link: перед сохранением обновлена память контейнеров: {done}")
    if left:
        agr_report(None, 'WARNING',
                   "⚠️ AGR Link: пропущены контейнеры с влитым обычным Ctrl+J — "
                   "нажмите «Закрепить память»: " + ", ".join(sorted(left)[:5])
                   + ("…" if len(left) > 5 else ""))
    if failed:
        agr_report(None, 'WARNING',
                   f"⚠️ AGR Link: зеркало НЕ перепаковано ({len(failed)}) — "
                   "FBX уедет без памяти: " + ", ".join(sorted(failed)[:5])
                   + ("…" if len(failed) > 5 else ""))
    if aux_done:
        agr_report(None, 'INFO',
                   f"✅ AGR: перед сохранением обновлены записи UDIM/атласов: {aux_done}")
    if aux_failed:
        agr_report(None, 'WARNING',
                   f"⚠️ AGR: записи UDIM/атласов НЕ перепакованы ({len(aux_failed)}) — "
                   "FBX уедет без них: " + ", ".join(sorted(aux_failed)[:5])
                   + ("…" if len(aux_failed) > 5 else ""))


def _clear_caches():
    """Every module-level cache in one place.  Called by register() as well
    as unregister(): a dev reload keeps these dicts alive across the module
    swap while the datablocks they describe may already be gone, and
    enumerating them inline is exactly what let one be forgotten before."""
    global _WATCH_LAST_FP, _UI_SYNC_LAST
    _NO_AUTOSYNC.clear()
    _TABLE_CACHE.clear()
    _MERGED_CACHE.clear()
    _WATCH_LAST_FP = None
    _UI_SYNC_LAST = 0.0


classes = (
    AGR_LinkGroupItem,
    AGR_LinkWatchColl,
    AGR_LinkWatchGroup,
    AGR_UL_LinkGroupsList,
    AGR_UL_LinkWatchColls,
    AGR_UL_LinkWatchGroups,
    AGR_OT_link_join,
    AGR_OT_link_extract_group,
    AGR_OT_link_separate_all,
    AGR_OT_link_restore,
    AGR_OT_link_rename_group,
    AGR_OT_link_groups_select_all,
    AGR_OT_link_select_by_faces,
    AGR_OT_link_watch_add,
    AGR_OT_link_watch_remove,
    AGR_OT_link_watch_apply,
    AGR_OT_link_watch_rename,
    AGR_OT_link_refresh,
    AGR_OT_link_strip,
    AGR_PT_LinkPanel,
    AGR_PT_LinkWatchPanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.agr_link_autosync = BoolProperty(
        name="Обновлять память при сохранении",
        description="Перед записью .blend перепаковать зеркало изменённых контейнеров: "
                    "иначе экспорт в FBX унесёт устаревшую или разрушенную память, "
                    "хотя сам .blend продолжит работать",
        default=True)
    bpy.types.Scene.agr_link_groups = CollectionProperty(type=AGR_LinkGroupItem)
    bpy.types.Scene.agr_link_groups_index = IntProperty(default=0)
    bpy.types.Scene.agr_link_groups_owner = StringProperty(default="")
    bpy.types.Scene.agr_link_watch_colls = CollectionProperty(type=AGR_LinkWatchColl)
    bpy.types.Scene.agr_link_watch_colls_index = IntProperty(default=0)
    bpy.types.Scene.agr_link_watch_groups = CollectionProperty(type=AGR_LinkWatchGroup)
    bpy.types.Scene.agr_link_watch_groups_index = IntProperty(default=0)
    bpy.types.Scene.agr_link_watch_enabled = BoolProperty(
        name="Следить и именовать автоматически",
        description="Держать имена линкованных копий в наблюдаемых коллекциях "
                    "по схеме Имя_001, Имя_002, … (у каждой группы своя нумерация); "
                    "без галки схему можно прогонять кнопкой",
        default=False)

    _clear_caches()
    # the depsgraph handler is GONE: with the verdict read from the mesh
    # there is nothing to mark, and a per-tick handler that only bookkeeps
    # is exactly what went stale behind undo and dev reloads
    _drop_stale_handlers(bpy.app.handlers.depsgraph_update_post, "_link_depsgraph_post")
    _drop_stale_handlers(bpy.app.handlers.depsgraph_update_post, "_link_ui_sync")
    bpy.app.handlers.depsgraph_update_post.append(_link_ui_sync)
    _drop_stale_handlers(bpy.app.handlers.save_pre, "_link_save_pre")
    bpy.app.handlers.save_pre.append(_link_save_pre)
    print("✅ AGR Link operators registered")


def unregister():
    _drop_stale_handlers(bpy.app.handlers.save_pre, "_link_save_pre")
    _drop_stale_handlers(bpy.app.handlers.depsgraph_update_post, "_link_ui_sync")
    _drop_stale_handlers(bpy.app.handlers.depsgraph_update_post, "_link_depsgraph_post")
    _clear_caches()
    for prop in ("agr_link_autosync", "agr_link_groups", "agr_link_groups_index",
                 "agr_link_groups_owner", "agr_link_watch_colls",
                 "agr_link_watch_colls_index", "agr_link_watch_groups",
                 "agr_link_watch_groups_index", "agr_link_watch_enabled"):
        if hasattr(bpy.types.Scene, prop):
            delattr(bpy.types.Scene, prop)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
