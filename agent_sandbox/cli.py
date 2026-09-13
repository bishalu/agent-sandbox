"""Command-line surface (spec R-08).

Deliberately thin: argparse wiring and presentation only. Every decision
lives in a module that can be called as a library, so SSSF can drive the same
substrate without going through this file (R-12).
"""

import argparse
import dataclasses
import datetime
import json
import os
import pathlib
import re
import sys

from . import (admission, agent_home, cleanup, config, credentials, doctor, gitdir,
               memlog, plugins, image, mounts, resources, state, worktree)
from .backend import SandboxSpec
from .docker_backend import LocalDockerBackend
from .errors import AdmissionRefused, AdmissionTimeout, SandboxError
from .metadata import RunRecord

PROG = "agent-sandbox"
# A launch that admission refused or timed out (R2). Distinct from 1 so a
# driver can tell "wait and retry" from "broken".
EXIT_ADMISSION = 3


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


def _fail_admission(err, as_json):
    """Exit 3 with the reasons and numbers, as one JSON object under --json."""
    if as_json:
        print(json.dumps({"admission": err.kind, "message": err.message,
                          "reasons": err.reasons, "numbers": err.decision.numbers,
                          "remedy": err.remedy}, indent=2))
    else:
        _fail(err)
    return EXIT_ADMISSION


def _parse_tags(items):
    """`--tag key=value`, repeatable, into a dict. The driver tags each run
    with its unit and milestone; `milestone` also selects the project lock."""
    tags = {}
    for item in items or []:
        key, eq, value = item.partition("=")
        if not eq or not key.strip():
            raise SandboxError(f"--tag needs key=value, got {item!r}",
                               "Example: --tag unit=milestone-app-6 --tag milestone=6")
        tags[key.strip()] = value
    return tags


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
def _resumable(target):
    """Existing, intact sandboxes of the repository `target` lives in, newest first.

    Direct workspaces are never resumed (they are the live checkout), and a
    record whose worktree is gone cannot be re-entered.
    """
    root = worktree.repo_root(target) or target
    out = []
    for r in RunRecord.all():
        d = r.data
        if d.get("workspace_kind") == "direct":
            continue
        if not d.get("repo") or pathlib.Path(d["repo"]).resolve() != root.resolve():
            continue
        if not d.get("workspace") or not pathlib.Path(d["workspace"]).exists():
            continue
        out.append(r)
    out.sort(key=lambda r: r.data.get("created_at") or "", reverse=True)
    return out


def _pick_resume(existing, args):
    """Several sandboxes for one repo: ask on a terminal, refuse otherwise.

    Returns a sandbox id, None for "make a new one", or raises SystemExit(1)
    when there is no terminal to ask.
    """
    if args.json or not sys.stdin.isatty() or not sys.stderr.isatty():
        _eprint(f"{PROG}: error: several sandboxes exist for this repository; "
                "say which one:")
        for r in existing:
            _eprint(f"  {PROG} enter {r.sandbox_id}")
        _eprint(f"  {PROG} {args.repo} --new        # a fresh sandbox instead")
        raise SystemExit(1)
    _eprint(f"[{PROG}] this repository has {len(existing)} sandboxes:")
    for i, r in enumerate(existing, 1):
        d = r.data
        _eprint(f"  {i}) {r.sandbox_id:<28} {d.get('status', '?'):<10} "
                f"{cleanup.age_days(r):>4.1f}d  {d.get('branch') or ''}")
    _eprint(f"  n) new sandbox")
    while True:
        try:
            ans = input(f"[{PROG}] resume which? [1] ").strip().lower() or "1"
        except EOFError:
            raise SystemExit(1)
        if ans == "n":
            return None
        if ans.isdigit() and 1 <= int(ans) <= len(existing):
            return existing[int(ans) - 1].sandbox_id
        _eprint(f"  enter 1-{len(existing)} or n")


def cmd_run(args, command):
    cfg = config.load_config()
    config.ensure_dirs()

    # Resume by default (R-04): a repository that already has a sandbox gets
    # that sandbox back, with its worktree, branch, and agent home, so the
    # conversation and the work continue. --new asks for a fresh one; --direct
    # never resumes because it is the live checkout, not a sandbox.
    if not args.direct and not args.new:
        existing = _resumable(pathlib.Path(args.repo).expanduser().resolve())
        resume_id = None
        if len(existing) == 1:
            resume_id = existing[0].sandbox_id
        elif len(existing) > 1:
            resume_id = _pick_resume(existing, args)
        if resume_id:
            if not args.json:
                _eprint(f"[{PROG}] resuming {resume_id} "
                        f"(this repository's sandbox; --new for a fresh one)")
            args.sandbox_id = resume_id
            return cmd_enter(args, command)

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
    tags = _parse_tags(args.tag)
    backend = LocalDockerBackend(strict_caps=args.strict_caps,
                                 force_admission=args.force, wait=args.wait)
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
               branch=ws.branch, tags=tags).save()

    try:
        # Gitignored secrets the repo keeps beside its code (R-23). Done first:
        # a seed failure must leave a sandbox that `list` and `rm` can see.
        seeded, seed_warnings = worktree.seed(
            ws, config.resolve("worktree_seed", None, cfg))
        rec.update(seeded=seeded).save()

        img = config.resolve("image", args.image, cfg)
        image.ensure(img, quiet=bool(args.json))

        home = agent_home.ensure(ws.sandbox_id, cfg, img, quiet=bool(args.json))
        mirrored = plugins.sync(home, cfg)
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
            if seeded:
                _eprint(f"[{PROG}] seeded: {', '.join(seeded)} "
                        f"(gitignored copies from {ws.repo})")
            for w in seed_warnings:
                _eprint(f"{PROG}: warning: {w}")
            if mirrored:
                _eprint(f"[{PROG}] plugins: {len(mirrored)} mirrored from the host, read-only")
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
                _eprint(f"[{PROG}] resume with: {PROG} {args.repo}   "
                        f"(or: {PROG} enter {ws.sandbox_id}; --new for a fresh one)")
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
    tags = _parse_tags(args.tag)

    backend = LocalDockerBackend(strict_caps=args.strict_caps,
                                 force_admission=args.force, wait=args.wait)
    backend.preflight()
    image.ensure(img, quiet=bool(args.json))

    # Gitignored secrets are refreshed from the source checkout on every
    # start, so a key rotated on the host reaches a resumed sandbox (R-23).
    seeded, seed_warnings = worktree.seed(
        ws, config.resolve("worktree_seed", None, cfg))
    rec.update(seeded=seeded).save()

    # Same helper as `run`: an existing home is never re-seeded, only
    # checked against the current image (R-18). Plugins are re-mirrored.
    home = agent_home.ensure(args.sandbox_id, cfg, img, quiet=bool(args.json))
    mirrored = plugins.sync(home, cfg)
    creds = credentials.resolve(
        sandbox_dir=config.RUNS / args.sandbox_id,
        with_github=args.with_github_auth,
        with_full_claude_state=args.with_full_claude_state,
        agent_home=home,
    )
    plan = mounts.plan_for(ws, cfg, home)
    rec.update(resources=res.to_dict(), credentials=creds.to_dict(),
               mode=mode, network=network, image=img, tags=tags,
               agent_home=str(home.path),
               mounts=[m.to_dict() for m in plan.mounts],
               trusted_mounts=[m.to_dict() for m in plan.trusted]).save()

    if not args.json:
        _eprint(f"[{PROG}] re-entering {args.sandbox_id} → {ws.path}")
        if seeded:
            _eprint(f"[{PROG}] seeded: {', '.join(seeded)} "
                    f"(gitignored copies refreshed from {ws.repo})")
        for w in seed_warnings:
            _eprint(f"{PROG}: warning: {w}")
        if mirrored:
            _eprint(f"[{PROG}] plugins: {len(mirrored)} mirrored from the host, read-only")
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


# ---------------------------------------------------------------- status
def _status_probes():
    """The two live readings `status` derives from: one `docker ps -a` over
    our label answers container_exists for every entry, and os.kill(pid, 0)
    answers pid_alive. Preflight first, so a daemon that is down is an error
    with a remedy, never "every container is gone" (R7)."""
    backend = LocalDockerBackend()
    backend.preflight()
    names = {row["name"] for row in backend.list_containers()}
    return names.__contains__, state.pid_alive


def cmd_status(args):
    recs = RunRecord.all()
    if args.sandbox_id:
        wanted = set(args.sandbox_id)
        recs = [r for r in recs if r.sandbox_id in wanted]
        missing = wanted - {r.sandbox_id for r in recs}
        if missing:
            return _fail(SandboxError(f"no such sandbox: {', '.join(sorted(missing))}",
                                      f"{PROG} list shows every sandbox id"))
    container_exists, pid_alive = _status_probes()
    settings = admission.resolve_settings(config.load_config())
    probes = dict(container_exists=container_exists, pid_alive=pid_alive,
                  wait_timeout_s=settings.wait_timeout_s,
                  wait_interval_s=settings.wait_interval_s)

    rows = []
    for rec in recs:
        derived = state.derive_record(rec, **probes)
        reconciled = None
        if args.reconcile and derived.changed:
            reconciled = state.reconcile(rec.file, **probes)
            fresh = RunRecord.load_path(rec.file)
            if fresh is not None:
                rec = fresh
                derived = state.derive_record(rec, **probes)
        d = rec.data
        row = derived.to_dict(rec)
        row.update({
            "tags": d.get("tags") or {},
            "workspace": d.get("workspace"),
            "branch": d.get("branch"),
            "logs": d.get("logs"),
            "reconciled": reconciled.to_dict() if reconciled else None,
        })
        rows.append(row)

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("no sandboxes")
        return 0
    print(f"{'SANDBOX ID':<28} {'STATE':<9} {'NEWEST':<10} {'FIX':>3}  {'TAGS':<22} EVIDENCE")
    for row in rows:
        newest = row["newest"] or {}
        tags = ",".join(f"{k}={v}" for k, v in sorted(row["tags"].items())) or "-"
        fix = row["corrections"]
        if row["reconciled"]:
            fix = f"{len(row['reconciled']['corrected'])}w" if row["reconciled"]["written"] else "!"
        print(f"{row['sandbox_id']:<28} {row['state']:<9} {(newest.get('status') or '-'):<10} "
              f"{str(fix):>3}  {tags[:22]:<22} {newest.get('evidence') or '-'}")
    if any(r["corrections"] and not r["reconciled"] for r in rows) and not args.reconcile:
        _eprint(f"[{PROG}] FIX counts stale entries; {PROG} status --reconcile corrects them")
    for r in rows:
        if r["reconciled"] and not r["reconciled"]["written"]:
            _eprint(f"[{PROG}] {r['sandbox_id']}: not written: {r['reconciled']['reason']}")
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
        val = config.parse_value(args.value)
        if val is None:
            cfg.pop(args.key, None)          # `null` restores the default
        else:
            cfg[args.key] = val
        config.save_config(cfg)
        print(f"{args.key} = {val if isinstance(val, str) else json.dumps(val)}")
        return 0
    return 1


# ---------------------------------------------------------------- admission
def cmd_admission(args):
    cfg = config.load_config()
    settings = admission.resolve_settings(cfg)
    if args.action == "install":
        config.ensure_dirs()
        state = admission.install(settings.budget)
        if args.json:
            print(json.dumps(state, indent=2))
        else:
            print(f"{state['slice']} MemoryMax={admission.fmt(state['memory_max'])} "
                  f"({settings.sources['budget']} budget); recorded in {admission.state_file()}")
            if not settings.enabled:
                print(f"admission is off; enable it with: {PROG} config set admission_enabled true")
        return 0
    if args.action == "show":
        # The same readings a launch takes, decided for the default request,
        # so a refusal can be understood without launching anything.
        res = resources.ResourceConfig(cfg=cfg)
        fresh = admission.read_memlog(settings)
        rows = admission.inspect_running()
        committed = admission.committed_memory(rows, fresh.sample, settings.budget)
        try:
            avail = admission.read_mem_available()
        except (OSError, ValueError):
            avail = 0
        # `show` always decides, even when admission is off, so the numbers
        # are visible before a host turns it on.
        live = dataclasses.replace(settings, enabled=True)
        d = admission.decide(res.memory_bytes, committed, avail, fresh, None, live,
                             request_source=res.memory_source)
        state = admission.read_state()
        if args.json:
            print(json.dumps({"enabled": settings.enabled, "sources": settings.sources,
                              "wait_timeout_s": settings.wait_timeout_s,
                              "wait_interval_s": settings.wait_interval_s,
                              "containers": rows, "memlog": {"fresh": fresh.fresh,
                                                             "reason": fresh.reason,
                                                             "age_s": fresh.age_s},
                              "decision": d.to_dict(), "slice": state}, indent=2))
        else:
            print(f"admission {'enabled' if settings.enabled else 'DISABLED'} "
                  f"({settings.sources['enabled']}); {admission.describe(d)}")
            for r in rows:
                lim = admission.fmt(r["memory_limit"]) if r["memory_limit"] else "unlimited"
                used = (fresh.sample.containers.get(r["name"]) if fresh.sample else None)
                print(f"  {r['name']}: limit {lim}"
                      + (f", using {admission.fmt(used)}" if used is not None else ""))
            print(f"memory log: {'fresh' if fresh.fresh else 'STALE: ' + fresh.reason}")
            if state:
                print(f"slice {state['slice']}: MemoryMax={admission.fmt(state['memory_max'])} "
                      f"set {state['set_at']}" + ("" if state["ok"] else " (FAILED)"))
            else:
                print(f"slice not set: {PROG} admission install")
            print(f"a default run ({admission.fmt(res.memory_bytes)}) would: {d.verdict}"
                  + (f" ({'; '.join(d.reasons)})" if d.reasons else ""))
        return 0
    return 1


# ---------------------------------------------------------------- memlog
def _since_monotonic(spec, mono_now):
    """`--since` as a duration (90s, 30m, 2h) or an ISO time, on the monotonic scale."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smh])", spec.strip())
    if m:
        secs = float(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)]
        return mono_now - secs
    try:
        then = datetime.datetime.fromisoformat(spec)
    except ValueError:
        raise SandboxError(f"--since {spec!r} is neither a duration nor an ISO time",
                           "Give a duration like 90m or 2h, or an ISO time like "
                           "2026-09-13T16:00:00+00:00.")
    if then.tzinfo is None:
        then = then.astimezone()
    elapsed = (datetime.datetime.now(datetime.timezone.utc) - then).total_seconds()
    return mono_now - elapsed


def cmd_memlog(args):
    if args.action == "install":
        config.ensure_dirs()
        written = memlog.install()
        for path in written:
            print(f"wrote {path}")
        print(f"enabled {memlog.TIMER}; samples land in {memlog.LOG}")
        return 0
    if args.action == "sample":
        config.ensure_dirs()
        line = memlog.sample(memlog.LOG)
        if args.json:
            print(json.dumps(dataclasses.asdict(memlog.parse_line(line))))
        return 0
    if args.action == "show":
        cfg = config.load_config()
        boot = memlog.read_boot_id()
        mono = memlog.read_monotonic()
        fresh = memlog.parse_last_sample(memlog.LOG, boot, mono, memlog.max_age_s(cfg))
        since = _since_monotonic(args.since, mono)
        minimum = memlog.minimum_since(memlog.LOG, since, boot)
        samples = list(memlog.iter_samples(memlog.LOG))[-args.last:]
        if args.json:
            print(json.dumps({
                "log": str(memlog.LOG),
                "timer_active": memlog.timer_active(),
                "fresh": fresh.fresh, "reason": fresh.reason, "age_s": fresh.age_s,
                "minimum_since": ({"mem_available": minimum[0], "time": minimum[1]}
                                  if minimum else None),
                "samples": [dataclasses.asdict(s) for s in samples],
            }, indent=2))
            return 0 if fresh.fresh else 1
        for s in samples:
            cs = " ".join(f"{n}={b / 1024 ** 2:.0f}M" for n, b in s.containers.items())
            print(f"{s.time}  avail={s.mem_available / 1024 ** 3:.1f}G  "
                  f"swap_free={s.swap_free / 1024 ** 3:.1f}G  {cs}")
        if not samples:
            print(f"no samples in {memlog.LOG}")
        state = "fresh" if fresh.fresh else f"STALE: {fresh.reason}"
        age = f", {fresh.age_s:.0f} s old" if fresh.age_s is not None else ""
        print(f"last sample: {state}{age}; timer "
              f"{'active' if memlog.timer_active() else 'INACTIVE'}")
        if minimum:
            print(f"minimum MemAvailable since {args.since}: "
                  f"{minimum[0] / 1024 ** 3:.1f}G at {minimum[1]}")
        else:
            print(f"no samples since {args.since}")
        if not fresh.fresh:
            print(f"  → {PROG} memlog install   (then wait one minute)")
        return 0 if fresh.fresh else 1
    return 1


# ---------------------------------------------------------------- parser
def build_parser():
    p = argparse.ArgumentParser(
        prog=PROG,
        description="Run coding agents in an isolated git worktree inside a "
                    "hardened, disposable rootless-Docker container.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""examples:
  {PROG} .                          this repo's sandbox (resumed), or a new one
  {PROG} . --new                    a fresh sandbox, even if one exists
  {PROG} ~/code/app -- claude       run Claude Code in a sandbox
  {PROG} . -- npm test              one-shot command
  {PROG} . --direct -- pytest       operate on the live checkout (locked)
  {PROG} list                       show sandboxes
  {PROG} status --json              derived state of every container entry
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
        sp.add_argument("--tag", action="append", metavar="KEY=VALUE", default=[],
                        help="label this run (repeatable); `milestone=<n>` also takes "
                             "the project's milestone lock")
        wait = sp.add_mutually_exclusive_group()
        wait.add_argument("--wait", dest="wait", action="store_true", default=True,
                          help="wait for admission headroom (the default)")
        wait.add_argument("--no-wait", dest="wait", action="store_false",
                          help="fail at once instead of waiting for headroom")
        sp.add_argument("--force", action="store_true",
                        help="bypass admission control for this run")
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
    sp.add_argument("--new", action="store_true",
                    help="create a fresh sandbox even if this repository already "
                         "has one (the default resumes it)")
    add_run_flags(sp)

    sp = sub.add_parser("enter", help="reopen an existing sandbox workspace")
    sp.add_argument("sandbox_id")
    add_run_flags(sp)

    sp = sub.add_parser("list", help="list sandboxes")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("status", help="the derived state of every sandbox's containers")
    sp.add_argument("sandbox_id", nargs="*",
                    help="only these sandboxes (default: all)")
    sp.add_argument("--reconcile", action="store_true",
                    help="write the corrections (crashed / orphaned entries and the "
                         "top-level status) back to run.json, guarded against a "
                         "record that changed under the read")
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

    sp = sub.add_parser("admission", help="the memory budget launches are admitted under")
    sp.add_argument("action", choices=["install", "show"],
                    help="install: cap the agent-sandbox.slice at the budget; "
                         "show: the effective numbers and what a run would get")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("memlog", help="the per-minute memory log launches depend on")
    sp.add_argument("action", choices=["install", "sample", "show"],
                    help="install: enable the systemd --user timer; sample: append one "
                         "line now; show: recent samples, freshness, and the minimum")
    sp.add_argument("--last", type=int, default=5, metavar="N",
                    help="show: how many recent samples to print (default 5)")
    sp.add_argument("--since", default="1h", metavar="WHEN",
                    help="show: minimum MemAvailable since a duration (90m, 2h) "
                         "or an ISO time (default 1h)")
    sp.add_argument("--json", action="store_true")

    return p


KNOWN = {"run", "enter", "list", "rm", "clean", "doctor", "build", "config", "memlog",
         "admission", "status"}


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
        if args.cmd == "status":
            return cmd_status(args)
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
        if args.cmd == "memlog":
            return cmd_memlog(args)
        if args.cmd == "admission":
            return cmd_admission(args)
    except (AdmissionRefused, AdmissionTimeout) as e:
        return _fail_admission(e, getattr(args, "json", False))
    except SandboxError as e:
        return _fail(e)
    except KeyboardInterrupt:
        _eprint("\ninterrupted")
        return 130
    return 0
