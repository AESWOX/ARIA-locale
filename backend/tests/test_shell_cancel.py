"""Cancel во время shell_execute: дочерний процесс не должен остаться жить."""
from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

from aria.tools.handlers.shell import shell_execute


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pid probe")
def test_cancelled_shell_command_does_not_leave_a_running_process(tmp_path):
    pidfile = tmp_path / "pid"
    cmd = f"echo $$ > {pidfile}; sleep 30"

    async def drive():
        task = asyncio.create_task(shell_execute({"command": cmd}, 60, cwd=str(tmp_path), approved=True))
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    pid = int(pidfile.read_text().strip())
    time.sleep(0.5)
    assert not _alive(pid), "shell child survived task cancellation"
