"""Geometry of the UI v2 window in the 1536 x 992 design frame.

Every rectangle is authored at the design size and scaled by the window's
actual client size, so the painter, the widget hit tests and the two 3D
viewports all read the same numbers.  The module is pure geometry: no VTK, no
Pillow, no i18n - it can be unit-tested on its own.
"""
from __future__ import annotations

from dataclasses import dataclass

from gui import theme

#: Design-frame layout constants (1536 x 992 client area).
TOOLBAR_H = 63
SIDEBAR_W = 238
MAIN_X0 = 258
MAIN_X1 = 1520
STATUS_TOP = 953

#: Card / panel rectangles in design coordinates (x0, y0, x1, y1).
REF_CARDS = {
    'plot_er': (256, 134, 862, 568),
    'plot_h': (871, 134, 1517, 568),
    'depth': (256, 580, 1086, 777),
    'hist': (1116, 580, 1517, 777),
    'export_dir': (1119, 788, 1518, 928),
}
REF_EXPORT_CARDS = (
    (270, 827, 518, 916),
    (531, 827, 796, 916),
    (809, 827, 1102, 916),
)
REF_STATUS = (0, 953, 1536, 992)


@dataclass(frozen=True)
class Rect:
    """Axis-aligned rectangle in top-left pixel coordinates."""

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        return max(0.0, self.y1 - self.y0)

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2.0

    @property
    def center(self) -> tuple[float, float]:
        return self.cx, self.cy

    def contains(self, x: float, y: float, pad: float = 0.0) -> bool:
        return (self.x0 - pad <= x <= self.x1 + pad
                and self.y0 - pad <= y <= self.y1 + pad)

    def inset(self, dx: float, dy: float | None = None) -> 'Rect':
        dy = dx if dy is None else dy
        return Rect(self.x0 + dx, self.y0 + dy, self.x1 - dx, self.y1 - dy)

    def offset(self, dx: float, dy: float) -> 'Rect':
        return Rect(self.x0 + dx, self.y0 + dy, self.x1 + dx, self.y1 + dy)

    def as_tuple(self) -> tuple[float, float, float, float]:
        return self.x0, self.y0, self.x1, self.y1

    def as_int(self) -> tuple[int, int, int, int]:
        return (int(round(self.x0)), int(round(self.y0)),
                int(round(self.x1)), int(round(self.y1)))

    def normalized(self, width: float, height: float):
        """Top-left-normalised coordinates (0..1), the hit-test space."""
        return (self.x0 / width, self.y0 / height,
                self.x1 / width, self.y1 / height)

    def vtk_viewport(self, width: float, height: float):
        """VTK viewport (origin bottom-left) matching this rectangle."""
        return (self.x0 / width, (height - self.y1) / height,
                self.x1 / width, (height - self.y0) / height)


class Layout:
    """All rectangles and text anchors for one client size.

    Both axes share one scale, so nothing is stretched: at 1920 x 1044 the
    scale is 1044/992 and the extra horizontal room goes to the two plot
    cards, the histogram and the export-directory card.  font_scale changes
    only the type size (and honours --ui-scale).
    """

    def __init__(self, width: int, height: int, ui_scale: float = 1.0,
                 font_scale: float | None = None):
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        # P8: one scale for both axes.  The old code stretched x and y
        # independently, so every card and padding disagreed as soon as the
        # window was not the 1536 x 992 design aspect (1920 x 1044 at 125 %,
        # or 1280 x 720).  Extra width is given to the main area below.
        self.scale = min(self.width / float(theme.DESIGN_WIDTH),
                         self.height / float(theme.DESIGN_HEIGHT))
        self.sx = self.sy = self.scale
        # ui_scale is the user's --ui-scale factor on top of the size fit.
        # font_scale overrides the derived factor when the caller renders a
        # supersampled pass: the geometry doubles but the 2.0 cap must not
        # clamp the text, or glyphs outgrow their cards.
        if font_scale is None:
            self.font_scale = max(
                0.7, min(self.scale * float(ui_scale), 2.0))
        else:
            self.font_scale = max(0.1, min(float(font_scale), 4.0))

        self.main_x0 = self._x(MAIN_X0)
        self.main_x1 = self.width - self._d(theme.DESIGN_WIDTH - MAIN_X1)

        self.toolbar = Rect(0.0, 0.0, self.width, self._d(TOOLBAR_H))
        self.sidebar = Rect(0.0, self._d(TOOLBAR_H),
                            self._d(SIDEBAR_W), self._d(STATUS_TOP))
        self.main = Rect(self.main_x0, self._d(TOOLBAR_H),
                         self.main_x1, self._d(STATUS_TOP))
        self.status_bar = Rect(0.0, self._d(STATUS_TOP),
                               self.width, self.height)
        self.notice_bar = Rect(self.main_x0, self._d(936),
                               self.main_x1, self._d(953))

        # ---- toolbar -----------------------------------------------------
        self.toolbar_title = self._pt(30, 16)
        self.toolbar_tagline = self._pt(235, 38)
        # Right-anchored so the language switch stays at the window edge on
        # wide screens; the offsets are the design-frame gaps from x=1506.
        right = self.width - self._d(30)
        self.toolbar_buttons = {
            'open': Rect(right - self._d(575), self._d(17),
                         right - self._d(427), self._d(53)),
            'load': Rect(right - self._d(417), self._d(17),
                         right - self._d(239), self._d(53)),
        }
        self.help_button = Rect(right - self._d(202), self._d(24),
                                right - self._d(178), self._d(48))
        self.lang_rect = Rect(right - self._d(167), self._d(18),
                              right, self._d(52))
        self.lang_split_x = right - self._d(83)
        self.lang_segments = (
            Rect(right - self._d(167), self._d(18),
                 right - self._d(83), self._d(52)),
            Rect(right - self._d(83), self._d(18),
                 right, self._d(52)),
        )

        # ---- sidebar -----------------------------------------------------
        self.sidebar_x = self._x(25)
        self.sidebar_data_x = self._x(30)
        self.sidebar_value_x = self._x(103)
        self.sidebar_value_limit_x = self._x(SIDEBAR_W - 10)
        self.sidebar_radio_x = self._x(34)
        self.sidebar_radio_label_x = self._x(69)
        self.sidebar_title_y = {
            'data': self._y(80), 'view': self._y(321),
            'range': self._y(486), 'reference': self._y(617),
        }
        self.sidebar_dividers = [self._y(306), self._y(470)]
        self.sidebar_divider_x = (self._x(8), self._x(250))
        self.data_rows = [self._y(119), self._y(144), self._y(169),
                          self._y(195)]
        self.data_step_label_y = self._y(221)
        self.step_rows = [self._y(247), self._y(272)]
        # radio rows are the circle tops in the design frame
        self.view_rows = [self._y(365), self._y(400), self._y(433)]
        self.range_rows = [self._y(531), self._y(567)]
        self.reference_rows = [self._y(662), self._y(698), self._y(734)]
        # P5: display-only slice interpolation selector, below the reference card.
        self.sidebar_title_y['interp'] = self._y(786)
        self.interp_rows = [self._y(820), self._y(850), self._y(880)]
        self.interp_note_y = self._y(910)
        self.radio_size = self._d(20)

        # ---- main header -------------------------------------------------
        self.header_title = self._pt(270, 78)
        self.segmented = self._ref(480, 76, 921, 121)
        self.segment_rects = (self._ref(480, 76, 700, 121),
                              self._ref(700, 76, 921, 121))
        self.header_divider = (self._x(1059), self._y(88), self._x(1059),
                               self._y(112))
        self.reset_button = Rect(
            self.main_x1 - self._d(MAIN_X1 - 1089), self._y(77),
            self.main_x1 - self._d(MAIN_X1 - 1228), self._y(122))
        self.header_hint = (self.main_x1 - self._d(MAIN_X1 - 1250),
                            self._y(100))
        # P3: haze toggle for the cloud view, between the segmented control and
        # the divider; the box uses the export-checkbox metrics.
        self.fog_box = self._ref(938, 87, 956, 105)
        self.header_fog = self._ref(930, 79, 1048, 113)

        # ---- plot cards --------------------------------------------------
        # The two plot cards share the main area's extra width.  At the
        # 1536 x 992 design size extra is zero and the rectangles are exact.
        er = REF_CARDS['plot_er']
        hp = REF_CARDS['plot_h']
        plot_left = self._x(er[0])
        plot_top = self._y(er[1])
        plot_bottom = self._y(er[3])
        plot_right = self.width - self._d(theme.DESIGN_WIDTH - hp[2])
        gap = self._d(hp[0] - er[2])
        base_er = self._d(er[2] - er[0])
        base_h = self._d(hp[2] - hp[0])
        extra = max(0.0, plot_right - plot_left - gap - base_er - base_h)
        er_w = base_er + extra / 2.0
        h_w = base_h + extra / 2.0
        self.plot_cards = {
            'Er': Rect(plot_left, plot_top, plot_left + er_w, plot_bottom),
            'H': Rect(plot_left + er_w + gap, plot_top,
                      plot_left + er_w + gap + h_w, plot_bottom),
        }
        self.plot_views = {}
        self.plot_colorbars = {}
        self.plot_pills = {}
        for metric, card in self.plot_cards.items():
            # Colour bar geometry taken from the design frame: on the Er card
            # (256, 134)-(862, 568) the bar spans x 778..794 and y 210..505,
            # i.e. 84..67 in from the right edge and 76 / 63 in from the top
            # and bottom.  The H card sits two pixels lower and nine pixels
            # further right in the mock-up; one shared rule is used.
            bar_x1 = card.x1 - self._d(67)
            self.plot_colorbars[metric] = Rect(card.x1 - self._d(84),
                                               card.y0 + self._d(76),
                                               bar_x1,
                                               card.y1 - self._d(63))
            # Leave a small gutter between the scene and the bar.
            self.plot_views[metric] = Rect(card.x0 + self._d(16),
                                           card.y0 + self._d(52),
                                           card.x1 - self._d(88),
                                           card.y1 - self._d(16))
            self.plot_pills[metric] = Rect(card.x1 - self._d(206),
                                           card.y0 + self._d(12),
                                           card.x1 - self._d(16),
                                           card.y0 + self._d(40))

        # ---- depth card --------------------------------------------------
        # Left: one bordered panel that holds the "current slice" label and
        # the thumbnail.  Right: readouts, slider, second value box and the
        # info strip, all starting where the panel ends.
        card = self._ref(*REF_CARDS['depth'])
        self.depth_card = card
        self.depth_title = self._at(card, 16, 7)
        self.thumb_panel = Rect(card.x0 + self._d(16), card.y0 + self._d(46),
                                card.x0 + self._d(208),
                                card.y0 + self._d(182))
        self.thumb_label = (self.thumb_panel.cx,
                            self.thumb_panel.y0 + self._d(8))
        self.thumb_rect = Rect(card.x0 + self._d(26), card.y0 + self._d(72),
                               card.x0 + self._d(198),
                               card.y0 + self._d(172))
        self.depth_label = self._at(card, 234, 44)
        self.depth_range_label = self._at(card, 552, 44)
        self.slider_track = Rect(card.x0 + self._d(234),
                                 card.y0 + self._d(94),
                                 card.x0 + self._d(662),
                                 card.y0 + self._d(100))
        self.slider_value_box = Rect(card.x0 + self._d(686),
                                     card.y0 + self._d(80),
                                     card.x0 + self._d(804),
                                     card.y0 + self._d(112))
        self.slider_unit = self._at(card, 804, 84)
        self.info_strip = Rect(card.x0 + self._d(234),
                               card.y0 + self._d(152),
                               card.x1 - self._d(8),
                               card.y0 + self._d(180))

        # ---- histogram card ---------------------------------------------
        hist = Rect(self._x(REF_CARDS['hist'][0]),
                    self._y(REF_CARDS['hist'][1]),
                    self.main_x1 - self._d(3),
                    self._y(REF_CARDS['hist'][3]))
        self.hist_card = hist
        self.hist_title = self._at(hist, 16, 12)
        self.hist_plot = Rect(hist.x0 + self._d(58), hist.y0 + self._d(48),
                              hist.x1 - self._d(16), hist.y1 - self._d(56))
        self.hist_note = (hist.x0 + self._d(16), hist.y1 - self._d(24))

        # ---- export area -------------------------------------------------
        self.export_heading = self._pt(270, 796)
        self.export_cards = tuple(self._ref(*r) for r in REF_EXPORT_CARDS)
        self.export_dir_card = Rect(
            self._x(REF_CARDS['export_dir'][0]),
            self._y(REF_CARDS['export_dir'][1]),
            self.main_x1 - self._d(2),
            self._y(REF_CARDS['export_dir'][3]))
        dir_card = self.export_dir_card
        self.export_dir_title = self._at(dir_card, 20, 19)
        self.export_path_box = Rect(dir_card.x0 + self._d(20),
                                    dir_card.y0 + self._d(48),
                                    dir_card.x1 - self._d(96),
                                    dir_card.y0 + self._d(82))
        self.export_browse = Rect(dir_card.x1 - self._d(86),
                                  dir_card.y0 + self._d(48),
                                  dir_card.x1 - self._d(16),
                                  dir_card.y0 + self._d(82))
        self.export_checkbox = Rect(dir_card.x0 + self._d(22),
                                    dir_card.y0 + self._d(96),
                                    dir_card.x0 + self._d(40),
                                    dir_card.y0 + self._d(114))
        self.export_dir_note = (dir_card.x0 + self._d(16),
                               dir_card.y1 - self._d(28))

        # ---- status bar ---------------------------------------------------
        self.status_dot = Rect(self._x(16), self._y(964),
                               self._x(28), self._y(976))
        self.status_text = self._pt(36, 960)
        self.status_heff = (self.width - self._d(theme.DESIGN_WIDTH - 1160),
                            self._y(960))
        self.status_version = (self.width - self._d(theme.DESIGN_WIDTH - 1430),
                               self._y(961))

    # ------------------------------------------------------------------ util
    def _x(self, value: float) -> float:
        return float(value) * self.scale

    def _y(self, value: float) -> float:
        return float(value) * self.scale

    def _d(self, value: float) -> float:
        """Uniform design-unit distance (geometry, not the font size)."""
        return float(value) * self.scale

    def _pt(self, x: float, y: float) -> tuple[float, float]:
        return self._x(x), self._y(y)

    def _at(self, rect: Rect, dx: float, dy: float) -> tuple[float, float]:
        """Anchor inside an already scaled rectangle (design-unit offsets)."""
        return rect.x0 + self._d(dx), rect.y0 + self._d(dy)

    def _ref(self, x0: float, y0: float, x1: float, y1: float) -> Rect:
        return Rect(self._x(x0), self._y(y0), self._x(x1), self._y(y1))

    # --------------------------------------------------------------- helpers
    def plot_viewport(self, metric: str):
        """VTK viewport tuple for the 3D interior of a plot card."""
        return self.plot_views[metric].vtk_viewport(self.width, self.height)

    def slider_fraction(self, depth: float | None, lo: float, hi: float) -> float:
        if depth is None or hi <= lo:
            return 0.0
        return min(max((float(depth) - lo) / (hi - lo), 0.0), 1.0)

    def slider_x(self, fraction: float) -> float:
        track = self.slider_track
        return track.x0 + fraction * track.width

    def export_card_rect(self, index: int) -> Rect:
        return self.export_cards[index]

    def radio_rect(self, group: str, index: int) -> Rect:
        rows = {'view': self.view_rows, 'range': self.range_rows,
                'reference': self.reference_rows}[group]
        y = rows[index]
        size = self.radio_size
        x = self.sidebar_radio_x
        return Rect(x, y, x + size, y + size)
