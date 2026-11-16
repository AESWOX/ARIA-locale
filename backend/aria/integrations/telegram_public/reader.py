"""T1: чтение публичных каналов через t.me/s (без аккаунта, фиксированный хост)."""
from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

from aria.integrations.telegram_public.parser import parse_posts
from aria.version import APP_VERSION

logger = logging.getLogger("local_agent.telegram_public")

BASE_URL = "https://t.me/s/"
USER_AGENT = f"ARIA/{APP_VERSION} (+https://github.com/AESWOX/ARIA-locale)"
TIMEOUT_SEC = 10.0
MAX_BYTES = 2_000_000
MAX_PAGES = 5
MIN_INTERVAL_SEC = 3.0
_CHANNEL_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")

_CACHE: dict[tuple[str, str], dict] = {}
_RATE_LOCK = asyncio.Lock()
_last_request = 0.0


class TelegramPublicError(Exception):
    """Публичный канал недоступен/страница не распознана/нарушен лимит."""


def reset_cache() -> None:
    _CACHE.clear()


def valid_channel(name: str) -> str:
    value = (name or "").strip().lstrip("@")
    if not _CHANNEL_RE.fullmatch(value):
        raise TelegramPublicError("invalid public channel name")
    return value


def _looks_unavailable(html: str) -> bool:
    return "tgme_widget_message" not in html and (
        "Preview channel" in html or "tgme_page_extra" in html or "cannot be displayed" in html
    )


def _looks_channel(html: str) -> bool:
    return "tgme_channel_info" in html or "tgme_channel_history" in html


async def _fetch(channel: str, before: str | None, *, transport, clock, sleep) -> str:
    global _last_request
    async with _RATE_LOCK:
        now = clock()
        delay = MIN_INTERVAL_SEC - (now - _last_request)
        if delay > 0:
            await sleep(delay)
            now = clock()
        _last_request = now
    params = {"before": before} if before else None
    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT_SEC, follow_redirects=False, transport=transport,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            async with client.stream("GET", f"{BASE_URL}{channel}", params=params) as response:
                if 300 <= response.status_code < 400:
                    raise TelegramPublicError("redirect is not allowed for t.me public pages")
                if response.status_code == 404:
                    raise TelegramPublicError("channel unavailable without an account")
                if response.status_code != 200:
                    raise TelegramPublicError(f"t.me HTTP {response.status_code}")
                buf = bytearray()
                async for chunk in response.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > MAX_BYTES:
                        raise TelegramPublicError("t.me page is too large")
                return bytes(buf).decode("utf-8", "replace")
    except TelegramPublicError:
        raise
    except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPError, OSError) as exc:
        raise TelegramPublicError(f"t.me request failed: {type(exc).__name__}") from exc


async def read_channel(
    channel: str,
    pages: int = 1,
    before: str | None = None,
    *,
    transport=None,
    clock=None,
    sleep=None,
) -> list[dict]:
    """Публичные сообщения канала; приватные/недоступные страницы честно отклоняются."""
    name = valid_channel(channel)
    page_count = max(1, min(int(pages or 1), MAX_PAGES))
    clock = clock or time.monotonic
    sleep = sleep or asyncio.sleep
    cursor = str(before) if before else None
    collected: list[dict] = []
    for _page in range(page_count):
        html = await _fetch(name, cursor, transport=transport, clock=clock, sleep=sleep)
        posts = parse_posts(html)
        if not posts:
            if _looks_unavailable(html):
                raise TelegramPublicError("channel unavailable without an account")
            if _looks_channel(html):
                return collected
            raise TelegramPublicError("t.me page layout is not recognized")
        fresh = []
        for post in posts:
            key = (name, str(post["message_id"]))
            if key in _CACHE:
                continue
            _CACHE[key] = post
            fresh.append(post)
        collected.extend(fresh)
        if not fresh:
            break
        try:
            cursor = str(min(int(str(post["message_id"])) for post in posts))
        except ValueError as exc:
            raise TelegramPublicError("t.me message id is not numeric") from exc
    return collected
