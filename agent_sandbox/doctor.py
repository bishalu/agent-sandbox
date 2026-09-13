"""Health checks (spec R-10).

Every failure carries a remediation line. The last check actually executes a
trivial container, because `docker info` succeeding does not prove the
runtime can start one.
"""

import glob
import json
import os
import pathlib
import platform
import re
import shutil
import subprocess
import tempfile
import time

from . import (agent_home, cleanup, config, credentials, image, memlog, mounts,
               resources, worktree)
from .backend import SandboxSpec
from .docker_backend import LocalDockerBackend
from .errors import SandboxError
from .metadata import RunRecord

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

G = 1024 ** 3

# The disk guard's WSL half (KTD11): the distro's root is a file on the
# Windows drive; when it is not sparse, freed guest blocks never return to
# Windows and the drive fills until a write burst kills the VM.
CMD_EXE = pathlib.Path("/mnt/c/Windows/System32/cmd.exe")
VHDX_GLOB = "/mnt/c/Users/*/AppData/Local/wsl/*/ext4.vhdx"
VHDX_REMEDY = ("wsl --shutdown; wsl --manage <Distro> --set-sparse true; "
               "then fstrim -v / as root (wsl.exe -u root -- fstrim -v /)")

# Doctor output lands in evidence files; never let a value that looks like a
# secret through, whatever a probe printed (R-22).
_SECRET_KV = re.compile(
    r"(['\"]?[A-Za-z0-9_]*(?:TOKEN|KEY|SECRET)[A-Za-z0-9_]*['\"]?\s*[=:]\s*)(['\"]?[^\s,'\"]+['\"]?)",
    re.IGNORECASE)
# Anthropic-style bearer tokens have a recognisable prefix; mask them even
# when no key name precedes them.
_BARE_TOKEN = re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")


def redact(text):
    out = _SECRET_KV.sub(lambda m: m.group(1) + "[redacted]", text or "")
    return _BARE_TOKEN.sub("[redacted]", out)


class Check:
    def __init__(self, name, status, detail="", remedy=""):
        self.name = name
        self.status = status
        self.detail = detail
        self.remedy = remedy

    def to_dict(self):
        return {"name": self.name, "status": self.status,
                "detail": self.detail, "remedy": self.remedy}


def _sh(args):
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              env=config.docker_env())
    except FileNotFoundError:
        return None


def run_checks(quick=False, with_quota=False):
    checks = []
    env = config.docker_env()
    cfg = config.load_config()

    # --- platform ---
    is_wsl = "microsoft" in platform.uname().release.lower()
    checks.append(Check(
        "WSL2 environment", PASS if is_wsl else WARN,
        platform.uname().release,
        "" if is_wsl else "Not WSL — the tool still works, but the WSL-specific "
                          "performance notes in the README will not apply.",
    ))

    # --- systemd user manager ---
    p = _sh(["systemctl", "--user", "is-system-running"])
    state = (p.stdout.strip() if p else "unknown") or "unknown"
    ok = state in ("running", "degraded")
    checks.append(Check(
        "systemd user manager", PASS if ok else FAIL, state,
        "" if ok else "The rootless Docker daemon is a systemd --user service. "
                      "Enable systemd in /etc/wsl.conf ([boot] systemd=true) and restart WSL.",
    ))

    # --- cgroup v2 delegation ---
    cg = pathlib.Path("/sys/fs/cgroup/user.slice") / f"user-{os.getuid()}.slice" / \
        f"user@{os.getuid()}.service" / "cgroup.controllers"
    delegated = cg.read_text().split() if cg.exists() else []
    need = {"cpu", "memory", "pids"}
    ok = need.issubset(set(delegated))
    checks.append(Check(
        "cgroup v2 delegation", PASS if ok else FAIL,
        f"delegated: {' '.join(delegated) or 'none'}",
        "" if ok else "Without cpu/memory/pids delegation the sandbox cannot enforce "
                      "resource limits. Check that cgroup v2 is active and systemd "
                      "delegates these controllers to your user slice.",
    ))

    # --- linger ---
    p = _sh(["loginctl", "show-user", os.environ.get("USER", "")])
    lingering = bool(p and "Linger=yes" in (p.stdout or ""))
    checks.append(Check(
        "systemd linger", PASS if lingering else WARN,
        "enabled" if lingering else "disabled",
        "" if lingering else
        f"sudo loginctl enable-linger {os.environ.get('USER','$USER')}\n"
        "     Without it the rootless daemon stops when your last session ends.",
    ))

    # --- docker present ---
    if shutil.which("docker") is None:
        checks.append(Check("docker CLI", FAIL, "not found on PATH",
                            "Install rootless Docker (see README setup)."))
        return checks
    checks.append(Check("docker CLI", PASS, shutil.which("docker")))

    # --- daemon reachable + rootless ---
    backend = LocalDockerBackend()
    p = _sh(["docker", "info", "--format", "{{json .SecurityOptions}}"])
    if not p or p.returncode != 0:
        checks.append(Check(
            "docker daemon", FAIL,
            f"unreachable at {env['DOCKER_HOST']}",
            "systemctl --user start docker",
        ))
        return checks
    rootless = "rootless" in p.stdout
    checks.append(Check(
        "rootless daemon", PASS if rootless else FAIL,
        env["DOCKER_HOST"],
        "" if rootless else
        "agent-sandbox refuses a rootful daemon: an escape there is host root.\n"
        "     Start the rootless daemon (systemctl --user start docker) and unset DOCKER_HOST.",
    ))

    # --- rootful daemon should not be in use ---
    p = _sh(["systemctl", "is-active", "docker"])
    rootful_active = bool(p and p.stdout.strip() == "active")
    checks.append(Check(
        "system-wide docker.service", WARN if rootful_active else PASS,
        "active" if rootful_active else "inactive/disabled",
        "sudo systemctl disable --now docker.service docker.socket\n"
        "     A rootful daemon is not used by this tool and is best left off."
        if rootful_active else "",
    ))

    # --- git ---
    p = _sh(["git", "--version"])
    checks.append(Check("git", PASS if p and p.returncode == 0 else FAIL,
                        (p.stdout.strip() if p else "missing"),
                        "" if p and p.returncode == 0 else "sudo apt install git"))

    # --- gh (optional) ---
    if shutil.which("gh"):
        p = _sh(["gh", "auth", "status"])
        ok = p and p.returncode == 0
        checks.append(Check("gh auth (optional)", PASS if ok else WARN,
                            "authenticated" if ok else "not authenticated",
                            "" if ok else "gh auth login — only needed for --with-github-auth"))
    else:
        checks.append(Check("gh (optional)", WARN, "not installed",
                            "Only needed for --with-github-auth."))

    # --- claude credentials ---
    creds_file = credentials.CREDENTIALS_FILE
    if creds_file.exists():
        mode = oct(creds_file.stat().st_mode & 0o777)
        checks.append(Check("Claude credentials", PASS,
                            f"{creds_file} ({mode})"))
    elif os.environ.get("ANTHROPIC_API_KEY"):
        checks.append(Check("Claude credentials", PASS,
                            "ANTHROPIC_API_KEY in environment"))
    else:
        checks.append(Check(
            "Claude credentials", WARN, "none found",
            "Run `claude` on the host to log in, or export ANTHROPIC_API_KEY.\n"
            "     Sandboxes still work; Claude Code inside them will not be authenticated.",
        ))

    # --- writable state dirs ---
    try:
        config.ensure_dirs()
        probe = config.WORKTREES / ".doctor-probe"
        probe.write_text("ok")
        probe.unlink()
        checks.append(Check("state directories", PASS, str(config.ROOT)))
    except OSError as e:
        checks.append(Check("state directories", FAIL, str(e),
                            f"Check permissions on {config.ROOT}"))

    # --- disk ---
    free = cleanup.disk_free_gb()
    warn_at = float(config.resolve("disk_warn_gb") or 40)
    checks.append(Check(
        "disk headroom", PASS if free >= warn_at else WARN,
        f"{free:.0f} GiB free",
        "" if free >= warn_at else
        "Low free space. `agent-sandbox clean --docker` prunes dangling images "
        "and build cache;\n     `agent-sandbox clean` removes old sandboxes.",
    ))

    # --- docker disk usage (R-14) ---
    rows = cleanup.docker_usage()
    if rows:
        detail = "; ".join(f"{r['type']}: {r['size']} ({r['reclaimable']} reclaimable)"
                           for r in rows)
        checks.append(Check("docker disk usage", PASS, detail))

    # --- image ---
    need, why = image.needs_build()
    checks.append(Check(
        "base image", WARN if need else PASS,
        why if need else config.IMAGE_NAME,
        "agent-sandbox build   (or just run a sandbox — it builds automatically)"
        if need else "",
    ))

    # --- gVisor status (R-01) ---
    if shutil.which("runsc"):
        available = backend.runtime_available("runsc")
        checks.append(Check(
            "gVisor (optional)", PASS if available else WARN,
            "registered, resource limits NOT enforced under runsc"
            if available else "installed but not registered with the daemon",
            "" if available else
            "Register it in ~/.config/docker/daemon.json to use --experimental-gvisor.",
        ))
    else:
        checks.append(Check("gVisor (optional)", WARN, "not installed",
                            "Only needed for --experimental-gvisor."))

    # --- the real test: execute a container (R-10) ---
    if not quick and not need:
        p = _sh(["docker", "run", "--rm",
                 "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
                 "--memory", "256m", "--pids-limit", "64", "--cpus", "1",
                 "--label", f"{config.LABEL_MANAGED}=true",
                 config.IMAGE_NAME, "bash", "-c", "echo sandbox-ok"])
        ok = p and p.returncode == 0 and "sandbox-ok" in (p.stdout or "")
        checks.append(Check(
            "trivial sandbox execution", PASS if ok else FAIL,
            "container ran and exited cleanly" if ok else
            (p.stderr.strip()[:300] if p else "failed to execute"),
            "" if ok else "The daemon is reachable but cannot start a container. "
                          "Check `journalctl --user -u docker` for the runtime error.",
        ))
    elif need:
        checks.append(Check("trivial sandbox execution", WARN, "skipped: image not built",
                            "Run `agent-sandbox build` first."))

    # --- limits must be ENFORCED, not merely accepted (R-03) ---
    # The daemon happily records --memory/--pids-limit and then ignores them if
    # the cgroup driver is wrong (native.cgroupdriver=cgroupfs breaks rootless
    # delegation). `docker inspect` still shows the values, so only reading the
    # cgroup from inside a container proves enforcement.
    if not quick and not need:
        p = _sh(["docker", "run", "--rm", "--memory", "64m", "--pids-limit", "16",
                 "--label", f"{config.LABEL_MANAGED}=true", config.IMAGE_NAME,
                 "bash", "-c",
                 "cat /sys/fs/cgroup/memory.max; cat /sys/fs/cgroup/pids.max"])
        out = (p.stdout or "").split()
        enforced = len(out) >= 2 and out[0] == "67108864" and out[1] == "16"
        checks.append(Check(
            "resource limits enforced", PASS if enforced else FAIL,
            f"memory.max={out[0] if out else '?'} pids.max={out[1] if len(out) > 1 else '?'}"
            if out else "could not read cgroup inside container",
            "" if enforced else
            "Limits are accepted but NOT enforced — containers can exhaust host memory.\n"
            "     Usual cause: a cgroup driver override in ~/.config/docker/daemon.json.\n"
            "     Remove any \"exec-opts\": [\"native.cgroupdriver=cgroupfs\"] entry\n"
            "     (rootless Docker needs the default systemd driver), then:\n"
            "     systemctl --user restart docker",
        ))

    # ------------------------------------------------------------ v1.1 (R-16..R-22)
    checks += _host_checks(cfg)
    if not quick and not need:
        checks += _sandbox_probe(cfg, with_quota=with_quota)
    elif need:
        checks.append(Check("worktree sandbox probe", WARN, "skipped: image not built",
                            "Run `agent-sandbox build` first."))
    if quick or need:
        # The thread-env check reads the probe's output; no probe, no reading.
        checks.append(thread_env_check(None, resources.ResourceConfig(cfg=cfg).thread_env()))
    checks += _drift_checks()
    return checks


# ---------------------------------------------------------------- v1.1 checks
def _throwaway_repo():
    d = pathlib.Path(tempfile.mkdtemp(prefix="agent-sandbox-doctor-"))
    subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.email", "doctor@agent-sandbox.local"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.name", "agent-sandbox doctor"], check=True)
    (d / "README").write_text("doctor probe\n")
    subprocess.run(["git", "-C", str(d), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(d), "commit", "-q", "-m", "probe"], check=True)
    return d


def _host_checks(cfg):
    checks = []

    # --- git identity resolves (R-17) ---
    d = _throwaway_repo()
    try:
        env = mounts.git_identity_env(worktree.Workspace("doctor", d, "worktree", repo=d))
        checks.append(Check("git identity", PASS,
                            f"resolves ({env['GIT_AUTHOR_EMAIL']} for a repo with a local identity)"))
    except SandboxError as e:
        checks.append(Check("git identity", FAIL, e.message, e.remedy or ""))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    glob_email = subprocess.run(["git", "config", "--global", "user.email"],
                                capture_output=True, text=True).stdout.strip()
    checks.append(Check(
        "global git identity", PASS if glob_email else WARN,
        glob_email or "not set",
        "" if glob_email else "git config --global user.email ... / user.name ...\n"
                              "     Repos without a local identity cannot commit inside a sandbox.",
    ))

    # --- skill mounts (R-19) ---
    try:
        sk = mounts.skill_mounts(cfg)
        checks.append(Check("skill mounts", PASS,
                            ", ".join(str(m.host) for m in sk) if sk else "none configured"))
    except SandboxError as e:
        checks.append(Check("skill mounts", FAIL, e.message, e.remedy or ""))

    # --- agent home template (R-18) ---
    try:
        t = agent_home._template_dir(cfg)
        checks.append(Check("agent home template", PASS, str(t)))
    except SandboxError as e:
        checks.append(Check("agent home template", FAIL, e.message, e.remedy or ""))

    # --- memory log (R8, KTD5): the timer runs and the last sample is fresh ---
    # A launch fails closed on this log, so a stopped timer or a stale line
    # is a failed launch waiting to happen, not a warning.
    active, fresh = memlog.health(cfg)
    ok = active and fresh.fresh
    if active and fresh.fresh:
        detail = f"timer active, last sample {fresh.age_s:.0f} s old"
    elif active:
        detail = f"timer active but {fresh.reason}"
    else:
        detail = f"timer inactive; {fresh.reason}" if not fresh.fresh else \
            f"timer inactive; last sample {fresh.age_s:.0f} s old"
    checks.append(Check(
        "memlog", PASS if ok else FAIL, detail,
        "" if ok else
        f"agent-sandbox memlog install   (enables {memlog.TIMER}; a sample lands within a minute)\n"
        f"     Launches refuse admission until the newest line of {memlog.LOG} is fresh.",
    ))

    # --- disk guard (KTD11): the host drive floor and, under WSL, a sparse vhdx ---
    checks.append(host_disk_check(cfg))
    if "microsoft" in platform.uname().release.lower():
        checks.append(vhdx_sparse_check(cfg))

    # --- credential expiry (R-18, D18): only the expiry field is read ---
    f = credentials.CREDENTIALS_FILE
    if f.exists():
        try:
            exp_ms = json.loads(f.read_text()).get("claudeAiOauth", {}).get("expiresAt")
        except (ValueError, OSError):
            exp_ms = None
        if exp_ms:
            left = exp_ms / 1000.0 - time.time()
            mins = int(left // 60)
            soon = left < 3600
            checks.append(Check(
                "Claude access token expiry", WARN if soon else PASS,
                f"{'expired' if left < 0 else f'{mins} min left'}",
                "" if not soon else
                "The read-only credentials mount cannot persist a refresh inside a sandbox.\n"
                "     Run `claude` on the host once to refresh before a long run, or see the\n"
                "     README on CLAUDE_CODE_OAUTH_TOKEN if refresh proves unreliable.",
            ))
    return checks


# ---------------------------------------------------------------- disk guard (KTD11)
def host_disk_check(cfg, read_disk_free=memlog.disk_free):
    """The host drive backing the guest has the admission floor free. A
    launch refuses below it, so this is a FAIL, not a warning."""
    path = config.host_disk_path(cfg)
    floor_gb = float(config.resolve("admission_disk_floor_gb", None, cfg))
    free = read_disk_free([path]).get(path)
    remedy = (
        f"Free space on {path}, the drive that holds this distro's ext4.vhdx under WSL:\n"
        "     empty the Windows recycle bin and Downloads, `agent-sandbox clean --docker`\n"
        "     (dangling images and build cache), `agent-sandbox clean` (old sandboxes).\n"
        "     Freed guest blocks only reach Windows when the vhdx is sparse; as owner:\n"
        f"     {VHDX_REMEDY}\n"
        f"     Launches refuse admission while {path} is below {floor_gb:g} GiB.")
    if free is None:
        return Check("host disk", FAIL, f"{path}: free space unreadable (floor {floor_gb:g} GiB)",
                     remedy)
    ok = free / G >= floor_gb
    return Check("host disk", PASS if ok else FAIL,
                 f"{path}: {free / G:.1f} GiB free (floor {floor_gb:g} GiB)",
                 "" if ok else remedy)


def windows_path(path):
    """/mnt/<drive>/... as the Windows path fsutil wants; anything else unchanged."""
    m = re.match(r"^/mnt/([a-zA-Z])(/.*)?$", str(path))
    if not m:
        return str(path)
    rest = (m.group(2) or "").replace("/", "\\")
    return f"{m.group(1).upper()}:{rest}"


def fsutil_sparse_queryflag(path, cmd_exe=CMD_EXE, run=subprocess.run):
    """`fsutil sparse queryflag` through cmd.exe interop; None when cmd.exe
    is not there (not WSL, or interop off). The text is returned raw."""
    cmd_exe = pathlib.Path(cmd_exe)
    if not cmd_exe.exists():
        return None
    try:
        p = run([str(cmd_exe), "/c", "fsutil", "sparse", "queryflag", windows_path(path)],
                capture_output=True, timeout=30)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    out = (p.stdout or b"") + (p.stderr or b"")
    return out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)


def vhdx_candidates(cfg, glob_paths=glob.glob):
    """`admission_vhdx_path` when set, then every distro's ext4.vhdx under
    the Windows profiles."""
    found = []
    configured = config.resolve("admission_vhdx_path", None, cfg)
    if configured:
        found.append(str(configured))
    for p in sorted(glob_paths(VHDX_GLOB)):
        if p not in found:
            found.append(p)
    return found


def vhdx_sparse_check(cfg, run=fsutil_sparse_queryflag, find=vhdx_candidates):
    """Under WSL, every distro vhdx is sparse. A file that cannot be found or
    cmd.exe missing is a WARN: the check could not run, which is not the
    same as the disk being at risk."""
    paths = find(cfg)
    if not paths:
        return Check("vhdx sparse", WARN, f"no ext4.vhdx found under {VHDX_GLOB}",
                     "Set admission_vhdx_path in config.json to the distro's ext4.vhdx "
                     "so doctor can check it.")
    results, bad, unknown = [], [], []
    for path in paths:
        out = run(path)
        if out is None:
            return Check("vhdx sparse", WARN, f"{CMD_EXE} not available; cannot query {path}",
                         "Windows interop is off or this is not WSL; check by hand: "
                         "fsutil sparse queryflag <vhdx> in an elevated prompt.")
        text = " ".join(out.replace("\x00", "").replace("\r", "").split())
        if "NOT set as sparse" in text:
            bad.append(path)
            results.append(f"{path}: NOT set as sparse")
        elif "is set as sparse" in text:
            results.append(f"{path}: sparse")
        else:
            unknown.append(path)
            results.append(f"{path}: unrecognised fsutil output: {text[:120]}")
    detail = "; ".join(results)
    if bad:
        return Check("vhdx sparse", FAIL, detail, VHDX_REMEDY)
    if unknown:
        return Check("vhdx sparse", WARN, detail,
                     "Check by hand: fsutil sparse queryflag <vhdx> in an elevated prompt.")
    return Check("vhdx sparse", PASS, detail)


def _sandbox_probe(cfg, with_quota=False):
    """One throwaway repo, two containers: commit, overlays, bridge, gate, persistence."""
    checks = []
    repo = _throwaway_repo()
    ws = None
    try:
        ws = worktree.create(repo)
        img = config.resolve("image", None, cfg)
        home = agent_home.ensure(ws.sandbox_id, cfg, img, quiet=True)
        creds = credentials.resolve(sandbox_dir=config.RUNS / ws.sandbox_id, agent_home=home)
        res = resources.ResourceConfig("1", "1g", "256", "5m", cfg)
        plan = mounts.plan_for(ws, cfg, home, resources=res)
        backend = LocalDockerBackend()

        def run(cmd):
            rec = RunRecord.load(ws.sandbox_id) or RunRecord(ws.sandbox_id)
            spec = SandboxSpec(ws.sandbox_id, ws, ["bash", "-lc", cmd], resources=res,
                               image=img, credentials=creds, record=rec,
                               stream_output=False, mounts=plan.mounts, env=plan.env)
            r = backend.run(spec)
            out = rec.stdout_log.read_text() if rec.stdout_log.exists() else ""
            return r, redact(out)

        script1 = (
            "echo x > probe && git add probe && git commit -qm probe && echo COMMIT_OK;"
            " C=$(git rev-parse --path-format=absolute --git-common-dir);"
            " (touch \"$C/hooks/doctor\" 2>/dev/null && echo HOOK_WRITABLE) || echo HOOK_RO;"
            " git worktree prune && echo PRUNE_OK;"
            " echo BRIDGE_TEMPLATE=$(PI_CODING_AGENT_DIR=/opt/agent-sandbox/pi-agent-template pi --list-models 2>/dev/null | grep -c claude-bridge);"
            " echo BRIDGE_HOME=$(pi --list-models 2>/dev/null | grep -c claude-bridge);"
            " mkdir -p /tmp/nocreds && G=$(CLAUDE_CONFIG_DIR=/tmp/nocreds claude -p hi --dangerously-skip-permissions 2>&1 | head -c 400);"
            " case \"$G\" in *'cannot be used with root'*) echo GATE_CLOSED;; *'ot logged in'*|*'login'*|*'Login'*) echo GATE_OPEN;; *) echo \"GATE_UNKNOWN: $G\";; esac;"
            " echo marker > /root/.claude/doctor-marker && echo MARKER_WRITTEN;"
            " echo THREADS=$OMP_NUM_THREADS,$OPENBLAS_NUM_THREADS,$MKL_NUM_THREADS,$AGENT_SANDBOX_THREADS"
        )
        r1, out1 = run(script1)
        checks.append(thread_env_check(out1, res.thread_env()))
        ok = r1.status == "completed" and "COMMIT_OK" in out1
        on_host = subprocess.run(["git", "-C", str(ws.path), "log", "--oneline"],
                                 capture_output=True, text=True).stdout.count("\n")
        checks.append(Check(
            "worktree sandbox commit", PASS if ok and on_host >= 2 else FAIL,
            "commit inside the sandbox is visible on the host branch" if ok and on_host >= 2
            else f"status={r1.status}; {out1.strip()[-300:]}",
            "" if ok and on_host >= 2 else
            "Git inside the worktree sandbox failed. Check the trusted mounts in\n"
            f"     runs/{ws.sandbox_id}/run.json and the container log.",
        ))
        checks.append(Check(
            "git overlays read-only", PASS if "HOOK_RO" in out1 else FAIL,
            "hooks/ refused a write" if "HOOK_RO" in out1 else "hooks/ accepted a write",
            "" if "HOOK_RO" in out1 else "The .git/hooks overlay is missing; a sandboxed agent could plant a hook.",
        ))
        checks.append(Check(
            "worktree prune is a no-op", PASS if "PRUNE_OK" in out1 and on_host >= 2 else FAIL,
            "prune inside the container kept the worktree",
        ))
        bt = re.search(r"BRIDGE_TEMPLATE=(\d+)", out1)
        bh = re.search(r"BRIDGE_HOME=(\d+)", out1)
        ok_b = bt and bh and int(bt.group(1)) > 0 and int(bh.group(1)) > 0
        checks.append(Check(
            "pi-claude-bridge models", PASS if ok_b else FAIL,
            f"image template: {bt.group(1) if bt else '?'}, seeded home: {bh.group(1) if bh else '?'}",
            "" if ok_b else "Rebuild the image (`agent-sandbox build`); the bridge did not load.",
        ))
        gate = "GATE_OPEN" if "GATE_OPEN" in out1 else ("GATE_CLOSED" if "GATE_CLOSED" in out1 else "unknown")
        checks.append(Check(
            "root bypass gate (IS_SANDBOX)", PASS if gate == "GATE_OPEN" else FAIL,
            "Claude Code accepts bypass as root inside the sandbox (no quota spent)"
            if gate == "GATE_OPEN" else f"{gate}: {out1.strip()[-200:]}",
            "" if gate == "GATE_OPEN" else "IS_SANDBOX=1 is not reaching claude; check the image env.",
        ))

        # Second container over the same sandbox: does the home persist?
        r2, out2 = run("test -f /root/.claude/doctor-marker && echo MARKER_PERSISTED"
                       + (" ; claude -p 'Reply with exactly: DOCTOR_OK' --output-format json --max-turns 1 2>&1 | grep -o '\"result\":\"[^\"]*\"'"
                          if with_quota else ""))
        persisted = "MARKER_PERSISTED" in out2
        checks.append(Check(
            "agent home persists across runs", PASS if persisted else FAIL,
            "file written in run 1 present in run 2" if persisted else out2.strip()[-200:],
            "" if persisted else "The agent home mount is not persisting; check runs/<id>/agent-home.",
        ))
        if with_quota:
            ok_q = "DOCTOR_OK" in out2
            checks.append(Check("authenticated claude -p (quota spent)", PASS if ok_q else FAIL,
                                "DOCTOR_OK" if ok_q else out2.strip()[-200:],
                                "" if ok_q else "Log in with `claude` on the host."))
    except SandboxError as e:
        checks.append(Check("worktree sandbox probe", FAIL, e.message, e.remedy or ""))
    except Exception as e:  # a probe must never take doctor down with it
        checks.append(Check("worktree sandbox probe", FAIL, f"{type(e).__name__}: {e}"))
    finally:
        if ws is not None:
            try:
                worktree.remove(ws.sandbox_id, force=True)
            except Exception:
                pass
        shutil.rmtree(repo, ignore_errors=True)
    return checks


_THREADS_LINE = re.compile(r"^THREADS=(.*)$", re.M)


def thread_env_check(probe_output, expected):
    """A container sees the thread caps plan_for merged into its env (R10).

    Reads the `THREADS=a,b,c,d` line the sandbox probe echoes rather than
    starting a container of its own; `expected` is the probe's
    `ResourceConfig.thread_env()`. `None` output means the probe did not run.
    """
    name = "thread caps in container env"
    want = [expected[k] for k in resources.ResourceConfig.THREAD_VARS]
    if probe_output is None:
        return Check(name, WARN, "probe not run",
                     "Run `agent-sandbox doctor` without --quick, after `agent-sandbox build`.")
    m = _THREADS_LINE.search(probe_output)
    if not m:
        return Check(name, FAIL, "the probe printed no THREADS line",
                     "The probe container did not reach the env check; see the checks above.")
    got = m.group(1).strip().split(",")
    got += [""] * (len(want) - len(got))
    wrong = [f"{k}={g or 'unset'} (want {w})"
             for k, g, w in zip(resources.ResourceConfig.THREAD_VARS, got, want) if g != w]
    if wrong:
        return Check(name, FAIL, "; ".join(wrong),
                     "mounts.plan_for merges ResourceConfig.thread_env() into the run env; "
                     "a backend that drops plan.env loses these caps.")
    return Check(name, PASS, ", ".join(f"{k}={w}" for k, w in
                                       zip(resources.ResourceConfig.THREAD_VARS, want)))


def _drift_checks():
    """Existing sandboxes whose agent home predates the current image (R-18)."""
    checks = []
    drifted = []
    for rec in RunRecord.all():
        home = agent_home.load(rec.sandbox_id)
        if home and home.drift():
            drifted.append(rec.sandbox_id)
    if drifted:
        checks.append(Check(
            "agent home drift", WARN, ", ".join(drifted),
            "These sandboxes were seeded from an older image. Re-seed with\n"
            "     agent-sandbox rm <id> && agent-sandbox <repo>  (or keep using them knowingly).",
        ))
    return checks


def summarize(checks):
    return {
        "pass": sum(1 for c in checks if c.status == PASS),
        "warn": sum(1 for c in checks if c.status == WARN),
        "fail": sum(1 for c in checks if c.status == FAIL),
    }
