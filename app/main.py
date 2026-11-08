"""Entry point.  Prepares the portable folders *before* importing PyVista.

MPLCONFIGDIR must point at this program's own ``cache/mpl`` before matplotlib is
imported by PyVista.  A restricted account gets a clear error; there is no
fallback to LOCALAPPDATA/TEMP and no attempt to widen permissions.
"""
from __future__ import annotations

import ctypes
import logging
import sys

from runtime.paths import APP_NAME, LayoutError, prepare


def _message_box(text: str, title: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
    except Exception:
        pass


def _fatal(text: str) -> int:
    print(text, file=sys.stderr, flush=True)
    _message_box(text, f'{APP_NAME} - cannot start')
    return 3


def _setup_logging(log_dir) -> None:
    from datetime import datetime
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    handler = logging.FileHandler(log_dir / f'app-{stamp}.log',
                                  encoding='utf-8')
    handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s %(name)s: %(message)s'))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    stream = logging.StreamHandler()
    stream.setLevel(logging.WARNING)
    root.addHandler(stream)


def main(argv=None) -> int:
    try:
        paths = prepare()
    except LayoutError as exc:
        return _fatal(str(exc))
    _setup_logging(paths.logs)
    try:
        from app.app import Options, build_parser, run
    except Exception as exc:  # noqa: BLE001 - import failure is a startup error
        logging.getLogger('triboscan').exception('startup import failed')
        return _fatal(f'The program could not start:\n\n{exc}\n\n'
                      f'See the log folder:\n{paths.logs}')
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.auto_close_ms < 0:
        parser.error('--auto-close-ms must be nonnegative')
    from gui import theme  # imported after parsing: it touches ctypes/Pillow

    theme.enable_dpi_awareness()
    dpi_scale = theme.system_ui_scale()
    if args.window_size is None:
        # Interactive windows open at the DPI-scaled design frame, capped to
        # the work area and never below the 1280 x 720 compact floor;
        # off-screen renders stay at 1536 x 992 so evidence shots compare.
        window_size = (theme.DEFAULT_WINDOW if args.offscreen
                       else theme.default_window_size(dpi_scale))
    else:
        try:
            width, height = (int(v) for v in args.window_size.lower().split('x'))
        except ValueError:
            parser.error('--window-size must look like 1536x992')
        window_size = (width, height)
    window_size = theme.clamp_window_size(
        window_size,
        scale=1.0 if args.offscreen else dpi_scale,
        minimum=not args.offscreen,
        maximum=not args.offscreen)
    if args.ui_scale <= 0.0:
        parser.error('--ui-scale must be positive')
    if not args.data and not args.open_result and args.offscreen:
        print('INPUT_ERROR: --offscreen needs --data or --open-result', flush=True)
        return 2
    options = Options(
        off_screen=args.offscreen,
        window_size=window_size,
        ui_scale=args.ui_scale,
        view_mode=args.view_mode, range_mode=args.range_mode,
        reference_mode=args.reference_mode,
        interpolation=args.interpolation, depth=args.depth,
        force_recompute=args.force_recompute, no_cache=args.no_cache,
        require_cache=args.require_cache, auto_close_ms=args.auto_close_ms,
        scene_report=args.scene_report, screenshot=args.screenshot,
        timings_json=args.timings_json, export_to=args.export_to)
    try:
        return run(options, initial_data=args.data,
                   initial_result=args.open_result, paths=paths)
    except Exception as exc:  # noqa: BLE001 - last-resort UI, log keeps traceback
        logging.getLogger('triboscan').exception('unhandled application error')
        if args.offscreen:
            print(f'INTERNAL_ERROR: {exc}', flush=True)
            return 1
        _message_box(f'The program hit an unexpected error:\n\n{exc}\n\n'
                     f'Technical details are in:\n{paths.logs}',
                     f'{APP_NAME} - error')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
