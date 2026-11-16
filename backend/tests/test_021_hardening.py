"""0.2.1 hardening: dev-mode auth, default hard stop, skill write validation."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from aria.api import auth as auth_mod
from aria.api.auth import require_runtime_token
from aria.core.guardrails import GuardrailConfig
from aria.routers import skills as skills_router_mod
from aria.routers.skills import router as skills_router


def test_dev_mode_skips_token_only_on_loopback(monkeypatch):
    fake = SimpleNamespace(aria_dev_mode=True, http_host="0.0.0.0")
    monkeypatch.setattr(auth_mod, "get_settings", lambda: fake)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(require_runtime_token(None))
    assert exc.value.status_code == 401

    fake.http_host = "127.0.0.1"
    assert asyncio.run(require_runtime_token(None)) == ""


def test_hard_stop_is_enabled_by_default():
    config = GuardrailConfig()
    assert config.hard_stop_enabled is True
    assert config.same_tool_failure_halt_after > 0


@pytest.fixture
def client(tmp_path, monkeypatch):
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setattr(skills_router_mod, "SKILLS_ROOT", skills)
    app = FastAPI()
    app.include_router(skills_router)
    app.dependency_overrides[require_runtime_token] = lambda: "test"
    c = TestClient(app)
    c.skills = skills
    yield c
    from aria.db.base import session_scope
    from aria.db.models import SkillMeta

    with session_scope() as db:
        db.query(SkillMeta).filter(SkillMeta.skill_name.in_(["zzq-bad", "zzq-big", "zzq-ok"])).delete(
            synchronize_session=False
        )


def test_create_skill_rejects_dangerous_content(client):
    body = {"name": "zzq-bad", "content": "---\nname: zzq-bad\ndescription: x\n---\n\nos.system('echo pwn')\n"}
    r = client.post("/skills", json=body)
    assert r.status_code == 400 and "dangerous" in r.json()["detail"]
    assert not (client.skills / "zzq-bad").exists()


def test_create_skill_rejects_broken_frontmatter(client):
    r = client.post("/skills", json={"name": "zzq-bad", "content": "---\nname: [oops\n---\nbody"})
    assert r.status_code == 400 and "frontmatter" in r.json()["detail"]


def test_create_skill_rejects_empty_content(client):
    r = client.post("/skills", json={"name": "zzq-bad", "content": "   "})
    assert r.status_code == 400


def test_update_skill_rejects_oversized_content(client):
    assert client.post("/skills", json={"name": "zzq-ok", "content": "---\nname: zzq-ok\n---\nbody"}).status_code == 200
    r = client.put("/skills/content", json={"name": "zzq-ok", "content": "x" * 200_001})
    assert r.status_code == 413
    assert (client.skills / "zzq-ok" / "SKILL.md").read_text(encoding="utf-8").startswith("---")
