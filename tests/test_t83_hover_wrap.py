"""t83 regression: the locate hover band wraps inside its own card.

The band used to wrap to half the window width (the layout the plots had before
the v2 cards) and to budget 0.62 em per character, so a long source path ran
over the colour bar and a Chinese line overran the card edge.  These tests pin
the two contracts: the budget is the card's plot width, and the measured ink
width of every produced line fits in it without dropping a character.
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
OUT = (Path(__file__).resolve().parent / 'out' / 't83').resolve()


class FakeHandle:
    def emit(self, *args, **kwargs):
        pass

    def check_cancel(self):
        pass


class HoverWrapTests(unittest.TestCase):

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

    def _budget(self):
        return self.viewer._hover_plot_width_px()

    def test_01_budget_is_the_card_plot_width(self):
        viewer = self.viewer
        x0, _y0, x1, _y1 = viewer.layout.plot_viewport('Er')
        width = float(viewer._window_size()[0])
        self.assertAlmostEqual(viewer._hover_plot_width_px(), (x1 - x0) * width,
                               places=6)
        # The pre-v2 half-window budget was much wider than one card.
        self.assertLess(viewer._hover_plot_width_px(), width / 2.0)

    def test_02_mixed_line_fits_and_keeps_every_character(self):
        viewer = self.viewer
        sample = ('来源: 3d_plot_Position_0_00022_LL_QS_LC_analysis.txt '
                  '真实 XYZ: X=11694760.0 Y=-20580240.0 '
                  '定位提示: 细屏幕圆环只是辅助指示')
        wrapped = viewer._wrap_hover_lines([sample])
        self.assertGreater(len(wrapped), 1)
        self.assertEqual(''.join(wrapped).replace(' ', ''),
                         sample.replace(' ', ''))
        prop = float(viewer.hover_actors['Er']
                     .GetTextProperty().GetFontSize())
        budget = self._budget()
        for line in wrapped:
            ink = viewer._ink_width_px(line, prop * 0.55, prop)
            self.assertLessEqual(ink, budget + 1e-6, line)

    def test_03_cjk_is_measured_as_a_wide_glyph(self):
        viewer = self.viewer
        text = '定位提示' * 15  # 60 ideographs: ~1 em each
        narrow = viewer._ink_width_px(text, 0.55, 1.0)
        wide = viewer._ink_width_px(text, 0.55, 1.0)  # same units, same result
        self.assertAlmostEqual(narrow, wide)
        self.assertAlmostEqual(narrow, 60.0)
        wrapped = viewer._wrap_hover_text(text, 40.0, 0.55, 1.0)
        self.assertGreater(len(wrapped), 1)
        self.assertEqual(''.join(wrapped), text)
        for line in wrapped:
            self.assertLessEqual(viewer._ink_width_px(line, 0.55, 1.0),
                                 40.0 + 1e-6)

    def test_04_long_token_is_split_but_never_dropped(self):
        viewer = self.viewer
        token = 'a' * 300
        lines = viewer._wrap_hover_text(token, 50.0, 0.55, 1.0)
        self.assertGreater(len(lines), 1)
        self.assertEqual(''.join(lines), token)

    def test_05_blank_lines_survive(self):
        viewer = self.viewer
        lines = viewer._wrap_hover_lines(['abc', '', 'def'])
        self.assertEqual(lines, ['abc', '', 'def'])

    def test_06_band_is_compact_and_low(self):
        """The locate annotation is a source block, not a headline: 12 px
        text (down from 16) and a 3 % bottom margin (down from 10.5 %) keep
        the 3-D data readable while staying near the card edge."""
        actor = self.viewer.hover_actors['Er']
        self.assertLessEqual(
            float(actor.GetTextProperty().GetFontSize()), 12.0)
        x, y = actor.GetPosition()
        self.assertLessEqual(float(y), 0.04)
        self.assertGreaterEqual(float(y), 0.0)


if __name__ == '__main__':
    unittest.main()
