"""Telegram gate: personal chats are unavailable until the bot is linked.

Thin adapter over :mod:`aria.core.channel_gate` (GATE-1). ``is_linked`` stays here because
BOT-1 will extend it to "token and (chat id or paired owner)"; everything else delegates so
there is exactly one detector and one registry.
"""
from __future__ import annotations

from aria.core import channel_gate

blocked_notice = channel_gate.blocked_notice
is_personal_request = channel_gate.is_personal_request
notice_text = channel_gate.telegram_notice_text

__all__ = ["blocked_notice", "is_linked", "is_personal_request", "notice_text"]


def is_linked(settings: object) -> bool:
    token = str(getattr(settings, "telegram_bot_token", "") or "").strip()
    chat_id = str(getattr(settings, "telegram_chat_id", "") or "").strip()
    return bool(token and chat_id)
