"""Log tailing routes: /api/logs.

Serves the tail of the backend's file logs. Logical log names map to files
under ``backend/logs`` (where ``aria/main.py`` writes with a
``RotatingFileHandler``): ``agent`` -> backend.log, ``gateway`` -> gateway.log.
Unknown logical names fall back to backend.log. Lines are filtered by level
(cascading: ERROR < WARNING < INFO < DEBUG) and by component keyword.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from aria import paths as _paths
from aria.api.auth import require_runtime_token
from aria.logredact import redact
from aria.version import APP_VERSION

router = APIRouter(tags=["logs"])

_LOG_DIR = _paths.log_dir()

_LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}


_LEVEL_RE = re.compile(r"^\S+ \S+ (DEBUG|INFO|WARNING|ERROR|CRITICAL) ")


def _classify_line(line: str) -> str:
    # Уровень берём из поля levelname, а не ищем слово «error» в тексте (иначе обычные строки краснеют).
    m = _LEVEL_RE.match(line)
    if m:
        return "ERROR" if m.group(1) in ("ERROR", "CRITICAL") else m.group(1)
    upper = line.upper()
    if "ERROR" in upper or "CRITICAL" in upper or "FATAL" in upper:
        return "ERROR"
    if "WARNING" in upper or "WARN" in upper:
        return "WARNING"
    if "DEBUG" in upper:
        return "DEBUG"
    return "INFO"


# Формат строки лога: "%(asctime)s %(levelname)s %(name)s: %(message)s"
_LINE_RE = re.compile(r"^\S+ \S+ (?:DEBUG|INFO|WARNING|ERROR|CRITICAL) ([\w.\-]+):")

# Компоненты соответствуют именам логгеров, а не угадываются по подстроке в тексте.
_COMPONENT_PREFIXES: dict[str, tuple[str, ...]] = {
    "gateway": ("uvicorn", "httpx", "aria.web"),
    "agent": ("aria.core", "local_agent.loop", "local_agent.taskrunner", "local_agent.tasks", "local_agent.audit",
              "local_agent.router", "local_agent.llm", "local_agent.key_pool", "local_agent.sessions",
              "local_agent.chat", "local_agent.compression", "local_agent.runprofile", "local_agent.timing"),
    "tools": ("aria.tools", "local_agent.tools", "local_agent.toolhooks", "local_agent.hooks", "local_agent.mcp",
              "local_agent.checkpoints", "aria.vision"),
    "cron": ("local_agent.main", "aria.scheduler"),
    "cli": ("aria.cli", "local_agent.setup", "local_agent.env", "local_agent.logs"),
}


def _logger_name(line: str) -> str | None:
    m = _LINE_RE.match(line)
    return m.group(1) if m else None


def _in_component(line: str, comp: str) -> bool:
    prefixes = _COMPONENT_PREFIXES.get(comp)
    if prefixes is None:
        return comp in line.lower()
    name = _logger_name(line)
    return bool(name) and name.startswith(prefixes)


def _resolve_file(name: str) -> Path:
    if not name:
        name = "backend.log"
    safe = Path(name).name  # strip any directory components
    candidates: list[Path] = []
    if safe.lower() in ("backend.log", "agent", "errors"):
        candidates.append(_LOG_DIR / "backend.log")
    else:
        candidates.append(_LOG_DIR / safe)
    candidates.append(_LOG_DIR / "backend.log")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return _LOG_DIR / "backend.log"


def _tail(path: Path, lines: int) -> list[str]:
    chunk = 8192
    read = b""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    pos = max(0, size - chunk)
    with open(path, "rb") as fh:
        while pos > 0:
            fh.seek(pos)
            read = fh.read() + read
            if read.count(b"\n") >= lines * 2:
                break
            pos = max(0, pos - chunk)
            if pos == 0:
                break
        if pos == 0:
            fh.seek(0)
            read = fh.read() + read
    text = read.decode("utf-8", errors="replace")
    parts = text.splitlines()
    return parts[-lines:] if len(parts) > lines else parts


@router.get("/logs")
async def get_logs(
    file: str = Query(default="agent"),
    lines: int = Query(default=100, ge=1, le=2000),
    level: str = Query(default="ALL"),
    component: str = Query(default="all"),
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    path = _resolve_file(file)
    raw = _tail(path, max(1, min(lines, 2000)))
    min_rank = _LEVEL_ORDER.get(level.upper(), 0) if level and level.upper() != "ALL" else 0
    kind = Path(file).name.lower() if file else "agent"
    if kind == "errors":  # вкладка «errors» — это предупреждения и ошибки, а не копия основного лога
        min_rank = max(min_rank, _LEVEL_ORDER["WARNING"])
    comp = component.strip().lower()
    if kind == "gateway" and comp in ("", "all"):  # вкладка «gateway» — HTTP-слой
        comp = "gateway"
    result: list[str] = []
    for line in raw:
        if min_rank and _LEVEL_ORDER.get(_classify_line(line), 0) < min_rank:
            continue
        if comp and comp != "all" and not _in_component(line, comp):
            continue
        result.append(line)
    return {"file": path.name, "lines": result[-lines:] if len(result) > lines else result}


def _truncate_log(path: Path) -> bool:
    """Обнулить файл лога. Если его держит открытым наш RotatingFileHandler, чистим через него,
    иначе дальнейшие записи продолжились бы со старого смещения."""
    if not path.is_file():
        return False
    target = os.path.normcase(str(path.resolve()))
    seen: list[logging.Handler] = []
    for name in ("", "uvicorn", "uvicorn.access"):
        for handler in logging.getLogger(name).handlers:
            base = getattr(handler, "baseFilename", None)
            if base and os.path.normcase(os.path.abspath(base)) == target and handler not in seen:
                seen.append(handler)
    for handler in seen:
        handler.acquire()
        try:
            handler.flush()
            stream = getattr(handler, "stream", None)
            if stream is not None:
                stream.seek(0)
                stream.truncate(0)
        finally:
            handler.release()
    if not seen:
        with open(path, "w", encoding="utf-8"):
            pass
    return True


@router.post("/logs/clear")
async def clear_logs(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    """Очистить лог (и ротированные копии), чтобы дальше были видны только новые записи."""
    cleared = _truncate_log(_LOG_DIR / "backend.log")
    removed = 0
    for i in range(1, 10):
        rotated = _LOG_DIR / f"backend.log.{i}"
        try:
            if rotated.is_file():
                rotated.unlink()
                removed += 1
        except OSError:
            pass
    logging.getLogger("local_agent.logs").info("log cleared by user")
    return {"ok": True, "cleared": cleared, "removed_backups": removed}


def _open_in_file_manager(path: Path) -> None:
    """Открыть каталог в файловом менеджере системы (Windows: Проводник, macOS: Finder, Linux: xdg-open)."""
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])  # noqa: S603, S607
    else:
        subprocess.Popen(["xdg-open", str(path)])  # noqa: S603, S607


@router.post("/logs/open-folder")
async def open_logs_folder(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    """Открыть каталог логов. Путь берётся только из log_dir(), из запроса не принимается."""
    path = _paths.log_dir()
    try:
        path.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(_open_in_file_manager, path)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"could not open the logs folder: {exc}") from exc
    return {"ok": True, "path": str(path)}


_DIAG_MAX_LOG_BYTES = 2 * 1024 * 1024


def _read_log_tail_redacted(path: Path) -> str:
    """Хвост лог-файла (не больше 2 МБ), каждая строка пропущена через redact()."""
    size = path.stat().st_size
    with open(path, "rb") as fh:
        if size > _DIAG_MAX_LOG_BYTES:
            fh.seek(size - _DIAG_MAX_LOG_BYTES)
            fh.readline()  # первая строка после среза может быть обрезана
        data = fh.read()
    text = data.decode("utf-8", errors="replace")
    return "\n".join(redact(line) for line in text.splitlines()) + "\n"


def _diag_providers() -> dict[str, bool]:
    """Только имена переменных ключей провайдеров и флаг «задан»; значения не читаются в вывод."""
    from aria.routers import env as _env_router

    out: dict[str, bool] = {}
    for name, meta in _env_router._REGISTRY.items():
        if meta.get("category") == "provider" and meta.get("is_password"):
            out[name] = bool(os.environ.get(name))
    return out


def _diag_system() -> dict[str, Any]:
    info: dict[str, Any] = {
        "os": platform.system(),
        "os_release": platform.release(),
        "arch": platform.machine(),
        "python": platform.python_version(),
    }
    try:
        info["disk_free_bytes"] = shutil.disk_usage(_paths.log_dir().parent).free
    except OSError:
        info["disk_free_bytes"] = None
    return info


def _build_diagnostics_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        log_dir = _paths.log_dir()
        if log_dir.is_dir():
            for path in sorted(log_dir.iterdir()):
                if path.is_file() and ".log" in path.name:
                    try:
                        zf.writestr(f"logs/{path.name}", _read_log_tail_redacted(path))
                    except OSError:
                        continue
        zf.writestr("version.txt", f"{APP_VERSION}\n")
        zf.writestr("system.json", json.dumps(_diag_system(), indent=2))
        zf.writestr("providers.json", json.dumps(_diag_providers(), indent=2))
    return buf.getvalue()


@router.get("/logs/diagnostics")
async def logs_diagnostics(_: str = Depends(require_runtime_token)) -> Response:
    """Zip для отчёта о проблеме: хвосты логов без секретов, версия, система, флаги ключей."""
    payload = await asyncio.to_thread(_build_diagnostics_zip)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="aria-diagnostics-{stamp}.zip"'},
    )
