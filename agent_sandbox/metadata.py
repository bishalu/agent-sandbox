"""Persistent run metadata, stored outside containers (spec R-09).

One directory per sandbox id under runs/. Writes are atomic (temp + rename)
so concurrent sandboxes cannot corrupt each other's records, and no global
lock is needed (R-13).
"""

import datetime
import json
import os
import pathlib

from . import config


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _write_atomic(path, data):
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


class RunRecord:
    """The metadata for one sandbox id, across one or more container runs."""

    def __init__(self, sandbox_id, data=None):
        self.sandbox_id = sandbox_id
        self.data = data or {
            "sandbox_id": sandbox_id,
            "created_at": _now(),
            "repo": None,
            "workspace": None,
            "workspace_kind": None,
            "branch": None,
            "command": None,
            "mode": None,
            "runtime": None,
            "network": None,
            "image": None,
            "resources": None,
            "credentials": None,
            "agent_home": None,        # runs/<id>/agent-home (R-18)
            "mounts": [],              # every declared bind mount (R-16)
            "trusted_mounts": [],      # the git common-dir set, rw into host state (R-20)
            "tags": {},                # --tag key=value, e.g. unit and milestone
            "admission": None,         # the effective numbers and sources (R4)
            "containers": [],
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "status": "created",
            "logs": None,
        }

    # -- paths -----------------------------------------------------------
    @property
    def dir(self):
        return config.RUNS / self.sandbox_id

    @property
    def file(self):
        return self.dir / "run.json"

    @property
    def stdout_log(self):
        return self.dir / "stdout.log"

    @property
    def stderr_log(self):
        return self.dir / "stderr.log"

    # -- persistence -----------------------------------------------------
    def save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        self.data["logs"] = {
            "dir": str(self.dir),
            "stdout": str(self.stdout_log),
            "stderr": str(self.stderr_log),
        }
        _write_atomic(self.file, self.data)
        return self

    @classmethod
    def load(cls, sandbox_id):
        return cls.load_path(config.RUNS / sandbox_id / "run.json")

    @classmethod
    def load_path(cls, path):
        """A record by its run.json path; None when missing or unreadable."""
        path = pathlib.Path(path)
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError):
            return None
        return cls(data.get("sandbox_id") or path.parent.name, data)

    @classmethod
    def all(cls):
        out = []
        if not config.RUNS.exists():
            return out
        for d in sorted(config.RUNS.iterdir()):
            if d.name.startswith("."):
                continue
            rec = cls.load(d.name)
            if rec:
                out.append(rec)
        return out

    # -- mutation --------------------------------------------------------
    def update(self, **kw):
        self.data.update(kw)
        return self

    def add_container(self, container_id, runtime, command, status="running",
                      pid=None, stdout_offset_start=None):
        """A sandbox id can outlive many containers (enter / re-run).

        `pid` is the launching process, so a later reconciliation can tell an
        orphaned container (up, launcher gone) from a running one; the stdout
        offsets bound this entry's own segment of the shared stdout.log.
        """
        self.data.setdefault("containers", []).append({
            "container": container_id,
            "runtime": runtime,
            "command": command,
            "pid": pid,
            "started_at": _now(),
            "waiting_since": None,
            "finished_at": None,
            "exit_code": None,
            "status": status,
            "stdout_offset_start": stdout_offset_start,
            "stdout_offset_end": None,
        })
        return self

    def finish_container(self, exit_code, status, stdout_offset_end=None):
        if self.data.get("containers"):
            c = self.data["containers"][-1]
            c["finished_at"] = _now()
            c["exit_code"] = exit_code
            c["status"] = status
            if stdout_offset_end is not None:
                c["stdout_offset_end"] = stdout_offset_end
        return self

    def wait(self):
        """Admission is holding this launch: visible as waiting, not as
        running or vanished (KTD1)."""
        self.data["status"] = "waiting"
        if self.data.get("containers"):
            c = self.data["containers"][-1]
            c["status"] = "waiting"
            c["waiting_since"] = c.get("waiting_since") or _now()
        return self

    def start(self):
        self.data["started_at"] = _now()
        self.data["status"] = "running"
        if self.data.get("containers") and self.data["containers"][-1].get("status") == "waiting":
            self.data["containers"][-1]["status"] = "running"
        return self

    def finish(self, exit_code, status):
        self.data["finished_at"] = _now()
        self.data["exit_code"] = exit_code
        self.data["status"] = status
        return self

    # -- guarded write-back (R7, KTD4) -----------------------------------
    def mark_entries(self, fields_by_index, status=None):
        """Set fields on container entries, and optionally the top-level
        status, through the re-read guard.

        Two writers share this file: the live sandbox process (plain atomic
        rename, no version check) and the reconciliation. So this re-reads the
        record immediately before writing and abandons the write when any
        entry it would change, or the top-level status when it would change
        that, differs from what this record read. Returns True when written,
        False when abandoned; on success this record's data follows the file.
        """
        fresh = self.load_path(self.file)
        if fresh is None:
            return False
        mine = self.data.get("containers") or []
        theirs = fresh.data.get("containers") or []
        for i in fields_by_index:
            if i >= len(mine) or i >= len(theirs) or mine[i] != theirs[i]:
                return False
        if status is not None and fresh.data.get("status") != self.data.get("status"):
            return False
        for i, fields in fields_by_index.items():
            theirs[i].update(fields)
        if status is not None:
            fresh.data["status"] = status
        _write_atomic(self.file, fresh.data)
        self.data = fresh.data
        return True

    def mark_entry(self, index, **fields):
        """One entry's fields through the same guard."""
        return self.mark_entries({index: fields})

    def public(self):
        """The structured result emitted by --json (R-09)."""
        d = self.data
        return {
            "sandbox_id": d["sandbox_id"],
            "repo": d.get("repo"),
            "worktree": d.get("workspace"),
            "workspace_kind": d.get("workspace_kind"),
            "branch": d.get("branch"),
            "container": (d["containers"][-1]["container"]
                          if d.get("containers") else None),
            "containers": [c.get("container") for c in d.get("containers", [])],
            "image": d.get("image"),
            "mode": d.get("mode"),
            "runtime": d.get("runtime"),
            "network": d.get("network"),
            "resources": d.get("resources"),
            "credentials": d.get("credentials"),
            "agent_home": d.get("agent_home"),
            "mounts": d.get("mounts") or [],
            "trusted_mounts": d.get("trusted_mounts") or [],
            "tags": d.get("tags") or {},
            "admission": d.get("admission"),
            "command": d.get("command"),
            "exit_code": d.get("exit_code"),
            "status": d.get("status"),
            "started_at": d.get("started_at"),
            "finished_at": d.get("finished_at"),
            "logs": d.get("logs"),
        }
