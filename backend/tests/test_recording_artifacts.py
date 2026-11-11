"""Fixed-source provenance and actual recording downloads; temporary state only."""
import base64
import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from invoiceclear import Service
from invoiceclear.delivery import billing_brief, billing_lines_csv
from invoiceclear.public_files import DIRECTORY
from invoiceclear.public_seed import FIXTURE, recording_case
from invoiceclear.service import ENTRY_FIELDS


class RecordingArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="invoiceclear-artifacts-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    @staticmethod
    def csv_text(entry):
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=ENTRY_FIELDS)
        writer.writeheader()
        writer.writerow(entry)
        return output.getvalue()

    @staticmethod
    def provenance(service, invoice_id):
        with service._connection() as connection:
            return json.loads(connection.execute("SELECT provenance FROM invoices WHERE id=?", (invoice_id,)).fetchone()[0])

    @staticmethod
    def act(service, invoice, name, parameters=None):
        return service.action(invoice["id"], {"action": name, "parameters": parameters or {},
                              "expected_version": invoice["version"], "idempotency_key": "recording-" + name})["invoice"]

    def prepared_import(self):
        service = Service(self.root / "recording.sqlite3", demo=False)
        entry = recording_case()["entry"]
        self.assertEqual(service.import_invoices(self.csv_text(entry)), {"created": 1, "duplicates": 0})
        invoice = service.get_invoice(service.list_invoices()["invoices"][0]["id"])
        original = (DIRECTORY / "MVH-2026-2128.pdf").read_bytes()
        invoice = service.attach_document(invoice["id"], {"filename": "MVH-2026-2128.pdf",
                    "content_base64": base64.b64encode(original).decode(),
                    "expected_version": invoice["version"], "idempotency_key": "recording-upload"})
        document = invoice["documents"][-1]
        invoice = self.act(service, invoice, "review_document", {"document_id": document["id"], "fields": document["suggested_fields"]})
        invoice = self.act(service, invoice, "propose_reference", {"po_reference": "MVH-2026-2128", "note": "Retained PO and customer source support the corrected reference."})
        return service, self.act(service, invoice, "approve_resolution"), original

    def test_exact_fixed_import_retains_context_in_both_modes(self):
        for mode in (False, True):
            service = Service(self.root / (str(mode) + ".sqlite3"), demo=mode)
            entry = recording_case()["entry"]
            service.import_invoices(self.csv_text(entry), actor="billing-team")
            invoice = service.list_invoices(q=entry["invoice_number"])["invoices"][0]
            source = self.provenance(service, invoice["id"])
            self.assertEqual(source["intake"], "csv")
            self.assertEqual(source["mode"], "demo" if mode else "private")
            self.assertEqual(source["actor"], "billing-team")
            self.assertEqual(source["fixture"], FIXTURE)
            self.assertIn("Fictional", source["disclosure"])
            self.assertEqual(source["billing_lines"], recording_case()["lines"])
            self.assertEqual(source["branch"], "Columbus East")

    def test_modified_rows_do_not_inherit_fabricated_billing_detail(self):
        entry = recording_case()["entry"]
        changes = {"invoice_number": "STA-2026-9999", "customer": entry["customer"].upper(),
                   "amount": "18451.00", "source_ref": entry["source_ref"] + " amended",
                   "customer_email": "another@mercervalleyhomehealth.example"}
        for field, value in changes.items():
            with self.subTest(field=field):
                service = Service(self.root / (field + ".sqlite3"), demo=False)
                service.import_invoices(self.csv_text(dict(entry, **{field: value})))
                invoice = service.list_invoices()["invoices"][0]
                source = self.provenance(service, invoice["id"])
                self.assertEqual(source["intake"], "csv")
                self.assertNotIn("fixture", source)
                self.assertNotIn("billing_lines", source)

    def test_import_pdf_handoff_brief_and_export_agree_on_frozen_source(self):
        service, invoice, original = self.prepared_import()
        frozen = invoice["resolution"]
        with service._write() as connection:
            connection.execute("UPDATE invoices SET provenance=? WHERE id=?", ('{"branch":"DO NOT EXPORT"}', invoice["id"]))
            connection.execute("UPDATE documents SET filename='changed.txt' WHERE id=?", (frozen["document_id"],))
        bundle, _ = service.handoff(invoice["id"])
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            self.assertEqual(archive.read("purchase-order.pdf"), original)
            self.assertEqual(json.loads(archive.read("resolution.json")), frozen)
            lines = list(csv.DictReader(io.StringIO(archive.read("billing-lines.csv").decode())))
            self.assertEqual(len(lines), 1)
            self.assertEqual((lines[0]["quantity"], lines[0]["unit_rate"], lines[0]["amount"]), ("246", "75.00", "18450.00"))
            self.assertEqual(lines[0]["source_fixture"], FIXTURE)
            brief = archive.read("billing-brief.html").decode()
            note = archive.read("billing-note.txt").decode()
            self.assertIn("Source invoice lines", brief)
            self.assertIn("Columbus East", brief)
            self.assertIn("18450.00", brief)
            self.assertEqual(brief.count("Example workflow fixture; not a client outcome."), 1)
            self.assertIn("Example workflow fixture; not a client outcome.", note)
            self.assertNotIn("DO NOT EXPORT", brief)
        csv_reader = csv.DictReader(io.StringIO(service.export_csv()))
        self.assertEqual(csv_reader.fieldnames[:13], ["invoice_number", "customer", "amount", "currency", "source_po_reference", "prepared_po_reference", "prepared_at", "prepared_by", "source_ref", "payment_status", "internal_status", "document_filename", "document_sha256"])
        rows = list(csv_reader)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["source_intake"], "csv")
        self.assertEqual(row["source_fixture"], FIXTURE)
        self.assertEqual(row["service_branch"], "Columbus East")
        self.assertEqual(row["document_filename"], "MVH-2026-2128.pdf")
        self.assertEqual(row["approval_id"], frozen["id"])
        self.assertEqual(json.loads(row["billing_lines"]), frozen["source_provenance"]["billing_lines"])
        self.assertEqual((row["source_po_reference"], row["prepared_po_reference"], row["amount"], row["payment_status"]), ("MVH-2026-2181", "MVH-2026-2128", "18450.00", "unpaid"))

    def test_new_export_cells_are_spreadsheet_safe_and_brief_escapes_source(self):
        service, invoice, _ = self.prepared_import()
        snapshot = json.loads(json.dumps(invoice["resolution"]))
        source = snapshot["source_provenance"]
        source.update(branch="=HYPERLINK()", department="@SUM(1)", account_code="+COMMAND", disclosure="\t=1")
        source["billing_lines"][0]["description"] = '<img src="https://untrusted.example/x" onerror="alert(1)">'
        with service._write() as connection:
            connection.execute("UPDATE resolutions SET data=? WHERE id=?", (json.dumps(snapshot), snapshot["id"]))
        row = next(csv.DictReader(io.StringIO(service.export_csv())))
        self.assertEqual(row["service_branch"], "'=HYPERLINK()")
        self.assertEqual(row["department"], "'@SUM(1)")
        self.assertEqual(row["business_account"], "'+COMMAND")
        self.assertEqual(row["source_disclosure"], "'\t=1")
        line = next(csv.DictReader(io.StringIO(billing_lines_csv(snapshot))))
        self.assertEqual(line["service_branch"], "'=HYPERLINK()")
        brief = billing_brief(snapshot, "purchase-order.pdf")
        self.assertNotIn('<img src="https://untrusted.example', brief)
        self.assertIn("&lt;img", brief)

    def test_legacy_without_retained_lines_does_not_invent_a_line_export(self):
        self.assertIsNone(billing_lines_csv({"source_provenance": {"intake": "manual"}}))
        service = Service(self.root / "public.sqlite3")
        prepared = service.get_invoice("demo-invoice-1071")
        row = next(csv.DictReader(io.StringIO(service.export_csv())))
        self.assertEqual(row["source_fixture"], FIXTURE)
        self.assertTrue(json.loads(row["billing_lines"]))
        self.assertEqual(prepared["payment_status"], "unpaid")


if __name__ == "__main__":
    unittest.main()
