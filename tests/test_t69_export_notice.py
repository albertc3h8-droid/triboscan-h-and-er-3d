"""t69: real Explorer failure reasons + a truly visible notice row.

Shell primitives are always mocked; no Explorer window is opened.  UI v2
paints the notice into its own bitmap row, so the test renders the real skin
and checks both the fitted width and the painted pixels, not a hidden VTK
actor bbox.
"""
from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from runtime.paths import prepare

PATHS = prepare()

from app import load_job, shell_open  # noqa: E402
from app.app import Application, Options  # noqa: E402
from gui.viewer import TrialViewer  # noqa: E402
from integration.protocols import ExportResult  # noqa: E402

DATA = (Path(__file__).resolve().parents[2] / '3d hardness plot raw data').resolve()
OUT = (Path(__file__).resolve().parent / 'out' / 't69').resolve()
LONG_DIR = (OUT / '长 中文 客户' / 'measurements' / 'exports' /
            'run_20261102_110214')


class FakeHandle:
    def emit(self, *args, **kwargs):
        pass

    def check_cancel(self):
        pass


class FakeViewer:
    language = 'en'
    plotter = None

    def __init__(self):
        self.notice = ''

    def build_snapshot(self, metric):
        return SimpleNamespace(metric=metric)

    def set_notice(self, text):
        self.notice = text


class ShellOpenReasonTests(unittest.TestCase):

    def setUp(self):
        shutil.rmtree(OUT, ignore_errors=True)
        OUT.mkdir(parents=True)

    def test_01_explorer_process_is_preferred(self):
        with mock.patch('app.shell_open.subprocess.Popen') as popen, \
                mock.patch('app.shell_open.os.startfile') as start:
            self.assertTrue(shell_open.open_folder(OUT))
            popen.assert_called_once_with(['explorer.exe', str(OUT)],
                                          shell=False)
            start.assert_not_called()
            self.assertEqual(shell_open.last_error(), '')

    def test_02_explorer_failure_falls_back_to_startfile(self):
        with mock.patch('app.shell_open.subprocess.Popen',
                        side_effect=OSError('explorer denied')) as popen, \
                mock.patch('app.shell_open.os.startfile') as start:
            self.assertTrue(shell_open.open_folder(OUT))
            popen.assert_called_once()
            start.assert_called_once_with(str(OUT))
            self.assertEqual(shell_open.last_error(), '')

    def test_03_both_mechanisms_fail_and_reason_is_exposed(self):
        with mock.patch('app.shell_open.subprocess.Popen',
                        side_effect=OSError('explorer denied')), \
                mock.patch('app.shell_open.os.startfile',
                           side_effect=OSError('no association')):
            self.assertFalse(shell_open.open_folder(OUT))
        reason = shell_open.last_error()
        self.assertIn('explorer.exe: OSError: explorer denied', reason)
        self.assertIn('os.startfile: OSError: no association', reason)

    def test_04_app_log_contains_real_reason_and_notice_still_degrades(self):
        app = Application(SimpleNamespace(results=OUT / 'results'), Options())
        viewer = FakeViewer()
        result = SimpleNamespace(dataset=object())
        try:
            with mock.patch(
                    'app.app.export_adapter.export_current',
                    return_value=ExportResult(ok=True, files=['a.csv'],
                                              message='')), \
                    mock.patch(
                        'app.shell_open.subprocess.Popen',
                        side_effect=PermissionError(
                            '[WinError 5] explorer denied')), \
                    mock.patch(
                        'app.shell_open.os.startfile',
                        side_effect=OSError('no association')):
                with self.assertLogs('triboscan.app', level='WARNING') as logs:
                    app._export(viewer, result, out_dir=OUT)
        finally:
            app.manager.shutdown()
        messages = [record.getMessage() for record in logs.records]
        self.assertTrue(any(
            'could not open export folder in Explorer: explorer.exe: '
            'PermissionError: [WinError 5] explorer denied' in message
            for message in messages), messages)
        self.assertTrue(any('os.startfile: OSError: no association' in message
                            for message in messages), messages)
        self.assertIn('could not open', viewer.notice)


class NoticeVisibilityTests(unittest.TestCase):
    """Skin notice row at the 1280x720 minimum and the 1536x992 design size."""

    @classmethod
    def setUpClass(cls):
        if not DATA.is_dir():
            raise unittest.SkipTest(f'raw data folder missing: {DATA}')
        shutil.rmtree(OUT / 'cache', ignore_errors=True)
        cls.result = load_job.load_directory(
            FakeHandle(), directory=str(DATA), cache_dir=OUT / 'cache',
            use_cache=False)

    def _viewer(self, width, height):
        return TrialViewer(
            self.result.dataset, self.result.field, self.result.raw_spacing,
            off_screen=True, window_size=(width, height),
            timings=self.result.timings, cache_hit=False)

    @staticmethod
    def _overlap(a, b):
        return a.x0 < b.x1 and b.x0 < a.x1 and a.y0 < b.y1 and b.y0 < a.y1

    def _dark_pixels(self, viewer, rect):
        state = viewer._skin_state()
        image = viewer.skin.render(state).convert('L')
        crop = image.crop((int(rect.x0), int(rect.y0),
                           int(rect.x1), int(rect.y1)))
        return sum(1 for value in crop.tobytes() if value < 150)

    def test_05_notice_is_visible_and_clear_at_both_sizes(self):
        notice = f'exported 4 files to {LONG_DIR}'
        for width, height in ((1280, 720), (1536, 992)):
            viewer = self._viewer(width, height)
            try:
                viewer.set_notice(notice)
                state = viewer._skin_state()
                bar = viewer.layout.notice_bar
                self.assertTrue(state.notice)
                self.assertTrue(state.notice_is_error is False)
                fitted = viewer.skin._fit_notice(state.notice)
                self.assertLessEqual(
                    viewer.skin.fonts.measure(fitted, 12),
                    bar.width - viewer.layout._d(20) + 1.0,
                    (width, height, fitted))
                self.assertGreaterEqual(bar.x0, 0.0)
                self.assertLessEqual(bar.x1, width + 1e-6)
                self.assertGreaterEqual(bar.y0, 0.0)
                self.assertLessEqual(bar.y1, height + 1e-6)
                self.assertGreater(self._dark_pixels(viewer, bar), 10,
                                   (width, height))
                for rect in (*viewer.layout.export_cards,
                             viewer.layout.export_dir_card,
                             viewer.layout.depth_card,
                             *viewer.layout.plot_cards.values(),
                             viewer.layout.status_bar):
                    self.assertFalse(self._overlap(bar, rect),
                                     (width, height, bar, rect))
            finally:
                viewer.plotter.close()

    def test_06_no_notice_keeps_the_bar_empty(self):
        for width, height in ((1280, 720), (1536, 992)):
            viewer = self._viewer(width, height)
            try:
                viewer.set_notice('')
                state = viewer._skin_state()
                self.assertEqual(state.notice, '')
                self.assertFalse(state.notice_is_error)
                self.assertGreaterEqual(len(state.status_items), 3)
                self.assertLessEqual(
                    self._dark_pixels(viewer, viewer.layout.notice_bar), 5,
                    (width, height))
            finally:
                viewer.plotter.close()


if __name__ == '__main__':
    unittest.main()
