"""Persistent per-task agent-loop state (0.2.1).

Iterations, the repeat-suppression counter and the guardrail controller counters used to live
only in local variables of ``execute_agent_loop``. Every pause for owner approval returned from
the loop, and ``resume_after_approval`` restarted the counters from zero: approving a repeated
call effectively refunded the step budget. The state now lives in ``agent_state`` under
``agent_loop:<task_id>`` and is restored on resume.
"""
from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session as OrmSession

from aria.core.guardrails import (
    ToolCallGuardrailController,
    ToolCallSignature,
    ToolGuardrailDecision,
)
from aria.db import repository as repo

KEY_PREFIX = "agent_loop:"


def key(task_id: uuid.UUID | str) -> str:
    return f"{KEY_PREFIX}{task_id}"


def load(db: OrmSession, task_id: uuid.UUID | str) -> dict:
    return dict(repo.get_agent_state(db, key(task_id)) or {})


def save(db: OrmSession, task_id: uuid.UUID | str, state: Mapping[str, Any]) -> None:
    repo.set_agent_state(db, key(task_id), dict(state), source="agent_loop")


def clear(db: OrmSession, task_id: uuid.UUID | str) -> None:
    save(db, task_id, {})


def _sig(tool_name: Any, args_hash: Any) -> ToolCallSignature:
    return ToolCallSignature(tool_name=str(tool_name), args_hash=str(args_hash))


def dump_guardrails(controller: ToolCallGuardrailController) -> dict:
    halt = controller.halt_decision
    return {
        "exact_failures": [
            [signature.tool_name, signature.args_hash, count]
            for signature, count in controller._exact_failure_counts.items()
        ],
        "same_tool_failures": dict(controller._same_tool_failure_counts),
        "no_progress": [
            [signature.tool_name, signature.args_hash, record[0], record[1]]
            for signature, record in controller._no_progress.items()
        ],
        "halt": (
            {
                "tool_name": halt.tool_name,
                "args_hash": halt.signature.args_hash,
                "action": halt.action,
                "code": halt.code,
                "message": halt.message,
                "count": halt.count,
            }
            if halt is not None
            else None
        ),
    }


def restore_guardrails(controller: ToolCallGuardrailController, data: Mapping[str, Any] | None) -> None:
    controller.reset_for_turn()
    if not isinstance(data, Mapping):
        return
    for entry in data.get("exact_failures") or []:
        try:
            tool_name, args_hash, count = entry
        except (TypeError, ValueError):
            continue
        controller._exact_failure_counts[_sig(tool_name, args_hash)] = int(count)
    for tool_name, count in dict(data.get("same_tool_failures") or {}).items():
        controller._same_tool_failure_counts[str(tool_name)] = int(count)
    for entry in data.get("no_progress") or []:
        try:
            tool_name, args_hash, result_hash, count = entry
        except (TypeError, ValueError):
            continue
        controller._no_progress[_sig(tool_name, args_hash)] = (str(result_hash), int(count))
    halt = data.get("halt")
    if isinstance(halt, Mapping):
        controller._halt_decision = ToolGuardrailDecision(
            tool_name=str(halt.get("tool_name") or ""),
            signature=_sig(halt.get("tool_name") or "", halt.get("args_hash") or ""),
            action=str(halt.get("action") or "allow"),
            code=str(halt.get("code") or ""),
            message=str(halt.get("message") or ""),
            count=int(halt.get("count") or 0),
        )
