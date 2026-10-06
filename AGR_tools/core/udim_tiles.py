"""
UDIM tile-number math — the ONE formula shared by AGR UV and AGR UDIM.

Producer (uv stub/organic unwrap) and consumer (tile picker guards, revert,
layout editor) used to carry two different formulas (`ceil(u)` vs
`floor(u)+1`) that disagreed exactly on cell borders — and a face unwrapped
by `agr.uv_grid_unwrap` lands EXACTLY on 0.0/1.0, so the disagreement was
the common case, not the corner case.

No `bpy` import here on purpose: the module is pure arithmetic, so both
operator modules and headless tests can use it without a Blender context.
"""

import math

# Border tolerance: UV coordinates arrive from float32 mesh data, so an
# "exact" 1.0 is routinely 1.0000001 — without the epsilon such a vertex
# would open a new column of its own.
EPS = 1e-6


def uv_to_udim_number(u, v, eps=EPS):
    """Tile number for one UV point; None outside the valid zone
    (u < -eps, v < -eps, u > 10 + eps, v > 10 + eps).

    Right-closed columns/rows: an exact integer coordinate belongs to the
    tile it CLOSES (u == 1.0 -> column 0), which is what a face unwrapped
    into exactly one tile expects.
    """
    if u < -eps or v < -eps or u > 10 + eps or v > 10 + eps:
        return None
    col = max(0, int(math.floor(u - eps)))
    row = max(0, int(math.floor(v - eps)))
    return 1001 + min(col, 9) + row * 10


def face_tile_number(uvs, eps=EPS):
    """Tile of a face from its loop UVs, decided by the CENTROID.

    A per-loop majority vote breaks down on the face that fills a tile
    exactly: its four corners sit on four different tiles with one vote
    each, and the winner becomes whichever corner the loop order happens
    to start at.  The centroid is order-independent and lands inside the
    tile the face actually occupies.

    Returns None when there are no UVs or the centroid is outside the
    valid zone.
    """
    total_u = 0.0
    total_v = 0.0
    count = 0
    for uv in uvs:
        total_u += uv[0]
        total_v += uv[1]
        count += 1
    if not count:
        return None
    return uv_to_udim_number(total_u / count, total_v / count, eps)
