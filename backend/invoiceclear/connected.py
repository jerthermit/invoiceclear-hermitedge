"""Bounded, approval-bound accounting and mailbox work with durable receipts."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime, getaddresses
from urllib.parse import quote, urlencode

from .service import DomainError, decimal_amount, packed, stamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS external_invoices (
 invoice_id TEXT PRIMARY KEY REFERENCES invoices(id), company_id TEXT NOT NULL,
 environment TEXT NOT NULL, external_id TEXT NOT NULL, original_source TEXT NOT NULL,
 remote_invoice TEXT NOT NULL, accounting TEXT NOT NULL, applied_reference TEXT,
 applied_at TEXT, UNIQUE(company_id,environment,external_id));
CREATE TABLE IF NOT EXISTS external_actions (
 id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL REFERENCES invoices(id), kind TEXT NOT NULL,
 key TEXT NOT NULL, fingerprint TEXT NOT NULL, state TEXT NOT NULL,
 company_id TEXT NOT NULL, environment TEXT NOT NULL, mailbox TEXT NOT NULL,
 payload TEXT NOT NULL, receipt TEXT NOT NULL DEFAULT '{}', error TEXT,
 recorded_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(invoice_id,key));
CREATE TABLE IF NOT EXISTS external_messages (
 mailbox TEXT NOT NULL, provider_id TEXT NOT NULL, invoice_id TEXT NOT NULL REFERENCES invoices(id),
 thread_id TEXT NOT NULL, reply_id TEXT REFERENCES replies(id),
 metadata TEXT NOT NULL, attachments TEXT NOT NULL, PRIMARY KEY(mailbox,provider_id));
CREATE TABLE IF NOT EXISTS external_intake_progress (
 invoice_id TEXT NOT NULL REFERENCES invoices(id), mailbox TEXT NOT NULL,
 scope TEXT NOT NULL, cursor TEXT NOT NULL,
 PRIMARY KEY(invoice_id,mailbox,scope));
"""
PAGE_SIZE = 20
MAX_ATTACHMENT = 5 * 1024 * 1024


def identifier(value, maximum=100):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1," + str(maximum) + r"}", value):
        raise DomainError("provider_data_invalid", "The provider returned an invalid record identity.", 502)
    return value


def cents(value, *, zero=False):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount > Decimal("999999999.99") or amount * 100 != (amount * 100).to_integral_value() or (not zero and not amount):
            raise ValueError()
        return int(amount * 100)
    except (InvalidOperation, ValueError, TypeError):
        raise DomainError("provider_data_invalid", "The accounting record has an invalid amount.", 502) from None


def decoded(value):
    if not isinstance(value, str) or len(value) > ((MAX_ATTACHMENT + 2) // 3) * 4:
        raise DomainError("document_too_large", "Mailbox attachments must be at most 5 MB.", 413)
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        raise DomainError("provider_data_invalid", "The mailbox returned invalid attachment content.", 502) from None


class ConnectedWorkflow:
    def __init__(self, service, connections, settings):
        self.service, self.connections, self.settings = service, connections, settings
        with service._connection() as connection:
            connection.executescript(SCHEMA)

    def _account(self, provider):
        if self.settings.mode == "demo" or self.service.demo:
            raise DomainError("demo_external_disabled", "Use a private workspace for connected actions.", 403)
        account = self.connections.status().get(provider, {})
        if not account.get("connected") or not account.get("account_id"):
            raise DomainError("connection_required", "Connect the accounting company and mailbox required for this action.", 409)
        return account

    def _mapping(self, connection, invoice_id, account):
        self.service._invoice(connection, invoice_id)
        row = connection.execute("SELECT * FROM external_invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
        if not row:
            raise DomainError("source_not_connected", "Import this invoice from QuickBooks before using connected actions.", 409)
        if row["company_id"] != str(account["account_id"]) or row["environment"] != account.get("environment"):
            raise DomainError("account_changed", "Reconnect the original QuickBooks company and environment for this invoice.", 409)
        return row

    def _source(self, raw, account, home_currency=None):
        if not isinstance(raw, dict):
            raise DomainError("provider_data_invalid", "QuickBooks did not return an invoice.", 502)
        external_id = identifier(raw.get("Id"))
        customer_ref = raw.get("CustomerRef") or {}
        customer_id = identifier(customer_ref.get("value"))
        email = (raw.get("BillEmail") or {}).get("Address")
        if not email:
            raise DomainError("missing_bill_email", "Set the invoice's billing email in QuickBooks before importing it.", 409)
        customer = customer_ref.get("name")
        if not customer:
            customer = (self.connections.qbo("GET", "customer/" + customer_id, expected_account=str(account["account_id"])).get("Customer") or {}).get("DisplayName")
        currency = (raw.get("CurrencyRef") or {}).get("value") or home_currency
        if not currency:
            preferences = self.connections.qbo("GET", "preferences", expected_account=str(account["account_id"])).get("Preferences") or {}
            currency = (preferences.get("CurrencyPrefs", {}).get("HomeCurrency") or {}).get("value")
        field_id = str(getattr(self.settings, "qbo_po_field_id", "") or "")
        fields = [f for f in raw.get("CustomField", []) if str(f.get("DefinitionId")) == field_id] if field_id else []
        if field_id and not fields:
            raise DomainError("po_field_unavailable", "The configured customer PO custom field is not exposed on this invoice. Check the QuickBooks field setup.", 409)
        if len(fields) > 1 or any(f.get("Type") != "StringType" for f in fields):
            raise DomainError("po_field_invalid", "The configured QuickBooks PO field must be one string custom field.", 409)
        values = self.service._entry({"invoice_number": raw.get("DocNumber"), "customer": customer,
            "customer_email": email, "amount": decimal_amount(cents(raw.get("TotalAmt"))),
            "currency": currency, "issue_date": raw.get("TxnDate"), "due_date": raw.get("DueDate"),
            "po_reference": fields[0].get("StringValue", "") if fields else "",
            "source_ref": f"qbo:{account.get('environment')}:{account['account_id']}:invoice:{external_id}"})
        return values, {**values, "customer_id": customer_id, "external_id": external_id, "po_field_id": field_id}

    def _remote_state(self, raw, source):
        balance = cents(raw.get("Balance"), zero=True)
        if balance > source["amount_cents"]:
            raise DomainError("provider_data_invalid", "The QuickBooks balance exceeds the invoice total.", 502)
        return {"balance_cents": balance, "payment_status": "paid" if not balance else "partial" if balance < source["amount_cents"] else "unpaid",
                "po_reference": source["po_reference"], "synced_at": stamp(), "source_changed": False, "error": None}

    def _save_refresh(self, connection, row, raw, source):
        baseline = json.loads(row["original_source"])
        if row["applied_reference"] is not None:
            baseline["po_reference"] = row["applied_reference"]
        state = self._remote_state(raw, source)
        state["source_changed"] = source != baseline
        connection.execute("UPDATE external_invoices SET remote_invoice=?,accounting=? WHERE invoice_id=?", (packed(raw), packed(state), row["invoice_id"]))
        return state

    def sync_invoices(self, offset=0):
        if type(offset) is not int or offset < 0 or offset > 1_000_000:
            raise DomainError("invalid_pagination", "Use a nonnegative accounting page offset.")
        account = self._account("qbo")
        query = f"SELECT * FROM Invoice WHERE Balance > '0' STARTPOSITION {offset + 1} MAXRESULTS {PAGE_SIZE}"
        if not self.settings.qbo_po_field_id:
            raise DomainError("po_field_required", "Configure the customer PO custom-field DefinitionId before syncing invoices.", 409)
        response = self.connections.qbo("GET", "query?" + urlencode({"query": query}), expected_account=str(account["account_id"]))
        records = (response.get("QueryResponse") or {}).get("Invoice", [])
        if not isinstance(records, list) or len(records) > PAGE_SIZE:
            raise DomainError("provider_data_invalid", "QuickBooks returned an invalid invoice page.", 502)
        created = refreshed = 0
        skipped, home_currency = [], None
        if any(not (raw.get("CurrencyRef") or {}).get("value") for raw in records):
            prefs = self.connections.qbo("GET", "preferences", expected_account=str(account["account_id"])).get("Preferences") or {}
            home_currency = (prefs.get("CurrencyPrefs", {}).get("HomeCurrency") or {}).get("value")
            if not home_currency:
                raise DomainError("currency_unavailable", "QuickBooks did not provide its home currency.", 409)
        for raw in records:
            try:
                values, source = self._source(raw, account, home_currency)
                state = self._remote_state(raw, source)
                if state["payment_status"] == "paid":
                    raise DomainError("invoice_paid", "This invoice no longer has an unpaid balance.", 409)
                current = self._account("qbo")
                with self.service._write() as connection:
                    if str(current["account_id"]) != str(account["account_id"]) or current.get("environment") != account.get("environment"):
                        raise DomainError("account_changed", "The connected QuickBooks company changed during import. Retry the current company.", 409)
                    row = connection.execute("SELECT * FROM external_invoices WHERE company_id=? AND environment=? AND external_id=?", (str(account["account_id"]), account["environment"], source["external_id"])).fetchone()
                    if row:
                        self._save_refresh(connection, row, raw, source)
                        refreshed += 1
                    else:
                        if connection.execute("SELECT 1 FROM invoices WHERE invoice_number=? AND customer_norm=?", (values["invoice_number"], values["customer_norm"])).fetchone():
                            raise DomainError("local_identity_exists", "An existing invoice has this number and customer; its source was preserved.", 409)
                        invoice_id = self.service._insert(connection, values, "QuickBooks import", provenance={"mode": "private", "intake": "qbo", "provider": "qbo", "company_id": str(account["account_id"]), "environment": account["environment"], "external_id": source["external_id"], "imported_at": stamp()})
                        connection.execute("INSERT INTO external_invoices(invoice_id,company_id,environment,external_id,original_source,remote_invoice,accounting) VALUES(?,?,?,?,?,?,?)", (invoice_id, str(account["account_id"]), account["environment"], source["external_id"], packed(source), packed(raw), packed(state)))
                        created += 1
            except DomainError as error:
                skipped.append({"external_id": str(raw.get("Id", ""))[:100], "code": error.code, "reason": error.message})
        return {"created": created, "refreshed": refreshed, "skipped": skipped, "has_more": len(records) == PAGE_SIZE, "next_offset": offset + len(records)}

    @staticmethod
    def work_summary(invoice, integration=None, *, replied=False):
        integration = integration if integration is not None else (invoice.get("integration") or {})
        accounting, messages = integration.get("accounting"), integration.get("messages", [])
        if any(message.get("direction") == "outgoing" and message.get("status") in {"pending", "unknown"} for message in messages):
            return {"kind": "verify-send", "label": "Verify Gmail receipt"}
        if accounting:
            if accounting.get("action", {}).get("state") in {"pending", "unknown"}:
                return {"kind": "verify-update", "label": "Verify QuickBooks receipt"}
            if accounting.get("connected") is False or accounting.get("error") or accounting.get("source_changed") or accounting.get("payment_status") != "unpaid":
                return {"kind": "refresh", "label": "Review QuickBooks source"}
        if invoice["status"] == "prepared":
            return {"kind": "handoff", "label": "Open handoff"}
        if invoice["status"] == "missing_po":
            if any(message.get("direction") == "incoming" and message.get("status") == "held" for message in messages):
                return {"kind": "replies", "label": "Message needs review"}
            if replied or invoice.get("replies") or any(message.get("direction") == "incoming" for message in messages):
                return {"kind": "replies", "label": "Review customer reply"}
            if any(message.get("direction") == "outgoing" and message.get("status") == "sent" for message in messages):
                return {"kind": "replies", "label": "Awaiting purchase order"}
            return {"kind": "request", "label": "Request purchase order"}
        if invoice["status"] == "needs_review":
            return {"kind": "review", "label": "Review purchase order"}
        if invoice["status"] == "ready":
            return {"kind": "approve", "label": "Approve handoff"}
        return {"kind": "review", "label": invoice["blocker"]}

    def worklist_context(self, invoices):
        """Project stored follow-up facts for one page; never contact providers."""
        if not invoices:
            return {}
        ids = [invoice["id"] for invoice in invoices]
        slots = ",".join("?" for _ in ids)
        integrations = {invoice_id: {"accounting": None, "messages": []} for invoice_id in ids}
        with self.service._connection() as connection:
            connection.execute("BEGIN")
            linked = connection.execute(f"SELECT invoice_id,company_id,environment,accounting FROM external_invoices WHERE invoice_id IN ({slots})", ids).fetchall()
            account = self.connections.status().get("qbo", {}) if linked else {}
            for row in linked:
                accounting = json.loads(row["accounting"])
                integrations[row["invoice_id"]]["accounting"] = {**accounting, "connected": bool(account.get("connected") and str(account.get("account_id")) == row["company_id"] and account.get("environment") == row["environment"])}
            for row in connection.execute(f"SELECT invoice_id,kind,state FROM external_actions WHERE invoice_id IN ({slots}) ORDER BY recorded_at,id", ids):
                integration = integrations[row["invoice_id"]]
                if row["kind"] == "send":
                    integration["messages"].append({"direction": "outgoing", "status": "sent" if row["state"] == "confirmed" else row["state"]})
                elif integration["accounting"] is not None:
                    integration["accounting"]["action"] = {"state": row["state"]}
            replied = {row[0] for row in connection.execute(f"SELECT DISTINCT invoice_id FROM replies WHERE invoice_id IN ({slots})", ids)}
            for row in connection.execute(f"SELECT DISTINCT invoice_id FROM external_messages WHERE invoice_id IN ({slots}) AND reply_id IS NULL AND json_extract(metadata,'$.status')='held'", ids):
                integrations[row[0]]["messages"].append({"direction": "incoming", "status": "held"})
        return {invoice["id"]: self.work_summary(invoice, integrations[invoice["id"]], replied=invoice["id"] in replied) for invoice in invoices}

    def get_detail(self, invoice_id):
        with self.service._connection() as connection:
            self.service._invoice(connection, invoice_id)
            row = connection.execute("SELECT * FROM external_invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
            accounting = None
            if row:
                account = self.connections.status().get("qbo", {})
                connected = bool(account.get("connected") and str(account.get("account_id")) == row["company_id"] and account.get("environment") == row["environment"])
                accounting = {**json.loads(row["accounting"]), "provider": "qbo", "environment": row["environment"], "account_id": row["company_id"], "external_id": row["external_id"], "connected": connected, "applied_at": row["applied_at"], "po_field_configured": bool(getattr(self.settings, "qbo_po_field_id", ""))}
                if not connected:
                    accounting["error"] = "Reconnect this invoice's original QuickBooks company and environment."
            messages = []
            for action in connection.execute("SELECT * FROM external_actions WHERE invoice_id=? ORDER BY recorded_at,id", (invoice_id,)):
                payload, receipt = json.loads(action["payload"]), json.loads(action["receipt"])
                if action["kind"] == "send":
                    messages.append({"direction": "outgoing", "status": "sent" if action["state"] == "confirmed" else action["state"], "state": action["state"], "to": payload["to"], "subject": payload["subject"], "text": payload["body"], "body": payload["body"], "sent_at": action["updated_at"] if action["state"] == "confirmed" else None, "recorded_at": action["updated_at"], "message_id": receipt.get("id"), "thread_id": receipt.get("threadId"), "threadId": receipt.get("threadId"), "rfc_message_id": payload["message_id"], "error": action["error"], "mailbox": action["mailbox"]})
                elif accounting is not None:
                    accounting["action"] = {"state": action["state"], "reference": payload["reference"], "error": action["error"], "recorded_at": action["updated_at"]}
            for row in connection.execute("SELECT m.*,r.text,r.received_at FROM external_messages m LEFT JOIN replies r ON r.id=m.reply_id WHERE m.invoice_id=? ORDER BY m.rowid", (invoice_id,)):
                metadata = json.loads(row["metadata"])
                if not row["reply_id"] and metadata.get("status") != "held":
                    continue
                status = "received" if row["reply_id"] else "held"
                messages.append({**metadata, "direction": "incoming", "status": status, "state": status, "message_id": row["provider_id"], "thread_id": row["thread_id"], "threadId": row["thread_id"], "text": row["text"], "body": row["text"], "received_at": row["received_at"], "recorded_at": row["received_at"] or metadata.get("recorded_at"), "attachments": [{k: v for k, v in a.items() if k != "data"} for a in json.loads(row["attachments"])], "mailbox": row["mailbox"], "sender_authenticated": False})
            return {"accounting": accounting, "messages": messages}

    def refresh_invoice(self, invoice_id):
        account = self._account("qbo")
        with self.service._connection() as connection:
            row = self._mapping(connection, invoice_id, account)
        try:
            raw = self.connections.qbo("GET", "invoice/" + identifier(row["external_id"]), expected_account=row["company_id"]).get("Invoice")
            _, source = self._source(raw, account)
            if source["external_id"] != row["external_id"]:
                raise DomainError("provider_data_invalid", "QuickBooks returned a different invoice.", 502)
            current = self._account("qbo")
            with self.service._write() as connection:
                self._save_refresh(connection, self._mapping(connection, invoice_id, current), raw, source)
        except Exception as error:
            with self.service._write() as connection:
                state = json.loads(connection.execute("SELECT accounting FROM external_invoices WHERE invoice_id=?", (invoice_id,)).fetchone()[0])
                state["error"] = "QuickBooks refresh failed. Retry before sending or applying a reference."
                connection.execute("UPDATE external_invoices SET accounting=? WHERE invoice_id=?", (packed(state), invoice_id))
            if isinstance(error, DomainError):
                raise
            raise DomainError("accounting_unavailable", state["error"], 502) from None
        return self.get_detail(invoice_id)["accounting"]

    def _request_identity(self, invoice_id, body, kind):
        if not isinstance(body, dict):
            raise DomainError("invalid_input", "Provide an action object.")
        key = body.get("idempotency_key")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", key):
            raise DomainError("invalid_idempotency_key", "Provide a valid idempotency key.")
        if type(body.get("expected_version")) is not int or body["expected_version"] < 1:
            raise DomainError("invalid_version", "Provide the current integer invoice version.")
        if not isinstance(body.get("parameters", {}), dict):
            raise DomainError("invalid_parameters", "Action parameters must be an object.")
        fingerprint = hashlib.sha256(packed({"kind": kind, "body": body}).encode()).hexdigest()
        with self.service._connection() as connection:
            prior = connection.execute("SELECT * FROM external_actions WHERE invoice_id=? AND key=?", (invoice_id, key)).fetchone()
        if prior and prior["fingerprint"] != fingerprint:
            raise DomainError("idempotency_conflict", "This key belongs to a different connected action.", 409)
        return fingerprint, prior

    def _result(self, invoice_id, duplicate=False):
        return {"invoice": self.service.get_invoice(invoice_id), "integration": self.get_detail(invoice_id), "duplicate": duplicate}

    def _clear(self, state):
        if state.get("error") or state.get("source_changed"):
            raise DomainError("accounting_stale", "The accounting source changed or could not be verified. Review it in QuickBooks before continuing.", 409)
        if state["payment_status"] != "unpaid":
            raise DomainError("payment_changed", "QuickBooks shows a payment. Review the remaining balance before continuing.", 409)

    def _reserve(self, invoice_id, body, kind, fingerprint, payload, mailbox=""):
        account = self._account("qbo")
        with self.service._write() as connection:
            row = self._mapping(connection, invoice_id, account)
            prior = connection.execute("SELECT * FROM external_actions WHERE invoice_id=? AND key=?", (invoice_id, body["idempotency_key"])).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise DomainError("idempotency_conflict", "This key belongs to a different connected action.", 409)
                if prior["state"] == "confirmed":
                    return None
                raise DomainError("external_action_uncertain", "This confirmation is awaiting its provider receipt. Verify it before retrying.", 409)
            invoice = self.service._invoice(connection, invoice_id)
            if invoice["version"] != body["expected_version"]:
                raise DomainError("stale_version", "This invoice changed. Reload before confirming the action.", 409)
            self._clear(json.loads(row["accounting"]))
            if connection.execute("SELECT 1 FROM external_actions WHERE invoice_id=? AND state IN ('pending','unknown')", (invoice_id,)).fetchone():
                raise DomainError("external_action_uncertain", "An earlier connected action needs reconciliation. Retry its original confirmation; do not send another request.", 409)
            if kind == "send":
                sent = connection.execute("SELECT payload FROM external_actions WHERE invoice_id=? AND kind='send' AND state='confirmed' AND mailbox=?", (invoice_id, mailbox)).fetchall()
                if any(all(json.loads(action["payload"])[field] == payload[field] for field in ("to", "subject", "body")) for action in sent):
                    return None
            if kind == "apply":
                detail = self.service._detail(connection, invoice_id)
                resolution = detail["resolution"]
                if not resolution or resolution["id"] != payload["resolution_id"] or any(c["status"] != "pass" for c in detail["checks"]):
                    raise DomainError("approval_required", "Prepare a current resolution with every reviewed check passing before applying its reference.", 409)
                document = detail["documents"][-1]
                if document["id"] != resolution["document_id"] or document["sha256"] != resolution["document_sha256"]:
                    raise DomainError("approval_stale", "The current evidence does not match the approved snapshot.", 409)
                original = connection.execute("SELECT content FROM documents WHERE id=? AND invoice_id=?", (document["id"], invoice_id)).fetchone()
                if not original or hashlib.sha256(original[0]).hexdigest() != resolution["document_sha256"]:
                    raise DomainError("evidence_unavailable", "The approved source document is missing or damaged.", 409)
                if row["applied_reference"] == payload["reference"] and connection.execute("SELECT 1 FROM external_actions WHERE invoice_id=? AND kind='apply' AND state='confirmed' AND json_extract(payload,'$.resolution_id')=?", (invoice_id, payload["resolution_id"])).fetchone():
                    return None
            action_id = str(uuid.uuid4())
            if kind == "send":
                payload["message_id"] = f"<invoiceclear.{action_id}@{mailbox.rsplit('@', 1)[1]}>"
            connection.execute("INSERT INTO external_actions(id,invoice_id,kind,key,fingerprint,state,company_id,environment,mailbox,payload,recorded_at,updated_at) VALUES(?,?,?,?,?,'pending',?,?,?,?,?,?)", (action_id, invoice_id, kind, body["idempotency_key"], fingerprint, row["company_id"], row["environment"], mailbox, packed(payload), stamp(), stamp()))
            self.service._audit(connection, invoice_id, "external_" + kind + "_reserved", payload["actor"], {"action_id": action_id, "expected_version": body["expected_version"], "company_id": row["company_id"], "environment": row["environment"], "mailbox": mailbox})
            return connection.execute("SELECT * FROM external_actions WHERE id=?", (action_id,)).fetchone()

    def _finish(self, action, receipt=None, error=None):
        state = "confirmed" if receipt is not None else "unknown"
        with self.service._write() as connection:
            current = connection.execute("SELECT state FROM external_actions WHERE id=?", (action["id"],)).fetchone()
            if current and current["state"] == "confirmed":
                return
            connection.execute("UPDATE external_actions SET state=?,receipt=?,error=?,updated_at=? WHERE id=?", (state, packed(receipt or {}), error, stamp(), action["id"]))
            self.service._audit(connection, action["invoice_id"], "external_" + action["kind"] + "_" + state, json.loads(action["payload"])["actor"], {"action_id": action["id"], "receipt": receipt, "error": error})
        if error:
            raise DomainError("external_action_uncertain", error, 409)

    def _validate_action_account(self, action):
        account = self._account("qbo")
        if str(account["account_id"]) != action["company_id"] or account["environment"] != action["environment"]:
            raise DomainError("account_changed", "Reconnect the original accounting company to reconcile this action.", 409)
        if action["kind"] == "send" and self._account("gmail")["account_id"].lower() != action["mailbox"]:
            raise DomainError("account_changed", "Reconnect the original mailbox to reconcile this message.", 409)

    @staticmethod
    def _headers(message):
        headers = {}
        for item in (message.get("payload") or {}).get("headers", []):
            name = item.get("name", "").lower()
            headers.setdefault(name, []).append(item.get("value", ""))
        return headers

    def _reconcile_send(self, action):
        self._validate_action_account(action)
        payload = json.loads(action["payload"])
        listing = self.connections.gmail("GET", "messages?" + urlencode({"q": "in:sent rfc822msgid:" + payload["message_id"], "maxResults": 10}), expected_account=action["mailbox"])
        for candidate in listing.get("messages", [])[:10]:
            message = self.connections.gmail("GET", "messages/" + identifier(candidate.get("id")) + "?format=metadata", expected_account=action["mailbox"])
            headers = self._headers(message)
            recipients = [address.lower() for _, address in getaddresses(headers.get("to", []))]
            if (headers.get("message-id") == [payload["message_id"]] and "SENT" in message.get("labelIds", []) and recipients == [payload["to"]] and identifier(message.get("threadId"))):
                self._finish(action, {"id": identifier(message.get("id")), "threadId": message["threadId"], "reconciled": True})
                return self._result(action["invoice_id"], True)
        self._finish(action, error="The send result is uncertain. Check the sent mailbox manually; this application will not resend it.")
        return self._result(action["invoice_id"], True)

    def send_request(self, invoice_id, body, actor="finance"):
        fingerprint, prior = self._request_identity(invoice_id, body, "send")
        if prior:
            self._validate_action_account(prior)
            return self._result(invoice_id, True) if prior["state"] == "confirmed" else self._reconcile_send(prior)
        mailbox = self._account("gmail")["account_id"].lower()
        self._clear(self.refresh_invoice(invoice_id))
        draft = self.service.get_request(invoice_id)
        domain = draft["to"].rsplit("@", 1)[-1].lower()
        if domain in {"example.com", "example.net", "example.org", "localhost"} or domain.endswith((".example", ".test", ".invalid", ".localhost", ".demo")):
            raise DomainError("recipient_not_real", "Set a real billing recipient in QuickBooks before sending.", 409)
        action = self._reserve(invoice_id, body, "send", fingerprint, {**draft, "actor": actor}, mailbox)
        if action is None:
            return self._result(invoice_id, True)
        payload = json.loads(action["payload"])
        message = EmailMessage(policy=policy.SMTP)
        for name, value in {"From": mailbox, "To": payload["to"], "Subject": payload["subject"], "Message-ID": payload["message_id"], "Date": format_datetime(datetime.now(timezone.utc))}.items():
            message[name] = value
        message.set_content(payload["body"])
        try:
            self._validate_action_account(action)
            receipt = self.connections.gmail("POST", "messages/send", {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")}, expected_account=action["mailbox"])
            receipt = {"id": identifier(receipt.get("id")), "threadId": identifier(receipt.get("threadId"))}
        except Exception:
            self._finish(action, error="The send result is uncertain. Retry this confirmation to check the mailbox; do not send another request.")
        self._finish(action, receipt)
        return self._result(invoice_id)

    def reconcile_request(self, invoice_id):
        with self.service._connection() as connection:
            self._mapping(connection, invoice_id, self._account("qbo"))
            action = connection.execute("SELECT * FROM external_actions WHERE invoice_id=? AND kind='send' AND state IN ('pending','unknown') ORDER BY recorded_at LIMIT 1", (invoice_id,)).fetchone()
        if not action:
            return self._result(invoice_id, True)
        return self._reconcile_send(action)

    def reconcile_reference(self, invoice_id):
        with self.service._connection() as connection:
            self._mapping(connection, invoice_id, self._account("qbo"))
            action = connection.execute("SELECT * FROM external_actions WHERE invoice_id=? AND kind='apply' AND state IN ('pending','unknown') ORDER BY recorded_at LIMIT 1", (invoice_id,)).fetchone()
        if not action:
            return self._result(invoice_id, True)
        return self._verify_apply(action, duplicate=True)

    def apply_reference(self, invoice_id, body, actor="finance"):
        fingerprint, prior = self._request_identity(invoice_id, body, "apply")
        if prior:
            self._validate_action_account(prior)
            if prior["state"] == "confirmed":
                return self._result(invoice_id, True)
            return self._verify_apply(prior, duplicate=True)
        if not getattr(self.settings, "qbo_po_field_id", ""):
            raise DomainError("po_field_required", "Configure the exact QuickBooks PO custom-field DefinitionId before applying a reference.", 409)
        self._clear(self.refresh_invoice(invoice_id))
        detail = self.service.get_invoice(invoice_id)
        resolution = detail["resolution"]
        if not resolution:
            raise DomainError("approval_required", "Prepare a reviewed resolution before applying its reference.", 409)
        with self.service._connection() as connection:
            row = self._mapping(connection, invoice_id, self._account("qbo"))
            remote = json.loads(row["remote_invoice"])
        fields = remote.get("CustomField", [])
        field_id = str(self.settings.qbo_po_field_id)
        target = next((f for f in fields if str(f.get("DefinitionId")) == field_id), None)
        if target is None or target.get("Type") != "StringType":
            raise DomainError("po_field_unavailable", "The configured PO custom field is absent or not a string on this QuickBooks invoice.", 409)
        token = remote.get("SyncToken")
        if not isinstance(token, str) or not re.fullmatch(r"[0-9]{1,100}", token):
            raise DomainError("provider_data_invalid", "QuickBooks did not provide a valid update token.", 502)
        fields = [{**f, "StringValue": resolution["po_reference"]} if str(f.get("DefinitionId")) == field_id else f for f in fields]
        payload = {"actor": actor, "reference": resolution["po_reference"], "resolution_id": resolution["id"], "update": {"Id": row["external_id"], "SyncToken": token, "sparse": True, "CustomField": fields}}
        action = self._reserve(invoice_id, body, "apply", fingerprint, payload)
        if action is None:
            return self._result(invoice_id, True)
        try:
            self._validate_action_account(action)
            self.connections.qbo("POST", "invoice", payload["update"], request_id=action["id"], expected_account=action["company_id"])
        except Exception:
            # A transport failure can follow a successful update; GET is the only recovery here.
            pass
        return self._verify_apply(action)

    def _verify_apply(self, action, duplicate=False):
        self._validate_action_account(action)
        payload = json.loads(action["payload"])
        try:
            raw = self.connections.qbo("GET", "invoice/" + identifier(payload["update"]["Id"]), expected_account=action["company_id"]).get("Invoice")
            current = self._account("qbo")
            _, source = self._source(raw, current)
            with self.service._write() as connection:
                row = self._mapping(connection, action["invoice_id"], current)
                baseline = json.loads(row["original_source"])
                baseline["po_reference"] = payload["reference"]
                if source != baseline:
                    raise DomainError("external_action_uncertain", "QuickBooks has not confirmed the approved reference with the same invoice source.", 409)
                connection.execute("UPDATE external_invoices SET applied_reference=?,applied_at=? WHERE invoice_id=?", (payload["reference"], stamp(), action["invoice_id"]))
                updated = self._mapping(connection, action["invoice_id"], current)
                self._save_refresh(connection, updated, raw, source)
            self._finish(action, {"Id": source["external_id"], "SyncToken": raw.get("SyncToken"), "po_reference": payload["reference"], "verified_at": stamp(), "document_uploaded": False})
        except Exception:
            self._finish(action, error="The accounting update is unconfirmed. Retry this confirmation to check QuickBooks; the source invoice and approval remain retained.")
        return self._result(action["invoice_id"], duplicate)

    @staticmethod
    def _parts(payload):
        pending, result = [payload], []
        while pending and len(result) < 100:
            part = pending.pop(0)
            if not isinstance(part, dict):
                continue
            result.append(part)
            pending.extend(part.get("parts", [])[:100])
        return result

    def _reply_intake_allowed(self, connection, invoice_id, open_only=False):
        if connection.execute("SELECT 1 FROM external_actions WHERE invoice_id=? AND state IN ('pending','unknown') LIMIT 1", (invoice_id,)).fetchone():
            raise DomainError("external_action_pending", "Reconcile the pending external action before capturing replies.", 409)
        if open_only:
            self.service._unprepared(connection, invoice_id)

    def _intake_cursor(self, invoice_id, mailbox, scope):
        with self.service._connection() as connection:
            row = connection.execute("SELECT cursor FROM external_intake_progress WHERE invoice_id=? AND mailbox=? AND scope=?", (invoice_id, mailbox, scope)).fetchone()
        return row[0] if row else ""

    def _advance_intake(self, invoice_id, mailbox, scope, cursor, open_only):
        # Progress survives provider failures and restart; it never marks evidence
        # captured. Every mutation still observes pending/prepared holds.
        with self.service._write() as connection:
            self._reply_intake_allowed(connection, invoice_id, open_only)
            connection.execute("INSERT INTO external_intake_progress VALUES(?,?,?,?) ON CONFLICT(invoice_id,mailbox,scope) DO UPDATE SET cursor=excluded.cursor", (invoice_id, mailbox, scope, cursor))

    def _hold_message(self, invoice_id, mailbox, message, thread_id, open_only):
        message_id = identifier(message.get("id"))
        if message.get("threadId") != thread_id:
            return
        metadata = {"status": "held", "recorded_at": stamp(), "sender_authenticated": False,
                    "error": "This Gmail message could not be captured. Retry reply sync to read it again from the original request thread."}
        with self.service._write() as connection:
            self._reply_intake_allowed(connection, invoice_id, open_only)
            connection.execute("INSERT INTO external_messages(mailbox,provider_id,invoice_id,thread_id,metadata,attachments) VALUES(?,?,?,?,?, '[]') ON CONFLICT(mailbox,provider_id) DO UPDATE SET metadata=excluded.metadata WHERE external_messages.invoice_id=excluded.invoice_id AND external_messages.reply_id IS NULL", (mailbox, message_id, invoice_id, thread_id, packed(metadata)))

    def _record_message(self, invoice_id, mailbox, message, thread_id, open_only=False):
        message_id = identifier(message.get("id"))
        if message.get("threadId") != thread_id:
            return False
        headers = self._headers(message)
        expected = self.service.get_invoice(invoice_id)["customer_email"].lower()
        senders = getaddresses(headers.get("from", []))
        if len(senders) != 1 or senders[0][1].lower() != expected or "SENT" in message.get("labelIds", []):
            self._skip_message(invoice_id, mailbox, message_id, thread_id, open_only)
            return False
        parts = self._parts(message.get("payload") or {})
        bodies, attachments = [], []
        for index, part in enumerate(parts):
            filename, blob = part.get("filename", ""), part.get("body") or {}
            if filename:
                if filename.lower().endswith((".txt", ".pdf")):
                    attachments.append({"part_id": str(part.get("partId", index)), "filename": filename, "attachment_id": blob.get("attachmentId"), "data": blob.get("data"), "size": blob.get("size", 0), "document_id": None, "error": None})
            elif part.get("mimeType") == "text/plain" and blob.get("data"):
                bodies.append(decoded(blob["data"]).decode("utf-8", errors="replace"))
        content = "\n".join(bodies).strip() or ("Customer supplied an attachment in the invoice request thread." if attachments else message.get("snippet", "").strip())
        if not content:
            self._skip_message(invoice_id, mailbox, message_id, thread_id, open_only)
            return False
        try:
            received_at = datetime.fromtimestamp(int(message["internalDate"]) / 1000, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        except (KeyError, ValueError, OverflowError):
            received_at = stamp()
        metadata = {"from": expected, "rfc_message_id": headers.get("message-id", [None])[0], "corroboration": "Provider thread containing a confirmed request; From address matched the invoice recipient.", "sender_authenticated": False}
        with self.service._write() as connection:
            self._reply_intake_allowed(connection, invoice_id, open_only)
            prior = connection.execute("SELECT invoice_id,reply_id,metadata FROM external_messages WHERE mailbox=? AND provider_id=?", (mailbox, message_id)).fetchone()
            if prior and (prior["invoice_id"] != invoice_id or prior["reply_id"] or json.loads(prior["metadata"]).get("status") != "held"):
                return False
            reply_id = str(uuid.uuid4())
            connection.execute("INSERT INTO replies(id,invoice_id,text,received_at,source) VALUES(?,?,?,?, 'gmail')", (reply_id, invoice_id, content[:5000], received_at))
            connection.execute("INSERT INTO external_messages(mailbox,provider_id,invoice_id,thread_id,reply_id,metadata,attachments) VALUES(?,?,?,?,?,?,?) ON CONFLICT(mailbox,provider_id) DO UPDATE SET reply_id=excluded.reply_id,metadata=excluded.metadata,attachments=excluded.attachments", (mailbox, message_id, invoice_id, thread_id, reply_id, packed(metadata), packed(attachments)))
            connection.execute("UPDATE invoices SET version=version+1 WHERE id=?", (invoice_id,))
            self.service._audit(connection, invoice_id, "gmail_reply_recorded", mailbox, {"reply_id": reply_id, "provider_id": message_id, "thread_id": thread_id, **metadata})
        return True

    def _skip_message(self, invoice_id, mailbox, message_id, thread_id, open_only=False):
        # Successfully checked non-customer messages advance the bounded thread scan,
        # without adding a reply or claiming an attachment was processed.
        with self.service._write() as connection:
            self._reply_intake_allowed(connection, invoice_id, open_only)
            connection.execute("INSERT INTO external_messages(mailbox,provider_id,invoice_id,thread_id,metadata,attachments) VALUES(?,?,?,?,?, '[]') ON CONFLICT(mailbox,provider_id) DO UPDATE SET metadata=excluded.metadata WHERE external_messages.invoice_id=excluded.invoice_id AND external_messages.reply_id IS NULL", (mailbox, message_id, invoice_id, thread_id, packed({"status": "ignored"})))

    def _attachments(self, invoice_id, mailbox, budget, open_only=False):
        added, failures = 0, []
        with self.service._connection() as connection:
            rows = connection.execute("SELECT rowid AS intake_order,* FROM external_messages WHERE invoice_id=? AND mailbox=? ORDER BY rowid", (invoice_id, mailbox)).fetchall()
        cursor = json.loads(self._intake_cursor(invoice_id, mailbox, "attachments") or "[0,-1]")
        candidates = [(row["intake_order"], index, row) for row in rows for index, attachment in enumerate(json.loads(row["attachments"])) if not attachment.get("document_id")]
        candidates.sort(key=lambda item: (list(item[:2]) <= cursor, item[:2]))
        for row_order, index, row in candidates[:budget]:
            with self.service._connection() as connection:
                row = connection.execute("SELECT * FROM external_messages WHERE mailbox=? AND provider_id=?", (mailbox, row["provider_id"])).fetchone()
            attachments = json.loads(row["attachments"])
            attachment = attachments[index]
            if attachment.get("document_id"):
                continue
            self._advance_intake(invoice_id, mailbox, "attachments", packed([row_order, index]), open_only)
            try:
                with self.service._connection() as connection:
                    self._reply_intake_allowed(connection, invoice_id, open_only)
                invoice = self.service.get_invoice(invoice_id)
                if invoice["resolution"]:
                    raise DomainError("reopen_required", "Reopen the prepared case to attach this retained customer document.", 409)
                if not isinstance(attachment["size"], int) or not 0 <= attachment["size"] <= MAX_ATTACHMENT:
                    raise DomainError("document_too_large", "Mailbox attachments must be at most 5 MB.", 413)
                data = attachment.get("data")
                if data is None:
                    blob = self.connections.gmail("GET", "messages/" + identifier(row["provider_id"]) + "/attachments/" + identifier(attachment.get("attachment_id"), 512), expected_account=mailbox)
                    data = blob.get("data")
                raw = decoded(data)
                digest = hashlib.sha256(raw).hexdigest()
                with self.service._connection() as connection:
                    existing = connection.execute("SELECT id FROM documents WHERE invoice_id=? AND sha256=?", (invoice_id, digest)).fetchone()
                if existing:
                    attachment["document_id"] = existing[0]
                else:
                    key = "gmail:" + hashlib.sha256((mailbox + row["provider_id"] + attachment["part_id"]).encode()).hexdigest()
                    detail = self.service.attach_document(invoice_id, {"filename": attachment["filename"], "content_base64": base64.b64encode(raw).decode(), "expected_version": invoice["version"], "idempotency_key": key}, actor="Gmail attachment intake")
                    attachment["document_id"] = next(d["id"] for d in detail["documents"] if d["sha256"] == digest)
                    added += 1
                attachment["data"], attachment["error"] = None, None
            except Exception as error:
                attachment["error"] = error.message if isinstance(error, DomainError) else "Attachment retrieval failed. Retry reply sync."
                failures.append({"message_id": row["provider_id"], "filename": attachment["filename"], "reason": attachment["error"]})
            try:
                with self.service._write() as connection:
                    self._reply_intake_allowed(connection, invoice_id, open_only)
                    connection.execute("UPDATE external_messages SET attachments=? WHERE mailbox=? AND provider_id=?", (packed(attachments), mailbox, row["provider_id"]))
            except DomainError as error:
                failures.append({"message_id": row["provider_id"], "filename": attachment["filename"], "reason": error.message})
                return added, failures
        return added, failures

    def sync_replies(self, invoice_id, *, open_only=False, expected_accounts=None):
        mailbox = self._account("gmail")["account_id"].lower()
        account = self._account("qbo")
        if expected_accounts is not None and (str(account["account_id"]), account["environment"], mailbox) != tuple(expected_accounts):
            raise DomainError("account_changed", "The capture accounts changed. Review the connections before enabling capture again.", 409)
        with self.service._connection() as connection:
            self._mapping(connection, invoice_id, account)
            self._reply_intake_allowed(connection, invoice_id, open_only)
            actions = connection.execute("SELECT receipt FROM external_actions WHERE invoice_id=? AND kind='send' AND state='confirmed' AND mailbox=? ORDER BY recorded_at DESC LIMIT 10", (invoice_id, mailbox)).fetchall()
        threads = list(dict.fromkeys(json.loads(a[0])["threadId"] for a in actions))
        cursor = self._intake_cursor(invoice_id, mailbox, "threads")
        position = threads.index(cursor) + 1 if cursor in threads else 0
        threads = threads[position:] + threads[:position]
        added = skipped = processed = 0
        failures = []
        has_more = False
        for thread_id in threads:
            if processed >= 20:
                has_more = True
                break
            try:
                self._advance_intake(invoice_id, mailbox, "threads", thread_id, open_only)
                thread = self.connections.gmail("GET", "threads/" + identifier(thread_id) + "?format=full", expected_account=mailbox)
                if thread.get("id") != thread_id:
                    raise DomainError("provider_data_invalid", "Gmail returned a different request thread.", 502)
                messages = thread.get("messages", [])
                cursor = self._intake_cursor(invoice_id, mailbox, "thread:" + thread_id)
                position = next((index + 1 for index, message in enumerate(messages) if message.get("id") == cursor), 0)
                messages = messages[position:] + messages[:position]
                for message in messages:
                    with self.service._connection() as connection:
                        seen = connection.execute("SELECT reply_id,metadata FROM external_messages WHERE mailbox=? AND provider_id=?", (mailbox, str(message.get("id", "")))).fetchone()
                    if seen and (seen["reply_id"] or json.loads(seen["metadata"]).get("status") != "held"):
                        continue
                    if processed >= 20:
                        has_more = True
                        break
                    processed += 1
                    self._advance_intake(invoice_id, mailbox, "thread:" + thread_id, str(message.get("id", "")), open_only)
                    try:
                        if self._record_message(invoice_id, mailbox, message, thread_id, open_only):
                            added += 1
                        else:
                            skipped += 1
                    except Exception:
                        try:
                            self._hold_message(invoice_id, mailbox, message, thread_id, open_only)
                        except DomainError as error:
                            if error.code in {"external_action_pending", "reopen_required"}:
                                raise
                        failures.append({"thread_id": thread_id, "message_id": str(message.get("id", ""))[:100], "reason": "A Gmail message is held for retry; its original remains in the request thread."})
            except Exception as error:
                failures.append({"thread_id": thread_id, "reason": error.message if isinstance(error, DomainError) else "Reply retrieval failed. Retry this thread sync."})
        documents_added, attachment_failures = self._attachments(invoice_id, mailbox, 10, open_only)
        return {"added": added, "documents_added": documents_added, "skipped": skipped, "failures": failures + attachment_failures, "has_more": has_more, **self._result(invoice_id)}
