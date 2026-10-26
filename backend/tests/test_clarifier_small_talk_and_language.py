"""UI-3 (language follows the user) and UI-4 (no questions for small talk)."""
from __future__ import annotations

import pytest

from aria.core import clarifier


@pytest.mark.parametrize("text", [
    "hi.", "Hello!", "hey", "привет", "Приветттт!", "как дела?", "спасибо", "thanks!", "ok", "привіт",
    "hi there", "hello Claude", "good morning",
])
def test_small_talk_skips_the_gate(text):
    assert clarifier.is_small_talk(text) is True
    assert clarifier.gate_required(text, "agent") is False


@pytest.mark.parametrize("text", [
    "привет, сделай отчёт", "hi, build me an app", "hey deploy the site", "hello world program",
    "fix the failing test", "сделай отчёт по продажам", "what is the capital of France",
])
def test_anything_with_a_request_still_goes_through_the_gate(text):
    assert clarifier.is_small_talk(text) is False
    assert clarifier.gate_required(text, "agent") is True


def test_answer_to_open_questions_is_never_small_talk():
    """«да» / «ок» в ответ на вопросы уточнения должны попасть в TaskSpec, а не пропустить гейт."""
    for reply in ("да", "ок", "нет", "yes", "ok"):
        assert clarifier.gate_required(reply, "agent", awaiting=True) is True


@pytest.mark.parametrize("text,lang", [
    ("write a short poem about spring", "en"), ("сделай отчёт по логам", "ru"),
    ("Зроби звіт про продажі", "uk"), ("12345 ???", "ru"),
])
def test_language_detection(text, lang):
    assert clarifier.detect_language(text) == lang


def test_english_request_gets_english_fallback_questions_and_message():
    qs = clarifier._fallback_questions("en")
    assert len(qs) == 3 and all(len(q["options"]) == 5 and q["default_value"] in q["options"] for q in qs)
    assert all(not any("а" <= ch.lower() <= "я" for ch in q["text"]) for q in qs)
    msg = clarifier._question_message(qs, "en")
    assert "(recommended)" in msg and "Accept assumptions" in msg and "рекомендую" not in msg


def test_russian_defaults_unchanged():
    assert clarifier._fallback_questions()[0]["text"] == "В каком виде нужен результат?"
    assert "(рекомендую)" in clarifier._question_message(clarifier._fallback_questions())


@pytest.mark.asyncio
async def test_model_is_told_to_answer_in_the_language_of_the_request(monkeypatch):
    seen = {}

    class R:
        async def route_chat(self, cls, messages, **kw):
            seen["user"] = messages[1].content

            class Res:
                class response:
                    text = "{}"
            return Res()

    from aria.core.taskspec import TaskSpec
    await clarifier._ask_model(R(), "write a short poem about spring", TaskSpec())
    assert "английский" in seen["user"] and "на этом же языке" in seen["user"]
    await clarifier._ask_model(R(), "сделай отчёт", TaskSpec())
    assert "русский" in seen["user"]
