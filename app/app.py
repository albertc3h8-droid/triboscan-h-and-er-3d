"""Product application: load -> viewer -> file actions, with clear states.

The main thread owns every VTK/PyVista object.  The compute worker only returns
plain data (:class:`integration.protocols.JobResult`); it never touches a
render window.  Cancelled or superseded job outcomes are discarded by
generation: only the handle the app is currently showing may reach the GUI.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import argparse
import logging
import time
import traceback

from app import jobs, load_job, shell_open
from gui import i18n, interaction, loading, picker, theme, welcome
from gui.viewer import TrialViewer
from integration import export_adapter, result_adapter
from integration.protocols import APP_TITLE, APP_VERSION, ExportResult, JobResult
from runtime import paths as paths_module
from runtime.errors import Cancelled, ProductError

log = logging.getLogger('triboscan.app')

ACTION_OPEN_DIR = 'open_dir'
ACTION_OPEN_RESULT = 'open_result'
ACTION_WELCOME = 'welcome'
ACTION_QUIT = 'quit'

#: UI v2 client-size limits live in gui.theme (compact 1280 x 720 floor).
MIN_WINDOW = theme.MIN_WINDOW
MAX_WINDOW = theme.MAX_WINDOW
DEFAULT_WINDOW = theme.DEFAULT_WINDOW


def clamp_window_size(size, scale: float = 1.0,
                      minimum: bool = True,
                      maximum: bool = True) -> tuple[int, int]:
    """Keep the client area inside the supported UI v2 range."""
    return theme.clamp_window_size(size, scale, minimum, maximum)


@dataclass
class Options:
    off_screen: bool = False
    window_size: tuple[int, int] = DEFAULT_WINDOW
    ui_scale: float = 1.0
    view_mode: str | None = None
    range_mode: str | None = None
    reference_mode: str | None = None
    interpolation: str = 'tps'
    depth: float | None = None
    force_recompute: bool = False
    no_cache: bool = False
    require_cache: bool = False
    auto_close_ms: int = 0
    scene_report: Path | None = None
    screenshot: Path | None = None
    timings_json: Path | None = None
    export_to: Path | None = None


class Application:
    def __init__(self, paths: paths_module.Paths, options: Options,
                 *, initial_data: str | None = None,
                 initial_result: str | None = None) -> None:
        self.paths = paths
        self.options = options
        self.manager = jobs.JobManager()
        #: First failure code seen by this run (0 = success).  Export failures
        #: exit 3 and user-save failures exit 5 (P4 W4); the first code wins so
        #: a later success never hides an earlier failure.
        self.exit_code = 0
        self.last_dir = initial_data
        if initial_result:
            self.action = (ACTION_OPEN_RESULT, initial_result)
        elif initial_data:
            self.action = (ACTION_OPEN_DIR, initial_data)
        else:
            # No source on the command line: show the empty workspace instead
            # of forcing a raw-folder dialog.  The user can open a folder, a
            # .tsr3 or a raw-points CSV from there, and closing the window
            # exits.
            self.action = (ACTION_WELCOME, None)

    # ------------------------------------------------------------------ run
    def run(self) -> int:
        log.info('temp dir: %s', getattr(self.paths, 'tmp', ''))
        try:
            while True:
                action, value = self.action
                if action == ACTION_QUIT:
                    return self.exit_code
                if action == ACTION_WELCOME:
                    chosen = self._show_welcome()
                    if chosen is None:
                        return 0
                    action, value = chosen
                if action == ACTION_OPEN_DIR:
                    outcome = self._load_directory(value)
                elif action == ACTION_OPEN_RESULT:
                    outcome = self._load_result(value)
                else:
                    raise RuntimeError(f'unknown app action {action!r}')

                if outcome is None:            # user cancelled the loading window
                    self.action = (ACTION_WELCOME, None)
                    continue
                result, error = outcome
                # A raw-points CSV is its own source: it has no result cache,
                # so --require-cache only governs directory / .tsr3 loads.
                points_csv_source = (result is not None and getattr(
                    result.source, 'kind', '') == 'points_csv')
                if (result is not None and self.options.require_cache
                        and not result.cache_hit and not points_csv_source):
                    print('CACHE_MISS: no cached field for this source', flush=True)
                    return 4
                if error is not None:
                    code = self._show_error(error, value, action)
                    if code is not None:
                        return code
                    self.action = (ACTION_WELCOME, None)
                    continue
                next_action = self._show_viewer(result)
                if next_action is None:
                    return self.exit_code
                self.action = next_action
        finally:
            self.manager.shutdown()

    # ------------------------------------------------------------ selection
    def _show_welcome(self):
        """Empty workspace; no source dialog until the user asks for one."""
        window = welcome.WelcomeWindow(
            window_size=self.options.window_size,
            ui_scale=self.options.ui_scale,
            auto_close_ms=self.options.auto_close_ms)
        window.set_callbacks(
            open_data=self._welcome_open_data,
            open_result=self._welcome_open_result)
        return window.show()

    def _welcome_open_data(self, window):
        try:
            folder = picker.choose_folder(
                'Select the folder with *_analysis.txt measurement files',
                initial=self.last_dir)
        except ProductError as exc:
            self._message('Cannot open the folder dialog', [str(exc)])
            return None
        if not folder:
            return None
        self.last_dir = folder
        return (ACTION_OPEN_DIR, folder)

    def _welcome_open_result(self, window):
        language = getattr(window, 'language', 'en')
        try:
            path = picker.choose_open_file(
                i18n.tr(language, 'dialog.open_result'),
                picker.result_file_filter())
        except ProductError as exc:
            self._message(i18n.tr(language, 'dialog.open_result'),
                          [str(exc)])
            return None
        if not path:
            return None
        return (ACTION_OPEN_RESULT, path)

    # -------------------------------------------------------------- loading
    def _load_directory(self, directory):
        handle = self.manager.start('directory', {
            'fn': lambda h: load_job.load_directory(
                h, directory=directory, cache_dir=self.paths.cache,
                use_cache=not self.options.no_cache,
                force_recompute=self.options.force_recompute)})
        return self._wait_for_job(handle)

    def _load_result(self, path):
        # A ".csv" is the raw-points export of this program: it has no
        # interpolated field, so it opens as measured points (P4 W5).
        loader_fn = (load_job.load_points_csv
                     if Path(path).suffix.lower() == '.csv'
                     else load_job.load_result_file)
        handle = self.manager.start('result_file', {
            'fn': lambda h: loader_fn(h, path=path)})
        return self._wait_for_job(handle)

    def _wait_for_job(self, handle):
        window = loading.LoadingWindow(self.options.off_screen,
                                       self.options.window_size)
        try:
            window.run(handle)
        except TimeoutError as exc:
            handle.cancel()
            return (None, ProductError(str(exc)))
        if window.closed_by_user:
            handle.cancel()
            return None
        outcome = handle.outcome
        if outcome is None:
            return (None, ProductError('The compute job ended without a result.'))
        if outcome.error is not None:
            return (None, outcome.error)
        return (outcome.result, None)

    # ---------------------------------------------------------------- viewer
    def _show_viewer(self, result: JobResult):
        started = time.perf_counter()
        points_only = not getattr(result, 'has_field', True)
        viewer = TrialViewer(
            result.dataset, result.field, result.raw_spacing,
            off_screen=self.options.off_screen,
            window_size=self.options.window_size,
            timings=result.timings, cache_hit=result.cache_hit,
            source_note=result.source.note, ui_scale=self.options.ui_scale,
            points_only=points_only)
        viewer.set_interpolation(self.options.interpolation, render=False)
        if points_only:
            # A raw-points CSV carries no interpolated field: show the measured
            # points only and say what is missing (P4 W5).
            viewer.set_view_mode('raw', render=False)
            viewer.set_notice(i18n.tr(viewer.language, 'notice.points_only'))
        elif self.options.view_mode:
            viewer.set_view_mode(self.options.view_mode, render=False)
        if self.options.range_mode:
            viewer.set_range_mode(self.options.range_mode, render=False)
        if self.options.reference_mode:
            viewer.set_reference_mode(self.options.reference_mode, render=False)
        if self.options.depth is not None:
            viewer.set_depth(self.options.depth, render=False)
        viewer.plotter.render()
        render_s = time.perf_counter() - started
        result.timings.render_s = render_s
        if result.timings.total_s is not None:
            result.timings.total_s += render_s
        viewer.set_timings(result.timings)
        viewer.set_save_available(result.timings.slow_compute and not result.cache_hit)
        viewer.set_file_callbacks(
            export=lambda v: self._export(
                v, result, kind=getattr(v, 'export_kind', 'image')),
            save=lambda v: self._save(v, result),
            open_data=lambda v: self._request_action(v, ACTION_OPEN_DIR),
            load_result=lambda v: self._request_action(v, ACTION_OPEN_RESULT),
            export_dir=lambda v: self._choose_export_dir(v))
        viewer.set_export_dir(str(self._export_initial_dir()), render=False)

        self._write_reports(viewer, result)
        if self.options.export_to is not None:
            # Automation exports all three kinds; a failure returns rc 3 at
            # once instead of entering the interactive loop (P4 W4).
            if not self._export(viewer, result, out_dir=self.options.export_to,
                                kind='all'):
                viewer.plotter.close()
                return None
        if self.options.off_screen:
            if self.options.screenshot:
                viewer.plotter.screenshot(str(self.options.screenshot))
            viewer.plotter.close()
            return None
        if self.options.auto_close_ms:
            self._schedule_close(viewer, self.options.auto_close_ms)
        try:
            viewer.show()
        finally:
            self._cancel_scheduled_close(viewer)
            viewer.plotter.close()
        pending = viewer.pending_action
        if pending is None:
            return None
        kind, value = pending
        return (kind, value)

    # --------------------------------------------------------------- actions
    def _request_action(self, viewer, kind: str) -> None:
        if kind == ACTION_OPEN_DIR:
            folder = picker.choose_folder(
                'Select the folder with *_analysis.txt measurement files',
                initial=self.last_dir)
            if folder:
                self.last_dir = folder
                viewer.request_action(kind, folder)
        elif kind == ACTION_OPEN_RESULT:
            file_path = picker.choose_open_file(
                i18n.tr(getattr(viewer, 'language', 'en'), 'dialog.open_result'),
                picker.result_file_filter())
            if file_path:
                viewer.request_action(kind, file_path)

    def _choose_export_dir(self, viewer) -> None:
        """Folder picker behind the export-folder card."""
        language = getattr(viewer, 'language', 'en')
        try:
            chosen = picker.choose_folder(
                i18n.tr(language, 'dialog.export_folder'),
                initial=str(self._export_initial_dir()))
        except ProductError as exc:
            viewer.set_notice(i18n.tr(language, 'notice.export_failed',
                                      message=str(exc), detail=''))
            return
        if chosen:
            viewer.set_export_dir(chosen)

    @staticmethod
    def _export_plotter(viewer):
        """Plotter handed to io_exports: clean crop when the viewer has it.

        The PNG writer screenshots the render window; TrialViewer supplies a
        proxy that crops the capture to the two plot cards so the file has no
        toolbar/sidebar/control chrome.  Fake viewers in tests keep plotter.
        """
        clean = getattr(viewer, 'clean_plotter', None)
        if callable(clean):
            try:
                return clean()
            except Exception:  # noqa: BLE001 - never fail the export over this
                log.warning('clean export plotter unavailable', exc_info=True)
        return viewer.plotter

    def _export(self, viewer, result: JobResult, out_dir: Path | None = None,
                kind: str | None = None) -> bool:
        """Run exactly one export kind: image, raw, field, or "all" three.

        The three on-screen cards call this with ``viewer.export_kind`` so each
        card writes exactly one artefact; ``--export-to`` asks for ``all``.
        Failures are logged and shown in the status line, and are recorded as
        rc 3 (export) / rc 5 (save) through :meth:`_fail`.
        """
        kind = str(kind or 'all').strip().lower()
        if kind not in ('image', 'raw', 'field', 'all'):
            kind = 'all'
        language = getattr(viewer, 'language', 'en')
        if kind == 'field':
            return self._export_field(viewer, result)
        snapshot = viewer.build_snapshot('both')
        prompted = False
        if out_dir is None and getattr(viewer, 'export_dir', ''):
            out_dir = Path(viewer.export_dir)
        if out_dir is None:
            if self._is_interactive_gui():
                try:
                    chosen = picker.choose_folder(
                        i18n.tr(language, 'dialog.export_folder'),
                        initial=str(self._export_initial_dir()))
                except ProductError as exc:
                    viewer.set_notice(i18n.tr(
                        language, 'notice.export_failed',
                        message=str(exc), detail=''))
                    self._fail(3)
                    return False
                if not chosen:
                    viewer.set_notice(i18n.tr(
                        language, 'notice.export_cancelled'))
                    return True
                out_dir = Path(chosen)
                prompted = True
            elif self.options.export_to is not None:
                out_dir = Path(self.options.export_to)
            else:
                stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                out_dir = self.paths.results / f'run_{stamp}'
        else:
            out_dir = Path(out_dir)
        out_dir = out_dir.resolve()
        plotter = self._export_plotter(viewer)
        if kind == 'image':
            exported = export_adapter.export_image(
                plotter, snapshot, out_dir, APP_VERSION)
        elif kind == 'raw':
            exported = export_adapter.export_raw(
                result.dataset, out_dir, APP_VERSION)
        else:
            exported = export_adapter.export_current(
                result.dataset, plotter, snapshot, out_dir, APP_VERSION)
            if (exported.ok and self.options.export_to is not None
                    and getattr(result, 'has_field', False)):
                # --export-to promises the complete export, interpolated field
                # included; the three on-screen cards each write one file only.
                field_export = self._write_field_export(result, out_dir)
                if field_export.ok:
                    exported = ExportResult(
                        ok=True,
                        files=[*exported.files, *field_export.files],
                        message=f'{exported.message}; {field_export.message}',
                        warnings=list(exported.warnings),
                        detail=exported.detail)
                else:
                    exported = field_export
        self._log_export(exported)
        if exported.ok:
            if prompted:
                # Best effort only: a read-only/broken settings file must never
                # turn a successful export into a failure.
                self._export_settings().save_export_dir(out_dir)
            log.info('export folder: %s', out_dir)
            open_failed = False
            if self._should_open_export_folder():
                reason = ''
                try:
                    opened = shell_open.open_folder(out_dir)
                    if not opened:
                        reason = (shell_open.last_error()
                                  if hasattr(shell_open, 'last_error')
                                  else '') or 'unknown failure'
                except Exception as exc:  # noqa: BLE001 - export already succeeded
                    opened = False
                    reason = f'{type(exc).__name__}: {exc}'
                    log.warning('opening export folder raised: target=%s type=%s: %s',
                                out_dir, type(exc).__name__, exc)
                if not opened:
                    open_failed = True
                    log.warning('could not open export folder in Explorer: %s '
                                '(target=%s)', reason, out_dir)
            key = ('notice.exported_open_failed' if open_failed
                   else 'notice.exported_path')
            viewer.set_notice(i18n.tr(
                getattr(viewer, 'language', 'en'), key,
                count=len(exported.files), path=str(out_dir)))
            return True
        detail = self._export_detail(exported)
        self._fail(3)
        if detail:
            viewer.set_notice(i18n.tr(
                language, 'notice.export_failed',
                message=exported.message, detail=detail))
        else:
            viewer.set_notice(exported.message)
        return False

    def _export_field(self, viewer, result: JobResult) -> bool:
        """Third card: write the current interpolated field as ``.tsr3``.

        No 5 s cache threshold applies: any result may be exported explicitly.
        The save dialog (interactive runs only) is pre-filled with the export
        folder and a readable name and may be changed; cancelling writes
        nothing and is not a failure.  A write error exits 5.
        """
        language = getattr(viewer, 'language', 'en')
        if not getattr(result, 'has_field', True):
            # Opened from a raw-points CSV: the placeholder field holds no
            # interpolated sample, so exporting it would ship an empty grid.
            viewer.set_notice(i18n.tr(language, 'notice.points_only'))
            return True
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        default_name = (f'插值场_{self._slug(self._result_label(result))}_'
                        f'{stamp}.tsr3')
        initial_dir = Path(self._export_initial_dir())
        target: Path
        if self._is_interactive_gui():
            try:
                chosen = picker.choose_save_file(
                    i18n.tr(language, 'dialog.export_field'),
                    picker.result_file_filter(),
                    initial=str(initial_dir / default_name),
                    default_ext='tsr3')
            except ProductError as exc:
                viewer.set_notice(i18n.tr(
                    language, 'notice.export_failed', message=str(exc),
                    detail=''))
                self._fail(5)
                return False
            if not chosen:
                viewer.set_notice(i18n.tr(language, 'notice.export_cancelled'))
                return True
            target = Path(chosen)
        else:
            target = initial_dir / default_name
        try:
            saved = self._write_result_file(result, target)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            winerror = getattr(exc, 'winerror', None)
            reason = str(exc) or exc.__class__.__name__
            if winerror is not None and f'WinError {winerror}' not in reason:
                reason = f'{reason} (WinError {winerror})'
            log.error('field export failed: target=%s type=%s winerror=%s '
                      'reason=%s', target, type(exc).__name__, winerror, reason,
                      exc_info=True)
            viewer.set_notice(i18n.tr(
                language, 'notice.export_failed',
                message=f'{type(exc).__name__}: {reason}', detail=reason[:200]))
            self._fail(5)
            return False
        try:
            self._export_settings().save_export_dir(saved.parent)
        except Exception as exc:  # noqa: BLE001 - the file is already written
            log.warning('remembering the export folder failed: %s', exc)
        viewer.set_notice(i18n.tr(language, 'notice.exported_path', count=1,
                                  path=str(saved.parent)))
        log.info('exported field: %s', saved)
        return True

    @staticmethod
    def _result_label(result: JobResult) -> str:
        """Human-readable dataset label used in exported file names."""
        source = getattr(result, 'source', None)
        return (getattr(source, 'display_label', '') or 'result')

    @staticmethod
    def _write_result_file(result: JobResult, target: Path) -> Path:
        """Serialise *result* into *target* through the result store."""
        bundle = result_adapter.to_bundle(result)
        return result_adapter.save_file(bundle, Path(target))

    def _write_field_export(self, result: JobResult, out_dir) -> ExportResult:
        """``--export-to`` partner of :meth:`_export_field` (no dialog)."""
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        name = f'插值场_{self._slug(self._result_label(result))}_{stamp}.tsr3'
        target = Path(out_dir) / name
        try:
            saved = self._write_result_file(result, target)
        except Exception as exc:  # noqa: BLE001
            return ExportResult(ok=False, message=f'导出插值场失败：{exc}',
                                detail=traceback.format_exc())
        return ExportResult(ok=True, files=[str(saved)],
                            message=f'已导出插值场 {saved.name}')

    def _fail(self, code: int) -> None:
        """Remember the first failure code so ``run()`` can return it."""
        if not self.exit_code:
            self.exit_code = int(code)

    def _export_settings(self) -> i18n.LanguageSettings:
        """The portable settings file next to the executable."""
        config = getattr(self.paths, 'config', None)
        if config is None:
            return i18n.LanguageSettings()
        return i18n.LanguageSettings(Path(config) / 'settings.json')

    def _export_initial_dir(self) -> Path:
        """Last successful export folder, else the default results folder."""
        remembered = self._export_settings().load_export_dir()
        if remembered:
            return Path(remembered)
        return Path(self.paths.results)

    def _is_interactive_gui(self) -> bool:
        """True only for a real GUI session (all automation is excluded)."""
        if self.options.off_screen or self.options.auto_close_ms:
            return False
        return self.options.export_to is None and \
            self.options.scene_report is None and \
            self.options.screenshot is None and \
            self.options.timings_json is None

    def _should_open_export_folder(self) -> bool:
        """True only for an interactive GUI run (never offscreen/automation)."""
        return self._is_interactive_gui()

    def _save(self, viewer, result: JobResult) -> None:
        if not viewer._save_available:
            viewer.set_notice(viewer._t('notice.save_unavailable'))
            return
        default_name = f'TRIBOSCAN_{self._slug(result.source.display_label)}_' \
                       f'{datetime.now().strftime("%Y%m%d_%H%M%S")}.tsr3'
        try:
            chosen = picker.choose_save_file(
                'Save the computed result (reopen without recomputing)',
                picker.result_file_filter(),
                initial=str(self.paths.results / default_name),
                default_ext='tsr3')
        except ProductError as exc:
            viewer.set_notice(str(exc))
            return
        if not chosen:
            return
        target = Path(chosen)
        try:
            bundle = result_adapter.to_bundle(result)
            saved = result_adapter.save_file(bundle, target)
            try:
                result_adapter.save(self.paths.cache, bundle, reason='user-save')
            except Exception as exc:  # noqa: BLE001 - chosen file is enough
                log.warning('cache copy after user save failed: target=%s '
                            'type=%s: %s', target, type(exc).__name__, exc)
            viewer._save_done = True
            viewer.set_notice(f'saved result: {saved.name}')
            log.info('user saved result to %s', saved)
        except Exception as exc:  # noqa: BLE001 - diagnostics only; never reroute
            winerror = getattr(exc, 'winerror', None)
            reason = str(exc) or exc.__class__.__name__
            if winerror is not None and f'WinError {winerror}' not in reason:
                reason = f'{reason} (WinError {winerror})'
            self._fail(5)
            log.error('save failed: target=%s type=%s winerror=%s reason=%s',
                      target, type(exc).__name__, winerror, reason,
                      exc_info=True)
            viewer.set_notice(i18n.tr(
                viewer.language, 'error.save_failed', path=str(target),
                kind=type(exc).__name__, reason=reason))

    # ----------------------------------------------------------------- misc
    @staticmethod
    def _slug(text: str, limit: int = 60) -> str:
        return export_adapter._slug(text, limit)

    def _schedule_close(self, viewer, milliseconds: int) -> None:
        started = time.perf_counter()

        def tick(*_args):
            if (time.perf_counter() - started) * 1000 >= milliseconds:
                self._cancel_scheduled_close(viewer)
                viewer.request_action('quit')

        observer, timer = interaction.add_timer(viewer.plotter, tick, 100)
        viewer._auto_close_timer = (observer, timer)

    @staticmethod
    def _cancel_scheduled_close(viewer) -> None:
        ids = getattr(viewer, '_auto_close_timer', None)
        if not ids:
            return
        viewer._auto_close_timer = None
        interaction.remove_timer(viewer.plotter, *ids)

    def _show_error(self, error: BaseException, value, action) -> int | None:
        kind = error.__class__.__name__
        message = str(error) or kind
        lines = [f'{kind}', '', *message.splitlines(), '',
                 f'log: {self.paths.logs}']
        log.error('load failed (%s): %s', kind, message)
        if not isinstance(error, ProductError):
            log.debug('traceback:\n%s', ''.join(
                traceback.format_exception(type(error), error, error.__traceback__)))
        if self.options.off_screen:
            print(f'LOAD_ERROR: {kind}: {message}', flush=True)
            return 2
        loading.show_message(False, self.options.window_size,
                             f'{APP_TITLE} - cannot load', lines)
        return None

    def _message(self, title: str, lines: list[str]) -> None:
        if self.options.off_screen:
            print(f'{title}: ' + ' | '.join(lines), flush=True)
            return
        loading.show_message(False, self.options.window_size, title, lines)

    def _log_export(self, exported: ExportResult) -> None:
        if exported.ok:
            log.info('exported: %s', exported.files)
        else:
            # Keep the real exception text in the log; the old code dropped it
            # and left only the high-level message plus an empty warning list.
            detail = self._export_detail(exported, limit=None)
            log.error('export failed: %s\ndetail: %s',
                      exported.message, detail or '(no detail)')

    @staticmethod
    def _export_detail(exported: ExportResult, limit: int | None = 200) -> str:
        """Flatten the adapter's detail and warning/traceback for display/log."""
        chunks: list[str] = []
        seen: set[str] = set()
        for item in [getattr(exported, 'detail', ''), *exported.warnings]:
            text = ' '.join(str(item).split())
            if text and text not in seen:
                seen.add(text)
                chunks.append(text)
        text = ' | '.join(chunks)
        return text if limit is None else text[:limit]

    def _write_reports(self, viewer: TrialViewer, result: JobResult) -> None:
        if self.options.scene_report is not None:
            import json
            self.options.scene_report.parent.mkdir(parents=True, exist_ok=True)
            report = viewer.scene_report()
            report['job'] = {
                'build_count': result.build_count,
                'cache_hit': result.cache_hit,
                'cache_reason': result.cache_reason,
                'cache_key': result.cache_key,
                'source': {
                    'kind': result.source.kind,
                    'path': result.source.path,
                    'label': result.source.display_label,
                    'verified': result.source.source_verified,
                    'note': result.source.note,
                },
                'warnings': result.warnings,
            }
            self.options.scene_report.write_text(
                json.dumps(report, indent=2), encoding='utf-8')
        if self.options.timings_json is not None:
            import json
            self.options.timings_json.parent.mkdir(parents=True, exist_ok=True)
            self.options.timings_json.write_text(
                json.dumps({'timings': result.timings.as_dict(),
                            'cache_hit': result.cache_hit,
                            'build_count': result.build_count},
                           indent=2), encoding='utf-8')


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_TITLE,
        description='Portable 3D hardness viewer: open measurement folders, '
                    'slice, export and reopen saved results.')
    parser.add_argument('--data', type=str, default=None,
                        help='folder containing *_analysis.txt files')
    parser.add_argument('--open-result', type=str, default=None,
                        help='open a saved *.tsr3 result or a raw-points '
                             '*.csv export (source folder not needed)')
    parser.add_argument('--offscreen', action='store_true')
    parser.add_argument('--window-size', default=None,
                        help='client size in device pixels; default keeps the '
                             '1536 x 992 design frame at the current DPI scale')
    parser.add_argument('--ui-scale', type=float, default=1.0,
                        help='extra UI scale on top of the window fit')
    parser.add_argument('--view-mode', choices=('normal', 'locate', 'raw'))
    parser.add_argument('--range-mode', choices=('common', 'actual'))
    parser.add_argument('--reference-mode',
                        choices=('auto', 'layers', 'envelope'))
    parser.add_argument('--interpolation',
                        choices=('linear', 'cubic', 'tps'),
                        default='tps',
                        help='display-only slice interpolation; tps (thin-plate '
                             'spline) and cubic pass through the measured '
                             'values without blurring, the stored field and '
                             'exports keep the raw linear values')
    parser.add_argument('--depth', type=float, default=None)
    parser.add_argument('--force-recompute', action='store_true')
    parser.add_argument('--no-cache', action='store_true')
    parser.add_argument('--require-cache', action='store_true',
                        help='exit 4 if no cached field is found')
    parser.add_argument('--auto-close-ms', type=int, default=0)
    parser.add_argument('--scene-report', type=Path, default=None)
    parser.add_argument('--screenshot', type=Path, default=None)
    parser.add_argument('--timings-json', type=Path, default=None)
    parser.add_argument('--export-to', type=Path, default=None,
                        help='after the first frame, export A (CSV/Excel) and '
                             'B (PNG) into this folder, then continue/close')
    return parser


def run(options: Options, initial_data: str | None = None,
        initial_result: str | None = None, paths=None) -> int:
    options.window_size = clamp_window_size(
        options.window_size,
        scale=1.0 if options.off_screen else theme.system_ui_scale(),
        minimum=not options.off_screen,
        maximum=not options.off_screen)
    if paths is None:
        paths = paths_module.prepare()
    if options.require_cache and not result_adapter.available():
        print('CACHE_ERROR: result cache module is not available', flush=True)
        return 4
    app = Application(paths, options, initial_data=initial_data,
                      initial_result=initial_result)
    return app.run()
