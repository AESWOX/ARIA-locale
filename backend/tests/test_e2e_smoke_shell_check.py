"""Проверка стадии agent_shell не должна принимать текст вопросов уточнения за вывод команды (BUG-2)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "e2e_smoke.py"
spec = importlib.util.spec_from_file_location("e2e_smoke", SCRIPT)
smoke = importlib.util.module_from_spec(spec)
sys.modules["e2e_smoke"] = smoke
spec.loader.exec_module(smoke)

# Красная сессия 26.10: до Approve в чате лежит вопрос уточнения со словами «git version ...», команда не запускалась.
BEFORE_APPROVAL = [
    {"role": "user", "content": "Delegate ... run exactly this command and report its output: git --version"},
    {"role": "assistant", "content": "1. Что считать результатом?\n A) только строку вида `git version ...`\n B) строку и код"},
    {"role": "user", "content": "Уточнения: q-1: строку и код выхода"},
    {"role": "tool", "content": "delegate_task -> awaiting_attention"},
]
AFTER_APPROVAL = BEFORE_APPROVAL + [
    {"role": "tool", "content": "shell_execute (approved) -> ok"},
    {"role": "assistant", "content": "git version 2.54.0.windows.1\nexit_code: 0"},
    {"role": "tool", "content": "delegate_task (resumed) -> done"},
]


def test_old_substring_check_flagged_the_clarification_question():
    assert "git version" in json.dumps(BEFORE_APPROVAL)  # так стадия падала ложно


def test_clarification_text_is_neither_a_run_nor_command_output():
    assert smoke.shell_ran(BEFORE_APPROVAL) is False
    assert smoke.git_output_seen(BEFORE_APPROVAL) is False


def test_real_run_and_output_are_detected_after_approval():
    assert smoke.shell_ran(AFTER_APPROVAL) is True
    assert smoke.git_output_seen(AFTER_APPROVAL) is True


def test_execution_before_approval_is_still_caught():
    leaked = BEFORE_APPROVAL + [{"role": "tool", "content": "shell_execute -> ok"}]
    assert smoke.shell_ran(leaked) is True


def test_output_alone_without_a_tool_run_does_not_finish_the_stage():
    only_prose = BEFORE_APPROVAL + [{"role": "assistant", "content": "git version 2.54.0.windows.1"}]
    assert smoke.git_output_seen(only_prose) is True
    assert smoke.shell_ran(only_prose) is False
