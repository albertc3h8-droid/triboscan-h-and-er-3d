"""Widget model, hit testing and small input buffers for the v2 skin.

The skin is a bitmap, so every interactive control is described here as a
rectangle plus an action tuple.  The viewer maps clicks to actions; the painter
reads the same widgets for hover/pressed/checked styling, which keeps what is
drawn and what is clickable in one place.

No VTK, no Pillow: this module is unit-testable on its own.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from gui.ui_layout import Layout, Rect


@dataclass
class Widget:
    """One interactive region of the skin."""

    id: str
    kind: str                      # button | icon | segment | radio |
                                   # checkbox | slider | value | path | browse |
                                   # card
    rect: Rect                     # clickable area (label included)
    action: tuple = ()
    label_key: str = ''
    label: str = ''
    group: str | None = None       # radio/segment group name
    value: str | None = None       # which group value this widget selects
    checked: bool = False
    hovered: bool = False
    pressed: bool = False
    focused: bool = False
    disabled: bool = False
    icon: str = ''                 # theme.ICONS key
    value_float: float | None = None
    value_range: tuple[float, float] | None = None
    text: str = ''                 # editable buffer (value boxes)
    secondary: str = ''            # secondary line (export cards)
    box: Rect | None = None        # drawn tick box when narrower than rect

    def hit(self, x: float, y: float) -> bool:
        return not self.disabled and self.rect.contains(x, y)

    def normalized_box(self, layout: Layout):
        return self.rect.normalized(layout.width, layout.height)

    def state(self) -> str:
        if self.disabled:
            return 'disabled'
        if self.pressed:
            return 'pressed'
        if self.hovered:
            return 'hover'
        return 'idle'


class WidgetSet:
    """Ordered widget collection with hover, focus and text-edit state."""

    def __init__(self, widgets: list[Widget]):
        self.widgets = list(widgets)
        self._by_id = {widget.id: widget for widget in self.widgets}
        self.focus_id: str | None = None
        self._buffer = ''

    # ------------------------------------------------------------- accessors
    def __iter__(self):
        return iter(self.widgets)

    def __len__(self) -> int:
        return len(self.widgets)

    def __getitem__(self, widget_id: str) -> Widget:
        return self._by_id[widget_id]

    def get(self, widget_id: str) -> Widget | None:
        return self._by_id.get(widget_id)

    def items(self):
        return self._by_id.items()

    # ----------------------------------------------------------- hit testing
    def hit(self, x: float, y: float) -> Widget | None:
        """Topmost enabled widget under a top-left pixel coordinate."""
        for widget in reversed(self.widgets):
            if widget.hit(x, y):
                return widget
        return None

    def hover(self, x: float | None, y: float | None) -> bool:
        """Update hover flags; returns True when the hovered widget changed."""
        target = self.hit(x, y) if x is not None and y is not None else None
        changed = False
        for widget in self.widgets:
            flag = widget is target
            if widget.hovered != flag:
                widget.hovered = flag
                changed = True
        return changed

    def clear_hover(self) -> bool:
        return self.hover(None, None)

    def clear_pressed(self) -> None:
        for widget in self.widgets:
            widget.pressed = False

    # ----------------------------------------------------------- group state
    def sync_group(self, group: str, value: str | None) -> None:
        for widget in self.widgets:
            if widget.group == group:
                widget.checked = (widget.value == value)

    def sync_groups(self, groups: dict[str, str | None]) -> None:
        for group, value in groups.items():
            self.sync_group(group, value)

    def set_checked(self, widget_id: str, checked: bool) -> None:
        widget = self._by_id.get(widget_id)
        if widget is not None:
            widget.checked = bool(checked)

    def set_disabled(self, widget_id: str, disabled: bool) -> None:
        widget = self._by_id.get(widget_id)
        if widget is not None:
            widget.disabled = bool(disabled)

    # --------------------------------------------------------- text editing
    def begin_edit(self, widget_id: str) -> None:
        widget = self._by_id.get(widget_id)
        if widget is None or widget.disabled:
            return
        if self.focus_id is not None:
            self.cancel_edit()
        self.focus_id = widget_id
        widget.focused = True
        self._buffer = widget.text or ''

    def cancel_edit(self) -> None:
        if self.focus_id is not None:
            widget = self._by_id.get(self.focus_id)
            if widget is not None:
                widget.focused = False
        self.focus_id = None
        self._buffer = ''

    def type_char(self, char: str) -> None:
        if self.focus_id is None:
            return
        allowed = '0123456789.'
        if char in allowed:
            self._buffer += char
            self._by_id[self.focus_id].text = self._buffer

    def backspace(self) -> None:
        if self.focus_id is None:
            return
        self._buffer = self._buffer[:-1]
        self._by_id[self.focus_id].text = self._buffer

    def buffer(self) -> str:
        return self._buffer

    def apply_buffer(self, text: str) -> None:
        if self.focus_id is None:
            return
        self._buffer = str(text)
        self._by_id[self.focus_id].text = self._buffer

    # -------------------------------------------------------------- snapshot
    def snapshot(self) -> list[dict]:
        """JSON-friendly description used by tests and the scene report."""
        return [{
            'id': widget.id,
            'kind': widget.kind,
            'action': list(widget.action),
            'label': widget.label,
            'group': widget.group,
            'value': widget.value,
            'checked': bool(widget.checked),
            'disabled': bool(widget.disabled),
            'focused': bool(widget.focused),
            'rect': [round(v, 1) for v in widget.rect.as_tuple()],
        } for widget in self.widgets]


def _radio_set(layout: Layout, group: str, values: list[tuple[str, str]],
               rows: list[float], label_x: float) -> list[Widget]:
    widgets = []
    size = layout.radio_size
    offset = layout._d(8)
    for index, (value, key) in enumerate(values):
        y = rows[index]
        rect = Rect(label_x - layout._d(20), y - offset,
                    layout.width, y + size + offset)
        widgets.append(Widget(id=f'{group}.{value}', kind='radio', rect=rect,
                              action=(group, value), label_key=key,
                              group=group, value=value))
    return widgets


def build(layout: Layout) -> WidgetSet:
    """Create the widget set for one layout (all sizes share these ids)."""
    widgets: list[Widget] = []

    # ---- toolbar ---------------------------------------------------------
    for name, key in (('open', 'toolbar.open'),
                      ('load', 'toolbar.open_result')):
        widgets.append(Widget(id=f'toolbar.{name}', kind='button',
                              rect=layout.toolbar_buttons[name],
                              action=('file', name), label_key=key))
    widgets.append(Widget(id='toolbar.help', kind='icon',
                          rect=layout.help_button, action=('help',),
                          label_key='toolbar.help', icon='help'))
    widgets.append(Widget(id='toolbar.lang.zh', kind='segment',
                          rect=layout.lang_segments[0], action=('language', 'zh-CN'),
                          label='中文', group='language', value='zh-CN'))
    widgets.append(Widget(id='toolbar.lang.en', kind='segment',
                          rect=layout.lang_segments[1], action=('language', 'en'),
                          label='English', group='language', value='en'))

    # ---- sidebar radiogroups --------------------------------------------
    widgets.extend(_radio_set(
        layout, 'view',
        [('normal', 'sidebar.normal'), ('locate', 'sidebar.locate'),
         ('raw', 'sidebar.raw')], layout.view_rows, layout.sidebar_radio_label_x))
    widgets.extend(_radio_set(
        layout, 'range',
        [('common', 'sidebar.common'), ('actual', 'sidebar.actual')],
        layout.range_rows, layout.sidebar_radio_label_x))
    widgets.extend(_radio_set(
        layout, 'reference',
        [('auto', 'sidebar.auto'), ('layers', 'sidebar.layers'),
         ('envelope', 'sidebar.envelope')], layout.reference_rows,
        layout.sidebar_radio_label_x))
    widgets.extend(_radio_set(
        layout, 'interp',
        [('linear', 'sidebar.interp.linear'),
         ('cubic', 'sidebar.interp.cubic'),
         ('tps', 'sidebar.interp.tps')], layout.interp_rows,
        layout.sidebar_radio_label_x))

    # ---- header ----------------------------------------------------------
    widgets.append(Widget(id='display.slice', kind='segment',
                          rect=layout.segment_rects[0],
                          action=('display', 'slice'),
                          label_key='header.slice', group='display',
                          value='slice'))
    widgets.append(Widget(id='display.cloud', kind='segment',
                          rect=layout.segment_rects[1],
                          action=('display', 'cloud'),
                          label_key='header.cloud', group='display',
                          value='cloud'))
    # Cloud-only display option: bring the points above the slice back as haze.
    widgets.append(Widget(id='display.fog', kind='checkbox',
                          rect=layout.header_fog,
                          action=('display', 'fog'),
                          label_key='header.fog', box=layout.fog_box))
    widgets.append(Widget(id='header.reset', kind='button',
                          rect=layout.reset_button, action=('reset',),
                          label_key='header.reset', icon='reset'))

    # ---- plot cards (click to link the depth-card thumbnail) -------------
    for metric in ('Er', 'H'):
        widgets.append(Widget(id=f'plot.{metric.lower()}', kind='plot',
                              rect=layout.plot_cards[metric],
                              action=('thumb', metric)))

    # ---- depth card ------------------------------------------------------
    hit = layout.slider_track
    widgets.append(Widget(id='depth.slider', kind='slider',
                          rect=Rect(hit.x0 - layout._d(4), hit.y0 - layout._d(14),
                                    hit.x1 + layout._d(4), hit.y1 + layout._d(14)),
                          action=('depth', 'slide')))
    widgets.append(Widget(id='depth.value', kind='value',
                          rect=layout.slider_value_box,
                          action=('depth', 'edit'), label_key='depth.current'))

    # ---- export cards ----------------------------------------------------
    for index, name in enumerate(('image', 'field', 'raw')):
        widgets.append(Widget(id=f'export.{name}', kind='card',
                              rect=layout.export_card_rect(index),
                              action=('export', name),
                              label_key=f'export.{name}.title',
                              secondary=f'export.{name}.line1',
                              icon=('image', 'table', 'points')[index]))
    widgets.append(Widget(id='export.dir', kind='path',
                          rect=layout.export_path_box,
                          action=('export_dir', 'browse'),
                          label_key='export.dir_title'))
    widgets.append(Widget(id='export.browse', kind='browse',
                          rect=layout.export_browse,
                          action=('export_dir', 'browse'),
                          label_key='export.browse'))
    widgets.append(Widget(id='export.open_after', kind='checkbox',
                          rect=Rect(layout.export_checkbox.x0,
                                    layout.export_checkbox.y0 - layout._d(4),
                                    layout.export_path_box.x1,
                                    layout.export_checkbox.y1 + layout._d(4)),
                          action=('export_dir', 'open_after'),
                          label_key='export.open_after', checked=True,
                          box=layout.export_checkbox))
    return WidgetSet(widgets)
