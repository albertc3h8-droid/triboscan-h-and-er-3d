"""Background compute job manager (one worker at a time).

Rules enforced here (captain's design boundary):

* a job never touches a render window, a Plotter or a shared VTK render object;
  it may use local VTK computational filters (the field builder does);
* cancelling a running job never starts a second worker in parallel: the next
  request waits until the worker of the old job has returned, and stale
  outcomes carry their ``generation`` so the app can discard them;
* the worker thread is the *only* launcher of a queued request.  ``start``
  never launches while another handle is still owned by the manager, so the
  A-finishes / B-queued / C-arrives race cannot lose B without a terminal
  outcome;
* every handle handed out by :meth:`JobManager.start` eventually reaches a
  terminal state: completed, failed, or cancelled with ``done`` set.  A
  superseded queued handle is cancelled immediately instead of being dropped;
* after the app closes, no cancelled result is written back anywhere.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
import queue
import threading
from typing import Callable, Optional

from runtime.errors import Cancelled


@dataclass
class JobEvent:
    generation: int
    stage: str
    text: str
    fraction: Optional[float] = None
    detail: dict = dc_field(default_factory=dict)


@dataclass
class JobOutcome:
    generation: int
    kind: str
    result: object = None
    error: Optional[BaseException] = None
    cancelled: bool = False


class JobHandle:
    """One request; also the safe API a job function may use."""

    _counter = 0
    _counter_lock = threading.Lock()

    def __init__(self, kind: str, payload: dict):
        with JobHandle._counter_lock:
            JobHandle._counter += 1
            self.generation = JobHandle._counter
        self.kind = kind
        self.payload = payload
        self.events: queue.Queue[JobEvent] = queue.Queue()
        self.done = threading.Event()
        self.cancel_event = threading.Event()
        self.outcome: Optional[JobOutcome] = None
        self.thread: Optional[threading.Thread] = None

    # -- job-side API -----------------------------------------------------
    def emit(self, stage: str, text: str, fraction: float | None = None,
             **detail) -> None:
        self.events.put(JobEvent(self.generation, stage, text, fraction, detail))

    def check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled('the load was cancelled')

    def cancel(self) -> None:
        self.cancel_event.set()

    # -- result -----------------------------------------------------------
    def result(self):
        if self.outcome is None:
            raise RuntimeError('job has not finished')
        if self.outcome.error is not None:
            raise self.outcome.error
        return self.outcome.result


class JobManager:
    """Serial worker: a new request is queued, never run in parallel."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: Optional[JobHandle] = None
        self._next: Optional[JobHandle] = None
        self._closed = False

    # -- public API -------------------------------------------------------
    def start(self, kind: str, payload: dict) -> JobHandle:
        handle = JobHandle(kind, payload)
        with self._lock:
            if self._closed:
                raise RuntimeError('job manager is closed')
            if self._current is not None:
                # The worker of the current job launches ``handle`` when it
                # returns; nothing may start in parallel with it.
                self._current.cancel()
                if self._next is not None:
                    self._finish_cancelled(
                        self._next, 'superseded by a newer request')
                self._next = handle
            else:
                self._launch(handle)
        return handle

    def cancel_current(self) -> None:
        with self._lock:
            pending = self._next
            self._next = None
            if self._current is not None:
                self._current.cancel()
        if pending is not None:
            self._finish_cancelled(pending, 'cancelled before it started')

    def shutdown(self, wait_s: float = 1.0) -> None:
        """Stop accepting jobs and ask the current worker to stop.

        It never starts new work after this call.  Waiting is bounded; the
        worker is daemon and its cancelled outcome is discarded by the app.
        """
        with self._lock:
            self._closed = True
            current = self._current
            pending = self._next
            self._next = None
            if current is not None:
                current.cancel()
        if pending is not None:
            self._finish_cancelled(pending, 'the application is closing')
        if current is not None and current.thread is not None:
            current.thread.join(timeout=wait_s)

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._current is not None and not self._current.done.is_set()

    # -- internals (lock held unless documented) --------------------------
    def _launch(self, handle: JobHandle) -> None:
        """Start ``handle``'s worker.  Caller holds ``self._lock``."""
        self._current = handle
        thread = threading.Thread(target=self._run, args=(handle,),
                                  name=f'triboscan-job-{handle.generation}',
                                  daemon=True)
        handle.thread = thread
        thread.start()

    @staticmethod
    def _finish_cancelled(handle: JobHandle, reason: str) -> None:
        """Give a handle that never ran a terminal cancelled outcome."""
        if handle.done.is_set():
            return
        handle.cancel()
        handle.outcome = JobOutcome(handle.generation, handle.kind,
                                    error=Cancelled(reason), cancelled=True)
        handle.done.set()

    def _run(self, handle: JobHandle) -> None:
        outcome = JobOutcome(handle.generation, handle.kind)
        try:
            handle.check_cancel()
            fn: Callable[[JobHandle], object] = handle.payload['fn']
            outcome.result = fn(handle)
        except Cancelled as exc:
            outcome.cancelled = True
            outcome.error = exc
        except BaseException as exc:  # noqa: BLE001 - shown to the user
            outcome.error = exc
        finally:
            handle.outcome = outcome
            handle.done.set()
            pending = None
            launch = None
            with self._lock:
                if self._current is handle:
                    self._current = None
                pending = self._next
                self._next = None
                if pending is not None and not self._closed:
                    self._launch(pending)
                    launch = pending
            if pending is not None and pending is not launch:
                # The application closed while this job was finishing.
                self._finish_cancelled(pending, 'the application is closing')
