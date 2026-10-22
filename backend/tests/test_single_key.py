"""Один ключ: пользователь с единственным ключом провайдера (здесь DeepSeek) должен иметь рабочие все текстовые пути.

С одним ключом DeepSeek в роутере только классы standard_reasoning и premium_reasoning; free_tier_reasoning,
subagent_execution и vision_multimodal пусты. Каждое место, которое просит один из пустых классов, обязано
уходить в запасной класс, а не падать с «no available provider».
"""
from __future__ import annotations

import pytest

from aria.config import get_settings
from aria.core.loop import AGENT_FALLBACK_CLASSES
from aria.core.roles import ROLE_REGISTRY
from aria.llm import compression
from aria.llm.providers.base import ChatMessage, LlmResponse
from aria.llm.providers.openai_compatible import OpenAICompatibleProvider
from aria.llm.router import ProviderUnavailable, build_default_router
from aria.routers.chat import CHAT_FALLBACK_CLASSES, CHAT_PROVIDER_CLASS

_KEY_ENVS = (
    "GEMINI_API_KEYS", "GEMINI_API_KEY", "GROQ_API_KEYS", "GROQ_API_KEY", "VISION_GEMINI_API_KEYS",
    "COMPRESSION_GEMINI_API_KEYS", "DEEPSEEK_API_KEY",
)

# Класс и запасные классы каждого места, которое ходит в модель (значения из исходников, а не придуманные):
# executor.py (Oracle, шаги, содержимое файлов), clarifier.py, audit.py, chat.py, loop.py.
CALL_SITES = {
    "chat": (CHAT_PROVIDER_CLASS, CHAT_FALLBACK_CLASSES),
    "executor_oracle": ("premium_reasoning", ("standard_reasoning", "free_tier_reasoning")),
    "executor_step": ("subagent_execution", ("free_tier_reasoning", "standard_reasoning")),
    "clarifier": ("standard_reasoning", ("free_tier_reasoning", "premium_reasoning")),
    "audit": ("standard_reasoning", ("standard_reasoning", "free_tier_reasoning")),
    "compression_via_router": ("free_tier_reasoning", ("standard_reasoning", "premium_reasoning")),
    **{f"loop_role_{rid}": (r.default_model_policy, AGENT_FALLBACK_CLASSES) for rid, r in ROLE_REGISTRY.items()},
}


@pytest.fixture
def deepseek_only(monkeypatch):
    for name in _KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek-only-1234567890")
    get_settings.cache_clear()
    calls: list[str] = []

    async def fake_chat(self, messages, tools, timeout_sec):
        calls.append(self.provider_id)
        return LlmResponse(text="ok-from-" + self.provider_id)

    async def fake_connectivity(self, timeout_sec):
        return True

    monkeypatch.setattr(OpenAICompatibleProvider, "chat", fake_chat)
    monkeypatch.setattr(OpenAICompatibleProvider, "check_connectivity", fake_connectivity)
    router = build_default_router()
    yield router, calls
    get_settings.cache_clear()


def test_only_deepseek_classes_exist_with_one_key(deepseek_only):
    router, _ = deepseek_only
    present = {cls for cls, providers in router.providers_by_class.items() if providers}
    assert present == {"standard_reasoning", "premium_reasoning"}


@pytest.mark.parametrize("site", sorted(CALL_SITES))
async def test_every_call_site_works_with_a_single_deepseek_key(deepseek_only, site):
    router, calls = deepseek_only
    cls, fallback = CALL_SITES[site]
    result = await router.route_chat(
        cls, [ChatMessage(role="user", content="ping")], [], timeout_sec=5, resilient=True, fallback_classes=fallback,
    )
    assert result.response.text.startswith("ok-from-deepseek")
    assert calls, "запрос должен дойти до DeepSeek"


async def test_a_class_without_fallback_would_fail_proving_the_matrix_is_meaningful(deepseek_only):
    router, _ = deepseek_only
    with pytest.raises(ProviderUnavailable):
        await router.route_chat("subagent_execution", [ChatMessage(role="user", content="ping")], [],
                                timeout_sec=5, resilient=True, fallback_classes=())


async def test_compression_uses_the_router_when_there_is_no_gemini_key(deepseek_only):
    router, calls = deepseek_only
    assert compression._get_compression_pool() is None
    assert await compression._summarize_via_router(router, "[user] привет\n[assistant] привет") == "ok-from-deepseek-chat"


async def test_maybe_compress_summarizes_through_router_without_gemini(deepseek_only, monkeypatch):
    from tests.test_ctx_compression import _cleanup, _make_session

    router, calls = deepseek_only
    s = get_settings()
    monkeypatch.setattr(s, "compression_enabled", True)
    monkeypatch.setattr(s, "compression_token_threshold", 1000)
    monkeypatch.setattr(s, "compression_hard_message_limit", 0)
    monkeypatch.setattr(s, "compression_protect_first_n", 2)
    monkeypatch.setattr(s, "compression_protect_last_n", 4)
    sid = _make_session(8)
    try:
        assert await compression.maybe_compress(sid, router) is True
        assert calls == ["deepseek-chat"]
    finally:
        _cleanup(str(sid))


async def test_maybe_compress_without_router_and_without_gemini_still_leaves_history_intact(deepseek_only, monkeypatch):
    from tests.test_ctx_compression import _cleanup, _make_session

    s = get_settings()
    monkeypatch.setattr(s, "compression_enabled", True)
    monkeypatch.setattr(s, "compression_token_threshold", 1000)
    monkeypatch.setattr(s, "compression_hard_message_limit", 0)
    monkeypatch.setattr(s, "compression_protect_first_n", 2)
    monkeypatch.setattr(s, "compression_protect_last_n", 4)
    sid = _make_session(8)
    try:
        assert await compression.maybe_compress(sid) is False  # прежнее поведение без роутера: не падает
    finally:
        _cleanup(str(sid))


# ── профиль запуска (стиль solo/boss, ярус моделей) тоже не должен требовать второго провайдера ──────────────

from aria.core import runprofile  # noqa: E402


async def _ping(router, cls, fallback):
    r = await router.route_chat(cls, [ChatMessage(role="user", content="ping")], [], timeout_sec=5,
                                resilient=True, fallback_classes=fallback)
    return r.response.text


@pytest.mark.parametrize("tier", sorted(runprofile.TIERS))
@pytest.mark.parametrize("style", runprofile.STYLES)
async def test_run_profiles_work_with_one_key(deepseek_only, style, tier):
    router, _ = deepseek_only
    profile = {"style": style, "audit": "strict", "thinking": "off", "main": {"tier": tier}, "workers": {"tier": tier}, "auditor": {}}

    # агентский цикл: корневая задача и делегированная (depth 1)
    for depth in (0, 1):
        cls, _prefer = runprofile.resolve_for_task(profile, "subagent_execution", depth, router)
        assert (await _ping(router, cls, AGENT_FALLBACK_CLASSES)).startswith("ok-from-deepseek")

    # обычный чат
    cls, fallback, _prefer = runprofile.chat_target(profile, router, CHAT_PROVIDER_CLASS, CHAT_FALLBACK_CLASSES)
    assert (await _ping(router, cls, fallback)).startswith("ok-from-deepseek")

    # аудитор (запасные классы зафиксированы в audit.py)
    _level, auditor_cls, _prefer = runprofile.audit_settings(profile, router)
    assert (await _ping(router, auditor_cls, ("standard_reasoning", "free_tier_reasoning"))).startswith("ok-from-deepseek")
