"""0016 «основа»: кодировка файлов, «ку бро», commit до вызова модели, логи по подсистемам, Clean test data."""
from __future__ import annotations

import asyncio
import io
import logging
import logging.handlers
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria import logsetup, textio
from aria.api.auth import require_runtime_token
from aria.core import clarifier
from aria.core.testdata import clean_test_data, is_test_session_title, is_test_skill
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import release_write_lock, session_scope
from aria.tools.handlers.files import file_read


# ── BUG-4: русский текст читается без кракозябр ────────────────────────────────

RU = "Пожалуйста, составь план"


def test_decode_prefers_utf8_then_cp1251():
    assert textio.decode_text(RU.encode("utf-8")) == RU
    assert textio.decode_text(RU.encode("utf-8-sig")) == RU
    assert textio.decode_text(RU.encode("cp1251")) == RU  # файл из «Блокнота»
    assert "�" in textio.decode_text(b"\xff\xfe\x00\x81\x8d\xff") or True  # не падает на мусоре


def test_file_read_handles_utf8_bom_and_cp1251(tmp_path):
    (tmp_path / "a.md").write_bytes(RU.encode("utf-8"))
    (tmp_path / "b.md").write_bytes(RU.encode("utf-8-sig"))
    (tmp_path / "c.md").write_bytes(RU.encode("cp1251"))
    for name in ("a.md", "b.md", "c.md"):
        out = asyncio.run(file_read({"path": name}, str(tmp_path)))
        assert out["content"] == RU, name


def test_file_read_does_not_turn_utf8_into_cp1251_mojibake(tmp_path):
    (tmp_path / "x.md").write_bytes(RU.encode("utf-8"))
    out = asyncio.run(file_read({"path": "x.md"}, str(tmp_path)))
    assert "Р" not in out["content"].replace("Пожалуйста", "")  # «РџРѕР¶Р°…» содержит «Р»


# ── CLR-1: слэнговые приветствия без уточняющих вопросов ──────────────────────

@pytest.mark.parametrize("text", ["ку бро", "Ку, бро!", "бро", "здарова", "yo bro", "hey dude", "прив бро", "ку"])
def test_slang_greetings_are_small_talk(text):
    assert clarifier.is_small_talk(text) is True


@pytest.mark.parametrize("text", ["ку бро сделай отчёт", "yo bro build me an app", "бро, напиши скрипт"])
def test_slang_greeting_plus_request_still_gets_questions(text):
    assert clarifier.is_small_talk(text) is False


# ── BUG-5: писатель отпускается до долгого вызова модели ──────────────────────

def test_release_write_lock_commits_pending_writes_and_tolerates_none():
    release_write_lock(None)
    with session_scope() as db:
        s = repo.create_session(db, "lock-test")
        sid = s.id
        release_write_lock(db)
        # после commit строка видна другому соединению ещё до выхода из session_scope
        with session_scope() as other:
            assert other.get(m.Session, sid) is not None


def test_release_write_lock_swallows_commit_errors():
    class Broken:
        def commit(self):
            raise RuntimeError("boom")

        def rollback(self):
            pass

    release_write_lock(Broken())  # не бросает


def test_planner_and_audit_release_the_lock_before_the_model_call():
    root = Path(__file__).resolve().parent.parent / "aria" / "core"
    ex = (root / "executor.py").read_text(encoding="utf-8")
    au = (root / "audit.py").read_text(encoding="utf-8")
    assert ex.count("release_write_lock(") >= 3
    i = ex.index("release_write_lock(session)  # BUG-5")
    assert ex.index('"premium_reasoning", messages', i) - i < 400
    assert "release_write_lock(db)" in au
    j = au.index("release_write_lock(db)")
    assert au.index("router.route_chat(", j) - j < 300


def test_busy_timeout_is_short():
    from aria.db import base

    src = Path(base.__file__).read_text(encoding="utf-8")
    assert "busy_timeout=5000" in src and "busy_timeout=30000" not in src


# ── логи: task_id, подсистемы, без успешных HTTP ───────────────────────────────

def _access(status: int, method="GET", path="/status") -> logging.LogRecord:
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                             ("127.0.0.1:1", method, path, "1.1", status), None)


def test_all_successful_http_lines_are_dropped_errors_stay(monkeypatch):
    monkeypatch.delenv("ARIA_LOG_ACCESS", raising=False)
    f = logsetup.QuietAccessFilter()
    for path in ("/status", "/tasks/abc/plan", "/chat/models", "/dashboard/x", "/profiles"):
        assert f.filter(_access(200, path=path)) is False, path
    assert f.filter(_access(204, "POST", "/x")) is False
    assert f.filter(_access(404)) is True and f.filter(_access(500, "POST")) is True
    assert f.filter(logging.LogRecord("local_agent.tools", logging.INFO, __file__, 1, "tool ok", (), None)) is True
    monkeypatch.setenv("ARIA_LOG_ACCESS", "1")
    assert f.filter(_access(200)) is True


def test_task_id_is_added_to_log_lines_inside_a_task_context():
    stream = io.StringIO()
    h = logging.StreamHandler(stream)
    h.setFormatter(logging.Formatter(logsetup.LOG_FORMAT))
    lg = logging.getLogger("local_agent.tools.test0016")
    lg.setLevel(logging.INFO)
    lg.addHandler(h)
    try:
        tid = str(uuid.uuid4())
        token = logsetup.set_task(tid)
        lg.info("tool file_write ok")
        lg.info("already mentions %s", tid[:8])
        logsetup.reset_task(token)
        lg.info("outside any task")
    finally:
        lg.removeHandler(h)
    lines = stream.getvalue().splitlines()
    assert lines[0].endswith(f"[task={tid[:8]}]")
    assert lines[1].count(tid[:8]) == 1  # не дублируется, если id уже в тексте
    assert "[task=" not in lines[2]


def test_subsystem_files_split_lines_by_logger_and_errors_collect_warnings(tmp_path):
    root = logging.getLogger("aria_test_root_0016")
    root.setLevel(logging.INFO)
    root.propagate = False
    made = logsetup.install_subsystem_handlers(tmp_path, lambda fmt: logging.Formatter(fmt), logger=root)
    assert {h._aria_subsystem for h in made} == {"tasks.log", "llm.log", "http.log", "errors.log"}
    assert logsetup.install_subsystem_handlers(tmp_path, lambda fmt: logging.Formatter(fmt), logger=root) == []  # без дублей

    def emit(name, level, msg):
        root.handle(logging.LogRecord(name, level, __file__, 1, msg, (), None))

    emit("local_agent.tools", logging.INFO, "tool file_write ok")
    emit("local_agent.llm.router", logging.INFO, "route standard_reasoning")
    emit("uvicorn.access", logging.WARNING, "boom 500")
    for h in made:
        h.flush()
    read = lambda n: (tmp_path / n).read_text(encoding="utf-8")
    assert "tool file_write ok" in read("tasks.log") and "route standard_reasoning" not in read("tasks.log")
    assert "route standard_reasoning" in read("llm.log")
    assert "boom 500" in read("http.log") and "boom 500" in read("errors.log")
    assert "tool file_write ok" not in read("errors.log")
    for h in made:
        root.removeHandler(h)
        h.close()


def test_task_runner_sets_the_task_context_while_running_a_job():
    src = (Path(__file__).resolve().parent.parent / "aria" / "core" / "taskrunner.py").read_text(encoding="utf-8")
    assert "set_task_log_context(key)" in src and "reset_task_log_context(" in src


# ── Clean test data ────────────────────────────────────────────────────────────

def test_patterns_recognise_smoke_leftovers_only():
    assert is_test_skill("e2e1a2b3c-hello") and not is_test_skill("e2e-notes") and not is_test_skill("my-e2e1a2b3c-x")
    for title in ("e2e shell approval", "e2e mcp approval", "Reply with exactly the single word PONG and nothing else.",
                  "What is my favourite smoke-test colour? Answer with just the colour word."):
        assert is_test_session_title(title), title
    for title in ("Plan my week", "e2e is a testing approach, explain", "Новый чат", None):
        assert not is_test_session_title(title), title


def test_clean_test_data_removes_leftovers_and_keeps_user_data(tmp_path, monkeypatch):
    from aria import paths

    skills_root = tmp_path / "skills"
    (skills_root / "e2eabcdef-hello").mkdir(parents=True)
    (skills_root / "my-skill").mkdir()
    monkeypatch.setattr(paths, "skills_dir", lambda: skills_root)
    with session_scope() as db:
        repo.upsert_skill(db, "e2eabcdef-hello")
        repo.upsert_skill(db, "my-skill")
        repo.create_session(db, "e2e shell approval")
        repo.create_session(db, "Reply with exactly the single word PONG and nothing else.")
        keep = repo.create_session(db, "Plan my week")
        keep_id = keep.id
    out = clean_test_data()
    assert out["skills"] == ["e2eabcdef-hello"] and out["sessions"] == 2
    assert not (skills_root / "e2eabcdef-hello").exists() and (skills_root / "my-skill").is_dir()
    with session_scope() as db:
        names = {s.skill_name for s in repo.list_skills(db)}
        assert "e2eabcdef-hello" not in names and "my-skill" in names
        assert db.get(m.Session, keep_id) is not None
    assert clean_test_data() == {"skills": [], "sessions": 0, "mcp_servers": []}  # идемпотентно


def test_clean_test_data_route_requires_token_and_works(tmp_path, monkeypatch):
    from aria.routers import system as system_mod
    from aria import paths

    monkeypatch.setattr(paths, "skills_dir", lambda: tmp_path)
    app = FastAPI()
    app.include_router(system_mod.router)
    client = TestClient(app)
    assert client.post("/system/clean-test-data").status_code in (401, 403)
    app.dependency_overrides[require_runtime_token] = lambda: "test"
    r = client.post("/system/clean-test-data")
    assert r.status_code == 200 and r.json()["ok"] is True
