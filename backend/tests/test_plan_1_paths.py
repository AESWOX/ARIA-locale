"""R4: `validate_plan_paths` — файловые шаги плана против sandbox_root.

Только инструменты `file_*`; sandbox_root пуст/None → проверка пропущена; vault-инструменты
не проверяются. `file_read` допустим, если файл существует ИЛИ его пишет шаг-предок.
"""
from __future__ import annotations

import pytest

from aria.core import plan_validator as pv


def _sb(tmp_path):
    sb = tmp_path / "sb"
    sb.mkdir()
    return sb


def test_rejects_nonexistent_file_read(tmp_path):
    sb = _sb(tmp_path)
    steps = [{"step_id": "s1", "tool_ref": "file_read", "path": "missing.txt", "depends_on": []}]
    with pytest.raises(ValueError) as ei:
        pv.validate_plan_paths(steps, str(sb))
    assert "non-existent" in str(ei.value)


def test_allows_file_written_by_previous_step(tmp_path):
    sb = _sb(tmp_path)
    steps = [
        {"step_id": "s1", "tool_ref": "file_write", "path": "a.txt", "write_scope": ["a.txt"], "depends_on": []},
        {"step_id": "s2", "tool_ref": "file_read", "path": "a.txt", "depends_on": ["s1"]},
    ]
    pv.validate_plan_paths(steps, str(sb))  # не падает: a.txt пишет предок


def test_allows_existing_file_read(tmp_path):
    sb = _sb(tmp_path)
    (sb / "present.txt").write_text("x")
    steps = [{"step_id": "s1", "tool_ref": "file_read", "path": "present.txt", "depends_on": []}]
    pv.validate_plan_paths(steps, str(sb))


def test_glob_not_treated_as_literal(tmp_path):
    sb = _sb(tmp_path)
    steps = [
        {"step_id": "s1", "tool_ref": "file_search", "glob": "**/*.py", "depends_on": []},
        {"step_id": "s2", "tool_ref": "file_grep", "glob": "*.txt", "depends_on": []},
    ]
    pv.validate_plan_paths(steps, str(sb))  # glob не обязан существовать


def test_skip_when_no_sandbox_root(tmp_path):
    steps = [{"step_id": "s1", "tool_ref": "file_read", "path": "C:\\definitely\\missing.txt", "depends_on": []}]
    pv.validate_plan_paths(steps, None)  # пропуск явный


def test_vault_tools_not_checked(tmp_path):
    sb = _sb(tmp_path)
    steps = [
        {"step_id": "s1", "tool_ref": "read_note", "note_name": "INDEX", "depends_on": []},
        {"step_id": "s2", "tool_ref": "write_note", "note_name": "X", "content": "y", "depends_on": []},
    ]
    pv.validate_plan_paths(steps, str(sb))  # vault-инструменты не проверяются


def test_rejects_file_write_outside_sandbox(tmp_path):
    sb = _sb(tmp_path)
    outside = str(tmp_path / "outside.txt")
    steps = [{"step_id": "s1", "tool_ref": "file_write", "path": outside, "depends_on": []}]
    with pytest.raises(ValueError) as ei:
        pv.validate_plan_paths(steps, str(sb))
    assert "outside sandbox" in str(ei.value)


def test_rejects_glob_escaping_sandbox(tmp_path):
    sb = _sb(tmp_path)
    steps = [{"step_id": "s1", "tool_ref": "file_search", "glob": "../*.py", "depends_on": []}]
    with pytest.raises(ValueError) as ei:
        pv.validate_plan_paths(steps, str(sb))
    assert "escapes sandbox" in str(ei.value)
