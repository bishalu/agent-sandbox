"""Paths, persistent config, and the flag > env > config > default ladder.

Every path the tool uses is resolved here so nothing else hardcodes a
location. Config precedence is deliberately explicit and testable.
"""

import json
import os
import pathlib

HOME = pathlib.Path(os.path.expanduser("~"))
ROOT = pathlib.Path(os.environ.get("AGENT_SANDBOX_HOME", HOME / "agent-sandbox"))

WORKTREES = ROOT / "worktrees"
RUNS = ROOT / "runs"
LOGS = ROOT / "logs"
CACHE = ROOT / "cache"
IMAGE_DIR = ROOT / "image"
DOCKERFILE = IMAGE_DIR / "Dockerfile"
CONFIG_FILE = ROOT / "config.json"
LOCK_DIR = RUNS / ".locks"
IMAGE_STATE = RUNS / ".image-state.json"

IMAGE_NAME = "agent-sandbox:base"

# Labels let `clean --docker` find exactly our containers and nothing else (R-14).
LABEL_MANAGED = "agent-sandbox.managed"
LABEL_ID = "agent-sandbox.id"

# Rootless Docker socket for this user. Resolved at import so a polluted
# inherited environment (WSL interop PATH, stray DOCKER_HOST) cannot leak in.
UID = os.getuid()
DEFAULT_DOCKER_HOST = f"unix:///run/user/{UID}/docker.sock"

DEFAULTS = {
    "mode": "safe",
    "cpus": "8",
    "memory": "16g",
    "pids": 2048,
    "timeout": "12h",
    "network": "full",
    "image": IMAGE_NAME,
    "disk_warn_gb": 40,
    # Persistent agent home (R-18): the template every new sandbox's
    # /root/.claude is seeded from. Relative to nothing; give an absolute path.
    "agent_home_template": str(ROOT / "templates" / "agent-home"),
    # Host skill directories mounted read-only into every sandbox's
    # /root/.claude/skills/<basename> (R-19). List-valued: edit config.json
    # by hand; `config set` handles scalars only.
    "skill_mounts": [],
    # Gitignored paths copied from the source checkout into every new worktree
    # (R-23). A worktree starts with tracked files only, so a repo's `.env`
    # never arrives on its own. Repo-relative files or directories; only
    # entries git ignores are copied. List-valued: edit config.json by hand.
    "worktree_seed": [],
    # Mirror the host's Claude Code plugins into every sandbox (R-24): the
    # registries are copied into the agent home and the cache and marketplace
    # directories mount read-only at their host paths.
    "mirror_plugins": True,
    # The memory log's staleness rule (KTD5): a launch refuses when the newest
    # sample is older than this many seconds by the monotonic clock. Three
    # missed one-minute ticks; the timer's own cadence is not configurable.
    "admission_memlog_max_age_s": 180,
    # Admission control (R1, KTD1): off by default so SPEC R-13 (no global
    # locking) stays true unless a host opts in. The budget counts every
    # running container's limit; the floor is MemAvailable the host keeps.
    "admission_enabled": False,
    "admission_memory_budget": "32g",
    "admission_mem_floor_gb": 8,
    "admission_wait_timeout_s": 1800,
    "admission_wait_interval_s": 30,
    # Disk guard (KTD11): a launch refuses, at once, when the host drive
    # backing the guest or the guest root has less than this many GiB free.
    # The 16:34 reboot was the Windows drive holding ext4.vhdx at 1.1 GB,
    # not memory. `admission_disk_path` None means auto: /mnt/c when
    # /proc/version names Microsoft (WSL), else /.
    "admission_disk_floor_gb": 20,
    "admission_disk_path": None,
    # Swap allowance per container; None means "equal to memory", so a
    # container cannot spill into the VM's swap (KTD8).
    "memory_swap": None,
}

_ENV = {
    "mode": "AGENT_SANDBOX_MODE",
    "cpus": "AGENT_SANDBOX_CPUS",
    "memory": "AGENT_SANDBOX_MEMORY",
    "pids": "AGENT_SANDBOX_PIDS",
    "timeout": "AGENT_SANDBOX_TIMEOUT",
    "network": "AGENT_SANDBOX_NETWORK",
    "image": "AGENT_SANDBOX_IMAGE",
    "admission_memlog_max_age_s": "AGENT_SANDBOX_ADMISSION_MEMLOG_MAX_AGE_S",
    "admission_enabled": "AGENT_SANDBOX_ADMISSION_ENABLED",
    "admission_memory_budget": "AGENT_SANDBOX_ADMISSION_MEMORY_BUDGET",
    "admission_mem_floor_gb": "AGENT_SANDBOX_ADMISSION_MEM_FLOOR_GB",
    "admission_wait_timeout_s": "AGENT_SANDBOX_ADMISSION_WAIT_TIMEOUT_S",
    "admission_wait_interval_s": "AGENT_SANDBOX_ADMISSION_WAIT_INTERVAL_S",
    "admission_disk_floor_gb": "AGENT_SANDBOX_ADMISSION_DISK_FLOOR_GB",
    "admission_disk_path": "AGENT_SANDBOX_ADMISSION_DISK_PATH",
    "memory_swap": "AGENT_SANDBOX_MEMORY_SWAP",
}

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}

PROC_VERSION = pathlib.Path("/proc/version")
WSL_HOST_DISK = "/mnt/c"


def ensure_dirs():
    for d in (ROOT, WORKTREES, RUNS, LOGS, LOCK_DIR, IMAGE_DIR,
              CACHE / "npm", CACHE / "pnpm", CACHE / "pip", CACHE / "uv"):
        d.mkdir(parents=True, exist_ok=True)


def load_config():
    """Persistent user config. Missing or corrupt file falls back to defaults."""
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
            if isinstance(data, dict):
                return data
        except (ValueError, OSError):
            pass
    return {}


def save_config(data):
    ensure_dirs()
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(CONFIG_FILE)


def resolve_source(key, flag_value=None, config=None):
    """(value, tier) with tier one of flag, env, config, default: the same
    ladder as `resolve`, keeping the tier so R4 can say where a number came from."""
    if flag_value is not None:
        return flag_value, "flag"
    env_name = _ENV.get(key)
    if env_name and os.environ.get(env_name):
        return os.environ[env_name], "env"
    cfg = load_config() if config is None else config
    if key in cfg and cfg[key] is not None:
        return cfg[key], "config"
    return DEFAULTS.get(key), "default"


def resolve(key, flag_value=None, config=None):
    """flag > env > config.json > built-in default."""
    return resolve_source(key, flag_value, config)[0]


def read_proc_version():
    try:
        return PROC_VERSION.read_text()
    except OSError:
        return ""


def host_disk_path(cfg=None, read_version=None):
    """The host drive backing the guest, for the disk guard (KTD11).

    `admission_disk_path` when set (flag > env > config); otherwise auto:
    /mnt/c when /proc/version names Microsoft, because under WSL the guest
    root is a file (ext4.vhdx) on the Windows drive and that drive filling
    is what kills the VM; / anywhere else.
    """
    return host_disk_path_source(cfg, read_version)[0]


def host_disk_path_source(cfg=None, read_version=None):
    """(path, tier) with tier "auto" when the path was derived, not set."""
    path, tier = resolve_source("admission_disk_path", None, cfg)
    if path:
        return str(path), tier
    version = (read_version or read_proc_version)()
    return (WSL_HOST_DISK if "microsoft" in version.lower() else "/"), "auto"


CGROUP_USER_SLICE = pathlib.Path("/sys/fs/cgroup/user.slice")


def user_manager_cgroup(*parts, uid=None):
    """A path under this user's systemd user manager in the cgroup v2 tree:
    /sys/fs/cgroup/user.slice/user-<uid>.slice/user@<uid>.service/<parts>.
    Rootless Docker's container scopes, the delegated controllers file and
    agent-sandbox.slice all live there; this is the one place the shape is
    spelled out."""
    uid = UID if uid is None else uid
    return CGROUP_USER_SLICE.joinpath(f"user-{uid}.slice", f"user@{uid}.service", *parts)


def as_bool(value):
    """Booleans arrive as JSON booleans from config.json and as strings from
    the environment; both must read the same way."""
    if isinstance(value, bool) or value is None:
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"not a boolean: {value!r}")


def parse_value(text):
    """How `config set` reads a value: true/false/null and integers become
    themselves, anything else stays a string. None means "unset the key"."""
    low = text.strip().lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low == "null":
        return None
    if text.isdigit():
        return int(text)
    return text


def run_docker(args):
    """One docker CLI call with the rootless daemon's environment, output
    captured. Shared by every module that reads the daemon without owning a
    backend (cleanup, admission, memlog)."""
    import subprocess
    return subprocess.run(["docker"] + args, env=docker_env(),
                          capture_output=True, text=True)


def docker_env():
    """A clean environment for docker subprocesses.

    WSL2 inherits a $PATH containing Windows interop entries with spaces,
    which has already broken systemd unit parsing on this machine. Docker
    invocations get a deliberately minimal, predictable environment.
    """
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(HOME),
        "XDG_RUNTIME_DIR": f"/run/user/{UID}",
        "DOCKER_HOST": os.environ.get("DOCKER_HOST", DEFAULT_DOCKER_HOST),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
