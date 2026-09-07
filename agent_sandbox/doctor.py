"""Health checks (spec R-10).

Every failure carries a remediation line. The last check actually executes a
trivial container, because `docker info` succeeding does not prove the
runtime can start one.
"""

import os
import pathlib
import platform
import shutil
import subprocess

from . import cleanup, config, credentials, image
from .docker_backend import LocalDockerBackend

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


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


def run_checks(quick=False):
    checks = []
    env = config.docker_env()

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

    return checks


def summarize(checks):
    return {
        "pass": sum(1 for c in checks if c.status == PASS),
        "warn": sum(1 for c in checks if c.status == WARN),
        "fail": sum(1 for c in checks if c.status == FAIL),
    }
