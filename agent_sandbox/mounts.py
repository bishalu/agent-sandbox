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
