"""E4: баланс DeepSeek — реальные цифры, честное «неизвестно», кэш 60 с."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from aria.config import get_settings

logger = logging.getLogger("local_agent.llm.balance")

BALANCE_URL = "https://api.deepseek.com/user/balance"
TIMEOUT_SEC = 10.0
MAX_BODY_BYTES = 20_000
CACHE_TTL_SEC = 60.0

_cache: dict[str, tuple[float, dict]] = {}


def _api_key() -> str | None:
    return get_settings().deepseek_api_key_resolved


def reset_cache() -> None:
    _cache.clear()


def _number(value: Any) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _error(message: str) -> dict:
    return {
        "configured": True,
        "available": None,
        "currency": None,
        "total_balance": None,
        "granted_balance": None,
        "topped_up_balance": None,
        "checked_at": None,
        "error": message,
    }


def _parse(payload: dict) -> dict:
    infos = payload.get("balance_infos") if isinstance(payload, dict) else None
    infos = [i for i in infos if isinstance(i, dict)] if isinstance(infos, list) else []
    chosen = next((i for i in infos if str(i.get("currency")).upper() == "USD"), None)
    if chosen is None:
        chosen = next((i for i in infos if str(i.get("currency")).upper() == "CNY"), None)
    if chosen is None and infos:
        chosen = infos[0]
    chosen = chosen or {}
    return {
        "configured": True,
        "available": bool(payload.get("is_available")) if payload.get("is_available") is not None else None,
        "currency": chosen.get("currency"),
        "total_balance": _number(chosen.get("total_balance")),
        "granted_balance": _number(chosen.get("granted_balance")),
        "topped_up_balance": _number(chosen.get("topped_up_balance")),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "error": None,
    }


async def fetch_balance() -> dict:
    key = _api_key()
    if not key:
        return {
            "configured": False,
            "available": None,
            "currency": None,
            "total_balance": None,
            "granted_balance": None,
            "topped_up_balance": None,
            "checked_at": None,
            "error": None,
        }
    cached = _cache.get(key)
    if cached is not None and time.monotonic() - cached[0] < CACHE_TTL_SEC:
        return cached[1]
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SEC) as client:
            response = await client.get(BALANCE_URL, headers={"Authorization": f"Bearer {key}"})
    except (httpx.TimeoutException, httpx.TransportError, OSError) as exc:
        return _error(f"network: {type(exc).__name__}")
    except Exception as exc:  # noqa: BLE001 — баланс не должен ронять маршрут
        logger.warning("balance request failed: %s", type(exc).__name__)
        return _error(f"request failed: {type(exc).__name__}")
    if response.status_code == 401:
        return _error("unauthorized")
    if response.status_code != 200:
        return _error(f"HTTP {response.status_code}")
    body = response.content or b""
    if len(body) > MAX_BODY_BYTES:
        return _error("response too large")
    try:
        payload = response.json()
    except ValueError:
        return _error("invalid JSON")
    if not isinstance(payload, dict):
        return _error("invalid JSON")
    result = _parse(payload)
    _cache[key] = (time.monotonic(), result)
    return result
