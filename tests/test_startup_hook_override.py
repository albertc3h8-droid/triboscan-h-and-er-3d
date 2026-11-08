"""t37: verify the local Matplotlib startup-hook override and spec binding."""
from __future__ import annotations

import ast
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

APP = Path(__file__).resolve().parents[1]
HOOK_DIR = APP / 'build' / 'hooks_override'
HOOK_FILE = HOOK_DIR / 'rthooks' / 'pyi_rth_mplconfig_local.py'
SPEC_FILE = APP / 'build' / 'triboscan.spec'


class StartupHookOverrideTests(unittest.TestCase):

    def setUp(self):
        self._old_exe = sys.executable
        self._old_cfg = os.environ.get('MPLCONFIGDIR')

    def tearDown(self):
        sys.executable = self._old_exe
        if self._old_cfg is None:
            os.environ.pop('MPLCONFIGDIR', None)
        else:
            os.environ['MPLCONFIGDIR'] = self._old_cfg

    def test_rthooks_dat_maps_matplotlib_to_local_hook(self):
        data = ast.literal_eval((HOOK_DIR / 'rthooks.dat').read_text(encoding='utf-8'))
        self.assertEqual(set(data), {'matplotlib'})
        self.assertEqual(data['matplotlib'], ['pyi_rth_mplconfig_local.py'])
        self.assertTrue(HOOK_FILE.is_file())

    def test_hook_source_has_no_system_temp_and_sets_cache_dir(self):
        text = HOOK_FILE.read_text(encoding='utf-8')
        self.assertNotIn('tempfile', text)
        self.assertNotIn('_pyi_rth_utils', text)
        self.assertIn('MPLCONFIGDIR', text)
        self.assertIn('cache', text)

    def test_spec_binds_hookspath_to_hooks_override(self):
        text = SPEC_FILE.read_text(encoding='utf-8')
        tree = ast.parse(text)
        hookspath_values = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, 'id', None) == 'Analysis':
                for kw in node.keywords:
                    if kw.arg == 'hookspath':
                        hookspath_values.append(kw.value)
        self.assertEqual(len(hookspath_values), 1)
        value = hookspath_values[0]
        self.assertIsInstance(value, ast.List)
        self.assertTrue(value.elts, 'hookspath must not be empty')
        segment = ast.get_source_segment(text, value) or ''
        self.assertIn('hooks_override', segment)

    def test_hook_exec_sets_mplconfigdir_without_filesystem_side_effects(self):
        sys.executable = r'C:\fake\TriboScan3D.exe'
        expected = os.path.join(r'C:\fake', 'cache', 'mpl')
        calls = []

        def record_makedirs(path, exist_ok=False):
            calls.append((path, exist_ok))

        namespace = {'__name__': 't37_hook_test', '__file__': str(HOOK_FILE)}
        with mock.patch.object(os, 'makedirs', side_effect=record_makedirs):
            exec(compile(HOOK_FILE.read_text(encoding='utf-8'),
                         str(HOOK_FILE), 'exec'), namespace)
        self.assertEqual(os.environ.get('MPLCONFIGDIR'), expected)
        self.assertEqual(calls, [(expected, True)])


if __name__ == '__main__':
    unittest.main()
