"""t8 regression tests: F1 interaction ownership, F2 job terminal states,
F6 RAW readout honesty, F7 reserved interfaces.

No window is opened: the VTK render windows here are off-screen, and the
interactive code paths run against fake interactors so the test is safe on a
machine without a GPU or an interactive desktop.
"""
from __future__ import annotations

import ast
import queue
import shutil
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

from runtime.paths import prepare

PATHS = prepare()

from app import load_job  # noqa: E402
from app.app import Application  # noqa: E402
from app.jobs import JobManager  # noqa: E402
from gui import interaction, loading  # noqa: E402
from gui.viewer import TrialViewer  # noqa: E402
from runtime import field  # noqa: E402
from runtime.errors import Cancelled  # noqa: E402

APP_DIR = Path(__file__).resolve().parents[1]
DATA = (Path(__file__).resolve().parents[2] / '3d hardness plot raw data').resolve()
OUT = (Path(__file__).resolve().parent / 'out' / 't8').resolve()

VTK_CAMEL = {'AddObserver', 'CreateRepeatingTimer', 'DestroyTimer',
             'TerminateApp', 'RemoveObserver'}


class _FakeActor:
    def __init__(self):
        self.text = ''

    def SetInput(self, text):
        self.text = str(text)


class _FakeNative:
    def __init__(self):
        self.observers = {}
        self.timers = []
        self.destroyed = []
        self.removed = []
        self.terminated = False
        self._next_observer = 10
        self._next_timer = 100

    def AddObserver(self, event, callback):
        self._next_observer += 1
        self.observers[self._next_observer] = callback
        return self._next_observer

    def RemoveObserver(self, observer_id):
        self.removed.append(observer_id)

    def CreateRepeatingTimer(self, milliseconds):
        self._next_timer += 1
        self.timers.append(int(milliseconds))
        return self._next_timer

    def DestroyTimer(self, timer_id):
        self.destroyed.append(timer_id)

    def TerminateApp(self):
        self.terminated = True


class _FakeIren:
    def __init__(self):
        self.interactor = _FakeNative()
        self.initialized = False
        self.initialize_calls = 0

    def initialize(self):
        self.initialize_calls += 1
        self.initialized = True


class _FakePlotter:
    def __init__(self, **kwargs):
        self.iren = _FakeIren()
        self.closed = False
        self.events = {}
        self.title = ''
        self.actors = {}
        self.rendered = 0
        self.on_show = None

    # PyVista-ish surface used by loading / app code
    def set_background(self, *_a, **_k):
        pass

    def add_text(self, text, **kwargs):
        actor = _FakeActor()
        actor.SetInput(text)
        self.actors[kwargs.get('name', text)] = actor
        return actor

    def add_key_event(self, key, callback):
        self.events[key] = callback

    def render(self):
        self.rendered += 1

    def screenshot(self, path):
        Path(path).write_bytes(b'png')

    def show(self, **_kwargs):
        if self.on_show is not None:
            self.on_show()

    def close(self):
        self.closed = True


class _DoneHandle:
    """Just enough of JobHandle for LoadingWindow/load_job."""

    def __init__(self):
        self.generation = 1
        self.events = queue.Queue()
        self.done = threading.Event()
        self.done.set()
        self.cancelled = False

    def emit(self, stage, text, fraction=None, **detail):
        self.events.put((stage, text, fraction, detail))

    def check_cancel(self):
        if self.cancelled:
            raise Cancelled('cancelled by test')

    def cancel(self):
        self.cancelled = True


def _fake_loading_window(plotter):
    window = object.__new__(loading.LoadingWindow)
    window.off_screen = False
    window.closed_by_user = False
    window.finished = False
    window.plotter = plotter
    window.title_actor = _FakeActor()
    window.stage_actor = _FakeActor()
    window.detail_actor = _FakeActor()
    window.progress_actor = _FakeActor()
    window.elapsed_actor = _FakeActor()
    window.hint_actor = _FakeActor()
    window._details = deque(maxlen=4)
    window._started = time.perf_counter()
    window._last_stage = ''
    window._last_fraction = None
    window._handle = None
    window._close_requested = False
    window._observer_id = None
    window._timer_id = None
    return window


class InteractionHelperTests(unittest.TestCase):
    def test_add_remove_timer_uses_native_interactor(self):
        plotter = _FakePlotter()
        calls = []
        observer, timer = interaction.add_timer(
            plotter, lambda *_: calls.append(1), 100)
        self.assertIn(observer, plotter.iren.interactor.observers)
        self.assertEqual(plotter.iren.initialize_calls, 1)
        self.assertEqual(plotter.iren.interactor.timers, [100])
        interaction.remove_timer(plotter, observer, timer)
        self.assertEqual(plotter.iren.interactor.destroyed, [timer])
        self.assertEqual(plotter.iren.interactor.removed, [observer])

    def test_missing_interactor_is_a_clear_error(self):
        plotter = _FakePlotter()
        plotter.iren = None
        with self.assertRaises(RuntimeError):
            interaction.add_timer(plotter, lambda *_: None, 100)
        interaction.remove_timer(plotter, 1, 1)  # no crash, nothing to remove
        interaction.terminate(plotter)  # falls back to plotter.close


class LoadingWindowLifecycleTests(unittest.TestCase):
    def test_run_binds_timer_and_cleans_it_up(self):
        plotter = _FakePlotter()

        def on_show():
            # drive the repeating timer once; the handle is already done
            for callback in list(plotter.iren.interactor.observers.values()):
                callback()

        plotter.on_show = on_show
        window = _fake_loading_window(plotter)
        handle = _DoneHandle()
        window.run(handle)
        self.assertTrue(window.finished)
        self.assertFalse(window.closed_by_user)
        self.assertIn('Escape', plotter.events)
        self.assertEqual(plotter.iren.interactor.timers, [loading.LoadingWindow.POLL_MS])
        self.assertEqual(len(plotter.iren.interactor.destroyed), 1)
        self.assertEqual(len(plotter.iren.interactor.removed), 1)
        self.assertTrue(plotter.iren.interactor.terminated)
        self.assertTrue(plotter.closed)

    def test_escape_cancels_and_closes(self):
        plotter = _FakePlotter()
        window = _fake_loading_window(plotter)

        class RunningHandle(_DoneHandle):
            def __init__(self):
                super().__init__()
                self.done.clear()

        handle = RunningHandle()
        plotter.on_show = lambda: plotter.events['Escape']()
        window.run(handle)
        self.assertTrue(handle.cancelled)
        self.assertTrue(window.closed_by_user)
        self.assertFalse(window.finished)
        self.assertTrue(plotter.closed)

    def test_show_message_interactive_lifecycle(self):
        plotter = _FakePlotter()
        plotter.on_show = lambda: plotter.events['Escape']()
        with mock.patch.object(loading.pv, 'Plotter', lambda **_k: plotter):
            loading.show_message(False, (800, 600), 'error', ['line'],
                                 auto_close_ms=500)
        self.assertIn('Escape', plotter.events)
        self.assertEqual(plotter.iren.interactor.timers, [1000])
        self.assertEqual(len(plotter.iren.interactor.destroyed), 1)
        self.assertEqual(len(plotter.iren.interactor.removed), 1)
        self.assertTrue(plotter.closed)

    def test_schedule_close_quits_and_removes_timer(self):
        plotter = _FakePlotter()

        class FakeViewer:
            def __init__(self):
                self.plotter = plotter
                self.actions = []

            def request_action(self, kind, value=None):
                self.actions.append(kind)

        viewer = FakeViewer()
        app = object.__new__(Application)
        app._schedule_close(viewer, 1)
        self.assertEqual(len(plotter.iren.interactor.observers), 1)
        for callback in list(plotter.iren.interactor.observers.values()):
            time.sleep(0.01)
            callback()
        self.assertEqual(viewer.actions, ['quit'])
        self.assertEqual(len(plotter.iren.interactor.destroyed), 1)
        self.assertEqual(len(plotter.iren.interactor.removed), 1)


class StaticInteractionOwnershipTests(unittest.TestCase):
    """No CamelCase VTK call may target PyVista's ``iren`` wrapper again."""

    def test_no_camelcase_calls_on_wrapper(self):
        violations = []
        for path in sorted(APP_DIR.glob('*/*.py')):
            if any(part in ('build', 'tests', '__pycache__') for part in path.parts):
                continue
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
            wrapper_names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Attribute) \
                        and node.value.attr == 'iren':
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            wrapper_names.add(target.id)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Attribute) and func.attr in VTK_CAMEL):
                    continue
                target = func.value
                if isinstance(target, ast.Attribute) and target.attr == 'iren':
                    violations.append(f'{path}:{node.lineno} .iren.{func.attr}')
                elif isinstance(target, ast.Name) and target.id in wrapper_names:
                    violations.append(f'{path}:{node.lineno} {target.id}.{func.attr}')
        self.assertEqual(violations, [], '\n'.join(violations))


class JobTerminalStateTests(unittest.TestCase):
    def _wait(self, handle, timeout=5.0):
        self.assertTrue(handle.done.wait(timeout), 'handle never finished')

    def test_every_handle_reaches_a_terminal_state(self):
        manager = JobManager()
        ok = manager.start('load', {'fn': lambda h: 42})
        self._wait(ok)
        self.assertEqual(ok.outcome.result, 42)

        boom = manager.start('load', {'fn': lambda h: 1 / 0})
        self._wait(boom)
        self.assertIsInstance(boom.outcome.error, ZeroDivisionError)

        gate = threading.Event()
        started = []

        def blocking(handle):
            started.append(handle.generation)
            # Ignore cancellation until released so the queued handle stays
            # pending (the exact window the review reproduced).
            gate.wait(5)
            return 'never'

        first = manager.start('load', {'fn': blocking})
        deadline = time.time() + 3
        while not started and time.time() < deadline:
            time.sleep(0.002)
        queued = manager.start('load', {'fn': lambda h: 'queued'})
        self.assertFalse(queued.done.is_set())
        superseding = manager.start('load', {'fn': lambda h: 'new'})
        self.assertTrue(queued.done.wait(2))
        self.assertTrue(queued.outcome.cancelled)
        gate.set()
        self._wait(first)
        self._wait(superseding)
        self.assertEqual(superseding.outcome.result, 'new')
        manager.shutdown()

    def test_ab_queued_c_race_never_loses_b(self):
        manager = JobManager()
        started = []
        active = 0
        max_active = 0
        lock = threading.Lock()

        def first(handle):
            nonlocal active, max_active
            with lock:
                started.append(handle.generation)
                active += 1
                max_active = max(max_active, active)
            try:
                # Deliberately slow to stop: keeps A current while B and C
                # arrive, which is the race the review reproduced.
                time.sleep(0.3)
                return 'first'
            finally:
                with lock:
                    active -= 1

        def quick(handle):
            nonlocal active, max_active
            with lock:
                started.append(handle.generation)
                active += 1
                max_active = max(max_active, active)
            try:
                return 'quick'
            finally:
                with lock:
                    active -= 1

        a = manager.start('load', {'fn': first})
        deadline = time.time() + 3
        while not started and time.time() < deadline:
            time.sleep(0.002)
        b = manager.start('load', {'fn': quick})
        c = manager.start('load', {'fn': quick})
        self.assertTrue(b.done.wait(2), 'superseded B never got a terminal state')
        self.assertTrue(b.outcome.cancelled)
        self.assertIsInstance(b.outcome.error, Cancelled)
        self.assertFalse(c.done.is_set())
        self._wait(a)
        self._wait(c)
        self.assertEqual(c.outcome.result, 'quick')
        self.assertEqual(started, [a.generation, c.generation])
        self.assertEqual(max_active, 1)
        manager.shutdown()

    def test_shutdown_cancels_queued_handle(self):
        manager = JobManager()
        release = threading.Event()

        def blocking(handle):
            handle.check_cancel()
            release.wait(5)
            handle.check_cancel()

        running = manager.start('load', {'fn': blocking})
        queued = manager.start('load', {'fn': lambda h: 'x'})
        manager.shutdown(wait_s=0.01)
        self.assertTrue(queued.done.wait(2))
        self.assertTrue(queued.outcome.cancelled)
        release.set()
        running.done.wait(2)


class ViewerHonestyAndReservedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not DATA.is_dir():
            raise unittest.SkipTest(f'raw data folder missing: {DATA}')
        if OUT.exists():
            shutil.rmtree(OUT, ignore_errors=True)
        OUT.mkdir(parents=True, exist_ok=True)
        cls.cache = OUT / 'cache'
        cls.cache.mkdir(parents=True, exist_ok=True)
        cls.handle = _DoneHandle()
        cls.result = load_job.load_directory(
            cls.handle, directory=str(DATA), cache_dir=cls.cache,
            use_cache=False)
        cls.viewer = TrialViewer(
            cls.result.dataset, cls.result.field, cls.result.raw_spacing,
            off_screen=True, window_size=(1200, 700),
            timings=cls.result.timings, cache_hit=False,
            source_note=cls.result.source.note)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.viewer.plotter.close()
        except Exception:
            pass

    def test_raw_readout_has_no_fake_single_depth(self):
        self.viewer.set_view_mode('raw')
        text = self.viewer.depth_value_actor.GetInput()
        self.assertIn('RAW', text)
        self.assertNotIn('heff', text)
        help_text = self.viewer.help_actor.GetInput()
        self.assertIn('RAW', help_text)
        self.assertNotIn('exact slice', help_text)
        snapshot = self.viewer.build_snapshot('both')
        self.assertEqual(snapshot.depth_kind, 'not_a_slice')
        self.assertIsNone(snapshot.depth_nm)

    def test_slice_readout_keeps_actual_depth(self):
        self.viewer.set_view_mode('normal')
        text = self.viewer.depth_value_actor.GetInput()
        self.assertIn('heff =', text)
        self.assertIn('exact slice', self.viewer.help_actor.GetInput())
        snapshot = self.viewer.build_snapshot('both')
        self.assertEqual(snapshot.depth_kind, 'slice')
        self.assertAlmostEqual(snapshot.depth_nm, self.viewer.current_depth)

    def test_reserved_xy_slice_and_exclusion_interfaces(self):
        with self.assertRaises(NotImplementedError):
            self.viewer.set_slice_axis('x')
        with self.assertRaises(NotImplementedError):
            self.viewer.set_slice_axis('y')
        with self.assertRaises(ValueError):
            self.viewer.set_slice_axis('q')
        with self.assertRaises(NotImplementedError):
            field.recompute_with_exclusions(self.result.dataset,
                                            excluded_group_ids=['G:0,0'])
        with self.assertRaises(NotImplementedError):
            field.recompute_with_exclusions(self.result.dataset)


if __name__ == '__main__':
    unittest.main()
