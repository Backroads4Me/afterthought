"""Every page writer operation round-trips through ``page.parse`` and ``lint``, and each
edit touches only its own lines: the rest of the page is compared byte for byte."""

from __future__ import annotations

from pathlib import Path

import pytest

from aft import lint, page, pagewrite
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
    return pagewrite.PageDoc.read(path)


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


def test_withdraw_refuses(initialized, doc):
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="cannot be struck through"):
        doc.withdraw(doc.fact("Long sessions need checkpoints."), "2026-10-04")
    with pytest.raises(pagewrite.WriteError, match="YYYY-MM-DD"):
        doc.withdraw(doc.fact("The GPU host runs vLLM."), "2026-13-01")
    assert doc.render() == before
    doc.supersede(
        doc.fact("The index is plain SQLite."),
        "The index is FTS5.",
        "stated",
        "2026-10-04",
        ("s2",),
    )
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="already out of memory"):
        doc.withdraw(doc.fact("The index is plain SQLite."), "2026-10-05")
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


def test_archive_superseded_removes_line_and_returns_content(initialized, doc):
    source = new_source(doc)
    old = doc.fact("The index is plain SQLite.")
    doc.supersede(old, "The index is SQLite FTS5.", "stated", "2026-10-04", (source,))
    struck = next(fact for fact in doc.page.facts if fact.state == page.SUPERSEDED)
    assert struck.statement == "The index is plain SQLite."
    line = doc.archive_superseded(struck)
    assert "~~The index is plain SQLite.~~" in line
    assert "superseded 2026-10-04 by" in line
    assert "The index is plain SQLite." not in doc.render()
    assert all(fact.statement != "The index is plain SQLite." for fact in doc.page.facts)


def test_archive_superseded_refuses_active_fact(doc):
    active = doc.fact("The GPU host runs vLLM.")
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="only a superseded line can be archived"):
        doc.archive_superseded(active)
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
