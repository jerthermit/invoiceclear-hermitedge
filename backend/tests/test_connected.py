"""Connected workflow checks use isolated databases and mocked providers only."""
import base64
import copy
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import uuid
from email import policy
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from invoiceclear.connected import ConnectedWorkflow
from invoiceclear.service import DomainError, Service


def invoice(number="101", **changes):
    return {"Id": number, "SyncToken": "0", "DocNumber": "INV-" + number,
            "CustomerRef": {"value": "42", "name": "Acme Logistics"},
            "BillEmail": {"Address": "ap@acme.co"}, "CurrencyRef": {"value": "USD"},
            "TotalAmt": 100, "Balance": 100, "TxnDate": "2026-08-01", "DueDate": "2026-08-31",
            "CustomField": [{"DefinitionId": "1", "Name": "Purchase order", "Type": "StringType", "StringValue": ""},
                            {"DefinitionId": "2", "Name": "Project", "Type": "StringType", "StringValue": "Staffing"}],
            "PrivateNote": "Never use this as a purchase order.", **changes}


class FakeConnections:
    def __init__(self):
        self.accounts = {"qbo": {"configured": True, "connected": True, "account_id": "123", "account": "Agency", "environment": "sandbox"},
                         "gmail": {"configured": True, "connected": True, "account_id": "finance@staffco.co", "account": "finance@staffco.co"}}
        self.invoices = {"101": invoice()}
        self.sent, self.threads, self.attachments = {}, {}, {}
        self.calls = []
        self.send_failure = self.qbo_failure = None
        self.before_send = None
        self.before_update = None

    def status(self):
        return copy.deepcopy(self.accounts)

    def qbo(self, method, path, body=None, request_id=None, expected_account=None):
        if expected_account != self.accounts["qbo"]["account_id"]:
            raise DomainError("account_changed", "Accounting company changed.", 409)
        self.calls.append(("qbo", method, path, copy.deepcopy(body), request_id))
        if self.qbo_failure == "get" and method == "GET":
            raise TimeoutError("provider unavailable")
        if path.startswith("query?"):
            query = parse_qs(urlsplit(path).query)["query"][0]
            offset = int(query.split("STARTPOSITION ")[1].split()[0]) - 1
            return {"QueryResponse": {"Invoice": copy.deepcopy(list(self.invoices.values())[offset:offset + 20])}}
        if path == "preferences":
            return {"Preferences": {"CurrencyPrefs": {"HomeCurrency": {"value": "PHP"}}}}
        if path == "customer/42":
            return {"Customer": {"Id": "42", "DisplayName": "Acme Logistics"}}
        if method == "GET" and path.startswith("invoice/"):
            return {"Invoice": copy.deepcopy(self.invoices[path.split("/")[1]])}
        if method == "POST" and path == "invoice":
            if self.before_update:
                self.before_update()
            if self.qbo_failure == "update":
                raise DomainError("provider_error", "QuickBooks rejected this update.", 502)
            row = self.invoices[body["Id"]]
            if body["SyncToken"] != row["SyncToken"]:
                raise DomainError("provider_error", "Stale provider token.", 409)
            row["CustomField"] = copy.deepcopy(body["CustomField"])
            row["SyncToken"] = str(int(row["SyncToken"]) + 1)
            if self.qbo_failure == "timeout_after_update":
                raise TimeoutError("response lost")
            return {"Invoice": copy.deepcopy(row)}
        raise AssertionError((method, path))

    def gmail(self, method, path, body=None, expected_account=None):
        if expected_account != self.accounts["gmail"]["account_id"]:
            raise DomainError("account_changed", "Mailbox changed.", 409)
        self.calls.append(("gmail", method, path, copy.deepcopy(body), None))
        if method == "POST" and path == "messages/send":
            if self.before_send:
                self.before_send()
            if self.send_failure == "before":
                raise TimeoutError("not known whether sent")
            message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(body["raw"] + "=" * (-len(body["raw"]) % 4)))
            message_id = "sent-" + str(len(self.sent) + 1)
            sent = {"id": message_id, "threadId": "thread-1", "labelIds": ["SENT"], "payload": {"headers": [{"name": name, "value": str(value)} for name, value in message.items()]}}
            self.sent[message_id] = sent
            self.threads.setdefault("thread-1", []).append(sent)
            if self.send_failure == "after":
                raise TimeoutError("response lost after delivery")
            if self.send_failure == "missing_receipt":
                return {}
            return {"id": message_id, "threadId": "thread-1"}
        if method == "GET" and path.startswith("messages?"):
            query = parse_qs(urlsplit(path).query)["q"][0]
            expected = query.split("rfc822msgid:", 1)[1]
            return {"messages": [{"id": row["id"], "threadId": row["threadId"]} for row in self.sent.values() if any(h["name"].lower() == "message-id" and h["value"] == expected for h in row["payload"]["headers"])]}
        if method == "GET" and path.startswith("threads/"):
            thread_id = path.split("/")[1].split("?")[0]
            return {"id": thread_id, "messages": copy.deepcopy(self.threads.get(thread_id, []))}
        if method == "GET" and "/attachments/" in path:
            return {"data": self.attachments[path.rsplit("/", 1)[1]]}
        if method == "GET" and path.startswith("messages/"):
            return copy.deepcopy(self.sent[path.split("/")[1].split("?")[0]])
        raise AssertionError((method, path))


class ConnectedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="invoiceclear-connected-")
        self.service = Service(Path(self.temp.name) / "private.sqlite3", demo=False)
        self.connections = FakeConnections()
        self.settings = SimpleNamespace(mode="private", qbo_po_field_id="1")
        self.workflow = ConnectedWorkflow(self.service, self.connections, self.settings)

    def tearDown(self):
        self.temp.cleanup()

    def imported(self):
        self.workflow.sync_invoices()
        return self.service.list_invoices(view="all")["invoices"][0]

    def body(self, source, key=None):
        return {"expected_version": source["version"], "idempotency_key": key or str(uuid.uuid4()), "parameters": {}}

    def error(self, code, function, *args):
        with self.assertRaises(DomainError) as raised:
            function(*args)
        self.assertEqual(raised.exception.code, code)

    def action(self, source, name, parameters=None):
        return self.service.action(source["id"], {**self.body(source), "action": name, "parameters": parameters or {}}, actor="reviewer@staffco.co")["invoice"]

    def ready(self, source):
        content = b"PO Number: PO-2026\nCustomer: Acme Logistics\nCurrency: USD\nAmount: 100.00\n"
        uploaded = self.service.attach_document(source["id"], {**self.body(source), "filename": "purchase-order.txt", "content_base64": base64.b64encode(content).decode()})
        document = uploaded["documents"][-1]
        reviewed = self.action(uploaded, "review_document", {"document_id": document["id"], "fields": document["suggested_fields"]})
        return self.action(reviewed, "propose_reference", {"po_reference": "PO-2026", "note": "The original purchase order supports this reference."})

    def prepared(self, source):
        return self.action(self.ready(source), "approve_resolution")

    def reply(self, name="reply-1", sender="ap@acme.co", *, attachment=False, thread="thread-1"):
        parts = [{"partId": "0", "mimeType": "text/plain", "filename": "", "body": {"data": base64.urlsafe_b64encode(b"Please use the attached purchase order.").decode()}}]
        if attachment:
            content = b"PO Number: PO-2026\nCustomer: Acme Logistics\nCurrency: USD\nAmount: 100.00\n"
            self.connections.attachments["attachment-1"] = base64.urlsafe_b64encode(content).decode()
            parts.append({"partId": "1", "mimeType": "text/plain", "filename": "purchase-order.txt", "body": {"size": len(content), "attachmentId": "attachment-1"}})
        return {"id": name, "threadId": thread, "internalDate": "1788220800000", "payload": {"mimeType": "multipart/mixed", "headers": [{"name": "From", "value": sender}, {"name": "Message-ID", "value": f"<{name}@acme.co>"}], "parts": parts}}

    def race_after_preflight(self, operation="send", same_key=False, conflicting=False):
        source = self.imported()
        if operation == "apply":
            source = self.prepared(source)
        original = self.workflow._reserve
        barrier = threading.Barrier(2)
        completed = threading.Event()
        roles = threading.local()

        def delayed_reserve(*args, **kwargs):
            barrier.wait(timeout=5)
            if roles.index == 1 and not completed.wait(timeout=5):
                raise RuntimeError("The first concurrent action did not finish.")
            return original(*args, **kwargs)

        self.workflow._reserve = delayed_reserve
        method = self.workflow.send_request if operation == "send" else self.workflow.apply_reference

        def confirm(index):
            roles.index = index
            body = self.body(source, key="same-key" if same_key else f"retry-{index}")
            if conflicting and index == 1:
                body["parameters"] = {"different_intent": True}
            try:
                result = method(source["id"], body)
                return "duplicate" if result["duplicate"] else "confirmed"
            except DomainError as error:
                return error.code
            finally:
                if index == 0:
                    completed.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(confirm, (0, 1)))
        return outcomes

    def test_same_key_retry_racing_completed_send_returns_duplicate(self):
        self.assertEqual(self.race_after_preflight(same_key=True), ["confirmed", "duplicate"])
        self.assertEqual(len(self.connections.sent), 1)

    def test_different_keys_racing_same_draft_send_only_once(self):
        self.assertEqual(self.race_after_preflight(), ["confirmed", "duplicate"])
        self.assertEqual(len(self.connections.sent), 1)
        with self.service._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM external_actions").fetchone()[0], 1)

    def test_conflicting_retry_racing_completed_send_is_rejected(self):
        self.assertEqual(self.race_after_preflight(same_key=True, conflicting=True), ["confirmed", "idempotency_conflict"])
        self.assertEqual(len(self.connections.sent), 1)

    def test_different_keys_racing_approved_reference_post_only_once(self):
        self.assertEqual(self.race_after_preflight(operation="apply"), ["confirmed", "duplicate"])
        self.assertEqual(sum(call[:3] == ("qbo", "POST", "invoice") for call in self.connections.calls), 1)

    def test_later_reviewed_resolution_can_apply_another_reference(self):
        first = self.prepared(self.imported())
        self.workflow.apply_reference(first["id"], self.body(first))
        reopened = self.action(first, "reopen", {"note": "Customer supplied corrected purchase-order evidence."})
        content = b"PO Number: PO-2027\nCustomer: Acme Logistics\nCurrency: USD\nAmount: 100.00\n"
        uploaded = self.service.attach_document(first["id"], {**self.body(reopened), "filename": "corrected-po.txt", "content_base64": base64.b64encode(content).decode()})
        document = uploaded["documents"][-1]
        reviewed = self.action(uploaded, "review_document", {"document_id": document["id"], "fields": document["suggested_fields"]})
        proposed = self.action(reviewed, "propose_reference", {"po_reference": "PO-2027", "note": "Corrected source document supports the revised reference."})
        prepared = self.action(proposed, "approve_resolution")
        applied = self.workflow.apply_reference(first["id"], self.body(prepared))
        self.assertFalse(applied["duplicate"])
        self.assertEqual(applied["integration"]["accounting"]["po_reference"], "PO-2027")
        self.assertEqual(applied["invoice"]["amount_cents"], 10000)
        self.assertEqual(applied["invoice"]["payment_status"], "unpaid")
        self.assertEqual(sum(call[:3] == ("qbo", "POST", "invoice") for call in self.connections.calls), 2)

    def test_new_retry_keys_do_not_repeat_confirmed_business_actions(self):
        source = self.imported()
        first = self.workflow.send_request(source["id"], self.body(source))
        repeated = self.workflow.send_request(source["id"], self.body(source))
        self.assertFalse(first["duplicate"])
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(len(self.connections.sent), 1)
        prepared = self.prepared(source)
        self.workflow.apply_reference(source["id"], self.body(prepared))
        before = sum(call[:3] == ("qbo", "POST", "invoice") for call in self.connections.calls)
        repeated = self.workflow.apply_reference(source["id"], self.body(prepared))
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(sum(call[:3] == ("qbo", "POST", "invoice") for call in self.connections.calls), before)

    def test_import_requires_real_po_field_mapping(self):
        self.settings.qbo_po_field_id = ""
        self.error("po_field_required", self.workflow.sync_invoices)
        self.assertEqual(self.service.list_invoices()["total"], 0)
        self.settings.qbo_po_field_id = "99"
        result = self.workflow.sync_invoices()
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["skipped"][0]["code"], "po_field_unavailable")

    def test_confirmed_receipt_cannot_be_downgraded_by_stale_reconciliation(self):
        source = self.imported()
        self.workflow.send_request(source["id"], self.body(source))
        with self.service._connection() as connection:
            action = connection.execute("SELECT * FROM external_actions WHERE invoice_id=?", (source["id"],)).fetchone()
        self.workflow._finish(action, error="A concurrent lookup did not find the receipt yet.")
        self.assertEqual(self.workflow.get_detail(source["id"])["messages"][0]["status"], "sent")
        self.assertIsNone(self.workflow.get_detail(source["id"])["messages"][0]["error"])

    def test_long_gmail_attachment_identity_is_supported(self):
        source = self.imported()
        self.workflow.send_request(source["id"], self.body(source))
        reply = self.reply(attachment=True)
        long_id = "attachment_" + "a" * 300
        self.connections.attachments[long_id] = self.connections.attachments.pop("attachment-1")
        reply["payload"]["parts"][1]["body"]["attachmentId"] = long_id
        self.connections.threads["thread-1"].append(reply)
        result = self.workflow.sync_replies(source["id"])
        self.assertEqual(result["documents_added"], 1)
        self.assertFalse(result["failures"])

    def test_import_real_source_atomic_mapping_and_deduplication(self):
        first = self.workflow.sync_invoices()
        self.assertEqual((first["created"], first["refreshed"]), (1, 0))
        self.assertEqual(first["skipped"], [])
        source = self.service.get_invoice(self.service.list_invoices(view="all")["invoices"][0]["id"])
        self.assertEqual(source["source_ref"], "qbo:sandbox:123:invoice:101")
        self.assertEqual(source["po_reference"], "")
        self.assertEqual(source["amount_cents"], 10000)
        with self.service._connection() as connection:
            provenance = json.loads(connection.execute("SELECT provenance FROM invoices").fetchone()[0])
        self.assertEqual(provenance["intake"], "qbo")
        self.assertEqual(provenance["company_id"], "123")
        second = self.workflow.sync_invoices()
        self.assertEqual((second["created"], second["refreshed"]), (0, 1))
        self.assertEqual(self.service.list_invoices(view="all")["total"], 1)
        self.assertIn("MAXRESULTS 20", parse_qs(urlsplit(self.connections.calls[0][2]).query)["query"][0])

    def test_missing_email_skips_and_manual_identity_is_never_linked(self):
        self.connections.invoices["101"].pop("BillEmail")
        result = self.workflow.sync_invoices()
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["skipped"][0]["code"], "missing_bill_email")
        self.connections.invoices["101"] = invoice()
        manual = self.service.create_invoice({"invoice_number": "INV-101", "customer": "Acme Logistics", "customer_email": "ap@acme.co", "amount": "100.00", "currency": "USD", "issue_date": "2026-08-01", "due_date": "2026-08-31", "po_reference": "", "source_ref": "manual:101"})
        result = self.workflow.sync_invoices()
        self.assertEqual(result["skipped"][0]["code"], "local_identity_exists")
        self.assertIsNone(self.workflow.get_detail(manual["id"])["accounting"])
        self.error("source_not_connected", self.workflow.send_request, manual["id"], self.body(manual))

    def test_home_currency_and_customer_name_come_from_provider(self):
        raw = self.connections.invoices["101"]
        raw.pop("CurrencyRef")
        raw["CustomerRef"].pop("name")
        source = self.imported()
        self.assertEqual(source["currency"], "PHP")
        self.assertEqual(source["customer"], "Acme Logistics")
        self.assertEqual(sum(c[2] == "preferences" for c in self.connections.calls), 1)

    def work(self, source):
        rows = self.service.list_invoices(view="all")["invoices"]
        calls = len(self.connections.calls)
        work = self.workflow.worklist_context(rows)[source["id"]]
        self.assertEqual(len(self.connections.calls), calls, "A worklist read must not contact providers.")
        detail = self.service.get_invoice(source["id"])
        detail["integration"] = self.workflow.get_detail(source["id"])
        self.assertEqual(work, self.workflow.work_summary(detail))
        self.assertEqual(set(work), {"kind", "label"})
        return work["kind"]

    def test_worklist_follows_recorded_request_reply_review_and_handoff(self):
        source = self.imported()
        self.assertEqual(self.work(source), "request")
        self.workflow.send_request(source["id"], self.body(source))
        self.assertEqual(self.work(source), "replies")
        self.assertEqual(self.workflow.worklist_context([self.service.get_invoice(source["id"])])[source["id"]]["label"], "Awaiting purchase order")
        current = self.service.get_invoice(source["id"])
        current = self.action(current, "record_reply", {"text": "We will send the purchase order shortly."})
        self.assertEqual(self.workflow.worklist_context([current])[source["id"]]["label"], "Review customer reply")
        current = self.ready(current)
        self.assertEqual(self.work(current), "approve")
        current = self.action(current, "approve_resolution")
        self.assertEqual(self.work(current), "handoff")
        self.assertEqual(current["payment_status"], "unpaid")
        self.assertEqual(self.workflow.worklist_context([]), {})

    def test_worklist_prioritizes_source_and_receipt_holds(self):
        source = self.imported()
        self.connections.send_failure = "before"
        self.error("external_action_uncertain", self.workflow.send_request, source["id"], self.body(source))
        self.assertEqual(self.work(source), "verify-send")
        self.connections.accounts["qbo"]["connected"] = False
        self.assertEqual(self.work(source), "verify-send")

    def test_worklist_accounting_attention_and_update_receipt(self):
        source = self.prepared(self.imported())
        self.connections.invoices["101"]["Balance"] = 50
        self.workflow.refresh_invoice(source["id"])
        self.assertEqual(self.work(source), "refresh")
        self.connections.invoices["101"]["Balance"] = 100
        self.workflow.refresh_invoice(source["id"])
        self.connections.accounts["qbo"]["account_id"] = "another-company"
        self.assertEqual(self.work(source), "refresh")
        self.connections.accounts["qbo"]["account_id"] = "123"
        self.connections.qbo_failure = "update"
        self.error("external_action_uncertain", self.workflow.apply_reference, source["id"], self.body(source))
        self.assertEqual(self.work(source), "verify-update")

    def test_refresh_marks_stale_and_payment_without_overwriting_source(self):
        source = self.imported()
        self.connections.invoices["101"].update(TotalAmt=150, Balance=75)
        state = self.workflow.refresh_invoice(source["id"])
        self.assertTrue(state["source_changed"])
        self.assertEqual(state["payment_status"], "partial")
        self.assertEqual(state["balance_cents"], 7500)
        unchanged = self.service.get_invoice(source["id"])
        self.assertEqual(unchanged["amount_cents"], 10000)
        self.assertEqual(unchanged["payment_status"], "unpaid")
        self.error("accounting_stale", self.workflow.send_request, source["id"], self.body(source))
        self.connections.invoices["101"].update(TotalAmt=100, Balance=0)
        state = self.workflow.refresh_invoice(source["id"])
        self.assertFalse(state["source_changed"])
        self.assertEqual(state["payment_status"], "paid")
        self.error("payment_changed", self.workflow.send_request, source["id"], self.body(source))

    def test_approval_holds_persisted_accounting_changes_and_keeps_idempotent_replay(self):
        current = self.ready(self.imported())
        body = {**self.body(current), "action": "approve_resolution"}
        states = (
            {"payment_status": "unpaid", "source_changed": True, "error": None},
            {"payment_status": "partial", "source_changed": False, "error": None},
            {"payment_status": "paid", "source_changed": False, "error": None},
            {"payment_status": "unpaid", "source_changed": False, "error": "QuickBooks is unavailable."},
            {},
        )
        for state in states:
            with self.subTest(accounting=state):
                with self.service._write() as connection:
                    connection.execute("UPDATE external_invoices SET accounting=? WHERE invoice_id=?", (json.dumps(state), current["id"]))
                self.error("accounting_changed", self.service.action, current["id"], body)
                unchanged = self.service.get_invoice(current["id"])
                self.assertIsNone(unchanged["resolution"])
                self.assertEqual(unchanged["version"], current["version"])
        self.workflow.refresh_invoice(current["id"])
        approved = self.service.action(current["id"], body)
        self.assertFalse(approved["duplicate"])
        with self.service._write() as connection:
            connection.execute("UPDATE external_invoices SET accounting=? WHERE invoice_id=?", (json.dumps(states[1]), current["id"]))
        replay = self.service.action(current["id"], body)
        self.assertTrue(replay["duplicate"])
        self.assertEqual(replay["invoice"]["resolution"], approved["invoice"]["resolution"])

    def test_company_environment_and_demo_gates(self):
        source = self.imported()
        self.connections.accounts["qbo"]["account_id"] = "456"
        self.error("account_changed", self.workflow.refresh_invoice, source["id"])
        self.assertFalse(self.workflow.get_detail(source["id"])["accounting"]["connected"])
        self.settings.mode = "demo"
        self.error("demo_external_disabled", self.workflow.sync_invoices)

    def test_send_confirmed_receipt_and_idempotent_retry(self):
        source = self.imported()
        body = self.body(source)
        first = self.workflow.send_request(source["id"], body, "reviewer@staffco.co")
        self.assertFalse(first["duplicate"])
        mail = first["integration"]["messages"][0]
        self.assertEqual(mail["status"], "sent")
        self.assertEqual(mail["threadId"], "thread-1")
        self.assertEqual(mail["to"], "ap@acme.co")
        second = self.workflow.send_request(source["id"], body, "reviewer@staffco.co")
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(self.connections.sent), 1)
        self.error("idempotency_conflict", self.workflow.send_request, source["id"], {**body, "parameters": {"changed": True}})

    def test_uncertain_send_never_resends_and_reconciles_delivery(self):
        source = self.imported()
        body = self.body(source)
        self.connections.send_failure = "before"
        self.error("external_action_uncertain", self.workflow.send_request, source["id"], body)
        self.error("external_action_uncertain", self.workflow.send_request, source["id"], body)
        self.error("external_action_uncertain", self.workflow.send_request, source["id"], self.body(source))
        self.assertEqual(sum(c[0:2] == ("gmail", "POST") for c in self.connections.calls), 1)
        self.assertEqual(self.workflow.get_detail(source["id"])["messages"][0]["status"], "unknown")

    def test_lost_send_receipt_reconciles_real_sent_record(self):
        source = self.imported()
        body = self.body(source)
        self.connections.send_failure = "after"
        self.error("external_action_uncertain", self.workflow.send_request, source["id"], body)
        restarted = ConnectedWorkflow(self.service, self.connections, self.settings)
        replay = restarted.reconcile_request(source["id"])
        self.assertTrue(replay["duplicate"])
        self.assertEqual(replay["integration"]["messages"][0]["status"], "sent")
        self.assertEqual(sum(c[0:2] == ("gmail", "POST") for c in self.connections.calls), 1)

    def test_pending_send_is_durable_before_provider_dispatch(self):
        source = self.imported()
        observed = []
        def inspect_pending():
            with sqlite3.connect(self.service.database_path) as connection:
                observed.append(connection.execute("SELECT state FROM external_actions").fetchone()[0])
        self.connections.before_send = inspect_pending
        self.workflow.send_request(source["id"], self.body(source))
        self.assertEqual(observed, ["pending"])

    def test_local_actions_and_documents_cannot_race_reserved_provider_actions(self):
        source = self.imported()
        observed = []
        def during_send():
            self.error("external_action_pending", self.action, source, "record_reply", {"text": "Concurrent local edit."})
            self.error("external_action_pending", self.service.attach_document, source["id"], {**self.body(source), "filename": "concurrent.txt", "content_base64": base64.b64encode(b"A real text document.").decode()})
            observed.append("send protected")
        self.connections.before_send = during_send
        self.workflow.send_request(source["id"], self.body(source))
        prepared = self.prepared(source)
        def during_update():
            self.error("external_action_pending", self.action, prepared, "reopen", {"note": "Concurrent reopen must wait for the accounting receipt."})
            observed.append("apply protected")
        self.connections.before_update = during_update
        self.workflow.apply_reference(prepared["id"], self.body(prepared))
        self.assertEqual(observed, ["send protected", "apply protected"])

    def test_unchanged_total_partial_payment_blocks_send(self):
        source = self.imported()
        self.connections.invoices["101"]["Balance"] = 25
        self.error("payment_changed", self.workflow.send_request, source["id"], self.body(source))
        accounting = self.workflow.get_detail(source["id"])["accounting"]
        self.assertFalse(accounting["source_changed"])
        self.assertEqual(accounting["payment_status"], "partial")

    def test_send_rejects_stale_version_and_reserved_example_recipient(self):
        source = self.imported()
        self.action(source, "record_reply", {"text": "A manual reply."})
        self.error("stale_version", self.workflow.send_request, source["id"], self.body(source))
        self.connections.invoices["102"] = invoice("102", BillEmail={"Address": "ap@customer.example"})
        self.workflow.sync_invoices()
        other = next(i for i in self.service.list_invoices(view="all")["invoices"] if i["invoice_number"] == "INV-102")
        self.error("recipient_not_real", self.workflow.send_request, other["id"], self.body(other))

    def test_thread_bound_exact_sender_replies_and_document_deduplication(self):
        source = self.imported()
        self.workflow.send_request(source["id"], self.body(source))
        self.connections.threads["thread-1"].extend([self.reply("wrong", "attacker@elsewhere.co"), self.reply("other-thread", thread="unrelated"), self.reply(attachment=True)])
        self.connections.threads["unrelated"] = [self.reply("unrelated-customer", thread="unrelated")]
        first = self.workflow.sync_replies(source["id"])
        self.assertEqual((first["added"], first["documents_added"]), (1, 1))
        detail = first["invoice"]
        self.assertEqual(detail["replies"][0]["source"], "gmail")
        self.assertFalse(detail["documents"][0]["reviewed"])
        self.assertIsNone(detail["resolution"])
        incoming = next(m for m in first["integration"]["messages"] if m["direction"] == "incoming")
        self.assertFalse(incoming["sender_authenticated"])
        second = self.workflow.sync_replies(source["id"])
        self.assertEqual((second["added"], second["documents_added"]), (0, 0))
        self.assertEqual(len(second["invoice"]["replies"]), 1)
        self.assertFalse(any(c[0] == "gmail" and c[2].startswith("messages?") for c in self.connections.calls))

    def test_prepared_reply_is_retained_attachment_waits_for_reopen(self):
        source = self.imported()
        self.workflow.send_request(source["id"], self.body(source))
        prepared = self.prepared(source)
        self.connections.threads["thread-1"].append(self.reply(attachment=True))
        first = self.workflow.sync_replies(source["id"])
        self.assertEqual(first["added"], 1)
        self.assertEqual(first["documents_added"], 0)
        self.assertIn("Reopen", first["failures"][0]["reason"])
        current = self.service.get_invoice(prepared["id"])
        self.action(current, "reopen", {"note": "Customer sent more evidence for review."})
        second = self.workflow.sync_replies(source["id"])
        self.assertEqual(second["added"], 0)
        self.assertEqual(second["failures"], [])
        self.assertTrue(second["integration"]["messages"][-1]["attachments"][0]["document_id"])

    def test_apply_requires_approval_and_writes_only_configured_custom_field(self):
        source = self.imported()
        self.error("approval_required", self.workflow.apply_reference, source["id"], self.body(source))
        prepared = self.prepared(source)
        body = self.body(prepared)
        first = self.workflow.apply_reference(prepared["id"], body, "reviewer@staffco.co")
        update = next(c for c in self.connections.calls if c[0:2] == ("qbo", "POST"))
        self.assertEqual(set(update[3]), {"Id", "SyncToken", "sparse", "CustomField"})
        self.assertTrue(update[4])
        self.assertEqual(update[3]["CustomField"][0]["StringValue"], "PO-2026")
        self.assertEqual(update[3]["CustomField"][1]["StringValue"], "Staffing")
        self.assertTrue(first["integration"]["accounting"]["applied_at"])
        self.assertFalse(first["integration"]["accounting"]["source_changed"])
        self.assertEqual(first["invoice"]["po_reference"], "")
        self.assertEqual(first["invoice"]["payment_status"], "unpaid")
        self.assertEqual(first["invoice"]["resolution"], prepared["resolution"])
        self.assertTrue(self.workflow.apply_reference(prepared["id"], body)["duplicate"])
        self.assertEqual(sum(c[0:2] == ("qbo", "POST") for c in self.connections.calls), 1)

    def test_failed_update_preserves_approval_and_never_claims_applied(self):
        prepared = self.prepared(self.imported())
        self.connections.qbo_failure = "update"
        body = self.body(prepared)
        self.error("external_action_uncertain", self.workflow.apply_reference, prepared["id"], body)
        self.error("external_action_uncertain", self.workflow.reconcile_reference, prepared["id"])
        self.assertIsNone(self.workflow.get_detail(prepared["id"])["accounting"]["applied_at"])
        self.assertEqual(self.service.get_invoice(prepared["id"])["resolution"], prepared["resolution"])
        self.assertEqual(sum(c[0:2] == ("qbo", "POST") for c in self.connections.calls), 1)

    def test_apply_needs_exact_configured_field_and_intact_approved_document(self):
        prepared = self.prepared(self.imported())
        self.settings.qbo_po_field_id = ""
        self.error("po_field_required", self.workflow.apply_reference, prepared["id"], self.body(prepared))
        self.settings.qbo_po_field_id = "1"
        with sqlite3.connect(self.service.database_path) as connection:
            connection.execute("UPDATE documents SET content=? WHERE id=?", (b"damaged", prepared["resolution"]["document_id"]))
        self.error("evidence_unavailable", self.workflow.apply_reference, prepared["id"], self.body(prepared))
        self.assertFalse(any(c[0:2] == ("qbo", "POST") for c in self.connections.calls))

    def test_bounded_reply_scan_advances_past_checked_non_customer_messages(self):
        source = self.imported()
        self.workflow.send_request(source["id"], self.body(source))
        self.connections.threads["thread-1"].extend(self.reply("noise-" + str(i), "elsewhere@another.co") for i in range(20))
        self.connections.threads["thread-1"].append(self.reply("late-customer"))
        first = self.workflow.sync_replies(source["id"])
        self.assertTrue(first["has_more"])
        self.assertEqual(first["added"], 0)
        second = self.workflow.sync_replies(source["id"])
        self.assertEqual(second["added"], 1)

    def test_lost_update_response_is_verified_by_get(self):
        prepared = self.prepared(self.imported())
        self.connections.qbo_failure = "timeout_after_update"
        result = self.workflow.apply_reference(prepared["id"], self.body(prepared))
        self.assertTrue(result["integration"]["accounting"]["applied_at"])

    def test_provider_refresh_failure_retains_source_and_blocks_writes(self):
        source = self.imported()
        self.connections.qbo_failure = "get"
        self.error("accounting_unavailable", self.workflow.refresh_invoice, source["id"])
        state = self.workflow.get_detail(source["id"])["accounting"]
        self.assertEqual(state["balance_cents"], 10000)
        self.assertTrue(state["error"])
        self.assertEqual(self.service.get_invoice(source["id"])["amount_cents"], 10000)


if __name__ == "__main__":
    unittest.main()
