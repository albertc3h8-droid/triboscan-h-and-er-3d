"""t14 regression: the v2 design floor replaces the old narrow control band.

UI v2 never opens below 1280 x 720 (scaled by DPI and capped to the work
area), and the sidebar always has room for the reference section, so the
narrow-summary fallback is gone.  The tests pin the reference selection that
used to be summarized in the hidden band and the v2 widget stacking/actions.
"""
from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from runtime.paths import prepare

PATHS = prepare()

from app import load_job  # noqa: E402
from gui.viewer import TrialViewer  # noqa: E402

DATA = (Path(__file__).resolve().parents[2] / '3d hardness plot raw data').resolve()
OUT = (Path(__file__).resolve().parent / 'out' / 't14').resolve()

EXPECTED_WIDGETS = (
    'view.normal', 'view.locate', 'view.raw',
    'range.common', 'range.actual',
    'reference.auto', 'reference.layers', 'reference.envelope',
    'interp.linear', 'interp.cubic', 'interp.tps',
    'display.slice', 'display.cloud', 'display.fog', 'header.reset',
    'depth.slider', 'depth.value',
    'export.image', 'export.field', 'export.raw', 'export.dir',
    'export.browse', 'export.open_after',
)


class FakeHandle:
    def emit(self, *args, **kwargs):
        pass

    def check_cancel(self):
        pass


class T14LayoutTests(unittest.TestCase):

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

    def test_01_reference_section_is_visible_and_selected(self):
        viewer = self.viewer
        widget = viewer.widgets.get('reference.auto')
        self.assertIsNotNone(widget)
        self.assertTrue(widget.checked)
        state = viewer._skin_state()
        self.assertEqual(state.reference_mode, 'auto')

    def test_02_reference_selection_keeps_all_ten_layers(self):
        selection = self.viewer.reference_selection
        self.assertEqual(selection['method'], 'peaks')
        self.assertEqual(len(selection['depths_nm']), 10)
        self.assertEqual(len(selection['layer_depths_nm']), 10)

    def test_03_sidebar_groups_stack_without_overlap(self):
        def rect(name):
            widget = self.viewer.widgets.get(name)
            self.assertIsNotNone(widget, name)
            return widget.rect

        for upper, lower in (('view.raw', 'range.common'),
                             ('range.actual', 'reference.auto'),
                             ('reference.envelope', 'interp.linear')):
            self.assertLessEqual(rect(upper).y1, rect(lower).y0 + 1.0)

    def test_04_widget_business_is_unchanged(self):
        missing = [name for name in EXPECTED_WIDGETS
                   if self.viewer.widgets.get(name) is None]
        self.assertEqual(missing, [])

    def test_05_skin_camera_stays_pinned_after_a_stray_roll(self):
        """A drag outside the plot cards can end up on the skin renderer.

        The 2-D skin camera must never keep a roll: otherwise the whole UI
        (side panel, card frames, chips) tilts while the 3-D plots stay
        upright -- the P2 user-visible regression.
        """
        viewer = self.viewer
        skin = viewer._skin_renderer
        camera = skin.GetActiveCamera()
        upright = tuple(round(v, 6) for v in camera.GetViewUp())
        camera.Roll(20.0)
        # An offscreen Plotter.render() may skip the real draw; Render() is
        # the path the window uses and fires the renderers' StartEvent.
        viewer.plotter.render_window.Render()
        self.assertEqual(tuple(round(v, 6) for v in camera.GetViewUp()), upright)
        width, height = viewer._skin_size
        self.assertAlmostEqual(camera.GetPosition()[0], width / 2.0, places=3)
        self.assertAlmostEqual(camera.GetPosition()[1], height / 2.0, places=3)
        self.assertAlmostEqual(camera.GetParallelScale(), height / 2.0, places=3)


if __name__ == '__main__':
    unittest.main()
