"""Adapter to the result-cache module (``result_store``, owned by export-engineer).

Three uses, all behind this one file so the app can run before t3 lands:

* folder auto-cache (``source_signature`` / ``load`` / ``save``);
* user-visible "save result" and "open saved result" (``save_file`` /
  ``load_file``) - a saved file is self-contained and openable without the
  original data folder;
* test injection (``set_module_for_tests``).
"""
from __future__ import annotations

from pathlib import Path
import os
import traceback

from integration.protocols import APP_VERSION
from runtime.errors import ProductError

_override = None
_import_error = None


def _module():
    global _override, _import_error
    if _override is not None:
        return _override
    try:
        import result_store  # type: ignore
        return result_store
    except Exception as exc:  # noqa: BLE001 - user is told the feature is absent
        _import_error = exc
        return None


def set_module_for_tests(module) -> None:
    global _override
    _override = module


def available() -> bool:
    return _module() is not None


def unavailable_note() -> str:
    if _import_error is None:
        return '计算结果保存/载入模块未就绪'
    return f'计算结果保存/载入模块未就绪（{_import_error}）'


def params():
    module = _module()
    if module is None:
        return None
    cls = getattr(module, 'FieldParams', None)
    return cls() if cls is not None else None


def source_signature(files) -> str:
    module = _module()
    if module is None:
        raise ProductError(unavailable_note())
    return str(module.source_signature(list(files)))


def load(cache_dir: Path, signature: str):
    module = _module()
    if module is None:
        raise ProductError(unavailable_note())
    return module.load(Path(cache_dir), signature)


def save(cache_dir: Path, bundle, reason: str):
    module = _module()
    if module is None:
        raise ProductError(unavailable_note())
    return module.save(Path(cache_dir), bundle, reason=reason)


def to_bundle(job_result):
    """Project our JobResult onto the result_store.ResultBundle contract."""
    module = _module()
    if module is None:
        raise ProductError(unavailable_note())
    cls = getattr(module, 'ResultBundle', None)
    if cls is None:
        return job_result
    try:
        return cls(dataset=job_result.dataset, field=job_result.field,
                   timings=job_result.timings,
                   source_signature=job_result.cache_key or '',
                   source_verified=bool(job_result.source.source_verified),
                   cache_path=None, app_version=APP_VERSION)
    except TypeError:
        return job_result


def save_file(bundle, path: Path) -> Path:
    """Write a self-contained ``.tsr3`` chosen by the user (Save As).

    ``result_store`` writes ``field_<signature[:16]>.tsr3`` inside a cache
    directory; for a user-chosen name the adapter moves that validated file to
    the exact path the user selected (same bytes, same container).
    """
    module = _module()
    if module is None:
        raise ProductError(unavailable_note())
    path = Path(path)
    if path.suffix.lower() != '.tsr3':
        path = path.with_suffix('.tsr3')
    path.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(module, 'save_file'):
        # result_store.save_file(path, bundle, *, reason, overwrite) -> SaveResult
        result = module.save_file(path, bundle, reason='user-save', overwrite=True)
        if not getattr(result, 'ok', False):
            raise ProductError(getattr(result, 'message', '保存计算结果失败'))
        written = Path(getattr(result, 'path', '') or '')
        if not written.is_file():
            raise ProductError('结果模块没有写出保存文件')
        if written.resolve() != path.resolve():
            os.replace(written, path)
        return path
    if hasattr(module, 'save'):
        result = module.save(path.parent, bundle, reason='user-save')
        if not getattr(result, 'ok', False):
            raise ProductError(getattr(result, 'message', '保存计算结果失败'))
        result_path = Path(getattr(result, 'path', '') or '')
        if not result_path.is_file():
            raise ProductError('结果模块没有写出保存文件')
        if result_path.resolve() != path.resolve():
            os.replace(result_path, path)
        return path
    raise ProductError('结果模块缺少 save_file API')


def load_file(path: Path):
    """Open a self-contained saved result; source folder is not required."""
    module = _module()
    if module is None:
        raise ProductError(unavailable_note())
    if hasattr(module, 'load_file'):
        return module.load_file(Path(path))
    raise ProductError('结果模块缺少 load_file API')


def describe_error(exc: BaseException) -> str:
    return f'{exc.__class__.__name__}: {exc}'
