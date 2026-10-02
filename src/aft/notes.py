"""Notes waiting on the user, and settling them (BUILD-PLAN F42, DESIGN 6 and 9).

The dream writes a ``[!conflict]`` or ``[!question]`` note under a line when newer evidence
disagrees with it and has no authority to replace it. Such a note waits for the user. This
module finds the notes (``open_notes``, ``count``), and settles one the way the user decides
(``settle``): the line stays, the line is withdrawn, or the line is replaced by a statement
resting on the note's own evidence. Whichever it is, the note itself moves, word for word, to
the page's archive, so nothing a note recorded is lost. The script does the bookkeeping; the
agent only explains each note and asks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from aft import pagewrite
from aft import vault as vaultmod
from aft.config import Config

KEEP = "keep"
WITHDRAW = "withdraw"
REPLACE = "replace"
CHOICES = (KEEP, WITHDRAW, REPLACE)

_NOTE_HEAD = re.compile(r"^> \[!(?:conflict|question)\]", re.MULTILINE)
_REF = re.compile(r"\[\^([A-Za-z0-9_-]+)\]")


class NoteError(Exception):
    """A note that cannot be found or settled as asked."""


@dataclass(frozen=True)
class Source:
    id: str
    resource: str
    quote: str
    date: str
    yours: bool
    verified: bool


@dataclass(frozen=True)
class Note:
    """One open note and what an agent needs to explain it."""

    page: str
    """The page's vault-relative path."""

    line: int
    """The line number of the fact the note sits under, which ``settle`` takes."""

    kind: str | None
    statement: str
    tag: str
    date: str
    note_type: str
    """``conflict`` or ``question``."""

    note_date: str
    note_text: list[str]
    line_sources: list[Source] = field(default_factory=list)
    note_sources: list[Source] = field(default_factory=list)

    @property
    def where(self) -> str:
        return f"{self.page}:{self.line}"


def pages(config: Config, slug: str | None, everywhere: bool = False) -> list[Path]:
    """The fact pages whose notes a session in ``slug`` is asked about: the project's own and
    the global pages, or only the global pages for a session of no project. ``everywhere``
    takes every project's pages as well."""
    vault = config.vault
    if everywhere:
        roots = sorted((vault / "projects").glob("*"))
    else:
        roots = [vault / "projects" / slug] if slug else []
    found = [
        path
        for root in roots
        if root.is_dir()
        for path in sorted(root.glob("*.md"))
        if path.name != "index.md"
    ]
    return found + sorted((vault / "global").glob("*.md"))


def count(config: Config, slug: str | None, everywhere: bool = False) -> int:
    """How many notes wait in those pages. Text only, no parse, so the session-start hook
    can afford it (DESIGN 10)."""
    total = 0
    for path in pages(config, slug, everywhere):
        try:
            total += len(_NOTE_HEAD.findall(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue
    return total


def open_notes(config: Config, slug: str | None, everywhere: bool = False) -> list[Note]:
    """Every open note in those pages, with the line it sits under and the quotes behind both."""
    found: list[Note] = []
    for path in pages(config, slug, everywhere):
        try:
            doc = pagewrite.PageDoc.read(path, vault=config.vault)
        except (OSError, UnicodeDecodeError, pagewrite.WriteError):
            continue
        sources = doc.sources
        rel = path.relative_to(config.vault).as_posix()
        for fact in doc.page.facts:
            for callout in fact.callouts:
                text = [row.removeprefix(">").strip() for row in callout.text]
                note_refs = [r for row in text for r in _REF.findall(row)]
                found.append(
                    Note(
                        page=rel,
                        line=fact.line,
                        kind=fact.kind,
                        statement=fact.statement,
                        tag=fact.source,
                        date=fact.date,
                        note_type=callout.type,
                        note_date=callout.date,
                        note_text=[row for row in text if row],
                        line_sources=_sources(config, sources, fact.refs, fact.date),
                        note_sources=_sources(config, sources, note_refs, callout.date),
                    )
                )
    return found


def settle(
    config: Config,
    where: str,
    choice: str,
    statement: str | None = None,
    now: datetime | None = None,
) -> str:
    """Settle the note under the line at ``where`` (``<page>:<line>``) as the user chose, and
    commit it. Returns the commit message.

    ``keep`` leaves the line standing. ``withdraw`` strikes it through, dated today.
    ``replace`` supersedes it with ``statement``, cited to the note's own sources: tagged
    ``[stated]`` when one of them is the user's own words and ``[observed]`` otherwise, and
    dated by the newest of them. A note with no sources cannot carry a replacement.
    """
    if choice not in CHOICES:
        raise NoteError(f"unknown choice {choice!r}; expected one of {', '.join(CHOICES)}")
    if (choice == REPLACE) != bool(statement and statement.strip()):
        raise NoteError("--replace takes the new statement, and only --replace does")
    rel, line = _where(where)
    path = config.vault / rel
    if not path.is_file():
        raise NoteError(f"no page {rel} in the vault")
    today = (now or datetime.now(UTC)).astimezone(UTC).date().isoformat()
    with vaultmod.locked(config):
        doc = pagewrite.PageDoc.read(path, vault=config.vault)
        fact = next((f for f in doc.page.facts if f.line == line), None)
        if fact is None or not fact.callouts:
            raise NoteError(f"no note under line {line} of {rel}; aft notes lists them")
        note = next(
            n for n in open_notes(config, None, everywhere=True) if n.page == rel and n.line == line
        )
        # R1: a replacement rests on the note's own evidence, quotes that still verify.
        # Checked before anything is written: closing the note writes its archive at once.
        evidence = [s for s in note.note_sources if s.verified]
        if choice == REPLACE and not evidence:
            raise NoteError(
                "this note cites no quote that still verifies, so no replacement can rest"
                " on it; settle it with --keep or --withdraw, and state the fact in a"
                " session instead"
            )
        statement_before = fact.statement
        archive = doc._archive_file()
        doc.resolve_callout(fact)
        fact = next(f for f in doc.page.facts if f.line == line)
        if choice == WITHDRAW:
            doc.withdraw(fact, today)
        elif choice == REPLACE:
            tag = "stated" if any(s.yours for s in evidence) else "observed"
            doc.supersede(
                fact,
                statement=" ".join(statement.split()),
                tag=tag,
                date=max(s.date for s in evidence),
                refs=[s.id for s in evidence],
            )
        path.write_text(doc.render(), encoding="utf-8")
        message = f"aft: settle {rel}:{line} ({choice}): {statement_before[:60]}"
        changed = [rel, archive.relative_to(config.vault).as_posix()]
        from aft.dream import report

        needs = report.regenerate_needs_you(config.vault)
        changed.append(needs.relative_to(config.vault).as_posix())
        vaultmod.commit(config.vault, changed, message)
    from aft import index

    index.update(config)
    return message


def _where(where: str) -> tuple[str, int]:
    rel, sep, number = where.rpartition(":")
    if not sep or not number.isdigit():
        raise NoteError(f"{where!r} is not <page>:<line>, as aft notes prints it")
    return rel, int(number)


def _sources(config: Config, sources: dict[str, dict], refs, fallback: str) -> list[Source]:
    from aft.dream.apply import _lookup_evidence

    found = []
    for ref in dict.fromkeys(refs):
        entry = sources.get(ref)
        if not entry:
            continue
        resource = str(entry.get("resource", ""))
        quote = str(entry.get("quote", ""))
        verified, _, when, yours = _lookup_evidence(config, resource, quote, fallback_date=fallback)
        found.append(Source(ref, resource, quote, when if verified else fallback, yours, verified))
    return found
