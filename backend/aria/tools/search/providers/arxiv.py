"""S2a: arXiv Atom API без ключей; XML разбирается stdlib с запретом DTD/ENTITY."""
from __future__ import annotations

import xml.etree.ElementTree as ET

from aria.tools.search.base import SearchProvider, SearchResult
from aria.tools.search.providers._http import get_bytes, strip_html

_API = "https://export.arxiv.org/api/query"
_NS = "{http://www.w3.org/2005/Atom}"


def _text(entry: ET.Element, tag: str) -> str:
    node = entry.find(f"{_NS}{tag}")
    return strip_html((node.text or "") if node is not None else "")


class ArxivProvider(SearchProvider):
    name = "arxiv"
    kinds = frozenset({"science"})

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        raw = await get_bytes(_API, {"search_query": f"all:{query}", "start": 0, "max_results": max_results})
        lowered = raw.lower()
        if b"<!doctype" in lowered or b"<!entity" in lowered:
            raise RuntimeError("XML DTD/ENTITY is not allowed")
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise RuntimeError("invalid XML") from exc
        results: list[SearchResult] = []
        for index, entry in enumerate(root.findall(f"{_NS}entry")[:max_results]):
            title = _text(entry, "title")
            url = _text(entry, "id")
            if not title or not url:
                continue
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=_text(entry, "summary"),
                    date=_text(entry, "published"),
                    source=self.name,
                    provider=self.name,
                    rank=index,
                )
            )
        return results
