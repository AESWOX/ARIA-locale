"""0017e: uv ставится отдельной фазой установщика, бэкенд находит его рядом с приложением."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from aria.mcp import executables

ROOT = Path(__file__).resolve().parents[2]
TAURI = ROOT / "desktop" / "src-tauri"


def test_bundled_uv_preferred_when_frozen(tmp_path, monkeypatch):
    uvdir = tmp_path / "uv"
    uvdir.mkdir()
    (uvdir / "uvx.exe").write_bytes(b"x")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "backend.exe"))
    monkeypatch.setattr(executables.shutil, "which", lambda c: "C:/other/uvx.exe")
    assert executables.resolve_executable("uvx") == str(uvdir / "uvx.exe")


def test_falls_back_to_path_when_bundled_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "backend.exe"))
    monkeypatch.setattr(executables.shutil, "which", lambda c: "C:/path/uvx.exe")
    assert executables.resolve_executable("uvx") == "C:/path/uvx.exe"


def test_not_frozen_uses_path_and_leaves_other_commands(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(executables.shutil, "which", lambda c: None)
    assert executables.resolve_executable("uvx") == "uvx"
    assert executables.resolve_executable("npx") == "npx"


def test_installer_phase_is_wired_and_pinned():
    conf = json.loads((TAURI / "tauri.conf.json").read_text(encoding="utf-8"))
    bundle = conf["bundle"]
    assert "install_uv.ps1" in bundle["resources"]
    hooks = bundle["windows"]["nsis"]["installerHooks"]
    assert (TAURI / hooks).is_file()

    ps1 = (TAURI / "install_uv.ps1").read_text(encoding="utf-8")
    assert re.search(r"\$Sha256 = '[0-9a-f]{64}'", ps1)
    assert re.search(r"\$Version = '\d+\.\d+\.\d+'", ps1)
    assert "Get-FileHash" in ps1 and "mismatch" in ps1
    assert "Invoke-Expression" not in ps1 and "iex" not in ps1.lower().split()
    ps1.encode("ascii")  # PowerShell 5.1 без BOM: только ASCII

    nsh = (TAURI / hooks).read_text(encoding="utf-8")
    assert "NSIS_HOOK_POSTINSTALL" in nsh and "install_uv.ps1" in nsh
    assert 'RMDir /r "$INSTDIR\\uv"' in nsh  # удаление при деинсталляции
