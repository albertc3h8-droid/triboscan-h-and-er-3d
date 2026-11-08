"""t58: export success feedback (full path, Explorer open policy, i18n).

All shell integration is mocked; this module never opens a real Explorer
window and never starts the product EXE.  The one VTK-based check renders an
off-screen viewer only to verify the status line stays inside the band.
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
from gui import i18n  # noqa: E402
from gui.viewer import TrialViewer  # noqa: E402
from integration.protocols import ExportResult  # noqa: E402

DATA = (Path(__file__).resolve().parents[2] / '3d hardness plot raw data').resolve()
OUT = (Path(__file__).resolve().parent / 'out' / 't58').resolve()
LONG_DIR = (OUT / 'a_very_long_customer_project_name' / 'measurements' /
            'exports' / 'run_20261102_093000')


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


class Fitter:
    def __init__(self, width):
        self.width = width

    def _window_width(self):
        return self.width

    _text_width_px = staticmethod(TrialViewer._text_width_px)


class ExportFeedbackTests(unittest.TestCase):

    def setUp(self):
        shutil.rmtree(OUT, ignore_errors=True)
        OUT.mkdir(parents=True)
        self.paths = SimpleNamespace(results=OUT / 'results')
        self.viewer = FakeViewer()
        self.result = SimpleNamespace(dataset=object())
        self.app = Application(self.paths, Options())

    def tearDown(self):
        self.app.manager.shutdown()

    def _run_export(self, *, options=None, ok=True, opener=None, files=None):
        app = Application(self.paths, options or Options())
        viewer = FakeViewer()
        files = list(files or ['a.csv', 'b.xlsx', 'c.png'])
        result = SimpleNamespace(dataset=object())
        with mock.patch('app.app.export_adapter.export_current',
                        return_value=ExportResult(ok=ok, files=files,
                                                  message='' if ok else 'export broke')):
            with mock.patch('app.app.shell_open.open_folder') as open_mock:
                if opener is not None:
                    open_mock.side_effect = opener
                app._export(viewer, result, out_dir=LONG_DIR)
                app.manager.shutdown()
        return viewer, open_mock

    def test_01_success_shows_full_export_directory(self):
        viewer, open_mock = self._run_export()
        expected = str(LONG_DIR.resolve())
        self.assertIn(expected, viewer.notice)
        self.assertNotIn(' -> ', viewer.notice)
        self.assertEqual(open_mock.call_count, 1)
        self.assertEqual(Path(open_mock.call_args.args[0]).resolve(), LONG_DIR)

    def test_02_offscreen_never_opens_folder(self):
        viewer, open_mock = self._run_export(options=Options(off_screen=True))
        self.assertIn(str(LONG_DIR.resolve()), viewer.notice)
        self.assertEqual(open_mock.call_count, 0)

    def test_03_automation_never_opens_folder(self):
        for options in (Options(auto_close_ms=1500),
                        Options(export_to=OUT / 'auto'),
                        Options(timings_json=OUT / 't.json'),
                        Options(scene_report=OUT / 'scene.json')):
            viewer, open_mock = self._run_export(options=options)
            self.assertIn(str(LONG_DIR.resolve()), viewer.notice)
            self.assertEqual(open_mock.call_count, 0, options)

    def test_04_open_failure_degrades_to_notice(self):
        viewer, open_mock = self._run_export(opener=lambda _path: False)
        self.assertIn(str(LONG_DIR.resolve()), viewer.notice)
        self.assertIn('could not open', viewer.notice)
        self.assertEqual(open_mock.call_count, 1)

    def test_05_open_exception_never_breaks_export(self):
        viewer, open_mock = self._run_export(opener=OSError('shell unavailable'))
        self.assertIn(str(LONG_DIR.resolve()), viewer.notice)
        self.assertIn('could not open', viewer.notice)
        self.assertEqual(open_mock.call_count, 1)

    def test_06_export_failure_does_not_open_folder(self):
        viewer, open_mock = self._run_export(ok=False, files=[])
        self.assertEqual(viewer.notice, 'export broke')
        self.assertEqual(open_mock.call_count, 0)

    def test_07_i18n_keeps_paths_technical(self):
        for language in ('en', 'zh-CN'):
            text = i18n.tr(language, 'notice.exported_path', count=4,
                           path=str(LONG_DIR))
            self.assertIn(str(LONG_DIR), text)
            self.assertTrue(text.strip())
        self.assertIn('已导出 4 个文件到', i18n.tr(
            'zh-CN', 'notice.exported_path', count=4, path='C:/x'))
        self.assertIn('exported 4 files to', i18n.tr(
            'en', 'notice.exported_path', count=4, path='C:/x'))

    def test_08_long_path_fits_900_and_1400_with_visible_tail(self):
        long_text = f'exported 4 files to {LONG_DIR}'
        for width in (900, 1400):
            fitter = Fitter(width)
            text = TrialViewer._fit_text_to(fitter, long_text, .012, .990, 8)
            max_px = (.990 - .012) * width
            self.assertLessEqual(TrialViewer._text_width_px(text, 8), max_px + 1.5)
            self.assertTrue(text)
            self.assertIn('run_20261102_093000', text)
            if width == 900:
                self.assertIn('...', text)


class StatusBandTests(unittest.TestCase):
    """One real off-screen render: the status actor itself, not a helper."""

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
            off_screen=True, window_size=(1400, 800),
            timings=result.timings, cache_hit=False)

    @classmethod
    def tearDownClass(cls):
        if cls.viewer is not None:
            cls.viewer.plotter.close()
            cls.viewer = None

    def test_09_status_keeps_long_path_tail_and_stays_in_band(self):
        viewer = self.viewer
        original = viewer._window_size
        notice = f'exported 4 files to {LONG_DIR}'
        try:
            for width, height in ((900, 600), (1400, 800)):
                viewer._window_size = lambda w=width, h=height: (w, h)
                viewer.set_notice(notice)
                viewer._refresh_status(render=False)
                text = viewer.notice_actor.GetInput()
                self.assertIn('run_20261102_093000', text)
                for line in text.splitlines():
                    self.assertLessEqual(
                        TrialViewer._text_width_px(line, 8),
                        (.990 - .012) * width + 1.5,
                        f'notice line wider than the band at {width}: {line!r}')
        finally:
            viewer._window_size = original
            viewer.set_notice('')
            viewer._refresh_status(render=False)


    def test_10_shell_open_swallows_startfile_errors(self):
        # t69: explorer.exe is tried first; force it to fail here so this test
        # still exercises the os.startfile fallback without launching Explorer.
        with mock.patch('app.shell_open.subprocess.Popen',
                        side_effect=OSError('no explorer')):
            with mock.patch('app.shell_open.os.startfile',
                            side_effect=OSError('no shell')) as start:
                self.assertFalse(shell_open.open_folder(OUT))
                start.assert_called_once_with(str(OUT))
        with mock.patch('app.shell_open.subprocess.Popen',
                        side_effect=OSError('no explorer')):
            with mock.patch('app.shell_open.os.startfile') as start:
                self.assertTrue(shell_open.open_folder(OUT))
                start.assert_called_once_with(str(OUT))


if __name__ == '__main__':
    unittest.main()
