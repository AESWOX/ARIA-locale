"""Этап 3: рельсы auto/approve/deny, лимит чекпоинтов и Undo."""
from __future__ import annotations

import pytest

from aria import paths
from aria.checkpoints import store as checkpoints
from aria.core import action_policy


@pytest.mark.parametrize("command,code", [
    ("format c:", "disk_format"),
    ("mkfs.ext4 /dev/sda1", "disk_format"),
    ("diskpart", "disk_format"),
    ("reg add HKLM\\Software /v x /d 1", "registry"),
    ("regedit", "registry"),
    ("bcdedit /set {default} safeboot minimal", "system_boot"),
    ("apt-get install nginx", "system_packages"),
    ("winget install Git.Git", "system_packages"),
    ("choco install nodejs", "system_packages"),
    ("git push origin main --force", "force_push"),
    ("git push -f origin main", "force_push"),
    ("rm -rf /", "outside_project"),
    ("rm -fr /tmp/x", "outside_project"),
    ("rm -r ~/docs", "outside_project"),
    ("del /s /q C:\\Windows\\System32\\x.dll", "outside_project"),
])
def test_shell_deny_list(command, code):
    decision = action_policy.decide("shell_execute", {"command": command}, sandbox_root="C:\\proj")
    assert decision.action == "deny" and decision.code == code


@pytest.mark.parametrize("command", [
    "git status", "dir", "echo hi", "pytest -q", "ls; rm -rf x",
])
def test_safe_or_relative_shell_defers_to_existing_allowlist(command):
    decision = action_policy.decide("shell_execute", {"command": command}, sandbox_root="C:\\proj")
    assert decision.action == "defer"


def test_file_tools_inside_project_are_auto():
    assert action_policy.decide("file_read", {"path": "src/app.py"}, sandbox_root="C:\\proj").action == "allow"
    assert action_policy.decide("file_write", {"path": "out/a.txt"}, sandbox_root="C:\\proj").action == "allow"


@pytest.mark.parametrize("path", ["../secret.txt", "C:\\Windows\\x.txt", "~/notes.txt"])
def test_file_tools_outside_project_are_denied(path):
    decision = action_policy.decide("file_write", {"path": path}, sandbox_root="C:\\proj")
    assert decision.action == "deny" and decision.code == "outside_project"


def test_sending_requires_approval_with_full_text():
    decision = action_policy.decide(
        "email_send", {"to": "a@b.c", "text": "полный текст письма"}, sandbox_root="C:\\proj"
    )
    assert decision.action == "approve" and "полный текст письма" in decision.summary


def test_untrusted_context_approves_writes_outside_project():
    allow = action_policy.decide("write_note", {"path": "x.md"}, sandbox_root="C:\\proj", untrusted=False)
    gated = action_policy.decide("write_note", {"path": "x.md"}, sandbox_root="C:\\proj", untrusted=True)
    assert allow.action == "allow"
    assert gated.action == "approve" and gated.code == "untrusted_context"


def test_checkpoint_retention_keeps_last_20_and_undo_restores(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    root = tmp_path / "project"
    root.mkdir()
    target = root / "app.py"
    target.write_text("v0", encoding="utf-8")

    for i in range(25):
        target.write_text(f"v{i + 1}", encoding="utf-8")
        assert checkpoints.snapshot("sess-1", None, "file_write", str(root), ["app.py"]) is not None

    manifests = list((tmp_path / "checkpoints" / "sess-1").glob("*/manifest.json"))
    assert len(manifests) == 20

    newest = checkpoints.list_checkpoints(session="sess-1")[-1]
    target.write_text("broken", encoding="utf-8")
    checkpoints.restore(newest["id"])
    assert target.read_text(encoding="utf-8") == "v25"
