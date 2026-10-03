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
over (BUILD-PLAN M2.2, M2.3). That row also carries whether a digest is *owed* for text the
archive already holds: a worker session stored without budget leaves no waiting job behind,
so capture keeps following the session, and the debt on the session row is what remembers
that a digest is still to come (BUILD-PLAN M2.7).
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Collection, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aft import keys
from aft.config import Config, is_excluded, is_worker
from aft.events import SessionEvent
from aft.ids import new_id
from aft.registry import Registry, unique_slug

QUEUE_NAME = "queue.sqlite"


class QueueError(Exception):
    """A queue operation that cannot be honoured as asked: a row this process may no
    longer close - gone from the queue, or gone on to another worker - or a journal mode
    that never becomes WAL."""


# The journal mode the queue is kept in: one writer and many readers at once, which is
# what lets a hook write while the digest works (DESIGN 8).
WAL_MODE = "wal"

# A writer gives up on the lock only after a long wait, because a row the queue refuses
# is capture work no hook will hand over again.
BUSY_TIMEOUT_MS = 5_000

# How long enqueue waits for the queue's write lock on the hook path. The hook must
# return within its 2-second budget even when another process holds the write lock,
# and SIGALRM cannot interrupt a blocking SQLite C call, so the SQLite busy timeout
# itself must sit inside the budget (BUILD-PLAN F23, M1 review 6).
HOOK_BUSY_TIMEOUT_MS = 1_000

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

# The one key of the meta table this module uses: when this host began watching for
# sessions. The first sweep writes it and nothing rewrites it.
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
    claimed_time    TEXT,
    error           TEXT,
    created_time    TEXT NOT NULL,
    updated_time    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    agent       TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    watermark   TEXT,
    off_record  INTEGER NOT NULL DEFAULT 0,
    digest_due  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (agent, session_id)
);
CREATE TABLE IF NOT EXISTS forgotten (
    session_id     TEXT PRIMARY KEY,
    forgotten_time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_projects (
    agent       TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    project_id  TEXT,
    slug        TEXT NOT NULL,
    path        TEXT,
    PRIMARY KEY (agent, session_id)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS jobs_by_status ON jobs (status, created_time, id);
CREATE INDEX IF NOT EXISTS jobs_by_session ON jobs (agent, session_id, status);
"""


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
    ``digest_due`` is the archive's record that this session's words are stored and no
    digest has read them yet (M2.7). It is a debt the queue carries for the digest, never
    capture work in flight: capture ignores it, which is the whole point of not leaving a
    waiting row behind an archived worker.
    ``read_at`` is when the queue last read the session's text: when its newest finished
    job was claimed, or, for a worker row the archive stored without digesting, when it was
    stored. A transcript untouched since then holds nothing unread. ``failed_at``
    is when the session's newest job failed, or None when that job did not fail.
    ``forgotten`` is whether ``aft forget session`` removed this session, which is the
    queue's way of knowing that a transcript still on disk holds words that were asked to
    be gone (DESIGN 12).
    """

    watermark: str | None
    off_record: bool
    has_open_job: bool
    digest_due: bool
    read_at: datetime | None = None
    failed_at: datetime | None = None
    forgotten: bool = False


@dataclass(frozen=True)
class ProjectRecord:
    """The session-start record: the project a session started in, and where (DESIGN 7).

    ``project_id`` is set when the registry knew the project at session start and None when
    it was still to be made. ``slug`` is the name the session's digests file under - the
    project's slug, or the name an unregistered directory resolved to. ``path`` is the
    directory the project would be registered at, kept so the dream can register a
    still-unregistered project with it and find a registered one by it; it is None on a
    record an earlier build wrote, which kept no directory.
    """

    project_id: str | None
    slug: str
    path: str | None = None


def enqueue(
    config: Config, event: SessionEvent, *, busy_timeout_ms: int = HOOK_BUSY_TIMEOUT_MS
) -> None:
    """Record one session event as capture work, in one transaction.

    One INSERT and one commit, so the hook that calls it stays inside its budget and
    concurrent writers from both agents serialize on the write lock (DESIGN 8). An excluded
    session writes no row and leaves no trace. Nothing here opens the transcript, resolves
    a project, or touches a watermark.
    A session ``aft forget session`` removed files no row, whatever its hook says: the
    transcript is still on disk, and a row for it would put the forgotten words back.
    """
    if is_excluded(event.cwd, config):
        return
    now = _now()
    with _writing(config, busy_timeout_ms=busy_timeout_ms) as conn:
        if conn.execute(
            "SELECT 1 FROM forgotten WHERE session_id = ? LIMIT 1", (event.session_id,)
        ).fetchone():
            return
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


def session_cwd(config: Config, session_id: str) -> Path | None:
    """The directory the queue last saw this session running in, or None if it never saw it.

    A row is the only place a session's directory is written down (M2.2: a row carries no
    project, and the evidence archive keeps no path), so this is what a step that starts
    from a session id alone - ``aft redigest`` - has to go on when it asks whether the
    session was a worker's or sits under ``capture.exclude_paths``. The newest event wins,
    because a session that was moved mid-conversation is classified as it last stood.
    """
    with _reading(config) as conn:
        row = conn.execute(
            "SELECT cwd FROM jobs WHERE session_id = ?"
            " ORDER BY event_time DESC, created_time DESC, id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    return Path(row[0]) if row else None


def drop_job(config: Config, job: Job | str) -> None:
    """Remove a job row from the queue without recording it as done or failed."""
    job_id = job.id if isinstance(job, Job) else job
    with _writing(config) as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))


def forget_session(config: Config, session_id: str) -> int:
    """Remove one session's whole place in the queue, and say so to capture (DESIGN 12).

    Its job rows go, at every status: a row names the session's transcript and the directory
    it ran in, and a failed row keeps up to 200 characters of the model's own answer in
    ``error``. Its session row goes too, so its watermark and its ``digest_due`` debt go with
    it. A debt is what turns stored words back into a job (:func:`redeem_digest_debts`), so a
    debt left owed hands the forgotten chunk to the next run as if nothing was asked.

    What stays is one row naming the session and the moment it was forgotten, because the
    sweep measures a session against its own progress: with no rows left the session looks
    unread, and a transcript the agent still holds would be captured, digested and indexed
    again, putting the words straight back. ``enqueue`` refuses a session with that row, so
    neither a hook nor a sweep files work for it again. A session the queue never saw gets
    the row too, so a hook still to come for it files nothing.

    Returns how many job rows were removed.
    """
    moment = _text(_now())
    with _writing(config) as conn:
        removed = conn.execute("DELETE FROM jobs WHERE session_id = ?", (session_id,)).rowcount
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM session_projects WHERE session_id = ?", (session_id,))
        conn.execute(
            "INSERT INTO forgotten (session_id, forgotten_time) VALUES (?, ?)"
            " ON CONFLICT (session_id) DO UPDATE SET forgotten_time = excluded.forgotten_time",
            (session_id, moment),
        )
    return removed


# How long recording a session's project waits for the queue's write lock. It runs on the
# session-start hook after the memory block is printed, so a busy queue costs the session
# nothing it can see, and the wait is kept well inside the hook's budget.
RECORD_BUSY_TIMEOUT_MS = 200


def record_project(
    config: Config,
    agent: str,
    session_id: str,
    project_id: str | None,
    slug: str,
    path: Path | str | None = None,
    registry: Registry | None = None,
) -> bool:
    """Remember which project a session started in, and where (DESIGN 7).

    A session's directory can be gone by the time its words are digested: a worktree is made,
    worked in and removed. The project is decided while the directory still exists, at
    session start, and the digest reads it from here when the directory no longer answers.
    ``project_id`` is None for a project the registry does not hold yet, which is named by its
    slug alone; ``path`` is the directory the project would be registered at, which is what
    the dream registers a still-unregistered project from.

    For an unregistered directory the slug is settled here, inside the same write the record
    takes, so two sessions starting in two directories of one name cannot both claim it
    (DESIGN 7, F49): the name is checked against the registry - pass ``registry``, which the
    callers that resolve directories hold loaded - and against every slug another path's
    record holds, and parent directory names are prefixed until it is free. A digest that
    settles an unregistered directory's slug rewrites the record with the slug it filed
    under, so every digest of the session agrees on one place. A session `aft forget` removed
    is not recorded again. Returns whether a row was written.
    """
    with _writing(config, busy_timeout_ms=RECORD_BUSY_TIMEOUT_MS) as conn:
        if conn.execute(
            "SELECT 1 FROM forgotten WHERE session_id = ? LIMIT 1", (session_id,)
        ).fetchone():
            return False
        if project_id is None and path is not None and registry is not None:
            slug = _settled_slug(conn, registry, config, Path(path), slug)
        conn.execute(
            "INSERT INTO session_projects (agent, session_id, project_id, slug, path)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (agent, session_id) DO UPDATE SET"
            " project_id = excluded.project_id, slug = excluded.slug, path = excluded.path",
            (agent, session_id, project_id, slug, None if path is None else str(path)),
        )
    return True


_CLAIM_ROWS = "SELECT slug, path, project_id, agent, session_id FROM session_projects"

# The directory a record's session ran in. Only a record that names no path needs it - a
# record of this build carries its path - so this query reads it for those alone, keeping
# the session-start record's cost flat in the tables' growth (T31).
_CLAIM_CWDS = (
    "SELECT agent, session_id,"
    " (SELECT j.cwd FROM jobs j WHERE j.agent = session_projects.agent"
    "   AND j.session_id = session_projects.session_id"
    "  ORDER BY j.event_time DESC, j.created_time DESC, j.id DESC LIMIT 1) AS cwd"
    " FROM session_projects WHERE path IS NULL"
)


def _claim_rows(
    conn: sqlite3.Connection,
) -> list[tuple[str, str | None, str | None, str, str, str | None]]:
    """Every session-start record as (slug, path, project id, agent, session id, cwd).

    The session's directory is fetched only for the records that name no path (T31): the
    settlement walk consults it for a pathless record alone, and a record of this build
    carries its path, so the hook that writes one record does not pay a lookup into jobs
    for every row the table holds. A record whose session left no row in jobs reads as
    None, and its claim is then a claim of no directory the queue can name.
    """
    rows = [
        (slug, path, project_id, agent, session_id, None)
        for slug, path, project_id, agent, session_id in conn.execute(_CLAIM_ROWS)
    ]
    cwds = {(agent, session_id): cwd for agent, session_id, cwd in conn.execute(_CLAIM_CWDS)}
    return [row[:5] + (cwds.get((row[3], row[4])),) for row in rows]


def _settled_slug(
    conn: sqlite3.Connection, registry: Registry, config: Config, root: Path, base: str
) -> str:
    """A slug this directory can file its digests under, decided against the whole claim table.

    Every session-start record is a claim: the slug it holds belongs to the directory its
    session ran in, so a slug another directory holds is not free for this one, whatever
    named it first (DESIGN 7, F49). A record naming a project the registry holds is that
    project's own claim, whose name the registry governs. The registry's own slugs
    ``unique_slug`` answers for; the walk it makes is prefixed with parent directory names
    until nothing claims the name.

    A record that names no path is one an earlier build wrote. Its session's directory is
    what the queue last saw for that session, so the record is this directory's own claim
    only when that directory resolves to ``root`` - a record of the same name whose session
    ran anywhere else is another directory's claim, and the name walks aside for it. A
    record that is adopted is given the path it was missing, so the sessions recorded
    before and after the upgrade file under one name and the dream makes one project of
    them.
    """
    rows = _claim_rows(conn)
    settled, own = _settle_from(rows, registry, config, root, base)
    for agent, session_id in own:
        conn.execute(
            "UPDATE session_projects SET path = ?"
            " WHERE agent = ? AND session_id = ? AND path IS NULL",
            (str(root), agent, session_id),
        )
    return settled


def _settle_from(
    rows: Iterable[tuple[str, str | None, str | None, str, str, str | None]],
    registry: Registry,
    config: Config,
    root: Path,
    base: str,
) -> tuple[str, list[tuple[str, str]]]:
    """The name ``root`` may file under, read off the claim table, and the records to adopt.

    ``rows`` holds every session-start record as (slug, path, project id, agent, session
    id, the session's directory). A claim is a name another directory holds, so it takes
    the name away from ``root``: a claim naming a project the registry holds is the
    registry's to govern, and a claim naming ``root`` itself is the directory's own. A
    claim with no path and exactly ``base`` is this directory's own only when the record's
    session ran in a directory that resolves to ``root``; the same name claimed by a
    session that ran anywhere else, or a session the queue no longer knows the directory
    of, holds the name against ``root`` (DESIGN 7, F49). Returns the settled name and the
    (agent, session id) pairs of the pathless claims that were adopted - so only when
    ``base`` survived the walk - each of which the caller may give ``root`` as its path.
    """
    taken: set[str] = set()
    own: list[tuple[str, str]] = []
    roots: dict[str | None, Path | None] = {}

    def session_root(cwd: str | None) -> Path | None:
        """The directory the record's session ran in, resolved the way a digest resolves."""
        if cwd not in roots:
            roots[cwd] = (
                None
                if cwd is None
                else keys.resolve(
                    Path(cwd), registry, config.host, never=config.settings.capture.exclude_paths
                ).root
            )
        return roots[cwd]

    for slug, path, project_id, agent, session_id, cwd in rows:
        if project_id is not None and registry.by_id(project_id) is not None:
            continue  # a registered project's claim: the registry's names answer for it
        if path == str(root):
            continue  # this directory's own claim
        if path is None and slug == base and session_root(cwd) == root:
            own.append((agent, session_id))  # a pathless record of this directory's own
            continue
        taken.add(slug)
    settled = unique_slug(root, registry, base=base, taken=taken)
    return settled, own if settled == base else []


def settle_slug(config: Config, registry: Registry, root: Path, base: str) -> str:
    """The name ``root`` would settle for itself, decided without writing (DESIGN 7, F49).

    The same decision a session-start record makes, asked by a step that must not claim
    anything: the walk against the registry and every live claim, with a pathless record
    counted as the claim of the directory its session ran in.
    """
    with _reading(config) as conn:
        rows = _claim_rows(conn)
    settled, _ = _settle_from(rows, registry, config, root, base)
    return settled


def settled_slugs(config: Config, registry: Registry) -> dict[Path, str]:
    """The name each recorded directory's sessions file their digests under (DESIGN 7, F49).

    A record is the answer its session's digest files under, so a step that dates a
    directory's sessions - the dream's quiet check, the idle and gone report - reads the
    record's name rather than resolving the directory afresh: a fresh answer can differ
    from the one the record was written with, and then the sessions look active under a
    name no digest uses. A record naming a project the registry holds decides its
    directory, because that project is where the session's digest files; records of one
    path that disagree over an unregistered name are an answer only in the sense that one
    of them must stand, and the first in sort order does.
    """
    with _reading(config) as conn:
        rows = conn.execute(
            "SELECT path, project_id, slug FROM session_projects WHERE path IS NOT NULL"
        ).fetchall()
    by_path: dict[Path, list[tuple[str, str]]] = {}
    for path, project_id, slug in rows:
        by_path.setdefault(Path(str(path)), []).append(
            ("" if project_id is None else str(project_id), str(slug))
        )
    slugs: dict[Path, str] = {}
    for path, candidates in by_path.items():
        named = [
            project.slug
            for project_id, _ in candidates
            if project_id and (project := registry.by_id(project_id)) is not None
        ]
        slugs[path] = sorted(named)[0] if named else sorted(slug for _, slug in candidates)[0]
    return slugs


def adopt_claim(config: Config, registry: Registry, root: Path, base: str) -> str:
    """Settle a name for ``root`` the way a session-start record does, and keep the table.

    A project made by hand - ``aft project add`` with no name of its own - takes the name
    the same walk gives a session-start record: against the registry and every live claim
    (DESIGN 7, F49). A pathless record whose session ran in this directory is this
    directory's claim, and is given the path it was missing, so the project made here and
    the sessions already recorded under the name are one project. A record of the same
    name whose session ran anywhere else holds the name against this directory.
    """
    with _writing(config) as conn:
        rows = _claim_rows(conn)
        settled, own = _settle_from(rows, registry, config, root, base)
        for agent, session_id in own:
            conn.execute(
                "UPDATE session_projects SET path = ?"
                " WHERE agent = ? AND session_id = ? AND path IS NULL",
                (str(root), agent, session_id),
            )
    return settled


def claimed_slugs(config: Config, registry: Registry, *, keep: Collection[str] = ()) -> set[str]:
    """The names the queue's records hold for directories other than ``keep`` (DESIGN 7, F49).

    Every session-start record is a claim: the slug it holds belongs to the directory its
    session ran in, so a project made another way may not take the name. A record naming a
    project the registry holds is left out, the registry governing that name. A record with
    no path is one an earlier build wrote; its claim's directory is what the queue last saw
    for its session, and one whose session's directory resolves to a ``keep`` path is that
    directory's own claim. ``keep`` names the paths whose own claims do not count against
    the caller: the directory a project is being made from, or the directories of a project
    being renamed.
    """
    with _reading(config) as conn:
        rows = _claim_rows(conn)
    keep_paths = {Path(path) for path in keep}
    roots: dict[str | None, Path | None] = {}

    def session_root(cwd: str | None) -> Path | None:
        """The directory the record's session ran in, resolved the way a digest resolves."""
        if cwd not in roots:
            roots[cwd] = (
                None
                if cwd is None
                else keys.resolve(
                    Path(cwd), registry, config.host, never=config.settings.capture.exclude_paths
                ).root
            )
        return roots[cwd]

    claimed: set[str] = set()
    for slug, path, project_id, _, _, cwd in rows:
        if project_id is not None and registry.by_id(project_id) is not None:
            continue
        if path is not None:
            if Path(path) not in keep_paths:
                claimed.add(slug)
        elif session_root(cwd) not in keep_paths:
            claimed.add(slug)
    return claimed


def recorded_project(config: Config, agent: str, session_id: str) -> ProjectRecord | None:
    """The project a session started in, as its session-start record keeps it (DESIGN 7)."""
    with _reading(config) as conn:
        row = conn.execute(
            "SELECT project_id, slug, path FROM session_projects"
            " WHERE agent = ? AND session_id = ?",
            (agent, session_id),
        ).fetchone()
    return ProjectRecord(row[0], row[1], row[2]) if row else None


def next_job(config: Config, *, role: str | None = None) -> Job | None:
    """Claim the oldest waiting job whose session has no other job being digested.

    A session digests one chunk at a time because it has one watermark to move, and the
    chunks go oldest first so a later chunk never reads past an earlier one.

    When role is specified, only a job matching that role (primary or worker) is claimed.
    When role is omitted, primary jobs take priority over worker jobs. Any row whose cwd is
    under capture.exclude_paths is dropped.
    """
    now = _now()
    # The lease is the larger of the cadence the work timer runs at and the floor the
    # design's quiet period sets: a row a live process holds stays claimed.
    lease = max(timedelta(minutes=config.settings.dream.interval_minutes), CLAIM_LEASE_FLOOR)
    with _writing(config) as conn:
        _reclaim_stale_claims(conn, now, lease)
        rows = conn.execute(
            "SELECT id, agent, session_id, cwd, transcript_path, kind, event_time"
            " FROM jobs j"
            " WHERE status = ?"
            "   AND NOT EXISTS ("
            "       SELECT 1 FROM jobs held"
            "       WHERE held.agent = j.agent AND held.session_id = j.session_id"
            "         AND held.status = ?)"
            " ORDER BY created_time, id",
            (PENDING, RUNNING),
        ).fetchall()
        for row in rows:
            cwd = Path(row[3])
            if is_excluded(cwd, config):
                conn.execute("DELETE FROM jobs WHERE id = ?", (row[0],))
                continue
            worker = is_worker(cwd, config)
            if role == "primary" and worker:
                continue
            if role == "worker" and not worker:
                continue
            if role is None and worker:
                has_pending_primary = any(
                    not is_excluded(Path(r[3]), config) and not is_worker(Path(r[3]), config)
                    for r in rows
                )
                if has_pending_primary:
                    continue
            conn.execute(
                "UPDATE jobs SET status = ?, claimed_pid = ?, claimed_time = ?, updated_time = ?"
                " WHERE id = ?",
                (RUNNING, os.getpid(), _text(now), _text(now), row[0]),
            )
            return _job(conn, row)
        return None


def pending_jobs(config: Config, *, role: str | None = None) -> list[Job]:
    """Waiting jobs, oldest first, optionally filtered by role."""
    with _reading(config) as conn:
        rows = conn.execute(
            "SELECT id, agent, session_id, cwd, transcript_path, kind, event_time"
            " FROM jobs j"
            " WHERE status = ?"
            "   AND NOT EXISTS ("
            "       SELECT 1 FROM jobs held"
            "       WHERE held.agent = j.agent AND held.session_id = j.session_id"
            "         AND held.status = ?)"
            " ORDER BY created_time, id",
            (PENDING, RUNNING),
        ).fetchall()
        result = []
        for row in rows:
            cwd = Path(row[3])
            if is_excluded(cwd, config):
                continue
            worker = is_worker(cwd, config)
            if role == "primary" and worker:
                continue
            if role == "worker" and not worker:
                continue
            result.append(_job(conn, row))
        return result


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
        # A digest that closed a job for this session read everything from the watermark to
        # the end of its transcript, so the archive's debt is settled by this close and not
        # by the page: a chunk the filter emptied owes no digest either (M2.7).
        conn.execute(
            "UPDATE sessions SET digest_due = 0 WHERE agent = ? AND session_id = ?",
            (job.agent, job.session_id),
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
    silence: why the digest could not run has to reach the report. A session the archive
    still owes a digest to stays owing when its job fails, so the digest is tried again
    rather than quietly dropped: a worker that went quiet the moment it was stored has no
    later sweep to enqueue its chunk for it (M2.7).
    """
    with _writing(config) as conn:
        recorded = conn.execute(
            "UPDATE jobs SET status = ?, claimed_pid = NULL, error = ?, updated_time = ?"
            " WHERE id = ? AND status = ? AND claimed_pid = ?",
            (FAILED, error, _text(_now()), job.id, RUNNING, os.getpid()),
        ).rowcount
        if not recorded:
            raise QueueError(f"job {job.id} is not held by this process")


def owe_digest(config: Config, job: Job) -> None:
    """Close a worker row the archive stored, and record that its digest is owed.

    Archiving read the transcript and put its words in the archive and the index, so the
    capture work the row stands for is finished and the row closes as any finished row
    does - with its session's watermark left where the last digest left it, so the digest
    this run could not afford reads the same chunk when budget allows. The row closes rather
    than waits because a waiting row is capture work in flight, and capture stops following
    a session that has one: an archived worker whose row stayed pending would never see its
    later text reach the vault, which is the hole this fills (BUILD-PLAN M2.7, the lead's
    review of #39).

    What is still owed goes on the session row instead, because capture never reads that
    table: the debt says the words are stored and no digest has read them, and
    :func:`redeem_digest_debts` is what turns it back into work. Only :func:`complete`
    clears it, so neither a failed digest nor a run that died mid-claim can lose it.
    """
    now = _now()
    with _writing(config) as conn:
        closed = conn.execute(
            "UPDATE jobs SET status = ?, claimed_pid = NULL, error = NULL, updated_time = ?"
            " WHERE id = ? AND status = ?",
            (DONE, _text(now), job.id, PENDING),
        ).rowcount
        if not closed:
            raise QueueError(f"job {job.id} is no longer waiting to be archived")
        conn.execute(
            "INSERT INTO sessions (agent, session_id, watermark, off_record, digest_due)"
            " VALUES (?, ?, NULL, 0, 1)"
            " ON CONFLICT (agent, session_id) DO UPDATE SET digest_due = 1",
            (job.agent, job.session_id),
        )


def close_waiting(config: Config, job: Job) -> None:
    """Close a waiting row that read nothing worth keeping, and owe nothing for it.

    The archiving path claims nothing, so this closes on the row's state alone, the way
    :func:`owe_digest` does. What it leaves is a closed row and a session whose watermark,
    span and debt are exactly as they were: the transcript was read and its text filed
    nowhere, and a session that goes on to say more is captured from its start (DESIGN 8).
    A row left waiting instead would be capture work in flight, and capture stops following
    a session that has one.
    """
    now = _now()
    with _writing(config) as conn:
        closed = conn.execute(
            "UPDATE jobs SET status = ?, claimed_pid = NULL, error = NULL, updated_time = ?"
            " WHERE id = ? AND status = ?",
            (DONE, _text(now), job.id, PENDING),
        ).rowcount
        if not closed:
            raise QueueError(f"job {job.id} is no longer waiting in the queue")


def redeem_digest_debts(config: Config) -> int:
    """File one waiting job for every session the archive owes a digest to, and count them.

    A debt is not a job, and only jobs can be claimed, leased, digested, and closed, so the
    debt becomes one here. A session that already has a row waiting or in flight is left
    alone: the digest that row brings settles the debt, and filing a second row for one
    session would be a chunk read twice (M2.2 digests a session one chunk at a time).

    The new row carries the event time of the session's newest row, not this moment, because
    that time is what the dream's quiet period measures a directory's last sign of life by
    (BUILD-PLAN M4.1), and a debt being collected says nothing about when the session itself
    last spoke. The debt outlives this call and is cleared by the digest that closes the row,
    so a failure to digest leaves it owed for the next run rather than lost.

    A debt is dropped, not filed, when there is nothing left to digest - no row of that
    session names a transcript, or the directory is now under ``capture.exclude_paths``. An
    excluded session leaves no trace in the queue, and a debt naming one is a trace (M2.7).

    Returns how many jobs were filed. A debt left owed - because the session already has a
    row in flight, or because filing it is what this call just did - is tried again by the
    next run, and the digest that eventually closes a job for that session is what clears it.
    """
    now = _now()
    filed = 0
    with _writing(config) as conn:
        owed = conn.execute(
            "SELECT agent, session_id FROM sessions WHERE digest_due = 1 ORDER BY agent, session_id"
        ).fetchall()
        for agent, session_id in owed:
            newest = conn.execute(
                "SELECT cwd, transcript_path, kind, event_time FROM jobs"
                " WHERE agent = ? AND session_id = ?"
                " ORDER BY created_time DESC, id DESC LIMIT 1",
                (agent, session_id),
            ).fetchone()
            if newest is None or newest[1] is None or is_excluded(Path(newest[0]), config):
                conn.execute(
                    "UPDATE sessions SET digest_due = 0 WHERE agent = ? AND session_id = ?",
                    (agent, session_id),
                )
                continue
            waiting = conn.execute(
                "SELECT 1 FROM jobs WHERE agent = ? AND session_id = ? AND status IN (?, ?)"
                " LIMIT 1",
                (agent, session_id, PENDING, RUNNING),
            ).fetchone()
            if waiting is not None:
                continue
            conn.execute(
                "INSERT INTO jobs (id, agent, session_id, cwd, transcript_path, kind,"
                " event_time, status, created_time, updated_time)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id("job"),
                    agent,
                    session_id,
                    newest[0],
                    newest[1],
                    newest[2],
                    newest[3],
                    PENDING,
                    _text(now),
                    _text(now),
                ),
            )
            filed += 1
    return filed


def session_state(config: Config, agent: str, session_id: str) -> SessionState:
    """The queue's facts about one session: where its digest read to, whether a span is open
    there, and whether it has capture work in flight.

    The state of a session the queue has no row for is no watermark and no span open, which
    is what a first chunk of any session starts with.
    """
    with _reading(config) as conn:
        row = conn.execute(
            "SELECT watermark, off_record, digest_due FROM sessions"
            " WHERE agent = ? AND session_id = ?",
            (agent, session_id),
        ).fetchone()
        open_row = conn.execute(
            "SELECT 1 FROM jobs WHERE agent = ? AND session_id = ? AND status IN (?, ?) LIMIT 1",
            (agent, session_id, PENDING, RUNNING),
        ).fetchone()
        read = conn.execute(
            # The moment the queue last read this session's text: when its newest finished
            # job was claimed, or - for a worker row the archive stored without digesting,
            # which no process ever claimed - the moment it was stored (M2.7).
            "SELECT max(coalesce(claimed_time, updated_time)) FROM jobs"
            " WHERE agent = ? AND session_id = ? AND status = ?",
            (agent, session_id, DONE),
        ).fetchone()
        newest = conn.execute(
            "SELECT status, updated_time FROM jobs WHERE agent = ? AND session_id = ?"
            " ORDER BY created_time DESC, id DESC LIMIT 1",
            (agent, session_id),
        ).fetchone()
        forgotten = conn.execute(
            "SELECT 1 FROM forgotten WHERE session_id = ? LIMIT 1", (session_id,)
        ).fetchone()
    return SessionState(
        watermark=row[0] if row else None,
        off_record=bool(row[1]) if row else False,
        has_open_job=open_row is not None,
        digest_due=bool(row[2]) if row else False,
        read_at=_moment(read[0]),
        failed_at=_moment(newest[1]) if newest and newest[0] == FAILED else None,
        forgotten=forgotten is not None,
    )


def last_events(config: Config) -> dict[Path, datetime]:
    """The newest event time the queue holds for each working directory, whatever became
    of the job it arrived on.

    This is a session's last sign of life as the queue knows it, which the dream's quiet
    period is measured from (DESIGN 9, BUILD-PLAN M4.1). It is keyed by directory, not by
    project, because a row carries no project (M2.2): the reader resolves each directory.
    """
    with _reading(config) as conn:
        # Every event time is stored as UTC ISO 8601 (_text), so the newest sorts last.
        rows = conn.execute("SELECT cwd, max(event_time) FROM jobs GROUP BY cwd").fetchall()
    return {Path(cwd): datetime.fromisoformat(text) for cwd, text in rows}


def project_activity(config: Config) -> dict[str, datetime]:
    """The newest event time the queue holds for each project a session-start record names.

    Keyed by the record's project id, this is a project's own last sign of life, read from
    the records rather than from the directories the events ran in, so a removed worktree
    does not make its project look quieter than it is (DESIGN 7, F49). A record that names
    no project id - one an earlier build wrote, or a session of no project - is not here;
    the reader of this answers for those from the directories instead.
    """
    with _reading(config) as conn:
        rows = conn.execute(
            "SELECT sp.project_id, max(j.event_time) FROM session_projects sp"
            " JOIN jobs j ON j.agent = sp.agent AND j.session_id = sp.session_id"
            " WHERE sp.project_id IS NOT NULL GROUP BY sp.project_id"
        ).fetchall()
    return {project_id: datetime.fromisoformat(text) for project_id, text in rows}


def watching_since(config: Config, now: datetime) -> datetime:
    """When this host began watching for sessions, recording ``now`` the first time.

    A session the queue has never digested counts as capture work only if it moved after
    this moment, so a new home with weeks of transcripts does not spend the model budget
    on sessions that predate afterthought (BUILD-PLAN M2.2).
    """
    with _writing(config) as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING",
            (WATCHING_SINCE, _text(now)),
        )
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (WATCHING_SINCE,)).fetchone()
    return datetime.fromisoformat(row[0])


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
def _writing(config: Config, *, busy_timeout_ms: int | None = None):
    """One connection held for one transaction, taken immediately.

    ``BEGIN IMMEDIATE`` takes the write lock before the first row is written, so two
    writers coming from the two agents take their rows in turn instead of one of them
    losing its row to a busy database (DESIGN 8). ``busy_timeout_ms`` shortens how long this
    writer waits for that lock, for a caller with less time than the queue's own default.
    """
    conn = _connect(config, busy_timeout_ms=busy_timeout_ms)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _session_project_columns(conn: sqlite3.Connection) -> set[str]:
    """The columns the queue's session_projects table names right now."""
    return {row[1] for row in conn.execute("PRAGMA table_info(session_projects)")}


def _connect(config: Config, *, busy_timeout_ms: int | None = None) -> sqlite3.Connection:
    """Open the queue, creating it on first use.

    Autocommit is left on so the transactions in this module are only the ones written
    here. WAL is what lets the digest read while a hook writes; ``synchronous=NORMAL`` is
    SQLite's companion setting for it, and a commit lost to a power cut is a missed hook,
    which the sweep recovers (DESIGN 14).
    """
    path = config.home / QUEUE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    timeout = BUSY_TIMEOUT_MS if busy_timeout_ms is None else int(busy_timeout_ms)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout = {timeout}")
    conn.execute("PRAGMA synchronous = NORMAL")
    if _journal_mode(conn) != WAL_MODE:
        _enter_wal(conn, timeout_ms=timeout)
    conn.executescript(_SCHEMA)
    if "path" not in _session_project_columns(conn):
        # A queue an earlier build wrote names no path on a session-start record. The column
        # is nullable, so its records read as ones kept before the directory was recorded
        # (F49) rather than refusing an upgrade that would cost every record.
        try:
            conn.execute("ALTER TABLE session_projects ADD COLUMN path TEXT")
        except sqlite3.OperationalError:
            # Two hooks starting together right after the upgrade can both pass the column
            # check before either ALTER lands; the loser finds the winner's column here,
            # the way _enter_wal settles its own race (DESIGN 8).
            if "path" not in _session_project_columns(conn):
                raise
    sessions = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    jobs = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "off_record" not in sessions or "digest_due" not in sessions or "claimed_time" not in jobs:
        conn.close()
        raise QueueError(
            f"{path} was written by an earlier build of afterthought; delete it to start"
            " the queue over"
        )
    return conn


def _journal_mode(conn: sqlite3.Connection) -> str:
    return conn.execute("PRAGMA journal_mode").fetchone()[0]


def _enter_wal(conn: sqlite3.Connection, *, timeout_ms: int = BUSY_TIMEOUT_MS) -> None:
    """Put the queue into WAL mode, once, without losing a race to create it.

    Changing the journal mode wants the database to itself and reports ``database is
    locked`` at once when a writer holds it, so a hook asked to change it on every
    enqueue would fail the moment two hooks wrote the first rows of a new queue. Each
    connection asks only while the mode is still the default one, and a loser of that
    race finds the winner's WAL a moment later, so every row still lands (DESIGN 8).
    A mode that never arrives inside the busy timeout is a queue no writer can count
    on, so it is reported rather than waited on without end.
    """
    deadline = time.monotonic() + timeout_ms / 1000
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


def _moment(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)


def _text(moment: datetime) -> str:
    """An instant as the queue stores it: UTC ISO 8601, which sorts as a string."""
    return moment.astimezone(UTC).isoformat()


def _now() -> datetime:
    return datetime.now(UTC)
