"""UX-2: единый pre-check Telegram и секретов работает и на agent-пути."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.config import get_settings
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.llm.providers.base import LlmProvider, LlmResponse
from aria.llm.router import ProviderRouter
from aria.routers import sessions as sessions_mod
from tests.test_telegram_gate import PERSONAL, PUBLIC
from tests.test_wave1_runner_and_policy import _RecordingRunner

SK = "sk-" + "a" * 24


class _EchoLlm(LlmProvider):
    def __init__(self) -> None:
        self.provider_id = "fake-chat"
        self.provider_class = "free_tier_reasoning"
        self.calls = 0

    async def check_connectivity(self, timeout_sec):
        return True

    async def chat(self, messages, tools, timeout_sec):
        self.calls += 1
        return LlmResponse(text="ok")


def _client():
    app = FastAPI()
    app.include_router(sessions_mod.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    rec = _RecordingRunner()
    app.state.task_runner = rec
    llm = ProviderRouter()
    provider = _EchoLlm()
    llm.register(provider)
    app.state.router = llm
    return TestClient(app), rec, provider


def _session() -> str:
    with session_scope() as db:
        return str(repo.create_session(db, "gates", active_role="general").id)


def _unlinked(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(settings, "telegram_chat_id", "")


def test_personal_telegram_request_is_gated_on_the_agent_path(monkeypatch):
    _unlinked(monkeypatch)
    client, rec, provider = _client()
    sid = _session()

    body = client.post(f"/sessions/{sid}/messages", json={"content": PERSONAL, "mode": "agent"}).json()

    assert body["mode"] == "telegram_not_connected"
    assert rec.submitted == [] and provider.calls == 0


def test_public_channel_still_reaches_the_agent(monkeypatch):
    _unlinked(monkeypatch)
    client, rec, _ = _client()
    sid = _session()

    body = client.post(f"/sessions/{sid}/messages", json={"content": PUBLIC, "mode": "agent"}).json()

    assert body["mode"] == "agent" and body["queued"] is True
    assert len(rec.submitted) == 1


def test_secret_is_gated_on_the_agent_path():
    client, rec, provider = _client()
    sid = _session()

    body = client.post(f"/sessions/{sid}/messages", json={"content": f"ключ {SK}", "mode": "agent"}).json()

    assert body["mode"] == "secret_redacted"
    assert SK not in str(body)
    assert rec.submitted == [] and provider.calls == 0
