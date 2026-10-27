"""Files page backend (/files*): sandbox browser. Covers traversal, root protection, type validation.

FILES-1: DELETE with path "/", "." or "./" and recursive=true used to wipe the whole sandbox root.
FILES-2: non-string `path` in mkdir/delete body gave HTTP 500 instead of 422.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aria.api.auth import token_store
from aria.config import get_settings
from aria.main import app


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    root = tmp_path / "sb"
    root.mkdir()
    monkeypatch.setattr(get_settings(), "agent_sandbox_root", str(root))
    return root


@pytest.fixture
def client(sandbox):
    return TestClient(app, raise_server_exceptions=False)


def _h():
    if not token_store.current_token():
        token_store.issue()
    return {"X-Local-Agent-Token": token_store.current_token()}


@pytest.mark.parametrize("path", ["/", ".", "./", "//", "/./"])
def test_delete_never_removes_the_sandbox_root(client, sandbox, path):
    (sandbox / "keep.txt").write_text("x", encoding="utf-8")
    r = client.request("DELETE", "/files", json={"path": path, "recursive": True}, headers=_h())
    assert r.status_code == 400, r.text
    assert sandbox.exists() and (sandbox / "keep.txt").exists()


@pytest.mark.parametrize("bad", [5, [1], {"a": 1}, True])
def test_mkdir_and_delete_reject_non_string_path_with_422(client, sandbox, bad):
    assert client.post("/files/mkdir", json={"path": bad}, headers=_h()).status_code == 422
    assert client.request("DELETE", "/files", json={"path": bad}, headers=_h()).status_code == 422


@pytest.mark.parametrize("path", ["../x", "a/../../x", "..", "a\\..\\b"])
def test_traversal_is_rejected_everywhere(client, path):
    assert client.get("/files", params={"path": path}, headers=_h()).status_code == 400
    assert client.get("/files/read", params={"path": path}, headers=_h()).status_code == 400
    assert client.post("/files/mkdir", json={"path": path}, headers=_h()).status_code == 400
    assert client.request("DELETE", "/files", json={"path": path}, headers=_h()).status_code == 400


def test_symlink_pointing_outside_cannot_be_read_or_listed(client, sandbox, tmp_path):
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "k.txt").write_text("secret", encoding="utf-8")
    try:
        (sandbox / "link").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert client.get("/files", params={"path": "link"}, headers=_h()).status_code == 400
    assert client.get("/files/read", params={"path": "link/k.txt"}, headers=_h()).status_code == 400


def test_mkdir_upload_list_read_delete_roundtrip(client, sandbox):
    h = _h()
    assert client.post("/files/mkdir", json={"path": "docs/inner"}, headers=h).status_code == 200
    assert client.post("/files/mkdir", json={"path": "docs/inner"}, headers=h).status_code == 409
    up = client.post("/files/upload-stream", data={"path": "docs/привет.txt"},
                     files={"file": ("x", "Пожалуйста".encode("utf-8"))}, headers=h)
    assert up.status_code == 200 and up.json()["path"] == "docs/привет.txt"
    listing = client.get("/files", params={"path": "docs"}, headers=h).json()
    assert {e["name"] for e in listing["entries"]} == {"inner", "привет.txt"}
    rd = client.get("/files/read", params={"path": "docs/привет.txt"}, headers=h).json()
    assert rd["size"] == len("Пожалуйста".encode("utf-8"))
    assert client.request("DELETE", "/files", json={"path": "docs"}, headers=h).status_code == 409
    assert client.request("DELETE", "/files", json={"path": "docs", "recursive": True}, headers=h).status_code == 200
    assert not (sandbox / "docs").exists()
    assert sandbox.exists()


def test_upload_respects_overwrite_flag(client, sandbox):
    h = _h()
    assert client.post("/files/upload-stream", data={"path": "a.txt"}, files={"file": ("x", b"one")}, headers=h).status_code == 200
    r = client.post("/files/upload-stream", data={"path": "a.txt", "overwrite": "false"}, files={"file": ("x", b"two")}, headers=h)
    assert r.status_code == 409
    assert (sandbox / "a.txt").read_bytes() == b"one"


def test_requires_runtime_token(client):
    assert client.get("/files").status_code in (401, 403)
    assert client.request("DELETE", "/files", json={"path": "x"}).status_code in (401, 403)
