"""Edit operations on a page, so an edit changes only the lines it touches.

Design 6 defines the page format and the written form of each fact state; this module
is how the dream's rules (design 9, M4.4) put those states into a page. A page is held
as the lines it was read as, next to the parse of those lines from ``aft.page``. Each
operation rewrites the line or two it is about and inserts any new lines beside them,
so every other line keeps its bytes: statements are never reflowed, frontmatter is
never re-serialised, and blank lines, comments and hand-written text stay put.

Two rules keep the result valid:

* Inputs are checked against the grammar before they are used, so a line is never
  written that ``page.parse`` would reject or that ``aft.lint`` would report: a known
  kind, a known source tag, a real date, and footnotes whose source exists and carries
  a quote, on the line (design 9 R1). A statement is accepted by
  round trip: the written line has to parse back to exactly that statement, so a statement
  ending in markup that only looks like a fact tail is carried as prose, and only blank
  text, ``~~`` and a line break are refused by pattern.
* The text an edit would produce is parsed before anything is written, and the page's
  parse errors have to come out the set they were. An error the edit would add is a line
  the grammar rejects; an error the edit would make disappear is the edit deleting text
  that lies outside the grammar, which is text the tool did not write (design 6). Either
  way the edit is refused and nothing is written, so a page that arrives broken stays
  broken until a person fixes it and a good one never leaves the writer broken.

Nothing here decides what a fact should become. Which operation a decision turns into,
and when a candidate has earned promotion, is the rules' job; the writer only records
the state it is told to record.
"""

from __future__ import annotations

import datetime
import json
import re
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import yaml

from aft import page

SIGNALS_GATE = 3
SESSIONS_GATE = 2
"""Gate progress is written as ``(signals a/3, sessions c/2)``: design 9 R3 asks for 3
signals across 2 sessions before a candidate leaves the Candidates section."""

SECTION_FOR_KIND = {kind: section for section, kind in page.KIND_SECTIONS.items()}
SECTION_ORDER = (*page.KIND_SECTIONS, page.CANDIDATES)
"""The sections of a fact page, in the order design 6 lists them: the kinds, then
Candidates. A section the page lacks is created at this rank."""

_ENTRY_LINE = re.compile(r"^(?P<indent>[ \t]*)-[ \t]")
_PLAIN_YAML = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ ./#:+~=@,;-]*$")
_SOURCE_ID = re.compile(r"s(?P<n>\d+)")


class WriteError(Exception):
    """An edit that cannot be made on this page, or that would not survive a parse."""


def counts_for(refs: Sequence[str], sources: dict[str, dict]) -> tuple[int, int]:
    """The gate progress of a line with these footnotes: (signals, sessions).

    Design 6 derives the evidence counts of a line from its footnotes, so both numbers
    come from the source entries it cites. Each source is one signal; sources written
    by the same session count once toward sessions, where a session is the part of the
    resource URI before the entry fragment (design 6, sources). A footnote with no
    source behind it is a lint error, and here one signal with no session.
    """
    unique = list(dict.fromkeys(refs))
    resources = [sources[ref].get("resource") for ref in unique if ref in sources]
    sessions = {_session_of(str(resource)) for resource in resources if resource}
    return len(unique), len(sessions)


def _session_of(resource: str) -> str:
    return resource.split("#", 1)[0].removeprefix("aft://")


def _counts_suffix(refs: Sequence[str], sources: dict[str, dict]) -> str:
    signals, sessions = counts_for(refs, sources)
    return f" (signals {signals}/{SIGNALS_GATE}, sessions {sessions}/{SESSIONS_GATE})"


def _fact_line(
    statement: str,
    source: str,
    date: str,
    refs: Sequence[str],
    *,
    struck: bool = False,
    suffix: str = "",
) -> str:
    """One fact line, as the grammar in design 6 reads it."""
    body = f"~~{statement}~~" if struck else statement
    line = f"- {body} [{source}] {date}" + "".join(f" [^{ref}]" for ref in refs)
    return line + suffix


def _scalar(value: str) -> str:
    """A frontmatter value as one YAML line: plain where that round-trips, else quoted."""
    if _PLAIN_YAML.match(value):
        try:
            if yaml.safe_load(value) == value:
                return value
        except yaml.YAMLError:
            pass
    return json.dumps(value, ensure_ascii=False)


def _next_id(sources: dict[str, dict]) -> str:
    highest = max(
        (int(m.group("n")) for sid in sources if (m := _SOURCE_ID.fullmatch(str(sid)))),
        default=0,
    )
    return f"s{highest + 1}"


def _today() -> str:
    return datetime.date.today().isoformat()


def _valid_date(value: str) -> bool:
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _check_date(value: str) -> str:
    if not _valid_date(value):
        raise WriteError(f"{value!r} is not a YYYY-MM-DD date")
    return value


def _check_statement(statement: str) -> str:
    """A statement a page can carry on one line: not blank, no line break, no marker."""
    statement = statement.strip()
    if not statement:
        raise WriteError("a fact needs a statement")
    if "\n" in statement or "\r" in statement:
        raise WriteError("a fact statement is one line")
    if "~~" in statement:
        raise WriteError("a fact statement cannot contain ~~")
    return statement


def _check_source_tag(tag: str) -> str:
    if tag not in page.SOURCES:
        raise WriteError(f"unknown source tag [{tag}]; expected {' '.join(page.SOURCES)}")
    return tag


def _section_for(kind: str) -> str:
    section = SECTION_FOR_KIND.get(kind)
    if section is None:
        raise WriteError(f"unknown kind {kind!r}; expected {' '.join(sorted(SECTION_FOR_KIND))}")
    return section


def _guard_state(fact: page.Fact, allowed: tuple[str, ...], action: str) -> None:
    if fact.state not in allowed:
        raise WriteError(f"cannot {action} a {fact.state} fact; expected {' or '.join(allowed)}")


def _guard_struck(fact: page.Fact, action: str) -> None:
    if fact.struck:
        raise WriteError(f"cannot {action} a struck line; it carries its own marker")


class PageDoc:
    """A page's text and its parse, with the page writer's operations.

    Read a page in, apply the operations, and write ``render()`` out. Operations take
    the ``page.Fact`` they are about, taken from ``facts`` or looked up by statement
    with ``fact()``; since an edit can move lines, a fact from before the last edit is
    refused rather than applied to the wrong line.

    Operations that create or move a line return its new ``page.Fact``, because its
    line number has changed; operations that edit a line in place return whether the
    text changed, so a run can tell an edit from a no-op.
    """

    def __init__(self, text: str, path: Path | str = "page.md") -> None:
        self.path = Path(path)
        self.lines: list[str] = text.split("\n")
        self._page = page.parse(text, self.path)

    @classmethod
    def read(cls, path: Path) -> PageDoc:
        return cls(path.read_text(), path)

    def render(self) -> str:
        return "\n".join(self.lines)

    @property
    def page(self) -> page.Page:
        """The parse of the current text."""
        return self._page

    @property
    def facts(self) -> list[page.Fact]:
        return self._page.facts

    @property
    def sections(self) -> list[str]:
        return self._page.sections

    @property
    def sources(self) -> dict[str, dict]:
        """The frontmatter's ``sources`` entries, by id.

        A ``---`` block the parser could not read is refused here rather than read as
        empty, so no operation writes an id or a count against sources it cannot see.
        """
        frontmatter, _, _, error = page.split_frontmatter(self.render())
        if frontmatter is None and error:
            raise WriteError(f"the page's sources are unreadable: {error}")
        return {
            str(entry["id"]): entry
            for entry in (frontmatter or {}).get("sources") or []
            if isinstance(entry, dict) and entry.get("id") is not None
        }

    # Operations

    def add_source(self, resource: str, *, title: str = "", quote: str = "") -> str:
        """Add a source to the frontmatter and return its id (design 6, sources).

        The entry is appended below the ones already there in their own indentation;
        the rest of the frontmatter is not rewritten. Its id is the next free ``s<n>``,
        which is how the example in design 6 numbers them.
        """
        if not isinstance(resource, str) or not resource.strip():
            raise WriteError("a source needs a resource")
        if not isinstance(quote, str) or not quote.strip():
            raise WriteError("a source needs a quote to cite")
        if title and (not isinstance(title, str) or not title.strip()):
            raise WriteError("a source title is text or nothing")
        bounds = self._fm_bounds()
        if bounds is None:
            raise WriteError("the page has no frontmatter to add a source to")
        new_id = _next_id(self.sources)
        entry = [f"- id: {new_id}", f"  resource: {_scalar(resource)}"]
        if title:
            entry.append(f"  title: {_scalar(title)}")
        entry.append(f"  quote: {_scalar(quote)}")
        key = self._key_line("sources", bounds)
        if key is None:
            # Sources come before generated, status and aft, as in the page in design 6.
            at = bounds[1] + 1
            for later in ("generated", "status", "aft"):
                line = self._key_line(later, bounds)
                if line is not None:
                    at = min(at, line)
            self._apply(at, at, ["sources:", *[f"  {line}" for line in entry]])
            return new_id
        value = self.lines[key].partition(":")[2].strip()
        if value and value != "[]":
            raise WriteError("cannot append to an inline sources list")
        last = self._sources_end(key, bounds)
        if value:  # `sources: []`, the empty flow list the vault starts with
            self._apply(key, key + 1, ["sources:", *[f"  {line}" for line in entry]])
            return new_id
        indent = next(
            (
                match.group("indent")
                for line in self.lines[key + 1 : last + 1]
                if (match := _ENTRY_LINE.match(line))
            ),
            "  ",
        )
        self._apply(last + 1, last + 1, [f"{indent}{line}" for line in entry])
        return new_id

    def add_fact(
        self,
        kind: str,
        statement: str,
        tag: str,
        date: str,
        refs: Sequence[str],
    ) -> page.Fact:
        """Add an active fact line under the section of ``kind`` (design 6).

        ``date`` is the last date the fact was evidenced, which is what a line's date
        holds; the caller knows it from the evidence, so it is never guessed here.
        """
        statement = _check_statement(statement)
        line = _fact_line(
            statement,
            _check_source_tag(tag),
            _check_date(date),
            self._check_refs(refs),
        )
        with self._atomic():
            at = self._append_line(_section_for(kind), line)
            return self._fact_written(at, statement)

    def add_candidate(self, statement: str, tag: str, date: str, refs: Sequence[str]) -> page.Fact:
        """Add a line to Candidates, with the gate progress its footnotes give it."""
        refs = self._check_refs(refs)
        statement = _check_statement(statement)
        line = _fact_line(
            statement,
            _check_source_tag(tag),
            _check_date(date),
            refs,
            suffix=_counts_suffix(refs, self.sources),
        )
        with self._atomic():
            at = self._append_line(page.CANDIDATES, line)
            return self._fact_written(at, statement)

    def update_counts(self, candidate: page.Fact) -> bool:
        """Rewrite a candidate's gate progress from its footnotes (design 6)."""
        found = self._resolve(candidate)
        if found.section != page.CANDIDATES:
            raise WriteError("gate progress belongs to a candidate line")
        _guard_struck(found, "update the counts of")
        index = found.line - 1
        line = _fact_line(
            found.statement,
            found.source,
            found.date,
            found.refs,
            suffix=_counts_suffix(found.refs, self.sources),
        )
        if line == self.lines[index]:
            return False
        self._apply(index, index + 1, [line])
        return True

    def promote(self, candidate: page.Fact, kind: str) -> page.Fact:
        """Move a candidate out of Candidates to become an active fact (design 6).

        The line arrives as a plain fact line: the footnotes travel with it and the gate
        progress they gave stays re-derivable from them, so its counts suffix is left
        behind. Which kind it joins is decided by the rules, so it is required here.
        """
        found = self._resolve(candidate)
        if found.section != page.CANDIDATES:
            raise WriteError("only a candidate line can be promoted")
        _guard_struck(found, "promote")
        section = _section_for(kind)
        line = _fact_line(found.statement, found.source, found.date, found.refs)
        with self._atomic():
            self._apply(found.line - 1, found.line, [])
            at = self._append_line(section, line)
            return self._fact_written(at, found.statement)

    # Lines and frontmatter

    def fact(self, statement: str) -> page.Fact:
        """The line with this statement, to name a fact again after lines have moved."""
        found = [fact for fact in self._page.facts if fact.statement == statement]
        if not found:
            raise WriteError(f"no fact line {statement!r} on this page")
        if len(found) > 1:
            raise WriteError(f"{len(found)} lines read {statement!r}; pass the Fact instead")
        return found[0]

    def _fact_written(self, line: int, statement: str) -> page.Fact:
        """The line just written, which has to parse back to the statement it was given.

        A statement is accepted by round trip (design 6): what the written line reads as
        is what the page will say from now on, so a statement the grammar re-reads as a
        different one is refused and the line is put back.
        """
        found = next((fact for fact in self._page.facts if fact.line == line), None)
        if found is None:
            raise WriteError(f"line {line} does not parse as a fact")
        if found.statement != statement:
            raise WriteError(
                f"the line does not read back as {statement!r}; it reads {found.statement!r}"
            )
        return found

    def _resolve(self, fact: page.Fact) -> page.Fact:
        """The same fact in the current text."""
        for fact_now in self._page.facts:
            if fact_now.line == fact.line and fact_now.statement == fact.statement:
                return fact_now
        raise WriteError(f"the fact at line {fact.line} moved; take it from the page again")

    def _check_refs(self, refs: Sequence[str]) -> tuple[str, ...]:
        """Footnotes that are usable on this line: defined, quoted, and without repeats."""
        sources = self.sources
        out: list[str] = []
        for ref in refs:
            ref = str(ref)
            if ref in out:
                continue
            entry = sources.get(ref)
            if entry is None:
                raise WriteError(f"source {ref} is not in the frontmatter; add it first")
            if not isinstance(entry.get("quote"), str) or not entry["quote"].strip():
                raise WriteError(f"source {ref} has no quote to cite")
            out.append(ref)
        return tuple(out)

    @contextmanager
    def _atomic(self) -> Iterator[None]:
        """Put back every line of an operation that is more than one edit.

        ``_apply`` refuses its own edit before it writes; this covers the operation as a
        whole, so a refusal once an earlier edit is in place - a later edit, a statement
        that does not read back, a move whose new line would not parse - cannot leave that
        earlier edit behind.
        """
        was, was_page = list(self.lines), self._page
        try:
            yield
        except Exception:
            self.lines[:] = was
            self._page = was_page
            raise

    def _apply(self, start: int, stop: int, new: Sequence[str]) -> None:
        """Replace lines ``start`` to ``stop`` (0-based) with ``new``, or refuse.

        The text the edit would produce is parsed before any of it is written, and the
        page's parse errors have to come out the set they were. An error the edit would
        add is a line the grammar rejects. An error the edit would make disappear is the
        edit deleting the bytes that error flagged, which is text outside the grammar the
        tool did not write, so a page that arrives broken stays broken until a person
        fixes what the error names. Nothing is written either way. The refusal names the
        flagged lines whose text the edit would take, the lines it replaces first.
        """
        known = Counter(message for _, message in self._page.errors)
        lines = [*self.lines[:start], *new, *self.lines[stop:]]
        now = page.parse("\n".join(lines), self.path)
        errors = Counter(message for _, message in now.errors)
        added = errors - known
        if added:
            raise WriteError("; ".join(sorted(added)))
        gone = known - errors
        if gone:
            still = dict(gone)
            flagged = []
            lost = sorted(
                [(line, message) for line, message in self._page.errors if message in still],
                key=lambda hit: not (start + 1 <= hit[0] <= stop),
            )
            for line, message in lost:
                if still[message]:
                    still[message] -= 1
                    flagged.append(f"line {line}: {message}")
            detail = "; ".join(flagged)
            raise WriteError(f"this edit would drop text outside the grammar: {detail}")
        self.lines[:] = lines
        self._page = now

    def _append_line(self, section: str, line: str) -> int:
        """Put one fact line under the end of ``section``, creating it if need be."""
        header = self._header_line(section)
        if header is None:
            return self._insert_section(section, line)
        at = self._section_end(header)
        empty = at == header + 1
        self._apply(at, at, ["", line] if empty else [line])
        return at + (2 if empty else 1)

    def _insert_section(self, section: str, line: str) -> int:
        """Create ``section`` where design 6 orders it and put ``line`` under it."""
        block = [f"## {section}", "", line]
        rank = SECTION_ORDER.index(section)
        for header, name in self._headings():
            if name in SECTION_ORDER and SECTION_ORDER.index(name) > rank:
                self._apply(header, header, [*block, ""])
                return header + 3
        at = len(self.lines)
        while at > 0 and not self.lines[at - 1].strip():
            at -= 1
        self._apply(at, at, ["", *block])
        return at + 4

    def _headings(self) -> list[tuple[int, str]]:
        """The ``##`` headings of the body, as the parser reads them."""
        found = []
        for index in range(self._page.body_start - 1, len(self.lines)):
            stripped = self.lines[index].rstrip()
            if stripped.startswith("## "):
                found.append((index, stripped[3:].strip()))
        return found

    def _header_line(self, section: str) -> int | None:
        return next(
            (index for index, name in self._headings() if name == section),
            None,
        )

    def _section_end(self, header: int) -> int:
        """The line just past a section's facts, before the blank lines that follow."""
        index = header + 1
        while index < len(self.lines) and not self.lines[index].rstrip().startswith("## "):
            index += 1
        while index > header + 1 and not self.lines[index - 1].strip():
            index -= 1
        return index

    def _fm_bounds(self) -> tuple[int, int] | None:
        """The first and last content lines of the frontmatter block, 0-based."""
        if not self.lines or self.lines[0].strip() != "---":
            return None
        for index in range(1, len(self.lines)):
            if self.lines[index].strip() == "---":
                return (1, index - 1)
        return None

    def _key_line(self, key: str, bounds: tuple[int, int]) -> int | None:
        """A top-level frontmatter key: a line starting at column zero, not indented."""
        return next(
            (
                index
                for index in range(bounds[0], bounds[1] + 1)
                if self.lines[index].startswith(f"{key}:")
            ),
            None,
        )

    def _sources_end(self, key: int, bounds: tuple[int, int]) -> int:
        """The last line of the ``sources`` block, which a new entry goes after."""
        last = key
        for index in range(key + 1, bounds[1] + 1):
            line = self.lines[index].strip()
            if not line:
                continue  # a blank line here belongs to whatever comes next
            if self.lines[index][:1] in (" ", "\t") or line.startswith("-"):
                last = index
            else:
                break  # the next top-level key ends the block
        return last
