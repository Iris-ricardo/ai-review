r"""Real Kimi smoke test using synthetic proposal content only.

仅使用合成样本；本脚本是唯一允许真实外发调用外部模型的脚本，必须显式配置 LLM_API_KEY 才启用（默认关闭、绝不进测试）。运行：.\.venv\Scripts\python.exe eval\smoke_llm.py
隔离：用临时数据库/上传/输出/日志与规则副本，不触碰正式 review.db、uploads、outputs，不使用正式账号或外围部署凭据；预检 POST /api/v1/system/llm-check 用隔离库内超级管理员会话。退出码：0=检出注入的身份信息；1=基础设施缺失或失败（未配置 LLM_API_KEY、样本缺失、预检失败、任务未 done）；2=任务 done 但未检出注入的身份信息。"""
from __future__ import annotations

import os
import secrets
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
EVAL_DIR = Path(__file__).resolve().parent
SAMPLE_DOCX = EVAL_DIR / "samples" / "sample_proposal.docx"
INSERTED_SENTENCE = "申请人张三，来自华南理工大学"
SEMANTIC_CHECKERS = {
    "anonymity_check",
    "title_content_match",
    "cross_consistency_semantic",
}
TERMINAL = {"done", "failed", "cancelled", "timed_out"}

# ── 编码：Windows 控制台/重定向默认 cp1252，中文输出会触发 UnicodeEncodeError ──
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# ── 隔离配置：必须在 import app 之前 ─────────────────────────
_runtime = Path(tempfile.mkdtemp(prefix="smoke-llm-runtime-"))
shutil.copytree(PROJECT_ROOT / "rules", _runtime / "rules")
os.environ["DATABASE_URL"] = f"sqlite:///{(_runtime / 'review.db').as_posix()}"
os.environ["UPLOAD_DIR"] = str(_runtime / "uploads")
os.environ["OUTPUT_DIR"] = str(_runtime / "outputs")
os.environ["RULES_DIR"] = str(_runtime / "rules")
os.environ["LOG_FILE"] = str(_runtime / "smoke.log")
os.environ["AUTH_SECRET"] = secrets.token_hex(32)
os.environ["AUTH_REQUIRED"] = "true"
os.environ["DEBUG"] = "false"
os.environ["ACCESS_TOKEN"] = ""   # 不依赖外围部署凭据
os.environ["ADMIN_TOKEN"] = ""    # 不依赖兼容管理凭据

sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

from docx import Document  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402
from app.services import auth_service  # noqa: E402


class InfrastructureError(RuntimeError):
    """密钥、样本或环境缺失导致的失败（退出码 1）。"""


def _make_headers(client: TestClient) -> dict[str, str]:
    """在隔离库内创建随机密码的临时超级管理员并真实登录。"""
    username = f"smoke_{secrets.token_hex(6)}"
    password = secrets.token_hex(16)
    auth_service.create_user(
        {"username": username, "password": password, "role": "super_admin"},
        actor_role="super_admin",
    )
    resp = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    if resp.status_code != 200:
        raise InfrastructureError(f"临时用户登录失败：HTTP {resp.status_code}")
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _wait_review(client: TestClient, review_id: str, headers: dict[str, str]) -> dict:
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/reviews/{review_id}", headers=headers)
        response.raise_for_status()
        data = response.json()
        if data.get("status") in TERMINAL:
            return data
        time.sleep(0.5)
    raise TimeoutError(f"Review {review_id} did not reach a terminal state")


def main() -> int:
    settings = get_settings()
    if not settings.LLM_API_KEY.strip():
        print(
            "未配置 LLM_API_KEY：真实模型冒烟测试需要密钥并会产生费用，本次跳过"
            "（退出码 1 = 基础设施缺失，不是任务失败）"
        )
        shutil.rmtree(_runtime, ignore_errors=True)
        return 1
    if not SAMPLE_DOCX.exists():
        print(f"缺少合成样本：{SAMPLE_DOCX}")
        shutil.rmtree(_runtime, ignore_errors=True)
        return 1

    try:
        with TestClient(app) as client:  # 完整生命周期
            headers = _make_headers(client)
            preflight = client.post(
                "/api/v1/system/llm-check",
                headers=headers,
            )
            if preflight.status_code != 200:
                print({"preflight_failed": preflight.text})
                return 1
            print({"preflight": preflight.json()})

            started_at = time.monotonic()
            with tempfile.TemporaryDirectory(prefix="proposal-smoke-") as temp_dir:
                temp_docx = Path(temp_dir) / "synthetic_identity_proposal.docx"
                shutil.copyfile(SAMPLE_DOCX, temp_docx)
                document = Document(temp_docx)
                document.add_paragraph(INSERTED_SENTENCE)
                document.save(temp_docx)

                with temp_docx.open("rb") as stream:
                    upload = client.post(
                        "/api/v1/documents",
                        files={"file": (temp_docx.name, stream)},
                        headers=headers,
                    )
                upload.raise_for_status()
                review = client.post(
                    "/api/v1/reviews",
                    data={
                        "document_id": upload.json()["document_id"],
                        "ruleset_id": "campus_general_v1",
                        "use_ai": "true",
                        "privacy_consent": "true",
                    },
                    headers=headers,
                )
                review.raise_for_status()
                data = _wait_review(
                    client, review.json()["review_id"], headers
                )
    finally:
        try:
            from app.models.base import engine
            engine.dispose()
        except Exception:
            pass
        shutil.rmtree(_runtime, ignore_errors=True)

    if data.get("status") != "done":
        print({"review_failed": data})
        return 1
    issues = data.get("issues", [])
    semantic = [issue for issue in issues if issue.get("checker") in SEMANTIC_CHECKERS]
    detected_identity = any(
        issue.get("checker") == "anonymity_check"
        and issue.get("severity") == "error"
        and ("张三" in issue.get("evidence", "") or "华南理工大学" in issue.get("evidence", ""))
        for issue in semantic
    )
    print({
        "status": data["status"],
        "conclusion": data.get("conclusion"),
        "semantic_issue_count": len(semantic),
        "identity_detected": detected_identity,
        "duration_seconds": round(time.monotonic() - started_at, 2),
        "rule_statuses": {
            key: value.get("status") for key, value in data.get("rule_statuses", {}).items()
            if value.get("type") in SEMANTIC_CHECKERS
        },
    })
    return 0 if detected_identity else 2


if __name__ == "__main__":
    raise SystemExit(main())
