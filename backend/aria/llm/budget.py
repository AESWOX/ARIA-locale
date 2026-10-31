"""E3: бюджет по факту из llm_usage (не оценка по token_estimate)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from aria.db import models as m
from aria.db.base import session_scope
from aria.http_utils import utc_now

# Верхняя граница (peak) из COST_MODEL §3, USD за типовую задачу.
FALLBACK_PEAK_USD = {"chat": 0.001, "project_q": 0.036, "research": 0.034, "swarm": 0.065}
DEFAULT_KIND = "project_q"


def _spend_since(since: datetime) -> tuple[float, bool]:
    with session_scope() as db:
        costs = list(
            db.execute(select(m.LlmUsage.cost_usd).where(m.LlmUsage.created_at >= since)).scalars()
        )
    known = [float(c) for c in costs if c is not None]
    return sum(known), any(c is None for c in costs)


def compute_budget_status(daily_usd: float, weekly_usd: float) -> dict:
    """Суммы по llm_usage; форма daily_pct/weekly_pct/daily_usd/weekly_usd сохранена."""
    now = utc_now()
    local_midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = local_midnight.astimezone(timezone.utc)  # SQLite stores naive UTC; compare in UTC
    day, day_incomplete = _spend_since(day_start)
    week, week_incomplete = _spend_since(now - timedelta(days=7))
    return {
        "daily_pct": round(day * 100 / daily_usd, 1) if daily_usd > 0 else 0,
        "weekly_pct": round(week * 100 / weekly_usd, 1) if weekly_usd > 0 else 0,
        "daily_usd": round(day, 4),
        "weekly_usd": round(week, 4),
        "incomplete": bool(day_incomplete or week_incomplete),
    }


def task_spend(task_id) -> float:
    with session_scope() as db:
        costs = list(
            db.execute(select(m.LlmUsage.cost_usd).where(m.LlmUsage.task_id == task_id)).scalars()
        )
    return round(sum(float(c) for c in costs if c is not None), 6)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    pos = (len(values) - 1) * pct / 100.0
    low = int(pos)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (pos - low)


def estimate_task_cost(kind: str | None = None) -> dict:
    """p50/p90 по последним 20 задачам с ценой; при <5 задачах — peak-фолбэк COST_MODEL §3."""
    with session_scope() as db:
        rows = db.execute(
            select(m.LlmUsage.task_id, func.sum(m.LlmUsage.cost_usd), func.max(m.LlmUsage.created_at))
            .where(m.LlmUsage.task_id.isnot(None), m.LlmUsage.cost_usd.isnot(None))
            .group_by(m.LlmUsage.task_id)
            .order_by(func.max(m.LlmUsage.created_at).desc())
            .limit(20)
        ).all()
    values = sorted(float(total) for _task, total, _ts in rows if total is not None)
    if len(values) >= 5:
        return {
            "p50": round(_percentile(values, 50), 6),
            "p90": round(_percentile(values, 90), 6),
            "n": len(values),
            "source": "history",
        }
    fallback = FALLBACK_PEAK_USD.get(kind or DEFAULT_KIND, FALLBACK_PEAK_USD[DEFAULT_KIND])
    return {"p50": fallback, "p90": fallback, "n": len(values), "source": "fallback"}
