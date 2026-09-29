"""The evidence archive: rows in, the same text back out, and when each row goes.

Entries are built by hand and the archive is the one under a temporary ``AFT_HOME``, so
no test here reads a transcript, touches ``~/.afterthought``, or depends on the machine's
clock or time zone. Every claim about what a call wrote is proved by reading the
resulting database back: through `aft.evidence` where that is what a caller would use,
and through ``sqlite3`` directly where the claim is about the file itself.
"""

from __future__ import annotations

import sqlite3
import zlib
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aft import config as configmod
from aft import evidence
from aft.readers import Entry

# The moment every test calls now. Retention is measured back from it, so nothing here
# reads the clock, and an entry's age is the same on a host in any time zone.
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

RETENTION = 90

# The values no reader would produce, held as Any because they are here to be refused.
NOT_A_TIME: Any = None
TIME_AS_TEXT: Any = "2026-03-01T12:00:00+00:00"
NOT_A_TEXT: Any = None
NOT_A_ROLE: Any = None


def at(days: int = 0, seconds: int = 0) -> datetime:
    """An instant ``days`` and ``seconds`` before ``NOW``."""
    return NOW - timedelta(days=days, seconds=seconds)


def entry(
    entry_id: str = "e1",
    time: datetime = NOW,
    role: str = "user",
    text: str = "the plan is zlib",
    is_error: bool = False,
) -> Entry:
    return Entry(entry_id, time, role, text, is_error)


def db(config: configmod.Config) -> Path:
    """The archive file the module writes."""
    return config.home / evidence.EVIDENCE_NAME


def row_count(config: configmod.Config) -> int:
    """Rows in the archive, counted off the file rather than as they went in."""
    with closing(sqlite3.connect(db(config))) as conn:
        return conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]


def stored(config: configmod.Config, session_id: str) -> list[tuple]:
    """One session's rows as the file holds them, the text still compressed."""
    with closing(sqlite3.connect(db(config))) as conn:
        return conn.execute(
            "SELECT entry_id, agent, project_id, time, role, text, is_error"
            " FROM evidence WHERE session_id = ? ORDER BY rowid",
            (session_id,),
        ).fetchall()


def spoil(config: configmod.Config, entry_id: str, blob: bytes) -> None:
    """Replace one row's stored text, the way a damaged file would."""
    with closing(sqlite3.connect(db(config))) as conn:
        conn.execute("UPDATE evidence SET text = ? WHERE entry_id = ?", (blob, entry_id))
        conn.commit()


# The ways a person says "keep everything" to a setting that has no value for it: windows
# so wide that counting them back from now leaves the calendar behind.
UNMEASURABLE_WINDOWS: list[Any] = [999_999_999, 10**18]

# The ways a project is not a project id: a whole Path or a number, which the database
# cannot bind, and a blank, which is a second spelling of the no project that is None.
NOT_A_PROJECT_ID: list[Any] = ["", "   ", Path("/srv/work/api"), 7]

# Text whose stored form is looked for in the archive's own bytes: `forget` has to leave
# none of it behind, in the file or in whatever log of it is still open.
FORGOTTEN_TEXT = (
    "the cedar ledger whispers ninety-seven willow under the seventh bridge, "
    "and the ledger keeps its own counsel about the vault"
)


def archive_files(config: configmod.Config) -> list[Path]:
    """Every file the archive is made of: the database and the log it is keeping."""
    return [f for f in db(config).parent.iterdir() if f.is_file()]


@contextmanager
def held_write_lock(config: configmod.Config) -> Iterator[None]:
    """A second writer that keeps the archive to itself, the way a person with the file
    open in the sqlite3 CLI, or an `aft` still running, does."""
    holder = sqlite3.connect(db(config), isolation_level=None, timeout=0.05)
    holder.execute("BEGIN IMMEDIATE")
    try:
        yield
    finally:
        holder.rollback()
        holder.close()


@contextmanager
def held_read_snapshot(config: configmod.Config) -> Iterator[None]:
    """A reader that keeps its snapshot of the archive open, the way `aft trace` in one
    process does while `aft forget session` runs in another."""
    reader = sqlite3.connect(db(config), isolation_level=None, timeout=0.05)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM evidence").fetchone()
    try:
        yield
    finally:
        reader.rollback()
        reader.close()


def connection_is_open(conn: sqlite3.Connection) -> bool:
    """Whether the connection still holds its handle: a closed one refuses to work."""
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return False
    return True


def spy_on_opens(monkeypatch) -> list[sqlite3.Connection]:
    """Every connection sqlite3 hands out from here on, so a test can see whether the
    ones that failed to finish opening were given back."""
    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def spy(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", spy)
    return opened


@pytest.fixture
def make_config(tmp_path, monkeypatch):
    """A Config whose home is a temporary directory, with the retention window as asked."""

    def make(retention_days: int = RETENTION) -> configmod.Config:
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        (home / "config.toml").write_text(
            f'vault = "{tmp_path / "vault"}"\nhost = "testhost"\n'
            "[evidence]\n"
            f"retention_days = {retention_days}\n"
        )
        monkeypatch.setenv(configmod.HOME_ENV, str(home))
        return configmod.load()

    return make


@pytest.fixture
def cfg(make_config) -> configmod.Config:
    return make_config()


# Round trip: what came out of the filter is what goes back out.


def test_entries_are_read_back_as_they_were_stored(cfg):
    entries = [
        entry("e1", at(days=2, seconds=30), "user", "ship the archive first"),
        entry("e2", at(days=2), "assistant", "the queue lands before it", True),
        entry("e3", at(days=1), "tool", "FAILED tests/test_evidence.py", True),
    ]
    evidence.store(cfg, "claude", "s1", "prj_one", entries)
    assert evidence.entries(cfg, "s1") == entries


def test_the_row_names_its_session_agent_and_project(cfg):
    evidence.store(cfg, "pi", "s1", "prj_one", [entry("e1")])
    entry_id, agent, project_id, time_text, role, blob, is_error = stored(cfg, "s1")[0]
    assert (entry_id, agent, project_id, role, is_error) == ("e1", "pi", "prj_one", "user", 0)
    assert zlib.decompress(blob).decode("utf-8") == "the plan is zlib"
    assert datetime.fromisoformat(time_text) == NOW


def test_a_session_that_resolved_to_no_project_is_still_archived(cfg):
    evidence.store(cfg, "pi", "s1", None, [entry("e1")])
    assert stored(cfg, "s1")[0][2] is None
    assert evidence.entries(cfg, "s1") == [entry("e1")]


@pytest.mark.parametrize(
    "text",
    [
        "prefacio ✓ 日本語 e não só ASCII",
        "a NUL\x00byte and another\x00",
        "tabs\tand\r\nwindows line endings\x0bkeep\x0ctheir shape",
        "",
        "é" * 4000,
    ],
    ids=["non-ascii", "nul-bytes", "odd-whitespace", "empty", "long-non-ascii"],
)
def test_text_that_is_not_plain_ascii_comes_back_unchanged(cfg, text):
    evidence.store(cfg, "pi", "s1", None, [entry("e1", text=text)])
    assert evidence.entries(cfg, "s1")[0].text == text


def test_text_is_still_compressed_in_the_file(cfg):
    text = "zlib " * 200
    evidence.store(cfg, "claude", "s1", None, [entry("e1", text=text)])
    blob = stored(cfg, "s1")[0][5]
    assert zlib.decompress(blob).decode("utf-8") == text
    assert len(blob) < len(text.encode())
    assert text.encode() not in db(cfg).read_bytes()


def test_a_timestamp_with_no_offset_is_filed_as_utc(cfg):
    """Read as local time instead, the same transcript would be a different age on two
    hosts, and `purge` would keep or drop entries by where the machine happens to be."""
    evidence.store(cfg, "pi", "s1", None, [entry("e1", time=datetime(2026, 1, 1, 0, 0))])
    assert evidence.entries(cfg, "s1")[0].time == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


# Idempotence, proved by the rows that are there afterwards.


def test_storing_the_same_entries_twice_leaves_one_row_each(cfg):
    entries = [entry("e1", at(days=3)), entry("e2", at(days=2)), entry("e3", at(days=1))]
    evidence.store(cfg, "claude", "s1", "prj_one", entries)
    before = stored(cfg, "s1")
    evidence.store(cfg, "claude", "s1", "prj_one", entries)

    assert row_count(cfg) == 3
    assert stored(cfg, "s1") == before
    assert evidence.entries(cfg, "s1") == entries


def test_re_storing_a_chunk_that_overlaps_one_archived_adds_nothing(cfg):
    """The digest reads on from a watermark and a chunk boundary can land mid-session, so
    the overlap arrives as the same entries again."""
    evidence.store(cfg, "claude", "s1", None, [entry("e1"), entry("e2")])
    evidence.store(cfg, "claude", "s1", None, [entry("e2"), entry("e3")])

    assert [row[0] for row in stored(cfg, "s1")] == ["e1", "e2", "e3"]
    assert row_count(cfg) == 3


def test_a_session_stored_out_of_order_reads_back_in_time_order(cfg):
    """A lost watermark reads a whole session again, so entries older than rows already in
    the archive arrive last. `aft trace` prints the entries around a quote and
    `aft redigest` hands the list to the model, so an order that followed write history
    would show one session two ways depending on how the reads happened to fall."""
    evidence.store(
        cfg,
        "claude",
        "s1",
        None,
        [entry("first-read", at(days=1)), entry("second-read", at(days=2))],
    )
    evidence.store(cfg, "claude", "s1", None, [entry("missed-first", at(days=9))])

    assert [e.id for e in evidence.entries(cfg, "s1")] == [
        "missed-first",
        "second-read",
        "first-read",
    ]
    assert row_count(cfg) == 3


def test_entries_sharing_one_timestamp_keep_the_order_they_were_written(cfg):
    """A Claude message and the tool results split out of it carry one timestamp, so only
    the order they were stored in says which came first."""
    said = at(days=3)
    evidence.store(
        cfg,
        "claude",
        "s1",
        None,
        [
            entry("user-message", said, "user", "run the tests"),
            entry("tool-result", said, "tool", "12 passed"),
            entry("answer", said, "assistant", "twelve passed, then"),
            entry("next-day", at(days=1), "user", "and tomorrow?"),
        ],
    )

    assert [e.id for e in evidence.entries(cfg, "s1")] == [
        "user-message",
        "tool-result",
        "answer",
        "next-day",
    ]


def test_an_entry_stored_again_takes_the_row_it_already_had(cfg):
    evidence.store(cfg, "claude", "s1", None, [entry("e1", text="kept the first time")])
    evidence.store(cfg, "claude", "s1", None, [entry("e1", text="the same entry, re-read")])

    assert row_count(cfg) == 1
    assert evidence.entries(cfg, "s1") == [entry("e1", text="the same entry, re-read")]


def test_one_entry_id_in_two_sessions_is_two_rows(cfg):
    evidence.store(cfg, "claude", "s1", None, [entry("e1", text="claude said this")])
    evidence.store(cfg, "pi", "s2", None, [entry("e1", text="pi said this")])

    assert row_count(cfg) == 2
    assert evidence.entries(cfg, "s1") == [entry("e1", text="claude said this")]
    assert evidence.entries(cfg, "s2") == [entry("e1", text="pi said this")]


def test_storing_nothing_writes_no_archive(cfg):
    """A chunk the filter threw all of is not a reason to create an archive."""
    evidence.store(cfg, "claude", "s1", None, [])
    assert not db(cfg).exists()


# Retention, tested on both sides of the boundary.


def test_purge_keeps_an_entry_at_the_limit_and_drops_the_one_past_it(cfg):
    limit = at(days=RETENTION)
    evidence.store(
        cfg,
        "claude",
        "s1",
        None,
        [
            entry("at-the-limit", limit),
            entry("one-second-past", limit - timedelta(seconds=1)),
            entry("one-second-short", limit + timedelta(seconds=1)),
            entry("today", NOW),
        ],
    )

    assert evidence.purge(cfg, NOW) == 1
    assert sorted(e.id for e in evidence.entries(cfg, "s1")) == [
        "at-the-limit",
        "one-second-short",
        "today",
    ]
    assert [row[0] for row in stored(cfg, "s1")] == [
        "at-the-limit",
        "one-second-short",
        "today",
    ]


def test_purge_counts_what_it_removed_across_sessions(make_config):
    cfg = make_config(retention_days=7)
    evidence.store(
        cfg,
        "claude",
        "s1",
        None,
        [entry("e1", at(days=1)), entry("e2", at(days=8)), entry("e3", at(days=30))],
    )
    evidence.store(cfg, "pi", "s2", None, [entry("e4", at(days=9))])

    assert evidence.purge(cfg, NOW) == 3
    assert [e.id for e in evidence.entries(cfg, "s1")] == ["e1"]
    assert evidence.entries(cfg, "s2") == []
    assert row_count(cfg) == 1


def test_purge_before_there_is_an_archive_removes_nothing_and_writes_no_file(cfg):
    assert evidence.purge(cfg, NOW) == 0
    assert not db(cfg).exists()


def test_purge_refuses_a_window_that_is_not_a_window_and_changes_nothing(make_config):
    """A negative window would drop entries dated after ``now``, which is no reading of
    retention; the archive keeps its rows and the run is told to fix the setting."""
    cfg = make_config(retention_days=-1)
    evidence.store(cfg, "claude", "s1", None, [entry("e1", at(days=400))])
    before = stored(cfg, "s1")

    with pytest.raises(evidence.EvidenceError, match=r"retention_days = -1"):
        evidence.purge(cfg, NOW)

    assert stored(cfg, "s1") == before


@pytest.mark.parametrize("window", UNMEASURABLE_WINDOWS, ids=["widest", "past-a-timedelta"])
def test_purge_refuses_a_window_that_runs_off_the_calendar_and_changes_nothing(make_config, window):
    """A huge retention_days is how someone writes "keep everything"; the setting has no"
    such value, and a window that cannot be counted back is the bad setting it is, told"
    on the way the negative one is."""
    cfg = make_config(retention_days=window)
    evidence.store(cfg, "claude", "s1", None, [entry("e1", at(days=400))])
    before = stored(cfg, "s1")

    with pytest.raises(evidence.EvidenceError, match=r"retention_days"):
        evidence.purge(cfg, NOW)

    assert stored(cfg, "s1") == before


def test_a_window_of_zero_keeps_nothing_older_than_now(make_config):
    cfg = make_config(retention_days=0)
    evidence.store(
        cfg,
        "claude",
        "s1",
        None,
        [entry("a-moment-ago", NOW - timedelta(microseconds=1)), entry("now", NOW)],
    )

    assert evidence.purge(cfg, NOW) == 1
    assert [e.id for e in evidence.entries(cfg, "s1")] == ["now"]


# Quote verification: spacing is the only thing set aside.

WRAPPED = (
    "We agreed the archive is zlib-compressed,\n"
    "  and that the filter   caps\n"
    "a message at 1,500 characters."
)

# The same words, spaced and wrapped differently.
MATCHING_QUOTES = [
    "zlib-compressed",
    "the filter caps a message at 1,500 characters",
    "We   agreed\tthe archive\n is zlib-compressed",
    "  the archive is zlib-compressed, and that the filter caps  ",
    "compressed,\n  and that",
]

# One letter, one case, or one punctuation mark away, and a permissive matcher would let a
# fact stand on words nobody said.
NON_MATCHING_QUOTES = [
    "zlib-compresst",
    "the filter caps a message at 1,500 charater",
    "ZLIB-COMPRESSED",
    "The Filter caps a message",
    "the filter caps a message at 1500 characters",
    "the filter caps a message characters",
    "caps a message at 1,500 characters!",
    "we agreed the archive is zlib compressed",
]


def test_a_quote_that_differs_only_in_spacing_and_wrapping_matches(cfg):
    evidence.store(cfg, "claude", "s1", None, [entry("e1", text=WRAPPED)])
    for quote in MATCHING_QUOTES:
        assert evidence.find_quote(cfg, "s1", "e1", quote) is True, quote


def test_a_quote_that_differs_in_anything_else_does_not_match(cfg):
    evidence.store(cfg, "claude", "s1", None, [entry("e1", text=WRAPPED)])
    for quote in NON_MATCHING_QUOTES:
        assert evidence.find_quote(cfg, "s1", "e1", quote) is False, quote


def test_a_quote_of_nothing_but_whitespace_verifies_nothing(cfg):
    """A quote of spaces is not words copied from the transcript, so it cannot stand as
    one even against an entry that is nothing but spaces, where `in` would say yes."""
    evidence.store(cfg, "claude", "s1", None, [entry("e1", text="   \n\t ")])
    for quote in ["", " ", "  \n\t ", "\n"]:
        assert evidence.find_quote(cfg, "s1", "e1", quote) is False, repr(quote)


def test_a_quote_is_found_in_the_entry_it_cites_only(cfg):
    evidence.store(
        cfg,
        "claude",
        "s1",
        None,
        [entry("e1", text="the archive is zlib-compressed"), entry("e2", text="not this one")],
    )
    assert evidence.find_quote(cfg, "s1", "e1", "zlib-compressed") is True
    assert evidence.find_quote(cfg, "s1", "e2", "zlib-compressed") is False


def test_a_quote_of_nothing_archived_is_no_match(cfg):
    """The digest's rule is to drop a fact whose quote is not in the entry it cites, so a
    missing session, entry or archive answers no rather than raising."""
    assert evidence.find_quote(cfg, "s1", "e1", "anything") is False
    evidence.store(cfg, "claude", "s1", None, [entry("e1", text="the archive is zlib")])
    assert evidence.find_quote(cfg, "s1", "no-such-entry", "zlib") is False
    assert evidence.find_quote(cfg, "another-session", "e1", "zlib") is False


def test_a_quote_of_text_with_a_nul_byte_is_found(cfg):
    evidence.store(cfg, "pi", "s1", None, [entry("e1", text="keep\x00this  one  intact")])
    assert evidence.find_quote(cfg, "s1", "e1", "keep\x00this one intact") is True


# Forgetting one session, and leaving the others alone.


def test_forget_removes_a_session_and_leaves_the_others_intact(cfg):
    evidence.store(cfg, "claude", "gone", "prj_one", [entry("e1"), entry("e2")])
    kept = [entry("k1"), entry("k2")]
    evidence.store(cfg, "pi", "kept", "prj_two", kept)

    evidence.forget(cfg, "gone")

    assert evidence.entries(cfg, "gone") == []
    assert evidence.entries(cfg, "kept") == kept
    assert [row[0] for row in stored(cfg, "kept")] == ["k1", "k2"]
    assert row_count(cfg) == 2


def test_forgetting_a_session_leaves_its_words_in_none_of_the_files(cfg):
    """DESIGN 12 promises the content is gone from everything afterthought controls, so the
    proof is the read-back an intruder would do: the raw bytes of the archive and of any
    write-ahead log still beside it, searched for the stored form of the text. That the
    session reads back empty would only prove the query."""
    buried = [
        entry(f"f{i}", text=f"filler that names no ledger at all, number {i}") for i in range(200)
    ]
    kept = [entry("k1", text="the kept session says nothing of ledgers")]
    evidence.store(cfg, "claude", "gone", None, [entry("e1", text=FORGOTTEN_TEXT), *buried])
    evidence.store(cfg, "pi", "kept", None, kept)
    written = zlib.compress(FORGOTTEN_TEXT.encode("utf-8"))
    size_before = db(cfg).stat().st_size

    assert any(written in f.read_bytes() for f in archive_files(cfg)), (
        "the text has to be in the archive for its absence afterwards to mean anything"
    )

    evidence.forget(cfg, "gone")

    assert evidence.entries(cfg, "gone") == []
    assert evidence.entries(cfg, "kept") == kept
    for archived in archive_files(cfg):
        assert written not in archived.read_bytes(), archived.name
    assert db(cfg).stat().st_size < size_before, "the rewrite is what clears freed pages"


def test_a_log_another_reader_holds_open_is_not_reported_as_forgotten(cfg):
    """A reader going past a writer is what WAL is for, and a log a reader is attached to
    cannot be drained: the checkpoint answers busy in a row and sets no error. The removed
    text is still in it then, so the command says so instead of returning a success the
    bytes contradict, and once the reader is gone the log drains and the words go."""
    written = zlib.compress(FORGOTTEN_TEXT.encode("utf-8"))
    evidence.store(cfg, "claude", "gone", None, [entry("e1", text=FORGOTTEN_TEXT)])
    evidence.store(cfg, "pi", "kept", None, [entry("k1", text="the kept session")])

    with held_read_snapshot(cfg):
        with pytest.raises(evidence.EvidenceError) as excinfo:
            evidence.forget(cfg, "gone")
        assert str(db(cfg)) in str(excinfo.value)
        assert any(written in f.read_bytes() for f in archive_files(cfg)), (
            "the error is about this residue, so the residue has to be there"
        )

    assert evidence.entries(cfg, "gone") == []
    for archived in archive_files(cfg):
        assert written not in archived.read_bytes(), archived.name


def test_forget_of_a_session_that_is_not_there_changes_nothing(cfg):
    """A typo is no reason to rewrite the archive: with nothing to delete, `forget` leaves
    the rows and every byte of the file where they were, the way it only rewrites the
    archive when a delete actually released something."""
    filler = [
        entry(f"f{i}", text=f"filler that names no ledger at all, number {i}") for i in range(200)
    ]
    evidence.store(cfg, "claude", "s1", None, filler)
    before = (row_count(cfg), stored(cfg, "s1"), db(cfg).read_bytes())

    evidence.forget(cfg, "never-captured")

    assert (row_count(cfg), stored(cfg, "s1"), db(cfg).read_bytes()) == before


def test_forget_and_reads_before_any_archive_write_no_file(cfg):
    evidence.forget(cfg, "s1")
    assert evidence.entries(cfg, "s1") == []
    assert not db(cfg).exists()


# A call that cannot do its work leaves the archive as it was.

BAD_ENTRIES = [
    pytest.param(entry("bad", text=NOT_A_TEXT), id="text-that-is-not-text"),
    pytest.param(entry("bad", time=NOT_A_TIME), id="no-timestamp"),
    pytest.param(entry("bad", time=TIME_AS_TEXT), id="timestamp-as-text"),
    pytest.param(entry("bad", role=NOT_A_ROLE), id="no-role"),
    pytest.param(entry("", text="an entry with no id"), id="no-entry-id"),
]


@pytest.mark.parametrize("bad", BAD_ENTRIES)
def test_an_unarchivable_entry_writes_nothing_and_loses_nothing(cfg, bad):
    """One call is one transaction, so the rows that went in before the unarchivable one
    are rolled back with it rather than left behind as half a chunk."""
    good = [entry("e1"), entry("e2")]
    evidence.store(cfg, "claude", "s1", None, good)
    before = (row_count(cfg), stored(cfg, "s1"))

    with pytest.raises(evidence.EvidenceError):
        evidence.store(cfg, "claude", "s1", None, [*good, bad])

    assert (row_count(cfg), stored(cfg, "s1")) == before


def test_a_chunk_that_fails_halfway_through_leaves_no_part_of_itself(cfg):
    def chunk():
        yield entry("e1")
        yield entry("e2")
        raise RuntimeError("the transcript went unreadable mid-chunk")

    evidence.store(cfg, "claude", "s1", None, [entry("kept", text="an earlier chunk")])
    before = (row_count(cfg), stored(cfg, "s1"))

    with pytest.raises(RuntimeError, match="unreadable"):
        evidence.store(cfg, "claude", "s1", None, chunk())

    assert (row_count(cfg), stored(cfg, "s1")) == before


def test_an_id_that_is_not_there_is_refused_before_anything_is_written(cfg):
    with pytest.raises(evidence.EvidenceError, match="session id"):
        evidence.store(cfg, "claude", "   ", None, [entry("e1")])
    with pytest.raises(evidence.EvidenceError, match="agent"):
        evidence.store(cfg, "", "s1", None, [entry("e1")])
    with pytest.raises(evidence.EvidenceError, match="session id"):
        evidence.entries(cfg, "")
    with pytest.raises(evidence.EvidenceError, match="entry id"):
        evidence.find_quote(cfg, "s1", "", "the plan is zlib")
    with pytest.raises(evidence.EvidenceError, match="session id"):
        evidence.forget(cfg, "")
    assert not db(cfg).exists()


def test_a_locked_archive_is_reported_and_changes_nothing(cfg, monkeypatch):
    """The archive is a plain file a person opens with the sqlite3 CLI, and M2.8 re-stores
    the whole of it, so a writer still holding the write lock is a busy archive rather than
    a crash. Each command fails as EvidenceError naming the file, and a reader gets past
    the writer, which is what the write-ahead log is for."""
    evidence.store(cfg, "claude", "s1", None, [entry("e1")])
    before = (row_count(cfg), stored(cfg, "s1"))
    monkeypatch.setattr(evidence, "BUSY_TIMEOUT_SECONDS", 0.05)

    with held_write_lock(cfg):
        assert evidence.entries(cfg, "s1") == [entry("e1")]
        for write in (
            lambda: evidence.store(cfg, "pi", "s2", None, [entry("e2")]),
            lambda: evidence.purge(cfg, NOW),
            lambda: evidence.forget(cfg, "s1"),
        ):
            with pytest.raises(evidence.EvidenceError) as excinfo:
                write()
            assert not isinstance(excinfo.value, sqlite3.Error)
            assert "locked" in str(excinfo.value)
            assert str(db(cfg)) in str(excinfo.value)

    assert (row_count(cfg), stored(cfg, "s1")) == before


def test_an_archive_that_will_not_give_its_text_back_is_reported_naming_it(cfg):
    """An archive that cannot be read - unreadable, or damaged the way a half-written file
    is - is the module's own error naming the file, not a database error escaping to
    whatever command was reading it."""
    evidence.store(cfg, "claude", "s1", None, [entry("e1")])
    db(cfg).chmod(0o000)
    try:
        for read in (
            lambda: evidence.entries(cfg, "s1"),
            lambda: evidence.find_quote(cfg, "s1", "e1", "the plan is zlib"),
        ):
            with pytest.raises(evidence.EvidenceError) as excinfo:
                read()
            assert not isinstance(excinfo.value, sqlite3.Error)
            assert str(db(cfg)) in str(excinfo.value)
    finally:
        db(cfg).chmod(0o644)


def test_an_archive_that_refuses_the_write_ahead_log_leaves_no_handle(cfg, monkeypatch):
    """WAL is what lets a reader go past a writer, so a file that will not take it is
    reported rather than quietly used as another kind of journal. The connection that got
    that far is one no caller ever received, so the failed open gives its handle back."""
    with closing(sqlite3.connect(db(cfg), isolation_level=None)) as foreign:
        foreign.execute("CREATE TABLE not_the_archive(x)")
        foreign.commit()
    db(cfg).chmod(0o444)
    opened = spy_on_opens(monkeypatch)

    try:
        with pytest.raises(evidence.EvidenceError) as excinfo:
            evidence.entries(cfg, "s1")
    finally:
        db(cfg).chmod(0o644)

    assert str(db(cfg)) in str(excinfo.value)
    assert opened
    assert not any(connection_is_open(conn) for conn in opened)


def test_an_archive_whose_mode_never_arrives_leaves_no_handle(cfg, monkeypatch):
    """A file system that cannot share memory answers the mode pragma with the mode it kept
    and sets no error, which is a refusal the module reports itself - and that refusal is
    not a database error, so the open has to give its handle back on that path too."""
    evidence.store(cfg, "claude", "s1", None, [entry("e1")])
    monkeypatch.setattr(evidence, "_journal_mode", lambda conn: "delete")
    opened = spy_on_opens(monkeypatch)

    with pytest.raises(evidence.EvidenceError, match="stays in delete mode"):
        evidence.entries(cfg, "s1")

    assert opened
    assert not any(connection_is_open(conn) for conn in opened)


@pytest.mark.parametrize(
    "project_id",
    NOT_A_PROJECT_ID,
    ids=["empty", "whitespace", "path", "number"],
)
def test_a_project_id_that_is_no_project_is_refused(cfg, project_id):
    """None is the only spelling of a session that resolved to no project (DESIGN 7), and
    a value the archive cannot bind would abort a whole chunk on a database error: both
    are refused as the bad input they are, before anything is written."""
    with pytest.raises(evidence.EvidenceError, match="project id"):
        evidence.store(cfg, "pi", "s1", project_id, [entry("e1")])

    assert not db(cfg).exists()


def test_a_row_that_will_not_decompress_is_reported_not_skipped(cfg):
    """Silently dropping it would take text out of `aft trace` and `aft search` with no one
    told: an unreadable row is neither an absent row nor an expired one."""
    evidence.store(cfg, "claude", "s1", None, [entry("e1"), entry("e2")])
    spoil(cfg, "e2", b"this was never compressed")

    with pytest.raises(evidence.EvidenceError, match="e2"):
        evidence.entries(cfg, "s1")
    with pytest.raises(evidence.EvidenceError, match="e2"):
        evidence.find_quote(cfg, "s1", "e2", "the plan is zlib")


def test_reads_of_a_session_that_was_never_archived_are_empty(cfg):
    evidence.store(cfg, "claude", "s1", None, [entry("e1")])
    assert evidence.entries(cfg, "another-session") == []
    assert stored(cfg, "another-session") == []
