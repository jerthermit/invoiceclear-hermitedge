"""Release boundaries against isolated databases; no provider or customer records."""
import base64
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from invoiceclear.service import DomainError, PUBLIC_TABLES, Service


class ReleaseGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="invoiceclear-release-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "workspace.sqlite3"

    def entry(self, number):
        return dict(invoice_number=number, customer="Example Company", customer_email="ap@example.test",
                    amount="100.00", currency="USD", issue_date="2026-08-01", due_date="2026-08-31",
                    po_reference="PO-TEST", source_ref="manual:test")

    def rows(self, service):
        with service._connection() as connection:
            return sum(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in PUBLIC_TABLES)

    def test_mode_is_durable_and_both_switch_directions_preserve_records(self):
        for demo in (False, True):
            path = self.path.with_name(str(demo) + ".sqlite3")
            service = Service(path, demo=demo)
            created = service.create_invoice(self.entry("OWN-RECORD"))
            before = service.list_invoices(view="all")["total"]
            with self.assertRaisesRegex(ValueError, "separate database path"):
                Service(path, demo=not demo)
            reopened = Service(path, demo=demo)
            self.assertEqual(reopened.list_invoices(view="all")["total"], before)
            self.assertEqual(reopened.get_invoice(created["id"])["invoice_number"], "OWN-RECORD")

    def test_legacy_known_provenance_binds_without_conversion(self):
        for demo in (False, True):
            path = self.path.with_name("legacy-" + str(demo) + ".sqlite3")
            service = Service(path, demo=demo)
            service.create_invoice(self.entry("LEGACY"))
            with service._connection() as connection:
                connection.execute("DROP TABLE workspace_identity")
            with self.assertRaises(ValueError):
                Service(path, demo=not demo)
            self.assertEqual(Service(path, demo=demo).list_invoices(view="all")["total"], 37 if demo else 1)

    def test_unknown_or_mixed_legacy_provenance_fails_closed(self):
        for value in ("not-json", "[]", "{}", json.dumps({"mode": "unknown"})):
            path = self.path.with_name("ambiguous.sqlite3")
            if not path.exists():
                service = Service(path, demo=False)
                service.create_invoice(self.entry("LEGACY"))
                with service._connection() as connection:
                    connection.execute("DROP TABLE workspace_identity")
            with sqlite3.connect(path) as connection:
                connection.execute("UPDATE invoices SET provenance=?", (value,))
            for demo in (False, True):
                with self.assertRaisesRegex(ValueError, "provenance is ambiguous"):
                    Service(path, demo=demo)
        with sqlite3.connect(path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM invoices").fetchone()[0], 1)

    def test_row_limit_rolls_back_reply_audit_version_and_idempotency_together(self):
        service = Service(self.path)
        invoice = service.list_invoices()["invoices"][0]
        detail = service.get_invoice(invoice["id"])
        body = {"action": "record_reply", "parameters": {"text": "Requested the missing PO."},
                "expected_version": detail["version"], "idempotency_key": "public-reply"}
        before = self.rows(service)
        with patch("invoiceclear.service.PUBLIC_MAX_ROWS", before):
            with self.assertRaises(DomainError) as raised:
                service.action(detail["id"], body)
            self.assertEqual((raised.exception.code, raised.exception.status), ("public_capacity", 429))
        self.assertEqual(service.get_invoice(detail["id"]), detail)
        self.assertEqual(self.rows(service), before)

    def test_import_is_atomic_at_public_budget(self):
        service = Service(self.path)
        before = self.rows(service)
        csv = "invoice_number,customer,customer_email,amount,currency,issue_date,due_date,po_reference,source_ref\n" + "\n".join(
            f"IMPORT-{n},Example Company,ap@example.test,100.00,USD,2026-08-01,2026-08-31,PO-TEST,manual:test" for n in range(3))
        with patch("invoiceclear.service.PUBLIC_MAX_ROWS", before + 4):
            with self.assertRaises(DomainError):
                service.import_invoices(csv)
        self.assertEqual(self.rows(service), before)

    def test_document_and_snapshot_size_are_bounded_and_original_survives_rejection(self):
        service = Service(self.path)
        detail = service.get_invoice("demo-invoice-1043")
        original = service.document_blob(detail["documents"][0]["id"])[0]
        with service._connection() as connection:
            size = connection.execute("PRAGMA page_count").fetchone()[0] * connection.execute("PRAGMA page_size").fetchone()[0]
        body = {"filename": "large-po.txt", "content_base64": base64.b64encode(b"Readable text\n" * 5000).decode(),
                "expected_version": detail["version"], "idempotency_key": "over-budget-document"}
        with patch("invoiceclear.service.PUBLIC_MAX_BYTES", size):
            with self.assertRaises(DomainError) as raised:
                service.attach_document(detail["id"], body)
        self.assertEqual(raised.exception.code, "public_capacity")
        self.assertEqual(service.get_invoice(detail["id"]), detail)
        self.assertEqual(service.document_blob(detail["documents"][0]["id"])[0], original)

    def test_exact_retry_reads_exports_and_restart_work_after_exhaustion(self):
        service = Service(self.path)
        detail = service.get_invoice("demo-invoice-1045")
        body = {"action": "approve_resolution", "parameters": {}, "expected_version": detail["version"], "idempotency_key": "approved-once"}
        result = service.action(detail["id"], body)
        with patch("invoiceclear.service.PUBLIC_MAX_ROWS", 0), patch("invoiceclear.service.PUBLIC_MAX_BYTES", 0):
            duplicate = service.action(detail["id"], body)
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(duplicate["invoice"], result["invoice"])
            self.assertTrue(service.handoff(detail["id"])[0])
            self.assertTrue(service.export_csv())
            rebuilt = Service(self.path)
            self.assertEqual(rebuilt.get_invoice(detail["id"])["resolution"], result["invoice"]["resolution"])

    def test_rejected_public_upload_cannot_rollback_dispatched_extraction_quota(self):
        service = Service(self.path, provider="together", together_key="unused-test-key")
        detail = service.get_invoice("demo-invoice-1043")
        original = service.document_blob(detail["documents"][0]["id"])[0]
        before = self.rows(service)
        body = {"filename": "another-po.txt", "content_base64": base64.b64encode(b"PO Number: ANOTHER-PO").decode(),
                "expected_version": detail["version"], "idempotency_key": "rejected-extraction"}
        with patch("invoiceclear.service.PUBLIC_MAX_ROWS", before), patch(
                "invoiceclear.service.together_extract", return_value=({}, {}, "Provider is unavailable.", 0)) as extraction:
            for _ in range(5):
                with self.assertRaises(DomainError) as raised:
                    service.attach_document(detail["id"], body)
                self.assertEqual(raised.exception.code, "public_capacity")
            self.assertEqual(extraction.call_count, 5)
            rebuilt = Service(self.path, provider="together", together_key="unused-test-key")
            with self.assertRaises(DomainError) as raised:
                rebuilt.attach_document(detail["id"], body)
            self.assertEqual(raised.exception.code, "public_capacity")
            self.assertEqual(extraction.call_count, 5)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT calls,tokens FROM provider_usage").fetchone(), (5, 500_000))
        self.assertEqual(service.get_invoice(detail["id"]), detail)
        self.assertEqual(self.rows(service), before)
        self.assertEqual(service.document_blob(detail["documents"][0]["id"])[0], original)

    def test_concurrent_creates_cannot_overrun_public_row_budget(self):
        service = Service(self.path)
        before = self.rows(service)
        def create(number):
            try:
                service.create_invoice(self.entry(f"CONCURRENT-{number}"))
                return "created"
            except DomainError as error:
                return error.code
        with patch("invoiceclear.service.PUBLIC_MAX_ROWS", before + 2):
            with ThreadPoolExecutor(max_workers=8) as workers:
                results = list(workers.map(create, range(8)))
        self.assertEqual(results.count("created"), 1)
        self.assertEqual(results.count("public_capacity"), 7)
        self.assertEqual(self.rows(service), before + 2)

    def test_public_caps_do_not_limit_private_workspaces(self):
        service = Service(self.path, demo=False)
        with patch("invoiceclear.service.PUBLIC_MAX_ROWS", 0), patch("invoiceclear.service.PUBLIC_MAX_BYTES", 0):
            invoice = service.create_invoice(self.entry("PRIVATE"))
        self.assertEqual(invoice["invoice_number"], "PRIVATE")


if __name__ == "__main__":
    unittest.main()
