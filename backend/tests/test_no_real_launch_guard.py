"""Страж: ни один тест не должен открывать реальный Проводник/Finder.

Причина: test_api_fuzz_contract обходит все маршруты с токеном, включая
POST /logs/open-folder, и раньше доходил до os.startfile на Windows.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from aria import paths
from aria.api.auth import token_store
from aria.main import app
from aria.routers import logs as logs_router


def _h():
    if not token_store.current_token():
        token_store.issue()
    return {"X-Local-Agent-Token": token_store.current_token()}


def test_file_manager_launch_is_stubbed_by_conftest():
    fn = logs_router._open_in_file_manager
    assert getattr(fn, "__name__", "") == "_no_real_launch", (
        "autouse-фикстура не подменила запуск файлового менеджера: "
        "тест откроет окно Проводника на Windows"
    )


def test_open_folder_endpoint_uses_the_stub(monkeypatch, tmp_path):
    fn = logs_router._open_in_file_manager
    calls = getattr(fn, "calls", None)
    assert calls is not None, "заглушка запуска не записывает вызовы"

    target = tmp_path / "logs"
    monkeypatch.setattr(paths, "log_dir", lambda: target)
    calls.clear()
    r = TestClient(app, raise_server_exceptions=False).post("/logs/open-folder", headers=_h())
    assert r.status_code == 200, r.text
    assert calls == [str(target)]
