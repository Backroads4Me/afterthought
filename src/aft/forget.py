"""Forget operations: removing session evidence, index rows and digests, and redacting patterns.

DESIGN 12 promises that forgotten content leaves everything afterthought controls.
``aft forget session <id>`` removes the session's evidence from the archive, unindexes
its rows, deletes its digest files from the vault, and marks curated lines resting only
on that session as withdrawn, scrubs the quotes of sources citing that session, clears the
session's rows, watermark and digest debt from the queue, and scrubs the session's words out
of the dream reports and needs-you, so no later run files the words back or repeats them.

``aft forget pattern <regex>`` replaces matching text with [redacted] in the evidence archive
and in every copy the vault holds - digest and page source quotes, fact statements and notes,
superseded lines and reports - then rebuilds the search index to match.

The vault's git history still holds older commits; removing them requires rewriting history.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from aft import evidence, index, lint, page, pagewrite, queue, redact, vault
from aft.config import Config
from aft.digest import _is_session_digest, _is_session_resource
from aft.library import propose

FORGOTTEN_MARKER = "[forgotten]"


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
    the session's words out of every page under ``reports/``, needs-you included.
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
                if _is_session_digest(p, session_id):
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
        for fact_dir_name in fact_dirs:
            fact_dir = config.vault / fact_dir_name
            if not fact_dir.exists():
                continue
            for p in sorted(fact_dir.rglob("*.md")):
                rel_parts = p.relative_to(config.vault).parts
                if lint.SKIP_DIRS.intersection(rel_parts[:-1]):
                    continue
                try:
                    doc = pagewrite.PageDoc.read(p)
                except (OSError, pagewrite.WriteError):
                    continue

                session_sources = {
                    sid
                    for sid, s in doc.sources.items()
                    if _is_session_resource(str(s.get("resource", "")), session_id)
                }
                if not session_sources:
                    continue
                for sid in session_sources:
                    quote = str(doc.sources[sid].get("quote", ""))
                    if quote.strip():
                        words.append(quote)

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
                            and fact.state
                            in (page.ACTIVE, page.STALE, page.CONFLICT, page.NEEDS_YOU)
                            and fact.refs
                            and all(ref in session_sources for ref in fact.refs)
                            and pagewrite.line_identity(fact) not in skipped_facts
                        ):
                            target = fact
                            break
                    if target is None:
                        break
                    try:
                        doc.withdraw(target, today)
                        withdrawn_on_page.append(target.statement)
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

                for sid in sorted(session_sources):
                    doc.scrub_source(sid, marker=FORGOTTEN_MARKER)

                if doc.render() != original_text:
                    p.write_text(doc.render(), encoding="utf-8")
                    pages_modified.append(rel)
                    lines_withdrawn.extend(withdrawn_on_page)
                    candidates_removed.extend(candidates_on_page)

                lines_unwithdrawn.extend(unwithdrawn_on_page)

        reports_scrubbed = _scrub_report_pages(config, words)

        to_commit = [*digests_removed, *pages_modified, *reports_scrubbed]
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
    )


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
            path.write_text(scrubbed, encoding="utf-8")
            changed.append(path.relative_to(config.vault).as_posix())
    return changed


def forget_pattern(config: Config, pattern: str) -> PatternForgetReport:
    """Replace one pattern's matches with ``[redacted]`` in every copy aft made (DESIGN 12).

    The archive goes first and the vault follows it. A page's quote is checked against the
    archived entry behind it (DESIGN 8), and the same replacement applied to both leaves the
    quote verifiable while leaving neither of them searchable. The copies are the digest's
    source quotes, the memory pages' sources, statements and notes, the superseded lines
    under ``archive/`` and the dream reports, so a redaction that stopped at the archive
    would leave the words sitting in every page the digest wrote them into, and ``aft search``
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
    exactly as it stands.
    """
    changed: list[str] = []
    refused: list[str] = []
    for path in sorted(config.vault.rglob("*.md")):
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
        if len(page.parse(redacted, path).errors) > len(page.parse(text, path).errors):
            refused.append(rel)
            continue
        path.write_text(redacted, encoding="utf-8")
        changed.append(rel)
    return changed, refused
