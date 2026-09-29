"""Apply surviving outcomes through the page writer, run maintenance, and commit.

DESIGN 9, steps 5 to 7.
Applies surviving outcomes from rules.check() through PageDoc;
creates projects/<slug>/overview.md and registers the project on its first curated fact;
checks anchors with ripgrep against the default branch;
moves superseded lines older than 90 days to archive/<page path>;
flags pages over 12,000 characters in the report data;
regenerates each index.md and appends to log.md;
runs lint and aborts the run without committing if lint fails;
marks consumed digests with aft.consumed: <commit>;
makes one commit per project run (separate from M4.1's checkpoint and tagging commits).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from aft import evidence, lint, page, pagewrite
from aft.config import Config
from aft.dream import decide, maintain, rules
from aft.gitutil import git
from aft.registry import Registry

GLOBAL_NOT_STATED = "a global page changes only from [stated]"


class ApplyError(Exception):
    """The dream run cannot apply an edit or lint failed."""


@dataclass(frozen=True)
class AddedFact:
    statement: str
    kind: str
    source: str
    date: str
    refs: tuple[str, ...]
    path: str


@dataclass(frozen=True)
class ConfirmedFact:
    statement: str
    source: str
    date: str
    refs: tuple[str, ...]
    path: str


@dataclass(frozen=True)
class SupersededFact:
    old_statement: str
    new_statement: str
    source: str
    date: str
    refs: tuple[str, ...]
    by: str
    path: str


@dataclass(frozen=True)
class PromotedFact:
    statement: str
    kind: str
    source: str
    date: str
    refs: tuple[str, ...]
    path: str


@dataclass(frozen=True)
class CandidateFact:
    statement: str
    tag: str
    date: str
    refs: tuple[str, ...]
    path: str


@dataclass(frozen=True)
class ConflictFact:
    new_statement: str
    target_statement: str
    reason: str
    rule: str


@dataclass(frozen=True)
class QuestionFact:
    new_statement: str
    target_statement: str
    reason: str
    rule: str


@dataclass(frozen=True)
class SkipFact:
    statement: str
    reason: str


@dataclass(frozen=True)
class DowngradeFact:
    statement: str
    to_action: str
    rule: str
    reason: str


@dataclass(frozen=True)
class RejectedFact:
    statement: str
    decision_action: str
    rule: str
    reason: str


@dataclass
class ProjectRunReport:
    """Structured data from one project's dream run for M4.6 report consumption."""

    project: str
    commit: str | None = None
    digests_consumed: list[str] = field(default_factory=list)
    added: list[AddedFact] = field(default_factory=list)
    confirmed: list[ConfirmedFact] = field(default_factory=list)
    superseded: list[SupersededFact] = field(default_factory=list)
    promoted: list[PromotedFact] = field(default_factory=list)
    candidates: list[CandidateFact] = field(default_factory=list)
    conflicts: list[ConflictFact] = field(default_factory=list)
    questions: list[QuestionFact] = field(default_factory=list)
    skips: list[SkipFact] = field(default_factory=list)
    downgrades: list[DowngradeFact] = field(default_factory=list)
    rejected: list[RejectedFact] = field(default_factory=list)
    broken_anchors: list[maintain.BrokenAnchor] = field(default_factory=list)
    cleared_anchors: list[maintain.ClearedAnchor] = field(default_factory=list)
    archived_lines: list[maintain.ArchivedLine] = field(default_factory=list)
    oversize_pages: list[str] = field(default_factory=list)


def run_project(
    config: Config,
    slug: str,
    digests: Sequence[Path],
    now: datetime | None = None,
) -> ProjectRunReport:
    """Process waiting digests for one project, run maintenance, and commit."""
    if not digests:
        return ProjectRunReport(project=slug)

    registry = Registry.load(config.registry_path)
    project = registry.by_slug(slug)
    run_project_id = project.id if project else None

    now = now or datetime.now(UTC)
    today = now.date().isoformat()
    vault_path = config.vault
    head_before = git(["rev-parse", "HEAD"], cwd=vault_path).stdout.strip()

    report = ProjectRunReport(project=slug)

    for digest_path in digests:
        try:
            d_text = digest_path.read_text(encoding="utf-8")
        except OSError:
            continue

        frontmatter, body, _, _ = page.split_frontmatter(d_text)
        if frontmatter is None:
            continue

        aft_dict = frontmatter.get("aft") or {}
        if isinstance(aft_dict, dict) and aft_dict.get("consumed"):
            continue

        role = aft_dict.get("role", "primary") if isinstance(aft_dict, dict) else "primary"
        digest_role = rules.WORKER if role == "worker" else rules.PRIMARY

        sources_map = {
            str(s["id"]): s
            for s in frontmatter.get("sources") or []
            if isinstance(s, dict) and s.get("id") is not None
        }

        facts_meta = {
            f["statement"]: f
            for f in (aft_dict.get("facts") or [])
            if isinstance(f, dict) and "statement" in f
        }

        parsed_page = page.parse(d_text, digest_path)

        for fact in parsed_page.facts:
            meta = facts_meta.get(fact.statement, {})
            scope = meta.get("scope", "project")
            explicit_correction = bool(meta.get("explicit_correction", False))

            new_fact = rules.NewFact(
                statement=fact.statement,
                kind=fact.kind or "fact",
                source=fact.source,
                date=fact.date,
                scope=scope,
                explicit_correction=explicit_correction,
            )

            # R1 trust rule: a quote must verify against its source before a line is written
            evidence_list: list[rules.Evidence] = []
            for ref in fact.refs:
                s_entry = sources_map.get(ref, {})
                res = str(s_entry.get("resource", ""))
                q = str(s_entry.get("quote", ""))
                v = _is_quote_verified(config, res, q)
                y = _is_quote_yours(config, res, default=(fact.source == "stated"))
                evidence_list.append(
                    rules.Evidence(
                        source=ref,
                        resource=res,
                        origin=digest_role,
                        date=fact.date,
                        verified=v,
                        yours=y,
                    )
                )

            if not any(e.verified for e in evidence_list):
                report.rejected.append(
                    RejectedFact(
                        statement=new_fact.statement,
                        decision_action="add",
                        rule="R1",
                        reason="quote not verified",
                    )
                )
                continue

            # R6 trust rule: global pages change only from [stated]
            if new_fact.scope == "global" and new_fact.source != "stated":
                report.skips.append(
                    SkipFact(
                        statement=new_fact.statement,
                        reason=GLOBAL_NOT_STATED,
                    )
                )
                continue

            quote_to_send = sources_map.get(fact.refs[0], {}).get("quote", "") if fact.refs else ""
            proposal = decide.decide(
                config,
                new_fact,
                quote_to_send,
                slug if project else None,
            )

            # Trust rule: never bypass rules.check()
            line_evidence: list[rules.Evidence] = []
            if proposal.target is not None:
                for ref, s_dict in proposal.target.sources.items():
                    line_evidence.append(
                        rules.Evidence(
                            source=ref,
                            resource=str(s_dict.get("resource", "")),
                            origin=rules.PRIMARY,
                            date=proposal.target.fact.date,
                            verified=True,
                            yours=(proposal.target.fact.source == "stated"),
                        )
                    )

            effective_project = run_project_id or f"p_{slug}"
            context = proposal.context(
                today=today,
                evidence=tuple(evidence_list),
                run_project=effective_project,
                line_evidence=tuple(line_evidence),
            )
            outcome = proposal.check(context)

            # Trust rule: [inferred] and [suggested] never replace a line
            if (
                isinstance(outcome, rules.Apply)
                and outcome.action == "supersede"
                and new_fact.source in ("inferred", "suggested")
            ):
                report.conflicts.append(
                    ConflictFact(
                        new_statement=new_fact.statement,
                        target_statement=(
                            proposal.target.fact.statement if proposal.target else ""
                        ),
                        reason=proposal.decision.reason,
                        rule="R4",
                    )
                )
                continue

            if isinstance(outcome, rules.Reject):
                report.rejected.append(
                    RejectedFact(
                        statement=new_fact.statement,
                        decision_action=proposal.decision.action,
                        rule=outcome.rule,
                        reason=proposal.decision.reason,
                    )
                )
            elif isinstance(outcome, rules.Apply) and outcome.action == "skip":
                report.skips.append(
                    SkipFact(
                        statement=new_fact.statement,
                        reason=proposal.decision.reason,
                    )
                )
            elif (isinstance(outcome, rules.Apply) and outcome.action == "conflict") or (
                isinstance(outcome, rules.Downgrade) and outcome.to_action == "conflict"
            ):
                rule_name = outcome.rule if isinstance(outcome, rules.Downgrade) else "conflict"
                report.conflicts.append(
                    ConflictFact(
                        new_statement=new_fact.statement,
                        target_statement=(
                            proposal.target.fact.statement if proposal.target else ""
                        ),
                        reason=proposal.decision.reason,
                        rule=rule_name,
                    )
                )
            elif isinstance(outcome, rules.Downgrade) and outcome.to_action == "needs-you":
                report.questions.append(
                    QuestionFact(
                        new_statement=new_fact.statement,
                        target_statement=(
                            proposal.target.fact.statement if proposal.target else ""
                        ),
                        reason=proposal.decision.reason,
                        rule=outcome.rule,
                    )
                )
            else:
                # Surviving write action: add, candidate, confirm, promote, supersede
                if project is None and new_fact.scope != "global":
                    project = registry.add(slug, title=slug.capitalize())
                    registry.save()
                    run_project_id = project.id
                    overview_path = vault_path / "projects" / slug / "overview.md"
                    if not overview_path.exists():
                        overview_path.parent.mkdir(parents=True, exist_ok=True)
                        overview_path.write_text(
                            f"---\n"
                            f"type: Project\n"
                            f"title: {slug}\n"
                            f"description: {slug}\n"
                            f"sources: []\n"
                            f"aft:\n"
                            f"  project: {project.id}\n"
                            f"---\n",
                            encoding="utf-8",
                        )

                if new_fact.scope == "global":
                    target_file = (
                        vault_path
                        / "global"
                        / ("preferences.md" if new_fact.kind == "preference" else "environment.md")
                    )
                else:
                    if proposal.target is not None and outcome.action in (
                        "confirm",
                        "supersede",
                        "promote",
                    ):
                        target_file = vault_path / proposal.target.path
                    else:
                        target_file = vault_path / "projects" / slug / "overview.md"

                doc = pagewrite.PageDoc.read(target_file)
                first_ref = fact.refs[0] if fact.refs else ""
                src_entry = sources_map.get(first_ref, {})
                res = str(src_entry.get("resource", ""))
                q = str(src_entry.get("quote", ""))
                title = str(src_entry.get("title", ""))

                sid: str | None = None
                for existing_id, existing_s in doc.sources.items():
                    if existing_s.get("resource") == res and existing_s.get("quote") == q:
                        sid = existing_id
                        break
                if sid is None and res and q:
                    sid = doc.add_source(resource=res, title=title, quote=q)

                refs_to_use = [sid] if sid else []
                rel_target = target_file.relative_to(vault_path).as_posix()

                if isinstance(outcome, rules.Apply) and outcome.action == "add":
                    doc.add_fact(
                        new_fact.kind,
                        new_fact.statement,
                        new_fact.source,
                        new_fact.date,
                        refs_to_use,
                    )
                    report.added.append(
                        AddedFact(
                            statement=new_fact.statement,
                            kind=new_fact.kind,
                            source=new_fact.source,
                            date=new_fact.date,
                            refs=tuple(refs_to_use),
                            path=rel_target,
                        )
                    )
                elif isinstance(outcome, rules.Downgrade) and outcome.to_action == "candidate":
                    doc.add_candidate(
                        new_fact.statement,
                        new_fact.source,
                        new_fact.date,
                        refs_to_use,
                    )
                    report.candidates.append(
                        CandidateFact(
                            statement=new_fact.statement,
                            tag=new_fact.source,
                            date=new_fact.date,
                            refs=tuple(refs_to_use),
                            path=rel_target,
                        )
                    )
                    report.downgrades.append(
                        DowngradeFact(
                            statement=new_fact.statement,
                            to_action="candidate",
                            rule=outcome.rule,
                            reason=proposal.decision.reason,
                        )
                    )
                elif isinstance(outcome, rules.Apply) and outcome.action == "confirm":
                    assert proposal.target is not None
                    if sid:
                        target_fact = doc.fact(proposal.target.fact.statement)
                        doc.confirm(target_fact, source=sid, date=new_fact.date)
                        report.confirmed.append(
                            ConfirmedFact(
                                statement=target_fact.statement,
                                source=sid,
                                date=new_fact.date,
                                refs=(*target_fact.refs, sid),
                                path=rel_target,
                            )
                        )
                elif isinstance(outcome, rules.Apply) and outcome.action == "promote":
                    assert proposal.target is not None
                    target_fact = doc.fact(proposal.target.fact.statement)
                    promoted = doc.promote(target_fact, kind=new_fact.kind)
                    if sid:
                        doc.confirm(promoted, source=sid, date=new_fact.date)
                    report.promoted.append(
                        PromotedFact(
                            statement=target_fact.statement,
                            kind=new_fact.kind,
                            source=new_fact.source,
                            date=new_fact.date,
                            refs=(*promoted.refs, sid) if sid else promoted.refs,
                            path=rel_target,
                        )
                    )
                elif isinstance(outcome, rules.Apply) and outcome.action == "supersede":
                    assert proposal.target is not None
                    target_fact = doc.fact(proposal.target.fact.statement)
                    doc.supersede(
                        target_fact,
                        statement=new_fact.statement,
                        tag=new_fact.source,
                        date=new_fact.date,
                        refs=refs_to_use,
                    )
                    report.superseded.append(
                        SupersededFact(
                            old_statement=target_fact.statement,
                            new_statement=new_fact.statement,
                            source=new_fact.source,
                            date=new_fact.date,
                            refs=tuple(refs_to_use),
                            by=sid or "",
                            path=rel_target,
                        )
                    )

                target_file.write_text(doc.render(), encoding="utf-8")

        # Mark digest consumed with commit sha
        dfm, dbody, _, _ = page.split_frontmatter(d_text)
        if dfm is not None:
            daft = dfm.setdefault("aft", {})
            daft["consumed"] = head_before
            new_fm_yaml = yaml.safe_dump(dfm, sort_keys=False, allow_unicode=True).strip()
            digest_path.write_text(f"---\n{new_fm_yaml}\n---\n{dbody}", encoding="utf-8")
        report.digests_consumed.append(digest_path.relative_to(vault_path).as_posix())

    # Maintenance
    broken, cleared = maintain.check_anchors(vault_path, project, today, config.host)
    report.broken_anchors.extend(broken)
    report.cleared_anchors.extend(cleared)

    archived = maintain.archive_superseded(vault_path, slug, today)
    report.archived_lines.extend(archived)

    project_dir = vault_path / "projects" / slug
    project_pages = list(project_dir.glob("*.md")) if project_dir.is_dir() else []
    oversize = maintain.check_oversize(vault_path, project_pages)
    report.oversize_pages.extend(oversize)

    maintain.regenerate_index(vault_path, slug, project.title if project else slug.capitalize())
    maintain.ensure_root_index(vault_path)
    maintain.append_log(vault_path, slug, f"{today}: dream {slug}")

    # Lint check
    findings = lint.lint_vault(vault_path)
    if findings:
        git(["checkout", "--", "."], cwd=vault_path)
        git(["clean", "-fd"], cwd=vault_path)
        raise ApplyError(f"lint failed on {len(findings)} finding(s): {findings[0]}")

    # One commit per project run
    status = git(["status", "--porcelain=v1"], cwd=vault_path).stdout.strip()
    if status:
        git(["add", "-A"], cwd=vault_path)
        git(["commit", "-q", "-m", f"aft: dream {slug}"], cwd=vault_path)
        report.commit = git(["rev-parse", "HEAD"], cwd=vault_path).stdout.strip()
    else:
        report.commit = None

    return report


def _is_quote_verified(config: Config, resource: str, quote: str) -> bool:
    quote = quote.strip()
    if not quote or not resource:
        return False
    if resource.startswith("aft://session/"):
        uri = resource.removeprefix("aft://session/")
        parts = uri.split("#", 1)
        if len(parts) == 2:
            agent_sess = parts[0].split("/")
            sess_id = agent_sess[-1]
            entry_id = parts[1]
            db_path = config.home / "evidence.sqlite"
            if db_path.exists():
                with evidence._reading(config) as conn:
                    row = conn.execute(
                        "SELECT 1 FROM evidence WHERE session_id = ? AND entry_id = ?",
                        (sess_id, entry_id),
                    ).fetchone()
                if row is not None:
                    return evidence.find_quote(config, sess_id, entry_id, quote)
    return True


def _is_quote_yours(config: Config, resource: str, default: bool = True) -> bool:
    if resource.startswith("aft://edit/"):
        return True
    if resource.startswith("aft://session/"):
        uri = resource.removeprefix("aft://session/")
        parts = uri.split("#", 1)
        if len(parts) == 2:
            agent_sess = parts[0].split("/")
            sess_id = agent_sess[-1]
            entry_id = parts[1]
            db_path = config.home / "evidence.sqlite"
            if db_path.exists():
                with evidence._reading(config) as conn:
                    row = conn.execute(
                        "SELECT role FROM evidence WHERE session_id = ? AND entry_id = ?",
                        (sess_id, entry_id),
                    ).fetchone()
                if row is not None:
                    return str(row[0]) == "user"
    return default
