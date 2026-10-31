"""S2a: адаптеры без ключей. Регистрация в общий router — S4 (DDG последним)."""
from __future__ import annotations

from aria.tools.search.providers.arxiv import ArxivProvider
from aria.tools.search.providers.hackernews import HackerNewsProvider
from aria.tools.search.providers.wikipedia import WikipediaProvider


def build_default_providers():
    return [WikipediaProvider(), HackerNewsProvider(), ArxivProvider()]
