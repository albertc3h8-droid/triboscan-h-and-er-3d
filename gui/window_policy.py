"""Win32 window constraints VTK does not expose.

The shell has no notion of the minimum size the painted layout needs, so a user
can drag the frame below it.  The Pillow skin is then left at its old size while
pointer hit-testing already uses the new layout, and every mouse pick drifts from
what is on screen.  A WM_GETMINMAXINFO subclass on the VTK window handle makes
Windows itself refuse those sizes, so the resize -> skin rebuild path stays in
sync and the pointer mapping stays exact.

Off-screen renders never install anything: tests and evidence keep exact sizes.
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

WM_GETMINMAXINFO = 0x0024
#: SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE
_SWP_STEADY = 0x0002 | 0x0004 | 0x0010
#: Frame thickness estimate for the rare case where the handle cannot be asked.
DEFAULT_FRAME = (16, 39)

if sys.platform == 'win32':
    _SUBCLASSPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, ctypes.c_size_t,
        ctypes.c_ssize_t, ctypes.c_size_t, ctypes.c_size_t)
else:  # pragma: no cover - the product is Windows-only
    _SUBCLASSPROC = None


class _MinMaxInfo(ctypes.Structure):
    _fields_ = [('ptReserved', wintypes.POINT),
                ('ptMaxSize', wintypes.POINT),
                ('ptMaxPosition', wintypes.POINT),
                ('ptMinTrackSize', wintypes.POINT),
                ('ptMaxTrackSize', wintypes.POINT)]


def _def_subclass(hwnd, msg, wparam, lparam) -> int:
    proc = ctypes.windll.comctl32.DefSubclassProc
    proc.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t,
                     ctypes.c_ssize_t]
    proc.restype = ctypes.c_ssize_t
    return int(proc(ctypes.c_void_p(hwnd), msg, wparam, lparam))


def window_frame(hwnd) -> tuple[int, int]:
    """Extra width/height the frame adds around the client area."""
    handle = int(hwnd or 0)
    if sys.platform != 'win32' or handle <= 0:
        return DEFAULT_FRAME
    try:
        user32 = ctypes.windll.user32
        window, client = wintypes.RECT(), wintypes.RECT()
        if (user32.GetWindowRect(ctypes.c_void_p(handle), ctypes.byref(window))
                and user32.GetClientRect(ctypes.c_void_p(handle),
                                         ctypes.byref(client))):
            dx = (window.right - window.left) - (client.right - client.left)
            dy = (window.bottom - window.top) - (client.bottom - client.top)
            if dx >= 0 and dy >= 0:
                return int(dx), int(dy)
    except Exception:  # noqa: BLE001 - the UI must never fail over a hint
        pass
    return DEFAULT_FRAME


def track_minimum(client_floor, frame=DEFAULT_FRAME) -> tuple[int, int]:
    """Window (track) size whose client area reaches the requested floor."""
    return (int(client_floor[0]) + int(frame[0]),
            int(client_floor[1]) + int(frame[1]))


def resize_client(hwnd, client_size) -> bool:
    """Resize the frame so the client area is exactly client_size."""
    handle = int(hwnd or 0)
    if sys.platform != 'win32' or handle <= 0:
        return False
    try:
        width, height = track_minimum(client_size, window_frame(handle))
        return bool(ctypes.windll.user32.SetWindowPos(
            ctypes.c_void_p(handle), None, 0, 0, width, height, _SWP_STEADY))
    except Exception:  # noqa: BLE001
        return False


class MinimumSize:
    """Keep one window's client area at or above a client-size floor.

    floor may be a (width, height) pair or a callable returning one (or None for
    "no floor"), so the owner can answer per monitor DPI instead of freezing the
    value at install time.
    """

    def __init__(self, hwnd, floor, frame=None):
        self.hwnd = int(hwnd or 0)
        self.floor_provider = floor if callable(floor) else (lambda: floor)
        self.frame = frame
        self.installed = False
        self._proc = None
        if sys.platform != 'win32' or self.hwnd <= 0 or _SUBCLASSPROC is None:
            return
        try:
            self._proc = _SUBCLASSPROC(self._on_message)
            self.installed = bool(ctypes.windll.comctl32.SetWindowSubclass(
                ctypes.c_void_p(self.hwnd), self._proc, 1, 0))
        except Exception:  # noqa: BLE001
            self.installed = False

    def _frame(self):
        return self.frame or window_frame(self.hwnd)

    def minimum_track(self):
        """Window size that satisfies the current floor, or None."""
        try:
            floor = self.floor_provider()
        except Exception:  # noqa: BLE001
            return None
        if not floor:
            return None
        return track_minimum(floor, self._frame())

    def _on_message(self, hwnd, msg, wparam, lparam, _subclass_id, _ref):
        if msg == WM_GETMINMAXINFO:
            try:
                track = self.minimum_track()
                if track is not None:
                    info = ctypes.cast(ctypes.c_void_p(lparam),
                                       ctypes.POINTER(_MinMaxInfo)).contents
                    info.ptMinTrackSize.x = int(track[0])
                    info.ptMinTrackSize.y = int(track[1])
            except Exception:  # noqa: BLE001
                pass
        return _def_subclass(hwnd, msg, wparam, lparam)

    def remove(self):
        """Detach the subclass (safe to call twice)."""
        if not self.installed:
            return
        try:
            ctypes.windll.comctl32.RemoveWindowSubclass(
                ctypes.c_void_p(self.hwnd), self._proc, 1)
        finally:
            self.installed = False
