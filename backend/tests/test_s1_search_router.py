"""S1: роутер поиска — параллельность, дедуп, кэш, кулдаун, классификатор."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from aria.db import models as m
from aria.db.base import session_scope
from aria.http_utils import utc_now
from aria.tools.handlers import web as web_handler
from aria.tools.search import cache as search_cache
from aria.tools.search import router as search_router
from aria.tools.search.base import SearchProvider, SearchRateLimited, SearchResult


class FakeProvider(SearchProvider):
    def __init__(self, name: str, results=None, *, raises: Exception | None = None, delay: float = 0.0):
        self.name = name
        self.kinds = frozenset({"general"})
        self._results = results or []
        self._raises = raises
        self._delay = delay
        self.calls = 0

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        return list(self._results)


def _result(url: str, title: str = "t") -> SearchResult:
    return SearchResult(title=title, url=url, snippet="s")


@pytest.fixture(autouse=True)
def _clean_router():
    search_router.reset_providers()
    yield
    search_router.reset_providers()


@pytest.mark.anyio
async def test_one_provider_failure_does_not_break_answer():
    good = FakeProvider("good", [_result("https://a.example/1")])
    bad = FakeProvider("bad", raises=RuntimeError("boom"))

    payload = await search_router.search("q", 5, providers=[bad, good])

    assert len(payload["results"]) == 1 and payload["results"][0]["provider"] == "good"


@pytest.mark.anyio
async def test_dedup_by_url_without_tracking():
    first = FakeProvider("one", [_result("https://Example.com/Page/?utm_source=a&q=1")])
    second = FakeProvider("two", [_result("https://example.com/Page?utm_medium=b&q=1")])

    payload = await search_router.search("q", 5, providers=[first, second])

    assert len(payload["results"]) == 1
    assert "utm_" not in payload["results"][0]["url"]


@pytest.mark.anyio
async def test_cache_hit_skips_providers():
    provider = FakeProvider("cached", [_result("https://a.example/1")])

    first = await search_router.search("Cache Me", 5, providers=[provider])
    second = await search_router.search("cache   me", 5, providers=[provider])

    assert first["results"] == second["results"]
    assert provider.calls == 1


@pytest.mark.anyio
async def test_expired_cache_calls_provider():
    provider = FakeProvider("expired", [_result("https://a.example/1")])
    await search_router.search("expire", 5, providers=[provider])
    with session_scope() as db:
        row = list(db.execute(select(m.SearchCache)).scalars())[0]
        row.created_at = utc_now() - timedelta(hours=25)

    await search_router.search("expire", 5, providers=[provider])

    assert provider.calls == 2


@pytest.mark.anyio
async def test_cache_write_failure_only_warns(monkeypatch, caplog):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(search_cache, "session_scope", boom)
    provider = FakeProvider("warn", [_result("https://a.example/1")])

    payload = await search_router.search("warn", 5, providers=[provider])

    assert payload["results"]
    assert "search cache write failed" in caplog.text


def test_classifier_examples():
    cases = {
        "python traceback list index": "code",
        "how to use api in javascript": "code",
        "pytest fixture error": "code",
        "arxiv paper on diffusion": "science",
        "doi 10.1000/xyz": "science",
        "научная статья про теорему": "science",
        "release of new model 2026": "tech_news",
        "новости технологий": "tech_news",
        "launch today": "tech_news",
        "как приготовить борщ": "general",
        "погода завтра": "general",
        "столица Франции": "general",
    }
    assert len(cases) == 12
    for query, expected in cases.items():
        assert search_router.classify(query) == expected


@pytest.mark.anyio
async def test_cooldown_after_rate_limit_and_expiry():
    limited = FakeProvider("limited", raises=SearchRateLimited("429"))
    now = 1000.0
    first = await search_router.search("cool", 5, providers=[limited], now=now)
    assert "cooling down" in first["error"] or "rate limited" in first["error"]

    await search_router.search("cool", 5, providers=[limited], now=now + 5)
    assert limited.calls == 1, "во время кулдауна провайдер не вызывается"

    await search_router.search("cool2", 5, providers=[limited], now=now + 601)
    assert limited.calls == 2, "после кулдауна провайдер снова доступен"


@pytest.mark.anyio
async def test_timeout_returns_error(monkeypatch):
    monkeypatch.setattr(search_router, "TOTAL_TIMEOUT_SEC", 0.05)
    slow = FakeProvider("slow", [_result("https://a.example/1")], delay=1.0)

    payload = await search_router.search("slow", 5, providers=[slow])

    assert payload["results"] == [] and "timeout" in payload["error"]


@pytest.mark.anyio
async def test_empty_results_error_lists_reasons():
    failing = FakeProvider("bad", raises=RuntimeError("boom"))
    empty = FakeProvider("empty", [])

    payload = await search_router.search("none", 5, providers=[failing, empty])

    assert payload["results"] == []
    assert "bad" in payload["error"]


@pytest.mark.anyio
async def test_web_search_uses_provider_and_keeps_format(monkeypatch):
    provider = FakeProvider("fake", [_result("https://a.example/1", "T")])
    search_router.register_provider(provider)

    payload = await web_handler.web_search({"query": "q"})

    assert set(payload) >= {"results", "source_trust", "notice"}
    assert payload["results"][0]["provider"] == "fake"
    assert payload["source_trust"] == "untrusted"


@pytest.mark.anyio
async def test_web_search_falls_back_to_ddg_when_no_providers(monkeypatch):
    called = {}

    def _legacy(q, n):
        called["q"] = q
        return {"results": [], "note": "no results found"}

    monkeypatch.setattr(web_handler, "_search_blocking", _legacy)

    payload = await web_handler.web_search({"query": "hello"})

    assert called["q"] == "hello" and payload["note"] == "no results found"


def test_migration_upgrade_and_downgrade(tmp_path):
    from alembic import command
    from alembic.config import Config
    import sqlalchemy as sa
    from pathlib import Path

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    cfg.attributes["dsn_explicit"] = f"sqlite:///{(tmp_path / 's1.db').as_posix()}"
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "g1b2c3d4e5f6")
    command.upgrade(cfg, "head")

    engine = sa.create_engine(cfg.get_main_option("sqlalchemy.url"))
    assert "search_cache" in sa.inspect(engine).get_table_names()


@pytest.mark.anyio
async def test_cooldown_filter_before_cap_keeps_healthy_provider():
    providers = [FakeProvider(f"cool-{i}", [_result(f"https://c{i}.example/")]) for i in range(3)]
    healthy = FakeProvider("healthy", [_result("https://ok.example/")])
    now = 500.0
    for p in providers:
        search_router._COOLDOWN[p.name] = now + 600

    payload = await search_router.search("q", 5, providers=providers + [healthy], now=now)

    assert payload["results"][0]["provider"] == "healthy"
    assert [p.calls for p in providers] == [0, 0, 0]


@pytest.mark.anyio
async def test_cached_payload_gets_untrusted_overlay():
    provider = FakeProvider("trusted-cache", [_result("https://a.example/1")])
    await search_router.search("trust", 5, providers=[provider])
    from sqlalchemy import select

    with session_scope() as db:
        row = list(db.execute(select(m.SearchCache)).scalars())[0]
        row.payload = {**row.payload, "source_trust": "trusted"}

    payload = await search_router.search("trust", 5, providers=[provider])
    assert payload["source_trust"] == "untrusted"


def test_provider_selection_by_kind_and_cap():
    code = FakeProvider("code", [_result("https://code.example/")])
    code.kinds = frozenset({"code"})
    science = FakeProvider("science", [_result("https://science.example/")])
    science.kinds = frozenset({"science"})
    general = FakeProvider("general", [_result("https://general.example/")])
    general.kinds = frozenset({"general"})
    extra = [FakeProvider(f"g{i}", [_result(f"https://g{i}.example/")]) for i in range(3)]
    for p in extra:
        p.kinds = frozenset({"general"})

    import asyncio
    asyncio.run(search_router.search("python traceback error", 10, providers=[science, code, general, *extra]))

    assert science.calls == 0, "code-запрос не идёт к science-only провайдеру"
    assert general.calls == 1, "general-провайдер выбран"
    assert sum(p.calls for p in extra) + code.calls + general.calls == 3, "не более 3 провайдеров"


@pytest.mark.anyio
async def test_cache_read_failure_only_warns(monkeypatch, caplog):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(search_cache, "session_scope", boom)
    provider = FakeProvider("read-fail", [_result("https://a.example/1")])

    payload = await search_router.search("readfail", 5, providers=[provider])

    assert payload["results"]
    assert "search cache read failed" in caplog.text
