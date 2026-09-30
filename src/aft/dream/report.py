"""The dream run report and needs-you page (DESIGN 9).

Each day has one report file, reports/<yyyy>/<mm>/<date>.md (type: Dream Report),
and each run that changes anything appends one section to it. Each run's section has
a fixed format of 9 sections in fixed order:
1. Summary: counts per decision, tokens used, digests processed, backlog remaining
2. Needs you: lines waiting on your decision
3. Superseded: old line, new line, the quote that justified it
4. Added and Confirmed: each with its quote
5. Conflicts: both claims and their evidence
6. Rejected: what the model proposed and the rule that refused it
7. Stale: facts whose anchors stopped matching
8. Candidates: progress toward the gate
9. Revert: git revert <sha>, and the separate checkpoint commit if one was made

Every run also regenerates reports/needs-you.md: one list of every open conflict and
question across all projects, each linking to its line and to the report section that
raised it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from aft import lint, page
from aft.config import Config
from aft.dream import apply
from aft.model import tokens_used_today


def _lookup_quote(vault_path: Path, page_rel_path: str, source_id: str) -> str:
    """Look up a quote from the frontmatter sources of a page in the vault."""
    if not page_rel_path or not source_id:
        return ""
    target_file = vault_path / page_rel_path
    if not target_file.exists():
        return ""
    try:
        text = target_file.read_text(encoding="utf-8")
        fm = page.split_frontmatter(text)[0] or {}
        clean_sid = source_id.lstrip("^")
        sources = {
            str(s.get("id")).lstrip("^"): s
            for s in fm.get("sources") or []
            if isinstance(s, dict) and s.get("id") is not None
        }
        return str(sources.get(clean_sid, {}).get("quote", "")).strip()
    except OSError:
        return ""


def _lookup_candidate_progress(vault_path: Path, page_rel_path: str, statement: str) -> str:
    """Look up gate progress suffix for a candidate line from its page."""
    if not page_rel_path or not statement:
        return ""
    target_file = vault_path / page_rel_path
    if not target_file.exists():
        return ""
    try:
        text = target_file.read_text(encoding="utf-8")
        parsed = page.parse(text, target_file)
        for fact in parsed.facts:
            if fact.statement == statement and fact.signals and fact.sessions:
                return (
                    f" (signals {fact.signals[0]}/{fact.signals[1]}, "
                    f"sessions {fact.sessions[0]}/{fact.sessions[1]})"
                )
        return ""
    except OSError:
        return ""


def format_run_section(
    report: apply.ProjectRunReport,
    vault_path: Path,
    now: datetime | None = None,
    checkpoint: str | None = None,
    tag_commit: str | None = None,
    tokens_used: int = 0,
    backlog: int = 0,
) -> str:
    """Format one run's section in the fixed 9-section order from DESIGN 9."""
    now = now or datetime.now(UTC)
    time_str = f"{now:%H:%M:%SZ}"
    lines: list[str] = [f"# {report.project} {time_str}", ""]

    # 1. Summary
    lines.append("## Summary")
    lines.append(f"- Added: {len(report.added)}")
    lines.append(f"- Confirmed: {len(report.confirmed)}")
    lines.append(f"- Superseded: {len(report.superseded)}")
    lines.append(f"- Promoted: {len(report.promoted)}")
    lines.append(f"- Candidates: {len(report.candidates)}")
    lines.append(f"- Conflicts: {len(report.conflicts)}")
    lines.append(f"- Needs you: {len(report.questions)}")
    lines.append(f"- Skips: {len(report.skips)}")
    if report.skips:
        for sk in report.skips:
            lines.append(f'  - "{sk.statement}": {sk.reason}')
    lines.append(f"- Rejected: {len(report.rejected)}")
    lines.append(f"- Tokens used: {tokens_used}")
    lines.append(f"- Digests processed: {len(report.digests_consumed)}")
    lines.append(f"- Backlog remaining: {backlog}")
    if report.oversize_pages:
        lines.append(f"- Oversize pages: {len(report.oversize_pages)}")
        for op in report.oversize_pages:
            lines.append(f"  - {op}")
    else:
        lines.append("- Oversize pages: 0")
    lines.append("")

    # 2. Needs you
    lines.append("## Needs you")
    if report.questions:
        for q in report.questions:
            lines.append(f"- Target: {q.target_statement}")
            lines.append(f"  New claim: {q.new_statement}")
            lines.append(f"  Reason: {q.reason} ({q.rule})")
    else:
        lines.append("None.")
    lines.append("")

    # 3. Superseded
    lines.append("## Superseded")
    if report.superseded:
        for s in report.superseded:
            quote = _lookup_quote(vault_path, s.path, s.by)
            lines.append(f"- Old: {s.old_statement}")
            lines.append(f"  New: {s.new_statement}")
            if quote:
                lines.append(f'  Quote: "{quote}"')
    else:
        lines.append("None.")
    lines.append("")

    # 4. Added and Confirmed
    lines.append("## Added and Confirmed")
    has_added_or_confirmed = bool(report.added or report.confirmed or report.promoted)
    if has_added_or_confirmed:
        if report.added:
            lines.append("### Added")
            for a in report.added:
                ref_id = a.refs[0] if a.refs else ""
                quote = _lookup_quote(vault_path, a.path, ref_id)
                lines.append(f"- {a.statement} [{a.source}] ({a.date})")
                if quote:
                    lines.append(f'  Quote: "{quote}"')
        if report.confirmed:
            lines.append("### Confirmed")
            for c in report.confirmed:
                quote = _lookup_quote(vault_path, c.path, c.source)
                lines.append(f"- {c.statement} (confirmed {c.date})")
                if quote:
                    lines.append(f'  Quote: "{quote}"')
        if report.promoted:
            lines.append("### Promoted")
            for p in report.promoted:
                ref_id = p.refs[-1] if p.refs else ""
                quote = _lookup_quote(vault_path, p.path, ref_id)
                lines.append(f"- {p.statement} (promoted to {p.kind} {p.date})")
                if quote:
                    lines.append(f'  Quote: "{quote}"')
    else:
        lines.append("None.")
    lines.append("")

    # 5. Conflicts
    lines.append("## Conflicts")
    if report.conflicts:
        for c in report.conflicts:
            lines.append(f"- Existing claim: {c.target_statement}")
            lines.append(f"  New claim: {c.new_statement}")
            lines.append(f"  Reason: {c.reason} ({c.rule})")
    else:
        lines.append("None.")
    lines.append("")

    # 6. Rejected
    lines.append("## Rejected")
    if report.rejected:
        for r in report.rejected:
            lines.append(
                f'- Proposed {r.decision_action} on "{r.statement}": '
                f"refused by {r.rule} ({r.reason})"
            )
    else:
        lines.append("None.")
    lines.append("")

    # 7. Stale
    lines.append("## Stale")
    if report.broken_anchors or report.cleared_anchors or report.oversize_pages:
        if report.broken_anchors:
            lines.append("### Broken anchors")
            for b in report.broken_anchors:
                lines.append(f"- {b.statement} on {b.page} [^{b.sid}] (broken: {b.reason})")
        if report.cleared_anchors:
            lines.append("### Cleared anchors")
            for cl in report.cleared_anchors:
                lines.append(f"- {cl.statement} on {cl.page} [^{cl.sid}] (matches again)")
        if report.oversize_pages:
            lines.append("### Oversize pages")
            for op in report.oversize_pages:
                lines.append(f"- {op} (over size cap)")
    else:
        lines.append("None.")
    lines.append("")

    # 8. Candidates
    lines.append("## Candidates")
    downgrade_map = {d.statement: d for d in report.downgrades}
    if report.candidates or report.promoted or report.downgrades:
        for cand in report.candidates:
            progress = _lookup_candidate_progress(vault_path, cand.path, cand.statement)
            lines.append(f"- {cand.statement} [{cand.tag}] ({cand.date}){progress}")
            d = downgrade_map.get(cand.statement)
            if d:
                lines.append(f"  Downgraded by {d.rule}: {d.reason}")
        for p in report.promoted:
            lines.append(f'- Promoted to {p.kind}: "{p.statement}"')
        for d in report.downgrades:
            if not any(c.statement == d.statement for c in report.candidates):
                lines.append(
                    f'- Downgraded "{d.statement}" to {d.to_action} by {d.rule}: {d.reason}'
                )
    else:
        lines.append("None.")
    lines.append("")

    # 9. Revert
    lines.append("## Revert")
    has_revert = False
    if report.commit:
        lines.append(f"git revert {report.commit}")
        has_revert = True
    if tag_commit:
        lines.append(f"Separate hand edits tag commit: git revert {tag_commit}")
        has_revert = True
    if checkpoint:
        lines.append(f"Separate checkpoint commit: git revert {checkpoint}")
        has_revert = True
    if not has_revert:
        lines.append("No commit made (run made no changes).")
    lines.append("")

    return "\n".join(lines)


def write_report(
    config: Config,
    report: apply.ProjectRunReport,
    now: datetime | None = None,
    checkpoint: str | None = None,
    tag_commit: str | None = None,
    tokens_used: int | None = None,
    backlog: int = 0,
) -> Path:
    """Append one run's section to reports/<yyyy>/<mm>/<date>.md."""
    now = now or datetime.now(UTC)
    vault_path = config.vault
    if tokens_used is None:
        try:
            tokens_used = tokens_used_today(config, now)
        except Exception:
            tokens_used = 0

    section_text = format_run_section(
        report=report,
        vault_path=vault_path,
        now=now,
        checkpoint=checkpoint,
        tag_commit=tag_commit,
        tokens_used=tokens_used,
        backlog=backlog,
    )

    date_str = now.date().isoformat()
    report_dir = vault_path / "reports" / f"{now.year:04d}" / f"{now.month:02d}"
    report_path = report_dir / f"{date_str}.md"
    report_dir.mkdir(parents=True, exist_ok=True)

    if not report_path.exists():
        header = f'---\ntype: Dream Report\ntitle: "{date_str}"\n---\n\n'
        report_path.write_text(header + section_text, encoding="utf-8")
    else:
        existing = report_path.read_text(encoding="utf-8")
        if not existing.endswith("\n\n"):
            if not existing.endswith("\n"):
                existing += "\n\n"
            else:
                existing += "\n"
        report_path.write_text(existing + section_text, encoding="utf-8")

    return report_path


def has_changes(rep: apply.ProjectRunReport) -> bool:
    """Whether a project run made any modifications to the vault memory."""
    return bool(
        rep.added
        or rep.confirmed
        or rep.superseded
        or rep.promoted
        or rep.candidates
        or rep.conflicts
        or rep.questions
        or rep.broken_anchors
        or rep.cleared_anchors
        or rep.archived_lines
    )


def regenerate_needs_you(vault_path: Path) -> Path:
    """Regenerate reports/needs-you.md listing every open conflict and question in the vault."""
    reports_dir = vault_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    needs_you_path = reports_dir / "needs-you.md"

    conflicts: list[tuple[str, int, str, page.Callout]] = []
    questions: list[tuple[str, int, str, page.Callout]] = []

    for top_dir in lint.FACT_DIRS:
        folder = vault_path / top_dir
        if not folder.is_dir():
            continue
        for md_file in sorted(folder.rglob("*.md")):
            if md_file.name == "index.md":
                continue
            try:
                text = md_file.read_text(encoding="utf-8")
            except OSError:
                continue
            parsed = page.parse(text, md_file)
            rel_path = md_file.relative_to(vault_path).as_posix()
            for fact in parsed.facts:
                for callout in fact.callouts:
                    if callout.type == "conflict":
                        conflicts.append((rel_path, fact.line, fact.statement, callout))
                    elif callout.type == "question":
                        questions.append((rel_path, fact.line, fact.statement, callout))

    header = (
        "---\n"
        "type: Needs You\n"
        "title: Needs you\n"
        "description: Every open conflict and question, regenerated on every dream run.\n"
        "---\n\n"
        "# Needs you\n\n"
    )

    if not conflicts and not questions:
        needs_you_path.write_text(header + "Nothing is waiting on you.\n", encoding="utf-8")
        return needs_you_path

    body_lines: list[str] = []

    if conflicts:
        body_lines.append("## Conflicts")
        body_lines.append("")
        for rel_path, line_no, stmt, callout in conflicts:
            report_ref = _report_link(callout.date, "conflicts")
            link_text = f"[{rel_path}:{line_no}](../{rel_path}#L{line_no})"
            body_lines.append(f"- {link_text}: {stmt} ({report_ref})")
            body_lines.append(f"  > [!conflict] {callout.date}")
            for row in callout.text:
                body_lines.append(f"  > {row}")
            body_lines.append("")

    if questions:
        body_lines.append("## Questions")
        body_lines.append("")
        for rel_path, line_no, stmt, callout in questions:
            report_ref = _report_link(callout.date, "needs-you")
            link_text = f"[{rel_path}:{line_no}](../{rel_path}#L{line_no})"
            body_lines.append(f"- {link_text}: {stmt} ({report_ref})")
            body_lines.append(f"  > [!question] {callout.date}")
            for row in callout.text:
                body_lines.append(f"  > {row}")
            body_lines.append("")

    needs_you_path.write_text(header + "\n".join(body_lines).rstrip() + "\n", encoding="utf-8")
    return needs_you_path


def _report_link(date_str: str, heading_id: str) -> str:
    """Format a relative markdown link to the report section that raised the note."""
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        parts = date_str.split("-")
        yyyy, mm = parts[0], parts[1]
        return f"raised in [{date_str} report]({yyyy}/{mm}/{date_str}.md#{heading_id})"
    return "raised in dream report"
