"""Contract: no HTTP route answers 500 (or crashes) on empty/garbage bodies or unknown ids.

Every route from /openapi.json is called with a token, several malformed bodies and fresh random ids.
Allowed 5xx: 501 (honest not_implemented stubs) and 503 (service not configured, e.g. no model key).
Anything else >= 500 is a bug; new routes are covered automatically.
"""
from __future__ import annotations

import re
import socket
import uuid

from fastapi.testclient import TestClient

from aria.api.auth import token_store
from aria.main import app

SKIP = ("shutdown", "restart", "kill", "open-external", "open_external", "/aria/update", "uninstall",
        "/system/clean", "sandbox/open")
BODIES = (None, {}, {"x": 1}, {"path": 5, "name": None, "content": [1], "id": [], "text": 7}, [1, 2], "str")
ALLOWED_5XX = (501, 503)


def _h():
    if not token_store.current_token():
        token_store.issue()
    return {"X-Local-Agent-Token": token_store.current_token()}


def test_no_route_answers_500_on_garbage_input(monkeypatch):
    def blocked(*a, **k):
        raise OSError("network disabled in tests")

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    client = TestClient(app, raise_server_exceptions=False)
    bad: dict[tuple[str, str], str] = {}
    sent = 0
    for path, ops in sorted(app.openapi()["paths"].items()):
        if any(s in path for s in SKIP):
            continue
        for method in ops:
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            url = path
            for seg in set(re.findall(r"\{(\w+)\}", path)):
                url = url.replace("{%s}" % seg, str(uuid.uuid4()) if "id" in seg else "x")
            for body in BODIES:
                kw: dict = {"headers": _h()}
                if method != "get" and body is not None:
                    kw["json"] = body
                try:
                    r = client.request(method.upper(), url, **kw)
                except Exception as exc:
                    bad.setdefault((method.upper(), path), f"EXC {type(exc).__name__}: {str(exc)[:80]}")
                    continue
                sent += 1
                if r.status_code >= 500 and r.status_code not in ALLOWED_5XX:
                    bad.setdefault((method.upper(), path), f"{r.status_code} body={str(body)[:40]} -> {r.text[:60]}")
    assert sent > 500, "fuzz did not cover the API"
    assert not bad, "routes with 5xx on malformed input:\n" + "\n".join(f"{k}: {v}" for k, v in bad.items())
