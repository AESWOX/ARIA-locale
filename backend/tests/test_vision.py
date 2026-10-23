"""Tests for vision_analyze handler (B2/v13).

Offline: mocks urllib.request.urlopen to test request/response cycle.
Live smoke: in live_smoke.py (Блок C).
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from aria.tools.handlers.vision import vision_analyze, _detect_mime

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
TEST_IMAGE = FIXTURE_DIR / "test_image.png"

SAMPLE_GEMINI_RESPONSE = {
    "choices": [{"message": {"content": "This is a red square pixel."}}],
    "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
}

SAMPLE_ERROR_RESPONSE = json.dumps({"error": {"message": "API key not valid"}})


class TestVisionMimeDetection:
    def test_detect_png(self):
        with open(TEST_IMAGE, "rb") as f:
            data = f.read()
        assert _detect_mime(data) == "image/png"

    def test_detect_unsupported(self):
        assert _detect_mime(b"not an image").startswith("error")


class TestVisionAnalyzeOffline:
    async def test_no_input_returns_error(self):
        result = await vision_analyze({})
        assert "error" in result

    async def test_file_not_found_returns_error(self):
        result = await vision_analyze({"file_path": "/nonexistent/image.png"})
        assert "error" in result

    async def test_missing_both_params_returns_error(self):
        result = await vision_analyze({"prompt": "desc"})
        assert "error" in result

    async def test_successful_analysis_with_file_path(self):
        """Mock the Gemini API call and verify response is parsed correctly."""
        with open(TEST_IMAGE, "rb") as f:
            img_data = f.read()
        b64 = base64.b64encode(img_data).decode("ascii")

        with patch("aria.secrets.secret_provider.get_key_list", return_value=["fake-key"]):
            with patch("urllib.request.urlopen", new_callable=MagicMock) as mock:
                mock.return_value.__enter__.return_value.read.return_value = json.dumps(SAMPLE_GEMINI_RESPONSE).encode("utf-8")
                result = await vision_analyze({"file_path": str(TEST_IMAGE)})

        assert "text" in result
        assert result["text"] == "This is a red square pixel."
        assert result["model"] == "gemini-3.8-flash"

    async def test_successful_analysis_with_base64(self):
        with open(TEST_IMAGE, "rb") as f:
            img_data = f.read()
        b64 = base64.b64encode(img_data).decode("ascii")

        with patch("aria.secrets.secret_provider.get_key_list", return_value=["fake-key"]):
            with patch("urllib.request.urlopen", new_callable=MagicMock) as mock:
                mock.return_value.__enter__.return_value.read.return_value = json.dumps(SAMPLE_GEMINI_RESPONSE).encode("utf-8")
                result = await vision_analyze({"image_base64": b64})

        assert "text" in result
        assert result["text"] == "This is a red square pixel."

    async def test_http_error_triggers_key_rotation(self):
        from urllib.error import HTTPError
        with patch("aria.secrets.secret_provider.get_key_list", return_value=["bad-key", "bad-key-2"]):
            with patch("urllib.request.urlopen", side_effect=[
                HTTPError("http://test", 429, "Too Many Requests", {}, None),
                HTTPError("http://test", 429, "Too Many Requests", {}, None),
            ]):
                result = await vision_analyze({"file_path": str(TEST_IMAGE)})

        assert "error" in result
        assert "429" in result["error"] or "Too Many" in result.get("error", "")

    async def test_no_keys_returns_error(self):
        with patch("aria.secrets.secret_provider.get_key_list", return_value=[]):
            result = await vision_analyze({"file_path": str(TEST_IMAGE)})
        assert "error" in result


# ── DeepSeek Flash как бэкенд vision (deepseek-flash принимает изображения, deepseek-v4-pro нет) ──────────────

import pytest  # noqa: E402
from urllib.error import HTTPError  # noqa: E402

SAMPLE_DEEPSEEK_RESPONSE = {
    "choices": [{"message": {"content": "A red square."}}],
    "usage": {"prompt_tokens": 400, "completion_tokens": 5, "total_tokens": 405},
}


def _ok(payload):
    resp = MagicMock()
    resp.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")
    return resp


@pytest.fixture
def deepseek_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek-vision-1234567890")


class TestVisionDeepSeekBackend:
    async def test_single_deepseek_key_is_enough(self, deepseek_key):
        with patch("aria.secrets.secret_provider.get_key_list", return_value=[]):
            with patch("urllib.request.urlopen", return_value=_ok(SAMPLE_DEEPSEEK_RESPONSE)) as mock:
                result = await vision_analyze({"file_path": str(TEST_IMAGE), "prompt": "what is it"})
        assert result["text"] == "A red square."
        assert result["provider"] == "deepseek" and result["model"] == "deepseek-flash"
        req = mock.call_args.args[0]
        assert req.full_url.endswith("/chat/completions") and "deepseek" in req.full_url
        assert req.get_header("Authorization") == "Bearer sk-test-deepseek-vision-1234567890"
        body = json.loads(req.data.decode("utf-8"))
        assert body["model"] == "deepseek-flash", "v4-pro изображения не принимает"
        assert body["thinking"] == {"type": "disabled"}, "thinking по умолчанию съел бы лимит токенов"
        parts = body["messages"][0]["content"]
        assert parts[0] == {"type": "text", "text": "what is it"}
        assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")

    async def test_gemini_stays_first_when_its_key_exists(self, deepseek_key):
        with patch("aria.secrets.secret_provider.get_key_list", return_value=["gem-key"]):
            with patch("urllib.request.urlopen", return_value=_ok(SAMPLE_GEMINI_RESPONSE)) as mock:
                result = await vision_analyze({"file_path": str(TEST_IMAGE)})
        assert result["provider"] == "gemini"
        assert mock.call_count == 1

    async def test_falls_back_to_deepseek_when_gemini_is_rate_limited(self, deepseek_key):
        with patch("aria.secrets.secret_provider.get_key_list", return_value=["gem-key"]):
            with patch("urllib.request.urlopen", side_effect=[
                HTTPError("http://test", 429, "Too Many Requests", {}, None),
                _ok(SAMPLE_DEEPSEEK_RESPONSE),
            ]) as mock:
                result = await vision_analyze({"file_path": str(TEST_IMAGE)})
        assert result["provider"] == "deepseek" and result["text"] == "A red square."
        assert mock.call_count == 2

    async def test_both_backends_failing_reports_the_last_error(self, deepseek_key):
        with patch("aria.secrets.secret_provider.get_key_list", return_value=["gem-key"]):
            with patch("urllib.request.urlopen", side_effect=[
                HTTPError("http://test", 429, "Too Many Requests", {}, None),
                HTTPError("http://test", 402, "Payment Required", {}, None),
            ]):
                result = await vision_analyze({"file_path": str(TEST_IMAGE)})
        assert "error" in result and "402" in result["error"]

    async def test_empty_deepseek_reply_is_an_error_not_an_empty_answer(self, deepseek_key):
        with patch("aria.secrets.secret_provider.get_key_list", return_value=[]):
            with patch("urllib.request.urlopen", return_value=_ok({"choices": [{"message": {"content": ""}}]})):
                result = await vision_analyze({"file_path": str(TEST_IMAGE)})
        assert "error" in result and "empty" in result["error"]

    async def test_formats_deepseek_cannot_read_are_rejected_without_a_request(self, deepseek_key):
        tiff = base64.b64encode(b"II*\x00" + b"\x00" * 64).decode("ascii")
        with patch("aria.secrets.secret_provider.get_key_list", return_value=[]):
            with patch("urllib.request.urlopen") as mock:
                result = await vision_analyze({"image_base64": tiff})
        assert "error" in result and "PNG/JPEG/WebP/GIF" in result["error"]
        mock.assert_not_called()

    async def test_no_keys_at_all_names_every_option(self, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        with patch("aria.secrets.secret_provider.get_key_list", return_value=[]):
            result = await vision_analyze({"file_path": str(TEST_IMAGE)})
        assert "DEEPSEEK_API_KEY" in result["error"] and "GEMINI_API_KEYS" in result["error"]

    def test_vision_model_is_flash_not_pro(self):
        from aria.config import get_settings

        assert get_settings().deepseek_vision_model == "deepseek-flash"
        assert "pro" not in get_settings().deepseek_vision_model
