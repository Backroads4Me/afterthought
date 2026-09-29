"""The capture queue: one SQLite file the capture hook writes and the digest worker reads.

The queue is the whole seam between the two processes that never talk to each other
(DESIGN 8): a hook writes one row inside a transaction and exits, never calling a model,
and the digest step claims one row at a time, digests it, and reports where it got to.
Both agents' sessions land in the same table under the same locks, so Claude Code and pi
finishing at the same moment cannot corrupt each other's watermarks.

The file is ``queue.sqlite`` in the afterthought home, the same place as the other
per-host state (DESIGN 6). The vault holds memory; the queue holds work, and neither
machine shares what is in here.

A row carries session identity, never a project: resolution to a project happens at
digest time (BUILD-PLAN M2.2).

Besides the rows, the queue keeps one line per session of its own: its watermark, the file
a sweep last read to no work, and the pause a chunk that did not land waits out before the
next one is offered.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aft.config import Config
from aft.events import SessionEvent
from aft.ids import new_id

QUEUE_NAME = "queue.sqlite"


class QueueError(Exception):
    """A queue operation that cannot be honoured as asked: a row this process may no
    longer close - gone from the queue, or gone on to another worker - or a journal mode
    that never becomes WAL."""


# The journal mode the queue is kept in: one writer and many readers at once, which is
# what lets a hook write while the digest works (DESIGN 8).
WAL_MODE = "wal"

# A writer gives up on the lock only after a long wait, because a row the queue refuses
# is capture work no hook will hand over again. The hook process cannot wait this long:
# its own share of the hook budget ends the wait (cli.HOOK_TIMEOUT_SECONDS), which is why
# the queue can afford to be the patient one.
BUSY_TIMEOUT_MS = 5_000

# Row states. A claimed row belongs to whoever claimed it until it completes, fails,
# or its claim goes stale.
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

# The floor of a claim's lease. The work cadence is a scheduling period anyone may set
# in config.toml, while the lease is what keeps one session digesting one chunk at a
# time, so the lease never drops below the quiet period the design commits a session to
# (DESIGN 8, BUILD-PLAN M2.2): a bound on correctness cannot rest on that number alone.
CLAIM_LEASE_FLOOR = timedelta(minutes=30)

# A chunk a session was offered and did not land is offered again on a pause that doubles
# with every try until the ceiling. That ceiling is the interval a session whose tries are
# spent keeps being offered on - six hours, hours rather than sweep ticks - so a broken
# reader or an endpoint that is down costs a handful of rows over an evening rather than one
# per sweep, and the session still comes back by itself. Past RETRY_ATTEMPTS tries it is
# named on `attention_needed` with the last reason it recorded while those long offers go on
# (DESIGN 14: left queued and reported, BUILD-PLAN M2.1, M2.2).
RETRY_AFTER = timedelta(minutes=30)
RETRY_CEILING = timedelta(hours=6)
RETRY_ATTEMPTS = 3

# The one key of the meta table this module uses: the instant this home began being
# watched, written by the opening sweep and never rewritten.
WATCHING_SINCE = "watching_since"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    agent           TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    cwd             TEXT NOT NULL,
    transcript_path TEXT,
    kind            TEXT NOT NULL,
    event_time      TEXT NOT NULL,
    status          TEXT NOT NULL,
    claimed_pid     INTEGER,
    error           TEXT,
    created_time    TEXT NOT NULL,
    updated_time    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    agent           TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    watermark       TEXT,
    seen_mtime      REAL,
    seen_size       INTEGER,
    attempts        INTEGER NOT NULL DEFAULT 0,
    retry_after     TEXT,
    PRIMARY KEY (agent, session_id)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS jobs_by_status ON jobs (status, created_time, id);
CREATE INDEX IF NOT EXISTS jobs_by_session ON jobs (agent, session_id, status);
"""

# The `sessions` columns a queue file written before this revision does not have. SQLite
# leaves an existing table exactly as it found it, so each one is added when it is missing.
SESSION_COLUMNS = {
    "seen_mtime": "REAL",
    "seen_size": "INTEGER",
    "attempts": "INTEGER NOT NULL DEFAULT 0",
    "retry_after": "TEXT",
}


@dataclass(frozen=True)
class Job:
    """One claim of capture work, and the watermark the digest has to read past.

    ``transcript_path`` is absent for a pi event reported before the session was first
    written (adapters/pi.py); the row still stands, and the sweep finds the file later.
    """

    id: str
    agent: str
    session_id: str
    cwd: Path
    transcript_path: Path | None
    kind: str
    event_time: datetime
    watermark: str | None


@dataclass(frozen=True)
class SessionState:
    """What the queue knows about one session, for the sweep's tests.

    A session the queue has never heard of is all of these defaults, which is what lets a
    sweep read the table once and ask each file it walks the same questions.

    ``watermark`` is the id of its last digested entry, or None for a session never
    digested. ``has_open_job`` covers a row awaiting a claim and one being digested now:
    both are capture work in flight for that session, and a second row for either would
    be a chunk the digest has already read. ``attempts`` counts the chunks this session was
    offered that did not land, and ``retry_after`` is the instant the next one may be
    offered; a chunk that lands returns both to zero and None. ``seen`` is the
    ``(mtime, size)`` of the bytes something last read to their end and called finished - a
    sweep that found no work, or a digest that found nothing to record - or None once either
    of them moves.
    """

    watermark: str | None = None
    has_open_job: bool = False
    attempts: int = 0
    retry_after: datetime | None = None
    seen: tuple[float, int] | None = None

    def waiting_on_retry(self, now: datetime) -> bool:
        """Whether the queue is holding this session off another chunk.

        A chunk that did not land waits out a pause that doubles with every try until the
        ceiling, and a session whose tries are spent goes on being offered on that ceiling -
        hours apart - rather than stopping for good. Nothing shortens the pause early; a chunk
        of the session landing ends it, which is what a session standing on
        `attention_needed` is waiting for too (DESIGN 14).
        """
        return self.retry_after is not None and now < self.retry_after


@dataclass(frozen=True)
class Attention:
    """One session whose tries are spent: its chunks stopped landing, and a person is
    what puts that right."""

    agent: str
    session_id: str
    attempts: int
    error: str | None


def enqueue(config: Config, event: SessionEvent) -> None:
    """Record one session event as capture work, in one transaction.

    One INSERT and one commit, so the hook that calls it stays inside its budget and
    concurrent writers from both agents serialize on the write lock (DESIGN 8). Nothing
    here opens the transcript, resolves a project, or touches a watermark.
    """
    now = _now()
    with _writing(config) as conn:
        conn.execute(
            "INSERT INTO jobs (id, agent, session_id, cwd, transcript_path, kind,"
            " event_time, status, created_time, updated_time)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("job"),
                event.agent,
                event.session_id,
                str(event.cwd),
                None if event.transcript_path is None else str(event.transcript_path),
                event.kind,
                _text(event.time),
                PENDING,
                _text(now),
                _text(now),
            ),
        )


def next_job(config: Config) -> Job | None:
    """Claim the oldest waiting job whose session has no other job being digested.

    A session digests one chunk at a time because it has one watermark to move, and the
    chunks go oldest first so a later chunk never reads past an earlier one.

    A claim names the pid of the process that took it, so a worker that died between
    reading a job and completing it leaves the job claimable again: the row it left
    behind says who held it and when, and it goes back to the queue once that process is
    gone or once the row's lease has run out.
    """
    now = _now()
    # The lease is the larger of the cadence the work timer runs at and the floor the
    # design's quiet period sets: a row a live process holds stays claimed.
    lease = max(timedelta(minutes=config.settings.dream.interval_minutes), CLAIM_LEASE_FLOOR)
    with _writing(config) as conn:
        _reclaim_stale_claims(conn, now, lease)
        row = conn.execute(
            "SELECT id, agent, session_id, cwd, transcript_path, kind, event_time"
            " FROM jobs j"
            " WHERE status = ?"
            "   AND NOT EXISTS ("
            "       SELECT 1 FROM jobs held"
            "       WHERE held.agent = j.agent AND held.session_id = j.session_id"
            "         AND held.status = ?)"
            " ORDER BY created_time, id LIMIT 1",
            (PENDING, RUNNING),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE jobs SET status = ?, claimed_pid = ?, updated_time = ? WHERE id = ?",
            (RUNNING, os.getpid(), _text(now), row[0]),
        )
        return _job(conn, row)


def complete(config: Config, job: Job, watermark: str | None) -> None:
    """Close a job and record the watermark its digest reached.

    This is where a session's watermark moves, and nowhere else: the digest step calls it
    with the entry it ended on when it wrote that digest (DESIGN 8, BUILD-PLAN M2.2).
    A digest that lands also ends the retry state its session was carrying: the tries
    start again from the first and the next chunk is offered on the sweep's own terms.
    Only the process still holding the claim closes it, so a worker that outlived its
    claim - its row already handed to another process - cannot close that row or move the
    watermark backwards. Closing a row this process does not hold is an error, not a
    success.

    A watermark of None closes the row and charges nothing: the digest read the chunk and
    found nothing there to record, which is a finished digest, not a failed try. Its
    checkpoint moves to the end of the bytes it just processed, so the sweep offers those
    bytes again only once they have moved, while the session's tries and the pause they set
    stand exactly where they were (BUILD-PLAN M2.2).
    """
    now = _now()
    with _writing(config) as conn:
        closed = conn.execute(
            "UPDATE jobs SET status = ?, claimed_pid = NULL, error = NULL, updated_time = ?"
            " WHERE id = ? AND status = ? AND claimed_pid = ?",
            (DONE, _text(now), job.id, RUNNING, os.getpid()),
        ).rowcount
        if not closed:
            raise QueueError(f"job {job.id} is not held by this process")
        if watermark:
            conn.execute(
                "INSERT INTO sessions (agent, session_id, watermark) VALUES (?, ?, ?)"
                " ON CONFLICT (agent, session_id) DO UPDATE SET watermark = excluded.watermark",
                (job.agent, job.session_id, watermark),
            )
            _advanced(conn, job.agent, job.session_id)
        else:
            _processed_to_the_end(conn, job)


def fail(config: Config, job: Job, error: str) -> None:
    """Leave a job behind as the record of why it could not be digested.

    A failed row is not handed out again: it is the report's line, and DESIGN 14 leaves
    the work queued rather than retrying it in the same run. Nothing moved the session's
    watermark, so the turns this chunk could not read are still capture work, and
    the sweep offers them again once the pause this failure sets has passed - a pause that
    doubles with every try rather than a row on every tick, up to an interval of hours. A
    session whose tries are spent is named on `attention_needed` holding this row's error,
    which is where a person finds it, and it goes on being offered on that long interval
    until something lands. The agent's own hook reaches the queue as a row like any other,
    and it is a chunk of that session landing, not the report, that starts the tries
    again from the first (BUILD-PLAN M2.1, M2.2). As when a job closes, only the process still
    holding the claim records its failure, so a worker whose row has gone on to another
    process cannot mark that row failed or clear that process's claim. Recording a failure on
    a row this process does not hold - one already handed on, or one no longer in the queue -
    is an error, not a silence: why the digest could not run has to reach the report.
    """
    now = _now()
    with _writing(config) as conn:
        recorded = conn.execute(
            "UPDATE jobs SET status = ?, claimed_pid = NULL, error = ?, updated_time = ?"
            " WHERE id = ? AND status = ? AND claimed_pid = ?",
            (FAILED, error, _text(now), job.id, RUNNING, os.getpid()),
        ).rowcount
        if not recorded:
            raise QueueError(f"job {job.id} is not held by this process")
        _held_back(conn, job.agent, job.session_id, now)


def session_states(config: Config) -> dict[tuple[str, str], SessionState]:
    """What the queue knows about every session it has heard of, in one read.

    A sweep walks a week of session files and asks each one the same questions, so it asks
    them of the table once rather than of every file it walks (BUILD-PLAN M2.2).
    """
    with _reading(config) as conn:
        rows = conn.execute(
            "SELECT agent, session_id, watermark, seen_mtime, seen_size, attempts,"
            " retry_after FROM sessions"
        ).fetchall()
        open_keys = conn.execute(
            "SELECT DISTINCT agent, session_id FROM jobs WHERE status IN (?, ?)",
            (PENDING, RUNNING),
        ).fetchall()
    states: dict[tuple[str, str], SessionState] = {}
    for row in rows:
        agent, session_id, watermark, seen_mtime, seen_size = row[:5]
        attempts, retry = row[5:]
        seen = None if seen_mtime is None or seen_size is None else (seen_mtime, seen_size)
        states[(agent, session_id)] = SessionState(
            watermark=watermark,
            attempts=attempts,
            retry_after=None if retry is None else datetime.fromisoformat(retry),
            seen=seen,
        )
    for agent, session_id in open_keys:
        states[(agent, session_id)] = replace(
            states.get((agent, session_id), SessionState()), has_open_job=True
        )
    return states


def attention_needed(config: Config) -> list[Attention]:
    """The sessions whose tries are spent, each with the last reason it recorded.

    A chunk that did not land is offered again on a widening pause, and a session whose tries
    have run out goes on being offered on that pause's ceiling hours later rather than being
    dropped. It stands here with the last reason any of its rows recorded, so the reason is
    readable while those long offers continue, and naming it is the report's (DESIGN 14: left
    queued and reported). A chunk of that session landing takes it off the list; a lifecycle
    event that only reports the session does not.
    """
    with _reading(config) as conn:
        rows = conn.execute(
            "SELECT agent, session_id, attempts,"
            "       (SELECT error FROM jobs j"
            "         WHERE j.agent = sessions.agent AND j.session_id = sessions.session_id"
            "           AND j.error IS NOT NULL"
            "          ORDER BY j.created_time DESC, j.id DESC LIMIT 1)"
            "  FROM sessions WHERE attempts >= ?"
            " ORDER BY agent, session_id",
            (RETRY_ATTEMPTS,),
        ).fetchall()
    return [
        Attention(agent, session_id, attempts, reason)
        for agent, session_id, attempts, reason in rows
    ]


def watching_since(config: Config) -> datetime | None:
    """When this home began being watched, or None until the first sweep says so.

    The sweep measures each session against its own watermark, so a home that has never
    been watched holds nothing to measure the sessions that predate afterthought against
    either. The instant stored here is what they are held against (BUILD-PLAN M2.2).
    """
    with _reading(config) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (WATCHING_SINCE,)).fetchone()
    return None if row is None else datetime.fromisoformat(row[0])


def begin_watching(config: Config, now: datetime) -> None:
    """Say this home has been watched from ``now`` on - once, and never again.

    The opening sweep writes the marker and no later sweep rewrites it, so what that sweep
    left uncaptured stays uncaptured. That is what keeps up to seven days of sessions that
    predate afterthought out of the queue (BUILD-PLAN M2.2).
    """
    with _writing(config) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
            (WATCHING_SINCE, _text(now)),
        )


def note_observed(config: Config, agent: str, session_id: str, mtime: float, size: int) -> None:
    """Record the bytes a sweep just read to their end and called finished (BUILD-PLAN M2.2)."""
    with _writing(config) as conn:
        _note_read(conn, agent, session_id, mtime, size)


def _note_read(
    conn: sqlite3.Connection, agent: str, session_id: str, mtime: float, size: int
) -> None:
    """Say which bytes of this session something read to the end and called finished.

    The pair is the cheap answer to "has anything happened here since": a transcript only
    grows, so a file at the same modification time and size as one something read to no work
    holds no more than that reader found. It is this module's own pair: a conflict refreshes
    it and leaves ``watermark`` exactly as the digest left it.
    """
    conn.execute(
        "INSERT INTO sessions (agent, session_id, seen_mtime, seen_size) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (agent, session_id) DO UPDATE SET"
        " seen_mtime = excluded.seen_mtime, seen_size = excluded.seen_size",
        (agent, session_id, mtime, size),
    )


def _advanced(conn: sqlite3.Connection, agent: str, session_id: str) -> None:
    """A chunk of this session landed, so its tries start again from the first.

    The pause and the spent tries describe the chunk that did not land. Once the session
    has a watermark further along, they say nothing about the next one.
    """
    conn.execute(
        "UPDATE sessions SET attempts = 0, retry_after = NULL WHERE agent = ? AND session_id = ?",
        (agent, session_id),
    )


def _processed_to_the_end(conn: sqlite3.Connection, job: Job) -> None:
    """Move a session's checkpoint to the end of the bytes a digest read and recorded nothing from.

    Nothing to record is still an end: the file's own modification time and size become what
    the queue holds, so those bytes come around again only once they move. The tries stay
    exactly as they were - a digest that succeeded at nothing owes no failed try. A row that
    named no transcript, or one the agent has since cleaned up, leaves the checkpoint where
    it was, and the sweep will meet that file when it is there (BUILD-PLAN M2.2).
    """
    if job.transcript_path is None:
        return
    try:
        stat = job.transcript_path.stat()
    except OSError:
        return
    _note_read(conn, job.agent, job.session_id, stat.st_mtime, stat.st_size)


def _retry_pause(attempts: int) -> timedelta:
    """How long a session waits after the ``attempts``-th chunk that did not land.

    The pause doubles with every try until the ceiling, so a week of failures costs no more
    waiting than six hours of them, and a session is never offered on every sweep. The
    ceiling is what keeps the widening interval a real instant: doubled without one, the
    ladder runs off the end of `timedelta`.
    """
    pause = RETRY_AFTER
    while attempts > 1 and pause < RETRY_CEILING:
        pause = min(pause * 2, RETRY_CEILING)
        attempts -= 1
    return pause


def _held_back(conn: sqlite3.Connection, agent: str, session_id: str, now: datetime) -> None:
    """Count one chunk this session was offered that did not land, and say when the next
    one may be offered.

    The pause widens with every try, so a transcript no reader can read or an endpoint
    that is down costs a handful of rows across an evening rather than one per sweep, and
    every one of them keeps the reason it gave (`attention_needed`).
    """
    held = conn.execute(
        "SELECT attempts FROM sessions WHERE agent = ? AND session_id = ?",
        (agent, session_id),
    ).fetchone()
    attempts = (held[0] if held else 0) + 1
    conn.execute(
        "INSERT INTO sessions (agent, session_id, attempts, retry_after) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (agent, session_id) DO UPDATE SET"
        " attempts = excluded.attempts, retry_after = excluded.retry_after",
        (agent, session_id, attempts, _text(now + _retry_pause(attempts))),
    )


def _reclaim_stale_claims(conn: sqlite3.Connection, now: datetime, lease: timedelta) -> None:
    """Hand back every job whose claimer is gone or whose claim has outlived its lease.

    The claim is a pid because that is what a dead worker actually leaves behind, and the
    queue is host-local, so a pid means what it says here. Age stands beside liveness
    because a process id can answer "alive" for a worker that is gone: the id was
    recycled to an unrelated process after it died, or the claimer belongs to another
    account and refuses the signal. ``lease`` is how long a row must sit untouched
    before another process may take it, so a row older than that has outlived any worker
    that could still finish it.
    """
    stale = [
        job_id
        for job_id, pid, updated in conn.execute(
            "SELECT id, claimed_pid, updated_time FROM jobs"
            " WHERE status = ? AND claimed_pid IS NOT NULL",
            (RUNNING,),
        )
        # A claim that only looks alive would strand the session, not lose one job.
        if not _alive(pid) or now - datetime.fromisoformat(updated) >= lease
    ]
    for job_id in stale:
        conn.execute(
            "UPDATE jobs SET status = ?, claimed_pid = NULL, updated_time = ? WHERE id = ?",
            (PENDING, _text(now), job_id),
        )


def _alive(pid: int) -> bool:
    """Whether a process is still there to finish what it claimed.

    A pid this process cannot signal counts as alive: the queue is one machine's state,
    and a process owned by someone else is a reason to leave the job alone, not a reason
    to hand it out again.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def _reading(config: Config):
    conn = _connect(config)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _writing(config: Config):
    """One connection held for one transaction, taken immediately.

    ``BEGIN IMMEDIATE`` takes the write lock before the first row is written, so two
    writers coming from the two agents take their rows in turn instead of one of them
    losing its row to a busy database (DESIGN 8).
    """
    conn = _connect(config)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _connect(config: Config) -> sqlite3.Connection:
    """Open the queue, creating it on first use.

    Autocommit is left on so the transactions in this module are only the ones written
    here. WAL is what lets the digest read while a hook writes; ``synchronous=NORMAL`` is
    SQLite's companion setting for it, and a commit lost to a power cut is a missed hook,
    which the sweep recovers (DESIGN 14).
    """
    path = config.home / QUEUE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous = NORMAL")
    if _journal_mode(conn) != WAL_MODE:
        _enter_wal(conn)
    conn.executescript(_SCHEMA)
    _add_missing_columns(conn)
    return conn


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Give a queue file written before this revision the columns it never got.

    ``CREATE TABLE IF NOT EXISTS`` leaves a table that already exists exactly as it found
    it, so a column added since that file was written has to arrive by ``ALTER TABLE``. A
    column an older file does not have reads back as None, which is an unobserved session.
    """
    present = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    for name, type_ in SESSION_COLUMNS.items():
        if name in present:
            continue
        try:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {type_}")
        except sqlite3.OperationalError as race:
            # Two first connections to an old file can reach this together; the loser of
            # the race already has the column it asked for.
            if "duplicate column" not in str(race):
                raise


def _journal_mode(conn: sqlite3.Connection) -> str:
    return conn.execute("PRAGMA journal_mode").fetchone()[0]


def _enter_wal(conn: sqlite3.Connection) -> None:
    """Put the queue into WAL mode, once, without losing a race to create it.

    Changing the journal mode wants the database to itself and reports ``database is
    locked`` at once when a writer holds it, so a hook asked to change it on every
    enqueue would fail the moment two hooks wrote the first rows of a new queue. Each
    connection asks only while the mode is still the default one, and a loser of that
    race finds the winner's WAL a moment later, so every row still lands (DESIGN 8).
    A mode that never arrives inside the busy timeout is a queue no writer can count
    on, so it is reported rather than waited on without end.
    """
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    while _journal_mode(conn) != WAL_MODE and time.monotonic() < deadline:
        try:
            conn.execute(f"PRAGMA journal_mode = {WAL_MODE}")
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.002)
    if _journal_mode(conn) != WAL_MODE:
        # A pragma that refuses by answering with the mode it kept sets no error.
        raise QueueError(f"queue stays in {_journal_mode(conn)} mode, not {WAL_MODE}")


def _job(conn: sqlite3.Connection, row: tuple) -> Job:
    """The claimed row, plus the watermark its session has reached so far."""
    watermark = conn.execute(
        "SELECT watermark FROM sessions WHERE agent = ? AND session_id = ?",
        (row[1], row[2]),
    ).fetchone()
    return Job(
        id=row[0],
        agent=row[1],
        session_id=row[2],
        cwd=Path(row[3]),
        transcript_path=None if row[4] is None else Path(row[4]),
        kind=row[5],
        event_time=datetime.fromisoformat(row[6]),
        watermark=watermark[0] if watermark else None,
    )


def _text(moment: datetime) -> str:
    """An instant as the queue stores it: UTC ISO 8601, which sorts as a string."""
    return moment.astimezone(UTC).isoformat()


def _now() -> datetime:
    return datetime.now(UTC)
