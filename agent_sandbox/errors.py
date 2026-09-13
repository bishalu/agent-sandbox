"""Typed errors that carry actionable remediation text.

Every error a user can hit should say what to do about it, not just what
went wrong. `SandboxError.remedy` is printed by the CLI under the message.
"""


class SandboxError(Exception):
    """Base error. `remedy` is an actionable next step shown to the user."""

    def __init__(self, message, remedy=None):
        super().__init__(message)
        self.message = message
        self.remedy = remedy

    def __str__(self):
        return self.message


class DockerUnavailable(SandboxError):
    pass


class RootfulDockerRefused(SandboxError):
    """We found a rootful daemon. Refusing per spec R-02."""


class ImageError(SandboxError):
    """The base image cannot be built or found: a missing Dockerfile, a failed
    `docker build`, or a build refused while a managed container runs (R10);
    the remedy for that last one is to build when idle or pass --force-build."""


class RepoError(SandboxError):
    pass


class WorktreeError(SandboxError):
    pass


class LockHeld(SandboxError):
    pass


class NotImplementedYet(SandboxError):
    """Recognized option that is deliberately not implemented (e.g. network=restricted)."""


class SandboxNotFound(SandboxError):
    pass


class CredentialError(SandboxError):
    pass


class MountError(SandboxError):
    """A declared bind mount cannot be honored: missing source, bad config,
    or an unresolvable git identity. One class for every mount failure; the
    message and remedy carry the specifics."""


class AdmissionRefused(SandboxError):
    """Admission said no and waiting would not help: the request exceeds the
    whole budget, the memory log is stale, or the caller asked not to wait.
    Carries the `Decision` so the CLI can emit its numbers as JSON."""

    kind = "refused"

    def __init__(self, decision, remedy=None):
        self.decision = decision
        self.reasons = list(decision.reasons)
        super().__init__(
            "admission refused: " + "; ".join(self.reasons),
            remedy or ("Lower --memory, stop a running container, or refresh the "
                       "memory log (`agent-sandbox memlog show`). `--force` bypasses "
                       "admission; `agent-sandbox admission show` prints the numbers."))


class AdmissionTimeout(SandboxError):
    """Waited the configured time for headroom, the floor or the project lock
    and never got it. Carries the last `Decision`."""

    kind = "timeout"

    def __init__(self, decision, waited_s, remedy=None):
        self.decision = decision
        self.reasons = list(decision.reasons)
        self.waited_s = waited_s
        super().__init__(
            f"admission timed out after {int(waited_s)} s: " + "; ".join(self.reasons),
            remedy or ("Wait for the running containers to finish, raise "
                       "admission_wait_timeout_s, or lower --memory. "
                       "`agent-sandbox admission show` prints the live numbers."))
