"""Workspace lifecycle: git worktrees, copied workspaces, and the --direct lock.

Worktrees live centrally under ~/agent-sandbox/worktrees/ rather than inside
the repo. Reason (spec R-04): a path inside the repo shows up in the repo's
own status/ignore/tooling surface and gets swept by cleaning scripts, and
nested worktrees confuse tools that walk upward looking for a repo root.
Central storage keeps the source repo untouched.
"""

import hashlib
import json
import os
import pathlib
import re
import secrets
import shutil
import subprocess

from . import config
from .errors import LockHeld, RepoError, WorktreeError
from . import locks
from .locks import PidLock

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


class DirectLock(PidLock):
    """Advisory per-repo lock, only for --direct (R-04).

    Worktree mode needs no lock: separate worktrees cannot race. --direct
    mutates the live checkout, so two concurrent runs genuinely conflict.
    The stale-holder rule lives in `locks.PidLock`.
    """

    def __init__(self, repo):
        super().__init__(config.LOCK_DIR / f"{locks.repo_hash(repo)}.lock", note=str(repo))
        self.repo = str(repo)

    def _held_error(self, holder):
        return LockHeld(
            f"another --direct sandbox is already running against {self.repo}\n"
            f"  lock: {self.path} ({holder})",
            "Wait for it to finish, or drop --direct to use an isolated "
            "worktree instead (worktree runs never conflict).",
        )


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


SEED_MANIFEST = "seed-manifest.json"


class SeedResult(tuple):
    """(copied, warnings), so `copied, warnings = seed(...)` keeps working,
    plus `.kept`: the repo-relative files left alone because the run changed
    them. A gate reads `.kept` to mark evidence from this worktree dirty."""

    def __new__(cls, copied, warnings, kept):
        self = super().__new__(cls, (copied, warnings))
        self.kept = kept
        return self

    @property
    def copied(self):
        return self[0]

    @property
    def warnings(self):
        return self[1]


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _seed_files(src, rel):
    """(source file, repo-relative posix path) for every file under a seed entry.
    Symlinks in the source are followed, as the copy always did."""
    if not src.is_dir():
        yield src, rel
        return
    for dirpath, dirnames, filenames in os.walk(src, followlinks=True):
        dirnames.sort()
        base = pathlib.Path(dirpath)
        for name in sorted(filenames):
            f = base / name
            yield f, str(pathlib.PurePosixPath(rel) / f.relative_to(src).as_posix())


def _load_manifest(path):
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}              # unreadable: treat every existing copy as unrecorded
    files = data.get("files") if isinstance(data, dict) else None
    return files if isinstance(files, dict) else {}


def seed(ws, paths, manifest=None):
    """Copy gitignored paths from the source checkout into a worktree (R-23).

    Runs on creation and again on every `enter`, so the host checkout stays the
    source of truth for secrets: a key rotated on the host reaches the sandbox.

    `git worktree add` gives a worktree the tracked files only, so the secrets a
    repo keeps in a gitignored `.env` never arrive on their own and an agent
    runs against a silently degraded setup. `worktree_seed` in config.json
    lists repo-relative paths (files or directories) to copy in after creation.

    Only paths git ignores in the source repo are copied: a tracked path is
    already in the worktree, and an untracked-but-not-ignored one would be a
    commit waiting to happen inside the sandbox. Entries missing from the
    source are skipped (a repo without a `.env` has nothing to seed). Copies
    preserve permissions, so a 0600 `.env` stays 0600.

    A refresh never overwrites what the run changed. Each copy's sha256 is
    recorded per file in `manifest` (default runs/<id>/seed-manifest.json,
    never inside the worktree, where it could be committed). On a later seed a
    file is copied when its worktree copy is missing or still matches the
    record; otherwise the run changed it (or replaced it with a symlink or
    directory), so it is kept and a warning names the path, never its
    contents. Directories follow the rule per file; files only the worktree
    has are left alone. With no manifest at all, everything is copied.

    Returns a SeedResult: unpacks to (copied, warnings), the seed entries at
    least one of whose files is in place from the source and one warning per
    non-gitignored or kept path; `.kept` lists the kept files.
    """
    if not paths:
        return SeedResult([], [], [])
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise WorktreeError(
            "worktree_seed in config.json must be a list of repo-relative paths, "
            f"got {type(paths).__name__}",
            'Example: "worktree_seed": [".env", "secrets"]',
        )
    if ws.kind != "worktree" or not ws.repo:
        return SeedResult([], [], [])
    root = pathlib.Path(ws.repo)
    manifest = pathlib.Path(manifest) if manifest else \
        config.RUNS / require_sandbox_id(ws.sandbox_id) / SEED_MANIFEST
    wt = ws.path.resolve()
    if manifest.resolve() == wt or wt in manifest.resolve().parents:
        raise WorktreeError(
            f"seed manifest {manifest} is inside the worktree {ws.path}",
            "Keep the manifest in the sandbox's run directory, where it cannot be committed.",
        )
    recorded = _load_manifest(manifest)
    first = recorded is None
    recorded = dict(recorded or {})
    copied, warnings, kept = [], [], []
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
        placed = False
        for src_file, frel in _seed_files(src, rel):
            dest = ws.path / frel
            if not _within(wt, dest.parent):
                changed = True            # a parent was swapped for a link out of the worktree
            elif dest.is_symlink() or dest.exists():
                if dest.is_symlink() or not dest.is_file():
                    changed = True        # never write through a link or over a directory
                elif first:
                    changed = False
                else:
                    have = _sha256(dest)
                    changed = have != recorded.get(frel) and have != _sha256(src_file)
            else:
                changed = False
            if changed:
                kept.append(frel)
                warnings.append(f"worktree_seed: kept run-modified seed path {frel}")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest)
            recorded[frel] = _sha256(dest)
            placed = True
        if placed:
            copied.append(rel)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    tmp = manifest.with_name(manifest.name + ".tmp")
    tmp.write_text(json.dumps({"version": 1, "files": recorded}, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, manifest)
    return SeedResult(copied, warnings, kept)


def _within(top, d):
    """`d` (existing or not) resolves to `top` or below it."""
    real = pathlib.Path(d).resolve()
    return real == top or top in real.parents


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


SANDBOX_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def require_sandbox_id(sandbox_id):
    """One id names one sandbox. An empty id once resolved `RUNS / ""` and
    `WORKTREES / ""` to the roots themselves and rm --force emptied both
    (2026-09-14), so an id must be a single non-empty path segment."""
    if not isinstance(sandbox_id, str) or not SANDBOX_ID_RE.match(sandbox_id) or ".." in sandbox_id:
        raise WorktreeError(
            f"not a sandbox id: {sandbox_id!r}",
            "Pass one id as `agent-sandbox list` prints it, for example vibeset-dj-1a2b3c4d.",
        )
    return sandbox_id


def _inside(path, root):
    path, root = pathlib.Path(path).resolve(), pathlib.Path(root).resolve()
    return path != root and root in path.parents


def remove(sandbox_id, force=False):
    """Remove a sandbox workspace. Refuses to discard work unless forced."""
    from .metadata import RunRecord
    require_sandbox_id(sandbox_id)
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

    if not _inside(path, config.WORKTREES) and kind == "worktree":
        raise WorktreeError(
            f"refusing to remove {path}: not a sandbox worktree under {config.WORKTREES}",
            "The record's workspace path is outside the worktrees directory; inspect it by hand.",
        )
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
