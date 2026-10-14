"""Tests for dream maintenance (DESIGN 9 step 6, BUILD-PLAN M4.5)."""

from __future__ import annotations

from datetime import UTC, datetime

from aft import page, vault
from aft.dream import maintain
from aft.registry import Registry, path_key
from conftest import make_repo, run_git

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
TODAY = "2026-10-05"
OLD_DATE = "2026-06-01"  # > 90 days before TODAY
RECENT_DATE = "2026-09-25"  # < 90 days before TODAY


def test_anchor_commit_not_on_default_branch_skipped(initialized, tmp_path):
    repo = make_repo(tmp_path / "widget")
    # Commit a file on a feature branch
    run_git(["checkout", "-b", "feature"], repo)
    (repo / "feature.py").write_text("def feature(): return 1\n")
    run_git(["add", "feature.py"], repo)
    run_git(["commit", "-m", "feature commit"], repo)
    feat_sha = run_git(["rev-parse", "HEAD"], repo)
    run_git(["checkout", "main"], repo)

    registry = Registry.load(initialized.registry_path)
    proj = registry.add("widget", "Widget")
    registry.add_path(proj, path_key(initialized.host, repo))
    registry.save()

    root = initialized.vault
    overview = root / "projects" / "widget" / "overview.md"
    overview.parent.mkdir(parents=True, exist_ok=True)
    overview.write_text(f"""---
type: Project
title: widget
sources:
  - id: s1
    resource: aft://session/claude/123#e1
    quote: "feature returns 1"
aft:
  project: {proj.id}
  anchors:
    s1: {{path: feature.py, all_of: ["returns 1"], commit: {feat_sha}}}
---

## Facts

- Feature returns 1. [observed] 2026-10-01 [^s1]
""")
    vault.commit(root, ["projects"], "seed")

    broken, cleared = maintain.check_anchors(root, proj, TODAY, initialized.host)
    assert broken == []
    assert cleared == []
    p = page.parse(overview.read_text(), overview.relative_to(root))
    assert p.facts[0].state == page.ACTIVE


def test_anchor_broken_marks_active_line_stale(initialized, tmp_path):
    repo = make_repo(tmp_path / "widget")
    (repo / "index.py").write_text("def search(): pass\n")
    run_git(["add", "index.py"], repo)
    run_git(["commit", "-m", "add index"], repo)
    commit_sha = run_git(["rev-parse", "HEAD"], repo)

    registry = Registry.load(initialized.registry_path)
    proj = registry.add("widget", "Widget")
    registry.add_path(proj, path_key(initialized.host, repo))
    registry.save()

    root = initialized.vault
    overview = root / "projects" / "widget" / "overview.md"
    overview.parent.mkdir(parents=True, exist_ok=True)
    overview.write_text(f"""---
type: Project
title: widget
sources:
  - id: s1
    resource: aft://session/claude/123#e1
    quote: "we use sqlite"
aft:
  project: {proj.id}
  anchors:
    s1: {{path: index.py, all_of: ["sqlite3"], commit: {commit_sha}}}
---

## Facts

- The index uses SQLite. [observed] 2026-09-30 [^s1]
""")
    vault.commit(root, ["projects"], "seed")

    broken, cleared = maintain.check_anchors(root, proj, TODAY, initialized.host)
    assert len(broken) == 1
    assert broken[0].reported_only is False
    assert broken[0].statement == "The index uses SQLite."
    assert cleared == []

    p = page.parse(overview.read_text(), overview.relative_to(root))
    assert p.facts[0].state == page.STALE
    assert f"(stale {TODAY}: anchor words gone from index.py)" in overview.read_text()


def test_anchor_broken_on_line_with_open_note_reported_only(initialized, tmp_path):
    repo = make_repo(tmp_path / "widget")
    (repo / "index.py").write_text("def search(): pass\n")
    run_git(["add", "index.py"], repo)
    run_git(["commit", "-m", "add index"], repo)
    commit_sha = run_git(["rev-parse", "HEAD"], repo)

    registry = Registry.load(initialized.registry_path)
    proj = registry.add("widget", "Widget")
    registry.add_path(proj, path_key(initialized.host, repo))
    registry.save()

    root = initialized.vault
    overview = root / "projects" / "widget" / "overview.md"
    overview.parent.mkdir(parents=True, exist_ok=True)
    overview.write_text(f"""---
type: Project
title: widget
sources:
  - id: s1
    resource: aft://session/claude/123#e1
    quote: "we use sqlite"
aft:
  project: {proj.id}
  anchors:
    s1: {{path: index.py, all_of: ["sqlite3"], commit: {commit_sha}}}
---

## Facts

- The index uses SQLite. [observed] 2026-09-30 [^s1]

> [!conflict] 2026-10-01
> Disputed implementation.
""")
    vault.commit(root, ["projects"], "seed")

    broken, cleared = maintain.check_anchors(root, proj, TODAY, initialized.host)
    assert len(broken) == 1
    assert broken[0].reported_only is True
    assert broken[0].statement == "The index uses SQLite."
    assert cleared == []

    p = page.parse(overview.read_text(), overview.relative_to(root))
    assert p.facts[0].state == page.CONFLICT  # Not changed to STALE
    assert "(stale" not in overview.read_text()


def test_anchor_reappearing_clears_stale_mark(initialized, tmp_path):
    repo = make_repo(tmp_path / "widget")
    (repo / "index.py").write_text("import sqlite3\ndef search(): pass\n")
    run_git(["add", "index.py"], repo)
    run_git(["commit", "-m", "add sqlite"], repo)
    commit_sha = run_git(["rev-parse", "HEAD"], repo)

    registry = Registry.load(initialized.registry_path)
    proj = registry.add("widget", "Widget")
    registry.add_path(proj, path_key(initialized.host, repo))
    registry.save()

    root = initialized.vault
    overview = root / "projects" / "widget" / "overview.md"
    overview.parent.mkdir(parents=True, exist_ok=True)
    overview.write_text(f"""---
type: Project
title: widget
sources:
  - id: s1
    resource: aft://session/claude/123#e1
    quote: "we use sqlite"
aft:
  project: {proj.id}
  anchors:
    s1: {{path: index.py, all_of: ["sqlite3"], commit: {commit_sha}}}
---

## Facts

- The index uses SQLite. [observed] 2026-09-20 [^s1] (stale 2026-09-25: words gone)
""")
    vault.commit(root, ["projects"], "seed")

    broken, cleared = maintain.check_anchors(root, proj, TODAY, initialized.host)
    assert broken == []
    assert len(cleared) == 1
    assert cleared[0].statement == "The index uses SQLite."

    p = page.parse(overview.read_text(), overview.relative_to(root))
    assert p.facts[0].state == page.ACTIVE
    assert p.facts[0].date == "2026-09-20"  # Original date kept
    assert "(stale" not in overview.read_text()


def test_archive_superseded_older_than_90_days(initialized):
    root = initialized.vault
    overview = root / "projects" / "widget" / "overview.md"
    overview.parent.mkdir(parents=True, exist_ok=True)
    overview.write_text("""---
type: Project
title: widget
sources:
  - id: s1
    resource: aft://session/claude/1#e1
    title: Source 1
    quote: "old quote"
  - id: s2
    resource: aft://session/claude/2#e2
    title: Source 2
    quote: "new quote"
aft:
  project: p_widget
---

## Decisions

- The index uses SQLite. [stated] 2026-09-20 [^s2]
- ~~The index uses JSON.~~ [stated] 2026-01-01 [^s1] superseded 2026-06-01 by [^s2]
- ~~Recent superseded.~~ [stated] 2026-09-15 [^s1] superseded 2026-09-25 by [^s2]
""")
    vault.commit(root, ["projects"], "seed")

    archived = maintain.archive_superseded(root, "widget", TODAY)
    assert len(archived) == 1
    assert archived[0].statement == "The index uses JSON."
    assert archived[0].archive_page == "archive/projects/widget/overview.md"

    # Original page no longer has the old superseded line, but keeps the recent one
    text = overview.read_text()
    assert "The index uses JSON." not in text
    assert "Recent superseded." in text
    assert "The index uses SQLite." in text

    # Archive page exists and contains the line
    archive_file = root / "archive" / "projects" / "widget" / "overview.md"
    assert archive_file.exists()
    archive_text = archive_file.read_text()
    assert "The index uses JSON." in archive_text
    p = page.parse(archive_text, archive_file.relative_to(root))
    assert len(p.facts) == 1
    assert p.facts[0].statement == "The index uses JSON."
    assert p.facts[0].state == page.SUPERSEDED


def test_check_oversize_pages(initialized):
    root = initialized.vault
    small_page = root / "projects" / "widget" / "small.md"
    small_page.parent.mkdir(parents=True, exist_ok=True)
    small_page.write_text("# Small\n\nShort page.\n")

    big_page = root / "projects" / "widget" / "big.md"
    big_page.write_text("# Big\n\n" + ("a" * 12_500) + "\n")

    oversize = maintain.check_oversize(root, [small_page, big_page])
    assert oversize == ["projects/widget/big.md"]


def test_regenerate_index_and_ensure_root(initialized):
    root = initialized.vault
    proj_dir = root / "projects" / "widget"
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "overview.md").write_text("# Overview\n")
    (proj_dir / "api.md").write_text("# API\n")
    (proj_dir / "zebra.md").write_text("# Zebra\n")

    maintain.regenerate_index(root, "widget", "Widget Project")
    index_file = proj_dir / "index.md"
    assert index_file.exists()
    content = index_file.read_text()
    assert content.splitlines() == [
        "# Widget Project",
        "",
        "- overview.md",
        "- api.md",
        "- zebra.md",
    ]
    # No frontmatter
    fm, _, _, _ = page.split_frontmatter(content)
    assert fm is None

    maintain.ensure_root_index(root)
    root_index = root / "index.md"
    assert root_index.exists()
    rfm, _, _, _ = page.split_frontmatter(root_index.read_text())
    assert rfm == {"okf_version": "0.2"}


def test_append_log(initialized):
    root = initialized.vault
    maintain.append_log(root, "widget", "first run")
    maintain.append_log(root, "widget", "second run")
    log_text = (root / "log.md").read_text()
    assert "- first run\n" in log_text
    assert "- second run\n" in log_text


def test_anchor_check_uses_python_in_without_subprocess(initialized, tmp_path):
    """L9: anchor checking uses Python in directly on content, without rg or subprocess."""
    assert not hasattr(maintain, "subprocess")
    assert not hasattr(maintain, "shutil")

    repo = make_repo(tmp_path / "widget")
    (repo / "index.py").write_text("def search(): pass\n")
    run_git(["add", "index.py"], repo)
    run_git(["commit", "-m", "add index"], repo)
    commit_sha = run_git(["rev-parse", "HEAD"], repo)

    registry = Registry.load(initialized.registry_path)
    proj = registry.add("widget", "Widget")
    registry.add_path(proj, path_key(initialized.host, repo))
    registry.save()

    root = initialized.vault
    overview = root / "projects" / "widget" / "overview.md"
    overview.parent.mkdir(parents=True, exist_ok=True)
    overview.write_text(f"""---
type: Project
title: widget
sources:
  - id: s1
    resource: aft://session/claude/123#e1
    quote: "we use sqlite"
aft:
  project: {proj.id}
  anchors:
    s1: {{path: index.py, all_of: ["sqlite3"], commit: {commit_sha}}}
---

## Facts

- The index uses SQLite. [observed] 2026-09-30 [^s1]
""")
    vault.commit(root, ["projects"], "seed")

    broken, cleared = maintain.check_anchors(root, proj, TODAY, initialized.host)
    assert len(broken) == 1
    assert broken[0].statement == "The index uses SQLite."


def test_anchor_check_merge_base_passes_end_of_options(initialized, tmp_path, monkeypatch):
    """L9: git merge-base call in check_anchors passes --end-of-options before revisions."""
    recorded_commands = []
    real_git = maintain.git

    def tracking_git(args, **kwargs):
        recorded_commands.append(list(args))
        return real_git(args, **kwargs)

    monkeypatch.setattr(maintain, "git", tracking_git)
    repo = make_repo(tmp_path / "widget")
    (repo / "index.py").write_text("import sqlite3\n")
    run_git(["add", "index.py"], repo)
    run_git(["commit", "-m", "add index"], repo)
    commit_sha = run_git(["rev-parse", "HEAD"], repo)

    registry = Registry.load(initialized.registry_path)
    proj = registry.add("widget", "Widget")
    registry.add_path(proj, path_key(initialized.host, repo))
    registry.save()

    root = initialized.vault
    overview = root / "projects" / "widget" / "overview.md"
    overview.parent.mkdir(parents=True, exist_ok=True)
    overview.write_text(f"""---
type: Project
title: widget
sources:
  - id: s1
    resource: aft://session/claude/123#e1
    quote: "we use sqlite"
aft:
  project: {proj.id}
  anchors:
    s1: {{path: index.py, all_of: ["sqlite3"], commit: {commit_sha}}}
---

## Facts

- The index uses SQLite. [observed] 2026-09-30 [^s1]
""")
    vault.commit(root, ["projects"], "seed")

    maintain.check_anchors(root, proj, TODAY, initialized.host)
    merge_bases = [cmd for cmd in recorded_commands if len(cmd) > 1 and cmd[0] == "merge-base"]
    assert len(merge_bases) >= 1
    assert merge_bases[0] == [
        "merge-base",
        "--is-ancestor",
        "--end-of-options",
        commit_sha,
        "main",
    ]


def test_fact_with_two_anchors_remains_stale_when_one_anchor_is_broken(initialized, tmp_path):
    repo = make_repo(tmp_path / "widget")
    (repo / "index.py").write_text("def search(): pass\n")
    (repo / "db.py").write_text("postgres = True\n")
    run_git(["add", "index.py", "db.py"], repo)
    run_git(["commit", "-m", "initial files"], repo)
    commit_sha = run_git(["rev-parse", "HEAD"], repo)

    registry = Registry.load(initialized.registry_path)
    proj = registry.add("widget", "Widget")
    registry.add_path(proj, path_key(initialized.host, repo))
    registry.save()

    root = initialized.vault
    overview = root / "projects" / "widget" / "overview.md"
    overview.parent.mkdir(parents=True, exist_ok=True)
    overview.write_text(f"""---
type: Project
title: widget
sources:
  - id: s1
    resource: aft://session/claude/123#e1
    quote: "we use sqlite"
  - id: s2
    resource: aft://session/claude/123#e2
    quote: "we use postgres"
aft:
  project: {proj.id}
  anchors:
    s1: {{path: index.py, all_of: ["sqlite3"], commit: {commit_sha}}}
    s2: {{path: db.py, all_of: ["postgres"], commit: {commit_sha}}}
---

## Facts

- Dual anchor fact. [observed] 2026-09-30 [^s1] [^s2]
""")
    vault.commit(root, ["projects"], "seed")

    # s1 is broken (sqlite3 missing from index.py), s2 is intact (postgres present in db.py)
    broken, cleared = maintain.check_anchors(root, proj, TODAY, initialized.host)
    assert len(broken) == 1
    assert broken[0].sid == "s1"
    assert cleared == []

    p = page.parse(overview.read_text(), overview.relative_to(root))
    assert p.facts[0].state == page.STALE

    # Second check: fact must remain stale and must not be cleared by the intact s2 anchor
    broken2, cleared2 = maintain.check_anchors(root, proj, TODAY, initialized.host)
    assert len(broken2) == 1
    assert broken2[0].sid == "s1"
    assert cleared2 == []
    p2 = page.parse(overview.read_text(), overview.relative_to(root))
    assert p2.facts[0].state == page.STALE

    # When s1 words reappear, both anchors are intact, so the stale mark clears
    (repo / "index.py").write_text("import sqlite3\ndef search(): pass\n")
    run_git(["add", "index.py"], repo)
    run_git(["commit", "-m", "restore sqlite"], repo)

    broken3, cleared3 = maintain.check_anchors(root, proj, TODAY, initialized.host)
    assert broken3 == []
    assert len(cleared3) == 2
    assert {c.sid for c in cleared3} == {"s1", "s2"}
    p3 = page.parse(overview.read_text(), overview.relative_to(root))
    assert p3.facts[0].state == page.ACTIVE


def test_anchor_check_handles_word_starting_with_dash(initialized, tmp_path):
    """An anchor word starting with - is passed with -- to rg and does not exit 2."""
    repo = make_repo(tmp_path / "widget")
    (repo / "cli.py").write_text("def run(options): return '--dry-run' in options\n")
    run_git(["add", "cli.py"], repo)
    run_git(["commit", "-m", "add dry-run option"], repo)
    commit_sha = run_git(["rev-parse", "HEAD"], repo)

    registry = Registry.load(initialized.registry_path)
    proj = registry.add("widget", "Widget")
    registry.add_path(proj, path_key(initialized.host, repo))
    registry.save()

    root = initialized.vault
    overview = root / "projects" / "widget" / "overview.md"
    overview.parent.mkdir(parents=True, exist_ok=True)
    overview.write_text(f"""---
type: Project
title: widget
sources:
  - id: s1
    resource: aft://session/claude/123#e1
    quote: "dry run mode"
aft:
  project: {proj.id}
  anchors:
    s1: {{path: cli.py, all_of: ["--dry-run"], commit: {commit_sha}}}
---

## Facts

- CLI supports dry run mode. [observed] 2026-09-30 [^s1]
""")
    vault.commit(root, ["projects"], "seed")

    broken, cleared = maintain.check_anchors(root, proj, TODAY, initialized.host)
    assert broken == []
    assert cleared == []
    p = page.parse(overview.read_text(), overview.relative_to(root))
    assert p.facts[0].state == page.ACTIVE


def test_archived_superseded_line_remaps_footnotes(initialized):
    """Archived superseded lines remap footnote ids to the archive page sources."""
    root = initialized.vault
    overview = root / "projects" / "widget" / "overview.md"
    overview.parent.mkdir(parents=True, exist_ok=True)

    overview.write_text("""---
type: Project
title: widget
sources:
  - id: s1
    resource: aft://session/c/1#e1
    quote: "active quote 1"
  - id: s2
    resource: aft://session/c/1#e2
    quote: "active quote 2"
  - id: s3
    resource: aft://session/c/2#e1
    quote: "superseded line quote"
  - id: s4
    resource: aft://session/c/2#e2
    quote: "other quote"
  - id: s5
    resource: aft://session/c/3#e1
    quote: "replacement line quote"
aft:
  project: p_widget
---

## Decisions

- Active fact. [stated] 2026-09-01 [^s1]
- ~~Expired decision.~~ [stated] 2026-05-01 [^s3] superseded 2026-05-02 by [^s5]
""")
    vault.commit(root, ["projects"], "seed")

    archived = maintain.archive_superseded(root, "widget", TODAY)
    assert len(archived) == 1
    assert archived[0].statement == "Expired decision."

    # Verify original page no longer has the superseded line
    doc_orig = page.parse(overview.read_text(), overview.relative_to(root))
    assert len(doc_orig.facts) == 1
    assert doc_orig.facts[0].statement == "Active fact."

    # Verify archive page
    archive_file = root / "archive" / "projects" / "widget" / "overview.md"
    assert archive_file.exists()
    doc_archive = page.parse(archive_file.read_text(), archive_file.relative_to(root))
    assert len(doc_archive.errors) == 0
    assert len(doc_archive.facts) == 1

    archived_fact = doc_archive.facts[0]
    assert archived_fact.statement == "Expired decision."
    assert archived_fact.state == page.SUPERSEDED
    assert archived_fact.superseded_by is not None

    # Both footnotes on the archived line must resolve to sources in the archive page
    source_list = (doc_archive.frontmatter or {}).get("sources") or []
    source_map = {str(s["id"]): s for s in source_list}
    for ref in archived_fact.refs:
        assert ref in source_map
        assert source_map[ref]["quote"] == "superseded line quote"

    by_ref = archived_fact.superseded_by
    assert by_ref in source_map
    assert source_map[by_ref]["quote"] == "replacement line quote"
