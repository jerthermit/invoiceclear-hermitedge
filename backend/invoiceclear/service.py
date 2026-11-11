"""Transactional source records, evidence review and internal resolution preparation."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import threading
import uuid
import zipfile
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from .delivery import billing_brief, billing_lines_csv, spreadsheet_cell
from .config import DEFAULT_TOGETHER_MODEL
from .public_seed import (FIXTURE, REVIEWER, SEED_TIME, case_provenance,
                          document_source, public_cases, recording_case)
from .documents import (DomainError, decimal_amount, decode_document, money,
                        normalized, reference, review_evidence, rules_extract,
                        together_extract, validate_fields)

ENTRY_FIELDS = ("invoice_number", "customer", "customer_email", "amount", "currency", "issue_date", "due_date", "po_reference", "source_ref")
# Bound the shared public installation, not a private client's invoice capacity.
PUBLIC_MAX_BYTES = 128 * 1024 * 1024
PUBLIC_MAX_ROWS = 50_000
PUBLIC_TABLES = ("invoices", "documents", "replies", "resolutions", "audit", "idempotency", "agent_runs")
_UPLOAD_LOCKS = {}
_UPLOAD_GUARD = threading.Lock()
SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
 id TEXT PRIMARY KEY, invoice_number TEXT NOT NULL, customer TEXT NOT NULL,
 customer_norm TEXT NOT NULL, customer_email TEXT NOT NULL, amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
 currency TEXT NOT NULL, issue_date TEXT NOT NULL, due_date TEXT NOT NULL,
 po_reference TEXT NOT NULL, proposed_po_reference TEXT, source_ref TEXT NOT NULL,
 payment_status TEXT NOT NULL DEFAULT 'unpaid' CHECK(payment_status='unpaid'),
 version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, provenance TEXT NOT NULL,
 UNIQUE(invoice_number,customer_norm));
CREATE TABLE IF NOT EXISTS documents (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
 invoice_id TEXT NOT NULL REFERENCES invoices(id), filename TEXT NOT NULL,
 sha256 TEXT NOT NULL, mime TEXT NOT NULL, content BLOB NOT NULL, pages TEXT NOT NULL,
 suggested_fields TEXT NOT NULL, fields TEXT NOT NULL DEFAULT '{}',
 evidence TEXT NOT NULL, review_evidence TEXT NOT NULL DEFAULT '{}', reviewed INTEGER NOT NULL DEFAULT 0,
 extraction_method TEXT NOT NULL, extraction_error TEXT, uploaded_at TEXT NOT NULL,
 reviewed_by TEXT, reviewed_at TEXT, review_note TEXT,
 UNIQUE(invoice_id,sha256));
CREATE TABLE IF NOT EXISTS replies (
 id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL REFERENCES invoices(id), text TEXT NOT NULL,
 received_at TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'manual');
CREATE TABLE IF NOT EXISTS resolutions (
 id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL REFERENCES invoices(id),
 customer_norm TEXT NOT NULL, po_norm TEXT NOT NULL, currency TEXT NOT NULL,
 amount_cents INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1, data TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_resolution ON resolutions(invoice_id) WHERE active=1;
CREATE INDEX IF NOT EXISTS resolution_allocations ON resolutions(customer_norm,po_norm,currency,active);
CREATE TABLE IF NOT EXISTS audit (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id TEXT NOT NULL REFERENCES invoices(id),
 event TEXT NOT NULL, actor TEXT NOT NULL, recorded_at TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS idempotency (
 invoice_id TEXT NOT NULL REFERENCES invoices(id), key TEXT NOT NULL, fingerprint TEXT NOT NULL,
 response TEXT NOT NULL, PRIMARY KEY(invoice_id,key));
CREATE TABLE IF NOT EXISTS provider_usage (
 day TEXT PRIMARY KEY, calls INTEGER NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS agent_runs (
 id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL REFERENCES invoices(id),
 idempotency_key TEXT NOT NULL, source_version INTEGER NOT NULL,
 source_fingerprint TEXT NOT NULL, source_snapshot TEXT NOT NULL,
 status TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT,
 lease_until REAL NOT NULL, model TEXT NOT NULL,
 summary TEXT NOT NULL DEFAULT '', next_step TEXT NOT NULL DEFAULT 'none',
 draft TEXT NOT NULL DEFAULT '', citations TEXT NOT NULL DEFAULT '[]',
 trace TEXT NOT NULL DEFAULT '[]', error TEXT,
 UNIQUE(invoice_id,idempotency_key));
CREATE UNIQUE INDEX IF NOT EXISTS one_running_investigation ON agent_runs(invoice_id) WHERE status='running';
"""


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def packed(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def text(value, name: str, maximum: int = 200, *, optional=False) -> str:
    if not isinstance(value, str):
        raise DomainError("invalid_input", f"{name} must be text.")
    value = value.strip()
    if (not value and not optional) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise DomainError("invalid_input", f"{name} must contain {'0' if optional else '1'}–{maximum} printable characters.")
    return value


def billing_instruction(source_reference: str, approved_reference: str) -> str:
    if not source_reference:
        return f"Add PO reference {approved_reference} to the billing record and attach the reviewed purchase order."
    if reference(source_reference) != reference(approved_reference):
        return f"Correct the billing PO reference from {source_reference} to {approved_reference} and attach the reviewed purchase order."
    return f"Attach the reviewed purchase order {approved_reference} to support this invoice's billing submission."


class Service:
    def __init__(self, database_path: Path, demo: bool = True, provider: str = "rules", together_key: str | None = None, together_model: str = DEFAULT_TOGETHER_MODEL):
        self.database_path = Path(database_path)
        # One server normally owns a workspace. Share this across Service
        # instances too, so an exact concurrent upload retry cannot dispatch
        # extraction again while its first attempt is outside SQLite.
        with _UPLOAD_GUARD:
            self._upload_lock = _UPLOAD_LOCKS.setdefault(str(self.database_path.resolve()), threading.Lock())
        self.demo, self.provider = bool(demo), provider
        self.together_key, self.together_model = together_key, together_model
        if provider not in ("rules", "together"):
            raise ValueError("InvoiceClear provider must be rules or together")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        # Protect source documents before SQLite creates its WAL/SHM sidecars.
        self.database_path.touch(mode=0o600, exist_ok=True)
        self.database_path.chmod(0o600)
        with self._connection() as connection:
            self._bind_mode(connection)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
        if self.demo:
            with self._write() as connection:
                self._seed(connection)

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.database_path.resolve().as_uri() + "?mode=rw", uri=True,
                                     timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write(self):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                if self.demo and connection.total_changes:
                    rows = sum(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in PUBLIC_TABLES)
                    size = connection.execute("PRAGMA page_count").fetchone()[0] * connection.execute("PRAGMA page_size").fetchone()[0]
                    if rows > PUBLIC_MAX_ROWS or size > PUBLIC_MAX_BYTES:
                        raise DomainError("public_capacity", "This public workspace has reached its storage limit. Existing records and downloads remain available; ask the host for a separate workspace.", 429)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _bind_mode(self, connection):
        """Bind before seeding or opening public access; never convert stored records."""
        requested = "demo" if self.demo else "private"
        connection.execute("BEGIN IMMEDIATE")
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            if "workspace_identity" in tables:
                row = connection.execute("SELECT mode FROM workspace_identity WHERE id=1").fetchone()
                existing = row[0] if row else None
                if existing not in {"demo", "private"}:
                    raise ValueError("Workspace identity is unavailable. Preserve this database and inspect a recovery copy before starting.")
            else:
                modes = set()
                if "invoices" in tables:
                    for row in connection.execute("SELECT DISTINCT provenance FROM invoices"):
                        try:
                            mode = json.loads(row[0]).get("mode")
                        except (ValueError, TypeError, AttributeError):
                            mode = None
                        modes.add(mode)
                if any(table in tables and connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                       for table in ("connection_grants", "external_invoices", "external_actions", "external_messages")):
                    modes.add("private")
                if modes - {"demo", "private"} or len(modes) > 1:
                    raise ValueError("Stored workspace provenance is ambiguous. Preserve this database and inspect a recovery copy; do not expose it publicly.")
                existing = next(iter(modes), requested)
            if existing != requested:
                raise ValueError("This database belongs to a different workspace mode. Use a separate database path; do not switch or erase stored records.")
            connection.execute("CREATE TABLE IF NOT EXISTS workspace_identity (id INTEGER PRIMARY KEY CHECK(id=1), mode TEXT NOT NULL CHECK(mode IN ('demo','private')))")
            connection.execute("INSERT OR IGNORE INTO workspace_identity VALUES(1,?)", (requested,))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    @staticmethod
    def _invoice(connection, invoice_id):
        row = connection.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        if row is None:
            raise DomainError("invoice_not_found", "Invoice was not found.", 404)
        return row

    @staticmethod
    def _audit(connection, invoice_id, event, actor, data):
        connection.execute("INSERT INTO audit(invoice_id,event,actor,recorded_at,data) VALUES(?,?,?,?,?)", (invoice_id, event, actor, stamp(), packed(data)))

    @staticmethod
    def _entry(body):
        if not isinstance(body, dict):
            raise DomainError("invalid_input", "Invoice entry must be an object.")
        values = {field: text(body.get(field, ""), field.replace("_", " "), 200, optional=field == "po_reference") for field in ENTRY_FIELDS if field != "amount"}
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", values["customer_email"]):
            raise DomainError("invalid_email", "Provide a valid customer email address.")
        values["currency"] = values["currency"].upper()
        if not re.fullmatch(r"[A-Z]{3}", values["currency"]):
            raise DomainError("invalid_currency", "Currency must be a three-letter code.")
        for field in ("issue_date", "due_date"):
            try:
                if date.fromisoformat(values[field]).isoformat() != values[field]:
                    raise ValueError()
            except ValueError:
                raise DomainError("invalid_date", "Issue and due dates must use YYYY-MM-DD.") from None
        if values["due_date"] < values["issue_date"]:
            raise DomainError("invalid_date", "Due date must be on or after issue date.")
        values["amount_cents"] = money(body.get("amount"))
        values["customer_norm"] = normalized(values["customer"])
        if not any(c.isalnum() for c in values["customer_norm"]):
            raise DomainError("invalid_customer", "Customer must contain letters or numbers.")
        values["invoice_number"] = values["invoice_number"].upper()
        values["customer_email"] = values["customer_email"].lower()
        return values

    def _insert(self, connection, values, actor, *, invoice_id=None, provenance=None, seed=False):
        invoice_id = invoice_id or str(uuid.uuid4())
        columns = ("id",) + tuple(values) + ("created_at", "provenance")
        provenance = provenance or {"mode": "demo" if self.demo else "private", "intake": "manual", "actor": actor}
        parameters = (invoice_id,) + tuple(values.values()) + (stamp(), packed(provenance))
        result = connection.execute(f"INSERT {'OR IGNORE ' if seed else ''}INTO invoices({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", parameters)
        if not result.rowcount:
            return invoice_id
        self._audit(connection, invoice_id, "invoice_created", actor, {"source": values, "provenance": provenance})
        return invoice_id

    @staticmethod
    def _document_json(row):
        document = {name: row[name] for name in ("id", "filename", "sha256", "extraction_method", "extraction_error", "uploaded_at", "reviewed_by", "reviewed_at")}
        document.update({name: json.loads(row[name]) for name in ("pages", "suggested_fields", "fields", "evidence", "review_evidence")})
        document["reviewed"] = bool(row["reviewed"])
        return document

    def _checks(self, connection, invoice, documents):
        document = documents[-1] if documents else None
        doc_id = document["id"] if document else None
        fields = json.loads(document["fields"]) if document and document["reviewed"] else {}
        evidence = json.loads(document["review_evidence"]) if document else {}
        checks = []

        def check(code, label, field, invoice_value, document_value, passed, message):
            state = "hold" if not document or not document["reviewed"] else "pass" if passed else "fail"
            checks.append({"code": code, "label": label, "status": state, "invoice_value": invoice_value, "document_value": document_value, "message": message if state != "hold" else "A source purchase order must be uploaded and reviewed.", "document_id": doc_id, "source_page": evidence.get(field, {}).get("page")})

        check("document_review", "Evidence review", "po_number", "Human-reviewed purchase order", "Reviewed" if fields else "Unreviewed" if document else None, bool(fields), "The purchase-order fields were reviewed against the retained source.")
        check("customer", "Customer", "customer", invoice["customer"], fields.get("customer"), bool(fields) and normalized(fields.get("customer", "")) == invoice["customer_norm"], "Customer identity must match the source invoice.")
        check("currency", "Currency", "currency", invoice["currency"], fields.get("currency"), fields.get("currency") == invoice["currency"], "Purchase-order currency must match the invoice currency.")
        effective = invoice["proposed_po_reference"] or invoice["po_reference"]
        po = fields.get("po_number", "")
        check("po_reference", "PO reference", "po_number", effective or None, po or None, bool(reference(effective)) and reference(effective) == reference(po), "Use a matching source reference or record a supported reference proposal.")
        reserved = 0
        amount = money(fields["amount"]) if fields else None
        if fields:
            reserved = connection.execute("SELECT COALESCE(SUM(amount_cents),0) FROM resolutions WHERE active=1 AND invoice_id<>? AND customer_norm=? AND po_norm=? AND currency=?", (invoice["id"], normalized(fields["customer"]), reference(po), fields["currency"])).fetchone()[0]
        available = amount - reserved if amount is not None else None
        check("po_coverage", "PO amount coverage", "amount", invoice["amount_cents"], amount, available is not None and available >= invoice["amount_cents"], f"PO amount must cover this invoice after {decimal_amount(reserved)} {invoice['currency']} in prepared workspace allocations. External PO usage is not verified.")
        checks[-1].update(available_cents=available, reserved_cents=reserved)
        return checks

    def _detail(self, connection, invoice_id, *, summary=False):
        row = self._invoice(connection, invoice_id)
        if summary:
            documents = connection.execute("SELECT id,fields,review_evidence,reviewed FROM documents WHERE invoice_id=? ORDER BY seq DESC LIMIT 1", (invoice_id,)).fetchall()
        else:
            documents = connection.execute("SELECT id,filename,sha256,pages,suggested_fields,fields,evidence,review_evidence,reviewed,extraction_method,extraction_error,uploaded_at,reviewed_by,reviewed_at FROM documents WHERE invoice_id=? ORDER BY seq", (invoice_id,)).fetchall()
        checks = self._checks(connection, row, documents)
        resolution_row = connection.execute(f"SELECT {'id' if summary else 'data'} FROM resolutions WHERE invoice_id=? AND active=1", (invoice_id,)).fetchone()
        resolution = json.loads(resolution_row[0]) if resolution_row and not summary else None
        if resolution_row:
            status, blocker = "prepared", "Resolution prepared for finance review"
        elif not documents:
            status, blocker = "missing_po", "Purchase order missing"
        elif not documents[-1]["reviewed"]:
            status, blocker = "needs_review", "Purchase-order fields need human review"
        elif any(check["status"] != "pass" for check in checks):
            status = "mismatch"
            failed = next(check for check in checks if check["status"] != "pass")
            blocker = "Insufficient PO coverage" if failed["code"] == "po_coverage" else failed["label"] + " does not match"
        else:
            status, blocker = "ready", "Evidence checked; ready to prepare"
        detail = {name: row[name] for name in ("id", "invoice_number", "customer", "customer_email", "amount_cents", "currency", "issue_date", "due_date", "po_reference", "proposed_po_reference", "source_ref", "payment_status", "version")}
        if summary:
            detail.update(status=status, blocker=blocker)
            return detail
        instruction = (resolution.get("billing_instruction") or billing_instruction(resolution["source_po_reference"], resolution["po_reference"])) if resolution else billing_instruction(row["po_reference"], row["proposed_po_reference"] or row["po_reference"])
        detail.update(status=status, blocker=blocker, checks=checks, documents=[self._document_json(d) for d in documents], replies=[dict(r) for r in connection.execute("SELECT id,text,received_at,source FROM replies WHERE invoice_id=? ORDER BY rowid", (invoice_id,))], resolution=resolution, billing_instruction=instruction, request=self._request(row, checks, status))
        return detail

    @staticmethod
    def _request(row, checks, status):
        ref = row["proposed_po_reference"] or row["po_reference"]
        body = f"Hello {row['customer']},\n\nWe are reviewing unpaid invoice {row['invoice_number']} for {decimal_amount(row['amount_cents'])} {row['currency']}. "
        if status == "missing_po":
            body += f"Please provide the purchase order and confirm reference {ref}." if ref else "Please provide the purchase order and confirm its reference."
            body += " Please include the customer name, currency and authorized amount."
        elif status == "needs_review":
            body += "We have a purchase-order document and are reviewing its source details. Please confirm that it authorizes this invoice, including the customer name, PO reference, currency and amount."
        elif status == "mismatch":
            explanations = []
            for check in checks:
                if check["status"] != "fail":
                    continue
                actual, expected = check["document_value"], check["invoice_value"]
                if check["code"] == "customer":
                    explanations.append(f"The document names {actual}, while the invoice customer is {expected}. Please confirm the customer the purchase order authorizes.")
                elif check["code"] == "currency":
                    explanations.append(f"The document uses {actual}, while the invoice uses {expected}. Please provide authorization in the invoice currency.")
                elif check["code"] == "po_reference":
                    explanations.append(f"The invoice review uses PO reference {expected or '(missing)'}, while the document shows {actual}. Please confirm the correct reference for this invoice.")
                elif check["code"] == "po_coverage":
                    explanations.append(f"The purchase order authorizes {decimal_amount(actual)} {row['currency']}, with {decimal_amount(check['available_cents'])} {row['currency']} available after prepared workspace allocations. Please provide sufficient authorization for this invoice.")
            body += " ".join(explanations)
        else:
            body += f"We have reviewed purchase order {ref} against this invoice. Please let us know if its reference or authorization needs correction."
        body += "\n\nThank you,\nFinance"
        return {"to": row["customer_email"], "subject": f"Purchase order needed — {row['invoice_number']}", "body": body}

    def list_invoices(self, view="open", q="", offset=0, limit=50):
        if view not in ("open", "prepared", "all") or not isinstance(q, str) or len(q) > 200:
            raise DomainError("invalid_query", "Choose open, prepared or all and a search of at most 200 characters.")
        if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 100:
            raise DomainError("invalid_pagination", "Offset must be nonnegative and limit must be between 1 and 100.")
        with self._connection() as connection:
            connection.execute("BEGIN")
            conditions, parameters = [], []
            prepared = "EXISTS (SELECT 1 FROM resolutions WHERE invoice_id=invoices.id AND active=1)"
            if view != "all":
                conditions.append("NOT " + prepared if view == "open" else prepared)
            needle = q.strip().casefold()
            if needle:
                # SQLite's built-in case folding is ASCII-only. Keep the same
                # Unicode and literal substring search as the source fields.
                connection.create_function("invoiceclear_casefold", 1, str.casefold, deterministic=True)
                searchable = " || ' ' || ".join(f"COALESCE({name},'')" for name in ("invoice_number", "customer", "customer_email", "po_reference", "proposed_po_reference", "source_ref"))
                conditions.append(f"instr(invoiceclear_casefold({searchable}), ?) > 0")
                parameters.append(needle)
            where = " WHERE " + " AND ".join(conditions) if conditions else ""
            total = connection.execute("SELECT COUNT(*) FROM invoices" + where, parameters).fetchone()[0]
            # Also handles valid Python offsets larger than SQLite's integers.
            ids = [] if offset >= total else [row[0] for row in connection.execute(
                "SELECT id FROM invoices" + where + " ORDER BY due_date,invoice_number,id LIMIT ? OFFSET ?",
                parameters + [limit, offset])]
            values = [self._detail(connection, invoice_id, summary=True) for invoice_id in ids]
            return {"invoices": values, "total": total, "offset": offset, "has_more": offset + limit < total}

    def get_invoice(self, invoice_id):
        with self._connection() as connection:
            connection.execute("BEGIN")
            return self._detail(connection, invoice_id)

    @staticmethod
    def _history_entry(row):
        """Project recorded facts only; provider/model payloads never leave this boundary."""
        summaries = {
            "invoice_created": "Invoice added",
            "document_uploaded": "Purchase order uploaded",
            "demo_document_seeded": "Purchase order retained",
            "review_document": "Purchase-order fields reviewed",
            "propose_reference": "PO reference correction proposed",
            "record_reply": "Customer reply recorded manually",
            "approve_resolution": "Billing handoff prepared",
            "reopen": "Billing handoff reopened",
            "gmail_reply_recorded": "Customer reply received in the request thread",
            "external_send_reserved": "Approved email request queued for sending",
            "external_send_confirmed": "Email request confirmed by Gmail",
            "external_send_unknown": "Email request needs receipt verification",
            "external_apply_reserved": "Approved accounting reference update queued",
            "external_apply_confirmed": "Accounting reference update verified",
            "external_apply_unknown": "Accounting reference update needs verification",
            "investigation_started": "Invoice investigation started",
            "investigation_completed": "Invoice investigation recorded",
            "investigation_failed": "Invoice investigation needs another attempt",
            "investigation_interrupted": "Invoice investigation interrupted",
        }
        event = row["event"]
        entry = {"id": row["seq"], "event": event, "actor": row["actor"],
                 "recorded_at": row["recorded_at"], "summary": summaries.get(event, "Case event recorded")}
        try:
            data = json.loads(row["data"])
        except (ValueError, TypeError):
            data = {}
        if not isinstance(data, dict):
            return entry
        details = {}

        def keep(key, source=None, *, name=None, maximum=500):
            value = (data if source is None else source).get(key)
            if isinstance(value, str) and value and len(value) <= maximum and not any(ord(c) < 32 for c in value):
                details[name or key] = value

        if event == "invoice_created":
            source, provenance = data.get("source"), data.get("provenance")
            if isinstance(source, dict):
                keep("source_ref", source, maximum=200)
            if isinstance(provenance, dict) and provenance.get("intake") in ("manual", "csv", "qbo", "fixed_demo_seed"):
                details["intake"] = provenance["intake"]
        elif event in ("document_uploaded", "demo_document_seeded", "review_document"):
            keep("document_id", maximum=128)
            if event == "document_uploaded":
                keep("filename", maximum=200)
                if data.get("extraction_method") in ("rules", "together"):
                    details["extraction_method"] = data["extraction_method"]
                details["extraction_needs_review"] = bool(data.get("extraction_error"))
            if event == "review_document":
                keep("note")
            evidence = data.get("source_evidence")
            if isinstance(evidence, dict):
                pages = sorted({item["page"] for item in evidence.values() if isinstance(item, dict)
                                and type(item.get("page")) is int and 1 <= item["page"] <= 20})
                if pages:
                    details["source_pages"] = pages
        elif event == "propose_reference":
            keep("source_po_reference", maximum=200)
            keep("proposed_po_reference", maximum=80)
            keep("note")
        elif event == "approve_resolution":
            keep("id", name="resolution_id", maximum=128)
            keep("po_reference", maximum=200)
            keep("document_id", maximum=128)
            keep("document_sha256", maximum=64)
            keep("note")
        elif event == "reopen":
            keep("resolution_id", maximum=128)
            keep("note")
        elif event in ("record_reply", "gmail_reply_recorded"):
            keep("reply_id", maximum=128)
            details["source"] = "manual" if event == "record_reply" else "gmail"
        elif event.startswith("investigation_") and event in summaries:
            keep("run_id", maximum=128)
            keep("model", maximum=200)
            keep("status", maximum=40)
        elif event in summaries and event.startswith("external_"):
            keep("action_id", maximum=128)
            if data.get("environment") in ("sandbox", "production"):
                details["environment"] = data["environment"]
            receipt = data.get("receipt")
            if isinstance(receipt, dict):
                keep("id", receipt, name="receipt_id", maximum=128)
                keep("Id", receipt, name="receipt_id", maximum=128)
                keep("threadId", receipt, name="thread_id", maximum=128)
                keep("po_reference", receipt, maximum=200)
                keep("verified_at", receipt, maximum=40)
                for name in ("reconciled", "document_uploaded"):
                    if type(receipt.get(name)) is bool:
                        details[name] = receipt[name]
        if details:
            entry["details"] = details
        return entry

    def history(self, invoice_id, limit=20, before=None):
        """Bounded, reverse-chronological audit history with an exclusive sequence cursor."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise DomainError("invalid_input", "History limit must be an integer between 1 and 100.")
        if before is not None and (type(before) is not int or not 1 <= before <= 9223372036854775807):
            raise DomainError("invalid_input", "History cursor must be a positive integer audit sequence.")
        with self._connection() as connection:
            connection.execute("BEGIN")
            self._invoice(connection, invoice_id)
            rows = connection.execute(
                "SELECT seq,event,actor,recorded_at,data FROM audit WHERE invoice_id=? "
                "AND (? IS NULL OR seq<?) ORDER BY seq DESC LIMIT ?",
                (invoice_id, before, before, limit + 1)).fetchall()
            has_more = len(rows) > limit
            events = [self._history_entry(row) for row in rows[:limit]]
            return {"events": events, "has_more": has_more,
                    "next_before": events[-1]["id"] if has_more else None}

    def create_invoice(self, body, actor="finance"):
        values = self._entry(body)
        with self._write() as connection:
            if connection.execute("SELECT id FROM invoices WHERE invoice_number=? AND customer_norm=?", (values["invoice_number"], values["customer_norm"])).fetchone():
                raise DomainError("duplicate_invoice", "This invoice number and customer already exist.", 409)
            invoice_id = self._insert(connection, values, actor)
            return self._detail(connection, invoice_id)

    def import_invoices(self, csv_text, actor="finance"):
        if not isinstance(csv_text, str):
            raise DomainError("invalid_csv", "CSV must be text.")
        if len(csv_text.encode("utf-8")) > 1024 * 1024:
            raise DomainError("csv_too_large", "CSV imports must be at most 1 MB.", 413)
        try:
            reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")), strict=True)
            if reader.fieldnames is None or len(reader.fieldnames) != len(ENTRY_FIELDS) or set(reader.fieldnames) != set(ENTRY_FIELDS):
                raise DomainError("invalid_csv", "CSV headers must match the nine invoice-entry fields.")
            rows = []
            for number, row in enumerate(reader, 2):
                if len(rows) >= 500:
                    raise DomainError("csv_too_large", "CSV imports must have at most 500 rows.", 413)
                if None in row or any(value is None for value in row.values()):
                    raise DomainError("invalid_csv", f"CSV row {number} has the wrong number of fields.")
                try:
                    rows.append((self._entry(row), row))
                except DomainError as error:
                    raise DomainError(error.code, f"CSV row {number}: {error.message}", error.status) from None
        except csv.Error:
            raise DomainError("invalid_csv", "CSV content is malformed.") from None
        if not rows:
            raise DomainError("invalid_csv", "CSV must contain at least one invoice.")
        created, duplicates = 0, 0
        # Only exact server-known source rows inherit authored-fixture detail.
        fixtures = public_cases() + [recording_case()]
        with self._write() as connection:
            for values, source_row in rows:
                prior = connection.execute("SELECT * FROM invoices WHERE invoice_number=? AND customer_norm=?", (values["invoice_number"], values["customer_norm"])).fetchone()
                if prior:
                    if any(prior[field] != value for field, value in values.items()):
                        raise DomainError("conflicting_duplicate", "A duplicate invoice has different source fields. The whole import was rejected.", 409)
                    duplicates += 1
                    continue
                source_case = next((item for item in fixtures if item["entry"] == source_row), None)
                provenance = case_provenance(source_case, intake="csv", actor=actor) if source_case else {"intake": "csv", "actor": actor}
                provenance["mode"] = "demo" if self.demo else "private"
                self._insert(connection, values, actor, provenance=provenance)
                created += 1
        return {"created": created, "duplicates": duplicates}

    def _mutation(self, connection, invoice_id, body, operation):
        invoice = self._invoice(connection, invoice_id)
        if not isinstance(body, dict):
            raise DomainError("invalid_input", "Action body must be an object.")
        key = body.get("idempotency_key")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", key):
            raise DomainError("invalid_idempotency_key", "Provide an idempotency key of 1–128 letters, digits, colons, underscores or hyphens.")
        fingerprint = hashlib.sha256(packed({"operation": operation, "body": body}).encode()).hexdigest()
        prior = connection.execute("SELECT fingerprint,response FROM idempotency WHERE invoice_id=? AND key=?", (invoice_id, key)).fetchone()
        if prior:
            if prior["fingerprint"] != fingerprint:
                raise DomainError("idempotency_conflict", "This idempotency key was already used for a different request.", 409)
            return invoice, fingerprint, json.loads(prior["response"])
        version = body.get("expected_version")
        if type(version) is not int or version < 1:
            raise DomainError("invalid_version", "Provide the current integer invoice version.")
        if version != invoice["version"]:
            raise DomainError("stale_version", "This invoice changed. Reload it before trying again.", 409)
        if connection.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='external_actions'").fetchone():
            if connection.execute("SELECT 1 FROM external_actions WHERE invoice_id=? AND state IN ('pending','unknown') LIMIT 1", (invoice_id,)).fetchone():
                raise DomainError("external_action_pending", "An external action is awaiting confirmation. Verify its result before changing this invoice.", 409)
        return invoice, fingerprint, None

    @staticmethod
    def _store_mutation(connection, invoice_id, body, fingerprint, result):
        connection.execute("INSERT INTO idempotency(invoice_id,key,fingerprint,response) VALUES(?,?,?,?)", (invoice_id, body["idempotency_key"], fingerprint, packed(result)))

    @staticmethod
    def _unprepared(connection, invoice_id):
        if connection.execute("SELECT 1 FROM resolutions WHERE invoice_id=? AND active=1", (invoice_id,)).fetchone():
            raise DomainError("reopen_required", "Reopen this prepared resolution before changing its documents or correction.", 409)

    def _prepare_resolution(self, connection, invoice, actor, note):
        """Use the same transactional gates for public fixtures and human actions."""
        invoice_id = invoice["id"]
        self._unprepared(connection, invoice_id)
        # Recheck stored observations while holding the workspace write lock.
        if connection.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='external_invoices'").fetchone():
            linked = connection.execute("SELECT accounting FROM external_invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
            if linked:
                accounting = json.loads(linked["accounting"])
                if not isinstance(accounting, dict) or accounting.get("error") or accounting.get("source_changed") or accounting.get("payment_status") != "unpaid":
                    raise DomainError("accounting_changed", "QuickBooks has changed or could not be verified. Refresh the invoice and review the source before approval.", 409)
        detail = self._detail(connection, invoice_id)
        if detail["status"] != "ready" or any(check["status"] != "pass" for check in detail["checks"]):
            raise DomainError("checks_not_passed", "Review the purchase order and resolve every failed or held check before preparing.", 409)
        document = detail["documents"][-1]
        fields = document["fields"]
        resolution = {"id": str(uuid.uuid4()), "prepared_at": stamp(), "prepared_by": actor, "note": note, "po_reference": invoice["proposed_po_reference"] or invoice["po_reference"], "document_id": document["id"], "document_sha256": document["sha256"], "fields": fields, "amount_cents": invoice["amount_cents"], "currency": invoice["currency"], "source_po_reference": invoice["po_reference"], "source_ref": invoice["source_ref"], "payment_status": "unpaid", "scope": "This approval prepares an internal handoff only; it does not itself send email, update accounting or create payment.", "checks": detail["checks"]}
        proposal = connection.execute("SELECT data FROM audit WHERE invoice_id=? AND event='propose_reference' ORDER BY seq DESC LIMIT 1", (invoice_id,)).fetchone()
        resolution.update(invoice_id=invoice_id, invoice_number=invoice["invoice_number"], customer=invoice["customer"], customer_email=invoice["customer_email"],
                          source_provenance=json.loads(invoice["provenance"]), document_filename=document["filename"],
                          correction_note=json.loads(proposal["data"])["note"] if proposal and invoice["proposed_po_reference"] else "",
                          billing_instruction=billing_instruction(invoice["po_reference"], resolution["po_reference"]))
        connection.execute("INSERT INTO resolutions(id,invoice_id,customer_norm,po_norm,currency,amount_cents,data) VALUES(?,?,?,?,?,?,?)", (resolution["id"], invoice_id, normalized(fields["customer"]), reference(fields["po_number"]), fields["currency"], invoice["amount_cents"], packed(resolution)))
        return resolution

    def _extract(self, pages):
        """Account for a bounded attempt before either upload or connection check calls Together."""
        if self.provider == "rules":
            return rules_extract(pages)
        if not any(page["text"].strip() for page in pages):
            return {}, {}, "No readable text was found. Upload a text-based PDF or TXT purchase order for review."
        if len(json.dumps([{"number": page["number"], "text": page["text"]} for page in pages])) > 24_000:
            return {}, {}, "Document text exceeds the extraction provider's input limit. Upload a shorter purchase order."
        if not self.together_key:
            return {}, {}, "Document extraction is unavailable. Configure the provider or upload labeled TXT evidence."
        day = datetime.now(timezone.utc).date().isoformat()
        reserved_tokens = 100_000
        with self._write() as connection:
            usage = connection.execute("SELECT calls,tokens FROM provider_usage WHERE day=?", (day,)).fetchone()
            if usage and (usage["calls"] >= 100 or usage["tokens"] + reserved_tokens > 500_000):
                return {}, {}, "Daily extraction limit reached. Source evidence is retained for manual review."
            connection.execute(
                "INSERT INTO provider_usage(day,calls,tokens) VALUES(?,1,?) "
                "ON CONFLICT(day) DO UPDATE SET calls=calls+1,tokens=tokens+excluded.tokens",
                (day, reserved_tokens))
        # Commit before dispatch. A rejected document transaction, interruption
        # or unknown provider outcome must never refund a dispatched attempt.
        try:
            fields, evidence, error, tokens = together_extract(pages, self.together_key, self.together_model)
        except Exception:
            return {}, {}, "Document extraction failed. Review the source or retry with labeled TXT evidence."
        # Zero/missing usage and failed requests retain the conservative reservation.
        # This is quota accounting, not a claim about the provider's billed usage.
        if error is None and type(tokens) is int and 1 <= tokens <= reserved_tokens:
            with self._write() as connection:
                connection.execute("UPDATE provider_usage SET tokens=tokens-?+? WHERE day=?", (reserved_tokens, tokens, day))
        return fields, evidence, error

    def probe_extraction(self):
        """Check the configured extraction path without creating authoritative case records."""
        pages = [{"number": 1, "text": "PO Number: IC-CONNECTION-CHECK\nCustomer: InvoiceClear Connection Check\nCurrency: USD\nAmount: 1.00"}]
        fields, evidence, error = self._extract(pages)
        return {"fields": fields, "evidence": evidence, "error": error}

    def _document_mutation(self, connection, invoice_id, body, digest):
        invoice, fingerprint, prior = self._mutation(connection, invoice_id, body, "attach_document")
        if prior is None:
            self._unprepared(connection, invoice_id)
            if connection.execute("SELECT 1 FROM documents WHERE invoice_id=? AND sha256=?", (invoice_id, digest)).fetchone():
                raise DomainError("duplicate_document", "This document is already attached to the invoice.", 409)
        return invoice, fingerprint, prior

    def attach_document(self, invoice_id, body, actor="finance"):
        if not isinstance(body, dict):
            raise DomainError("invalid_input", "Document body must be an object.")
        filename, content, mime, pages = decode_document(body)
        digest = hashlib.sha256(content).hexdigest()
        with self._upload_lock:
            with self._connection() as connection:
                connection.execute("BEGIN")
                _, _, prior = self._document_mutation(connection, invoice_id, body, digest)
                if prior is not None:
                    return prior
            suggestions, evidence, error = self._extract(pages)
            with self._write() as connection:
                # Staff edits or a provider action may have started during the
                # model call. Recheck every mutation gate before storing evidence.
                _, fingerprint, prior = self._document_mutation(connection, invoice_id, body, digest)
                if prior is not None:
                    return prior
                document_id = str(uuid.uuid4())
                connection.execute("INSERT INTO documents(id,invoice_id,filename,sha256,mime,content,pages,suggested_fields,evidence,extraction_method,extraction_error,uploaded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (document_id, invoice_id, filename, digest, mime, content, packed(pages), packed(suggestions), packed(evidence), self.provider, error, stamp()))
                connection.execute("UPDATE invoices SET version=version+1 WHERE id=?", (invoice_id,))
                self._audit(connection, invoice_id, "document_uploaded", actor, {"document_id": document_id, "sha256": digest, "filename": filename, "suggested_fields": suggestions, "source_evidence": evidence, "extraction_method": self.provider, "extraction_error": error})
                result = self._detail(connection, invoice_id)
                self._store_mutation(connection, invoice_id, body, fingerprint, result)
                return result

    def action(self, invoice_id, body, actor="finance"):
        with self._write() as connection:
            invoice, fingerprint, prior = self._mutation(connection, invoice_id, body, "action")
            if prior is not None:
                return {"invoice": prior, "duplicate": True}
            action = body.get("action")
            parameters = body.get("parameters", {})
            if not isinstance(parameters, dict):
                raise DomainError("invalid_parameters", "Action parameters must be an object.")
            if action in ("review_document", "propose_reference"):
                self._unprepared(connection, invoice_id)
            if action == "review_document":
                document_id = parameters.get("document_id")
                if not isinstance(document_id, str) or not document_id:
                    raise DomainError("invalid_document_id", "Provide the document to review.")
                document = connection.execute("SELECT id,pages,suggested_fields FROM documents WHERE id=? AND invoice_id=?", (document_id, invoice_id)).fetchone()
                if not document:
                    raise DomainError("document_not_found", "Document was not found on this invoice.", 404)
                fields = validate_fields(parameters.get("fields"))
                note = text(parameters.get("note", ""), "Review note", 500, optional=True)
                suggestions = json.loads(document["suggested_fields"])
                corrections = [field for field, value in suggestions.items() if value and fields.get(field) != value]
                if corrections and len(note) < 10:
                    raise DomainError("correction_note_required", "Explain changes to extracted fields in a note of 10–500 characters.")
                evidence = review_evidence(fields, json.loads(document["pages"]))
                connection.execute("UPDATE documents SET fields=?,review_evidence=?,reviewed=1,reviewed_by=?,reviewed_at=?,review_note=? WHERE id=?", (packed(fields), packed(evidence), actor, stamp(), note, document_id))
                audit_data = {"document_id": document_id, "original_suggestions": suggestions, "fields": fields, "source_evidence": evidence, "note": note}
            elif action == "propose_reference":
                po = text(parameters.get("po_reference"), "Proposed PO reference", 80)
                note = text(parameters.get("note"), "Correction note", 500)
                if not any(c.isalnum() for c in po) or len(note) < 10:
                    raise DomainError("invalid_proposal", "Provide a PO reference and a correction note of 10–500 characters.")
                connection.execute("UPDATE invoices SET proposed_po_reference=? WHERE id=?", (po, invoice_id))
                audit_data = {"source_po_reference": invoice["po_reference"], "proposed_po_reference": po, "note": note}
            elif action == "record_reply":
                reply = parameters.get("text")
                if not isinstance(reply, str) or not reply.strip() or len(reply) > 5000 or "\x00" in reply:
                    raise DomainError("invalid_reply", "Reply text must contain 1–5000 characters.")
                reply_id = str(uuid.uuid4())
                connection.execute("INSERT INTO replies(id,invoice_id,text,received_at) VALUES(?,?,?,?)", (reply_id, invoice_id, reply.strip(), stamp()))
                audit_data = {"reply_id": reply_id, "text": reply.strip(), "source": "manual"}
            elif action == "approve_resolution":
                note = text(parameters.get("note", ""), "Approval note", 500, optional=True)
                audit_data = self._prepare_resolution(connection, invoice, actor, note)
            elif action == "reopen":
                note = text(parameters.get("note"), "Reopen note", 500)
                active = connection.execute("SELECT id FROM resolutions WHERE invoice_id=? AND active=1", (invoice_id,)).fetchone()
                if not active:
                    raise DomainError("not_prepared", "Only a prepared resolution can be reopened.", 409)
                connection.execute("UPDATE resolutions SET active=0 WHERE id=?", (active["id"],))
                audit_data = {"resolution_id": active["id"], "note": note}
            else:
                raise DomainError("invalid_action", "This invoice action is not supported.")
            connection.execute("UPDATE invoices SET version=version+1 WHERE id=?", (invoice_id,))
            self._audit(connection, invoice_id, action, actor, audit_data)
            result = self._detail(connection, invoice_id)
            self._store_mutation(connection, invoice_id, body, fingerprint, result)
            return {"invoice": result, "duplicate": False}

    def get_request(self, invoice_id):
        return self.get_invoice(invoice_id)["request"]

    def handoff(self, invoice_id):
        """Read-only, approval-bound bundle; never re-evaluate or replace its evidence."""
        with self._connection() as connection:
            connection.execute("BEGIN")
            invoice = self._invoice(connection, invoice_id)
            row = connection.execute("SELECT data FROM resolutions WHERE invoice_id=? AND active=1", (invoice_id,)).fetchone()
            if not row:
                raise DomainError("not_prepared", "Approve the billing handoff before downloading it.", 409)
            resolution = json.loads(row["data"])
            document = connection.execute("SELECT invoice_id,content,mime,filename,sha256 FROM documents WHERE id=?", (resolution["document_id"],)).fetchone()
            if (not document or document["invoice_id"] != invoice_id or
                    document["sha256"] != resolution["document_sha256"] or
                    hashlib.sha256(document["content"]).hexdigest() != resolution["document_sha256"]):
                raise DomainError("evidence_unavailable", "The approved document is missing or damaged. Check the stored evidence before recovery.", 409)
            # Compatibility for existing approvals: source invoice fields cannot be edited.
            snapshot = dict(resolution)
            for name in ("invoice_number", "customer", "customer_email"):
                snapshot.setdefault(name, invoice[name])
            snapshot.setdefault("invoice_id", invoice_id)
            snapshot.setdefault("source_provenance", json.loads(invoice["provenance"]))
            snapshot.setdefault("document_filename", document["filename"])
            snapshot.setdefault("billing_instruction", billing_instruction(snapshot["source_po_reference"], snapshot["po_reference"]))
            attachment = "purchase-order.pdf" if document["mime"] == "application/pdf" else "purchase-order.txt"
            provenance = snapshot["source_provenance"]
            fixture = provenance.get("intake") == "fixed_demo_seed" or bool(provenance.get("fixture") and provenance.get("disclosure"))
            lines = ["InvoiceClear — billing handoff", "",
                     f"Invoice: {snapshot['invoice_number']}", f"Customer: {snapshot['customer']}",
                     f"Amount: {decimal_amount(snapshot['amount_cents'])} {snapshot['currency']}",
                     f"Source record: {snapshot['source_ref']}", f"Original PO reference: {snapshot['source_po_reference'] or '(missing)'}",
                     f"Approved PO reference: {snapshot['po_reference']}", "", "NEXT BILLING ACTION", snapshot["billing_instruction"], ""]
            if snapshot.get("correction_note"):
                lines += ["Correction reason: " + snapshot["correction_note"]]
            if snapshot.get("note"):
                lines += ["Approval note: " + snapshot["note"]]
            lines += [f"Approved by: {snapshot['prepared_by']}", f"Approved at: {snapshot['prepared_at']}",
                      f"Attachment: {attachment} (original filename: {snapshot['document_filename']})",
                      f"Document SHA-256: {snapshot['document_sha256']}", "", snapshot["scope"]]
            if fixture:
                lines += ["Example workflow fixture; not a client outcome."]
            content = bytes(document["content"])
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("billing-note.txt", "\n".join(lines) + "\n")
            archive.writestr("billing-brief.html", billing_brief(snapshot, attachment))
            archive.writestr("resolution.json", json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
            line_detail = billing_lines_csv(snapshot)
            if line_detail is not None:
                archive.writestr("billing-lines.csv", line_detail)
            archive.writestr(attachment, content)
        filename = re.sub(r"[^A-Za-z0-9._-]", "_", snapshot["invoice_number"]) + "-billing-handoff.zip"
        return output.getvalue(), filename

    def handoff_brief(self, invoice_id):
        bundle, filename = self.handoff(invoice_id)
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            snapshot = json.loads(archive.read("resolution.json"))
            attachment = "purchase-order.pdf" if "purchase-order.pdf" in archive.namelist() else "purchase-order.txt"
            brief = billing_brief(snapshot, attachment, linked_attachment=False).encode("utf-8")
        return brief, filename.removesuffix("-billing-handoff.zip") + "-billing-brief.html"

    def document_blob(self, document_id):
        with self._connection() as connection:
            row = connection.execute("SELECT content,filename,mime FROM documents WHERE id=?", (document_id,)).fetchone()
            if not row:
                raise DomainError("document_not_found", "Document was not found.", 404)
            return bytes(row["content"]), row["filename"], row["mime"]

    def export_csv(self):
        columns = ("invoice_number", "customer", "amount", "currency", "source_po_reference", "prepared_po_reference", "prepared_at", "prepared_by", "source_ref", "payment_status", "internal_status", "document_filename", "document_sha256", "source_intake", "source_fixture", "source_disclosure", "business_account", "service_branch", "department", "service_period", "payment_terms_days", "billing_lines", "approval_id", "customer_email")
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(columns)
        with self._connection() as connection:
            rows = connection.execute("SELECT invoices.*,resolutions.data,documents.filename,documents.sha256 FROM resolutions JOIN invoices ON invoices.id=resolutions.invoice_id JOIN documents ON documents.id=json_extract(resolutions.data,'$.document_id') WHERE resolutions.active=1 ORDER BY invoices.invoice_number").fetchall()
            for row in rows:
                resolution = json.loads(row["data"])
                provenance = resolution.get("source_provenance", json.loads(row["provenance"]))
                provenance = provenance if isinstance(provenance, dict) else {}
                values = (resolution.get("invoice_number", row["invoice_number"]), resolution.get("customer", row["customer"]),
                          decimal_amount(resolution.get("amount_cents", row["amount_cents"])), resolution.get("currency", row["currency"]),
                          resolution.get("source_po_reference", row["po_reference"]), resolution["po_reference"], resolution["prepared_at"],
                          resolution["prepared_by"], resolution.get("source_ref", row["source_ref"]), "unpaid", "prepared",
                          resolution.get("document_filename", row["filename"]), resolution["document_sha256"],
                          provenance.get("intake", ""), provenance.get("fixture", ""), provenance.get("disclosure", ""),
                          provenance.get("account_code", ""), provenance.get("branch", ""), provenance.get("department", ""),
                          provenance.get("service_period", ""), provenance.get("terms_days", ""),
                          json.dumps(provenance["billing_lines"], ensure_ascii=False) if provenance.get("billing_lines") else "",
                          resolution["id"], resolution.get("customer_email", row["customer_email"]))
                writer.writerow([spreadsheet_cell(value) for value in values])
        return output.getvalue()

    def health(self):
        result = {"status": "ok", "provider": self.provider, "mode": "demo" if self.demo else "private"}
        error = None
        if not self.database_path.is_file():
            error = "Workspace database is unavailable. Restore persistent storage."
        else:
            try:
                connection = sqlite3.connect(self.database_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
                try:
                    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
                    required = {"invoices", "documents", "replies", "resolutions", "audit", "idempotency", "provider_usage", "agent_runs"}
                    if not required.issubset(tables):
                        error = "Workspace database schema is incomplete. Check the stored database before recovery."
                    else:
                        connection.execute("SELECT id,invoice_id,idempotency_key,source_version,source_fingerprint,source_snapshot,status,created_at,completed_at,lease_until,model,summary,next_step,draft,citations,trace,error FROM agent_runs LIMIT 0")
                finally:
                    connection.close()
            except (OSError, sqlite3.Error):
                error = "Workspace database could not be read. Check persistent storage and recovery."
        if error:
            result.update(status="degraded", database={"status": "degraded", "error": error})
        else:
            result["database"] = {"status": "ok"}
        return result

    def _seed(self, connection):
        # A pack revision is not a migration. Never add, replace or repair seed
        # records in an occupied public database, including a legacy six-case DB.
        if any(connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() for table in PUBLIC_TABLES):
            return
        cases = public_cases()
        for case in cases:
            invoice_id = case["id"]
            provenance = case_provenance(case)
            self._insert(connection, self._entry(case["entry"]), "public-fixture-seed", invoice_id=invoice_id, provenance=provenance)
            for index, document in enumerate(case["documents"], 1):
                filename, content, mime, pages = document_source(case, document)
                suggestions, evidence, error = rules_extract(pages)
                # Reviewed fixture fields still require real source support.
                fields = validate_fields(suggestions) if document["reviewed"] else {}
                confirmed = review_evidence(fields, pages) if fields else {}
                document_id = f"demo-document-{case['entry']['invoice_number'].rsplit('-', 1)[-1]}-r{index}"
                connection.execute("INSERT INTO documents(id,invoice_id,filename,sha256,mime,content,pages,suggested_fields,fields,evidence,review_evidence,reviewed,extraction_method,extraction_error,uploaded_at,reviewed_by,reviewed_at,review_note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (document_id, invoice_id, filename, hashlib.sha256(content).hexdigest(), mime, content, packed(pages), packed(suggestions), packed(fields), packed(evidence), packed(confirmed), int(document["reviewed"]), "rules", error, SEED_TIME, REVIEWER if fields else None, SEED_TIME if fields else None, "Checked legal buyer, reference, currency and authorized ceiling against the retained source pages." if fields else None))
                self._audit(connection, invoice_id, "demo_document_seeded", "public-fixture-seed", {"document_id": document_id, "reviewed": bool(fields), "fixture": FIXTURE, "original_suggestions": suggestions, "source_evidence": confirmed})
            for index, reply in enumerate(case["replies"], 1):
                reply_id = f"public-reply-{case['entry']['invoice_number']}-{index}"
                reply_text = f"From: {case['contact']} <{case['entry']['customer_email']}>\nSubject: PO review / {case['entry']['invoice_number']}\n\n{reply}"
                connection.execute("INSERT INTO replies(id,invoice_id,text,received_at,source) VALUES(?,?,?,?,?)", (reply_id, invoice_id, reply_text, SEED_TIME, "manual"))
                self._audit(connection, invoice_id, "record_reply", "public-fixture-seed", {"reply_id": reply_id, "source": "manual", "fixture": FIXTURE})
            if case["proposal"]:
                connection.execute("UPDATE invoices SET proposed_po_reference=? WHERE id=?", (case["proposal"], invoice_id))
                self._audit(connection, invoice_id, "propose_reference", REVIEWER, {"source_po_reference": case["entry"]["po_reference"], "proposed_po_reference": case["proposal"], "note": "Retained current PO and customer correspondence support this reference correction.", "fixture": FIXTURE})
        for case in cases:
            if case["prepared"]:
                resolution = self._prepare_resolution(connection, self._invoice(connection, case["id"]), REVIEWER, "Internal allocation reserved against this PO. External consumption remains unverified.")
                self._audit(connection, case["id"], "approve_resolution", REVIEWER, resolution)
