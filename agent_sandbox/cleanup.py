"""Removal and disk hygiene (spec R-14).

Targeted by design. `clean --docker` prunes dangling images, build cache, and
stopped containers carrying this tool's label — never unrelated containers,
images, networks or volumes, and never on a schedule.
"""

import datetime
import shutil

from . import config, state, worktree
from .metadata import RunRecord


_docker = config.run_docker


def age_days(rec):
    ts = rec.data.get("finished_at") or rec.data.get("created_at")
    if not ts:
        return 0.0
    try:
        then = datetime.datetime.fromisoformat(ts)
    except ValueError:
        return 0.0
    if then.tzinfo is None:
        then = then.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now - then).total_seconds() / 86400.0


def managed_container_names():
    """Every container carrying this tool's label, in one docker call."""
    p = _docker(["ps", "-a", "--filter", f"label={config.LABEL_MANAGED}=true",
                 "--format", "{{.Names}}"])
    if p.returncode != 0:
        return set()
    return {l.strip() for l in p.stdout.splitlines() if l.strip()}


def survey(older_than_days=14, container_exists=None, pid_alive=None):
    """Classify sandboxes into removable / protected without deleting anything.

    `status` is the derived top-level status and `state` the derived sandbox
    state (state.derive_record), not what run.json happens to say: a record
    a dead launcher left at running reads as crashed here. The probes are
    injectable; by default one `docker ps` and os.kill(pid, 0) answer them.
    """
    if container_exists is None:
        names = managed_container_names()
        container_exists = names.__contains__
    pid_alive = pid_alive or state.pid_alive
    removable, protected, young = [], [], []
    for rec in RunRecord.all():
        sid = rec.sandbox_id
        path = rec.data.get("workspace")
        kind = rec.data.get("workspace_kind")
        if kind == "direct":
            continue                       # nothing of ours to remove
        age = age_days(rec)
        derived = state.derive_record(rec, container_exists, pid_alive)
        info = {"id": sid, "age_days": round(age, 1), "path": path,
                "branch": rec.data.get("branch"), "status": derived.status,
                "state": derived.state}
        if age < older_than_days:
            young.append(info)
            continue
        dirty = worktree.has_uncommitted_work(path) if path else False
        ahead = worktree.unpushed_commits(path) if path else 0
        if dirty or ahead:
            info["reason"] = ("uncommitted changes" if dirty else
                              f"{ahead} commit(s) not on any other branch")
            protected.append(info)
        else:
            removable.append(info)
    return {"removable": removable, "protected": protected, "young": young}


def clean(older_than_days=14, force=False, dry_run=False):
    """Age-based sweep that never silently discards useful work (R-14)."""
    s = survey(older_than_days)
    targets = list(s["removable"])
    if force:
        targets += s["protected"]
    removed = []
    if not dry_run:
        for t in targets:
            try:
                worktree.remove(t["id"], force=True)
                removed.append(t["id"])
            except Exception:
                pass
    return {"removed": removed, "candidates": targets,
            "protected": [] if force else s["protected"], "young": s["young"]}


def docker_usage():
    """Docker's disk footprint, for doctor and clean --docker."""
    p = _docker(["system", "df", "--format", "{{.Type}}\t{{.Size}}\t{{.Reclaimable}}"])
    rows = []
    if p.returncode == 0:
        for line in p.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                rows.append({"type": parts[0], "size": parts[1],
                             "reclaimable": parts[2]})
    return rows


def docker_prune(dry_run=False):
    """Prune ONLY: dangling images, build cache, and our own stopped containers.

    Never touches unrelated containers, tagged images, networks, or volumes.
    """
    actions = {"containers": [], "images": None, "build_cache": None}

    p = _docker(["ps", "-a", "--filter", f"label={config.LABEL_MANAGED}=true",
                 "--filter", "status=exited", "--filter", "status=created",
                 "--filter", "status=dead", "--format", "{{.ID}} {{.Names}}"])
    ours = [l for l in p.stdout.strip().splitlines() if l.strip()]
    for line in ours:
        cid = line.split()[0]
        if not dry_run:
            _docker(["rm", "-f", cid])
        actions["containers"].append(line)

    if not dry_run:
        # Dangling (untagged) images only — no -a, so tagged images survive.
        r = _docker(["image", "prune", "-f"])
        actions["images"] = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "nothing to prune"
        r = _docker(["builder", "prune", "-f"])
        actions["build_cache"] = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "nothing to prune"
    return actions


def disk_free_gb(path=None):
    usage = shutil.disk_usage(str(path or config.ROOT))
    return usage.free / (1024 ** 3)
