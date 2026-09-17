"""worktree.seed: per-start refresh that never overwrites a run's changes (R-23, plan U10 / R26).

The host checkout is the source of truth for gitignored secrets, so each start
refreshes them. A seeded file the run itself changed is kept instead, and the
warning names the path only, never its contents.
"""

import json
import shutil
import subprocess

import pytest

from agent_sandbox import config, worktree
from agent_sandbox.errors import WorktreeError

SECRET = "sk-live-DO-NOT-LEAK-7f3a9c"


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(home, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".env\nsecrets/\n")
    (root / "README").write_text("x\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    return root


@pytest.fixture
def ws(repo, tmp_path):
    dest = tmp_path / "worktrees" / "src-00000001"
    dest.mkdir(parents=True)
    return worktree.Workspace("src-00000001", dest, "worktree", repo=repo)


def test_first_seed_copies_and_writes_manifest_outside_worktree(repo, ws, capsys):
    (repo / ".env").write_text(f"KEY={SECRET}\n")
    copied, warnings = worktree.seed(ws, [".env"])
    assert copied == [".env"] and warnings == []
    assert (ws.path / ".env").read_text() == f"KEY={SECRET}\n"
    manifest = config.RUNS / ws.sandbox_id / "seed-manifest.json"
    assert manifest.is_file()
    assert ".env" in json.loads(manifest.read_text())["files"]
    # Nothing but the seeded file lands in the worktree.
    assert sorted(p.name for p in ws.path.rglob("*")) == [".env"]
    assert ws.path.resolve() not in manifest.resolve().parents


def test_run_modified_file_is_kept_and_named(repo, ws, capsys):
    (repo / ".env").write_text("KEY=one\n")
    worktree.seed(ws, [".env"])
    (ws.path / ".env").write_text(f"KEY={SECRET}\n")
    (repo / ".env").write_text("KEY=two\n")
    result = worktree.seed(ws, [".env"])
    copied, warnings = result
    assert (ws.path / ".env").read_text() == f"KEY={SECRET}\n"
    assert result.kept == [".env"]
    assert warnings == ["worktree_seed: kept run-modified seed path .env"]
    # Kept stays kept on the next start too.
    _, warnings2 = result2 = worktree.seed(ws, [".env"])
    assert result2.kept == [".env"]
    assert (ws.path / ".env").read_text() == f"KEY={SECRET}\n"


def test_host_rotation_refreshes_unmodified_copy(repo, ws):
    (repo / ".env").write_text("KEY=old\n")
    worktree.seed(ws, [".env"])
    (repo / ".env").write_text("KEY=rotated\n")
    result = worktree.seed(ws, [".env"])
    assert (ws.path / ".env").read_text() == "KEY=rotated\n"
    assert result.kept == [] and list(result) == [[".env"], []]


def test_missing_worktree_copy_is_restored(repo, ws):
    (repo / ".env").write_text("KEY=a\n")
    worktree.seed(ws, [".env"])
    (ws.path / ".env").unlink()
    result = worktree.seed(ws, [".env"])
    assert (ws.path / ".env").read_text() == "KEY=a\n" and result.kept == []


def test_warnings_and_output_never_carry_contents(repo, ws, capsys):
    (repo / ".env").write_text(f"KEY={SECRET}-host\n")
    (repo / "secrets").mkdir()
    (repo / "secrets" / "token").write_text(f"{SECRET}-token\n")
    worktree.seed(ws, [".env", "secrets"])
    (ws.path / ".env").write_text(f"KEY={SECRET}-run\n")
    (ws.path / "secrets" / "token").write_text(f"{SECRET}-run-token\n")
    (repo / ".env").write_text(f"KEY={SECRET}-rotated\n")
    result = worktree.seed(ws, [".env", "secrets"])
    assert len(result.warnings) == 2
    out = capsys.readouterr()
    for text in [*result.warnings, *result.kept, *result.copied, out.out, out.err]:
        assert SECRET not in text
    manifest = (config.RUNS / ws.sandbox_id / "seed-manifest.json").read_text()
    assert SECRET not in manifest


def test_directory_files_follow_the_rule_per_file(repo, ws):
    d = repo / "secrets"
    d.mkdir()
    (d / "a").write_text("a1\n")
    (d / "b").write_text("b1\n")
    worktree.seed(ws, ["secrets"])
    (ws.path / "secrets" / "a").write_text("a-run\n")
    (ws.path / "secrets" / "extra").write_text("run-only\n")
    (d / "a").write_text("a2\n")
    (d / "b").write_text("b2\n")
    result = worktree.seed(ws, ["secrets"])
    assert (ws.path / "secrets" / "a").read_text() == "a-run\n"
    assert (ws.path / "secrets" / "b").read_text() == "b2\n"
    assert (ws.path / "secrets" / "extra").read_text() == "run-only\n"
    assert result.kept == ["secrets/a"]
    assert result.warnings == ["worktree_seed: kept run-modified seed path secrets/a"]
    assert result.copied == ["secrets"]


def test_new_source_file_over_run_created_copy_is_kept(repo, ws):
    """A manifest exists but has no record of this file, and the worktree already
    holds a different copy: that copy came from the run, so it is kept."""
    (repo / "secrets").mkdir()
    (repo / "secrets" / "a").write_text("a\n")
    worktree.seed(ws, ["secrets"])
    (ws.path / "secrets" / "new").write_text("run\n")
    (repo / "secrets" / "new").write_text("host\n")
    result = worktree.seed(ws, ["secrets"])
    assert (ws.path / "secrets" / "new").read_text() == "run\n"
    assert result.kept == ["secrets/new"]


def test_symlink_in_worktree_is_never_written_through(repo, ws, tmp_path):
    (repo / ".env").write_text("KEY=a\n")
    worktree.seed(ws, [".env"])
    outside = tmp_path / "outside"
    outside.write_text("untouched\n")
    (ws.path / ".env").unlink()
    (ws.path / ".env").symlink_to(outside)
    (repo / ".env").write_text("KEY=b\n")
    result = worktree.seed(ws, [".env"])
    assert outside.read_text() == "untouched\n"
    assert result.kept == [".env"]


def test_manifest_path_inside_worktree_is_refused(repo, ws):
    (repo / ".env").write_text("KEY=a\n")
    with pytest.raises(WorktreeError):
        worktree.seed(ws, [".env"], manifest=ws.path / "seed-manifest.json")
    assert not (ws.path / "seed-manifest.json").exists()


def test_symlinked_seed_directory_is_never_written_through(repo, ws, tmp_path):
    (repo / "secrets").mkdir()
    (repo / "secrets" / "a").write_text("a1\n")
    worktree.seed(ws, ["secrets"])
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    shutil.rmtree(ws.path / "secrets")
    (ws.path / "secrets").symlink_to(outside)
    (repo / "secrets" / "a").write_text("a2\n")
    result = worktree.seed(ws, ["secrets"])
    assert list(outside.iterdir()) == []
    assert result.kept == ["secrets/a"]
