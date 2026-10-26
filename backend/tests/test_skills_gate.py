"""Проверка навыков в сборке и смоуке: гейт перед PyInstaller, стадия skills, строка навыка плагина."""
from __future__ import annotations

import importlib.util
import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


gate = _load("check_release_skills")
smoke = _load("e2e_smoke")
rebuild = _load("rebuild_and_smoke")


def _skill(root: Path, name: str, body: str = "---\nname: x\ndescription: d\n---\n# x\n") -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    return d


# ── гейт перед сборкой ──────────────────────────────────────────────────

def test_gate_strict_fails_when_an_allowlisted_skill_is_absent(tmp_path):
    _skill(tmp_path, "aria-agent")
    problems, _ = gate.check(tmp_path, ["aria-agent", "obsidian"], strict=True, root=tmp_path)
    assert any(p.startswith("obsidian:") for p in problems)
    # без --strict прежнее поведение: пропуск, не ошибка
    problems, notes = gate.check(tmp_path, ["aria-agent", "obsidian"], strict=False, root=tmp_path)
    assert problems == [] and any("obsidian" in n for n in notes)


def test_gate_strict_fails_on_folder_without_skill_md(tmp_path):
    (tmp_path / "obsidian").mkdir()
    problems, _ = gate.check(tmp_path, ["obsidian"], strict=True, root=tmp_path)
    assert any("no SKILL.md" in p for p in problems)


def test_gate_flags_secrets_and_internal_addresses(tmp_path):
    _skill(tmp_path, "aria-agent", "---\nname: a\n---\nkey: sk-abcdefghijklmnopqrstuvwx\nhost 192.168.1.20\npath C:\\Users\\bob\\x\n")
    problems, _ = gate.check(tmp_path, ["aria-agent"], strict=True, root=tmp_path)
    labels = " ".join(problems)
    assert "known key prefix" in labels and "ip address" in labels and "windows user path" in labels


def test_gate_clean_skill_passes(tmp_path):
    _skill(tmp_path, "aria-agent")
    assert gate.check(tmp_path, ["aria-agent"], strict=True, root=tmp_path) == ([], [])


def test_gate_main_private_build_is_not_enforced(monkeypatch, capsys):
    monkeypatch.setenv("ARIA_BUNDLE_ALL_SKILLS", "1")
    monkeypatch.setattr(gate, "ROOT", Path("/nonexistent-root"))
    monkeypatch.setattr(gate, "load_allowlist", lambda root=None: ["aria-agent"])
    assert gate.main(["--strict"]) == 0
    assert "NOT a release build" in capsys.readouterr().out


def test_real_allowlist_is_parsed_and_non_empty():
    allow = gate.load_allowlist()
    assert {"aria-agent", "aria-knowledge", "obsidian"} <= set(allow)
    assert smoke.release_allowlist() == allow  # смоук и гейт читают один и тот же файл одинаково


# ── фаза rebuild ────────────────────────────────────────────────────────

def test_skills_gate_runs_before_pyinstaller_and_only_when_building():
    names = [n for n, _ in rebuild.PHASES]
    assert names.index("skills_src") < names.index("pyinstaller")
    base = dict(skip_tests=False, skip_rehearsal=False, skip_build=False, no_install=False, live_only=False)
    assert rebuild.enabled("skills_src", Namespace(**base))
    assert not rebuild.enabled("skills_src", Namespace(**{**base, "skip_build": True}))
    assert not rebuild.enabled("skills_src", Namespace(**{**base, "live_only": True}))


def test_skills_phase_dry_run_prints_the_strict_command(tmp_path, capsys):
    ctx = rebuild.Ctx(args=Namespace(dry_run=True), outdir=tmp_path)
    assert rebuild.ph_skills_src(ctx) == "dry-run"
    assert "check_release_skills.py --strict" in capsys.readouterr().out


# ── стадия skills смоука ────────────────────────────────────────────────

ALLOW = ["aria-agent", "aria-knowledge", "obsidian"]


def _rows(*names):
    return [{"name": n, "description": "", "category": "general", "enabled": True} for n in names]


def test_listing_missing_bundled_skill_is_a_problem():
    problems, _ = smoke.check_skill_listing(_rows("aria-agent", "obsidian"), ALLOW, exact=False)
    assert problems and "aria-knowledge" in problems[0]


def test_listing_fresh_profile_must_be_exactly_the_allowlist():
    problems, _ = smoke.check_skill_listing(_rows(*ALLOW, "private-workflow"), ALLOW, exact=True)
    assert problems and "private-workflow" in problems[0]
    assert smoke.check_skill_listing(_rows(*ALLOW), ALLOW, exact=True) == ([], [])


def test_listing_leftover_e2e_rows_fail_in_fresh_profile_but_only_warn_on_real_one():
    rows = _rows(*ALLOW, "e2e1a2b3c-hello")
    problems, _ = smoke.check_skill_listing(rows, ALLOW, exact=True)
    assert problems and "e2e1a2b3c-hello" in problems[0]
    problems, warnings = smoke.check_skill_listing(rows + _rows("my-own-skill"), ALLOW, exact=False)
    assert problems == [] and warnings and "e2e1a2b3c-hello" in warnings[0]


def test_listing_junk_instead_of_a_list_is_missing_everything():
    problems, _ = smoke.check_skill_listing({"detail": "boom"}, ALLOW, exact=False)
    assert problems


class _Api:
    def __init__(self, listing, content_status=200, content="# skill"):
        self.listing, self.content_status, self.content = listing, content_status, content

    def ok(self, method, path, body=None, **kw):
        assert (method, path) == ("GET", "/skills")
        return self.listing

    def call(self, method, path, body=None, **kw):
        assert path.startswith("/skills/content?name=")
        return self.content_status, ({"content": self.content} if self.content_status == 200 else {"detail": "nope"})


def _stage(api, mode="real"):
    return smoke.s_skills(Namespace(api=api, args=Namespace(skills=mode)))


def test_stage_passes_on_a_healthy_profile():
    _stage(_Api(_rows(*smoke.release_allowlist())), "exact")


def test_stage_fails_when_skill_content_is_unreadable_or_empty():
    allow = smoke.release_allowlist()
    with pytest.raises(AssertionError, match="not readable"):
        _stage(_Api(_rows(*allow), content_status=404))
    with pytest.raises(AssertionError, match="not readable"):
        _stage(_Api(_rows(*allow), content="   "))


def test_stage_is_skipped_for_source_runs_where_nothing_is_bundled():
    with pytest.raises(smoke.Skip):
        _stage(_Api([]), "none")  # профиль репетиции из исходников пуст: это не ошибка сборки


def test_stage_in_exact_mode_catches_a_private_skill_leaking_into_the_bundle():
    allow = smoke.release_allowlist()
    with pytest.raises(AssertionError, match="private-workflow"):
        _stage(_Api(_rows(*allow, "private-workflow")), "exact")


def test_stage_is_registered_before_plugin_and_rehearsal_passes_the_mode():
    names = [n for n, _, _ in smoke.STAGES]
    assert names.index("skills") < names.index("plugin")
    src = (SCRIPTS / "e2e_rehearsal.py").read_text(encoding="utf-8")
    assert '"--skills", "exact" if cli.exe else "none"' in src
