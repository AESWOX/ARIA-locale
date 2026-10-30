"""E3: бюджет по факту из llm_usage, task_spend, предоценка и Approve до старта."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.config import get_settings
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.llm import budget
from aria.llm.router import ProviderRouter
from aria.routers import sessions as sessions_mod
from tests.test_wave1_runner_and_policy import _RecordingRunner


def _cost(amount: float | None, *, minutes_ago: int = 0, task_id=None, session_id=None) -> None:
    with session_scope() as db:
        db.add(
            m.LlmUsage(
                session_id=session_id,
                task_id=task_id,
                provider_id="deepseek-chat",
                model="deepseek-flash",
                purpose="agent",
                prompt_total=1000,
                completion=100,
                cost_usd=amount,
                tier="off",
                created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
            )
        )


def test_daily_weekly_sums_and_incomplete():
    _cost(0.01)
    _cost(None)
    _cost(0.02, minutes_ago=60 * 24 * 2)

    status = budget.compute_budget_status(1.0, 2.0)

    assert status["daily_usd"] == 0.01
    assert status["weekly_usd"] == 0.03
    assert status["incomplete"] is True
    assert status["daily_pct"] == 1.0


def test_day_boundary_excludes_yesterday_local():
    from aria.http_utils import utc_now

    day_start = utc_now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    with session_scope() as db:
        db.add(
            m.LlmUsage(
                provider_id="deepseek-chat",
                model="deepseek-flash",
                purpose="agent",
                prompt_total=1,
                completion=1,
                cost_usd=0.05,
                tier="off",
                created_at=day_start - timedelta(minutes=1),
            )
        )

    status = budget.compute_budget_status(1.0, 2.0)

    assert status["daily_usd"] == 0.0
    assert status["weekly_usd"] == 0.05


def test_task_spend():
    with session_scope() as db:
        session = repo.create_session(db, "e3-spend", active_role="general")
        task = repo.create_task(db, session, role="general", objective="x")
        task_id = task.id
    _cost(0.1, task_id=task_id)
    _cost(0.2, task_id=task_id)
    _cost(None, task_id=task_id)

    assert budget.task_spend(task_id) == 0.3


def test_estimate_fallback_and_history():
    assert budget.estimate_task_cost("project_q") == {
        "p50": 0.036, "p90": 0.036, "n": 0, "source": "fallback",
    }
    for amount in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6):
        with session_scope() as db:
            session = repo.create_session(db, "e3-est", active_role="general")
            task = repo.create_task(db, session, role="general", objective="x")
            task_id = task.id
        _cost(amount, task_id=task_id)

    estimate = budget.estimate_task_cost("project_q")
    assert estimate["source"] == "history" and estimate["n"] == 6
    assert 0.3 <= estimate["p50"] <= 0.4
    assert 0.4 <= estimate["p90"] <= 0.6


def test_budget_gate_blocks_premium(monkeypatch):
    _cost(0.02)
    monkeypatch.setattr(get_settings(), "budget_daily_usd", 0.01)
    monkeypatch.setattr(get_settings(), "budget_weekly_usd", 0.0)

    router = ProviderRouter()
    assert router.budget_gate("premium_reasoning") == (False, True)
    assert router.budget_gate("standard_reasoning") == (False, True)
    assert router.budget_gate("free_tier_reasoning") == (True, True)


def _client() -> tuple[TestClient, _RecordingRunner]:
    app = FastAPI()
    app.include_router(sessions_mod.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    runner = _RecordingRunner()
    app.state.task_runner = runner
    app.state.router = None
    return TestClient(app), runner


def _session() -> str:
    with session_scope() as db:
        return str(repo.create_session(db, "e3-approval", active_role="general").id)


def _seed_expensive_tasks() -> None:
    for amount in (0.8, 0.9, 1.0, 1.1, 1.2, 1.3):
        with session_scope() as db:
            session = repo.create_session(db, "e3-seed", active_role="general")
            task = repo.create_task(db, session, role="general", objective="x")
            task_id = task.id
        _cost(amount, task_id=task_id)


def test_expensive_task_waits_for_approval_and_resumes_plan(monkeypatch):
    monkeypatch.setattr("aria.core.clarifier.gate_required", lambda *a, **k: False)
    _seed_expensive_tasks()
    client, runner = _client()
    sid = _session()

    response = client.post(f"/sessions/{sid}/messages", json={"content": "сделай большое", "mode": "plan"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["queued"] is False and body["status"] == "awaiting_attention"
    assert body["questions"] == [] and body["attention_required"] is True
    assert body["estimate"]["p90"] >= 0.8
    assert runner.submitted == []
    with session_scope() as db:
        items = [i for i in repo.list_attention_items(db, only_pending=True) if str(i.task_id) == body["task_id"]]
    assert len(items) == 1 and (items[0].payload_json or {}).get("kind") == "estimate"

    approved = client.post(f"/attention-items/{items[0].id}/approve")

    assert approved.status_code == 200, approved.text
    assert runner.submitted and runner.submitted[0][1] == "plan"
