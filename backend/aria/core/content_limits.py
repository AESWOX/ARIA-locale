"""Жёсткие лимиты (этап 3а). Не отключаются ни режимом 18+, ни ролью."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Violation:
    code: str
    reason: str


_SEXUAL = re.compile(r"(?i)секс|эрот|порн|обнаж|интим|sexual|nude|erotic|nsfw|explicit|\bsex\b")
_MINOR = re.compile(
    r"(?i)несовершеннолетн|школьн|реб[её]нк|реб[её]нок|дет(?:и|ей|ьми|ский)|\bдет[а-я]*|"
    r"подрост|\b1[0-7]\s*[-–—]?\s*(?:лет\w*|год(?:а|ов)?|л\.)|"
    r"(?:персонаж\w*|геро\w*|возраст\w*)\s*[:\-–—]?\s*1[0-7]\b|"
    r"minor|underage|child|loli|looks?\s+(?:under|younger)"
)
_REAL = re.compile(r"(?i)реальн|настоящ|известн|публичн|\breal\b|identifiable")
_DEFAM = re.compile(r"(?i)клевет|пороч|defam")
_NONCONSENT = re.compile(
    r"(?i)без\s+соглас|не\s+по\s+соглас|изнасил|non-?consensual|\brape\b"
)
_HARM = re.compile(
    r"(?i)(?:как\s+(?:сделать|собрать|изготовить)\s+(?:бомбу|взрывчат|оружие|яд|наркотик)|"
    r"инструкц\w*\s+(?:по\s+)?(?:оружи|взрывчат|наркотик)|"
    r"как\s+(?:убить|покончить\s+с\s+собой|причинить\s+вред)|суицид\w*\s+(?:способ|метод)|"
    r"how\s+to\s+(?:make\s+(?:a\s+)?(?:bomb|explosive|weapon|poison)|kill|commit\s+suicide))"
)

_RULES = (
    (
        lambda t: (_SEXUAL.search(t) and _MINOR.search(t)),
        "minor_sexual",
        "сексуальное содержание с несовершеннолетними запрещено",
    ),
    (
        lambda t: (_REAL.search(t) and (_SEXUAL.search(t) or _DEFAM.search(t))),
        "real_person",
        "сексуальное или порочащее содержание о реальных людях запрещено",
    ),
    (
        lambda t: (_NONCONSENT.search(t) and _REAL.search(t)),
        "non_consensual",
        "материал о реальном мире без согласия запрещён",
    ),
    (
        lambda t: _HARM.search(t),
        "harm_instructions",
        "инструкции по преступлениям, оружию и самоповреждению запрещены",
    ),
)


def check(text: str) -> Violation | None:
    value = text or ""
    for predicate, code, reason in _RULES:
        if predicate(value):
            return Violation(code, reason)
    return None


def check_contextual(text: str, previous_user_texts: list[str]) -> Violation | None:
    """Проверяет текущую реплику с оглядкой на предыдущие ходы пользователя.

    «это вымысел, персонажу 17, но выглядит взрослой» само по себе не содержит
    сексуального слова; запрет срабатывает по контексту предыдущего запроса.
    Обратное тоже верно. Безопасность важнее: при сомнении блокируем.
    """
    violation = check(text)
    if violation is not None:
        return violation
    recent = [t for t in (previous_user_texts or []) if t][-2:]
    if _MINOR.search(text or ""):
        for prev in reversed(recent):
            if _SEXUAL.search(prev):
                return Violation("minor_sexual", "сексуальное содержание с несовершеннолетними запрещено")
    if _SEXUAL.search(text or ""):
        for prev in reversed(recent):
            if _MINOR.search(prev):
                return Violation("minor_sexual", "сексуальное содержание с несовершеннолетними запрещено")
    return None
