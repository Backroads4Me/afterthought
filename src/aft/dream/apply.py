"""Apply surviving outcomes through the page writer, run maintenance, and commit.

DESIGN 9, steps 5 to 7.
Applies surviving outcomes from rules.check() through PageDoc;
creates projects/<slug>/overview.md and registers the project on its first curated fact;
checks anchors with ripgrep against the default branch;
moves superseded lines older than 90 days to archive/<page path>;
flags pages over 12,000 characters in the report data;
regenerates each index.md and appends to log.md;
runs lint and aborts the run without committing if lint fails;
marks consumed digests with aft.consumed: <run id>, which the run's commit message
also names, so the commit that consumed a digest is found with git log --grep;
makes one commit per project run (separate from M4.1's checkpoint and tagging commits).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import yaml

from aft import digest, evidence, lint, model, page, pagewrite, queue
from aft import vault as vaultmod
from aft.config import Config
from aft.dream import decide, maintain, reconcile, rules, shortterm
from aft.gitutil import git
from aft.registry import Project, Registry, path_key


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
    refs: tuple[str, ...] = ()
    path: str = ""


@dataclass(frozen=True)
class QuestionFact:
    new_statement: str
    target_statement: str
    reason: str
    rule: str
    refs: tuple[str, ...] = ()
    path: str = ""


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


@dataclass(frozen=True)
class LiftedFact:
    """A short-term line that left ``recent.md`` to be decided as a long-term fact."""

    statement: str
    why: str


@dataclass(frozen=True)
class RolledOffLine:
    """A short-term line whose newest source left the project's last sessions."""

    statement: str
    state: str


@dataclass(frozen=True)
class ExpiredLine:
    """A short-term line whose newest source is older than ``dream.short_term_days``."""

    statement: str


UNFILED_SKIP = "a session of no project: only its global facts become memory"

LIFT_CONFIRMED = "a later session's long-term fact confirms it"
LIFT_OUTLASTED = "its sources span the short-term window"


@dataclass
class ProjectRunReport:
    """Structured data from one project's dream run for M4.6 report consumption."""

    project: str
    commit: str | None = None
    digests_consumed: list[str] = field(default_factory=list)
    stopped_for_budget: bool = False
    """The day's token budget ran out before the run applied everything it read (DESIGN
    13): the digest being applied and the digests after it, or the short-term lines being
    decided, wait for the next run."""
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
    lifted: list[LiftedFact] = field(default_factory=list)
    rolled_off: list[RolledOffLine] = field(default_factory=list)
    withdrawn: list[reconcile.WithdrawnLine] = field(default_factory=list)
    """Short-term lines the newest session applied showed done, false, or replaced."""
    expired: list[ExpiredLine] = field(default_factory=list)
    """Short-term lines rolled off by the ``dream.short_term_days`` backstop."""
    refused: list[reconcile.RefusedWithdrawal] = field(default_factory=list)
    """Recent lines the reconcile was asked to withdraw and the script refused."""
    error: str | None = None


def _record_of(config: Config, digest_path: Path) -> tuple[str, str, queue.ProjectRecord] | None:
    """The session-start record a digest's session carries, with the record's key (F49).

    A digest's name holds the agent and session that made it, and the record says which
    project that session started in and where. A later part of one session carries the
    session's id with a ``-<n>`` on the end, and the record is keyed by the session's own id,
    which is what comes back so the record can be rewritten against it.
    """
    match = digest._DIGEST_NAME.match(digest_path.stem)
    if match is None:
        return None
    agent, session = match.group("agent"), match.group("session")
    record = queue.recorded_project(config, agent, session)
    if record is None:
        prefix, sep, suffix = session.rpartition("-")
        if sep and suffix.isdigit():
            session, record = prefix, queue.recorded_project(config, agent, prefix)
    if record is None:
        return None
    return agent, session, record


def digest_groups(
    config: Config, registry: Registry, slug: str, digests: Sequence[Path]
) -> list[list[Path]]:
    """One slug's waiting digests split into the projects their own records name (DESIGN 7,
    F49).

    Two directories that once claimed one name can file their digests under one directory -
    ``digests/<slug>/`` - when a project was registered over the claim, or when a record
    predates the paths records carry. The records still say which directory each session
    ran in, so a digest whose record names a different project, or a different directory
    that would register one, is a run of its own, whatever the group's first record says.
    A digest whose session left no record at all is run by itself, falling back to the
    group's name as it did before records carried a path.

    The groups come back in session order, each list in the order its digests were given.
    """
    groups: dict[tuple[str, str], list[Path]] = {}
    for digest_path in digests:
        named = _record_of(config, digest_path)
        if named is None:
            key = ("recordless", slug)
        else:
            _, _, record = named
            project = registry.by_id(record.project_id) if record.project_id else None
            if project is not None:
                key = ("project", project.id)
            elif record.path is not None:
                path = Path(record.path)
                project = registry.by_path_key(path_key(config.host, path))
                key = ("project", project.id) if project is not None else ("path", record.path)
            else:
                project = registry.by_slug(record.slug)
                key = ("project", project.id) if project is not None else ("slug", record.slug)
        groups.setdefault(key, []).append(digest_path)
    return list(groups.values())


def _project_of_group(
    config: Config, registry: Registry, slug: str, digests: Sequence[Path]
) -> tuple[Project | None, Path | None]:
    """The project this group of digests belongs to, and the path that would register it.

    The session-start records are what say which project each session started in: by the
    project's id when the registry knew it at session start, and by the recorded path
    otherwise, since the path, not the name, is what a project is (DESIGN 7, F49). The path
    is also what an unregistered group carries back, for the dream to register the project
    at. A record that names neither an id the registry answers for nor a path - one an
    earlier build wrote - falls back to the slug it holds, and a digest whose session left
    no record at all falls back to the group's own directory name, as it did before records
    carried a path.
    """
    for digest_path in digests:
        named = _record_of(config, digest_path)
        if named is None:
            continue
        _, _, record = named
        project = registry.by_id(record.project_id) if record.project_id else None
        if project is not None:
            return project, None
        if record.path is not None:
            path = Path(record.path)
            project = registry.by_path_key(path_key(config.host, path))
            return project, (None if project is not None else path)
        return registry.by_slug(record.slug), None
    return registry.by_slug(slug), None


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
    project, project_path = _project_of_group(config, registry, slug, digests)
    # The pages a run writes follow the project's slug, and the digests it reads sit under
    # the name their sessions filed them under; the two are the same unless the project was
    # registered under a name the group's directory could not keep (DESIGN 7, F49).
    page_slug = project.slug if project else slug
    run_project_id = project.id if project else None
    # Digests of sessions that belong to no project: only their global facts are memory.
    unfiled = slug == digest.UNFILED
    now = now or datetime.now(UTC)
    today = now.date().isoformat()
    # A digest is marked consumed by the same commit that consumes it, so the marker
    # cannot hold that commit's sha: it holds this run id instead, and the commit
    # message names the same id, so `git log --grep <run id>` answers with the sha.
    run_id = f"dream-{page_slug}-{now:%Y%m%dT%H%M%SZ}"
    vault_path = config.vault.resolve()

    report = ProjectRunReport(project=page_slug)

    written_paths: set[Path] = set()
    created_paths: set[Path] = set()
    pre_run_content: dict[Path, bytes] = {}

    def record_write(p: Path) -> None:
        resolved = p.resolve()
        written_paths.add(resolved)
        if resolved in created_paths:
            return
        if not p.exists():
            created_paths.add(resolved)
        elif resolved not in pre_run_content:
            pre_run_content[resolved] = p.read_bytes()

    def rollback() -> None:
        for p in created_paths:
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
            parent = p.parent
            while parent != vault_path and parent.is_relative_to(vault_path):
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent

        for p, content in pre_run_content.items():
            try:
                pagewrite.atomic_write_bytes(p, content)
            except OSError:
                pass

    # Short-term lines a decision or their sessions carry to long-term memory, with why.
    to_lift: list[tuple[str, str]] = []

    # Set when the day's token budget ran out before a decide call: the digest being
    # applied and the digests after it wait for the next run (DESIGN 13).
    budget_stop = False

    def settle(
        new_fact: rules.NewFact,
        evidence_list: list[rules.Evidence],
        sources_map: dict[str, dict],
        lifting: bool = False,
    ) -> bool:
        """Decide one fact against memory, judge the decision by the rules, and write what
        survives (design 9 steps 2 to 5). ``sources_map`` holds the sources the fact's
        evidence cites: its digest's, or for a short-term line becoming long-term, the
        sources of ``recent.md``. ``lifting`` decides a line that is leaving short-term
        memory: the retrieval leaves ``recent.md`` out (R10), and a decision that names one
        of its lines anyway is judged as a new long-term line, which lands on the
        overview."""
        nonlocal project, run_project_id, page_slug, run_id, budget_stop
        if budget_stop:
            # The day's budget already stopped this run: nothing further is decided, and
            # what was not applied waits for the next one (DESIGN 13).
            return False
        verified_evidence = [e for e in evidence_list if e.verified]
        if not verified_evidence:
            report.rejected.append(
                RejectedFact(
                    statement=new_fact.statement,
                    decision_action="add",
                    rule="R1",
                    reason="quote not verified",
                )
            )
            return False

        verified_quotes = [
            str(sources_map.get(e.source, {}).get("quote", "")) for e in verified_evidence
        ]
        if new_fact.source == "stated":
            user_quotes = [
                (
                    str(sources_map.get(e.source, {}).get("quote", "")),
                    _turn_text(config, e.resource),
                )
                for e in verified_evidence
                if e.yours
            ]
            if any(
                "?" in q or (turn is not None and "?" in digest.enclosing_sentence(turn, q))
                for q, turn in user_quotes
            ):
                report.rejected.append(
                    RejectedFact(
                        statement=new_fact.statement,
                        decision_action="add",
                        rule="R2",
                        reason="the quote is a question, never a settled item",
                    )
                )
                return False
            grounding_texts: list[str] = []
            when_candidates: list[datetime] = []
            for e in verified_evidence:
                quote_text = str(sources_map.get(e.source, {}).get("quote", ""))
                if e.yours:
                    turn = _turn_text(config, e.resource)
                    grounding_texts.append(turn if turn is not None else quote_text)
                    when = evidence.entry_time(config, e.resource)
                    if when is None and e.date:
                        try:
                            when = datetime.fromisoformat(e.date + "T00:00:00+00:00")
                        except ValueError:
                            when = None
                    if when is not None:
                        when_candidates.append(when)
                else:
                    grounding_texts.append(quote_text)
            said = "\n".join(t for t in grounding_texts if t)
            if when_candidates:
                missing = digest.ungrounded(new_fact.statement, said, when=when_candidates[0])
                for when in when_candidates[1:]:
                    if not missing:
                        break
                    cand_missing = digest.ungrounded(new_fact.statement, said, when=when)
                    if len(cand_missing) < len(missing):
                        missing = cand_missing
            else:
                missing = digest.ungrounded(new_fact.statement, said)
            if missing:
                any_turn_present = any(turn is not None for _, turn in user_quotes)
                all_recorded = all(
                    bool(sources_map.get(e.source, {}).get("check")) for e in verified_evidence
                )
                if any_turn_present or not all_recorded:
                    report.rejected.append(
                        RejectedFact(
                            statement=new_fact.statement,
                            decision_action="add",
                            rule="R2",
                            reason=(
                                "the statement names words neither the user's turn nor a proposal"
                                f" holds: {', '.join(missing)}"
                            ),
                        )
                    )
                    return False

        quote_to_send = "\n".join(q for q in verified_quotes if q) if verified_quotes else ""
        # The day's cap counts the dream's decide calls with the digests' (DESIGN 13), so
        # the check sits before each call: a project with more waiting facts than the day
        # has room for stops at the day's last affordable decision instead of spending
        # into a budget it has already used up.
        if model.tokens_used_today(config, now) >= config.settings.work.daily_token_budget:
            budget_stop = True
            report.stopped_for_budget = True
            return False
        proposal = decide.decide(
            config,
            new_fact,
            quote_to_send,
            page_slug if project else None,
            **({"exclude_recent": True} if lifting else {}),
        )

        # Target line evidence from archive
        line_evidence: list[rules.Evidence] = []
        if proposal.target is not None:
            for ref in proposal.target.fact.refs:
                s_dict = proposal.target.sources.get(ref, {})
                res = str(s_dict.get("resource", ""))
                q = str(s_dict.get("quote", ""))
                v, origin, ev_date, yours = _lookup_evidence(
                    config,
                    res,
                    q,
                    fallback_date=proposal.target.fact.date,
                    source_entry=s_dict,
                )
                line_evidence.append(
                    rules.Evidence(
                        source=ref,
                        resource=res,
                        origin=origin,
                        date=ev_date,
                        verified=v,
                        yours=yours,
                    )
                )

        effective_project = run_project_id or f"p_{page_slug}"
        context = proposal.context(
            today=today,
            evidence=tuple(evidence_list),
            run_project=effective_project,
            line_evidence=tuple(line_evidence),
        )
        outcome = proposal.check(context)

        # A lift is decided against long-term memory alone (the retrieval left recent.md
        # out), so a decision that names one of its lines is not the confirm of the line
        # being lifted: judged as the new long-term line it takes itself for, it is
        # written to the overview. Left as the decision asked, it would re-cite the line
        # on recent.md and the lift would then delete it.
        if lifting and proposal.target is not None and proposal.target.recent:
            outcome = rules.check(rules.Decision("add"), new_fact, None, context)

        if isinstance(outcome, rules.Reject):
            report.rejected.append(
                RejectedFact(
                    statement=new_fact.statement,
                    decision_action=proposal.decision.action,
                    rule=outcome.rule,
                    reason=proposal.decision.reason,
                )
            )
            return False

        action = outcome.action if isinstance(outcome, rules.Apply) else outcome.to_action
        if action == "skip":
            report.skips.append(
                SkipFact(
                    statement=new_fact.statement,
                    reason=proposal.decision.reason,
                )
            )
        else:
            # Actions: add, candidate, confirm, promote, supersede, conflict, needs-you
            if project is None and new_fact.scope != "global":
                # The directory this group of sessions ran in becomes a project (DESIGN 7,
                # F49): registered with the recorded path, under the name the digests file
                # it under unless that is taken. A group whose records named no path - a
                # digest of a session that left no record - registers under the group's name
                # alone, as it did before paths were recorded.
                if project_path is not None:
                    project = registry.add_for_path(config.host, project_path, base=slug)
                else:
                    project = registry.add(slug, title=slug.capitalize())
                record_write(config.registry_path)
                registry.save()
                run_project_id = project.id
                page_slug = project.slug
                # The pages this run writes from here stand under the project the registry
                # made, and the report names it (DESIGN 7, F49). The run id is not made
                # again: the digests consumed before the registration already carry it, and
                # one run keeps one id, the one its commit names.
                report.project = page_slug
                # The records of the sessions this group was digested from name the project
                # they became, so a later digest of the same session finds it (DESIGN 7, F49).
                for digest_path in digests:
                    named = _record_of(config, digest_path)
                    if named is None:
                        continue
                    agent, session, record = named
                    queue.record_project(
                        config,
                        agent,
                        session,
                        project.id,
                        project.slug,
                        None if record.path is None else Path(record.path),
                    )
            if proposal.target is not None and action in (
                "confirm",
                "lift",
                "supersede",
                "promote",
                "conflict",
                "needs-you",
            ):
                target_file = vault_path / proposal.target.path
                record_write(target_file)
            elif new_fact.term == rules.SHORT:
                target_file = _recent_page(vault_path, page_slug)
                record_write(target_file)
                if not target_file.exists():
                    pagewrite.atomic_write_text(
                        target_file, _recent_text(page_slug, project, run_project_id)
                    )
            elif new_fact.scope == "global":
                g_file = "preferences.md" if new_fact.kind == "preference" else "environment.md"
                target_file = vault_path / "global" / g_file
                if not target_file.exists():
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    content = (
                        vaultmod.PREFERENCES
                        if new_fact.kind == "preference"
                        else vaultmod.ENVIRONMENT
                    )
                    record_write(target_file)
                    pagewrite.atomic_write_text(target_file, content)
                else:
                    record_write(target_file)
            else:
                overview_path = vault_path / "projects" / page_slug / "overview.md"
                if not overview_path.exists():
                    overview_path.parent.mkdir(parents=True, exist_ok=True)
                    proj_id = run_project_id or (project.id if project else f"p_{page_slug}")
                    proj_title = project.title if project else page_slug.capitalize()
                    record_write(overview_path)
                    pagewrite.atomic_write_text(
                        overview_path,
                        f"---\n"
                        f"type: Project\n"
                        f"title: {proj_title}\n"
                        f"description: {page_slug}\n"
                        f"sources: []\n"
                        f"aft:\n"
                        f"  project: {proj_id}\n"
                        f"---\n",
                    )
                else:
                    record_write(overview_path)
                target_file = overview_path

            doc = pagewrite.PageDoc.read(target_file)
            verified_refs = [e.source for e in verified_evidence]
            refs_to_use: list[str] = []
            for ref in verified_refs:
                src_entry = sources_map.get(ref, {})
                res = str(src_entry.get("resource", ""))
                q = str(src_entry.get("quote", ""))
                title = str(src_entry.get("title", ""))
                v_date = str(src_entry.get("verified", ""))
                s_said = str(src_entry.get("said", ""))
                s_role = str(src_entry.get("role", ""))
                c_hash = str(src_entry.get("check", ""))
                if not c_hash and res:
                    parsed = evidence.parse_resource(res)
                    if parsed:
                        if res.startswith("aft://session/"):
                            if not s_role:
                                s_role = "user"
                            if not s_said:
                                s_said = new_fact.date or today
                            c_hash = evidence.quote_check(parsed[0], parsed[1], s_role, s_said, q)
                        else:
                            c_hash = evidence.quote_check(parsed[0], parsed[1], q)
                        if not v_date:
                            v_date = today

                sid: str | None = None
                for existing_id, existing_s in doc.sources.items():
                    if existing_s.get("resource") == res and existing_s.get("quote") == q:
                        sid = existing_id
                        break
                if sid is None and res and q:
                    sid = doc.add_source(
                        resource=res,
                        title=title,
                        quote=q,
                        verified=v_date,
                        said=s_said,
                        role=s_role,
                        check=c_hash,
                    )
                if sid:
                    refs_to_use.append(sid)

            rel_target = target_file.relative_to(vault_path).as_posix()

            if action == "add":
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
            elif action == "candidate":
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
                        rule=(
                            outcome.rule if isinstance(outcome, rules.Downgrade) else "candidate"
                        ),
                        reason=proposal.decision.reason,
                    )
                )
            elif action == "lift":
                # Confirmed by a long-term fact: the line is cited and, once this run's
                # digests are in, leaves recent.md as a long-term fact (design 6, R10).
                assert proposal.target is not None
                for sid in refs_to_use:
                    target_fact = doc.fact(proposal.target.fact.statement)
                    doc.confirm(target_fact, source=sid, date=new_fact.date)
                to_lift.append((proposal.target.fact.statement, LIFT_CONFIRMED))
            elif action == "confirm":
                assert proposal.target is not None
                for sid in refs_to_use:
                    # Each confirm rewrites the line, so the handle is taken again per
                    # source: one taken before the first confirm is stale by the second.
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
            elif action == "promote":
                assert proposal.target is not None
                target_fact = doc.fact(proposal.target.fact.statement)
                promoted = doc.promote(target_fact, kind=new_fact.kind)
                confirmed_refs = list(promoted.refs)
                for sid in refs_to_use:
                    target_fact = doc.fact(promoted.statement)
                    doc.confirm(target_fact, source=sid, date=new_fact.date)
                    confirmed_refs.append(sid)
                report.promoted.append(
                    PromotedFact(
                        statement=target_fact.statement,
                        kind=new_fact.kind,
                        source=new_fact.source,
                        date=new_fact.date,
                        refs=tuple(confirmed_refs),
                        path=rel_target,
                    )
                )
            elif action == "supersede":
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
                        by=refs_to_use[0] if refs_to_use else "",
                        path=rel_target,
                    )
                )
            elif action in ("conflict", "needs-you"):
                assert proposal.target is not None
                target_fact = doc.fact(proposal.target.fact.statement)
                if refs_to_use and not any(f"[^{r}]" in new_fact.statement for r in refs_to_use):
                    ref_part = " ".join(f"[^{r}]" for r in refs_to_use)
                    note_text = f"{new_fact.statement} ({ref_part})"
                else:
                    note_text = new_fact.statement
                callout_type = "conflict" if action == "conflict" else "question"
                doc.add_callout(target_fact, callout_type, note_text, today)
                if action == "conflict":
                    rule_name = outcome.rule if isinstance(outcome, rules.Downgrade) else "conflict"
                    report.conflicts.append(
                        ConflictFact(
                            new_statement=new_fact.statement,
                            target_statement=proposal.target.fact.statement,
                            reason=proposal.decision.reason,
                            rule=rule_name,
                            refs=tuple(refs_to_use),
                            path=rel_target,
                        )
                    )
                else:
                    report.questions.append(
                        QuestionFact(
                            new_statement=new_fact.statement,
                            target_statement=proposal.target.fact.statement,
                            reason=proposal.decision.reason,
                            rule=(
                                outcome.rule
                                if isinstance(outcome, rules.Downgrade)
                                else "needs-you"
                            ),
                            refs=tuple(refs_to_use),
                            path=rel_target,
                        )
                    )
                if isinstance(outcome, rules.Downgrade):
                    report.downgrades.append(
                        DowngradeFact(
                            statement=new_fact.statement,
                            to_action=action,
                            rule=outcome.rule,
                            reason=proposal.decision.reason,
                        )
                    )

            record_write(target_file)
            pagewrite.atomic_write_text(target_file, doc.render())
        return True

    try:
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
                if unfiled and scope != "global":
                    # A session of no project has no project page for this fact; it stays
                    # in the digest, where search finds it (DESIGN 7).
                    report.skips.append(SkipFact(statement=fact.statement, reason=UNFILED_SKIP))
                    continue
                explicit_correction = bool(meta.get("explicit_correction", False))

                new_fact = rules.NewFact(
                    statement=fact.statement,
                    kind=fact.kind or "fact",
                    source=fact.source,
                    date=fact.date,
                    scope=scope,
                    term=digest.term_of(meta),
                    explicit_correction=explicit_correction,
                )

                # R1 trust rule: a quote must verify against its source before a line is written
                # R1: a quote must verify against its source before a line is written. Each ref
                # is dated by its own evidence entry; the digest line's date is that day, and it
                # is only the fallback for a ref the archive no longer holds.
                evidence_list: list[rules.Evidence] = []
                for ref in fact.refs:
                    s_entry = sources_map.get(ref, {})
                    res = str(s_entry.get("resource", ""))
                    q = str(s_entry.get("quote", ""))
                    v, origin, ev_date, y = _lookup_evidence(
                        config, res, q, fallback_date=fact.date or today, source_entry=s_entry
                    )
                    evidence_list.append(
                        rules.Evidence(
                            source=ref,
                            resource=res,
                            origin=digest_role if origin == rules.PRIMARY else origin,
                            date=ev_date,
                            verified=v,
                            yours=y,
                        )
                    )

                settle(new_fact, evidence_list, sources_map)
                if budget_stop:
                    break

            if budget_stop:
                # The budget ran out inside this digest: its remaining facts were never
                # decided, so it is left unconsumed and the digests after it wait for the
                # next run, which applies this one again from its start (DESIGN 13). The
                # facts applied so far stand under this run's commit.
                break

            # Mark digest consumed with run id
            dfm, dbody, _, _ = page.split_frontmatter(d_text)
            if dfm is not None:
                daft = dfm.setdefault("aft", {})
                daft["consumed"] = run_id
                new_fm_yaml = yaml.safe_dump(dfm, sort_keys=False, allow_unicode=True).strip()
                record_write(digest_path)
                pagewrite.atomic_write_text(digest_path, f"---\n{new_fm_yaml}\n---\n{dbody}")
            report.digests_consumed.append(digest_path.relative_to(vault_path).as_posix())

        # The short-term machinery edits ``projects/<page_slug>/recent.md``. Until the run
        # registers a project, that name is the group's pending one, which the project
        # takes at its first project fact; when a registered project already holds it, the
        # page there belongs to that project and not to this group, and the run leaves it
        # alone (DESIGN 7, F49).
        if project is not None or registry.by_slug(page_slug) is None:
            # What the newest session this run applied shows about the short-term lines: a
            # line it showed done, false or replaced is withdrawn on its verified quote
            # (BUILD-PLAN F47).
            reconcile.run(
                config, vault_path, page_slug, digests, today, report, record_write, now=now
            )

            # Short-term memory: lift the lines that became long-term, then roll off the
            # lines whose sessions have gone by, and expire the ones their days have left
            # behind (design 6, design 9 step 6). The session order is counted over the
            # digests the group was filed under, and the page it edits is the project's
            # (DESIGN 6, F49).
            _short_term(
                config, vault_path, slug, page_slug, to_lift, settle, report, record_write, today
            )
        # Maintenance
        broken, cleared = maintain.check_anchors(
            vault_path, project, today, config.host, record_write=record_write
        )
        report.broken_anchors.extend(broken)
        report.cleared_anchors.extend(cleared)

        archived = maintain.archive_superseded(
            vault_path, page_slug, today, record_write=record_write
        )
        report.archived_lines.extend(archived)

        project_dir = vault_path / "projects" / page_slug
        project_pages = list(project_dir.glob("*.md")) if project_dir.is_dir() else []
        oversize = maintain.check_oversize(vault_path, project_pages)
        report.oversize_pages.extend(oversize)

        if not unfiled:
            maintain.regenerate_index(
                vault_path,
                page_slug,
                project.title if project else page_slug.capitalize(),
                record_write=record_write,
            )
        maintain.ensure_root_index(vault_path, record_write=record_write)
        maintain.append_log(
            vault_path, page_slug, f"{today}: dream {page_slug}", record_write=record_write
        )

        # Lint check: lint only the pages this run wrote, not the whole vault
        rel_written = [
            p.relative_to(vault_path) for p in written_paths if p.is_relative_to(vault_path)
        ]
        try:
            reg = Registry.load(vault_path / "registry.yaml")
        except Exception:
            reg = None
        findings = lint.lint_pages(vault_path, rel_written, reg)
        if findings:
            rollback()
            raise ApplyError(f"lint failed on {len(findings)} finding(s): {findings[0]}")

        # One commit per project run
        status = git(["status", "--porcelain=v1"], cwd=vault_path).stdout.strip()
        if status:
            rel_written = [
                p.relative_to(vault_path).as_posix()
                for p in written_paths
                if p.is_relative_to(vault_path)
            ]
            if rel_written:
                git(["add", "--", *rel_written], cwd=vault_path)
                staged = git(["diff", "--cached", "--name-only"], cwd=vault_path).stdout.strip()
                if staged:
                    git(
                        [
                            "commit",
                            "-q",
                            "-m",
                            f"aft: dream {page_slug}",
                            "-m",
                            f"Run: {run_id}",
                        ],
                        cwd=vault_path,
                    )
                    report.commit = git(["rev-parse", "HEAD"], cwd=vault_path).stdout.strip()
                else:
                    report.commit = None
            else:
                report.commit = None
        else:
            report.commit = None
    except Exception:
        rollback()
        raise

    return report


def _recent_page(vault_path: Path, slug: str) -> Path:
    """The project's short-term page, ``projects/<slug>/recent.md`` (design 6)."""
    path = vault_path / "projects" / slug / page.RECENT_PAGE
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _recent_text(slug: str, project, project_id: str | None) -> str:
    """A new short-term page: frontmatter only, its lines added by the writer."""
    title = project.title if project else slug.capitalize()
    frontmatter = {
        "type": "Project",
        "title": f"{title} recent",
        "description": "Short-term memory: what the project's last few sessions were working with.",
        "sources": [],
        "aft": {"project": project_id or (project.id if project else f"p_{slug}")},
    }
    return f"---\n{yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)}---\n"


def _short_term(
    config: Config,
    vault_path: Path,
    slug: str,
    page_slug: str,
    to_lift: list[tuple[str, str]],
    settle,
    report: ProjectRunReport,
    record_write,
    today: str,
) -> None:
    """Lift the short-term lines that became long-term, then roll off the lines whose
    sessions have gone by, and expire the ones their days have left behind (design 6,
    design 9 step 6, R10).

    A line is lifted when a later session's long-term fact confirmed it (``to_lift``), or
    when its sources are at least ``dream.short_term_sessions`` sessions apart. Lifting
    puts the line, with every source it gathered, to the decision as a long-term fact
    through ``settle``, which decides it against long-term memory alone (R10): the
    retrieval leaves ``recent.md`` out, so the line is never its own target. The line
    leaves ``recent.md`` once its fact stands on a long-term page, or the decision
    skipped the fact, which is the decision itself dropping it. A line rolls off once its
    newest source has left the project's
    last ``short_term_sessions`` sessions, whatever its state; a note under it moves to the
    archive first, so nothing a person typed there is lost. A project that stops having
    sessions never rolls a line off by count, so a line whose newest source is older than
    ``dream.short_term_days`` days expires the same way roll-off does.

    ``slug`` is the directory the group's digests were filed under, which is where the
    session order is read from; ``page_slug`` is the project's slug, which is where
    ``recent.md`` lives. They are the same unless the project was registered under a name
    the group's directory could not keep (DESIGN 7, F49).
    """
    recent = vault_path / "projects" / page_slug / page.RECENT_PAGE
    if not recent.exists():
        return
    window = config.settings.dream.short_term_sessions
    order = shortterm.sessions(vault_path, slug)

    doc = pagewrite.PageDoc.read(recent, vault=vault_path)
    for fact in doc.page.facts:
        if fact.state != page.ACTIVE or any(s == fact.statement for s, _ in to_lift):
            continue
        if shortterm.outlasted(shortterm.positions(fact, doc.sources, order, vault_path), window):
            to_lift.append((fact.statement, LIFT_OUTLASTED))

    # settle can lift a further line while one is decided, so the list grows as it is read.
    done: set[str] = set()
    index = 0
    while index < len(to_lift):
        statement, why = to_lift[index]
        index += 1
        if statement in done:
            continue
        done.add(statement)
        doc = pagewrite.PageDoc.read(recent, vault=vault_path)
        line = next(
            (f for f in doc.page.facts if f.statement == statement and f.state == page.ACTIVE),
            None,
        )
        if line is None or line.kind is None:
            continue
        sources = doc.sources
        evidence_list = []
        for ref in line.refs:
            entry = sources.get(ref, {})
            resource = str(entry.get("resource", ""))
            verified, origin, when, yours = _lookup_evidence(
                config,
                resource,
                str(entry.get("quote", "")),
                fallback_date=line.date,
                source_entry=entry,
            )
            evidence_list.append(
                rules.Evidence(
                    source=ref,
                    resource=resource,
                    origin=origin,
                    date=when,
                    verified=verified,
                    yours=yours,
                )
            )
        survived = settle(
            rules.NewFact(
                statement=line.statement,
                kind=line.kind,
                source=line.source,
                date=line.date,
                term=rules.LONG,
            ),
            evidence_list,
            sources,
            lifting=True,
        )
        if not survived:
            continue
        # The line leaves short-term memory only once its fact stands on a long-term page:
        # the decision wrote it there, or the decision skipped the fact, which is the
        # decision itself dropping it (the report says so). A lift that stored nothing
        # would otherwise take the line off recent.md and lose it.
        dropped = any(entry.statement == statement for entry in report.skips)
        if not dropped and not _stored_long_term(
            vault_path, page_slug, statement, sources, line.refs
        ):
            continue
        doc = pagewrite.PageDoc.read(recent, vault=vault_path)
        cur_line = next(
            (f for f in doc.page.facts if f.statement == statement and f.state == page.ACTIVE),
            None,
        )
        if cur_line is not None:
            doc.remove_short_term(cur_line)
            record_write(recent)
            pagewrite.atomic_write_text(recent, doc.render())
        report.lifted.append(LiftedFact(statement=statement, why=why))

    doc = pagewrite.PageDoc.read(recent, vault=vault_path)
    changed = False
    while True:
        sources = doc.sources
        going = next(
            (
                f
                for f in doc.page.facts
                if shortterm.rolls_off(
                    shortterm.positions(f, sources, order, vault_path), order, window
                )
            ),
            None,
        )
        if going is None:
            break
        if going.callouts:
            record_write(doc._archive_file())
            doc.resolve_callout(going)
            going = next(f for f in doc.page.facts if f.line == going.line)
        doc.remove_short_term(going)
        report.rolled_off.append(RolledOffLine(statement=going.statement, state=going.state))
        changed = True
    # The day backstop (F47): counted in sessions, a line of a project that stops having
    # sessions never leaves by count, so what time has left behind expires the same way.
    # A line the reconcile withdrew in this run is already gone by its own verdict: it
    # stands struck as the record of what the newest session showed, and the backstop
    # would strike the same line again in one run and list it twice in the report.
    settled = {line.statement for line in report.withdrawn}
    if _expired_lines(config, vault_path, doc, report, today, settled, record_write):
        changed = True
    if changed:
        record_write(recent)
        pagewrite.atomic_write_text(recent, doc.render())


def _stored_long_term(
    vault_path: Path,
    page_slug: str,
    statement: str,
    sources: dict[str, dict],
    refs: tuple[str, ...],
) -> bool:
    """Whether the fact a lift is settling now stands on a long-term page.

    Its statement written on one, as an added, superseded, candidate or noted line, or one
    of the lifted line's own sources cited from one: a confirm judges the fact to be a
    long-term line already standing and moves the evidence onto it. A page that cannot be
    read holds nothing.
    """
    quoted = set()
    for ref in refs:
        entry = sources.get(ref, {})
        quoted.add((str(entry.get("resource", "")), str(entry.get("quote", ""))))
    quoted.discard(("", ""))
    project_dir = vault_path / "projects" / page_slug
    pages = (
        [
            path
            for path in sorted(project_dir.rglob("*.md"))
            if not page.is_recent(path.relative_to(vault_path))
        ]
        if project_dir.is_dir()
        else []
    )
    for path in pages:
        if not path.is_file():
            continue
        try:
            doc = pagewrite.PageDoc.read(path, vault=vault_path)
        except (OSError, UnicodeDecodeError, pagewrite.WriteError):
            continue
        page_sources = doc.sources
        for fact in doc.page.facts:
            if fact.statement == statement:
                return True
            if any(statement in "\n".join(callout.text) for callout in fact.callouts):
                return True
            for ref in fact.refs:
                entry = page_sources.get(ref, {})
                if (str(entry.get("resource", "")), str(entry.get("quote", ""))) in quoted:
                    return True
    return False


def expire_short_term(
    config: Config, slug: str, now: datetime | None = None
) -> tuple[ProjectRunReport, list[str]] | None:
    """Expire one project's short-term lines whose days are spent, and write the page.

    The same expiry a project's own run applies, for a project with nothing waiting: its
    sessions stopped, so no run applies its digests, and its stale lines would wait for a
    digest that never comes (DESIGN 6, R10). A note under an expiring line moves to the
    archive first, the way roll-off moves one. Returns the report and the vault-relative
    paths it wrote, or None when the page has nothing to expire.
    """
    vault_path = config.vault.resolve()
    recent = vault_path / "projects" / slug / page.RECENT_PAGE
    if not recent.exists():
        return None
    now = now or datetime.now(UTC)
    report = ProjectRunReport(project=slug)
    written: list[str] = []

    def record_write(p: Path) -> None:
        rel = p.relative_to(vault_path).as_posix()
        if rel not in written:
            written.append(rel)

    doc = pagewrite.PageDoc.read(recent, vault=vault_path)
    if not _expired_lines(
        config, vault_path, doc, report, now.date().isoformat(), set(), record_write
    ):
        return None
    record_write(recent)
    pagewrite.atomic_write_text(recent, doc.render())
    return report, written


def _expired_lines(
    config: Config,
    vault_path: Path,
    doc: pagewrite.PageDoc,
    report: ProjectRunReport,
    today: str,
    settled: set[str],
    record_write,
) -> bool:
    """Take every line whose newest source is older than ``dream.short_term_days`` off the
    page ``doc`` holds, and say whether anything went.

    One line at a time, its dates read from the page as it stands, so a line's leaving does
    not disturb the dates the rest are judged by. A note under an expiring line moves to
    the archive first, the way roll-off moves one, so nothing a person typed there is lost.
    A line the caller names in ``settled`` stays: the reconcile withdrew it in this run
    already, and the report must not list it twice.
    """
    changed = False
    while True:
        going = next(
            (
                f
                for f in doc.page.facts
                if f.statement not in settled
                and _expired(config, f, doc.sources, vault_path, today)
            ),
            None,
        )
        if going is None:
            return changed
        if going.callouts:
            record_write(doc._archive_file())
            doc.resolve_callout(going)
            going = next(f for f in doc.page.facts if f.line == going.line)
        doc.remove_short_term(going)
        report.expired.append(ExpiredLine(statement=going.statement))
        changed = True


def _expired(
    config: Config,
    fact: page.Fact,
    sources: dict[str, dict],
    vault_path: Path,
    today: str,
) -> bool:
    """Whether the line's newest source is older than ``dream.short_term_days`` days.

    A source's date is the day its evidence entry was said, or the commit's for a hand
    edit, or the line's own day when the archive no longer holds the entry (DESIGN 14
    keeps the quote on the page); a line whose sources carry no date at all stays, as a
    line no source can place stays for roll-off.
    """
    newest = _newest_source_date(config, fact, sources, vault_path)
    if not newest:
        return False
    try:
        age = (date.fromisoformat(today) - date.fromisoformat(newest)).days
    except ValueError:
        return False
    return age > config.settings.dream.short_term_days


def _newest_source_date(
    config: Config, fact: page.Fact, sources: dict[str, dict], vault_path: Path
) -> str:
    """The newest date among the line's sources, as their evidence carries it."""
    dates = []
    for ref in fact.refs:
        entry = sources.get(ref, {})
        _, _, when, _ = _lookup_evidence(
            config,
            str(entry.get("resource", "")),
            str(entry.get("quote", "")),
            fallback_date=fact.date,
            source_entry=entry,
        )
        if when:
            dates.append(when)
    return max(dates) if dates else ""


def _worker_digest_names(config: Config, agent: str, sess_id: str) -> bool:
    """Whether one of this session's digest pages carries ``aft.role: worker``.

    The evidence archive stores no worker role, so the digest page is what says a session
    was a worker's (DESIGN 8). ``digest._path`` names a digest
    ``<date>-<agent>-<session>.md``, with ``-<part>`` before the extension when a session
    is digested more than once in a day, so the name must have that shape and the agent
    must match too: a session id that is another session's prefix, and the same id under
    another agent, keep their own role.
    """
    digests_dir = config.vault / "digests"
    if not digests_dir.is_dir():
        return False
    later_part = re.compile(rf"{re.escape(sess_id)}-\d+")
    for path in digests_dir.rglob("*.md"):
        named = digest._DIGEST_NAME.match(path.stem)
        if named is None or (agent and named.group("agent") != agent):
            continue
        session = named.group("session")
        if session != sess_id and not later_part.fullmatch(session):
            continue
        try:
            frontmatter, _, _, _ = page.split_frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        aft_dict = frontmatter.get("aft") if isinstance(frontmatter, dict) else None
        if isinstance(aft_dict, dict) and aft_dict.get("role") == "worker":
            return True
    return False


def _lookup_library_evidence(
    config: Config, resource: str, quote: str, fallback_date: str
) -> tuple[bool, str, str, bool]:
    """Whether a collection quote stands, and what it is worth.

    No session stands behind an imported quote, so it verifies a fact and carries no recurrence:
    the dream counts none of it toward R3 however many imported files repeat the sentence, and the
    fact waits as a candidate (DESIGN 11 step 5).

    The copy in the vault is first weighed to the hash the import recorded, so a sentence written
    into that copy afterwards verifies nothing. The quote is then looked for in the extracted text
    the index and the reference pages carry - the same text `aft library propose` checked it
    against, page breaks joined - and the extraction is rebuilt from the file when the cache under
    the home directory is gone. What a person excluded keeps no copy at all, so its proof is the
    record `aft library propose` left, which holds the quote as it was checked.
    """


def _lookup_library_evidence(
    config: Config,
    resource: str,
    quote: str,
    fallback_date: str = "",
    source_entry: dict | None = None,
) -> tuple[bool, str, str, bool]:
    from aft.library import importing
    from aft.library.propose import is_verified_import_quote
    from aft.library.survey import file_sha256

    rel = resource.removeprefix(evidence.LIBRARY_RESOURCE).strip()
    collection, _, within = rel.partition("/")
    if not collection or not within:
        return False, rules.LIBRARY, fallback_date, False

    recorded_said = str(source_entry.get("said", "")) if source_entry else ""
    ev_date = recorded_said or fallback_date
    stands = True, rules.LIBRARY, ev_date, False
    refused = False, rules.LIBRARY, ev_date, False

    # F75: Verify digest-time check record if present
    recorded_check = source_entry.get("check") if source_entry else None
    if recorded_check:
        expected_check = evidence.quote_check(collection, within, quote)
        if recorded_check == expected_check:
            return stands

    lib_root = (config.vault / "library" / collection).resolve()
    lib_path = (lib_root / within).resolve()
    if lib_path.is_relative_to(lib_root) and lib_path.is_file():
        record = importing.read_import_record(config.vault, collection)
        recorded = record.hashes.get(within) if record is not None else None
        if recorded is None or file_sha256(lib_path) != recorded:
            return refused
        text = importing.extracted_text(lib_path, config.home)
        if evidence._spaced(quote) in evidence._spaced(text):
            return stands
        return refused

    if is_verified_import_quote(config.home, resource, quote):
        return stands
    return refused


def _lookup_evidence(
    config: Config,
    resource: str,
    quote: str,
    fallback_date: str = "",
    source_entry: dict | None = None,
) -> tuple[bool, str, str, bool]:
    """Look up an evidence footnote in the archive or library.

    Returns (verified, origin, date, yours).
    """
    quote = quote.strip()
    if not quote or not resource:
        return False, rules.PRIMARY, fallback_date, False

    if evidence.is_library_resource(resource):
        return _lookup_library_evidence(
            config, resource, quote, fallback_date=fallback_date, source_entry=source_entry
        )

    if resource.startswith("aft://session/"):
        uri = resource.removeprefix("aft://session/")
        parts = uri.split("#", 1)
        if len(parts) == 2:
            agent_sess = parts[0].split("/")
            agent = agent_sess[0] if len(agent_sess) > 1 else ""
            sess_id = agent_sess[-1]
            entry_id = parts[1]

            # F75: Check digest-time verification record
            recorded_check = source_entry.get("check") if source_entry else None
            if recorded_check:
                recorded_role = str(source_entry.get("role", "")) if source_entry else ""
                recorded_said = str(source_entry.get("said", "")) if source_entry else ""
                expected_check = evidence.quote_check(
                    sess_id, entry_id, recorded_role, recorded_said, quote
                )
                if recorded_check == expected_check:
                    is_worker = _worker_digest_names(config, agent, sess_id)
                    origin = rules.WORKER if is_worker else rules.PRIMARY
                    db_path = config.home / "evidence.sqlite"
                    row = None
                    if db_path.exists():
                        try:
                            with evidence._reading(config) as conn:
                                query = (
                                    "SELECT time, role FROM evidence "
                                    "WHERE session_id = ? AND entry_id = ?"
                                )
                                row = conn.execute(query, (sess_id, entry_id)).fetchone()
                        except Exception:
                            row = None
                    if row is not None:
                        ev_date = str(row[0])[:10]
                        is_yours = str(row[1]) == "user"
                    else:
                        ev_date = recorded_said or fallback_date
                        is_yours = recorded_role == "user" if recorded_role else False
                    return True, origin, ev_date, is_yours

            # Pre-F75 or hash mismatch: verify against archive
            db_path = config.home / "evidence.sqlite"
            if db_path.exists():
                try:
                    if evidence.find_quote(config, sess_id, entry_id, quote):
                        with evidence._reading(config) as conn:
                            query = (
                                "SELECT time, role FROM evidence "
                                "WHERE session_id = ? AND entry_id = ?"
                            )
                            row = conn.execute(query, (sess_id, entry_id)).fetchone()
                        if row is not None:
                            entry_time, entry_role = str(row[0]), str(row[1])
                            entry_date = entry_time[:10]
                            is_yours = entry_role == "user"
                            is_worker = entry_role == "worker"
                            if not is_worker:
                                is_worker = _worker_digest_names(config, agent, sess_id)
                            origin = rules.WORKER if is_worker else rules.PRIMARY
                            return True, origin, entry_date, is_yours
                except Exception:
                    return False, rules.PRIMARY, fallback_date, False
        return False, rules.PRIMARY, fallback_date, False

    if resource.startswith("aft://edit/"):
        sha = resource.removeprefix("aft://edit/").strip()
        if not sha or not re.match(r"^[0-9a-f]{7,40}$", sha):
            return False, rules.PRIMARY, fallback_date, False
        try:
            res = git(
                ["show", "--format=", "--end-of-options", sha],
                cwd=config.vault,
                check=False,
            )
            if res.returncode == 0:
                spaced_quote = evidence._spaced(quote)
                current_run: list[str] = []
                matched = False
                for line in res.stdout.splitlines():
                    if line.startswith("+") and not line.startswith("+++"):
                        current_run.append(line[1:])
                    else:
                        if current_run:
                            if spaced_quote in evidence._spaced("\n".join(current_run)):
                                matched = True
                                break
                            current_run = []
                if not matched and current_run:
                    if spaced_quote in evidence._spaced("\n".join(current_run)):
                        matched = True
                if matched:
                    c_res = git(
                        ["show", "-s", "--format=%ct", "--end-of-options", sha],
                        cwd=config.vault,
                        check=False,
                    )
                    if c_res.returncode == 0:
                        stamp = int(c_res.stdout.strip())
                        c_date = datetime.fromtimestamp(stamp, UTC).date().isoformat()
                        return True, rules.PRIMARY, c_date, False
        except Exception:
            return False, rules.PRIMARY, fallback_date, False
        return False, rules.PRIMARY, fallback_date, False

    return False, rules.PRIMARY, fallback_date, False


def _is_quote_verified(config: Config, resource: str, quote: str) -> bool:
    v, _, _, _ = _lookup_evidence(config, resource, quote)
    return v


def _is_quote_yours(config: Config, resource: str, recorded_role: str = "") -> bool:
    if evidence.is_library_resource(resource):
        return False
    if resource.startswith("aft://edit/"):
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
                try:
                    with evidence._reading(config) as conn:
                        row = conn.execute(
                            "SELECT role FROM evidence WHERE session_id = ? AND entry_id = ?",
                            (sess_id, entry_id),
                        ).fetchone()
                    if row is not None:
                        return str(row[0]) == "user"
                except Exception:
                    pass
            if recorded_role:
                return recorded_role == "user"
    return False


def _turn_text(config: Config, resource: str) -> str | None:
    if resource.startswith("aft://session/"):
        uri = resource.removeprefix("aft://session/")
        parts = uri.split("#", 1)
        if len(parts) == 2:
            agent_sess = parts[0].split("/")
            agent = agent_sess[0] if len(agent_sess) > 1 else None
            sess_id = agent_sess[-1]
            entry_id = parts[1]
            return evidence.entry_text(config, sess_id, entry_id, agent=agent or None)
    return None
