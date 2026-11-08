"""The only place that reaches the native VTK interactor.

PyVista's ``Plotter.iren`` is a Python *wrapper*
(``pyvista.plotting.render_window_interactor.RenderWindowInteractor``); it
exposes snake_case helpers (``add_observer``, ``create_timer`` ...), not the
VTK CamelCase methods.  The native ``vtkRenderWindowInteractor`` lives at
``plotter.iren.interactor`` and is the object whose ``AddObserver`` /
``CreateRepeatingTimer`` / ``DestroyTimer`` / ``TerminateApp`` we need.

Keeping every such call in this module means the wrapper/native mix cannot
creep back into loading windows, the app loop or the viewer.
"""
from __future__ import annotations


def native_interactor(plotter):
    """Return the native ``vtkRenderWindowInteractor`` or ``None``."""
    iren = getattr(plotter, 'iren', None)
    if iren is None:
        return None
    return getattr(iren, 'interactor', None)


def add_timer(plotter, callback, interval_ms: int):
    """Attach a repeating ``TimerEvent`` observer and start the timer.

    Returns ``(observer_id, timer_id)`` for :func:`remove_timer`.  Raises
    ``RuntimeError`` when the window has no native interactor, because silently
    dropping progress/cancel handling would freeze the loading window.

    The native interactor must be initialised before a timer is created: a
    timer registered on a not-yet-initialised Windows interactor is accepted
    but never fires under PyVista's ``ProcessEvents`` loop, which leaves the
    loading/auto-close window open forever (verified on this machine).  PyVista
    initialises the interactor lazily in ``show``; doing it here is equivalent
    and leaves the later ``show`` call unchanged.
    """
    wrapper = getattr(plotter, 'iren', None)
    if wrapper is not None and not getattr(wrapper, 'initialized', True):
        wrapper.initialize()
    iren = native_interactor(plotter)
    if iren is None:
        raise RuntimeError('the render window has no native interactor')
    observer = int(iren.AddObserver('TimerEvent', callback))
    timer = int(iren.CreateRepeatingTimer(int(interval_ms)))
    return observer, timer


def remove_timer(plotter, observer, timer) -> None:
    """Best-effort destruction of an :func:`add_timer` pair."""
    iren = native_interactor(plotter)
    if iren is None:
        return
    if timer is not None:
        try:
            iren.DestroyTimer(int(timer))
        except Exception:  # noqa: BLE001 - the window may already be gone
            pass
    if observer is not None:
        try:
            iren.RemoveObserver(int(observer))
        except Exception:  # noqa: BLE001
            pass


def terminate(plotter) -> None:
    """Stop the VTK event loop for ``plotter`` (native interactor first)."""
    iren = native_interactor(plotter)
    if iren is not None:
        try:
            iren.TerminateApp()
            return
        except Exception:  # noqa: BLE001 - fall through to plotter.close
            pass
    try:
        plotter.close()
    except Exception:  # noqa: BLE001
        pass
