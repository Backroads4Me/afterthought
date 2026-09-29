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


def fact_page(body: str, page_type: str = "Project") -> str:
    return f"---\ntype: {page_type}\nsources: []\n---\n{body}"


INJECTED = {page.ACTIVE, page.CONFLICT, page.NEEDS_YOU}


def actionable(result: page.Page) -> list[str]:
    """The statements a consumer that reads only `page.parse` would put before an agent.

    Injection shows active, conflict and needs-you facts and excludes candidates, stale,
    superseded and withdrawn lines (design section 10, BUILD-PLAN M1.2), so a line reaches
    memory exactly when it is in `facts` with one of those three states.
    """
    return [f.statement for f in result.facts if f.state in INJECTED]


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
    return [message for _, message in parse(fact_page(body, page_type)).errors]


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


def test_a_line_the_parser_reports_on_is_not_memory():
    refused = [
        # The hand-typed line that only missed the strikethrough.
        (
            "## Facts",
            "- The index is SQLite. [stated] 2026-01-01 [^s1] superseded 2026-01-02 by [^s2]",
            "unrecognized text after the sources",
        ),
        # Trailing text that leaves the stale marker unreadable.
        (
            "## Facts",
            "- Capture takes 12 ms. [observed] 2026-01-01 [^s1] (stale 2026-01-02: gone) more",
            "unrecognized text after the sources",
        ),
        # A struck line naming a state the parser does not know, and one naming none at all.
        ("## Facts", "- ~~Old.~~ [stated] 2026-01-01 [^s1] retired last year", "a struck fact"),
        ("## Facts", "- ~~Old.~~ [stated] 2026-01-01 [^s1]", "a struck fact"),
        # A line whose own fields are wrong.
        ("## Facts", "- A fact. [rumored] 2026-01-01 [^s1]", "unknown source tag"),
        ("## Facts", "- A fact. [stated] 2026-13-01 [^s1]", "invalid date"),
        # A candidate line the Candidates rules reject.
        (
            "## Candidates",
            "- ~~Old.~~ [inferred] 2026-01-01 [^s1] withdrawn 2026-01-02",
            "a candidate cannot be struck through",
        ),
        (
            "## Candidates",
            "- New. [inferred] 2026-01-01 [^s1] (stale 2026-01-02: x)",
            "a candidate may end only with its signal counts",
        ),
    ]
    for section, line, expected in refused:
        text = fact_page(f"{section}\n{line}\n")
        result = parse(text)
        assert len(result.errors) == 1, (line, result.errors)
        number, message = result.errors[0]
        assert text.split("\n")[number - 1] == line
        assert expected in message
        assert result.facts == []
        assert actionable(result) == []


def test_struck_fact_with_a_recognized_suffix_is_not_injected():
    body = (
        "## Decisions\n"
        "- ~~Use FTS5.~~ [stated] 2026-01-01 [^a] superseded 2026-01-02 by [^a]\n"
        "- ~~Use Redis.~~ [stated] 2026-01-01 [^a] withdrawn 2026-01-03\n"
    )
    result = parse(fact_page(body))
    assert result.errors == []
    assert [(f.statement, f.state, f.struck) for f in result.facts] == [
        ("Use FTS5.", page.SUPERSEDED, True),
        ("Use Redis.", page.WITHDRAWN, True),
    ]
    assert actionable(result) == []


def test_plain_fact_is_active_memory():
    result = parse(fact_page("## Facts\n- A fact. [stated] 2026-01-01 [^a]\n"))
    assert result.errors == []
    assert [(f.statement, f.state, f.struck) for f in result.facts] == [
        ("A fact.", page.ACTIVE, False)
    ]
    assert actionable(result) == ["A fact."]


def test_note_after_a_rejected_struck_line_has_no_fact_to_be_about():
    body = (
        "## Facts\n"
        "- A fact. [stated] 2026-01-01 [^a]\n"
        "- ~~Old.~~ [stated] 2026-01-01 [^a] retired\n"
        "> [!conflict] 2026-01-03\n"
    )
    result = parse(fact_page(body))
    assert "a callout must follow the fact it is about" in [message for _, message in result.errors]
    assert actionable(result) == ["A fact."]


def test_note_after_prose_in_a_fact_section_has_no_fact_to_be_about():
    body = (
        "## Facts\n"
        "- A fact. [stated] 2026-01-01 [^a]\n"
        "A sentence someone typed by hand.\n"
        "> [!conflict] 2026-01-03\n"
    )
    result = parse(fact_page(body))
    assert "a callout must follow the fact it is about" in [m for _, m in result.errors]
    assert actionable(result) == ["A fact."]


def test_a_note_with_an_invalid_date_writes_no_state():
    body = (
        "## Facts\n"
        "- A fact. [stated] 2026-01-01 [^a]\n"
        "> [!conflict] 2026-13-01\n"
        "> A session says otherwise.\n"
    )
    result = parse(fact_page(body))
    assert [message for _, message in result.errors] == [
        "invalid date 2026-13-01",
        "expected '> [!conflict] YYYY-MM-DD' or '> [!question] ...'",
    ]
    assert [(f.statement, f.state, f.callouts) for f in result.facts] == [
        ("A fact.", page.ACTIVE, [])
    ]
    assert actionable(result) == ["A fact."]


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
