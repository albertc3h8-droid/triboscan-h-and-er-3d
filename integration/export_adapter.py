"""Adapter to the export module (``io_exports``, owned by export-engineer).

The app never re-implements CSV/Excel/PNG writing.  Until the module exists the
adapter returns ``ok=False`` with a clear message (never a fake success and
never a temporary homemade CSV).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import traceback

from integration.protocols import ExportResult

_override = None
_import_error = None


def _module():
    global _override, _import_error
    if _override is not None:
        return _override
    try:
        import io_exports  # type: ignore
        return io_exports
    except Exception as exc:  # noqa: BLE001 - reported to the user as unavailable
        _import_error = exc
        return None


def set_module_for_tests(module) -> None:
    global _override
    _override = module


def available() -> bool:
    return _module() is not None


def _slug(text: str, limit: int = 60) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(text))
    cleaned = re.sub(r'_+', '_', cleaned).strip(' .')
    return (cleaned or 'dataset')[:limit]


def _unavailable() -> ExportResult:
    detail = '' if _import_error is None else f'（{_import_error}）'
    return ExportResult(
        ok=False,
        message='导出模块未就绪，本次没有写出任何文件' + detail,
        warnings=['export module io_exports is not available in this build'],
        detail='' if _import_error is None else str(_import_error))


def _prepare_out_dir(out_dir):
    try:
        path = Path(out_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path, None
    except OSError as exc:
        return None, ExportResult(ok=False, message=f'无法创建导出文件夹：{exc}',
                                  detail=str(exc))


def export_raw(dataset, out_dir: Path, app_version: str) -> ExportResult:
    """A: the real measured points as one UTF-8 CSV (+ the explanation txt).

    ``xlsx`` stays available inside ``io_exports`` but the product never asks
    for it any more: Excel opens the CSV directly (P4 W3).
    """
    module = _module()
    if module is None:
        return _unavailable()
    if not hasattr(module, 'export_raw_points'):
        return ExportResult(ok=False, message='导出模块未提供原始点导出函数')
    out_dir, error = _prepare_out_dir(out_dir)
    if error is not None:
        return error
    try:
        raw = module.export_raw_points(dataset, out_dir, formats=('csv',),
                                       app_version=app_version)
    except Exception as exc:  # noqa: BLE001
        return ExportResult(ok=False, message=f'导出原始点失败：{exc}',
                            detail=traceback.format_exc())
    if not getattr(raw, 'ok', False):
        return ExportResult(ok=False, files=list(getattr(raw, 'files', []) or []),
                            message=getattr(raw, 'message', '导出原始点失败'),
                            detail=str(getattr(raw, 'detail', '')))
    return ExportResult(ok=True,
                        files=[str(p) for p in getattr(raw, 'files', []) or []],
                        message=getattr(raw, 'message', ''))


def export_image(plotter, snapshot, out_dir: Path, app_version: str) -> ExportResult:
    """B: the clean current frame as one PNG (no on-screen controls)."""
    module = _module()
    if module is None:
        return _unavailable()
    if not hasattr(module, 'export_slice_png'):
        return ExportResult(ok=False, message='导出模块未提供 PNG 导出函数')
    out_dir, error = _prepare_out_dir(out_dir)
    if error is not None:
        return error
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    depth = (f'z{snapshot.depth_nm:.3f}nm' if snapshot.depth_nm is not None
             else 'raw_alldepths')
    if hasattr(module, 'png_filename'):
        name = module.png_filename(snapshot)
    else:
        name = (f'TRIBOSCAN_{_slug(snapshot.dataset_label)}_'
                f'{snapshot.view_mode}_{snapshot.metric}_'
                f'{depth}_{stamp}.png')
    png = out_dir / name
    try:
        image = module.export_slice_png(plotter, snapshot, png)
    except Exception as exc:  # noqa: BLE001
        return ExportResult(ok=False, message=f'导出 PNG 失败：{exc}',
                            detail=traceback.format_exc())
    if not getattr(image, 'ok', False):
        return ExportResult(ok=False,
                            files=[str(p) for p in getattr(image, 'files', []) or []],
                            message=getattr(image, 'message', '导出 PNG 失败'),
                            detail=str(getattr(image, 'detail', '')))
    return ExportResult(ok=True,
                        files=[str(p) for p in getattr(image, 'files', [png])],
                        message=getattr(image, 'message', ''))


def export_current(dataset, plotter, snapshot, out_dir: Path,
                   app_version: str) -> ExportResult:
    """Export A (raw points CSV) and B (current frame PNG) into one folder.

    ``--export-to`` uses this composition, and the app appends the interpolated
    field (C) itself so the three on-screen cards can each call one typed helper
    (P4 W1); keeping C outside this function also avoids a save dialog here.
    """
    module = _module()
    if module is None:
        return _unavailable()
    out_dir = Path(out_dir)
    files: list[str] = []
    for step in (lambda: export_raw(dataset, out_dir, app_version),
                 lambda: export_image(plotter, snapshot, out_dir, app_version)):
        result = step()
        files.extend(str(p) for p in result.files)
        if not result.ok:
            return ExportResult(ok=False, files=files, message=result.message,
                                warnings=list(result.warnings),
                                detail=result.detail)

    if not files:
        return ExportResult(ok=False, message='导出模块未提供可调用的导出函数')
    return ExportResult(ok=True, files=files,
                        message=f'已导出 {len(files)} 个文件到 {out_dir}')
