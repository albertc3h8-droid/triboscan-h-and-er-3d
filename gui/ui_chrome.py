"""Pillow painter for the whole UI v2 chrome (VTK layer 0).

The window is drawn as one RGB bitmap: page, cards, sidebar, header, colour
bars, histogram, export tiles and status bar.  A cached static base is painted
per (layout, language, data) and each frame only re-draws the dynamic parts
(thumb, slider, values, hover/checked states, status text), which keeps a depth
change well under the 20 ms budget measured in P0.

Pure Pillow: no VTK import here, so the painter can be tested offscreen.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from PIL import Image, ImageDraw, ImageFilter

from gui import i18n, theme
from gui.ui_layout import Layout, Rect

FONT = 'msyh'
RADIUS = 10
#: Layer-2 help overlay: the dimmed backdrop and the centred card.  Sizes are
#: design-frame pixels, scaled by Layout._d like every other piece of chrome.
HELP_DIM = (10, 22, 44, 118)
HELP_CARD = (860, 660)
HELP_PAD = 36
#: Repository link on the help card footer (fixed text, not translated).
HELP_REPO_URL = 'https://github.com/albertc3h8-droid/triboscan-h-and-er-3d'


@dataclass
class SkinState:
    """Everything the painter needs; filled by TrialViewer each frame."""

    language: str = 'en'
    depth: float | None = None
    depth_lo: float = 0.0
    depth_hi: float = 0.0
    range_valid: bool = True
    slice_index: int = 0
    slice_count: int = 0
    step_nm: float = 0.0
    view_mode: str = 'normal'
    range_mode: str = 'common'
    reference_mode: str = 'auto'
    display_mode: str = 'slice'
    thumb_metric: str = 'Er'
    dataset_label: str = ''
    point_count: int = 0
    file_count: int = 0
    grid_label: str = ''
    step_labels: tuple[str, str, str] = ('', '', '')
    ranges: dict[str, tuple[float, float]] = field(default_factory=dict)
    lut_colors: dict[str, list[tuple[int, int, int]]] = field(default_factory=dict)
    thumb: Any = None
    hist_counts: Any = None
    hist_key: int = 0
    status_items: tuple[str, ...] = ()
    status_heff: str = ''
    notice: str = ''
    notice_is_error: bool = False
    export_dir: str = ''
    open_after_export: bool = True
    version: str = ''
    data_ready: bool = True
    editing_depth: bool = False
    widgets: Any = None


class Skin:
    """Draws the chrome bitmap for a layout and keeps the static base cached."""

    def __init__(self, layout: Layout, fonts: theme.FontBook,
                 language: str = 'en', supersample: int = 1):
        self.layout = layout
        self.fonts = fonts
        self.language = language
        # Whole-number factor for the static base: the base is painted that
        # much larger and averaged back down.  FreeType's grayscale
        # antialiasing snaps stems to whole pixels at the smallest window
        # sizes, which reads as "pixelated" text; supersampling removes those
        # steps.  Dynamic overlays always draw at 1x.
        self.supersample = max(1, int(supersample))
        self._base: Image.Image | None = None
        self._base_key: tuple | None = None

    # ------------------------------------------------------------------ i18n
    def t(self, key: str, **kwargs) -> str:
        return i18n.tr(self.language, key, **kwargs)

    def invalidate(self, language: str | None = None) -> None:
        if language is not None:
            self.language = language
        self._base = None
        self._base_key = None

    # -------------------------------------------------------------- painting
    def render(self, state: SkinState) -> Image.Image:
        """Full frame: cached static base + dynamic controls and readouts."""
        base = self._ensure_base(state)
        image = base.copy()
        draw = ImageDraw.Draw(image)
        self._draw_widgets(draw, state)
        self._draw_depth(draw, state, image)
        self._draw_histogram_marker(draw, state)
        self._draw_export_dir_values(draw, state)
        self._draw_status(draw, state)
        self._draw_notice(draw, state)
        return image

    # ----------------------------------------------------------- help layer
    def help_geometry(self) -> tuple[Rect, Rect]:
        """Card and close-button rectangles of the layer-2 help overlay."""
        layout = self.layout
        pad = layout._d(HELP_PAD)
        width = min(layout._d(HELP_CARD[0]),
                    layout.width - 2 * layout._d(48))
        height = min(layout._d(HELP_CARD[1]),
                     layout.height - 2 * layout._d(48))
        x0 = (layout.width - width) / 2.0
        y0 = (layout.height - height) / 2.0
        card = Rect(x0, y0, x0 + width, y0 + height)
        cx = card.x1 - layout._d(46)
        cy = card.y0 + layout._d(48)
        radius = layout._d(18)
        close = Rect(cx - radius, cy - radius, cx + radius, cy + radius)
        return card, close

    def render_help(self) -> Image.Image:
        """RGBA bitmap of the help card over a dimmed copy of the window.

        The viewer uploads this to a full-window vtkImageMapper on layer 2;
        transparent pixels let the plots and the skin show through, and the
        semi-transparent backdrop is blended by VTK per pixel (verified in
        the P6 probe).  Drawn with the same fonts/palette as the skin.
        """
        layout, fonts = self.layout, self.fonts
        colors = theme.COLORS
        width, height = layout.width, layout.height
        image = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        image.alpha_composite(Image.new('RGBA', (width, height), HELP_DIM))
        card, close = self.help_geometry()
        pad = layout._d(HELP_PAD)
        # Soft shadow under the card; GaussianBlur only runs when help opens.
        shadow = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        shadow_draw = ImageDraw.Draw(shadow)
        shadow_draw.rounded_rectangle(
            (card.x0, card.y0 + layout._d(10), card.x1,
             card.y1 + layout._d(10)),
            radius=layout._d(18), fill=(12, 28, 52, 90))
        image.alpha_composite(shadow.filter(
            ImageFilter.GaussianBlur(layout._d(18))))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(card.as_tuple(), radius=layout._d(18),
                               fill=colors['card'], outline=colors['border'],
                               width=1)
        # header
        draw.text((card.x0 + pad, card.y0 + layout._d(26)),
                  self.t('help.title'), font=fonts.font(26, bold=True),
                  fill=colors['ink_strong'])
        draw.text((card.x0 + pad, card.y0 + layout._d(66)),
                  self.t('help.subtitle'), font=fonts.font(12),
                  fill=colors['muted'])
        radius = close.width / 2.0
        draw.ellipse(close.as_tuple(), fill=colors['primary_soft'])
        draw.line((close.cx - radius * .34, close.cy - radius * .34,
                   close.cx + radius * .34, close.cy + radius * .34),
                  fill=colors['primary_dark'], width=2)
        draw.line((close.cx - radius * .34, close.cy + radius * .34,
                   close.cx + radius * .34, close.cy - radius * .34),
                  fill=colors['primary_dark'], width=2)
        divider_y = card.y0 + layout._d(104)
        draw.line((card.x0 + pad, divider_y, card.x1 - pad, divider_y),
                  fill=colors['divider_soft'], width=1)
        # two-column body: view/depth on top, interpolation/export below
        content_x = card.x0 + pad
        content_w = card.width - 2 * pad
        gap = layout._d(36)
        col_w = (content_w - gap) / 2.0
        right_x = content_x + col_w + gap
        body_top = divider_y + layout._d(26)
        view_bottom = self._help_section(
            draw, content_x, body_top, col_w, 'help.view.title',
            ('help.view.b1', 'help.view.b2', 'help.view.b3'))
        depth_bottom = self._help_section(
            draw, right_x, body_top, col_w, 'help.depth.title',
            ('help.depth.b1', 'help.depth.b2', 'help.depth.b3',
             'help.depth.b4'))
        lower = max(view_bottom, depth_bottom) + layout._d(24)
        interp_bottom = self._help_section(
            draw, content_x, lower, col_w, 'help.interp.title',
            ('help.interp.b1', 'help.interp.b2', 'help.interp.b3'))
        export_bottom = self._help_section(
            draw, right_x, lower, col_w, 'help.export.title',
            ('help.export.b1', 'help.export.b2', 'help.export.b3',
             'help.export.b4'))
        footer_y = card.y1 - layout._d(70)
        # P5 note: only when the window has room for it; on the 1280 x 720
        # stress size the body already reaches the footer and it is skipped.
        note_y = footer_y - layout._d(52)
        if note_y - max(interp_bottom, export_bottom) >= layout._d(14):
            self._help_note(draw, card.x0 + pad, note_y,
                            card.width - 2 * pad)
        draw.line((card.x0 + pad, footer_y, card.x1 - pad, footer_y),
                  fill=colors['divider_soft'], width=1)
        draw.text((card.x0 + pad, footer_y + layout._d(12)),
                  self.t('help.footer'), font=fonts.font(12),
                  fill=colors['muted'])
        draw.text((card.x1 - pad, footer_y + layout._d(12)), 'TriboScan3D',
                  font=fonts.font(12), fill=colors['muted'], anchor='ra')
        contact_y = footer_y + layout._d(30)
        draw.text((card.x0 + pad, contact_y), HELP_REPO_URL,
                  font=fonts.font(10), fill=colors['muted'])
        return image

    def _help_section(self, draw: ImageDraw.ImageDraw, x: float, y: float,
                      width: float, title_key: str,
                      bullet_keys: tuple[str, ...]) -> float:
        layout, fonts = self.layout, self.fonts
        primary = theme.COLORS['primary']
        draw.rounded_rectangle((x, y + layout._d(3), x + layout._d(4),
                                y + layout._d(21)), radius=layout._d(2),
                               fill=primary)
        draw.text((x + layout._d(15), y), self.t(title_key),
                  font=fonts.font(15, bold=True),
                  fill=theme.COLORS['ink_strong'])
        y += layout._d(33)
        for key in bullet_keys:
            draw.ellipse((x + layout._d(5), y + layout._d(8),
                          x + layout._d(9), y + layout._d(12)), fill=primary)
            y = self._help_text(draw, x + layout._d(19), y, self.t(key),
                                width - layout._d(19))
            y += layout._d(8)
        return y - layout._d(8)

    def _help_text(self, draw: ImageDraw.ImageDraw, x: float, y: float,
                   text: str, width: float) -> float:
        font = self.fonts.font(13)
        step = max(1, int(round(font.size * 1.55)))
        for line in self._help_wrap(text, font, width):
            draw.text((x, y), line, font=font, fill=theme.COLORS['sub'])
            y += step
        return y

    def _help_note(self, draw: ImageDraw.ImageDraw, x: float, y: float,
                   width: float) -> None:
        layout, fonts = self.layout, self.fonts
        font = fonts.font(12)
        lines = self._help_wrap(self.t('help.note'), font,
                                width - layout._d(24))
        step = max(1, int(round(font.size * 1.5)))
        height = layout._d(10) + len(lines) * step
        draw.rounded_rectangle((x, y, x + width, y + height),
                               radius=layout._d(8),
                               fill=theme.COLORS['primary_soft'])
        ty = y + layout._d(5)
        for line in lines:
            draw.text((x + layout._d(12), ty), line, font=font,
                      fill=theme.COLORS['primary_dark'])
            ty += step

    @staticmethod
    def _help_wrap(text: str, font, max_width: float) -> list[str]:
        """Wrap at spaces when possible and per character for CJK text."""
        lines: list[str] = []
        for raw in str(text).split('\n'):
            current = ''
            last_space = -1
            for char in raw:
                if font.getlength(current + char) <= max_width:
                    current += char
                    if char == ' ':
                        last_space = len(current) - 1
                elif last_space > 0:
                    lines.append(current[:last_space].rstrip())
                    current = current[last_space + 1:] + char
                    last_space = -1
                else:
                    lines.append(current.rstrip())
                    current = char
            if current:
                lines.append(current.rstrip())
        return lines or ['']

    # ----------------------------------------------------------------- base
    def _signature(self, state: SkinState) -> tuple:
        ranges = tuple((k, round(v[0], 6), round(v[1], 6))
                       for k, v in sorted(state.ranges.items()))
        return (self.layout.width, self.layout.height, self.supersample,
                self.language,
                state.dataset_label, state.point_count, state.file_count,
                state.grid_label, state.step_labels, state.thumb_metric, ranges,
                state.version, state.hist_key, state.range_valid)

    def _ensure_base(self, state: SkinState) -> Image.Image:
        key = self._signature(state)
        if self._base is not None and key == self._base_key:
            return self._base
        self._base = self._build_base(state)
        self._base_key = key
        return self._base

    def _build_base(self, state: SkinState) -> Image.Image:
        factor = self.supersample
        if factor <= 1:
            return self._paint_base(state)
        layout, fonts = self.layout, self.fonts
        scale = float(layout.font_scale)
        self.layout = Layout(layout.width * factor, layout.height * factor,
                             font_scale=scale * factor)
        self.fonts = theme.FontBook(scale * factor)
        try:
            image = self._paint_base(state)
        finally:
            self.layout, self.fonts = layout, fonts
        return image.resize((layout.width, layout.height), Image.BOX)

    def _paint_base(self, state: SkinState) -> Image.Image:
        image = self._paint_page()
        draw = ImageDraw.Draw(image)
        self._paint_toolbar(draw, state)
        self._paint_sidebar(draw, state)
        self._paint_header(draw, state)
        self._paint_plots(draw, state)
        self._paint_depth_card(draw, state)
        self._paint_histogram(draw, state)
        self._paint_export(draw, state)
        self._paint_status_frame(draw, state)
        return image

    def _paint_page(self) -> Image.Image:
        width, height = self.layout.width, self.layout.height
        top = theme.COLORS['page_top']
        bottom = theme.COLORS['page_bottom']
        rows = []
        for index in range(height):
            t = index / max(height - 1, 1)
            rows.append(tuple(round(top[c] + (bottom[c] - top[c]) * t)
                              for c in range(3)))
        strip = Image.new('RGB', (1, height))
        strip.putdata(rows)
        return strip.resize((width, height), Image.NEAREST)

    # -------------------------------------------------------------- toolbar
    def _paint_toolbar(self, draw: ImageDraw.ImageDraw, state: SkinState) -> None:
        layout = self.layout
        fonts = self.fonts
        bar = layout.toolbar
        draw.rectangle(bar.as_int(), fill=(244, 248, 252))
        draw.line([(bar.x0, bar.y1 - 1), (bar.x1, bar.y1 - 1)],
                  fill=theme.COLORS['border'])
        x, y = layout.toolbar_title
        draw.text((x, y), 'TriboScan3D',
                  font=fonts.font(25, bold=True),
                  fill=theme.COLORS['toolbar_ink'])
        draw.text(layout.toolbar_tagline, self.t('toolbar.tagline'),
                  font=fonts.font(15), fill=theme.COLORS['tagline'],
                  anchor='ls')
        draw.rounded_rectangle(layout.lang_rect.as_int(), radius=8,
                               fill=theme.COLORS['white'],
                               outline=(211, 224, 238), width=1)

    # -------------------------------------------------------------- sidebar
    def _paint_sidebar(self, draw: ImageDraw.ImageDraw, state: SkinState) -> None:
        layout = self.layout
        fonts = self.fonts
        ink, sub, muted = (theme.COLORS['ink'], theme.COLORS['sub'],
                           theme.COLORS['muted'])
        x = layout.sidebar_x
        titles = {
            'data': self.t('sidebar.title.data'),
            'view': self.t('sidebar.title.view'),
            'range': self.t('sidebar.title.range'),
            'reference': self.t('sidebar.title.reference'),
            'interp': self.t('sidebar.title.interp'),
        }
        strong = theme.COLORS['ink_strong']
        for key, y in layout.sidebar_title_y.items():
            draw.text((layout.sidebar_x, y), titles[key],
                      font=fonts.font(17, bold=True), fill=strong)
        for y in layout.sidebar_dividers:
            draw.line([(layout.sidebar_divider_x[0], y),
                       (layout.sidebar_divider_x[1], y)],
                      fill=theme.COLORS['divider_soft'])
        rows = [
            (self.t('sidebar.dataset'), state.dataset_label),
            (self.t('sidebar.points'), str(state.point_count)),
            (self.t('sidebar.files'), str(state.file_count)),
            (self.t('sidebar.grid'), state.grid_label),
        ]
        colon = self.t('punct.colon')
        label_x = layout.sidebar_data_x
        limit = layout.sidebar_value_limit_x - layout.sidebar_value_x
        dataset = self._ellipsize(state.dataset_label, 13, limit)
        for index, ((label, value), y) in enumerate(zip(rows, layout.data_rows)):
            draw.text((label_x, y), label + colon, font=fonts.font(13),
                      fill=ink)
            text = dataset if index == 0 else value
            draw.text((layout.sidebar_value_x, y), text,
                      font=fonts.font(13), fill=ink)
        label_y = layout.data_step_label_y
        draw.text((label_x, label_y), self.t('sidebar.step') + colon,
                  font=fonts.font(13), fill=ink)
        if state.step_labels:
            draw.text((layout.sidebar_value_x, label_y),
                      state.step_labels[0], font=fonts.font(13), fill=ink)
        for text, y in zip(state.step_labels[1:], layout.step_rows):
            draw.text((layout.sidebar_value_x, y), text,
                      font=fonts.font(13), fill=ink)
        for group, keys, rows_y in (
                ('view', ('sidebar.normal', 'sidebar.locate', 'sidebar.raw'),
                 layout.view_rows),
                ('range', ('sidebar.common', 'sidebar.actual'),
                 layout.range_rows),
                ('reference', ('sidebar.auto', 'sidebar.layers',
                               'sidebar.envelope'), layout.reference_rows),
                ('interp', ('sidebar.interp.linear', 'sidebar.interp.cubic',
                            'sidebar.interp.tps'),
                 layout.interp_rows)):
            for key, y in zip(keys, rows_y):
                draw.text((layout.sidebar_radio_label_x,
                           y + layout.radio_size / 2.0), self.t(key),
                          font=fonts.font(16), fill=ink, anchor='lm')
        draw.text((layout.sidebar_x, layout.interp_note_y),
                  self.t('sidebar.interp.note'),
                  font=fonts.font(11), fill=muted)

    # --------------------------------------------------------------- header
    def _paint_header(self, draw: ImageDraw.ImageDraw, state: SkinState) -> None:
        layout = self.layout
        fonts = self.fonts
        x, y = layout.header_title
        draw.text((x, y + layout._d(2)), self.t('header.title'),
                  font=fonts.font(21, bold=True), fill=theme.COLORS['ink'])
        x0, y0, x1, y1 = layout.segmented.as_int()
        draw.rounded_rectangle([x0, y0, x1, y1], radius=8,
                               fill=(251, 252, 252),
                               outline=(208, 220, 234), width=1)
        draw.line(layout.header_divider, fill=(172, 184, 206), width=1)
        _, y = layout.header_hint
        hint = self.t('header.hint')
        font = fonts.font(15)
        start = layout.width - layout._d(32) - fonts.measure(hint, 15)
        if start >= layout.reset_button.x1 + layout._d(10):
            draw.text((start, y), hint, font=font,
                      fill=(86, 101, 140), anchor='lm')

    # ---------------------------------------------------------------- plots
    def _paint_plots(self, draw: ImageDraw.ImageDraw, state: SkinState) -> None:
        layout = self.layout
        fonts = self.fonts
        for metric, card in layout.plot_cards.items():
            self._card(draw, card)
            if metric == state.thumb_metric:
                draw.rounded_rectangle(card.as_int(), radius=RADIUS,
                                       outline=theme.COLORS['primary'], width=2)
            title = self.t('card.er' if metric == 'Er' else 'card.h')
            draw.text((card.x0 + layout._d(16), card.y0 + layout._d(18)),
                      title, font=fonts.font(17, bold=True),
                      fill=theme.COLORS['ink_strong'])
            view = layout.plot_views[metric]
            draw.rectangle(view.as_int(), fill=theme.COLORS['white'])
            self._colorbar(draw, metric, state)
            self._pill(draw, layout.plot_pills[metric], '')

    def _colorbar(self, draw: ImageDraw.ImageDraw, metric: str,
                  state: SkinState) -> None:
        layout = self.layout
        fonts = self.fonts
        rect = layout.plot_colorbars[metric]
        colors = state.lut_colors.get(metric) or []
        lo, hi = state.ranges.get(metric, (0.0, 1.0))
        x0, y0, x1, y1 = rect.as_int()
        # The skin base is painted supersample times larger and box-downscaled,
        # so a one-pixel frame would turn into a half-transparent column that
        # fades into the gradient and reads as a notch on the bar's edge.
        # Draw the frame supersample pixels wide (one device pixel) and keep
        # the gradient inside it.  The inclusive end also has to move by
        # frame - 1 so that the box covers whole supersample cells; otherwise
        # the last device row/column keeps only half of its frame.
        frame = max(1, int(getattr(self, 'supersample', 1)))
        label_x1, label_y0, label_y1 = x1, y0, y1
        x1 += frame - 1
        y1 += frame - 1
        span = max(y1 - y0, 1)
        if colors and y1 - y0 > 2 * frame:
            count = len(colors)
            left, right = x0 + frame, x1 - frame
            for row in range(y0 + frame, y1 - frame + 1):
                value = 1.0 - (row - y0) / span
                index = min(max(int(value * (count - 1)), 0), count - 1)
                draw.line([(left, row), (right, row)], fill=colors[index])
        draw.rectangle([x0, y0, x1, y1], outline=(232, 238, 246), width=frame)
        for fraction, value in ((0.0, hi), (0.25, hi - (hi - lo) * 0.25),
                                (0.5, (hi + lo) / 2.0),
                                (0.75, hi - (hi - lo) * 0.75), (1.0, lo)):
            ty = label_y0 + fraction * (label_y1 - label_y0)
            draw.text((label_x1 + layout._d(12), ty), self._fmt_value(value),
                      font=fonts.font(11), fill=theme.COLORS['sub'],
                      anchor='lm')

    @staticmethod
    def _fmt_value(value: float) -> str:
        if value is None:
            return '--'
        magnitude = abs(float(value))
        if magnitude >= 100:
            return f'{value:.0f}'
        if magnitude >= 10:
            return f'{value:.1f}'
        return f'{value:.2f}'

    # ----------------------------------------------------------- depth card
    def _paint_depth_card(self, draw: ImageDraw.ImageDraw,
                          state: SkinState) -> None:
        layout = self.layout
        fonts = self.fonts
        card = layout.depth_card
        self._card(draw, card)
        draw.text(layout.depth_title, self.t('depth.title'),
                  font=fonts.font(15, bold=True), fill=theme.COLORS['ink'])
        panel = layout.thumb_panel.as_int()
        draw.rounded_rectangle(panel, radius=6,
                               fill=(246, 249, 253),
                               outline=theme.COLORS['border'])
        draw.text(layout.thumb_label,
                  self.t('depth.thumb', metric=state.thumb_metric),
                  font=fonts.font(12), fill=theme.COLORS['sub'], anchor='mt')
        frame = layout.thumb_rect.as_int()
        draw.rectangle(frame, fill=(246, 249, 253),
                       outline=theme.COLORS['border'])
        draw.text(layout.depth_label, self.t('depth.current'),
                  font=fonts.font(13), fill=theme.COLORS['sub'])
        draw.text((layout.depth_range_label[0], layout.depth_label[1]),
                  self.t('depth.total', lo=state.depth_lo, hi=state.depth_hi)
                  if state.range_valid else self.t('depth.none'),
                  font=fonts.font(13), fill=theme.COLORS['sub'])
        self._value_box(draw, layout.slider_value_box, '')
        draw.text((layout.slider_unit[0], layout.slider_value_box.cy),
                  'nm', font=fonts.font(12), fill=theme.COLORS['sub'],
                  anchor='lm')
        strip = layout.info_strip.as_int()
        draw.rounded_rectangle(strip, radius=4, fill=theme.COLORS['strip'])

    # ------------------------------------------------------------ histogram
    def _paint_histogram(self, draw: ImageDraw.ImageDraw,
                         state: SkinState) -> None:
        layout = self.layout
        fonts = self.fonts
        self._card(draw, layout.hist_card)
        draw.text(layout.hist_title, self.t('hist.title'),
                  font=fonts.font(13, bold=True),
                  fill=theme.COLORS['ink_strong'])
        plot = layout.hist_plot
        counts = state.hist_counts or []
        lo, hi = state.depth_lo, state.depth_hi
        draw.line([(plot.x0, plot.y1), (plot.x1, plot.y1)],
                  fill=theme.COLORS['axis'])
        draw.line([(plot.x0, plot.y0), (plot.x0, plot.y1)],
                  fill=theme.COLORS['axis'])
        if len(counts):
            peak = max(max(int(c) for c in counts), 1)
            bar_width = max(1.0, plot.width / len(counts))
            for index, count in enumerate(counts):
                if count <= 0:
                    continue
                height = (count / peak) * plot.height
                bx0 = plot.x0 + index * bar_width
                draw.rectangle([bx0, plot.y1 - height,
                                bx0 + max(bar_width - 0.4, 1.0), plot.y1],
                               fill=theme.COLORS['bar'])
            draw.text((plot.x0 - layout._d(6), plot.y0), str(int(peak)),
                      font=fonts.font(10), fill=theme.COLORS['muted'],
                      anchor='rt')
            draw.text((plot.x0 - layout._d(6), plot.y1 - layout._d(2)), '0',
                      font=fonts.font(10), fill=theme.COLORS['muted'],
                      anchor='rt')
            draw.text((plot.x0, plot.y0 - layout._d(14)), self.t('hist.y'),
                      font=fonts.font(10), fill=theme.COLORS['muted'])
        for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
            value = lo + (hi - lo) * fraction
            tx = plot.x0 + fraction * plot.width
            draw.line([(tx, plot.y1), (tx, plot.y1 + layout._d(3))],
                      fill=theme.COLORS['axis'])
            draw.text((tx, plot.y1 + layout._d(5)), self._fmt_value(value),
                      font=fonts.font(10), fill=theme.COLORS['muted'],
                      anchor='mt')
        draw.text(((plot.x0 + plot.x1) / 2.0, plot.y1 + layout._d(18)),
                  self.t('hist.x'), font=fonts.font(10),
                  fill=theme.COLORS['sub'], anchor='mt')
        draw.text(layout.hist_note, self.t('hist.note'),
                  font=fonts.font(10), fill=theme.COLORS['muted'])

    # --------------------------------------------------------------- export
    def _paint_export(self, draw: ImageDraw.ImageDraw,
                      state: SkinState) -> None:
        layout = self.layout
        fonts = self.fonts
        draw.text(layout.export_heading, self.t('export.section'),
                  font=fonts.font(15, bold=True), fill=theme.COLORS['ink'])
        for index, name in enumerate(('image', 'field', 'raw')):
            card = layout.export_card_rect(index)
            self._card(draw, card)
            # FontBook scales its argument again, so this design-size icon is
            # already double scaled; dividing by the supersample factor keeps
            # it at the same on-screen size in the 2x pass.
            icon_size = layout._d(24) / max(1, self.supersample)
            draw.text((card.x0 + layout._d(22),
                       card.y0 + layout._d(14)),
                      theme.ICONS[('image', 'table', 'points')[index]],
                      font=fonts.font(icon_size, icons=True),
                      fill=theme.COLORS['ink'])
            draw.text((card.x0 + layout._d(82), card.y0 + layout._d(10)),
                      self.t(f'export.{name}.title'),
                      font=fonts.font(15, bold=True), fill=theme.COLORS['ink'])
            draw.text((card.x0 + layout._d(82), card.y0 + layout._d(36)),
                      self.t(f'export.{name}.line1'),
                      font=fonts.font(13), fill=theme.COLORS['sub'])
            draw.text((card.x0 + layout._d(82), card.y0 + layout._d(59)),
                      self.t(f'export.{name}.line2'),
                      font=fonts.font(13), fill=theme.COLORS['sub'])
        card = layout.export_dir_card
        self._card(draw, card)
        draw.text(layout.export_dir_title, self.t('export.dir_title'),
                  font=fonts.font(14, bold=True), fill=theme.COLORS['ink'])
        draw.rounded_rectangle(layout.export_path_box.as_int(), radius=5,
                               fill=theme.COLORS['white'],
                               outline=(206, 219, 235), width=1)
        self._button(draw, layout.export_browse, self.t('export.browse'))

    # ----------------------------------------------------------- status bar
    def _paint_status_frame(self, draw: ImageDraw.ImageDraw,
                            state: SkinState) -> None:
        bar = self.layout.status_bar
        draw.rectangle(bar.as_int(), fill=(247, 250, 253))
        draw.line([(bar.x0, bar.y0), (bar.x1, bar.y0)],
                  fill=theme.COLORS['border'])

    # -------------------------------------------------------------- dynamic
    def _draw_widgets(self, draw: ImageDraw.ImageDraw,
                      state: SkinState) -> None:
        widgets = state.widgets
        layout = self.layout
        fonts = self.fonts
        if widgets is None:
            return
        for widget in widgets:
            kind = widget.kind
            state_name = widget.state()
            if kind == 'button':
                self._toolbar_button(draw, widget, state_name)
            elif kind == 'icon':
                self._icon_button(draw, widget, state_name)
            elif kind == 'segment':
                self._segment(draw, widget, state_name)
            elif kind == 'radio':
                self._radio(draw, widget)
            elif kind == 'checkbox':
                self._checkbox(draw, widget)
            elif kind == 'value':
                self._value_box(draw, widget.rect, '')
            elif kind == 'card' and state_name == 'hover':
                draw.rounded_rectangle(widget.rect.as_int(), radius=RADIUS,
                                       outline=theme.COLORS['card_border_hover'],
                                       width=2)
            elif kind == 'path' and state_name == 'hover':
                draw.rounded_rectangle(widget.rect.as_int(), radius=5,
                                       outline=theme.COLORS['primary'], width=1)
            elif kind == 'browse':
                self._button(draw, widget.rect, self.t('export.browse'),
                             hovered=state_name == 'hover',
                             pressed=state_name == 'pressed')
        del fonts

    def _toolbar_button(self, draw: ImageDraw.ImageDraw, widget,
                        state_name: str) -> None:
        layout = self.layout
        fonts = self.fonts
        rect = widget.rect
        if state_name == 'hover':
            fill, ink = (235, 243, 255), theme.COLORS['primary']
        else:
            fill, ink = (248, 250, 252), theme.COLORS['toolbar_ink']
        draw.rounded_rectangle(rect.as_int(), radius=7, fill=fill,
                               outline=(210, 223, 237), width=1)
        label = self.t(widget.label_key)
        name = widget.id.split('.')[-1]
        reset = widget.icon == 'reset'
        size = 16 if reset else 13
        gap = layout._d(12 if reset else 8)
        icon_w = layout._d(23 if reset else 18)
        tw = fonts.measure(label, size)
        total = tw + icon_w + gap
        x = rect.cx - total / 2.0
        if reset:
            self._refresh_icon(draw, x, rect.cy - 1, ink, icon_w)
        else:
            self._folder_icon(draw, x, rect.cy - 1, ink, opened=name == 'load')
        x += icon_w + gap
        draw.text((x, rect.cy - 2), label, font=fonts.font(size), fill=ink,
                  anchor='lm')

    def _folder_icon(self, draw: ImageDraw.ImageDraw, x: float, cy: float,
                     ink, *, opened: bool = False) -> None:
        """Draw a thin outline folder icon (the MDL2 folder glyph is missing
        in some Windows builds, where E8B7 renders as a blank page).

        The mark is painted from primitives in design units, so every offset is
        multiplied by the layout scale; a hard-coded 18 px icon next to DPI
        scaled toolbar text reads as a clipped stub on a 200 % window.
        """
        unit = self.layout._d(1.0)
        x = float(x)
        w = 18.0 * unit
        top = cy - 7.5 * unit
        bottom = cy + 6.5 * unit
        width = max(1, int(round(unit)))
        draw.rounded_rectangle([x, top + 3.0 * unit, x + 8.0 * unit,
                                top + 6.0 * unit],
                               radius=1.2 * unit, outline=ink, width=width)
        draw.rounded_rectangle([x, top + 4.5 * unit, x + w, bottom],
                               radius=2.0 * unit, outline=ink, width=width)
        if opened:
            draw.line([(x + 4.5 * unit, top + 9.0 * unit),
                       (x + 8.5 * unit, top + 5.8 * unit),
                       (x + 12.5 * unit, top + 9.0 * unit)],
                      fill=ink, width=width)

    @staticmethod
    def _refresh_icon(draw: ImageDraw.ImageDraw, x: float, cy: float, ink,
                      size: float) -> None:
        """Two-arrow circular refresh icon (the design's "reset view" mark)."""
        radius = size / 2.0 - 1.0
        cx = x + size / 2.0
        cy = float(cy)
        box = [cx - radius, cy - radius, cx + radius, cy + radius]
        unit = max(0.5, size / 18.0)
        width = max(1, min(4, int(round(2.0 * unit))))
        for start, end in ((30.0, 150.0), (210.0, 330.0)):
            draw.arc(box, start=start, end=end, fill=ink, width=width)
            angle = math.radians(end)
            px = cx + radius * math.cos(angle)
            py = cy + radius * math.sin(angle)
            tx, ty = -math.sin(angle), math.cos(angle)
            nx, ny = math.cos(angle), math.sin(angle)
            draw.polygon([(px + tx * 4.5 * unit, py + ty * 4.5 * unit),
                          (px - nx * 3.0 * unit, py - ny * 3.0 * unit),
                          (px + nx * 3.0 * unit, py + ny * 3.0 * unit)], fill=ink)

    def _icon_button(self, draw: ImageDraw.ImageDraw, widget,
                     state_name: str) -> None:
        rect = widget.rect
        draw.ellipse(rect.as_int(), fill=theme.COLORS['primary'])
        draw.text(rect.center, '\ue897',
                  font=self.fonts.font(13, icons=True),
                  fill=theme.COLORS['white'], anchor='mm')

    def _segment(self, draw: ImageDraw.ImageDraw, widget, state_name: str) -> None:
        rect = widget.rect
        if widget.group == 'language':
            self._language_segment(draw, widget, state_name)
            return
        split = int(self.layout.segment_rects[0].x1)
        if widget.checked:
            fill, ink = theme.COLORS['primary'], (255, 255, 255)
            if state_name == 'hover':
                fill = theme.COLORS['primary_dark']
        else:
            fill = (251, 252, 252) if state_name != 'hover' else (238, 244, 255)
            ink = theme.COLORS['ink_strong']
        x0, y0, x1, y1 = rect.as_int()
        draw.rounded_rectangle([x0, y0, x1, y1], radius=8, fill=fill)
        if widget.checked:
            # the design joins the two halves with a straight edge
            if x1 <= split:
                draw.rectangle([x1 - 8, y0 + 1, x1, y1 - 1], fill=fill)
            else:
                draw.rectangle([x0, y0 + 1, x0 + 8, y1 - 1], fill=fill)
        draw.text(rect.center, widget.label or self.t(widget.label_key),
                  font=self.fonts.font(15, bold=widget.checked), fill=ink,
                  anchor='mm')

    def _language_segment(self, draw: ImageDraw.ImageDraw, widget,
                          state_name: str) -> None:
        layout = self.layout
        container = layout.lang_rect
        x0, y0, x1, y1 = container.as_int()
        split = int(layout.lang_split_x)
        if widget.value == 'zh-CN':
            left, right = x0, split
        else:
            left, right = split, x1
        # The two halves are separate widgets but share one pill.  Each one
        # paints only its own half and then the shared outline: painting the
        # whole pill for the checked half (the old code) erased the already
        # drawn label of the other half.
        if widget.checked:
            fill = theme.COLORS['primary']
        elif state_name == 'hover':
            fill = (238, 245, 255)
        else:
            fill = theme.COLORS['white']
        radii = ((True, False, False, True) if widget.value == 'zh-CN'
                 else (False, True, True, False))
        draw.rounded_rectangle([left, y0, right, y1], radius=8, fill=fill,
                               corners=radii)
        draw.rounded_rectangle([x0, y0, x1, y1], radius=8,
                               outline=(211, 224, 238), width=1)
        ink = (255, 255, 255) if widget.checked else theme.COLORS['ink_strong']
        draw.text(((left + right) / 2.0, (y0 + y1) / 2.0),
                  widget.label or self.t(widget.label_key),
                  font=self.fonts.font(16, bold=widget.checked),
                  fill=ink, anchor='mm')

    def _radio(self, draw: ImageDraw.ImageDraw, widget) -> None:
        size = self.layout.radio_size
        x = self.layout.sidebar_radio_x
        y = widget.rect.y0 + (widget.rect.height - size) / 2.0
        radius = size / 2.0
        center = (x + radius, y + radius)
        if widget.checked:
            draw.ellipse([x, y, x + size, y + size],
                         fill=theme.COLORS['primary'])
            inner = radius * 0.32
            draw.ellipse([center[0] - inner, center[1] - inner,
                          center[0] + inner, center[1] + inner],
                         fill=(255, 255, 255))
        else:
            draw.ellipse([x, y, x + size, y + size], outline=(186, 199, 216),
                         width=2, fill=(255, 255, 255))

    def _checkbox(self, draw: ImageDraw.ImageDraw, widget) -> None:
        # The clickable rect carries the label too; the tick box is the
        # widget's own box rectangle when one is given (export row, header
        # haze toggle).
        box = widget.box or self.layout.export_checkbox
        x0, y0, x1, y1 = box.as_int()
        off = 1 if widget.disabled else 0
        if widget.checked:
            fill, outline = theme.COLORS['check'], None
        elif widget.disabled:
            fill, outline = theme.COLORS['strip'], (206, 216, 229)
        else:
            fill, outline = (255, 255, 255), (186, 199, 216)
        draw.rounded_rectangle([x0, y0, x1, y1], radius=3,
                               fill=fill, outline=outline, width=1)
        if widget.checked:
            tick = (255, 255, 255) if not widget.disabled else (240, 244, 250)
            draw.line([x0 + 4, (y0 + y1) // 2, x0 + 7, y1 - 4],
                      fill=tick, width=2)
            draw.line([x0 + 7, y1 - 4, x1 - 4, y0 + 4], fill=tick, width=2)
        label = self.t(widget.label_key) if widget.label_key else ''
        if label:
            ink = (theme.COLORS['muted'] if widget.disabled
                   else theme.COLORS['ink'])
            draw.text((x1 + self.layout._d(8) - off, (y0 + y1) / 2.0),
                      label, font=self.fonts.font(13), fill=ink, anchor='lm')

    def _draw_depth(self, draw: ImageDraw.ImageDraw, state: SkinState,
                    image: Image.Image) -> None:
        layout = self.layout
        fonts = self.fonts
        depth = state.depth
        text = f'{depth:.3f}' if depth is not None else '--'
        if state.editing_depth and state.widgets is not None:
            widget = state.widgets.get('depth.value')
            if widget is not None:
                text = widget.text or '_'
        focus = (state.editing_depth and state.widgets is not None
                 and state.widgets.focus_id == 'depth.value')
        self._value_box(draw, layout.slider_value_box, text, focused=focus)
        heff = self.t('card.heff', depth=depth) if depth is not None else 'heff = --'
        for metric in ('Er', 'H'):
            pill = layout.plot_pills[metric]
            self._pill(draw, pill, heff)
        self._slider(draw, state)
        self._info_strip(draw, state)
        thumb = state.thumb
        frame = layout.thumb_rect
        if thumb is not None:
            thumb = thumb.convert('RGB')
            # Keep the measured X-Y aspect: letterbox inside the frame instead
            # of stretching the slice to the panel shape.
            scale = min(frame.width / max(thumb.width, 1),
                        frame.height / max(thumb.height, 1))
            size = (max(1, int(round(thumb.width * scale))),
                    max(1, int(round(thumb.height * scale))))
            pasted = thumb.resize(size, Image.NEAREST)
            image.paste(pasted, (int(frame.x0 + (frame.width - size[0]) / 2),
                                 int(frame.y0 + (frame.height - size[1]) / 2)))
        draw.rectangle(frame.as_int(), outline=theme.COLORS['border'])

    def _slider(self, draw: ImageDraw.ImageDraw, state: SkinState) -> None:
        layout = self.layout
        track = layout.slider_track
        y = (track.y0 + track.y1) / 2.0
        lo, hi = state.depth_lo, state.depth_hi
        fraction = layout.slider_fraction(state.depth, lo, hi)
        draw.line([(track.x0, y), (track.x1, y)], fill=theme.COLORS['track'],
                  width=4)
        draw.line([(track.x0, y), (layout.slider_x(fraction), y)],
                  fill=theme.COLORS['primary'], width=4)
        knob = layout.slider_x(fraction)
        radius = layout._d(8)
        draw.ellipse([knob - radius, y - radius, knob + radius, y + radius],
                     fill=theme.COLORS['primary'], outline=(255, 255, 255),
                     width=2)
        draw.text((track.x0, y + layout._d(10)), self._fmt_value(lo),
                  font=self.fonts.font(10), fill=theme.COLORS['muted'])
        draw.text((track.x1 - layout._d(16), y + layout._d(10)),
                  self._fmt_value(hi), font=self.fonts.font(10),
                  fill=theme.COLORS['muted'])

    def _info_strip(self, draw: ImageDraw.ImageDraw, state: SkinState) -> None:
        layout = self.layout
        fonts = self.fonts
        strip = layout.info_strip
        y = strip.cy
        items = [self.t('depth.strip.index', idx=state.slice_index,
                        count=state.slice_count),
                 self.t('depth.strip.step', dz=state.step_nm),
                 self.t('depth.strip.view')]
        x = strip.x0 + layout._d(10)
        for index, text in enumerate(items):
            draw.text((x, y), text, font=fonts.font(12),
                      fill=theme.COLORS['sub'], anchor='lm')
            x += fonts.measure(text, 12) + layout._d(14)
            if index < len(items) - 1:
                draw.line([(x - layout._d(7), y - layout._d(7)),
                           (x - layout._d(7), y + layout._d(7))],
                          fill=theme.COLORS['divider'])

    def _draw_histogram_marker(self, draw: ImageDraw.ImageDraw,
                               state: SkinState) -> None:
        layout = self.layout
        plot = layout.hist_plot
        fraction = layout.slider_fraction(state.depth, state.depth_lo,
                                          state.depth_hi)
        x = plot.x0 + fraction * plot.width
        draw.line([(x, plot.y0), (x, plot.y1)], fill=theme.COLORS['red'],
                  width=2)
        tx = min(x + layout._d(4), plot.x1 - layout._d(52))
        draw.text((tx, plot.y0 + layout._d(2)), self.t('hist.marker'),
                  font=self.fonts.font(10), fill=theme.COLORS['red'])

    def _draw_export_dir_values(self, draw: ImageDraw.ImageDraw,
                                state: SkinState) -> None:
        layout = self.layout
        box = layout.export_path_box
        text = state.export_dir or ''
        max_width = box.width - layout._d(20)
        while text and self.fonts.measure(text, 13) > max_width:
            text = text[:-4] + '...' if len(text) > 6 else ''
        draw.text((box.x0 + layout._d(10), box.cy), text,
                  font=self.fonts.font(13), fill=theme.COLORS['ink'],
                  anchor='lm')

    def _draw_status(self, draw: ImageDraw.ImageDraw, state: SkinState) -> None:
        layout = self.layout
        fonts = self.fonts
        dot = layout.status_dot.as_int()
        draw.ellipse(dot, fill=theme.COLORS['green'] if state.data_ready
                     else theme.COLORS['muted'])
        x, y = layout.status_text
        y += layout._d(2)
        for index, text in enumerate(state.status_items):
            draw.text((x, y), text, font=fonts.font(13),
                      fill=theme.COLORS['sub'], anchor='lm')
            x += fonts.measure(text, 13) + layout._d(18)
            if index < len(state.status_items) - 1:
                draw.line([(x - layout._d(9), y - layout._d(8)),
                           (x - layout._d(9), y + layout._d(8))],
                          fill=theme.COLORS['divider'])
        version = self.t('status.version', version=state.version)
        draw.text((layout.width - layout._d(20), y), version,
                  font=fonts.font(12), fill=theme.COLORS['sub'], anchor='rm')

    def _draw_notice(self, draw: ImageDraw.ImageDraw, state: SkinState) -> None:
        if not state.notice:
            return
        layout = self.layout
        draw.text((layout.notice_bar.x0, layout.notice_bar.cy),
                  self._fit_notice(state.notice),
                  font=self.fonts.font(12),
                  fill=theme.COLORS['red'] if state.notice_is_error
                  else theme.COLORS['notice'], anchor='lm')

    def _fit_notice(self, text: str) -> str:
        """Single-line notice clipped with an ellipsis inside its own bar."""
        layout = self.layout
        max_width = layout.notice_bar.width - layout._d(20)
        text = str(text or '')
        while text and self.fonts.measure(text, 12) > max_width:
            text = text[:-4] + '...' if len(text) > 6 else ''
        return text

    # ------------------------------------------------------------- primitives
    def _ellipsize(self, text: str, base_size: float,
                   max_width: float) -> str:
        """Trim a single-line sidebar value with an ellipsis at a word break."""
        text = str(text or '')
        font = self.fonts.font(base_size)
        if max_width <= 0 or font.getlength(text) <= max_width:
            return text
        trimmed = text
        while trimmed and font.getlength(trimmed + '...') > max_width:
            trimmed = trimmed[:-1]
        space = trimmed.rfind(' ')
        if space >= 0:
            trimmed = trimmed[:space]
        return trimmed.rstrip(' ,;:/') + '...'

    def _wrap_value(self, text: str, base_size: float,
                    max_width: float) -> list[str]:
        """Split a sidebar value into at most two lines, ellipsising the tail."""
        text = str(text or '')
        font = self.fonts.font(base_size)
        if max_width <= 0 or font.getlength(text) <= max_width:
            return [text]
        lines: list[str] = []
        current = ''
        for char in text:
            if current and font.getlength(current + char) > max_width:
                lines.append(current)
                current = ''
            current += char
        if current:
            lines.append(current)
        if len(lines) > 2:
            lines = lines[:2]
            while lines[1] and font.getlength(lines[1] + '...') > max_width:
                lines[1] = lines[1][:-1]
            lines[1] = lines[1].rstrip() + '...'
        return lines

    def _card(self, draw: ImageDraw.ImageDraw, rect, radius: int = RADIUS,
              shadow: bool = True) -> None:
        x0, y0, x1, y1 = rect.as_int()
        if shadow:
            draw.rounded_rectangle([x0 + 1, y0 + 2, x1 + 1, y1 + 2],
                                   radius=radius, fill=theme.COLORS['shadow'])
        draw.rounded_rectangle([x0, y0, x1, y1], radius=radius,
                               fill=theme.COLORS['card'],
                               outline=theme.COLORS['border'], width=1)

    def _pill(self, draw: ImageDraw.ImageDraw, rect, text: str) -> None:
        if not text:
            return
        draw.rounded_rectangle(rect.as_int(), radius=7,
                               fill=(249, 251, 254),
                               outline=theme.COLORS['border'], width=1)
        draw.text((rect.x0 + self.layout._d(12), rect.cy), text,
                  font=self.fonts.font(12), fill=theme.COLORS['ink'],
                  anchor='lm')

    def _value_box(self, draw: ImageDraw.ImageDraw, rect,
                   text: str, *, focused: bool = False) -> None:
        draw.rounded_rectangle(rect.as_int(), radius=4, fill=(255, 255, 255),
                               outline=theme.COLORS['primary'] if focused
                               else (206, 219, 235),
                               width=2 if focused else 1)
        if text:
            draw.text((rect.x0 + self.layout._d(10), rect.cy), text,
                      font=self.fonts.font(13), fill=theme.COLORS['ink'],
                      anchor='lm')

    def _button(self, draw: ImageDraw.ImageDraw, rect, label: str, *,
                hovered: bool = False, pressed: bool = False) -> None:
        fill = (238, 244, 255) if (hovered or pressed) else (255, 255, 255)
        draw.rounded_rectangle(rect.as_int(), radius=6, fill=fill,
                               outline=(214, 224, 238), width=1)
        draw.text(rect.center, label, font=self.fonts.font(12),
                  fill=theme.COLORS['ink'], anchor='mm')
