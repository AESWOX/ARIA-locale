"""E2: цены DeepSeek и расчёт пик/не-пик из llm/prices.json (данные, не код)."""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger("local_agent.llm.pricing")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _prices_path() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "aria" / "llm" / "prices.json"
    return Path(__file__).with_name("prices.json")


@lru_cache(maxsize=1)
def load_prices() -> dict:
    try:
        with _prices_path().open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001 — без файла цен честно пишем n/a
        logger.warning("prices.json unavailable; cost will be unknown", exc_info=True)
        return {"models": {}, "aliases": {}, "peak_windows_utc": [], "peak_weekdays": [], "offpeak_dates": []}
    return data if isinstance(data, dict) else {}


def normalize_model(model: str | None) -> str | None:
    if not model:
        return None
    name = str(model).strip().lower()
    aliases = load_prices().get("aliases") or {}
    return str(aliases.get(name) or name)


def _minutes(value: str) -> int | None:
    try:
        hours, minutes = str(value).split(":", 1)
        return int(hours) * 60 + int(minutes)
    except (TypeError, ValueError):
        return None


def tier_for(when: datetime | None = None) -> str:
    """'peak' или 'off' для момента времени (UTC). Выходные и offpeak_dates — off."""
    moment = when or _utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    spec = load_prices()
    if moment.date().isoformat() in set(spec.get("offpeak_dates") or []):
        return "off"
    if moment.weekday() not in set(spec.get("peak_weekdays") or []):
        return "off"
    current = moment.hour * 60 + moment.minute
    for window in spec.get("peak_windows_utc") or []:
        if not isinstance(window, (list, tuple)) or len(window) != 2:
            continue
        start, end = _minutes(window[0]), _minutes(window[1])
        if start is not None and end is not None and start <= current < end:
            return "peak"
    return "off"


def cost_usd(
    model: str | None,
    hit: int | None,
    miss: int | None,
    out: int | None,
    when: datetime | None = None,
) -> tuple[float | None, str]:
    """Стоимость вызова в USD и тариф. Unknown model -> (None, 'n/a')."""
    spec = load_prices()
    name = normalize_model(model)
    rates = (spec.get("models") or {}).get(name or "")
    tier = tier_for(when)
    if not isinstance(rates, dict):
        return None, "n/a"
    price = rates.get(tier) or rates.get("off")
    if not isinstance(price, dict):
        return None, "n/a"
    try:
        usd = (
            max(0, int(hit or 0)) / 1_000_000 * float(price["hit"])
            + max(0, int(miss or 0)) / 1_000_000 * float(price["miss"])
            + max(0, int(out or 0)) / 1_000_000 * float(price["out"])
        )
    except (KeyError, TypeError, ValueError):
        return None, "n/a"
    return round(usd, 6), tier


def next_offpeak(now: datetime | None = None) -> datetime:
    """Ближайший момент off-peak: if already off — сам now, иначе ближайшая минута окна off."""
    moment = now or _utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if tier_for(moment) == "off":
        return moment
    cursor = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(8 * 24 * 60):
        if tier_for(cursor) == "off":
            return cursor
        cursor += timedelta(minutes=1)
    return moment
