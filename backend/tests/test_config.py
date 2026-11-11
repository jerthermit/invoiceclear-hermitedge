import os
import unittest
from pathlib import Path
from unittest.mock import patch

from invoiceclear.config import DEFAULT_TOGETHER_MODEL, Settings, together_parameters


class ConfigTests(unittest.TestCase):
    def environment(self, **values):
        return patch.dict(os.environ, values, clear=True)

    def test_backups_default_on_and_accept_explicit_boolean_values(self):
        with self.environment():
            self.assertTrue(Settings.from_env().backups_enabled)
        for value, expected in (("true", True), ("false", False), (" TRUE ", True), (" FALSE ", False)):
            with self.subTest(value=value), self.environment(INVOICECLEAR_BACKUPS_ENABLED=value):
                self.assertIs(Settings.from_env().backups_enabled, expected)

    def test_backups_reject_ambiguous_values(self):
        for value in ("", "0", "1", "yes", "no", "enabled", "treu"):
            with self.subTest(value=value), self.environment(INVOICECLEAR_BACKUPS_ENABLED=value):
                with self.assertRaisesRegex(ValueError, "INVOICECLEAR_BACKUPS_ENABLED must be true or false"):
                    Settings.from_env()

    def test_railway_uses_assigned_port_and_container_host(self):
        with self.environment(RAILWAY_SERVICE_ID="test-service", PORT="9234", INVOICECLEAR_DB_PATH="/data/workspace.sqlite3"):
            settings = Settings.from_env()
        self.assertEqual(settings.host, "0.0.0.0")
        self.assertEqual(settings.port, 9234)
        self.assertEqual(settings.database_path, Path("/data/workspace.sqlite3"))

    def test_active_example_preserves_railway_binding_and_assigned_port(self):
        backend = Path(__file__).resolve().parents[1]
        example = {}
        for line in (backend / ".env.example").read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                name, value = line.split("=", 1)
                example[name] = value
        self.assertNotIn("INVOICECLEAR_HOST", example)
        self.assertNotIn("INVOICECLEAR_PORT", example)
        self.assertNotIn("RAILWAY_RUN_UID", example)
        with self.environment(**example, RAILWAY_SERVICE_ID="test-service", PORT="9234",
                              INVOICECLEAR_DB_PATH="/data/invoiceclear-demo.sqlite3"):
            settings = Settings.from_env()
        self.assertEqual(settings.host, "0.0.0.0")
        self.assertEqual(settings.port, 9234)
        self.assertEqual(settings.database_path, Path("/data/invoiceclear-demo.sqlite3"))

    def test_container_defaults_to_non_root_bootstrap_and_persistent_storage(self):
        backend = Path(__file__).resolve().parents[1]
        dockerfile = (backend / "Dockerfile").read_text()
        self.assertTrue(dockerfile.startswith("FROM python:3.11-slim\n"))
        self.assertEqual((backend / ".python-version").read_text().strip(), "3.11")
        self.assertIn("INVOICECLEAR_DB_PATH=/data/invoiceclear.sqlite3", dockerfile)
        self.assertIn("COPY run.py start.py ./", dockerfile)
        self.assertIn("USER invoiceclear\n", dockerfile)
        self.assertIn('CMD ["python", "start.py"]', dockerfile)
        self.assertIn("chown invoiceclear:invoiceclear /data", dockerfile)
        self.assertNotIn("chown -R", dockerfile)
        self.assertNotIn("/app /data", dockerfile)

    def test_private_credentials_and_exact_origins_are_required(self):
        with self.environment(INVOICECLEAR_MODE="private"):
            with self.assertRaisesRegex(ValueError, "Private mode"):
                Settings.from_env()
        with self.environment(INVOICECLEAR_ALLOWED_ORIGINS="https://*.example.com"):
            with self.assertRaisesRegex(ValueError, "exact http or https origins"):
                Settings.from_env()
        with self.environment(INVOICECLEAR_ALLOWED_ORIGINS="https://finance.example.com/, https://finance.example.com"):
            self.assertEqual(Settings.from_env().allowed_origins, ("https://finance.example.com",))

    def test_key_enables_investigation_without_changing_extraction(self):
        with self.environment():
            self.assertFalse(Settings.from_env().agent_enabled)
        with self.environment(TOGETHER_API_KEY=" test-key ", TOGETHER_MODEL="configured-tool-model"):
            settings = Settings.from_env()
        self.assertTrue(settings.agent_enabled)
        self.assertEqual(settings.provider, "rules")
        self.assertEqual(settings.together_model, "configured-tool-model")
        self.assertEqual(settings.together_key, "test-key")

    def test_blank_key_and_explicit_disable_keep_investigation_off(self):
        with self.environment(TOGETHER_API_KEY="   "):
            self.assertFalse(Settings.from_env().agent_enabled)
        with self.environment(TOGETHER_API_KEY="test-key", INVOICECLEAR_AGENT_ENABLED=" FALSE "):
            self.assertFalse(Settings.from_env().agent_enabled)
        with self.environment(TOGETHER_API_KEY="test-key", INVOICECLEAR_PROVIDER="together",
                              INVOICECLEAR_AGENT_ENABLED="false"):
            settings = Settings.from_env()
            self.assertFalse(settings.agent_enabled)
            self.assertEqual(settings.provider, "together")

    def test_investigator_rejects_ambiguous_opt_in_or_missing_configuration(self):
        for value in ("", "1", "yes", "on"):
            with self.subTest(value=value), self.environment(INVOICECLEAR_AGENT_ENABLED=value):
                with self.assertRaisesRegex(ValueError, "INVOICECLEAR_AGENT_ENABLED"):
                    Settings.from_env()
        for key in ("", "   "):
            with self.subTest(key=key), self.environment(INVOICECLEAR_AGENT_ENABLED="true",
                                                        TOGETHER_MODEL="configured-tool-model", TOGETHER_API_KEY=key):
                with self.assertRaisesRegex(ValueError, "TOGETHER_API_KEY"):
                    Settings.from_env()

    def test_agent_defaults_to_the_pinned_function_calling_model_with_key(self):
        with self.environment(TOGETHER_API_KEY="test-key"):
            settings = Settings.from_env()
        self.assertEqual(DEFAULT_TOGETHER_MODEL, "zai-org/GLM-5.3-Flash")
        self.assertEqual(settings.together_model, DEFAULT_TOGETHER_MODEL)
        self.assertTrue(settings.agent_enabled)
        with self.environment(INVOICECLEAR_AGENT_ENABLED="true", TOGETHER_API_KEY="test-key", TOGETHER_MODEL=""):
            with self.assertRaisesRegex(ValueError, "tool-calling model"):
                Settings.from_env()

    def test_model_request_parameters_follow_the_configured_family(self):
        self.assertEqual(together_parameters(DEFAULT_TOGETHER_MODEL), {
            "temperature": 1.0, "top_p": 0.95, "reasoning_effort": "low",
        })
        self.assertEqual(together_parameters("custom-model"), {"temperature": 0})


if __name__ == "__main__":
    unittest.main()
