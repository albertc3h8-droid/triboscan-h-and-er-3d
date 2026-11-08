"""Product 3D viewer: native PyVista/VTK window, no Qt, no web view.

The viewer is a *consumer*: the field grid and the RAW display-spacing
statistics are computed by the background job (``runtime.precompute``) and
passed in.  It never rebuilds the field, never reads source files and never
writes the source folder.  File actions (open data, export, save/open result)
are wired by ``app.app``; the viewer only reports the user's choice through
``pending_action`` and calls the read-only callbacks it is given.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import math
import time
import unicodedata

import numpy as np
import pyvista as pv
import vtk
from PIL import Image
from vtk.util.numpy_support import numpy_to_vtk

from integration.protocols import (
    APP_TITLE, APP_VERSION, CameraView, GridView, PlaneView, SliceSnapshot)
from gui import (i18n, interaction, scene_axes, theme, ui_chrome, ui_layout,
                 ui_widgets, window_policy)
from runtime import display_spacing, field3d, layer_peaks, loader as dm
from runtime import selection as trial_selection
from runtime.precompute import Z_EXAGGERATION

#: Maximum number of grey reference-layer surfaces drawn at once.  A dataset
#: with more derived layers is decimated to this many evenly spaced surfaces
#: (77 layers on the production data) instead of falling back to the envelope.
REFERENCE_LAYER_LIMIT = 20
#: Cubic/TPS slices are evaluated on this many times the stored X/Y samples
#: before the mesh is built.  A 55 x 55 quad mesh is too coarse at 2300 px:
#: the interpolant is smooth, but the mesh still shows each quad as a block.
SLICE_DISPLAY_SUPERSAMPLE = 4
#: Auto mode keeps the view readable by drawing only this many evenly spaced
#: layers; "Layers" draws every real layer depth the dataset provides.
AUTO_LAYER_TARGET = 6
#: P3: the grey reference layers go where the depth data clusters.  Peaks below
#: this prominence (fraction of the tallest per-layer population) are noise, and
#: a dataset without any peak keeps the evenly spaced decimation instead of
#: showing no reference layer at all -- the production data is a flat plateau.
REFERENCE_PEAK_PROMINENCE = layer_peaks.DEFAULT_PROMINENCE_FRAC
#: Point-cloud LOD: at most this many measured points, fixed seed so the same
#: dataset always draws the same points (no flicker between rebuilds).
CLOUD_POINT_LIMIT = 60000
CLOUD_SEED = 7
CLOUD_POINT_SIZE = 2.0
CLOUD_OPACITY = 0.25
#: P3: optional "haze" of the points *above* the current depth, so the slice
#: face sits inside a volume instead of on top of a solid block.  Drawn over
#: the whole segment up to the data top (the same points the dense cloud drops,
#: just lighter); the axes frame still stops at the slice.  Off by default and
#: toggled by the header checkbox (display.fog).
CLOUD_FOG_SIZE = 1.6
CLOUD_FOG_OPACITY = 0.04
#: P3: width of the depth histogram the reference-layer peak rule reads.  1 nm
#: matches the "measured depth distribution (1 nm bins)" card and separates the
#: ten measurement levels of the production data.
DEPTH_PROFILE_BIN_NM = 1.0
# Measured points sit on a regular 500 nm x 500 nm x ~2 nm grid; sampling it
# keeps the lattice visible as stripes and Moire gaps.  Each cloud point is
# nudged inside its own cell (display only) by this fraction of the spacing.
CLOUD_JITTER_CELL = 0.45
VIEW_MODES = ('normal', 'locate', 'raw')


class _RawMapperView:
    """Compatibility view: glyph mapper over the real source point cloud.

    Tests (and scene reports) must be able to prove that the raw representation
    is built from the measured points and scalars, not from a glyph mesh that
    hides the original values.  ``dataset`` stays the real XYZ point cloud and
    ``scalar_range`` is the fixed full-data clim.
    """

    def __init__(self, dataset, scalar_range):
        self.dataset = dataset
        self.scalar_range = tuple(float(v) for v in scalar_range)


class _RawGlyphActor:
    """Thin wrapper around the batched sphere-glyph actor for raw mode."""

    def __init__(self, actor, glyph_mapper, dataset, scalar_range):
        self.actor = actor
        self.glyph_mapper = glyph_mapper
        self.mapper = _RawMapperView(dataset, scalar_range)

    def __getattr__(self, name):
        return getattr(self.actor, name)


class _CleanPlotterProxy:
    """Duck-typed plotter for io_exports.png_export that crops the capture.

    The PNG writer only calls render()/screenshot(); everything else is
    delegated to the live plotter, so the export can crop to the two plot cards
    without teaching io_exports about the UI layout.  screenshot() also hides
    the focused-card outline for the capture and restores the skin afterwards.
    """

    def __init__(self, viewer: 'TrialViewer'):
        self._viewer = viewer

    def __getattr__(self, name):
        return getattr(self._viewer.plotter, name)

    def screenshot(self, filename=None, **kwargs):
        kwargs.pop('return_img', None)
        image = np.asarray(self._viewer._capture_export_pixels(**kwargs),
                           dtype=np.uint8)
        x0, y0, x1, y1 = self._viewer.clean_export_rect()
        height, width = image.shape[:2]
        crop = image[max(0, min(y0, height)):max(0, min(y1, height)),
                     max(0, min(x0, width)):max(0, min(x1, width))]
        if crop.size == 0:
            raise ValueError('plot export crop is empty')
        if filename is not None:
            Image.fromarray(crop).save(str(filename))
        return crop


class TrialViewer:
    """Two-panel native viewer.  Geometry/scalars are physical nm / GPa."""

    BAND_HEIGHT = .26
    ROW1 = .975
    ROW2 = .925
    SLIDER_Y = .882
    # Major labels of the upper row sit above the track without touching the
    # row-2 button boxes (their bottom edge is at ~.915 at 650 px height).
    TICK_ABOVE_Y = .894
    TICK_BELOW_Y = .858
    MAJOR_ABOVE_END = .892
    MAJOR_BELOW_END = .864
    MINOR_END = .872
    TRACK_X0 = .06
    TRACK_X1 = .94
    STATUS_Y = .745
    NOTICE_Y = .820
    PLOT_TITLE_Y = .940
    PLOT_HELP_Y = .895
    # Locate provenance block: bottom-left of the card, close to the lower
    # frame edge (the old .105 margin plus 16 px text read as a large block
    # in the middle of the plot).
    HOVER_Y = .03
    HOVER_FONT_SIZE = 6

    ROW1_BUTTONS = {'normal': .305, 'locate': .395, 'raw': .485,
                    'reset': .575}
    ROW2_BUTTONS = {'common': .125, 'actual': .215,
                    'auto': .370, 'layers': .455, 'envelope': .575}

    # Locate marker: fallback *screen* radius in pixels, used only when the
    # dataset has no RAW spacing statistic (degenerate cloud).  The normal
    # radius is the RAW sphere radius of the metric, so a located measured point
    # is exactly as big as a measured point in the RAW view (user-approved).
    LOCATE_MARKER_RADIUS_PX = 1.6
    # DEPRECATED (this round): kept only so the previous round's frozen
    # verification script can still import it.  It is NOT used for sizing any
    # more.  The RAW sphere diameter is now 0.5 * the median display-space
    # nearest-neighbour distance between unique real 3D positions
    # (display_spacing.compute_spacing); radius = 0.25 * that median.
    RAW_SPHERE_RADIUS_FRACTION = 0.005
    RAW_SPHERE_RADIUS_RULE = (
        'display-space unique-3d nearest-neighbour median * 0.25; '
        'diameter = median * 0.5')
    # Screen-space locate guide ring (pixels, constant at every zoom/orientation).
    LOCATE_RING_RADIUS_PX = 7.0
    LOCATE_RING_INNER_PX = 5.2
    # Free viewport space kept around the data bounds on startup/resize/reset so
    # the outer cube-axes labels stay inside the viewport.  The design fills the
    # card with the data box, so this is tuned down from the 12 % skeleton value
    # and re-measured against the design (see build/evidence/ui_v2 P2 notes).
    VIEW_FIT_MARGIN = 0.07
    # Cube-axes ink, measured on the design: dark navy labels/titles on
    # light steel frame lines; ticks are inside and minor ticks are off.
    AXIS_INK = '#1b3358'
    AXIS_LINE = '#8296ae'
    AXIS_LABEL_FONT = 10
    AXIS_TITLE_FONT = 11
    # Pillow paints UI text with grayscale antialiasing; at the smallest window
    # sizes FreeType's hinting snaps stems to whole pixels and the text reads
    # as pixelated.  Painting the static base at 2x and averaging it back down
    # fixes that; the cost only matters on resize, so it is limited to windows
    # at or below 1536 x 992.
    SKIN_SUPERSAMPLE = 2
    SKIN_SUPERSAMPLE_MAX_SCALE = 1.55

    BUTTON_STYLES = {
        'idle': {'fill': '#f8fafc', 'border': '#cbd5e1', 'text': '#475569'},
        'hover': {'fill': '#eff6ff', 'border': '#60a5fa', 'text': '#1d4ed8'},
        'selected': {'fill': '#dbeafe', 'border': '#2563eb', 'text': '#1e3a8a'},
    }

    def __init__(self, dataset: dm.Dataset, field: field3d.FieldGrid,
                 raw_spacing: dict, *, off_screen: bool = False,
                 window_size: tuple[int, int] = (1400, 800),
                 timings=None, cache_hit: bool = False,
                 source_note: str = '', language: str | None = None,
                 settings_path=None, ui_scale: float = 1.0,
                 points_only: bool = False) -> None:
        if field is None:
            raise ValueError('the viewer requires the precomputed field; '
                             'it never rebuilds the 3D field')
        if raw_spacing is None:
            raise ValueError('the viewer requires precomputed RAW display-'
                             'spacing statistics')
        self.dataset = dataset
        self.field = field
        # P4 (W5): a raw-points CSV has no interpolated field.  The viewer then
        # shows the measured points only and refuses the field-based views with
        # an explanation instead of rendering an empty slice.
        self.points_only = bool(points_only)
        self.off_screen = bool(off_screen)
        #: Win32 minimum-size hook installed on the first ConfigureEvent.
        self._window_policy = None
        # Interactive windows never open below the 1280 x 720 compact floor;
        # off-screen renders keep the requested size for reproducible evidence.
        window_size = theme.clamp_window_size(
            window_size,
            scale=1.0 if self.off_screen else theme.system_ui_scale(),
            minimum=not self.off_screen,
            maximum=not self.off_screen)
        self.timings = timings
        self.cache_hit = bool(cache_hit)
        self.source_note = str(source_note or '')
        self.language_settings = i18n.LanguageSettings(settings_path)
        requested_language = (i18n.normalize_language(language)
                              or self.language_settings.load())
        self.font_file = i18n.find_cjk_font()
        self.language = requested_language
        self._font_note = ''
        if self.language == 'zh-CN' and not self.font_file:
            self.language = 'en'
            self._font_note = i18n.tr('en', 'notice.font_fallback')
        self.pending_action = None
        self._export_callback = None
        self._export_dir_callback = None
        self._save_callback = None
        self._open_callback = None
        self._load_callback = None
        self._save_available = False
        self._notice = self._font_note
        self._closed = False
        self._save_done = False
        self.layers = dm.layer_mode_depths(dataset)
        # P3: the measured-depth histogram drives the reference-layer placement
        # (depth peaks); reference_selection records how the grey layers were
        # picked so reports and evidence shots can prove the method.
        self._depth_profile = None
        self.depth_profile_bin_nm = float(DEPTH_PROFILE_BIN_NM)
        self.reference_selection = {'method': 'unbuilt', 'peaks': [],
                                    'depths_nm': []}
        self.range_modes = {mode: dm.depth_range(dataset, mode)
                            for mode in ('common', 'actual')}
        self.range_mode = 'common'
        self.current_range = self.range_modes[self.range_mode]
        self.current_depth = self._initial_depth()
        self.view_mode = 'raw' if self.points_only else 'normal'
        self.reference_mode = 'auto'
        self.resolved_reference_mode = self._resolve_reference_mode()
        # UI v2 (P2): the point-cloud view is the default display mode; the
        # slice mode keeps the current-depth view only.
        self.display_mode = 'cloud'
        # P5: display-only slice interpolation; the stored field stays raw.
        self.interpolation = 'tps'
        self.export_dir = ''
        self.open_after_export = True
        self.export_kind = 'image'
        self._slice_drag = False
        self._pressed_widget = None
        self._skin_ready = False
        self._skin_size = None
        #: Layer-2 help overlay; the renderer is created in _setup_skin and
        #: only drawn while help_open is true (SetDraw(False) otherwise).
        self.help_open = False
        self._help_renderer = None
        self._help_image = None
        self._help_actor = None
        self._help_size = None
        self._thumb_cache = (None, None)
        self._lut_cache = {}
        self.ui_scale = max(0.5, min(float(ui_scale or 1.0), 3.0))
        self.fixed_ranges = dataset.scalar_ranges
        self._group_x = np.asarray([g.x_nm for g in dataset.groups], dtype=np.float64)
        self._group_y = np.asarray([g.y_nm for g in dataset.groups], dtype=np.float64)
        self.current_planes: dict[str, field3d.Plane] = {}
        self._plane_meshes: dict[str, pv.PolyData | None] = {'Er': None, 'H': None}
        self._plane_mask = None
        self._hover_text = ''
        self._hover_metric = None
        self._last_selection = None
        self._hover_button = None
        self._interactions_installed = False
        self._camera_customized = False
        self._initial_state = None
        self._tick_width = None
        self._tick_font_size = 7
        self._ready = False

        # Display-only Z exaggeration keeps the true XY aspect ratio while the
        # depth axis stays visible.  All labels/values remain physical.
        x0, x1, y0, y1, z0, z1 = self.field.bounds_nm
        self.physical_bounds = (x0, x1, y0, y1, z0, z1)
        span_x, span_y, span_z = x1 - x0, y1 - y0, z1 - z0
        self.z_scale = (Z_EXAGGERATION * min(span_x, span_y) / span_z
                        if span_z > 0 else 1.0)
        # P2 display frame (shared by slice, cloud, grey layers, envelope and
        # the axes): XY relative to the grid's minimum corner, Z mirrored so
        # depth 0 is at the TOP and grows downwards, exactly like the design.
        # The physical data, exports, reports and labels stay absolute.
        self.disp_origin = (float(x0), float(y0), float(z1))
        self.display_bounds = (0.0, float(span_x), 0.0, float(span_y),
                               0.0, float(span_z) * self.z_scale)
        self.xy_span_nm = (float(span_x), float(span_y))
        self.display_span_nm = (float(span_x), float(span_y), float(span_z))
        self.dataset_label = self._dataset_label(dataset)
        self.window_title = f'{APP_TITLE} | {self.dataset_label}'
        # Fixed full-data colour maps, used by the slice and by the locate
        # marker so a marker keeps the original measured value's colour.
        self.color_tables = {}
        for metric, cmap in (('Er', 'viridis'), ('H', 'plasma')):
            lut = pv.LookupTable(cmap=cmap)
            lut.scalar_range = tuple(float(v) for v in self.fixed_ranges[metric])
            self.color_tables[metric] = lut

        self.metrics = ('Er', 'H')
        self.plotter = pv.Plotter(
            shape=(2, 2), groups=[([0], [0, 1])],
            row_weights=(self.BAND_HEIGHT, 1 - self.BAND_HEIGHT),
            off_screen=off_screen, window_size=window_size, border=False)
        self.plotter.title = self.window_title
        self.plotter.set_background('white', all_renderers=True)
        self.plotter.render_window.SetMultiSamples(0)
        self.plotter.render_window.SetAlphaBitPlanes(1)
        self.depth_peeling_requested = {'number_of_peels': 20,
                                        'occlusion_ratio': 0.0, 'multisamples': 0}
        self.control_renderer = self.plotter.renderers[0]
        self.plot_renderers = {metric: self.plotter.renderers[i + 1]
                              for i, metric in enumerate(self.metrics)}
        self.reference_actors: dict[str, list] = {'Er': [], 'H': []}
        self.envelope_actors: dict[str, object] = {}
        self.envelope_edge_actors: dict[str, object] = {}
        self.slice_actors: dict[str, object] = {}
        # P2: measured point cloud (vertex cells) + the cube axes per plot.
        self.cloud_actors: dict[str, object] = {}
        self.cloud_clip_planes: dict[str, object] = {}
        # P3: optional light fog above the current depth (legacy B pilot).
        self.cloud_fog_actors: dict[str, object] = {}
        self.cloud_fog_planes: dict[str, object] = {}
        self.cloud_fog_enabled = False
        self.axes_actors: dict[str, object] = {}
        self._cloud_sources: dict[str, dict] = {}
        self.cloud_points_shown: dict[str, int] = {'Er': 0, 'H': 0}
        self.cloud_lod_counts: dict[str, int] = {'Er': 0, 'H': 0}
        self.point_actors: dict[str, object] = {}
        self.raw_sphere_radius_nm: dict[str, float] = {}
        self.raw_spacing = raw_spacing
        self.raw_spacing_computations = 0  # statistics come precomputed from the job
        self.raw_sphere_resolution = {'theta': 8, 'phi': 6}
        self.hover_actors: dict[str, object] = {}
        self.locate_markers: dict[str, object] = {}
        self.locate_halos: dict[str, object] = {}
        self.locate_marker_radius_nm = None
        self.locate_rings: dict[str, list] = {}
        self.locate_ring_state: dict[str, dict] = {}
        self.locate_occluded = None
        self._ring_refresh_guard = False
        self.tick_label_actors: dict[str, object] = {}
        self.tick_line_actors: list = []
        self.tick_label_rows: dict[int, str] = {}
        self._major_indices: list[int] = []
        self.buttons: dict[str, dict] = {}
        # Which metric the depth-card thumbnail shows; clicking a plot card
        # links it to that card (Er by default).
        self._thumb_metric = 'Er'

        self._build_control_band()
        self._build_plots()
        # Keep Er/H at renderer indices 0/1 for the plots, control renderer last.
        plot_e = self.plot_renderers['Er']
        plot_h = self.plot_renderers['H']
        control = self.control_renderer
        renderers = self.plotter.renderers
        renderers._renderers = [plot_e, plot_h, control]
        renderers._active_index = 0
        self._layout_buttons()
        self._setup_skin(window_size)
        self._apply_visibility()
        self._update_slice(render=False)
        self._refresh_status(render=False)
        # vtkRenderWindow never emits ConfigureEvent; the interactor does, for
        # OS-driven resizes and for programmatic SetSize alike.  Watching the
        # interactor is what makes the Pillow skin follow a maximised/zoomed
        # window instead of being bitmap-stretched by VTK.
        interactor = getattr(getattr(self.plotter, 'iren', None),
                            'interactor', None)
        self._skin_refine_at = 0.0
        if interactor is not None:
            interactor.AddObserver('ConfigureEvent', self._on_configure)
            # One-shot timers armed by _arm_skin_refine land here.
            interactor.AddObserver('TimerEvent', self._on_skin_timer)
        # The shell floor needs the native handle, which only exists once the
        # window has rendered at least once; ConfigureEvent may arrive earlier.
        self._render_observer_id = self.plotter.render_window.AddObserver(
            'RenderEvent', self._on_first_render)
        self._capture_initial_state()
        self._ready = True

    def _dataset_label(self, dataset: dm.Dataset) -> str:
        path = Path(dataset.source_dir)
        # Sample launchers point at <case>/input; show the case name, not the
        # uninformative 'input' directory.
        label = path.parent.name if path.name.lower() == 'input' else path.name
        kind = ('short_synthetic' if dataset.kind == 'synthetic'
                else 'short_transformed' if dataset.kind == 'transformed'
                else 'short_measured')
        return f'{label} [{self._t(f"kind.{kind}")}]'

    # ------------------------------------------------------------------ setup
    def _initial_depth(self) -> float | None:
        """Start in the middle of the current depth range.

        The value is derived from the data (50 % of the common range) rather
        than pinned to a number, so the first slice and the default cloud sit
        mid-volume like the design reference.
        """
        current = self.current_range
        if not current.valid:
            return None
        lo = float(current.lo_nm)
        hi = float(current.hi_nm)
        return lo + 0.5 * (hi - lo)

    def _resolve_reference_mode(self) -> str:
        # P2: the grey layers are decimated to REFERENCE_LAYER_LIMIT surfaces,
        # so "auto" can always use them; a 77-layer dataset no longer collapses
        # to the envelope, and Auto / Layers / Envelope stay visually distinct.
        return 'layers' if self.reference_mode == 'auto' else self.reference_mode

    def _depth_histogram(self):
        """Measured-depth histogram: the profile the reference peak rule reads.

        The reference layers follow the depth *clustering* of the measurements,
        so the analysed signal is the distribution of the measured depths
        (dataset.z_nm), binned at DEPTH_PROFILE_BIN_NM -- the same statistic the
        sidebar histogram card shows.  The interpolated field's support mask is
        NOT usable here: on the production data it is a saturated plateau (see
        tools/ui_v2/p3_depth_probe.py), while the measured depths keep
        the ten measurement levels as ten separate clusters.
        """
        cached = getattr(self, '_depth_profile', None)
        if cached is not None:
            return cached
        depths = np.asarray(self.dataset.z_nm, dtype=np.float64)
        depths = depths[np.isfinite(depths)]
        if depths.size == 0:
            profile = (np.empty(0, dtype=np.float64), np.empty(0, dtype=np.int64))
        else:
            step = float(DEPTH_PROFILE_BIN_NM)
            lo = float(np.floor(depths.min()))
            hi = float(np.ceil(depths.max()))
            edges = np.arange(lo, hi + step, step)
            counts, edges = np.histogram(depths, bins=edges)
            profile = (edges[:-1] + step / 2.0, counts.astype(np.int64))
        self._depth_profile = profile
        return profile

    def _reference_depths(self) -> list[float]:
        """Grey layer surfaces built at startup: depth peaks, else real layers.

        P3 rule: the per-depth-plane population decides.  Significant peaks put
        a surface on every clustering depth (at most REFERENCE_LAYER_LIMIT); a
        dataset without a significant peak falls back to the real per-rank
        layer depths of P2, never to "no reference layer at all".
        """
        profile_depths, counts = self._depth_histogram()
        layer_depths = np.asarray(self.layers.depths_nm, dtype=np.float64)
        limit = max(2, int(REFERENCE_LAYER_LIMIT))
        depths, method, detail = layer_peaks.select_reference_depths(
            profile_depths, counts, fallback_depths_nm=layer_depths,
            limit=limit, prominence_frac=REFERENCE_PEAK_PROMINENCE)
        detail['method'] = method
        detail['depths_nm'] = [float(value) for value in depths]
        detail['layer_depths_nm'] = [float(value) for value in layer_depths]
        detail['profile_depths_nm'] = [float(value) for value in profile_depths]
        detail['profile_bin_nm'] = float(DEPTH_PROFILE_BIN_NM)
        detail['counts'] = [int(value) for value in counts]
        self.reference_selection = detail
        return [float(value) for value in depths]

    @staticmethod
    def _auto_layer_subset(count: int) -> set:
        """Indices Auto mode shows: fewer, evenly spaced real layers.

        All surfaces are built once; Auto only reveals a coarse subset, so the
        three reference pills stay visibly different without rebuilding meshes
        when the user switches.
        """
        if count <= 0:
            return set()
        target = max(2, min(int(AUTO_LAYER_TARGET), count))
        if target >= count:
            return set(range(count))
        picks = np.unique(np.linspace(0, count - 1, target)
                          .round().astype(int))
        return {int(index) for index in picks}

    # ------------------------------------------------------- display frame
    def _disp_xy(self, x_nm, y_nm) -> tuple[float, float]:
        """Absolute physical XY (nm) -> relative display XY."""
        return (float(x_nm) - self.disp_origin[0],
                float(y_nm) - self.disp_origin[1])

    def _mirror_z(self, z_nm) -> float:
        """Physical depth -> display depth in nm (before the actor Z scale)."""
        return float(self.disp_origin[2]) - float(z_nm)

    def _disp_point(self, x_nm, y_nm, z_nm) -> tuple[float, float, float]:
        """Absolute physical point -> world coordinates of the display scene."""
        dx, dy = self._disp_xy(x_nm, y_nm)
        return dx, dy, self._mirror_z(z_nm) * self.z_scale

    def _phys_xy(self, x_disp, y_disp) -> tuple[float, float]:
        """Display world XY -> absolute physical XY in nm."""
        return (float(x_disp) + self.disp_origin[0],
                float(y_disp) + self.disp_origin[1])

    def _band_y(self, display_y: float) -> float:
        viewport = self.control_renderer.GetViewport()
        return (display_y - viewport[1]) / (viewport[3] - viewport[1])

    def _window_size(self) -> tuple[int, int]:
        try:
            size = self.plotter.render_window.GetSize()
            if size and size[0] > 0 and size[1] > 0:
                return int(size[0]), int(size[1])
        except Exception:
            pass
        width, height = self.plotter.window_size
        return int(width), int(height)

    def _window_width(self) -> int:
        return self._window_size()[0]

    @staticmethod
    def _band_text(text):
        """VTK's text renderer treats ``|`` as a tab stop (one line becomes
        several distant columns), so the product never feeds it a pipe."""
        return str(text).replace(' | ', '   ').replace('|', ' ')

    def _t(self, key: str, **kwargs) -> str:
        return i18n.tr(self.language, key, **kwargs)

    def _font_kwargs(self) -> dict:
        return {'font_file': self.font_file} if self.font_file else {}

    def _apply_text_font(self, prop) -> None:
        """Make a VTK text property use the configured CJK font, if possible."""
        if not self.font_file or prop is None:
            return
        try:
            if hasattr(vtk, 'VTK_FONT_FILE'):
                prop.SetFontFamily(vtk.VTK_FONT_FILE)
            if hasattr(prop, 'SetFontFile'):
                prop.SetFontFile(self.font_file)
        except Exception:
            pass

    def _apply_axes_font(self, axes_actor) -> None:
        if axes_actor is None:
            return
        for name in ('XAxisCaptionActor2D', 'YAxisCaptionActor2D',
                     'ZAxisCaptionActor2D'):
            try:
                actor = getattr(axes_actor, f'Get{name}')()
                self._apply_text_font(actor.GetTextProperty())
            except Exception:
                pass
        for axis in range(3):
            try:
                self._apply_text_font(axes_actor.GetLabelTextProperty(axis))
            except Exception:
                pass

    def _apply_scalar_bar_font(self, actor) -> None:
        try:
            bar = actor.GetScalarBarActor()
            self._apply_text_font(bar.GetTitleTextProperty())
            self._apply_text_font(bar.GetLabelTextProperty())
        except Exception:
            pass

    def _button_label(self, name: str) -> str:
        if name == 'lang':
            return self._t('btn.language')
        return self._t(f'btn.{name}')

    def _mode_label(self, mode: str) -> str:
        return self._button_label(mode)

    def _add_band_text(self, name, text, x, display_y, font_size, color):
        return self.plotter.add_text(
            self._band_text(text), position=(x, self._band_y(display_y)),
            viewport=True, font_size=font_size, color=color, name=name,
            render=False, **self._font_kwargs())

    def _add_button(self, name, text, x, display_y, action, font_size=8):
        fill = self._make_box_actor(f'btn_bg_{name}', filled=True)
        border = self._make_box_actor(f'btn_border_{name}', filled=False)
        actor = self._add_band_text(f'btn_{name}', text, x, display_y, font_size,
                                    self.BUTTON_STYLES['idle']['text'])
        self.buttons[name] = {'actor': actor, 'x': x, 'y': display_y,
                              'font': font_size, 'action': action, 'text': text,
                              'fill': fill, 'border': border, 'box': None}

    def _make_box_actor(self, name, filled: bool):
        """2-D box in the control band (no Qt, no HTML, no new dependency)."""
        mapper = vtk.vtkPolyDataMapper2D()
        coordinate = vtk.vtkCoordinate()
        coordinate.SetCoordinateSystemToNormalizedViewport()
        mapper.SetTransformCoordinate(coordinate)
        mapper.SetInputData(vtk.vtkPolyData())
        actor = vtk.vtkActor2D()
        actor.SetMapper(mapper)
        actor.PickableOff()
        if filled:
            actor.GetProperty().SetOpacity(1.0)
        else:
            actor.GetProperty().SetLineWidth(1.2)
        self.control_renderer.AddActor(actor)
        return actor

    @staticmethod
    def _box_polydata(x0, x1, y0, y1, filled: bool):
        points = vtk.vtkPoints()
        cells = vtk.vtkCellArray()
        if filled:
            corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            for x, y in corners:
                points.InsertNextPoint(float(x), float(y), 0.0)
            cells.InsertNextCell(4)
            for index in range(4):
                cells.InsertCellPoint(index)
        else:
            corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
            for x, y in corners:
                points.InsertNextPoint(float(x), float(y), 0.0)
            cells.InsertNextCell(5)
            for index in range(5):
                cells.InsertCellPoint(index)
        poly = vtk.vtkPolyData()
        poly.SetPoints(points)
        if filled:
            poly.SetPolys(cells)
        else:
            poly.SetLines(cells)
        return poly

    def _button_state(self, name: str) -> str:
        if name == 'lang':
            return 'idle'
        if self._hover_button == name:
            return 'hover'
        kind, value = self.buttons[name]['action']
        if kind in ('file', 'language'):
            return 'idle'
        active = {'view': self.view_mode, 'range': self.range_mode,
                  'reference': self.reference_mode, 'reset': None}[kind]
        return 'selected' if value == active else 'idle'

    def _button_items(self):
        items = list(self.buttons.items())
        button = getattr(self, 'language_button', None)
        if button is not None:
            items.append(('lang', button))
        return items

    def _layout_buttons(self):
        """Recompute button boxes from the real window width (all sizes).

        Text actors are anchored bottom-left; boxes and hit tests are derived
        from the same pixel rectangle so a click matches what is drawn.
        """
        width, height = self._window_size()
        if width <= 0 or height <= 0:
            return
        for name, info in self._button_items():
            # +14 px (not +10) keeps the slightly wider bold/selected glyphs
            # inside their box so a selected button never touches its neighbour.
            w_px = self._text_width_px(info['text'], info['font']) + 14.0
            h_px = max(16.0, info['font'] * 2.0)
            w_frac = w_px / width
            h_frac = h_px / height
            x0 = min(max(info['x'] - w_frac / 2.0, 0.004), 1.0 - 0.004 - w_frac)
            x1 = x0 + w_frac
            y0 = info['y'] - h_frac / 2.0
            y1 = info['y'] + h_frac / 2.0
            info['box'] = (x0, x1, y0, y1)
            vp_y0 = self._band_y(y0)
            vp_y1 = self._band_y(y1)
            fill = self._box_polydata(x0, x1, vp_y0, vp_y1, True)
            border = self._box_polydata(x0, x1, vp_y0, vp_y1, False)
            info['fill'].GetMapper().SetInputData(fill)
            info['border'].GetMapper().SetInputData(border)
            # Bottom-left anchored text, vertically centred in the box.
            info['actor'].SetPosition(
                x0 + 5.0 / width,
                self._band_y(info['y'] - info['font'] * 0.93 / height))
        self._refresh_buttons()

    def _refresh_buttons(self):
        for name, info in self._button_items():
            if name == 'save':
                visible = bool(self._save_available)
                info['actor'].SetVisibility(visible)
                for actor in (info['fill'], info['border']):
                    actor.SetVisibility(visible)
            state = self._button_state(name)
            style = self.BUTTON_STYLES[state]
            info['fill'].GetProperty().SetColor(*pv.Color(style['fill']).float_rgb)
            info['border'].GetProperty().SetColor(*pv.Color(style['border']).float_rgb)
            info['actor'].GetTextProperty().SetColor(
                *pv.Color(style['text']).float_rgb)
            info['actor'].GetTextProperty().SetBold(state == 'selected')
        if hasattr(self, 'status_actor'):
            self._refresh_status(render=False)

    def _build_control_band(self):
        p = self.plotter
        p.subplot(0, 0)
        self.depth_value_actor = self._add_band_text(
            'depth_value',
            self._t('depth.heff', depth=self.current_depth)
            if self.current_depth is not None else self._t('depth.none'),
            .012, self.ROW1, self._value_font_size(), '#1f2937')
        for mode in VIEW_MODES:
            self._add_button(mode, self._button_label(mode),
                             self.ROW1_BUTTONS[mode], self.ROW1,
                             ('view', mode), font_size=8)
        self._add_button('reset', self._button_label('reset'),
                         self.ROW1_BUTTONS['reset'], self.ROW1,
                         ('reset', 'view'), font_size=8)
        for name, x in (('open', .650), ('export', .735),
                        ('save', .820), ('load', .905)):
            self._add_button(name, self._button_label(name), x, self.ROW1,
                             ('file', name), font_size=8)
        self.range_title_actor = self._add_band_text(
            'row2_range', self._t('label.range'), .012, self.ROW2, 7,
            '#94a3b8')
        for mode in ('common', 'actual'):
            self._add_button(mode, self._button_label(mode),
                             self.ROW2_BUTTONS[mode], self.ROW2,
                             ('range', mode), font_size=8)
        self.grey_title_actor = self._add_band_text(
            'row2_grey', self._t('label.grey'), .285, self.ROW2, 7,
            '#94a3b8')
        for mode in ('auto', 'layers', 'envelope'):
            self._add_button(mode, self._button_label(mode),
                             self.ROW2_BUTTONS[mode], self.ROW2,
                             ('reference', mode), font_size=8)
        self._add_button('lang', self._button_label('lang'), .935, self.ROW2,
                         ('language', 'toggle'), font_size=7)
        self.language_button = self.buttons.pop('lang')
        self.summary_actor = self._add_band_text('range_summary', '', .632, self.ROW2, 7, '#64748b')
        self.status_actor = self._add_band_text('status_line', '', .012, self.STATUS_Y, 8, '#334155')
        # Notices (export/save/language) get their own visible row above the
        # status lines.  A third status line was below the real client area on
        # short windows; this actor is hidden when empty, so the no-notice
        # layout (and the t14 row gaps) is unchanged.
        self.notice_actor = self._add_band_text(
            'notice_line', '', .012, self.NOTICE_Y, 8, '#b45309')
        self.notice_actor.SetVisibility(False)
        self._build_slider()

    def _refresh_depth_value(self):
        """Keep the main readout in sync with the actual current slice.

        RAW mode shows every measured point; the slider position is not a
        displayed slice there, so a single ``heff = ... nm`` value would be
        false information and is replaced by an explicit RAW label.
        """
        actor = getattr(self, 'depth_value_actor', None)
        if actor is None:
            return
        if self.view_mode == 'raw':
            text = self._t('depth.raw')
        else:
            text = (self._t('depth.heff', depth=self.current_depth)
                    if self.current_depth is not None
                    else self._t('depth.none'))
        actor.SetInput(self._band_text(text))

    def _refresh_help_text(self):
        """Plot-panel subtitle must describe what is actually drawn."""
        actor = getattr(self, 'help_actor', None)
        if actor is None:
            return
        if self.view_mode == 'raw':
            text = self._t('help.raw')
        else:
            text = self._t('help.slice')
        actor.SetInput(text)

    def _value_font_size(self) -> int:
        width = self._window_width()
        # PyVista doubles the requested text font size internally.
        return 8 if width >= 1100 else 7

    def _build_slider(self):
        p = self.plotter
        self.plotter.renderers._active_index = self._control_index()
        if not self.current_range.valid:
            self.slider = None
            return
        if self.current_depth is None:
            # A valid range can be entered from an invalid one; the slider must
            # always receive a real number, never float(None).
            self.current_depth = self._initial_depth()
        self.slider = p.add_slider_widget(
            self._on_slide,
            rng=(float(self.current_range.lo_nm), float(self.current_range.hi_nm)),
            value=float(self.current_depth),
            pointa=(self.TRACK_X0, self._band_y(self.SLIDER_Y)),
            pointb=(self.TRACK_X1, self._band_y(self.SLIDER_Y)),
            interaction_event='always', fmt='%.3f', color='#334155',
            title_height=.001)
        rep = self.slider.GetRepresentation()
        rep.GetTubeProperty().SetColor(.58, .64, .71)
        rep.GetSliderProperty().SetColor(.145, .388, .921)
        rep.GetCapProperty().SetColor(.58, .64, .71)
        rep.SetSliderWidth(.12)
        rep.SetSliderLength(.01)
        rep.SetTubeWidth(.02)
        rep.SetEndCapLength(0)
        rep.SetEndCapWidth(0)
        rep.GetTitleProperty().SetOpacity(0.0)
        rep.GetLabelProperty().SetOpacity(0.0)
        self._apply_text_font(rep.GetTitleProperty())
        self._apply_text_font(rep.GetLabelProperty())
        self.slider.AddObserver(
            'EndInteractionEvent',
            lambda widget, event: self.set_depth(
                float(widget.GetRepresentation().GetValue()), render=True))
        self._bind_slider_widget()
        self._refresh_ticks(render=False)
        # UI v2 draws its own track; the band widget stays for reports only.
        self.slider.SetEnabled(False)

    def _build_plots(self):
        p = self.plotter
        for column, metric in enumerate(self.metrics):
            p.subplot(1, column)
            renderer = p.renderer
            renderer.SetUseDepthPeeling(True)
            renderer.SetMaximumNumberOfPeels(20)
            renderer.SetOcclusionRatio(0.0)
            # Grey reference layers: one surface per drawn (decimated) layer.
            for index, depth in enumerate(self._reference_depths()):
                grid = self._xy_grid(depth)
                actor = p.add_mesh(
                    grid, color='#9aa0a6', opacity=.12, show_scalar_bar=False,
                    lighting=False, pickable=False,
                    name=f'{metric}_reference_{index}', render=False)
                actor.SetScale(1, 1, self.z_scale)
                self.reference_actors[metric].append(actor)
            # Envelope uses the ACTUAL data extent in the display frame.
            box_bounds = (0.0, self.xy_span_nm[0], 0.0, self.xy_span_nm[1],
                          0.0, self.display_span_nm[2])
            box = pv.Box(bounds=box_bounds)
            envelope = p.add_mesh(
                box, color='#9aa0a6', opacity=.12, show_scalar_bar=False,
                lighting=False, pickable=False, name=f'{metric}_envelope',
                render=False)
            envelope.SetScale(1, 1, self.z_scale)
            edges = pv.Box(bounds=box_bounds).extract_all_edges()
            envelope_edges = p.add_mesh(
                edges, color='#94a3b8', opacity=.55, lighting=False,
                pickable=False, name=f'{metric}_envelope_edges', render=False)
            envelope_edges.SetScale(1, 1, self.z_scale)
            self.envelope_actors[metric] = envelope
            self.envelope_edge_actors[metric] = envelope_edges

            # The interpolated slice actor is created once; only its mapper input
            # changes on depth moves (no field rebuild).
            mesh = self._empty_plane_mesh()
            actor = p.add_mesh(
                mesh, scalars='value',
                cmap='viridis' if metric == 'Er' else 'plasma',
                clim=self.fixed_ranges[metric], lighting=False,
                smooth_shading=False, name=f'{metric}_slice', pickable=False,
                show_scalar_bar=False, render=False)
            actor.SetScale(1, 1, self.z_scale)
            self.slice_actors[metric] = actor

            # P2: the measured point cloud (vertex cells, 60 k LOD subset).
            self._build_cloud(metric)

            # Raw mode: every real measured point, original Er/H values, no
            # interpolation and no surface.  One batched glyph mapper turns the
            # real point cloud into low-resolution spheres.  The glyph source is
            # pre-divided by the display Z scale so the actor's (1, 1, z_scale)
            # keeps spheres round instead of stretching them into columns.
            origin_x, origin_y, origin_z = self.disp_origin
            points = pv.PolyData(np.column_stack([
                self.dataset.x_nm - origin_x,
                self.dataset.y_nm - origin_y,
                origin_z - self.dataset.z_nm]))
            points.point_data['Er'] = self.dataset.er_gpa
            points.point_data['H'] = self.dataset.h_gpa
            # One spacing computation per loaded dataset, cached for the whole
            # session: the sphere diameter is 0.5 * the median display-space
            # nearest-neighbour distance of the unique real 3D positions (the
            # old fixed fraction of the XY span is deliberately not used).
            radius = self.raw_spacing['radius_nm']
            radius = 0.0 if radius is None else float(radius)
            self.raw_sphere_radius_nm[metric] = radius
            sphere = vtk.vtkSphereSource()
            sphere.SetRadius(radius)
            sphere.SetThetaResolution(8)
            sphere.SetPhiResolution(6)
            sphere.SetStartPhi(0.0)
            sphere.SetEndPhi(180.0)
            z_shrink = vtk.vtkTransform()
            z_shrink.Scale(1.0, 1.0, 1.0 / self.z_scale)
            sphere_round = vtk.vtkTransformFilter()
            sphere_round.SetInputConnection(sphere.GetOutputPort())
            sphere_round.SetTransform(z_shrink)
            glyph_mapper = vtk.vtkGlyph3DMapper()
            glyph_mapper.SetInputData(points)
            glyph_mapper.SetSourceConnection(sphere_round.GetOutputPort())
            glyph_mapper.SetScaleModeToNoDataScaling()
            glyph_mapper.SetScalarVisibility(True)
            glyph_mapper.SetScalarModeToUsePointFieldData()
            glyph_mapper.SetColorModeToMapScalars()
            glyph_mapper.SelectColorArray(metric)
            glyph_mapper.SetLookupTable(self.color_tables[metric])
            glyph_mapper.SetScalarRange(*self.fixed_ranges[metric])
            point_actor = pv.Actor(mapper=glyph_mapper)
            point_actor.SetScale(1, 1, self.z_scale)
            point_actor.GetProperty().SetLighting(False)
            point_actor.GetProperty().SetInterpolationToFlat()
            point_actor.PickableOff()
            self.plot_renderers[metric].AddActor(point_actor)
            self.point_actors[metric] = _RawGlyphActor(
                point_actor, glyph_mapper, points,
                tuple(self.fixed_ranges[metric]))

            # Locate marker: one reusable sphere actor per view, placed at the
            # real measured XYZ (not the interpolation slice) and coloured by
            # the real Er/H value with the same fixed clim.
            marker = p.add_mesh(
                pv.Sphere(radius=1.0, theta_resolution=12, phi_resolution=12),
                color='#dc2626', show_scalar_bar=False, lighting=False,
                pickable=False, name=f'{metric}_locate_marker', render=False)
            marker.SetVisibility(False)
            marker.GetProperty().SetOpacity(.95)
            self.locate_markers[metric] = marker
            # Dark halo drawn behind the value-coloured marker so it stays
            # distinguishable from a slice of the same colour.
            halo = p.add_mesh(
                pv.Sphere(radius=1.0, theta_resolution=10, phi_resolution=10),
                color='#0f172a', show_scalar_bar=False, lighting=False,
                pickable=False, name=f'{metric}_locate_halo', render=False)
            halo.SetVisibility(False)
            halo.GetProperty().SetOpacity(.85)
            halo.GetProperty().SetFrontfaceCulling(True)
            self.locate_halos[metric] = halo
            # Screen-space locate guide ring: a clearly auxiliary, thin
            # two-line ring in DISPLAY coordinates (constant pixel size).  It is
            # drawn after the 3D scene and is not depth-tested, so it stays
            # findable when the real 3-D sphere at real XYZ is hidden behind
            # the opaque slice; it never replaces or moves the real sphere.
            ring_outer = self._new_ring_actor('#0f172a', 3.0)
            ring_inner = self._new_ring_actor('#dc2626', 1.6)
            for ring_actor in (ring_outer, ring_inner):
                ring_actor.SetVisibility(False)
                self.plot_renderers[metric].AddViewProp(ring_actor)
            self.locate_rings[metric] = [ring_outer, ring_inner]


            title_actor = p.add_text(
                'Er (GPa)' if metric == 'Er' else 'H (GPa)',
                position=(.02, self.PLOT_TITLE_Y), viewport=True,
                font_size=12, color='#1f2937',
                name=f'metric_title_{metric}', render=False,
                **self._font_kwargs())
            # The Pillow skin paints the card title, colour bar and pill.
            title_actor.SetVisibility(False)
            hover_actor = p.add_text('', position=(.02, self.HOVER_Y),
                                     viewport=True,
                                     font_size=self.HOVER_FONT_SIZE,
                                     color='#7c2d12',
                                     name=f'hover_{metric}', render=False,
                                     **self._font_kwargs())
            self.hover_actors[metric] = hover_actor
            hover_actor.SetVisibility(False)

            self._init_plot_camera(metric)
            # P2: relative-coordinate axes drawn by gui/scene_axes.py.  They
            # replace plotter.show_bounds(), which repeated the titles on four
            # edges, printed absolute 1.6e+07 labels and ran a Z axis through
            # the data.  The frame they draw is the only box outline we keep.
            self.axes_actors[metric] = self._build_scene_axes(metric)
        self.help_actor = p.add_text(
            self._t('help.slice'),
            position=(.02, self.PLOT_HELP_Y),
            viewport=True, font_size=8, color='#475569',
            name='interaction_help', render=False, **self._font_kwargs())
        # Kept for reports/tests; the design shows the hint in the header.
        self.help_actor.SetVisibility(False)

    # ------------------------------------------------------------ cube axes
    def _build_scene_axes(self, metric: str):
        """Relative-coordinate axes: wireframe box + 2D tick labels/titles."""
        renderer = self.plot_renderers[metric]
        span_x, span_y, span_z = self.display_span_nm
        x_ticks = [(value, scene_axes.format_nm(value))
                   for value in scene_axes.nice_ticks(span_x, 3)]
        y_ticks = [(value, scene_axes.format_nm(value))
                   for value in scene_axes.nice_ticks(span_y, 3)]
        # The depth labels read 0 at the top face and grow downwards, while
        # world Z runs upwards (mirrored display frame).
        z_ticks = [((span_z - depth) * self.z_scale,
                    scene_axes.format_nm(depth))
                   for depth in scene_axes.nice_ticks(span_z, 4)]
        return scene_axes.SceneAxes(
            renderer, self.display_bounds, x_ticks, y_ticks, z_ticks,
            titles=('X (nm)', 'Y (nm)', 'heff (nm)'),
            ink=self.AXIS_INK, line=self.AXIS_LINE,
            label_font=self.AXIS_LABEL_FONT,
            title_font=self.AXIS_TITLE_FONT)

    # ---------------------------------------------------------- point cloud
    def _cloud_source(self, metric: str) -> dict:
        """Deterministic depth-sorted LOD subset of the measured points.

        Built once per dataset and metric.  Points live in the relative,
        mirrored display frame; sorting by physical depth descending makes
        "keep every point at least as deep as the slice" a prefix of the array.
        """
        cached = self._cloud_sources.get(metric)
        if cached is not None:
            return cached
        field = self.field
        shape = field.valid.shape
        ids = np.flatnonzero(field.valid.ravel(order='C'))
        values_all = (field.er_gpa if metric == 'Er' else field.h_gpa)
        values = np.asarray(values_all, dtype=np.float64).ravel(order='C')[ids]
        k_index, j_index, i_index = np.unravel_index(ids, shape)
        z_nm = np.asarray(field.z_nm, dtype=np.float64)[k_index]
        finite = np.isfinite(values)
        ids, values, k_index, j_index, i_index, z_nm = (
            ids[finite], values[finite], k_index[finite], j_index[finite],
            i_index[finite], z_nm[finite])
        if ids.size > CLOUD_POINT_LIMIT:
            rng = np.random.default_rng(CLOUD_SEED)
            pick = np.sort(rng.choice(ids.size, size=CLOUD_POINT_LIMIT,
                                      replace=False))
            ids, values, k_index = ids[pick], values[pick], k_index[pick]
            j_index, i_index, z_nm = (j_index[pick], i_index[pick], z_nm[pick])
        order = np.argsort(-z_nm, kind='stable')
        z_nm = z_nm[order]
        values = values[order]
        k_index, j_index, i_index = (k_index[order], j_index[order],
                                     i_index[order])
        xyz = np.column_stack([
            np.asarray(field.x_nm, dtype=np.float64)[i_index]
            - self.disp_origin[0],
            np.asarray(field.y_nm, dtype=np.float64)[j_index]
            - self.disp_origin[1],
            self.disp_origin[2] - z_nm])
        # Break the visible lattice without touching the measured values: each
        # point stays inside its own voxel cell, so depths and colours still
        # belong to the original measurement.
        spacing = np.array([
            np.abs(np.diff(np.asarray(field.x_nm, dtype=np.float64))).max(initial=0.0),
            np.abs(np.diff(np.asarray(field.y_nm, dtype=np.float64))).max(initial=0.0),
            np.abs(np.diff(np.asarray(field.z_nm, dtype=np.float64))).max(initial=0.0),
        ]) * CLOUD_JITTER_CELL
        if np.any(spacing > 0.0):
            jitter = np.random.default_rng(CLOUD_SEED + 1).uniform(
                -1.0, 1.0, size=xyz.shape) * spacing
            xyz = xyz + jitter
        data = {'xyz': np.ascontiguousarray(xyz, dtype=np.float64),
                'value': np.ascontiguousarray(values, dtype=np.float64),
                'z_desc': np.ascontiguousarray(-z_nm, dtype=np.float64),
                'count': int(z_nm.size)}
        self._cloud_sources[metric] = data
        return data

    def _build_cloud(self, metric: str):
        """One translucent vertex-cell actor per plot, hidden until cloud mode."""
        data = self._cloud_source(metric)
        poly = pv.PolyData(data['xyz'])
        poly.point_data['value'] = data['value']
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(poly)
        mapper.SetLookupTable(self.color_tables[metric])
        mapper.SetScalarRange(*self.fixed_ranges[metric])
        mapper.SetScalarModeToUsePointData()
        # The depth cut moves on the existing geometry: a clipping plane is a
        # GPU-side mask, so dragging the slider never rebuilds or re-uploads
        # the 60k point cloud.
        clip = vtk.vtkPlane()
        clip.SetNormal(0.0, 0.0, -1.0)
        mapper.AddClippingPlane(clip)
        self.cloud_clip_planes[metric] = clip
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetPointSize(CLOUD_POINT_SIZE)
        prop.SetOpacity(CLOUD_OPACITY)
        prop.SetLighting(False)
        actor.SetScale(1, 1, self.z_scale)
        actor.PickableOff()
        actor.SetVisibility(False)
        self.plot_renderers[metric].AddActor(actor)
        self.cloud_actors[metric] = actor
        # Legacy B pilot: the same points above the depth cut, drawn as a very
        # light fog (small dots, low opacity, never picked).  Opt-in through
        # set_cloud_fog(); the design shows the slice face inside such a haze.
        fog_mapper = vtk.vtkPolyDataMapper()
        fog_mapper.SetInputData(poly)
        fog_mapper.SetLookupTable(self.color_tables[metric])
        fog_mapper.SetScalarRange(*self.fixed_ranges[metric])
        fog_mapper.SetScalarModeToUsePointData()
        fog_plane = vtk.vtkPlane()
        fog_plane.SetNormal(0.0, 0.0, 1.0)
        fog_mapper.AddClippingPlane(fog_plane)
        self.cloud_fog_planes[metric] = fog_plane
        fog = vtk.vtkActor()
        fog.SetMapper(fog_mapper)
        fog_prop = fog.GetProperty()
        fog_prop.SetPointSize(CLOUD_FOG_SIZE)
        fog_prop.SetOpacity(CLOUD_FOG_OPACITY)
        fog_prop.SetLighting(False)
        fog.SetScale(1, 1, self.z_scale)
        fog.PickableOff()
        fog.SetVisibility(False)
        self.plot_renderers[metric].AddActor(fog)
        self.cloud_fog_actors[metric] = fog
        self.cloud_lod_counts[metric] = int(data['count'])
        self._apply_cloud_clip()
        return actor

    def _cloud_keep_count(self, data: dict) -> int:
        """How many leading points are at or below the current slice."""
        total = int(data['count'])
        if not self.current_range.valid or self.current_depth is None:
            return total
        # z_desc is ascending -z_nm, so "at least as deep as the slice" is a
        # prefix of the depth-sorted array (P0 prototype).
        return int(np.searchsorted(data['z_desc'], -float(self.current_depth),
                                   side='right'))

    def _apply_cloud_clip(self) -> None:
        """Move the depth cut without touching the cloud geometry.

        Only the measured points at the current layer and deeper stay visible,
        as in the design reference: the cloud above the slice would otherwise
        hide the layer the user is inspecting.  A clipping plane on the mapper
        is applied by the GPU, so a depth change is just a plane origin update
        (the previous prefix rebuild cost 6-10 ms and re-uploaded vertices).
        """
        depth = None if not self.current_range.valid else self.current_depth
        # Mapper clipping planes are specified in world coordinates, i.e. after
        # the actor's (1, 1, z_scale); keep the cut in that frame.
        top = float(self.display_bounds[5]) + 1.0
        for metric in self.metrics:
            clip = getattr(self, 'cloud_clip_planes', {}).get(metric)
            if clip is None:
                continue
            if depth is None:
                z_world = top
            else:
                z_world = (self.disp_origin[2] - float(depth)) * self.z_scale
            clip.SetOrigin(0.0, 0.0, z_world)
            clip.Modified()
            fog_plane = getattr(self, 'cloud_fog_planes', {}).get(metric)
            if fog_plane is not None:
                # Opposite normal: the fog keeps what the dense cloud drops.
                fog_plane.SetOrigin(0.0, 0.0, z_world)
                fog_plane.Modified()
            data = self._cloud_sources.get(metric)
            if data is not None:
                self.cloud_points_shown[metric] = self._cloud_keep_count(data)


    # -------------------------------------------------------------- UI v2 skin
    def _setup_skin(self, window_size) -> None:
        """Attach the Pillow skin (layer 0) and move the plots to layer 1."""
        width, height = int(window_size[0]), int(window_size[1])
        self.plotter.render_window.SetNumberOfLayers(3)
        self._build_skin_context(width, height)
        self._build_help_context(width, height)
        for metric, renderer in self.plot_renderers.items():
            renderer.SetLayer(1)
            renderer.SetBackground(1.0, 1.0, 1.0)
            renderer.SetViewport(*self.layout.plot_viewport(metric))
        self._skin_ready = True
        self._render_skin(render=False)
        # One real render first: a renderer only reports its viewport pixel
        # size after a render, and the margin fit below needs that aspect.
        self.plotter.render()
        self.fit_views(render=False)
        # Place the axis labels once now; the render observer keeps them in
        # sync afterwards (offscreen renders do not always fire it).
        self._refresh_axes_scale()
        for axes in self.axes_actors.values():
            axes.update()
        self._render_skin(render=False)

    def _build_skin_context(self, width: int, height: int,
                            supersample: int | None = None) -> None:
        """(Re)build layout, fonts, widgets, skin and the image actor."""
        self.layout = ui_layout.Layout(width, height, self.ui_scale)
        self.fonts = theme.FontBook(self.layout.font_scale)
        self.widgets = ui_widgets.build(self.layout)
        factor = (self._skin_supersample() if supersample is None
                  else max(1, int(supersample)))
        self.skin = ui_chrome.Skin(self.layout, self.fonts, self.language,
                                   supersample=factor)
        renderer = getattr(self, '_skin_renderer', None)
        if renderer is None:
            renderer = vtk.vtkRenderer()
            renderer.SetLayer(0)
            renderer.SetInteractive(False)
            renderer.SetBackground(1.0, 1.0, 1.0)
            renderer.SetViewport(0.0, 0.0, 1.0, 1.0)
            self.plotter.render_window.AddRenderer(renderer)
            image = vtk.vtkImageData()
            image.SetDimensions(width, height, 1)
            # Map the bitmap in display units instead of world units: the
            # slice mapper stretches a (w-1) x (h-1) world rectangle over the
            # w x h viewport, so nearest-neighbour sampling flips single
            # columns from row to row (visible as a one-pixel step on the
            # colour-bar edge).  vtkImageMapper blits one texel per pixel.
            mapper = vtk.vtkImageMapper()
            mapper.SetInputData(image)
            mapper.SetColorWindow(255.0)
            mapper.SetColorLevel(127.5)
            actor = vtk.vtkActor2D()
            actor.SetMapper(mapper)
            actor.SetPosition(0.0, 0.0)
            actor.SetPickable(False)
            renderer.AddActor(actor)
            # The picker can hand a drag outside the plot cards to this
            # renderer; re-pin its camera before every frame.
            renderer.AddObserver('StartEvent', self._pin_skin_camera)
            self._skin_renderer = renderer
            self._skin_image = image
            self._skin_actor = actor
        else:
            self._skin_image.SetDimensions(width, height, 1)
        self._skin_size = (int(width), int(height))
        self._thumb_cache = (None, None)
        self._pin_skin_camera()

    def _pin_skin_camera(self, *_args) -> None:
        """Force the 2-D skin camera back to its fixed top-down pose.

        The interactor picker can land on this full-window renderer when a
        drag starts outside every plot card (for example on the depth slider);
        the trackball style would then rotate the whole UI.  Pinning the
        camera on every render makes any such rotation invisible.
        """
        if not self._skin_size:
            return
        width, height = self._skin_size
        camera = self._skin_renderer.GetActiveCamera()
        camera.ParallelProjectionOn()
        camera.SetPosition(width / 2.0, height / 2.0, 1.0)
        camera.SetFocalPoint(width / 2.0, height / 2.0, 0.0)
        camera.SetViewUp(0.0, 1.0, 0.0)
        camera.SetParallelScale(height / 2.0)

    def _build_help_context(self, width: int, height: int) -> None:
        """Attach the layer-2 RGBA help overlay (hidden until help opens)."""
        renderer = getattr(self, '_help_renderer', None)
        if renderer is None:
            renderer = vtk.vtkRenderer()
            renderer.SetLayer(2)
            renderer.SetInteractive(False)
            renderer.SetBackground(0.0, 0.0, 0.0)
            # Keep the plots' pixels: the card image is alpha-composited on
            # top of them instead of replacing the frame with a black page.
            renderer.SetBackgroundAlpha(0.0)
            renderer.SetPreserveColorBuffer(True)
            renderer.SetPreserveDepthBuffer(True)
            renderer.SetViewport(0.0, 0.0, 1.0, 1.0)
            renderer.SetDraw(False)
            self.plotter.render_window.AddRenderer(renderer)
            image = vtk.vtkImageData()
            image.SetDimensions(width, height, 1)
            mapper = vtk.vtkImageMapper()
            mapper.SetInputData(image)
            mapper.SetColorWindow(255.0)
            mapper.SetColorLevel(127.5)
            actor = vtk.vtkActor2D()
            actor.SetMapper(mapper)
            actor.SetPosition(0.0, 0.0)
            actor.SetPickable(False)
            renderer.AddActor(actor)
            self._help_renderer = renderer
            self._help_image = image
            self._help_actor = actor
        else:
            self._help_image.SetDimensions(width, height, 1)
        self._help_size = (int(width), int(height))

    def _upload_help(self, image) -> None:
        array = np.asarray(image, dtype=np.uint8)[::-1]
        flat = np.ascontiguousarray(array.reshape(-1, 4))
        scalars = numpy_to_vtk(flat, deep=True,
                               array_type=vtk.VTK_UNSIGNED_CHAR)
        self._help_image.GetPointData().SetScalars(scalars)
        self._help_image.Modified()

    def _render_help(self, render: bool = True) -> None:
        if (self._help_renderer is None
                or not getattr(self, '_skin_ready', False)):
            return
        width, height = self._window_size()
        if (int(width), int(height)) != tuple(self._help_size or ()):
            self._help_image.SetDimensions(int(width), int(height), 1)
            self._help_size = (int(width), int(height))
        self._upload_help(self.skin.render_help())
        if render:
            self.plotter.render()

    def set_help_open(self, open_: bool, render: bool = True) -> None:
        """Show/hide the help card; F1, Esc, the close box and the backdrop
        all funnel through here so the camera lock and repaint stay paired."""
        open_ = bool(open_)
        if open_ == self.help_open:
            return
        self.help_open = open_
        if self._help_renderer is None:
            return
        if open_:
            self._render_help(render=False)
            self._help_renderer.SetDraw(True)
            self._set_plots_interactive(False)
        else:
            self._help_renderer.SetDraw(False)
            self._set_plots_interactive(True)
        if render:
            self.plotter.render()

    def _set_plots_interactive(self, enabled: bool) -> None:
        for renderer in self.plot_renderers.values():
            renderer.SetInteractive(bool(enabled))

    def _rebuild_skin(self, width: int, height: int,
                      supersample: int | None = None) -> None:
        if (int(width), int(height)) == tuple(self._skin_size or ()):
            return
        self._build_skin_context(int(width), int(height), supersample)
        for metric, renderer in self.plot_renderers.items():
            renderer.SetViewport(*self.layout.plot_viewport(metric))
        if self._help_renderer is not None:
            self._help_image.SetDimensions(int(width), int(height), 1)
            self._help_size = (int(width), int(height))
            if self.help_open:
                self._render_help(render=False)
        self._render_skin(render=False)

    def _skin_supersample(self) -> int:
        """2x text supersampling while the text is small enough to need it."""
        scale = float(getattr(self.layout, 'font_scale', 1.0) or 1.0)
        if scale <= self.SKIN_SUPERSAMPLE_MAX_SCALE:
            return self.SKIN_SUPERSAMPLE
        return 1

    def _arm_skin_refine(self, delay: float = 0.22) -> None:
        """Ask for one supersampled base once a live resize has settled.

        The static base costs ~4x more to paint at 2x, which would make a
        window drag crawl; repainting it at 1x during the drag and refining
        after a short quiet period keeps the drag smooth and the text crisp.
        """
        self._skin_refine_at = time.monotonic() + float(delay)
        interactor = getattr(getattr(self.plotter, 'iren', None),
                             'interactor', None)
        if interactor is None:
            return
        try:
            # vtkRenderWindowInteractor takes milliseconds here.
            interactor.CreateOneShotTimer(int(round(float(delay) * 1000)))
        except Exception:  # pragma: no cover - interactor without timers
            self._skin_refine_at = 0.0

    def _on_skin_timer(self, *_args) -> None:
        due = float(getattr(self, '_skin_refine_at', 0.0) or 0.0)
        if not due or time.monotonic() < due:
            return
        self._skin_refine_at = 0.0
        skin = getattr(self, 'skin', None)
        if skin is None or not getattr(self, '_skin_ready', False):
            return
        wanted = self._skin_supersample()
        if wanted <= skin.supersample:
            return
        skin.supersample = wanted
        skin.invalidate()
        self._render_skin()

    def _refresh_axes_scale(self) -> None:
        """Keep the 2-D axis labels proportional to the window scale."""
        scale = float(getattr(self.layout, 'font_scale', 1.0) or 1.0)
        for axes in self.axes_actors.values():
            axes.apply_scale(scale)

    def _lut_colors(self) -> dict:
        """256 RGB entries per metric, the same palette the slice actor uses."""
        if self._lut_cache:
            return self._lut_cache
        tables = {'Er': 'viridis', 'H': 'plasma'}
        cache = {}
        for metric in self.metrics:
            lut = pv.LookupTable(cmap=tables[metric])
            lut.scalar_range = (0.0, 1.0)
            colors = []
            for value in np.linspace(0.0, 1.0, 256):
                rgba = lut.map_value(float(value))
                colors.append((int(round(float(rgba[0]) * 255)),
                               int(round(float(rgba[1]) * 255)),
                               int(round(float(rgba[2]) * 255))))
            cache[metric] = colors
        self._lut_cache = cache
        return cache

    def _thumbnail(self):
        """Small top view of the current slice, linked to the clicked card."""
        depth = self.current_depth
        metric = self._thumb_metric if self._thumb_metric in self.metrics else 'Er'
        if depth is None or not self.current_range.valid:
            return None
        key = (metric, round(float(depth), 6),
               tuple(self.fixed_ranges[metric]))
        if self._thumb_cache[0] == key and self._thumb_cache[1] is not None:
            return self._thumb_cache[1]
        plane = self.current_planes.get(metric)
        if plane is None:
            plane = field3d.slice_metric(self.field, metric, 'z', float(depth))
        mask = np.asarray(plane.mask, dtype=bool)
        if mask.size == 0:
            return None
        lo, hi = (float(v) for v in self.fixed_ranges[metric])
        span = max(hi - lo, 1e-9)
        values = np.nan_to_num(np.asarray(plane.values, dtype=np.float64),
                               nan=lo)
        scaled = np.clip((values - lo) / span, 0.0, 1.0)
        indices = np.clip((scaled * 255.0).astype(np.int64), 0, 255)
        colors = np.asarray(self._lut_colors()[metric], dtype=np.uint8)
        rgb = colors[indices]
        rgb[~mask] = np.array([244, 247, 251], dtype=np.uint8)
        image = Image.fromarray(np.flipud(rgb), 'RGB')
        self._thumb_cache = (key, image)
        return image

    def _histogram_counts(self, lo: float, hi: float):
        """Measured points per 1 nm depth bin (never interpolated layers)."""
        if hi <= lo:
            return None
        bins = int(min(120, max(8, round(float(hi) - float(lo)))))
        counts, _edges = np.histogram(
            np.asarray(self.dataset.z_nm, dtype=np.float64),
            bins=bins, range=(float(lo), float(hi)))
        return [int(value) for value in counts]

    def _slice_position(self) -> tuple[int, int]:
        depths = np.asarray(self.layers.depths_nm, dtype=np.float64)
        count = int(depths.size)
        if self.current_depth is None or count == 0:
            return 0, count
        index = int(np.argmin(np.abs(depths - float(self.current_depth)))) + 1
        return index, count

    def _status_items(self) -> tuple:
        items = [self._t('status.ready')]
        info = self._info_line(include_notice=False)
        for part in str(info).split(' | '):
            part = part.strip()
            if part:
                items.append(part)
        key = 'header.cloud' if self.display_mode == 'cloud' else 'header.slice'
        items.append(self._t('status.view', view=self._t(key)))
        if self.view_mode != 'raw' and self.current_depth is not None:
            items.append(self._t('card.heff', depth=float(self.current_depth)))
        return tuple(items)

    def _notice_is_error(self) -> bool:
        text = str(self._notice or '').lower()
        return any(token in text for token in
                   ('fail', 'error', '错误', '失败', 'unavailable', '不可'))

    def _skin_state(self) -> ui_chrome.SkinState:
        valid = bool(self.current_range.valid)
        lo = float(self.current_range.lo_nm) if valid else 0.0
        hi = float(self.current_range.hi_nm) if valid else 0.0
        index, count = self._slice_position()
        nx, ny, nz = int(self.field.nx), int(self.field.ny), int(self.field.nz)
        self.widgets.sync_groups({
            'language': self.language, 'view': self.view_mode,
            'range': self.range_mode, 'reference': self.reference_mode,
            'interp': self.interpolation,
            'display': self.display_mode})
        self.widgets.set_checked('export.open_after', self.open_after_export)
        # Re-sync the haze toggle too: a resize rebuilds the widget set, and
        # the checkbox must not come back unchecked while the haze is on.
        fog_on = (self.display_mode == 'cloud' and self.view_mode != 'raw'
                  and self.current_range.valid and self.current_depth is not None)
        self.widgets.set_checked('display.fog', bool(self.cloud_fog_enabled))
        self.widgets.set_disabled('display.fog', not fog_on)
        depths = np.asarray(self.dataset.z_nm, dtype=np.float64)
        hist_key = int(round(lo * 10.0)) * 100003 + int(round(hi * 10.0))
        return ui_chrome.SkinState(
            language=self.language,
            depth=(None if self.current_depth is None
                   else float(self.current_depth)),
            depth_lo=lo, depth_hi=hi, range_valid=valid,
            slice_index=index, slice_count=count,
            step_nm=float(self.field.dz_nm),
            view_mode=self.view_mode, range_mode=self.range_mode,
            reference_mode=self.reference_mode,
            display_mode=self.display_mode,
            thumb_metric=str(self._thumb_metric),
            dataset_label=self.dataset_label,
            point_count=int(getattr(self.dataset, 'point_count',
                                    depths.size)),
            file_count=int(getattr(self.dataset, 'group_count',
                                   len(self.dataset.groups))),
            grid_label=f'{nx} x {ny} x {nz}',
            step_labels=(f'dx = {self.field.dx_nm:.0f} nm',
                         f'dy = {self.field.dy_nm:.0f} nm',
                         f'dz = {self.field.dz_nm:.2f} nm'),
            ranges={metric: tuple(float(v) for v in self.fixed_ranges[metric])
                    for metric in self.metrics},
            lut_colors=self._lut_colors(),
            thumb=self._thumbnail(),
            hist_counts=self._histogram_counts(lo, hi) if valid else None,
            hist_key=hist_key,
            status_items=self._status_items(),
            status_heff=(self._t('card.heff', depth=float(self.current_depth))
                         if self.current_depth is not None else ''),
            notice=self._notice,
            notice_is_error=self._notice_is_error(),
            export_dir=self.export_dir,
            open_after_export=self.open_after_export,
            version=APP_VERSION,
            data_ready=valid,
            editing_depth=self.widgets.focus_id == 'depth.value',
            widgets=self.widgets)

    def _upload_skin(self, image) -> None:
        array = np.asarray(image.convert('RGB'), dtype=np.uint8)[::-1]
        flat = np.ascontiguousarray(array.reshape(-1, 3))
        scalars = numpy_to_vtk(flat, deep=True,
                               array_type=vtk.VTK_UNSIGNED_CHAR)
        self._skin_image.GetPointData().SetScalars(scalars)
        self._skin_image.Modified()

    def _render_skin(self, render: bool = True) -> None:
        if not getattr(self, '_skin_ready', False):
            return
        self._upload_skin(self.skin.render(self._skin_state()))
        if render:
            self.plotter.render()

    def clean_render_png(self, path) -> str:
        """Screenshot of the 3D views only, without the UI skin or help card."""
        target = str(path)
        self._skin_actor.SetVisibility(False)
        help_visible = (self._help_actor is not None
                        and bool(self._help_actor.GetVisibility()))
        if help_visible:
            self._help_actor.SetVisibility(False)
        try:
            self.plotter.render()
            self.plotter.screenshot(target)
        finally:
            self._skin_actor.SetVisibility(True)
            if help_visible:
                self._help_actor.SetVisibility(True)
            self._render_skin(render=False)
        return target

    # --------------------------------------------------------- export pixels
    def clean_export_rect(self) -> tuple[int, int, int, int]:
        """Pixel rectangle of the two plot cards in the render window."""
        cards = list(getattr(self.layout, 'plot_cards', {}).values())
        if not cards:
            width, height = self._window_size()
            return (0, 0, width, height)
        return (int(round(min(card.x0 for card in cards))),
                int(round(min(card.y0 for card in cards))),
                int(round(max(card.x1 for card in cards))),
                int(round(max(card.y1 for card in cards))))

    def clean_plotter(self):
        """Plotter proxy whose screenshot is cropped to the two plot cards.

        io_exports.png_export only needs render() and screenshot(); the proxy
        returns the plot-card union so the exported figure keeps the card
        titles, colour bars and heff pill but none of the toolbar, sidebar,
        depth card, export cards or status bar (P4 W6).  The live window,
        cameras and actors are untouched after the capture.
        """
        return _CleanPlotterProxy(self)

    def _capture_export_pixels(self, **kwargs):
        """Screenshot with the focused-card outline and help card hidden."""
        thumb = self._thumb_metric
        self._thumb_metric = ''
        help_visible = (self._help_actor is not None
                        and bool(self._help_actor.GetVisibility()))
        if help_visible:
            self._help_actor.SetVisibility(False)
        try:
            self._render_skin(render=False)
            self.plotter.render()
            return self.plotter.screenshot(None, return_img=True, **kwargs)
        finally:
            self._thumb_metric = thumb
            if help_visible:
                self._help_actor.SetVisibility(True)
            self._render_skin(render=False)
            self.plotter.render()

    # ------------------------------------------------------------- UI v2 api
    def ui_widgets(self):
        """Widget set of the current layout (hit tests share the painter)."""
        return self.widgets

    def set_display_mode(self, mode: str, render: bool = True) -> None:
        """Switch between the current-depth slice and the full point cloud."""
        mode = str(mode).strip().lower()
        if mode not in ('slice', 'cloud'):
            raise ValueError(f'Unknown display mode: {mode!r}')
        if self.points_only and mode != 'cloud':
            self.set_notice(self._t('notice.points_only'))
            return
        changed = mode != self.display_mode
        self.display_mode = mode
        self._apply_visibility()
        self._apply_cloud_clip()
        self._refresh_status(render=False)
        if render:
            self.plotter.render()

    def set_cloud_fog(self, enabled: bool, render: bool = True) -> None:
        """Toggle the haze of the points above the slice (cloud view only).

        Off by default -- W1's clean cloud hides everything above the current
        depth.  On, the whole segment from the slice up to the data top is
        drawn again as lighter dots, so the slice face sits inside a volume
        while the axes frame stays cut at the slice.
        """
        self.cloud_fog_enabled = bool(enabled)
        self._apply_visibility()
        self._refresh_status(render=False)
        if render:
            self.plotter.render()

    def set_thumb_metric(self, metric: str, render: bool = True) -> None:
        """Link the depth-card thumbnail to the Er or H plot card."""
        metric = str(metric or 'Er')
        if metric not in self.metrics or metric == self._thumb_metric:
            return
        self._thumb_metric = metric
        self._thumb_cache = (None, None)
        self._render_skin(render=render)

    def set_export_dir(self, path, render: bool = True) -> None:
        self.export_dir = '' if path is None else str(path)
        self._refresh_status(render=False)
        if render:
            self.plotter.render()

    def set_open_after_export(self, enabled: bool, render: bool = True) -> None:
        self.open_after_export = bool(enabled)
        self._refresh_status(render=False)
        if render:
            self.plotter.render()

    # ------------------------------------------------------- UI v2 interaction
    def _ui_point(self):
        iren = self.plotter.iren
        if iren is None:
            return None
        try:
            raw_x, raw_y = iren.get_event_position()
        except Exception:
            return None
        width, height = self._window_size()
        return float(raw_x), float(height - raw_y)

    def _pressed(self):
        return (self.widgets.get(self._pressed_widget)
                if self._pressed_widget else None)

    def _on_ui_press(self, *_args) -> None:
        point = self._ui_point()
        if point is None:
            return
        if self.help_open:
            x, y = point
            card, close = self.skin.help_geometry()
            if (close.contains(x, y, pad=self.layout._d(6))
                    or not card.contains(x, y)):
                self.set_help_open(False)
            return
        x, y = point
        widget = self.widgets.hit(x, y)
        self._slice_drag = False
        self.widgets.clear_pressed()
        self._pressed_widget = widget.id if widget is not None else None
        if widget is None:
            self._render_skin(render=True)
            return
        widget.pressed = True
        if widget.id == 'depth.slider':
            self._slice_drag = True
            self._drag_slider_to(x)
        self._render_skin(render=True)

    def _on_ui_release(self, *_args) -> None:
        if self.help_open:
            return
        dragged = self._slice_drag
        self._slice_drag = False
        pressed = self._pressed()
        self._pressed_widget = None
        self.widgets.clear_pressed()
        if dragged:
            self._render_skin(render=True)
            return
        point = self._ui_point()
        widget = self.widgets.hit(*point) if point is not None else None
        if (pressed is not None and widget is not None
                and widget.id == pressed.id):
            self._activate_widget(widget)
        else:
            self._render_skin(render=True)

    def _on_ui_move(self, *_args) -> None:
        iren = self.plotter.iren
        if iren is None:
            return
        point = self._ui_point()
        if point is None:
            return
        if self.help_open:
            return
        x, y = point
        if self._slice_drag:
            self._drag_slider_to(x)
            return
        if self.widgets.hover(x, y):
            self._render_skin(render=True)
        if self.view_mode == 'locate':
            try:
                raw_x, raw_y = iren.get_event_position()
            except Exception:
                return
            inside = any(card.contains(x, y)
                         for card in self.layout.plot_cards.values())
            if inside:
                self._update_hover(raw_x, raw_y)
            else:
                self._clear_hover(render=True)

    def _on_ui_drag(self, *_args) -> None:
        """Keep a skin slider under the mouse while the button is down.

        VTK reports mouse moves during a drag as InteractionEvent (a plain
        MouseMoveEvent is suppressed), so the hover/move observer never sees
        them; without this the slider only jumps on press and then ignores
        the drag.
        """
        if self.help_open or not self._slice_drag:
            return
        point = self._ui_point()
        if point is None:
            return
        self._drag_slider_to(point[0])

    def _on_ui_leave(self, *_args) -> None:
        if self.widgets.clear_hover():
            self._render_skin(render=True)
        self._clear_hover(render=True)

    def _on_ui_key(self, obj=None, *_args) -> None:
        # add_observer forwards the raw VTK interactor as obj; the PyVista
        # RenderWindowInteractor wrapper has no GetKeySym of its own.
        iren = obj if hasattr(obj, 'GetKeySym') else None
        if iren is None:
            iren = getattr(getattr(self.plotter, 'iren', None),
                           'interactor', None)
        if iren is None:
            return
        try:
            key = str(iren.GetKeySym())
        except Exception:
            return
        if key == 'F1':
            self.set_help_open(not self.help_open)
            return
        if self.help_open:
            if key == 'Escape':
                self.set_help_open(False)
            return
        if self.widgets.focus_id != 'depth.value':
            return
        if key in ('Return', 'KP_Enter'):
            text = self.widgets.buffer()
            self.widgets.cancel_edit()
            try:
                self.set_depth(float(text), render=True)
            except (TypeError, ValueError):
                self._render_skin(render=True)
        elif key == 'Escape':
            self.widgets.cancel_edit()
            self._render_skin(render=True)
        elif key == 'BackSpace':
            self.widgets.backspace()
            self._render_skin(render=True)
        elif len(key) == 1 and (key.isdigit() or key == '.'):
            self.widgets.type_char(key)
            self._render_skin(render=True)

    def _drag_slider_to(self, x: float) -> None:
        if not self.current_range.valid:
            return
        track = self.layout.slider_track
        if track.width <= 0.0:
            return
        fraction = min(max((float(x) - track.x0) / track.width, 0.0), 1.0)
        lo = float(self.current_range.lo_nm)
        hi = float(self.current_range.hi_nm)
        self.set_depth(lo + fraction * (hi - lo), render=True)

    def _begin_depth_edit(self) -> None:
        widget = self.widgets.get('depth.value')
        if widget is None:
            return
        self.widgets.begin_edit('depth.value')
        self._render_skin(render=True)

    def _activate_widget(self, widget) -> None:
        action = widget.action or ()
        if not action:
            if widget.id == 'depth.value':
                self._begin_depth_edit()
            return
        kind = action[0]
        value = action[1] if len(action) > 1 else None
        if kind == 'file':
            self._handle_file_action(value)
        elif kind == 'language':
            self.set_language(value, persist=True)
        elif kind == 'view':
            self.set_view_mode(value)
        elif kind == 'range':
            self.set_range_mode(value)
        elif kind == 'reference':
            self.set_reference_mode(value)
        elif kind == 'interp':
            self.set_interpolation(value)
        elif kind == 'display' and value == 'fog':
            self.set_cloud_fog(not self.cloud_fog_enabled)
        elif kind == 'display':
            self.set_display_mode(value)
        elif kind == 'reset':
            self.reset_view()
        elif kind == 'help':
            self.set_help_open(True)
        elif kind == 'export':
            self.export_kind = value or 'image'
            self._handle_file_action('export')
        elif kind == 'export_dir':
            if value == 'open_after':
                self.set_open_after_export(not self.open_after_export)
            elif self._export_dir_callback is not None:
                self._export_dir_callback(self)
            else:
                self.request_action('export_dir')
        elif kind == 'depth' and value == 'edit':
            self._begin_depth_edit()
        elif kind == 'thumb':
            self.set_thumb_metric(value)

    # ------------------------------------------------------------- camera/fit
    def _init_plot_camera(self, metric: str):
        """Canonical oblique camera + a margin so the full data box is visible.

        The previous code zoomed in after ``reset_camera`` and shifted the
        window centre, which pushed the data edges (and the axis labels) out of
        the viewport.  The camera direction, focal point and physical XY aspect
        are the same idea as before; only the fit keeps a real margin.
        """
        renderer = self.plot_renderers[metric]
        camera = renderer.GetActiveCamera()
        x0, x1, y0, y1, z0, z1 = self.display_bounds
        center = np.array([(x0 + x1) / 2.0, (y0 + y1) / 2.0, (z0 + z1) / 2.0])
        span = max(x1 - x0, y1 - y0)
        camera.ParallelProjectionOff()
        camera.SetPosition(*(center + span * np.array([-1.42, -1.42, 0.66])))
        camera.SetFocalPoint(*center)
        camera.SetViewUp(0, 0, 1)
        camera.SetWindowCenter(0, 0)
        renderer.ResetCamera(self.display_bounds)
        self._fit_view_margin(metric)
        # Mark the fitted camera as user-set: otherwise pyvista's first image
        # request (``show``/``screenshot``) replaces it with its own default
        # camera and silently undoes the margin fit.
        renderer.camera.is_set = True

    def _plot_viewport_px(self, metric: str) -> tuple[float, float, float, float]:
        width, height = self._window_size()
        vp = self.plot_renderers[metric].GetViewport()
        return (vp[0] * width, vp[1] * height, vp[2] * width, vp[3] * height)

    def projected_bounds(self, metric: str):
        """Display (pixel) coordinates of the eight real data-box corners."""
        renderer = self.plot_renderers[metric]
        x0, x1, y0, y1, z0, z1 = self.display_bounds
        points = []
        for x in (x0, x1):
            for y in (y0, y1):
                for z in (z0, z1):
                    renderer.SetWorldPoint(x, y, z, 1.0)
                    renderer.WorldToDisplay()
                    dx, dy, _dz = renderer.GetDisplayPoint()
                    points.append((float(dx), float(dy)))
        return points

    def projected_bounds_margins(self, metric: str):
        """Per-corner pixel margins of the data box inside the plot viewport."""
        vx0, vy0, vx1, vy1 = self._plot_viewport_px(metric)
        margins = []
        for dx, dy in self.projected_bounds(metric):
            margins.append((dx - vx0, vx1 - dx, dy - vy0, vy1 - dy))
        return margins

    @staticmethod
    def _camera_basis(camera):
        """(forward, right, up) unit vectors of the camera's view frame."""
        forward = np.asarray(camera.GetFocalPoint(), dtype=np.float64) - \
            np.asarray(camera.GetPosition(), dtype=np.float64)
        norm = float(np.linalg.norm(forward))
        if norm <= 0.0:
            forward = np.array([0.0, 0.0, -1.0])
        else:
            forward = forward / norm
        up = np.asarray(camera.GetViewUp(), dtype=np.float64)
        right = np.cross(forward, up)
        right_norm = float(np.linalg.norm(right))
        if right_norm <= 0.0:
            right = np.array([1.0, 0.0, 0.0])
        else:
            right = right / right_norm
        true_up = np.cross(right, forward)
        return forward, right, true_up

    def _fit_view_margin(self, metric: str):
        """Pull the camera back until the real data bounds keep a free margin.

        The fit is computed analytically in the camera's own frame (not from
        ``WorldToDisplay``, whose cached view transform is stale until render),
        so it works for perspective and parallel projection, keeps the current
        orientation and never changes the physical XY proportions.
        """
        renderer = self.plot_renderers[metric]
        camera = renderer.GetActiveCamera()
        margin = min(max(float(self.VIEW_FIT_MARGIN), 0.0), 0.40)
        if margin <= 0.0:
            renderer.ResetCameraClippingRange()
            return
        target = 1.0 - 2.0 * margin
        width, height = renderer.GetSize()
        width = max(float(width), 1.0)
        height = max(float(height), 1.0)
        aspect = width / height
        tan_y = math.tan(math.radians(float(camera.GetViewAngle())) / 2.0)
        tan_x = tan_y * aspect
        if tan_x <= 0.0 or tan_y <= 0.0:
            renderer.ResetCameraClippingRange()
            return
        forward, right, up = self._camera_basis(camera)
        position = np.asarray(camera.GetPosition(), dtype=np.float64)
        focal = np.asarray(camera.GetFocalPoint(), dtype=np.float64)
        x0, x1, y0, y1, z0, z1 = self.display_bounds
        corners = [np.array([x, y, z], dtype=np.float64)
                   for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
        if camera.GetParallelProjection():
            scale = max(float(camera.GetParallelScale()), 1e-12)
            need = 1.0
            for corner in corners:
                offset = corner - position
                need = max(need,
                           abs(float(np.dot(offset, up))) / (scale * target),
                           abs(float(np.dot(offset, right))) /
                           (scale * aspect * target))
            if need > 1.0:
                camera.SetParallelScale(scale * need)
        else:
            distance = max(float(camera.GetDistance()), 1e-9)
            deficit = None
            for corner in corners:
                offset = corner - position
                z_view = float(np.dot(offset, forward))
                if z_view <= 1e-9:
                    continue
                need = max(abs(float(np.dot(offset, right))) / (tan_x * target),
                           abs(float(np.dot(offset, up))) / (tan_y * target))
                value = need - z_view
                deficit = value if deficit is None else max(deficit, value)
            # ResetCamera() only fits the bounding sphere, which leaves a lot of
            # empty frame around an oblique box; a negative deficit therefore
            # moves the camera closer until the box really touches the margin.
            if deficit is not None and abs(deficit) > 1e-6:
                camera.SetPosition(*(focal - forward * (distance + deficit)))
                distance += deficit
            self._center_view(metric, camera, forward, right, up, tan_x, tan_y,
                              target)
        renderer.ResetCameraClippingRange()

    def _center_view(self, metric: str, camera, forward, right, up,
                     tan_x: float, tan_y: float, target: float) -> None:
        """Pan so the projected data box sits in the middle of the viewport.

        The oblique box is not symmetric on screen (perspective pushes the near
        corners down), so a symmetric fit still leaves one side with twice the
        free margin.  Panning by the measured offset lets the next pass zoom in
        on both sides.
        """
        width, height = self.plot_renderers[metric].GetSize()
        if width < 8 or height < 8:
            return
        position = np.asarray(camera.GetPosition(), dtype=np.float64)
        focal = np.asarray(camera.GetFocalPoint(), dtype=np.float64)
        distance = max(float(camera.GetDistance()), 1e-9)
        pixels = []
        for x in self.display_bounds[0:2]:
            for y in self.display_bounds[2:4]:
                for z in self.display_bounds[4:6]:
                    offset = np.array([x, y, z], dtype=np.float64) - position
                    depth = max(float(np.dot(offset, forward)), 1e-6)
                    px = float(np.dot(offset, right)) / (depth * tan_x)
                    py = float(np.dot(offset, up)) / (depth * tan_y)
                    pixels.append((px, py))
        left = min(p[0] for p in pixels)
        right_edge = max(p[0] for p in pixels)
        bottom = min(p[1] for p in pixels)
        top = max(p[1] for p in pixels)
        offset_x = (left + right_edge) * 0.5
        offset_y = (bottom + top) * 0.5
        if abs(offset_x) < 1e-6 and abs(offset_y) < 1e-6:
            return
        units_per_ndc = 2.0 * tan_y * distance
        shift = right * (offset_x * 0.5 * units_per_ndc) + \
            up * (offset_y * 0.5 * units_per_ndc)
        camera.SetPosition(*(position + shift))
        camera.SetFocalPoint(*(focal + shift))
        self._fit_view_margin_distance(camera, forward, distance, tan_x, tan_y,
                                       target)

    def _fit_view_margin_distance(self, camera, forward, distance, tan_x,
                                  tan_y, target) -> None:
        """Second fit pass after the pan: zoom to the margin again."""
        position = np.asarray(camera.GetPosition(), dtype=np.float64)
        focal = np.asarray(camera.GetFocalPoint(), dtype=np.float64)
        forward = np.asarray(forward, dtype=np.float64)
        right = np.cross(forward, np.asarray(camera.GetViewUp(), dtype=np.float64))
        norm = float(np.linalg.norm(right))
        if norm <= 1e-9:
            return
        right = right / norm
        up = np.cross(right, forward)
        distance = max(float(camera.GetDistance()), 1e-9)
        deficit = None
        for x in self.display_bounds[0:2]:
            for y in self.display_bounds[2:4]:
                for z in self.display_bounds[4:6]:
                    offset = np.array([x, y, z], dtype=np.float64) - position
                    z_view = float(np.dot(offset, forward))
                    if z_view <= 1e-9:
                        continue
                    need = max(abs(float(np.dot(offset, right))) / (tan_x * target),
                               abs(float(np.dot(offset, up))) / (tan_y * target))
                    value = need - z_view
                    deficit = value if deficit is None else max(deficit, value)
        if deficit is not None and abs(deficit) > 1e-6:
            camera.SetPosition(*(focal - forward * (distance + deficit)))

    def fit_views(self, render: bool = False):
        """Re-fit both plots to the full data bounds, keeping the orientation."""
        for metric in self.metrics:
            renderer = self.plot_renderers[metric]
            renderer.ResetCamera(self.display_bounds)
            self._fit_view_margin(metric)
        if render:
            self.plotter.render()

    def _capture_initial_state(self):
        """Remember the dataset's startup view for the reset button."""
        cameras = {}
        for metric in self.metrics:
            camera = self.plot_renderers[metric].GetActiveCamera()
            cameras[metric] = {
                'position': tuple(camera.GetPosition()),
                'focal': tuple(camera.GetFocalPoint()),
                'view_up': tuple(camera.GetViewUp()),
                'parallel': bool(camera.GetParallelProjection()),
                'parallel_scale': float(camera.GetParallelScale()),
                'view_angle': float(camera.GetViewAngle()),
                'window_center': tuple(camera.GetWindowCenter()),
                'clipping': tuple(camera.GetClippingRange()),
            }
        self._initial_state = {
            'cameras': cameras,
            'depth': None if self.current_depth is None else float(self.current_depth),
            'range_mode': self.range_mode,
            'reference_mode': self.reference_mode,
            'view_mode': self.view_mode,
            'display_mode': self.display_mode,
            'interpolation': self.interpolation,
        }

    def _restore_camera(self, metric: str, camera_state: dict):
        camera = self.plot_renderers[metric].GetActiveCamera()
        camera.SetPosition(*camera_state['position'])
        camera.SetFocalPoint(*camera_state['focal'])
        camera.SetViewUp(*camera_state['view_up'])
        camera.SetParallelProjection(camera_state['parallel'])
        camera.SetParallelScale(camera_state['parallel_scale'])
        camera.SetViewAngle(camera_state['view_angle'])
        camera.SetWindowCenter(*camera_state['window_center'])
        camera.SetClippingRange(*camera_state['clipping'])

    def reset_view(self, render: bool = True):
        """Restore the current dataset's startup state (no data reload/rebuild).

        Restores both cameras/projection/zoom, the initial depth, normal /
        common / auto modes, slider renderer/enabled/value and actor
        visibility, and clears the locate marker and hover hint.  Repeated
        clicks are idempotent; loaded data, the field and source files are
        never touched.
        """
        state = getattr(self, '_initial_state', None)
        if state is None:
            return
        self.view_mode = state['view_mode']
        self.range_mode = state['range_mode']
        self.reference_mode = state['reference_mode']
        self.display_mode = state.get('display_mode', 'cloud')
        self.interpolation = state.get('interpolation', self.interpolation)
        self.resolved_reference_mode = self._resolve_reference_mode()
        self.current_range = self.range_modes[self.range_mode]
        depth = state['depth']
        if depth is not None and self.current_range.valid:
            depth = float(np.clip(depth, self.current_range.lo_nm,
                                  self.current_range.hi_nm))
            self.current_depth = depth
        else:
            self.current_depth = depth
        self._clear_hover(render=False)
        for metric, camera_state in state['cameras'].items():
            self._restore_camera(metric, camera_state)
            # Keep the startup framing valid for the *current* viewport: after
            # a resize the stored camera may no longer leave the 12 % margin.
            # At the original size this is a no-op.
            self._fit_view_margin(metric)
        self._camera_customized = False
        if self.slider is not None:
            rep = self.slider.GetRepresentation()
            if self.current_range.valid:
                rep.SetMinimumValue(float(self.current_range.lo_nm))
                rep.SetMaximumValue(float(self.current_range.hi_nm))
                rep.SetValue(float(self.current_depth))
                self.slider.SetEnabled(True)
                self._bind_slider_widget()
            else:
                self.slider.SetEnabled(False)
        self._update_slice(render=False)
        self._apply_visibility()
        self._refresh_depth_value()
        self._refresh_ticks(render=False)
        self._refresh_status(render=False)
        if render:
            self.plotter.render()

    def _slice_display_field(self):
        """Dense X/Y axes for the display-only cubic/TPS slice mesh.

        The stored field keeps its own (55 x 55) sampling; only the mesh the
        viewer builds is densified, so the smooth interpolant is not turned
        back into 55 visible quads at large window sizes.
        """
        x = np.asarray(self.field.x_nm, dtype=np.float64)
        y = np.asarray(self.field.y_nm, dtype=np.float64)
        factor = max(1, int(SLICE_DISPLAY_SUPERSAMPLE))
        if x.size > 1:
            x = np.linspace(x[0], x[-1], (x.size - 1) * factor + 1)
        if y.size > 1:
            y = np.linspace(y[0], y[-1], (y.size - 1) * factor + 1)
        return SimpleNamespace(x_nm=x, y_nm=y)

    def _xy_grid(self, depth: float) -> pv.StructuredGrid:
        xx, yy = np.meshgrid(self.field.x_nm - self.disp_origin[0],
                             self.field.y_nm - self.disp_origin[1])
        return pv.StructuredGrid(
            xx, yy, np.full(xx.shape, self._mirror_z(depth)))

    @staticmethod
    def _empty_plane_mesh() -> pv.PolyData:
        mesh = pv.PolyData(np.zeros((1, 3)))
        mesh.point_data['value'] = np.zeros(1)
        return mesh

    # ------------------------------------------------------------- visibility
    def _cloud_frame_cut(self):
        """World Z of the current depth in the cloud view, or None.

        The cloud view must stay clean above the current depth (P3, W2), so the
        wireframe box and the depth labels stop at the same plane that clips
        the point cloud.  Slice view and Raw mode keep the whole frame.
        """
        if self.view_mode == 'raw' or self.display_mode != 'cloud':
            return None
        if not self.current_range.valid or self.current_depth is None:
            return None
        return (float(self.disp_origin[2]) - float(self.current_depth)) * self.z_scale

    def _apply_axes_clip(self) -> None:
        cut = self._cloud_frame_cut()
        for axes in getattr(self, 'axes_actors', {}).values():
            axes.set_depth_cut(cut)
            # Depth-tick visibility is decided inside update(); offscreen
            # renders do not fire the observers that normally refresh it, so
            # refresh explicitly or the labels above the cut stay drawn.
            if cut is not None:
                axes.update()

    def _apply_visibility(self, render: bool = False):
        raw = self.view_mode == 'raw'
        slice_valid = self.current_range.valid and self.current_depth is not None
        cloud_mode = self.display_mode == 'cloud'
        resolved = self.resolved_reference_mode
        # P3: the grey reference layers belong to the slice view only.  In the
        # cloud view the volume above the current depth has to stay clean, and
        # those grey surfaces are exactly the occlusion the user asked to
        # remove.  The Auto / Layers / Envelope pills stay clickable; they just
        # have no visible effect while the cloud is showing.
        reference_on = (not raw) and not cloud_mode
        auto_layers = (reference_on and self.reference_mode == 'auto'
                       and resolved == 'layers')
        layers_on = reference_on and resolved == 'layers' and not auto_layers
        envelope_on = reference_on and resolved == 'envelope'
        cloud_on = (not raw) and cloud_mode and slice_valid
        # P3 (W1): the interpolated slice face is drawn in both views now; in
        # cloud mode it is the bright face the design puts inside the cloud.
        # At the very top/bottom of the cloud the face would coincide with the
        # volume's outer face, so it is hidden there (P4 feedback).
        slice_on = (not raw) and slice_valid and not self._slice_at_cloud_edge()
        for metric in self.metrics:
            actors = self.reference_actors[metric]
            subset = (self._auto_layer_subset(len(actors)) if auto_layers
                      else None)
            for index, actor in enumerate(actors):
                if layers_on:
                    actor.SetVisibility(True)
                elif subset is not None:
                    actor.SetVisibility(index in subset)
                else:
                    actor.SetVisibility(False)
            self.envelope_actors[metric].SetVisibility(envelope_on and not raw)
            self.envelope_edge_actors[metric].SetVisibility(envelope_on and not raw)
            self.slice_actors[metric].SetVisibility(slice_on)
            cloud = self.cloud_actors.get(metric)
            if cloud is not None:
                cloud.SetVisibility(cloud_on)
            fog = self.cloud_fog_actors.get(metric)
            if fog is not None:
                fog.SetVisibility(cloud_on and bool(self.cloud_fog_enabled))
            self.point_actors[metric].SetVisibility(raw)
            self.locate_markers[metric].SetVisibility(False)
            self.locate_halos[metric].SetVisibility(False)
            if self.slider is not None:
                enabled = not raw and slice_valid
                self.slider.SetEnabled(enabled)
                if enabled:
                    # SetEnabled(True) rebinds the widget to the last poked
                    # renderer (a plot); force it back onto the control band.
                    self._bind_slider_widget()
        # P3 (W2): in the cloud view nothing may be drawn above the current
        # depth, the box frame and its depth labels included.
        self._apply_axes_clip()
        self._hide_locate_rings()
        self.locate_occluded = None
        for actor in self.tick_label_actors.values():
            actor.SetVisibility(not raw)
        for actor in self.tick_line_actors:
            actor.SetVisibility(not raw)
        for actor in self.hover_actors.values():
            actor.SetVisibility(False)
        self._hover_text = ''
        self._hover_metric = None
        self._last_selection = None
        # The haze only exists in the cloud view: grey it out elsewhere instead
        # of letting a click flip an invisible option.
        widgets = getattr(self, 'widgets', None)
        if widgets is not None and widgets.get('display.fog') is not None:
            widgets.set_checked('display.fog', bool(self.cloud_fog_enabled))
            widgets.set_disabled('display.fog', not cloud_on)
        self._sync_points_only_widgets(widgets)
        self._refresh_buttons()
        if render:
            self.plotter.render()

    #: View/depth/reference controls that need the interpolated field.
    _FIELD_ONLY_WIDGETS = (
        'view.normal', 'view.locate', 'display.slice',
        'reference.auto', 'reference.layers', 'reference.envelope',
        'range.common', 'range.actual', 'depth.value', 'depth.slider',
        'interp.linear', 'interp.cubic', 'interp.tps', 'export.field')

    def _slice_at_cloud_edge(self) -> bool:
        """The cloud slice sits exactly at the data's top or bottom."""
        if self.display_mode != 'cloud':
            return False
        if not self.current_range.valid or self.current_depth is None:
            return False
        lo = float(self.current_range.lo_nm)
        hi = float(self.current_range.hi_nm)
        if not hi > lo:
            return False
        tolerance = (hi - lo) * 1e-6
        depth = float(self.current_depth)
        return depth <= lo + tolerance or depth >= hi - tolerance

    def _sync_points_only_widgets(self, widgets) -> None:
        """Grey out the controls a points-only result cannot honour."""
        if widgets is None:
            return
        for widget_id in self._FIELD_ONLY_WIDGETS:
            if widgets.get(widget_id) is not None:
                widgets.set_disabled(widget_id, self.points_only)

    def _bind_slider_widget(self):
        """Keep the slider representation anchored to the top control band.

        PyVista/VTK re-bind a slider widget and its representation to the
        interactor's current renderer when the widget is re-enabled after a
        disable.  Move the representation prop back to the control renderer,
        set both renderer references and rebuild the representation so the
        tube is drawn in the control viewport again.
        """
        if self.slider is None:
            return
        rep = self.slider.GetRepresentation()
        control = self.control_renderer
        for renderer in self.plotter.renderers:
            if renderer is not control and renderer.HasViewProp(rep):
                renderer.RemoveViewProp(rep)
        if not control.HasViewProp(rep):
            control.AddViewProp(rep)
        self.slider.SetCurrentRenderer(control)
        rep.SetRenderer(control)
        rep.BuildRepresentation()

    def _refresh_status(self, render: bool = True):
        width = self._window_width()
        compact = width < 1000
        if self.current_range.valid and self.current_depth is not None:
            nz, ny, nx = self.field.shape
            kind_key = ('synthetic' if self.dataset.kind == 'synthetic'
                        else 'transformed' if self.dataset.kind == 'transformed'
                        else 'measured')
            kind = self._t(f'kind.{kind_key}')
            short_kind = self._t(f'kind.short_{kind_key}')
            er0, er1 = self.fixed_ranges['Er']
            h0, h1 = self.fixed_ranges['H']
            if self.view_mode != 'raw' and 'Er' in self.current_planes:
                plane = self.current_planes['Er']
                n_valid = int(plane.mask.sum())
                n_blank = int((~plane.mask).sum())
            else:
                n_valid = n_blank = 0
            if compact:
                line1 = self._t(
                    'status.exact_compact', depth=self.current_depth,
                    valid=n_valid, nx=nx, ny=ny, dx=self.field.dx_nm)
                line2 = self._t(
                    'status.range_compact',
                    mode=self._mode_label(self.range_mode),
                    lo=self.current_range.lo_nm, hi=self.current_range.hi_nm,
                    view=self._mode_label(self.view_mode), kind=short_kind,
                    er0=er0, er1=er1, h0=h0, h1=h1)
            else:
                line1 = self._t(
                    'status.exact_full', depth=self.current_depth,
                    valid=n_valid, blank=n_blank, nx=nx, ny=ny, nz=nz,
                    dx=self.field.dx_nm, dy=self.field.dy_nm,
                    dz=self.field.dz_nm)
                line2 = self._t(
                    'status.range_full',
                    mode=self._mode_label(self.range_mode),
                    lo=self.current_range.lo_nm, hi=self.current_range.hi_nm,
                    view=self._mode_label(self.view_mode), kind=kind,
                    er0=er0, er1=er1, h0=h0, h1=h1)
            if self.view_mode == 'raw':
                line1 = self._t(
                    'status.raw1', points=self.dataset.point_count,
                    groups=self.dataset.group_count)
                line2 = self._t(
                    'status.raw2', z0=self.dataset.z_bounds_nm[0],
                    z1=self.dataset.z_bounds_nm[1], kind=short_kind)
            info = self._info_line(include_notice=False)
            lines = [line1, line2] + (
                [info] if info and not self._notice else [])
            self.status_actor.SetInput(self._band_text(chr(10).join(
                self._fit_text_to(line, .012, .990, 8) for line in lines)))
        else:
            info = self._info_line(include_notice=False)
            lines = [self._t(
                'status.unavailable', mode=self._mode_label(self.range_mode),
                note=self.current_range.note)]
            if info and not self._notice:
                lines.append(info)
            self.status_actor.SetInput(self._band_text(chr(10).join(
                self._fit_text_to(line, .012, .990, 8) for line in lines)))
        self._refresh_notice()
        self._refresh_summary()
        self._render_skin(render=False)
        if render:
            self.plotter.render()

    def _refresh_notice(self) -> None:
        """Show the notice on its own band row, never as a clipped 3rd line."""
        actor = getattr(self, 'notice_actor', None)
        if actor is None:
            return
        text = self._notice
        actor.SetVisibility(bool(text))
        actor.SetInput(
            self._band_text(self._fit_text_to(text, .012, .990, 8))
            if text else '')

    def _info_line(self, include_notice: bool = True) -> str:
        parts = []
        timings = self.timings
        if timings is not None:
            def fmt(value):
                return '--' if value is None else f'{float(value):.2f}s'
            if self._window_width() < 1000:
                parts.append(self._t(
                    'info.cache_short' if self.cache_hit else 'info.computed_short',
                    seconds=fmt(timings.compute_s)))
                parts.append(self._t('info.ref', ref=self._reference_short()))
            else:
                parts.append(self._t(
                    'info.cache_full' if self.cache_hit else 'info.computed_full'))
                parts.append(self._t(
                    'info.timings', read=fmt(timings.read_s),
                    compute=fmt(timings.compute_s), render=fmt(timings.render_s)))
                if timings.slow_compute and not getattr(self, '_save_done', False):
                    parts.append(self._t('info.save_hint'))
        if self.cache_hit:
            parts.append(self._t('info.source_saved'))
        if include_notice and self._notice:
            parts.append(self._notice)
        return ' | '.join(parts)

    def _slice_summary(self) -> str:
        if self.current_depth is None:
            return self._t('slice.none')
        n_valid = 0
        if 'Er' in self.current_planes:
            plane = self.current_planes['Er']
            n_valid = int(plane.mask.sum())
        blank = (int((~self.current_planes["Er"].mask).sum())
                 if "Er" in self.current_planes else 0)
        return self._t('slice.summary', depth=self.current_depth,
                       valid=n_valid, blank=blank)

    def _reference_short(self) -> str:
        """Compact reference-mode token for the narrow status line."""
        n = len(self.layers.depths_nm)
        key = ('reference.auto_layers' if self.reference_mode == 'auto'
               else 'reference.manual_layers')
        if self.resolved_reference_mode == 'layers':
            return self._t(key, n=n)
        return self._t('reference.auto_envelope'
                       if self.reference_mode == 'auto'
                       else 'reference.manual_envelope')

    def _refresh_summary(self):
        n = len(self.layers.depths_nm)
        prefix = self._t('prefix.auto' if self.reference_mode == 'auto'
                         else 'prefix.manual')
        resolved = (self._t('resolved.layers', n=n)
                    if self.resolved_reference_mode == 'layers'
                    else self._t('resolved.envelope', n=n))
        if self._window_width() < 1000:
            # At 900 px the row-2 cell would sit under open/export.  The
            # reference mode is redundant there, so it moves to the existing
            # status block instead of squeezing a second control-band line.
            self.summary_actor.SetInput('')
            return
        if self.current_range.valid:
            # The numeric range is already on the status line; this cell only
            # reports the resolved reference mode and is clipped to the space
            # between the last button and the file-button column.
            text = self._t('summary.mode', prefix=prefix, resolved=resolved)
        else:
            text = self._t('summary.range_note',
                           mode=self._mode_label(self.range_mode),
                           note=self.current_range.note)
        self.summary_actor.SetInput(self._band_text(
            self._fit_text_to(text, .632, .862, 7)))

    def _fit_text_to(self, text: str, x: float, max_x: float,
                     font_size: int) -> str:
        """Return ``text`` clipped with an ellipsis so it ends before ``max_x``.

        Measured with the same pixel estimate used for the button boxes, so a
        long range/export notice can never run past the window edge.
        """
        width = self._window_width()
        if width <= 0:
            return text
        max_px = max(16.0, (max_x - x) * width)
        if self._text_width_px(text, font_size) <= max_px:
            return text
        # Keep both ends: the start identifies the action, the tail carries the
        # actual run-folder name.  A tail-only clip would hide that name.
        ellipsis = '...'
        min_tail = max(1, min(32, len(text) // 3))
        unit = font_size * 1.28
        prefix = [0.0]
        for ch in text:
            prefix.append(prefix[-1] + (1.0 if ord(ch) < 128 else 1.7))
        # Width of n units is (units * unit + 2); the ellipsis costs 3 units.
        budget_units = (max_px - 2.0) / unit - 3.0
        for total in range(len(text) - 1, 1, -1):
            tail = min(max(min_tail, total // 3), total - 1)
            head = total - tail
            used = prefix[head] + (prefix[-1] - prefix[-1 - tail])
            if used <= budget_units:
                return text[:head].rstrip() + ellipsis + text[-tail:]
        clipped = text[-min_tail:]
        return ellipsis + clipped if clipped else ''

    # ---------------------------------------------------------------- ticking
    def _tick_depths(self):
        if not self.current_range.valid:
            return []
        lo, hi = self.current_range.lo_nm, self.current_range.hi_nm
        return [float(d) for d in self.layers.depths_nm
                if lo - 1e-9 <= d <= hi + 1e-9]

    def _tick_fraction(self, depth: float) -> float:
        lo, hi = self.current_range.lo_nm, self.current_range.hi_nm
        return (depth - lo) / (hi - lo) if hi > lo else 0.0

    @staticmethod
    def _text_width_px(text: str, font: int) -> float:
        # CJK glyphs are roughly 1.7x an ASCII advance in the VTK bitmap fonts;
        # the previous len()-based estimate let Chinese button text overflow.
        units = sum(1.0 if ord(ch) < 128 else 1.7 for ch in str(text))
        return units * font * 1.28 + 2.0

    def _select_major_indices(self, width, count):
        track_px = max(1., (self.TRACK_X1 - self.TRACK_X0) * width)
        capacity = max(2, min(count, int(track_px // 110))) if count else 0
        if count <= capacity:
            indices = list(range(count))
        else:
            indices = sorted({int(round(v)) for v in np.linspace(0, count - 1, capacity)})
        return indices, capacity

    def _add_line_actor(self, segments, color, line_width):
        points = vtk.vtkPoints()
        cells = vtk.vtkCellArray()
        for x, y0, y1 in segments:
            i0 = points.InsertNextPoint(float(x), float(y0), 0.)
            i1 = points.InsertNextPoint(float(x), float(y1), 0.)
            cells.InsertNextCell(2)
            cells.InsertCellPoint(i0)
            cells.InsertCellPoint(i1)
        poly = vtk.vtkPolyData()
        poly.SetPoints(points)
        poly.SetLines(cells)
        mapper = vtk.vtkPolyDataMapper2D()
        coordinate = vtk.vtkCoordinate()
        coordinate.SetCoordinateSystemToNormalizedViewport()
        mapper.SetTransformCoordinate(coordinate)
        mapper.SetInputData(poly)
        actor = vtk.vtkActor2D()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(*pv.Color(color).float_rgb)
        actor.GetProperty().SetLineWidth(line_width)
        actor.PickableOff()
        self.control_renderer.AddActor(actor)
        return actor

    def _refresh_ticks(self, render: bool = False):
        width = self._window_width()
        _win_w, height = self._window_size()
        self._tick_width = width
        for actor in self.tick_label_actors.values():
            self.control_renderer.remove_actor(actor, render=False)
        self.tick_label_actors.clear()
        for actor in self.tick_line_actors:
            self.control_renderer.RemoveActor(actor)
        self.tick_line_actors.clear()
        self.tick_label_rows.clear()
        self._major_indices = []
        depths = self._tick_depths()
        if not depths:
            return
        self.plotter.renderers._active_index = self._control_index()
        indices, capacity = self._select_major_indices(width, len(depths))
        font = 7 if (self.TRACK_X1 - self.TRACK_X0) * width / max(1, len(indices) - 1) >= 72 else 6
        self._tick_font_size = font
        xs = [self.TRACK_X0 + self._tick_fraction(d) * (self.TRACK_X1 - self.TRACK_X0)
              for d in depths]
        widths = [self._text_width_px(f'{d:.1f} nm', font) for d in depths]
        if self.current_range.valid:
            header_texts = (
                self._t('depth.heff', depth=self.current_range.lo_nm),
                self._t('depth.heff', depth=self.current_range.hi_nm))
        else:
            header_texts = (self._t('depth.none'),)
        header_right = .012 * width + max(
            self._text_width_px(t, self._value_font_size())
            for t in header_texts) + 6.
        row_boxes = {'above': [], 'below': []}

        # Button boxes (and the static group titles in the same row) are real
        # glyph-exclusion zones: a major label must never be drawn on top of
        # them.  The actual tick depths/X positions are untouched; a conflicting
        # major label simply falls back to the other row or becomes minor.
        label_h = font * 2.1
        band_rects = []
        for info in self.buttons.values():
            box = info.get('box')
            if box:
                band_rects.append((box[0] * width, box[2] * height,
                                   box[1] * width, box[3] * height))
        for title, title_x in ((self._t('label.range'), .012),
                               (self._t('label.grey'), .262)):
            title_w = self._text_width_px(title, 7)
            title_x0 = title_x * width
            band_rects.append((title_x0, self.ROW2 * height,
                               title_x0 + title_w,
                               (self.ROW2 + 7 * 2.0 / height) * height))
        row_y = {'above': self.TICK_ABOVE_Y, 'below': self.TICK_BELOW_Y}

        def collides(x0, w_px, row):
            y0 = row_y[row] * height
            y1 = y0 + label_h
            for bx0, by0, bx1, by1 in band_rects:
                if not (x0 + w_px + 1. <= bx0 or bx1 + 1. <= x0
                        or y1 + 1. <= by0 or by1 + 1. <= y0):
                    return True
            return False

        def place(x_px, w_px, row):
            x0 = min(max(x_px - w_px / 2., 4.), width - 4. - w_px)
            if x0 < 4.:
                return None
            if row == 'above' and x0 <= header_right + 6.:
                return None
            for bx0, bx1 in row_boxes[row]:
                if not (x0 + w_px + 6. <= bx0 or bx1 + 6. <= x0):
                    return None
            if collides(x0, w_px, row):
                return None
            return x0

        labeled = {}
        for order, index in enumerate(indices):
            preferred = 'below' if order % 2 == 0 else 'above'
            for row in (preferred, 'above' if preferred == 'below' else 'below'):
                x0 = place(xs[index] * width, widths[index], row)
                if x0 is not None:
                    labeled[index] = (row, x0)
                    row_boxes[row].append((x0, x0 + widths[index]))
                    break
        for endpoint, row in ((indices[0], 'below'), (indices[-1], 'above')):
            if endpoint in labeled:
                continue
            w_px = widths[endpoint]
            x0 = min(max(xs[endpoint] * width - w_px / 2., 4.), width - 4. - w_px)
            if x0 < 4.:
                continue
            if collides(x0, w_px, row):
                other = 'below' if row == 'above' else 'above'
                if collides(x0, w_px, other):
                    continue
                row = other
            for other in [j for j, (r, box) in labeled.items()
                          if r == row and not (x0 + w_px + 6. <= box[0] or box[1] + 6. <= x0)]:
                del labeled[other]
            row_boxes[row] = [(box[0], box[1]) for j, (r, box) in labeled.items() if r == row]
            labeled[endpoint] = (row, x0)
            row_boxes[row].append((x0, x0 + w_px))

        major_lines, minor_lines = [], []
        for index, depth in enumerate(depths):
            x = xs[index]
            if index in labeled:
                row, x0 = labeled[index]
                self.tick_label_rows[index] = row
                if row == 'below':
                    major_lines.append((x, self._band_y(self.SLIDER_Y),
                                        self._band_y(self.MAJOR_BELOW_END)))
                    centre = self.TICK_BELOW_Y
                else:
                    major_lines.append((x, self._band_y(self.MAJOR_ABOVE_END),
                                        self._band_y(self.SLIDER_Y)))
                    centre = self.TICK_ABOVE_Y
                name = f'depth_tick_{index}'
                label = self.plotter.add_text(
                    f'{depth:.1f} nm',
                    position=(x, self._band_y(centre)),
                    viewport=True, font_size=font, color='#334155', name=name,
                    render=False, **self._font_kwargs())
                # Centred on the real tick X (the collision boxes above were
                # still computed from the full label width).
                label.GetTextProperty().SetJustificationToCentered()
                self.tick_label_actors[name] = label
            else:
                minor_lines.append((x, self._band_y(self.SLIDER_Y),
                                    self._band_y(self.MINOR_END)))
        self._major_indices = sorted(labeled)
        if major_lines:
            self.tick_line_actors.append(self._add_line_actor(major_lines, '#64748b', 1.2))
        if minor_lines:
            self.tick_line_actors.append(self._add_line_actor(minor_lines, '#cbd5e1', 1.0))
        if self.view_mode == 'raw':
            for actor in self.tick_label_actors.values():
                actor.SetVisibility(False)
            for actor in self.tick_line_actors:
                actor.SetVisibility(False)
        if render:
            self.plotter.render()

    def _control_index(self):
        for index in range(len(self.plotter.renderers)):
            if self.plotter.renderers[index] is self.control_renderer:
                return index
        return len(self.plotter.renderers) - 1

    def refresh_layout(self, render=False):
        # Button geometry is the tick-label exclusion source, so it must be
        # refreshed for the *current* window size before tick labels are placed.
        self._layout_buttons()
        self._refresh_ticks(render=False)
        font_size = self._value_font_size()
        if self.depth_value_actor.GetTextProperty().GetFontSize() != font_size:
            self.depth_value_actor.GetTextProperty().SetFontSize(font_size)
        wide = self._window_width() >= 820
        for name in ('row1_view', 'row1_hint', 'row2_range', 'row2_grey'):
            actor = self.control_renderer.actors.get(name)
            if actor is not None:
                actor.SetVisibility(wide)
        self.summary_actor.SetVisibility(wide)
        if self.slider is not None and self.slider.GetEnabled():
            self._bind_slider_widget()
        if render:
            self.plotter.render()

    def _window_handle(self) -> int:
        """Native window handle of the VTK window (0 before it is realised)."""
        return theme.native_handle(self.plotter.render_window)

    def _minimum_window_size(self):
        """Interactive resize floor in device pixels (None when off-screen)."""
        if self.off_screen:
            return None
        return theme.window_floor(
            theme.dpi_scale_for_window(self.plotter.render_window))

    def _on_first_render(self, *_args) -> None:
        """Install the shell floor as soon as the native window exists."""
        if not self.off_screen:
            self._install_window_policy()
        done = self.off_screen or (self._window_policy is not None
                                   and self._window_policy.installed)
        if not done:
            return
        observer_id = getattr(self, '_render_observer_id', None)
        if observer_id is None:
            return
        try:
            self.plotter.render_window.RemoveObserver(observer_id)
        finally:
            self._render_observer_id = None
        # The window manager can hand back a client size that differs from the
        # size requested at construction (maximise, work-area clamp, DPI
        # change).  The skin is a 1:1 bitmap anchored at the bottom-left, so a
        # stale size would crop the toolbar instead of scaling it.  Rebuild
        # once the real framebuffer size is known.
        width, height = self._window_size()
        if (getattr(self, '_skin_ready', False)
                and (int(width), int(height)) != tuple(self._skin_size or ())):
            self._rebuild_skin(int(width), int(height), supersample=1)
            self._refresh_axes_scale()
            if not self._camera_customized:
                self.fit_views(render=False)
            self.plotter.render()

    def _install_window_policy(self) -> None:
        """Make the shell itself refuse sizes below the layout floor.

        Without this hook the frame can be dragged smaller than the painted
        skin: the layout follows the new size while the skin keeps the old one,
        which makes every pointer pick land away from what is on screen.
        """
        if self.off_screen:
            return
        if self._window_policy is not None and self._window_policy.installed:
            return
        handle = self._window_handle()
        if handle <= 0:
            return
        self._window_policy = window_policy.MinimumSize(
            handle, self._minimum_window_size)

    def _on_configure(self, *_args):
        try:
            width, height = self._window_size()
        except Exception:
            return
        self._install_window_policy()
        floor = self._minimum_window_size()
        if floor is not None and (width < floor[0] or height < floor[1]):
            # The shell enforces this through WM_GETMINMAXINFO; a programmatic
            # resize can still slip through, so put the frame back on the floor
            # and let the resulting ConfigureEvent rebuild the skin.
            window_policy.resize_client(self._window_handle(), floor)
            return
        if (getattr(self, '_skin_ready', False)
                and (int(width), int(height)) != tuple(self._skin_size or ())):
            # A live resize fires this per frame: repaint the skin at 1x now and
            # let _refine_skin supersample it once the drag settles.
            self._rebuild_skin(int(width), int(height), supersample=1)
            self._arm_skin_refine()
            self._refresh_axes_scale()
            if not self._camera_customized:
                self.fit_views(render=False)
            self.plotter.render()
        if width == self._tick_width:
            return
        self.refresh_layout(render=False)
        if not getattr(self, '_ready', False):
            return
        # A resize changes the camera aspect; re-fit the pristine startup view so
        # the full data box, edge spheres and axis labels stay in the viewport.
        # A view the user has rotated/zoomed is deliberately not overridden.
        if not self._camera_customized:
            self.fit_views(render=False)
            self.plotter.render()

    def _on_camera_interaction(self, *_args):
        self._camera_customized = True

    # ------------------------------------------------------------------ modes
    def set_slice_axis(self, axis: str) -> None:
        """Reserved XY-slice entry point; this version only exposes Z.

        The field/slice machinery already supports X and Y; only the UI is
        reserved.  Reject unknown axes loudly, then report that the UI switch
        is not implemented instead of silently slicing along Z.
        """
        key = str(axis).strip().lower()
        if key not in ('x', 'y', 'z'):
            raise ValueError(f'Unknown slice axis: {axis!r}')
        raise NotImplementedError('XY slice UI is reserved')

    def set_view_mode(self, mode: str, render: bool = True):
        mode = str(mode).strip().lower()
        if mode not in VIEW_MODES:
            raise ValueError(f'Unknown view mode: {mode!r}')
        if self.points_only and mode != 'raw':
            # No interpolated field exists, so normal/locate would draw an
            # empty scene; explain instead of silently doing nothing.
            self.set_notice(self._t('notice.points_only'))
            return
        self.view_mode = mode
        self._hover_text = ''
        self._hover_metric = None
        for actor in self.hover_actors.values():
            actor.SetVisibility(False)
        self._apply_visibility()
        self._refresh_depth_value()
        self._refresh_help_text()
        self._refresh_status(render=False)
        if render:
            self.plotter.render()

    def set_reference_mode(self, mode: str, render: bool = True):
        aliases = {'auto': 'auto', 'automatic': 'auto', 'layers': 'layers',
                   'layer': 'layers', 'per-layer': 'layers',
                   'envelope': 'envelope', 'volume': 'envelope'}
        key = aliases.get(str(mode).strip().lower())
        if key is None:
            raise ValueError(f'Unknown reference mode: {mode!r}')
        self.reference_mode = key
        self.resolved_reference_mode = self._resolve_reference_mode()
        self._apply_visibility()
        self._refresh_status(render=False)
        if render:
            self.plotter.render()

    def set_interpolation(self, mode: str, render: bool = True):
        """Display-only slice interpolation: linear / cubic / tps."""
        key = str(mode).strip().lower()
        if key not in field3d.SLICE_INTERPOLATION_MODES:
            raise ValueError(f'Unknown slice interpolation: {mode!r}')
        self.interpolation = key
        if self.points_only:
            self.set_notice(self._t('notice.points_only'))
            return
        self._update_slice(render=False)
        self._refresh_status(render=False)
        if render:
            self.plotter.render()

    def set_range_mode(self, mode: str, render: bool = True):
        mode = str(mode).strip().lower()
        if mode not in ('common', 'actual'):
            raise ValueError(f'Unknown depth range mode: {mode!r}')
        self.range_mode = mode
        self.current_range = self.range_modes[mode]
        if self.slider is not None:
            if self.current_range.valid:
                rep = self.slider.GetRepresentation()
                rep.SetMinimumValue(float(self.current_range.lo_nm))
                rep.SetMaximumValue(float(self.current_range.hi_nm))
                depth = float(np.clip(self.current_depth if self.current_depth is not None
                                      else self.current_range.hi_nm,
                                      self.current_range.lo_nm,
                                      self.current_range.hi_nm))
                self.current_depth = depth
                rep.SetValue(depth)
                self._refresh_ticks(render=False)
            else:
                self.slider.SetEnabled(False)
                self.current_depth = None
        elif self.current_range.valid:
            # no slider existed (previous range invalid): establish the depth
            # BEFORE building the widget, so the slider never sees None.
            self.current_depth = float(np.clip(
                self.current_depth if self.current_depth is not None
                else self.current_range.hi_nm,
                self.current_range.lo_nm, self.current_range.hi_nm))
            self._build_slider()
            if self.slider is not None:
                self.slider.GetRepresentation().SetValue(self.current_depth)
        if self.current_range.valid:
            self.current_depth = float(np.clip(
                self.current_depth if self.current_depth is not None else self.current_range.hi_nm,
                self.current_range.lo_nm, self.current_range.hi_nm))
            self._update_slice(render=False)
        self._refresh_depth_value()
        self._apply_visibility()
        self._refresh_status(render=False)
        if render:
            self.plotter.render()

    def set_depth(self, depth: float, render: bool = True):
        if not self.current_range.valid:
            return
        depth = float(np.clip(float(depth), self.current_range.lo_nm,
                              self.current_range.hi_nm))
        self.current_depth = depth
        self._refresh_depth_value()
        self._update_slice(render=False)
        # _apply_visibility ends in _refresh_buttons(), which refreshes the
        # status line (depth included) and the skin; a second full skin redraw
        # here doubled the cost of every slider step (~10 ms each).
        self._apply_visibility()
        if self.slider is not None:
            rep = self.slider.GetRepresentation()
            if abs(float(rep.GetValue()) - depth) > 1e-9:
                rep.SetValue(depth)
        if self.view_mode == 'locate':
            self._clear_hover(render=False)
        if render:
            self.plotter.render()

    def _on_slide(self, value):
        if not getattr(self, '_ready', False):
            return
        self.set_depth(float(value), render=True)

    # ------------------------------------------------------------------ slice
    def _plane_mesh(self, plane: field3d.Plane) -> pv.PolyData | None:
        if plane.axis != 'z':
            raise ValueError('UI slice assembly only supports Z this round')
        mask = plane.mask
        if mask.size == 0:
            return None
        nv, nu = mask.shape
        uu, vv = np.meshgrid(plane.u_nm, plane.v_nm)
        points = np.empty((nv * nu, 3), dtype=np.float64)
        points[:, 0] = uu.ravel() - self.disp_origin[0]
        points[:, 1] = vv.ravel() - self.disp_origin[1]
        points[:, 2] = self._mirror_z(plane.position_nm)
        ids = np.arange(nv * nu).reshape(nv, nu)
        cells = (mask[:-1, :-1] & mask[:-1, 1:] & mask[1:, 1:] & mask[1:, :-1])
        quad_ids = np.stack([ids[:-1, :-1], ids[:-1, 1:],
                             ids[1:, 1:], ids[1:, :-1]], axis=-1)[cells]
        if quad_ids.size == 0:
            return None
        faces = np.empty((quad_ids.shape[0], 5), dtype=np.int64)
        faces[:, 0] = 4
        faces[:, 1:] = quad_ids
        mesh = pv.PolyData(points, faces.ravel())
        mesh.point_data['value'] = plane.values.ravel(order='C')
        return mesh

    def _update_slice(self, render: bool = True):
        if self.points_only or not self.current_range.valid \
                or self.current_depth is None:
            self.current_planes.clear()
            for metric in self.metrics:
                self.slice_actors[metric].SetVisibility(False)
            return
        planes = None
        display_field = self._slice_display_field()
        if self.interpolation == 'tps':
            planes = field3d.slice_planes_tps(
                self.dataset, display_field, self.current_depth)
            if planes is None:
                planes = field3d.slice_planes_cubic(
                    self.dataset, display_field, self.current_depth)
        elif self.interpolation == 'cubic':
            planes = field3d.slice_planes_cubic(
                self.dataset, display_field, self.current_depth)
        if planes is None:
            planes = {metric: field3d.slice_metric(
                          self.field, metric, 'z', self.current_depth)
                      for metric in self.metrics}
        self.current_planes = planes
        mask = planes['Er'].mask
        same_topology = (self._plane_mask is not None
                         and self._plane_mask.shape == mask.shape
                         and np.array_equal(self._plane_mask, mask))
        for metric in self.metrics:
            plane = planes[metric]
            if same_topology and self._plane_meshes[metric] is not None:
                mesh = self._plane_meshes[metric]
                mesh.points[:, 2] = self._mirror_z(plane.position_nm)
                mesh.point_data['value'][:] = plane.values.ravel(order='C')
                mesh.GetPoints().Modified()
                mesh.GetPointData().GetArray('value').Modified()
            else:
                mesh = self._plane_mesh(plane)
                self._plane_meshes[metric] = mesh
                if mesh is not None:
                    self.slice_actors[metric].GetMapper().SetInputData(mesh)
            visible = mesh is not None
            # P3 (W1): the bright slice face is drawn in the cloud view too;
            # _apply_visibility owns the per-mode rules, this only says whether
            # there is a mesh to draw at all.
            self.slice_actors[metric].SetVisibility(
                visible and self.view_mode != 'raw'
                and not self._slice_at_cloud_edge())
        if self.display_mode == 'cloud':
            self._apply_cloud_clip()
        if not same_topology:
            self._plane_mask = mask.copy()
            self._plane_mask.setflags(write=False)
        if render:
            self.plotter.render()

    # ------------------------------------------------------------------ hover
    def _renderer_at_display(self, x_px, y_px):
        width, height = self._window_size()
        if width <= 0 or height <= 0:
            return None
        x = x_px / width
        y = y_px / height
        for metric in self.metrics:
            viewport = self.plot_renderers[metric].GetViewport()
            if viewport[0] <= x <= viewport[2] and viewport[1] <= y <= viewport[3]:
                return metric, self.plot_renderers[metric]
        return None

    def display_for_world(self, metric: str, x_nm: float, y_nm: float,
                          depth_nm: float | None = None) -> tuple[float, float]:
        """Display pixels of a physical world point (used by tests and hit math)."""
        depth = self.current_depth if depth_nm is None else depth_nm
        renderer = self.plot_renderers[metric]
        world = self._disp_point(x_nm, y_nm, depth)
        renderer.SetWorldPoint(world[0], world[1], world[2], 1.0)
        renderer.WorldToDisplay()
        display = renderer.GetDisplayPoint()
        return float(display[0]), float(display[1])

    def hover_from_display(self, x_px: float, y_px: float):
        """Map real display pixels -> slice world XY -> nearest real XY group.

        Returns None when the pointer is off the slice plane, outside the data
        extent, or over a hole (no 3D support).  No fabricated hit.
        """
        if self.view_mode != 'locate' or self.current_depth is None:
            return None
        found = self._renderer_at_display(x_px, y_px)
        if found is None:
            return None
        metric, renderer = found
        plane_world_z = self._mirror_z(self.current_depth) * self.z_scale
        renderer.SetDisplayPoint(float(x_px), float(y_px), 0.0)
        renderer.DisplayToWorld()
        near = np.asarray(renderer.GetWorldPoint()[:3], dtype=np.float64)
        renderer.SetDisplayPoint(float(x_px), float(y_px), 1.0)
        renderer.DisplayToWorld()
        far = np.asarray(renderer.GetWorldPoint()[:3], dtype=np.float64)
        direction = far - near
        if abs(direction[2]) < 1e-12:
            return None
        t = (plane_world_z - near[2]) / direction[2]
        if t < -1e-6 or t > 1.0 + 1e-6:
            return None
        world = near + np.clip(t, 0.0, 1.0) * direction
        x_nm, y_nm = self._phys_xy(world[0], world[1])
        # Round to the nearest real grid cell instead of rejecting anything
        # outside the exact float bounds: a pointer pixel on the outer rim of
        # the slice still belongs to the edge cell (the old strict bounds test
        # created a dead pixel strip along the outline).  Off-plane points are
        # still rejected below by the index range and the support mask.
        i = int(round((x_nm - self.field.x0_nm) / self.field.dx_nm))
        j = int(round((y_nm - self.field.y0_nm) / self.field.dy_nm))
        if not (0 <= i < self.field.nx and 0 <= j < self.field.ny):
            return None
        plane = self.current_planes.get(metric)
        if plane is None or not bool(plane.mask[j, i]):
            return None
        selection = trial_selection.locate(
            self.dataset, x_nm, y_nm, float(self.current_depth))
        return {'metric': metric, 'group': selection.group,
                'x_nm': x_nm, 'y_nm': y_nm,
                'distance_nm': selection.distance_xy_nm,
                'point': selection.point, 'selection': selection,
                'delta_z_nm': selection.delta_z_nm}

    def source_label(self, group: dm.SourceGroup) -> str:
        files = group.source_files
        if len(files) == 1:
            return self._t('source.one', file=files[0])
        if len(files) == 2:
            return self._t('source.same_xy', first=files[0], second=files[1])
        return self._t('source.many', first=files[0], second=files[1],
                       extra=len(files) - 2)

    def locate_label(self, selection: trial_selection.Selection,
                     occluded: bool | None = None) -> str:
        """Source provenance + the real measured point, never a slice vertex."""
        point = selection.point
        lines = [
            self.source_label(selection.group),
            self._t('point.line', file=point.source_file, line=point.line_no,
                    row=point.row_index),
            self._t('point.xyz', x=point.x_nm, y=point.y_nm),
            self._t('point.depth', z=point.z_nm,
                    slice=selection.plane_depth_nm, dz=selection.delta_z_nm),
            self._t('point.values', er=point.er_gpa, h=point.h_gpa),
            self._t('point.group', x=selection.group.x_nm,
                    y=selection.group.y_nm, dist=selection.distance_xy_nm),
        ]
        if selection.has_same_xy_tie:
            lines.append(self._t('point.tie', files=' | '.join(
                selection.same_xy_files)))
        if occluded is not None:
            lines.append(self._t('hint.occluded' if occluded else 'hint.front'))
        return '\n'.join(self._wrap_hover_lines(lines))

    def _hover_plot_width_px(self) -> float:
        """Width in device pixels of one 3D card's plot area.

        The hover band is drawn inside a card renderer, so its text budget is
        that card's plot viewport - not half the window.  The old half-window
        budget dated from the pre-P1 layout where a plot really did own half the
        window; on the card layout it let a long source path run over the
        colour bar and get clipped by the card edge.
        """
        width = float(self._window_width())
        try:
            x0, _y0, x1, _y1 = self.layout.plot_viewport('Er')
            span = (float(x1) - float(x0)) * width
        except Exception:
            span = width / 2.0
        return max(120.0, span)

    @staticmethod
    def _ink_width_px(text: str, narrow_px: float, wide_px: float) -> float:
        """Rendered ink width of the text in pixels.

        Measured on this renderer at the hover font: one CJK glyph is about one
        em and one Latin glyph about 0.55 em, so a character count alone
        under-measures a Chinese line by roughly 1.6x.
        """
        total = 0.0
        for char in str(text):
            total += (wide_px if unicodedata.east_asian_width(char) in ('W', 'F')
                      else narrow_px)
        return total

    def _wrap_hover_text(self, text: str, budget_px: float, narrow_px: float,
                         wide_px: float) -> list[str]:
        """Greedy wrap measured in rendered pixels; long tokens are split.

        A long measured filename may be split exactly where the plot area ends
        (even mid-string): no character is dropped and the font size is not
        reduced.  At least one line is always returned so blank lines survive.
        """
        measure = lambda part: self._ink_width_px(part, narrow_px, wide_px)
        rows: list[str] = []
        row = ''
        for word in str(text).split(' '):
            candidate = word if not row else row + ' ' + word
            if measure(candidate) <= budget_px:
                row = candidate
                continue
            if row:
                rows.append(row)
                row = ''
            while measure(word) > budget_px:
                taken = ''
                for char in word:
                    if measure(taken + char) > budget_px:
                        break
                    taken += char
                if not taken:
                    taken = word[:1]
                rows.append(taken)
                word = word[len(taken):]
            row = word
        if row or not rows:
            rows.append(row)
        return rows

    def _wrap_hover_lines(self, lines):
        """Wrap provenance to the card's plot-area pixel budget.

        The band is drawn inside a card renderer, so the budget is that card's
        plot width: a wrapped line may never reach the colour bar or the card
        edge.
        """
        prop_px = 16.0
        try:
            prop_px = float(self.hover_actors['Er']
                            .GetTextProperty().GetFontSize())
        except Exception:
            pass
        narrow_px = max(3.0, prop_px * 0.55)
        wide_px = max(narrow_px, prop_px)
        budget_px = max(60.0, self._hover_plot_width_px() * 0.96 - 8.0)
        wrapped: list[str] = []
        for line in lines:
            wrapped.extend(self._wrap_hover_text(str(line), budget_px,
                                                 narrow_px, wide_px))
        return wrapped

    def _metric_rgba(self, metric: str, value: float):
        rgba = self.color_tables[metric].map_value(float(value))
        return float(rgba[0]), float(rgba[1]), float(rgba[2])

    def _marker_radius_nm(self, metric: str, point=None) -> float:
        """Locate-sphere radius: the RAW sphere radius of this metric.

        The located point is one of the measured points, so it is drawn at the
        same size the RAW view gives every measured point (0.25 x the median
        display-space nearest-neighbour distance, cached once per dataset).
        The marker used to be a fixed 1.6 px on screen, which read as a dot next
        to the 7-8 px RAW spheres (user: "定位模式下显示位置的小球太小了，使用和
        RAW一样大小的球").

        The camera-projection branch is now only the fallback for a dataset with
        no spacing statistic at all (degenerate cloud, radius_nm None), so a
        located point is never invisible.  It is computed at the marker itself
        from the camera's vertical field of view (or the parallel scale), which
        is independent of orientation, and capped to a quarter of the real XY
        extent, so a rotated view can never blow it up into a ball.
        """
        raw_radius = float(self.raw_sphere_radius_nm.get(metric) or 0.0)
        if raw_radius > 0.0:
            x0, x1, y0, y1, _z0, _z1 = self.display_bounds
            cap = 0.25 * min(x1 - x0, y1 - y0)
            return float(min(raw_radius, max(cap, 1e-9)))
        renderer = self.plot_renderers[metric]
        camera = renderer.GetActiveCamera()
        size = renderer.GetSize()
        height = float(size[1]) if size and size[1] > 0 else float(
            self._window_size()[1])
        height = max(height, 1.0)
        if point is not None:
            world = np.array(self._disp_point(point.x_nm, point.y_nm,
                                              point.z_nm))
        else:
            x0, x1, y0, y1, _z0, _z1 = self.display_bounds
            depth = (self.current_depth if self.current_depth is not None
                     else self.field.bounds_nm[5])
            world = np.array([(x0 + x1) / 2.0, (y0 + y1) / 2.0,
                              self._mirror_z(depth) * self.z_scale])
        if camera.GetParallelProjection():
            world_per_px = 2.0 * float(camera.GetParallelScale()) / height
        else:
            cam_pos = np.asarray(camera.GetPosition(), dtype=np.float64)
            focal = np.asarray(camera.GetFocalPoint(), dtype=np.float64)
            forward = focal - cam_pos
            norm = float(np.linalg.norm(forward))
            if norm <= 0.0:
                return 1e-9
            forward /= norm
            depth_along = float(np.dot(world - cam_pos, forward))
            if depth_along <= 0.0:
                depth_along = float(np.linalg.norm(world - cam_pos))
            view_angle = math.radians(float(camera.GetViewAngle()))
            world_per_px = (2.0 * depth_along * math.tan(view_angle / 2.0)
                            / height)
        radius = self.LOCATE_MARKER_RADIUS_PX * max(world_per_px, 0.0)
        x0, x1, y0, y1, _z0, _z1 = self.display_bounds
        cap = 0.25 * min(x1 - x0, y1 - y0)
        return float(min(max(radius, 1e-9), max(cap, 1e-9)))

    def _place_locate_markers(self, hit):
        selection = hit['selection']
        point = selection.point
        for metric, actor in self.locate_markers.items():
            position = self._disp_point(point.x_nm, point.y_nm, point.z_nm)
            radius = self._marker_radius_nm(metric, point)
            actor.SetPosition(*position)
            # Uniform scale: the display Z exaggeration is never applied to the
            # marker, so it stays a sphere and is not stretched into a column.
            actor.SetScale(radius, radius, radius)
            value = point.er_gpa if metric == 'Er' else point.h_gpa
            actor.GetProperty().SetColor(*self._metric_rgba(metric, value))
            actor.SetVisibility(True)
            halo = self.locate_halos[metric]
            halo.SetPosition(*position)
            halo.SetScale(radius * 1.22, radius * 1.22, radius * 1.22)
            halo.SetVisibility(True)
        self.locate_marker_radius_nm = self._marker_radius_nm('Er', point)

    # ------------------------------------------------------- locate guide ring
    @staticmethod
    def _ring_polydata(cx: float, cy: float, radius: float, n: int = 48):
        """Closed 2-D circle in viewport-local DISPLAY pixels."""
        theta = np.linspace(0.0, 2.0 * math.pi, n, endpoint=False)
        pts = np.column_stack([float(cx) + float(radius) * np.cos(theta),
                               float(cy) + float(radius) * np.sin(theta),
                               np.zeros(n)])
        lines = np.empty(n * 3, dtype=np.int64)
        lines[0::3] = 2
        lines[1::3] = np.arange(n)
        lines[2::3] = np.roll(np.arange(n), -1)
        poly = pv.PolyData()
        poly.points = pts
        poly.lines = lines
        return poly

    def _new_ring_actor(self, color: str, line_width: float):
        mapper = vtk.vtkPolyDataMapper2D()
        coordinate = vtk.vtkCoordinate()
        # DISPLAY is viewport-local pixels in this VTK build (verified: the
        # right-hand plot at x0=0.5 shifts the same local coordinate by 450 px).
        coordinate.SetCoordinateSystemToDisplay()
        mapper.SetTransformCoordinate(coordinate)
        mapper.SetInputData(self._ring_polydata(0.0, 0.0, 0.0))
        actor = vtk.vtkActor2D()
        actor.SetMapper(mapper)
        actor.PickableOff()
        actor.GetProperty().SetColor(*pv.Color(color).float_rgb)
        actor.GetProperty().SetLineWidth(float(line_width))
        return actor

    def _display_local(self, metric: str, position) -> tuple[float, float]:
        """World position -> the viewport-local DISPLAY pixels of ``metric``."""
        renderer = self.plot_renderers[metric]
        world = np.asarray(position, dtype=np.float64)
        renderer.SetWorldPoint(float(world[0]), float(world[1]),
                               float(world[2]), 1.0)
        renderer.WorldToDisplay()
        display = renderer.GetDisplayPoint()
        viewport = renderer.GetViewport()
        width, height = self._window_size()
        local_x = float(display[0]) - float(viewport[0]) * float(width)
        local_y = float(display[1]) - float(viewport[1]) * float(height)
        return local_x, local_y

    def _set_ring_geometry(self, actor, cx: float, cy: float, radius: float):
        actor.GetMapper().SetInputData(self._ring_polydata(cx, cy, radius))

    def _slice_occludes(self, point) -> bool:
        """Is the opaque slice plane between the camera and the real point?

        Geometry only: no visibility flag and no size assumption.  The slice
        is opaque in this viewer, so if the ray from the camera to the real
        sphere crosses the slice plane inside a supported grid cell, the real
        sphere is painted behind it (and is therefore not visible as a ball).
        """
        if self.current_depth is None or 'Er' not in self.current_planes:
            return False
        renderer = self.plot_renderers['Er']
        camera = renderer.GetActiveCamera()
        position = np.asarray(camera.GetPosition(), dtype=np.float64)
        marker = np.array(self._disp_point(point.x_nm, point.y_nm,
                                           point.z_nm))
        plane_z = self._mirror_z(self.current_depth) * self.z_scale
        dz = marker[2] - position[2]
        if abs(dz) < 1e-12:
            return False
        t = (plane_z - position[2]) / dz
        if not (0.0 < t < 1.0):
            return False
        hit = position + t * (marker - position)
        x0, x1, y0, y1 = self.display_bounds[:4]
        if not (min(x0, x1) - 1e-9 <= hit[0] <= max(x0, x1) + 1e-9
                and min(y0, y1) - 1e-9 <= hit[1] <= max(y0, y1) + 1e-9):
            return False
        plane = self.current_planes['Er']
        hit_x, hit_y = self._phys_xy(hit[0], hit[1])
        i = int(round((hit_x - self.field.x0_nm) / self.field.dx_nm))
        j = int(round((hit_y - self.field.y0_nm) / self.field.dy_nm))
        if not (0 <= i < self.field.nx and 0 <= j < self.field.ny):
            return False
        return bool(plane.mask[j, i])

    def _place_locate_rings(self, hit, occluded: bool):
        """Place the auxiliary screen ring at the real point's projection."""
        selection = hit['selection']
        point = selection.point
        position = self._disp_point(point.x_nm, point.y_nm, point.z_nm)
        for metric, pair in self.locate_rings.items():
            local_x, local_y = self._display_local(metric, position)
            outer, inner = pair
            self._set_ring_geometry(outer, local_x, local_y,
                                    self.LOCATE_RING_RADIUS_PX)
            value = point.er_gpa if metric == 'Er' else point.h_gpa
            inner.GetProperty().SetColor(*self._metric_rgba(metric, value))
            self._set_ring_geometry(inner, local_x, local_y,
                                    self.LOCATE_RING_INNER_PX)
            outer.SetVisibility(True)
            inner.SetVisibility(True)
            self.locate_ring_state[metric] = {
                'display_local_px': [local_x, local_y],
                'radius_px': self.LOCATE_RING_RADIUS_PX,
                'inner_radius_px': self.LOCATE_RING_INNER_PX,
                'centre_px': [float(v) for v in
                              self._display_window(metric, position)],
                'occluded_by_slice': bool(occluded),
                'kind': 'auxiliary screen guide ring (not the measured sphere)',
            }
        self.locate_occluded = bool(occluded)

    def _display_window(self, metric: str, position) -> tuple[float, float]:
        renderer = self.plot_renderers[metric]
        renderer.SetWorldPoint(float(position[0]), float(position[1]),
                               float(position[2]), 1.0)
        renderer.WorldToDisplay()
        display = renderer.GetDisplayPoint()
        return float(display[0]), float(display[1])

    def _hide_locate_rings(self):
        for pair in self.locate_rings.values():
            for actor in pair:
                actor.SetVisibility(False)
        self.locate_ring_state = {}

    def _refresh_locate_ring_positions(self, *_args):
        """Keep the 2-D guide ring on the real point after camera/resize."""
        if self._ring_refresh_guard:
            return
        selection = self._last_selection
        if (self.view_mode != 'locate' or selection is None
                or self.locate_markers.get('Er') is None
                or not bool(self.locate_markers['Er'].GetVisibility())
                or not self.locate_rings):
            return
        self._ring_refresh_guard = True
        try:
            occluded = self._slice_occludes(selection.point)
            if occluded != self.locate_occluded:
                self.locate_occluded = occluded
                text = self.locate_label(selection, occluded=occluded)
                self._hover_text = text
                for actor in self.hover_actors.values():
                    if bool(actor.GetVisibility()):
                        actor.SetInput(self._band_text(text))
            hit = {'selection': selection}
            self._place_locate_rings(hit, occluded)
        finally:
            self._ring_refresh_guard = False

    def _clear_hover(self, render: bool = True):
        had = bool(self._hover_text) or self._hover_metric is not None
        self._hover_text = ''
        self._hover_metric = None
        self._last_selection = None
        for actor in self.hover_actors.values():
            actor.SetVisibility(False)
        for actor in self.locate_markers.values():
            actor.SetVisibility(False)
        for actor in self.locate_halos.values():
            actor.SetVisibility(False)
        self._hide_locate_rings()
        self.locate_occluded = None
        if had and render:
            self.plotter.render()

    def _update_hover(self, x_px, y_px):
        hit = self.hover_from_display(x_px, y_px)
        if hit is None:
            self._clear_hover(render=True)
            return
        selection = hit['selection']
        occluded = self._slice_occludes(selection.point)
        text = self.locate_label(selection, occluded=occluded)
        self._hover_text = text
        self._hover_metric = hit['metric']
        self._last_selection = selection
        for actor in self.hover_actors.values():
            actor.SetInput(self._band_text(text))
            actor.SetVisibility(True)
        self._place_locate_markers(hit)
        self._place_locate_rings(hit, occluded)
        self.plotter.render()

    def _button_at(self, x_px, y_px):
        width, height = self._window_size()
        if width <= 0 or height <= 0:
            return None
        fx, fy = x_px / width, y_px / height
        for name, info in self._button_items():
            box = info.get('box')
            if box is None:
                continue
            if name == 'save' and not self._save_available:
                continue
            if box[0] <= fx <= box[1] and box[2] <= fy <= box[3]:
                return name
        return None

    def _update_button_hover(self, x_px, y_px):
        name = self._button_at(x_px, y_px)
        if name == self._hover_button:
            return
        self._hover_button = name
        self._refresh_buttons()
        self.plotter.render()

    def _on_mouse_move(self, *_args):
        if self.plotter.iren is None:
            return
        try:
            x, y = self.plotter.iren.get_event_position()
        except Exception:
            return
        self._update_button_hover(x, y)
        if self.view_mode == 'locate':
            self._update_hover(x, y)

    def _on_leave(self, *_args):
        if self._hover_button is not None:
            self._hover_button = None
            self._refresh_buttons()
        self._clear_hover(render=True)

    # -------------------------------------------------------------- interaction
    def _install_interactions(self):
        if self._interactions_installed or self.plotter.iren is None:
            return
        iren = self.plotter.iren
        iren.add_observer('LeftButtonPressEvent', self._on_ui_press)
        iren.add_observer('LeftButtonReleaseEvent', self._on_ui_release)
        iren.add_observer('MouseMoveEvent', self._on_ui_move)
        iren.add_observer('InteractionEvent', self._on_ui_drag)
        iren.add_observer('LeaveEvent', self._on_ui_leave)
        iren.add_observer('KeyPressEvent', self._on_ui_key)
        for renderer in self.plot_renderers.values():
            renderer.AddObserver('StartInteractionEvent',
                                 self._on_camera_interaction)
            # Keep the auxiliary 2-D locate ring glued to the real point when
            # the camera moves or the window is resized (pixels, not world nm).
            renderer.AddObserver('StartEvent',
                                 self._refresh_locate_ring_positions)
        # The style also fires StartInteractionEvent on the interactor itself;
        # observe it there as well so a real rotate/dolly always counts.
        iren.add_observer('StartInteractionEvent', self._on_camera_interaction)
        iren.add_observer('MouseWheelForwardEvent', self._on_camera_interaction)
        iren.add_observer('MouseWheelBackwardEvent', self._on_camera_interaction)
        def hotkey(action):
            return lambda: None if self.help_open else action()

        self.plotter.add_key_event('1', hotkey(lambda: self.set_view_mode('normal')))
        self.plotter.add_key_event('2', hotkey(lambda: self.set_view_mode('locate')))
        self.plotter.add_key_event('3', hotkey(lambda: self.set_view_mode('raw')))
        self.plotter.add_key_event('r', hotkey(self.reset_view))
        self.plotter.add_key_event('c', hotkey(lambda: self.set_range_mode(
            'actual' if self.range_mode == 'common' else 'common')))
        for key, mode in (('a', 'auto'), ('l', 'layers'), ('e', 'envelope')):
            self.plotter.add_key_event(
                key, hotkey(lambda mode=mode: self.set_reference_mode(mode)))
        self.plotter.add_key_event('o', hotkey(
            lambda: self._handle_file_action('open')))
        self.plotter.add_key_event('x', hotkey(
            lambda: self._handle_file_action('export')))
        self.plotter.add_key_event('s', hotkey(
            lambda: self._handle_file_action('save')))
        self.plotter.add_key_event('k', hotkey(
            lambda: self._handle_file_action('load')))
        self.plotter.add_key_event('g', hotkey(self.toggle_language))
        self._interactions_installed = True

    # ------------------------------------------------------------- language
    def set_language(self, language: str, *, persist: bool = False,
                     render: bool = True) -> None:
        target = i18n.normalize_language(language)
        if target is None or target == self.language:
            return
        if target == 'zh-CN' and not self.font_file:
            self._notice = self._t('notice.font_fallback')
            self._refresh_status(render=render)
            return
        self.language = target
        self._notice = ''
        if persist:
            try:
                self.language_settings.save(target)
                self._notice = self._t('notice.language_saved')
            except OSError as exc:
                self._notice = f'{exc.__class__.__name__}: {exc}'
        self._refresh_language_texts(render=render)

    def toggle_language(self, *, persist: bool = True) -> None:
        self.set_language('en' if self.language == 'zh-CN' else 'zh-CN',
                          persist=persist)

    def _refresh_language_texts(self, *, render: bool = True) -> None:
        for name, info in self._button_items():
            text = self._button_label(name)
            info['text'] = text
            info['actor'].SetInput(self._band_text(text))
        for actor, key in ((getattr(self, 'range_title_actor', None),
                            'label.range'),
                           (getattr(self, 'grey_title_actor', None),
                            'label.grey')):
            if actor is not None:
                actor.SetInput(self._t(key))
        self._refresh_depth_value()
        self._refresh_help_text()
        self.dataset_label = self._dataset_label(self.dataset)
        self.window_title = f'{APP_TITLE} | {self.dataset_label}'
        self.plotter.title = self.window_title
        self._layout_buttons()
        self._refresh_buttons()
        self.skin.invalidate(self.language)
        self._refresh_status(render=False)
        if self.help_open:
            self._render_help(render=False)
        if render:
            self.plotter.render()

    def _on_click(self, *_args):
        if self.plotter.iren is None:
            return
        try:
            x, y = self.plotter.iren.get_event_position()
        except Exception:
            return
        name = self._button_at(x, y)
        if name is None:
            return
        if name == 'lang':
            self.toggle_language()
            return
        kind, value = self.buttons[name]['action']
        if kind == 'view':
            self.set_view_mode(value)
        elif kind == 'range':
            self.set_range_mode(value)
        elif kind == 'reference':
            self.set_reference_mode(value)
        elif kind == 'interp':
            self.set_interpolation(value)
        elif kind == 'file':
            self._handle_file_action(value)
        elif kind == 'language':
            self.toggle_language()
        else:
            self.reset_view()

    # ------------------------------------------------------------ file actions
    def set_file_callbacks(self, *, export=None, save=None, open_data=None,
                           load_result=None, export_dir=None) -> None:
        """Wire app-level file actions.  Callbacks receive this viewer."""
        self._export_callback = export
        self._save_callback = save
        self._open_callback = open_data
        self._load_callback = load_result
        self._export_dir_callback = export_dir

    def set_save_available(self, available: bool) -> None:
        self._save_available = bool(available)
        self._refresh_buttons()

    def set_notice(self, text: str) -> None:
        self._notice = i18n.localize_notice(str(text or ''), self.language)
        self._refresh_status(render=True)

    def set_timings(self, timings) -> None:
        self.timings = timings
        self._refresh_status(render=True)

    def _handle_file_action(self, name: str) -> None:
        callback = {'export': self._export_callback,
                    'save': self._save_callback,
                    'open': self._open_callback,
                    'load': self._load_callback}.get(name)
        if name == 'save' and not self._save_available:
            self.set_notice(self._t('notice.save_unavailable'))
            return
        if callback is None:
            self.set_notice(f'{self._button_label(name)}: unavailable')
            return
        callback(self)

    def request_action(self, kind: str, value=None) -> None:
        """Close the window and hand one action back to the app loop."""
        self.pending_action = (kind, value)
        self._closed = True
        interaction.terminate(self.plotter)

    def show(self, *args, **kwargs):
        self._install_interactions()
        return self.plotter.show(*args, **kwargs)

    # ------------------------------------------------------------ export data
    def build_snapshot(self, metric: str = 'both') -> SliceSnapshot:
        """Self-contained description of the current frame for PNG metadata."""
        sliced = (self.view_mode != 'raw' and self.current_range.valid
                  and self.current_depth is not None)
        grid = GridView(
            x_nm=[float(v) for v in self.field.x_nm],
            y_nm=[float(v) for v in self.field.y_nm],
            z_nm=[float(v) for v in self.field.z_nm],
            nx=int(self.field.nx), ny=int(self.field.ny), nz=int(self.field.nz),
            dx_nm=float(self.field.dx_nm), dy_nm=float(self.field.dy_nm),
            dz_nm=float(self.field.dz_nm))
        plane = None
        if sliced:
            plane_metric = 'Er' if metric == 'both' else metric
            src = self.current_planes.get(plane_metric)
            if src is not None:
                plane = PlaneView(
                    axis=src.axis, position_nm=float(src.position_nm),
                    metric=plane_metric,
                    u_nm=[float(v) for v in src.u_nm],
                    v_nm=[float(v) for v in src.v_nm],
                    values=src.values.astype(float).ravel(order='C').tolist(),
                    mask=src.mask.astype(bool).ravel(order='C').tolist())
        camera = None
        renderer = self.plot_renderers.get('Er')
        if renderer is not None:
            cam = renderer.camera
            camera = CameraView(
                position=tuple(float(v) for v in cam.position),
                focal_point=tuple(float(v) for v in cam.focal_point),
                up=tuple(float(v) for v in cam.up),
                parallel_scale=float(cam.parallel_scale),
                view_angle=float(cam.view_angle),
                clipping_range=tuple(float(v) for v in cam.clipping_range))
        if self.view_mode == 'raw':
            depth_note = 'RAW measured point view; this PNG is not an interpolated slice'
        elif sliced:
            depth_note = f'actual displayed Z-slice heff = {self.current_depth:.3f} nm'
        else:
            depth_note = 'no slice displayed in this frame'
        return SliceSnapshot(
            app_version=APP_VERSION,
            dataset_label=self.dataset_label,
            depth_nm=(None if self.view_mode == 'raw' else
                      (float(self.current_depth) if self.current_depth is not None
                       else None)),
            depth_kind=('slice' if sliced else 'not_a_slice'),
            view_mode=self.view_mode,
            range_mode=self.range_mode,
            metric=metric,
            depth_note=depth_note,
            grid=grid,
            plane=plane,
            camera=camera,
            source_path=self.dataset.source_dir,
            source_signature=getattr(self.dataset, 'source_sha256', None))

    # ---------------------------------------------------------------- reports
    def _slider_report(self) -> dict:
        info = {'exists': self.slider is not None, 'enabled': False,
                'current_renderer_viewport': None,
                'representation_renderer_viewport': None,
                'representation_in_control': False,
                'representation_in_others': [],
                'point1': None, 'point2': None,
                'coord_system': None}
        if self.slider is None:
            return info
        rep = self.slider.GetRepresentation()
        info['enabled'] = bool(self.slider.GetEnabled())
        current = self.slider.GetCurrentRenderer()
        info['current_renderer_viewport'] = (list(current.GetViewport())
                                             if current is not None else None)
        rep_renderer = rep.GetRenderer()
        info['representation_renderer_viewport'] = (
            list(rep_renderer.GetViewport()) if rep_renderer is not None else None)
        info['representation_in_control'] = bool(
            self.control_renderer.HasViewProp(rep))
        for index, renderer in enumerate(self.plotter.renderers):
            if renderer is not self.control_renderer and renderer.HasViewProp(rep):
                info['representation_in_others'].append(index)
        info['point1'] = list(rep.GetPoint1Coordinate().GetValue())
        info['point2'] = list(rep.GetPoint2Coordinate().GetValue())
        info['coord_system'] = int(rep.GetPoint1Coordinate().GetCoordinateSystem())
        return info

    def _raw_spacing_report(self) -> dict | None:
        """Compact, JSON-friendly copy of the cached display-spacing facts."""
        if not self.raw_spacing:
            return None
        report = {key: value for key, value in self.raw_spacing.items()
                  if key not in ('nearest_distances_nm',
                                 'per_point_multiplicity')}
        nearest = self.raw_spacing.get('nearest_distances_nm') or []
        report['nearest_distance_count'] = len(nearest)
        return report

    def scene_report(self) -> dict:
        x0, x1, y0, y1, z0, z1 = self.field.bounds_nm
        plots = {}
        for metric in self.metrics:
            renderer = self.plot_renderers[metric]
            used_peeling = bool(renderer.GetLastRenderingUsedDepthPeeling())
            marker = self.locate_markers[metric]
            plots[metric] = {
                'depth_peeling_requested': bool(renderer.GetUseDepthPeeling()),
                'last_rendering_used_depth_peeling': used_peeling,
                'slice_visible': bool(self.slice_actors[metric].GetVisibility()),
                'slice_points': int(self.slice_actors[metric].GetMapper()
                                    .GetInput().GetNumberOfPoints())
                if self.slice_actors[metric].GetMapper().GetInput() is not None else 0,
                'slice_clim': list(self.slice_actors[metric].GetMapper().GetScalarRange()),
                'raw_visible': bool(self.point_actors[metric].GetVisibility()),
                'raw_points': int(self.point_actors[metric].GetMapper()
                                  .GetInput().GetNumberOfPoints()),
                'raw_mapper_type': type(
                    self.point_actors[metric].GetMapper()).__name__,
                'raw_glyph_input_points': int(
                    self.point_actors[metric].GetMapper().GetInput()
                    .GetNumberOfPoints()),
                'raw_sphere_radius_nm': float(self.raw_sphere_radius_nm[metric]),
                'raw_sphere_radius_rule': self.RAW_SPHERE_RADIUS_RULE,
                'raw_spacing': self._raw_spacing_report(),
                'raw_spacing_computations': int(self.raw_spacing_computations),
                'raw_sphere_resolution': dict(self.raw_sphere_resolution),
                'raw_point_size': float(self.point_actors[metric].GetProperty().GetPointSize()),
                'raw_render_points_as_spheres': bool(
                    self.point_actors[metric].GetProperty().GetRenderPointsAsSpheres()),
                'raw_actor_scale': list(self.point_actors[metric].GetScale()),
                'locate_marker_visible': bool(marker.GetVisibility()),
                'locate_marker_position': list(marker.GetPosition()),
                'locate_marker_scale': list(marker.GetScale()),
                'locate_marker_color': list(marker.GetProperty().GetColor()),
                'locate_ring_visible': any(
                    bool(actor.GetVisibility())
                    for actor in self.locate_rings.get(metric, [])),
                'locate_ring': self.locate_ring_state.get(metric),
                'locate_occluded_by_slice': self.locate_occluded,
                'reference_visible_count': sum(
                    int(bool(actor.GetVisibility()))
                    for actor in self.reference_actors[metric]),
                'reference_drawn_count': len(self.reference_actors[metric]),
                'envelope_visible': bool(self.envelope_actors[metric].GetVisibility()),
                'envelope_bounds_nm': list(self.physical_bounds),
                'envelope_display_bounds_nm': [0.0, float(self.xy_span_nm[0]),
                                               0.0, float(self.xy_span_nm[1]),
                                               0.0, float(self.display_span_nm[2])],
                'cloud_visible': bool(self.cloud_actors[metric].GetVisibility()),
                'cloud_points_lod': int(self.cloud_lod_counts.get(metric, 0)),
                'cloud_points_shown': int(self.cloud_points_shown.get(metric, 0)),
                'cloud_clip_depth_nm': (None if not self.current_range.valid
                                        else float(self.current_depth)),
                'cloud_point_size': float(
                    self.cloud_actors[metric].GetProperty().GetPointSize()),
                'cloud_opacity': float(
                    self.cloud_actors[metric].GetProperty().GetOpacity()),
                # The depth cut is a GPU clipping plane: the cloud geometry is
                # never rebuilt when the slider moves.
                'cloud_mask': 'clipping_plane',
                'cloud_fog': bool(self.cloud_fog_enabled),
                'cloud_fog_visible': (bool(self.cloud_fog_actors[metric].GetVisibility())
                                      if metric in self.cloud_fog_actors else False),
                'cloud_fog_opacity': float(CLOUD_FOG_OPACITY),
                'slice_face_visible': bool(self.slice_actors[metric].GetVisibility()),
                'slice_face_opacity': float(
                    self.slice_actors[metric].GetProperty().GetOpacity()),
                'axes_relative': True,
                'axes_bounds': list(self.display_bounds),
                'axes_labels': self.axes_actors[metric].report(),
            }
        slice_info = None
        if self.current_depth is not None and 'Er' in self.current_planes:
            plane = self.current_planes['Er']
            h_plane = self.current_planes['H']
            slice_info = {
                'depth_nm': self.current_depth,
                'shape': [int(plane.values.shape[0]), int(plane.values.shape[1])],
                'mask_fraction': float(plane.mask.mean()),
                'supported_samples': int(plane.mask.sum()),
                'finite_er': int(np.isfinite(plane.values).sum()),
                'finite_h': int(np.isfinite(h_plane.values).sum()),
                'er_min_max': [float(np.nanmin(plane.values)), float(np.nanmax(plane.values))],
                'h_min_max': [float(np.nanmin(h_plane.values)), float(np.nanmax(h_plane.values))],
                'method': plane.method,
            }
        return {
            'dataset': {
                'source_dir': self.dataset.source_dir,
                'kind': self.dataset.kind,
                'files': len(self.dataset.files),
                'points': self.dataset.point_count,
                'groups': self.dataset.group_count,
                'xy_bounds_nm': list(self.dataset.xy_bounds_nm),
                'z_bounds_nm': list(self.dataset.z_bounds_nm),
                'scalar_ranges': {k: list(v) for k, v in self.fixed_ranges.items()},
            },
            'field': self.field.metadata(),
            'range': {
                'mode': self.range_mode,
                'lo_nm': self.current_range.lo_nm,
                'hi_nm': self.current_range.hi_nm,
                'valid': self.current_range.valid,
                'note': self.current_range.note,
                'common': {'valid': self.range_modes['common'].valid,
                           'lo_nm': self.range_modes['common'].lo_nm,
                           'hi_nm': self.range_modes['common'].hi_nm},
                'actual': {'valid': self.range_modes['actual'].valid,
                           'lo_nm': self.range_modes['actual'].lo_nm,
                           'hi_nm': self.range_modes['actual'].hi_nm},
            },
            'layers': {
                'depths_nm': list(self.layers.depths_nm),
                'counts': list(self.layers.counts),
                'bin_nm': self.layers.bin_nm,
                'anchor_nm': self.layers.anchor_nm,
                'rank_count': self.layers.rank_count,
                'method': self.layers.method,
                'reference_layer_limit': REFERENCE_LAYER_LIMIT,
                'reference_selection': dict(self.reference_selection),
                'peak_prominence': float(REFERENCE_PEAK_PROMINENCE),
            },
            'view_mode': self.view_mode,
            'display_mode': self.display_mode,
            'reference_mode': self.reference_mode,
            'resolved_reference_mode': self.resolved_reference_mode,
            'interpolation': self.interpolation,
            'display_frame': {
                'origin_nm': list(self.disp_origin),
                'bounds': list(self.display_bounds),
                'z_mirrored': True,
                'z_scale': float(self.z_scale),
                'cloud_point_limit': CLOUD_POINT_LIMIT,
            },
            'window_title': self.window_title,
            'dataset_label': self.dataset_label,
            'product': {
                'app': APP_TITLE,
                'version': APP_VERSION,
                'cache_hit': bool(self.cache_hit),
                'save_available': bool(self._save_available),
                'source_note': self.source_note,
                'timings': None if self.timings is None else self.timings.as_dict(),
                'raw_spacing_computations': int(self.raw_spacing_computations),
                'pending_action': (None if self.pending_action is None
                                   else [self.pending_action[0],
                                         str(self.pending_action[1])]),
            },
            'buttons': {name: {'box': list(info['box']) if info['box'] else None,
                               'state': self._button_state(name),
                               'action': list(info['action']),
                               'center': [info['x'], info['y']]}
                        for name, info in self.buttons.items()},
            'slider': self._slider_report(),
            'locate': {
                'marker_radius_nm': self.locate_marker_radius_nm,
                'marker_radius_rule': ('RAW sphere radius of the metric '
                                       '(display nm)'),
                'marker_radius_fallback_px': self.LOCATE_MARKER_RADIUS_PX,
                'marker_mode': 'real measured point, original value colour',
                'selection': None if self._last_selection is None else {
                    'point_id': self._last_selection.point.point_id,
                    'source_file': self._last_selection.point.source_file,
                    'line_no': self._last_selection.point.line_no,
                    'row_index': self._last_selection.point.row_index,
                    'x_nm': self._last_selection.point.x_nm,
                    'y_nm': self._last_selection.point.y_nm,
                    'z_nm': self._last_selection.point.z_nm,
                    'er_gpa': self._last_selection.point.er_gpa,
                    'h_gpa': self._last_selection.point.h_gpa,
                    'plane_depth_nm': self._last_selection.plane_depth_nm,
                    'delta_z_nm': self._last_selection.delta_z_nm,
                    'distance_xy_nm': self._last_selection.distance_xy_nm,
                    'same_xy_files': list(self._last_selection.same_xy_files),
                },
            },
            'fixed_ranges': {k: list(v) for k, v in self.fixed_ranges.items()},
            'slice': slice_info,
            'plots': plots,
            'ticks': {'depths_nm': self._tick_depths(),
                      'major_indices': list(self._major_indices),
                      'font_size': self._tick_font_size},
            'display': {
                'physical_bounds_nm': list(self.physical_bounds),
                'display_bounds_nm': list(self.display_bounds),
                'z_display_scale': self.z_scale,
                'z_exaggeration_note': 'display-only actor scale; labels and data '
                                       'remain physical nm; XY aspect ratio is true',
                'window_size': list(self._window_size()),
                'view_fit_margin': self.VIEW_FIT_MARGIN,
                'camera_customized': bool(self._camera_customized),
            },
            'reset': {
                'available': self._initial_state is not None,
                'initial_depth_nm': (None if not self._initial_state
                                     else self._initial_state['depth']),
                'initial_range_mode': (None if not self._initial_state
                                       else self._initial_state['range_mode']),
                'initial_reference_mode': (None if not self._initial_state
                                           else self._initial_state['reference_mode']),
                'initial_view_mode': (None if not self._initial_state
                                      else self._initial_state['view_mode']),
                'initial_interpolation': (None if not self._initial_state
                                          else self._initial_state.get(
                                              'interpolation')),
                'cameras': {} if not self._initial_state else
                {m: {'position': list(c['position']),
                     'focal': list(c['focal']),
                     'view_up': list(c['view_up']),
                     'parallel': c['parallel'],
                     'parallel_scale': c['parallel_scale'],
                     'clipping': list(c['clipping'])}
                 for m, c in self._initial_state['cameras'].items()},
                'data_reload': False,
                'field_rebuild': False,
            },
            'hover': {'text': self._hover_text, 'metric': self._hover_metric},
            'depth_peeling_config': self.depth_peeling_requested,
            'capabilities': self.plotter.render_window.ReportCapabilities(),
            'pyvista_version': pv.__version__,
            'vtk_version': pv.vtk_version_info,
        }
