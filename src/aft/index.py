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
``digests`` ``digests/<slug>/<yyyy>/<mm>/*.md``                     one heading section or
                                                                    its frontmatter handoff
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
record, so its rows carry no agent or session, and an evidence entry sits under no heading. A
row's project id comes from the page's own ``aft.project``, the projects a reference page
lists, or the directory the file is filed under; a row with none is a page that is every
project's memory (DESIGN 6), not a page whose project went missing.

Two keys keep the index in step with what it is built from (BUILD-PLAN M3.1: "updated
incrementally by file content hash and evidence row id"):

* **Files.** ``files`` records the SHA-256 of every file the index has read, and ``origins``
  records the bytes of the project registry those rows were filed against. A file whose hash
  is unchanged is not parsed, re-tokenized or touched again, so the content is the key and a
  new modification time on unchanged text changes nothing. What the file's bytes do not answer
  is which project the row belongs to: the registry said that for every page filed under a
  slug, and a registry that has since moved re-derives every file row rather than leaving
  rows attributing memory to a project that no longer claims them. A file that is gone has
  its rows removed: the files are the memory (DESIGN 14), so text from a deleted page has no
  memory left to be found, and `aft forget` in DESIGN 12 removes content "from everything
  afterthought controls".
* **Evidence.** A row is keyed by the archive's ``(session_id, entry_id)`` primary key, which
  ``aft.evidence.store`` keeps unique across a rerun, and records the SHA-256 of the stored
  payload beside it. Rows the index has not seen are read once; rows the archive no longer
  holds - purged past the retention window (DESIGN 8) or forgotten (DESIGN 12) - lose their
  index rows. The archive's text is zlib-compressed (``aft.evidence._pack``), so every
  payload is digested as it is read and only the rows that moved are decompressed.

A row is read again when its digest moved or when any field the index row copies from the
archive row - its agent, its project and its time - moved with it. The one writer that
rewrites archived text in place is ``aft forget pattern`` (BUILD-PLAN M2.8), whose redaction
has to reach the index to leave no unredacted words behind (DESIGN 12), and an unregistered
session's project moves under the index when its directory is registered.

One damaged file does not stop a run. A page that cannot be read, is not UTF-8, or has
frontmatter that will not parse is left out of the build, named by its path and its reason in
the report `update` and `reindex` return and `aft reindex` prints, and no hash is recorded for
it, so the next update reads it again once it is repaired. ``aft inject`` skips a broken page
for the same reason and ``aft lint`` is where every broken page gets named. Only the index
file's own failure stops a run, because that is the one failure `aft reindex` exists to answer
(DESIGN 14).

Whatever leaves the index leaves by name: a run removes nothing it never recorded, and every
removal names the one row it means by the path or archive key it was stored under. FTS5 keeps
those columns beside the row unindexed, so a delete over a collection reads every row the
collection holds however it filters; holding to recorded keys is what makes that read cost what
a run left behind rather than what the index held, and it is why a rebuild removes nothing.

Beyond those keys the file states which build wrote it. What a row *is* - its columns, the
states a collection admits, the project a page's rows belong to - is decided here rather than
in the vault, so rows written under other rules would answer a query looking exactly as
trustworthy as fresh ones: a build that meets another's rows refuses and names `aft reindex` rather
than adding to them, only a rebuild - which drops them all first - meets them at all, and a
file holding no rows at all has nothing for that claim to protect and is written.

A row's text is not the only thing a row is built from, so text alone does not say whether a
row is current. A digest keeps its handoff note in frontmatter rather than in a body section,
and it is indexed there as a section of its own, because DESIGN 10 files handoff notes under
``digests``; a project's registry can move under a page that has not changed; and an archived
row can be re-stored with a new project while its text stays word for word the same. Each of
those says the row is stale on its own terms.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from aft import inject, lint, page
from aft.config import Config
from aft.evidence import EVIDENCE_NAME
from aft.registry import Registry, RegistryError

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
LIBRARY = "library"

#: The collections this index maintains (DESIGN 10, BUILD-PLAN M3.1, M7.3).
COLLECTIONS = (PAGES, ARCHIVE, DIGESTS, EVIDENCE, REPORTS, LIBRARY)

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

# The directories DESIGN 6 files one project's own text under, where the slug is the second
# path component: ``projects/<slug>/``, ``digests/<slug>/`` and ``archive/<slug>.md``. The
# rest name no project: ``global/`` is every project's memory, ``references/`` lists the
# projects a page covers, and a report day spans them all.
PROJECT_DIRS = ("projects", DIGESTS, ARCHIVE)

# Which fact states a collection admits, read off the state table in DESIGN 6 and the
# collection table in DESIGN 10. ``pages`` holds "active curated lines", which are the lines
# `aft.inject` injects, so the index and the block a session starts with carry the same lines
# by construction. A candidate is its own state and DESIGN 10 files candidate facts under
# Digests, where the digest that raised it is searchable. Superseded, withdrawn and stale
# lines are marked "Injected? No" and are not active memory; they rest in ``archive``, which
# DESIGN 10 searches only when ``--archive`` asks for it.
FACT_STATES: dict[str, tuple[str, ...]] = {
    PAGES: inject.INJECTED,
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

#: The identifiers a row carries: path, project id, agent, session, time and a heading or
#: entry id (BUILD-PLAN M3.1), stored so a hit reads back without opening its file again,
#: plus the digest of the stored payload of an evidence row.
IDENTITY_COLUMNS = (
    "path",
    "project_id",
    "agent",
    "session_id",
    "entry_id",
    "time",
    "row_key",
    "entry_hash",
)

#: Every column of every collection, in table order.
COLUMNS = INDEXED_COLUMNS + IDENTITY_COLUMNS

WEIGHTS = (TITLE_WEIGHT, DESCRIPTION_WEIGHT, HEADING_WEIGHT, BODY_WEIGHT) + (0.0,) * len(
    IDENTITY_COLUMNS
)

FILES_TABLE = "files"

# What an index row's project id was derived from, recorded beside the file hashes rather
# than among them: the registry is no collection's source, so it is never walked, and keeping
# it in a table of its own leaves every row of `files` a page that can be looked up by path.
ORIGINS_TABLE = "origins"

#: The `origins` record naming the project registry, the one non-file input rows are built
#: from: it answered which project a page filed under a slug belongs to.
PROJECTS_ORIGIN = "registry"
#: The build that wrote these rows, recorded in the file beside the hashes that say which files
#: they came from. What a row *is* - its columns, the states its collection admits, the project
#: it carries - is decided by this module rather than by any file, so rows written under another
#: build answer a query looking exactly as trustworthy as fresh ones, and no query can tell.
#: Bump the stamp whenever that changes; the repair for meeting another build's rows is
#: DESIGN 14's `aft reindex` (`_require_current_build`).
BUILD_STAMP = "rows-1"
BUILD_TABLE = "build"

#: One row, keyed so a build replaces its own stamp instead of leaving a second behind: a file
#: is written by one build at a time.
_BUILD_KEY = "rows"
_CREATE_BUILD = (
    f"CREATE TABLE IF NOT EXISTS {BUILD_TABLE} (name TEXT PRIMARY KEY, stamp TEXT NOT NULL)"
)
_SELECT_BUILD = f"SELECT stamp FROM {BUILD_TABLE} WHERE name = ?"
_RECORD_BUILD = (
    f"INSERT INTO {BUILD_TABLE} (name, stamp) VALUES (?, ?)"
    f" ON CONFLICT (name) DO UPDATE SET stamp = excluded.stamp"
)

#: Whether the index holds anything a build wrote. A run records a hash for every file it read
#: and the state of every archive row it read, in the same transaction as the rows themselves,
#: so with neither record the file holds no row whose meaning a stamp would vouch for.
_HOLDS_ROWS = f"SELECT EXISTS (SELECT 1 FROM {FILES_TABLE}) OR EXISTS (SELECT 1 FROM {EVIDENCE})"

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
_DROP_HASH = f"DELETE FROM {FILES_TABLE} WHERE path = ?"
# What the index recorded about each evidence row: the key it was stored under, the digest of
# its stored payload, and the fields the row copied out of the archive beside it. A row is read
# again when any of it moved.
_EVIDENCE_STATE = (
    f"SELECT session_id, entry_id, entry_hash, agent, project_id, time FROM {EVIDENCE}"
)
_SELECT_HASHES = f"SELECT path, digest FROM {FILES_TABLE}"
_RECORD_HASH = (
    f"INSERT INTO {FILES_TABLE} (path, digest) VALUES (?, ?)"
    f" ON CONFLICT (path) DO UPDATE SET digest = excluded.digest"
)
_SELECT_ORIGIN = f"SELECT digest FROM {ORIGINS_TABLE} WHERE input = ?"
_RECORD_ORIGIN = (
    f"INSERT INTO {ORIGINS_TABLE} (input, digest) VALUES (?, ?)"
    f" ON CONFLICT (input) DO UPDATE SET digest = excluded.digest"
)
# What a moved project registry invalidates: every row built from a file, and the hashes that
# said those files had been read. The evidence rows keep theirs - a project id reaches an
# evidence row from the archive row beside it, not from the registry.
_RESET_FILE_ROWS: tuple[str, ...] = (
    *(f"DELETE FROM {name}" for name in FILE_COLLECTIONS),
    f"DELETE FROM {LIBRARY}",
    f"DELETE FROM {FILES_TABLE}",
)

# The schema: a table of file hashes, a table of the other inputs rows were derived from, and
# one FTS5 table per collection, holding the indexed columns and storing the identifiers
# beside them.
_CREATE_FILES = (
    f"CREATE TABLE IF NOT EXISTS {FILES_TABLE} (path TEXT PRIMARY KEY, digest TEXT NOT NULL)"
)
_CREATE_ORIGINS = (
    f"CREATE TABLE IF NOT EXISTS {ORIGINS_TABLE} (input TEXT PRIMARY KEY, digest TEXT NOT NULL)"
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
    (ORIGINS_TABLE, _CREATE_ORIGINS),
    (BUILD_TABLE, _CREATE_BUILD),
    *((name, _CREATE_COLLECTION.format(collection=name)) for name in COLLECTIONS),
)
# A rebuild drops every table before it recreates them, so nothing an earlier update got wrong
# survives into it (DESIGN 14); an ordinary update only creates what is missing.
_DROP_STATEMENTS: tuple[tuple[str, str], ...] = tuple(
    (name, f"DROP TABLE IF EXISTS {name}")
    for name in (*COLLECTIONS, FILES_TABLE, ORIGINS_TABLE, BUILD_TABLE)
)

_PROSE_HEADING = re.compile("^(#+)[ \t]+(.*[^ \t])?[ \t]*$")

# Opens and closes a fenced block: up to three spaces of indentation, then three of the same
# character, and whatever follows the opening marks names the language. Only a line of those
# marks alone - same character, at least as many of them, nothing after - closes the block, and
# a page ending inside a block ends there. A `#` line inside a block is the comment of a quoted
# command rather than a heading of the page (`_prose_chunks`).
_FENCE = re.compile("^[ ]{0,3}(`{3,}|~{3,})(.*)$")
_DIGEST_NAME = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})-(?P<agent>[^-]+)-(?P<session>.+)$")

# The resource form DESIGN 6 gives a session's sources, used as the path of an evidence row:
# an evidence hit is a session's text, and this is how a page already names one.
SESSION_RESOURCE = "aft://session/"

#: The heading a digest's handoff note is indexed under. The note itself lives in the digest's
#: frontmatter (DESIGN 8) and `aft.inject` renders it for a new session; this is the name a
#: search hit names it by, and DESIGN 10 files handoff notes under ``digests``.
HANDOFF = "Handoff"


class SearchIndexError(Exception):
    """An index operation that cannot be honoured: this SQLite has no FTS5, the project
    registry or the evidence archive cannot be read, an archived entry's text does not
    decompress as it was written, or the index file will not open, will not enter WAL, or
    will not take its write lock."""


class _Unreadable(Exception):
    """One file the index cannot turn into rows: it will not read, is not UTF-8, or its
    frontmatter will not parse. Its path and this reason go in the report the run returns,
    the rest of the run goes on, and no hash is recorded for it, so a later update that
    finds it repaired indexes it after all."""


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
    entry_hash: str | None = None

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


def update(config: Config) -> list[str]:
    """Bring the index in step with the vault and the archive, leaving untouched rows alone.

    This is the incremental path BUILD-PLAN M3.1 names: a file is rebuilt only when its
    content hash moved or when the project registry the rows were filed against moved,
    an evidence row is read only when the index has not seen its stored bytes or the archive
    row beside them changed what the index row copies, and whatever has disappeared from
    either loses its rows. Nothing here waits for a model or a git commit; it reads the files
    as they are.

    What it returns is the report: one line per file it could not read or parse, naming the
    file and the reason. None of those recorded a hash, so the next update reads them again.

    The file holding the index is asked which build wrote its rows before this one adds to them;
    a file holding no rows at all has never been built and is written now
    (`_require_current_build`).
    """
    with _writing(config) as conn:
        registry = _project_registry(config)
        report = _sync_files(conn, config, registry)
        _sync_evidence(conn, config)
        report.extend(_sync_library(conn, config))
        _record_build(conn)
        return report


def reindex(config: Config) -> list[str]:
    """Drop the index and build it again from the files and the archive (DESIGN 14).

    Every table goes, shadow tables and all, before anything is rebuilt - so a row an
    earlier update got wrong cannot survive into the result, and so this is the one operation
    that may meet an index written by another build (`_require_current_build`). The rebuild then
    runs the same sync as an incremental update over an empty index, which is why a rebuild and
    an index kept up to date come out with the same rows and the same report.
    """
    with _writing(config, rebuild=True) as conn:
        _create(conn, fresh=True)
        report = _sync_files(conn, config, _project_registry(config))
        _sync_evidence(conn, config)
        report.extend(_sync_library(conn, config))
        _record_build(conn)
        return report


def rank_expression(collection: str) -> str:
    """The ``bm25()`` expression that ranks one collection: title, description and heading above
    the body text they were pulled from, and the stored identifiers not at all.

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


def _sync_files(conn: sqlite3.Connection, config: Config, projects: _Projects) -> list[str]:
    """Index every file whose content or project the index has not already read, and unindex
    the ones that are gone.

    Returns the report: one line per file this could not index, naming the file and the
    reason. A file that is unreadable, not UTF-8, or has frontmatter that will not parse
    loses its rows and its hash record, exactly as `reindex` would leave it, so its words
    are not searched out from an older version of itself and a later update reads it again.

    A moved registry asks for a full re-derivation rather than a per-file one, because it was
    the registry and not the page that answered which project most of these rows belong to:
    the bytes of a page say nothing about a slug the registry has since moved on from, so no
    file hash can say which rows went stale. The evidence rows are not touched: an evidence
    row takes its project from the archive row beside it, which the index compares directly.
    """
    recorded = dict(conn.execute(_SELECT_HASHES))
    found, report = _vault_files(config)
    filed_by = conn.execute(_SELECT_ORIGIN, (PROJECTS_ORIGIN,)).fetchone()
    if filed_by is None or filed_by[0] != projects.digest:
        _reset_files(conn)
        rebuild = sorted(found)
    else:
        rebuild = sorted(
            (set(recorded) - set(found))
            | {rel for rel, current in found.items() if recorded.get(rel) != current.digest}
        )
        _drop_paths(conn, rebuild)
    for rel in rebuild:
        current = found.get(rel)
        if current is None:
            continue  # gone from the vault: the drop above was the whole job
        try:
            rows = _file_rows(rel, current, projects.registry)
        except _Unreadable as exc:
            report.append(f"{rel}: {exc}")
            continue  # its rows and hash went with the drop, so a repaired page reads again
        for row in rows:
            _insert(conn, current.collection, row)
        conn.execute(_RECORD_HASH, (rel, current.digest))
    conn.execute(_RECORD_ORIGIN, (PROJECTS_ORIGIN, projects.digest))
    return report


def _vault_files(config: Config) -> tuple[dict[str, _File], list[str]]:
    """Every vault file the collections are built from, by vault-relative path, and the
    report lines for the ones that could not be read at all.

    Only the directories of `FILE_COLLECTIONS` are walked, and `aft.lint`'s skipped
    directories are skipped here too, so the index reads the same pages the linter checks and
    nothing from ``.git``, ``.obsidian`` or the library's files.
    """
    found: dict[str, _File] = {}
    report: list[str] = []
    for collection, directories in FILE_COLLECTIONS.items():
        for name in directories:
            root = config.vault / name
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*.md")):
                rel = path.relative_to(config.vault)
                if lint.SKIP_DIRS.intersection(rel.parts[:-1]):
                    continue
                try:
                    found[rel.as_posix()] = _read_file(collection, path)
                except _Unreadable as exc:
                    report.append(f"{rel.as_posix()}: {exc}")
    return found, report


def _read_file(collection: str, path: Path) -> _File:
    """One file as the index reads it.

    A file that will not read, cannot be stat'd, or is not UTF-8 raises `_Unreadable`, which
    is reported while the rest of the run goes on - a dangling symlink or a page caught
    halfway through a save is no reason for the index to refuse every other page.
    """
    try:
        data = path.read_bytes()
        moment = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
    except OSError as exc:
        raise _Unreadable(f"cannot be read to index it: {exc}") from exc
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _Unreadable(f"is not UTF-8 text, so its words cannot be indexed: {exc}") from exc
    return _File(
        collection=collection,
        text=text,
        digest=hashlib.sha256(data).hexdigest(),
        time=moment,
    )


def _drop_paths(conn: sqlite3.Connection, rels: list[str]) -> None:
    """Stop holding the rows and the hash record of every one of these vault paths.

    One statement per path, naming that path and nothing else: FTS5 stores a row's path beside
    it unindexed, so a delete over a collection reads every row it holds whatever it filters
    on, which is why a run may only ever ask it for rows it knows are there. The rows of a path
    this does not name keep their place."""
    by_collection: dict[str, list[str]] = {}
    for rel in rels:
        collection = _collection_of(rel)
        if collection is not None:
            by_collection.setdefault(collection, []).append(rel)
    for collection, paths in by_collection.items():
        conn.executemany(_DELETE_BY_PATH[collection], [(path,) for path in paths])
    conn.executemany(_DROP_HASH, [(path,) for path in rels])


def _reset_files(conn: sqlite3.Connection) -> None:
    """Stop holding every row built from a file and every hash that said a file was read.

    This is what a moved project registry asks for: the registry answered the project of most
    of those rows, so every one of them is re-derived from its file rather than trusted to
    what was filed before the move. The evidence collection keeps its rows - the archive is
    the other source, and it is compared row by row on its own."""
    for statement in _RESET_FILE_ROWS:
        conn.execute(statement)


def _collection_of(rel: str) -> str | None:
    """The collection a vault-relative path belongs to, by the directory it sits in."""
    top = Path(rel).parts[0] if Path(rel).parts else ""
    for collection, directories in FILE_COLLECTIONS.items():
        if top in directories:
            return collection
    return None


def _file_rows(rel: str, current: _File, registry: Registry) -> list[Row]:
    """The rows one file contributes to its collection.

    A fact collection (``pages``, ``archive``) takes the lines its own state admits, read
    through ``aft.page``, grouped under the section each line sits in: the parser owns what a
    line says and what state it is in, and a line it refused holds no fact at all, so a
    broken line reaches the index no more than it reaches `aft.inject`. A prose collection
    (``digests``, ``reports``) is split under its headings, since what it holds is written
    prose rather than fact lines.

    A digest's handoff note lives in its frontmatter rather than its body, so it joins the
    digest's own sections as one more of them (`_handoff_chunks`) - DESIGN 10 files handoff
    notes under ``digests``, and words no body section carries reach search from the frontmatter
    or not at all.

    A page scoped to several projects contributes a row per project and a heading that
    repeats in one file contributes a row per occurrence, so every section of the file
    reaches search as a row of its own (`_row_key`).
    """
    frontmatter, body, _, error = page.split_frontmatter(current.text)
    if error:
        raise _Unreadable(f"cannot be indexed: {error}")
    title = inject.one_line((frontmatter or {}).get("title")) or Path(rel).stem
    description = inject.one_line((frontmatter or {}).get("description"))
    scopes = _project_scopes(rel, frontmatter, registry)
    agent, session_id = _session_of(rel) if current.collection == DIGESTS else (None, None)
    admitted = FACT_STATES.get(current.collection)
    if admitted is None:
        chunks = _prose_chunks(body)
        if current.collection == DIGESTS:
            chunks = [*chunks, *_handoff_chunks(frontmatter)]
    else:
        chunks = _fact_chunks(page.parse(current.text, Path(rel)), admitted)
    rows: list[Row] = []
    occurrences: dict[str, int] = {}
    for heading, text in chunks:
        if not text:
            continue
        occurrences[heading] = seen = occurrences.get(heading, 0) + 1
        for project_id, key_part in scopes:
            rows.append(
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
                    row_key=_row_key(rel, heading, key_part, seen),
                )
            )
    return rows


def _row_key(rel: str, heading: str, key_part: str | None, occurrence: int) -> str:
    """A row's id: the file it came from and the section it is about (BUILD-PLAN M3.1).

    A page covering several projects gets one row per project, so the project id joins the
    key to keep those rows apart. A heading that occurs more than once in a file numbers its
    second and later occurrences, so two sections under the same heading are two rows rather
    than one replacing the other - which is what a day of reports with one fixed set of
    section names per run needs (DESIGN 9).
    """
    parts = [rel] if key_part is None else [rel, key_part]
    parts.append(heading if occurrence == 1 else f"{heading} {occurrence}")
    return "#".join(parts)


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
    """A page's prose under each heading, with the text before the first heading under none.

    A line inside a fenced block is quoted text rather than the page's own structure: a digest
    quoting the commands of the session it covers quotes `#` lines with it, so a heading-shaped
    line in the middle of a code sample is the comment of that command and belongs, with the
    rest of the block, to the section that holds it - not a heading, a row, or a ranking of its
    own.

    A block opens at a marker line and closes at the next line of those same marks alone - same
    character, at least as many of them, nothing after - so the marks of quoted markup stay
    quoted rather than ending the block around them. A page that ends inside a block ends there,
    its rest held by the section that opened the block."""
    chunks: list[tuple[str, list[str]]] = []
    heading = ""
    lines: list[str] = []
    fence = ""
    for line in body.split("\n"):
        marker = _FENCE.match(line)
        if marker is not None:
            marks, rest = marker.group(1), marker.group(2)
            if not fence:
                fence = marks
            elif marks[0] == fence[0] and len(marks) >= len(fence) and not rest.strip():
                fence = ""
        elif not fence:
            found = _PROSE_HEADING.match(line.rstrip())
            if found is not None:
                chunks.append((heading, lines))
                heading, lines = found.group(2) or "", []
                continue
        lines.append(line)
    chunks.append((heading, lines))
    return [(held, "\n".join(text).strip()) for held, text in chunks]


def _handoff_chunks(frontmatter: dict | None) -> list[tuple[str, str]]:
    """The digest's handoff note as the section it contributes to its collection.

    M2.6 writes a digest's handoff into its frontmatter - ``aft.handoff`` carrying what was in
    flight, what failed and what is open (DESIGN 8) - and `aft.inject` shows those same lines
    to a new session, so the note is indexed as the block reads it rather than from a second
    reading of the field."""
    lines = inject.handoff(frontmatter)
    return [(HANDOFF, "\n".join(lines))] if lines else []


@dataclass(frozen=True)
class _Projects:
    """The project registry, and the bytes it was read from.

    Those bytes are what the project id of a row was derived from, so `update` keeps their
    digest against `PROJECTS_ORIGIN`: a row filed by its path slug stays current only while
    the registry still gives that slug the id the row carries."""

    registry: Registry
    digest: str


def _project_registry(config: Config) -> _Projects:
    """The project registry, read once per sync as `aft.lint` reads it, with its bytes digested.

    A registry that will not parse is an error rather than an empty one: it is what turns the
    slug of a digest, an archived page or an unlabelled project page into a project id, and
    reading it as empty would label all of those every project's memory.
    """
    try:
        registry = Registry.load(config.registry_path)
        stored = config.registry_path.read_bytes() if config.registry_path.exists() else b""
    except (RegistryError, OSError) as exc:
        raise SearchIndexError(
            f"cannot read the project registry at {config.registry_path}"
            f" to file rows by project: {exc}"
        ) from exc
    return _Projects(registry, hashlib.sha256(stored).hexdigest())


def _project_scopes(
    rel: str, frontmatter: dict | None, registry: Registry
) -> list[tuple[str | None, str | None]]:
    """The project each row of this file belongs to, and what its row key carries to say so.

    A page's own ``aft.project`` (DESIGN 6) wins. A reference page is one collection of lines
    covering several projects at once (DESIGN 11), so ``aft.projects`` gives a row per project
    and each row names itself in its key. Failing both, the directory a file is filed under
    names the project it belongs to and the registry turns that slug into an id; a slug the
    registry does not know is no error and gives no id.

    A row with no id is every project's memory - the global pages of DESIGN 6, a report day,
    or a reference page that covers nothing yet - and not a page whose project went missing.
    """
    aft = (frontmatter or {}).get("aft")
    if isinstance(aft, dict):
        project = aft.get("project")
        if isinstance(project, str) and project:
            return [(project, None)]
        listed = aft.get("projects")
        if isinstance(listed, list):
            ids: list[str] = []
            for value in listed:
                if isinstance(value, str) and value and value not in ids:
                    ids.append(value)
            if ids:
                return [(project_id, project_id) for project_id in ids]
    slug = _path_slug(rel)
    if slug is not None:
        found = registry.by_slug(slug)
        if found is not None:
            return [(found.id, None)]
    return [(None, None)]


def _path_slug(rel: str) -> str | None:
    """The slug of the project a file is filed under (DESIGN 6's vault tree), or None where
    its directory names no project. ``archive/`` files one page per project, so its slug is
    the page's name."""
    parts = Path(rel).parts
    if len(parts) < 2 or parts[0] not in PROJECT_DIRS:
        return None
    if parts[0] != ARCHIVE:
        return parts[1]
    return Path(parts[1]).stem if len(parts) == 2 else None


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


#: The archive's rows in one scan: the key, everything an index row copies out of the row, and
#: the stored payload its digest is taken from. Ordered by the key, so a rebuild arrives the
#: same way whatever order the archive's rows happen to sit in.
_SCAN_ARCHIVE = (
    "SELECT session_id, entry_id, agent, project_id, time, text FROM evidence"
    " ORDER BY session_id, entry_id"
)


def _sync_library(conn: sqlite3.Connection, config: Config) -> list[str]:
    """Index extracted library documents, keeping them in step with references and cache."""
    report: list[str] = []
    refs_dir = config.vault / "references"
    if not refs_dir.is_dir():
        return report

    recorded = dict(conn.execute(_SELECT_HASHES))
    active_library_paths: set[str] = set()

    for col_dir in sorted(refs_dir.iterdir()):
        if not col_dir.is_dir():
            continue
        col_file = col_dir / "collection.md"
        if not col_file.is_file():
            continue
        try:
            col_text = col_file.read_text(encoding="utf-8")
        except OSError as exc:
            report.append(f"{col_file.relative_to(config.vault).as_posix()}: cannot read: {exc}")
            continue

        frontmatter, _, _, _ = page.split_frontmatter(col_text)
        aft = (frontmatter or {}).get("aft")
        if not isinstance(aft, dict):
            continue
        col_name = aft.get("collection") or col_dir.name
        col_desc = (frontmatter or {}).get("description") or ""
        projects = aft.get("projects") or ()
        files_dict = aft.get("files") or {}
        if not isinstance(files_dict, dict):
            continue

        # Derived text is indexed in place of its original (DESIGN 11)
        derived_targets: set[str] = set()
        for doc_md in col_dir.glob("*.md"):
            if doc_md.name == "collection.md":
                continue
            try:
                doc_text = doc_md.read_text(encoding="utf-8")
                doc_fm, _, _, _ = page.split_frontmatter(doc_text)
                if doc_fm and doc_fm.get("aft", {}).get("role") == "derived":
                    for line in doc_text.splitlines():
                        if "derived-from:" in line:
                            m = re.search(r"\[\[.*?\|(.*?)\]\]", line)
                            if m:
                                derived_targets.add(m.group(1).strip())
            except OSError:
                pass

        for rel_path, f_hash in files_dict.items():
            if rel_path in derived_targets:
                continue
            indexed_path = f"library/{col_name}/{rel_path}"
            active_library_paths.add(indexed_path)

            if recorded.get(indexed_path) == f_hash:
                continue

            conn.execute(_DELETE_BY_PATH[LIBRARY], (indexed_path,))

            src_file = config.vault / indexed_path
            cache_file = config.home / "library" / f"{f_hash}.json"
            chunks: list[tuple[str, str]] = []
            if cache_file.is_file():
                try:
                    cached_data = json.loads(cache_file.read_text(encoding="utf-8"))
                    chunks = [
                        (c["heading"], c["text"])
                        for c in cached_data.get("chunks", [])
                        if c.get("text")
                    ]
                except (json.JSONDecodeError, OSError):
                    pass

            if not chunks:
                if src_file.is_file():
                    from aft.library.importing import extract_document_chunks

                    chunks = extract_document_chunks(src_file)

            if not chunks:
                conn.execute(_RECORD_HASH, (indexed_path, f_hash))
                continue

            doc_title = Path(rel_path).name
            doc_desc = col_desc
            project_ids = list(projects) if projects else [None]

            occurrences: dict[str, int] = {}
            for heading, text in chunks:
                if not text:
                    continue
                occurrences[heading] = seen = occurrences.get(heading, 0) + 1
                for pid in project_ids:
                    row = Row(
                        title=doc_title,
                        description=doc_desc,
                        heading=heading,
                        body=text,
                        path=indexed_path,
                        project_id=pid,
                        agent=None,
                        session_id=None,
                        entry_id=None,
                        time=(
                            datetime.fromtimestamp(src_file.stat().st_mtime, UTC).isoformat()
                            if src_file.exists()
                            else ""
                        ),
                        row_key=_row_key(indexed_path, heading, pid, seen),
                        entry_hash=f_hash,
                    )
                    _insert(conn, LIBRARY, row)

            conn.execute(_RECORD_HASH, (indexed_path, f_hash))

    # Drop any library files no longer in active collections
    for old_path in list(recorded):
        if old_path.startswith("library/") and old_path not in active_library_paths:
            conn.execute(_DELETE_BY_PATH[LIBRARY], (old_path,))
            conn.execute(_DROP_HASH, (old_path,))

    return report


def _sync_evidence(conn: sqlite3.Connection, config: Config) -> None:
    """Index the archive rows this index has not seen, and stop holding the ones it dropped.

    The row identity is the archive's ``(session_id, entry_id)`` primary key, which the
    archive itself keeps unique across a rerun (``aft.evidence.store`` replaces a row rather
    than adding a copy). What says whether a row is current is everything the index row copies
    out of the archive row - the SHA-256 of the stored payload, and the agent, project and time
    the row carries - so a redaction that rewrote archived text in place (`aft forget pattern`,
    BUILD-PLAN M2.8) reaches the index whatever the compressed text measures, and so does a
    session whose project moved because its directory was registered after it was archived.
    The stored bytes are digested as they are read; only the rows that moved are decompressed.

    Nothing is removed that was never recorded: a row and the state that says it was read land
    in the same transaction, so a key the index holds no state for is a key with no row to
    drop. What does go - rows whose archive row is gone, and rows whose copied state moved -
    goes out after the scan, each named by the key it was stored under, which is why a rebuild
    over an empty index deletes nothing at all (`_unindex`).
    """
    recorded = {
        (session_id, entry_id): (digest, agent, project_id, moment)
        for session_id, entry_id, digest, agent, project_id, moment in conn.execute(_EVIDENCE_STATE)
    }
    archive = config.home / EVIDENCE_NAME
    if not archive.exists():
        _unindex(conn, EVIDENCE, sorted(recorded))
        return
    live: set[tuple[str, str]] = set()
    rewritten: list[tuple[tuple[str, str], sqlite3.Row, bytes, str]] = []
    with _reading_archive(archive) as source:
        for record in source.execute(_SCAN_ARCHIVE):
            key = (record["session_id"], record["entry_id"])
            payload = bytes(record["text"])
            digest = hashlib.sha256(payload).hexdigest()
            live.add(key)
            copied = (digest, record["agent"], record["project_id"], record["time"])
            if recorded.get(key) == copied:
                continue  # every field this row was built from is what it was built from
            if key in recorded:
                # Its rows are here from before, and they say what this row now says they
                # should not: they go with the rest, and the fresh one follows the removal.
                rewritten.append((key, record, payload, digest))
            else:
                _insert(conn, EVIDENCE, _evidence_row(record, payload, digest))
    _unindex(
        conn,
        EVIDENCE,
        sorted(set(recorded) - live) + sorted(key for key, _, _, _ in rewritten),
    )
    for _, record, payload, digest in rewritten:
        _insert(conn, EVIDENCE, _evidence_row(record, payload, digest))


def _unindex(conn: sqlite3.Connection, collection: str, keys: list[tuple[str, str]]) -> None:
    """Stop holding the rows of these archive keys, one statement per key naming exactly it.

    ``(session_id, entry_id)`` is where an evidence row was stored, so it is what its removal
    names. FTS5 keeps that key beside the row unindexed and so reads the whole collection for a
    delete whatever the WHERE clause says, which is why the list it is handed is only ever keys
    whose rows this index recorded itself: the read then costs what a run left behind. Rows
    under a key this was not given keep their place."""
    conn.executemany(_DELETE_BY_ENTRY[collection], keys)


def _evidence_row(record: sqlite3.Row, payload: bytes, digest: str) -> Row:
    """One archive row as an index row: its text decompressed, its stored bytes digested.

    The digest is of the payload as the archive holds it, not of the text, so a redaction
    that rewrote the words moved it whatever the compressed form measures (`_sync_evidence`).
    """
    session_id = record["session_id"]
    entry_id = record["entry_id"]
    try:
        text = zlib.decompress(payload).decode("utf-8")
    except (zlib.error, UnicodeDecodeError) as exc:
        raise SearchIndexError(
            f"the archived text of entry {entry_id} of session {session_id}"
            " does not decompress as it was written, so it cannot be indexed"
        ) from exc
    return Row(
        body=text,
        path=f"{SESSION_RESOURCE}{record['agent']}/{session_id}",
        project_id=record["project_id"],
        agent=record["agent"],
        session_id=session_id,
        entry_id=entry_id,
        time=record["time"],
        row_key=f"{session_id}#{entry_id}",
        entry_hash=digest,
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
        conn.row_factory = sqlite3.Row
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


def _secure_delete_tokens(conn: sqlite3.Connection) -> None:
    """Turn on FTS5's secure-delete for each collection, so a deleted row's tokens leave the
    full-text index itself rather than lingering in its segments until a merge."""
    for name in COLLECTIONS:
        if not conn.execute(f"SELECT 1 FROM {name}_config WHERE k = 'secure-delete'").fetchone():
            conn.execute(f"INSERT INTO {name}({name}, rank) VALUES ('secure-delete', 1)")


def _insert(conn: sqlite3.Connection, collection: str, row: Row) -> None:
    _require_collection(collection)
    conn.execute(_INSERT[collection], row.values())


def _require_current_build(conn: sqlite3.Connection, path: Path) -> None:
    """Refuse an index another build wrote, naming the one command that rebuilds it.

    A row says what it holds and nothing about the rules that made it: its columns, which
    states its collection admits, and the project it carries are decided here rather than in the
    vault. An index written under other rules therefore answers a query looking exactly as
    trustworthy as a fresh one, and the only honest answer to finding one is the rebuild that
    replaces its rows.

    Two answers are refusals and say so differently: rows carrying another build's stamp mean
    what that build meant, and rows carrying no stamp at all came from a build that said nothing
    about itself. A file with no stamp and no rows has nothing for the claim to protect - its
    tables may be there from a run that failed before writing a row - and is written now."""
    recorded = conn.execute(_SELECT_BUILD, (_BUILD_KEY,)).fetchone()
    if recorded is None:
        if conn.execute(_HOLDS_ROWS).fetchone()[0]:
            raise SearchIndexError(
                f"the index at {path} holds rows written before an index said which build wrote"
                " them, so nothing vouches for what they mean: run aft reindex to rebuild it"
                " from the vault and the archive"
            )
        return
    if recorded[0] != BUILD_STAMP:
        raise SearchIndexError(
            f"the index at {path} holds rows written by build {recorded[0]}, not by build"
            f" {BUILD_STAMP}: run aft reindex to rebuild it from the vault and the archive"
        )


def _record_build(conn: sqlite3.Connection) -> None:
    """Say which build wrote these rows, in the same transaction as the rows themselves."""
    conn.execute(_RECORD_BUILD, (_BUILD_KEY, BUILD_STAMP))


@contextmanager
def _writing(config: Config, *, rebuild: bool = False) -> Iterator[sqlite3.Connection]:
    """One connection held for one transaction, its write lock taken before the first row.

    Two writers - a search that finds the index behind, and a run rebuilding it - land their
    rows in turn rather than one of them failing on a busy database. An update that cannot
    finish leaves no part of itself behind to be read as a whole index.

    The file is asked who wrote its rows before this one adds to them, and a file holding no rows
    at all has never been built and answers that way. `rebuild` is the one operation allowed to
    skip the asking, because the first thing it does is drop every row in the file
    (`_require_current_build`)."""
    path = _db_path(config)
    conn = _connect(config, rebuild=rebuild)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _secure_delete_tokens(conn)
        yield conn
        conn.commit()
        # A reader still attached to the log keeps the checkpoint from finishing; the next
        # write drains what this one could not.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error as exc:
        conn.rollback()
        raise SearchIndexError(f"cannot write the index at {path}: {exc}") from exc
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _connect(config: Config, *, rebuild: bool = False) -> sqlite3.Connection:
    """Open the index, creating it on first use.

    Connections stay in autocommit so the only transaction is the one `_writing` opens by hand,
    and the journal is WAL so a search reads while a run writes. The index holds a copy of the
    archive's conversation text, so a forget has to reach it (DESIGN 12): secure delete
    overwrites the pages a delete frees, each collection's FTS5 secure-delete option takes a
    removed row's tokens out of the full-text index, and `_writing` drains the write-ahead log
    after every write.

    The file is asked who wrote its rows, and an answer that is not this build's is refused before
    a row of ours joins them - asked after the schema is created, so that a file old enough to
    predate the stamp still answers the question instead of failing it, and a file holding no rows
    at all has never been built and is written now. `rebuild` is the one caller that may skip the
    asking, since what it drops is every row the answer would be about."""
    path = _db_path(config)
    conn: sqlite3.Connection | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None, timeout=BUSY_TIMEOUT_SECONDS)
        conn.execute("PRAGMA secure_delete = ON")
        if _journal_mode(conn) != WAL_MODE:
            _enter_wal(conn, path)
        _create(conn)
        if not rebuild:
            _require_current_build(conn, path)
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
