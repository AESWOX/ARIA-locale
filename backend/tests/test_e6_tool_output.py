"""E6: сворачивание длинного вывода инструментов и tool_output_read."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from aria.core import loop
from aria.tools import registry
from aria.tools.handlers import files as files_handler

REF = "11111111-1111-1111-1111-111111111111"
BIG = {"content": "a" * 20_000}


def test_fold_writes_full_output_and_render_is_small(tmp_path):
    meta = loop._fold_tool_output(str(tmp_path), "task-1", REF, BIG)
    saved = tmp_path / ".tool_output" / "task-1" / f"{REF}.txt"

    assert meta["fold_ref"] == REF and meta["folded_chars"] > 3000
    assert saved.read_text(encoding="utf-8") == json.dumps(BIG, ensure_ascii=False, default=str)

    msg = SimpleNamespace(content_json={"tool_name": "file_grep", "output": BIG, "status": "ok", **meta})
    rendered = loop._render_tool_result(msg)

    assert rendered is not None
    assert "свёрнуто:" in rendered and f"ref={REF}" in rendered and "tool_output_read" in rendered
    omitted = meta["folded_chars"] - loop.FOLD_HEAD_CHARS - loop.FOLD_TAIL_CHARS
    assert f"start={loop.FOLD_HEAD_CHARS}, end={loop.FOLD_HEAD_CHARS + omitted}" in rendered
    assert len(rendered) < 3000
    assert len(rendered) < len(json.dumps(BIG)) * 0.7


def test_render_is_deterministic(tmp_path):
    meta = loop._fold_tool_output(str(tmp_path), "task-1", REF, BIG)
    msg = SimpleNamespace(content_json={"tool_name": "x", "output": BIG, "status": "ok", **meta})
    assert loop._render_tool_result(msg) == loop._render_tool_result(msg)


def test_short_output_is_not_folded(tmp_path):
    assert loop._fold_tool_output(str(tmp_path), "task-1", REF, {"ok": True}) == {}
    assert not (tmp_path / ".tool_output").exists()

    rendered = loop._render_tool_result(
        SimpleNamespace(content_json={"tool_name": "x", "output": {"ok": True}, "status": "ok"})
    )
    assert "свёрнуто" not in rendered


@pytest.mark.anyio
async def test_tool_output_read_slices_and_caps(tmp_path):
    directory = tmp_path / ".tool_output" / "task-1"
    directory.mkdir(parents=True)
    text = "".join(str(i % 10) for i in range(10_000))
    (directory / f"{REF}.txt").write_text(text, encoding="utf-8")

    part = await files_handler.tool_output_read({"ref": REF, "start": 5, "end": 15}, str(tmp_path), task_id="task-1")
    assert part["content"] == text[5:15] and part["total"] == len(text)

    capped = await files_handler.tool_output_read({"ref": REF, "start": 0, "end": 999_999}, str(tmp_path), task_id="task-1")
    assert len(capped["content"]) == 4000 and capped["truncated"] is True


@pytest.mark.anyio
async def test_tool_output_read_rejects_bad_refs_and_context(tmp_path):
    assert (await files_handler.tool_output_read({"ref": "../../etc/passwd"}, str(tmp_path), task_id="task-1"))["error"] == "invalid ref"
    assert (await files_handler.tool_output_read({"ref": REF}, str(tmp_path), task_id=None))["error"] == "no task context"
    assert (await files_handler.tool_output_read({"ref": REF}, str(tmp_path), task_id="task-1"))["error"] == "output not found"


def test_tool_is_registered_read_only():
    spec = registry.get_tool("tool_output_read")
    assert spec.requires_approval is False
    assert "general" in spec.allowed_roles
