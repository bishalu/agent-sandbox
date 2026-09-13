"""Derived sandbox state (R6, R7, KTD4).

`run.json` records what the launching process managed to write before it
died; this module never trusts that. Each container entry is judged from the
entry, whether its container is visible to `docker inspect`, and whether the
process that launched it is alive:

    waiting   admission is holding the launch, and it has not overrun
    running   the container is up (and the launcher, when recorded, alive)
    crashed   the entry says running or waiting, but the container is gone
    orphaned  the container is up but the process that launched it is gone
    finished  the launcher recorded an exit (completed, failed, timed_out)

The sandbox's state is its newest entry's. Milestone meaning (units, reports,
401s) is the driver's business, not this module's (SPEC non-goal).

Every probe is injected: the pure functions take booleans or callables, so
tests run without Docker, and `reconcile` writes only through the re-read
guard in `RunRecord.mark_entries`.
"""

import dataclasses
import datetime
import os

from . import config
from .metadata import RunRecord

STATES = ("waiting", "running", "crashed", "orphaned", "finished")
FINISHED = ("completed", "failed", "timed_out")

# The admission wait window (R2); the same config keys the launcher reads.
DEFAULT_WAIT_TIMEOUT_S = float(config.DEFAULTS["admission_wait_timeout_s"])
DEFAULT_WAIT_INTERVAL_S = float(config.DEFAULTS["admission_wait_interval_s"])


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _parse(ts):
    if not ts:
        return None
    try:
        t = datetime.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)


def pid_alive(pid):
    """None when no pid was recorded (a pre-U1 entry); otherwise whether the
    launching process still exists. A live pid owned by another user
    answers EPERM, which still means alive."""
    if pid is None:
        return None
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (TypeError, ValueError, OverflowError):
        return None                # not a pid at all: judge as unrecorded
    return True


# ---------------------------------------------------------------- per entry
@dataclasses.dataclass
class EntryState:
    index: int
    container: str
    recorded: str          # the status the entry carries in run.json
    state: str             # one of STATES
    evidence: str
    corrected: str = None  # the status the entry should carry, when different

    def to_dict(self, entry=None):
        d = dataclasses.asdict(self)
        if entry is not None:
            for k in ("pid", "started_at", "waiting_since", "finished_at", "exit_code",
                      "stdout_offset_start", "stdout_offset_end"):
                d[k] = entry.get(k)
            d["status"] = entry.get("status")
        return d


def derive_entry(entry, container_exists, pid_alive, now=None,
                 wait_timeout_s=DEFAULT_WAIT_TIMEOUT_S,
                 wait_interval_s=DEFAULT_WAIT_INTERVAL_S, index=0):
    """Pure: judge one container entry.

    `container_exists` is whether `docker inspect` finds the entry's
    container; `pid_alive` is True/False for the recorded launcher, None when
    the entry recorded no pid. `now` is a tz-aware datetime.
    """
    now = now or _now()
    name = entry.get("container") or "?"
    recorded = entry.get("status") or "?"
    pid = entry.get("pid")

    def result(state, evidence, corrected=None):
        return EntryState(index, name, recorded, state, evidence, corrected)

    if recorded in FINISHED:
        return result("finished", f"{recorded}, exit {entry.get('exit_code')} "
                                  f"at {entry.get('finished_at')}")

    if recorded == "crashed":
        # Already corrected by an earlier reconciliation: stable, so a second
        # derive changes nothing.
        return result("crashed", entry.get("evidence") or "recorded as crashed")

    if recorded == "waiting":
        if container_exists:
            # Admission passed and the container is up; the launcher is
            # between acquire and start(). Not a correction: it will write.
            return result("running", f"container {name} up before the record "
                                     "caught up with admission")
        if pid_alive is False:
            return result("crashed", f"waiting for admission, launcher pid {pid} gone, "
                                     f"no container {name}", corrected="crashed")
        since = _parse(entry.get("waiting_since")) or _parse(entry.get("started_at"))
        window = wait_timeout_s + wait_interval_s
        if since is not None:
            waited = (now - since).total_seconds()
            if waited > window:
                return result("crashed",
                              f"waiting since {since.isoformat()}, {waited:.0f} s, past "
                              f"the {window:.0f} s admission window, no container {name}",
                              corrected="crashed")
            return result("waiting", f"waiting for admission {waited:.0f} s of {window:.0f} s")
        return result("waiting", "waiting for admission (no timestamp recorded)")

    # running, orphaned, or anything unexpected: the container decides.
    if container_exists:
        if pid_alive is False:
            return result("orphaned", f"container {name} up, launcher pid {pid} gone",
                          corrected=None if recorded == "orphaned" else "orphaned")
        if pid_alive is None:
            return result("running", f"container {name} up, launcher pid unrecorded")
        return result("running", f"container {name} up, launcher pid {pid} alive")
    return result("crashed", f"entry says {recorded}, container {name} not found by "
                             "docker inspect", corrected="crashed")


# ---------------------------------------------------------------- per record
@dataclasses.dataclass
class RecordState:
    sandbox_id: str
    state: str                 # the sandbox's state: its newest entry's
    recorded_status: str       # top-level status as run.json carries it
    status: str                # the top-level status it should carry
    entries: list
    newest: EntryState = None

    @property
    def changes(self):
        """(index, EntryState) for every entry whose status must be corrected."""
        return [(e.index, e) for e in self.entries if e.corrected]

    @property
    def status_change(self):
        if self.status != self.recorded_status:
            return (self.recorded_status, self.status)
        return None

    @property
    def changed(self):
        return bool(self.changes) or self.status_change is not None

    def to_dict(self, record=None):
        containers = (record.data.get("containers") or []) if record else []
        return {
            "sandbox_id": self.sandbox_id,
            "state": self.state,
            "status": self.status,
            "recorded_status": self.recorded_status,
            "newest": (self.newest.to_dict(containers[self.newest.index]
                                           if self.newest.index < len(containers) else None)
                       if self.newest else None),
            "entries": [e.to_dict(containers[e.index] if e.index < len(containers) else None)
                        for e in self.entries],
            "corrections": len(self.changes),
        }


def _top_level_status(entry_state, entry):
    if entry_state.state == "finished":
        return entry.get("status")
    return entry_state.state


def derive_record(record, container_exists, pid_alive, now=None,
                  wait_timeout_s=DEFAULT_WAIT_TIMEOUT_S,
                  wait_interval_s=DEFAULT_WAIT_INTERVAL_S):
    """Apply `derive_entry` to every entry; the sandbox's state is the newest
    entry's. `container_exists(name)` and `pid_alive(pid)` are callables."""
    now = now or _now()
    data = record.data
    containers = data.get("containers") or []
    recorded = data.get("status") or "created"
    entries = []
    for i, c in enumerate(containers):
        entries.append(derive_entry(c, container_exists(c.get("container")),
                                    pid_alive(c.get("pid")), now=now,
                                    wait_timeout_s=wait_timeout_s,
                                    wait_interval_s=wait_interval_s, index=i))
    if not entries:
        # Nothing was launched. Terminal records are finished; a record still
        # at created/waiting has no pid to judge, so it stays as it says.
        state = "finished" if recorded in FINISHED else "waiting"
        return RecordState(record.sandbox_id, state, recorded, recorded, [], None)
    newest = entries[-1]
    return RecordState(record.sandbox_id, newest.state, recorded,
                       _top_level_status(newest, containers[-1]), entries, newest)


# ---------------------------------------------------------------- write-back
@dataclasses.dataclass
class Reconciled:
    sandbox_id: str
    written: bool
    reason: str
    state: str
    status: str
    changes: list              # [(index, new_status)] applied, or that would have been

    def to_dict(self):
        return {"written": self.written, "reason": self.reason, "state": self.state,
                "status": self.status, "changes": len(self.changes),
                "corrected": [{"index": i, "status": s} for i, s in self.changes]}


def reconcile(record_path, container_exists, pid_alive, now=None,
              wait_timeout_s=DEFAULT_WAIT_TIMEOUT_S,
              wait_interval_s=DEFAULT_WAIT_INTERVAL_S):
    """Correct every stale entry and the top-level status in one write (R7).

    Reads the record, derives, then writes through `RunRecord.mark_entries`,
    which re-reads immediately before writing and abandons the write when any
    entry it would change, or the top-level status, differs from what was
    read: the live sandbox process writes the same file with a plain atomic
    rename and no version check (KTD4). Returns what it did; when abandoned,
    the state reported is derived from the fresh record.
    """
    record_path = str(record_path)
    probes = dict(container_exists=container_exists, pid_alive=pid_alive, now=now,
                  wait_timeout_s=wait_timeout_s, wait_interval_s=wait_interval_s)
    rec = RunRecord.load_path(record_path)
    if rec is None:
        return Reconciled("?", False, "record missing or unreadable", "?", "?", [])
    derived = derive_record(rec, **probes)
    if not derived.changed:
        return Reconciled(rec.sandbox_id, False, "nothing to change", derived.state,
                          derived.status, [])
    stamp = _now().isoformat()
    fields = {i: {"status": e.corrected, "evidence": e.evidence, "reconciled_at": stamp}
              for i, e in derived.changes}
    changes = [(i, e.corrected) for i, e in derived.changes]
    ok = rec.mark_entries(fields, status=derived.status if derived.status_change else None)
    if ok:
        return Reconciled(rec.sandbox_id, True, "written", derived.state, derived.status,
                          changes)
    fresh = RunRecord.load_path(record_path)
    if fresh is None:
        return Reconciled(rec.sandbox_id, False, "record vanished between read and write",
                          "?", "?", [])
    now_state = derive_record(fresh, **probes)
    return Reconciled(rec.sandbox_id, False, "record changed between read and write",
                      now_state.state, now_state.status,
                      [(i, e.corrected) for i, e in now_state.changes])
