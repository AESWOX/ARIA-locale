"""B3: инструмент make_plan — Oracle-план через router с идемпотентностью и force_replan.

Каждый тест герметичен (conftest даёт свежую БД) и не ходит в сеть: план отдаёт
StubProvider, зарегистрированный как premium_reasoning."""
from __future__ import annotations

import asyncio
import json

from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.models import TaskPlan
from aria.llm.providers.stub import StubProvider, final_answer
from aria.llm.router import ProviderRouter
from aria.tools.registry import _make_plan


def _step(goal: str, tool: str = "read_note") -> dict:
    return {"objective": goal, "tool_ref": tool}


def _plan_json(steps: list[dict]) -> str:
    return json.dumps(steps, ensure_ascii=False)


def _router_with_plan(steps: list[dict]) -> ProviderRouter:
    router = ProviderRouter()
    premium = StubProvider(provider_id="stub-premium")
    premium.provider_class = "premium_reasoning"
    router.register(premium)
    premium.push(final_answer(_plan_json(steps)))
    return router


def _new_task() -> str:
    with session_scope() as db:
        session = repo.create_session(db, title="make-plan-session")
        task = repo.create_task(db, session, role="general", objective="построй план")
        return str(task.id)


def _plan_rows(task_id: str) -> list[TaskPlan]:
    import uuid

    with session_scope() as db:
        from sqlalchemy import select

        rows = db.execute(select(TaskPlan).where(TaskPlan.task_id == uuid.UUID(task_id))).scalars().all()
        return list(rows)


def test_make_plan_builds_plan_and_is_idempotent():
    task_id = _new_task()
    router = _router_with_plan([_step("шаг один"), _step("шаг два")])

    first = asyncio.run(_make_plan({}, router=router, task_id=task_id))

    assert first["status"] == "in_progress"
    assert len(first["plan"]) == 2
    assert first["version"] == 1

    # Второй вызов без новых scripted-ответов: если пойдёт в LLM, ответ не-JSON -> escalation.
    second = asyncio.run(_make_plan({}, router=router, task_id=task_id))
    assert second["status"] == "in_progress"
    assert len(second["plan"]) == 2
    assert second["version"] == 1

    rows = _plan_rows(task_id)
    assert len(rows) == 1
    assert rows[0].plan_json == first["plan"]


def test_make_plan_force_replan_updates_in_place():
    task_id = _new_task()
    router = _router_with_plan([_step("первая версия")])

    first = asyncio.run(_make_plan({}, router=router, task_id=task_id))
    assert len(first["plan"]) == 1

    premium = router.providers_by_class["premium_reasoning"][0]
    premium.push(final_answer(_plan_json([_step("вторая версия"), _step("третья версия")])))
    replanned = asyncio.run(_make_plan({"force_replan": True}, router=router, task_id=task_id))

    assert len(replanned["plan"]) == 2
    assert replanned["version"] == 2

    rows = _plan_rows(task_id)
    assert len(rows) == 1
    assert rows[0].version == 2
    assert rows[0].plan_json == replanned["plan"]


def test_make_plan_requires_task_id():
    router = _router_with_plan([_step("недостижим")])
    result = asyncio.run(_make_plan({}, router=router, task_id=None))
    assert "error" in result and "task_id" in result["error"]


def test_make_plan_without_router_returns_error():
    task_id = _new_task()
    result = asyncio.run(_make_plan({}, router=None, task_id=task_id))
    assert "error" in result and "no LLM provider" in result["error"]


def test_make_plan_unknown_task_returns_error():
    router = _router_with_plan([_step("недостижим")])
    import uuid

    result = asyncio.run(_make_plan({}, router=router, task_id=str(uuid.uuid4())))
    assert "error" in result and "not found" in result["error"]
