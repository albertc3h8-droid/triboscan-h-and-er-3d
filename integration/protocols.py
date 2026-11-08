"""Structured values exchanged by the app, the GUI and the export adapter.

These are plain data: the compute worker fills a :class:`JobResult`, the main
thread passes only plain values (no live VTK actors) into the export adapter.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Optional

from runtime.loader import Dataset
from runtime.field3d import FieldGrid

APP_TITLE = 'TriboScan3D'
APP_VERSION = '1.0.0'


@dataclass
class Timings:
    """Monotonic, stage-separated timings for one load."""

    read_s: Optional[float] = None
    compute_s: Optional[float] = None
    render_s: Optional[float] = None
    cache_load_s: Optional[float] = None
    total_s: Optional[float] = None
    compute_steps: list[dict] = dc_field(default_factory=list)
    slow_compute_threshold_s: float = 5.0
    # Real number of native field builds performed by this load (0 on a cache
    # hit or a reopened result file, 1 on a fresh compute).  Reported only; it
    # never takes part in timing or the slow-compute decision.
    build_count: int = 0

    @property
    def slow_compute(self) -> bool:
        return (self.compute_s is not None
                and self.compute_s > self.slow_compute_threshold_s)

    def as_dict(self) -> dict:
        return {
            'read_s': self.read_s,
            'compute_s': self.compute_s,
            'render_s': self.render_s,
            'cache_load_s': self.cache_load_s,
            'total_s': self.total_s,
            'compute_steps': list(self.compute_steps),
            'slow_compute': self.slow_compute,
            'slow_compute_threshold_s': self.slow_compute_threshold_s,
            'build_count': int(self.build_count),
        }


@dataclass
class SourceInfo:
    """Immutable provenance of the loaded data (never a source mutation)."""

    kind: str                    # 'directory' | 'result_file'
    path: str                    # folder or .tsr3 file the user chose
    display_label: str
    files: list[dict] = dc_field(default_factory=list)
    signature: Optional[str] = None
    point_count: int = 0
    group_count: int = 0
    source_sha256: Optional[str] = None
    source_verified: Optional[bool] = None
    note: str = ''


@dataclass
class JobResult:
    dataset: Dataset
    field: FieldGrid
    raw_spacing: dict
    source: SourceInfo
    timings: Timings
    cache_hit: bool = False
    cache_reason: str = ''
    cache_key: Optional[str] = None
    warnings: list[str] = dc_field(default_factory=list)
    build_count: int = 1
    #: False only for a raw-points CSV: the measured points are real but no
    #: interpolated field exists, so the viewer opens in points-only mode.
    has_field: bool = True


@dataclass
class GridView:
    x_nm: list
    y_nm: list
    z_nm: list
    nx: int
    ny: int
    nz: int
    dx_nm: float
    dy_nm: float
    dz_nm: float


@dataclass
class PlaneView:
    axis: str
    position_nm: float
    metric: str
    u_nm: list
    v_nm: list
    values: list
    mask: list


@dataclass
class CameraView:
    position: tuple
    focal_point: tuple
    up: tuple
    parallel_scale: float
    view_angle: float
    clipping_range: tuple


@dataclass
class SliceSnapshot:
    """What a PNG export must be able to state on its own (no source files)."""

    app_version: str
    dataset_label: str
    depth_nm: Optional[float]
    depth_kind: str              # 'slice' | 'not_a_slice'
    view_mode: str
    range_mode: str
    metric: str                  # 'Er' | 'H' | 'both'
    depth_note: str
    grid: GridView
    plane: Optional[PlaneView] = None
    camera: Optional[CameraView] = None
    source_path: str = ''
    source_signature: Optional[str] = None


@dataclass
class ExportResult:
    ok: bool
    files: list[str] = dc_field(default_factory=list)
    message: str = ''
    warnings: list[str] = dc_field(default_factory=list)
    detail: str = ''


@dataclass
class LoadedView:
    """Everything the main thread needs to build a viewer (plain data only)."""

    result: JobResult
    window_title: str
    save_available: bool = False


def error_text(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return f'{exc.__class__.__name__}: {text}'
