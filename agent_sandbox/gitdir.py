"""Git inside a worktree sandbox: the common-dir mount and its overlays (spec R-20).

A linked worktree's `.git` is a file pointing at `<repo>/.git/worktrees/<id>`,
and that admin dir points back at the common directory. 1.0 mounted only the
worktree, so every git command inside the container failed. The fix is the
narrowest mount that makes git work:

  <repo>/.git                     read-write, at its own host path (git
                                  follows the absolute gitdir pointer)
  <worktree>                      a second time, at its host path, so
                                  `git worktree list|prune` sees the
                                  back-pointer as valid instead of prunable
  <repo>/.git/config              read-only   } every path a write could turn
  <repo>/.git/hooks               read-only   } into code that runs on the host
  <repo>/.git/modules             read-only   } the next time the user runs git
  <repo>/.git/worktrees           read-only   } (fsmonitor, hooks, hooksPath,
  <repo>/.git/worktrees/<id>      read-write  }  submodule dirs, other
  <repo>/.git/worktrees/<id>/config.worktree  read-only  } worktrees' config)
  <repo>/.git/HEAD, <repo>/.git/index  read-only: the main checkout's own
                                  state, which the worktree never needs

The parent checkout itself is never mounted. An overlay whose host source
does not exist is served from an empty file or directory under
runs/<id>/git-overlays/ so the overlay is never skipped. What stays writable
is documented in README as the accepted residual exposure: refs and objects
of the host repository, which an agent can create, rewrite, or delete.

Only a repository whose common dir is `<repo>/.git` qualifies. A submodule
or a repository that is itself a linked worktree keeps the 1.0 behaviour
(no git mounts) with a warning, rather than failing after the branch exists.
"""

import pathlib
import subprocess

from . import config
from .errors import MountError
from .mounts import Mount

OVERLAY_DIRNAME = "git-overlays"
RO_DIRS = ("hooks", "modules", "worktrees")
RO_FILES = ("config", "HEAD", "index")


def common_dir(path):
    """Absolute git common dir for `path`, or None when it is not a repo."""
    p = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--path-format=absolute",
         "--git-common-dir"],
        capture_output=True, text=True,
    )
    if p.returncode != 0 or not p.stdout.strip():
        return None
    return pathlib.Path(p.stdout.strip()).resolve()


def check(repo_root):
    """Decide before anything is created whether git mounts are possible.

    Returns (common_dir, warning). A warning means: run without git mounts.
    """
    if repo_root is None:
        return None, None
    root = pathlib.Path(repo_root).resolve()
    common = common_dir(root)
    if common is None:
        return None, None
    expected = (root / ".git").resolve()
    if common != expected or not common.is_dir():
        return None, (
            f"{root} is a submodule or a linked worktree (git dir {common}); "
            "git will not work inside this sandbox. Run agent-sandbox against "
            "the main repository, or use --direct."
        )
    return common, None


def _overlay_source(sandbox_id, real, kind):
    """The read-only source for an overlay: the real path, or an empty stand-in."""
    if real.exists():
        return real
    stand_in = config.RUNS / sandbox_id / OVERLAY_DIRNAME / real.name
    stand_in.parent.mkdir(parents=True, exist_ok=True)
    if kind == "dir":
        stand_in.mkdir(exist_ok=True)
    else:
        stand_in.touch()
    return stand_in


def git_mounts(workspace, common):
    """The mount set for one worktree sandbox. Returns (mounts, trusted)."""
    sid = workspace.sandbox_id
    ws_path = pathlib.Path(workspace.path).resolve()
    admin = common / "worktrees" / ws_path.name
    if not admin.is_dir():
        raise MountError(
            f"worktree admin directory missing: {admin}",
            "The worktree was not created by git. Remove the sandbox and retry.",
        )

    trusted = [
        Mount(common, common, purpose="git-common-dir"),
        Mount(ws_path, ws_path, purpose="worktree-path"),
        Mount(admin, admin, purpose="git-worktree-admin"),
    ]
    overlays = []
    for name in RO_FILES:
        overlays.append(Mount(_overlay_source(sid, common / name, "file"),
                              common / name, read_only=True, purpose="git-overlay"))
    for name in RO_DIRS:
        overlays.append(Mount(_overlay_source(sid, common / name, "dir"),
                              common / name, read_only=True, purpose="git-overlay"))
    overlays.append(Mount(_overlay_source(sid, admin / "config.worktree", "file"),
                          admin / "config.worktree", read_only=True,
                          purpose="git-overlay"))
    return trusted + overlays, trusted


def guard_removal(path, repo):
    """Refuse to delete anything under the host repository's git directory."""
    if not repo:
        return
    target = pathlib.Path(path).resolve()
    gitdir = (pathlib.Path(repo) / ".git").resolve()
    if target == gitdir or gitdir in target.parents:
        raise MountError(
            f"refusing to remove {target}: it is inside the host repository's git directory",
            "This is a bug guard; nothing was deleted. Report it with the sandbox id.",
        )
