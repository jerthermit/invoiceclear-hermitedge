"""Representative input files and access boundaries; no user records or providers."""
import base64
import csv
import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch
from decimal import Decimal

from invoiceclear import Service
from invoiceclear.api import InvoiceHandler, create_server
from invoiceclear.config import Settings
from invoiceclear.documents import DomainError, decode_document, rules_extract
from invoiceclear.public_files import DIRECTORY, FILES, source_files
from invoiceclear.public_seed import public_cases, recording_case


class PublicFilesTests(unittest.TestCase):
    def test_archive_contains_only_declared_inputs_and_documents_match_the_cases(self):
        with zipfile.ZipFile(io.BytesIO(source_files())) as archive:
            self.assertEqual(archive.namelist(), list(FILES))
            self.assertIn("fictional", archive.read("README.md").decode())
            rows = list(csv.DictReader(io.StringIO(archive.read("invoices.csv").decode())))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["invoice_number"], "STA-2026-2101")
            self.assertEqual(rows[0], recording_case()["entry"])
            def extract(filename):
                content = archive.read(filename)
                self.assertTrue(content.startswith(b"%PDF-"))
                _, _, mime, pages = decode_document({"filename": Path(filename).name,
                                                    "content_base64": base64.b64encode(content).decode()})
                self.assertEqual(mime, "application/pdf")
                self.assertEqual(len(pages), 2)
                return rules_extract(pages)
            fields, _, error = extract("MVH-2026-2128.pdf")
            self.assertIsNone(error)
            self.assertEqual(fields, {"customer": rows[0]["customer"], "currency": "USD", "po_number": "MVH-2026-2128", "amount": "24000.00"})
            self.assertNotEqual(fields["po_number"], rows[0]["po_reference"])
            for filename, customer, amount in (("MVH-CLIN-2026-0801.pdf", "Mercer Valley Home Health LLC", "24000.00"),
                                                ("LRL-TEMP-2026-082.pdf", "Leonard & Reyes Logistics LLC", "12600.00")):
                fields, _, error = extract(filename)
                self.assertIsNone(error)
                self.assertEqual((fields["customer"], fields["amount"]), (customer, amount))

    def test_complete_pack_matches_every_source_invoice_line_reply_and_retained_pdf(self):
        with tempfile.TemporaryDirectory(prefix="invoiceclear-pack-parity-") as directory:
            with zipfile.ZipFile(io.BytesIO(source_files())) as archive:
                def rows(name):
                    return list(csv.DictReader(io.StringIO(archive.read(name).decode())))
                entries = rows("all-invoices.csv")
                self.assertEqual(entries, [item["entry"] for item in public_cases()])
                self.assertEqual(len(entries), 36)
                invoices = {row["invoice_number"]: row for row in entries}
                lines = rows("billing-lines.csv")
                self.assertEqual(len(lines), 64)
                for number, invoice in invoices.items():
                    billing = [line for line in lines if line["invoice_number"] == number]
                    self.assertEqual(sum(Decimal(line["amount"]) for line in billing), Decimal(invoice["amount"]))
                    self.assertTrue(all(line["customer"] == invoice["customer"] and line["currency"] == invoice["currency"] for line in billing))
                self.assertEqual(len(rows("correspondence.csv")), 16)
                manifest = rows("purchase-orders.csv")
                self.assertEqual(len(manifest), 35)
                service = Service(Path(directory) / "public.sqlite3")
                for record in manifest:
                    self.assertIn(record["filename"], archive.namelist())
                    self.assertNotIn("..", Path(record["filename"]).parts)
                    number = record["invoice_number"].rsplit("-", 1)[-1]
                    detail = service.get_invoice("demo-invoice-" + number)
                    document = next(doc for doc in detail["documents"] if doc["filename"] == Path(record["filename"]).name)
                    original, filename, mime = service.document_blob(document["id"])
                    self.assertEqual(mime, "application/pdf")
                    self.assertEqual(original, archive.read(record["filename"]))
                    self.assertEqual(document["sha256"], hashlib.sha256(original).hexdigest())
                    self.assertEqual(document["suggested_fields"], {"po_number": record["po_number"], "customer": record["customer"], "currency": record["currency"], "amount": record["authorized_ceiling"]})
                # CSV import never fabricates PO attachments, reviews or approvals.
                private = Service(Path(directory) / "private.sqlite3", demo=False)
                payload = archive.read("all-invoices.csv").decode()
                self.assertEqual(private.import_invoices(payload), {"created": 36, "duplicates": 0})
                self.assertEqual(private.import_invoices(payload), {"created": 0, "duplicates": 36})
                self.assertEqual(private.list_invoices(view="prepared")["total"], 0)
                self.assertTrue(all(not private.get_invoice(row["id"])["documents"] for row in private.list_invoices(view="all")["invoices"]))

    def test_fixed_pdf_pack_is_complete_and_bounded(self):
        # Regeneration is authoring-only; production needs only committed PDFs.
        self.assertEqual(len([name for name in FILES if name.endswith(".pdf")]), 38)
        self.assertFalse(any(name.endswith(".txt") for name in FILES))
        for name in FILES:
            self.assertLessEqual((DIRECTORY / name).stat().st_size, 65_536)

    def test_missing_or_oversized_source_fails_without_disclosing_paths(self):
        with tempfile.TemporaryDirectory(prefix="invoiceclear-public-file-test-") as directory:
            with patch("invoiceclear.public_files.DIRECTORY", Path(directory)):
                with self.assertRaises(DomainError) as missing:
                    source_files()
                self.assertEqual(missing.exception.status, 503)
                self.assertNotIn(directory, missing.exception.message)
                (Path(directory) / FILES[0]).write_bytes(b"x" * 65_537)
                with self.assertRaises(DomainError):
                    source_files()

    def test_route_requires_sign_in_exact_origin_and_public_mode(self):
        with tempfile.TemporaryDirectory(prefix="invoiceclear-public-route-") as directory:
            for mode in ("demo", "private"):
                settings = Settings(database_path=Path(directory) / (mode + ".sqlite3"), web_path=Path(directory),
                                    mode=mode, email="finance@acceptance.example", password="isolated-private-password", backups_enabled=False)
                with patch("invoiceclear.api.ThreadingHTTPServer.__init__", return_value=None):
                    server = create_server(settings)
                try:
                    token = server.sessions.create(settings.email, settings.password)["access_token"]
                    for signed_in, origin, expected in ((False, None, 401), (True, "https://untrusted.example", 403),
                                                        (True, None, 200 if mode == "demo" else 403)):
                        handler = InvoiceHandler.__new__(InvoiceHandler)
                        handler.server, handler.path, handler.command = server, "/api/public/source-files", "GET"
                        handler.headers = {"Host": "127.0.0.1"}
                        if signed_in:
                            handler.headers["Authorization"] = "Bearer " + token
                        if origin:
                            handler.headers["Origin"] = origin
                        handler._json, handler._send = Mock(), Mock()
                        handler.do_GET()
                        if expected == 200:
                            handler._send.assert_called_once()
                            self.assertEqual(handler._send.call_args.args[1], "application/zip")
                            handler._json.assert_not_called()
                        else:
                            self.assertEqual(handler._json.call_args.args[1], expected)
                            handler._send.assert_not_called()
                finally:
                    server.capture.close()
                    server.maintenance.close()

    def test_container_includes_the_fixed_source_files(self):
        backend = Path(__file__).resolve().parents[1]
        self.assertIn("COPY fixtures ./fixtures", (backend / "Dockerfile").read_text())
        self.assertEqual(DIRECTORY, backend / "fixtures/public")
        self.assertTrue(all((DIRECTORY / name).is_file() for name in FILES))


if __name__ == "__main__":
    unittest.main()
