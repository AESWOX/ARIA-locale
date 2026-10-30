"""R2/R3: PlanStep.command + отсев unix-команд в трёх точках (validate_plan, executor, loop).

R3(a) валидация плана; R3(b) после `_llm_generate_command` в executor; R3(c) в loop
до classify_shell_command/Approve. R3(c) отдельно закрывает пересечение
«опасная (SEC-2) × unix-only (R1)»: отклоняется, Approve и attention-item не созданы.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from aria import paths
from aria.core import loop as loop_mod
from aria.core import plan_validator as pv
from aria.core.loop import execute_agent_loop
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import init_db, session_scope
from aria.db.enums import TaskStatus
from aria.llm.providers.stub import StubProvider, final_answer, tool_call
from aria.llm.router import ProviderRouter

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


# ── R2 ─────────────────────────────────────────────────────────────────────

def test_planstep_has_command_field():
    step = pv.PlanStep(objective="list files", tool_ref="shell_execute", command="dir")
    assert step.command == "dir"
    assert pv.PlanStep(objective="x", tool_ref="file_read").command is None


# ── R3(a) ──────────────────────────────────────────────────────────────────

def test_plan_rejects_unix_command(monkeypatch):
    monkeypatch.setattr(pv, "is_windows_command", lambda cmd: False)
    monkeypatch.setattr(pv, "command_rejection_reason", lambda cmd: "unix_only_command")
    with pytest.raises(ValueError) as ei:
        pv.validate_plan([
            {"step_id": "s1", "objective": "clean", "tool_ref": "shell_execute", "command": "bash -c x"},
        ])
    assert "not runnable on Windows" in str(ei.value)


def test_plan_accepts_windows_command(monkeypatch):
    monkeypatch.setattr(pv, "is_windows_command", lambda cmd: True)
    steps = pv.validate_plan([
        {"step_id": "s1", "objective": "list", "tool_ref": "shell_execute", "command": "dir"},
    ])
    assert steps[0].command == "dir"


# ── R3(b) ──────────────────────────────────────────────────────────────────

def test_command_gen_rejects_unix(tmp_path, monkeypatch):
    from aria.core import executor as ex

    init_db(f"sqlite:///{(tmp_path / 't.db').as_posix()}", create_all=True)
    monkeypatch.setattr(ex, "is_windows_command", lambda cmd: False)
    monkeypatch.setattr(ex, "command_rejection_reason", lambda cmd: "unix_only_command")

    async def fake_cmd(router, *a, **k):
        return "bash -c 'rm -rf /'"

    monkeypatch.setattr(ex, "_llm_generate_command", fake_cmd)
    step = {"step_id": str(uuid.uuid4()), "tool_ref": "shell_execute", "objective": "clean"}
    result = asyncio.run(ex._execute_step_isolated(
        step, index=0, task_id=uuid.uuid4(), sandbox_root=str(tmp_path), router=object(),
    ))
    assert result["status"] == "error"
    assert "unix_only_command" in result["output_json"]["error"]


# ── R3(c) ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    router = ProviderRouter()
    stubs = {}
    for cls in ("premium_reasoning", "standard_reasoning", "subagent_execution"):
        s = StubProvider(provider_id=f"stub-{cls}")
        s.provider_class = cls
        router.register(s)
        stubs[cls] = s
    for _ in range(6):
        stubs["standard_reasoning"].push(final_answer("OK"))
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    with session_scope() as db:
        session = repo.create_session(db, title=f"p1-{uuid.uuid4().hex[:6]}")
        task = repo.create_task(db, session, role="coder", objective="clean the build dir")
        repo.set_task_status(db, task, TaskStatus.approved)
        task_id = task.id
    return router, stubs, str(sandbox), task_id


def _snapshot(task_id):
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        items = db.execute(select(m.AttentionItem).where(m.AttentionItem.task_id == task_id)).scalars().all()
        calls = [(c.tool_name, c.status.value, c.approval_item_id) for c in repo.list_tool_calls(db, task_id)]
        msgs = [msg.content for msg in repo.list_messages(db, task.session_id)]
        return task.status, items, calls, msgs


@pytest.mark.parametrize("cmd", ["rm -rf /", "bash -c 'rm -rf x'"])
def test_loop_rejects_dangerous_unix_before_approve(cmd, world, trap, monkeypatch):
    router, stubs, sandbox, task_id = world
    monkeypatch.setattr(loop_mod, "is_windows_command", lambda c: False)
    monkeypatch.setattr(loop_mod, "command_rejection_reason", lambda c: "unix_only_command")
    stubs["subagent_execution"].push(tool_call("shell_execute", {"command": cmd}))
    asyncio.run(execute_agent_loop(task_id, router, sandbox))

    status, items, calls, msgs = _snapshot(task_id)
    assert items == [], "unix-команда не должна создавать Approve/attention-item"
    assert status != TaskStatus.awaiting_attention
    assert not any(c[0] == "shell_execute" for c in calls), "инструмент не вызван"
    assert trap == [], "процесс не порождается"
    assert any("rejected: unix_only_command" in msg for msg in msgs), msgs
