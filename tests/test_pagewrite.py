"""Every page writer operation round-trips through ``page.parse`` and ``lint``, and each
edit touches only its own lines: the rest of the page is compared byte for byte."""

from __future__ import annotations

from pathlib import Path

import pytest

from aft import config as configmod
from aft import lint, page, pagewrite
from aft import vault as vaultmod
from aft.registry import Registry

REL = Path("projects/widget/overview.md")

PAGE = """\
---
type: Project
title: widget
description: A widget project.
sources:
  - id: s1
    resource: aft://session/claude/abc#entry-1
    title: Design session
    quote: "we will use SQLite"
  - id: s2
    resource: aft://session/pi/def#entry-9
    quote: "switched to FTS5"
generated: {by: "aft 0.1", at: 2026-09-28T16:02:00Z}
aft:
  project: {project_id}
  anchors:
    s1: {path: src/index.py, all_of: [sqlite3], commit: 4f2a91c}
---

# Widget

## Decisions

- The index is plain SQLite. [stated] 2026-09-20 [^s1]

## State

- The GPU host runs vLLM. [observed] 2026-09-30 [^s1]

## Candidates

- Long sessions need checkpoints. [inferred] 2026-09-30 [^s2] (signals 1/3, sessions 1/2)
"""

BROKEN_FM = PAGE.replace("title: widget", "title: [widget")

UNTERMINATED_FM = """\
---
type: Project
title: widget
description: A page whose frontmatter never closes.
sources: []

## Facts
"""

CANDIDATE = "- Long sessions need checkpoints. [inferred] 2026-09-30 [^s2]"

BARE = """\
---
type: Project
title: widget
description: A page with nothing on it yet.
sources: []
aft:
  project: {project_id}
---

## Facts
"""

NO_SOURCES = """\
---
type: Project
title: widget
description: A page written before it had sources.
status: stable
aft:
  project: {project_id}
---

## Facts
"""

NO_FACTS = """\
---
type: Project
title: widget
description: A page with a section and no facts on it.
sources:
  - id: s1
    resource: aft://session/claude/abc#entry-1
    quote: "we will use SQLite"
aft:
  project: {project_id}
---

## Facts
"""

INLINE_SOURCES = """\
---
type: Project
title: widget
description: A page whose sources list was typed by hand.
sources: [{id: s1, resource: aft://session/pi/def, quote: "switched to FTS5"}]
aft:
  project: {project_id}
---

## Facts
"""

NO_FRONTMATTER = """\
# Widget

## Facts
"""

BROKEN = PAGE.replace("## State\n", "## State\n\nA sentence, not a fact.\n")


def make(initialized, template: str) -> pagewrite.PageDoc:
    """Write a page into the vault as a registered project's overview page."""
    registry = Registry.load(initialized.registry_path)
    project = registry.by_slug("widget") or registry.add("widget")
    registry.save()
    path = initialized.vault / REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(template.replace("{project_id}", project.id))
    return pagewrite.PageDoc.read(path, vault=initialized.vault)


@pytest.fixture
def projects_vault(tmp_path, monkeypatch) -> configmod.Config:
    """A vault that itself sits under a directory named ``projects``, which is a layout
    anyone can pick: the config names any absolute path.
    """
    home = tmp_path / "home"
    home.mkdir()
    vault = tmp_path / "projects" / "afterthought"
    (home / configmod.CONFIG_NAME).write_text(f'vault = "{vault}"\nhost = "testhost"\n')
    monkeypatch.setenv(configmod.HOME_ENV, str(home))
    config = configmod.load()
    vaultmod.init(config)
    return config


@pytest.fixture
def doc(initialized) -> pagewrite.PageDoc:
    return make(initialized, PAGE)


def edits(before: str, after: str) -> tuple[tuple[int, ...], tuple[str, ...]]:
    """The one run of lines an edit touched: the 1-based lines of ``before`` it replaced,
    which is empty when the edit only inserted, and the lines that took their place.

    Whatever the writer changed lies inside that run: both texts are trimmed from both
    ends, so every line outside it is byte-identical in the two.
    """
    was, now = before.split("\n"), after.split("\n")
    start = 0
    while start < len(was) and start < len(now) and was[start] == now[start]:
        start += 1
    stop = 0
    while (
        stop < len(was) - start
        and stop < len(now) - start
        and was[len(was) - 1 - stop] == now[len(now) - 1 - stop]
    ):
        stop += 1
    return tuple(range(start + 1, len(was) - stop + 1)), tuple(now[start : len(now) - stop])


def without(text: str, line: int) -> str:
    """A page with one line cut out, for comparing everything else byte for byte."""
    lines = text.split("\n")
    return "\n".join(lines[: line - 1] + lines[line:])


def written(initialized, doc: pagewrite.PageDoc) -> page.Page:
    """Lint the rendered page as written to the vault, and return its parse."""
    doc.path.write_text(doc.render())
    assert [str(finding) for finding in lint.lint_vault(initialized.vault)] == []
    result = page.parse(doc.render(), REL)
    assert result.errors == []
    return result


def states(parsed: page.Page, section: str) -> list[tuple[str, str]]:
    return [(fact.statement, fact.state) for fact in parsed.facts if fact.section == section]


def test_an_untouched_page_renders_as_it_was_read():
    assert pagewrite.PageDoc(PAGE).render() == PAGE


def test_add_fact_appends_under_its_kind(initialized, doc):
    before = doc.render()
    doc.add_fact("decision", "Ship from a tag.", "stated", "2026-10-04", ("s1",))
    result = written(initialized, doc)
    assert edits(before, doc.render()) == (
        (),
        ("- Ship from a tag. [stated] 2026-10-04 [^s1]",),
    )
    assert states(result, "Decisions") == [
        ("The index is plain SQLite.", page.ACTIVE),
        ("Ship from a tag.", page.ACTIVE),
    ]


def test_add_fact_fills_an_empty_section(initialized):
    doc = make(initialized, NO_FACTS)
    before = doc.render()
    doc.add_fact("fact", "Amperage is offset encoded.", "observed", "2026-10-04", ("s1",))
    written(initialized, doc)
    line = "- Amperage is offset encoded. [observed] 2026-10-04 [^s1]"
    assert doc.render() == before.replace("## Facts\n", f"## Facts\n\n{line}\n")


def test_add_fact_creates_a_missing_section_where_design_orders_it(initialized, doc):
    before = doc.render()
    doc.add_fact(
        "lesson",
        "When the build breaks on ABI mismatch, pin the version.",
        "observed",
        "2026-10-02",
        ("s2",),
    )
    result = written(initialized, doc)
    assert edits(before, doc.render()) == (
        (),
        (
            "## Lessons",
            "",
            "- When the build breaks on ABI mismatch, pin the version. [observed] 2026-10-02 [^s2]",
            "",
        ),
    )
    assert result.sections == ["Decisions", "State", "Lessons", "Candidates"]


def test_add_fact_refuses_input_the_page_cannot_carry(doc):
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="add it first"):
        doc.add_fact("fact", "Unattested.", "observed", "2026-10-04", ("s9",))
    with pytest.raises(pagewrite.WriteError, match="unknown kind"):
        doc.add_fact("musing", "Idle thought.", "observed", "2026-10-04", ("s1",))
    with pytest.raises(pagewrite.WriteError, match="unknown source tag"):
        doc.add_fact("fact", "Hearsay.", "rumored", "2026-10-04", ("s1",))
    with pytest.raises(pagewrite.WriteError, match="YYYY-MM-DD"):
        doc.add_fact("fact", "Undated.", "observed", "last tuesday", ("s1",))
    with pytest.raises(pagewrite.WriteError, match="fact line must read"):
        doc.add_fact("fact", "Nothing cites it.", "observed", "2026-10-04", ())
    with pytest.raises(pagewrite.WriteError, match="fact line must read"):
        doc.add_candidate("Nothing cites it.", "inferred", "2026-10-04", ())
    assert doc.render() == before


def test_an_edit_that_would_not_parse_is_refused_and_undone(doc):
    before = doc.render()
    # A statement carrying a fact tail of its own leaves a line the grammar reads as a
    # fact with text running on after its sources, which only the re-parse can catch.
    with pytest.raises(pagewrite.WriteError, match="unrecognized text"):
        doc.add_fact(
            "fact",
            "Amperage is offset encoded. [stated] 2026-01-01 [^s1] maybe",
            "stated",
            "2026-10-04",
            ("s1",),
        )
    assert doc.render() == before


def test_a_statement_keeps_text_the_line_grammar_reads_as_a_tail(initialized, doc):
    """Only the round trip judges a statement: a footnote inside one is prose the line
    grammar carries, and one that names no source is refused before anything is written.
    """
    statement = "Trim the slug with [^s2] before hashing."
    before = doc.render()
    doc.add_fact("state", statement, "observed", "2026-10-04", ("s1",))
    written(initialized, doc)
    assert edits(before, doc.render()) == ((), (f"- {statement} [observed] 2026-10-04 [^s1]",))
    assert doc.fact(statement).refs == ("s1",)
    after = doc.render()
    with pytest.raises(pagewrite.WriteError, match=r"source a-z is not in the frontmatter"):
        doc.add_fact(
            "state", "Trim the slug with [^a-z] before hashing.", "observed", "2026-10-04", ("s1",)
        )
    assert doc.render() == after


def test_a_statement_ending_in_text_that_looks_like_a_tail_is_written_and_reads_back(
    initialized, doc
):
    """A statement is accepted by round trip: the written line parses back to it."""
    prose_tails = [
        "Bearing preload is set by hand. [stated] 2020-01-01",
        "Amperage is offset encoded, see [^s1]",
        "The old host ran vLLM. (stale 2026-10-01: anchor drifted)",
    ]
    before = doc.render()
    for statement in prose_tails:
        doc.add_fact("state", statement, "observed", "2026-10-04", ("s1",))
        written(initialized, doc)
        assert doc.fact(statement).statement == statement
    result = written(initialized, doc)
    assert states(result, "State") == [
        ("The GPU host runs vLLM.", page.ACTIVE),
        *[(statement, page.ACTIVE) for statement in prose_tails],
    ]
    assert edits(before, doc.render())[0] == ()


def test_a_candidate_statement_ending_in_a_look_alike_counts_suffix_stays_prose(initialized, doc):
    statement = "Compaction should trigger capture. (signals 9/9, sessions 9/9)"
    candidate = doc.add_candidate(statement, "inferred", "2026-10-04", ("s1",))
    written(initialized, doc)
    assert candidate.statement == statement
    assert candidate.signals == (1, pagewrite.SIGNALS_GATE)
    assert candidate.sessions == (1, pagewrite.SESSIONS_GATE)


def test_a_statement_keeps_markup_that_is_not_at_its_end(initialized, doc):
    statement = "The note [stated] 2020-01-01 in the middle stays prose, as does *this*."
    before = doc.render()
    doc.add_fact("state", statement, "observed", "2026-10-04", ("s1",))
    written(initialized, doc)
    assert edits(before, doc.render()) == ((), (f"- {statement} [observed] 2026-10-04 [^s1]",))
    assert doc.fact(statement).statement == statement


def test_add_source_refuses_an_entry_it_could_not_cite(doc):
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="needs a quote"):
        doc.add_source("aft://session/pi/ghi#entry-3")
    with pytest.raises(pagewrite.WriteError, match="needs a quote"):
        doc.add_source("aft://session/pi/ghi#entry-3", quote=" ")
    with pytest.raises(pagewrite.WriteError, match="title is text or nothing"):
        doc.add_source("aft://session/pi/ghi#entry-3", title=" ", quote="switched to FTS5")
    assert doc.render() == before


def test_add_source_appends_an_entry_and_leaves_the_rest_alone(initialized, doc):
    before = doc.render()
    source = doc.add_source(
        "aft://session/pi/ghi#entry-3",
        title="Capture hook test",
        quote="capture.py: SessionEnd handler enqueued the job in 12 ms",
    )
    assert source == "s3"
    written(initialized, doc)
    assert edits(before, doc.render()) == (
        (),
        (
            "  - id: s3",
            "    resource: aft://session/pi/ghi#entry-3",
            "    title: Capture hook test",
            '    quote: "capture.py: SessionEnd handler enqueued the job in 12 ms"',
        ),
    )


def test_add_source_fills_an_empty_sources_list(initialized):
    doc = make(initialized, BARE)
    before = doc.render()
    doc.add_source("aft://edit/4f2a91c", quote="a line Ted added by hand")
    written(initialized, doc)
    assert edits(before, doc.render()) == (
        (5,),
        (
            "sources:",
            "  - id: s1",
            "    resource: aft://edit/4f2a91c",
            "    quote: a line Ted added by hand",
        ),
    )


def test_add_source_adds_the_key_where_design_orders_it(initialized):
    doc = make(initialized, NO_SOURCES)
    before = doc.render()
    doc.add_source("aft://session/claude/xyz#entry-2", quote="release by tagging on main")
    written(initialized, doc)
    assert edits(before, doc.render()) == (
        (),
        (
            "sources:",
            "  - id: s1",
            "    resource: aft://session/claude/xyz#entry-2",
            "    quote: release by tagging on main",
        ),
    )
    assert doc.render().split("\n")[4:9] == [
        "sources:",
        "  - id: s1",
        "    resource: aft://session/claude/xyz#entry-2",
        "    quote: release by tagging on main",
        "status: stable",
    ]


def test_add_source_refuses_a_list_someone_typed_inline(initialized):
    doc = make(initialized, INLINE_SOURCES)
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="cannot append to an inline sources list"):
        doc.add_source("aft://edit/4f2a91c", quote="a line Ted added by hand")
    assert doc.render() == before


def test_add_source_refuses_a_page_with_no_frontmatter(initialized):
    doc = make(initialized, NO_FRONTMATTER)
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="no frontmatter to add a source to"):
        doc.add_source("aft://edit/4f2a91c", quote="a line Ted added by hand")
    assert doc.render() == before


def test_add_source_names_an_unterminated_frontmatter_as_unreadable(initialized):
    doc = make(initialized, UNTERMINATED_FM)
    before = doc.render()
    with pytest.raises(
        pagewrite.WriteError,
        match="sources are unreadable: frontmatter has no closing",
    ):
        doc.add_source("aft://edit/4f2a91c", quote="a line Ted added by hand")
    assert doc.render() == before


def test_a_source_value_ending_in_a_line_break_is_written_as_one_line(initialized, doc):
    """A quote or title off a transcript keeps its break: the value goes out quoted on
    a single line, so the frontmatter gains no stray blank line.
    """
    before = doc.render()
    new_id = doc.add_source(
        "aft://session/pi/ghi#entry-4",
        title="Capture hook test\n",
        quote="we will use SQLite\n",
    )
    assert new_id == "s3"
    written(initialized, doc)
    assert edits(before, doc.render()) == (
        (),
        (
            "  - id: s3",
            "    resource: aft://session/pi/ghi#entry-4",
            '    title: "Capture hook test\\n"',
            '    quote: "we will use SQLite\\n"',
        ),
    )
    assert doc.sources["s3"] == {
        "id": "s3",
        "resource": "aft://session/pi/ghi#entry-4",
        "title": "Capture hook test\n",
        "quote": "we will use SQLite\n",
    }


def test_a_source_value_with_an_interior_line_break_rounds_trip(initialized, doc):
    before = doc.render()
    doc.add_source("aft://edit/4f2a91c", quote="first line\nsecond line")
    written(initialized, doc)
    assert edits(before, doc.render()) == (
        (),
        (
            "  - id: s3",
            "    resource: aft://edit/4f2a91c",
            '    quote: "first line\\nsecond line"',
        ),
    )
    assert doc.sources["s3"]["quote"] == "first line\nsecond line"


def test_a_source_value_a_quoted_scalar_would_fold_is_written_escaped(initialized, doc):
    """YAML reads U+0085 inside a quoted scalar as a line break, so a value carrying it
    goes out with the code point escaped and comes back as the text it was given.
    """
    folded = "first\u0085second"
    before = doc.render()
    doc.add_source(folded, title=folded, quote=folded)
    written(initialized, doc)
    assert edits(before, doc.render()) == (
        (),
        (
            "  - id: s3",
            '    resource: "first\\u0085second"',
            '    title: "first\\u0085second"',
            '    quote: "first\\u0085second"',
        ),
    )
    assert doc.sources["s3"] == {
        "id": "s3",
        "resource": folded,
        "title": folded,
        "quote": folded,
    }


def test_a_hand_typed_tail_makes_every_operation_on_the_candidate_refuse(initialized):
    """A line the parse refused holds no fact: the writer names nothing where that line
    sits, and a handle taken before the tail was typed cannot be used to rebuild it.
    """
    counts = f"{CANDIDATE} (signals 1/3, sessions 1/2)"
    doc = make(initialized, PAGE)
    candidate = doc.fact("Long sessions need checkpoints.")
    doc.path.write_text(doc.render().replace(counts, f"{counts} pending review"))
    doc = pagewrite.PageDoc.read(doc.path)
    assert [message for _, message in doc.page.errors] == [
        "a candidate may end only with its signal counts"
    ]
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="no fact line"):
        doc.fact("Long sessions need checkpoints.")
    with pytest.raises(pagewrite.WriteError, match="moved"):
        doc.update_counts(candidate)
    with pytest.raises(pagewrite.WriteError, match="moved"):
        doc.promote(candidate, "state")
    assert doc.render() == before


def test_add_candidate_computes_its_gate_progress(initialized, doc):
    before = doc.render()
    doc.add_candidate(
        "Capture should also run on compaction.", "inferred", "2026-10-04", ("s1", "s2")
    )
    result = written(initialized, doc)
    assert edits(before, doc.render()) == (
        (),
        (
            "- Capture should also run on compaction. [inferred] 2026-10-04 [^s1] [^s2] "
            "(signals 2/3, sessions 2/2)",
        ),
    )
    assert states(result, "Candidates") == [
        ("Long sessions need checkpoints.", page.CANDIDATE),
        ("Capture should also run on compaction.", page.CANDIDATE),
    ]


def test_add_candidate_creates_the_candidates_section_last(initialized):
    doc = make(initialized, NO_FACTS)
    doc.add_candidate("Long sessions need checkpoints.", "inferred", "2026-10-04", ("s1",))
    result = written(initialized, doc)
    assert result.sections == ["Facts", "Candidates"]
    assert states(result, "Candidates") == [("Long sessions need checkpoints.", page.CANDIDATE)]
    assert states(result, "Facts") == []


def test_update_counts_writes_the_progress_the_footnotes_give(initialized, doc):
    stale_counts = PAGE.replace(f"{CANDIDATE} (signals 1/3", f"{CANDIDATE} [^s1] (signals 1/3")
    doc = make(initialized, stale_counts)
    before = doc.render()
    candidate = doc.fact("Long sessions need checkpoints.")
    assert doc.update_counts(candidate) is True
    written(initialized, doc)
    assert edits(before, doc.render()) == (
        (candidate.line,),
        (f"{CANDIDATE} [^s1] (signals 2/3, sessions 2/2)",),
    )


def test_update_counts_changes_nothing_when_the_line_already_reads_right(doc):
    before = doc.render()
    assert doc.update_counts(doc.fact("Long sessions need checkpoints.")) is False
    assert doc.render() == before


def test_add_source_refuses_a_frontmatter_the_parser_cannot_read(initialized):
    doc = make(initialized, BROKEN_FM)
    assert len(doc.page.errors) == 1
    assert doc.page.errors[0][1].startswith("frontmatter is not valid YAML")
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="sources are unreadable"):
        doc.add_source("aft://session/pi/ghi#entry-3", quote="switched to FTS5")
    assert doc.render() == before


def test_update_counts_refuses_sources_it_cannot_read(initialized):
    doc = make(initialized, BROKEN_FM)
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="sources are unreadable"):
        doc.update_counts(doc.fact("Long sessions need checkpoints."))
    assert doc.render() == before


def test_counts_for_gives_a_source_without_a_resource_no_session():
    sources = {
        "s1": {"id": "s1", "resource": "aft://session/pi/abc#entry-1", "quote": "q"},
        "s2": {"id": "s2", "quote": "q"},
    }
    assert pagewrite.counts_for(("s1", "s2"), sources) == (2, 1)


def test_promote_moves_a_candidate_to_its_kind_and_drops_its_progress(initialized, doc):
    before = doc.render()
    candidate = doc.fact("Long sessions need checkpoints.")
    moved = doc.promote(candidate, "state")
    result = written(initialized, doc)
    assert moved.line == doc.fact("The GPU host runs vLLM.").line + 1
    assert doc.render().split("\n")[moved.line - 1] == CANDIDATE
    assert states(result, "State") == [
        ("The GPU host runs vLLM.", page.ACTIVE),
        ("Long sessions need checkpoints.", page.ACTIVE),
    ]
    assert states(result, "Candidates") == []
    assert without(doc.render(), moved.line) == without(before, candidate.line)


def test_promote_creates_the_section_it_needs(initialized, doc):
    candidate = doc.fact("Long sessions need checkpoints.")
    doc.promote(candidate, "lesson")
    result = written(initialized, doc)
    assert result.sections == ["Decisions", "State", "Lessons", "Candidates"]
    assert states(result, "Lessons") == [("Long sessions need checkpoints.", page.ACTIVE)]
    assert states(result, "Candidates") == []


def test_promote_undoes_the_move_when_the_new_line_would_not_parse(initialized):
    # A hand-written candidate whose statement is blank parses, but there is no fact
    # line to write into the kind section, so the move has to be refused whole.
    blank_statement = PAGE.replace(CANDIDATE, "-   [inferred] 2026-09-30 [^s2]")
    doc = make(initialized, blank_statement)
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="fact line must read"):
        doc.promote(doc.fact(""), "state")
    assert doc.render() == before


def test_a_note_a_person_typed_under_a_candidate_stops_the_move(initialized):
    """A callout is text these operations do not own: a move that would change what it
    reads as is refused, and the note stays where it was typed. The row a person typed
    under the header belongs to that note, so the page reports this note once.
    """
    line = f"{CANDIDATE} (signals 1/3, sessions 1/2)"
    note = f"{line}\n\n> [!question] 2026-10-01\n> Did the capture hook replace this?"
    doc = make(initialized, PAGE.replace(line, note))
    assert [message for _, message in doc.page.errors] == [
        "a callout cannot follow a candidate fact",
    ]
    before = doc.render()
    with pytest.raises(
        pagewrite.WriteError, match="a callout marks the fact line directly above it"
    ):
        doc.promote(doc.fact("Long sessions need checkpoints."), "state")
    assert doc.render() == before


def test_an_append_lands_below_a_note_rather_than_under_it(initialized):
    """A note ends before the next fact: an append keeps the note attached to the fact
    above it and starts its own paragraph underneath, whichever operation appends.
    """
    state = "- The GPU host runs vLLM. [observed] 2026-09-30 [^s1]"
    note = "> [!question] 2026-10-01\n> why?"
    template = PAGE.replace(f"{state}\n", f"{state}\n\n{note}\n")

    added = "- The timer runs every 30 minutes. [observed] 2026-10-04 [^s1]"
    doc = make(initialized, template)
    before = doc.render()
    doc.add_fact("state", "The timer runs every 30 minutes.", "observed", "2026-10-04", ("s1",))
    result = written(initialized, doc)
    assert edits(before, doc.render()) == ((), (added, ""))
    assert f"{state}\n\n{note}\n\n{added}\n" in doc.render()
    noted = [(f.statement, [c.type for c in f.callouts]) for f in result.facts if f.callouts]
    assert noted == [("The GPU host runs vLLM.", ["question"])]

    doc = make(initialized, template)
    doc.promote(doc.fact("Long sessions need checkpoints."), "state")
    moved = "- Long sessions need checkpoints. [inferred] 2026-09-30 [^s2]"
    written(initialized, doc)
    assert f"{note}\n\n{moved}\n" in doc.render()


def test_a_page_that_arrives_broken_still_takes_its_edit(initialized):
    doc = make(initialized, BROKEN)
    before = doc.render()
    assert [message for _, message in doc.page.errors] == [
        "only fact lines are allowed in a fact section"
    ]
    doc.add_fact("state", "The timer runs every 30 minutes.", "observed", "2026-10-04", ("s1",))
    assert [message for _, message in doc.page.errors] == [
        "only fact lines are allowed in a fact section"
    ]
    assert edits(before, doc.render()) == (
        (),
        ("- The timer runs every 30 minutes. [observed] 2026-10-04 [^s1]",),
    )


def test_a_fact_read_before_an_edit_is_refused(initialized, doc):
    """A handle taken before an edit is refused, not applied to whatever line moved there."""
    candidate = doc.fact("Long sessions need checkpoints.")
    doc.add_fact("decision", "Ship from a tag.", "stated", "2026-10-04", ("s1",))
    with pytest.raises(pagewrite.WriteError, match="moved"):
        doc.update_counts(candidate)


def test_a_statement_on_two_lines_is_only_named_by_a_fact_handle(initialized, doc):
    """``fact()`` answers for one line or refuses: nothing stops duplicate statements."""
    doc.add_fact("state", "The GPU host runs vLLM.", "observed", "2026-10-04", ("s1",))
    written(initialized, doc)
    with pytest.raises(pagewrite.WriteError, match="2 lines read"):
        doc.fact("The GPU host runs vLLM.")


def test_a_handle_names_one_of_two_lines_sharing_a_statement(initialized, doc):
    """Two lines can carry one statement; a handle still rewrites the line it came from."""
    doc.add_candidate("Dup statement.", "inferred", "2026-09-30", ("s1",))
    second = doc.add_candidate("Dup statement.", "inferred", "2026-09-30", ("s1", "s2"))
    doc.promote(second, "decision")
    result = written(initialized, doc)
    decisions = [fact for fact in result.facts if fact.section == "Decisions"]
    assert states(result, "Decisions") == [
        ("The index is plain SQLite.", page.ACTIVE),
        ("Dup statement.", page.ACTIVE),
    ]
    assert decisions[-1].refs == ("s1", "s2")
    assert states(result, "Candidates") == [
        ("Long sessions need checkpoints.", page.CANDIDATE),
        ("Dup statement.", page.CANDIDATE),
    ]


def test_a_handle_shifted_onto_its_twin_is_refused(initialized, doc):
    """The line a handle names can come to hold its twin; that handle is refused the way
    a handle onto a moved line is, not quietly applied to the neighbour.
    """
    doc.add_candidate("Dup statement.", "inferred", "2026-09-30", ("s1",))
    second = doc.add_candidate("Dup statement.", "inferred", "2026-09-30", ("s1", "s2"))
    doc.add_fact("state", "The timer runs every 30 minutes.", "observed", "2026-10-04", ("s1",))
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="moved"):
        doc.promote(second, "decision")
    assert doc.render() == before


def test_a_struck_candidate_takes_no_edit_that_would_unstrike_it(initialized):
    """A candidate line struck by hand is a line the parse refuses, so it holds no fact:
    no operation can name it, and a handle taken before the strike cannot rewrite it.
    """
    struck = "- ~~Long sessions need checkpoints.~~ [inferred] 2026-09-30 [^s2]"
    doc = make(initialized, PAGE)
    candidate = doc.fact("Long sessions need checkpoints.")
    doc.path.write_text(doc.render().replace(CANDIDATE, f"{struck} (signals 1/3, sessions 1/2)"))
    doc = pagewrite.PageDoc.read(doc.path)
    assert [message for _, message in doc.page.errors] == ["a candidate cannot be struck through"]
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="no fact line"):
        doc.fact("Long sessions need checkpoints.")
    with pytest.raises(pagewrite.WriteError, match="moved"):
        doc.update_counts(candidate)
    with pytest.raises(pagewrite.WriteError, match="moved"):
        doc.promote(candidate, "state")
    assert doc.render() == before


def test_the_refusal_names_only_the_error_this_edit_would_add(initialized):
    """A line flagged elsewhere on the page is not this edit's loss to report: the refusal
    names what this edit would change and leaves the error already on the page alone.
    """
    doc = make(initialized, BROKEN)
    page_text = doc.render()
    assert [message for _, message in doc.page.errors] == [
        "only fact lines are allowed in a fact section"
    ]
    with pytest.raises(
        pagewrite.WriteError, match="unrecognized text after the sources"
    ) as refused:
        doc.add_fact(
            "state",
            "Amperage is offset encoded. [stated] 2026-01-01 [^s1] maybe",
            "observed",
            "2026-10-04",
            ("s1",),
        )
    assert "only fact lines" not in str(refused.value)
    assert doc.render() == page_text


def test_text_that_would_split_a_page_line_is_refused(doc):
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="a fact statement is one line"):
        doc.add_fact(
            "state",
            "the index\ris plain SQLite",
            "observed",
            "2026-10-05",
            ("s1",),
        )
    assert doc.render() == before


STRUCK = "- ~~The GPU host runs vLLM.~~ [observed] 2026-09-30 [^s1] withdrawn 2026-10-02"
STATE = "- The GPU host runs vLLM. [observed] 2026-09-30 [^s1]"
DECISION = "- The index is plain SQLite. [stated] 2026-09-20 [^s1]"
NOTE = "> [!conflict] 2026-10-01\n> A session says the host runs llama.cpp ([^s2])."


def new_source(doc: pagewrite.PageDoc) -> str:
    return doc.add_source("aft://session/claude/ghi#entry-4", quote="the host still runs vLLM")


def test_confirm_cites_the_source_and_carries_the_later_date(initialized, doc):
    source = new_source(doc)
    before = doc.render()
    fact = doc.fact("The GPU host runs vLLM.")
    assert doc.confirm(fact, source, "2026-10-04") is True
    result = written(initialized, doc)
    assert edits(before, doc.render()) == (
        (fact.line,),
        (f"- The GPU host runs vLLM. [observed] 2026-10-04 [^s1] [^{source}]",),
    )
    assert states(result, "State") == [("The GPU host runs vLLM.", page.ACTIVE)]


def test_confirm_with_older_evidence_keeps_the_line_date(initialized, doc):
    fact = doc.fact("The GPU host runs vLLM.")
    before = doc.render()
    assert doc.confirm(fact, "s2", "2026-09-01") is True
    written(initialized, doc)
    assert edits(before, doc.render()) == (
        (fact.line,),
        ("- The GPU host runs vLLM. [observed] 2026-09-30 [^s1] [^s2]",),
    )


def test_confirm_by_a_source_already_cited_changes_nothing_or_only_the_date(initialized, doc):
    before = doc.render()
    assert doc.confirm(doc.fact("The GPU host runs vLLM."), "s1", "2026-09-30") is False
    assert doc.render() == before
    assert doc.confirm(doc.fact("The GPU host runs vLLM."), "s1", "2026-10-02") is True
    written(initialized, doc)
    assert edits(before, doc.render())[1] == (
        "- The GPU host runs vLLM. [observed] 2026-10-02 [^s1]",
    )


def test_confirm_on_a_candidate_rewrites_its_gate_progress(initialized, doc):
    source = new_source(doc)
    candidate = doc.fact("Long sessions need checkpoints.")
    before = doc.render()
    assert doc.confirm(candidate, source, "2026-10-04") is True
    result = written(initialized, doc)
    assert edits(before, doc.render()) == (
        (candidate.line,),
        (
            f"- Long sessions need checkpoints. [inferred] 2026-10-04 [^s2] [^{source}] "
            "(signals 2/3, sessions 2/2)",
        ),
    )
    assert states(result, "Candidates") == [("Long sessions need checkpoints.", page.CANDIDATE)]


def test_confirm_leaves_the_note_under_the_line(initialized):
    doc = make(initialized, PAGE.replace(f"{STATE}\n", f"{STATE}\n\n{NOTE}\n"))
    before = doc.render()
    fact = doc.fact("The GPU host runs vLLM.")
    assert doc.confirm(fact, "s2", "2026-10-03") is True
    result = written(initialized, doc)
    assert edits(before, doc.render()) == (
        (fact.line,),
        ("- The GPU host runs vLLM. [observed] 2026-10-03 [^s1] [^s2]",),
    )
    assert states(result, "State") == [("The GPU host runs vLLM.", page.CONFLICT)]


def test_confirm_refuses(initialized, doc):
    fact = doc.fact("The GPU host runs vLLM.")
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="add it first"):
        doc.confirm(fact, "s9", "2026-10-04")
    with pytest.raises(pagewrite.WriteError, match="YYYY-MM-DD"):
        doc.confirm(fact, "s2", "yesterday")
    assert doc.render() == before
    doc.mark_stale(fact, "anchor words gone", "2026-10-04")
    stale = doc.fact("The GPU host runs vLLM.")
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="stale line is no longer memory"):
        doc.confirm(stale, "s2", "2026-10-05")
    doc.withdraw(stale, "2026-10-05")
    with pytest.raises(pagewrite.WriteError, match="withdrawn line is no longer memory"):
        doc.confirm(doc.fact("The GPU host runs vLLM."), "s2", "2026-10-05")
    old = doc.fact("The index is plain SQLite.")
    doc.supersede(old, "The index is SQLite FTS5.", "stated", "2026-10-05", ("s2",))
    with pytest.raises(pagewrite.WriteError, match="superseded line is no longer memory"):
        doc.confirm(doc.fact("The index is plain SQLite."), "s2", "2026-10-06")


def test_supersede_strikes_the_line_and_links_its_replacement_above_it(initialized, doc):
    source = new_source(doc)
    old = doc.fact("The index is plain SQLite.")
    before = doc.render()
    new = doc.supersede(old, "The index is SQLite FTS5.", "stated", "2026-10-04", (source,))
    result = written(initialized, doc)
    assert edits(before, doc.render()) == (
        (old.line,),
        (
            f"- The index is SQLite FTS5. [stated] 2026-10-04 [^{source}]",
            f"- ~~The index is plain SQLite.~~ [stated] 2026-09-20 [^s1] "
            f"superseded 2026-10-04 by [^{source}]",
        ),
    )
    assert new.line == old.line
    assert states(result, "Decisions") == [
        ("The index is SQLite FTS5.", page.ACTIVE),
        ("The index is plain SQLite.", page.SUPERSEDED),
    ]
    struck = next(fact for fact in result.facts if fact.state == page.SUPERSEDED)
    assert (struck.state_date, struck.superseded_by) == ("2026-10-04", source)


def test_supersede_links_by_the_footnote_it_is_given(initialized, doc):
    source = new_source(doc)
    old = doc.fact("The GPU host runs vLLM.")
    doc.supersede(
        old, "The GPU host runs SGLang.", "observed", "2026-10-04", ("s2", source), by=source
    )
    result = written(initialized, doc)
    struck = next(fact for fact in result.facts if fact.state == page.SUPERSEDED)
    assert struck.superseded_by == source
    assert doc.fact("The GPU host runs SGLang.").refs == ("s2", source)


def test_supersede_refuses(initialized, doc):
    old = doc.fact("The index is plain SQLite.")
    candidate = doc.fact("Long sessions need checkpoints.")
    before = doc.render()
    cases = [
        ((candidate, "X.", "stated", "2026-10-04", ("s2",)), {}, "never superseded"),
        ((old, "X.", "stated", "2026-10-04", ("s2",)), {"by": "s1"}, r"has to cite \[\^s1\]"),
        ((old, "X.", "stated", "2026-10-04", ()), {}, "needs a source"),
        ((old, "X.", "stated", "2026-10-04", ("s9",)), {}, "add it first"),
        ((old, "X.", "rumored", "2026-10-04", ("s2",)), {}, "unknown source tag"),
        ((old, "X.", "stated", "soon", ("s2",)), {}, "YYYY-MM-DD"),
        ((old, " ", "stated", "2026-10-04", ("s2",)), {}, "needs a statement"),
        ((old, "~~X~~", "stated", "2026-10-04", ("s2",)), {}, "cannot contain ~~"),
        ((old, "X [^s9].", "stated", "2026-10-04", ("s2",)), {}, "source s9"),
        (
            (old, "X. [stated] 2026-01-01 [^s1] maybe", "stated", "2026-10-04", ("s2",)),
            {},
            "unrecognized text",
        ),
    ]
    for args, kwargs, message in cases:
        with pytest.raises(pagewrite.WriteError, match=message):
            doc.supersede(*args, **kwargs)
        assert doc.render() == before


def test_supersede_refuses_a_line_that_does_not_stand_or_has_a_note(initialized):
    doc = make(initialized, PAGE.replace(f"{STATE}\n", f"{STATE}\n\n{NOTE}\n"))
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="this one is conflict"):
        doc.supersede(doc.fact("The GPU host runs vLLM."), "X.", "observed", "2026-10-04", ("s2",))
    assert doc.render() == before
    doc = make(initialized, PAGE)
    doc.mark_stale(doc.fact("The GPU host runs vLLM."), "anchor words gone", "2026-10-04")
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="this one is stale"):
        doc.supersede(doc.fact("The GPU host runs vLLM."), "X.", "observed", "2026-10-04", ("s2",))
    assert doc.render() == before


def test_withdraw_strikes_the_line_with_its_date(initialized, doc):
    fact = doc.fact("The GPU host runs vLLM.")
    before = doc.render()
    assert doc.withdraw(fact, "2026-10-04") is True
    result = written(initialized, doc)
    assert edits(before, doc.render()) == (
        (fact.line,),
        ("- ~~The GPU host runs vLLM.~~ [observed] 2026-09-30 [^s1] withdrawn 2026-10-04",),
    )
    assert states(result, "State") == [("The GPU host runs vLLM.", page.WITHDRAWN)]
    after = doc.render()
    assert doc.withdraw(doc.fact("The GPU host runs vLLM."), "2026-10-09") is False
    assert doc.render() == after


def test_withdraw_takes_a_stale_line_out_of_memory(initialized, doc):
    doc.mark_stale(doc.fact("The GPU host runs vLLM."), "anchor words gone", "2026-10-04")
    assert doc.withdraw(doc.fact("The GPU host runs vLLM."), "2026-10-05") is True
    result = written(initialized, doc)
    assert states(result, "State") == [("The GPU host runs vLLM.", page.WITHDRAWN)]


def test_withdraw_takes_a_superseded_line_out_of_memory(initialized, doc):
    """A struck line whose evidence is being forgotten is withdrawn too. Its ``superseded`` mark
    says a newer line replaced it, and that line stays above it on its own evidence; what is
    going is the old line's support, and DESIGN 12's answer to a memory line with none left is
    withdrawn - which is also what gets it out of the archive collection, built of superseded
    lines as that is (DESIGN 10)."""
    doc.supersede(
        doc.fact("The index is plain SQLite."),
        "The index is FTS5.",
        "stated",
        "2026-10-04",
        ("s2",),
    )
    old = doc.fact("The index is plain SQLite.")
    assert old.state == page.SUPERSEDED
    assert doc.withdraw(old, "2026-10-05") is True
    result = written(initialized, doc)
    assert states(result, "Decisions") == [
        ("The index is FTS5.", page.ACTIVE),
        ("The index is plain SQLite.", page.WITHDRAWN),
    ]
    (struck,) = [line for line in doc.render().splitlines() if "plain SQLite" in line]
    assert struck == (
        "- ~~The index is plain SQLite.~~ [stated] 2026-09-20 [^s1] withdrawn 2026-10-05"
    )
    # A line already withdrawn keeps the date it was withdrawn on rather than taking a new one.
    after = doc.render()
    assert doc.withdraw(doc.fact("The index is plain SQLite."), "2026-10-09") is False
    assert doc.render() == after


def test_withdraw_refuses(initialized, doc):
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="cannot be struck through"):
        doc.withdraw(doc.fact("Long sessions need checkpoints."), "2026-10-04")
    with pytest.raises(pagewrite.WriteError, match="YYYY-MM-DD"):
        doc.withdraw(doc.fact("The GPU host runs vLLM."), "2026-13-01")
    assert doc.render() == before
    doc = make(initialized, PAGE.replace(f"{STATE}\n", f"{STATE}\n\n{NOTE}\n"))
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="open note"):
        doc.withdraw(doc.fact("The GPU host runs vLLM."), "2026-10-04")
    assert doc.render() == before


def test_mark_stale_ends_the_line_with_its_date_and_reason(initialized, doc):
    fact = doc.fact("The GPU host runs vLLM.")
    before = doc.render()
    assert doc.mark_stale(fact, "anchor words gone from src/index.py", "2026-10-04") is True
    result = written(initialized, doc)
    assert edits(before, doc.render()) == (
        (fact.line,),
        (f"{STATE} (stale 2026-10-04: anchor words gone from src/index.py)",),
    )
    stale = doc.fact("The GPU host runs vLLM.")
    assert (stale.state, stale.state_date, stale.reason) == (
        page.STALE,
        "2026-10-04",
        "anchor words gone from src/index.py",
    )
    assert states(result, "State") == [("The GPU host runs vLLM.", page.STALE)]


def test_mark_stale_again_keeps_the_first_mark(doc):
    doc.mark_stale(doc.fact("The GPU host runs vLLM."), "anchor words gone", "2026-10-04")
    before = doc.render()
    assert doc.mark_stale(doc.fact("The GPU host runs vLLM."), "still gone", "2026-10-05") is False
    assert doc.render() == before


def test_mark_stale_refuses(initialized, doc):
    fact = doc.fact("The GPU host runs vLLM.")
    before = doc.render()
    cases = [
        ((fact, " ", "2026-10-04"), "needs a reason"),
        ((fact, "gone (again)", "2026-10-04"), "without '\\)'"),
        ((fact, "gone\nagain", "2026-10-04"), "one line"),
        ((fact, "see [^s9]", "2026-10-04"), "source s9"),
        ((fact, "gone", "2026-02-30"), "YYYY-MM-DD"),
        ((doc.fact("Long sessions need checkpoints."), "gone", "2026-10-04"), "signal counts"),
    ]
    for args, message in cases:
        with pytest.raises(pagewrite.WriteError, match=message):
            doc.mark_stale(*args)
        assert doc.render() == before
    doc.withdraw(fact, "2026-10-04")
    with pytest.raises(pagewrite.WriteError, match="this one is withdrawn"):
        doc.mark_stale(doc.fact("The GPU host runs vLLM."), "gone", "2026-10-05")
    doc = make(initialized, PAGE.replace(f"{STATE}\n", f"{STATE}\n\n{NOTE}\n"))
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="this one is conflict"):
        doc.mark_stale(doc.fact("The GPU host runs vLLM."), "gone", "2026-10-04")
    assert doc.render() == before


def test_a_stale_reason_may_cite_a_source_of_the_page(initialized, doc):
    doc.mark_stale(doc.fact("The GPU host runs vLLM."), "contradicted by [^s2]", "2026-10-04")
    written(initialized, doc)


def test_clear_stale_returns_the_line_to_active(initialized, doc):
    doc.mark_stale(doc.fact("The GPU host runs vLLM."), "anchor words gone", "2026-10-04")
    stale = doc.render()
    fact = doc.fact("The GPU host runs vLLM.")
    assert doc.clear_stale(fact, "2026-10-06") is True
    result = written(initialized, doc)
    assert edits(stale, doc.render()) == ((fact.line,), (STATE,))
    assert doc.fact("The GPU host runs vLLM.").state == page.ACTIVE
    assert states(result, "State") == [("The GPU host runs vLLM.", page.ACTIVE)]
    assert doc.render() == PAGE.replace("{project_id}", result.frontmatter["aft"]["project"])


def test_clear_stale_on_an_active_line_changes_nothing(doc):
    before = doc.render()
    assert doc.clear_stale(doc.fact("The GPU host runs vLLM."), "2026-10-06") is False
    assert doc.render() == before


def test_clear_stale_refuses(doc):
    doc.withdraw(doc.fact("The GPU host runs vLLM."), "2026-10-04")
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="this one is withdrawn"):
        doc.clear_stale(doc.fact("The GPU host runs vLLM."), "2026-10-06")
    with pytest.raises(pagewrite.WriteError, match="YYYY-MM-DD"):
        doc.clear_stale(doc.fact("The index is plain SQLite."), "someday")
    with pytest.raises(pagewrite.WriteError, match="this one is candidate"):
        doc.clear_stale(doc.fact("Long sessions need checkpoints."), "2026-10-06")
    assert doc.render() == before


def test_a_hand_typed_tail_makes_every_state_operation_refuse(initialized):
    """A line carrying text outside the grammar holds no fact, so a handle taken before the
    text was typed is refused by every operation and the page is left as it was."""
    doc = make(initialized, PAGE)
    fact = doc.fact("The GPU host runs vLLM.")
    doc.path.write_text(doc.render().replace(STATE, f"{STATE} checked by hand"))
    doc = pagewrite.PageDoc.read(doc.path)
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="no fact line"):
        doc.fact("The GPU host runs vLLM.")
    operations = [
        lambda: doc.confirm(fact, "s2", "2026-10-04"),
        lambda: doc.supersede(fact, "X.", "observed", "2026-10-04", ("s2",)),
        lambda: doc.withdraw(fact, "2026-10-04"),
        lambda: doc.mark_stale(fact, "gone", "2026-10-04"),
        lambda: doc.clear_stale(fact, "2026-10-04"),
    ]
    for operation in operations:
        with pytest.raises(pagewrite.WriteError, match="moved"):
            operation()
        assert doc.render() == before


def test_scrub_source_replaces_quote_and_title_with_marker(initialized):
    """Scrubbing a source replaces quote and title with the marker and preserves other sources."""
    page_text = (
        "---\n"
        "type: Project\n"
        "title: Demo\n"
        "sources:\n"
        "  - id: s1\n"
        "    resource: aft://session/claude/s-1#e1\n"
        '    title: "Session 1"\n'
        '    quote: "secret words 1"\n'
        "  - id: s2\n"
        "    resource: aft://session/claude/s-2#e2\n"
        '    quote: "keep words 2"\n'
        "aft:\n"
        "  project: p_demo\n"
        "---\n\n"
        "## Decisions\n\n"
        "- Decision 1. [stated] 2026-10-01 [^s1]\n"
    )
    doc = make(initialized, page_text)
    changed = doc.scrub_source("s1", "[forgotten]")
    assert changed is True
    assert doc.sources["s1"]["quote"] == "[forgotten]"
    assert doc.sources["s1"]["title"] == "[forgotten]"
    assert doc.sources["s2"]["quote"] == "keep words 2"

    assert doc.scrub_source("s1", "[forgotten]") is False

    with pytest.raises(pagewrite.WriteError, match="not in the frontmatter"):
        doc.scrub_source("s99")


def test_remove_candidate_removes_line_and_refuses_non_candidate_or_open_note(initialized):
    """Removing a candidate removes the line; non-candidates or lines with notes refuse."""
    page_text = (
        "---\n"
        "type: Project\n"
        "title: Demo\n"
        "sources:\n"
        "  - id: s1\n"
        "    resource: aft://session/claude/s-1#e1\n"
        '    quote: "words 1"\n'
        "aft:\n"
        "  project: p_demo\n"
        "---\n\n"
        "## Decisions\n\n"
        "- Standing fact. [stated] 2026-10-01 [^s1]\n\n"
        "## Candidates\n\n"
        "- Candidate to remove. [inferred] 2026-10-01 [^s1] (signals 1/3, sessions 1/2)\n"
        "- Candidate with note. [inferred] 2026-10-01 [^s1] (signals 1/3, sessions 1/2)\n"
        "> [!question] 2026-10-01\n"
        "> An open note on candidate.\n"
    )
    doc = make(initialized, page_text)
    with pytest.raises(pagewrite.WriteError, match="only a candidate line can be removed"):
        doc.remove_candidate(doc.fact("Standing fact."))

    with pytest.raises(pagewrite.WriteError, match="open note"):
        doc.remove_candidate(doc.fact("Candidate with note."))

    doc.remove_candidate(doc.fact("Candidate to remove."))
    assert "Candidate to remove." not in doc.render()
    assert "Candidate with note." in doc.render()


def archived(initialized, rel: Path = REL) -> list[str]:
    """The lines of the archive file this project's resolved notes went to (ruling 7943f9a)."""
    return (initialized.vault / "archive" / rel).read_text().split("\n")


def test_add_callout_puts_the_conflict_under_its_line(initialized, doc):
    line = doc.fact("The GPU host runs vLLM.")
    before = doc.render()
    assert doc.add_callout(line, "conflict", "A session says the host runs SGLang.", "2026-10-05")
    result = written(initialized, doc)
    assert edits(before, doc.render()) == (
        (),
        (
            "> [!conflict] 2026-10-05",
            "> A session says the host runs SGLang.",
            "",
        ),
    )
    assert states(result, "State") == [("The GPU host runs vLLM.", page.CONFLICT)]
    note = result.facts[1].callouts[0]
    assert (note.type, note.date, note.text) == (
        "conflict",
        "2026-10-05",
        ["A session says the host runs SGLang."],
    )
    # the blank line sits above the header, so the note renders as its own block
    assert doc.render().split("\n")[line.line] == ""


def test_a_callout_wraps_over_rows_as_design_writes_it(initialized, doc):
    """One note over several rows reads back as it was written, and the page reads back as
    the page that never had a note once the note is resolved.
    """
    text = (
        "A session says capture should also run on every compaction ([^s2]). The line above\n"
        "stands until stronger evidence arrives."
    )
    plain = doc.render()
    assert doc.add_callout(doc.fact("The GPU host runs vLLM."), "conflict", text, "2026-10-01")
    result = written(initialized, doc)
    assert states(result, "State") == [("The GPU host runs vLLM.", page.CONFLICT)]
    assert result.facts[1].callouts[0].text == [
        "A session says capture should also run on every compaction ([^s2]). The line above",
        "stands until stronger evidence arrives.",
    ]
    assert doc.resolve_callout(doc.fact("The GPU host runs vLLM.")) is True
    assert written(initialized, doc).facts[1].state == page.ACTIVE
    assert doc.render() == plain


def test_add_callout_refuses_a_line_struck_out_by_hand(initialized):
    doc = make(initialized, PAGE.replace(STATE, STRUCK))
    assert states(doc.page, "State") == [("The GPU host runs vLLM.", page.WITHDRAWN)]
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="cannot add a callout to a withdrawn line"):
        doc.add_callout(doc.fact("The GPU host runs vLLM."), "conflict", "A dispute.", "2026-10-05")
    assert doc.render() == before


def test_add_callout_refuses_a_candidate(initialized, doc):
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="cannot add a callout to a candidate line"):
        doc.add_callout(
            doc.fact("Long sessions need checkpoints."), "question", "Ask?", "2026-10-05"
        )
    assert doc.render() == before


def test_add_callout_refuses_to_write_over_text_typed_under_the_line(initialized):
    quoted = f"{STATE}\n> [!note] 2026-10-01\n> what I think about that"
    doc = make(initialized, PAGE.replace(STATE, quoted))
    assert [message for _, message in doc.page.errors] == [
        "expected '> [!conflict] YYYY-MM-DD' or '> [!question] ...'"
    ]
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match=r"> \[!note\] 2026-10-01 is already quote text"):
        doc.add_callout(doc.fact("The GPU host runs vLLM."), "conflict", "Ours.", "2026-10-05")
    assert doc.render() == before


def test_a_callout_citing_a_source_the_page_lacks_is_refused(initialized, doc):
    """A note is read next to its fact, so every citation in it resolves like the line's."""
    before = doc.render()
    for type, text in [
        ("conflict", "A session says otherwise, see [^s9]."),
        ("question", "Which is right?\n[^s9] is not a source of this page."),
    ]:
        with pytest.raises(pagewrite.WriteError, match="source s9 is not in the frontmatter"):
            doc.add_callout(doc.fact("The GPU host runs vLLM."), type, text, "2026-10-05")
    assert doc.render() == before


def test_add_callout_refuses_input_the_page_cannot_carry(doc):
    before = doc.render()
    line = doc.fact("The GPU host runs vLLM.")
    with pytest.raises(pagewrite.WriteError, match="unknown callout"):
        doc.add_callout(line, "note", "Not a state.", "2026-10-05")
    with pytest.raises(pagewrite.WriteError, match="needs text and no blank lines"):
        doc.add_callout(line, "question", "   ", "2026-10-05")
    with pytest.raises(pagewrite.WriteError, match="needs text and no blank lines"):
        doc.add_callout(line, "question", "one\n\ntwo", "2026-10-05")
    with pytest.raises(pagewrite.WriteError, match="a callout row is one line"):
        doc.add_callout(line, "conflict", "row one\rrow two", "2026-10-05")
    with pytest.raises(pagewrite.WriteError, match="YYYY-MM-DD"):
        doc.add_callout(line, "conflict", "Undated.", "last tuesday")
    assert doc.render() == before


def test_a_hand_typed_tail_leaves_the_line_out_of_reach_of_the_note_operations(initialized):
    """A line the parse refused holds no fact, so its note operations name nothing and a
    handle taken before the tail was typed cannot be used on it.
    """
    doc = make(initialized, PAGE)
    line = doc.fact("The GPU host runs vLLM.")
    doc.path.write_text(doc.render().replace(STATE, f"{STATE} pending review"))
    doc = pagewrite.PageDoc.read(doc.path, vault=doc.vault)
    assert [message for _, message in doc.page.errors] == [
        "unrecognized text after the sources: 'pending review'"
    ]
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="no fact line"):
        doc.fact("The GPU host runs vLLM.")
    with pytest.raises(pagewrite.WriteError, match="moved"):
        doc.add_callout(line, "conflict", "A dispute.", "2026-10-05")
    with pytest.raises(pagewrite.WriteError, match="moved"):
        doc.resolve_callout(line)
    assert doc.render() == before


def test_a_row_typed_into_an_open_note_is_never_rewritten_and_archives_verbatim(initialized):
    """A note and the rows under it are one object: no note operation rebuilds it, and
    closing it moves every row, the one a person typed included, to the archive.
    """
    noted = "\n".join(
        [
            STATE,
            "> [!conflict] 2026-10-01",
            "> A session says the host runs SGLang.",
            "> Ted: SGLang was only the benchmark box.",
        ]
    )
    doc = make(initialized, PAGE.replace(STATE, noted))
    disputed = doc.fact("The GPU host runs vLLM.")
    assert disputed.state == page.CONFLICT
    assert disputed.callouts[0].text == [
        "A session says the host runs SGLang.",
        "Ted: SGLang was only the benchmark box.",
    ]
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="cannot add a callout to a conflict line"):
        doc.add_callout(disputed, "question", "A second note.", "2026-10-06")
    assert doc.render() == before
    assert doc.resolve_callout(doc.fact("The GPU host runs vLLM.")) is True
    assert states(written(initialized, doc), "State") == [("The GPU host runs vLLM.", page.ACTIVE)]
    assert "> Ted: SGLang was only the benchmark box." in archived(initialized)


def test_resolve_callout_gives_the_line_back_and_archives_it_once(initialized, doc):
    plain = doc.render()
    doc.add_callout(doc.fact("The GPU host runs vLLM."), "question", "Does it still?", "2026-10-05")
    assert written(initialized, doc).facts[1].state == page.NEEDS_YOU
    before = doc.render()
    assert doc.resolve_callout(doc.fact("The GPU host runs vLLM.")) is True
    assert written(initialized, doc).facts[1].state == page.ACTIVE
    removed, added = edits(before, doc.render())
    assert added == () and len(removed) == 3
    assert doc.render() == plain  # the blank line that set the note apart went with it
    after = doc.render()
    with pytest.raises(pagewrite.WriteError, match="no callout to resolve"):
        doc.resolve_callout(doc.fact("The GPU host runs vLLM."))
    assert doc.render() == after
    assert archived(initialized).count("> [!question] 2026-10-05") == 1


def test_resolve_callout_archives_the_note_verbatim_with_the_line_it_was_about(initialized):
    """The note is never deleted: its header, every row and the line it was about go to the
    archive as they were read, the date travelling in the header.
    """
    note = [
        "> [!conflict] 2026-10-01",
        "> A session says the host runs SGLang.",
        "> Ted: SGLang was only the benchmark box.",
    ]
    doc = make(initialized, PAGE.replace(STATE, "\n".join([STATE, *note])))
    line = doc.fact("The GPU host runs vLLM.")
    before = doc.render()
    assert doc.resolve_callout(doc.fact("The GPU host runs vLLM.")) is True
    assert states(written(initialized, doc), "State") == [("The GPU host runs vLLM.", page.ACTIVE)]
    assert edits(before, doc.render()) == ((line.line + 1, line.line + 2, line.line + 3), ())
    assert archived(initialized) == [
        "---",
        "type: Archive",
        "title: widget archive",
        "description: Notes and lines resolved out of widget.",
        "---",
        "",
        "## Resolved notes",
        "",
        f"> {STATE}",
        *note,
        "",
    ]
    parsed_archive = page.parse(
        "\n".join(archived(initialized)), Path("archive/projects/widget/overview.md")
    )
    assert parsed_archive.errors == []


def test_resolving_two_notes_appends_both_blocks_to_the_archive(initialized, doc):
    index = doc.fact("The index is plain SQLite.")
    doc.add_callout(index, "question", "Is plain SQLite still the plan?", "2026-10-01")
    doc.add_callout(
        doc.fact("The GPU host runs vLLM."), "conflict", "A session says SGLang.", "2026-10-02"
    )
    assert doc.resolve_callout(doc.fact("The index is plain SQLite.")) is True
    assert doc.resolve_callout(doc.fact("The GPU host runs vLLM.")) is True
    written(initialized, doc)
    assert archived(initialized)[-9:] == [
        "",
        "> - The index is plain SQLite. [stated] 2026-09-20 [^s1]",
        "> [!question] 2026-10-01",
        "> Is plain SQLite still the plan?",
        "",
        f"> {STATE}",
        "> [!conflict] 2026-10-02",
        "> A session says SGLang.",
        "",
    ]


def test_a_note_on_a_global_page_archives_into_archive_global(initialized):
    """Ruling 7943f9a: archive path mirrors page path in vault, so global/preferences.md
    archives to archive/global/preferences.md.
    """
    noted = "\n".join([STATE, "> [!question] 2026-10-01", "> Does the host still run vLLM?"])
    pref_path = initialized.vault / "global/preferences.md"
    pref_path.parent.mkdir(parents=True, exist_ok=True)
    pref_path.write_text(PAGE.replace(STATE, noted).replace("{project_id}", "p1"))
    doc = pagewrite.PageDoc.read(pref_path, vault=initialized.vault)
    assert doc.resolve_callout(doc.fact("The GPU host runs vLLM.")) is True
    assert (initialized.vault / "archive/global/preferences.md").is_file()
    pref_archive = (initialized.vault / "archive/global/preferences.md").read_text()
    assert "> [!question] 2026-10-01" in pref_archive


def test_a_page_outside_the_vault_refuses_to_archive(initialized, tmp_path):
    """A page outside the vault has no vault-relative archive location and refuses."""
    outside = tmp_path / "outside.md"
    noted = "\n".join([STATE, "> [!question] 2026-10-01", "> Does the host still run vLLM?"])
    outside.write_text(PAGE.replace(STATE, noted).replace("{project_id}", "p1"))
    doc = pagewrite.PageDoc.read(outside, vault=initialized.vault)
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="lies outside vault"):
        doc.resolve_callout(doc.fact("The GPU host runs vLLM."))
    assert doc.render() == before


def test_text_typed_below_a_note_stays_on_the_page_when_the_note_closes(initialized):
    """A row belongs to the note only while the parse reads it as a note row: the note goes
    to the archive and the line a person typed plainly under it stays, still reported.
    """
    typed = "Answer: vLLM, restarted on Tuesday."
    noted = "\n".join([STATE, "> [!conflict] 2026-10-01", "> A session says SGLang.", typed])
    doc = make(initialized, PAGE.replace(STATE, noted))
    assert [message for _, message in doc.page.errors] == [
        "only fact lines are allowed in a fact section"
    ]
    assert doc.resolve_callout(doc.fact("The GPU host runs vLLM.")) is True
    doc.path.write_text(doc.render())
    assert [finding.message for finding in lint.lint_vault(initialized.vault)] == [
        "only fact lines are allowed in a fact section"
    ]
    assert typed in doc.render()
    assert archived(initialized)[-4:] == [
        f"> {STATE}",
        "> [!conflict] 2026-10-01",
        "> A session says SGLang.",
        "",
    ]
    assert typed not in archived(initialized)


def test_closing_a_note_under_an_answer_someone_typed_leaves_their_answer_alone(initialized):
    """The ruling protects text the tool did not write; it does not hold a close because
    the page reads badly while the note is open. The note goes to the archive, the answer
    keeps every byte on the page, and the page ends up reading as grammar.
    """
    ours = [STATE, "> [!conflict] 2026-10-01", "> A session says SGLang."]
    answer = ["", "> [!question] 2026-10-09", "> vLLM, restarted on Tuesday."]
    noted = "\n".join([STATE, "", "> [!conflict] 2026-10-01", "> A session says SGLang.", *answer])
    doc = make(initialized, PAGE.replace(STATE, noted))
    assert [message for _, message in doc.page.errors] == [
        "a callout cannot follow a conflict fact"
    ]
    line = doc.fact("The GPU host runs vLLM.")
    before = doc.render()
    assert doc.resolve_callout(doc.fact("The GPU host runs vLLM.")) is True
    result = written(initialized, doc)
    assert states(result, "State") == [("The GPU host runs vLLM.", page.NEEDS_YOU)]
    cut = without(without(before, line.line + 3), line.line + 2)
    assert doc.render() == without(cut, line.line + 1)
    assert archived(initialized)[-4:] == [f"> {STATE}", *ours[1:], ""]
    assert "restarted on Tuesday" not in "".join(archived(initialized))


def test_a_vault_under_a_projects_directory_archives_inside_itself(projects_vault):
    """The archive key comes from the page's place in the vault the configuration names,
    not from a directory named ``projects`` somewhere on the way to it.
    """
    noted = "\n".join([STATE, "> [!conflict] 2026-10-01", "> A session says SGLang."])
    doc = make(projects_vault, PAGE.replace(STATE, noted))
    assert doc.resolve_callout(doc.fact("The GPU host runs vLLM.")) is True
    assert states(written(projects_vault, doc), "State") == [
        ("The GPU host runs vLLM.", page.ACTIVE)
    ]
    assert archived(projects_vault, REL)[-4:] == [
        f"> {STATE}",
        "> [!conflict] 2026-10-01",
        "> A session says SGLang.",
        "",
    ]
    assert not (projects_vault.vault.parent / "archive").exists()


def test_an_archive_write_that_fails_leaves_page_and_archive_as_they_were(
    initialized, doc, monkeypatch
):
    """The page edit and the archive write are one step: a note is never left on the page
    and in the archive at once, nor filed in the archive and lost from the page.
    """
    doc.add_callout(
        doc.fact("The index is plain SQLite."), "question", "Still plain SQLite?", "2026-10-01"
    )
    assert doc.resolve_callout(doc.fact("The index is plain SQLite.")) is True
    doc.add_callout(
        doc.fact("The GPU host runs vLLM."), "conflict", "A session says SGLang.", "2026-10-02"
    )
    archive = initialized.vault / "archive" / REL
    was_archive, was_page = archive.read_bytes(), doc.render()
    write, failures = Path.write_bytes, iter([OSError("no space left on device")])

    def flaky(self, data, **kwargs):
        error = next(failures, None)
        if error is not None:
            raise error
        return write(self, data, **kwargs)

    monkeypatch.setattr(Path, "write_bytes", flaky)
    with pytest.raises(OSError, match="no space left on device"):
        doc.resolve_callout(doc.fact("The GPU host runs vLLM."))
    assert archive.read_bytes() == was_archive
    assert doc.render() == was_page
    assert page.parse(doc.render(), REL).facts[1].state == page.CONFLICT


def test_a_note_type_the_parser_grows_is_one_the_writer_writes(initialized, doc, monkeypatch):
    """The writer writes the note types the parser reads, from the one table design 6's
    fact state is defined by, so the two cannot drift apart.
    """
    monkeypatch.setitem(page.CALLOUTS, "watch", page.NEEDS_YOU)
    assert doc.add_callout(
        doc.fact("The GPU host runs vLLM."), "watch", "Keep an eye on this.", "2026-10-05"
    )
    result = written(initialized, doc)
    assert states(result, "State") == [("The GPU host runs vLLM.", page.NEEDS_YOU)]
    assert result.facts[1].callouts[0].type == "watch"


def test_an_archive_file_named_by_hand_lints_after_the_writer_appends(initialized, doc):
    """``archive/<slug>.md`` is a person-visible path, so the writer takes responsibility
    for one it did not make: their text stays and the file gets the frontmatter every
    page in the vault is linted for.
    """
    archive_file = initialized.vault / "archive" / REL
    archive_file.parent.mkdir(parents=True, exist_ok=True)
    archive_file.write_text("# Resolved notes\n\nNothing here yet.\n")
    assert doc.add_callout(doc.fact("The GPU host runs vLLM."), "question", "Still?", "2026-10-05")
    assert doc.resolve_callout(doc.fact("The GPU host runs vLLM.")) is True
    assert written(initialized, doc).facts[1].state == page.ACTIVE
    text = archive_file.read_text()
    assert text.startswith("---\ntype: Archive\n")
    assert "# Resolved notes\n\nNothing here yet." in text
    assert archived(initialized)[-3:] == ["> [!question] 2026-10-05", "> Still?", ""]


def test_an_archive_that_cannot_be_read_is_refused_with_its_path(initialized, doc):
    """Rather than append into a file whose own frontmatter does not read, the writer says
    which file it means and leaves both files alone.
    """
    archive = initialized.vault / "archive"
    filed = archive / REL
    filed.parent.mkdir(parents=True, exist_ok=True)
    assert doc.add_callout(doc.fact("The GPU host runs vLLM."), "question", "Still?", "2026-10-05")
    before = doc.render()
    for text in ["---\ntype: [Archive\n---\n", "---\ntype: Archive\n\n# Notes\n"]:
        filed.write_text(text)
        was = filed.read_bytes()
        with pytest.raises(pagewrite.WriteError, match="cannot be read") as caught:
            doc.resolve_callout(doc.fact("The GPU host runs vLLM."))
        assert str(filed) in str(caught.value)
        assert filed.read_bytes() == was
        assert doc.render() == before


def test_add_callout_writes_trailing_blank_when_line_below_is_non_blank(initialized):
    """When the line below the fact is non-blank (e.g. typed prose or another fact),
    add_callout writes a trailing blank line so typed text sitting below the fact is
    not absorbed into the note blockquote.
    """
    typed = "Some typed prose sitting right under the fact."
    doc = make(initialized, PAGE.replace(STATE, f"{STATE}\n{typed}"))
    fact = doc.fact("The GPU host runs vLLM.")
    assert doc.add_callout(fact, "conflict", "A session says SGLang.", "2026-10-05")

    lines = doc.render().splitlines()
    callout_idx = lines.index("> [!conflict] 2026-10-05")
    assert lines[callout_idx - 1] == ""  # blank line above callout
    assert lines[callout_idx + 1] == "> A session says SGLang."
    assert lines[callout_idx + 2] == ""  # trailing blank line written below callout
    assert lines[callout_idx + 3] == typed  # typed text is not absorbed into callout

    reparsed = page.parse(doc.render(), REL)
    note = reparsed.facts[1].callouts[0]
    assert note.text == ["A session says SGLang."]


def test_carried_off_narrowed_to_errors_on_removed_lines(initialized):
    """narrow carried_off: when carried_off=True, only errors on the removed lines are
    allowed to be carried off. An error outside the removed lines that disappears must still
    trigger WriteError.
    """
    doc = make(initialized, PAGE)
    doc.lines.insert(25, "## InvalidSection")
    doc._page = page.parse("\n".join(doc.lines), REL)
    assert any("unknown section 'InvalidSection'" in msg for _, msg in doc.page.errors)

    # Error outside the removed lines: if line 26's error disappears during an edit to lines 27..28
    doc.lines.pop(25)
    with pytest.raises(pagewrite.WriteError, match="this edit would drop text outside the grammar"):
        doc._apply(27, 28, [doc.lines[27]], carried_off=True)

    # Error on the removed lines: removing lines 25..26 with carried_off=True is permitted
    doc.lines.insert(25, "## InvalidSection")
    doc._page = page.parse("\n".join(doc.lines), REL)
    doc._apply(25, 26, [], carried_off=True)
    assert not any("unknown section 'InvalidSection'" in msg for _, msg in doc.page.errors)


def test_archive_slug_uses_vault_path_slug():
    """_archive_slug delegates to vault.path_slug, falling back to stem for non-project paths."""
    from aft.pagewrite import _archive_slug

    assert _archive_slug(Path("projects/widget/overview.md")) == "widget"
    assert _archive_slug(Path("archive/projects/widget/overview.md")) == "widget"
    assert _archive_slug(Path("archive/widget.md")) == "widget"
    assert _archive_slug(Path("global/preferences.md")) == "preferences"
    assert _archive_slug(Path("random/custom.md")) == "custom"


LIBRARY_CITED = """\
---
type: Project
title: widget
description: A page whose evidence includes a word from an imported document.
sources:
  - id: s1
    resource: aft://session/claude/abc#entry-1
    quote: "the controller runs at 250 kbps"
  - id: s2
    resource: aft://library/specimen/rvc_spec.md#entry-1
    quote: "the controller runs at 250 kbps"
aft:
  project: {project_id}
---

## Facts
"""


def test_the_counts_on_a_candidate_count_sessions_and_not_imported_copies(initialized):
    """The counts printed on a candidate are the gate's own numbers, so a quote taken from an
    imported document - a sentence no session spoke - shows on the page as the nothing R3 counts
    it toward (design 11 step 5)."""
    doc = make(initialized, LIBRARY_CITED)
    doc.add_candidate("The controller runs at 250 kbps.", "inferred", "2026-10-05", ("s1", "s2"))
    assert (
        "- The controller runs at 250 kbps. [inferred] 2026-10-05 [^s1] [^s2]"
        " (signals 1/3, sessions 1/2)"
    ) in doc.render()
    written(initialized, doc)
