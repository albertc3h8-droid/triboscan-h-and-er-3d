"""P6: the layer-2 translucent help card.

The toolbar help entry used to flash one status-bar line.  P6 moves it to a
real overlay on render-window layer 2: a dimmed backdrop and a centred card
painted with the same Pillow palette/fonts as the skin.  VTK composites the
card's RGBA image per pixel over the plots (the P6 alpha probe measured
(0,127,128) for a 50 % blue over green), so the 3D scene stays visible behind
the dim while the card itself is opaque.

The tests pin the layer contract, the painter (dim alpha / opaque card), the
open-close paths (button, F1, Esc, close box, backdrop), the language repaint
and the rule that PNG exports never contain the help card.
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
from gui import i18n  # noqa: E402
from gui.viewer import TrialViewer  # noqa: E402

DATA = (Path(__file__).resolve().parents[2]
        / '3d hardness plot raw data').resolve()
OUT = (Path(__file__).resolve().parent / 'out' / 't91').resolve()


class FakeHandle:
    generation = 1
    cancelled = False

    def emit(self, *args, **kwargs):
        pass

    def check_cancel(self):
        pass


class HelpOverlayTests(unittest.TestCase):

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
            off_screen=True, window_size=(1536, 992), language='en',
            timings=result.timings, cache_hit=False)
        cls.viewer.plotter.render()

    @classmethod
    def tearDownClass(cls):
        if cls.viewer is not None:
            cls.viewer.plotter.close()
            cls.viewer = None

    def setUp(self):
        self.viewer.set_help_open(False, render=False)
        if self.viewer.language != 'en':
            self.viewer.set_language('en', render=False)
        self._point = self.viewer._ui_point

    def tearDown(self):
        self.viewer._ui_point = self._point
        self.viewer.set_help_open(False, render=False)

    # ------------------------------------------------------------- helpers
    def _screenshot(self):
        return np.asarray(self.viewer.plotter.screenshot(None, return_img=True),
                          dtype=np.float64)

    def _help_pixels(self):
        scalars = self.viewer._help_image.GetPointData().GetScalars()
        return np.asarray(scalars, dtype=np.uint8).copy()

    # --------------------------------------------------------------- tests
    def test_01_help_lives_on_layer_two_and_starts_hidden(self):
        window = self.viewer.plotter.render_window
        self.assertEqual(window.GetNumberOfLayers(), 3)
        renderer = self.viewer._help_renderer
        self.assertEqual(renderer.GetLayer(), 2)
        self.assertFalse(renderer.GetDraw())
        self.assertTrue(renderer.GetPreserveColorBuffer())
        self.assertTrue(renderer.GetPreserveDepthBuffer())
        self.assertEqual(renderer.GetBackgroundAlpha(), 0.0)
        self.assertEqual(self.viewer._help_image.GetDimensions(),
                         (1536, 992, 1))
        self.assertFalse(self.viewer.help_open)
        actor = self.viewer._help_actor
        self.assertIsInstance(actor, vtk.vtkActor2D)
        self.assertIsInstance(actor.GetMapper(), vtk.vtkImageMapper)

    def test_02_painter_dims_backdrop_and_keeps_the_card_opaque(self):
        image = self.viewer.skin.render_help()
        self.assertEqual(image.mode, 'RGBA')
        self.assertEqual(image.size, (1536, 992))
        card, close = self.viewer.skin.help_geometry()
        # A corner shows only the 118-alpha dim; the card centre is opaque.
        self.assertEqual(image.getpixel((4, 4)), (10, 22, 44, 118))
        red, green, blue, alpha = image.getpixel(
            (int(card.cx), int(card.cy)))
        self.assertEqual(alpha, 255)
        self.assertTrue(all(channel >= 200 for channel in (red, green, blue)))
        self.assertTrue(card.contains(close.cx, close.cy))
        self.assertGreater(close.width, 0)

    def test_03_open_draws_the_layer_and_locks_plot_cameras(self):
        self.viewer.set_help_open(True, render=False)
        self.assertTrue(self.viewer.help_open)
        self.assertTrue(self.viewer._help_renderer.GetDraw())
        for renderer in self.viewer.plot_renderers.values():
            self.assertFalse(renderer.GetInteractive())
        self.viewer.set_help_open(False, render=False)
        for renderer in self.viewer.plot_renderers.values():
            self.assertTrue(renderer.GetInteractive())

    def test_04_composite_darkens_the_scene_and_close_restores_it(self):
        closed = self._screenshot()
        self.viewer.set_help_open(True)
        opened = self._screenshot()
        self.assertLess(float(opened.mean()), float(closed.mean()) - 10.0)
        self.viewer.set_help_open(False)
        again = self._screenshot()
        np.testing.assert_allclose(again, closed, atol=2.0)

    def test_05_language_toggle_repaints_the_open_help(self):
        if not self.viewer.font_file:
            raise unittest.SkipTest('CJK font missing')
        self.viewer.set_language('zh-CN', render=False)
        self.viewer.set_help_open(True, render=False)
        zh = self._help_pixels()
        self.viewer.set_language('en', render=False)
        self.assertTrue(self.viewer.help_open)
        self.assertTrue(self.viewer._help_renderer.GetDraw())
        en = self._help_pixels()
        self.assertFalse(np.array_equal(zh, en))

    def test_06_f1_toggles_and_escape_closes(self):
        raw = self.viewer.plotter.iren.interactor
        original = raw.GetKeySym
        try:
            raw.GetKeySym = lambda: 'F1'
            self.viewer._on_ui_key(raw)
            self.assertTrue(self.viewer.help_open)
            self.viewer._on_ui_key(raw)
            self.assertFalse(self.viewer.help_open)
            self.viewer.set_help_open(True, render=False)
            raw.GetKeySym = lambda: 'Escape'
            self.viewer._on_ui_key(raw)
            self.assertFalse(self.viewer.help_open)
        finally:
            raw.GetKeySym = original

    def test_07_close_box_and_backdrop_close_inside_keeps(self):
        card, close = self.viewer.skin.help_geometry()
        self.viewer.set_help_open(True, render=False)
        self.viewer._ui_point = lambda: (close.cx, close.cy)
        self.viewer._on_ui_press()
        self.assertFalse(self.viewer.help_open)
        self.viewer.set_help_open(True, render=False)
        self.viewer._ui_point = lambda: (card.cx, card.cy)
        self.viewer._on_ui_press()
        self.assertTrue(self.viewer.help_open)
        self.viewer._ui_point = lambda: (4.0, 4.0)
        self.viewer._on_ui_press()
        self.assertFalse(self.viewer.help_open)

    def test_08_export_captures_hide_the_help_card(self):
        self.viewer.set_help_open(True, render=False)
        seen = []
        original = self.viewer.plotter.screenshot

        def spy(*args, **kwargs):
            seen.append(bool(self.viewer._help_actor.GetVisibility()))
            return original(*args, **kwargs)

        self.viewer.plotter.screenshot = spy
        try:
            image = self.viewer._capture_export_pixels()
        finally:
            self.viewer.plotter.screenshot = original
        self.assertEqual(image.ndim, 3)
        self.assertEqual(seen, [False])
        self.assertTrue(self.viewer._help_actor.GetVisibility())

    def test_09_clean_render_hides_the_card_and_restores_it(self):
        target = OUT / 'help_clean.png'
        self.viewer.set_help_open(True, render=False)
        seen = []
        original = self.viewer.plotter.screenshot

        def spy(*args, **kwargs):
            seen.append(bool(self.viewer._help_actor.GetVisibility()))
            return original(*args, **kwargs)

        self.viewer.plotter.screenshot = spy
        try:
            self.viewer.clean_render_png(target)
        finally:
            self.viewer.plotter.screenshot = original
        self.assertTrue(target.is_file())
        self.assertEqual(seen, [False])
        self.assertTrue(self.viewer._help_actor.GetVisibility())

    def test_10_toolbar_button_opens_the_overlay(self):
        widget = self.viewer.widgets.get('toolbar.help')
        self.assertIsNotNone(widget)
        before = self.viewer._notice
        self.viewer._activate_widget(widget)
        self.assertTrue(self.viewer.help_open)
        self.assertEqual(self.viewer._notice, before)

    def test_11_help_strings_exist_in_both_languages(self):
        keys = ('help.title', 'help.subtitle', 'help.footer', 'help.note',
                'help.view.title', 'help.view.b1', 'help.view.b2',
                'help.view.b3', 'help.depth.title', 'help.depth.b1',
                'help.depth.b2', 'help.depth.b3', 'help.depth.b4',
                'help.interp.title', 'help.interp.b1', 'help.interp.b2',
                'help.interp.b3', 'help.export.title', 'help.export.b1',
                'help.export.b2', 'help.export.b3', 'help.export.b4')
        for language in i18n.LANGUAGES:
            for key in keys:
                self.assertIn(key, i18n.STRINGS[language], (language, key))
