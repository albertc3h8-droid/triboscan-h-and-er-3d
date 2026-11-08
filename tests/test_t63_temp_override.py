"""t63: the program must not depend on the inherited Windows TEMP folder.

All writable state, including ``tempfile``'s process temp dir, lives under the
program folder.  These tests use a workspace folder, never the system temp.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from integration import export_adapter
from runtime.errors import LayoutError
from runtime.paths import prepare

OUT = (Path(__file__).resolve().parent / 'out' / 't63').resolve()
BAD_TEMP = r'C:\Windows\System32\__t63_missing_temp__'


class TempOverrideTests(unittest.TestCase):

    def setUp(self):
        shutil.rmtree(OUT, ignore_errors=True)
        OUT.mkdir(parents=True)
        self.root = OUT / 'program folder'
        self.saved = {key: os.environ.get(key)
                      for key in ('TEMP', 'TMP', 'TMPDIR', 'MPLCONFIGDIR')}
        self.saved_tempdir = tempfile.tempdir

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        tempfile.tempdir = self.saved_tempdir
        shutil.rmtree(OUT, ignore_errors=True)

    def test_01_prepare_points_tempfile_at_program_cache_tmp(self):
        paths = prepare(self.root)
        expected = self.root.resolve() / 'cache' / 'tmp'
        self.assertEqual(paths.tmp, expected)
        self.assertTrue(expected.is_dir())
        self.assertEqual(tempfile.gettempdir(), str(expected))
        for key in ('TEMP', 'TMP', 'TMPDIR'):
            self.assertEqual(Path(os.environ[key]), expected)

    def test_02_bad_inherited_temp_is_replaced_not_used(self):
        os.environ['TEMP'] = BAD_TEMP
        os.environ['TMP'] = BAD_TEMP
        os.environ['TMPDIR'] = BAD_TEMP
        tempfile.tempdir = BAD_TEMP
        paths = prepare(self.root)
        expected = self.root.resolve() / 'cache' / 'tmp'
        self.assertEqual(Path(os.environ['TEMP']), expected)
        self.assertEqual(Path(os.environ['TMP']), expected)
        self.assertEqual(Path(os.environ['TMPDIR']), expected)
        self.assertEqual(tempfile.gettempdir(), str(expected))
        self.assertEqual(paths.tmp, expected)

    def test_03_unwritable_program_folder_raises_not_falls_back(self):
        blocker = OUT / 'not-a-folder'
        blocker.write_text('x', encoding='ascii')
        os.environ['TEMP'] = BAD_TEMP
        with self.assertRaises(LayoutError) as caught:
            prepare(blocker)
        self.assertIn('no fallback', str(caught.exception).lower())
        self.assertIn('not-a-folder', str(caught.exception).lower())
        self.assertEqual(os.environ['TEMP'], BAD_TEMP)

    def test_04_export_failure_log_and_notice_keep_detail(self):
        from app.app import Application
        from integration.protocols import ExportResult

        detail = ('Traceback (most recent call last): ... '
                  'PermissionError: [WinError 5] access denied while writing xlsx')
        exported = ExportResult(ok=False, message='export failed: cannot write Excel',
                                detail=detail)
        app = object.__new__(Application)
        app.exit_code = 0
        with self.assertLogs('triboscan.app', level='ERROR') as logs:
            app._log_export(exported)
        logged = '\n'.join(record.getMessage() for record in logs.records)
        self.assertIn('PermissionError', logged)
        self.assertIn('access denied', logged)

        viewer = SimpleNamespace(
            language='zh-CN', plotter=None,
            notices=[],
            build_snapshot=lambda metric: SimpleNamespace(metric=metric),
            set_notice=lambda text: viewer.notices.append(text))
        with mock.patch('app.app.export_adapter.export_current',
                        return_value=exported):
            app._export(viewer, SimpleNamespace(dataset=object()), out_dir=OUT)
        self.assertEqual(len(viewer.notices), 1)
        notice = viewer.notices[0]
        self.assertIn('导出失败', notice)
        self.assertIn('PermissionError', notice)
        clipped = notice.split('原因：', 1)[1]
        self.assertLessEqual(len(clipped), 200)
        self.assertNotIn('\n', notice)


    def test_05_adapter_passes_io_exports_detail(self):
        class FailedRaw:
            ok = False
            files = ()
            message = '无法写入 Excel 文件'
            detail = 'raw_3d_hardness.xlsx: [WinError 5] access denied'

        class FakeModule:
            @staticmethod
            def export_raw_points(dataset, out_dir, app_version, formats=None):
                return FailedRaw()

        export_adapter.set_module_for_tests(FakeModule())
        try:
            result = export_adapter.export_current(
                object(), None, SimpleNamespace(), OUT, '1.0.0')
        finally:
            export_adapter.set_module_for_tests(None)
        self.assertFalse(result.ok)
        self.assertIn('WinError 5', result.detail)
        self.assertIn('raw_3d_hardness.xlsx', result.detail)


if __name__ == '__main__':
    unittest.main()
