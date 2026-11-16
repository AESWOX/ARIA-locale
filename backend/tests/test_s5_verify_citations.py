"""S5: проверка цитат без LLM — подтверждено/не подтверждено/недоступно, лимит 5, кэш."""
from __future__ import annotations

import pytest

from aria.core import verify_citations as vc
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.tools import page_cache


def _fetch_map(mapping):
    calls = []

    async def fetch(url):
        calls.append(url)
        value = mapping.get(url)
        if isinstance(value, Exception):
            raise value
        return value

    return fetch, calls


def test_extract_quotes_and_numbers():
    text = (
        'В отчёте сказано «курс вырос на 12 процентов» (https://example.com/a). '
        "Another fact 42 at https://example.com/b."
    )
    pairs = vc.extract_citations(text)

    assert pairs[0] == ("https://example.com/a", "курс вырос на 12 процентов")
    assert pairs[1][0] == "https://example.com/b" and pairs[1][1] == "42"


@pytest.mark.anyio
async def test_confirmed_and_fabricated():
    fetch, _ = _fetch_map({
        "https://example.com/a": "Курс вырос на 12 процентов в октябре.",
        "https://example.com/b": "Совсем другой текст без нужных слов.",
    })

    good = await vc.verify_citation("https://example.com/a", "курс вырос на 12 процентов", fetch=fetch)
    bad = await vc.verify_citation("https://example.com/b", "курс вырос на 12 процентов", fetch=fetch)

    assert good.status == "confirmed"
    assert bad.status == "not_confirmed"


@pytest.mark.anyio
async def test_unavailable_and_limit_five():
    urls = [f"https://example.com/{i}" for i in range(6)]
    fetch, calls = _fetch_map({url: "текст" for url in urls})
    answer = " ".join(f"«факт номер {i} здесь» {url}" for i, url in enumerate(urls))

    citations = await vc.verify_answer(answer, fetch=fetch)

    assert len(calls) == 5
    assert len({c.url for c in citations}) == 5

    failing, _ = _fetch_map({"https://example.com/x": RuntimeError("boom")})
    unavailable = await vc.verify_citation("https://example.com/x", "факт", fetch=failing)
    assert unavailable.status == "unavailable"


@pytest.mark.anyio
async def test_default_fetch_uses_cache_no_network(monkeypatch):
    monkeypatch.setattr(
        page_cache, "get",
        lambda url: {"content": "Курс вырос на 12 процентов в октябре."},
    )
    from aria.tools.handlers import web

    async def _no_network(_payload):
        raise AssertionError("сеть не должна вызываться при попадании в кэш")

    monkeypatch.setattr(web, "web_fetch", _no_network)

    citation = await vc.verify_citation("https://example.com/a", "курс вырос на 12 процентов")

    assert citation.status == "confirmed"


@pytest.mark.anyio
async def test_summary_and_research_append():
    fetch, _ = _fetch_map({"https://example.com/a": "Курс вырос на 12 процентов в октябре."})
    citations = await vc.verify_answer("«курс вырос на 12 процентов» https://example.com/a", fetch=fetch)
    text = vc.summary(citations)
    assert "подтверждено 1" in text and "не подтверждено 0" in text

    with session_scope() as db:
        session = repo.create_session(db, "s5", active_role="research")
        task = repo.create_task(db, session, role="research", objective="research")
        repo.append_message(
            db, session, role="assistant",
            content="«курс вырос на 12 процентов» https://example.com/a",
        )
        assert await vc.maybe_verify_research_answer(db, session, task, fetch=fetch) is not None
        messages = [m.content for m in repo.list_messages(db, session.id, limit=20)]
    assert any(m.startswith("Проверка цитат:") for m in messages)


@pytest.mark.anyio
async def test_non_research_task_untouched():
    fetch, calls = _fetch_map({"https://example.com/a": "x"})
    with session_scope() as db:
        session = repo.create_session(db, "s5g", active_role="general")
        task = repo.create_task(db, session, role="general", objective="x")
        repo.append_message(db, session, role="assistant", content="«факт» https://example.com/a")
        result = await vc.maybe_verify_research_answer(db, session, task, fetch=fetch)
        count = len(repo.list_messages(db, session.id, limit=20))

    assert result is None and calls == [] and count == 1
