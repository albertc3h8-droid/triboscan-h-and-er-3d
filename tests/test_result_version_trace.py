"""t22 regression: app/VTK version traceability in user saves and auto cache.

Covers the product path (``integration.result_adapter.to_bundle`` -> both
``save_file`` and ``save``), the frozen-package metadata fallback, backward
compatibility with older files whose version fields are empty, and preservation
of the field/mask when such a file is reopened without rebuilding.

Run from ``triboscan_build/desktop_app`` with either interpreter::

    .buildenv\\\\Scripts\\\\python.exe -m unittest discover -s tests -t tests -p "test_*.py" -v
"""
from __future__ import annotations

import ast
import importlib.metadata as importlib_metadata
import json
import shutil
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from runtime.paths import prepare

PATHS = prepare()

import numpy as np  # noqa: E402

from app import load_job  # noqa: E402
from app import app as application_module  # noqa: E402
from integration import result_adapter  # noqa: E402
from integration.protocols import APP_VERSION  # noqa: E402
from result_store import format as rs_format  # noqa: E402
from runtime import field3d  # noqa: E402

DATA = (Path(__file__).resolve().parents[2] / '3d hardness plot raw data').resolve()
OUT = (Path(__file__).resolve().parent / 'out' / 't22').resolve()
CACHE = OUT / 'cache'
MANIFEST = 'manifest.json'


class FakeHandle:
    def __init__(self) -> None:
        self.generation = 1
        self.events: list[tuple] = []
        self.cancelled = False

    def emit(self, stage, text, fraction=None, **detail):
        self.events.append((stage, text, fraction))

    def check_cancel(self):
        return None

    def cancel(self):
        self.cancelled = True


def _manifest(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        return json.loads(archive.read(MANIFEST).decode('utf-8'))


def _rewrite_manifest(source: Path, target: Path, mutate) -> None:
    """Copy a valid container and change only its JSON manifest."""
    with zipfile.ZipFile(source, 'r') as src, zipfile.ZipFile(target, 'w') as dst:
        for info in src.infolist():
            payload = src.read(info.filename)
            if info.filename == MANIFEST:
                manifest = json.loads(payload.decode('utf-8'))
                mutate(manifest)
                payload = json.dumps(manifest, ensure_ascii=False,
                                     sort_keys=True, indent=2).encode('utf-8')
            dst.writestr(info, payload)


def _runtime_vtk_version() -> str:
    from vtkmodules.vtkCommonCore import vtkVersion
    return str(vtkVersion.GetVTKVersion())


class ResultVersionTraceTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not DATA.is_dir():
            raise unittest.SkipTest(f'raw data folder missing: {DATA}')
        shutil.rmtree(OUT, ignore_errors=True)
        OUT.mkdir(parents=True, exist_ok=True)
        CACHE.mkdir(parents=True, exist_ok=True)
        cls.result = load_job.load_directory(
            FakeHandle(), directory=str(DATA), cache_dir=OUT / 'initial_cache',
            use_cache=False)
        cls.bundle = result_adapter.to_bundle(cls.result)

    def test_01_bundle_carries_real_app_version(self):
        self.assertEqual(self.bundle.app_version, APP_VERSION)
        self.assertEqual(APP_VERSION, '1.0.0')
        self.assertNotEqual(self.bundle.app_version, '')

    def test_02_user_save_and_auto_cache_write_both_versions(self):
        saved = result_adapter.save_file(self.bundle, OUT / 'user_save.tsr3')
        manifest = _manifest(saved)
        self.assertEqual(manifest['app_version'], APP_VERSION)
        self.assertEqual(manifest['app_env']['vtk'], _runtime_vtk_version())
        self.assertTrue(manifest['app_env']['vtk'])

        cache_result = result_adapter.save(CACHE, self.bundle, 't22 auto cache')
        self.assertTrue(cache_result.ok, cache_result.message)
        cache_file = Path(cache_result.path)
        self.assertTrue(cache_file.is_file())
        cache_manifest = _manifest(cache_file)
        self.assertEqual(cache_manifest['app_version'], APP_VERSION)
        self.assertEqual(cache_manifest['app_env']['vtk'], _runtime_vtk_version())

        # The auto-cache path really feeds load_directory and avoids build_field.
        with mock.patch.object(field3d, 'build_field',
                               side_effect=AssertionError('build_field called')):
            again = load_job.load_directory(
                FakeHandle(), directory=str(DATA), cache_dir=CACHE)
        self.assertTrue(again.cache_hit)
        self.assertEqual(again.build_count, 0)
        self.assertEqual(again.dataset.point_count,
                         self.result.dataset.point_count)

    def test_03_metadata_missing_falls_back_to_linked_runtime(self):
        # Metadata present: it wins and the fallback is not touched.
        with mock.patch.object(rs_format, '_vtk_runtime_version',
                               side_effect=AssertionError('fallback used')):
            self.assertEqual(rs_format._package_version('vtk'),
                             importlib_metadata.version('vtk'))
        # Metadata absent (the frozen-package case): read the linked VTK.
        with mock.patch.object(rs_format, '_vtk_runtime_version',
                               wraps=rs_format._vtk_runtime_version) as fallback:
            real_version = rs_format.importlib_metadata.version

            def fake_version(dist):
                if dist == 'vtk':
                    raise importlib_metadata.PackageNotFoundError(dist)
                return real_version(dist)

            with mock.patch.object(rs_format.importlib_metadata, 'version',
                                   side_effect=fake_version):
                self.assertEqual(rs_format._package_version('vtk'),
                                 _runtime_vtk_version())
                fallback_saved = result_adapter.save(
                    CACHE, self.bundle, 't22 fallback')
            self.assertGreaterEqual(fallback.call_count, 1)

        fallback_manifest = _manifest(Path(fallback_saved.path))
        self.assertTrue(fallback_manifest['app_env']['vtk'])
        self.assertEqual(fallback_manifest['app_env']['vtk'],
                         _runtime_vtk_version())

    def test_04_old_empty_fields_and_missing_source_reopen_without_rebuild(self):
        original = result_adapter.save_file(self.bundle, OUT / 'current.tsr3')
        old = OUT / 'old_empty.tsr3'
        missing = OUT / 'old_missing_source.tsr3'

        def clear_versions(manifest):
            manifest['app_version'] = ''
            manifest['app_env']['vtk'] = ''

        def clear_versions_and_source(manifest):
            clear_versions(manifest)
            manifest['source']['directory_hint'] = str(OUT / 'source_that_is_gone')

        _rewrite_manifest(original, old, clear_versions)
        _rewrite_manifest(original, missing, clear_versions_and_source)

        for path, expect_verified in ((old, True), (missing, False)):
            with mock.patch.object(field3d, 'build_field',
                                   side_effect=AssertionError('build_field called')):
                reopened = load_job.load_result_file(FakeHandle(), path=str(path))
            self.assertEqual(reopened.build_count, 0)
            self.assertTrue(reopened.cache_hit)
            self.assertEqual(reopened.source.source_verified, expect_verified)
            np.testing.assert_allclose(reopened.field.er_gpa,
                                       self.result.field.er_gpa,
                                       rtol=0, atol=0, equal_nan=True)
            np.testing.assert_allclose(reopened.field.h_gpa,
                                       self.result.field.h_gpa,
                                       rtol=0, atol=0, equal_nan=True)
            np.testing.assert_array_equal(reopened.field.valid,
                                          self.result.field.valid)
            self.assertEqual(reopened.dataset.point_count,
                             self.result.dataset.point_count)
            # The old file remains readable; its traceability fields stay empty
            # (no invented version is written back during load).
            self.assertEqual(_manifest(path)['app_version'], '')
            self.assertEqual(_manifest(path)['app_env']['vtk'], '')

    def test_06_application_save_writes_versions_to_chosen_and_cache(self):
        """Drive the real ``Application._save`` path, not only the adapter."""
        from app.app import Application, Options
        from runtime import paths as paths_module

        target = OUT / 'app_chosen.tsr3'
        app_paths = paths_module.Paths(
            root=OUT, cache=OUT / 'app_cache', results=OUT / 'app_results',
            logs=OUT / 'app_logs', config=OUT / 'app_config',
            mpl_cache=OUT / 'app_mpl')

        class FakeViewer:
            _save_available = True
            _save_done = False
            notice = ''

            def set_notice(self, text):
                self.notice = text

        application = Application(app_paths, Options())
        fake_viewer = FakeViewer()
        try:
            with mock.patch.object(application_module.picker,
                                   'choose_save_file',
                                   return_value=str(target)):
                application._save(fake_viewer, self.result)
        finally:
            application.manager.shutdown()

        self.assertTrue(target.is_file())
        self.assertTrue(fake_viewer._save_done)
        self.assertIn('saved result', fake_viewer.notice)
        chosen_manifest = _manifest(target)
        self.assertEqual(chosen_manifest['app_version'], APP_VERSION)
        self.assertEqual(chosen_manifest['app_env']['vtk'],
                         _runtime_vtk_version())

        cache_files = sorted((OUT / 'app_cache').glob('field_*.tsr3'))
        self.assertTrue(cache_files)
        cache_manifest = _manifest(cache_files[-1])
        self.assertEqual(cache_manifest['app_version'], APP_VERSION)
        self.assertEqual(cache_manifest['app_env']['vtk'],
                         _runtime_vtk_version())

    def test_05_no_vtk_import_at_result_store_module_import(self):
        source = Path(rs_format.__file__).read_text(encoding='utf-8')
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name.startswith('vtk'),
                                     f'top-level VTK import: {alias.name}')
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ''
                self.assertFalse(module.startswith('vtk'),
                                 f'top-level VTK import: {module}')
        self.assertIn('_vtk_runtime_version', source)
        self.assertIn('from vtkmodules.vtkCommonCore import vtkVersion', source)


if __name__ == '__main__':
    unittest.main()
