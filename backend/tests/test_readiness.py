"""Readiness uses isolated storage and mocked providers, never live requests."""
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet

from invoiceclear.api import InvoiceHandler, create_server
from invoiceclear.config import Settings
from invoiceclear.documents import DomainError
from invoiceclear.readiness import Readiness


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="invoiceclear-readiness-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(database_path=self.root / "private-workspace.sqlite3", web_path=self.root,
                                 mode="private", email="finance@example.test", password="unique-workspace-password",
                                 public_url="https://api.example.test", frontend_url="https://finance.example.test",
                                 allowed_origins=("https://finance.example.test",), backups_enabled=False,
                                 token_key=Fernet.generate_key().decode(), qbo_client_id="private-qbo-client-id",
                                 qbo_client_secret="private-qbo-client-secret", google_client_id="private-google-client-id",
                                 google_client_secret="private-google-client-secret", provider="together",
                                 together_key="private-together-secret", together_model="configured-model", qbo_po_field_id="1")
        with patch("invoiceclear.api.ThreadingHTTPServer.__init__", return_value=None):
            self.server = create_server(self.settings)
        self.ready = self.server.readiness
        self.network = patch.object(self.server.connections.opener, "open", side_effect=AssertionError("Live provider requests are forbidden in tests"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.addCleanup(self.server.maintenance.close)
        self.addCleanup(self.server.capture.close)

    def grant(self, provider, account=None):
        account = account or ("12345" if provider == "qbo" else "finance@example.test")
        grant = {"access_token": "private-access-token", "refresh_token": "private-refresh-token",
                 "expires": time.time() + 3600, "account": account, "account_id": account}
        self.server.connections._save(provider, grant)
        return grant

    def call(self, path, *, method="GET", token=True, origin="https://finance.example.test"):
        handler = InvoiceHandler.__new__(InvoiceHandler)
        handler.server, handler.path, handler.command = self.server, path, method
        handler.headers = {"Host": "api.example.test", "Origin": origin}
        if token:
            grant = self.server.sessions.create(self.settings.email, self.settings.password)
            handler.headers["Authorization"] = "Bearer " + grant["access_token"]
        handler._json = Mock()
        handler._body = Mock(return_value={})
        getattr(handler, "do_" + method)()
        self.assertEqual(handler._json.call_count, 1)
        args = handler._json.call_args.args
        return args[0], args[1] if len(args) > 1 else 200

    def test_setup_is_honest_secret_free_and_get_performs_no_external_requests(self):
        payload = self.ready.snapshot()
        self.assertEqual(payload["mode"], "private")
        self.assertTrue(payload["external_checks_allowed"])
        self.assertEqual(payload["verification_scope"], "process")
        self.assertEqual(payload["storage"]["database"]["status"], "ok")
        self.assertEqual(payload["storage"]["backups"], {"enabled": False, "status": "disabled"})
        self.assertFalse(payload["capabilities"]["ocr"])
        self.assertTrue(payload["capabilities"]["human_review_required"])
        for provider in ("qbo", "gmail", "together"):
            item = payload["providers"][provider]
            self.assertTrue(item["configured"])
            self.assertFalse(item["connected"])
            self.assertEqual(item["verification"]["status"], "not_checked")
            self.assertIsNone(item["verification"]["checked_at"])
            self.assertEqual(item["missing_environment"], [])
        self.assertEqual(payload["providers"]["qbo"]["callback_url"], "https://api.example.test/api/connections/qbo/callback")
        self.assertEqual(payload["providers"]["gmail"]["callback_url"], "https://api.example.test/api/connections/gmail/callback")
        self.assertIsNone(payload["providers"]["together"]["callback_url"])
        self.assertEqual(payload["providers"]["together"]["model"], "configured-model")
        self.assertEqual(payload["usage"]["call_limit"], 100)
        self.assertEqual(payload["usage"]["token_limit"], 500000)
        rendered = json.dumps(payload)
        for value in (str(self.root), self.settings.database_path.name, self.settings.password, self.settings.token_key,
                      self.settings.qbo_client_id, self.settings.qbo_client_secret, self.settings.google_client_id,
                      self.settings.google_client_secret, self.settings.together_key):
            self.assertNotIn(value, rendered)
        self.server.connections.opener.open.assert_not_called()

    def test_actual_connection_is_distinct_from_explicit_access_check(self):
        self.grant("qbo")
        item = self.ready.snapshot()["providers"]["qbo"]
        self.assertTrue(item["connected"])
        self.assertEqual(item["verification"]["status"], "not_checked")
        with patch.object(self.server.connections, "qbo", return_value={"QueryResponse": {"Invoice": [{"Id": "7"}]}}) as query:
            checked = self.ready.verify("qbo")
        self.assertEqual(checked["verification"]["status"], "passed")
        self.assertTrue(checked["verification"]["checked_at"].endswith("Z"))
        query.assert_called_once()
        self.assertEqual(query.call_args.args[0], "GET")
        self.assertIn("SELECT+Id+FROM+Invoice+MAXRESULTS+1", query.call_args.args[1])
        self.assertEqual(query.call_args.kwargs["expected_account"], "12345")
        self.assertEqual(self.server.service.list_invoices()["total"], 0)
        with self.server.service._connection() as connection:
            for table in ("audit", "documents", "external_actions", "external_invoices", "external_messages"):
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)

    def test_empty_qbo_company_can_pass_read_access_without_importing(self):
        self.grant("qbo")
        with patch.object(self.server.connections, "qbo", return_value={"QueryResponse": {}}):
            self.assertEqual(self.ready.verify("qbo")["verification"]["status"], "passed")

    def test_invalid_qbo_response_cannot_claim_invoice_read_access(self):
        self.grant("qbo")
        for response in ({"QueryResponse": {"Invoice": "raw response"}}, {"QueryResponse": {"Invoice": ["unknown"]}}, {}):
            self.ready._attempts.clear()
            with patch.object(self.server.connections, "qbo", return_value=response):
                with self.assertRaises(DomainError):
                    self.ready.verify("qbo")
            self.assertEqual(self.ready.snapshot()["providers"]["qbo"]["verification"]["status"], "failed")

    def test_gmail_checks_current_mailbox_identity_and_sanitizes_errors(self):
        self.grant("gmail")
        with patch.object(self.server.connections, "gmail", return_value={"emailAddress": "other@example.test"}) as profile:
            with self.assertRaises(DomainError) as error:
                self.ready.verify("gmail")
        self.assertEqual(error.exception.code, "account_changed")
        profile.assert_called_once_with("GET", "profile", expected_account="finance@example.test")
        item = self.ready.snapshot()["providers"]["gmail"]
        self.assertEqual(item["verification"]["status"], "failed")
        self.ready._attempts.clear()
        with patch.object(self.server.connections, "gmail", side_effect=RuntimeError("secret raw provider body")):
            with self.assertRaises(DomainError) as error:
                self.ready.verify("gmail")
        self.assertNotIn("secret", error.exception.message)
        self.assertNotIn("secret", json.dumps(self.ready.snapshot()))

    def test_verified_outcome_resets_when_account_config_or_process_changes(self):
        grant = self.grant("gmail")
        with patch.object(self.server.connections, "gmail", return_value={"emailAddress": "FINANCE@example.test"}):
            self.ready.verify("gmail")
        self.assertEqual(Readiness(self.server).snapshot()["providers"]["gmail"]["verification"]["status"], "not_checked")
        self.server.connections._save("gmail", dict(grant, account="another@example.test", account_id="another@example.test"))
        self.assertEqual(self.ready.snapshot()["providers"]["gmail"]["verification"]["status"], "not_checked")
        self.server.connections._save("gmail", grant)
        self.server.connections._clear("gmail")
        self.assertEqual(self.ready.snapshot()["providers"]["gmail"]["verification"]["status"], "not_checked")

    def test_together_probe_uses_shared_service_budget_and_remains_unreviewed(self):
        fields = {"po_number": "IC-CONNECTION-CHECK", "customer": "InvoiceClear Connection Check", "currency": "USD", "amount": "1.00"}
        result = {"fields": fields, "evidence": {name: {"page": 1, "snippet": value} for name, value in fields.items()}, "error": None}
        with patch.object(self.server.service, "probe_extraction", create=True, return_value=result) as probe:
            checked = self.ready.verify("together")
        probe.assert_called_once_with()
        self.assertTrue(checked["connected"])
        self.assertEqual(checked["verification"]["status"], "passed")
        self.assertEqual(self.server.service.list_invoices()["total"], 0)
        with self.server.service._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audit").fetchone()[0], 0)
        self.server.settings = replace(self.settings, together_key="rotated-private-key")
        self.assertEqual(self.ready.snapshot()["providers"]["together"]["verification"]["status"], "not_checked")
        self.assertFalse(self.ready.snapshot()["providers"]["together"]["connected"])

    def test_together_failed_evidence_budget_limit_and_public_mode_never_claim_verified(self):
        failures = ({"fields": {}, "evidence": {}, "error": "unsupported evidence"},
                    {"fields": {"po_number": "invented"}, "evidence": {}, "error": None})
        for result in failures:
            self.ready._attempts.clear()
            with patch.object(self.server.service, "probe_extraction", create=True, return_value=result):
                with self.assertRaises(DomainError) as error:
                    self.ready.verify("together")
            self.assertEqual(error.exception.code, "verification_failed")
            self.assertFalse(self.ready.snapshot()["providers"]["together"]["connected"])
        self.ready._attempts.clear()
        with patch.object(self.server.service, "probe_extraction", create=True, side_effect=DomainError("provider_usage_limit", "Daily extraction budget reached.", 429)):
            with self.assertRaises(DomainError) as error:
                self.ready.verify("together")
        self.assertEqual(error.exception.status, 429)
        self.server.settings = replace(self.settings, mode="demo")
        with patch.object(self.server.service, "probe_extraction", create=True) as probe:
            with self.assertRaises(DomainError) as error:
                self.ready.verify("together")
        self.assertEqual(error.exception.status, 403)
        probe.assert_not_called()

    def test_missing_unconnected_expired_and_public_modes_never_probe(self):
        for provider in ("qbo", "gmail"):
            with self.assertRaises(DomainError) as error:
                self.ready.verify(provider)
            self.assertEqual(error.exception.code, "connection_required")
        self.server.settings = replace(self.settings, provider="rules", together_key=None)
        with self.assertRaises(DomainError) as error:
            self.ready.verify("together")
        self.assertEqual(error.exception.code, "connection_unconfigured")
        item = self.ready.snapshot()["providers"]["together"]
        self.assertEqual(item["missing_environment"], ["INVOICECLEAR_PROVIDER", "TOGETHER_API_KEY"])
        grant = self.grant("qbo")
        self.server.connections._save("qbo", dict(grant, refresh_expires=time.time() - 1))
        with patch.object(self.server.connections, "qbo") as query:
            with self.assertRaises(DomainError):
                self.ready.verify("qbo")
        query.assert_not_called()
        self.server.settings = replace(self.settings, mode="demo")
        for provider in ("qbo", "gmail", "together"):
            with self.assertRaises(DomainError) as error:
                self.ready.verify(provider)
            self.assertEqual(error.exception.status, 403)
        self.server.connections.opener.open.assert_not_called()

    def test_checks_have_cooldown_and_bound_concurrent_dispatch(self):
        self.grant("qbo")
        with patch.object(self.server.connections, "qbo", return_value={"QueryResponse": {}}) as query:
            self.ready.verify("qbo")
            with self.assertRaises(DomainError) as error:
                self.ready.verify("qbo")
            self.assertEqual(error.exception.code, "verification_cooldown")
            self.assertEqual(query.call_count, 1)
        self.ready._lock.acquire()
        try:
            with self.assertRaises(DomainError) as error:
                self.ready.verify("gmail")
            self.assertEqual(error.exception.code, "verification_busy")
        finally:
            self.ready._lock.release()

    def test_unavailable_storage_returns_safe_degraded_status_without_recreating(self):
        self.settings.database_path.unlink()
        payload = self.ready.snapshot()
        self.assertEqual(payload["storage"]["database"]["status"], "degraded")
        self.assertEqual(payload["usage"]["status"], "unavailable")
        self.assertIsNone(payload["usage"]["calls"])
        self.assertNotIn(str(self.root), json.dumps(payload))
        self.assertFalse(self.settings.database_path.exists())

    def test_readiness_verify_and_history_keep_session_and_exact_origin_guards(self):
        for path, method in (("/api/readiness", "GET"), ("/api/invoices/example/history", "GET"),
                             ("/api/connections/together/verify", "POST")):
            self.assertEqual(self.call(path, method=method, token=False)[1], 401)
            self.assertEqual(self.call(path, method=method, origin="https://foreign.example.test")[1], 403)
        self.assertEqual(self.call("/api/readiness")[1], 200)
        self.server.settings = replace(self.settings, mode="demo")
        self.assertEqual(self.call("/api/connections/together/verify", method="POST")[1], 403)
        self.server.settings = self.settings
        with patch.object(self.server.service, "history", create=True, return_value={"events": [], "has_more": False, "next_before": None}) as history:
            self.assertEqual(self.call("/api/invoices/example/history?limit=7&before=100")[1], 200)
            history.assert_called_once_with("example", limit=7, before=100)
            history.reset_mock()
            self.call("/api/invoices/example/history")
            history.assert_called_once_with("example", limit=20, before=None)
            for suffix in ("limit=0", "limit=101", "limit=1&limit=2", "limit=", "before=", "before=0", "before=1&before=2", "before=9223372036854775808"):
                self.assertEqual(self.call("/api/invoices/example/history?" + suffix)[1], 400)

    def test_brief_download_keeps_authentication_and_uses_approved_service_artifact(self):
        path = "/api/invoices/example/brief"
        self.assertEqual(self.call(path, token=False)[1], 401)
        self.assertEqual(self.call(path, origin="https://foreign.example.test")[1], 403)
        handler = InvoiceHandler.__new__(InvoiceHandler)
        handler.server, handler.path, handler.command = self.server, path, "GET"
        handler._check_origin, handler._token, handler._send = Mock(), Mock(), Mock()
        with patch.object(self.server.service, "handoff_brief", create=True, return_value=(b"<html>Approved brief</html>", "billing-brief.html")) as brief:
            handler.do_GET()
        brief.assert_called_once_with("example")
        handler._send.assert_called_once_with(b"<html>Approved brief</html>", "text/html; charset=utf-8", filename="billing-brief.html")


if __name__ == "__main__":
    unittest.main()
