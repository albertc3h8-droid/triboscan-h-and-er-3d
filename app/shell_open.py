"""Thin, mockable Windows shell integration for export feedback.

The only product behavior here is "open this folder in Explorer".  Tests replace
``open_folder`` (or its process/shell primitives) so no real Explorer window is
ever opened.

``open_folder`` deliberately tries ``explorer.exe`` directly first: it does not
depend on ShellExecute/COM/file associations.  If that cannot start, it falls
back to ``os.startfile``.  Failures are returned to the caller as ``False`` and
the real exception chain is exposed through :func:`last_error` so the app log
can say why opening failed, without breaking the already-successful export.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

_last_error = ''


def last_error() -> str:
    """Reason collected by the most recent :func:`open_folder` call."""
    return _last_error


def open_folder(path: Path | str) -> bool:
    """Open *path* in Explorer; return False instead of raising on failure.

    The export itself must stay successful even when the shell cannot open a
    window, so every failure is captured.  On failure :func:`last_error`
    contains both attempted mechanisms and their real exception text.
    """
    global _last_error
    _last_error = ''
    target = str(Path(path))
    errors: list[str] = []

    if not Path(path).is_dir():
        errors.append(f'not a directory: {target}')
    else:
        try:
            subprocess.Popen(['explorer.exe', target], shell=False)
            return True
        except Exception as exc:  # noqa: BLE001 - shell integration is best effort
            errors.append(
                f'explorer.exe: {type(exc).__name__}: {exc}')

    # Fallback: shell file association (os.startfile cannot spawn a process
    # with an explicit argument list, but it can still open a folder).
    try:
        os.startfile(target)
        return True
    except Exception as exc:  # noqa: BLE001 - shell integration is best effort
        errors.append(f'os.startfile: {type(exc).__name__}: {exc}')

    _last_error = '; '.join(errors) or 'unknown failure'
    return False
