"""Поиск исполняемых файлов для запуска MCP-серверов.

uv/uvx ставит установщик в <папка приложения>\\uv (см. desktop/src-tauri/install_uv.ps1).
Ищем сначала там, потом в PATH: так MCP `fetch` работает на чистой машине без ручной установки uv.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

_BUNDLED = {"uv", "uvx"}


def bundled_uv_dir() -> Path | None:
    """Папка uv рядом с backend.exe (только в упакованной сборке)."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent / "uv"


def resolve_executable(command: str) -> str:
    """Полный путь к команде: встроенный uv/uvx, затем PATH, иначе имя как есть."""
    stem = Path(command).stem.lower() if Path(command).name == command else ""
    if stem in _BUNDLED:
        base = bundled_uv_dir()
        if base is not None:
            candidate = base / f"{stem}.exe"
            if candidate.is_file():
                return str(candidate)
    return shutil.which(command) or command
