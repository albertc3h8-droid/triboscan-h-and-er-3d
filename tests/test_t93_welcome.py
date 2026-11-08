"""t93 regression: the startup empty workspace (P9).

The old startup forced a raw-folder dialog: cancelling it exited the program,
so a user who had an exported .tsr3/CSV had no way in.  The empty workspace
must paint without a Dataset/FieldGrid, expose both open actions, return the
chosen action to Application, and leave a cancelled picker on the same empty
screen.
"""
from __future__ import annotations

import unittest
from unittest import mock

from runtime.paths import prepare

PATHS = prepare()

from app import app as app_module  # noqa: E402
from gui import i18n  # noqa: E402
from gui.welcome import WelcomeWindow  # noqa: E402


class WelcomeWindowTests(unittest.TestCase):

    def setUp(self):
        self.window = WelcomeWindow(
            off_screen=True, window_size=(1536, 992), language='zh-CN')

    def tearDown(self):
        self.window.close()

    def test_01_paints_an_empty_workspace_with_both_open_actions(self):
        image = self.window.image
        self.assertIsNotNone(image)
        self.assertEqual(image.size, (1536, 992))
        buttons = self.window.buttons
        for name in ('open_data', 'open_result', 'card.open_data',
                     'card.open_result', 'lang.zh', 'lang.en'):
            self.assertIn(name, buttons)
            rect = buttons[name]
            self.assertLess(rect[0], rect[2])
            self.assertLess(rect[1], rect[3])
            self.assertGreaterEqual(rect[0], -1)
            self.assertGreaterEqual(rect[1], -1)
            self.assertLessEqual(rect[2], 1536 + 1)
            self.assertLessEqual(rect[3], 992 + 1)
        self.assertEqual(self.window.labels['open_data'], '打开数据文件夹')
        self.assertEqual(self.window.labels['open_result'], '打开已导出结果')
        self.assertEqual(self.window.pending_action, None)

    def test_02_top_and_card_buttons_are_hit_testable(self):
        for name in ('open_data', 'open_result', 'card.open_data',
                     'card.open_result'):
            rect = self.window.buttons[name]
            hit = self.window._hit((rect[0] + rect[2]) / 2.0,
                                   (rect[1] + rect[3]) / 2.0)
            self.assertEqual(hit, name)
        self.assertIsNone(self.window._hit(10.0, 10.0))

    def test_03_callback_action_closes_with_the_right_pending_action(self):
        seen = []

        def open_data(window):
            seen.append('data')
            return (app_module.ACTION_OPEN_DIR, 'C:/data')

        self.window.set_callbacks(open_data=open_data)
        self.window._activate('card.open_data')
        self.assertEqual(seen, ['data'])
        self.assertEqual(self.window.pending_action,
                         (app_module.ACTION_OPEN_DIR, 'C:/data'))

    def test_04_language_toggle_repaints_and_updates_labels(self):
        self.assertEqual(self.window.labels['open_data'], '打开数据文件夹')
        self.window.set_language('en')
        self.assertEqual(self.window.language, 'en')
        self.assertEqual(self.window.labels['open_data'], 'Open data folder')
        self.assertEqual(self.window.labels['open_result'],
                         'Open exported result')
        self.assertIn('no data', self.window.plotter.title)
        self.window.set_language('zh-CN')
        self.assertEqual(self.window.labels['open_data'], '打开数据文件夹')

    def test_05_offscreen_show_without_pending_returns_none(self):
        self.assertIsNone(self.window.show())

    def test_06_resize_updates_the_image_data_dimensions(self):
        """A resize must update vtkImageData dimensions, not just its scalars.

        Otherwise VTK reinterprets the new bitmap with the old width/height and
        the window shows tiled/stretched copies (the P9 resize bug).
        """
        self.window.plotter.render_window.SetSize(1920, 1044)
        self.window.renderer.SetViewport(0.0, 0.0, 1.0, 1.0)
        self.window._paint(render=True)
        self.assertEqual(tuple(self.window._image_data.GetDimensions())[:2],
                         (1920, 1044))
        self.assertEqual(self.window.image.size, (1920, 1044))


class ApplicationWelcomeTests(unittest.TestCase):

    def setUp(self):
        self.app = app_module.Application(PATHS, app_module.Options(
            off_screen=True))

    def tearDown(self):
        self.app.manager.shutdown()

    def test_01_default_start_is_the_empty_workspace(self):
        self.assertEqual(self.app.action,
                         (app_module.ACTION_WELCOME, None))

    def test_02_closing_the_welcome_window_exits_zero(self):
        self.app._show_welcome = mock.Mock(return_value=None)
        self.assertEqual(self.app.run(), 0)

    def test_03_cancelled_load_returns_to_the_empty_workspace(self):
        self.app._show_welcome = mock.Mock(side_effect=[
            (app_module.ACTION_OPEN_DIR, 'C:/data'), None])
        self.app._load_directory = mock.Mock(return_value=None)
        self.assertEqual(self.app.run(), 0)
        self.assertEqual(self.app._show_welcome.call_count, 2)

    def test_04_chosen_result_reaches_load_and_viewer(self):
        result = mock.Mock()
        result.source.kind = 'result_file'
        result.cache_hit = True
        self.app._show_welcome = mock.Mock(
            return_value=(app_module.ACTION_OPEN_RESULT, 'C:/x.tsr3'))
        self.app._load_result = mock.Mock(return_value=(result, None))
        self.app._show_viewer = mock.Mock(return_value=None)
        self.assertEqual(self.app.run(), 0)
        self.app._load_result.assert_called_once_with('C:/x.tsr3')
        self.app._show_viewer.assert_called_once_with(result)


class WelcomePickerTests(unittest.TestCase):

    def setUp(self):
        self.app = app_module.Application(PATHS, app_module.Options(
            off_screen=True))
        self.window = WelcomeWindow(
            off_screen=True, window_size=(640, 480), language='en')

    def tearDown(self):
        self.window.close()
        self.app.manager.shutdown()

    def test_01_open_data_returns_the_folder_and_remembers_it(self):
        with mock.patch('app.app.picker.choose_folder',
                        return_value='C:/数据 目录') as choose:
            action = self.app._welcome_open_data(self.window)
        self.assertEqual(action,
                         (app_module.ACTION_OPEN_DIR, 'C:/数据 目录'))
        self.assertEqual(self.app.last_dir, 'C:/数据 目录')
        choose.assert_called_once()

    def test_02_cancelled_folder_picker_stays_on_the_empty_screen(self):
        with mock.patch('app.app.picker.choose_folder', return_value=''):
            self.assertIsNone(self.app._welcome_open_data(self.window))

    def test_03_open_result_returns_the_file(self):
        with mock.patch('app.app.picker.choose_open_file',
                        return_value='C:/x.tsr3'):
            action = self.app._welcome_open_result(self.window)
        self.assertEqual(action,
                         (app_module.ACTION_OPEN_RESULT, 'C:/x.tsr3'))

    def test_04_cancelled_result_picker_stays_on_the_empty_screen(self):
        with mock.patch('app.app.picker.choose_open_file', return_value=''):
            self.assertIsNone(self.app._welcome_open_result(self.window))


if __name__ == '__main__':
    unittest.main()
