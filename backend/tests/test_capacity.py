"""B8: 有界执行器容量边界 + 队列满 429 语义（确定性同步，不做计时断言）。"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.api import routes
from app.main import app
from app.services import review_store
from app.services.bounded_executor import BoundedTaskExecutor, TaskQueueFull


def test_bounded_executor_queue_full_then_slots_release():
    """1 worker + 2 排队槽；第 4 个提交必须队列满；任务完成后槽位释放可再提交。"""
    started = threading.Event()
    release = threading.Event()
    executor = BoundedTaskExecutor(max_workers=1, queue_capacity=2, timeout_seconds=30)
    finished: list[str] = []

    def work(task_id: str):
        if task_id == "t1":
            started.set()
        assert release.wait(timeout=5), f"timeout waiting for release in {task_id}"
        finished.append(task_id)

    try:
        future1 = executor.submit("t1", work, "t1")
        assert started.wait(timeout=2), "worker should start t1"
        executor.submit("t2", work, "t2")
        executor.submit("t3", work, "t3")
        with pytest.raises(TaskQueueFull):
            executor.submit("t4", work, "t4")

        release.set()
        assert future1.result(timeout=5) is None
        # done 回调释放槽位 → 可以再次提交
        future5 = executor.submit("t5", work, "t5")
        assert executor.has_task("t5")
        assert future5.result(timeout=5) is None
        assert sorted(finished) == ["t1", "t2", "t3", "t5"]
    finally:
        executor.shutdown(wait=True)


def test_bounded_executor_cancel_queued_task():
    """排队中的任务可被取消；取消不释放已占 worker 之外的空间导致异常。"""
    started = threading.Event()
    release = threading.Event()
    executor = BoundedTaskExecutor(max_workers=1, queue_capacity=1, timeout_seconds=30)

    def blocker():
        started.set()
        release.wait(timeout=5)

    def quick():
        release.wait(timeout=5)

    try:
        executor.submit("block", blocker)
        assert started.wait(timeout=2)
        queued = executor.submit("queued", quick)
        assert executor.cancel("queued") is True
        assert executor.has_task("queued") is False
        assert executor.has_task("block") is True
        release.set()
        assert queued.cancelled()  # 排队 future 取消成功，未执行
    finally:
        executor.shutdown(wait=True)


def test_create_review_returns_429_when_queue_full(monkeypatch):
    """队列满时创建审查返回 429（不丢状态：任务落为 failed/queue_full）。"""
    client = TestClient(app)
    uploaded = client.post(
        "/api/v1/documents",
        files={"file": ("queue-full.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")},
    )
    assert uploaded.status_code == 200
    document_id = uploaded.json()["document_id"]

    class FullExecutor:
        def submit(self, *args, **kwargs):
            raise TaskQueueFull("审查队列已满，请稍后再试")

    monkeypatch.setattr(routes, "_task_executor", FullExecutor())
    response = client.post(
        "/api/v1/reviews",
        data={
            "document_id": document_id,
            "ruleset_id": "campus_general_v1",
            "use_ai": "false",
        },
    )
    assert response.status_code == 429
    review_store.purge_documents([document_id])
