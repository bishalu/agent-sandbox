"""Persistent run metadata, stored outside containers (spec R-09).

One directory per sandbox id under runs/. Writes are atomic (temp + rename)
so concurrent sandboxes cannot corrupt each other's records, and no global
lock is needed (R-13).
"""

import datetime
import json
import os

from . import config


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


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
        tmp = self.file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2) + "\n")
        os.replace(tmp, self.file)
        return self

    @classmethod
    def load(cls, sandbox_id):
        path = config.RUNS / sandbox_id / "run.json"
        if not path.exists():
            return None
        try:
            return cls(sandbox_id, json.loads(path.read_text()))
        except (ValueError, OSError):
            return None

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

    def add_container(self, container_id, runtime, command, status="running"):
        """A sandbox id can outlive many containers (enter / re-run)."""
        self.data.setdefault("containers", []).append({
            "container": container_id,
            "runtime": runtime,
            "command": command,
            "started_at": _now(),
            "finished_at": None,
            "exit_code": None,
            "status": status,
        })
        return self

    def finish_container(self, exit_code, status):
        if self.data.get("containers"):
            c = self.data["containers"][-1]
            c["finished_at"] = _now()
            c["exit_code"] = exit_code
            c["status"] = status
        return self

    def start(self):
        self.data["started_at"] = _now()
        self.data["status"] = "running"
        return self

    def finish(self, exit_code, status):
        self.data["finished_at"] = _now()
        self.data["exit_code"] = exit_code
        self.data["status"] = status
        return self

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
            "command": d.get("command"),
            "exit_code": d.get("exit_code"),
            "status": d.get("status"),
            "started_at": d.get("started_at"),
            "finished_at": d.get("finished_at"),
            "logs": d.get("logs"),
        }
