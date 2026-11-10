"""F-010: web_fetch понимает gzip/deflate, JSON, и честно говорит про JS-страницу."""
from __future__ import annotations

import gzip
import io
import zlib
from email.message import Message

import pytest
from aria.tools.handlers import web


class FakeResp:
    def __init__(self, body: bytes, headers: Message, url: str = "https://example.com/page"):
        self._stream = io.BytesIO(body)
        self.headers = headers
        self.status = 200
        self._url = url

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _headers(content_type: str, encoding: str | None = None) -> Message:
    headers = Message()
    headers["Content-Type"] = content_type
    if encoding:
        headers["Content-Encoding"] = encoding
    return headers


@pytest.fixture
def no_dns(monkeypatch):
    monkeypatch.setattr(web, "validate_public_url", lambda url: url)


def _fetch(monkeypatch, body: bytes, headers: Message) -> dict:
    monkeypatch.setattr(web, "_open_url", lambda req, timeout: FakeResp(body, headers))
    return web._fetch_blocking("https://example.com/page", 5000)


def test_gzip_body_is_decoded(no_dns, monkeypatch):
    out = _fetch(monkeypatch, gzip.compress(b"hello from gzip"), _headers("text/plain; charset=utf-8", "gzip"))

    assert "error" not in out
    assert out["content"] == "hello from gzip"
    assert out["content_type"] == "text/plain"


def test_deflate_body_is_decoded(no_dns, monkeypatch):
    out = _fetch(monkeypatch, zlib.compress(b"hello deflate"), _headers("text/plain; charset=utf-8", "deflate"))

    assert "error" not in out
    assert out["content"] == "hello deflate"


def test_raw_deflate_body_is_decoded(no_dns, monkeypatch):
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    body = compressor.compress(b"raw deflate") + compressor.flush()

    out = _fetch(monkeypatch, body, _headers("text/plain; charset=utf-8", "deflate"))

    assert "error" not in out
    assert out["content"] == "raw deflate"


def test_json_content_type_is_readable(no_dns, monkeypatch):
    out = _fetch(monkeypatch, b'{"weather": "sunny", "temp": 21}', _headers("application/json"))

    assert "error" not in out
    assert out["content"] == '{"weather": "sunny", "temp": 21}'


def test_javascript_application_type_is_readable(no_dns, monkeypatch):
    out = _fetch(monkeypatch, b"console.log('x')", _headers("application/javascript"))

    assert "error" not in out
    assert out["content"] == "console.log('x')"


def test_js_rendered_page_is_reported_honestly(no_dns, monkeypatch):
    html = (
        "<html><head><script src='a.js'></script><script>b.js</script>"
        "<script>c()</script></head><body><div id='root'></div></body></html>"
    )

    out = _fetch(monkeypatch, html.encode("utf-8"), _headers("text/html; charset=utf-8"))

    assert "error" not in out
    assert out["js_rendered"] is True
    assert out["content"] == ""
    assert "JavaScript" in out["note"]


def test_accept_encoding_never_promises_brotli_without_decoder():
    value = web.accept_encoding()

    assert "gzip" in value and "deflate" in value
    if not web._brotli_available():
        assert "br" not in value
