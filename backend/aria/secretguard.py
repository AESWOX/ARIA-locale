"""SEC-6: секреты не доходят до модели, shell, экспорта и логов.

Детектор работает на входе чата и агентского цикла и на границах записи:
сообщения, аргументы и результаты инструментов, экспорт сессии, zip диагностики.
Найденное заменяется на ``[скрыто]``; исходное значение не сохраняется.
"""
from __future__ import annotations

import re
from typing import Any

MASK = "[скрыто]"

_BOT_TOKEN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_SK = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")
_GOOGLE = re.compile(r"\bAIza[0-9A-Za-z_-]{20,}")
_GROQ = re.compile(r"\bgsk_[A-Za-z0-9]{16,}")
_GITHUB = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")
_SLACK = re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")
_AWS = re.compile(r"\bAKIA[0-9A-Z]{16}\b")

_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    _BOT_TOKEN,
    _BEARER,
    _SK,
    _GOOGLE,
    _GROQ,
    _GITHUB,
    _SLACK,
    _AWS,
)

_ASSIGN = re.compile(
    r"(?i)(?<![\w-])"
    r"((?:[a-z0-9]+[_-]+)?(?:api[_-]?key|token|secret|password|passwd|pin))"
    r"([\"']?\s*[:=]\s*)"
    r"([\"'])"
    r"([^\s\"',;}]{4,})"
    r"\3"
)
_ASSIGN_BARE = re.compile(
    r"(?i)(?<![\w-])"
    r"((?:[a-z0-9]+[_-]+)?(?:api[_-]?key|token|secret|password|passwd|pin))"
    r"([\"']?\s*[:=]\s*)"
    r"([^\s\"',;}]{4,})"
)

_MASKED_VALUES = {MASK, "***", "[REDACTED]", "[redacted]", "xxx", "xxxx"}


def _assign_repl(match: re.Match[str]) -> str:
    value = match.group(4) if match.lastindex and match.lastindex >= 4 else match.group(3)
    if value.strip().lower() in _MASKED_VALUES:
        return match.group(0)
    head = match.group(1) + match.group(2)
    if match.lastindex and match.lastindex >= 4:
        quote = match.group(3)
        return f"{head}{quote}{MASK}{quote}"
    return f"{head}{MASK}"


def contains_secret(text: str | None) -> bool:
    """True, если в строке есть незамаскированный секрет."""
    if not text:
        return False
    if any(pattern.search(text) for pattern in _KEY_PATTERNS):
        return True
    for match in _ASSIGN_BARE.finditer(text):
        if match.group(3).strip().lower() not in _MASKED_VALUES:
            return True
    return False


def mask_secrets(text: str | None) -> str:
    """Заменяет секреты на ``[скрыто]``; остальной текст не меняет."""
    if not text:
        return text or ""
    out = text
    out = _BEARER.sub(f"Bearer {MASK}", out)
    for pattern in (_BOT_TOKEN, _SK, _GOOGLE, _GROQ, _GITHUB, _SLACK, _AWS):
        out = pattern.sub(MASK, out)
    out = _ASSIGN.sub(_assign_repl, out)
    out = _ASSIGN_BARE.sub(_assign_repl, out)
    return out


def mask_obj(value: Any) -> Any:
    """Рекурсивно маскирует строки в dict/list; структура и не-строки сохраняются."""
    if isinstance(value, str):
        return mask_secrets(value)
    if isinstance(value, dict):
        return {key: mask_obj(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_obj(item) for item in value]
    return value


def notice_text(lang: str = "ru") -> str:
    if str(lang).lower().startswith("en"):
        return (
            "A secret was detected: it is hidden and was not sent to the model. "
            "Enter the key in Settings → Providers."
        )
    return (
        "Обнаружен секрет: он скрыт и не отправлен модели. "
        "Введите ключ в Settings → Providers."
    )
