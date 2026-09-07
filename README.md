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
| `agent-sandbox doctor` | verify the whole stack end to end |
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
from agent_sandbox import SandboxSpec, LocalDockerBackend, ResourceConfig, RunRecord, worktree

ws   = worktree.create("/path/to/repo")
rec  = RunRecord(ws.sandbox_id)
spec = SandboxSpec(ws.sandbox_id, ws, ["pytest"],
                   resources=ResourceConfig("4", "8g", 1024, "2h"), record=rec)
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
container is killed, the run is marked `timed_out` rather than a generic
failure, and the workspace and logs are preserved.

## What the container can and cannot reach

Mounted in:

- your sandbox workspace, at `/workspace`, read-write — the only host path the
  agent can modify
- shared package caches from `~/agent-sandbox/cache/{npm,pnpm,pip}` — separate
  from your real host caches, so a sandbox can never corrupt them
- credentials, narrowly, as described below

Not present: the Docker socket, your home directory, `/`, your SSH keys, and any
host credential you did not explicitly ask for.

Hardening applied to every run, in every mode:

- rootless Docker only — a rootful daemon is refused outright, because a
  container escape there would be host root
- `no-new-privileges`
- `--cap-drop ALL`, then six capabilities added back (see below)
- resource limits always applied
- ephemeral container, removed on exit

### Authentication exposed to containers

**Claude, by default (layers 1 and 2 of the ladder):**

- `~/.claude/.credentials.json`, read-only. This is your OAuth token. An agent
  in the sandbox can make Claude API calls as you.
- a generated ~90-byte config at `~/agent-sandbox/runs/<id>/claude.json`,
  read-only, containing only onboarding flags.

Your real `~/.claude.json` is **not** mounted. That file is ~63 KB and carries
project history, MCP server configuration, and account and machine identifiers.
Verified: Claude Code authenticates inside the sandbox with the minimal mount
alone, so the broader file is never needed. `--with-full-claude-state` exists to
mount it read-only, but nothing escalates to it automatically.

If `ANTHROPIC_API_KEY` is set and no credential file exists, the key is injected
as an environment variable instead and nothing is mounted.

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
    credentials.py           the four-layer credential ladder
    resources.py             limits, parsing, defaults
    metadata.py              per-run records
    image.py                 fingerprint and auto-build
    doctor.py                health checks
    cleanup.py               rm / clean / targeted docker prune
  image/Dockerfile           the base image
  worktrees/<id>/            sandbox workspaces (your work lives here)
  runs/<id>/run.json         metadata, stdout.log, stderr.log
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
id as the durable handle — it outlives any single container.
