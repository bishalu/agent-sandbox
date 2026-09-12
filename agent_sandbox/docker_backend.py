"""LocalDockerBackend: rootless Docker execution (spec R-02, R-05, R-06).

Hardening applied to every run, in every mode:
  - rootless daemon only (a rootful daemon is refused outright)
  - --security-opt no-new-privileges
  - --cap-drop ALL, with a documented minimal add-back set
  - no docker socket, no host $HOME, no /, no SSH keys
  - resource limits always applied
  - ephemeral container (--rm)

On the container user: the process runs as UID 0 *inside the container*.
Under rootless Docker that maps to the invoking host user, so it is not host
root, and it is what keeps files written into the bind-mounted workspace
owned by you rather than by an unusable subordinate uid.
"""

import os
import shutil
import signal
import subprocess
import sys
import threading
import time

from . import config, mounts
from .backend import SandboxBackend, SandboxResult
from .errors import DockerUnavailable, NotImplementedYet, RootfulDockerRefused

# Dropping every capability breaks apt/dpkg, which agents legitimately use to
# install OS packages inside their disposable container. These are added back
# after --cap-drop ALL. Still far below Docker's default set: no NET_RAW, no
# NET_ADMIN, no SYS_ADMIN, no SYS_PTRACE, no MKNOD, no SYS_CHROOT.
DEFAULT_CAP_ADD = ["CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "SETGID", "SETUID"]

VALID_NETWORKS = ("full", "none", "restricted")


class LocalDockerBackend(SandboxBackend):
    name = "local-docker"

    def __init__(self, strict_caps=False):
        self.strict_caps = strict_caps
        self._env = config.docker_env()

    # -- environment checks ---------------------------------------------
    def _docker(self, args, **kw):
        return subprocess.run(["docker"] + args, env=self._env,
                              capture_output=True, text=True, **kw)

    def preflight(self):
        if shutil.which("docker") is None:
            raise DockerUnavailable(
                "docker is not installed or not on PATH",
                "Install rootless Docker: see the setup section of the README.",
            )
        p = self._docker(["info", "--format", "{{json .SecurityOptions}}"])
        if p.returncode != 0:
            raise DockerUnavailable(
                f"cannot reach the Docker daemon at {self._env['DOCKER_HOST']}\n"
                f"  {p.stderr.strip()[:400]}",
                "Start it with: systemctl --user start docker\n"
                "  (and `sudo loginctl enable-linger $USER` so it survives logout)",
            )
        if "rootless" not in p.stdout:
            raise RootfulDockerRefused(
                "the reachable Docker daemon is NOT rootless",
                "agent-sandbox refuses to use a rootful daemon, because a "
                "container escape there is host root.\n"
                "  Start the rootless daemon: systemctl --user start docker\n"
                "  and unset DOCKER_HOST if it points at the system socket.",
            )
        return True

    def is_rootless(self):
        p = self._docker(["info", "--format", "{{json .SecurityOptions}}"])
        return p.returncode == 0 and "rootless" in p.stdout

    def runtime_available(self, runtime):
        p = self._docker(["info", "--format", "{{json .Runtimes}}"])
        return p.returncode == 0 and f'"{runtime}"' in p.stdout

    # -- argv construction ----------------------------------------------
    def build_args(self, spec, container_name):
        """The full docker run argv. Separated out so it is testable."""
        if spec.network not in VALID_NETWORKS:
            raise NotImplementedYet(
                f"unknown network policy: {spec.network}",
                f"Valid values: {', '.join(VALID_NETWORKS)}.",
            )
        if spec.network == "restricted":
            # Never silently downgrade to full (R-06).
            raise NotImplementedYet(
                "network policy 'restricted' is not implemented yet",
                "Use --network full (default) or --network none. "
                "'restricted' is reserved for a future outbound allowlist proxy "
                "and deliberately fails rather than silently granting full access.",
            )

        args = ["run", "--rm", "--name", container_name]

        # --- hardening (R-02) ---
        args += ["--security-opt", "no-new-privileges"]
        args += ["--cap-drop", "ALL"]
        if not self.strict_caps:
            for cap in DEFAULT_CAP_ADD:
                args += ["--cap-add", cap]

        # --- resources (R-03) ---
        args += spec.resources.docker_args()

        # --- network (R-06) ---
        if spec.network == "none":
            args += ["--network", "none"]

        # --- labels, so cleanup can find exactly our containers (R-14) ---
        args += ["--label", f"{config.LABEL_MANAGED}=true"]
        args += ["--label", f"{config.LABEL_ID}={spec.sandbox_id}"]

        # --- workspace: the only host path mounted read-write (R-05) ---
        args += ["-v", f"{spec.workspace.path}:/workspace"]
        args += ["-w", "/workspace"]

        # --- shared package caches, never the host's real caches (R-05) ---
        args += ["-v", f"{config.CACHE / 'npm'}:/root/.npm"]
        args += ["-v", f"{config.CACHE / 'pnpm'}:/root/.local/share/pnpm/store"]
        args += ["-v", f"{config.CACHE / 'pip'}:/root/.cache/pip"]
        args += ["-v", f"{config.CACHE / 'uv'}:/root/.cache/uv"]

        # --- declared mounts: agent home, skills, git common dir (R-16..R-18) ---
        # Rendered with --mount so a missing host source is an error, never a
        # silently created empty directory. Docker orders mounts by
        # destination, so a nested read-only file (credentials) still layers
        # correctly on top of a read-write parent declared here.
        args += mounts.render(spec.mounts)

        # --- read-only root, an independent option (R-05) ---
        if spec.read_only_root:
            args += ["--read-only"]
            args += ["--tmpfs", "/tmp:rw,exec,nosuid,size=2g"]
            args += ["--tmpfs", "/run:rw,nosuid,size=64m"]
            # HOME must stay writable for package managers and agent state.
            args += ["--tmpfs", "/root:rw,exec,nosuid,size=1g"]

        # --- credentials (R-07) ---
        if spec.credentials:
            args += spec.credentials.docker_args()

        # --- environment ---
        args += ["-e", "AGENT_SANDBOX=1"]
        args += ["-e", f"AGENT_SANDBOX_ID={spec.sandbox_id}"]
        args += ["-e", "HOME=/root"]
        # IS_SANDBOX=1 is the gate Claude Code reads before refusing
        # bypassPermissions as root; the container IS the sandbox (R-17).
        # CLAUDE_CONFIG_DIR keeps .claude.json inside the (persistent) home.
        # Both are also baked into the image; repeating them here means a
        # custom --image cannot silently drop them.
        args += ["-e", "IS_SANDBOX=1"]
        args += ["-e", f"CLAUDE_CONFIG_DIR={mounts.CONTAINER_HOME}/.claude"]
        for k, v in (spec.env or {}).items():
            args += ["-e", f"{k}={v}"]

        # --- gVisor escape hatch (R-01) ---
        if spec.experimental_gvisor:
            args += ["--runtime", "runsc"]

        if spec.interactive:
            args += ["-it"] if sys.stdin.isatty() else ["-i"]

        args += [spec.image or config.IMAGE_NAME]
        args += list(spec.command) if spec.command else ["bash", "-l"]
        return args

    # -- execution -------------------------------------------------------
    def run(self, spec):
        self.preflight()

        if spec.experimental_gvisor and not self.runtime_available("runsc"):
            raise DockerUnavailable(
                "gVisor runtime 'runsc' is not registered with the daemon",
                "Install gVisor and register it, or drop --experimental-gvisor.",
            )

        suffix = f"{int(time.time())}-{os.getpid()}"
        container_name = f"agent-sandbox-{spec.sandbox_id}-{suffix}"
        args = self.build_args(spec, container_name)

        rec = spec.record
        started = time.time()
        if rec:
            rec.add_container(container_name,
                              "runsc" if spec.experimental_gvisor else "runc",
                              spec.command or ["bash", "-l"])
            rec.start().save()

        timeout = spec.resources.timeout
        timer = None
        timed_out = {"hit": False}

        def _kill():
            """Hard wall-clock ceiling (R-03). Preserve the workspace.

            Stop first so the agent's own signal handlers can close their
            traces (an SSSF run marks its session ended on SIGTERM), then
            kill whatever is still up after the grace period (R-21).
            """
            timed_out["hit"] = True
            subprocess.run(["docker", "stop", "-t", "30", container_name],
                           env=self._env, capture_output=True, text=True)
            subprocess.run(["docker", "kill", container_name],
                           env=self._env, capture_output=True, text=True)

        try:
            if spec.interactive:
                # Interactive: hand the terminal straight to the container.
                # Output is the user's session, not a captured log.
                if timeout:
                    timer = threading.Timer(timeout, _kill)
                    timer.daemon = True
                    timer.start()
                proc = subprocess.Popen(["docker"] + args, env=self._env)
                exit_code = proc.wait()
            else:
                # Non-interactive: tee to the terminal and to the run logs (R-09).
                if rec:
                    rec.dir.mkdir(parents=True, exist_ok=True)
                    out_f = open(rec.stdout_log, "ab")
                    err_f = open(rec.stderr_log, "ab")
                else:
                    out_f = err_f = None

                proc = subprocess.Popen(
                    ["docker"] + args, env=self._env,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                if timeout:
                    timer = threading.Timer(timeout, _kill)
                    timer.daemon = True
                    timer.start()

                stream = spec.stream_output

                def pump(src, sink, dst_file):
                    for chunk in iter(lambda: src.readline(), b""):
                        if stream:
                            try:
                                sink.buffer.write(chunk)
                                sink.flush()
                            except (ValueError, OSError):
                                pass
                        if dst_file:
                            dst_file.write(chunk)
                            dst_file.flush()

                t_out = threading.Thread(target=pump,
                                         args=(proc.stdout, sys.stdout, out_f))
                t_err = threading.Thread(target=pump,
                                         args=(proc.stderr, sys.stderr, err_f))
                t_out.start()
                t_err.start()
                exit_code = proc.wait()
                t_out.join(timeout=5)
                t_err.join(timeout=5)
                if out_f:
                    out_f.close()
                if err_f:
                    err_f.close()
        except KeyboardInterrupt:
            subprocess.run(["docker", "kill", container_name],
                           env=self._env, capture_output=True, text=True)
            exit_code = 130
        finally:
            if timer:
                timer.cancel()

        status = "timed_out" if timed_out["hit"] else (
            "completed" if exit_code == 0 else "failed")
        finished = time.time()

        if rec:
            rec.finish_container(exit_code, status)
            rec.finish(exit_code, status).save()

        # Ephemeral: --rm removes it. Sweep defensively in case of a crash.
        self._docker(["rm", "-f", container_name])

        return SandboxResult(
            spec.sandbox_id, exit_code, status, container=container_name,
            started_at=started, finished_at=finished, record=rec,
        )

    def list_containers(self):
        p = self._docker(["ps", "-a", "--filter",
                          f"label={config.LABEL_MANAGED}=true",
                          "--format", "{{.ID}}\t{{.Names}}\t{{.Status}}"])
        if p.returncode != 0:
            return []
        rows = []
        for line in p.stdout.strip().splitlines():
            if line.strip():
                parts = line.split("\t")
                rows.append({"id": parts[0], "name": parts[1],
                             "status": parts[2] if len(parts) > 2 else ""})
        return rows
