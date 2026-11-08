"""t82 regression: the locate sphere is the same size as a RAW sphere.

The located point is one of the measured points, so it must be drawn at the RAW
sphere radius instead of the old constant 1.6 px dot (user: "定位模式下显示位置
的小球太小了，使用和RAW一样大小的球").  These tests pin the size contract:

* the marker radius is the RAW sphere radius of the metric, for both cards;
* the placed actor is a uniform sphere of exactly that radius, at every depth;
* the RAW radius is used, not a fixed pixel size, so rotating or zooming never
  changes the located point's size relative to its measured neighbours;
* a dataset without a spacing statistic still gets a visible fallback marker.
"""
from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from runtime.paths import prepare

PATHS = prepare()

from app import load_job  # noqa: E402
from gui.viewer import TrialViewer  # noqa: E402
from runtime import selection as trial_selection  # noqa: E402

DATA = (Path(__file__).resolve().parents[2] / '3d hardness plot raw data').resolve()
OUT = (Path(__file__).resolve().parent / 'out' / 't82').resolve()


class FakeHandle:
    def emit(self, *args, **kwargs):
        pass

    def check_cancel(self):
        pass


class LocateMarkerSizeTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not DATA.is_dir():
            raise unittest.SkipTest(f'raw data folder missing: {DATA}')
        shutil.rmtree(OUT, ignore_errors=True)
        (OUT / 'cache').mkdir(parents=True, exist_ok=True)
        result = load_job.load_directory(
            FakeHandle(), directory=str(DATA), cache_dir=OUT / 'cache',
            use_cache=False)
        cls.dataset = result.dataset
        cls.viewer = TrialViewer(
            result.dataset, result.field, result.raw_spacing,
            off_screen=True, window_size=(1536, 992),
            timings=result.timings, cache_hit=False)
        group = result.dataset.groups[len(result.dataset.groups) // 2]
        cls.selection = trial_selection.locate(
            result.dataset, group.x_nm, group.y_nm, cls.viewer.current_depth)

    @classmethod
    def tearDownClass(cls):
        if cls.viewer is not None:
            cls.viewer.plotter.close()
            cls.viewer = None

    def test_01_radius_equals_the_raw_sphere_radius(self):
        for metric in ('Er', 'H'):
            raw = self.viewer.raw_sphere_radius_nm[metric]
            self.assertGreater(raw, 0.0)
            self.assertAlmostEqual(self.viewer._marker_radius_nm(metric),
                                   raw, places=6)

    def test_02_placed_marker_is_a_uniform_sphere_of_that_radius(self):
        viewer = self.viewer
        viewer._place_locate_markers({'selection': self.selection})
        for metric in ('Er', 'H'):
            raw = viewer.raw_sphere_radius_nm[metric]
            scale = list(viewer.locate_markers[metric].GetScale())
            self.assertEqual(len(scale), 3)
            for value in scale:
                self.assertAlmostEqual(value, raw, places=6)
            halo = list(viewer.locate_halos[metric].GetScale())
            for value in halo:
                self.assertAlmostEqual(value, raw * 1.22, places=6)
        self.assertAlmostEqual(viewer.locate_marker_radius_nm,
                               viewer.raw_sphere_radius_nm['Er'], places=6)

    def test_03_radius_is_rederived_at_every_point_not_cached(self):
        # Two different measured points must both use the RAW radius; the marker
        # size never depends on where the point sits in the box.
        viewer = self.viewer
        groups = self.dataset.groups
        ends = [groups[0], groups[-1]]
        for group in ends:
            sel = trial_selection.locate(self.dataset, group.x_nm, group.y_nm,
                                         viewer.current_depth)
            viewer._place_locate_markers({'selection': sel})
            for axis in viewer.locate_markers['Er'].GetScale():
                self.assertAlmostEqual(
                    float(axis), viewer.raw_sphere_radius_nm['Er'], places=6)

    def test_04_degenerate_spacing_falls_back_to_a_visible_marker(self):
        viewer = self.viewer
        saved = dict(viewer.raw_sphere_radius_nm)
        try:
            viewer.raw_sphere_radius_nm = {'Er': 0.0, 'H': 0.0}
            radius = viewer._marker_radius_nm('Er', self.selection.point)
            self.assertGreater(radius, 0.0)
            viewer._place_locate_markers({'selection': self.selection})
            scale = list(viewer.locate_markers['Er'].GetScale())
            for axis in scale:
                self.assertAlmostEqual(float(axis), radius, places=9)
        finally:
            viewer.raw_sphere_radius_nm = saved

    def test_05_scene_report_states_the_size_rule(self):
        report = self.viewer.scene_report()
        locate = report['locate']
        self.assertIn('RAW sphere radius', locate['marker_radius_rule'])
        self.assertEqual(locate['marker_radius_fallback_px'],
                         self.viewer.LOCATE_MARKER_RADIUS_PX)


if __name__ == '__main__':
    unittest.main()
