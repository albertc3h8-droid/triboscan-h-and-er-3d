"""Depth-population peaks for the grey reference layers (UI v2, P3).

P2 drew the reference layers on an *evenly spaced decimation* of the derived
layer depths (runtime.loader.layer_mode_depths).  P3 changes the rule to
"follow the depth clustering": count how many measured points sit on every
derived layer and, when that distribution has significant peaks, put a grey
surface on each peak.

The production dataset has no such peak: its per-layer population is a flat
3025-point plateau between the two thin boundary ramps (see
tools/ui_v2/p3_depth_probe.py and the table in
TriboScan3D_UI_v2_P3_交接.md section 2).  "No significant peak" therefore
falls back to the P2 decimation -- otherwise this dataset would show no
reference layer at all.

Everything here is pure numpy on purpose: the GUI imports it, but so do the
tests, and no render window may be created outside the main thread.
"""
from __future__ import annotations

import math

import numpy as np

#: A peak must rise this fraction of the tallest column above its deepest
#: saddle, otherwise it is layer-to-layer noise rather than a depth cluster.
DEFAULT_PROMINENCE_FRAC = 0.25
#: Smoothing width (samples) applied before looking for peaks.  The layer
#: spacing is ~2 nm, so three samples already span more than one layer and the
#: 1 nm binning beat ("comb" in the histogram) cannot fake a peak.
DEFAULT_SMOOTH_WINDOW = 3
#: Columns below this fraction of the tallest one are treated as the boundary
#: ramp of the valid depth range, never as a cluster (the first/last layers of
#: the production data hold 1 and 63 points instead of ~3025).
DEFAULT_EDGE_FRACTION = 0.5
#: A flat top wider than this fraction of the analysed range is a saturation
#: plateau (the population is capped by the grid), not a depth cluster.  The
#: production data is saturated over 66 of its 72 usable planes (92 %), while
#: a genuine cluster that saturates occupies a band, not the whole range.
DEFAULT_MAX_PLATEAU_FRACTION = 0.33


def smooth_counts(counts, window: int = DEFAULT_SMOOTH_WINDOW):
    """Centred moving average with replicated edges.

    window is the number of samples averaged (1 = no smoothing).  An even
    window is widened by one so the result stays centred on the layer it
    belongs to.
    """
    values = np.asarray(counts, dtype=np.float64).ravel()
    window = int(window)
    if window <= 1 or values.size < 3:
        return values.copy()
    if window % 2 == 0:
        window += 1
    window = min(window, values.size if values.size % 2 else values.size - 1)
    half = window // 2
    padded = np.concatenate([np.repeat(values[:1], half), values,
                             np.repeat(values[-1:], half)])
    kernel = np.ones(window, dtype=np.float64) / float(window)
    return np.convolve(padded, kernel, mode='valid')


def _core_span(values, edge_fraction: float) -> tuple:
    """First/last index of the valid-depth core (boundary ramps excluded)."""
    if values.size == 0:
        return 0, -1
    tallest = float(values.max())
    if tallest <= 0.0:
        return 0, values.size - 1
    core = values >= float(edge_fraction) * tallest
    if not core.any():
        return 0, values.size - 1
    first = int(np.argmax(core))
    last = int(values.size - 1 - np.argmax(core[::-1]))
    return first, last


def _plateau_peaks(values, first: int, last: int,
                  max_plateau_fraction: float = DEFAULT_MAX_PLATEAU_FRACTION) -> list:
    """Indices of local maxima inside [first, last], flat tops centred.

    A very wide flat top is not a cluster, it is a plateau: every depth plane
    of it holds the same number of valid samples because the population is
    capped by the grid (the production data is saturated at 3025 = 55 x 55 over
    66 of its 72 usable planes).  Such a run is rejected instead of being
    reported as one huge "peak" in the middle of the range.
    """
    peaks = []
    span = max(last - first + 1, 1)
    widest = max(1, int(math.floor(float(max_plateau_fraction) * span)))
    index = first + 1
    while index <= last - 1:
        if values[index] <= values[index - 1]:
            index += 1
            continue
        # Walk right over an equal-valued run (a flat top).
        end = index
        while end + 1 <= last and values[end + 1] == values[index]:
            end += 1
        if (end + 1 <= last and values[end + 1] < values[index]
                and (end - index + 1) <= widest):
            peaks.append((index + end) // 2)
        index = end + 1
    return peaks


def _prominence(values, index: int, first: int, last: int) -> float:
    """Height above the higher of the two saddles that separate this peak."""
    height = float(values[index])
    left_saddle = height
    cursor = index - 1
    while cursor >= first and values[cursor] <= height:
        left_saddle = min(left_saddle, float(values[cursor]))
        cursor -= 1
    right_saddle = height
    cursor = index + 1
    while cursor <= last and values[cursor] <= height:
        right_saddle = min(right_saddle, float(values[cursor]))
        cursor += 1
    return height - max(left_saddle, right_saddle)


def find_peaks(depths_nm, counts, *,
               prominence_frac: float = DEFAULT_PROMINENCE_FRAC,
               smooth_window: int = DEFAULT_SMOOTH_WINDOW,
               edge_fraction: float = DEFAULT_EDGE_FRACTION,
               max_plateau_fraction: float = DEFAULT_MAX_PLATEAU_FRACTION,
               min_separation_nm=None) -> list:
    """Significant depth peaks, shallowest first.

    Each item is {index, depth_nm, height, prominence}.  An empty list means
    "this dataset has no depth clustering" and the caller must fall back to the
    P2 decimation.  The boundary ramp of the valid range never counts as a
    peak: a candidate needs a lower neighbour on *both* sides.
    """
    depths = np.asarray(depths_nm, dtype=np.float64).ravel()
    values = np.asarray(counts, dtype=np.float64).ravel()
    if depths.size == 0 or depths.size != values.size:
        return []
    smoothed = smooth_counts(values, smooth_window)
    first, last = _core_span(smoothed, edge_fraction)
    if last - first < 2:
        return []
    reference = float(smoothed[first:last + 1].max())
    if reference <= 0.0:
        return []
    threshold = float(prominence_frac) * reference
    peaks = []
    for index in _plateau_peaks(smoothed, first, last, max_plateau_fraction):
        # The saddles are searched over the *whole* profile, not just the core:
        # the core boundary is a threshold crossing, not a valley, and using it
        # would under-report the prominence of every peak next to a taller one.
        prominence = _prominence(smoothed, index, 0, smoothed.size - 1)
        if prominence < threshold or prominence <= 0.0:
            continue
        peaks.append({'index': int(index),
                      'depth_nm': float(depths[index]),
                      'height': float(smoothed[index]),
                      'prominence': float(prominence)})
    if min_separation_nm is not None and len(peaks) > 1:
        gap = abs(float(min_separation_nm))
        kept = []
        for peak in sorted(peaks, key=lambda item: -item['prominence']):
            if all(abs(peak['depth_nm'] - other['depth_nm']) >= gap
                   for other in kept):
                kept.append(peak)
        peaks = kept
    peaks.sort(key=lambda item: item['depth_nm'])
    return peaks


def decimated_indices(count: int, limit: int) -> list:
    """P2 fallback: evenly spaced layer indices, never more than limit."""
    count = int(count)
    limit = max(2, int(limit))
    if count <= 0:
        return []
    if count <= limit:
        return list(range(count))
    picks = np.unique(np.linspace(0, count - 1, limit).round().astype(int))
    return [int(index) for index in picks]


def select_reference_depths(profile_depths_nm, profile_counts, *,
                            fallback_depths_nm=(),
                            limit: int = 20,
                            prominence_frac: float = DEFAULT_PROMINENCE_FRAC,
                            smooth_window: int = DEFAULT_SMOOTH_WINDOW,
                            edge_fraction: float = DEFAULT_EDGE_FRACTION):
    """Reference-layer depths: peak positions, or the fallback.

    profile_depths_nm / profile_counts describe the depth profile that is
    analysed (for the GUI: the field Z planes and their population).
    fallback_depths_nm is the P2 rule's answer -- the derived real layer
    depths -- used when the profile has no significant peak.

    Returns (depths, method, detail).  method is "peaks" (depth clustering
    found), "layers" (no peak: the P2 real layer depths) or "decimated" (no
    peak and no fallback supplied: evenly spaced profile samples).
    """
    depths = np.asarray(profile_depths_nm, dtype=np.float64).ravel()
    values = np.asarray(profile_counts, dtype=np.float64).ravel()
    fallback = np.asarray(fallback_depths_nm, dtype=np.float64).ravel()
    limit = max(2, int(limit))
    detail = {
        'peak_prominence_frac': float(prominence_frac),
        'smooth_window': int(smooth_window),
        'edge_fraction': float(edge_fraction),
        'profile_count': int(depths.size),
        'limit': int(limit),
        'peaks': [],
    }
    if depths.size == 0:
        detail['method'] = 'empty'
        return [], 'empty', detail
    spacing = float(np.median(np.diff(depths))) if depths.size > 1 else 0.0
    peaks = find_peaks(depths, values, prominence_frac=prominence_frac,
                       smooth_window=smooth_window,
                       edge_fraction=edge_fraction,
                       min_separation_nm=(1.5 * spacing) if spacing > 0 else None)
    detail['peaks'] = [dict(peak) for peak in peaks]
    if peaks:
        ranked = sorted(peaks, key=lambda item: -item['prominence'])[:limit]
        selected = sorted(float(peak['depth_nm']) for peak in ranked)
        detail['method'] = 'peaks'
        return selected, 'peaks', detail
    if fallback.size:
        picks = decimated_indices(fallback.size, limit)
        detail['method'] = 'layers'
        detail['note'] = ('no significant depth peak: fall back to the real '
                          'layer depths (P2 rule)')
        return [float(fallback[index]) for index in picks], 'layers', detail
    picks = decimated_indices(depths.size, limit)
    detail['method'] = 'decimated'
    detail['note'] = ('no significant depth peak and no layer fallback: '
                      'evenly spaced profile samples')
    return [float(depths[index]) for index in picks], 'decimated', detail
