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

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from aft import evidence, lint, page, pagewrite
from aft import vault as vaultmod
from aft.config import Config
from aft.dream import decide, maintain, rules
from aft.gitutil import git
from aft.registry import Registry


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
    # A digest is marked consumed by the same commit that consumes it, so the marker
    # cannot hold that commit's sha: it holds this run id instead, and the commit
    # message names the same id, so `git log --grep <run id>` answers with the sha.
    run_id = f"dream-{slug}-{now:%Y%m%dT%H%M%SZ}"
    vault_path = config.vault

    report = ProjectRunReport(project=slug)

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
                p.write_bytes(content)
            except OSError:
                pass

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
                    v, origin, ev_date, y = _lookup_evidence(
                        config, res, q, fallback_date=fact.date, source_entry=s_entry
                    )
                    evidence_list.append(
                        rules.Evidence(
                            source=ref,
                            resource=res,
                            origin=digest_role if origin == rules.PRIMARY else origin,
                            date=fact.date,
                            verified=v,
                            yours=y,
                        )
                    )

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
                    continue

                quote_to_send = (
                    sources_map.get(verified_evidence[0].source, {}).get("quote", "")
                    if verified_evidence
                    else ""
                )
                proposal = decide.decide(
                    config,
                    new_fact,
                    quote_to_send,
                    slug if project else None,
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

                effective_project = run_project_id or f"p_{slug}"
                context = proposal.context(
                    today=today,
                    evidence=tuple(evidence_list),
                    run_project=effective_project,
                    line_evidence=tuple(line_evidence),
                )
                outcome = proposal.check(context)

                if isinstance(outcome, rules.Reject):
                    report.rejected.append(
                        RejectedFact(
                            statement=new_fact.statement,
                            decision_action=proposal.decision.action,
                            rule=outcome.rule,
                            reason=proposal.decision.reason,
                        )
                    )
                    continue

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
                        project = registry.add(slug, title=slug.capitalize())
                        record_write(config.registry_path)
                        registry.save()
                        run_project_id = project.id

                    if proposal.target is not None and action in (
                        "confirm",
                        "supersede",
                        "promote",
                        "conflict",
                        "needs-you",
                    ):
                        target_file = vault_path / proposal.target.path
                        record_write(target_file)
                    elif new_fact.scope == "global":
                        g_file = (
                            "preferences.md" if new_fact.kind == "preference" else "environment.md"
                        )
                        target_file = vault_path / "global" / g_file
                        if not target_file.exists():
                            target_file.parent.mkdir(parents=True, exist_ok=True)
                            content = (
                                vaultmod.PREFERENCES
                                if new_fact.kind == "preference"
                                else vaultmod.ENVIRONMENT
                            )
                            record_write(target_file)
                            target_file.write_text(content, encoding="utf-8")
                        else:
                            record_write(target_file)
                    else:
                        overview_path = vault_path / "projects" / slug / "overview.md"
                        if not overview_path.exists():
                            overview_path.parent.mkdir(parents=True, exist_ok=True)
                            proj_id = run_project_id or (project.id if project else f"p_{slug}")
                            proj_title = project.title if project else slug.capitalize()
                            record_write(overview_path)
                            overview_path.write_text(
                                f"---\n"
                                f"type: Project\n"
                                f"title: {proj_title}\n"
                                f"description: {slug}\n"
                                f"sources: []\n"
                                f"aft:\n"
                                f"  project: {proj_id}\n"
                                f"---\n",
                                encoding="utf-8",
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

                        sid: str | None = None
                        for existing_id, existing_s in doc.sources.items():
                            if existing_s.get("resource") == res and existing_s.get("quote") == q:
                                sid = existing_id
                                break
                        if sid is None and res and q:
                            sid = doc.add_source(resource=res, title=title, quote=q)
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
                                    outcome.rule
                                    if isinstance(outcome, rules.Downgrade)
                                    else "candidate"
                                ),
                                reason=proposal.decision.reason,
                            )
                        )
                    elif action == "confirm":
                        assert proposal.target is not None
                        target_fact = doc.fact(proposal.target.fact.statement)
                        for sid in refs_to_use:
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
                            doc.confirm(promoted, source=sid, date=new_fact.date)
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
                        reason = (
                            proposal.decision.reason.strip()
                            if proposal.decision.reason
                            else new_fact.statement
                        )
                        if refs_to_use and not any(f"[^{r}]" in reason for r in refs_to_use):
                            ref_part = " ".join(f"[^{r}]" for r in refs_to_use)
                            note_text = f"{reason} ({ref_part})"
                        else:
                            note_text = reason
                        callout_type = "conflict" if action == "conflict" else "question"
                        doc.add_callout(target_fact, callout_type, note_text, today)
                        if action == "conflict":
                            rule_name = (
                                outcome.rule if isinstance(outcome, rules.Downgrade) else "conflict"
                            )
                            report.conflicts.append(
                                ConflictFact(
                                    new_statement=new_fact.statement,
                                    target_statement=proposal.target.fact.statement,
                                    reason=proposal.decision.reason,
                                    rule=rule_name,
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
                                )
                            )

                    record_write(target_file)
                    target_file.write_text(doc.render(), encoding="utf-8")

            # Mark digest consumed with run id
            dfm, dbody, _, _ = page.split_frontmatter(d_text)
            if dfm is not None:
                daft = dfm.setdefault("aft", {})
                daft["consumed"] = run_id
                new_fm_yaml = yaml.safe_dump(dfm, sort_keys=False, allow_unicode=True).strip()
                record_write(digest_path)
                digest_path.write_text(f"---\n{new_fm_yaml}\n---\n{dbody}", encoding="utf-8")
            report.digests_consumed.append(digest_path.relative_to(vault_path).as_posix())

        # Maintenance
        broken, cleared = maintain.check_anchors(
            vault_path, project, today, config.host, record_write=record_write
        )
        report.broken_anchors.extend(broken)
        report.cleared_anchors.extend(cleared)

        archived = maintain.archive_superseded(vault_path, slug, today, record_write=record_write)
        report.archived_lines.extend(archived)

        project_dir = vault_path / "projects" / slug
        project_pages = list(project_dir.glob("*.md")) if project_dir.is_dir() else []
        oversize = maintain.check_oversize(vault_path, project_pages)
        report.oversize_pages.extend(oversize)

        maintain.regenerate_index(
            vault_path,
            slug,
            project.title if project else slug.capitalize(),
            record_write=record_write,
        )
        maintain.ensure_root_index(vault_path, record_write=record_write)
        maintain.append_log(vault_path, slug, f"{today}: dream {slug}", record_write=record_write)

        # Lint check
        findings = lint.lint_vault(vault_path)
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
                            f"aft: dream {slug}",
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

    if resource.startswith("aft://library/"):
        rel = resource.removeprefix("aft://library/").strip()
        lib_root = (config.vault / "library").resolve()
        lib_path = (lib_root / rel).resolve()
        if lib_path.is_relative_to(lib_root) and lib_path.is_file():
            try:
                content = lib_path.read_text(encoding="utf-8", errors="replace")
                if evidence._spaced(quote) in evidence._spaced(content):
                    return True, rules.PRIMARY, fallback_date, False
            except Exception:
                pass
            try:
                import hashlib
                import json

                f_hash = hashlib.sha256(lib_path.read_bytes()).hexdigest()
                cache_file = config.home / "library" / f"{f_hash}.json"
                if cache_file.is_file():
                    data = json.loads(cache_file.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and "chunks" in data:
                        for chunk in data["chunks"]:
                            chunk_text = chunk.get("text", "")
                            if evidence._spaced(quote) in evidence._spaced(chunk_text):
                                return True, rules.PRIMARY, fallback_date, False
            except Exception:
                pass

        # Check tamper-proof verified quotes record written by propose (for excluded files)
        from aft.library.propose import is_verified_import_quote

        if is_verified_import_quote(config.home, resource, quote):
            return True, rules.PRIMARY, fallback_date, False

        return False, rules.PRIMARY, fallback_date, False

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
                                for d_path in (config.vault / "digests").glob(
                                    f"*/*/*/*{sess_id}*.md"
                                ):
                                    try:
                                        dfm, _, _, _ = page.split_frontmatter(
                                            d_path.read_text(encoding="utf-8")
                                        )
                                        if (
                                            dfm
                                            and isinstance(dfm.get("aft"), dict)
                                            and dfm["aft"].get("role") == "worker"
                                        ):
                                            is_worker = True
                                            break
                                    except OSError:
                                        pass
                            origin = rules.WORKER if is_worker else rules.PRIMARY
                            return True, origin, entry_date, is_yours
                except Exception:
                    return False, rules.PRIMARY, fallback_date, False
        return False, rules.PRIMARY, fallback_date, False

    if resource.startswith("aft://edit/"):
        sha = resource.removeprefix("aft://edit/").strip()
        if not sha:
            return False, rules.PRIMARY, fallback_date, False
        try:
            res = git(["show", "--format=", sha], cwd=config.vault, check=False)
            if res.returncode == 0 and evidence._spaced(quote) in evidence._spaced(res.stdout):
                c_res = git(["show", "-s", "--format=%ct", sha], cwd=config.vault, check=False)
                if c_res.returncode == 0:
                    stamp = int(c_res.stdout.strip())
                    c_date = datetime.fromtimestamp(stamp, UTC).date().isoformat()
                    return True, rules.PRIMARY, c_date, True
        except Exception:
            return False, rules.PRIMARY, fallback_date, False
        return False, rules.PRIMARY, fallback_date, False

    return False, rules.PRIMARY, fallback_date, False


def _is_quote_verified(config: Config, resource: str, quote: str) -> bool:
    v, _, _, _ = _lookup_evidence(config, resource, quote)
    return v


def _is_quote_yours(config: Config, resource: str, default: bool = False) -> bool:
    if resource.startswith("aft://library/"):
        return False
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
                try:
                    with evidence._reading(config) as conn:
                        row = conn.execute(
                            "SELECT role FROM evidence WHERE session_id = ? AND entry_id = ?",
                            (sess_id, entry_id),
                        ).fetchone()
                    if row is not None:
                        return str(row[0]) == "user"
                except Exception:
                    return False
    return default
