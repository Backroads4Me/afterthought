"""Forget operations: removing session evidence, index rows and digests, and redacting patterns.

DESIGN 12 promises that forgotten content leaves everything afterthought controls.
``aft forget session <id>`` removes the session's evidence from the archive, unindexes
its rows, deletes its digest files from the vault, and marks curated lines resting only
on that session as withdrawn.

``aft forget pattern <regex>`` redacts matching text across the evidence archive and
updates the search index to reflect the redactions.

The vault's git history still holds older commits; removing them requires rewriting history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from aft import evidence, index, lint, page, pagewrite, vault
from aft.config import Config
from aft.digest import _is_session_digest, _is_session_resource

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

    @property
    def unwithdrawn_lines(self) -> tuple[str, ...]:
        return self.lines_unwithdrawn


def forget_session(config: Config, session_id: str) -> SessionForgetReport:
    """Remove every trace of one session that afterthought controls (DESIGN 12, M2.8).

    Removes the session's evidence rows, index rows, and digest files, marks curated
    lines resting only on that session as withdrawn with PageDoc.withdraw, scrubs
    quotes of sources citing that session, and removes candidate lines resting only on it.
    """
    evidence._require_name(session_id, "session id")
    vault.require(config)

    evidence.forget(config, session_id)
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

        to_commit = [*digests_removed, *pages_modified]
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
    )


def forget_pattern(config: Config, pattern: str) -> int:
    """Redact matches of ``pattern`` across the archive and index (DESIGN 12, M2.8).

    Rewrites matching archive entries in place with [redacted], cleans freed pages
    and log, and updates the search index to reflect the redactions.
    Returns the number of archive entries redacted.
    """
    count = evidence.redact_pattern(config, pattern)
    index.update(config)
    return count
