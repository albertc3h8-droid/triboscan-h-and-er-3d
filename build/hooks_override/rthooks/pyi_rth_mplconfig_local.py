"""Portable Matplotlib config directory for the frozen product.

Replaces PyInstaller's built-in runtime hook, which places the config in the
system temporary area.  The portable product keeps all writable state beside
the executable, so the config directory is ``<package>/cache/mpl``.
"""
import os
import sys

_base = os.path.dirname(sys.executable)
_cfg = os.path.join(_base, 'cache', 'mpl')
try:
    os.makedirs(_cfg, exist_ok=True)
    os.environ['MPLCONFIGDIR'] = _cfg
except Exception:
    # Startup must never fail because the cache directory is unavailable.
    pass
