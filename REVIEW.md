# Review — implementation vs SPEC.md

Reviewed 2026-09-07 against SPEC.md R-01..R-15 and PLAN.md M1..M7.
Every row cites evidence observed on this machine, not intent.

## Verdicts

| Req | Verdict | Evidence |
|---|---|---|
| R-01 modes | PASS | `--mode fast` and `--mode safe` both accepted, both report `runtime: runc`; names are stable labels decoupled from implementation. `--experimental-gvisor` runs under a real gVisor kernel (`Linux version 4.19.0-gvisor`) and prints the loud limits-forfeited warning. |
| R-02 hardening | PASS | Inside a sandbox: `NoNewPrivs: 1`, `CapEff: 00000000000000db` (exactly the six add-back caps). Negative checks: docker.sock absent, `/home/bishal` absent, `/root/.ssh` absent, `GH_TOKEN` unset unless requested. Rootful daemon refused by `preflight()`; `doctor` confirms rootless. Container runs `--rm`; `docker ps -a` empty after runs. |
| R-03 resources | PASS | Defaults observed in-cgroup: `mem=17179869184` (16 GiB), `cpu=800000 100000` (8 CPUs), `pids=2048`. Flags: `--memory 512m --cpus 2 --pids-limit 64` → `536870912 / 200000 100000 / 64`. Env: `AGENT_SANDBOX_MEMORY=256m AGENT_SANDBOX_PIDS=32` → `268435456 / 32`. Enforcement: `--memory 64m` → exit 137; `--pids-limit 16` → `bash: fork: Resource temporarily unavailable`. Timeout: `--timeout 5s` on `sleep 60` → killed in 5.5s, `status: timed_out`, exit 137, worktree preserved. |
| R-04 worktrees | PASS | Central `~/agent-sandbox/worktrees/<id>`, branch `agent-sandbox/<repo-slug>-<8hex>` created in the source repo. Preserved after exit. Non-git dir → `workspace_kind: copy`, source untouched. `--direct` second concurrent run refused with actionable text; lock released on exit; stale locks reclaimed via `os.kill(pid, 0)`. |
| R-05 filesystem | PASS | Only the workspace is bind-mounted rw. `--read-only-root`: workspace/tmp/HOME writable, `/usr/bin` read-only; `python3 -m venv` still succeeds. Caches bind from `~/agent-sandbox/cache/*`, never the host's real caches. |
| R-06 network | PASS | `--network none` → BLOCKED; `--network full` → REACHED; `--network restricted` → fails fast with "not implemented yet" and never falls back to full. |
| R-07 credentials | PASS | Layer 1+2 used by default: only `.credentials.json` (ro) plus a generated 90-byte config containing `hasCompletedOnboarding/autoUpdates/installMethod`. Host `~/.claude.json` (63 KB, project + MCP + identifiers) NOT mounted. `claude -p` inside the sandbox returned `SANDBOX_AUTH_OK`, proving the minimal mount is sufficient — layer 4 never needed. GitHub off by default; `--with-github-auth` injects `GH_TOKEN`. |
| R-08 CLI | PASS (after fix) | All subcommands exercised: run (bare path shorthand), enter, list, rm, clean, doctor, build, config. `--` splits the agent command correctly. **First review pass wrongly marked this PASS while `--keep`/`--rm` were missing entirely — see Gap 7.** |
| R-09 observability | PASS | `runs/<id>/run.json` carries every required field; `enter` appended a second entry (`containers recorded: 2`) under the same sandbox id. stdout/stderr written to `runs/<id>/*.log`. `--json` parses and carries the full schema. |
| R-10 doctor | PASS | 17 checks, all PASS, including real container execution **and** a new enforcement probe (see Gap 1). Every failure path carries a `→` remediation line. |
| R-11 image | PASS | All required tools present: git curl wget jq rg fd python3 pip3 node npm pnpm gh claude tar unzip gcc make. Node v22.23.2, pnpm 12.3.4, Claude Code 2.1.263. Fingerprint state recorded; unchanged Dockerfile → no-op rebuild. |
| R-12 structure | PASS | Library use verified without the CLI: `SandboxSpec` + `LocalDockerBackend().run()` returned `completed 0` and wrote its log. Concerns are in separate modules; `SandboxBackend` ABC has exactly one implementation. |
| R-13 performance | PASS | Central WSL-native worktrees, shared caches, no copies for git repos. Three concurrent sandboxes against one repo all completed with distinct branches and no locking. `/mnt/c` path produced the perf warning and still ran. |
| R-14 disk hygiene | PASS | `doctor` reports docker disk usage; `clean --docker` targets only `agent-sandbox.managed=true` containers, dangling images, and build cache. Dry-run messaging fixed (see Gap 3). |
| R-15 deliverables | PASS | All 12 verification steps executed against a throwaway repo in the scratchpad. No existing repo was modified. |

## Gaps found and fixed during review

**Gap 1 — resource limits silently not enforced (critical).**
First run of the limit tests showed `cpu.max=max 100000`, `memory.max=max`, and 60
processes spawning under a 16-PID cap, while `docker inspect` still reported
`Memory=67108864 PidsLimit=16`. Cause: during the earlier gVisor investigation
`runsc install -cgroupdriver=cgroupfs` had written
`"exec-opts": ["native.cgroupdriver=cgroupfs"]` into `~/.config/docker/daemon.json`.
Rootless Docker needs the default systemd driver; with cgroupfs it accepts limits
and never applies them. Removed the override, restarted the daemon, re-verified
enforcement three ways. Added a permanent `doctor` check that reads the cgroup
from **inside** a container rather than trusting `docker inspect`, so this class
of silent failure cannot recur undetected.

**Gap 2 — `--json` output corrupted by container stdout.**
Container output was teed to our stdout and interleaved with the JSON document,
making it unparseable. Added `SandboxSpec.stream_output`; in `--json` mode
container output goes to the run logs only.

**Gap 3 — silent `clean` dry runs.**
`clean --dry-run` printed nothing when there was nothing to remove. Now reports
counts of newer and protected sandboxes, and states explicitly that images and
build cache were left untouched.

**Gap 4 — entry point broke through the PATH symlink.**
`bin/agent-sandbox` used `os.path.abspath(__file__)`, which resolves to the
symlink in `~/.local/bin` rather than the real package directory. Switched to
`os.path.realpath`.

**Gap 5 — `--direct` run records could never be removed.**
`rm` raised unconditionally for `workspace_kind: direct`, so those records
accumulated in `list` forever with no way to clear them. Now `rm <id> --force`
deletes the run record and logs only, and the un-forced path explains that
there is no sandbox workspace to delete and never touches the real checkout.

**Gap 6 — branch and worktree registration cleanup (verified, no defect).**
Checked explicitly because a leaked branch or a stale `git worktree` entry would
pollute the user's real repo. On a fresh repo: run created
`agent-sandbox/branchtest-84ee4e98` and a second worktree registration; after
`rm --force` both were gone and `git worktree list` returned to one entry. No
fix needed.

## Second review pass — auditing the first review

The first pass was written from my own test notes, which is how a review turns
into self-congratulation. The second pass re-read SPEC.md against the *code* and
re-tested every claim that had been asserted rather than observed. It found one
real miss and five unverified assertions.

**Gap 7 — `--keep`/`--rm` were never implemented, and R-08 was marked PASS anyway.**
Diffing the spec's R-08 flag list against `add_argument` calls in `cli.py` showed
`--keep` and `--rm` present in the spec and absent from the code. This is a
review failure as much as an implementation one. Now implemented as a mutually
exclusive pair, with the safety rule the spec demands: `--rm` disposes of the
workspace only on a `completed` run. Verified: success → removed; `exit 3` →
kept with "`--rm` ignored: run failed"; timeout → kept; `--keep` and bare
default both preserve; passing both flags is rejected by argparse.

**Claims that were asserted in pass 1 and only actually tested in pass 2** (all
held up, but none of them had evidence when first written):

| Claim | Pass-2 result |
|---|---|
| stale `--direct` lock is reclaimed | Planted a lock owned by dead pid 999999; next run reclaimed it and executed. |
| `AGENT_SANDBOX_CPUS` / `_TIMEOUT` env overrides | `CPUS=3` → `cpu.max 300000 100000`; `TIMEOUT=4s` → `status: timed_out`. Pass 1 had only tested MEMORY and PIDS. |
| Dockerfile change forces a rebuild | Appending a line flipped `needs_build` to True; restoring the exact bytes flipped it back to False. |
| `clean --docker` spares unrelated objects | Created an unrelated stopped container, an unrelated created container, and a tagged `my-unrelated-image:keepme`; ran `clean --docker` for real (not `--dry-run`). All three survived, as did `agent-sandbox:base`. Pass 1 had only dry-run evidence. |
| image auto-builds transparently on first use | Deleted the image and cleared the fingerprint state, then ran a sandbox: it printed "image not present", built, and ran the command. Pass 1 never exercised this path because the image always existed. |
| R-09 record completeness | Enumerated all 15 spec-required fields against a real `run.json`: none missing, log files exist on disk. |

## Deliberate deviations from the spec, with justification

**D-1 — capabilities: `--cap-drop ALL` plus six add-backs, not a bare drop-all.**
R-02 says drop all "preferably ... unless something actually requires one".
Measured: with a bare `--cap-drop ALL`, `apt-get update` fails (apt drops
privileges to the `_apt` user and needs SETUID/SETGID). With CHOWN,
DAC_OVERRIDE, FOWNER, FSETID, SETGID, SETUID added back, apt install succeeds.
`npm init` and `python3 -m venv` work under either setting. Since Q17 explicitly
required that OS package installs keep working for arbitrary repos, the add-back
set is the default and `--strict-caps` exposes the bare drop-all for workloads
that do not need apt. Still far below Docker's default: no NET_RAW, NET_ADMIN,
SYS_ADMIN, SYS_PTRACE, MKNOD, or SYS_CHROOT.

**D-2 — `fast` and `safe` are currently identical.**
Per R-01 and the interview: gVisor cannot enforce cgroup limits on this machine,
and read-only-root was ruled out as the differentiator, so no profile is
stronger while still meeting the baseline. Both labels exist and are stable so
the distinction can become real without a CLI change.

**D-3 — container UID 0.**
Documented exception to "non-root where practical", chosen so files written into
the bind-mounted worktree come back owned by the host user. Verified:
`stat` on a sandbox-created file returns `bishal:bishal`. Under rootless Docker
this is not host root.

## Not implemented (deferred by the spec, correctly absent)

`--network restricted` implementation, paranoid/microVM backend, per-repo config
files, milestone orchestration, model routing, judges, evals, factory logic,
dashboards.

## Result

All 15 requirements pass **after two review passes**.

Pass 1 (against my own test notes) found six gaps, five fixed and one verified
clean. Pass 2 (against the spec text and the code, re-testing asserted claims)
found that pass 1 had marked R-08 PASS while two of its flags did not exist, and
that six further claims had been asserted without evidence. All are now
implemented or independently verified.

Three deviations from the spec are documented with measured justification. No
requirement is silently unmet.

The lesson worth keeping: a review written from the builder's own notes will
confirm the builder's own beliefs. Pass 2 only found the `--keep`/`--rm` miss by
mechanically diffing the spec's flag list against `add_argument` calls, not by
re-reading prose.
