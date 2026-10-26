"""First-run wizard: UI-1 (overlapping button labels) and UI-5 (key links per provider).

There is no frontend test runner yet (T-18), so the UI-1 guard is a static contract on
the component source: the kit Button uses line-height 0, a wrapped label is drawn on top
of itself, so every footer button must opt out of wrapping and shrinking.
"""
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.routers import setup as setup_mod

WIZARD = Path(__file__).resolve().parents[2] / "desktop" / "src" / "components" / "SetupWizard.tsx"


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(setup_mod.router)
    app.dependency_overrides[require_runtime_token] = lambda: "test"
    return TestClient(app)


def test_get_a_key_link_matches_each_provider(client):
    urls = {p["id"]: p["url"] for p in client.get("/setup/providers").json()}
    assert urls == {
        "gemini": "https://aistudio.google.com/app/apikey",
        "groq": "https://console.groq.com/keys",
        "deepseek": "https://platform.deepseek.com/api_keys",
    }


def _footer_buttons(src: str) -> list[str]:
    footer = src[src.index("flex flex-wrap items-center justify-between"):]
    return re.findall(r"<Button\b[^>]*>", footer)


def test_footer_buttons_never_wrap_or_shrink():
    src = WIZARD.read_text(encoding="utf-8")
    buttons = _footer_buttons(src)
    assert len(buttons) >= 6, buttons
    for tag in buttons:
        assert "FOOTER_BTN" in tag or "whitespace-nowrap" in tag, tag
    assert re.search(r'FOOTER_BTN\s*=\s*"[^"]*whitespace-nowrap[^"]*shrink-0', src)


def test_footer_rows_can_wrap_instead_of_squeezing_buttons():
    src = WIZARD.read_text(encoding="utf-8")
    assert "flex flex-wrap items-center justify-between" in src
    assert "flex flex-wrap items-center gap-2" in src
