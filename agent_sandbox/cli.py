"""Command-line surface (spec R-08).

Deliberately thin: argparse wiring and presentation only. Every decision
lives in a module that can be called as a library, so SSSF can drive the same
substrate without going through this file (R-12).
"""

import argparse
import json
import os
import pathlib
import sys

from . import (agent_home, cleanup, config, credentials, doctor, gitdir,
               image, mounts, resources, worktree)
from .backend import SandboxSpec
from .docker_backend import LocalDockerBackend
from .errors import SandboxError
from .metadata import RunRecord

PROG = "agent-sandbox"


# ---------------------------------------------------------------- helpers
def _eprint(*a):
    print(*a, file=sys.stderr)


def _fail(err):
    """Print an error with its remediation and exit non-zero."""
    _eprint(f"{PROG}: error: {err.message if isinstance(err, SandboxError) else err}")
    remedy = getattr(err, "remedy", None)
    if remedy:
        for line in str(remedy).splitlines():
            _eprint(f"  → {line}")
    return 1


def _split_command(argv):
    """Everything after a bare `--` is the command to run inside the sandbox."""
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def _mode_runtime(mode, gvisor):
    """Modes are capability labels, stable regardless of implementation (R-01)."""
    if gvisor:
        return "runsc"
    return "runc"


def _describe_plan(plan):
    """Disclose every declared mount and every warning before the container starts."""
    for m in plan.mounts:
        _eprint(f"[{PROG}] mount: {m.host} → {m.container} ({m.to_dict()['mode']}, {m.purpose})")
    for w in plan.warnings:
        _eprint(f"{PROG}: warning: {w}")


# ---------------------------------------------------------------- run
def cmd_run(args, command):
    cfg = config.load_config()
    config.ensure_dirs()

    mode = config.resolve("mode", args.mode, cfg)
    network = config.resolve("network", args.network, cfg)
    res = resources.ResourceConfig(args.cpus, args.memory, args.pids_limit,
                                   args.timeout, cfg)

    target = pathlib.Path(args.repo).expanduser().resolve()
    if worktree.on_windows_filesystem(target) and not args.json:
        _eprint(f"{PROG}: warning: {target} is on the Windows filesystem (/mnt).")
        _eprint("  → Expect slower I/O. A WSL-native clone (e.g. under ~/) is much faster.")

    # Nothing is created before the backend proves it can run: a Docker
    # outage must not leave an orphan worktree and branch behind (R-20).
    backend = LocalDockerBackend(strict_caps=args.strict_caps)
    backend.preflight()
    mounts.skill_mounts(cfg)            # config errors surface before anything exists
    if not args.direct:
        # Decide git-mount eligibility before the worktree and branch exist,
        # so an unsupported repository shape warns instead of orphaning them.
        _, git_warning = gitdir.check(worktree.repo_root(target))
        if git_warning and not args.json:
            _eprint(f"{PROG}: warning: {git_warning}")

    ws = worktree.create(target, direct=args.direct)
    rec = RunRecord(ws.sandbox_id)
    # Persist the record the moment the workspace exists: anything that fails
    # between here and the container start (image build, home seeding,
    # credentials, mount planning) must leave a sandbox that `list` and `rm`
    # can see, never an invisible worktree and branch (R-20).
    rec.update(repo=ws.repo, workspace=str(ws.path), workspace_kind=ws.kind,
               branch=ws.branch).save()

    try:
        img = config.resolve("image", args.image, cfg)
        image.ensure(img, quiet=bool(args.json))

        home = agent_home.ensure(ws.sandbox_id, cfg, img, quiet=bool(args.json))
        creds = credentials.resolve(
            sandbox_dir=config.RUNS / ws.sandbox_id,
            with_github=args.with_github_auth,
            with_full_claude_state=args.with_full_claude_state,
            agent_home=home,
        )
        plan = mounts.plan_for(ws, cfg, home)

        rec.update(
            repo=ws.repo, workspace=str(ws.path), workspace_kind=ws.kind,
            branch=ws.branch, command=command or None, mode=mode,
            runtime=_mode_runtime(mode, args.experimental_gvisor),
            network=network, image=img, resources=res.to_dict(),
            credentials=creds.to_dict(), agent_home=str(home.path),
            mounts=[m.to_dict() for m in plan.mounts],
            trusted_mounts=[m.to_dict() for m in plan.trusted],
        ).save()

        if not args.json:
            _eprint(f"[{PROG}] sandbox {ws.sandbox_id}")
            _eprint(f"[{PROG}] workspace {ws.path} ({ws.kind}"
                    + (f", branch {ws.branch}" if ws.branch else "") + ")")
            _eprint(f"[{PROG}] mode={mode} network={network} "
                    f"cpus={res.cpus} memory={res.memory} pids={res.pids} "
                    f"timeout={res.timeout_raw}")
            if args.experimental_gvisor:
                _eprint(f"[{PROG}] WARNING: --experimental-gvisor is on. "
                        "gVisor cannot enforce cgroup limits on this machine, so "
                        "--cpus/--memory/--pids-limit are NOT enforced for this run.")
            for line in creds.describe():
                _eprint(f"[{PROG}] credentials: {line}")
            _describe_plan(plan)

        spec = SandboxSpec(
            sandbox_id=ws.sandbox_id, workspace=ws, command=command,
            resources=res, mode=mode, network=network, image=img,
            credentials=creds, read_only_root=args.read_only_root,
            experimental_gvisor=args.experimental_gvisor, record=rec,
            stream_output=not args.json, mounts=plan.mounts, env=plan.env,
        )
        result = backend.run(spec)
    finally:
        ws.release()

    # --rm disposes of the workspace, but only on a clean exit AND only when
    # the guard agrees: commits that exist on no other branch, or
    # uncommitted changes, keep the workspace regardless (R-04, R-20).
    disposed = False
    if args.keep is False and ws.kind != "direct":
        if result.status == "completed":
            try:
                worktree.remove(ws.sandbox_id, force=False)
                disposed = True
            except SandboxError as e:
                disposed = False
                if not args.json:
                    _eprint(f"[{PROG}] --rm ignored: {e.message}")
                    for line in str(e.remedy or "").splitlines():
                        _eprint(f"  → {line}")
        elif not args.json:
            _eprint(f"[{PROG}] --rm ignored: run {result.status}, "
                    "workspace kept so the work is not lost")

    if args.json:
        out = result.to_dict()
        out["workspace_disposed"] = disposed
        print(json.dumps(out, indent=2))
    else:
        _eprint(f"[{PROG}] {result.status} (exit {result.exit_code})")
        if ws.kind != "direct":
            if disposed:
                _eprint(f"[{PROG}] workspace removed (--rm)")
            else:
                _eprint(f"[{PROG}] workspace preserved: {ws.path}")
                _eprint(f"[{PROG}] reopen with: {PROG} enter {ws.sandbox_id}")
    return result.exit_code


# ---------------------------------------------------------------- enter
def cmd_enter(args, command):
    ws = worktree.reopen(args.sandbox_id)
    if ws is None:
        _eprint(f"{PROG}: error: no sandbox {args.sandbox_id!r} (or its workspace is gone)")
        _eprint(f"  → {PROG} list")
        return 1

    cfg = config.load_config()
    rec = RunRecord.load(args.sandbox_id) or RunRecord(args.sandbox_id)
    res = resources.ResourceConfig(args.cpus, args.memory, args.pids_limit,
                                   args.timeout, cfg)
    mode = config.resolve("mode", args.mode, cfg)
    network = config.resolve("network", args.network, cfg)
    img = config.resolve("image", args.image, cfg)

    backend = LocalDockerBackend(strict_caps=args.strict_caps)
    backend.preflight()
    image.ensure(img, quiet=bool(args.json))

    # Same helper as `run`: an existing home is never re-seeded, only
    # checked against the current image (R-18).
    home = agent_home.ensure(args.sandbox_id, cfg, img, quiet=bool(args.json))
    creds = credentials.resolve(
        sandbox_dir=config.RUNS / args.sandbox_id,
        with_github=args.with_github_auth,
        with_full_claude_state=args.with_full_claude_state,
        agent_home=home,
    )
    plan = mounts.plan_for(ws, cfg, home)
    rec.update(resources=res.to_dict(), credentials=creds.to_dict(),
               mode=mode, network=network, image=img,
               agent_home=str(home.path),
               mounts=[m.to_dict() for m in plan.mounts],
               trusted_mounts=[m.to_dict() for m in plan.trusted]).save()

    if not args.json:
        _eprint(f"[{PROG}] re-entering {args.sandbox_id} → {ws.path}")
        _describe_plan(plan)

    spec = SandboxSpec(
        sandbox_id=args.sandbox_id, workspace=ws, command=command,
        resources=res, mode=mode, network=network, image=img,
        credentials=creds, read_only_root=args.read_only_root,
        experimental_gvisor=args.experimental_gvisor, record=rec,
        stream_output=not args.json, mounts=plan.mounts, env=plan.env,
    )
    result = backend.run(spec)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    return result.exit_code


# ---------------------------------------------------------------- list
def cmd_list(args):
    recs = RunRecord.all()
    if args.json:
        print(json.dumps([r.public() for r in recs], indent=2))
        return 0
    if not recs:
        print("no sandboxes")
        return 0
    print(f"{'SANDBOX ID':<28} {'STATUS':<10} {'AGE':>6}  {'KIND':<9} WORKSPACE")
    for r in recs:
        d = r.data
        exists = d.get("workspace") and pathlib.Path(d["workspace"]).exists()
        age = cleanup.age_days(r)
        status = d.get("status", "?")
        if not exists and d.get("workspace_kind") != "direct":
            status += " (gone)"
        print(f"{r.sandbox_id:<28} {status:<10} {age:>5.1f}d  "
              f"{(d.get('workspace_kind') or '?'):<9} {d.get('workspace') or ''}")
    return 0


# ---------------------------------------------------------------- rm
def cmd_rm(args):
    try:
        worktree.remove(args.sandbox_id, force=args.force)
    except SandboxError as e:
        return _fail(e)
    print(f"removed {args.sandbox_id}")
    return 0


# ---------------------------------------------------------------- clean
def cmd_clean(args):
    out = {}
    if not args.docker_only:
        res = cleanup.clean(args.older_than, force=args.force,
                            dry_run=args.dry_run)
        out["sandboxes"] = res
        if not args.json:
            if args.dry_run:
                for t in res["candidates"]:
                    print(f"would remove {t['id']} ({t['age_days']}d)")
                if not res["candidates"]:
                    print(f"nothing to remove: no sandboxes older than "
                          f"{args.older_than}d ({len(res['young'])} newer, "
                          f"{len(res['protected'])} protected)")
            else:
                for sid in res["removed"]:
                    print(f"removed {sid}")
                if not res["removed"]:
                    print(f"no sandboxes older than {args.older_than}d to remove")
            for p in res["protected"]:
                print(f"kept {p['id']}: {p['reason']} (use --force to override)")

    if args.docker:
        actions = cleanup.docker_prune(dry_run=args.dry_run)
        out["docker"] = actions
        if not args.json:
            for c in actions["containers"]:
                print(f"{'would remove' if args.dry_run else 'removed'} container {c}")
            if not actions["containers"]:
                print("no stopped agent-sandbox containers to remove")
            if args.dry_run:
                print("dry run: dangling images and build cache left untouched")
            else:
                print(f"images: {actions['images']}")
                print(f"build cache: {actions['build_cache']}")

    if args.json:
        print(json.dumps(out, indent=2))
    return 0


# ---------------------------------------------------------------- doctor
def cmd_doctor(args):
    checks = doctor.run_checks(quick=args.quick, with_quota=args.with_quota)
    # Every line doctor prints may end up in a committed evidence file (R-22).
    for c in checks:
        c.detail = doctor.redact(c.detail)
        c.remedy = doctor.redact(c.remedy)
    if args.json:
        print(json.dumps({"checks": [c.to_dict() for c in checks],
                          "summary": doctor.summarize(checks)}, indent=2))
    else:
        for c in checks:
            print(f"[{c.status}] {c.name}" + (f": {c.detail}" if c.detail else ""))
            if c.remedy:
                for line in c.remedy.splitlines():
                    print(f"     → {line}")
        s = doctor.summarize(checks)
        print(f"\n{s['pass']} passed, {s['warn']} warnings, {s['fail']} failed")
    return 1 if doctor.summarize(checks)["fail"] else 0


# ---------------------------------------------------------------- build
def cmd_build(args):
    try:
        image.build(config.resolve("image", args.image), no_cache=args.no_cache)
    except SandboxError as e:
        return _fail(e)
    return 0


# ---------------------------------------------------------------- config
def cmd_config(args):
    cfg = config.load_config()
    if args.action == "show":
        merged = dict(config.DEFAULTS)
        merged.update(cfg)
        print(json.dumps(merged, indent=2))
        return 0
    if args.action == "set":
        if not args.key or args.value is None:
            _eprint(f"{PROG}: usage: {PROG} config set <key> <value>")
            return 1
        val = args.value
        if val.isdigit():
            val = int(val)
        cfg[args.key] = val
        config.save_config(cfg)
        print(f"{args.key} = {val}")
        return 0
    return 1


# ---------------------------------------------------------------- parser
def build_parser():
    p = argparse.ArgumentParser(
        prog=PROG,
        description="Run coding agents in an isolated git worktree inside a "
                    "hardened, disposable rootless-Docker container.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""examples:
  {PROG} .                          isolated worktree, interactive shell
  {PROG} ~/code/app -- claude       run Claude Code in a sandbox
  {PROG} . -- npm test              one-shot command
  {PROG} . --direct -- pytest       operate on the live checkout (locked)
  {PROG} list                       show sandboxes
  {PROG} enter app-1a2b3c4d         reopen an existing workspace
  {PROG} clean --docker             sweep old sandboxes and docker junk
""")
    sub = p.add_subparsers(dest="cmd")

    def add_run_flags(sp):
        sp.add_argument("--mode", choices=["fast", "safe"],
                        help="capability profile (default from config)")
        sp.add_argument("--cpus")
        sp.add_argument("--memory")
        sp.add_argument("--pids-limit", dest="pids_limit")
        sp.add_argument("--timeout", help="hard wall-clock ceiling, e.g. 12h")
        sp.add_argument("--network", choices=["full", "none", "restricted"])
        sp.add_argument("--read-only-root", action="store_true",
                        help="read-only container root; /workspace, /tmp, $HOME stay writable")
        sp.add_argument("--strict-caps", action="store_true",
                        help="drop ALL capabilities with no add-back (breaks apt)")
        sp.add_argument("--with-github-auth", action="store_true",
                        help="inject GH_TOKEN from the host `gh` login")
        sp.add_argument("--with-full-claude-state", action="store_true",
                        help="also mount ~/.claude.json read-only (exposes project "
                             "history, MCP config, account identifiers)")
        sp.add_argument("--experimental-gvisor", action="store_true",
                        help="run under gVisor; forfeits enforced resource limits")
        sp.add_argument("--image")
        sp.add_argument("--json", action="store_true")
        # Worktrees persist by default (R-04). --rm opts into disposing of the
        # workspace on a clean exit; it never discards work after a failure.
        keep = sp.add_mutually_exclusive_group()
        keep.add_argument("--keep", dest="keep", action="store_true",
                          default=None,
                          help="preserve the workspace after exit (the default)")
        keep.add_argument("--rm", dest="keep", action="store_false",
                          help="delete the workspace after a SUCCESSFUL run; "
                               "a failed or timed-out run is always preserved")

    sp = sub.add_parser("run", help="run a sandbox (default)")
    sp.add_argument("repo")
    sp.add_argument("--direct", action="store_true",
                    help="operate on the live checkout instead of a worktree")
    add_run_flags(sp)

    sp = sub.add_parser("enter", help="reopen an existing sandbox workspace")
    sp.add_argument("sandbox_id")
    add_run_flags(sp)

    sp = sub.add_parser("list", help="list sandboxes")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("rm", help="remove a sandbox workspace")
    sp.add_argument("sandbox_id")
    sp.add_argument("--force", action="store_true")

    sp = sub.add_parser("clean", help="sweep old sandboxes / docker junk")
    sp.add_argument("--older-than", type=int, default=14, metavar="DAYS")
    sp.add_argument("--force", action="store_true",
                    help="also remove sandboxes holding uncommitted work")
    sp.add_argument("--docker", action="store_true",
                    help="also prune dangling images, build cache, our containers")
    sp.add_argument("--docker-only", action="store_true")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("doctor", help="verify the environment end to end")
    sp.add_argument("--quick", action="store_true",
                    help="skip the real container execution check")
    sp.add_argument("--with-quota", action="store_true",
                    help="also run one authenticated `claude -p` inside the probe "
                         "sandbox (spends a little of your Claude quota)")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("build", help="build the base image")
    sp.add_argument("--no-cache", action="store_true")
    sp.add_argument("--image")

    sp = sub.add_parser("config", help="show or set persistent defaults")
    sp.add_argument("action", choices=["show", "set"])
    sp.add_argument("key", nargs="?")
    sp.add_argument("value", nargs="?")

    return p


KNOWN = {"run", "enter", "list", "rm", "clean", "doctor", "build", "config"}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    argv, command = _split_command(argv)

    # `agent-sandbox <path>` is shorthand for `agent-sandbox run <path>` (R-08).
    if argv and argv[0] not in KNOWN and not argv[0].startswith("-"):
        argv = ["run"] + argv
    elif not argv:
        build_parser().print_help()
        return 0

    args = build_parser().parse_args(argv)
    if args.cmd is None:
        build_parser().print_help()
        return 0

    try:
        if args.cmd == "run":
            return cmd_run(args, command)
        if args.cmd == "enter":
            return cmd_enter(args, command)
        if args.cmd == "list":
            return cmd_list(args)
        if args.cmd == "rm":
            return cmd_rm(args)
        if args.cmd == "clean":
            return cmd_clean(args)
        if args.cmd == "doctor":
            return cmd_doctor(args)
        if args.cmd == "build":
            return cmd_build(args)
        if args.cmd == "config":
            return cmd_config(args)
    except SandboxError as e:
        return _fail(e)
    except KeyboardInterrupt:
        _eprint("\ninterrupted")
        return 130
    return 0
