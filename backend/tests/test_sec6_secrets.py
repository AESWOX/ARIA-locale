"""SEC-6: секрет не доходит ни до модели, ни до shell, ни до экспорта, ни в логи."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria import secretguard
from aria.api.auth import require_runtime_token
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import ToolStatus
from aria.http_utils import render_session_export_markdown
from aria.llm.providers.base import LlmProvider, LlmResponse
from aria.llm.router import ProviderRouter
from aria.logredact import redact
from aria.routers.chat import router as chat_router

BOT = "123456789:" + "A" * 35
SK = "sk-" + "a" * 24
GOOGLE = "AIza" + "B" * 35
GROQ = "gsk_" + "c" * 30
BEARER = "Bearer " + "d" * 24


class _Fake(LlmProvider):
    def __init__(self) -> None:
        self.provider_id = "fake-gemini"
        self.provider_class = "free_tier_reasoning"
        self.calls = 0

    async def check_connectivity(self, timeout_sec):
        return True

    async def chat(self, messages, tools, timeout_sec):
        self.calls += 1
        return LlmResponse(text="ok")


def _client():
    llm = ProviderRouter()
    provider = _Fake()
    llm.register(provider)
    app = FastAPI()
    app.include_router(chat_router)
    app.state.router = llm
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    return TestClient(app), provider


def test_mask_secrets_covers_detector_patterns():
    text = f"key {SK} google {GOOGLE} groq {GROQ} auth {BEARER} bot {BOT}"
    out = secretguard.mask_secrets(text)
    for value in (SK, GOOGLE, GROQ, BOT, "d" * 24):
        assert value not in out
    assert out.count(secretguard.MASK) >= 5


def test_mask_secrets_covers_assignments():
    out = secretguard.mask_secrets("PASSWORD = hunter2\nTOKEN: abcdef123456\napi_key=supersecret")
    for value in ("hunter2", "abcdef123456", "supersecret"):
        assert value not in out
    assert "PASSWORD" in out and "TOKEN" in out and "api_key" in out


def test_mask_obj_recurses_into_args_and_output():
    payload = {"command": f"curl -H '{BEARER}'", "nested": [{"token": SK}], "ok": 1}
    masked = secretguard.mask_obj(payload)
    assert secretguard.contains_secret(str(masked)) is False
    assert masked["ok"] == 1


def test_chat_secret_is_masked_not_sent_to_model_and_not_stored():
    client, provider = _client()
    sid = client.post("/chat/sessions", json={}).json()["session_id"]

    body = client.post(
        f"/chat/sessions/{sid}/send", json={"content": f"мой ключ {SK}"}
    ).json()

    assert body["mode"] == "secret_redacted"
    assert provider.calls == 0
    assert SK not in str(body)
    assert "Settings" in body["assistant"]["content"]
    with session_scope() as db:
        messages = repo.list_messages(db, __import__("uuid").UUID(sid), limit=10)
    stored = "\n".join(msg.content or "" for msg in messages)
    assert SK not in stored
    assert secretguard.MASK in stored


def test_tool_call_args_and_output_are_masked_in_db():
    with session_scope() as db:
        session = repo.create_session(db, title="sec6-tool")
        call = repo.start_tool_call(
            db, session, None, "shell_execute", "general", "high",
            {"command": f"echo {SK}"},
        )
        repo.finish_tool_call(db, call, m.ToolStatus.ok if hasattr(m, "ToolStatus") else __import__("aria.db.enums", fromlist=["ToolStatus"]).ToolStatus.ok,
                              output_json={"stdout": f"leaked {GOOGLE}"})
        raw = str(call.input_json) + str(call.output_json)

    assert SK not in raw and GOOGLE not in raw
    assert secretguard.MASK in raw


def test_session_export_has_no_secret():
    with session_scope() as db:
        session = repo.create_session(db, title="sec6-export")
        session.title = f"title {BOT}"
        repo.append_message(db, session, role="user", content=f"here {SK}")
        task = m.Task(session_id=session.id, objective=f"do {GROQ}", role="general")
        db.add(task)
        db.flush()
        call = repo.start_tool_call(
            db, session, task, "web_search", "general", "low",
            {"query": f"q {GOOGLE}"},
        )
        repo.finish_tool_call(db, call, __import__("aria.db.enums", fromlist=["ToolStatus"]).ToolStatus.ok,
                              output_json={"result": f"r {BEARER}"})
        text = render_session_export_markdown(
            session, task, repo.list_messages(db, session.id), [call], []
        )

    for value in (SK, GOOGLE, GROQ, BOT, "d" * 24):
        assert value not in text


def test_logredact_masks_bare_bot_token_and_groq_key():
    out = redact(f"send https://api.telegram.org/bot{BOT}/x and {GROQ}")
    assert "A" * 35 not in out and "c" * 30 not in out


def test_diagnostics_log_tail_has_no_bare_token(tmp_path):
    from aria.routers.logs import _read_log_tail_redacted

    path = tmp_path / "backend.log"
    path.write_text(f"INFO call /{BOT}/getUpdates and {SK}\n", encoding="utf-8")
    text = _read_log_tail_redacted(path)
    assert "A" * 35 not in text and "a" * 24 not in text


def test_tool_handler_never_receives_secret_arguments():
    import asyncio
    from types import SimpleNamespace

    from aria.core import loop

    seen: dict = {}

    async def handler(input_json, timeout_sec, sandbox_root, router=None, task_id=None, **kwargs):
        seen.update(input_json)
        return {"stdout": "ok"}

    spec = SimpleNamespace(
        tool_name="shell_execute",
        input_schema={},
        timeout_sec=5,
        handler=handler,
    )
    asyncio.run(loop._execute_tool(spec, {"command": f"echo {SK}"}, "."))
    assert SK not in str(seen)
    assert secretguard.MASK in str(seen)


def test_scrub_script_backs_up_and_masks_saved_secrets(tmp_path):
    import importlib.util
    from pathlib import Path

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as OrmSession

    from aria.db import models as models

    script = Path(__file__).resolve().parents[1] / "scripts" / "scrub_secrets_db.py"
    spec = importlib.util.spec_from_file_location("scrub_secrets_db", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    db_path = tmp_path / "local_agent.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    models.Base.metadata.create_all(engine)
    with OrmSession(engine) as db:
        session = models.Session(title=f"title {BOT}")
        db.add(session)
        db.flush()
        db.add(models.Message(session_id=session.id, seq_no=1, role="user", content=f"key {SK}"))
        task = models.Task(session_id=session.id, objective="t", role="general")
        db.add(task)
        db.flush()
        db.add(
            models.ToolCall(
                session_id=session.id, task_id=task.id, tool_name="shell_execute",
                role="general", input_json={"command": f"echo {SK}"},
                output_json={"stdout": f"out {GOOGLE}"},
            )
        )
        db.commit()
    engine.dispose()

    assert module.main(["--db", str(db_path)]) == 0
    backups = list(tmp_path.glob("local_agent.db.bak-*"))
    assert len(backups) == 1

    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        dump = "\n".join(
            str(row)
            for table in ("messages", "tool_calls", "sessions")
            for row in conn.execute(f"SELECT * FROM {table}").fetchall()
        )
    finally:
        conn.close()
    assert SK not in dump and GOOGLE not in dump and BOT not in dump
    assert secretguard.MASK in dump


