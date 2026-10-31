"""/vault/discover: вне цикла событий, подсчёт ограничен, результат кэшируется."""
from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient

from aria.api.auth import token_store
from aria.main import app
from aria.routers import vault as vault_router
from aria.storage import vault_index


@pytest.fixture(autouse=True)
def _fresh_cache():
    vault_index.reset_discover_cache()
    yield
    vault_index.reset_discover_cache()


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def _h():
    if not token_store.current_token():
        token_store.issue()
    return {"X-Local-Agent-Token": token_store.current_token()}


def _make_vault(root, name: str, notes: int):
    v = root / name
    (v / ".obsidian").mkdir(parents=True)
    for i in range(notes):
        (v / f"n{i}.md").write_text("x", encoding="utf-8")
    return v


@pytest.fixture
def scan_root(tmp_path, monkeypatch):
    monkeypatch.setenv("ARIA_VAULT_SCAN_ROOTS", str(tmp_path))
    monkeypatch.delenv("ARIA_OBSIDIAN_CONFIG", raising=False)
    return tmp_path


def _vault(result: dict, path) -> dict:
    return next(v for v in result["vaults"] if os.path.normcase(v["path"]) == os.path.normcase(str(path)))


def test_small_vault_count_is_exact_and_not_capped(scan_root):
    v = _make_vault(scan_root, "small", 7)
    entry = _vault(vault_index.discover(), v)
    assert entry["note_count"] == 7
    assert entry["note_count_capped"] is False


def test_huge_vault_count_is_capped(scan_root):
    v = _make_vault(scan_root, "huge", vault_index._DISCOVER_NOTE_CAP + 300)
    entry = _vault(vault_index.discover(), v)
    assert entry["note_count_capped"] is True
    assert 0 < entry["note_count"] <= vault_index._DISCOVER_NOTE_CAP


def test_call_stays_within_time_budget(scan_root):
    for i in range(40):
        _make_vault(scan_root, f"v{i}", 200)
    budget = 0.05
    t0 = time.monotonic()
    vault_index.discover(time_budget_sec=budget)
    assert time.monotonic() - t0 < budget + 1.0


def test_second_call_is_served_from_cache_until_reset(scan_root, monkeypatch):
    calls = {"n": 0}
    real = vault_index._scan_roots

    def counting():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(vault_index, "_scan_roots", counting)
    _make_vault(scan_root, "cached", 3)
    first = vault_index.discover()
    second = vault_index.discover()
    assert calls["n"] == 1
    assert second is first
    vault_index.reset_discover_cache()
    vault_index.discover()
    assert calls["n"] == 2


def test_route_runs_discover_in_a_worker_thread(client, scan_root, monkeypatch):
    used = {"to_thread": False}
    real = vault_router.asyncio.to_thread

    async def spy(func, /, *args, **kwargs):
        used["to_thread"] = True
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(vault_router.asyncio, "to_thread", spy)
    _make_vault(scan_root, "viaroute", 2)
    r = client.get("/vault/discover", headers=_h())
    assert r.status_code == 200, r.text
    assert used["to_thread"] is True


def test_route_response_shape_has_capped_flag(client, scan_root):
    v = _make_vault(scan_root, "shape", 4)
    body = client.get("/vault/discover", headers=_h()).json()
    assert set(body) == {"current", "vaults"}
    entry = _vault(body, v)
    assert {"path", "name", "sources", "is_obsidian_vault", "note_count", "note_count_capped",
            "last_modified", "open_in_obsidian", "current"} <= set(entry)
