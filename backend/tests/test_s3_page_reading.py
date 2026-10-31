"""S3: извлечение основного текста, focus, кэш страниц с etag/TTL."""
from __future__ import annotations

import urllib.error
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from aria.db import models as m
from aria.db.base import session_scope
from aria.http_utils import utc_now
from aria.tools import page_cache
from aria.tools.handlers import web

PAGES = Path(__file__).parent / "fixtures" / "pages"
NOISE = ("Home", "Login", "Subscribe", "Copyright", "Privacy", "Terms", "Contact", "Advertise", "Rules", "FAQ")


class FakeResp:
    def __init__(self, body: bytes, *, ctype: str = "text/html", etag: str | None = None):
        self._body = body
        self._ctype = ctype
        self.headers = {
            "Content-Type": ctype,
            "ETag": etag,
        }
        self.status = 200

    def read(self, n: int = -1) -> bytes:
        chunk, self._body = self._body[:n], self._body[n:]
        return chunk

    def geturl(self) -> str:
        return "https://example.com/page"

    def getheaders(self):
        return list(self.headers.items())

    def info(self):
        return self.headers

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Headers(dict):
    def get_content_type(self):
        return self.get("Content-Type", "text/html").split(";")[0]

    def get_content_charset(self):
        return None


def _resp(body: bytes, *, etag: str | None = None) -> FakeResp:
    r = FakeResp(body, etag=etag)
    r.headers = _Headers(r.headers)
    return r


def _page(body: bytes, monkeypatch, counter: list[str], *, etag: str | None = None) -> None:
    def fake_open(req, timeout):
        counter.append(req.full_url)
        return _resp(body, etag=etag)

    monkeypatch.setattr(web, "_open_url", fake_open)


def test_extraction_removes_boilerplate_and_keeps_facts():
    expectations = {
        "news.html": "bridge reopened",
        "docs.html": "exponential backoff",
        "forum.html": "fixture run twice",
        "blog.html": "local inference",
        "table.html": "$20",
    }
    for name, fact in expectations.items():
        _title, text = web.extract_readable((PAGES / name).read_text(encoding="utf-8"))
        assert fact in text, name
        assert not any(word in text for word in NOISE), (name, text[:200])


def test_meta_date_is_extracted():
    _title, _text, date = web.extract_readable_meta((PAGES / "news.html").read_text(encoding="utf-8"))
    assert date.startswith("2026-10-30")


@pytest.mark.anyio
async def test_focus_prefers_relevant_paragraph(monkeypatch):
    filler = "".join(f"<p>Filler paragraph number {i} about unrelated topics and noise.</p>" for i in range(12))
    key = "<p>Retries must use exponential backoff and an idempotency key.</p>"
    _page((f"<html><body>{filler}{key}</body></html>").encode(), monkeypatch, [])

    out = await web.web_fetch(
        {"url": "https://example.com/page", "max_chars": 220, "focus": "retries backoff idempotency"}
    )

    assert "exponential backoff" in out["content"]
    assert len(out["content"]) <= 220 and out["truncated"] is True


@pytest.mark.anyio
async def test_focus_reduces_default_content_without_losing_answer(monkeypatch):

    filler = "".join(f"<p>Unrelated filler paragraph {i} with menu noise and ads.</p>" for i in range(15))
    key = "<p>Retries must use exponential backoff and an idempotency key.</p>"
    _page((f"<html><body>{filler}{key}</body></html>").encode(), monkeypatch, [])
    plain = await web.web_fetch({"url": "https://example.com/page", "max_chars": 400})
    focused = await web.web_fetch(
        {"url": "https://example.com/page", "max_chars": 400, "focus": "retries exponential backoff idempotency"}
    )

    assert "exponential backoff" in focused["content"]
    assert len(focused["content"]) <= 0.7 * len(plain["content"])


@pytest.mark.anyio
async def test_cache_hit_skips_network(monkeypatch):
    counter: list[str] = []
    _page(b"<html><body><p>Cached page body with facts.</p></body></html>", monkeypatch, counter, etag="e1")

    first = await web.web_fetch({"url": "https://example.com/page"})
    second = await web.web_fetch({"url": "https://example.com/page", "max_chars": 25})

    assert first["content"] and second["content"]
    assert len(counter) == 1, "второй вызов должен брать кэш"
    assert second["source_trust"] == "untrusted"


@pytest.mark.anyio
async def test_expired_cache_refetches(monkeypatch):
    counter: list[str] = []
    _page(b"<html><body><p>Fresh body text here.</p></body></html>", monkeypatch, counter, etag="e1")
    await web.web_fetch({"url": "https://example.com/page"})
    with session_scope() as db:
        row = db.execute(select(m.PageCache)).scalar_one()
        row.created_at = utc_now() - timedelta(hours=25)

    await web.web_fetch({"url": "https://example.com/page"})
    assert len(counter) == 2


@pytest.mark.anyio
async def test_304_returns_stale_cache(monkeypatch):
    url = "https://example.com/page"
    page_cache.put(url, {
        "url": url, "final_url": url, "status": 200, "content_type": "text/html",
        "title": "Old", "date": "", "content": "Old cached content with the answer.",
        "etag": "e1", "last_modified": None, "truncated": False,
    })
    with session_scope() as db:
        row = db.execute(select(m.PageCache)).scalar_one()
        row.created_at = utc_now() - timedelta(hours=25)

    def fake_open(req, timeout):
        assert {k.lower(): v for k, v in req.headers.items()}.get("if-none-match") == "e1"
        raise urllib.error.HTTPError(url, 304, "Not Modified", {"ETag": "e1"}, None)

    monkeypatch.setattr(web, "_open_url", fake_open)

    out = await web.web_fetch({"url": url})

    assert out["content"] == "Old cached content with the answer." and not out.get("error")


def test_migration_upgrade_and_downgrade(tmp_path):
    from alembic import command
    from alembic.config import Config
    import sqlalchemy as sa

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    cfg.attributes["dsn_explicit"] = f"sqlite:///{(tmp_path / 's3.db').as_posix()}"
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "h2c3d4e5f6a7")
    command.upgrade(cfg, "head")

    engine = sa.create_engine(cfg.get_main_option("sqlalchemy.url"))
    assert "page_cache" in sa.inspect(engine).get_table_names()
