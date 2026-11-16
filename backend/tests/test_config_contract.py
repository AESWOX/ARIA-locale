import unittest

from aria.config import get_settings


class ConfigContractTests(unittest.TestCase):
    def test_canonical_defaults_present(self):
        s = get_settings()
        self.assertEqual(s.loop_max_iterations, 15)
        self.assertEqual(s.audit_max_attempts, 3)
        self.assertEqual(s.delegate_max_parallel, 3)
        self.assertEqual(s.security_auto_lock_minutes, 15)
        self.assertEqual(s.budget_warn_threshold_pct, 80)
        self.assertEqual(s.budget_block_threshold_pct, 100)


def test_legacy_codex_settings_in_env_file_are_ignored(tmp_path):
    from aria.config import Settings

    env = tmp_path / ".env"
    env.write_text(
        "CODEX_ENABLED=true\n"
        "CODEX_SANDBOX_MODE=danger-full-access\n"
        "CODEX_WORKSPACE_DIR=/tmp/project\n"
        "DEEPSEEK_API_KEY=sk-legacy\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=str(env))

    assert not hasattr(settings, "codex_enabled")
    assert not any(name.startswith("codex_") for name in settings.model_fields)
    assert settings.deepseek_api_key == "sk-legacy"


if __name__ == '__main__':
    unittest.main()
