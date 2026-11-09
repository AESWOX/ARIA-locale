"""Этапы 5–7: минимальный Advanced, финальный UI и recall в frozen-rehearsal."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "desktop" / "src"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_advanced_has_only_three_controls():
    composer = _read(SRC / "components" / "ChatComposer.tsx")
    assert "Daily limit, USD" in composer
    assert "Show process details" in composer
    assert "chatComposer.reset" in composer
    for gone in ("useForNew", "TeamPanel", "Boss + helpers", "Use for new chats"):
        assert gone not in composer, gone
    assert not (SRC / "components" / "TeamPanel.tsx").exists()


def test_composer_rows_and_enter_shift_enter():
    composer = _read(SRC / "components" / "ChatComposer.tsx")
    assert "rows={1}" in composer
    assert "168" in composer
    assert 'event.key === "Enter" && !event.shiftKey' in composer


def test_session_tabs_have_new_close_rename_switch_and_overflow():
    tabs = _read(SRC / "components" / "SessionTabs.tsx")
    for marker in ("onNew", "onClose", "onOpen", "onRename", "commitRename", "All sessions", "details"):
        assert marker in tabs, marker


def test_processes_and_right_panel_exist():
    page = _read(SRC / "pages" / "NativeChatPage.tsx")
    work = _read(SRC / "components" / "WorkPanel.tsx")
    group = _read(SRC / "components" / "ToolCallGroup.tsx")
    assert "<WorkPanel" in page and "min-h-0" in page
    assert "showProcessDetails" in group and "Undo" in group
    assert "SessionInspector" in work


def test_themes_black_white_gray_and_code_copy():
    presets = _read(SRC / "themes" / "presets.ts")
    for label in ('"Black"', '"White"', '"Gray"'):
        assert label in presets, label
    assert "Copy code" in _read(SRC / "components" / "Markdown.tsx")


def test_sidebar_status_links_are_separate():
    strip = _read(SRC / "components" / "SidebarStatusStrip.tsx")
    assert 'to="/channels"' in strip and 'to="/sessions"' in strip


def test_no_none_options_in_chat_selectors():
    composer = _read(SRC / "components" / "ChatComposer.tsx")
    assert ">None<" not in composer and 'value="None"' not in composer


def test_frozen_rehearsal_requires_memory_recall():
    script = _read(ROOT / "backend" / "scripts" / "e2e_rehearsal.py")
    assert 'row.get("stage") == "memory"' in script
    assert 'memory[0].get("status") == "PASS"' in script
    smoke = _read(ROOT / "backend" / "scripts" / "e2e_smoke.py")
    assert "memory recall failed" in smoke


def test_qa_auditor_prompt_is_real():
    prompt = _read(ROOT / "backend" / "aria" / "prompts" / "qa_auditor.md")
    assert "qa_auditor" in prompt
    assert "Нет доказательства — fail" in prompt
    assert "UNVERIFIABLE" in prompt
