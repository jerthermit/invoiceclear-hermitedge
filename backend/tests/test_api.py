"""Isolated HTTP checks; never use a user's database or external provider."""
import base64
import csv
import io
import json
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from invoiceclear.api import InvoiceHandler, create_server
from invoiceclear.config import Settings
from invoiceclear.connected import ConnectedWorkflow
from test_connected import FakeConnections


class ApiTests(unittest.TestCase):
    def test_accept_queue_covers_the_bounded_parallel_release_workload(self):
        self.assertGreaterEqual(self.server.request_queue_size, 8)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="invoiceclear-api-")
        self.root = Path(self.temp.name)
        self.web = self.root / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text("<h1>InvoiceClear</h1>", encoding="utf-8")
        (self.root / "secret.txt").write_text("outside-web-root", encoding="utf-8")
        self.start()
        self.token = self.login()

    def start(self, mode="private", password="isolated-test-password"):
        self.settings = Settings(database_path=self.root / f"{mode}.sqlite", web_path=self.web,
                                 mode=mode, email="finance@example.test", password=password, port=0)
        self.server = create_server(self.settings)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def tearDown(self):
        self.stop()
        self.temp.cleanup()

    def request(self, path, data=None, token=True, headers=None, status=200):
        outgoing = dict(headers or {})
        if token and getattr(self, "token", None):
            outgoing["Authorization"] = f"Bearer {self.token}"
        body = None if data is None else json.dumps(data).encode()
        if body is not None:
            outgoing["Content-Type"] = "application/json"
        request = Request(self.base + path, data=body, headers=outgoing)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            result = json.loads(raw) if "application/json" in response.headers.get("Content-Type", "") else raw
            expected = (status,) if isinstance(status, int) else status
            self.assertIn(response.status, expected, result)
            if response.status >= 400 and path.startswith("/api/"):
                self.assertIsInstance(result, dict)
                self.assertTrue(result["error"]["code"])
                self.assertTrue(result["error"]["message"])
            return result

    def login(self, password="isolated-test-password"):
        return self.request("/api/session", {"email": "finance@example.test", "password": password},
                            token=False)["access_token"]

    def invoice(self, number="INV-TEST"):
        data = {"invoice_number": number, "customer": "Example Staffing", "customer_email": "ap@example.test",
                "amount": "100.00", "currency": "USD", "issue_date": "2026-08-01",
                "due_date": "2026-08-31", "po_reference": "PO-TEST", "source_ref": "manual:test"}
        return self.request("/api/invoices", data, status=(200, 201))

    def action(self, invoice, action, parameters, key, status=200, **extra):
        body = {"action": action, "parameters": parameters, "expected_version": invoice["version"],
                "idempotency_key": key, **extra}
        return self.request(f"/api/invoices/{invoice['id']}/actions", body, status=status)

    def upload(self, invoice, key="upload-test", status=(200, 201), **extra):
        content = b"PO Number: PO-TEST\nCustomer: Example Staffing\nCurrency: USD\nAmount: 100.00\n"
        body = {"filename": "purchase-order.txt", "content_base64": base64.b64encode(content).decode(),
                "expected_version": invoice["version"], "idempotency_key": key, **extra}
        result = self.request(f"/api/invoices/{invoice['id']}/documents", body, status=status)
        return result, content

    def test_private_runtime_and_business_authentication(self):
        self.assertEqual(self.request("/api/runtime", token=False), {"mode": "private"})
        self.request("/api/health", token=False)
        self.assertEqual(self.request("/api/invoices")["total"], 0)
        invoice = self.invoice()
        for path in ("/api/invoices", f"/api/invoices/{invoice['id']}",
                     f"/api/invoices/{invoice['id']}/request", f"/api/invoices/{invoice['id']}/handoff", "/api/export.csv"):
            self.request(path, token=False, status=401)
        for path in ("/api/invoices", "/api/invoices/import", f"/api/invoices/{invoice['id']}/documents",
                     f"/api/invoices/{invoice['id']}/actions"):
            self.request(path, {}, token=False, status=401)
        self.request("/api/session", {"email": "finance@example.test", "password": "wrong"}, token=False, status=401)

    def test_connected_routes_require_workspace_authentication(self):
        invoice = self.invoice()
        self.request("/api/connections", token=False, status=401)
        status = self.request("/api/connections")
        self.assertEqual(set(status), {"qbo", "gmail"})
        self.assertFalse(status["qbo"]["configured"])
        self.assertFalse(status["gmail"]["connected"])
        routes = ("/api/connections/qbo/start", "/api/connections/gmail/start",
                  "/api/connections/qbo/disconnect", "/api/connections/qbo/sync")
        for path in routes:
            self.request(path, {}, token=False, status=401)
        for route in ("send-request", "sync-replies", "apply-reference", "refresh-accounting", "reconcile-request"):
            self.request(f"/api/invoices/{invoice['id']}/{route}", {}, token=False, status=401)
        self.request("/api/connections/qbo/start", {}, status=(400, 403, 409))
        self.assertEqual(self.request(f"/api/invoices/{invoice['id']}")["integration"]["accounting"], None)

    def test_capture_requires_auth_origin_and_explicit_private_opt_in(self):
        self.request("/api/capture", token=False, status=401)
        self.request("/api/capture", {"enabled": True}, token=False, status=401)
        initial = self.request("/api/capture")
        self.assertFalse(initial["enabled"])
        self.assertFalse(initial["available"])
        self.request("/api/capture", {"enabled": True}, status=409)
        self.request("/api/capture", {"enabled": "yes"}, status=400)
        self.request("/api/capture", {"enabled": True, "approve": True}, status=400)
        self.request("/api/capture", {"enabled": True}, headers={"Origin": "https://untrusted.example"}, status=403)
        fake = FakeConnections()
        self.server.connections = fake
        self.server.connected = ConnectedWorkflow(self.server.service, fake, self.settings)
        self.assertTrue(self.request("/api/capture")["available"])
        self.assertTrue(self.request("/api/capture", {"enabled": True})["enabled"])
        self.assertFalse(self.request("/api/capture", {"enabled": False})["enabled"])
        self.assertEqual(fake.calls, [])  # No confirmed request threads exist.

    def test_public_capture_is_unavailable_even_with_configured_accounts(self):
        self.stop()
        self.start(mode="demo")
        self.token = self.login()
        with patch.object(self.server.connections, "status", side_effect=AssertionError("public provider read")):
            capture = self.request("/api/capture")
            self.assertFalse(capture["enabled"])
            self.assertFalse(capture["available"])
            self.assertEqual(capture["failures"], [])
            self.request("/api/capture", {"enabled": True}, status=403)
        self.assertIsNone(self.server.capture._thread)

    def test_approval_checks_connected_source_before_mutation(self):
        invoice = self.invoice()
        uploaded, _ = self.upload(invoice)
        document = uploaded["documents"][-1]
        reviewed = self.action(uploaded, "review_document", {"document_id": document["id"], "fields": document["suggested_fields"]}, "source-review")["invoice"]
        for accounting in ({"payment_status": "paid"}, {"payment_status": "partial"},
                           {"payment_status": "unpaid", "source_changed": True},
                           {"payment_status": "unpaid", "error": "Verification unavailable"}):
            with self.subTest(accounting=accounting), patch.object(self.server.connected, "get_detail", return_value={"accounting": accounting}), patch.object(self.server.connected, "refresh_invoice") as refresh:
                result = self.action(reviewed, "approve_resolution", {}, "source-approval", status=409)
                self.assertEqual(result["error"]["code"], "accounting_changed")
                refresh.assert_called_once_with(invoice["id"])
        self.assertEqual(self.server.service.get_invoice(invoice["id"])["status"], "ready")
        self.assertEqual(self.server.service.get_invoice(invoice["id"])["version"], reviewed["version"])

    def test_connected_http_vertical_slice_uses_confirmed_provider_receipts(self):
        connections = FakeConnections()
        settings = replace(self.settings, qbo_po_field_id="1")
        self.server.connections = connections
        self.server.connected = ConnectedWorkflow(self.server.service, connections, settings)
        imported = self.request("/api/connections/qbo/sync", {})
        self.assertEqual(imported["created"], 1)
        summary = self.request("/api/invoices")["invoices"][0]
        source = self.request(f"/api/invoices/{summary['id']}")
        self.assertEqual(summary["work"], source["work"])
        self.assertEqual(source["work"]["kind"], "request")
        sent = self.request(f"/api/invoices/{source['id']}/send-request", {"expected_version": source["version"], "idempotency_key": "http-send"})
        self.assertEqual(sent["invoice"]["integration"]["messages"][0]["status"], "sent")
        queued = self.request("/api/invoices")["invoices"][0]
        self.assertEqual(queued["work"], sent["invoice"]["work"])
        self.assertEqual(queued["work"]["label"], "Awaiting purchase order")
        content = b"PO Number: PO-2026\nCustomer: Acme Logistics\nCurrency: USD\nAmount: 100.00\n"
        connections.threads["thread-1"].append({"id": "http-reply", "threadId": "thread-1", "internalDate": "1788220800000", "payload": {"mimeType": "multipart/mixed", "headers": [{"name": "From", "value": "ap@acme.co"}], "parts": [{"partId": "0", "mimeType": "text/plain", "filename": "", "body": {"data": base64.urlsafe_b64encode(b"Here is the purchase order.").decode()}}, {"partId": "1", "mimeType": "text/plain", "filename": "purchase-order.txt", "body": {"data": base64.urlsafe_b64encode(content).decode(), "size": len(content)}}]}})
        received = self.request(f"/api/invoices/{source['id']}/sync-replies", {})
        self.assertEqual((received["added"], received["documents_added"]), (1, 1))
        current = received["invoice"]
        document = current["documents"][-1]
        self.assertFalse(document["reviewed"])
        current = self.action(current, "review_document", {"document_id": document["id"], "fields": document["suggested_fields"]}, "http-review")["invoice"]
        current = self.action(current, "propose_reference", {"po_reference": "PO-2026", "note": "Reviewed source document confirms the missing invoice reference."}, "http-propose")["invoice"]
        current = self.action(current, "approve_resolution", {}, "http-approve")["invoice"]
        applied = self.request(f"/api/invoices/{source['id']}/apply-reference", {"expected_version": current["version"], "idempotency_key": "http-apply"})
        accounting = applied["invoice"]["integration"]["accounting"]
        self.assertTrue(accounting["applied_at"])
        self.assertEqual(accounting["po_reference"], "PO-2026")
        self.assertEqual(accounting["balance_cents"], 10000)
        self.assertEqual(applied["invoice"]["po_reference"], "")
        self.assertEqual(applied["invoice"]["payment_status"], "unpaid")
        with zipfile.ZipFile(io.BytesIO(self.request(f"/api/invoices/{source['id']}/handoff"))) as archive:
            self.assertEqual(archive.read("purchase-order.txt"), content)
            self.assertEqual(json.loads(archive.read("resolution.json"))["source_provenance"]["intake"], "qbo")

    def test_approval_rechecks_accounting_refreshed_after_api_precheck(self):
        connections = FakeConnections()
        self.server.connections = connections
        self.server.connected = ConnectedWorkflow(self.server.service, connections, replace(self.settings, qbo_po_field_id="1"))
        self.request("/api/connections/qbo/sync", {})
        current = self.request("/api/invoices")["invoices"][0]
        content = b"PO Number: PO-2026\nCustomer: Acme Logistics\nCurrency: USD\nAmount: 100.00\n"
        current = self.request(f"/api/invoices/{current['id']}/documents", {
            "filename": "purchase-order.txt", "content_base64": base64.b64encode(content).decode(),
            "expected_version": current["version"], "idempotency_key": "race-upload"})
        document = current["documents"][-1]
        current = self.action(current, "review_document", {"document_id": document["id"], "fields": document["suggested_fields"]}, "race-review")["invoice"]
        current = self.action(current, "propose_reference", {"po_reference": "PO-2026", "note": "The original purchase order supports this reference."}, "race-proposal")["invoice"]
        original = InvoiceHandler._approval_source
        for condition in ("changed", "partial", "paid", "unavailable"):
            with self.subTest(condition=condition):
                connections.invoices["101"].update(TotalAmt=100, Balance=100)
                connections.qbo_failure = None
                checked, resume = threading.Event(), threading.Event()

                def deferred(handler, invoice_id):
                    original(handler, invoice_id)
                    checked.set()
                    if not resume.wait(timeout=5):
                        raise RuntimeError("The concurrent accounting refresh did not complete.")

                with patch.object(InvoiceHandler, "_approval_source", deferred), ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(self.action, current, "approve_resolution", {}, "race-approval", status=409)
                    try:
                        self.assertTrue(checked.wait(timeout=5))
                        if condition == "unavailable":
                            connections.qbo_failure = "get"
                        elif condition == "changed":
                            connections.invoices["101"].update(TotalAmt=125, Balance=125)
                        else:
                            connections.invoices["101"]["Balance"] = 50 if condition == "partial" else 0
                        self.request(f"/api/invoices/{current['id']}/refresh-accounting", {}, status=502 if condition == "unavailable" else 200)
                        observed = self.request(f"/api/invoices/{current['id']}")
                        self.assertEqual(observed["version"], current["version"])
                        accounting = observed["integration"]["accounting"]
                        if condition == "unavailable":
                            self.assertTrue(accounting["error"])
                        elif condition == "changed":
                            self.assertTrue(accounting["source_changed"])
                        else:
                            self.assertEqual(accounting["payment_status"], condition)
                    finally:
                        resume.set()
                    result = pending.result(timeout=5)
                self.assertEqual(result["error"]["code"], "accounting_changed")
                unchanged = self.server.service.get_invoice(current["id"])
                self.assertIsNone(unchanged["resolution"])
                self.assertEqual(unchanged["version"], current["version"])
                self.assertEqual(unchanged["status"], "ready")

    def test_handoff_download_is_authenticated_and_approval_bound(self):
        invoice = self.invoice()
        path = f"/api/invoices/{invoice['id']}/handoff"
        self.request(path, status=409)
        uploaded, original = self.upload(invoice)
        document = uploaded["documents"][-1]
        reviewed = self.action(uploaded, "review_document", {"document_id": document["id"], "fields": document["suggested_fields"]}, "review")["invoice"]
        prepared = self.action(reviewed, "approve_resolution", {}, "prepare")["invoice"]
        self.request(path, token=False, status=401)
        with zipfile.ZipFile(io.BytesIO(self.request(path))) as archive:
            self.assertEqual(archive.read("purchase-order.txt"), original)
            snapshot = json.loads(archive.read("resolution.json"))
            self.assertEqual(snapshot["prepared_by"], self.settings.email)
            self.assertEqual(snapshot["payment_status"], "unpaid")
        self.action(prepared, "reopen", {"note": "Review again before another handoff."}, "reopen")
        self.request(path, status=409)

    def test_demo_authentication_and_seed_once(self):
        self.stop()
        self.start(mode="demo")
        runtime = self.request("/api/runtime", token=False)
        self.assertEqual(runtime["demo_credentials"]["email"], self.settings.email)
        self.assertEqual(runtime["demo_credentials"]["password"], self.settings.password)
        self.request("/api/invoices", token=False, status=401)
        self.token = self.login()
        count = self.request("/api/invoices?view=all")["total"]
        self.assertEqual(count, 36)
        self.stop()
        self.start(mode="demo")
        self.assertEqual(self.request("/api/invoices?view=all")["total"], count)

    def test_session_restart_logout_and_password_rotation(self):
        token = self.token
        self.stop()
        self.start()
        self.assertEqual(self.token, token)
        self.request("/api/invoices")
        self.request("/api/session/logout", {})
        self.request("/api/invoices", status=401)
        self.token = self.login()
        self.stop()
        self.start(password="rotated-test-password")
        self.request("/api/invoices", status=401)
        self.request("/api/session", {"email": self.settings.email, "password": "isolated-test-password"}, token=False, status=401)
        self.token = self.login(password="rotated-test-password")
        self.request("/api/invoices")

    def test_origin_and_static_traversal(self):
        self.request("/api/session", {"email": self.settings.email, "password": self.settings.password},
                     token=False, headers={"Origin": "https://foreign.example"}, status=403)
        self.request("/api/invoices", {}, headers={"Origin": "https://foreign.example"}, status=403)
        self.request("/api/session", {"email": self.settings.email, "password": self.settings.password},
                     token=False, headers={"Origin": self.base})
        for path in ("/%2e%2e/secret.txt", "/..%2fsecret.txt", "/%2e%2e%2fsecret.txt"):
            self.request(path, token=False, status=(400, 403, 404))
        self.assertIn(b"InvoiceClear", self.request("/", token=False))

    def test_csv_atomic_import_and_conflicting_duplicates(self):
        columns = ["invoice_number", "customer", "customer_email", "amount", "currency", "issue_date",
                   "due_date", "po_reference", "source_ref"]
        row = ["CSV-1", "CSV Customer", "ap@csv.example", "125.25", "USD", "2026-08-01",
               "2026-08-31", "", "manual:csv"]
        def content(rows):
            stream = io.StringIO()
            writer = csv.writer(stream)
            writer.writerow(columns)
            writer.writerows(rows)
            return {"csv": stream.getvalue()}
        self.assertEqual(self.request("/api/invoices/import", content([row]), status=(200, 201)), {"created": 1, "duplicates": 0})
        self.assertEqual(self.request("/api/invoices/import", content([row]), status=(200, 201)), {"created": 0, "duplicates": 1})
        conflict, new = row.copy(), row.copy()
        conflict[3], new[0] = "126.25", "CSV-2"
        self.request("/api/invoices/import", content([new, conflict]), status=409)
        result = self.request("/api/invoices?view=all")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["invoices"][0]["amount_cents"], 12525)

    def test_write_versions_and_idempotent_retries(self):
        invoice = self.invoice()
        result = self.action(invoice, "record_reply", {"text": "Please use PO-TEST."}, "reply-one")
        updated = result["invoice"]
        self.assertGreater(updated["version"], invoice["version"])
        retry = self.action(invoice, "record_reply", {"text": "Please use PO-TEST."}, "reply-one")
        self.assertTrue(retry["duplicate"])
        self.assertEqual(retry["invoice"]["version"], updated["version"])
        self.action(invoice, "record_reply", {"text": "Changed payload"}, "reply-one", status=409)
        self.action(invoice, "record_reply", {"text": "Stale write"}, "reply-two", status=409)
        self.request(f"/api/invoices/{invoice['id']}/actions", {"action": "record_reply", "parameters": {"text": "Missing version"}, "idempotency_key": "missing"}, status=400)
        self.action(updated, "approve_resolution", {"actor": "administrator", "role": "admin"}, "forged-actor",
                    actor="administrator", status=(400, 409))
        detail = self.request(f"/api/invoices/{invoice['id']}")
        self.assertEqual(detail["payment_status"], "unpaid")
        self.assertEqual(len(detail["replies"]), 1)

    def test_original_attachment_and_preparation_preserve_source_invoice(self):
        invoice = self.invoice()
        uploaded, original = self.upload(invoice)
        document = uploaded["documents"][0]
        self.assertFalse(document["reviewed"])
        self.request(f"/api/documents/{document['id']}/download", token=False, status=401)
        self.assertEqual(self.request(f"/api/documents/{document['id']}/download"), original)
        retry, _ = self.upload(invoice)
        self.assertEqual(len(retry["documents"]), 1)
        self.assertEqual(retry["version"], uploaded["version"])
        stale, _ = self.upload(invoice, key="stale-upload", status=409)
        self.assertEqual(stale["error"]["code"], "stale_version")
        self.upload(uploaded, key="missing-upload-version", expected_version=None, status=400)
        self.action(uploaded, "approve_resolution", {}, "premature", status=(400, 409))
        fields = {"po_number": "PO-TEST", "customer": "Example Staffing", "currency": "USD", "amount": "100.00"}
        reviewed = self.action(uploaded, "review_document", {"document_id": document["id"], "fields": fields}, "review-one",
                               actor="forged-admin@example.test")["invoice"]
        prepared = self.action(reviewed, "approve_resolution", {}, "prepare-one", actor="forged-admin@example.test")["invoice"]
        self.assertEqual(prepared["status"], "prepared")
        self.assertEqual(prepared["payment_status"], "unpaid")
        self.assertEqual(prepared["amount_cents"], invoice["amount_cents"])
        self.assertEqual(prepared["po_reference"], invoice["po_reference"])
        self.assertTrue(prepared["resolution"])
        self.assertEqual(prepared["resolution"]["prepared_by"], self.settings.email)
        self.assertIn(b"INV-TEST", self.request("/api/export.csv"))


if __name__ == "__main__":
    unittest.main()
