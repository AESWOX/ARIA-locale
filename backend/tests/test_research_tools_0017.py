"""0017 «исследование кода»: file_grep, чтение по строкам, исключения и порядок в file_search, корень проекта только для чтения."""
from __future__ import annotations

import asyncio

import pytest

from aria.tools.handlers import files as fh
from aria.tools.handlers.files import PathEscapeError
from aria.tools.registry import TOOL_REGISTRY


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def proj(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "build" / "x").mkdir(parents=True)
    (root / "node_modules" / "m").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    (root / "src" / "main.cpp").write_text("#include <x>\nint main() {\n  parse_args(argc);\n  return 0;\n}\n", encoding="utf-8")
    (root / "src" / "util.h").write_text("void parse_args(int n);\n", encoding="utf-8")
    (root / "src" / "ru.txt").write_bytes("Ошибка разбора аргументов\n".encode("cp1251"))
    (root / "build" / "x" / "junk.cpp").write_text("parse_args junk\n", encoding="utf-8")
    (root / "node_modules" / "m" / "i.js").write_text("parse_args dep\n", encoding="utf-8")
    (root / ".git" / "config").write_text("parse_args git\n", encoding="utf-8")
    (root / "src" / "blob.bin").write_bytes(b"parse_args\0\1\2")
    return root


def test_grep_returns_path_line_and_skips_junk(proj):
    out = run(fh.file_grep({"pattern": "parse_args"}, str(proj)))
    got = [(m["path"], m["line"]) for m in out["matches"]]
    assert got == [("src/main.cpp", 3), ("src/util.h", 1)]  # без build/, node_modules/, .git/, бинарников
    assert not out["truncated"]


def test_grep_is_literal_by_default_and_regex_on_request(proj):
    lit = run(fh.file_grep({"pattern": "main()"}, str(proj)))
    assert [(m["path"], m["line"]) for m in lit["matches"]] == [("src/main.cpp", 2)]
    rx = run(fh.file_grep({"pattern": r"parse_\w+\(", "regex": True}, str(proj)))
    assert len(rx["matches"]) == 2
    assert "invalid regex" in run(fh.file_grep({"pattern": "(", "regex": True}, str(proj)))["error"]
    assert "error" in run(fh.file_grep({"pattern": ""}, str(proj)))


def test_grep_glob_path_case_and_context(proj):
    only_h = run(fh.file_grep({"pattern": "parse_args", "glob": "**/*.h"}, str(proj)))
    assert [m["path"] for m in only_h["matches"]] == ["src/util.h"]
    cs = run(fh.file_grep({"pattern": "PARSE_ARGS", "case_sensitive": True}, str(proj)))
    assert cs["matches"] == []
    ctx = run(fh.file_grep({"pattern": "return 0", "context": 1}, str(proj)))["matches"][0]
    assert ctx["context"][0].startswith("3|") and ctx["context"][-1].startswith("5|")
    sub = run(fh.file_grep({"pattern": "parse_args", "path": "src"}, str(proj)))
    assert len(sub["matches"]) == 2


def test_grep_reads_russian_cp1251_and_limits_results(proj):
    assert run(fh.file_grep({"pattern": "разбора"}, str(proj)))["matches"][0]["path"] == "src/ru.txt"
    (proj / "many.txt").write_text("\n".join("hit" for _ in range(50)), encoding="utf-8")
    out = run(fh.file_grep({"pattern": "hit", "max_results": 10}, str(proj)))
    assert len(out["matches"]) == 10 and out["truncated"] is True


def test_grep_cannot_leave_the_root(proj, tmp_path):
    with pytest.raises(PathEscapeError):
        run(fh.file_grep({"pattern": "x", "glob": "../*"}, str(proj)))
    with pytest.raises(PathEscapeError):
        run(fh.file_grep({"pattern": "x", "path": "../"}, str(proj)))
    outside = tmp_path / "secret.txt"
    outside.write_text("parse_args secret\n", encoding="utf-8")
    try:
        (proj / "src" / "link.txt").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    out = run(fh.file_grep({"pattern": "secret"}, str(proj)))
    assert out["matches"] == []


def test_read_range_has_line_numbers_and_total(tmp_path):
    # write_bytes: на Windows write_text превратил бы \n в \r\n и сломал проверку «без диапазона»
    (tmp_path / "f.txt").write_bytes("\n".join(f"line{i}" for i in range(1, 1001)).encode("utf-8"))
    out = run(fh.file_read({"path": "f.txt", "start_line": 998, "end_line": 1000}, str(tmp_path)))
    assert out["content"].splitlines() == [" 998| line998", " 999| line999", "1000| line1000"]
    assert (out["start_line"], out["end_line"], out["total_lines"]) == (998, 1000, 1000)
    assert out["truncated"] is False
    part = run(fh.file_read({"path": "f.txt", "start_line": 1, "end_line": 5}, str(tmp_path)))
    assert part["truncated"] is True
    past = run(fh.file_read({"path": "f.txt", "start_line": 5000}, str(tmp_path)))
    assert "error" in past
    # без диапазона поведение прежнее
    assert run(fh.file_read({"path": "f.txt"}, str(tmp_path)))["content"].startswith("line1\n")
    # файл с CRLF: номера строк на месте, \r в ответ не попадает
    (tmp_path / "crlf.txt").write_bytes(b"a\r\nb\r\n")
    crlf = run(fh.file_read({"path": "crlf.txt", "start_line": 1, "end_line": 2}, str(tmp_path)))
    assert crlf["content"] == "1| a\n2| b" and crlf["total_lines"] == 2


def test_read_big_file_reports_truncation(tmp_path):
    (tmp_path / "big.txt").write_text("x" * 300_000, encoding="utf-8")
    out = run(fh.file_read({"path": "big.txt"}, str(tmp_path)))
    assert len(out["content"]) == 200_000 and out["truncated"] is True and "hint" in out


def test_search_skips_junk_and_is_sorted(proj):
    def posix(items):  # на Windows file_search по-прежнему отдаёт пути с обратным слэшем
        return [p.replace("\\", "/") for p in items]

    allf = posix(run(fh.file_search({"glob": "**/*"}, str(proj)))["matches"])
    assert allf == sorted(allf, key=str.lower)
    assert allf[0] == "README.md"
    assert not any(p.startswith(("build/", "node_modules/", ".git/")) for p in allf)
    again = posix(run(fh.file_search({"glob": "**/*"}, str(proj)))["matches"])
    assert allf == again
    everything = posix(run(fh.file_search({"glob": "**/*", "include_ignored": True}, str(proj)))["matches"])
    assert any(p.startswith("build/") for p in everything)


def test_project_scope_off_by_default_then_read_only(proj, tmp_path, monkeypatch):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    grep = TOOL_REGISTRY["file_grep"].handler
    read = TOOL_REGISTRY["file_read"].handler
    res = run(grep(input_json={"pattern": "parse_args", "scope": "project"}, timeout_sec=5, sandbox_root=str(sandbox)))
    assert "not set" in res["error"]
    from aria.config import get_settings

    monkeypatch.setattr(get_settings(), "agent_project_root", str(proj))
    res = run(grep(input_json={"pattern": "parse_args", "scope": "project"}, timeout_sec=5, sandbox_root=str(sandbox)))
    assert [m["path"] for m in res["matches"]] == ["src/main.cpp", "src/util.h"]
    got = run(read(input_json={"path": "src/util.h", "scope": "project"}, timeout_sec=5, sandbox_root=str(sandbox)))
    assert "parse_args" in got["content"]
    with pytest.raises(PathEscapeError):  # из корня проекта не выйти ни наверх, ни в песочницу
        run(read(input_json={"path": "../x", "scope": "project"}, timeout_sec=5, sandbox_root=str(sandbox)))
    assert "unknown scope" in run(read(input_json={"path": "a", "scope": "root"}, timeout_sec=5, sandbox_root=str(sandbox)))["error"]


def test_writing_tools_have_no_project_scope():
    # корень проекта получают только читающие инструменты: запись и shell всегда в песочнице
    for name in ("file_write", "shell_execute"):
        assert "scope" not in TOOL_REGISTRY[name].input_schema.get("properties", {})
    for name in ("file_read", "file_search", "file_grep"):
        assert TOOL_REGISTRY[name].requires_approval is False
