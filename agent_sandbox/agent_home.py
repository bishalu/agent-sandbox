"""Persistent per-sandbox agent home (spec R-18).

A sandbox id outlives its containers; with 1.0 the agent's own state
(`/root/.claude`, `/root/.pi/agent`) died with each container, so nothing
resumed. Every sandbox now owns `runs/<id>/agent-home/{claude,pi-agent}`,
bind-mounted read-write at those two paths and seeded exactly once:

  claude/    settings.json from the shipped template (bypass mode inside the
             sandbox, a deny list for what the sandbox cannot undo), a
             generated .claude.json (onboarding flags plus /workspace trust,
             no host project data), an empty 0600 .credentials.json so the
             read-only credentials mount never lands on a world-readable
             mountpoint, and an empty skills/ for read-only skill mounts.
  pi-agent/  the image's Pi template: the bridge package, its settings.json
             package entry, claude-bridge.json, and an empty models.json.
  seed.json  the image fingerprint and pinned tool versions at seed time, so
             `enter` and `doctor` can warn when the image has moved on.

Seeding is idempotent: `enter` never overwrites an existing home. Removal is
`agent-sandbox rm`, which already deletes runs/<id>/. The whole tree is
created 0700; transcripts of the operator and every worker live here.
"""

import json
import os
import pathlib
import shutil
import subprocess

from . import config, credentials, image
from .errors import ImageError, MountError
from .mounts import CONTAINER_HOME, Mount

DIRNAME = "agent-home"
SEED_FILE = "seed.json"
IMAGE_PI_TEMPLATE = "/opt/agent-sandbox/pi-agent-template"
DEFAULT_TEMPLATE = config.ROOT / "templates" / "agent-home"


class AgentHome:
    def __init__(self, sandbox_id, path):
        self.sandbox_id = sandbox_id
        self.path = pathlib.Path(path)
        self.claude = self.path / "claude"
        self.pi = self.path / "pi-agent"
        self.seed_file = self.path / SEED_FILE

    @property
    def exists(self):
        """Fully seeded: both homes and the seed record. A home whose seeding
        died halfway (docker cp failed, disk full) has no seed record and is
        rebuilt on the next run instead of being trusted forever."""
        return self.claude.is_dir() and self.pi.is_dir() and self.seed_file.is_file()

    @property
    def partial(self):
        return self.path.exists() and not self.exists

    def seed(self):
        try:
            return json.loads(self.seed_file.read_text())
        except (ValueError, OSError):
            return {}

    def mounts(self):
        return [
            Mount(self.claude, f"{CONTAINER_HOME}/.claude", purpose="agent-home"),
            Mount(self.pi, f"{CONTAINER_HOME}/.pi/agent", purpose="agent-home"),
        ]

    def drift(self, img=None):
        """A warning line when the home was seeded from a different image."""
        seed = self.seed()
        if not seed:
            return None
        try:
            current = image.fingerprint()
        except ImageError:
            return None
        if seed.get("image_fingerprint") == current:
            return None
        return (f"agent home of {self.sandbox_id} was seeded from an older image "
                f"({seed.get('seeded_at', '?')}); tools inside it may not match. "
                f"Re-seed with: agent-sandbox rm {self.sandbox_id} && agent-sandbox <repo>")


def home_path(sandbox_id):
    return config.RUNS / sandbox_id / DIRNAME


def _mkdir_private(path):
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _template_dir(cfg):
    raw = config.resolve("agent_home_template", None, cfg) or str(DEFAULT_TEMPLATE)
    path = pathlib.Path(os.path.expanduser(str(raw))).resolve()
    if not (path / "claude" / "settings.json").is_file():
        raise MountError(
            f"agent home template is missing claude/settings.json: {path}",
            "Point agent_home_template in ~/agent-sandbox/config.json at a "
            "directory containing claude/settings.json, or remove the key to "
            f"use the shipped template at {DEFAULT_TEMPLATE}.",
        )
    return path


def _claude_state():
    """The generated .claude.json: onboarding flags plus /workspace trust.

    Same field allow-list as the read-only layer-2 file (credentials.py),
    so nothing from the host's project history, MCP config, or identifiers
    is copied; the difference is that this file is per sandbox and writable,
    so Claude Code can persist trust and bypass acknowledgements.
    """
    state = credentials.sanitized_claude_state()
    state.setdefault("projects", {})
    state["projects"].setdefault("/workspace", {})
    state["projects"]["/workspace"]["hasTrustDialogAccepted"] = True
    return state


def _pinned_versions():
    out = {}
    for label, path in (("toolchain", config.IMAGE_DIR / "toolchain" / "package.json"),
                        ("pi-agent", config.IMAGE_DIR / "pi-agent-template" / "npm" / "package.json")):
        try:
            deps = json.loads(path.read_text()).get("dependencies", {})
            out.update(deps)
        except (ValueError, OSError):
            pass
    return out


def _extract_pi_template(img, dest):
    """Copy the image's Pi template out through a throwaway container.

    The template lives outside /root in the image because anything under
    /root is shadowed by the runtime bind at /root/.pi/agent (and by the
    read-only-root tmpfs). `docker cp` needs no running process.
    """
    env = config.docker_env()
    p = subprocess.run(["docker", "create", "--label", f"{config.LABEL_MANAGED}=true",
                        img, "true"], env=env, capture_output=True, text=True)
    if p.returncode != 0:
        raise MountError(
            f"could not create a container from {img} to seed the Pi home:\n"
            f"  {p.stderr.strip()[:300]}",
            "Run `agent-sandbox build` and retry.",
        )
    cid = p.stdout.strip()
    try:
        p = subprocess.run(["docker", "cp", f"{cid}:{IMAGE_PI_TEMPLATE}/.", str(dest)],
                           env=env, capture_output=True, text=True)
        if p.returncode != 0:
            raise MountError(
                f"image {img} has no Pi template at {IMAGE_PI_TEMPLATE}:\n"
                f"  {p.stderr.strip()[:300]}",
                "The image predates 1.1. Run `agent-sandbox build`.",
            )
    finally:
        subprocess.run(["docker", "rm", "-f", cid], env=env,
                       capture_output=True, text=True)


def ensure(sandbox_id, cfg=None, img=None, quiet=False):
    """Create the home once; return it. Never touches an existing home."""
    cfg = config.load_config() if cfg is None else cfg
    img = img or config.IMAGE_NAME
    home = AgentHome(sandbox_id, home_path(sandbox_id))
    if home.exists:
        return home
    if home.partial:
        # A previous seeding did not finish; nothing in it was ever used by a
        # container (the container only starts after ensure() returns), so
        # start over rather than serve a home with no bridge and no record.
        # Set aside rather than delete: if the seed record ever goes missing
        # from a used home, nothing is lost, and `rm` still sweeps runs/<id>.
        aside = home.path.with_name(f"{DIRNAME}.broken-{_now().replace(':', '')}")
        home.path.rename(aside)
        if not quiet:
            print(f"[agent-sandbox] re-seeding incomplete agent home "
                  f"(previous tree kept at {aside})", flush=True)

    template = _template_dir(cfg)
    _mkdir_private(home.path)

    # Claude home. Files copied from the template keep their content; the
    # directory tree is private to the invoking user.
    shutil.copytree(template / "claude", home.claude)
    _mkdir_private(home.claude)
    _mkdir_private(home.claude / "skills")
    _mkdir_private(home.claude / "projects")
    state_file = home.claude / ".claude.json"
    state_file.write_text(json.dumps(_claude_state(), indent=2) + "\n")
    state_file.chmod(0o600)
    # Valid JSON, not zero bytes: under credential layer 3 or 0 nothing is
    # mounted over it and Claude Code reads it as-is.
    creds_placeholder = home.claude / ".credentials.json"
    creds_placeholder.write_text("{}\n")
    creds_placeholder.chmod(0o600)

    # Pi home, from the image.
    _mkdir_private(home.pi)
    _extract_pi_template(img, home.pi)
    (home.pi / "models.json").exists() or (home.pi / "models.json").write_text('{"providers": {}}\n')

    try:
        fp = image.fingerprint()
    except ImageError:
        fp = None
    home.seed_file.write_text(json.dumps({
        "sandbox_id": sandbox_id,
        "image": img,
        "image_fingerprint": fp,
        "seeded_at": _now(),
        "template": str(template),
        "versions": _pinned_versions(),
    }, indent=2) + "\n")
    if not quiet:
        print(f"[agent-sandbox] seeded agent home {home.path}", flush=True)
    return home


def _now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def load(sandbox_id):
    """The home for an existing sandbox, or None if it was never seeded."""
    home = AgentHome(sandbox_id, home_path(sandbox_id))
    return home if home.exists else None
