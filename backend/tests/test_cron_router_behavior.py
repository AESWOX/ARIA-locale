"""Поведение роутера cron: создание, чтение, изменение, пауза, удаление, валидация.

Планировщик не запускается, реальные задачи не исполняются: trigger работает через подменённый раннер.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from aria.api.auth import token_store
from aria.main import app
from aria.routers import cron as cron_router

JOB_KEYS = {
    "id", "profile", "profile_name", "aria_home", "is_default_profile", "name", "prompt", "script", "skills",
    "schedule", "schedule_display", "enabled", "state", "deliver", "last_run_at", "next_run_at", "last_error",
    "urgent",
    "defer_until",
    "deferred_note",
}


@pytest.fixture(autouse=True)
def _clean_meta():
    cron_router._JOB_META.clear()
    yield
    cron_router._JOB_META.clear()


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def _h():
    if not token_store.current_token():
        token_store.issue()
    return {"X-Local-Agent-Token": token_store.current_token()}


def _create(client, **over) -> dict:
    body = {"prompt": "say hi", "schedule": "0 9 * * *", "name": "Morning"} | over
    r = client.post("/cron/jobs", json=body, headers=_h())
    assert r.status_code == 200, r.text
    return r.json()


def test_create_returns_full_job_shape(client):
    job = _create(client, skills=["file_read"])
    assert set(job) == JOB_KEYS
    assert job["name"] == "Morning" and job["prompt"] == "say hi"
    assert job["schedule"] == {"kind": "cron", "expr": "0 9 * * *", "display": "0 9 * * *"}
    assert job["skills"] == ["file_read"]
    assert job["enabled"] is True and job["state"] == "scheduled"
    assert job["deliver"] == "local" and job["profile"] == "default"


def test_create_without_name_generates_one(client):
    job = _create(client, name="")
    assert job["name"].startswith("Job ")


@pytest.mark.parametrize("body", [{"prompt": "x"}, {"schedule": "* * * * *"}, {"prompt": " ", "schedule": " "}, {}])
def test_create_requires_prompt_and_schedule(client, body):
    assert client.post("/cron/jobs", json=body, headers=_h()).status_code == 400


def test_created_job_is_listed(client):
    job = _create(client)
    listed = client.get("/cron/jobs", headers=_h()).json()
    assert job["id"] in [j["id"] for j in listed]


def test_list_filters_by_profile(client):
    mine = client.post("/cron/jobs?profile=work", json={"prompt": "p", "schedule": "* * * * *"}, headers=_h()).json()
    other = _create(client)
    ids = [j["id"] for j in client.get("/cron/jobs?profile=work", headers=_h()).json()]
    assert ids == [mine["id"]]
    all_ids = [j["id"] for j in client.get("/cron/jobs", headers=_h()).json()]
    assert {mine["id"], other["id"]} <= set(all_ids)


def test_update_changes_are_visible_on_next_get(client):
    job = _create(client)
    r = client.put(
        f"/cron/jobs/{job['id']}",
        json={"updates": {"name": "Renamed", "schedule": "30 8 * * 1", "prompt": "new", "deliver": "telegram"}},
        headers=_h(),
    )
    assert r.status_code == 200, r.text
    listed = next(j for j in client.get("/cron/jobs", headers=_h()).json() if j["id"] == job["id"])
    assert listed["name"] == "Renamed" and listed["prompt"] == "new"
    assert listed["schedule_display"] == "30 8 * * 1"
    assert listed["deliver"] == "telegram"


def test_update_unknown_job_is_404(client):
    assert client.put(f"/cron/jobs/{uuid.uuid4()}", json={"updates": {"name": "x"}}, headers=_h()).status_code == 404


def test_pause_and_resume_toggle_enabled(client):
    job = _create(client)
    paused = client.post(f"/cron/jobs/{job['id']}/pause", headers=_h())
    assert paused.status_code == 200, paused.text
    row = next(j for j in client.get("/cron/jobs", headers=_h()).json() if j["id"] == job["id"])
    assert row["enabled"] is False and row["state"] == "disabled"
    assert client.post(f"/cron/jobs/{job['id']}/resume", headers=_h()).status_code == 200
    row = next(j for j in client.get("/cron/jobs", headers=_h()).json() if j["id"] == job["id"])
    assert row["enabled"] is True and row["state"] == "scheduled"


@pytest.mark.parametrize("action", ["pause", "resume", "trigger"])
def test_job_actions_on_unknown_job_are_404(client, action):
    assert client.post(f"/cron/jobs/{uuid.uuid4()}/{action}", headers=_h()).status_code == 404


def test_trigger_uses_runner_and_records_run(client, monkeypatch):
    calls: list[str] = []

    async def fake_run(name, router=None):
        calls.append(name)
        return {"ok": True, "job": name}

    monkeypatch.setattr("aria.scheduler.jobs.run_scheduler_job_by_name", fake_run)
    job = _create(client, name="expire_stale_attention_items")
    r = client.post(f"/cron/jobs/{job['id']}/trigger", headers=_h())
    assert r.status_code == 200, r.text
    assert calls == ["expire_stale_attention_items"]
    assert r.json()["last_run_at"] is not None


def test_trigger_runner_failure_is_recorded_not_raised(client, monkeypatch):
    async def boom(name, router=None):
        raise RuntimeError("nope")

    monkeypatch.setattr("aria.scheduler.jobs.run_scheduler_job_by_name", boom)
    job = _create(client)
    r = client.post(f"/cron/jobs/{job['id']}/trigger", headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["last_run_at"] is not None


def test_delete_removes_job_and_second_delete_is_404(client):
    job = _create(client)
    assert client.delete(f"/cron/jobs/{job['id']}", headers=_h()).status_code == 200
    assert job["id"] not in [j["id"] for j in client.get("/cron/jobs", headers=_h()).json()]
    assert client.delete(f"/cron/jobs/{job['id']}", headers=_h()).status_code == 404


def test_delivery_targets_shape(client):
    body = client.get("/cron/delivery-targets", headers=_h()).json()
    targets = {t["id"]: t for t in body["targets"]}
    assert {"local", "telegram"} <= set(targets)
    assert targets["local"]["home_target_set"] is False
    assert set(targets["telegram"]) == {"id", "name", "home_target_set", "home_env_var"}


def test_telegram_target_reflects_env_token(client, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:fake")
    targets = {t["id"]: t for t in client.get("/cron/delivery-targets", headers=_h()).json()["targets"]}
    assert targets["telegram"]["home_target_set"] is True


def test_blueprints_empty_and_instantiate_is_404(client):
    assert client.get("/cron/blueprints", headers=_h()).json() == {"blueprints": []}
    assert client.post("/cron/blueprints/instantiate", json={}, headers=_h()).status_code == 404


@pytest.mark.parametrize("schedule", ["not a cron", "every day", "* * *", "61 * * * *", "*/0 * * * *"])
def test_create_rejects_invalid_schedule(client, schedule):
    r = client.post("/cron/jobs", json={"prompt": "p", "schedule": schedule}, headers=_h())
    assert r.status_code == 422, r.text
    assert client.get("/cron/jobs", headers=_h()).json() == []


@pytest.mark.parametrize("schedule", ["0 9 * * *", "*/5 * * * *", "30 8 * * 1"])
def test_create_accepts_valid_schedule(client, schedule):
    r = client.post("/cron/jobs", json={"prompt": "p", "schedule": schedule}, headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["schedule_display"] == schedule


def test_update_rejects_invalid_schedule_and_keeps_old(client):
    job = _create(client)
    r = client.put(
        f"/cron/jobs/{job['id']}", json={"updates": {"schedule": "not a cron"}}, headers=_h()
    )
    assert r.status_code == 422
    listed = next(j for j in client.get("/cron/jobs", headers=_h()).json() if j["id"] == job["id"])
    assert listed["schedule_display"] == "0 9 * * *"


def test_job_ids_must_be_uuid(client):
    assert client.delete("/cron/jobs/not-a-uuid", headers=_h()).status_code == 422


def test_protected_routes_reject_missing_token(client):
    assert client.get("/cron/jobs").status_code in (401, 403)
    assert client.post("/cron/jobs", json={"prompt": "p", "schedule": "* * * * *"}).status_code in (401, 403)
