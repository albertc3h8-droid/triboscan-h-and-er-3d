"""Read-only Bruker ``*_analysis.txt`` reader and immutable 3D sample model.

This trial reads the real analysis TXT files (one XY position per file, several
depth rows per file).  It never writes to the source directory, never invents
measurement points and never re-ranks the data onto the old program's fixed
25 x 25 x 10 grid.

Encoding: the instrument files are single-byte cp1252-compatible; the micro
sign is byte 0xB5.  ``latin-1`` decodes every byte losslessly, so it is used
instead of UTF-8.

Coordinates are converted X/Y mm -> nm (x 1e6) and kept at the absolute
instrument coordinates.  ``heff`` is already nm, Er/H are GPa.

The dataset is immutable: numpy arrays are read-only and points are frozen
dataclasses.  Excluding a source group (and rebuilding the field from the
remaining points) is *not implemented* in this trial; see the explicit future
API stubs at the bottom.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import math
import re

import numpy as np

DEFAULT_LAYER_BIN_NM = 1.0
_REQUIRED_COLUMNS = ('File', 'heff(nm)', 'Er(GPa)', 'H(GPa)', 'X(mm)', 'Y(mm)')
_MM_TO_NM = 1_000_000.0
_XY_KEY_DECIMALS = 6  # nm; exact text values only, no clustering


class DataError(ValueError):
    """Friendly, user-facing input error (never a silent fallback)."""


@dataclass(frozen=True)
class Point:
    """One measured row, traceable to its source file and physical line."""

    point_id: str
    group_id: str
    source_file: str
    line_no: int          # 1-based physical line number in the source file
    row_index: int        # 0-based data-row index within the source file
    x_nm: float
    y_nm: float
    z_nm: float
    er_gpa: float
    h_gpa: float


@dataclass(frozen=True)
class SourceGroup:
    """One real XY position; may contain more than one source file (conflict)."""

    group_id: str
    x_nm: float
    y_nm: float
    source_files: tuple[str, ...]
    point_ids: tuple[str, ...]
    z_min_nm: float
    z_max_nm: float


@dataclass(frozen=True)
class DepthRange:
    mode: str
    lo_nm: float | None
    hi_nm: float | None
    valid: bool
    note: str


@dataclass(frozen=True)
class LayerModes:
    """Grey-reference layer depths derived from the *real* per-rank depths."""

    depths_nm: tuple[float, ...]
    counts: tuple[int, ...]
    bin_nm: float
    anchor_nm: float
    rank_count: int
    method: str


@dataclass(frozen=True)
class Dataset:
    source_dir: str
    files: tuple[str, ...]
    kind: str                     # raw | synthetic | transformed | mixed
    points: tuple[Point, ...]
    groups: tuple[SourceGroup, ...]
    x_nm: np.ndarray
    y_nm: np.ndarray
    z_nm: np.ndarray
    er_gpa: np.ndarray
    h_gpa: np.ndarray
    source_sha256: tuple[tuple[str, str], ...]

    @property
    def point_count(self) -> int:
        return len(self.points)

    @property
    def group_count(self) -> int:
        return len(self.groups)

    @property
    def xy_bounds_nm(self) -> tuple[float, float, float, float]:
        return (float(self.x_nm.min()), float(self.x_nm.max()),
                float(self.y_nm.min()), float(self.y_nm.max()))

    @property
    def z_bounds_nm(self) -> tuple[float, float]:
        return float(self.z_nm.min()), float(self.z_nm.max())

    @property
    def scalar_ranges(self) -> dict[str, tuple[float, float]]:
        return {'Er': (float(self.er_gpa.min()), float(self.er_gpa.max())),
                'H': (float(self.h_gpa.min()), float(self.h_gpa.max()))}

    def group_by_id(self, group_id: str) -> SourceGroup:
        for group in self.groups:
            if group.group_id == group_id:
                return group
        raise KeyError(group_id)

    def point_by_id(self, point_id: str) -> Point:
        for point in self.points:
            if point.point_id == point_id:
                return point
        raise KeyError(point_id)


def _decode(path: Path) -> str:
    """Lossless single-byte decode; the micro sign stays U+00B5."""
    try:
        return path.read_bytes().decode('latin-1')
    except OSError as exc:
        raise DataError(f'Cannot read {path.name}: {exc}') from exc


def _parse_file(path: Path) -> tuple[list[Point], str, int]:
    text = _decode(path)
    lines = text.splitlines()
    declared = None
    for line in lines:
        match = re.match(r'\s*Number of Data Points\s*=\s*(\d+)\s*$', line)
        if match:
            declared = int(match.group(1))
            break

    header_index = None
    header = None
    for index, line in enumerate(lines):
        if line.startswith('File\t'):
            candidate = [cell.strip() for cell in line.split('\t')]
            if all(name in candidate for name in _REQUIRED_COLUMNS):
                header_index, header = index, candidate
                break
    if header is None:
        raise DataError(f'{path.name}: no tab-separated header containing '
                        f'{", ".join(_REQUIRED_COLUMNS)}')
    if len(set(header)) != len(header):
        raise DataError(f'{path.name}: duplicate column names in header')
    columns = {name: header.index(name) for name in _REQUIRED_COLUMNS}
    ncols = len(header)

    points: list[Point] = []
    for index in range(header_index + 1, len(lines)):
        raw = lines[index]
        if not raw.strip():
            continue
        cells = raw.split('\t')
        line_no = index + 1
        if len(cells) != ncols:
            raise DataError(f'{path.name} line {line_no}: expected {ncols} '
                            f'tab-separated fields, got {len(cells)}')
        try:
            file_label = cells[columns['File']].strip()
            z_nm = float(cells[columns['heff(nm)']])
            er = float(cells[columns['Er(GPa)']])
            h = float(cells[columns['H(GPa)']])
            x_nm = float(cells[columns['X(mm)']]) * _MM_TO_NM
            y_nm = float(cells[columns['Y(mm)']]) * _MM_TO_NM
        except ValueError as exc:
            raise DataError(f'{path.name} line {line_no}: non-numeric value '
                            f'({exc})') from exc
        values = (x_nm, y_nm, z_nm, er, h)
        if not all(math.isfinite(value) for value in values):
            raise DataError(f'{path.name} line {line_no}: non-finite numeric value')
        if not file_label:
            raise DataError(f'{path.name} line {line_no}: empty File label')
        group_id = make_group_id(x_nm, y_nm)
        point_id = f'P:{path.name}#{line_no}'
        points.append(Point(
            point_id=point_id, group_id=group_id, source_file=path.name,
            line_no=line_no, row_index=len(points), x_nm=x_nm, y_nm=y_nm,
            z_nm=z_nm, er_gpa=er, h_gpa=h))
    if not points:
        raise DataError(f'{path.name}: header found but no data rows')
    if declared is not None and declared != len(points):
        raise DataError(f'{path.name}: header declares {declared} data points, '
                        f'parsed {len(points)}')
    return points, header[columns['File']], len(points)


def make_group_id(x_nm: float, y_nm: float) -> str:
    """Stable, human-readable ID for one real XY position (rounded to 1e-6 nm)."""
    return f'G:{round(float(x_nm), _XY_KEY_DECIMALS):.6f},{round(float(y_nm), _XY_KEY_DECIMALS):.6f}'


def load_directory(directory: str | Path) -> Dataset:
    """Load every ``*_analysis.txt`` in the first level of *directory* only."""
    path = Path(directory)
    if not path.exists():
        raise DataError(f'Input path does not exist: {path}')
    if not path.is_dir():
        raise DataError('Pass the directory that contains the *_analysis.txt '
                        f'files, not the file: {path.name}')
    files = sorted((p for p in path.iterdir()
                    if p.is_file() and p.suffix.lower() == '.txt'
                    and p.name.endswith('_analysis.txt')),
                   key=lambda p: p.name.lower())
    if not files:
        raise DataError(f'No *_analysis.txt files found in {path}')
    per_file: list[tuple[Path, list[Point], int]] = []
    for file_path in files:
        points, _label, rows = _parse_file(file_path)
        per_file.append((file_path, points, rows))

    all_points: list[Point] = []
    for _path, points, _rows in per_file:
        all_points.extend(points)

    grouped: dict[str, list[Point]] = {}
    for point in all_points:
        grouped.setdefault(point.group_id, []).append(point)
    groups: list[SourceGroup] = []
    for group_id in sorted(grouped, key=lambda gid: _group_sort_key(gid)):
        members = grouped[group_id]
        zs = [p.z_nm for p in members]
        groups.append(SourceGroup(
            group_id=group_id,
            x_nm=members[0].x_nm,
            y_nm=members[0].y_nm,
            source_files=tuple(sorted({p.source_file for p in members})),
            point_ids=tuple(p.point_id for p in members),
            z_min_nm=min(zs), z_max_nm=max(zs)))

    def array(values) -> np.ndarray:
        if isinstance(values, np.ndarray):
            result = np.array(values, dtype=np.float64)
        elif isinstance(values, (list, tuple)):
            result = np.asarray(values, dtype=np.float64)
        else:
            result = np.fromiter(values, dtype=np.float64)
        result.setflags(write=False)
        return result

    lower_names = [p.name.lower() for p in files]
    synthetic = all(name.startswith('synthetic_') for name in lower_names)
    transformed = any(name.startswith('transformed_') for name in lower_names)
    if synthetic:
        kind = 'synthetic'
    elif transformed:
        kind = 'transformed'
    elif any(name.startswith('synthetic_') for name in lower_names):
        kind = 'mixed'
    else:
        kind = 'raw'

    sha = tuple((p.name, hashlib.sha256(p.read_bytes()).hexdigest()) for p in files)
    return Dataset(
        source_dir=str(path.resolve()),
        files=tuple(p.name for p in files),
        kind=kind,
        points=tuple(all_points),
        groups=tuple(groups),
        x_nm=array(p.x_nm for p in all_points),
        y_nm=array(p.y_nm for p in all_points),
        z_nm=array(p.z_nm for p in all_points),
        er_gpa=array(p.er_gpa for p in all_points),
        h_gpa=array(p.h_gpa for p in all_points),
        source_sha256=sha)


def _group_sort_key(group_id: str) -> tuple[float, float]:
    body = group_id[2:]
    x_text, y_text = body.split(',')
    return float(y_text), float(x_text)


def depth_range(dataset: Dataset, mode: str = 'common') -> DepthRange:
    """Default *common* box vs. *actual* per-group coverage.

    common: Z = [max over XY groups of depth_min, min over XY groups of depth_max]
            and XY = the actual active data extent.
    actual: Z = [min depth_min, max depth_max]; at a given Z only the real 3D
            support (Delaunay convex hull, see field3d) is shown, blank elsewhere.
    """
    if mode not in ('common', 'actual'):
        raise ValueError(f'Unknown depth range mode: {mode!r}')
    if not dataset.groups:
        return DepthRange(mode, None, None, False, 'No source groups loaded')
    lows = [g.z_min_nm for g in dataset.groups]
    highs = [g.z_max_nm for g in dataset.groups]
    if mode == 'actual':
        lo, hi = min(lows), max(highs)
        return DepthRange(mode, lo, hi, True,
                          'actual per-group coverage; blank outside the 3D support')
    lo, hi = max(lows), min(highs)
    if lo > hi:
        return DepthRange(mode, None, None, False,
                          'no common depth overlap: max(depth_min) > min(depth_max); '
                          'switch to actual coverage')
    return DepthRange(mode, lo, hi, True,
                      'common overlap of every active XY group; XY = actual data extent')


def layer_mode_depths(dataset: Dataset,
                      bin_nm: float = DEFAULT_LAYER_BIN_NM) -> LayerModes:
    """Grey reference depths from real per-rank depths, 1 nm bins, mode per rank.

    Rule (documented, deterministic, visual only):
    * rank = order of a row's ``heff`` inside its own XY group (sorted by depth);
    * for each rank collect every real depth across all XY groups;
    * bin at ``bin_nm`` (default 1 nm) anchored at the global minimum depth;
    * choose the most populated bin; ties -> the shallower bin;
    * representative depth = median of the real depths inside that bin
      (never outside the real data range), so grey planes stay near real data.
    """
    if not math.isfinite(bin_nm) or bin_nm <= 0:
        raise ValueError('bin_nm must be a positive finite number')
    per_rank: dict[int, list[float]] = {}
    for group in dataset.groups:
        members = sorted((dataset.point_by_id(pid) for pid in group.point_ids),
                         key=lambda p: p.z_nm)
        for rank, point in enumerate(members):
            per_rank.setdefault(rank, []).append(point.z_nm)
    if not per_rank:
        return LayerModes((), (), float(bin_nm), 0.0, 0,
                          'no points: no grey reference layers')
    z_min = float(dataset.z_nm.min())
    depths: list[float] = []
    counts: list[int] = []
    for rank in sorted(per_rank):
        values = np.asarray(per_rank[rank], dtype=np.float64)
        indices = np.floor((values - z_min) / bin_nm).astype(np.int64)
        unique, counts_per_bin = np.unique(indices, return_counts=True)
        best = int(counts_per_bin.max())
        # ties -> smallest bin index (shallower), deterministic
        best_index = int(unique[np.flatnonzero(counts_per_bin == best)[0]])
        in_bin = values[indices == best_index]
        depths.append(float(np.median(in_bin)))
        counts.append(int(in_bin.size))
    order = np.argsort(depths)
    method = (f'per-rank real depths, {bin_nm:g} nm bins anchored at global '
              f'z_min={z_min:.3f} nm; mode bin; ties->shallower bin; '
              'representative=median of real depths in the winning bin')
    return LayerModes(
        depths_nm=tuple(float(depths[i]) for i in order),
        counts=tuple(int(counts[i]) for i in order),
        bin_nm=float(bin_nm), anchor_nm=z_min, rank_count=len(per_rank),
        method=method)


# ---------------------------------------------------------------------------
# Future active-data rebuild interfaces (explicitly NOT implemented in this
# trial).  The user narrowed the scope: no in-memory group/point exclusion and
# no dynamic range/support rebuild yet.  These stubs keep the stable source
# IDs and the future entry point, but they must never pretend to work and must
# never change the loaded data or any file.
# ---------------------------------------------------------------------------
_FUTURE_NOTE = ('Temporary in-memory exclusion/rebuild is out of scope for this '
                'trial and is NOT implemented. The loaded dataset and source '
                'files are unchanged.')


def exclude_group(dataset: Dataset, group_id: str) -> None:
    """Future API stub: exclude one whole real XY group in memory, then rebuild."""
    dataset.group_by_id(group_id)  # validate the stable ID exists
    raise NotImplementedError(_FUTURE_NOTE)


def restore_group(dataset: Dataset, group_id: str) -> None:
    """Future API stub: restore one excluded XY group (not implemented)."""
    dataset.group_by_id(group_id)
    raise NotImplementedError(_FUTURE_NOTE)


def exclude_point(dataset: Dataset, point_id: str) -> None:
    """Future API stub: exclude one measured point by stable file+line ID."""
    dataset.point_by_id(point_id)
    raise NotImplementedError(_FUTURE_NOTE)


def restore_point(dataset: Dataset, point_id: str) -> None:
    """Future API stub: restore one excluded point (not implemented)."""
    dataset.point_by_id(point_id)
    raise NotImplementedError(_FUTURE_NOTE)


def rebuild_from_active_points(dataset: Dataset,
                               active_point_ids) -> None:
    """Future API stub: rebuild field/ranges/support from an active point set."""
    ids = tuple(active_point_ids)
    for point_id in ids:
        dataset.point_by_id(point_id)
    raise NotImplementedError(_FUTURE_NOTE)
