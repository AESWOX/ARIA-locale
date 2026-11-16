"""0.2.1: persistent per-task iteration budget and the approval ledger.

Two guarantees:
  * ``iterations``, ``repeat_suppressed`` and the guardrail controller counters are stored in
    ``agent_state`` for the task, so ``resume_after_approval`` continues the budget instead of
    resetting it (and the step limit is not bypassed by approving repeatedly).
  * An explicit "allow always" approval is stored per (session, tool, args hash) with a TTL and
    later identical calls run without a new card; approvals per task are hard-capped.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from aria import paths
from aria.api.auth import require_runtime_token
from aria.config import get_settings
from aria.core import approval_ledger, approvals, loop, loop_state
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import ApprovalStatus, TaskStatus, ToolStatus
from aria.llm.providers.stub import StubProvider, final_answer, tool_call
from aria.llm.router import ProviderRouter
from aria.routers import sessions as sessions_router

SPAWNED: list[str] = []


class _FakeProc:
    returncode = 0

    async def communicate(self):
        return b"fake-out", b""

    def terminate(self):
        pass

    def kill(self):
        pass

    async def wait(self):
        return 0


@pytest.fixture(autouse=True)
def trap(monkeypatch):
    SPAWNED.clear()

    async def _spawn(command, **_kw):
        SPAWNED.append(command)
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _spawn)
    return SPAWNED


@pytest.fixture(autouse=True)
def _windows_runnable(monkeypatch):
    monkeypatch.setattr(loop, "is_windows_command", lambda command: True)


def _spawned_command(raw: str) -> str:
    if "-EncodedCommand" in raw:
        import base64

        return base64.b64decode(raw.rsplit(" ", 1)[-1]).decode("utf-16-le")
    return raw


def _world(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    router = ProviderRouter()
    providers = []
    for cls in ("premium_reasoning", "standard_reasoning", "subagent_execution", "free_tier_reasoning"):
        provider = StubProvider(provider_id=f"stub-{cls}")
        provider.provider_class = cls
        router.register(provider)
        providers.append(provider)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    with session_scope() as db:
        session = repo.create_session(db, title=f"budget-{uuid.uuid4().hex[:6]}")
        task = repo.create_task(db, session, role="coder", objective="budget and ledger")
        repo.set_task_status(db, task, TaskStatus.approved)
        return router, providers, str(sandbox), task.id, session.id


def _push(providers, response) -> None:
    for provider in providers:
        provider.push(response)


def _finals(providers, count: int = 6) -> None:
    for _ in range(count):
        _push(providers, final_answer("OK"))


def _state(task_id) -> tuple[TaskStatus, str | None]:
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        return task.status, task.error_code


def _items(task_id):
    with session_scope() as db:
        rows = db.execute(select(m.AttentionItem).where(m.AttentionItem.task_id == task_id)).scalars().all()
        return [(row.id, row.status) for row in rows]


def _pending_item(task_id):
    with session_scope() as db:
        rows = db.execute(select(m.AttentionItem).where(m.AttentionItem.task_id == task_id)).scalars().all()
        pending = [row for row in rows if row.status == ApprovalStatus.pending]
        assert len(pending) == 1, pending
        item = repo.get_attention_item(db, pending[0].id)
        db.expunge(item)
        return item


def _saved(task_id) -> dict:
    with session_scope() as db:
        return loop_state.load(db, task_id)


def _tool_calls(task_id):
    with session_scope() as db:
        return [(c.tool_name, c.status, dict(c.input_json or {})) for c in repo.list_tool_calls(db, task_id)]


def _approve_and_resume(router, providers, sandbox, task_id, pending_next):
    item = _pending_item(task_id)
    with session_scope() as db:
        approvals.resolve(db, repo.get_attention_item(db, item.id), approve=True)
        item = repo.get_attention_item(db, item.id)
        db.expunge(item)
    _push(providers, pending_next)
    asyncio.run(loop.resume_after_approval(task_id, router, sandbox, item))


# --------------------------------------------------------------- iteration budget persistence


def test_iteration_and_guardrail_state_survive_approval_pause(tmp_path, monkeypatch):
    router, providers, sandbox, task_id, _session_id = _world(tmp_path, monkeypatch)
    monkeypatch.setattr(get_settings(), "agent_max_llm_calls", 10)
    monkeypatch.setattr(get_settings(), "loop_max_iterations", 10)
    _push(providers, tool_call("file_read", {"path": "missing.txt"}))
    _push(providers, tool_call("shell_execute", {"command": "rm -rf build"}))

    asyncio.run(loop.execute_agent_loop(task_id, router, sandbox))

    state = _saved(task_id)
    assert state["iterations"] == 2, "two model calls happened before the owner was asked"
    from aria.core.guardrails import ToolCallSignature

    signature = ToolCallSignature.from_call("file_read", {"path": "missing.txt"})
    no_progress = {(entry[0], entry[1]): entry[3] for entry in state["guardrails"]["no_progress"]}
    assert no_progress.get((signature.tool_name, signature.args_hash)) == 1, state

    _approve_and_resume(router, providers, sandbox, task_id, tool_call("file_read", {"path": "missing.txt"}))
    _finals(providers)
    # the loop got its second model call after the pause (no early final answer was needed)
    assert _saved(task_id)["iterations"] >= 3
    after = _saved(task_id)
    no_progress_after = {
        (entry[0], entry[1]): entry[3] for entry in after["guardrails"]["no_progress"]
    }
    assert no_progress_after.get((signature.tool_name, signature.args_hash)) == 2, after


def test_step_limit_counts_model_calls_across_approvals(tmp_path, monkeypatch):
    router, providers, sandbox, task_id, _session_id = _world(tmp_path, monkeypatch)
    monkeypatch.setattr(get_settings(), "agent_max_llm_calls", 2)
    monkeypatch.setattr(get_settings(), "loop_max_iterations", 2)
    for command in ("rm -rf build1", "rm -rf build2", "rm -rf build3"):
        _push(providers, tool_call("shell_execute", {"command": command}))
    _finals(providers)

    asyncio.run(loop.execute_agent_loop(task_id, router, sandbox))
    assert len(SPAWNED) == 0
    assert _saved(task_id)["iterations"] == 1

    _approve_and_resume(
        router, providers, sandbox, task_id, tool_call("shell_execute", {"command": "rm -rf build2"})
    )
    status, _code = _state(task_id)
    assert status == TaskStatus.awaiting_attention
    item = _pending_item(task_id)
    with session_scope() as db:
        approvals.resolve(db, repo.get_attention_item(db, item.id), approve=True)
        item = repo.get_attention_item(db, item.id)
        db.expunge(item)
    asyncio.run(loop.resume_after_approval(task_id, router, sandbox, item))

    status, code = _state(task_id)
    assert status == TaskStatus.failed and code == "step_limit", (status, code)
    assert [c for c in (_spawned_command(cmd) for cmd in SPAWNED)] == ["rm -rf build1", "rm -rf build2"]
    assert _saved(task_id)["iterations"] == 3, "third model call must be counted, not restarted"


# --------------------------------------------------------------------- approval ledger


def test_allow_always_runs_identical_call_without_a_new_card(tmp_path, monkeypatch):
    router, providers, sandbox, task_id, session_id = _world(tmp_path, monkeypatch)
    command = "rm -rf build"
    with session_scope() as db:
        approval_ledger.record(db, session_id, "shell_execute", {"command": command}, mode="always")
    _push(providers, tool_call("shell_execute", {"command": command}))
    _push(providers, final_answer("done"))
    _finals(providers)

    asyncio.run(loop.execute_agent_loop(task_id, router, sandbox))

    assert _items(task_id) == [], "allow always: no second approval card"
    assert [_spawned_command(cmd) for cmd in SPAWNED] == [command]
    calls = _tool_calls(task_id)
    assert next(c for c in calls if c[0] == "shell_execute")[1] == ToolStatus.ok
    with session_scope() as db:
        assert approval_ledger.consume(db, session_id, "shell_execute", {"command": command}) == "always"


def test_allow_always_does_not_cover_other_arguments(tmp_path, monkeypatch):
    router, providers, sandbox, task_id, session_id = _world(tmp_path, monkeypatch)
    with session_scope() as db:
        approval_ledger.record(
            db, session_id, "shell_execute", {"command": "rm -rf build1"}, mode="always"
        )
    _push(providers, tool_call("shell_execute", {"command": "rm -rf build2"}))

    asyncio.run(loop.execute_agent_loop(task_id, router, sandbox))

    assert len(_items(task_id)) == 1
    assert SPAWNED == []


def test_allow_once_is_consumed_by_the_next_identical_call(tmp_path, monkeypatch):
    router, providers, sandbox, task_id, session_id = _world(tmp_path, monkeypatch)
    command = "rm -rf build"
    with session_scope() as db:
        approval_ledger.record(db, session_id, "shell_execute", {"command": command}, mode="once")
    _push(providers, tool_call("shell_execute", {"command": command}))
    _push(providers, final_answer("done"))
    _finals(providers)

    asyncio.run(loop.execute_agent_loop(task_id, router, sandbox))
    assert SPAWNED and _items(task_id) == [], "the stored one-shot approval lifts the card once"
    with session_scope() as db:
        assert approval_ledger.consume(db, session_id, "shell_execute", {"command": command}) is None


def test_expired_ledger_entry_does_not_lift_the_card(tmp_path, monkeypatch):
    router, providers, sandbox, task_id, session_id = _world(tmp_path, monkeypatch)
    command = "rm -rf build"
    with session_scope() as db:
        approval_ledger.record(
            db, session_id, "shell_execute", {"command": command}, mode="always", ttl_hours=-1
        )
    _push(providers, tool_call("shell_execute", {"command": command}))

    asyncio.run(loop.execute_agent_loop(task_id, router, sandbox))

    assert len(_items(task_id)) == 1
    assert SPAWNED == []


def test_approvals_per_task_are_hard_capped(tmp_path, monkeypatch):
    router, providers, sandbox, task_id, _session_id = _world(tmp_path, monkeypatch)
    monkeypatch.setattr(get_settings(), "agent_max_approvals_per_task", 1)
    _push(providers, tool_call("shell_execute", {"command": "rm -rf build1"}))
    _push(providers, tool_call("shell_execute", {"command": "rm -rf build2"}))
    _finals(providers)

    asyncio.run(loop.execute_agent_loop(task_id, router, sandbox))
    assert len(_items(task_id)) == 1

    item = _pending_item(task_id)
    with session_scope() as db:
        approvals.resolve(db, repo.get_attention_item(db, item.id), approve=True)
        item = repo.get_attention_item(db, item.id)
        db.expunge(item)
    asyncio.run(loop.resume_after_approval(task_id, router, sandbox, item))

    status, code = _state(task_id)
    assert status == TaskStatus.failed and code == "approval_limit", (status, code)
    assert len(_items(task_id)) == 1, "no approval card past the hard cap"
    assert SPAWNED == ["rm -rf build1"]


def test_approve_endpoint_stores_allow_always_for_the_session(tmp_path, monkeypatch):
    router, providers, sandbox, task_id, session_id = _world(tmp_path, monkeypatch)
    command = "rm -rf build"
    _push(providers, tool_call("shell_execute", {"command": command}))
    asyncio.run(loop.execute_agent_loop(task_id, router, sandbox))
    item = _pending_item(task_id)

    app = FastAPI()
    app.include_router(sessions_router.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    client = TestClient(app)
    response = client.post(f"/attention-items/{item.id}/approve", json={"mode": "always"})

    assert response.status_code == 200, response.text
    with session_scope() as db:
        assert approval_ledger.consume(db, session_id, "shell_execute", {"command": command}) == "always"


def test_approve_endpoint_defaults_to_allow_once(tmp_path, monkeypatch):
    router, providers, sandbox, task_id, session_id = _world(tmp_path, monkeypatch)
    command = "rm -rf build"
    _push(providers, tool_call("shell_execute", {"command": command}))
    asyncio.run(loop.execute_agent_loop(task_id, router, sandbox))
    item = _pending_item(task_id)

    app = FastAPI()
    app.include_router(sessions_router.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    client = TestClient(app)
    response = client.post(f"/attention-items/{item.id}/approve")

    assert response.status_code == 200, response.text
    with session_scope() as db:
        assert approval_ledger.consume(db, session_id, "shell_execute", {"command": command}) is None
