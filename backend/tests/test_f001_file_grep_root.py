"""F-001: file_grep отдаёт фактический root и честно отказывает на внешний путь."""
from __future__ import annotations

import pytest
from aria.tools.handlers import files as files_handler


@pytest.mark.anyio
async def test_file_grep_reports_actual_root(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text("def calculate_total(items):\n    return sum(items)\n", encoding="utf-8")

    out = await files_handler.file_grep({"pattern": "calculate_total"}, str(root))

    assert out["root"] == str(root.resolve())
    assert out["files_scanned"] == 1
    assert out["matches"] and out["matches"][0]["path"] == "app.py"
    assert out["matches"][0]["line"] == 1


@pytest.mark.anyio
async def test_file_grep_external_path_is_unavailable(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "app.py").write_text("def calculate_total(items):\n    return sum(items)\n", encoding="utf-8")
    outside = tmp_path / "fixture"
    outside.mkdir()
    (outside / "other.py").write_text("calculate_total = 1\n", encoding="utf-8")

    out = await files_handler.file_grep({"pattern": "calculate_total", "path": str(outside)}, str(root))

    assert out["root"] == str(root.resolve())
    assert out["files_scanned"] == 0
    assert "outside the readable root" in out["error"]
    assert str(outside) in out["error"]
