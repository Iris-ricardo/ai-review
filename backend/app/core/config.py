"""
Application configuration loaded from environment variables.
"""
import hashlib
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_INSTANCE_FINGERPRINT = hashlib.sha256(
    Path(__file__).resolve().parents[3].as_posix().casefold().encode("utf-8")
).hexdigest()[:16]


def _source_fingerprint() -> str:
    digest = hashlib.sha256()
    app_dir = Path(__file__).resolve().parents[1]
    for path in sorted(app_dir.rglob("*.py")):
        digest.update(path.relative_to(app_dir).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


SOURCE_FINGERPRINT = _source_fingerprint()


class Settings(BaseSettings):
    """Application settings loaded from .env file and environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── App ──────────────────────────────────────────────
    APP_NAME: str = "AI 项目申报书智能形式审查系统"
    APP_VERSION: str = "1.4.0"
    APP_BUILD_ID: str = "20260721-strict-approval"
    DEBUG: bool = False
    ACCESS_TOKEN: str = ""
    ADMIN_TOKEN: str = ""
    AUTH_SECRET: str = ""
    AUTH_REQUIRED: bool = True
    RATE_LIMIT_PER_MINUTE: int = 120
    # 登录接口按“用户名”维度的滑动窗口限速（B5a；另有 IP+路径全局限速）
    LOGIN_USER_RATE_LIMIT_PER_MINUTE: int = 20
    # 空库账号初始化（B7b / R07）：生产默认**不创建**演示账号，演示/比赛环境须显式设 true。
    # 提供了 AUTH_BOOTSTRAP_ADMIN_PASSWORD 时创建单个超管并强制首次改密；两者都没有则不创建账号并明确告警（不回落默认口令）。
    AUTH_CREATE_DEMO_USERS: bool = False
    AUTH_FORCE_DEMO_PASSWORD_CHANGE: bool = True
    AUTH_BOOTSTRAP_ADMIN_USERNAME: str = "admin"
    AUTH_BOOTSTRAP_ADMIN_PASSWORD: str = ""
    LOG_FILE: str = ""

    # ── Database ──────────────────────────────────────────
    DATABASE_URL: str = "sqlite:///./review.db"
    DATABASE_ECHO: bool = False

    # ── Durable task execution ────────────────────────────────
    TASK_MAX_WORKERS: int = 2
    TASK_QUEUE_CAPACITY: int = 64
    TASK_TIMEOUT_SECONDS: int = 900
    RULE_TIMEOUT_SECONDS: int = 180
    TASK_HEARTBEAT_TIMEOUT_SECONDS: int = 180
    DATA_RETENTION_DAYS: int = 30

    # ── LLM (OpenAI-compatible) ───────────────────────────
    LLM_BASE_URL: str = "https://api.moonshot.cn/v1"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "kimi-k3"
    # 部署级外发总开关（B5b / R08）：默认 False = 全局禁止材料外发，
    # 优先于用户勾选与 LLM_API_KEY；由 call_json 统一出口强制检查。
    # 即使显式开启，任务仍必须持有持久化的用户授权记录才会真正发送材料。
    AI_EGRESS_ENABLED: bool = False
    LLM_TIMEOUT: int = 60
    LLM_CONNECT_TIMEOUT: int = 10
    LLM_MAX_RETRIES: int = 1
    LLM_MAX_CONCURRENCY: int = 1
    LLM_MAX_OUTPUT_TOKENS: int = 2000
    LLM_MAX_PROMPT_CHARS: int = 16000
    LLM_MAX_CALLS_PER_TASK: int = 8
    LLM_TEMPERATURE: float = 0

    # ── File Storage ──────────────────────────────────────
    UPLOAD_DIR: str = "./uploads"
    OUTPUT_DIR: str = "./outputs"
    MAX_UPLOAD_SIZE_MB: int = 50
    MIN_EXTRACTED_TEXT_CHARS: int = 80
    SOFFICE_PATH: str = ""
    OCR_ENABLED: bool = True
    OCR_LANGUAGE: str = "chi_sim+eng"
    OCR_DPI: int = 150
    OCR_MIN_PAGE_CHARS: int = 20
    OCR_MAX_PAGES: int = 30
    TESSDATA_PREFIX: str = ""

    # ── Rules ─────────────────────────────────────────────
    RULES_DIR: str = "../rules"

    # ── 中文报告字体（B6） ─────────────────────────────────
    # 显式指定报告 PDF 中文字体路径；为空时按平台候选查找（Windows/Linux）。
    REPORT_FONT_PATH: str = ""


@lru_cache()
def get_settings() -> Settings:
    return Settings()
