# agent-sandbox — Build Specification

Version 1.1.0.

Frozen specification, settled by design interview on 2026-09-06. This is the
contract the implementation is reviewed against. Numbered requirements (R-nn)
are testable; the review pass must cite evidence for each. R-01 to R-15 are
the 1.0.0 contract; R-16 to R-22 were added for 1.1.0 on 2026-09-07 from the
SSSF v1 plan (`~/sssf/PLAN.md`, requirements R1 to R11 and KTD1 to KTD14).

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
| Image toolchain (1.1) | Claude Code 2.1.263, Pi 0.85.1, pi-claude-bridge 0.7.0, uv 0.12.10, just 1.21.0, sqlite3 3.45.1 |

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
2. narrowly constructed sandbox-specific Claude config, only required fields.
   With an agent home (R-18) this is the per-sandbox writable `.claude.json`
   inside `runs/<id>/agent-home/claude/`, seeded once from the same
   allow-list and then owned by Claude Code for that sandbox; without an
   agent home it is the 1.0 read-only generated file mount.
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
agent-sandbox doctor [--quick] [--with-quota]
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

1.1 addendum: the image also carries `uv` and `uvx` copied from the official
image pinned by digest, `just`, `sqlite3`, and a Node toolchain (Claude Code
2.1.263, Pi 0.85.1) installed with `npm ci` from a committed lockfile under
`image/toolchain/`, so the pin is the lockfile rather than a version string.
`DISABLE_AUTOUPDATER=1`, `IS_SANDBOX=1`, and `CLAUDE_CONFIG_DIR=/root/.claude`
are image environment. A Pi agent template with pi-claude-bridge 0.7.0
(`image/pi-agent-template/`, its own lockfile) is built at
`/opt/agent-sandbox/pi-agent-template`, outside `/root` because the runtime
home mount shadows `/root`. The build fails unless the pinned versions report
back and the template lists `claude-bridge` models. The fingerprint hashes
every file under `image/` except `node_modules`, sorted by path.

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

### R-16 Typed mounts
- Every bind mount beyond the workspace, the package caches, and the
  credential files is a `Mount` (host path, container path, read-only flag,
  one-word purpose) carried on `SandboxSpec.mounts` and rendered as
  `--mount type=bind,...`, never `-v`.
- A configured mount whose host source does not exist is a hard error
  (`MountError`) naming the path and purpose, raised before the container
  starts. Nothing is ever created implicitly to satisfy a mount.
- One planner, `mounts.plan_for(workspace, config, agent_home)`, composes the
  agent-home, skill, and git mounts for both `run` and `enter`; `enter`
  re-derives the git set from the host rather than replaying the record.
- `run.json` records every declared mount under `mounts` (host, container,
  mode, purpose) and the git set additionally under `trusted_mounts`. The
  credential ladder (R-07) keeps its own module and disclosure record.

### R-17 Container environment
- Every container has `IS_SANDBOX=1` and `CLAUDE_CONFIG_DIR=/root/.claude`,
  set both as image environment and again at run time so a custom `--image`
  cannot drop them. `IS_SANDBOX=1` is the gate Claude Code reads before
  refusing `bypassPermissions` as root.
- A git identity is resolved per run from the source repository with
  `git -C <repo> config --get user.name` and `user.email` (local, then
  global) and injected as `GIT_AUTHOR_*` and `GIT_COMMITTER_*`. A worktree or
  direct workspace with no resolvable identity is a hard error before the
  container starts; a copy workspace gets no identity variables.
- No `~/.gitconfig` is mounted.

### R-18 Persistent agent home
- Each sandbox owns `runs/<id>/agent-home/{claude,pi-agent}`, bind-mounted
  read-write at `/root/.claude` and `/root/.pi/agent`, created 0700, seeded
  exactly once on the first run, never re-seeded by `enter`, and retained
  until `agent-sandbox rm <id>`.
- `seed.json` records the image fingerprint, the pinned tool versions, the
  template path, and the seed time. When the fingerprint no longer matches
  the current image, `run`, `enter`, and `doctor` print a drift warning that
  names the re-seed procedure (`rm`, then recreate) and still start.
- The Claude home is copied from the template named by the config key
  `agent_home_template` (default `templates/agent-home`). The shipped
  `settings.json` sets `permissions.defaultMode: bypassPermissions`,
  `skipDangerousModePermissionPrompt: true`, and a deny list covering
  `git push`, `gh pr|issue|release|repo`, `git worktree prune|repair|remove`,
  `git update-ref`, `git reflog`, `git gc`, and `git branch -D`; it sets no
  `model`.
- The seed also writes a per-sandbox writable `.claude.json` (0600) built from
  the R-07 layer-2 allow-list plus `/workspace` pre-trusted, an empty 0600
  `.credentials.json` so the read-only credentials mount never lands on a
  world-readable mountpoint, and empty `skills/` and `projects/` directories.
  The host `~/.claude.json` is never exposed.
- The Pi home is the image template copied out through a throwaway container:
  the bridge package, its `settings.json` package entry, `claude-bridge.json`
  pointing at the image's `claude` with `plan: max`, and `models.json` as
  `{"providers": {}}`.
- `agent-sandbox enter <id>` followed by `claude --resume` continues a session
  created in an earlier container of the same sandbox. A sandbox created by 1.0 has no home; its first 1.1 `enter` seeds one, with no prior session state to recover. A home whose seeding did not finish (no seed record) is rebuilt on the next run rather than trusted.

### R-19 Skill mounts
- `skill_mounts` in `config.json` is a list of host paths. Each is expanded
  and symlink-resolved on the host, must be a directory, and is mounted
  read-only at `/root/.claude/skills/<basename>` inside the agent home, on
  both `run` and `enter`.
- A missing or non-directory entry, a non-list value, or two entries sharing
  a basename is a hard error naming the entry, raised before any worktree or
  branch is created. An empty list mounts nothing.

### R-20 Git inside worktree sandboxes
- For a worktree workspace whose git common directory resolves to
  `<repo>/.git` (checked with `git rev-parse --git-common-dir` before the
  worktree is created), the container gets: the common directory read-write
  at its host path; the worktree a second time at its host path; read-only
  overlays for `.git/config`, `.git/config.worktree`, `.git/HEAD`, `.git/index`, `.git/hooks`,
  `.git/modules`, and `.git/worktrees`; the sandbox's own
  `.git/worktrees/<id>` read-write on top of the `worktrees` overlay; and its
  `config.worktree` read-only on top of that. The parent checkout is never
  mounted.
- An overlay whose host source is absent is served from an empty file or
  directory created under `runs/<id>/git-overlays/`, so no overlay is ever
  skipped.
- A repository whose common directory is not `<repo>/.git` (a submodule or a
  linked worktree) runs with no git mounts and a named warning, the 1.0
  behaviour. `preflight()` and this check both run before `worktree.create`,
  so a Docker outage or an unsupported repository shape leaves no orphan
  worktree or branch.
- `rm` and `clean` never remove a path inside the host repository's git
  directory; a guard refuses and reports it as a bug. The host repository is
  touched only by `git worktree remove`, `git worktree prune`, and deleting
  the sandbox branch.
- `--rm` honours the unpushed-commit guard regardless of exit status: a
  workspace whose branch carries commits present on no other branch is kept
  and the guard's reason is printed under the existing "ignored" notice.
- What stays writable is the accepted residual exposure: refs, reflogs,
  objects, and the index files of other worktrees of that repository. The
  R-18 deny list binds only the Claude operator; other processes in the
  container are bound by the overlays alone.

### R-21 Timeout stops before it kills
On expiry the backend runs `docker stop -t 30` (SIGTERM, 30 s grace) and
then `docker kill` for whatever is still up, so an agent's own signal
handlers can close their traces. Status is still `timed_out`; workspace and
logs are still preserved (R-03).

### R-22 doctor for 1.1
- Host checks: a git identity resolves for a throwaway repository with a
  local identity; the global identity is set (warn otherwise); every
  configured `skill_mounts` entry validates; the agent home template
  validates; the host access token's remaining lifetime, read from the expiry
  field only, warns under one hour.
- Sandbox probe, one throwaway repository, two containers: a commit inside a
  worktree sandbox appears on the host branch; a write to `.git/hooks` is
  refused; `git worktree prune` inside the container is a no-op; `pi
  --list-models` lists `claude-bridge` models from both the image template and
  the seeded home; `claude -p` with bypass and no credentials reports the
  not-logged-in outcome rather than the root refusal, spending no quota; a
  file written in the first container is present in the second. The
  throwaway repository, worktree, and run record are removed in `finally`.
- Every existing sandbox whose `seed.json` fingerprint differs from the
  current image is named in one drift warning.
- `--with-quota` opts in to one authenticated `claude -p` inside the probe;
  nothing else spends quota.
- Probe output written to evidence passes through a redactor that masks the
  value of any key containing `TOKEN`, `KEY`, or `SECRET`.

### R-23 Worktree seeding
- `worktree_seed` in `config.json` is a list of repo-relative paths (files or
  directories). On `run` against a git repository, each entry that exists in
  the source checkout **and is gitignored there** is copied into the new
  worktree right after `git worktree add`, permissions preserved. Missing
  entries are skipped. `enter` never re-seeds; `--direct` and copy workspaces
  are unaffected.
- An entry that is present but not gitignored is not copied and produces a
  warning: a tracked path is already in the worktree, and an untracked one
  would be committed from inside the sandbox. An absolute entry, one
  containing `..`, or a non-list value is a hard error naming the entry.
- The copied paths are disclosed on stderr before the container starts and
  recorded as `seeded` in `run.json`. An empty list copies nothing.

## Deferred (must NOT be built now)

`--network restricted` implementation, paranoid/microVM backend, per-repo
config files, milestone orchestration, model routing, judges, evals, factory
logic, dashboards.
