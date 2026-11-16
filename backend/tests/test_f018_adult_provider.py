"""18+: взрослый запрос получает провайдера (DeepSeek помечен adult_ok), а не
упирается в «нет провайдера с флагом adult_ok»."""
from __future__ import annotations

from types import SimpleNamespace

from aria.core.adult_mode import available_providers
from aria.llm.providers.openai_compatible import OpenAICompatibleProvider


def _provider(provider_id: str, *, adult_ok: bool) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        provider_id=provider_id,
        provider_class="standard_reasoning",
        base_url="https://api.deepseek.com/v1",
        model=provider_id,
        api_key="test-key-not-real",
        adult_ok=adult_ok,
    )


def test_deepseek_is_adult_approved():
    deepseek = _provider("deepseek-chat", adult_ok=True)
    gemini = _provider("gemini-flash", adult_ok=False)
    router = SimpleNamespace(providers_by_class={"standard_reasoning": [gemini, deepseek]})

    assert available_providers(router) == [deepseek]


def test_non_adult_provider_stays_out_of_adult_gate():
    gemini = _provider("gemini-flash", adult_ok=False)
    router = SimpleNamespace(providers_by_class={"standard_reasoning": [gemini]})

    assert available_providers(router) == []
