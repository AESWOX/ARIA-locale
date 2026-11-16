"""F-005: суточные квоты free-tier по проектам (RPD), а не «на ключ».

Лимиты Google считаются на проект; у владельца 10 проектов. Бакет ведётся по
имени модели (Flash-Lite 500 RPD/проект, 3.8/3.6 Flash 20 RPD/проект) и
сбрасывается в полночь UTC. Значения не секреты, но ключи сюда не попадают.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("aria.llm.quota")

# (подстрока модели, RPD на один проект)
MODEL_DAILY_RPD: tuple[tuple[str, int], ...] = (
    ("flash-lite", 500),
    ("3.8-flash", 20),
    ("3.6-flash", 20),
    ("2.5-flash", 20),
)

_LOCK = threading.Lock()
_STATE: dict[str, dict[str, int]] = {}  # date -> model -> used


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _path() -> Path:
    from aria import paths

    return paths.data_dir() / "quota.json"


def _load() -> dict[str, dict[str, int]]:
    global _STATE
    if _STATE:
        return _STATE
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _STATE = {str(day): {str(k): int(v) for k, v in (models or {}).items()} for day, models in data.items()}
    except (OSError, ValueError):
        _STATE = {}
    return _STATE


def _save(state: dict[str, dict[str, int]]) -> None:
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logger.warning("quota: state file is not writable", exc_info=True)


def daily_limit(model: str | None, projects: int = 1) -> int | None:
    """RPD на модель с учётом числа проектов; None — модель без free-квоты."""
    name = str(model or "").lower()
    for marker, per_project in MODEL_DAILY_RPD:
        if marker in name:
            return per_project * max(1, int(projects or 1))
    return None


def used(model: str | None) -> int:
    with _LOCK:
        return _load().get(_today(), {}).get(str(model or ""), 0)


def exhausted(model: str | None, projects: int = 1) -> bool:
    limit = daily_limit(model, projects)
    return limit is not None and used(model) >= limit


def record(model: str | None) -> None:
    """Считать попытку (успех или отказ) — RPD считает запросы, а не токены."""
    name = str(model or "")
    if not name:
        return
    with _LOCK:
        state = _load()
        day = _today()
        models = state.setdefault(day, {})
        models[name] = int(models.get(name, 0)) + 1
        _save(state)


def reset() -> None:
    """Только для тестов: очистить счётчики в памяти и на диске."""
    global _STATE
    with _LOCK:
        _STATE = {}
        try:
            _path().unlink()
        except OSError:
            pass
