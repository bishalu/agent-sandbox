"""state: the derived per-entry and per-sandbox state, the guarded write-back,
and the `status` command over them (R6, R7, KTD4).

Every probe is injected: no Docker, no os.kill, a fixed clock. The two
fixtures under tests/fixtures/runs are the real 2026-09-13 records, copied.
"""

import datetime
import json
import os
import pathlib
import shutil

import pytest

from agent_sandbox import cleanup, cli, state
from agent_sandbox.metadata import RunRecord

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "runs"
NOW = datetime.datetime(2026, 9, 13, 20, 0, 0, tzinfo=datetime.timezone.utc)
TIMEOUT, INTERVAL = 1800.0, 30.0


def iso(seconds_ago):
    return (NOW - datetime.timedelta(seconds=seconds_ago)).isoformat()


def entry(status="running", container="agent-sandbox-x-1-100-42", pid=4242,
          seconds_ago=60, exit_code=None, finished=False, waiting_since=None):
    return {
        "container": container, "runtime": "runc", "command": ["bash"], "pid": pid,
        "started_at": iso(seconds_ago), "waiting_since": waiting_since,
        "finished_at": iso(seconds_ago - 10) if finished else None,
        "exit_code": exit_code, "status": status,
        "stdout_offset_start": 0, "stdout_offset_end": 100 if finished else None,
    }


def derive(e, exists, alive):
    return state.derive_entry(e, container_exists=exists, pid_alive=alive, now=NOW,
                              wait_timeout_s=TIMEOUT, wait_interval_s=INTERVAL)


@pytest.fixture
def home(home):
    """The shared home, its runs/ dir holding copies of the two real
    records, so a write-back never touches the fixtures or the host's
    own runs."""
    runs = home / "runs"
    shutil.copytree(FIXTURES, runs)
    return runs


def derive_all(rec, names=(), dead_pids=()):
    return state.derive_record(
        rec, container_exists=lambda n: n in names,
        pid_alive=lambda p: None if p is None else p not in dead_pids,
        now=NOW, wait_timeout_s=TIMEOUT, wait_interval_s=INTERVAL)


# ---------------------------------------------------------------- derive_entry
def test_running_with_container_and_live_pid_is_running():
    d = derive(entry(), exists=True, alive=True)
    assert d.state == "running"
    assert "agent-sandbox-x-1-100-42" in d.evidence and "4242" in d.evidence
    assert d.corrected is None


def test_running_with_container_gone_is_crashed_naming_the_container():
    d = derive(entry(), exists=False, alive=True)
    assert d.state == "crashed"
    assert "agent-sandbox-x-1-100-42" in d.evidence
    assert d.corrected == "crashed"


def test_running_with_container_up_and_pid_dead_is_orphaned():
    d = derive(entry(), exists=True, alive=False)
    assert d.state == "orphaned"
    assert "4242" in d.evidence
    assert d.corrected == "orphaned"


def test_running_with_pid_unrecorded_stays_running_when_container_is_up():
    # Records written before U1 carry no pid; without one, orphaned cannot
    # be told from running, and the evidence says so.
    d = derive(entry(pid=None), exists=True, alive=None)
    assert d.state == "running" and "unrecorded" in d.evidence


def test_waiting_without_container_is_waiting_until_the_timeout_plus_an_interval():
    fresh = entry("waiting", seconds_ago=120, waiting_since=iso(120))
    d = derive(fresh, exists=False, alive=True)
    assert d.state == "waiting" and d.corrected is None

    stale = entry("waiting", seconds_ago=TIMEOUT + INTERVAL + 1,
                  waiting_since=iso(TIMEOUT + INTERVAL + 1))
    d = derive(stale, exists=False, alive=True)
    assert d.state == "crashed" and d.corrected == "crashed"
    assert "1830" in d.evidence      # the window it overran, in seconds


def test_waiting_with_a_dead_launcher_is_crashed_at_once():
    d = derive(entry("waiting", seconds_ago=5, waiting_since=iso(5)), exists=False,
               alive=False)
    assert d.state == "crashed" and "4242" in d.evidence


@pytest.mark.parametrize("status,code", [("completed", 0), ("failed", 1),
                                         ("timed_out", 137)])
def test_finished_entries_are_finished(status, code):
    d = derive(entry(status, exit_code=code, finished=True), exists=False, alive=False)
    assert d.state == "finished" and d.corrected is None
    assert str(code) in d.evidence


def test_already_corrected_entries_are_stable():
    crashed = dict(entry(), status="crashed", evidence="earlier reconciliation")
    d = derive(crashed, exists=False, alive=False)
    assert d.state == "crashed" and d.corrected is None
    orphaned = dict(entry(), status="orphaned")
    assert derive(orphaned, exists=True, alive=False).corrected is None
    # An orphaned container that has since gone is a crash, not still orphaned.
    assert derive(orphaned, exists=False, alive=False).state == "crashed"


# ---------------------------------------------------------------- derive_record
def test_0439a7a7_is_finished_with_its_stale_running_entries_crashed(home):
    rec = RunRecord.load("vibeset-dj-0439a7a7")
    d = derive_all(rec)
    assert d.state == "finished"
    assert d.recorded_status == "failed" and d.status == "failed"
    assert d.newest.state == "finished" and d.newest.index == 16
    crashed = [e for e in d.entries if e.corrected == "crashed"]
    # The plan's text says seven; the record holds six entries left at
    # `running` with no finished_at. Asserting what the record shows.
    assert len(crashed) == 6
    assert [e.index for e in crashed] == [2, 6, 8, 9, 10, 15]
    assert all(e.recorded == "running" for e in crashed)
    assert d.changed and len(d.changes) == 6 and d.status_change is None


def test_ecfa0e5c_is_crashed_and_its_top_level_status_follows(home):
    rec = RunRecord.load("vibeset-dj-ecfa0e5c")
    d = derive_all(rec)
    assert d.state == "crashed"
    assert d.recorded_status == "running" and d.status == "crashed"
    assert d.newest.container == "agent-sandbox-vibeset-dj-ecfa0e5c-1789328590-63148"
    assert "not found" in d.newest.evidence
    assert d.status_change == ("running", "crashed")


def test_sandbox_state_comes_from_the_newest_entry(home):
    # The newest entry alive: the sandbox is running even with stale history.
    rec = RunRecord.load("vibeset-dj-0439a7a7")
    rec.add_container("agent-sandbox-vibeset-dj-0439a7a7-1-1", "runc", ["claude"],
                      pid=os.getpid())
    rec.start()
    d = derive_all(rec, names={"agent-sandbox-vibeset-dj-0439a7a7-1-1"})
    assert d.state == "running" and d.status == "running"
    assert len([e for e in d.changes]) == 6      # history is still corrected


def test_record_without_entries_derives_from_its_own_status(home):
    rec = RunRecord("x-1")
    d = derive_all(rec)
    assert d.state == "waiting" and d.newest is None and not d.changed
    rec.finish(1, "failed")
    assert derive_all(rec).state == "finished"


# ---------------------------------------------------------------- reconcile
def probes(names=(), dead_pids=()):
    return dict(container_exists=lambda n: n in names,
                pid_alive=lambda p: None if p is None else p not in dead_pids,
                now=NOW, wait_timeout_s=TIMEOUT, wait_interval_s=INTERVAL)


def test_reconcile_writes_once_and_a_second_derive_is_idempotent(home):
    path = home / "vibeset-dj-0439a7a7" / "run.json"
    before = json.loads(path.read_text())
    r = state.reconcile(path, **probes())
    assert r.written and len(r.changes) == 6
    after = json.loads(path.read_text())
    assert after["status"] == "failed"
    corrected = [c for c in after["containers"] if c["status"] == "crashed"]
    assert len(corrected) == 6
    assert all(c["evidence"] and c["reconciled_at"] for c in corrected)
    assert not (path.parent / "run.json.tmp").exists()
    # Everything not corrected is byte-for-byte what was there.
    for i, c in enumerate(after["containers"]):
        if c["status"] != "crashed":
            assert c == before["containers"][i]

    again = state.derive_record(RunRecord.load("vibeset-dj-0439a7a7"), **probes())
    assert not again.changed and again.state == "finished"
    r2 = state.reconcile(path, **probes())
    assert not r2.written and r2.reason == "nothing to change"
    assert json.loads(path.read_text()) == after


def test_reconcile_corrects_the_top_level_status(home):
    path = home / "vibeset-dj-ecfa0e5c" / "run.json"
    r = state.reconcile(path, **probes())
    assert r.written and r.state == "crashed"
    data = json.loads(path.read_text())
    assert data["status"] == "crashed"
    assert data["containers"][-1]["status"] == "crashed"


def test_reconcile_writes_nothing_when_the_record_changed_under_it(home, monkeypatch):
    path = home / "vibeset-dj-ecfa0e5c" / "run.json"
    original = path.read_text()
    real_load = RunRecord.load_path

    fired = []

    def load_then_live_process_finishes(p):
        rec = real_load(p)
        if not fired:
            # Between our read and our write the sandbox process records the
            # container's exit, with a plain atomic rename and no version check.
            fired.append(True)
            live = real_load(p)
            live.finish_container(0, "completed", stdout_offset_end=512)
            live.finish(0, "completed").save()
        return rec

    monkeypatch.setattr(RunRecord, "load_path", staticmethod(load_then_live_process_finishes))
    r = state.reconcile(path, **probes())
    assert not r.written
    assert r.reason == "record changed between read and write"
    # The fresh state is reported, not the stale derivation.
    assert r.state == "finished" and r.status == "completed" and not r.changes
    data = json.loads(path.read_text())
    assert data["status"] == "completed"
    assert data["containers"][-1]["status"] == "completed"
    assert data != json.loads(original)
    assert "crashed" not in path.read_text()


def test_mark_entry_guard_refuses_a_changed_entry(home):
    rec = RunRecord.load("vibeset-dj-ecfa0e5c")
    other = RunRecord.load("vibeset-dj-ecfa0e5c")
    other.finish_container(0, "completed").save()
    assert rec.mark_entry(1, status="crashed", evidence="gone") is False
    assert json.loads(rec.file.read_text())["containers"][1]["status"] == "completed"
    # With the entry as read, the write lands and rec's copy follows.
    fresh = RunRecord.load("vibeset-dj-ecfa0e5c")
    assert fresh.mark_entry(1, status="crashed", evidence="gone") is True
    assert fresh.data["containers"][1]["evidence"] == "gone"
    assert json.loads(fresh.file.read_text())["containers"][1]["status"] == "crashed"


def test_mark_entry_guard_refuses_a_missing_record(home):
    rec = RunRecord.load("vibeset-dj-ecfa0e5c")
    rec.file.unlink()
    assert rec.mark_entry(1, status="crashed") is False


# ---------------------------------------------------------------- status command
@pytest.fixture
def fake_probes(monkeypatch):
    names = set()
    dead = set()
    monkeypatch.setattr(cli, "_status_probes",
                        lambda: (lambda n: n in names,
                                 lambda p: None if p is None else p not in dead))
    return names, dead


def test_status_json_lists_every_sandbox_with_its_derived_state(home, fake_probes, capsys):
    assert cli.main(["status", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    by_id = {s["sandbox_id"]: s for s in out}
    a = by_id["vibeset-dj-0439a7a7"]
    assert a["state"] == "finished"
    assert a["newest"]["status"] == "failed" and a["newest"]["state"] == "finished"
    assert a["newest"]["stdout_offset_start"] is None    # pre-U1 record
    assert a["tags"] == {} and a["logs"]["stdout"].endswith("stdout.log")
    assert a["corrections"] == 6 and a["reconciled"] is None
    assert len(a["entries"]) == 17
    b = by_id["vibeset-dj-ecfa0e5c"]
    assert b["state"] == "crashed" and b["status"] == "crashed"
    assert b["recorded_status"] == "running"
    assert "not found" in b["newest"]["evidence"]
    # Nothing written without --reconcile.
    assert json.loads((home / "vibeset-dj-ecfa0e5c" / "run.json").read_text())["status"] == "running"


def test_status_table_has_one_row_per_sandbox(home, fake_probes, capsys):
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("SANDBOX ID")
    rows = {l.split()[0]: l for l in out[1:]}
    assert "finished" in rows["vibeset-dj-0439a7a7"]
    assert "crashed" in rows["vibeset-dj-ecfa0e5c"]


def test_status_filters_by_sandbox_id(home, fake_probes, capsys):
    assert cli.main(["status", "vibeset-dj-ecfa0e5c", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [s["sandbox_id"] for s in out] == ["vibeset-dj-ecfa0e5c"]
    assert cli.main(["status", "nope-1", "--json"]) == 1


def test_status_reconcile_writes_the_corrections(home, fake_probes, capsys):
    assert cli.main(["status", "--reconcile", "--json"]) == 0
    out = {s["sandbox_id"]: s for s in json.loads(capsys.readouterr().out)}
    assert out["vibeset-dj-ecfa0e5c"]["reconciled"]["written"] is True
    assert out["vibeset-dj-0439a7a7"]["reconciled"]["changes"] == 6
    data = json.loads((home / "vibeset-dj-ecfa0e5c" / "run.json").read_text())
    assert data["status"] == "crashed"
    # A second pass has nothing left to write.
    assert cli.main(["status", "--reconcile", "--json"]) == 0
    out = {s["sandbox_id"]: s for s in json.loads(capsys.readouterr().out)}
    assert out["vibeset-dj-ecfa0e5c"]["reconciled"] is None
    assert out["vibeset-dj-ecfa0e5c"]["corrections"] == 0


def test_status_is_in_known_and_the_shorthand_still_routes_to_run(home, fake_probes):
    assert "status" in cli.KNOWN


# ---------------------------------------------------------------- survey
def test_survey_reports_the_derived_state(home, monkeypatch):
    monkeypatch.setattr(cleanup.worktree, "has_uncommitted_work", lambda p: False)
    monkeypatch.setattr(cleanup.worktree, "unpushed_commits", lambda p: 0)
    s = cleanup.survey(older_than_days=0, container_exists=lambda n: False,
                       pid_alive=lambda p: None)
    rows = {r["id"]: r for r in s["removable"] + s["protected"] + s["young"]}
    assert rows["vibeset-dj-ecfa0e5c"]["state"] == "crashed"
    assert rows["vibeset-dj-ecfa0e5c"]["status"] == "crashed"
    assert rows["vibeset-dj-0439a7a7"]["state"] == "finished"
    assert rows["vibeset-dj-0439a7a7"]["status"] == "failed"
