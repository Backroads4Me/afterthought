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

from datetime import UTC, datetime

from aft import index, pagewrite, search, vault
from aft.config import Config
from aft.dream import apply, decide, maintain, report, rules, schedule
from aft.gitutil import git
from aft.registry import Registry

NOW = datetime(2026, 10, 4, 14, 30, tzinfo=UTC)
TODAY = "2026-10-04"


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
    broken_anchors: list[maintain.BrokenAnchor] | None = None,
    cleared_anchors: list[maintain.ClearedAnchor] | None = None,
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
        broken_anchors=broken_anchors or [],
        cleared_anchors=cleared_anchors or [],
    )


def test_section_order_is_fixed(initialized: Config):
    """The 9 sections appear in the exact fixed order specified by DESIGN 9."""
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
    assert content1.startswith("---\ntype: Dream Report\ntitle: 2026-10-04\n---")
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
        "## Added and Confirmed\n"
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
