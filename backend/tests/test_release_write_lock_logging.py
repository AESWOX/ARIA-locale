"""release_write_lock: сбой commit попадает в лог как ERROR и не роняет вызов."""
from __future__ import annotations

import logging

from aria.db.base import release_write_lock


class _BrokenSession:
    def commit(self):
        raise RuntimeError("database is locked")

    def rollback(self):
        pass


def test_commit_failure_is_logged_as_error(caplog):
    with caplog.at_level(logging.ERROR, logger="local_agent.db"):
        release_write_lock(_BrokenSession())
    assert any(r.levelno == logging.ERROR and "release_write_lock" in r.getMessage() for r in caplog.records)


def test_none_session_is_noop():
    release_write_lock(None)
