"""19.1: POST /chat/sessions/{id}/send с {stream:true} отвечает NDJSON (delta -> done).

Провайдер фейкуется на уровне ``stream()``: проверяем сам протокол, сохранение
ассистента один раз и fallback на не-стрим при сбое потока.
"""
from __future__ import annotations

import json
import uuid

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.db import models as m
from aria.db.base import session_scope
from aria.db.models import Session as SessionRow
from aria.llm.providers.base import LlmProvider, LlmResponse, StreamChunk
from aria.llm.router import ProviderRouter
from aria.routers.chat import router as chat_router


class StreamingFake(LlmProvider):
    def __init__(self, provider_id="fake-gemini", chunks=("Hel", "lo", "!")):
        self.provider_id = provider_id
        self.provider_class = "free_tier_reasoning"
        self.chunks = list(chunks)
        self.stream_error: Exception | None = None

    async def check_connectivity(self, timeout_sec):
        return True

    async def chat(self, messages, tools, timeout_sec):
        return LlmResponse(text="".join(self.chunks))

    async def stream(self, messages, tools, timeout_sec):
        if self.stream_error is not None:
            raise self.stream_error
        for chunk in self.chunks:
            yield StreamChunk(text=chunk, final=False)
        yield StreamChunk(text="", final=True, usage={"total_tokens": 3})


def _client(provider):
    llm = ProviderRouter()
    if provider is not None:
        llm.register(provider)
    app = FastAPI()
    app.include_router(chat_router)
    app.state.router = llm
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    return TestClient(app)


def _new_session(c):
    return c.post("/chat/sessions", json={}).json()["session_id"]


def _cleanup(*ids):
    with session_scope() as db:
        for i in ids:
            db.query(m.Message).filter(m.Message.session_id == uuid.UUID(i)).delete()
            db.query(SessionRow).filter(SessionRow.id == uuid.UUID(i)).delete()


def _lines(text: str) -> list[dict]:
    return [json.loads(line) for line in text.strip().splitlines() if line.strip()]


def test_stream_returns_deltas_then_done_and_persists_once():
    fake = StreamingFake(chunks=("Hel", "lo", "!"))
    c = _client(fake)
    sid = _new_session(c)
    try:
        r = c.post(f"/chat/sessions/{sid}/send", json={"content": "hi", "stream": True})
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("application/x-ndjson")
        lines = _lines(r.text)
        deltas = [l for l in lines if l["type"] == "delta"]
        assert deltas == [
            {"type": "delta", "text": "Hel"},
            {"type": "delta", "text": "lo"},
            {"type": "delta", "text": "!"},
        ]
        done = [l for l in lines if l["type"] == "done"]
        assert len(done) == 1
        assert done[0]["result"]["assistant"]["content"] == "Hello!"
        assert done[0]["result"]["provider_id"] == "fake-gemini"
        msgs = c.get(f"/chat/sessions/{sid}/messages").json()
        assert [(m["role"], m["content"]) for m in msgs] == [("user", "hi"), ("assistant", "Hello!")]
    finally:
        _cleanup(sid)


def test_stream_falls_back_to_nonstream_when_provider_stream_fails():
    fake = StreamingFake(chunks=("Hel", "lo", "!"))
    fake.stream_error = httpx.ReadTimeout("slow")
    c = _client(fake)
    sid = _new_session(c)
    try:
        r = c.post(f"/chat/sessions/{sid}/send", json={"content": "hi", "stream": True})
        assert r.status_code == 200, r.text
        lines = _lines(r.text)
        assert all(l["type"] != "delta" for l in lines)
        done = [l for l in lines if l["type"] == "done"]
        assert len(done) == 1
        assert done[0]["result"]["assistant"]["content"] == "Hello!"
    finally:
        _cleanup(sid)


def test_stream_when_no_real_provider_is_still_a_clear_hint():
    c = _client(None)
    sid = _new_session(c)
    try:
        r = c.post(f"/chat/sessions/{sid}/send", json={"content": "hi", "stream": True})
        assert r.status_code == 503
        assert "Open Keys" in r.json()["detail"]
    finally:
        _cleanup(sid)


def test_send_without_stream_flag_stays_plain_json():
    fake = StreamingFake(chunks=("Hel", "lo", "!"))
    c = _client(fake)
    sid = _new_session(c)
    try:
        r = c.post(f"/chat/sessions/{sid}/send", json={"content": "hi"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        assert r.json()["assistant"]["content"] == "Hello!"
    finally:
        _cleanup(sid)
