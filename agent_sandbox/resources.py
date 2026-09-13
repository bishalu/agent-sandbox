"""Resource configuration: parsing, defaults, and docker argv rendering.

Defaults per spec R-03: cpus 8, memory 16g, pids 2048, timeout 12h.
Deliberately generous — this substrate exists because remote dev boxes were
adding wall-clock overhead, so it must not behave like a tiny CI container.
"""

import re

from . import config
from .errors import SandboxError

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.I)
_MEMORY = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)b?\s*$", re.I)

_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "": 3600}


def parse_duration(value):
    """'12h' -> 43200. Bare numbers are hours, matching how timeouts are discussed."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value * 3600)
    m = _DURATION.match(str(value))
    if not m:
        raise SandboxError(
            f"could not parse duration: {value!r}",
            "Use forms like 30s, 45m, 12h, 2d.",
        )
    return int(float(m.group(1)) * _SECONDS[m.group(2).lower()])


def parse_memory(value):
    """'16g' -> bytes. Returned normalized for docker (`16g`) and for checks."""
    m = _MEMORY.match(str(value))
    if not m:
        raise SandboxError(
            f"could not parse memory: {value!r}",
            "Use forms like 512m, 8g, 16g.",
        )
    scale = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}
    return int(float(m.group(1)) * scale[m.group(2).lower()])


class ResourceConfig:
    """Resolved limits for one run."""

    # Every managed container runs under this user slice when admission is on,
    # so the slice's MemoryMax holds the budget continuously (KTD8).
    SLICE = "agent-sandbox.slice"

    def __init__(self, cpus=None, memory=None, pids=None, timeout=None, cfg=None,
                 memory_swap=None):
        cfg = config.load_config() if cfg is None else cfg
        self.cpus = str(config.resolve("cpus", cpus, cfg))
        self.memory, self.memory_source = config.resolve_source("memory", memory, cfg)
        self.memory = str(self.memory)
        self.pids = int(config.resolve("pids", pids, cfg))
        self.timeout_raw = str(config.resolve("timeout", timeout, cfg))
        self.timeout = parse_duration(self.timeout_raw)
        # Swap allowance defaults to the memory limit itself: no swap (KTD8).
        swap = config.resolve("memory_swap", memory_swap, cfg)
        self.memory_swap = str(swap) if swap is not None else self.memory
        self.cgroup_parent = (self.SLICE
                              if config.as_bool(config.resolve("admission_enabled", None, cfg))
                              else None)
        # Validate eagerly so a bad value fails before a container is created.
        float(self.cpus)
        self.memory_bytes = parse_memory(self.memory)
        if self.memory_swap != "-1":
            parse_memory(self.memory_swap)

    # Compute libraries default to one thread per host core, which inside a
    # --cpus limited container means N threads fighting over a fraction of a
    # core each. Four is where the libraries stop scaling on this workload
    # anyway (KTD6).
    THREAD_CAP = 4
    THREAD_VARS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                   "MKL_NUM_THREADS", "AGENT_SANDBOX_THREADS")

    def thread_env(self):
        """Thread caps for compute libraries, derived from the CPU limit (R10).

        `min(cpus, 4)`, floored to whole threads and never below one. The
        three standard variables cover OpenMP, OpenBLAS and MKL;
        `AGENT_SANDBOX_THREADS` is the documented handle for libraries that
        read no standard variable (onnxruntime), so a project sets its own
        thread count from it. `AGENT_SANDBOX_CPUS` keeps its existing meaning
        as the host-side CPU override (SPEC R-03).
        """
        n = max(1, min(int(float(self.cpus)), self.THREAD_CAP))
        return {k: str(n) for k in self.THREAD_VARS}

    def docker_args(self):
        args = [
            "--cpus", self.cpus,
            "--memory", self.memory,
            "--memory-swap", self.memory_swap,
            "--pids-limit", str(self.pids),
        ]
        if self.cgroup_parent:
            args.append(f"--cgroup-parent={self.cgroup_parent}")
        return args

    def to_dict(self):
        return {
            "cpus": self.cpus,
            "memory": self.memory,
            "memory_bytes": self.memory_bytes,
            "memory_swap": self.memory_swap,
            "cgroup_parent": self.cgroup_parent,
            "pids": self.pids,
            "timeout": self.timeout_raw,
            "timeout_seconds": self.timeout,
        }

    def __repr__(self):
        return (f"ResourceConfig(cpus={self.cpus}, memory={self.memory}, "
                f"pids={self.pids}, timeout={self.timeout_raw})")
