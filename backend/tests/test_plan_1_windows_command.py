"""R1: `is_windows_command` — команда выполнима на Windows (cmd-builtin или `shutil.which`).

Герметично: платформа через `validators._is_windows_platform`, поиск бинарников —
подменённый `shutil.which`. На Linux-CI `ls`/`grep` не должны ломать тесты.
"""
from __future__ import annotations

import pytest

from aria.tools import validators as v

# Полный набор внутренних команд cmd.exe (решение владельца 30.10). `where` здесь
# нет: он определяется через shutil.which (where.exe в System32), как внешняя утилита.
_CMD_BUILTINS = [
    "assoc", "break", "call", "cd", "chdir", "cls", "color", "copy", "date",
    "del", "dir", "echo", "endlocal", "erase", "exit", "for", "ftype", "goto",
    "if", "md", "mkdir", "mklink", "move", "path", "pause", "popd", "prompt",
    "pushd", "rd", "rem", "ren", "rename", "rmdir", "set", "setlocal", "shift",
    "start", "time", "title", "type", "ver", "verify", "vol",
]


@pytest.fixture()
def win(monkeypatch):
    monkeypatch.setattr(v, "_is_windows_platform", lambda: True)
    found = {
        "curl": "C:\\curl.exe",
        "myutil": "C:\\tools\\myutil.exe",
        "where": "C:\\Windows\\System32\\where.exe",
    }

    def fake_which(name: str):
        return found.get(name.lower())

    monkeypatch.setattr(v.shutil, "which", fake_which)
    return found


@pytest.fixture()
def win_nopath(monkeypatch):
    """Windows, но shutil.which ничего не находит — только cmd-builtin проходят."""
    monkeypatch.setattr(v, "_is_windows_platform", lambda: True)
    monkeypatch.setattr(v.shutil, "which", lambda name: None)


def test_rejects_bash_sh_iconv(win):
    assert v.is_windows_command("bash -c 'echo hi'") is False
    assert v.is_windows_command("sh script.sh") is False
    assert v.is_windows_command("iconv -f utf-8 -t ascii x") is False


def test_chain_cd_then_bash(win):
    assert v.is_windows_command("cd C:\\tmp; bash -c 'x'") is False


def test_pipe_type_grep(win):
    assert v.is_windows_command("type a.txt | grep foo") is False


@pytest.mark.parametrize("cmd", _CMD_BUILTINS)
def test_cmd_builtins_allowed_without_path(win_nopath, cmd):
    assert v.is_windows_command(cmd) is True


def test_cmd_builtins_allowed_in_commands(win_nopath):
    assert v.is_windows_command("echo hi") is True
    assert v.is_windows_command("cd C:\\tmp") is True
    assert v.is_windows_command("rmdir /s /q x") is True
    assert v.is_windows_command("del a.txt") is True
    assert v.is_windows_command("copy a b") is True


def test_where_and_external_via_shutil_which(win):
    assert v.is_windows_command("where python") is True
    assert v.is_windows_command("where.exe /?") is True
    assert v.is_windows_command("curl https://example.com") is True


def test_exe_suffix_and_quotes(win):
    assert v.is_windows_command('"C:\\tools\\myutil.exe" --flag') is True


def test_non_windows_returns_true(monkeypatch):
    monkeypatch.setattr(v, "_is_windows_platform", lambda: False)
    assert v.is_windows_command("bash -c 'rm -rf /'") is True


def test_message_distinguishes_not_in_path(win):
    assert v.command_rejection_reason("bash -c x") == "unix_only_command"
    assert v.command_rejection_reason("pytest tests") == "command_not_found_in_path"
