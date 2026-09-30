"""The search index: what each collection holds, and how it is kept in step with the vault
and the evidence archive (BUILD-PLAN M3.1, DESIGN 10).

Every row is read back out of ``index.sqlite`` and compared as a row, not as a count: counts
agree when rows are missing in one build and duplicated in the other, which is how a broken
update path reports green. A row's id is its ``row_key``, so comparing rows says the same rows
under the same ids came out, per collection.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from aft import evidence, index, page, pagewrite
from aft.cli import main
from aft.config import Config
from aft.readers import Entry
from aft.registry import Project, Registry

REL = "projects/widget/overview.md"

PAGE = """---
type: Project
title: widget
description: Everything known about the widget.
sources:
  - id: s1
    resource: aft://session/claude/abc#entry-1
    title: Design session
    quote: "we will use SQLite"
  - id: s2
    resource: aft://session/pi/def#entry-9
    quote: "switched to FTS5"
aft:
  project: p_widget
---

# Widget

## Decisions

- The index is plain SQLite. [stated] 2026-09-20 [^s1]
- ~~Agents write their own summary.~~ [suggested] 2026-09-19 [^s1] superseded 2026-09-21 by [^s2]

## State

- The capture hook enqueues work. [observed] 2026-09-22 [^s2]
- ~~The hook calls the model directly.~~ [observed] 2026-09-18 [^s1] withdrawn 2026-09-23
- Capture takes 12 ms. [observed] 2026-09-22 [^s2] (stale 2026-09-25: the hook changed)

## Candidates

- Long sessions need checkpoints. [inferred] 2026-09-22 [^s2] (signals 1/3, sessions 1/2)
"""

# The lines PAGE holds back, with the state ``aft.page`` reads from their markers and the
# section they sit in. Each is checked against the parser before the index is tested on it.
SUPERSEDED = (page.SUPERSEDED, "Agents write their own summary.", "Decisions")
WITHDRAWN = (page.WITHDRAWN, "The hook calls the model directly.", "State")
STALE = (page.STALE, "Capture takes 12 ms.", "State")
HELD = [SUPERSEDED, WITHDRAWN, STALE]
LIVE = ["The index is plain SQLite.", "The capture hook enqueues work."]
CANDIDATE = "Long sessions need checkpoints."

ARCHIVE = """---
title: widget archive
description: Lines that rested.
sources: []
---

## Decisions

- ~~Ship the widget in steel.~~ [stated] 2026-01-01 [^s1] superseded 2026-02-01 by [^s1]

## Facts

- ~~The bronze prototype still exists.~~ [observed] 2026-01-04 [^s1] withdrawn 2026-02-02
"""

DIGEST = """---
type: Session Digest
title: widget session
description: What the session on 2026-09-28 did.
---

## Summary

The widget gained a flange.

## Handoff

In flight: the flange tests.
"""

REPORT = """---
type: Dream Report
title: 2026-09-28
description: One day of dreaming.
---

## widget

Confirmed the flange fact and left two candidates below the gate.
"""

PREFERENCES = """---
type: Preferences
title: Preferences
description: What Ted wants everywhere.
sources: []
---

## Preferences

- No RGB lighting. [stated] 2026-09-20 [^s1]
"""

# A digest that carries the same heading twice, as a day's notes written over themselves
# read. The two sections hold different words, and each has to reach search on its own.
TWO_SUMMARIES = """---
type: Session Digest
title: widget session
description: Two run notes in one file.
---

## Summary

The widget gained a flange.

## Handoff

Nothing is open.

## Summary

The gasket arrived oversize.
"""

# A page whose frontmatter opens and never closes: the page as it looks halfway through a
# save, or as it looks when a hand-typed block is missing its closing rule.
UNCLOSED_PAGE = (
    "---\ntype: Project\ntitle: widget\nsources: []\n\n## Facts\n\n"
    "- Orphan. [stated] 2026-09-20 [^s1]\n"
)

# Two different texts the archive compresses to the same number of bytes, so an update that
# decided whether an archived row had changed from the size of its stored text sees nothing
# move. Their equality is asserted from the archive's own bytes where it is used.
FIRST_TEXT = "The flange is bronze, cast at 401 degrees and checked twice."
SECOND_TEXT = "The flange is nickel, cast at 400 degrees and checked twice."


def write(cfg: Config, rel: str, text: str) -> str:
    """Put one page in the vault and return where it went."""
    path = cfg.vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return rel


def register(cfg: Config, slug: str) -> Project:
    """Put one project in the registry, and return it so a test can name the id it gives out."""
    registry = Registry.load(cfg.registry_path)
    project = registry.add(slug)
    registry.save()
    return project


def stored_size(cfg: Config, session_id: str, entry_id: str) -> int:
    """The bytes the archive holds for one entry, compressed as it stores them."""
    with sqlite3.connect(cfg.home / evidence.EVIDENCE_NAME) as conn:
        return conn.execute(
            "SELECT length(text) FROM evidence WHERE session_id = ? AND entry_id = ?",
            (session_id, entry_id),
        ).fetchone()[0]


def edit(cfg: Config, rel: str) -> pagewrite.PageDoc:
    """The page as the merged page writer sees it, so an edit in a test is a real one."""
    return pagewrite.PageDoc((cfg.vault / rel).read_text(), cfg.vault / rel)


def rows(cfg: Config, collection: str) -> list[tuple]:
    """Every row of one collection, in ``row_key`` order.

    Two rows under one key would read as one section found twice in a search, so a collection
    that holds them fails here rather than quietly averaging into a matching count.
    """
    columns = ", ".join(index.COLUMNS)
    with _open(cfg) as conn:
        found = conn.execute(f"SELECT {columns} FROM {collection} ORDER BY row_key").fetchall()
    keys = [_column(row, "row_key") for row in found]
    assert len(keys) == len(set(keys)), f"{collection} indexed some rows twice: {keys}"
    return found


def row_for(cfg: Config, collection: str, row_key: str) -> tuple:
    """One row by its id."""
    wanted = [row for row in rows(cfg, collection) if _column(row, "row_key") == row_key]
    assert len(wanted) == 1, f"{row_key!r} is indexed {len(wanted)} times in {collection}"
    return wanted[0]


def keys_of(cfg: Config, collection: str) -> list[str]:
    return [_column(row, "row_key") for row in rows(cfg, collection)]


def search(cfg: Config, collection: str, query: str) -> list[str]:
    """The ids of the rows a query matches in one collection, best-ranked first."""
    rank = index.rank_expression(collection)
    with _open(cfg) as conn:
        return [
            key
            for (key,) in conn.execute(
                f"SELECT row_key FROM {collection} WHERE {collection} MATCH ? ORDER BY {rank}",
                (query,),
            )
        ]


def hashes(cfg: Config) -> dict[str, str]:
    """The content hash the index recorded for every file it has read."""
    with _open(cfg) as conn:
        return dict(conn.execute(f"SELECT path, digest FROM {index.FILES_TABLE}"))


def pin(path: Path, moment: int) -> None:
    """Give a file a modification time in whole seconds, so it can be set and read back alike."""
    os.utime(path, (moment, moment))


def _open(cfg: Config) -> sqlite3.Connection:
    return sqlite3.connect(cfg.home / index.INDEX_NAME)


def _column(row: tuple, column: str) -> Any:
    return row[index.COLUMNS.index(column)]


def entries(*texts: str) -> list[Entry]:
    """Archive entries one hour apart from the start of 2026-09-28."""
    return [
        Entry(
            id=f"e{number}",
            time=datetime(2026, 9, 28, number - 1, tzinfo=UTC),
            role="user" if number % 2 else "assistant",
            text=text,
            is_error=False,
        )
        for number, text in enumerate(texts, start=1)
    ]


def test_this_sqlite_builds_fts5_tables_and_a_query_reads_them_back(initialized: Config) -> None:
    """The collections are FTS5 tables holding the weighted columns, and a query reads them.

    Nothing gets skipped to reach this point: a plain table would satisfy a row count and then
    fail every ``MATCH``, so the schema is read out of ``sqlite_master`` rather than trusted.
    On a SQLite without the FTS5 module the same creation is where ``update`` would raise, which
    is why the module ships no skip and this suite no ``skipif``.
    """
    write(initialized, REL, PAGE)
    index.update(initialized)

    with _open(initialized) as conn:
        schema = dict(
            conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall()
        )
    for collection in index.COLLECTIONS:
        assert collection in schema, f"{collection} has no table at all"
        assert "fts5(" in schema[collection], schema[collection]
        for column in index.INDEXED_COLUMNS:
            assert f"{column}" in schema[collection], f"{column} is not a column of {collection}"

    assert search(initialized, "pages", "SQLite") == [f"{REL}#Decisions"]
    assert search(initialized, "pages", "nosuchwordhere") == []


def test_an_edit_through_the_page_writer_moves_the_row_and_its_hash_together(
    initialized: Config,
) -> None:
    """The update keys on content: a line added with the merged page writer reaches its
    section's row, and the recorded hash moves with it, even with the modification time held
    still - which an update keyed on mtime would have skipped whole."""
    write(initialized, REL, PAGE)
    moment = int(os.stat(initialized.vault / REL).st_mtime)
    pin(initialized.vault / REL, moment)
    index.update(initialized)
    before = row_for(initialized, "pages", f"{REL}#Decisions")
    hash_before = hashes(initialized)[REL]

    doc = edit(initialized, REL)
    doc.add_fact("decision", "Flanges are cast, not machined.", "stated", "2026-09-28", ["s1"])
    (initialized.vault / REL).write_text(doc.render())
    pin(initialized.vault / REL, moment)

    index.update(initialized)

    after = row_for(initialized, "pages", f"{REL}#Decisions")
    assert _column(after, "body") == "The index is plain SQLite.\nFlanges are cast, not machined."
    assert _column(after, "body") != _column(before, "body")
    assert _column(after, "time") == _column(before, "time")
    assert hashes(initialized)[REL] != hash_before


def test_an_update_leaves_an_unrelated_page_row_untouched(initialized: Config) -> None:
    """An update that rebuilt every row would hide a broken update path, so the page that did
    not change is compared field by field against the row it already had."""
    write(initialized, REL, PAGE)
    other = write(initialized, "global/preferences.md", PREFERENCES)
    index.update(initialized)
    before = [row for row in rows(initialized, "pages") if _column(row, "path") == other]
    assert before, "the unchanged page was never indexed to begin with"

    doc = edit(initialized, REL)
    doc.add_fact("state", "The flange gauge reads in tenths.", "observed", "2026-09-28", ["s1"])
    (initialized.vault / REL).write_text(doc.render())

    index.update(initialized)

    after = [row for row in rows(initialized, "pages") if _column(row, "path") == other]
    assert after == before


def test_a_line_moved_between_sections_moves_between_rows(initialized: Config) -> None:
    """The writer's own ``promote`` moves a candidate line out of Candidates: the statement
    arrives in the row of the section it joined, and the section it left loses its row entirely
    rather than keeping an empty one a search could return. The other rows do not move."""
    write(initialized, REL, PAGE)
    path = initialized.vault / REL
    moment = int(os.stat(path).st_mtime)
    pin(path, moment)
    index.update(initialized)
    state_before = row_for(initialized, "pages", f"{REL}#State")

    doc = edit(initialized, REL)
    candidate = next(fact for fact in doc.page.facts if fact.state == page.CANDIDATE)
    doc.promote(candidate, "decision")
    path.write_text(doc.render())
    pin(path, moment)  # the move is the only change this update is allowed to see

    index.update(initialized)

    assert f"{REL}#Candidates" not in keys_of(initialized, "pages")
    assert CANDIDATE in _column(row_for(initialized, "pages", f"{REL}#Decisions"), "body")
    assert row_for(initialized, "pages", f"{REL}#State") == state_before


def test_dead_lines_are_not_in_pages(initialized: Config) -> None:
    """Superseded, withdrawn and stale lines, and candidates, contribute no text to ``pages``.

    The states come from ``aft.page``, and the page this test writes is read by the parser
    first: the fixture has to carry the states the index is judged against, so a line the
    parser reads any other way fails here instead of quietly agreeing with the index. DESIGN 6
    marks all four "Injected? No" and DESIGN 10 files candidate facts under Digests instead.
    """
    write(initialized, REL, PAGE)
    parsed = page.parse(PAGE, Path(REL))
    by_statement = {fact.statement: fact for fact in parsed.facts}
    assert not parsed.errors, parsed.errors
    for state, statement, section in HELD + [(page.CANDIDATE, CANDIDATE, "Candidates")]:
        fact = by_statement[statement]
        assert fact.state == state, f"{statement!r} reads as {fact.state}, not {state}"
        assert fact.section == section

    index.update(initialized)

    bodies = {_column(row, "row_key"): _column(row, "body") for row in rows(initialized, "pages")}
    assert bodies == {f"{REL}#Decisions": LIVE[0], f"{REL}#State": LIVE[1]}
    for _state, statement, _section in HELD + [(page.CANDIDATE, CANDIDATE, "Candidates")]:
        assert all(statement not in body for body in bodies.values()), statement


def test_a_page_with_no_live_line_contributes_no_rows(initialized: Config) -> None:
    """``pages`` holds active curated lines, so a page with none contributes nothing: no empty
    row for a search to return, and not its title or description either."""
    write(initialized, "projects/widget/resting.md", _ALL_DEAD)
    index.update(initialized)

    assert rows(initialized, "pages") == []
    assert search(initialized, "pages", "Steel") == []


_ALL_DEAD = """---
type: Project
title: Steel
description: A page whose every line is dead.
sources: []
---

## Decisions

- ~~Ship it in steel.~~ [stated] 2026-01-01 [^s1] superseded 2026-02-01 by [^s1]
"""

# A digest whose writer kept the handoff where DESIGN 8 puts it - in the frontmatter, which is
# what `aft inject` reads - so none of these words appears in a body section.
HANDOFF_DIGEST = """---
type: Session Digest
title: widget session
description: What the session on 2026-09-28 did.
aft:
  handoff:
    in_flight: the gasket arrived oversize
    failed:
      - brazing the flange at 380 degrees
    open:
      - whether the jig holds tolerances
---

## Summary

The widget gained a flange.
"""

# A digest quoting the commands of the session it covers: the comment inside the block is shaped
# like a heading, and the words of the command are the only place they appear.
FENCED_DIGEST = """---
type: Session Digest
title: widget session
description: What the session on 2026-09-28 did.
---

## Summary

The widget gained a flange, and this is how the build was run.

```bash
#!/usr/bin/env python3
# ship it
aft reindex --collection bronze
```

## Handoff

Nothing was left open.
"""

# A digest quoting the marks of a fenced block: the marks it quotes carry a language name, and
# marks carrying a name open a block rather than closing one, so only the bare marks below close
# the block the digest opened.
QUOTED_MARKUP_DIGEST = """---
type: Session Digest
title: widget session
description: What the session on 2026-09-28 did.
---

## Summary

How a digest quotes a command is itself worth quoting:

~~~text
~~~bash
# ship it
~~~

That is the whole recipe.

## Handoff

Nothing was left open.
"""

# A digest quoting a document that is itself full of blocks: the quoted document's headings and
# fenced blocks are quoted text, and only the outer marks, carrying nothing after them, end the
# block the digest opened.
NESTED_FENCE_DIGEST = """---
type: Session Digest
title: widget session
description: What the session on 2026-09-28 did.
---

## Summary

Quoting a document means quoting its own blocks:

````markdown
# Memory: widget

```yaml
title: widget
```

## Decisions

Bronze was chosen.

```bash
# ship it
aft reindex --collection bronze
```
````

## Handoff

Nothing was left open.
"""

# A digest whose fenced block never closes: the block runs to the end of the page, and the
# heading-shaped line that was going to open the next section is quoted text like the rest.
UNCLOSED_FENCE_DIGEST = """---
type: Session Digest
title: widget session
description: What the session on 2026-09-28 did.
---

## Summary

The build ran like this:

```bash
# ship it
aft reindex --collection bronze

## Handoff

Nothing was left open.
"""


def test_the_archive_is_a_collection_of_its_own(initialized: Config) -> None:
    """Resting lines under ``archive/`` are a collection apart (DESIGN 10): a query-time read of
    ``pages`` does not reach them, and a later update does not empty the collection that holds
    them."""
    write(initialized, "archive/widget.md", ARCHIVE)
    index.update(initialized)

    assert keys_of(initialized, "archive") == [
        "archive/widget.md#Decisions",
        "archive/widget.md#Facts",
    ]
    assert (
        _column(row_for(initialized, "archive", "archive/widget.md#Decisions"), "body")
        == "Ship the widget in steel."
    )

    index.update(initialized)  # nothing changed, and the archive's own rows have to stay

    assert search(initialized, "pages", "steel OR bronze OR prototype") == []
    assert search(initialized, "archive", "bronze") == ["archive/widget.md#Facts"]


def test_content_changes_a_row_and_an_mtime_alone_does_not(initialized: Config) -> None:
    """The plan makes content the update key, so both halves are proved: a touched modification
    time over unchanged text leaves the row and the hash exactly as they were, and text that
    changed behind an unchanged modification time is re-indexed."""
    rel = write(initialized, REL, PAGE)
    path = initialized.vault / rel
    moment = int(os.stat(path).st_mtime)
    pin(path, moment)
    index.update(initialized)
    before = row_for(initialized, "pages", f"{rel}#Decisions")
    hash_before = hashes(initialized)[rel]

    pin(path, moment + 3600)
    index.update(initialized)
    assert row_for(initialized, "pages", f"{rel}#Decisions") == before
    assert hashes(initialized)[rel] == hash_before

    path.write_text((initialized.vault / rel).read_text().replace("plain SQLite", "SQLite FTS5"))
    pin(path, moment + 3600)
    index.update(initialized)
    assert _column(row_for(initialized, "pages", f"{rel}#Decisions"), "body") == (
        "The index is SQLite FTS5."
    )
    assert hashes(initialized)[rel] != hash_before


def test_a_deleted_page_loses_its_rows(initialized: Config) -> None:
    """The vault's files are the memory (DESIGN 14), so words from a page that is gone are not
    memory for a search to return: its rows and its hash record go on the next update."""
    rel = write(initialized, REL, PAGE)
    index.update(initialized)
    assert rows(initialized, "pages")

    (initialized.vault / rel).unlink()
    index.update(initialized)

    assert rows(initialized, "pages") == []
    assert rel not in hashes(initialized)
    assert search(initialized, "pages", "SQLite") == []


def test_removing_one_page_leaves_every_row_it_never_named(initialized: Config) -> None:
    """Each removal names the one path it means, so the page a run dropped loses its rows and its
    hash record while the page it never mentioned keeps its row field for field. A removal that
    swept the collection alongside it would look identical until a search came up empty."""
    gone = write(initialized, "projects/widget/scrapped.md", _a_page(title="scrapped"))
    kept = write(initialized, "projects/widget/decisions.md", _a_page(title="decisions"))
    index.update(initialized)
    standing = tuple(row_for(initialized, "pages", f"{kept}#Decisions"))

    (initialized.vault / gone).unlink()
    assert index.update(initialized) == []

    assert [tuple(row) for row in rows(initialized, "pages")] == [standing]
    assert gone not in hashes(initialized)
    assert search(initialized, "pages", "schedule") == [f"{kept}#Decisions"]


def test_an_appended_evidence_row_is_indexed_once(initialized: Config) -> None:
    """Evidence is keyed by the archive's own row identity: an appended entry adds one row, and
    the archive being read again - a rerun, or the same entries archived a second time - does
    not index the rows already there a second time, because duplicated FTS rows show up as
    relevance damage rather than as an error."""
    evidence.store(initialized, "claude", "abc", "p_widget", entries("ship the flange"))
    index.update(initialized)
    once = rows(initialized, "evidence")
    assert [_column(row, "row_key") for row in once] == ["abc#e1"]

    # The session archived again with one entry added on top: the rows it re-reads stay as they
    # were, and only the new one arrives.
    evidence.store(initialized, "claude", "abc", "p_widget", entries("ship the flange", "shipped"))
    index.update(initialized)
    index.update(initialized)

    twice = rows(initialized, "evidence")
    assert [_column(row, "row_key") for row in twice] == ["abc#e1", "abc#e2"]
    assert twice[0] == once[0]


def test_the_indexed_evidence_text_is_the_archives_own_text(initialized: Config) -> None:
    """The index reads the archive row by row and decompresses what it finds, so its rows are
    checked against the archive's own reader: wherever the two disagree about what a row holds,
    this is where it shows."""
    evidence.store(initialized, "pi", "def", "p_widget", entries("a flange", "another flange"))
    index.update(initialized)

    indexed = {
        _column(row, "entry_id"): _column(row, "body") for row in rows(initialized, "evidence")
    }
    assert indexed == {entry.id: entry.text for entry in evidence.entries(initialized, "def")}


def test_a_rewritten_archive_row_is_reindexed_though_its_stored_size_did_not_move(
    initialized: Config,
) -> None:
    """``aft forget pattern`` redacts archived text under the row's own key (BUILD-PLAN M2.8),
    and DESIGN 12 requires the redaction to reach the index as well as the archive. The change
    is caught on the bytes, not on what they happen to measure: these two texts compress to the
    same length under the same key, and the row still reads back as the new text - while the
    session the removal never names stays exactly where it was."""
    evidence.store(initialized, "claude", "abc", "p_widget", entries(FIRST_TEXT))
    evidence.store(initialized, "pi", "def", "p_widget", entries(SECOND_TEXT))
    stored = stored_size(initialized, "abc", "e1")
    assert stored_size(initialized, "def", "e1") == stored
    index.update(initialized)
    before = row_for(initialized, "evidence", "abc#e1")
    untouched = row_for(initialized, "evidence", "def#e1")
    assert _column(before, "body") == FIRST_TEXT

    # The redaction: the same key, the same number of stored bytes, other words.
    evidence.store(initialized, "claude", "abc", "p_widget", entries(SECOND_TEXT))
    assert stored_size(initialized, "abc", "e1") == stored
    assert index.update(initialized) == []

    after = row_for(initialized, "evidence", "abc#e1")
    assert _column(after, "body") == SECOND_TEXT
    assert _column(after, "entry_hash") != _column(before, "entry_hash")
    assert row_for(initialized, "evidence", "def#e1") == untouched
    assert sorted(search(initialized, "evidence", "nickel")) == ["abc#e1", "def#e1"]
    assert search(initialized, "evidence", "bronze") == []


def test_forgotten_evidence_leaves_the_index(initialized: Config) -> None:
    """``aft forget`` and the retention window remove content from everything afterthought
    controls (DESIGN 12, DESIGN 8), and the index is one of those things."""
    evidence.store(initialized, "claude", "abc", "p_widget", entries("a flange", "old words"))
    index.update(initialized)
    assert keys_of(initialized, "evidence") == ["abc#e1", "abc#e2"]

    evidence.forget(initialized, "abc")
    index.update(initialized)

    assert rows(initialized, "evidence") == []
    assert search(initialized, "evidence", "flange") == []


def test_forgotten_evidence_leaves_no_bytes_in_the_index_files(initialized: Config) -> None:
    """DESIGN 12: forgetting reaches everything afterthought controls. The proof is the raw
    bytes of the index and its write-ahead log, not a query that comes back empty."""
    secret = "the vault combination is zanzibar-quartz"
    filler = [f"filler entry number {i} about nothing in particular" for i in range(20)]
    evidence.store(initialized, "claude", "gone", None, entries(secret, *filler))
    index.update(initialized)
    files = [p for p in initialized.home.glob("index.sqlite*") if p.is_file()]
    assert any(b"zanzibar" in p.read_bytes() for p in files)

    evidence.forget(initialized, "gone")
    index.update(initialized)

    for path in initialized.home.glob("index.sqlite*"):
        if path.is_file():
            assert b"zanzibar" not in path.read_bytes(), path.name


def test_forgetting_one_session_leaves_every_row_it_never_named(initialized: Config) -> None:
    """Each removal names the one archive key it means, so the session a forget never mentioned is
    still there afterwards - field for field, and still searchable. Removal that took the
    collection with it would only show up as a search that comes back empty."""
    evidence.store(initialized, "claude", "abc", "p_widget", entries("the flange is bronze"))
    evidence.store(initialized, "pi", "def", "p_gizmo", entries("the gasket is nitrile"))
    index.update(initialized)
    standing = tuple(row_for(initialized, "evidence", "def#e1"))

    evidence.forget(initialized, "abc")
    assert index.update(initialized) == []

    assert [tuple(row) for row in rows(initialized, "evidence")] == [standing]
    assert search(initialized, "evidence", "nitrile") == ["def#e1"]


def test_a_bulk_forget_loses_every_row_it_named_and_only_those(initialized: Config) -> None:
    """A retention purge or ``aft forget`` can drop hundreds of archive rows in one run, and each
    goes out named by its own key: every row of the forgotten session is gone from the index and
    the session that was never named keeps its row field for field, still searchable."""
    moment = datetime(2026, 9, 28, 12, tzinfo=UTC)
    bulk = [
        Entry(
            id=f"e{number}",
            time=moment,
            role="user",
            text=f"the archive holds entry {number}",
            is_error=False,
        )
        for number in range(1, 502)
    ]
    evidence.store(initialized, "claude", "abc", "p_widget", bulk)
    evidence.store(initialized, "pi", "def", "p_gizmo", entries("the gasket is nitrile"))
    assert index.update(initialized) == []
    assert len(rows(initialized, "evidence")) == 502

    evidence.forget(initialized, "abc")
    assert index.update(initialized) == []

    assert search(initialized, "evidence", "entry") == []
    assert [tuple(row) for row in rows(initialized, "evidence")] == [
        tuple(row_for(initialized, "evidence", "def#e1"))
    ]
    assert search(initialized, "evidence", "nitrile") == ["def#e1"]


def test_an_archive_row_whose_project_moved_is_read_again(initialized: Config) -> None:
    """`aft project add` gives a directory an id, and the next chunk of a session already
    archived from it is stored under that id while the entries it re-reads keep their text
    (BUILD-PLAN M2.4): the archive row moves under its own key and the index row copied the
    project, the agent and the time out of it. Text alone would leave that session's words
    indexed as every project's memory for as long as they stayed word-for-word the same."""
    evidence.store(initialized, "claude", "abc", None, entries(FIRST_TEXT))
    assert index.update(initialized) == []
    assert _column(row_for(initialized, "evidence", "abc#e1"), "project_id") is None

    evidence.store(initialized, "claude", "abc", "p_widget", entries(FIRST_TEXT))
    assert index.update(initialized) == []

    after = row_for(initialized, "evidence", "abc#e1")
    assert _column(after, "project_id") == "p_widget"
    assert _column(after, "body") == FIRST_TEXT


def test_reindex_equals_the_incremental_result(initialized: Config) -> None:
    """The plan's test: an index kept in step as the vault and the archive changed, and the same
    index dropped and rebuilt from those sources, hold the same rows under the same ids in
    every collection - including after a page and an evidence session went away, and including
    the report of the one page neither build could read."""
    rel = write(initialized, REL, PAGE)
    write(initialized, "archive/widget.md", ARCHIVE)
    write(initialized, "archive/steel.md", _ALL_DEAD)
    write(initialized, "digests/widget/2026/09/2026-09-28-claude-abc.md", DIGEST)
    write(initialized, "reports/2026-09-28.md", REPORT)
    write(initialized, "library/widget/notes.txt", "Library notes for widget\n")
    write(
        initialized,
        "references/widget/collection.md",
        """---
type: Reference
title: Widget Library
description: Reference materials for widget
aft:
  collection: widget
  projects:
    - p_widget
  files:
    notes.txt: 44fe67898e8cca3b8252ddb6234814147b5dca7f1ea020a3baf7f032ec7dcff4
---
# Widget Library
""",
    )
    write(initialized, "projects/widget/broken.md", UNCLOSED_PAGE)
    evidence.store(initialized, "claude", "abc", "p_widget", entries("a flange"))
    evidence.store(initialized, "pi", "def", "p_widget", entries("another flange"))
    index.update(initialized)

    doc = edit(initialized, rel)
    doc.add_fact("fact", "The flange is bronze.", "observed", "2026-09-28", ["s2"])
    (initialized.vault / rel).write_text(doc.render())
    evidence.store(initialized, "claude", "abc", "p_widget", entries("a flange", "shipped"))
    index.update(initialized)

    (initialized.vault / "archive" / "steel.md").unlink()
    evidence.forget(initialized, "abc")
    write(initialized, "digests/widget/2026/10/2026-10-01-pi-def-9.md", DIGEST)
    report = index.update(initialized)

    built = {name: rows(initialized, name) for name in index.COLLECTIONS}
    assert all(built[name] for name in index.COLLECTIONS), {
        name: len(built[name]) for name in index.COLLECTIONS
    }

    assert index.reindex(initialized) == report

    assert {name: rows(initialized, name) for name in index.COLLECTIONS} == built


def test_reindex_drops_what_no_file_or_row_justifies(initialized: Config) -> None:
    """``aft reindex`` drops and rebuilds everything, so a row no source justifies - here one
    written straight into a collection table - cannot survive into the result."""
    write(initialized, REL, PAGE)
    index.update(initialized)
    with _open(initialized) as conn:
        conn.execute(
            "INSERT INTO pages (title, body, row_key) VALUES (?, ?, ?)",
            ("invented", "no file says this", "nowhere"),
        )
    assert search(initialized, "pages", "invented") == ["nowhere"]

    index.reindex(initialized)

    assert search(initialized, "pages", "invented") == []
    assert keys_of(initialized, "pages") == [f"{REL}#Decisions", f"{REL}#State"]


def test_an_index_written_by_another_build_names_the_repair_rather_than_answering(
    initialized: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a row *is* is decided by the code that wrote it, not by the file holding it, so an
    carried across an upgrade answers rows nothing vouches for as though they were current. The
    file states which build wrote it, and a build that is not that one refuses with the repair in
    the sentence, naming the build it did find, rather than adding rows of its own to rows it
    cannot vouch for."""
    rel = write(initialized, REL, PAGE)
    assert index.update(initialized) == []

    monkeypatch.setattr(index, "BUILD_STAMP", "the-next-build")
    with pytest.raises(index.SearchIndexError, match="written by build rows-1.*aft reindex"):
        index.update(initialized)
    assert keys_of(initialized, "pages") == [f"{rel}#Decisions", f"{rel}#State"]

    # the rebuild is the repair, and it leaves the rows it rebuilt
    assert index.reindex(initialized) == []
    assert keys_of(initialized, "pages") == [f"{rel}#Decisions", f"{rel}#State"]
    assert index.update(initialized) == []


def test_an_index_holding_rows_that_never_said_who_wrote_them_names_the_repair(
    initialized: Config,
) -> None:
    """An index written before the file said anything about its build holds rows and answers
    nothing about them, which is the case the stamp exists to catch: the refusal says what is
    unknown about the file, in other words than the one naming another build, and the rebuild is
    what answers it."""
    rel = write(initialized, REL, PAGE)
    assert index.update(initialized) == []
    with _open(initialized) as conn:
        conn.execute(f"DROP TABLE {index.BUILD_TABLE}")

    with pytest.raises(index.SearchIndexError, match="before an index said which build wrote"):
        index.update(initialized)
    assert index.reindex(initialized) == []
    assert keys_of(initialized, "pages") == [f"{rel}#Decisions", f"{rel}#State"]


def test_an_index_file_holding_nothing_is_written_rather_than_refused(initialized: Config) -> None:
    """A file with no rows in it has no row whose build a stamp could vouch for, so it is an
    invitation: a run finding one writes it and says nothing about rebuilding."""
    rel = write(initialized, REL, PAGE)
    (initialized.home / index.INDEX_NAME).write_bytes(b"")

    assert index.update(initialized) == []
    assert keys_of(initialized, "pages") == [f"{rel}#Decisions", f"{rel}#State"]


def test_an_index_schema_left_by_a_failed_run_is_finished_rather_than_refused(
    initialized: Config,
) -> None:
    """A first run that failed after creating the schema leaves a file holding no rows and no
    stamp: the tables came from autocommit and the rows did not, so the run that follows it
    builds the index once the cause is gone rather than demanding a rebuild of an index that
    never held anything."""
    rel = write(initialized, REL, PAGE)
    write(initialized, "registry.yaml", "version: 1\nprojects: not-a-list\n")
    with pytest.raises(index.SearchIndexError, match="projects must be a list"):
        index.update(initialized)
    assert (initialized.home / index.INDEX_NAME).exists()
    assert hashes(initialized) == {}

    write(initialized, "registry.yaml", "version: 1\nprojects: []\n")
    assert index.update(initialized) == []
    assert keys_of(initialized, "pages") == [f"{rel}#Decisions", f"{rel}#State"]


def test_aft_reindex_runs_the_rebuild_from_the_command_line(
    initialized: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """``aft reindex`` is the command the plan names (DESIGN 14). It is wiring: the rebuild is
    `aft.index`'s, and what the command adds is the exit code and the file it says it wrote."""
    write(initialized, REL, PAGE)

    assert main(["reindex"]) == 0

    printed = capsys.readouterr().out
    assert f"rebuilt the search index at {initialized.home / index.INDEX_NAME}" in printed
    assert keys_of(initialized, "pages") == [f"{REL}#Decisions", f"{REL}#State"]


def test_aft_reindex_refuses_a_vault_that_is_not_there(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rebuild reads the vault, so with none it refuses rather than writing an index that
    holds nothing but the archive."""
    assert main(["reindex"]) == 2

    assert "aft init" in capsys.readouterr().err
    assert not (home / index.INDEX_NAME).exists()


def test_title_and_description_outrank_the_line_text(initialized: Config) -> None:
    """BM25 weights title and description above body (BUILD-PLAN M3.1): the same word in a
    page's frontmatter beats the same word in a curated line, which is what puts the page about
    a thing above the page that merely mentions it.

    Every page carries the same line, so body length is held equal and the only thing that moves
    is the column the word sits in."""
    pages = {
        "projects/alpha/overview.md": _a_page(title="Zebramine planning"),
        "projects/beta/overview.md": _a_page(description="Zebramine notes"),
        "projects/delta/overview.md": _a_page(statement="Zebramine ships this quarter."),
    }
    for rel, text in pages.items():
        write(initialized, rel, text)
    index.update(initialized)

    ranked = search(initialized, "pages", "Zebramine")
    assert ranked == [
        "projects/alpha/overview.md#Decisions",  # the title alone holds the word
        "projects/beta/overview.md#Decisions",  # the description alone holds it
        "projects/delta/overview.md#Decisions",  # the line text alone holds it
    ]


def test_a_heading_match_outranks_the_same_word_in_body(initialized: Config) -> None:
    """A section's own heading is weighted above the body, not with it: the digest whose heading
    carries the word comes first, the digest that only mentions the word in its prose comes
    second, with the same prose and the same shape in both.

    This one runs over ``digests``, where a heading is free text someone chose. In ``pages`` the
    heading column carries a section of the fact grammar instead, which is why the comparison
    above is between a page's frontmatter and its lines."""
    heading_rel = "digests/widget/2026/09/2026-09-28-claude-gamma.md"
    body_rel = "digests/widget/2026/09/2026-09-28-pi-delta.md"
    write(initialized, heading_rel, _a_digest(heading="Zebramine"))
    write(initialized, body_rel, _a_digest(prose="Zebramine shipped as agreed."))
    index.update(initialized)

    assert search(initialized, "digests", "Zebramine") == [
        f"{heading_rel}#Zebramine",  # the heading alone holds the word
        f"{body_rel}#Summary",  # the prose alone holds it
    ]


def test_two_sections_under_one_heading_are_two_rows(initialized: Config) -> None:
    """A heading that repeats in one file is two rows, not one section replacing the other.
    A busy day puts every run's report in one daily file and every run carries the same
    section names (DESIGN 9), so the second ``## Summary`` of a day has its own row, under its
    own id, and a query for its own words finds it and only it."""
    rel = write(initialized, "digests/widget/2026/09/2026-09-28-claude-abc.md", TWO_SUMMARIES)
    index.update(initialized)

    assert keys_of(initialized, "digests") == [
        f"{rel}#Handoff",
        f"{rel}#Summary",
        f"{rel}#Summary 2",
    ]
    assert search(initialized, "digests", "flange") == [f"{rel}#Summary"]
    assert search(initialized, "digests", "gasket") == [f"{rel}#Summary 2"]
    assert _column(row_for(initialized, "digests", f"{rel}#Summary 2"), "heading") == "Summary"


def test_a_handoff_note_is_searchable_from_the_digests_collection(initialized: Config) -> None:
    """DESIGN 10 files handoff notes under ``digests``, and M2.6 keeps one in the digest's
    frontmatter (`aft.handoff`, DESIGN 8) rather than in a section a body reader would find -
    it is what `aft inject` reads for a new session. The words whose whole job is to be found
    by something looking for context reach search from the frontmatter then, as the block shows
    them, under one heading a hit can name them by."""
    rel = write(initialized, "digests/widget/2026/09/2026-09-28-claude-abc.md", HANDOFF_DIGEST)

    assert index.update(initialized) == []

    assert search(initialized, "digests", "oversize") == [f"{rel}#Handoff"]
    assert search(initialized, "digests", "tolerances") == [f"{rel}#Handoff"]
    note = _column(row_for(initialized, "digests", f"{rel}#Handoff"), "body")
    assert "In flight: the gasket arrived oversize" in note
    assert "Failed: brazing the flange at 380 degrees" in note
    assert "Open: whether the jig holds tolerances" in note
    assert _column(row_for(initialized, "digests", f"{rel}#Summary"), "body") == (
        "The widget gained a flange."
    )


def test_a_line_inside_a_code_fence_is_the_quoted_text_and_not_a_heading(
    initialized: Config,
) -> None:
    """A digest quoting the commands of the session it covers quotes `#` lines with them: a
    comment in a code sample is not a section of the digest, and the words of the block are not
    lifted out of the section holding it and given a heading of their own - which is what a
    model-written digest of a terminal session is mostly made of."""
    rel = write(initialized, "digests/widget/2026/09/2026-09-28-claude-abc.md", FENCED_DIGEST)

    assert index.update(initialized) == []

    assert keys_of(initialized, "digests") == [f"{rel}#Handoff", f"{rel}#Summary"]
    body = _column(row_for(initialized, "digests", f"{rel}#Summary"), "body")
    assert "# ship it" in body
    assert "aft reindex" in body
    # the command's words answer from the section that quotes them, under its heading
    assert search(initialized, "digests", "reindex") == [f"{rel}#Summary"]
    assert search(initialized, "digests", "bronze") == [f"{rel}#Summary"]


def test_a_fence_opened_inside_a_fence_does_not_end_the_block_around_it(
    initialized: Config,
) -> None:
    """A digest quoting a document quotes that document's own headings and its own fenced blocks:
    a heading of the quoted document and a comment inside its own block are quoted text, and the
    quoted document's blocks do not end the block around them - the page comes out with the two
    sections it has, and one row holding all of the quote."""
    rel = write(initialized, "digests/widget/2026/09/2026-09-28-pi-def.md", NESTED_FENCE_DIGEST)

    assert index.update(initialized) == []
    assert keys_of(initialized, "digests") == [f"{rel}#Handoff", f"{rel}#Summary"]
    body = _column(row_for(initialized, "digests", f"{rel}#Summary"), "body")
    assert "# Memory: widget" in body
    assert "## Decisions" in body
    assert "# ship it" in body
    assert search(initialized, "digests", "bronze") == [f"{rel}#Summary"]


def test_marks_carrying_a_language_name_open_a_block_and_close_none(
    initialized: Config,
) -> None:
    """A digest quoting the markup of a code block quotes its opening marks too, marks and language
    name together: those are the way in, never the way out, or the block ends halfway through the
    thing being quoted and everything after it belongs to someone else's section."""
    rel = write(initialized, "digests/widget/2026/09/2026-09-28-pi-def.md", QUOTED_MARKUP_DIGEST)

    assert index.update(initialized) == []
    assert keys_of(initialized, "digests") == [f"{rel}#Handoff", f"{rel}#Summary"]
    body = _column(row_for(initialized, "digests", f"{rel}#Summary"), "body")
    assert "~~~bash" in body
    assert "# ship it" in body
    assert "That is the whole recipe." in body


def test_a_fence_that_never_closes_keeps_the_rest_of_the_page_inside_it(
    initialized: Config,
) -> None:
    """A block that is never closed runs to the end of the page: what follows the marks is quoted
    text, a section heading among it, and the page stays the one section the block sat in rather
    than ending in a state the splitter has to guess at."""
    rel = write(initialized, "digests/widget/2026/09/2026-09-28-pi-def.md", UNCLOSED_FENCE_DIGEST)

    assert index.update(initialized) == []
    assert keys_of(initialized, "digests") == [f"{rel}#Summary"]
    body = _column(row_for(initialized, "digests", f"{rel}#Summary"), "body")
    assert "## Handoff" in body
    assert search(initialized, "digests", "left") == [f"{rel}#Summary"]


def _a_page(
    title: str = "A project",
    description: str = "Nothing about the word here.",
    statement: str = "The schedule holds for the quarter.",
    project: str | None = None,
) -> str:
    """A fact page with one line under ``Decisions``, and one source that line can cite.
    ``project`` is the ``aft.project`` it names, when it names one."""
    aft = f"aft:\n  project: {project}\n" if project else ""
    return (
        f"---\ntype: Project\ntitle: {title}\ndescription: {description}\n"
        "sources:\n  - id: s1\n    resource: aft://session/pi/def#entry-9\n"
        '    quote: "quoted words"\n'
        f"{aft}---\n\n"
        f"## Decisions\n\n- {statement} [stated] 2026-09-20 [^s1]\n"
    )


def _a_scoped_page(
    project_ids: list[str], statement: str = "Flanges are cast, not machined."
) -> str:
    """A collection page whose scope is the list of projects it covers (DESIGN 11)."""
    scope = "".join(f"  - {project_id}\n" for project_id in project_ids)
    return (
        "---\ntype: Collection\ntitle: Flanges\ndescription: How flanges are made.\n"
        "sources:\n  - id: s1\n    resource: aft://session/pi/def#entry-9\n"
        '    quote: "quoted words"\n'
        f"aft:\n  projects:\n{scope}---\n\n"
        f"## Decisions\n\n- {statement} [stated] 2026-09-20 [^s1]\n"
    )


def _a_digest(
    title: str = "A session",
    description: str = "Nothing about the word here.",
    heading: str = "Summary",
    prose: str = "The schedule holds for the quarter.",
) -> str:
    """A digest with one section, so it contributes exactly the row that section makes."""
    return (
        f"---\ntype: Session Digest\ntitle: {title}\ndescription: {description}\n---\n\n"
        f"## {heading}\n\n{prose}\n"
    )


def test_rows_carry_their_identity(initialized: Config) -> None:
    """Every row says where its text came from: path, project id, agent, session, time, and a
    heading or an entry id (BUILD-PLAN M3.1). Where a collection has no answer the field is
    empty rather than invented: a curated page is not one agent's record, and an evidence entry
    sits under no heading."""
    write(initialized, REL, PAGE)
    write(initialized, "digests/widget/2026/09/2026-09-28-claude-abc-123.md", DIGEST)
    write(initialized, "reports/2026-09-28.md", REPORT)
    evidence.store(initialized, "claude", "abc-123", "p_widget", entries("a flange"))
    index.update(initialized)

    page_row = row_for(initialized, "pages", f"{REL}#Decisions")
    assert _column(page_row, "path") == REL
    assert _column(page_row, "project_id") == "p_widget"
    assert _column(page_row, "heading") == "Decisions"
    assert (_column(page_row, "title"), _column(page_row, "description")) == (
        "widget",
        "Everything known about the widget.",
    )
    assert _column(page_row, "agent") is None
    assert _column(page_row, "session_id") is None
    assert datetime.fromisoformat(_column(page_row, "time")).tzinfo is UTC

    digest_row = row_for(
        initialized, "digests", "digests/widget/2026/09/2026-09-28-claude-abc-123.md#Summary"
    )
    assert _column(digest_row, "agent") == "claude"
    assert _column(digest_row, "session_id") == "abc-123", "a session id may hold a hyphen"

    report_row = row_for(initialized, "reports", "reports/2026-09-28.md#widget")
    assert _column(report_row, "heading") == "widget"
    assert _column(report_row, "project_id") is None

    evidence_row = row_for(initialized, "evidence", "abc-123#e1")
    assert _column(evidence_row, "entry_id") == "e1"
    assert _column(evidence_row, "path") == "aft://session/claude/abc-123"
    assert _column(evidence_row, "project_id") == "p_widget"
    assert _column(evidence_row, "heading") == ""
    assert _column(evidence_row, "entry_hash") is not None


def test_a_digest_and_an_archived_page_take_their_project_from_their_path(
    initialized: Config,
) -> None:
    """``digests/<slug>/`` and ``archive/<slug>.md`` name their project by the path they are
    filed under (DESIGN 6), so their rows carry the registry's id for that slug - which is
    what lets `aft search --project` leave the other projects' memory out. A slug the
    registry does not know is no error and gives no id, and a global page carries none:
    that row is every project's memory, not one whose project went missing."""
    widget = register(initialized, "widget")
    digest = write(initialized, "digests/widget/2026/09/2026-09-28-claude-abc.md", DIGEST)
    rested = write(initialized, "archive/widget.md", ARCHIVE)
    rested_mirrored = write(initialized, "archive/projects/widget/overview.md", ARCHIVE)
    unknown = write(initialized, "digests/gizmo/2026/09/2026-09-28-pi-def.md", DIGEST)
    every_project = write(initialized, "global/preferences.md", PREFERENCES)
    assert index.update(initialized) == []

    digest_row = row_for(initialized, "digests", f"{digest}#Summary")
    rested_row = row_for(initialized, "archive", f"{rested}#Decisions")
    rested_mirrored_row = row_for(initialized, "archive", f"{rested_mirrored}#Decisions")
    unknown_row = row_for(initialized, "digests", f"{unknown}#Summary")
    global_row = row_for(initialized, "pages", f"{every_project}#Preferences")
    assert _column(digest_row, "project_id") == widget.id
    assert _column(rested_row, "project_id") == widget.id
    assert _column(rested_mirrored_row, "project_id") == widget.id
    assert _column(unknown_row, "project_id") is None
    assert _column(global_row, "project_id") is None


def test_a_registry_that_will_not_parse_stops_the_run_rather_than_guessing(
    initialized: Config,
) -> None:
    """The registry is what turns a digest's or a rested page's path slug into a project id, so
    reading a broken one as an empty one would file every project's own memory as global
    memory. The run refuses instead and leaves no half-built index behind; `aft lint` is where
    the file itself gets named."""
    write(initialized, REL, PAGE)
    write(initialized, "digests/widget/2026/09/2026-09-28-claude-abc.md", DIGEST)
    write(initialized, "registry.yaml", "version: 1\nprojects: not-a-list\n")

    with pytest.raises(index.SearchIndexError, match="project registry.*projects must be a list"):
        index.update(initialized)

    assert rows(initialized, "digests") == []
    assert rows(initialized, "pages") == []


def test_moving_a_slug_in_the_registry_refiles_the_rows_it_derived(initialized: Config) -> None:
    """`aft project forget` leaves a project's pages where they are and a later `aft project add`
    issues a fresh id, so the slug a page sits under can end up naming a different project than
    the one its rows claim. The registry is an input those rows were derived from, so its bytes
    are recorded and a run with no file touched re-derives them: no row is left attributing
    memory to a project that no longer claims it."""
    forgotten = register(initialized, "widget")
    page = write(initialized, "projects/widget/index.md", _a_page())
    digest = write(initialized, "digests/widget/2026/09/2026-09-28-claude-abc.md", DIGEST)
    assert index.update(initialized) == []
    assert _column(row_for(initialized, "pages", f"{page}#Decisions"), "project_id") == (
        forgotten.id
    )

    registry = Registry.load(initialized.registry_path)
    registry.remove("widget")
    re_registered = registry.add("widget")
    registry.save()
    assert re_registered.id != forgotten.id

    assert index.update(initialized) == []

    assert _column(row_for(initialized, "pages", f"{page}#Decisions"), "project_id") == (
        re_registered.id
    )
    digest_row = row_for(initialized, "digests", f"{digest}#Summary")
    assert _column(digest_row, "project_id") == re_registered.id
    assert _column(digest_row, "body") == "The widget gained a flange."


def test_a_project_page_takes_its_directory_unless_the_frontmatter_names_another(
    initialized: Config,
) -> None:
    """A page under ``projects/<slug>/`` belongs to that project whether or not its frontmatter
    says so: the block can be absent, hand-typed, or written so the parser cannot read it, and
    the directory is what DESIGN 6 files the page in. The frontmatter still wins where it
    speaks, so the two cannot disagree silently - `aft lint` is what reports the difference."""
    widget = register(initialized, "widget")
    unnamed = write(initialized, "projects/widget/overview.md", _a_page())
    named = write(initialized, "projects/widget/decisions.md", _a_page(project="p_other"))
    index.update(initialized)

    assert _column(row_for(initialized, "pages", f"{unnamed}#Decisions"), "project_id") == (
        widget.id
    )
    assert _column(row_for(initialized, "pages", f"{named}#Decisions"), "project_id") == "p_other"


def test_a_page_that_covers_several_projects_is_a_row_for_each(initialized: Config) -> None:
    """A reference page is one collection of lines covering several projects at once
    (DESIGN 11), so it indexes as one row per project it applies to: each row carries that
    project's id and names it in its key, so a search filtered to one of them finds the page
    and the rows are never collapsed into one another or into the global pages."""
    ids = [register(initialized, "widget").id, register(initialized, "gizmo").id]
    rel = write(initialized, "references/flanges/collection.md", _a_scoped_page(ids))
    index.update(initialized)

    found = rows(initialized, "pages")
    assert sorted(_column(row, "project_id") for row in found) == sorted(ids)
    assert sorted(_column(row, "row_key") for row in found) == sorted(
        f"{rel}#{project_id}#Decisions" for project_id in ids
    )
    assert sorted(search(initialized, "pages", "cast")) == sorted(
        f"{rel}#{project_id}#Decisions" for project_id in ids
    )


def test_a_page_that_will_not_parse_is_reported_and_the_rebuild_still_comes(
    initialized: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """A page caught halfway through a save - or hand-typed without the closing rule - has
    frontmatter that will not parse. It is left out of the build and reported by its path and
    its reason rather than refusing the run, because `aft reindex` is the remedy for a bad
    index (DESIGN 14) and must stay available. It holds no rows and no hash, so the rebuild
    prints the same report and indexes the page once it is repaired."""
    good = write(initialized, REL, PAGE)
    broken = write(initialized, "projects/widget/broken.md", UNCLOSED_PAGE)

    assert index.update(initialized) == [
        f"{broken}: cannot be indexed: frontmatter has no closing ---"
    ]
    assert keys_of(initialized, "pages") == [f"{good}#Decisions", f"{good}#State"]
    assert broken not in hashes(initialized)

    assert main(["reindex"]) == 0

    printed = capsys.readouterr().out
    assert f"{broken}: cannot be indexed: frontmatter has no closing ---" in printed
    assert f"rebuilt the search index at {initialized.home / index.INDEX_NAME}" in printed
    assert keys_of(initialized, "pages") == [f"{good}#Decisions", f"{good}#State"]

    write(initialized, broken, _a_page(statement="The gasket is nitrile."))
    assert index.update(initialized) == []
    assert f"{broken}#Decisions" in keys_of(initialized, "pages")


def test_a_page_named_after_a_skipped_directory_is_still_indexed(initialized: Config) -> None:
    """``library``, ``.git`` and ``.obsidian`` are directories the walk keeps out of the index, and
    the directories a page sits in are what decide it: a page whose own file carries one of those
    names is a page like any other, with its rows in the collection, its hash recorded, and
    nothing about it named in the report."""
    named = write(initialized, "references/library.md", _a_page(title="library"))
    filed = write(initialized, "references/library/notes.md", _a_page(title="notes"))

    assert index.update(initialized) == []
    assert keys_of(initialized, "pages") == [f"{named}#Decisions"]
    assert named in hashes(initialized)
    assert filed not in hashes(initialized)


def test_unreadable_pages_are_reported_and_the_good_page_still_indexes(
    initialized: Config,
) -> None:
    """A dangling symlink and a page of bytes that are not UTF-8 are named in the report while
    every other page indexes: the files are the memory, but one broken file is no reason for
    search to lose them all. Neither records a hash, so both are read again on the next
    update, and a page that has been repaired indexes then."""
    good = write(initialized, REL, PAGE)
    dangling = "projects/widget/dangling.md"
    not_text = "projects/widget/not-text.md"
    (initialized.vault / "projects" / "widget").mkdir(parents=True, exist_ok=True)
    os.symlink(initialized.vault / "nowhere.md", initialized.vault / dangling)
    (initialized.vault / not_text).write_bytes(b"---\ntitle: \xff\xfe\n---\n\n## Facts\n\n")

    report = index.update(initialized)

    assert sorted(line.split(":")[0] for line in report) == sorted([dangling, not_text])
    assert any("cannot be read" in line for line in report)
    assert any("not UTF-8" in line for line in report)
    assert keys_of(initialized, "pages") == [f"{good}#Decisions", f"{good}#State"]
    assert dangling not in hashes(initialized) and not_text not in hashes(initialized)

    (initialized.vault / dangling).unlink()
    (initialized.vault / not_text).unlink()
    write(initialized, dangling, _a_page(statement="The gasket is nitrile."))

    assert index.update(initialized) == []
    assert f"{dangling}#Decisions" in keys_of(initialized, "pages")


def test_path_slug_handles_all_vault_layouts():
    from aft import vault

    assert vault.path_slug("projects/widget/overview.md") == "widget"
    assert vault.path_slug(Path("projects/widget/overview.md")) == "widget"
    assert vault.path_slug("projects/widget") == "widget"
    assert vault.path_slug("projects") is None
    assert vault.path_slug("digests/widget/2026/09/2026-09-30-session.md") == "widget"
    assert vault.path_slug("archive/projects/widget/overview.md") == "widget"
    assert vault.path_slug("archive/digests/widget/2026/09/2026-09-30-session.md") == "widget"
    assert vault.PROJECT_DIRS == index.PROJECT_DIRS
    assert vault.path_slug("archive/projects/widget") == "widget"
    assert vault.path_slug("archive/widget.md") == "widget"
    assert vault.path_slug("archive/index.md") is None
    assert vault.path_slug("archive/global/preferences.md") is None
    assert vault.path_slug("global/preferences.md") is None
    assert vault.path_slug("reports/needs-you.md") is None
    assert vault.path_slug("references/col/ref.md") is None
    assert vault.path_slug("") is None


def test_archived_resolved_notes_are_indexed_with_their_fact(initialized: Config) -> None:
    """F10: the archive collection indexes resolved notes with the fact they belonged to."""
    archive_text = """---
type: Archive
title: widget archive
description: Notes and lines resolved out of widget.
---

## Resolved notes

> - The GPU host runs vLLM. [stated] 2026-09-20 [^s1]
> [!question] 2026-10-01
> Is plain SQLite still the plan?
> Yes, plain SQLite is fast and simple.
"""
    write(initialized, "archive/projects/widget/overview.md", archive_text)
    index.update(initialized)

    assert "archive/projects/widget/overview.md#Resolved notes" in keys_of(initialized, "archive")
    body = _column(
        row_for(initialized, "archive", "archive/projects/widget/overview.md#Resolved notes"),
        "body",
    )
    assert "The GPU host runs vLLM." in body
    assert "Is plain SQLite still the plan?" in body
    assert "Yes, plain SQLite is fast and simple." in body

    # Search in archive matches answer, question, and fact
    assert search(initialized, "archive", "simple") == [
        "archive/projects/widget/overview.md#Resolved notes"
    ]
    assert search(initialized, "archive", "vLLM") == [
        "archive/projects/widget/overview.md#Resolved notes"
    ]
    assert search(initialized, "archive", "plan") == [
        "archive/projects/widget/overview.md#Resolved notes"
    ]


def test_archive_indexes_both_superseded_facts_and_resolved_notes(initialized: Config) -> None:
    """F10: an archive file with both resting fact lines and resolved notes indexes both."""
    archive_text = """---
type: Archive
title: widget archive
description: Notes and lines resolved out of widget.
---

## Decisions

- ~~Ship the widget in steel.~~ [stated] 2026-01-01 [^s1] superseded 2026-02-01 by [^s1]

## Resolved notes

> - The GPU host runs vLLM. [stated] 2026-09-20 [^s1]
> [!question] 2026-10-01
> Is plain SQLite still the plan?
> Yes, plain SQLite is fast and simple.
"""
    write(initialized, "archive/projects/widget/overview.md", archive_text)
    index.update(initialized)

    assert keys_of(initialized, "archive") == [
        "archive/projects/widget/overview.md#Decisions",
        "archive/projects/widget/overview.md#Resolved notes",
    ]
    decisions_body = _column(
        row_for(initialized, "archive", "archive/projects/widget/overview.md#Decisions"), "body"
    )
    assert "Ship the widget in steel." in decisions_body
    notes_body = _column(
        row_for(initialized, "archive", "archive/projects/widget/overview.md#Resolved notes"),
        "body",
    )
    assert "Yes, plain SQLite is fast and simple." in notes_body
