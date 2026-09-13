"""image: the build guard (R10, KTD7). One helper both build paths call;
`ensure` consults it before a fingerprint-triggered rebuild. The container
list is injected, so no Docker is touched."""

import json

import pytest

from agent_sandbox import config, image
from agent_sandbox.errors import ImageError

RUNNING = {"id": "abc123", "name": "agent-sandbox-vibeset-dj-0439a7a7", "status": "Up 3 hours"}
EXITED = {"id": "def456", "name": "agent-sandbox-old-1111", "status": "Exited (0) 2 days ago"}


def listing(*rows):
    return lambda: list(rows)


def never_listed():
    raise AssertionError("list_containers consulted when no build was needed")


# ---------------------------------------------------------------- the guard
def test_guard_raises_naming_the_running_container():
    with pytest.raises(ImageError) as ei:
        image.refuse_build_while_running(listing(RUNNING), force=False)
    assert RUNNING["name"] in ei.value.message
    assert "--force-build" in ei.value.remedy and "idle" in ei.value.remedy


def test_guard_passes_with_force():
    image.refuse_build_while_running(listing(RUNNING), force=True)


def test_guard_passes_with_no_containers():
    image.refuse_build_while_running(listing(), force=False)


def test_guard_ignores_exited_containers():
    image.refuse_build_while_running(listing(EXITED), force=False)


def test_guard_names_every_running_container():
    other = dict(RUNNING, id="999", name="agent-sandbox-other-2222")
    with pytest.raises(ImageError) as ei:
        image.refuse_build_while_running(listing(RUNNING, EXITED, other))
    assert RUNNING["name"] in ei.value.message and other["name"] in ei.value.message
    assert EXITED["name"] not in ei.value.message


# ---------------------------------------------------------------- ensure
@pytest.fixture
def state(tmp_path, monkeypatch):
    """The image exists; its recorded fingerprint is whatever the test writes."""
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(config, "RUNS", runs)
    monkeypatch.setattr(config, "IMAGE_STATE", runs / ".image-state.json")
    monkeypatch.setattr(image, "exists", lambda image=None: True)
    monkeypatch.setattr(image, "fingerprint", lambda: "fp-current")

    def write(fp):
        (runs / ".image-state.json").write_text(json.dumps({config.IMAGE_NAME: fp}))
    return write


@pytest.fixture
def builds(monkeypatch):
    calls = []
    monkeypatch.setattr(image, "build", lambda *a, **k: calls.append((a, k)) or True)
    return calls


def test_ensure_stale_fingerprint_refuses_before_any_build(state, builds):
    state("fp-old")
    with pytest.raises(ImageError) as ei:
        image.ensure(quiet=True, list_containers=listing(RUNNING))
    assert RUNNING["name"] in ei.value.message
    assert builds == []


def test_ensure_stale_fingerprint_builds_when_idle(state, builds):
    state("fp-old")
    assert image.ensure(quiet=True, list_containers=listing()) == config.IMAGE_NAME
    assert len(builds) == 1


def test_ensure_stale_fingerprint_builds_when_forced(state, builds):
    state("fp-old")
    image.ensure(quiet=True, list_containers=listing(RUNNING), force=True)
    assert len(builds) == 1


def test_ensure_fresh_fingerprint_neither_lists_nor_builds(state, builds):
    state("fp-current")
    assert image.ensure(quiet=True, list_containers=never_listed) == config.IMAGE_NAME
    assert builds == []
