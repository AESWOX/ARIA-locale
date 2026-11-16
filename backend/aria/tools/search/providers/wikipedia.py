"""S2a: Wikipedia Search API без ключей."""
from __future__ import annotations

from urllib.parse import quote

from aria.tools.search.base import SearchProvider, SearchResult
from aria.tools.search.providers._http import get_json, strip_html

_API = "https://{lang}.wikipedia.org/w/api.php"
_UK = set("іїєґ")
_CYR = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюя")


def _lang(query: str) -> str:
    text = (query or "").lower()
    if any(ch in _UK for ch in text):
        return "uk"
    if any(ch in _CYR for ch in text):
        return "ru"
    return "en"


class WikipediaProvider(SearchProvider):
    name = "wikipedia"
    kinds = frozenset({"general", "science"})

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        lang = _lang(query)
        data = await get_json(
            _API.format(lang=lang),
            {
                "action": "query",
                "list": "search",
                "format": "json",
                "utf8": 1,
                "srsearch": query,
                "srlimit": max_results,
            },
        )
        pages = ((data.get("query") or {}).get("search") or []) if isinstance(data, dict) else []
        results: list[SearchResult] = []
        for index, page in enumerate(pages[:max_results]):
            if not isinstance(page, dict):
                continue
            title = str(page.get("title") or "")
            if not title:
                continue
            results.append(
                SearchResult(
                    title=title,
                    url=f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'), safe='/:')}",
                    snippet=strip_html(str(page.get("snippet") or "")),
                    source=self.name,
                    provider=self.name,
                    rank=index,
                )
            )
        return results
