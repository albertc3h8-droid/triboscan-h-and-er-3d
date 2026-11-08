"""t74: choose an export folder and write the four products directly there.

All Win32/shell primitives are mocked; no real dialog or Explorer is opened.
The name-conflict checks exercise the existing io_exports helpers without
openpyxl or a render window.
"""
from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from runtime.paths import prepare

PATHS = prepare()

from app.app import Application, Options  # noqa: E402
from gui import i18n  # noqa: E402
from integration.protocols import ExportResult  # noqa: E402
from io_exports import png_export, raw_points  # noqa: E402

OUT = (Path(__file__).resolve().parent / 'out' / 't74').resolve()
BASE = '原始测量点_3d_hardness_plot_raw_data'
FILES = [f'{BASE}.csv', f'{BASE}.xlsx', f'{BASE}_导出说明.txt',
         'TRIBOSCAN_3d_hardness_plot_raw_data_[实测]_normal_both_z195.266nm_20261102T120000Z.png']


class FakeViewer:
    language = 'en'
    plotter = None

    def __init__(self, language: str = 'en'):
        self.language = language
        self.notice = ''

    def build_snapshot(self, metric):
        return SimpleNamespace(metric=metric)

    def set_notice(self, text):
        self.notice = text


class ExportPickerTests(unittest.TestCase):

    def setUp(self):
        shutil.rmtree(OUT, ignore_errors=True)
        OUT.mkdir(parents=True)
        self.results = OUT / 'results'
        self.results.mkdir()
        self.config = OUT / 'config'
        self.config.mkdir()
        self.paths = SimpleNamespace(results=self.results, config=self.config)
        self.target = OUT / '客户 导出'
        self.target.mkdir()
        self.viewer = FakeViewer()
        self.result = SimpleNamespace(dataset=object())
        self.app = Application(self.paths, Options())

    def tearDown(self):
        self.app.manager.shutdown()

    def _run(self, *, chosen, options=None):
        """Run the real ``_export`` with a fake four-file writer."""
        app = Application(self.paths, options or Options())
        calls = []

        def fake_export(dataset, plotter, snapshot, out_dir, version):
            out_dir = Path(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            for name in FILES:
                (out_dir / name).write_text(name, encoding='utf-8')
            calls.append(out_dir)
            return ExportResult(ok=True, files=[str(out_dir / n) for n in FILES],
                                message='ok')

        with mock.patch('app.app.picker.choose_folder',
                        return_value=chosen) as chooser, \
                mock.patch('app.app.export_adapter.export_current',
                           side_effect=fake_export), \
                mock.patch('app.app.shell_open.open_folder',
                           return_value=True) as opener:
            app._export(self.viewer, self.result)
        app.manager.shutdown()
        return chooser, opener, calls

    def test_01_cancel_writes_nothing_and_calls_no_shell(self):
        chooser, opener, calls = self._run(chosen=None)
        chooser.assert_called_once()
        self.assertEqual(chooser.call_args.kwargs['initial'], str(self.results))
        self.assertEqual(calls, [])
        self.assertEqual(opener.call_count, 0)
        self.assertIn('cancelled', self.viewer.notice)
        self.assertEqual(list(self.results.iterdir()), [])
        self.assertFalse((self.config / 'settings.json').exists())

    def test_02_selected_folder_is_written_directly_without_run_dir(self):
        chooser, opener, calls = self._run(chosen=str(self.target))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], self.target.resolve())
        self.assertEqual(sorted(p.name for p in self.target.iterdir()),
                         sorted(FILES))
        self.assertFalse(any(p.name.startswith('run_') for p in self.target.parent.iterdir()))
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(Path(opener.call_args.args[0]), self.target.resolve())
        self.assertIn(str(self.target.resolve()), self.viewer.notice)
        # Remembered for the next dialog.
        data = json.loads((self.config / 'settings.json').read_text(encoding='utf-8'))
        self.assertEqual(data['export_dir'], str(self.target.resolve()))

    def test_03_last_directory_is_used_as_initial_value(self):
        chooser, _opener, _calls = self._run(chosen=str(self.target))
        chooser.reset_mock()
        # A second application instance reads the same portable settings.
        second = Application(self.paths, Options())
        try:
            self.assertEqual(second._export_initial_dir(), self.target.resolve())
        finally:
            second.manager.shutdown()

    def test_04_automation_never_asks_for_a_folder(self):
        # Auto-close and off-screen paths keep the old results/run_<stamp> shape.
        for options in (Options(off_screen=True), Options(auto_close_ms=1500)):
            chooser = mock.Mock()
            captured = []

            def fake_export(dataset, plotter, snapshot, out_dir, version):
                captured.append(Path(out_dir))
                return ExportResult(ok=True, files=['x'], message='ok')

            app = Application(self.paths, options)
            try:
                with mock.patch('app.app.picker.choose_folder', chooser), \
                        mock.patch('app.app.export_adapter.export_current',
                                   side_effect=fake_export), \
                        mock.patch('app.app.shell_open.open_folder',
                                   return_value=True) as opener:
                    app._export(self.viewer, self.result)
                chooser.assert_not_called()
                self.assertEqual(opener.call_count, 0)
                self.assertEqual(len(captured), 1)
                self.assertEqual(captured[0].parent, self.results.resolve())
                self.assertTrue(captured[0].name.startswith('run_'))
            finally:
                app.manager.shutdown()

    def test_05_cli_export_to_never_asks(self):
        cli_target = OUT / 'cli target'
        app = Application(self.paths, Options(export_to=cli_target))
        chooser = mock.Mock()
        captured = []

        def fake_export(dataset, plotter, snapshot, out_dir, version):
            captured.append(Path(out_dir))
            return ExportResult(ok=True, files=['x'], message='ok')

        try:
            with mock.patch('app.app.picker.choose_folder', chooser), \
                    mock.patch('app.app.export_adapter.export_current',
                               side_effect=fake_export), \
                    mock.patch('app.app.shell_open.open_folder',
                               return_value=True):
                app._export(self.viewer, self.result)
            chooser.assert_not_called()
            self.assertEqual(captured, [cli_target.resolve()])
        finally:
            app.manager.shutdown()

    def test_06_conflict_suffix_keeps_first_files(self):
        first, _name = raw_points._resolve_targets(
            self.target, BASE, ('csv', 'xlsx'), overwrite=False)
        for path in first.values():
            path.write_text('first', encoding='utf-8')
        second, name2 = raw_points._resolve_targets(
            self.target, BASE, ('csv', 'xlsx'), overwrite=False)
        self.assertEqual(name2, f'{BASE}_2')
        self.assertEqual(second['csv'], self.target / f'{BASE}_2.csv')
        self.assertEqual((self.target / f'{BASE}.csv').read_text(encoding='utf-8'),
                         'first')
        png = self.target / 'frame.png'
        png.write_text('first png', encoding='utf-8')
        self.assertEqual(png_export._resolve_dest(png, overwrite=False),
                         self.target / 'frame_2.png')

    def test_07_corrupt_settings_do_not_block_export(self):
        settings = self.config / 'settings.json'
        settings.write_text('{not valid json', encoding='utf-8')
        chooser, opener, calls = self._run(chosen=str(self.target))
        self.assertEqual(len(calls), 1)
        self.assertEqual(opener.call_count, 1)
        repaired = json.loads(settings.read_text(encoding='utf-8'))
        self.assertEqual(repaired['export_dir'], str(self.target.resolve()))

    def test_08_unwritable_settings_do_not_block_export(self):
        with mock.patch('gui.i18n.LanguageSettings._write_dict',
                        return_value=False):
            _chooser, opener, calls = self._run(chosen=str(self.target))
        self.assertEqual(len(calls), 1)
        self.assertEqual(opener.call_count, 1)
        self.assertIn(str(self.target.resolve()), self.viewer.notice)
        self.assertIn('files to', self.viewer.notice)

    def test_09_dialog_title_is_bilingual(self):
        self.assertIn('导出文件夹', i18n.tr('zh-CN', 'dialog.export_folder'))
        self.assertIn('Choose export folder', i18n.tr('en', 'dialog.export_folder'))

    def test_10_language_save_keeps_export_dir(self):
        settings = i18n.LanguageSettings(self.config / 'settings.json')
        self.assertTrue(settings.save_export_dir(self.target))
        settings.save('zh-CN')
        data = json.loads((self.config / 'settings.json').read_text(encoding='utf-8'))
        self.assertEqual(data['language'], 'zh-CN')
        self.assertEqual(data['export_dir'], str(self.target.resolve()))
        self.assertEqual(settings.load(), 'zh-CN')

    def test_11_stale_export_dir_falls_back_to_results(self):
        settings = i18n.LanguageSettings(self.config / 'settings.json')
        stale = OUT / 'missing folder'
        (self.config / 'settings.json').write_text(
            json.dumps({'export_dir': str(stale)}), encoding='utf-8')
        self.assertIsNone(settings.load_export_dir())
        self.assertEqual(self.app._export_initial_dir(), self.results.resolve())


if __name__ == '__main__':
    unittest.main()
