from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from pathlib import Path


class Sessions:
    """Single workspace grants; only token hashes are stored, including after restart."""

    def __init__(self, path: Path, email: str, password: str, ttl: int):
        self.path, self.email, self.ttl = path, email, ttl
        self.email_hash = hashlib.sha256(email.encode()).digest()
        self.password_hash = hashlib.sha256(password.encode()).digest()
        self.fingerprint = hashlib.sha256(self.email_hash + self.password_hash).hexdigest()
        with self.connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS access_sessions (
                token_hash TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, expires REAL NOT NULL
            )""")
            connection.execute("DELETE FROM access_sessions WHERE fingerprint<>? OR expires<=?", (self.fingerprint, time.time()))

    def connect(self):
        return sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=10)

    def create(self, email: object, password: object) -> dict | None:
        if not isinstance(email, str) or not isinstance(password, str) or len(email) > 254 or len(password) > 256:
            return None
        matches_email = hmac.compare_digest(hashlib.sha256(email.encode()).digest(), self.email_hash)
        matches_password = hmac.compare_digest(hashlib.sha256(password.encode()).digest(), self.password_hash)
        if not (matches_email and matches_password):
            return None
        token = secrets.token_urlsafe(32)
        expires = time.time() + self.ttl
        with self.connect() as connection:
            connection.execute("DELETE FROM access_sessions WHERE expires<=? OR fingerprint<>?", (time.time(), self.fingerprint))
            if connection.execute("SELECT COUNT(*) FROM access_sessions").fetchone()[0] >= 256:
                connection.execute("DELETE FROM access_sessions WHERE token_hash=(SELECT token_hash FROM access_sessions ORDER BY expires LIMIT 1)")
            connection.execute("INSERT INTO access_sessions VALUES(?,?,?)", (self.digest(token), self.fingerprint, expires))
        return {"access_token": token, "expires_at": expires}

    @staticmethod
    def digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def valid(self, token: str) -> bool:
        if not isinstance(token, str) or not 20 <= len(token) <= 200:
            return False
        with self.connect() as connection:
            row = connection.execute("SELECT 1 FROM access_sessions WHERE token_hash=? AND fingerprint=? AND expires>?", (self.digest(token), self.fingerprint, time.time())).fetchone()
            return row is not None

    def revoke(self, token: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM access_sessions WHERE token_hash=?", (self.digest(token),))
