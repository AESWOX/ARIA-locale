"""Версия приложения одна: Tauri, Cargo, npm и API бэкенда не расходятся (HYG-4)."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _versions() -> dict[str, str]:
    cargo = re.search(r'^version = "([^"]+)"', (ROOT / "desktop/src-tauri/Cargo.toml").read_text(encoding="utf-8"), re.M)
    from aria.main import app

    return {
        "tauri.conf.json": json.loads((ROOT / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))["version"],
        "Cargo.toml": cargo.group(1),
        "package.json": json.loads((ROOT / "desktop/package.json").read_text(encoding="utf-8"))["version"],
        "FastAPI": app.version,
    }


def test_all_version_sources_agree():
    versions = _versions()
    assert len(set(versions.values())) == 1, versions


def test_cargo_lock_and_package_lock_match_package_version():
    expected = _versions()["package.json"]
    lock = json.loads((ROOT / "desktop/package-lock.json").read_text(encoding="utf-8"))
    assert lock["version"] == expected and lock["packages"][""]["version"] == expected
    cargo_lock = (ROOT / "desktop/src-tauri/Cargo.lock").read_text(encoding="utf-8")
    assert f'name = "local-agent-ui"\nversion = "{expected}"' in cargo_lock
