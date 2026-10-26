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


def test_app_version_constant_matches_other_sources():
    from aria.version import APP_VERSION

    assert set(_versions().values()) == {APP_VERSION}


def test_no_importlib_metadata_version_lookup_in_backend():
    """В PyInstaller-сборке метаданных пакета нет: версия берётся только из aria.version."""
    offenders = [
        str(p.relative_to(ROOT))
        for p in (ROOT / "backend" / "aria").rglob("*.py")
        if "importlib.metadata" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, offenders


def test_endpoints_report_app_version_without_package_metadata(monkeypatch):
    """Имитация backend.exe: importlib.metadata.version падает, версия всё равно верная."""
    import importlib.metadata

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aria.api.auth import require_runtime_token
    from aria.routers import stubs, system
    from aria.version import APP_VERSION

    def _missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", _missing)

    app = FastAPI()
    app.include_router(stubs.router)
    app.include_router(system.router)
    app.dependency_overrides[require_runtime_token] = lambda: "test"
    client = TestClient(app)

    r = client.get("/aria/update/check")
    assert r.status_code == 200, r.text
    assert r.json()["current_version"] == APP_VERSION

    r = client.get("/system/stats")
    assert r.status_code == 200, r.text
    assert r.json()["aria_version"] == APP_VERSION
