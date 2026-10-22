"""BUG-1: a key saved in the first-run wizard / Keys page must reach the TaskRunner.

The wizard (POST /setup/apply) and the Keys page (PUT /env) rebuild the provider router and
store it in ``app.state.router``. TaskRunner keeps its own reference, so Agent/Plan kept running
on the empty router built at startup: chat worked, tasks failed with
``no available provider for class=premium_reasoning``.
"""
from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.config import get_settings
from aria.core.taskrunner import TaskRunner
from aria.llm.router import ProviderRouter
from aria.routers import env as env_mod
from aria.routers import setup as setup_mod

KEY = "sk-test-deepseek-key-1234567890"
_ENV_NAMES = ("DEEPSEEK_API_KEY", "DEEPSEEK_CHAT_MODEL")


@pytest.fixture
def app_with_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(env_mod, "_ENV_FILE", tmp_path / ".env")
    for name in _ENV_NAMES:
        os.environ.pop(name, None)
    get_settings.cache_clear()

    app = FastAPI()
    app.include_router(env_mod.router)
    app.include_router(setup_mod.router)
    app.dependency_overrides[require_runtime_token] = lambda: "test"

    empty = ProviderRouter()  # what main.py builds on a machine without keys
    app.state.router = empty
    app.state.task_runner = TaskRunner(empty, str(tmp_path / "sandbox"), max_workers=1)
    assert app.state.task_runner._router.providers_by_class == {}
    yield app
    for name in _ENV_NAMES:
        os.environ.pop(name, None)
    get_settings.cache_clear()


def _classes(router) -> set[str]:
    return {cls for cls, providers in router.providers_by_class.items() if providers}


def test_wizard_apply_updates_task_runner_router(app_with_runner):
    app = app_with_runner
    r = TestClient(app).post("/setup/apply", json={"provider": "deepseek", "key": KEY, "model": "deepseek-chat"})
    assert r.json()["ok"] is True

    runner_router = app.state.task_runner._router
    assert runner_router is app.state.router
    assert "premium_reasoning" in _classes(runner_router)
    assert "standard_reasoning" in _classes(runner_router)


def test_keys_page_put_env_updates_task_runner_router(app_with_runner):
    app = app_with_runner
    r = TestClient(app).put("/env", json={"key": "DEEPSEEK_API_KEY", "value": KEY})
    assert r.json() == {"ok": True}

    assert app.state.task_runner._router is app.state.router
    assert "premium_reasoning" in _classes(app.state.task_runner._router)


def test_refresh_without_task_runner_does_not_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(env_mod, "_ENV_FILE", tmp_path / ".env")
    app = FastAPI()
    app.include_router(env_mod.router)
    app.dependency_overrides[require_runtime_token] = lambda: "test"
    app.state.router = ProviderRouter()
    try:
        assert TestClient(app).put("/env", json={"key": "ARIA_TEST_NOOP", "value": "1"}).json() == {"ok": True}
    finally:
        os.environ.pop("ARIA_TEST_NOOP", None)
        get_settings.cache_clear()


def test_set_router_replaces_reference():
    old, new = ProviderRouter(), ProviderRouter()
    runner = TaskRunner(old, "/tmp/aria_set_router_sb", max_workers=1)
    runner.set_router(new)
    assert runner._router is new
