"""agent-sandbox: a local execution substrate for coding agents.

Library entry points, so another program (e.g. SSSF) can drive the substrate
without going through the CLI:

    from agent_sandbox import (SandboxSpec, LocalDockerBackend, ResourceConfig,
                               worktree, credentials, RunRecord)

    ws   = worktree.create("/path/to/repo")
    res  = ResourceConfig(cpus="4", memory="8g")
    rec  = RunRecord(ws.sandbox_id)
    spec = SandboxSpec(ws.sandbox_id, ws, ["pytest"], resources=res, record=rec)
    result = LocalDockerBackend().run(spec)
"""

__version__ = "1.1.0"

from .backend import SandboxBackend, SandboxResult, SandboxSpec
from .docker_backend import LocalDockerBackend
from .metadata import RunRecord
from .resources import ResourceConfig
from .mounts import Mount
from . import (agent_home, cleanup, config, credentials, doctor, gitdir,
               image, mounts, worktree)

__all__ = [
    "SandboxBackend", "SandboxSpec", "SandboxResult", "LocalDockerBackend",
    "ResourceConfig", "RunRecord", "Mount", "worktree", "credentials",
    "config", "image", "doctor", "cleanup", "agent_home", "mounts", "gitdir",
    "__version__",
]
