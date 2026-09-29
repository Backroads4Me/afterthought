"""The rules that decide whether one dream decision becomes memory (design 9).

``check`` takes the model's decision about one candidate fact (design 9 step 3), the fact,
the existing line the decision names if any, and the numbers the decision is judged on,
and answers with one of three outcomes:

* ``apply(action)`` - make the edit the decision asked for.
* ``downgrade(to_action, rule)`` - the decision lost its force; make this edit instead,
  and name the rule that took it away.
* ``reject(rule)`` - write nothing, and name the rule that refused.

Every refusal and downgrade names its rule, because a dream run that cannot say why it
lost its nerve is undebuggable, and design 9's report holds a Rejected row per refusal.

Pure by construction: no page, archive, index, clock or database is read or written here.
Each rule that would otherwise need the outside world takes the value from ``context``,
which carries the run's date, every quote the line would cite with its session, role and
date, and the project ids of the run and of the page the decision would edit.

Which of design 9's rules are here, and where the rest are:

* **R1** provenance, **R2** stated means committed, **R3** the recurrence gate,
  **R4** authority by kind, **R5** what cannot be re-derived and **R6** one project per
  unit of work are checked here, in that order.
* **R7** size caps flags a long page in the report (M4.5). No decision changes because a
  page is long, so no outcome here could carry the flag.
* **R8** anchors marks a line stale when its words leave the file, which needs ripgrep
  and git and answers no decision (M4.5's maintenance).
* **R9** no free-form writes is enforced before this module by the decision schema
  (M4.3). What is enforced here is the tail of it: an answer naming no action the schema
  allows reaches nothing, and no outcome here names a file write.

The actions an outcome can name are the model's five - ``add``, ``confirm``,
``supersede``, ``conflict``, ``skip`` (design 9 step 3) - plus three the rules reach for
and the caller writes with the page writer's operation of the same name:

* ``candidate`` - the Candidates section (design 6's fact state), where R3 and design 8's
  worker rule hold a fact that has not earned a line of its own.
* ``needs-you`` - design 6's fact state, what a supersede of one of your own commitments
  becomes when the new evidence cannot replace it (design 9's authority table).
* ``promote`` - the writer's ``promote`` operation (M4.2), when corroborated evidence
  carries a candidate over R3's gate; the page writer leaves promotion to these rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from aft import page, pagewrite

PRIMARY = "primary"
WORKER = "worker"
"""How the session behind a quote was classified: a session under a worker path is
"evidence of a lower rank" whose facts "carry that role into the dream" (design 8, M2.7).
"""

SIGNALS_GATE = pagewrite.SIGNALS_GATE
SESSIONS_GATE = pagewrite.SESSIONS_GATE
"""R3's numbers, taken from the writer that prints them in a candidate's counts suffix so
the gate and the page a person reads can never disagree."""

HALF_WEIGHT_AFTER_DAYS = 30
"""A signal older than this counts half toward R3's gate (design 9 R3)."""

MODEL_ACTIONS = ("add", "confirm", "supersede", "conflict", "skip")
"""The decisions the model may name (design 9 step 3, M4.3's schema)."""

AUTHORITY = {
    "preference": ("stated",),
    "decision": ("stated",),
    "state": ("observed", "stated"),
    "fact": ("observed", "stated"),
    "lesson": ("observed", "stated"),
    "procedure": ("observed", "stated"),
    "reference": ("observed", "stated"),
}
"""Who may replace a line of each kind, by source tag (design 9, authority to replace a
fact). ``inferred`` and ``suggested`` appear in no row: they never replace anything. The
``stated`` in the first two rows is *your* newer statement, which R2 has already grounded
in your own words, and every row needs newer evidence besides: the date check runs next to
this table in ``_supersede``."""

NEEDS_THE_GATE = ("inferred", "suggested")
"""The tags whose facts are the model's own output rather than something you said or a run
observed. R3's gate holds them in Candidates; design 9 says nothing about what clears a
``suggested`` fact, so it is held to the same bar as the stronger ``inferred`` tag."""

NO_LONGER_MEMORY = (page.SUPERSEDED, page.WITHDRAWN, page.STALE)
"""Lines that stand no longer (design 6's fact state): the authority table has rows for
the seven kinds of line that stand as memory, and none for these."""

_PATH_TOKEN = re.compile(r"[\w.\-]*(?:/[\w.\-]+)+")
"""A path: the shape a file listing is made of (R5)."""


@dataclass(frozen=True)
class Evidence:
    """One quote the fact would cite, as the caller found it.

    The digest verifies a quote when it writes it (design 8); the dream re-checks it from
    these values rather than opening the archive (R1, R2, R3)."""

    session: str
    """The session id the quote came from. R3 counts distinct sessions, and a session is
    one of these ids, not one of its chunks."""

    origin: str
    """``PRIMARY`` or ``WORKER``: how that session was classified (design 8, worker sessions)."""

    date: str
    """YYYY-MM-DD the evidence is dated, which decides whether it counts half (R3)."""

    verified: bool = False
    """The quote appears in the entry it cites (design 8, design 9 R1)."""

    yours: bool = False
    """The words are one of your turns, or your own edit (R2; M4.1 tags a hand-typed line
    ``[stated]`` with a quote that is the line itself)."""


@dataclass(frozen=True)
class NewFact:
    """The candidate fact a digest proposed, as the dream sees it (design 8)."""

    statement: str
    kind: str
    """One of the seven kinds. The caller writes the line under it; the authority table
    judges a replacement by the *target* line's kind, never by this one."""

    source: str
    """One of the four source tags, as the digest tagged it (``page.SOURCES``)."""

    date: str
    """YYYY-MM-DD the fact was last evidenced, which is the date its line would carry."""

    scope: str = "project"
    """``project`` or ``global`` (design 8). Retrieval uses it to decide which pages to
    search; R6 reads the page a decision would edit."""

    explicit_correction: bool = False
    """You corrected the record yourself, which skips R3's gate (design 9 R3)."""


@dataclass(frozen=True)
class Decision:
    """One model answer about one candidate fact (design 9 step 3, M4.3's schema)."""

    action: str
    line: str | None = None
    """The ``L1`` to ``L10`` name the model gave. Retrieval resolves it to the target line
    passed beside it; the reason it named nothing usable is M4.3's to reject."""

    reason: str = ""
    """Carried through untouched for the report's Conflicts and Rejected rows (design 9)."""


@dataclass(frozen=True)
class Context:
    """What the rules are allowed to know, and the whole of it.

    Each value stands in for one read this module must not do: the run's date for the
    clock, the quotes for the evidence archive, the project ids for the vault."""

    today: str
    """YYYY-MM-DD of the run. Nothing here reads a clock."""

    evidence: tuple[Evidence, ...]
    """Every quote the line would cite, the new fact's own included. An unverified quote
    travels here too, so R1's refusal is visible rather than absent."""

    run_project: str
    """The project the run is on (R6)."""

    page_project: str | None = None
    """The ``aft.project`` id of the page the decision would edit, or None for a page
    under ``global/``, which belongs to no project (R6)."""


@dataclass(frozen=True)
class Apply:
    """Make the edit the decision asked for."""

    action: str


@dataclass(frozen=True)
class Downgrade:
    """The decision lost its force; make this edit instead. ``rule`` says what took it."""

    to_action: str
    rule: str


@dataclass(frozen=True)
class Reject:
    """Write nothing. ``rule`` says what refused."""

    rule: str


Outcome = Apply | Downgrade | Reject


def apply(action: str) -> Apply:
    return Apply(action)


def downgrade(to_action: str, rule: str) -> Downgrade:
    return Downgrade(to_action, rule)


def reject(rule: str) -> Reject:
    return Reject(rule)


def check(
    decision: Decision,
    new_fact: NewFact,
    target_line: page.Fact | None,
    context: Context,
) -> Outcome:
    """Judge one decision against the rules (design 9 step 4).

    The checks run in rule order, so a decision that breaks two rules reports the first,
    and a refusal never hides behind a weaker result. ``skip`` writes nothing, so nothing
    is checked for it. ``add`` ignores a named line: the schema allows one on it, and
    adding touches no existing line.
    """
    if decision.action not in MODEL_ACTIONS:
        return reject("R9")  # no action the schema allows: nothing to apply
    if decision.action == "skip":
        return apply("skip")

    # R1: one known source tag, and at least one quote a script found in its source.
    if new_fact.source not in page.SOURCES:
        return reject("R1")
    if not any(quote.verified for quote in context.evidence):
        return reject("R1")

    # R2: [stated] means you committed to it, so the quote has to be your own words.
    if new_fact.source == "stated" and not any(
        quote.verified and quote.yours for quote in context.evidence
    ):
        return reject("R2")

    # R6: one project per run, and a global page changes only from what you stated.
    if context.page_project is not None and context.page_project != context.run_project:
        return reject("R6")
    if context.page_project is None and new_fact.source != "stated":
        return reject("R6")

    # R5: a fact line holds what the repository does not already record.
    if _derivable(new_fact.statement):
        return reject("R5")

    # A fact resting only on worker sessions is evidence of a lower rank: it stays a
    # candidate until a primary session corroborates it, whatever its tag (design 8).
    # Confirming is allowed because it only corroborates memory already standing.
    if decision.action != "confirm" and not _primary(context.evidence):
        return downgrade("candidate", "design 8 worker sessions")

    if decision.action == "add":
        return _new_line(new_fact, context)

    if target_line is None:
        return reject("R9")  # the schema names a line for these three decisions (M4.3)
    if target_line.state in NO_LONGER_MEMORY:
        return reject("R4")  # the table gives no authority over a line that no longer stands
    if target_line.kind is None and decision.action != "confirm":
        # A Candidates line has no kind, so no table row reaches it: nothing replaces it,
        # and a note marks an active line and never a candidate (design 6's fact state).
        # Confirming a candidate is how it gathers evidence toward its gate.
        return reject("R4")

    if decision.action == "confirm":
        return _confirm(new_fact, target_line, context)
    if decision.action == "conflict":
        return apply("conflict")
    return _supersede(new_fact, target_line)


def _new_line(new_fact: NewFact, context: Context) -> Outcome:
    """Where an added line goes: R3's gate decides between a kind section and Candidates."""
    if new_fact.source in NEEDS_THE_GATE and not new_fact.explicit_correction:
        if not _passes_gate(context):
            return downgrade("candidate", "R3")
    return apply("add")


def _confirm(new_fact: NewFact, target_line: page.Fact, context: Context) -> Outcome:
    """Confirming cites the new source on the line and counts toward recurrence.

    On a candidate line it is also where the gate is judged, since a candidate leaves
    Candidates only when its evidence carries it over (design 6, R3)."""
    if target_line.kind is not None:
        return apply("confirm")
    if new_fact.explicit_correction:
        return apply("promote")  # R3: an explicit correction from you skips the gate
    if target_line.source in NEEDS_THE_GATE:
        return apply("promote") if _passes_gate(context) else apply("confirm")
    # A stated or observed line is in Candidates because the worker rule held it; a
    # primary session corroborating it is what the worker rule asks for.
    if any(quote.verified and quote.origin == PRIMARY for quote in context.evidence):
        return apply("promote")
    return apply("confirm")


def _supersede(new_fact: NewFact, target_line: page.Fact) -> Outcome:
    """R4: replacing needs the authority the table gives *and* evidence newer than the
    line's latest evidence. Without either the old line stands and the result is a
    conflict, or needs you when it is one of your commitments that is contradicted."""
    authority = AUTHORITY.get(target_line.kind or "", ())
    if new_fact.source in authority and new_fact.date > target_line.date:
        return apply("supersede")
    return downgrade(_refusal(new_fact, target_line), "R4")


def _refusal(new_fact: NewFact, target_line: page.Fact) -> str:
    """What a refused supersede becomes (design 9's table): a conflict note, or needs you
    when only weaker evidence disagrees with a preference or a decision of yours and that
    evidence is an observation."""
    if target_line.kind in ("preference", "decision") and new_fact.source == "observed":
        return "needs-you"
    return "conflict"


def _passes_gate(context: Context) -> bool:
    """R3: three signals across two primary sessions, signals older than 30 days counting
    half. Both numbers come from ``context.evidence``, never from a page or an archive."""
    signals = _primary(context.evidence)
    weight = sum(_weight(quote, context.today) for quote in signals)
    return weight >= SIGNALS_GATE and len({quote.session for quote in signals}) >= SESSIONS_GATE


def _primary(evidence: tuple[Evidence, ...]) -> list[Evidence]:
    """The verified quotes from primary sessions: a worker session's evidence never
    carries a fact over the gate, and an unverified quote is no signal at all."""
    return [q for q in evidence if q.verified and q.origin == PRIMARY]


def _weight(quote: Evidence, today: str) -> float:
    age = (date.fromisoformat(today) - date.fromisoformat(quote.date)).days
    return 0.5 if age > HALF_WEIGHT_AFTER_DAYS else 1.0


def _derivable(statement: str) -> bool:
    """R5's script half: the two shapes a script can name without the repository in front
    of it. Three or more paths in one statement is a file listing; a fence is a code
    block. "Anything git already records" without either shape is the digest prompt's job
    (design 8), and R5's preference for durable phrasing over stale figures is a
    preference, not a bar."""
    if "```" in statement:
        return True
    return len(_PATH_TOKEN.findall(statement)) >= 3
