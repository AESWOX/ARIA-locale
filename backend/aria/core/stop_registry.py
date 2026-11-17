"""In-process Stop signals for running agent loops (STOP-1).

The durable marker that a task is paused lives in ``agent_loop:<task_id>``
(see :mod:`aria.core.loop_state`); this registry only carries the live signal that
interrupts an in-flight LLM call, tool step or delegate batch inside one process.
Never persisted: a process restart has nothing to stop.

Signals are loop-agnostic on purpose: a request may arrive from the HTTP loop or a sync
thread, while the waiter belongs to the agent loop. Waiter futures are therefore bound to
their own loop and resolved through ``call_soon_threadsafe`` when needed.
"""
from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

_requested: set[str] = set()
_waiters: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Future]]] = {}


def _key(task_id: uuid.UUID | str) -> str:
    return str(task_id)


def _resolve(future: asyncio.Future) -> None:
    if not future.done():
        future.set_result(None)


def _cancel(future: Any) -> None:
    with contextlib.suppress(Exception):
        future.cancel()


def request(task_id: uuid.UUID | str) -> None:
    key = _key(task_id)
    _requested.add(key)
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    for loop, future in list(_waiters.get(key, ())):
        if future.done():
            continue
        if running is loop:
            _resolve(future)
        else:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(_resolve, future)


def is_set(task_id: uuid.UUID | str) -> bool:
    return _key(task_id) in _requested


def clear(task_id: uuid.UUID | str) -> None:
    _requested.discard(_key(task_id))


async def wait(task_id: uuid.UUID | str) -> None:
    key = _key(task_id)
    if key in _requested:
        return
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    _waiters.setdefault(key, set()).add((loop, future))
    try:
        await future
    finally:
        waiters = _waiters.get(key)
        if waiters is not None:
            waiters.discard((loop, future))
            if not waiters:
                _waiters.pop(key, None)


def reset() -> None:
    """Test helper: forget every signal and release pending waiters."""
    _requested.clear()
    for waiters in list(_waiters.values()):
        for _loop, future in waiters:
            _cancel(future)
    _waiters.clear()
