"""The rules ``aft.dream.rules`` checks (design 9 R1 to R6 and R9) and every cell of the
authority table, in both directions. R7's size caps and R8's anchors belong to M4.5's
maintenance, and no outcome here could name them.

A rule whose whole job is to refuse proves nothing tested only where it permits, so each
row of the table is tested as the supersede it allows and as the refusal, conflict or
needs-you it comes to when the evidence is weaker or older. Every outcome names the rule
behind it, and no test reads or writes a file: the existing lines come from a page text
parsed in memory, and the quotes, their footnotes, their entries and their dates come
from the context they are handed."""

from __future__ import annotations

import builtins
import datetime
import socket
from pathlib import Path

import pytest

from aft import page, pagewrite
from aft.dream import rules
from aft.dream.rules import Outcome, apply, check, downgrade, reject

PAGE_PATH = Path("projects/widget/overview.md")

RUN = "2026-10-05"
"""The day the run is on, which ``context`` carries in place of a clock."""

NEWER = "2026-10-04"
OLDER = "2026-09-10"
THIRTY_DAYS = "2026-09-05"
LONG_AGO = "2026-08-05"
YOUR_LINE_DATE = "2026-09-20"
OBSERVED_LINE_DATE = "2026-09-30"
"""Dates the tests name: the standing lines a supersede targets read 2026-09-20 when
they are yours and 2026-09-30 when a run observed them. Every supersede cell below
carries its own date, so which side is newer is written in the test rather than
inherited from a default."""

S1_RESOURCE = "aft://session/claude/2c1e9d0a#entry-148"
S2_RESOURCE = "aft://session/pi/01a0e256#entry-88"
S9_RESOURCE = "aft://session/pi/7777aaaa#entry-4"
"""The ``aft://`` entries the fixture's sources s1 and s2 name, and one no page cites."""

NO_RGB = "No RGB lighting."
SQLITE = "The index is plain SQLite."
VLLM = "The GPU host runs vLLM."
RVC = "RV-C amperage is offset-encoded."
ABI = "When the build fails on ABI mismatch, pin the version."
TAG_ON_MAIN = "Release by tagging on main after the gate."
DGN_2023 = "For DGN definitions use the 2023 edition."
JSON_INDEX = "Store the index as JSON files."
DGN_2016 = "For DGN definitions use the 2016 edition."
HOOK_ENQUEUES = "The capture hook enqueues and returns."
DIGEST_PER_CHUNK = "Digests run once per chunk."
WORKER_BUDGET = "Worker sessions are digested when the budget has room."
CHECKPOINTS = "Long sessions need checkpoint capture."
SESSION_END = "Capture runs at session end."
OFF_THE_RECORD = "Off the record spans survive compaction."
SGLANG = "The GPU host runs SGLang."

RESIN = "Print with the resin profile, not the default one."
SWEEP = "The queue sweeps on the timer."
NOTED_LINES = (DIGEST_PER_CHUNK, WORKER_BUDGET, RESIN)
"""Lines that stand with a note still open on them: one under a conflict, one under a
question, and one of the commitments that only your newer word may replace."""

TARGET_BY_KIND = {
    "preference": NO_RGB,
    "decision": SQLITE,
    "state": VLLM,
    "fact": RVC,
    "lesson": ABI,
    "procedure": TAG_ON_MAIN,
    "reference": DGN_2023,
}
"""One standing line of each kind, so every row of the authority table has a target."""

PAGE = (
    """\
---
type: Project
title: widget
description: A widget project.
sources:
  - id: s1
    resource: aft://session/claude/2c1e9d0a#entry-148
    title: Design session
    quote: "we will use SQLite, and no RGB lighting"
  - id: s2
    resource: aft://session/pi/01a0e256#entry-88
    title: Host audit
    quote: "the GPU host runs vLLM"
  - id: s3
    resource: aft://session/claude/9f8e7d6c#entry-12
    title: Capture review
    quote: "long orchestrator sessions need checkpoint capture"
aft:
  project: p_widget
---

# Widget

## Preferences

- """
    + NO_RGB
    + """ [stated] 2026-09-20 [^s1]

- """
    + RESIN
    + """ [stated] 2026-09-22 [^s1]

> [!question] 2026-10-03
> Does the resin profile cover the tough variants?

## Decisions

- """
    + SQLITE
    + """ [stated] 2026-09-20 [^s1]
- ~~"""
    + JSON_INDEX
    + """~~ [suggested] 2026-09-10 [^s2] superseded 2026-09-20 by [^s1]

## State

- """
    + VLLM
    + """ [observed] 2026-09-30 [^s2]
- """
    + HOOK_ENQUEUES
    + """ [observed] 2026-09-30 [^s2] (stale 2026-10-01: words gone)
- """
    + DIGEST_PER_CHUNK
    + """ [observed] 2026-09-29 [^s2]

> [!conflict] 2026-10-01
> A session says digests run per entry ([^s3]).

- """
    + WORKER_BUDGET
    + """ [observed] 2026-09-29 [^s2]

> [!question] 2026-10-02
> Does the budget count tokens or calls?

## Facts

- """
    + RVC
    + """ [observed] 2026-09-30 [^s2]

## Lessons

- """
    + ABI
    + """ [observed] 2026-09-30 [^s2]

## Procedures

- """
    + TAG_ON_MAIN
    + """ [stated] 2026-09-20 [^s1]

## References

- """
    + DGN_2023
    + """ [observed] 2026-09-30 [^s2]
- ~~"""
    + DGN_2016
    + """~~ [observed] 2026-08-02 [^s2] withdrawn 2026-09-01

## Candidates

- """
    + CHECKPOINTS
    + """ [inferred] 2026-09-30 [^s3] (signals 1/3, sessions 1/2)
- """
    + SWEEP
    + """ [inferred] 2026-09-29 [^s1] [^s2] (signals 2/3, sessions 2/2)
- """
    + SESSION_END
    + """ [stated] 2026-09-28 [^s1] (signals 1/3, sessions 1/2)
- """
    + OFF_THE_RECORD
    + """ [suggested] 2026-09-28 [^s3] (signals 1/3, sessions 1/2)
"""
)

STATE_PAGE = """\
---
type: Project
title: widget
description: A widget project.
aft:
  project: p_widget
---

# Widget

## State

"""

NOTED_LINE = (
    STATE_PAGE
    + f"- {DIGEST_PER_CHUNK} [observed] 2026-09-29 [^s2]\n"
    + "> [!conflict] 2026-10-01\n> A session says digests run per entry.\n"
)

STRUCK_UNDER_THE_NOTE = (
    STATE_PAGE
    + f"- ~~{DIGEST_PER_CHUNK}~~ [observed] 2026-09-29 [^s2] superseded 2026-10-04 by [^s2]\n"
    + "> [!conflict] 2026-10-01\n> A session says digests run per entry.\n"
)

SECOND_NOTE_ON_A_LINE = NOTED_LINE + (
    "\n> [!conflict] 2026-10-05\n> A second session says digests run per entry.\n"
)


def line(statement: str) -> page.Fact:
    """The standing line with this statement, read as the dream reads a page."""
    parsed = page.parse(PAGE, PAGE_PATH)
    assert not parsed.errors, parsed.errors
    found = [fact for fact in parsed.facts if fact.statement == statement]
    assert len(found) == 1, (statement, [fact.statement for fact in parsed.facts])
    return found[0]


def sig(
    session: str,
    date: str,
    *,
    source: str | None = None,
    resource: str | None = None,
    origin: str = rules.PRIMARY,
    verified: bool = True,
    yours: bool = True,
) -> rules.Evidence:
    """One quote the fact can cite, from one session on one date. Each quote carries a
    footnote id of its own unless the test names one, so a fact is counted as citing fewer
    sources than it has quotes only where that count is what the test is about."""
    return rules.Evidence(
        source=source or f"{session}-{date}",
        resource=resource or f"aft://session/pi/{session}#entry-1",
        origin=origin,
        date=date,
        verified=verified,
        yours=yours,
    )


def new_fact(**changes) -> rules.NewFact:
    """A candidate fact as a digest proposed it, with the fields a test cares about named."""
    fields: dict[str, object] = {
        "statement": SGLANG,
        "kind": "state",
        "source": "observed",
        "date": NEWER,
    }
    fields.update(changes)
    return rules.NewFact(**fields)  # type: ignore[arg-type]


def decide(action: str, line: str | None = "L1") -> rules.Decision:
    """One model answer (design 9 step 3)."""
    return rules.Decision(action=action, line=line, reason="the host changed")


def ctx(*evidence, **changes) -> rules.Context:
    """The whole of what the rules may know: a date, the quotes, and the two projects."""
    fields: dict[str, object] = {
        "today": RUN,
        "evidence": evidence,
        "run_project": "p_widget",
        "page_project": "p_widget",
    }
    fields.update(changes)
    return rules.Context(**fields)  # type: ignore[arg-type]


def gate() -> tuple[rules.Evidence, ...]:
    """Three signals across two primary sessions, all recent: what clears R3's gate."""
    return (sig("a", "2026-10-04"), sig("a", "2026-10-03"), sig("b", "2026-10-02"))


def worker_only() -> tuple[rules.Evidence, ...]:
    """Three signals across three sessions, none of them a primary session."""
    return (
        sig("w1", "2026-10-04", origin=rules.WORKER),
        sig("w2", "2026-10-03", origin=rules.WORKER),
        sig("w3", "2026-10-02", origin=rules.WORKER),
    )


# The page the rules act on, and the states it parses to.


def test_the_fixture_page_holds_the_states_the_rules_read():
    parsed = page.parse(PAGE, PAGE_PATH)
    assert not parsed.errors
    states = {fact.statement: fact.state for fact in parsed.facts}
    kinds = {fact.statement: fact.kind for fact in parsed.facts}
    assert states[VLLM] == page.ACTIVE and kinds[VLLM] == "state"
    assert states[NO_RGB] == page.ACTIVE and kinds[NO_RGB] == "preference"
    assert states[DIGEST_PER_CHUNK] == page.CONFLICT
    assert states[WORKER_BUDGET] == page.NEEDS_YOU
    assert states[RESIN] == page.NEEDS_YOU
    assert [callout.type for callout in line(RESIN).callouts] == ["question"]
    assert [callout.type for callout in line(DIGEST_PER_CHUNK).callouts] == ["conflict"]
    assert states[HOOK_ENQUEUES] == page.STALE
    assert states[JSON_INDEX] == page.SUPERSEDED
    assert states[DGN_2016] == page.WITHDRAWN
    candidate = line(CHECKPOINTS)
    assert candidate.state == page.CANDIDATE and candidate.kind is None
    assert candidate.signals == (1, 3) and candidate.sessions == (1, 2)
    cited = line(SWEEP)
    assert cited.refs == ("s1", "s2") and cited.signals == (2, 3)


# R9: an answer naming no action the decision schema allows reaches nothing.


def test_an_action_outside_the_five_the_schema_allows_is_refused():
    outcome = check(decide("rewrite"), new_fact(), None, ctx(sig("a", NEWER)))
    assert outcome == reject("R9")


def test_an_empty_action_is_refused_rather_than_read_as_skip():
    outcome = check(decide(""), new_fact(), None, ctx(sig("a", NEWER)))
    assert outcome == reject("R9")


def test_skip_writes_nothing_so_no_rule_is_consulted():
    outcome = check(decide("skip", line=None), new_fact(source="assumed"), None, ctx())
    assert outcome == apply("skip")


@pytest.mark.parametrize("action", ["confirm", "supersede", "conflict"])
def test_a_decision_that_names_no_line_edits_nothing(action):
    outcome = check(decide(action, line=None), new_fact(), None, ctx(sig("a", NEWER)))
    assert outcome == reject("M4.3 decision schema")


def test_adding_ignores_the_line_the_model_named_beside_it():
    """M4.3's schema allows a line on ``add``, and adding touches no existing line, so the
    name travels to the report rather than coming back as a refusal."""
    outcome = check(decide("add"), new_fact(source="stated"), line(VLLM), ctx(sig("a", NEWER)))
    assert outcome == apply("add")


# R1: one source tag, and at least one quote a script found in its source.


def test_a_fact_with_no_verified_quote_is_rejected():
    outcome = check(
        decide("add", line=None), new_fact(), None, ctx(sig("a", NEWER, verified=False))
    )
    assert outcome == reject("R1")


def test_a_fact_with_no_evidence_at_all_is_rejected():
    assert check(decide("add", line=None), new_fact(), None, ctx()) == reject("R1")


def test_one_verified_quote_among_unverified_ones_is_enough():
    outcome = check(
        decide("add", line=None),
        new_fact(),
        None,
        ctx(sig("a", NEWER, verified=False), sig("b", NEWER, verified=True)),
    )
    assert outcome == apply("add")


@pytest.mark.parametrize("source", ["assumed", "stated ", "", "STATED", "stated, observed"])
def test_an_unknown_source_tag_is_rejected(source):
    outcome = check(decide("add", line=None), new_fact(source=source), None, ctx(sig("a", NEWER)))
    assert outcome == reject("R1")


@pytest.mark.parametrize("source", list(page.SOURCES))
def test_each_source_tag_carries_a_line_of_its_own(source):
    """What a tag may *replace* is the authority table's business, tested cell by cell
    below; carrying a line of its own is open to every tag with proven evidence."""
    outcome = check(decide("add", line=None), new_fact(source=source), None, ctx(*gate()))
    assert outcome == apply("add")


# R2: [stated] means you committed to it.


def test_a_stated_fact_whose_quote_is_not_yours_is_rejected():
    outcome = check(
        decide("add", line=None),
        new_fact(source="stated"),
        None,
        ctx(sig("a", NEWER, yours=False)),
    )
    assert outcome == reject("R2")


def test_a_stated_quote_must_be_yours_and_verified():
    """A verified quote from elsewhere satisfies R1, and your own words satisfy R2 only
    when they are inside a verified quote."""
    outcome = check(
        decide("add", line=None),
        new_fact(source="stated"),
        None,
        ctx(
            sig("a", NEWER, verified=True, yours=False),
            sig("b", NEWER, verified=False, yours=True),
        ),
    )
    assert outcome == reject("R2")


def test_your_own_words_make_a_stated_fact_stand():
    outcome = check(decide("add", line=None), new_fact(source="stated"), None, ctx(sig("a", NEWER)))
    assert outcome == apply("add")


@pytest.mark.parametrize("source", ["observed", "inferred", "suggested"])
def test_only_a_stated_claim_has_to_be_yours(source):
    outcome = check(
        decide("add", line=None),
        new_fact(source=source),
        None,
        ctx(*gate(), sig("c", NEWER, yours=False)),
    )
    assert outcome == apply("add")


# R3: the recurrence gate, counted from the numbers in context and nothing else.


def test_an_inferred_fact_below_the_gate_stays_a_candidate():
    outcome = check(
        decide("add", line=None),
        new_fact(source="inferred"),
        None,
        ctx(sig("a", "2026-10-04"), sig("a", "2026-10-03")),
    )
    assert outcome == downgrade("candidate", "R3")


def test_three_signals_across_two_primary_sessions_clear_the_gate():
    outcome = check(decide("add", line=None), new_fact(source="inferred"), None, ctx(*gate()))
    assert outcome == apply("add")


def test_three_signals_in_one_primary_session_do_not_clear_the_gate():
    outcome = check(
        decide("add", line=None),
        new_fact(source="inferred"),
        None,
        ctx(sig("a", "2026-10-04"), sig("a", "2026-10-03"), sig("a", "2026-10-02")),
    )
    assert outcome == downgrade("candidate", "R3")


def test_two_signals_across_two_primary_sessions_do_not_clear_the_gate():
    outcome = check(
        decide("add", line=None),
        new_fact(source="inferred"),
        None,
        ctx(sig("a", "2026-10-04"), sig("b", "2026-10-03")),
    )
    assert outcome == downgrade("candidate", "R3")


def test_signals_older_than_thirty_days_count_half():
    """Five old signals across three primary sessions weigh 2.5, which is not the gate;
    six weigh 3.0, which is. Only the dates handed in decide it."""
    five = [
        sig("a", LONG_AGO),
        sig("a", "2026-08-06"),
        sig("b", LONG_AGO),
        sig("b", "2026-08-06"),
        sig("c", LONG_AGO),
    ]
    six = [*five, sig("c", "2026-08-06")]
    assert check(decide("add", line=None), new_fact(source="inferred"), None, ctx(*five)) == (
        downgrade("candidate", "R3")
    )
    assert check(decide("add", line=None), new_fact(source="inferred"), None, ctx(*six)) == apply(
        "add"
    )


def test_more_sessions_do_not_make_up_for_halved_signals():
    """Four signals across two primary sessions weigh 2.0 once every one of them is older
    than 30 days, so two sessions alone do not clear a gate the signals did not."""
    outcome = check(
        decide("add", line=None),
        new_fact(source="inferred"),
        None,
        ctx(
            sig("a", LONG_AGO),
            sig("a", "2026-08-06"),
            sig("b", LONG_AGO),
            sig("b", "2026-08-06"),
        ),
    )
    assert outcome == downgrade("candidate", "R3")


def test_a_signal_exactly_thirty_days_old_counts_whole():
    outcome = check(
        decide("add", line=None),
        new_fact(source="inferred"),
        None,
        ctx(sig("a", THIRTY_DAYS), sig("a", "2026-09-06"), sig("b", THIRTY_DAYS)),
    )
    assert outcome == apply("add")


def test_a_worker_signal_never_counts_toward_the_gate():
    """One primary signal survives of the three, so R3 holds the fact here rather than the
    worker rule: a primary session did speak about it."""
    outcome = check(
        decide("add", line=None),
        new_fact(source="inferred"),
        None,
        ctx(
            sig("a", "2026-10-04"),
            sig("w1", "2026-10-03", origin=rules.WORKER),
            sig("w2", "2026-10-02", origin=rules.WORKER),
        ),
    )
    assert outcome == downgrade("candidate", "R3")


def test_an_unverified_quote_is_no_signal_at_all():
    outcome = check(
        decide("add", line=None),
        new_fact(source="inferred"),
        None,
        ctx(
            sig("a", "2026-10-04"),
            sig("a", "2026-10-03"),
            sig("b", "2026-10-02", verified=False),
        ),
    )
    assert outcome == downgrade("candidate", "R3")


def test_an_explicit_correction_from_you_skips_the_gate():
    outcome = check(
        decide("add", line=None),
        new_fact(source="stated", explicit_correction=True),
        None,
        ctx(sig("a", "2026-10-04")),
    )
    assert outcome == apply("add")


def test_a_suggested_fact_is_held_to_the_same_gate_as_an_inferred_one():
    """R3 names the gate for ``[inferred]``; firstmate ruled on 2026-09-29 that the same bar
    holds a ``[suggested]`` fact, so the weaker tag gets no easier path out of
    Candidates."""
    outcome = check(
        decide("add", line=None),
        new_fact(source="suggested"),
        None,
        ctx(sig("a", "2026-10-04")),
    )
    assert outcome == downgrade("candidate", "R3")


def test_a_suggested_fact_over_the_gate_is_added():
    outcome = check(decide("add", line=None), new_fact(source="suggested"), None, ctx(*gate()))
    assert outcome == apply("add")


@pytest.mark.parametrize("source", ["stated", "observed"])
def test_what_you_said_or_a_run_observed_needs_no_gate(source):
    outcome = check(
        decide("add", line=None), new_fact(source=source), None, ctx(sig("a", "2026-10-04"))
    )
    assert outcome == apply("add")


def test_two_quotes_citing_one_source_are_one_signal():
    """The gate counts what the writer counts: one signal per distinct footnote and one
    session per distinct session among them. Four quotes describing two sources across two
    sessions do not clear a gate that names three signals."""
    quotes = (
        sig("a", "2026-10-04", source="s1", resource=S1_RESOURCE),
        sig("a", "2026-10-03", source="s1", resource=S1_RESOURCE),
        sig("b", "2026-10-02", source="s2", resource=S2_RESOURCE),
        sig("b", "2026-10-01", source="s2", resource=S2_RESOURCE),
    )
    assert pagewrite.counts_for(
        [quote.source for quote in quotes],
        {quote.source: {"resource": quote.resource} for quote in quotes},
    ) == (2, 2)
    outcome = check(
        decide("add", line=None),
        new_fact(source="inferred"),
        None,
        ctx(*quotes),
    )
    assert outcome == downgrade("candidate", "R3")


def test_a_candidate_citing_two_sources_leaves_on_one_more_corroborating_source():
    """The gate asks about the line as the edit leaves it, so the footnotes it already
    carries count beside the new quote, and those are the numbers update_counts prints."""
    outcome = check(
        decide("confirm"),
        new_fact(source="inferred"),
        line(SWEEP),
        ctx(
            sig("s9", "2026-10-02", source="s9", resource=S9_RESOURCE),
            line_evidence=(
                sig("s1", "2026-10-04", source="s1", resource=S1_RESOURCE),
                sig("s2", "2026-10-03", source="s2", resource=S2_RESOURCE),
            ),
        ),
    )
    assert outcome == apply("promote")


def test_a_caller_that_hands_in_only_the_new_quote_leaves_the_line_a_candidate():
    """The line's own quotes are what carry the sessions behind its footnotes: without
    them the count under-reads and the gate holds rather than guesses, which is what
    ``Context.line_evidence`` asks the caller for."""
    outcome = check(
        decide("confirm"),
        new_fact(source="inferred"),
        line(SWEEP),
        ctx(sig("s9", "2026-10-02", source="s9", resource=S9_RESOURCE)),
    )
    assert outcome == apply("confirm")


# Design 8's worker rule: a fact of a lower rank stays a candidate.


@pytest.mark.parametrize("source", list(page.SOURCES))
def test_a_worker_only_fact_stays_a_candidate_whatever_its_tag(source):
    outcome = check(decide("add", line=None), new_fact(source=source), None, ctx(*worker_only()))
    assert outcome == downgrade("candidate", "design 8 worker sessions")


def test_a_worker_only_fact_replaces_nothing():
    outcome = check(
        decide("supersede"),
        new_fact(source="stated"),
        line(VLLM),
        ctx(sig("w1", NEWER, origin=rules.WORKER)),
    )
    assert outcome == downgrade("candidate", "design 8 worker sessions")


def test_a_worker_only_fact_marks_no_line_in_conflict():
    outcome = check(
        decide("conflict"),
        new_fact(source="observed"),
        line(VLLM),
        ctx(sig("w1", NEWER, origin=rules.WORKER)),
    )
    assert outcome == downgrade("candidate", "design 8 worker sessions")


def test_a_worker_only_fact_never_confirms_a_standing_line():
    """Worker evidence adds no source and no date to a line: the confirm is refused and
    reported (design 8)."""
    outcome = check(
        decide("confirm"),
        new_fact(source="observed"),
        line(VLLM),
        ctx(sig("w1", NEWER, origin=rules.WORKER)),
    )
    assert outcome == reject("design 8 worker sessions")


def test_one_primary_session_corroborating_a_worker_fact_lifts_the_lower_rank():
    outcome = check(
        decide("add", line=None),
        new_fact(source="observed"),
        None,
        ctx(sig("w1", "2026-10-04", origin=rules.WORKER), sig("a", "2026-10-03")),
    )
    assert outcome == apply("add")


# R4: the authority to replace a line, cell by cell, with evidence newer than the line's.

SUPERSEDE_CELLS = [
    ("preference", "stated", apply("supersede")),
    ("preference", "observed", downgrade("needs-you", "R4")),
    ("preference", "inferred", downgrade("conflict", "R4")),
    ("preference", "suggested", downgrade("conflict", "R4")),
    ("decision", "stated", apply("supersede")),
    ("decision", "observed", downgrade("needs-you", "R4")),
    ("decision", "inferred", downgrade("conflict", "R4")),
    ("decision", "suggested", downgrade("conflict", "R4")),
    ("state", "stated", apply("supersede")),
    ("state", "observed", apply("supersede")),
    ("state", "inferred", downgrade("conflict", "R4")),
    ("state", "suggested", downgrade("conflict", "R4")),
    ("fact", "stated", apply("supersede")),
    ("fact", "observed", apply("supersede")),
    ("fact", "inferred", downgrade("conflict", "R4")),
    ("fact", "suggested", downgrade("conflict", "R4")),
    ("lesson", "stated", apply("supersede")),
    ("lesson", "observed", apply("supersede")),
    ("lesson", "inferred", downgrade("conflict", "R4")),
    ("lesson", "suggested", downgrade("conflict", "R4")),
    ("procedure", "stated", apply("supersede")),
    ("procedure", "observed", apply("supersede")),
    ("procedure", "inferred", downgrade("conflict", "R4")),
    ("procedure", "suggested", downgrade("conflict", "R4")),
    ("reference", "stated", apply("supersede")),
    ("reference", "observed", apply("supersede")),
    ("reference", "inferred", downgrade("conflict", "R4")),
    ("reference", "suggested", downgrade("conflict", "R4")),
]

NOT_NEWER_CELLS = [
    ("preference", "stated", downgrade("conflict", "R4")),
    ("preference", "observed", downgrade("needs-you", "R4")),
    ("decision", "stated", downgrade("conflict", "R4")),
    ("decision", "observed", downgrade("needs-you", "R4")),
    ("state", "stated", downgrade("conflict", "R4")),
    ("state", "observed", downgrade("conflict", "R4")),
    ("fact", "stated", downgrade("conflict", "R4")),
    ("fact", "observed", downgrade("conflict", "R4")),
    ("lesson", "stated", downgrade("conflict", "R4")),
    ("lesson", "observed", downgrade("conflict", "R4")),
    ("procedure", "stated", downgrade("conflict", "R4")),
    ("procedure", "observed", downgrade("conflict", "R4")),
    ("reference", "stated", downgrade("conflict", "R4")),
    ("reference", "observed", downgrade("conflict", "R4")),
]


def cell_ids(cells: list[tuple[str, str, Outcome]]) -> list[str]:
    """One id per cell of the table: the kind, the tag, and what the outcome became."""
    return [
        f"{kind} by {tag} to {e.action if isinstance(e, rules.Apply) else e.to_action}"
        for kind, tag, e in cells
    ]


@pytest.mark.parametrize(
    ("kind", "tag", "expected"), SUPERSEDE_CELLS, ids=cell_ids(SUPERSEDE_CELLS)
)
def test_each_cell_of_the_authority_table(kind: str, tag: str, expected: Outcome) -> None:
    """Design 9's table read as cells: the kind of the line the new fact would replace,
    the source tag of the new fact, and what a supersede by newer evidence comes to."""
    outcome = check(
        decide("supersede"),
        new_fact(source=tag, date=NEWER),
        line(TARGET_BY_KIND[kind]),
        ctx(sig("a", NEWER)),
    )
    assert outcome == expected


@pytest.mark.parametrize(
    ("kind", "tag", "expected"), NOT_NEWER_CELLS, ids=cell_ids(NOT_NEWER_CELLS)
)
def test_no_row_of_the_table_replaces_a_line_without_newer_evidence(
    kind: str, tag: str, expected: Outcome
) -> None:
    """The other half of every permitting row: the authority the table grants is worth
    nothing until the quote is newer than the line's latest evidence."""
    outcome = check(
        decide("supersede"),
        new_fact(source=tag, date=OLDER),
        line(TARGET_BY_KIND[kind]),
        ctx(sig("a", OLDER)),
    )
    assert outcome == expected


@pytest.mark.parametrize(
    ("statement", "source", "date"),
    [(VLLM, "observed", OBSERVED_LINE_DATE), (NO_RGB, "stated", YOUR_LINE_DATE)],
)
def test_evidence_as_old_as_the_line_is_not_newer_than_it(
    statement: str, source: str, date: str
) -> None:
    outcome = check(
        decide("supersede"),
        new_fact(source=source, date=date),
        line(statement),
        ctx(sig("a", date)),
    )
    assert outcome == downgrade("conflict", "R4")


def test_an_observation_never_replaces_a_preference_how_new_or_old_its_evidence_is():
    for date in (NEWER, OLDER, OBSERVED_LINE_DATE):
        outcome = check(
            decide("supersede"),
            new_fact(source="observed", date=date),
            line(NO_RGB),
            ctx(sig("a", date)),
        )
        assert outcome == downgrade("needs-you", "R4"), date


def test_a_fact_dated_after_the_line_with_no_quote_after_it_replaces_nothing():
    """R4 reads the date off a verified quote, not off what the fact claims: a claim is
    only as strong as the words a script can find in its source (design 9)."""
    outcome = check(
        decide("supersede"),
        new_fact(source="observed", date=NEWER),
        line(VLLM),
        ctx(sig("a", OLDER)),
    )
    assert outcome == downgrade("conflict", "R4")


def test_a_verified_quote_carries_the_replacement_however_the_fact_dates_itself():
    outcome = check(
        decide("supersede"),
        new_fact(source="observed", date=OLDER),
        line(VLLM),
        ctx(sig("a", NEWER)),
    )
    assert outcome == apply("supersede")


def test_an_unverified_quote_newer_than_the_line_carries_no_replacement():
    outcome = check(
        decide("supersede"),
        new_fact(source="observed", date=NEWER),
        line(VLLM),
        ctx(sig("a", OLDER), sig("b", NEWER, verified=False)),
    )
    assert outcome == downgrade("conflict", "R4")


def test_a_date_that_is_not_one_carries_no_replacement():
    outcome = check(
        decide("supersede"),
        new_fact(source="observed", date="2026-9-5"),
        line(VLLM),
        ctx(sig("a", "2026-9-5"), sig("b", OLDER)),
    )
    assert outcome == downgrade("conflict", "R4")


def test_an_explicit_correction_skips_the_gate_and_no_other_rule():
    """Design 9 lets your own correction skip R3's gate; R4's authority and its newer
    verified quote still stand between a fact and a standing line."""
    outcome = check(
        decide("supersede"),
        new_fact(source="stated", date=NEWER, explicit_correction=True),
        line(VLLM),
        ctx(sig("a", OLDER)),
    )
    assert outcome == downgrade("conflict", "R4")


@pytest.mark.parametrize("statement", [NO_RGB, SQLITE])
@pytest.mark.parametrize("action", ["supersede", "conflict"])
def test_an_observation_contradicting_a_commitment_needs_you_whichever_action_named_it(
    action: str, statement: str
) -> None:
    """M4.3's prompt names ``conflict`` for the fact that contradicts a line with no clear
    change, which is the pair design 9's table sends to needs you: one physical situation
    comes to one outcome whatever the model called it."""
    outcome = check(
        decide(action),
        new_fact(source="observed", date=NEWER),
        line(statement),
        ctx(sig("a", NEWER)),
    )
    assert outcome == downgrade("needs-you", "R4"), (action, statement)


@pytest.mark.parametrize("source", ["stated", "inferred", "suggested"])
def test_weaker_evidence_contradicting_a_commitment_is_still_only_a_note(source: str) -> None:
    outcome = check(
        decide("conflict"),
        new_fact(source=source, date=NEWER),
        line(NO_RGB),
        ctx(sig("a", NEWER)),
    )
    assert outcome == apply("conflict")


@pytest.mark.parametrize("source", ["inferred", "suggested"])
def test_an_inferred_or_suggested_fact_never_replaces_a_line_of_any_kind(source: str) -> None:
    for kind, statement in TARGET_BY_KIND.items():
        outcome = check(
            decide("supersede"),
            new_fact(source=source, date=NEWER),
            line(statement),
            ctx(*gate()),
        )
        assert outcome == downgrade("conflict", "R4"), kind


# R4's other refusals: lines the table has no row for, because they no longer stand as
# memory or do not stand as it yet.

DEAD_LINES = [JSON_INDEX, DGN_2016, HOOK_ENQUEUES]
"""Superseded, withdrawn and stale: none of them is memory a decision may act on."""


@pytest.mark.parametrize("statement", DEAD_LINES)
@pytest.mark.parametrize("action", ["confirm", "supersede", "conflict"])
def test_a_decision_naming_a_line_that_no_longer_stands_is_refused(
    action: str, statement: str
) -> None:
    outcome = check(
        decide(action),
        new_fact(source="stated"),
        line(statement),
        ctx(sig("a", NEWER)),
    )
    assert outcome == reject("R4")


@pytest.mark.parametrize("action", ["supersede", "conflict"])
def test_a_candidate_line_is_neither_replaced_nor_marked(action: str) -> None:
    """A Candidates line has no kind, so no row of the authority table reaches it, and a
    note marks an active line and never a candidate (design 6's fact state)."""
    outcome = check(decide(action), new_fact(source="stated"), line(CHECKPOINTS), ctx(*gate()))
    assert outcome == reject("R4")


@pytest.mark.parametrize("action", ["supersede", "conflict"])
@pytest.mark.parametrize("statement", NOTED_LINES)
def test_a_strike_or_a_second_note_on_a_line_whose_note_is_open_is_refused(
    action: str, statement: str
) -> None:
    """Design 6's example note says the line stands until stronger evidence arrives, but a
    note stays open until a person closes it and a line carries one at a time: striking the
    line or noting it again would add a parse error, and the writer refuses an edit that
    adds one, so the rules refuse instead of handing the writer an edit it would drop."""
    outcome = check(
        decide(action),
        new_fact(source="observed", date=NEWER),
        line(statement),
        ctx(sig("a", NEWER)),
    )
    assert outcome == reject("design 6 open note"), (action, statement)


def test_a_note_under_a_standing_line_is_what_the_page_can_write():
    assert page.parse(NOTED_LINE, PAGE_PATH).errors == []


def test_a_strike_through_a_noted_line_is_text_the_parser_refuses():
    errors = [message for _, message in page.parse(STRUCK_UNDER_THE_NOTE, PAGE_PATH).errors]
    assert errors == ["a callout cannot follow a superseded fact"]


def test_a_second_note_on_a_noted_line_is_text_the_parser_refuses():
    errors = [message for _, message in page.parse(SECOND_NOTE_ON_A_LINE, PAGE_PATH).errors]
    assert errors == ["a callout cannot follow a conflict fact"]


# confirm: a citation on a line that stands, and the one moment a candidate leaves.


def test_confirming_a_standing_line_needs_no_gate_and_no_newer_date():
    outcome = check(
        decide("confirm"),
        new_fact(source="inferred", date=OLDER),
        line(VLLM),
        ctx(sig("a", OLDER)),
    )
    assert outcome == apply("confirm")


def test_confirming_a_line_under_an_open_note_adds_the_evidence():
    outcome = check(
        decide("confirm"), new_fact(source="observed"), line(DIGEST_PER_CHUNK), ctx(sig("a", NEWER))
    )
    assert outcome == apply("confirm")


def test_confirming_a_candidate_below_the_gate_keeps_it_a_candidate():
    outcome = check(
        decide("confirm"),
        new_fact(source="inferred", date=OLDER),
        line(CHECKPOINTS),
        ctx(sig("a", OLDER)),
    )
    assert outcome == apply("confirm")


def test_a_candidate_that_clears_the_gate_is_promoted():
    """The line itself still reads ``(signals 1/3, sessions 1/2)``: the gate is judged on
    the evidence handed in, since no page has been written for this run yet."""
    outcome = check(decide("confirm"), new_fact(source="inferred"), line(CHECKPOINTS), ctx(*gate()))
    assert outcome == apply("promote")


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [(ctx(sig("a", OLDER)), apply("confirm")), (ctx(*gate()), apply("promote"))],
    ids=["below the gate", "over the gate"],
)
def test_a_suggested_candidate_leaves_only_over_the_gate(
    evidence: rules.Context, expected: Outcome
) -> None:
    outcome = check(decide("confirm"), new_fact(source="suggested"), line(OFF_THE_RECORD), evidence)
    assert outcome == expected


def test_an_explicit_correction_promotes_a_candidate_without_the_gate():
    outcome = check(
        decide("confirm"),
        new_fact(source="stated", explicit_correction=True),
        line(CHECKPOINTS),
        ctx(sig("a", "2026-10-04")),
    )
    assert outcome == apply("promote")


def test_a_candidate_held_by_the_worker_rule_needs_only_a_primary_session():
    """This line is ``[stated]`` and sits in Candidates because a worker session wrote it.
    R3's gate is a rule about inferred facts, so the one thing the worker rule asks for,
    a primary session corroborating it, is all it takes to promote this one."""
    outcome = check(
        decide("confirm"),
        new_fact(source="observed"),
        line(SESSION_END),
        ctx(sig("w1", "2026-10-04", origin=rules.WORKER), sig("a", "2026-10-03")),
    )
    assert outcome == apply("promote")


def test_a_candidate_held_by_the_worker_rule_waits_without_a_primary_session():
    outcome = check(
        decide("confirm"),
        new_fact(source="observed"),
        line(SESSION_END),
        ctx(
            sig("w1", "2026-10-04", origin=rules.WORKER),
            sig("w2", "2026-10-03", origin=rules.WORKER),
        ),
    )
    assert outcome == reject("design 8 worker sessions")


# conflict: a note, which replaces nothing and so needs neither authority nor newer dates.


@pytest.mark.parametrize("date", [NEWER, OLDER, OBSERVED_LINE_DATE])
def test_a_conflict_note_needs_no_authority_and_no_newer_evidence(date: str) -> None:
    outcome = check(
        decide("conflict"),
        new_fact(source="observed", date=date),
        line(VLLM),
        ctx(sig("a", date)),
    )
    assert outcome == apply("conflict")


# R5: only what cannot be re-derived from the repository.


def test_a_statement_that_is_a_run_of_file_paths_is_refused():
    listing = "src/aft/capture.py src/aft/queue.py src/aft/filter.py hold the capture path."
    outcome = check(
        decide("add", line=None), new_fact(statement=listing), None, ctx(sig("a", NEWER))
    )
    assert outcome == reject("R5")


def test_a_statement_carrying_a_fenced_code_block_is_refused():
    fenced = "The fix is ```pin libolm to 0.9``` in the recipe."
    outcome = check(
        decide("add", line=None), new_fact(statement=fenced), None, ctx(sig("a", NEWER))
    )
    assert outcome == reject("R5")


@pytest.mark.parametrize(
    "statement",
    [
        "src/aft/capture.py holds the SessionEnd handler.",
        "src/aft/capture.py enqueues and src/aft/queue.py stores the row.",
        "Release by tagging v1.2.3 on main after the gate passes.",
    ],
)
def test_a_statement_naming_one_or_two_files_or_a_version_is_kept(statement: str) -> None:
    outcome = check(
        decide("add", line=None), new_fact(statement=statement), None, ctx(sig("a", NEWER))
    )
    assert outcome == apply("add")


@pytest.mark.parametrize(
    "statement",
    [
        "DGN lives at https://a.com/b, https://c.com/d and https://e.com/f.",
        "The US/UK, EU/Asia and APAC/EMEA splits differ.",
        "The handbook is at https://aft.dev/handbook beside src/aft/page.py.",
        "Fit it with the 3/12 twist lock and a 4mm hex key.",
    ],
)
def test_urls_a_slash_pair_or_a_size_in_a_statement_is_no_file_listing(statement: str) -> None:
    """R5 refuses a file listing and a code block. A reference is a kind of its own whose
    pages hold URLs, and a run like ``US/UK`` or ``3/12`` names no path in the repo."""
    outcome = check(
        decide("add", line=None), new_fact(statement=statement), None, ctx(sig("a", NEWER))
    )
    assert outcome == apply("add")


# R6: one project per unit of work, and global pages are yours alone.


def test_an_edit_to_another_projects_page_is_refused():
    outcome = check(
        decide("supersede"),
        new_fact(source="stated", date=NEWER),
        line(VLLM),
        ctx(sig("a", NEWER), page_project="p_other"),
    )
    assert outcome == reject("R6")


def test_a_run_may_edit_the_page_of_the_project_it_is_on():
    outcome = check(
        decide("confirm"), new_fact(source="observed"), line(VLLM), ctx(sig("a", NEWER))
    )
    assert outcome == apply("confirm")


@pytest.mark.parametrize("action", ["add", "confirm", "supersede", "conflict"])
@pytest.mark.parametrize("source", ["observed", "inferred", "suggested"])
def test_a_global_page_changes_only_from_what_you_stated(action: str, source: str) -> None:
    target = None if action == "add" else line(VLLM)
    outcome = check(
        decide(action, line=None if action == "add" else "L1"),
        new_fact(source=source),
        target,
        ctx(*gate(), page_project=None),
    )
    assert outcome == reject("R6")


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("add", apply("add")),
        ("confirm", apply("confirm")),
        ("supersede", apply("supersede")),
        ("conflict", apply("conflict")),
    ],
)
def test_your_stated_word_is_what_may_change_a_global_page(action: str, expected: Outcome) -> None:
    """Every action stays open on a global page behind a ``[stated]`` fact; what R6 withholds
    from the others is the authority to write into memory that is not this project's."""
    target = None if action == "add" else line(VLLM)
    outcome = check(
        decide(action, line=None if action == "add" else "L1"),
        new_fact(source="stated", scope="global"),
        target,
        ctx(*gate(), page_project=None),
    )
    assert outcome == expected


# The order of the rules: the first one broken is the one the report names.


def test_the_first_rule_to_break_is_the_one_the_report_names():
    """This decision breaks R1, R2, R6 and R5 at once and reports R1: a refusal naming four
    rules would let one rejection be counted four times in the report."""
    outcome = check(
        decide("supersede"),
        new_fact(source="assumed", statement="src/a/a.py src/a/b.py src/a/c.py"),
        line(NO_RGB),
        ctx(sig("a", NEWER, verified=False), page_project=None),
    )
    assert outcome == reject("R1")


def test_a_refused_provenance_hides_no_cross_project_edit():
    """Repair the provenance and the same decision is refused again, now by R6: the order
    names one rule per decision, not one rule and three silent passes."""
    outcome = check(
        decide("supersede"),
        new_fact(source="observed", statement=SGLANG),
        line(NO_RGB),
        ctx(sig("a", NEWER), page_project=None),
    )
    assert outcome == reject("R6")


# Purity: the whole of the module's world is its arguments.


class _NoClock:
    """Dates still parse; anything that reads the present moment is refused."""

    @staticmethod
    def fromisoformat(value: str) -> datetime.date:
        return datetime.date.fromisoformat(value)

    @staticmethod
    def today(*_args: object) -> datetime.date:
        raise AssertionError("the rules read the clock")

    @staticmethod
    def now(*_args: object) -> datetime.date:
        raise AssertionError("the rules read the clock")


@pytest.mark.parametrize(
    ("decision", "fact", "target", "context", "expected"),
    [
        (
            decide("add", line=None),
            new_fact(source="inferred"),
            None,
            ctx(*gate()),
            apply("add"),
        ),
        (
            decide("supersede"),
            new_fact(source="stated"),
            line(VLLM),
            ctx(sig("a", NEWER)),
            apply("supersede"),
        ),
        (decide("skip", line=None), new_fact(), None, ctx(), apply("skip")),
    ],
)
def test_check_reaches_nothing_outside_the_values_it_is_handed(
    monkeypatch: pytest.MonkeyPatch,
    decision: rules.Decision,
    fact: rules.NewFact,
    target: page.Fact | None,
    context: rules.Context,
    expected: Outcome,
) -> None:
    def forbid(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the rules reached the outside world")

    monkeypatch.setattr(rules, "date", _NoClock)
    monkeypatch.setattr(builtins, "open", forbid)
    monkeypatch.setattr(socket, "socket", forbid)
    assert check(decision, fact, target, context) == expected


# The new fact's quotes and the line's own are kept apart: only the new fact's carry a
# decision, and only verified primary quotes count toward the gate.


def test_the_lines_own_verified_quote_does_not_vouch_for_an_unverified_new_one():
    outcome = check(
        decide("confirm"),
        new_fact(source="observed"),
        line(VLLM),
        ctx(sig("x", NEWER, verified=False), line_evidence=(sig("s2", OLDER),)),
    )
    assert outcome == reject("R1")


def test_a_stated_supersede_needs_your_own_newer_words():
    """The newer quote has to be the one carrying the authority: someone else's newer words
    beside an old quote of yours replace nothing of yours."""
    outcome = check(
        decide("supersede"),
        new_fact(source="stated", kind="preference"),
        line(NO_RGB),
        ctx(sig("old", "2026-09-01"), sig("x", NEWER, yours=False)),
    )
    assert outcome == downgrade("conflict", "R4")


def test_a_worker_quote_beside_the_lines_primary_quote_still_cannot_confirm():
    outcome = check(
        decide("confirm"),
        new_fact(source="observed"),
        line(VLLM),
        ctx(sig("w1", NEWER, origin=rules.WORKER), line_evidence=(sig("s2", OLDER),)),
    )
    assert outcome == reject("design 8 worker sessions")


def test_a_worker_quote_beside_the_lines_primary_quote_cannot_supersede():
    outcome = check(
        decide("supersede"),
        new_fact(source="observed"),
        line(VLLM),
        ctx(sig("w1", NEWER, origin=rules.WORKER), line_evidence=(sig("s2", OLDER),)),
    )
    assert outcome == downgrade("candidate", "design 8 worker sessions")


def test_worker_and_unverified_footnotes_on_a_candidate_add_nothing_to_the_gate():
    """Two primary signals across two sessions do not clear the gate, however many worker
    or unverified footnotes the candidate carries beside them."""
    outcome = check(
        decide("confirm"),
        new_fact(source="inferred"),
        line(SWEEP),
        ctx(
            sig("n", "2026-10-04", source="n", resource="aft://claude/n#e1"),
            line_evidence=(
                sig("s3", "2026-10-03", source="s3", resource="aft://claude/s3#e1"),
                sig("w1", "2026-10-03", source="w1", origin=rules.WORKER),
                sig("w2", "2026-10-02", source="w2", origin=rules.WORKER),
                sig("u1", "2026-10-02", source="u1", verified=False),
            ),
        ),
    )
    assert outcome == apply("confirm")


def test_an_explicit_correction_without_a_quote_of_yours_skips_no_gate():
    outcome = check(
        decide("add", line=None),
        new_fact(source="inferred", explicit_correction=True),
        None,
        ctx(sig("a", NEWER, yours=False)),
    )
    assert outcome == downgrade("candidate", "R3")


def test_your_explicit_correction_promotes_the_candidate_it_confirms():
    outcome = check(
        decide("confirm"),
        new_fact(source="stated", explicit_correction=True),
        line(SWEEP),
        ctx(sig("a", NEWER)),
    )
    assert outcome == apply("promote")


def test_a_correction_in_your_words_from_a_worker_session_skips_no_gate():
    outcome = check(
        decide("confirm"),
        new_fact(source="stated", explicit_correction=True),
        line(SWEEP),
        ctx(sig("p", OLDER, yours=False), sig("w", NEWER, origin=rules.WORKER)),
    )
    assert outcome == apply("confirm")


def test_a_worker_quote_is_never_the_newer_quote_a_supersede_needs():
    outcome = check(
        decide("supersede"),
        new_fact(source="observed"),
        line(VLLM),
        ctx(sig("p", "2026-09-01"), sig("w", NEWER, origin=rules.WORKER)),
    )
    assert outcome == downgrade("conflict", "R4")


def test_your_words_from_a_worker_session_replace_no_preference():
    outcome = check(
        decide("supersede"),
        new_fact(source="stated", kind="preference"),
        line(NO_RGB),
        ctx(sig("p", "2026-09-01", yours=False), sig("w", NEWER, origin=rules.WORKER)),
    )
    assert outcome == downgrade("conflict", "R4")


def test_a_global_fact_that_is_not_stated_is_refused():
    outcome = check(
        decide("add", line=None),
        new_fact(source="observed", scope="global"),
        None,
        ctx(sig("a", NEWER), page_project=None),
    )
    assert outcome == reject("R6")


@pytest.mark.parametrize(("scope", "page_project"), [("global", "p_widget"), ("project", None)])
def test_a_fact_goes_only_to_a_page_of_its_own_scope(scope: str, page_project: str | None) -> None:
    outcome = check(
        decide("add", line=None),
        new_fact(source="stated", scope=scope),
        None,
        ctx(*gate(), page_project=page_project),
    )
    assert outcome == reject("R6")
