"""Чтение текстовых файлов без зависимости от кодировки Windows по умолчанию (BUG-4).

`Path.read_text()` без encoding на Windows берёт cp1251: файл, записанный агентом в UTF-8, читался как
«РџРѕР¶Р°Р»СѓР№СЃС‚Р°», и аудитор браковал результат. Сначала UTF-8 (с BOM или без), для файлов из «Блокнота»
запасной вариант cp1251, в самом конце UTF-8 с заменой нечитаемых байт.
"""
from __future__ import annotations

from pathlib import Path


def decode_text(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1251")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def read_text_auto(path: Path) -> str:
    return decode_text(Path(path).read_bytes())
