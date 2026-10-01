"""The rules that decide whether one dream decision becomes memory (design 9).

``check`` takes the model's decision about one candidate fact (design 9 step 3), the fact,
the existing line the decision names if any, and the quotes and dates the decision is
judged on, and answers with one of three outcomes:

* ``apply(action)`` - make the edit the decision asked for.
* ``downgrade(to_action, rule)`` - the decision lost its force; make this edit instead,
  and name the rule that took it away.
* ``reject(rule)`` - write nothing, and name the rule that refused.

Every refusal and downgrade names its rule, because a dream run that cannot say why it
lost its nerve is undebuggable, and design 9's report holds a Rejected row per refusal.

Pure by construction: no page, archive, index, clock or database is read or written here.
Each rule that would otherwise need the outside world takes the value from ``context``,
which carries the run's date, the new fact's quotes and the target line's own quotes (kept
apart, because only the new fact's quotes can carry a decision), and the project ids of the
run and of the page the decision would edit.

Which of design 9's rules are here, and where the rest are:

* **R1** provenance, **R2** stated means committed, **R6** one project per unit of work
  and **R5** what cannot be re-derived judge the fact and the page rather than the
  action, and are checked here in that order before any action is looked at. **R3**'s
  gate and **R4**'s table each answer for one action, so they run inside it, and design
  6's open note is the last word on whatever they decide to write. ``check`` gives the
  whole order, and the first rule a decision breaks is the one its outcome names.
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
* ``lift`` - a long-term fact confirms a line of short-term memory, which takes the line
  off ``recent.md`` and puts it to the next decision as a long-term fact (design 6, R10).

**R10** short term stays short is checked here for what one decision may do: a
short-term fact edits only ``recent.md`` except to confirm a long-term line. When a line
rolls off ``recent.md``, and when its sessions carry it to long-term memory, are counted
over the project's sessions, which is the caller's (``aft.dream.apply``).
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
LIBRARY = "library"
"""No session stands behind the quote: it was taken from an imported document or an
instruction file (DESIGN 11 step 5). Such a quote verifies a fact and carries no recurrence,
so R3 counts none of it, however many imported files repeat the sentence, and a fact proposed
from a collection waits as a candidate.
"""

SIGNALS_GATE = pagewrite.SIGNALS_GATE
SESSIONS_GATE = pagewrite.SESSIONS_GATE
"""R3's numbers, taken from the writer that prints them in a candidate's counts suffix so
the gate and the page a person reads can never disagree."""

WORKER_RULE = "design 8 worker sessions"
LIBRARY_RULE = "design 11 library evidence"
"""Which rule a refusal names: a worker session's quote is the lower rank design 8 holds, and a
quote with no session behind it at all is the collection evidence of design 11. The report prints
this string in the Rejected row, so a person reading it learns what stood behind the quote."""


HALF_WEIGHT_AFTER_DAYS = 30
"""A signal older than this counts half toward R3's gate (design 9 R3)."""

MODEL_ACTIONS = ("add", "confirm", "supersede", "conflict", "skip")
"""The decisions the model may name (design 9 step 3, M4.3's schema)."""

LONG = "long"
SHORT = "short"
"""A fact's term (design 6): long-term memory is what stays true, short-term memory what the
project's last few sessions were working with."""


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
in your own words, and every row needs a verified quote newer than the line besides: that
check runs beside this table in ``_supersede``."""

NEEDS_THE_GATE = ("inferred", "suggested")
"""The tags whose facts are the model's own output rather than something you said or a run
observed. R3's gate holds them in Candidates, and a ruling on R3 holds a ``[suggested]``
fact exactly as it holds an ``[inferred]`` one."""

NO_LONGER_MEMORY = (page.SUPERSEDED, page.WITHDRAWN, page.STALE)
"""Lines that stand no longer (design 6's fact state): the authority table has rows for
the seven kinds of line that stand as memory, and none for these."""

NOTE_OR_STRIKE = ("supersede", "conflict", "needs-you")
"""The outcomes that write below the target line: a strike through it, or a callout under
it. A line whose note is still open can carry neither (design 6's fact state): a second
note marks a fact that is no longer active and a strike leaves the note following a
superseded fact, both of which ``page.parse`` reports and the page writer refuses."""

_PATH_TOKEN = re.compile(r"(?<![\w./-])(?:[\w.-]+/)+[\w-]+\.[A-Za-z][A-Za-z0-9]*")
"""A file path: a run of name segments separated by slashes whose last one carries an
extension, which is the shape a file listing is made of (R5). ``:`` is not a path
character and a run may not start inside one, so a URL is never read as a path, and a
pair like ``US/UK`` is not a path either."""


@dataclass(frozen=True)
class Evidence:
    """One quote the fact would cite, as the caller found it.

    The digest verifies a quote when it writes it (design 8); the dream re-checks it from
    these values rather than opening the archive (R1, R2, R3 and R4)."""

    source: str
    """The footnote id the quote would be cited under on the resulting line (design 6,
    sources). R3's counts are the writer's, which count one signal per distinct id, so
    two quotes carrying one id are one signal."""

    resource: str
    """The ``aft://`` entry the quote came in, fragment included. Design 6 makes a session
    the part of that uri before the fragment, which is what R3 counts sessions of."""

    origin: str
    """``PRIMARY``, ``WORKER`` or ``LIBRARY``: the session that stands behind the quote, or
    no session at all when the quote came out of an imported collection (design 8, design 11).
    """

    date: str
    """YYYY-MM-DD the evidence is dated, which decides whether it counts half (R3) and
    whether it is newer than a standing line (R4)."""

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
    """YYYY-MM-DD the fact was last evidenced, which is the date its line would carry. It
    carries no replacement on its own: R4 judges a replacement by the quotes."""

    scope: str = "project"
    """``project`` or ``global`` (design 8). R6 holds it to the page the decision would
    edit: a global fact goes only to a global page, and only when it is ``[stated]``."""

    term: str = LONG
    """``long`` or ``short`` (design 6). A short-term fact goes only to ``recent.md`` and
    edits a long-term line only to confirm it (R10)."""

    explicit_correction: bool = False
    """You corrected the record yourself. It counts only on a ``[stated]`` fact backed by a
    verified quote of yours from a primary session, and then it skips R3's gate, which
    promotes a candidate line it confirms (design 9)."""


@dataclass(frozen=True)
class Decision:
    """One model answer about one candidate fact (design 9 step 3, M4.3's schema)."""

    action: str
    line: str | None = None
    """The ``L1`` to ``L10`` name the model gave. Retrieval resolves it to the target line
    passed beside it, and the schema requires one on an action that edits a line."""

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
    """The new fact's own quotes, and nothing else: they are what R1, R2, R4's newer quote
    and the worker rule judge. An unverified quote travels here too, so R1's refusal is
    visible rather than absent."""

    run_project: str
    """The project the run is on (R6)."""

    page_project: str | None = None
    """The ``aft.project`` id of the page the decision would edit, or None for a page
    under ``global/``, which belongs to no project (R6)."""

    line_evidence: tuple[Evidence, ...] = ()
    """The target line's own quotes, one per footnote it carries. Only R3's gate reads
    them, and only a verified quote from a primary session counts: a footnote without one
    here adds nothing to the count."""

    target_recent: bool = False
    """The line the decision names sits on the project's ``recent.md``, its short-term
    memory (R10)."""


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

    The checks run in one order, so a decision that breaks two rules reports the first and
    a refusal never hides behind a weaker result:

    1. the action is one of the five the schema allows, and ``skip`` answers at once;
    2. R1, R2, R6 and R5, which judge the fact and the page rather than the action;
    3. design 8's worker rule, which holds a fact of a lower rank in Candidates;
    4. the action's own rule, which is R3's gate for ``add``, R3's gate or the worker
       release inside a candidate's ``confirm``, R4's table and a newer verified quote
       for ``supersede``, and R4's table for the note a ``conflict`` writes. An action
       that named no line, or named one the table gives no row for, is refused where
       that rule would have run, before it;
    5. design 6's open note, last because it depends on which note or strike the rules
       above decided to write.

    ``skip`` writes nothing, so nothing else is checked for it. ``add`` ignores a named
    line: the schema allows one on it, and adding touches no existing line.
    """
    if decision.action not in MODEL_ACTIONS:
        return reject("R9")  # no action the schema allows: nothing to apply
    if decision.action == "skip":
        return apply("skip")

    # R1: one known source tag, and at least one quote of the new fact's own that a script
    # found in its source. The target line's quotes vouch for the line, not for this fact.
    if new_fact.source not in page.SOURCES:
        return reject("R1")
    if not any(quote.verified for quote in context.evidence):
        return reject("R1")

    # R2: [stated] means you committed to it, so the quote has to be your own words.
    if new_fact.source == "stated" and not any(
        quote.verified and quote.yours for quote in context.evidence
    ):
        return reject("R2")

    # R6: one project per run, a global fact goes only to a global page and a project fact
    # only to a project page, and a global page changes only from what you stated.
    if context.page_project is not None and context.page_project != context.run_project:
        return reject("R6")
    if (new_fact.scope == "global") != (context.page_project is None):
        return reject("R6")
    if context.page_project is None and new_fact.source != "stated":
        return reject("R6")

    # R5: a fact line holds what the repository does not already record.
    if _derivable(new_fact.statement):
        return reject("R5")

    if new_fact.term == SHORT:
        # R10: short-term memory holds only what you said or a session saw, from a primary
        # session, for a project; the digest refuses anything else, and the rule holds it
        # here as well.
        if new_fact.source not in page.SHORT_TERM_SOURCES or new_fact.scope == "global":
            return reject("R10")
        if not _primary(context.evidence):
            return reject(WORKER_RULE if _only_worker(context.evidence) else LIBRARY_RULE)
        if decision.action == "add":
            return apply("add")
        if (
            decision.action in ("supersede", "conflict")
            and target_line is not None
            and not context.target_recent
        ):
            # A passing state never edits long-term memory: it is a new short-term line.
            return downgrade("add", "R10")

    # A fact resting only on worker sessions is evidence of a lower rank: it stays a
    # candidate until a primary session corroborates it, whatever its tag, and it never
    # confirms a line, so it adds no source and no date to one (design 8).
    #
    # That rule is about the rank of the session that spoke. A quote from an imported collection
    # was spoken by no session at all, so it is not the lower rank design 8 holds: DESIGN 11 gives
    # an authoritative document's own word an [observed] line and sends its facts through R4 like
    # any other, while R3 still counts the quote toward recurrence nothing. Confirming is the one
    # action that is recurrence by itself, and no collection can supply it; the refusal says which
    # kind of evidence came up short, since that is what a person reads in the report.
    if not _primary(context.evidence):
        if decision.action == "confirm":
            return reject(WORKER_RULE if _only_worker(context.evidence) else LIBRARY_RULE)
        if _only_worker(context.evidence):
            return downgrade("candidate", WORKER_RULE)

    if decision.action == "add":
        return _new_line(new_fact, context)

    if target_line is None:
        # Only the three actions that edit a line reach here, and M4.3's schema makes
        # one mandatory on each of them: naming nothing usable is the schema failure it is.
        return reject("M4.3 decision schema")
    if target_line.state in NO_LONGER_MEMORY:
        return reject("R4")  # the table gives no authority over a line that no longer stands
    if target_line.kind is None and decision.action != "confirm":
        # A Candidates line has no kind, so no table row reaches it: nothing replaces it,
        # and a note marks an active line and never a candidate (design 6's fact state).
        # Confirming a candidate is how it gathers evidence toward its gate.
        return reject("R4")

    if decision.action == "confirm" and context.target_recent and new_fact.term == LONG:
        # A later session labels the line long term: it leaves short-term memory (R10).
        # A line under an open note waits for the note to close and is confirmed meanwhile,
        # since the note is the dispute a person has still to settle (design 6).
        outcome: Outcome = apply("confirm") if target_line.callouts else apply("lift")
    elif decision.action == "confirm" and new_fact.term == SHORT and not context.target_recent:
        # R10: a short-term fact confirms a long-term line and does no more; a candidate it
        # confirms gathers the source and waits for long-term evidence to carry it over.
        outcome = apply("confirm")
    elif decision.action == "confirm":
        outcome = _confirm(new_fact, target_line, context)
    elif decision.action == "conflict":
        outcome = _note(new_fact, target_line)
    else:
        outcome = _supersede(new_fact, target_line, context)

    # Last, because which note or strike to write is settled by the rules above: a line
    # whose note is still open carries neither, and the text either would need is one the
    # parser reports, so the writer would refuse the edit and write nothing anyway.
    if _open_note(outcome, target_line):
        return reject("design 6 open note")
    return outcome


def _new_line(new_fact: NewFact, context: Context) -> Outcome:
    """Where an added line goes: R3's gate decides between a kind section and Candidates.
    The line has no footnotes yet, so the gate counts the quotes handed in and nothing
    else."""
    if new_fact.source in NEEDS_THE_GATE and not _corrected(new_fact, context):
        if not _passes_gate(context):
            return downgrade("candidate", "R3")
    return apply("add")


def _confirm(new_fact: NewFact, target_line: page.Fact, context: Context) -> Outcome:
    """Confirming cites the new source on the line and counts toward recurrence.

    On a candidate line it is also where the gate is judged, since a candidate leaves
    Candidates only when its evidence carries it over (design 6, R3). The gate counts the
    footnotes the line would end up citing, so the candidate's own come from the line."""
    if target_line.kind is not None:
        return apply("confirm")
    if _corrected(new_fact, context):
        return apply("promote")  # R3: an explicit correction from you skips the gate
    if target_line.source in NEEDS_THE_GATE:
        return apply("promote") if _passes_gate(context) else apply("confirm")
    # A stated or observed line is in Candidates because the worker rule held it; a
    # primary session corroborating it is what the worker rule asks for.
    if any(quote.verified and quote.origin == PRIMARY for quote in context.evidence):
        return apply("promote")
    return apply("confirm")


def _note(new_fact: NewFact, target_line: page.Fact) -> Outcome:
    """The note a ``conflict`` action asks for. Design 9's table answers one physical
    situation with one note, whichever action named it: an observation contradicting a
    preference or a decision of yours is the pair that goes to needs you, and M4.3's prompt
    defines ``conflict`` for exactly that case."""
    if _refusal(new_fact, target_line) == "needs-you":
        return downgrade("needs-you", "R4")
    return apply("conflict")


def _supersede(new_fact: NewFact, target_line: page.Fact, context: Context) -> Outcome:
    """R4: replacing needs the authority the table gives *and* a verified quote newer than
    the line's own date - the quote check stands in for a second model, so what the fact
    claims about its own date carries no replacement. Without either the old line stands
    and the result is a conflict, or needs you when it is one of your commitments that is
    contradicted."""
    authority = AUTHORITY.get(target_line.kind or "", ())

    # The quote that carries the authority is the one that has to be newer: your own words
    # for a [stated] fact, and for an [observed] one a primary session's quote or the word of
    # an imported document - DESIGN 11 sends a collection's facts through R4 "like any
    # [observed] fact". A worker session's quote carries no such authority (design 8).
    carrying = (
        _yours(context.evidence) if new_fact.source == "stated" else _carrying(context.evidence)
    )
    if new_fact.source in authority and _newer_than_the_line(carrying, target_line):
        return apply("supersede")
    return downgrade(_refusal(new_fact, target_line), "R4")


def _newer_than_the_line(quotes, target_line: page.Fact) -> bool:
    """R4's date half: one of these verified quotes outdates the date on the line (design 9:
    "a verified quote newer than the line's latest evidence"). An unverified quote is no
    evidence of a newer world, and a quote whose date does not parse is none either. The
    line's own date always parses: a line with an unreadable date holds no fact."""
    line_date = _date(target_line.date)
    dated = [d for q in quotes if q.verified if (d := _date(q.date)) is not None]
    return line_date is not None and bool(dated) and max(dated) > line_date


def _yours(evidence: tuple[Evidence, ...]) -> list[Evidence]:
    """The verified quotes in your own words from a primary session: the ones that carry
    your authority, which nothing said in a worker session does (design 8)."""
    return [q for q in _primary(evidence) if q.yours]


def _corrected(new_fact: NewFact, context: Context) -> bool:
    """An explicit correction is your own ``[stated]`` words, backed by a verified quote of
    yours from a primary session. Anything less is not a correction from you."""
    return (
        new_fact.explicit_correction
        and new_fact.source == "stated"
        and bool(_yours(context.evidence))
    )


def _refusal(new_fact: NewFact, target_line: page.Fact) -> str:
    """What a disagreement with a standing line becomes (design 9's table): a conflict
    note, or needs you when it is an observation that contradicts a preference or a
    decision of yours. Both the refused supersede and the conflict note ask it, so the pair
    comes to one outcome whichever action named it."""
    if target_line.kind in ("preference", "decision") and new_fact.source == "observed":
        return "needs-you"
    return "conflict"


def _open_note(outcome: Outcome, target_line: page.Fact) -> bool:
    """Whether the outcome strikes a line, or notes it again, while its note is still
    open. Notes stay open until a person closes them, so this is the ordinary state of a
    line a later run contradicts again (design 6's fact state)."""
    if not target_line.callouts:
        return False
    if isinstance(outcome, Apply):
        return outcome.action in NOTE_OR_STRIKE
    return isinstance(outcome, Downgrade) and outcome.to_action in NOTE_OR_STRIKE


def _passes_gate(context: Context) -> bool:
    """R3: three signals across two primary sessions, signals older than 30 days counting
    half. The two numbers are the writer's own ``counts_for`` over the footnotes the line
    would end up citing that rest on a verified quote from a primary session - the new
    fact's and the line's own - so a worker footnote or an unverified one adds nothing,
    and the weighting is arithmetic on the count it returns. Nothing is read from a page
    or an archive."""
    quotes = _primary(context.evidence) + _primary(context.line_evidence)
    entries = {quote.source: {"resource": quote.resource} for quote in quotes}
    refs = [quote.source for quote in quotes]
    signals, sessions = pagewrite.counts_for(refs, entries)
    aged = sum(1 for ref in dict.fromkeys(refs) if _aged(ref, quotes, context.today))
    return signals - aged / 2 >= SIGNALS_GATE and sessions >= SESSIONS_GATE


def _primary(evidence: tuple[Evidence, ...]) -> list[Evidence]:
    """The verified quotes from primary sessions: a worker session's evidence and a quote
    imported from a collection never carry a fact over the gate, and an unverified quote is no
    signal at all."""
    return [q for q in evidence if q.verified and q.origin == PRIMARY]


def _carrying(evidence: tuple[Evidence, ...]) -> list[Evidence]:
    """The verified quotes an ``[observed]`` fact replaces a standing line on: a primary
    session's, or an imported document's own word.

    DESIGN 11 gives a collection's facts the authority of ``[observed]`` and sends them through
    R4 like any other, which asks for one verified quote newer than the line. A worker session's
    quote is the lower rank design 8 holds back, so it carries nothing, and R3 still counts an
    imported quote toward recurrence nothing: replacement and recurrence are different questions.
    """
    return [q for q in evidence if q.verified and q.origin in (PRIMARY, LIBRARY)]


def _only_worker(evidence: tuple[Evidence, ...]) -> bool:
    """Whether every verified quote rests on a worker session.

    Design 8 ranks the session that spoke, and an imported quote came from no session, so a fact
    resting on a collection is not the lower-ranked evidence this rule was written to hold.
    """
    verified = [q for q in evidence if q.verified]
    return bool(verified) and all(q.origin == WORKER for q in verified)


def _aged(ref: str, quotes: list[Evidence], today: str) -> bool:
    """Whether the newest quote handed in for this source is older than 30 days, which is
    how a source is aged once rather than each quote that cites it. A date that does not
    parse is no proof of a recent signal, and neither is a footnote no quote here carries."""
    seen = [d for q in quotes if q.source == ref if (d := _date(q.date)) is not None]
    run = _date(today)
    return run is None or not seen or (run - max(seen)).days > HALF_WEIGHT_AFTER_DAYS


def _date(value: str) -> date | None:
    """A value as the date the page grammar writes, or None when it is not one."""
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _derivable(statement: str) -> bool:
    """R5's script half: the two shapes a script can name without the repository in front
    of it. Three or more file paths in one statement is a file listing; a fence is a code
    block. A URL, a pair like ``US/UK`` and a version number name neither, and a reference
    is a kind of its own whose pages hold URLs. "Anything git already records" without
    either shape is the digest prompt's job (design 8), and R5's preference for durable
    phrasing over figures that go stale is a preference, not a bar."""
    if "```" in statement:
        return True
    return len(_PATH_TOKEN.findall(statement)) >= 3
