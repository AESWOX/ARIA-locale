"""S3: кэш прочитанных страниц (TTL, etag, только успешные ответы)."""
from __future__ import annotations

import logging
from datetime import timedelta

from aria.config import get_settings
from aria.db import models as m
from aria.db.base import session_scope
from aria.http_utils import utc_now

logger = logging.getLogger("local_agent.web.page_cache")


def _ttl() -> timedelta:
    return timedelta(hours=int(get_settings().page_cache_ttl_hours or 0))


def _fresh(row: m.PageCache) -> bool:
    created = row.created_at
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=utc_now().tzinfo)
    return created is not None and utc_now() - created <= _ttl()


def get(url: str) -> dict | None:
    row = _row(url)
    if row is None or not _fresh(row):
        return None
    return _payload(row)


def get_stale(url: str) -> dict | None:
    row = _row(url)
    return _payload(row) if row is not None else None


def _row(url: str):
    try:
        with session_scope() as db:
            return db.get(m.PageCache, url)
    except Exception:  # noqa: BLE001 — кэш не должен ломать чтение
        logger.warning("page cache read failed", exc_info=True)
        return None


def _payload(row: m.PageCache) -> dict:
    return {
        "url": row.url,
        "final_url": row.final_url or row.url,
        "status": row.status,
        "content_type": row.content_type,
        "title": row.title,
        "date": row.date,
        "content": row.content,
        "truncated": bool(row.truncated),
        "etag": row.etag,
        "last_modified": row.last_modified,
    }


def put(url: str, data: dict) -> None:
    if int(data.get("status") or 0) != 200:
        return
    try:
        with session_scope() as db:
            row = db.get(m.PageCache, url)
            if row is None:
                row = m.PageCache(url=url)
                db.add(row)
            row.final_url = str(data.get("final_url") or url)
            row.etag = data.get("etag")
            row.last_modified = data.get("last_modified")
            row.title = str(data.get("title") or "")
            row.date = str(data.get("date") or "")
            row.content = str(data.get("content") or "")
            row.status = int(data.get("status") or 200)
            row.content_type = str(data.get("content_type") or "text/html")
            row.truncated = bool(data.get("truncated"))
            row.created_at = utc_now()
    except Exception:  # noqa: BLE001 — сбой кэша только WARNING
        logger.warning("page cache write failed", exc_info=True)
