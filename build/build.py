"""Prepare, but never execute, the standard PyInstaller build for TriboScan3D.

This round (t12) deliberately separates **preparation** from **execution**:

* default mode prints (and records) the exact official ``python -m PyInstaller``
  command, its working directory, inputs, expected outputs and the permission
  blockers a real build can hit.  It performs no packaging and imports no
  PyInstaller machinery.
* ``--finalize <dir>`` post-processes a tree produced *by that standard command*
  (assets, versions, SHA256SUMS, manifest).  It never invokes a build either.

The historical ``build/pyi_bootstrap.py`` is kept only as evidence of the
earlier sandbox workaround; this script neither imports nor runs it.  The
standard PyInstaller entry with its isolated child processes is the only
supported path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]          # desktop_app/
BUILD = ROOT / 'build'
SPEC = BUILD / 'triboscan.spec'
ENTRY = BUILD / 'entry.py'
ASSETS = BUILD / 'assets'
WORK = BUILD / 'pyinstaller_work_candidate'
EVIDENCE = BUILD / 'evidence' / 't12'
PLAN_PATH = EVIDENCE / 'build_plan.json'
STDOUT_LOG = EVIDENCE / 'pyinstaller_stdout.log'

# Frozen, already verified package from t5: never touched by this script.
FROZEN_DIST = ROOT / 'dist'
FROZEN_PACKAGE = FROZEN_DIST / 'TriboScan3D_Portable_Windows_x64'

# New candidate lives in its own directory so a failed attempt cannot destroy
# the frozen package or its SHA256SUMS.txt.
CANDIDATE_DIST = ROOT / 'dist_candidate'
BUILT_NAME = 'TriboScan3D'
PACKAGE_NAME = 'TriboScan3D_Portable_Windows_x64_candidate'
CANDIDATE_PACKAGE = CANDIDATE_DIST / PACKAGE_NAME
VERSION = '1.0.0'
PYTHON = ROOT / '.buildenv' / 'Scripts' / 'python.exe'

STANDARD_COMMAND = [
    str(PYTHON), '-m', 'PyInstaller',
    '--noconfirm', '--clean',
    '--distpath', str(CANDIDATE_DIST),
    '--workpath', str(WORK),
    str(SPEC),
]

LICENSE_PACKAGES = {
    'vtk': 'VTK',
    'pyvista': 'PyVista',
    'numpy': 'numpy',
    'matplotlib': 'matplotlib',
    'pillow': 'Pillow',
    'openpyxl': 'openpyxl',
    'et-xmlfile': 'et_xmlfile',
    'pyinstaller': 'PyInstaller',
    'pyinstaller-hooks-contrib': 'PyInstaller hooks contrib',
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _versions() -> dict:
    import importlib.metadata as md

    names = ['Python', 'vtk', 'pyvista', 'numpy', 'matplotlib', 'Pillow',
             'openpyxl', 'PyInstaller', 'pyinstaller-hooks-contrib']
    values = {}
    for name in names:
        if name == 'Python':
            values[name] = sys.version.split()[0]
            continue
        try:
            values[name] = md.version(name)
        except md.PackageNotFoundError:
            values[name] = 'not installed'
    return values


def _plan() -> int:
    """Record and print the standard build command; execute nothing."""
    inputs = [
        ROOT / 'app', ROOT / 'gui', ROOT / 'runtime',
        ROOT / 'integration', ROOT / 'io_exports', ROOT / 'result_store',
        ENTRY, SPEC, BUILD / 'version_info.txt', ASSETS,
    ]
    missing = [str(path) for path in inputs if not path.exists()]
    if missing:
        raise SystemExit('missing build inputs:\n  ' + '\n  '.join(missing))
    if not PYTHON.is_file():
        raise SystemExit(f'build environment interpreter missing: {PYTHON}')
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    plan = {
        'generated_at': datetime.now(timezone.utc).astimezone().isoformat(
            timespec='seconds'),
        'app_version': VERSION,
        'workdir': str(ROOT),
        'interpreter': str(PYTHON),
        'interpreter_versions': _versions(),
        'standard_entry': 'python -m PyInstaller (isolated subprocesses kept)',
        'command': STANDARD_COMMAND,
        'command_shell': (
            f'cd /d "{ROOT}" && '
            f'"{PYTHON}" -m PyInstaller --noconfirm --clean '
            f'--distpath "{CANDIDATE_DIST}" --workpath "{WORK}" '
            f'"{SPEC}" > "{STDOUT_LOG}" 2>&1'),
        'inputs': [str(path) for path in inputs],
        'spec_sha256': _sha256(SPEC),
        'entry_sha256': _sha256(ENTRY),
        'expected_outputs': {
            'candidate_dist': str(CANDIDATE_DIST),
            'candidate_package_in_progress': str(CANDIDATE_DIST / BUILT_NAME),
            'workpath': str(WORK),
            'stdout_stderr_log': str(STDOUT_LOG),
        },
        'finalize_command': (
            f'"{PYTHON}" "{BUILD / "build.py"}" --finalize '
            f'"{CANDIDATE_DIST / BUILT_NAME}"'),
        'must_not_touch': str(FROZEN_PACKAGE),
        'permission_blockers': [
            'PyInstaller runs helper interpreters for hooks/collect_all; under '
            'a restricted token creating their anonymous pipe fails with '
            '"PermissionError: [WinError 5]". Stop and report; do not replace '
            'or monkey-patch the isolated launcher.',
            'The build writes only below ' + str(ROOT) + '; no network access, '
            'no global Python changes and no writes to '
            'native_viewer/.venv are required.',
            'Run from an ordinary interactive account (not a service/sandbox '
            'token) so the VTK/PyVista hook collection can read the .buildenv '
            'site-packages and the Windows temp folder.',
        ],
        'does_not_run_build': True,
        'legacy_note': (
            'build/pyi_bootstrap.py remains in the tree as historical evidence; '
            'it is not imported, not called and not a supported build path.'),
    }
    PLAN_PATH.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + '\n',
                         encoding='utf-8', newline='\n')
    print('standard build command (NOT executed by this script):')
    print(plan['command_shell'])
    print(f'\nworkdir: {ROOT}')
    print(f'inputs: {len(inputs)} paths (all present)')
    print(f'expected package: {CANDIDATE_PACKAGE}')
    print(f'log: {STDOUT_LOG}')
    print(f'plan: {PLAN_PATH}')
    print(f'frozen package left untouched: {FROZEN_PACKAGE}')
    return 0


def _write_version_file(package: Path) -> None:
    versions = _versions()
    built = datetime.now(timezone.utc).astimezone()
    lines = [
        f'TriboScan3D {VERSION}',
        'Windows x64 portable (directory / onedir, no installer, no Python required)',
        '',
        f'built_at: {built.isoformat(timespec="seconds")}',
        f'built_on: {os.environ.get("COMPUTERNAME", "unknown")}',
        'source: triboscan_build/desktop_app (app/gui/runtime/integration, '
        'io_exports, result_store)',
        'build_environment: triboscan_build/desktop_app/.buildenv '
        '(copied from native_viewer/.venv; offline wheels only)',
        '',
        'runtime versions:',
        *[f'  {name}: {value}' for name, value in versions.items()],
        '',
        'launchers:',
        '  TriboScan3D.exe          windowed product launcher',
        '  TriboScan3D_console.exe  same program with console (diagnostics)',
        '',
        'requirements:',
        '  Windows 10/11 x64; OpenGL 3.2+ capable graphics driver.',
        '  Intel iGPU / Remote Desktop / VM are not verified. Not code-signed.',
        '',
        'outputs (created next to the executable): results/, cache/, logs/, config/',
        'source data folders are never modified.',
    ]
    (package / 'VERSION.txt').write_text('\r\n'.join(lines) + '\r\n',
                                         encoding='utf-8')


def _copy_licenses(package: Path) -> int:
    site = Path(sys.prefix) / 'Lib' / 'site-packages'
    out = package / 'licenses'
    out.mkdir(parents=True, exist_ok=True)
    copied = 0
    for dist in sorted(site.glob('*.dist-info')):
        lowered = dist.name.lower()
        label = None
        for key, name in LICENSE_PACKAGES.items():
            if lowered.startswith(key.replace('-', '_') + '-') or \
                    lowered.startswith(key + '-'):
                label = name
                break
        if label is None:
            continue
        candidates = []
        lic_dir = dist / 'licenses'
        if lic_dir.is_dir():
            candidates.extend(sorted(p for p in lic_dir.iterdir() if p.is_file()))
        candidates.extend(sorted(p for p in dist.iterdir()
                                 if p.is_file() and
                                 p.name.upper().startswith(('LICENSE', 'COPYING'))))
        for item in candidates:
            target = out / f'{label}__{item.name}'
            shutil.copy2(item, target)
            copied += 1
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if python_license.is_file():
        shutil.copy2(python_license, out / 'Python__LICENSE.txt')
        copied += 1
    notice = ASSETS / 'THIRD_PARTY_NOTICES.txt'
    if notice.is_file():
        shutil.copy2(notice, package / 'THIRD_PARTY_NOTICES.txt')
    return copied


def _write_config(package: Path) -> None:
    (package / 'config').mkdir(exist_ok=True)
    (package / 'config' / 'app.json').write_text(
        json.dumps({
            'app': 'TriboScan3D',
            'version': VERSION,
            'export_root': 'results',
            'cache_root': 'cache',
            'log_level': 'INFO',
            'note': 'Non-executable configuration. All paths are relative to '
                    'the program folder and never to the source data folders.',
        }, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    for name in ('cache', 'results', 'logs'):
        (package / name).mkdir(exist_ok=True)


def _write_sums(package: Path) -> tuple[int, int]:
    files = sorted(p for p in package.rglob('*') if p.is_file())
    lines = []
    for path in files:
        if path.name == 'SHA256SUMS.txt':
            continue
        rel = path.relative_to(package).as_posix()
        lines.append(f'{_sha256(path)}  {rel}')
    (package / 'SHA256SUMS.txt').write_text('\n'.join(lines) + '\n',
                                            encoding='utf-8', newline='\n')
    return len(lines), sum(p.stat().st_size for p in files)


def _write_evidence_manifest(package: Path) -> None:
    rows = []
    total = 0
    for path in sorted(p for p in package.rglob('*') if p.is_file()):
        size = path.stat().st_size
        total += size
        rows.append({'path': path.relative_to(package).as_posix(),
                     'bytes': size, 'sha256': _sha256(path)})
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / 'package_manifest_candidate.json').write_text(json.dumps({
        'package': PACKAGE_NAME,
        'app_version': VERSION,
        'generated_at': datetime.now(timezone.utc).astimezone().isoformat(
            timespec='seconds'),
        'file_count': len(rows),
        'total_bytes': total,
        'files': rows,
    }, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')


def _dedupe_vtk_libs(package: Path) -> None:
    """Drop ``_internal/vtk.libs`` only when every byte is duplicated flat.

    PyInstaller keeps a nested copy although the spec flattens the same VTK
    DLLs into ``_internal``.  A copy is removed **only** after proving that a
    file with identical SHA-256 exists next to ``python312.dll``; a different
    same-named file aborts the finalize instead of silently deleting data.
    """
    nested = package / '_internal' / 'vtk.libs'
    if not nested.is_dir():
        return
    internal = package / '_internal'
    checked = 0
    for path in sorted(nested.rglob('*')):
        if not path.is_file():
            continue
        flat = internal / path.relative_to(nested)
        if not flat.is_file():
            raise SystemExit(f'refusing to drop {nested}: no flattened copy '
                             f'for {path.relative_to(nested)}')
        if _sha256(path) != _sha256(flat):
            raise SystemExit(f'refusing to drop {nested}: different bytes for '
                             f'{path.relative_to(nested)}')
        checked += 1
    shutil.rmtree(nested)
    print(f'VTK dedupe: removed {nested} ({checked} byte-identical DLLs)')


def _check_contents(package: Path) -> None:
    problems = []
    if not (package / 'TriboScan3D.exe').is_file():
        problems.append('missing TriboScan3D.exe')
    if not (package / '_internal').is_dir():
        problems.append('missing _internal runtime folder')
    for path in package.rglob('*_analysis.txt'):
        problems.append(f'measurement data leaked into package: {path}')
    for path in package.rglob('*.py'):
        problems.append(f'Python source leaked into package: {path}')
    for banned in ('docs', 'tests', 'build', '.buildenv'):
        if (package / banned).exists():
            problems.append(f'development folder leaked: {banned}')
    for runtime_dir in ('cache', 'results', 'logs'):
        stray = [p for p in (package / runtime_dir).rglob('*') if p.is_file()]
        if stray:
            problems.append(f'{runtime_dir}/ must ship empty, found: '
                            f'{[p.name for p in stray[:5]]}')
    if problems:
        raise SystemExit('package content check failed:\n  ' +
                         '\n  '.join(problems[:20]))


def _finalize(built: Path) -> Path:
    if not built.is_dir():
        raise SystemExit(f'missing build output: {built}')
    package = CANDIDATE_PACKAGE
    frozen = FROZEN_PACKAGE.resolve()
    if package.resolve() == frozen or frozen in package.resolve().parents:
        raise SystemExit('refusing to finalize inside the frozen t5 package')
    if package.exists():
        shutil.rmtree(package)
    built.rename(package)
    for cache in package.rglob('__pycache__'):
        shutil.rmtree(cache, ignore_errors=True)
    _write_config(package)
    shutil.copy2(ASSETS / '说明_README.txt', package / '说明_README.txt')
    _write_version_file(package)
    licenses = _copy_licenses(package)
    _dedupe_vtk_libs(package)
    _check_contents(package)
    count, total = _write_sums(package)
    _write_evidence_manifest(package)
    print(f'package: {package}')
    print(f'files: {count}, bytes: {total} ({total / (1024 ** 3):.2f} GiB)')
    print(f'licenses copied: {licenses}')
    return package


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', action='store_true',
                        help='print/record the standard command (default; '
                             'never executes a build)')
    parser.add_argument('--finalize', type=Path, default=None,
                        help='post-process a tree produced by the standard '
                             'PyInstaller command')
    args = parser.parse_args(argv)
    if args.finalize is not None:
        package = _finalize(args.finalize)
        print(f'OK {package}')
        return 0
    return _plan()


if __name__ == '__main__':
    raise SystemExit(main())
