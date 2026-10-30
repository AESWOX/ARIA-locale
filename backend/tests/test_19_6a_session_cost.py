"""19.6a: usage провайдера сохраняется, session_cost считается из цен ProviderModel."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.db import models as m
from aria.db.base import session_scope
from aria.llm import usage as usage_mod
from aria.llm.providers.base import LlmProvider, LlmResponse
from aria.llm.router import ProviderRouter
from aria.routers import chat as chat_mod

USAGE = {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}


class UsageLlm(LlmProvider):
    def __init__(self, provider_id: str, model: str, usage: dict):
        self.provider_id = provider_id
        self.provider_class = "free_tier_reasoning"
        self.model = model
        self.usage = usage
        self.calls = 0

    async def check_connectivity(self, timeout_sec):
        return True

    async def chat(self, messages, tools, timeout_sec):
        self.calls += 1
        return LlmResponse(text="ok", usage=dict(self.usage))


def _client(provider: UsageLlm) -> TestClient:
    app = FastAPI()
    app.include_router(chat_mod.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    router = ProviderRouter()
    router.register(provider)
    app.state.router = router
    return TestClient(app)


def _session(client: TestClient) -> str:
    return client.post("/chat/sessions", json={}).json()["session_id"]


def _add_price(provider_id: str, model_id: str, prompt: float, completion: float) -> None:
    with session_scope() as db:
        db.add(
            m.ProviderModel(
                id=f"{provider_id}:{model_id}",
                provider_id=provider_id,
                model_id=model_id,
                price_prompt_usd=prompt,
                price_completion_usd=completion,
            )
        )


def test_usage_tokens_accepts_both_shapes():
    assert usage_mod.usage_tokens({"prompt_tokens": 10, "completion_tokens": 5}) == (10, 5)
    assert usage_mod.usage_tokens({"input_tokens": 7, "output_tokens": 3}) == (7, 3)
    assert usage_mod.usage_tokens({}) is None
    assert usage_mod.usage_tokens(None) is None


def test_usage_cost_multiplies_per_token_prices():
    assert usage_mod.usage_cost(USAGE, 1e-6, 2e-6) == 0.002
    assert usage_mod.usage_cost(USAGE, None, 2e-6) is None
    assert usage_mod.usage_cost({}, 1e-6, 2e-6) is None


def test_session_cost_is_computed_from_provider_prices():
    client = _client(UsageLlm("cost-prov-3", "m3", USAGE))
    _add_price("cost-prov-3", "m3", 1e-6, 2e-6)
    sid = _session(client)

    sent = client.post(f"/chat/sessions/{sid}/send", json={"content": "hi"})
    assert sent.status_code == 200, sent.text
    body = client.get(f"/chat/sessions/{sid}/usage").json()

    assert body["session_cost"] == 0.002
    assert body["priced"] is True
    assert body["priced_messages"] == 1 and body["unpriced_messages"] == 0


def test_session_cost_is_null_without_prices():
    client = _client(UsageLlm("cost-prov-4", "m4", USAGE))
    sid = _session(client)

    client.post(f"/chat/sessions/{sid}/send", json={"content": "hi"})
    body = client.get(f"/chat/sessions/{sid}/usage").json()

    assert body["session_cost"] is None
    assert body["priced"] is False
    assert body["unpriced_messages"] == 1


def test_streaming_usage_is_persisted():
    client = _client(UsageLlm("cost-prov-5", "m5", USAGE))
    _add_price("cost-prov-5", "m5", 1e-6, 2e-6)
    sid = _session(client)

    streamed = client.post(f"/chat/sessions/{sid}/send", json={"content": "hi", "stream": True})
    assert streamed.status_code == 200, streamed.text
    body = client.get(f"/chat/sessions/{sid}/usage").json()

    assert body["session_cost"] == 0.002
    assert body["priced"] is True and body["priced_messages"] == 1
