"""Numeric work that must happen before the viewer is created.

This is deliberately *not* GUI work: field construction and the display-space
nearest-neighbour statistics for the RAW sphere size are the slow numerical
steps of a load.  They run in the background compute thread and are timed in
the ``compute`` stage, so a cache hit that skips the field build also skips
this work (or loads the stored spacing when the cache provides it).

The viewer only consumes the results; it must not rebuild the field.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
import time
from typing import Callable

import numpy as np

from . import display_spacing, field3d
from .errors import Cancelled
from .loader import Dataset

DEFAULT_XY_STEP_NM = 500.0
DEFAULT_Z_STEP_NM = 2.0
Z_EXAGGERATION = 0.55

# Real count of native field builds in this process.  The counter only wraps
# the single call site; it never changes the numeric work or the >5 s verdict.
_build_field_calls = 0


def reset_build_field_calls() -> None:
    global _build_field_calls
    _build_field_calls = 0


def build_field_calls() -> int:
    return _build_field_calls


def _counted_build_field(*args, **kwargs):
    global _build_field_calls
    _build_field_calls += 1
    return field3d.build_field(*args, **kwargs)


class ComputeError(RuntimeError):
    """The numeric pre-computation failed (field or spacing statistics)."""


@dataclass
class ComputeResult:
    field: field3d.FieldGrid
    z_scale: float
    raw_spacing: dict
    compute_s: float
    steps: list[dict] = dc_field(default_factory=list)


def display_z_scale(field: field3d.FieldGrid) -> float:
    """The viewer's display-only Z exaggeration for a field's physical bounds."""
    x0, x1, y0, y1, z0, z1 = field.bounds_nm
    span_x, span_y, span_z = x1 - x0, y1 - y0, z1 - z0
    return Z_EXAGGERATION * min(span_x, span_y) / span_z if span_z > 0 else 1.0


def compute_spacing(dataset: Dataset, z_scale: float) -> dict:
    positions = display_spacing.display_positions(
        dataset.x_nm, dataset.y_nm, dataset.z_nm, z_scale)
    return display_spacing.compute_spacing(positions)


def precompute(dataset: Dataset, *,
               xy_step_nm: float = DEFAULT_XY_STEP_NM,
               z_step_nm: float = DEFAULT_Z_STEP_NM,
               check_cancel: Callable[[], None] | None = None,
               progress: Callable[[str, dict], None] | None = None
               ) -> ComputeResult:
    """Build the field and the display statistics; timings kept per step.

    ``compute_s`` is measured with the monotonic clock and contains both steps.
    ``check_cancel`` is called before each step; a cancelled job raises
    :class:`Cancelled` and its partial result must be dropped.
    """
    steps: list[dict] = []

    def checkpoint(label: str) -> None:
        if check_cancel is not None:
            check_cancel()
        if progress is not None:
            progress(label, {})

    started = time.perf_counter()
    checkpoint('field')
    t0 = time.perf_counter()
    try:
        grid = _counted_build_field(
            dataset.x_nm, dataset.y_nm, dataset.z_nm,
            dataset.er_gpa, dataset.h_gpa,
            xy_step_nm=float(xy_step_nm), z_step_nm=float(z_step_nm))
    except field3d.FieldError as exc:
        raise ComputeError(f'Cannot build 3D field: {exc}') from exc
    field_s = time.perf_counter() - t0
    steps.append({'step': 'field', 'seconds': field_s,
                  'detail': grid.metadata()})

    checkpoint('spacing')
    t0 = time.perf_counter()
    z_scale = display_z_scale(grid)
    try:
        spacing = compute_spacing(dataset, z_scale)
    except display_spacing.DisplaySpacingError as exc:
        raise ComputeError(f'Cannot compute RAW display spacing: {exc}') from exc
    spacing_s = time.perf_counter() - t0
    steps.append({'step': 'spacing', 'seconds': spacing_s,
                  'detail': {k: v for k, v in spacing.items()
                             if k not in ('nearest_distances_nm',
                                          'per_point_multiplicity')}})

    return ComputeResult(field=grid, z_scale=float(z_scale),
                         raw_spacing=spacing,
                         compute_s=time.perf_counter() - started,
                         steps=steps)
