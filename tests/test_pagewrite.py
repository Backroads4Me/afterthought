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
    """Only the round trip judges a statement: markup that looks like a tail is prose."""
    statement = "Trim the slug with [^a-z] before hashing."
    before = doc.render()
    doc.add_fact("state", statement, "observed", "2026-10-04", ("s1",))
    written(initialized, doc)
    assert edits(before, doc.render()) == ((), (f"- {statement} [observed] 2026-10-04 [^s1]",))
    assert doc.fact(statement).statement == statement


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


def test_a_hand_typed_tail_makes_every_operation_on_the_candidate_refuse(initialized):
    counts = f"{CANDIDATE} (signals 1/3, sessions 1/2)"
    doc = make(initialized, PAGE.replace(counts, f"{counts} pending review"))
    assert [message for _, message in doc.page.errors] == [
        "a candidate may end only with its signal counts"
    ]
    before = doc.render()
    candidate = doc.fact("Long sessions need checkpoints.")
    with pytest.raises(pagewrite.WriteError, match="text outside the grammar"):
        doc.update_counts(candidate)
    with pytest.raises(pagewrite.WriteError, match="text outside the grammar"):
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
    reads as is refused, and the note stays where it was typed.
    """
    line = f"{CANDIDATE} (signals 1/3, sessions 1/2)"
    note = f"{line}\n\n> [!question] 2026-10-01\n> Did the capture hook replace this?"
    doc = make(initialized, PAGE.replace(line, note))
    assert [message for _, message in doc.page.errors] == [
        "a callout cannot follow a candidate fact",
        "expected '> [!conflict] YYYY-MM-DD' or '> [!question] ...'",
    ]
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="a callout must follow the fact"):
        doc.promote(doc.fact("Long sessions need checkpoints."), "state")
    assert doc.render() == before


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


def test_a_struck_candidate_takes_no_edit_that_would_unstrike_it(initialized):
    struck = "- ~~Long sessions need checkpoints.~~ [inferred] 2026-09-30 [^s2]"
    doc = make(initialized, PAGE.replace(CANDIDATE, f"{struck} (signals 1/3, sessions 1/2)"))
    assert [message for _, message in doc.page.errors] == ["a candidate cannot be struck through"]
    before = doc.render()
    with pytest.raises(pagewrite.WriteError, match="cannot update the counts of a struck line"):
        doc.update_counts(doc.fact("Long sessions need checkpoints."))
    with pytest.raises(pagewrite.WriteError, match="cannot promote a struck line"):
        doc.promote(doc.fact("Long sessions need checkpoints."), "state")
    assert doc.render() == before


def test_the_refusal_names_only_the_line_the_edit_would_take_text_from(initialized):
    """A line flagged the same way elsewhere is not this edit's loss to report."""
    both = (
        "- Long sessions need checkpoints. [inferred] 2026-09-30 [^s2] pending review\n"
        "- The timer runs hourly. [observed] 2026-09-21 [^s1] pending review"
    )
    doc = make(initialized, PAGE.replace(CANDIDATE, both))
    page_text = doc.render()
    assert [message for _, message in doc.page.errors] == [
        "a candidate may end only with its signal counts",
        "a candidate may end only with its signal counts",
    ]
    first, second = doc.fact("Long sessions need checkpoints."), doc.fact("The timer runs hourly.")
    with pytest.raises(
        pagewrite.WriteError,
        match=f"line {first.line}: a candidate may end only",
    ) as refused:
        doc.update_counts(first)
    assert f"line {second.line}" not in str(refused.value)
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
