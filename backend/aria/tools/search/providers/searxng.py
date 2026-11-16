"""S7: собственный SearXNG (JSON-вывод) — необязательный адаптер."""
from __future__ import annotations

from urllib.parse import urlsplit

from aria.config import get_settings
from aria.tools.search.base import SearchProvider, SearchResult
from aria.tools.search.providers._http import get_json, strip_html


def valid_searxng_url(url: str) -> str | None:
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    if parts.username or parts.password:
        return None
    return url.strip().rstrip("/")


class SearXNGProvider(SearchProvider):
    name = "searxng"
    kinds = frozenset({"general", "code", "tech_news", "science"})

    def __init__(self, base_url: str | None = None):
        self.base_url = valid_searxng_url(base_url or get_settings().searxng_url or "")

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        if not self.base_url:
            return []
        data = await get_json(f"{self.base_url}/search", {"q": query, "format": "json"})
        results = []
        for index, item in enumerate((data.get("results") or [])[:max_results]):
            if not isinstance(item, dict) or not item.get("url"):
                continue
            results.append(SearchResult(
                title=strip_html(str(item.get("title") or "")), url=str(item["url"]),
                snippet=strip_html(str(item.get("content") or "")), source=self.name,
                provider=self.name, rank=index,
            ))
        return results


def active_searxng_provider() -> SearchProvider | None:
    base = valid_searxng_url(get_settings().searxng_url or "")
    return SearXNGProvider(base) if base else None
