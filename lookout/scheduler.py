"""Runs checks on the interval, and on demand from the dashboard."""

from __future__ import annotations

import logging
import threading
import time

from .checker import Checker, effective_config

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, checker: Checker, run_on_start: bool = True):
        self.checker = checker
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.queue: list[set[str] | None] = []   # None = full check
        self.checking = False
        self.next_run: float = time.time() if run_on_start else time.time() + self.interval()
        self.thread = threading.Thread(target=self._loop, name="lookout-scheduler", daemon=True)

    def interval(self) -> int:
        return effective_config(self.checker.base_cfg, self.checker.store)["interval_seconds"]

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake.set()

    def trigger(self, only: set[str] | None = None):
        with self.lock:
            if only is None:
                self.queue = [q for q in self.queue if q is not None] + [None]
            else:
                self.queue.append(set(only))
        self.wake.set()

    def reschedule(self):
        """Interval changed in settings: count from the last run."""
        last = self.checker.store.data.get("last_run") or time.time()
        self.next_run = max(time.time() + 5, last + self.interval())
        self.wake.set()

    def _loop(self):
        while not self.stop_event.is_set():
            with self.lock:
                job = self.queue.pop(0) if self.queue else "idle"
            if job == "idle" and time.time() >= self.next_run:
                job = None
            if job == "idle":
                self.wake.wait(timeout=max(1.0, min(60.0, self.next_run - time.time())))
                self.wake.clear()
                continue
            self.checking = True
            try:
                self.checker.run(only=job)
            except Exception:
                log.exception("check failed")
            finally:
                self.checking = False
                if job is None:
                    self.next_run = time.time() + self.interval()
