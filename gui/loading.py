"""Native VTK progress/error windows (main thread only).

The window itself never computes: it polls a :class:`app.jobs.JobHandle`, shows
the stage text and elapsed monotonic time, and lets the user close it.  Closing
cancels the running job; the app then discards every late result.
"""
from __future__ import annotations

from collections import deque
import time

import pyvista as pv

from app.jobs import JobHandle
from gui import i18n, interaction
from runtime.errors import Cancelled


class LoadingWindow:
    POLL_MS = 100
    DETAIL_Y = .62
    DETAIL_FONT_SIZE = 9
    DETAIL_ASCII_PX = 1.9
    DETAIL_CJK_FACTOR = 1.7

    def __init__(self, off_screen: bool, window_size: tuple[int, int],
                 title: str | None = None, *, language: str | None = None,
                 settings_path=None, font_file: str | None = None) -> None:
        self.off_screen = bool(off_screen)
        self.closed_by_user = False
        self.finished = False
        self.language, self.font_file = i18n.resolve_language(
            language, settings_path, font_file)
        if title is None:
            title = i18n.tr(self.language, 'loading.title')
        self.plotter = pv.Plotter(off_screen=off_screen,
                                  window_size=window_size, border=False)
        self.plotter.set_background('#f8fafc')
        self.plotter.title = title
        font = self._font_kwargs()
        self.title_actor = self.plotter.add_text(
            title, position=(.04, .90), font_size=16, color='#0f172a',
            name='loading_title', viewport=True, **font)
        self.stage_actor = self.plotter.add_text(
            self._t('loading.preparing'), position=(.04, .78), font_size=11,
            color='#1d4ed8', name='loading_stage', viewport=True, **font)
        self.detail_actor = self.plotter.add_text(
            '', position=(.04, self.DETAIL_Y), font_size=self.DETAIL_FONT_SIZE,
            color='#475569', name='loading_detail', viewport=True, **font)
        self.progress_actor = self.plotter.add_text(
            '', position=(.04, .52), font_size=10, color='#0f172a',
            name='loading_progress', viewport=True, **font)
        self.elapsed_actor = self.plotter.add_text(
            '', position=(.04, .44), font_size=9, color='#64748b',
            name='loading_elapsed', viewport=True, **font)
        self.hint_actor = self.plotter.add_text(
            self._t('loading.hint'), position=(.04, .10), font_size=8,
            color='#94a3b8', name='loading_hint', viewport=True, **font)
        self._details: deque[str] = deque(maxlen=4)
        self._started = time.perf_counter()
        self._last_stage = ''
        self._last_fraction = None
        self._handle: JobHandle | None = None
        self._close_requested = False
        self._observer_id: int | None = None
        self._timer_id: int | None = None

    # ------------------------------------------------------------------ UI
    def _font_kwargs(self) -> dict:
        font = getattr(self, 'font_file', None)
        return {'font_file': font} if font else {}

    def _t(self, key: str, **kwargs) -> str:
        language = getattr(self, 'language', 'en') or 'en'
        return i18n.tr(language, key, **kwargs)

    def _stage_label(self, stage: str) -> str:
        key = f'stage.{stage}'
        label = self._t(key)
        if label == key:
            return str(stage).replace('_', ' ')
        return label

    def _fit_detail_line(self, text: str) -> str:
        """Keep long paths readable at 900 px with a middle ellipsis.

        The full text is still carried by the job event/log; only the display
        line is shortened.  The head keeps the drive/root and the tail keeps
        the final folder or file name.
        """
        text = str(text)
        plotter = getattr(self, 'plotter', None)
        try:
            width = float(plotter.window_size[0])
        except (AttributeError, IndexError, TypeError, ValueError):
            width = 900.0
        max_units = max(16.0, (width * 0.92) /
                        (self.DETAIL_FONT_SIZE * self.DETAIL_ASCII_PX))

        def units(value: str) -> float:
            return sum(self.DETAIL_CJK_FACTOR if ord(ch) > 127 else 1.0
                       for ch in value)

        if units(text) <= max_units:
            return text
        budget = max_units - 3.0
        head = max(8, min(12, int(budget * 0.3)))
        while head > 0 and units(text[:head]) > budget * 0.45:
            head -= 1
        tail_chars = []
        tail_units = 0.0
        for ch in reversed(text[head:]):
            u = self.DETAIL_CJK_FACTOR if ord(ch) > 127 else 1.0
            if tail_units + u > budget - units(text[:head]):
                break
            tail_chars.append(ch)
            tail_units += u
        if not tail_chars:
            return text[:int(budget)]
        return text[:head] + '...' + ''.join(reversed(tail_chars))

    def _detail_text(self) -> str:
        lines = getattr(self, '_details', ())
        return '\n'.join(self._fit_detail_line(line) for line in lines)

    def _drain(self) -> None:
        handle = self._handle
        if handle is None:
            return
        while True:
            try:
                event = handle.events.get_nowait()
            except Exception:
                break
            if event.stage:
                self._last_stage = event.stage
                self.stage_actor.SetInput(self._stage_label(event.stage))
            if event.text:
                self._details.append(event.text)
                self.detail_actor.SetInput(self._detail_text())
            if event.fraction is not None:
                self._last_fraction = float(event.fraction)

    def _paint_progress(self) -> None:
        fraction = self._last_fraction
        cells = 28
        if fraction is None:
            bar = '[' + '.' * cells + ']'
            percent = ''
        else:
            filled = max(0, min(cells, int(round(fraction * cells))))
            bar = '[' + '#' * filled + '.' * (cells - filled) + ']'
            percent = f'  {fraction * 100:5.1f}%'
        self.progress_actor.SetInput(bar + percent)
        elapsed = time.perf_counter() - self._started
        self.elapsed_actor.SetInput(
            self._t('loading.elapsed', seconds=elapsed))

    def _update(self) -> None:
        self._drain()
        self._paint_progress()
        self.plotter.render()

    # ------------------------------------------------------------------ run
    def run(self, handle: JobHandle, max_wait_s: float = 900.0) -> None:
        self._handle = handle
        if self.off_screen:
            started = time.perf_counter()
            while not handle.done.is_set():
                if time.perf_counter() - started > max_wait_s:
                    handle.cancel()
                    raise TimeoutError('loading exceeded the off-screen wait limit')
                self._drain()
                self._paint_progress()
                time.sleep(.02)
            self._update()
            self.plotter.close()
            self.finished = True
            return

        try:
            self._observer_id, self._timer_id = interaction.add_timer(
                self.plotter, self._on_timer, self.POLL_MS)
            self.plotter.add_key_event('Escape', self._request_close)
            self.plotter.show(auto_close=False, interactive=True)
        finally:
            interaction.remove_timer(self.plotter, self._observer_id,
                                     self._timer_id)
            self._observer_id = None
            self._timer_id = None
            self.plotter.close()
        if not handle.done.is_set():
            self.closed_by_user = True
            handle.cancel()
        self.finished = handle.done.is_set()

    def _on_timer(self, *_args) -> None:
        if self._close_requested:
            return
        self._drain()
        self._paint_progress()
        if self._handle is not None and self._handle.done.is_set():
            self._update()
            self._terminate()
            return
        self.plotter.render()

    def _request_close(self) -> None:
        self.closed_by_user = True
        self._close_requested = True
        if self._handle is not None:
            self._handle.cancel()
        self._terminate()

    def _terminate(self) -> None:
        interaction.terminate(self.plotter)


def _localize_message_title(title: str, language: str) -> str:
    text = str(title)
    for suffix, key in ((' - cannot load', 'message.cannot_load'),
                        (' - error', 'message.error_title')):
        if text.endswith(suffix):
            app = text[:-len(suffix)]
            return i18n.tr(language, key, app=app)
    return text


def show_message(off_screen: bool, window_size: tuple[int, int], title: str,
                 lines: list[str], *, auto_close_ms: int | None = None,
                 screenshot: str | None = None, language: str | None = None,
                 settings_path=None, font_file: str | None = None) -> None:
    """Static message window; on-screen it waits until the user closes it."""
    language, font_file = i18n.resolve_language(
        language, settings_path, font_file)
    title = _localize_message_title(title, language)
    font = {'font_file': font_file} if font_file else {}
    plotter = pv.Plotter(off_screen=off_screen, window_size=window_size,
                         border=False)
    plotter.set_background('#f8fafc')
    plotter.title = title
    plotter.add_text(title, position=(.04, .88), font_size=14,
                     color='#b91c1c', name='message_title', viewport=True, **font)
    body = '\n'.join(lines)
    plotter.add_text(body, position=(.04, .78), font_size=9, color='#334155',
                     name='message_body', viewport=True, **font)
    plotter.add_text(i18n.tr(language, 'message.close_hint'),
                     position=(.04, .10), font_size=8, color='#94a3b8',
                     name='message_hint', viewport=True, **font)
    if auto_close_ms is None:
        auto_close_ms = 0
    observer = timer = None
    try:
        if off_screen:
            plotter.render()
            if screenshot:
                plotter.screenshot(screenshot)
            return
        if auto_close_ms and auto_close_ms > 0:
            observer, timer = interaction.add_timer(
                plotter, lambda *_: _auto_close(plotter, auto_close_ms), 1000)
        plotter.add_key_event('Escape', lambda: interaction.terminate(plotter))
        plotter.show(auto_close=False, interactive=True)
    finally:
        interaction.remove_timer(plotter, observer, timer)
        plotter.close()


def _auto_close(plotter, auto_close_ms: int) -> None:
    if auto_close_ms and auto_close_ms > 0:
        if not hasattr(plotter, '_shown_at'):
            plotter._shown_at = time.perf_counter()
        elif (time.perf_counter() - plotter._shown_at) * 1000 >= auto_close_ms:
            interaction.terminate(plotter)
