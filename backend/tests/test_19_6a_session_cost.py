"""19.6a/E3: /usage считает стоимость из llm_usage и честно сообщает неполноту."""
from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.db import models as m
from aria.db.base import session_scope
from aria.routers import chat as chat_mod

USAGE = {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500}


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(chat_mod.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    app.state.router = None
    return TestClient(app)


def _session(client: TestClient) -> str:
    return client.post("/chat/sessions", json={}).json()["session_id"]


def _add_usage(session_id: str, cost: float | None, purpose: str = "chat") -> None:
    with session_scope() as db:
        db.add(
            m.LlmUsage(
                session_id=uuid.UUID(session_id),
                provider_id="deepseek-chat",
                model="deepseek-flash",
                purpose=purpose,
                prompt_total=1000,
                completion=500,
                cost_usd=cost,
                tier="off",
            )
        )


def test_session_cost_sums_llm_usage_rows():
    client = _client()
    sid = _session(client)
    _add_usage(sid, 0.001)
    _add_usage(sid, 0.001)

    body = client.get(f"/chat/sessions/{sid}/usage").json()

    assert body["session_cost"] == 0.002
    assert body["session_cost_complete"] is True and body["priced"] is True
    assert body["priced_messages"] == 2 and body["unpriced_messages"] == 0


def test_unknown_price_makes_cost_incomplete():
    client = _client()
    sid = _session(client)
    _add_usage(sid, 0.001)
    _add_usage(sid, None)

    body = client.get(f"/chat/sessions/{sid}/usage").json()

    assert body["session_cost_complete"] is False and body["priced"] is False
    assert body["priced_messages"] == 1 and body["unpriced_messages"] == 1


def test_empty_session_has_no_cost():
    client = _client()
    sid = _session(client)

    body = client.get(f"/chat/sessions/{sid}/usage").json()

    assert body["session_cost"] is None and body["priced"] is False
    assert body["session_cost_complete"] is False


def test_usage_endpoint_unknown_session_404():
    client = _client()
    response = client.get(f"/chat/sessions/{uuid.uuid4()}/usage")
    assert response.status_code == 404
