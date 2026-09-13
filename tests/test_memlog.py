"""memlog: the sample line, its staleness rule, the minimum, and rotation.

Every reader is injected, so nothing here needs Docker, a cgroup tree, or
the host clock (KTD9).
"""

import pytest

from agent_sandbox import config, memlog

BOOT = "5e8933b9-ec47-4a27-872d-e9a1c365ce3a"
OTHER_BOOT = "00000000-0000-0000-0000-000000000000"


def line(boot=BOOT, mono=1000.0, when="2026-09-13T16:00:00+00:00",
         avail=40 * 1024 ** 3, swap=16 * 1024 ** 3, containers=None):
    return memlog.format_line(memlog.Sample(
        boot_id=boot, monotonic=mono, time=when, mem_available=avail,
        swap_free=swap, containers=containers or {}))


def write(path, lines):
    path.write_text("".join(l + "\n" for l in lines))


# ---------------------------------------------------------------- parse_last_sample
def test_fresh_line_from_current_boot(tmp_path):
    log = tmp_path / "memory.log"
    write(log, [line(mono=1000.0, containers={"a": 123, "b": 456})])
    f = memlog.parse_last_sample(log, BOOT, 1060.0, max_age_s=180)
    assert f.fresh is True
    assert f.reason == "fresh"
    assert f.age_s == pytest.approx(60.0)
    assert f.sample.mem_available == 40 * 1024 ** 3
    assert f.sample.swap_free == 16 * 1024 ** 3
    assert f.sample.containers == {"a": 123, "b": 456}
    assert f.sample.time == "2026-09-13T16:00:00+00:00"


def test_other_boot_id_is_stale(tmp_path):
    log = tmp_path / "memory.log"
    write(log, [line(boot=OTHER_BOOT, mono=1000.0)])
    f = memlog.parse_last_sample(log, BOOT, 1010.0, max_age_s=180)
    assert f.fresh is False
    assert "boot" in f.reason
    assert f.sample is not None          # values are still available for show


def test_old_by_monotonic_is_stale_even_when_wall_time_is_recent(tmp_path):
    log = tmp_path / "memory.log"
    write(log, [line(mono=1000.0, when="2999-01-01T00:00:00+00:00")])
    f = memlog.parse_last_sample(log, BOOT, 1200.0, max_age_s=180)
    assert f.fresh is False
    assert f.age_s == pytest.approx(200.0)
    assert "180" in f.reason


def test_exactly_max_age_is_fresh(tmp_path):
    log = tmp_path / "memory.log"
    write(log, [line(mono=1000.0)])
    assert memlog.parse_last_sample(log, BOOT, 1180.0, max_age_s=180).fresh is True


def test_missing_file_is_stale(tmp_path):
    f = memlog.parse_last_sample(tmp_path / "absent.log", BOOT, 1.0, max_age_s=180)
    assert f.fresh is False
    assert "missing" in f.reason
    assert f.sample is None and f.age_s is None


def test_empty_file_is_stale(tmp_path):
    log = tmp_path / "memory.log"
    log.write_text("")
    f = memlog.parse_last_sample(log, BOOT, 1.0, max_age_s=180)
    assert f.fresh is False
    assert "empty" in f.reason


def test_malformed_last_line_is_stale_with_parse_reason(tmp_path):
    log = tmp_path / "memory.log"
    write(log, [line(mono=1000.0), "garbage that is not a sample"])
    f = memlog.parse_last_sample(log, BOOT, 1010.0, max_age_s=180)
    assert f.fresh is False
    assert "malformed" in f.reason
    assert f.sample is None              # the earlier good line is not used


def test_trailing_blank_lines_do_not_hide_the_last_sample(tmp_path):
    log = tmp_path / "memory.log"
    log.write_text(line(mono=1000.0) + "\n\n")
    assert memlog.parse_last_sample(log, BOOT, 1010.0, max_age_s=180).fresh is True


def test_future_monotonic_is_stale(tmp_path):
    # A sample "from the future" means a clock we do not understand; refuse.
    log = tmp_path / "memory.log"
    write(log, [line(mono=5000.0)])
    assert memlog.parse_last_sample(log, BOOT, 1000.0, max_age_s=180).fresh is False


# ---------------------------------------------------------------- minimum_since
def test_minimum_since_over_three_samples(tmp_path):
    log = tmp_path / "memory.log"
    write(log, [
        line(mono=100.0, when="t1", avail=30),
        line(mono=200.0, when="t2", avail=10),
        line(mono=300.0, when="t3", avail=20),
    ])
    assert memlog.minimum_since(log, 100.0, BOOT) == (10, "t2")
    assert memlog.minimum_since(log, 250.0, BOOT) == (20, "t3")
    assert memlog.minimum_since(log, 400.0, BOOT) is None


def test_minimum_since_ignores_other_boots_and_malformed_lines(tmp_path):
    log = tmp_path / "memory.log"
    write(log, [
        line(boot=OTHER_BOOT, mono=150.0, when="old-boot", avail=1),
        "not a sample",
        line(mono=150.0, when="t", avail=5),
    ])
    assert memlog.minimum_since(log, 100.0, BOOT) == (5, "t")


def test_minimum_since_missing_file(tmp_path):
    assert memlog.minimum_since(tmp_path / "absent.log", 0.0, BOOT) is None


# ---------------------------------------------------------------- rotation
def test_rotation_keeps_the_newest_lines(tmp_path):
    log = tmp_path / "memory.log"
    write(log, [f"l{i}" for i in range(25)])
    memlog.rotate(log, limit=20, keep=10)
    kept = log.read_text().splitlines()
    assert kept == [f"l{i}" for i in range(15, 25)]


def test_rotation_leaves_a_short_log_alone(tmp_path):
    log = tmp_path / "memory.log"
    write(log, [f"l{i}" for i in range(20)])
    memlog.rotate(log, limit=20, keep=10)
    assert len(log.read_text().splitlines()) == 20


def test_rotation_defaults():
    assert memlog.ROTATE_AT == 20000 and memlog.ROTATE_KEEP == 10000


# ---------------------------------------------------------------- sample
def fake_cgroup(tmp_path, ids_to_bytes):
    root = tmp_path / "cgroup"
    for cid, b in ids_to_bytes.items():
        d = root / f"docker-{cid}.scope"
        d.mkdir(parents=True)
        (d / "memory.current").write_text(f"{b}\n")
    return root


def meminfo_text():
    return ("MemTotal:       65536000 kB\n"
            "MemFree:        1000 kB\n"
            "MemAvailable:   46810764 kB\n"
            "SwapTotal:      16777216 kB\n"
            "SwapFree:       15623452 kB\n")


def test_sample_reads_both_containers_from_the_cgroup_tree(tmp_path):
    root = fake_cgroup(tmp_path, {"a" * 64: 100, "b" * 64: 200})
    stats_calls = []

    def stats():
        stats_calls.append(1)
        return {}

    log = tmp_path / "memory.log"
    memlog.sample(
        log, read_boot_id=lambda: BOOT, read_monotonic=lambda: 1234.5,
        read_meminfo=lambda: meminfo_text(),
        docker_ps=lambda: [("a" * 64, "alpha"), ("b" * 64, "beta")],
        cgroup_roots=[root], docker_stats=stats,
        now=lambda: "2026-09-13T16:00:00+00:00")
    f = memlog.parse_last_sample(log, BOOT, 1240.0, max_age_s=180)
    assert f.fresh
    assert f.sample.containers == {"alpha": 100, "beta": 200}
    assert f.sample.mem_available == 46810764 * 1024
    assert f.sample.swap_free == 15623452 * 1024
    assert f.sample.monotonic == 1234.5
    assert stats_calls == []             # the slow path was not taken


def test_container_memory_finds_a_scope_under_the_admission_slice(tmp_path):
    # With admission on, the scope lives under agent-sandbox.slice, not
    # user.slice; the fast path must look there before paying for docker stats.
    user_root = tmp_path / "user.slice"
    user_root.mkdir()
    slice_root = fake_cgroup(tmp_path, {"a" * 64: 100})
    stats_calls = []

    def stats():
        stats_calls.append(1)
        return {}

    got = memlog.container_memory([("a" * 64, "alpha")], cgroup_roots=[user_root, slice_root],
                                  docker_stats=stats)
    assert got == {"alpha": 100}
    assert stats_calls == []


def test_default_cgroup_roots_are_user_slice_then_the_admission_slice():
    from agent_sandbox.resources import ResourceConfig
    assert memlog.CGROUP_ROOTS == (config.user_manager_cgroup("user.slice"),
                                   config.user_manager_cgroup(ResourceConfig.SLICE))
    assert memlog.SLICE_CGROUP_ROOT.name == "agent-sandbox.slice"
    assert memlog.SLICE_CGROUP_ROOT.parent.name == f"user@{config.UID}.service"


def test_sample_falls_back_to_docker_stats_when_a_directory_is_missing(tmp_path):
    root = fake_cgroup(tmp_path, {"a" * 64: 100})
    log = tmp_path / "memory.log"
    memlog.sample(
        log, read_boot_id=lambda: BOOT, read_monotonic=lambda: 1.0,
        read_meminfo=lambda: meminfo_text(),
        docker_ps=lambda: [("a" * 64, "alpha"), ("c" * 64, "gamma")],
        cgroup_roots=[root], docker_stats=lambda: {"gamma": 999, "alpha": 1},
        now=lambda: "t")
    f = memlog.parse_last_sample(log, BOOT, 2.0, max_age_s=180)
    assert f.sample.containers == {"alpha": 100, "gamma": 999}


def test_sample_appends_and_rotates(tmp_path):
    log = tmp_path / "memory.log"
    kw = dict(read_boot_id=lambda: BOOT, read_monotonic=lambda: 1.0,
              read_meminfo=lambda: meminfo_text(), docker_ps=lambda: [],
              cgroup_roots=[tmp_path / "nope"], docker_stats=lambda: {},
              now=lambda: "t")
    memlog.sample(log, **kw)
    memlog.sample(log, **kw)
    assert len(log.read_text().splitlines()) == 2
    write(log, [line()] * memlog.ROTATE_AT)
    memlog.sample(log, **kw)
    assert len(log.read_text().splitlines()) == memlog.ROTATE_KEEP


def test_sample_with_no_containers_writes_a_parseable_line(tmp_path):
    log = tmp_path / "memory.log"
    memlog.sample(log, read_boot_id=lambda: BOOT, read_monotonic=lambda: 1.0,
                  read_meminfo=lambda: meminfo_text(), docker_ps=lambda: [],
                  cgroup_roots=[tmp_path / "nope"], docker_stats=lambda: {},
                  now=lambda: "t")
    f = memlog.parse_last_sample(log, BOOT, 2.0, max_age_s=180)
    assert f.fresh and f.sample.containers == {}


# ---------------------------------------------------------------- readers
def test_parse_meminfo_converts_kib_to_bytes():
    assert memlog.parse_meminfo(meminfo_text()) == {
        "MemAvailable": 46810764 * 1024, "SwapFree": 15623452 * 1024}


def test_parse_docker_stats_units():
    out = ("alpha\t1.5GiB / 8GiB\n"
           "beta\t512MiB / 8GiB\n"
           "gamma\t100kB / 8GiB\n")
    assert memlog.parse_docker_stats(out) == {
        "alpha": int(1.5 * 1024 ** 3), "beta": 512 * 1024 ** 2, "gamma": 100_000}


def test_parse_line_roundtrip_and_rejects_bad_numbers():
    s = memlog.parse_line(line(containers={"x-1.y": 7}))
    assert s.containers == {"x-1.y": 7}
    with pytest.raises(ValueError):
        memlog.parse_line("boot=b mono=abc time=t mem_available=1 swap_free=1 containers=")


# ---------------------------------------------------------------- install
def test_install_writes_units_and_enables_the_timer(tmp_path):
    calls = []

    def run(args):
        calls.append(args)

    unit_dir = tmp_path / "systemd" / "user"
    written = memlog.install(unit_dir=unit_dir, bin_path="/opt/x/bin/agent-sandbox", run=run)
    svc = (unit_dir / "agent-sandbox-memlog.service").read_text()
    tmr = (unit_dir / "agent-sandbox-memlog.timer").read_text()
    assert set(p.name for p in written) == {"agent-sandbox-memlog.service",
                                           "agent-sandbox-memlog.timer"}
    assert "ExecStart=/opt/x/bin/agent-sandbox memlog sample" in svc
    assert "Type=oneshot" in svc
    assert "OnBootSec=1min" in tmr and "OnUnitActiveSec=1min" in tmr
    assert "WantedBy=timers.target" in tmr
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert ["systemctl", "--user", "enable", "--now", "agent-sandbox-memlog.timer"] in calls


def test_shipped_templates_exist():
    assert (memlog.TEMPLATE_DIR / "agent-sandbox-memlog.service").is_file()
    assert (memlog.TEMPLATE_DIR / "agent-sandbox-memlog.timer").is_file()


# ---------------------------------------------------------------- config
def test_max_age_default_and_ladder(monkeypatch):
    assert config.DEFAULTS["admission_memlog_max_age_s"] == 180
    assert memlog.max_age_s({}) == 180
    assert memlog.max_age_s({"admission_memlog_max_age_s": 60}) == 60
    monkeypatch.setenv("AGENT_SANDBOX_ADMISSION_MEMLOG_MAX_AGE_S", "90")
    assert memlog.max_age_s({}) == 90


def test_log_path_lives_under_logs():
    assert memlog.LOG == config.LOGS / "memory.log"


# ---------------------------------------------------------------- cli wiring
def test_cli_knows_memlog():
    from agent_sandbox import cli
    assert "memlog" in cli.KNOWN
    args = cli.build_parser().parse_args(["memlog", "show", "--last", "3"])
    assert args.cmd == "memlog" and args.action == "show" and args.last == 3


def test_peak_containers_and_suggest_memory(tmp_path):
    log = tmp_path / "memory.log"
    lines = []
    for mono, used in ((100, 1 * 1024 ** 3), (160, 3 * 1024 ** 3), (220, 2 * 1024 ** 3)):
        lines.append(memlog.format_line(memlog.Sample(
            boot_id="b1", monotonic=mono, time=f"t{mono}", mem_available=40 * 1024 ** 3,
            swap_free=0, containers={"m6": used, "mcp": 100 * 1024 ** 2})))
    lines.append(memlog.format_line(memlog.Sample(
        boot_id="b0", monotonic=999, time="old", mem_available=1, swap_free=0,
        containers={"m6": 9 * 1024 ** 3})))
    log.write_text("\n".join(lines) + "\n")
    peaks = memlog.peak_containers(log, 150, "b1")
    assert peaks["m6"] == (3 * 1024 ** 3, "t160")
    assert peaks["mcp"][0] == 100 * 1024 ** 2
    assert memlog.suggest_memory(3 * 1024 ** 3) == "4.5g"
    assert memlog.suggest_memory(100 * 1024 ** 2) == "1g"
    assert memlog.suggest_memory(int(2.1 * 1024 ** 3)) == "3.5g"


def test_summarize_since_matches_the_single_purpose_readers(tmp_path):
    log = tmp_path / "memory.log"
    rows = []
    for mono, avail, used in ((10, 5, 1), (20, 3, 4), (30, 4, 2), (40, 6, 3)):
        rows.append(memlog.format_line(memlog.Sample(
            boot_id="b", monotonic=mono, time=f"t{mono}", mem_available=avail * 1024 ** 3,
            swap_free=0, containers={"c": used * 1024 ** 3})))
    log.write_text("\n".join(rows) + "\n")
    minimum, peaks, tail = memlog.summarize_since(log, 15, "b", last=2)
    assert minimum == memlog.minimum_since(log, 15, "b") == (3 * 1024 ** 3, "t20")
    assert peaks == memlog.peak_containers(log, 15, "b") == {"c": (4 * 1024 ** 3, "t20")}
    assert [s.time for s in tail] == ["t30", "t40"]
