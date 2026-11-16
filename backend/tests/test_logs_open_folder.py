"""POST /logs/open-folder: открывает только log_dir(), без реального файлового менеджера."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aria import paths as _paths
from aria.api.auth import token_store
from aria.main import app
from aria.routers import logs as logs_router


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def logs_dir(tmp_path, monkeypatch):
    target = tmp_path / "logs-root" / "logs"  # намеренно не создан: маршрут должен создать каталог
    monkeypatch.setattr(_paths, "log_dir", lambda: target)
    return target


def _h():
    if not token_store.current_token():
        token_store.issue()
    return {"X-Local-Agent-Token": token_store.current_token()}


def test_open_folder_calls_file_manager_once_with_log_dir(client, logs_dir, monkeypatch):
    calls: list = []
    monkeypatch.setattr(logs_router, "_open_in_file_manager", lambda p: calls.append(p))
    r = client.post("/logs/open-folder", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"ok": True, "path": str(logs_dir)}
    assert calls == [logs_dir]
    assert logs_dir.is_dir()


def test_open_folder_failure_is_500_with_detail(client, logs_dir, monkeypatch):
    def boom(_path):
        raise OSError("no file manager")

    monkeypatch.setattr(logs_router, "_open_in_file_manager", boom)
    r = client.post("/logs/open-folder", headers=_h())
    assert r.status_code == 500
    assert "no file manager" in r.json()["detail"]


def test_open_folder_ignores_path_from_body(client, logs_dir, tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(logs_router, "_open_in_file_manager", lambda p: calls.append(p))
    other = tmp_path / "elsewhere"
    r = client.post("/logs/open-folder", json={"path": str(other)}, headers=_h())
    assert r.status_code == 200, r.text
    assert calls == [logs_dir]
    assert not other.exists()


def test_open_folder_requires_token(client, logs_dir, monkeypatch):
    calls: list = []
    monkeypatch.setattr(logs_router, "_open_in_file_manager", lambda p: calls.append(p))
    r = client.post("/logs/open-folder")
    assert r.status_code in (401, 403)
    assert calls == []
