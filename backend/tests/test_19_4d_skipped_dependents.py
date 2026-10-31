"""19.4d D-22: шаг с упавшей зависимостью пропускается и не вызывает провайдера."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import aria.core.executor as ex
from aria.core import plan_validator as pv

S1 = "11111111-1111-1111-1111-111111111111"
S2 = "22222222-2222-2222-2222-222222222222"
S3 = "33333333-3333-3333-3333-333333333333"
S4 = "44444444-4444-4444-4444-444444444444"


def _env(plan_json, tmp_path, monkeypatch):
    from aria.config import get_settings
    from aria.db import models as m
    from aria.db.base import init_db, session_scope
    from sqlalchemy import select

    init_db(f"sqlite:///{(tmp_path / 'skip.db').as_posix()}", create_all=True)
    monkeypatch.setattr(get_settings(), "agent_sandbox_root", str(tmp_path / "sb"))
    with session_scope() as seed:
        sess = m.Session(title="skip", current_task_id=None)
        seed.add(sess)
        seed.flush()
        task = m.Task(session_id=sess.id, objective="проверь пропуск зависимых шагов")
        seed.add(task)
        seed.flush()
        seed.add(m.TaskPlan(task_id=task.id, plan_json=plan_json))
        task_id = task.id
    cm = session_scope()
    db = cm.__enter__()
    db._test_cm = cm
    task = db.get(m.Task, task_id)
    plan = db.execute(select(m.TaskPlan).where(m.TaskPlan.task_id == task_id)).scalar_one()
    return db, task, plan


class _Router:
    def __init__(self, answers):
        self.answers = list(answers)
        self.sent: list[list] = []

    async def route_chat(self, cls, messages, **kw):
        self.sent.append(messages)
        return SimpleNamespace(response=SimpleNamespace(text=self.answers.pop(0)))


def _file_read_step(step_id: str, depends_on=None) -> dict:
    return {
        "step_id": step_id,
        "objective": "прочитай contracts/TaskSpec.md",
        "role": "general",
        "tool_ref": "file_read",
        "path": "contracts/TaskSpec.md",
        "depends_on": [] if depends_on is None else depends_on,
    }


def _llm_step(step_id: str, depends_on, objective: str = "суммируй") -> dict:
    return {
        "step_id": step_id,
        "objective": objective,
        "role": "general",
        "tool_ref": "llm_task",
        "depends_on": depends_on,
    }


def test_dependent_step_is_skipped_without_provider_call(tmp_path, monkeypatch):
    plan = [_file_read_step(S1), _llm_step(S2, [S1])]
    db, task, p = _env(plan, tmp_path, monkeypatch)
    router = _Router(["итог"])

    calls = asyncio.run(ex._stage3_execute(db, task, p, router=router))

    assert [c["status"] for c in calls] == ["error", "skipped"]
    assert router.sent == [], "провайдер вызван для шага с упавшей зависимостью"
    assert calls[1]["output_json"]["skipped"] is True
    assert str(S1) in calls[1]["output_json"]["reason"]
    assert calls[1]["output_json"]["blocked_by"]


def test_skipped_toolcall_is_persisted(tmp_path, monkeypatch):
    from aria.db import models as m
    from sqlalchemy import select

    plan = [_file_read_step(S1), _llm_step(S2, [S1])]
    db, task, p = _env(plan, tmp_path, monkeypatch)
    router = _Router(["итог"])

    asyncio.run(ex._stage3_execute(db, task, p, router=router))

    rows = list(db.execute(select(m.ToolCall).where(m.ToolCall.task_id == task.id)).scalars())
    skipped = [r for r in rows if str(r.status) in ("skipped", "ToolStatus.skipped")]
    assert len(skipped) == 1
    assert (skipped[0].output_json or {}).get("skipped") is True


def test_independent_step_in_a_later_wave_still_runs(tmp_path, monkeypatch):
    plan = [
        _llm_step(S1, [], objective="первый"),
        _file_read_step(S2),
        _llm_step(S3, [S1], objective="второй"),
    ]
    db, task, p = _env(plan, tmp_path, monkeypatch)
    router = _Router(["один", "два"])

    calls = asyncio.run(ex._stage3_execute(db, task, p, router=router))

    assert [c["status"] for c in calls] == ["ok", "error", "ok"]
    assert len(router.sent) == 2, "независимый шаг волны 2 не исполнен"


def test_transitive_skip(tmp_path, monkeypatch):
    plan = [
        _file_read_step(S1),
        _llm_step(S2, [S1]),
        _llm_step(S3, [S2]),
        _llm_step(S4, [], objective="независимый"),
    ]
    db, task, p = _env(plan, tmp_path, monkeypatch)
    router = _Router(["независимый"])

    calls = asyncio.run(ex._stage3_execute(db, task, p, router=router))

    assert [c["status"] for c in calls] == ["error", "skipped", "skipped", "ok"]
    assert len(router.sent) == 1


def test_plan_step_status_accepts_skipped():
    assert pv.PlanStep(objective="x", tool_ref="list_vault", status="skipped").status == "skipped"


def test_tool_calls_follow_plan_order(tmp_path, monkeypatch):
    plan = [
        _llm_step(S1, [], objective="первый"),
        _llm_step(S2, [S1], objective="второй"),
        _llm_step(S3, [], objective="третий"),
    ]
    db, task, p = _env(plan, tmp_path, monkeypatch)
    router = _Router(["один", "два", "три"])

    calls = asyncio.run(ex._stage3_execute(db, task, p, router=router))

    assert [c["status"] for c in calls] == ["ok", "ok", "ok"]
    assert calls[0]["input_json"]["objective"] == "первый"
    assert calls[1]["input_json"]["objective"] == "второй"
    assert calls[2]["input_json"]["objective"] == "третий"


def test_unknown_dependency_is_not_skipped():
    deps = [set(), {0}]
    assert ex._failed_dependency_indices(1, deps, {}) == []
    assert ex._failed_dependency_indices(1, deps, {0: {"status": "error"}}) == [0]
    assert ex._failed_dependency_indices(1, deps, {0: {"status": "ok"}}) == []
