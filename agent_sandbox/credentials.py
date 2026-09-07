"""Credential resolution for sandboxed agents (spec R-07).

The ladder, in order, stopping at the first layer that can work:

  1. mount ~/.claude/.credentials.json read-only, alone
  2. a narrowly constructed sandbox-specific Claude config with only the
     fields that are actually required
  3. ANTHROPIC_API_KEY environment injection, no file mount at all
  4. --with-full-claude-state: mount ~/.claude.json read-only (opt-in ONLY)

Layer 4 is never reached automatically. ~/.claude.json carries project
history, MCP server config, and machine/account identifiers; that is
information exposure even read-only, so it requires an explicit flag.

Nothing is ever baked into the image, and nothing is copied into the repo.
"""

import json
import os
import pathlib
import subprocess

from . import config
from .errors import CredentialError

CLAUDE_DIR = config.HOME / ".claude"
CREDENTIALS_FILE = CLAUDE_DIR / ".credentials.json"
CLAUDE_STATE_FILE = config.HOME / ".claude.json"

# Where the container's HOME lives. Claude Code inside the container reads
# from here; we populate only what is needed.
CONTAINER_HOME = "/root"


class Credentials:
    """Resolved credential plan: docker mounts, env vars, and a disclosure."""

    def __init__(self):
        self.mounts = []        # list of docker -v arguments
        self.env = {}           # env vars passed with -e
        self.exposed = []       # human-readable disclosure lines
        self.layer = None       # which ladder rung was used
        self.claude_available = False
        self.github = False

    def docker_args(self):
        args = []
        for m in self.mounts:
            args += ["-v", m]
        for k, v in self.env.items():
            args += ["-e", f"{k}={v}"]
        return args

    def describe(self):
        if not self.exposed:
            return ["nothing from the host is exposed"]
        return list(self.exposed)

    def to_dict(self):
        return {
            "layer": self.layer,
            "claude": self.claude_available,
            "github": self.github,
            # Never record values, only what was exposed.
            "exposed": self.exposed,
            "env_names": sorted(self.env.keys()),
        }


def _api_key_from_env():
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"):
        v = os.environ.get(name)
        if v:
            return name, v
    return None, None


def sanitized_claude_state():
    """The minimal Claude config dict built from host state (layer 2).

    Copies only the fields Claude Code needs to consider itself configured,
    deliberately excluding projects, history, MCP servers, and identifiers.
    Shared by the read-only generated file below and by the per-sandbox
    writable .claude.json in agent_home.py (R-18).
    """
    keep = ("hasCompletedOnboarding", "theme", "autoUpdates",
            "hasTrustDialogAccepted", "installMethod")
    out = {}
    if CLAUDE_STATE_FILE.exists():
        try:
            data = json.loads(CLAUDE_STATE_FILE.read_text())
            for k in keep:
                if k in data:
                    out[k] = data[k]
        except (ValueError, OSError):
            pass
    out.setdefault("hasCompletedOnboarding", True)
    return out


def _sanitized_claude_config(dest_dir):
    """Layer 2 (no agent home): write the minimal config for a read-only mount."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / "claude.json"
    path.write_text(json.dumps(sanitized_claude_state(), indent=2) + "\n")
    path.chmod(0o600)
    return path


def resolve(sandbox_dir=None, with_github=False, with_full_claude_state=False,
            force_layer=None, agent_home=None):
    """Build the credential plan for one run.

    sandbox_dir: per-run scratch dir for generated config (layer 2).
    agent_home:  when the sandbox has a persistent agent home (R-18), the
                 layer-2 config lives inside it as a writable per-sandbox
                 .claude.json instead of a read-only generated mount, so
                 Claude Code can persist trust and bypass acknowledgements.
    """
    c = Credentials()

    # --- Claude ------------------------------------------------------
    env_name, env_val = _api_key_from_env()

    if force_layer == 3 or (env_val and force_layer is None and
                            not CREDENTIALS_FILE.exists()):
        # Layer 3: API key injection, no file mount.
        if not env_val:
            raise CredentialError(
                "no ANTHROPIC_API_KEY in the environment",
                "export ANTHROPIC_API_KEY=... or log in with `claude` on the host.",
            )
        c.env["ANTHROPIC_API_KEY"] = env_val
        c.layer = 3
        c.claude_available = True
        c.exposed.append(
            "ANTHROPIC_API_KEY (environment variable, value not written to disk)")

    elif with_full_claude_state:
        # Layer 4: explicit opt-in only.
        if not CLAUDE_STATE_FILE.exists():
            raise CredentialError(
                f"{CLAUDE_STATE_FILE} does not exist",
                "Log in with `claude` on the host first.",
            )
        c.mounts.append(f"{CREDENTIALS_FILE}:{CONTAINER_HOME}/.claude/.credentials.json:ro")
        c.mounts.append(f"{CLAUDE_STATE_FILE}:{CONTAINER_HOME}/.claude.json:ro")
        c.layer = 4
        c.claude_available = True
        c.exposed.append(f"{CREDENTIALS_FILE} (read-only) — Claude OAuth token")
        c.exposed.append(
            f"{CLAUDE_STATE_FILE} (read-only) — FULL Claude state: project history, "
            "MCP server config, account and machine identifiers")

    elif CREDENTIALS_FILE.exists():
        # Layer 1: the credential file alone. Preferred.
        c.mounts.append(f"{CREDENTIALS_FILE}:{CONTAINER_HOME}/.claude/.credentials.json:ro")
        c.layer = 1
        c.claude_available = True
        c.exposed.append(f"{CREDENTIALS_FILE} (read-only) — Claude OAuth token only")

        # Layer 2 companion: a sanitized config so Claude Code does not treat
        # the container as a fresh unonboarded install. Contains no secrets
        # and no host project data.
        if agent_home is not None:
            c.layer = 2
            c.exposed.append(
                f"{agent_home.claude / '.claude.json'} (read-write, per sandbox) — "
                "generated minimal Claude config (onboarding flags and /workspace "
                "trust only, no host project data)")
        elif sandbox_dir:
            cfg = _sanitized_claude_config(pathlib.Path(sandbox_dir))
            c.mounts.append(f"{cfg}:{CONTAINER_HOME}/.claude.json:ro")
            c.layer = 2
            c.exposed.append(
                f"{cfg} (read-only) — generated minimal Claude config "
                "(onboarding flags only, no host project data)")

    elif env_val:
        c.env["ANTHROPIC_API_KEY"] = env_val
        c.layer = 3
        c.claude_available = True
        c.exposed.append(
            "ANTHROPIC_API_KEY (environment variable, value not written to disk)")
    else:
        c.layer = 0
        c.claude_available = False

    # --- GitHub ------------------------------------------------------
    # Off by default. No SSH keys, ever (R-02).
    if with_github:
        p = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        token = p.stdout.strip()
        if p.returncode != 0 or not token:
            raise CredentialError(
                "could not read a GitHub token from `gh auth token`",
                "Run `gh auth login` on the host, or drop --with-github-auth.",
            )
        c.env["GH_TOKEN"] = token
        c.env["GITHUB_TOKEN"] = token
        c.github = True
        c.exposed.append(
            "GH_TOKEN / GITHUB_TOKEN (environment) — your GitHub token, "
            "scoped as your host `gh` login")

    return c
