"""E4: баланс DeepSeek — нормальный разбор, ошибки, кэш, отсутствие ключа в логах."""
from __future__ import annotations

import logging

import httpx
import pytest

from aria.llm import balance


class _Resp:
    def __init__(self, status: int = 200, payload=None, *, bad_json: bool = False, body: bytes | None = None):
        self.status_code = status
        self._payload = payload
        self._bad_json = bad_json
        self.content = body if body is not None else b"{}"

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


class _Client:
    def __init__(self, resp=None, exc: Exception | None = None):
        self.resp = resp
        self.exc = exc
        self.requests = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, headers=None):
        self.requests += 1
        assert "Authorization" in (headers or {})
        if self.exc is not None:
            raise self.exc
        return self.resp


@pytest.fixture(autouse=True)
def _fresh_cache():
    balance.reset_cache()
    yield
    balance.reset_cache()


def _patch(monkeypatch, resp=None, exc: Exception | None = None) -> list[_Client]:
    made: list[_Client] = []

    def factory(*args, **kwargs):
        client = _Client(resp, exc)
        made.append(client)
        return client

    monkeypatch.setattr(balance.httpx, "AsyncClient", factory)
    monkeypatch.setattr(balance, "_api_key", lambda: "sk-test-key")
    return made


PAYLOAD = {
    "is_available": True,
    "balance_infos": [
        {"currency": "CNY", "total_balance": "10.00", "granted_balance": "1.00", "topped_up_balance": "9.00"},
        {"currency": "USD", "total_balance": "2.50", "granted_balance": "0.50", "topped_up_balance": "2.00"},
    ],
}


@pytest.mark.anyio
async def test_usd_balance_parsed(monkeypatch):
    _patch(monkeypatch, _Resp(payload=PAYLOAD))

    data = await balance.fetch_balance()

    assert data["configured"] is True and data["available"] is True
    assert data["currency"] == "USD"
    assert (data["total_balance"], data["granted_balance"], data["topped_up_balance"]) == (2.5, 0.5, 2.0)
    assert data["error"] is None and data["checked_at"]


@pytest.mark.anyio
async def test_cny_only_and_unavailable(monkeypatch):
    payload = {"is_available": False, "balance_infos": [{"currency": "CNY", "total_balance": "0", "granted_balance": "0", "topped_up_balance": "0"}]}
    _patch(monkeypatch, _Resp(payload=payload))

    data = await balance.fetch_balance()

    assert data["available"] is False and data["currency"] == "CNY"


@pytest.mark.anyio
async def test_empty_infos_and_non_numeric(monkeypatch):
    _patch(monkeypatch, _Resp(payload={"is_available": True, "balance_infos": []}))
    data = await balance.fetch_balance()
    assert data["available"] is True and data["currency"] is None and data["total_balance"] is None

    _patch(monkeypatch, _Resp(payload={"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": "N/A"}]}))
    data = await balance.fetch_balance()
    assert data["total_balance"] is None and data["error"] is None


@pytest.mark.anyio
@pytest.mark.parametrize("status,expected", [(401, "unauthorized"), (500, "HTTP 500")])
async def test_http_errors_are_honest(monkeypatch, status, expected):
    _patch(monkeypatch, _Resp(status=status))

    data = await balance.fetch_balance()

    assert data["configured"] is True and data["available"] is None
    assert data["error"] == expected


@pytest.mark.anyio
async def test_network_and_json_errors(monkeypatch):
    _patch(monkeypatch, exc=httpx.TimeoutException("slow"))
    assert (await balance.fetch_balance())["error"] == "network: TimeoutException"

    _patch(monkeypatch, exc=httpx.ConnectError("no dns"))
    assert (await balance.fetch_balance())["error"] == "network: ConnectError"

    _patch(monkeypatch, _Resp(bad_json=True, body=b"not-json"))
    assert (await balance.fetch_balance())["error"] == "invalid JSON"


@pytest.mark.anyio
async def test_response_size_limit(monkeypatch):
    _patch(monkeypatch, _Resp(body=b"x" * (balance.MAX_BODY_BYTES + 1)))

    assert (await balance.fetch_balance())["error"] == "response too large"


@pytest.mark.anyio
async def test_no_key(monkeypatch):
    monkeypatch.setattr(balance, "_api_key", lambda: None)
    data = await balance.fetch_balance()
    assert data["configured"] is False and data["available"] is None


@pytest.mark.anyio
async def test_cache_and_key_invalidation(monkeypatch):
    made = _patch(monkeypatch, _Resp(payload=PAYLOAD))
    await balance.fetch_balance()
    await balance.fetch_balance()
    assert len(made) == 1, "второй вызов должен брать кэш"

    monkeypatch.setattr(balance, "_api_key", lambda: "sk-other-key")
    await balance.fetch_balance()
    assert len(made) == 2, "смена ключа должна инвалидировать кэш"


@pytest.mark.anyio
async def test_key_never_appears_in_logs(monkeypatch, caplog):
    _patch(monkeypatch, _Resp(status=401))
    with caplog.at_level(logging.DEBUG):
        await balance.fetch_balance()

    assert "sk-test-key" not in caplog.text

@pytest.mark.anyio
async def test_errors_are_not_cached(monkeypatch):
    made = _patch(monkeypatch, _Resp(status=401))
    await balance.fetch_balance()
    await balance.fetch_balance()
    assert len(made) == 2, "?????? ?? ??????????"


@pytest.mark.anyio
async def test_unknown_currency_falls_back_to_first(monkeypatch):
    payload = {"is_available": True, "balance_infos": [{"currency": "EUR", "total_balance": "3.0", "granted_balance": "0", "topped_up_balance": "3.0"}]}
    _patch(monkeypatch, _Resp(payload=payload))

    data = await balance.fetch_balance()

    assert data["currency"] == "EUR" and data["total_balance"] == 3.0

