"""Read this program's raw-points CSV export back into a :class:`Dataset`.

The export contract is frozen in ``io_exports/raw_points.py`` (12 columns,
UTF-8 with BOM, CRLF); it is duplicated here because ``runtime`` must not
import ``io_exports``.  Header, row width and numeric values are checked
strictly: a foreign CSV is rejected with a clear reason instead of being
silently reinterpreted as measurement data.

The CSV carries the real measured points only -- there is no interpolated
field -- so :func:`points_only_field` builds a bounds-only :class:`FieldGrid`
with every voxel invalid.  The viewer can therefore place the camera, axes and
depth controls physically without showing invented values.
"""
from __future__ import annotations

import csv
import hashlib
import io
import math
from pathlib import Path

import numpy as np

from .field3d import FieldGrid
from .loader import Dataset, Point, SourceGroup

#: Frozen export columns, in order (io_exports/raw_points.py:COLUMNS).
EXPECTED_COLUMNS: tuple[str, ...] = (
    'point_id', 'group_id', 'source_file', 'source_line', 'source_row',
    'X_mm', 'Y_mm', 'X_nm', 'Y_nm', 'heff_nm', 'Er_GPa', 'H_GPa')


class PointsCsvError(ValueError):
    """User-facing failure with a machine-readable reason for i18n."""

    def __init__(self, kind: str, message: str, **details) -> None:
        super().__init__(message)
        self.kind = kind        # 'header' | 'row' | 'empty'
        self.details = details


def _unguard(text: str) -> str:
    """Undo the spreadsheet formula-injection guard of the exporter."""
    return text[1:] if text.startswith("'") else text


def _read_only(array) -> np.ndarray:
    result = np.asarray(array, dtype=np.float64)
    result.setflags(write=False)
    return result


def load_points_csv(path) -> Dataset:
    """Strictly read one raw-points CSV export into an immutable Dataset."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PointsCsvError('empty', f'cannot read {path.name}: {exc}',
                             name=path.name) from exc
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise PointsCsvError('header', f'not UTF-8: {exc}', name=path.name,
                             missing=list(EXPECTED_COLUMNS),
                             found=[]) from exc
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise PointsCsvError('empty', 'empty file', name=path.name)
    header = tuple(cell.strip() for cell in rows[0])
    if header != EXPECTED_COLUMNS:
        missing = [name for name in EXPECTED_COLUMNS if name not in header]
        raise PointsCsvError('header', f'header mismatch: {header!r}',
                             name=path.name, missing=missing,
                             found=list(header))
    points: list[Point] = []
    for number, row in enumerate(rows[1:], start=2):
        if not row or all(not cell.strip() for cell in row):
            continue
        if len(row) != len(EXPECTED_COLUMNS):
            raise PointsCsvError('row', 'wrong cell count', name=path.name,
                                 row=number,
                                 reason=f'expected {len(EXPECTED_COLUMNS)} '
                                        f'cells, got {len(row)}')
        cells = [cell.strip() for cell in row]
        try:
            point = Point(
                point_id=_unguard(cells[0]),
                group_id=_unguard(cells[1]),
                source_file=_unguard(cells[2]),
                line_no=int(float(cells[3])),
                row_index=int(float(cells[4])),
                x_nm=float(cells[7]),
                y_nm=float(cells[8]),
                z_nm=float(cells[9]),
                er_gpa=float(cells[10]),
                h_gpa=float(cells[11]))
        except ValueError as exc:
            raise PointsCsvError('row', 'non-numeric cell', name=path.name,
                                 row=number, reason=str(exc)) from exc
        values = (point.x_nm, point.y_nm, point.z_nm, point.er_gpa, point.h_gpa)
        if not all(math.isfinite(value) for value in values):
            raise PointsCsvError('row', 'non-finite value', name=path.name,
                                 row=number, reason="not a finite number")
        points.append(point)
    if not points:
        raise PointsCsvError('empty', 'no data rows', name=path.name)
    grouped: dict[str, list[Point]] = {}
    for point in points:
        grouped.setdefault(point.group_id, []).append(point)
    groups: list[SourceGroup] = []
    for group_id, members in grouped.items():
        depths = [member.z_nm for member in members]
        groups.append(SourceGroup(
            group_id=group_id,
            x_nm=members[0].x_nm,
            y_nm=members[0].y_nm,
            source_files=tuple(sorted({member.source_file for member in members})),
            point_ids=tuple(member.point_id for member in members),
            z_min_nm=min(depths),
            z_max_nm=max(depths)))
    return Dataset(
        source_dir=str(path.parent),
        files=(path.name,),
        kind='raw',
        points=tuple(points),
        groups=tuple(groups),
        x_nm=_read_only([point.x_nm for point in points]),
        y_nm=_read_only([point.y_nm for point in points]),
        z_nm=_read_only([point.z_nm for point in points]),
        er_gpa=_read_only([point.er_gpa for point in points]),
        h_gpa=_read_only([point.h_gpa for point in points]),
        source_sha256=((path.name, hashlib.sha256(raw).hexdigest()),))


#: A points-only field never holds a sample; the grid only carries the real
#: extents.  Cap its cell count so a CSV with a unique coordinate per point
#: cannot allocate an absurd placeholder volume.
PLACEHOLDER_MAX_CELLS = 2_000_000


def _endpoints(values):
    """Two-value axis (min, max) and its step, for a capped placeholder grid."""
    lo = float(min(values))
    hi = float(max(values))
    step = (hi - lo) if hi > lo else 1.0
    return np.array([lo, hi], dtype=np.float64), max(step, 1e-9)


def _axis(values):
    """Unique sorted coordinates and a spacing that keeps the true extent."""
    unique = np.unique(np.asarray(values, dtype=np.float64))
    if unique.size > 1:
        step = float(unique[-1] - unique[0]) / float(unique.size - 1)
    else:
        step = 1.0
    return unique, max(step, 1e-9)


def points_only_field(dataset) -> FieldGrid:
    """Bounds-only grid for a points-only dataset: no interpolated sample.

    Every voxel stays invalid and NaN, so nothing is drawn as an interpolated
    field; the axes still use the real measured extents.
    """
    xs, dx = _axis(dataset.x_nm)
    ys, dy = _axis(dataset.y_nm)
    zs, dz = _axis(dataset.z_nm)
    if int(xs.size) * int(ys.size) * int(zs.size) > PLACEHOLDER_MAX_CELLS:
        xs, dx = _endpoints(dataset.x_nm)
        ys, dy = _endpoints(dataset.y_nm)
        zs, dz = _endpoints(dataset.z_nm)
    shape = (int(zs.size), int(ys.size), int(xs.size))
    valid = np.zeros(shape, dtype=bool)
    valid.setflags(write=False)
    empty = np.full(shape, np.nan, dtype=np.float64)
    empty.setflags(write=False)
    return FieldGrid(
        x0_nm=float(xs[0]), y0_nm=float(ys[0]), z0_nm=float(zs[0]),
        dx_nm=dx, dy_nm=dy, dz_nm=dz,
        nx=int(xs.size), ny=int(ys.size), nz=int(zs.size),
        valid=valid, er_gpa=empty, h_gpa=empty.copy(),
        point_count=int(dataset.point_count), delaunay_cells=0,
        xy_step_requested_nm=dx, z_step_requested_nm=dz)
