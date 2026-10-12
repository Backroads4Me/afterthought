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
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aft.config import Config
from aft.page import FACT_DIRS

# pagewrite, the vault and the rules are imported where a note is read or settled: the
# session-start hook only counts notes, and its time is the block's (DESIGN 10).

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
    origin: str
    """Where the quote came from: a primary session, a worker session or the library."""


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
    """The fact pages whose notes a session in ``slug`` is asked about: the project's own,
    the global pages and the reference pages, or only the last two for a session of no
    project. ``everywhere`` takes every project's pages as well, the same pages
    ``reports/needs-you.md`` lists notes from."""
    vault = config.vault
    if everywhere:
        roots = [vault / top for top in FACT_DIRS]
    else:
        shared = [vault / top for top in FACT_DIRS if top != "projects"]
        roots = ([vault / "projects" / slug] if slug else []) + shared
    return [
        path
        for root in roots
        if root.is_dir()
        for path in sorted(root.rglob("*.md"))
        if path.name != "index.md"
    ]


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
    from aft import pagewrite

    found: list[Note] = []
    for path in pages(config, slug, everywhere):
        try:
            doc = pagewrite.PageDoc.read(path, vault=config.vault)
        except (OSError, UnicodeDecodeError, pagewrite.WriteError):
            continue
        rel = path.relative_to(config.vault).as_posix()
        for fact in doc.page.facts:
            found.extend(_notes_on(config, doc, rel, fact))
    return found


@dataclass(frozen=True)
class Settled:
    """What a settle committed: the settle commit's message, and a replacement's citation
    commit's message when one was made (T43)."""

    message: str
    citation: str | None = None


def settle(
    config: Config,
    where: str,
    choice: str | int,
    statement: str | None = None,
    now: datetime | None = None,
    *args: Any,
) -> Settled:
    """Settle the note under the line at ``where`` (``<page>:<line>``) as the user chose,
    and commit it. Returns what it committed: the settle commit's message, and the
    citation commit's message when a replacement gained one.

    ``keep`` leaves the line standing. ``withdraw`` strikes it through, dated today.
    ``replace`` supersedes it with ``statement``, cited to the notes' own sources: tagged
    ``[stated]`` when one of them is the user's own words and ``[observed]`` otherwise, and
    dated by the newest of them. The settle commit is itself the user's edit, so a
    replacement is a second commit: the new line gains a source ``aft://edit/<sha>`` of
    the settle commit, quoted with the statement, and takes the commit's UTC day as its
    date (T43); a citation that cannot be made leaves the settle commit standing alone.
    Only a quote that still verifies and does not come from a worker session can carry the
    replacement, as no other quote carries a long-term line (R1, DESIGN 8); a note without
    one cannot be settled by a replacement.
    A replacement must say only what the quotes cited to it say (R2): one that names a
    word or number none of them holds is refused, and nothing is written.
    """
    from aft import digest, pagewrite
    from aft import vault as vaultmod
    from aft.dream import rules

    if isinstance(choice, int):
        rel = where
        line = choice
        actual_choice = str(statement)
        if actual_choice == REPLACE:
            actual_statement = now if isinstance(now, str) else (args[0] if args else None)
            actual_now = (
                args[1]
                if len(args) > 1
                else (
                    args[0]
                    if args and isinstance(args[0], datetime)
                    else (now if isinstance(now, datetime) else None)
                )
            )
        else:
            actual_statement = None
            actual_now = args[0] if args else (now if isinstance(now, datetime) else None)
        choice = actual_choice
        statement = actual_statement
        now = actual_now
    else:
        rel, line = _where(where)

    if choice not in CHOICES:
        raise NoteError(f"unknown choice {choice!r}; expected one of {', '.join(CHOICES)}")
    if (choice == REPLACE) != bool(statement and statement.strip()):
        raise NoteError("--replace takes the new statement, and only --replace does")
    if choice == REPLACE:
        statement = " ".join(statement.split())
    path = config.vault / rel
    if not path.is_file():
        raise NoteError(f"no page {rel} in the vault")
    today = (now or datetime.now(UTC)).astimezone(UTC).date().isoformat()
    with vaultmod.locked(config):
        doc = pagewrite.PageDoc.read(path, vault=config.vault)
        fact = _fact_at(doc, line)
        if fact is None or not fact.callouts:
            raise NoteError(f"no note under line {line} of {rel}; aft notes lists them")
        sources = [s for n in _notes_on(config, doc, rel, fact) for s in n.note_sources]
        evidence = list(
            {s.id: s for s in sources if s.verified and s.origin != rules.WORKER}.values()
        )
        if choice == REPLACE and not evidence:
            raise NoteError(
                "this note cites no quote that still verifies from one of the user's sessions"
                " or the library, so no replacement can rest on it; settle it with --keep or"
                " --withdraw, and state the fact in a session instead"
            )
        if choice == REPLACE:
            # R2: the replacement says only what the quotes cited to it say, as every
            # [stated] line must. The check runs before anything is written, so a refused
            # replacement leaves the page and the vault as they were.
            missing = digest.ungrounded(statement, "\n".join(s.quote for s in evidence))
            if missing:
                raise NoteError(
                    f"the replacement names {', '.join(missing)} that none of the note's"
                    " quotes says; reword it or settle with --keep or --withdraw"
                )
        statement_before = fact.statement
        archive = doc._archive_file()
        archive_before = archive.read_bytes() if archive.is_file() else None
        try:
            # Closing a note writes its archive at once, so a refusal from here on puts the
            # archive back as it was: a settle that fails leaves no trace.
            doc.resolve_callout(fact)
            fact = _fact_at(doc, line)
            if choice == WITHDRAW:
                doc.withdraw(fact, today)
            elif choice == REPLACE:
                doc.supersede(
                    fact,
                    statement=statement,
                    tag="stated" if any(s.yours for s in evidence) else "observed",
                    date=max(s.date for s in evidence),
                    refs=[s.id for s in evidence],
                )
            rendered = doc.render()
        except pagewrite.WriteError as exc:
            if archive_before is None:
                archive.unlink(missing_ok=True)
            else:
                pagewrite.atomic_write_bytes(archive, archive_before)
            raise NoteError(f"cannot settle {rel}:{line}: {exc}") from None
        pagewrite.atomic_write_text(path, rendered)
        message = f"aft: settle {rel}:{line} ({choice}): {statement_before[:60]}"
        changed = [rel, archive.relative_to(config.vault).as_posix()]
        from aft.dream import report

        needs = report.regenerate_needs_you(config.vault)
        changed.append(needs.relative_to(config.vault).as_posix())
        vaultmod.commit(config.vault, changed, message)
        citation = None
        if choice == REPLACE:
            # T43: the settle commit is itself the user's edit, so the replacement cites
            # it as its own newest source and takes its day as the line's date. A failed
            # citation leaves the settle commit as it stands; the command reports it.
            try:
                citation = _cite_settle(config, rel, line, statement)
            except Exception:
                citation = None
    from aft import index

    index.update(config)
    return Settled(message, citation)


def _cite_settle(config: Config, rel: str, line: int, statement: str) -> str:
    """Cite the settle commit as the replacement line's own newest source (T43).

    A replacement is dated by the note's quotes, so evidence that arrived after those
    quotes but before the user settled could reach the dream later and count as newer
    than the user's decision. The settle commit is itself the user's edit, so the line
    gains a source ``aft://edit/<sha>`` of that commit, quoted with the statement, and
    takes the commit's UTC day as its date, the way ``tag_hand_edits`` cites a hand edit
    (DESIGN 9). Returns the citation commit's message; raises when the citation cannot
    be made, leaving the settle commit as it stands.
    """
    from contextlib import suppress

    from aft import page, pagewrite
    from aft import vault as vaultmod
    from aft.gitutil import git

    path = config.vault / rel
    sha = git(["rev-parse", "HEAD"], cwd=config.vault).stdout.strip()
    shown = git(["show", "-s", "--format=%ct", "--end-of-options", sha], cwd=config.vault)
    when = datetime.fromtimestamp(int(shown.stdout.strip()), UTC).date().isoformat()
    text = path.read_text(encoding="utf-8")
    before = page.parse(text, path)
    doc = pagewrite.PageDoc(text, path)
    fact = _fact_at(doc, line)
    if (
        fact is None
        or fact.struck
        or fact.state != page.ACTIVE
        or fact.statement != statement
        or not fact.refs
    ):
        raise NoteError(f"{rel}:{line} does not hold the settled replacement")
    source_id = doc.add_source(f"aft://edit/{sha}", title=f"File edit {when}", quote=statement)
    lines = doc.render().split("\n")
    shift = len(lines) - len(text.split("\n"))  # add_source writes only frontmatter
    lines[fact.line - 1 + shift] = f"- {statement} [{fact.source}] {when}" + "".join(
        f" [^{ref}]" for ref in (*fact.refs, source_id)
    )
    result = "\n".join(lines)
    after = page.parse(result, path)
    cited = next((f for f in after.facts if source_id in f.refs), None)
    entry = pagewrite.PageDoc(result, path).sources.get(source_id, {})
    was = Counter(message for _, message in before.errors)
    now = Counter(message for _, message in after.errors)
    if (
        cited is None
        or cited.struck
        or cited.statement != statement
        or cited.date != when
        or set(cited.refs) != set((*fact.refs, source_id))
        or entry.get("resource") != f"aft://edit/{sha}"
        or entry.get("quote") != statement
        or any(count > was[message] for message, count in now.items())
    ):
        raise NoteError(f"the settle citation on {rel}:{line} would not read back as written")
    before_bytes = path.read_bytes()
    pagewrite.atomic_write_text(path, result)
    message = f"aft: cite settle {sha[:7]} on {rel}:{line}"
    try:
        if not vaultmod.commit(config.vault, [rel], message):
            raise NoteError(f"{rel} was left unchanged; the settle citation wrote nothing")
    except Exception:
        # The citation is all or nothing: a commit that did not land puts the page and the
        # index back as the settle commit left them, so the first commit stands alone.
        pagewrite.atomic_write_bytes(path, before_bytes)
        with suppress(Exception):
            git(["reset", "-q", "--", rel], cwd=config.vault)
        raise
    return message


def _fact_at(doc, line: int):
    return next((f for f in doc.page.facts if f.line == line), None)


def _notes_on(config: Config, doc, rel: str, fact) -> list[Note]:
    """The notes under one line of ``doc``, each with the quotes behind it and the line."""
    found = []
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
                line_sources=_sources(config, doc.sources, fact.refs, fact.date),
                note_sources=_sources(config, doc.sources, note_refs, callout.date),
            )
        )
    return found


def _where(where: str) -> tuple[str, int]:
    rel, sep, number = where.rpartition(":")
    if not sep or not number.isdigit():
        raise NoteError(f"{where!r} is not <page>:<line>, as aft notes prints it")
    parts = Path(rel).parts
    if not parts or parts[0] not in FACT_DIRS or ".." in parts:
        raise NoteError(f"{rel} is not a fact page; aft notes lists the notes")
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
        verified, origin, when, yours = _lookup_evidence(
            config, resource, quote, fallback_date=fallback, source_entry=entry
        )
        found.append(
            Source(ref, resource, quote, when if verified else fallback, yours, verified, origin)
        )
    return found
