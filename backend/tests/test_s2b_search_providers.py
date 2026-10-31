"""S2b: StackExchange/GitHub/OpenAlex на фикстурах, без реальной сети."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from aria.config import get_settings
from aria.secrets import secret_provider
from aria.tools.search.base import SearchRateLimited
from aria.tools.search.providers import (
    GitHubProvider,
    OpenAlexProvider,
    StackExchangeProvider,
    build_default_providers,
)

FIXTURES = Path(__file__).parent / "fixtures" / "search"
_REAL_CLIENT = httpx.AsyncClient


def _transport(monkeypatch, handler) -> None:
    def factory(*args, **kwargs):
        return _REAL_CLIENT(transport=httpx.MockTransport(handler), timeout=8, headers=kwargs.get("headers"))

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _body(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.mark.anyio
async def test_stackexchange_parses_and_strips(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k: request.url.params.get(k) for k in ("site", "order", "sort", "pagesize")})
        return httpx.Response(200, content=_body("stackexchange.json"))

    _transport(monkeypatch, handler)

    results = await StackExchangeProvider().search("python", 2)

    assert seen == {"site": "stackoverflow", "order": "desc", "sort": "relevance", "pagesize": "2"}

    assert len(results) == 2 and results[0].source == "stackexchange"
    assert results[0].title == 'How to "fix" Python error'
    assert results[0].date.startswith("2024-") and results[0].url.startswith("https://stackoverflow.com/")


@pytest.mark.anyio
async def test_github_token_header_not_url(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, content=_body("github.json"))

    monkeypatch.setattr(secret_provider, "get_key", lambda name: "tok-123" if name == "GITHUB_SEARCH_TOKEN" else None)
    _transport(monkeypatch, handler)

    results = await GitHubProvider().search("aria", 2)

    assert seen["auth"] == "Bearer tok-123"
    assert "tok-123" not in seen["url"]
    assert results[0].snippet == "Local agent with tools"
    assert results[1].snippet == ""


@pytest.mark.anyio
async def test_github_403_is_rate_limited(monkeypatch):
    monkeypatch.setattr(secret_provider, "get_key", lambda name: None)
    _transport(monkeypatch, lambda request: httpx.Response(403))

    with pytest.raises(SearchRateLimited):
        await GitHubProvider().search("aria", 1)


@pytest.mark.anyio
async def test_openalex_mailto_param_and_abstract(monkeypatch):
    seen = {}
    monkeypatch.setattr(get_settings(), "openalex_mailto", "owner@example.com")

    def handler(request: httpx.Request) -> httpx.Response:
        seen["mailto"] = request.url.params.get("mailto")
        seen["per_page"] = request.url.params.get("per-page")
        return httpx.Response(200, content=_body("openalex.json"))

    _transport(monkeypatch, handler)

    results = await OpenAlexProvider().search("diffusion", 2)

    assert seen["mailto"] == "owner@example.com" and seen["per_page"] == "2"
    assert results[0].snippet.startswith("We study diffusion models")
    assert "doi:" in results[0].snippet
    assert "doi:" not in results[1].snippet


@pytest.mark.anyio
async def test_openalex_without_mailto(monkeypatch):
    seen = {}
    monkeypatch.setattr(get_settings(), "openalex_mailto", "")

    def handler(request: httpx.Request) -> httpx.Response:
        seen["mailto"] = request.url.params.get("mailto")
        return httpx.Response(200, content=_body("openalex.json"))

    _transport(monkeypatch, handler)
    await OpenAlexProvider().search("q", 1)
    assert seen["mailto"] is None


@pytest.mark.anyio
async def test_empty_and_http_error(monkeypatch):
    _transport(monkeypatch, lambda request: httpx.Response(200, content=b'{"items": []}'))
    assert await StackExchangeProvider().search("q", 1) == []

    _transport(monkeypatch, lambda request: httpx.Response(500))
    with pytest.raises(RuntimeError) as exc:
        await OpenAlexProvider().search("q", 1)
    assert "HTTP 500" in str(exc.value)


def test_factory_has_seven_providers():
    names = [p.name for p in build_default_providers()]
    assert names == ["wikipedia", "hackernews", "arxiv", "stackexchange", "github", "openalex", "duckduckgo"]


@pytest.mark.anyio
async def test_stackexchange_429_is_rate_limited(monkeypatch):
    _transport(monkeypatch, lambda request: httpx.Response(429))
    with pytest.raises(SearchRateLimited):
        await StackExchangeProvider().search("q", 1)


@pytest.mark.anyio
async def test_github_429_is_rate_limited(monkeypatch):
    monkeypatch.setattr(secret_provider, "get_key", lambda name: None)
    _transport(monkeypatch, lambda request: httpx.Response(429))
    with pytest.raises(SearchRateLimited):
        await GitHubProvider().search("q", 1)


@pytest.mark.anyio
async def test_user_agent_cannot_be_overridden(monkeypatch):
    from aria.tools.search.providers._http import get_bytes

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("User-Agent")
        return httpx.Response(200, content=b"{}")

    _transport(monkeypatch, handler)
    await get_bytes("https://example.invalid/", headers={"User-Agent": "spoofed"})

    assert seen["ua"].startswith("ARIA/")
