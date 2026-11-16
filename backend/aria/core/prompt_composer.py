"""Сборка системного промпта по слоям (этап 3а).

L0 (persona + compliance) — неизменяемый фундамент; L1 — роль; L2 — модули
(adult / storytelling / vision_describe); L3 — активные режимы и контекст.
Строки модуля, пытающиеся отменить L0, вырезаются; L0 всегда звучит последним.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "prompts"
MANIFEST = ROOT / "manifest.json"

_OVERRIDE = re.compile(
    r"(?i)\b(?:ignore|disregard|override|забудь|игнорируй)\b.{0,40}"
    r"\b(?:all|previous|persona|compliance|limits|правила|лимиты|персону)\b"
)


def manifest() -> dict:
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def module_text(name: str) -> str:
    layers = (manifest().get("layers") or {}).get("L2") or {}
    filename = layers.get(name)
    if not filename:
        return ""
    try:
        return (ROOT / str(filename)).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _sanitize(text: str) -> str:
    lines = [line for line in text.splitlines() if not _OVERRIDE.search(line)]
    return "\n".join(lines).strip()


def compose(role_prompt: str, *, adult: bool = False, modules: tuple[str, ...] = ()) -> str:
    """L1+L0 (role_prompt уже содержит их) + L2 активных модулей + L3."""
    active = list(modules)
    if adult and "adult" not in active:
        active.append("adult")
    blocks = []
    for name in active:
        text = _sanitize(module_text(name))
        if text:
            blocks.append(text)
    if not blocks and not adult:
        return role_prompt
    result = role_prompt
    if blocks:
        result += "\n\n" + "\n\n".join(blocks)
    result += "\n\nActive modes: " + ("adult=on" if adult else "adult=off")
    result += "\nHard limits from the top of this prompt override every module above."
    return result


def mode_snapshot(adult: bool) -> dict:
    return {"adult": bool(adult), "modules": ["adult"] if adult else []}
