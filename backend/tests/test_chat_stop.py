"""19.2: POST /chat/sessions/{id}/stop прерывает идущую генерацию ≤ 1 с, сессия живёт.

Провайдер фейкуется «висящим» стримом: первый чанк приходит сразу, дальше зависание.
Stop отменяет задачу стрима через stop-событие, клиент получает ``{"type":"stopped"}``
без сохранения ассистента, и та же сессия принимает следующее сообщение.

ASGITransport буферизует тело ответа целиком, поэтому стрим гоняется в фоновой задаче,
а факт старта отслеживается по регистрации сессии в ``_ACTIVE_STREAMS``.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid

import httpx
from fastapi import FastAPI

from aria.api.auth import require_runtime_token
from aria.db import models as m
from aria.db.base import session_scope
from aria.db.models import Session as SessionRow
from aria.llm.providers.base import LlmProvider, LlmResponse, StreamChunk
from aria.llm.router import ProviderRouter
from aria.routers.chat import _ACTIVE_STREAMS, router as chat_router

STOP_BUDGET_SEC = 1.0


class HangingStream(LlmProvider):
    """Стримит первый чанк и виснет; Stop обязан оборвать зависание в пределах бюджета."""

    def __init__(self, provider_id="hang-stream"):
        self.provider_id = provider_id
        self.provider_class = "free_tier_reasoning"
        self.stopped = False

    async def check_connectivity(self, timeout_sec):
        return True

    async def chat(self, messages, tools, timeout_sec):
        return LlmResponse(text="Hello!")

    async def stream(self, messages, tools, timeout_sec):
        yield StreamChunk(text="Hel", final=False)
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.stopped = True
            raise
        yield StreamChunk(text="lo!", final=False)
        yield StreamChunk(text="", final=True)


def _app(provider: LlmProvider | None) -> FastAPI:
    llm = ProviderRouter()
    if provider is not None:
        llm.register(provider)
    app = FastAPI()
    app.include_router(chat_router)
    app.state.router = llm
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    return app


def _cleanup(*ids: str) -> None:
    with session_scope() as db:
        for i in ids:
            db.query(m.Message).filter(m.Message.session_id == uuid.UUID(i)).delete()
            db.query(SessionRow).filter(SessionRow.id == uuid.UUID(i)).delete()


async def _stop_inflight(app: FastAPI) -> tuple[str, float, int, list[str]]:
    """Запускает стрим в фоне, ждёт регистрации, шлёт stop; возвращает (sid, секунды, статус, строки)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        sid = (await c.post("/chat/sessions", json={})).json()["session_id"]
        sid_uuid = uuid.UUID(sid)

        async def do_stream():
            async with c.stream("POST", f"/chat/sessions/{sid}/send", json={"content": "hi", "stream": True}) as resp:
                return resp.status_code, [ln async for ln in resp.aiter_lines()]

        stream_task = asyncio.create_task(do_stream())
        end = time.monotonic() + 5
        while sid_uuid not in _ACTIVE_STREAMS and time.monotonic() < end:
            await asyncio.sleep(0.01)
        assert sid_uuid in _ACTIVE_STREAMS, "стрим не стартовал"

        t0 = time.monotonic()
        r = await c.post(f"/chat/sessions/{sid}/stop")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "stopped": True}
        status, lines = await stream_task
        took = time.monotonic() - t0
    return sid, took, status, lines


def test_stop_with_no_active_stream_reports_false():
    app = _app(HangingStream())

    async def drive():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            sid = (await c.post("/chat/sessions", json={})).json()["session_id"]
            try:
                r = await c.post(f"/chat/sessions/{sid}/stop")
                assert r.status_code == 200
                assert r.json() == {"ok": True, "stopped": False}
            finally:
                _cleanup(sid)

    asyncio.run(drive())


def test_stop_of_inflight_stream_finishes_within_budget():
    fake = HangingStream()
    app = _app(fake)

    async def drive():
        sid, took, status, lines = await _stop_inflight(app)
        try:
            assert status == 200
            assert took <= STOP_BUDGET_SEC, f"Stop took {took:.2f}s, budget {STOP_BUDGET_SEC}s"
            assert [json.loads(ln)["type"] for ln in lines] == ["delta", "stopped"]
            assert fake.stopped is True
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                msgs = (await c.get(f"/chat/sessions/{sid}/messages")).json()
                assert [m["role"] for m in msgs] == ["user"], "прерванный ответ не должен сохраняться"
        finally:
            _cleanup(sid)

    asyncio.run(drive())


def test_session_accepts_new_message_after_stop():
    fake = HangingStream()
    app = _app(fake)

    async def drive():
        sid, _, _, _ = await _stop_inflight(app)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                r = await c.post(f"/chat/sessions/{sid}/send", json={"content": "again"})
                assert r.status_code == 200, r.text
                assert r.json()["assistant"]["content"] == "Hello!"
        finally:
            _cleanup(sid)

    asyncio.run(drive())
