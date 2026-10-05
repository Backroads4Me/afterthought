"""Unit tests for shared SQLite plumbing in aft.db."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from aft import db


def test_db_connect_defaults_and_journal_mode(tmp_path: Path) -> None:
    db_file = tmp_path / "test.sqlite"
    conn = db.connect(db_file)
    try:
        assert conn.isolation_level is None
        # Default journal mode on fresh db is delete
        assert db.journal_mode(conn) in ("delete", "wal")
    finally:
        conn.close()


def test_db_enter_wal_sets_mode(tmp_path: Path) -> None:
    db_file = tmp_path / "test.sqlite"
    conn = db.connect(db_file)
    try:
        conn.execute("PRAGMA journal_mode = delete")
        assert db.journal_mode(conn) == "delete"
        db.enter_wal(conn, timeout_seconds=1.0, path=db_file)
        assert db.journal_mode(conn) == db.WAL_MODE
    finally:
        conn.close()


def test_db_enter_wal_refusal_raises_error() -> None:
    # In-memory SQLite databases do not support WAL mode and stay in memory mode
    conn = db.connect(":memory:")

    class CustomError(Exception):
        pass

    try:
        with pytest.raises(CustomError, match="stays in memory mode, not wal"):
            db.enter_wal(
                conn,
                timeout_seconds=0.01,
                error_factory=CustomError,
            )
    finally:
        conn.close()


def test_db_write_transaction_commits(tmp_path: Path) -> None:
    db_file = tmp_path / "test.sqlite"
    conn = db.connect(db_file)
    try:
        conn.execute("CREATE TABLE items (id TEXT PRIMARY KEY, val TEXT)")
        with db.write_transaction(conn, path=db_file):
            conn.execute("INSERT INTO items VALUES ('k1', 'v1')")

        row = conn.execute("SELECT val FROM items WHERE id = 'k1'").fetchone()
        assert row == ("v1",)
    finally:
        conn.close()


def test_db_write_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    db_file = tmp_path / "test.sqlite"
    conn = db.connect(db_file)
    try:
        conn.execute("CREATE TABLE items (id TEXT PRIMARY KEY, val TEXT)")
        with pytest.raises(RuntimeError):
            with db.write_transaction(conn, path=db_file):
                conn.execute("INSERT INTO items VALUES ('k1', 'v1')")
                raise RuntimeError("something went wrong")

        row = conn.execute("SELECT val FROM items WHERE id = 'k1'").fetchone()
        assert row is None
    finally:
        conn.close()


def test_db_write_transaction_error_factory(tmp_path: Path) -> None:
    db_file = tmp_path / "test.sqlite"
    conn = db.connect(db_file)

    class CustomWriteError(Exception):
        pass

    try:
        conn.execute("CREATE TABLE items (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO items VALUES ('k1')")

        with pytest.raises(CustomWriteError, match="cannot write"):
            with db.write_transaction(
                conn,
                path=db_file,
                error_factory=lambda msg, exc: CustomWriteError(f"cannot write: {exc}"),
            ):
                conn.execute("INSERT INTO items VALUES ('k1')")  # Primary key collision
    finally:
        conn.close()


def test_db_write_transaction_serializes_concurrent_writers(tmp_path: Path) -> None:
    db_file = tmp_path / "test.sqlite"
    conn1 = db.connect(db_file, timeout=0.01)
    conn2 = db.connect(db_file, timeout=0.01)
    try:
        db.enter_wal(conn1, path=db_file)
        conn1.execute("CREATE TABLE t (x INT)")

        with db.write_transaction(conn1, path=db_file):
            conn1.execute("INSERT INTO t VALUES (1)")
            # conn2 attempts to acquire write lock while conn1 holds it
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                with db.write_transaction(conn2, path=db_file):
                    conn2.execute("INSERT INTO t VALUES (2)")

        # After conn1 finishes, conn2 can write
        with db.write_transaction(conn2, path=db_file):
            conn2.execute("INSERT INTO t VALUES (2)")

        rows = conn1.execute("SELECT x FROM t ORDER BY x").fetchall()
        assert rows == [(1,), (2,)]
    finally:
        conn1.close()
        conn2.close()


def test_db_wal_checkpoint_truncate(tmp_path: Path) -> None:
    db_file = tmp_path / "test.sqlite"
    conn = db.connect(db_file)
    try:
        db.enter_wal(conn, path=db_file)
        conn.execute("CREATE TABLE t (x TEXT)")
        conn.execute("INSERT INTO t VALUES ('hello')")

        busy, frames, moved = db.wal_checkpoint_truncate(conn)
        assert busy == 0
        assert frames >= 0
        assert moved >= 0
    finally:
        conn.close()
