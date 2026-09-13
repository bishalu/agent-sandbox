"""Two lock shapes the launcher relies on (R3, KTD2).

`PidLock` is a lock file holding `<pid> <note>`; a holder whose process is
dead is reclaimed, so a launcher killed mid-run never wedges the next one.
It is the base `worktree.DirectLock` always was, factored out so the
per-project milestone lock shares the same stale rule.

`FileFlock` is a kernel `flock`: held for the short window between a
reading and `docker run`, blocking, released explicitly. Separate open file
descriptions conflict even inside one process, which is what lets two
launcher threads serialize on it in tests.
"""

import fcntl
import os
import pathlib
import threading

from .errors import LockHeld


class PidLock:
    """Advisory lock file keyed by the holder's pid; a dead holder is reclaimed."""

    def __init__(self, path, note=""):
        self.path = pathlib.Path(path)
        self.note = str(note)

    def holder(self):
        """(pid, note) as written, or None when the file is missing or garbage."""
        try:
            pid, _, note = self.path.read_text().strip().partition(" ")
            return int(pid), note
        except (ValueError, OSError):
            return None

    def _stale(self):
        h = self.holder()
        if h is None:
            return True
        pid = h[0]
        if pid == os.getpid():
            return True
        try:
            os.kill(pid, 0)          # signal 0 only tests existence
        except ProcessLookupError:
            return True
        except PermissionError:
            return False             # exists, owned by someone else
        return False

    def is_held(self):
        """True when another live process holds it (our own hold does not count)."""
        return self.path.exists() and not self._stale()

    def _held_error(self, holder):
        return LockHeld(f"lock held: {self.path} ({holder})",
                        "Wait for the holder to finish, or remove the lock if it is dead.")

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and not self._stale():
            raise self._held_error(self.path.read_text().strip())
        if self.path.exists():
            self.path.unlink()       # reclaim stale
        self.path.write_text(f"{os.getpid()} {self.note}\n")
        return self

    def release(self):
        try:
            if self.path.exists():
                pid = int(self.path.read_text().split()[0])
                if pid == os.getpid():
                    self.path.unlink()
        except (ValueError, OSError, IndexError):
            pass


class FileFlock:
    """A blocking `flock` on one file; release is idempotent and thread-safe,
    because the backend releases it from a watcher thread and again in its
    cleanup."""

    def __init__(self, path):
        self.path = pathlib.Path(path)
        self._fd = None
        self._mu = threading.Lock()

    def acquire(self, blocking=True):
        """True when held. Non-blocking: False when someone else holds it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            os.close(fd)
            return False
        with self._mu:
            self._fd = fd
        return True

    def release(self):
        with self._mu:
            fd, self._fd = self._fd, None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
