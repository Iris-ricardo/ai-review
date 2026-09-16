"""并发安全的“按路径串行 + 原子替换”助手（R15）。

同一输出路径串行化，避免并发请求同时“校验缓存 → 写临时文件 → 替换”而互相覆盖；替换用带重试的 ``os.replace``，容忍 Windows 上目标被占用（如并发下载）的 ``PermissionError``，不把并发读变成 500。
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def path_lock(path: str | Path) -> threading.Lock:
    """返回该输出路径对应的进程内互斥锁（按真实路径归一化）。"""
    key = str(Path(path).resolve())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def atomic_replace(
    temporary: str | Path, target: str | Path, *, attempts: int = 40, delay: float = 0.05
) -> None:
    """把临时文件原子替换到目标位置；对 Windows 共享冲突做有限重试。"""
    source = Path(temporary)
    destination = Path(target)
    last_error: OSError | None = None
    for attempt in range(max(1, attempts)):
        try:
            os.replace(source, destination)
            return
        except PermissionError as exc:  # Windows: 目标被其它读者占用
            last_error = exc
            if attempt == attempts - 1:
                break
            time.sleep(delay)
    assert last_error is not None
    raise last_error
