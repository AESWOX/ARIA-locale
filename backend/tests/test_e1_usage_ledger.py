"""E1: журнал llm_usage — одна строка на успешный вызов, назначение, токены, миграция."""
from __future__ import annotations

import ast
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from aria.api.auth import require_runtime_token
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.llm.providers.base import LlmProvider, LlmResponse, StreamChunk
from aria.llm.router import ProviderRouter
from aria.routers import chat as chat_mod

USAGE = {
    "prompt_tokens": 1000,
    "completion_tokens": 500,
    "prompt_cache_hit_tokens": 700,
    "prompt_cache_miss_tokens": 300,
    "completion_tokens_details": {"reasoning_tokens": 42},
}


class FakeLlm(LlmProvider):
    def __init__(self, provider_id: str = "ledger-prov", text: str = "ok", usage: dict | None = None):
        self.provider_id = provider_id
        self.provider_class = "free_tier_reasoning"
        self.model = "ledger-model"
        self._text = text
        self.usage = dict(USAGE if usage is None else usage)

    async def check_connectivity(self, timeout_sec):
        return True

    async def chat(self, messages, tools, timeout_sec):
        return LlmResponse(text=self._text, usage=dict(self.usage))

    async def stream(self, messages, tools, timeout_sec):
        # Настоящий стрим: дельты + финальный чанк с usage (иначе чат уйдёт в fallback).
        if self._text:
            yield StreamChunk(text=self._text, final=False)
        yield StreamChunk(text="", final=True, usage=dict(self.usage))


def _router(*providers: LlmProvider) -> ProviderRouter:
    router = ProviderRouter()
    for provider in providers:
        router.register(provider)
    return router


def _rows() -> list[m.LlmUsage]:
    with session_scope() as db:
        return list(db.execute(select(m.LlmUsage).order_by(m.LlmUsage.created_at)).scalars())


@pytest.mark.anyio
async def test_route_chat_records_usage_row():
    router = _router(FakeLlm())
    sid = uuid.uuid4()
    result = await router.route_chat(
        "free_tier_reasoning", [], [], purpose="chat", session_id=sid
    )
    assert result.response.text == "ok"

    rows = _rows()
    assert len(rows) == 1
    row = rows[0]
    assert (row.provider_id, row.model, row.purpose) == ("ledger-prov", "ledger-model", "chat")
    assert (row.prompt_hit, row.prompt_miss, row.prompt_total) == (700, 300, 1000)
    assert (row.completion, row.reasoning) == (500, 42)
    assert row.session_id == sid and row.task_id is None
    assert row.cost_usd is None and row.tier == "n/a" and row.estimated is False


@pytest.mark.anyio
async def test_usage_without_cache_fields():
    router = _router(FakeLlm(usage={"prompt_tokens": 10, "completion_tokens": 5}))
    await router.route_chat("free_tier_reasoning", [], [], purpose="chat")

    row = _rows()[0]
    assert (row.prompt_hit, row.prompt_miss) == (None, None)
    assert (row.prompt_total, row.completion, row.reasoning) == (10, 5, None)


@pytest.mark.anyio
async def test_no_usage_writes_no_row(caplog):
    router = _router(FakeLlm(usage={}))
    await router.route_chat("free_tier_reasoning", [], [], purpose="chat")

    assert _rows() == []
    assert "llm_usage_missing" in caplog.text


@pytest.mark.anyio
async def test_write_failure_does_not_break_response(monkeypatch, caplog):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("aria.db.base.session_scope", boom)
    router = _router(FakeLlm())
    result = await router.route_chat("free_tier_reasoning", [], [], purpose="chat")

    assert result.response.text == "ok"
    assert "llm_usage_write_failed" in caplog.text


@pytest.mark.anyio
async def test_preferred_empty_falls_through_to_one_row():
    empty = FakeLlm("empty-prov", text="")
    good = FakeLlm("good-prov", text="answer")
    router = _router(empty, good)

    result = await router.route_chat(
        "free_tier_reasoning",
        [],
        [],
        prefer_provider_id="empty-prov",
        reject_empty=True,
        resilient=True,
        purpose="chat",
    )

    assert result.response.text == "answer"
    rows = _rows()
    assert len(rows) == 1 and rows[0].provider_id == "good-prov"


def _client(provider: LlmProvider) -> TestClient:
    app = FastAPI()
    app.include_router(chat_mod.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    app.state.router = _router(provider)
    return TestClient(app)


def test_stream_records_ledger_row():
    client = _client(FakeLlm("stream-prov"))
    sid = client.post("/chat/sessions", json={}).json()["session_id"]

    response = client.post(f"/chat/sessions/{sid}/send", json={"content": "hi", "stream": True})

    assert response.status_code == 200, response.text
    rows = [row for row in _rows() if row.purpose == "chat"]
    assert len(rows) == 1 and rows[0].provider_id == "stream-prov"
    assert rows[0].prompt_total == 1000


@pytest.mark.anyio
async def test_llm_task_step_records_subagent_usage(monkeypatch):
    from aria.core import executor, toolhooks

    router = _router(FakeLlm("ledger-prov", text="step done"))
    decision = SimpleNamespace(blocked=False, reason="")

    async def _pre(*args, **kwargs):
        return decision, None

    async def _post(*args, **kwargs):
        return decision

    monkeypatch.setattr(toolhooks, "pre_tool", _pre)
    monkeypatch.setattr(toolhooks, "post_tool", _post)
    with session_scope() as db:
        session = repo.create_session(db, "e1-task", active_role="general")
        task = repo.create_task(db, session, role="general", objective="суммируй")
        task_id = task.id

    step = {"step_id": "s-1", "objective": "суммируй", "role": "general", "tool_ref": "llm_task"}
    out = await executor._execute_step_isolated(
        step, index=0, task_id=task_id, sandbox_root="C:/tmp", router=router
    )

    assert out["status"] == "ok", out
    rows = [row for row in _rows() if row.task_id == task_id]
    assert len(rows) == 1 and rows[0].purpose == "subagent"


def test_every_route_chat_call_passes_purpose():
    offenders = []
    root = Path(__file__).resolve().parents[1] / "aria"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "route_chat":
                if not any(k.arg == "purpose" for k in node.keywords):
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == [], f"route_chat без purpose: {offenders}"


def test_migration_upgrade_and_downgrade(tmp_path):
    from alembic import command
    from alembic.config import Config

    # Без alembic.ini: fileConfig() переконфигурирует logging и ломает caplog
    # в последующих тестах того же процесса.
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    cfg.attributes["dsn_explicit"] = f"sqlite:///{(tmp_path / 'mig.db').as_posix()}"

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "e3f5a6b7c8d9")
    command.upgrade(cfg, "head")

    import sqlalchemy as sa

    engine = sa.create_engine(cfg.get_main_option("sqlalchemy.url"))
    columns = {c["name"] for c in sa.inspect(engine).get_columns("llm_usage")}
    assert {"prompt_hit", "prompt_miss", "prompt_total", "cost_usd", "purpose"} <= columns
