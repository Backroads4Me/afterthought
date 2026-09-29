"""The search index: one FTS5 table per collection, kept in the afterthought home.

``aft search`` ranks what this module builds (DESIGN 10). The memory itself stays where it
is - the vault's markdown on disk, the filtered conversation in ``aft.evidence``'s archive -
and the index holds a derived, tokenized copy of what each collection is defined to hold.
That makes the index disposable: the failure table in DESIGN 14 answers a corrupt index with
``aft reindex``, which `reindex` here implements as a drop and a rebuild from those files.

One table per collection (DESIGN 10's collection table, BUILD-PLAN M3.1):

=========== ====================================================== ==========================
Table       Built from                                             A row is
=========== ====================================================== ==========================
``pages``   fact pages under ``projects/``, ``global/``,            one ``##`` section's live
            ``references/``                                         curated lines
``archive`` the pages under ``archive/``                            one ``##`` section's
                                                                   resting lines
``digests`` ``digests/<slug>/<yyyy>/<mm>/*.md``                     one heading section
``reports`` ``reports/**/*.md``                                     one heading section
``evidence``  one row of ``evidence.sqlite``                        one filtered entry
=========== ====================================================== ==========================

The library collection of DESIGN 11 is created by its own task (BUILD-PLAN M7) and not here.

A row is one heading's worth of text, and it carries its document path and its heading
together. BUILD-PLAN M3.1 identifies a row by "a heading or entry id", and a curated line has
no identifier of its own - the bracketed reference at the end of a line names the source entry
it rests on, not the line - so the section is what a row is about. DESIGN 11 says a search
result names "its document and section" and DESIGN 10 returns "the matching snippet" with the
section on it, so both halves travel on the row: ``path`` and ``heading``, joined in
``row_key``. DESIGN 10's "active curated lines" says what goes into a row rather than how big
a row is: a ``pages`` row holds that section's live lines and nothing else.

The heading is an indexed column beside the title and the description, so the page title, the
description and the section's own heading all outrank the line text: BUILD-PLAN M3.1's "BM25
weights title and description above body" holds with the heading on the near side of that
divide. Every row also carries path, project id, agent, session and time. Fields a collection
has no answer for are left empty rather than invented - a curated page is not one agent's
record, so its rows carry no agent or session, and an evidence entry sits under no heading.

Two keys keep the index in step with what it is built from (BUILD-PLAN M3.1: "updated
incrementally by file content hash and evidence row id"):

* **Files.** ``files`` records the SHA-256 of every file the index has read. A file whose
  hash is unchanged is not parsed, re-tokenized or touched again, so the content is the key
  and a new modification time on unchanged text changes nothing. A file that is gone has its
  rows removed: the files are the memory (DESIGN 14), so text from a deleted page has no
  memory left to be found, and `aft forget` in DESIGN 12 removes content "from everything
  afterthought controls".
* **Evidence.** A row is keyed by the archive's own row identity, its ``(session_id,
  entry_id)`` primary key, and records the archive rowid it was read from. Rows the index has
  never seen are read and indexed once; rows the archive no longer holds - purged past the
  retention window (DESIGN 8) or forgotten (DESIGN 12) - lose their index rows. The archive's
  text is zlib-compressed (``aft.evidence._pack``), so only the rows being read now are
  decompressed; the rest are compared on their primary key and their stored payload size.

A row whose text is rewritten in place under the same primary key is caught by that payload
size, which is why it is recorded. The one writer that rewrites archived text in place is
``aft forget pattern`` (BUILD-PLAN M2.8); a redaction that happened to leave the compressed
size identical would slip past the size check, and the exact answer for that case is
`reindex`, which rebuilds the table from the archive without trusting any key.

An update reports what it cannot read instead of indexing around it. ``aft inject`` skips an
unreadable page because a session must not fail over a broken one, and ``aft lint`` is where
broken pages get named; for the index, quietly leaving a page out would take its text out of
search with no record at all, which is the failure `aft.evidence` refuses for its rows.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from aft import lint, page
from aft.config import Config
from aft.evidence import EVIDENCE_NAME

INDEX_NAME = "index.sqlite"

# The index is per-host derived state, so it lives with the archive and the queue rather
# than in the vault (DESIGN 6: the home holds "state: evidence archive, search index, ...").
WAL_MODE = "wal"

# A reader of the index - `aft search` in front of a person - should not fail because a
# digest or a dream run is rebuilding it, so a lock is waited out (as in aft.evidence).
BUSY_TIMEOUT_SECONDS = 5.0

PAGES = "pages"
ARCHIVE = "archive"
DIGESTS = "digests"
EVIDENCE = "evidence"
REPORTS = "reports"

#: The collections this index maintains (DESIGN 10, BUILD-PLAN M3.1).
COLLECTIONS = (PAGES, ARCHIVE, DIGESTS, EVIDENCE, REPORTS)

# Which top-level vault directories feed each file collection. ``pages`` takes the
# directories whose pages hold curated fact lines, which is `aft.lint`'s list, so the index
# and the linter cannot disagree about what a curated page is. The archive, the digests and
# the reports are the directories DESIGN 6's vault tree files them in.
FILE_COLLECTIONS: dict[str, tuple[str, ...]] = {
    PAGES: lint.FACT_DIRS,
    ARCHIVE: ("archive",),
    DIGESTS: ("digests",),
    REPORTS: ("reports",),
}

# Which fact states a collection admits, read off the state table in DESIGN 6 and the
# collection table in DESIGN 10. ``pages`` holds "active curated lines": Conflict and
# Needs you are an active line with a callout under it, so their lines are active lines, and
# `aft.inject` carries the same three for the same reason. A candidate is its own state and
# DESIGN 10 files candidate facts under Digests, where the digest that raised it is searchable.
# Superseded, withdrawn and stale lines are marked "Injected? No" and are not active memory;
# they rest in ``archive``, which DESIGN 10 searches only when ``--archive`` asks for it.
FACT_STATES: dict[str, tuple[str, ...]] = {
    PAGES: (page.ACTIVE, page.CONFLICT, page.NEEDS_YOU),
    ARCHIVE: (page.SUPERSEDED, page.WITHDRAWN, page.STALE),
}

# BM25 weights (BUILD-PLAN M3.1: "BM25 weights title and description above body", with the
# section's own heading belonging with the columns above that divide). The plan fixes the
# order only; these ratios make a match in the title, the description or the heading beat the
# same word in body text by a clear margin. Unindexed columns cannot match, so they get 0.
TITLE_WEIGHT = 10.0
DESCRIPTION_WEIGHT = 5.0
HEADING_WEIGHT = 5.0
BODY_WEIGHT = 1.0

#: The columns a query can match, in the order ``bm25`` takes its weights.
INDEXED_COLUMNS = ("title", "description", "heading", "body")

#: The columns that only identify a row: path, project id, agent, session, time and a heading
#: or entry id (BUILD-PLAN M3.1), stored so a hit reads back without opening its file again.
IDENTITY_COLUMNS = (
    "path",
    "project_id",
    "agent",
    "session_id",
    "entry_id",
    "time",
    "row_key",
    "entry_rowid",
    "text_bytes",
)

#: Every column of every collection, in table order.
COLUMNS = INDEXED_COLUMNS + IDENTITY_COLUMNS

WEIGHTS = (TITLE_WEIGHT, DESCRIPTION_WEIGHT, HEADING_WEIGHT, BODY_WEIGHT) + (0.0,) * len(
    IDENTITY_COLUMNS
)

FILES_TABLE = "files"

# SQLite takes a table name in the statement text and never as a parameter, so every
# statement that names one is written once here from `COLLECTIONS` and `FILES_TABLE` - this
# module's own constants, never a caller's string.
_COLUMNS_SQL = ", ".join(COLUMNS)
_PLACEHOLDERS = ", ".join("?" * len(COLUMNS))
_INSERT = {
    name: f"INSERT INTO {name} ({_COLUMNS_SQL}) VALUES ({_PLACEHOLDERS})" for name in COLLECTIONS
}
_DELETE_BY_PATH = {name: f"DELETE FROM {name} WHERE path = ?" for name in COLLECTIONS}
_DELETE_BY_ENTRY = {
    name: f"DELETE FROM {name} WHERE session_id = ? AND entry_id = ?" for name in COLLECTIONS
}
_SELECT_HASHES = f"SELECT path, digest FROM {FILES_TABLE}"
_RECORD_HASH = (
    f"INSERT INTO {FILES_TABLE} (path, digest) VALUES (?, ?)"
    f" ON CONFLICT (path) DO UPDATE SET digest = excluded.digest"
)
_DROP_HASH = f"DELETE FROM {FILES_TABLE} WHERE path = ?"

# The schema: a table of file hashes and one FTS5 table per collection, holding the indexed
# columns and storing the identifiers beside them.
_CREATE_FILES = (
    f"CREATE TABLE IF NOT EXISTS {FILES_TABLE} (path TEXT PRIMARY KEY, digest TEXT NOT NULL)"
)
_CREATE_COLLECTION = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS {collection} USING fts5("
    + ", ".join(INDEXED_COLUMNS)
    + ", "
    + ", ".join(f"{column} UNINDEXED" for column in IDENTITY_COLUMNS)
    + ")"
)
_SCHEMA_TABLES: tuple[tuple[str, str], ...] = (
    (FILES_TABLE, _CREATE_FILES),
    *((name, _CREATE_COLLECTION.format(collection=name)) for name in COLLECTIONS),
)
# A rebuild drops every table before it recreates them, so nothing an earlier update got wrong
# survives into it (DESIGN 14); an ordinary update only creates what is missing.
_DROP_STATEMENTS: tuple[tuple[str, str], ...] = tuple(
    (name, f"DROP TABLE IF EXISTS {name}") for name in (*COLLECTIONS, FILES_TABLE)
)

_PROSE_HEADING = re.compile("^(#+)[ \t]+(.*[^ \t])?[ \t]*$")
_DIGEST_NAME = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})-(?P<agent>[^-]+)-(?P<session>.+)$")

# The resource form DESIGN 6 gives a session's sources, used as the path of an evidence row:
# an evidence hit is a session's text, and this is how a page already names one.
SESSION_RESOURCE = "aft://session/"


class SearchIndexError(Exception):
    """An index operation that cannot be honoured: this SQLite has no FTS5, a file or the
    archive cannot be read, a page cannot be parsed into rows, or the index file will not
    open, will not enter WAL, or will not take its write lock."""


@dataclass(frozen=True)
class Row:
    """One row of one collection, in `COLUMNS` order."""

    title: str = ""
    description: str = ""
    heading: str = ""
    body: str = ""
    path: str = ""
    project_id: str | None = None
    agent: str | None = None
    session_id: str | None = None
    entry_id: str | None = None
    time: str = ""
    row_key: str = ""
    entry_rowid: int | None = None
    text_bytes: int | None = None

    def values(self) -> tuple:
        """The row in `COLUMNS` order, so the columns and the record cannot drift apart."""
        return tuple(getattr(self, column) for column in COLUMNS)


@dataclass(frozen=True)
class _File:
    """One vault file as the index reads it: which collection it feeds, its content, the
    hash that decides whether it needs re-reading, and the moment it last changed."""

    collection: str
    text: str
    digest: str
    time: str


def update(config: Config) -> None:
    """Bring the index in step with the vault and the archive, leaving untouched rows alone.

    This is the incremental path BUILD-PLAN M3.1 names: a file is rebuilt only when its
    content hash moved, an evidence row is read only when the index has not seen it, and
    whatever has disappeared from either loses its rows. Nothing here waits for a model or a
    git commit; it reads the files as they are.
    """
    with _writing(config) as conn:
        _sync_files(conn, config)
        _sync_evidence(conn, config)


def reindex(config: Config) -> None:
    """Drop the index and build it again from the files and the archive (DESIGN 14).

    Every table goes, shadow tables and all, before anything is rebuilt - so a row an
    earlier update got wrong cannot survive into the result. The rebuild then runs the same
    sync as an incremental update over an empty index, which is why a rebuild and an index
    kept up to date come out with the same rows.
    """
    with _writing(config) as conn:
        _create(conn, fresh=True)
        _sync_files(conn, config)
        _sync_evidence(conn, config)


def rank_expression(collection: str) -> str:
    """The ``bm25()`` expression that ranks one collection: title, description and heading above

    Whoever queries the index orders on this (`aft search`, BUILD-PLAN M3.2); the weights go
    in column order, so the weighting is the index's to state and not each caller's to guess.
    """
    _require_collection(collection)
    return f"bm25({collection}, {', '.join(f'{w:g}' for w in WEIGHTS)})"


def _require_collection(collection: str) -> None:
    if collection not in COLLECTIONS:
        raise SearchIndexError(
            f"{collection!r} is not a collection; expected one of {', '.join(COLLECTIONS)}"
        )


# Reading the vault


def _sync_files(conn: sqlite3.Connection, config: Config) -> None:
    """Index every file whose content the index has not already read, and unindex the ones
    that are gone."""
    read = dict(conn.execute(_SELECT_HASHES))
    found = _vault_files(config)
    for rel in sorted(set(read) - set(found)):
        _drop_path(conn, rel)
    for rel, current in found.items():
        if read.get(rel) == current.digest:
            continue
        _drop_path(conn, rel)
        for row in _file_rows(rel, current):
            _insert(conn, current.collection, row)
        conn.execute(_RECORD_HASH, (rel, current.digest))


def _vault_files(config: Config) -> dict[str, _File]:
    """Every vault file the collections are built from, by vault-relative path.

    Only the directories of `FILE_COLLECTIONS` are walked, and `aft.lint`'s skipped
    directories are skipped here too, so the index reads the same pages the linter checks and
    nothing from ``.git``, ``.obsidian`` or the library's files.
    """
    found: dict[str, _File] = {}
    for collection, directories in FILE_COLLECTIONS.items():
        for name in directories:
            root = config.vault / name
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*.md")):
                rel = path.relative_to(config.vault)
                if lint.SKIP_DIRS.intersection(rel.parts):
                    continue
                found[rel.as_posix()] = _read_file(collection, path, rel)
    return found


def _read_file(collection: str, path: Path, rel: Path) -> _File:
    """One file as the index reads it, reported on when it cannot be read."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SearchIndexError(f"cannot read {path} to index it: {exc}") from exc
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SearchIndexError(
            f"{path} is not UTF-8 text, so its words cannot be indexed: {exc}"
        ) from exc
    moment = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
    return _File(
        collection=collection,
        text=text,
        digest=hashlib.sha256(data).hexdigest(),
        time=moment,
    )


def _drop_path(conn: sqlite3.Connection, rel: str) -> None:
    """Remove a file's rows and its hash record, wherever in the index its path lands."""
    collection = _collection_of(rel)
    if collection is not None:
        conn.execute(_DELETE_BY_PATH[collection], (rel,))
    conn.execute(_DROP_HASH, (rel,))


def _collection_of(rel: str) -> str | None:
    """The collection a vault-relative path belongs to, by the directory it sits in."""
    top = Path(rel).parts[0] if Path(rel).parts else ""
    for collection, directories in FILE_COLLECTIONS.items():
        if top in directories:
            return collection
    return None


def _file_rows(rel: str, current: _File) -> list[Row]:
    """The rows one file contributes to its collection.

    A fact collection (``pages``, ``archive``) takes the lines its own state admits, read
    through ``aft.page``, grouped under the section each line sits in: the parser owns what a
    line says and what state it is in, and a line it refused holds no fact at all, so a
    broken line reaches the index no more than it reaches `aft.inject`. A prose collection
    (``digests``, ``reports``) is split under its headings, since what it holds is written
    prose rather than fact lines.
    """
    frontmatter, body, _, error = page.split_frontmatter(current.text)
    if error:
        raise SearchIndexError(f"cannot index {rel}: {error}")
    title = _one_line((frontmatter or {}).get("title")) or Path(rel).stem
    description = _one_line((frontmatter or {}).get("description"))
    project_id = _project_id(frontmatter)
    agent, session_id = _session_of(rel) if current.collection == DIGESTS else (None, None)
    admitted = FACT_STATES.get(current.collection)
    if admitted is None:
        chunks = _prose_chunks(body)
    else:
        chunks = _fact_chunks(page.parse(current.text, Path(rel)), admitted)
    return [
        Row(
            title=title,
            description=description,
            body=text,
            path=rel,
            project_id=project_id,
            agent=agent,
            session_id=session_id,
            time=current.time,
            heading=heading,
            row_key=f"{rel}#{heading}",
        )
        for heading, text in chunks
        if text
    ]


def _fact_chunks(parsed: page.Page, admitted: tuple[str, ...]) -> list[tuple[str, str]]:
    """The admitted lines of each section, as the section's indexed text.

    A line is indexed as what it says: the source tag, the date and the footnotes are the
    page's grammar rather than its content, and `aft.inject` leaves them out of the block it
    builds for the same reason. A callout is a note about a line rather than a line of its
    own, so its text is not indexed either (DESIGN 6, DESIGN 10).
    """
    grouped: dict[str, list[str]] = {}
    for fact in parsed.facts:
        if fact.state in admitted:
            grouped.setdefault(fact.section, []).append(fact.statement)
    return [(section, "\n".join(statements)) for section, statements in grouped.items()]


def _prose_chunks(body: str) -> list[tuple[str, str]]:
    """A page's prose under each heading, with the text before the first heading under none."""
    chunks: list[tuple[str, list[str]]] = []
    heading = ""
    lines: list[str] = []
    for line in body.split("\n"):
        match = _PROSE_HEADING.match(line.rstrip())
        if match:
            chunks.append((heading, lines))
            heading, lines = match.group(2) or "", []
        else:
            lines.append(line)
    chunks.append((heading, lines))
    return [(heading, "\n".join(lines).strip()) for heading, lines in chunks]


def _one_line(value: object) -> str:
    """A frontmatter value as one line: a block scalar keeps its words and loses its breaks,
    so no value reaches a row as a second line (as in `aft.inject`)."""
    return " ".join(value.split()) if isinstance(value, str) else ""


def _project_id(frontmatter: dict | None) -> str | None:
    """The project a page names in ``aft.project`` (DESIGN 6), which is what a search is
    filtered by. A global page names none, which is what makes it every project's memory."""
    aft = (frontmatter or {}).get("aft")
    project = aft.get("project") if isinstance(aft, dict) else None
    return project if isinstance(project, str) and project else None


def _session_of(rel: str) -> tuple[str | None, str | None]:
    """The agent and session a digest belongs to, from the name BUILD-PLAN M2.6 files it
    under: ``digests/<slug>/<yyyy>/<mm>/<date>-<agent>-<session>.md``. The session id may
    hold hyphens, so only the first two fields of the name are taken. A name that does not
    read that way leaves both empty; the path still says where the text came from."""
    match = _DIGEST_NAME.match(Path(rel).stem)
    if match is None:
        return None, None
    return match.group("agent"), match.group("session")


# Reading the evidence archive


def _sync_evidence(conn: sqlite3.Connection, config: Config) -> None:
    """Index the archive rows this index has not seen, and unindex the ones it dropped.

    The row identity is the archive's ``(session_id, entry_id)`` primary key, which the
    archive itself keeps unique across a rerun (``aft.evidence.store`` replaces a row rather
    than adding a copy). The archive rowid rides along, because BUILD-PLAN M3.1 keys the
    update on the evidence row, and is not trusted as the key itself: once the highest rowid
    goes, SQLite hands that number to the next row inserted.

    A row is read again when its rowid moved or its stored text changed size, which is how a
    rewrite of archived text in place - `aft forget pattern`'s redaction (BUILD-PLAN M2.8) -
    reaches the index. Whatever is read is written once, under its own key.
    """
    seen = {
        (session_id, entry_id): (rowid, size)
        for session_id, entry_id, rowid, size in conn.execute(
            "SELECT session_id, entry_id, entry_rowid, text_bytes FROM evidence"
        )
    }
    archive = config.home / EVIDENCE_NAME
    if not archive.exists():
        _unindex(conn, EVIDENCE, sorted(seen))
        return
    with _reading_archive(archive) as source:
        live = {
            (session_id, entry_id): (rowid, size)
            for rowid, session_id, entry_id, size in source.execute(
                "SELECT rowid, session_id, entry_id, length(text) FROM evidence"
            )
        }
        _unindex(conn, EVIDENCE, sorted(set(seen) - set(live)))
        for key in sorted(key for key in live if live[key] != seen.get(key)):
            row = _evidence_row(source, key, *live[key])
            if row is None:
                continue  # gone between the key scan and the read; a later update sees it
            _unindex(conn, EVIDENCE, [key])
            _insert(conn, EVIDENCE, row)


def _unindex(conn: sqlite3.Connection, collection: str, keys: list[tuple[str, str]]) -> None:
    """Drop indexed rows by the archive key that identifies each one."""
    conn.executemany(_DELETE_BY_ENTRY[collection], keys)


def _evidence_row(
    source: sqlite3.Connection, key: tuple[str, str], rowid: int, size: int
) -> Row | None:
    """One archive row as an index row, or None when the archive no longer holds it."""
    session_id, entry_id = key
    found = source.execute(
        "SELECT agent, project_id, time, text FROM evidence WHERE session_id = ? AND entry_id = ?",
        (session_id, entry_id),
    ).fetchone()
    if found is None:
        return None
    agent, project_id, moment, packed = found
    try:
        text = zlib.decompress(bytes(packed)).decode("utf-8")
    except (zlib.error, UnicodeDecodeError) as exc:
        raise SearchIndexError(
            f"the archived text of entry {entry_id} of session {session_id}"
            " does not decompress as it was written, so it cannot be indexed"
        ) from exc
    return Row(
        body=text,
        path=f"{SESSION_RESOURCE}{agent}/{session_id}",
        project_id=project_id,
        agent=agent,
        session_id=session_id,
        entry_id=entry_id,
        time=moment,
        row_key=f"{session_id}#{entry_id}",
        entry_rowid=rowid,
        text_bytes=size,
    )


@contextmanager
def _reading_archive(archive: Path) -> Iterator[sqlite3.Connection]:
    """A read-only connection to ``aft.evidence``'s archive.

    Read-only by construction: the archive belongs to `aft.evidence`, and the index never
    writes it. Its text arrives as it is stored, zlib-compressed UTF-8, because BUILD-PLAN
    M3.1 updates the index per evidence row and the archive's own reader returns a whole
    session at a time."""
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(
            archive.as_uri() + "?mode=ro", uri=True, timeout=BUSY_TIMEOUT_SECONDS
        )
    except sqlite3.Error as exc:
        raise SearchIndexError(
            f"cannot open the evidence archive at {archive} to index it: {exc}"
        ) from exc
    try:
        yield conn
    except sqlite3.Error as exc:
        raise SearchIndexError(f"cannot read the evidence archive at {archive}: {exc}") from exc
    finally:
        conn.close()


# The index file itself


def _create(conn: sqlite3.Connection, *, fresh: bool = False) -> None:
    """Create the schema, dropping every table first when the index is being rebuilt.

    Creating a table is where a missing FTS5 makes itself known: the error names the module
    that is absent rather than leaving an index that quietly holds nothing."""
    statements = (*_DROP_STATEMENTS, *_SCHEMA_TABLES) if fresh else _SCHEMA_TABLES
    for label, statement in statements:
        try:
            conn.execute(statement)
        except sqlite3.Error as exc:
            if "no such module" in str(exc):
                raise SearchIndexError(
                    f"{INDEX_NAME} cannot be built: this SQLite has no FTS5 module ({exc})"
                ) from exc
            raise SearchIndexError(
                f"cannot create the {label} table in {INDEX_NAME}: {exc}"
            ) from exc


def _insert(conn: sqlite3.Connection, collection: str, row: Row) -> None:
    _require_collection(collection)
    conn.execute(_INSERT[collection], row.values())


@contextmanager
def _writing(config: Config) -> Iterator[sqlite3.Connection]:
    """One connection held for one transaction, its write lock taken before the first row.

    Two writers - a search that finds the index behind, and a run rebuilding it - land their
    rows in turn rather than one of them failing on a busy database. An update that cannot
    finish leaves no part of itself behind to be read as a whole index."""
    path = _db_path(config)
    conn = _connect(config)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise SearchIndexError(f"cannot write the index at {path}: {exc}") from exc
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _connect(config: Config) -> sqlite3.Connection:
    """Open the index, creating it on first use.

    Connections stay in autocommit so the only transaction is the one `_writing` opens by
    hand, and the journal is WAL so a search reads while a run writes. Unlike the evidence
    archive this file is disposable - `reindex` rebuilds it from the vault and the archive -
    so it is opened without the archive's secure delete."""
    path = _db_path(config)
    conn: sqlite3.Connection | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None, timeout=BUSY_TIMEOUT_SECONDS)
        if _journal_mode(conn) != WAL_MODE:
            _enter_wal(conn, path)
        _create(conn)
    except BaseException as exc:
        if conn is not None:
            conn.close()
        if isinstance(exc, (sqlite3.Error, OSError)):
            raise SearchIndexError(f"cannot open the index at {path}: {exc}") from exc
        raise
    return conn


def _journal_mode(conn: sqlite3.Connection) -> str:
    return conn.execute("PRAGMA journal_mode").fetchone()[0]


def _enter_wal(conn: sqlite3.Connection, path: Path) -> None:
    """Put the index in WAL mode, on the connection that first touches the file.

    The mode is re-read rather than trusted from the write, because a pragma that refuses by
    answering with the mode it kept sets no error at all, and WAL is what lets a search read
    while a run rebuilds (as in `aft.evidence`)."""
    conn.execute("PRAGMA journal_mode = wal")
    mode = _journal_mode(conn)
    if mode != WAL_MODE:
        raise SearchIndexError(f"{path} stays in {mode} mode, not {WAL_MODE}")


def _db_path(config: Config) -> Path:
    return config.home / INDEX_NAME
