"""S7: keyed-адаптеры и SearXNG на фикстурах, без реальной сети и ключей."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from aria.config import get_settings
from aria.secrets import secret_provider
from aria.tools.search.base import SearchRateLimited
from aria.tools.search.providers import build_default_providers
from aria.tools.search.providers.keyed import (
    BraveProvider,
    ExaProvider,
    SerperProvider,
    TavilyProvider,
)
from aria.tools.search.providers.searxng import SearXNGProvider, valid_searxng_url

FIX = Path(__file__).parent / "fixtures" / "search"
_REAL = httpx.AsyncClient


def _transport(monkeypatch, handler):
    def factory(*args, **kwargs):
        return _REAL(transport=httpx.MockTransport(handler), timeout=8, headers=kwargs.get("headers"))

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _body(name):
    return (FIX / name).read_bytes()


@pytest.mark.anyio
async def test_tavily_parse_and_bearer(monkeypatch):
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, content=_body("tavily.json"))

    _transport(monkeypatch, handler)
    results = await TavilyProvider("fake-key").search("q", 1)

    assert seen["auth"] == "Bearer fake-key" and "fake-key" not in seen["url"]
    assert results[0].provider == "tavily" and "Tavily content" in results[0].snippet


@pytest.mark.anyio
async def test_brave_header_and_parse(monkeypatch):
    seen = {}

    def handler(request):
        seen["key"] = request.headers.get("X-Subscription-Token")
        return httpx.Response(200, content=_body("brave.json"))

    _transport(monkeypatch, handler)
    results = await BraveProvider("brave-key").search("q", 1)

    assert seen["key"] == "brave-key" and results[0].url == "https://b.example/1"


@pytest.mark.anyio
async def test_exa_and_serper_headers(monkeypatch):
    seen = {}

    def handler(request):
        seen[request.url.host] = request.headers.get("x-api-key") or request.headers.get("X-API-KEY")
        return httpx.Response(200, content=_body("exa.json" if "exa.ai" in request.url.host else "serper.json"))

    _transport(monkeypatch, handler)
    exa = await ExaProvider("exa-key").search("q", 1)
    serper = await SerperProvider("serper-key").search("q", 1)

    assert seen["api.exa.ai"] == "exa-key" and seen["google.serper.dev"] == "serper-key"
    assert exa[0].url == "https://e.example/1" and serper[0].url == "https://s.example/1"


@pytest.mark.anyio
async def test_401_is_hard_error_429_rate_limited(monkeypatch):
    _transport(monkeypatch, lambda request: httpx.Response(401))
    with pytest.raises(RuntimeError) as exc:
        await TavilyProvider("bad").search("q", 1)
    assert "HTTP 401" in str(exc.value)

    _transport(monkeypatch, lambda request: httpx.Response(403))
    with pytest.raises(SearchRateLimited):
        await BraveProvider("bad").search("q", 1)


@pytest.mark.anyio
async def test_searxng_parse_and_validation(monkeypatch):
    _transport(monkeypatch, lambda request: httpx.Response(200, content=_body("searxng.json")))
    results = await SearXNGProvider("http://localhost:8080/").search("q", 1)
    assert results[0].provider == "searxng" and results[0].url == "https://x.example/1"

    assert valid_searxng_url("ftp://x") is None
    assert valid_searxng_url("http://user:pass@host") is None
    assert valid_searxng_url("http://localhost:8080/") == "http://localhost:8080"


def test_factory_order_with_keys_and_searxng(monkeypatch):
    monkeypatch.setattr(secret_provider, "get_key", lambda name: "k" if name == "TAVILY_API_KEY" else None)
    monkeypatch.setattr(get_settings(), "searxng_url", "http://localhost:8080")
    names = [p.name for p in build_default_providers()]
    assert names[:2] == ["tavily", "searxng"]
    assert names[-1] == "duckduckgo"

    monkeypatch.setattr(secret_provider, "get_key", lambda name: None)
    monkeypatch.setattr(get_settings(), "searxng_url", "")
    assert [p.name for p in build_default_providers()] == [
        "wikipedia", "hackernews", "arxiv", "stackexchange", "github", "openalex", "duckduckgo",
    ]


def test_active_keyed_only_with_key(monkeypatch):
    from aria.tools.search.providers.keyed import active_keyed_providers

    monkeypatch.setattr(secret_provider, "get_key", lambda name: "k" if name == "BRAVE_API_KEY" else None)
    assert [p.name for p in active_keyed_providers()] == ["brave"]

    monkeypatch.setattr(secret_provider, "get_key", lambda name: None)
    assert active_keyed_providers() == []


@pytest.mark.anyio
async def test_searxng_empty_and_broken_json(monkeypatch):
    _transport(monkeypatch, lambda request: httpx.Response(200, content=b"{}"))
    assert await SearXNGProvider("http://localhost:8080").search("q", 5) == []

    _transport(monkeypatch, lambda request: httpx.Response(200, content=b'{"results": []}'))
    assert await SearXNGProvider("http://localhost:8080").search("q", 5) == []

    _transport(monkeypatch, lambda request: httpx.Response(200, content=b"not json"))
    with pytest.raises(RuntimeError) as exc:
        await SearXNGProvider("http://localhost:8080").search("q", 5)
    assert "invalid JSON" in str(exc.value)
