"""t12 regression tests: v2 layout partitioning, real build_count, and the
standard-only build entry point.

The UI v2 control band is hidden, so the old VTK box checks no longer say
anything about what the user sees.  These tests read the same widget
rectangles the Pillow skin paints and hit-tests: toolbar controls must not
overlap, sidebar groups must stack, and a long notice must stay inside its
own row.
"""
from __future__ import annotations

import importlib.util
import inspect
import shutil
import sys
import unittest
from pathlib import Path

# MPLCONFIGDIR must be set before PyVista/matplotlib is imported.
from runtime.paths import prepare

PATHS = prepare()

from app import load_job  # noqa: E402
from app.app import Application  # noqa: E402
from gui.viewer import TrialViewer  # noqa: E402
from integration.protocols import Timings  # noqa: E402
from runtime import precompute  # noqa: E402

APP_DIR = Path(__file__).resolve().parents[1]
DATA = (Path(__file__).resolve().parents[2] / '3d hardness plot raw data').resolve()
OUT = (Path(__file__).resolve().parent / 'out' / 't12').resolve()


class FakeHandle:
    def __init__(self) -> None:
        self.generation = 1
        self.events: list[tuple] = []
        self.cancelled = False

    def emit(self, stage, text, fraction=None, **detail):
        self.events.append((stage, text, fraction))

    def check_cancel(self):
        pass

    def cancel(self):
        self.cancelled = True


def _load_build_module():
    path = APP_DIR / 'build' / 'build.py'
    spec = importlib.util.spec_from_file_location('t12_build_script', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class T12RegressionTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not DATA.is_dir():
            raise unittest.SkipTest(f'raw data folder missing: {DATA}')
        shutil.rmtree(OUT, ignore_errors=True)
        (OUT / 'cache').mkdir(parents=True, exist_ok=True)
        cls.result = load_job.load_directory(
            FakeHandle(), directory=str(DATA), cache_dir=OUT / 'cache',
            use_cache=False)
        cls.viewer = TrialViewer(
            cls.result.dataset, cls.result.field, cls.result.raw_spacing,
            off_screen=True, window_size=(1536, 992),
            timings=cls.result.timings, cache_hit=False)

    @classmethod
    def tearDownClass(cls):
        if cls.viewer is not None:
            try:
                cls.viewer.plotter.close()
            except Exception:  # noqa: BLE001
                pass
            cls.viewer = None

    def _widget_rect(self, name: str):
        widget = self.viewer.widgets.get(name)
        self.assertIsNotNone(widget, name)
        return widget.rect

    def test_01_toolbar_buttons_do_not_touch_their_neighbours(self):
        """v2: one toolbar row of Pillow widgets; overlapping hit boxes would
        fire the wrong action (the old band used VTK boxes)."""
        order = ('toolbar.open', 'toolbar.load', 'toolbar.help',
                 'toolbar.lang.zh', 'toolbar.lang.en')
        rects = [self._widget_rect(name) for name in order]
        for left, right in zip(rects, rects[1:]):
            self.assertLessEqual(left.x1, right.x0 + 1e-9)

    def test_02_sidebar_groups_and_export_cards_are_partitioned(self):
        """v2: sidebar groups stack without collisions and the three export
        cards end before the folder card starts."""
        rect = self._widget_rect
        self.assertLessEqual(rect('view.raw').y1, rect('range.common').y0 + 1.0)
        self.assertLessEqual(rect('range.actual').y1,
                             rect('reference.auto').y0 + 1.0)
        self.assertLessEqual(rect('reference.envelope').y1,
                             rect('interp.linear').y0 + 1.0)
        for left, right in (('export.image', 'export.field'),
                            ('export.field', 'export.raw'),
                            ('export.raw', 'export.dir'),
                            ('export.dir', 'export.browse')):
            self.assertLessEqual(rect(left).x1, rect(right).x0 + 1e-9)

    def test_03_notice_is_clipped_inside_its_own_bar(self):
        """v2: a long export path becomes one ellipsized skin line; the old
        test measured the hidden VTK status band."""
        viewer = self.viewer
        notice = ('exported 4 files -> '
                  + 'a very deep folder name / ' * 14 + 'tail')
        fitted = viewer.skin._fit_notice(notice)
        self.assertTrue(fitted.endswith('...'))
        self.assertLessEqual(
            viewer.skin.fonts.measure(fitted, 12),
            viewer.layout.notice_bar.width - viewer.layout._d(20) + 1.0)

    def test_04_notice_bar_is_clear_of_status_and_cards(self):
        """v2: the notice row is its own band between the export cards and the
        status bar; it must not intersect either."""
        layout = self.viewer.layout
        bar = layout.notice_bar
        others = [layout.status_bar, *layout.plot_cards.values(),
                  *layout.export_cards, layout.depth_card,
                  layout.export_dir_card]
        for rect in others:
            overlap = (bar.x0 < rect.x1 and rect.x0 < bar.x1
                       and bar.y0 < rect.y1 and rect.y0 < bar.y1)
            self.assertFalse(overlap, (bar, rect))

    def test_05_build_count_is_a_real_native_count(self):
        self.assertEqual(self.result.build_count, 1)
        self.assertEqual(self.result.timings.build_count, 1)
        self.assertEqual(self.result.timings.as_dict()['build_count'], 1)
        self.assertEqual(precompute.build_field_calls(), 1)
        precompute.reset_build_field_calls()
        self.assertEqual(precompute.build_field_calls(), 0)
        self.assertEqual(Timings(build_count=3).as_dict()['build_count'], 3)

    def test_06_cached_load_reports_zero_build_count(self):
        from integration import result_adapter
        if not result_adapter.available():
            self.skipTest('result_store not available')
        bundle = result_adapter.to_bundle(self.result)
        result_adapter.save(OUT / 'cache', bundle, reason='t12-test')
        cached = load_job.load_directory(
            FakeHandle(), directory=str(DATA), cache_dir=OUT / 'cache')
        self.assertTrue(cached.cache_hit)
        self.assertEqual(cached.build_count, 0)
        self.assertEqual(cached.timings.as_dict()['build_count'], 0)

    def test_07_app_export_notice_uses_full_folder_path(self):
        source = inspect.getsource(Application._export)
        self.assertIn("notice.exported_path", source)
        self.assertIn('str(out_dir)', source)
        self.assertNotIn('out_dir.name', source)
        self.assertIn('log.info', inspect.getsource(Application._log_export))

    def test_08_build_script_prepares_the_standard_entry_only(self):
        build = _load_build_module()
        command = build.STANDARD_COMMAND
        self.assertIn('-m', command)
        self.assertEqual(command[command.index('-m') + 1], 'PyInstaller')
        self.assertFalse(any('pyi_bootstrap' in str(item) for item in command))
        self.assertFalse(hasattr(build, '_run_pyinstaller'))
        source = (APP_DIR / 'build' / 'build.py').read_text(encoding='utf-8')
        self.assertNotIn('import subprocess', source)
        self.assertNotIn('PyInstaller.__main__', source)
        self.assertFalse(build.CANDIDATE_DIST.resolve() ==
                         build.FROZEN_DIST.resolve())
        self.assertIn('dist_candidate', str(build.CANDIDATE_DIST))

    def test_09_vtk_dedupe_requires_byte_identical_copies(self):
        build = _load_build_module()
        tmp = OUT / 'dedupe_tmp'
        try:
            shutil.rmtree(tmp, ignore_errors=True)
            package = tmp / 'identical' / 'pkg'
            internal = package / '_internal'
            nested = internal / 'vtk.libs'
            nested.mkdir(parents=True)
            (nested / 'vtkCommonCore-9.7.1.dll').write_bytes(b'identical')
            (internal / 'vtkCommonCore-9.7.1.dll').write_bytes(b'identical')
            build._dedupe_vtk_libs(package)
            self.assertFalse(nested.exists(), 'identical duplicate must be removed')

            package = tmp / 'different' / 'pkg'
            internal = package / '_internal'
            nested = internal / 'vtk.libs'
            nested.mkdir(parents=True)
            (nested / 'vtkCommonCore-9.7.1.dll').write_bytes(b'original')
            (internal / 'vtkCommonCore-9.7.1.dll').write_bytes(b'different')
            with self.assertRaises(SystemExit):
                build._dedupe_vtk_libs(package)
            self.assertTrue(nested.exists(), 'different bytes must be kept')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_10_depth_slider_follows_interaction_events_while_dragging(self):
        """VTK sends InteractionEvent (not MouseMoveEvent) during a drag.

        The skin slider must track the pointer for the whole drag; observing
        only MouseMoveEvent made it jump once on press and then ignore the
        mouse while the UI rotated with the drag instead.
        """
        import vtk

        viewer = self.viewer
        viewer._install_interactions()
        iren = viewer.plotter.iren.interactor
        track = viewer.layout.slider_track
        height = viewer._window_size()[1]
        y_vtk = int(height - (track.y0 + track.height / 2.0))
        lo = float(viewer.current_range.lo_nm)
        span = float(viewer.current_range.hi_nm) - lo
        before = viewer.current_depth

        def x_at(fraction):
            return int(track.x0 + track.width * fraction)

        def dispatch(event, x):
            iren.SetEventInformation(x, y_vtk)
            iren.InvokeEvent(event)

        try:
            dispatch(vtk.vtkCommand.LeftButtonPressEvent, x_at(0.2))
            self.assertTrue(viewer._slice_drag)
            self.assertAlmostEqual(viewer.current_depth, lo + 0.2 * span, delta=0.5)
            dispatch(vtk.vtkCommand.InteractionEvent, x_at(0.6))
            self.assertAlmostEqual(viewer.current_depth, lo + 0.6 * span, delta=0.5)
            dispatch(vtk.vtkCommand.InteractionEvent, x_at(0.85))
            self.assertAlmostEqual(viewer.current_depth, lo + 0.85 * span, delta=0.5)
        finally:
            dispatch(vtk.vtkCommand.LeftButtonReleaseEvent, x_at(0.85))
            viewer._slice_drag = False
            viewer.set_depth(before, render=False)


if __name__ == '__main__':
    unittest.main()
