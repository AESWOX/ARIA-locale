"""Этап 4: аудит только для задач с инструментами, дешёвая модель, одна попытка, бейдж."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from aria.config import get_settings
from aria.core.audit import run_audit
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import AuditVerdict, ToolStatus


class _FakeRouter:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0
        self.messages: list[list] = []

    async def route_chat(self, provider_class, messages, **kwargs):
        self.calls += 1
        self.messages.append(list(messages))
        return SimpleNamespace(
            provider_id="cheap-auditor",
            degraded_to_free=False,
            response=SimpleNamespace(text=self.text),
        )


def _world():
    with session_scope() as db:
        session = repo.create_session(db, "audit4", active_role="worker")
        task = repo.create_task(db, session, role="worker", objective="write report")
        return session.id, task.id


def _add_tool(task_id, status=ToolStatus.ok):
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        session = repo.get_session(db, task.session_id)
        call = repo.start_tool_call(db, session, task, "file_write", "worker", "low", {"path": "out.md"})
        repo.finish_tool_call(db, call, status, output_json={"bytes_written": 3})


def test_conversational_turn_is_not_audited():
    _sid, tid = _world()
    router = _FakeRouter('{"verdict":"pass"}')
    with session_scope() as db:
        task = repo.get_task(db, tid)
        session = repo.get_session(db, task.session_id)
        report = asyncio.run(run_audit(db, session, task, router))
    assert report.verdict == AuditVerdict.unaudited
    assert report.auditor_model == "structural-only"
    assert router.calls == 0


def test_tool_task_is_audited_by_a_model_and_logs_the_model():
    _sid, tid = _world()
    _add_tool(tid)
    router = _FakeRouter('{"verdict":"pass","issues":[]}')
    with session_scope() as db:
        task = repo.get_task(db, tid)
        session = repo.get_session(db, task.session_id)
        report = asyncio.run(run_audit(db, session, task, router))
    assert router.calls == 1
    assert report.auditor_model == "cheap-auditor" and report.auditor_model != "off"
    assert report.verdict == AuditVerdict.pass_


def test_auditor_uses_the_qa_prompt_asset():
    _sid, tid = _world()
    _add_tool(tid)
    router = _FakeRouter('{"verdict":"pass","issues":[]}')
    with session_scope() as db:
        task = repo.get_task(db, tid)
        session = repo.get_session(db, task.session_id)
        asyncio.run(run_audit(db, session, task, router))
    system = router.messages[0][0].content
    assert "Ты qa_auditor" in system
    assert "Нет доказательства — fail" in system or "нет доказательства" in system.lower()


def test_one_automatic_rework_attempt_then_honest_fail():
    assert get_settings().audit_max_attempts == 3
    _sid, tid = _world()
    _add_tool(tid, status=ToolStatus.error)
    router = _FakeRouter("fail")
    with session_scope() as db:
        task = repo.get_task(db, tid)
        session = repo.get_session(db, task.session_id)
        first = asyncio.run(run_audit(db, session, task, router))
        second = asyncio.run(run_audit(db, session, task, router))
        third = asyncio.run(run_audit(db, session, task, router))
    assert first.verdict == AuditVerdict.needs_rework and first.attempt_no == 1
    assert second.verdict == AuditVerdict.needs_rework and second.attempt_no == 2
    assert third.verdict == AuditVerdict.fail_after_max_attempts and third.attempt_no == 3
    assert router.calls == 0, "структурный провал решается без модели"


def test_run_state_exposes_the_audit_badge():
    sid, tid = _world()
    with session_scope() as db:
        session = repo.get_session(db, sid)
        task = repo.get_task(db, tid)
        repo.create_audit_report(
            db, session, task, attempt_no=1, auditor_role="qa_auditor",
            auditor_model="cheap-auditor", budget_degraded=False,
            verdict=AuditVerdict.pass_, plan_vs_fact={}, tool_success_summary={},
            missing_requirements=[], patch_suggestions=[], metrics_compared={},
        )
        session.current_task_id = tid
        db.flush()
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aria.api.auth import require_runtime_token
    from aria.routers.sessions import router as sessions_router

    app = FastAPI()
    app.include_router(sessions_router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    body = TestClient(app).get(f"/sessions/{sid}/run").json()
    assert body["audit"]["verdict"] == "pass" and body["audit"]["model"] == "cheap-auditor"


def test_ui_shows_verified_and_unverified_badges():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    bar = (root / "desktop/src/components/ChatTopBar.tsx").read_text(encoding="utf-8")
    assert "verified" in bar and "unverified" in bar
    run_state = (root / "desktop/src/lib/api.ts").read_text(encoding="utf-8")
    assert "audit?: { verdict: string" in run_state
