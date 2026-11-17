"""STOP-1: Stop прерывает агентскую задачу, задача остаётся живой и продолжается.

Все проверки без сети и без реальных процессов: LLM-провайдеры висят на sleep,
шаг инструмента подменяется медленной корутиной, shell не запускается.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from aria.api.auth import require_runtime_token
from aria.core import loop, loop_state, stop_registry, stopctl
from aria.core.events import event_bus
from aria.core.taskrunner import TaskRunner
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import ApprovalStatus, AttentionType, TaskStatus, ToolStatus
from aria.llm.providers.base import ChatMessage, LlmProvider, LlmResponse
from aria.llm.providers.stub import final_answer, tool_call
from aria.llm.router import ProviderRouter
from aria.routers import chat as chat_router
from aria.routers import sessions as sessions_router
from aria.routers import tasks as tasks_router

STOP_BUDGET_SEC = 1.0
SLEEP_FOREVER = 60


class HangingLlm(LlmProvider):
    provider_id = "hang-stub"
    provider_class = "standard_reasoning"

    async def check_connectivity(self, timeout_sec) -> bool:  # pragma: no cover
        return True

    async def chat(self, messages: list[ChatMessage], tools, timeout_sec) -> LlmResponse:
        await asyncio.sleep(SLEEP_FOREVER)
        return LlmResponse(text="never")


class SequenceLlm(LlmProvider):
    """Отдаёт заготовки по очереди; ``"hang"`` — висит до Stop."""

    provider_id = "seq-stub"
    provider_class = "standard_reasoning"

    def __init__(self, items):
        self.items = list(items)
        self.calls = 0

    async def check_connectivity(self, timeout_sec) -> bool:  # pragma: no cover
        return True

    async def chat(self, messages: list[ChatMessage], tools, timeout_sec) -> LlmResponse:
        self.calls += 1
        item = self.items.pop(0) if self.items else final_answer("done")
        if item == "hang":
            await asyncio.sleep(SLEEP_FOREVER)
        return item


@pytest.fixture(autouse=True)
def _quiet_vault(monkeypatch):
    monkeypatch.setattr("aria.storage.obsidian_vault.relevant_block", lambda *a, **k: "")


@pytest.fixture(autouse=True)
def _reset_stop_registry():
    stop_registry.reset()
    yield
    stop_registry.reset()


def _router(provider) -> ProviderRouter:
    router = ProviderRouter()
    router.register(provider)
    return router


def _make_task(
    status: TaskStatus = TaskStatus.approved,
    role: str = "general",
    parent_task_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    with session_scope() as db:
        session = repo.create_session(db, title=f"stop-{uuid.uuid4().hex[:6]}", active_role=role)
        task = repo.create_task(db, session, role=role, objective="stop test", parent_task_id=parent_task_id)
        repo.set_task_status(db, task, TaskStatus.approved)
        if status in (TaskStatus.in_progress, TaskStatus.under_audit, TaskStatus.awaiting_attention):
            repo.set_task_status(db, task, TaskStatus.in_progress)
        if status != TaskStatus.in_progress:
            repo.set_task_status(db, task, status)
        return session.id, task.id


def _status_of(task_id: uuid.UUID) -> str:
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        return task.status.value if hasattr(task.status, "value") else str(task.status)


def _saved(task_id: uuid.UUID) -> dict:
    with session_scope() as db:
        return loop_state.load(db, task_id)


def _wait_until(predicate, timeout: float = 10.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


async def _wait_async(predicate, timeout: float = 10.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


def _app(*routers) -> FastAPI:
    app = FastAPI()
    for r in routers:
        app.include_router(r)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    return app


class _FakeRequest:
    def __init__(self, runner):
        self.app = SimpleNamespace(state=SimpleNamespace(task_runner=runner, router=None))


# ---------------------------------------------------------------- endpoint / runner


def test_stop_endpoint_reaches_agent_task():
    session_id, task_id = _make_task()
    runner = TaskRunner(_router(HangingLlm()), "/tmp/aria_stop_test", max_workers=1)
    app = _app(chat_router.router)
    app.state.task_runner = runner

    async def drive():
        await runner.start()
        try:
            runner.submit(task_id, mode="agent", source="test")
            assert await _wait_async(lambda: str(task_id) in runner.status()["running"]), "task never started"
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://aria") as client:
                response = await client.post(f"/chat/sessions/{session_id}/stop")
            assert response.status_code == 200
            assert response.json() == {"ok": True, "stopped": True}
            assert await _wait_async(lambda: str(task_id) not in runner.status()["running"]), "task did not stop"
        finally:
            await runner.stop()

    asyncio.run(drive())
    assert _status_of(task_id) != "cancelled"
    assert _saved(task_id)["stopped"] is True


def test_stop_during_llm_call(monkeypatch):
    _session_id, task_id = _make_task()
    runner = TaskRunner(_router(HangingLlm()), "/tmp/aria_stop_test", max_workers=1)
    events: list[str] = []

    async def drive():
        await runner.start()
        try:
            queue = event_bus.subscribe()
            runner.submit(task_id, mode="agent", source="test")
            assert await _wait_async(lambda: str(task_id) in runner.status()["running"])
            t0 = time.monotonic()
            assert runner.request_stop(task_id) is True
            assert await _wait_async(lambda: str(task_id) not in runner.status()["running"]), "stop took too long"
            took = time.monotonic() - t0
            while not queue.empty():
                events.append(queue.get_nowait()["event_type"])
            event_bus.unsubscribe(queue)
            return took
        finally:
            await runner.stop()

    took = asyncio.run(drive())
    assert took <= STOP_BUDGET_SEC, f"Stop took {took:.2f}s"
    assert _status_of(task_id) != "cancelled"
    assert "task.finished" not in events and "task.cancelled" not in events
    assert _saved(task_id)["stopped"] is True


def test_stop_during_tool_step(monkeypatch):
    _session_id, task_id = _make_task()
    provider = SequenceLlm([tool_call("file_read", {"path": "x.txt"})])
    loop_router = _router(provider)
    started = asyncio.Event()
    calls = 0

    async def fake_tool(*_a, **_kw):
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.sleep(SLEEP_FOREVER)
        return {}, ToolStatus.ok, None, None

    monkeypatch.setattr(loop, "_execute_tool", fake_tool)

    async def drive():
        task = asyncio.create_task(loop.execute_agent_loop(task_id, loop_router, "/tmp/aria_stop_test"))
        await asyncio.wait_for(started.wait(), timeout=10)
        t0 = time.monotonic()
        stop_registry.request(task_id)
        await asyncio.wait_for(task, timeout=5)
        return time.monotonic() - t0

    took = asyncio.run(drive())
    assert took <= STOP_BUDGET_SEC, f"Stop during tool took {took:.2f}s"
    assert calls == 1, "no tool call may start after Stop"
    assert _saved(task_id)["stopped"] is True
    assert _status_of(task_id) != "cancelled"


def test_stop_between_iterations(monkeypatch):
    _session_id, task_id = _make_task()
    provider = SequenceLlm([tool_call("file_read", {"path": "x.txt"}), final_answer("must not run")])
    loop_router = _router(provider)

    async def fake_tool(*_a, **_kw):
        stop_registry.request(task_id)
        return {"content": "x"}, ToolStatus.ok, None, None

    monkeypatch.setattr(loop, "_execute_tool", fake_tool)

    asyncio.run(asyncio.wait_for(loop.execute_agent_loop(task_id, loop_router, "/tmp/aria_stop_test"), timeout=10))

    assert provider.calls == 1, "the model must not be called after the boundary Stop"
    assert _saved(task_id)["stopped"] is True


def test_stop_stops_children(monkeypatch):
    monkeypatch.setattr("aria.core.taskrunner.available_ram_mb", lambda: None)
    _parent_session, parent_id = _make_task()
    children = []
    for _ in range(3):
        _child_session, child_id = _make_task(parent_task_id=parent_id)
        children.append(child_id)
    runner = TaskRunner(_router(HangingLlm()), "/tmp/aria_stop_test", max_workers=4)

    async def drive():
        await runner.start()
        try:
            for tid in [parent_id, *children]:
                runner.submit(tid, mode="agent", source="test")
            for tid in [parent_id, *children]:
                assert await _wait_async(lambda t=tid: str(t) in runner.status()["running"]), f"{tid} never started"
            t0 = time.monotonic()
            assert runner.request_stop(parent_id) is True
            assert await _wait_async(lambda: not runner.status()["running"]), "children did not stop"
            return time.monotonic() - t0
        finally:
            await runner.stop()

    took = asyncio.run(drive())
    assert took <= STOP_BUDGET_SEC, f"Stop of children took {took:.2f}s"
    for tid in [parent_id, *children]:
        assert _saved(tid)["stopped"] is True, tid


def test_stop_persists_loop_budget(monkeypatch):
    _session_id, task_id = _make_task()
    provider = SequenceLlm([tool_call("file_read", {"path": "x.txt"}), "hang"])
    loop_router = _router(provider)

    async def fake_tool(*_a, **_kw):
        return {"content": "x"}, ToolStatus.ok, None, None

    monkeypatch.setattr(loop, "_execute_tool", fake_tool)

    async def drive():
        task = asyncio.create_task(loop.execute_agent_loop(task_id, loop_router, "/tmp/aria_stop_test"))
        assert await _wait_async(lambda: provider.calls >= 2), "second model call did not start"
        stop_registry.request(task_id)
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(drive())
    state = _saved(task_id)
    assert state["stopped"] is True
    assert state["iterations"] == 2
    assert "guardrails" in state and "repeat_suppressed" in state


def test_stopped_task_not_resumed_on_restart():
    _session_id, task_id = _make_task(status=TaskStatus.in_progress)
    with session_scope() as db:
        loop_state.set_stopped(db, task_id, True)
    runner = TaskRunner(ProviderRouter(), "/tmp/aria_stop_test", max_workers=1)
    submitted: list[uuid.UUID] = []
    runner.submit = lambda tid, **kw: submitted.append(tid) or {"queued": True}  # type: ignore[method-assign]

    asyncio.run(runner.resume_pending())
    assert task_id not in submitted


def test_stopped_task_does_not_block_session():
    session_id, task_id = _make_task(status=TaskStatus.in_progress)
    with session_scope() as db:
        loop_state.set_stopped(db, task_id, True)
    runner = TaskRunner(ProviderRouter(), "/tmp/aria_stop_test", max_workers=1)

    result = asyncio.run(
        chat_router._auto_route_to_agent(session_id, {"content": "а ещё добавь тест"}, _FakeRequest(runner))
    )
    assert result is not None and result["mode"] == "agent", result
    assert result["task_id"] == str(task_id)
    with session_scope() as db:
        session = repo.get_session(db, session_id)
        assert session.current_task_id == task_id
        assert len(db.execute(select(m.Task).where(m.Task.session_id == session_id)).scalars().all()) == 1


def test_next_message_after_stop_continues_task():
    session_id, task_id = _make_task(status=TaskStatus.in_progress)
    with session_scope() as db:
        loop_state.save(db, task_id, {"iterations": 5, "repeat_suppressed": 1, "approvals": 2, "stopped": True})
    runner = TaskRunner(ProviderRouter(), "/tmp/aria_stop_test", max_workers=1)
    app = _app(sessions_router.router)
    app.state.task_runner = runner
    app.state.router = ProviderRouter()

    async def drive():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://aria") as client:
            return await client.post(
                f"/sessions/{session_id}/messages",
                json={"content": "а ещё сделай X", "mode": "agent", "role": "user"},
            )

    response = asyncio.run(drive())
    assert response.status_code == 200, response.text
    with session_scope() as db:
        session = repo.get_session(db, session_id)
        tasks = db.execute(select(m.Task).where(m.Task.session_id == session_id)).scalars().all()
        assert [t.id for t in tasks] == [task_id], "the same task must continue, no new task"
        assert session.current_task_id == task_id
    state = _saved(task_id)
    assert state["iterations"] == 5 and state["stopped"] is False


def test_next_message_after_stop_is_seen_by_model():
    session_id, _task_id = _make_task(status=TaskStatus.in_progress)
    with session_scope() as db:
        session = repo.get_session(db, session_id)
        msg = repo.append_message(db, session, role="user", content="а ещё сделай X")
        msg.content_json = {"resume_after_stop": True}
    with session_scope() as db:
        history = repo.list_messages_for_prompt(db, session_id)
    messages = loop._build_messages("system", history)
    user = [m for m in messages if m.role == "user" and "а ещё сделай X" in m.content]
    assert user and user[-1].content.startswith("[Уточнение владельца после остановки]")
    stored = [m.content for m in history if m.role == "user"]
    assert "а ещё сделай X" in stored, "stored text must stay unchanged"


def test_stop_and_resume_callable_without_http():
    session_id, task_id = _make_task()
    runner = TaskRunner(_router(HangingLlm()), "/tmp/aria_stop_test", max_workers=1)
    events: list[dict] = []

    async def drive():
        await runner.start()
        queue = event_bus.subscribe()
        try:
            runner.submit(task_id, mode="agent", source="test")
            assert await _wait_async(lambda: str(task_id) in runner.status()["running"])
            assert stopctl.stop_session_task(session_id) is True
            assert await _wait_async(lambda: str(task_id) not in runner.status()["running"])
            while not queue.empty():
                events.append(queue.get_nowait())
            assert stop_registry.is_set(task_id) is True
            assert stopctl.resume_stopped_task(session_id, "дополнение владельца") is True
            assert stop_registry.is_set(task_id) is False, "stale stop signal must be cleared on resume"
            assert await _wait_async(lambda: str(task_id) in runner.status()["running"]), "not resumed"
        finally:
            event_bus.unsubscribe(queue)
            await runner.stop()

    asyncio.run(drive())
    stopped_events = [e for e in events if e.get("event_type") == "task.stopped"]
    assert stopped_events and stopped_events[0].get("session_id") == str(session_id)
    assert _saved(task_id)["stopped"] is False


def test_stop_signal_cleared_on_submit():
    _session_id, task_id = _make_task()
    runner = TaskRunner(ProviderRouter(), "/tmp/aria_stop_test", max_workers=1)
    stop_registry.request(task_id)
    assert stop_registry.is_set(task_id) is True
    runner.submit(task_id, mode="agent", source="test")
    assert stop_registry.is_set(task_id) is False


def test_stop_idempotent_and_no_active_task():
    session_id, _task_id = _make_task(status=TaskStatus.approved)
    runner = TaskRunner(ProviderRouter(), "/tmp/aria_stop_test", max_workers=1)
    app = _app(chat_router.router)
    app.state.task_runner = runner

    async def drive():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://aria") as client:
            first = await client.post(f"/chat/sessions/{session_id}/stop")
            second = await client.post(f"/chat/sessions/{session_id}/stop")
        return first, second

    first, second = asyncio.run(drive())
    assert first.status_code == 200 and first.json() == {"ok": True, "stopped": False}
    assert second.status_code == 200 and second.json() == {"ok": True, "stopped": False}


def test_stop_while_awaiting_attention_keeps_card():
    session_id, task_id = _make_task(status=TaskStatus.approved)
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        session = repo.get_session(db, session_id)
        item = repo.create_attention_item(
            db,
            type_=AttentionType.high_risk_shell,
            title="Approve",
            body_md="x",
            session=session,
            task=task,
            payload_json={"command": "rm -rf x"},
        )
        repo.set_task_status(db, task, TaskStatus.in_progress)
        repo.set_task_status(db, task, TaskStatus.awaiting_attention)
        item_id = item.id
    assert stopctl.stop_session_task(session_id) is False
    with session_scope() as db:
        assert repo.get_attention_item(db, item_id).status == ApprovalStatus.pending
    assert _saved(task_id).get("stopped") is not True


def test_stop_under_audit_returns_false():
    session_id, task_id = _make_task(status=TaskStatus.under_audit)
    runner = TaskRunner(ProviderRouter(), "/tmp/aria_stop_test", max_workers=1)

    async def drive():
        await runner.start()
        try:
            return stopctl.stop_session_task(session_id)
        finally:
            await runner.stop()

    assert asyncio.run(drive()) is False
    assert stop_registry.is_set(task_id) is False
    assert _saved(task_id).get("stopped") is not True


def test_session_run_and_children_report_stopped():
    session_id, parent_id = _make_task(status=TaskStatus.in_progress)
    _child_session, child_id = _make_task(status=TaskStatus.in_progress, parent_task_id=parent_id)
    with session_scope() as db:
        loop_state.set_stopped(db, parent_id, True)
        loop_state.set_stopped(db, child_id, True)
    app = _app(sessions_router.router, tasks_router.router)

    async def drive():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://aria") as client:
            run = await client.get(f"/sessions/{session_id}/run")
            children = await client.get(f"/tasks/{parent_id}/children")
        return run, children

    run, children = asyncio.run(drive())
    assert run.status_code == 200 and run.json()["stopped"] is True
    assert children.status_code == 200
    assert [c["stopped"] for c in children.json()] == [True]
