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
    pass


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
