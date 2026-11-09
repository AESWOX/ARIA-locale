"""UX-2: один автоматический режим — переключателя Chat/Plan в UI нет."""
from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "desktop" / "src"
PAGE = SRC / "pages" / "NativeChatPage.tsx"
COMPOSER = SRC / "components" / "ChatComposer.tsx"
TOPBAR = SRC / "components" / "ChatTopBar.tsx"
WORK = SRC / "components" / "WorkPanel.tsx"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_chat_mode_module_is_gone():
    assert not (SRC / "lib" / "chat-modes.ts").exists()


def test_composer_has_no_mode_switcher():
    composer = _read(COMPOSER)
    for marker in ("CHAT_MODES", "radiogroup", "onModeChange", "placeholderPlan"):
        assert marker not in composer, marker


def test_page_has_no_mode_state_or_plan_tab():
    page = _read(PAGE)
    for marker in ("CHAT_MODES", "normalizeChatMode", "type ChatMode", "useState<ChatMode>", "setMode("):
        assert marker not in page, marker
    assert 'from "@/lib/chat-modes"' not in page


def test_topbar_has_no_mode_chip():
    bar = _read(TOPBAR)
    assert 'title="Mode"' not in bar
    assert "mode:" not in bar


def test_work_panel_has_no_plan_tab():
    work = _read(WORK)
    assert 'id: "plan"' not in work
    assert "<TaskSpecPanel" not in work
    assert "switchToPlan" not in work


def test_no_agent_mode_tabs_anywhere_in_the_ui():
    offenders = [
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.tsx")
        if 'id: "agent"' in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
