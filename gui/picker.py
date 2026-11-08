"""Native Win32 file/folder pickers without Qt or tkinter.

Only ``ctypes`` + ``comdlg32``/``shell32``/``ole32`` are used, so the portable
package needs no extra GUI toolkit.  Every failure raises a user-facing
:class:`ProductError`; the app never invents a path.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path

from runtime.errors import ProductError

_ole32 = ctypes.WinDLL('ole32', use_last_error=True)
_shell32 = ctypes.WinDLL('shell32', use_last_error=True)
_comdlg32 = ctypes.WinDLL('comdlg32', use_last_error=True)
_user32 = ctypes.WinDLL('user32', use_last_error=True)


class BROWSEINFOW(ctypes.Structure):
    _fields_ = [('hwndOwner', wintypes.HWND),
                ('pidlRoot', ctypes.c_void_p),
                ('pszDisplayName', wintypes.LPWSTR),
                ('lpszTitle', wintypes.LPCWSTR),
                ('ulFlags', wintypes.UINT),
                ('lpfn', ctypes.c_void_p),
                ('lParam', wintypes.LPARAM),
                ('iImage', ctypes.c_int)]


class OPENFILENAMEW(ctypes.Structure):
    _fields_ = [('lStructSize', wintypes.DWORD),
                ('hwndOwner', wintypes.HWND),
                ('hInstance', wintypes.HINSTANCE),
                ('lpstrFilter', wintypes.LPCWSTR),
                ('lpstrCustomFilter', wintypes.LPWSTR),
                ('nMaxCustFilter', wintypes.DWORD),
                ('nFilterIndex', wintypes.DWORD),
                ('lpstrFile', wintypes.LPWSTR),
                ('nMaxFile', wintypes.DWORD),
                ('lpstrFileTitle', wintypes.LPWSTR),
                ('nMaxFileTitle', wintypes.DWORD),
                ('lpstrInitialDir', wintypes.LPCWSTR),
                ('lpstrTitle', wintypes.LPCWSTR),
                ('Flags', wintypes.DWORD),
                ('nFileOffset', wintypes.WORD),
                ('nFileExtension', wintypes.WORD),
                ('lpstrDefExt', wintypes.LPCWSTR),
                ('lCustData', wintypes.LPARAM),
                ('lpfnHook', ctypes.c_void_p),
                ('lpTemplateName', wintypes.LPCWSTR),
                ('pvReserved', ctypes.c_void_p),
                ('dwReserved', wintypes.DWORD),
                ('FlagsEx', wintypes.DWORD)]


_shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
_shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFOW)]
_shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
_shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
# Start SHBrowseForFolderW at the caller's folder (t74).
_shell32.SHSimpleIDListFromPath.restype = ctypes.c_void_p
_shell32.SHSimpleIDListFromPath.argtypes = [wintypes.LPCWSTR]
# COM/Win32 prototypes: PIDL and HWND are pointer-sized.  Without these,
# ctypes defaults integer arguments to 32-bit ``c_int`` and a 64-bit PIDL can
# raise ``OverflowError`` (t28: gui/picker.py:97).
_ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
_ole32.CoTaskMemFree.restype = None
_ole32.CoInitialize.argtypes = [ctypes.c_void_p]
_ole32.CoInitialize.restype = ctypes.c_long       # HRESULT
_ole32.CoUninitialize.argtypes = []
_ole32.CoUninitialize.restype = None
_user32.GetActiveWindow.argtypes = []
_user32.GetActiveWindow.restype = wintypes.HWND
_comdlg32.GetOpenFileNameW.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
_comdlg32.GetOpenFileNameW.restype = wintypes.BOOL
_comdlg32.GetSaveFileNameW.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
_comdlg32.GetSaveFileNameW.restype = wintypes.BOOL
_OFN_OVERWRITEPROMPT = 0x00000002
_OFN_PATHMUSTEXIST = 0x00000800
_OFN_FILEMUSTEXIST = 0x00001000
_OFN_NOCHANGEDIR = 0x00000008
_BIF_RETURNONLYFSDIRS = 0x00000001
_BIF_NEWDIALOGSTYLE = 0x00000040
_BFFM_INITIALIZED = 0x00000001
_BFFM_SETSELECTIONW = 0x0400 + 103
_SHELL_CALLBACK = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND,
                                     wintypes.UINT, wintypes.LPARAM,
                                     wintypes.LPARAM)
_user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM]
_user32.SendMessageW.restype = ctypes.c_void_p
_send_message = _user32.SendMessageW


def _browse_info(initial: str | None, title: str = 'Select folder',
                 send=None):
    """Build BROWSEINFOW with pidlRoot=NULL and a BFFM selection callback.

    Windows 11 shows an extra "folder unavailable" dialog when ``pidlRoot`` is
    non-NULL, so the initial folder is selected from ``BFFM_INITIALIZED``
    instead.  The callback and its path buffer are returned to the caller so
    they stay alive for the whole ``SHBrowseForFolderW`` call.
    """
    send = _send_message if send is None else send
    target = None
    if initial:
        try:
            candidate = Path(initial)
            if candidate.is_dir():
                target = ctypes.create_unicode_buffer(str(candidate))
        except (OSError, ValueError):
            target = None

    def on_event(hwnd, message, _lparam, _data):
        if message == _BFFM_INITIALIZED and target is not None:
            try:
                send(hwnd, _BFFM_SETSELECTIONW, 1,
                     ctypes.cast(target, ctypes.c_void_p).value)
            except OSError:
                pass
        return 0

    callback = _SHELL_CALLBACK(on_event)
    info = BROWSEINFOW()
    info.hwndOwner = _user32.GetActiveWindow()
    info.pidlRoot = None
    info.pszDisplayName = None
    info.lpszTitle = title
    info.ulFlags = _BIF_RETURNONLYFSDIRS | _BIF_NEWDIALOGSTYLE
    info.lpfn = ctypes.cast(callback, ctypes.c_void_p)
    info.lParam = 0
    info.iImage = 0
    return info, callback


def choose_folder(title: str = 'Select data folder',
                  initial: str | None = None) -> str | None:
    display = ctypes.create_unicode_buffer(260)
    info, callback = _browse_info(initial, title)
    info.pszDisplayName = ctypes.cast(display, wintypes.LPWSTR)
    # Keep the callback (and the path buffer captured by it) alive until the
    # modal dialog returns; it is referenced by the local variable only.
    _keepalive = (info, callback, display)
    _ole32.CoInitialize(None)
    try:
        try:
            pidl = _shell32.SHBrowseForFolderW(ctypes.byref(info))
        except OSError as exc:
            raise ProductError(f'The folder dialog could not be opened: {exc}') from exc
        if not pidl:
            return None
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            if not _shell32.SHGetPathFromIDListW(pidl, buffer):
                raise ProductError('The folder dialog returned a path Windows '
                                   'could not convert. Please choose the folder again.')
            return buffer.value
        finally:
            _ole32.CoTaskMemFree(pidl)
    finally:
        _ole32.CoUninitialize()


def _file_dialog(title: str, filter_pairs, initial: str | None,
                 default_ext: str | None, save: bool) -> str | None:
    if not filter_pairs:
        filter_pairs = [('All files', '*.*')]
    parts = []
    for label, pattern in filter_pairs:
        parts.append(f'{label}\0{pattern}\0')
    parts.append('\0')
    filter_text = ''.join(parts)
    buffer = ctypes.create_unicode_buffer(32768)
    if initial:
        buffer.value = initial
    info = OPENFILENAMEW()
    info.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    info.hwndOwner = _user32.GetActiveWindow()
    info.lpstrFilter = filter_text
    info.nFilterIndex = 1
    info.lpstrFile = ctypes.cast(buffer, wintypes.LPWSTR)
    info.nMaxFile = len(buffer)
    info.lpstrInitialDir = None
    info.lpstrTitle = title
    info.Flags = _OFN_NOCHANGEDIR | (_OFN_OVERWRITEPROMPT if save
                                     else (_OFN_FILEMUSTEXIST | _OFN_PATHMUSTEXIST))
    info.lpstrDefExt = default_ext
    try:
        if save:
            ok = _comdlg32.GetSaveFileNameW(ctypes.byref(info))
        else:
            ok = _comdlg32.GetOpenFileNameW(ctypes.byref(info))
    except OSError as exc:
        raise ProductError(f'The file dialog could not be opened: {exc}') from exc
    if not ok:
        return None
    return buffer.value


def choose_open_file(title: str, filter_pairs, initial: str | None = None) -> str | None:
    return _file_dialog(title, filter_pairs, initial, None, save=False)


def choose_save_file(title: str, filter_pairs, initial: str | None = None,
                     default_ext: str | None = None) -> str | None:
    return _file_dialog(title, filter_pairs, initial, default_ext, save=True)


def result_file_filter():
    """Save/open filter: saved results and this program's raw-points CSV."""
    return [('TriboScan3D result (*.tsr3, *.csv)', '*.tsr3;*.csv'),
            ('Saved result (*.tsr3)', '*.tsr3'),
            ('Raw points export (*.csv)', '*.csv'),
            ('All files', '*.*')]


def data_folder_hint(path: Path) -> str:
    return f'Select the folder with *_analysis.txt files (for example {path})'
