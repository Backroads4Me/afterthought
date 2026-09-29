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
  a quote wherever the edit writes one: the footnotes of a line and any ``[^id]`` inside
  a statement (design 6, design 9 R1). A statement is accepted by
  round trip: the written line has to parse back to exactly that statement, so a statement
  ending in markup that only looks like a fact tail is carried as prose, and only blank
  text, ``~~``, an unresolved citation and a line break are refused by pattern.
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
_SOURCE_ID = re.compile(r"s(?P<n>\d+)")
_FOOTNOTE = re.compile(r"\[\^([A-Za-z0-9_-]+)\]")
"""A ``[^id]`` citation, as ``aft lint`` and the line grammar read it."""


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
    """A frontmatter value as one YAML line: the first form that reads back as itself."""
    for form in (value, json.dumps(value, ensure_ascii=False)):
        try:
            if yaml.safe_load(f"k: {form}") == {"k": value}:
                return form
        except yaml.YAMLError:
            continue
    return json.dumps(value)  # escapes every code point YAML could reinterpret


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


def _check_source_tag(tag: str) -> str:
    if tag not in page.SOURCES:
        raise WriteError(f"unknown source tag [{tag}]; expected {' '.join(page.SOURCES)}")
    return tag


def _section_for(kind: str) -> str:
    section = SECTION_FOR_KIND.get(kind)
    if section is None:
        raise WriteError(f"unknown kind {kind!r}; expected {' '.join(sorted(SECTION_FOR_KIND))}")
    return section


def line_identity(fact: page.Fact) -> tuple[str, str, str, tuple[str, ...], bool]:
    """What a fact line says, apart from the line number it currently sits on."""
    return fact.statement, fact.source, fact.date, fact.refs, fact.struck


_line_identity = line_identity


class PageDoc:
    """A page's text and its parse, with the page writer's operations.

    Read a page in, apply the operations, and write ``render()`` out. Operations take
    the ``page.Fact`` they are about, taken from ``page.facts`` or looked up by
    statement with ``fact()``; since an edit can move lines, a handle whose line has
    moved or changed since it was taken is refused rather than applied to the wrong line.

    A line the parse reports a problem with holds no fact (``page.Page``), so no
    operation is ever handed text outside the grammar to rebuild: ``fact()`` names only
    lines the page read cleanly, and a handle a hand edit made stale is refused.

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
        sources = self.sources
        bounds = self._fm_bounds()
        if bounds is None:
            raise WriteError("the page has no frontmatter to add a source to")
        new_id = _next_id(sources)
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

    def scrub_source(self, source_id: str, marker: str = "[forgotten]") -> bool:
        """Replace the quote and title of a source entry with a marker (design 12, M2.8).

        Footnotes citing this source still resolve, but verbatim text from the forgotten
        session is removed from the frontmatter.
        """
        source_id = str(source_id)
        if source_id not in self.sources:
            raise WriteError(f"source {source_id} is not in the frontmatter")
        bounds = self._fm_bounds()
        if bounds is None:
            raise WriteError("the page has no frontmatter")
        key = self._key_line("sources", bounds)
        if key is None:
            raise WriteError("the page has no sources block")
        last = self._sources_end(key, bounds)

        sources_indent = next(
            (
                match.group("indent")
                for line in self.lines[key + 1 : last + 1]
                if (match := _ENTRY_LINE.match(line))
            ),
            "  ",
        )
        entry_starts: list[int] = [
            idx
            for idx in range(key + 1, last + 1)
            if (m := _ENTRY_LINE.match(self.lines[idx])) and m.group("indent") == sources_indent
        ]

        for k, start in enumerate(entry_starts):
            stop = entry_starts[k + 1] if k + 1 < len(entry_starts) else last + 1
            entry_lines = self.lines[start:stop]
            raw = "\n".join(entry_lines)
            try:
                data = yaml.safe_load(raw)
            except yaml.YAMLError:
                data = None
            entry_dict: dict = {}
            if isinstance(data, list) and data and isinstance(data[0], dict):
                entry_dict = data[0]
            elif isinstance(data, dict):
                entry_dict = data

            if str(entry_dict.get("id")) != source_id:
                continue

            entry_data = self.sources.get(source_id, entry_dict)
            quote_val = entry_data.get("quote")
            title_val = entry_data.get("title")
            text_val = entry_data.get("text")
            has_title = "title" in entry_data or "title" in entry_dict
            has_text = "text" in entry_data or "text" in entry_dict

            if (
                quote_val == marker
                and (not has_title or title_val == marker)
                and (not has_text or text_val == marker)
            ):
                return False

            cont_indent = sources_indent + "  "
            resource = entry_data.get("resource", "")
            new_lines = [
                f"{sources_indent}- id: {source_id}",
                f"{cont_indent}resource: {_scalar(str(resource))}",
            ]
            if has_title:
                new_lines.append(f"{cont_indent}title: {_scalar(marker)}")
            if has_text:
                new_lines.append(f"{cont_indent}text: {_scalar(marker)}")
            new_lines.append(f"{cont_indent}quote: {_scalar(marker)}")

            with self._atomic():
                self._apply(start, stop, new_lines)
            return True

        raise WriteError(f"could not find source {source_id} entry lines in frontmatter")

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
        statement = self._check_statement(statement)
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
        statement = self._check_statement(statement)
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

    def confirm(self, fact: page.Fact, source: str, date: str) -> bool:
        """Cite one more source on a standing line and carry its date (design 9, confirm).

        A line's date is the date it was last evidenced (design 6), so it becomes the later
        of the date it carries and ``date``; an older confirmation adds its footnote and
        leaves the date. A source the line already cites adds nothing. On a candidate the
        gate progress is rewritten from the footnotes it ends up with; whether that carries
        it over the gate is the rules' call, made through ``promote``. A line that no
        longer stands (superseded, withdrawn or stale) takes no evidence.
        """
        found = self._resolve(fact)
        if found.state in (page.SUPERSEDED, page.WITHDRAWN, page.STALE):
            raise WriteError(f"a {found.state} line is no longer memory to confirm")
        (ref,) = self._check_refs((source,))
        refs = found.refs if ref in found.refs else (*found.refs, ref)
        latest = max(found.date, _check_date(date))
        suffix = _counts_suffix(refs, self.sources) if found.section == page.CANDIDATES else ""
        line = _fact_line(found.statement, found.source, latest, refs, suffix=suffix)
        return self._rewrite(found, line)

    def supersede(
        self,
        fact: page.Fact,
        statement: str,
        tag: str,
        date: str,
        refs: Sequence[str],
        *,
        by: str | None = None,
    ) -> page.Fact:
        """Replace an active line with a new one and strike the old one (design 6, 9).

        The old line is struck through and ends ``superseded <date> by [^by]``; the new
        line sits directly above it in the same section, as in design 6's example, carrying
        ``statement`` with ``tag``, ``date`` and ``refs``. ``by`` is the footnote that links
        the struck line to its replacement, so it has to be one the replacement cites; it
        defaults to the replacement's first footnote. ``date`` is both the replacement's
        date last evidenced and the date of the strike, since the evidence that replaced
        the line is what dates the replacement. Whether the replacement has the authority
        is R4's call, made before this is called.
        """
        found = self._resolve(fact)
        if found.section == page.CANDIDATES:
            raise WriteError("a candidate is promoted or left, never superseded")
        if found.state != page.ACTIVE:
            raise WriteError(f"only an active line can be superseded; this one is {found.state}")
        statement = self._check_statement(statement)
        refs = self._check_refs(refs)
        if not refs:
            raise WriteError("a replacement needs a source to cite")
        by = refs[0] if by is None else str(by)
        if by not in refs:
            raise WriteError(f"the replacement has to cite [^{by}], the source that links to it")
        date = _check_date(date)
        new = _fact_line(statement, _check_source_tag(tag), date, refs)
        struck = _fact_line(
            found.statement,
            found.source,
            found.date,
            found.refs,
            struck=True,
            suffix=f" superseded {date} by [^{by}]",
        )
        with self._atomic():
            at = found.line - 1
            self._apply(at, at + 1, [new, struck])
            self._line_reads(at + 2, found.statement, page.SUPERSEDED)
            return self._fact_written(at + 1, statement)

    def withdraw(self, fact: page.Fact, date: str) -> bool:
        """Strike a line out of memory after ``aft forget`` (design 6, withdrawn).

        A stale line can be withdrawn too, and its stale mark gives way to the strike, since
        a withdrawn line is out of memory whatever its anchor says. A line already withdrawn
        is left as it is, keeping the date it was first withdrawn on.
        """
        found = self._resolve(fact)
        date = _check_date(date)
        if found.section == page.CANDIDATES:
            raise WriteError("a candidate line cannot be struck through")
        if found.state == page.WITHDRAWN:
            return False
        if found.state == page.SUPERSEDED:
            raise WriteError("a superseded line is already out of memory")
        if found.callouts:
            raise WriteError("a line with an open note cannot be struck; close the note first")
        line = _fact_line(
            found.statement,
            found.source,
            found.date,
            found.refs,
            struck=True,
            suffix=f" withdrawn {date}",
        )
        return self._rewrite(found, line, page.WITHDRAWN)

    def mark_stale(self, fact: page.Fact, reason: str, date: str) -> bool:
        """Mark an active line stale, ending it ``(stale <date>: <reason>)`` (design 6, R8).

        A line already stale keeps the date and reason it went stale with, so a check that
        finds the same broken anchor on every run changes nothing. A line with an open note
        cannot carry the mark (the parse refuses a note under a stale line), which is R8's
        "only reported until the note is closed".
        """
        found = self._resolve(fact)
        date = _check_date(date)
        reason = reason.strip() if isinstance(reason, str) else ""
        if not reason:
            raise WriteError("a stale mark needs a reason")
        if "\n" in reason or "\r" in reason or ")" in reason:
            raise WriteError("a stale reason is one line without ')'")
        self._check_citations(reason)
        if found.section == page.CANDIDATES:
            raise WriteError("a candidate may end only with its signal counts")
        if found.state == page.STALE:
            return False
        if found.state != page.ACTIVE:
            raise WriteError(f"only an active line can go stale; this one is {found.state}")
        line = _fact_line(
            found.statement,
            found.source,
            found.date,
            found.refs,
            suffix=f" (stale {date}: {reason})",
        )
        return self._rewrite(found, line, page.STALE)

    def clear_stale(self, fact: page.Fact, date: str) -> bool:
        """Take the stale mark off a line whose anchor matches again (R8, M4.5).

        The line goes back to the plain active line it was, with the date it was last
        evidenced unchanged: words reappearing in a file are not a quote, so they are not
        new evidence. ``date`` is the day the anchor matched again, which M4.5 reports; it
        is checked, and the grammar gives it no place on the line. A line that is not stale
        is left as it is.
        """
        found = self._resolve(fact)
        _check_date(date)
        if found.state == page.ACTIVE:
            return False
        if found.state != page.STALE:
            raise WriteError(f"only a stale line has a stale mark; this one is {found.state}")
        line = _fact_line(found.statement, found.source, found.date, found.refs)
        return self._rewrite(found, line, page.ACTIVE)

    def promote(self, candidate: page.Fact, kind: str) -> page.Fact:
        """Move a candidate out of Candidates to become an active fact (design 6).

        The line arrives as a plain fact line: the footnotes travel with it and the gate
        progress they gave stays re-derivable from them, so its counts suffix is left
        behind. Which kind it joins is decided by the rules, so it is required here.
        """
        found = self._resolve(candidate)
        if found.section != page.CANDIDATES:
            raise WriteError("only a candidate line can be promoted")
        section = _section_for(kind)
        line = _fact_line(found.statement, found.source, found.date, found.refs)
        with self._atomic():
            self._apply(found.line - 1, found.line, [])
            at = self._append_line(section, line)
            return self._fact_written(at, found.statement)

    def remove_candidate(self, fact: page.Fact) -> bool:
        """Remove a candidate line resting on forgotten evidence (design 12, M2.8)."""
        found = self._resolve(fact)
        if found.section != page.CANDIDATES:
            raise WriteError("only a candidate line can be removed")
        at = found.line - 1
        idx = at + 1
        while idx < len(self.lines) and not self.lines[idx].strip():
            idx += 1
        if found.callouts or (idx < len(self.lines) and self.lines[idx].lstrip().startswith(">")):
            raise WriteError("a line with an open note cannot be removed; close the note first")
        with self._atomic():
            self._apply(at, at + 1, [])
        return True

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

    def _rewrite(self, found: page.Fact, line: str, state: str | None = None) -> bool:
        """Replace one fact line in place; whether its text changed.

        The new line has to read back as the same statement, in ``state`` when one is
        named and otherwise in the state the line had, or the edit is put back.
        """
        index = found.line - 1
        if line == self.lines[index]:
            return False
        with self._atomic():
            self._apply(index, index + 1, [line])
            self._line_reads(found.line, found.statement, state or found.state)
        return True

    def _line_reads(self, line: int, statement: str, state: str) -> None:
        """Refuse unless ``line`` parses as a fact with this statement in this state."""
        found = next((fact for fact in self._page.facts if fact.line == line), None)
        if found is None or found.statement != statement or found.state != state:
            raw = self.lines[line - 1]
            raise WriteError(f"line {line} does not read back as a {state} {statement!r}: {raw!r}")

    def _resolve(self, fact: page.Fact) -> page.Fact:
        """The fact in the current text this handle names: its line, and every field of
        that line, so a twin carrying the same statement cannot answer for it."""
        wanted = _line_identity(fact)
        for fact_now in self._page.facts:
            if fact_now.line == fact.line and _line_identity(fact_now) == wanted:
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

    def _check_statement(self, statement: str) -> str:
        """A statement as a fact line carries it: one line, no marker, citations resolve."""
        statement = statement.strip()
        if not statement:
            raise WriteError("a fact needs a statement")
        if "\n" in statement or "\r" in statement:
            raise WriteError("a fact statement is one line")
        if "~~" in statement:
            raise WriteError("a fact statement cannot contain ~~")
        self._check_citations(statement)
        return statement

    def _check_citations(self, text: str) -> None:
        """The ``[^id]`` citations inside text this writer puts onto a page.

        Design 6 makes a footnote name a source of its page wherever it sits, and ``aft
        lint`` reads the footnotes over the page as written, so a ``[^id]`` inside a
        statement has to resolve just as the footnotes at the end of a line do.
        """
        self._check_refs(_FOOTNOTE.findall(text))

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
        # Design 6 keeps a blank line under a section header, and a note belongs to the
        # fact above it, so a fact appended after either starts below a blank line.
        gap = at == header + 1 or self.lines[at - 1].rstrip().startswith(">")
        self._apply(at, at, ["", line] if gap else [line])
        return at + (2 if gap else 1)

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
        """The line just past a section's content, before the blank lines that follow."""
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
