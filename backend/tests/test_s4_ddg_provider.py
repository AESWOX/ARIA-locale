"""S4: DDG-провайдер, кулдаун после блока, debug только по флагу."""
from __future__ import annotations

import pytest

from aria.tools.handlers import web
from aria.tools.search import router as search_router
from aria.tools.search.providers import build_default_providers, install_default_providers
from aria.tools.search.providers.ddg import DuckDuckGoProvider


@pytest.fixture(autouse=True)
def _clean_router():
    search_router.reset_providers()
    yield
    search_router.reset_providers()


@pytest.mark.anyio
async def test_ddg_provider_maps_results(monkeypatch):
    monkeypatch.setattr(
        web,
        "_search_blocking",
        lambda q, n: {"results": [{"title": "T", "url": "https://a.example/", "snippet": "S"}], "query": q},
    )

    results = await DuckDuckGoProvider().search("q", 5)

    assert results[0].provider == "duckduckgo" and results[0].url == "https://a.example/"


@pytest.mark.anyio
async def test_ddg_blocked_raises_rate_limit_and_router_cools(monkeypatch):
    calls = {"n": 0}

    def blocked(q, n):
        calls["n"] += 1
        return {"results": [], "blocked": True, "error": "duckduckgo blocked"}

    monkeypatch.setattr(web, "_search_blocking", blocked)
    provider = DuckDuckGoProvider()


    first = await search_router.search("q", 5, providers=[provider], now=1000.0)
    second = await search_router.search("q2", 5, providers=[provider], now=1005.0)
    assert "rate limited" in first["error"] or "cooling" in first["error"]
    assert "cooling" in second["error"]
    assert calls["n"] == 1


@pytest.mark.anyio
async def test_ddg_genuine_empty_and_network_error(monkeypatch):
    monkeypatch.setattr(web, "_search_blocking", lambda q, n: {"results": [], "note": "no results found"})
    assert await DuckDuckGoProvider().search("q", 5) == []

    monkeypatch.setattr(web, "_search_blocking", lambda q, n: {"results": [], "error": "request failed: dns"})
    with pytest.raises(RuntimeError):
        await DuckDuckGoProvider().search("q", 5)


def test_debug_pages_only_with_env(monkeypatch, tmp_path):
    monkeypatch.delenv("ARIA_DEBUG_SEARCH", raising=False)
    monkeypatch.setattr(web.paths, "data_dir", lambda: tmp_path)
    assert web._save_debug_page("lite", "<html>blocked</html>") is None
    assert not (tmp_path / "debug").exists()

    monkeypatch.setenv("ARIA_DEBUG_SEARCH", "1")
    saved = web._save_debug_page("lite", "<html>blocked</html>")
    assert saved and (tmp_path / "debug").exists()


def test_factory_ddg_last_and_install_idempotent():
    names = [p.name for p in build_default_providers()]
    assert names[-1] == "duckduckgo" and len(names) == 7

    install_default_providers()
    install_default_providers()
    assert len(search_router._PROVIDERS) == 7
