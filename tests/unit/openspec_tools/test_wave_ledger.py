"""Unit tests for the wave-status ledger helper (openspec/tools/wave_ledger.py).

Pure-function tests touch no filesystem; filesystem tests use a per-test
`tmp_path` as the `--base`, so the whole module is safe under `pytest -n auto`
(no shared fixed path, no module-level mutable state).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_WL_PATH = Path(__file__).resolve().parents[3] / "openspec" / "tools" / "wave_ledger.py"
_spec = importlib.util.spec_from_file_location("openspec_tools_wave_ledger", _WL_PATH)
assert _spec and _spec.loader
wl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wl)


# --- build_record ----------------------------------------------------------


def test_build_record_running_has_schema_and_timestamp():
    rec = wl.build_record("1", "running", change="add-x", tasks=["1.1", "1.2"], now=0.0)
    assert rec == {
        "wave": "1",
        "change": "add-x",
        "status": "running",
        "tasks": ["1.1", "1.2"],
        "files": [],
        "tests": None,
        "reason": None,
        "updatedAt": "1970-01-01T00:00:00Z",
    }


def test_build_record_done_carries_files_and_tests():
    rec = wl.build_record(
        "2",
        "done",
        change="add-x",
        tasks=["2.1"],
        files=["libs/oci/x.py"],
        tests="pytest: 7 passed",
    )
    assert rec["status"] == "done"
    assert rec["files"] == ["libs/oci/x.py"]
    assert rec["tests"] == "pytest: 7 passed"


def test_build_record_rejects_unknown_status():
    with pytest.raises(ValueError, match="unknown status"):
        wl.build_record("1", "inprogress", change="add-x")


def test_build_record_failed_requires_reason():
    with pytest.raises(ValueError, match="requires a non-empty --reason"):
        wl.build_record("1", "failed", change="add-x")
    # a real reason is accepted
    rec = wl.build_record("1", "failed", change="add-x", reason="boom")
    assert rec["reason"] == "boom"


# --- pure queries ----------------------------------------------------------


def _rec(wave: str, status: str, tasks: list[str]) -> dict[str, Any]:
    return {"wave": wave, "status": status, "tasks": tasks}


def test_completed_task_ids_unions_only_done_waves():
    records = [
        _rec("1", "done", ["1.1", "1.2"]),
        _rec("2", "failed", ["2.1"]),
        _rec("3", "done", ["3.1", "1.2"]),  # overlap dedupes
        _rec("4", "running", ["4.1"]),
    ]
    assert wl.completed_task_ids(records) == {"1.1", "1.2", "3.1"}


def test_outstanding_waves_returns_non_done_in_expected_order():
    records = [_rec("1", "done", []), _rec("2", "failed", []), _rec("3", "running", [])]
    # "4" is expected but absent from the ledger -> outstanding; order follows expected
    assert wl.outstanding_waves(records, ["1", "2", "3", "4"]) == ["2", "3", "4"]


def test_outstanding_waves_all_done_is_empty():
    records = [_rec("1", "done", []), _rec("2", "done", [])]
    assert wl.outstanding_waves(records, ["1", "2"]) == []


def test_summarize_groups_by_status_and_lists_completed_tasks():
    records = [
        _rec("1", "done", ["1.1"]),
        _rec("3", "running", []),
        _rec("2", "done", ["2.1"]),
        _rec("4", "failed", []),
    ]
    summary = wl.summarize(records)
    assert summary["waves"] == 4
    assert summary["byStatus"]["done"] == ["1", "2"]  # sorted
    assert summary["byStatus"]["running"] == ["3"]
    assert summary["byStatus"]["failed"] == ["4"]
    assert summary["completedTasks"] == ["1.1", "2.1"]


# --- filesystem layer ------------------------------------------------------


def test_write_then_read_round_trip(tmp_path):
    rec = wl.build_record("1", "done", change="add-x", tasks=["1.1"], files=["a.py"])
    path = wl.write_record(rec, base=tmp_path)
    assert path.exists()
    assert path.name == "wave-1.json"
    back = wl.read_records("add-x", base=tmp_path)
    assert back == [rec]


def test_write_record_is_atomic_no_temp_left_behind(tmp_path):
    wl.write_record(wl.build_record("1", "running", change="add-x", tasks=["1.1"]), base=tmp_path)
    directory = wl.run_dir("add-x", base=tmp_path)
    # exactly one wave file, and no leftover .tmp sibling
    names = sorted(p.name for p in directory.iterdir())
    assert names == ["wave-1.json"]


def test_run_dir_created_0700(tmp_path):
    directory = wl.run_dir("add-x", base=tmp_path, create=True)
    assert directory.is_dir()
    assert (directory.stat().st_mode & 0o777) == 0o700
    assert (directory.parent.stat().st_mode & 0o777) == 0o700


def test_read_records_missing_dir_is_empty(tmp_path):
    assert wl.read_records("never-created", base=tmp_path) == []


def test_read_records_fails_closed_on_malformed_json(tmp_path):
    directory = wl.run_dir("add-x", base=tmp_path, create=True)
    (directory / "wave-9.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed ledger record"):
        wl.read_records("add-x", base=tmp_path)


def test_read_records_fails_closed_on_non_object(tmp_path):
    directory = wl.run_dir("add-x", base=tmp_path, create=True)
    (directory / "wave-9.json").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="expected a JSON object"):
        wl.read_records("add-x", base=tmp_path)


def test_later_write_supersedes_same_wave(tmp_path):
    wl.write_record(wl.build_record("1", "running", change="add-x", tasks=["1.1"]), base=tmp_path)
    wl.write_record(
        wl.build_record("1", "done", change="add-x", tasks=["1.1"], tests="pytest: 1 passed"),
        base=tmp_path,
    )
    records = wl.read_records("add-x", base=tmp_path)
    assert len(records) == 1
    assert records[0]["status"] == "done"


def test_write_record_requires_change(tmp_path):
    rec = wl.build_record("1", "running", tasks=["1.1"])  # no change
    with pytest.raises(ValueError, match="must carry a 'change'"):
        wl.write_record(rec, base=tmp_path)


# --- path-component safety -------------------------------------------------


@pytest.mark.parametrize("bad", ["../escape", "a/b", "..", "with space", "semi;colon"])
def test_run_dir_rejects_unsafe_change(tmp_path, bad):
    with pytest.raises(ValueError, match="unsafe change"):
        wl.run_dir(bad, base=tmp_path)


def test_write_record_rejects_unsafe_wave(tmp_path):
    rec = wl.build_record("../oops", "running", change="add-x", tasks=[])
    with pytest.raises(ValueError, match="unsafe wave"):
        wl.write_record(rec, base=tmp_path)


# --- CLI round-trip --------------------------------------------------------


def test_cli_record_then_status_json(tmp_path, capsys):
    base = str(tmp_path)
    assert wl.main(["--base", base, "record", "add-x", "1", "done", "--tasks", "1.1,1.2"]) == 0
    capsys.readouterr()  # drop the printed path
    assert wl.main(["--base", base, "record", "add-x", "2", "failed", "--reason", "boom"]) == 0
    capsys.readouterr()

    assert wl.main(["--base", base, "status", "add-x", "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["byStatus"]["done"] == ["1"]
    assert summary["byStatus"]["failed"] == ["2"]
    assert summary["completedTasks"] == ["1.1", "1.2"]


def test_cli_completed_tasks_lists_done_tasks(tmp_path, capsys):
    base = str(tmp_path)
    wl.main(["--base", base, "record", "add-x", "1", "done", "--tasks", "1.1,1.2"])
    capsys.readouterr()
    assert wl.main(["--base", base, "completed-tasks", "add-x"]) == 0
    out = capsys.readouterr().out.split()
    assert out == ["1.1", "1.2"]


def test_cli_outstanding_lists_non_done(tmp_path, capsys):
    base = str(tmp_path)
    wl.main(["--base", base, "record", "add-x", "1", "done", "--tasks", "1.1"])
    capsys.readouterr()
    rc = wl.main(["--base", base, "outstanding", "add-x", "--waves", "1,2,3"])
    assert rc == 0
    assert capsys.readouterr().out.split() == ["2", "3"]


def test_cli_failed_without_reason_exits_2(tmp_path, capsys):
    rc = wl.main(["--base", str(tmp_path), "record", "add-x", "1", "failed"])
    assert rc == 2
    assert "requires a non-empty --reason" in capsys.readouterr().err
