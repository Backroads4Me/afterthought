"""The evidence archive: filtered conversation kept compressed in the home, outside git.

DESIGN 8 makes the archive short-term evidence rather than memory. It is there so that
search and `aft trace` see both agents' sessions as one record, independent of what each
agent does with its own transcripts (Claude Code keeps 90 days on this host; pi sets no
limit), and so `aft redigest` can rebuild a recent digest from what is still on disk
(BUILD-PLAN M2.8). Nothing in it is injected: the memories are the digests and the
curated pages, and those keep their quotes for good, which is what lets the conversation
behind them expire.

The file is ``evidence.sqlite`` in the afterthought home, beside the queue and the vault
lock: per-host state, not memory, so it is outside the vault's git and is never shared
between machines. One row goes in per filtered entry, keyed by the session and entry ids
the readers gave it, with the text zlib-compressed. Re-storing a session - a chunk that
overlaps one already archived, or a whole session read again after a watermark was lost -
replaces its rows instead of adding copies, so a rerun leaves one row per entry.

A row that cannot be read back is reported rather than skipped. DESIGN 14 lets an expired
transcript go because its digest kept the quote; a row that is present and unreadable is
another thing, and dropping it quietly would take text out of `aft trace` and `aft search`
without telling anyone.
"""

from __future__ import annotations

import re
import sqlite3
import zlib
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aft.config import Config
from aft.readers import Entry

EVIDENCE_NAME = "evidence.sqlite"

# A reader of the archive should not have to give up while a writer is in it, so a lock is
# waited out rather than failed on: `aft trace` runs in front of a person, and whoever holds
# the write lock is the digest archiving a chunk or one night's purge. SQLite's connect
# `timeout` is this wait; a PRAGMA of the same name takes no parameter to pass it in.
BUSY_TIMEOUT_SECONDS = 5.0

WAL_MODE = "wal"

# Whitespace runs, and the single space they become: `find_quote` compares text with its
# line wrapping and spacing set aside, and nothing else (DESIGN 8, digest).
_WHITESPACE = re.compile(r"\s+")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence (
    session_id TEXT NOT NULL,
    entry_id   TEXT NOT NULL,
    agent      TEXT NOT NULL,
    project_id TEXT,
    time       TEXT NOT NULL,
    role       TEXT NOT NULL,
    text       BLOB NOT NULL,
    is_error   INTEGER NOT NULL,
    PRIMARY KEY (session_id, entry_id)
);
CREATE INDEX IF NOT EXISTS evidence_by_time ON evidence (time);
"""


class EvidenceError(Exception):
    """An archive operation that cannot be honoured: an entry with nothing to file it by,
    a retention window that is not a window, or an archive that will not open or will not
    give its text back."""


def store(
    config: Config,
    agent: str,
    session_id: str,
    project_id: str | None,
    entries: Iterable[Entry],
) -> None:
    """Archive ``entries`` as the evidence of one session, in one transaction.

    The caller is the digest step (BUILD-PLAN M2.6), which hands over what the filter kept
    of a chunk: ``agent`` and ``session_id`` say whose session it was, and ``project_id``
    is what the event's directory resolved to, or None for a session that resolved to no
    project (DESIGN 7). It is stored beside the text rather than derived from it because
    the archive outlives the directory the session happened to be in.

    Every row of one call lands together or none of them do, so a chunk that turns out
    halfway through to be unarchivable leaves nothing behind for a later read to mistake
    for the whole chunk. Text is compressed on the way in; `entries` gives it back as it
    came. An empty batch - a chunk the filter threw all of - writes nothing and leaves no
    archive behind.
    """
    session_id = _require_name(session_id, "session id")
    agent = _require_name(agent, "agent")
    rows = [_row(entry, session_id, agent, project_id) for entry in entries]
    if not rows:
        return
    with _writing(config) as conn:
        conn.executemany(
            "INSERT INTO evidence (session_id, entry_id, agent, project_id, time, role,"
            " text, is_error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            # A session is stored once per chunk read, and chunks can overlap: the same
            # entry arriving again carries the same text, so the row is written again
            # rather than duplicated. A conflict left unhandled would be a rerun that
            # fails, and a rerun is what BUILD-PLAN M2 says has to add nothing.
            " ON CONFLICT (session_id, entry_id) DO UPDATE SET"
            " agent = excluded.agent,"
            " project_id = excluded.project_id,"
            " time = excluded.time,"
            " role = excluded.role,"
            " text = excluded.text,"
            " is_error = excluded.is_error",
            rows,
        )


def entries(config: Config, session_id: str) -> list[Entry]:
    """The archived entries of one session, oldest first.

    What `aft trace` and `aft redigest` read: the same `Entry` records the filter handed
    over, ids, times, roles and error flags included. Order comes off each entry's own
    time rather than off the order rows were written, because a session read again after
    a lost watermark adds the entries it missed last time at the end of the file. Entries
    sharing one timestamp - a Claude message and the tool results split out of it - keep
    the order they were written in.

    A session with no rows - forgotten, purged, never captured - reads as no entries
    rather than as an error, which is the answer `aft trace` reports when only the
    digest's quote survives (DESIGN 14).
    """
    session_id = _require_name(session_id, "session id")
    if not _db_path(config).exists():
        return []
    with _reading(config) as conn:
        rows = conn.execute(
            "SELECT session_id, entry_id, time, role, text, is_error"
            " FROM evidence WHERE session_id = ? ORDER BY time, rowid",
            (session_id,),
        ).fetchall()
    return [_entry(row) for row in rows]


def find_quote(config: Config, session_id: str, entry_id: str, quote: str) -> bool:
    """Whether ``quote`` is the words of one archived entry, spacing aside.

    The digest drops a fact whose quote does not appear in the entry it cites, compared
    with whitespace normalized (DESIGN 8). Whitespace is all that is normalized: runs of
    it, wrapping included, become single spaces on both sides, while letters, case and
    punctuation still have to match. A matcher loose about those would let a fact stand
    on words nobody said, which is the one thing a quote is there to prevent.

    No match is the answer, never an error, when the session or the entry is not in the
    archive: the digest's rule is to drop the fact. A quote that holds nothing but
    whitespace is not words copied from a transcript, so it verifies nothing.
    """
    session_id = _require_name(session_id, "session id")
    entry_id = _require_name(entry_id, "entry id")
    wanted = _spaced(quote)
    if not wanted:
        return False
    if not _db_path(config).exists():
        return False
    with _reading(config) as conn:
        row = conn.execute(
            "SELECT text FROM evidence WHERE session_id = ? AND entry_id = ?",
            (session_id, entry_id),
        ).fetchone()
    if row is None:
        return False
    return wanted in _spaced(_unpack(row[0], entry_id))


def purge(config: Config, now: datetime) -> int:
    """Drop the archived entries older than ``evidence.retention_days``, and say how many.

    The window is counted back from ``now``, which the caller passes so a run and a test
    can both name the moment they mean. An entry exactly ``retention_days`` old is not
    older than the window and stays; one past it goes. 0 is a window that keeps nothing
    but the moment itself, and a negative number is not a window at all: it would drop
    entries dated after ``now``, so it is refused rather than taken as a way to empty the
    archive (DESIGN 8). A window so wide that counting it back runs past the first moment
    a timestamp can hold is the same mistake at the other end and is refused the same way
    - the setting has no spelling for "keep everything", and a huge number is not one.

    The space the rows took stays in the file for the entries that come after, which is
    how the archive holds its size without a VACUUM of its own. The text itself does not
    stay: its pages are written over on the way to the free list, and what went is written
    out of the write-ahead log (DESIGN 12).
    """
    days = config.settings.evidence.retention_days
    if days < 0:
        raise EvidenceError(
            f"[evidence] retention_days = {days} is not a retention window;"
            " 0 keeps nothing older than now"
        )
    try:
        cutoff = _text(now - timedelta(days=days), "purge's now")
    except OverflowError as exc:
        raise EvidenceError(
            f"[evidence] retention_days = {days} is not a retention window; counting"
            f" {days} days back from {now.isoformat()} is no moment to file by"
        ) from exc
    if not _db_path(config).exists():
        return 0
    with _writing(config) as conn:
        removed = conn.execute("DELETE FROM evidence WHERE time < ?", (cutoff,)).rowcount
    if removed:
        _release(config, rewrite=False)
    return removed


def forget(config: Config, session_id: str) -> None:
    """Remove every archived entry of one session, and the text they held (DESIGN 12,
    `aft forget session`).

    The rows go, the write-ahead log is emptied, and the archive is rewritten, so the
    forgotten words are not left in a freed page or in the log for whoever opens the file
    next. Rewriting the archive is why this is a command a person runs, not a sweep.

    The archive only. The index rows, the digest files and the curated lines resting on
    the session are the command's to remove and withdraw (BUILD-PLAN M2.8), and the
    vault's git history is beyond any of them: forgetting explains how to rewrite that,
    and never does it.

    A session with no rows, and an archive that was never written, both do nothing.
    """
    session_id = _require_name(session_id, "session id")
    if not _db_path(config).exists():
        return
    with _writing(config) as conn:
        conn.execute("DELETE FROM evidence WHERE session_id = ?", (session_id,))
    _release(config, rewrite=True)


def _row(entry: Entry, session_id: str, agent: str, project_id: str | None) -> tuple:
    """One entry as the row it becomes, with its text compressed and its key checked."""
    return (
        session_id,
        _require_name(entry.id, "entry id"),
        agent,
        _optional_name(project_id, "project id"),
        _text(entry.time, f"entry {entry.id}"),
        _require_name(entry.role, "role"),
        _pack(entry),
        1 if entry.is_error else 0,
    )


def _entry(row: tuple) -> Entry:
    """One row as the record it was stored from."""
    session_id, entry_id, time_text, role, blob, is_error = row
    return Entry(
        id=entry_id,
        time=datetime.fromisoformat(time_text),
        role=role,
        text=_unpack(blob, entry_id),
        is_error=bool(is_error),
    )


def _spaced(text: str) -> str:
    """``text`` with every run of whitespace, wrapping included, become one space."""
    return _WHITESPACE.sub(" ", text).strip()


def _pack(entry: Entry) -> bytes:
    """The entry's text, compressed: filtered conversation is the bulk of the archive, and
    the retention window keeps a long run of it (DESIGN 8)."""
    if not isinstance(entry.text, str):
        raise EvidenceError(f"entry {entry.id} holds text that is not text")
    return zlib.compress(entry.text.encode("utf-8"))


def _unpack(blob: bytes, entry_id: str) -> str:
    try:
        return zlib.decompress(bytes(blob)).decode("utf-8")
    except (zlib.error, UnicodeDecodeError) as exc:
        raise EvidenceError(
            f"the archived text of entry {entry_id} does not decompress as it was written"
        ) from exc


def _require_name(value: str, what: str) -> str:
    """A string a row is filed by or read back as: a session, agent or entry id, or a role.

    A row missing one cannot be found again, or cannot be read for what it was, so it is
    refused as the hole in the archive it would otherwise be. The ids the callers pass are
    what `entries` and `find_quote` look a session up by, and the role is what tells the
    text its place in the conversation.
    """
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{what} {value!r} is not a name to file evidence by")
    return value


def _optional_name(value: str | None, what: str) -> str | None:
    """A name a row may legitimately not have, such as the project a session resolved to.

    ``None`` is the only spelling of none. A blank id would file one session as both no
    project and a project, which is one fact held two ways for `aft search --project` to
    disagree about (DESIGN 7).
    """
    if value is None:
        return None
    return _require_name(value, what)


def _text(moment: object, what: str) -> str:
    """An instant as the archive stores it: UTC ISO 8601, which sorts as a string.

    A timestamp with no offset is read as UTC rather than as local time. The readers hand
    over UTC (``aft.readers.parse_time``), and a transcript's own times are what an entry
    is filed by, so a local reading would file the same session at a different age on two
    hosts and let `purge` keep or drop entries by where the machine happens to be.
    """
    if not isinstance(moment, datetime):
        raise EvidenceError(f"{what} has no timestamp to file it by, got {moment!r}")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


@contextmanager
def _reading(config: Config) -> Iterator[sqlite3.Connection]:
    """One connection for a select, and for the WAL work that cannot run in a transaction.

    A database that will not give its text back - unreadable, damaged, or held by someone
    who opened the archive with another tool - is reported as EvidenceError naming the
    file, the way every other failure here is.
    """
    path = _db_path(config)
    conn = _connect(config)
    try:
        yield conn
    except sqlite3.Error as exc:
        raise EvidenceError(f"cannot use the evidence archive at {path}: {exc}") from exc
    finally:
        conn.close()


@contextmanager
def _writing(config: Config) -> Iterator[sqlite3.Connection]:
    """One connection held for one transaction, its write lock taken before the first row.

    ``BEGIN IMMEDIATE`` is what makes two writers - the digest archiving a chunk and
    `aft forget session` - land their rows in turn rather than one of them losing them to
    a busy database. A lock that never arrives, a commit the disk refuses, and a row the
    archive cannot hold are all EvidenceError naming the file, and the rolled-back attempt
    leaves no part of itself behind to be read as a whole chunk.
    """
    path = _db_path(config)
    conn = _connect(config)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise EvidenceError(f"cannot write the evidence archive at {path}: {exc}") from exc
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _release(config: Config, *, rewrite: bool) -> None:
    """Empty the write-ahead log after a write that removed rows, and rewrite the archive

    when told to. The committed pages move back into the archive file and the log is cut
    off, so the log keeps no page of the text that was just removed. ``rewrite`` vacuums
    as well, which is what clears the pages a delete freed inside the file itself;
    `forget` pays for that and `purge`, which runs on a timer, does not (DESIGN 12).
    Neither statement runs inside a transaction, so this follows the write that removed
    the rows rather than joining it.
    """
    with _reading(config) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if rewrite:
            conn.execute("VACUUM")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _connect(config: Config) -> sqlite3.Connection:
    """Open the archive, creating it on first use.

    Connections stay in autocommit, so the only transaction is the one `_writing` opens by
    hand. The journal is WAL, so `aft trace` reads while the digest writes; the sync mode
    is SQLite's default FULL, because a store that reported success and then lost its rows
    to a power cut leaves a digest whose quotes no longer verify. Unlike the queue, nothing
    sweeps the archive again to fill it in.

    secure_delete is on so the pages a delete frees are written over on their way to the
    free list rather than left holding the words they carried (DESIGN 12). A connection
    that only reads frees no pages, so it pays nothing for it.
    """
    path = _db_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = None
    try:
        conn = sqlite3.connect(path, isolation_level=None, timeout=BUSY_TIMEOUT_SECONDS)
        conn.execute("PRAGMA secure_delete = ON")
        if _journal_mode(conn) != WAL_MODE:
            _enter_wal(conn, path)
        conn.executescript(_SCHEMA)
    except sqlite3.Error as exc:
        # `_reading` and `_writing` never receive a connection that failed to open, so
        # this is the only place its handle can be given back.
        if conn is not None:
            conn.close()
        raise EvidenceError(f"cannot open the evidence archive at {path}: {exc}") from exc
    return conn


def _db_path(config: Config) -> Path:
    return config.home / EVIDENCE_NAME


def _journal_mode(conn: sqlite3.Connection) -> str:
    return conn.execute("PRAGMA journal_mode").fetchone()[0]


def _enter_wal(conn: sqlite3.Connection, path: Path) -> None:
    """Put the archive in WAL mode, on the connection that first touches the file.

    Changing the journal mode wants the file to itself and answers ``database is locked``
    at once when another process holds it, so the first two processes to touch a new
    archive cannot both set it on the first try; the one that loses is told, and its work
    is a rerun. The mode is re-read rather than trusted from the write, because a pragma
    that refuses by answering with the mode it kept - on a file system that cannot share
    memory, say - sets no error at all. WAL is what lets a reader go past a writer, so a
    file that will not do it says so rather than quietly archiving into another journal.
    """
    conn.execute("PRAGMA journal_mode = wal")
    mode = _journal_mode(conn)
    if mode != WAL_MODE:
        raise EvidenceError(f"{path} stays in {mode} mode, not {WAL_MODE}")
