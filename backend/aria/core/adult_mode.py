"""Режим 18+ (этап 3а): состояние, включение фразой и выбор провайдера.

18+ — режим поверх любой роли, не отдельная роль. Жёсткие лимиты из
``content_limits`` действуют всегда и этим модулем не отключаются.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from aria.config import get_settings
from aria.db import repository as repo
from sqlalchemy.orm import Session as OrmSession

MODE_KEY = "modes:default"


def _session_key(session_id) -> str:
    return f"modes:session:{session_id}"


def _default_state() -> dict:
    return {"adult": False, "adult_scope": "session", "adult_confirmed_at": None}


def get_state(db: OrmSession, session_id=None) -> dict:
    base = repo.get_agent_state(db, MODE_KEY) or {}
    state = {**_default_state(), **base}
    if session_id is not None:
        override = repo.get_agent_state(db, _session_key(session_id))
        if override:
            state = {**state, **override}
    return state


def is_enabled(db: OrmSession, session_id=None) -> bool:
    return bool(get_state(db, session_id).get("adult"))


def confirmed(state: dict | None = None) -> bool:
    return bool((state or {}).get("adult_confirmed_at"))


def needs_confirmation() -> bool:
    """В публичной сборке первое включение требует «Мне есть 18 лет»."""
    return not bool(getattr(get_settings(), "adult_default", False))


def set_adult(
    db: OrmSession,
    enabled: bool,
    *,
    session_id=None,
    scope: str = "session",
    mark_confirmed: bool = False,
) -> dict:
    key = _session_key(session_id) if (scope == "session" and session_id is not None) else MODE_KEY
    current = repo.get_agent_state(db, key) or _default_state()
    current["adult"] = bool(enabled)
    current["adult_scope"] = scope
    if mark_confirmed:
        current["adult_confirmed_at"] = datetime.now(timezone.utc).isoformat()
    if not enabled:
        current["adult_confirmed_at"] = None
    repo.set_agent_state(db, key, current, source="user")
    return current


_ON = re.compile(r"(?i)(?:включи|вруби|enable)\s*(?:режим\s*)?18\+?|/adult\s+on|включи\s+взросл")
_OFF = re.compile(r"(?i)(?:выключи|отключи|disable)\s*(?:режим\s*)?18\+?|/adult\s+off|выключи\s+взросл")

_ADULT_WORDS = re.compile(
    r"(?i)18\+|взросл(?:ый|ая|ое)\s+режим|эрот|порно|секс|обнаж|интим|nsfw|"
    r"откровенн(?:ая|ый|ое)\s+(?:сцена|содержание|рассказ)|explicit\s+sex"
)


def detect_command(text: str) -> str | None:
    value = text or ""
    if _OFF.search(value):
        return "off"
    if _ON.search(value):
        return "on"
    return None


def looks_adult(text: str) -> bool:
    return bool(_ADULT_WORDS.search(text or ""))


def available_providers(router) -> list:
    """Провайдеры с флагом adult_ok; список можно расширить конфигом."""
    if router is None:
        return []
    configured = {
        item.strip() for item in str(getattr(get_settings(), "adult_provider_ids", "") or "").split(",")
        if item.strip()
    }
    result = []
    for group in getattr(router, "providers_by_class", {}).values():
        for provider in group:
            if getattr(provider, "adult_ok", False) or provider.provider_id in configured:
                result.append(provider)
    return result
