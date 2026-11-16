"""S2a: Hacker News через Algolia (без ключей)."""
from __future__ import annotations

from aria.tools.search.base import SearchProvider, SearchResult
from aria.tools.search.providers._http import get_json, strip_html

_API = "https://hn.algolia.com/api/v1/search"


class HackerNewsProvider(SearchProvider):
    name = "hackernews"
    kinds = frozenset({"tech_news", "code"})

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        data = await get_json(
            _API,
            {"query": query, "tags": "story", "hitsPerPage": max_results},
        )
        hits = data.get("hits") if isinstance(data, dict) else None
        hits = hits if isinstance(hits, list) else []
        results: list[SearchResult] = []
        for index, hit in enumerate(hits[:max_results]):
            if not isinstance(hit, dict):
                continue
            title = str(hit.get("title") or "").strip()
            if not title:
                continue
            url = str(hit.get("url") or "") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
            snippet = strip_html(str(hit.get("story_text") or "")) or f"{hit.get('points', 0)} points"
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=snippet,
                    date=str(hit.get("created_at") or ""),
                    source=self.name,
                    provider=self.name,
                    rank=index,
                )
            )
        return results
