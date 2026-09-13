"""resources: thread caps derived from the CPU limit (R10, KTD6), and their
route into every container's env through mounts.plan_for and the doctor's
probe-output check. Pure: no Docker, no git."""

import os

import pytest

from agent_sandbox import config, doctor, mounts, resources

THREAD_VARS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
               "MKL_NUM_THREADS", "AGENT_SANDBOX_THREADS")


@pytest.fixture
def clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("AGENT_SANDBOX_"):
            monkeypatch.delenv(k)


@pytest.fixture
def cfg(tmp_path, monkeypatch, clean_env):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    return {}


def res(cpus, cfg):
    return resources.ResourceConfig(cpus=cpus, memory="1g", pids=64, timeout="1h", cfg=cfg)


# ---------------------------------------------------------------- thread_env
@pytest.mark.parametrize("cpus, expected", [
    ("6", "4"),      # capped at four
    ("2", "2"),      # below the cap: the limit itself
    ("1.5", "1"),    # fractional cpus floor to whole threads
    ("0.5", "1"),    # never zero
    (8, "4"),
])
def test_thread_env_derives_from_cpus(cfg, cpus, expected):
    env = res(cpus, cfg).thread_env()
    assert set(env) == set(THREAD_VARS)
    assert all(env[k] == expected for k in THREAD_VARS)


def test_thread_env_values_are_strings_for_docker(cfg):
    assert all(isinstance(v, str) for v in res("6", cfg).thread_env().values())


# ---------------------------------------------------------------- plan_for
class _CopyWorkspace:
    """A copy-kind workspace: no repository, so no git identity and no git mounts."""
    kind = "copy"
    repo = None
    branch = None


def test_plan_for_merges_thread_env_from_given_resources(cfg):
    plan = mounts.plan_for(_CopyWorkspace(), cfg, None, resources=res("2", cfg))
    assert {k: plan.env[k] for k in THREAD_VARS} == dict.fromkeys(THREAD_VARS, "2")


def test_plan_for_derives_resources_from_config_when_not_given(cfg):
    cfg = {"cpus": 3}
    plan = mounts.plan_for(_CopyWorkspace(), cfg, None)
    assert {k: plan.env[k] for k in THREAD_VARS} == dict.fromkeys(THREAD_VARS, "3")


def test_plan_for_thread_env_does_not_displace_other_env(cfg, monkeypatch):
    monkeypatch.setattr(mounts, "git_identity_env",
                        lambda ws: {"GIT_AUTHOR_NAME": "n", "GIT_AUTHOR_EMAIL": "e",
                                    "GIT_COMMITTER_NAME": "n", "GIT_COMMITTER_EMAIL": "e"})
    plan = mounts.plan_for(_CopyWorkspace(), cfg, None, resources=res("6", cfg))
    assert plan.env["GIT_AUTHOR_NAME"] == "n" and plan.env["OMP_NUM_THREADS"] == "4"


# ---------------------------------------------------------------- doctor check
def test_doctor_thread_env_check_passes_on_matching_probe_output(cfg):
    expected = res("1", cfg).thread_env()
    out = "COMMIT_OK\nTHREADS=1,1,1,1\nHOOK_RO\n"
    c = doctor.thread_env_check(out, expected)
    assert c.status == doctor.PASS and c.remedy == ""


def test_doctor_thread_env_check_fails_when_a_variable_is_unset(cfg):
    expected = res("1", cfg).thread_env()
    c = doctor.thread_env_check("THREADS=1,,1,1\n", expected)
    assert c.status == doctor.FAIL and "OPENBLAS_NUM_THREADS" in c.detail


def test_doctor_thread_env_check_fails_when_marker_missing(cfg):
    c = doctor.thread_env_check("COMMIT_OK\n", res("1", cfg).thread_env())
    assert c.status == doctor.FAIL


def test_doctor_thread_env_check_warns_when_probe_not_run(cfg):
    c = doctor.thread_env_check(None, res("1", cfg).thread_env())
    assert c.status == doctor.WARN and "probe not run" in c.detail
