"""S2a: адаптеры без ключей. Регистрация в общий router — S4 (DDG последним)."""
from __future__ import annotations

from aria.tools.search.providers.arxiv import ArxivProvider
from aria.tools.search.providers.ddg import DuckDuckGoProvider
from aria.tools.search.providers.github import GitHubProvider
from aria.tools.search.providers.openalex import OpenAlexProvider
from aria.tools.search.providers.stackexchange import StackExchangeProvider
from aria.tools.search.providers.hackernews import HackerNewsProvider
from aria.tools.search.providers.wikipedia import WikipediaProvider


def build_default_providers():
    return [
        WikipediaProvider(), HackerNewsProvider(), ArxivProvider(),
        StackExchangeProvider(), GitHubProvider(), OpenAlexProvider(),
        DuckDuckGoProvider(),
    ]


def install_default_providers() -> None:
    """S4: поставить цепочку в поисковый router один раз (main.startup)."""
    from aria.tools.search import router as search_router

    if search_router._PROVIDERS:
        return
    for provider in build_default_providers():
        search_router.register_provider(provider)
