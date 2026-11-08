"""t80 regression: window floor, DPI scaling and live-resize rebuild.

Three defects showed up on a 150 % / 4K setup: the 1536 x 992 window opened
smaller than the floor, maximising it left white side bands with soft
(Windows-stretched) text, and dragging the frame below the floor left the skin
at its old size, so pointer picks drifted from what was drawn.  The causes
pinned here:

* the minimum client size policy (gui.theme) - design pixels scaled by DPI for
  interactive windows, untouched sizes for off-screen evidence renders;
* the shell-side floor (gui.window_policy) - WM_GETMINMAXINFO on the VTK
  handle, so Windows itself refuses the sizes that would desync the skin;
* the skin rebuild trigger - vtkRenderWindow never emits ConfigureEvent; the
  interactor does, so the Pillow skin and the plot viewports must follow it.

No window is opened: the interactor event is invoked directly on the
off-screen plotter, exactly what VTK does for an OS resize.
"""
from __future__ import annotations

import ctypes
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

from runtime.paths import prepare

PATHS = prepare()

from app import load_job  # noqa: E402
from gui import theme  # noqa: E402
from gui.viewer import TrialViewer  # noqa: E402

DATA = (Path(__file__).resolve().parents[2] / '3d hardness plot raw data').resolve()
OUT = (Path(__file__).resolve().parent / 'out' / 't80').resolve()


class FakeHandle:
    def emit(self, *args, **kwargs):
        pass

    def check_cancel(self):
        pass


class WindowPolicyTests(unittest.TestCase):

    def test_01_floor_is_the_compact_1280x720_frame_at_any_dpi(self):
        with mock.patch.object(theme, 'work_area', return_value=(3840, 2160)):
            self.assertEqual(theme.window_floor(1.0), (1280, 720))
            self.assertEqual(theme.window_floor(1.5), (1920, 1080))

    def test_02_interactive_clamp_raises_small_requests(self):
        with mock.patch.object(theme, 'work_area', return_value=(3840, 2160)):
            self.assertEqual(theme.clamp_window_size((900, 600), 1.5),
                             (1920, 1080))
            self.assertEqual(theme.clamp_window_size((2304, 1488), 1.5),
                             (2304, 1488))

    def test_03_offscreen_clamp_keeps_the_requested_size(self):
        self.assertEqual(theme.clamp_window_size((1536, 992), minimum=False),
                         (1536, 992))
        self.assertEqual(theme.clamp_window_size((900, 600), minimum=False),
                         (900, 600))
        # Evidence renders may exceed the design frame: a 150 % screen's
        # 2304 x 1488 bitmap must not be capped to 2560 x 1440.
        self.assertEqual(
            theme.clamp_window_size((2304, 1488), minimum=False,
                                    maximum=False),
            (2304, 1488))

    def test_04_default_window_is_the_scaled_design_size_or_floor(self):
        with mock.patch.object(theme, 'work_area', return_value=(3840, 2160)):
            self.assertEqual(theme.default_window_size(1.0), (1536, 992))
            self.assertEqual(theme.default_window_size(1.5), (2304, 1488))

    def test_05_floor_never_exceeds_the_work_area(self):
        with mock.patch.object(theme, 'work_area', return_value=(1366, 768)):
            self.assertEqual(theme.window_floor(1.0), (1280, 720))
            self.assertEqual(theme.clamp_window_size((900, 700)), (1280, 720))

    def test_06_process_becomes_dpi_aware(self):
        if sys.platform != 'win32':
            self.skipTest('Windows-only DPI policy')
        theme.enable_dpi_awareness()
        value = ctypes.c_int()
        ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(value))
        self.assertGreaterEqual(value.value, 1,
                                'the process must render at device pixels')

    def test_07_shell_floor_adds_the_window_frame(self):
        from gui import window_policy
        self.assertEqual(window_policy.track_minimum((2304, 1488), (16, 39)),
                         (2320, 1527))
        # No handle (off-screen render, headless test): nothing is installed and
        # the fallback frame keeps the arithmetic deterministic.
        policy = window_policy.MinimumSize(0, lambda: (2304, 1488))
        self.assertFalse(policy.installed)
        self.assertEqual(policy.minimum_track(),
                         (2304 + 16, 1488 + 39))
        self.assertFalse(window_policy.resize_client(0, (100, 100)))

    def test_08_native_handle_parses_vtk_void_pointer_reprs(self):
        class FakeWindow:
            def __init__(self, value):
                self.value = value

            def GetGenericWindowId(self):
                return self.value

        self.assertEqual(
            theme.native_handle(FakeWindow('_00000000028e08fe_p_void')),
            0x28e08fe)
        self.assertEqual(theme.native_handle(FakeWindow(0x1234)), 0x1234)
        self.assertEqual(theme.native_handle(FakeWindow(None)), 0)
        self.assertEqual(theme.native_handle(FakeWindow('nonsense')), 0)
        self.assertEqual(theme.native_handle(None), 0)


class ResizeRebuildTests(unittest.TestCase):

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
            off_screen=True, window_size=(900, 600),
            timings=result.timings, cache_hit=False)

    @classmethod
    def tearDownClass(cls):
        if cls.viewer is not None:
            cls.viewer.plotter.close()
            cls.viewer = None

    def test_01_offscreen_viewer_keeps_a_size_below_the_floor(self):
        self.assertEqual(self.viewer._window_size(), (900, 600))
        self.assertEqual(tuple(self.viewer._skin_size), (900, 600))

    def test_02_interactor_configure_rebuilds_the_skin(self):
        viewer = self.viewer
        interactor = viewer.plotter.iren.interactor
        self.assertGreaterEqual(interactor.HasObserver('ConfigureEvent'), 1)
        original = viewer._window_size
        viewer._window_size = lambda: (1600, 1000)
        try:
            interactor.InvokeEvent('ConfigureEvent')
        finally:
            viewer._window_size = original
        self.assertEqual(tuple(viewer._skin_size), (1600, 1000))
        # Put the shared viewer back to its 900 x 600 layout.
        viewer._on_configure()
        self.assertEqual(tuple(viewer._skin_size), (900, 600))

    def test_03_axis_labels_follow_the_layout_scale(self):
        viewer = self.viewer
        original = viewer.layout.font_scale
        try:
            viewer.layout.font_scale = 2.0
            viewer._refresh_axes_scale()
            self.assertEqual(viewer.axes_actors['Er'].label_font, 20)
            self.assertAlmostEqual(viewer.axes_actors['Er'].label_offset, 20.0)
        finally:
            viewer.layout.font_scale = original
            viewer._refresh_axes_scale()
        self.assertEqual(viewer.axes_actors['Er'].label_font,
                         max(8, int(round(10 * original))))


if __name__ == '__main__':
    unittest.main()
