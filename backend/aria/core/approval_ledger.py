"""Session-scoped approval ledger (0.2.1).

An owner can approve one exact tool call for the whole session ("allow always"). The ledger is
keyed by ``(session_id, tool_name, args_hash)`` and stored in ``agent_state`` under
``approval_ledger:<session_id>`` — no schema migration, the same store the rest of the agent
state uses. Entries expire after ``approval_ledger_ttl_hours``; ``once`` entries are consumed by
the next identical call.

Only an explicit ``mode="always"`` approval creates a lasting entry. The default "allow once"
approval keeps the old behaviour: it lifts exactly one card and is not stored.
"""
from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session as OrmSession

from aria.core.guardrails import ToolCallSignature
from aria.db import repository as repo

MODE_ONCE = "once"
MODE_ALWAYS = "always"
MODES = (MODE_ONCE, MODE_ALWAYS)

KEY_PREFIX = "approval_ledger:"
MAX_ENTRIES = 200


def args_hash(tool_name: str, arguments: Mapping[str, Any] | None) -> str:
    return ToolCallSignature.from_call(tool_name, arguments or {}).args_hash


def _key(session_id: uuid.UUID | str) -> str:
    return f"{KEY_PREFIX}{session_id}"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _ttl_hours() -> float:
    from aria.config import get_settings

    return float(getattr(get_settings(), "approval_ledger_ttl_hours", 24) or 24)


def _parse(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


def _read(db: OrmSession, session_id: uuid.UUID | str) -> list[dict]:
    raw = repo.get_agent_state(db, _key(session_id)) or {}
    entries = raw.get("entries") if isinstance(raw, Mapping) else None
    return [dict(entry) for entry in (entries or []) if isinstance(entry, Mapping)]


def _write(db: OrmSession, session_id: uuid.UUID | str, entries: list[dict]) -> None:
    repo.set_agent_state(
        db, _key(session_id), {"entries": entries[-MAX_ENTRIES:]}, source="approval_ledger"
    )


def _live(entries: list[dict], now: dt.datetime) -> list[dict]:
    live = []
    for entry in entries:
        expires = _parse(entry.get("expires_at"))
        if expires is not None and expires > now:
            live.append(entry)
    return live


def _match(entries: list[dict], tool_name: str, fingerprint: str) -> dict | None:
    for entry in entries:
        if entry.get("tool") == tool_name and entry.get("args_hash") == fingerprint:
            return entry
    return None


def record(
    db: OrmSession,
    session_id: uuid.UUID | str,
    tool_name: str,
    arguments: Mapping[str, Any] | None,
    *,
    mode: str = MODE_ALWAYS,
    task_id: uuid.UUID | str | None = None,
    ttl_hours: float | None = None,
) -> dict:
    if mode not in MODES:
        raise ValueError(f"unknown approval mode: {mode!r}")
    now = _now()
    ttl = _ttl_hours() if ttl_hours is None else float(ttl_hours)
    fingerprint = args_hash(tool_name, arguments)
    entries = [
        entry
        for entry in _live(_read(db, session_id), now)
        if not (entry.get("tool") == tool_name and entry.get("args_hash") == fingerprint)
    ]
    entry = {
        "tool": tool_name,
        "args_hash": fingerprint,
        "mode": mode,
        "task_id": str(task_id) if task_id is not None else None,
        "created_at": now.isoformat(),
        "expires_at": (now + dt.timedelta(hours=ttl)).isoformat(),
    }
    entries.append(entry)
    _write(db, session_id, entries)
    return entry


def consume(
    db: OrmSession, session_id: uuid.UUID | str, tool_name: str, arguments: Mapping[str, Any] | None
) -> str | None:
    """Return the live approval mode for this exact call, or ``None``.

    ``once`` entries are removed: they lift exactly one card. ``always`` entries stay until TTL.
    """
    now = _now()
    entries = _live(_read(db, session_id), now)
    fingerprint = args_hash(tool_name, arguments)
    match = _match(entries, tool_name, fingerprint)
    if match is None:
        return None
    if str(match.get("mode")) == MODE_ALWAYS:
        return MODE_ALWAYS
    entries.remove(match)
    _write(db, session_id, entries)
    return MODE_ONCE


def clear(db: OrmSession, session_id: uuid.UUID | str) -> None:
    _write(db, session_id, [])
