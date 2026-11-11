from __future__ import annotations

import json
import mimetypes
import re
import sqlite3
import threading
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .config import Settings
from .connections import Connections
from .connected import ConnectedWorkflow
from .capture import InboxCapture
from .operations import Maintenance
from .readiness import Readiness
from .investigator import Investigator
from .public_files import source_files
from .service import DomainError, Service
from .sessions import Sessions


MAX_BODY = 7_500_000
ID = r"([A-Za-z0-9_-]{1,100})"


class InvoiceHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # Python 3.11's default backlog is five; the bounded release exercise uses
    # eight simultaneous clients. Queue that burst without resetting connections.
    request_queue_size = 32

    def __init__(self, settings: Settings):
        if settings.mode not in {"demo", "private"}:
            raise ValueError("Unknown workspace mode.")
        if settings.mode == "private" and (settings.email == "finance@invoiceclear.demo" or settings.password == "invoiceclear-demo" or len(settings.password) < 16):
            raise ValueError("Private workspaces require unique access credentials.")
        self.settings = settings
        self.service = Service(settings.database_path, demo=settings.mode == "demo", provider=settings.provider,
                               together_key=settings.together_key, together_model=settings.together_model)
        self.sessions = Sessions(settings.database_path, settings.email, settings.password, settings.ttl_seconds)
        self.connections = Connections(settings, fingerprint=self.sessions.fingerprint)
        self.connected = ConnectedWorkflow(self.service, self.connections, settings)
        self.investigator = Investigator(self.service, settings, lambda invoice_id: self.connected.get_detail(invoice_id))
        self.login_failures: dict[str, tuple[int, float]] = {}
        self.login_lock = threading.Lock()
        super().__init__((settings.host, settings.port), InvoiceHandler)
        self.maintenance = Maintenance(settings.database_path, settings.backups_enabled)
        self.maintenance.start()
        self.readiness = Readiness(self)
        self.capture = InboxCapture(self.service, lambda: self.connected, settings, self.sessions.fingerprint)
        self.capture.start()

    def health(self):
        result = self.service.health()
        if result["status"] == "ok":
            try:
                uri = self.settings.database_path.resolve().as_uri() + "?mode=ro"
                with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
                    identity = connection.execute("SELECT mode FROM workspace_identity WHERE id=1").fetchone()
                    if not identity or identity[0] != self.settings.mode:
                        raise sqlite3.DatabaseError("Workspace identity mismatch")
                    connection.execute("SELECT token_hash,fingerprint,expires FROM access_sessions LIMIT 0")
                    connection.execute("SELECT context,encrypted FROM connection_grants LIMIT 0")
                    connection.execute("SELECT state_hash,expires FROM connection_states LIMIT 0")
                    connection.execute("SELECT invoice_id,accounting FROM external_invoices LIMIT 0")
                    connection.execute("SELECT invoice_id,state FROM external_actions LIMIT 0")
                    connection.execute("SELECT invoice_id,metadata FROM external_messages LIMIT 0")
                    connection.execute("SELECT invoice_id,mailbox,scope,cursor FROM external_intake_progress LIMIT 0")
                    if self.settings.mode == "private":
                        connection.execute("SELECT enabled,binding,cursor FROM inbox_capture WHERE id=1")
            except (OSError, sqlite3.Error):
                result.update(status="degraded", database={
                    "status": "degraded", "error": "Workspace access schema is unavailable. Check the stored database before recovery.",
                })
        state = self.maintenance.status()
        result["backups"] = {
            "enabled": state["enabled"],
            "status": "disabled" if not state["enabled"] else "degraded" if state["error"] else "ok" if state["last_backup"] else "pending",
        }
        if state["error"]:
            result["status"] = "degraded"
            result["backups"]["error"] = state["error"]
        return result

    def server_close(self):
        if hasattr(self, "capture"):
            self.capture.close()
        if hasattr(self, "maintenance"):
            self.maintenance.close()
        super().server_close()


class InvoiceHandler(BaseHTTPRequestHandler):
    server: InvoiceHTTPServer
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, format, *args):
        # Never log financial text, credentials, tokens or uploaded filenames.
        return

    def _origin_allowed(self, origin: str) -> bool:
        if origin in self.server.settings.allowed_origins:
            return True
        try:
            parsed = urlsplit(origin)
            host = self.headers.get("Host", "")
            request_host = urlsplit("http://" + host)
            return (parsed.scheme in {"http", "https"} and parsed.netloc == request_host.netloc
                    and request_host.hostname in {"127.0.0.1", "localhost", "[::1]", "::1"}
                    and not parsed.username and not parsed.password and not parsed.path and not parsed.query)
        except ValueError:
            return False

    def _check_origin(self):
        origin = self.headers.get("Origin")
        if origin and not self._origin_allowed(origin):
            raise DomainError("origin_forbidden", "This application origin is not allowed.", status=403)

    def _token(self) -> str:
        header = self.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        if not self.server.sessions.valid(token):
            raise DomainError("unauthorized", "Sign in to continue.", status=401)
        return token

    def _body(self, maximum=MAX_BODY) -> dict:
        if self.headers.get("Transfer-Encoding"):
            raise DomainError("invalid_body", "Use a complete JSON request.", status=400)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise DomainError("invalid_body", "Request length is invalid.")
        if length <= 0:
            raise DomainError("invalid_body", "A JSON request is required.")
        if length > maximum:
            raise DomainError("request_too_large", "This upload is too large.", status=413)
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            raise DomainError("unsupported_media", "Use JSON for this request.", status=415)
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise DomainError("invalid_body", "The upload was interrupted.")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DomainError("invalid_json", "This request could not be read.")
        if not isinstance(data, dict):
            raise DomainError("invalid_body", "A JSON object is required.")
        return data

    def _send(self, data: bytes, content_type: str, status=200, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        origin = self.headers.get("Origin")
        if origin and self._origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        if filename:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(str(filename), safe=""))
        if status >= 400:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, payload, status=200):
        self._send(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def _error(self, error):
        self._json({"error": {"code": error.code, "message": error.message}}, error.status)

    def _invoice_detail(self, invoice_id):
        detail = self.server.service.get_invoice(invoice_id)
        detail["integration"] = self.server.connected.get_detail(invoice_id)
        detail["work"] = self.server.connected.work_summary(detail)
        detail["investigation"] = self.server.investigator.snapshot(invoice_id)
        return detail

    def _approval_source(self, invoice_id):
        current = self.server.connected.get_detail(invoice_id).get("accounting")
        if not current:
            return
        self.server.connected.refresh_invoice(invoice_id)
        current = self.server.connected.get_detail(invoice_id)["accounting"]
        if current.get("error") or current.get("source_changed") or current.get("payment_status") != "unpaid":
            raise DomainError("accounting_changed", "QuickBooks has changed or could not be verified. Refresh the invoice and review the source before approval.", 409)

    def do_OPTIONS(self):
        try:
            self._check_origin()
            method = self.headers.get("Access-Control-Request-Method")
            requested = {name.strip().lower() for name in self.headers.get("Access-Control-Request-Headers", "").split(",") if name.strip()}
            if method not in {"GET", "POST"} or requested - {"authorization", "content-type"}:
                raise DomainError("preflight_forbidden", "This request is not allowed.", status=403)
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", ""))
            self.send_header("Access-Control-Allow-Methods", "GET, POST")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Vary", "Origin")
            self.end_headers()
        except DomainError as error:
            self._error(error)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == "/api/runtime":
                result = {"mode": self.server.settings.mode}
                if self.server.settings.mode == "demo":
                    result["demo_credentials"] = {"email": self.server.settings.email, "password": self.server.settings.password}
                return self._json(result)
            if path == "/api/health":
                result = self.server.health()
                return self._json(result, 200 if result.get("status") == "ok" else 503)
            match = re.fullmatch(r"/api/connections/(qbo|gmail)/callback", path)
            if match:
                if self.command == "HEAD":
                    raise DomainError("method_not_allowed", "Use the provider's authorization callback.", 405)
                query = parse_qs(parsed.query, keep_blank_values=True)
                if any(len(values) != 1 for values in query.values()):
                    raise DomainError("invalid_callback", "The connection response is invalid.")
                self.server.connections.callback(match.group(1), {key: value[0] for key, value in query.items()}, self.server.sessions.fingerprint)
                self.send_response(303)
                self.send_header("Location", self.server.settings.frontend_url or self.server.settings.public_url)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not path.startswith("/api/"):
                return self._static(path)
            self._check_origin()
            self._token()
            service = self.server.service
            if path == "/api/public/source-files":
                if self.server.settings.mode != "demo":
                    raise DomainError("public_files_only", "Source files are available in the public workspace only.", 403)
                return self._send(source_files(), "application/zip", filename="invoiceclear-source-files.zip")
            if path == "/api/connections":
                return self._json(self.server.connections.status())
            if path == "/api/readiness":
                return self._json(self.server.readiness.snapshot())
            if path == "/api/capture":
                return self._json(self.server.capture.status())
            if path == "/api/invoices":
                query = parse_qs(parsed.query)
                try:
                    offset = int(query.get("offset", ["0"])[0])
                    limit = int(query.get("limit", ["50"])[0])
                except ValueError:
                    raise DomainError("invalid_pagination", "Choose a valid page.")
                if offset < 0 or not 1 <= limit <= 100:
                    raise DomainError("invalid_pagination", "Choose a valid page.")
                result = service.list_invoices(view=query.get("view", ["open"])[0], q=query.get("q", [""])[0], offset=offset, limit=limit)
                context = self.server.connected.worklist_context(result["invoices"])
                for invoice in result["invoices"]:
                    invoice["work"] = context[invoice["id"]]
                return self._json(result)
            if path == "/api/export.csv":
                return self._send(service.export_csv().encode("utf-8-sig"), "text/csv; charset=utf-8", filename="invoiceclear-prepared.csv")
            match = re.fullmatch(r"/api/invoices/" + ID, path)
            if match:
                return self._json(self._invoice_detail(match.group(1)))
            match = re.fullmatch(r"/api/invoices/" + ID + r"/request", path)
            if match:
                return self._json(service.get_request(match.group(1)))
            match = re.fullmatch(r"/api/invoices/" + ID + r"/history", path)
            if match:
                query = parse_qs(parsed.query, keep_blank_values=True)
                try:
                    if any(len(query[name]) != 1 for name in ("limit", "before") if name in query):
                        raise ValueError()
                    limit = int(query.get("limit", ["20"])[0])
                    before = int(query["before"][0]) if "before" in query else None
                    if not 1 <= limit <= 100 or (before is not None and not 1 <= before <= 9_223_372_036_854_775_807):
                        raise ValueError()
                except ValueError:
                    raise DomainError("invalid_pagination", "Choose a valid history page.") from None
                return self._json(service.history(match.group(1), limit=limit, before=before))
            match = re.fullmatch(r"/api/invoices/" + ID + r"/investigation", path)
            if match:
                return self._json(self.server.investigator.snapshot(match.group(1)))
            match = re.fullmatch(r"/api/invoices/" + ID + r"/handoff", path)
            if match:
                content, filename = service.handoff(match.group(1))
                return self._send(content, "application/zip", filename=filename)
            match = re.fullmatch(r"/api/invoices/" + ID + r"/brief", path)
            if match:
                content, filename = service.handoff_brief(match.group(1))
                return self._send(content, "text/html; charset=utf-8", filename=filename)
            match = re.fullmatch(r"/api/documents/" + ID + r"/download", path)
            if match:
                content, filename, mime = service.document_blob(match.group(1))
                return self._send(content, mime, filename=filename)
            raise DomainError("not_found", "This page was not found.", status=404)
        except DomainError as error:
            self._error(error)
        except (ConnectionError, BrokenPipeError):
            self.close_connection = True
        except Exception:
            self._error(DomainError("internal_error", "This view could not be loaded. Try again.", status=500))

    def _login(self, data):
        peer = self.client_address[0]
        now = time.monotonic()
        with self.server.login_lock:
            self.server.login_failures = {key: value for key, value in self.server.login_failures.items() if value[1] > now}
            failures, until = self.server.login_failures.get(peer, (0, now + 300))
            if failures >= 5:
                raise DomainError("login_limited", "Wait a few minutes before trying again.", status=429)
        grant = self.server.sessions.create(data.get("email"), data.get("password"))
        if not grant:
            with self.server.login_lock:
                if len(self.server.login_failures) < 1024 or peer in self.server.login_failures:
                    self.server.login_failures[peer] = (failures + 1, until)
            raise DomainError("unauthorized", "The email or password is incorrect.", status=401)
        with self.server.login_lock:
            self.server.login_failures.pop(peer, None)
        self._json(grant)

    def do_POST(self):
        try:
            self._check_origin()
            path = urlsplit(self.path).path
            if path == "/api/session":
                return self._login(self._body(16_000))
            token = self._token()
            if path == "/api/session/logout":
                self.server.sessions.revoke(token)
                # Consume the optional body before a keep-alive connection is reused.
                if self.headers.get("Content-Length", "0") != "0":
                    self._body(16_000)
                return self._json({"signed_out": True})
            data = self._body(16_000 if re.fullmatch(r"/api/invoices/" + ID + r"/investigate", path) else MAX_BODY)
            service, actor = self.server.service, self.server.settings.email
            if path == "/api/capture":
                if set(data) != {"enabled"}:
                    raise DomainError("invalid_capture", "Provide only the inbox capture enabled setting.")
                return self._json(self.server.capture.set_enabled(data["enabled"]))
            match = re.fullmatch(r"/api/connections/(qbo|gmail|together)/verify", path)
            if match:
                return self._json(self.server.readiness.verify(match.group(1)))
            match = re.fullmatch(r"/api/connections/(qbo|gmail)/(start|disconnect)", path)
            if match:
                provider, action = match.groups()
                if action == "start":
                    return self._json(self.server.connections.start(provider, self.server.sessions.fingerprint))
                self.server.connections.disconnect(provider)
                return self._json(self.server.connections.status())
            if path == "/api/connections/qbo/sync":
                return self._json(self.server.connected.sync_invoices(offset=data.get("offset", 0)))
            match = re.fullmatch(r"/api/invoices/" + ID + r"/(send-request|sync-replies|apply-reference|refresh-accounting|reconcile-request|reconcile-reference)", path)
            if match:
                invoice_id, action = match.groups()
                if action == "send-request":
                    result = self.server.connected.send_request(invoice_id, data, actor)
                elif action == "apply-reference":
                    result = self.server.connected.apply_reference(invoice_id, data, actor)
                elif action == "sync-replies":
                    result = self.server.connected.sync_replies(invoice_id)
                elif action == "reconcile-request":
                    result = self.server.connected.reconcile_request(invoice_id)
                elif action == "reconcile-reference":
                    result = self.server.connected.reconcile_reference(invoice_id)
                else:
                    result = {"accounting": self.server.connected.refresh_invoice(invoice_id)}
                result["invoice"] = self._invoice_detail(invoice_id)
                return self._json(result)
            if path == "/api/invoices":
                return self._json(service.create_invoice(data, actor=actor), 201)
            if path == "/api/invoices/import":
                return self._json(service.import_invoices(data.get("csv"), actor=actor))
            match = re.fullmatch(r"/api/invoices/" + ID + r"/investigate", path)
            if match:
                return self._json(self.server.investigator.investigate(match.group(1), data, actor))
            match = re.fullmatch(r"/api/invoices/" + ID + r"/documents", path)
            if match:
                service.attach_document(match.group(1), data, actor=actor)
                return self._json(self._invoice_detail(match.group(1)))
            match = re.fullmatch(r"/api/invoices/" + ID + r"/actions", path)
            if match:
                if data.get("action") == "approve_resolution":
                    self._approval_source(match.group(1))
                result = service.action(match.group(1), data, actor=actor)
                result["invoice"] = self._invoice_detail(match.group(1))
                return self._json(result)
            raise DomainError("not_found", "This action was not found.", status=404)
        except DomainError as error:
            self._error(error)
        except (ConnectionError, BrokenPipeError):
            self.close_connection = True
        except Exception:
            self._error(DomainError("internal_error", "This action could not be completed. Reload and try again.", status=500))

    def _static(self, raw_path):
        root = self.server.settings.web_path.resolve()
        try:
            decoded = unquote(raw_path)
            if "\x00" in decoded or "\\" in decoded or any(part == ".." for part in decoded.split("/")):
                raise ValueError
            target = (root / (decoded.lstrip("/") or "index.html")).resolve()
            target.relative_to(root)
            if not target.is_file():
                raise ValueError
        except (ValueError, OSError):
            raise DomainError("not_found", "This file was not found.", status=404)
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if mime.startswith("text/") or target.suffix == ".js":
            mime += "; charset=utf-8"
        self._send(target.read_bytes(), mime)


def create_server(settings: Settings) -> InvoiceHTTPServer:
    return InvoiceHTTPServer(settings)


def run_server():
    settings = Settings.from_env()
    try:
        server = create_server(settings)
    except OSError as error:
        if error.errno in {48, 98}:
            raise SystemExit(f"Port {settings.port} is in use. Try make run PORT=8101.") from error
        raise
    print(f"InvoiceClear · http://{settings.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
