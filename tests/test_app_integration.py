"""App integration tests (unittest only; no pytest in the portable env).

Run with either interpreter from ``triboscan_build/desktop_app``::

    ..\\native_viewer\\.venv\\Scripts\\python.exe -m unittest discover -s tests -t . -v
    .buildenv\\Scripts\\python.exe -m unittest discover -s tests -t . -v

The Excel/PNG assertions need openpyxl and are skipped on the development venv.
All VTK work is off-screen; no user window is opened.
"""
from __future__ import annotations

import csv
import os
import shutil
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

# MPLCONFIGDIR must be set before PyVista/matplotlib is imported.
from runtime.paths import prepare

PATHS = prepare()

import numpy as np  # noqa: E402

from app import load_job, main as app_main  # noqa: E402
from app.app import Application, Options  # noqa: E402
from app.jobs import JobManager  # noqa: E402
from gui.viewer import TrialViewer  # noqa: E402
from integration import export_adapter, result_adapter  # noqa: E402
from integration.protocols import APP_VERSION  # noqa: E402
from runtime.errors import Cancelled, InputError  # noqa: E402
from runtime import field3d  # noqa: E402

DATA = (Path(__file__).resolve().parents[2] / '3d hardness plot raw data').resolve()
OUT = (Path(__file__).resolve().parent / 'out').resolve()
CACHE = OUT / 'cache_test'


class FakeHandle:
    """Minimal stand-in for app.jobs.JobHandle inside a worker call."""

    def __init__(self) -> None:
        self.generation = 1
        self.events: list[tuple] = []
        self.cancelled = False

    def emit(self, stage, text, fraction=None, **detail):
        self.events.append((stage, text, fraction))

    def check_cancel(self):
        if self.cancelled:
            raise Cancelled('cancelled by test')

    def cancel(self):
        self.cancelled = True


class AppIntegrationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not DATA.is_dir():
            raise unittest.SkipTest(f'raw data folder missing: {DATA}')
        shutil.rmtree(OUT, ignore_errors=True)
        OUT.mkdir(parents=True, exist_ok=True)
        CACHE.mkdir(parents=True, exist_ok=True)
        cls.handle = FakeHandle()
        cls.result = load_job.load_directory(
            cls.handle, directory=str(DATA), cache_dir=CACHE)
        cls.viewer = None

    @classmethod
    def tearDownClass(cls):
        if cls.viewer is not None:
            try:
                cls.viewer.plotter.close()
            except Exception:  # noqa: BLE001
                pass
            cls.viewer = None

    # ------------------------------------------------------------------ load
    def test_01_fresh_load_timings_and_build_count(self):
        r = self.result
        self.assertEqual(r.build_count, 1)
        self.assertFalse(r.cache_hit)
        self.assertGreater(r.dataset.point_count, 0)
        self.assertGreater(r.dataset.group_count, 0)
        self.assertIsNotNone(r.timings.read_s)
        self.assertIsNotNone(r.timings.compute_s)
        self.assertGreater(r.timings.read_s, 0.0)
        self.assertGreater(r.timings.compute_s, 0.0)
        self.assertGreater(r.timings.total_s, 0.0)
        steps = [s['step'] for s in r.timings.compute_steps]
        self.assertIn('field', steps)
        self.assertIn('spacing', steps)
        field_s = sum(s['seconds'] for s in r.timings.compute_steps
                      if s['step'] == 'field')
        spacing_s = sum(s['seconds'] for s in r.timings.compute_steps
                        if s['step'] == 'spacing')
        self.assertAlmostEqual(field_s + spacing_s, r.timings.compute_s,
                               delta=0.05)
        # The slow-compute decision uses compute only.
        self.assertFalse(r.timings.slow_compute)

    def test_02_save_result_and_reopen_without_building_field(self):
        self.assertTrue(result_adapter.available())
        bundle = result_adapter.to_bundle(self.result)
        target = OUT / 'roundtrip.tsr3'
        if target.exists():
            target.unlink()
        saved = result_adapter.save_file(bundle, target)
        self.assertTrue(saved.is_file())
        self.assertGreater(saved.stat().st_size, 1000)
        with mock.patch.object(field3d, 'build_field',
                               side_effect=AssertionError('build_field called')):
            reopened = load_job.load_result_file(FakeHandle(), path=str(saved))
        self.assertEqual(reopened.build_count, 0)
        self.assertTrue(reopened.cache_hit)
        self.assertEqual(reopened.dataset.point_count,
                         self.result.dataset.point_count)
        np.testing.assert_allclose(reopened.field.er_gpa,
                                   self.result.field.er_gpa, rtol=0, atol=0,
                                   equal_nan=True)
        np.testing.assert_array_equal(reopened.field.valid,
                                      self.result.field.valid)
        self.assertTrue(reopened.source.source_verified)

    def test_03_user_save_enables_folder_cache_without_rebuild(self):
        bundle = result_adapter.to_bundle(self.result)
        saved = result_adapter.save(CACHE, bundle, 'unittest user save')
        self.assertTrue(saved.ok, saved.message)
        with mock.patch.object(field3d, 'build_field',
                               side_effect=AssertionError('build_field called')):
            again = load_job.load_directory(FakeHandle(), directory=str(DATA),
                                            cache_dir=CACHE)
        self.assertTrue(again.cache_hit)
        self.assertEqual(again.build_count, 0)
        self.assertLess(again.timings.compute_s, 1.0)
        self.assertEqual(again.dataset.point_count,
                         self.result.dataset.point_count)

    def test_04_force_recompute_and_no_cache_still_build(self):
        forced = load_job.load_directory(FakeHandle(), directory=str(DATA),
                                         cache_dir=CACHE, force_recompute=True)
        self.assertEqual(forced.build_count, 1)
        self.assertFalse(forced.cache_hit)
        uncached = load_job.load_directory(FakeHandle(), directory=str(DATA),
                                           cache_dir=CACHE, use_cache=False)
        self.assertEqual(uncached.build_count, 1)
        self.assertFalse(uncached.cache_hit)

    def test_05_empty_folder_is_a_clean_input_error(self):
        empty = OUT / 'empty_folder'
        empty.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(InputError):
            load_job.scan_analysis_files(empty)
        code = app_main.main(['--data', str(empty), '--offscreen',
                              '--window-size', '800x600'])
        self.assertEqual(code, 2)

    # ------------------------------------------------------------ job thread
    def test_06_single_worker_serial_and_stale_generation_discarded(self):
        manager = JobManager()
        order: list[str] = []
        active = 0
        max_active = 0
        lock = threading.Lock()

        def slow(handle):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
                order.append(f'start{handle.generation}')
            try:
                for _ in range(200):
                    handle.check_cancel()
                    time.sleep(0.005)
                order.append(f'end{handle.generation}')
                return 'first'
            except Cancelled:
                order.append(f'cancel{handle.generation}')
                raise
            finally:
                with lock:
                    active -= 1

        def quick(handle):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
                order.append(f'start{handle.generation}')
            try:
                time.sleep(0.02)
                order.append(f'end{handle.generation}')
                return 'second'
            finally:
                with lock:
                    active -= 1

        first = manager.start('load', {'fn': slow})
        deadline = time.time() + 3
        while f'start{first.generation}' not in order and time.time() < deadline:
            time.sleep(0.005)
        second = manager.start('load', {'fn': quick})
        self.assertTrue(second.generation > first.generation)
        first.done.wait(5)
        second.done.wait(5)
        self.assertTrue(first.outcome.cancelled)
        self.assertEqual(second.outcome.result, 'second')
        self.assertEqual(max_active, 1)
        self.assertIn(f'cancel{first.generation}', order)
        self.assertEqual(order.index(f'start{second.generation}'),
                         order.index(f'cancel{first.generation}') + 1)
        self.assertLess(order.index(f'start{second.generation}'),
                        order.index(f'end{second.generation}'))
        manager.shutdown()
        with self.assertRaises(RuntimeError):
            manager.start('load', {'fn': quick})

    # ---------------------------------------------------------------- viewer
    def test_07_viewer_consumes_precomputed_field_only(self):
        with mock.patch.object(field3d, 'build_field',
                               side_effect=AssertionError('build_field called')):
            viewer = TrialViewer(self.result.dataset, self.result.field,
                                 self.result.raw_spacing, off_screen=True,
                                 window_size=(1400, 800),
                                 timings=self.result.timings,
                                 cache_hit=self.result.cache_hit,
                                 source_note=self.result.source.note)
        type(self).viewer = viewer
        report = viewer.scene_report()
        self.assertEqual(report['product']['app'], 'TriboScan3D')
        self.assertEqual(report['product']['raw_spacing_computations'], 0)
        self.assertEqual(report['product']['cache_hit'], False)
        self.assertEqual(set(report['buttons']),
                         {'normal', 'locate', 'raw', 'reset',
                          'common', 'actual', 'auto', 'layers', 'envelope',
                          'open', 'export', 'save', 'load'})
        self.assertEqual(report['buttons']['save']['state'], 'idle')
        self.assertIsNotNone(report['product']['timings'])
        with self.assertRaises(ValueError):
            TrialViewer(self.result.dataset, None, self.result.raw_spacing,
                        off_screen=True)
        # Kept open for test_08; tearDownClass closes the render window.

    def test_08_export_real_csv_and_png(self):
        try:
            import PIL.Image  # noqa: F401
        except ImportError:
            self.skipTest('Pillow is not installed in this interpreter')
        if self.viewer is None:
            self.test_07_viewer_consumes_precomputed_field_only()
        viewer = self.viewer
        viewer.plotter.render()  # the PNG adapter needs a live render window
        snapshot = viewer.build_snapshot('both')
        self.assertEqual(snapshot.depth_kind, 'slice')
        self.assertIsNotNone(snapshot.depth_nm)
        out_dir = OUT / 'export'
        exported = export_adapter.export_current(
            self.result.dataset, viewer.clean_plotter(), snapshot, out_dir,
            app_version=APP_VERSION)
        self.assertTrue(exported.ok, exported.message)
        files = [Path(p) for p in exported.files]
        csvs = [p for p in files if p.suffix.lower() == '.csv']
        pngs = [p for p in files if p.suffix.lower() == '.png']
        others = [p for p in files
                  if p.suffix.lower() not in ('.csv', '.png', '.txt')]
        self.assertTrue(csvs, exported.files)
        self.assertTrue(pngs, exported.files)
        # P4 W3: the raw-point export is CSV only; no Excel file is written.
        self.assertEqual(others, [], exported.files)
        self.assertIn('_both_', pngs[0].name)
        self.assertIn('z', pngs[0].name)
        self.assertIn('nm', pngs[0].name)
        with csvs[0].open('r', encoding='utf-8-sig', newline='') as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(len(rows) - 1, self.result.dataset.point_count)
        self.assertEqual(rows[0], ['point_id', 'group_id', 'source_file',
                                   'source_line', 'source_row', 'X_mm', 'Y_mm',
                                   'X_nm', 'Y_nm', 'heff_nm', 'Er_GPa', 'H_GPa'])
        image = PIL.Image.open(pngs[0])
        text = dict(image.text or {})
        self.assertAlmostEqual(float(text['Depth_nm']), snapshot.depth_nm,
                               places=3)
        self.assertEqual(text['DepthKind'], 'slice')
        self.assertIn('nm', text['Units'])
        self.assertGreater(image.width, 100)
        # P4 W6: the figure is the plot-card union plus an annotation band;
        # no toolbar, sidebar or export cards are in the exported pixels.
        x0, y0, x1, y1 = viewer.clean_export_rect()
        self.assertEqual(image.width, x1 - x0)
        self.assertGreater(image.height, y1 - y0)
        self.assertLess(image.width, viewer._window_width())
        image.close()

    def test_08b_field_export_card_writes_a_reopenable_tsr3(self):
        if self.viewer is None:
            self.test_07_viewer_consumes_precomputed_field_only()
        viewer = self.viewer
        out_dir = OUT / 'export_field'
        shutil.rmtree(out_dir, ignore_errors=True)
        app = Application(PATHS, Options(off_screen=True))
        try:
            with mock.patch.object(app, '_export_initial_dir',
                                   return_value=out_dir):
                ok = app._export(viewer, self.result, kind='field')
            self.assertTrue(ok, viewer._notice)
            self.assertEqual(app.exit_code, 0)
        finally:
            app.manager.shutdown()
        files = sorted(out_dir.glob('*.tsr3'))
        self.assertEqual(len(files), 1, [p.name for p in out_dir.iterdir()])
        self.assertTrue(files[0].name.startswith('插值场_'))
        # W2 acceptance: the exported .tsr3 reopens without any field rebuild.
        with mock.patch.object(field3d, 'build_field',
                               side_effect=AssertionError('build_field called')):
            reopened = load_job.load_result_file(FakeHandle(), path=str(files[0]))
        self.assertTrue(reopened.cache_hit)
        self.assertEqual(reopened.build_count, 0)
        self.assertEqual(reopened.dataset.point_count,
                         self.result.dataset.point_count)
        np.testing.assert_array_equal(reopened.field.valid,
                                      self.result.field.valid)

    def test_09_save_button_writes_a_reopenable_result(self):
        if self.viewer is None:
            self.test_07_viewer_consumes_precomputed_field_only()
        viewer = self.viewer
        # The real button is not clickable before a slow compute, and says why.
        report = viewer.scene_report()
        box = report['buttons']['save']['box']
        width, height = viewer._window_size()
        px = (box[0] + box[1]) / 2.0 * width
        py = (box[2] + box[3]) / 2.0 * height
        self.assertIsNone(viewer._button_at(px, py))
        called = []
        viewer.set_file_callbacks(save=lambda v: called.append(v),
                                  open_data=lambda v: called.append('open'),
                                  load_result=lambda v: called.append('load'))
        viewer._handle_file_action('open')
        viewer._handle_file_action('load')
        self.assertEqual(called, ['open', 'load'])
        called.clear()
        viewer._handle_file_action('save')
        self.assertEqual(called, [])
        self.assertIn('>5 s', viewer._notice)
        viewer.set_save_available(True)
        self.assertEqual(viewer._button_at(px, py), 'save')
        viewer._handle_file_action('save')
        self.assertEqual(called, [viewer])
        # The app-level Save As handler writes a reopenable .tsr3 result.
        target = OUT / 'ui_saved.tsr3'
        if target.exists():
            target.unlink()
        app = Application(PATHS, Options(off_screen=True))
        with mock.patch('app.app.picker.choose_save_file',
                        return_value=str(target)):
            app._save(viewer, self.result)
        self.assertTrue(target.is_file(), viewer._notice)
        self.assertTrue(viewer._save_done)
        with mock.patch.object(field3d, 'build_field',
                               side_effect=AssertionError('build_field called')):
            reopened = load_job.load_result_file(FakeHandle(), path=str(target))
        self.assertEqual(reopened.build_count, 0)
        np.testing.assert_array_equal(reopened.field.valid,
                                      self.result.field.valid)


if __name__ == '__main__':
    unittest.main(verbosity=2)
