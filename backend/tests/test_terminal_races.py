"""B3: terminal-state race contract (C1/C2) — store atomics + route decision.
No sleeps: finalize is first-writer-wins (cancel_requested refuses done/failed/timed_out, allows cancelled);
mark_running cannot resurrect cancelled rows; late "done" after user cancel → cancelled; concurrent finalize → one winner.
胜出规则（与 review_store 一致）：cancelled(user) > timed_out(system) > done/failed(worker)。"""
from __future__ import annotations

import threading
import uuid

from app.api import routes
from app.services import review_store


def _queued_review(tmp_path) -> str:
    document_id = uuid.uuid4().hex[:12]
    source = tmp_path / f"{document_id}.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF")
    review_store.create_document({
        "id": document_id,
        "filename": source.name,
        "file_path": str(source),
        "file_type": "pdf",
        "size": source.stat().st_size,
        "mime_type": "application/pdf",
        "sha256": uuid.uuid4().hex,
    })
    stored = review_store.create_review(
        document_id,
        "campus_general_v1",
        ruleset_snapshot="ruleset: campus_general_v1\nname: t\nrules: []\n",
        idempotency_key=uuid.uuid4().hex,
        use_ai=False,
    )
    return stored["review_id"]


def _status(review_id: str) -> str:
    row = review_store.get_review(review_id)
    assert row is not None
    return row["status"]


def test_finalize_first_writer_wins(tmp_path):
    review_id = _queued_review(tmp_path)
    assert review_store.finalize(review_id, "done", progress=100) is True
    # 已终态的行不接受任何新的终态写入
    assert review_store.finalize(review_id, "timed_out") is False
    assert review_store.finalize(review_id, "failed") is False
    assert review_store.finalize(review_id, "cancelled") is False
    assert _status(review_id) == "done"


def test_finalize_rejects_non_cancelled_after_cancel_request(tmp_path):
    review_id = _queued_review(tmp_path)
    review_store.request_cancel(review_id)
    assert _status(review_id) == "cancelling"
    # 取消优先：完成/失败/超时都不能覆盖取消请求
    assert review_store.finalize(review_id, "done") is False
    assert review_store.finalize(review_id, "failed") is False
    assert review_store.finalize(review_id, "timed_out") is False
    assert review_store.finalize(review_id, "cancelled") is True
    assert _status(review_id) == "cancelled"


def test_mark_running_does_not_resurrect_cancelled_row(tmp_path):
    review_id = _queued_review(tmp_path)
    review_store.request_cancel(review_id)
    review_store.finalize(review_id, "cancelled")
    assert _status(review_id) == "cancelled"
    # 排队期间被取消的任务不允许被工作线程再次拉起
    assert review_store.mark_running(review_id) is None
    assert _status(review_id) == "cancelled"


def test_mark_running_refuses_row_with_cancel_requested(tmp_path):
    review_id = _queued_review(tmp_path)
    review_store.request_cancel(review_id)
    assert _status(review_id) == "cancelling"
    assert review_store.mark_running(review_id) is None
    assert _status(review_id) == "cancelling"


def test_mark_running_allowed_on_fresh_queued_row(tmp_path):
    review_id = _queued_review(tmp_path)
    running = review_store.mark_running(review_id)
    assert running is not None
    assert _status(review_id) == "running"


def test_worker_late_done_after_cancel_ends_as_cancelled(tmp_path):
    """C1：用户取消发生在 worker 最后一次 checkpoint 之后、写 done 之前。

    复现：直接让 worker 的终态出口在“已请求取消”的行上写 done，规则应拒绝 done 并改判 cancelled（等价真实窗口行为）。"""
    review_id = _queued_review(tmp_path)
    review_store.request_cancel(review_id)
    applied = routes._finalize_review(
        review_id, "done", stage="completed", progress=100
    )
    assert applied is True  # 行最终确实落为终态
    assert _status(review_id) == "cancelled"


def test_worker_timed_out_after_cancel_ends_as_cancelled(tmp_path):
    """C2：取消与超时同现 → 取消优先。"""
    review_id = _queued_review(tmp_path)
    review_store.request_cancel(review_id)
    applied = routes._finalize_review(
        review_id, "timed_out", stage="timed_out", progress=100
    )
    assert applied is True
    assert _status(review_id) == "cancelled"


def test_finalize_syncs_inmemory_read_cache(tmp_path):
    """B3 缓存回归：_finalize_review 走 Core UPDATE 后，读路径必须看到终态。

    缺陷：终态只落库、_reviews 缓存仍是 running/finalizing，GET 永远看不到 done（e2e 轮询 45s 超时）。"""
    review_id = _queued_review(tmp_path)
    assert routes._finalize_review(review_id, "done", stage="completed", progress=100) is True
    assert review_store.get_review(review_id)["status"] == "done"
    assert routes._get_review(review_id)["status"] == "done"


def test_cancel_finalize_syncs_inmemory_read_cache(tmp_path):
    review_id = _queued_review(tmp_path)
    review_store.request_cancel(review_id)
    assert routes._finalize_review(review_id, "cancelled", progress=100) is True
    assert review_store.get_review(review_id)["status"] == "cancelled"
    assert routes._get_review(review_id)["status"] == "cancelled"


def test_concurrent_terminal_writers_only_one_wins(tmp_path):
    """barrier 同步的两个终态主张：恰好一个写入成功，行落在唯一胜者状态。"""
    review_id = _queued_review(tmp_path)
    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, bool]] = []
    outcomes_lock = threading.Lock()

    def writer(status: str):
        barrier.wait()
        applied = review_store.finalize(review_id, status)
        with outcomes_lock:
            outcomes.append((status, applied))

    threads = [
        threading.Thread(target=writer, args=("done",)),
        threading.Thread(target=writer, args=("cancelled",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    winners = [(status, applied) for status, applied in outcomes if applied]
    assert len(winners) == 1, f"终态写入应恰好胜出一个，实际 {outcomes}"
    final = _status(review_id)
    assert winners[0][0] == final
