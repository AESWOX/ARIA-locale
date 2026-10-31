"""19.4c D-24: аргументы инструментов из плана доходят до handler'а (PlanStep.input)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from aria.core import executor
from aria.core import plan_validator as pv
from aria.db import repository as repo
from aria.db.base import session_scope


def _spec(name: str, handler, schema: dict):
    return SimpleNamespace(
        tool_name=name,
        handler=handler,
        risk_level=SimpleNamespace(value="low"),
        timeout_sec=5,
        description="test tool",
        input_schema=schema,
    )


def test_planstep_keeps_input_and_flat_arguments():
    step = pv.PlanStep(
        **{
            "tool_ref": "memory_search",
            "objective": "найди в памяти",
            "input": {"query": "logs"},
            "limit": 3,
        }
    )
    dumped = step.model_dump(mode="json")
    assert dumped["input"] == {"query": "logs"}
    assert dumped["limit"] == 3
    assert pv.PlanStep(tool_ref="list_vault", objective="x", input=None).input is None


def test_validate_plan_passes_input_to_handler_step():
    validated = pv.validate_plan(
        [
            {
                "step_id": "s1",
                "objective": "найди в памяти",
                "tool_ref": "memory_search",
                "input": {"query": "logs"},
            }
        ]
    )
    assert validated[0].input == {"query": "logs"}


def test_step_tool_input_merges_flat_schema_keys_only():
    spec = SimpleNamespace(input_schema={"properties": {"query": {}, "limit": {}}, "required": ["query"]})
    step = {
        "step_id": "s1",
        "objective": "найди",
        "role": "coder",
        "tool_ref": "memory_search",
        "depends_on": [],
        "query": "flat",
        "input_json": {"limit": 5},
        "input": {"query": "explicit"},
    }
    assert pv.step_tool_input(step, spec) == {"query": "explicit", "limit": 5}

    grep_spec = SimpleNamespace(input_schema={"properties": {"path": {}, "glob": {}, "pattern": {}}})
    assert pv.step_tool_input(
        {"objective": "x", "tool_ref": "file_grep", "path": "src"}, grep_spec
    ) == {"path": "src"}

    log_spec = SimpleNamespace(input_schema={"properties": {"title": {}, "decision": {}, "status": {}}})
    assert pv.step_tool_input(
        {"tool_ref": "log_decision", "status": "accepted", "title": "t", "decision": "d"}, log_spec
    ) == {"title": "t", "decision": "d"}
    assert pv.step_tool_input(
        {"tool_ref": "log_decision", "input": {"status": "accepted"}}, log_spec
    ) == {"status": "accepted"}


@pytest.mark.anyio
async def test_executor_memory_search_receives_plan_arguments(monkeypatch):
    import json

    seen: dict = {}

    async def handler(*, input_json, timeout_sec, sandbox_root):
        seen.update(input_json)
        return {"items": [], "total": 0}

    spec = _spec(
        "memory_search",
        handler,
        {"properties": {"query": {}, "limit": {}}, "required": ["query"]},
    )
    monkeypatch.setattr("aria.tools.registry.get_tool", lambda name: spec)

    from aria.core import toolhooks

    decision = SimpleNamespace(blocked=False, reason="")

    async def _pre(*args, **kwargs):
        return decision, None

    async def _post(*args, **kwargs):
        return decision

    monkeypatch.setattr(toolhooks, "pre_tool", _pre)
    monkeypatch.setattr(toolhooks, "post_tool", _post)

    class PlanRouter:
        async def route_chat(self, cls, messages, **kw):
            steps = [
                {
                    "step_id": "s-1",
                    "objective": "найди в памяти",
                    "role": "general",
                    "tool_ref": "memory_search",
                    "input": {"query": "logs"},
                }
            ]
            return SimpleNamespace(response=SimpleNamespace(text=json.dumps(steps), tool_calls=[]))

    with session_scope() as db:
        session = repo.create_session(db, "d24", active_role="general")
        task = repo.create_task(db, session, role="general", objective="найди в памяти")

        plan, _flags = await executor._stage2_plan(db, task, {}, PlanRouter())
        raw = await executor._stage3_execute(db, task, plan, router=SimpleNamespace())

    assert raw and raw[0]["status"] == "ok", raw
    assert seen == {"query": "logs"}


def test_plan_validation_rejects_missing_required_arguments():
    with pytest.raises(ValueError) as exc:
        pv.validate_plan(
            [{"step_id": "s1", "objective": "найди в памяти", "tool_ref": "memory_search"}]
        )
    message = str(exc.value)
    assert "memory_search" in message and "query" in message


def test_plan_validation_allows_tools_without_required_arguments():
    assert pv.validate_plan([{"step_id": "s1", "objective": "список vault", "tool_ref": "list_vault"}])


def test_plan_validation_skips_skill_and_unknown_tools():
    pv.validate_plan([{"step_id": "s1", "objective": "x", "skill_ref": "some-skill"}])
    pv.validate_plan([{"step_id": "s2", "objective": "x", "tool_ref": "llm_task"}])
    pv.validate_plan([{"step_id": "s3", "objective": "x", "tool_ref": "no_such_tool"}])


def test_web_fetch_url_from_step_input_is_reachable():
    validated = pv.validate_plan(
        [
            {
                "step_id": "s1",
                "objective": "открой страницу",
                "tool_ref": "web_fetch",
                "input": {"url": "https://example.com"},
            }
        ]
    )
    assert pv.resolve_web_fetch_url(validated[0].model_dump(mode="json")) == "https://example.com"


def test_vision_analyze_oneof_schema_still_validated():
    validated = pv.validate_plan(
        [
            {
                "step_id": "s1",
                "objective": "опиши картинку",
                "tool_ref": "vision_analyze",
                "input": {"file_path": "images/a.png"},
            }
        ]
    )
    assert validated[0].input == {"file_path": "images/a.png"}
