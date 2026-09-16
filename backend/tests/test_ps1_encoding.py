"""打包/安装脚本的编码契约（Windows PowerShell 5.1 兼容性）。

背景：系统自带的 Windows PowerShell 5.1 在没有 BOM 时按 ANSI 代码页解码 `.ps1`，
文件里的中文注释/字符串会变成乱码并导致解析失败——例如 `package_project.ps1` 会报
`Missing closing ')' in subexpression`，`setup_windows.ps1` 里的中文路径也会被读成乱码。
因此：**含非 ASCII 字节的 .ps1 必须带 UTF-8 BOM**。

`.bat` 由 cmd.exe 以 OEM 代码页读取，无法可靠携带 UTF-8，因此必须保持纯 ASCII
（中文只出现在文件名里，脚本内容本身是 ASCII）。

这两条契约是字节级的，与操作系统和已安装工具无关，可在任何平台运行。
"""
from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SKIP_PARTS = {
    ".venv", ".tools", "node_modules", "__pycache__", "dist", "release",
    "results", "output", "templates", "uploads", "outputs", ".pytest_cache",
}
UTF8_BOM = b"\xef\xbb\xbf"


def _iter_files(suffix: str) -> list[Path]:
    found: list[Path] = []
    for path in PROJECT_ROOT.rglob(f"*{suffix}"):
        if not path.is_file():
            continue
        if any(part in SKIP_PARTS for part in path.relative_to(PROJECT_ROOT).parts):
            continue
        found.append(path)
    return sorted(found)


def test_ps1_with_non_ascii_has_utf8_bom():
    checked = 0
    for path in _iter_files(".ps1"):
        raw = path.read_bytes()
        body = raw[3:] if raw.startswith(UTF8_BOM) else raw
        if not any(byte > 127 for byte in body):
            continue
        checked += 1
        assert raw.startswith(UTF8_BOM), (
            f"{path.relative_to(PROJECT_ROOT)} 含非 ASCII 字节但缺少 UTF-8 BOM；"
            "Windows PowerShell 5.1 会按 ANSI 解码并解析失败"
        )
    # 至少 setup_windows.ps1 与 package_project.ps1 属于这一类，避免测试变成空转
    assert checked >= 2, f"只检查到 {checked} 个含非 ASCII 的 .ps1，脚本集合可能被改动"


def test_bat_files_are_ascii_only():
    bats = _iter_files(".bat")
    assert bats, "未找到任何 .bat 启动脚本"
    for path in bats:
        raw = path.read_bytes()
        offenders = [index for index, byte in enumerate(raw) if byte > 127]
        assert not offenders, (
            f"{path.relative_to(PROJECT_ROOT)} 含非 ASCII 字节（偏移 {offenders[:5]}）；"
            "cmd.exe 按 OEM 代码页读取，会显示乱码"
        )


def test_packaging_script_handles_non_ascii_output_path():
    """打包脚本必须对非 ASCII 输出路径有回退处理（tar.exe 处理不了中文路径）。"""
    script = PROJECT_ROOT / "package_project.ps1"
    if not script.exists():
        return  # 打包脚本不参与容器/精简包部署时跳过
    text = script.read_text(encoding="utf-8-sig")
    assert "Compress-Archive" in text, "打包脚本缺少 Compress-Archive 回退"
    assert "asciiPath" in text or "\\x00-\\x7F" in text, "打包脚本未检测非 ASCII 输出路径"
