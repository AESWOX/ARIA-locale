"""Решение 10.11: compression trigger 70% / hard 85%, сжатый ≤25%, Flash-Lite,
redflag-валидация — при провале raw не заменяется, эскалация на DeepSeek."""
from __future__ import annotations

from aria.config import get_settings
from aria.llm import compression

GOOD = (
    "Instructions: keep\nTask: t\nConstraints: c\nDecisions: d\n"
    "Verified facts: f\nEntities: e\nOpen questions: none\nState: s\n"
    '{"ok": true, "missing": []}'
)


def test_thresholds_are_70_and_85_percent():
    settings = get_settings()
    window = settings.compression_context_window

    assert window == 1_048_576
    assert settings.compression_trigger_ratio == 0.70
    assert settings.compression_hard_ratio == 0.85
    assert compression.compression_threshold() == int(window * 0.70)
    assert int(window * 0.70) == 734003
    assert int(window * 0.85) == 891289


def test_default_compression_model_is_flash_lite():
    assert get_settings().compression_model == "gemini-3.5-flash-lite"


def test_valid_summary_passes():
    ok, problems = compression.validate_summary(GOOD, original_chars=10_000)

    assert ok and problems == []


def test_missing_sections_are_a_redflag():
    ok, problems = compression.validate_summary("краткое резюме без секций", original_chars=10_000)

    assert ok is False
    assert any("sections" in item for item in problems)


def test_oversized_summary_is_a_redflag():
    large = GOOD + "\n" + ("x" * 5000)

    ok, problems = compression.validate_summary(large, original_chars=10_000)

    assert ok is False
    assert any("25%" in item for item in problems)


def test_fidelity_false_is_a_redflag():
    bad = GOOD.replace('{"ok": true, "missing": []}', '{"ok": false, "missing": ["Decisions"]}')

    ok, problems = compression.validate_summary(bad, original_chars=10_000)

    assert ok is False
    assert any("fidelity" in item for item in problems)


def test_redflag_does_not_replace_raw_history(monkeypatch):
    from tests.test_ctx_compression import _cleanup, _make_session

    settings = get_settings()
    monkeypatch.setattr(settings, "compression_enabled", True)
    monkeypatch.setattr(settings, "compression_token_threshold", 1000)
    monkeypatch.setattr(settings, "compression_protect_first_n", 2)
    monkeypatch.setattr(settings, "compression_protect_last_n", 4)
    monkeypatch.setattr(compression, "_get_compression_pool", lambda model=None: object())

    async def bad_summary(_block: str) -> str:
        return "просто проза без структуры"

    async def no_escalation(*_args, **_kwargs):
        return None

    monkeypatch.setattr(compression, "_summarize", bad_summary)
    monkeypatch.setattr(compression, "_escalate_summary", no_escalation)
    sid = _make_session(8)
    try:
        import asyncio

        assert asyncio.run(compression.maybe_compress(sid)) is False
        from aria.db import repository as repo
        from aria.db.base import session_scope

        with session_scope() as db:
            history = repo.list_messages_for_prompt(db, sid)
        assert not any("просто проза" in (msg.content or "") for msg in history), "raw не должен быть заменён"
        assert not any((msg.content or "").startswith(compression.SUMMARY_PREFIX) for msg in history)
    finally:
        _cleanup(str(sid))
