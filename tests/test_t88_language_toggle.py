"""t88 regression: the language pill keeps the label of its unselected half.

The toolbar language switch is one rounded pill made of two widgets.  The
checked half used to repaint the whole pill (blue + a white rectangle for the
other half) after the other label had already been drawn, so switching to
English erased the already white-on-white "中文" label.  Each half now paints
only itself; these tests render both states and check that the unselected
half still has dark ink pixels and the selected half keeps its white label.
"""
from __future__ import annotations

import unittest

from PIL import Image, ImageDraw

from runtime.paths import prepare

PATHS = prepare()

from gui import theme, ui_chrome, ui_widgets  # noqa: E402
from gui.ui_layout import Layout  # noqa: E402


def _render(language: str):
    layout = Layout(1536, 992)
    fonts = theme.FontBook(layout.font_scale)
    skin = ui_chrome.Skin(layout, fonts, language)
    widgets = ui_widgets.build(layout)
    widgets.sync_groups({"language": language})
    image = Image.new("RGB", (layout.width, layout.height), (244, 248, 252))
    draw = ImageDraw.Draw(image)
    for widget in widgets:
        if widget.id.startswith("toolbar.lang"):
            skin._language_segment(draw, widget, "idle")
    by_value = {widget.value: widget for widget in widgets
                if widget.id.startswith("toolbar.lang")}
    return layout, image, by_value


def _half_box(layout, value: str):
    rect = layout.lang_rect
    split = int(layout.lang_split_x)
    if value == "zh-CN":
        return (int(rect.x0) + 5, int(rect.y0) + 5, split - 5, int(rect.y1) - 5)
    return (split + 5, int(rect.y0) + 5, int(rect.x1) - 5, int(rect.y1) - 5)


class LanguageSegmentTests(unittest.TestCase):

    def test_unselected_half_keeps_dark_label(self):
        for language, other in (("en", "zh-CN"), ("zh-CN", "en")):
            layout, image, _widgets = _render(language)
            crop = image.crop(_half_box(layout, other))
            pixels = crop.load()
            dark = sum(
                1
                for y in range(crop.height) for x in range(crop.width)
                if sum(pixels[x, y]) < 450)
            self.assertGreater(
                dark, 150,
                f"{other} label disappeared while {language} was selected")

    def test_selected_half_keeps_white_label(self):
        for language in ("en", "zh-CN"):
            layout, image, _widgets = _render(language)
            crop = image.crop(_half_box(layout, language))
            pixels = crop.load()
            white = sum(
                1
                for y in range(crop.height) for x in range(crop.width)
                if min(pixels[x, y]) > 235)
            self.assertGreater(
                white, 150,
                f"{language} label is not legible on the selected blue half")

    def test_checked_state_follows_language(self):
        for language in ("en", "zh-CN"):
            _layout, _image, widgets = _render(language)
            self.assertTrue(widgets[language].checked)
            other = "zh-CN" if language == "en" else "en"
            self.assertFalse(widgets[other].checked)


if __name__ == "__main__":
    unittest.main()