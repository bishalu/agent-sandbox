"""The backend's admission window: what `acquire` hands over must be
released on every exit path, including a record write that fails between
the decision and the container (KTD1, KTD2).

No Docker: preflight, argv construction and the admission decision are all
stubbed; the run never reaches subprocess.
"""

import pytest

from agent_sandbox import admission, config, docker_backend, resources
from agent_sandbox.backend import SandboxSpec
from agent_sandbox.metadata import RunRecord


class _WS:
    def __init__(self, repo="/repo/x"):
        self.repo = repo
        self.path = repo


class FakeHandle:
    def __init__(self):
        self.released = 0

    def release(self):
        self.released += 1

    def release_when_visible(self, *a, **kw):
        raise AssertionError("must not reach the container launch")


class BrokenRecord(RunRecord):
    def save(self):
        raise OSError("disk full")


@pytest.fixture
def home(home, monkeypatch):
    """The shared home, with docker pointed nowhere."""
    monkeypatch.setattr(config, "docker_env", lambda: {"DOCKER_HOST": "unix:///nowhere"})
    return home


def test_failed_record_save_after_admission_releases_the_handle(home, monkeypatch):
    handle = FakeHandle()
    monkeypatch.setattr(admission, "acquire", lambda *a, **kw: handle)
    backend = docker_backend.LocalDockerBackend()
    monkeypatch.setattr(backend, "preflight", lambda: True)
    monkeypatch.setattr(backend, "build_args", lambda spec, name: ["run", name])

    rec = BrokenRecord("x-1")
    res = resources.ResourceConfig("1", "8g", "256", "5m", {})
    spec = SandboxSpec("x-1", _WS(), ["true"], resources=res, record=rec)

    with pytest.raises(OSError, match="disk full"):
        backend.run(spec)
    assert handle.released == 1
