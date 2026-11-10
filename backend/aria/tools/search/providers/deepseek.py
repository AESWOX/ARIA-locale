"""F-011: DeepSeek Search API — провайдер на том же ключе, что и модели (1=1).

POST {base_url}/tools/search, тот же Bearer-ключ DeepSeek. Отдаёт title/url/
snippet/summary/date. Ошибка/402/429 — RuntimeError/SearchRateLimited, и
router переходит к следующему провайдеру (DDG последний).
"""
from __future__ import annotations

from aria.tools.search.base import SearchProvider, SearchResult
from aria.tools.search.providers._http import post_json, strip_html


class DeepSeekSearchProvider(SearchProvider):
    name = "deepseek"
    kinds = frozenset({"general", "code", "tech_news", "science"})

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key
        self.base_url = (base_url or "").rstrip("/")

    def _resolve(self) -> tuple[str | None, str]:
        if not self.api_key or not self.base_url:
            from aria.config import get_settings

            settings = get_settings()
            if not self.api_key:
                self.api_key = settings.deepseek_api_key_resolved
            if not self.base_url:
                self.base_url = (settings.deepseek_base_url or "").rstrip("/")
        return self.api_key, self.base_url

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        key, base = self._resolve()
        if not key or not base:
            raise RuntimeError("deepseek api key is not configured")
        count = max(1, min(int(max_results), 50))
        data = await post_json(
            f"{base}/tools/search",
            {"query": (query or "")[:128], "count": count, "need_content": False},
            headers={"Authorization": f"Bearer {key}"},
        )
        results: list[SearchResult] = []
        for index, item in enumerate((data.get("results") or [])[:count]):
            if not isinstance(item, dict) or not item.get("url"):
                continue
            snippet = item.get("snippet") or item.get("summary") or ""
            results.append(
                SearchResult(
                    title=str(item.get("title") or ""),
                    url=str(item["url"]),
                    snippet=strip_html(str(snippet))[:300],
                    date=str(item.get("date") or ""),
                    source=self.name,
                    provider=self.name,
                    rank=index,
                )
            )
        return results


def active_deepseek_provider() -> DeepSeekSearchProvider | None:
    """Провайдер включается только при наличии ключа DeepSeek."""
    from aria.config import get_settings

    if not get_settings().deepseek_api_key_resolved:
        return None
    return DeepSeekSearchProvider()
