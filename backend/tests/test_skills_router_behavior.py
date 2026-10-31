"""Skills router (/skills*, /skills/hub*): behaviour on the on-disk skills root.

Each test asserts status + response shape, and for mutating routes that the
change is visible on the next GET. Skills written to disk are removed by the
`make_skill` fixture so a random test order stays hermetic.
"""
from __future__ import annotations

import shutil

import pytest
from fastapi.testclient import TestClient

from aria import paths
from aria.api.auth import token_store
from aria.main import app


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def _h():
    if not token_store.current_token():
        token_store.issue()
    return {"X-Local-Agent-Token": token_store.current_token()}


@pytest.fixture
def make_skill(client):
    created: list[str] = []

    def make(name: str, content: str = "# Demo\n\nDemo skill.", **extra):
        r = client.post("/skills", json={"name": name, "content": content, **extra}, headers=_h())
        assert r.status_code == 200, r.text
        created.append(name)
        return r

    yield make
    root = paths.skills_dir()
    for name in created:
        shutil.rmtree(root / name, ignore_errors=True)


def test_list_returns_seeded_skills(client):
    rows = client.get("/skills", headers=_h()).json()
    names = {row["name"] for row in rows}
    assert {"web-research", "code-review", "obsidian-notes"} <= names
    assert all(isinstance(row["enabled"], bool) for row in rows)


def test_toggle_is_visible_after_list(client):
    r = client.put("/skills/toggle", json={"name": "web-research", "enabled": False}, headers=_h())
    assert r.status_code == 200 and r.json()["enabled"] is False
    rows = {row["name"]: row for row in client.get("/skills", headers=_h()).json()}
    assert rows["web-research"]["enabled"] is False
    client.put("/skills/toggle", json={"name": "web-research", "enabled": True}, headers=_h())
    rows = {row["name"]: row for row in client.get("/skills", headers=_h()).json()}
    assert rows["web-research"]["enabled"] is True


def test_toggle_unknown_skill_404(client):
    r = client.put("/skills/toggle", json={"name": "no-such-skill", "enabled": True}, headers=_h())
    assert r.status_code == 404


def test_toggle_requires_name(client):
    assert client.put("/skills/toggle", json={"enabled": True}, headers=_h()).status_code == 400


def test_content_of_seeded_skill(client):
    body = client.get("/skills/content", params={"name": "web-research"}, headers=_h()).json()
    assert body["name"] == "web-research"
    assert "Web research" in body["content"]
    assert body["path"].endswith("SKILL.md")


def test_content_of_unknown_skill_404(client):
    assert client.get("/skills/content", params={"name": "nope"}, headers=_h()).status_code == 404


@pytest.mark.parametrize("name", ["../escape", "..", "a/b"])
def test_content_rejects_traversal(client, name):
    assert client.get("/skills/content", params={"name": name}, headers=_h()).status_code == 400


def test_create_skill_is_visible_in_list(client, make_skill):
    make_skill("zzq-demo", "# Demo\n\ndescription: created by test")
    rows = {row["name"]: row for row in client.get("/skills", headers=_h()).json()}
    assert "zzq-demo" in rows
    assert rows["zzq-demo"]["enabled"] is True
    assert rows["zzq-demo"]["description"] == "created by test"


def test_create_requires_name(client):
    assert client.post("/skills", json={"content": "x"}, headers=_h()).status_code == 400


@pytest.mark.parametrize("name", ["../x", "..", "."])
def test_create_rejects_traversal(client, name):
    assert client.post("/skills", json={"name": name, "content": "x"}, headers=_h()).status_code == 400


def test_update_content_roundtrip(client, make_skill):
    make_skill("zzq-update", "old")
    r = client.put("/skills/content", json={"name": "zzq-update", "content": "new body"}, headers=_h())
    assert r.status_code == 200 and r.json()["success"] is True
    body = client.get("/skills/content", params={"name": "zzq-update"}, headers=_h()).json()
    assert body["content"] == "new body"


def test_update_requires_name_and_content(client):
    assert client.put("/skills/content", json={"name": "web-research"}, headers=_h()).status_code == 400
    assert client.put("/skills/content", json={"content": "x"}, headers=_h()).status_code == 400


def test_update_unknown_skill_404(client):
    r = client.put("/skills/content", json={"name": "no-such-skill", "content": "x"}, headers=_h())
    assert r.status_code == 404


def test_hub_sources_shape(client):
    body = client.get("/skills/hub/sources", headers=_h()).json()
    assert {s["id"] for s in body["sources"]} == {"local", "aria-index", "github"}
    assert body["index_available"] is True
    assert "local:web-research" in body["installed"]
    assert any(item["identifier"] == "local:web-research" for item in body["featured"])


def test_hub_search_filters_and_reports_installed(client):
    body = client.get("/skills/hub/search", params={"q": "web"}, headers=_h()).json()
    names = [item["name"] for item in body["results"]]
    assert names == ["web-research"]
    assert body["source_counts"] == {"local": 1}
    assert body["timed_out"] == []
    assert "local:web-research" in body["installed"]


def test_hub_search_zero_limit_returns_nothing(client):
    body = client.get("/skills/hub/search", params={"q": "web", "limit": 0}, headers=_h()).json()
    assert body["results"] == []


def test_hub_preview_local_skill(client):
    body = client.get(
        "/skills/hub/preview", params={"identifier": "local:web-research"}, headers=_h()
    ).json()
    assert body["name"] == "web-research"
    assert "Web research" in body["skill_md"]
    assert "SKILL.md" in body["files"]


def test_hub_preview_rejects_unknown_source(client):
    r = client.get("/skills/hub/preview", params={"identifier": "bogus:x"}, headers=_h())
    assert r.status_code == 400


def test_hub_preview_missing_skill_404(client):
    r = client.get("/skills/hub/preview", params={"identifier": "local:nope"}, headers=_h())
    assert r.status_code == 404


def test_hub_install_reports_missing_and_existing(client):
    r = client.post("/skills/hub/install", json={"identifier": "local:web-research"}, headers=_h())
    assert r.status_code == 200 and r.json()["ok"] is True
    r = client.post("/skills/hub/install", json={"identifier": "local:nope"}, headers=_h())
    assert r.status_code == 200 and r.json()["ok"] is False
    assert "not found" in r.json()["error"]


def test_hub_uninstall_hides_skill(client):
    r = client.post("/skills/hub/uninstall", json={"name": "web-research"}, headers=_h())
    assert r.status_code == 200 and r.json()["ok"] is True
    rows = {row["name"]: row for row in client.get("/skills", headers=_h()).json()}
    assert rows["web-research"]["enabled"] is False
    r = client.post("/skills/hub/uninstall", json={"name": "nope"}, headers=_h())
    assert r.json()["ok"] is False


def test_hub_update_reports_sync(client):
    body = client.post("/skills/hub/update", json={}, headers=_h()).json()
    assert body["ok"] is True and body["name"] == "skills-hub-update"
    assert "synced" in body["message"]


def test_hub_scan_clean_skill_is_safe(client):
    body = client.get(
        "/skills/hub/scan", params={"identifier": "local:web-research"}, headers=_h()
    ).json()
    assert body["verdict"] == "safe"
    assert body["policy"] == "allow"
    assert body["findings"] == []
    assert body["severity_counts"] == {"high": 0, "medium": 0}


def test_hub_scan_flags_dangerous_content(client, make_skill):
    make_skill("zzq-evil", "# Evil\n\nos.system('echo hi')\n")
    body = client.get(
        "/skills/hub/scan", params={"identifier": "local:zzq-evil"}, headers=_h()
    ).json()
    assert body["verdict"] == "dangerous"
    assert body["policy"] == "block"
    assert body["severity_counts"]["high"] == 1
    assert body["findings"][0]["file"] == "SKILL.md"
