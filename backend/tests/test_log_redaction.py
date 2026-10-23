"""M5: токен Telegram-бота и ключи не попадают в backend.log."""
from __future__ import annotations

import logging

from aria import logredact
from aria.logredact import RedactingFormatter, redact

TG = "bot1234567890:AAH-abcdefghijklmnopqrstuvwxyz012345"


def test_telegram_token_in_httpx_style_line_is_masked():
    line = f'HTTP Request: POST https://api.telegram.org/{TG}/getUpdates "HTTP/1.1 200 OK"'
    out = redact(line)
    assert "AAH-abcdefghijkl" not in out
    assert "bot1234567890:***" in out and "getUpdates" in out


def test_bearer_and_provider_keys_are_masked():
    for secret in ("sk-abcdefghijklmnopqrstu", "AIzaSyA1234567890abcdefghijk", "ghp_abcdefghijklmnopqrstuvwx"):
        assert secret not in redact(f"key was {secret} here")
    assert "abcdef123456" not in redact("Authorization: Bearer abcdef123456.xyz")
    assert "supersecret1" not in redact("GET /x?api_key=supersecret1&a=1")


def test_ordinary_text_is_untouched():
    text = "Stage 3: волна 1/2, группа [0] — до 3 узлов параллельно (task 3a5f4292)"
    assert redact(text) == text


def test_formatter_masks_tracebacks_too():
    fmt = RedactingFormatter("%(levelname)s %(message)s")
    try:
        raise RuntimeError(f"fail https://api.telegram.org/{TG}/sendMessage")
    except RuntimeError:
        import sys

        rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", None, sys.exc_info())
    assert "AAH-abcdefghijkl" not in fmt.format(rec)


def test_main_file_handler_uses_redacting_formatter_and_httpx_is_quiet():
    import aria.main as main

    assert isinstance(main._file_handler.formatter, RedactingFormatter)
    for name in logredact.NOISY_LIBRARY_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
