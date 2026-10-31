"""S1: классификатор запроса без LLM (code|science|tech_news|general)."""
from __future__ import annotations

_CODE = (
    "traceback", "exception", "ошибк", "error", "api", "python", "javascript", "typescript",
    "c++", "rust", "golang", "regex", "pytest", "docker", "kubernetes", "sql", "git",
)
_SCIENCE = (
    "arxiv", "paper", "doi", "pubmed", "исследован", "научн", "статья", "preprint", "теорем",
)
_NEWS = ("новост", "release", "announce", "сегодня", "2026", "2025", "launch", "обзор")


def classify(query: str) -> str:
    text = (query or "").lower()
    if any(word in text for word in _SCIENCE):
        return "science"
    if any(word in text for word in _CODE):
        return "code"
    if any(word in text for word in _NEWS):
        return "tech_news"
    return "general"
