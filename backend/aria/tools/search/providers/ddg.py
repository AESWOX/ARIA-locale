"""S4: DuckDuckGo как последний провайдер цепочки; блок уходит в кулдаун router'а."""
from __future__ import annotations

import asyncio

from aria.tools.search.base import SearchProvider, SearchRateLimited, SearchResult


class DuckDuckGoProvider(SearchProvider):
    name = "duckduckgo"
    kinds = frozenset({"general"})

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        from aria.tools.handlers import web

        out = await asyncio.to_thread(web._search_blocking, query, max_results)
        if out.get("blocked"):
            raise SearchRateLimited(str(out.get("error") or "blocked"))
        if out.get("error"):
            raise RuntimeError(str(out.get("error")))
        results: list[SearchResult] = []
        for index, item in enumerate((out.get("results") or [])[:max_results]):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "")
            if not url:
                continue
            results.append(
                SearchResult(
                    title=str(item.get("title") or ""),
                    url=url,
                    snippet=str(item.get("snippet") or ""),
                    source=self.name,
                    provider=self.name,
                    rank=index,
                )
            )
        return results
