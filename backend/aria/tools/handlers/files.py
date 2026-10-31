from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from aria.textio import read_text_auto


class PathEscapeError(Exception):
    pass


def _resolve_within(base: Path, relative: str) -> Path:
    base = base.resolve()
    target = (base / relative).resolve()
    # is_relative_to, а не startswith: «/tmp/sandbox_evil» не внутри «/tmp/sandbox» (D8)
    if not target.is_relative_to(base):
        raise PathEscapeError(f"path {relative} escapes sandbox root {base}")
    return target


def _has_git_component(rel: Path) -> bool:
    return any(part.lower() == ".git" for part in rel.parts)


# Папки, в которых «исходников» нет: сборка, зависимости, кэш, история git.
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "build", "dist", "out", "target", "bin", "obj",
    "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".idea", ".vs", ".gradle",
    "cmake-build-debug", "cmake-build-release", ".next", ".cache",
})
MAX_SEARCH_RESULTS = 500
MAX_GREP_RESULTS = 200
MAX_GREP_FILE_BYTES = 2_000_000
MAX_GREP_LINE_CHARS = 240
MAX_READ_CHARS = 200_000
MAX_READ_LINES = 2000


def _is_skipped(rel: Path) -> bool:
    return any(part in SKIP_DIRS for part in rel.parts[:-1]) or rel.name in {".DS_Store"}


def _looks_binary(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return b"\0" in fh.read(8192)
    except OSError:
        return True


def _glob_match(rel_posix: str, pattern: str) -> bool:
    pat = pattern.replace("\\", "/")
    if "/" not in pat:
        return fnmatch.fnmatch(rel_posix.rsplit("/", 1)[-1], pat)
    if fnmatch.fnmatch(rel_posix, pat):
        return True
    return pat.startswith("**/") and fnmatch.fnmatch(rel_posix, pat[3:])


def _check_glob(pattern: object, root: Path) -> str:
    norm = str(pattern).replace("\\", "/")
    if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm) or ".." in norm.split("/"):
        raise PathEscapeError(f"glob {pattern!r} выходит за корень песочницы {root}")
    return norm


def _as_int(value: object, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default


async def file_read(input_json: dict, sandbox_root: str) -> dict:
    path = _resolve_within(Path(sandbox_root), input_json["path"])
    if not path.exists():
        return {"exists": False, "content": None}
    if path.is_dir():
        return {"exists": True, "is_dir": True, "entries": sorted(p.name for p in path.iterdir())}
    content = read_text_auto(path)
    start = _as_int(input_json.get("start_line"))
    end = _as_int(input_json.get("end_line"))
    if start is None and end is None:
        # прежний режим: файл целиком (без номеров строк), но теперь с признаком обрезки
        out = {"exists": True, "is_dir": False, "content": content[:MAX_READ_CHARS]}
        if len(content) > MAX_READ_CHARS:
            out["truncated"] = True
            out["total_lines"] = content.count("\n") + 1
            out["hint"] = "файл обрезан: читай по диапазону строк (start_line/end_line) или ищи file_grep"
        return out
    lines = content.splitlines()
    total = len(lines)
    first = max(start or 1, 1)
    last = min(end if end is not None else first + MAX_READ_LINES - 1, total)
    if last - first + 1 > MAX_READ_LINES:
        last = first + MAX_READ_LINES - 1
    if first > total:
        return {"exists": True, "is_dir": False, "content": "", "start_line": first, "end_line": first - 1,
                "total_lines": total, "error": f"start_line {first} за концом файла ({total} строк)"}
    width = len(str(last))
    body = "\n".join(f"{n:>{width}}| {lines[n - 1]}" for n in range(first, last + 1))
    return {
        "exists": True, "is_dir": False, "content": body[:MAX_READ_CHARS],
        "start_line": first, "end_line": last, "total_lines": total,
        "truncated": last < total,
    }


async def file_write(input_json: dict, sandbox_root: str) -> dict:
    path = _resolve_within(Path(sandbox_root), input_json["path"])
    # .git/hooks и .git/config — это исполнение кода при ближайшем `git commit`: запись туда только вручную
    if _has_git_component(path.relative_to(Path(sandbox_root).resolve())):
        raise PathEscapeError("запись внутрь .git запрещена (хуки и конфиг git исполняют код)")
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = input_json.get("mode", "overwrite")
    content = input_json.get("content", "")

    # hash_before: только если файл уже существует
    import hashlib
    hash_before = None
    if path.exists():
        hash_before = hashlib.sha256(path.read_bytes()).hexdigest()

    if mode == "append":
        with path.open("a", encoding="utf-8") as fh:
            fh.write(content)
    else:
        path.write_text(content, encoding="utf-8")

    # hash_after: всегда
    hash_after = hashlib.sha256(path.read_bytes()).hexdigest()

    return {
        "path": str(path.relative_to(Path(sandbox_root).resolve())),
        "bytes_written": len(content),
        "hash_before": hash_before,
        "hash_after": hash_after,
    }


async def file_search(input_json: dict, sandbox_root: str) -> dict:
    root = Path(sandbox_root).resolve()
    norm = _check_glob(input_json.get("glob", "**/*"), root)
    include_all = bool(input_json.get("include_ignored"))
    matches: list[str] = []
    for p in root.glob(norm):
        # симлинк внутри корня не должен вести наружу
        if not (p.is_file() and p.resolve().is_relative_to(root)):
            continue
        rel = p.relative_to(root)
        if not include_all and _is_skipped(rel):
            continue
        matches.append(rel.as_posix() if os.sep == "/" else str(rel))
    matches.sort(key=lambda s: s.replace("\\", "/").lower())
    return {"matches": matches[:MAX_SEARCH_RESULTS], "truncated": len(matches) > MAX_SEARCH_RESULTS,
            "total": len(matches)}


async def file_grep(input_json: dict, sandbox_root: str) -> dict:
    """Поиск текста (или regex) по содержимому файлов. Ответ: path, line, text."""
    root = Path(sandbox_root).resolve()
    pattern = str(input_json.get("pattern") or "")
    if not pattern:
        return {"error": "pattern is empty"}
    use_regex = bool(input_json.get("regex"))
    flags = 0 if input_json.get("case_sensitive") else re.IGNORECASE
    try:
        rx = re.compile(pattern if use_regex else re.escape(pattern), flags)
    except re.error as exc:
        return {"error": f"invalid regex: {exc}"}
    glob = _check_glob(input_json.get("glob") or "**/*", root)
    limit = min(max(_as_int(input_json.get("max_results"), 100) or 100, 1), MAX_GREP_RESULTS)
    ctx = min(max(_as_int(input_json.get("context"), 0) or 0, 0), 5)
    sub = input_json.get("path")
    base = _resolve_within(root, sub) if sub else root
    if not base.is_dir():
        return {"error": f"path is not a directory: {sub}"}

    matches: list[dict] = []
    files_scanned = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            if not _glob_match(rel, glob):
                continue
            try:
                if full.is_symlink() and not full.resolve().is_relative_to(root):
                    continue
                if full.stat().st_size > MAX_GREP_FILE_BYTES or _looks_binary(full):
                    continue
                text = read_text_auto(full)
            except (OSError, ValueError):
                continue
            files_scanned += 1
            lines = text.splitlines()
            for i, line in enumerate(lines, start=1):
                if rx.search(line):
                    if len(matches) >= limit:
                        truncated = True
                        break
                    item = {"path": rel, "line": i, "text": line.strip()[:MAX_GREP_LINE_CHARS]}
                    if ctx:
                        lo, hi = max(i - ctx, 1), min(i + ctx, len(lines))
                        item["context"] = [f"{n}| {lines[n - 1][:MAX_GREP_LINE_CHARS]}" for n in range(lo, hi + 1)]
                    matches.append(item)
            if truncated:
                break
        if truncated:
            break
    return {"matches": matches, "truncated": truncated, "files_scanned": files_scanned}


MAX_TOOL_OUTPUT_READ = 4000
_REF_RE = re.compile(r"^[0-9a-fA-F-]{8,36}$")


async def tool_output_read(input_json: dict, sandbox_root: str | None = None, task_id=None, **_ctx) -> dict:
    """E6: чтение полного вывода инструмента из sandbox/.tool_output/<task_id>/<ref>.txt (только чтение)."""
    ref = str(input_json.get("ref") or "")
    if not _REF_RE.fullmatch(ref):
        return {"error": "invalid ref"}
    if task_id is None:
        return {"error": "no task context"}
    root = (Path(sandbox_root or ".").resolve() / ".tool_output" / str(task_id))
    target = (root / f"{ref}.txt").resolve()
    if not target.is_relative_to(root):
        return {"error": "ref outside .tool_output"}
    if not target.exists():
        return {"error": "output not found"}
    text = read_text_auto(target)
    start = max(_as_int(input_json.get("start")) or 0, 0)
    end = _as_int(input_json.get("end"))
    limit = MAX_TOOL_OUTPUT_READ if end is None else min(max(end - start, 1), MAX_TOOL_OUTPUT_READ)
    chunk = text[start:start + limit]
    return {
        "ref": ref,
        "start": start,
        "end": start + len(chunk),
        "total": len(text),
        "content": chunk,
        "truncated": start + len(chunk) < len(text),
    }
