"""F-012/F-014: Approve-карточка = действие + цель (без свода правил);
18+ не отключает жёсткие лимиты."""
from __future__ import annotations

import uuid

from aria.core import adult_mode, approvals, content_limits
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import TaskStatus


def _session_task(objective: str = "собрать релиз"):
    with session_scope() as db:
        session = repo.create_session(db, f"f012-{uuid.uuid4().hex[:6]}")
        task = repo.create_task(db, session, role="general", objective=objective)
        repo.set_task_status(db, task, TaskStatus.approved)
        repo.set_task_status(db, task, TaskStatus.in_progress)
        return session.id, task.id


def test_high_risk_approval_shows_only_action_and_goal():
    sid, tid = _session_task("собрать релиз")
    with session_scope() as db:
        session = repo.get_session(db, sid)
        task = repo.get_task(db, tid)
        item = approvals.request_high_risk_shell_approval(
            db, session, task, "rm -rf build", reason="matched HIGH_RISK_PATTERNS (§14.2)"
        )

    assert item.title.startswith("Approve:")
    assert "rm -rf build" in item.body_md
    assert "собрать релиз" in item.body_md
    assert "§14.2" not in item.body_md
    assert "Причина" not in item.body_md
    # причина остаётся в payload для аудита, но не в карточке
    assert item.payload_json["reason"]


def test_adult_mode_does_not_disable_hard_limits():
    with session_scope() as db:
        adult_mode.set_adult(db, True, scope="global", mark_confirmed=True)
        assert adult_mode.is_enabled(db) is True

    assert content_limits.check("секс с несовершеннолетней") is not None
    assert content_limits.check("underage nude") is not None
    assert content_limits.check("обычный взрослый роман") is None
