import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import Fernet

from invoiceclear.config import Settings
from invoiceclear.connections import Connections, MAX_BYTES, SCOPES
from invoiceclear.documents import DomainError


class Response:
    def __init__(self, request, value=None, raw=None, headers=None, url=None):
        self.url = url or request.full_url
        self.status = 200
        self.headers = headers or {}
        self.raw = json.dumps(value).encode() if raw is None else raw
        self.maximum = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return self.url

    def read(self, maximum):
        self.maximum = maximum
        return self.raw[:maximum]


class ConnectionsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.settings = Settings(database_path=Path(self.directory.name) / "test.sqlite3", web_path=Path(self.directory.name),
                                 mode="private", email="finance@example.com", password="unique-workspace-password",
                                 public_url="http://127.0.0.1:8100", frontend_url="https://finance.example.com",
                                 token_key=Fernet.generate_key().decode(), qbo_client_id="qbo-client", qbo_client_secret="qbo-secret",
                                 google_client_id="google-client", google_client_secret="google-secret", qbo_po_field_id="1")
        self.connections = Connections(self.settings)
        self.requests = []

    def tearDown(self):
        self.directory.cleanup()

    def http(self, values):
        values = iter(values)

        def open_request(request, timeout):
            self.assertEqual(timeout, 15)
            self.requests.append(request)
            value = next(values)
            if isinstance(value, BaseException):
                raise value
            return Response(request, value=value)

        return patch.object(self.connections.opener, "open", side_effect=open_request)

    @staticmethod
    def tokens(provider, **changes):
        result = {"access_token": "private-access-token", "refresh_token": "private-refresh-token", "token_type": "Bearer", "expires_in": 3600}
        if provider == "gmail":
            result["scope"] = SCOPES[provider]
        return dict(result, **changes)

    def state(self, provider):
        return parse_qs(urlsplit(self.connections.start(provider, self.connections.fingerprint)["url"]).query)["state"][0]

    def connect(self, provider):
        query = {"state": self.state(provider), "code": "private-code"}
        if provider == "qbo":
            query["realmId"] = "934145348913"
        profile = {"CompanyInfo": {"Id": "1", "CompanyName": "Staffing company"}} if provider == "qbo" else {"emailAddress": "finance@example.com"}
        with self.http([self.tokens(provider), profile]):
            return self.connections.callback(provider, query, self.connections.fingerprint), query

    def test_state_is_hashed_one_use_expiring_and_fingerprint_bound(self):
        state = self.state("gmail")
        with sqlite3.connect(self.settings.database_path) as db:
            stored = db.execute("SELECT state_hash,expires FROM connection_states").fetchone()
        self.assertNotEqual(state, stored[0])
        self.assertLessEqual(stored[1], time.time() + 600)
        query = {"state": state, "code": "private-code"}
        with self.assertRaises(DomainError):
            self.connections.callback("gmail", query, "other-fingerprint")
        with self.http([self.tokens("gmail"), {"emailAddress": "finance@example.com"}]):
            self.connections.callback("gmail", query, self.connections.fingerprint)
        with self.assertRaises(DomainError):
            self.connections.callback("gmail", query, self.connections.fingerprint)
        expired = self.state("qbo")
        with sqlite3.connect(self.settings.database_path) as db:
            db.execute("UPDATE connection_states SET expires=0")
        with self.assertRaises(DomainError):
            self.connections.callback("qbo", {"state": expired, "code": "private-code", "realmId": "12"}, self.connections.fingerprint)

    def test_gmail_authorization_offline_scopes_and_validated_status(self):
        started = self.connections.start("gmail", self.connections.fingerprint)
        params = parse_qs(urlsplit(started["url"]).query)
        self.assertEqual(params["access_type"], ["offline"])
        self.assertEqual(params["prompt"], ["consent"])
        self.assertEqual(params["scope"], [SCOPES["gmail"]])
        self.assertNotIn("gmail.modify", params["scope"][0])
        self.assertEqual(params["redirect_uri"], [self.settings.public_url + "/api/connections/gmail/callback"])
        result, _ = self.connect("gmail")
        self.assertEqual(result, {"provider": "gmail", "connected": True, "account": "finance@example.com"})
        self.assertEqual(self.connections.status()["gmail"]["account_id"], "finance@example.com")
        self.assertNotIn("token", json.dumps(self.connections.status()))

    def test_authorization_identity_changes_on_consent_but_not_token_refresh(self):
        self.connect("gmail")
        original = self.connections.status()["gmail"]["grant_id"]
        grant = self.connections._load("gmail")
        grant["expires"] = 0
        self.connections._save("gmail", grant)
        with self.http([self.tokens("gmail", access_token="refreshed-access-token")]):
            refreshed = self.connections._access("gmail")
        self.assertEqual(refreshed["grant_id"], original)
        self.assertEqual(self.connections.status()["gmail"]["grant_id"], original)
        self.connect("gmail")
        self.assertNotEqual(self.connections.status()["gmail"]["grant_id"], original)

    def test_grants_encrypted_and_rotated_with_google_refresh_preserved(self):
        for provider in ("qbo", "gmail"):
            self.connect(provider)
            grant = self.connections._load(provider)
            grant["expires"] = 0
            self.connections._save(provider, grant)
            refreshed = self.tokens(provider, access_token="rotated-access", refresh_token="rotated-refresh")
            if provider == "gmail":
                refreshed.pop("refresh_token")
                refreshed.pop("scope")
            with self.http([refreshed, {"ok": True}]):
                if provider == "qbo":
                    self.connections.qbo("POST", "invoice?minorversion=75", {"Id": "17"}, request_id="update-identity")
                else:
                    self.connections.gmail("GET", "messages?maxResults=5&q=from%3Acustomer%40example.com")
            stored = self.connections._load(provider)
            self.assertEqual(stored["access_token"], "rotated-access")
            self.assertEqual(stored["refresh_token"], "rotated-refresh" if provider == "qbo" else "private-refresh-token")
            with sqlite3.connect(self.settings.database_path) as db:
                encrypted = db.execute("SELECT encrypted FROM connection_grants WHERE provider=?", (provider,)).fetchone()[0]
            for secret in (b"private-refresh-token", b"rotated-refresh", b"rotated-access"):
                self.assertNotIn(secret, encrypted)
            self.assertEqual(json.loads(Fernet(self.settings.token_key.encode()).decrypt(encrypted))["access_token"], "rotated-access")
        self.assertIn("sandbox-quickbooks.api.intuit.com/v3/company/934145348913/", self.requests[3].full_url)
        self.assertIn("requestid=update-identity", self.requests[3].full_url)
        self.assertTrue(self.requests[2].get_header("Authorization").startswith("Basic "))

    def test_refresh_is_serialized_across_connection_instances(self):
        self.connect("qbo")
        grant = self.connections._load("qbo")
        grant["expires"] = 0
        self.connections._save("qbo", grant)
        second = Connections(self.settings)
        counts = {"refresh": 0}

        def open_request(request, timeout):
            if "tokens/bearer" in request.full_url:
                counts["refresh"] += 1
                return Response(request, self.tokens("qbo", refresh_token="rotated-refresh"))
            return Response(request, {"Invoice": {"Id": "17"}})

        with patch.object(self.connections.opener, "open", side_effect=open_request), patch.object(second.opener, "open", side_effect=open_request):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda c: c.qbo("GET", "invoice/17"), (self.connections, second)))
        self.assertEqual(counts["refresh"], 1)
        self.assertEqual(len(results), 2)

    def test_credentials_environment_fingerprint_and_key_rotation_invalidate(self):
        for changes in ({"password": "changed-workspace-password"}, {"qbo_client_secret": "changed-secret"},
                        {"qbo_environment": "production"}, {"token_key": Fernet.generate_key().decode()}):
            with self.subTest(changes=list(changes)):
                self.connect("qbo")
                changed = Connections(replace(self.settings, **changes))
                self.assertFalse(changed.status()["qbo"]["connected"])
                with self.assertRaises(DomainError):
                    changed.qbo("GET", "invoice/17")
        state = self.state("gmail")
        changed = Connections(self.settings, fingerprint="new-session-fingerprint")
        with self.assertRaises(DomainError):
            changed.callback("gmail", {"state": state, "code": "code"}, "new-session-fingerprint")

    def test_denial_partial_scopes_invalid_realm_and_profile_fail_closed(self):
        self.connect("gmail")
        state = self.state("gmail")
        with self.assertRaises(DomainError) as caught:
            self.connections.callback("gmail", {"state": state, "error": "secret-provider-detail"}, self.connections.fingerprint)
        self.assertNotIn("secret", caught.exception.message)
        self.assertFalse(self.connections.status()["gmail"]["connected"])
        for token in (self.tokens("gmail", scope="https://www.googleapis.com/auth/gmail.readonly"), self.tokens("gmail", scope=None), self.tokens("gmail", refresh_token="")):
            state = self.state("gmail")
            with self.http([token]), self.assertRaises(DomainError):
                self.connections.callback("gmail", {"state": state, "code": "code"}, self.connections.fingerprint)
            self.assertFalse(self.connections.status()["gmail"]["connected"])
        state = self.state("qbo")
        with patch.object(self.connections.opener, "open") as http, self.assertRaises(DomainError):
            self.connections.callback("qbo", {"state": state, "code": "code", "realmId": "12/other"}, self.connections.fingerprint)
        http.assert_not_called()
        state = self.state("gmail")
        with self.http([self.tokens("gmail"), {"emailAddress": "bad\r\naddress@example.com"}]), self.assertRaises(DomainError):
            self.connections.callback("gmail", {"state": state, "code": "code"}, self.connections.fingerprint)

    def test_provider_dispatch_binds_original_company_and_mailbox(self):
        for provider in ("qbo", "gmail"):
            self.connect(provider)
            method = self.connections.qbo if provider == "qbo" else self.connections.gmail
            with patch.object(self.connections.opener, "open") as http, self.assertRaises(DomainError) as raised:
                method("GET", "invoice/17" if provider == "qbo" else "profile", expected_account="different-account")
            self.assertEqual(raised.exception.code, "account_changed")
            http.assert_not_called()

    def test_provider_auth_and_remote_revocation_fail_closed(self):
        self.connect("qbo")
        error = urllib.error.HTTPError("https://secret.example", 401, "private-access-token", {}, io.BytesIO(b"private-refresh-token"))
        with self.http([error]), self.assertRaises(DomainError) as caught:
            self.connections.qbo("GET", "invoice/17")
        self.assertNotIn("private", caught.exception.message)
        self.assertFalse(self.connections.status()["qbo"]["connected"])
        self.connect("gmail")
        with self.http([TimeoutError("private-refresh-token")]), self.assertRaises(DomainError):
            self.connections.disconnect("gmail")
        self.assertFalse(self.connections.status()["gmail"]["connected"])

    def test_missing_rotating_refresh_token_and_corrupt_storage_fail_closed(self):
        self.connect("qbo")
        grant = self.connections._load("qbo")
        grant["expires"] = 0
        self.connections._save("qbo", grant)
        incomplete = self.tokens("qbo")
        incomplete.pop("refresh_token")
        with self.http([incomplete]), self.assertRaises(DomainError):
            self.connections.qbo("GET", "invoice/17")
        self.assertFalse(self.connections.status()["qbo"]["connected"])
        self.connect("gmail")
        with sqlite3.connect(self.settings.database_path) as db:
            db.execute("UPDATE connection_grants SET encrypted=? WHERE provider='gmail'", (b"corrupted-token",))
        status = self.connections.status()["gmail"]
        self.assertFalse(status["connected"])
        self.assertNotIn("corrupted-token", status["error"])

    def test_unconfigured_and_demo_never_call_external_http(self):
        for settings in (replace(self.settings, mode="demo"), replace(self.settings, qbo_client_id="", qbo_client_secret="", google_client_id="", google_client_secret="")):
            connections = Connections(settings)
            self.assertFalse(connections.status()["qbo"]["configured"])
            with patch.object(connections.opener, "open") as http:
                for provider in ("qbo", "gmail"):
                    with self.assertRaises(DomainError):
                        connections.start(provider, connections.fingerprint)
                with self.assertRaises(DomainError):
                    connections.qbo("GET", "invoice/17")
                http.assert_not_called()

    def test_bounded_requests_safe_paths_redirects_and_errors(self):
        for path in ("https://attacker.example/invoice", "//attacker.example/invoice", "//[", "invoice/../17", "invoice/%31", "invoice/17#fragment", "query?access_token=secret", "query?query=bad%0Avalue", "query?query=bad%zz"):
            with self.assertRaises(DomainError):
                self.connections.qbo("GET", path)
        with self.assertRaises(DomainError):
            self.connections._http("GET", "https://attacker.example/profile")
        url = "https://www.googleapis.com/gmail/v1/users/me/profile"
        for raw, headers in ((b"secret-bad-json", {}), (b"{}", {"Content-Length": str(MAX_BYTES + 1)}), (b"x" * (MAX_BYTES + 1), {})):
            with patch.object(self.connections.opener, "open", side_effect=lambda req, timeout: Response(req, raw=raw, headers=headers)), self.assertRaises(DomainError) as caught:
                self.connections._http("GET", url)
            self.assertNotIn("secret", caught.exception.message)
        redirect = urllib.error.HTTPError(url, 302, "secret", {"Location": "https://attacker.example"}, None)
        with self.http([redirect]), self.assertRaises(DomainError):
            self.connections._http("GET", url)


class ConnectionConfigTests(unittest.TestCase):
    def test_exact_origins_partial_credentials_and_key_requirements(self):
        for values in ({"QBO_CLIENT_ID": "client"}, {"GOOGLE_CLIENT_SECRET": "secret"},
                       {"INVOICECLEAR_PUBLIC_URL": "http://finance.example.com"},
                       {"INVOICECLEAR_FRONTEND_URL": "https://finance.example.com/path"},
                       {"INVOICECLEAR_PUBLIC_URL": "https://user:secret@example.com"},
                       {"INVOICECLEAR_TOKEN_KEY": "not-a-key"}, {"QBO_ENVIRONMENT": "live"}):
            with patch.dict(os.environ, values, clear=True), self.assertRaises(ValueError):
                Settings.from_env()
        private = {"INVOICECLEAR_MODE": "private", "INVOICECLEAR_EMAIL": "finance@example.com", "INVOICECLEAR_PASSWORD": "unique-workspace-password", "QBO_CLIENT_ID": "client", "QBO_CLIENT_SECRET": "secret"}
        with patch.dict(os.environ, private, clear=True), self.assertRaises(ValueError):
            Settings.from_env()
        private.update(INVOICECLEAR_TOKEN_KEY=Fernet.generate_key().decode(), INVOICECLEAR_PUBLIC_URL="http://[::1]:8100/")
        with patch.dict(os.environ, private, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.public_url, "http://[::1]:8100")
        self.assertEqual(settings.frontend_url, settings.public_url)


if __name__ == "__main__":
    unittest.main()
