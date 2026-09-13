"""disk guard (KTD11): admission refuses on a host or guest disk below the
floor, the memory log records disk_free per sample, and doctor checks the
host drive and, under WSL, that the distro's vhdx is sparse.

Every reading is injected (KTD9): no shutil.disk_usage on a real path the
test did not choose, no cmd.exe, no /proc/version.
"""

import os

import pytest

from agent_sandbox import admission, config, doctor, memlog, resources
from agent_sandbox.backend import SandboxSpec
from agent_sandbox.errors import AdmissionRefused

G = 1024 ** 3
BOOT = "5e8933b9-ec47-4a27-872d-e9a1c365ce3a"
WSL_VERSION = ("Linux version 6.6.87.2-microsoft-standard-WSL2 (root@...) "
               "(gcc (GCC) 11.2.0) #1 SMP PREEMPT_DYNAMIC")
PLAIN_VERSION = "Linux version 6.1.0-21-amd64 (debian-kernel@lists.debian.org) #1 SMP"


def sample(disk_free=None):
    return memlog.Sample(boot_id=BOOT, monotonic=1000.0, time="2026-09-13T16:00:00+00:00",
                         mem_available=40 * G, swap_free=16 * G, containers={},
                         disk_free=disk_free or {})


def fresh():
    return memlog.Freshness(True, "fresh", 30.0, sample())


def settings(**more):
    cfg = {"admission_enabled": True, "admission_memory_budget": "16g",
           "admission_mem_floor_gb": 8, "admission_disk_path": "/mnt/c"}
    cfg.update(more)
    return admission.resolve_settings(cfg)


def disks(host=100 * G, root=40 * G):
    return {"/mnt/c": host, "/": root}


@pytest.fixture
def clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("AGENT_SANDBOX_"):
            monkeypatch.delenv(k)


@pytest.fixture
def home(tmp_path, monkeypatch, clean_env):
    monkeypatch.setattr(config, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(config, "LOCK_DIR", tmp_path / "runs" / ".locks")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config, "ROOT", tmp_path)
    return tmp_path


# ---------------------------------------------------------------- decide
def test_host_disk_below_floor_refuses_and_names_path_free_and_floor(clean_env):
    d = admission.decide(1 * G, 0, 30 * G, fresh(), None, settings(),
                         disk_free=disks(host=int(1.1 * G)))
    assert d.verdict == "refuse"
    assert len(d.reasons) == 1
    assert "/mnt/c" in d.reasons[0] and "1.1g" in d.reasons[0] and "20g" in d.reasons[0]


def test_guest_root_below_floor_refuses_too(clean_env):
    d = admission.decide(1 * G, 0, 30 * G, fresh(), None, settings(),
                         disk_free=disks(root=5 * G))
    assert d.verdict == "refuse"
    assert any(r.startswith("disk / ") and "5g" in r for r in d.reasons)


def test_unreadable_disk_reading_refuses(clean_env):
    d = admission.decide(1 * G, 0, 30 * G, fresh(), None, settings(),
                         disk_free={"/mnt/c": None, "/": 40 * G})
    assert d.verdict == "refuse"
    assert any("/mnt/c" in r and "unreadable" in r for r in d.reasons)


def test_disk_refusal_wins_over_a_memory_wait(clean_env):
    # Headroom is short (a wait) and the disk is short (a refusal): refuse,
    # because waiting cannot free a disk.
    d = admission.decide(12 * G, 8 * G, 30 * G, fresh(), None, settings(),
                         disk_free=disks(host=2 * G))
    assert d.verdict == "refuse"
    assert all("disk" in r for r in d.reasons)


def test_both_above_floor_admits_with_both_readings_sourced(clean_env):
    d = admission.decide(1 * G, 0, 30 * G, fresh(), None, settings(),
                         disk_free=disks(host=100 * G, root=40 * G))
    assert d.verdict == "admit" and d.reasons == []
    n = d.numbers
    assert n["disk_floor"] == {"bytes": 20 * G, "source": "default"}
    assert n["disk_free:/mnt/c"]["bytes"] == 100 * G
    assert n["disk_free:/"]["bytes"] == 40 * G
    assert "live" in n["disk_free:/mnt/c"]["source"]
    assert "live" in n["disk_free:/"]["source"]
    line = admission.describe(d)
    assert "disk_floor=20g (default)" in line and "/mnt/c" in line and "100g" in line


def test_no_disk_readings_means_not_checked(clean_env):
    # `admission show` in the CLI decides without disk readings; that path
    # must keep working and must not invent a refusal.
    d = admission.decide(1 * G, 0, 30 * G, fresh(), None, settings())
    assert d.verdict == "admit"
    assert not any(k.startswith("disk_free") for k in d.numbers)


# ---------------------------------------------------------------- acquire
class _WS:
    def __init__(self, repo):
        self.repo = repo
        self.path = repo


def _spec(memory="8g"):
    res = resources.ResourceConfig("1", memory, "256", "5m", {})
    return SandboxSpec("x-1", _WS("/repo/x"), ["true"], resources=res, record=None)


def test_acquire_disk_refusal_does_not_wait_even_with_memory_headroom(home):
    sleeps = []
    with pytest.raises(AdmissionRefused) as e:
        admission.acquire(_spec(), settings(), quiet=True,
                          inspect_running=lambda: [],
                          read_mem_available=lambda: 30 * G,
                          read_memlog=lambda c: fresh(),
                          read_disk_free=lambda c: disks(host=int(1.1 * G)),
                          now=lambda: 0.0, sleep=sleeps.append)
    assert sleeps == []
    assert any("/mnt/c" in r for r in e.value.reasons)
    assert e.value.decision.numbers["disk_free:/mnt/c"]["bytes"] == int(1.1 * G)


def test_acquire_admits_when_both_disks_are_above_the_floor(home):
    h = admission.acquire(_spec(), settings(), quiet=True,
                          inspect_running=lambda: [],
                          read_mem_available=lambda: 30 * G,
                          read_memlog=lambda c: fresh(),
                          read_disk_free=lambda c: disks())
    assert h.decision.verdict == "admit"
    h.release()


def test_default_disk_reader_reads_the_settings_path_and_root(clean_env):
    seen = []

    def usage(path):
        seen.append(path)
        return type("Usage", (), {"free": 7 * G})()

    cfg = settings(admission_disk_path="/somewhere")
    out = memlog.disk_free([cfg.disk_path, "/"], usage=usage)
    assert out == {"/somewhere": 7 * G, "/": 7 * G}
    assert seen == ["/somewhere", "/"]


def test_disk_free_marks_an_unreadable_path_none():
    def usage(path):
        raise OSError("no such path")
    assert memlog.disk_free(["/nowhere"], usage=usage) == {"/nowhere": None}


# ---------------------------------------------------------------- config surface
def test_disk_keys_in_defaults_and_env_map(clean_env):
    assert config.DEFAULTS["admission_disk_floor_gb"] == 20
    assert config.DEFAULTS["admission_disk_path"] is None
    assert config._ENV["admission_disk_floor_gb"] == "AGENT_SANDBOX_ADMISSION_DISK_FLOOR_GB"
    assert config._ENV["admission_disk_path"] == "AGENT_SANDBOX_ADMISSION_DISK_PATH"


def test_host_disk_path_auto_on_wsl_and_on_plain_linux(clean_env):
    assert config.host_disk_path({}, read_version=lambda: WSL_VERSION) == "/mnt/c"
    assert config.host_disk_path({}, read_version=lambda: PLAIN_VERSION) == "/"
    # a configured path wins over auto, whatever the kernel says
    assert config.host_disk_path({"admission_disk_path": "/data"},
                                 read_version=lambda: WSL_VERSION) == "/data"


def test_host_disk_path_env_wins_and_missing_proc_version_means_root(clean_env, monkeypatch):
    monkeypatch.setenv("AGENT_SANDBOX_ADMISSION_DISK_PATH", "/mnt/d")
    assert config.host_disk_path({"admission_disk_path": "/data"},
                                 read_version=lambda: WSL_VERSION) == "/mnt/d"
    monkeypatch.delenv("AGENT_SANDBOX_ADMISSION_DISK_PATH")
    assert config.host_disk_path({}, read_version=lambda: "") == "/"


def test_settings_carry_disk_floor_and_path_with_sources(clean_env, monkeypatch):
    s = admission.resolve_settings({"admission_disk_path": "/mnt/c"})
    assert s.disk_floor == 20 * G and s.sources["disk_floor"] == "default"
    assert s.disk_path == "/mnt/c" and s.sources["disk_path"] == "config"
    monkeypatch.setenv("AGENT_SANDBOX_ADMISSION_DISK_FLOOR_GB", "5")
    s = admission.resolve_settings({"admission_enabled": True, "admission_disk_floor_gb": 30,
                                    "admission_disk_path": "/"})
    assert s.disk_floor == 5 * G and s.sources["disk_floor"] == "env"
    d = admission.decide(1 * G, 0, 30 * G, fresh(), None, s, disk_free={"/": 6 * G})
    assert d.verdict == "admit"
    assert d.numbers["disk_floor"] == {"bytes": 5 * G, "source": "env"}


def test_settings_auto_disk_path_is_reported_as_auto(clean_env, monkeypatch):
    monkeypatch.setattr(config, "read_proc_version", lambda: WSL_VERSION)
    s = admission.resolve_settings({})
    assert s.disk_path == "/mnt/c" and s.sources["disk_path"] == "auto"


# ---------------------------------------------------------------- memlog line
def test_sample_line_roundtrips_disk_free():
    line = memlog.format_line(sample({"/mnt/c": 123, "/": 456}))
    assert "disk_free=/mnt/c=123,/=456" in line
    assert memlog.parse_line(line).disk_free == {"/mnt/c": 123, "/": 456}


def test_unreadable_disk_free_roundtrips_as_none():
    line = memlog.format_line(sample({"/mnt/c": None, "/": 456}))
    assert "disk_free=/mnt/c=none,/=456" in line
    assert memlog.parse_line(line).disk_free == {"/mnt/c": None, "/": 456}


def test_old_line_without_disk_free_still_parses():
    old = (f"boot={BOOT} mono=1000.000 time=t mem_available=1 swap_free=2 "
           "containers=a=3")
    s = memlog.parse_line(old)
    assert s.disk_free == {} and s.containers == {"a": 3} and s.mem_available == 1


def test_disk_free_with_no_readings_is_an_empty_field():
    line = memlog.format_line(sample({}))
    assert line.endswith(" disk_free=")
    assert memlog.parse_line(line).disk_free == {}


def test_sample_records_the_injected_disk_readings(tmp_path):
    log = tmp_path / "memory.log"
    meminfo = ("MemTotal:       65536000 kB\nMemAvailable:   46810764 kB\n"
               "SwapTotal:      16777216 kB\nSwapFree:       15623452 kB\n")
    memlog.sample(log, read_boot_id=lambda: BOOT, read_monotonic=lambda: 1.0,
                  read_meminfo=lambda: meminfo, docker_ps=lambda: [],
                  cgroup_roots=[tmp_path / "nope"], docker_stats=lambda: {},
                  now=lambda: "t", read_disk_free=lambda: {"/mnt/c": 5 * G, "/": 6 * G})
    f = memlog.parse_last_sample(log, BOOT, 2.0, max_age_s=180)
    assert f.fresh and f.sample.disk_free == {"/mnt/c": 5 * G, "/": 6 * G}


# ---------------------------------------------------------------- doctor: host disk
def cfg_disk(**more):
    cfg = {"admission_disk_path": "/mnt/c"}
    cfg.update(more)
    return cfg


def test_doctor_host_disk_fails_below_the_floor_with_owner_steps(clean_env):
    c = doctor.host_disk_check(cfg_disk(),
                               read_disk_free=lambda paths: disks(host=int(1.1 * G)))
    assert c.status == doctor.FAIL
    assert "/mnt/c" in c.detail and "1.1" in c.detail and "20" in c.detail
    assert "wsl --shutdown" in c.remedy and "--set-sparse true" in c.remedy
    assert "fstrim -v /" in c.remedy
    assert "clean --docker" in c.remedy


def test_doctor_host_disk_passes_above_the_floor_listing_every_path(clean_env):
    c = doctor.host_disk_check(cfg_disk(), read_disk_free=lambda paths: disks(host=100 * G))
    assert c.status == doctor.PASS and c.remedy == ""
    assert "/mnt/c: 100.0 GiB free" in c.detail
    assert "/: 40.0 GiB free" in c.detail


def test_doctor_host_disk_honours_the_configured_floor(clean_env):
    c = doctor.host_disk_check(cfg_disk(admission_disk_floor_gb=5),
                               read_disk_free=lambda paths: disks(host=6 * G, root=6 * G))
    assert c.status == doctor.PASS


def test_doctor_host_disk_unreadable_fails(clean_env):
    c = doctor.host_disk_check(cfg_disk(),
                               read_disk_free=lambda paths: {"/mnt/c": None, "/": 40 * G})
    assert c.status == doctor.FAIL and "/mnt/c: free space unreadable" in c.detail


def test_doctor_host_disk_fails_when_only_the_guest_root_is_short(clean_env):
    # Admission refuses on the guest root too; doctor must not pass a host
    # whose launches will be refused.
    c = doctor.host_disk_check(cfg_disk(), read_disk_free=lambda paths: disks(root=5 * G))
    assert c.status == doctor.FAIL
    assert "/: 5.0 GiB free" in c.detail and "/mnt/c: 100.0 GiB free" in c.detail
    assert "wsl --shutdown" in c.remedy


def test_doctor_host_disk_unreadable_guest_root_fails(clean_env):
    c = doctor.host_disk_check(cfg_disk(),
                               read_disk_free=lambda paths: {"/mnt/c": 100 * G, "/": None})
    assert c.status == doctor.FAIL and "/: free space unreadable" in c.detail


def test_doctor_host_disk_reads_exactly_the_paths_admission_reads(clean_env):
    seen = []

    def read(paths):
        seen.append(list(paths))
        return disks()

    c = doctor.host_disk_check(cfg_disk(), read_disk_free=read)
    assert c.status == doctor.PASS
    assert seen == [memlog.disk_paths(cfg_disk())]
    # admission.read_disk_free reads [settings.disk_path, "/"]; the same set.
    assert set(seen[0]) == {settings().disk_path, "/"}


def test_doctor_host_disk_off_wsl_reads_the_root_once(clean_env, monkeypatch):
    monkeypatch.setattr(config, "read_proc_version", lambda: PLAIN_VERSION)
    seen = []

    def read(paths):
        seen.append(list(paths))
        return {"/": 40 * G}

    c = doctor.host_disk_check({}, read_disk_free=read)
    assert c.status == doctor.PASS
    assert seen == [["/"]]
    assert c.detail.startswith("/: 40.0 GiB free")


# ---------------------------------------------------------------- doctor: vhdx sparse
VHDX = "/mnt/c/Users/bishal/AppData/Local/wsl/{abc}/ext4.vhdx"


def test_vhdx_not_sparse_fails_with_the_owner_remedy():
    out = "T\x00his file is NOT set as sparse\r\n"
    c = doctor.vhdx_sparse_check({}, run=lambda p: out, find=lambda cfg: [VHDX])
    assert c.status == doctor.FAIL
    assert VHDX in c.detail and "NOT set as sparse" in c.detail
    assert c.remedy == ("wsl --shutdown; wsl --manage <Distro> --set-sparse true; "
                        "then fstrim -v / as root (wsl.exe -u root -- fstrim -v /)")


def test_vhdx_sparse_passes():
    c = doctor.vhdx_sparse_check({}, run=lambda p: "This file is set as sparse\r\n",
                                 find=lambda cfg: [VHDX])
    assert c.status == doctor.PASS and c.remedy == ""
    assert VHDX in c.detail


def test_vhdx_check_warns_without_cmd_exe():
    c = doctor.vhdx_sparse_check({}, run=lambda p: None, find=lambda cfg: [VHDX])
    assert c.status == doctor.WARN
    assert "cmd.exe" in c.detail


def test_vhdx_check_warns_when_no_file_is_found():
    c = doctor.vhdx_sparse_check({}, run=lambda p: "irrelevant", find=lambda cfg: [])
    assert c.status == doctor.WARN
    assert "ext4.vhdx" in c.detail


def test_vhdx_check_one_unsparse_file_among_two_fails():
    outs = {VHDX: "This file is set as sparse", "/mnt/c/x/ext4.vhdx": "This file is NOT set as sparse"}
    c = doctor.vhdx_sparse_check({}, run=lambda p: outs[p],
                                 find=lambda cfg: list(outs))
    assert c.status == doctor.FAIL


def test_vhdx_candidates_honour_the_config_key_and_the_glob():
    found = doctor.vhdx_candidates({"admission_vhdx_path": "/mnt/d/wsl/ext4.vhdx"},
                                   glob_paths=lambda pattern: [VHDX])
    assert found == ["/mnt/d/wsl/ext4.vhdx", VHDX]
    assert doctor.vhdx_candidates({}, glob_paths=lambda pattern: []) == []


def test_windows_path_for_fsutil():
    assert doctor.windows_path(VHDX) == r"C:\Users\bishal\AppData\Local\wsl\{abc}\ext4.vhdx"
    assert doctor.windows_path("/mnt/d/x/ext4.vhdx") == r"D:\x\ext4.vhdx"
    assert doctor.windows_path("/home/x/ext4.vhdx") == "/home/x/ext4.vhdx"


def test_fsutil_runner_builds_the_interop_command(tmp_path):
    calls = []

    def run(args, **kw):
        calls.append(args)
        return type("P", (), {"stdout": b"This file is set as sparse\r\n", "stderr": b""})()

    cmd = tmp_path / "cmd.exe"
    cmd.write_text("")
    out = doctor.fsutil_sparse_queryflag(VHDX, cmd_exe=cmd, run=run)
    assert out == "This file is set as sparse\r\n"
    assert calls == [[str(cmd), "/c", "fsutil", "sparse", "queryflag", doctor.windows_path(VHDX)]]
    assert doctor.fsutil_sparse_queryflag(VHDX, cmd_exe=tmp_path / "absent.exe", run=run) is None
