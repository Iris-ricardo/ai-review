"""Bounded, observable OpenAI-compatible JSON client."""
from __future__ import annotations

import json
import logging
import threading
import time
from functools import lru_cache
from typing import Any

import httpx
from openai import OpenAI

from app.core.config import get_settings
from app.services.task_runtime import (
    checkpoint,
    consume_llm_call,
    egress_is_authorized,
    heartbeat,
    remaining_seconds,
)

logger = logging.getLogger(__name__)
PROMPT_VERSION = "formal-review-v2"

# 连通性检查专用提示词：不含任何用户材料，且由本模块硬编码，调用方无法注入内容。
_CONNECTIVITY_SYSTEM = "你是连通性检查器，只输出 JSON。"
_CONNECTIVITY_USER = '请输出 {"ok":true}，不要输出其他内容。'


class LLMUnavailableError(RuntimeError):
    pass


class LLMFormatError(RuntimeError):
    pass


class EgressNotAuthorizedError(LLMUnavailableError):
    """Raised when a material egress is attempted without task-level consent."""


FORMAT_RETRY_MESSAGE = "仅输出合法 JSON，不要输出 Markdown 或其他文字。"
_semaphore_lock = threading.Lock()
_semaphore_size = 0
_semaphore: threading.BoundedSemaphore | None = None


def call_json(system: str, user: str) -> dict:
    """材料外发的唯一出口：部署开关 + **任务级授权**双重校验（R08）。
    每次真实请求（含重试与 JSON 修复）都重新走到这里：发送前策略被关闭 → 立即拒绝，不排队/重试绕过；
    任务没有持久化授权记录（含历史任务）→ 默认拒绝，不推定用户同意。
    """
    settings = get_settings()
    # B5b：部署级外发总开关在统一出口强制检查——即使调用方漏了入口校验，
    # 任何真实外发请求都先被这里拦下（AI_EGRESS_ENABLED=False 时不产生外部请求）。
    if not getattr(settings, "AI_EGRESS_ENABLED", False):
        raise LLMUnavailableError("外部 AI 外发已被部署级策略禁用")
    # R08：任务级授权必须显式打开（见 task_runtime.authorize_egress）
    if not egress_is_authorized():
        raise EgressNotAuthorizedError(
            "该任务未获得材料外发授权（缺少授权记录或未开启 AI），已阻止外发"
        )
    max_chars = max(1000, int(getattr(settings, "LLM_MAX_PROMPT_CHARS", 16000)))
    if len(system) + len(user) > max_chars:
        raise LLMFormatError(
            f"LLM prompt exceeds the {max_chars}-character safety limit"
        )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    content = _call_with_retries(messages)
    parsed = _parse_json(content)
    if parsed is not None:
        return parsed

    repair_messages = [
        *messages,
        {"role": "assistant", "content": content[:4000]},
        {"role": "user", "content": FORMAT_RETRY_MESSAGE},
    ]
    repaired = _call_with_retries(repair_messages)
    parsed = _parse_json(repaired)
    if parsed is None:
        raise LLMFormatError("LLM response is not a valid JSON object")
    return parsed


def probe_connectivity() -> dict:
    """无材料的连通性检查（R08 要求 7）：提示词由本模块硬编码，调用方**无法**传入任何材料。
    不套用文档授权身份、不需要任务级材料授权（不发材料）；但仍受部署级外发总开关约束。
    """
    settings = get_settings()
    if not getattr(settings, "AI_EGRESS_ENABLED", False):
        raise LLMUnavailableError("外部 AI 外发已被部署级策略禁用")
    return _call_json_unchecked(_CONNECTIVITY_SYSTEM, _CONNECTIVITY_USER)


def _call_json_unchecked(system: str, user: str) -> dict:
    max_chars = max(1000, int(getattr(get_settings(), "LLM_MAX_PROMPT_CHARS", 16000)))
    if len(system) + len(user) > max_chars:
        raise LLMFormatError(
            f"LLM prompt exceeds the {max_chars}-character safety limit"
        )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    content = _call_with_retries(messages)
    parsed = _parse_json(content)
    if parsed is None:
        raise LLMFormatError("LLM response is not a valid JSON object")
    return parsed


def _parse_json(content: str) -> dict | None:
    try:
        value = json.loads(_strip_json_fence(content))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _call_with_retries(messages: list[dict[str, str]]) -> str:
    settings = get_settings()
    attempts = max(1, int(getattr(settings, "LLM_MAX_RETRIES", 1)) + 1)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        checkpoint()
        heartbeat(f"llm_attempt_{attempt}")
        try:
            return _post_chat(messages)
        except LLMUnavailableError:
            raise
        except Exception as exc:
            last_error = exc
            logger.warning(
                "LLM request attempt %s/%s failed: %s",
                attempt,
                attempts,
                type(exc).__name__,
            )
            if attempt < attempts:
                _cooperative_backoff(min(2 ** (attempt - 1), 4))
    raise LLMUnavailableError("LLM service unavailable after bounded retries") from last_error


def _post_chat(messages: list[dict[str, str]]) -> str:
    settings = get_settings()
    if not settings.LLM_API_KEY:
        raise LLMUnavailableError("LLM_API_KEY is not configured")
    timeout = remaining_seconds(float(settings.LLM_TIMEOUT))
    if not _acquire_provider_slot(timeout):
        raise LLMUnavailableError(
            "LLM concurrency wait exceeded the active deadline"
        )
    try:
        if not consume_llm_call(int(getattr(settings, "LLM_MAX_CALLS_PER_TASK", 8))):
            raise LLMUnavailableError("LLM call budget exhausted for this review task")
        heartbeat("llm_request_started")
        started = time.monotonic()
        base_client = _client(
            settings.LLM_API_KEY,
            settings.LLM_BASE_URL.rstrip("/"),
            float(settings.LLM_TIMEOUT),
            float(getattr(settings, "LLM_CONNECT_TIMEOUT", 10)),
        )
        client = (
            base_client.with_options(timeout=timeout)
            if hasattr(base_client, "with_options")
            else base_client
        )
        payload: dict[str, Any] = {
            "model": settings.LLM_MODEL,
            "messages": messages,
            "temperature": getattr(settings, "LLM_TEMPERATURE", 0),
            "max_tokens": getattr(settings, "LLM_MAX_OUTPUT_TOKENS", 2000),
            "response_format": {"type": "json_object"},
        }
        response = client.chat.completions.create(**payload)
        elapsed = time.monotonic() - started
        heartbeat("llm_response_received")
        usage = getattr(response, "usage", None)
        logger.info(
            "LLM response model=%s request_id=%s latency=%.2fs prompt_tokens=%s completion_tokens=%s",
            settings.LLM_MODEL,
            getattr(response, "id", ""),
            elapsed,
            getattr(usage, "prompt_tokens", None),
            getattr(usage, "completion_tokens", None),
        )
    finally:
        _provider_semaphore().release()

    try:
        content = response.choices[0].message.content or ""
    except (AttributeError, IndexError, TypeError) as exc:
        raise LLMUnavailableError(
            "LLM response missing choices[0].message.content"
        ) from exc
    if not content.strip():
        raise LLMFormatError("LLM response content is empty")
    return content


@lru_cache(maxsize=4)
def _client(
    api_key: str,
    base_url: str,
    read_timeout: float,
    connect_timeout: float,
) -> OpenAI:
    timeout = httpx.Timeout(
        connect=max(1.0, connect_timeout),
        read=max(1.0, read_timeout),
        write=max(1.0, read_timeout),
        pool=max(1.0, connect_timeout),
    )
    http_client = httpx.Client(
        trust_env=False,
        timeout=timeout,
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    )
    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=0,
        http_client=http_client,
    )


def _provider_semaphore() -> threading.BoundedSemaphore:
    global _semaphore, _semaphore_size
    requested = max(1, int(getattr(get_settings(), "LLM_MAX_CONCURRENCY", 1)))
    with _semaphore_lock:
        if _semaphore is None or _semaphore_size != requested:
            _semaphore = threading.BoundedSemaphore(requested)
            _semaphore_size = requested
        return _semaphore


def _acquire_provider_slot(timeout: float) -> bool:
    semaphore = _provider_semaphore()
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        checkpoint()
        remaining = deadline - time.monotonic()
        if semaphore.acquire(timeout=min(0.5, max(0.01, remaining))):
            return True
    return False


def _cooperative_backoff(seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        checkpoint()
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))


def _strip_json_fence(content: str) -> str:
    text = content.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()
