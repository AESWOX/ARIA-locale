"""Skills zip import: bundles with SKILL.md are installed, unsafe archives are rejected."""
import io
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.routers import skills as skills_router_mod
from aria.routers.skills import router as skills_router


def _zip(entries: dict[str, bytes], *, symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, payload in entries.items():
            zf.writestr(name, payload)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (0o120777 << 16) | 0o777
            zf.writestr(info, b"target")
    return buf.getvalue()


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
        db.query(SkillMeta).filter(SkillMeta.skill_name.in_(["zzq-zip", "zzq-skip"])).delete(
            synchronize_session=False
        )


def test_import_installs_every_skill_bundle(client):
    data = _zip(
        {
            "archify-main/.agents/skills/zzq-zip/SKILL.md": b"---\nname: zzq-zip\ndescription: demo\n---\n# Demo\n",
            "archify-main/.agents/skills/zzq-zip/scripts/run.py": b"print(1)\n",
            "archify-main/README.md": b"not a skill",
        }
    )
    r = client.post("/skills/import", files={"file": ("skills.zip", data, "application/zip")})
    assert r.status_code == 200, r.text
    assert r.json()["imported"] == ["zzq-zip"] and r.json()["skipped"] == []
    assert (client.skills / "zzq-zip" / "SKILL.md").is_file()
    assert (client.skills / "zzq-zip" / "scripts" / "run.py").is_file()
    assert not (client.skills / "archify-main").exists()
    listed = {row["name"] for row in client.get("/skills").json()}
    assert "zzq-zip" in listed


def test_import_skips_existing_skill_folder(client):
    (client.skills / "zzq-skip").mkdir()
    (client.skills / "zzq-skip" / "SKILL.md").write_text("mine", encoding="utf-8")
    data = _zip({"zzq-skip/SKILL.md": b"theirs"})
    body = client.post("/skills/import", files={"file": ("s.zip", data, "application/zip")}).json()
    assert body["imported"] == [] and body["skipped"] == ["zzq-skip"]
    assert (client.skills / "zzq-skip" / "SKILL.md").read_text(encoding="utf-8") == "mine"


def test_import_rejects_zip_slip(client):
    data = _zip({"../evil-zzq/SKILL.md": b"x"})
    r = client.post("/skills/import", files={"file": ("s.zip", data, "application/zip")})
    assert r.status_code == 400
    assert not (client.skills.parent / "evil-zzq").exists()


def test_import_rejects_symlink_entries(client):
    data = _zip({}, symlink="zzq-zip/link")
    r = client.post("/skills/import", files={"file": ("s.zip", data, "application/zip")})
    assert r.status_code == 400 and "symlink" in r.json()["detail"]


def test_import_requires_a_skill_md(client):
    data = _zip({"docs/readme.md": b"hello"})
    r = client.post("/skills/import", files={"file": ("s.zip", data, "application/zip")})
    assert r.status_code == 400 and "SKILL.md" in r.json()["detail"]
