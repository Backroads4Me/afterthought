"""The capture queue: two writers, one dead worker, one budget, one missed hook.

Every test runs against a queue file and session directories under a temporary home, so
nothing here touches ``~/.afterthought`` (BUILD-PLAN 1.5).
"""

from __future__ import annotations

import json
import os
import sqlite3
import statistics
import time
import traceback
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from multiprocessing import get_context
from multiprocessing.synchronize import Barrier
from pathlib import Path

import pytest

from aft import capture, queue
from aft.config import Config
from aft.events import COMPACTION, SESSION_END, SessionEvent
from aft.readers import CLAUDE, PI

# A hook and a digest worker are separate interpreters, so where a test needs a second
# process it starts one, with its own imports and its own connection to the queue file.
SPAWN = get_context("spawn")

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

# DESIGN 8: a capture hook writes its row and exits inside this.
HOOK_BUDGET_MS = 50.0

# One sample is one hook's own write, so the test takes the samples a run of hooks makes.
HOOK_SAMPLES = 20


def event(
    session_id: str = "s1",
    agent: str = CLAUDE,
    kind: str = SESSION_END,
    transcript: object = "/home/ted/.claude/projects/-home-ted/s1.jsonl",
    at: datetime = NOW,
) -> SessionEvent:
    return SessionEvent(
        agent=agent,
        kind=kind,
        session_id=session_id,
        cwd=Path("/home/ted/dev/app"),
        transcript_path=None if transcript is None else Path(str(transcript)),
        time=at,
        detail=None,
    )


def query(config: Config, sql: str, params: tuple = ()) -> list:
    """Rows read straight out of the queue file: how the tests check the table itself."""
    conn = sqlite3.connect(config.home / queue.QUEUE_NAME)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def session_file(
    root: Path,
    agent: str,
    session_id: str,
    cwd: str = "/home/ted/dev/app",
    records: list[dict] | None = None,
    touched: datetime | None = None,
) -> Path:
    """One session file, in the layout that agent keeps and under the name it uses.

    Both agents record the session's working directory near the front of the file, so
    that is the first record here; the rest is whatever the test needs the file to hold.
    """
    directory, name = (
        (root / "-home-ted-dev-app", session_id)
        if agent == CLAUDE
        else (root / "--home-ted-dev-app--", f"2026-10-01T11-59-00-000Z_{session_id}")
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for record in [{"cwd": cwd}, *(records or [{"uuid": "entry-1"}])]:
            handle.write(json.dumps(record) + "\n")
    if touched is not None:
        stamp = touched.timestamp()
        os.utime(path, (stamp, stamp))
    return path


def fill(config: Config, rows: int) -> None:
    """Put ``rows`` jobs in the queue, the bulk of them in one transaction.
    The budget test measures what one hook write costs against a full queue, so the rows
    that make the queue full go in bulk: one commit per row would spend the test's time on
    fsyncs instead of on the thing it claims to measure. The first row goes in through
    ``enqueue``, which is what builds the queue file and its tables.
    """
    queue.enqueue(config, event(session_id="filled-0"))
    conn = sqlite3.connect(config.home / queue.QUEUE_NAME, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            "INSERT INTO jobs (id, agent, session_id, cwd, transcript_path, kind,"
            " event_time, status, created_time, updated_time)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    f"job_{index:06d}",
                    CLAUDE,
                    f"filled-{index}",
                    "/home/ted/dev/app",
                    "/home/ted/.claude/projects/-home-ted/x.jsonl",
                    SESSION_END,
                    NOW.isoformat(),
                    queue.PENDING,
                    NOW.isoformat(),
                    NOW.isoformat(),
                )
                for index in range(1, rows)
            ],
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


def enqueue_in_a_second_process(config: Config, prefix: str, count: int, start: Barrier) -> None:
    """A hook process's whole job, run as the separate process a hook really is."""
    try:
        start.wait(timeout=30)
        for index in range(count):
            queue.enqueue(config, event(session_id=f"{prefix}-{index}"))
    except BaseException:
        traceback.print_exc()
        os._exit(1)
    os._exit(0)


def claim_the_job_and_die(config: Config, claim: Path) -> None:
    """Read a job, name it, and stop dead: no complete, no fail, no cleanup."""
    try:
        job = queue.next_job(config)
        claim.write_text(job.id if job else "")
    except BaseException:
        traceback.print_exc()
    os._exit(0)


def test_two_processes_enqueue_concurrently_without_loss(cfg: Config) -> None:
    """DESIGN 8: Claude Code and pi finishing at the same moment both land their row."""
    start = SPAWN.Barrier(2)
    workers = [
        SPAWN.Process(target=enqueue_in_a_second_process, args=(cfg, prefix, 50, start))
        for prefix in ("claude", "pi")
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=120)

    assert [worker.exitcode for worker in workers] == [0, 0], "a writer lost a row"
    rows = query(cfg, "SELECT session_id, id FROM jobs")
    assert len(rows) == 100, "both processes wrote, so the queue holds both"
    assert len({row[1] for row in rows}) == 100, "every row is its own job"
    expected = {f"{prefix}-{index}" for prefix in ("claude", "pi") for index in range(50)}
    assert {row[0] for row in rows} == expected


def test_a_crash_between_reading_and_completing_leaves_the_job_retryable(
    cfg: Config, tmp_path: Path
) -> None:
    """The retry comes from the row the dead worker left, not from a failure handler."""
    queue.enqueue(cfg, event(session_id="crashed"))
    claim = tmp_path / "claim.txt"
    worker = SPAWN.Process(target=claim_the_job_and_die, args=(cfg, claim))
    worker.start()
    deadline = time.monotonic() + 30
    while not claim.exists():
        assert time.monotonic() < deadline, "the worker never claimed the job"
        time.sleep(0.005)
    pid = worker.pid
    assert pid is not None
    worker.join(timeout=30)

    # What the dead process left behind: a row that names a process that is gone.
    assert query(cfg, "SELECT status, claimed_pid FROM jobs") == [("running", pid)]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)

    retry = queue.next_job(cfg)
    assert retry is not None
    assert retry.id == claim.read_text(), "the same job, not a new one"
    assert query(cfg, "SELECT status, claimed_pid FROM jobs WHERE id = ?", (retry.id,)) == [
        ("running", os.getpid())
    ]

    queue.complete(cfg, retry, "entry-7")
    assert queue.next_job(cfg) is None


def test_the_hook_path_holds_its_budget_with_a_thousand_row_queue(cfg: Config) -> None:
    """DESIGN 8: the capture hook writes one row and returns in under 50 ms."""
    fill(cfg, 1000)
    assert query(cfg, "SELECT count(*) FROM jobs") == [(1000,)]

    samples = []
    for index in range(HOOK_SAMPLES):
        started = time.perf_counter()
        queue.enqueue(cfg, event(session_id=f"hook-{index}"))
        samples.append((time.perf_counter() - started) * 1000)

    worst = max(samples)
    assert worst < HOOK_BUDGET_MS, (
        f"the slowest hook took {worst:.1f} ms against a {HOOK_BUDGET_MS:.0f} ms budget"
        f" (median {statistics.median(samples):.1f} ms,"
        f" fastest {min(samples):.1f} ms, slowest {max(samples):.1f} ms)"
    )
    assert query(cfg, "SELECT count(*) FROM jobs") == [(1000 + HOOK_SAMPLES,)]


def test_the_queue_is_the_file_and_mode_the_design_names(cfg: Config) -> None:
    """DESIGN 8: the queue is ``queue.sqlite`` in the home, in WAL mode."""
    queue.enqueue(cfg, event(session_id="wal", transcript=None))

    assert (cfg.home / "queue.sqlite").exists()
    assert query(cfg, "PRAGMA journal_mode") == [("wal",)]
    assert query(
        cfg,
        "SELECT agent, session_id, cwd, transcript_path, kind, status FROM jobs",
    ) == [(CLAUDE, "wal", "/home/ted/dev/app", None, SESSION_END, queue.PENDING)]


def test_a_watermark_reaches_the_next_job_and_only_complete_moves_it(cfg: Config) -> None:
    """DESIGN 8: one watermark per session, written when its digest is written."""
    queue.enqueue(cfg, event(session_id="wm"))
    first = queue.next_job(cfg)
    assert first is not None and first.watermark is None
    queue.fail(cfg, first, "the reader could not read it")

    assert queue.session_state(cfg, CLAUDE, "wm").watermark is None
    assert query(cfg, "SELECT count(*) FROM sessions") == [(0,)], "nothing here digests"

    queue.enqueue(cfg, event(session_id="wm"))
    retry = queue.next_job(cfg)
    assert retry is not None and retry.watermark is None
    queue.complete(cfg, retry, "entry-42")

    assert queue.session_state(cfg, CLAUDE, "wm").watermark == "entry-42"
    queue.enqueue(cfg, event(session_id="wm"))
    third = queue.next_job(cfg)
    assert third is not None and third.watermark == "entry-42", "the digest reads past it"

    queue.complete(cfg, third, None)
    assert queue.session_state(cfg, CLAUDE, "wm").watermark == "entry-42"


def test_a_failed_job_is_left_behind_for_the_next_capture_to_retry(cfg: Config) -> None:
    """DESIGN 14: a job that could not be digested is reported, not retried at once."""
    queue.enqueue(cfg, event(session_id="broken"))
    job = queue.next_job(cfg)
    assert job is not None
    queue.fail(cfg, job, "no transcript reader for agent 'broken'")

    assert queue.next_job(cfg) is None, "the same run does not go round it again"
    assert query(cfg, "SELECT status, error FROM jobs WHERE id = ?", (job.id,)) == [
        (queue.FAILED, "no transcript reader for agent 'broken'")
    ]

    queue.enqueue(cfg, event(session_id="broken"))
    again = queue.next_job(cfg)
    assert again is not None and again.id != job.id
    assert query(cfg, "SELECT count(*) FROM jobs WHERE status = ?", (queue.FAILED,)) == [(1,)]


def test_a_session_digests_one_chunk_at_a_time_oldest_first(cfg: Config) -> None:
    """One watermark per session, so its chunks go out in order, one in flight."""
    queue.enqueue(cfg, event(session_id="earlier-session"))
    queue.enqueue(cfg, event(session_id="chunks"))
    queue.enqueue(cfg, event(session_id="chunks"))

    first = queue.next_job(cfg)
    assert first is not None and first.session_id == "earlier-session", "oldest work first"
    queue.complete(cfg, first, "entry-1")

    holding = queue.next_job(cfg)
    assert holding is not None and holding.session_id == "chunks" and holding.watermark is None
    assert queue.next_job(cfg) is None, "its next chunk waits for this one's watermark"

    queue.complete(cfg, holding, "entry-9")
    second = queue.next_job(cfg)
    assert second is not None and second.session_id == "chunks"
    assert second.watermark == "entry-9", "the next chunk reads on from the last one's"


def test_the_sweep_finds_a_session_whose_hook_never_fired(
    cfg: Config, tmp_path: Path, monkeypatch
) -> None:
    """DESIGN 8: a session file with no job row at all still becomes capture work."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        capture,
        "SESSION_DIRS",
        {CLAUDE: Path("~/.claude/projects"), PI: Path("~/.pi/agent/sessions")},
    )
    sessions = tmp_path / ".claude/projects"
    touched = NOW - timedelta(minutes=31)
    path = session_file(sessions, CLAUDE, "missed", touched=touched)

    assert capture.sweep(cfg, NOW - timedelta(hours=1)) == []
    assert query(cfg, "SELECT count(*) FROM jobs") == [(0,)], "an opening sweep captures nothing"

    found = capture.sweep(cfg, NOW)

    assert [e.agent for e in found] == [CLAUDE]
    assert found[0].session_id == path.stem
    assert found[0].cwd == Path("/home/ted/dev/app")
    assert found[0].transcript_path == path
    assert found[0].kind == SESSION_END, "a session quiet past the bound is over"
    assert found[0].detail == "sweep"
    assert found[0].time == touched, "the event names when the session last spoke"
    assert query(
        cfg,
        "SELECT agent, session_id, cwd, transcript_path, kind, status FROM jobs",
    ) == [(CLAUDE, path.stem, "/home/ted/dev/app", str(path), SESSION_END, queue.PENDING)]
    assert query(cfg, "SELECT count(*) FROM sessions") == [(0,)]


def test_the_sweep_chunks_a_long_session_that_outgrew_its_watermark(
    cfg: Config, tmp_path: Path, monkeypatch
) -> None:
    """DESIGN 8: a running session is digested in chunks, not all at the end."""
    sessions = tmp_path / "agent-sessions"
    padding = [{"uuid": f"pad-{i}", "text": "x" * 900} for i in range(300)]
    path = session_file(
        sessions,
        PI,
        "long-running",
        records=[{"id": "entry-9", "type": "message"}, *padding],
        touched=NOW - timedelta(minutes=1),
    )
    monkeypatch.setattr(capture, "SESSION_DIRS", {CLAUDE: tmp_path / "no-claude", PI: sessions})
    queue.record_sweep(cfg, NOW - timedelta(minutes=10))

    queue.enqueue(cfg, event(session_id="long-running", agent=PI, transcript=path))
    job = queue.next_job(cfg)
    assert job is not None
    queue.complete(cfg, job, "entry-9")

    found = capture.sweep(cfg, NOW)

    assert [(e.agent, e.session_id, e.kind) for e in found] == [(PI, "long-running", COMPACTION)]
    assert queue.next_job(cfg) is not None


def test_the_sweep_leaves_alone_what_is_quiet_enough_or_already_queued(
    cfg: Config, tmp_path: Path, monkeypatch
) -> None:
    """The bounds it does not cross: untouched, too new and too small, work in flight."""
    sessions = tmp_path / "agent-sessions"
    session_file(sessions, CLAUDE, "untouched", touched=NOW - timedelta(minutes=55))
    session_file(sessions, CLAUDE, "recent", touched=NOW - timedelta(minutes=1))
    waiting = session_file(sessions, CLAUDE, "waiting", touched=NOW - timedelta(minutes=31))
    digesting = session_file(sessions, CLAUDE, "digesting", touched=NOW - timedelta(minutes=31))
    monkeypatch.setattr(capture, "SESSION_DIRS", {CLAUDE: sessions, PI: tmp_path / "no-pi"})
    queue.record_sweep(cfg, NOW - timedelta(minutes=40))

    for session_id, path in (("digesting", digesting), ("waiting", waiting)):
        queue.enqueue(cfg, event(session_id=session_id, transcript=path))
    held = queue.next_job(cfg)
    assert held is not None and held.session_id == "digesting", "the oldest work goes out first"

    assert capture.sweep(cfg, NOW) == []
    assert query(
        cfg,
        "SELECT session_id, status FROM jobs ORDER BY session_id",
    ) == [("digesting", queue.RUNNING), ("waiting", queue.PENDING)]


def hand_claim_to(config: Config, job: queue.Job, pid: int, age: timedelta) -> None:
    """Rewrite a row this process just claimed as one ``pid`` left behind ``age`` ago.

    Age and liveness are what decide a claim, and both are facts of the row, so the
    test stamps them there: who the row says holds it, and when it last moved.
    """
    conn = sqlite3.connect(config.home / queue.QUEUE_NAME, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE jobs SET claimed_pid = ?, updated_time = ? WHERE id = ?",
            (pid, (datetime.now(UTC) - age).isoformat(), job.id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


def cadence(cfg: Config) -> timedelta:
    """The bound a claim ages out on: the cadence the work timer runs at."""
    return timedelta(minutes=cfg.settings.dream.interval_minutes)


def work_cadence(cfg: Config, minutes: int) -> Config:
    """The same home and queue file, with the work timer set to ``minutes``."""
    dream = replace(cfg.settings.dream, interval_minutes=minutes)
    return replace(cfg, settings=replace(cfg.settings, dream=dream))


def test_a_claim_whose_pid_was_recycled_to_a_live_process_ages_out(cfg: Config) -> None:
    """A pid names a process on this machine, not the worker that took the claim."""
    queue.enqueue(cfg, event(session_id="recycled"))
    first = queue.next_job(cfg)
    assert first is not None

    # A running process that never touched this queue: the signal check answers "alive"
    # for it, so only the age of the row can hand the claim back.
    stranger = os.getppid()
    assert stranger != os.getpid()
    os.kill(stranger, 0)
    hand_claim_to(cfg, first, stranger, cadence(cfg) + timedelta(minutes=1))

    retry = queue.next_job(cfg)
    assert retry is not None and retry.id == first.id, "the aged row is work again"
    queue.complete(cfg, retry, "entry-3")
    assert queue.session_state(cfg, CLAUDE, "recycled") == queue.SessionState("entry-3", False)


def test_a_claim_another_account_owns_ages_out(cfg: Config, monkeypatch) -> None:
    """A claimer this account cannot signal is one the signal check can never answer."""
    other_owner = 4242  # a pid only another account could own
    real_kill = os.kill

    def signal_check(pid: int, signal: int) -> None:
        if pid == other_owner:
            raise PermissionError
        real_kill(pid, signal)

    monkeypatch.setattr(queue.os, "kill", signal_check)
    with pytest.raises(PermissionError):
        os.kill(other_owner, 0)

    queue.enqueue(cfg, event(session_id="other-owner"))
    first = queue.next_job(cfg)
    assert first is not None
    hand_claim_to(cfg, first, other_owner, cadence(cfg) + timedelta(minutes=1))

    retry = queue.next_job(cfg)
    assert retry is not None and retry.id == first.id, "the aged row is work again"
    queue.complete(cfg, retry, "entry-3")
    assert queue.session_state(cfg, CLAUDE, "other-owner") == queue.SessionState("entry-3", False)


def test_a_claim_younger_than_one_work_cadence_is_left_alone(cfg: Config) -> None:
    """Age is the bound on a hand-back, so a live worker keeps its row inside one."""
    queue.enqueue(cfg, event(session_id="held"))
    first = queue.next_job(cfg)
    assert first is not None
    holder = os.getppid()
    hand_claim_to(cfg, first, holder, timedelta(minutes=1))

    assert queue.next_job(cfg) is None, "the row is still being digested"
    assert query(cfg, "SELECT status, claimed_pid FROM jobs") == [(queue.RUNNING, holder)]


@pytest.mark.parametrize("minutes", [0, -60])
def test_a_work_cadence_of_zero_or_less_ages_out_no_live_claim(cfg: Config, minutes: int) -> None:
    """A scheduling period cannot drive the lease below the quiet period the design states."""
    hostile = work_cadence(cfg, minutes)
    queue.enqueue(hostile, event(session_id="live-digest"))
    first = queue.next_job(hostile)
    assert first is not None
    holder = os.getppid()
    hand_claim_to(hostile, first, holder, timedelta(minutes=1))

    assert queue.next_job(hostile) is None, "a live worker keeps the row it is digesting"
    assert query(hostile, "SELECT status, claimed_pid FROM jobs") == [(queue.RUNNING, holder)]


def test_a_worker_that_lost_its_claim_cannot_move_the_watermark(cfg: Config) -> None:
    """The digest that reaches its row after it went on has no say over the session."""
    queue.enqueue(cfg, event(session_id="slow-worker"))
    stale = queue.next_job(cfg)
    assert stale is not None
    holder = os.getppid()
    hand_claim_to(cfg, stale, holder, timedelta(0))

    with pytest.raises(queue.QueueError):
        queue.complete(cfg, stale, "entry-99")

    assert queue.session_state(cfg, CLAUDE, "slow-worker") == queue.SessionState(None, True)
    assert query(cfg, "SELECT status, claimed_pid FROM jobs") == [(queue.RUNNING, holder)]


def test_a_worker_that_lost_its_claim_cannot_mark_the_row_failed(cfg: Config) -> None:
    """The row another process is digesting keeps its status and its claimer."""
    queue.enqueue(cfg, event(session_id="slow-failure"))
    stale = queue.next_job(cfg)
    assert stale is not None
    holder = os.getppid()
    hand_claim_to(cfg, stale, holder, timedelta(0))

    with pytest.raises(queue.QueueError):
        queue.fail(cfg, stale, "the model call failed")

    assert query(cfg, "SELECT status, claimed_pid, error FROM jobs") == [
        (queue.RUNNING, holder, None)
    ]
    assert queue.session_state(cfg, CLAUDE, "slow-failure") == queue.SessionState(None, True)


def test_closing_a_row_the_queue_no_longer_holds_is_an_error(cfg: Config) -> None:
    """A queue file removed underneath a worker leaves its rows gone, not closed."""
    queue.enqueue(cfg, event(session_id="vanished"))
    job = queue.next_job(cfg)
    assert job is not None
    (cfg.home / queue.QUEUE_NAME).unlink()

    with pytest.raises(queue.QueueError):
        queue.complete(cfg, job, "entry-8")
    with pytest.raises(queue.QueueError):
        queue.fail(cfg, job, "the model call failed")
    assert query(cfg, "SELECT count(*) FROM sessions") == [(0,)]


def test_a_watermark_quoted_only_as_a_parent_is_no_watermark_at_all(
    cfg: Config, tmp_path: Path, monkeypatch
) -> None:
    """A later record that names the watermark's entry proves the file grew."""
    sessions = tmp_path / "agent-sessions"
    padding = [{"uuid": f"pad-{i}", "text": "x" * 900} for i in range(300)]
    # A pi session resumed with /tree goes back to an entry already digested and appends
    # a new child of it, so the watermark's id survives as that child's parent.
    path = session_file(
        sessions,
        PI,
        "tree-jumped",
        records=[
            {"id": "entry-9", "type": "message"},
            *padding,
            {"id": "branch-1", "parentId": "entry-9", "type": "message"},
        ],
        touched=NOW - timedelta(minutes=1),
    )
    monkeypatch.setattr(capture, "SESSION_DIRS", {CLAUDE: tmp_path / "no-claude", PI: sessions})
    queue.record_sweep(cfg, NOW - timedelta(minutes=10))
    queue.enqueue(cfg, event(session_id="tree-jumped", agent=PI, transcript=path))
    job = queue.next_job(cfg)
    assert job is not None
    queue.complete(cfg, job, "entry-9")

    found = capture.sweep(cfg, NOW)

    assert [(e.agent, e.session_id, e.kind) for e in found] == [(PI, "tree-jumped", COMPACTION)], (
        "the new branch is capture work the watermark must not answer for"
    )
    assert queue.next_job(cfg) is not None
