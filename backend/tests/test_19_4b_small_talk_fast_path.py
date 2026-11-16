"""19.4b B1: small talk is answered by chat in any mode — no task, no plan, no cards."""
from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import TaskStatus
from aria.llm.providers.base import LlmProvider, LlmResponse
from aria.llm.router import ProviderRouter
from aria.routers import sessions as sessions_mod
from tests.test_wave1_runner_and_policy import _RecordingRunner


class _EchoLlm(LlmProvider):
    def __init__(self):
        self.provider_id = "fake-chat"
        self.provider_class = "free_tier_reasoning"
        self.calls = 0

    async def check_connectivity(self, timeout_sec):
        return True

    async def chat(self, messages, tools, timeout_sec):
        self.calls += 1
        return LlmResponse(text="Привет! Чем помочь?")


def _client(provider=None):
    app = FastAPI()
    app.include_router(sessions_mod.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    rec = _RecordingRunner()
    app.state.task_runner = rec
    llm = ProviderRouter()
    if provider is not None:
        llm.register(provider)
    app.state.router = llm
    return TestClient(app), rec


def _session(title: str = "small-talk") -> str:
    with session_scope() as db:
        return str(repo.create_session(db, title, active_role="general").id)


@pytest.mark.parametrize("mode", ["agent", "plan"])
@pytest.mark.parametrize("text", ["привет", "ку бро"])
def test_small_talk_answers_directly_without_a_task(mode, text):
    provider = _EchoLlm()
    client, rec = _client(provider)
    sid = _session()

    r = client.post(f"/sessions/{sid}/messages", json={"content": text, "mode": mode})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "chat"
    assert body.get("queued") is not True
    assert rec.submitted == []
    with session_scope() as db:
        session = repo.get_session(db, uuid.UUID(sid))
        assert session.current_task_id is None
        roles = [msg.role for msg in repo.list_messages(db, uuid.UUID(sid), limit=10)]
    assert roles[-1] == "assistant"
    assert provider.calls == 1


def test_small_talk_after_a_finished_task_does_not_start_a_new_one():
    client, rec = _client(_EchoLlm())
    with session_scope() as db:
        session = repo.create_session(db, "done", active_role="general")
        task = repo.create_task(db, session, role="general", objective="старая задача")
        repo.set_task_status(db, task, TaskStatus.cancelled)
        sid, old_id = str(session.id), str(task.id)

    r = client.post(f"/sessions/{sid}/messages", json={"content": "спасибо", "mode": "agent"})

    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "chat"
    assert rec.submitted == []
    with session_scope() as db:
        assert str(repo.get_session(db, uuid.UUID(sid)).current_task_id) == old_id


def test_answer_to_open_questions_is_not_small_talk():
    """«да»/«ок» в ожидании уточнения уходят в TaskSpec, а не в быстрый чат."""
    client, _ = _client(_EchoLlm())
    with session_scope() as db:
        session = repo.create_session(db, "awaiting", active_role="general")
        task = repo.create_task(db, session, role="general", objective="сделай отчёт по логам")
        repo.set_task_status(db, task, TaskStatus.awaiting_clarification)
        sid = str(session.id)

    r = client.post(f"/sessions/{sid}/messages", json={"content": "да", "mode": "plan"})

    assert r.status_code == 200, r.text
    assert r.json().get("mode") != "chat"
