"""The capture queue: two writers, one dead worker, one budget, one missed hook.

Every test runs against a queue file and session directories under a temporary home, so
nothing here touches ``~/.afterthought`` (BUILD-PLAN 1.5).
"""

from __future__ import annotations

import builtins
import io
import json
import os
import sqlite3
import statistics
import sys
import time
import traceback
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from multiprocessing import get_context
from multiprocessing.synchronize import Barrier
from pathlib import Path

import pytest

from aft import capture, model, queue, readers
from aft.cli import main
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

# The timestamp every record of a hand-written session file carries. The sweep reads
# which entries a file holds and in what order, not when each turn happened.
TURN_TIME = "2026-10-01T11:59:00+00:00"

# The payloads `aft hook` is handed, captured from the agents themselves (M1.1).
FIXTURES = Path(__file__).parent / "fixtures" / "hooks"

# The lifecycle events that mean capture work, as each agent names them, and the kind
# each normalizes to.
CAPTURE_EVENTS = [
    ("claude", "PreCompact", "claude-pre-compact.json", COMPACTION),
    ("claude", "SessionEnd", "claude-session-end.json", SESSION_END),
    ("pi", "session_before_compact", "pi-session-before-compact.json", COMPACTION),
    ("pi", "session_shutdown", "pi-session-shutdown.json", SESSION_END),
]


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


def conversation(agent: str, turn_ids: list[str], *, pad: int = 0) -> list[dict]:
    """Records the readers recognize as entries, one record per id, in the order given.

    A file that holds no conversation is one the readers cannot read at all, so a test
    that asks what a session's entries are - which is what the sweep's watermark check
    answers - has to hand over real ones. pi chains ``parentId`` because only the branch
    ending at its last record is that session's conversation (`readers/pi.py`). ``pad``
    lengthens each turn's text, which is how a test reaches a file past the sweep's
    growth bound out of turns a reader reads rather than out of filler it cannot.
    """
    texts = [f"turn {index}{'x' * pad}" for index in range(len(turn_ids))]
    if agent == CLAUDE:
        return [
            {
                "type": "user",
                "uuid": entry_id,
                "timestamp": TURN_TIME,
                "message": {"content": texts[index]},
            }
            for index, entry_id in enumerate(turn_ids)
        ]
    return [
        {
            "type": "message",
            "id": entry_id,
            "parentId": turn_ids[index - 1] if index else None,
            "timestamp": TURN_TIME,
            "message": {"role": "user", "content": texts[index]},
        }
        for index, entry_id in enumerate(turn_ids)
    ]


def digest_to(
    config: Config, agent: str, session_id: str, transcript: Path, watermark: str
) -> None:
    """Capture and digest one session as far as ``watermark``, through the queue's own calls.

    The sessions table is never written by hand: a watermark arrives only through
    ``complete``, which is the rule these tests are checking.
    """
    queue.enqueue(config, event(session_id=session_id, agent=agent, transcript=transcript))
    job = queue.next_job(config)
    assert job is not None and job.session_id == session_id, "the row the test queued is work"
    queue.complete(config, job, watermark)


class ReaderCalls:
    """Counts the calls a sweep makes at the reader's door, and delegates to the reader."""

    def __init__(self, read) -> None:
        self.count = 0
        self.read = read

    def __call__(self, agent: str, path: Path, after: str | None = None):
        self.count += 1
        return self.read(agent, path, after)


def count_reads(monkeypatch) -> ReaderCalls:
    """Wrap the reader the sweep calls, so a test can count the transcripts it parses."""
    calls = ReaderCalls(capture.readers.read)
    monkeypatch.setattr(capture.readers, "read", calls)
    return calls


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
    path = session_file(
        sessions,
        CLAUDE,
        "missed",
        records=conversation(CLAUDE, ["entry-1", "entry-2", "entry-3"]),
        touched=touched,
    )

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


def test_the_first_sweep_of_a_home_watches_it_without_capturing_its_history(
    cfg: Config, tmp_path: Path, monkeypatch
) -> None:
    """Two sweeps, counted: the net is reachable and it does not flood on install.

    afterthought arrives on a home holding a week of sessions no hook ever reported. The
    opening sweep records that the watching has begun and captures none of them; the very
    next sweep runs the ordinary rule, so the one session that went on talking after the
    watching began is capture work and the ones that stopped before it never are
    (BUILD-PLAN M2.2).
    """
    sessions = tmp_path / "agent-sessions"
    monkeypatch.setattr(capture, "SESSION_DIRS", {CLAUDE: sessions, PI: tmp_path / "no-pi"})
    turns = conversation(CLAUDE, ["entry-1", "entry-2", "entry-3"])
    session_file(
        sessions,
        CLAUDE,
        "stopped-before-install",
        records=turns,
        touched=NOW - timedelta(days=2),
    )
    session_file(
        sessions,
        CLAUDE,
        "stopped-before-install-too",
        records=turns,
        touched=NOW - timedelta(days=3),
    )

    assert capture.sweep(cfg, NOW) == []
    assert query(cfg, "SELECT count(*) FROM jobs") == [(0,)], "no history on the first tick"

    # One of the two sessions goes on talking after the watching begins, then goes quiet.
    session_file(
        sessions,
        CLAUDE,
        "stopped-before-install",
        records=[*turns, *conversation(CLAUDE, ["entry-4"])],
        touched=NOW + timedelta(minutes=1),
    )
    found = capture.sweep(cfg, NOW + timedelta(minutes=31))

    assert [(e.session_id, e.kind) for e in found] == [("stopped-before-install", SESSION_END)], (
        "the file moved once the home was being watched, then went quiet"
    )
    assert query(cfg, "SELECT count(*) FROM jobs WHERE status = ?", (queue.PENDING,)) == [(1,)]


def test_the_sweep_chunks_a_long_session_that_outgrew_its_watermark(
    cfg: Config, tmp_path: Path, monkeypatch
) -> None:
    """DESIGN 8: a running session is digested in chunks, not all at the end."""
    sessions = tmp_path / "agent-sessions"
    # Past the 200 KB the sweep calls a chunk worth, out of turns the reader reads, with
    # the digested entry behind the tail it looks at.
    path = session_file(
        sessions,
        PI,
        "long-running",
        records=conversation(PI, ["entry-9", *[f"turn-{i}" for i in range(300)]], pad=900),
        touched=NOW - timedelta(minutes=1),
    )
    monkeypatch.setattr(capture, "SESSION_DIRS", {CLAUDE: tmp_path / "no-claude", PI: sessions})
    queue.begin_watching(cfg, NOW - timedelta(minutes=10))

    digest_to(cfg, PI, "long-running", path, "entry-9")

    found = capture.sweep(cfg, NOW)

    assert [(e.agent, e.session_id, e.kind) for e in found] == [(PI, "long-running", COMPACTION)]
    assert queue.next_job(cfg) is not None


def test_the_sweep_leaves_alone_what_is_quiet_enough_or_already_queued(
    cfg: Config, tmp_path: Path, monkeypatch
) -> None:
    """The bounds it does not cross: too new and too small, and work already in flight."""
    sessions = tmp_path / "agent-sessions"
    turns = conversation(CLAUDE, ["entry-1", "entry-2", "entry-3"])
    session_file(sessions, CLAUDE, "recent", records=turns, touched=NOW - timedelta(minutes=1))
    waiting = session_file(
        sessions, CLAUDE, "waiting", records=turns, touched=NOW - timedelta(minutes=31)
    )
    digesting = session_file(
        sessions, CLAUDE, "digesting", records=turns, touched=NOW - timedelta(minutes=31)
    )
    monkeypatch.setattr(capture, "SESSION_DIRS", {CLAUDE: sessions, PI: tmp_path / "no-pi"})
    queue.begin_watching(cfg, NOW - timedelta(minutes=40))

    for session_id, path in (("digesting", digesting), ("waiting", waiting)):
        queue.enqueue(cfg, event(session_id=session_id, transcript=path))
    held = queue.next_job(cfg)
    assert held is not None and held.session_id == "digesting", "the oldest work goes out first"

    assert capture.sweep(cfg, NOW) == []
    assert query(
        cfg,
        "SELECT session_id, status FROM jobs ORDER BY session_id",
    ) == [("digesting", queue.RUNNING), ("waiting", queue.PENDING)]


def test_the_sweep_finds_a_session_the_last_sweep_ran_past(
    cfg: Config, tmp_path: Path, monkeypatch
) -> None:
    """BUILD-PLAN M2.2: a session is measured against its own watermark, not against the
    time of the last sweep, which is what makes the missed-hook net reachable at all.

    The file went quiet before the last sweep ran and has not moved since, which is what a
    session whose hook never fired looks like to a sweep that runs every ten minutes:
    compared with the time of that sweep it stays invisible for good, and compared with
    its own watermark - the second of its three turns - it is work.
    """
    sessions = tmp_path / "agent-sessions"
    monkeypatch.setattr(capture, "SESSION_DIRS", {CLAUDE: sessions, PI: tmp_path / "no-pi"})
    path = session_file(
        sessions,
        CLAUDE,
        "watermark-behind",
        records=conversation(CLAUDE, ["entry-1", "entry-2", "entry-3"]),
        touched=NOW - timedelta(minutes=60),
    )
    digest_to(cfg, CLAUDE, "watermark-behind", path, "entry-2")
    queue.begin_watching(cfg, NOW - timedelta(minutes=20))

    found = capture.sweep(cfg, NOW)

    assert [(e.agent, e.session_id, e.kind) for e in found] == [
        (CLAUDE, "watermark-behind", SESSION_END)
    ], "quiet past the bound, so the session reads as over"
    assert query(
        cfg,
        "SELECT session_id, kind, status FROM jobs WHERE status = ?",
        (queue.PENDING,),
    ) == [("watermark-behind", SESSION_END, queue.PENDING)]


def test_the_sweep_stops_at_the_seven_days_it_is_given_and_goes_no_further(
    cfg: Config, tmp_path: Path, monkeypatch
) -> None:
    """The window the plan names is a bound in both directions (BUILD-PLAN M2.2).

    Both sessions hold the same undigested turn, so only the age of the file tells them
    apart: eight days of quiet is left alone, six days of it is capture work.
    """
    sessions = tmp_path / "agent-sessions"
    monkeypatch.setattr(capture, "SESSION_DIRS", {CLAUDE: sessions, PI: tmp_path / "no-pi"})
    turns = ["entry-1", "entry-2", "entry-3"]
    stale = session_file(
        sessions,
        CLAUDE,
        "eight-days-old",
        records=conversation(CLAUDE, turns),
        touched=NOW - timedelta(days=8),
    )
    inside = session_file(
        sessions,
        CLAUDE,
        "six-days-old",
        records=conversation(CLAUDE, turns),
        touched=NOW - timedelta(days=6),
    )
    digest_to(cfg, CLAUDE, "eight-days-old", stale, "entry-1")
    digest_to(cfg, CLAUDE, "six-days-old", inside, "entry-1")
    queue.begin_watching(cfg, NOW - timedelta(days=1))

    found = capture.sweep(cfg, NOW)

    assert [(e.agent, e.session_id) for e in found] == [(CLAUDE, "six-days-old")]


def test_the_sweep_enqueues_nothing_whose_watermark_names_its_last_entry(
    cfg: Config, tmp_path: Path, monkeypatch
) -> None:
    """A session whose digest reached the end of its file is not work, however it looks.

    One session is mid-conversation and one has been quiet for most of an hour, so the two
    bounds the sweep offers are both met by neither: a watermark that names the last
    entry answers for the session however the timer next happens to run, and a sweep that
    handed out a job on every pass would be its own failure.
    """
    sessions = tmp_path / "agent-sessions"
    monkeypatch.setattr(capture, "SESSION_DIRS", {CLAUDE: sessions, PI: tmp_path / "no-pi"})
    turns = ["entry-1", "entry-2", "entry-3"]
    talking = session_file(
        sessions,
        CLAUDE,
        "mid-conversation",
        records=conversation(CLAUDE, turns),
        touched=NOW,
    )
    quiet = session_file(
        sessions,
        CLAUDE,
        "quiet-and-digested",
        records=conversation(CLAUDE, turns),
        touched=NOW - timedelta(minutes=40),
    )
    digest_to(cfg, CLAUDE, "mid-conversation", talking, "entry-3")
    digest_to(cfg, CLAUDE, "quiet-and-digested", quiet, "entry-3")
    queue.begin_watching(cfg, NOW - timedelta(days=1))

    assert capture.sweep(cfg, NOW) == []
    assert query(cfg, "SELECT status, count(*) FROM jobs GROUP BY status") == [(queue.DONE, 2)], (
        "the queue holds the two digests, not a job the sweep invented"
    )


def test_the_sweep_reads_a_session_file_only_when_that_file_has_moved(
    cfg: Config, tmp_path: Path, monkeypatch
) -> None:
    """A sweep that runs on a timer over a week of sessions reads an unchanged
    transcript once and never again until it changes (BUILD-PLAN M2.2).

    Counted at the reader's own door: a digested session and a session the queue has never
    digested are each parsed once, to find nothing to do, and a second sweep over the same
    untouched directory parses nothing at all. The session going on talking is what earns
    the next read, and the capture that comes with it.
    """
    sessions = tmp_path / "agent-sessions"
    monkeypatch.setattr(capture, "SESSION_DIRS", {CLAUDE: sessions, PI: tmp_path / "no-pi"})
    turns = conversation(CLAUDE, ["entry-1", "entry-2", "entry-3"])
    path = session_file(
        sessions,
        CLAUDE,
        "quiet-and-digested",
        records=turns,
        touched=NOW - timedelta(minutes=40),
    )
    # A session the queue has never digested, whose records are all bookkeeping: readable,
    # and with no entry for any watermark to be behind.
    session_file(
        sessions,
        CLAUDE,
        "bookkeeping-only",
        records=[{"type": "mode", "mode": "code"}],
        touched=NOW - timedelta(minutes=40),
    )
    digest_to(cfg, CLAUDE, "quiet-and-digested", path, "entry-3")
    queue.begin_watching(cfg, NOW - timedelta(hours=1))
    reads = count_reads(monkeypatch)

    assert capture.sweep(cfg, NOW) == []
    assert reads.count == 2, "each file read once, to find nothing left to digest"
    assert query(
        cfg,
        "SELECT session_id, watermark IS NULL, seen_size IS NOT NULL,"
        " seen_watermark IS watermark FROM sessions ORDER BY session_id",
    ) == [("bookkeeping-only", 1, 1, 1), ("quiet-and-digested", 0, 1, 1)], (
        "each file is observed at the watermark it was read against, and no watermark moved"
    )

    assert capture.sweep(cfg, NOW + timedelta(minutes=5)) == []
    assert reads.count == 2, "the same bytes hold the same answers"

    session_file(
        sessions,
        CLAUDE,
        "quiet-and-digested",
        records=[*turns, *conversation(CLAUDE, ["entry-4"])],
        touched=NOW + timedelta(minutes=5),
    )
    found = capture.sweep(cfg, NOW + timedelta(minutes=35))

    assert [e.session_id for e in found] == ["quiet-and-digested"]
    assert reads.count == 3, "the file that moved is the one worth reading"


def test_a_session_that_keeps_failing_stops_being_swept_and_keeps_its_reason(
    cfg: Config, tmp_path: Path, monkeypatch
) -> None:
    """A failure ends the sweep's interest and stands as the record of why (BUILD-PLAN
    M2.2, DESIGN 14).

    The session's first chunk reached its second turn and its second chunk then failed, and
    keeps failing. Measured against its own watermark that chunk is eligible again on every
    tick for the rest of the week, so the queue has to stop the retry rather than bury the
    reason under the rows of it: three sweeps and two failures leave three rows, and the
    failures name themselves. What settles a failure is a row that is not one, after which
    the turns it never reached are capture work again.
    """
    sessions = tmp_path / "agent-sessions"
    monkeypatch.setattr(capture, "SESSION_DIRS", {CLAUDE: sessions, PI: tmp_path / "no-pi"})
    path = session_file(
        sessions,
        CLAUDE,
        "failed-midway",
        records=conversation(CLAUDE, ["entry-1", "entry-2", "entry-3"]),
        touched=NOW - timedelta(minutes=45),
    )
    digest_to(cfg, CLAUDE, "failed-midway", path, "entry-2")
    queue.begin_watching(cfg, NOW - timedelta(hours=1))

    found = capture.sweep(cfg, NOW)
    assert [e.session_id for e in found] == ["failed-midway"], "one turn past its watermark"
    chunk = queue.next_job(cfg)
    assert chunk is not None
    assert chunk.watermark == "entry-2", "the chunk reads on from the watermark it inherits"
    queue.fail(cfg, chunk, "the model endpoint was down")

    assert queue.session_state(cfg, CLAUDE, "failed-midway").watermark == "entry-2"
    assert capture.sweep(cfg, NOW + timedelta(minutes=10)) == []
    assert capture.sweep(cfg, NOW + timedelta(minutes=20)) == []
    assert query(
        cfg,
        "SELECT status, error FROM jobs WHERE session_id = 'failed-midway' ORDER BY created_time",
    ) == [
        (queue.DONE, None),
        (queue.FAILED, "the model endpoint was down"),
    ], "the failure is the session's last word and it says why"

    # The agent's own hook still reports its own session, and that chunk fails too.
    queue.enqueue(cfg, event(session_id="failed-midway", transcript=path))
    retry = queue.next_job(cfg)
    assert retry is not None, "a hook is the agent's own report, not the sweep's guess"
    queue.fail(cfg, retry, "the model endpoint is still down")
    assert capture.sweep(cfg, NOW + timedelta(minutes=30)) == []
    assert query(cfg, "SELECT count(*) FROM jobs") == [(3,)], "no third row for the same chunk"

    # A digest that lands settles the failure, and the sweep measures the session against
    # its watermark again: the third turn is still uncaptured.
    queue.enqueue(cfg, event(session_id="failed-midway", transcript=path))
    fixed = queue.next_job(cfg)
    assert fixed is not None
    queue.complete(cfg, fixed, "entry-2")

    assert [e.session_id for e in capture.sweep(cfg, NOW + timedelta(minutes=40))] == [
        "failed-midway"
    ]


def test_the_sweep_enqueues_a_transcript_its_reader_cannot_read(
    cfg: Config, tmp_path: Path, monkeypatch
) -> None:
    """BUILD-PLAN M2.1: a file the reader refuses is capture work, not a session with
    nothing to say, because a job row is how that failure reaches a report.

    The fixture holds text and no record of it names a type, which is exactly what
    ``readers.UnrecognizedTranscript`` is for. The last assertion hands the row over to the
    same read a digest would, and that names the reason the sweep refused to call the
    session empty.
    """
    sessions = tmp_path / "agent-sessions"
    monkeypatch.setattr(capture, "SESSION_DIRS", {CLAUDE: sessions, PI: tmp_path / "no-pi"})
    path = session_file(sessions, CLAUDE, "unreadable-shape", touched=NOW - timedelta(minutes=45))
    queue.begin_watching(cfg, NOW - timedelta(hours=1))

    found = capture.sweep(cfg, NOW)

    assert [(e.agent, e.session_id, e.kind, e.detail) for e in found] == [
        (CLAUDE, "unreadable-shape", SESSION_END, "sweep")
    ]
    assert query(cfg, "SELECT transcript_path, kind, status FROM jobs") == [
        (str(path), SESSION_END, queue.PENDING)
    ]
    job = queue.next_job(cfg)
    assert job is not None
    with pytest.raises(readers.UnrecognizedTranscript):
        list(readers.read(job.agent, job.transcript_path, job.watermark))


def test_the_sweep_finds_a_session_once_and_a_second_pass_adds_no_row(
    cfg: Config, tmp_path: Path, monkeypatch
) -> None:
    """Twice over unchanged state, counted in the queue's own rows rather than in the list
    either sweep returned: the row the first pass wrote is why the second walks past.
    """
    sessions = tmp_path / "agent-sessions"
    monkeypatch.setattr(capture, "SESSION_DIRS", {CLAUDE: sessions, PI: tmp_path / "no-pi"})
    session_file(
        sessions,
        CLAUDE,
        "found-once",
        records=conversation(CLAUDE, ["entry-1", "entry-2"]),
        touched=NOW - timedelta(minutes=45),
    )
    queue.begin_watching(cfg, NOW - timedelta(hours=1))

    assert len(capture.sweep(cfg, NOW)) == 1, "uncaptured and quiet, so the pass finds it"
    rows = query(cfg, "SELECT id, session_id, status FROM jobs ORDER BY id")
    assert len(rows) == 1

    capture.sweep(cfg, NOW + timedelta(minutes=10))

    assert query(cfg, "SELECT id, session_id, status FROM jobs ORDER BY id") == rows, (
        "the same state once more came with a row of its own"
    )


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
    assert queue.session_state(cfg, CLAUDE, "recycled") == queue.SessionState(
        "entry-3", False, False, None
    )


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
    assert queue.session_state(cfg, CLAUDE, "other-owner") == queue.SessionState(
        "entry-3", False, False, None
    )


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

    assert queue.session_state(cfg, CLAUDE, "slow-worker") == queue.SessionState(
        None, True, False, None
    )
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
    assert queue.session_state(cfg, CLAUDE, "slow-failure") == queue.SessionState(
        None, True, False, None
    )


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


def test_a_queue_file_from_before_the_sweep_observation_still_opens(cfg: Config) -> None:
    """The queue file outlives a release, so the old one has to open and keep its rows.

    The fixture is that file: the `sessions` table as a previous revision wrote it, with a
    digested session in it. Opening it adds the columns this revision reads and leaves the
    watermark it already held exactly where it was.
    """
    queue.begin_watching(cfg, NOW - timedelta(days=1))
    conn = sqlite3.connect(cfg.home / queue.QUEUE_NAME, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP TABLE sessions")
        conn.execute(
            "CREATE TABLE sessions (agent TEXT NOT NULL, session_id TEXT NOT NULL,"
            " watermark TEXT, PRIMARY KEY (agent, session_id))"
        )
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?)", (CLAUDE, "from-an-older-file", "entry-7")
        )
        conn.execute("COMMIT")
    finally:
        conn.close()

    assert queue.session_state(cfg, CLAUDE, "from-an-older-file") == queue.SessionState(
        "entry-7", False, False, None
    ), "the watermark survived, and nothing has been observed yet"


def test_a_sweeps_note_never_moves_a_sessions_watermark(cfg: Config) -> None:
    """Only a digest moves a watermark (BUILD-PLAN M2.2), so the cheap observation a sweep
    leaves behind is stored beside it and cannot overwrite it.

    A digest can move the watermark between the sweep reading the session and its note
    arriving, which is the shape of the first call here: the note lands, the digest's
    answer stands, and the observation names a watermark that is no longer the session's -
    so it answers for nothing until the file is read again.
    """
    queue.enqueue(cfg, event(session_id="noted"))
    first = queue.next_job(cfg)
    assert first is not None
    queue.complete(cfg, first, "entry-7")

    queue.note_observed(cfg, CLAUDE, "noted", "entry-6", 1234.5, 4096)

    assert query(cfg, "SELECT watermark, seen_watermark FROM sessions") == [
        ("entry-7", "entry-6")
    ], "the note refreshed and moved nothing else"
    assert queue.session_state(cfg, CLAUDE, "noted").seen is None, "a stale note is no verdict"

    queue.note_observed(cfg, CLAUDE, "noted", "entry-7", 1234.5, 4096)
    assert queue.session_state(cfg, CLAUDE, "noted").seen == (1234.5, 4096)


def test_a_watermark_quoted_only_as_a_parent_is_no_watermark_at_all(
    cfg: Config, tmp_path: Path, monkeypatch
) -> None:
    """A later record that names the watermark's entry proves the file grew."""
    sessions = tmp_path / "agent-sessions"
    # A pi session resumed with /tree goes back to an entry already digested and appends
    # a new child of it, so the watermark's id survives as that child's parent. The turns
    # in between carry the file past the 200 KB tail the growth check reads.
    digested, new_branch = conversation(PI, ["entry-9", "branch-1"])
    turns = conversation(PI, [f"turn-{i}" for i in range(300)], pad=900)
    path = session_file(
        sessions,
        PI,
        "tree-jumped",
        records=[digested, *turns, new_branch],
        touched=NOW - timedelta(minutes=1),
    )
    monkeypatch.setattr(capture, "SESSION_DIRS", {CLAUDE: tmp_path / "no-claude", PI: sessions})
    queue.begin_watching(cfg, NOW - timedelta(minutes=10))
    digest_to(cfg, PI, "tree-jumped", path, "entry-9")

    found = capture.sweep(cfg, NOW)

    assert [(e.agent, e.session_id, e.kind) for e in found] == [(PI, "tree-jumped", COMPACTION)], (
        "the new branch is capture work the watermark must not answer for"
    )
    assert queue.next_job(cfg) is not None


def run_hook(
    monkeypatch, capsys, agent: str, event_name: str, stdin_text: str
) -> tuple[int, str, str]:
    """`aft hook <agent> <event>` with ``stdin_text`` on stdin, as the agent runs it."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    code = main(["hook", agent, event_name])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def hook_log(home: Path) -> list[str]:
    """The one record a failed hook is allowed to leave behind."""
    record = home / "logs" / "hooks.log"
    return record.read_text().splitlines() if record.exists() else []


def refuse_the_forbidden(monkeypatch, transcript: Path) -> list[str]:
    """Guard the two things a capture hook may not do, and name what it tried.

    ``open`` is wrapped so a hook that read the conversation is refused by name rather
    than quietly paying for the read, and the model is refused at its own door and at the
    HTTP call behind it. Either guard raises inside the hook, where the hook contract turns
    it into a line in the hook log - which is what the caller of this then checks.
    """
    tried: list[str] = []
    real_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        name = str(file)
        if name == str(transcript):
            tried.append(name)
            raise OSError(f"a hook opened the transcript: {name}")
        return real_open(file, *args, **kwargs)

    def refused(*args, **kwargs):
        tried.append("the model")
        raise AssertionError("a hook asked the model")

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_open)
    monkeypatch.setattr(model, "complete_json", refused)
    monkeypatch.setattr(model.urllib.request, "build_opener", refused)
    return tried


@pytest.mark.parametrize(
    ("agent", "event_name", "fixture", "kind"),
    CAPTURE_EVENTS,
    ids=[f"{agent}-{event}" for agent, event, _, _ in CAPTURE_EVENTS],
)
def test_a_capture_event_at_the_command_line_lands_one_queue_row(
    home, cfg, tmp_path, monkeypatch, capsys, agent, event_name, fixture, kind
) -> None:
    """BUILD-PLAN M2.2 names `aft hook` as the caller of `enqueue`, so it is the caller here.

    The row is the hook's whole job, and the two things the entry forbids are proved
    absent rather than trusted to the call graph: the transcript is a directory, so a hook
    that opened it would fail on the spot, and every door to a model is guarded.
    """
    transcript = tmp_path / "transcript.jsonl"
    transcript.mkdir()
    payload = json.loads((FIXTURES / fixture).read_text())
    payload["transcript_path"] = str(transcript)
    tried = refuse_the_forbidden(monkeypatch, transcript)

    assert run_hook(monkeypatch, capsys, agent, event_name, json.dumps(payload)) == (0, "", "")
    assert hook_log(home) == [], "a guard fired, or the hook found something to fail on"
    assert tried == []
    assert query(
        cfg,
        "SELECT agent, session_id, cwd, transcript_path, kind, status FROM jobs",
    ) == [(agent, payload["session_id"], payload["cwd"], str(transcript), kind, queue.PENDING)]


@pytest.mark.parametrize(
    ("stdin_text", "refusal"),
    [
        ("{ not json", "JSONDecodeError"),
        (json.dumps({"cwd": "/home/ted/dev/app"}), "session_id"),
    ],
    ids=["malformed-json", "missing-session-id"],
)
def test_an_event_that_never_becomes_an_event_reaches_no_queue_row(
    home, monkeypatch, capsys, stdin_text, refusal
) -> None:
    """The hook contract of BUILD-PLAN M1.1, checked where the queue can see it.

    Silence and exit 0 the contract already promises, and one thing this delivery owes it:
    a payload that never became a `SessionEvent` never became a row either, so the queue
    file is not even there to hold one.
    """
    assert run_hook(monkeypatch, capsys, "claude", "SessionEnd", stdin_text) == (0, "", "")

    lines = hook_log(home)
    assert len(lines) == 1, "the log line is the whole record of what went wrong"
    assert refusal in lines[0]
    assert "SessionEnd" in lines[0]
    assert not (home / queue.QUEUE_NAME).exists()
