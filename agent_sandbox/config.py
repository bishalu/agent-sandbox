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
}

_ENV = {
    "mode": "AGENT_SANDBOX_MODE",
    "cpus": "AGENT_SANDBOX_CPUS",
    "memory": "AGENT_SANDBOX_MEMORY",
    "pids": "AGENT_SANDBOX_PIDS",
    "timeout": "AGENT_SANDBOX_TIMEOUT",
    "network": "AGENT_SANDBOX_NETWORK",
    "image": "AGENT_SANDBOX_IMAGE",
}


def ensure_dirs():
    for d in (ROOT, WORKTREES, RUNS, LOGS, LOCK_DIR, IMAGE_DIR,
              CACHE / "npm", CACHE / "pnpm", CACHE / "pip"):
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


def resolve(key, flag_value=None, config=None):
    """flag > env > config.json > built-in default."""
    if flag_value is not None:
        return flag_value
    env_name = _ENV.get(key)
    if env_name and os.environ.get(env_name):
        return os.environ[env_name]
    cfg = load_config() if config is None else config
    if key in cfg and cfg[key] is not None:
        return cfg[key]
    return DEFAULTS.get(key)


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
