"""S1: кэш результатов поиска в search_cache (TTL из настроек)."""
from __future__ import annotations

import logging
from datetime import timedelta

from aria.config import get_settings
from aria.db import models as m
from aria.db.base import session_scope
from aria.http_utils import utc_now

logger = logging.getLogger("local_agent.search.cache")


def _ttl() -> timedelta:
    return timedelta(hours=int(get_settings().search_cache_ttl_hours or 0))


def get(key: str) -> dict | None:
    try:
        with session_scope() as db:
            row = db.get(m.SearchCache, key)
            if row is None:
                return None
            created = row.created_at
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=utc_now().tzinfo)
            if created is None or utc_now() - created > _ttl():
                return None
            return dict(row.payload or {})
    except Exception:  # noqa: BLE001 — кэш не должен ломать поиск
        logger.warning("search cache read failed", exc_info=True)
        return None


def put(key: str, payload: dict) -> None:
    try:
        from sqlalchemy import select

        with session_scope() as db:
            row = db.execute(select(m.SearchCache).where(m.SearchCache.key == key)).scalar_one_or_none()
            if row is None:
                db.add(m.SearchCache(key=key, payload=payload))
            else:
                row.payload = payload
                row.created_at = utc_now()
    except Exception:  # noqa: BLE001 — сбой записи кэша только WARNING
        logger.warning("search cache write failed", exc_info=True)
