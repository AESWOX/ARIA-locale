"""Sessions router (/sessions*): behaviour, not just route existence.

Each test asserts a status code, the response shape, and — for mutating routes —
that the change is visible on the next GET. No network, no real LLM.
Destructive routes (/sessions/prune, DELETE /sessions/empty) are not exercised.
"""
from __future__ import annotations

import uuid

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


def _create(client, title=None):
    payload = {} if title is None else {"title": title}
    r = client.post("/sessions", json=payload, headers=_h())
    assert r.status_code == 200, r.text
    return r.json()


def test_list_sessions_empty_shape(client):
    body = client.get("/sessions", headers=_h()).json()
    assert body["sessions"] == []
    assert body["total"] == 0
    assert body["limit"] == 20 and body["offset"] == 0


def test_create_session_returns_ids_and_default_title(client):
    body = _create(client)
    assert body["session_id"] and body["task_id"]
    snap = client.get(f"/sessions/{body['session_id']}", headers=_h()).json()
    assert snap["session"]["title"] == "Untitled session"
    assert snap["current_task"]["id"] == body["task_id"]


def test_create_session_keeps_title_and_lists_it(client):
    sid = _create(client, "Разбор сборки")["session_id"]
    listing = client.get("/sessions", headers=_h()).json()
    assert listing["total"] == 1
    assert listing["sessions"][0]["id"] == sid
    assert listing["sessions"][0]["title"] == "Разбор сборки"
    assert listing["sessions"][0]["message_count"] == 0


def test_session_snapshot_contains_session_and_messages(client):
    sid = _create(client, "snap")["session_id"]
    snap = client.get(f"/sessions/{sid}", headers=_h()).json()
    assert snap["session"]["id"] == sid
    assert snap["messages"] == []
    for key in ("sessions", "current_task", "tool_calls", "attention_items", "providers"):
        assert key in snap


def test_snapshot_unknown_session_404(client):
    r = client.get("/sessions/00000000-0000-0000-0000-000000000000", headers=_h())
    assert r.status_code == 404


def test_messages_unknown_session_404(client):
    r = client.get("/sessions/00000000-0000-0000-0000-000000000000/messages", headers=_h())
    assert r.status_code == 404


def test_run_unknown_session_404(client):
    r = client.get("/sessions/00000000-0000-0000-0000-000000000000/run", headers=_h())
    assert r.status_code == 404


def test_run_returns_draft_task_for_new_session(client):
    body = _create(client, "run")
    r = client.get(f"/sessions/{body['session_id']}/run", headers=_h())
    assert r.status_code == 200
    run = r.json()
    assert run["task_id"] == body["task_id"]
    assert run["status"] == "draft"
    assert run["terminal"] is False
    assert run["attention"] == [] and run["questions"] == []


def test_rename_session_visible_after_get(client):
    sid = _create(client, "before")["session_id"]
    r = client.patch(f"/sessions/{sid}", json={"title": "after"}, headers=_h())
    assert r.status_code == 200 and r.json()["title"] == "after"
    snap = client.get(f"/sessions/{sid}", headers=_h()).json()
    assert snap["session"]["title"] == "after"


def test_rename_without_title_400(client):
    sid = _create(client, "before")["session_id"]
    assert client.patch(f"/sessions/{sid}", json={}, headers=_h()).status_code == 400
    assert client.patch(f"/sessions/{sid}", json={"title": "   "}, headers=_h()).status_code == 400


def test_rename_unknown_404(client):
    r = client.patch(
        "/sessions/00000000-0000-0000-0000-000000000000", json={"title": "x"}, headers=_h()
    )
    assert r.status_code == 404


def test_delete_unknown_404(client):
    r = client.delete("/sessions/00000000-0000-0000-0000-000000000000", headers=_h())
    assert r.status_code == 404


def test_delete_session_removes_from_list(client):
    sid = _create(client, "to delete")["session_id"]
    assert client.delete(f"/sessions/{sid}", headers=_h()).status_code == 200
    assert client.get("/sessions", headers=_h()).json()["total"] == 0
    assert client.get(f"/sessions/{sid}", headers=_h()).status_code == 404


def test_post_message_requires_content(client):
    sid = _create(client)["session_id"]
    r = client.post(f"/sessions/{sid}/messages", json={"content": "  "}, headers=_h())
    assert r.status_code == 400


def test_post_message_rejects_unknown_mode(client):
    sid = _create(client)["session_id"]
    r = client.post(
        f"/sessions/{sid}/messages", json={"content": "hi", "mode": "yolo"}, headers=_h()
    )
    assert r.status_code == 400


def test_post_message_unknown_session_404(client):
    r = client.post(
        "/sessions/00000000-0000-0000-0000-000000000000/messages",
        json={"content": "hi"},
        headers=_h(),
    )
    assert r.status_code == 404


def test_post_assistant_message_visible_in_messages(client):
    sid = _create(client)["session_id"]
    r = client.post(
        f"/sessions/{sid}/messages",
        json={"content": "готово zzq-marker", "role": "assistant"},
        headers=_h(),
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    msgs = client.get(f"/sessions/{sid}/messages", headers=_h()).json()
    assert msgs["session_id"] == sid
    assert [m["content"] for m in msgs["messages"]] == ["готово zzq-marker"]
    assert msgs["messages"][0]["role"] == "assistant"


def test_search_finds_message_and_returns_snippet(client):
    sid = _create(client)["session_id"]
    client.post(
        f"/sessions/{sid}/messages",
        json={"content": "уникальная строка zzq-search", "role": "assistant"},
        headers=_h(),
    )
    results = client.get("/sessions/search", params={"q": "zzq-search"}, headers=_h()).json()["results"]
    assert len(results) == 1
    assert results[0]["session_id"] == sid
    assert "zzq-search" in results[0]["snippet"]


def test_search_blank_returns_empty(client):
    _create(client)
    assert client.get("/sessions/search", params={"q": "  "}, headers=_h()).json() == {"results": []}


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 500}, {"offset": -1}])
def test_list_rejects_invalid_paging(client, params):
    assert client.get("/sessions", params=params, headers=_h()).status_code == 422


def test_stats_reflect_created_sessions(client):
    _create(client)
    _create(client)
    stats = client.get("/sessions/stats", headers=_h()).json()
    assert stats["total"] == 2
    assert stats["active_store"] == 2
    assert stats["archived"] == 0
    assert stats["messages"] == 0


def test_empty_sessions_count_and_list(client):
    _create(client, "empty one")
    count = client.get("/sessions/empty/count", headers=_h()).json()
    assert count == {"count": 1}
    listed = client.get("/sessions/empty", headers=_h()).json()
    assert listed["count"] == 1
    assert [s["title"] for s in listed["sessions"]] == ["empty one"]


def test_export_markdown_headers(client):
    sid = _create(client, "export me")["session_id"]
    r = client.get(f"/sessions/{sid}/export.md", headers=_h())
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "attachment; filename=" in r.headers["content-disposition"]
    assert "#" in r.text


def test_export_filename_cyrillic_is_not_latin1():
    """Причина 500: имя файла содержит кириллицу, а HTTP-заголовки Starlette кодирует в latin-1."""
    from aria.http_utils import _safe_export_filename

    name = _safe_export_filename("экспорт", uuid.uuid4())
    assert "экспорт" in name
    with pytest.raises(UnicodeEncodeError):
        name.encode("latin-1")


def test_export_markdown_with_cyrillic_title(client):
    from urllib.parse import unquote

    sid = _create(client, "экспорт")["session_id"]
    r = client.get(f"/sessions/{sid}/export.md", headers=_h())
    assert r.status_code == 200, r.text
    disposition = r.headers["content-disposition"]
    disposition.encode("latin-1")
    assert "filename*=UTF-8''" in disposition
    assert "экспорт" in unquote(disposition.split("filename*=UTF-8''", 1)[1])


def test_export_unknown_404(client):
    r = client.get("/sessions/00000000-0000-0000-0000-000000000000/export.md", headers=_h())
    assert r.status_code == 404


def test_bulk_delete_ignores_garbage_ids(client):
    sid = _create(client, "keep me")["session_id"]
    r = client.post("/sessions/bulk-delete", json={"ids": ["nope", None, 5]}, headers=_h())
    assert r.status_code == 200 and r.json()["deleted"] == 0
    assert client.get("/sessions", headers=_h()).json()["total"] == 1
    r = client.post("/sessions/bulk-delete", json={"ids": [sid]}, headers=_h())
    assert r.json()["deleted"] == 1
    assert client.get("/sessions", headers=_h()).json()["total"] == 0
