"""E9: sensitive никогда не уходит на бесплатные провайдеры (Gemini/Groq)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.config import get_settings
from aria.core.privacy import contains_sensitive
from aria.db import models as m
from aria.db.base import session_scope
from aria.llm.providers.base import LlmProvider, LlmResponse
from aria.llm.router import ProviderRouter, ProviderUnavailable
from aria.routers import chat as chat_mod

USAGE = {"prompt_tokens": 100, "completion_tokens": 10}


class FakeProvider(LlmProvider):
    def __init__(self, provider_id: str, provider_class: str, text: str):
        self.provider_id = provider_id
        self.provider_class = provider_class
        self.model = provider_id
        self.text = text
        self.calls = 0

    async def check_connectivity(self, timeout_sec):
        return True

    async def chat(self, messages, tools, timeout_sec):
        self.calls += 1
        return LlmResponse(text=self.text, usage=dict(USAGE))


def _router(*providers):
    router = ProviderRouter()
    for provider in providers:
        router.register(provider)
    return router


@pytest.mark.anyio
async def test_sensitive_uses_deepseek_not_gemini():
    gemini = FakeProvider("gemini-flash", "free_tier_reasoning", "gemini")
    deepseek = FakeProvider("deepseek-chat", "standard_reasoning", "deepseek")
    router = _router(gemini, deepseek)

    result = await router.route_chat("free_tier_reasoning", [], [], sensitive=True)

    assert result.response.text == "deepseek" and deepseek.calls == 1 and gemini.calls == 0


@pytest.mark.anyio
async def test_sensitive_fails_when_only_free_providers():
    gemini = FakeProvider("gemini-flash", "free_tier_reasoning", "gemini")
    router = _router(gemini)

    with pytest.raises(ProviderUnavailable):
        await router.route_chat("free_tier_reasoning", [], [], sensitive=True)

    assert gemini.calls == 0


@pytest.mark.anyio
async def test_sensitive_ignores_pinned_free_provider():
    gemini = FakeProvider("gemini-flash", "free_tier_reasoning", "gemini")
    deepseek = FakeProvider("deepseek-chat", "standard_reasoning", "deepseek")
    router = _router(gemini, deepseek)

    result = await router.route_chat(
        "free_tier_reasoning", [], [], sensitive=True, prefer_provider_id="gemini-flash"
    )

    assert result.response.text == "deepseek" and gemini.calls == 0


@pytest.mark.anyio
async def test_sensitive_blocked_by_budget(monkeypatch):
    deepseek = FakeProvider("deepseek-chat", "standard_reasoning", "deepseek")
    router = _router(deepseek)
    monkeypatch.setattr(get_settings(), "budget_daily_usd", 0.01)
    monkeypatch.setattr(get_settings(), "budget_weekly_usd", 0.0)
    with session_scope() as db:
        db.add(
            m.LlmUsage(
                provider_id="deepseek-chat",
                model="deepseek-flash",
                purpose="agent",
                prompt_total=1,
                completion=1,
                cost_usd=0.02,
                tier="off",
                created_at=datetime.now(timezone.utc),
            )
        )

    with pytest.raises(ProviderUnavailable):
        await router.route_chat("premium_reasoning", [], [], sensitive=True)
    assert deepseek.calls == 0


@pytest.mark.anyio
async def test_non_sensitive_still_uses_free_tier():
    gemini = FakeProvider("gemini-flash", "free_tier_reasoning", "gemini")
    deepseek = FakeProvider("deepseek-chat", "standard_reasoning", "deepseek")
    router = _router(gemini, deepseek)

    result = await router.route_chat("free_tier_reasoning", [], [])

    assert result.response.text == "gemini" and gemini.calls == 1 and deepseek.calls == 0


def _client(*providers) -> TestClient:
    app = FastAPI()
    app.include_router(chat_mod.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    app.state.router = _router(*providers)
    return TestClient(app)


def test_chat_secret_auto_sensitive():
    gemini = FakeProvider("gemini-flash", "free_tier_reasoning", "gemini")
    deepseek = FakeProvider("deepseek-chat", "standard_reasoning", "deepseek")
    client = _client(gemini, deepseek)
    sid = client.post("/chat/sessions", json={}).json()["session_id"]

    body = client.post(
        f"/chat/sessions/{sid}/send", json={"content": "мой ключ sk-abcdefgh12345678"}
    ).json()

    assert body["assistant"]["content"] == "deepseek"
    assert gemini.calls == 0 and deepseek.calls == 1


def test_chat_stream_sensitive_falls_back_to_non_stream():
    gemini = FakeProvider("gemini-flash", "free_tier_reasoning", "gemini")
    deepseek = FakeProvider("deepseek-chat", "standard_reasoning", "deepseek")
    client = _client(gemini, deepseek)
    sid = client.post("/chat/sessions", json={}).json()["session_id"]

    response = client.post(
        f"/chat/sessions/{sid}/send", json={"content": "пароль user@example.com", "stream": True}
    )

    assert response.status_code == 200 and "deepseek" in response.text
    assert gemini.calls == 0


def test_contains_sensitive_patterns():
    assert contains_sensitive("sk-abcdefgh12345678") is True
    assert contains_sensitive("напиши на user@example.com") is True
    assert contains_sensitive("обычный текст без секретов") is False


@pytest.mark.anyio
async def test_sensitive_chat_class_blocked_by_budget(monkeypatch):
    deepseek = FakeProvider("deepseek-chat", "standard_reasoning", "deepseek")
    router = _router(deepseek)
    monkeypatch.setattr(get_settings(), "budget_daily_usd", 0.01)
    monkeypatch.setattr(get_settings(), "budget_weekly_usd", 0.0)
    with session_scope() as db:
        db.add(
            m.LlmUsage(
                provider_id="deepseek-chat", model="deepseek-flash", purpose="agent",
                prompt_total=1, completion=1, cost_usd=0.02, tier="off",
                created_at=datetime.now(timezone.utc),
            )
        )

    with pytest.raises(ProviderUnavailable):
        await router.route_chat("free_tier_reasoning", [], [], sensitive=True)
    assert deepseek.calls == 0
