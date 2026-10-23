"""M5: секреты не попадают в backend.log.

INFO-лог httpx печатает URL запроса целиком, а у Telegram токен бота лежит в пути
(`/bot<id>:<token>/getUpdates`). Здесь два слоя защиты: логгеры httpx/httpcore не
пишут INFO, а форматтер файла маскирует токены в любой строке (в том числе в
трассировках), чтобы новая зависимость или новый вызов не вернули утечку.
"""
from __future__ import annotations

import logging
import re

_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bbot(\d{5,}):[A-Za-z0-9_-]{20,}"), r"bot\1:***"),
    (re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 ***"),
    (re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|token|secret|password|key)=([^&\s\"']{6,})"), r"\1=***"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "sk-***"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "gh*_***"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"), "AIza***"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA***"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "xox*-***"),
)

NOISY_LIBRARY_LOGGERS = ("httpx", "httpcore")


def redact(text: str) -> str:
    """Маскирует токены и ключи в строке. Остальной текст не меняет."""
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Formatter, после которого в строке (включая traceback) нет секретов."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def quiet_library_loggers() -> None:
    """httpx/httpcore пишут URL запросов на INFO: оставляем только WARNING и выше."""
    for name in NOISY_LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
