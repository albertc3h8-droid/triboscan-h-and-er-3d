"""PyInstaller launcher that works inside a no-pipe (restricted) sandbox.

PyInstaller's ``isolated`` helper starts a child Python and talks over Win32
anonymous pipes.  In this build sandbox ``CreatePipe`` is denied
(``PermissionError: [WinError 5]``), which aborts hook collection.  Running the
"isolated" calls inline in the build process is acceptable here: the build is
offline, deterministic and short-lived.  Nothing in the produced package
depends on this shim.
"""
from __future__ import annotations

import sys

from PyInstaller import isolated
from PyInstaller.isolated import _parent


class _InlineChild:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def call(self, function, *args, **kwargs):
        return function(*args, **kwargs)


_parent.Python = _InlineChild
isolated.Python = _InlineChild

from PyInstaller.__main__ import run  # noqa: E402

if __name__ == '__main__':
    sys.exit(run())
