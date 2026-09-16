"""R05 回归测试：取消请求与终态提交的竞争、终态展示不被污染、取消可收敛。

原缺陷：``review_store.request_cancel`` 先读后无条件写，取消线程读到 running 之后、写回之前工作线程提交 done，陈旧写把终态翻回
cancelling。覆盖：受控交错（非 sleep）竞争、取消优先、超时/末期回调不污染终态、重复与排队取消收敛，缓存与库一致且无遗留 cancelling。"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta

from app.api import routes
from app.core.config import get_settings
from app.models.base import SessionLocal as RealSessionLocal
from app.services import review_store

LOCAL_USER = {"id": "local-dev", "username": "local-dev", "capabilities": []}


def _queued_review(owner_id: str = "local-dev") -> str:
    document_id = uuid.uuid4().hex[:12]
    review_store.create_document({
        "id": document_id,
        "filename": f"{document_id}.pdf",
        "file_path": f"{document_id}.pdf",
        "file_type": "pdf",
        "size": 14,
        "mime_type": "application/pdf",
        "sha256": uuid.uuid4().hex,
    })
    stored = review_store.create_review(
        document_id,
        "campus_general_v1",
        ruleset_snapshot="ruleset: campus_general_v1\nname: t\nrules: []\n",
        idempotency_key=uuid.uuid4().hex,
        use_ai=False,
        owner_id=owner_id,
    )
    return stored["review_id"]


def _row(review_id: str) -> dict:
    row = review_store.get_review(review_id)
    assert row is not None
    return row


# ── 受控交错：工作线程在取消的写入之前提交终态 ──────────────

class _HookedSession:
    """在第一次 execute（取消的条件 UPDATE）之前触发工作线程终态提交。"""

    def __init__(self, real_session, hook):
        self._real = real_session
        self._hook = hook
        self._fired = False

    def execute(self, *args, **kwargs):
        if not self._fired:
            self._fired = True
            self._hook()
        return self._real.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _HookedBegin:
    def __init__(self, factory):
        self._factory = factory
        self._cm = None

    def __enter__(self):
        self._cm = RealSessionLocal.begin()
        session = self._cm.__enter__()
        return _HookedSession(session, self._factory.hook)

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)


class _HookedSessionFactory:
    def __init__(self):
        self.hook = lambda: None
        self.real = True

    def begin(self):
        if self.real:
            return RealSessionLocal.begin()
        return _HookedBegin(self)


def test_worker_done_before_cancel_update_keeps_done(monkeypatch):
    """工作线程先提交 done → 取消的条件 UPDATE 不命中，行保持 done。"""
    review_id = _queued_review()
    review_store.mark_running(review_id)
    factory = _HookedSessionFactory()
    fired: dict = {}

    def hook():
        factory.real = True  # 工作线程用独立连接提交（真实交错点）
        try:
            fired["done"] = review_store.finalize(
                review_id, "done", stage="completed", progress=100,
                error_message=None, completed_at=review_store.now(),
            )
        finally:
            factory.real = False

    factory.hook = hook
    factory.real = False
    monkeypatch.setattr(review_store, "SessionLocal", factory)

    returned = review_store.request_cancel(review_id)
    monkeypatch.undo()

    assert fired.get("done") is True
    assert returned is not None and returned["status"] == "done"
    row = _row(review_id)
    assert row["status"] == "done"
    assert row["cancel_requested"] is False
    assert row["stage"] == "completed"


def test_cancel_after_terminal_is_noop(monkeypatch):
    review_id = _queued_review()
    assert review_store.finalize(review_id, "done", progress=100) is True
    returned = review_store.request_cancel(review_id)
    assert returned is not None and returned["status"] == "done"
    row = _row(review_id)
    assert row["status"] == "done" and row["cancel_requested"] is False


def test_cancel_first_then_late_worker_done_ends_cancelled():
    review_id = _queued_review()
    review_store.mark_running(review_id)
    assert review_store.request_cancel(review_id) is not None
    assert _row(review_id)["status"] == "cancelling"
    # 工作线程迟到：done 被拒 → routes._finalize_review 依胜出规则改判 cancelled
    assert routes._finalize_review(review_id, "done", progress=100) is True
    row = _row(review_id)
    assert row["status"] == "cancelled"
    assert row["cancel_requested"] is True


def test_duplicate_cancel_is_idempotent():
    review_id = _queued_review()
    first = review_store.request_cancel(review_id)
    second = review_store.request_cancel(review_id)
    assert first is not None and second is not None
    assert first["status"] == second["status"] == "cancelling"
    assert second["cancel_requested"] is True
    assert review_store.finalize(review_id, "cancelled") is True
    assert _row(review_id)["status"] == "cancelled"


def test_store_rejection_requires_route_level_convergence():
    """契约：store.finalize 被取消请求拒绝时返回 False，由出口改判 cancelled。"""
    review_id = _queued_review()
    review_store.mark_running(review_id)
    review_store.request_cancel(review_id)
    assert review_store.finalize(review_id, "done", progress=100) is False
    assert _row(review_id)["status"] == "cancelling"  # 等待出口收敛，不得停在此态
    assert routes._finalize_review(review_id, "done", progress=100) is True
    assert _row(review_id)["status"] == "cancelled"


# ── 终态展示不被污染 ─────────────────────────────────────

def test_update_review_does_not_pollute_terminal_row():
    review_id = _queued_review()
    review_store.finalize(
        review_id, "done", stage="completed", progress=100,
        result_json={"conclusion": "pass", "issues": []},
        completed_at=review_store.now(),
    )
    # 迟到的规则回调/心跳尝试写进度
    returned = review_store.update_review(
        review_id, stage="rules", progress=1, current_rule="C001"
    )
    assert returned is not None
    assert returned["status"] == "done"
    assert returned["stage"] == "completed"
    assert returned["progress"] == 100
    assert returned["current_rule"] is None
    row = _row(review_id)
    assert (row["stage"], row["progress"], row["current_rule"]) == (
        "completed", 100, None,
    )


def test_update_review_still_updates_active_row():
    review_id = _queued_review()
    review_store.mark_running(review_id)
    returned = review_store.update_review(review_id, stage="rules", progress=42)
    assert returned is not None
    assert returned["progress"] == 42 and returned["stage"] == "rules"


def test_routes_update_review_cache_matches_db_after_terminal():
    review_id = _queued_review()
    review_store.finalize(review_id, "cancelled", stage="cancelled", progress=100)
    routes._update_review(review_id, stage="rules", progress=3)
    stored = _row(review_id)
    cached = routes._get_review(review_id) or {}
    assert cached.get("status") == stored["status"] == "cancelled"
    assert cached.get("stage") == stored["stage"] == "cancelled"
    assert cached.get("progress") == stored["progress"] == 100


# ── 路由层：排队取消与执行器收敛 ───────────────────────────

def test_cancel_route_converges_when_executor_has_no_task(monkeypatch):
    """执行器已不持有该任务 → 取消必须收敛为 cancelled，不留 cancelling。"""
    review_id = _queued_review()
    review_store.mark_running(review_id)
    monkeypatch.setattr(routes._task_executor, "cancel", lambda task_id: False)
    payload = routes.cancel_review(review_id, LOCAL_USER)
    assert payload["status"] == "cancelled"
    assert _row(review_id)["status"] == "cancelled"
    assert _row(review_id)["cancel_requested"] is True


def test_cancel_route_queued_review_ends_cancelled(monkeypatch):
    review_id = _queued_review()
    monkeypatch.setattr(routes._task_executor, "cancel", lambda task_id: False)
    payload = routes.cancel_review(review_id, LOCAL_USER)
    assert payload["status"] == "cancelled"
    row = _row(review_id)
    assert row["status"] == "cancelled" and row["cancel_requested"] is True
    cached = routes._get_review(review_id) or {}
    assert cached["status"] == "cancelled"


def test_cancel_route_on_completed_review_returns_terminal(monkeypatch):
    review_id = _queued_review()
    review_store.finalize(review_id, "done", progress=100, stage="completed")
    calls: list[str] = []
    monkeypatch.setattr(
        routes._task_executor, "cancel", lambda task_id: calls.append(task_id) or True
    )
    payload = routes.cancel_review(review_id, LOCAL_USER)
    assert payload["status"] == "done"
    assert calls == []  # 终态先提交者胜出：不再打扰执行器
    assert _row(review_id)["cancel_requested"] is False


# ── 看门狗收敛 ───────────────────────────────────────────

def test_watchdog_converges_stuck_cancelling(monkeypatch):
    # 注意：别的测试会 cache_clear get_settings，必须打 routes 实际引用的对象
    monkeypatch.setattr(routes.settings, "TASK_HEARTBEAT_TIMEOUT_SECONDS", 0)
    review_id = _queued_review()
    review_store.mark_running(review_id)
    review_store.request_cancel(review_id)
    assert _row(review_id)["status"] == "cancelling"
    routes._reap_stale_reviews(datetime.now().astimezone())
    assert _row(review_id)["status"] == "cancelled"


def test_watchdog_times_out_idle_running_review(monkeypatch):
    monkeypatch.setattr(routes.settings, "TASK_HEARTBEAT_TIMEOUT_SECONDS", 0)
    review_id = _queued_review()
    review_store.mark_running(review_id)
    routes._reap_stale_reviews(datetime.now().astimezone())
    assert _row(review_id)["status"] == "timed_out"


# ── 并发一致性：永不遗留 cancelling ───────────────────────

def test_concurrent_cancel_and_done_never_leave_cancelling():
    review_id = _queued_review()
    review_store.mark_running(review_id)
    barrier = threading.Barrier(2)
    errors: list[str] = []

    def canceller():
        try:
            barrier.wait(timeout=10)
            review_store.request_cancel(review_id)
        except Exception as exc:  # noqa: BLE001 —— SQLite 写竞争记为可接受错误
            errors.append(f"cancel:{type(exc).__name__}")

    def worker():
        try:
            barrier.wait(timeout=10)
            # 真实工作线程出口：finalize 被拒时由 _finalize_review 依胜出规则改判
            routes._finalize_review(
                review_id, "done", stage="completed", progress=100,
                completed_at=review_store.now(),
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"done:{type(exc).__name__}")

    threads = [threading.Thread(target=canceller), threading.Thread(target=worker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    row = _row(review_id)
    assert row["status"] in {"done", "cancelled"}, (row["status"], errors)
    if row["status"] == "done":
        assert row["cancel_requested"] is False
    else:
        assert row["cancel_requested"] is True
    # 本任务不得遗留在 cancelling（会话级数据库中可能有其它测试的活动任务）
    assert row["status"] != "cancelling"
    assert review_store.get_review(review_id)["status"] in {"done", "cancelled"}
