"""S1: роутер поисковых провайдеров — класс запроса, параллельность, кэш, кулдаун."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aria.config import get_settings
from aria.tools.search import cache as search_cache
from aria.tools.search.base import SearchProvider, SearchRateLimited
from aria.tools.search.classifier import classify

logger = logging.getLogger("local_agent.search.router")

MAX_PROVIDERS = 3
TOTAL_TIMEOUT_SEC = 12.0
_UTRACKING = re.compile(r"^utm_", re.IGNORECASE)

_PROVIDERS: list[SearchProvider] = []
_COOLDOWN: dict[str, float] = {}  # в памяти процесса: рестарт сбрасывает


def register_provider(provider: SearchProvider) -> None:
    _PROVIDERS.append(provider)


def reset_providers() -> None:
    _PROVIDERS.clear()
    _COOLDOWN.clear()


def normalize_query(query: str) -> str:
    return " ".join((query or "").split()).lower()


def normalize_url(url: str) -> str:
    parts = urlsplit((url or "").strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _UTRACKING.match(k)]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def _cache_key(query: str, names: list[str], max_results: int) -> str:
    raw = "|".join([normalize_query(query), ",".join(sorted(names)), str(max_results)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _call(provider: SearchProvider, query: str, max_results: int) -> tuple[str, list, str, bool]:
    try:
        results = await provider.search(query, max_results)
        return provider.name, list(results or []), "", False
    except SearchRateLimited as exc:
        return provider.name, [], f"{provider.name}: rate limited ({exc})", True
    except Exception as exc:  # noqa: BLE001 — падение одного провайдера не ломает ответ
        return provider.name, [], f"{provider.name}: {type(exc).__name__}", False


async def search(
    query: str,
    max_results: int,
    *,
    providers: list[SearchProvider] | None = None,
    now: float | None = None,
) -> dict | None:
    """None — провайдеров нет (legacy DDG-путь). Иначе прежний формат ответа."""
    pool = list(providers if providers is not None else _PROVIDERS)
    if not pool:
        return None
    moment = time.monotonic() if now is None else now
    kind = classify(query)
    # фильтр кулдауна до капа: здоровый 4-й провайдер должен попасть в выборку
    available = [
        p for p in pool
        if (kind in p.kinds or "general" in p.kinds) and _COOLDOWN.get(p.name, 0.0) <= moment
    ]
    chosen = available[:MAX_PROVIDERS]
    if not chosen:
        chosen = [p for p in pool if _COOLDOWN.get(p.name, 0.0) <= moment][:MAX_PROVIDERS]
    reasons: list[str] = []
    if not chosen:
        return {
            "results": [],
            "error": "search failed: all providers are cooling down",
            "source_trust": "untrusted",
        }
    key = _cache_key(query, [p.name for p in chosen], max_results)
    cached = search_cache.get(key)
    if cached is not None:
        cached["source_trust"] = "untrusted"
        return cached

    semaphore = asyncio.Semaphore(MAX_PROVIDERS)

    async def _guarded(provider: SearchProvider):
        async with semaphore:
            return await _call(provider, query, max_results)

    try:
        gathered = await asyncio.wait_for(
            asyncio.gather(*(_guarded(p) for p in chosen), return_exceptions=True),
            timeout=TOTAL_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        return {
            "results": [],
            "error": f"search failed: timeout after {int(TOTAL_TIMEOUT_SEC)}s",
            "source_trust": "untrusted",
        }

    merged: list[dict] = []
    seen: set[str] = set()
    cooldown_sec = 60 * int(get_settings().search_provider_cooldown_min or 0)
    for item in gathered:
        if isinstance(item, BaseException):
            reasons.append(f"provider failed: {type(item).__name__}")
            continue
        name, results, reason, rate_limited = item
        if rate_limited:
            if cooldown_sec > 0:
                _COOLDOWN[name] = moment + cooldown_sec
        if reason:
            reasons.append(reason)
            logger.info("search provider=%s failed reason=%s", name, reason)
        for result in results:
            url = normalize_url(getattr(result, "url", ""))
            if not url or url in seen:
                continue
            seen.add(url)
            merged.append(
                {
                    "title": getattr(result, "title", ""),
                    "url": url,
                    "snippet": getattr(result, "snippet", ""),
                    "date": getattr(result, "date", ""),
                    "source": getattr(result, "source", ""),
                    "provider": getattr(result, "provider", "") or name,
                }
            )
            if len(merged) >= max_results:
                break
        if len(merged) >= max_results:
            break

    if not merged:
        return {
            "results": [],
            "error": "search failed: " + ("; ".join(reasons) if reasons else "no results"),
            "source_trust": "untrusted",
        }
    payload = {"results": merged, "source_trust": "untrusted"}
    search_cache.put(key, payload)
    return payload
