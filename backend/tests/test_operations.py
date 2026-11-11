"""Storage and readiness checks use temporary databases and no network sockets."""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from invoiceclear.api import InvoiceHandler, create_server
from invoiceclear.config import Settings
from invoiceclear.operations import Maintenance, backup_database
from invoiceclear.service import Service


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="invoiceclear-backups-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "workspace.sqlite3"

    def test_snapshot_includes_committed_wal_and_preserves_source(self):
        Service(self.path, demo=False)
        writer = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE records (value TEXT)")
        writer.execute("INSERT INTO records VALUES ('committed')")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO records VALUES ('uncommitted')")
        original = self.path.read_bytes()
        snapshot = backup_database(self.path)
        self.assertEqual(self.path.read_bytes(), original)
        with sqlite3.connect(snapshot) as backup:
            self.assertEqual(backup.execute("SELECT value FROM records").fetchall(), [("committed",)])
            self.assertEqual(backup.execute("PRAGMA quick_check").fetchone()[0], "ok")
        self.assertEqual(writer.execute("SELECT value FROM records").fetchall(), [("committed",), ("uncommitted",)])
        writer.rollback()
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
        self.assertEqual(snapshot.parent.stat().st_mode & 0o777, 0o700)
        immutable = snapshot.read_bytes()
        writer.execute("INSERT INTO records VALUES ('later')")
        self.assertEqual(backup_database(self.path), snapshot)
        self.assertEqual(snapshot.read_bytes(), immutable)
        self.assertFalse(list(snapshot.parent.glob("*.partial")))

    def test_pre_investigator_snapshot_remains_valid_and_immutable(self):
        Service(self.path, demo=False)
        snapshot = backup_database(self.path)
        with sqlite3.connect(snapshot) as connection:
            connection.execute("DROP TABLE agent_runs")
        original = snapshot.read_bytes()
        self.assertEqual(backup_database(self.path), snapshot)
        self.assertEqual(snapshot.read_bytes(), original)

    def test_present_but_incomplete_investigation_backup_is_rejected(self):
        Service(self.path, demo=False)
        snapshot = backup_database(self.path)
        with sqlite3.connect(snapshot) as connection:
            connection.execute("DROP TABLE agent_runs")
            connection.execute("CREATE TABLE agent_runs (id TEXT PRIMARY KEY)")
        original = snapshot.read_bytes()
        with self.assertRaises(sqlite3.DatabaseError):
            backup_database(self.path)
        self.assertEqual(snapshot.read_bytes(), original)

    def daily_snapshot(self):
        directory = self.root / "backups"
        directory.mkdir(mode=0o700, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        return directory / f"{self.path.stem}-{day}.sqlite3"

    def test_existing_snapshot_is_verified_read_only_and_kept_immutable(self):
        Service(self.path, demo=False)
        snapshot = backup_database(self.path)
        original, modified = snapshot.read_bytes(), snapshot.stat().st_mtime_ns
        with patch("invoiceclear.operations.sqlite3.connect", wraps=sqlite3.connect) as connect:
            with patch("invoiceclear.operations.os.link") as publish:
                self.assertEqual(backup_database(self.path), snapshot)
        connect.assert_called_once_with(snapshot.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        publish.assert_not_called()
        self.assertEqual(snapshot.read_bytes(), original)
        self.assertEqual(snapshot.stat().st_mtime_ns, modified)

    def test_corrupt_and_empty_existing_snapshots_are_preserved_and_reported_failed(self):
        Service(self.path, demo=False)
        snapshot = self.daily_snapshot()
        for contents in (b"not a SQLite database", b""):
            with self.subTest(contents=contents):
                snapshot.write_bytes(contents)
                with self.assertRaises(sqlite3.DatabaseError):
                    backup_database(self.path)
                maintenance = Maintenance(self.path, True)
                maintenance.snapshot()
                self.assertIsNotNone(maintenance.error)
                self.assertIsNone(maintenance.last_backup)
                self.assertEqual(snapshot.read_bytes(), contents)
                self.assertEqual(list(snapshot.parent.iterdir()), [snapshot])

    def test_existing_snapshot_requires_complete_core_schema(self):
        Service(self.path, demo=False)
        snapshot = backup_database(self.path)
        with sqlite3.connect(snapshot) as connection:
            connection.execute("DROP TABLE documents")
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
        original = snapshot.read_bytes()
        with self.assertRaises(sqlite3.DatabaseError):
            backup_database(self.path)
        self.assertEqual(snapshot.read_bytes(), original)

    def test_existing_snapshot_requires_core_columns_and_workspace_identity(self):
        Service(self.path, demo=False)
        snapshot = backup_database(self.path)
        with sqlite3.connect(snapshot) as connection:
            connection.execute("DELETE FROM workspace_identity")
        original = snapshot.read_bytes()
        with self.assertRaises(sqlite3.DatabaseError):
            backup_database(self.path)
        self.assertEqual(snapshot.read_bytes(), original)
        with sqlite3.connect(snapshot) as connection:
            connection.execute("INSERT INTO workspace_identity VALUES (1, 'private')")
            connection.execute("DROP TABLE documents")
            connection.execute("CREATE TABLE documents (id TEXT)")
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
        original = snapshot.read_bytes()
        with self.assertRaises(sqlite3.DatabaseError):
            backup_database(self.path)
        self.assertEqual(snapshot.read_bytes(), original)

    def test_non_regular_existing_snapshot_is_never_replaced(self):
        Service(self.path, demo=False)
        snapshot = self.daily_snapshot()
        snapshot.mkdir()
        with self.assertRaises(sqlite3.DatabaseError):
            backup_database(self.path)
        self.assertTrue(snapshot.is_dir())
        snapshot.rmdir()
        snapshot.symlink_to(self.root / "missing-recovery-file.sqlite3")
        with self.assertRaises(sqlite3.DatabaseError):
            backup_database(self.path)
        self.assertTrue(snapshot.is_symlink())
        self.assertFalse(snapshot.exists())

    def test_invalid_snapshot_created_during_publication_is_never_replaced(self):
        Service(self.path, demo=False)
        snapshot = self.daily_snapshot()
        original_link = os.link
        def conflicting_snapshot(staging, destination):
            destination.write_bytes(b"preserve this damaged recovery file")
            return original_link(staging, destination)
        with patch("invoiceclear.operations.os.link", side_effect=conflicting_snapshot):
            with self.assertRaises(sqlite3.DatabaseError):
                backup_database(self.path)
        self.assertEqual(snapshot.read_bytes(), b"preserve this damaged recovery file")
        self.assertEqual(list(snapshot.parent.iterdir()), [snapshot])

    def test_fresh_snapshot_rejects_sqlite_without_workspace_schema(self):
        with sqlite3.connect(self.path) as connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
        original = self.path.read_bytes()
        with self.assertRaises(sqlite3.DatabaseError):
            backup_database(self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list((self.root / "backups").iterdir()), [])

    def test_missing_database_does_not_create_database_or_backup_directory(self):
        self.assertIsNone(backup_database(self.path))
        self.assertFalse(self.path.exists())
        self.assertFalse((self.root / "backups").exists())
        maintenance = Maintenance(self.path, True)
        maintenance.snapshot()
        self.assertIn("unavailable", maintenance.error)
        self.assertFalse(self.path.exists())

    def test_failed_snapshot_keeps_original_and_removes_partial(self):
        self.path.write_bytes(b"not a SQLite database")
        with self.assertRaises(sqlite3.DatabaseError):
            backup_database(self.path)
        self.assertEqual(self.path.read_bytes(), b"not a SQLite database")
        self.assertEqual(list((self.root / "backups").iterdir()), [])
        maintenance = Maintenance(self.path, True)
        maintenance.snapshot()
        self.assertEqual(maintenance.error, "Backup failed. Check persistent storage and available space.")

    def test_disabled_maintenance_never_snapshots(self):
        maintenance = Maintenance(self.path, False)
        with patch("invoiceclear.operations.backup_database") as backup:
            maintenance.start()
            maintenance.snapshot()
            maintenance.close()
        backup.assert_not_called()
        self.assertIsNone(maintenance._thread)


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="invoiceclear-health-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "confidential-client.sqlite3"

    def server(self, *, backups_enabled=True):
        settings = Settings(database_path=self.path, web_path=self.root, mode="private",
                            email="finance@example.test", password="isolated-test-password",
                            backups_enabled=backups_enabled)
        # Exercise construction and lifecycle while omitting socket initialization.
        with patch("invoiceclear.api.ThreadingHTTPServer.__init__", return_value=None):
            server = create_server(settings)
        self.addCleanup(server.maintenance.close)
        self.addCleanup(server.capture.close)
        return server

    def response(self, server):
        handler = InvoiceHandler.__new__(InvoiceHandler)
        handler.server, handler.path = server, "/api/health"
        handler._json = Mock()
        handler.do_GET()
        self.assertEqual(handler._json.call_count, 1)
        payload, status = handler._json.call_args.args
        public = json.dumps(payload)
        self.assertNotIn(str(self.root), public)
        self.assertNotIn(self.path.name, public)
        self.assertNotIn("finance@example.test", public)
        return payload, status

    def test_startup_backup_and_close_lifecycle(self):
        server = self.server()
        payload, status = self.response(server)
        self.assertEqual(status, 200)
        self.assertEqual(payload["backups"], {"enabled": True, "status": "ok"})
        self.assertTrue(list((self.root / "backups").glob("*.sqlite3")))
        self.assertEqual(server.maintenance._thread.name, "invoiceclear-backup")
        with patch("invoiceclear.api.ThreadingHTTPServer.server_close") as close:
            server.server_close()
        close.assert_called_once()
        self.assertFalse(server.maintenance._thread.is_alive())

    def test_health_missing_database_returns_503_without_replacement(self):
        server = self.server()
        self.path.unlink()
        payload, status = self.response(server)
        self.assertEqual(status, 503)
        self.assertEqual(payload["status"], "degraded")
        self.assertIn("unavailable", payload["database"]["error"])
        self.assertFalse(self.path.exists())
        with self.assertRaises(sqlite3.OperationalError):
            server.service.list_invoices()
        with self.assertRaises(sqlite3.OperationalError):
            server.sessions.valid("a" * 32)
        self.assertFalse(self.path.exists())

    def test_health_missing_schema_returns_503_without_repair(self):
        server = self.server()
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP TABLE provider_usage")
        payload, status = self.response(server)
        self.assertEqual(status, 503)
        self.assertIn("schema", payload["database"]["error"])
        with sqlite3.connect(self.path) as connection:
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_schema WHERE name='provider_usage'").fetchone())

    def test_backup_failure_returns_503_and_retry_recovers(self):
        with patch("invoiceclear.operations.backup_database", side_effect=OSError("/private/client-data secret")):
            server = self.server()
        payload, status = self.response(server)
        self.assertEqual(status, 503)
        self.assertEqual(payload["backups"]["status"], "degraded")
        self.assertNotIn("client-data", json.dumps(payload))
        server.maintenance.snapshot()
        self.assertEqual(self.response(server)[1], 200)

    def test_invalid_existing_backup_returns_503_without_replacing_it(self):
        server = self.server()
        snapshot = self.root / "backups" / server.maintenance.last_backup
        snapshot.write_bytes(b"preserve damaged snapshot")
        server.maintenance.snapshot()
        payload, status = self.response(server)
        self.assertEqual(status, 503)
        self.assertEqual(payload["backups"]["status"], "degraded")
        self.assertEqual(snapshot.read_bytes(), b"preserve damaged snapshot")

    def test_missing_access_schema_returns_503_without_repair(self):
        server = self.server()
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP TABLE access_sessions")
        payload, status = self.response(server)
        self.assertEqual(status, 503)
        self.assertIn("access schema", payload["database"]["error"])
        with sqlite3.connect(self.path) as connection:
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_schema WHERE name='access_sessions'").fetchone())

    def test_explicitly_disabled_backups_keep_healthy_database_ready(self):
        server = self.server(backups_enabled=False)
        payload, status = self.response(server)
        self.assertEqual(status, 200)
        self.assertEqual(payload["backups"], {"enabled": False, "status": "disabled"})
        self.assertFalse((self.root / "backups").exists())

    def test_missing_capture_schema_is_unhealthy_without_repair(self):
        server = self.server()
        server.capture.close()
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP TABLE inbox_capture")
        payload, status = self.response(server)
        self.assertEqual(status, 503)
        self.assertEqual(payload["status"], "degraded")
        with sqlite3.connect(self.path) as connection:
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_schema WHERE name='inbox_capture'").fetchone())

    def test_missing_intake_progress_schema_is_unhealthy_without_repair(self):
        server = self.server()
        server.capture.close()
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP TABLE external_intake_progress")
        payload, status = self.response(server)
        self.assertEqual(status, 503)
        self.assertEqual(payload["status"], "degraded")
        with sqlite3.connect(self.path) as connection:
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_schema WHERE name='external_intake_progress'").fetchone())


if __name__ == "__main__":
    unittest.main()
