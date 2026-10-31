"""19.5: лимит роя ≤3, RAM-лимит и видимая причина в API."""
from __future__ import annotations

import asyncio

from aria.config import get_settings
from aria.core import delegate as delegate_mod
from aria.core.taskrunner import TaskRunner


def _ram(monkeypatch, value):
    monkeypatch.setattr("aria.core.taskrunner.available_ram_mb", lambda: value)


def test_parallel_limit_low_ram_is_one_with_reason(monkeypatch):
    _ram(monkeypatch, 500)
    monkeypatch.setattr(get_settings(), "delegate_max_parallel", 3)

    limit, reason = delegate_mod.parallel_limit()

    assert limit == 1 and "RAM" in reason


def test_parallel_limit_none_ram_uses_configured(monkeypatch):
    _ram(monkeypatch, None)
    monkeypatch.setattr(get_settings(), "delegate_max_parallel", 3)

    assert delegate_mod.parallel_limit() == (3, "delegate_max_parallel=3")


def test_parallel_limit_caps_configured_above_three(monkeypatch):
    _ram(monkeypatch, 8000)
    monkeypatch.setattr(get_settings(), "delegate_max_parallel", 5)

    limit, reason = delegate_mod.parallel_limit()

    assert limit == 3 and "потолок" in reason


def test_parallel_limit_enough_ram_uses_configured(monkeypatch):
    _ram(monkeypatch, 8000)
    monkeypatch.setattr(get_settings(), "delegate_max_parallel", 3)

    assert delegate_mod.parallel_limit() == (3, "delegate_max_parallel=3")


def test_swarm_disabled_runs_delegations_sequentially(monkeypatch):
    _ram(monkeypatch, 8000)
    monkeypatch.setattr(get_settings(), "swarm_enabled", False)
    state = {"live": 0, "max_live": 0}

    async def factory():
        state["live"] += 1
        state["max_live"] = max(state["max_live"], state["live"])
        await asyncio.sleep(0.05)
        state["live"] -= 1
        return True

    async def run():
        return await delegate_mod.run_bounded([factory, factory, factory])

    assert asyncio.run(run()) == [True, True, True]
    assert state["max_live"] == 1, "при выключенном рое субагенты пошли параллельно"
    limit, reason = delegate_mod.parallel_limit()
    assert limit == 1 and "disabled" in reason


def test_runner_status_explains_low_ram(tmp_path, monkeypatch):
    _ram(monkeypatch, 500)
    monkeypatch.setattr(get_settings(), "delegate_max_parallel", 3)
    runner = TaskRunner(None, str(tmp_path), max_workers=3)

    asyncio.run(runner.start())
    try:
        st = runner.status()
    finally:
        asyncio.run(runner.stop())

    assert st["workers"] == 1 and "RAM" in st["workers_reason"]
    assert st["parallel_limit"] == 1 and st["parallel_limit_reason"]
    assert st["swarm_enabled"] is True


def test_runner_status_reports_swarm_limit(tmp_path, monkeypatch):
    _ram(monkeypatch, 8000)
    monkeypatch.setattr(get_settings(), "delegate_max_parallel", 3)
    runner = TaskRunner(None, str(tmp_path), max_workers=3)

    asyncio.run(runner.start())
    try:
        st = runner.status()
    finally:
        asyncio.run(runner.stop())

    assert st["workers"] == 3
    assert "runner_max_workers=3" in st["workers_reason"]
    assert st["parallel_limit"] == 3 and st["parallel_limit_reason"] == "delegate_max_parallel=3"


def test_runner_status_before_start_is_safe(tmp_path, monkeypatch):
    runner = TaskRunner(None, str(tmp_path), max_workers=2)

    st = runner.status()

    assert st["workers"] == 0 and st["workers_reason"] == "not started"
