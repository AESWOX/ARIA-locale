"""F-016: per-call usage.jsonl — токены/цена/источник, неуспешные попытки с cost=0,
реальная стоимость сессии по всем источникам, тривиальный чат = один вызов."""
from __future__ import annotations

import json
import uuid

import httpx
import pytest
from aria.api.auth import require_runtime_token
from aria.db import models as m
from aria.db.base import session_scope
from aria.llm import usage as llm_usage
from aria.llm.providers.base import LlmProvider, LlmResponse
from aria.llm.router import ProviderRouter
from aria.routers import chat as chat_mod
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

USAGE = {
    "prompt_tokens": 1000,
    "completion_tokens": 500,
    "prompt_cache_hit_tokens": 700,
    "prompt_cache_miss_tokens": 300,
    "completion_tokens_details": {"reasoning_tokens": 42},
}

REQUIRED_FIELDS = {
    "ts", "task_id", "session_id", "source", "class", "provider", "model",
    "input", "cache_hit", "cache_miss", "output", "reasoning", "ms",
    "cost_usd", "outcome",
}


class FakeLlm(LlmProvider):
    def __init__(self, provider_id: str = "probe", text: str = "ok", model: str = "deepseek-chat"):
        self.provider_id = provider_id
        self.provider_class = "free_tier_reasoning"
        self.model = model
        self._text = text
        self.calls = 0

    async def check_connectivity(self, timeout_sec):
        return True

    async def chat(self, messages, tools, timeout_sec):
        self.calls += 1
        return LlmResponse(text=self._text, usage=dict(USAGE))


class FailingLlm(FakeLlm):
    def __init__(self, status: int = 413):
        super().__init__(provider_id="oversize-prov")
        self.status = status

    async def chat(self, messages, tools, timeout_sec):
        self.calls += 1
        request = httpx.Request("POST", "https://example.invalid/chat")
        response = httpx.Response(self.status, request=request)
        raise httpx.HTTPStatusError(f"HTTP {self.status}", request=request, response=response)


def _router(*providers: LlmProvider) -> ProviderRouter:
    router = ProviderRouter()
    for provider in providers:
        router.register(provider)
    return router


@pytest.fixture(autouse=True)
def _clean_usage_log():
    path = llm_usage.usage_log_path()
    if path.exists():
        path.unlink()
    yield


def _lines() -> list[dict]:
    path = llm_usage.usage_log_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _db_rows() -> list[m.LlmUsage]:
    with session_scope() as db:
        return list(db.execute(select(m.LlmUsage)).scalars())


@pytest.mark.anyio
async def test_usage_jsonl_has_per_call_fields():
    router = _router(FakeLlm(model="deepseek-chat"))
    sid = uuid.uuid4()
    await router.route_chat("free_tier_reasoning", [], [], purpose="chat", session_id=sid)

    lines = _lines()
    assert len(lines) == 1
    line = lines[0]
    assert REQUIRED_FIELDS <= set(line), sorted(REQUIRED_FIELDS - set(line))
    assert line["source"] == "chat"
    assert line["class"] == "free_tier_reasoning"
    assert line["provider"] == "probe" and line["model"] == "deepseek-chat"
    assert (line["input"], line["cache_hit"], line["cache_miss"], line["output"], line["reasoning"]) == (
        1000, 700, 300, 500, 42,
    )
    assert line["outcome"] == "ok"
    assert line["cost_usd"] is not None and line["cost_usd"] > 0
    assert line["session_id"] == str(sid)
    assert line["ms"] is not None


@pytest.mark.anyio
async def test_failed_attempt_written_with_zero_cost():
    router = _router(FailingLlm(413))
    with pytest.raises(httpx.HTTPStatusError):
        await router.route_chat("free_tier_reasoning", [], [], resilient=True, purpose="subagent")

    lines = _lines()
    assert lines, "неуспешная попытка должна попасть в usage.jsonl"
    failed = [line for line in lines if line["outcome"] == "http_413"]
    assert failed and failed[0]["cost_usd"] == 0.0
    assert failed[0]["source"] == "subagent"
    assert _db_rows() == [], "неуспешные попытки не входят в агрегат стоимости"


def _client(provider: LlmProvider) -> TestClient:
    app = FastAPI()
    app.include_router(chat_mod.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    app.state.router = _router(provider)
    return TestClient(app)


def test_trivial_chat_is_exactly_one_llm_call():
    fake = FakeLlm(text="Привет!")
    client = _client(fake)
    sid = client.post("/chat/sessions", json={}).json()["session_id"]

    response = client.post(f"/chat/sessions/{sid}/send", json={"content": "привет"})

    assert response.status_code == 200, response.text
    assert fake.calls == 1
    chat_lines = [line for line in _lines() if line["source"] == "chat"]
    assert len(chat_lines) == 1 and chat_lines[0]["provider"] == "probe"


def test_session_cost_sums_agent_subagent_audit_compression():
    client = _client(FakeLlm())
    sid = client.post("/chat/sessions", json={}).json()["session_id"]
    for purpose in ("chat", "agent", "subagent", "audit", "compress"):
        llm_usage.record_usage(
            provider_id="deepseek-chat",
            model="deepseek-chat",
            usage=USAGE,
            purpose=purpose,
            session_id=uuid.UUID(sid),
        )

    body = client.get(f"/chat/sessions/{sid}/usage").json()

    assert body["calls"] == 5
    assert body["priced"] is True
    assert body["session_cost"] and body["session_cost"] > 0
    assert body["total_completion_tokens"] == 2500
    assert body["total_reasoning_tokens"] == 210
