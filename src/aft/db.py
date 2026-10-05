"""Shared SQLite plumbing: connection defaults, WAL mode, busy timeouts, and write locks.

Used by aft.evidence, aft.index, and aft.queue to avoid duplicating SQLite pragmas,
WAL mode negotiation, and transaction write lock serialization across stores.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

WAL_MODE: str = "wal"
BUSY_TIMEOUT_SECONDS: float = 5.0
BUSY_TIMEOUT_MS: int = 5_000


def journal_mode(conn: sqlite3.Connection) -> str:
    """Return the active journal mode of the connection."""
    return conn.execute("PRAGMA journal_mode").fetchone()[0]


def enter_wal(
    conn: sqlite3.Connection,
    *,
    timeout_seconds: float = BUSY_TIMEOUT_SECONDS,
    path: Path | None = None,
    error_factory: Callable[[str], Exception] | None = None,
    get_journal_mode: Callable[[sqlite3.Connection], str] = journal_mode,
) -> None:
    """Put the database into WAL mode, waiting out any concurrent writer.

    Changing the journal mode wants the database to itself and reports 'database is locked'
    at once when a writer holds it. Each connection asks only while the mode is still the
    default one, retrying until timeout_seconds elapses so concurrent initializers do not fail.
    """
    deadline = time.monotonic() + timeout_seconds
    while get_journal_mode(conn) != WAL_MODE and time.monotonic() < deadline:
        try:
            conn.execute(f"PRAGMA journal_mode = {WAL_MODE}")
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.002)
    mode = get_journal_mode(conn)
    if mode != WAL_MODE:
        target = f"{path} " if path else ""
        msg = f"{target}stays in {mode} mode, not {WAL_MODE}"
        if error_factory:
            raise error_factory(msg)
        raise sqlite3.OperationalError(msg)


def connect(
    path: Path | str,
    *,
    timeout: float = BUSY_TIMEOUT_SECONDS,
    isolation_level: str | None = None,
) -> sqlite3.Connection:
    """Open a SQLite connection with autocommit mode by default."""
    return sqlite3.connect(path, isolation_level=isolation_level, timeout=timeout)


@contextmanager
def write_transaction(
    conn: sqlite3.Connection,
    *,
    path: Path | None = None,
    error_factory: Callable[[str, sqlite3.Error], Exception] | None = None,
) -> Iterator[sqlite3.Connection]:
    """Execute BEGIN IMMEDIATE to take the write lock before yields, committing on return.

    BEGIN IMMEDIATE takes the write lock before the first row is written, so concurrent
    writers take their turns rather than colliding mid-transaction. On error, the
    transaction rolls back; if error_factory is provided, sqlite3.Error is converted.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        if error_factory:
            msg = f"cannot write database at {path}: {exc}" if path else str(exc)
            raise error_factory(msg, exc) from exc
        raise
    except BaseException:
        conn.rollback()
        raise


def wal_checkpoint_truncate(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """Checkpoint and truncate the write-ahead log.

    Returns (busy, log_frames, checkpointed_frames).
    """
    row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    return int(row[0]), int(row[1]), int(row[2])
