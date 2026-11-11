"""Cent-exact public case variety and non-destructive startup; temporary DBs only."""
import base64
import hashlib
import json
import tempfile
import unittest
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from invoiceclear import DomainError, Service
from invoiceclear.documents import decode_document, money, rules_extract
from invoiceclear.public_seed import FIXTURE, public_cases
from invoiceclear.service import PUBLIC_TABLES


class PublicSeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="invoiceclear-public-seed-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "public.sqlite3"

    @staticmethod
    def snapshot(service):
        with service._connection() as connection:
            tables = PUBLIC_TABLES + ("provider_usage",)
            return {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")] for table in tables}

    @staticmethod
    def get(service, number):
        return service.get_invoice("demo-invoice-" + str(number))

    @staticmethod
    def act(service, invoice, action, parameters=None):
        return service.action(invoice["id"], {"action": action, "parameters": parameters or {},
                              "expected_version": invoice["version"], "idempotency_key": "case-pack-" + action})["invoice"]

    def test_pack_has_36_coherent_cases_real_source_fields_and_no_provider_activity(self):
        with patch("urllib.request.urlopen") as network:
            service = Service(self.path)
            rows = service.list_invoices(view="all")["invoices"]
            details = [service.get_invoice(row["id"]) for row in rows]
        network.assert_not_called()
        self.assertEqual(len(rows), 36)
        self.assertEqual(len({row["customer"] for row in rows}), 8)
        self.assertEqual(Counter(row["status"] for row in rows),
                         {"missing_po": 6, "needs_review": 4, "mismatch": 14, "ready": 10, "prepared": 2})
        self.assertEqual(sum(len(row["documents"]) for row in details), 35)
        self.assertEqual(sum(len(row["replies"]) for row in details), 16)
        self.assertTrue(all(row["payment_status"] == "unpaid" for row in details))
        serialized = json.dumps(details)
        self.assertFalse(any(name in serialized for name in ("Northstar", "Beacon", "Cedar")))
        self.assertEqual(service.list_invoices(view="prepared")["total"], 2)
        self.assertEqual(service.list_invoices(view="open")["total"], 34)
        with service._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM provider_usage").fetchone()[0], 0)
            for row in connection.execute("SELECT * FROM invoices"):
                provenance = json.loads(row["provenance"])
                self.assertEqual(provenance["fixture"], FIXTURE)
                self.assertIn("Fictional", provenance["disclosure"])
                self.assertEqual(sum(money(line["amount"]) for line in provenance["billing_lines"]), row["amount_cents"])
                self.assertEqual((date.fromisoformat(row["due_date"]) - date.fromisoformat(row["issue_date"])).days, provenance["terms_days"])
                self.assertGreaterEqual(date.fromisoformat(row["issue_date"]), date.fromisoformat(provenance["service_period"].split(" through ")[1]))
                self.assertTrue(row["customer_email"].endswith(".example"))
                for line in provenance["billing_lines"]:
                    self.assertEqual(Decimal(line["amount"]), (Decimal(line["quantity"]) * Decimal(line["unit_rate"])).quantize(Decimal("0.01"), rounding="ROUND_HALF_UP"))
        for invoice in details:
            for document in invoice["documents"]:
                original, filename, _ = service.document_blob(document["id"])
                self.assertTrue(filename.endswith(".pdf"))
                self.assertTrue(original.startswith(b"%PDF-"))
                self.assertGreater(len(original), 1200)
                self.assertEqual(document["sha256"], hashlib.sha256(original).hexdigest())
                _, _, _, pages = decode_document({"filename": filename, "content_base64": base64.b64encode(original).decode()})
                self.assertEqual(pages, document["pages"])
                self.assertEqual(len(pages), 2)
                fields, _, error = rules_extract(pages)
                self.assertIsNone(error)
                self.assertEqual(fields, document["suggested_fields"])
                if document["reviewed"]:
                    self.assertEqual(document["fields"], fields)
                    self.assertEqual(set(document["review_evidence"]), {"po_number", "customer", "currency", "amount"})
                else:
                    self.assertEqual(document["fields"], {})
                    self.assertEqual(document["review_evidence"], {})
            self.assertTrue(all(reply["source"] == "manual" for reply in invoice["replies"]))

    def test_expected_exceptions_are_computed_from_latest_evidence(self):
        service = Service(self.path)
        failures = {1043: {"po_reference"}, 1044: {"po_coverage"}, 1052: {"po_reference"},
                    1053: {"po_reference"}, 1054: {"po_reference"}, 1055: {"customer"},
                    1056: {"customer"}, 1057: {"currency"}, 1058: {"currency"},
                    1059: {"po_coverage"}, 1060: {"po_coverage"}, 1063: {"customer"},
                    1068: {"po_reference"}, 1074: {"po_coverage"}}
        for number, expected in failures.items():
            with self.subTest(number=number):
                invoice = self.get(service, number)
                self.assertEqual(invoice["status"], "mismatch")
                self.assertEqual({check["code"] for check in invoice["checks"] if check["status"] != "pass"}, expected)
                with self.assertRaises(DomainError) as raised:
                    self.act(service, invoice, "approve_resolution")
                self.assertEqual(raised.exception.code, "checks_not_passed")
        cent_short = self.get(service, 1059)
        coverage = next(check for check in cent_short["checks"] if check["code"] == "po_coverage")
        self.assertEqual((cent_short["amount_cents"], coverage["available_cents"]), (1581876, 1581875))
        awaiting = self.get(service, 1061)
        self.assertEqual([doc["reviewed"] for doc in awaiting["documents"]], [True, False])
        self.assertEqual(awaiting["status"], "needs_review")
        wrong_latest = self.get(service, 1063)
        self.assertEqual(wrong_latest["documents"][0]["fields"]["customer"], wrong_latest["customer"])
        self.assertNotEqual(wrong_latest["documents"][-1]["fields"]["customer"], wrong_latest["customer"])
        third = self.get(service, 1064)
        self.assertEqual(len(third["documents"]), 3)
        self.assertEqual(third["status"], "ready")
        self.assertTrue(all(check["document_id"] == third["documents"][-1]["id"] for check in third["checks"]))
        for number in (1047, 1049, 1070):
            invoice = self.get(service, number)
            self.assertTrue(invoice["replies"])
            self.assertEqual(invoice["status"], "missing_po")

    def test_shared_allocations_are_real_scoped_exact_and_rechecked_on_approval(self):
        service = Service(self.path)
        coverage = lambda invoice: next(check for check in invoice["checks"] if check["code"] == "po_coverage")
        remaining = self.get(service, 1072)
        self.assertEqual((coverage(remaining)["reserved_cents"], coverage(remaining)["available_cents"]), (1483750, 731265))
        short = self.get(service, 1074)
        self.assertEqual((coverage(short)["reserved_cents"], coverage(short)["available_cents"], short["amount_cents"]), (1250000, 550000, 550001))
        self.assertEqual(coverage(self.get(service, 1075))["reserved_cents"], 0)
        self.assertEqual(coverage(self.get(service, 1076))["reserved_cents"], 0)
        prepared = self.act(service, remaining, "approve_resolution")
        self.assertEqual(prepared["status"], "prepared")
        self.assertEqual(prepared["amount_cents"], 731265)
        # Reopening the first allocation releases its local reservation; it does
        # not create a payment or change the remaining case's source amount.
        self.act(service, self.get(service, 1073), "reopen", {"note": "Review the current allocation before another billing handoff."})
        released = self.get(service, 1074)
        self.assertEqual(released["status"], "ready")
        self.assertEqual(coverage(released)["reserved_cents"], 0)
        self.assertEqual(released["amount_cents"], 550001)
        self.assertEqual(released["payment_status"], "unpaid")
        for number in (1071, 1073):
            history = service.history("demo-invoice-" + str(number))["events"]
            self.assertTrue(any(event["event"] == "approve_resolution" for event in history))

    def test_restart_preserves_every_record_and_never_replaces_an_occupied_pack(self):
        service = Service(self.path)
        self.act(service, self.get(service, 1045), "record_reply", {"text": "Retain this user-entered note after restart."})
        before = self.snapshot(service)
        with patch("invoiceclear.service.public_cases", side_effect=AssertionError("must not load a replacement pack")):
            restarted = Service(self.path)
        self.assertEqual(self.snapshot(restarted), before)

    def test_legacy_and_user_public_records_are_not_augmented_or_silently_migrated(self):
        # The old identity exists solely in this explicit legacy fixture. It is
        # never part of a fresh default pack or the downloadable current bundle.
        with patch.object(Service, "_seed", return_value=None):
            service = Service(self.path)
        entry = dict(invoice_number="STA-2026-1041", customer="Northstar Health", customer_email="accounts@northstarhealth.example",
                     amount="18450.00", currency="USD", issue_date="2026-08-01", due_date="2026-08-31", po_reference="", source_ref="Retained legacy ledger")
        with service._write() as connection:
            service._insert(connection, service._entry(entry), "legacy-fixture", invoice_id="demo-invoice-1041",
                            provenance={"mode": "demo", "intake": "fixed_demo_seed", "fixture": "staffing-six-v1"})
        own = dict(entry, invoice_number="OWN-RECORD", customer="Own Account LLC", customer_email="ap@ownaccount.example")
        service.create_invoice(own)
        before = self.snapshot(service)
        restarted = Service(self.path)
        self.assertEqual(self.snapshot(restarted), before)
        self.assertEqual(restarted.list_invoices(view="all")["total"], 2)
        self.assertEqual(self.get(restarted, 1041)["customer"], "Northstar Health")
        self.assertEqual(Service(Path(self.temp.name) / "private.sqlite3", demo=False).list_invoices(view="all")["total"], 0)

    def test_text_form_feeds_preserve_pages_and_enforce_a_bounded_page_count(self):
        body = {"filename": "two-page.txt", "content_base64": base64.b64encode(b"Page one\fPage two").decode()}
        self.assertEqual(decode_document(body)[3], [{"number": 1, "text": "Page one"}, {"number": 2, "text": "Page two"}])
        body["content_base64"] = base64.b64encode(b"Page\f" * 20 + b"21st page").decode()
        with self.assertRaises(DomainError) as raised:
            decode_document(body)
        self.assertEqual((raised.exception.code, raised.exception.status), ("document_too_large", 413))


if __name__ == "__main__":
    unittest.main()
