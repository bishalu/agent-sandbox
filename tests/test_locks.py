"""locks: the stale-PID lock base and the flock, and DirectLock's unchanged shape."""

import os
import subprocess
import sys
import threading
import time

import pytest

from agent_sandbox import config, locks, worktree
from agent_sandbox.errors import LockHeld


def _dead_pid():
    """A pid that existed a moment ago and is gone now."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


# ---------------------------------------------------------------- PidLock
def test_acquire_writes_pid_and_note(tmp_path):
    lock = locks.PidLock(tmp_path / "a.lock", note="hello")
    lock.acquire()
    pid, note = lock.holder()
    assert pid == os.getpid() and note == "hello"
    assert lock.is_held() is False          # ours, so not "held by another"
    lock.release()
    assert not lock.path.exists()


def test_live_foreign_holder_blocks(tmp_path):
    path = tmp_path / "a.lock"
    path.write_text(f"{os.getppid()} parent\n")     # our parent is alive
    lock = locks.PidLock(path)
    assert lock.is_held() is True
    with pytest.raises(LockHeld):
        lock.acquire()
    assert path.read_text().startswith(str(os.getppid()))


def test_dead_holder_is_reclaimed(tmp_path):
    path = tmp_path / "a.lock"
    path.write_text(f"{_dead_pid()} gone\n")
    lock = locks.PidLock(path, note="mine")
    assert lock.is_held() is False
    lock.acquire()
    assert lock.holder()[0] == os.getpid()


def test_garbage_lock_file_is_reclaimed(tmp_path):
    path = tmp_path / "a.lock"
    path.write_text("not a pid\n")
    lock = locks.PidLock(path)
    assert lock.holder() is None
    lock.acquire()
    assert lock.holder()[0] == os.getpid()


def test_release_leaves_a_foreign_lock_alone(tmp_path):
    path = tmp_path / "a.lock"
    path.write_text(f"{os.getppid()} parent\n")
    locks.PidLock(path).release()
    assert path.exists()


def test_release_when_file_gone_is_quiet(tmp_path):
    locks.PidLock(tmp_path / "a.lock").release()


# ---------------------------------------------------------------- DirectLock
def test_directlock_is_a_pidlock_with_the_same_path_and_format(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path)
    lock = worktree.DirectLock("/some/repo")
    assert isinstance(lock, locks.PidLock)
    assert lock.path.parent == tmp_path and lock.path.suffix == ".lock"
    lock.acquire()
    assert lock.path.read_text() == f"{os.getpid()} /some/repo\n"
    lock.release()


def test_directlock_message_still_names_direct(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path)
    lock = worktree.DirectLock("/some/repo")
    lock.path.write_text(f"{os.getppid()} /some/repo\n")
    with pytest.raises(LockHeld) as e:
        lock.acquire()
    assert "--direct" in e.value.message and "worktree" in e.value.remedy


# ---------------------------------------------------------------- FileFlock
def test_flock_serializes_two_holders(tmp_path):
    path = tmp_path / "admission.lock"
    order = []
    a = locks.FileFlock(path)
    a.acquire()

    def second():
        b = locks.FileFlock(path)
        b.acquire()
        order.append(("b", time.monotonic()))
        b.release()

    t = threading.Thread(target=second)
    t.start()
    time.sleep(0.3)
    order.append(("a-release", time.monotonic()))
    a.release()
    t.join(5)
    assert [o[0] for o in order] == ["a-release", "b"]
    assert order[1][1] >= order[0][1]


def test_flock_release_is_idempotent(tmp_path):
    f = locks.FileFlock(tmp_path / "x.lock")
    f.release()
    f.acquire()
    f.release()
    f.release()


def test_remove_refuses_an_empty_or_pathlike_id(tmp_path, monkeypatch):
    from agent_sandbox import worktree
    from agent_sandbox.errors import WorktreeError
    import pytest
    for bad in ("", " ", "../x", "a/b", "/", ".."):
        with pytest.raises(WorktreeError):
            worktree.remove(bad, force=True)
    assert worktree.require_sandbox_id("vibeset-dj-1a2b3c4d") == "vibeset-dj-1a2b3c4d"
