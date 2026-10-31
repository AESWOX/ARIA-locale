"""S2b: OpenAlex works; вежливый пул через mailto из настроек."""
from __future__ import annotations

from aria.config import get_settings
from aria.tools.search.base import SearchProvider, SearchResult
from aria.tools.search.providers._http import get_json

_API = "https://api.openalex.org/works"
_ABSTRACT_LIMIT = 300


def _abstract(item: dict) -> str:
    inverted = item.get("abstract_inverted_index")
    if not isinstance(inverted, dict):
        return ""
    positions: list[tuple[int, str]] = []
    for word, places in inverted.items():
        if not isinstance(places, list):
            continue
        for place in places:
            if isinstance(place, int):
                positions.append((place, str(word)))
    text = " ".join(word for _pos, word in sorted(positions))
    return text[:_ABSTRACT_LIMIT]


class OpenAlexProvider(SearchProvider):
    name = "openalex"
    kinds = frozenset({"science"})

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        params = {"search": query, "per-page": max_results}
        mailto = get_settings().openalex_mailto
        if mailto:
            params["mailto"] = mailto
        data = await get_json(_API, params)
        items = data.get("results") if isinstance(data, dict) else None
        items = items if isinstance(items, list) else []
        results: list[SearchResult] = []
        for index, item in enumerate(items[:max_results]):
            if not isinstance(item, dict):
                continue
            title = str(item.get("display_name") or "").strip()
            url = str(item.get("id") or "").strip()
            if not title or not url:
                continue
            doi = item.get("doi")
            snippet = _abstract(item)
            if doi:
                snippet = f"{snippet} doi: {doi}".strip()
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=snippet,
                    date=str(item.get("publication_date") or ""),
                    source=self.name,
                    provider=self.name,
                    rank=index,
                )
            )
        return results
