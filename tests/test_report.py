"""Tests for M4.6: report and needs-you (DESIGN 9).

Verifies:
1. Appending one section per run to reports/<yyyy>/<mm>/<date>.md
2. Second run on a day appends to the same file
3. Fixed 9-section order in reports
4. Regeneration of reports/needs-you.md listing every open conflict and question with links
5. Reports are indexed only in the reports collection
6. The dream's inputs never include a report path
7. Temp dirs only
"""

import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aft import evidence, index, pagewrite, queue, search, vault
from aft.config import Config
from aft.dream import apply, decide, maintain, report, rules, schedule
from aft.events import SESSION_END, SessionEvent
from aft.gitutil import git
from aft.registry import Registry, path_key
from conftest import make_repo, run_git
from fakes.model_server import FakeEndpoint, Reply

NOW = datetime(2026, 10, 4, 14, 30, tzinfo=UTC)
TODAY = "2026-10-04"


def _before_now(cfg: Config) -> None:
    """Dates every file in the vault an hour before NOW: the dream's quiet period reads
    real modification times, and a file the test wrote moments ago would otherwise be a
    hand edit made after NOW."""
    stamp = (NOW - timedelta(hours=1)).timestamp()
    for path in cfg.vault.rglob("*"):
        if ".git" not in path.relative_to(cfg.vault).parts:
            os.utime(path, (stamp, stamp), follow_symlinks=False)


def _make_dummy_report(
    slug: str = "widget",
    commit_sha: str = "1111222233334444555566667777888899990000",
    added: list[apply.AddedFact] | None = None,
    confirmed: list[apply.ConfirmedFact] | None = None,
    superseded: list[apply.SupersededFact] | None = None,
    promoted: list[apply.PromotedFact] | None = None,
    candidates: list[apply.CandidateFact] | None = None,
    conflicts: list[apply.ConflictFact] | None = None,
    questions: list[apply.QuestionFact] | None = None,
    skips: list[apply.SkipFact] | None = None,
    rejected: list[apply.RejectedFact] | None = None,
    downgrades: list[apply.DowngradeFact] | None = None,
    broken_anchors: list[maintain.BrokenAnchor] | None = None,
    cleared_anchors: list[maintain.ClearedAnchor] | None = None,
    oversize_pages: list[str] | None = None,
) -> apply.ProjectRunReport:
    """Create a structured ProjectRunReport with given or empty items."""
    return apply.ProjectRunReport(
        project=slug,
        commit=commit_sha,
        digests_consumed=[f"digests/{slug}/2026/10/d1.md"],
        added=added or [],
        confirmed=confirmed or [],
        superseded=superseded or [],
        promoted=promoted or [],
        candidates=candidates or [],
        conflicts=conflicts or [],
        questions=questions or [],
        skips=skips or [],
        rejected=rejected or [],
        downgrades=downgrades or [],
        broken_anchors=broken_anchors or [],
        cleared_anchors=cleared_anchors or [],
        oversize_pages=oversize_pages or [],
    )


def test_section_order_is_fixed(initialized: Config):
    """The 10 sections appear in the exact fixed order specified by DESIGN 9."""
    rep = _make_dummy_report()
    path = report.write_report(initialized, rep, now=NOW, checkpoint="cccc1111")
    assert path.exists()

    content = path.read_text(encoding="utf-8")
    headings = [line.strip() for line in content.splitlines() if line.startswith("## ")]
    expected_order = [
        "## Summary",
        "## Needs you",
        "## Superseded",
        "## Added and Confirmed",
        "## Conflicts",
        "## Rejected",
        "## Stale",
        "## Candidates",
        "## Short term",
        "## Revert",
    ]
    assert headings == expected_order


def test_section_order_and_content_with_populated_data(initialized: Config):
    """Sections render their structured data correctly in the fixed 9-section format."""
    # Seed a page with sources so quote lookup succeeds
    overview = initialized.vault / "projects" / "widget" / "overview.md"
    overview.parent.mkdir(parents=True, exist_ok=True)
    overview.write_text(
        "---\n"
        "type: Project\n"
        "title: Widget\n"
        "description: Widget project\n"
        "sources:\n"
        "  - id: s1\n"
        "    resource: aft://session/c/1#e1\n"
        "    quote: verifiable source quote\n"
        "  - id: s2\n"
        "    resource: aft://session/c/2#e1\n"
        "    quote: superseded quote\n"
        "---\n\n"
        "## State\n\n"
        "- Old fact statement. [observed] 2026-09-01 [^s2]\n",
        encoding="utf-8",
    )

    rep = _make_dummy_report(
        slug="widget",
        commit_sha="abcd1234ef567890",
        added=[
            apply.AddedFact(
                statement="New added fact.",
                kind="decision",
                source="stated",
                date=TODAY,
                refs=("s1",),
                path="projects/widget/overview.md",
            )
        ],
        confirmed=[
            apply.ConfirmedFact(
                statement="Confirmed fact.",
                source="s1",
                date=TODAY,
                refs=("s1",),
                path="projects/widget/overview.md",
            )
        ],
        superseded=[
            apply.SupersededFact(
                old_statement="Old fact statement.",
                new_statement="New fact statement.",
                source="observed",
                date=TODAY,
                refs=("s1",),
                by="s1",
                path="projects/widget/overview.md",
            )
        ],
        promoted=[
            apply.PromotedFact(
                statement="Promoted fact.",
                kind="fact",
                source="inferred",
                date=TODAY,
                refs=("s1",),
                path="projects/widget/overview.md",
            )
        ],
        candidates=[
            apply.CandidateFact(
                statement="Candidate fact line.",
                tag="inferred",
                date=TODAY,
                refs=("s1",),
                path="projects/widget/overview.md",
            )
        ],
        conflicts=[
            apply.ConflictFact(
                new_statement="New conflicting claim.",
                target_statement="Existing claim.",
                reason="Direct contradiction",
                rule="R4",
            )
        ],
        questions=[
            apply.QuestionFact(
                new_statement="Weak claim against preference.",
                target_statement="Preference claim.",
                reason="Preference requires stated authority",
                rule="R4",
            )
        ],
        rejected=[
            apply.RejectedFact(
                statement="Rejected fact.",
                decision_action="add",
                rule="R1",
                reason="quote not verified",
            )
        ],
        broken_anchors=[
            maintain.BrokenAnchor(
                page="projects/widget/overview.md",
                statement="Anchored fact.",
                sid="s1",
                reason="words gone",
            )
        ],
        cleared_anchors=[
            maintain.ClearedAnchor(
                page="projects/widget/overview.md",
                statement="Recovered fact.",
                sid="s1",
            )
        ],
    )

    path = report.write_report(
        initialized,
        rep,
        now=NOW,
        checkpoint="999988887777",
        tokens_used=1500,
        backlog=3,
    )
    text = path.read_text(encoding="utf-8")

    # Fixed headings
    assert "## Summary" in text
    assert "## Needs you" in text
    assert "## Superseded" in text
    assert "## Added and Confirmed" in text
    assert "## Conflicts" in text
    assert "## Rejected" in text
    assert "## Stale" in text
    assert "## Candidates" in text
    assert "## Revert" in text

    # Data content checks
    assert "- Added: 1" in text
    assert "- Tokens used: 1500" in text
    assert "- Backlog remaining: 3" in text
    assert "Weak claim against preference." in text
    assert "Old fact statement." in text
    assert "verifiable source quote" in text
    assert "New conflicting claim." in text
    assert "refused by R1" in text
    assert "words gone" in text
    assert "Recovered fact." in text
    assert "Candidate fact line." in text
    assert "git revert abcd1234ef567890" in text
    assert "git revert 999988887777" in text


def test_second_run_on_a_day_appends(initialized: Config):
    """A second run on a day appends to the same report file with frontmatter preserved."""
    rep1 = _make_dummy_report(slug="widget", commit_sha="aaaa1111")
    time1 = datetime(2026, 10, 4, 10, 0, tzinfo=UTC)
    path1 = report.write_report(initialized, rep1, now=time1)

    assert path1.exists()
    content1 = path1.read_text(encoding="utf-8")
    assert content1.startswith('---\ntype: Dream Report\ntitle: "2026-10-04"\n---')
    assert "# widget 10:00:00Z" in content1

    # Second run later on the same day
    rep2 = _make_dummy_report(slug="gadget", commit_sha="bbbb2222")
    time2 = datetime(2026, 10, 4, 16, 30, tzinfo=UTC)
    path2 = report.write_report(initialized, rep2, now=time2)

    assert path1 == path2
    content2 = path2.read_text(encoding="utf-8")

    # Frontmatter is not duplicated
    assert content2.count("type: Dream Report") == 1
    # Both run sections are present
    assert "# widget 10:00:00Z" in content2
    assert "# gadget 16:30:00Z" in content2
    assert "git revert aaaa1111" in content2
    assert "git revert bbbb2222" in content2

    # Fixed headings appear twice (once per run)
    assert content2.count("## Summary") == 2
    assert content2.count("## Revert") == 2


def test_needs_you_lists_every_open_conflict_and_question_with_links(initialized: Config):
    """needs-you.md lists every open conflict and question in the vault with page and line links."""
    vault_path = initialized.vault

    # Create widget overview with an open conflict
    widget_file = vault_path / "projects" / "widget" / "overview.md"
    widget_file.parent.mkdir(parents=True, exist_ok=True)
    widget_file.write_text(
        "---\n"
        "type: Project\n"
        "title: Widget\n"
        "description: Widget\n"
        "sources: []\n"
        "---\n\n"
        "## State\n\n"
        "- Widget uses SQLite. [observed] 2026-09-20 [^s1]\n"
        "\n"
        "> [!conflict] 2026-10-01\n"
        "> Another session claims Postgres is used.\n"
        "\n"
        "- Another active line. [observed] 2026-09-21 [^s2]\n",
        encoding="utf-8",
    )

    # Create global preferences with an open question
    pref_file = vault_path / "global" / "preferences.md"
    pref_file.parent.mkdir(parents=True, exist_ok=True)
    pref_file.write_text(
        "---\n"
        "type: Preferences\n"
        "title: Preferences\n"
        "description: Preferences\n"
        "sources: []\n"
        "---\n\n"
        "## Preferences\n\n"
        "- No telemetry enabled. [stated] 2026-09-15 [^s1]\n"
        "\n"
        "> [!question] 2026-10-02\n"
        "> Observed opt-in telemetry prompt in tool.\n",
        encoding="utf-8",
    )

    needs_you_path = report.regenerate_needs_you(vault_path)
    assert needs_you_path.exists()
    ny_text = needs_you_path.read_text(encoding="utf-8")

    assert ny_text.startswith("---\ntype: Needs You\ntitle: Needs you")
    assert "## Conflicts" in ny_text
    assert "## Questions" in ny_text

    # Links to page and line numbers
    assert "[projects/widget/overview.md:10](../projects/widget/overview.md#L10)" in ny_text
    assert "Widget uses SQLite." in ny_text
    assert "Another session claims Postgres is used." in ny_text
    assert "[2026-10-01 report](2026/10/2026-10-01.md#conflicts)" in ny_text

    assert "[global/preferences.md:10](../global/preferences.md#L10)" in ny_text
    assert "No telemetry enabled." in ny_text
    assert "Observed opt-in telemetry prompt in tool." in ny_text
    assert "[2026-10-02 report](2026/10/2026-10-02.md#needs-you)" in ny_text

    # Resolve callouts and regenerate: returns to "Nothing is waiting on you."
    doc_widget = pagewrite.PageDoc.read(widget_file, vault=vault_path)
    fact_widget = doc_widget.fact("Widget uses SQLite.")
    doc_widget.resolve_callout(fact_widget)
    widget_file.write_text(doc_widget.render(), encoding="utf-8")

    doc_pref = pagewrite.PageDoc.read(pref_file, vault=vault_path)
    fact_pref = doc_pref.fact("No telemetry enabled.")
    doc_pref.resolve_callout(fact_pref)
    pref_file.write_text(doc_pref.render(), encoding="utf-8")

    report.regenerate_needs_you(vault_path)
    cleared_text = needs_you_path.read_text(encoding="utf-8")
    assert "Nothing is waiting on you." in cleared_text
    assert "## Conflicts" not in cleared_text
    assert "## Questions" not in cleared_text


def test_dream_inputs_never_include_report_path(initialized: Config):
    """The dream's inputs (decide.related, waiting_digests) never include a report path."""
    vault_path = initialized.vault

    # Plant a report file that contains fact-like keywords
    rep_file = vault_path / "reports" / "2026" / "10" / "2026-10-04.md"
    rep_file.parent.mkdir(parents=True, exist_ok=True)
    rep_file.write_text(
        "---\n"
        "type: Dream Report\n"
        "title: 2026-10-04\n"
        "---\n\n"
        "# widget 12:00:00Z\n\n"
        "## Decisions\n"
        "- Secret dream report knowledge. [stated] 2026-10-04 [^s1]\n",
        encoding="utf-8",
    )

    # 1. decide.related never retrieves from reports
    new_f = rules.NewFact(
        statement="Secret dream report knowledge.",
        kind="decision",
        source="stated",
        date=TODAY,
    )
    candidates = decide.related(vault_path, new_f, "widget")
    assert all("reports" not in cand.path for cand in candidates)

    # 2. waiting_digests never looks in reports
    waiting = schedule.waiting_digests(vault_path)
    for _slug, digest_paths in waiting.items():
        assert all("reports" not in str(p) for p in digest_paths)


def test_reports_are_indexed_only_in_reports_collection(initialized: Config):
    """Reports are indexed solely in reports collection and not in pages."""
    vault_path = initialized.vault

    rep_file = vault_path / "reports" / "2026" / "10" / "2026-10-04.md"
    rep_file.parent.mkdir(parents=True, exist_ok=True)
    rep_file.write_text(
        "---\n"
        "type: Dream Report\n"
        "title: 2026-10-04\n"
        "---\n\n"
        "# widget 14:00:00Z\n\n"
        "## Summary\n"
        "SpecialConsolidationToken recorded metrics.\n",
        encoding="utf-8",
    )

    # Index the vault
    index.update(initialized)

    # Default search (pages, digests, evidence, library) excludes reports
    default_results = search.search(initialized, "SpecialConsolidationToken")
    assert len(default_results) == 0

    # Explicit --reports search finds it in reports collection
    report_results = search.search(initialized, "SpecialConsolidationToken", reports=True)
    assert len(report_results) >= 1
    assert any("reports/2026/10/2026-10-04.md" in r.path for r in report_results)
    assert all(r.collection == "reports" for r in report_results)

    # Prove reports file key is only in index.REPORTS collection
    import sqlite3

    db_path = initialized.home / "index.sqlite"
    with sqlite3.connect(db_path) as conn:
        keys_pages = [k for (k,) in conn.execute("SELECT row_key FROM pages")]
        assert not any("reports/2026/10/2026-10-04.md" in k for k in keys_pages)

        keys_reports = [k for (k,) in conn.execute("SELECT row_key FROM reports")]
        assert any("reports/2026/10/2026-10-04.md" in k for k in keys_reports)


def test_dream_run_wires_report_and_needs_you(initialized: Config, monkeypatch):
    """schedule.run generates report and needs-you when changes occur, committing them."""
    cfg = initialized

    # Register project
    reg = Registry.load(cfg.registry_path)
    reg.add("widget", "Widget")
    reg.save()
    vault.commit(cfg.vault, ["registry.yaml"], "seed registry")

    # Seed an overview page with an open conflict
    overview = cfg.vault / "projects" / "widget" / "overview.md"
    overview.parent.mkdir(parents=True, exist_ok=True)
    overview.write_text(
        "---\n"
        "type: Project\n"
        "title: Widget\n"
        "description: Widget\n"
        "sources:\n"
        "  - id: s1\n"
        "    resource: aft://session/c/1#e1\n"
        "    quote: verifiable quote\n"
        "---\n\n"
        "## State\n\n"
        "- Widget statement. [observed] 2026-09-01 [^s1]\n"
        "\n"
        "> [!conflict] 2026-10-01\n"
        "> Conflicting session observation.\n",
        encoding="utf-8",
    )
    vault.commit(cfg.vault, ["projects/widget/overview.md"], "seed overview")

    # Fake process that returns a report with an added fact
    mock_report = apply.ProjectRunReport(
        project="widget",
        commit="5555aaaa6666bbbb",
        digests_consumed=["digests/widget/2026/10/d1.md"],
        added=[
            apply.AddedFact(
                statement="Added through dream run.",
                kind="decision",
                source="stated",
                date=TODAY,
                refs=("s1",),
                path="projects/widget/overview.md",
            )
        ],
    )

    # Seed a digest so schedule.run treats the project as ready
    digest = cfg.vault / "digests" / "widget" / "2026" / "10" / "d1.md"
    digest.parent.mkdir(parents=True, exist_ok=True)
    digest.write_text(
        "---\n"
        "type: Session Digest\n"
        "generated: {at: 2026-10-04T12:00:00Z}\n"
        "aft: {role: primary}\n"
        "---\n\n"
        "## Facts\n\n- Fact.\n",
        encoding="utf-8",
    )

    def fake_process(c, s, digs):
        return mock_report

    _before_now(cfg)

    res = schedule.run(cfg, process=fake_process, now=NOW)
    assert "widget" in res.reports

    # Report file was generated
    rep_path = cfg.vault / "reports" / "2026" / "10" / "2026-10-04.md"
    assert rep_path.exists()
    rep_content = rep_path.read_text(encoding="utf-8")
    assert "## Summary" in rep_content
    assert "git revert 5555aaaa6666bbbb" in rep_content

    # needs-you.md was generated
    ny_path = cfg.vault / "reports" / "needs-you.md"
    assert ny_path.exists()
    ny_content = ny_path.read_text(encoding="utf-8")
    assert "Conflicting session observation." in ny_content

    # Working tree is clean
    status = git(["status", "--porcelain=v1"], cwd=cfg.vault).stdout.strip()
    assert status == ""


def test_has_changes_detection():
    """has_changes returns False for an empty report and True when modifications occur."""
    empty = apply.ProjectRunReport(project="widget")
    assert not report.has_changes(empty)

    assert report.has_changes(
        apply.ProjectRunReport(
            project="widget",
            added=[
                apply.AddedFact(
                    statement="s",
                    kind="decision",
                    source="stated",
                    date=TODAY,
                    refs=("s1",),
                    path="p",
                )
            ],
        )
    )
    assert report.has_changes(
        apply.ProjectRunReport(
            project="widget",
            confirmed=[
                apply.ConfirmedFact(
                    statement="s",
                    source="s1",
                    date=TODAY,
                    refs=("s1",),
                    path="p",
                )
            ],
        )
    )
    assert report.has_changes(
        apply.ProjectRunReport(
            project="widget",
            superseded=[
                apply.SupersededFact(
                    old_statement="o",
                    new_statement="n",
                    source="stated",
                    date=TODAY,
                    refs=("s1",),
                    by="s1",
                    path="p",
                )
            ],
        )
    )
    assert report.has_changes(
        apply.ProjectRunReport(
            project="widget",
            promoted=[
                apply.PromotedFact(
                    statement="s",
                    kind="fact",
                    source="inferred",
                    date=TODAY,
                    refs=("s1",),
                    path="p",
                )
            ],
        )
    )
    assert report.has_changes(
        apply.ProjectRunReport(
            project="widget",
            candidates=[
                apply.CandidateFact(
                    statement="s",
                    tag="inferred",
                    date=TODAY,
                    refs=("s1",),
                    path="p",
                )
            ],
        )
    )
    assert report.has_changes(
        apply.ProjectRunReport(
            project="widget",
            conflicts=[
                apply.ConflictFact(
                    new_statement="n",
                    target_statement="t",
                    reason="r",
                    rule="R4",
                )
            ],
        )
    )
    assert report.has_changes(
        apply.ProjectRunReport(
            project="widget",
            questions=[
                apply.QuestionFact(
                    new_statement="n",
                    target_statement="t",
                    reason="r",
                    rule="R4",
                )
            ],
        )
    )
    assert report.has_changes(
        apply.ProjectRunReport(
            project="widget",
            broken_anchors=[
                maintain.BrokenAnchor(
                    page="p",
                    statement="s",
                    sid="s1",
                    reason="r",
                )
            ],
        )
    )
    assert report.has_changes(
        apply.ProjectRunReport(
            project="widget",
            cleared_anchors=[
                maintain.ClearedAnchor(
                    page="p",
                    statement="s",
                    sid="s1",
                )
            ],
        )
    )


def test_dream_run_with_no_changes_does_not_append_report(initialized: Config):
    """schedule.run does not append a report section when the run makes no changes."""
    cfg = initialized

    reg = Registry.load(cfg.registry_path)
    reg.add("widget", "Widget")
    reg.save()
    vault.commit(cfg.vault, ["registry.yaml"], "seed registry")

    digest = cfg.vault / "digests" / "widget" / "2026" / "10" / "d1.md"
    digest.parent.mkdir(parents=True, exist_ok=True)
    digest.write_text(
        "---\n"
        "type: Session Digest\n"
        "generated: {at: 2026-10-04T12:00:00Z}\n"
        "aft: {role: primary}\n"
        "---\n\n"
        "## Facts\n\n- Fact.\n",
        encoding="utf-8",
    )

    empty_report = apply.ProjectRunReport(project="widget")

    def fake_process_no_changes(c, s, digs):
        return empty_report

    _before_now(cfg)

    res = schedule.run(cfg, process=fake_process_no_changes, now=NOW)
    assert "widget" in res.reports

    rep_path = cfg.vault / "reports" / "2026" / "10" / "2026-10-04.md"
    assert not rep_path.exists()


def test_candidate_gate_progress_in_report(initialized: Config):
    """Section 8 of the report renders candidate gate progress from the page."""
    overview = initialized.vault / "projects" / "widget" / "overview.md"
    overview.parent.mkdir(parents=True, exist_ok=True)
    overview.write_text(
        "---\n"
        "type: Project\n"
        "title: Widget\n"
        "description: Widget project\n"
        "sources:\n"
        "  - id: s1\n"
        "    resource: aft://session/c/1#e1\n"
        "    quote: verifiable source quote\n"
        "  - id: s2\n"
        "    resource: aft://session/c/2#e1\n"
        "    quote: second session quote\n"
        "---\n\n"
        "## Candidates\n\n"
        "- Long sessions need checkpoint capture. [inferred] 2026-10-04 [^s1] [^s2] "
        "(signals 2/3, sessions 2/2)\n",
        encoding="utf-8",
    )

    rep = _make_dummy_report(
        slug="widget",
        candidates=[
            apply.CandidateFact(
                statement="Long sessions need checkpoint capture.",
                tag="inferred",
                date=TODAY,
                refs=("s1", "s2"),
                path="projects/widget/overview.md",
            )
        ],
    )

    path = report.write_report(initialized, rep, now=NOW)
    text = path.read_text(encoding="utf-8")
    assert (
        "Long sessions need checkpoint capture. [inferred] (2026-10-04) (signals 2/3, sessions 2/2)"
        in text
    )


def test_run_with_only_rejected_facts_creates_report_and_revert(initialized: Config):
    """A run that only rejects gets a report section because it made a commit."""
    cfg = initialized
    reg = Registry.load(cfg.registry_path)
    reg.add("widget", "Widget")
    reg.save()
    vault.commit(cfg.vault, ["registry.yaml"], "seed registry")

    digest = cfg.vault / "digests" / "widget" / "2026" / "10" / "d1.md"
    digest.parent.mkdir(parents=True, exist_ok=True)
    digest.write_text(
        "---\n"
        "type: Session Digest\n"
        "generated: {at: 2026-10-04T12:00:00Z}\n"
        "aft: {role: primary}\n"
        "---\n\n"
        "## Facts\n\n- Bad fact.\n",
        encoding="utf-8",
    )

    rejected_rep = apply.ProjectRunReport(
        project="widget",
        commit="deadbeef1234",
        digests_consumed=["digests/widget/2026/10/d1.md"],
        rejected=[
            apply.RejectedFact(
                statement="Bad fact.",
                decision_action="add",
                rule="R1",
                reason="bad quote rejected",
            )
        ],
    )
    assert not report.has_changes(rejected_rep)

    def fake_process_rejects(c, s, digs):
        return rejected_rep

    _before_now(cfg)

    res = schedule.run(cfg, process=fake_process_rejects, now=NOW)
    assert "widget" in res.reports

    rep_path = cfg.vault / "reports" / "2026" / "10" / "2026-10-04.md"
    assert rep_path.exists()
    content = rep_path.read_text(encoding="utf-8")
    assert "refused by R1 (bad quote rejected)" in content
    assert "git revert deadbeef1234" in content


def test_oversize_pages_are_printed_in_report(initialized: Config):
    """Pages exceeding their cap are flagged in the report."""
    rep = _make_dummy_report(
        slug="widget",
        oversize_pages=["projects/widget/overview.md"],
    )

    path = report.write_report(initialized, rep, now=NOW)
    content = path.read_text(encoding="utf-8")
    assert "projects/widget/overview.md" in content
    assert "Oversize pages" in content


def test_downgrades_and_skips_print_rule_and_reason(initialized: Config):
    """Every downgrade prints with its rule and reason, and every skip with its reason."""
    rep = _make_dummy_report(
        slug="widget",
        candidates=[
            apply.CandidateFact(
                statement="Tentative worker fact.",
                tag="inferred",
                date=TODAY,
                refs=("s1",),
                path="projects/widget/overview.md",
            )
        ],
        downgrades=[
            apply.DowngradeFact(
                statement="Tentative worker fact.",
                to_action="candidate",
                rule="R3",
                reason="needs 3 signals across 2 primary sessions",
            )
        ],
        skips=[
            apply.SkipFact(
                statement="Transient build log message.",
                reason="not durable, re-derivable from git",
            )
        ],
    )

    path = report.write_report(initialized, rep, now=NOW)
    content = path.read_text(encoding="utf-8")

    # Downgrade rule and reason are printed
    assert "Tentative worker fact." in content
    assert "Downgraded by R3: needs 3 signals across 2 primary sessions" in content

    # Skip statement and reason are printed
    assert "Transient build log message." in content
    assert "not durable, re-derivable from git" in content


def test_needs_you_regenerated_when_resolved_by_hand(initialized: Config):
    """needs-you.md is regenerated on every run so hand-resolved notes disappear."""
    cfg = initialized
    overview = cfg.vault / "projects" / "widget" / "overview.md"
    overview.parent.mkdir(parents=True, exist_ok=True)
    overview.write_text(
        "---\n"
        "type: Project\n"
        "title: Widget\n"
        "description: Widget\n"
        "sources:\n"
        "  - id: s1\n"
        "    resource: aft://session/c/1#e1\n"
        "    quote: verifiable quote\n"
        "---\n\n"
        "## Decisions\n\n"
        "- A question fact. [stated] 2026-10-04 [^s1]\n\n"
        "> [!question] 2026-10-04\n"
        "> Question to Ted\n",
        encoding="utf-8",
    )
    vault.commit(cfg.vault, ["projects/widget/overview.md"], "seed question")

    # Initial generation lists the question
    report.regenerate_needs_you(cfg.vault)
    ny_path = cfg.vault / "reports" / "needs-you.md"
    assert "Question to Ted" in ny_path.read_text(encoding="utf-8")

    # Hand-resolve the question
    overview.write_text(
        "---\n"
        "type: Project\n"
        "title: Widget\n"
        "description: Widget\n"
        "sources:\n"
        "  - id: s1\n"
        "    resource: aft://session/c/1#e1\n"
        "    quote: verifiable quote\n"
        "---\n\n"
        "## Decisions\n\n"
        "- A question fact. [stated] 2026-10-04 [^s1]\n",
        encoding="utf-8",
    )
    vault.commit(cfg.vault, ["projects/widget/overview.md"], "hand resolve question")

    # Seed a digest for schedule.run
    digest = cfg.vault / "digests" / "widget" / "2026" / "10" / "d1.md"
    digest.parent.mkdir(parents=True, exist_ok=True)
    digest.write_text(
        "---\n"
        "type: Session Digest\n"
        "generated: {at: 2026-10-04T12:00:00Z}\n"
        "aft: {role: primary}\n"
        "---\n\n"
        "## Facts\n\n- Digest fact.\n",
        encoding="utf-8",
    )

    # Process makes a dream commit with no fact changes (e.g. only logged/consumed)
    no_memory_changes_report = apply.ProjectRunReport(project="widget", commit="11223344")

    _before_now(cfg)

    schedule.run(cfg, process=lambda c, s, d: no_memory_changes_report, now=NOW)

    # needs-you.md must have regenerated and cleared the question
    cleared_text = ny_path.read_text(encoding="utf-8")
    assert "Question to Ted" not in cleared_text
    assert "Nothing is waiting on you." in cleared_text


def test_revert_section_includes_tag_commit_and_checkpoint_once_per_run(initialized: Config):
    """The Revert section includes the tag-hand-edits commit and names the
    checkpoint once per run.
    """
    cfg = initialized

    # Project 1 report with commit, tag_commit, and checkpoint
    rep1 = _make_dummy_report(slug="p1", commit_sha="11111111")
    path1 = report.write_report(
        cfg,
        rep1,
        now=NOW,
        checkpoint="cccc1111",
        tag_commit="tttt2222",
    )
    text1 = path1.read_text(encoding="utf-8")
    assert "git revert 11111111" in text1
    assert "Separate hand edits tag commit: git revert tttt2222" in text1
    assert "Separate checkpoint commit: git revert cccc1111" in text1

    # In a multi-project run, schedule passes checkpoint=None, tag_commit=None for project 2
    rep2 = _make_dummy_report(slug="p2", commit_sha="33333333")
    path2 = report.write_report(
        cfg,
        rep2,
        now=NOW,
        checkpoint=None,
        tag_commit=None,
    )
    text2 = path2.read_text(encoding="utf-8")
    p2_section = text2.split("# p2")[1]
    assert "git revert 33333333" in p2_section
    assert "Separate checkpoint commit" not in p2_section
    assert "Separate hand edits tag commit" not in p2_section


@pytest.fixture
def fake():
    endpoint = FakeEndpoint()
    endpoint.start()
    yield endpoint
    endpoint.stop()


def test_schedule_run_drives_real_apply_with_fake_endpoint(initialized, home, fake):
    """schedule.run drives real apply.run_project through to report generation."""
    from aft import config as configmod
    from aft.readers import USER, Entry

    (home / "config.toml").write_text(
        f'vault = "{initialized.vault}"\nhost = "testhost"\n'
        f'[model]\nendpoint = "{fake.endpoint}"\nname = "fake-model"\n'
    )
    cfg = configmod.load()

    registry = Registry.load(cfg.registry_path)
    proj = registry.add("widget", "Widget")
    registry.save()

    # Seed overview page
    overview = cfg.vault / "projects" / "widget" / "overview.md"
    overview.parent.mkdir(parents=True, exist_ok=True)
    overview.write_text(
        "---\n"
        "type: Project\n"
        "title: Widget\n"
        "description: Widget project\n"
        f"aft:\n  project: {proj.id}\n"
        "sources:\n"
        "  - id: s1\n"
        "    resource: aft://session/c/1#e1\n"
        "    quote: verifiable quote\n"
        "---\n\n"
        "## State\n\n"
        "- Initial fact. [observed] 2026-10-01 [^s1]\n",
        encoding="utf-8",
    )
    vault.commit(cfg.vault, ["projects/widget/overview.md"], "seed overview")

    # Write digest
    digest = cfg.vault / "digests" / "widget" / "2026" / "10" / "d1.md"
    digest.parent.mkdir(parents=True, exist_ok=True)
    digest.write_text(
        "---\n"
        "type: Session Digest\n"
        "title: Digest d1\n"
        "description: Summary of session.\n"
        "generated:\n"
        "  by: aft\n"
        "  at: 2026-10-05T08:00:00Z\n"
        "sources:\n"
        "  - id: s10\n"
        "    resource: aft://session/c/2#e5\n"
        "    quote: The queue uses SQLite WAL mode.\n"
        "aft:\n"
        f"  project: {proj.id}\n"
        "  role: primary\n"
        "  facts:\n"
        "    - statement: The queue uses SQLite WAL mode.\n"
        "      kind: decision\n"
        "      source: stated\n"
        "      date: '2026-10-05'\n"
        "      ref: s10\n"
        "---\n\n"
        "## Decisions\n\n"
        "- The queue uses SQLite WAL mode. [stated] 2026-10-05 [^s10]\n",
        encoding="utf-8",
    )

    # Store evidence in SQLite so R1 passes
    evidence.store(
        cfg,
        agent="c",
        session_id="2",
        project_id=proj.id,
        entries=[
            Entry(
                id="e5",
                time=NOW,
                role=USER,
                text="The queue uses SQLite WAL mode.",
                is_error=False,
            )
        ],
    )

    fake.script = [Reply(content={"action": "add", "line": None, "reason": "new decision"})]

    _before_now(cfg)

    res = schedule.run(cfg, now=NOW)
    assert "widget" in res.ran
    report_obj = res.reports["widget"]
    assert len(report_obj.added) == 1
    assert report_obj.commit is not None

    # Report file exists and contains the run section
    date_str = NOW.date().isoformat()
    rep_path = cfg.vault / "reports" / f"{NOW.year:04d}" / f"{NOW.month:02d}" / f"{date_str}.md"
    assert rep_path.exists()
    content = rep_path.read_text(encoding="utf-8")
    assert f"git revert {report_obj.commit}" in content
    assert "The queue uses SQLite WAL mode." in content
    assert "## Summary" in content

    # needs-you.md exists and is committed
    ny_path = cfg.vault / "reports" / "needs-you.md"
    assert ny_path.exists()

    status = git(["status", "--porcelain=v1"], cwd=cfg.vault).stdout.strip()
    assert status == ""


# The idle and gone report (DESIGN 7, F49)


def _named_project(cfg, tmp_path, slug: str, *, days: int, keep: bool = True) -> Path:
    """A project registered at a standing directory with one recorded session ``days``
    before NOW."""
    repo = make_repo(tmp_path / slug)
    registry = Registry.load(cfg.registry_path)
    project = registry.add(slug, slug.capitalize())
    registry.add_path(project, path_key("testhost", repo))
    registry.save()
    queue.record_project(cfg, "claude", f"s-{slug}", project.id, slug, repo)
    queue.enqueue(
        cfg,
        SessionEvent(
            agent="claude",
            kind=SESSION_END,
            session_id=f"s-{slug}",
            cwd=repo,
            transcript_path=tmp_path / "t.jsonl",
            time=NOW - timedelta(days=days),
            detail=None,
        ),
    )
    if not keep:
        shutil.rmtree(repo)
    return repo


def test_idle_and_gone_names_a_project_with_no_recent_session(initialized, tmp_path):
    """A project whose newest session is older than 90 days is named with the date of its
    last one, and a project with a session this week says nothing (DESIGN 7, F49)."""
    _named_project(initialized, tmp_path, "idle-gizmo", days=100)
    _named_project(initialized, tmp_path, "widget", days=1)
    assert report.idle_and_gone(initialized, now=NOW) == ["idle-gizmo: no session since 2026-06-26"]


def test_a_project_the_queue_has_never_heard_of_is_not_named(initialized, tmp_path):
    """The report dates what it names (DESIGN 7, F49): a project no session in the queue
    has ever touched has nothing to date its line with, so a quiet dream says nothing."""
    repo = make_repo(tmp_path / "widget")
    registry = Registry.load(initialized.registry_path)
    project = registry.add("widget", "Widget")
    registry.add_path(project, path_key("testhost", repo))
    registry.save()
    assert report.idle_and_gone(initialized, now=NOW) == []


def test_idle_and_gone_names_a_project_whose_paths_are_all_gone(initialized, tmp_path):
    """A project every recorded directory of has left the machine is named by its slug;
    with one directory standing it is not (DESIGN 7, F49)."""
    _named_project(initialized, tmp_path, "widget", days=1, keep=False)
    assert report.idle_and_gone(initialized, now=NOW) == ["widget: recorded paths are all gone"]


def test_an_archived_project_is_not_named_by_the_idle_report(initialized, tmp_path):
    """The registry's own archived flag keeps a project out of the report (DESIGN 7,
    F49): archiving is the person's answer to it."""
    repo = _named_project(initialized, tmp_path, "idle-gizmo", days=100)
    registry = Registry.load(initialized.registry_path)
    registry.set_archived("idle-gizmo", TODAY)
    registry.save()
    assert report.idle_and_gone(initialized, now=NOW) == []
    shutil.rmtree(repo)


def test_write_idle_gone_appends_to_the_days_report_and_commits(initialized, tmp_path):
    """The section is written to the day's report page and committed once; a run that has
    nothing to say writes nothing (DESIGN 7, F49)."""
    _named_project(initialized, tmp_path, "idle-gizmo", days=100)
    path = report.write_idle_gone(initialized, now=NOW)
    assert path is not None and path.exists()
    text = path.read_text()
    assert text.endswith("\n\n## Idle and gone\n\n- idle-gizmo: no session since 2026-06-26\n")
    assert run_git(["log", "--format=%s"], initialized.vault).splitlines()[0] == (
        "aft: report idle and gone"
    )

    assert report.write_idle_gone(initialized, now=NOW) is None
    assert run_git(["log", "--format=%s"], initialized.vault).splitlines()[0] == (
        "aft: report idle and gone"
    )


def test_write_idle_gone_appends_below_a_runs_sections(initialized, tmp_path):
    """A report the dream's runs already wrote keeps them: the section joins at the end
    (DESIGN 7, F49)."""
    _named_project(initialized, tmp_path, "idle-gizmo", days=100)
    run_path = report.write_report(
        initialized, _make_dummy_report(), now=NOW, checkpoint="cccc1111"
    )
    path = report.write_idle_gone(initialized, now=NOW)
    assert path == run_path
    text = path.read_text()
    assert "# widget 14:30:00Z" in text and "## Idle and gone" in text
    assert text.index("## Idle and gone") > text.index("# widget 14:30:00Z")
