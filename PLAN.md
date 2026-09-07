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
    backend.py      # SandboxBackend ABC + SandboxSpec/SandboxResult  (R-12)
    docker_backend.py  # LocalDockerBackend  (R-02, R-05, R-06)
    image.py        # fingerprint, auto-build  (R-11)
    doctor.py       # health checks  (R-10)
    cleanup.py      # rm / clean / docker prune  (R-14)
    cli.py          # argparse wiring only  (R-08)
  image/Dockerfile               # (R-11)
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

## Risks

| Risk | Mitigation |
|---|---|
| gVisor cannot enforce limits | opt-in only, loud warning, documented (R-01) |
| rootless UID mapping surprises | run as container UID 0, documented (R-02) |
| WSL PATH pollution breaking exec | build a clean env for docker invocations |
| interactive TTY vs captured logs | allocate tty only when interactive; tee otherwise |
| disk growth from images | doctor threshold + targeted `clean --docker` (R-14) |
