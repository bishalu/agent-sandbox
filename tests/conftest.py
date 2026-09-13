"""Shared pytest setup.

The repo ships no packaging (KTD9), so the package is imported from the
checkout itself: put the repo root first on sys.path before any test runs.
"""

import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_sandbox import config  # noqa: E402  (needs the sys.path shim above)


@pytest.fixture
def clean_env(monkeypatch):
    """No AGENT_SANDBOX_* override from the host leaks into a test."""
    for k in list(os.environ):
        if k.startswith("AGENT_SANDBOX_"):
            monkeypatch.delenv(k)


@pytest.fixture
def home(tmp_path, monkeypatch, clean_env):
    """An empty agent-sandbox home under tmp_path: runs/, its lock dir, the
    config file and ROOT all point there. Modules layer extra setup on top
    by overriding `home` and requesting this one."""
    monkeypatch.setattr(config, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "runs" / ".locks")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config, "ROOT", tmp_path)
    return tmp_path
