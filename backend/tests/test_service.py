import base64
import csv
import io
import json
import sqlite3
import tempfile
import threading
import unittest
import uuid
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from invoiceclear import DomainError, Service
from invoiceclear.documents import MAX_PDF_STREAM, together_extract


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "private.sqlite3"
        self.service = Service(self.path, demo=False)

    def create(self, **changes):
        body = {"invoice_number": "INV-" + uuid.uuid4().hex[:8], "customer": "Example Staffing", "customer_email": "ap@staffing.example", "amount": "60.00", "currency": "USD", "issue_date": "2026-08-01", "due_date": "2026-08-31", "po_reference": "PO-TEST", "source_ref": "Manual staffing ledger"}
        body.update(changes)
        return self.service.create_invoice(body), body

    def attach(self, invoice, *, po="PO-TEST", customer="Example Staffing", currency="USD", amount="100.00", extra=""):
        content = f"PO Number: {po}\nCustomer: {customer}\nCurrency: {currency}\nAmount: {amount}\n{extra}".encode()
        body = {"filename": "purchase-order.txt", "content_base64": base64.b64encode(content).decode(), "expected_version": invoice["version"], "idempotency_key": uuid.uuid4().hex}
        return self.service.attach_document(invoice["id"], body), body, content

    def action(self, invoice, action, parameters=None, key=None):
        return self.service.action(invoice["id"], {"action": action, "parameters": parameters or {}, "expected_version": invoice["version"], "idempotency_key": key or uuid.uuid4().hex})

    def ready(self, invoice, **changes):
        uploaded, _, _ = self.attach(invoice, **changes)
        document = uploaded["documents"][-1]
        return self.action(uploaded, "review_document", {"document_id": document["id"], "fields": document["suggested_fields"]})["invoice"]

    def assert_error(self, code, function, *args, **kwargs):
        with self.assertRaises(DomainError) as raised:
            function(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_private_empty_demo_is_persistent_and_downloadable(self):
        self.assertEqual(self.service.list_invoices()["total"], 0)
        demo_path = Path(self.temporary.name) / "demo.sqlite3"
        demo = Service(demo_path)
        invoices = demo.list_invoices(view="all")["invoices"]
        self.assertEqual(len(invoices), 36)
        for summary in invoices:
            self.assertFalse(set(summary) & {"documents", "checks", "replies", "resolution", "request", "pages", "content"})
        self.assertEqual(Counter(i["status"] for i in invoices), {"missing_po": 6, "needs_review": 4, "mismatch": 14, "ready": 10, "prepared": 2})
        ready = next(i for i in invoices if i["status"] == "ready")
        detail = demo.get_invoice(ready["id"])
        doc = detail["documents"][0]
        original, filename, mime = demo.document_blob(doc["id"])
        self.assertIn(doc["fields"]["po_number"].encode(), original)
        self.assertTrue(filename.endswith(".pdf"))
        self.assertEqual(mime, "application/pdf")
        self.assertTrue(original.startswith(b"%PDF-"))
        changed = demo.action(detail["id"], {"action": "record_reply", "expected_version": detail["version"], "idempotency_key": "demo-reply", "parameters": {"text": "Customer confirmed the request."}})["invoice"]
        rebuilt = Service(demo_path)
        self.assertEqual(rebuilt.list_invoices(view="all")["total"], 36)
        self.assertEqual(rebuilt.get_invoice(detail["id"])["version"], changed["version"])
        with sqlite3.connect(demo_path) as connection:
            provenance = json.loads(connection.execute("SELECT provenance FROM invoices LIMIT 1").fetchone()[0])
        self.assertEqual(provenance["intake"], "fixed_demo_seed")

    def test_handoff_requires_active_approval(self):
        invoice, _ = self.create()
        self.assert_error("not_prepared", self.service.handoff, invoice["id"])
        uploaded, _, _ = self.attach(invoice)
        self.assert_error("not_prepared", self.service.handoff, uploaded["id"])
        reviewed = self.action(uploaded, "review_document", {"document_id": uploaded["documents"][-1]["id"], "fields": uploaded["documents"][-1]["suggested_fields"]})["invoice"]
        self.assert_error("not_prepared", self.service.handoff, reviewed["id"])
        mismatch, _ = self.create(po_reference="OTHER-PO")
        mismatch = self.ready(mismatch)
        self.assert_error("checks_not_passed", self.action, mismatch, "approve_resolution")
        self.assert_error("not_prepared", self.service.handoff, mismatch["id"])

    def test_invoice_pages_match_former_results_for_mixed_case_states(self):
        self.create(invoice_number="PAGE-MISSING", due_date="2026-08-15")
        self.attach(self.create(invoice_number="PAGE-UNREVIEWED")[0])
        self.ready(self.create(invoice_number="PAGE-MISMATCH", po_reference="WRONG")[0], amount="500.00")
        self.ready(self.create(invoice_number="PAGE-READY")[0], amount="500.00")
        prepared = self.ready(self.create(invoice_number="PAGE-PREPARED", po_reference="PO-PREPARED")[0], po="PO-PREPARED")
        self.action(prepared, "approve_resolution")
        reopened = self.ready(self.create(invoice_number="PAGE-REOPENED", po_reference="PO-REOPENED")[0], po="PO-REOPENED")
        reopened = self.action(reopened, "approve_resolution")["invoice"]
        self.action(reopened, "reopen", {"note": "Retain the earlier approval while reviewing again."})
        # Same due date and invoice number exercise the final ID tie-breaker.
        self.create(invoice_number="PAGE-TIE", customer="Tie customer B")
        self.create(invoice_number="PAGE-TIE", customer="Tie customer A")
        demo = Service(Path(self.temporary.name) / "paging-demo.sqlite3")
        ready = next(row for row in demo.list_invoices(view="all")["invoices"] if row["status"] == "ready")
        demo.action(ready["id"], {"action": "approve_resolution", "expected_version": ready["version"], "idempotency_key": "paging-prepare", "parameters": {}})

        # Retain the former filter/slice behavior as the compatibility oracle.
        for service in (self.service, demo):
            with service._connection() as connection:
                rows = [service._detail(connection, row[0], summary=True) for row in connection.execute("SELECT id FROM invoices ORDER BY due_date,invoice_number,id")]
            self.assertIn("prepared", {row["status"] for row in rows})
            self.assertIn("missing_po", {row["status"] for row in rows})
            for view in ("open", "prepared", "all"):
                for query in ("", "   ", "page-", "staffing", "PO-TEST", "never-found"):
                    matching = [row for row in rows if not (view == "open" and row["status"] == "prepared" or view == "prepared" and row["status"] != "prepared") and query.strip().casefold() in " ".join(str(row[name] or "") for name in ("invoice_number", "customer", "customer_email", "po_reference", "proposed_po_reference", "source_ref")).casefold()]
                    for offset, limit in ((0, 1), (1, 2), (0, 3), (3, 3), (8, 100), (30, 20)):
                        with self.subTest(demo=service.demo, view=view, query=query, offset=offset, limit=limit):
                            self.assertEqual(service.list_invoices(view=view, q=query, offset=offset, limit=limit), {
                                "invoices": matching[offset:offset + limit], "total": len(matching),
                                "offset": offset, "has_more": offset + limit < len(matching)})

    def test_invoice_search_preserves_unicode_literals_and_field_boundaries(self):
        invoice, _ = self.create(invoice_number="SEARCH_100%", customer="Straße İΣςﬃ", customer_email="unicode@billing.example", po_reference="RÉF_%", source_ref="LastField ' OR 1=1 --")
        invoice = self.action(invoice, "propose_reference", {"po_reference": "PROPOSED-UNICODE", "note": "Customer supplied a corrected purchase order reference."})["invoice"]
        self.create(invoice_number="SEARCH-CONTROL", customer="Another customer", po_reference="OTHER", source_ref="Ordinary source")
        for query in ("SEARCH_100%", "STRASSE İΣΣFFI", "UNICODE@BILLING.EXAMPLE", "réf_%", "proposed-unicode", "LASTFIELD ' OR 1=1 --", "%", "_", "100% strasse", "réf_% proposed-unicode lastfield", "  strasse  "):
            with self.subTest(query=query):
                result = self.service.list_invoices(q=query)
                self.assertEqual([row["id"] for row in result["invoices"]], [invoice["id"]])
                self.assertEqual(result["total"], 1)
                self.assertFalse(result["has_more"])
        for query in ("%' OR 1=1 --", "strasse iσσffi", "missing_%"):
            with self.subTest(query=query):
                self.assertEqual(self.service.list_invoices(q=query)["total"], 0)

    def test_invoice_pages_only_hydrate_displayed_cases(self):
        for index in range(35):
            self.create(invoice_number=f"PAGE-{index:03}")
        for query, offset, limit, expected in (("", 0, 20, 20), ("", 32, 20, 3), ("PAGE-007", 0, 20, 1), ("no-match", 0, 20, 0), ("", 35, 20, 0), ("", 10 ** 100, 20, 0)):
            with self.subTest(query=query, offset=offset, limit=limit), patch.object(self.service, "_detail", wraps=self.service._detail) as detail:
                page = self.service.list_invoices(q=query, offset=offset, limit=limit)
                self.assertEqual(len(page["invoices"]), expected)
                self.assertEqual(detail.call_count, expected)
                self.assertTrue(all(call.kwargs == {"summary": True} for call in detail.call_args_list))
                self.assertEqual(page["offset"], offset)
        with patch.object(self.service, "_detail", wraps=self.service._detail) as detail:
            self.assertEqual(self.service.list_invoices(view="prepared")["total"], 0)
            detail.assert_not_called()

    def test_invoice_page_query_and_pagination_bounds_are_unchanged(self):
        for view in ("paid", "' OR 1=1 --", None):
            self.assert_error("invalid_query", self.service.list_invoices, view=view)
        for query in (None, 1, [], "x" * 201):
            self.assert_error("invalid_query", self.service.list_invoices, q=query)
        for offset in (-1, True, "0", 1.0, None):
            self.assert_error("invalid_pagination", self.service.list_invoices, offset=offset)
        for limit in (0, 101, True, "20", 1.5, None):
            self.assert_error("invalid_pagination", self.service.list_invoices, limit=limit)
        self.assertEqual(self.service.list_invoices(q="x" * 200, limit=100), {"invoices": [], "total": 0, "offset": 0, "has_more": False})

    def test_handoff_instruction_for_missing_and_unchanged_references(self):
        for source in ("", "PO-TEST"):
            with self.subTest(source=source):
                invoice, _ = self.create(po_reference=source)
                reviewed = self.ready(invoice, amount="500.00")
                if not source:
                    reviewed = self.action(reviewed, "propose_reference", {"po_reference": "PO-TEST", "note": "Reviewed PO supplies the missing invoice reference."})["invoice"]
                prepared = self.action(reviewed, "approve_resolution")["invoice"]
                bundle, _ = self.service.handoff(prepared["id"])
                with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
                    note = archive.read("billing-note.txt").decode()
                    self.assertIn("Add PO reference PO-TEST" if not source else "Attach the reviewed purchase order PO-TEST", note)
                    self.assertNotIn("Example workflow fixture", note)
                    self.assertEqual(json.loads(archive.read("resolution.json"))["payment_status"], "unpaid")

    def test_handoff_legacy_snapshot_and_demo_provenance(self):
        invoice, _ = self.create()
        prepared = self.action(self.ready(invoice), "approve_resolution")["invoice"]
        legacy = dict(prepared["resolution"])
        for field in ("billing_instruction", "source_provenance", "invoice_id", "invoice_number", "customer", "customer_email", "document_filename", "correction_note"):
            legacy.pop(field)
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE resolutions SET data=? WHERE id=?", (json.dumps(legacy), legacy["id"]))
        bundle, _ = self.service.handoff(prepared["id"])
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            snapshot = json.loads(archive.read("resolution.json"))
            self.assertEqual(snapshot["invoice_number"], prepared["invoice_number"])
            self.assertEqual(snapshot["checks"], legacy["checks"])
            brief = archive.read("billing-brief.html").decode()
            self.assertIn(prepared["invoice_number"], brief)
            self.assertIn("Source pages supporting checks", brief)
        self.assertEqual(self.service.get_invoice(prepared["id"])["resolution"], legacy)
        demo = Service(Path(self.temporary.name) / "demo.sqlite3")
        invoice = demo.get_invoice("demo-invoice-1045")
        demo.action(invoice["id"], {"action": "approve_resolution", "expected_version": invoice["version"], "idempotency_key": "demo-approve", "parameters": {}})
        bundle, _ = demo.handoff(invoice["id"])
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            self.assertIn("Example workflow fixture; not a client outcome.", archive.read("billing-note.txt").decode())
            self.assertEqual(archive.read("billing-brief.html").decode().count("Example workflow fixture; not a client outcome."), 1)

    def test_billing_brief_is_offline_printable_escaped_and_approval_bound(self):
        hostile = '<img src="https://untrusted.example/x" onerror="alert(1)">'
        invoice, _ = self.create(customer="Staffing " + hostile, source_ref="https://untrusted.example/<script>alert(2)</script>")
        uploaded, body, original = self.attach(invoice, customer=invoice["customer"])
        document = uploaded["documents"][-1]
        reviewed = self.action(uploaded, "review_document", {"document_id": document["id"], "fields": document["suggested_fields"]})["invoice"]
        prepared = self.service.action(invoice["id"], {"action": "approve_resolution", "parameters": {"note": '</style><script>alert("note")</script>'}, "expected_version": reviewed["version"], "idempotency_key": "safe-brief"}, actor='reviewer <svg onload="alert(3)">')["invoice"]
        frozen = prepared["resolution"]
        # The printable brief reads the snapshot even if mutable document metadata changes.
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE documents SET filename=? WHERE id=?", ('changed<iframe src="evil">.txt', document["id"]))
            audits_before = connection.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        before = self.service.get_invoice(invoice["id"])
        bundle, filename = self.service.handoff(invoice["id"])
        class Markup(HTMLParser):
            def __init__(self):
                super().__init__()
                self.tags, self.links, self.attrs = [], [], []
            def handle_starttag(self, tag, attrs):
                self.tags.append(tag)
                self.attrs.extend(attrs)
                self.links.extend(value for name, value in attrs if name == "href")
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            self.assertEqual(set(archive.namelist()), {"billing-note.txt", "billing-brief.html", "resolution.json", "purchase-order.txt"})
            brief = archive.read("billing-brief.html").decode()
            self.assertEqual(json.loads(archive.read("resolution.json")), frozen)
            self.assertEqual(archive.read("purchase-order.txt"), original)
        markup = Markup()
        markup.feed(brief)
        self.assertFalse(set(markup.tags) & {"script", "img", "iframe", "object", "svg", "link", "form", "base"})
        self.assertFalse(any(name.startswith("on") or name == "src" for name, _ in markup.attrs))
        self.assertEqual(markup.links, ["purchase-order.txt"])
        self.assertIn('&lt;script&gt;alert(&quot;note&quot;)&lt;/script&gt;', brief)
        self.assertIn('&lt;svg onload=&quot;alert(3)&quot;&gt;', brief)
        self.assertIn("@media print", brief)
        self.assertIn("@page", brief)
        self.assertIn("Content-Security-Policy", brief)
        self.assertNotIn("url(", brief)
        self.assertNotIn("changed&lt;iframe", brief)
        self.assertIn(frozen["document_sha256"], brief)
        self.assertIn("Source payment status: unpaid", brief)
        self.assertIn("does not itself send email, update accounting or create payment", brief)
        brief_bytes, brief_filename = self.service.handoff_brief(invoice["id"])
        self.assertIn(frozen["document_sha256"], brief_bytes.decode())
        self.assertNotIn('href="purchase-order.txt"', brief_bytes.decode())
        self.assertIn("included in the complete handoff ZIP", brief_bytes.decode())
        self.assertTrue(brief_filename.endswith("-billing-brief.html"))
        self.assertEqual(self.service.get_invoice(invoice["id"]), before)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audit").fetchone()[0], audits_before)

    def test_billing_brief_keeps_pdf_attachment_name_fixed(self):
        invoice, _ = self.create()
        prepared = self.action(self.ready(invoice), "approve_resolution")["invoice"]
        # The bundle uses the retained MIME identity, never a caller-supplied path.
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE documents SET mime='application/pdf',filename='../../untrusted.pdf' WHERE id=?", (prepared["resolution"]["document_id"],))
        bundle, _ = self.service.handoff(invoice["id"])
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            self.assertEqual(set(archive.namelist()), {"billing-note.txt", "billing-brief.html", "resolution.json", "purchase-order.pdf"})
            self.assertIn('href="purchase-order.pdf"', archive.read("billing-brief.html").decode())

    def test_handoff_rejects_damaged_or_wrong_invoice_evidence(self):
        invoice, _ = self.create()
        prepared = self.action(self.ready(invoice), "approve_resolution")["invoice"]
        doc_id = prepared["resolution"]["document_id"]
        original, _, _ = self.service.document_blob(doc_id)
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE documents SET content=? WHERE id=?", (b"tampered", doc_id))
        self.assert_error("evidence_unavailable", self.service.handoff, prepared["id"])
        other, _ = self.create()
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE documents SET content=?,invoice_id=? WHERE id=?", (original, other["id"], doc_id))
        self.assert_error("evidence_unavailable", self.service.handoff, prepared["id"])

    def test_prepare_reopen_keeps_source_money_reference_and_payment(self):
        invoice, _ = self.create(po_reference="WRONG-PO")
        reviewed = self.ready(invoice)
        self.assertEqual(reviewed["status"], "mismatch")
        proposed = self.action(reviewed, "propose_reference", {"po_reference": "PO-TEST", "note": "Customer purchase order confirms this reference."})["invoice"]
        self.assertEqual(proposed["status"], "ready")
        self.assertEqual(self.service.get_request(proposed["id"]), proposed["request"])
        prepared = self.action(proposed, "approve_resolution", {"note": "Checked original evidence."})["invoice"]
        self.assertEqual(prepared["status"], "prepared")
        self.assertEqual(prepared["amount_cents"], 6000)
        self.assertEqual(prepared["po_reference"], "WRONG-PO")
        self.assertEqual(prepared["payment_status"], "unpaid")
        self.assertEqual(prepared["resolution"]["po_reference"], "PO-TEST")
        bundle, filename = self.service.handoff(prepared["id"])
        self.assertTrue(filename.endswith("-billing-handoff.zip"))
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            self.assertEqual(set(archive.namelist()), {"billing-note.txt", "billing-brief.html", "resolution.json", "purchase-order.txt"})
            note = archive.read("billing-note.txt").decode()
            self.assertIn("Correct the billing PO reference from WRONG-PO to PO-TEST", note)
            self.assertIn("Customer purchase order confirms this reference.", note)
            self.assertIn("does not itself send email, update accounting or create payment", note)
            snapshot = json.loads(archive.read("resolution.json"))
            self.assertEqual(snapshot, prepared["resolution"])
            original, _, _ = self.service.document_blob(snapshot["document_id"])
            self.assertEqual(archive.read("purchase-order.txt"), original)
        self.assertEqual(self.service.get_invoice(prepared["id"]), prepared)
        self.assertEqual(self.service.list_invoices(view="prepared")["total"], 1)
        exported = list(csv.DictReader(io.StringIO(self.service.export_csv())))
        self.assertEqual(exported[0]["payment_status"], "unpaid")
        self.assertEqual(exported[0]["internal_status"], "prepared")
        self.assert_error("reopen_required", self.action, prepared, "propose_reference", {"po_reference": "PO-OTHER", "note": "Another reference was proposed."})
        reopened = self.action(prepared, "reopen", {"note": "Customer requested another review."})["invoice"]
        self.assert_error("not_prepared", self.service.handoff, reopened["id"])
        self.assertEqual(reopened["status"], "ready")
        self.assertIsNone(reopened["resolution"])
        self.assertEqual(self.service.list_invoices(view="prepared")["total"], 0)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM resolutions").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audit WHERE event='approve_resolution'").fetchone()[0], 1)

    def test_history_uses_real_sequences_and_bounded_exclusive_pagination(self):
        invoice, _ = self.create()
        other, _ = self.create()
        reviewed = self.ready(invoice)
        replied = self.action(reviewed, "record_reply", {"text": "Reply body is retained in the reply record."})["invoice"]
        prepared = self.action(replied, "approve_resolution")["invoice"]
        with sqlite3.connect(self.path) as connection:
            expected = connection.execute("SELECT seq,event FROM audit WHERE invoice_id=? ORDER BY seq DESC", (invoice["id"],)).fetchall()
        events, cursor = [], None
        for index in range(3):
            page = self.service.history(invoice["id"], limit=2, before=cursor)
            events.extend(page["events"])
            self.assertEqual(page["has_more"], index < 2)
            self.assertEqual(page["next_before"], page["events"][-1]["id"] if page["has_more"] else None)
            cursor = page["next_before"]
        self.assertEqual([(event["id"], event["event"]) for event in events], expected)
        self.assertEqual(events[0]["summary"], "Billing handoff prepared")
        self.assertEqual(events[-1]["details"]["intake"], "manual")
        for event in events:
            self.assertEqual(set(event) - {"details"}, {"id", "event", "actor", "recorded_at", "summary"})
        self.assertNotIn("Reply body", json.dumps(events))
        self.assertEqual(self.service.get_invoice(invoice["id"]), prepared)
        self.assertEqual(self.service.history(other["id"])["events"][0]["event"], "invoice_created")

    def test_history_bounds_missing_and_unrecorded_intake(self):
        invoice, _ = self.create()
        for limit in (0, 101, True, "20", 1.5, None):
            self.assert_error("invalid_input", self.service.history, invoice["id"], limit=limit)
        for before in (0, -1, True, "1", 1.5, 9223372036854775808):
            self.assert_error("invalid_input", self.service.history, invoice["id"], before=before)
        self.assert_error("invoice_not_found", self.service.history, "missing-invoice")
        with sqlite3.connect(self.path) as connection:
            connection.execute("DELETE FROM audit WHERE invoice_id=?", (invoice["id"],))
        self.assertEqual(self.service.history(invoice["id"]), {"events": [], "has_more": False, "next_before": None})

    def test_history_projects_allowlisted_details_without_provider_or_secret_payloads(self):
        invoice, _ = self.create()
        secret = "PRIVATE-TOKEN-NEVER-RETURN"
        with self.service._write() as connection:
            self.service._audit(connection, invoice["id"], "external_apply_confirmed", "finance", {
                "action_id": "action-one", "receipt": {"Id": "qbo-one", "po_reference": "PO-TEST", "verified_at": "2026-08-01T00:00:00Z", "document_uploaded": False,
                                                        "access_token": secret, "provider_body": secret, "raw": {"secret": secret}},
                "error": secret, "model_response": secret, "api_key": secret})
            self.service._audit(connection, invoice["id"], "future_event", "finance", {"secret": secret, "body": secret})
        history = self.service.history(invoice["id"])
        self.assertNotIn(secret, json.dumps(history))
        self.assertNotIn("details", history["events"][0])
        self.assertEqual(history["events"][1]["details"], {"action_id": "action-one", "receipt_id": "qbo-one", "po_reference": "PO-TEST", "verified_at": "2026-08-01T00:00:00Z", "document_uploaded": False})

    def test_missing_unreviewed_mismatch_and_insufficient_cannot_approve(self):
        invoice, _ = self.create()
        self.assert_error("checks_not_passed", self.action, invoice, "approve_resolution")
        uploaded, _, _ = self.attach(invoice)
        self.assertEqual(uploaded["status"], "needs_review")
        self.assert_error("checks_not_passed", self.action, uploaded, "approve_resolution")
        for changes, failed in [({"customer": "Another Customer"}, "customer"), ({"currency": "PHP"}, "currency"), ({"amount": "59.99"}, "po_coverage")]:
            invoice, _ = self.create()
            reviewed = self.ready(invoice, **changes)
            self.assertEqual(reviewed["status"], "mismatch")
            self.assertEqual(next(c for c in reviewed["checks"] if c["code"] == failed)["status"], "fail")
            self.assertEqual(self.service.get_request(reviewed["id"]), reviewed["request"])
            if failed == "customer":
                self.assertIn("Another Customer", reviewed["request"]["body"])
            elif failed == "currency":
                self.assertIn("PHP", reviewed["request"]["body"])
            else:
                self.assertIn("59.99", reviewed["request"]["body"])
            self.assert_error("checks_not_passed", self.action, reviewed, "approve_resolution")

    def test_document_source_support_and_correction_note(self):
        invoice, _ = self.create()
        uploaded, _, _ = self.attach(invoice, extra="Corrected PO Number: PO-NEW\n")
        doc = uploaded["documents"][0]
        self.assertEqual(doc["extraction_method"], "rules")
        self.assertEqual(doc["evidence"]["po_number"], {"page": 1, "snippet": "PO Number: PO-TEST"})
        self.assertEqual(doc["fields"], {})
        self.assertEqual(doc["review_evidence"], {})
        self.assertFalse(doc["reviewed"])
        self.assertIsNone(doc["reviewed_by"])
        self.assertIsNone(doc["reviewed_at"])
        self.assertRegex(doc["uploaded_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        invented = dict(doc["suggested_fields"], amount="999.00")
        self.assert_error("correction_note_required", self.action, uploaded, "review_document", {"document_id": doc["id"], "fields": invented})
        self.assert_error("unsupported_evidence", self.action, uploaded, "review_document", {"document_id": doc["id"], "fields": invented, "note": "Reviewed the actual purchase order."})
        supported = dict(doc["suggested_fields"], po_number="PO-NEW")
        reviewed = self.action(uploaded, "review_document", {"document_id": doc["id"], "fields": supported, "note": "Use the corrected reference printed in the source."})["invoice"]
        reviewed_doc = reviewed["documents"][0]
        self.assertTrue(reviewed_doc["reviewed"])
        self.assertEqual(reviewed_doc["suggested_fields"]["po_number"], "PO-TEST")
        self.assertEqual(reviewed_doc["evidence"], doc["evidence"])
        self.assertEqual(reviewed_doc["fields"]["po_number"], "PO-NEW")
        self.assertEqual(reviewed_doc["review_evidence"]["po_number"], {"page": 1, "snippet": "Corrected PO Number: PO-NEW"})
        self.assertEqual(reviewed_doc["uploaded_at"], doc["uploaded_at"])
        self.assertEqual(reviewed_doc["reviewed_by"], "finance")
        self.assertRegex(reviewed_doc["reviewed_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(self.service.get_invoice(invoice["id"])["documents"][0], reviewed_doc)
        with sqlite3.connect(self.path) as connection:
            audit = json.loads(connection.execute("SELECT data FROM audit WHERE event='review_document'").fetchone()[0])
        self.assertEqual(audit["source_evidence"]["po_number"]["page"], 1)
        self.assertEqual(audit["original_suggestions"]["po_number"], "PO-TEST")

    def test_idempotency_stale_version_duplicate_documents_and_reply(self):
        invoice, _ = self.create()
        uploaded, body, content = self.attach(invoice)
        self.assertEqual(self.service.attach_document(invoice["id"], body), uploaded)
        self.assertEqual(self.service.document_blob(uploaded["documents"][0]["id"])[0], content)
        duplicate_body = dict(body, expected_version=uploaded["version"], idempotency_key="new-doc-attempt")
        self.assert_error("duplicate_document", self.service.attach_document, invoice["id"], duplicate_body)
        self.assert_error("stale_version", self.action, invoice, "record_reply", {"text": "Old view"})
        body = {"action": "record_reply", "parameters": {"text": "Customer replied manually."}, "expected_version": uploaded["version"], "idempotency_key": "same-reply"}
        first = self.service.action(invoice["id"], body)
        replay = self.service.action(invoice["id"], body)
        self.assertFalse(first["duplicate"])
        self.assertTrue(replay["duplicate"])
        self.assertEqual(len(replay["invoice"]["replies"]), 1)
        self.assertFalse(replay["invoice"]["documents"][0]["reviewed"])
        self.assert_error("idempotency_conflict", self.service.action, invoice["id"], dict(body, parameters={"text": "Changed reply"}))

    def test_simultaneous_approvals_cannot_overallocate_purchase_order(self):
        invoices = [self.ready(self.create()[0]) for _ in range(2)]
        def prepare(invoice):
            try:
                return self.action(invoice, "approve_resolution")["invoice"]["status"]
            except DomainError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(prepare, invoices))
        self.assertEqual(sorted(outcomes), ["checks_not_passed", "prepared"])
        self.assertEqual(self.service.list_invoices(view="prepared")["total"], 1)

    def test_provider_suggestions_retain_source_and_require_human_review(self):
        self.service = Service(self.path, demo=False, provider="together", together_key="unused-test-key")
        invoice, _ = self.create()
        fields = {"po_number": "PO-TEST", "customer": "Example Staffing", "currency": "USD", "amount": "100.00"}
        evidence = {name: {"page": 1, "snippet": snippet} for name, snippet in {
            "po_number": "PO Number: PO-TEST", "customer": "Customer: Example Staffing",
            "currency": "Currency: USD", "amount": "Amount: 100.00",
        }.items()}
        with patch("invoiceclear.service.together_extract", return_value=(fields, evidence, None, 120)):
            uploaded, _, _ = self.attach(invoice)
        document = uploaded["documents"][0]
        self.assertEqual(document["extraction_method"], "together")
        self.assertEqual(document["suggested_fields"], fields)
        self.assertEqual(document["evidence"], evidence)
        for source in document["evidence"].values():
            page = next(p for p in document["pages"] if p["number"] == source["page"])
            self.assertIn(source["snippet"], page["text"])
        self.assertIsNone(document["extraction_error"])
        self.assertFalse(document["reviewed"])
        self.assertEqual(document["review_evidence"], {})
        self.assertIsNone(document["reviewed_by"])
        self.assertIsNone(document["reviewed_at"])
        self.assertEqual(self.service.get_invoice(invoice["id"])["documents"][0], document)
        self.assertEqual(uploaded["status"], "needs_review")
        self.assert_error("checks_not_passed", self.action, uploaded, "approve_resolution")

    def test_provider_failure_is_retained_and_cannot_approve(self):
        self.service = Service(self.path, demo=False, provider="together", together_key="unused-test-key")
        invoice, _ = self.create()
        with patch("invoiceclear.service.together_extract", return_value=({}, {}, "Provider is unavailable.", 0)) as extraction:
            uploaded, _, _ = self.attach(invoice)
        extraction.assert_called_once()
        document = uploaded["documents"][0]
        self.assertEqual(document["extraction_method"], "together")
        self.assertIn("unavailable", document["extraction_error"])
        self.assertFalse(document["reviewed"])
        self.assertEqual(document["evidence"], {})
        self.assertEqual(document["review_evidence"], {})
        self.assertIsNone(document["reviewed_by"])
        self.assertIsNone(document["reviewed_at"])
        self.assert_error("checks_not_passed", self.action, uploaded, "approve_resolution")

    def test_extraction_probe_rules_path_has_no_provider_or_case_mutations(self):
        with patch("invoiceclear.service.together_extract") as extraction:
            result = self.service.probe_extraction()
        extraction.assert_not_called()
        self.assertEqual(result["fields"], {"po_number": "IC-CONNECTION-CHECK", "customer": "InvoiceClear Connection Check", "currency": "USD", "amount": "1.00"})
        self.assertIsNone(result["error"])
        self.assertEqual(set(result), {"fields", "evidence", "error"})
        with sqlite3.connect(self.path) as connection:
            for table in ("invoices", "documents", "replies", "audit", "resolutions", "idempotency", "provider_usage"):
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_probe_and_upload_share_reserved_together_quota(self):
        self.service = Service(self.path, demo=False, provider="together", together_key="unused-test-key")
        invoice, _ = self.create()
        with patch("invoiceclear.service.together_extract", return_value=({}, {}, "Provider is unavailable.", 0)) as extraction:
            for _ in range(4):
                self.assertEqual(self.service.probe_extraction()["error"], "Provider is unavailable.")
            uploaded, _, original = self.attach(invoice)
            result = self.service.probe_extraction()
        self.assertEqual(extraction.call_count, 5)
        self.assertIn("Daily extraction limit", result["error"])
        self.assertEqual(self.service.document_blob(uploaded["documents"][0]["id"])[0], original)
        self.assertEqual(uploaded["status"], "needs_review")
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT calls,tokens FROM provider_usage").fetchone(), (5, 500_000))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM invoices").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audit").fetchone()[0], 2)

    def test_unusable_model_input_never_dispatches_or_reserves_quota(self):
        self.service = Service(self.path, demo=False, provider="together", together_key="unused-test-key")
        with patch("invoiceclear.service.together_extract") as extraction:
            for source in ("", " " * 3, "x" * 24001):
                fields, evidence, error = self.service._extract([{"number": 1, "text": source}])
                self.assertEqual(fields, {}); self.assertEqual(evidence, {}); self.assertTrue(error)
        extraction.assert_not_called()
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM provider_usage").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM invoices").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)

    def test_together_dispatch_has_committed_quota_and_no_database_write_lock(self):
        self.service = Service(self.path, demo=False, provider="together", together_key="unused-test-key")
        invoice, _ = self.create()

        def extract(*_):
            with sqlite3.connect(self.path, timeout=0) as connection:
                self.assertEqual(connection.execute("SELECT calls,tokens FROM provider_usage").fetchone(), (1, 100_000))
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()
            return {}, {}, "Provider is unavailable.", 0

        with patch("invoiceclear.service.together_extract", side_effect=extract) as extraction:
            uploaded, body, original = self.attach(invoice)
            with patch("invoiceclear.service.PUBLIC_MAX_ROWS", 0), patch("invoiceclear.service.PUBLIC_MAX_BYTES", 0):
                self.assertEqual(self.service.attach_document(invoice["id"], body), uploaded)
            self.assert_error("idempotency_conflict", self.service.attach_document, invoice["id"], dict(body, filename="another.txt"))
        extraction.assert_called_once()
        self.assertEqual(self.service.document_blob(uploaded["documents"][0]["id"])[0], original)
        self.assertFalse(uploaded["documents"][0]["reviewed"])
        self.assertEqual(uploaded["documents"][0]["extraction_error"], "Provider is unavailable.")

    def test_simultaneous_together_upload_retries_across_service_instances_dispatch_once(self):
        self.service = Service(self.path, demo=False, provider="together", together_key="unused-test-key")
        another = Service(self.path, demo=False, provider="together", together_key="unused-test-key")
        invoice, _ = self.create()
        body = {"filename": "po.txt", "content_base64": base64.b64encode(b"PO Number: PO-TEST").decode(),
                "expected_version": invoice["version"], "idempotency_key": "concurrent-extraction"}
        start = threading.Barrier(4)
        release = threading.Event()

        def attach(index):
            start.wait(timeout=2)
            return (self.service if index % 2 else another).attach_document(invoice["id"], body)

        def extract(*_):
            release.wait(timeout=0.1)
            return {}, {}, "Provider is unavailable.", 0

        with patch("invoiceclear.service.together_extract", side_effect=extract) as extraction:
            with ThreadPoolExecutor(max_workers=4) as workers:
                results = list(workers.map(attach, range(4)))
        extraction.assert_called_once()
        self.assertTrue(all(result == results[0] for result in results))
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT calls,tokens FROM provider_usage").fetchone(), (1, 100_000))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 1)

    def test_together_upload_rechecks_version_after_dispatch_without_refunding_attempt(self):
        self.service = Service(self.path, demo=False, provider="together", together_key="unused-test-key")
        invoice, _ = self.create()

        def extract(*_):
            self.action(invoice, "record_reply", {"text": "A customer reply arrived during extraction."})
            return {}, {}, "Provider is unavailable.", 0

        with patch("invoiceclear.service.together_extract", side_effect=extract) as extraction:
            self.assert_error("stale_version", self.attach, invoice)
        extraction.assert_called_once()
        detail = self.service.get_invoice(invoice["id"])
        self.assertEqual(len(detail["replies"]), 1)
        self.assertEqual(detail["documents"], [])
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT calls,tokens FROM provider_usage").fetchone(), (1, 100_000))

    def test_together_quota_reconciles_reported_usage_and_reserves_unknown_attempts(self):
        self.service = Service(self.path, demo=False, provider="together", together_key="unused-test-key")
        day = datetime.now(timezone.utc).date().isoformat()
        fields = {"po_number": "IC-CONNECTION-CHECK", "customer": "InvoiceClear Connection Check", "currency": "USD", "amount": "1.00"}
        with patch("invoiceclear.service.together_extract", return_value=(fields, {}, None, 120)) as extraction:
            result = self.service.probe_extraction()
        self.assertEqual(result["fields"], fields)
        args = extraction.call_args.args
        self.assertIn("PO Number: IC-CONNECTION-CHECK", args[0][0]["text"])
        self.assertEqual(args[2], self.service.together_model)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT calls,tokens FROM provider_usage").fetchone(), (1, 120))
        with patch("invoiceclear.service.together_extract", return_value=(fields, {}, None, 0)):
            self.service.probe_extraction()
        with patch("invoiceclear.service.together_extract", side_effect=RuntimeError("private provider response")):
            result = self.service.probe_extraction()
        self.assertNotIn("private provider", result["error"])
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT calls,tokens FROM provider_usage").fetchone(), (3, 200_120))
            connection.execute("UPDATE provider_usage SET calls=100,tokens=1 WHERE day=?", (day,))
        with patch("invoiceclear.service.together_extract") as extraction:
            self.assertIn("Daily extraction limit", self.service.probe_extraction()["error"])
        extraction.assert_not_called()

    def test_csv_atomic_duplicates_and_formula_safe_export(self):
        invoice, entry = self.create(invoice_number="=FORMULA()", source_ref="@source-formula")
        def as_csv(rows):
            output = io.StringIO(newline="")
            writer = csv.DictWriter(output, fieldnames=list(entry))
            writer.writeheader()
            writer.writerows(rows)
            return output.getvalue()
        self.assertEqual(self.service.import_invoices(as_csv([entry, entry])), {"created": 0, "duplicates": 2})
        self.assert_error("conflicting_duplicate", self.service.import_invoices, as_csv([dict(entry, invoice_number="NEW-IMPORT"), dict(entry, amount="61.00")]))
        self.assertEqual(self.service.list_invoices()["total"], 1)
        ready = self.ready(invoice)
        self.action(ready, "approve_resolution")
        row = next(csv.DictReader(io.StringIO(self.service.export_csv())))
        self.assertTrue(row["invoice_number"].startswith("'="))
        self.assertTrue(row["source_ref"].startswith("'@"))
        self.assert_error("invalid_csv", self.service.import_invoices, "invoice_number,amount\nNEW,60.00\n")

    def test_money_and_file_validation(self):
        for amount in [60, True, 60.0, "01.00", "-1.00", "0", "1.001", "NaN", "1e2"]:
            self.assert_error("invalid_amount", self.create, amount=amount)
        invoice, _ = self.create()
        body = {"filename": "fake.pdf", "content_base64": base64.b64encode(b"not a PDF").decode(), "expected_version": invoice["version"], "idempotency_key": "bad-file"}
        self.assert_error("corrupt_document", self.service.attach_document, invoice["id"], body)
        self.assert_error("unsupported_document", self.service.attach_document, invoice["id"], dict(body, filename="image.jpg"))
        self.assertEqual(self.service.get_invoice(invoice["id"])["version"], invoice["version"])

    def test_distinct_reference_and_customer_punctuation_never_collapse(self):
        invoice, _ = self.create(po_reference="PO-12")
        reviewed = self.ready(invoice, po="PO1-2")
        self.assertEqual(next(c for c in reviewed["checks"] if c["code"] == "po_reference")["status"], "fail")
        self.assert_error("checks_not_passed", self.action, reviewed, "approve_resolution")
        invoice, _ = self.create(customer="Example-Staffing")
        reviewed = self.ready(invoice, customer="Example Staffing")
        self.assertEqual(next(c for c in reviewed["checks"] if c["code"] == "customer")["status"], "fail")
        uploaded, _, _ = self.attach(self.create()[0])
        doc = uploaded["documents"][0]
        fields = dict(doc["suggested_fields"], customer="Example\nStaffing")
        self.assert_error("invalid_fields", self.action, uploaded, "review_document", {"document_id": doc["id"], "fields": fields, "note": "This is a deliberately malformed correction."})

    def test_pdf_text_original_bytes_scanned_encrypted_and_stream_limits(self):
        try:
            from pypdf import PdfWriter
            from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
        except ImportError:
            self.skipTest("pypdf is required for PDF ingestion checks")
        def pdf_content(*, count=1, stream=None, encrypted=False):
            writer = PdfWriter()
            for _ in range(count):
                page = writer.add_blank_page(width=612, height=792)
                if stream is not None:
                    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
                    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
                    contents = DecodedStreamObject()
                    contents.set_data(stream)
                    page[NameObject("/Contents")] = writer._add_object(contents.flate_encode())
            if encrypted:
                writer.encrypt("test-secret")
            output = io.BytesIO()
            writer.write(output)
            return output.getvalue()
        invoice, _ = self.create()
        stream = b"BT /F1 12 Tf 50 700 Td (PO Number: PO-TEST) Tj 0 -20 Td (Customer: Example Staffing) Tj 0 -20 Td (Currency: USD) Tj 0 -20 Td (Amount: 100.00) Tj ET"
        content = pdf_content(stream=stream)
        body = {"filename": "source.pdf", "content_base64": base64.b64encode(content).decode(), "expected_version": invoice["version"], "idempotency_key": "pdf-original"}
        uploaded = self.service.attach_document(invoice["id"], body)
        self.assertEqual(uploaded["documents"][0]["suggested_fields"]["po_number"], "PO-TEST")
        self.assertEqual(self.service.document_blob(uploaded["documents"][0]["id"])[0], content)
        for content, error in [(pdf_content(encrypted=True), "encrypted_document"), (pdf_content(count=21), "document_too_large"), (pdf_content(stream=b" " * (MAX_PDF_STREAM + 1)), "document_too_large")]:
            fresh, _ = self.create()
            body.update(content_base64=base64.b64encode(content).decode(), expected_version=fresh["version"], idempotency_key=uuid.uuid4().hex)
            self.assert_error(error, self.service.attach_document, fresh["id"], body)
        fresh, _ = self.create()
        body.update(content_base64=base64.b64encode(pdf_content()).decode(), expected_version=fresh["version"], idempotency_key="scanned-only")
        held = self.service.attach_document(fresh["id"], body)
        self.assertEqual(held["status"], "needs_review")
        self.assertIn("No readable text", held["documents"][0]["extraction_error"])
        self.assert_error("checks_not_passed", self.action, held, "approve_resolution")

    def test_model_evidence_is_validated_without_network_calls(self):
        pages = [{"number": 1, "text": "PO Number: PO-TEST\nCustomer: Example Staffing\nCurrency: USD\nAmount: 100.00"}]
        fields = {"po_number": "PO-TEST", "customer": "Example Staffing", "currency": "USD", "amount": "100.00"}
        evidence = {name: {"page": 1, "snippet": pages[0]["text"]} for name in fields}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size): return json.dumps({"choices": [{"message": {"content": json.dumps({"fields": fields, "evidence": evidence})}}], "usage": {"total_tokens": 10}}).encode()
        with patch("invoiceclear.documents.urllib.request.urlopen", return_value=Response()):
            extracted, _, error, _ = together_extract(pages, "test-key", "test-model")
            self.assertEqual(extracted, fields)
            self.assertIsNone(error)
            evidence["amount"]["snippet"] = "Amount: 999.00"
            extracted, _, error, _ = together_extract(pages, "test-key", "test-model")
            self.assertEqual(extracted, {})
            self.assertIn("unsupported evidence", error)


if __name__ == "__main__":
    unittest.main()
