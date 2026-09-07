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

    def __init__(self, cpus=None, memory=None, pids=None, timeout=None, cfg=None):
        cfg = config.load_config() if cfg is None else cfg
        self.cpus = str(config.resolve("cpus", cpus, cfg))
        self.memory = str(config.resolve("memory", memory, cfg))
        self.pids = int(config.resolve("pids", pids, cfg))
        self.timeout_raw = str(config.resolve("timeout", timeout, cfg))
        self.timeout = parse_duration(self.timeout_raw)
        # Validate eagerly so a bad value fails before a container is created.
        float(self.cpus)
        self.memory_bytes = parse_memory(self.memory)

    def docker_args(self):
        return [
            "--cpus", self.cpus,
            "--memory", self.memory,
            "--pids-limit", str(self.pids),
        ]

    def to_dict(self):
        return {
            "cpus": self.cpus,
            "memory": self.memory,
            "memory_bytes": self.memory_bytes,
            "pids": self.pids,
            "timeout": self.timeout_raw,
            "timeout_seconds": self.timeout,
        }

    def __repr__(self):
        return (f"ResourceConfig(cpus={self.cpus}, memory={self.memory}, "
                f"pids={self.pids}, timeout={self.timeout_raw})")
