"""F-005: квоты free-tier по проектам, pre-flight 413/429 → cooldown без трёх ретраев."""
from __future__ import annotations

import httpx
import pytest
from aria.llm import quota
from aria.llm.providers.base import LlmProvider, LlmResponse
from aria.llm.router import ProviderRouter


class FakeLlm(LlmProvider):
    def __init__(self, provider_id: str, model: str, status: int | None = None):
        self.provider_id = provider_id
        self.provider_class = "free_tier_reasoning"
        self.model = model
        self.status = status
        self.calls = 0

    async def check_connectivity(self, timeout_sec):
        return True

    async def chat(self, messages, tools, timeout_sec):
        self.calls += 1
        if self.status is not None:
            request = httpx.Request("POST", "https://example.invalid/chat")
            response = httpx.Response(self.status, request=request)
            raise httpx.HTTPStatusError(f"HTTP {self.status}", request=request, response=response)
        return LlmResponse(text="ok", usage={"prompt_tokens": 10, "completion_tokens": 5})


def _router(*providers: LlmProvider) -> ProviderRouter:
    router = ProviderRouter()
    for provider in providers:
        router.register(provider)
    return router


@pytest.fixture(autouse=True)
def clean_quota():
    quota.reset()
    yield
    quota.reset()


def test_limits_are_per_project():
    assert quota.daily_limit("gemini-3.5-flash-lite", 10) == 5000
    assert quota.daily_limit("gemini-3.8-flash", 10) == 200
    assert quota.daily_limit("deepseek-flash", 10) is None


def test_exhausted_after_record():
    assert quota.exhausted("gemini-3.8-flash", 1) is False
    for _ in range(quota.daily_limit("gemini-3.8-flash", 1)):
        quota.record("gemini-3.8-flash")
    assert quota.exhausted("gemini-3.8-flash", 1) is True


@pytest.mark.anyio
async def test_router_skips_exhausted_provider(monkeypatch):
    lite = FakeLlm("lite", "gemini-3.5-flash-lite")
    good = FakeLlm("deepseek-chat", "deepseek-flash")
    monkeypatch.setattr(
        quota, "exhausted", lambda model, projects=1: str(model) == "gemini-3.5-flash-lite"
    )
    router = _router(lite, good)

    result = await router.route_chat("free_tier_reasoning", [], [], resilient=True)

    assert result.response.text == "ok"
    assert lite.calls == 0
    assert good.calls == 1


@pytest.mark.anyio
async def test_413_cools_down_without_three_retries():
    broken = FakeLlm("broken", "gemini-3.5-flash-lite", status=413)
    good = FakeLlm("good", "deepseek-flash")
    router = _router(broken, good)

    first = await router.route_chat("free_tier_reasoning", [], [], resilient=True)
    second = await router.route_chat("free_tier_reasoning", [], [], resilient=True)

    assert first.provider_id == "good" and second.provider_id == "good"
    assert broken.calls == 1, "413 не повторяется: один ход, затем cooldown"


@pytest.mark.anyio
async def test_429_cools_down_provider():
    limited = FakeLlm("limited", "gemini-3.5-flash-lite", status=429)
    good = FakeLlm("good", "deepseek-flash")
    router = _router(limited, good)

    await router.route_chat("free_tier_reasoning", [], [], resilient=True)
    await router.route_chat("free_tier_reasoning", [], [], resilient=True)

    assert limited.calls == 1
