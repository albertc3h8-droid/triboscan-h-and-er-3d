"""Load jobs: source folder -> JobResult, or saved .tsr3 -> JobResult.

Runs only in the background worker.  It never constructs a Plotter and never
touches a render window.  All timings use ``time.perf_counter``.
"""
from __future__ import annotations

from pathlib import Path
import time

from app.jobs import JobHandle
from gui import i18n
from integration import result_adapter
from integration.protocols import JobResult, SourceInfo, Timings
from runtime import loader, points_csv, precompute, views
from runtime.errors import InputError, ProductError


def _language(language: str | None = None, settings_path=None) -> str:
    return (i18n.normalize_language(language)
            or i18n.preferred_language(settings_path))


def scan_analysis_files(directory, *, language: str | None = None,
                        settings_path=None) -> list[Path]:
    lang = _language(language, settings_path)
    path = Path(directory)
    if not path.exists():
        raise InputError(i18n.tr(lang, 'error.data_folder_missing', path=path))
    if not path.is_dir():
        raise InputError(i18n.tr(lang, 'error.select_folder', path=path))
    files = sorted((p for p in path.iterdir()
                    if p.is_file() and p.suffix.lower() == '.txt'
                    and p.name.endswith('_analysis.txt')),
                   key=lambda p: p.name.lower())
    if not files:
        raise InputError(i18n.tr(lang, 'error.no_analysis_files', path=path))
    return files


def _bundle_field(bundle):
    return views.as_field(bundle.field)


def _spacing_for(dataset, field, bundle=None):
    stored = getattr(bundle, 'raw_spacing', None) if bundle is not None else None
    if stored:
        return stored, 0.0
    started = time.perf_counter()
    spacing = precompute.compute_spacing(dataset, precompute.display_z_scale(field))
    return spacing, time.perf_counter() - started


def _result_from_cache(bundle, *, source: SourceInfo, read_s: float,
                       cache_load_s: float, key: str, warnings: list[str],
                       cache_reason: str = '') -> JobResult:
    dataset = views.as_dataset(bundle.dataset)
    field = _bundle_field(bundle)
    spacing, spacing_s = _spacing_for(dataset, field, bundle)
    timings = Timings(read_s=read_s, compute_s=spacing_s,
                      cache_load_s=cache_load_s, compute_steps=[
                          {'step': 'spacing', 'seconds': spacing_s}])
    return JobResult(dataset=dataset, field=field, raw_spacing=spacing,
                     source=source, timings=timings, cache_hit=True,
                     cache_reason=cache_reason, cache_key=key,
                     warnings=list(warnings), build_count=0)


def load_directory(handle: JobHandle, *, directory: str, cache_dir: Path,
                   use_cache: bool = True, force_recompute: bool = False,
                   language: str | None = None, settings_path=None,
                   ) -> JobResult:
    started = time.perf_counter()
    lang = _language(language, settings_path)
    precompute.reset_build_field_calls()
    handle.emit('scan', i18n.tr(lang, 'stage.detail.scan', directory=directory),
                .04)
    files = scan_analysis_files(directory, language=lang)
    handle.check_cancel()
    path = Path(directory).resolve()
    label = path.parent.name if path.name.lower() == 'input' else path.name
    warnings: list[str] = []
    key = None
    read_s = 0.0
    cache_load_s = 0.0
    cache_reason = ''

    cache_ready = result_adapter.available()
    if cache_ready:
        # The signature is also needed for a later user save; compute it even
        # when only the cache *lookup* is disabled.
        handle.emit('signature', i18n.tr(lang, 'stage.detail.signature',
                                         count=len(files)), .12)
        try:
            key = result_adapter.source_signature(files)
        except Exception as exc:  # noqa: BLE001 - cache is optional
            warnings.append(f'cache signature unavailable: {exc}')
            key = None
        handle.check_cancel()

    if use_cache and key is not None and not force_recompute:
        handle.emit('cache_check',
                    i18n.tr(lang, 'stage.detail.cache_check'), .20)
        t0 = time.perf_counter()
        try:
            bundle = result_adapter.load(cache_dir, key)
        except Exception as exc:  # noqa: BLE001 - corruption must not kill the load
            warnings.append(f'cache ignored: {exc}')
            bundle = None
        cache_load_s = time.perf_counter() - t0
        if bundle is not None:
            handle.emit('cache_hit',
                        i18n.tr(lang, 'stage.detail.cache_hit'), .70)
            source = SourceInfo(
                kind='directory', path=str(path), display_label=label,
                files=[{'name': p.name, 'bytes': p.stat().st_size} for p in files],
                signature=key, source_verified=True,
                note='field loaded from cache; build_field was not called')
            result = _result_from_cache(
                bundle, source=source, read_s=read_s, cache_load_s=cache_load_s,
                key=key, warnings=warnings, cache_reason='cache hit')
            result.timings.total_s = time.perf_counter() - started
            return result
        cache_reason = 'cache miss (or disabled/unsupported)'

    handle.emit('parse', i18n.tr(lang, 'stage.detail.parse', count=len(files)),
                .24)
    try:
        dataset = loader.load_directory(path)
    except loader.DataError as exc:
        raise InputError(i18n.tr(lang, 'error.data_invalid', error=exc)) from exc
    read_s = time.perf_counter() - started - cache_load_s
    handle.check_cancel()

    handle.emit('field', i18n.tr(lang, 'stage.detail.field'), .35)
    try:
        computed = precompute.precompute(
            dataset, check_cancel=handle.check_cancel,
            progress=lambda stage, extra: handle.emit(
                stage, i18n.tr(lang, f'stage.detail.{stage}'), .55))
    except precompute.ComputeError as exc:
        raise InputError(i18n.tr(lang, 'error.compute_failed', error=exc)) from exc
    handle.check_cancel()
    handle.emit('ready', i18n.tr(lang, 'stage.detail.ready'), 1.0)

    source = SourceInfo(
        kind='directory', path=str(path), display_label=label,
        files=[{'name': p.name, 'bytes': p.stat().st_size} for p in files],
        signature=key, point_count=dataset.point_count,
        group_count=dataset.group_count,
        source_sha256=str(getattr(dataset, 'source_sha256', '') or ''),
        source_verified=True,
        note=cache_reason or 'fresh compute')
    timings = Timings(read_s=read_s, compute_s=computed.compute_s,
                      cache_load_s=cache_load_s,
                      compute_steps=computed.steps,
                      build_count=precompute.build_field_calls())
    result = JobResult(dataset=dataset, field=views.as_field(computed.field),
                       raw_spacing=computed.raw_spacing, source=source,
                       timings=timings, cache_hit=False, cache_reason=cache_reason,
                       cache_key=key, warnings=warnings,
                       build_count=timings.build_count)
    result.timings.total_s = time.perf_counter() - started
    return result


def load_result_file(handle: JobHandle, *, path: str,
                     language: str | None = None, settings_path=None) -> JobResult:
    started = time.perf_counter()
    lang = _language(language, settings_path)
    precompute.reset_build_field_calls()
    file_path = Path(path)
    if not file_path.exists():
        raise InputError(i18n.tr(lang, 'error.saved_result_missing',
                                 path=file_path))
    if not result_adapter.available():
        raise ProductError(i18n.tr(lang, 'error.result_adapter_unavailable'))
    handle.emit('result_file', i18n.tr(
        lang, 'stage.detail.result_file', name=file_path.name), .15)
    t0 = time.perf_counter()
    try:
        bundle = result_adapter.load_file(file_path)
    except ProductError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ProductError(i18n.tr(lang, 'error.open_saved_result',
                                   path=file_path, error=exc)) from exc
    cache_load_s = time.perf_counter() - t0
    handle.check_cancel()
    dataset = views.as_dataset(bundle.dataset)
    field = _bundle_field(bundle)
    spacing, spacing_s = _spacing_for(dataset, field, bundle)
    verified = bool(getattr(bundle, 'source_verified', False))
    source = SourceInfo(
        kind='result_file', path=str(file_path),
        display_label=file_path.stem,
        signature=str(getattr(bundle, 'source_signature', '') or '') or None,
        point_count=dataset.point_count, group_count=dataset.group_count,
        source_sha256=str(getattr(dataset, 'source_sha256', '') or ''),
        source_verified=verified,
        note=('saved result file; the original data folder is not required'
              if not verified else 'saved result file; source verified at save time'))
    timings = Timings(read_s=0.0, compute_s=spacing_s,
                      cache_load_s=cache_load_s,
                      compute_steps=[{'step': 'spacing', 'seconds': spacing_s}])
    result = JobResult(dataset=dataset, field=field, raw_spacing=spacing,
                       source=source, timings=timings, cache_hit=True,
                       cache_reason='opened saved .tsr3 result',
                       cache_key=source.signature, warnings=[], build_count=0)
    result.timings.total_s = time.perf_counter() - started
    handle.emit('ready', i18n.tr(lang, 'stage.detail.result_ready'), 1.0)
    return result


def _points_csv_message(language: str, error) -> str:
    """Turn a PointsCsvError into the user's language (never a raw traceback)."""
    details = error.details
    if error.kind == 'header':
        return i18n.tr(language, 'error.points_csv_header',
                       missing=', '.join(details.get('missing') or []) or '-',
                       found=', '.join(details.get('found') or []) or '-')
    if error.kind == 'row':
        return i18n.tr(language, 'error.points_csv_row',
                       row=details.get('row', '?'),
                       name=details.get('name', ''),
                       reason=details.get('reason', str(error)))
    return i18n.tr(language, 'error.data_invalid', error=error)


def load_points_csv(handle: JobHandle, *, path: str,
                    language: str | None = None,
                    settings_path=None, rebuild_field: bool = True,
                    ) -> JobResult:
    """Open a raw-points CSV export and, by default, rebuild its field.

    The CSV stores the real measured points only, so the interpolated field is
    not part of the file.  Rebuilding it takes well under a second for the
    exports this program writes and reproduces the saved field exactly; the
    viewer then keeps every depth/slice/reference control working.  With
    ``rebuild_field=False`` (or if the rebuild fails) the result stays
    points-only: measured points, no interpolated sample.
    """
    started = time.perf_counter()
    lang = _language(language, settings_path)
    precompute.reset_build_field_calls()
    file_path = Path(path)
    if not file_path.exists():
        raise InputError(i18n.tr(lang, 'error.saved_result_missing',
                                 path=file_path))
    handle.emit('points_csv', i18n.tr(
        lang, 'stage.detail.points_csv', name=file_path.name), .30)
    try:
        dataset = points_csv.load_points_csv(file_path)
    except points_csv.PointsCsvError as exc:
        raise InputError(_points_csv_message(lang, exc)) from exc
    handle.check_cancel()
    field = points_csv.points_only_field(dataset)
    spacing, spacing_s = _spacing_for(dataset, field)
    has_field = False
    warnings: list[str] = []
    note = 'raw points CSV; measured points only (no interpolated field)'
    compute_steps = [{'step': 'spacing', 'seconds': spacing_s}]
    compute_s = spacing_s
    build_count = 0
    if rebuild_field:
        handle.emit('field', i18n.tr(lang, 'stage.detail.field'), .55)
        try:
            computed = precompute.precompute(
                dataset, check_cancel=handle.check_cancel,
                progress=lambda stage, extra: handle.emit(
                    stage, i18n.tr(lang, f'stage.detail.{stage}'), .70))
        except precompute.ComputeError as exc:
            # Keep the measured points usable instead of refusing the file.
            warnings.append(f'field rebuild failed: {exc}')
            note = 'raw points CSV; field rebuild failed, measured points only'
        else:
            field = views.as_field(computed.field)
            spacing = computed.raw_spacing
            compute_steps = list(computed.steps)
            compute_s = computed.compute_s
            build_count = precompute.build_field_calls()
            has_field = True
            note = 'raw points CSV; field rebuilt from these measured points'
        handle.check_cancel()
    source = SourceInfo(
        kind='points_csv', path=str(file_path),
        display_label=file_path.stem,
        files=[{'name': file_path.name, 'bytes': file_path.stat().st_size}],
        point_count=dataset.point_count, group_count=dataset.group_count,
        source_sha256=dict(dataset.source_sha256).get(file_path.name, ''),
        source_verified=True, note=note)
    timings = Timings(read_s=0.0, compute_s=compute_s,
                      compute_steps=compute_steps, build_count=build_count)
    result = JobResult(dataset=dataset, field=field, raw_spacing=spacing,
                       source=source, timings=timings, cache_hit=False,
                       cache_reason=(
                           'opened a raw points CSV export; field rebuilt '
                           'from its points' if has_field
                           else 'opened a raw points CSV export'),
                       cache_key=None, warnings=warnings,
                       build_count=build_count, has_field=has_field)
    result.timings.total_s = time.perf_counter() - started
    handle.emit('ready', i18n.tr(lang, 'stage.detail.result_ready'), 1.0)
    return result
