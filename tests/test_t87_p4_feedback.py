"""P4 acceptance feedback: CSV rebuild, cloud slice extremes, dead controls.

The user opened a raw-points CSV and found every view control inert, because
the CSV carries no interpolated field.  Opening a CSV therefore rebuilds the
field from its measured points by default, and the points-only fallback now
also greys out the controls it cannot honour.  In the cloud view the slice
face is hidden when it sits exactly at the data's top or bottom.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from runtime.paths import prepare

PATHS = prepare()

from app import load_job  # noqa: E402
from gui.ui_layout import Rect  # noqa: E402
from gui.ui_widgets import Widget, WidgetSet  # noqa: E402
from gui.viewer import TrialViewer  # noqa: E402

HEADER = ('point_id,group_id,source_file,source_line,source_row,X_mm,Y_mm,'
          'X_nm,Y_nm,heff_nm,Er_GPa,H_GPa')


class FakeHandle:

    generation = 1

    def emit(self, *args, **kwargs):
        pass

    def check_cancel(self):
        pass

    def cancel(self):
        pass


def write_grid_csv(path: Path, *, rows: int = 12, depths: int = 3) -> None:
    """A small regular grid: enough for build_field, still fast in tests."""
    lines = [HEADER]
    number = 0
    for depth in range(depths):
        for y in range(rows):
            for x in range(rows):
                lines.append(
                    f'p{number},g{depth},a.txt,{number + 2},{number + 1},'
                    f'0,0,{10000.0 + x * 500.0:.3f},'
                    f'{20000.0 + y * 500.0:.3f},{depth * 2.0:.3f},'
                    f'{50.0 + x + y + depth:.6f},'
                    f'{5.0 + 0.1 * x + 0.05 * y + depth:.6f}')
                number += 1
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8-sig')


class CsvFieldRebuildTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_01_rebuild_is_on_by_default_and_yields_a_real_field(self):
        path = self.dir / '点.csv'
        write_grid_csv(path)
        result = load_job.load_points_csv(FakeHandle(), path=str(path),
                                          language='zh-CN')
        self.assertTrue(result.has_field)
        self.assertGreater(result.build_count, 0)
        self.assertTrue(bool(result.field.valid.any()))
        self.assertFalse(result.cache_hit)
        self.assertIn('rebuilt', result.source.note)
        self.assertEqual(result.dataset.point_count, 12 * 12 * 3)

    def test_02_rebuild_can_be_skipped_for_a_points_only_result(self):
        path = self.dir / '点.csv'
        write_grid_csv(path)
        result = load_job.load_points_csv(FakeHandle(), path=str(path),
                                          language='zh-CN',
                                          rebuild_field=False)
        self.assertFalse(result.has_field)
        self.assertEqual(result.build_count, 0)
        self.assertFalse(bool(result.field.valid.any()))

    def test_03_broken_geometry_falls_back_to_points_only(self):
        path = self.dir / 'line.csv'
        lines = [HEADER]
        for index in range(3):
            lines.append(
                f'p{index},g0,a.txt,{index + 2},{index + 1},0,0,'
                f'{10000.0:.3f},20000.0,0.000,50.0,5.0')
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8-sig')
        result = load_job.load_points_csv(FakeHandle(), path=str(path),
                                          language='zh-CN')
        self.assertFalse(result.has_field)
        self.assertEqual(result.dataset.point_count, 3)
        self.assertTrue(result.warnings)
        self.assertFalse(bool(result.field.valid.any()))


class CloudSliceEdgeTests(unittest.TestCase):
    """The slice face disappears at the top/bottom of the cloud."""

    def viewer(self, *, mode='cloud', lo=62.5, hi=195.0, depth=128.0):
        viewer = TrialViewer.__new__(TrialViewer)
        viewer.display_mode = mode
        viewer.current_range = SimpleNamespace(valid=True, lo_nm=lo,
                                               hi_nm=hi)
        viewer.current_depth = depth
        return viewer

    def test_01_hidden_at_both_extremes_only_in_cloud_mode(self):
        top = self.viewer(depth=195.0)
        bottom = self.viewer(depth=62.5)
        middle = self.viewer(depth=128.0)
        near_top = self.viewer(depth=194.9)
        self.assertTrue(top._slice_at_cloud_edge())
        self.assertTrue(bottom._slice_at_cloud_edge())
        self.assertFalse(middle._slice_at_cloud_edge())
        self.assertFalse(near_top._slice_at_cloud_edge())
        self.assertFalse(
            self.viewer(mode='slice', depth=195.0)._slice_at_cloud_edge())

    def test_02_invalid_range_never_hides(self):
        viewer = self.viewer(depth=62.5)
        viewer.current_range = SimpleNamespace(valid=False, lo_nm=0.0,
                                               hi_nm=1.0)
        self.assertFalse(viewer._slice_at_cloud_edge())


class PointsOnlyControlTests(unittest.TestCase):

    def build_widgets(self):
        widgets = []
        for widget_id in TrialViewer._FIELD_ONLY_WIDGETS:
            widgets.append(Widget(id=widget_id, kind='button',
                                  rect=Rect(0, 0, 10, 10)))
        return WidgetSet(widgets)

    def test_01_field_controls_are_disabled_for_points_only(self):
        viewer = TrialViewer.__new__(TrialViewer)
        viewer.points_only = True
        widgets = self.build_widgets()
        viewer._sync_points_only_widgets(widgets)
        self.assertTrue(all(widget.disabled for widget in widgets))

    def test_02_field_controls_stay_enabled_with_a_field(self):
        viewer = TrialViewer.__new__(TrialViewer)
        viewer.points_only = False
        widgets = self.build_widgets()
        viewer._sync_points_only_widgets(widgets)
        self.assertFalse(any(widget.disabled for widget in widgets))


if __name__ == '__main__':
    unittest.main()
