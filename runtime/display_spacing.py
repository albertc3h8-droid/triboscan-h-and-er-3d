"""Display-space spacing of the real RAW point cloud.

The RAW mode sphere diameter is derived from the **real measured 3D point
positions after the viewer's actual display transform** (the point actor scales
Z by ``z_scale``; X and Y stay physical nm).  It is a spacing marker, not an
uncertainty or a coverage estimate, and it never changes the measured data.

Rules (user-approved this round):

* positions are deduplicated by the **complete 3D coordinate**; two points at
  the same XY but a different real Z stay distinct positions;
* for every unique position the Euclidean distance to the nearest *different*
  unique position is measured in display space;
* ``median`` of those nearest-neighbour distances is ``m``;
  sphere **diameter = 0.5 * m** and **radius = 0.25 * m**;
* the median of *all pairwise* distances and any fixed fraction of an axis span
  are explicitly **not** used;
* the result is computed once per loaded dataset and cached (never per frame).

Memory is bounded: the exact pairwise search runs in row blocks, so the
temporary array is ``block x unique`` instead of ``unique x unique``.  No new
dependency is introduced (plain numpy, which the viewer already uses).  Time
is O(unique^2) for these measured datasets (hundreds to a few thousand
points); a hard safety cap prevents an accidental hang on pathological input.
"""
from __future__ import annotations

import numpy as np

METHOD = 'unique-3d-nearest-neighbour-median'
DISPLAY_TRANSFORM = 'x=x_nm, y=y_nm, z=z_nm*z_scale (the raw glyph actor scale)'
RADIUS_FACTOR = 0.25
DIAMETER_FACTOR = 0.5
# 20000 unique points -> 4e8 distance computations, still bounded in time for a
# one-off load; beyond this the exact search would make the viewer unresponsive.
MAX_UNIQUE_POINTS = 20000
# Target number of distance entries per block (bounded temporary memory).
BLOCK_ENTRIES = 500_000


class DisplaySpacingError(RuntimeError):
    """The display spacing cannot be computed from the real points."""


def display_positions(x_nm, y_nm, z_nm, z_scale: float) -> np.ndarray:
    """Real measured XYZ -> the positions the RAW display actor actually uses."""
    x = np.asarray(x_nm, dtype=np.float64).ravel()
    y = np.asarray(y_nm, dtype=np.float64).ravel()
    z = np.asarray(z_nm, dtype=np.float64).ravel()
    if not (x.size == y.size == z.size):
        raise DisplaySpacingError('x/y/z arrays have different lengths')
    return np.column_stack([x, y, z * float(z_scale)])


def _nearest_distances(points: np.ndarray) -> np.ndarray:
    """Exact nearest-different-position distance for each unique row.

    Row-blocked brute force: peak temporary memory is
    ``BLOCK_ENTRIES * 3 * 8`` bytes (plus one output block), never an
    ``N x N`` matrix for the whole cloud.
    """
    n = int(points.shape[0])
    out = np.empty(n, dtype=np.float64)
    block = max(1, min(n, int(BLOCK_ENTRIES // max(n, 1))))
    for start in range(0, n, block):
        stop = min(n, start + block)
        diff = points[start:stop, None, :] - points[None, :, :]
        sq = np.einsum('ijk,ijk->ij', diff, diff)
        local = np.arange(stop - start)
        sq[local, np.arange(start, stop)] = np.inf
        out[start:stop] = sq.min(axis=1)
    return np.sqrt(out)


def compute_spacing(positions) -> dict:
    """Statistics + the sphere size for one display-space point cloud.

    Returns a JSON-friendly dict.  Degenerate inputs are handled explicitly:
    non-finite positions are excluded (and counted); duplicate 3D positions are
    one unique position; fewer than two unique finite positions yields
    ``degenerate=True`` with ``radius_nm=None`` (never a fabricated size).
    """
    raw = np.asarray(positions, dtype=np.float64)
    if raw.ndim != 2 or raw.shape[1] != 3:
        raise DisplaySpacingError('positions must be an (N, 3) array')
    finite = np.isfinite(raw).all(axis=1)
    points = raw[finite]
    stats = {
        'method': METHOD,
        'display_transform': DISPLAY_TRANSFORM,
        'radius_factor': RADIUS_FACTOR,
        'diameter_factor': DIAMETER_FACTOR,
        'total_points': int(raw.shape[0]),
        'finite_points': int(points.shape[0]),
        'non_finite_points': int((~finite).sum()),
        'unique_positions': 0,
        'duplicate_points': 0,
        'duplicate_positions': 0,
        'max_multiplicity': 0,
        'nearest_distances_nm': [],
        'min_nearest_nm': None,
        'max_nearest_nm': None,
        'median_nearest_nm': None,
        'diameter_nm': None,
        'radius_nm': None,
        'degenerate': True,
        'degenerate_reason': 'no finite positions',
    }
    if points.shape[0] == 0:
        return stats
    unique, inverse, counts = np.unique(points, axis=0, return_inverse=True,
                                        return_counts=True)
    multiplicity = np.bincount(inverse, minlength=int(unique.shape[0]))
    stats.update({
        'unique_positions': int(unique.shape[0]),
        'duplicate_points': int(points.shape[0] - unique.shape[0]),
        'duplicate_positions': int(np.count_nonzero(counts > 1)),
        'max_multiplicity': int(counts.max()),
    })
    if unique.shape[0] < 2:
        stats['degenerate_reason'] = (
            'fewer than 2 unique finite 3D positions; no nearest-neighbour '
            'distance exists, so no sphere size is invented')
        return stats
    if unique.shape[0] > MAX_UNIQUE_POINTS:
        raise DisplaySpacingError(
            f'{unique.shape[0]} unique positions exceed the exact nearest-'
            f'neighbour safety cap ({MAX_UNIQUE_POINTS}); refusing to invent a '
            f'sphere size')
    nearest = _nearest_distances(unique)
    median = float(np.median(nearest))
    stats.update({
        'nearest_distances_nm': [float(v) for v in nearest],
        'min_nearest_nm': float(nearest.min()),
        'max_nearest_nm': float(nearest.max()),
        'median_nearest_nm': median,
        'diameter_nm': median * DIAMETER_FACTOR,
        'radius_nm': median * RADIUS_FACTOR,
        'degenerate': False,
        'degenerate_reason': '',
        'per_point_multiplicity': [int(v) for v in multiplicity],
    })
    return stats
