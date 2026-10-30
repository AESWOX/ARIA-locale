"""Учёт токенов и стоимости из usage провайдера (19.6a).

Провайдеры отдают usage по-разному: OpenAI/DeepSeek — prompt_tokens/completion_tokens,
другие — input_tokens/output_tokens. Цены ProviderModel — USD за токен (как отдаёт
провайдер в pricing.prompt/completion), не за миллион.
"""
from __future__ import annotations

from typing import Any


def usage_tokens(usage: dict[str, Any] | None) -> tuple[int, int] | None:
    """(вход, выход) из usage; None, если токенов нет вовсе или они битые."""
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
    completion = usage.get("completion_tokens", usage.get("output_tokens"))
    try:
        inp = int(prompt) if prompt is not None else None
        out = int(completion) if completion is not None else None
    except (TypeError, ValueError):
        return None
    if inp is None and out is None:
        return None
    return max(0, inp or 0), max(0, out or 0)


def usage_cost(
    usage: dict[str, Any] | None,
    prompt_price: float | None,
    completion_price: float | None,
) -> float | None:
    """Стоимость вызова в USD; None, если цен нет или usage пуст."""
    if prompt_price is None or completion_price is None:
        return None
    tokens = usage_tokens(usage)
    if tokens is None:
        return None
    inp, out = tokens
    try:
        cost = inp * float(prompt_price) + out * float(completion_price)
    except (TypeError, ValueError):
        return None
    return round(cost, 6)
