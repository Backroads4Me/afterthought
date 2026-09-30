"""Tests for search, read and trace (BUILD-PLAN M3.2, DESIGN 10)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from aft import evidence, index, search
from aft.cli import main
from aft.config import Config
from aft.readers import Entry
from aft.registry import Project, Registry

WIDGET_OVERVIEW = """---
type: Project
title: Widget
description: Everything known about the widget.
sources:
  - id: s1
    resource: aft://session/claude/s-widget-1#e-1
    title: Design session
    quote: "we will use plain SQLite storage"
  - id: s2
    resource: aft://session/pi/s-widget-2#e-99
    quote: "missing evidence quote"
aft:
  project: p_widget
---

# Widget

## Decisions

- The index is plain SQLite storage. [stated] 2026-09-20 [^s1]

## Facts

- Steel components come from Ohio manufacturing. [observed] 2026-09-21 [^s1]
"""

WIDGET_ASSEMBLY = """---
type: Project
title: Widget Assembly
description: Procedures for assembling the widget.
sources: []
aft:
  project: p_widget
---

# Widget Assembly

## State

- Fastening requires titanium bolt tightening to 15 Nm. [observed] 2026-09-22 [^s1]

## Procedures

- Ensure wire harness routing clears internal brackets. [observed] 2026-09-23 [^s1]
"""

WIDGET_FLANGE = """---
type: Project
title: Widget Flange
description: Technical specs for the custom flange.
sources: []
aft:
  project: p_widget
---

# Widget Flange

## State

- The flange tolerance is within 50 microns. [observed] 2026-09-22 [^s1]

## Facts

- Flanges are cut on a 5-axis milling machine. [observed] 2026-09-23 [^s1]
"""

GADGET_OVERVIEW = """---
type: Project
title: Gadget
description: Architecture of the handheld gadget.
sources:
  - id: s1
    resource: aft://session/claude/s-gadget-1#e-1
    quote: "firmware uses Rust embedded"
aft:
  project: p_gadget
---

# Gadget

## Decisions

- Built with Rust embedded firmware for reliability. [stated] 2026-09-20 [^s1]

## State

- Powered by a 4500 mAh battery cell. [observed] 2026-09-21 [^s1]
"""

GADGET_DISPLAY = """---
type: Project
title: Gadget Display
description: OLED screen interface and driver.
sources: []
aft:
  project: p_gadget
---

# Gadget Display

## State

- Driven by a monochrome OLED screen controller. [observed] 2026-09-22 [^s1]

## Decisions

- The display uses a 400 kHz bus clock. [stated] 2026-09-23 [^s1]
"""

GADGET_SENSOR = """---
type: Project
title: Gadget Sensor
description: Optical touch and motion sensor.
sources: []
aft:
  project: p_gadget
---

# Gadget Sensor

## State

- Requires optical touch calibration at startup. [observed] 2026-09-22 [^s1]

## Facts

- The photodiode sensitivity is adjustable via register. [observed] 2026-09-23 [^s1]
"""

GLOBAL_PREFERENCES = """---
type: Preferences
title: Preferences
description: Global developer preferences.
sources: []
---

# Preferences

## Preferences

- Always select dark theme syntax in editors. [stated] 2026-09-15 [^s1]
- Prefer the pytest test runner over unittest. [stated] 2026-09-16 [^s1]
"""

GLOBAL_ENVIRONMENT = """---
type: Environment
title: Environment
description: Developer machine configuration.
sources: []
---

# Environment

## State

- Primary desktop runs an Arch Linux workstation. [observed] 2026-09-17 [^s1]
- Local model runs on a vLLM inference server. [observed] 2026-09-18 [^s1]
"""

WIDGET_ARCHIVE = """---
title: Archived widget facts
description: Superseded lines from earlier designs.
sources: []
---

## Decisions

- ~~Ship the bronze casting prototype.~~ [stated] 2026-01-01 [^s1] superseded 2026-02-01 by [^s1]
"""

GADGET_ARCHIVE = """---
title: Archived gadget facts
description: Superseded lines from earlier gadget designs.
sources: []
---

## Decisions

- ~~Original unit had Bluetooth 4.0.~~ [stated] 2026-01-01 [^s1] superseded 2026-02-01 by [^s1]
"""

WIDGET_DIGEST_1 = """---
type: Session Digest
title: Widget session 1
description: Summary of the widget prototype digest.
aft:
  project: p_widget
---

# Widget session 1

## Summary

Initial discussion of the widget prototype digest.

## Handoff

In flight: flange tolerance tests.
"""

GADGET_DIGEST_1 = """---
type: Session Digest
title: Gadget session 1
description: Summary of gadget SPI debugging.
aft:
  project: p_gadget
---

# Gadget session 1

## Summary

Investigated the SPI baudrate fix on the prototype board.

## Handoff

Open: finish driver implementation.
"""

REPORT_1 = """---
type: Dream Report
title: Dream report
description: Dream report for September 28.
---

# Dream report 2026-09-28

## Summary

September consolidation metrics recorded 5 decisions.
"""


@pytest.fixture
def populated_vault(initialized: Config) -> Config:
    """Populate a temporary vault and evidence archive with test fixtures."""
    reg = Registry(
        initialized.registry_path,
        [
            Project(
                id="p_widget",
                slug="widget",
                title="Widget",
                paths=[f"testhost:{initialized.vault / 'projects' / 'widget'}"],
            ),
            Project(
                id="p_gadget",
                slug="gadget",
                title="Gadget",
                paths=[f"testhost:{initialized.vault / 'projects' / 'gadget'}"],
            ),
        ],
    )
    reg.save()

    files = {
        "projects/widget/overview.md": WIDGET_OVERVIEW,
        "projects/widget/assembly.md": WIDGET_ASSEMBLY,
        "projects/widget/flange.md": WIDGET_FLANGE,
        "projects/gadget/overview.md": GADGET_OVERVIEW,
        "projects/gadget/display.md": GADGET_DISPLAY,
        "projects/gadget/sensor.md": GADGET_SENSOR,
        "global/preferences.md": GLOBAL_PREFERENCES,
        "global/environment.md": GLOBAL_ENVIRONMENT,
        "archive/projects/widget/overview.md": WIDGET_ARCHIVE,
        "archive/projects/gadget/overview.md": GADGET_ARCHIVE,
        "digests/widget/2026/09/2026-09-28-claude-s-widget-1.md": WIDGET_DIGEST_1,
        "digests/gadget/2026/09/2026-09-28-pi-s-gadget-1.md": GADGET_DIGEST_1,
        "reports/2026/09/2026-09-28.md": REPORT_1,
    }

    for rel_path, content in files.items():
        full_path = initialized.vault / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")

    now = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    entries_widget = [
        Entry("e-0", now, "user", "start planning the storage layer", False),
        Entry("e-1", now, "user", "we will use plain SQLite storage", False),
        Entry("e-2", now, "assistant", "plain SQLite storage is configured", False),
        Entry("e-3", now, "user", "wrap up the session", False),
    ]
    evidence.store(initialized, "claude", "s-widget-1", "p_widget", entries_widget)

    entries_gadget = [
        Entry("e-0", now, "user", "debugging SPI bus", False),
        Entry("e-1", now, "user", "firmware uses Rust embedded", False),
        Entry("e-2", now, "assistant", "SPI bus verified", False),
    ]
    evidence.store(initialized, "claude", "s-gadget-1", "p_gadget", entries_gadget)

    index.reindex(initialized)
    return initialized


KNOWN_ANSWER_SET = [
    ("titanium bolt", "projects/widget/assembly.md", {}),
    ("wire harness routing", "projects/widget/assembly.md", {}),
    ("flange tolerance", "projects/widget/flange.md", {}),
    ("milling machine", "projects/widget/flange.md", {}),
    ("plain SQLite storage", "projects/widget/overview.md", {}),
    ("Ohio manufacturing", "projects/widget/overview.md", {}),
    ("Rust embedded firmware", "projects/gadget/overview.md", {}),
    ("4500 mAh battery", "projects/gadget/overview.md", {}),
    ("monochrome OLED screen", "projects/gadget/display.md", {}),
    ("400 kHz bus clock", "projects/gadget/display.md", {}),
    ("optical touch calibration", "projects/gadget/sensor.md", {}),
    ("photodiode sensitivity", "projects/gadget/sensor.md", {}),
    ("dark theme syntax", "global/preferences.md", {}),
    ("pytest test runner", "global/preferences.md", {}),
    ("Arch Linux workstation", "global/environment.md", {}),
    ("vLLM inference server", "global/environment.md", {}),
    ("widget prototype digest", "digests/widget/2026/09/2026-09-28-claude-s-widget-1.md", {}),
    ("SPI baudrate fix", "digests/gadget/2026/09/2026-09-28-pi-s-gadget-1.md", {}),
    ("bronze casting prototype", "archive/projects/widget/overview.md", {"archive": True}),
    ("September consolidation metrics", "reports/2026/09/2026-09-28.md", {"reports": True}),
]


def test_known_answer_20_queries(populated_vault: Config):
    """Verify each of the 20 known-answer queries returns its expected file in top 5."""
    assert len(KNOWN_ANSWER_SET) == 20
    for query, expected_path, kwargs in KNOWN_ANSWER_SET:
        hits = search.search(populated_vault, query, all_projects=True, **kwargs)
        top_five = [h.path for h in hits[:5]]
        assert expected_path in top_five, (
            f"Query {query!r} did not return {expected_path} in top 5: {top_five}"
        )


def test_default_search_excludes_archive_and_reports(populated_vault: Config):
    """Default search excludes archive and reports collections."""
    hits = search.search(populated_vault, "bronze casting prototype", all_projects=True)
    assert not any("archive" in h.path for h in hits)

    hits_archive = search.search(
        populated_vault, "bronze casting prototype", all_projects=True, archive=True
    )
    assert any("archive/projects/widget/overview.md" in h.path for h in hits_archive)

    hits_reports = search.search(
        populated_vault, "September consolidation metrics", all_projects=True
    )
    assert not any("reports" in h.path for h in hits_reports)

    hits_with_reports = search.search(
        populated_vault, "September consolidation metrics", all_projects=True, reports=True
    )
    assert any("reports/2026/09/2026-09-28.md" in h.path for h in hits_with_reports)


def test_search_calls_index_update_automatically(populated_vault: Config):
    """Search calls index.update() before querying so newly written files appear."""
    new_page = populated_vault.vault / "projects" / "widget" / "gears.md"
    new_page.write_text(
        "---\n"
        "title: Gears\n"
        "description: Gear ratio specifications.\n"
        "aft:\n"
        "  project: p_widget\n"
        "---\n"
        "# Gears\n"
        "## State\n"
        "- Planetary gearbox ratio is 10:1. [observed] 2026-09-24 [^s1]\n"
    )

    hits = search.search(populated_vault, "Planetary gearbox ratio", all_projects=True)
    assert any("projects/widget/gears.md" in h.path for h in hits)


def test_project_filter_scoping(populated_vault: Config):
    """Project-scoped search includes that project and global pages, excluding others."""
    hits_widget = search.search(populated_vault, "firmware", project="widget")
    assert not any("projects/gadget" in h.path for h in hits_widget)

    hits_gadget = search.search(populated_vault, "firmware", project="gadget")
    assert any("projects/gadget/overview.md" in h.path for h in hits_gadget)

    hits_global = search.search(populated_vault, "dark theme", project="widget")
    assert any("global/preferences.md" in h.path for h in hits_global)


def test_search_scopes_to_the_project_of_the_current_directory(
    populated_vault: Config, monkeypatch
):
    """Inside a registered project search covers it plus global pages; from outside every
    project it covers global pages only."""
    monkeypatch.chdir(populated_vault.vault / "projects" / "widget")
    hits = search.search(populated_vault, "titanium bolt")
    assert any(h.path == "projects/widget/assembly.md" for h in hits)
    hits = search.search(populated_vault, "dark theme")
    assert any(h.path == "global/preferences.md" for h in hits)
    hits = search.search(populated_vault, "monochrome OLED screen")
    assert not any(h.path.startswith("projects/gadget") for h in hits)

    outside = populated_vault.vault.parent / "unregistered"
    outside.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(outside)
    hits = search.search(populated_vault, "titanium bolt")
    assert not any(h.path.startswith(("projects/", "digests/")) for h in hits)
    hits = search.search(populated_vault, "dark theme")
    assert any(h.path == "global/preferences.md" for h in hits)


def test_search_json_and_limit(populated_vault: Config, capsys):
    """Test --json output and --limit flag via CLI."""
    ret = main(["search", "titanium", "--all", "--json", "--limit", "2"])
    assert ret == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert isinstance(data, list)
    assert len(data) <= 2
    assert "path" in data[0]
    assert "description" in data[0]
    assert "snippet" in data[0]


def test_search_cli_plain_text(populated_vault: Config, capsys):
    """Test CLI plain text output format is tab-separated single lines."""
    ret = main(["search", "titanium", "--all", "--limit", "5"])
    assert ret == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) >= 1
    for line in lines:
        parts = line.split("\t")
        assert len(parts) == 3


def test_read_outline(populated_vault: Config):
    """read --level outline extracts headings only, skipping code fences."""
    page_with_fence = populated_vault.vault / "projects" / "widget" / "code.md"
    page_with_fence.write_text(
        "---\n"
        "title: Code Page\n"
        "---\n"
        "# Main Header\n\n"
        "```python\n"
        "# This is a comment\n"
        "x = 1\n"
        "```\n\n"
        "## Subheader\n"
    )
    outline = search.read_page(populated_vault, "projects/widget/code.md", level="outline")
    assert outline == "# Main Header\n## Subheader"


def test_read_outline_skips_lines_that_are_not_headings(populated_vault: Config):
    """Headings match ^#{1,6}\\s, so a #tag or #include line is not a heading."""
    tagged = populated_vault.vault / "projects" / "widget" / "tags.md"
    tagged.write_text(
        "---\n"
        "title: Tag Page\n"
        "---\n"
        "# Real Header\n\n"
        "#not_a_heading\n"
        "#include <config.h>\n"
        "####### seven hashes are not a heading\n\n"
        "## Sub Header\n",
        encoding="utf-8",
    )
    outline = search.read_page(populated_vault, "projects/widget/tags.md", level="outline")
    assert outline == "# Real Header\n## Sub Header"


def test_read_abstract(populated_vault: Config):
    """read --level abstract prints description plus active facts."""
    abstract = search.read_page(populated_vault, "projects/widget/overview.md", level="abstract")
    assert "Everything known about the widget." in abstract
    assert "- The index is plain SQLite storage." in abstract
    assert "- Steel components come from Ohio manufacturing." in abstract


def test_read_full(populated_vault: Config):
    """read --level full prints the full file content."""
    full = search.read_page(populated_vault, "projects/widget/overview.md", level="full")
    assert "type: Project" in full
    assert "# Widget" in full


def test_read_cli(populated_vault: Config, capsys):
    """Test aft read via CLI."""
    assert main(["read", "projects/widget/overview.md", "--level", "outline"]) == 0
    out = capsys.readouterr().out
    assert "# Widget" in out
    assert "## Decisions" in out


def test_read_resolves_a_relative_path_against_the_vault_first(
    populated_vault: Config, tmp_path, monkeypatch
):
    """A page path is vault-relative, so a same-named file in the cwd does not win."""
    decoy = tmp_path / "projects" / "widget" / "overview.md"
    decoy.parent.mkdir(parents=True, exist_ok=True)
    decoy.write_text("---\ntitle: Decoy\n---\n\n# Decoy\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    full = search.read_page(populated_vault, "projects/widget/overview.md")
    assert "Everything known about the widget." in full
    assert "Decoy" not in full


def test_read_refuses_a_file_outside_the_vault(
    populated_vault: Config, tmp_path, monkeypatch, capsys
):
    """A path that resolves outside the vault is refused, not read."""
    secret = tmp_path / "secrets.md"
    secret.write_text("# Bank account numbers\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(search.SearchError, match="outside the vault"):
        search.read_page(populated_vault, "secrets.md")
    assert main(["read", "secrets.md"]) == 2
    assert "outside the vault" in capsys.readouterr().err


def test_read_refuses_a_path_that_climbs_out_of_the_vault(populated_vault: Config, monkeypatch):
    """A .. that leaves the vault is refused even from a cwd inside it."""
    outside = populated_vault.vault.parent / "outside.md"
    outside.write_text("# Outside the vault\n", encoding="utf-8")
    monkeypatch.chdir(populated_vault.vault / "projects" / "widget")
    with pytest.raises(search.SearchError, match="outside the vault"):
        search.read_page(populated_vault, "../outside.md")


def test_read_absolute_paths_inside_and_outside_the_vault(populated_vault: Config, tmp_path):
    """An absolute path inside the vault reads; one outside the vault is refused."""
    inside = populated_vault.vault / "projects" / "widget" / "overview.md"
    assert "# Widget" in search.read_page(populated_vault, str(inside), level="outline")
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text("# Elsewhere\n", encoding="utf-8")
    with pytest.raises(search.SearchError, match="outside the vault"):
        search.read_page(populated_vault, str(elsewhere))


def test_trace_with_evidence(populated_vault: Config):
    """aft trace prints quote and 3 entries around it when evidence exists."""
    output = search.trace(populated_vault, "projects/widget/overview.md", "s1")
    assert "we will use plain SQLite storage" in output
    assert "[e-0] USER" in output
    assert "[e-1] USER" in output
    assert "[e-2] ASSISTANT" in output
    assert "[e-3] USER" not in output


def test_trace_clamped_boundary(populated_vault: Config):
    """aft trace clamps to 3 entries at session start or end."""
    output = search.trace(populated_vault, "projects/gadget/overview.md", "s1")
    assert "firmware uses Rust embedded" in output
    assert "[e-0] USER" in output
    assert "[e-1] USER" in output
    assert "[e-2] ASSISTANT" in output


def test_trace_expired_evidence(populated_vault: Config):
    """aft trace prints quote and expiration notice when evidence is missing."""
    output = search.trace(populated_vault, "projects/widget/overview.md", "s2")
    assert "missing evidence quote" in output
    assert "The full context has expired." in output


def test_trace_cli(populated_vault: Config, capsys):
    """Test aft trace via CLI with footnote syntax [^s1]."""
    assert main(["trace", "projects/widget/overview.md", "[^s1]"]) == 0
    out = capsys.readouterr().out
    assert "we will use plain SQLite storage" in out
    assert "[e-1] USER" in out


def test_search_unknown_project(populated_vault: Config, capsys):
    """aft search with unknown project exits 2."""
    assert main(["search", "titanium", "--project", "nonexistent"]) == 2
    assert "unknown project 'nonexistent'" in capsys.readouterr().err


def test_search_needs_vault(home, capsys):
    """aft search requires an initialized vault."""
    assert main(["search", "anything"]) == 2
    assert "no vault at" in capsys.readouterr().err


def test_read_nonexistent_file(populated_vault: Config, capsys):
    """aft read with missing file exits 2."""
    assert main(["read", "missing.md"]) == 2
    assert "file not found: missing.md" in capsys.readouterr().err


def test_trace_missing_source(populated_vault: Config, capsys):
    """aft trace with missing source id exits 2."""
    assert main(["trace", "projects/widget/overview.md", "s999"]) == 2
    assert "source 's999' not found" in capsys.readouterr().err


def test_trace_missing_page(populated_vault: Config, capsys):
    """aft trace with missing page exits 2."""
    assert main(["trace", "missing.md", "s1"]) == 2
    assert "file not found: missing.md" in capsys.readouterr().err


def test_trace_combined_argument(populated_vault: Config, capsys):
    """aft trace supports <page>#<source-id> as single argument."""
    assert main(["trace", "projects/widget/overview.md#s1"]) == 0
    assert "we will use plain SQLite storage" in capsys.readouterr().out


def test_trace_missing_source_id_arg(populated_vault: Config, capsys):
    """aft trace without source id exits 2."""
    assert main(["trace", "projects/widget/overview.md"]) == 2
    assert "source id required" in capsys.readouterr().err


def test_trace_refuses_a_page_that_is_not_text(populated_vault: Config, capsys):
    """aft trace on a non-text page gives the clean error aft read gives."""
    blob = populated_vault.vault / "projects" / "widget" / "blob.md"
    blob.write_bytes(b"\xff\xfe\x00\x01 not UTF-8 text")
    assert main(["trace", "projects/widget/blob.md", "s1"]) == 2
    assert "is not UTF-8 text" in capsys.readouterr().err


def test_empty_query_returns_nothing(populated_vault: Config):
    """An empty or whitespace-only query returns no hits."""
    assert search.search(populated_vault, "") == []
    assert search.search(populated_vault, "   ") == []


def test_archived_resolved_notes_are_searchable_with_archive_flag(
    populated_vault: Config, monkeypatch, capsys
):
    """F10: after a note is resolved, aft search --archive <words of the answer> finds it,
    and default search does not.
    """
    from aft import pagewrite

    overview_path = populated_vault.vault / "projects/widget/overview.md"
    doc = pagewrite.PageDoc.read(overview_path, vault=populated_vault.vault)
    fact = doc.fact("The index is plain SQLite storage.")
    doc.add_callout(
        fact,
        "question",
        (
            "Should we switch to PostgreSQL for concurrency?\n"
            "We will stick with embedded SQLite for offline portability."
        ),
        "2026-10-01",
    )
    overview_path.write_text(doc.render())

    # Switch into project directory for implicit cwd scoping
    monkeypatch.chdir(populated_vault.vault / "projects" / "widget")

    # Before resolving, searching for the answer finds nothing (callouts on
    # active pages are not indexed)
    assert search.search(populated_vault, "offline portability", archive=True) == []
    assert search.search(populated_vault, "offline portability") == []

    # Resolve the callout
    doc = pagewrite.PageDoc.read(overview_path, vault=populated_vault.vault)
    fact = doc.fact("The index is plain SQLite storage.")
    assert doc.resolve_callout(fact) is True
    overview_path.write_text(doc.render())

    # After resolving:
    # 1. aft search --archive <words of the answer> finds it!
    archive_hits = search.search(populated_vault, "offline portability", archive=True)
    assert len(archive_hits) >= 1
    assert archive_hits[0].path == "archive/projects/widget/overview.md"
    assert archive_hits[0].collection == "archive"
    assert archive_hits[0].heading == "Resolved notes"
    assert "portability" in archive_hits[0].snippet or "embedded" in archive_hits[0].snippet

    # 2. default search does NOT find it
    default_hits = search.search(populated_vault, "offline portability")
    assert default_hits == []

    # 3. aft search --archive <words of the fact> also finds it in archive
    fact_hits = search.search(populated_vault, "plain SQLite storage", archive=True)
    assert any(h.path == "archive/projects/widget/overview.md" for h in fact_hits)

    # 4. CLI test with AFT_HOME pointing to populated_vault.home
    monkeypatch.setenv("AFT_HOME", str(populated_vault.home))
    assert main(["search", "offline portability", "--archive"]) == 0
    out = capsys.readouterr().out
    assert "archive/projects/widget/overview.md" in out
    assert "portability" in out

    assert main(["search", "offline portability"]) == 0
    out_default = capsys.readouterr().out
    assert "archive/projects/widget/overview.md" not in out_default
