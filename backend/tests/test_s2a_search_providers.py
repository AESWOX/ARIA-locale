"""S2a: Wikipedia/HN/arXiv на фикстурах, без реальной сети."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from aria.tools.search.base import SearchRateLimited
from aria.tools.search.providers import build_default_providers
from aria.tools.search.providers import arxiv as arxiv_mod
from aria.tools.search.providers import hackernews as hn_mod
from aria.tools.search.providers import wikipedia as wiki_mod

FIXTURES = Path(__file__).parent / "fixtures" / "search"


_REAL_CLIENT = httpx.AsyncClient


def _transport(monkeypatch, handler) -> None:
    def factory(*args, **kwargs):
        return _REAL_CLIENT(transport=httpx.MockTransport(handler), timeout=8)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _body(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.mark.anyio
async def test_wikipedia_parses_and_strips_html(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        seen["query"] = request.url.params.get("srsearch")
        return httpx.Response(200, content=_body("wikipedia.json"))

    _transport(monkeypatch, handler)
    results = await wiki_mod.WikipediaProvider().search("python язык", 2)

    assert seen["host"] == "ru.wikipedia.org" and seen["query"] == "python язык"
    assert len(results) == 2 and results[0].source == "wikipedia"
    assert "<span" not in results[0].snippet and "&" in results[0].snippet
    assert results[0].url.startswith("https://ru.wikipedia.org/wiki/")


@pytest.mark.anyio
async def test_wikipedia_ukrainian_detection(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        return httpx.Response(200, content=_body("wikipedia.json"))

    _transport(monkeypatch, handler)
    await wiki_mod.WikipediaProvider().search("історія", 1)
    assert seen["host"] == "uk.wikipedia.org"


@pytest.mark.anyio
async def test_hn_parses_fallback_url(monkeypatch):
    _transport(monkeypatch, lambda request: httpx.Response(200, content=_body("hn.json")))

    results = await hn_mod.HackerNewsProvider().search("aria", 5)

    assert results[0].url == "https://example.com/aria"
    assert results[0].snippet == "text bold"
    assert results[1].url == "https://news.ycombinator.com/item?id=222"


@pytest.mark.anyio
async def test_arxiv_parses_namespace_and_strips_html(monkeypatch):
    _transport(monkeypatch, lambda request: httpx.Response(200, content=_body("arxiv.xml")))

    results = await arxiv_mod.ArxivProvider().search("diffusion", 2)

    assert len(results) == 2 and results[0].source == "arxiv"
    assert "diffusion" in results[0].snippet and "<b>" not in results[0].snippet


@pytest.mark.anyio
async def test_arxiv_xxe_is_rejected(monkeypatch):
    _transport(monkeypatch, lambda request: httpx.Response(200, content=_body("arxiv_xxe.xml")))

    with pytest.raises(RuntimeError) as exc:
        await arxiv_mod.ArxivProvider().search("xxe", 1)

    assert "DTD" in str(exc.value)


@pytest.mark.anyio
async def test_arxiv_malformed_xml(monkeypatch):
    _transport(monkeypatch, lambda request: httpx.Response(200, content=b"<feed><entry>"))

    with pytest.raises(RuntimeError) as exc:
        await arxiv_mod.ArxivProvider().search("bad", 1)

    assert "invalid XML" in str(exc.value)


@pytest.mark.anyio
async def test_rate_limit_and_http_error(monkeypatch):
    _transport(monkeypatch, lambda request: httpx.Response(429))
    with pytest.raises(SearchRateLimited):
        await wiki_mod.WikipediaProvider().search("q", 1)

    _transport(monkeypatch, lambda request: httpx.Response(503))
    with pytest.raises(RuntimeError) as exc:
        await hn_mod.HackerNewsProvider().search("q", 1)
    assert "HTTP 503" in str(exc.value)


@pytest.mark.anyio
async def test_connection_error_wrapped(monkeypatch):
    def handler(request: httpx.Request):
        raise httpx.ConnectError("no dns")

    _transport(monkeypatch, handler)
    with pytest.raises(RuntimeError) as exc:
        await wiki_mod.WikipediaProvider().search("q", 1)
    assert "ConnectError" in str(exc.value)


@pytest.mark.anyio
async def test_response_size_limit(monkeypatch):
    _transport(monkeypatch, lambda request: httpx.Response(200, content=b"x" * 1_000_001))

    with pytest.raises(RuntimeError) as exc:
        await wiki_mod.WikipediaProvider().search("q", 1)
    assert "too large" in str(exc.value)


@pytest.mark.anyio
async def test_empty_and_garbage_json(monkeypatch):
    _transport(monkeypatch, lambda request: httpx.Response(200, content=b'{"query": {"search": []}}'))
    assert await wiki_mod.WikipediaProvider().search("q", 1) == []

    _transport(monkeypatch, lambda request: httpx.Response(200, content=b"not json"))
    with pytest.raises(RuntimeError) as exc:
        await wiki_mod.WikipediaProvider().search("q", 1)
    assert "invalid JSON" in str(exc.value)


def test_default_factory():
    providers = build_default_providers()
    assert [p.name for p in providers] == ["wikipedia", "hackernews", "arxiv"]
    assert providers[0].kinds and providers[2].kinds == frozenset({"science"})
