"""admission: the pure decision, the committed-memory rule, the locked
acquire loop, and the config, CLI and record surfaces around it.

Every reading is injected (KTD9): no Docker, no /proc, no real clock.
"""

import json
import os
import subprocess
import sys
import threading
import time

import pytest

from agent_sandbox import admission, cli, config, memlog, resources
from agent_sandbox.backend import SandboxSpec
from agent_sandbox.errors import AdmissionRefused, AdmissionTimeout, SandboxError
from agent_sandbox.metadata import RunRecord

G = 1024 ** 3
BOOT = "5e8933b9-ec47-4a27-872d-e9a1c365ce3a"


def sample(containers=None, avail=40 * G):
    return memlog.Sample(boot_id=BOOT, monotonic=1000.0, time="2026-09-13T16:00:00+00:00",
                        mem_available=avail, swap_free=16 * G, containers=containers or {})


def fresh(containers=None):
    return memlog.Freshness(True, "fresh", 30.0, sample(containers))


def stale(reason="last sample is 400 s old, over the 180 s maximum"):
    return memlog.Freshness(False, reason, 400.0, sample())


def settings(enabled=True, budget="16g", floor=8, timeout=1800, interval=30, **more):
    cfg = {"admission_enabled": enabled, "admission_memory_budget": budget,
           "admission_mem_floor_gb": floor, "admission_wait_timeout_s": timeout,
           "admission_wait_interval_s": interval}
    cfg.update(more)
    return admission.resolve_settings(cfg)


def row(name, limit):
    return {"name": name, "memory_limit": limit}


def roomy_disks(cfg=None):
    """Disk readings above the floor, so these tests exercise memory alone;
    the disk guard has its own tests in test_disk_guard.py (KTD11)."""
    return {cfg.disk_path if cfg is not None else "/mnt/c": 100 * G, "/": 40 * G}


# ---------------------------------------------------------------- decide
def test_disabled_admits_regardless_of_inputs():
    d = admission.decide(100 * G, 100 * G, 0, stale(), 4242, settings(enabled=False))
    assert d.verdict == "admit" and d.reasons == []


def test_fits_admits_with_sourced_numbers(clean_env):
    cfg = admission.resolve_settings({"admission_enabled": True,
                                      "admission_memory_budget": "16g"})
    d = admission.decide(8 * G, 8 * G, 30 * G, fresh(), None, cfg, request_source="flag")
    assert d.verdict == "admit" and d.reasons == []
    n = d.numbers
    assert n["budget"] == {"bytes": 16 * G, "source": "config"}
    assert n["floor"] == {"bytes": 8 * G, "source": "default"}
    assert n["request"] == {"bytes": 8 * G, "source": "flag"}
    assert n["committed"]["bytes"] == 8 * G and "live" in n["committed"]["source"]
    assert n["mem_available"]["bytes"] == 30 * G and "live" in n["mem_available"]["source"]
    assert n["headroom"]["bytes"] == 8 * G


def test_request_over_headroom_waits():
    d = admission.decide(12 * G, 8 * G, 30 * G, fresh(), None, settings())
    assert d.verdict == "wait"
    assert d.reasons == ["headroom 8g below request 12g"]


def test_request_over_budget_refuses_at_once():
    d = admission.decide(20 * G, 0, 30 * G, fresh(), None, settings())
    assert d.verdict == "refuse"
    assert len(d.reasons) == 1 and "20g" in d.reasons[0] and "16g" in d.reasons[0]


def test_floor_not_met_waits_and_names_the_floor():
    d = admission.decide(4 * G, 0, 6 * G, fresh(), None, settings(floor=8))
    assert d.verdict == "wait"
    assert any("floor" in r and "8g" in r and "6g" in r for r in d.reasons)


@pytest.mark.parametrize("f", [
    stale(),
    memlog.Freshness(False, "last sample is from another boot", 10.0, sample()),
    memlog.Freshness(False, "memory log missing at /nowhere/memory.log"),
])
def test_stale_memlog_refuses(f):
    d = admission.decide(1 * G, 0, 30 * G, f, None, settings())
    assert d.verdict == "refuse"
    assert any(r.startswith("memory log stale") for r in d.reasons)


def test_project_lock_held_waits_and_untagged_is_not_checked():
    held = admission.decide(1 * G, 0, 30 * G, fresh(), 4242, settings())
    assert held.verdict == "wait" and any("4242" in r for r in held.reasons)
    free = admission.decide(1 * G, 0, 30 * G, fresh(), False, settings())
    assert free.verdict == "admit"
    untagged = admission.decide(1 * G, 0, 30 * G, fresh(), None, settings())
    assert untagged.verdict == "admit" and "project_lock" not in untagged.numbers


# ---------------------------------------------------------------- committed_memory
def test_limited_container_commits_its_limit():
    assert admission.committed_memory([row("a", 8 * G)], sample({"a": 1 * G}), 16 * G) == 8 * G


def test_unlimited_container_commits_usage_plus_a_quarter():
    rows = [row("mcp", 0)]
    assert admission.committed_memory(rows, sample({"mcp": 2 * G}), 16 * G) == int(2.5 * G)


def test_unlimited_container_absent_from_sample_commits_whole_budget():
    rows = [row("mcp", 0)]
    assert admission.committed_memory(rows, sample({"other": 1}), 16 * G) == 16 * G
    assert admission.committed_memory(rows, None, 16 * G) == 16 * G


def test_parse_inspect_rows():
    text = "/agent-sandbox-x-1\t8589934592\n/mcp\t0\n\n"
    assert admission.parse_inspect(text) == [row("agent-sandbox-x-1", 8 * G), row("mcp", 0)]


# ---------------------------------------------------------------- acquire
class _WS:
    def __init__(self, repo):
        self.repo = repo
        self.path = repo


def _spec(memory="8g", tags=None, rec=None, repo="/repo/x"):
    res = resources.ResourceConfig("1", memory, "256", "5m", {})
    if rec is not None and tags:
        rec.update(tags=tags)
    return SandboxSpec("x-1", _WS(repo), ["true"], resources=res, record=rec)


class Clock:
    """A clock that only moves when someone sleeps."""
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def _acquire(spec, cfg, rows, mem=30 * G, freshness=None, clock=None, wait=True, **kw):
    clock = clock or Clock()
    return admission.acquire(
        spec, cfg, wait=wait, quiet=True,
        inspect_running=lambda: list(rows),
        read_mem_available=lambda: mem,
        read_memlog=lambda c: freshness or fresh(),
        read_disk_free=roomy_disks,
        now=clock.now, sleep=clock.sleep, **kw)


def test_disabled_acquire_returns_none_and_touches_nothing(home):
    h = _acquire(_spec(), settings(enabled=False), [])
    assert h is None
    assert not (home / "runs" / ".locks").exists()


def test_force_bypasses_a_refusal(home):
    h = _acquire(_spec("20g"), settings(), [], force=True)
    assert h is None


def test_admit_holds_the_flock_until_released(home):
    h = _acquire(_spec(), settings(), [])
    assert h.decision.verdict == "admit"
    other = admission.locks.FileFlock(config.LOCK_DIR / "admission.lock")
    assert other.acquire(blocking=False) is False
    h.release()
    assert other.acquire(blocking=False) is True
    other.release()


def test_over_budget_refuses_without_waiting(home):
    clock = Clock()
    rec = RunRecord("x-1")
    rec.add_container("c1", "runc", ["true"])
    with pytest.raises(AdmissionRefused) as e:
        _acquire(_spec("20g", rec=rec), settings(), [], clock=clock)
    assert clock.sleeps == []
    assert rec.data["containers"][-1]["status"] == "failed"
    assert rec.data["status"] == "failed"
    assert e.value.decision.numbers["request"]["bytes"] == 20 * G


def test_no_wait_turns_a_wait_into_a_refusal(home):
    with pytest.raises(AdmissionRefused) as e:
        _acquire(_spec("12g"), settings(), [row("a", 8 * G)], wait=False)
    assert "headroom" in e.value.message


def test_wait_timeout_fails_with_last_reasons_and_record_states(home):
    clock = Clock()
    rec = RunRecord("x-1")
    rec.add_container("c1", "runc", ["true"])
    seen = []

    def sleep(s):
        on_disk = json.loads(rec.file.read_text())["containers"][-1]
        seen.append((on_disk["status"], on_disk["waiting_since"]))
        clock.sleep(s)

    with pytest.raises(AdmissionTimeout) as e:
        admission.acquire(_spec("12g", rec=rec), settings(timeout=120, interval=30),
                          quiet=True, inspect_running=lambda: [row("a", 8 * G)],
                          read_mem_available=lambda: 30 * G, read_memlog=lambda c: fresh(),
                          read_disk_free=roomy_disks,
                          now=clock.now, sleep=sleep)
    assert seen and all(s == "waiting" and since for s, since in seen)
    assert clock.t >= 120
    assert "headroom 8g below request 12g" in e.value.reasons
    entry = json.loads(rec.file.read_text())["containers"][-1]
    assert entry["status"] == "failed"
    assert json.loads(rec.file.read_text())["status"] == "failed"


def test_wait_then_admit_when_headroom_appears(home):
    clock = Clock()
    rows = [row("a", 8 * G)]

    def inspect_running():
        return list(rows) if clock.t < 60 else []

    h = admission.acquire(_spec("12g"), settings(interval=30), quiet=True,
                          inspect_running=inspect_running,
                          read_mem_available=lambda: 30 * G, read_memlog=lambda c: fresh(),
                          read_disk_free=roomy_disks,
                          now=clock.now, sleep=clock.sleep)
    assert h.decision.verdict == "admit" and clock.sleeps == [30, 30]
    h.release()


def test_milestone_tag_takes_the_project_lock(home):
    rec = RunRecord("x-1")
    h = _acquire(_spec(tags={"milestone": "6", "unit": "u"}, rec=rec), settings(), [])
    path = admission.project_lock_path("/repo/x")
    assert path.exists() and path.read_text().split()[0] == str(os.getpid())
    assert h.decision.numbers["project_lock"]["held"] is False
    h.release()
    assert not path.exists()


def test_untagged_run_neither_checks_nor_takes_the_project_lock(home):
    path = admission.project_lock_path("/repo/x")
    path.parent.mkdir(parents=True)
    path.write_text(f"{os.getppid()} live holder\n")     # alive; would block a tagged run
    h = _acquire(_spec(), settings(), [])
    assert h.decision.verdict == "admit"
    assert path.read_text().startswith(str(os.getppid()))
    h.release()
    assert path.exists()


def test_live_project_lock_holder_makes_a_tagged_run_wait(home):
    path = admission.project_lock_path("/repo/x")
    path.parent.mkdir(parents=True)
    path.write_text(f"{os.getppid()} live holder\n")
    rec = RunRecord("x-1")
    with pytest.raises(AdmissionRefused) as e:
        _acquire(_spec(tags={"milestone": "6"}, rec=rec), settings(), [], wait=False)
    assert any("project lock" in r for r in e.value.reasons)


def test_dead_project_lock_holder_is_reclaimed(home):
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    path = admission.project_lock_path("/repo/x")
    path.parent.mkdir(parents=True)
    path.write_text(f"{p.pid} dead holder\n")
    rec = RunRecord("x-1")
    h = _acquire(_spec(tags={"milestone": "6"}, rec=rec), settings(), [])
    assert h.decision.verdict == "admit"
    assert path.read_text().split()[0] == str(os.getpid())
    h.release()


def test_two_launchers_serialize_on_the_flock_and_count_each_other(home):
    """The second acquire must block on the flock until the first container
    is visible, then see it in the committed total."""
    rows = []
    started = time.monotonic()
    results = {}
    cfg = settings(budget="16g")

    def inspect_exists(name):
        if time.monotonic() - started >= 1.0:
            rows.append(row(name, 8 * G))        # docker ps now shows it
            return True
        return False

    def first():
        h = admission.acquire(_spec("8g"), cfg, quiet=True,
                              inspect_running=lambda: list(rows),
                              read_mem_available=lambda: 30 * G,
                              read_memlog=lambda c: fresh(),
                              read_disk_free=roomy_disks)
        results["a"] = (h.decision, time.monotonic())
        h.release_when_visible("c-a", inspect_exists, timeout=30, poll=0.05)
        h.thread.join(10)
        results["a_released"] = time.monotonic()
        h.release()

    def second():
        time.sleep(0.2)                            # let the first take the flock
        h = admission.acquire(_spec("8g"), cfg, quiet=True,
                              inspect_running=lambda: list(rows),
                              read_mem_available=lambda: 30 * G,
                              read_memlog=lambda c: fresh(),
                              read_disk_free=roomy_disks)
        results["b"] = (h.decision, time.monotonic())
        h.release()

    ta, tb = threading.Thread(target=first), threading.Thread(target=second)
    ta.start(); tb.start()
    ta.join(15); tb.join(15)
    assert results["a"][0].numbers["committed"]["bytes"] == 0
    assert results["b"][1] >= results["a_released"] - 0.01
    assert results["b"][1] - started >= 1.0
    assert results["b"][0].numbers["committed"]["bytes"] == 8 * G
    assert results["b"][0].verdict == "admit"


def test_release_when_visible_gives_up_after_the_timeout(home):
    h = _acquire(_spec(), settings(), [])
    h.release_when_visible("never", lambda name: False, timeout=0.2, poll=0.05)
    h.thread.join(5)
    other = admission.locks.FileFlock(config.LOCK_DIR / "admission.lock")
    assert other.acquire(blocking=False) is True
    other.release()
    h.release()


def test_numbers_line_printed_once_on_admit(home, capsys):
    rec = RunRecord("x-1")
    rec.add_container("c1", "runc", ["true"])
    h = admission.acquire(_spec("8g", rec=rec), settings(), quiet=False,
                          inspect_running=lambda: [], read_mem_available=lambda: 30 * G,
                          read_memlog=lambda c: fresh(),
                          read_disk_free=roomy_disks)
    h.release()
    err = capsys.readouterr().err
    assert "admission:" in err and "budget=16g (config)" in err
    assert "request=8g (flag)" in err and "floor=8g (config)" in err
    assert rec.data["admission"]["numbers"]["budget"]["bytes"] == 16 * G
    assert rec.data["admission"]["verdict"] == "admit"


# ---------------------------------------------------------------- config surface
def test_defaults_and_env_map(clean_env):
    for k in ("admission_enabled", "admission_memory_budget", "admission_mem_floor_gb",
              "admission_wait_timeout_s", "admission_wait_interval_s",
              "admission_memlog_max_age_s"):
        assert k in config.DEFAULTS
        assert config._ENV[k] == "AGENT_SANDBOX_" + k.upper()
    assert config.DEFAULTS["admission_enabled"] is False
    assert admission.resolve_settings({}).enabled is False


def test_config_set_parses_booleans_and_null(home, capsys):
    assert cli.main(["config", "set", "admission_enabled", "false"]) == 0
    assert json.loads(config.CONFIG_FILE.read_text())["admission_enabled"] is False
    assert cli.main(["config", "set", "admission_enabled", "true"]) == 0
    assert json.loads(config.CONFIG_FILE.read_text())["admission_enabled"] is True
    assert cli.main(["config", "set", "admission_mem_floor_gb", "8"]) == 0
    assert json.loads(config.CONFIG_FILE.read_text())["admission_mem_floor_gb"] == 8
    assert cli.main(["config", "set", "admission_enabled", "null"]) == 0
    assert "admission_enabled" not in json.loads(config.CONFIG_FILE.read_text())


def test_env_budget_wins_over_config_and_reports_env(home, monkeypatch):
    monkeypatch.setenv("AGENT_SANDBOX_ADMISSION_MEMORY_BUDGET", "24g")
    s = admission.resolve_settings({"admission_memory_budget": "16g"})
    assert s.budget == 24 * G and s.sources["budget"] == "env"
    monkeypatch.setenv("AGENT_SANDBOX_ADMISSION_ENABLED", "true")
    assert admission.resolve_settings({"admission_enabled": False}).enabled is True


# ---------------------------------------------------------------- resources
def test_memory_swap_defaults_to_memory_and_slice_only_when_enabled(clean_env):
    r = resources.ResourceConfig("2", "8g", "256", "1h", {})
    a = r.docker_args()
    assert a[a.index("--memory-swap") + 1] == "8g"
    assert not any(x.startswith("--cgroup-parent") for x in a)
    r2 = resources.ResourceConfig("2", "8g", "256", "1h", {"admission_enabled": True,
                                                            "memory_swap": "12g"})
    a2 = r2.docker_args()
    assert a2[a2.index("--memory-swap") + 1] == "12g"
    assert "--cgroup-parent=agent-sandbox.slice" in a2
    assert r2.to_dict()["memory_swap"] == "12g"


# ---------------------------------------------------------------- metadata
def test_record_entries_carry_pid_and_offsets_and_tags(home):
    rec = RunRecord("x-1")
    rec.update(tags={"unit": "x", "milestone": "6"})
    rec.add_container("c1", "runc", ["true"], pid=4242, stdout_offset_start=17)
    rec.wait().save()
    d = json.loads(rec.file.read_text())
    assert d["tags"] == {"unit": "x", "milestone": "6"}
    e = d["containers"][-1]
    assert e["pid"] == 4242 and e["stdout_offset_start"] == 17 and e["stdout_offset_end"] is None
    assert e["status"] == "waiting" and e["waiting_since"] and d["status"] == "waiting"
    rec.start()
    assert rec.data["containers"][-1]["status"] == "running"
    rec.finish_container(0, "completed", stdout_offset_end=900).save()
    d = json.loads(rec.file.read_text())
    assert d["containers"][-1]["stdout_offset_end"] == 900
    assert rec.public()["tags"] == {"unit": "x", "milestone": "6"}


# ---------------------------------------------------------------- cli surface
def test_tag_flags_parse_into_a_dict_and_land_in_run_json(home):
    args = cli.build_parser().parse_args(
        ["run", ".", "--tag", "unit=x", "--tag", "milestone=6", "--no-wait", "--force"])
    tags = cli._parse_tags(args.tag)
    assert tags == {"unit": "x", "milestone": "6"}
    assert args.wait is False and args.force is True
    rec = RunRecord("x-1").update(tags=tags).save()
    assert json.loads(rec.file.read_text())["tags"] == {"unit": "x", "milestone": "6"}
    assert cli.build_parser().parse_args(["run", "."]).wait is True
    assert cli.build_parser().parse_args(["enter", "x-1", "--tag", "a=b"]).tag == ["a=b"]


def test_bad_tag_is_an_error():
    from agent_sandbox.errors import SandboxError
    with pytest.raises(SandboxError):
        cli._parse_tags(["novalue"])


def test_refusal_exits_3_with_json_payload(home, monkeypatch, capsys):
    d = admission.decide(20 * G, 0, 30 * G, fresh(), None, settings())

    def boom(args, command):
        raise AdmissionRefused(d)

    monkeypatch.setattr(cli, "cmd_run", boom)
    assert cli.main(["run", ".", "--json"]) == 3
    out = json.loads(capsys.readouterr().out)
    assert out["admission"] == "refused"
    assert out["reasons"] == d.reasons
    assert out["numbers"]["budget"]["bytes"] == 16 * G
    assert cli.main(["run", "."]) == 3
    err = capsys.readouterr().err
    assert "20g" in err and "16g" in err


def test_timeout_exits_3_too(home, monkeypatch, capsys):
    d = admission.decide(12 * G, 8 * G, 30 * G, fresh(), None, settings())

    def boom(args, command):
        raise AdmissionTimeout(d, waited_s=1800)

    monkeypatch.setattr(cli, "cmd_enter", boom)
    assert cli.main(["enter", "x-1", "--json"]) == 3
    out = json.loads(capsys.readouterr().out)
    assert out["admission"] == "timeout" and "headroom" in out["reasons"][0]


def test_cli_knows_admission(home):
    assert "admission" in cli.KNOWN
    args = cli.build_parser().parse_args(["admission", "show", "--json"])
    assert args.cmd == "admission" and args.action == "show" and args.json


def test_install_sets_the_slice_memory_max_and_records_it(home):
    calls = []

    def run(args):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    state = admission.install(16 * G, run=run)
    assert calls == [["systemctl", "--user", "set-property", "agent-sandbox.slice",
                      f"MemoryMax={16 * G}"]]
    assert state["memory_max"] == 16 * G and state["ok"] is True
    assert admission.state_file() == home / "runs" / ".admission-state.json"
    assert json.loads(admission.state_file().read_text())["memory_max"] == 16 * G


def test_install_records_a_failed_systemctl_before_raising(home):
    def run(args):
        return subprocess.CompletedProcess(args, 1, "", "Failed to set unit properties")

    with pytest.raises(SandboxError) as e:
        admission.install(16 * G, run=run)
    assert "Failed to set unit properties" in e.value.message
    state = json.loads(admission.state_file().read_text())
    assert state["ok"] is False
    assert state["memory_max"] == 16 * G
    assert "Failed to set unit properties" in state["output"]


def test_install_records_a_missing_systemctl_before_raising(home):
    def run(args):
        raise FileNotFoundError("systemctl")

    with pytest.raises(SandboxError):
        admission.install(16 * G, run=run)
    state = json.loads(admission.state_file().read_text())
    assert state["ok"] is False
    assert "systemctl" in state["output"]


# ---------------------------------------------------------------- cmd_admission
@pytest.fixture
def fake_readers(home, monkeypatch):
    """Every reading `admission show` takes, injected; mutate the dict to
    steer a test. `install` never touches the real home's dirs either."""
    G2 = 2 * G
    live = {
        "rows": [{"name": "agent-sandbox-a", "memory_limit": 4 * G},
                 {"name": "stray", "memory_limit": 0}],
        "memlog": fresh({"agent-sandbox-a": G2, "stray": G2}),
        "avail": 30 * G,
    }
    monkeypatch.setattr(config, "ensure_dirs", lambda: None)
    monkeypatch.setattr(admission, "inspect_running", lambda: live["rows"])
    monkeypatch.setattr(admission, "read_memlog", lambda cfg: live["memlog"])
    monkeypatch.setattr(admission, "read_mem_available", lambda: live["avail"])
    monkeypatch.setattr(admission, "read_disk_free",
                        lambda cfg: {cfg.disk_path: 100 * G, "/": 40 * G})
    return live


@pytest.fixture
def fake_systemctl(monkeypatch):
    """`admission install` through the CLI, with systemctl replaced; the
    list of argv lists it was given, and `rc` to make it fail."""
    class Systemctl:
        rc = 0
        calls = []

        @classmethod
        def run(cls, args):
            cls.calls.append(args)
            return subprocess.CompletedProcess(args, cls.rc, "", "Failed to set unit properties")

    real = admission.install
    monkeypatch.setattr(admission, "install", lambda budget: real(budget, run=Systemctl.run))
    return Systemctl


def write_cfg(home, **cfg):
    config.CONFIG_FILE.write_text(json.dumps(cfg))


def test_admission_install_json_caps_the_slice_and_records_it(home, fake_readers,
                                                              fake_systemctl, capsys):
    write_cfg(home, admission_memory_budget="16g")
    assert cli.main(["admission", "install", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["slice"] == "agent-sandbox.slice"
    assert out["memory_max"] == 16 * G and out["ok"] is True
    assert fake_systemctl.calls == [["systemctl", "--user", "set-property", "agent-sandbox.slice",
                               f"MemoryMax={16 * G}"]]
    assert json.loads((home / "runs" / ".admission-state.json").read_text()) == out


def test_admission_install_text_names_the_budget_source_and_says_when_off(
        home, fake_readers, fake_systemctl, capsys):
    write_cfg(home, admission_memory_budget="16g")
    assert cli.main(["admission", "install"]) == 0
    out = capsys.readouterr().out
    assert "agent-sandbox.slice MemoryMax=16g (config budget)" in out
    assert str(home / "runs" / ".admission-state.json") in out
    assert "admission is off" in out and "config set admission_enabled true" in out
    write_cfg(home, admission_memory_budget="16g", admission_enabled=True)
    assert cli.main(["admission", "install"]) == 0
    assert "admission is off" not in capsys.readouterr().out


def test_admission_install_failure_exits_1_with_the_remedy(home, fake_readers,
                                                           fake_systemctl, capsys):
    fake_systemctl.rc = 1
    assert cli.main(["admission", "install"]) == 1
    err = capsys.readouterr().err
    assert "could not set MemoryMax" in err and "Failed to set unit properties" in err
    assert "is-system-running" in err


def test_admission_show_json_decides_for_the_default_run_even_when_off(
        home, fake_readers, fake_systemctl, capsys):
    write_cfg(home, admission_memory_budget="32g")
    assert cli.main(["admission", "show", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"enabled", "sources", "wait_timeout_s", "wait_interval_s",
                        "containers", "memlog", "decision", "slice"}
    assert out["enabled"] is False and out["sources"]["budget"] == "config"
    assert out["wait_timeout_s"] == 1800 and out["wait_interval_s"] == 30
    assert out["containers"] == fake_readers["rows"]
    assert out["memlog"] == {"fresh": True, "reason": "fresh", "age_s": 30.0}
    assert out["slice"] is None                      # nothing installed yet
    d = out["decision"]
    assert d["verdict"] == "admit" and d["reasons"] == []
    n = d["numbers"]
    assert n["request"] == {"bytes": 16 * G, "source": "default"}
    # 4g limit + a 2g unlimited container at the 1.25 factor.
    assert n["committed"]["bytes"] == 4 * G + int(2 * G * admission.UNLIMITED_FACTOR)
    assert n["mem_available"]["bytes"] == 30 * G
    assert n["headroom"]["bytes"] == 32 * G - n["committed"]["bytes"]
    assert n["disk_floor"]["bytes"] == 20 * G and n["disk_free:/"]["bytes"] == 40 * G
    # After install, the recorded slice state rides along.
    assert cli.main(["admission", "install", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["admission", "show", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["slice"]["memory_max"] == 32 * G


def test_admission_show_text_lists_containers_usage_and_the_verdict(
        home, fake_readers, fake_systemctl, capsys):
    write_cfg(home, admission_memory_budget="32g", admission_enabled=True)
    assert cli.main(["admission", "show"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("admission enabled (config)")
    assert "  agent-sandbox-a: limit 4g, using 2g\n" in out
    assert "  stray: limit unlimited, using 2g\n" in out
    assert "memory log: fresh\n" in out
    assert "slice not set: agent-sandbox admission install" in out
    assert "a default run (16g) would: admit\n" in out
    # A container the log has not seen yet prints its limit alone.
    fake_readers["rows"].append({"name": "new", "memory_limit": 0})
    assert cli.main(["admission", "install"]) == 0
    capsys.readouterr()
    assert cli.main(["admission", "show"]) == 0
    out = capsys.readouterr().out
    assert "  new: limit unlimited\n" in out
    assert "slice agent-sandbox.slice: MemoryMax=32g set 20" in out
    assert "(FAILED)" not in out


def test_admission_show_reports_a_stale_log_as_a_refusal(home, fake_readers, capsys):
    fake_readers["memlog"] = stale()
    write_cfg(home, admission_memory_budget="32g")
    assert cli.main(["admission", "show", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["memlog"]["fresh"] is False and "400 s old" in out["memlog"]["reason"]
    assert out["decision"]["verdict"] == "refuse"
    assert out["decision"]["reasons"] == ["memory log stale: " + stale().reason]
    assert cli.main(["admission", "show"]) == 0
    out = capsys.readouterr().out
    assert "admission DISABLED (default)" in out
    assert "memory log: STALE: last sample is 400 s old" in out
    assert "would: refuse (memory log stale" in out


@pytest.mark.parametrize("exc", [OSError("meminfo"), ValueError("MemAvailable")])
def test_admission_show_treats_an_unreadable_meminfo_as_zero(home, fake_readers, monkeypatch,
                                                              capsys, exc):
    def boom():
        raise exc
    monkeypatch.setattr(admission, "read_mem_available", boom)
    write_cfg(home, admission_memory_budget="32g")
    assert cli.main(["admission", "show", "--json"]) == 0
    d = json.loads(capsys.readouterr().out)["decision"]
    assert d["numbers"]["mem_available"]["bytes"] == 0
    assert d["verdict"] == "wait"
    assert d["reasons"] == ["MemAvailable 0g below floor 8g"]
