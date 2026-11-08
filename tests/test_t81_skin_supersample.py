"""t81 regression: 2x text supersampling for the Pillow skin.

Pillow paints UI text with grayscale antialiasing, and at the smallest window
size FreeType's hinting snaps stems onto whole pixels; the result reads as
"pixelated" next to Windows' own text.  The static base is therefore painted at
2x and averaged back down, which removes the hinting steps.  These tests pin the
pieces the technique depends on:

* gui.ui_layout.Layout can force the text factor, so the doubled pass keeps the
  same geometry as the 1x pass instead of clamping at the 2.0 cap;
* gui.ui_chrome.Skin paints the same layout at either factor;
* the viewer only supersamples windows at or below the 1536 x 992 design frame,
  and it repaints the base at 1x during a live resize, refining afterwards.
"""
from __future__ import annotations

import shutil
import time
import unittest
from pathlib import Path

import numpy as np

from runtime.paths import prepare

PATHS = prepare()

from app import load_job  # noqa: E402
from gui import theme, ui_chrome  # noqa: E402
from gui.ui_layout import Layout  # noqa: E402
from gui.viewer import TrialViewer  # noqa: E402

DATA = (Path(__file__).resolve().parents[2] / '3d hardness plot raw data').resolve()
OUT = (Path(__file__).resolve().parent / 'out' / 't81').resolve()


class FakeHandle:
    def emit(self, *args, **kwargs):
        pass

    def check_cancel(self):
        pass


class LayoutScaleTests(unittest.TestCase):

    def test_01_forced_font_scale_doubles_the_geometry(self):
        one = Layout(1536, 992)
        two = Layout(3072, 1984, font_scale=one.font_scale * 2)
        self.assertAlmostEqual(two.font_scale, 2.0)
        self.assertAlmostEqual(two._d(16), 2 * one._d(16))
        self.assertAlmostEqual(two.sidebar_x, 2 * one.sidebar_x)
        self.assertAlmostEqual(two.export_card_rect(0).x0,
                               2 * one.export_card_rect(0).x0)
        self.assertAlmostEqual(two.plot_cards['Er'].y1,
                               2 * one.plot_cards['Er'].y1)

    def test_02_forced_scale_escapes_the_two_point_zero_cap(self):
        # The 2x pass needs 2.0 text at a 1.0 window, i.e. an effective 2.0 ...
        self.assertAlmostEqual(Layout(1536, 992, font_scale=2.0).font_scale, 2.0)
        # ... and 3.0 when the window itself is already at 1.5.
        self.assertAlmostEqual(Layout(4608, 2976, font_scale=3.0).font_scale, 3.0)

    def test_03_default_keeps_the_cap(self):
        self.assertAlmostEqual(Layout(4608, 2976).font_scale, 2.0)


class SkinSupersampleTests(unittest.TestCase):

    def _state(self):
        return ui_chrome.SkinState(dataset_label='data set', point_count=1000,
                                   file_count=100,
                                   grid_label='55 x 55 x 77', version='v1.0.0')

    def test_01_base_keeps_the_layout_size_and_the_same_geometry(self):
        layout = Layout(768, 496)
        state = self._state()
        plain = ui_chrome.Skin(layout, theme.FontBook(layout.font_scale), 'zh-CN')
        superx = ui_chrome.Skin(layout, theme.FontBook(layout.font_scale), 'zh-CN',
                                supersample=2)
        base_plain = plain._build_base(state)
        base_super = superx._build_base(state)
        self.assertEqual(base_plain.size, (768, 496))
        self.assertEqual(base_super.size, (768, 496))
        diff = np.abs(np.asarray(base_plain, dtype=int)
                      - np.asarray(base_super, dtype=int)).mean()
        # Only antialiasing differs: a shifted card or a rescaled icon would
        # push this well past 8.
        self.assertLess(float(diff), 8.0)

    def test_02_signature_rebuilds_when_the_factor_changes(self):
        layout = Layout(768, 496)
        state = self._state()
        skin = ui_chrome.Skin(layout, theme.FontBook(layout.font_scale), 'en')
        first = skin._ensure_base(state)
        skin.supersample = 2
        second = skin._ensure_base(state)
        self.assertIsNot(first, second)

    def test_03_paint_scale_restores_the_one_x_objects(self):
        layout = Layout(768, 496)
        skin = ui_chrome.Skin(layout, theme.FontBook(layout.font_scale), 'en',
                              supersample=2)
        skin._build_base(self._state())
        self.assertIs(skin.layout, layout)
        self.assertAlmostEqual(skin.fonts.font_scale, layout.font_scale)


class ViewerSupersampleTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not DATA.is_dir():
            raise unittest.SkipTest(f'raw data folder missing: {DATA}')
        shutil.rmtree(OUT, ignore_errors=True)
        (OUT / 'cache').mkdir(parents=True, exist_ok=True)
        result = load_job.load_directory(
            FakeHandle(), directory=str(DATA), cache_dir=OUT / 'cache',
            use_cache=False)
        cls.viewer = TrialViewer(
            result.dataset, result.field, result.raw_spacing,
            off_screen=True, window_size=(1536, 992),
            timings=result.timings, cache_hit=False)

    @classmethod
    def tearDownClass(cls):
        if cls.viewer is not None:
            cls.viewer.plotter.close()
            cls.viewer = None

    def test_01_design_window_is_supersampled(self):
        viewer = self.viewer
        self.assertAlmostEqual(viewer.layout.font_scale, 1.0)
        self.assertEqual(viewer.skin.supersample, 2)

    def test_02_large_windows_stay_at_one_x(self):
        viewer = self.viewer
        original = viewer.layout
        try:
            viewer.layout = Layout(3840, 2480)
            self.assertAlmostEqual(viewer.layout.font_scale, 2.0)
            self.assertEqual(viewer._skin_supersample(), 1)
            viewer.layout = Layout(2400, 1550)
            self.assertGreater(viewer.layout.font_scale,
                               viewer.SKIN_SUPERSAMPLE_MAX_SCALE)
            self.assertEqual(viewer._skin_supersample(), 1)
        finally:
            viewer.layout = original
        self.assertEqual(viewer._skin_supersample(), 2)

    def test_03_live_resize_paints_one_x_then_refines(self):
        viewer = self.viewer
        original = viewer._window_size
        viewer._window_size = lambda: (1700, 1100)
        try:
            viewer.plotter.iren.interactor.InvokeEvent('ConfigureEvent')
        finally:
            viewer._window_size = original
        self.assertEqual(tuple(viewer._skin_size), (1700, 1100))
        self.assertEqual(viewer.skin.supersample, 1)
        self.assertGreater(viewer._skin_refine_at, 0.0)
        # Pretend the one-shot timer has come due.
        viewer._skin_refine_at = time.monotonic() - 1.0
        viewer._on_skin_timer()
        self.assertEqual(viewer.skin.supersample, 2)
        self.assertEqual(viewer._skin_refine_at, 0.0)
        viewer._on_configure()
        self.assertEqual(tuple(viewer._skin_size), (1536, 992))

    def test_04_timer_is_skipped_when_already_refined(self):
        viewer = self.viewer
        viewer._skin_refine_at = time.monotonic() - 1.0
        before = viewer.skin
        viewer._on_skin_timer()
        self.assertIs(viewer.skin, before)
        self.assertEqual(viewer._skin_refine_at, 0.0)


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
