#!/usr/bin/env bash
# chaos-check.sh: the milestone-run hardening plan's success criteria as
# numbered steps, each with one expected line. Exit non-zero on the first
# failure, naming the step.
#
#   THIS SCRIPT LAUNCHES CONTAINERS. It must not run while
#   `agent-sandbox doctor --quick` reports the host disk as FAIL: on WSL the
#   distro's ext4.vhdx lives on the Windows drive, and a write burst on a
#   full drive kills the VM. The script runs that check itself first and
#   aborts on a host disk FAIL (and on a memlog FAIL, because then the first
#   launch would be refused before the test could start).
#
# What it does, against a scratch git repo under a temp dir:
#   1. enable admission with a 1g budget and launch one sandbox running
#      `sleep 120` from a throwaway bash (the launching shell)
#   2. a second launch for the same project with --no-wait is refused with
#      exit 3 and a JSON payload that names budget and committed
#   3. kill the launching shell; the container survives
#   4. kill the launcher too (so run.json is left at running, the way a dead
#      supervisor leaves it), `docker rm -f` the container; `agent-sandbox
#      status --json` shows crashed for it
#   5. the driver's `resume` prints a finishing command for that sandbox
#      (skipped with a note when the driver has no resume verb yet)
#   6. restore config
#
# Admission is enabled for the test through the environment, which the
# config ladder (flag > env > config.json > default) puts above
# ~/agent-sandbox/config.json; that file is never written. The scratch
# floor is 1 GiB so a busy host does not turn step 1 into a wait.
#
# Driver-independent: the launches are plain `agent-sandbox run`, not
# run-milestones.sh, so a driver regression cannot mask an admission one.
set -euo pipefail

DRIVER="${DRIVER:-$HOME/.claude/skills/milestone-supervisor/run-milestones.sh}"
# The budget is what other containers already commit (MCP servers and the like)
# plus one gibibyte, so exactly one 768m sandbox fits and a second is refused
# whatever else the host runs.
committed_bytes="$(agent-sandbox admission show --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["decision"]["numbers"]["committed"]["bytes"])' 2>/dev/null || echo 0)"
BUDGET="$(( (committed_bytes + 1024*1024*1024) / (1024*1024) ))m"
MEMORY="768m"          # two of these exceed the free gibibyte; one fits
LABEL="agent-sandbox.managed=true"

step() { printf 'step %s: %s\n' "$1" "$2"; }
fail() { printf 'FAIL at step %s: %s\n' "$1" "$2" >&2; exit 1; }

# ---------------------------------------------------------------- 0. the gate
doctor_json="$(agent-sandbox doctor --quick --json || true)"
[[ -n "$doctor_json" ]] || fail 0 "agent-sandbox doctor --quick --json printed nothing"
gate="$(printf '%s' "$doctor_json" | python3 -c '
import json, sys
rows = {c["name"]: c for c in json.load(sys.stdin)["checks"]}
for name in ("host disk", "memlog"):
    c = rows.get(name)
    if c is None:
        print(f"ABORT {name}: no such doctor row"); break
    if c["status"] == "FAIL":
        print("ABORT " + name + ": " + c["detail"]); break
else:
    print("OK " + "; ".join(n + ": " + rows[n]["detail"] for n in ("host disk", "memlog")))
')"
case "$gate" in
  OK*) step 0 "doctor gate ${gate#OK }" ;;
  *) fail 0 "$gate" ;;
esac

# ---------------------------------------------------------------- setup
TMP="$(mktemp -d -t chaos-check.XXXXXX)"
REPO_NAME="chaos$(head -c 4 /dev/urandom | od -An -tx1 | tr -d ' \n')"
REPO="$TMP/$REPO_NAME"
SHELL_PID=""; LAUNCHER_PID=""; CONTAINER=""

cleanup() {
  set +e
  [[ -n "$LAUNCHER_PID" ]] && kill -9 "$LAUNCHER_PID" 2>/dev/null
  [[ -n "$SHELL_PID" ]] && kill -9 "$SHELL_PID" 2>/dev/null
  # Every container and sandbox this run created, by the scratch repo's name.
  docker ps -a --filter "label=$LABEL" --format '{{.Names}}' 2>/dev/null \
    | grep "^agent-sandbox-$REPO_NAME-" | xargs -r docker rm -f >/dev/null 2>&1
  agent-sandbox list --json 2>/dev/null | python3 -c '
import json, sys
repo = sys.argv[1]
for r in json.load(sys.stdin):
    if (r.get("repo") or "").rstrip("/") == repo or r["sandbox_id"].startswith(sys.argv[2] + "-"):
        print(r["sandbox_id"])' "$REPO" "$REPO_NAME" 2>/dev/null \
    | xargs -r -n1 agent-sandbox rm --force >/dev/null 2>&1
  unset AGENT_SANDBOX_ADMISSION_ENABLED AGENT_SANDBOX_ADMISSION_MEMORY_BUDGET \
        AGENT_SANDBOX_ADMISSION_MEM_FLOOR_GB
  rm -rf "$TMP"
}
trap cleanup EXIT

git init -q -b main "$REPO"
git -C "$REPO" config user.email "chaos@agent-sandbox.local"
git -C "$REPO" config user.name "chaos-check"
mkdir -p "$REPO/.milestones" "$REPO/docs/spec"
printf 'MILESTONES_FILE=docs/spec/11-milestones.md\nREPORT_DIR=docs/reports\nGATE=true\n' \
  > "$REPO/.milestones/config"
printf '# Milestones\n\n## Milestone 1\n\nchaos\n' > "$REPO/docs/spec/11-milestones.md"
printf 'chaos-check scratch repo\n' > "$REPO/README"
git -C "$REPO" add -A
git -C "$REPO" commit -qm "chaos-check scratch"

# ---------------------------------------------------------------- 1. admission on, one launch
export AGENT_SANDBOX_ADMISSION_ENABLED=true
export AGENT_SANDBOX_ADMISSION_MEMORY_BUDGET="$BUDGET"
export AGENT_SANDBOX_ADMISSION_MEM_FLOOR_GB=1

# A throwaway bash is the launching shell; its child is the launcher.
bash -c 'agent-sandbox run "$1" --new --memory "$2" --tag milestone=chaos --no-wait -- sleep 120;
         echo "launcher exited $?"' _ "$REPO" "$MEMORY" > "$TMP/run1.log" 2>&1 &
SHELL_PID=$!

for _ in $(seq 1 90); do
  CONTAINER="$(docker ps --filter "label=$LABEL" --format '{{.Names}}' \
               | grep "^agent-sandbox-$REPO_NAME-" | head -1 || true)"
  [[ -n "$CONTAINER" ]] && break
  kill -0 "$SHELL_PID" 2>/dev/null || break
  sleep 1
done
[[ -n "$CONTAINER" ]] || fail 1 "no container appeared; launcher output: $(tail -n 20 "$TMP/run1.log")"
LAUNCHER_PID="$(pgrep -P "$SHELL_PID" | head -1 || true)"
[[ -n "$LAUNCHER_PID" ]] || fail 1 "the launching shell $SHELL_PID has no child launcher"
SANDBOX_ID="$(printf '%s' "$CONTAINER" | sed -E 's/^agent-sandbox-(.*)-[0-9]+-[0-9]+$/\1/')"
step 1 "admission enabled (budget $BUDGET); container $CONTAINER up, launcher pid $LAUNCHER_PID under shell $SHELL_PID"

# ---------------------------------------------------------------- 2. the second launch is refused
set +e
second="$(agent-sandbox run "$REPO" --new --memory "$MEMORY" --tag milestone=chaos --no-wait --json -- true 2>"$TMP/run2.err")"
rc=$?
set -e
[[ $rc -eq 3 ]] || fail 2 "second launch exited $rc, expected 3; stdout: ${second:0:400}; stderr: $(tail -n 5 "$TMP/run2.err")"
numbers="$(printf '%s' "$second" | python3 -c '
import json, sys
d = json.load(sys.stdin)
n = d["numbers"]
reasons = "; ".join(d["reasons"])
print("budget=%s committed=%s reasons=%s" % (n["budget"]["bytes"], n["committed"]["bytes"], reasons))' 2>&1)" \
  || fail 2 "refusal payload lacks budget/committed numbers: ${second:0:400}"
step 2 "second launch refused, exit 3; $numbers"

# ---------------------------------------------------------------- 3. the launching shell dies
kill -9 "$SHELL_PID"
sleep 3
docker ps --filter "label=$LABEL" --format '{{.Names}}' | grep -qx "$CONTAINER" \
  || fail 3 "container $CONTAINER gone after killing shell $SHELL_PID"
kill -0 "$LAUNCHER_PID" 2>/dev/null || fail 3 "launcher $LAUNCHER_PID died with its shell"
SHELL_PID=""
step 3 "launching shell killed; container $CONTAINER and launcher $LAUNCHER_PID survive"

# ---------------------------------------------------------------- 4. a simulated crash
# The launcher dies without recording an exit (a dead supervisor's unit
# stopped), then the container is removed: run.json is left at running.
kill -9 "$LAUNCHER_PID"
LAUNCHER_PID=""
sleep 1
docker rm -f "$CONTAINER" >/dev/null
sleep 1
state="$(agent-sandbox status --json | python3 -c '
import json, sys
name = sys.argv[1]
for row in json.load(sys.stdin):
    newest = row.get("newest") or {}
    if newest.get("container") == name:
        print(row["state"], row["sandbox_id"], newest.get("evidence", "")); break
else:
    print("no-row")' "$CONTAINER")"
[[ "$state" == crashed\ * ]] || fail 4 "status for $CONTAINER: $state (expected crashed)"
step 4 "launcher killed, container removed; status shows $state"

# ---------------------------------------------------------------- 5. resume names the finishing command
if [[ -f "$DRIVER" ]] && grep -qE '(^|[^[:alnum:]_-])resume\)' "$DRIVER"; then
  resume_out="$(cd "$REPO" && bash "$DRIVER" resume 2>&1 || true)"
  printf '%s\n' "$resume_out" | grep -F "$SANDBOX_ID" | grep -q 'agent-sandbox' \
    || fail 5 "resume printed no finishing command for $SANDBOX_ID: ${resume_out:0:600}"
  step 5 "resume prints: $(printf '%s\n' "$resume_out" | grep -F "$SANDBOX_ID" | head -1)"
else
  step 5 "SKIP: $DRIVER has no resume verb yet"
fi

# ---------------------------------------------------------------- 6. restore
unset AGENT_SANDBOX_ADMISSION_ENABLED AGENT_SANDBOX_ADMISSION_MEMORY_BUDGET \
      AGENT_SANDBOX_ADMISSION_MEM_FLOOR_GB
step 6 "config restored (env override dropped; ~/agent-sandbox/config.json was never written)"

echo PASS
