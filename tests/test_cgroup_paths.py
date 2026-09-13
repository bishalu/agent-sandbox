"""systemd nests slices by dash: agent-sandbox.slice sits under agent.slice."""
from agent_sandbox import config, memlog, doctor


def test_slice_parts_nest_by_dash():
    assert config.slice_cgroup_parts("agent-sandbox.slice") == ("agent.slice", "agent-sandbox.slice")
    assert config.slice_cgroup_parts("user.slice") == ("user.slice",)
    assert config.slice_cgroup_parts("a-b-c.slice") == ("a.slice", "a-b.slice", "a-b-c.slice")


def test_slice_cgroup_matches_the_measured_host_layout():
    got = config.slice_cgroup("agent-sandbox.slice", uid=1000)
    assert str(got) == ("/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service"
                        "/agent.slice/agent-sandbox.slice")


def test_memlog_and_doctor_agree_on_the_slice_directory():
    assert memlog.SLICE_CGROUP_ROOT == doctor.slice_cgroup_dir()
    assert memlog.SLICE_CGROUP_ROOT.name == "agent-sandbox.slice"
    assert memlog.SLICE_CGROUP_ROOT.parent.name == "agent.slice"
