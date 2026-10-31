"""GET /logs/diagnostics: zip без секретов."""
from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from aria import paths as _paths
from aria.api.auth import token_store
from aria.main import app

FAKE_SECRETS = [
    "sk-testsecret1234567890abcdef",
    "AIzaTestSecret1234567890abcdefghij",
    "Bearer abcdef1234567890",
    "abcdef1234567890",
]


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def logs_dir(tmp_path, monkeypatch):
    d = tmp_path / "logs"
    d.mkdir()
    monkeypatch.setattr(_paths, "log_dir", lambda: d)
    return d


def _h():
    if not token_store.current_token():
        token_store.issue()
    return {"X-Local-Agent-Token": token_store.current_token()}


def _zip(resp) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(resp.content))


def test_diagnostics_is_valid_zip_with_core_files(client, logs_dir):
    r = client.get("/logs/diagnostics", headers=_h())
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/zip")
    assert 'filename="aria-diagnostics-' in r.headers["content-disposition"]
    names = set(_zip(r).namelist())
    assert {"version.txt", "system.json", "providers.json"} <= names


def test_diagnostics_contains_no_secrets(client, logs_dir, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-envsecret9999999999999999")
    (logs_dir / "backend.log").write_text(
        "2026-10-28 10:00:00 INFO app: key sk-testsecret1234567890abcdef used\n"
        "2026-10-28 10:00:01 INFO app: gemini AIzaTestSecret1234567890abcdefghij ok\n"
        "2026-10-28 10:00:02 INFO app: header Bearer abcdef1234567890 sent\n",
        encoding="utf-8",
    )
    r = client.get("/logs/diagnostics", headers=_h())
    assert r.status_code == 200
    z = _zip(r)
    assert "logs/backend.log" in z.namelist()
    for name in z.namelist():
        blob = z.read(name).decode("utf-8", errors="replace")
        for secret in FAKE_SECRETS + ["sk-envsecret9999999999999999"]:
            assert secret not in blob, f"{secret!r} leaked into {name}"


def test_providers_json_has_flags_only(client, logs_dir, monkeypatch):
    import json

    monkeypatch.setenv("OPENAI_API_KEY", "sk-envsecret9999999999999999")
    r = client.get("/logs/diagnostics", headers=_h())
    data = json.loads(_zip(r).read("providers.json"))
    assert data["OPENAI_API_KEY"] is True
    assert all(isinstance(v, bool) for v in data.values())


def test_large_log_is_truncated(client, logs_dir):
    line = "2026-10-28 10:00:00 INFO app: padding padding padding padding padding\n"
    (logs_dir / "backend.log").write_text(line * (3 * 1024 * 1024 // len(line)), encoding="utf-8")
    r = client.get("/logs/diagnostics", headers=_h())
    size = len(_zip(r).read("logs/backend.log"))
    assert 0 < size <= 2 * 1024 * 1024 + 1024


def test_diagnostics_without_token_is_rejected(client, logs_dir):
    r = client.get("/logs/diagnostics")
    assert r.status_code in (401, 403)
