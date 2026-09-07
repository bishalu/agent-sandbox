"""Typed bind mounts and per-run environment (spec R-16, R-17).

Everything a container sees beyond the workspace and the package caches is
declared here as a `Mount`: a host path, a container path, a read-only flag,
and a one-word purpose that the run record discloses. Mounts render as
`--mount type=bind,...` rather than `-v`, because `-v` silently creates a
missing host directory (empty, owned by the invoking user) and a forgotten
seeding step would then look like success. `--mount` refuses instead.

Credentials keep their own module and disclosure ladder (credentials.py);
this module owns the non-credential mounts: the persistent agent home, skill
directories, and the git common directory with its overlays. `plan_for()`
composes them for both `run` and `enter`, so the two entry points cannot
drift apart.
"""

import os
import pathlib
import subprocess

from .errors import MountError

# Where the container's HOME lives; mirrors credentials.CONTAINER_HOME.
CONTAINER_HOME = "/root"


class Mount:
    """One bind mount. `purpose` is disclosed in run.json, never the mode alone."""

    def __init__(self, host, container, read_only=False, purpose="mount"):
        self.host = pathlib.Path(host)
        self.container = str(container)
        self.read_only = bool(read_only)
        self.purpose = purpose

    def docker_args(self):
        spec = f"type=bind,src={self.host},dst={self.container}"
        if self.read_only:
            spec += ",ro"
        return ["--mount", spec]

    def to_dict(self):
        return {
            "host": str(self.host),
            "container": self.container,
            "mode": "ro" if self.read_only else "rw",
            "purpose": self.purpose,
        }

    def validate(self):
        """A missing host source is a hard error, never a silent empty dir (R-16)."""
        if not self.host.exists():
            raise MountError(
                f"mount source does not exist: {self.host} ({self.purpose})",
                "Create it, fix the path in ~/agent-sandbox/config.json, or remove "
                "the entry. agent-sandbox never creates a mount source implicitly.",
            )
        return self


def render(mounts):
    args = []
    for m in mounts or []:
        args += m.docker_args()
    return args


class Plan:
    """What `run` and `enter` both feed into SandboxSpec: mounts plus env."""

    def __init__(self, mounts=None, env=None, trusted=None, warnings=None):
        self.mounts = list(mounts or [])
        self.env = dict(env or {})
        self.trusted = list(trusted or [])      # subset of mounts: rw into host git state
        self.warnings = list(warnings or [])


def plan_for(workspace, cfg, home=None):
    """Compose every declared mount and env var for one container (R-16).

    One planner for both entry points, so `enter` reproduces exactly what
    `run` mounted, re-deriving the git set from the host each time. Order is
    disclosure order only; Docker sorts by destination when mounting.
    """
    from . import gitdir                      # gitdir imports Mount from here
    plan = Plan()
    if home is not None:
        plan.mounts += home.mounts()
        drift = home.drift()
        if drift:
            plan.warnings.append(drift)
        plan.mounts += skill_mounts(cfg)
    if workspace.kind == "worktree":
        common, warning = gitdir.check(workspace.repo)
        if warning:
            plan.warnings.append(warning)
        elif common is not None:
            git, trusted = gitdir.git_mounts(workspace, common)
            plan.mounts += git
            plan.trusted += trusted
        else:
            plan.warnings.append(
                f"source repository {workspace.repo} no longer resolves as a git "
                "repository; git will not work inside this sandbox.")
    plan.env.update(git_identity_env(workspace))
    for m in plan.mounts:
        m.validate()
    return plan


# ---------------------------------------------------------------- skills
def skill_mounts(cfg):
    """Host skill directories, read-only under /root/.claude/skills/ (R-19).

    Each entry of `skill_mounts` in config.json is expanded and symlink-
    resolved on the host (a ~/.claude/skills entry is usually a symlink into
    a checkout elsewhere; the container cannot follow a host symlink), must
    be a directory, and lands at skills/<basename>. Two entries with the same
    basename would shadow each other, so that is an error too.
    """
    from . import config as _config
    raw = _config.resolve("skill_mounts", None, cfg) or []
    if not isinstance(raw, list):
        raise MountError(
            f"skill_mounts in config.json must be a list of paths, got {type(raw).__name__}",
            'Example: "skill_mounts": ["~/.claude/skills/sssf"]',
        )
    out, seen = [], {}
    for entry in raw:
        src = pathlib.Path(os.path.expanduser(str(entry)))
        real = src.resolve()
        if not real.is_dir():
            raise MountError(
                f"skill_mounts entry is not a directory: {entry} (resolves to {real})",
                "Fix the path in ~/agent-sandbox/config.json or remove the entry.",
            )
        name = src.name
        if name in seen:
            raise MountError(
                f"two skill_mounts entries share the name {name!r}: {seen[name]} and {entry}",
                "Skills mount by basename; rename or drop one of them.",
            )
        seen[name] = entry
        out.append(Mount(real, f"{CONTAINER_HOME}/.claude/skills/{name}",
                         read_only=True, purpose="skill"))
    return out


# ---------------------------------------------------------------- git identity
def _git_config(repo, key):
    p = subprocess.run(["git", "-C", str(repo), "config", "--get", key],
                       capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ""


def git_identity_env(workspace):
    """GIT_AUTHOR_* / GIT_COMMITTER_* for the container (R-17).

    Resolved per run from the source repository, local config first, then the
    global one — the same answer `git commit` would find on the host. Needed
    because nothing mounts ~/.gitconfig and the read-only overlay on
    .git/config (see git_mounts) makes `git config user.*` fail inside.

    A copy-kind workspace has no repository and no commit phase: no variables.
    A worktree or direct workspace without an identity is an error before the
    container starts, not a mystery `git commit` failure inside it.
    """
    if workspace.kind == "copy" or not workspace.repo:
        return {}
    repo = workspace.repo
    if not pathlib.Path(repo).exists():
        # The source repository moved or was deleted: plan_for warns that git
        # will not work in this sandbox; an identity error on top would hide
        # that warning behind a less useful one.
        return {}
    name = _git_config(repo, "user.name")
    email = _git_config(repo, "user.email")
    if not name or not email:
        raise MountError(
            f"no git identity resolves for {repo} (user.name / user.email)",
            "Set one for the repository (git config user.name / user.email) or "
            "globally (git config --global ...). Commits inside the sandbox "
            "cannot fall back to a container identity.",
        )
    return {
        "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
    }
