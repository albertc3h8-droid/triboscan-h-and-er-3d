"""Writable-folder layout for the single-folder portable program.

The program never guesses a fallback under the user profile or ``%TEMP%``: all
writable state lives below one root:

* frozen (PyInstaller ``onedir``): ``<folder containing TriboScan3D.exe>``
* source checkout: ``triboscan_build/desktop_app``

Folders: ``cache/`` (result cache + matplotlib cache + process temp),
``results/`` (user exports and saved ``.tsr3`` files), ``logs/``.

``prepare()`` must run before PyVista/matplotlib/openpyxl are imported so that
``MPLCONFIGDIR`` points at the program's own ``cache/mpl`` folder and the
process ``TEMP``/``TMP``/``TMPDIR`` point at ``cache/tmp``.  A restricted
account that cannot write there gets a clear error, never a silent redirect to
the system temp folder.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import sys

from .errors import LayoutError

APP_NAME = 'TriboScan3D'
APP_VERSION = '1.0.0'
RESULT_SUFFIX = '.tsr3'
CACHE_SCHEMA = 1


@dataclass(frozen=True)
class Paths:
    root: Path
    cache: Path
    results: Path
    logs: Path
    config: Path
    mpl_cache: Path
    tmp: Path | None = None

    def describe(self) -> str:
        return (f'program folder: {self.root}\n'
                f'cache: {self.cache}\n'
                f'temp: {self.tmp}\n'
                f'results: {self.results}\n'
                f'logs: {self.logs}')


def app_root() -> Path:
    """Folder that contains the executable (frozen) or the source package."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _ensure_writable(path: Path, description: str) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LayoutError(
            f'Cannot create the program {description} folder:\n{path}\n\n'
            f'{exc}\n\n'
            'Move the program to a folder your account can write to, or ask '
            'your administrator to grant write access to this folder. '
            'No fallback folder is used.') from exc
    probe = path / '.write_probe'
    try:
        probe.write_text('ok', encoding='ascii')
        probe.unlink()
    except OSError as exc:
        raise LayoutError(
            f'The program {description} folder is not writable:\n{path}\n\n'
            f'{exc}\n\n'
            'Move the program to a folder your account can write to, or ask '
            'your administrator to grant write access to this folder. '
            'No fallback folder is used.') from exc


def prepare(root: Path | None = None) -> Paths:
    """Create and verify the program's own folders, config dirs and temp dir.

    Raises :class:`LayoutError` with the exact path and the reason.  It never
    redirects to LOCALAPPDATA, the inherited TEMP or another user-writable
    location.
    """
    base = Path(root).resolve() if root is not None else app_root()
    paths = Paths(root=base, cache=base / 'cache',
                  results=base / 'results', logs=base / 'logs',
                  config=base / 'config', mpl_cache=base / 'cache' / 'mpl',
                  tmp=base / 'cache' / 'tmp')
    for folder, description in ((paths.cache, 'cache'),
                                (paths.tmp, 'temporary'),
                                (paths.results, 'results'),
                                (paths.logs, 'log'),
                                (paths.config, 'config'),
                                (paths.mpl_cache, 'matplotlib cache')):
        _ensure_writable(folder, description)
    # Set before PyVista/matplotlib/openpyxl import; do not setdefault: the
    # program owns these variables and must not inherit external values.
    os.environ['MPLCONFIGDIR'] = str(paths.mpl_cache)
    os.environ['TEMP'] = str(paths.tmp)
    os.environ['TMP'] = str(paths.tmp)
    os.environ['TMPDIR'] = str(paths.tmp)
    # tempfile caches the first result; clear it so gettempdir() sees the
    # process-local value immediately (also matters to in-process tests).
    import tempfile
    tempfile.tempdir = None
    return paths
