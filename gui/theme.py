"""UI v2 visual tokens, DPI/window policy and cached Pillow fonts.

The redesign paints the whole window chrome with Pillow into one RGB image that
VTK shows on layer 0; geometry lives in gui.ui_layout in the 1536 x 992 design
frame.  This module owns the palette, type scale, icon glyphs, the font cache
and the window-size/DPI policy so the painter and the layout stay independent
of the viewer.
"""
from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

from PIL import ImageFont

DESIGN_WIDTH = 1536
DESIGN_HEIGHT = 992
DEFAULT_WINDOW = (DESIGN_WIDTH, DESIGN_HEIGHT)
#: Interactive window floor in design pixels.  It is the compact layout
#: (1280 x 720), not the full design frame, so a 125 % 1920 x 1080 desktop can
#: open at 1600 x 900 and still fit its work area.  The shell refuses to shrink
#: a user window below floor x DPI; off-screen renders keep whatever size they
#: ask for.
MIN_WINDOW = (1280, 720)
MAX_WINDOW = (2560, 1440)

#: Design palette.  RGB tuples are shared by Pillow and (as hex) by VTK props.
COLORS = {
    'page_top': (237, 244, 250),
    'page_bottom': (248, 251, 254),
    'card': (255, 255, 255),
    'border': (227, 234, 242),
    'shadow': (226, 235, 245),
    'primary': (7, 101, 250),
    'primary_dark': (5, 84, 210),
    'primary_soft': (238, 244, 255),
    'check': (4, 123, 252),
    'green': (50, 180, 13),
    'red': (225, 29, 72),
    'ink': (23, 42, 74),
    'ink_strong': (12, 26, 66),
    'sub': (84, 100, 128),
    'muted': (122, 138, 159),
    'divider_soft': (222, 232, 243),
    'strip': (243, 247, 252),
    'track': (219, 228, 240),
    'divider': (203, 214, 229),
    'notice': (180, 83, 9),
    'white': (255, 255, 255),
    'axis': (205, 217, 232),
    'bar': (122, 178, 236),
    'bar_soft': (176, 205, 240),
    'toolbar_ink': (23, 42, 74),
    'tagline': (47, 60, 103),
    'card_border_hover': (150, 190, 250),
}

#: Segoe MDL2 Assets glyphs used by the toolbar and the export cards.
ICONS = {
    'folder': '\ue8b7',
    'folder_open': '\ue838',
    'save': '\ue74e',
    'help': '\ue897',
    'image': '\ue8b9',
    'table': '\ue80a',
    'points': '\uea37',
    'reset': '\ue72c',
}

#: Global type-size multiplier on top of the window scale: the design mock's
#: body copy reads a touch larger than the raw 12-13 px tokens at 1536 x 992.
TYPE_BOOST = 1.10

FONT_DIR = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts'
FONT_FILES = {
    'regular': ('msyh.ttc', 'simhei.ttf', 'simsun.ttc'),
    'bold': ('msyhbd.ttc', 'msyh.ttc', 'simhei.ttf'),
    'icons': ('segmdl2.ttf', 'SegoeIcons.ttf'),
}


def color(name: str, alpha: int | None = None):
    """Palette lookup as an RGB(A) tuple; raises for unknown names."""
    base = COLORS[name]
    return base if alpha is None else (*base, alpha)


def hex_color(name: str) -> str:
    """Palette lookup as a #rrggbb string (for VTK/PyVista props)."""
    return '#%02x%02x%02x' % COLORS[name]


def system_ui_scale() -> float:
    """Windows UI scale (96 DPI == 1.0); 1.0 when it cannot be queried."""
    if sys.platform != 'win32':
        return 1.0
    try:
        user32 = ctypes.windll.user32
        try:
            dpi = int(user32.GetDpiForSystem())
        except AttributeError:  # pragma: no cover - pre Windows 10 1607
            gdi32 = ctypes.windll.gdi32
            dpi = int(gdi32.GetDeviceCaps(user32.GetDC(0), 88))
        if dpi > 0:
            return dpi / 96.0
    except Exception:  # noqa: BLE001 - the UI must never fail over a hint
        pass
    return 1.0


def native_handle(window) -> int:
    """Native HWND of a VTK window, 0 when it has none yet.

    VTK's Python bindings render pointer returns as C-style reprs such as
    '_00000000028e08fe_p_void', so int() on the raw value always fails and the
    hex digits have to be parsed out.
    """
    if window is None:
        return 0
    try:
        raw = window.GetGenericWindowId()
    except Exception:  # noqa: BLE001 - no such method / no window yet
        return 0
    if isinstance(raw, int):
        return raw
    text = str(raw or '')
    if '_p_void' in text:
        text = text.split('_p_void', 1)[0]
    try:
        return int(text.strip('_'), 16)
    except ValueError:
        return 0


def dpi_scale_for_window(window) -> float:
    """Per-monitor scale of a live VTK window, 1.0 off-screen or on failure."""
    handle = native_handle(window)
    if not handle or sys.platform != 'win32':
        return system_ui_scale()
    try:
        dpi = int(ctypes.windll.user32.GetDpiForWindow(ctypes.c_void_p(handle)))
        if dpi > 0:
            return dpi / 96.0
    except Exception:  # noqa: BLE001
        pass
    return system_ui_scale()


def enable_dpi_awareness() -> bool:
    """Let the process render at device pixels on scaled displays.

    A DPI-unaware process is bitmap-stretched by the shell at 125 %-200 %, so
    every Pillow-painted label turns soft no matter how large the window is.
    Call once, before the first window is created; repeated calls are safe.
    """
    if sys.platform != 'win32':
        return False
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return True
    except Exception:  # noqa: BLE001 - older Windows has no context API
        pass
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return True
    except Exception:  # noqa: BLE001 - shcore is Windows 8.1+
        pass
    try:
        return bool(ctypes.windll.user32.SetProcessDPIAware())
    except Exception:  # noqa: BLE001
        return False


def work_area() -> tuple[int, int]:
    """Primary monitor work area in device pixels, or the design frame."""
    if sys.platform == 'win32':
        try:
            import ctypes.wintypes as wintypes
            rect = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(
                    0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
                width = int(rect.right - rect.left)
                height = int(rect.bottom - rect.top)
                if width > 0 and height > 0:
                    return width, height
        except Exception:  # noqa: BLE001 - the UI must never fail over a hint
            pass
    return DESIGN_WIDTH, DESIGN_HEIGHT


def window_floor(scale: float = 1.0) -> tuple[int, int]:
    """Minimum interactive client size in device pixels (work area capped).

    The floor is the compact layout, 1280 x 720 design pixels, grown by the
    DPI scale (1600 x 900 at 125 %, 1920 x 1080 at 150 %) and capped to the
    monitor work area.
    """
    area = work_area()
    scale = max(float(scale), 0.1)
    return (min(int(round(MIN_WINDOW[0] * scale)), int(area[0])),
            min(int(round(MIN_WINDOW[1] * scale)), int(area[1])))


def clamp_window_size(size, scale: float = 1.0,
                      minimum: bool = True,
                      maximum: bool = True) -> tuple[int, int]:
    """Clamp a requested client size into the supported window envelope.

    Both limits are design-frame values multiplied by the DPI scale.  The
    compact floor is 1280 x 720, so a 150 % display opens at 1920 x 1080
    device pixels.  The floor never exceeds the monitor work area.  Off-screen
    renders pass minimum/maximum=False and keep any requested size, so the
    1536 x 992 design frame stays reproducible.
    """
    width, height = (int(size[0]), int(size[1]))
    scale = max(float(scale), 0.1)
    ceiling = ((int(round(MAX_WINDOW[0] * scale)),
                int(round(MAX_WINDOW[1] * scale))) if maximum
               else (10 ** 6, 10 ** 6))
    floor = window_floor(scale) if minimum else (1, 1)
    width = min(max(width, floor[0]), max(ceiling[0], floor[0]))
    height = min(max(height, floor[1]), max(ceiling[1], floor[1]))
    return width, height


def default_window_size(scale: float = 1.0) -> tuple[int, int]:
    """The 1536 x 992 design frame grown by DPI, capped to the work area.

    Capping matters at 125 % on a 1920 x 1080 desktop: the design frame grows
    to 1920 x 1240 but the usable client height is about 1044, and a window
    taller than that would put the toolbar off-screen (the P8 report).
    """
    size = clamp_window_size(
        (round(DESIGN_WIDTH * scale), round(DESIGN_HEIGHT * scale)), scale)
    area = work_area()
    return (min(size[0], int(area[0])), min(size[1], int(area[1])))


class FontBook:
    """Cached msyh/msyhbd/segmdl2 font objects scaled by the layout factor.

    Base sizes are typographic points in the 1536 x 992 design frame; px()
    converts them with the window's layout factor so text keeps its relative
    size at every window size.  Missing font files fall back to Pillow's
    bundled default so an offscreen test never crashes.
    """

    def __init__(self, font_scale: float = 1.0, font_dir: Path | None = None):
        self.font_scale = max(0.5, min(float(font_scale), 4.0))
        self.font_dir = Path(font_dir) if font_dir else FONT_DIR
        self._files = {name: self._first_existing(candidates)
                       for name, candidates in FONT_FILES.items()}
        self._cache: dict[tuple[int, str], ImageFont.FreeTypeFont] = {}

    def _first_existing(self, candidates) -> Path | None:
        for name in candidates:
            path = self.font_dir / name
            if path.exists():
                return path
        return None

    def px(self, base_size: float) -> int:
        """Pixel size of a design-frame point size at this window scale."""
        return max(8, int(round(float(base_size) * self.font_scale * TYPE_BOOST)))

    def font(self, base_size: float, *, bold: bool = False,
             icons: bool = False):
        size = self.px(base_size)
        key = (size, 'icons' if icons else ('bold' if bold else 'regular'))
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        path = self._files.get(key[1])
        font = None
        if path is not None:
            try:
                font = ImageFont.truetype(str(path), size)
            except OSError:  # pragma: no cover - corrupt font file
                font = None
        if font is None:  # pragma: no cover - only on stripped Windows images
            try:
                font = ImageFont.load_default(size=size)
            except TypeError:
                font = ImageFont.load_default()
        self._cache[key] = font
        return font

    def measure(self, text: str, base_size: float, *, bold: bool = False,
                icons: bool = False) -> float:
        """Advance width of the text in pixels at this window scale."""
        try:
            return float(self.font(base_size, bold=bold, icons=icons)
                         .getlength(text))
        except Exception:  # noqa: BLE001 - very old Pillow fallback
            return float(self.px(base_size)) * 0.6 * len(text)

    def line_height(self, base_size: float, *, bold: bool = False) -> int:
        return max(1, int(round(self.px(base_size) * 1.35)))


if __name__ == '__main__':  # pragma: no cover - manual sanity check
    book = FontBook(1.0)
    print('scale', system_ui_scale(), 'window', default_window_size())
    print('f1', book.font(13).getlength('3D 三维视图'), 'f2', book.px(13))
