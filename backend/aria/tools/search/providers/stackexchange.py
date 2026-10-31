"""S2b: StackExchange (Stack Overflow) без ключей."""
from __future__ import annotations

from datetime import datetime, timezone

from aria.tools.search.base import SearchProvider, SearchResult
from aria.tools.search.providers._http import get_json, strip_html

_API = "https://api.stackexchange.com/2.3/search/advanced"


def _iso(value) -> str:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return str(value or "")


class StackExchangeProvider(SearchProvider):
    name = "stackexchange"
    kinds = frozenset({"code"})

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        data = await get_json(
            _API,
            {"q": query, "site": "stackoverflow", "pagesize": max_results, "order": "desc", "sort": "relevance"},
        )
        items = data.get("items") if isinstance(data, dict) else None
        items = items if isinstance(items, list) else []
        results: list[SearchResult] = []
        for index, item in enumerate(items[:max_results]):
            if not isinstance(item, dict):
                continue
            title = strip_html(str(item.get("title") or ""))
            link = str(item.get("link") or "")
            if not title or not link:
                continue
            results.append(
                SearchResult(
                    title=title,
                    url=link,
                    snippet=f"score {item.get('score', 0)}; answered: {bool(item.get('is_answered'))}",
                    date=_iso(item.get("creation_date")),
                    source=self.name,
                    provider=self.name,
                    rank=index,
                )
            )
        return results
