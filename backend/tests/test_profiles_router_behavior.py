"""Profiles router (/profiles*): behaviour on the on-disk profile store.

Each test asserts status + response shape, and for mutating routes that the
change is visible on the next GET. Created profiles are removed by the
`profiles` fixture; the active profile is restored after every test.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aria.api.auth import token_store
from aria.main import app


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def _h():
    if not token_store.current_token():
        token_store.issue()
    return {"X-Local-Agent-Token": token_store.current_token()}


@pytest.fixture(autouse=True)
def _restore_active(client):
    before = client.get("/profiles/active", headers=_h()).json()["active"]
    names = [p["name"] for p in client.get("/profiles", headers=_h()).json()["profiles"]]
    target = before if before in names else "default"
    yield
    client.post("/profiles/active", json={"name": target}, headers=_h())


@pytest.fixture
def profiles(client):
    created: list[str] = []

    def create(name: str, **payload):
        r = client.post("/profiles", json={"name": name, **payload}, headers=_h())
        if r.status_code == 200:
            created.append(name)
        return r

    yield create
    for name in created:
        client.delete(f"/profiles/{name}", headers=_h())


def test_list_contains_default(client):
    body = client.get("/profiles", headers=_h()).json()
    names = [p["name"] for p in body["profiles"]]
    assert names[0] == "default"
    default = body["profiles"][0]
    assert default["is_default"] is True
    for key in ("path", "skill_count", "has_env", "description"):
        assert key in default


def test_active_points_to_a_listed_profile(client):
    # profile_state.json is shared on disk, so another test may have changed the
    # active profile: the contract is "active is a real profile and current==active".
    body = client.get("/profiles/active", headers=_h()).json()
    names = [p["name"] for p in client.get("/profiles", headers=_h()).json()["profiles"]]
    assert body["active"] in names
    assert body["current"] == body["active"]


def test_create_profile_is_visible_in_list(client, profiles):
    r = profiles("zzq-profile", description="test profile", provider="deepseek", model="deepseek-flash")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["name"] == "zzq-profile" and body["model_set"] is True
    rows = {p["name"]: p for p in client.get("/profiles", headers=_h()).json()["profiles"]}
    assert rows["zzq-profile"]["description"] == "test profile"
    assert rows["zzq-profile"]["model"] == "deepseek-flash"
    assert rows["zzq-profile"]["provider"] == "deepseek"
    assert rows["zzq-profile"]["is_default"] is False


def test_create_duplicate_409(client, profiles):
    profiles("zzq-dup")
    assert profiles("zzq-dup").status_code == 409


@pytest.mark.parametrize("name", ["", "-bad", "bad name", "bad/name", "x" * 65])
def test_create_rejects_invalid_name(client, profiles, name):
    assert profiles(name).status_code == 400


def test_create_clone_from_default(client, profiles):
    r = profiles("zzq-clone", clone_from="default", model="m1", provider="p1")
    assert r.status_code == 200 and r.json()["ok"] is True
    rows = {p["name"]: p for p in client.get("/profiles", headers=_h()).json()["profiles"]}
    assert rows["zzq-clone"]["model"] == "m1"


def test_set_active_is_visible(client, profiles):
    profiles("zzq-active")
    r = client.post("/profiles/active", json={"name": "zzq-active"}, headers=_h())
    assert r.status_code == 200 and r.json()["active"] == "zzq-active"
    assert client.get("/profiles/active", headers=_h()).json()["active"] == "zzq-active"


def test_set_active_unknown_404(client):
    assert client.post("/profiles/active", json={"name": "nope"}, headers=_h()).status_code == 404


def test_update_description_is_visible(client, profiles):
    profiles("zzq-desc")
    r = client.put("/profiles/zzq-desc/description", json={"description": "new text"}, headers=_h())
    assert r.status_code == 200 and r.json()["description"] == "new text"
    rows = {p["name"]: p for p in client.get("/profiles", headers=_h()).json()["profiles"]}
    assert rows["zzq-desc"]["description"] == "new text"
    assert rows["zzq-desc"]["description_auto"] is False


def test_update_description_unknown_404(client):
    r = client.put("/profiles/nope/description", json={"description": "x"}, headers=_h())
    assert r.status_code == 404


def test_describe_auto_generates_from_metadata(client, profiles):
    profiles("zzq-auto", model="deepseek-flash", provider="deepseek")
    body = client.post("/profiles/zzq-auto/describe-auto", json={}, headers=_h()).json()
    assert body["ok"] is True and body["description_auto"] is True
    assert body["description"].startswith("Profile 'zzq-auto'")
    assert body["description"].endswith(".")
    rows = {p["name"]: p for p in client.get("/profiles", headers=_h()).json()["profiles"]}
    assert rows["zzq-auto"]["description_auto"] is True


def test_describe_auto_keeps_manual_description(client, profiles):
    profiles("zzq-manual", description="manual text")
    body = client.post(
        "/profiles/zzq-manual/describe-auto", json={"overwrite": False}, headers=_h()
    ).json()
    assert body["ok"] is False
    assert body["description"] == "manual text"


def test_set_model_is_visible(client, profiles):
    profiles("zzq-model")
    r = client.put(
        "/profiles/zzq-model/model", json={"provider": "p2", "model": "m2"}, headers=_h()
    )
    assert r.status_code == 200 and r.json()["model"] == "m2"
    rows = {p["name"]: p for p in client.get("/profiles", headers=_h()).json()["profiles"]}
    assert rows["zzq-model"]["provider"] == "p2" and rows["zzq-model"]["model"] == "m2"


def test_rename_profile(client, profiles):
    profiles("zzq-old")
    r = client.patch("/profiles/zzq-old", json={"new_name": "zzq-new"}, headers=_h())
    assert r.status_code == 200 and r.json()["name"] == "zzq-new"
    names = [p["name"] for p in client.get("/profiles", headers=_h()).json()["profiles"]]
    assert "zzq-new" in names and "zzq-old" not in names


def test_rename_default_is_rejected(client):
    assert client.patch("/profiles/default", json={"new_name": "x"}, headers=_h()).status_code == 400


def test_rename_unknown_404(client):
    assert client.patch("/profiles/nope", json={"new_name": "x"}, headers=_h()).status_code == 404


def test_rename_to_existing_409(client, profiles):
    profiles("zzq-a")
    profiles("zzq-b")
    assert client.patch("/profiles/zzq-a", json={"new_name": "zzq-b"}, headers=_h()).status_code == 409


def test_delete_profile(client, profiles):
    profiles("zzq-gone")
    assert client.delete("/profiles/zzq-gone", headers=_h()).status_code == 200
    names = [p["name"] for p in client.get("/profiles", headers=_h()).json()["profiles"]]
    assert "zzq-gone" not in names


def test_delete_default_is_rejected(client):
    assert client.delete("/profiles/default", headers=_h()).status_code == 400


def test_delete_unknown_404(client):
    assert client.delete("/profiles/nope", headers=_h()).status_code == 404


def test_setup_command_mentions_profile(client):
    body = client.get("/profiles/default/setup-command", headers=_h()).json()
    assert "--profile default" in body["command"]
    assert "--port" in body["command"]


def test_soul_roundtrip(client, profiles):
    profiles("zzq-soul")
    assert client.get("/profiles/zzq-soul/soul", headers=_h()).json() == {
        "content": "",
        "exists": False,
    }
    r = client.put("/profiles/zzq-soul/soul", json={"content": "Ты — агент."}, headers=_h())
    assert r.status_code == 200 and r.json()["ok"] is True
    body = client.get("/profiles/zzq-soul/soul", headers=_h()).json()
    assert body == {"content": "Ты — агент.", "exists": True}


def test_soul_unknown_404(client):
    assert client.get("/profiles/nope/soul", headers=_h()).status_code == 404


def test_requires_runtime_token(client):
    assert client.get("/profiles").status_code in (401, 403)
    assert client.post("/profiles", json={"name": "x"}).status_code in (401, 403)
