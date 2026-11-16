"""F-004: аудит двухуровневый — light зовёт Flash-Lite, heavy — ruff + DeepSeek;
бейдж tier, честная деградация без toolchain, пометка same provider, реворк до 3."""
from __future__ import annotations

import asyncio
import uuid

from aria.config import get_settings
from aria.core.audit import run_audit
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import AuditVerdict, ToolStatus
from aria.llm.providers.base import LlmProvider, LlmResponse
from aria.llm.router import ProviderRouter

PASS_JSON = '{"verdict": "pass", "issues": [], "criteria": []}'


class FakeAuditor(LlmProvider):
    def __init__(self, provider_id: str, provider_class: str, model: str):
        self.provider_id = provider_id
        self.provider_class = provider_class
        self.model = model
        self.calls = 0

    async def check_connectivity(self, timeout_sec):
        return True

    async def chat(self, messages, tools, timeout_sec):
        self.calls += 1
        return LlmResponse(text=PASS_JSON)


def _router(*providers: LlmProvider) -> ProviderRouter:
    router = ProviderRouter()
    for provider in providers:
        router.register(provider)
    return router


def _task_with_call(tool_name: str = "file_read", payload: dict | None = None):
    with session_scope() as db:
        session = repo.create_session(db, f"f004-{uuid.uuid4().hex[:6]}")
        task = repo.create_task(db, session, role="general", objective="проверь")
        call = repo.start_tool_call(db, session, task, tool_name, "general", "low", payload or {})
        repo.finish_tool_call(db, call, ToolStatus.ok, output_json={"ok": True})
        return session.id, task.id


def test_light_audit_prefers_flash_lite():
    lite = FakeAuditor("lite-a", "free_tier_reasoning", "gemini-3.5-flash-lite")
    deep = FakeAuditor("deepseek-chat", "standard_reasoning", "deepseek-flash")
    router = _router(lite, deep)
    sid, tid = _task_with_call()

    with session_scope() as db:
        session = repo.get_session(db, sid)
        task = repo.get_task(db, tid)
        report = asyncio.run(run_audit(db, session, task, router, level="light"))

    assert report.verdict == AuditVerdict.pass_
    assert report.auditor_model == "lite-a"
    assert report.plan_vs_fact["audit_tier"] == "light"
    assert lite.calls == 1 and deep.calls == 0


def test_heavy_audit_runs_ruff_on_changed_code(tmp_path, monkeypatch):
    from aria import toolchain

    code_file = tmp_path / "bad.py"
    code_file.write_text("import os\n", encoding="utf-8")
    monkeypatch.setattr(
        toolchain,
        "probe",
        lambda: {"versions": {"python": "3.11.9", "pytest": "8.3.5", "ruff": "0.11.13", "pip-audit": "2.9.0"}, "installed": True, "missing": []},
    )
    monkeypatch.setattr(toolchain, "scripts_dir", lambda: tmp_path)
    monkeypatch.setattr(toolchain, "run", lambda args, timeout=120: (1, "bad.py:1:1: F401 'os' imported but unused"))

    deep = FakeAuditor("deepseek-chat", "standard_reasoning", "deepseek-flash")
    router = _router(deep)
    sid, tid = _task_with_call("file_write", {"path": str(code_file)})

    with session_scope() as db:
        session = repo.get_session(db, sid)
        task = repo.get_task(db, tid)
        report = asyncio.run(run_audit(db, session, task, router, level="strict"))

    assert report.verdict == AuditVerdict.needs_rework
    assert report.auditor_model == "ruff"
    assert report.plan_vs_fact["audit_tier"] == "heavy"
    assert any("F401" in item for item in report.patch_suggestions)
    assert deep.calls == 0


def test_heavy_degrades_honestly_without_ruff(tmp_path, monkeypatch):
    from aria import toolchain

    code_file = tmp_path / "ok.py"
    code_file.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        toolchain,
        "probe",
        lambda: {"versions": {"python": None, "pytest": None, "ruff": None, "pip-audit": None}, "installed": False, "missing": ["python"]},
    )

    deep = FakeAuditor("deepseek-chat", "standard_reasoning", "deepseek-flash")
    router = _router(deep)
    sid, tid = _task_with_call("file_write", {"path": str(code_file)})

    with session_scope() as db:
        session = repo.get_session(db, sid)
        task = repo.get_task(db, tid)
        report = asyncio.run(run_audit(db, session, task, router, level="strict"))

    assert report.verdict == AuditVerdict.pass_
    assert report.plan_vs_fact["audit_tier"] == "light"
    notes = " ".join(report.plan_vs_fact["heavy_checks"]["notes"])
    assert "ruff is not installed" in notes and "pip-audit is not installed" in notes


def test_same_provider_is_marked():
    deep = FakeAuditor("deepseek-chat", "standard_reasoning", "deepseek-flash")
    router = _router(deep)
    sid, tid = _task_with_call()
    with session_scope() as db:
        session = repo.get_session(db, sid)
        task = repo.get_task(db, tid)
        report = asyncio.run(run_audit(db, session, task, router, level="light"))

    assert report.plan_vs_fact["audited_same_provider"] is True


def test_rework_limit_is_three():
    assert get_settings().audit_max_attempts == 3
