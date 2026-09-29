"""The capture queue: one SQLite file the capture hook writes and the digest worker reads.

The queue is the whole seam between the two processes that never talk to each other
(DESIGN 8): a hook writes one row inside a transaction and exits, never calling a model,
and the digest step claims one row at a time, digests it, and reports where it got to.
Both agents' sessions land in the same table under the same locks, so Claude Code and pi
finishing at the same moment cannot corrupt each other's watermarks.

The file is ``queue.sqlite`` in the afterthought home, the same place as the other
per-host state (DESIGN 6). The vault holds memory; the queue holds work, and neither
machine shares what is in here.

A jobs row carries session identity, never a project: resolution to a project happens at
digest time (BUILD-PLAN M2.2). The one row a session has in the ``sessions`` table carries
where its digest has read to - the entry it stopped on and whether an off-the-record span
is open there - so the next chunk of that session continues the work instead of starting
over (BUILD-PLAN M2.2, M2.3).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aft.config import Config
from aft.events import SessionEvent
from aft.ids import new_id

QUEUE_NAME = "queue.sqlite"

LOGGER = logging.getLogger(__name__)


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

# The one key of the meta table this module uses.
LAST_SWEEP = "last_sweep"

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
    agent       TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    watermark   TEXT,
    off_record  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (agent, session_id)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS jobs_by_status ON jobs (status, created_time, id);
CREATE INDEX IF NOT EXISTS jobs_by_session ON jobs (agent, session_id, status);
"""

# A column a shipped table gained afterwards, with the definition that adds it and the
# default a row already in the file gets. ``CREATE TABLE IF NOT EXISTS`` leaves a queue an
# earlier version wrote exactly as it is, so every later column is checked for and added
# when the queue opens: a session already in the file keeps its watermark and starts with
# no span open, the reading that captures text rather than holding a session off the
# record without anyone having asked (M2.2, M2.3).
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("sessions", "off_record", "INTEGER NOT NULL DEFAULT 0"),
)


@dataclass(frozen=True)
class Job:
    """One claim of capture work, and where its session's digest has read to so far.

    ``transcript_path`` is absent for a pi event reported before the session was first
    written (adapters/pi.py); the row still stands, and the sweep finds the file later.
    ``watermark`` and ``off_record`` are the two facts a digest needs to pick up where the
    last chunk of this session left off: the entry to read on from, and whether an
    off-the-record span is open at that point, so the chunk starts inside the span rather
    than treating it as over (M2.2, M2.3).
    """

    id: str
    agent: str
    session_id: str
    cwd: Path
    transcript_path: Path | None
    kind: str
    event_time: datetime
    watermark: str | None
    off_record: bool


@dataclass(frozen=True)
class SessionState:
    """What the queue knows about one session, for the sweep's tests and for a reader.

    ``watermark`` is the id of its last digested entry, or None for a session never
    digested. ``off_record`` is whether an off-the-record span is open at that watermark
    (M2.3); a session with no row of its own has no span open. ``has_open_job`` covers a
    row awaiting a claim and one being digested now: both are capture work in flight for
    that session, and a second row for either would be a chunk the digest has already read.
    """

    watermark: str | None
    off_record: bool
    has_open_job: bool


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


def complete(
    config: Config,
    job: Job,
    watermark: str | None,
    *,
    off_record: bool,
) -> None:
    """Close a job and record where its digest left this session: its watermark, and
    whether an off-the-record span is open there.

    This is where a session's watermark moves, and nowhere else: the digest step calls it
    with the entry it ended on when it wrote that digest (DESIGN 8, BUILD-PLAN M2.2).
    ``off_record`` is the second element of the ``filter_entries`` call over that chunk
    (M2.3), and a caller has to give it: a close never guesses a span's state. It moves in
    the same statement as the watermark because the two describe one moment: the end of
    what this session has been read through. Advance a watermark and
    leave the span behind and the next chunk reads its first entry as though no span were
    open; store a span against a watermark that did not move and the next chunk is held
    off the record for text no digest has read yet. A watermark of None closes the job and
    leaves both where they were, so those entries are read again and the span re-derived.

    Only the process still holding the claim closes it, so a worker that outlived its
    claim - its row already handed to another process - cannot close that row or move the
    watermark backwards. Closing a row this process does not hold is an error, not a
    success.
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
                "INSERT INTO sessions (agent, session_id, watermark, off_record)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT (agent, session_id) DO UPDATE SET"
                " watermark = excluded.watermark, off_record = excluded.off_record",
                (job.agent, job.session_id, watermark, int(off_record)),
            )


def fail(config: Config, job: Job, error: str) -> None:
    """Leave a job behind as the record of why it could not be digested.

    A failed row is not handed out again: it is the report's line, and DESIGN 14 leaves
    the work queued rather than retrying it in the same run. Nothing moved the
    session's watermark, so the next sweep or hook for that session enqueues the same
    chunk again, and a session whose transcript is fixed gets digested then (BUILD-PLAN
    M2.1). As when a job closes, only the process still holding the claim records its
    failure, so a worker whose row has gone on to another process cannot mark that row
    failed or clear that process's claim. Recording a failure on a row this process does
    not hold - one already handed on, or one no longer in the queue - is an error, not a
    silence: why the digest could not run has to reach the report.
    """
    with _writing(config) as conn:
        recorded = conn.execute(
            "UPDATE jobs SET status = ?, claimed_pid = NULL, error = ?, updated_time = ?"
            " WHERE id = ? AND status = ? AND claimed_pid = ?",
            (FAILED, error, _text(_now()), job.id, RUNNING, os.getpid()),
        ).rowcount
        if not recorded:
            raise QueueError(f"job {job.id} is not held by this process")


def session_state(config: Config, agent: str, session_id: str) -> SessionState:
    """The queue's facts about one session: where its digest read to, whether a span is open
    there, and whether it has capture work in flight.

    The state of a session the queue has no row for is no watermark and no span open, which
    is what a first chunk of any session starts with.
    """
    with _reading(config) as conn:
        row = conn.execute(
            "SELECT watermark, off_record FROM sessions WHERE agent = ? AND session_id = ?",
            (agent, session_id),
        ).fetchone()
        open_row = conn.execute(
            "SELECT 1 FROM jobs WHERE agent = ? AND session_id = ? AND status IN (?, ?) LIMIT 1",
            (agent, session_id, PENDING, RUNNING),
        ).fetchone()
    return SessionState(
        watermark=row[0] if row else None,
        off_record=bool(row[1]) if row else False,
        has_open_job=open_row is not None,
    )


def last_sweep(config: Config) -> datetime | None:
    """When the last completed sweep ran, or None before there has been one.

    A sweep records this only after it has enqueued everything it found, so a sweep that
    died partway leaves the window it was working open and finds those sessions again.
    """
    with _reading(config) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (LAST_SWEEP,)).fetchone()
    return None if row is None else datetime.fromisoformat(row[0])


def record_sweep(config: Config, now: datetime) -> None:
    """Close the sweep window at ``now``: the next one looks at sessions touched after it."""
    with _writing(config) as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (LAST_SWEEP, _text(now)),
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
        _check_columns(conn)
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
        _add_columns(conn)
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
    return conn


def _add_columns(conn: sqlite3.Connection) -> None:
    """Add to ``conn`` any column of `_ADDED_COLUMNS` the table it names does not have yet.

    A queue file an earlier version wrote keeps its rows and its own columns, so a column
    `_SCHEMA` has gained since has to arrive by ``ALTER TABLE`` before any statement names
    it, and only a writer brings it in, under the lock `_writing` already holds: a reader
    never asks for that lock to make a column exist. A second process can win that race,
    and its column is the one this connection then finds, so an ``ALTER`` that reports the
    name already taken is checked rather than raised: the queue opened either way. Any
    other report the ``ALTER`` makes - a locked database above all - is the queue's own
    trouble and is raised as it stands. What is still missing afterwards, here or on a
    read, is the same report `_check_columns` makes.
    """
    for table, column, definition in _ADDED_COLUMNS:
        if column in _columns(conn, table):
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        except sqlite3.OperationalError as lost_race:
            if "duplicate column name" not in str(lost_race):
                raise
        else:
            _name_sessions_of_unknown_span(conn)
    _check_columns(conn)


def _check_columns(conn: sqlite3.Connection) -> None:
    """Report a column of `_ADDED_COLUMNS` that ``conn``'s table does not have.

    A reader that meets one absent says so rather than making it exist: the column
    arrives on the write side, never under a read intent.
    """
    for table, column, _ in _ADDED_COLUMNS:
        if column not in _columns(conn, table):
            raise QueueError(f"queue table {table} has no {column} column to use")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """The names of ``table``'s columns as the file on disk has them."""
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _name_sessions_of_unknown_span(conn: sqlite3.Connection) -> None:
    """Name the sessions whose span state was unknown when the queue gained ``off_record``.

    This is the plan author's open question on that upgrade window, ruled at 050a56c.
    """
    named = [
        f"{agent}/{session_id}"
        for agent, session_id in conn.execute(
            "SELECT agent, session_id FROM sessions WHERE watermark IS NOT NULL LIMIT 10"
        )
    ]
    if named:
        LOGGER.warning(
            "queue gained off_record: sessions resuming on the record with no span stored"
            " include %s; open question on BUILD-PLAN M2.3",
            ", ".join(named),
        )


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
    """The claimed row, plus where its session's digest has read to: the watermark it
    reached and whether an off-the-record span is open there.

    The two come off the same row the claim is answered from, so a digest never gets one
    without the other (M2.2, M2.3).
    """
    state = conn.execute(
        "SELECT watermark, off_record FROM sessions WHERE agent = ? AND session_id = ?",
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
        watermark=state[0] if state else None,
        off_record=bool(state[1]) if state else False,
    )


def _text(moment: datetime) -> str:
    """An instant as the queue stores it: UTC ISO 8601, which sorts as a string."""
    return moment.astimezone(UTC).isoformat()


def _now() -> datetime:
    return datetime.now(UTC)
