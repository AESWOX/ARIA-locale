"""Release skills gate: the public installer ships exactly the skills in release_skills.txt, and none of
them contains something that looks like a secret or an internal address.

  python scripts/check_release_skills.py            # scan: allowlisted skills present on this machine
  python scripts/check_release_skills.py --strict   # build gate: every allowlisted skill must exist
                                                    # (folder with SKILL.md); run by rebuild_and_smoke.py
                                                    # before PyInstaller

A private build (ARIA_BUNDLE_ALL_SKILLS=1) bundles everything, so --strict only scans there and says so.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "api key assignment": re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}"),
    "known key prefix": re.compile(r"\b(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_\-]{20,}|gsk_[A-Za-z0-9]{20,})"),
    "ip address": re.compile(r"\b(?!127\.|0\.0\.0\.0|10\.0\.0\.)\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
    "windows user path": re.compile(r"(?i)[A-Z]:\\Users\\[^\\\s]+"),
}


def load_allowlist(root: Path = ROOT) -> list[str]:
    text = (root / "release_skills.txt").read_text(encoding="utf-8")
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def scan_skill(folder: Path, root: Path = ROOT) -> list[str]:
    """Findings for one skill folder: '<relative path>: <label>'."""
    found: list[str] = []
    for f in sorted(folder.rglob("*")):
        if not f.is_file() or f.stat().st_size > 2_000_000:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for label, rx in PATTERNS.items():
            if rx.search(text):
                try:
                    shown = f.relative_to(root)
                except ValueError:
                    shown = f
                found.append(f"{shown}: {label}")
    return found


def check(skills_dir: Path, allow: list[str], *, strict: bool, root: Path = ROOT) -> tuple[list[str], list[str]]:
    """Returns (problems, notes). Without strict a missing skill is a note, with strict a problem."""
    problems: list[str] = []
    notes: list[str] = []
    for name in allow:
        folder = skills_dir / name
        if not folder.is_dir():
            (problems if strict else notes).append(
                f"{name}: allowlisted skill is not present in {skills_dir}" if strict else f"[skip] {name}: not present on this machine"
            )
            continue
        if strict and not (folder / "SKILL.md").is_file():
            problems.append(f"{name}: folder has no SKILL.md (the installer would ship an empty skill)")
        problems.extend(scan_skill(folder, root))
    return problems, notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strict", action="store_true", help="every allowlisted skill must be present with a SKILL.md")
    args = ap.parse_args(argv)
    allow = load_allowlist()
    strict = args.strict
    if strict and os.environ.get("ARIA_BUNDLE_ALL_SKILLS") == "1":
        print("[note] ARIA_BUNDLE_ALL_SKILLS=1: private build, allowlist presence is not enforced (this is NOT a release build)")
        strict = False
    problems, notes = check(ROOT / "data" / "skills", allow, strict=strict)
    for line in notes:
        print(line)
    for line in problems:
        print(f"[FAIL] {line}")
    if problems:
        print(f"{len(problems)} finding(s)")
        return 1
    print("OK: allowlisted skills are clean" + (f" ({len(allow)} required skills present)" if strict else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
