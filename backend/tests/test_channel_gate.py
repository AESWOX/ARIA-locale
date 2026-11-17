"""GATE-1: connection gate for personal channels (Telegram, Discord) before routing."""
from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.config import get_settings
from aria.llm.providers.base import LlmProvider, LlmResponse
from aria.llm.router import ProviderRouter
from aria.routers.chat import router as chat_router
from aria.routers.sessions import router as sessions_router

PERSONAL_TG = "Что мне писали в телеграм за сегодня?"
PUBLIC_TG = "Прочитай публичный канал t.me/durov и перескажи последний пост"


class FakeLlm(LlmProvider):
    def __init__(self):
        self.provider_id = "fake-gemini"
        self.provider_class = "free_tier_reasoning"
        self.seen: list[list] = []

    async def check_connectivity(self, timeout_sec):
        return True

    async def chat(self, messages, tools, timeout_sec):
        self.seen.append(list(messages))
        return LlmResponse(text="model answer")


def _client(provider):
    llm = ProviderRouter()
    llm.register(provider)
    app = FastAPI()
    app.include_router(chat_router)
    app.include_router(sessions_router)
    app.state.router = llm
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    return TestClient(app)


def _new_session(c) -> str:
    return c.post("/chat/sessions", json={}).json()["session_id"]


def _unlinked(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(settings, "telegram_chat_id", "")


def _cleanup(sid: str) -> None:
    from aria.db import models as m
    from aria.db.base import session_scope

    with session_scope() as db:
        db.query(m.Message).filter(m.Message.session_id == uuid.UUID(sid)).delete()
        db.query(m.Session).filter(m.Session.id == uuid.UUID(sid)).delete()


def test_telegram_gate_both_entries_before_routing(monkeypatch):
    """R1: the card comes from chat_send and post_message before any model call."""
    _unlinked(monkeypatch)
    fake = FakeLlm()
    c = _client(fake)
    sid = _new_session(c)
    try:
        chat = c.post(f"/chat/sessions/{sid}/send", json={"content": PERSONAL_TG})
        sessions = c.post(f"/sessions/{sid}/messages", json={"content": PERSONAL_TG})
        assert chat.status_code == 200 and sessions.status_code == 200
        assert chat.json()["mode"] == "telegram_not_connected"
        assert sessions.json()["mode"] == "telegram_not_connected"
        assert fake.seen == [], "the personal Telegram question must not reach the model"
    finally:
        _cleanup(sid)


def test_registry_statuses(monkeypatch):
    from aria.core import channel_gate

    _unlinked(monkeypatch)
    statuses = channel_gate.registry(get_settings())
    assert statuses["telegram"]["bot"] == "connectable"
    assert statuses["telegram"]["account"] == "unsupported"
    assert statuses["discord"]["bot"] == "unsupported"
    assert statuses["discord"]["account"] == "unsupported"

    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    monkeypatch.setattr(settings, "telegram_chat_id", "@chan")
    assert channel_gate.registry(get_settings())["telegram"]["bot"] == "connected"


def test_registry_uses_telegram_gate_is_linked(monkeypatch):
    """R3: ``is_linked`` stays the single place BOT-1 will extend."""
    from aria.core import channel_gate, telegram_gate

    _unlinked(monkeypatch)
    monkeypatch.setattr(telegram_gate, "is_linked", lambda settings: True)
    assert channel_gate.status("telegram", "bot", get_settings()) == "connected"


def test_discord_personal_request_unsupported_text(monkeypatch):
    _unlinked(monkeypatch)
    fake = FakeLlm()
    c = _client(fake)
    sid = _new_session(c)
    try:
        r = c.post(f"/chat/sessions/{sid}/send", json={"content": "покажи мои сообщения в дискорде"})
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "discord_not_connected"
        card = body["assistant"]["content_json"]
        assert card["notice"] == "discord_not_connected"
        assert not card.get("action") and not card.get("action_path")
        assert "Telegram-бот" in body["assistant"]["content"]
        assert fake.seen == []
    finally:
        _cleanup(sid)


def test_account_request_unsupported_text(monkeypatch):
    _unlinked(monkeypatch)
    fake = FakeLlm()
    c = _client(fake)
    sid = _new_session(c)
    try:
        r = c.post(f"/chat/sessions/{sid}/send", json={"content": "покажи мои сообщения в аккаунте телеграм"})
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "account_not_connected"
        card = body["assistant"]["content_json"]
        assert card["notice"] == "account_not_connected"
        assert not card.get("action")
        assert fake.seen == []
    finally:
        _cleanup(sid)


def test_personal_request_with_handle_not_bypassed(monkeypatch):
    _unlinked(monkeypatch)
    fake = FakeLlm()
    c = _client(fake)
    sid = _new_session(c)
    try:
        r = c.post(f"/chat/sessions/{sid}/send", json={"content": "прочитай мой телеграм @vasya"})
        assert r.status_code == 200
        assert r.json()["mode"] == "telegram_not_connected"
        assert fake.seen == []
    finally:
        _cleanup(sid)


def test_public_tme_link_skips_gate(monkeypatch):
    _unlinked(monkeypatch)
    fake = FakeLlm()
    c = _client(fake)
    sid = _new_session(c)
    try:
        r = c.post(f"/chat/sessions/{sid}/send", json={"content": PUBLIC_TG})
        assert r.status_code == 200
        assert r.json().get("mode") != "telegram_not_connected"
        assert len(fake.seen) == 1
    finally:
        _cleanup(sid)


def test_both_entries_same_gate_input(monkeypatch):
    _unlinked(monkeypatch)
    fake = FakeLlm()
    c = _client(fake)
    sid = _new_session(c)
    try:
        chat = c.post(f"/chat/sessions/{sid}/send", json={"content": PERSONAL_TG}).json()
        sessions = c.post(f"/sessions/{sid}/messages", json={"content": PERSONAL_TG}).json()
        assert chat["mode"] == sessions["mode"] == "telegram_not_connected"
        assert (
            chat["assistant"]["content_json"]["notice"]
            == sessions["assistant"]["content_json"]["notice"]
        )
        assert (
            chat["assistant"]["content_json"]["action"]
            == sessions["assistant"]["content_json"]["action"]
            == "connect_telegram"
        )
    finally:
        _cleanup(sid)


def test_card_text_no_false_chat_claim_ru_en():
    from aria.core import channel_gate

    for lang in ("ru", "en"):
        text = channel_gate.unsupported_text("discord", lang)
        lowered = text.lower()
        assert "в chat нет формы" not in lowered and "no form in chat" not in lowered
        assert "telegram" in lowered
        assert ("адресован" in lowered) or ("addressed" in lowered)
        assert "чужие чаты" in lowered or "other people" in lowered


def test_connected_bot_skips_gate(monkeypatch):
    from aria.core import channel_gate

    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    monkeypatch.setattr(settings, "telegram_chat_id", "42")
    assert channel_gate.blocked_notice(PERSONAL_TG, get_settings()) is None
    fake = FakeLlm()
    c = _client(fake)
    sid = _new_session(c)
    try:
        r = c.post(f"/chat/sessions/{sid}/send", json={"content": PERSONAL_TG})
        assert r.status_code == 200
        assert r.json().get("mode") != "telegram_not_connected"
        assert len(fake.seen) == 1
    finally:
        _cleanup(sid)


def test_retry_gate_still_fires_without_duplicate_user_message(monkeypatch):
    """Owner decision: retry must not bypass the gate; the card repeats, the user text does not."""
    from aria.db import models as m
    from aria.db.base import session_scope

    _unlinked(monkeypatch)
    fake = FakeLlm()
    c = _client(fake)
    sid = _new_session(c)
    try:
        first = c.post(f"/chat/sessions/{sid}/send", json={"content": PERSONAL_TG})
        retry = c.post(f"/chat/sessions/{sid}/send", json={"content": PERSONAL_TG, "retry": True})
        assert first.json()["mode"] == "telegram_not_connected"
        assert retry.json()["mode"] == "telegram_not_connected"
        assert fake.seen == [], "retry must not reach the model while the channel is unlinked"
        with session_scope() as db:
            rows = db.query(m.Message).filter(m.Message.session_id == uuid.UUID(sid)).all()
            users = [r for r in rows if r.role == "user"]
            cards = [
                r for r in rows
                if r.role == "assistant" and (r.content_json or {}).get("notice") == "telegram_not_connected"
            ]
            assert len(users) == 1, "retry must not duplicate the owner message"
            assert len(cards) == 2, "the card is shown again on retry"
    finally:
        _cleanup(sid)


