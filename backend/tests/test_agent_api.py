"""Investigation HTTP boundaries; isolated storage, no live provider calls."""
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from dataclasses import replace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from invoiceclear.api import create_server
from invoiceclear.config import Settings
from invoiceclear.service import Service
from test_investigator import Completion, tool


class InvestigationApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="invoiceclear-agent-api-")
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(database_path=Path(self.temp.name) / "public.sqlite3",
                                 web_path=Path(self.temp.name), port=0, backups_enabled=False)
        self.start()
        self.addCleanup(self.stop)

    def start(self):
        self.server = create_server(self.settings)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)
        self.token = self.request("/api/session", {"email": self.settings.email, "password": self.settings.password}, auth=False)["access_token"]
        self.invoice = self.request("/api/invoices")["invoices"][0]
        self.path = "/api/invoices/" + self.invoice["id"]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, path, data=None, *, auth=True, origin=None, status=200):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + self.token
        if origin:
            headers["Origin"] = origin
        body = None if data is None else json.dumps(data).encode()
        try:
            response = urlopen(Request(self.base + path, data=body, headers=headers), timeout=5)
        except HTTPError as error:
            response = error
        with response:
            result = json.loads(response.read())
            self.assertEqual(response.status, status, result)
            return result

    def test_authentication_and_origin_guard_reads_and_investigation(self):
        with patch.object(self.server.investigator, "investigate") as run:
            self.request(self.path + "/investigation", auth=False, status=401)
            self.request(self.path + "/investigate", {}, auth=False, status=401)
            for suffix, body in (("/investigation", None), ("/investigate", {})):
                self.request(self.path + suffix, body, origin="https://untrusted.example", status=403)
            run.assert_not_called()

    def test_disabled_agent_never_fabricates_a_run_or_calls_provider(self):
        with patch("urllib.request.urlopen", wraps=urlopen) as transport:
            state = self.request(self.path + "/investigation")
            detail = self.request(self.path)
            result = self.request(self.path + "/investigate", {
                "expected_version": self.invoice["version"], "idempotency_key": "disabled-run",
            }, status=409)
        self.assertFalse(state["available"])
        self.assertIsNone(state["latest"])
        self.assertEqual(detail["investigation"], state)
        self.assertTrue(result["error"]["code"])
        transport.assert_not_called()
        with self.server.service._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0], 0)

    def test_route_passes_only_the_authenticated_case_and_actor(self):
        body = {"expected_version": self.invoice["version"], "idempotency_key": "route-test"}
        response = {"id": "run-route-test", "status": "abstained", "summary": "More evidence needed."}
        with patch.object(self.server.investigator, "investigate", return_value=response) as run:
            self.assertEqual(self.request(self.path + "/investigate", body), response)
        run.assert_called_once_with(self.invoice["id"], body, self.settings.email)
        with patch.object(self.server.investigator, "investigate") as run:
            self.request(self.path + "/investigate", {"extra": "x" * 16_000}, status=413)
            run.assert_not_called()

    def test_legacy_database_adds_investigation_storage_without_resetting_invoices(self):
        path = Path(self.temp.name) / "legacy.sqlite3"
        original = Service(path, demo=True)
        invoices = original.list_invoices(view="all")
        with sqlite3.connect(path) as connection:
            connection.execute("DROP TABLE agent_runs")
        reopened = Service(path, demo=True)
        self.assertEqual(reopened.list_invoices(view="all"), invoices)
        self.assertEqual(reopened.health()["status"], "ok")
        with reopened._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0], 0)

    def test_key_only_startup_exposes_a_deliberate_tool_loop_without_background_calls(self):
        with patch.dict(os.environ, {"TOGETHER_API_KEY": "isolated-test-key",
                                    "INVOICECLEAR_DB_PATH": str(self.settings.database_path),
                                    "INVOICECLEAR_BACKUPS_ENABLED": "false"}, clear=True):
            self.settings = replace(Settings.from_env(), port=0)
        self.stop()
        args = {"status": "completed", "summary": "A purchase order is missing.", "next_step": "request_po",
                "draft": "Please provide the purchase order.", "citations": [{"source_id": "invoice", "excerpt": self.invoice["invoice_number"]}]}
        responses = [Completion([tool("read_invoice", call_id="invoice"), tool("read_checks", call_id="checks")]),
                     Completion([tool("finish_investigation", args, "finish")])]
        body = {"expected_version": self.invoice["version"], "idempotency_key": "http-investigation"}
        with patch("invoiceclear.investigator.urllib.request.urlopen", side_effect=responses) as dispatch:
            self.start()
            before = self.server.service.get_invoice(self.invoice["id"])
            detail = self.request(self.path)
            readiness = self.request("/api/readiness")
            self.assertTrue(detail["investigation"]["available"])
            self.assertIsNone(detail["investigation"]["latest"])
            self.assertTrue(readiness["investigation"]["available"])
            self.assertFalse(readiness["providers"]["together"]["selected"])
            self.assertEqual(self.settings.provider, "rules")
            dispatch.assert_not_called()
            result = self.request(self.path + "/investigate", body)
            duplicate = self.request(self.path + "/investigate", body)
            snapshot = self.request(self.path + "/investigation")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(duplicate, result)
        self.assertEqual(snapshot["latest"], result)
        self.assertEqual(dispatch.call_count, 2)
        self.assertEqual(self.server.service.get_invoice(self.invoice["id"]), before)


if __name__ == "__main__":
    unittest.main()
