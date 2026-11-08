"""Duck-typed adapters between cached DTO views and the runtime objects.

``result_store`` returns read-only projections (fields named ``x0_nm`` etc.);
the viewer and ``field3d`` expect the runtime objects (``x0`` etc.).  These
adapters are the *only* translation point, so neither side has to change and
the cache is never forced to depend on PyVista.
"""
from __future__ import annotations

import numpy as np

from .field3d import FieldGrid
from .loader import Dataset


class DatasetViewAdapter:
    """Expose a cached DatasetView with the runtime Dataset's read helpers."""

    def __init__(self, view):
        self._view = view
        self.source_dir = str(view.source_dir)
        self.kind = str(view.kind)
        self.files = tuple(view.files)
        self.points = tuple(view.points)
        self.groups = tuple(view.groups)
        self.x_nm = np.asarray(view.x_nm, dtype=np.float64)
        self.y_nm = np.asarray(view.y_nm, dtype=np.float64)
        self.z_nm = np.asarray(view.z_nm, dtype=np.float64)
        self.er_gpa = np.asarray(view.er_gpa, dtype=np.float64)
        self.h_gpa = np.asarray(view.h_gpa, dtype=np.float64)
        self.source_sha256 = tuple(tuple(item) for item in view.source_sha256)
        # Cached views carry the size table from the .tsr3 manifest; a freshly
        # loaded Dataset does not (the loader keeps only sha256), so this stays
        # empty and io_exports falls back to the live files.
        self.source_sizes = tuple(
            tuple(item) for item in getattr(view, 'source_sizes', ()) or ())

    def __getattr__(self, name):
        return getattr(self._view, name)

    @property
    def point_count(self) -> int:
        return int(self.x_nm.size)

    @property
    def group_count(self) -> int:
        return len(self.groups)

    @property
    def xy_bounds_nm(self):
        x, y = self.x_nm, self.y_nm
        return (float(x.min()), float(x.max()), float(y.min()), float(y.max()))

    @property
    def z_bounds_nm(self):
        return (float(self.z_nm.min()), float(self.z_nm.max()))

    @property
    def scalar_ranges(self):
        result = {}
        for name, array in (('Er', self.er_gpa), ('H', self.h_gpa)):
            finite = array[np.isfinite(array)]
            if finite.size == 0:
                raise ValueError(f'no finite {name} values in the cached dataset')
            result[name] = (float(finite.min()), float(finite.max()))
        return result


class FieldViewAdapter:
    """Expose a cached FieldGridView with the runtime FieldGrid surface."""

    def __init__(self, view):
        self._view = view
        self.x0 = float(view.x0_nm)
        self.y0 = float(view.y0_nm)
        self.z0 = float(view.z0_nm)
        self.dx = float(view.dx_nm)
        self.dy = float(view.dy_nm)
        self.dz = float(view.dz_nm)
        self.nx = int(view.nx)
        self.ny = int(view.ny)
        self.nz = int(view.nz)
        self.valid = np.asarray(view.valid, dtype=bool)
        self.er_gpa = np.asarray(view.er_gpa, dtype=np.float64)
        self.h_gpa = np.asarray(view.h_gpa, dtype=np.float64)
        self.point_count = int(getattr(view, 'point_count', 0))

    def __getattr__(self, name):
        return getattr(self._view, name)

    @property
    def shape(self):
        return (self.nz, self.ny, self.nx)

    @property
    def x_nm(self):
        return self.x0 + np.arange(self.nx, dtype=np.float64) * self.dx

    @property
    def y_nm(self):
        return self.y0 + np.arange(self.ny, dtype=np.float64) * self.dy

    @property
    def z_nm(self):
        return self.z0 + np.arange(self.nz, dtype=np.float64) * self.dz

    @property
    def bounds_nm(self):
        return (self.x0, self.x0 + (self.nx - 1) * self.dx,
                self.y0, self.y0 + (self.ny - 1) * self.dy,
                self.z0, self.z0 + (self.nz - 1) * self.dz)

    @property
    def valid_fraction(self) -> float:
        return float(self.valid.mean()) if self.valid.size else 0.0

    def metadata(self) -> dict:
        method = getattr(self._view, 'metadata', None)
        if callable(method):
            return method()
        return {'format': getattr(self._view, 'format', 'triboscan.field3d'),
                'source': 'saved result file'}


def as_dataset(obj):
    return obj if isinstance(obj, Dataset) else DatasetViewAdapter(obj)


def as_field(obj):
    return obj if isinstance(obj, FieldGrid) else FieldViewAdapter(obj)
