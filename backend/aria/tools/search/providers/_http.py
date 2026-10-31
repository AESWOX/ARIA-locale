"""S2a: общий HTTP-слой поисковых адаптеров (лимиты, UA, коды)."""
from __future__ import annotations

import html
import re

import httpx

from aria.version import APP_VERSION

TIMEOUT_SEC = 8.0
MAX_BYTES = 1_000_000
USER_AGENT = f"ARIA/{APP_VERSION} (+https://github.com/AESWOX/ARIA-locale)"
_TAGS = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    return html.unescape(_TAGS.sub("", html.unescape(text or ""))).strip()


async def get_bytes(url: str, params: dict | None = None, *, max_bytes: int = MAX_BYTES, headers: dict | None = None) -> bytes:
    merged = {**(headers or {}), "User-Agent": USER_AGENT}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SEC, headers=merged) as client:
            async with client.stream("GET", url, params=params) as response:
                if response.status_code in (403, 429):
                    from aria.tools.search.base import SearchRateLimited

                    raise SearchRateLimited(f"HTTP {response.status_code}")
                if response.status_code != 200:
                    raise RuntimeError(f"HTTP {response.status_code}")
                buf = bytearray()
                async for chunk in response.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        raise RuntimeError("response too large")
                return bytes(buf)
    except RuntimeError:
        raise
    except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPError, OSError) as exc:
        raise RuntimeError(f"{type(exc).__name__}") from exc


async def get_json(url: str, params: dict | None = None, *, max_bytes: int = MAX_BYTES, headers: dict | None = None) -> dict:
    import json

    try:
        data = json.loads((await get_bytes(url, params, max_bytes=max_bytes, headers=headers)).decode("utf-8", "replace"))
    except ValueError as exc:
        raise RuntimeError("invalid JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("invalid JSON")
    return data


async def post_json(url: str, payload: dict, *, headers: dict | None = None, max_bytes: int = MAX_BYTES) -> dict:
    """S7: POST JSON ??? keyed-???????????; ????? ?????? ? headers, ?? ? URL."""
    import json

    merged = {**(headers or {}), "Content-Type": "application/json", "User-Agent": USER_AGENT}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SEC, headers=merged) as client:
            async with client.stream("POST", url, content=body) as response:
                if response.status_code in (403, 429):
                    from aria.tools.search.base import SearchRateLimited

                    raise SearchRateLimited(f"HTTP {response.status_code}")
                if response.status_code != 200:
                    raise RuntimeError(f"HTTP {response.status_code}")
                buf = bytearray()
                async for chunk in response.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        raise RuntimeError("response too large")
                raw = bytes(buf)
    except RuntimeError:
        raise
    except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPError, OSError) as exc:
        raise RuntimeError(f"{type(exc).__name__}") from exc
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise RuntimeError("invalid JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("invalid JSON")
    return data
