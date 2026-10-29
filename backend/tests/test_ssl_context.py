"""SSL-1: urllib must trust certifi's CA bundle, and a test CA can replace it.

Behavioural: a real local HTTPS server with a certificate signed by a test CA.
With certifi.where pointed at the test CA the request succeeds; with the real
bundle the same certificate is rejected (CERTIFICATE_VERIFY_FAILED).
"""
from __future__ import annotations

import builtins
import contextlib
import http.server
import logging
import ssl
import threading
import urllib.error
import urllib.request
from pathlib import Path

import certifi
import pytest

from aria.http_utils import _ssl_context, _ssl_context_for

TLS = Path(__file__).resolve().parent / "fixtures" / "tls"
CA_PEM = TLS / "test-ca.pem"
REAL_BUNDLE = certifi.where()


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@contextlib.contextmanager
def _https_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(TLS / "test-server.pem", TLS / "test-server.key")
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=5, context=_ssl_context()) as resp:
        return resp.read()


def test_context_still_verifies_certificates():
    ctx = _ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_test_ca_via_certifi_is_trusted(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setattr(certifi, "where", lambda: str(CA_PEM))
    with _https_server() as url:
        assert _get(url) == b"ok"


def test_unknown_ca_is_rejected(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setattr(certifi, "where", lambda: REAL_BUNDLE)
    with _https_server() as url:
        with pytest.raises(urllib.error.URLError) as exc:
            _get(url)
    assert isinstance(exc.value.reason, ssl.SSLCertVerificationError)


def test_ssl_cert_file_wins_over_certifi(monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", str(CA_PEM))
    monkeypatch.setattr(certifi, "where", lambda: "/nonexistent/cacert.pem")
    with _https_server() as url:
        assert _get(url) == b"ok"


def test_missing_certifi_falls_back_with_warning(monkeypatch, caplog):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "certifi":
            raise ImportError("certifi is not installed")
        return real_import(name, *args, **kwargs)

    _ssl_context_for.cache_clear()
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    with caplog.at_level(logging.WARNING, logger="aria.http_utils"):
        ctx = _ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert any("certifi" in record.message for record in caplog.records)
