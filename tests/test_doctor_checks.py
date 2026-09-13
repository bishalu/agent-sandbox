"""doctor (U7, KTD9): the five admission-era checks are pure functions over
injected readings, so they run without Docker, systemd or a cgroup tree.

    admission config      the admission_* keys parse and cohere; the recorded
                          slice cap matches the budget
    stale running entries no run.json entry says running for a container
                          docker cannot find
    project locks         no lock under LOCK_DIR is held by a dead pid
    slice memory.max      agent-sandbox.slice carries MemoryMax=budget
    slice cgroup parent   the probe container ran inside the slice

The sandbox probe must bypass admission with the same `force` path a
`--force` run takes, so a stale memory log or a full disk shows up as its
own row and not as a failed probe.
"""

import os
import pathlib
import shutil
import subprocess
import sys

import pytest

from agent_sandbox import admission, config, doctor
from agent_sandbox.metadata import RunRecord

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "runs"
G = 1024 ** 3
SLICE = "agent-sandbox.slice"


@pytest.fixture
def clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("AGENT_SANDBOX_"):
            monkeypatch.delenv(k)


@pytest.fixture
def home(tmp_path, monkeypatch, clean_env):
    """A runs/ dir with copies of the two real 2026-09-13 records."""
    runs = tmp_path / "runs"
    shutil.copytree(FIXTURES, runs)
    monkeypatch.setattr(config, "RUNS", runs)
    monkeypatch.setattr(config, "LOCK_DIR", runs / ".locks")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config, "ROOT", tmp_path)
    return runs


def cfg(**more):
    c = {"admission_enabled": True, "admission_memory_budget": "16g",
         "admission_mem_floor_gb": 8, "admission_disk_path": "/mnt/c"}
    c.update(more)
    return c


def settings(**more):
    return admission.resolve_settings(cfg(**more))


def slice_state(memory_max=16 * G):
    return {"slice": SLICE, "memory_max": memory_max, "ok": True, "output": "",
            "command": f"systemctl --user set-property {SLICE} MemoryMax={memory_max}",
            "set_at": "2026-09-13T22:18:26+00:00"}


def _dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


# ---------------------------------------------------------------- admission config
def test_admission_config_disabled_passes_and_says_so(clean_env):
    c = doctor.admission_config_check(cfg(admission_enabled=False), slice_state=None)
    assert c.status == doctor.PASS
    assert "disabled" in c.detail and c.remedy == ""


def test_admission_config_enabled_with_matching_slice_state_passes(clean_env):
    c = doctor.admission_config_check(cfg(), slice_state=slice_state())
    assert c.status == doctor.PASS
    assert "budget=16g" in c.detail and "floor=8g" in c.detail


def test_admission_config_unparseable_budget_fails(clean_env):
    c = doctor.admission_config_check(cfg(admission_memory_budget="sixteen"), slice_state=None)
    assert c.status == doctor.FAIL
    assert "admission_memory_budget" in c.detail or "sixteen" in c.detail
    assert "config.json" in c.remedy


def test_admission_config_negative_floor_fails(clean_env):
    c = doctor.admission_config_check(cfg(admission_mem_floor_gb=-1), slice_state=slice_state())
    assert c.status == doctor.FAIL and "floor" in c.detail


def test_admission_config_negative_disk_floor_fails(clean_env):
    c = doctor.admission_config_check(cfg(admission_disk_floor_gb=-5), slice_state=slice_state())
    assert c.status == doctor.FAIL and "disk floor" in c.detail


def test_admission_config_interval_not_below_timeout_fails(clean_env):
    c = doctor.admission_config_check(cfg(admission_wait_timeout_s=30, admission_wait_interval_s=30),
                                      slice_state=slice_state())
    assert c.status == doctor.FAIL
    assert "interval" in c.detail and "timeout" in c.detail


def test_admission_config_slice_state_mismatch_fails_with_install_remedy(clean_env):
    c = doctor.admission_config_check(cfg(), slice_state=slice_state(memory_max=32 * G))
    assert c.status == doctor.FAIL
    assert "32g" in c.detail and "16g" in c.detail
    assert "agent-sandbox admission install" in c.remedy


def test_admission_config_enabled_without_recorded_slice_state_warns(clean_env):
    c = doctor.admission_config_check(cfg(), slice_state=None)
    assert c.status == doctor.WARN
    assert "agent-sandbox admission install" in c.remedy


def test_admission_config_ignores_slice_state_when_disabled(clean_env):
    c = doctor.admission_config_check(cfg(admission_enabled=False),
                                      slice_state=slice_state(memory_max=1))
    assert c.status == doctor.PASS


# ---------------------------------------------------------------- stale running entries
def probes(names=(), dead_pids=()):
    return dict(container_exists=lambda n: n in names,
                pid_alive=lambda p: None if p is None else p not in dead_pids)


def test_stale_running_fixture_fails_naming_ids_and_the_reconcile_remedy(home):
    c = doctor.stale_running_check(RunRecord.all(), **probes())
    assert c.status == doctor.FAIL
    assert "vibeset-dj-ecfa0e5c" in c.detail and "vibeset-dj-0439a7a7" in c.detail
    # the record's stale entry count, so the reader knows the size of the fix
    assert "6" in c.detail and "1" in c.detail
    assert "agent-sandbox status --reconcile" in c.remedy


def test_stale_running_passes_when_every_running_container_exists(home):
    rec = RunRecord.load("vibeset-dj-ecfa0e5c")
    live = rec.data["containers"][-1]["container"]
    # the other record's stale history is corrected by hand first
    other = RunRecord.load("vibeset-dj-0439a7a7")
    for e in other.data["containers"]:
        if e["status"] == "running":
            e["status"] = "crashed"
    c = doctor.stale_running_check([rec, other], **probes(names={live}))
    assert c.status == doctor.PASS and c.remedy == ""
    assert "2 records" in c.detail


def test_stale_running_with_no_records_passes():
    c = doctor.stale_running_check([], **probes())
    assert c.status == doctor.PASS


def test_stale_running_only_orphaned_entries_warns(home):
    rec = RunRecord("x-1")
    rec.add_container("agent-sandbox-x-1-1-99", "runc", ["bash"], pid=99)
    rec.start()
    c = doctor.stale_running_check([rec], **probes(names={"agent-sandbox-x-1-1-99"},
                                                   dead_pids={99}))
    assert c.status == doctor.WARN
    assert "orphaned" in c.detail and "x-1" in c.detail


# ---------------------------------------------------------------- project locks
def test_project_locks_dead_pid_fails_naming_the_file_and_pid(tmp_path):
    lock_dir = tmp_path / ".locks"
    lock_dir.mkdir()
    dead = _dead_pid()
    path = admission.project_lock_path("/repo/x", lock_dir)
    path.write_text(f"{dead} milestone 6 /repo/x\n")
    c = doctor.project_locks_check(lock_dir)
    assert c.status == doctor.FAIL
    assert path.name in c.detail and str(dead) in c.detail
    assert "reclaim" in c.remedy and str(path) in c.remedy


def test_project_locks_live_holder_passes_and_names_it(tmp_path):
    lock_dir = tmp_path / ".locks"
    lock_dir.mkdir()
    (lock_dir / "milestone-abc.lock").write_text(f"{os.getppid()} milestone 6 /repo/x\n")
    c = doctor.project_locks_check(lock_dir)
    assert c.status == doctor.PASS
    assert str(os.getppid()) in c.detail and "milestone 6" in c.detail


def test_project_locks_ignore_the_admission_flock_file(tmp_path):
    lock_dir = tmp_path / ".locks"
    lock_dir.mkdir()
    (lock_dir / admission.ADMISSION_LOCK).write_text("")     # a flock, no pid inside
    c = doctor.project_locks_check(lock_dir)
    assert c.status == doctor.PASS and "no lock files" in c.detail


def test_project_locks_garbage_file_warns(tmp_path):
    lock_dir = tmp_path / ".locks"
    lock_dir.mkdir()
    (lock_dir / "milestone-zzz.lock").write_text("not a pid\n")
    c = doctor.project_locks_check(lock_dir)
    assert c.status == doctor.WARN and "unreadable" in c.detail


def test_project_locks_missing_dir_passes(tmp_path):
    c = doctor.project_locks_check(tmp_path / "absent")
    assert c.status == doctor.PASS


def test_project_locks_pid_probe_is_injected(tmp_path):
    lock_dir = tmp_path / ".locks"
    lock_dir.mkdir()
    (lock_dir / "milestone-abc.lock").write_text("424242 note\n")
    assert doctor.project_locks_check(lock_dir, pid_alive=lambda p: True).status == doctor.PASS
    assert doctor.project_locks_check(lock_dir, pid_alive=lambda p: False).status == doctor.FAIL


# ---------------------------------------------------------------- slice memory.max
def test_slice_memory_max_matching_passes(clean_env):
    c = doctor.slice_memory_max_check(settings(), f"MemoryMax={16 * G}\n", cgroup_exists=True)
    assert c.status == doctor.PASS and "16g" in c.detail


def test_slice_memory_max_matching_passes_before_anything_ran_in_the_slice(clean_env):
    c = doctor.slice_memory_max_check(settings(), f"MemoryMax={16 * G}\n", cgroup_exists=False)
    assert c.status == doctor.PASS


def test_slice_memory_max_mismatch_fails_with_install_remedy(clean_env):
    c = doctor.slice_memory_max_check(settings(), f"MemoryMax={32 * G}\n", cgroup_exists=True)
    assert c.status == doctor.FAIL
    assert "32g" in c.detail and "16g" in c.detail
    assert "agent-sandbox admission install" in c.remedy


def test_slice_memory_max_infinity_fails(clean_env):
    c = doctor.slice_memory_max_check(settings(), "MemoryMax=infinity\n", cgroup_exists=True)
    assert c.status == doctor.FAIL and "infinity" in c.detail


def test_slice_memory_max_unset_and_slice_never_started_warns(clean_env):
    c = doctor.slice_memory_max_check(settings(), "MemoryMax=infinity\n", cgroup_exists=False)
    assert c.status == doctor.WARN
    assert "nothing has run" in c.detail
    assert "agent-sandbox admission install" in c.remedy


def test_slice_memory_max_disabled_passes(clean_env):
    c = doctor.slice_memory_max_check(settings(admission_enabled=False), None, cgroup_exists=False)
    assert c.status == doctor.PASS and "disabled" in c.detail


def test_slice_memory_max_unreadable_systemctl_warns(clean_env):
    c = doctor.slice_memory_max_check(settings(), None, cgroup_exists=True)
    assert c.status == doctor.WARN and "systemctl" in c.detail


def test_parse_memory_max_line():
    assert doctor.parse_memory_max(f"MemoryMax={16 * G}\n") == 16 * G
    assert doctor.parse_memory_max("MemoryMax=infinity") is None
    assert doctor.parse_memory_max("") is None
    assert doctor.parse_memory_max(None) is None


# ---------------------------------------------------------------- slice cgroup parent
IN_SLICE = ("0::/user.slice/user-1000.slice/user@1000.service/agent-sandbox.slice/"
            "docker-abc123.scope")
OUTSIDE = "0::/user.slice/user-1000.slice/user@1000.service/user.slice/docker-abc123.scope"


def out(cgroup):
    return f"COMMIT_OK\nCGROUP={cgroup}\nTHREADS=4,4,4,4\n"


def test_slice_cgroup_probe_not_run_warns(clean_env):
    c = doctor.slice_cgroup_check(None, settings())
    assert c.status == doctor.WARN and "probe not run" in c.detail


def test_slice_cgroup_disabled_passes(clean_env):
    c = doctor.slice_cgroup_check(out(OUTSIDE), settings(admission_enabled=False))
    assert c.status == doctor.PASS and "disabled" in c.detail


def test_slice_cgroup_path_in_the_slice_passes(clean_env):
    c = doctor.slice_cgroup_check(out(IN_SLICE), settings())
    assert c.status == doctor.PASS and SLICE in c.detail


def test_slice_cgroup_path_outside_the_slice_fails_with_the_ktd8_fallback(clean_env):
    c = doctor.slice_cgroup_check(out(OUTSIDE), settings())
    assert c.status == doctor.FAIL
    assert OUTSIDE in c.detail
    assert "MemoryMax" in c.remedy and "user@" in c.remedy and "owner" in c.remedy


def test_slice_cgroup_no_line_fails(clean_env):
    c = doctor.slice_cgroup_check("COMMIT_OK\n", settings())
    assert c.status == doctor.FAIL and "CGROUP" in c.detail
    assert "MemoryMax" in c.remedy


def test_slice_cgroup_private_namespace_falls_back_to_the_host_reading(clean_env):
    # With a private cgroup namespace the container sees `0::/`, so the
    # slice's existence on the host right after the probe decides.
    c = doctor.slice_cgroup_check(out("0::/"), settings(), slice_cgroup_exists=True)
    assert c.status == doctor.PASS and "namespace" in c.detail
    c = doctor.slice_cgroup_check(out("0::/"), settings(), slice_cgroup_exists=False)
    assert c.status == doctor.FAIL and "MemoryMax" in c.remedy


def test_slice_cgroup_dir_is_under_the_user_manager():
    p = doctor.slice_cgroup_dir(uid=1000)
    assert str(p) == "/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/agent-sandbox.slice"


# ---------------------------------------------------------------- the probe's backend
def test_probe_backend_forces_admission():
    # KTD9: a stale memory log or a full disk must report as its own row,
    # never as a failed probe.
    b = doctor.probe_backend()
    assert b.force_admission is True


def test_probe_script_echoes_its_cgroup_path():
    assert "CGROUP=" in doctor.PROBE_SCRIPT and "/proc/self/cgroup" in doctor.PROBE_SCRIPT
