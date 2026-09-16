"""B6: 中文报告字体候选/配置优先/失败可诊断 + 报告原子写 + 健康检查分级。

测试顺序敏感：字体一旦注册成功即保留到进程结束，“失败/回退”用例必须排在“注册成功”用例
之前（pytest 按文件内定义顺序执行）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import get_settings
from app.services.report import reporter
from app.services.report.reporter import generate_review_report


def _windows_font() -> Path:
    for name in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simsun.ttc"):
        path = Path(name)
        if path.is_file():
            return path
    return Path("")


def _sample_review() -> dict:
    return {
        "conclusion": "needs_revision",
        "issues": [
            {
                "rule_id": "C001",
                "severity": "error",
                "page": 2,
                "message": "预算表存在不一致",
                "suggestion": "请核对",
            }
        ],
    }


def test_register_font_failure_lists_checked_paths(tmp_path):
    """直接测候选遍历内函数：不依赖进程内字体注册表的当前状态（确定性）。"""
    missing_a = tmp_path / "missing_a.ttc"
    missing_b = tmp_path / "missing_b.ttf"
    with pytest.raises(RuntimeError) as excinfo:
        reporter._register_from_candidates([missing_a, missing_b])
    message = str(excinfo.value)
    assert "missing_a.ttc" in message
    assert "missing_b.ttf" in message
    assert "未找到" in message


@pytest.mark.skipif(not _windows_font(), reason="需要系统中有可用中文字体")
def test_register_font_falls_back_when_explicit_missing(monkeypatch, tmp_path):
    """显式配置路径无效时回退到平台候选，而不是失败。"""
    real = _windows_font()
    settings = get_settings()
    monkeypatch.setattr(settings, "REPORT_FONT_PATH", str(tmp_path / "no-such.ttc"))
    monkeypatch.setattr(
        reporter,
        "_font_candidates",
        lambda: [tmp_path / "no-such.ttc", real],
    )
    assert reporter.register_chinese_font() == "M4Chinese"


@pytest.mark.skipif(not _windows_font(), reason="需要系统中有可用中文字体")
def test_register_font_explicit_config_priority(monkeypatch):
    real = _windows_font()
    settings = get_settings()
    monkeypatch.setattr(settings, "REPORT_FONT_PATH", str(real))
    assert reporter.register_chinese_font() == "M4Chinese"


def test_generate_report_raises_cleanly_without_partial_file(tmp_path, monkeypatch):
    """生成失败时不留半成品：输出与临时文件都不存在（原子写/C14）。"""
    output = tmp_path / "report.pdf"

    def fail_font():
        raise RuntimeError("未找到可用中文字体（测试注入）")

    monkeypatch.setattr(reporter, "register_chinese_font", fail_font)
    with pytest.raises(RuntimeError, match="测试注入"):
        generate_review_report(
            _sample_review(),
            document_name="样例.docx",
            ruleset_name="测试规则集",
            output_path=output,
        )
    assert not output.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_health_reports_report_font_without_leaking_paths():
    from starlette.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    response = client.get("/api/health")
    assert response.status_code == 200
    checks = response.json()["checks"]
    assert isinstance(checks["report_font_available"], bool)
    # 公开接口不泄露宿主字体路径
    assert "C:/Windows" not in response.text
    assert "/usr/share/fonts" not in response.text
