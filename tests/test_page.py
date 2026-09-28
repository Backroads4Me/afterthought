from __future__ import annotations

from pathlib import Path

from aft import page

VALID = """\
---
type: Project
title: widget
sources:
  - id: s1
    resource: aft://session/claude/abc#entry-1
    quote: "we will use SQLite"
  - id: s2
    resource: aft://session/pi/def#entry-9
    quote: "switched to FTS5"
---

## Decisions

- The index is SQLite FTS5. [stated] 2026-09-28 [^s2]
- ~~The index is plain SQLite.~~ [stated] 2026-09-20 [^s1] superseded 2026-09-28 by [^s2]
- ~~Use a vector store.~~ [suggested] 2026-09-19 [^s1] withdrawn 2026-09-29

## State

- The hook enqueues without a model call. [observed] 2026-09-30 [^s1] [^s2]
- Capture takes 12 ms. [observed] 2026-09-30 [^s2] (stale 2026-10-02: anchor no longer matches)

- Capture runs at session end. [stated] 2026-09-28 [^s1]

> [!conflict] 2026-10-01
> A session says capture should also run on every compaction.

- Dreaming commits to main. [stated] 2026-09-28 [^s1]
> [!question] 2026-10-01
> An observation contradicts this decision.

## Candidates

- Long sessions need checkpoint capture. [inferred] 2026-09-30 [^s2] (signals 2/3, sessions 1/2)
"""


def parse(text: str) -> page.Page:
    return page.parse(text, Path("projects/widget/overview.md"))


def test_valid_page_parses_every_state():
    result = parse(VALID)
    assert result.errors == []
    states = [(f.statement, f.state) for f in result.facts]
    assert states == [
        ("The index is SQLite FTS5.", page.ACTIVE),
        ("The index is plain SQLite.", page.SUPERSEDED),
        ("Use a vector store.", page.WITHDRAWN),
        ("The hook enqueues without a model call.", page.ACTIVE),
        ("Capture takes 12 ms.", page.STALE),
        ("Capture runs at session end.", page.CONFLICT),
        ("Dreaming commits to main.", page.NEEDS_YOU),
        ("Long sessions need checkpoint capture.", page.CANDIDATE),
    ]


def test_fact_details():
    facts = {f.statement: f for f in parse(VALID).facts}
    superseded = facts["The index is plain SQLite."]
    assert (superseded.kind, superseded.superseded_by, superseded.state_date) == (
        "decision",
        "s2",
        "2026-09-28",
    )
    assert facts["The hook enqueues without a model call."].refs == ("s1", "s2")
    candidate = facts["Long sessions need checkpoint capture."]
    assert (candidate.kind, candidate.signals, candidate.sessions) == (None, (2, 3), (1, 2))
    conflict = facts["Capture runs at session end."]
    assert conflict.callouts[0].text == [
        "A session says capture should also run on every compaction."
    ]


def test_line_numbers_count_frontmatter():
    first = parse(VALID).facts[0]
    assert VALID.split("\n")[first.line - 1].startswith("- The index is SQLite FTS5.")


def errors_for(body: str, page_type: str = "Project") -> list[str]:
    text = f"---\ntype: {page_type}\nsources: []\n---\n{body}"
    return [message for _, message in parse(text).errors]


def test_unknown_section():
    assert errors_for("## Musings\n- x [stated] 2026-01-01 [^a]\n") == ["unknown section 'Musings'"]


def test_contents_section_only_on_reference_pages():
    assert errors_for("## Contents\nAnything goes here.\n", "Reference") == []
    assert errors_for("## Contents\nAnything goes here.\n") == ["unknown section 'Contents'"]


def test_text_before_first_section():
    assert "before the first section" in errors_for("Some prose.\n## Facts\n")[0]


def test_prose_in_fact_section():
    assert "only fact lines" in errors_for("## Facts\nJust a sentence.\n")[0]


def test_missing_source_tag_or_footnote():
    assert "fact line must read" in errors_for("## Facts\n- A fact with no tag.\n")[0]
    assert "fact line must read" in errors_for("## Facts\n- A fact. [stated] 2026-01-01\n")[0]


def test_unknown_source_tag():
    assert errors_for("## Facts\n- A fact. [rumored] 2026-01-01 [^a]\n") == [
        "unknown source tag [rumored]"
    ]


def test_invalid_date():
    assert errors_for("## Facts\n- A fact. [stated] 2026-13-01 [^a]\n") == [
        "invalid date 2026-13-01"
    ]


def test_struck_fact_needs_a_reason():
    assert (
        "struck fact must end" in errors_for("## Facts\n- ~~Old.~~ [stated] 2026-01-01 [^a]\n")[0]
    )


def test_trailing_text_rejected():
    assert (
        "unrecognized text" in errors_for("## Facts\n- A fact. [stated] 2026-01-01 [^a] maybe\n")[0]
    )


def test_candidate_rules():
    struck = "## Candidates\n- ~~Old.~~ [inferred] 2026-01-01 [^a] withdrawn 2026-01-02\n"
    assert errors_for(struck) == ["a candidate cannot be struck through"]
    stale = "## Candidates\n- New. [inferred] 2026-01-01 [^a] (stale 2026-01-02: x)\n"
    assert errors_for(stale) == ["a candidate may end only with its signal counts"]


def test_callout_rules():
    orphan = "## Facts\n> [!conflict] 2026-01-01\n"
    assert errors_for(orphan) == ["a callout must follow the fact it is about"]
    bad = "## Facts\n- A. [stated] 2026-01-01 [^a]\n> [!note] 2026-01-01\n"
    assert "expected '> [!conflict]" in errors_for(bad)[0]
    struck = (
        "## Facts\n- ~~A.~~ [stated] 2026-01-01 [^a] withdrawn 2026-01-02\n"
        "> [!conflict] 2026-01-03\n"
    )
    assert errors_for(struck) == ["a callout cannot follow a withdrawn fact"]


def test_frontmatter_errors():
    assert page.split_frontmatter("---\ntype: [\n---\n")[3].startswith("frontmatter is not valid")
    assert page.split_frontmatter("---\n- a\n---\n")[3] == "frontmatter must be a mapping"
    assert page.split_frontmatter("---\ntype: x\n")[3] == "frontmatter has no closing ---"
    assert page.split_frontmatter("# no frontmatter\n")[0] is None
