"""F-009/F-004: управляемый venv приложения с базой инструментов.

База (python 3.11 + pytest + ruff + pip-audit) живёт в `<app>/runtime`, а не в
системном Python и не в venv заказа: агент в песочнице вызывает `python`/
`pytest` из этого venv, а heavy-аудит берёт ruff/pip-audit оттуда же.

Онлайн — установка идёт сразу (в фоне на старте приложения). Офлайн — кнопка
«Доустановить инструменты» (`POST /toolchain/install`): без сети возвращается
честный статус, приложение не блокируется. Heavy-аудит без инструментов
деградирует до syntax/структуры, а не выдумывает «проверено».
"""
from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

logger = logging.getLogger("aria.toolchain")

# Pin exact versions: воспроизводимая база, без «latest» из сети.
PINNED_VERSIONS: dict[str, str] = {
    "pytest": "8.3.5",
    "ruff": "0.11.13",
    "pip-audit": "2.9.0",
}
PINNED_REQUIREMENTS = [f"{name}=={version}" for name, version in PINNED_VERSIONS.items()]

_INSTALL_LOCK = threading.Lock()


def app_dir() -> Path:
    """Корень приложения: рядом с backend.exe (портативная) или resources/../ (Tauri)."""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        return exe_dir.parent if exe_dir.name.lower() == "resources" else exe_dir
    # dev: backend/aria/toolchain.py -> backend/
    return Path(__file__).resolve().parent.parent


def runtime_dir() -> Path:
    override = os.environ.get("ARIA_RUNTIME_DIR", "").strip()
    return Path(override).expanduser() if override else app_dir() / "runtime"


def scripts_dir() -> Path:
    return runtime_dir() / ("Scripts" if os.name == "nt" else "bin")


def venv_python() -> Path:
    return scripts_dir() / ("python.exe" if os.name == "nt" else "python3")


def path_entries() -> list[str]:
    """Каталог venv для PATH дочерних процессов: python/pytest/ruff находятся без установки в систему."""
    scripts = scripts_dir()
    return [str(scripts)] if scripts.is_dir() else []


def _run(args: list[str], timeout: int = 120) -> tuple[int, str]:
    return run(args, timeout=timeout)


def run(args: list[str], timeout: int = 120) -> tuple[int, str]:
    """Публичный запуск инструмента: (exit_code, stdout+stderr). Не бросает."""
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    output = ((completed.stdout or "") + (completed.stderr or "")).strip()
    return completed.returncode, output[:2000]


def _version_of(tool: str, python: Path | None) -> str | None:
    if tool == "python":
        if python is None or not python.is_file():
            return None
        code, out = _run([str(python), "-c", "import sys;print(sys.version.split()[0])"], timeout=20)
        return out.splitlines()[0].strip() if code == 0 and out else None
    if python is None or not python.is_file():
        return None
    if tool == "ruff":
        ruff = scripts_dir() / ("ruff.exe" if os.name == "nt" else "ruff")
        command = str(ruff) if ruff.is_file() else (shutil.which("ruff") or "")
        if not command:
            return None
        code, out = _run([command, "--version"], timeout=30)
        return out.splitlines()[0].strip()[:120] if code == 0 and out else None
    args = {
        "pytest": [str(python), "-m", "pytest", "--version"],
        "pip-audit": [str(python), "-m", "pip_audit", "--version"],
    }[tool]
    code, out = _run(args, timeout=30)
    if code != 0 or not out:
        return None
    return out.splitlines()[0].strip()[:120]


def probe() -> dict:
    """Фактические версии базы. Не бросает: отсутствует — None."""
    python = venv_python()
    has_python = python.is_file()
    versions = {
        "python": _version_of("python", python if has_python else None),
        "pytest": _version_of("pytest", python if has_python else None),
        "ruff": _version_of("ruff", python if has_python else None),
        "pip-audit": _version_of("pip-audit", python if has_python else None),
    }
    missing = [name for name, version in versions.items() if not version]
    return {
        "runtime_dir": str(runtime_dir()),
        "python_path": str(python),
        "versions": versions,
        "missing": missing,
        "installed": not missing,
    }


def _online(timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection(("pypi.org", 443), timeout=timeout):
            return True
    except OSError:
        return False


def _find_uv() -> str | None:
    try:
        from aria.mcp.executables import resolve_executable

        found = resolve_executable("uv")
    except Exception:  # noqa: BLE001
        found = shutil.which("uv")
    if not found:
        return None
    if found == "uv" and not shutil.which("uv"):
        return None
    return found


def install(timeout: int = 600) -> dict:
    """Создать/дополнить managed venv базой. Возвращает честный отчёт."""
    with _INSTALL_LOCK:
        target = runtime_dir()
        python = venv_python()
        steps: list[str] = []
        errors: list[str] = []
        if not _online():
            return {
                "ok": False,
                "offline": True,
                "error": "no network: PyPI is unreachable; keep working with the offline toolchain",
                "runtime_dir": str(target),
                "steps": steps,
            }
        if not python.is_file():
            uv = _find_uv()
            if uv:
                code, out = _run([uv, "venv", "--python", "3.11", str(target)], timeout=timeout)
                steps.append(f"uv venv -> {code}")
                if code != 0:
                    errors.append(f"uv venv failed: {out}")
            else:
                code, out = _run([sys.executable, "-m", "venv", str(target)], timeout=timeout)
                steps.append(f"python -m venv -> {code}")
                if code != 0:
                    errors.append(f"venv creation failed: {out}")
        if python.is_file():
            uv = _find_uv()
            if uv:
                cmd = [uv, "pip", "install", "--python", str(python), *PINNED_REQUIREMENTS]
            else:
                cmd = [str(python), "-m", "pip", "install", "--no-input", "--disable-pip-version-check", *PINNED_REQUIREMENTS]
            code, out = _run(cmd, timeout=timeout)
            steps.append(f"install base -> {code}")
            if code != 0:
                errors.append(f"pip install failed: {out}")
        else:
            errors.append("no venv python after setup (uv/python3.11 missing?)")
        status = probe()
        return {
            "ok": status["installed"] and not errors,
            "offline": False,
            "runtime_dir": str(target),
            "steps": steps,
            "errors": errors,
            "status": status,
        }


def ensure_online_install() -> None:
    """Фоновая установка базы при старте, если сети доступно. Офлайн — тихо ждёт кнопки."""
    try:
        if not (getattr(sys, "frozen", False) or os.environ.get("ARIA_RUNTIME_DIR", "").strip()):
            # dev-запуск: не ставим сотни МБ в backend/runtime без команды (кнопка/явный вызов).
            logger.info("toolchain: dev run, automatic base install skipped")
            return
        status = probe()
        if status["installed"]:
            return
        if not _online():
            logger.info("toolchain: PyPI unreachable, base tools stay uninstalled (offline button available)")
            return
        result = install()
        if result.get("ok"):
            logger.info("toolchain: base installed into %s", result.get("runtime_dir"))
        else:
            logger.warning("toolchain: base install failed: %s", result.get("errors") or result.get("error"))
    except Exception:
        logger.warning("toolchain: background install crashed", exc_info=True)
