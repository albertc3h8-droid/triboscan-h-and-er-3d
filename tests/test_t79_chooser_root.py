"""Tests for the t79 export-folder dialog initial location.

The Windows 11 "folder unavailable" dialog is triggered by a non-NULL
``pidlRoot``.  These tests pin the replacement: pidlRoot stays NULL and the
initial folder is selected only from the BFFM_INITIALIZED callback.
"""
from __future__ import annotations

import ctypes
import unittest
from pathlib import Path
from unittest import mock

from gui import picker


class ChooserRootTests(unittest.TestCase):
    def setUp(self):
        self.existing = str(Path(picker.__file__).resolve().parent)
        self.missing = str(Path(__file__).resolve().parent / '__t79_missing__')
        self.file = str(Path(__file__).resolve())

    def _recording_send(self):
        calls = []

        def send(hwnd, message, wparam, lparam):
            calls.append((hwnd, message, wparam, lparam))
            return 0

        return calls, send

    def test_info_has_null_root_and_callback(self):
        info, callback = picker._browse_info(self.existing)
        self.assertIsNone(info.pidlRoot)
        self.assertIsNotNone(callback)
        self.assertNotEqual(info.lpfn, None)

    def test_initialized_sends_selection_once_with_existing_dir(self):
        calls, send = self._recording_send()
        _info, callback = picker._browse_info(self.existing, send=send)
        callback(4321, picker._BFFM_INITIALIZED, 0, 0)
        self.assertEqual(len(calls), 1)
        hwnd, message, wparam, lparam = calls[0]
        self.assertEqual(hwnd, 4321)
        self.assertEqual(message, picker._BFFM_SETSELECTIONW)
        self.assertEqual(wparam, 1)
        self.assertEqual(ctypes.wstring_at(lparam), self.existing)

    def test_non_initialized_messages_do_not_send(self):
        calls, send = self._recording_send()
        _info, callback = picker._browse_info(self.existing, send=send)
        callback(1, 0, 0, 0)
        callback(1, picker._BFFM_SETSELECTIONW, 0, 0)
        self.assertEqual(calls, [])

    def test_missing_initial_does_not_send(self):
        calls, send = self._recording_send()
        _info, callback = picker._browse_info(self.missing, send=send)
        callback(1, picker._BFFM_INITIALIZED, 0, 0)
        self.assertEqual(calls, [])

    def test_file_instead_of_directory_does_not_send(self):
        calls, send = self._recording_send()
        _info, callback = picker._browse_info(self.file, send=send)
        callback(1, picker._BFFM_INITIALIZED, 0, 0)
        self.assertEqual(calls, [])

    def test_default_send_is_the_module_prototype_and_is_monkeypatchable(self):
        calls, send = self._recording_send()
        with mock.patch.object(picker, '_send_message', send):
            _info, callback = picker._browse_info(self.existing)
            callback(7, picker._BFFM_INITIALIZED, 0, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 7)


if __name__ == '__main__':
    unittest.main()
