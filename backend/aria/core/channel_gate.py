"""Channel connection gate (GATE-1).

One place that answers "is this personal channel request connectable?": a registry
(channel × method → connected / connectable / unsupported) and one RU/EN detector for
personal Telegram and Discord questions. Public Telegram links (``t.me/…``) are not gated and
stay on the ``telegram_channel`` tool.

``telegram_gate.is_linked`` remains the single source of truth for "Telegram bot linked";
BOT-1 will extend it to "token and (chat id or paired owner)" without touching this module.
"""
from __future__ import annotations

import re
from typing import Any

CONNECTED = "connected"
CONNECTABLE = "connectable"
UNSUPPORTED = "unsupported"

CHANNELS = ("telegram", "discord")
METHODS = ("bot", "account")
_SUPPORTED = {("telegram", "bot")}

_TELEGRAM_MARKERS = re.compile(r"телеграм|телеге|(?<![A-Za-zА-Яа-я])тг(?![A-Za-zА-Яа-я])|telegram", re.IGNORECASE)
_DISCORD_MARKERS = re.compile(r"дискорд|discord|(?<![A-Za-zА-Яа-я])дс(?![A-Za-zА-Яа-я])", re.IGNORECASE)
_ACCOUNT_MARKERS = re.compile(r"аккаунт|account", re.IGNORECASE)
_PUBLIC_TME = re.compile(r"t\.me/", re.IGNORECASE)

_PERSONAL_RE = re.compile(
    # «мой телеграм / моём дискорде / мой аккаунт» — личные формулировки для обоих каналов
    r"(?:мой|моем|моём|моего|мою|мои|моих|мне)\s+(?:телеграм|телеграме|телеграма|телеге|тг|"
    r"дискорд|дискорде|дискорда|discord|дс)"
    # «мои сообщения / переписка / чаты / канал» — канал определяется маркером в тексте
    r"|(?:мои|моих)\s+(?:сообщени|переписк|чат|канал)"
    r"|(?:что|кто)\s+(?:мне\s+)?(?:пишет|писал|написал|прислал|пишут).{0,40}"
    r"(?:телеграм|телеге|тг|дискорд|дискорде|discord|дс)"
    r"|(?:прочитай|покажи|проверь|посмотри|глянь).{0,30}"
    r"(?:телеграм|телеге|тг|дискорд|дискорде|discord|дс)"
    r"|my\s+(?:telegram|discord)"
    r"|(?:telegram|discord)\s+(?:messages|chats|account|inbox)",
    re.IGNORECASE,
)


def status(channel: str, method: str, settings: object) -> str:
    """Registry lookup. ``telegram+bot`` is the only real connector in this build."""
    if (channel, method) not in _SUPPORTED:
        return UNSUPPORTED
    from aria.core import telegram_gate

    return CONNECTED if telegram_gate.is_linked(settings) else CONNECTABLE


def registry(settings: object) -> dict[str, dict[str, str]]:
    return {channel: {method: status(channel, method, settings) for method in METHODS} for channel in CHANNELS}


def detect(text: str) -> tuple[str | None, str]:
    """Return ``(channel, method)`` for a personal channel request, or ``(None, "bot")``."""
    content = (text or "").strip()
    if not content or _PUBLIC_TME.search(content):
        return None, "bot"
    if not _PERSONAL_RE.search(content):
        return None, "bot"
    method = "account" if _ACCOUNT_MARKERS.search(content) else "bot"
    if _DISCORD_MARKERS.search(content) and not _TELEGRAM_MARKERS.search(content):
        return "discord", method
    return "telegram", method


def is_personal_request(text: str) -> bool:
    return detect(text)[0] is not None


def telegram_notice_text(lang: str = "ru") -> str:
    if str(lang).lower().startswith("en"):
        return (
            "Telegram is not connected: I have no access to your account or messages.\n"
            "How to connect:\n"
            "1. Open @BotFather → /newbot → copy the token.\n"
            "2. Add the bot as an admin to your channel or chat.\n"
            "3. Enter the token and the channel as @name below.\n"
            "Public channels already work via t.me/s — send a link."
        )
    return (
        "Telegram не подключён: у меня нет доступа к твоему аккаунту и сообщениям.\n"
        "Как подключить:\n"
        "1. Открой @BotFather → /newbot → скопируй токен.\n"
        "2. Добавь бота админом в свой канал или чат.\n"
        "3. Введи токен и канал через @ (например @my_channel) в форме ниже.\n"
        "Публичные каналы уже работают через t.me/s — просто пришли ссылку."
    )


def unsupported_text(channel: str, lang: str = "ru", method: str = "bot") -> str:
    """No promise of reading chats: the bot only sees messages addressed to it."""
    if str(lang).lower().startswith("en"):
        return (
            "This connection method is not supported in this build yet: only the Telegram bot "
            "is available. The bot sees only messages addressed to it and does not read other "
            "people's chats or channels."
        )
    return (
        "Этот способ подключения пока не поддерживается в этой сборке: доступен только "
        "Telegram-бот. Бот видит только сообщения, адресованные ему, и не читает чужие чаты "
        "и каналы."
    )


def blocked_notice(text: str, settings: object) -> dict[str, Any] | None:
    """The gate decision. ``None`` means the request may go the normal way."""
    channel, method = detect(text)
    if channel is None:
        return None
    state = status(channel, method, settings)
    if state == CONNECTED:
        return None
    from aria.core.clarifier import detect_language

    lang = detect_language(text)
    if state == CONNECTABLE:
        return {
            "notice": "telegram_not_connected",
            "mode": "telegram_not_connected",
            "text": telegram_notice_text(lang),
            "action": "connect_telegram",
            "action_path": "/channels",
        }
    notice = "discord_not_connected" if channel == "discord" else "account_not_connected"
    return {
        "notice": notice,
        "mode": notice,
        "text": unsupported_text(channel, lang, method),
    }
