"""Container startup checks without changing real permissions or identities."""
import stat
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import start as bootstrap


class ContainerStartupTests(unittest.TestCase):
    def setUp(self):
        self.operations = Mock()
        self.mocks = {}
        for name in ("chown", "chmod", "setgroups", "setgid", "setuid", "execv"):
            self.mocks[name] = self.enterContext(patch.object(bootstrap.os, name))
            self.operations.attach_mock(self.mocks[name], name)
        self.euid = self.enterContext(patch.object(bootstrap.os, "geteuid", side_effect=[0, 10001]))
        self.egid = self.enterContext(patch.object(bootstrap.os, "getegid", return_value=10001))
        self.groups = self.enterContext(patch.object(bootstrap.os, "getgroups", return_value=[]))
        self.lstat = self.enterContext(patch.object(Path, "lstat", return_value=SimpleNamespace(st_mode=stat.S_IFDIR | 0o755)))
        self.mkdir = self.enterContext(patch.object(Path, "mkdir"))
        self.iterdir = self.enterContext(patch.object(Path, "iterdir", side_effect=AssertionError("Existing children must not be inspected.")))
        self.application = str(Path(bootstrap.__file__).resolve().with_name("run.py"))

    def test_root_prepares_only_mount_then_drops_privileges_before_exec(self):
        # A mounted volume may contain a root-owned lost+found: never inspect it.
        bootstrap.main()
        self.assertEqual(self.operations.mock_calls, [
            call.chown(Path("/data"), 10001, 10001, follow_symlinks=False),
            call.chmod(Path("/data"), 0o700),
            call.setgroups([]), call.setgid(10001), call.setuid(10001),
            call.execv(sys.executable, [sys.executable, self.application]),
        ])
        self.iterdir.assert_not_called()
        self.mkdir.assert_not_called()

    def test_missing_mount_directory_is_created_before_drop(self):
        self.lstat.side_effect = [FileNotFoundError(), SimpleNamespace(st_mode=stat.S_IFDIR | 0o700)]
        bootstrap.main()
        self.mkdir.assert_called_once_with(mode=0o700)
        self.mocks["execv"].assert_called_once()

    def test_symlink_mount_is_rejected_before_permissions_or_exec(self):
        self.lstat.return_value = SimpleNamespace(st_mode=stat.S_IFLNK | 0o777)
        with self.assertRaisesRegex(SystemExit, "real directory"):
            bootstrap.main()
        self.assertEqual(self.operations.mock_calls, [])

    def test_failed_storage_setup_does_not_start_api(self):
        self.mocks["chown"].side_effect = PermissionError("Denied")
        with self.assertRaisesRegex(SystemExit, "API was not started"):
            bootstrap.main()
        self.mocks["setuid"].assert_not_called()
        self.mocks["execv"].assert_not_called()

    def test_failed_privilege_drop_does_not_start_api(self):
        self.mocks["setuid"].side_effect = PermissionError("Denied")
        with self.assertRaisesRegex(SystemExit, "API was not started"):
            bootstrap.main()
        self.mocks["execv"].assert_not_called()

    def test_incomplete_privilege_drop_does_not_start_api(self):
        self.euid.side_effect = [0, 0]
        with self.assertRaisesRegex(SystemExit, "drop root privileges"):
            bootstrap.main()
        self.mocks["execv"].assert_not_called()

    def test_non_root_start_directly_executes_without_storage_or_identity_changes(self):
        self.euid.side_effect = None
        self.euid.return_value = 10001
        bootstrap.main()
        self.assertEqual(self.operations.mock_calls, [
            call.execv(sys.executable, [sys.executable, self.application]),
        ])
        self.lstat.assert_not_called()
        self.mkdir.assert_not_called()
        self.iterdir.assert_not_called()
        self.egid.assert_not_called()
        self.groups.assert_not_called()


if __name__ == "__main__":
    unittest.main()
