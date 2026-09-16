"""A bounded in-process executor with cooperative cancellation controls."""
from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable

from app.services.task_runtime import task_scope


class TaskQueueFull(RuntimeError):
    pass


class BoundedTaskExecutor:
    def __init__(self, max_workers: int, queue_capacity: int, timeout_seconds: int):
        self.max_workers = max(1, max_workers)
        self.queue_capacity = max(0, queue_capacity)
        self.timeout_seconds = max(1, timeout_seconds)
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="proposal-review",
        )
        self._slots = threading.BoundedSemaphore(self.max_workers + self.queue_capacity)
        self._lock = threading.RLock()
        self._futures: dict[str, Future] = {}
        self._cancel_events: dict[str, threading.Event] = {}

    def submit(
        self,
        task_id: str,
        fn: Callable,
        *args,
        heartbeat: Callable[[str], None] | None = None,
    ) -> Future:
        if not self._slots.acquire(blocking=False):
            raise TaskQueueFull("审查队列已满，请稍后再试")
        cancel_event = threading.Event()

        def runner():
            with task_scope(
                task_id,
                cancel_event,
                self.timeout_seconds,
                heartbeat_callback=heartbeat,
            ):
                return fn(*args)

        try:
            future = self._executor.submit(runner)
        except Exception:
            self._slots.release()
            raise

        with self._lock:
            self._futures[task_id] = future
            self._cancel_events[task_id] = cancel_event

        def cleanup(_future: Future) -> None:
            with self._lock:
                self._futures.pop(task_id, None)
                self._cancel_events.pop(task_id, None)
            self._slots.release()

        future.add_done_callback(cleanup)
        return future

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            event = self._cancel_events.get(task_id)
            future = self._futures.get(task_id)
        if event is None or future is None:
            return False
        event.set()
        future.cancel()
        return True

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for future in self._futures.values() if not future.done())

    def has_task(self, task_id: str) -> bool:
        with self._lock:
            future = self._futures.get(task_id)
            return future is not None and not future.done()

    def shutdown(self, wait: bool = False) -> None:
        with self._lock:
            events = list(self._cancel_events.values())
        for event in events:
            event.set()
        self._executor.shutdown(wait=wait, cancel_futures=True)
