"""Поведение роутера system: форма ответов, ошибки валидации, изменения видны при следующем GET.

Не вызываются: /system/shutdown, /system/clean-test-data, /ws, /system/open-external (см. test_open_external.py).
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


def test_health_shape(client):
    r = client.get("/health")
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), dict)


def test_status_alias_matches_health_keys(client):
    a = client.get("/status")
    b = client.get("/health")
    assert a.status_code == 200 and b.status_code == 200
    assert set(a.json()) == set(b.json())


def test_model_info_shape(client):
    r = client.get("/model/info", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["models"], list)
    assert isinstance(body["tier_hints"], (dict, list))


def test_model_info_lists_inserted_model(client):
    from aria.db import models as m
    from aria.db.base import session_scope

    with session_scope() as db:
        db.add(m.ProviderModel(id="sys-test-m", provider_id="sys-test-p", model_id="mm", context_window=4096))
    models = client.get("/model/info", headers=_h()).json()["models"]
    row = next(x for x in models if x["id"] == "sys-test-m")
    assert row["provider_id"] == "sys-test-p"
    assert row["context_window"] == 4096
    assert set(row) == {"id", "provider_id", "model_id", "context_window", "is_free_tier"}


def test_self_test_shape(client):
    r = client.get("/system/self-test", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent"] == "ARIA"
    assert body["status"] in ("ok", "degraded")
    assert isinstance(body["issues"], list)
    assert isinstance(body["checks"], dict)
    assert isinstance(body["ts"], str)


def test_timings_shape_default_and_window(client):
    body = client.get("/system/timings", headers=_h()).json()
    assert body["window_sec"] is None and isinstance(body["stages"], dict)
    body = client.get("/system/timings?window_sec=30", headers=_h()).json()
    assert body["window_sec"] == 30


@pytest.mark.parametrize("value", ["0", "-5", "abc"])
def test_timings_rejects_bad_window(client, value):
    assert client.get(f"/system/timings?window_sec={value}", headers=_h()).status_code == 422


def test_stats_shape(client):
    r = client.get("/system/stats", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("os", "arch", "platform", "python_version", "aria_version"):
        assert isinstance(body[key], str) and body[key]


def test_feedback_rating_out_of_range_is_422(client):
    r = client.post("/system/feedback", json={"task_id": "t-range", "rating": 9}, headers=_h())
    assert r.status_code == 422
    assert "rating" in r.json()["detail"]


def test_feedback_missing_task_id_is_422(client):
    assert client.post("/system/feedback", json={}, headers=_h()).status_code == 422


def test_feedback_saved_without_skill(client):
    r = client.post(
        "/system/feedback", json={"task_id": f"t-{uuid.uuid4()}", "rating": 3, "comment": "meh"}, headers=_h()
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "feedback_saved": True, "skill_created": False}


@pytest.mark.parametrize(
    "method,path",
    [("GET", "/model/info"), ("GET", "/system/self-test"), ("GET", "/system/timings"), ("GET", "/system/stats")],
)
def test_protected_routes_reject_missing_token(client, method, path):
    assert client.request(method, path).status_code in (401, 403)
