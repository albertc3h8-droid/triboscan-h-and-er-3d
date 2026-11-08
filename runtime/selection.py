"""Pure selection helpers for the native 3D trial viewer.

The locate gesture must never use an interpolated slice vertex as if it were a
measured point.  This module only walks the *real* loaded points:

1. nearest real XY group to the pointer's world XY (deterministic tie-break);
2. inside that group, the real measured row whose own ``heff`` is closest to
   the current Z slice (deterministic tie-break: shallower depth first, then
   stable point id).

No VTK, no file access, no mutation of the dataset.  ``Point``/``SourceGroup``
objects come straight from ``trial_data_model`` so the caller keeps the stable
source file, physical line number and row index for every selection.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import loader as dm


@dataclass(frozen=True)
class Selection:
    """One real measured point selected by a locate gesture."""

    group: dm.SourceGroup
    point: dm.Point
    plane_depth_nm: float
    distance_xy_nm: float
    delta_z_nm: float

    @property
    def same_xy_files(self) -> tuple[str, ...]:
        return self.group.source_files

    @property
    def has_same_xy_tie(self) -> bool:
        return len(self.group.source_files) > 1


def nearest_group(dataset: dm.Dataset, x_nm: float,
                  y_nm: float) -> tuple[dm.SourceGroup, float]:
    """Nearest real XY group; ties resolve to the smaller stable group id.

    ``Dataset.groups`` is sorted by (Y, X) with a stable group id, so a plain
    argmin would already be deterministic.  The explicit tie-break keeps that
    guarantee even if the input order ever changes.
    """
    best = None
    best_key = None
    for group in dataset.groups:
        distance = ((group.x_nm - float(x_nm)) ** 2
                    + (group.y_nm - float(y_nm)) ** 2)
        key = (distance, group.group_id)
        if best_key is None or key < best_key:
            best_key = key
            best = group
    if best is None:
        raise dm.DataError('No XY groups loaded; cannot locate a source point')
    return best, float(best_key[0] ** 0.5)


def nearest_point_in_group(dataset: dm.Dataset, group: dm.SourceGroup,
                           depth_nm: float) -> dm.Point:
    """Real point of *group* whose own heff is nearest to *depth_nm*.

    Deterministic tie-break: smaller absolute depth difference, then the
    shallower real depth, then the stable point id (file + physical line).
    """
    depth = float(depth_nm)
    points = [dataset.point_by_id(point_id) for point_id in group.point_ids]
    if not points:
        raise dm.DataError(f'Group {group.group_id} has no measured points')
    return min(points, key=lambda p: (abs(p.z_nm - depth), p.z_nm, p.point_id))


def locate(dataset: dm.Dataset, x_nm: float, y_nm: float,
           depth_nm: float) -> Selection:
    """Apply the approved two-step rule of nearest XY group -> nearest real Z."""
    group, distance_xy = nearest_group(dataset, x_nm, y_nm)
    point = nearest_point_in_group(dataset, group, depth_nm)
    return Selection(
        group=group,
        point=point,
        plane_depth_nm=float(depth_nm),
        distance_xy_nm=distance_xy,
        delta_z_nm=abs(point.z_nm - float(depth_nm)),
    )
