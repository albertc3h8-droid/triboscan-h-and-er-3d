"""P5: display-only cubic / thin-plate-spline slice interpolation.

The measured XY pitch of the real sample is 3 um, so a Z slice cut from the
linear Delaunay/probe field is a patchwork of tetrahedral facets (the visible
cross / colour blocks).  The viewer can instead interpolate the measured
points directly when the XY positions form a complete grid: a natural cubic
spline or a thin-plate spline in X/Y plus a PCHIP along every measured depth
column.  Both are true interpolants - they pass through each measurement -
not a smoothing filter.  TPS is isotropic, so a peak does not grow the
axis-aligned cross of the tensor-product cubic.  The stored FieldGrid and
every export keep the raw linear values.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import numpy as np

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime import field3d  # noqa: E402
from gui import i18n  # noqa: E402
from gui.ui_layout import Rect  # noqa: E402
from gui.ui_widgets import Widget, WidgetSet  # noqa: E402
from gui.viewer import TrialViewer  # noqa: E402


def make_field(values, dx=500.0, dy=500.0, dz=2.0):
    values = np.asarray(values, dtype=np.float64)
    nz, ny, nx = values.shape
    valid = np.isfinite(values)
    er = np.where(valid, values, np.nan)
    return field3d.FieldGrid(
        x0_nm=0.0, y0_nm=0.0, z0_nm=0.0,
        dx_nm=dx, dy_nm=dy, dz_nm=dz,
        nx=nx, ny=ny, nz=nz,
        valid=valid, er_gpa=er, h_gpa=er.copy(),
        point_count=0, delaunay_cells=0,
        xy_step_requested_nm=dx, z_step_requested_nm=dz)


def regular_dataset(xs, ys, zs, er_fn, h_fn):
    rows = []
    for y in ys:
        for x in xs:
            for z in zs:
                rows.append((x, y, z, er_fn(x, y, z), h_fn(x, y, z)))
    data = np.asarray(rows, dtype=np.float64)
    return SimpleNamespace(x_nm=data[:, 0], y_nm=data[:, 1], z_nm=data[:, 2],
                           er_gpa=data[:, 3], h_gpa=data[:, 4])


class CubicCoreTests(unittest.TestCase):
    def test_pchip_reproduces_every_measured_depth(self):
        x = np.array([0.0, 1.0, 2.0, 4.0])
        y = np.array([0.0, 0.5, 0.5, 1.0])
        m = field3d._pchip_slopes(x, y)
        for xi, yi in zip(x, y):
            self.assertAlmostEqual(field3d._pchip_value(x, y, m, xi), yi,
                                   places=12)

    def test_pchip_keeps_a_monotone_column_monotone(self):
        x = np.arange(5.0)
        y = np.array([0.0, 0.0, 1.0, 1.0, 1.0])
        m = field3d._pchip_slopes(x, y)
        samples = np.linspace(0.0, 4.0, 401)
        values = np.array([field3d._pchip_value(x, y, m, q) for q in samples])
        self.assertTrue(np.all(np.diff(values) >= -1e-12))
        self.assertGreaterEqual(float(values.min()), 0.0)
        self.assertLessEqual(float(values.max()), 1.0)

    def test_natural_cubic_reproduces_knots_and_linear_data(self):
        x = np.array([0.0, 1.0, 2.5, 4.0])
        y = 2.0 - 0.75 * x
        m = field3d._natural_cubic_curvature(x, y)
        np.testing.assert_allclose(
            field3d._natural_cubic_values(x, y, m, x), y, atol=1e-12)
        q = np.linspace(0.0, 4.0, 31)
        np.testing.assert_allclose(
            field3d._natural_cubic_values(x, y, m, q), 2.0 - 0.75 * q,
            atol=1e-12)


class CubicSliceTests(unittest.TestCase):
    def setUp(self):
        self.xs = np.array([0.0, 1000.0, 2000.0, 3000.0])
        self.ys = np.array([0.0, 1000.0, 2000.0])
        self.zs = np.array([10.0, 20.0, 30.0])
        self.dataset = regular_dataset(
            self.xs, self.ys, self.zs,
            lambda x, y, z: 5.0 + 0.01 * x + 0.02 * y + 0.001 * z * z,
            lambda x, y, z: 1.0 + 0.1 * x - 0.05 * y + 0.01 * z)
        self.field = SimpleNamespace(
            x_nm=np.arange(0.0, 3000.0 + 1.0, 500.0),
            y_nm=np.arange(0.0, 2000.0 + 1.0, 500.0))

    def test_cubic_slice_is_exact_at_measured_depth(self):
        ix = [int(np.where(np.isclose(self.field.x_nm, x))[0][0])
              for x in self.xs]
        iy = [int(np.where(np.isclose(self.field.y_nm, y))[0][0])
              for y in self.ys]
        for depth in self.zs:
            planes = field3d.slice_planes_cubic(self.dataset, self.field, depth)
            self.assertIsNotNone(planes)
            for metric, attr in (('Er', 'er_gpa'), ('H', 'h_gpa')):
                expected = np.array([
                    [getattr(self.dataset, attr)[
                        (np.isclose(self.dataset.x_nm, x))
                        & (np.isclose(self.dataset.y_nm, y))
                        & (np.isclose(self.dataset.z_nm, depth))][0]
                     for x in self.xs] for y in self.ys])
                actual = planes[metric].values[np.ix_(iy, ix)]
                np.testing.assert_allclose(actual, expected, atol=1e-12)
                self.assertEqual(planes[metric].method,
                                 field3d._CUBIC_SLICE_METHOD)

    def test_depth_between_measured_layers_uses_pchip(self):
        depth = 15.0
        planes = field3d.slice_planes_cubic(self.dataset, self.field, depth)
        self.assertIsNotNone(planes)
        # The first column (x=0, y=0) is Er = 5 + 0.001 z^2; PCHIP through
        # its three measured depths gives the value at z=15, and the tensor
        # product reproduces that node exactly at x=0, y=0.
        column = 5.0 + 0.001 * self.zs ** 2
        slopes = field3d._pchip_slopes(self.zs, column)
        expected = field3d._pchip_value(self.zs, column, slopes, depth)
        self.assertAlmostEqual(float(planes['Er'].values[0, 0]), expected,
                               places=10)
        self.assertTrue(np.all(np.isfinite(planes['Er'].values)))

    def test_cubic_slice_refuses_out_of_column_range_and_irregular_grid(self):
        planes = field3d.slice_planes_cubic(self.dataset, self.field, 5.0)
        self.assertIsNone(planes)
        irregular = SimpleNamespace(
            x_nm=np.array([0.0, 1.0, 0.0, 1.0, 0.0]),
            y_nm=np.array([0.0, 0.0, 1.0, 1.0, 1.0]),
            z_nm=np.array([1.0, 1.0, 1.0, 1.0, 1.0]),
            er_gpa=np.ones(5), h_gpa=np.ones(5))
        self.assertIsNone(field3d.slice_planes_cubic(
            irregular, SimpleNamespace(x_nm=np.array([0.0, 1.0]),
                                       y_nm=np.array([0.0, 1.0])), 1.0))

    def test_linear_slice_remains_the_stored_field_path(self):
        values = np.zeros((3, 7, 7))
        for layer in range(3):
            values[layer] = layer * 10.0 + np.arange(7)[None, :] \
                + 0.5 * np.arange(7)[:, None]
        field = make_field(values)
        plane = field3d.slice_plane(field, 'z', 1.0)
        expected = 0.5 * (values[0] + values[1])
        np.testing.assert_allclose(plane.values, expected)
        self.assertEqual(plane.method, field3d._SLICE_METHOD)


class TpsSliceTests(unittest.TestCase):
    def setUp(self):
        self.xs = np.array([0.0, 1000.0, 2000.0, 3000.0])
        self.ys = np.array([0.0, 1000.0, 2000.0])
        self.zs = np.array([10.0, 20.0, 30.0])
        self.dataset = regular_dataset(
            self.xs, self.ys, self.zs,
            lambda x, y, z: 5.0 + 0.01 * x + 0.02 * y + 0.001 * z * z,
            lambda x, y, z: 1.0 + 0.1 * x - 0.05 * y + 0.01 * z)
        self.field = SimpleNamespace(
            x_nm=np.arange(0.0, 3000.0 + 1.0, 500.0),
            y_nm=np.arange(0.0, 2000.0 + 1.0, 500.0))

    def test_tps_is_exact_at_every_measured_node(self):
        ix = [int(np.where(np.isclose(self.field.x_nm, x))[0][0])
              for x in self.xs]
        iy = [int(np.where(np.isclose(self.field.y_nm, y))[0][0])
              for y in self.ys]
        for depth in self.zs:
            planes = field3d.slice_planes_tps(self.dataset, self.field, depth)
            self.assertIsNotNone(planes)
            for metric, attr in (('Er', 'er_gpa'), ('H', 'h_gpa')):
                expected = np.array([
                    [getattr(self.dataset, attr)[
                        (np.isclose(self.dataset.x_nm, x))
                        & (np.isclose(self.dataset.y_nm, y))
                        & (np.isclose(self.dataset.z_nm, depth))][0]
                     for x in self.xs] for y in self.ys])
                actual = planes[metric].values[np.ix_(iy, ix)]
                np.testing.assert_allclose(actual, expected, atol=1e-9)
                self.assertEqual(planes[metric].method,
                                 field3d._TPS_SLICE_METHOD)

    def test_tps_refuses_out_of_column_range_and_irregular_grid(self):
        self.assertIsNone(field3d.slice_planes_tps(
            self.dataset, self.field, 5.0))
        irregular = SimpleNamespace(
            x_nm=np.array([0.0, 1.0, 0.0, 1.0, 0.0]),
            y_nm=np.array([0.0, 0.0, 1.0, 1.0, 1.0]),
            z_nm=np.array([1.0, 1.0, 1.0, 1.0, 1.0]),
            er_gpa=np.ones(5), h_gpa=np.ones(5))
        self.assertIsNone(field3d.slice_planes_tps(
            irregular, SimpleNamespace(x_nm=np.array([0.0, 1.0]),
                                       y_nm=np.array([0.0, 1.0])), 1.0))


class ViewerInterpolationTests(unittest.TestCase):
    def test_display_grid_is_denser_than_the_stored_field(self):
        viewer = TrialViewer.__new__(TrialViewer)
        viewer.field = SimpleNamespace(
            x_nm=np.linspace(0.0, 1000.0, 5),
            y_nm=np.linspace(0.0, 500.0, 3))
        display = viewer._slice_display_field()
        self.assertEqual(display.x_nm.size,
                         4 * (viewer.field.x_nm.size - 1) + 1)
        self.assertEqual(display.y_nm.size,
                         4 * (viewer.field.y_nm.size - 1) + 1)
        self.assertAlmostEqual(display.x_nm[0], 0.0)
        self.assertAlmostEqual(display.x_nm[-1], 1000.0)

    def test_setter_accepts_three_modes_and_redraws(self):
        viewer = TrialViewer.__new__(TrialViewer)
        viewer.points_only = False
        viewer.interpolation = 'tps'
        calls = []
        viewer._update_slice = lambda render=True: calls.append(('slice', render))
        viewer._refresh_status = lambda render=True: calls.append(('status', render))
        viewer.plotter = type('P', (), {'render': lambda self: calls.append(('render',))})()
        viewer.set_interpolation('linear', render=True)
        self.assertEqual(viewer.interpolation, 'linear')
        self.assertIn(('slice', False), calls)
        self.assertIn(('render',), calls)
        with self.assertRaises(ValueError):
            viewer.set_interpolation('smooth')

    def test_points_only_disables_the_interp_radios(self):
        widgets = []
        for index, value in enumerate(('linear', 'cubic', 'tps')):
            widgets.append(Widget(
                id=f'interp.{value}', kind='radio',
                rect=Rect(0, index * 20, 200, index * 20 + 20),
                action=('interp', value), label_key=f'sidebar.interp.{value}',
                group='interp', value=value))
        viewer = TrialViewer.__new__(TrialViewer)
        viewer.points_only = True
        viewer._sync_points_only_widgets(WidgetSet(widgets))
        self.assertTrue(all(widget.disabled for widget in widgets))

    def test_interp_labels_exist_in_both_languages(self):
        for language in i18n.LANGUAGES:
            for key in ('sidebar.title.interp', 'sidebar.interp.linear',
                        'sidebar.interp.cubic', 'sidebar.interp.tps',
                        'sidebar.interp.note'):
                self.assertIn(key, i18n.STRINGS[language], (language, key))

    def test_cli_defaults_to_tps_and_accepts_three_modes(self):
        from app.app import build_parser
        parser = build_parser()
        self.assertEqual(parser.parse_args(['--data', 'x']).interpolation,
                         'tps')
        for mode in ('linear', 'cubic', 'tps'):
            self.assertEqual(parser.parse_args(
                ['--data', 'x', '--interpolation', mode]).interpolation, mode)


if __name__ == '__main__':
    unittest.main()
