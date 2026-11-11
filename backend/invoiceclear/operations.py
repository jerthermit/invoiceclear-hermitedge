from __future__ import annotations

import os
import sqlite3
import stat
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def _verify_snapshot(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise sqlite3.DatabaseError("Backup verification failed")
    required = {"invoices", "documents", "replies", "resolutions", "audit", "idempotency", "provider_usage", "workspace_identity"}
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    if not required.issubset(tables):
        raise sqlite3.DatabaseError("Backup workspace schema is incomplete")
    # Pre-investigator snapshots remain valid recovery inputs. When present,
    # retained investigations must have the complete recoverable record shape.
    if "agent_runs" in tables:
        connection.execute("SELECT id,invoice_id,idempotency_key,source_version,source_fingerprint,source_snapshot,status,created_at,completed_at,lease_until,model,summary,next_step,draft,citations,trace,error FROM agent_runs LIMIT 0")
    # A valid empty SQLite file is not a usable InvoiceClear recovery snapshot.
    for table, columns in (
        ("invoices", "id,provenance"), ("documents", "id,invoice_id,content,sha256"),
        ("replies", "id,invoice_id,text"), ("resolutions", "id,invoice_id,data"),
        ("audit", "seq,invoice_id,event,data"), ("idempotency", "invoice_id,key,fingerprint,response"),
        ("provider_usage", "day,calls,tokens"), ("workspace_identity", "id,mode"),
    ):
        connection.execute(f"SELECT {columns} FROM {table} LIMIT 0")
    identity = connection.execute("SELECT mode FROM workspace_identity WHERE id=1").fetchone()
    if not identity or identity[0] not in {"demo", "private"}:
        raise sqlite3.DatabaseError("Backup workspace identity is unavailable")


def _verify_existing_snapshot(path: Path) -> None:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise sqlite3.DatabaseError("Backup snapshot is not a regular file")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as connection:
        _verify_snapshot(connection)


def backup_database(path: Path) -> Path | None:
    """Consistent SQLite snapshot without replacing or changing source records."""
    if str(path) == ":memory:" or not path.is_file():
        return None
    directory = path.parent / "backups"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    # One immutable snapshot per UTC day. Backups are not automatically deleted.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    destination = directory / f"{path.stem}-{stamp}.sqlite3"
    if destination.exists() or destination.is_symlink():
        _verify_existing_snapshot(destination)
        return destination
    staging = directory / f".{path.stem}-{stamp}-{os.getpid()}.partial"
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as source:
            with closing(sqlite3.connect(str(staging), timeout=10)) as target:
                source.backup(target)
                _verify_snapshot(target)
        os.chmod(staging, 0o600)
        try:
            # Publish atomically without replacing a snapshot created meanwhile.
            os.link(staging, destination)
        except FileExistsError:
            _verify_existing_snapshot(destination)
        staging.unlink()
    except Exception:
        if staging.exists():
            staging.unlink()
        raise
    return destination


class Maintenance:
    def __init__(self, database_path: Path, enabled: bool):
        self.path = database_path
        self.enabled = enabled
        self.last_backup: str | None = None
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def snapshot(self):
        if not self.enabled:
            return
        try:
            result = backup_database(self.path)
            self.last_backup = result.name if result else None
            self.error = None if result else "Workspace database is unavailable. Restore persistent storage."
        except (OSError, sqlite3.Error):
            self.error = "Backup failed. Check persistent storage and available space."

    def start(self):
        if self.enabled and self._thread is None:
            self.snapshot()
            self._thread = threading.Thread(target=self._run, name="invoiceclear-backup", daemon=True)
            self._thread.start()

    def _run(self):
        while not self._stop.wait(3600):
            self.snapshot()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def status(self):
        return {"enabled": self.enabled, "last_backup": self.last_backup, "error": self.error}
