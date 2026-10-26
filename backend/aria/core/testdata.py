"""«Clean test data»: убирает следы smoke-прогонов из реального профиля.

Smoke (`scripts/e2e_smoke.py`) ходит в установленное приложение и создаёт навыки `e2e<hex>-hello`,
сессии «e2e shell approval», «e2e mcp approval», «PONG», «smoke-test colour» и MCP-сервер `e2e-smoke`.
Если прогон оборвался или шёл с --no-cleanup, они остаются в Skills и в списке чатов.
Удаляется только то, что узнаётся по этим шаблонам; всё остальное не трогается.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from aria import paths
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope

E2E_SKILL_RE = re.compile(r"^e2e[0-9a-f]{6}-")
_SESSION_TITLE_RES = (
    re.compile(r"^\s*e2e (shell|mcp) approval\s*$", re.I),
    re.compile(r"^\s*reply with exactly the single word pong", re.I),
    re.compile(r"smoke-test colou?r", re.I),
)


def is_test_skill(name: str | None) -> bool:
    return bool(name and E2E_SKILL_RE.match(name))


def is_test_session_title(title: str | None) -> bool:
    return bool(title and any(rx.search(title) for rx in _SESSION_TITLE_RES))


def is_test_server(name: str | None) -> bool:
    return bool(name and name.startswith("e2e-"))


def preview_test_data() -> dict[str, Any]:
    """Что будет удалено, ничего не меняя."""
    skills: list[str] = []
    skills_root = paths.skills_dir()
    with session_scope() as db:
        skills = [r.skill_name for r in repo.list_skills(db) if is_test_skill(r.skill_name)]
        sessions = [s.title for s in db.query(m.Session).all() if is_test_session_title(s.title)]
    if skills_root.is_dir():
        for folder in skills_root.iterdir():
            if folder.is_dir() and is_test_skill(folder.name) and folder.name not in skills:
                skills.append(folder.name)
    servers: list[str] = []
    try:
        from aria.mcp.manager import load_servers

        servers = [s.get("name") for s in load_servers() if is_test_server(s.get("name"))]
    except Exception:  # noqa: BLE001
        pass
    return {"skills": skills, "sessions": len(sessions), "mcp_servers": servers}


def backup_database() -> str | None:
    """Копия БД рядом с ней (sqlite backup API, безопасно при WAL). None, если БД не файл."""
    import sqlite3
    import time

    src = paths.user_data_dir() / "local_agent.db"
    if not src.is_file():
        return None
    dst = src.with_name(f"local_agent.before-clean-{time.strftime('%Y%m%d-%H%M%S')}.db")
    with sqlite3.connect(str(src)) as s, sqlite3.connect(str(dst)) as d:
        s.backup(d)
    return str(dst)


def clean_test_data(*, backup: bool = True) -> dict[str, Any]:
    plan = preview_test_data()
    backup_path: str | None = None
    if backup and (plan["skills"] or plan["sessions"] or plan["mcp_servers"]):
        try:
            backup_path = backup_database()
        except Exception:  # noqa: BLE001 - нет копии = не удаляем сессии (необратимо)
            return {"skills": [], "sessions": 0, "mcp_servers": [], "backup": None,
                    "error": "backup failed, nothing was deleted"}
    skills_removed: list[str] = []
    sessions_removed = 0
    skills_root = paths.skills_dir()
    with session_scope() as db:
        for row in repo.list_skills(db):
            if is_test_skill(row.skill_name):
                folder = skills_root / Path(row.skill_name).name
                if folder.is_dir():
                    shutil.rmtree(folder, ignore_errors=True)
                skills_removed.append(row.skill_name)
                db.delete(row)
    # папки без строки в БД (оборванный импорт плагина)
    if skills_root.is_dir():
        for folder in skills_root.iterdir():
            if folder.is_dir() and is_test_skill(folder.name):
                shutil.rmtree(folder, ignore_errors=True)
                if folder.name not in skills_removed:
                    skills_removed.append(folder.name)
    with session_scope() as db:
        ids = [s.id for s in db.query(m.Session).all() if is_test_session_title(s.title)]
        for sid in ids:
            if repo.delete_session(db, sid):
                sessions_removed += 1
    servers_removed: list[str] = []
    try:
        from aria.mcp.manager import load_servers, save_servers

        current = load_servers()
        keep = [s for s in current if not is_test_server(s.get("name"))]
        if len(keep) != len(current):
            servers_removed = [s.get("name") for s in current if is_test_server(s.get("name"))]
            save_servers(keep)
    except Exception:  # noqa: BLE001 - MCP-реестр не должен ломать чистку остального
        pass
    return {"skills": skills_removed, "sessions": sessions_removed, "mcp_servers": servers_removed, "backup": backup_path}
