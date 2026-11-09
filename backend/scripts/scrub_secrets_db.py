"""SEC-6: разовая зачистка уже сохранённых секретов в local_agent.db.

Запускает владелец при закрытом приложении:
    python backend/scripts/scrub_secrets_db.py [--db PATH] [--dry-run]

Перед изменением рядом создаётся резервная копия ``local_agent.db.bak-<stamp>``.
Печатаются только счётчики, значения секретов не выводятся.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aria import secretguard  # noqa: E402


def default_db() -> Path:
    import os

    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / "ARIA" / "local_agent.db"
    return ROOT / "data" / "local_agent.db"


def _mask_json(raw: str | None) -> tuple[str | None, bool]:
    if not raw:
        return raw, False
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        masked = secretguard.mask_secrets(raw)
        return masked, masked != raw
    masked_obj = secretguard.mask_obj(parsed)
    if masked_obj == parsed:
        return raw, False
    return json.dumps(masked_obj, ensure_ascii=False), True


def _mask_text(raw: str | None) -> tuple[str | None, bool]:
    if not raw:
        return raw, False
    masked = secretguard.mask_secrets(raw)
    return masked, masked != raw


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0] for row in rows}


def scrub(db_path: Path, *, dry_run: bool = False) -> dict[str, int]:
    counts: dict[str, int] = {}
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        tables = _tables(conn)
        jobs: list[tuple[str, str, str, bool]] = []
        if "messages" in tables:
            jobs.append(("messages", "content", "text", False))
            jobs.append(("messages", "content_json", "json", True))
        if "tool_calls" in tables:
            jobs.append(("tool_calls", "input_json", "json", True))
            jobs.append(("tool_calls", "output_json", "json", True))
            jobs.append(("tool_calls", "error_message", "text", False))
        if "sessions" in tables:
            jobs.append(("sessions", "title", "text", False))
        for table, column, kind, is_json in jobs:
            rows = conn.execute(f"SELECT rowid, {column} FROM {table} WHERE {column} IS NOT NULL").fetchall()
            changed = 0
            for rowid, value in rows:
                if is_json:
                    if not isinstance(value, str):
                        continue
                    masked, dirty = _mask_json(value)
                else:
                    masked, dirty = _mask_text(value)
                if not dirty:
                    continue
                changed += 1
                if not dry_run:
                    conn.execute(f"UPDATE {table} SET {column} = ? WHERE rowid = ?", (masked, rowid))
            counts[f"{table}.{column}"] = changed
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mask stored secrets in local_agent.db")
    parser.add_argument("--db", default=str(default_db()), help="path to local_agent.db")
    parser.add_argument("--dry-run", action="store_true", help="count only, do not modify")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"database not found: {db_path}")
        return 1

    backup: Path | None = None
    if not args.dry_run:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = db_path.with_name(f"{db_path.name}.bak-{stamp}")
        shutil.copy2(db_path, backup)

    counts = scrub(db_path, dry_run=args.dry_run)
    total = sum(counts.values())
    for name, count in counts.items():
        print(f"{name}: {count}")
    print(f"total rows masked: {total}")
    if backup is not None:
        print(f"backup: {backup}")
    elif args.dry_run:
        print("dry run: nothing changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
