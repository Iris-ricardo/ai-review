from __future__ import annotations

import sys
import threading
from types import SimpleNamespace
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings
from app.services.llm import client as llm_client
from app.services.llm.client import LLMFormatError, LLMUnavailableError, call_json
from app.services.task_runtime import task_scope


@pytest.fixture(autouse=True)
def _authorized_egress(monkeypatch):
    """R08：材料外发需要“部署开关打开 + 任务级授权”，测试显式提供这两者。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "AI_EGRESS_ENABLED", True)
    monkeypatch.setattr(llm_client, "get_settings", lambda: settings)
    with task_scope("llm-client-test", threading.Event(), 60, egress_authorized=True):
        yield


def test_call_json_parses_normal_json(monkeypatch):
    monkeypatch.setattr(llm_client, "_post_chat", lambda messages: '{"ok": true}')

    result = call_json("system", "user")

    assert result == {"ok": True}


def test_call_json_strips_json_fence(monkeypatch):
    monkeypatch.setattr(llm_client, "_post_chat", lambda messages: '```json\n{"ok": true}\n```')

    result = call_json("system", "user")

    assert result == {"ok": True}


def test_call_json_retries_once_on_format_error(monkeypatch):
    seen_messages = []
    responses = iter(["not json", '{"ok": true}'])

    def fake_post(messages):
        seen_messages.append(messages)
        return next(responses)

    monkeypatch.setattr(llm_client, "_post_chat", fake_post)

    result = call_json("system", "user")

    assert result == {"ok": True}
    assert seen_messages[1][-1]["content"] == llm_client.FORMAT_RETRY_MESSAGE


def test_call_json_raises_format_error_after_retry(monkeypatch):
    monkeypatch.setattr(llm_client, "_post_chat", lambda messages: "not json")

    with pytest.raises(LLMFormatError):
        call_json("system", "user")


def test_call_json_retries_network_errors_then_unavailable(monkeypatch):
    attempts = []

    def fake_post(messages):
        attempts.append(messages)
        raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(llm_client, "_post_chat", fake_post)
    monkeypatch.setattr(llm_client.time, "sleep", lambda seconds: None)

    with pytest.raises(LLMUnavailableError):
        call_json("system", "user")

    assert len(attempts) == llm_client.get_settings().LLM_MAX_RETRIES + 1


def test_call_json_rejects_oversized_prompt_before_network(monkeypatch):
    monkeypatch.setattr(
        llm_client,
        "get_settings",
        lambda: SimpleNamespace(LLM_MAX_PROMPT_CHARS=1000, AI_EGRESS_ENABLED=True),
    )
    called = False

    def fake_post(_messages):
        nonlocal called
        called = True
        return '{"ok": true}'

    monkeypatch.setattr(llm_client, "_post_chat", fake_post)
    with pytest.raises(LLMFormatError):
        call_json("system", "x" * 1001)
    assert called is False


def test_post_chat_uses_http_client_with_trust_env_false(monkeypatch):
    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured["create_kwargs"] = kwargs
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))]
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.chat = SimpleNamespace(completions=FakeCompletions())

        def close(self):
            captured["closed"] = True
            captured["http_client"].close()

    monkeypatch.setattr(
        llm_client,
        "get_settings",
        lambda: SimpleNamespace(
            LLM_API_KEY="sk-test",
            LLM_BASE_URL="https://example.test/v1",
            LLM_MODEL="test-model",
            LLM_TIMEOUT=60,
        ),
    )
    monkeypatch.setattr(llm_client, "OpenAI", FakeOpenAI)

    content = llm_client._post_chat([{"role": "user", "content": "hello"}])

    assert content == '{"ok": true}'
    assert captured["http_client"]._trust_env is False
    assert captured["create_kwargs"]["model"] == "test-model"
    assert captured["max_retries"] == 0
    assert "closed" not in captured
    assert captured["create_kwargs"]["response_format"] == {"type": "json_object"}
