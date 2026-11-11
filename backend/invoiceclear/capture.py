"""Opt-in, local-workspace inbox intake from confirmed request threads only."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
from contextlib import contextmanager

from .service import DomainError, packed, stamp


SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox_capture (
 id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
 binding TEXT NOT NULL DEFAULT '', cursor TEXT NOT NULL DEFAULT '',
 last_run_at TEXT, last_error TEXT, checked INTEGER NOT NULL DEFAULT 0,
 replies_added INTEGER NOT NULL DEFAULT 0, documents_added INTEGER NOT NULL DEFAULT 0,
 pending_cases INTEGER NOT NULL DEFAULT 0, failures TEXT NOT NULL DEFAULT '[]');
INSERT OR IGNORE INTO inbox_capture(id) VALUES(1);
"""


class InboxCapture:
    interval_seconds = 60
    batch_size = 10

    def __init__(self, service, connected, settings, fingerprint):
        self.service, self._connected, self.settings = service, connected, settings
        self.fingerprint = fingerprint
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._pass_lock = threading.Lock()
        self._running = False
        if self._private:
            with service._connection() as connection:
                connection.executescript(SCHEMA)

    @property
    def _private(self):
        return self.settings.mode == "private" and not self.service.demo

    @property
    def connected(self):
        return self._connected() if callable(self._connected) else self._connected

    def _empty(self):
        return {"enabled": False, "available": False, "running": False,
                "interval_seconds": self.interval_seconds, "batch_size": self.batch_size,
                "last_run_at": None, "last_error": None, "checked": 0,
                "replies_added": 0, "documents_added": 0, "pending_cases": 0, "failures": []}

    def _binding(self):
        # Connections.status reads only local encrypted grants; it never refreshes tokens.
        accounts = self.connected.connections.status()
        qbo, gmail = accounts.get("qbo", {}), accounts.get("gmail", {})
        if not all(item.get("connected") and item.get("account_id") for item in (qbo, gmail)):
            return None
        company, environment, mailbox = str(qbo["account_id"]), qbo.get("environment"), gmail["account_id"].lower()
        if environment not in {"sandbox", "production"}:
            return None
        # A changed extraction configuration requires fresh opt-in before background
        # intake can send any new attachment to the selected extractor.
        config = [self.fingerprint, company, environment, mailbox, qbo.get("grant_id", ""), gmail.get("grant_id", ""), self.service.provider,
                  self.service.together_model, self.service.together_key or ""]
        digest = hashlib.sha256(packed(config).encode()).hexdigest()
        return digest, company, environment, mailbox

    def _validated(self):
        try:
            binding = self._binding()
        except Exception:
            binding = None
        with self.service._write() as connection:
            row = connection.execute("SELECT * FROM inbox_capture WHERE id=1").fetchone()
            if row["enabled"] and (not binding or row["binding"] != binding[0]):
                connection.execute("UPDATE inbox_capture SET enabled=0,last_error=? WHERE id=1",
                                   ("Capture stopped because the workspace, connected accounts or extraction settings changed. Enable it again after reviewing the connections.",))
                row = connection.execute("SELECT * FROM inbox_capture WHERE id=1").fetchone()
        return dict(row), binding

    def status(self):
        result = self._empty()
        if not self._private:
            return result
        row, binding = self._validated()
        result.update({key: row[key] for key in ("last_run_at", "last_error", "checked", "replies_added", "documents_added", "pending_cases")})
        result.update(enabled=bool(row["enabled"]), available=bool(binding), running=self._running,
                      failures=json.loads(row["failures"]))
        return result

    def set_enabled(self, enabled):
        if not self._private:
            raise DomainError("capture_private_only", "Inbox capture is available in a private workspace only.", 403)
        if type(enabled) is not bool:
            raise DomainError("invalid_capture", "Choose whether inbox capture is enabled.")
        binding = self._binding() if enabled else None
        if enabled and not binding:
            raise DomainError("connection_required", "Connect the original QuickBooks company and Gmail mailbox before enabling inbox capture.", 409)
        with self.service._write() as connection:
            connection.execute("UPDATE inbox_capture SET enabled=?,binding=?,last_error=NULL WHERE id=1",
                               (int(enabled), binding[0] if binding else ""))
        self._wake.set()
        return self.status()

    def start(self):
        if not self._private or (self._thread and self._thread.is_alive()) or self._stop.is_set():
            return
        self._thread = threading.Thread(target=self._loop, name="invoiceclear-inbox-capture", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                # Stored errors are fixed application text, never exception/provider bodies.
                try:
                    with self.service._write() as connection:
                        connection.execute("UPDATE inbox_capture SET last_error=? WHERE id=1",
                                           ("Inbox capture could not check this pass. It will retry after the normal interval.",))
                except Exception:
                    pass
            self._wake.wait(self.interval_seconds)
            self._wake.clear()

    @contextmanager
    def _claim(self):
        # An OS-held lock survives long provider calls and is released on process
        # exit. A lease timeout alone could overlap a slow pass during restart.
        path = str(self.service.database_path.resolve()) + ".capture.lock"
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        claimed = False
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                claimed = True
            except BlockingIOError:
                pass
            yield claimed
        finally:
            if claimed:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _cases(self, connection, binding, cursor):
        _, company, environment, mailbox = binding
        base = """FROM invoices i JOIN external_invoices e ON e.invoice_id=i.id
          WHERE e.company_id=? AND e.environment=?
          AND NOT EXISTS(SELECT 1 FROM resolutions r WHERE r.invoice_id=i.id AND r.active=1)
          AND EXISTS(SELECT 1 FROM external_actions a WHERE a.invoice_id=i.id
            AND a.kind='send' AND a.state='confirmed' AND a.company_id=e.company_id
            AND a.environment=e.environment AND a.mailbox=?)"""
        args = (company, environment, mailbox)
        pending = connection.execute("SELECT COUNT(*) " + base + " AND EXISTS(SELECT 1 FROM external_actions p WHERE p.invoice_id=i.id AND p.state IN ('pending','unknown'))", args).fetchone()[0]
        rows = connection.execute("SELECT i.id,i.invoice_number " + base + " AND NOT EXISTS(SELECT 1 FROM external_actions p WHERE p.invoice_id=i.id AND p.state IN ('pending','unknown')) ORDER BY CASE WHEN i.id>? THEN 0 ELSE 1 END,i.id LIMIT ?", (*args, cursor, self.batch_size)).fetchall()
        return rows, pending

    def run_once(self):
        if not self._private or self._stop.is_set():
            return self._empty()
        if not self._pass_lock.acquire(blocking=False):
            return self.status()
        try:
            with self._claim() as claimed:
                if not claimed:
                    return self.status()
                row, binding = self._validated()
                if not row["enabled"] or not binding:
                    return self.status()
                self._running = True
                checked = replies = documents = 0
                failures = []
                with self.service._write() as connection:
                    cases, pending = self._cases(connection, binding, row["cursor"])
                    # Advance before dispatch so a failing/crashed case cannot starve
                    # later cases; a subsequent rotation safely retries its intake.
                    if cases:
                        connection.execute("UPDATE inbox_capture SET cursor=? WHERE id=1", (cases[-1]["id"],))
                for case in cases:
                    if self._stop.is_set():
                        break
                    current, current_binding = self._validated()
                    if not current["enabled"] or not current_binding or current_binding[0] != binding[0]:
                        break
                    checked += 1
                    try:
                        result = self.connected.sync_replies(case["id"], open_only=True, expected_accounts=binding[1:])
                        replies += result["added"]
                        documents += result["documents_added"]
                        if result.get("failures"):
                            failures.append({"invoice_id": case["id"], "invoice_number": case["invoice_number"],
                                             "error": "Some replies or attachments could not be captured. Open the case to review the intake hold."})
                    except DomainError as error:
                        message = "This case is held for review. Capture will check it in a later pass."
                        if error.code == "external_action_pending":
                            pending += 1
                            message = "A pending external action must be reconciled before reply intake."
                        failures.append({"invoice_id": case["id"], "invoice_number": case["invoice_number"], "error": message})
                    except Exception:
                        failures.append({"invoice_id": case["id"], "invoice_number": case["invoice_number"],
                                         "error": "Reply capture failed for this case. It will retry in a later pass."})
                with self.service._write() as connection:
                    connection.execute("UPDATE inbox_capture SET last_run_at=?,last_error=?,checked=?,replies_added=?,documents_added=?,pending_cases=?,failures=? WHERE id=1",
                                       (stamp(), "Some cases need attention before all replies can be captured." if failures else None,
                                        checked, replies, documents, pending, packed(failures)))
        finally:
            self._running = False
            self._pass_lock.release()
        return self.status()
