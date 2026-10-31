"""S2b: GitHub repository search; токен (если задан) только в заголовке."""
from __future__ import annotations

from aria.secrets import secret_provider
from aria.tools.search.base import SearchProvider, SearchResult
from aria.tools.search.providers._http import get_json, strip_html

_API = "https://api.github.com/search/repositories"


def _token() -> str | None:
    return secret_provider.get_key("GITHUB_SEARCH_TOKEN") or secret_provider.get_key("GITHUB_TOKEN")


class GitHubProvider(SearchProvider):
    name = "github"
    kinds = frozenset({"code"})

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        headers = {"Accept": "application/vnd.github+json"}
        token = _token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = await get_json(_API, {"q": query, "per_page": max_results}, headers=headers)
        items = data.get("items") if isinstance(data, dict) else None
        items = items if isinstance(items, list) else []
        results: list[SearchResult] = []
        for index, item in enumerate(items[:max_results]):
            if not isinstance(item, dict):
                continue
            title = str(item.get("full_name") or "").strip()
            url = str(item.get("html_url") or "").strip()
            if not title or not url:
                continue
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=strip_html(str(item.get("description") or "")),
                    date=str(item.get("updated_at") or ""),
                    source=self.name,
                    provider=self.name,
                    rank=index,
                )
            )
        return results
