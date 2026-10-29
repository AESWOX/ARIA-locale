"""Исполняемое ТЗ беты («one simple working app»): каждый тест = пункт цели беты.

 * Без маркера: свойство выполнено и закреплено.
 * `xfail(strict=True)`: ещё не реализовано. Когда фича появится, тест станет XPASS и упадёт:
   маркер снимают, пункт переходит в закреплённые. ТЗ не может молча устареть.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from aria.llm.providers.base import LlmProvider
from aria.main import app
from aria.tools.registry import TOOL_REGISTRY

REPO = Path(__file__).resolve().parents[2]
APP_TSX = REPO / "desktop" / "src" / "App.tsx"
BETA = "BETA: "


def _routes() -> set[tuple[str, str]]:
    return {(m.upper(), p) for p, ops in app.openapi()["paths"].items()
            for m in ops if m in ("get", "post", "put", "patch", "delete")}


def _nav_items() -> int:
    src = APP_TSX.read_text(encoding="utf-8")
    start = src.index("const BUILTIN_NAV_REST")
    end = src.index("\n];", start)
    return 1 + len(re.findall(r"\bpath:\s*\"/", src[start:end]))  # +1 = CHAT_NAV_ITEM


def test_only_shell_needs_approval_and_read_tools_run_freely():
    needs = {n for n, s in TOOL_REGISTRY.items() if s.requires_approval}
    assert needs == {"shell_execute"}, needs
    for name in ("file_read", "file_grep", "file_search"):
        assert not TOOL_REGISTRY[name].requires_approval


def test_cancel_task_endpoint_exists():
    assert ("POST", "/tasks/{task_id}/cancel") in _routes()


def test_navigation_has_exactly_five_items():
    assert _nav_items() == 5, f"сейчас {_nav_items()} пунктов"


def test_providers_can_stream():
    assert hasattr(LlmProvider, "stream")


@pytest.mark.xfail(strict=True, reason=BETA + "агент сам строит план инструментом make_plan")
def test_make_plan_tool_is_registered():
    assert "make_plan" in TOOL_REGISTRY


def test_chat_has_a_stop_endpoint():
    assert ("POST", "/chat/sessions/{session_id}/stop") in _routes()


def test_logs_can_be_opened_from_the_app():
    assert any(re.search(r"logs.*(folder|open)", p) for _m, p in _routes())


def test_diagnostics_can_be_exported():
    assert any("diagnostic" in p for _m, p in _routes())


def test_session_exposes_context_and_cost():
    from aria.routers import chat as chat_router

    src = Path(chat_router.__file__).read_text(encoding="utf-8")
    assert "context_tokens" in src and "session_cost" in src
