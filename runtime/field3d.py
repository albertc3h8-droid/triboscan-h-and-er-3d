"""VTK-based true 3D scattered field and axis slices.

Method (documented, no fake measurement points):
1. The real XYZ samples are translated by the minimum corner *only* (reversible,
   same units, no per-axis normalisation or scaling) to keep Delaunay numerics
   well conditioned.
2. ``vtkDelaunay3D`` builds the 3D triangulation (linear, no smoothing) of those
   real points; a tiny relative tolerance avoids merging real 10-30 nm depth gaps.
3. A regular ``vtkImageData`` field is built over the *actual* active data extent
   (no convex-hull padding, no nearest-neighbour fill) at a bounded, configurable
   physical step (default 0.5 um in X/Y, 2 nm in Z) and sampled with
   ``vtkProbeFilter``.  ``vtkValidPointMask`` gives the true 3D convex-hull
   support: outside it the volume is NaN, never back-filled.
4. A Z slice is the trilinear cut of that 3D field (X/Y slices are provided by
   the same API).  A slice exactly on a volume node reads only that node/layer
   (its own support); a sample between layers is valid only when both bracketing
   nodes are inside the support.  Unsupported neighbours never contaminate a
   supported sample (no 0*NaN blend).
5. For display, slice_planes_cubic can rebuild a Z slice directly from the
   measured columns with a tensor-product natural cubic spline in X/Y and PCHIP
   along depth; it passes through every measurement and is not a blur.  The
   stored field and every export stay the linear result from step 4; an
   irregular X/Y grid or a depth outside a column falls back to step 4.

The stored field is built once per load; the linear slice only re-samples the
existing volume.  The cubic display slice rebuilds from the measured columns on
each change (about 10 ms for the real 10 x 10 x 10 sample).
"""
from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np
import vtk
from vtk.util.numpy_support import vtk_to_numpy

DEFAULT_XY_STEP_NM = 500.0     # 0.5 um
DEFAULT_Z_STEP_NM = 2.0
MAX_AXIS_SAMPLES = 1024
MAX_VOLUME_SAMPLES = 6_000_000

#: Display-only slice interpolation.  The stored field (and every export)
#: stays the raw linear Delaunay/probe result; the viewer can rebuild a Z
#: slice directly from the measured columns instead.  'cubic' uses a
#: tensor-product natural cubic spline in X/Y (C2, interpolating) and a
#: shape-preserving PCHIP curve along each measurement column.  'tps' uses a
#: thin-plate spline over the same measured X/Y lattice: it is isotropic, so a
#: single peak does not grow the axis-aligned cross of a tensor-product
#: interpolant.  Both pass through every measured value and never replace the
#: data by a local average.  'linear' is the exact old behaviour.
SLICE_INTERPOLATION_MODES = ('linear', 'cubic', 'tps')


class FieldError(ValueError):
    """Friendly field construction/slice error (reported, never hidden)."""


@dataclass(frozen=True)
class FieldGrid:
    x0_nm: float
    y0_nm: float
    z0_nm: float
    dx_nm: float
    dy_nm: float
    dz_nm: float
    nx: int
    ny: int
    nz: int
    valid: np.ndarray        # bool (nz, ny, nx), read-only
    er_gpa: np.ndarray       # float64 (nz, ny, nx), NaN outside support
    h_gpa: np.ndarray
    point_count: int
    delaunay_cells: int
    xy_step_requested_nm: float
    z_step_requested_nm: float

    @property
    def shape(self) -> tuple[int, int, int]:
        return self.nz, self.ny, self.nx

    @property
    def x_nm(self) -> np.ndarray:
        return self.x0_nm + np.arange(self.nx) * self.dx_nm

    @property
    def y_nm(self) -> np.ndarray:
        return self.y0_nm + np.arange(self.ny) * self.dy_nm

    @property
    def z_nm(self) -> np.ndarray:
        return self.z0_nm + np.arange(self.nz) * self.dz_nm

    @property
    def bounds_nm(self) -> tuple[float, float, float, float, float, float]:
        return (self.x0_nm, self.x0_nm + (self.nx - 1) * self.dx_nm,
                self.y0_nm, self.y0_nm + (self.ny - 1) * self.dy_nm,
                self.z0_nm, self.z0_nm + (self.nz - 1) * self.dz_nm)

    @property
    def valid_fraction(self) -> float:
        return float(self.valid.mean())

    def metadata(self) -> dict:
        """Auditable grid/method/support report for the UI and tests."""
        return {
            'method': 'VTK vtkDelaunay3D (linear 3D triangulation) + '
                      'vtkProbeFilter onto a regular volume; support = '
                      'vtkValidPointMask (3D convex hull), NaN outside',
            'translation_nm': [self.x0_nm, self.y0_nm, self.z0_nm],
            'translation_only_reversible': True,
            'per_axis_normalised': False,
            'units': {'x': 'nm', 'y': 'nm', 'z': 'nm', 'Er': 'GPa', 'H': 'GPa'},
            'Nx': self.nx, 'Ny': self.ny, 'Nz': self.nz,
            'dx_nm': self.dx_nm, 'dy_nm': self.dy_nm, 'dz_nm': self.dz_nm,
            'xy_step_requested_nm': self.xy_step_requested_nm,
            'z_step_requested_nm': self.z_step_requested_nm,
            'bounds_nm': list(self.bounds_nm),
            'point_count': self.point_count,
            'delaunay_tetrahedra': self.delaunay_cells,
            'valid_fraction': self.valid_fraction,
            'outside_support': 'NaN / not rendered; no nearest-neighbour fill',
        }


@dataclass(frozen=True)
class Plane:
    axis: str                 # 'x' | 'y' | 'z'
    position_nm: float
    u_nm: np.ndarray          # physical absolute u coordinates (nm)
    v_nm: np.ndarray          # physical absolute v coordinates (nm)
    values: np.ndarray        # (len(v), len(u)); NaN outside support
    mask: np.ndarray          # bool (len(v), len(u))
    u_label: str
    v_label: str
    method: str


def build_field(x_nm, y_nm, z_nm, er_gpa, h_gpa,
                xy_step_nm: float = DEFAULT_XY_STEP_NM,
                z_step_nm: float = DEFAULT_Z_STEP_NM) -> FieldGrid:
    """Build one 3D field from real points; never creates synthetic samples."""
    x = np.asarray(x_nm, dtype=np.float64)
    y = np.asarray(y_nm, dtype=np.float64)
    z = np.asarray(z_nm, dtype=np.float64)
    er = np.asarray(er_gpa, dtype=np.float64)
    h = np.asarray(h_gpa, dtype=np.float64)
    if not (x.shape == y.shape == z.shape == er.shape == h.shape):
        raise FieldError('Point coordinate/value arrays have different lengths')
    if x.size < 4:
        raise FieldError(f'Need at least 4 real points for a 3D field, got {x.size}')
    if not (np.isfinite(x).all() and np.isfinite(y).all() and np.isfinite(z).all()
            and np.isfinite(er).all() and np.isfinite(h).all()):
        raise FieldError('Non-finite point coordinate/value')
    for name, step in (('xy_step_nm', xy_step_nm), ('z_step_nm', z_step_nm)):
        if not math.isfinite(step) or step <= 0:
            raise FieldError(f'{name} must be a positive finite number')

    x0, y0, z0 = float(x.min()), float(y.min()), float(z.min())
    sx, sy, sz = float(x.max()) - x0, float(y.max()) - y0, float(z.max()) - z0
    if min(sx, sy, sz) <= 0:
        raise FieldError('Degenerate 3D extent (zero span in X, Y or Z); '
                         'a 3D field needs non-coplanar real points')

    def axis_samples(span: float, step: float, name: str) -> tuple[int, float]:
        count = int(math.ceil(span / step)) + 1
        count = max(2, count)
        if count > MAX_AXIS_SAMPLES:
            raise FieldError(f'{name} grid would need {count} samples '
                             f'(> {MAX_AXIS_SAMPLES}); increase the physical step')
        return count, span / (count - 1)

    nx, dx = axis_samples(sx, float(xy_step_nm), 'X')
    ny, dy = axis_samples(sy, float(xy_step_nm), 'Y')
    nz, dz = axis_samples(sz, float(z_step_nm), 'Z')
    total = nx * ny * nz
    if total > MAX_VOLUME_SAMPLES:
        raise FieldError(f'Volume would need {total} samples '
                         f'(> {MAX_VOLUME_SAMPLES}); increase the physical step')

    # Translation only: x' = x - x0, y' = y - y0, z' = z - z0. Units and topology
    # are untouched; every output is mapped back to absolute instrument nm.
    poly = vtk.vtkPolyData()
    points = vtk.vtkPoints()
    points.SetNumberOfPoints(x.size)
    for index, (px, py, pz) in enumerate(zip(x - x0, y - y0, z - z0)):
        points.SetPoint(index, float(px), float(py), float(pz))
    poly.SetPoints(points)
    for name, values in (('Er', er), ('H', h)):
        vtk_array = vtk.vtkDoubleArray()
        vtk_array.SetName(name)
        vtk_array.SetNumberOfValues(x.size)
        for index, value in enumerate(values):
            vtk_array.SetValue(index, float(value))
        poly.GetPointData().AddArray(vtk_array)

    delaunay = vtk.vtkDelaunay3D()
    delaunay.SetInputData(poly)
    delaunay.SetTolerance(1e-6)   # relative; avoids merging real 10-30 nm z gaps
    delaunay.SetAlpha(0.0)
    delaunay.BoundingTriangulationOff()
    delaunay.Update()
    triangulation = delaunay.GetOutput()
    cells = int(triangulation.GetNumberOfCells())
    if cells <= 0:
        raise FieldError('VTK Delaunay produced no tetrahedra; the active points '
                         'may be coplanar/degenerate. No nearest-neighbour or '
                         'layer-average fallback is used.')

    image = vtk.vtkImageData()
    image.SetOrigin(0.0, 0.0, 0.0)
    image.SetSpacing(dx, dy, dz)
    image.SetDimensions(nx, ny, nz)
    probe = vtk.vtkProbeFilter()
    probe.SetInputData(image)
    probe.SetSourceData(triangulation)
    probe.Update()
    output = probe.GetOutput()
    if output.GetNumberOfPoints() != nx * ny * nz:
        raise FieldError('VTK probe returned an unexpected volume size')
    point_data = output.GetPointData()
    valid_array = point_data.GetArray('vtkValidPointMask')
    er_array = point_data.GetArray('Er')
    h_array = point_data.GetArray('H')
    if valid_array is None or er_array is None or h_array is None:
        raise FieldError('VTK probe did not return the expected mask/Er/H arrays')

    valid = vtk_to_numpy(valid_array).astype(bool).reshape(nz, ny, nx)
    er_volume = vtk_to_numpy(er_array).astype(np.float64).reshape(nz, ny, nx)
    h_volume = vtk_to_numpy(h_array).astype(np.float64).reshape(nz, ny, nx)
    er_volume[~valid] = np.nan
    h_volume[~valid] = np.nan
    for array in (valid, er_volume, h_volume):
        array.setflags(write=False)

    return FieldGrid(
        x0_nm=x0, y0_nm=y0, z0_nm=z0, dx_nm=dx, dy_nm=dy, dz_nm=dz,
        nx=nx, ny=ny, nz=nz, valid=valid, er_gpa=er_volume, h_gpa=h_volume,
        point_count=int(x.size), delaunay_cells=cells,
        xy_step_requested_nm=float(xy_step_nm),
        z_step_requested_nm=float(z_step_nm))


def _axis_position(field: FieldGrid, axis: str, position_nm: float):
    """Return (k0, k1, t) for the bracketing volume layers, or raise."""
    if axis == 'x':
        origin, step, count = field.x0_nm, field.dx_nm, field.nx
    elif axis == 'y':
        origin, step, count = field.y0_nm, field.dy_nm, field.ny
    elif axis == 'z':
        origin, step, count = field.z0_nm, field.dz_nm, field.nz
    else:
        raise FieldError(f'Unknown slice axis: {axis!r} (use x, y or z)')
    if not math.isfinite(position_nm):
        raise FieldError('Slice position must be finite')
    high = origin + (count - 1) * step
    tolerance = max(step * 1e-9, 1e-9)
    if position_nm < origin - tolerance or position_nm > high + tolerance:
        raise FieldError(f'Slice position {position_nm:.6f} nm is outside the '
                         f'field {axis} range {origin:.6f}..{high:.6f} nm')
    position_nm = min(max(position_nm, origin), high)
    index = (position_nm - origin) / step
    if count == 1:
        return 0, 0, 0.0
    k0 = int(math.floor(index))
    k0 = min(max(k0, 0), count - 2)
    t = float(min(max(index - k0, 0.0), 1.0))
    return k0, k0 + 1, t


_SLICE_METHOD = ('trilinear cut of the VTK Delaunay-probed regular volume; an '
                 'exact node reads only that node/layer, otherwise a sample is '
                 'valid only when both bracketing nodes are inside the 3D '
                 'convex-hull support; no nearest-neighbour fill')

_NODE_EPS = 1e-9  # fraction of one cell treated as "exact volume node"


def _node_layer(field: FieldGrid, axis: str, layer: int):
    """Valid/Er/H arrays of one volume layer, oriented as (v, u)."""
    if axis == 'z':
        return (field.valid[layer], field.er_gpa[layer],
                field.h_gpa[layer], field.x_nm, field.y_nm,
                'X (nm)', 'Y (nm)')
    if axis == 'x':
        # volume arrays are (z, y): already (v=z, u=y)
        return (field.valid[:, :, layer], field.er_gpa[:, :, layer],
                field.h_gpa[:, :, layer], field.y_nm, field.z_nm,
                'Y (nm)', 'Z / heff (nm)')
    # volume arrays are (z, x): already (v=z, u=x)
    return (field.valid[:, layer, :], field.er_gpa[:, layer, :],
            field.h_gpa[:, layer, :], field.x_nm, field.z_nm,
            'X (nm)', 'Z / heff (nm)')


_PCHIP_EPS = 1e-12


def _pchip_slopes(x, y):
    """Fritsch-Carlson monotone cubic slopes for strictly increasing x."""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    n = x.size
    if n < 2:
        raise FieldError('A measurement column needs at least two depths')
    h = np.diff(x)
    if (h <= 0).any():
        raise FieldError('Measurement depths must be strictly increasing')
    delta = np.diff(y) / h
    m = np.zeros(n, dtype=np.float64)
    if n == 2:
        m[:] = delta[0]
        return m
    for k in range(1, n - 1):
        if delta[k - 1] * delta[k] <= 0:
            m[k] = 0.0
        else:
            w1 = 2.0 * h[k] + h[k - 1]
            w2 = h[k] + 2.0 * h[k - 1]
            m[k] = (w1 + w2) / (w1 / delta[k - 1] + w2 / delta[k])
    m[0] = ((2.0 * h[0] + h[1]) * delta[0] - h[0] * delta[1]) / (h[0] + h[1])
    m[-1] = ((2.0 * h[-1] + h[-2]) * delta[-1] - h[-1] * delta[-2]) / (h[-1] + h[-2])
    if m[0] * delta[0] <= 0:
        m[0] = 0.0
    if m[-1] * delta[-1] <= 0:
        m[-1] = 0.0
    return m


def _pchip_value(x, y, m, xq):
    """Evaluate a PCHIP segment; NaN outside the measured depth range."""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    m = np.asarray(m, dtype=np.float64)
    xq = float(xq)
    if xq < x[0] or xq > x[-1]:
        return float('nan')
    k = int(np.searchsorted(x, xq, side='right')) - 1
    k = min(max(k, 0), x.size - 2)
    h = x[k + 1] - x[k]
    t = (xq - x[k]) / h
    h00 = 2.0 * t ** 3 - 3.0 * t ** 2 + 1.0
    h10 = t ** 3 - 2.0 * t ** 2 + t
    h01 = -2.0 * t ** 3 + 3.0 * t ** 2
    h11 = t ** 3 - t ** 2
    return float(h00 * y[k] + h10 * h * m[k]
                 + h01 * y[k + 1] + h11 * h * m[k + 1])


def _natural_cubic_curvature(x, y):
    """Second derivatives of the natural cubic spline through (x, y)."""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    n = x.size
    if n < 3:
        return np.zeros(n, dtype=np.float64)
    h = np.diff(x)
    alpha = np.zeros(n, dtype=np.float64)
    for i in range(1, n - 1):
        alpha[i] = (3.0 / h[i]) * (y[i + 1] - y[i]) \
            - (3.0 / h[i - 1]) * (y[i] - y[i - 1])
    l = np.ones(n, dtype=np.float64)
    mu = np.zeros(n, dtype=np.float64)
    z = np.zeros(n, dtype=np.float64)
    for i in range(1, n - 1):
        l[i] = 2.0 * (x[i + 1] - x[i - 1]) - h[i - 1] * mu[i - 1]
        if abs(l[i]) < _PCHIP_EPS:
            return np.zeros(n, dtype=np.float64)
        mu[i] = h[i] / l[i]
        z[i] = (alpha[i] - h[i - 1] * z[i - 1]) / l[i]
    m = np.zeros(n, dtype=np.float64)
    for j in range(n - 2, -1, -1):
        m[j] = z[j] - mu[j] * m[j + 1]
    return m


def _natural_cubic_values(x, y, m, xq):
    """Vectorised natural-cubic evaluation; NaN outside [x[0], x[-1]]."""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    m = np.asarray(m, dtype=np.float64)
    q = np.asarray(xq, dtype=np.float64)
    out = np.full(q.shape, np.nan, dtype=np.float64)
    inside = (q >= x[0]) & (q <= x[-1])
    if not inside.any():
        return out
    k = np.searchsorted(x, q[inside], side='right') - 1
    k = np.clip(k, 0, x.size - 2)
    h = x[k + 1] - x[k]
    a = (x[k + 1] - q[inside]) / h
    b = (q[inside] - x[k]) / h
    out[inside] = (a * y[k] + b * y[k + 1]
                   + ((a ** 3 - a) * m[k] + (b ** 3 - b) * m[k + 1])
                   * h * h / 6.0)
    return out


def _regular_columns(x_nm, y_nm, z_nm, er_gpa, h_gpa):
    """Group measurements into a full regular X/Y grid of depth columns.

    The real sample is a 10 x 10 lattice (3 um pitch), 10 depths per column.
    Returns (xs, ys, columns) where columns[j][i] is (z, er, h) sorted by
    depth, or None when the XY positions are not a complete Cartesian grid
    (the caller then keeps the linear field).
    """
    x = np.asarray(x_nm, dtype=np.float64)
    y = np.asarray(y_nm, dtype=np.float64)
    z = np.asarray(z_nm, dtype=np.float64)
    er = np.asarray(er_gpa, dtype=np.float64)
    h = np.asarray(h_gpa, dtype=np.float64)
    xs = np.unique(x)
    ys = np.unique(y)
    if xs.size < 2 or ys.size < 2:
        return None
    ix = np.searchsorted(xs, x)
    iy = np.searchsorted(ys, y)
    if (np.abs(xs[ix] - x) > 1e-6).any() or (np.abs(ys[iy] - y) > 1e-6).any():
        return None
    buckets = {}
    for k in range(x.size):
        buckets.setdefault((int(ix[k]), int(iy[k])), []).append(
            (float(z[k]), float(er[k]), float(h[k])))
    if len(buckets) != xs.size * ys.size:
        return None
    columns = [[None] * xs.size for _ in range(ys.size)]
    for (i, j), values in buckets.items():
        if len(values) < 2:
            return None
        values.sort(key=lambda item: item[0])
        zz = np.asarray([v[0] for v in values], dtype=np.float64)
        if (np.diff(zz) <= 0).any():
            return None
        columns[j][i] = (zz,
                         np.asarray([v[1] for v in values], dtype=np.float64),
                         np.asarray([v[2] for v in values], dtype=np.float64))
    for row in columns:
        if any(cell is None for cell in row):
            return None
    return xs, ys, columns


def _measured_node_grid(dataset, position_nm):
    """PCHIP value of every measured X/Y column at one depth.

    Returns (xs, ys, grid_er, grid_h) on the complete 10 x 10 lattice, or
    None when the XY positions are not a full grid or the depth is outside any
    column's measured range.  Both display interpolants share this step.
    """
    x = np.asarray(getattr(dataset, 'x_nm', []), dtype=np.float64)
    y = np.asarray(getattr(dataset, 'y_nm', []), dtype=np.float64)
    z = np.asarray(getattr(dataset, 'z_nm', []), dtype=np.float64)
    er = np.asarray(getattr(dataset, 'er_gpa', []), dtype=np.float64)
    h = np.asarray(getattr(dataset, 'h_gpa', []), dtype=np.float64)
    if not (x.size and x.shape == y.shape == z.shape == er.shape == h.shape):
        return None
    grouped = _regular_columns(x, y, z, er, h)
    if grouped is None:
        return None
    xs, ys, columns = grouped
    depth = float(position_nm)
    ny, nx = len(ys), len(xs)
    grid_er = np.full((ny, nx), np.nan, dtype=np.float64)
    grid_h = np.full((ny, nx), np.nan, dtype=np.float64)
    for j in range(ny):
        for i in range(nx):
            zz, ee, hh = columns[j][i]
            if depth < zz[0] or depth > zz[-1]:
                return None
            grid_er[j, i] = _pchip_value(zz, ee, _pchip_slopes(zz, ee), depth)
            grid_h[j, i] = _pchip_value(zz, hh, _pchip_slopes(zz, hh), depth)
    if not (np.isfinite(grid_er).all() and np.isfinite(grid_h).all()):
        return None
    return xs, ys, grid_er, grid_h


_CUBIC_SLICE_METHOD = (
    'natural cubic spline in X/Y (C2, interpolating) + PCHIP along each '
    'measured depth column; passes through every measured value, no '
    'neighbourhood averaging')


def slice_planes_cubic(dataset, field, position_nm: float):
    """Build both Z slices with a cubic interpolant of the measured points.

    Only valid for a complete regular X/Y grid and a depth that lies inside
    every column's measured range; otherwise None tells the caller to use the
    stored linear field.  The interpolant is exact at the measured points:
    PCHIP reproduces each column value at its own depths and the tensor-product
    natural cubic reproduces every node value, so no measurement is replaced
    by a local average.
    """
    grouped = _measured_node_grid(dataset, position_nm)
    if grouped is None:
        return None
    xs, ys, grid_er, grid_h = grouped
    depth = float(position_nm)
    ny, nx = len(ys), len(xs)

    gx = np.asarray(getattr(field, 'x_nm'), dtype=np.float64)
    gy = np.asarray(getattr(field, 'y_nm'), dtype=np.float64)
    if gx.size < 2 or gy.size < 2:
        return None
    if gx[0] < xs[0] - 1e-6 or gx[-1] > xs[-1] + 1e-6 \
            or gy[0] < ys[0] - 1e-6 or gy[-1] > ys[-1] + 1e-6:
        return None

    def evaluate(values):
        # First along Y at each measured X node, then along X at each target Y.
        along_y = np.full((gy.size, nx), np.nan, dtype=np.float64)
        for i in range(nx):
            m = _natural_cubic_curvature(ys, values[:, i])
            along_y[:, i] = _natural_cubic_values(ys, values[:, i], m, gy)
        out = np.full((gy.size, gx.size), np.nan, dtype=np.float64)
        for j in range(gy.size):
            m = _natural_cubic_curvature(xs, along_y[j])
            out[j] = _natural_cubic_values(xs, along_y[j], m, gx)
        return out

    er_out = evaluate(grid_er)
    h_out = evaluate(grid_h)
    valid = np.isfinite(er_out) & np.isfinite(h_out)
    if not valid.any():
        return None
    common = dict(axis='z', position_nm=depth, u_nm=gx, v_nm=gy,
                  mask=valid, u_label='X (nm)', v_label='Y (nm)',
                  method=_CUBIC_SLICE_METHOD)
    return {
        'Er': Plane(values=np.where(valid, er_out, np.nan), **common),
        'H': Plane(values=np.where(valid, h_out, np.nan), **common),
    }


_TPS_SLICE_METHOD = (
    'thin-plate spline (r^2 log r) fitted to the regular measured X/Y lattice; '
    'isotropic, passes through every measured value, no neighbourhood averaging')


_TPS_OPERATOR_CACHE = {}
_TPS_CACHE_LIMIT = 4


def _tps_operator(xs, ys, gx, gy):
    """Precompute the thin-plate spline evaluation matrix for one grid.

    The spline is fitted on coordinates centred and scaled by half their
    largest span, which keeps the linear system well conditioned.  The matrix
    depends only on the geometry, so every depth reuses it and solving for the
    measured values costs one small matrix-vector product.
    """
    key = (xs.tobytes(), ys.tobytes(), gx.tobytes(), gy.tobytes())
    cached = _TPS_OPERATOR_CACHE.get(key)
    if cached is not None:
        return cached
    span = max(float(xs[-1] - xs[0]), float(ys[-1] - ys[0]))
    if not np.isfinite(span) or span <= 0.0:
        return None
    cx = 0.5 * (float(xs[0]) + float(xs[-1]))
    cy = 0.5 * (float(ys[0]) + float(ys[-1]))
    scale = 0.5 * span
    node_x = (xs - cx) / scale
    node_y = (ys - cy) / scale
    qx = (gx - cx) / scale
    qy = (gy - cy) / scale
    nx, ny = node_x.size, node_y.size
    # C-order flattening of the (ny, nx) node grid: X varies fastest.
    px = np.tile(node_x, ny)
    py = np.repeat(node_y, nx)
    n = px.size
    dx = px[:, None] - px[None, :]
    dy = py[:, None] - py[None, :]
    r2 = dx * dx + dy * dy
    with np.errstate(divide='ignore', invalid='ignore'):
        kernel = np.where(r2 > 0.0, 0.5 * r2 * np.log(r2), 0.0)
    poly = np.column_stack((np.ones(n), px, py))
    system = np.zeros((n + 3, n + 3), dtype=np.float64)
    system[:n, :n] = kernel
    system[:n, n:] = poly
    system[n:, :n] = poly.T
    try:
        inverse = np.linalg.inv(system)
    except np.linalg.LinAlgError:
        return None
    qxx, qyy = np.meshgrid(qx, qy)
    qpx = qxx.ravel()
    qpy = qyy.ravel()
    qdx = qpx[:, None] - px[None, :]
    qdy = qpy[:, None] - py[None, :]
    qr2 = qdx * qdx + qdy * qdy
    with np.errstate(divide='ignore', invalid='ignore'):
        qkernel = np.where(qr2 > 0.0, 0.5 * qr2 * np.log(qr2), 0.0)
    qpoly = np.column_stack((np.ones(qpx.size), qpx, qpy))
    matrix = np.column_stack((qkernel, qpoly)) @ inverse
    operator = (matrix, gx, gy, n)
    if len(_TPS_OPERATOR_CACHE) >= _TPS_CACHE_LIMIT:
        _TPS_OPERATOR_CACHE.pop(next(iter(_TPS_OPERATOR_CACHE)))
    _TPS_OPERATOR_CACHE[key] = operator
    return operator


def _tps_evaluate(operator, values):
    matrix, gx, gy, n = operator
    rhs = np.zeros(n + 3, dtype=np.float64)
    rhs[:n] = np.asarray(values, dtype=np.float64).ravel()
    return (matrix @ rhs).reshape(gy.size, gx.size)


def slice_planes_tps(dataset, field, position_nm: float):
    """Build both Z slices with an isotropic thin-plate spline.

    Same validity rules as the cubic slice: complete regular X/Y lattice and a
    depth inside every column's measured range, otherwise None falls back to
    the stored linear field.  It reproduces every measured node exactly.
    """
    grouped = _measured_node_grid(dataset, position_nm)
    if grouped is None:
        return None
    xs, ys, grid_er, grid_h = grouped
    gx = np.asarray(getattr(field, 'x_nm'), dtype=np.float64)
    gy = np.asarray(getattr(field, 'y_nm'), dtype=np.float64)
    if gx.size < 2 or gy.size < 2:
        return None
    if gx[0] < xs[0] - 1e-6 or gx[-1] > xs[-1] + 1e-6 \
            or gy[0] < ys[0] - 1e-6 or gy[-1] > ys[-1] + 1e-6:
        return None
    operator = _tps_operator(xs, ys, gx, gy)
    if operator is None:
        return None
    er_out = _tps_evaluate(operator, grid_er)
    h_out = _tps_evaluate(operator, grid_h)
    valid = np.isfinite(er_out) & np.isfinite(h_out)
    if not valid.any():
        return None
    common = dict(axis='z', position_nm=float(position_nm), u_nm=gx, v_nm=gy,
                  mask=valid, u_label='X (nm)', v_label='Y (nm)',
                  method=_TPS_SLICE_METHOD)
    return {
        'Er': Plane(values=np.where(valid, er_out, np.nan), **common),
        'H': Plane(values=np.where(valid, h_out, np.nan), **common),
    }


def _cut(field: FieldGrid, axis: str, position_nm: float):
    axis = str(axis).strip().lower()
    k0, k1, t = _axis_position(field, axis, float(position_nm))
    v0, e0, h0, u, v, u_label, v_label = _node_layer(field, axis, k0)
    # A slice exactly on a node must not depend on - or be poisoned by - the
    # neighbouring node/layer (which may be outside the support).
    if t <= _NODE_EPS:
        return u, v, e0, h0, v0, u_label, v_label, axis
    if (1.0 - t) <= _NODE_EPS:
        v1, e1, h1, u, v, u_label, v_label = _node_layer(field, axis, k1)
        return u, v, e1, h1, v1, u_label, v_label, axis
    v1, e1, h1, _, _, _, _ = _node_layer(field, axis, k1)
    valid = v0 & v1
    # Only combine where both bracketing nodes are valid: NaN/unsupported
    # neighbours can never contaminate a supported sample (0 * NaN).
    er = np.where(valid, e0 * (1.0 - t) + e1 * t, np.nan)
    h = np.where(valid, h0 * (1.0 - t) + h1 * t, np.nan)
    return u, v, er, h, valid, u_label, v_label, axis


def slice_plane(field: FieldGrid, axis: str, position_nm: float,
                metric: str = 'Er') -> Plane:
    """Cut an X, Y or Z plane from the already-built 3D field.

    Only Z is exposed in this trial's UI; X/Y are provided for the future
    interface and are covered by the verification script.  This is the raw
    linear cut used by the 'linear' interpolation choice; the 'cubic' choice
    rebuilds the Z slice from the measured columns (slice_planes_cubic).
    """
    if metric not in ('Er', 'H'):
        raise FieldError(f'Unknown metric: {metric!r}')
    u, v, er, h, valid, u_label, v_label, axis_name = _cut(field, axis, position_nm)
    values = er if metric == 'Er' else h
    values = np.where(valid, values, np.nan)
    return Plane(axis=axis_name, position_nm=float(position_nm),
                 u_nm=u, v_nm=v, values=values,
                 mask=valid, u_label=u_label, v_label=v_label,
                 method=_SLICE_METHOD)


def slice_metric(field: FieldGrid, metric: str, axis: str,
                 position_nm: float) -> Plane:
    """Convenience wrapper: Z-slice carrying Er or H for the two panels."""
    return slice_plane(field, axis, position_nm, metric=metric)
