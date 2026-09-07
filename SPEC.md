# agent-sandbox — Build Specification

Frozen specification, settled by design interview on 2026-09-06. This is the
contract the implementation is reviewed against. Numbered requirements (R-nn)
are testable; the review pass must cite evidence for each.

## Purpose

A generic local execution substrate for coding agents. Isolated git worktree →
disposable hardened container → agent. Reusable underneath SSSF, Claude Code,
Codex, pi, or anything else, across arbitrary repositories, zero per-repo setup.

Explicit non-goals: milestone orchestration, model routing, judges, evals,
project-specific config, factory logic. Those are SSSF. This is the substrate.

## Environment (verified 2026-09-06)

| Fact | Value |
|---|---|
| OS | Ubuntu 24.04.3 LTS, WSL2, kernel 6.6.87.2-microsoft-standard-WSL2 |
| systemd | active, user manager running, linger enabled for `bishal` |
| cgroups | v2 unified; `cpu memory pids` delegated to user@1000.service |
| CPU / RAM | 20 logical CPUs / 47 GiB RAM / 16 GiB swap |
| Disk | 245 GiB free of 1007 GiB (75% used) |
| Docker | rootless 29.8.0, socket `/run/user/1000/docker.sock`, no `docker` group |
| gVisor | runsc release-20260831.0 installed |
| Claude auth | `~/.claude/.credentials.json` (0600, OAuth); no ANTHROPIC_API_KEY set |
| gh | authenticated, scopes gist/read:org/repo/workflow |

### Verified runtime findings

- **Rootless Docker enforces limits.** memory: growth test under `--memory=64m`
  SIGKILLed (exit 137). pids: `--pids-limit=10` produced
  `can't fork: Resource temporarily unavailable`. cpus: accepted, cgroup v2
  delegation chain confirmed down to `docker.service`.
- **gVisor cannot enforce limits here.** `runsc` only runs rootless with
  `-ignore-cgroups`; with systemd cgroup driver it hits polkit
  ("Interactive authentication required"), with cgroupfs driver it hits
  `open /sys/fs/cgroup/cgroup.subtree_control: permission denied`. With
  `-ignore-cgroups` containers run and network works, but `--memory`, `--cpus`
  and `--pids-limit` are NOT host-enforced.

Consequence: gVisor is a trade, not an upgrade. Resource limits are a baseline
requirement for every mode, so gVisor cannot back the default `safe` mode.

## Requirements

### R-01 Modes
- `fast` and `safe` are capability labels, stable in the CLI regardless of
  implementation. Both currently resolve to rootless Docker + runc + full
  baseline hardening (identical), because no profile on this machine is
  stronger while still meeting the baseline.
- `--experimental-gvisor` is an opt-in escape hatch layering runsc, which
  explicitly forfeits host-enforced resource limits. Must warn loudly.
- Mode default read from config; `--mode` overrides per run.

### R-02 Baseline hardening (every mode, every run)
- rootless daemon only; refuse to run against a rootful daemon
- `--security-opt no-new-privileges`
- `--cap-drop ALL`
- no Docker socket mounted, ever
- no host SSH keys, no `$HOME` mount, no `/` mount
- container main process runs as UID 0 *inside the container*, which rootless
  maps to the host user — documented exception to "non-root", chosen for
  correct file ownership in the worktree
- ephemeral container (`--rm`), persistent worktree
- resource limits always applied

### R-03 Resource configuration
Defaults: cpus 8, memory 16g, pids 2048, timeout 12h (single universal
default). Overridable by flag and by environment variable
(`AGENT_SANDBOX_CPUS`, `_MEMORY`, `_PIDS`, `_TIMEOUT`). Timeout is a hard
wall-clock ceiling; on expiry terminate cleanly, preserve worktree/logs/metadata,
mark run `timed_out` (distinct from `failed`).

### R-04 Worktrees
- resolve canonical repo root from any path inside the repo
- create worktree at `~/agent-sandbox/worktrees/<sandbox-id>/`
- new branch per run: `agent-sandbox/<repo-slug>-<short-id>`
- persist after exit by default; never auto-deleted on failure
- `--direct` operates on the live checkout, guarded by an advisory per-repo
  lock (PID + timestamp, stale locks auto-cleared); worktree mode takes no lock
- non-git paths supported via a copied workspace

### R-05 Filesystem exposure
- only the workspace is bind-mounted read-write
- `--read-only-root` is an independent flag (either mode): root filesystem
  read-only, with `/workspace`, tmpfs `/tmp`, and container `$HOME` writable
- package caches from `~/agent-sandbox/cache/{npm,pnpm,pip}`, never the host's
  real caches

### R-06 Network
`--network full` (default) works; `none` works; `restricted` is recognized and
fails fast with a clear not-implemented error. `restricted` must never silently
map to `full`.

### R-07 Credentials
Layered, each tried only if the previous is insufficient:
1. mount `~/.claude/.credentials.json` read-only, alone
2. narrowly constructed sandbox-specific Claude config, only required fields
3. `ANTHROPIC_API_KEY` env injection, no file mount
4. `--with-full-claude-state` explicit opt-in to mount `~/.claude.json` ro
Never automatic escalation to layer 4. GitHub: none by default;
`--with-github-auth` injects `GH_TOKEN` from `gh auth token` for that run only.
Nothing baked into the image; nothing copied into git.

### R-08 CLI
```
agent-sandbox <repo> [-- <command...>]
agent-sandbox <repo> --direct [-- <command...>]
agent-sandbox list
agent-sandbox enter <sandbox-id>
agent-sandbox rm <sandbox-id> [--force]
agent-sandbox clean [--older-than N] [--force] [--docker]
agent-sandbox doctor
agent-sandbox build [--no-cache]
```
Flags: `--mode`, `--cpus`, `--memory`, `--pids-limit`, `--timeout`,
`--network`, `--read-only-root`, `--with-github-auth`,
`--with-full-claude-state`, `--experimental-gvisor`, `--json`, `--keep`/`--rm`.

### R-09 Observability
Per run, persisted outside the container under `~/agent-sandbox/runs/<id>/`:
sandbox id, repo, worktree path, branch, command, timestamps, exit code,
status, container id(s), image, resource settings, mode, network, log paths.
`enter` reuses the sandbox id and appends to a `containers` history.
`--json` emits the structured result. No dashboard.

### R-10 doctor
Verifies: WSL2, systemd user manager, cgroup v2 delegation, rootless Docker
reachable, rootful daemon not in use, image present, git, gh, credential
prerequisites, worktree dir writable, disk headroom, gVisor status, and
actually executes a trivial container end to end. Errors must be actionable.

### R-11 Image
One reusable Ubuntu-based image: git, curl, wget, ca-certificates, bash, jq,
ripgrep, fd, build-essential, Python3 + pip + venv, Node LTS + npm, pnpm,
archive utils, gh, Claude Code via its supported install method. Understandable
Dockerfile, layer-cache friendly. Auto-built transparently on first use;
rebuild triggered by Dockerfile fingerprint change. `build` forces a rebuild.

### R-12 Structure for future SSSF integration
Separate modules: sandbox lifecycle, worktree lifecycle, execution, resource
config, credentials, metadata/logs. `SandboxBackend` ABC with
`LocalDockerBackend` as the only implementation. Callable as a library without
going through the interactive CLI. No other backends built now.

### R-13 Performance
Fast startup, base image reuse, WSL-native filesystem, no unnecessary copies,
shared caches, concurrent sandboxes against one repo without global locking.
Warn (not block) when a repo lives under `/mnt/` (Windows filesystem).

### R-14 Disk hygiene
`doctor` reports Docker disk usage with a warning threshold. `clean --docker`
prunes only dangling images, build cache, and stopped containers labeled by
this tool. Never touches unrelated containers/images/networks/volumes. Nothing
automatic or background.

### R-15 Deliverables
Implemented and tested on the machine: image built, CLI on PATH, doctor passes,
shell sandbox against a throwaway repo, file isolation verified, worktree
behavior verified, resource limits verified, container cleanup verified, work
survives container destruction, Claude Code tested inside the sandbox if auth
permits, usage documented. Existing repos not modified during testing.

## Deferred (must NOT be built now)

`--network restricted` implementation, paranoid/microVM backend, per-repo
config files, milestone orchestration, model routing, judges, evals, factory
logic, dashboards.
