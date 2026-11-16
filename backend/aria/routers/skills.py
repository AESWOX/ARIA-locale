"""Skills routes: /api/skills (CRUD) + /api/skills/hub/*.

Skills are stored as bundle directories on disk — one folder per skill with
a SKILL.md entry point — while ``skills_meta`` is the registry for status,
category and usage analytics. The hub is functional for the local source
(the on-disk skills root); community/GitHub sources are declared but return
no results, so the SkillsPage renders and installs/uninstalls locally.
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile

from aria import paths
from aria.api.auth import require_runtime_token
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import SkillStatus
from aria.routers import actions as actions_module

router = APIRouter(tags=["skills"])

SKILLS_ROOT = paths.skills_dir()

# Hub identifiers use "source:name" (e.g. "local:web-research"). The local
# and aria-index sources both point at SKILLS_ROOT.
_SOURCES = {
    "local": "Local skills",
    "aria-index": "aria-index",
    "github": "GitHub",
}


def _status_enabled(row: m.SkillMeta) -> bool:
    return row.status not in (SkillStatus.archived, SkillStatus.rejected)


def _skill_dir(name: str) -> Path:
    safe = Path(name).name
    if safe != name or not safe or safe in (".", ".."):
        raise HTTPException(status_code=400, detail=f"invalid skill name: {name}")
    return SKILLS_ROOT / safe


def _read_description(name: str) -> str:
    try:
        d = _skill_dir(name)
    except HTTPException:
        # Некорректное имя из БД (например, legacy путь "cat/skill/SKILL")
        # не должно ронять весь список — описание просто пустое.
        return ""
    md = d / "SKILL.md"
    if not md.exists():
        return ""
    try:
        text = md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = re.search(r"(?im)^description:\s*(.+)$", text)
    return match.group(1).strip() if match else ""


def _serialize(row: m.SkillMeta) -> dict[str, Any]:
    return {
        "name": row.skill_name,
        "description": _read_description(row.skill_name),
        "category": row.category or "general",
        "enabled": _status_enabled(row),
    }


def _installed_map(db) -> dict[str, dict[str, str | None]]:
    """identifier -> installed lock entry (for hub "already installed" badges)."""
    result: dict[str, dict[str, str | None]] = {}
    for row in repo.list_skills(db):
        result[f"local:{row.skill_name}"] = {
            "name": row.skill_name,
            "trust_level": "local",
            "scan_verdict": "safe",
        }
    return result


def _hub_result(name: str, description: str, source: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "source": source,
        "identifier": f"{source}:{name}",
        "trust_level": "local" if source != "github" else "community",
        "repo": None,
        "tags": [],
    }


def _local_results() -> list[dict[str, Any]]:
    if not SKILLS_ROOT.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(SKILLS_ROOT.iterdir()):
        if not path.is_dir() or not (path / "SKILL.md").exists():
            continue
        md = (path / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        desc_match = re.search(r"(?im)^description:\s*(.+)$", md)
        out.append(
            _hub_result(
                path.name,
                desc_match.group(1).strip() if desc_match else "",
                "local",
            )
        )
    return out


def _resolve_identifier(identifier: str) -> tuple[str, Path]:
    name = identifier
    if ":" in identifier:
        source, _, name = identifier.partition(":")
        if source not in _SOURCES:
            raise HTTPException(status_code=400, detail=f"unknown source: {source}")
    d = _skill_dir(name)
    if not (d / "SKILL.md").exists():
        raise HTTPException(status_code=404, detail=f"skill not found: {name}")
    return name, d


# ─────────────────────────────────────────────────────────────── core CRUD

@router.get("/skills")
async def list_skills(
    _: str = Depends(require_runtime_token),
    profile: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    with session_scope() as db:
        rows = repo.list_skills(db)
        return [row for row in (_serialize(r) for r in rows)]


@router.put("/skills/toggle")
async def toggle_skill(
    body: dict[str, Any],
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    name = (body.get("name") or "").strip()
    enabled = bool(body.get("enabled"))
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    with session_scope() as db:
        skill = repo.get_skill(db, name)
        if skill is None:
            raise HTTPException(status_code=404, detail=f"skill not found: {name}")
        skill.status = SkillStatus.active if enabled else SkillStatus.archived
    return {"ok": True, "name": name, "enabled": enabled}


@router.get("/skills/content")
async def get_skill_content(
    name: str = Query(...),
    _: str = Depends(require_runtime_token),
    profile: str | None = Query(default=None),
) -> dict[str, Any]:
    d = _skill_dir(name)
    md = d / "SKILL.md"
    if not md.exists():
        raise HTTPException(status_code=404, detail=f"no content for skill: {name}")
    content = md.read_text(encoding="utf-8", errors="replace")
    return {"name": name, "content": content, "path": str(md)}


@router.post("/skills")
async def create_skill(
    body: dict[str, Any],
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    name = (body.get("name") or "").strip()
    content = body.get("content") or ""
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    d = _skill_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    md = d / "SKILL.md"
    md.write_text(content, encoding="utf-8")
    with session_scope() as db:
        repo.upsert_skill(
            db,
            name,
            category=(body.get("category") or "general").strip() or "general",
            status=SkillStatus.active,
            source_origin="manual",
        )
    return {"success": True, "message": f"created {name}", "path": str(md)}


_MAX_ZIP_BYTES = 25 * 1024 * 1024
_MAX_UNPACKED_BYTES = 64 * 1024 * 1024
_MAX_ZIP_ENTRIES = 1000
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _safe_zip_path(name: str) -> bool:
    """Reject absolute paths, drive letters and `..` segments (zip-slip)."""
    normalized = name.replace("\\", "/")
    if not normalized or normalized.startswith("/"):
        return False
    if len(normalized) > 1 and normalized[1] == ":":
        return False
    return all(part not in ("", ".", "..") for part in normalized.split("/"))


def _import_skills_from_zip(data: bytes) -> tuple[list[str], list[str]]:
    """Install every `<dir>/SKILL.md` bundle from the archive. Returns (imported, skipped)."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="not a zip archive") from exc
    infos = archive.infolist()
    if len(infos) > _MAX_ZIP_ENTRIES:
        raise HTTPException(status_code=400, detail=f"zip has more than {_MAX_ZIP_ENTRIES} entries")
    if sum(info.file_size for info in infos) > _MAX_UNPACKED_BYTES:
        raise HTTPException(status_code=413, detail="unpacked size is larger than 64 MB")
    for info in infos:
        if ((info.external_attr >> 16) & 0o170000) == 0o120000:
            raise HTTPException(status_code=400, detail=f"symlink in zip: {info.filename}")
        if not _safe_zip_path(info.filename):
            raise HTTPException(status_code=400, detail=f"unsafe path in zip: {info.filename}")

    bundles: dict[str, Path] = {}
    for info in infos:
        if info.is_dir():
            continue
        parts = Path(info.filename.replace("\\", "/")).parts
        if len(parts) < 2 or parts[-1] != "SKILL.md" or not _SKILL_NAME_RE.match(parts[-2]):
            continue
        bundles.setdefault(parts[-2], Path(*parts[:-1]))

    if not bundles:
        raise HTTPException(status_code=400, detail="no SKILL.md found in the archive")

    SKILLS_ROOT.mkdir(parents=True, exist_ok=True)
    imported: list[str] = []
    skipped: list[str] = []
    with session_scope() as db:
        for name, prefix in sorted(bundles.items()):
            dest = SKILLS_ROOT / name
            if dest.exists():
                skipped.append(name)
                continue
            for info in infos:
                if info.is_dir():
                    continue
                rel_path = Path(info.filename.replace("\\", "/"))
                try:
                    sub = rel_path.relative_to(prefix)
                except ValueError:
                    continue
                target = dest / sub
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(info))
            repo.upsert_skill(
                db,
                name,
                category="imported",
                status=SkillStatus.active,
                source_origin="import",
            )
            imported.append(name)
    return imported, skipped


@router.post("/skills/import")
async def import_skills(
    file: UploadFile = File(...),
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    """Import skill bundles from a .zip: every folder with SKILL.md becomes a skill."""
    data = await file.read(_MAX_ZIP_BYTES + 1)
    if len(data) > _MAX_ZIP_BYTES:
        raise HTTPException(status_code=413, detail="zip is larger than 25 MB")
    imported, skipped = _import_skills_from_zip(data)
    return {"ok": True, "imported": imported, "skipped": skipped}


@router.put("/skills/content")
async def update_skill_content(
    body: dict[str, Any],
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    name = (body.get("name") or "").strip()
    content = body.get("content")
    if not name or content is None:
        raise HTTPException(status_code=400, detail="name and content are required")
    d = _skill_dir(name)
    md = d / "SKILL.md"
    if not md.exists():
        raise HTTPException(status_code=404, detail=f"skill not found: {name}")
    md.write_text(content, encoding="utf-8")
    return {"success": True, "message": f"updated {name}", "path": str(md)}


# ─────────────────────────────────────────────────────────────── skills hub

@router.get("/skills/hub/sources")
async def hub_sources(
    _: str = Depends(require_runtime_token),
    profile: str | None = Query(default=None),
) -> dict[str, Any]:
    with session_scope() as db:
        installed = _installed_map(db)
    local_available = SKILLS_ROOT.exists() and any(SKILLS_ROOT.iterdir())
    return {
        "sources": [
            {"id": "local", "label": "Local skills", "available": local_available},
            {"id": "aria-index", "label": "aria-index", "available": local_available},
            {"id": "github", "label": "GitHub", "rate_limited": False},
        ],
        "index_available": local_available,
        "featured": _local_results()[:6],
        "installed": installed,
    }


@router.get("/skills/hub/search")
async def hub_search(
    q: str = Query(default=""),
    source: str = Query(default="all"),
    limit: int = Query(default=20),
    _: str = Depends(require_runtime_token),
    profile: str | None = Query(default=None),
) -> dict[str, Any]:
    results = _local_results()
    if q:
        needle = q.lower()
        results = [r for r in results if needle in r["name"].lower() or needle in r["description"].lower()]
    results = results[:max(0, limit)]
    with session_scope() as db:
        installed = _installed_map(db)
    return {
        "results": results,
        "source_counts": {"local": len(results)},
        "timed_out": [],
        "installed": installed,
    }


@router.get("/skills/hub/preview")
async def hub_preview(
    identifier: str = Query(...),
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    name, d = _resolve_identifier(identifier)
    md = d / "SKILL.md"
    skill_md = md.read_text(encoding="utf-8", errors="replace")
    files = sorted(str(p.relative_to(d)).replace("\\", "/") for p in d.rglob("*") if p.is_file())
    return {
        "name": name,
        "description": _read_description(name),
        "source": identifier.partition(":")[0] if ":" in identifier else "local",
        "identifier": identifier,
        "trust_level": "local",
        "repo": None,
        "tags": [],
        "skill_md": skill_md,
        "files": files,
    }


@router.post("/skills/hub/install")
async def hub_install(
    body: dict[str, Any],
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    identifier = (body.get("identifier") or "").strip()
    if not identifier:
        raise HTTPException(status_code=400, detail="identifier is required")
    source, sep, name = identifier.partition(":")
    if not sep:
        name = identifier
    d = _skill_dir(name)
    if not (d / "SKILL.md").exists():
        return {"name": "skills-hub-install", "ok": False, "pid": None,
                "error": f"skill not found locally: {name}"}
    with session_scope() as db:
        repo.upsert_skill(db, name, status=SkillStatus.active, source_origin="hub")
    return {"name": "skills-hub-install", "ok": True, "pid": None,
            "message": f"installed {name} from {source}"}


@router.post("/skills/hub/uninstall")
async def hub_uninstall(
    body: dict[str, Any],
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    with session_scope() as db:
        skill = repo.get_skill(db, name)
        if skill is None:
            return {"name": "skills-hub-uninstall", "ok": False, "pid": None,
                    "error": f"skill not found: {name}"}
        skill.status = SkillStatus.archived
    return {"name": "skills-hub-uninstall", "ok": True, "pid": None,
            "message": f"uninstalled {name}"}


@router.post("/skills/hub/update")
async def hub_update(
    body: dict[str, Any],
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    synced = 0
    for r in _local_results():
        name = r["name"]
        with session_scope() as db:
            if repo.get_skill(db, name) is None:
                repo.upsert_skill(db, name, status=SkillStatus.active, source_origin="hub")
                synced += 1
    message = f"synced {synced} new local skill(s)"
    actions_module.record("skills-hub-update", 0, [message])
    return {"name": "skills-hub-update", "ok": True, "pid": None, "message": message}


# Heuristic scanner: dangerous command-substitution / execution patterns.
_SCAN_PATTERNS: list[tuple[str, str]] = [
    ("high", r"\b(os\.system|subprocess\.(run|Popen|call)|eval\s*\(|exec\s*\()"),
    ("high", r"\bcurl\b.*\|\s*(bash|sh)|wget\b.*\|\s*(bash|sh)"),
    ("high", r"\brm\s+-rf\b|del\s+/[fqs].*\/s"),
    ("medium", r"\bchmod\s+\+x\b|base64\s+-d|/bin/sh\s+-c"),
    ("medium", r"\bssh\s+(-o|root@)|scp\b|rsync\b"),
]


@router.get("/skills/hub/scan")
async def hub_scan(
    identifier: str = Query(...),
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    name, d = _resolve_identifier(identifier)
    findings: list[dict[str, Any]] = []
    severity_score = {"high": 0, "medium": 0}
    for path in sorted(d.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(d)).replace("\\", "/")
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, 1):
            for severity, pattern in _SCAN_PATTERNS:
                if re.search(pattern, line):
                    findings.append({
                        "severity": severity,
                        "category": "command-execution" if severity == "high" else "suspicious-shell",
                        "file": rel,
                        "line": lineno,
                        "description": line.strip()[:160],
                    })
                    severity_score[severity] += 1
    if severity_score["high"]:
        verdict = "dangerous"
    elif severity_score["medium"]:
        verdict = "caution"
    else:
        verdict = "safe"

    if verdict == "safe":
        policy, policy_reason = "allow", "No dangerous or suspicious patterns detected."
    elif verdict == "dangerous":
        policy, policy_reason = "block", "Dangerous command-execution patterns found."
    else:
        policy, policy_reason = "ask", "Suspicious shell patterns found; review before install."

    summary = f"{len(findings)} finding(s): {severity_score['high']} high, {severity_score['medium']} medium"
    return {
        "name": name,
        "identifier": identifier,
        "source": identifier.partition(":")[0] if ":" in identifier else "local",
        "trust_level": "local",
        "verdict": verdict,
        "summary": summary,
        "policy": policy,
        "policy_reason": policy_reason,
        "findings": findings,
        "severity_counts": severity_score,
    }
