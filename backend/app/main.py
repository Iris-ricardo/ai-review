"""FastAPI 应用入口。"""
from __future__ import annotations

import mimetypes
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

import fitz
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import (
    PROJECT_INSTANCE_FINGERPRINT,
    SOURCE_FINGERPRINT,
    get_settings,
)
from app.core.logging import setup_logging
from app.services import review_store
from app.services.parser.converter import _find_libreoffice

# 待追踪：config.py中的配置加载到settings中，并设置日志记录
settings = get_settings()
setup_logging(settings.DEBUG, settings.LOG_FILE)

# Windows 常通过注册表把 ES 模块文件映射为 ``text/plain``。浏览器
# 会拒绝 PDF.js 的模块 worker，除非它以 JavaScript 类型返回。
mimetypes.add_type("application/javascript", ".mjs", strict=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from app.api import routes
    routes.startup_services()
    try:
        yield   #分界线，之前是启动，之后是关闭
    finally:
        routes.shutdown_services()

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

_rate_lock = threading.Lock()
_rate_events: dict[str, deque[float]] = defaultdict(deque)
_rate_limited_paths = {
    "/api/v1/auth/login",
    "/api/v1/documents",
    "/api/v1/reviews",
    "/api/v1/batches",
    "/api/v1/rulesets/from-guideline",
}


def _access_token(request: Request) -> str:
    # 边界访问令牌与已签名的用户会话是两套相互独立的凭据。
    # ``Authorization: Bearer`` 保留给用户会话使用。
    return request.headers.get("x-access-token", "")


def _rate_limit_key(request: Request) -> str:
    host = request.client.host if request.client else "unknown"
    return f"{host}:{request.url.path}"


def _rate_allowed(request: Request) -> bool:
    limit = max(1, int(settings.RATE_LIMIT_PER_MINUTE))
    now_value = time.monotonic()
    cutoff = now_value - 60
    key = _rate_limit_key(request)
    with _rate_lock:
        events = _rate_events[key]
        while events and events[0] < cutoff:
            events.popleft()
        if len(events) >= limit:
            return False
        events.append(now_value)
    return True


@app.middleware("http")
async def api_security(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/v1") and settings.ACCESS_TOKEN:
        supplied = _access_token(request)
        if not supplied or not secrets.compare_digest(supplied, settings.ACCESS_TOKEN):
            return JSONResponse(
                status_code=401,
                content={"detail": "需要访问凭据"},
                headers={"WWW-Authenticate": "Bearer"},
            )
    if request.method in {"POST", "PUT", "PATCH"} and path in _rate_limited_paths:
        if not _rate_allowed(request):
            return JSONResponse(
                status_code=429,
                content={"detail": "请求过于频繁，请稍后再试"},
                headers={"Retry-After": "60"},
            )
    return await call_next(request)

# CORS —— 允许前端开发服务器跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册 API 路由
from app.api import routes as api_routes
from app.api.auth_routes import router as auth_router
from app.api.routes import router as api_router
from app.api.rule_admin_routes import router as rule_admin_router
app.include_router(auth_router)
app.include_router(api_router)
app.include_router(rule_admin_router)


@app.get("/api/health")
@app.get("/health")
def health_check():
    database_ok = True
    try:
        review_store.list_reviews(1)
    except Exception:
        database_ok = False
    rulesets = api_routes.list_rulesets().get("rulesets", [])
    upload_dir = Path(settings.UPLOAD_DIR).resolve()
    output_dir = Path(settings.OUTPUT_DIR).resolve()
    storage_ok = all(
        path.exists() and os.access(path, os.W_OK)
        for path in (upload_dir, output_dir)
    )
    try:
        fitz.get_tessdata(settings.TESSDATA_PREFIX or None)
        ocr_available = True
    except Exception:
        ocr_available = False
    # B6：中文报告能力——“实际注册成功”才算可用（不是路径存在）；只返回布尔，不泄露路径
    try:
        from app.services.report.reporter import register_chinese_font

        report_font_available = register_chinese_font() != ""
    except Exception:
        report_font_available = False
    core_ready = database_ok and bool(rulesets) and storage_ok
    return {
        "status": "ok" if core_ready else "degraded",
        "version": settings.APP_VERSION,
        "build_id": settings.APP_BUILD_ID,
        "process_id": os.getpid(),
        "instance_fingerprint": PROJECT_INSTANCE_FINGERPRINT,
        "source_fingerprint": SOURCE_FINGERPRINT,
        "checks": {
            "database": database_ok,
            "rulesets": len(rulesets),
            "storage_writable": storage_ok,
            "libreoffice": _find_libreoffice() is not None,
            "ocr_available": ocr_available,
            "report_font_available": report_font_available,
            "llm_configured": bool(settings.LLM_API_KEY),
            "access_control": bool(settings.ACCESS_TOKEN),
            "active_tasks": api_routes.active_task_count(),
            "queue_capacity": settings.TASK_QUEUE_CAPACITY,
        },
    }


# 生产/Docker 构建会把编译后的前端放在后端旁边。
# 先注册 API 路由，避免 SPA 兜底路由将其遮蔽。
_frontend_candidates = [
    Path.cwd() / "frontend" / "dist",
    Path(__file__).resolve().parents[2] / "frontend" / "dist",
]
for _frontend_dir in _frontend_candidates:
    if (_frontend_dir / "index.html").exists():
        app.mount("/", StaticFiles(directory=_frontend_dir, html=True), name="frontend")
        break
