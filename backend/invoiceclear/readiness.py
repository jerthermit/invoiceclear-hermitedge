"""Safe setup status and explicitly requested, read-only provider probes."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from .documents import DomainError, MAX_DOCUMENT_BYTES, MAX_TEXT


CHECK_COOLDOWN_SECONDS = 60
DAILY_CALL_LIMIT = 100
DAILY_TOKEN_LIMIT = 500_000
REQUIRED_ENVIRONMENT = {
    "qbo": ("INVOICECLEAR_MODE", "INVOICECLEAR_PUBLIC_URL", "INVOICECLEAR_FRONTEND_URL", "INVOICECLEAR_TOKEN_KEY", "QBO_CLIENT_ID", "QBO_CLIENT_SECRET"),
    "gmail": ("INVOICECLEAR_MODE", "INVOICECLEAR_PUBLIC_URL", "INVOICECLEAR_FRONTEND_URL", "INVOICECLEAR_TOKEN_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"),
    "together": ("INVOICECLEAR_MODE", "INVOICECLEAR_PROVIDER", "TOGETHER_API_KEY"),
}


def checked_time():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Readiness:
    """Checks are process-local observations, never durable authorization."""

    def __init__(self, server):
        self.server = server
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._results = {}
        self._attempts = {}

    def _missing(self, provider):
        settings = self.server.settings
        values = {
            "INVOICECLEAR_MODE": settings.mode == "private",
            "INVOICECLEAR_PUBLIC_URL": settings.public_url,
            "INVOICECLEAR_FRONTEND_URL": settings.frontend_url,
            "INVOICECLEAR_TOKEN_KEY": settings.token_key,
            "QBO_CLIENT_ID": settings.qbo_client_id,
            "QBO_CLIENT_SECRET": settings.qbo_client_secret,
            "GOOGLE_CLIENT_ID": settings.google_client_id,
            "GOOGLE_CLIENT_SECRET": settings.google_client_secret,
            "INVOICECLEAR_PROVIDER": settings.provider == "together",
            "TOGETHER_API_KEY": settings.together_key,
        }
        return [name for name in REQUIRED_ENVIRONMENT[provider] if not values[name]]

    def _identity(self, provider, grant=None):
        settings = self.server.settings
        if provider == "together":
            if self._missing(provider):
                return None
            parts = [settings.mode, settings.provider, settings.together_key, settings.together_model,
                     self.server.sessions.fingerprint]
        else:
            connections = self.server.connections
            if grant is None:
                with connections._lock(provider):
                    grant = connections._load(provider)
            if not grant or (grant.get("refresh_expires") and grant["refresh_expires"] <= time.time()):
                return None
            parts = [connections._context(provider), grant["account_id"], grant["refresh_token"]]
        return hashlib.sha256(json.dumps(parts, ensure_ascii=True).encode()).hexdigest()

    def _verification(self, provider, identity):
        with self._state_lock:
            result = self._results.get(provider)
        if identity and result and result[0] == identity:
            return dict(result[1])
        return {"status": "not_checked", "checked_at": None,
                "message": "Run an explicit check after configuring and connecting this provider."}

    def _usage(self):
        day = datetime.now(timezone.utc).date().isoformat()
        result = {"day": day, "reset_timezone": "UTC", "calls": 0, "tokens": 0,
                  "call_limit": DAILY_CALL_LIMIT, "token_limit": DAILY_TOKEN_LIMIT, "status": "ok"}
        try:
            with self.server.service._connection() as connection:
                row = connection.execute("SELECT calls,tokens FROM provider_usage WHERE day=?", (day,)).fetchone()
                if row:
                    result.update(calls=row["calls"], tokens=row["tokens"])
        except Exception:
            result.update(status="unavailable", calls=None, tokens=None)
        return result

    def _provider(self, provider, statuses=None):
        settings = self.server.settings
        if provider == "together":
            item = {"configured": bool(settings.provider == "together" and settings.together_key), "connected": False,
                    "selected": settings.provider == "together", "model": settings.together_model,
                    "callback_url": None}
        else:
            item = dict((statuses or self._connection_statuses())[provider])
            item["callback_url"] = settings.public_url + "/api/connections/" + provider + "/callback" if settings.public_url else None
        try:
            identity = self._identity(provider)
        except (DomainError, OSError, sqlite3.Error):
            identity = None
        item.update(required_environment=list(REQUIRED_ENVIRONMENT[provider]),
                    missing_environment=self._missing(provider), verification=self._verification(provider, identity))
        if provider == "together":
            item["connected"] = item["verification"]["status"] == "passed"
        return item

    def _connection_statuses(self):
        try:
            return self.server.connections.status()
        except (DomainError, OSError, sqlite3.Error):
            result = {provider: {"configured": not self._missing(provider), "connected": False,
                                 "account": "", "account_id": "", "error": "Stored connection status is unavailable."}
                      for provider in ("qbo", "gmail")}
            result["qbo"].update(environment=self.server.settings.qbo_environment,
                                 po_field_configured=bool(self.server.settings.qbo_po_field_id))
            return result

    def snapshot(self):
        health = self.server.health()
        statuses = self._connection_statuses()
        return {
            "mode": self.server.settings.mode,
            "external_checks_allowed": self.server.settings.mode == "private",
            "verification_scope": "process",
            "capabilities": {"manual_invoice_entry": True, "csv_import": True, "pdf_txt_upload": True,
                             "human_review_required": True, "ocr": False, "payments": False,
                             "email_send_requires_approval": True, "qbo_reference_update_requires_approval": True},
            "providers": {provider: self._provider(provider, statuses) for provider in REQUIRED_ENVIRONMENT},
            "storage": {"database": health["database"], "backups": health["backups"],
                        "backup_scope": "Local snapshots only. Persistent storage and off-host recovery are operator-managed."},
            "limits": {"document_bytes": MAX_DOCUMENT_BYTES, "document_text_characters": MAX_TEXT,
                       "pdf_pages": 20, "model_input_characters": 24_000, "model_output_tokens": 1200,
                       "model_timeout_seconds": 12, "check_cooldown_seconds": CHECK_COOLDOWN_SECONDS},
            "usage": self._usage(),
            "investigation": {"enabled": self.server.settings.agent_enabled,
                              "available": self.server.settings.agent_enabled and bool(self.server.settings.together_key),
                              "model": self.server.settings.together_model if self.server.settings.agent_enabled else None,
                              "read_only": True, "max_rounds": 5, "timeout_seconds": 20},
        }

    def _record(self, provider, identity, status, message):
        outcome = {"status": status, "checked_at": checked_time(), "message": message}
        with self._state_lock:
            self._results[provider] = (identity, outcome)

    def _together_probe(self):
        # Service owns the fixed non-customer source, existing extraction
        # validation and the same durable usage reservation as document uploads.
        result = self.server.service.probe_extraction()
        if result.get("error", "") and str(result["error"]).startswith("Daily extraction limit"):
            raise DomainError("provider_usage_limit", "Daily extraction limit reached. Manual document review remains available.", 429)
        expected = {"po_number": "IC-CONNECTION-CHECK", "customer": "InvoiceClear Connection Check", "currency": "USD", "amount": "1.00"}
        if (result.get("error") or result.get("fields") != expected
                or not isinstance(result.get("evidence"), dict) or set(result["evidence"]) != set(expected)):
            raise DomainError("verification_failed", "The extraction check failed or returned unsupported evidence. Review the backend model configuration.", 502)

    def _oauth_probe(self, provider):
        connections = self.server.connections
        connections._require(provider)
        # Hold the same grant lock used at dispatch and disconnect. This also
        # binds the outcome to the actual refreshed account/configuration.
        with connections._lock(provider):
            grant = connections._load(provider)
            if not grant:
                raise DomainError("connection_required", "Connect this provider before checking access.", 409)
            if grant.get("refresh_expires") and grant["refresh_expires"] <= time.time():
                raise DomainError("connection_expired", "Provider access expired. Connect again.", 409)
            account = grant["account_id"]
            if provider == "qbo":
                payload = connections.qbo("GET", "query?" + urlencode({"query": "SELECT Id FROM Invoice MAXRESULTS 1"}), expected_account=account)
                response = payload.get("QueryResponse")
                invoices = response.get("Invoice", []) if isinstance(response, dict) else None
                if (not isinstance(response, dict) or not isinstance(invoices, list) or len(invoices) > 1
                        or any(not isinstance(invoice, dict) or not str(invoice.get("Id", "")).isdigit() for invoice in invoices)):
                    raise DomainError("verification_failed", "QuickBooks did not confirm invoice read access. Review the connection.", 502)
            else:
                payload = connections.gmail("GET", "profile", expected_account=account)
                actual = payload.get("emailAddress")
                if not isinstance(actual, str) or actual.casefold() != account.casefold():
                    raise DomainError("account_changed", "The mailbox no longer matches this connection. Connect the intended account again.", 409)
            return self._identity(provider)

    def verify(self, provider):
        if provider not in REQUIRED_ENVIRONMENT:
            raise DomainError("invalid_provider", "Choose QuickBooks, Gmail or Together.")
        if self.server.settings.mode != "private":
            raise DomainError("verification_private_only", "External checks are available only in a private workspace.", 403)
        if self._missing(provider):
            raise DomainError("connection_unconfigured", "Configure this provider in the backend before checking access.", 409)
        # One external check at a time, with a per-provider cooldown even after
        # failure. A copied public session cannot turn this into a probe relay.
        if not self._lock.acquire(blocking=False):
            raise DomainError("verification_busy", "Another connection check is running. Wait for it to finish.", 429)
        try:
            now = time.monotonic()
            if now - self._attempts.get(provider, -CHECK_COOLDOWN_SECONDS) < CHECK_COOLDOWN_SECONDS:
                raise DomainError("verification_cooldown", "Wait one minute before checking this provider again.", 429)
            identity = self._identity(provider)
            if not identity:
                raise DomainError("connection_required", "Connect this provider before checking access.", 409)
            self._attempts[provider] = now
            try:
                if provider == "together":
                    self._together_probe()
                    identity = self._identity(provider)
                else:
                    identity = self._oauth_probe(provider)
            except Exception as exc:
                self._record(provider, identity, "failed", "Access check failed. Review the connection and try again.")
                if isinstance(exc, DomainError) and exc.code in {"provider_usage_limit", "provider_rate_limit", "account_changed", "connection_required", "connection_expired"}:
                    raise
                raise DomainError("verification_failed", "Access check failed. Review the backend configuration or reconnect the provider.", 502) from None
            message = "Source-grounded extraction check passed. No invoice or approval was created." if provider == "together" else "Read-only access check passed. No invoice, email or accounting record was changed."
            self._record(provider, identity, "passed", message)
            return dict(provider=provider, **self._provider(provider))
        finally:
            self._lock.release()
