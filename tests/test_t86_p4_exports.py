"""P4 (UI v2 export stage): CSV reopen, exit codes 0/3/5, field export.

These tests are headless: no VTK window, no dialogs.  They pin the behaviours
the P4 handover requires:

* opening a raw-points CSV yields a points-only result (measured points only);
* a foreign CSV is rejected with a readable, localised header error;
* export failure is rc 3, a .tsr3 write failure is rc 5, cancelling is rc 0.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from runtime.paths import prepare

PATHS = prepare()

from app import load_job  # noqa: E402
from app.app import Application, Options  # noqa: E402
from integration.protocols import ExportResult  # noqa: E402
from runtime.errors import InputError, ProductError  # noqa: E402

HEADER = ('point_id,group_id,source_file,source_line,source_row,X_mm,Y_mm,'
          'X_nm,Y_nm,heff_nm,Er_GPa,H_GPa')


class FakeHandle:
    """Minimal stand-in for app.jobs.JobHandle inside a worker call."""

    def __init__(self):
        self.generation = 1
        self.events = []
        self.cancelled = False

    def emit(self, stage, text, fraction=None, **detail):
        self.events.append((stage, text, fraction))

    def check_cancel(self):
        if self.cancelled:
            raise RuntimeError('cancelled by test')

    def cancel(self):
        self.cancelled = True


class FakeViewer:
    """Just enough viewer for the app-level export paths."""

    def __init__(self):
        self.language = 'zh-CN'
        self.plotter = None
        self.notices = []
        self.export_dir = ''

    def build_snapshot(self, metric):
        return SimpleNamespace(metric=metric, depth_nm=128.0,
                               dataset_label='demo', view_mode='normal')

    def set_notice(self, text):
        self.notices.append(text)

    @property
    def notice(self):
        return self.notices[-1] if self.notices else ''


def write_points_csv(path: Path, rows: int = 3) -> None:
    lines = [HEADER]
    for index in range(rows):
        lines.append(
            f'p{index},g{index % 2},a.txt,{index + 2},{index + 1},'
            f'{index * 0.001:.9f},0.000000000,{1000.0 + index:.6f},2000.0,'
            f'100.0,{3.0 + index:.9g},4.5')
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8-sig')


class PointsCsvOpenTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_01_csv_opens_as_measured_points_only(self):
        path = self.dir / '原始测量点_demo.csv'
        write_points_csv(path, rows=4)
        result = load_job.load_points_csv(FakeHandle(), path=str(path),
                                          language='zh-CN',
                                          rebuild_field=False)
        self.assertFalse(result.has_field)
        self.assertEqual(result.dataset.point_count, 4)
        self.assertEqual(result.dataset.kind, 'raw')
        self.assertEqual(result.source.kind, 'points_csv')
        self.assertEqual(result.build_count, 0)
        self.assertFalse(result.cache_hit)
        # The placeholder field never claims an interpolated sample.
        self.assertFalse(bool(result.field.valid.any()))
        self.assertEqual(result.field.point_count, 4)
        # Values survive the round trip through the CSV text.
        self.assertEqual(float(result.dataset.x_nm[0]), 1000.0)
        self.assertEqual(float(result.dataset.er_gpa[1]), 4.0)

    def test_02_foreign_csv_is_rejected_with_a_header_error(self):
        path = self.dir / 'foreign.csv'
        path.write_text('point_id,group_id\np,1\n', encoding='utf-8-sig')
        with self.assertRaises(InputError) as caught:
            load_job.load_points_csv(FakeHandle(), path=str(path),
                                     language='zh-CN')
        message = str(caught.exception)
        self.assertIn('不是本程序导出的原始点文件', message)
        self.assertIn('X_nm', message)

    def test_03_missing_file_uses_the_saved_result_message(self):
        with self.assertRaises(InputError) as caught:
            load_job.load_points_csv(FakeHandle(), path=str(self.dir / 'nope.csv'),
                                     language='zh-CN')
        self.assertIn('nope.csv', str(caught.exception))


class ExportExitCodeTests(unittest.TestCase):
    """P4 W4: 3 = export failed, 5 = save failed, 0 = fine/cancelled."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        paths = SimpleNamespace(results=self.root / 'results')
        self.app = Application(paths, Options(off_screen=True))
        self.viewer = FakeViewer()
        self.result = SimpleNamespace(
            dataset=object(), has_field=True,
            source=SimpleNamespace(display_label='demo'))

    def tearDown(self):
        self.app.manager.shutdown()
        self.tmp.cleanup()

    def test_01_export_failure_returns_rc3(self):
        failed = ExportResult(ok=False, message='导出失败：权限不足',
                              detail='WinError 5')
        with mock.patch('app.app.export_adapter.export_current',
                        return_value=failed):
            ok = self.app._export(self.viewer, self.result,
                                  out_dir=self.root / 'out')
        self.assertFalse(ok)
        self.assertEqual(self.app.exit_code, 3)
        self.assertIn('导出失败', self.viewer.notice)

    def test_02_export_success_keeps_rc0(self):
        done = ExportResult(ok=True, files=['a.csv'], message='ok')
        with mock.patch('app.app.export_adapter.export_current',
                        return_value=done), \
                mock.patch('app.app.shell_open.open_folder',
                           return_value=True):
            ok = self.app._export(self.viewer, self.result,
                                  out_dir=self.root / 'out')
        self.assertTrue(ok)
        self.assertEqual(self.app.exit_code, 0)
        self.assertIn('已导出', self.viewer.notice)

    def test_03_field_write_failure_returns_rc5(self):
        with mock.patch.object(self.app, '_write_result_file',
                               side_effect=PermissionError(13, 'Permission denied')):
            ok = self.app._export(self.viewer, self.result, kind='field')
        self.assertFalse(ok)
        self.assertEqual(self.app.exit_code, 5)
        self.assertIn('导出失败', self.viewer.notice)

    def test_04_cancelled_save_dialog_is_not_a_failure(self):
        with mock.patch.object(self.app, '_is_interactive_gui',
                               return_value=True), \
                mock.patch('app.app.picker.choose_save_file',
                           return_value=None):
            ok = self.app._export(self.viewer, self.result, kind='field')
        self.assertTrue(ok)
        self.assertEqual(self.app.exit_code, 0)
        self.assertIn('取消', self.viewer.notice)

    def test_05_save_dialog_error_returns_rc5(self):
        with mock.patch.object(self.app, '_is_interactive_gui',
                               return_value=True), \
                mock.patch('app.app.picker.choose_save_file',
                           side_effect=ProductError('no dialog')):
            ok = self.app._export(self.viewer, self.result, kind='field')
        self.assertFalse(ok)
        self.assertEqual(self.app.exit_code, 5)

    def test_06_first_failure_code_wins(self):
        self.app._fail(3)
        self.app._fail(5)
        self.assertEqual(self.app.exit_code, 3)

    def test_07_field_card_skips_a_points_only_result(self):
        # A CSV-opened result has no interpolated field: the card explains
        # that instead of writing an all-invalid placeholder .tsr3.
        result = SimpleNamespace(dataset=object(), has_field=False,
                                 source=SimpleNamespace(display_label='demo'))
        with mock.patch.object(self.app, '_write_result_file') as write:
            ok = self.app._export(self.viewer, result, kind='field')
        self.assertTrue(ok)
        self.assertEqual(self.app.exit_code, 0)
        write.assert_not_called()
        self.assertIn('仅显示实测测点', self.viewer.notice)


if __name__ == '__main__':
    unittest.main()
