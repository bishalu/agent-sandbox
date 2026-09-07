"""Backend interface (spec R-12).

The CLI is one caller. SSSF will be another, calling this directly without
going through argparse. Keeping the contract here — spec in, result out —
is what makes that possible later without a rewrite.

Only LocalDockerBackend is implemented. ExeBackend and MicroVMBackend are
deliberately NOT built.
"""

import abc


class SandboxSpec:
    """Everything needed to execute one sandbox run."""

    def __init__(self, sandbox_id, workspace, command=None, resources=None,
                 mode="safe", network="full", image=None, credentials=None,
                 read_only_root=False, experimental_gvisor=False,
                 interactive=None, env=None, record=None,
                 stream_output=True, mounts=None):
        self.sandbox_id = sandbox_id
        self.workspace = workspace          # Workspace object
        self.command = command or []        # [] means interactive shell
        self.resources = resources
        self.mode = mode
        self.network = network
        self.image = image
        self.credentials = credentials
        self.read_only_root = read_only_root
        self.experimental_gvisor = experimental_gvisor
        self.interactive = (not command) if interactive is None else interactive
        self.env = env or {}
        self.record = record                # RunRecord, for logs/metadata
        # Non-credential bind mounts (agent home, skills, git common dir): a
        # list of mounts.Mount, rendered with --mount so a missing host path
        # fails loudly (R-16).
        self.mounts = list(mounts or [])
        # False in --json mode: container output goes to the run logs only, so
        # it cannot interleave with the structured result on stdout.
        self.stream_output = stream_output


class SandboxResult:
    """Outcome of one run."""

    def __init__(self, sandbox_id, exit_code, status, container=None,
                 started_at=None, finished_at=None, record=None):
        self.sandbox_id = sandbox_id
        self.exit_code = exit_code
        self.status = status                # completed | failed | timed_out
        self.container = container
        self.started_at = started_at
        self.finished_at = finished_at
        self.record = record

    def to_dict(self):
        if self.record is not None:
            return self.record.public()
        return {
            "sandbox_id": self.sandbox_id,
            "exit_code": self.exit_code,
            "status": self.status,
            "container": self.container,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class SandboxBackend(abc.ABC):
    """Execution substrate. Implementations own container/VM lifecycle only.

    Worktree lifecycle, credentials, resources and metadata are separate
    concerns handled by their own modules and passed in via SandboxSpec.
    """

    name = "abstract"

    @abc.abstractmethod
    def preflight(self):
        """Raise if the backend cannot run here. Cheap, no side effects."""

    @abc.abstractmethod
    def run(self, spec):
        """Execute one sandbox to completion. Returns SandboxResult."""

    @abc.abstractmethod
    def list_containers(self):
        """Containers this backend currently manages."""
