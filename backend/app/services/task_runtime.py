"""Cooperative cancellation and deadlines for review worker threads."""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator


class TaskCancelled(RuntimeError):
    """Raised when a user cancels a queued or running review."""


class TaskDeadlineExceeded(TimeoutError):
    """Raised when a review or rule exceeds its configured deadline."""


@dataclass
class RuntimeState:
    task_id: str
    cancel_event: threading.Event
    task_deadline: float
    rule_deadline: float | None = None
    heartbeat_callback: Callable[[str], None] | None = None
    llm_calls: int = 0
    # R08：材料外发授权必须在任务作用域内显式打开；默认关闭（拒绝一切外发）。
    egress_authorized: bool = False


_local = threading.local()


@contextmanager
def task_scope(
    task_id: str,
    cancel_event: threading.Event,
    timeout_seconds: float,
    heartbeat_callback: Callable[[str], None] | None = None,
    *,
    egress_authorized: bool = False,
) -> Iterator[None]:
    previous = getattr(_local, "state", None)
    _local.state = RuntimeState(
        task_id=task_id,
        cancel_event=cancel_event,
        task_deadline=time.monotonic() + max(1.0, timeout_seconds),
        heartbeat_callback=heartbeat_callback,
        egress_authorized=bool(egress_authorized),
    )
    try:
        yield
    finally:
        _local.state = previous


def authorize_egress(enabled: bool = True) -> None:
    """在**当前任务作用域内**显式打开/关闭材料外发授权（R08）。

    只有走完“任务已持久化授权记录 + 部署策略允许”的调用方才能打开；作用域结束即随 state 丢弃，不泄漏给其它任务。"""
    state = current_state()
    if state is not None:
        state.egress_authorized = bool(enabled)


def egress_is_authorized() -> bool:
    """当前线程是否被授权外发材料。

    无任务作用域（连通性检查、后台脚本）一律返回 False —— 默认拒绝，避免“忘了校验”变成“默认放行”。"""
    state = current_state()
    return bool(state is not None and state.egress_authorized)


@contextmanager
def rule_scope(timeout_seconds: float) -> Iterator[None]:
    state = current_state()
    if state is None:
        yield
        return
    previous = state.rule_deadline
    state.rule_deadline = min(
        state.task_deadline,
        time.monotonic() + max(1.0, timeout_seconds),
    )
    try:
        checkpoint()
        yield
        checkpoint()
    finally:
        state.rule_deadline = previous


def current_state() -> RuntimeState | None:
    return getattr(_local, "state", None)


def checkpoint() -> None:
    state = current_state()
    if state is None:
        return
    if state.cancel_event.is_set():
        raise TaskCancelled("审查任务已取消")
    deadline = state.rule_deadline or state.task_deadline
    if time.monotonic() >= deadline:
        if state.rule_deadline is not None:
            raise TaskDeadlineExceeded("单条审查规则执行超时")
        raise TaskDeadlineExceeded("审查任务执行超时")


def remaining_seconds(default: float) -> float:
    """Return a positive request timeout bounded by the active deadline."""
    state = current_state()
    if state is None:
        return max(1.0, default)
    checkpoint()
    deadline = state.rule_deadline or state.task_deadline
    return max(1.0, min(default, deadline - time.monotonic()))


def heartbeat(detail: str = "working") -> None:
    state = current_state()
    if state is None:
        return
    checkpoint()
    if state.heartbeat_callback is not None:
        state.heartbeat_callback(detail)


def consume_llm_call(max_calls: int) -> bool:
    """Reserve one provider call within the active task cost budget."""
    state = current_state()
    if state is None:
        return True
    checkpoint()
    if state.llm_calls >= max(1, max_calls):
        return False
    state.llm_calls += 1
    return True
