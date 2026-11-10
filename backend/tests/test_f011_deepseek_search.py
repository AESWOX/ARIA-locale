"""F-011: DeepSeek Search — провайдер на том же ключе; DDG остаётся fallback."""
from __future__ import annotations

import pytest
from aria.tools.search import router as search_router
from aria.tools.search.base import SearchProvider, SearchResult
from aria.tools.search.providers import deepseek as deepseek_mod


@pytest.mark.anyio
async def test_deepseek_provider_calls_search_endpoint(monkeypatch):
    captured: dict = {}

    async def fake_post(url, payload, *, headers=None, max_bytes=None):
        captured.update(url=url, payload=payload, headers=headers)
        return {
            "results": [
                {
                    "title": "DeepSeek V4",
                    "url": "https://example.com/v4",
                    "snippet": "short",
                    "summary": "long summary",
                    "date": "2026-06-01T00:00:00+00:00",
                }
            ]
        }

    monkeypatch.setattr(deepseek_mod, "post_json", fake_post)
    provider = deepseek_mod.DeepSeekSearchProvider(
        api_key="test-key-not-real", base_url="https://api.deepseek.com/v1"
    )

    results = await provider.search("DeepSeek V4", 3)

    assert captured["url"] == "https://api.deepseek.com/v1/tools/search"
    assert captured["headers"]["Authorization"].startswith("Bearer ")
    assert captured["payload"]["count"] == 3
    assert captured["payload"]["need_content"] is False
    assert results and results[0].provider == "deepseek"
    assert (results[0].title, results[0].url) == ("DeepSeek V4", "https://example.com/v4")
    assert results[0].snippet == "short"
    assert results[0].date.startswith("2026-06-01")


@pytest.mark.anyio
async def test_deepseek_requires_key():
    provider = deepseek_mod.DeepSeekSearchProvider(api_key="", base_url="")

    with pytest.raises(RuntimeError):
        await provider.search("q", 1)


class _Boom(SearchProvider):
    name = "deepseek"
    kinds = frozenset({"general"})

    async def search(self, query, max_results):
        raise RuntimeError("HTTP 500")


class _Ddg(SearchProvider):
    name = "duckduckgo"
    kinds = frozenset({"general"})

    async def search(self, query, max_results):
        return [SearchResult(title="fallback", url="https://example.org/f", snippet="s", provider="duckduckgo")]


@pytest.mark.anyio
async def test_router_falls_back_to_ddg_when_deepseek_fails():
    out = await search_router.search(
        "f011 unique fallback query", 5, providers=[_Boom(), _Ddg()], now=0
    )

    assert out is not None
    assert out["results"] and out["results"][0]["title"] == "fallback"
    assert out["results"][0]["provider"] == "duckduckgo"


def test_deepseek_is_first_default_provider(monkeypatch):
    import aria.tools.search.providers as providers_pkg

    sentinel = _Ddg()
    sentinel.name = "deepseek"
    monkeypatch.setattr(providers_pkg, "active_deepseek_provider", lambda: sentinel)
    monkeypatch.setattr(providers_pkg, "active_keyed_providers", list)

    built = providers_pkg.build_default_providers()

    assert built[0] is sentinel
    assert built[-1].name == "duckduckgo"
