"""Workspace lifecycle: git worktrees, copied workspaces, and the --direct lock.

Worktrees live centrally under ~/agent-sandbox/worktrees/ rather than inside
the repo. Reason (spec R-04): a path inside the repo shows up in the repo's
own status/ignore/tooling surface and gets swept by cleaning scripts, and
nested worktrees confuse tools that walk upward looking for a repo root.
Central storage keeps the source repo untouched.
"""

import hashlib
import os
import pathlib
import re
import secrets
import shutil
import subprocess

from . import config
from .errors import LockHeld, RepoError, WorktreeError

_SLUG = re.compile(r"[^a-z0-9]+")


def _run(args, cwd=None, check=True):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise WorktreeError(
            f"command failed: {' '.join(args)}\n{p.stderr.strip()}",
            "Check the repository state and try again.",
        )
    return p


def slugify(name):
    s = _SLUG.sub("-", str(name).lower()).strip("-")
    return s or "workspace"


def new_sandbox_id(repo_path):
    """<repo-slug>-<8 hex>. Short and unique, no timestamp noise (R-04)."""
    return f"{slugify(pathlib.Path(repo_path).name)}-{secrets.token_hex(4)}"


def is_git_repo(path):
    p = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    return p.returncode == 0


def repo_root(path):
    p = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    if p.returncode != 0:
        return None
    return pathlib.Path(p.stdout.strip())


def current_branch(path):
    p = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    return p.stdout.strip() if p.returncode == 0 else None


def has_commits(path):
    p = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True, text=True,
    )
    return p.returncode == 0


def on_windows_filesystem(path):
    """WSL2: /mnt/... is the Windows filesystem and is markedly slower (R-13)."""
    return str(pathlib.Path(path).resolve()).startswith("/mnt/")


class Workspace:
    """A resolved workspace ready to be mounted into a container."""

    def __init__(self, sandbox_id, path, kind, repo=None, branch=None, lock=None):
        self.sandbox_id = sandbox_id
        self.path = pathlib.Path(path)
        self.kind = kind          # "worktree" | "copy" | "direct"
        self.repo = str(repo) if repo else None
        self.branch = branch
        self.lock = lock

    def release(self):
        if self.lock:
            self.lock.release()


class DirectLock:
    """Advisory per-repo lock, only for --direct (R-04).

    Worktree mode needs no lock: separate worktrees cannot race. --direct
    mutates the live checkout, so two concurrent runs genuinely conflict.
    """

    def __init__(self, repo):
        h = hashlib.sha256(str(repo).encode()).hexdigest()[:16]
        self.path = config.LOCK_DIR / f"{h}.lock"
        self.repo = str(repo)

    def _stale(self):
        try:
            pid = int(self.path.read_text().split()[0])
        except (ValueError, OSError, IndexError):
            return True
        if pid == os.getpid():
            return True
        try:
            os.kill(pid, 0)          # signal 0 only tests existence
        except ProcessLookupError:
            return True
        except PermissionError:
            return False             # exists, owned by someone else
        return False

    def acquire(self):
        config.LOCK_DIR.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and not self._stale():
            holder = self.path.read_text().strip()
            raise LockHeld(
                f"another --direct sandbox is already running against {self.repo}\n"
                f"  lock: {self.path} ({holder})",
                "Wait for it to finish, or drop --direct to use an isolated "
                "worktree instead (worktree runs never conflict).",
            )
        if self.path.exists():
            self.path.unlink()       # reclaim stale
        self.path.write_text(f"{os.getpid()} {self.repo}\n")
        return self

    def release(self):
        try:
            if self.path.exists():
                pid = int(self.path.read_text().split()[0])
                if pid == os.getpid():
                    self.path.unlink()
        except (ValueError, OSError, IndexError):
            pass


def create(target, sandbox_id=None, direct=False):
    """Resolve `target` into a Workspace ready for mounting."""
    target = pathlib.Path(target).expanduser().resolve()
    if not target.exists():
        raise RepoError(
            f"path does not exist: {target}",
            "Pass a path to a git repository or a directory.",
        )

    root = repo_root(target)

    if direct:
        if root is None:
            raise RepoError(
                f"--direct requires a git repository, but {target} is not one",
                "Drop --direct to run against a copied workspace instead.",
            )
        sid = sandbox_id or new_sandbox_id(root)
        lock = DirectLock(root).acquire()
        return Workspace(sid, root, "direct", repo=root,
                         branch=current_branch(root), lock=lock)

    if root is not None:
        sid = sandbox_id or new_sandbox_id(root)
        dest = config.WORKTREES / sid
        branch = f"agent-sandbox/{sid}"
        if not has_commits(root):
            raise RepoError(
                f"repository has no commits yet: {root}",
                "Make an initial commit, then run agent-sandbox again "
                "(a worktree needs a commit to branch from).",
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "-C", str(root), "worktree", "add", "-b", branch,
              str(dest), "HEAD"])
        return Workspace(sid, dest, "worktree", repo=root, branch=branch)

    # Not a git repo: copy into an isolated workspace (R-04).
    sid = sandbox_id or new_sandbox_id(target)
    dest = config.WORKTREES / sid
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(target, dest, symlinks=True, dirs_exist_ok=False)
    return Workspace(sid, dest, "copy", repo=target)


def seed(ws, paths):
    """Copy gitignored paths from the source checkout into a worktree (R-23).

    Runs on creation and again on every `enter`, overwriting the copies, so the
    host checkout stays the source of truth for secrets.

    `git worktree add` gives a worktree the tracked files only, so the secrets a
    repo keeps in a gitignored `.env` never arrive on their own and an agent
    runs against a silently degraded setup. `worktree_seed` in config.json
    lists repo-relative paths (files or directories) to copy in after creation.

    Only paths git ignores in the source repo are copied: a tracked path is
    already in the worktree, and an untracked-but-not-ignored one would be a
    commit waiting to happen inside the sandbox. Entries missing from the
    source are skipped (a repo without a `.env` has nothing to seed). Copies
    preserve permissions, so a 0600 `.env` stays 0600.

    Returns (copied, warnings): the relative paths copied, and one warning per
    entry that was present but not gitignored.
    """
    if not paths:
        return [], []
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise WorktreeError(
            "worktree_seed in config.json must be a list of repo-relative paths, "
            f"got {type(paths).__name__}",
            'Example: "worktree_seed": [".env", "secrets"]',
        )
    if ws.kind != "worktree" or not ws.repo:
        return [], []
    root = pathlib.Path(ws.repo)
    copied, warnings = [], []
    for raw in paths:
        rel = raw.strip().strip("/")
        parts = pathlib.PurePosixPath(rel).parts
        if not rel or pathlib.PurePosixPath(raw.strip()).is_absolute() or ".." in parts:
            raise WorktreeError(
                f"worktree_seed entry must be a relative path inside the repo: {raw!r}",
                'Example: "worktree_seed": [".env", "secrets"]',
            )
        src = root / rel
        if not src.exists():
            continue
        p = _run(["git", "-C", str(root), "check-ignore", "-q", "--", rel], check=False)
        if p.returncode != 0:
            warnings.append(
                f"worktree_seed: {rel} is not gitignored in {root}; not copied "
                "(a tracked path is already in the worktree, and an untracked one "
                "would be committed from inside the sandbox)")
            continue
        dest = ws.path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dest, symlinks=False, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dest)
        copied.append(rel)
    return copied, warnings


def reopen(sandbox_id):
    """Workspace for an existing sandbox id (used by `enter`)."""
    from .metadata import RunRecord
    rec = RunRecord.load(sandbox_id)
    if rec is None:
        return None
    path = rec.data.get("workspace")
    if not path or not pathlib.Path(path).exists():
        return None
    return Workspace(sandbox_id, path, rec.data.get("workspace_kind") or "worktree",
                     repo=rec.data.get("repo"), branch=rec.data.get("branch"))


def has_uncommitted_work(path):
    """True if the worktree holds changes that would be lost by removal (R-04)."""
    path = pathlib.Path(path)
    if not path.exists():
        return False
    if not is_git_repo(path):
        return True                  # a copied workspace: never assume disposable
    p = subprocess.run(["git", "-C", str(path), "status", "--porcelain"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return True
    return bool(p.stdout.strip())


def unpushed_commits(path):
    """Commits on this branch not contained in any other ref (R-04)."""
    path = pathlib.Path(path)
    if not path.exists() or not is_git_repo(path):
        return 0
    # --exclude patterns for --branches are matched against the short name
    # (no refs/heads/ prefix); with the prefix the sandbox branch was never
    # excluded and this always returned 0, so the guard never fired.
    p = subprocess.run(
        ["git", "-C", str(path), "log", "--oneline", "HEAD", "--not",
         "--exclude=agent-sandbox/*", "--branches", "--remotes"],
        capture_output=True, text=True,
    )
    if p.returncode != 0:
        return 0
    return len([l for l in p.stdout.splitlines() if l.strip()])


def remove(sandbox_id, force=False):
    """Remove a sandbox workspace. Refuses to discard work unless forced."""
    from .metadata import RunRecord
    rec = RunRecord.load(sandbox_id)
    path = pathlib.Path(rec.data["workspace"]) if rec and rec.data.get("workspace") else \
        config.WORKTREES / sandbox_id
    kind = (rec.data.get("workspace_kind") if rec else None) or "worktree"

    if kind == "direct":
        # A --direct run has no workspace of ours: it used the real checkout.
        # Removing it clears the run record only, and never touches the repo.
        if not force:
            raise WorktreeError(
                f"{sandbox_id} was a --direct run against your real checkout",
                "There is no sandbox workspace to delete. To clear just its run "
                f"record and logs:\n  agent-sandbox rm {sandbox_id} --force",
            )
        from .gitdir import guard_removal
        run_dir = config.RUNS / sandbox_id
        guard_removal(run_dir, rec.data.get("repo") if rec else None)
        if run_dir.exists():
            shutil.rmtree(run_dir, ignore_errors=True)
        return True

    if path.exists() and not force:
        dirty = has_uncommitted_work(path)
        ahead = unpushed_commits(path)
        if dirty or ahead:
            what = []
            if dirty:
                what.append("uncommitted changes")
            if ahead:
                what.append(f"{ahead} commit(s) not present on any other branch")
            raise WorktreeError(
                f"{sandbox_id} has {' and '.join(what)} — refusing to delete",
                f"Inspect it first: agent-sandbox enter {sandbox_id}\n"
                f"  Then, if you really want it gone: agent-sandbox rm {sandbox_id} --force",
            )

    repo = rec.data.get("repo") if rec else None
    # Never delete anything inside the host repository's git directory, which
    # a 1.1 sandbox mounts read-write (R-20). Only the worktree, its branch,
    # and our own run directory are ours to remove.
    from .gitdir import guard_removal
    guard_removal(path, repo)
    guard_removal(config.RUNS / sandbox_id, repo)
    if repo and pathlib.Path(repo).exists() and kind == "worktree":
        subprocess.run(["git", "-C", str(repo), "worktree", "remove",
                        "--force", str(path)], capture_output=True, text=True)
        branch = rec.data.get("branch") if rec else None
        if branch:
            subprocess.run(["git", "-C", str(repo), "branch", "-D", branch],
                           capture_output=True, text=True)
        subprocess.run(["git", "-C", str(repo), "worktree", "prune"],
                       capture_output=True, text=True)

    if path.exists():
        shutil.rmtree(path, ignore_errors=True)

    run_dir = config.RUNS / sandbox_id
    if run_dir.exists():
        shutil.rmtree(run_dir, ignore_errors=True)
    return True
