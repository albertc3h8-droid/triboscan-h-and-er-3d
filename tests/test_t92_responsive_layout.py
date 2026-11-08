"""t92 regression: one uniform scale plus a responsive main area (P8).

The 125 % / 1920 x 1080 report showed the old model: x scaled by width/1536
and y by height/992, so a 1920 x 1044 client (work area after the taskbar)
stretched every horizontal gap by 1.25 while the vertical scale was 1.05, and
the skin was painted for the requested 1240-tall window but anchored at the
bottom of the real 1044-tall one, cropping the toolbar.

The pinned behaviour: s = min(w/1536, h/992); extra width is handed to the two
3-D cards, the histogram and the export-directory card; the floor is 1280x720
and the default window is capped to the work area.
"""
from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from unittest import mock

from runtime.paths import prepare

PATHS = prepare()

from app import load_job  # noqa: E402
from gui import theme  # noqa: E402
from gui.ui_layout import Layout  # noqa: E402
from gui.viewer import TrialViewer  # noqa: E402

DATA = (Path(__file__).resolve().parents[2] / '3d hardness plot raw data').resolve()
OUT = (Path(__file__).resolve().parent / 'out' / 't92').resolve()


class FakeHandle:
    def emit(self, *args, **kwargs):
        pass

    def check_cancel(self):
        pass


class ResponsiveGeometryTests(unittest.TestCase):

    def test_01_1920x1044_uses_one_scale_and_expands_the_main_area(self):
        L = Layout(1920, 1044)
        self.assertAlmostEqual(L.scale, 1044 / 992.0, places=6)
        self.assertAlmostEqual(L.main_x1, 1920 - 16 * L.scale, places=6)
        er, hp = L.plot_cards['Er'], L.plot_cards['H']
        self.assertLess(er.x1, hp.x0)
        self.assertAlmostEqual(hp.x1, 1920 - 19 * L.scale, places=5)
        # Extra width goes to the plots, histogram and export directory.
        self.assertGreater(er.width, 606 * L.scale)
        self.assertGreater(hp.width, 646 * L.scale)
        self.assertGreater(L.hist_card.width, 401 * L.scale)
        self.assertGreater(L.export_dir_card.width, 399 * L.scale)
        self.assertAlmostEqual(L.hist_card.x1, L.main_x1 - 3 * L.scale, places=5)
        self.assertAlmostEqual(L.export_dir_card.x1,
                               L.main_x1 - 2 * L.scale, places=5)
        for rect in (L.toolbar_buttons['open'], L.toolbar_buttons['load'],
                     L.help_button, L.lang_rect, L.status_bar):
            self.assertGreaterEqual(rect.x0, -0.5)
            self.assertLessEqual(rect.x1, 1920 + 0.5)
            self.assertLessEqual(rect.y1, 1044 + 0.5)

    def test_02_1280x720_is_the_compact_floor(self):
        L = Layout(1280, 720)
        self.assertAlmostEqual(L.scale, 720 / 992.0, places=6)
        self.assertGreaterEqual(L.font_scale, 0.7)
        self.assertGreater(L.plot_cards['Er'].width, 0)
        self.assertGreater(L.plot_cards['H'].width, 0)
        self.assertLessEqual(L.plot_cards['H'].x1, 1280 + 0.5)
        self.assertLessEqual(L.export_dir_card.x1, 1280 + 0.5)
        self.assertLessEqual(L.status_bar.y1, 720 + 0.5)
        self.assertLessEqual(L.plot_views['Er'].y1, L.plot_cards['Er'].y1)

    def test_03_startup_and_floor_fit_the_125pct_work_area(self):
        with mock.patch.object(theme, 'work_area', return_value=(1920, 1044)):
            self.assertEqual(theme.window_floor(1.25), (1600, 900))
            self.assertEqual(theme.default_window_size(1.25), (1920, 1044))
            self.assertEqual(theme.clamp_window_size((1920, 1044), 1.25),
                             (1920, 1044))
            self.assertEqual(theme.clamp_window_size((900, 600), 1.25),
                             (1600, 900))
        with mock.patch.object(theme, 'work_area', return_value=(1280, 680)):
            self.assertEqual(theme.window_floor(1.0), (1280, 680))
            self.assertEqual(theme.default_window_size(1.0), (1280, 680))

    def test_04_viewer_widgets_and_skin_fill_the_real_client(self):
        if not DATA.is_dir():
            self.skipTest(f'raw data folder missing: {DATA}')
        shutil.rmtree(OUT, ignore_errors=True)
        (OUT / 'cache').mkdir(parents=True, exist_ok=True)
        result = load_job.load_directory(
            FakeHandle(), directory=str(DATA), cache_dir=OUT / 'cache',
            use_cache=False)
        viewer = TrialViewer(
            result.dataset, result.field, result.raw_spacing,
            off_screen=True, window_size=(1920, 1044),
            timings=result.timings, cache_hit=False)
        try:
            self.assertEqual(tuple(viewer._skin_size), (1920, 1044))
            self.assertAlmostEqual(viewer.layout.scale, 1044 / 992.0, places=6)
            for name, widget in viewer.widgets.items():
                rect = widget.rect
                self.assertGreaterEqual(rect.x0, -0.5, name)
                self.assertGreaterEqual(rect.y0, -0.5, name)
                self.assertLessEqual(rect.x1, 1920 + 0.5, name)
                self.assertLessEqual(rect.y1, 1044 + 0.5, name)
            for metric in ('Er', 'H'):
                viewport = viewer.layout.plot_viewport(metric)
                self.assertAlmostEqual(viewport[0], viewer.layout.plot_views[metric].x0 / 1920)
                self.assertGreater(viewport[2], viewport[0])
                self.assertGreater(viewport[3], viewport[1])
        finally:
            viewer.plotter.close()

    def test_05_resize_rebuilds_to_the_compact_size(self):
        if not DATA.is_dir():
            self.skipTest(f'raw data folder missing: {DATA}')
        result = load_job.load_directory(
            FakeHandle(), directory=str(DATA), cache_dir=OUT / 'cache',
            use_cache=True)
        viewer = TrialViewer(
            result.dataset, result.field, result.raw_spacing,
            off_screen=True, window_size=(1920, 1044),
            timings=result.timings, cache_hit=True)
        original = viewer._window_size
        try:
            viewer._window_size = lambda: (1280, 720)
            viewer.plotter.iren.interactor.InvokeEvent('ConfigureEvent')
            self.assertEqual(tuple(viewer._skin_size), (1280, 720))
            self.assertAlmostEqual(viewer.layout.scale, 720 / 992.0, places=6)
            for name in ('header.reset', 'export.dir', 'depth.slider',
                         'interp.tps'):
                rect = viewer.widgets.get(name).rect
                self.assertLessEqual(rect.x1, 1280 + 0.5, name)
                self.assertLessEqual(rect.y1, 720 + 0.5, name)
        finally:
            viewer._window_size = original
            viewer.plotter.close()


if __name__ == '__main__':
    unittest.main()
