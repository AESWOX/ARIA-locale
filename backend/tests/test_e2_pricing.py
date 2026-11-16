"""E2: цены из prices.json, тариф peak/off, next_offpeak, стоимость в llm_usage."""
from __future__ import annotations

from datetime import datetime, timezone
import pytest
from sqlalchemy import select

from aria.db import models as m
from aria.db.base import session_scope
from aria.llm import pricing
from aria.llm.providers.base import LlmProvider, LlmResponse
from aria.llm.router import ProviderRouter

MONDAY = (2026, 11, 2)
SATURDAY = (2026, 11, 7)


def _dt(day: tuple[int, int, int], hour: int, minute: int) -> datetime:
    return datetime(*day, hour, minute, tzinfo=timezone.utc)


def test_peak_window_boundaries():
    assert pricing.tier_for(_dt(MONDAY, 0, 59)) == "off"
    assert pricing.tier_for(_dt(MONDAY, 1, 0)) == "peak"
    assert pricing.tier_for(_dt(MONDAY, 3, 59)) == "peak"
    assert pricing.tier_for(_dt(MONDAY, 4, 0)) == "off"
    assert pricing.tier_for(_dt(MONDAY, 6, 0)) == "peak"
    assert pricing.tier_for(_dt(MONDAY, 9, 59)) == "peak"
    assert pricing.tier_for(_dt(MONDAY, 10, 0)) == "off"


def test_weekend_is_off_peak():
    assert pricing.tier_for(_dt(SATURDAY, 2, 0)) == "off"


def test_offpeak_dates_and_aliases(monkeypatch):
    spec = pricing.load_prices()
    patched = dict(spec)
    patched["offpeak_dates"] = ["2026-11-02"]
    monkeypatch.setattr(pricing, "load_prices", lambda: patched)
    assert pricing.tier_for(_dt(MONDAY, 2, 0)) == "off"

    monkeypatch.setattr(pricing, "load_prices", lambda: spec)
    via_alias, tier = pricing.cost_usd("deepseek-v4-flash-vision-exp", 1_000_000, 0, 0, _dt(SATURDAY, 2, 0))
    via_name, _ = pricing.cost_usd("deepseek-flash", 1_000_000, 0, 0, _dt(SATURDAY, 2, 0))
    assert tier == "off" and via_alias == via_name == 0.003


def test_unknown_and_none_model():
    assert pricing.cost_usd(None, 1, 1, 1) == (None, "n/a")
    assert pricing.cost_usd("no-such-model", 1, 1, 1) == (None, "n/a")


def test_flash_off_arithmetic():
    cost, tier = pricing.cost_usd(
        "deepseek-flash", 1_000_000, 1_000_000, 1_000_000, _dt(SATURDAY, 2, 0)
    )
    assert tier == "off"
    assert cost == 0.753


def test_next_offpeak():
    assert pricing.next_offpeak(_dt(MONDAY, 2, 0)) == _dt(MONDAY, 4, 0)
    assert pricing.next_offpeak(_dt(MONDAY, 6, 30)) == _dt(MONDAY, 10, 0)
    assert pricing.next_offpeak(_dt(MONDAY, 9, 30)) == _dt(MONDAY, 10, 0)
    assert pricing.next_offpeak(_dt(MONDAY, 4, 30)) == _dt(MONDAY, 4, 30)
    friday = (2026, 11, 6)
    assert pricing.next_offpeak(_dt(friday, 10, 30)) == _dt(friday, 10, 30)
    assert pricing.next_offpeak(_dt(SATURDAY, 2, 0)) == _dt(SATURDAY, 2, 0)


class PriceLlm(LlmProvider):
    def __init__(self, usage: dict):
        self.provider_id = "price-prov"
        self.provider_class = "free_tier_reasoning"
        self.model = "deepseek-flash"
        self.usage = usage

    async def check_connectivity(self, timeout_sec):
        return True

    async def chat(self, messages, tools, timeout_sec):
        return LlmResponse(text="ok", usage=dict(self.usage))


def _rows() -> list[m.LlmUsage]:
    with session_scope() as db:
        return list(db.execute(select(m.LlmUsage)).scalars())


@pytest.mark.anyio
async def test_ledger_row_gets_cost_and_tier(monkeypatch):
    monkeypatch.setattr(pricing, "_utcnow", lambda: _dt(SATURDAY, 2, 0))
    router = ProviderRouter()
    router.register(PriceLlm({"prompt_tokens": 2_000_000, "completion_tokens": 1_000_000,
                              "prompt_cache_hit_tokens": 1_000_000, "prompt_cache_miss_tokens": 1_000_000}))

    await router.route_chat("free_tier_reasoning", [], [], purpose="chat")

    row = _rows()[0]
    assert row.cost_usd == 0.753 and row.tier == "off"


@pytest.mark.anyio
async def test_missing_cache_fields_count_as_miss(monkeypatch):
    monkeypatch.setattr(pricing, "_utcnow", lambda: _dt(SATURDAY, 2, 0))
    router = ProviderRouter()
    router.register(PriceLlm({"prompt_tokens": 1_000_000, "completion_tokens": 0}))

    await router.route_chat("free_tier_reasoning", [], [], purpose="chat")

    row = _rows()[0]
    assert row.cost_usd == 0.15 and row.tier == "off"
    assert (row.prompt_hit, row.prompt_miss) == (None, None)
