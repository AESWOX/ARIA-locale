"""core/delegate.py — §7.2/§7.3.

Один основной event loop (принцип §4.2): саб-агент — не отдельный процесс,
а рекурсивный вызов той же loop-функции с урезанным role/tool_whitelist и
запретом повышать бюджетный класс модели (§7.2).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Sequence

from aria.config import get_settings
from aria.core.roles import get_role

# Значения по умолчанию (§7.2). Реальные лимиты читаются из Settings (.env) через max_depth()/max_parallel().
MAX_DEPTH = 1
MAX_PARALLEL = 3
# 19.5: жёсткий потолок роя и порог свободной RAM (Q-6).
SWARM_CAP = 3
LOW_RAM_MB = 1500
DEFAULT_TIMEOUT_SEC = 60
RETRY_COUNT = 1


def max_depth() -> int:
    """Максимальная глубина делегирования: settings.delegate_max_depth (D2)."""
    return int(get_settings().delegate_max_depth)


def max_parallel() -> int:
    """Максимум параллельных саб-агентов: settings.delegate_max_parallel (D2)."""
    return int(get_settings().delegate_max_parallel)


def available_ram_mb() -> int | None:
    """Свободная RAM через taskrunner (единая точка подмены в тестах)."""
    from aria.core import taskrunner

    return taskrunner.available_ram_mb()


def parallel_limit() -> tuple[int, str]:
    """Эффективный лимит субагентов и причина (19.5 Q-6).

    Порядок: флаг роя → низкая RAM → потолок 3. Причина отдаётся в API.
    """
    if not bool(getattr(get_settings(), "swarm_enabled", True)):
        return 1, "swarm disabled (swarm_enabled=false) — последовательный запуск"
    ram = available_ram_mb()
    if ram is not None and ram < LOW_RAM_MB:
        return 1, f"мало свободной RAM: {ram} MB < {LOW_RAM_MB} MB — 1 саб-агент"
    configured = max(1, max_parallel())
    if configured > SWARM_CAP:
        return SWARM_CAP, f"потолок роя {SWARM_CAP} (delegate_max_parallel={configured})"
    return configured, f"delegate_max_parallel={configured}"


@dataclass
class DelegateRequest:
    session_id: str
    parent_task_id: str
    role: str
    objective: str
    allowed_tools: list[str]
    allow_paid: bool = False
    hard_timeout_sec: int = DEFAULT_TIMEOUT_SEC
    depth: int = 0


@dataclass
class DelegateResult:
    status: str  # ok|failed|timeout|cancelled|exhausted_free_tier
    role: str
    summary: str
    artifacts: list[str]
    tool_calls_count: int
    error: str | None = None


async def delegate_task(request: DelegateRequest, run_subtask_fn) -> DelegateResult:
    """run_subtask_fn — callable(role, objective, allowed_tools, allow_paid) -> DelegateResult,
    внедряется вызывающим кодом (core/loop.py), чтобы избежать циклического импорта
    и явно провести depth/budget инварианты."""
    limit = max_depth()
    if request.depth >= limit:
        return DelegateResult(status="failed", role=request.role, summary="", artifacts=[], tool_calls_count=0, error=f"max delegation depth ({limit}) exceeded, §7.2")

    try:
        get_role(request.role)
    except (KeyError, PermissionError) as exc:  # D4: get_role бросает, а не возвращает None
        return DelegateResult(status="failed", role=request.role, summary="", artifacts=[], tool_calls_count=0, error=f"unknown or disabled role: {request.role} ({exc})")

    last_error: str | None = None
    for attempt in range(RETRY_COUNT + 1):
        try:
            coro = run_subtask_fn(request.role, request.objective, request.allowed_tools, request.allow_paid)
            result: DelegateResult = await asyncio.wait_for(coro, timeout=request.hard_timeout_sec)
            return result
        except asyncio.TimeoutError:
            last_error = "timeout"
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)

    status = "timeout" if last_error == "timeout" else "failed"
    return DelegateResult(status=status, role=request.role, summary="", artifacts=[], tool_calls_count=0, error=last_error)


async def run_bounded(factories: Sequence[Callable[[], Awaitable[Any]]], limit: int | None = None) -> list[Any]:
    """B5-D1: исполнить несколько делегирований ОДНОВРЕМЕННО, но не больше ``limit`` за раз.

    ``factories`` — вызываемые без аргументов, каждый возвращает корутину (корутина создаётся только когда
    освободился слот). Порядок результатов совпадает с порядком ``factories``. Лимит по умолчанию —
    ``delegate_max_parallel`` из Settings (§7.2); избыточные вызовы не отбрасываются, а ждут своей очереди.
    Исключение одной корутины не обрывает остальные: оно возвращается на её месте как объект исключения.
    """
    cap = max(1, int(limit)) if limit is not None else parallel_limit()[0]
    gate = asyncio.Semaphore(cap)

    async def _one(factory: Callable[[], Awaitable[Any]]) -> Any:
        async with gate:
            return await factory()

    return list(await asyncio.gather(*(_one(f) for f in factories), return_exceptions=True))
