"""Фаза S: интерфейс поисковых провайдеров (S1)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    date: str = ""
    source: str = ""
    rank: int = 0
    provider: str = ""


class SearchProvider(ABC):
    name: str = ""
    kinds: frozenset[str] = field(default_factory=frozenset)

    @abstractmethod
    async def search(self, query: str, max_results: int) -> list[SearchResult]: ...


class SearchRateLimited(Exception):
    """429/блок: провайдер уходит в кулдаун."""
