"""E5: стабильный префикс промпта/инструментов и cache_hit_ratio из llm_usage."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.core import loop
from aria.db import models as m
from aria.db.base import session_scope
from aria.db.enums import SourceTrust
from aria.routers import chat as chat_mod

BACKEND = Path(__file__).resolve().parents[1]
WHITELIST = ("file_read", "file_write", "shell_execute")


def _msg(role: str, content: str) -> SimpleNamespace:
    return SimpleNamespace(role=role, content=content, source_trust=SourceTrust.trusted)


def _serialize(schemas: list[dict]) -> str:
    return json.dumps(schemas, sort_keys=True, ensure_ascii=False)


def test_system_prefix_stable_when_history_grows():
    prompt = "Ты тестовый агент." + loop._load_persona()
    first = loop._build_messages(prompt, [_msg("user", "один")])
    grown = loop._build_messages(prompt, [_msg("user", "один"), _msg("user", "два")])

    assert first[0].content == grown[0].content == prompt
    snapshot = _serialize(loop._tool_schemas_for_role(WHITELIST))
    assert _serialize(loop._tool_schemas_for_role(WHITELIST)) == snapshot


def test_tool_order_follows_whitelist():
    schemas = loop._tool_schemas_for_role(WHITELIST)
    assert [s["function"]["name"] for s in schemas] == list(WHITELIST)


SUBPROCESS_SRC = """
import hashlib, json
from aria.core.loop import _build_messages, _tool_schemas_for_role, _load_persona
prompt = "Ты тестовый агент." + _load_persona()
schemas = _tool_schemas_for_role(("file_read", "file_write", "shell_execute"))
msgs = _build_messages(prompt, [])
payload = json.dumps({"p": prompt, "s": schemas, "m": [x.content for x in msgs]},
                     sort_keys=True, ensure_ascii=False)
print(hashlib.sha256(payload.encode("utf-8")).hexdigest())
"""


def _subprocess_hash(tmp_path: Path) -> str:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BACKEND)
    env["PYTHONIOENCODING"] = "utf-8"
    env["ARIA_DATA_DIR"] = str(tmp_path / f"aria-{uuid.uuid4().hex[:6]}")
    result = subprocess.run(
        [sys.executable, "-c", SUBPROCESS_SRC],
        cwd=str(BACKEND),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )
    return result.stdout.strip()


def test_prefix_is_process_independent(tmp_path):
    assert _subprocess_hash(tmp_path) == _subprocess_hash(tmp_path)


def test_no_dynamic_values_in_system_text():
    forbidden = ("datetime.now", "time.time", "uuid", "random")
    texts = [
        (BACKEND / "aria" / "prompts").glob("*.md"),
    ]
    prompt_texts = [p.read_text(encoding="utf-8") for p in texts[0]]
    prompt_texts += [chat_mod.SYSTEM_PROMPT, loop._load_persona()]
    loop_lines = (BACKEND / "aria" / "core" / "loop.py").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(loop_lines) if "role_prompt =" in line)
    stop = next(i for i, line in enumerate(loop_lines[start:], start=start) if "iterations = 0" in line)
    prompt_texts.append("\n".join(loop_lines[start:stop]))
    prompt_texts.append((BACKEND / "aria" / "core" / "roles.py").read_text(encoding="utf-8"))
    prompt_texts.append((BACKEND / "aria" / "core" / "runprofile.py").read_text(encoding="utf-8"))
    for text in prompt_texts:
        for token in forbidden:
            assert token not in text, f"нестабильный маркер {token} в системном тексте"


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(chat_mod.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    app.state.router = None
    return TestClient(app)


def test_cache_hit_ratio_in_usage():
    client = _client()
    sid = client.post("/chat/sessions", json={}).json()["session_id"]
    with session_scope() as db:
        db.add(
            m.LlmUsage(
                session_id=uuid.UUID(sid),
                provider_id="deepseek-chat",
                model="deepseek-flash",
                purpose="chat",
                prompt_hit=700,
                prompt_miss=300,
                prompt_total=1000,
                completion=10,
                cost_usd=0.001,
                tier="off",
            )
        )
        db.add(
            m.LlmUsage(
                session_id=uuid.UUID(sid),
                provider_id="gemini",
                model="gemini-flash",
                purpose="chat",
                prompt_total=50,
                completion=5,
                cost_usd=None,
                tier="n/a",
            )
        )

    body = client.get(f"/chat/sessions/{sid}/usage").json()

    assert body["cache_hit_ratio"] == 0.7


def test_cache_hit_ratio_none_without_cache_data():
    client = _client()
    sid = client.post("/chat/sessions", json={}).json()["session_id"]
    assert client.get(f"/chat/sessions/{sid}/usage").json()["cache_hit_ratio"] is None
