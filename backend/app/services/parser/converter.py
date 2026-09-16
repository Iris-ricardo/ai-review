"""DOCX → PDF converter using LibreOffice headless.

All documents are unified to PDF first (DOCX → LibreOffice → PDFParser) so that page-accurate
bbox data stays consistent for annotation overlay and PDF preview highlighting."""
from __future__ import annotations

import logging
import os
import atexit
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from app.core.config import get_settings

logger = logging.getLogger(__name__)
_profiles_registered: set[Path] = set()
_profiles_lock = threading.Lock()

# Timeout for LibreOffice conversion (seconds)
CONVERSION_TIMEOUT = 60


def convert_docx_to_pdf(docx_path: str | Path) -> Path:
    """Convert a DOCX file to PDF via LibreOffice headless; returns the PDF path
    next to the source. Raises FileNotFoundError (missing input), RuntimeError
    (LibreOffice missing or conversion failed) or subprocess.TimeoutExpired.
    """
    docx_path = Path(docx_path).resolve()
    if not docx_path.exists():
        raise FileNotFoundError(f"DOCX file not found: {docx_path}")

    if docx_path.suffix.lower() not in (".docx", ".doc"):
        raise ValueError(f"Expected .docx or .doc file, got: {docx_path.suffix}")

    # Find LibreOffice executable
    soffice = _find_libreoffice()
    if soffice is None:
        raise RuntimeError(
            "LibreOffice not found. Install it with:\n"
            "  Ubuntu/Debian: sudo apt-get install libreoffice\n"
            "  macOS:          brew install libreoffice\n"
            "  Windows:        choco install libreoffice\n"
            "Ensure 'soffice' is on PATH."
        )

    output_dir = docx_path.parent
    logger.info(f"Converting {docx_path.name} → PDF via LibreOffice ...")
    start = time.monotonic()

    # One isolated profile per API/worker process: avoids first-start UI and
    # cross-process profile locks without paying init cost for every conversion.
    profile_dir = Path(tempfile.gettempdir()) / (
        f"ai-proposal-lo-profile-{os.getpid()}-{threading.get_ident()}"
    )
    profile_dir.mkdir(parents=True, exist_ok=True)
    with _profiles_lock:
        if profile_dir not in _profiles_registered:
            atexit.register(shutil.rmtree, profile_dir, ignore_errors=True)
            _profiles_registered.add(profile_dir)
    profile_uri = profile_dir.resolve().as_uri()
    cmd = [
        soffice,
        f"-env:UserInstallation={profile_uri}",
        "--headless",
        "--nologo",
        "--nodefault",
        "--nolockcheck",
        "--nofirststartwizard",
        "--convert-to", "pdf",
        "--outdir", str(output_dir),
        str(docx_path),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CONVERSION_TIMEOUT,
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW")
                else 0
            ),
        )
    except subprocess.TimeoutExpired:
        logger.error(f"LibreOffice conversion timed out after {CONVERSION_TIMEOUT}s")
        raise

    elapsed = time.monotonic() - start
    logger.info(f"LibreOffice stdout: {result.stdout.strip()}")
    if result.stderr:
        logger.warning(f"LibreOffice stderr: {result.stderr.strip()}")

    if result.returncode != 0:
        raise RuntimeError(
            f"LibreOffice conversion failed (rc={result.returncode}): "
            f"{result.stderr.strip()}"
        )

    # LibreOffice names the output <stem>.pdf in the output directory
    pdf_path = output_dir / f"{docx_path.stem}.pdf"
    if not pdf_path.exists():
        # Sometimes LibreOffice uses the full original filename + .pdf
        alt_path = output_dir / f"{docx_path.name}.pdf"
        if alt_path.exists():
            pdf_path = alt_path
        else:
            raise RuntimeError(
                f"PDF output not found at {pdf_path} or {alt_path}. "
                f"LibreOffice stdout: {result.stdout.strip()}"
            )

    logger.info(f"Conversion complete in {elapsed:.1f}s: {pdf_path.name}")
    return pdf_path


def _find_libreoffice() -> str | None:
    """Locate the LibreOffice executable: SOFFICE_PATH env override, then
    ``soffice`` / ``libreoffice`` on PATH, then common Windows install paths;
    returns None if not found.
    """
    # 1. Explicit environment variable override
    env_path = os.environ.get("SOFFICE_PATH", "") or get_settings().SOFFICE_PATH
    if env_path and Path(env_path).exists():
        logger.debug(f"Using LibreOffice from SOFFICE_PATH: {env_path}")
        return env_path

    # 2. Look on PATH
    candidates = ["soffice", "libreoffice"]
    for name in candidates:
        path = shutil.which(name)
        if path:
            logger.debug(f"Found LibreOffice at: {path}")
            return path

    # 3. Windows: check common install paths
    if os.name == "nt":
        win_paths = [
            str(Path(__file__).resolve().parents[4] / ".tools" / "LibreOffice" / "program" / "soffice.exe"),
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ]
        for p in win_paths:
            if Path(p).exists():
                logger.debug(f"Found LibreOffice at: {p}")
                return p

    return None
