"""t43 regressions: real range-mode crash, bilingual UI, localized errors.

UI v2 paints the chrome as a Pillow bitmap, so the visible state is asserted
through ``_skin_state`` and the widget rectangles instead of the hidden VTK
summary/status/language actors.  The crash test still reproduces the exact
failing path from t35: a result file whose
``common`` range has no usable depth is opened, so the slider is absent and
``current_depth`` is None; switching to ``actual`` used to call
``float(None)`` while creating the slider.  The test constructs that state
deterministically (common invalid, actual valid) without touching the source
data or opening a real window.
"""
from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from runtime.paths import prepare

PATHS = prepare()

from app import load_job  # noqa: E402
from app.app import Application  # noqa: E402
from gui import i18n, picker  # noqa: E402
from gui.viewer import TrialViewer  # noqa: E402
from integration import result_adapter  # noqa: E402
from runtime import loader as dm  # noqa: E402

DATA = (Path(__file__).resolve().parents[2] / '3d hardness plot raw data').resolve()
OUT = (Path(__file__).resolve().parent / 'out' / 't43').resolve()

V2_LABEL_KEYS = (
    'sidebar.normal', 'sidebar.locate', 'sidebar.raw',
    'sidebar.common', 'sidebar.actual', 'sidebar.auto', 'sidebar.layers',
    'sidebar.envelope', 'sidebar.interp.linear', 'sidebar.interp.cubic',
    'sidebar.interp.tps', 'toolbar.open', 'toolbar.open_result',
    'header.slice', 'header.cloud', 'header.fog', 'header.reset',
)


class FakeHandle:
    def emit(self, *args, **kwargs):
        pass

    def check_cancel(self):
        pass


class T43Tests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not DATA.is_dir():
            raise unittest.SkipTest(f'raw data folder missing: {DATA}')
        shutil.rmtree(OUT, ignore_errors=True)
        (OUT / 'cache').mkdir(parents=True, exist_ok=True)
        result = load_job.load_directory(
            FakeHandle(), directory=str(DATA), cache_dir=OUT / 'cache',
            use_cache=False)
        cls.dataset = result.dataset
        cls.field = result.field
        cls.raw_spacing = result.raw_spacing
        cls.timings = result.timings
        cls.actual_range = dm.depth_range(cls.dataset, 'actual')
        cls.invalid_common = dm.DepthRange(
            mode='common', lo_nm=None, hi_nm=None, valid=False,
            note='synthetic invalid common range')

    def _enable_english(self):
        return OUT / 'settings_en.json'

    def _make_viewer(self, *, language='en', settings_path=None, **kwargs):
        if settings_path is None:
            settings_path = self._enable_english()
            settings_path.write_text(json.dumps({'language': 'en'}),
                                     encoding='utf-8')
        return TrialViewer(
            self.dataset, self.field, self.raw_spacing,
            off_screen=True, window_size=(1536, 992),
            timings=self.timings, cache_hit=False,
            language=language, settings_path=settings_path, **kwargs)

    # ------------------------------------------------------- A. crash path
    def test_01_switching_to_actual_from_invalid_common_builds_slider(self):
        def ranges(_dataset, mode='common'):
            return self.invalid_common if mode == 'common' else self.actual_range

        viewer = None
        try:
            with mock.patch.object(dm, 'depth_range', ranges):
                viewer = self._make_viewer()
                self.assertIsNone(viewer.current_depth)
                self.assertIsNone(viewer.slider)
                viewer.set_range_mode('actual')
                self.assertIsNotNone(viewer.current_depth)
                self.assertIsNotNone(viewer.slider)
                self.assertTrue(viewer.current_range.valid)
                self.assertIn('Er', viewer.current_planes)
                self.assertIsNotNone(viewer.slice_actors.get('Er'))
                self.assertIn('heff =', viewer.depth_value_actor.GetInput())
        finally:
            if viewer is not None:
                viewer.plotter.close()

    def test_02_common_invalid_actual_valid_text_states(self):
        def ranges(_dataset, mode='common'):
            return self.invalid_common if mode == 'common' else self.actual_range

        viewer = None
        try:
            with mock.patch.object(dm, 'depth_range', ranges):
                viewer = self._make_viewer()
                state = viewer._skin_state()
                self.assertFalse(state.range_valid)
                self.assertEqual(state.range_mode, 'common')
                self.assertIsNone(viewer.slider)
                viewer.set_range_mode('actual')
                state = viewer._skin_state()
                self.assertTrue(state.range_valid)
                self.assertEqual(state.range_mode, 'actual')
                self.assertTrue(viewer.widgets.get('range.actual').checked)
                self.assertIsNotNone(viewer.slider)
                self.assertIn('heff =', viewer.depth_value_actor.GetInput())
        finally:
            if viewer is not None:
                viewer.plotter.close()

    # -------------------------------------------------------------- B. i18n
    def test_03_toggle_language_updates_every_visible_group(self):
        settings = OUT / 'settings_toggle.json'
        viewer = self._make_viewer(settings_path=settings)
        try:
            if not viewer.font_file:
                self.skipTest('no CJK font installed')
            self.assertEqual(viewer.language, 'en')
            self.assertTrue(viewer.widgets.get('toolbar.lang.en').checked)
            viewer.toggle_language()
            viewer._skin_state()
            self.assertEqual(viewer.language, 'zh-CN')
            self.assertTrue(viewer.widgets.get('toolbar.lang.zh').checked)
            self.assertIn('正常视图', viewer._t('sidebar.normal'))
            self.assertEqual(viewer._t('toolbar.open_result'), '打开结果文件')
            self.assertIn('已计算', viewer._info_line())
            self.assertIn('薄板样条', viewer._t('sidebar.interp.tps'))
            self.assertIn('[实测]', viewer.window_title)
            self.assertTrue(viewer.depth_value_actor.GetInput().startswith('heff = '))
            saved = json.loads(settings.read_text(encoding='utf-8'))
            self.assertEqual(saved['language'], 'zh-CN')
            # Immediate undo, without writing again.
            viewer.set_language('en', persist=False)
            self.assertEqual(viewer._t('sidebar.normal'), 'Normal')
            self.assertIn('computed', viewer._info_line())
        finally:
            viewer.plotter.close()

    def test_04_cjk_font_reaches_the_skin_painter(self):
        viewer = self._make_viewer(language='zh-CN')
        try:
            if not viewer.font_file:
                self.skipTest('no CJK font installed')
            self.assertEqual(viewer.language, 'zh-CN')
            regular = viewer.fonts._files.get('regular')
            self.assertIsNotNone(regular)
            font = viewer.fonts.font(13)
            self.assertEqual(Path(getattr(font, 'path', regular)).resolve(),
                             Path(viewer.font_file).resolve())
            self.assertGreater(font.getbbox('视图')[2], 0)
            self.assertGreater(viewer.fonts.measure('视图操作', 13), 0.0)
        finally:
            viewer.plotter.close()

    def test_05_default_language_follows_settings_and_system_fallback(self):
        settings = OUT / 'settings_remembered.json'
        settings.write_text(json.dumps({'language': 'zh-CN'}), encoding='utf-8')
        viewer = self._make_viewer(language=None, settings_path=settings)
        try:
            if viewer.font_file:
                self.assertEqual(viewer.language, 'zh-CN')
        finally:
            viewer.plotter.close()
        with mock.patch.object(i18n.ctypes.windll.kernel32,
                               'GetUserDefaultUILanguage', return_value=0x0804):
            self.assertEqual(i18n.system_language(), 'zh-CN')
        with mock.patch.object(i18n.ctypes.windll.kernel32,
                               'GetUserDefaultUILanguage', return_value=0x0409):
            self.assertEqual(i18n.system_language(), 'en')

    def test_05b_missing_cjk_font_falls_back_to_english_with_notice(self):
        missing = OUT / 'settings_missing_font.json'
        missing.unlink(missing_ok=True)
        with mock.patch.object(i18n, 'find_cjk_font', return_value=None), \
                mock.patch.object(i18n, 'system_language', return_value='zh-CN'):
            viewer = self._make_viewer(language=None, settings_path=missing)
        try:
            self.assertEqual(viewer.language, 'en')
            self.assertEqual(viewer.font_file, None)
            self.assertIn('font not found', viewer._notice)
            self.assertIn('font not found', viewer._info_line())
        finally:
            viewer.plotter.close()

    def test_05c_export_and_saved_notices_are_localized(self):
        viewer = self._make_viewer(language='zh-CN')
        try:
            if not viewer.font_file:
                self.skipTest('no CJK font installed')
            viewer.set_notice('exported 4 files -> C:/tmp/run_1')
            self.assertIn('已导出 4 个文件', viewer._notice)
            viewer.set_notice('saved result: test.tsr3')
            self.assertIn('已保存结果', viewer._notice)
        finally:
            viewer.plotter.close()

    def test_06_language_dictionary_covers_all_business_labels(self):
        required = V2_LABEL_KEYS + (
            'help.title', 'help.subtitle', 'help.footer', 'help.note',
            'notice.help', 'notice.exported',
        )
        for language, table in i18n.STRINGS.items():
            for key in required:
                self.assertIn(key, table, (language, key))
            for key, value in table.items():
                self.assertIsInstance(value, str, key)
                self.assertTrue(value, key)

    def test_06b_chinese_widgets_stay_inside_the_window(self):
        viewer = self._make_viewer(
            language='zh-CN', settings_path=OUT / 'settings_fit.json')
        try:
            if not viewer.font_file:
                self.skipTest('no CJK font installed')
            width, height = viewer._window_size()
            for widget in viewer.widgets:
                self.assertGreaterEqual(widget.rect.x0, 0.0, widget.id)
                self.assertGreaterEqual(widget.rect.y0, 0.0, widget.id)
                self.assertLessEqual(widget.rect.x1, width, widget.id)
                self.assertLessEqual(widget.rect.y1, height, widget.id)
            for name in ('toolbar.open', 'toolbar.load'):
                widget = viewer.widgets.get(name)
                text = viewer._t(widget.label_key)
                self.assertLessEqual(
                    viewer.fonts.measure(text, 13) + viewer.layout._d(18),
                    widget.rect.width + 1, name)
        finally:
            viewer.plotter.close()

    # --------------------------------------------------- C. save diagnostics
    def test_07_save_failure_reports_target_type_and_suggestion(self):
        target = OUT / 'readonly' / 'result.tsr3'
        viewer = SimpleNamespace(
            language='en', _save_available=True,
            _t=lambda key: i18n.tr('en', key),
            notice='',
            set_notice=lambda text: setattr(viewer, 'notice', text))
        app = SimpleNamespace(
            _slug=Application._slug,
            _fail=lambda code: None,
            exit_code=0,
            paths=SimpleNamespace(results=OUT / 'results',
                                  cache=OUT / 'cache'))
        result = SimpleNamespace(source=SimpleNamespace(display_label='slow_src'))
        error = PermissionError(13, 'Access is denied')
        error.winerror = 5
        with mock.patch.object(picker, 'choose_save_file', return_value=str(target)), \
                mock.patch.object(result_adapter, 'to_bundle', return_value=object()), \
                mock.patch.object(result_adapter, 'save_file', side_effect=error), \
                self.assertLogs('triboscan.app', level='ERROR') as logs:
            Application._save(app, viewer, result)
        self.assertIn(str(target), viewer.notice)
        self.assertIn('PermissionError', viewer.notice)
        self.assertIn('WinError 5', viewer.notice)
        self.assertIn('writable', viewer.notice.lower())
        self.assertIn('results', viewer.notice.lower())
        self.assertIn(f'target={target}', '\n'.join(logs.output))
        self.assertIn('winerror=5', '\n'.join(logs.output))


if __name__ == '__main__':
    unittest.main()
