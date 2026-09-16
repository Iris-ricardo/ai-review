"""R01 回归测试：样本池生成器的输出目录安全：无关文件内容/哈希不变，--include/--exclude 与零样本
选择不删无关文件或清空目录；无效/危险/无标记非空目录在任何文件操作前被拒绝；来源不明同名文件不
覆盖；失败不破坏上一份有效产物；旧版无标记目录仅在产物可验证时接管，重复发布只替换受管产物。
只用 pytest 临时目录、不触碰真实数据，不导入 generate_samples 主模块，CLI 用子进程隔离执行。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent
CORPUS_DIR = PROJECT_ROOT / "eval" / "corpus"
sys.path.insert(0, str(CORPUS_DIR))

import output_safety as safety  # noqa: E402

KNOWN = {"campus_clean.docx", "layout_margin_hit.docx", "pdf_blank_mid.pdf"}
GENERATOR = CORPUS_DIR / "generate_samples.py"


def _sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(dir_path: Path, name: str, content: str | bytes) -> Path:
    path = dir_path / name
    data = content if isinstance(content, bytes) else content.encode("utf-8")
    path.write_bytes(data)
    return path


def _make_manifest(dir_path: Path, sample_files: list[str]) -> None:
    (dir_path / "manifest.json").write_text(
        json.dumps({
            "generated_at": "corpus-v3",
            "samples": [{"file": name} for name in sample_files],
        }, ensure_ascii=False),
        encoding="utf-8",
    )


# ── 目录归属判定 ────────────────────────────────────────────

def test_classify_states(tmp_path):
    missing = tmp_path / "missing"
    state, old = safety.classify_directory(missing, KNOWN)
    assert state == "missing" and old == []

    empty = tmp_path / "empty"
    empty.mkdir()
    state, old = safety.classify_directory(empty, KNOWN)
    assert state == "empty" and old == []

    foreign = tmp_path / "foreign"
    foreign.mkdir()
    _write(foreign, "note.txt", "user data")
    state, old = safety.classify_directory(foreign, KNOWN)
    assert state == "foreign"


def test_owned_marker_directory(tmp_path):
    owned = tmp_path / "owned"
    owned.mkdir()
    _write(owned, "old.docx", "old managed artifact")
    _write(owned, "manifest.json", "{}")
    _write(owned, "selfcheck.txt", "x")
    safety.write_marker(owned, ["old.docx", "manifest.json", "selfcheck.txt"], "all")
    state, old = safety.classify_directory(owned, KNOWN)
    assert state == "owned"
    assert "old.docx" in old and safety.MANAGED_MARKER not in old


def test_legacy_managed_directory_only_when_fully_known(tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    _write(legacy, "campus_clean.docx", "sample")
    _make_manifest(legacy, ["campus_clean.docx"])
    _write(legacy, "selfcheck.txt", "ok")
    state, old = safety.classify_directory(legacy, KNOWN)
    assert state == "legacy_managed"
    assert set(old) == {"campus_clean.docx", "manifest.json", "selfcheck.txt"}

    # 多一个无关文件 → 不能接管（foreign）
    _write(legacy, "note.txt", "user data")
    state, _ = safety.classify_directory(legacy, KNOWN)
    assert state == "foreign"

    # manifest 声明与磁盘不符 → 不能接管
    odd = tmp_path / "odd"
    odd.mkdir()
    _write(odd, "campus_clean.docx", "sample")
    _make_manifest(odd, ["pdf_blank_mid.pdf"])  # 与磁盘不一致
    state, _ = safety.classify_directory(odd, KNOWN)
    assert state == "foreign"


# ── 预检拒绝（任何文件操作之前） ────────────────────────────

def test_preflight_refuses_foreign_directory(tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    note = _write(foreign, "note.txt", "precious user data")
    before = _sha256(note)
    with pytest.raises(safety.OutputSafetyError):
        safety.preflight(foreign, KNOWN)
    assert _sha256(note) == before
    assert list(foreign.iterdir()) == [note]  # 目录未被动过


def test_preflight_refuses_protected_roots(tmp_path):
    # 真实危险路径只验证“拒绝发生在任何操作前”，不写入
    targets = [
        str(Path.home()),
        str(Path.home() / "Desktop"),
        str(safety.PROJECT_ROOT),
        str(safety.OUTER_ROOT),
        str(Path.cwd().anchor) if Path.cwd().anchor else "/",
    ]
    for target in targets:
        with pytest.raises(safety.OutputSafetyError):
            safety.preflight(target, KNOWN)


def test_preflight_refuses_file_target(tmp_path):
    target = _write(tmp_path, "somefile", "not a dir")
    with pytest.raises(safety.OutputSafetyError):
        safety.preflight(target, KNOWN)


def test_preflight_allows_empty_and_missing(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    resolved, state, old = safety.preflight(empty, KNOWN)
    assert state == "empty" and resolved == empty.resolve() and old == []
    missing = tmp_path / "missing"
    resolved, state, _ = safety.preflight(missing, KNOWN)
    assert state == "missing"


# ── 发布行为 ────────────────────────────────────────────────

def test_publish_replaces_old_owned_keeps_unrelated(tmp_path):
    owned = tmp_path / "owned"
    owned.mkdir()
    _write(owned, "old.docx", b"old")
    _write(owned, "manifest.json", "{}")
    _write(owned, "selfcheck.txt", "x")
    note = _write(owned, "note.txt", "keep me")
    safety.write_marker(owned, ["old.docx", "manifest.json", "selfcheck.txt"], "all")

    stage = safety.create_staging(owned)
    _write(stage, "new.docx", b"new")
    _write(stage, "manifest.json", "{}")
    _write(stage, "selfcheck.txt", "ok")
    safety.publish(
        stage, owned,
        ["old.docx", "manifest.json", "selfcheck.txt"],
        ["new.docx", "manifest.json", "selfcheck.txt"],
        "subset",
    )
    assert not (owned / "old.docx").exists()
    assert (owned / "new.docx").read_bytes() == b"new"
    assert note.read_text(encoding="utf-8") == "keep me"  # 无关文件保留
    marker = safety.read_marker(owned)
    assert marker is not None
    assert "new.docx" in marker["files"]


def test_publish_refuses_unknown_same_name(tmp_path):
    owned = tmp_path / "owned"
    owned.mkdir()
    safety.write_marker(owned, [], "all")
    # 目标位置存在一个“不在旧产物清单”的同名文件（来源不明）
    _write(owned, "new.docx", b"somebody elses file")

    stage = safety.create_staging(owned)
    _write(stage, "new.docx", b"generated")
    _write(stage, "manifest.json", "{}")
    _write(stage, "selfcheck.txt", "ok")
    with pytest.raises(safety.OutputSafetyError):
        safety.publish(
            stage, owned, [], ["new.docx", "manifest.json", "selfcheck.txt"], "all"
        )
    # 拒绝后：外来文件未被覆盖，暂存仍在（由调用方负责清理），旧目录无新文件
    assert (owned / "new.docx").read_bytes() == b"somebody elses file"
    safety.cleanup_staging(stage)


def test_publish_staging_mismatch_aborts(tmp_path):
    owned = tmp_path / "owned"
    owned.mkdir()
    _write(owned, "keep.docx", b"old result")
    safety.write_marker(owned, ["keep.docx"], "all")

    stage = safety.create_staging(owned)
    _write(stage, "a.docx", b"a")  # 与 produced 清单不符
    with pytest.raises(safety.OutputSafetyError):
        safety.publish(stage, owned, ["keep.docx"], ["manifest.json"], "all")
    assert (owned / "keep.docx").read_bytes() == b"old result"
    safety.cleanup_staging(stage)


def test_stale_staging_cleanup_only_prefix_owned(tmp_path):
    owned = tmp_path / "owned"
    owned.mkdir()
    safety.write_marker(owned, [], "all")
    stale = owned / f"{safety.STAGE_PREFIX}crash1"
    stale.mkdir()
    (stale / "partial.docx").write_bytes(b"partial")
    (owned / "userdir").mkdir()  # 非本工具目录
    safety.remove_stale_staging_dirs(owned)
    assert not stale.exists()
    assert (owned / "userdir").is_dir()


def test_repeat_run_semantics(tmp_path):
    """第二次发布只替换旧受管产物，无关文件跨轮次保留。"""
    owned = tmp_path / "owned"
    owned.mkdir()
    note = _write(owned, "note.txt", "still mine")
    _write(owned, "a.docx", b"v1")
    _write(owned, "manifest.json", "{}")
    _write(owned, "selfcheck.txt", "ok")
    safety.write_marker(owned, ["a.docx", "manifest.json", "selfcheck.txt"], "all")

    for iteration in range(2):
        stage = safety.create_staging(owned)
        name = f"a{iteration}.docx"
        _write(stage, name, b"new")
        _write(stage, "manifest.json", "{}")
        _write(stage, "selfcheck.txt", "ok")
        old = list(safety.read_marker(owned).get("files", []))
        safety.publish(
            stage, owned, old,
            [name, "manifest.json", "selfcheck.txt"],
            f"run{iteration}",
        )
        assert note.read_text(encoding="utf-8") == "still mine"
    assert (owned / "a1.docx").exists()
    assert not (owned / "a0.docx").exists()


# ── CLI 行为（子进程隔离，不触发 LibreOffice） ───────────────

def _run_cli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GENERATOR), *args],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def _all_sample_keys() -> list[str]:
    listing = _run_cli(Path.cwd(), "--list")
    assert listing.returncode == 0, listing.stdout + listing.stderr
    keys = []
    for line in listing.stdout.splitlines():
        line = line.strip()
        if line.startswith("[") or not line or line.startswith(("可用", "python")):
            continue
        token = line.split()[0]
        if token.endswith((".docx", ".pdf")):
            continue
        keys.append(token)
    assert keys, f"--list 未解析出样本键：{listing.stdout}"
    return keys


def test_cli_zero_selection_does_not_touch_directory(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    note = _write(out, "note.txt", "user file")
    keys = ",".join(_all_sample_keys())
    proc = _run_cli(out, "--out", str(out), "--exclude", keys)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert note.read_text(encoding="utf-8") == "user file"
    assert [p.name for p in out.iterdir()] == ["note.txt"]


def test_cli_refuses_foreign_directory_without_touching(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    note = _write(out, "note.txt", "precious")
    proc = _run_cli(out, "--out", str(out), "--include", "campus_clean")
    assert proc.returncode != 0
    assert "已拒绝" in proc.stdout or "未通过" in proc.stdout
    assert note.read_text(encoding="utf-8") == "precious"
    assert [p.name for p in out.iterdir()] == ["note.txt"]


def test_cli_refuses_dangerous_directory(tmp_path):
    proc = _run_cli(tmp_path, "--out", str(Path.home()), "--include", "campus_clean")
    assert proc.returncode != 0
    assert "拒绝" in proc.stdout or "未通过" in proc.stdout
