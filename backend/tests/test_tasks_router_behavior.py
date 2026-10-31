"""Поведение роутера tasks. Исполнитель подменён заглушкой: реальный TaskRunner и LLM не стартуют."""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from aria.api.auth import token_store
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import TaskStatus
from aria.main import app


class FakeRunner:
    def __init__(self) -> None:
        self.submitted: list[tuple] = []
        self.cancelled: list[uuid.UUID] = []

    def status(self) -> dict:
        return {"workers": 1, "queued": len(self.submitted)}

    def submit(self, task_id, mode="plan", source="ui") -> dict:
        self.submitted.append((task_id, mode, source))
        return {"ok": True, "queued": True, "task_id": str(task_id), "mode": mode}

    def cancel(self, task_id) -> None:
        self.cancelled.append(task_id)


@pytest.fixture
def runner(monkeypatch):
    fake = FakeRunner()
    monkeypatch.setattr(app.state, "task_runner", fake, raising=False)
    return fake


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def _h():
    if not token_store.current_token():
        token_store.issue()
    return {"X-Local-Agent-Token": token_store.current_token()}


def _task(status: TaskStatus | None = None, parent: uuid.UUID | None = None) -> uuid.UUID:
    with session_scope() as db:
        session = repo.create_session(db, "tasks behavior")
        task = repo.create_task(db, session, "general", "write a report", parent_task_id=parent)
        if status is not None and task.status != status:
            task.status = status
        return task.id


def test_runner_status_shape(client, runner):
    r = client.get("/tasks/runner/status", headers=_h())
    assert r.status_code == 200, r.text
    assert r.json() == {"workers": 1, "queued": 0}


def test_runner_status_503_without_runner(client, monkeypatch):
    monkeypatch.setattr(app.state, "task_runner", None, raising=False)
    assert client.get("/tasks/runner/status", headers=_h()).status_code == 503


def test_start_rejects_unknown_mode(client, runner):
    r = client.post(f"/tasks/{_task()}/start", json={"mode": "turbo"}, headers=_h())
    assert r.status_code == 400
    assert runner.submitted == []


def test_start_unknown_task_is_404(client, runner):
    assert client.post(f"/tasks/{uuid.uuid4()}/start", headers=_h()).status_code == 404


def test_start_in_progress_task_is_queued_with_default_plan_mode(client, runner):
    tid = _task(TaskStatus.in_progress)
    r = client.post(f"/tasks/{tid}/start", headers=_h())
    assert r.status_code == 200, r.text
    assert runner.submitted == [(tid, "plan", "ui")]


def test_start_passes_agent_mode(client, runner):
    tid = _task(TaskStatus.in_progress)
    r = client.post(f"/tasks/{tid}/start", json={"mode": "AGENT"}, headers=_h())
    assert r.status_code == 200
    assert runner.submitted[-1][1] == "agent"


def test_start_draft_without_spec_is_blocked_by_gate(client, runner):
    r = client.post(f"/tasks/{_task(TaskStatus.draft)}/start", headers=_h())
    assert r.status_code == 409
    assert runner.submitted == []


def test_start_without_runner_is_503(client, monkeypatch):
    monkeypatch.setattr(app.state, "task_runner", None, raising=False)
    assert client.post(f"/tasks/{_task()}/start", headers=_h()).status_code == 503


def test_run_executor_unknown_task_is_404(client, runner):
    assert client.post(f"/tasks/{uuid.uuid4()}/run-executor", headers=_h()).status_code == 404


def test_run_executor_queues_plan_mode_for_executor(client, runner):
    tid = _task(TaskStatus.in_progress)
    r = client.post(f"/tasks/{tid}/run-executor", headers=_h())
    assert r.status_code == 200, r.text
    assert runner.submitted == [(tid, "plan", "executor")]


def test_children_unknown_task_is_404(client):
    assert client.get(f"/tasks/{uuid.uuid4()}/children", headers=_h()).status_code == 404


def test_children_lists_child_tasks(client):
    parent = _task()
    child = _task(parent=parent)
    body = client.get(f"/tasks/{parent}/children", headers=_h()).json()
    assert [c["id"] for c in body] == [str(child)]
    assert client.get(f"/tasks/{child}/children", headers=_h()).json() == []


def test_cancel_unknown_task_is_404(client, runner):
    assert client.post(f"/tasks/{uuid.uuid4()}/cancel", headers=_h()).status_code == 404


def test_cancel_marks_task_cancelled_and_asks_runner(client, runner):
    tid = _task(TaskStatus.in_progress)
    r = client.post(f"/tasks/{tid}/cancel", headers=_h())
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "cancelled": True, "task_id": str(tid)}
    assert runner.cancelled == [tid]
    with session_scope() as db:
        assert repo.get_task(db, tid).status == TaskStatus.cancelled


def test_cancel_finished_task_keeps_its_status(client, runner):
    tid = _task(TaskStatus.done)
    assert client.post(f"/tasks/{tid}/cancel", headers=_h()).status_code == 200
    with session_scope() as db:
        assert repo.get_task(db, tid).status == TaskStatus.done


def test_audit_reports_empty_list_for_new_task(client):
    assert client.get(f"/tasks/{_task()}/audit-reports", headers=_h()).json() == []


def test_audit_reports_unknown_task_is_404(client):
    assert client.get(f"/tasks/{uuid.uuid4()}/audit-reports", headers=_h()).status_code == 404


def test_plan_without_plan_has_empty_shape(client):
    tid = _task()
    r = client.get(f"/tasks/{tid}/plan", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["task_id"] == str(tid)
    assert body["steps"] == [] and body["waves"] == [] and isinstance(body["criteria"], list)


def test_plan_unknown_task_is_404(client):
    assert client.get(f"/tasks/{uuid.uuid4()}/plan", headers=_h()).status_code == 404


def test_request_approval_unknown_task_is_404(client):
    assert client.post(f"/tasks/{uuid.uuid4()}/plan/request-approval", headers=_h()).status_code == 404


def test_request_approval_from_finished_task_is_409(client):
    r = client.post(f"/tasks/{_task(TaskStatus.done)}/plan/request-approval", headers=_h())
    assert r.status_code == 409


@pytest.mark.parametrize("method,path", [("GET", "/tasks/runner/status"), ("POST", f"/tasks/{uuid.uuid4()}/cancel")])
def test_protected_routes_reject_missing_token(client, method, path):
    assert client.request(method, path).status_code in (401, 403)
