"""F-009: managed toolchain — статус, офлайн-отказ, pinned установка, PATH песочницы."""
from __future__ import annotations

import os
import sys

import pytest
from aria import toolchain
from aria.tools.handlers import shell


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("ARIA_RUNTIME_DIR", str(tmp_path / "runtime"))
    return tmp_path / "runtime"


def test_status_reports_missing_without_venv(runtime):
    status = toolchain.probe()

    assert status["installed"] is False
    assert status["runtime_dir"] == str(runtime)
    assert set(status["missing"]) == {"python", "pytest", "ruff", "pip-audit"}


def test_path_entries_only_when_scripts_exist(runtime):
    assert toolchain.path_entries() == []

    toolchain.scripts_dir().mkdir(parents=True)
    entries = toolchain.path_entries()

    assert entries == [str(toolchain.scripts_dir())]


def test_install_offline_is_honest(runtime, monkeypatch):
    monkeypatch.setattr(toolchain, "_online", lambda timeout=2.0: False)
    calls: list[list[str]] = []
    monkeypatch.setattr(toolchain, "run", lambda args, timeout=120: calls.append(args) or (0, ""))

    result = toolchain.install()

    assert result["ok"] is False and result["offline"] is True
    assert "PyPI" in result["error"]
    assert calls == [], "офлайн не должен пытаться ставить пакеты"


def test_install_online_uses_pinned_versions(runtime, monkeypatch):
    monkeypatch.setattr(toolchain, "_online", lambda timeout=2.0: True)
    monkeypatch.setattr(toolchain, "_find_uv", lambda: None)
    executed: list[list[str]] = []

    def fake_run(args, timeout=120):
        executed.append(list(args))
        if "venv" in args:
            toolchain.scripts_dir().mkdir(parents=True, exist_ok=True)
            toolchain.venv_python().write_text("", encoding="utf-8")
            (toolchain.scripts_dir() / "ruff.exe").write_text("", encoding="utf-8")
            return 0, ""
        if "pip" in args:
            return 0, "Successfully installed"
        if "--version" in args:
            tool = args[0].lower()
            if tool.endswith(("ruff.exe", "ruff")):
                return 0, "ruff 0.11.13"
            return 0, "pytest 8.3.5"
        if "pip_audit" in args:
            return 0, "pip-audit 2.9.0"
        return 0, "3.11.9"

    monkeypatch.setattr(toolchain, "run", fake_run)

    result = toolchain.install()

    pip_cmd = next(args for args in executed if "pip" in args)
    for name, version in toolchain.PINNED_VERSIONS.items():
        assert f"{name}=={version}" in pip_cmd
    assert "--require-hashes" not in pip_cmd
    assert result["ok"] is True, result


def test_shell_env_prepends_toolchain(monkeypatch):
    monkeypatch.setattr(toolchain, "path_entries", lambda: ["C:/app/runtime/Scripts"])

    env = shell.build_shell_env({"PATH": "C:/Windows/System32"})

    assert env["PATH"].split(os.pathsep)[0].replace("\\", "/") == "C:/app/runtime/Scripts"
    assert "C:/Windows/System32" in env["PATH"]


def test_auto_install_skipped_in_dev(monkeypatch, runtime):
    """Не ставим сотни МБ в dev-режиме: установка только в собранной сборке или по кнопке."""
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.delenv("ARIA_RUNTIME_DIR", raising=False)
    called: list[str] = []
    monkeypatch.setattr(toolchain, "install", lambda: called.append("install") or {"ok": True})

    toolchain.ensure_online_install()

    assert called == []
