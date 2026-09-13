"""Admission control: may this container start now? (R1-R4, KTD1, KTD2)

Off by default; on, `LocalDockerBackend.run()` calls `acquire()` right
before `docker run`, so every caller of the library passes through it.

`decide` and `committed_memory` are pure: readings in, verdict out. The
budget counts every running container on the host, not only ours: a
limited container commits its limit, an unlimited one its live usage from
the newest memory-log sample plus a quarter, or the whole budget when the
sample does not name it, so an uncapped process neither deadlocks admission
nor escapes it.

`acquire` holds the host-wide flock from its first reading until the backend
sees the new container in `docker inspect` (or thirty seconds pass), so two
launchers cannot both pass on one stale snapshot. The per-project milestone
lock is a second, longer lock, taken only for a run tagged `milestone` and
released when that run returns.

Disk is a refusal, never a wait (KTD11): the host drive backing the guest
(under WSL the Windows drive holding ext4.vhdx) or the guest root below the
disk floor, or unreadable, refuses at once, because waiting cannot free a
disk and the 16:34 death was disk, not memory.
"""

import dataclasses
import datetime
import json
import subprocess
import sys
import threading
import time

from . import config, locks, memlog, metadata, resources
from .errors import AdmissionRefused, AdmissionTimeout, SandboxError

ADMISSION_LOCK = "admission.lock"
VISIBLE_TIMEOUT_S = 30.0
UNLIMITED_FACTOR = 1.25

G = 1024 ** 3


@dataclasses.dataclass
class Settings:
    """The admission keys resolved through the config ladder, with the tier
    each came from so the launcher can say so (R4)."""
    enabled: bool
    budget: int              # bytes
    floor: int               # bytes
    wait_timeout_s: float
    wait_interval_s: float
    memlog_max_age_s: float
    disk_floor: int          # bytes (KTD11)
    disk_path: str           # the host drive backing the guest
    sources: dict


@dataclasses.dataclass
class Decision:
    verdict: str             # admit | wait | refuse
    reasons: list
    numbers: dict            # name -> {"bytes"|"held"|..., "source"}

    def to_dict(self):
        return dataclasses.asdict(self)


def resolve_settings(cfg=None):
    cfg = config.load_config() if cfg is None else cfg
    vals, sources = {}, {}
    for key in ("enabled", "memory_budget", "mem_floor_gb", "wait_timeout_s",
                "wait_interval_s", "memlog_max_age_s", "disk_floor_gb"):
        vals[key], sources[key] = config.resolve_source(f"admission_{key}", None, cfg)
    sources["budget"] = sources.pop("memory_budget")
    sources["floor"] = sources.pop("mem_floor_gb")
    sources["disk_floor"] = sources.pop("disk_floor_gb")
    disk_path, sources["disk_path"] = config.host_disk_path_source(cfg)
    try:
        return Settings(
            enabled=config.as_bool(vals["enabled"]),
            budget=resources.parse_memory(vals["memory_budget"]),
            floor=int(float(vals["mem_floor_gb"]) * G),
            wait_timeout_s=float(vals["wait_timeout_s"]),
            wait_interval_s=float(vals["wait_interval_s"]),
            memlog_max_age_s=float(vals["memlog_max_age_s"]),
            disk_floor=int(float(vals["disk_floor_gb"]) * G),
            disk_path=disk_path,
            sources=sources)
    except ValueError as e:
        raise SandboxError(f"bad admission setting: {e}",
                           "Check the admission_* keys in config.json and any "
                           "AGENT_SANDBOX_ADMISSION_* variables.")


def fmt(nbytes):
    """Bytes as GiB the way the flags spell them: 8g, 7.5g."""
    g = nbytes / G
    return f"{g:.0f}g" if abs(g - round(g)) < 0.005 else f"{g:.1f}g"


# ---------------------------------------------------------------- pure logic
def committed_memory(inspect_rows, memlog_sample, budget):
    """Bytes already committed by every running container (KTD1).

    `inspect_rows` are `{"name", "memory_limit"}` from `docker inspect`
    (0 = unlimited); `memlog_sample` is the newest `memlog.Sample` or None.
    """
    usage = memlog_sample.containers if memlog_sample is not None else {}
    total = 0
    for row in inspect_rows:
        limit = int(row.get("memory_limit") or 0)
        if limit > 0:
            total += limit
        elif row.get("name") in usage:
            total += int(usage[row["name"]] * UNLIMITED_FACTOR)
        else:
            total += budget
    return total


def decide(request, committed, mem_available, memlog_sample, project_locked, cfg,
           request_source="config", disk_free=None):
    """admit, wait or refuse, with reasons and the effective numbers.

    `memlog_sample` is the `memlog.Freshness` the launcher read; a stale one
    refuses (fail closed, KTD5). `project_locked` is None for an untagged run
    (not checked), False when the tagged run's project lock is free, or the
    holder's pid. `disk_free` maps each path the launcher read (the host
    drive backing the guest and the guest root) to its free bytes, None when
    the path could not be read; any reading below the disk floor, or
    unreadable, refuses (KTD11). None means the caller took no disk readings
    (`admission show` reports without them). Refuse means waiting cannot
    help; wait means it might.
    """
    if not cfg.enabled:
        return Decision("admit", [], {})
    numbers = {
        "budget": {"bytes": cfg.budget, "source": cfg.sources.get("budget", "config")},
        "floor": {"bytes": cfg.floor, "source": cfg.sources.get("floor", "config")},
        "request": {"bytes": request, "source": request_source},
        "committed": {"bytes": committed, "source": "live: docker inspect + memory log"},
        "mem_available": {"bytes": mem_available, "source": "live: /proc/meminfo"},
        "headroom": {"bytes": cfg.budget - committed, "source": "derived"},
        "memlog": {"fresh": memlog_sample.fresh, "age_s": memlog_sample.age_s,
                   "source": "live: memory log"},
    }
    if project_locked is not None:
        numbers["project_lock"] = {"held": bool(project_locked),
                                   "pid": project_locked or None, "source": "live: lock file"}
    if disk_free is not None:
        numbers["disk_floor"] = {"bytes": cfg.disk_floor,
                                 "source": cfg.sources.get("disk_floor", "config")}
        for path, free in disk_free.items():
            numbers[f"disk_free:{path}"] = {"bytes": free, "source": "live: shutil.disk_usage"}
    refuse = []
    if not memlog_sample.fresh:
        refuse.append(f"memory log stale: {memlog_sample.reason}")
    if request > cfg.budget:
        refuse.append(f"request {fmt(request)} above the whole budget {fmt(cfg.budget)}")
    for path, free in (disk_free or {}).items():
        if free is None:
            refuse.append(f"disk {path} free space unreadable (floor {fmt(cfg.disk_floor)})")
        elif free < cfg.disk_floor:
            refuse.append(f"disk {path} has {fmt(free)} free, below floor {fmt(cfg.disk_floor)}")
    if refuse:
        return Decision("refuse", refuse, numbers)
    wait = []
    headroom = cfg.budget - committed
    if request > headroom:
        wait.append(f"headroom {fmt(headroom)} below request {fmt(request)}")
    if mem_available < cfg.floor:
        wait.append(f"MemAvailable {fmt(mem_available)} below floor {fmt(cfg.floor)}")
    if project_locked:
        wait.append(f"project lock held by pid {project_locked}")
    return Decision("wait" if wait else "admit", wait, numbers)


# ---------------------------------------------------------------- readers
_docker = config.run_docker


def parse_inspect(text):
    """Rows from `docker inspect --format '{{.Name}}\\t{{.HostConfig.Memory}}'`."""
    rows = []
    for line in text.splitlines():
        name, _, limit = line.strip().partition("\t")
        if not name:
            continue
        rows.append({"name": name.lstrip("/"), "memory_limit": int(limit or 0)})
    return rows


def inspect_running():
    """Every running container's name and memory limit, managed or not."""
    p = _docker(["ps", "-q", "--no-trunc"])
    ids = p.stdout.split() if p.returncode == 0 else []
    if not ids:
        return []
    p = _docker(["inspect", "--format", "{{.Name}}\t{{.HostConfig.Memory}}"] + ids)
    return parse_inspect(p.stdout) if p.returncode == 0 else []


def inspect_exists(name):
    return _docker(["inspect", "--format", "{{.Id}}", name]).returncode == 0


def read_mem_available():
    return memlog.parse_meminfo(memlog.read_meminfo())["MemAvailable"]


def read_memlog(cfg):
    return memlog.parse_last_sample(memlog.LOG, memlog.read_boot_id(),
                                    memlog.read_monotonic(), cfg.memlog_max_age_s)


def read_disk_free(cfg):
    """Free bytes on the host drive backing the guest and on the guest root,
    the same reading `cleanup.disk_free_gb` takes, None where unreadable."""
    return memlog.disk_free([cfg.disk_path, "/"])


def state_file():
    """Where `install` records the slice cap. Resolved per call, like the
    locks, so a test's RUNS override reaches it."""
    return config.RUNS / ".admission-state.json"


def project_lock_path(repo, lock_dir=None):
    h = locks.repo_hash(repo)
    return (config.LOCK_DIR if lock_dir is None else lock_dir) / f"milestone-{h}.lock"


# ---------------------------------------------------------------- the handle
class Admission:
    """What `acquire` hands the backend: the flock still held, the project
    lock if one was taken, and the decision it was admitted on."""

    def __init__(self, flock, project_lock, decision):
        self.flock = flock
        self.project_lock = project_lock
        self.decision = decision
        self.thread = None

    def release_when_visible(self, container_name, inspect_exists=inspect_exists,
                             timeout=VISIBLE_TIMEOUT_S, poll=0.1, max_poll=2.0,
                             sleep=time.sleep, now=time.monotonic):
        """Drop the flock once `docker inspect` sees the container, or after
        `timeout`: from then on the container itself counts in the budget.
        The poll starts fast and doubles, since the container usually appears
        within the first tick and a slow start should not cost sixty inspects."""
        def watch():
            deadline = now() + timeout
            delay = poll
            while now() < deadline and not inspect_exists(container_name):
                sleep(delay)
                delay = min(delay * 2, max_poll)
            self.flock.release()
        self.thread = threading.Thread(target=watch, daemon=True)
        self.thread.start()
        return self.thread

    def release(self):
        """Both locks; called from the backend's cleanup. Idempotent."""
        self.flock.release()
        if self.project_lock is not None:
            self.project_lock.release()


def _print(out, msg):
    print(f"[agent-sandbox] admission: {msg}", file=out or sys.stderr)


def describe(decision):
    """The R4 line: every effective number with where it came from."""
    n = decision.numbers
    parts = []
    for key in ("budget", "floor", "request", "committed", "mem_available", "headroom"):
        if key in n:
            parts.append(f"{key}={fmt(n[key]['bytes'])} ({n[key]['source']})")
    if "memlog" in n and n["memlog"].get("age_s") is not None:
        parts.append(f"memlog={n['memlog']['age_s']:.0f}s old")
    disks = [(k[len("disk_free:"):], v["bytes"]) for k, v in n.items()
             if k.startswith("disk_free:")]
    if disks:
        parts.append("disk_free=" + ",".join(
            f"{p}:{'unreadable' if b is None else fmt(b)}" for p, b in disks))
    if "disk_floor" in n:
        parts.append(f"disk_floor={fmt(n['disk_floor']['bytes'])} ({n['disk_floor']['source']})")
    if "project_lock" in n:
        parts.append("project_lock=" + ("held" if n["project_lock"]["held"] else "free"))
    return " ".join(parts)


def _record(rec, decision, entry_status=None, top_status=None):
    if rec is None:
        return
    rec.data["admission"] = {"verdict": decision.verdict, "reasons": decision.reasons,
                             "numbers": decision.numbers,
                             "decided_at": datetime.datetime.now(
                                 datetime.timezone.utc).isoformat()}
    if entry_status or top_status:
        rec.fail(entry_status=entry_status, top_status=top_status)
    rec.save()


def acquire(spec, cfg=None, *, force=False, wait=True, quiet=False,
            inspect_running=inspect_running, read_mem_available=read_mem_available,
            read_memlog=read_memlog, read_disk_free=read_disk_free,
            now=time.monotonic, sleep=time.sleep, lock_dir=None, out=None):
    """Hold the admission flock, decide, wait if that may help, and return an
    `Admission` handle (flock still held) or None when admission does not
    apply (disabled, or `force`).

    The loop releases and retakes the flock each interval (KTD2) so other
    launchers are not starved while this one waits. The record's newest
    container entry is marked waiting before the first sleep and failed on
    refusal or timeout; the caller's `RunRecord.start()` flips it to running.
    """
    cfg = resolve_settings() if cfg is None else cfg
    if force or not cfg.enabled:
        return None
    lock_dir = config.LOCK_DIR if lock_dir is None else lock_dir
    rec = spec.record
    tags = (rec.data.get("tags") or {}) if rec is not None else {}
    request = spec.resources.memory_bytes
    request_source = getattr(spec.resources, "memory_source", "config")
    project_lock = None
    if "milestone" in tags:
        repo = spec.workspace.repo or str(spec.workspace.path)
        project_lock = locks.PidLock(project_lock_path(repo, lock_dir),
                                     note=f"milestone {tags['milestone']} {repo}")

    flock = locks.FileFlock(lock_dir / ADMISSION_LOCK)
    started = now()
    deadline = started + cfg.wait_timeout_s
    waiting = False
    while True:
        flock.acquire()
        try:
            locked = None
            if project_lock is not None:
                h = project_lock.holder() if project_lock.is_held() else None
                locked = h[0] if h else False
            fresh = read_memlog(cfg)
            committed = committed_memory(inspect_running(), fresh.sample, cfg.budget)
            decision = decide(request, committed, read_mem_available(), fresh, locked, cfg,
                              request_source=request_source, disk_free=read_disk_free(cfg))
            if decision.verdict == "admit":
                if project_lock is not None:
                    project_lock.acquire()
                if not quiet:
                    _print(out, describe(decision))
                _record(rec, decision)
                return Admission(flock, project_lock, decision)
        except BaseException:
            flock.release()
            raise
        flock.release()

        if decision.verdict == "refuse" or not wait:
            _record(rec, decision, entry_status="failed", top_status="failed")
            raise AdmissionRefused(decision)
        elapsed = now() - started
        if now() >= deadline:
            _record(rec, decision, entry_status="failed", top_status="failed")
            raise AdmissionTimeout(decision, waited_s=elapsed)
        if not waiting:
            waiting = True
            if rec is not None:
                rec.wait()
        _record(rec, decision)
        if not quiet:
            _print(out, f"waiting ({'; '.join(decision.reasons)}); retry in "
                        f"{cfg.wait_interval_s:.0f} s, up to {cfg.wait_timeout_s:.0f} s; "
                        + describe(decision))
        sleep(min(cfg.wait_interval_s, max(0.0, deadline - now())))


# ---------------------------------------------------------------- the slice
def _systemctl(args):
    return subprocess.run(args, env=config.docker_env(), capture_output=True, text=True)


def install(budget_bytes, run=_systemctl, path=None, slice_name=resources.ResourceConfig.SLICE):
    """Cap the user slice every managed container runs in at the budget
    (KTD8), and record what happened so `doctor` and `admission show` can
    say when it was set."""
    path = state_file() if path is None else path
    args = ["systemctl", "--user", "set-property", slice_name, f"MemoryMax={budget_bytes}"]
    try:
        p = run(args)
        ok, output = p.returncode == 0, (p.stdout + p.stderr).strip()
    except FileNotFoundError as e:
        ok, output = False, str(e)
    state = {"slice": slice_name, "memory_max": budget_bytes, "ok": ok, "output": output,
             "command": " ".join(args),
             "set_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata._write_atomic(path, state)
    if not ok:
        raise SandboxError(f"could not set MemoryMax on {slice_name}: {output}",
                           "Check `systemctl --user is-system-running`; the user "
                           "manager must be up and cgroup v2 memory delegated.")
    return state


def read_state(path=None):
    path = state_file() if path is None else path
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None
