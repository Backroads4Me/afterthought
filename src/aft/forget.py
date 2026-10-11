"""Forget operations: removing session evidence, index rows and digests, and redacting patterns.

DESIGN 12 promises that forgotten content leaves everything afterthought controls.
``aft forget session <id>`` removes the session's evidence from the archive, unindexes
its rows, deletes its digest files from the vault, and marks curated lines resting only
on that session as withdrawn, scrubs the quotes of sources citing that session, clears the
session's rows, watermark and digest debt from the queue, and scrubs the session's words out
of the dream reports and needs-you, so no later run files the words back or repeats them.
A line already superseded is withdrawn too when nothing else evidences it, because DESIGN 10
keeps superseded lines searchable under ``--archive`` and a forgotten session's sentence has no
business being found there.

Its phrase files go with it: the phrases of every line it
withdrew or rewrote and of every digest it deleted are pruned, and a deleted digest's phrase
file is deleted with the digest (BUILD-PLAN F85).

``aft forget pattern <regex>`` replaces matching text with [redacted] in the evidence archive
and in every copy the vault holds - digest and page source quotes, fact statements and notes,
superseded lines and reports, and the vault's phrase files - then rebuilds the search index
to match. The vault's half of
that runs under the vault's lock, as every other writer of those pages does.

The vault's git history still holds older commits; removing them requires rewriting history.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from aft import evidence, index, lint, page, pagewrite, phrases, queue, redact, vault
from aft.config import Config
from aft.digest import _is_session_digest, _is_session_resource
from aft.library import propose

FORGOTTEN_MARKER = "[forgotten]"
_FOOTNOTE = re.compile(r"\[\^([A-Za-z0-9_-]+)\]")

#: The line states `aft forget session` withdraws when nothing but the forgotten session
#: supports them. Active, stale, disputed and waiting lines are memory standing on that evidence.
#: A superseded line is struck already, and its statement still reads as a claim the archive
#: holds: DESIGN 10's ``archive`` collection is built of superseded lines, and DESIGN 12 says a
#: forgotten session leaves everything afterthought controls, so a struck line resting on no
#: evidence but the forgotten one goes out of memory the same way, as withdrawn.
_WITHDRAWABLE = (
    page.ACTIVE,
    page.SUPERSEDED,
    page.STALE,
    page.CONFLICT,
    page.NEEDS_YOU,
    page.WITHDRAWN,
)


@dataclass(frozen=True)
class SessionForgetReport:
    """The result of forgetting one session across afterthought."""

    session_id: str
    evidence_removed: bool
    digests_removed: tuple[str, ...]
    lines_withdrawn: tuple[str, ...]
    lines_unwithdrawn: tuple[str, ...]
    vault_committed: bool
    candidates_removed: tuple[str, ...] = ()
    jobs_removed: int = 0
    reports_scrubbed: tuple[str, ...] = ()
    phrases_pruned: int = 0

    @property
    def unwithdrawn_lines(self) -> tuple[str, ...]:
        return self.lines_unwithdrawn


@dataclass(frozen=True)
class PatternForgetReport:
    """What a pattern redaction reached, and the pages it would have broken (DESIGN 12).

    ``entries_redacted`` counts the archive rows rewritten, ``pages_redacted`` names the vault
    pages that now read ``[redacted]`` where the pattern matched, and ``pages_refused`` names
    the pages left untouched because the redaction would have left them unreadable - a person
    has to hear about those, because the words in them are still there.
    """

    entries_redacted: int
    pages_redacted: tuple[str, ...] = ()
    pages_refused: tuple[str, ...] = ()
    vault_committed: bool = False
    verified_quotes_redacted: int = 0


def forget_session(config: Config, session_id: str) -> SessionForgetReport:
    """Remove every trace of one session that afterthought controls (DESIGN 12, M2.8).

    Removes the session's evidence rows, index rows, and digest files, marks curated
    lines resting only on that session as withdrawn with PageDoc.withdraw, scrubs
    quotes of sources citing that session, and removes candidate lines resting only on it.
    It clears the session's job rows, watermark and digest debt from the queue, and scrubs
    the session's words out of every page under ``reports/``, needs-you included. The phrase
    files go with it: the phrases of every line it withdrew or rewrote and of every digest
    it deleted are pruned, and a deleted digest's phrase file is deleted with the digest
    (BUILD-PLAN F85).
    """
    evidence._require_name(session_id, "session id")
    vault.require(config)

    # The session's own words, read while the archive still holds them: the dream reports
    # quote them, and once the archive is gone nothing else says what to look for.
    words = [entry.text for entry in evidence.entries(config, session_id)]
    evidence.forget(config, session_id)
    jobs_removed = queue.forget_session(config, session_id)
    index.update(config)

    digests_removed: list[str] = []
    lines_withdrawn: list[str] = []
    lines_unwithdrawn: list[str] = []
    candidates_removed: list[str] = []
    pages_modified: list[str] = []

    with vault.locked(config):
        digests_dir = config.vault / "digests"
        if digests_dir.exists():
            for p in list(digests_dir.rglob("*.md")):
                if _is_session_digest(p, session_id, config):
                    # What the page carried goes with it: a dream report quotes a
                    # digest's facts, including the ones the verifier refused, and the
                    # page is the only place those sentences are written down.
                    words.extend(_digest_words(p))
                    rel = p.relative_to(config.vault).as_posix()
                    p.unlink()
                    digests_removed.append(rel)
                    parent = p.parent
                    while parent != digests_dir and parent.exists():
                        try:
                            parent.rmdir()
                        except OSError:
                            break
                        parent = parent.parent

        today = datetime.now(UTC).date().isoformat()
        fact_dirs = (*lint.FACT_DIRS, "archive")
        all_session_sources: set[str] = set()
        for fact_dir_name in fact_dirs:
            fact_dir = config.vault / fact_dir_name
            if not fact_dir.exists():
                continue
            for p in sorted(fact_dir.rglob("*.md")):
                rel_parts = p.relative_to(config.vault).parts
                if lint.SKIP_DIRS.intersection(rel_parts[:-1]):
                    continue
                try:
                    doc = pagewrite.PageDoc.read(p, vault=config.vault)
                except (OSError, pagewrite.WriteError):
                    continue
                for sid, s in doc.sources.items():
                    if _is_session_resource(str(s.get("resource", "")), session_id):
                        all_session_sources.add(sid)
                        quote = str(s.get("quote", ""))
                        if quote.strip():
                            words.append(quote)
                        title = str(s.get("title", ""))
                        if title.strip():
                            words.append(title)

        for fact_dir_name in fact_dirs:
            fact_dir = config.vault / fact_dir_name
            if not fact_dir.exists():
                continue
            for p in sorted(fact_dir.rglob("*.md")):
                rel_parts = p.relative_to(config.vault).parts
                if lint.SKIP_DIRS.intersection(rel_parts[:-1]):
                    continue
                try:
                    doc = pagewrite.PageDoc.read(p, vault=config.vault)
                except (OSError, pagewrite.WriteError):
                    continue

                session_sources = {
                    sid
                    for sid, s in doc.sources.items()
                    if _is_session_resource(str(s.get("resource", "")), session_id)
                }
                if not session_sources:
                    continue

                rel = p.relative_to(config.vault).as_posix()
                original_text = doc.render()
                withdrawn_on_page: list[str] = []
                candidates_on_page: list[str] = []
                unwithdrawn_on_page: list[str] = []
                skipped_facts: set[tuple] = set()

                while True:
                    target = None
                    for fact in doc.page.facts:
                        if (
                            fact.section in page.KIND_SECTIONS
                            and fact.state in _WITHDRAWABLE
                            and not (
                                fact.state == page.WITHDRAWN and fact.statement == FORGOTTEN_MARKER
                            )
                            and fact.refs
                            and all(ref in session_sources for ref in fact.refs)
                            and pagewrite.line_identity(fact) not in skipped_facts
                        ):
                            target = fact
                            break
                    if target is None:
                        break
                    try:
                        if target.callouts:
                            doc.resolve_callout(target)
                            archive_rel = doc._archive_file().relative_to(config.vault).as_posix()
                            if archive_rel not in pages_modified:
                                pages_modified.append(archive_rel)
                            target = next(
                                f
                                for f in doc.page.facts
                                if f.line == target.line or f.statement == target.statement
                            )
                        original_statement = target.statement
                        doc.withdraw(target, today, statement=FORGOTTEN_MARKER)
                        if original_statement != FORGOTTEN_MARKER:
                            withdrawn_on_page.append(original_statement)
                    except pagewrite.WriteError:
                        skipped_facts.add(pagewrite.line_identity(target))
                        unwithdrawn_on_page.append(f"{rel}:{target.line}: {target.statement}")

                while True:
                    target = None
                    for fact in doc.page.facts:
                        if (
                            fact.section == page.CANDIDATES
                            and fact.refs
                            and all(ref in session_sources for ref in fact.refs)
                            and pagewrite.line_identity(fact) not in skipped_facts
                        ):
                            target = fact
                            break
                    if target is None:
                        break
                    try:
                        doc.remove_candidate(target)
                        candidates_on_page.append(target.statement)
                    except pagewrite.WriteError:
                        skipped_facts.add(pagewrite.line_identity(target))
                        unwithdrawn_on_page.append(f"{rel}:{target.line}: {target.statement}")

                doc.scrub_callouts(session_sources, words=words, marker=FORGOTTEN_MARKER)

                for sid in sorted(session_sources):
                    doc.scrub_source(sid, marker=FORGOTTEN_MARKER)

                if doc.render() != original_text:
                    pagewrite.atomic_write_text(p, doc.render())
                    if rel not in pages_modified:
                        pages_modified.append(rel)
                    lines_withdrawn.extend(withdrawn_on_page)
                    candidates_removed.extend(candidates_on_page)

                lines_unwithdrawn.extend(unwithdrawn_on_page)

        archive_scrubbed = _scrub_archive_pages(
            config, session_id, words, today, all_session_sources
        )
        for rel in archive_scrubbed:
            if rel not in pages_modified:
                pages_modified.append(rel)

        reports_scrubbed = _scrub_report_pages(config, words)

        # The phrase files go with the forget: the lines this session's withdrawal rewrote
        # and the digests this session's deletion removed have keys no current row answers
        # any more, so their entries are pruned and the changed files are committed with
        # the rest (BUILD-PLAN F85).
        phrases_pruned, phrase_files = phrases.prune_paths(
            config, [*pages_modified, *digests_removed]
        )
        to_commit = sorted(
            dict.fromkeys([*digests_removed, *pages_modified, *reports_scrubbed, *phrase_files])
        )
        committed = False
        if to_commit:
            committed = vault.commit(config.vault, to_commit, f"aft: forget session {session_id}")

    index.update(config)

    return SessionForgetReport(
        session_id=session_id,
        evidence_removed=True,
        digests_removed=tuple(digests_removed),
        lines_withdrawn=tuple(lines_withdrawn),
        lines_unwithdrawn=tuple(lines_unwithdrawn),
        vault_committed=committed,
        candidates_removed=tuple(candidates_removed),
        jobs_removed=jobs_removed,
        reports_scrubbed=tuple(reports_scrubbed),
        phrases_pruned=phrases_pruned,
    )


def _scrub_archive_pages(
    config: Config,
    session_id: str,
    words: list[str],
    today: str,
    all_session_sources: set[str],
) -> list[str]:
    """Scrub resolved notes, fact statements, frontmatter sources, and words in archive/."""
    archive_dir = config.vault / "archive"
    if not archive_dir.is_dir():
        return []
    phrases = sorted({w for w in words if w.strip()}, key=len, reverse=True)
    changed: list[str] = []
    for path in sorted(archive_dir.rglob("*.md")):
        if ".git" in path.parts:
            continue
        try:
            raw_text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        original_text = raw_text

        # 1. If parseable as PageDoc, handle structured fact lines and sources
        try:
            doc = pagewrite.PageDoc.read(path, vault=config.vault)
            session_sources = {
                sid
                for sid, s in doc.sources.items()
                if _is_session_resource(str(s.get("resource", "")), session_id)
            }
            combined_sources = session_sources | all_session_sources
            for fact in list(doc.page.facts):
                if (
                    fact.section in page.KIND_SECTIONS
                    and fact.refs
                    and all(r in combined_sources for r in fact.refs)
                    and not (fact.state == page.WITHDRAWN and fact.statement == FORGOTTEN_MARKER)
                ):
                    try:
                        if fact.callouts:
                            doc.resolve_callout(fact)
                            fact = next(f for f in doc.page.facts if f.line == fact.line)
                        doc.withdraw(fact, today, statement=FORGOTTEN_MARKER)
                    except pagewrite.WriteError:
                        pass
            doc.scrub_callouts(combined_sources, words=words, marker=FORGOTTEN_MARKER)
            for sid in sorted(session_sources):
                doc.scrub_source(sid, marker=FORGOTTEN_MARKER)
            text = doc.render()
        except (OSError, pagewrite.WriteError):
            text = raw_text
            combined_sources = all_session_sources

        # 2. Scrub resolved note blocks and remaining words in text
        lines = text.split("\n")
        scrubbed_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("> - "):
                footnotes = set(_FOOTNOTE.findall(stripped))
                if any(fn in combined_sources for fn in footnotes):
                    m = re.match(
                        r"^(>\s*-\s*)(?:~~)?(.+?)(?:~~)?(\s+\[[a-z]+\]\s+\d{4}-\d{2}-\d{2}.*)$",
                        line,
                    )
                    if m:
                        prefix, _, rest = m.groups()
                        line = f"{prefix}{FORGOTTEN_MARKER}{rest}"
            elif stripped.startswith("> ") and not stripped.startswith("> [!"):
                footnotes = set(_FOOTNOTE.findall(stripped))
                if any(fn in combined_sources for fn in footnotes):
                    line = f"> {FORGOTTEN_MARKER}"
            for phrase in phrases:
                line = line.replace(phrase, FORGOTTEN_MARKER)
            scrubbed_lines.append(line)

        scrubbed_text = "\n".join(scrubbed_lines)
        if scrubbed_text != original_text:
            pagewrite.atomic_write_text(path, scrubbed_text)
            changed.append(path.relative_to(config.vault).as_posix())
    return changed


def _digest_words(path: Path) -> list[str]:
    """Every sentence a session digest page carries, read before the page is deleted.

    A digest records what it proposed in full: the kept facts as lines and again in
    ``aft.facts``, and the facts the verifier refused in ``aft.dropped`` with the reason each
    one failed on. A dream report writes a kept fact's statement above its quote and names a
    refused fact's statement in its Rejected section, so a sentence the model wrote and the
    rules refused lives only on this page and in that report: deleting the page first would
    leave the report quoting words no archive row and no page can point at any more. A page
    that cannot be read contributes nothing, and is deleted all the same.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    words: list[str] = []
    frontmatter, _, _, error = page.split_frontmatter(text)
    for source in (frontmatter or {}).get("sources") or []:
        if isinstance(source, dict):
            words.extend(str(source[key]) for key in ("quote", "title") if source.get(key))
    aft = (frontmatter or {}).get("aft")
    if isinstance(aft, dict):
        for key in ("facts", "dropped"):
            for fact in aft.get(key) or []:
                if isinstance(fact, dict) and fact.get("statement"):
                    words.append(str(fact["statement"]))
    if error is None:
        words.extend(fact.statement for fact in page.parse(text, path).facts)
    return [word for word in words if word.strip()]


def _scrub_report_pages(config: Config, words: list[str]) -> list[str]:
    """Replace the session's words in every page under ``reports/``, and name what changed.

    A dream report writes the quote behind each line it added, confirmed or superseded
    (DESIGN 9), and needs-you repeats the callout text of the lines it lists, so a forgotten
    session's words would go on living in those pages after its evidence, digests and curated
    lines are gone. The ruling a forgetting works to is that any text carrying the session's
    words takes a fixed marker the page grammar accepts, which is what ``FORGOTTEN_MARKER``
    is. Longer texts go first, so a quote is never left in pieces behind a shorter text that
    sits inside it.
    """
    reports_dir = config.vault / index.REPORTS
    if not reports_dir.is_dir():
        return []
    phrases = sorted({word for word in words if word.strip()}, key=len, reverse=True)
    changed: list[str] = []
    for path in sorted(reports_dir.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        scrubbed = text
        for phrase in phrases:
            scrubbed = scrubbed.replace(phrase, FORGOTTEN_MARKER)
        if scrubbed != text:
            pagewrite.atomic_write_text(path, scrubbed)
            changed.append(path.relative_to(config.vault).as_posix())
    return changed


def forget_pattern(config: Config, pattern: str) -> PatternForgetReport:
    """Replace one pattern's matches with ``[redacted]`` in every copy aft made (DESIGN 12).

    The archive goes first and the vault follows it. A page's quote is checked against the
    archived entry behind it (DESIGN 8), and the same replacement applied to both leaves the
    quote verifiable while leaving neither of them searchable. The copies are the digest's
    source quotes, the memory pages' sources, statements and notes, the superseded lines
    under ``archive/``, the dream reports and the phrase files, so a redaction that stopped
    at the archive would leave the words sitting in every page the digest wrote them into,
    and ``aft search``
    would keep answering to them.

    The archive's own rewrite cleans its freed pages and log; the vault's pages are committed
    so the working tree holds nothing either, and the index is rebuilt over both. The vault's
    git history is beyond this, as it is for a forgotten session, so the commit says only what
    happened: a redaction pattern is often the secret it removes, and this command writes it to
    no page, commit or config file. The record of imported library quotes takes the same
    replacement, because an imported quote is verified against the page citing it and both
    sides have to move together to keep verifying.
    """
    compiled = redact.compile_patterns([pattern])[0]
    count = evidence.redact_pattern(config, compiled)
    # The vault lock is what every other writer of these pages takes (DESIGN 12): this one
    # rewrites a page only after checking its own redaction leaves it readable, and a page
    # caught half-written by a dream run or a digest commit would answer that check as neither
    # the old text nor the new. The commit and the index rebuild that follow it read the same
    # pages, so they stay inside the lock with the writes.
    with vault.locked(config):
        pages, refused = _redact_pages(config, compiled)
        quotes_redacted = propose.redact_verified_quotes(config.home, compiled)
        index.update(config)
        committed = False
        if pages:
            # A pattern is often the secret itself, so the commit says what happened and names
            # nothing: a forgetting cannot rewrite git history, so it writes none of it either.
            committed = vault.commit(config.vault, pages, "aft: forget pattern")
    return PatternForgetReport(count, tuple(pages), tuple(refused), committed, quotes_redacted)


def _redact_pages(config: Config, compiled: re.Pattern[str]) -> tuple[list[str], list[str]]:
    """Redact the pattern in every page of the vault; return what changed and what refused.

    A page is rewritten only when the result still reads as the page it was: the parse of a
    redacted page may find no more problems than the parse of the page as written. That is
    the guard against a regex that would swallow a fact line's citation or a page's
    frontmatter, which would otherwise leave the person with a lint failure they cannot read
    back to the pattern they typed. A refused page is named on the command's report and left
    exactly as it stands. The vault's phrase files are redacted with the same replacement;
    their guard is the JSON parse a reader would run, since a rewrite that broke the file
    would leave it read as no phrases at all.
    """
    changed: list[str] = []
    refused: list[str] = []
    phrase_dir = config.vault / phrases.PHRASES_DIR
    phrase_files = sorted(phrase_dir.rglob("*.json")) if phrase_dir.is_dir() else []
    for path in sorted((*config.vault.rglob("*.md"), *phrase_files)):
        if ".git" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        redacted = compiled.sub(redact.REDACTED, text)
        if redacted == text:
            continue
        rel = path.relative_to(config.vault).as_posix()
        if path.suffix == ".json":
            # A phrase file is JSON, so its guard is the parse a reader would run: a
            # redaction that matched the JSON's own marks would leave a file the index
            # reads as no phrases at all, and the words would stay.
            try:
                json.loads(redacted)
            except json.JSONDecodeError:
                refused.append(rel)
                continue
        elif len(page.parse(redacted, path).errors) > len(page.parse(text, path).errors):
            refused.append(rel)
            continue
        pagewrite.atomic_write_text(path, redacted)
        changed.append(rel)
    return changed, refused
