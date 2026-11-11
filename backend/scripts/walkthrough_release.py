"""Representative HTTP acceptance. Temporary records, controlled model protocol, no live accounts."""
import base64
import io
import json
import sqlite3
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from invoiceclear.api import create_server
from invoiceclear.config import Settings


def call(name, arguments=None):
    return {"id": "call-" + name, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments or {}),
    }}


class ProtocolResponse:
    def __init__(self, calls):
        self.raw = json.dumps({"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": calls,
        }}], "usage": {"total_tokens": 300}}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        return self.raw[:size]


def run():
    with tempfile.TemporaryDirectory(prefix="invoiceclear-representative-") as folder:
        settings = Settings(database_path=Path(folder) / "public.sqlite3", web_path=ROOT / "frontend/web",
                            port=0, agent_enabled=True, together_key="isolated-protocol-only-key", backups_enabled=False)
        server, thread, base, token = None, None, None, None
        model_calls = 0

        def start():
            nonlocal server, thread, base
            server = create_server(settings)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = "http://127.0.0.1:" + str(server.server_port)

        def stop():
            if server:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

        def request(path, data=None, expected=200, auth=True):
            headers = {"Content-Type": "application/json"}
            if auth and token:
                headers["Authorization"] = "Bearer " + token
            raw = None if data is None else json.dumps(data).encode()
            try:
                response = urlopen(Request(base + path, data=raw, headers=headers), timeout=10)
            except HTTPError as error:
                response = error
            with response:
                content = response.read()
                value = json.loads(content) if "application/json" in response.headers.get("Content-Type", "") else content
                assert response.status == expected, (path, response.status, value)
                return value

        def action(invoice, name, parameters=None, *, expected=200):
            return request("/api/invoices/" + invoice["id"] + "/actions", {
                "action": name, "parameters": parameters or {}, "expected_version": invoice["version"],
                "idempotency_key": "walkthrough-" + name,
            }, expected=expected)

        def protocol(request_, **kwargs):
            nonlocal model_calls
            assert request_.full_url == "https://api.together.ai/v1/chat/completions"
            model_calls += 1
            payload = json.loads(request_.data)
            seen = {message["tool_call_id"]: json.loads(message["content"]) for message in payload["messages"] if message["role"] == "tool"}
            if not seen:
                return ProtocolResponse([call("read_invoice"), call("read_checks"), call("list_evidence")])
            if "call-read_document_page" not in seen:
                listing = seen["call-list_evidence"]
                return ProtocolResponse([
                    call("read_document_page", {"document_id": listing["documents"][-1]["id"], "page": 1}),
                    call("read_reply", {"reply_id": listing["replies"][-1]["id"]}),
                ])
            document = seen["call-read_document_page"]
            reply = seen["call-read_reply"]
            return ProtocolResponse([call("finish_investigation", {
                "status": "completed", "summary": "The returned PO and customer reply support MVH-2026-2128; the invoice reference is transposed.",
                "next_step": "propose_reference", "draft": "",
                "citations": [{"source_id": document["source_id"], "excerpt": "PO Number: MVH-2026-2128"},
                              {"source_id": reply["source_id"], "excerpt": "The ledger reference MVH-2026-2181 was transposed."}],
            })])

        start()
        try:
            token = request("/api/session", {"email": settings.email, "password": settings.password}, auth=False)["access_token"]
            assert request("/api/invoices?view=all")["total"] == 36
            request("/api/public/source-files", auth=False, expected=401)
            with zipfile.ZipFile(io.BytesIO(request("/api/public/source-files"))) as archive:
                csv = archive.read("invoices.csv").decode()
                original_po = archive.read("MVH-2026-2128.pdf")
                all_invoices_csv = archive.read("all-invoices.csv").decode()
                assert "fictional" in archive.read("README.md").decode()
            assert request("/api/invoices/import", {"csv": csv}) == {"created": 1, "duplicates": 0}
            assert request("/api/invoices/import", {"csv": csv}) == {"created": 0, "duplicates": 1}
            assert request("/api/invoices/import", {"csv": all_invoices_csv}) == {"created": 0, "duplicates": 36}
            row = request("/api/invoices?q=STA-2026-2101")["invoices"][0]
            path = "/api/invoices/" + row["id"]
            invoice = request(path)
            assert invoice["status"] == "missing_po" and invoice["request"]["to"] == "ap@mercervalleyhomehealth.example"
            reply = "Please use MVH-2026-2128 for August clinical staffing. The ledger reference MVH-2026-2181 was transposed. This is authorization, not payment confirmation."
            invoice = action(invoice, "record_reply", {"text": reply})["invoice"]
            assert invoice["work"]["label"] == "Review customer reply"
            invoice = request(path + "/documents", {"filename": "MVH-2026-2128.pdf", "content_base64": base64.b64encode(original_po).decode(),
                              "expected_version": invoice["version"], "idempotency_key": "walkthrough-upload"})
            assert invoice["status"] == "needs_review"
            assert action(invoice, "approve_resolution", expected=409)["error"]["code"] == "checks_not_passed"
            document = invoice["documents"][-1]
            invoice = action(invoice, "review_document", {"document_id": document["id"], "fields": document["suggested_fields"]})["invoice"]
            assert invoice["status"] == "mismatch" and {check["code"] for check in invoice["checks"] if check["status"] != "pass"} == {"po_reference"}
            before = server.service.get_invoice(invoice["id"])
            body = {"expected_version": invoice["version"], "idempotency_key": "walkthrough-investigation"}
            with patch("invoiceclear.investigator.urllib.request.urlopen", side_effect=protocol):
                investigation = request(path + "/investigate", body)
                assert request(path + "/investigate", body) == investigation
            assert investigation["status"] == "completed" and investigation["next_step"] == "propose_reference"
            assert len(investigation["citations"]) == 2 and not investigation["stale"]
            assert model_calls == 3 and server.service.get_invoice(invoice["id"]) == before
            invoice = action(invoice, "propose_reference", {"po_reference": "MVH-2026-2128", "note": "Retained PO and customer reply confirm the transposed source reference."})["invoice"]
            assert invoice["status"] == "ready" and invoice["investigation"]["latest"]["stale"]
            approval = {"action": "approve_resolution", "parameters": {}, "expected_version": invoice["version"], "idempotency_key": "walkthrough-approve"}
            result = request(path + "/actions", approval)
            assert not result["duplicate"] and request(path + "/actions", approval)["duplicate"]
            current = request(path)
            assert current["status"] == "prepared" and current["payment_status"] == "unpaid"
            assert current["amount_cents"] == 1_845_000 and current["po_reference"] == "MVH-2026-2181"
            with zipfile.ZipFile(io.BytesIO(request(path + "/handoff"))) as archive:
                assert archive.read("purchase-order.pdf") == original_po
                snapshot = json.loads(archive.read("resolution.json"))
                assert snapshot["source_po_reference"] == "MVH-2026-2181" and snapshot["po_reference"] == "MVH-2026-2128"
                assert "billing-brief.html" in archive.namelist()
                assert "billing-lines.csv" in archive.namelist()
                assert snapshot["source_provenance"]["fixture"] == "staffing-36-v3"
                assert snapshot["source_provenance"]["billing_lines"][0]["amount"] == "18450.00"
                assert "not a client outcome" in archive.read("billing-brief.html").decode().lower()
            assert "STA-2026-2101" in request("/api/export.csv").decode("utf-8-sig")
            blocked = request("/api/invoices/demo-invoice-1044")
            assert action(blocked, "approve_resolution", expected=409)["error"]["code"] == "checks_not_passed"
            stop()
            start()
            restarted = request(path)  # Same stored sign-in remains valid.
            assert restarted["resolution"]["id"] == current["resolution"]["id"] and restarted["payment_status"] == "unpaid"
            assert restarted["investigation"]["latest"]["id"] == investigation["id"]
            with sqlite3.connect(settings.database_path) as connection:
                assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                approvals = connection.execute("SELECT COUNT(*) FROM audit WHERE invoice_id=? AND event='approve_resolution'", (invoice["id"],)).fetchone()[0]
                assert approvals == 1
            print(json.dumps({"status": "passed", "workspace_invoices": 37, "source_file_import": "real local HTTP",
                              "agent_protocol": "controlled responses; no live LLM", "agent_tool_calls_recorded": len(investigation["trace"]),
                              "model_rounds": model_calls, "duplicate_import_skipped": True, "committed_approvals": approvals,
                              "insufficient_coverage_blocked": True, "original_po_bytes_preserved": True,
                              "restart_persistence": True, "payment_status": "unpaid", "sqlite_integrity": "ok"}))
        finally:
            stop()


if __name__ == "__main__":
    run()
