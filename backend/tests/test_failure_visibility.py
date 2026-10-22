"""Сбои задач и логи видны в самом приложении: строка в логе, сообщение в чате, кнопка Clear."""
from __future__ import annotations

import logging
import logging.handlers

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.core.executor import _audit_reason, _transition, build_failure_message
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import AuditVerdict, TaskStatus
from aria.routers import logs as logs_mod


def _logs_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(logs_mod, "_LOG_DIR", tmp_path)
    app = FastAPI()
    app.include_router(logs_mod.router)
    app.dependency_overrides[require_runtime_token] = lambda: "test"
    return TestClient(app)


def test_clear_logs_empties_file_and_removes_rotated_copies(tmp_path, monkeypatch):
    (tmp_path / "backend.log").write_text("old line 1\nold line 2\n", encoding="utf-8")
    (tmp_path / "backend.log.1").write_text("older\n", encoding="utf-8")
    client = _logs_client(tmp_path, monkeypatch)

    r = client.post("/logs/clear").json()
    assert r["ok"] is True and r["cleared"] is True and r["removed_backups"] == 1
    assert not (tmp_path / "backend.log.1").exists()
    lines = client.get("/logs", params={"file": "agent"}).json()["lines"]
    assert not any("old line" in ln for ln in lines)


def test_clear_logs_with_live_handler_keeps_logging_from_the_start(tmp_path, monkeypatch):
    path = tmp_path / "backend.log"
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    log = logging.getLogger("aria.test.clear")
    log.setLevel(logging.INFO)
    log.addHandler(handler)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        log.info("before clear")
        client = _logs_client(tmp_path, monkeypatch)
        client.post("/logs/clear")
        log.info("after clear")
        handler.flush()
        text = path.read_text(encoding="utf-8")
        assert "before clear" not in text
        assert "after clear" in text
        assert "\x00" not in text, "после очистки файл не должен начинаться с нулевых байтов"
    finally:
        root.removeHandler(handler)
        log.removeHandler(handler)
        handler.close()


def test_failed_transition_writes_a_log_line(caplog):
    with session_scope() as db:
        s = repo.create_session(db, "fail-log")
        t = repo.create_task(db, s, role="general", objective="x", draft_tz_md="# tz\n")
        repo.set_task_status(db, t, TaskStatus.approved)
        repo.set_task_status(db, t, TaskStatus.in_progress)
        with caplog.at_level(logging.WARNING, logger="local_agent.tasks"):
            repo.set_task_status(db, t, TaskStatus.failed, error_code="bounded_retry_exhausted", error_message="аудитор отказал")
    assert any("bounded_retry_exhausted" in rec.getMessage() and "аудитор отказал" in rec.getMessage() for rec in caplog.records)


def test_executor_failure_posts_chat_message_with_auditor_remarks():
    with session_scope() as db:
        s = repo.create_session(db, "fail-chat")
        t = repo.create_task(db, s, role="general", objective="какой план?", draft_tz_md="# tz\n")
        repo.set_task_status(db, t, TaskStatus.approved)
        repo.set_task_status(db, t, TaskStatus.in_progress)
        repo.create_audit_report(
            db, s, t, attempt_no=1, auditor_role="qa_auditor", auditor_model="m", budget_degraded=False,
            verdict=AuditVerdict.needs_rework, plan_vs_fact={}, tool_success_summary={},
            missing_requirements=["нет итогового ответа"], patch_suggestions=["QA: файл перезаписан несколько раз"],
            metrics_compared={},
        )
        assert _transition(db, t, TaskStatus.failed, error_code="bounded_retry_exhausted", error_message="Correctness QA: needs_rework") is True
        db.flush()
        texts = [m.content for m in repo.list_messages(db, s.id) if m.role == "assistant"]
    assert len(texts) == 1
    assert "Задача не выполнена" in texts[0]
    assert "bounded_retry_exhausted" in texts[0]
    assert "файл перезаписан несколько раз" in texts[0] and "нет итогового ответа" in texts[0]


def test_failure_message_without_remarks_still_explains():
    class T:
        error_code = "approval_required"
        error_message = "Шаги плана требуют подтверждения"

    text = build_failure_message(T(), [])
    assert "нужно подтверждение владельца" in text and "Замечания аудитора" not in text


def test_audit_reason_carries_remarks():
    class R:
        patch_suggestions = ["QA: противоречивый документ"]
        missing_requirements = ["нет файла"]

    reason = _audit_reason(R())
    assert reason.startswith("Correctness QA (run_audit) verdict=needs_rework: ")
    assert "противоречивый документ" in reason and "нет файла" in reason
    assert _audit_reason(type("E", (), {"patch_suggestions": [], "missing_requirements": []})()) == "Correctness QA (run_audit) verdict=needs_rework"


def _access(method: str, path: str, status: int) -> logging.LogRecord:
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                             ("127.0.0.1:1", method, path, "1.1", status), None)


def test_polling_access_lines_are_hidden_but_errors_stay(monkeypatch):
    from aria.main import _QuietPollingFilter

    f = _QuietPollingFilter()
    assert f.filter(_access("GET", "/chat/sessions", 200)) is False
    assert f.filter(_access("GET", "/status", 200)) is False
    assert f.filter(_access("GET", "/chat/sessions", 500)) is True
    assert f.filter(_access("POST", "/chat/sessions/x/send", 200)) is True
    assert f.filter(logging.LogRecord("aria.core", logging.INFO, __file__, 1, "msg", (), None)) is True
    monkeypatch.setenv("ARIA_LOG_ACCESS", "1")
    assert f.filter(_access("GET", "/status", 200)) is True


# ── полные логи: инструменты, статусы, аудит, вкладки ──────────────────────────────

def test_tool_calls_status_changes_and_audit_are_logged(caplog):
    from aria.db.enums import ToolStatus

    with session_scope() as db:
        s = repo.create_session(db, "full-logs")
        t = repo.create_task(db, s, role="general", objective="x", draft_tz_md="# tz\n")
        with caplog.at_level(logging.INFO):
            repo.set_task_status(db, t, TaskStatus.approved)
            call = repo.start_tool_call(db, s, t, "file_write", "general", "low", {"path": "a.md", "content": "SECRET-BODY"})
            repo.finish_tool_call(db, call, ToolStatus.ok, output_json={"bytes_written": 3})
            bad = repo.start_tool_call(db, s, t, "shell_execute", "general", "high", {"command": "echo TOKEN123"})
            repo.finish_tool_call(db, bad, ToolStatus.error, error_code="blocked", error_message="нет прав")
            repo.create_audit_report(
                db, s, t, attempt_no=1, auditor_role="qa_auditor", auditor_model="deepseek-chat", budget_degraded=False,
                verdict=AuditVerdict.needs_rework, plan_vs_fact={}, tool_success_summary={},
                missing_requirements=[], patch_suggestions=["QA: нет итога"], metrics_compared={},
            )
    text = "\n".join(f"{r.name}|{r.levelname}|{r.getMessage()}" for r in caplog.records)
    assert "local_agent.tasks|INFO|task" in text and "-> approved" in text
    assert "local_agent.tools|INFO|tool file_write ok" in text and "path=a.md" in text
    assert "local_agent.tools|WARNING|tool shell_execute error" in text and "error=blocked" in text
    assert "SECRET-BODY" not in text and "TOKEN123" not in text, "содержимое файлов и команды в лог не попадают"
    assert "local_agent.audit|INFO|audit task=" in text and "needs_rework" in text and "нет итога" in text


def _seed_log(tmp_path):
    lines = [
        "2026-10-22 13:00:01,000 INFO uvicorn.access: 127.0.0.1:1 - \"POST /chat/sessions/x/send HTTP/1.1\" 200",
        "2026-10-22 13:00:02,000 INFO local_agent.tools: tool file_write ok task=3a5f4292 12ms path=a.md",
        "2026-10-22 13:00:03,000 WARNING local_agent.tasks: task 3a5f4292 failed: code=bounded_retry_exhausted message=x",
        "2026-10-22 13:00:04,000 INFO aria.core.executor: Stage 6: Retry check for task 3a5f4292",
        "2026-10-22 13:00:05,000 INFO local_agent.main: cron tick: due=[] submitted=[]",
        "2026-10-22 13:00:06,000 INFO local_agent.audit: audit task=3a5f4292 attempt=1 verdict=needs_rework error handling remarks=-",
    ]
    (tmp_path / "backend.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_errors_tab_shows_only_warnings_and_errors(tmp_path, monkeypatch):
    _seed_log(tmp_path)
    got = _logs_client(tmp_path, monkeypatch).get("/logs", params={"file": "errors"}).json()["lines"]
    assert len(got) == 1 and "bounded_retry_exhausted" in got[0]


def test_gateway_tab_and_components_filter_by_logger_name(tmp_path, monkeypatch):
    _seed_log(tmp_path)
    c = _logs_client(tmp_path, monkeypatch)
    gw = c.get("/logs", params={"file": "gateway"}).json()["lines"]
    assert len(gw) == 1 and "uvicorn.access" in gw[0]
    tools = c.get("/logs", params={"component": "tools"}).json()["lines"]
    assert len(tools) == 1 and "tool file_write" in tools[0]
    agent = c.get("/logs", params={"component": "agent"}).json()["lines"]
    assert any("Stage 6" in ln for ln in agent) and any("failed" in ln for ln in agent) and not any("file_write" in ln for ln in agent)
    cron = c.get("/logs", params={"component": "cron"}).json()["lines"]
    assert len(cron) == 1 and "cron tick" in cron[0]


def test_level_comes_from_levelname_not_from_message_text(tmp_path, monkeypatch):
    _seed_log(tmp_path)
    got = _logs_client(tmp_path, monkeypatch).get("/logs", params={"level": "ERROR"}).json()["lines"]
    assert got == [], "слово error в тексте INFO-строки не делает её ошибкой"
