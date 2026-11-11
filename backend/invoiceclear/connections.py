"""Private-workspace OAuth grants and bounded QuickBooks/Gmail requests."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing
from urllib.parse import parse_qsl, urlencode, urlsplit

from cryptography.fernet import Fernet, InvalidToken

from .documents import DomainError

MAX_BYTES = 16 * 1024 * 1024
SCOPES = {
    "qbo": "com.intuit.quickbooks.accounting",
    "gmail": "https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/gmail.readonly",
}
AUTH = {"qbo": "https://appcenter.intuit.com/connect/oauth2", "gmail": "https://accounts.google.com/o/oauth2/v2/auth"}
TOKEN = {"qbo": "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer", "gmail": "https://oauth2.googleapis.com/token"}
QBO_HOST = {"sandbox": "sandbox-quickbooks.api.intuit.com", "production": "quickbooks.api.intuit.com"}
HOSTS = {*QBO_HOST.values(), "oauth.platform.intuit.com", "oauth2.googleapis.com", "www.googleapis.com", "developer.api.intuit.com"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS connection_grants (
 provider TEXT PRIMARY KEY, context TEXT NOT NULL, encrypted BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS connection_states (
 state_hash TEXT PRIMARY KEY, provider TEXT NOT NULL, context TEXT NOT NULL,
 fingerprint TEXT NOT NULL, expires REAL NOT NULL);
"""
LOCKS = {}
LOCK_GUARD = threading.Lock()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def packed(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class Connections:
    def __init__(self, settings, fingerprint=""):
        self.settings = settings
        self.path = settings.database_path
        self.fingerprint = fingerprint or hashlib.sha256(hashlib.sha256(settings.email.encode()).digest()
                                                         + hashlib.sha256(settings.password.encode()).digest()).hexdigest()
        self.cipher = Fernet(settings.token_key.encode()) if settings.token_key else None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        with closing(self._db()) as db:
            db.executescript(SCHEMA)
            for provider in SCOPES:
                db.execute("DELETE FROM connection_grants WHERE provider=? AND context<>?", (provider, self._context(provider)))
            db.execute("DELETE FROM connection_states WHERE expires<=?", (time.time(),))
            db.commit()

    def _db(self):
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def _lock(self, provider):
        with LOCK_GUARD:
            return LOCKS.setdefault((str(self.path.resolve()), provider), threading.RLock())

    def _client(self, provider):
        if provider == "qbo":
            return self.settings.qbo_client_id, self.settings.qbo_client_secret
        return self.settings.google_client_id, self.settings.google_client_secret

    def _context(self, provider):
        return hashlib.sha256(packed([self._client(provider), self.fingerprint, self.settings.token_key,
                                     self.settings.public_url, self.settings.frontend_url, self.settings.mode,
                                     self.settings.qbo_environment if provider == "qbo" else ""]).encode()).hexdigest()

    def _configured(self, provider):
        return bool(self.settings.mode == "private" and all(self._client(provider)) and self.cipher
                    and self.settings.public_url and self.settings.frontend_url)

    def _require(self, provider):
        if provider not in SCOPES:
            raise DomainError("invalid_provider", "Choose QuickBooks or Gmail.")
        if not self._configured(provider):
            raise DomainError("connection_unconfigured", "Configure this provider in a private workspace first.", 409)

    def _clear(self, provider):
        with closing(self._db()) as db:
            db.execute("DELETE FROM connection_grants WHERE provider=?", (provider,))
            db.execute("DELETE FROM connection_states WHERE provider=?", (provider,))
            db.commit()

    def _load(self, provider):
        with closing(self._db()) as db:
            row = db.execute("SELECT context,encrypted FROM connection_grants WHERE provider=?", (provider,)).fetchone()
        if not row or row["context"] != self._context(provider) or not self._configured(provider):
            return None
        try:
            grant = json.loads(self.cipher.decrypt(row["encrypted"]))
            if not isinstance(grant, dict) or not self._token_text(grant.get("access_token")) or not self._token_text(grant.get("refresh_token")):
                raise ValueError()
            if type(grant.get("expires")) not in {int, float} or not math.isfinite(grant["expires"]):
                raise ValueError()
            if (not isinstance(grant.get("account"), str) or not 1 <= len(grant["account"]) <= 1024
                    or any(ord(c) < 32 for c in grant["account"]) or not isinstance(grant.get("account_id"), str)
                    or (provider == "qbo" and not re.fullmatch(r"[0-9]{1,30}", grant["account_id"]))
                    or (provider == "gmail" and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", grant["account_id"]))
                    or (grant.get("refresh_expires") is not None and
                        (type(grant["refresh_expires"]) not in {int, float} or not math.isfinite(grant["refresh_expires"])))):
                raise ValueError()
            if not isinstance(grant.get("grant_id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{32}", grant["grant_id"]):
                # Upgrade a legacy grant once; routine token refresh retains this
                # authorization identity rather than resetting capture consent.
                grant["grant_id"] = secrets.token_urlsafe(24)
                self._save(provider, grant)
            return grant
        except (InvalidToken, ValueError, TypeError, KeyError, UnicodeDecodeError):
            self._clear(provider)
            raise DomainError("connection_invalid", "Stored connection is unavailable. Connect this provider again.", 409) from None

    def _save(self, provider, grant):
        encrypted = self.cipher.encrypt(packed(grant).encode())
        with closing(self._db()) as db:
            db.execute("INSERT OR REPLACE INTO connection_grants VALUES(?,?,?)", (provider, self._context(provider), encrypted))
            db.commit()

    def status(self):
        result = {}
        for provider in SCOPES:
            item = {"configured": self._configured(provider), "connected": False, "account": "", "account_id": ""}
            if provider == "qbo":
                item["environment"] = self.settings.qbo_environment
                item["po_field_configured"] = bool(self.settings.qbo_po_field_id)
            try:
                with self._lock(provider):
                    grant = self._load(provider)
                    if grant and grant.get("refresh_expires") and grant["refresh_expires"] <= time.time():
                        self._clear(provider)
                        grant = None
                    if grant:
                        item.update(connected=True, account=grant["account"], account_id=grant["account_id"], grant_id=grant["grant_id"])
            except DomainError as exc:
                item["error"] = exc.message
            result[provider] = item
        return result

    def _check_fingerprint(self, fingerprint):
        if not isinstance(fingerprint, str) or not hmac.compare_digest(fingerprint, self.fingerprint):
            raise DomainError("oauth_state_invalid", "Connection request expired or is invalid. Start again.", 403)

    def start(self, provider, fingerprint):
        self._require(provider)
        self._check_fingerprint(fingerprint)
        state = secrets.token_urlsafe(32)
        with self._lock(provider), closing(self._db()) as db:
            db.execute("DELETE FROM connection_states WHERE provider=? OR expires<=?", (provider, time.time()))
            excess = max(0, db.execute("SELECT COUNT(*) FROM connection_states").fetchone()[0] - 31)
            db.execute("DELETE FROM connection_states WHERE state_hash IN (SELECT state_hash FROM connection_states ORDER BY expires LIMIT ?)", (excess,))
            db.execute("INSERT INTO connection_states VALUES(?,?,?,?,?)", (hashlib.sha256(state.encode()).hexdigest(), provider,
                        self._context(provider), self.fingerprint, time.time() + 600))
            db.commit()
        params = {"client_id": self._client(provider)[0], "redirect_uri": self._redirect(provider),
                  "response_type": "code", "scope": SCOPES[provider], "state": state}
        if provider == "gmail":
            params.update(access_type="offline", prompt="consent")
        return {"url": AUTH[provider] + "?" + urlencode(params)}

    def _redirect(self, provider):
        return self.settings.public_url + "/api/connections/" + provider + "/callback"

    def callback(self, provider, query, fingerprint):
        self._require(provider)
        self._check_fingerprint(fingerprint)
        if not isinstance(query, dict) or any(not isinstance(k, str) or not isinstance(v, str) or len(v) > 8192 for k, v in query.items()):
            raise DomainError("oauth_state_invalid", "Connection request expired or is invalid. Start again.", 403)
        state = query.get("state", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{30,200}", state):
            raise DomainError("oauth_state_invalid", "Connection request expired or is invalid. Start again.", 403)
        with self._lock(provider):
            with closing(self._db()) as db:
                db.execute("BEGIN IMMEDIATE")
                digest = hashlib.sha256(state.encode()).hexdigest()
                row = db.execute("SELECT * FROM connection_states WHERE state_hash=?", (digest,)).fetchone()
                db.execute("DELETE FROM connection_states WHERE state_hash=?", (digest,))
                db.commit()
            if not row or row["provider"] != provider or row["fingerprint"] != fingerprint or row["context"] != self._context(provider) or row["expires"] <= time.time():
                raise DomainError("oauth_state_invalid", "Connection request expired or is invalid. Start again.", 403)
            self._clear(provider)
            if query.get("error") or not self._token_text(query.get("code")):
                raise DomainError("oauth_denied", "Provider authorization was not completed. Connect again.", 409)
            realm = query.get("realmId", "")
            if provider == "qbo" and not re.fullmatch(r"[0-9]{1,30}", realm):
                raise DomainError("oauth_invalid_account", "QuickBooks returned an invalid company. Connect again.", 409)
            try:
                grant = self._tokens(provider, {"grant_type": "authorization_code", "code": query["code"], "redirect_uri": self._redirect(provider)})
                headers = {"Authorization": "Bearer " + grant["access_token"]}
                if provider == "gmail":
                    profile = self._http("GET", "https://www.googleapis.com/gmail/v1/users/me/profile", headers=headers)
                    address = profile.get("emailAddress")
                    if not isinstance(address, str) or len(address) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", address) or any(ord(c) < 32 for c in address):
                        raise DomainError("oauth_invalid_account", "Gmail did not return a valid mailbox address.", 409)
                    grant.update(account=address, account_id=address)
                else:
                    profile = self._http("GET", self._qbo_base(realm) + "companyinfo/" + realm, headers=headers)
                    info = profile.get("CompanyInfo")
                    if not isinstance(info, dict) or not re.fullmatch(r"[0-9]{1,30}", str(info.get("Id", ""))):
                        raise DomainError("oauth_invalid_account", "QuickBooks did not confirm this company.", 409)
                    label = info.get("CompanyName") or info.get("LegalName")
                    if not isinstance(label, str) or not 1 <= len(label) <= 1024 or any(ord(c) < 32 for c in label):
                        raise DomainError("oauth_invalid_account", "QuickBooks did not return a valid company name.", 409)
                    grant.update(account=label, account_id=realm)
                grant["grant_id"] = secrets.token_urlsafe(24)
                self._save(provider, grant)
                return {"provider": provider, "connected": True, "account": grant["account"]}
            except DomainError:
                self._clear(provider)
                raise

    @staticmethod
    def _token_text(value):
        return isinstance(value, str) and 1 <= len(value) <= 8192 and all(32 < ord(c) < 127 for c in value)

    def _tokens(self, provider, form, old=None):
        client, secret = self._client(provider)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if provider == "qbo":
            headers["Authorization"] = "Basic " + base64.b64encode((client + ":" + secret).encode()).decode()
        else:
            form = dict(form, client_id=client, client_secret=secret)
        data = self._http("POST", TOKEN[provider], urlencode(form).encode(), headers)
        refresh = data.get("refresh_token", old.get("refresh_token") if old and provider == "gmail" else None)
        expiry = data.get("expires_in")
        scopes = data.get("scope", old.get("scope") if old else "")
        if (not self._token_text(data.get("access_token")) or not self._token_text(refresh)
                or str(data.get("token_type", "")).lower() != "bearer" or type(expiry) is not int or not 60 <= expiry <= 86400
                or (provider == "gmail" and (not isinstance(scopes, str) or not set(SCOPES[provider].split()) <= set(scopes.split())))):
            raise DomainError("connection_invalid_response", "Provider credentials were incomplete. Connect again.", 409)
        grant = dict(old or {}, access_token=data["access_token"], refresh_token=refresh,
                     expires=time.time() + expiry, scope=scopes)
        lifetime = data.get("x_refresh_token_expires_in") if provider == "qbo" else data.get("refresh_token_expires_in")
        if lifetime is not None:
            if type(lifetime) is not int or not 1 <= lifetime <= 366 * 86400:
                raise DomainError("connection_invalid_response", "Provider credentials were incomplete. Connect again.", 409)
            grant["refresh_expires"] = time.time() + lifetime
        return grant

    def disconnect(self, provider):
        if provider not in SCOPES:
            raise DomainError("invalid_provider", "Choose QuickBooks or Gmail.")
        with self._lock(provider):
            grant = self._load(provider)
            self._clear(provider)  # Local access ends even if the remote revocation fails.
            if not grant:
                return
            if provider == "gmail":
                self._http("POST", "https://oauth2.googleapis.com/revoke", urlencode({"token": grant["refresh_token"]}).encode(),
                           {"Content-Type": "application/x-www-form-urlencoded"}, allow_empty=True)
            else:
                client, secret = self._client(provider)
                self._http("POST", "https://developer.api.intuit.com/v2/oauth2/tokens/revoke", packed({"token": grant["refresh_token"]}).encode(),
                           {"Content-Type": "application/json", "Authorization": "Basic " + base64.b64encode((client + ":" + secret).encode()).decode()}, allow_empty=True)

    def _access(self, provider):
        grant = self._load(provider)
        if not grant:
            raise DomainError("connection_required", "Connect this provider before continuing.", 409)
        if grant["expires"] <= time.time() + 60:
            try:
                if grant.get("refresh_expires") and grant["refresh_expires"] <= time.time():
                    raise DomainError("connection_expired", "Provider access expired. Connect again.", 409)
                grant = self._tokens(provider, {"grant_type": "refresh_token", "refresh_token": grant["refresh_token"]}, grant)
                self._save(provider, grant)
            except DomainError:
                self._clear(provider)
                raise
        return grant

    def _qbo_base(self, realm):
        return "https://" + QBO_HOST[self.settings.qbo_environment] + "/v3/company/" + realm + "/"

    def qbo(self, method, path, body=None, request_id=None, expected_account=None):
        return self._provider_request("qbo", method, path, body, request_id, expected_account)

    def gmail(self, method, path, body=None, expected_account=None):
        return self._provider_request("gmail", method, path, body, expected_account=expected_account)

    def _provider_request(self, provider, method, path, body, request_id=None, expected_account=None):
        self._require(provider)
        if method not in {"GET", "POST"} or not isinstance(path, str) or len(path) > 12000 or any(ord(c) <= 32 for c in path) or any(c in path for c in "\\#"):
            raise DomainError("invalid_provider_request", "Provider request path or method is invalid.")
        try:
            parsed = urlsplit(path)
        except ValueError:
            raise DomainError("invalid_provider_request", "Provider request path is invalid.") from None
        route = parsed.path
        pattern = r"(?:query|preferences|(?:invoice|customer)(?:/[0-9]{1,30})?|companyinfo/[0-9]{1,30})" if provider == "qbo" else r"(?:profile|messages(?:/[A-Za-z0-9_-]{1,256}(?:/attachments/[A-Za-z0-9_-]{1,512})?)?|threads(?:/[A-Za-z0-9_-]{1,256})?)"
        if parsed.scheme or parsed.netloc or not re.fullmatch(pattern, route) or re.search(r"%(?![0-9a-fA-F]{2})", parsed.query):
            raise DomainError("invalid_provider_request", "Provider request path is invalid.")
        keys = {"query", "minorversion", "requestid"} if provider == "qbo" else {"q", "maxResults", "pageToken", "format", "metadataHeaders", "includeSpamTrash", "labelIds", "fields"}
        params = parse_qsl(parsed.query, keep_blank_values=True)
        if any(k not in keys or len(v) > 10000 or any(ord(c) < 32 for c in v) for k, v in params):
            raise DomainError("invalid_provider_request", "Provider request query is invalid.")
        if request_id is not None:
            if provider != "qbo" or not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", request_id) or any(k == "requestid" for k, _ in params):
                raise DomainError("invalid_provider_request", "QuickBooks request identity is invalid.")
            params.append(("requestid", request_id))
        try:
            encoded = packed(body).encode() if body is not None else None
        except (ValueError, TypeError):
            raise DomainError("invalid_provider_request", "Provider request body is invalid.") from None
        with self._lock(provider):
            grant = self._access(provider)
            actual = grant["account_id"].lower() if provider == "gmail" else grant["account_id"]
            expected = str(expected_account).lower() if provider == "gmail" else str(expected_account)
            if expected_account is not None and actual != expected:
                raise DomainError("account_changed", "The provider account changed. Reconnect the original account for this record.", 409)
            base = self._qbo_base(grant["account_id"]) if provider == "qbo" else "https://www.googleapis.com/gmail/v1/users/me/"
            url = base + route + ("?" + urlencode(params) if params else "")
            try:
                return self._http(method, url, encoded, {"Content-Type": "application/json", "Authorization": "Bearer " + grant["access_token"]})
            except DomainError as exc:
                if exc.code in {"connection_auth", "connection_invalid_response"}:
                    self._clear(provider)
                raise

    def _http(self, method, url, body=None, headers=None, *, allow_empty=False):
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme == "https" and parsed.hostname in HOSTS and not parsed.username
                     and not parsed.password and parsed.port in {None, 443} and not parsed.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise DomainError("invalid_provider_request", "Provider endpoint is invalid.")
        if body is not None and len(body) > MAX_BYTES:
            raise DomainError("provider_request_too_large", "Provider request is too large.", 413)
        request = urllib.request.Request(url, data=body, method=method, headers={"Accept": "application/json", **(headers or {})})
        try:
            with self.opener.open(request, timeout=15) as response:
                if response.geturl() != url or not 200 <= response.status < 300:
                    raise DomainError("connection_invalid_response", "Provider returned an unexpected response.", 502)
                length = response.headers.get("Content-Length")
                if length and (not length.isdigit() or int(length) > MAX_BYTES):
                    raise DomainError("connection_invalid_response", "Provider response exceeded the supported size.", 502)
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise DomainError("connection_invalid_response", "Provider response exceeded the supported size.", 502)
                if allow_empty and not raw:
                    return {}
                data = json.loads(raw.decode("utf-8"))
                if not isinstance(data, dict):
                    raise ValueError()
                return data
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403} or (url in TOKEN.values() and exc.code == 400):
                raise DomainError("connection_auth", "Provider access was denied or expired. Connect again.", 409) from None
            if exc.code == 429:
                raise DomainError("provider_rate_limit", "Provider rate limit reached. Try again later.", 429) from None
            raise DomainError("provider_request_failed", "Provider request failed. Review the connection and try again.", 502) from None
        except (UnicodeDecodeError, ValueError, TypeError):
            raise DomainError("connection_invalid_response", "Provider returned an unreadable response.", 502) from None
        except (OSError, TimeoutError):
            raise DomainError("provider_unavailable", "Provider is unavailable. Try again later.", 502) from None
