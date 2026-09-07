# agent-sandbox

Run coding agents against your repositories inside an isolated git worktree and
a hardened, disposable rootless-Docker container. The container is thrown away;
the work survives.

```bash
agent-sandbox .                      # isolated worktree, interactive shell
agent-sandbox ~/code/app -- claude   # run Claude Code in a sandbox
agent-sandbox . -- npm test          # one-shot command
```

No per-repository setup. Works on any repo you have now or add later, with no
assumptions about language, test runner, or branch strategy.

## Architecture

```
your repo  (never modified)
    │
    ├─ git worktree ──► ~/agent-sandbox/worktrees/<sandbox-id>/
    │                   own branch, survives everything
    │
    └─ rootless Docker container  (disposable, --rm)
           └─ your agent: claude, bash, pytest, anything
```

One sandbox id owns one workspace and may outlive many containers. Metadata and
logs live outside the container in `~/agent-sandbox/runs/<sandbox-id>/`, so a
destroyed container never takes your results with it.

The substrate is deliberately generic. It knows nothing about milestones,
model routing, judges, or evals — those belong to whatever drives it.

## Everyday commands

| Command | What it does |
|---|---|
| `agent-sandbox .` | worktree + hardened container + interactive shell |
| `agent-sandbox . -- claude` | run Claude Code inside the sandbox |
| `agent-sandbox . -- claude -p "Implement X"` | one-shot agent run |
| `agent-sandbox . -- npm test` | run any command |
| `agent-sandbox . --direct -- pytest` | operate on your live checkout (locked) |
| `agent-sandbox list` | all sandboxes, status, age, workspace |
| `agent-sandbox enter <id>` | fresh container over an existing workspace |
| `agent-sandbox rm <id>` | delete a workspace (refuses to discard work) |
| `agent-sandbox clean` | sweep sandboxes older than 14 days |
| `agent-sandbox clean --docker` | also prune dangling images and build cache |
| `agent-sandbox doctor` | verify the whole stack end to end, including git and Claude inside a probe sandbox |
| `agent-sandbox doctor --with-quota` | the same, plus one authenticated `claude -p` (spends a little quota) |
| `agent-sandbox build` | rebuild the base image |
| `agent-sandbox config show\|set` | persistent defaults |

Useful flags: `--cpus`, `--memory`, `--pids-limit`, `--timeout`, `--network`,
`--mode`, `--read-only-root`, `--strict-caps`, `--with-github-auth`,
`--experimental-gvisor`, `--json`, `--keep`/`--rm`.

Workspaces are preserved by default. `--rm` disposes of one after a run, but
only when the run actually succeeded: a failed or timed-out run always keeps its
workspace, so a crashed agent never costs you the work it did first.

Everything is also available as a library, so another program can drive the
substrate without the CLI:

```python
from agent_sandbox import (SandboxSpec, LocalDockerBackend, ResourceConfig, RunRecord,
                           agent_home, config, mounts, worktree)

cfg  = config.load_config()
ws   = worktree.create("/path/to/repo")
rec  = RunRecord(ws.sandbox_id)
home = agent_home.ensure(ws.sandbox_id, cfg)          # seeds once, then reuses
plan = mounts.plan_for(ws, cfg, home)                  # agent home, skills, git
spec = SandboxSpec(ws.sandbox_id, ws, ["pytest"],
                   resources=ResourceConfig("4", "8g", 1024, "2h"), record=rec,
                   mounts=plan.mounts, env=plan.env)
result = LocalDockerBackend().run(spec)
```

## Defaults

| Setting | Default | Override |
|---|---|---|
| CPUs | 8 | `--cpus`, `AGENT_SANDBOX_CPUS` |
| Memory | 16g | `--memory`, `AGENT_SANDBOX_MEMORY` |
| PIDs | 2048 | `--pids-limit`, `AGENT_SANDBOX_PIDS` |
| Timeout | 12h | `--timeout`, `AGENT_SANDBOX_TIMEOUT` |
| Network | full | `--network`, `AGENT_SANDBOX_NETWORK` |
| Mode | safe | `--mode`, `AGENT_SANDBOX_MODE` |

Precedence is flag, then environment variable, then `~/agent-sandbox/config.json`,
then the built-in default. Timeout is a hard wall-clock ceiling: on expiry the
container is stopped (SIGTERM, 30 seconds of grace so an agent can close its
traces) and then killed, the run is marked `timed_out` rather than a generic
failure, and the workspace and logs are preserved.

`skill_mounts` is list-valued and has no `config set` path; edit
`~/agent-sandbox/config.json` by hand. `agent_home_template` is a plain string:

```json
{
  "mode": "safe",
  "skill_mounts": ["~/.claude/skills/sssf"],
  "agent_home_template": "/home/you/agent-sandbox/templates/agent-home"
}
```

`agent_home_template` defaults to the shipped template and only needs setting
if you keep your own (it must contain `claude/settings.json`). `config show`
prints both.

## What the container can and cannot reach

Mounted in:

- your sandbox workspace, at `/workspace`, read-write — the only host path the
  agent can modify
- shared package caches from `~/agent-sandbox/cache/{npm,pnpm,pip}` — separate
  from your real host caches, so a sandbox can never corrupt them
- credentials, narrowly, as described below
- the sandbox's own agent home at `/root/.claude` and `/root/.pi/agent`,
  read-write, from `~/agent-sandbox/runs/<id>/agent-home/`
- configured skill directories, read-only, under `/root/.claude/skills/`
- for a worktree sandbox, the host repository's `.git` directory with
  read-only overlays, described below

Every one of these is declared with `--mount`, so a missing host path is an
error before the container starts, never a silently created empty directory.
All of them are listed in `run.json` under `mounts`, and printed before each
run.

Not present: the Docker socket, your home directory, `/`, your SSH keys, and any
host credential you did not explicitly ask for.

Hardening applied to every run, in every mode:

- rootless Docker only — a rootful daemon is refused outright, because a
  container escape there would be host root
- `no-new-privileges`
- `--cap-drop ALL`, then six capabilities added back (see below)
- resource limits always applied
- ephemeral container, removed on exit

### The persistent agent home

A sandbox id outlives its containers, and since 1.1 so does the agent's own
state. Each sandbox owns `~/agent-sandbox/runs/<id>/agent-home/`, created 0700
and mounted read-write:

- `claude/` at `/root/.claude`: `settings.json` from the shipped template,
  a per-sandbox `.claude.json`, the operator's sessions and transcripts under
  `projects/`, and `skills/` where skill mounts land.
- `pi-agent/` at `/root/.pi/agent`: Pi's agent directory with pi-claude-bridge
  installed, `claude-bridge.json` pointing at the image's `claude` on the Max
  plan, and worker sessions.
- `seed.json`: which image and tool versions the home was seeded from.

The home is seeded once, on the sandbox's first run, and `enter` never
re-seeds it. `claude --resume` after `agent-sandbox enter <id>` lists and
continues the session you left. The home lives until `agent-sandbox rm <id>`.

When the image is rebuilt, existing homes keep their older tools. `run`,
`enter`, and `doctor` print a drift warning naming the sandbox; re-seed with
`agent-sandbox rm <id>` and a fresh run, or keep using it knowingly.

The seeded `settings.json` puts Claude Code in `bypassPermissions` inside the
sandbox (the container is the permission boundary) and denies the commands the
sandbox cannot undo: `git push`, `gh pr|issue|release|repo`,
`git worktree prune|repair|remove`, `git update-ref`, `git reflog`, `git gc`,
`git branch -D`. Deny rules apply even in bypass mode. They bind only the Claude
operator; anything else running in the container is bound by the mounts alone.

`IS_SANDBOX=1` is set in every container. It is the gate Claude Code reads
before allowing bypass as root, and it is how a skill or script can tell it is
running inside a sandbox rather than on your host.

### Skill mounts

`skill_mounts` in `config.json` lists host directories to expose read-only at
`/root/.claude/skills/<basename>` in every sandbox. Symlinks are resolved on
the host first (a `~/.claude/skills/x` entry is usually a link into a checkout
elsewhere, and the container cannot follow a host symlink). A missing or
non-directory entry fails the run by name before any worktree or branch is
created. An empty list mounts nothing.

### Git inside a worktree sandbox

A linked worktree's `.git` is a file pointing at `<repo>/.git/worktrees/<id>`,
so in 1.0 every git command inside a worktree sandbox failed. Since 1.1 a
worktree sandbox additionally mounts, at their host paths:

| Path | Mode |
|---|---|
| `<repo>/.git` (the common directory) | read-write |
| the worktree itself, a second time | read-write |
| `.git/worktrees/<id>` (this sandbox's admin dir) | read-write |
| `.git/config`, `.git/HEAD`, `.git/index` | read-only overlay |
| `.git/hooks`, `.git/modules`, `.git/worktrees` | read-only overlay |
| `.git/worktrees/<id>/config.worktree` | read-only overlay |

The overlays cover every path a write could turn into code that runs on your
host the next time you run git: hooks, `core.hooksPath`, `core.fsmonitor`,
submodule git dirs, other worktrees' config. An overlay whose host source does
not exist is served from an empty stand-in under `runs/<id>/git-overlays/`, so
none is ever skipped. Your main checkout is not mounted.

What works inside: `git status`, `add`, `commit`, `log`, `diff`, `branch`,
`stash`, `rebase` on the sandbox branch, and `git worktree list|prune`, which
sees this worktree at its real path and prunes nothing. What does not, by
design: `git config` writes, `git remote add`, `git push -u` (it needs to write
the upstream into `.git/config`), submodule operations, and `git worktree add`.
`git branch --set-upstream-to` writes nothing and does not say so. Commit
identity comes from the environment instead: `GIT_AUTHOR_*` and
`GIT_COMMITTER_*` are resolved per run from the repository's own config, local
identity first, then global. A worktree sandbox on a repository with no
resolvable identity refuses to start.

Only a repository whose common directory is `<repo>/.git` qualifies. A
submodule or a repository that is itself a linked worktree runs the 1.0 way,
with no git mounts and a warning, and a Docker outage fails before any worktree
or branch exists.

**Accepted exposure.** The common directory is read-write, so a process in the
container can still irreversibly destroy host repository state: delete or
rewrite refs, expire reflogs, prune objects, and overwrite the index of every
other worktree of that repository. The operator deny list covers the obvious
verbs, but it binds only the Claude operator; workers and scripts are bound by
the overlays alone. This is accepted, not closed. Run untrusted agents against
a clone.

`agent-sandbox rm` and `clean` touch the host repository only through
`git worktree remove`, `git worktree prune`, and deleting the sandbox branch; a
guard refuses to delete anything inside the repository's `.git`. `--rm` after a
run keeps the workspace when its branch carries commits present on no other
branch, and prints why.

### Authentication exposed to containers

**Claude, by default (layers 1 and 2 of the ladder):**

- `~/.claude/.credentials.json`, read-only. This is your OAuth token. An agent
  in the sandbox can make Claude API calls as you.
- a per-sandbox `.claude.json` inside the agent home, read-write, seeded once
  with onboarding flags and `/workspace` pre-trusted and then owned by Claude
  Code for that sandbox. Without an agent home (library use that skips
  `agent_home.ensure`) it is the 1.0 read-only generated file instead.

Your real `~/.claude.json` is **not** mounted. That file is ~63 KB and carries
project history, MCP server configuration, and account and machine identifiers.
Verified: Claude Code authenticates inside the sandbox with the minimal mount
alone, so the broader file is never needed. `--with-full-claude-state` exists to
mount it read-only, but nothing escalates to it automatically.

If `ANTHROPIC_API_KEY` is set and no credential file exists, the key is injected
as an environment variable instead and nothing is mounted.

The credentials mount is read-only, so a token refresh inside the container
cannot be written back. `doctor` warns when the host access token expires
within an hour; run `claude` on the host once before a long run. How Claude
Code behaves inside a sandbox on a session longer than the token lifetime is
unverified in 1.1. If it fails, the fallback is `claude setup-token` on the
host and `CLAUDE_CODE_OAUTH_TOKEN` in the container's environment.

**GitHub: nothing, unless you ask.** `--with-github-auth` injects `GH_TOKEN` and
`GITHUB_TOKEN` from your host `gh` login for that one run, letting the agent push
and open PRs as you. Without the flag no GitHub credential exists in the
container. SSH keys are never mounted in any mode.

Nothing is ever baked into the image, and nothing is copied into a repository.

## Modes

`fast` and `safe` are capability labels, stable in the CLI regardless of what
backs them. **Today they are identical**: rootless Docker with runc and the full
hardening baseline.

That is an honest result, not an oversight. gVisor was measured on this machine
and cannot enforce cgroup limits here — it only runs rootless with
`-ignore-cgroups`, which forfeits `--memory`, `--cpus`, and `--pids-limit`.
Since enforced limits are part of the baseline, gVisor is a trade rather than an
upgrade, so it cannot back the default. If that changes upstream, `safe` can
absorb it without any change to how you invoke the tool.

`--experimental-gvisor` runs under gVisor anyway, for cases where syscall-level
containment matters more than resource ceilings. It warns loudly and records
`runtime: runsc` in the run metadata.

## Security limitations

Be clear-eyed about what this does and does not stop.

- **A sandboxed agent can act as you against the Claude API.** The OAuth token
  is mounted. It cannot read the rest of your home directory, but it can spend
  your quota and see anything in the workspace.
- **`--with-github-auth` grants real write access** to every repository your
  `gh` token can reach, for the duration of that run.
- **Network is unrestricted by default.** An agent can reach anything your
  machine can. `--network none` is the only real restriction today;
  `restricted` deliberately fails rather than pretending to filter.
- **Six capabilities are retained.** CHOWN, DAC_OVERRIDE, FOWNER, FSETID,
  SETGID, SETUID, because `apt-get` needs them and installing OS packages is
  normal agent work. `--strict-caps` drops them, which breaks apt but leaves
  npm, pip, and venv working.
- **The process runs as UID 0 inside the container.** Under rootless Docker
  that maps to your own host user, so it is not host root and cannot exceed your
  privileges. It is chosen so files written into the worktree come back owned by
  you rather than by an unusable subordinate uid. `docker ps` will still say
  root; that is expected here and means something different than it does under a
  rootful daemon.
- **Container isolation is not a VM.** A kernel exploit is still a kernel
  exploit. Rootless mode limits the blast radius to your user account, not the
  host. A microVM backend is deliberately out of scope for now.
- **`--direct` has no isolation at all.** It mutates your real checkout by
  design, guarded only by an advisory lock against concurrent `--direct` runs.
- **A worktree sandbox can destroy host repository history.** The git common
  directory is read-write; the overlays stop code execution on the host, not
  ref deletion, reflog expiry, object pruning, or index overwrites in other
  worktrees. See the accepted exposure above.
- **Claude Code runs with bypass inside the sandbox.** The seeded settings
  skip permission prompts because the container is the boundary. The deny
  list is a convenience for the operator, not a control on other processes.
- **Do not exit the operator shell while a long agent run is in progress.**
  Leaving the container ends every process in it. The timeout stops first and
  kills 30 seconds later, but a manual exit gives a running chain no such
  grace; wait for it, or run it detached inside the sandbox.

## Setup on a fresh machine

Rootless Docker, with no `docker` group and no rootful daemon:

```bash
sudo apt-get install -y ca-certificates curl iptables uidmap dbus-user-session \
                        slirp4netns fuse-overlayfs
# Docker's official repo, then:
sudo apt-get install -y docker-ce docker-ce-cli docker-ce-rootless-extras containerd.io
sudo systemctl disable --now docker.service docker.socket   # rootful daemon off
dockerd-rootless-setuptool.sh install
systemctl --user enable --now docker
sudo loginctl enable-linger "$USER"        # daemon survives logout
```

Then `agent-sandbox doctor`. It checks all of the above and tells you what to
run if something is missing.

Two WSL2-specific traps worth knowing, both already handled by this tool but
worth recognizing if you set this up elsewhere:

1. The generated `docker.service` bakes in your inherited `PATH`, which on WSL2
   contains Windows interop entries with spaces. systemd's `Environment=` parser
   splits those into garbage and the daemon fails to start. Replace that line
   with a clean PATH.
2. Do **not** set `native.cgroupdriver=cgroupfs` in `~/.config/docker/daemon.json`.
   Rootless Docker needs the default systemd driver; with cgroupfs the daemon
   accepts `--memory` and `--pids-limit` and then silently fails to enforce them.
   `gVisor`'s installer writes that setting, so re-check after installing runsc.
   `agent-sandbox doctor` probes actual enforcement from inside a container
   specifically to catch this.

## Layout

```
~/agent-sandbox/
  bin/agent-sandbox          entry point (symlinked into ~/.local/bin)
  agent_sandbox/             the package
    cli.py                   argparse wiring only, no policy
    backend.py               SandboxBackend ABC, SandboxSpec, SandboxResult
    docker_backend.py        LocalDockerBackend: hardening, limits, execution
    worktree.py              worktree/copy lifecycle, --direct lock
    mounts.py                typed bind mounts, skill mounts, git identity
    agent_home.py            persistent per-sandbox agent home, seeding, drift
    gitdir.py                git common-dir mount and read-only overlays
    credentials.py           the four-layer credential ladder
    resources.py             limits, parsing, defaults
    metadata.py              per-run records
    image.py                 fingerprint and auto-build
    doctor.py                health checks
    cleanup.py               rm / clean / targeted docker prune
  image/Dockerfile           the base image
  image/toolchain/           Claude Code and Pi, pinned by lockfile
  image/pi-agent-template/   pi-claude-bridge template seeded into each home
  templates/agent-home/      the seeded Claude settings.json
  worktrees/<id>/            sandbox workspaces (your work lives here)
  runs/<id>/run.json         metadata, stdout.log, stderr.log
  runs/<id>/agent-home/      /root/.claude and /root/.pi/agent, until rm
  cache/{npm,pnpm,pip}/      shared package caches
  config.json                persistent defaults
  SPEC.md PLAN.md REVIEW.md  the build contract, plan, and verification
```

## Extending it later

The seams that matter are already separate: sandbox lifecycle, worktree
lifecycle, execution, resource configuration, credentials, and metadata each
live in their own module, and `SandboxBackend` is an abstract base with exactly
one implementation.

To add a backend, implement `preflight`, `run`, and `list_containers` against
the same `SandboxSpec` and return a `SandboxResult`. Nothing in the CLI needs to
change. `ExeBackend` and `MicroVMBackend` are deliberately not built.

For an orchestrator on top: drive the library API rather than the CLI, use
`--json` or `RunRecord.public()` for structured results, and treat the sandbox
id as the durable handle — it outlives any single container. Declare extra
exposure as `Mount` objects on `SandboxSpec.mounts`; call `agent_home.ensure`
and then `mounts.plan_for(workspace, config, home)` so `run` and `enter` mount
the same set; and read the record's `agent_home`, `mounts`, and
`trusted_mounts` fields to see exactly what a sandbox could reach.
