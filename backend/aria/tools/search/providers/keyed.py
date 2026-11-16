"""S7: необязательные keyed-адаптеры (Tavily, Brave, Exa, Serper).

Адаптер активен только если в окружении есть его ключ. Ключ идёт только в заголовке.
"""
from __future__ import annotations

from aria.secrets import secret_provider
from aria.tools.search.base import SearchProvider, SearchResult
from aria.tools.search.providers._http import get_json, post_json, strip_html


def _key(name: str) -> str | None:
    return secret_provider.get_key(name)


class TavilyProvider(SearchProvider):
    name = "tavily"
    kinds = frozenset({"general", "code", "tech_news", "science"})

    def __init__(self, key: str | None = None):
        self.key = key or _key("TAVILY_API_KEY")

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        data = await post_json(
            "https://api.tavily.com/search",
            {"query": query, "max_results": max_results},
            headers={"Authorization": f"Bearer {self.key}"},
        )
        results = []
        for index, item in enumerate((data.get("results") or [])[:max_results]):
            if not isinstance(item, dict) or not item.get("url"):
                continue
            results.append(SearchResult(
                title=str(item.get("title") or ""), url=str(item["url"]),
                snippet=strip_html(str(item.get("content") or "")), source=self.name,
                provider=self.name, rank=index,
            ))
        return results


class BraveProvider(SearchProvider):
    name = "brave"
    kinds = frozenset({"general", "code", "tech_news", "science"})

    def __init__(self, key: str | None = None):
        self.key = key or _key("BRAVE_API_KEY")

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        data = await get_json(
            "https://api.search.brave.com/res/v1/web/search",
            {"q": query, "count": max_results},
            headers={"X-Subscription-Token": str(self.key), "Accept": "application/json"},
        )
        items = ((data.get("web") or {}).get("results") or []) if isinstance(data.get("web"), dict) else []
        results = []
        for index, item in enumerate(items[:max_results]):
            if not isinstance(item, dict) or not item.get("url"):
                continue
            results.append(SearchResult(
                title=strip_html(str(item.get("title") or "")), url=str(item["url"]),
                snippet=strip_html(str(item.get("description") or "")), source=self.name,
                provider=self.name, rank=index,
            ))
        return results


class ExaProvider(SearchProvider):
    name = "exa"
    kinds = frozenset({"general", "code", "tech_news", "science"})

    def __init__(self, key: str | None = None):
        self.key = key or _key("EXA_API_KEY")

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        data = await post_json(
            "https://api.exa.ai/search",
            {"query": query, "numResults": max_results},
            headers={"x-api-key": str(self.key)},
        )
        results = []
        for index, item in enumerate((data.get("results") or [])[:max_results]):
            if not isinstance(item, dict) or not item.get("url"):
                continue
            results.append(SearchResult(
                title=str(item.get("title") or ""), url=str(item["url"]),
                snippet=strip_html(str(item.get("text") or ""))[:500], source=self.name,
                provider=self.name, rank=index,
            ))
        return results


class SerperProvider(SearchProvider):
    name = "serper"
    kinds = frozenset({"general", "code", "tech_news", "science"})

    def __init__(self, key: str | None = None):
        self.key = key or _key("SERPER_API_KEY")

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        data = await post_json(
            "https://google.serper.dev/search",
            {"q": query, "num": max_results},
            headers={"X-API-KEY": str(self.key)},
        )
        results = []
        for index, item in enumerate((data.get("organic") or [])[:max_results]):
            if not isinstance(item, dict) or not item.get("link"):
                continue
            results.append(SearchResult(
                title=str(item.get("title") or ""), url=str(item["link"]),
                snippet=strip_html(str(item.get("snippet") or "")), source=self.name,
                provider=self.name, rank=index,
            ))
        return results


def active_keyed_providers() -> list[SearchProvider]:
    providers: list[SearchProvider] = []
    for cls, key_name in (
        (TavilyProvider, "TAVILY_API_KEY"),
        (BraveProvider, "BRAVE_API_KEY"),
        (ExaProvider, "EXA_API_KEY"),
        (SerperProvider, "SERPER_API_KEY"),
    ):
        key = _key(key_name)
        if key:
            providers.append(cls(key))
    return providers
