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

---

# Review of v1.1 against SPEC.md R-16..R-22

Reviewed 2026-09-07 against SPEC.md R-16 to R-22 (and the R-07 and R-11
changes) and PLAN.md M8. Every row cites evidence observed on this machine.
Doctor and container output quoted here passed through the R-22 redactor.

## Verdicts

| Req | Verdict | Evidence |
|---|---|---|
| R-07 layer 2 (1.1) | PASS | Inside a sandbox `/root/.claude` was mode 700, `.credentials.json` and `.claude.json` 600. `claude -p --session-id <uuid>` under bypass returned `HOME_OK`. Host `~/.claude.json` still not mounted. See D-4 for the wording change. |
| R-11 image (1.1) | PASS | Image build 91 s full, 6 s cached; the Dockerfile's own version assertions passed. Fingerprint changed when a file under `image/` changed and ignored `node_modules`. `doctor`: "pi-claude-bridge models: image template: 8, seeded home: 8". |
| R-16 typed mounts | PASS | A missing `skill_mounts` entry failed before any worktree was created (worktrees before=2 after=2), naming the entry. A linked-worktree target ran with `trusted_mounts []` in `run.json` and a warning. Mounts are printed before each run and listed in `run.json` under `mounts`. |
| R-17 environment | PASS | `env` inside the container, filtered to the named keys, showed `IS_SANDBOX=1`, `CLAUDE_CONFIG_DIR=/root/.claude`, `GIT_AUTHOR_EMAIL=local@example.test` (the throwaway repo's local identity, not the global one). `doctor`: "git identity: resolves (doctor@agent-sandbox.local for a repo with a local identity)". `doctor`: "root bypass gate (IS_SANDBOX): Claude Code accepts bypass as root inside the sandbox (no quota spent)". |
| R-18 agent home | PASS | `/root/.claude` 700; `.credentials.json` and `.claude.json` 600. Seeded Pi home listed 8 `claude-bridge` models. `claude -p --session-id <uuid>` returned `HOME_OK`; after `agent-sandbox enter`, `claude -p --resume <uuid>` returned the remembered word `PELICAN` with the same session id, and `settings.json` mtime was unchanged (no re-seed). `doctor`: "agent home persists across runs: file written in run 1 present in run 2", "agent home template" PASS. |
| R-19 skill mounts | PASS | A symlinked skill dir appeared read-only at `/root/.claude/skills/grill-me` and `touch` there failed. A missing entry failed before any worktree was created (worktrees before=2 after=2). `doctor`: "skill mounts" PASS. |
| R-20 git in worktree sandboxes | PASS (after fix) | `git commit` inside succeeded (`11659fd`) and `git log agent-sandbox/<id>` on the host showed it. `git worktree prune` inside left 2 entries. Six overlay writes failed: `touch: cannot touch '.../.git/hooks/pre-commit': Read-only file system`; `error: could not write config file .../.git/config: Device or resource busy`; HEAD write `Read-only file system`; `mkdir: cannot create directory '.../.git/modules/x': Read-only file system`; `worktrees/marker` `Read-only file system`; `config.worktree` `Read-only file system`. With `DOCKER_HOST=unix:///nonexistent.sock` the run failed at preflight with branches before=1 after=1. A linked-worktree target ran with `trusted_mounts []` and a warning. `--rm` on a run that committed printed "--rm ignored: <id> has 1 commit(s) not present on any other branch — refusing to delete" and kept the workspace; `--rm` with no new commits printed "workspace removed (--rm)". The guard had never fired in 1.0; see Gap 8. `doctor`: "worktree sandbox commit: commit inside the sandbox is visible on the host branch", "git overlays read-only: hooks/ refused a write", "worktree prune is a no-op". |
| R-21 timeout | PASS | A 15 s timeout on a container trapping SIGTERM printed `TRAPPED-SIGTERM` and ended with status `timed_out`, exit 0: the stop reached the handler before the kill. |
| R-22 doctor | PASS | `agent-sandbox doctor` on 2026-09-07: 28 passed, 0 warnings, 0 failed, 13.9 s. New checks all PASS: "git identity: resolves (doctor@agent-sandbox.local for a repo with a local identity)", "skill mounts", "agent home template", "Claude access token expiry: 333 min left", "worktree sandbox commit: commit inside the sandbox is visible on the host branch", "git overlays read-only: hooks/ refused a write", "worktree prune is a no-op", "pi-claude-bridge models: image template: 8, seeded home: 8", "root bypass gate (IS_SANDBOX): Claude Code accepts bypass as root inside the sandbox (no quota spent)", "agent home persists across runs: file written in run 1 present in run 2". Probe output is redacted for `TOKEN`, `KEY`, `SECRET` values before it is written. |

## Gaps found and fixed during review

**Gap 8: the 1.0 `unpushed_commits` guard never fired.**
Its `git log --not --exclude=refs/heads/agent-sandbox/* --branches` pattern
carried the `refs/heads/` prefix, but `--exclude` patterns for `--branches` are
matched against the short name, so the sandbox branch was never excluded and
the count was always 0. `rm` and `--rm` would have deleted unpushed work
silently. Fixed in `worktree.py` (`--exclude=agent-sandbox/*`); verified by the
`--rm ignored: ... 1 commit(s) not present on any other branch` observation
above.

**Gap 9: a bad `skill_mounts` entry surfaced after the worktree existed.**
The first version validated skill mounts inside `plan_for`, which ran after
`worktree.create`, so a typo in `config.json` left an orphan worktree and
branch behind. `cmd_run` now calls `mounts.skill_mounts(cfg)` right after
`preflight()`, before anything is created (worktrees before=2 after=2).

**Gap 10: an empty `models.json` triggered a Pi schema warning.**
The template seeded `models.json` as `{}`; Pi warned about the missing
`providers` key on every start. The seed is now `{"providers": {}}` in
`image/pi-agent-template/` and in `agent_home.ensure`.

## Deliberate deviations from the spec, with justification

**D-4: the per-sandbox `.claude.json` is writable.**
1.0 mounted a read-only generated config as R-07 layer 2. With an agent home
that file lives inside `runs/<id>/agent-home/claude/` read-write, because
trust and bypass acknowledgements and the project list must persist per
sandbox for `claude --resume` to work. It still contains only the generated
keys (the same allow-list as before, plus `/workspace` pre-trusted) and
whatever Claude Code writes for that sandbox; the host `~/.claude.json` is
never exposed. R-07's layer-2 wording was rewritten to say so.

**D-5: the read-only `.git/config` overlay breaks some git verbs on purpose.**
`git config` writes, `git remote add`, and `git push -u` fail inside the
sandbox (`Device or resource busy`), and `git branch --set-upstream-to` writes
nothing silently. The overlay exists because `core.hooksPath` and
`core.fsmonitor` in that file are host code execution; commit identity is
supplied through the environment instead (R-17). None of the failing verbs is
needed by the workloads the sandbox targets, and pushing is denied anyway.

**D-6: `.git/modules` is overlaid read-only wholesale.**
Rather than enumerating each submodule's `hooks/` directory, the whole
`modules` tree is read-only, so submodule operations fail inside the sandbox
instead of exposing submodule hooks. Refusing the git mount outright for
repositories with submodules is deferred.

## Result

All seven new requirements pass, plus the 1.1 changes to R-07 and R-11.
Three gaps were found and fixed, one of them (Gap 8) a latent 1.0 defect that
the new `--rm` path exposed. Three further deviations are documented with
their justification. No requirement is silently unmet.

### Repair cycle 1 (M1 independent review, 2026-09-07)

Findings F1 to F3 (major) and F4 to F14 (minor/notes) from the milestone review, fixed and re-verified:

| Finding | Fix | Observed |
|---|---|---|
| F1 partial agent home trusted forever | `AgentHome.exists` requires `seed.json`; a partial home is removed and re-seeded | A home with `claude/` and `pi-agent/` but no seed record reported `exists: False partial: True`; `ensure()` printed `re-seeding incomplete agent home`, and afterwards `exists: True`, seed present, bridge package present |
| F2 failure between `worktree.create` and the first record save orphaned an invisible sandbox | `run.json` is saved with repo, workspace, kind and branch immediately after `worktree.create` | With no resolvable git identity the run failed after the worktree existed; `agent-sandbox list` showed `r1-30bc7a93  created`, `rm --force` removed it, and the source repo had 1 worktree left |
| F3 main checkout's `config.worktree` writable | `config.worktree` added to the read-only overlay set (stand-in when absent) | Inside the container `echo x > <common>/config.worktree` failed with `Read-only file system` |
| F4 zero-byte credentials placeholder | placeholder is `{}` | `.credentials.json` in a fresh home reads `{}` |
| F5 redactor coverage | pattern accepts quoted keys, masks bare `sk-ant-` tokens; `cmd_doctor` redacts every check | `"accessToken": "sk-ant-oat01-..."`, `GH_TOKEN=xyz` and a bare `sk-ant-api03-...` all rendered `[redacted]` |
| F6 moved source repo silently lost git | warning appended when the recorded repo no longer resolves; identity resolution skips a missing repo | see re-test below |
| F7 pre-1.1 sandbox seeding undocumented | README and SPEC R-18 sentence added | n/a |
| F8 test residue | `runs/t-repo-e6bc8b92` and its worktree removed | `runs/` and `worktrees/` hold only `fingerprinting-28fa9117` |
| F9 deny list gaps | added `gh api|workflow|secret|gist`, `git -C * push`, `git branch --delete` | Under bypass, `claude -p` asked to run `git push origin main` reported the permission request was denied and nothing was pushed |
| F10 build context | `image/.dockerignore` with `node_modules/` | image rebuilt |
| F12 `--read-only-root` with the agent home | tested | a file written to `/root/.claude` under `--read-only-root` was present after `enter --read-only-root` |
| F13 `models.json` wording | sssf SPEC S3 and PLAN R5 updated to `{"providers": {}}` | n/a |
| F14 direct-kind removal guard | `guard_removal` runs before the direct-kind `rmtree` | n/a |

Additional scenarios from the reviewer's untested list, observed: two sandboxes of one repo have disjoint homes (a marker written in one was absent in the other); `rm --force` deleted the agent home with the run record; a copy-kind workspace received no `GIT_AUTHOR_*` variables; `--json` output carried `agent_home`, `mounts`, and `trusted_mounts`; a repo with no `.git/hooks` got read-only stand-ins for `hooks`, `modules`, and `config.worktree`; `git worktree prune` inside the container left a sibling host worktree registered (3 entries before and after); `doctor --quick` reported `[FAIL] skill mounts` for a missing entry and `[WARN] agent home drift` for a sandbox whose seed record named a stale fingerprint.
