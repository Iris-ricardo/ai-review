"""缺少 LibreOffice 时的错误契约：要给可操作的提示，而不是笼统的 500。

背景：DOCX 审查链路是 DOCX → LibreOffice → PDFParser（见 services/parser/docx_parser.py）。
CI 的 Windows Server 镜像、以及没装 LibreOffice 的开发机上转换会失败；此前接口一律 500，
使用者分不清是“环境缺依赖”还是“文档本身有问题”。这里锁住三件事：
1. 转换函数抛的是专门类型 ``LibreOfficeNotFound``（便于上层区分）；
2. 下载接口返回 503 且提示里写了怎么装；
3. 审查任务的失败原因同样写明缺依赖，界面上直接可见。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from docx import Document
from fastapi.testclient import TestClient

from app.main import app
from app.services.parser import converter
from app.services.parser.converter import LibreOfficeNotFound, convert_docx_to_pdf

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLEAN_DOCX = PROJECT_ROOT / "eval" / "samples" / "sample_proposal.docx"

client = TestClient(app)


@pytest.fixture(autouse=True)
def hide_libreoffice(monkeypatch):
    """让查找函数恒为 None，等价于“这台机器没装 LibreOffice”。"""
    monkeypatch.setattr(converter, "_find_libreoffice", lambda: None)


def test_conversion_raises_dedicated_exception():
    with pytest.raises(LibreOfficeNotFound) as excinfo:
        convert_docx_to_pdf(CLEAN_DOCX)
    # 原有的英文安装指引保留（日志/脚本里有引用），只是异常类型变具体
    assert "LibreOffice not found" in str(excinfo.value)


def _upload_unique_docx(tmp_path: Path) -> str:
    """上传一份“字节唯一”的 DOCX。

    上传接口按 sha256 去重，直接传样本会命中别的用例已经转换过的旧文档，
    那样就拿不到“缺依赖”的分支了（只改文档属性，正文不变）。
    """
    document = Document(str(CLEAN_DOCX))
    document.core_properties.title = f"missing-libreoffice-{tmp_path.name}"
    path = tmp_path / "缺少依赖时的材料.docx"
    document.save(str(path))
    with open(path, "rb") as handle:
        response = client.post("/api/v1/documents", files={"file": (path.name, handle)})
    assert response.status_code == 200, response.text
    return response.json()["document_id"]


def test_download_reports_missing_dependency_as_503(tmp_path):
    document_id = _upload_unique_docx(tmp_path)
    response = client.get(f"/api/v1/documents/{document_id}/file")
    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert "LibreOffice" in detail
    assert "choco install" in detail or "SOFFICE_PATH" in detail


def test_review_failure_message_is_actionable(tmp_path):
    document_id = _upload_unique_docx(tmp_path)
    created = client.post(
        "/api/v1/reviews",
        data={
            "document_id": document_id,
            "ruleset_id": "campus",
            "use_ai": "false",
            "privacy_consent": "false",
        },
    )
    assert created.status_code == 200, created.text
    review_id = created.json()["review_id"]

    deadline = time.monotonic() + 30
    data: dict = {}
    while time.monotonic() < deadline:
        data = client.get(f"/api/v1/reviews/{review_id}").json()
        if data.get("status") in {"done", "failed"}:
            break
        time.sleep(0.05)

    assert data.get("status") == "failed", data
    error = data.get("error") or ""
    assert "LibreOffice" in error, data
    # 必须给出“怎么装/怎么指定路径”，而不是把英文异常原样抛给使用者
    assert "SOFFICE_PATH" in error, data
    assert "PDF 材料不受影响" in error, data
