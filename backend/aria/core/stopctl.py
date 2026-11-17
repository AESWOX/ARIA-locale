"""Stop/resume entry points usable without HTTP (STOP-1).

``chat_stop`` and the Telegram bot (BOT-1) call exactly these functions, so Stop means the
same thing from the app and from the bot. The live signal is in :mod:`aria.core.stop_registry`;
the durable ``stopped`` marker is in ``agent_loop:<task_id>``.
"""
from __future__ import annotations

import uuid

from aria.core import loop_state, stop_registry
from aria.core import taskrunner as taskrunner_mod
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import SourceTrust, TaskStatus

# Statuses a Stop can act on. ``under_audit`` is deliberately excluded: the audit is short and
# the owner chose "stopped: false" for it; ``awaiting_attention`` has no running loop at all.
_STOPPABLE = (TaskStatus.in_progress, TaskStatus.approved, TaskStatus.needs_rework)


def stop_session_task(session_id: uuid.UUID) -> bool:
    """Stop the session's current agent task. Returns True when a Stop was delivered."""
    with session_scope() as db:
        session = repo.get_session(db, session_id)
        if session is None or session.current_task_id is None:
            return False
        task = repo.get_task(db, session.current_task_id)
        if task is None or task.status not in _STOPPABLE:
            return False
        task_id = task.id
    runner = taskrunner_mod.active_runner()
    if runner is None:
        return False
    return runner.request_stop(task_id)


def resume_stopped_task(session_id: uuid.UUID, addition: str | None = None) -> bool:
    """Continue the stopped task (R10/R14). ``addition`` is stored as an owner message and
    marked for the prompt prefix; stored text is unchanged."""
    with session_scope() as db:
        session = repo.get_session(db, session_id)
        if session is None or session.current_task_id is None:
            return False
        task = repo.get_task(db, session.current_task_id)
        if task is None or task.status not in _STOPPABLE or not loop_state.is_stopped(db, task.id):
            return False
        text = (addition or "").strip()
        if text:
            message = repo.append_message(
                db, session, role="user", content=text, source_trust=SourceTrust.trusted,
            )
            message.content_json = {"resume_after_stop": True}
        loop_state.set_stopped(db, task.id, False)
        task_id = task.id
    runner = taskrunner_mod.active_runner()
    if runner is None:
        stop_registry.clear(task_id)
        return False
    queued = runner.submit(task_id, mode="agent", source="stop_continue")
    return bool(queued.get("queued"))
