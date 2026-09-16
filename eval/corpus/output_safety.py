"""样本池输出目录安全管理（R01）：只做目录归属判断 / 危险路径拒绝 / 暂存构建 / 原子发布，不含生成、解析、规则或环境配置逻辑，可被测试安全导入。

只有“能证明属于本生成器”的目录才会被清理：含受管标记（.sample_pool_managed.json 且 tool 字段匹配）→ owned；无标记但内容全部可验证为本工具已知产物（旧版目录）→ legacy_managed，仅允许接管一次并补写标记；不存在或空目录 → 由本工具创建/接管；无标记且非空、存在任何无法证明属本工具的文件/子目录 → foreign，一律拒绝、绝不删除任何内容。
危险目录（磁盘根、用户主目录、桌面、项目根、外层工作区根、真实业务数据目录）在解析真实路径后一律拒绝，且拒绝发生在任何文件操作之前；生成流程先写入同卷暂存子目录，全部成功后才发布，失败只清理暂存目录，上一份有效产物原样保留。"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

MANAGED_TOOL = "qingyuanbei-sample-pool"
MANAGED_MARKER = ".sample_pool_managed.json"
MARKER_SCHEMA = 1
STAGE_PREFIX = ".sample-pool-stage-"
ALWAYS_FILES = ("manifest.json", "selfcheck.txt")

# 本模块位于 eval/corpus/output_safety.py
# parents[0]=corpus parents[1]=eval parents[2]=项目根 parents[3]=外层工作区
_HERE = Path(__file__).resolve()
PROJECT_ROOT = _HERE.parents[2]
OUTER_ROOT = _HERE.parents[3]


class OutputSafetyError(Exception):
    """输出目录安全检查/发布失败。message 面向用户。"""


def managed_marker_path(directory: Path) -> Path:
    return Path(directory) / MANAGED_MARKER


def read_marker(directory: Path) -> dict | None:
    path = managed_marker_path(directory)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("tool") != MANAGED_TOOL or data.get("schema") != MARKER_SCHEMA:
        return None
    return data


def write_marker(
    directory: Path,
    files: list[str],
    selection: str,
    status: str = "ok",
    note: str = "",
) -> None:
    payload = {
        "tool": MANAGED_TOOL,
        "schema": MARKER_SCHEMA,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "selection": selection,
        "status": status,
        "note": note,
        "files": sorted(files),
    }
    path = managed_marker_path(directory)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _is_root(path: Path) -> bool:
    return path == path.parent and bool(path.anchor)


def protected_reasons() -> dict[Path, str]:
    """受保护目录（真实路径 → 原因）。拒绝发生在任何文件操作前。"""
    home = Path.home()
    backend = PROJECT_ROOT / "backend"
    candidates: list[tuple[Path, str]] = [
        (PROJECT_ROOT, "项目根目录"),
        (OUTER_ROOT, "外层工作区根目录（快照与交付文档所在）"),
        (home, "用户主目录"),
        (home / "Desktop", "桌面目录"),
        (backend / "review.db", "真实数据库文件（--out 必须是目录）"),
        (backend / "uploads", "正式上传数据目录"),
        (backend / "outputs", "正式输出数据目录"),
    ]
    reasons: dict[Path, str] = {}
    for path, reason in candidates:
        reasons[path.resolve()] = reason
    return reasons


def resolve_and_check(arg: str | Path) -> Path:
    """把 --out 解析成真实路径并做第一道保护检查（不产生任何文件操作）。
    Raises: OutputSafetyError —— 危险目录 / 不是目录 / 无法解析。
    """
    if not str(arg).strip():
        raise OutputSafetyError("--out 为空")
    resolved = Path(arg).expanduser().resolve(strict=False)
    if _is_root(resolved):
        raise OutputSafetyError(f"拒绝把磁盘根目录作为输出目录：{resolved}")
    reason = protected_reasons().get(resolved)
    if reason:
        raise OutputSafetyError(f"拒绝把{reason}作为输出目录：{resolved}")
    if resolved.exists() and not resolved.is_dir():
        raise OutputSafetyError(f"--out 指向的不是目录：{resolved}")
    return resolved


def _json_samples_files(directory: Path) -> set[str] | None:
    """读取旧版 manifest.json，返回其声明的样本文件名集合；无法解析返回 None。"""
    path = directory / "manifest.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    samples = data.get("samples")
    if not isinstance(samples, list):
        return None
    names = {item.get("file", "") for item in samples if isinstance(item, dict)}
    if not names or any(not name for name in names):
        return None
    return names


def classify_directory(
    resolved: Path,
    known_files: set[str],
) -> tuple[str, list[str]]:
    """返回 (state, previous_owned_files)；state ∈ {"missing", "empty", "owned", "legacy_managed", "foreign", "invalid"}。
    previous_owned_files：本次允许替换/清理的、能证明属于本工具的旧产物。
    """
    if not resolved.exists():
        return "missing", []
    if not resolved.is_dir():
        return "invalid", []
    marker = read_marker(resolved)
    if marker is not None:
        old = list(marker.get("files", []))
        # 标记里应包含 manifest/selfcheck，兼容早期缺失的情况
        for name in ALWAYS_FILES:
            if (resolved / name).is_file() and name not in old:
                old.append(name)
        return "owned", old

    entries = list(resolved.iterdir())
    if not entries:
        return "empty", []

    # 无标记：只有全部内容可验证为本工具产物时才允许接管（旧版目录迁移）。
    subdirs = [e for e in entries if not e.is_file()]
    if subdirs:
        return "foreign", []
    files = {e.name for e in entries if e.is_file()}
    declared = _json_samples_files(resolved)
    if declared is None:
        return "foreign", []
    sample_files = files - set(ALWAYS_FILES)
    if not sample_files:
        return "foreign", []
    if not sample_files <= known_files:
        return "foreign", []
    if declared != sample_files:
        return "foreign", []
    if (resolved / "selfcheck.txt").is_file():
        always_present = {
            name for name in ALWAYS_FILES if (resolved / name).is_file()
        }
        if files != (sample_files | always_present):
            return "foreign", []
    return "legacy_managed", sorted(files)


def preflight(
    arg: str | Path,
    known_files: set[str],
) -> tuple[Path, str, list[str]]:
    """输出目录预检（只读）：返回 (真实路径, state, 旧产物清单)；写盘前必须调用。
    Raises: OutputSafetyError —— 危险目录 / foreign 目录（一律拒绝且不删任何文件） / 非目录。
    """
    resolved = resolve_and_check(arg)
    state, old = classify_directory(resolved, known_files)
    if state == "invalid":
        raise OutputSafetyError(f"--out 指向的不是目录：{resolved}")
    if state == "foreign":
        hint = ""
        if any(
            child.is_dir() and child.name.startswith(STAGE_PREFIX)
            for child in resolved.iterdir()
        ):
            hint = (
                "\n提示：目录内有残留暂存子目录 .sample-pool-stage-*"
                "（疑似本工具上次生成中断遗留）；确认其中没有你要保留的文件后，"
                "可删除该暂存子目录再重试。"
            )
        raise OutputSafetyError(
            "输出目录非空且没有本工具的受管标记，为免误删已拒绝："
            f"{resolved}\n"
            "请改用一个空目录或由本工具新建的目录（--out 指向不存在的路径即可）；"
            "本工具不会删除该目录中的任何文件。"
            f"{hint}"
        )
    return resolved, state, old


def create_staging(resolved: Path) -> Path:
    """在目标目录内创建同卷暂存子目录（保证 os.replace 原子性）。"""
    resolved.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=STAGE_PREFIX, dir=str(resolved)))


def cleanup_staging(staged: Path) -> None:
    shutil.rmtree(staged, ignore_errors=True)


def remove_stale_staging_dirs(resolved: Path) -> None:
    """清理本工具历史崩溃遗留的暂存目录（仅限 owned 目录、且名称前缀匹配）。"""
    if not resolved.is_dir():
        return
    for child in resolved.iterdir():
        if child.is_dir() and child.name.startswith(STAGE_PREFIX):
            shutil.rmtree(child, ignore_errors=True)


def publish(
    staged: Path,
    resolved: Path,
    old_files: list[str],
    produced_files: list[str],
    selection: str,
    *,
    status: str = "ok",
    note: str = "",
) -> None:
    """把暂存目录中的产物发布到目标目录：同名校验 → 只删除“旧受管产物” → os.replace 逐个原子发布 → 写受管标记 → 删暂存目录。
    任一失败抛出 OutputSafetyError：发布前删除只发生在校验通过后，旧产物仅可能在 os.replace 中断时被部分删除，需重跑恢复（见 README“失败恢复”说明）。"""
    staged_files = {p.name for p in staged.iterdir() if p.is_file()}
    produced = set(produced_files)
    if staged_files != produced:
        raise OutputSafetyError(
            "暂存内容与期望不一致，已中止发布："
            f"暂存={sorted(staged_files)} 期望={sorted(produced)}"
        )
    old_set = set(old_files)
    for name in produced:
        dest = resolved / name
        if dest.exists() and name not in old_set:
            raise OutputSafetyError(
                f"目标位置存在同名但非本工具旧产物的文件，拒绝覆盖：{dest}"
            )
    # 只删除旧受管产物；目录内其它文件（无关文件）一律保留。
    for name in old_set:
        candidate = resolved / name
        if candidate.is_file():
            try:
                candidate.unlink()
            except OSError as exc:
                raise OutputSafetyError(f"清理旧产物失败：{candidate} ({exc})") from exc
    try:
        for name in produced:
            os.replace(staged / name, resolved / name)
    except OSError as exc:
        raise OutputSafetyError(f"发布失败（旧产物可能部分保留，请重跑恢复）：{exc}") from exc
    write_marker(resolved, sorted(produced), selection, status=status, note=note)
    cleanup_staging(staged)


def ensure_owned_marker_for_tests(directory: Path, files: list[str]) -> None:
    """测试辅助：给一个已有目录补上受管标记，模拟“自有目录”。"""
    write_marker(directory, files, selection="test")
