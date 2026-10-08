import json
import os
import stat

from app.submission_logger import SubmissionLogger


def _entries(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def test_log_arrival_writes_code(tmp_path):
    path = tmp_path / "submissions.jsonl"
    sl = SubmissionLogger(str(path))
    sl.log_arrival("r1", "1.2.3.4", "print 1+1;")

    [entry] = _entries(path)
    assert entry["request_id"] == "r1"
    assert entry["client_ip"] == "1.2.3.4"
    assert entry["code"] == "print 1+1;"
    assert entry["timestamp"]


def test_log_completion_carries_outcome(tmp_path):
    path = tmp_path / "submissions.jsonl"
    sl = SubmissionLogger(str(path))
    sl.log_arrival("r1", "1.2.3.4", "print 1+1;")
    sl.log_completion(
        "r1", exit_code=0, timed_out=False, seccomp_killed=False, elapsed=0.5
    )

    [_, completion] = _entries(path)
    assert completion["request_id"] == "r1"
    assert completion["exit_code"] == 0
    assert completion["timed_out"] is False
    assert completion["seccomp_killed"] is False
    assert completion["elapsed"] == 0.5


def test_log_rejected_is_metadata_only(tmp_path):
    path = tmp_path / "submissions.jsonl"
    sl = SubmissionLogger(str(path))
    sl.log_rejected("r1", "1.2.3.4", "too_large", input_size=5000)

    [entry] = _entries(path)
    assert entry["request_id"] == "r1"
    assert entry["client_ip"] == "1.2.3.4"
    assert entry["reason"] == "too_large"
    assert entry["input_size"] == 5000
    assert "code" not in entry


def test_log_rejected_reasons_carry_through(tmp_path):
    path = tmp_path / "submissions.jsonl"
    sl = SubmissionLogger(str(path))
    sl.log_rejected("r1", "1.2.3.4", "rate_limited", input_size=10)
    sl.log_rejected("r2", "1.2.3.4", "slots_busy", input_size=10)

    entries = _entries(path)
    assert [e["reason"] for e in entries] == ["rate_limited", "slots_busy"]


def test_file_mode_is_0600(tmp_path):
    path = tmp_path / "submissions.jsonl"
    sl = SubmissionLogger(str(path))
    sl.log_arrival("r1", "1.2.3.4", "print 1+1;")

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_empty_path_disables_logging(tmp_path):
    sl = SubmissionLogger("")
    sl.log_arrival("r1", "1.2.3.4", "print 1+1;")
    sl.log_completion("r1", exit_code=0, timed_out=False, seccomp_killed=False, elapsed=0.1)
    sl.log_rejected("r2", "1.2.3.4", "too_large", input_size=10)
    # Nothing to assert on disk; disabling must simply not raise.


def test_unwritable_directory_disables_logging_permanently(tmp_path):
    # A file in place of the parent directory makes mkdir fail every time:
    # a startup failure, which (unlike a later write error) does latch off.
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    path = blocker / "submissions.jsonl"

    sl = SubmissionLogger(str(path))
    sl.log_arrival("r1", "1.2.3.4", "print 1+1;")  # must not raise
    assert not path.exists()

    blocker.unlink()
    blocker.mkdir()
    sl.log_arrival("r2", "1.2.3.4", "print 1+1;")  # still disabled: no retry on startup failure
    assert _entries(path) == []


def test_transient_write_error_does_not_permanently_disable_logging(tmp_path):
    path = tmp_path / "submissions.jsonl"
    sl = SubmissionLogger(str(path))
    sl.log_arrival("r1", "1.1.1.1", "first")  # creates the file

    path.chmod(0o400)  # owner read-only: the next open for append fails
    sl.log_arrival("r2", "2.2.2.2", "second")  # must not raise; warns and skips

    path.chmod(0o600)  # transient condition clears
    sl.log_arrival("r3", "3.3.3.3", "third")  # must succeed: no permanent latch

    entries = _entries(path)
    assert [e["request_id"] for e in entries] == ["r1", "r3"]


def test_multiple_requests_append_in_order(tmp_path):
    path = tmp_path / "submissions.jsonl"
    sl = SubmissionLogger(str(path))
    sl.log_arrival("r1", "1.1.1.1", "a")
    sl.log_arrival("r2", "2.2.2.2", "b")
    sl.log_completion("r1", exit_code=0, timed_out=False, seccomp_killed=False, elapsed=0.1)

    entries = _entries(path)
    assert [e["request_id"] for e in entries] == ["r1", "r2", "r1"]
