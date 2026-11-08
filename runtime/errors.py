"""Product-level error types shared by runtime, app and integration layers.

All errors here are *user-facing*: their text is shown in the GUI and logged.
No error handler silently falls back to another range, field or directory.
"""
from __future__ import annotations


class ProductError(RuntimeError):
    """Base class for errors the portable program can explain to the user."""


class InputError(ProductError):
    """The selected source folder / file is not usable as requested."""


class LayoutError(ProductError):
    """The portable program's own writable folders cannot be prepared."""


class Cancelled(ProductError):
    """The active compute job was cancelled; its result must be discarded."""


class AdapterUnavailable(ProductError):
    """A plug-in point (exports, result cache) is not present in this build."""
