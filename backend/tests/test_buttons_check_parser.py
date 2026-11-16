"""aria_buttons_check.py: разбор api.ts не должен ломаться молча (иначе отчёт про «мёртвые кнопки» врёт)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
API_TS = Path(__file__).resolve().parents[2] / "desktop" / "src" / "lib" / "api.ts"


def _load():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("aria_buttons_check", SCRIPTS / "aria_buttons_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_parse_api_ts_finds_methods_and_strips_query_suffix():
    mod = _load()
    parsed = mod.parse_api_ts("""export const api = {
  getA: () => fetchJSON(`/api/skills${profileQuery(p)}`),
  delB: (id: string) => fetchJSON(`/api/sessions/${encodeURIComponent(id)}`, { method: "DELETE" }),
};""")
    assert parsed["getA"] == [("GET", "/skills")]
    assert parsed["delB"] == [("DELETE", "/sessions/{}")]


def test_real_api_ts_is_parsed_and_matches_most_routes():
    mod = _load()
    parsed = mod.parse_api_ts(API_TS.read_text(encoding="utf-8"))
    assert len(parsed) > 100
    from aria.main import app

    matchers = mod.openapi_matchers(app.openapi())
    missing = [n for n, calls in parsed.items() for m, p in calls if mod.match_route(matchers, m, p)[0] == "missing"]
    assert missing == [], f"вызовы api.ts без маршрута на бэкенде: {missing}"
