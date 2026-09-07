# agent-sandbox — Implementation Plan

Derived from SPEC.md. Each milestone lists the files it produces and the
acceptance criteria the review pass must verify. Requirement IDs (R-nn) map
back to the spec.

## Layout

```
~/agent-sandbox/
  SPEC.md  PLAN.md  REVIEW.md  README.md
  bin/agent-sandbox              # exec shim, no logic
  agent_sandbox/
    __init__.py
    errors.py       # typed errors carrying actionable remediation text
    config.py       # defaults, config.json, env overrides, paths
    resources.py    # ResourceConfig  (R-03)
    metadata.py     # RunRecord + store  (R-09)
    worktree.py     # worktree/copy workspace lifecycle + direct lock  (R-04)
    credentials.py  # layered credential resolution  (R-07)
    mounts.py       # typed bind mounts, skill mounts, git identity  (R-16, R-17, R-19)
    agent_home.py   # persistent per-sandbox agent home  (R-18)
    gitdir.py       # git common-dir mount and overlays  (R-20)
    backend.py      # SandboxBackend ABC + SandboxSpec/SandboxResult  (R-12)
    docker_backend.py  # LocalDockerBackend  (R-02, R-05, R-06)
    image.py        # fingerprint, auto-build  (R-11)
    doctor.py       # health checks  (R-10)
    cleanup.py      # rm / clean / docker prune  (R-14)
    cli.py          # argparse wiring only  (R-08)
  image/Dockerfile               # (R-11)
  image/toolchain/  image/pi-agent-template/   # pinned Node tools and Pi template (1.1)
  templates/agent-home/          # seeded Claude settings.json (R-18)
  config.json                    # user config, created on first run
  worktrees/  runs/  logs/  cache/{npm,pnpm,pip}
~/.local/bin/agent-sandbox -> ~/agent-sandbox/bin/agent-sandbox
```

Rule: `cli.py` holds no policy. Every decision lives in a module callable as a
library, so SSSF can `from agent_sandbox import ...` later (R-12).

## M1 — Foundation

Files: `errors.py`, `config.py`, `resources.py`, `metadata.py`.

- `config.py` owns all paths, loads/saves `config.json`, resolves precedence
  **flag > env > config.json > built-in default**.
- `resources.py` parses `8`, `16g`, `2048`, `12h`; defaults cpus 8 / memory 16g
  / pids 2048 / timeout 12h (R-03).
- `metadata.py` writes `runs/<id>/run.json` atomically (temp + rename) so
  concurrent sandboxes never corrupt each other (R-09, R-13); appends to a
  `containers` list rather than overwriting (R-01 enter semantics).

Acceptance: defaults match spec exactly; env vars override; two processes
writing records concurrently produce two valid files.

## M2 — Workspace lifecycle

File: `worktree.py` (R-04).

- resolve canonical repo root via `git rev-parse --show-toplevel`
- sandbox id: `<repo-slug>-<8 hex>`; branch `agent-sandbox/<sandbox-id>`
- `git worktree add -b <branch> ~/agent-sandbox/worktrees/<id> HEAD`
- non-git path → copy tree into the worktree dir (excluding nothing by
  default), recorded as `kind: "copy"`
- `--direct` → advisory lock `runs/.locks/<repo-hash>.lock` holding pid+time,
  stale (dead pid) locks reclaimed; released on exit
- `list_worktrees`, `remove(id, force)`, `has_uncommitted_work(id)`
- `/mnt/` path → warning, never a block (R-13)

Acceptance: worktree created on a real repo; branch exists; two concurrent
worktrees on the same repo succeed; `--direct` second run refuses while first
holds the lock; stale lock reclaimed.

## M3 — Image

Files: `image/Dockerfile`, `image.py` (R-11).

- Ubuntu 24.04 base; one apt layer for system packages; Node LTS via NodeSource;
  pnpm via corepack; gh via its apt repo; Claude Code via
  `npm i -g @anthropic-ai/claude-code`; ordered least-changing-first for cache
  reuse.
- Non-root user `agent` created (uid 1000) for optional use, but the default
  run uses UID 0 inside the rootless namespace (R-02) — documented in the file.
- `image.py` computes a SHA256 of the Dockerfile, stores it in
  `runs/.image-state.json`, rebuilds when it moves or the image is missing.

Acceptance: image builds; every tool in R-11 present and on PATH inside it;
second `build` with unchanged Dockerfile is a no-op; fingerprint change forces
rebuild.

## M4 — Backend and execution

Files: `backend.py`, `docker_backend.py`, `credentials.py`
(R-02, R-05, R-06, R-07, R-12).

- `SandboxSpec` (workspace, command, resources, mode, network, flags,
  credentials) → `SandboxBackend.run(spec) -> SandboxResult`.
- `LocalDockerBackend` builds argv:
  `--rm --init --network <policy> --cpus --memory --pids-limit
   --security-opt no-new-privileges --cap-drop ALL
   --label agent-sandbox.id=<id> --label agent-sandbox.managed=true
   -v <workspace>:/workspace -v <cache>/npm:/root/.npm ...
   -w /workspace [-i -t if interactive] [--read-only + tmpfs if flag]
   [--runtime=runsc if experimental]`
- Refuse to run if the resolved daemon is rootful (R-02).
- Timeout enforced by the parent with `subprocess` + kill, status `timed_out`,
  worktree preserved (R-03).
- Logs streamed to `runs/<id>/stdout.log` / `stderr.log` while still reaching
  the terminal for interactive use.
- `credentials.py` implements the 4-layer ladder, returns the mounts/env plus a
  human-readable description of exactly what is exposed (R-07).

Acceptance: hardening flags present in the real argv; network none blocks and
full reaches the internet; restricted fails fast; memory/pids limits enforced
inside a container; timeout kills and marks `timed_out`; credential layer 1
tried first and layer 4 never automatic.

## M5 — CLI, doctor, cleanup

Files: `cli.py`, `doctor.py`, `cleanup.py`, `bin/agent-sandbox`
(R-08, R-10, R-14).

- argparse with the exact surface in R-08; `--` separates the agent command;
  bare `agent-sandbox <repo>` opens an interactive shell.
- `--json` prints one JSON object with the R-09 fields and suppresses
  decorative output.
- `doctor` runs every check in R-10 including a real container, prints
  PASS/WARN/FAIL with a remediation line per failure, exits non-zero on FAIL.
- `clean --docker` prunes dangling images, build cache, and only containers
  carrying `agent-sandbox.managed=true` (R-14).

Acceptance: every subcommand runs; `doctor` passes on this machine; `--json`
output parses and carries all R-09 fields; `clean --docker` leaves unrelated
docker objects untouched.

## M6 — Verification (R-15)

Against a throwaway repo in `/tmp`, never an existing repo:

1. `doctor` passes
2. shell sandbox runs a command and exits
3. file isolation: writes inside the sandbox do not appear in the source repo
4. worktree behavior: branch created, files present, list/enter/rm work
5. resource limits: memory kill, pids fork failure inside the sandbox
6. cleanup: no container survives the run (`docker ps -a` clean)
7. persistence: a file written in the container survives container destruction
8. concurrency: two sandboxes on one repo simultaneously
9. `--network none` blocks, `full` reaches out, `restricted` errors
10. `--direct` lock refuses a concurrent second run
11. Claude Code auth inside the sandbox (layer ladder), if it authenticates
12. `--json` schema check

Each recorded in REVIEW.md with the command and observed evidence.

## M7 — Review and document

- REVIEW.md: table of R-01..R-15, verdict, evidence, gaps. Fix and re-review
  until every row passes or is explicitly deferred by the spec.
- README.md: architecture, paths, everyday commands, exactly what credentials
  are exposed, security limitations, known issues, SSSF integration notes.

## M8 — v1.1: agent home, skill mounts, git in worktree sandboxes

Derived from `~/sssf/PLAN.md` (units U1 to U9, requirements R1 to R11), which
adds SPEC.md R-16 to R-22. Baseline commit `3ece0c7` is the untouched 1.0.0
tree; every unit below is one commit on `main`.

Files: `mounts.py` (R-16, R-17, R-19), `agent_home.py` (R-18), `gitdir.py`
(R-20), `templates/agent-home/claude/settings.json`, `image/toolchain/`,
`image/pi-agent-template/`, plus changes in `cli.py`, `credentials.py`,
`config.py`, `metadata.py`, `docker_backend.py`, `worktree.py`, `image.py`,
`doctor.py`, `image/Dockerfile`.

| Unit | Commit | What it delivers |
|---|---|---|
| U1 | `3ece0c7`, `deab465` | repository baseline, `.gitignore` for state and `config.json`, version 1.1.0 |
| U2 | `176d619` | `uv`, `just`, `sqlite3`, Claude Code 2.1.263 and Pi 0.85.1 from a lockfile, Pi template with pi-claude-bridge 0.7.0 outside `/root`, image env, fingerprint over all of `image/` |
| U3 | `45d29a3` | `Mount` and `SandboxSpec.mounts` rendered with `--mount`, `MountError`, `IS_SANDBOX`, `CLAUDE_CONFIG_DIR`, per-run git identity |
| U4, U5 | `f0dbc87` | `agent_home.ensure`: 0700 home seeded once, `seed.json`, drift warning, writable per-sandbox `.claude.json`, 0600 credentials mountpoint, Pi home from the image; `skill_mounts` and `agent_home_template` config keys; `run.json` `agent_home` and `mounts` |
| U6 | `8e91a1a` | `gitdir.check` before `worktree.create`, common-dir mount, second worktree mount, read-only overlays with stand-ins, `trusted_mounts`, removal guard, `--rm` through the unpushed-commit guard, `preflight()` first |
| U7 | `c8b8279` | timeout runs `docker stop -t 30` then `docker kill`; `public()` carries the new fields |
| U8 | `ffc609a` | doctor host checks, two-container sandbox probe, drift check, `--with-quota`, evidence redaction |
| U9 | this change | SPEC 1.1.0 with R-16 to R-22, README, REVIEW v1.1 section, this milestone |

Order inside `cmd_run`: `preflight()`, skill-mount validation, `gitdir.check`,
`worktree.create`, `image.ensure`, `agent_home.ensure`, `credentials.resolve`,
`mounts.plan_for`, then the spec. `cmd_enter` uses the same helpers and never
re-seeds.

Acceptance (each observed and recorded in REVIEW.md, v1.1 section):

- `doctor` exits 0 with every new check PASS (observed: 28 passed, 0 warnings,
  0 failed).
- `IS_SANDBOX=1`, `CLAUDE_CONFIG_DIR=/root/.claude`, and the repository's local
  git identity are visible inside the container.
- A commit made inside a worktree sandbox appears on the host branch;
  `git worktree prune` inside the container prunes nothing; writes to
  `.git/hooks`, `.git/config`, `.git/HEAD`, `.git/modules`, `.git/worktrees`,
  and `config.worktree` are refused.
- A Docker outage or a bad `skill_mounts` entry fails before any worktree or
  branch exists; a linked-worktree target starts with a warning and no trusted
  mounts.
- The agent home is 0700 with 0600 `.claude.json` and `.credentials.json`; a
  session created with `claude -p --session-id` resumes after `enter` with the
  same id; `settings.json` is not rewritten by `enter`; the seeded Pi home lists
  `claude-bridge` models.
- A configured symlinked skill directory is read-only under
  `/root/.claude/skills/<name>`.
- `--rm` keeps a workspace whose branch has commits on no other branch and
  prints the reason; removes it otherwise.
- A container trapping SIGTERM sees the trap run under a timeout and the run
  ends `timed_out`.
- A change to any file under `image/` flips `needs_build`; `node_modules` is
  ignored.

## M9 — v1.2: worktree seeding (R-23)

One commit. `worktree.seed()` copies the gitignored paths named by
`worktree_seed` from the source checkout into a fresh worktree; `cmd_run`
calls it right after the record is first saved, discloses the copies, and
records them under `seeded`. Motivation: the vibeset-dj factory ran a full
chain against a worktree with no `.env`, silently measuring a degraded
source set, because gitignored files never cross `git worktree add`.

## M10 — v1.2: resume by default (R-04)

One commit. `cmd_run` looks up intact sandboxes of the target repository
before creating anything; one match is re-entered, several are offered on a
terminal, `--new` skips the lookup. Motivation: `agent-sandbox .` after a
pause created a second sandbox with an empty agent home, and the engineer
could not find their conversations.

## Risks

| Risk | Mitigation |
|---|---|
| gVisor cannot enforce limits | opt-in only, loud warning, documented (R-01) |
| rootless UID mapping surprises | run as container UID 0, documented (R-02) |
| WSL PATH pollution breaking exec | build a clean env for docker invocations |
| interactive TTY vs captured logs | allocate tty only when interactive; tee otherwise |
| disk growth from images | doctor threshold + targeted `clean --docker` (R-14) |
