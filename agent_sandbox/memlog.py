"""The standing memory log the launcher fails closed on (R8, KTD5).

A systemd user timer appends one sample a minute to `LOGS/memory.log`. Each
line carries the boot id and a monotonic timestamp so a reader can tell a
sample from a previous boot, or one older than it looks, apart from a fresh
one; wall time is recorded for humans, never trusted for age. Per-container
memory comes from each container's `memory.current` in the user's delegated
cgroup tree because `docker stats` takes seconds on this host; it is the
fallback only when that file is absent. Each line also carries `disk_free`
for the host drive backing the guest and the guest root (KTD11), so the log
shows a disk filling up the way it shows memory going.

Every reader is a parameter with a default, so the pure parts run under
pytest without Docker or a cgroup tree (KTD9).
"""

import dataclasses
import datetime
import os
import pathlib
import re
import shutil
import subprocess
import time

from . import config
from .errors import SandboxError
from .resources import ResourceConfig

LOG = config.LOGS / "memory.log"
ROTATE_AT = 20000
ROTATE_KEEP = 10000

BOOT_ID_FILE = pathlib.Path("/proc/sys/kernel/random/boot_id")
MEMINFO_FILE = pathlib.Path("/proc/meminfo")
# Rootless Docker puts each container in a transient scope under the user
# manager's tree; that is where the delegated memory controller lives. With
# admission off the scope sits under user.slice; with admission on every
# managed container runs under agent-sandbox.slice (KTD8), so both roots are
# tried before the slow `docker stats` fallback.
CGROUP_ROOT = config.user_manager_cgroup("user.slice")
SLICE_CGROUP_ROOT = config.slice_cgroup(ResourceConfig.SLICE)
CGROUP_ROOTS = (CGROUP_ROOT, SLICE_CGROUP_ROOT)

TEMPLATE_DIR = pathlib.Path(__file__).resolve().parent.parent / "templates" / "systemd"
BIN = pathlib.Path(__file__).resolve().parent.parent / "bin" / "agent-sandbox"
UNIT_DIR = config.HOME / ".config" / "systemd" / "user"
SERVICE = "agent-sandbox-memlog.service"
TIMER = "agent-sandbox-memlog.timer"

_STATS_UNITS = {"b": 1, "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3,
                "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3}


@dataclasses.dataclass
class Sample:
    boot_id: str
    monotonic: float
    time: str
    mem_available: int
    swap_free: int
    containers: dict
    # path -> free bytes, None when the path could not be read (KTD11).
    # Empty for lines written before the field existed.
    disk_free: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class Freshness:
    """What the launcher decides on: `fresh`, and `reason` when it is not."""
    fresh: bool
    reason: str
    age_s: float = None
    sample: Sample = None


# ---------------------------------------------------------------- readers
def read_boot_id():
    return BOOT_ID_FILE.read_text().strip()


def read_monotonic():
    return time.clock_gettime(time.CLOCK_MONOTONIC)


def read_meminfo():
    return MEMINFO_FILE.read_text()


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


_docker = config.run_docker


def docker_ps():
    """(full id, name) for every running container, managed or not.

    Full ids: the cgroup scope is `docker-<64 hex>.scope`, and the budget
    counts every container on the host (KTD1), not only ours.
    """
    p = _docker(["ps", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}"])
    if p.returncode != 0:
        return []
    rows = []
    for line in p.stdout.splitlines():
        parts = line.strip().split("\t")
        if len(parts) == 2:
            rows.append((parts[0], parts[1]))
    return rows


def docker_stats():
    """name -> bytes from `docker stats --no-stream`; slow, so a fallback only."""
    p = _docker(["stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}"])
    return parse_docker_stats(p.stdout) if p.returncode == 0 else {}


def parse_docker_stats(text):
    out = {}
    for line in text.splitlines():
        parts = line.strip().split("\t")
        if len(parts) != 2:
            continue
        m = re.match(r"([\d.]+)\s*([A-Za-z]+)", parts[1].split("/")[0].strip())
        if m and m.group(2).lower() in _STATS_UNITS:
            out[parts[0]] = int(float(m.group(1)) * _STATS_UNITS[m.group(2).lower()])
    return out


def parse_meminfo(text):
    """MemAvailable and SwapFree in bytes. /proc/meminfo reports kB (KiB)."""
    want = {"MemAvailable", "SwapFree"}
    out = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key in want:
            out[key] = int(rest.split()[0]) * 1024
    missing = want - set(out)
    if missing:
        raise ValueError(f"meminfo lacks {', '.join(sorted(missing))}")
    return out


def disk_free(paths, usage=shutil.disk_usage):
    """path -> free bytes for each distinct path; None where the path cannot
    be read, so a missing mount is a reading the guard refuses on, not a
    silent skip (KTD11)."""
    out = {}
    for path in dict.fromkeys(str(p) for p in paths):
        try:
            out[path] = int(usage(path).free)
        except OSError:
            out[path] = None
    return out


def disk_paths(cfg=None):
    """The host drive backing the guest, then the guest root."""
    return list(dict.fromkeys([config.host_disk_path(cfg), "/"]))


def read_disk_free(cfg=None):
    return disk_free(disk_paths(cfg))


def _scope_memory(cid, cgroup_roots):
    """memory.current for `docker-<cid>.scope` under the first root that has
    it; None when no root does."""
    for root in cgroup_roots:
        f = pathlib.Path(root) / f"docker-{cid}.scope" / "memory.current"
        try:
            return int(f.read_text().strip())
        except (OSError, ValueError):
            continue
    return None


def container_memory(containers, cgroup_roots=CGROUP_ROOTS, docker_stats=docker_stats):
    """name -> bytes, from cgroup files under any of `cgroup_roots`;
    `docker stats` once for any that lack one."""
    out, missing = {}, []
    for cid, name in containers:
        used = _scope_memory(cid, cgroup_roots)
        if used is None:
            missing.append(name)
        else:
            out[name] = used
    if missing:
        stats = docker_stats()
        for name in missing:
            if name in stats:
                out[name] = stats[name]
    return out


# ---------------------------------------------------------------- line format
def format_line(s):
    """One line, key=value, greppable; containers as name=bytes joined by commas."""
    containers = ",".join(f"{n}={b}" for n, b in s.containers.items())
    disks = ",".join(f"{p}={'none' if b is None else b}" for p, b in s.disk_free.items())
    return (f"boot={s.boot_id} mono={s.monotonic:.3f} time={s.time} "
            f"mem_available={s.mem_available} swap_free={s.swap_free} "
            f"containers={containers} disk_free={disks}")


def parse_line(line):
    """Inverse of format_line. Raises ValueError on anything it did not write."""
    fields = {}
    for tok in line.strip().split(" "):
        key, eq, val = tok.partition("=")
        if not eq:
            raise ValueError(f"not a key=value token: {tok!r}")
        fields[key] = val
    try:
        containers = {}
        if fields["containers"]:
            for item in fields["containers"].split(","):
                name, _, b = item.rpartition("=")
                containers[name] = int(b)
        disks = {}
        # Older lines have no disk_free; they still parse (KTD11 landed later).
        if fields.get("disk_free"):
            for item in fields["disk_free"].split(","):
                path, _, b = item.rpartition("=")
                disks[path] = None if b == "none" else int(b)
        return Sample(boot_id=fields["boot"], monotonic=float(fields["mono"]),
                      time=fields["time"], mem_available=int(fields["mem_available"]),
                      swap_free=int(fields["swap_free"]), containers=containers,
                      disk_free=disks)
    except KeyError as e:
        raise ValueError(f"missing field {e.args[0]}")


# ---------------------------------------------------------------- writing
def sample(path=LOG, *, read_boot_id=read_boot_id, read_monotonic=read_monotonic,
           read_meminfo=read_meminfo, docker_ps=docker_ps, cgroup_roots=CGROUP_ROOTS,
           docker_stats=docker_stats, now=now_iso, read_disk_free=read_disk_free):
    """Append one sample to `path`, rotating afterwards. Returns the line."""
    mem = parse_meminfo(read_meminfo())
    s = Sample(boot_id=read_boot_id(), monotonic=read_monotonic(), time=now(),
               mem_available=mem["MemAvailable"], swap_free=mem["SwapFree"],
               containers=container_memory(docker_ps(), cgroup_roots, docker_stats),
               disk_free=read_disk_free())
    line = format_line(s)
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(line + "\n")
    rotate(path)
    return line


def rotate(path, limit=ROTATE_AT, keep=ROTATE_KEEP):
    """Past `limit` lines, keep the newest `keep`. Atomic, so a reader
    between the two writes still sees a whole file."""
    path = pathlib.Path(path)
    try:
        lines = path.read_text().splitlines(keepends=True)
    except OSError:
        return
    if len(lines) <= limit:
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(lines[-keep:]))
    tmp.replace(path)


# ---------------------------------------------------------------- reading
def _last_line(path):
    """The last non-blank line, or None. Reads the tail only; the log is
    appended a minute at a time and may hold twenty thousand lines."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        if line.strip():
            return line
    return None


def parse_last_sample(path, boot_id, monotonic_now, max_age_s):
    """The newest sample and whether the launcher may trust it.

    Fail closed: a missing or empty log, a malformed last line, another boot
    id, or a monotonic age over `max_age_s` (or negative) is stale. The
    earlier lines are never consulted; a broken writer must be noticed, not
    papered over.
    """
    path = pathlib.Path(path)
    if not path.exists():
        return Freshness(False, f"memory log missing at {path}")
    last = _last_line(path)
    if last is None:
        return Freshness(False, f"memory log empty at {path}")
    try:
        s = parse_line(last)
    except ValueError as e:
        return Freshness(False, f"memory log last line malformed: {e}")
    age = monotonic_now - s.monotonic
    if s.boot_id != boot_id:
        return Freshness(False, "last sample is from another boot", age, s)
    if age < 0:
        return Freshness(False, f"last sample is {-age:.0f} s in the future", age, s)
    if age > max_age_s:
        return Freshness(False, f"last sample is {age:.0f} s old, over the {max_age_s} s maximum",
                         age, s)
    return Freshness(True, "fresh", age, s)


def iter_samples(path):
    """Every parseable sample in order; malformed lines are skipped."""
    try:
        text = pathlib.Path(path).read_text()
    except OSError:
        return
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            yield parse_line(line)
        except ValueError:
            continue


def minimum_since(path, since_monotonic, boot_id):
    """(smallest MemAvailable, its wall time) over this boot's samples at or
    after `since_monotonic`; None when there are none. This is the number
    that settles whether a run starved the host (R8)."""
    best = None
    for s in iter_samples(path):
        if s.boot_id != boot_id or s.monotonic < since_monotonic:
            continue
        if best is None or s.mem_available < best[0]:
            best = (s.mem_available, s.time)
    return best


def peak_containers(path, since_monotonic, boot_id):
    """Per container, (largest memory.current, its wall time) over this boot's
    samples at or after `since_monotonic`. This is what sizes the next
    request for the same job: a milestone that peaked at 3 GB does not need
    the 8 GB default, and the budget it leaves admits another run."""
    peaks = {}
    for s in iter_samples(path):
        if s.boot_id != boot_id or s.monotonic < since_monotonic:
            continue
        for name, used in s.containers.items():
            if name not in peaks or used > peaks[name][0]:
                peaks[name] = (used, s.time)
    return peaks


def summarize_since(path, since_monotonic, boot_id, last=5):
    """One pass over the log: (minimum MemAvailable, per-container peaks,
    the newest `last` samples). `show` calls this instead of reading the
    file three times; the two single-purpose functions stay for admission."""
    import collections
    minimum, peaks = None, {}
    tail = collections.deque(maxlen=last if last > 0 else 0)
    for s in iter_samples(path):
        tail.append(s)
        if s.boot_id != boot_id or s.monotonic < since_monotonic:
            continue
        if minimum is None or s.mem_available < minimum[0]:
            minimum = (s.mem_available, s.time)
        for name, used in s.containers.items():
            if name not in peaks or used > peaks[name][0]:
                peaks[name] = (used, s.time)
    return minimum, peaks, list(tail)


def suggest_memory(peak_bytes, headroom=1.5, step=512 * 1024 ** 2, floor=1024 ** 3):
    """A --memory value from a measured peak: the peak plus half again,
    rounded up to 512 MiB, never below 1 GiB. Returned as a docker size
    string ("3.5g" or "2g")."""
    want = max(int(peak_bytes * headroom), floor)
    steps = -(-want // step)
    gib = steps * step / 1024 ** 3
    return f"{gib:g}g"


def max_age_s(cfg=None):
    return int(config.resolve("admission_memlog_max_age_s", None, cfg))


# ---------------------------------------------------------------- systemd
def _systemctl(args):
    subprocess.run(args, check=True, env=config.docker_env(), capture_output=True, text=True)


def install(unit_dir=UNIT_DIR, bin_path=BIN, run=_systemctl):
    """Write the two units from templates and enable the timer now."""
    unit_dir = pathlib.Path(unit_dir)
    unit_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name in (SERVICE, TIMER):
        src = TEMPLATE_DIR / name
        if not src.is_file():
            raise SandboxError(f"missing template {src}",
                               "The checkout is incomplete; restore templates/systemd/.")
        text = src.read_text().replace("@BIN@", str(bin_path))
        dst = unit_dir / name
        dst.write_text(text)
        written.append(dst)
    try:
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "enable", "--now", TIMER])
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise SandboxError(
            f"could not enable {TIMER}: {getattr(e, 'stderr', '') or e}",
            "Check `systemctl --user is-system-running`; the user manager must be up.")
    return written


def timer_active():
    try:
        p = subprocess.run(["systemctl", "--user", "is-active", TIMER],
                           capture_output=True, text=True, env=config.docker_env())
    except FileNotFoundError:
        return False
    return p.stdout.strip() == "active"


def health(cfg=None, path=LOG):
    """(timer active, Freshness of the last sample) for doctor and admission."""
    return timer_active(), parse_last_sample(path, read_boot_id(), read_monotonic(),
                                             max_age_s(cfg))
