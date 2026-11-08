"""Regression: the point-cloud depth cut is in world coordinates.

The mapper's clipping plane is evaluated after the actor's (1, 1, z_scale)
display scale, so the cut origin must be scaled with z_scale.  A "data-space"
origin left the cut far below the cloud and removed it entirely.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gui.viewer import TrialViewer  # noqa: E402


class FakePlane:
    def __init__(self):
        self.origin = (0.0, 0.0, 0.0)
        self.modified = 0

    def SetOrigin(self, x, y, z):
        self.origin = (x, y, z)

    def Modified(self):
        self.modified += 1


class CloudClipTests(unittest.TestCase):
    def make_viewer(self):
        viewer = TrialViewer.__new__(TrialViewer)
        viewer.metrics = ('Er', 'H')
        viewer.disp_origin = (11688760.0, -20586240.0, 204.760621)
        viewer.physical_bounds = (11688760.0, 11718760.0,
                                  -20586240.0, -20556240.0,
                                  50.95, 204.760621)
        viewer.z_scale = 98.46860563574093
        viewer.display_bounds = (0.0, 30000.0, 0.0, 30000.0,
                                 0.0, 153.81 * viewer.z_scale)
        viewer.cloud_clip_planes = {'Er': FakePlane(), 'H': FakePlane()}
        viewer.cloud_fog_planes = {'Er': FakePlane(), 'H': FakePlane()}
        viewer._cloud_sources = {}
        viewer.cloud_points_shown = {'Er': 0, 'H': 0}
        return viewer

    def test_depth_cut_is_scaled_to_world_coordinates(self):
        viewer = self.make_viewer()
        viewer.current_range = SimpleNamespace(valid=True)
        viewer.current_depth = 128.903
        viewer._apply_cloud_clip()
        expected = (204.760621 - 128.903) * viewer.z_scale
        for metric in viewer.metrics:
            self.assertAlmostEqual(
                viewer.cloud_clip_planes[metric].origin[2], expected, places=6)
            self.assertAlmostEqual(
                viewer.cloud_fog_planes[metric].origin[2], expected, places=6)
        self.assertGreater(expected, 1000.0)

    def test_no_depth_cut_uses_the_scaled_display_top(self):
        viewer = self.make_viewer()
        viewer.current_range = SimpleNamespace(valid=False)
        viewer.current_depth = None
        viewer._apply_cloud_clip()
        expected = viewer.display_bounds[5] + 1.0
        for metric in viewer.metrics:
            self.assertAlmostEqual(
                viewer.cloud_clip_planes[metric].origin[2], expected, places=6)


if __name__ == '__main__':
    unittest.main()
