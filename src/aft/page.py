"""Pages: OKF frontmatter plus a body of fact lines (design section 6).

A fact page's body is a series of ``##`` sections. Each kind section holds fact lines::

    - <statement> [<source>] <YYYY-MM-DD> [^id] ...[ <state suffix>]

The state suffix, strikethrough and any callout that follows a line decide its state.
The parser reports every problem with its line number and never raises on bad input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

SOURCES = ("stated", "observed", "inferred", "suggested")

KIND_SECTIONS = {
    "Preferences": "preference",
    "Decisions": "decision",
    "State": "state",
    "Facts": "fact",
    "Lessons": "lesson",
    "Procedures": "procedure",
    "References": "reference",
}
CANDIDATES = "Candidates"
# Sections of generated prose rather than facts, allowed only on the page types listed.
PROSE_SECTIONS = {
    "Contents": {"Reference"},
    "Description": {"Reference"},
    "Resolved notes": {"Archive"},
    "Notes": {"Archive"},
}

CANDIDATE = "candidate"
ACTIVE = "active"
SUPERSEDED = "superseded"
WITHDRAWN = "withdrawn"
STALE = "stale"
CONFLICT = "conflict"
NEEDS_YOU = "needs-you"

# The note types a fact line can carry, and the state each one writes. This is the
# table the parser reads, so it is the table a writer writes too.
CALLOUTS = {
    "conflict": CONFLICT,
    "question": NEEDS_YOU,
}
_REF = r"\[\^[A-Za-z0-9_-]+\]"
_TAIL = (
    r"\[(?P<source>[a-z]+)\] (?P<date>\d{4}-\d{2}-\d{2})"
    rf"(?P<refs>(?: {_REF})+)"
    r"(?P<suffix>.*)$"
)
_ACTIVE_LINE = re.compile(rf"^(?P<statement>.+?) {_TAIL}")
_STRUCK_LINE = re.compile(rf"^~~(?P<statement>.+?)~~ {_TAIL}")
_SUPERSEDED = re.compile(rf"^ superseded (?P<date>\d{{4}}-\d{{2}}-\d{{2}}) by (?P<by>{_REF})$")
_WITHDRAWN = re.compile(r"^ withdrawn (?P<date>\d{4}-\d{2}-\d{2})$")
_STALE = re.compile(r"^ \(stale (?P<date>\d{4}-\d{2}-\d{2}): (?P<reason>[^)]+)\)$")
_COUNTS = re.compile(r"^ \(signals (\d+)/(\d+), sessions (\d+)/(\d+)\)$")
_CALLOUT_START = re.compile(r"^> \[!(?P<type>[a-z]+)\] (?P<date>\d{4}-\d{2}-\d{2})\s*$")


@dataclass
class Callout:
    type: str
    date: str
    line: int
    text: list[str] = field(default_factory=list)


@dataclass
class Fact:
    line: int
    section: str
    kind: str | None
    """The fact's kind from its section, or None for a candidate."""

    statement: str
    source: str
    date: str
    refs: tuple[str, ...]
    struck: bool
    state: str = ACTIVE
    state_date: str | None = None
    superseded_by: str | None = None
    reason: str | None = None
    signals: tuple[int, int] | None = None
    sessions: tuple[int, int] | None = None
    callouts: list[Callout] = field(default_factory=list)


@dataclass
class Page:
    """The memory one page holds, and every problem the parser found reading it.

    `facts` is what a consumer acts on. A line the parser reports a problem with - an
    unreadable tail, an unknown source tag, an invalid date, a marker or a note that writes
    no state - is named in `errors` and reaches nothing: a refused fact line holds no fact,
    a refused note marks no fact. So every fact here carries the state its own whole line
    and whole notes wrote, and a consumer reading `facts` and `Fact.state` never acts on a
    fact line the parse complained about. A page with entries in `errors` is a broken page:
    `aft lint` reports each one and checks the page's footnotes over its text as written,
    which still holds the refused lines.
    """

    path: Path
    frontmatter: dict | None
    body_start: int
    """The file line number of the body's first line."""

    facts: list[Fact] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    errors: list[tuple[int, str]] = field(default_factory=list)
    rejected: list[tuple[int, str, str]] = field(default_factory=list)
    """The fact lines the parser refused, each as (file line, the line as written, the reason).

    A refused line holds no fact, so it is in none of `facts` and nothing that reads facts shows
    it. It stays visible here so a script can tell a line the grammar refuses from a page that
    simply has none, and so a hand-typed line is never dropped without a trace (BUILD-PLAN F2).
    """


def split_frontmatter(text: str) -> tuple[dict | None, str, int, str | None]:
    """Return (frontmatter, body, body start line, error)."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, text, 1, None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            raw = "\n".join(lines[1:index])
            body = "\n".join(lines[index + 1 :])
            try:
                data = yaml.safe_load(raw) if raw.strip() else {}
            except yaml.YAMLError as exc:
                return None, body, index + 2, f"frontmatter is not valid YAML: {exc}"
            if not isinstance(data, dict):
                return None, body, index + 2, "frontmatter must be a mapping"
            return data, body, index + 2, None
    return None, text, 1, "frontmatter has no closing ---"


def parse(text: str, path: Path) -> Page:
    """Parse a fact page."""
    frontmatter, body, start, error = split_frontmatter(text)
    page = Page(path=path, frontmatter=frontmatter, body_start=start)
    if error:
        page.errors.append((1, error))
    page_type = (frontmatter or {}).get("type")
    section: str | None = None
    last_fact: Fact | None = None
    fact_refused = False
    callout: Callout | None = None
    in_note = False
    for offset, line in enumerate(body.split("\n")):
        number = start + offset
        stripped = line.rstrip()
        note_line = stripped.startswith(">")
        if in_note and note_line:
            # The lines under a note header are text someone typed: absorbed into the note
            # block and never reported as a header of their own.
            if callout is not None:
                callout.text.append(stripped[1:].strip())
            continue
        in_note = note_line
        callout = None
        if not stripped:
            continue
        if stripped.startswith("# "):
            continue
        if stripped.startswith("## "):
            section = stripped[3:].strip()
            page.sections.append(section)
            last_fact = None
            fact_refused = False
            if (
                section not in KIND_SECTIONS
                and section != CANDIDATES
                and page_type not in PROSE_SECTIONS.get(section, set())
            ):
                page.errors.append((number, f"unknown section '{section}'"))
            continue
        if section is None:
            page.errors.append((number, "text before the first section; facts go in sections"))
            continue
        if section not in KIND_SECTIONS and section != CANDIDATES:
            continue  # prose section, or an unknown one already reported
        if note_line:
            callout = _start_callout(page, stripped, number, last_fact, fact_refused)
            continue
        if stripped.startswith("- "):
            reported = len(page.errors)
            fact = _parse_fact(page, stripped[2:], number, section)
            fact_refused = fact is None
            if fact is None:
                page.rejected.append((number, stripped, _refusal(page, reported)))
        else:
            message = "only fact lines are allowed in a fact section"
            page.errors.append((number, message))
            page.rejected.append((number, stripped, message))
            fact = None
            fact_refused = False
        # Only a line the parser accepted holds a fact, so a note after a hand-typed
        # sentence or a refused line has no fact to mark and is reported rather than
        # marking the fact above it.
        last_fact = fact
        if fact is not None:
            page.facts.append(fact)
    return page


def _refusal(page: Page, reported: int) -> str:
    """Join what the parser reported about one refused line into the one reason it holds."""
    return "; ".join(message for _, message in page.errors[reported:])


def _start_callout(
    page: Page, line: str, number: int, fact: Fact | None, fact_refused: bool
) -> Callout | None:
    """Mark the fact with a note; None when the note is refused and writes no state."""
    match = _CALLOUT_START.match(line)
    if not match or match.group("type") not in CALLOUTS:
        page.errors.append((number, "expected '> [!conflict] YYYY-MM-DD' or '> [!question] ...'"))
        return None
    if not _valid_date(match.group("date")):
        page.errors.append((number, f"invalid date {match.group('date')}"))
        return None
    if fact is None:
        page.errors.append(
            (
                number,
                "a callout cannot mark a refused fact line"
                if fact_refused
                else "a callout marks the fact line directly above it",
            )
        )
        return None
    if fact.state != ACTIVE:
        page.errors.append((number, f"a callout cannot follow a {fact.state} fact"))
        return None
    callout = Callout(match.group("type"), match.group("date"), number)
    fact.callouts.append(callout)
    fact.state = CALLOUTS[callout.type]
    return callout


def _parse_fact(page: Page, text: str, number: int, section: str) -> Fact | None:
    """Read one fact line; None when the parser reports any problem with it.

    A line reported against is refused whole, whichever part of it produced the report, so
    nothing reaches `Page.facts` carrying a complaint about its own line.
    """
    reported = len(page.errors)
    struck = text.startswith("~~")
    match = (_STRUCK_LINE if struck else _ACTIVE_LINE).match(text)
    if not match:
        page.errors.append(
            (number, "fact line must read '- <statement> [<source>] YYYY-MM-DD [^id]'")
        )
        return None
    source = match.group("source")
    if source not in SOURCES:
        page.errors.append((number, f"unknown source tag [{source}]"))
    if not _valid_date(match.group("date")):
        page.errors.append((number, f"invalid date {match.group('date')}"))
    refs = tuple(re.findall(r"\[\^([A-Za-z0-9_-]+)\]", match.group("refs")))
    candidate = section == CANDIDATES
    fact = Fact(
        line=number,
        section=section,
        kind=None if candidate else KIND_SECTIONS[section],
        statement=match.group("statement").strip(),
        source=source,
        date=match.group("date"),
        refs=refs,
        struck=struck,
        state=CANDIDATE if candidate else ACTIVE,
    )
    _apply_suffix(page, fact, match.group("suffix"), number)
    return None if len(page.errors) > reported else fact


def _apply_suffix(page: Page, fact: Fact, suffix: str, number: int) -> None:
    """Write the state the line's marker carries, reporting a problem with line or marker."""
    candidate = fact.section == CANDIDATES
    if candidate:
        if fact.struck:
            page.errors.append((number, "a candidate cannot be struck through"))
            return
        if suffix:
            counts = _COUNTS.match(suffix)
            if counts:
                values = [int(v) for v in counts.groups()]
                fact.signals = (values[0], values[1])
                fact.sessions = (values[2], values[3])
            else:
                page.errors.append((number, "a candidate may end only with its signal counts"))
        return
    if fact.struck:
        superseded = _SUPERSEDED.match(suffix)
        withdrawn = _WITHDRAWN.match(suffix)
        if superseded:
            fact.state = SUPERSEDED
            fact.state_date = superseded.group("date")
            fact.superseded_by = superseded.group("by")[2:-1]
        elif withdrawn:
            fact.state = WITHDRAWN
            fact.state_date = withdrawn.group("date")
        else:
            page.errors.append(
                (
                    number,
                    "a struck fact must end 'superseded <date> by [^id]' or 'withdrawn <date>'",
                )
            )
            return
        if not _valid_date(fact.state_date):
            page.errors.append((number, f"invalid date {fact.state_date}"))
        return
    if not suffix:
        return
    stale = _STALE.match(suffix)
    if stale:
        fact.state = STALE
        fact.state_date = stale.group("date")
        fact.reason = stale.group("reason")
        if not _valid_date(fact.state_date):
            page.errors.append((number, f"invalid date {fact.state_date}"))
        return
    page.errors.append((number, f"unrecognized text after the sources: {suffix.strip()!r}"))


def _valid_date(value: str | None) -> bool:
    try:
        date.fromisoformat(value or "")
    except ValueError:
        return False
    return True
