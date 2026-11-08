"""t30: pin 64-bit Win32 ctypes prototypes in gui/picker.py.

The t28 crash was ``OverflowError: int too long to convert`` at
``_ole32.CoTaskMemFree(pidl)`` because that function had no ``argtypes`` and
ctypes therefore converted a 64-bit PIDL as a 32-bit ``c_int``.  These tests
do not open a dialog and never pass a fake address to the real free function:
the successful/cancel/error paths use fake shell/COM objects, and the high
pointer is carried through a WINFUNCTYPE callback.
"""
from __future__ import annotations

import ctypes
import unittest
from ctypes import wintypes
from unittest import mock

from gui import picker

HIGH_PIDL = 0x1_2345_6789          # > 2**32, definitely not a 32-bit value
FAKE_PATH = r'C:\t30 fake data folder'


class FakeShell32:
    def __init__(self, pidl, path_ok=True):
        self.pidl = pidl
        self.path_ok = path_ok
        self.browse_calls = 0
        self.path_calls = []

    def SHBrowseForFolderW(self, info_ref):
        self.browse_calls += 1
        return self.pidl

    def SHGetPathFromIDListW(self, pidl, buffer):
        self.path_calls.append(pidl)
        if not self.path_ok:
            return 0
        buffer.value = FAKE_PATH
        return 1


class FakeOle32:
    def __init__(self):
        self.init_calls = 0
        self.uninit_calls = 0
        self.freed = []
        # A real C callback with a pointer-sized argument; deliberately not the
        # real CoTaskMemFree, so a fake address is never freed.
        self.CoTaskMemFree = ctypes.WINFUNCTYPE(None, ctypes.c_void_p)(self._record_free)

    def _record_free(self, pointer):
        self.freed.append(pointer)

    def CoInitialize(self, pv):
        self.init_calls += 1
        return 0

    def CoUninitialize(self):
        self.uninit_calls += 1


class FakeUser32:
    def __init__(self, hwnd=0):
        self.hwnd = hwnd

    def GetActiveWindow(self):
        return self.hwnd


class PickerPrototypeTests(unittest.TestCase):

    def test_cotaskmemfree_declares_pointer_and_void_return(self):
        self.assertEqual(picker._ole32.CoTaskMemFree.argtypes, [ctypes.c_void_p])
        self.assertIsNone(picker._ole32.CoTaskMemFree.restype)

    def test_com_prototypes(self):
        self.assertEqual(picker._ole32.CoInitialize.argtypes, [ctypes.c_void_p])
        self.assertIs(picker._ole32.CoInitialize.restype, ctypes.c_long)
        self.assertEqual(picker._ole32.CoUninitialize.argtypes, [])
        self.assertIsNone(picker._ole32.CoUninitialize.restype)

    def test_getactivewindow_returns_hwnd(self):
        self.assertEqual(picker._user32.GetActiveWindow.argtypes, [])
        self.assertIs(picker._user32.GetActiveWindow.restype, wintypes.HWND)

    def test_file_dialog_prototypes(self):
        for name in ('GetOpenFileNameW', 'GetSaveFileNameW'):
            fn = getattr(picker._comdlg32, name)
            self.assertEqual(len(fn.argtypes), 1, name)
            self.assertIs(fn.argtypes[0]._type_, picker.OPENFILENAMEW, name)
            self.assertIs(fn.restype, wintypes.BOOL, name)

    def test_high_pointer_callback_preserves_all_bits(self):
        seen = []
        cb = ctypes.WINFUNCTYPE(None, ctypes.c_void_p)(lambda ptr: seen.append(ptr))
        cb(HIGH_PIDL)
        self.assertEqual(seen, [HIGH_PIDL])

    def test_choose_folder_success_frees_once_and_returns_path(self):
        shell = FakeShell32(HIGH_PIDL)
        ole = FakeOle32()
        with mock.patch.object(picker, '_shell32', shell), \
                mock.patch.object(picker, '_ole32', ole), \
                mock.patch.object(picker, '_user32', FakeUser32()):
            result = picker.choose_folder('t30 fixture')

        self.assertEqual(result, FAKE_PATH)
        self.assertEqual(shell.browse_calls, 1)
        self.assertEqual(shell.path_calls, [HIGH_PIDL])
        self.assertEqual(ole.init_calls, 1)
        self.assertEqual(ole.uninit_calls, 1)
        self.assertEqual(ole.freed, [HIGH_PIDL])

    def test_choose_folder_cancel_keeps_none_semantics_and_skips_free(self):
        ole = FakeOle32()
        shell = FakeShell32(None)
        with mock.patch.object(picker, '_shell32', shell), \
                mock.patch.object(picker, '_ole32', ole), \
                mock.patch.object(picker, '_user32', FakeUser32()):
            result = picker.choose_folder('t30 fixture')

        self.assertIsNone(result)
        self.assertEqual(shell.path_calls, [])
        self.assertEqual(ole.init_calls, 1)
        self.assertEqual(ole.uninit_calls, 1)
        self.assertEqual(ole.freed, [])

    def test_choose_folder_path_error_still_frees_exactly_once(self):
        ole = FakeOle32()
        shell = FakeShell32(HIGH_PIDL, path_ok=False)
        with mock.patch.object(picker, '_shell32', shell), \
                mock.patch.object(picker, '_ole32', ole), \
                mock.patch.object(picker, '_user32', FakeUser32()):
            with self.assertRaises(picker.ProductError):
                picker.choose_folder('t30 fixture')

        self.assertEqual(shell.path_calls, [HIGH_PIDL])
        self.assertEqual(ole.init_calls, 1)
        self.assertEqual(ole.uninit_calls, 1)
        self.assertEqual(ole.freed, [HIGH_PIDL])


if __name__ == '__main__':
    unittest.main()
