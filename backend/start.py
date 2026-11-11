"""Prepare the container's mounted directory, then run the API without root."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys


DATA = Path("/data")
APP_UID = APP_GID = 10001


def main():
    if os.geteuid() == 0:
        try:
            try:
                directory = DATA.lstat()
            except FileNotFoundError:
                DATA.mkdir(mode=0o700)
                directory = DATA.lstat()
            if not stat.S_ISDIR(directory.st_mode):
                raise SystemExit("Container storage /data must be a real directory, not a symlink.")
            # Change only the mount directory; preserve every existing child.
            os.chown(DATA, APP_UID, APP_GID, follow_symlinks=False)
            os.chmod(DATA, 0o700)
            os.setgroups([])
            os.setgid(APP_GID)
            os.setuid(APP_UID)
            if os.geteuid() != APP_UID or os.getegid() != APP_GID or os.getgroups():
                raise SystemExit("Container could not drop root privileges; API was not started.")
        except OSError:
            raise SystemExit("Container storage setup or privilege drop failed; API was not started.") from None
    application = str(Path(__file__).resolve().with_name("run.py"))
    os.execv(sys.executable, [sys.executable, application])


if __name__ == "__main__":
    main()
