"""Startup empty state: open a source without constructing a dataset.

The normal viewer requires a precomputed field and cannot exist without one.
This window is therefore a deliberately small 2-D surface: it paints the same
palette and fonts into a full-window bitmap and returns an ``(action, value)``
pair to :class:`app.app.Application`.  It owns no Dataset/FieldGrid and never
touches the stored field or exports.

Actions returned by ``show()``:
    ('open_dir', folder) or ('open_result', path); None means the user closed it.
"""
from __future__ import annotations

import time

import numpy as np
import pyvista as pv
import vtk
from PIL import Image, ImageDraw
from vtk.util.numpy_support import numpy_to_vtk

from gui import i18n, interaction, theme, window_policy
from integration.protocols import APP_TITLE, APP_VERSION

Rect = tuple[float, float, float, float]

# Segoe MDL2: E8BA is the closed folder, E8A7 the folder with an out-arrow.
# (theme.ICONS keeps the older mapping for the main toolbar; this screen uses
# the clearer pair for the two different open actions.)
ICON_FOLDER = '\ue8ba'
ICON_OPEN_FOLDER = '\ue8a7'


class WelcomeWindow:
    """The no-data window shown before the first dataset is opened."""

    DESIGN = (1536.0, 992.0)
    HEADER = 70.0
    STATUS = 30.0
    CARD = (760.0, 320.0)

    def __init__(self, *, off_screen: bool = False,
                 window_size: tuple[int, int] = (1536, 992),
                 language: str | None = None, settings_path=None,
                 ui_scale: float = 1.0,
                 auto_close_ms: int = 0) -> None:
        self.off_screen = bool(off_screen)
        self.ui_scale = max(0.5, min(float(ui_scale or 1.0), 3.0))
        self.auto_close_ms = max(0, int(auto_close_ms or 0))
        self.window_size = theme.clamp_window_size(
            window_size,
            scale=1.0 if self.off_screen else theme.system_ui_scale(),
            minimum=not self.off_screen,
            maximum=not self.off_screen)
        self.language_settings = i18n.LanguageSettings(settings_path)
        self.font_file = i18n.find_cjk_font()
        self.language = (i18n.normalize_language(language)
                         or self.language_settings.load())
        if self.language == 'zh-CN' and not self.font_file:
            self.language = 'en'

        self.pending_action: tuple[str, str] | None = None
        self.closed_by_user = False
        self._open_data_callback = None
        self._open_result_callback = None
        self._hover: str | None = None
        self._pressed: str | None = None
        self._buttons: dict[str, Rect] = {}
        self._labels: dict[str, str] = {}
        self._image: Image.Image | None = None
        self._shown_at = 0.0
        self._window_policy = None
        self._render_observer_id = None
        self._timer_observer = None
        self._timer_id = None

        self.plotter = pv.Plotter(
            off_screen=self.off_screen, window_size=self.window_size,
            border=False)
        self.plotter.set_background(theme.hex_color('page_bottom'))
        self.plotter.title = self._window_title()
        self.renderer = self.plotter.renderers[0]
        self.renderer.SetInteractive(False)
        self.renderer.SetLayer(0)
        self.renderer.SetViewport(0.0, 0.0, 1.0, 1.0)

        image = vtk.vtkImageData()
        width, height = self._client_size()
        image.SetDimensions(width, height, 1)
        mapper = vtk.vtkImageMapper()
        mapper.SetInputData(image)
        mapper.SetColorWindow(255.0)
        mapper.SetColorLevel(127.5)
        actor = vtk.vtkActor2D()
        actor.SetMapper(mapper)
        actor.SetPosition(0.0, 0.0)
        actor.SetPickable(False)
        self.renderer.AddActor(actor)
        self.renderer.AddObserver('StartEvent', self._pin_camera)
        self._image_data = image
        self._image_actor = actor

        self._pin_camera()
        self._paint(render=False)
        if not self.off_screen:
            self._install_interactions()
            self._render_observer_id = self.plotter.render_window.AddObserver(
                'RenderEvent', self._on_first_render)
        self.plotter.render()

    # ------------------------------------------------------------------ i18n
    @property
    def _language(self) -> str:
        return getattr(self, 'language', 'en') or 'en'

    def _t(self, key: str, **kwargs) -> str:
        return i18n.tr(self._language, key, **kwargs)

    def _window_title(self) -> str:
        return f'{APP_TITLE} | {self._t("welcome.window_title")}'

    # ---------------------------------------------------------------- layout
    def _client_size(self) -> tuple[int, int]:
        try:
            width, height = self.plotter.render_window.GetSize()
        except Exception:  # noqa: BLE001 - before the window exists
            width, height = self.window_size
        if not width or not height:
            width, height = self.window_size
        return int(width), int(height)

    def _geometry(self, width: int, height: int,
                  fonts: theme.FontBook) -> dict[str, object]:
        scale = min(width / self.DESIGN[0], height / self.DESIGN[1])
        scale = max(scale, 0.1)
        margin = 24.0 * scale
        header_h = self.HEADER * scale
        status_h = self.STATUS * scale

        top_h = 36.0 * scale
        top_y = (header_h - top_h) / 2.0
        lang_h = 28.0 * scale
        lang_y = (header_h - lang_h) / 2.0
        lang_zh_w = fonts.measure('中文', 9.5) + 18.0 * scale
        lang_en_w = fonts.measure('English', 9.5) + 18.0 * scale
        lang_en = (width - margin - lang_en_w, lang_y,
                   width - margin, lang_y + lang_h)
        lang_zh = (lang_en[0] - 4.0 * scale - lang_zh_w, lang_y,
                   lang_en[0] - 4.0 * scale, lang_y + lang_h)

        def top_button_width(key: str) -> float:
            return (12.0 * scale + 8.0 * scale
                    + fonts.measure(self._t(key), 10.0) + 24.0 * scale)

        open_result_w = top_button_width('toolbar.open_result')
        open_results = (lang_zh[0] - 10.0 * scale - open_result_w, top_y,
                        lang_zh[0] - 10.0 * scale, top_y + top_h)
        open_data_w = top_button_width('toolbar.open')
        open_data = (open_results[0] - 10.0 * scale - open_data_w, top_y,
                     open_results[0] - 10.0 * scale, top_y + top_h)

        card_w = min(self.CARD[0] * scale, width - 2.0 * 64.0 * scale)
        card_h = min(self.CARD[1] * scale,
                     height - header_h - status_h - 2.0 * 40.0 * scale)
        card_x = (width - card_w) / 2.0
        card_y = (header_h + (height - header_h - status_h - card_h) / 2.0)
        card = (card_x, card_y, card_x + card_w, card_y + card_h)

        button_h = 46.0 * scale
        button_w = min(270.0 * scale,
                       (card_w - 2.0 * 40.0 * scale - 20.0 * scale) / 2.0)
        button_y = card_y + card_h - 112.0 * scale
        primary = (card_x + card_w / 2.0 - button_w - 10.0 * scale,
                   button_y, card_x + card_w / 2.0 - 10.0 * scale,
                   button_y + button_h)
        secondary = (card_x + card_w / 2.0 + 10.0 * scale, button_y,
                     card_x + card_w / 2.0 + 10.0 * scale + button_w,
                     button_y + button_h)

        return {
            'scale': scale, 'margin': margin, 'header_h': header_h,
            'status_h': status_h, 'card': card, 'primary': primary,
            'secondary': secondary, 'open_data': open_data,
            'open_result': open_results, 'lang_zh': lang_zh,
            'lang_en': lang_en,
        }

    # ---------------------------------------------------------------- paint
    def _paint(self, render: bool = True) -> Image.Image:
        width, height = self._client_size()
        scale = max(min(width / self.DESIGN[0], height / self.DESIGN[1]), 0.1)
        font_scale = max(0.7, min(scale * self.ui_scale, 2.0))
        fonts = theme.FontBook(font_scale)
        image = Image.new('RGB', (width, height), theme.color('page_bottom'))
        draw = ImageDraw.Draw(image)

        top = theme.color('page_top')
        bottom = theme.color('page_bottom')
        for y in range(height):
            t = y / max(1, height - 1)
            fill = tuple(int(round(top[i] + (bottom[i] - top[i]) * t))
                         for i in range(3))
            draw.line([(0, y), (width, y)], fill=fill)

        g = self._geometry(width, height, fonts)
        scale = float(g['scale'])
        margin = float(g['margin'])
        header_h = float(g['header_h'])
        status_h = float(g['status_h'])
        card = g['card']
        primary = g['primary']
        secondary = g['secondary']
        open_data = g['open_data']
        open_result = g['open_result']
        lang_zh = g['lang_zh']
        lang_en = g['lang_en']

        draw.line([(0, header_h), (width, header_h)],
                  fill=theme.color('divider_soft'), width=1)

        logo_font = fonts.font(22.0, bold=True)
        logo_y = header_h / 2.0
        draw.text((margin, logo_y), APP_TITLE, font=logo_font,
                  fill=theme.color('ink_strong'), anchor='lm')
        tagline = self._t('toolbar.tagline')
        tag_x = margin + fonts.measure(APP_TITLE, 22.0, bold=True) + 16.0 * scale
        tag_font = fonts.font(9.0)
        if (open_data[0] - 24.0 * scale - tag_x
                >= fonts.measure(tagline, 9.0)):
            draw.text((tag_x, logo_y + 1.0 * scale), tagline,
                      font=tag_font, fill=theme.color('tagline'), anchor='lm')

        labels = {
            'open_data': self._t('toolbar.open'),
            'open_result': self._t('toolbar.open_result'),
        }
        self._buttons = {
            'open_data': open_data,
            'open_result': open_result,
            'lang.zh': lang_zh,
            'lang.en': lang_en,
            'card.open_data': primary,
            'card.open_result': secondary,
        }
        self._labels = dict(labels)
        self._draw_top_button(draw, open_data, labels['open_data'],
                              ICON_FOLDER, fonts, scale,
                              self._hover == 'open_data',
                              self._pressed == 'open_data')
        self._draw_top_button(draw, open_result, labels['open_result'],
                              ICON_OPEN_FOLDER, fonts, scale,
                              self._hover == 'open_result',
                              self._pressed == 'open_result')
        self._draw_language(draw, lang_zh, '中文', 'zh-CN', fonts, scale)
        self._draw_language(draw, lang_en, 'English', 'en', fonts, scale)

        # Card shadow and card.
        shadow_off = 4.0 * scale
        draw.rounded_rectangle(
            [card[0] + 2.0 * scale, card[1] + shadow_off,
             card[2] + 2.0 * scale, card[3] + shadow_off],
            radius=14.0 * scale, fill=theme.color('shadow'))
        draw.rounded_rectangle(
            [card[0], card[1], card[2], card[3]],
            radius=14.0 * scale, fill=theme.color('card'),
            outline=theme.color('border'), width=max(1, int(round(scale))))
        cx = (card[0] + card[2]) / 2.0
        icon_r = 28.0 * scale
        icon_cy = card[1] + 52.0 * scale
        draw.ellipse([cx - icon_r, icon_cy - icon_r,
                      cx + icon_r, icon_cy + icon_r],
                     fill=theme.color('primary_soft'))
        draw.text((cx, icon_cy), ICON_OPEN_FOLDER,
                  font=fonts.font(26.0, icons=True),
                  fill=theme.color('primary'), anchor='mm')

        title_font = fonts.font(20.0, bold=True)
        title_y = card[1] + 104.0 * scale
        draw.text((cx, title_y), self._t('welcome.title'),
                  font=title_font, fill=theme.color('ink_strong'),
                  anchor='mm')
        body_font = fonts.font(10.5)
        body_lines = self._wrap(draw, self._t('welcome.subtitle'),
                                body_font, card[2] - card[0] - 80.0 * scale)
        body_y = title_y + 30.0 * scale
        for line in body_lines:
            draw.text((cx, body_y), line, font=body_font,
                      fill=theme.color('sub'), anchor='mm')
            body_y += fonts.line_height(10.5)

        card_labels = {
            'open_data': self._t('welcome.open_data'),
            'open_result': self._t('welcome.open_result'),
        }
        self._labels.update(card_labels)
        self._draw_card_button(
            draw, primary, card_labels['open_data'], ICON_FOLDER,
            fonts, scale, True,
            self._hover in ('open_data', 'card.open_data'),
            self._pressed in ('open_data', 'card.open_data'))
        self._draw_card_button(
            draw, secondary, card_labels['open_result'],
            ICON_OPEN_FOLDER, fonts, scale, False,
            self._hover in ('open_result', 'card.open_result'),
            self._pressed in ('open_result', 'card.open_result'))

        hint_font = fonts.font(8.5)
        hint_lines = self._wrap(draw, self._t('welcome.hint'), hint_font,
                                card[2] - card[0] - 80.0 * scale)
        hint_y = card[3] - 28.0 * scale
        for line in reversed(hint_lines):
            draw.text((cx, hint_y), line, font=hint_font,
                      fill=theme.color('muted'), anchor='mm')
            hint_y -= fonts.line_height(8.5)

        line_y = height - status_h
        draw.line([(0, line_y), (width, line_y)],
                  fill=theme.color('divider_soft'), width=1)
        status_font = fonts.font(8.5)
        draw.text((margin, line_y + status_h / 2.0),
                  self._t('welcome.status'), font=status_font,
                  fill=theme.color('muted'), anchor='lm')
        draw.text((width - margin, line_y + status_h / 2.0),
                  self._t('status.version', version=APP_VERSION),
                  font=status_font, fill=theme.color('muted'), anchor='rm')

        self._image = image
        self._upload(image)
        if render:
            self.plotter.render()
        return image

    # ------------------------------------------------------------- primitives
    @staticmethod
    def _wrap(draw, text: str, font, max_width: float) -> list[str]:
        lines: list[str] = []
        line = ''
        for token in str(text).split(' '):
            candidate = (line + ' ' + token).strip()
            if draw.textlength(candidate, font=font) <= max_width:
                line = candidate
                continue
            if line:
                lines.append(line)
                line = ''
            for ch in token:
                trial = line + ch
                if not line or draw.textlength(trial, font=font) <= max_width:
                    line = trial
                else:
                    lines.append(line)
                    line = ch
        if line:
            lines.append(line)
        return lines or ['']

    def _draw_top_button(self, draw, rect: Rect, label: str, icon: str,
                         fonts: theme.FontBook, scale: float,
                         hover: bool, pressed: bool) -> None:
        fill = theme.color('primary_soft') if hover else theme.color('white')
        border = (theme.color('primary') if hover
                  else theme.color('divider'))
        draw.rounded_rectangle([rect[0], rect[1], rect[2], rect[3]],
                               radius=8.0 * scale, fill=fill,
                               outline=border,
                               width=max(1, int(round(scale))))
        icon_font = fonts.font(12.0, icons=True)
        text_font = fonts.font(10.0)
        icon_w = draw.textlength(icon, font=icon_font)
        text_w = draw.textlength(label, font=text_font)
        gap = 8.0 * scale
        total = icon_w + gap + text_w
        x = (rect[0] + rect[2]) / 2.0 - total / 2.0
        cy = (rect[1] + rect[3]) / 2.0
        draw.text((x, cy), icon, font=icon_font,
                  fill=theme.color('primary'), anchor='lm')
        draw.text((x + icon_w + gap, cy), label, font=text_font,
                  fill=theme.color('toolbar_ink'), anchor='lm')

    def _draw_language(self, draw, rect: Rect, label: str, value: str,
                       fonts: theme.FontBook, scale: float) -> None:
        selected = self.language == value
        fill = (theme.color('primary_soft') if selected
                else theme.color('white'))
        border = (theme.color('primary') if selected
                  else theme.color('divider'))
        draw.rounded_rectangle([rect[0], rect[1], rect[2], rect[3]],
                               radius=7.0 * scale, fill=fill,
                               outline=border,
                               width=max(1, int(round(scale))))
        draw.text(((rect[0] + rect[2]) / 2.0, (rect[1] + rect[3]) / 2.0),
                  label, font=fonts.font(9.5),
                  fill=(theme.color('primary') if selected
                        else theme.color('sub')), anchor='mm')

    def _draw_card_button(self, draw, rect: Rect, label: str, icon: str,
                          fonts: theme.FontBook, scale: float, primary: bool,
                          hover: bool, pressed: bool) -> None:
        fill = theme.color('primary_dark') if (primary and hover) else (
            theme.color('primary') if primary else (
                theme.color('primary_soft') if hover else theme.color('white')))
        border = (theme.color('primary') if primary
                  else theme.color('divider'))
        draw.rounded_rectangle([rect[0], rect[1], rect[2], rect[3]],
                               radius=9.0 * scale, fill=fill,
                               outline=border,
                               width=max(1, int(round(scale))))
        icon_font = fonts.font(13.0, icons=True)
        text_font = fonts.font(10.5, bold=True)
        icon_w = draw.textlength(icon, font=icon_font)
        text_w = draw.textlength(label, font=text_font)
        gap = 8.0 * scale
        total = icon_w + gap + text_w
        x = (rect[0] + rect[2]) / 2.0 - total / 2.0
        cy = (rect[1] + rect[3]) / 2.0
        ink = theme.color('white') if primary else theme.color('primary')
        draw.text((x, cy), icon, font=icon_font, fill=ink, anchor='lm')
        draw.text((x + icon_w + gap, cy), label, font=text_font,
                  fill=ink, anchor='lm')

    def _upload(self, image: Image.Image) -> None:
        # The window can be resized after construction (or opened at a larger
        # DPI size than the first probe).  The vtkImageData dimensions must
        # follow the bitmap, otherwise the mapper reinterprets the new scalars
        # with the old width/height and the window shows tiled/stretched
        # copies (or a black border) after a resize.
        width, height = image.size
        self._image_data.SetDimensions(width, height, 1)
        self._image_data.SetExtent(0, width - 1, 0, height - 1, 0, 0)
        array = np.asarray(image.convert('RGB'), dtype=np.uint8)[::-1]
        flat = np.ascontiguousarray(array.reshape(-1, 3))
        scalars = numpy_to_vtk(flat, deep=True,
                               array_type=vtk.VTK_UNSIGNED_CHAR)
        self._image_data.GetPointData().SetScalars(scalars)
        self._image_data.Modified()

    def _pin_camera(self, *_args) -> None:
        width, height = self._client_size()
        camera = self.renderer.GetActiveCamera()
        camera.ParallelProjectionOn()
        camera.SetPosition(width / 2.0, height / 2.0, 1.0)
        camera.SetFocalPoint(width / 2.0, height / 2.0, 0.0)
        camera.SetViewUp(0.0, 1.0, 0.0)
        camera.SetParallelScale(height / 2.0)

    # -------------------------------------------------------------- hit test
    def _point(self):
        iren = getattr(self.plotter, 'iren', None)
        if iren is None:
            return None
        try:
            x, y = iren.get_event_position()
        except Exception:  # noqa: BLE001 - no event position yet
            return None
        _, height = self._client_size()
        return float(x), float(height - y)

    def _hit(self, x: float, y: float) -> str | None:
        pad = 2.0
        for name, rect in self._buttons.items():
            if (rect[0] - pad <= x <= rect[2] + pad
                    and rect[1] - pad <= y <= rect[3] + pad):
                return name
        return None

    def _activate(self, name: str) -> None:
        if name in ('lang.zh', 'lang.en'):
            self.set_language('zh-CN' if name.endswith('zh') else 'en',
                              persist=True)
            return
        if name in ('open_data', 'card.open_data'):
            callback = self._open_data_callback
        elif name in ('open_result', 'card.open_result'):
            callback = self._open_result_callback
        else:
            return
        if callback is None:
            return
        action = callback(self)
        if action:
            self.request_action(*action)

    def set_callbacks(self, *, open_data=None, open_result=None) -> None:
        self._open_data_callback = open_data
        self._open_result_callback = open_result

    def request_action(self, kind: str, value: str) -> None:
        self.pending_action = (kind, value)
        self._terminate()

    # --------------------------------------------------------------- language
    def set_language(self, language: str, *, persist: bool = False) -> None:
        target = i18n.normalize_language(language)
        if target is None or target == self.language:
            return
        if target == 'zh-CN' and not self.font_file:
            return
        self.language = target
        if persist:
            try:
                self.language_settings.save(target)
            except OSError:
                pass
        self.plotter.title = self._window_title()
        self._paint()

    # -------------------------------------------------------------- interaction
    def _install_interactions(self) -> None:
        iren = getattr(self.plotter, 'iren', None)
        if iren is None:
            return
        iren.add_observer('LeftButtonPressEvent', self._on_press)
        iren.add_observer('LeftButtonReleaseEvent', self._on_release)
        iren.add_observer('MouseMoveEvent', self._on_move)
        iren.add_observer('LeaveEvent', self._on_leave)
        native = getattr(iren, 'interactor', None)
        if native is not None:
            native.AddObserver('ConfigureEvent', self._on_configure)

    def _on_press(self, *_args) -> None:
        point = self._point()
        if point is None:
            return
        self._pressed = self._hit(*point)
        self._hover = self._pressed
        self._paint()

    def _on_release(self, *_args) -> None:
        point = self._point()
        name = self._hit(*point) if point is not None else None
        pressed = self._pressed
        self._pressed = None
        self._paint()
        if pressed is not None and name == pressed:
            self._activate(name)

    def _on_move(self, *_args) -> None:
        point = self._point()
        if point is None:
            return
        name = self._hit(*point)
        if name != self._hover:
            self._hover = name
            self._paint()

    def _on_leave(self, *_args) -> None:
        if self._hover is not None:
            self._hover = None
            self._paint()

    def _on_configure(self, *_args) -> None:
        if self.off_screen:
            return
        width, height = self._client_size()
        self.window_size = (width, height)
        self._paint(render=True)

    def _on_first_render(self, *_args) -> None:
        if not self.off_screen:
            self._install_window_policy()
        # The native window can be created larger than the initial offscreen
        # probe (DPI / work-area clamp).  Repaint at the real client size so
        # the vtkImageData dimensions match the bitmap exactly.
        width, height = self._client_size()
        if (width, height) != tuple(self._image_data.GetDimensions())[:2]:
            self._paint(render=False)
        if self._window_policy is not None and self._window_policy.installed:
            observer = self._render_observer_id
            self._render_observer_id = None
            if observer is not None:
                self.plotter.render_window.RemoveObserver(observer)

    def _install_window_policy(self) -> None:
        if self.off_screen or self._window_policy is not None:
            return
        handle = theme.native_handle(self.plotter.render_window)
        if handle <= 0:
            return
        floor = theme.window_floor(
            theme.dpi_scale_for_window(self.plotter.render_window))
        self._window_policy = window_policy.MinimumSize(handle, floor)

    # --------------------------------------------------------------- lifecycle
    def _terminate(self) -> None:
        interaction.terminate(self.plotter)

    def _on_auto_close(self, *_args) -> None:
        if self.auto_close_ms and self._shown_at:
            if (time.perf_counter() - self._shown_at) * 1000.0 >= self.auto_close_ms:
                self._terminate()

    def show(self) -> tuple[str, str] | None:
        if self.off_screen:
            self._paint(render=True)
            return self.pending_action
        self._shown_at = time.perf_counter()
        if self.auto_close_ms:
            self._timer_observer, self._timer_id = interaction.add_timer(
                self.plotter, self._on_auto_close, 100)
        try:
            self.plotter.show(auto_close=False, interactive=True)
        finally:
            interaction.remove_timer(self.plotter, self._timer_observer,
                                     self._timer_id)
            self._timer_observer = None
            self._timer_id = None
            self.plotter.close()
        if self.pending_action is None:
            self.closed_by_user = True
        return self.pending_action

    def render(self) -> Image.Image:
        return self._paint(render=True)

    def screenshot(self, path) -> None:
        self._paint(render=True)
        self.plotter.screenshot(str(path))

    def close(self) -> None:
        try:
            self.plotter.close()
        except Exception:  # noqa: BLE001 - closing a dead window is harmless
            pass

    @property
    def image(self) -> Image.Image | None:
        return self._image

    @property
    def buttons(self) -> dict[str, Rect]:
        return dict(self._buttons)

    @property
    def labels(self) -> dict[str, str]:
        return dict(self._labels)
