"""t84 regression: the Pillow skin is composited one texel per pixel.

The skin used to be drawn with vtkImageActor, whose world bounds are one
pixel smaller than the image (w - 1 by h - 1) and therefore stretch the
bitmap over the w x h viewport.  At about 0.1 % nearest-neighbour sampling
flips whole columns from row to row, which showed up as a one-pixel step on
the colour bar frame.  The skin now goes through a vtkImageMapper /
vtkActor2D pair that blits in display units, so these tests pin:

* the actor/mapper types and the RGB passthrough window;
* a pixel-identical composite inside the static skin;
* a one-device-pixel colour bar frame that survives the 2x supersample pass
  (the frame has to be drawn supersample pixels wide and aligned to the
  supersample grid, or box-downscaling leaves a half-transparent edge).
"""
from __future__ import annotations

import shutil
import unittest
from pathlib import Path

import numpy as np
import vtk

from runtime.paths import prepare

PATHS = prepare()

from app import load_job  # noqa: E402
from gui.viewer import TrialViewer  # noqa: E402

DATA = (Path(__file__).resolve().parents[2] / '3d hardness plot raw data').resolve()
OUT = (Path(__file__).resolve().parent / 'out' / 't84').resolve()
FRAME = (232, 238, 246)


class FakeHandle:
    generation = 1
    cancelled = False

    def emit(self, *args, **kwargs):
        pass

    def check_cancel(self):
        pass


class SkinBlitTests(unittest.TestCase):

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
        cls.viewer.plotter.render()

    @classmethod
    def tearDownClass(cls):
        if cls.viewer is not None:
            cls.viewer.plotter.close()
            cls.viewer = None

    # ------------------------------------------------------------- helpers
    def _bitmap(self, factor=None):
        skin = self.viewer.skin
        if factor is not None:
            skin.supersample = factor
            skin.invalidate()
        return np.asarray(skin.render(self.viewer._skin_state()).convert('RGB'),
                          dtype=np.uint8)

    def _bar_box(self):
        return self.viewer.layout.plot_colorbars['Er'].as_int()

    # --------------------------------------------------------------- tests
    def test_01_skin_layer_blits_in_display_space(self):
        actor = self.viewer._skin_actor
        self.assertIsInstance(actor, vtk.vtkActor2D)
        mapper = actor.GetMapper()
        self.assertIsInstance(mapper, vtk.vtkImageMapper)
        # Straight RGB passthrough: any other window/level would tint the UI.
        self.assertEqual(mapper.GetColorWindow(), 255.0)
        self.assertEqual(mapper.GetColorLevel(), 127.5)
        self.assertEqual(actor.GetPosition(), (0.0, 0.0))
        self.assertFalse(actor.GetPickable())

    def test_02_skin_layers_under_the_plot_views(self):
        self.assertEqual(self.viewer._skin_renderer.GetLayer(), 0)
        for renderer in self.viewer.plot_renderers.values():
            self.assertEqual(renderer.GetLayer(), 1)

    def test_03_composite_is_pixel_identical_inside_the_skin(self):
        viewer = self.viewer
        bitmap = self._bitmap(2)
        frame = np.asarray(viewer.plotter.screenshot(return_img=True))
        frame = frame[..., :3]
        self.assertEqual(frame.shape, bitmap.shape)
        h, w = bitmap.shape[:2]
        inside = np.ones((h, w), dtype=bool)
        for metric in ('Er', 'H'):
            x0, y0, x1, y1 = viewer.layout.plot_viewport(metric)
            inside[int((1.0 - y1) * h):int((1.0 - y0) * h),
                   int(x0 * w):int(x1 * w)] = False
        inside &= bitmap.max(axis=2) < 250
        self.assertGreater(int(inside.sum()), 50000)
        diff = np.abs(bitmap.astype(np.int16) - frame.astype(np.int16)).sum(axis=2)
        worst = int(diff[inside].max())
        self.assertEqual(worst, 0,
                         f'{int((diff[inside] > 12).sum())} skin pixels differ, '
                         f'worst {worst} -- the blit is resampling again')

    def test_04_colour_bar_frame_column_stays_one_pixel(self):
        x0, y0, x1, y1 = self._bar_box()
        for factor in (1, 2):
            with self.subTest(supersample=factor):
                bitmap = self._bitmap(factor)
                left = {tuple(int(v) for v in row) for row in bitmap[y0:y1 + 1, x0]}
                right = {tuple(int(v) for v in row) for row in bitmap[y0:y1 + 1, x1]}
                self.assertEqual(left, {FRAME}, 'left frame has a mixed column')
                self.assertEqual(right, {FRAME}, 'right frame has a mixed column')
                # One device pixel wide: the neighbour is gradient, not frame.
                self.assertNotEqual(tuple(int(v) for v in bitmap[(y0 + y1) // 2, x0 + 1]), FRAME)
                self.assertNotEqual(tuple(int(v) for v in bitmap[(y0 + y1) // 2, x1 - 1]), FRAME)

    def test_05_colour_bar_frame_rows_stay_one_pixel(self):
        x0, y0, x1, y1 = self._bar_box()
        for factor in (1, 2):
            with self.subTest(supersample=factor):
                bitmap = self._bitmap(factor)
                top = {tuple(int(v) for v in bitmap[y0, x]) for x in range(x0, x1 + 1)}
                bottom = {tuple(int(v) for v in bitmap[y1, x]) for x in range(x0, x1 + 1)}
                self.assertEqual(top, {FRAME}, 'top frame row is half covered')
                self.assertEqual(bottom, {FRAME}, 'bottom frame row is half covered')


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
