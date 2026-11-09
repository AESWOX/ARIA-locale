"""S10: на Windows shell-инструмент исполняет команды через PowerShell."""
from __future__ import annotations

import asyncio
import os

import pytest

from aria.tools.handlers import shell as shell_handler


class _Proc:
    returncode = 0

    async def communicate(self):
        return b"", b""


@pytest.mark.skipif(os.name != "nt", reason="Windows-only interpreter check")
def test_shell_execute_uses_powershell(monkeypatch):
    seen: dict = {}

    async def _spawn(command, **kwargs):
        seen["command"] = command
        seen.update(kwargs)
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _spawn)
    asyncio.run(shell_handler.shell_execute({"command": "echo hi"}, timeout_sec=5, approved=True))

    import base64

    raw = seen["command"]
    assert "powershell" in raw.lower() and "-EncodedCommand" in raw
    decoded = base64.b64decode(raw.rsplit(" ", 1)[-1]).decode("utf-16-le")
    assert decoded == "echo hi"


def test_bash_syntax_is_not_allowed():
    from aria.tools.validators import classify_shell_command

    assert classify_shell_command("bash -c 'ls'") != "allow"
    assert classify_shell_command("ls; rm -rf x") != "allow"
