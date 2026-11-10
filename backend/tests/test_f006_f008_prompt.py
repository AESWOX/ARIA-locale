"""F-006/F-008: промпт знает 501-заглушки и запрещает числа без запуска."""
from __future__ import annotations

from pathlib import Path

from aria.routers.chat import _system_prompt

PROMPTS = Path(__file__).resolve().parents[1] / "aria" / "prompts"


def test_chat_prompt_lists_stubs_and_the_number_rule():
    text = _system_prompt()

    for marker in (
        "501",
        "OAuth",
        "gateway",
        "pairing",
        "plugin hub",
        "credential pool",
        "curator",
        "config migration",
        "not implemented in this build",
    ):
        assert marker in text, marker
    assert "have not actually obtained" in text
    assert "have not checked" in text


def test_general_prompt_is_honest_about_stubs_and_numbers():
    text = (PROMPTS / "general.md").read_text(encoding="utf-8")

    assert "501" in text
    assert "не реализовано в этой сборке" in text
    assert "без запуска" in text


def test_worker_prompt_is_honest_about_stubs_and_numbers():
    text = (PROMPTS / "worker.md").read_text(encoding="utf-8")

    assert "HTTP 501" in text
    assert "not implemented in this build" in text
    assert 'say "not checked"' in text
