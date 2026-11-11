"""Inbox capture checks use temporary databases and mocked providers only."""
import json
import base64
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from invoiceclear.capture import InboxCapture
from invoiceclear.connected import ConnectedWorkflow
from invoiceclear.service import DomainError, Service
import test_connected as connected_fixtures
from test_connected import FakeConnections, invoice


class CaptureConnections(FakeConnections):
    def gmail(self, method, path, body=None, expected_account=None):
        result = super().gmail(method, path, body, expected_account)
        if method == "POST":
            message = self.sent[result["id"]]
            self.threads["thread-1"].remove(message)
            result["threadId"] = message["threadId"] = "request-" + result["id"]
            self.threads[result["threadId"]] = [message]
        return result


class CaptureTests(unittest.TestCase):
    body = connected_fixtures.ConnectedTests.body
    action = connected_fixtures.ConnectedTests.action
    ready = connected_fixtures.ConnectedTests.ready
    prepared = connected_fixtures.ConnectedTests.prepared
    reply = connected_fixtures.ConnectedTests.reply

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="invoiceclear-capture-")
        self.service = Service(Path(self.temp.name) / "private.sqlite", demo=False)
        self.connections = CaptureConnections()
        self.settings = SimpleNamespace(mode="private", qbo_po_field_id="1")
        self.workflow = ConnectedWorkflow(self.service, self.connections, self.settings)
        self.capture = InboxCapture(self.service, self.workflow, self.settings, "workspace-fingerprint")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.capture.close)

    def cases(self, count=1):
        self.connections.invoices = {str(101 + n): invoice(str(101 + n)) for n in range(count)}
        self.workflow.sync_invoices()
        sources = self.service.list_invoices(view="all", limit=100)["invoices"]
        for source in sources:
            sent = self.workflow.send_request(source["id"], self.body(source))
            source["thread"] = sent["integration"]["messages"][0]["thread_id"]
        return sources

    def receive(self, source, *, attachment=False, name=None):
        message = self.reply(name or "reply-" + source["id"], attachment=attachment, thread=source["thread"])
        self.connections.threads[source["thread"]].append(message)

    def enable(self):
        self.capture.set_enabled(True)
        self.connections.calls.clear()

    def test_default_is_disabled_and_status_never_contacts_providers(self):
        self.cases()
        self.connections.calls.clear()
        self.assertFalse(self.capture.status()["enabled"])
        self.assertTrue(self.capture.status()["available"])
        self.capture.run_once()
        self.assertEqual(self.connections.calls, [])
        for value in (1, "true", None, {}):
            with self.assertRaises(DomainError):
                self.capture.set_enabled(value)

    def test_captures_without_opening_invoice_and_preserves_review_payment_boundaries(self):
        source = self.cases()[0]
        self.receive(source, attachment=True)
        self.enable()
        status = self.capture.run_once()
        self.assertEqual((status["checked"], status["replies_added"], status["documents_added"]), (1, 1, 1))
        detail = self.service.get_invoice(source["id"])
        document = detail["documents"][0]
        self.assertFalse(document["reviewed"])
        self.assertEqual(document["fields"], {})
        self.assertEqual(document["review_evidence"], {})
        self.assertIsNone(detail["resolution"])
        self.assertEqual((detail["amount_cents"], detail["payment_status"]), (10000, "unpaid"))
        self.assertTrue(all(call[:2] == ("gmail", "GET") for call in self.connections.calls))
        self.assertTrue(all(call[2].startswith(("threads/", "messages/")) for call in self.connections.calls))
        self.assertFalse(any(call[2].startswith("messages?") for call in self.connections.calls))

    def test_restart_preserves_opt_in_and_duplicate_prevention(self):
        source = self.cases()[0]
        self.receive(source, attachment=True)
        self.enable()
        self.capture.run_once()
        self.capture.close()
        resumed = InboxCapture(self.service, self.workflow, self.settings, "workspace-fingerprint")
        self.addCleanup(resumed.close)
        self.assertTrue(resumed.status()["enabled"])
        status = resumed.run_once()
        self.assertEqual((status["replies_added"], status["documents_added"]), (0, 0))
        detail = self.service.get_invoice(source["id"])
        self.assertEqual((len(detail["replies"]), len(detail["documents"])), (1, 1))

    def test_fair_rotation_is_bounded_and_cursor_survives_restart(self):
        cases = self.cases(15)
        for source in cases:
            self.receive(source)
        self.enable()
        first = self.capture.run_once()
        self.assertEqual((first["checked"], first["replies_added"]), (10, 10))
        resumed = InboxCapture(self.service, self.workflow, self.settings, "workspace-fingerprint")
        self.addCleanup(resumed.close)
        second = resumed.run_once()
        self.assertEqual((second["checked"], second["replies_added"]), (10, 5))
        self.assertTrue(all(len(self.service.get_invoice(source["id"])["replies"]) == 1 for source in cases))

    def test_only_open_linked_confirmed_request_cases_are_selected(self):
        cases = self.cases(3)
        for source in cases:
            self.receive(source)
        prepared = self.prepared(self.service.get_invoice(cases[0]["id"]))
        with self.service._write() as connection:
            connection.execute("UPDATE external_actions SET state='unknown' WHERE invoice_id=?", (cases[1]["id"],))
        self.enable()
        status = self.capture.run_once()
        self.assertEqual((status["checked"], status["replies_added"]), (1, 1))
        self.assertEqual(self.service.get_invoice(prepared["id"])["replies"], [])
        self.assertEqual(self.service.get_invoice(cases[1]["id"])["replies"], [])
        # Explicitly reopening the prepared case makes it eligible on the next pass.
        self.action(prepared, "reopen", {"note": "Review the newly received customer evidence."})
        self.assertEqual(self.capture.run_once()["replies_added"], 1)

    def test_pending_apply_hold_skips_case_then_resumes_after_reconciliation(self):
        source = self.cases()[0]
        self.receive(source)
        with self.service._write() as connection:
            connection.execute("INSERT INTO external_actions SELECT 'hold',invoice_id,'apply','hold','held','unknown',company_id,environment,mailbox,?, '{}',NULL,recorded_at,updated_at FROM external_actions WHERE invoice_id=?", (json.dumps({"reference": "PO-TEST"}), source["id"]))
        self.enable()
        status = self.capture.run_once()
        self.assertEqual((status["checked"], status["pending_cases"]), (0, 1))
        self.assertEqual(self.connections.calls, [])
        with self.service._write() as connection:
            connection.execute("UPDATE external_actions SET state='failed' WHERE id='hold'")
        self.assertEqual(self.capture.run_once()["replies_added"], 1)

    def test_pending_race_is_rechecked_in_reply_write_transaction(self):
        source = self.cases()[0]
        self.receive(source)
        original = self.workflow._record_message
        def race(*args, **kwargs):
            with self.service._write() as connection:
                connection.execute("UPDATE external_actions SET state='unknown' WHERE invoice_id=?", (source["id"],))
            return original(*args, **kwargs)
        self.enable()
        with patch.object(self.workflow, "_record_message", side_effect=race):
            status = self.capture.run_once()
        self.assertEqual(status["replies_added"], 0)
        self.assertTrue(status["failures"])
        self.assertEqual(self.service.get_invoice(source["id"])["replies"], [])
        with self.service._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM external_messages").fetchone()[0], 0)

    def test_prepared_race_is_rechecked_before_any_background_reply_write(self):
        source = self.cases()[0]
        self.receive(source)
        ready = self.ready(self.service.get_invoice(source["id"]))
        original = self.workflow._record_message
        def race(*args, **kwargs):
            self.action(ready, "approve_resolution")
            return original(*args, **kwargs)
        self.enable()
        with patch.object(self.workflow, "_record_message", side_effect=race):
            status = self.capture.run_once()
        self.assertEqual(status["replies_added"], 0)
        self.assertEqual(self.service.get_invoice(source["id"])["replies"], [])

    def test_disconnect_account_fingerprint_and_extractor_changes_require_new_opt_in(self):
        self.cases()
        self.enable()
        self.connections.accounts["gmail"]["connected"] = False
        self.assertFalse(self.capture.status()["enabled"])
        self.assertFalse(self.capture.status()["available"])
        self.connections.accounts["gmail"]["connected"] = True
        self.assertFalse(self.capture.run_once()["enabled"])
        self.assertEqual(self.connections.calls, [])
        self.capture.set_enabled(True)
        self.connections.accounts["gmail"]["account_id"] = "different@staffco.co"
        self.assertFalse(self.capture.run_once()["enabled"])
        self.connections.accounts["gmail"]["account_id"] = "finance@staffco.co"
        self.capture.set_enabled(True)
        changed = InboxCapture(self.service, self.workflow, self.settings, "new-fingerprint")
        self.addCleanup(changed.close)
        self.assertFalse(changed.status()["enabled"])
        self.capture.set_enabled(True)
        self.service.provider = "together"
        self.assertFalse(self.capture.run_once()["enabled"])

    def test_account_change_at_dispatch_cannot_read_new_mailbox_without_new_opt_in(self):
        source = self.cases()[0]
        self.receive(source)
        self.enable()
        original = self.workflow.sync_replies
        def changed(*args, **kwargs):
            self.connections.accounts["gmail"]["account_id"] = "different@staffco.co"
            return original(*args, **kwargs)
        with patch.object(self.workflow, "sync_replies", side_effect=changed):
            status = self.capture.run_once()
        self.assertFalse(status["enabled"])
        self.assertEqual(self.connections.calls, [])
        self.assertEqual(self.service.get_invoice(source["id"])["replies"], [])

    def test_case_provider_failure_is_bounded_sanitized_and_does_not_starve_others(self):
        cases = self.cases(2)
        for source in cases:
            self.receive(source)
        bad_thread = cases[0]["thread"]
        original = self.connections.gmail
        def fail(method, path, *args, **kwargs):
            if path.startswith("threads/" + bad_thread + "?"):
                raise TimeoutError("raw customer body and secret should never appear")
            return original(method, path, *args, **kwargs)
        self.enable()
        with patch.object(self.connections, "gmail", side_effect=fail):
            status = self.capture.run_once()
        self.assertEqual((status["checked"], status["replies_added"]), (2, 1))
        self.assertEqual(len(status["failures"]), 1)
        self.assertNotIn("raw customer", json.dumps(status))
        self.assertEqual(self.capture.run_once()["replies_added"], 1)

    def test_attachment_retry_rotation_survives_restart_and_retains_failures(self):
        source = self.cases()[0]
        failed = self.reply("failed-files", thread=source["thread"])
        failed["payload"]["parts"].extend({"partId": str(index + 1), "mimeType": "application/pdf", "filename": f"oversized-{index}.pdf", "body": {"size": 5 * 1024 * 1024 + 1, "attachmentId": "oversized"}} for index in range(10))
        self.connections.threads[source["thread"]].extend([failed, self.reply("later-valid", attachment=True, thread=source["thread"])])
        self.enable()
        first = self.capture.run_once()
        self.assertEqual((first["replies_added"], first["documents_added"]), (2, 0))
        resumed = InboxCapture(self.service, self.workflow, self.settings, "workspace-fingerprint")
        self.addCleanup(resumed.close)
        second = resumed.run_once()
        self.assertEqual(second["documents_added"], 1)
        messages = self.workflow.get_detail(source["id"])["messages"]
        held = next(item for item in messages if item.get("message_id") == "failed-files")
        self.assertEqual(len(held["attachments"]), 10)
        self.assertTrue(all(item["error"] and not item["document_id"] for item in held["attachments"]))
        self.assertEqual(resumed.run_once()["documents_added"], 0)
        detail = self.service.get_invoice(source["id"])
        self.assertEqual(len(detail["documents"]), 1)
        self.assertFalse(detail["documents"][0]["reviewed"])

    def test_malformed_messages_rotate_remain_inspectable_and_retry_safely(self):
        source = self.cases()[0]
        broken = []
        for index in range(21):
            message = self.reply(f"broken-{index}", thread=source["thread"])
            message["payload"]["parts"][0]["body"]["data"] = "!invalid-base64!"
            broken.append(message)
        self.connections.threads[source["thread"]].extend([*broken, self.reply("later-valid", attachment=True, thread=source["thread"])])
        self.enable()
        self.assertEqual(self.capture.run_once()["replies_added"], 0)
        resumed = InboxCapture(self.service, self.workflow, self.settings, "workspace-fingerprint")
        self.addCleanup(resumed.close)
        second = resumed.run_once()
        self.assertEqual((second["replies_added"], second["documents_added"]), (1, 1))
        holds = [item for item in self.workflow.get_detail(source["id"])["messages"] if item["status"] == "held"]
        self.assertEqual(len(holds), 21)
        self.assertTrue(all(item["error"] and item["mailbox"] == "finance@staffco.co" and item["thread_id"] == source["thread"] for item in holds))
        self.assertNotIn("invalid-base64", json.dumps(holds))
        broken[0]["payload"]["parts"][0]["body"]["data"] = base64.urlsafe_b64encode(b"The corrected source message is readable.").decode()
        # A complete rotation retries the held source and replaces the hold once.
        results = [resumed.run_once(), resumed.run_once()]
        self.assertEqual(sum(item["replies_added"] for item in results), 1)
        detail = self.workflow.get_detail(source["id"])
        repaired = [item for item in detail["messages"] if item.get("message_id") == "broken-0"]
        self.assertEqual(len(repaired), 1)
        self.assertEqual(repaired[0]["status"], "received")
        self.assertNotIn("error", repaired[0])
        self.assertEqual(len(self.service.get_invoice(source["id"])["replies"]), 2)

    def test_new_authorization_for_same_account_requires_new_capture_opt_in(self):
        self.cases()
        self.connections.accounts["gmail"]["grant_id"] = "original-authorization"
        self.enable()
        self.connections.accounts["gmail"]["grant_id"] = "renewed-authorization"
        self.assertFalse(self.capture.run_once()["enabled"])
        self.assertEqual(self.connections.calls, [])
        self.capture.set_enabled(True)
        self.assertTrue(self.capture.status()["enabled"])

    def test_thread_rotation_survives_restart_without_fetching_past_message_budget(self):
        source = self.cases()[0]
        for index in range(21):
            message = self.reply(f"broken-{index}", thread=source["thread"])
            message["payload"]["parts"][0]["body"]["data"] = "!invalid-base64!"
            self.connections.threads[source["thread"]].append(message)
        other_thread = "later-request-thread"
        self.connections.threads[other_thread] = [self.reply("later-valid", attachment=True, thread=other_thread)]
        with self.service._write() as connection:
            connection.execute("INSERT INTO external_actions SELECT 'other-request',invoice_id,kind,'other-request',fingerprint,state,company_id,environment,mailbox,payload,?,error,'2020-01-01T00:00:00Z',updated_at FROM external_actions WHERE invoice_id=?", (json.dumps({"id": "other-request", "threadId": other_thread}), source["id"]))
        self.enable()
        first = self.capture.run_once()
        self.assertEqual(first["replies_added"], 0)
        self.assertEqual([call[2] for call in self.connections.calls if call[2].startswith("threads/")], ["threads/" + source["thread"] + "?format=full"])
        self.connections.calls.clear()
        resumed_workflow = ConnectedWorkflow(self.service, self.connections, self.settings)
        resumed = InboxCapture(self.service, resumed_workflow, self.settings, "workspace-fingerprint")
        self.addCleanup(resumed.close)
        second = resumed.run_once()
        self.assertEqual((second["replies_added"], second["documents_added"]), (1, 1))
        self.assertEqual(self.connections.calls[0][2], "threads/" + other_thread + "?format=full")
        self.assertEqual(len(self.service.get_invoice(source["id"])["documents"]), 1)

    def test_held_reply_projects_into_detail_and_one_page_bounded_worklist_query(self):
        source, other = self.cases(2)
        message = self.reply("held-reply", thread=source["thread"])
        message["payload"]["parts"][0]["body"]["data"] = "!invalid-base64!"
        self.connections.threads[source["thread"]].append(message)
        self.workflow.sync_replies(source["id"])
        detail = self.service.get_invoice(source["id"])
        detail["integration"] = self.workflow.get_detail(source["id"])
        expected = {"kind": "replies", "label": "Message needs review"}
        self.assertEqual(self.workflow.work_summary(detail), expected)
        self.assertEqual(detail["replies"], [])
        statements = []
        original = self.service._connection
        @contextmanager
        def traced():
            with original() as connection:
                connection.set_trace_callback(statements.append)
                yield connection
        self.connections.calls.clear()
        with patch.object(self.service, "_connection", side_effect=traced):
            projected = self.workflow.worklist_context([source])
        self.assertEqual(projected, {source["id"]: expected})
        intake_queries = [query for query in statements if "FROM external_messages" in query]
        self.assertEqual(len(intake_queries), 1)
        self.assertIn(source["id"], intake_queries[0])
        self.assertNotIn(other["id"], intake_queries[0])
        self.assertNotRegex(intake_queries[0], r"\b(?:text|attachments)\b")
        self.assertEqual(self.connections.calls, [])
        message["payload"]["parts"][0]["body"]["data"] = base64.urlsafe_b64encode(b"Readable customer reply.").decode()
        self.workflow.sync_replies(source["id"])
        self.assertEqual(self.workflow.worklist_context([source])[source["id"]]["label"], "Review customer reply")

    def test_same_database_claim_prevents_overlapping_workers(self):
        self.cases()
        self.enable()
        another = InboxCapture(self.service, self.workflow, self.settings, "workspace-fingerprint")
        self.addCleanup(another.close)
        entered, release = threading.Event(), threading.Event()
        original = self.workflow.sync_replies
        def wait(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(*args, **kwargs)
        with patch.object(self.workflow, "sync_replies", side_effect=wait) as sync:
            thread = threading.Thread(target=self.capture.run_once)
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertTrue(self.capture.status()["running"])
                another.run_once()
                self.assertEqual(sync.call_count, 1)
            finally:
                release.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(self.capture.status()["running"])

    def test_public_mode_never_starts_enables_or_inspects_accounts(self):
        self.cases()
        self.enable()
        public = InboxCapture(self.service, self.workflow, SimpleNamespace(mode="demo"), "workspace-fingerprint")
        with patch.object(self.connections, "status", side_effect=AssertionError("public account read")):
            public.start()
            self.assertIsNone(public._thread)
            self.assertEqual(public.run_once(), public.status())
            self.assertEqual(public.status(), public._empty())
            with self.assertRaises(DomainError) as raised:
                public.set_enabled(True)
            self.assertEqual(raised.exception.status, 403)
        public.close()

    def test_one_background_thread_stops_cleanly_without_polling_when_disabled(self):
        called = threading.Event()
        with patch.object(self.capture, "run_once", side_effect=lambda: called.set()):
            self.capture.start()
            first = self.capture._thread
            self.capture.start()
            self.assertIs(first, self.capture._thread)
            self.assertTrue(called.wait(3))
            self.capture.close()
        self.assertFalse(first.is_alive())


if __name__ == "__main__":
    unittest.main()
