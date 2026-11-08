"""t49: loading-window, stage and error text use the shared UI language."""
from __future__ import annotations

from collections import deque
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime import paths  # noqa: E402

paths.prepare()

from app import load_job  # noqa: E402
from app.jobs import JobHandle  # noqa: E402
from gui import i18n, loading  # noqa: E402
from runtime.errors import InputError  # noqa: E402

DATA = ROOT.parent / '3d hardness plot raw data'
OUT = Path(__file__).resolve().parent / 'out'
STAGES = ('scan', 'signature', 'cache_check', 'cache_hit', 'parse',
          'field', 'spacing', 'ready', 'result_file')
LOADING_KEYS = (
    'loading.title', 'loading.preparing', 'loading.elapsed', 'loading.hint',
    'message.close_hint', 'message.cannot_load', 'message.error_title',
    'error.data_folder_missing', 'error.select_folder',
    'error.no_analysis_files', 'error.saved_result_missing',
    'error.open_saved_result', 'error.result_adapter_unavailable',
    'error.data_invalid', 'error.compute_failed',
) + tuple(f'stage.{stage}' for stage in STAGES) + tuple(
    f'stage.detail.{stage}' for stage in STAGES if stage != 'ready') + (
        'stage.detail.ready', 'stage.detail.result_ready')


class FakePlotter:
    def __init__(self, *args, **kwargs):
        self.texts = []
        self.title = ''
        self.closed = False

    def set_background(self, *_args, **_kwargs):
        return None

    def add_text(self, text, **kwargs):
        self.texts.append((text, kwargs))

    def render(self):
        return None

    def close(self):
        self.closed = True


class FakeActor:
    def __init__(self):
        self.value = ''

    def SetInput(self, value):
        self.value = value

    def GetInput(self):
        return self.value


class CaptureHandle:
    def __init__(self):
        self.messages = []

    def emit(self, stage, text, fraction=None):
        self.messages.append((stage, text, fraction))

    def check_cancel(self):
        return None

    def cancel(self):
        return None


class LoadingI18nTests(unittest.TestCase):
    def test_keys_complete_in_both_languages(self):
        for language in i18n.LANGUAGES:
            for key in LOADING_KEYS:
                self.assertIn(key, i18n.STRINGS[language], (language, key))

    def test_stage_labels_translate_every_stage_id(self):
        window = object.__new__(loading.LoadingWindow)
        for language in i18n.LANGUAGES:
            window.language = language
            for stage in STAGES:
                label = window._stage_label(stage)
                self.assertNotEqual(label, f'stage.{stage}')
                self.assertTrue(label)
            self.assertNotEqual(window._stage_label('scan'),
                                window._stage_label('field'))

    def test_loading_window_chinese_uses_cjk_font(self):
        font = r'C:\Windows\Fonts\msyh.ttc'
        with mock.patch.object(loading.pv, 'Plotter', FakePlotter), \
                mock.patch.object(i18n, 'find_cjk_font', return_value=font):
            window = loading.LoadingWindow(True, (900, 600),
                                           language='zh-CN')
        try:
            self.assertEqual(window.language, 'zh-CN')
            self.assertEqual(window.font_file, font)
            self.assertIn('载入', window.plotter.title)
            texts = dict((name, text) for text, kwargs in window.plotter.texts
                         for name in (kwargs.get('name'),))
            self.assertEqual(texts['loading_stage'], '准备中')
            self.assertIn('Esc', texts['loading_hint'])
            for _text, kwargs in window.plotter.texts:
                self.assertEqual(kwargs.get('font_file'), font)
        finally:
            window.plotter.close()

    def test_detail_anchor_clears_four_lines_and_progress(self):
        # Four 9 px lines need roughly .112 of a 600 px window above the
        # anchor; .62 leaves clear rows below the stage (.78) and above the
        # progress line (.52).
        self.assertLessEqual(loading.LoadingWindow.DETAIL_Y,
                             0.78 - 4 * 0.028)
        self.assertGreaterEqual(loading.LoadingWindow.DETAIL_Y,
                                0.52 + 0.028)

    def test_detail_path_middle_ellipsis_keeps_drive_and_tail(self):
        window = object.__new__(loading.LoadingWindow)
        window.plotter = type('P', (), {'window_size': (900, 600)})()
        text = (r'scanning C:\Users\example\Desktop\3d hardness plot '
                'raw data')
        fitted = window._fit_detail_line(text)
        self.assertLessEqual(len(fitted), len(text))
        self.assertTrue(fitted.startswith('scanning C:\\'))
        self.assertIn('...', fitted)
        self.assertTrue(fitted.endswith('3d hardness plot raw data'))
        self.assertNotEqual(fitted, text)

    def test_drain_applies_ellipsis_to_live_job_event(self):
        window = object.__new__(loading.LoadingWindow)
        window.language = 'en'
        window.plotter = SimpleNamespace(window_size=(900, 600))
        window._details = deque(maxlen=4)
        window._last_stage = ''
        window.stage_actor = FakeActor()
        window.detail_actor = FakeActor()
        window._handle = JobHandle('test', {})
        full = (r'scanning C:\Users\example\Desktop\3d hardness plot '
                'raw data')
        window._handle.emit('scan', full, 0.04)
        event = window._handle.events.queue[0]
        self.assertEqual(event.text, full)
        window._drain()
        shown = window.detail_actor.GetInput()
        self.assertIn('...', shown)
        self.assertTrue(shown.endswith('3d hardness plot raw data'))
        self.assertEqual(window._details[0], full)

    def test_language_switch_uses_one_shared_settings_file(self):
        OUT.mkdir(parents=True, exist_ok=True)
        settings_path = OUT / 't49_settings.json'
        if settings_path.exists():
            settings_path.unlink()
        settings = i18n.LanguageSettings(settings_path)
        settings.save('en')
        self.assertEqual(i18n.preferred_language(settings_path), 'en')
        with mock.patch.object(loading.pv, 'Plotter', FakePlotter):
            window = loading.LoadingWindow(True, (900, 600),
                                           settings_path=settings_path)
        try:
            self.assertEqual(window.language, 'en')
            self.assertIn('loading', window.plotter.title.lower())
        finally:
            window.plotter.close()
            settings_path.unlink(missing_ok=True)

    def test_missing_cjk_font_falls_back_to_english(self):
        with mock.patch.object(loading.pv, 'Plotter', FakePlotter), \
                mock.patch.object(i18n, 'find_cjk_font', return_value=None):
            window = loading.LoadingWindow(True, (900, 600),
                                           language='zh-CN')
        try:
            self.assertEqual(window.language, 'en')
            self.assertIsNone(window.font_file)
            texts = dict((name, text) for text, kwargs in window.plotter.texts
                         for name in (kwargs.get('name'),))
            self.assertEqual(texts['loading_stage'], 'preparing')
            self.assertIn('background compute', texts['loading_hint'])
        finally:
            window.plotter.close()

    def test_show_message_localizes_title_and_hint(self):
        font = r'C:\Windows\Fonts\msyh.ttc'
        with mock.patch.object(loading.pv, 'Plotter', FakePlotter), \
                mock.patch.object(i18n, 'find_cjk_font', return_value=font):
            loading.show_message(
                True, (900, 600), 'TriboScan3D - cannot load',
                ['body'], language='zh-CN')
        # show_message closes its plotter; build a second one to inspect text.
        with mock.patch.object(loading.pv, 'Plotter', FakePlotter), \
                mock.patch.object(i18n, 'find_cjk_font', return_value=font):
            plotter = FakePlotter()
            with mock.patch.object(loading, 'pv') as fake_pv:
                fake_pv.Plotter.return_value = plotter
                loading.show_message(
                    True, (900, 600), 'TriboScan3D - cannot load',
                    ['body'], language='zh-CN')
        texts = [text for text, _kwargs in plotter.texts]
        self.assertIn('TriboScan3D - 无法载入', texts)
        self.assertIn('关闭此窗口以继续。', texts)
        for _text, kwargs in plotter.texts:
            self.assertEqual(kwargs.get('font_file'), font)

    def test_load_stages_and_errors_are_localized(self):
        OUT.mkdir(parents=True, exist_ok=True)
        cache_dir = OUT / 't49_cache'
        cache_dir.mkdir(exist_ok=True)
        handle = CaptureHandle()
        result = load_job.load_directory(
            handle, directory=str(DATA), cache_dir=cache_dir,
            language='zh-CN')
        self.assertGreater(result.dataset.point_count, 0)
        seen = [stage for stage, _text, _fraction in handle.messages]
        for stage in ('scan', 'signature', 'cache_check', 'parse',
                      'field', 'spacing', 'ready'):
            self.assertIn(stage, seen)
        texts = {stage: text for stage, text, _fraction in handle.messages}
        self.assertIn('正在扫描', texts['scan'])
        self.assertIn('正在读取', texts['parse'])
        self.assertIn('Delaunay3D', texts['field'])
        self.assertIn('就绪', texts['ready'])

        missing = OUT / 't49_missing_folder'
        with self.assertRaises(InputError) as cm:
            load_job.scan_analysis_files(missing, language='zh-CN')
        self.assertIn('数据文件夹不存在', str(cm.exception))
        with self.assertRaises(InputError) as cm:
            load_job.scan_analysis_files(missing, language='en')
        self.assertIn('does not exist', str(cm.exception))


if __name__ == '__main__':
    unittest.main()
