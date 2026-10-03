"""The capture queue: two writers, one dead worker, one budget, one missed hook.

Every test runs against a queue file and session directories under a temporary home, so
nothing here touches ``~/.afterthought`` (BUILD-PLAN 1.5).
"""

from __future__ import annotations

import json
import math
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

from aft import capture, evidence, queue
from aft.config import Config
from aft.events import COMPACTION, SESSION_END, SessionEvent
from aft.filter import filter_entries
from aft.readers import CLAUDE, PI, Entry
from aft.registry import Registry

# A hook and a digest worker are separate interpreters, so where a test needs a second
# process it starts one, with its own imports and its own connection to the queue file.
SPAWN = get_context("spawn")

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

# DESIGN 8: a capture hook writes its row and exits inside this.
HOOK_BUDGET_MS = 50.0

# The run is long so that its 95th percentile is a sample of the run itself and not one of
# its two extremes.
HOOK_SAMPLES = 100


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

    queue.complete(cfg, retry, "entry-7", off_record=False)
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

    # A loaded host can hold one hook for a whole scheduler slice, so the budget is checked
    # where the run of hooks sits, at its 95th percentile, which lets 5 percent of a run cost
    # more than the budget. The worst sample is named in the failure, so the spike a host
    # caused is still the first thing a reader sees.
    # A shared CI runner's disk stalls for a tenth of a second at a time, which says nothing
    # about the hook, so there the 95th percentile only has to show the hook is not doing
    # work it should not (a scan of the queue grows with its thousand rows); the median must
    # still sit inside the budget everywhere.
    ordered = sorted(samples)
    p95 = ordered[math.ceil(0.95 * len(ordered)) - 1]
    worst = ordered[-1]
    median = statistics.median(samples)
    tail_budget = HOOK_BUDGET_MS * 10 if os.environ.get("CI") else HOOK_BUDGET_MS
    said = f"(worst {worst:.1f} ms, median {median:.1f} ms, fastest {ordered[0]:.1f} ms)"
    assert median < HOOK_BUDGET_MS, f"the median hook took {median:.1f} ms {said}"
    assert p95 < tail_budget, (
        f"a hook at the 95th percentile took {p95:.1f} ms against a "
        f"{tail_budget:.0f} ms bound {said}"
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
    queue.complete(cfg, retry, "entry-42", off_record=False)

    assert queue.session_state(cfg, CLAUDE, "wm").watermark == "entry-42"
    queue.enqueue(cfg, event(session_id="wm"))
    third = queue.next_job(cfg)
    assert third is not None and third.watermark == "entry-42", "the digest reads past it"

    queue.complete(cfg, third, None, off_record=False)
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
    queue.complete(cfg, first, "entry-1", off_record=False)

    holding = queue.next_job(cfg)
    assert holding is not None and holding.session_id == "chunks" and holding.watermark is None
    assert queue.next_job(cfg) is None, "its next chunk waits for this one's watermark"

    queue.complete(cfg, holding, "entry-9", off_record=False)
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
    # The queue stamps a job's claim from its own clock; pin it before the file was last
    # touched, as a running session's is, so the test holds on any date.
    monkeypatch.setattr(queue, "_now", lambda: NOW - timedelta(minutes=5))
    padding = [{"uuid": f"pad-{i}", "text": "x" * 900} for i in range(300)]
    path = session_file(
        sessions,
        PI,
        "long-running",
        records=[{"id": "entry-9", "type": "message"}, *padding],
        touched=NOW - timedelta(minutes=1),
    )
    monkeypatch.setattr(capture, "SESSION_DIRS", {CLAUDE: tmp_path / "no-claude", PI: sessions})
    queue.watching_since(cfg, NOW - timedelta(minutes=10))

    queue.enqueue(cfg, event(session_id="long-running", agent=PI, transcript=path))
    job = queue.next_job(cfg)
    assert job is not None
    queue.complete(cfg, job, "entry-9", off_record=False)

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
    queue.watching_since(cfg, NOW - timedelta(minutes=40))

    for session_id, path in (("digesting", digesting), ("waiting", waiting)):
        queue.enqueue(cfg, event(session_id=session_id, transcript=path))
    held = queue.next_job(cfg)
    assert held is not None and held.session_id == "digesting", "the oldest work goes out first"

    assert capture.sweep(cfg, NOW) == []
    assert query(
        cfg,
        "SELECT session_id, status FROM jobs ORDER BY session_id",
    ) == [("digesting", queue.RUNNING), ("waiting", queue.PENDING)]


def progress(config: Config, session_id: str) -> tuple[str | None, bool, bool]:
    """Where a Claude Code session's digest has read to, and whether work is in flight."""
    state = queue.session_state(config, CLAUDE, session_id)
    return state.watermark, state.off_record, state.has_open_job


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
    queue.complete(cfg, retry, "entry-3", off_record=False)
    assert progress(cfg, "recycled") == ("entry-3", False, False)


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
    queue.complete(cfg, retry, "entry-3", off_record=False)
    assert progress(cfg, "other-owner") == ("entry-3", False, False)


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
        queue.complete(cfg, stale, "entry-99", off_record=False)

    assert progress(cfg, "slow-worker") == (None, False, True)
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
    assert progress(cfg, "slow-failure") == (None, False, True)


def test_closing_a_row_the_queue_no_longer_holds_is_an_error(cfg: Config) -> None:
    """A queue file removed underneath a worker leaves its rows gone, not closed."""
    queue.enqueue(cfg, event(session_id="vanished"))
    job = queue.next_job(cfg)
    assert job is not None
    (cfg.home / queue.QUEUE_NAME).unlink()

    with pytest.raises(queue.QueueError):
        queue.complete(cfg, job, "entry-8", off_record=False)
    with pytest.raises(queue.QueueError):
        queue.fail(cfg, job, "the model call failed")
    assert query(cfg, "SELECT count(*) FROM sessions") == [(0,)]


def test_a_watermark_quoted_only_as_a_parent_is_no_watermark_at_all(
    cfg: Config, tmp_path: Path, monkeypatch
) -> None:
    """A later record that names the watermark's entry proves the file grew."""
    sessions = tmp_path / "agent-sessions"
    # The queue stamps a job's claim from its own clock; pin it before the file was last
    # touched, as a running session's is, so the test holds on any date.
    monkeypatch.setattr(queue, "_now", lambda: NOW - timedelta(minutes=5))
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
    queue.watching_since(cfg, NOW - timedelta(minutes=10))
    queue.enqueue(cfg, event(session_id="tree-jumped", agent=PI, transcript=path))
    job = queue.next_job(cfg)
    assert job is not None
    queue.complete(cfg, job, "entry-9", off_record=False)

    found = capture.sweep(cfg, NOW)

    assert [(e.agent, e.session_id, e.kind) for e in found] == [(PI, "tree-jumped", COMPACTION)], (
        "the new branch is capture work the watermark must not answer for"
    )
    assert queue.next_job(cfg) is not None


# The off-the-record span is the queue's to carry from one chunk of a session to the next,
# beside the watermark it moves with (M2.2, M2.3). Each test here claims one job at a time,
# so which session a claim belongs to is never left to chance.

# Text a session would never want archived, written so it says so itself.
PRIVATE = "the vault passphrase for staging is not-a-real-one at all"


def prompt(text: str, entry_id: str) -> Entry:
    return Entry(entry_id, NOW, "user", text, False)


def test_a_span_open_in_one_session_is_no_other_sessions_span(cfg: Config) -> None:
    """The state is keyed by session, and the second session keeps capturing."""
    queue.enqueue(cfg, event(session_id="off-record"))
    off = queue.next_job(cfg)
    assert off is not None and off.session_id == "off-record"
    assert (off.watermark, off.off_record) == (None, False), "a session starts on the record"
    queue.complete(cfg, off, "o-3", off_record=True)

    queue.enqueue(cfg, event(session_id="ordinary"))
    ordinary = queue.next_job(cfg)
    assert ordinary is not None and ordinary.session_id == "ordinary"
    assert ordinary.off_record is False, "a neighbour's open span is not this session's"
    queue.complete(cfg, ordinary, "n-1", off_record=False)

    queue.enqueue(cfg, event(session_id="off-record"))
    again = queue.next_job(cfg)
    assert again is not None and again.session_id == "off-record"
    assert (again.watermark, again.off_record) == ("o-3", True), "its own span is still open"
    assert progress(cfg, "ordinary") == ("n-1", False, False)


def test_two_agents_sessions_of_one_id_keep_their_own_span(cfg: Config) -> None:
    """The queue holds a session per agent, and so does its span."""
    queue.enqueue(cfg, event(session_id="shared", agent=CLAUDE))
    claude = queue.next_job(cfg)
    assert claude is not None and claude.agent == CLAUDE
    queue.complete(cfg, claude, "c-1", off_record=True)

    queue.enqueue(cfg, event(session_id="shared", agent=PI))
    pi = queue.next_job(cfg)
    assert pi is not None and pi.agent == PI
    assert pi.off_record is False, "the same id in the other agent is a different session"


def test_the_span_and_the_watermark_move_in_one_close_or_not_at_all(cfg: Config) -> None:
    """One statement carries both, so no close leaves them describing different moments."""
    stored = "SELECT watermark, off_record FROM sessions WHERE session_id = ?"

    queue.enqueue(cfg, event(session_id="coupled"))
    first = queue.next_job(cfg)
    assert first is not None
    queue.complete(cfg, first, "c-2", off_record=True)
    assert query(cfg, stored, ("coupled",)) == [("c-2", 1)]

    # A close that moves no watermark moves no span either: the next digest reads those
    # entries again and derives the span from them rather than from a stored half.
    queue.enqueue(cfg, event(session_id="coupled"))
    second = queue.next_job(cfg)
    assert second is not None and second.watermark == "c-2"
    queue.complete(cfg, second, None, off_record=False)
    assert query(cfg, stored, ("coupled",)) == [("c-2", 1)]

    # And a close this process has no part in moves neither half of it.
    queue.enqueue(cfg, event(session_id="coupled"))
    third = queue.next_job(cfg)
    assert third is not None
    hand_claim_to(cfg, third, os.getppid(), timedelta(0))
    with pytest.raises(queue.QueueError):
        queue.complete(cfg, third, "c-9", off_record=False)


def test_a_close_must_name_the_span_state(cfg: Config) -> None:
    """Every close names the span's state: ``off_record`` is a required keyword (M2.3)."""
    stored = "SELECT watermark, off_record FROM sessions WHERE session_id = ?"

    queue.enqueue(cfg, event(session_id="unspoken"))
    first = queue.next_job(cfg)
    assert first is not None
    queue.complete(cfg, first, "u-1", off_record=True)
    queue.enqueue(cfg, event(session_id="unspoken"))
    second = queue.next_job(cfg)
    assert second is not None and second.off_record is True

    with pytest.raises(TypeError):
        queue.complete(cfg, second, "u-9")

    assert query(cfg, stored, ("unspoken",)) == [("u-1", 1)], (
        "the refusal left the last honest close exactly where it stood"
    )
    assert query(cfg, "SELECT status FROM jobs WHERE id = ?", (second.id,)) == [(queue.RUNNING,)], (
        "a refused close never closed the row"
    )


def complete_a_chunk_off_the_record_and_exit(config: Config, closed: Path) -> None:
    """Digest one chunk as a worker process would, and leave the span open behind.

    The process exits without touching a module-level variable the next chunk could be
    served from, so what a later claim finds can only be what the queue file holds.
    """
    try:
        job = queue.next_job(config)
        queue.complete(config, job, "w-4", off_record=True)
        closed.write_text(job.id)
    except BaseException:
        traceback.print_exc()
        os._exit(1)
    os._exit(0)


def test_a_span_one_process_stored_reaches_the_next_process_to_claim(
    cfg: Config, tmp_path: Path
) -> None:
    """The state lives in the queue file, which is what survives a worker exiting."""
    queue.enqueue(cfg, event(session_id="across-process"))
    closed = tmp_path / "closed.txt"
    worker = SPAWN.Process(target=complete_a_chunk_off_the_record_and_exit, args=(cfg, closed))
    worker.start()
    worker.join(timeout=60)
    assert worker.exitcode == 0 and closed.exists(), "the worker never closed its chunk"

    queue.enqueue(cfg, event(session_id="across-process"))
    next_chunk = queue.next_job(cfg)
    assert next_chunk is not None
    assert (next_chunk.watermark, next_chunk.off_record) == ("w-4", True)


def test_a_failed_job_hands_its_open_span_to_the_chunk_that_retries_it(cfg: Config) -> None:
    """A retry resumes the same moment: the span is neither lost nor applied twice."""
    stored = "SELECT watermark, off_record FROM sessions WHERE session_id = ?"

    queue.enqueue(cfg, event(session_id="retried"))
    first = queue.next_job(cfg)
    assert first is not None
    queue.complete(cfg, first, "r-4", off_record=True)

    queue.enqueue(cfg, event(session_id="retried"))
    second = queue.next_job(cfg)
    assert second is not None
    assert (second.watermark, second.off_record) == ("r-4", True)
    queue.fail(cfg, second, "the model call failed")
    assert query(cfg, stored, ("retried",)) == [("r-4", 1)], "a failure moves neither half"

    queue.enqueue(cfg, event(session_id="retried"))
    third = queue.next_job(cfg)
    assert third is not None
    assert (third.watermark, third.off_record) == ("r-4", True), "the retry resumes, not restarts"

    kept, off_record = filter_entries(
        [prompt("aft on", "r-5"), prompt("back to it", "r-6")], cfg, third.off_record
    )
    assert [entry.text for entry in kept] == ["back to it"]
    assert off_record is False, "the one marker closes the span once"
    queue.complete(cfg, third, "r-6", off_record=off_record)
    assert query(cfg, stored, ("retried",)) == [("r-6", 0)]


def test_a_span_opened_in_one_chunk_still_holds_the_next_chunk_of_its_session(
    cfg: Config,
) -> None:
    """The leak's own shape, run the way capture runs it (M2.2, M2.3, DESIGN 12)."""
    queue.enqueue(cfg, event(session_id="leaking"))
    first = queue.next_job(cfg)
    assert first is not None and first.session_id == "leaking"
    kept, off_record = filter_entries(
        [prompt("the plan", "e1"), prompt("aft off", "e2"), prompt(PRIVATE, "e3")], cfg
    )
    evidence.store(cfg, CLAUDE, first.session_id, None, kept)
    queue.complete(cfg, first, "e3", off_record=off_record)
    assert off_record is True, "the chunk ends inside the span it opened"

    queue.enqueue(cfg, event(session_id="ordinary"))
    ordinary = queue.next_job(cfg)
    assert ordinary is not None and ordinary.session_id == "ordinary"
    kept, off_record = filter_entries(
        [prompt("the deploy needs a restart", "n1")], cfg, ordinary.off_record
    )
    assert [entry.text for entry in kept] == ["the deploy needs a restart"]
    evidence.store(cfg, CLAUDE, ordinary.session_id, None, kept)
    queue.complete(cfg, ordinary, "n1", off_record=off_record)

    queue.enqueue(cfg, event(session_id="leaking"))
    second = queue.next_job(cfg)
    assert second is not None and second.session_id == "leaking"
    assert (second.watermark, second.off_record) == ("e3", True)
    kept, off_record = filter_entries(
        [
            prompt(PRIVATE + ", repeated", "e4"),
            prompt("aft on", "e5"),
            prompt("the deploy is next", "e6"),
        ],
        cfg,
        second.off_record,
    )
    assert [entry.text for entry in kept] == ["the deploy is next"]
    assert off_record is False
    evidence.store(cfg, CLAUDE, second.session_id, None, kept)
    queue.complete(cfg, second, "e6", off_record=off_record)

    archived = [entry.text for entry in evidence.entries(cfg, "leaking")]
    assert archived == ["the plan", "the deploy is next"], (
        "the span's text reaches the archive in neither chunk, and what followed its "
        "marker does reach it"
    )
    assert [entry.text for entry in evidence.entries(cfg, "ordinary")] == [
        "the deploy needs a restart"
    ]


def test_a_queue_file_without_span_state_is_refused(cfg: Config) -> None:
    """A queue file from before sessions carried span state is refused, never guessed at."""
    cfg.home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.home / queue.QUEUE_NAME, isolation_level=None)
    conn.execute(
        "CREATE TABLE sessions (agent TEXT NOT NULL, session_id TEXT NOT NULL,"
        " watermark TEXT, PRIMARY KEY (agent, session_id))"
    )
    conn.close()

    with pytest.raises(queue.QueueError, match="earlier build"):
        queue.enqueue(cfg, event(session_id="old"))


def test_a_queue_file_without_the_digest_debt_is_refused(cfg: Config) -> None:
    """A queue written before sessions carried the digest debt is refused, not guessed at.

    The alternative is quieter and worse: archiving would go on leaving its rows waiting -
    which is what stopped capture following an archived worker at all - and the run that
    collects owed digests would read a table with nothing in it (BUILD-PLAN M2.7).
    """
    cfg.home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.home / queue.QUEUE_NAME, isolation_level=None)
    conn.execute(
        "CREATE TABLE sessions (agent TEXT NOT NULL, session_id TEXT NOT NULL,"
        " watermark TEXT, off_record INTEGER NOT NULL DEFAULT 0,"
        " PRIMARY KEY (agent, session_id))"
    )
    conn.close()

    with pytest.raises(queue.QueueError, match="earlier build"):
        queue.enqueue(cfg, event(session_id="old"))


def test_an_owed_digest_is_filed_once_and_settled_by_the_digest(cfg: Config) -> None:
    """The archive's debt becomes one row, not one row per run, and a digest settles it.

    The debt is deliberately not capture work in flight, so capture keeps following the
    session; it is work the digest owes, and filing it twice would hand one chunk out to be
    read twice (BUILD-PLAN M2.7, M2.2's one chunk at a time).
    """
    queue.enqueue(cfg, event(session_id="owed"))
    waiting = queue.pending_jobs(cfg)
    assert [job.session_id for job in waiting] == ["owed"]
    queue.owe_digest(cfg, waiting[0])

    assert queue.session_state(cfg, CLAUDE, "owed").digest_due is True
    assert query(cfg, "SELECT count(*) FROM jobs WHERE status = ?", (queue.PENDING,)) == [(0,)]

    assert queue.redeem_digest_debts(cfg) == 1
    assert queue.redeem_digest_debts(cfg) == 0, "the same debt filed a second row"
    assert queue.session_state(cfg, CLAUDE, "owed").digest_due is True, "a row is not a digest"

    claimed = queue.next_job(cfg)
    assert claimed is not None and claimed.session_id == "owed"
    queue.complete(cfg, claimed, "entry-9", off_record=False)

    assert queue.session_state(cfg, CLAUDE, "owed").digest_due is False


def test_closing_a_waiting_row_owes_no_digest_and_moves_nothing(cfg: Config) -> None:
    """The archiving path's other close: read, filed nowhere, nothing owed (DESIGN 8).

    A session the two-prompt floor turned away has no text in the archive, so a debt would
    hand a chunk of it to a digest of nothing. The watermark and the debt stay exactly where
    they were, so the session that goes on to say more is read from its start - and a row
    another process is digesting is still not this close's to take (M2.7).
    """
    queue.enqueue(cfg, event(session_id="thin"))
    waiting = queue.pending_jobs(cfg)
    assert [job.session_id for job in waiting] == ["thin"]

    queue.close_waiting(cfg, waiting[0])

    assert queue.session_state(cfg, CLAUDE, "thin").digest_due is False
    assert queue.session_state(cfg, CLAUDE, "thin").watermark is None
    assert queue.redeem_digest_debts(cfg) == 0, "a close of nothing owed a digest"
    assert query(cfg, "SELECT count(*) FROM jobs WHERE status = ?", (queue.PENDING,)) == [(0,)]
    assert query(cfg, "SELECT count(*) FROM jobs WHERE status = ?", (queue.DONE,)) == [(1,)]
    assert queue.next_job(cfg) is None, "the closed row came back as work"

    queue.enqueue(cfg, event(session_id="held"))
    claimed = queue.next_job(cfg)
    assert claimed is not None
    with pytest.raises(queue.QueueError, match="no longer waiting"):
        queue.close_waiting(cfg, claimed)


def test_owing_a_digest_to_a_row_another_worker_holds_is_refused(cfg: Config) -> None:
    """A row some other process is digesting is not the archive's to close.

    Closing it would both owe a digest for text a live digest is reading and move the
    session's read-at out from under the run that holds the claim (M2.7, DESIGN 8).
    """
    queue.enqueue(cfg, event(session_id="held"))
    held = queue.next_job(cfg)
    assert held is not None

    with pytest.raises(queue.QueueError, match="no longer waiting"):
        queue.owe_digest(cfg, held)

    assert queue.session_state(cfg, CLAUDE, "held").digest_due is False
    assert query(cfg, "SELECT status FROM jobs") == [(queue.RUNNING,)]


def test_a_debt_whose_digest_failed_is_filed_again(cfg: Config) -> None:
    """A debt is cleared by a digest, not by an attempt at one.

    An archived worker that went quiet has no later sweep to bring its chunk back, so a run
    that gave up on it has to leave the debt owed - or the session's words sit in the archive
    with nothing left anywhere that remembers a digest is missing (BUILD-PLAN M2.7, M2.1).
    """
    queue.enqueue(cfg, event(session_id="dry"))
    queue.owe_digest(cfg, queue.pending_jobs(cfg)[0])
    assert queue.redeem_digest_debts(cfg) == 1
    claimed = queue.next_job(cfg)
    assert claimed is not None

    queue.fail(cfg, claimed, "the transcript would not parse")

    assert queue.session_state(cfg, CLAUDE, "dry").digest_due is True
    assert queue.redeem_digest_debts(cfg) == 1


def sweep_dirs(monkeypatch, sessions: Path, tmp_path: Path) -> None:
    monkeypatch.setattr(capture, "SESSION_DIRS", {CLAUDE: sessions, PI: tmp_path / "no-pi"})


def test_the_sweep_finds_a_session_the_last_sweep_ran_past(
    cfg: Config, tmp_path: Path, monkeypatch
) -> None:
    """A session still talking at one sweep is found by a later one, whatever the cadence:
    each session is measured against its own progress, not the time of the last sweep."""
    sessions = tmp_path / "agent-sessions"
    sweep_dirs(monkeypatch, sessions, tmp_path)
    queue.watching_since(cfg, NOW - timedelta(hours=2))
    path = session_file(sessions, CLAUDE, "slow-to-quiet", touched=NOW - timedelta(minutes=20))

    assert capture.sweep(cfg, NOW - timedelta(minutes=10)) == [], "still talking"
    found = capture.sweep(cfg, NOW + timedelta(minutes=15))

    assert [e.session_id for e in found] == [path.stem]


def test_a_session_untouched_since_its_digest_read_it_is_not_offered_again(
    cfg: Config, tmp_path: Path, monkeypatch
) -> None:
    """A digest that kept nothing moves no watermark, and still ends the work: the file has
    not moved since the digest claimed it. When the session speaks again, it is work."""
    sessions = tmp_path / "agent-sessions"
    sweep_dirs(monkeypatch, sessions, tmp_path)
    monkeypatch.setattr(queue, "_now", lambda: NOW)
    queue.watching_since(cfg, NOW - timedelta(hours=2))
    path = session_file(sessions, CLAUDE, "dry", touched=NOW - timedelta(minutes=31))
    queue.enqueue(cfg, event(session_id="dry", transcript=path))
    job = queue.next_job(cfg)
    assert job is not None
    queue.complete(cfg, job, None, off_record=False)

    assert capture.sweep(cfg, NOW + timedelta(hours=1)) == []

    later = NOW + timedelta(minutes=5)
    os.utime(path, (later.timestamp(), later.timestamp()))
    found = capture.sweep(cfg, NOW + timedelta(hours=1))
    assert [e.session_id for e in found] == ["dry"]


def test_a_failed_session_waits_before_it_is_offered_again(
    cfg: Config, tmp_path: Path, monkeypatch
) -> None:
    sessions = tmp_path / "agent-sessions"
    sweep_dirs(monkeypatch, sessions, tmp_path)
    monkeypatch.setattr(queue, "_now", lambda: NOW)
    queue.watching_since(cfg, NOW - timedelta(hours=2))
    path = session_file(sessions, CLAUDE, "broken", touched=NOW - timedelta(minutes=40))
    queue.enqueue(cfg, event(session_id="broken", transcript=path))
    job = queue.next_job(cfg)
    assert job is not None
    queue.fail(cfg, job, "unrecognized transcript")

    assert capture.sweep(cfg, NOW + timedelta(minutes=10)) == []
    found = capture.sweep(cfg, NOW + capture.RETRY_AFTER + timedelta(minutes=1))
    assert [e.session_id for e in found] == ["broken"]


def test_the_sweep_looks_back_only_over_its_window(
    cfg: Config, tmp_path: Path, monkeypatch
) -> None:
    sessions = tmp_path / "agent-sessions"
    sweep_dirs(monkeypatch, sessions, tmp_path)
    queue.watching_since(cfg, NOW - timedelta(days=30))
    session_file(sessions, CLAUDE, "ancient", touched=NOW - timedelta(days=8))

    assert capture.sweep(cfg, NOW) == []


def test_watching_begins_once(cfg: Config) -> None:
    assert queue.watching_since(cfg, NOW) == NOW
    assert queue.watching_since(cfg, NOW + timedelta(days=1)) == NOW


# A session-start record settles its slug at write time (DESIGN 7, F49)


def test_a_records_slug_is_settled_against_the_registry_and_the_other_paths_claims(
    cfg: Config, tmp_path
) -> None:
    """Two directories of one name never share a digest directory: the second session's
    record is written under the name its parent gives it, decided inside the same write
    that records it, and a re-record of the same session keeps what was settled."""
    registry = Registry(cfg.registry_path)
    first = (tmp_path / "work" / "foo").resolve()
    first.mkdir(parents=True)
    second = (tmp_path / "yard" / "foo").resolve()
    second.mkdir(parents=True)

    assert queue.record_project(cfg, "claude", "s-1", None, "foo", first, registry=registry)
    assert queue.record_project(cfg, "claude", "s-2", None, "foo", second, registry=registry)
    one = queue.recorded_project(cfg, "claude", "s-1")
    two = queue.recorded_project(cfg, "claude", "s-2")
    assert (one.slug, one.path) == ("foo", str(first))
    assert (two.slug, two.path) == ("yard-foo", str(second))

    # The digest that settled the record rewrites it with what it filed under, and the
    # write keeps it: this session's own path does not count as another claim.
    assert queue.record_project(cfg, "claude", "s-2", None, "yard-foo", second, registry=registry)
    assert queue.recorded_project(cfg, "claude", "s-2").slug == "yard-foo"


def test_a_record_that_names_no_path_still_holds_its_name(cfg: Config, tmp_path) -> None:
    """A record an earlier build wrote carries a slug and no directory. The name it holds
    stays taken, so a later directory of that name does not file its digests over the
    first one's."""
    registry = Registry(cfg.registry_path)
    plain = (tmp_path / "other" / "foo").resolve()
    plain.mkdir(parents=True)
    assert queue.record_project(cfg, "claude", "s-old", None, "foo")
    assert queue.record_project(cfg, "claude", "s-new", None, "foo", plain, registry=registry)
    assert queue.recorded_project(cfg, "claude", "s-new").slug == "other-foo"


def test_a_registered_projects_name_is_not_a_free_slug(cfg: Config, tmp_path) -> None:
    """The registry answers for the names it holds: a session in a directory of a taken
    name settles under the name its parent gives it (DESIGN 7, F49)."""
    registry = Registry(cfg.registry_path)
    registry.add("foo")
    plain = (tmp_path / "field" / "foo").resolve()
    plain.mkdir(parents=True)
    assert queue.record_project(cfg, "claude", "s-1", None, "foo", plain, registry=registry)
    assert queue.recorded_project(cfg, "claude", "s-1").slug == "field-foo"
