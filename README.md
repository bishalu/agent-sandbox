# agent-sandbox

Let a coding agent loose on your repository without letting it loose on your machine.

`agent-sandbox .` gives the agent its own git worktree on its own branch, inside a hardened rootless-Docker container that is thrown away when it exits. The work survives on the branch. The container does not.

```bash
agent-sandbox .                      # this repo's sandbox: resumed if it exists, created if not
agent-sandbox . -- claude            # Claude Code inside it
agent-sandbox . -- npm test          # any one-shot command
agent-sandbox . --new                # a second sandbox for the same repo
```

No per-repository setup, and no assumptions about language or test runner.

## What you get

**Your checkout is never touched.** The agent works in `~/agent-sandbox/worktrees/<id>/` on branch `agent-sandbox/<id>`. Merge what you like. Delete the rest.

**The sandbox remembers.** Each one keeps its own Claude home, so `agent-sandbox .` followed by `claude --resume` picks up the conversation you left. It resumes the same sandbox every time until you ask for `--new`.

**Secrets arrive on their own.** A worktree starts with tracked files only. List your gitignored `.env` under `worktree_seed` in the config and every new sandbox gets a copy, permissions intact. Only gitignored paths are copied, so nothing can end up in a commit.

**Git works inside.** Commit, branch, rebase, stash, all on the sandbox branch. Push is denied for the Claude operator, and the parts of `.git` that could run code on your host the next time you type `git` are mounted read-only.

**Skills ride along.** Host skill directories mount read-only at `/root/.claude/skills/`, so a factory like [SSSF](https://github.com/bishalu/super-simple-software-factory) runs inside without being copied into the repo.

## What the agent can reach

Mounted in: the worktree, read-write. Shared package caches, separate from your real ones. Your Claude login, read-only. The sandbox's own agent home. Configured skills, read-only. The repository's `.git`, with read-only overlays on hooks and config.

Not mounted: your home directory, `/`, SSH keys, the Docker socket, and any credential you did not ask for. `--with-github-auth` injects your `gh` token for one run. Nothing else ever grants GitHub access.

Every mount is declared, printed before the run, and recorded in `runs/<id>/run.json`.

Hardening on every run: rootless Docker only, `no-new-privileges`, all capabilities dropped and six added back for `apt`, CPU, memory and PID limits enforced, container removed on exit.

## What it does not stop

- The agent holds your Claude OAuth token. It can spend your quota and read the workspace.
- Network is open by default. `--network none` is the only restriction on offer.
- The repository's git common directory is read-write, so an agent can delete refs or prune objects. The overlays stop code execution on your host. They do nothing about history loss. Run untrusted agents against a clone.
- Inside the container the process is root. Under rootless Docker that maps to your own host user, so it cannot exceed your privileges.
- A container is not a VM. A kernel exploit is still a kernel exploit.
- `--direct` runs on your live checkout with no isolation at all, guarded by a lock and nothing else.
- Leaving the operator shell ends every process in the container. Wait for a long run, or run it detached.

## Commands

| | |
|---|---|
| `agent-sandbox .` | resume this repo's sandbox, or create one |
| `agent-sandbox . --new` | a fresh sandbox even if one exists |
| `agent-sandbox . -- <cmd>` | run one command and exit |
| `agent-sandbox list` | every sandbox: status, age, workspace |
| `agent-sandbox enter <id>` | a specific sandbox by id |
| `agent-sandbox rm <id>` | remove one; refuses to discard unmerged work |
| `agent-sandbox clean` | sweep sandboxes older than 14 days; `--docker` prunes images too |
| `agent-sandbox doctor` | 28 checks, including git and Claude inside a probe container |
| `agent-sandbox config show` | current defaults |

Defaults: 8 CPUs, 16g, 2048 PIDs, 12h timeout, full network. Override per run with a flag, or persistently in `~/agent-sandbox/config.json`. On timeout the container gets SIGTERM and 30 seconds to close its traces, then SIGKILL. The workspace is kept.

## Setup

Rootless Docker, no `docker` group, rootful daemon off:

```bash
sudo apt-get install -y ca-certificates curl iptables uidmap dbus-user-session slirp4netns fuse-overlayfs
# add Docker's apt repo, then:
sudo apt-get install -y docker-ce docker-ce-cli docker-ce-rootless-extras containerd.io
sudo systemctl disable --now docker.service docker.socket
dockerd-rootless-setuptool.sh install
systemctl --user enable --now docker
sudo loginctl enable-linger "$USER"
```

Then `agent-sandbox doctor`. It says what is missing and what to run.

Two WSL2 traps, both caught by `doctor`: the generated `docker.service` inherits a Windows `PATH` with spaces that systemd cannot parse, and `native.cgroupdriver=cgroupfs` makes Docker accept memory limits it then silently ignores. gVisor's installer writes the second one.

## The rest

`SPEC.md` is the requirement list, R-01 to R-23. `REVIEW.md` records evidence for every one, plus the deviations and why. `PLAN.md` is the build order. The package is a library too: `SandboxBackend` has one implementation, and the seams for another are already separate.

`fast` and `safe` modes are identical today. gVisor cannot enforce cgroup limits on this machine, so it is opt-in through `--experimental-gvisor` rather than the default.
