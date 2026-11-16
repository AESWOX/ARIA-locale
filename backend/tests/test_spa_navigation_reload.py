"""UI-12: обновление окна на странице SPA, чей путь совпадает с маршрутом API, открывает приложение, а не JSON 401."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import aria.main as main
from aria.api.auth import token_store
from aria.main import app

HTML = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
SPA_PATHS = ["/sessions", "/files", "/logs", "/config", "/skills", "/profiles", "/env", "/webhooks", "/pairing", "/plugins", "/chat", "/system"]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "_render_index", lambda: "<html>SPA-SHELL</html>")
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("path", SPA_PATHS)
def test_browser_navigation_gets_the_app(client, path):
    r = client.get(path, headers=HTML)
    assert r.status_code == 200 and "SPA-SHELL" in r.text, (path, r.status_code, r.text[:80])


@pytest.mark.parametrize("path", ["/sessions", "/logs", "/config"])
def test_fetch_without_token_still_gets_401(client, path):
    for accept in ({}, {"Accept": "application/json"}, {"Accept": "*/*"}):
        r = client.get(path, headers=accept)
        assert r.status_code == 401, (path, accept, r.status_code)


def test_request_with_token_reaches_the_api_even_with_html_accept(client):
    token, _ = token_store.issue()
    r = client.get("/sessions", headers={**HTML, "X-Local-Agent-Token": token})
    assert r.status_code == 200 and "SPA-SHELL" not in r.text


def test_api_prefix_and_openapi_are_not_swallowed(client):
    assert client.get("/api/sessions", headers=HTML).status_code == 401
    assert "SPA-SHELL" not in client.get("/openapi.json", headers=HTML).text
