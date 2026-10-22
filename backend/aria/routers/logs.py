"""Log tailing routes: /api/logs.

Serves the tail of the backend's file logs. Logical log names map to files
under ``backend/logs`` (where ``aria/main.py`` writes with a
``RotatingFileHandler``): ``agent`` -> backend.log, ``gateway`` -> gateway.log.
Unknown logical names fall back to backend.log. Lines are filtered by level
(cascading: ERROR < WARNING < INFO < DEBUG) and by component keyword.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from aria import paths as _paths
from aria.api.auth import require_runtime_token

router = APIRouter(tags=["logs"])

_LOG_DIR = _paths.log_dir()

_LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}


def _classify_line(line: str) -> str:
    upper = line.upper()
    if "ERROR" in upper or "CRITICAL" in upper or "FATAL" in upper:
        return "ERROR"
    if "WARNING" in upper or "WARN" in upper:
        return "WARNING"
    if "DEBUG" in upper:
        return "DEBUG"
    return "INFO"


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
    comp = component.strip().lower()
    result: list[str] = []
    for line in raw:
        if min_rank and _LEVEL_ORDER.get(_classify_line(line), 0) < min_rank:
            continue
        if comp and comp != "all" and comp not in line.lower():
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
