"""19.4b D-21: the mode chosen in the Agent tab survives the clarification round."""
from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.core import clarifier
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import TaskStatus
from aria.routers import sessions as sessions_mod
from aria.routers import tasks as tasks_mod
from tests.test_wave1_runner_and_policy import _RecordingRunner


def _client():
    app = FastAPI()
    app.include_router(sessions_mod.router)
    app.include_router(tasks_mod.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    rec = _RecordingRunner()
    app.state.task_runner = rec
    app.state.router = None  # deterministic clarifier gate: fallback questions
    return TestClient(app), rec


def _session(title: str = "d21") -> str:
    with session_scope() as db:
        return str(repo.create_session(db, title, active_role="general").id)


def test_mode_from_the_message_is_stored_in_the_task_spec():
    client, _ = _client()
    sid = _session()

    r = client.post(f"/sessions/{sid}/messages", json={"content": "сделай отчёт по логам", "mode": "agent"})

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "awaiting_clarification"
    with session_scope() as db:
        session = repo.get_session(db, uuid.UUID(sid))
        spec = clarifier.load_spec(db, session.current_task_id)
    assert spec is not None and spec.mode == "agent"


def test_clarify_without_mode_uses_the_stored_mode():
    client, rec = _client()
    with session_scope() as db:
        session = repo.create_session(db, "d21-clarify", active_role="general")
        task = repo.create_task(db, session, role="general", objective="сделай отчёт по логам")
        repo.set_task_status(db, task, TaskStatus.awaiting_clarification)
        from aria.core.taskspec import TaskSpec, default_criterion

        spec = TaskSpec(
            goal="сделай отчёт по логам",
            acceptance_criteria=[default_criterion("отчёт готов")],
            mode="agent",
        )
        repo.upsert_task_spec(db, task, spec.model_dump(mode="json"), valid=True, clarify_rounds=0)
        task_id = str(task.id)

    r = client.post(f"/tasks/{task_id}/clarify", json={"answers": [], "accept_assumptions": True})

    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "agent"
    assert rec.submitted[0][1] == "agent"


def test_clarify_still_defaults_to_plan_when_no_mode_is_stored():
    client, rec = _client()
    with session_scope() as db:
        session = repo.create_session(db, "d21-plan", active_role="general")
        task = repo.create_task(db, session, role="general", objective="сделай отчёт по логам")
        repo.set_task_status(db, task, TaskStatus.awaiting_clarification)
        from aria.core.taskspec import TaskSpec, default_criterion

        spec = TaskSpec(goal="сделай отчёт по логам", acceptance_criteria=[default_criterion("отчёт готов")])
        repo.upsert_task_spec(db, task, spec.model_dump(mode="json"), valid=True, clarify_rounds=0)
        task_id = str(task.id)

    r = client.post(f"/tasks/{task_id}/clarify", json={"answers": [], "accept_assumptions": True})

    assert r.status_code == 200, r.text
    assert rec.submitted[0][1] == "plan"
