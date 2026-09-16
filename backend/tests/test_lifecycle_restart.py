"""R12 回归测试：同一进程内的启动 / 关闭 / 再次启动。

原缺陷：``_task_executor`` 在模块导入时创建并永久复用，``shutdown_services`` 关闭它后再次 ``startup_services`` 仍持有已关闭执行器，
第二次提交抛 "cannot schedule new futures after shutdown"。覆盖：两次生命周期、失败启动后重试、关闭幂等、排队/运行任务时关闭后重启、导入模块不启动服务。"""
from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from app.api import routes
from app.core.config import get_settings


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def lifecycle():
    """保存并恢复进程级服务状态，避免影响同会话中的其它测试。"""
    was_started = routes._services_started
    yield
    # 先让本次测试可能遗留的任务结束，再恢复启动状态
    retired = routes._retired_executor
    if retired is not None:
        _wait_until(lambda: retired.active_count() == 0, timeout=5)
    if not routes._services_started:
        routes.startup_services(allow_retired_tasks=True)
    assert routes._services_started and routes._task_executor is not None
    assert was_started  # 会话级夹具保证进入测试前服务已启动


def test_restart_same_process_accepts_new_submissions(lifecycle):
    started_executor = routes._task_executor
    assert started_executor is not None
    done = threading.Event()
    routes._require_executor().submit("lifecycle-1", done.set)
    assert done.wait(5)

    routes.shutdown_services()
    assert routes._services_started is False
    assert routes._task_executor is None
    assert routes._watchdog_thread is None

    routes.startup_services()
    new_executor = routes._task_executor
    assert new_executor is not None
    assert new_executor is not started_executor  # 绝不复用已关闭的执行器
    done2 = threading.Event()
    new_executor.submit("lifecycle-2", done2.set)
    assert done2.wait(5), "同一进程重新启动后必须能再次提交任务"


def test_double_shutdown_is_idempotent(lifecycle):
    routes.shutdown_services()
    routes.shutdown_services()  # 第二次必须是安全 no-op
    assert routes._services_started is False
    assert routes._task_executor is None
    assert routes.active_task_count() == 0


def test_restart_refused_while_retired_task_still_running(lifecycle, monkeypatch):
    release = threading.Event()
    started = threading.Event()

    def blocker():
        started.set()
        release.wait(10)

    monkeypatch.setattr(routes.settings, "TASK_MAX_WORKERS", 1)
    routes.shutdown_services()
    routes.startup_services(allow_retired_tasks=True)
    routes._require_executor().submit("lifecycle-stuck", blocker)
    assert started.wait(5)

    routes.shutdown_services()  # 运行中的任务不可中断 → 旧执行器仍有活动任务
    assert routes._retired_executor is not None
    assert routes._retired_executor.active_count() > 0

    with pytest.raises(RuntimeError, match="拒绝重叠启动"):
        routes.startup_services()
    # 拒绝后不得留下半启动状态
    assert routes._services_started is False
    assert routes._task_executor is None
    assert routes._watchdog_thread is None

    release.set()
    assert _wait_until(lambda: routes._retired_executor.active_count() == 0, 10)
    routes.startup_services(allow_retired_tasks=True)
    assert routes._services_started is True


def test_queued_tasks_cancelled_on_shutdown_then_restart_ok(lifecycle, monkeypatch):
    release = threading.Event()
    started = threading.Event()

    def blocker():
        started.set()
        release.wait(10)

    monkeypatch.setattr(routes.settings, "TASK_MAX_WORKERS", 1)
    routes.shutdown_services()
    routes.startup_services(allow_retired_tasks=True)
    executor = routes._require_executor()
    executor.submit("lifecycle-run", blocker)
    assert started.wait(5)
    executor.submit("lifecycle-queued", lambda: None)  # 只有一个 worker → 排队

    routes.shutdown_services()  # 排队任务被 cancel_futures 取消
    release.set()
    assert _wait_until(lambda: routes._retired_executor.active_count() == 0, 10)

    routes.startup_services()  # 无残留任务 → 允许直接重启
    done = threading.Event()
    routes._require_executor().submit("lifecycle-after", done.set)
    assert done.wait(5)


def test_failed_startup_releases_created_resources(lifecycle, monkeypatch):
    routes.shutdown_services()
    created: list = []

    class _SpyExecutor:
        def __init__(self, **kwargs):
            self.shutdown_called = False
            created.append(self)

        def shutdown(self, wait: bool = False) -> None:
            self.shutdown_called = True

        def active_count(self) -> int:
            return 0

    monkeypatch.setattr(routes, "BoundedTaskExecutor", _SpyExecutor)
    monkeypatch.setattr(
        routes.review_store, "init_store",
        lambda: (_ for _ in ()).throw(RuntimeError("db unavailable")),
    )
    with pytest.raises(RuntimeError, match="db unavailable"):
        routes.startup_services()

    assert created and created[0].shutdown_called is True
    assert routes._services_started is False
    assert routes._task_executor is None
    assert routes._watchdog_thread is None


def test_watchdog_thread_is_recreated_and_not_left_behind(lifecycle):
    routes.shutdown_services()
    routes.startup_services(allow_retired_tasks=True)
    first = routes._watchdog_thread
    assert first is not None and first.is_alive()
    routes.shutdown_services()
    assert routes._watchdog_thread is None
    assert not first.is_alive()
    routes.startup_services()
    second = routes._watchdog_thread
    assert second is not None and second.is_alive()
    assert second is not first


def test_health_reports_zero_tasks_when_stopped(lifecycle):
    routes.shutdown_services()
    assert routes.active_task_count() == 0  # 不得因执行器缺失而抛异常


def test_asgi_lifespan_start_stop_and_restart(lifecycle):
    """真实 ASGI 生命周期：进入/退出 lifespan 后可再次启动并提交任务。"""
    from starlette.testclient import TestClient

    from app.main import app

    routes.shutdown_services()
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["checks"]["active_tasks"] == 0
        assert routes._services_started is True
    assert routes._services_started is False
    assert routes._task_executor is None

    routes.startup_services(allow_retired_tasks=True)
    done = threading.Event()
    routes._require_executor().submit("lifecycle-lifespan", done.set)
    assert done.wait(5)


def test_importing_routes_does_not_start_services():
    """子进程验证：导入模块本身不创建执行器、不启动服务。"""
    settings = get_settings()
    code = (
        "import app.api.routes as r;"
        "print(r._services_started, r._task_executor is None, r._watchdog_thread is None)"
    )
    env = {
        "PATH": __import__("os").environ.get("PATH", ""),
        "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""),
        "DATABASE_URL": f"sqlite:///{(settings.UPLOAD_DIR + '/import-check.db').replace(chr(92), '/')}",
        "UPLOAD_DIR": settings.UPLOAD_DIR,
        "OUTPUT_DIR": settings.OUTPUT_DIR,
        "RULES_DIR": settings.RULES_DIR,
        "AUTH_REQUIRED": "false",
        "LLM_API_KEY": "",
        "ACCESS_TOKEN": "",
        "ADMIN_TOKEN": "",
        "OCR_ENABLED": "false",
    }
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(__import__("pathlib").Path(__file__).resolve().parent.parent),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "False True True", completed.stdout
