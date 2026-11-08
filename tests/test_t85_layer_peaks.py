"""t85 regression: reference layers follow depth peaks, with a no-peak fallback.

P3 (W4) replaced "draw every derived layer depth" with a depth-profile rule:
smooth the per-plane population, keep significant peaks (prominence relative
to the profile maximum, boundary ramps excluded, saturated plateaus rejected)
and fall back to the real per-rank layer depths when the profile has no peak.
The production dataset is a saturated 3025-sample plateau, i.e. the fallback
case, and its slice view must stay pixel-identical to the P2 baseline.

Pinned here:
* the peak rule on synthetic bimodal / ramp / saturated-plateau profiles;
* the reference-depth selection (peaks, fallback, limit);
* the cloud view hiding the grey layers and clipping the frame above the
  current depth, while the slice view keeps both.
"""
from __future__ import annotations

import shutil
import unittest
from pathlib import Path

import numpy as np

from runtime.paths import prepare

PATHS = prepare()

from app import load_job  # noqa: E402
from gui.viewer import (REFERENCE_LAYER_LIMIT,  # noqa: E402
                        REFERENCE_PEAK_PROMINENCE, TrialViewer)
from runtime import layer_peaks  # noqa: E402

DATA = (Path(__file__).resolve().parents[2] / '3d hardness plot raw data').resolve()
OUT = (Path(__file__).resolve().parent / 'out' / 't85').resolve()


def bump(depths, centre, sigma, amplitude, base=300.0):
    return base + amplitude * np.exp(-0.5 * ((depths - centre) / sigma) ** 2)


class FakeHandle:
    generation = 1
    cancelled = False

    def emit(self, *args, **kwargs):
        pass

    def check_cancel(self):
        pass


class PeakRuleTests(unittest.TestCase):
    """Pure profile arithmetic -- no VTK, no dataset."""

    def test_01_smoothing_uses_edge_replication(self):
        flat = layer_peaks.smooth_counts([10.0, 10.0, 10.0, 10.0], 3)
        np.testing.assert_allclose(flat, np.full(4, 10.0))
        spike = layer_peaks.smooth_counts([0.0, 10.0, 0.0], 3)
        # A 3-wide box filter on a single-sample spike spreads it evenly.
        np.testing.assert_allclose(spike, np.full(3, 10.0 / 3.0))

    def test_02_synthetic_two_clusters_give_two_peaks(self):
        depths = np.linspace(0.0, 100.0, 60)
        counts = bump(depths, 30.0, 4.0, 900.0) + bump(depths, 72.0, 3.0, 1400.0,
                                                      base=0.0)
        peaks = layer_peaks.find_peaks(depths, counts,
                                       prominence_frac=REFERENCE_PEAK_PROMINENCE)
        self.assertEqual(len(peaks), 2)
        found = sorted(peak['depth_nm'] for peak in peaks)
        self.assertAlmostEqual(found[0], 30.0, delta=1.5)
        self.assertAlmostEqual(found[1], 72.0, delta=1.5)
        self.assertTrue(all(peak['prominence'] > (
            REFERENCE_PEAK_PROMINENCE * counts.max())) for peak in peaks)

    def test_03_boundary_ramp_is_not_a_peak(self):
        # The production field: two thin support-hull ramps around a plateau.
        counts = np.concatenate([[1.0, 63.0, 660.0, 2895.0, 3020.0],
                                 np.full(50, 3025.0),
                                 [3016.0, 2962.0, 2679.0, 805.0, 1.0]])
        depths = np.linspace(53.951, 204.761, counts.size)
        self.assertEqual(layer_peaks.find_peaks(depths, counts), [])

    def test_04_saturated_plateau_is_not_a_peak(self):
        # 3025 = 55 x 55 full grid: the population saturates, which says nothing
        # about clustering.  A flat run this wide must not become one huge peak.
        counts = np.concatenate([[1.0, 63.0, 660.0, 2895.0, 3020.0],
                                 np.full(66, 3025.0), [805.0, 1.0]])
        depths = np.linspace(53.951, 204.761, counts.size)
        self.assertEqual(layer_peaks.find_peaks(depths, counts), [])

    def test_05_narrow_saturated_bands_are_peaks(self):
        counts = np.zeros(70)
        counts[18:27] = 3025.0
        counts[44:50] = 3025.0
        depths = np.linspace(50.0, 205.0, counts.size)
        peaks = layer_peaks.find_peaks(depths, counts)
        self.assertEqual(len(peaks), 2)
        # Band centres: the rule reports the middle of each flat band.
        self.assertAlmostEqual(peaks[0]['depth_nm'], depths[22], delta=depths[1] - depths[0])
        self.assertAlmostEqual(peaks[1]['depth_nm'], depths[46], delta=depths[1] - depths[0])

    def test_06_reference_falls_back_to_real_layers(self):
        counts = np.concatenate([[1.0, 63.0, 660.0, 2895.0, 3020.0],
                                 np.full(66, 3025.0), [805.0, 1.0]])
        depths = np.linspace(53.951, 204.761, counts.size)
        layers = [58.451, 86.494, 107.419, 125.412]
        selected, method, detail = layer_peaks.select_reference_depths(
            depths, counts, fallback_depths_nm=layers)
        self.assertEqual(method, 'layers')
        self.assertEqual(selected, layers)
        self.assertEqual(detail['peaks'], [])

    def test_07_reference_uses_peak_depths(self):
        depths = np.linspace(0.0, 100.0, 60)
        counts = bump(depths, 30.0, 4.0, 900.0) + bump(depths, 72.0, 3.0, 1400.0,
                                                      base=0.0)
        selected, method, detail = layer_peaks.select_reference_depths(
            depths, counts, fallback_depths_nm=[1.0, 2.0, 3.0])
        self.assertEqual(method, 'peaks')
        self.assertEqual(len(selected), 2)
        self.assertAlmostEqual(selected[0], 30.0, delta=1.5)
        self.assertAlmostEqual(selected[1], 72.0, delta=1.5)
        self.assertTrue(detail['peaks'])

    def test_08_peak_count_is_capped_and_sorted(self):
        depths = np.arange(120, dtype=float)
        counts = np.full(depths.size, 10.0)
        for centre in range(5, 120, 4):
            counts[centre] = 100.0
        selected, method, _detail = layer_peaks.select_reference_depths(
            depths, counts, limit=7)
        self.assertEqual(method, 'peaks')
        self.assertEqual(len(selected), 7)
        self.assertEqual(selected, sorted(selected))

    def test_09_limit_covers_the_default_reference_budget(self):
        self.assertGreaterEqual(REFERENCE_LAYER_LIMIT, 2)
        self.assertGreater(REFERENCE_PEAK_PROMINENCE, 0.0)


class ViewerViewTests(unittest.TestCase):
    """Cloud view: no grey layers, frame cut.  Slice view: both kept."""

    @classmethod
    def setUpClass(cls):
        if not DATA.is_dir():
            raise unittest.SkipTest(f'raw data folder missing: {DATA}')
        shutil.rmtree(OUT, ignore_errors=True)
        (OUT / 'cache').mkdir(parents=True, exist_ok=True)
        result = load_job.load_directory(
            FakeHandle(), directory=str(DATA), cache_dir=OUT / 'cache',
            use_cache=False)
        cls.result = result
        cls.viewer = TrialViewer(
            result.dataset, result.field, result.raw_spacing,
            off_screen=True, window_size=(1536, 992),
            timings=result.timings, cache_hit=False)
        cls.viewer.plotter.render()

    @classmethod
    def tearDownClass(cls):
        if cls.viewer is not None:
            cls.viewer.plotter.close()
            cls.viewer = None

    def test_10_reference_selection_is_recorded(self):
        selection = self.viewer.reference_selection
        self.assertIn(selection['method'], ('layers', 'peaks'))
        self.assertTrue(selection['depths_nm'])
        # The profile is the measured-depth histogram (1 nm bins), not the
        # interpolated field's Z planes: 152 bins over 53.951 .. 204.761 nm.
        step = float(self.viewer.depth_profile_bin_nm)
        self.assertAlmostEqual(step, 1.0, places=6)
        depths = selection['profile_depths_nm']
        self.assertAlmostEqual(depths[1] - depths[0], step, places=6)
        self.assertEqual(len(depths), len(selection['counts']))

    def test_11_production_profile_shows_the_ten_levels(self):
        # The measured depths cluster on the ten measurement levels, so the
        # measured-depth histogram has ten significant peaks and the rule must
        # place the reference layers on them (no fallback).
        selection = self.viewer.reference_selection
        self.assertEqual(selection['method'], 'peaks')
        recorded = [float(value) for value in selection['depths_nm']]
        self.assertEqual(len(recorded), 10)
        layers = [float(value) for value in self.viewer.layers.depths_nm]
        self.assertEqual(len(layers), 10)
        for peak, layer in zip(recorded, layers):
            self.assertLess(abs(peak - layer), 1.5,
                            f'peak {peak:.2f} nm vs layer {layer:.2f} nm')

    def test_12_cloud_view_hides_reference_layers(self):
        self.viewer.set_display_mode('cloud', render=False)
        drawn = sum(1 for metric in self.viewer.metrics
                    for actor in self.viewer.reference_actors[metric]
                    if actor.GetVisibility())
        self.assertEqual(drawn, 0)
        for metric in self.viewer.metrics:
            self.assertFalse(self.viewer.envelope_actors[metric].GetVisibility())
            self.assertTrue(self.viewer.cloud_actors[metric].GetVisibility())
            # W1: the interpolated slice face is the bright face in the cloud.
            self.assertTrue(self.viewer.slice_actors[metric].GetVisibility())

    def test_13_cloud_view_clips_the_frame_above_the_depth(self):
        self.viewer.set_display_mode('cloud', render=False)
        self.viewer.plotter.render()
        axes = self.viewer.axes_actors['Er']
        report = axes.report()
        self.assertIsNotNone(report['depth_cut_world'])
        cut = report['depth_cut_world']
        self.assertLess(cut, report['bounds'][5])
        # Depth ticks above the cut are gone, the lower ones stay.
        self.assertLess(report['z_labels_visible'], len(axes.z_ticks))

    def test_14_slice_view_keeps_layers_and_full_frame(self):
        self.viewer.set_display_mode('slice', render=False)
        self.viewer.plotter.render()
        drawn = sum(1 for metric in self.viewer.metrics
                    for actor in self.viewer.reference_actors[metric]
                    if actor.GetVisibility())
        self.assertGreater(drawn, 0)
        for metric in self.viewer.metrics:
            self.assertFalse(self.viewer.cloud_actors[metric].GetVisibility())
            self.assertTrue(self.viewer.slice_actors[metric].GetVisibility())
        self.assertIsNone(self.viewer.axes_actors['Er'].report()['depth_cut_world'])

    def test_15_cloud_fog_is_opt_in(self):
        self.viewer.set_display_mode('cloud', render=False)
        fog = self.viewer.cloud_fog_actors['Er']
        self.assertFalse(fog.GetVisibility())
        self.assertTrue(self.viewer.widgets['display.fog'].checked is False)
        self.viewer.set_cloud_fog(True, render=False)
        self.assertTrue(fog.GetVisibility())
        self.assertFalse(fog.GetPickable())
        self.assertTrue(self.viewer.widgets['display.fog'].checked)
        self.viewer.set_cloud_fog(False, render=False)
        self.assertFalse(fog.GetVisibility())
        self.assertFalse(self.viewer.widgets['display.fog'].checked)
        # The toggle only means something in the cloud view.
        self.assertFalse(self.viewer.widgets['display.fog'].disabled)
        self.viewer.set_display_mode('slice', render=False)
        self.assertTrue(self.viewer.widgets['display.fog'].disabled)
        self.viewer.set_display_mode('cloud', render=False)
        self.viewer._activate_widget(self.viewer.widgets['display.fog'])
        self.assertTrue(self.viewer.cloud_fog_enabled)
        self.assertTrue(fog.GetVisibility())


if __name__ == '__main__':
    unittest.main(verbosity=2)
