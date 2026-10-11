"""Tests for search, read and trace (BUILD-PLAN M3.2, DESIGN 10)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

import pytest

from aft import evidence, forget, index, search
from aft.cli import main
from aft.config import Config
from aft.readers import Entry
from aft.registry import Project, Registry
from conftest import run_git

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


def test_search_finds_other_forms_of_a_word(populated_vault: Config):
    """Words are stemmed (BUILD-PLAN F61): "pause" finds "paused", "configure" finds
    "configured", whichever form the line used."""
    (populated_vault.vault / "projects" / "widget" / "hooks.md").write_text(
        "---\n"
        "title: Hooks\n"
        "description: Hook rewrite status.\n"
        "aft:\n"
        "  project: p_widget\n"
        "---\n"
        "# Hooks\n"
        "## State\n"
        "- The hook rewrite is paused until the retries are configured."
        " [observed] 2026-09-24 [^s1]\n"
    )
    for query in ("pause", "pausing", "configure", "retry"):
        hits = search.search(populated_vault, query, all_projects=True)
        assert any("projects/widget/hooks.md" in h.path for h in hits), query


def test_project_filter_scoping(populated_vault: Config):
    """Project-scoped search includes that project and global pages, excluding others."""
    hits_widget = search.search(populated_vault, "firmware", project="widget")
    assert not any("projects/gadget" in h.path for h in hits_widget)

    hits_gadget = search.search(populated_vault, "firmware", project="gadget")
    assert any("projects/gadget/overview.md" in h.path for h in hits_gadget)

    hits_global = search.search(populated_vault, "dark theme", project="widget")
    assert any("global/preferences.md" in h.path for h in hits_global)


GADGET_ASSEMBLY_TWIN = """---
type: Project
title: Widget Assembly
description: Procedures for assembling the widget.
sources: []
aft:
  project: p_gadget
---

# Widget Assembly

## State

- Fastening requires titanium bolt tightening to 15 Nm. [observed] 2026-09-22 [^s1]
"""

GADGET_TEAM_PREFERENCES = """---
type: Preferences
title: Gadget Preferences
description: The gadget team's editor preferences.
sources: []
aft:
  project: p_gadget
---

# Gadget Preferences

## Preferences

- The gadget runs dark theme syntax in dark theme colors for contrast. [stated] 2026-09-26 [^s1]
"""


def test_the_default_search_ranks_this_projects_memory_first(populated_vault: Config, monkeypatch):
    """F50: the default search reaches every project that is not archived, with the current
    directory's project ranked first. An equal match in another project answers behind this
    project's, the global pages answer ahead of the other projects' memory even when the other
    project's row outscores theirs, and every hit names the project it belongs to."""
    (populated_vault.vault / "projects/gadget/assembly.md").write_text(
        GADGET_ASSEMBLY_TWIN, encoding="utf-8"
    )
    (populated_vault.vault / "projects/gadget/preferences.md").write_text(
        GADGET_TEAM_PREFERENCES, encoding="utf-8"
    )
    monkeypatch.chdir(populated_vault.vault / "projects" / "widget")

    hits = search.search(populated_vault, "titanium bolt", limit=20)
    widget = [hit for hit in hits if hit.path == "projects/widget/assembly.md"]
    gadget = [hit for hit in hits if hit.path == "projects/gadget/assembly.md"]
    assert widget and gadget, "the same fact in both projects is indexed and reaches the search"
    assert hits.index(widget[0]) < hits.index(gadget[0]), (
        "the fact answers from this project first, the equal match in the other behind it"
    )
    assert widget[0].project == "widget" and gadget[0].project == "gadget", (
        "each hit names the project it belongs to"
    )

    # The gadget page carries "dark theme" twice in one row and outscores the global page's
    # single mention; the global page still answers first, on the tier and not on the words.
    hits = search.search(populated_vault, "dark theme", limit=20)
    assert [hit.path for hit in hits][:2] == [
        "global/preferences.md",
        "projects/gadget/preferences.md",
    ]
    assert [hit.project for hit in hits][:2] == ["global", "gadget"]


def test_the_all_flag_changes_nothing(populated_vault: Config, monkeypatch) -> None:
    """F50: `--all` named a cross-project search while the default still scoped to one project.
    The default reaches every project that is not archived now (DESIGN 10), so the flag is
    accepted and changes nothing, and the scripts that pass it keep working."""
    cfg = populated_vault
    plant_unfiled(cfg)
    monkeypatch.chdir(cfg.vault / "projects" / "widget")

    plain = [hit.path for hit in search.search(cfg, "zirconium", limit=20)]
    flagged = [hit.path for hit in search.search(cfg, "zirconium", all_projects=True, limit=20)]
    assert plain == flagged != [], "the unfiled text answers by default, --all or not"
    assert {(hit.project, hit.collection) for hit in search.search(cfg, "zirconium", limit=20)} == {
        ("unfiled", "digests"),
        ("unfiled", "evidence"),
    }, "the unfiled text answers as the unfiled group, named, from either collection"


def test_search_json_and_limit(populated_vault: Config, capsys):
    """Test --json output and --limit flag via CLI."""
    ret = main(["search", "titanium", "--all", "--json", "--limit", "2"])
    assert ret == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert isinstance(data, list)
    assert len(data) <= 2
    assert "path" in data[0]
    assert "path" in data[0]
    assert "project" in data[0]
    assert "description" in data[0]
    assert "snippet" in data[0]


def test_search_cli_plain_text(populated_vault: Config, capsys):
    """The plain-text line names where the hit came from as well as what matched: document,
    collection, section, project, description, snippet, tab-separated on one line. DESIGN 10
    asks a result to name its document and its section, F50 its project, and a hit from the
    archive, the library or another project's memory reads differently once the line says
    which collection and project it came from."""
    ret = main(["search", "titanium", "--limit", "5"])
    assert ret == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) >= 1
    for line in lines:
        assert len(line.split("\t")) == 6
    path, collection, heading, project, description, snippet = lines[0].split("\t")
    assert path == "projects/widget/assembly.md"
    assert collection == "pages"
    assert heading == "State"
    assert project == "widget"
    assert description == "Procedures for assembling the widget."
    assert "titanium" in snippet.lower()


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


# A page whose superseded line is still on it. That is where a struck line sits from the day a
# dream strikes it until `dream.maintain` moves the file at 90 days, so DESIGN 10's ``--archive``
# has to reach it where it stands (BUILD-PLAN F20). Each line is searched below by a word none
# other carries, so a hit says where that line was indexed rather than which page is wordy.
PROTOTYPE_PAGE = """---
type: Project
title: Widget prototype
description: The earlier prototype that was dropped.
sources: []
aft:
  project: p_widget
---

# Widget prototype

## Decisions

- The prototype runs on a nixie driver board. [stated] 2026-09-26 [^s1]
- ~~The prototype ran on a valve board.~~ [stated] 2026-01-01 [^s1] superseded 2026-02-01 by [^s1]
- ~~The prototype ran on a mercury battery.~~ [stated] 2026-01-02 [^s1] withdrawn 2026-02-02
"""


def test_a_line_superseded_today_answers_the_archive_flag_from_its_own_page(
    populated_vault: Config, monkeypatch, capsys
) -> None:
    """F20: a line superseded today, still on the page that carries it, is found with
    `aft search --archive` and never by the default search (DESIGN 10) - and the hit names the page
    it is on, so `aft read` opens the page that still holds the struck line. A withdrawn line
    reaches neither, which is what DESIGN 12 says a withdrawal is for."""
    (populated_vault.vault / "projects/widget/prototype.md").write_text(
        PROTOTYPE_PAGE, encoding="utf-8"
    )
    monkeypatch.chdir(populated_vault.vault / "projects" / "widget")

    # One word each, alone in the vault: search terms answer on their own, so "driver" would come
    # back from the live line of the same page and say nothing about the struck one.
    assert [hit.path for hit in search.search(populated_vault, "nixie")] == [
        "projects/widget/prototype.md"
    ], "the live line is memory and answers on its own"

    assert search.search(populated_vault, "valve") == [], "out of memory, not in a hit"

    hits = search.search(populated_vault, "valve", archive=True)
    assert [(hit.path, hit.collection, hit.heading) for hit in hits] == [
        ("projects/widget/prototype.md", "archive", "Decisions")
    ]

    assert search.search(populated_vault, "mercury", archive=True) == []
    assert search.search(populated_vault, "mercury") == []

    monkeypatch.setenv("AFT_HOME", str(populated_vault.home))
    assert main(["search", "valve", "--archive"]) == 0
    assert "projects/widget/prototype.md\tarchive\tDecisions" in capsys.readouterr().out
    assert main(["search", "valve"]) == 0
    assert "prototype.md" not in capsys.readouterr().out


GLOBAL_ENVIRONMENT_WITH_A_STRUCK_LINE = """---
type: Environment
title: Environment
description: Developer machine configuration.
sources: []
---

# Environment

## State

- The workstation runs a tiling compositor. [stated] 2026-09-26 [^s1]
- ~~The workstation ran a zeppelin.~~ [stated] 2026-01-01 [^s1] superseded 2026-02-01 by [^s1]
"""


def test_a_global_pages_struck_line_answers_the_archive_flag_as_shared_history(
    populated_vault: Config, monkeypatch, tmp_path
) -> None:
    """The archive mirrors the pages: a global page's line is every project's memory while it is
    active, so once struck it answers every project's `--archive` as shared history, a tier
    ahead of the projects. F50 takes `--archive` further from the same query: a project's own
    resting line answers from every working directory too, and names the project it belongs to."""
    (populated_vault.vault / "global/environment.md").write_text(
        GLOBAL_ENVIRONMENT_WITH_A_STRUCK_LINE, encoding="utf-8"
    )

    monkeypatch.chdir(populated_vault.vault / "projects" / "widget")
    assert search.search(populated_vault, "zeppelin") == [], "out of memory, not in a hit"
    hits = search.search(populated_vault, "zeppelin", archive=True)
    assert [(hit.path, hit.collection) for hit in hits] == [("global/environment.md", "archive")]
    hits = search.search(populated_vault, "Bluetooth", archive=True)
    assert [(hit.path, hit.project) for hit in hits] == [
        ("archive/projects/gadget/overview.md", "gadget")
    ], "the gadget project's resting line answers --archive from here, behind the shared history"

    outside = tmp_path / "nowhere"
    outside.mkdir()
    monkeypatch.chdir(outside)
    assert search.search(populated_vault, "zeppelin") == []
    hits = search.search(populated_vault, "zeppelin", archive=True)
    assert [(hit.path, hit.collection) for hit in hits] == [("global/environment.md", "archive")]
    hits = search.search(populated_vault, "Bluetooth", archive=True)
    assert [(hit.path, hit.project) for hit in hits] == [
        ("archive/projects/gadget/overview.md", "gadget")
    ]


# What a search asks SQLite for (the M3 performance review). A hit is a document and a row is a
# section, so a limit cannot simply be asked for at the door - and without one asked for at all,
# a search whose every line carries the queried word built, snippeted and carried back every
# match in the vault to keep eight of them.


def test_a_search_asks_sqlite_for_the_rows_it_keeps(
    populated_vault: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The limit belongs in the statement, not in the loop that reads the answer.

    Every statement the search sends to SQLite is caught on its way out and read. Each one that
    ranks a collection has to name how many rows it wants, and each one that cuts a snippet has
    to name the rows it is cutting for: a snippet is only correct when it is cut by a scan of
    that collection's matches, so the rows kept by the ranking statement come back by rowid and
    are snippeted by a second statement that scans exactly them. A search that asked for none and
    then stopped counting at eight still returned the right eight hits, which is exactly how the
    cost stayed invisible, and a search that snippeted its way through every match to get a
    correct snippet paid for all of them."""
    statements: list[str] = []
    real_connect = sqlite3.connect

    def traced_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(search.sqlite3, "connect", traced_connect)

    hits = search.search(populated_vault, "SQLite", project="widget", limit=3)
    assert hits, "the search found nothing, so it proved nothing"

    ranked = [sql for sql in statements if "MATCH" in sql and "bm25" in sql]
    assert ranked, f"no ranking statement reached SQLite: {statements}"
    assert all("LIMIT" in sql for sql in ranked), ranked

    snippeted = [sql for sql in statements if "MATCH" in sql and "snippet(" in sql]
    assert snippeted, f"no snippet statement reached SQLite: {statements}"
    assert all("rowid IN (" in sql for sql in snippeted), snippeted
    assert any("SQLite" in hit.snippet for hit in hits), [hit.snippet for hit in hits]


def test_one_document_with_many_rows_does_not_take_the_whole_limit(
    populated_vault: Config,
) -> None:
    """The room a collection is asked for is documents, not rows.

    A page whose every line carries the queried word is one row per section and one hit, and a
    limit asked for before the rows are collapsed to documents hands back that page's sections and
    calls the search over. Each collection is narrowed to one row per document - the best-ranked
    one, the only row of it a search could have kept - inside the statement that limits it, so what
    comes back is documents: the busy page shares a limit of two with an archived entry rather than
    filling it with its own sections."""
    facts = "\n\n".join(
        f"## {section}\n\n- The shim behind plate {number} is obsidian. [observed] 2026-09-20 [^s1]"
        for number, section in enumerate(
            ("Facts", "State", "Decisions", "Preferences", "Lessons", "Procedures", "References")
        )
    )
    (populated_vault.vault / "projects/widget/shims.md").write_text(
        "---\n"
        "type: Project\n"
        "title: Widget Shims\n"
        "description: Every shim in the widget.\n"
        "sources: []\n"
        "aft:\n  project: p_widget\n"
        "---\n\n"
        "# Widget Shims\n\n"
        f"{facts}\n",
        encoding="utf-8",
    )
    evidence.store(
        populated_vault,
        "pi",
        "s-shim",
        "p_widget",
        [
            Entry(
                "e-0",
                datetime(2026, 9, 28, 12, 0, tzinfo=UTC),
                "user",
                "the shim is obsidian",
                False,
            )
        ],
    )

    hits = search.search(populated_vault, "obsidian", project="widget", limit=2)
    paths = [hit.path for hit in hits]

    with sqlite3.connect(populated_vault.home / index.INDEX_NAME) as conn:
        sections = conn.execute(
            "SELECT count(*) FROM pages WHERE path = 'projects/widget/shims.md'"
        ).fetchone()[0]
    assert sections >= 6, f"the page indexed {sections} rows, so the test proved nothing"

    assert len(paths) == 2, f"only {len(paths)} documents answered: {paths}"
    assert len(set(paths)) == 2, paths
    assert "projects/widget/shims.md" in paths, paths
    assert any(path.startswith("aft://session/pi/s-shim") for path in paths), paths


# ------------------------------------------- narrowed scoping: unfiled is no project's memory
UNFILED_DIGEST = """---
type: Session Digest
title: Scratch session
description: Notes taken in a directory that names no project.
---

## Summary

The scratch build was held together with zirconium pins.
"""


def plant_unfiled(cfg: Config) -> None:
    """Memory the index holds with no project on its rows: an archived entry whose session resolved
    to no project (DESIGN 7 step 4), and a digest filed under a slug no registry answers for."""
    evidence.store(
        cfg,
        "pi",
        "s-unreg",
        None,
        [Entry("e-7", datetime(2026, 9, 28, 12, 0, tzinfo=UTC), "user", "zirconium pins", False)],
    )
    path = cfg.vault / "digests/scratchdir/2026/09/2026-09-28-pi-s-scratch.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(UNFILED_DIGEST, encoding="utf-8")
    index.update(cfg)


def test_a_project_search_leaves_out_memory_that_names_no_project(populated_vault: Config) -> None:
    """A query narrowed with ``--project`` takes that project's memory plus the global pages
    (DESIGN 10), and only a page row's empty project means shared (DESIGN 6). An entry whose
    session resolved to no project and a digest under a slug the registry has never heard of
    are text that was never filed, so neither belongs in a project's narrowed results - while
    the default search reaches them, named the unfiled group (F50)."""
    cfg = populated_vault
    plant_unfiled(cfg)

    named = {(hit.project, hit.collection) for hit in search.search(cfg, "zirconium", limit=20)}
    assert ("unfiled", "digests") in named and ("unfiled", "evidence") in named, (
        "the default search answers the unfiled text from both of its collections"
    )

    assert search.search(cfg, "zirconium", project="widget") == [], (
        "a narrowed query takes no text that was never filed under a project"
    )


def test_search_from_outside_every_project_reaches_every_project(
    populated_vault: Config, monkeypatch
) -> None:
    """F50: from a working directory inside no project, the default search still reaches every
    project that is not archived, and memory that names no project answers under its own name:
    the other projects' pages, and the unfiled text of a session whose directory belonged to no
    project, with its digest beside it."""
    cfg = populated_vault
    plant_unfiled(cfg)
    outside = cfg.vault.parent / "unregistered"
    outside.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(outside)

    hits = search.search(cfg, "dark theme")
    assert [hit.path for hit in hits] == ["global/preferences.md"]
    assert hits[0].project == "global"

    hits = search.search(cfg, "titanium bolt", limit=20)
    assert [hit.path for hit in hits if hit.project == "widget"] == [
        "projects/widget/assembly.md"
    ], "the other project's page answers from here, named for the project it belongs to"

    hits = search.search(cfg, "zirconium", limit=20)
    assert {(hit.project, hit.collection) for hit in hits} == {
        ("unfiled", "digests"),
        ("unfiled", "evidence"),
    }, "the unfiled text answers as the unfiled group, named, from either collection"


def test_a_known_answer_pass_at_the_default_scope_reaches_every_collection(
    populated_vault: Config,
) -> None:
    """The known-answer set above runs with ``--all``, so it never expected a hit from the
    evidence archive. This is the same promise at the default scope: the project's curated line and
    the archived entry it quotes both answer, its digest answers a query about what it recorded,
    and no row of the other project comes back."""
    cfg = populated_vault
    paths = {hit.path for hit in search.search(cfg, "plain SQLite storage", project="widget")}

    assert "projects/widget/overview.md" in paths, "the curated line"
    assert any(path.startswith("aft://session/claude/s-widget-1#") for path in paths), (
        "the archived entry itself"
    )
    assert not any(path.startswith(("projects/gadget", "digests/gadget")) for path in paths)

    digest_paths = {hit.path for hit in search.search(cfg, "prototype", project="widget")}
    assert "digests/widget/2026/09/2026-09-28-claude-s-widget-1.md" in digest_paths, "the digest"


# ------------------------------------------- a hit says where it came from


def test_an_evidence_hit_names_its_entry_so_a_trace_opens_it(
    populated_vault: Config, capsys
) -> None:
    """An evidence hit used to print the session's resource and nothing else, which `aft read`
    refuses and `aft trace` cannot use: the hit led nowhere. The row is one archived entry, so the
    path names that entry, and DESIGN 10's `aft trace` opens the entries around it straight from
    the path a search printed."""
    hits = search.search(populated_vault, "debugging SPI bus", all_projects=True, limit=20)
    paths = {hit.path for hit in hits if hit.collection == "evidence"}
    assert "aft://session/claude/s-gadget-1#e-0" in paths, "the hit names the entry it matched"

    assert main(["trace", "aft://session/claude/s-gadget-1#e-0"]) == 0
    out = capsys.readouterr().out
    assert "[e-0] USER" in out and "debugging SPI bus" in out
    assert "[e-1] USER" in out, "the trace shows the entries around the hit"

    # The two-argument form, with the session and the entry named apart, works the same way.
    assert main(["trace", "aft://session/claude/s-gadget-1", "e-0"]) == 0
    assert "debugging SPI bus" in capsys.readouterr().out


def test_json_hits_carry_their_collection_and_section(populated_vault: Config, capsys) -> None:
    """The machine-readable hit answers the same question the plain-text line does: which
    collection, which section of it, and which project it belongs to (DESIGN 10 names a library
    result by its page or section, F50 has every result name its project, and there is no reason
    to make a person read the page to learn where a hit came from)."""
    assert main(["search", "titanium", "--all", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    (first,) = [hit for hit in data if hit["path"] == "projects/widget/assembly.md"]
    assert first["collection"] == "pages"
    assert first["heading"] == "State"
    assert first["project"] == "widget"


# ------------------------------------------- the abstract carries what the block injects

CONFLICTED_PAGE = """---
type: Project
title: Widget Materials
description: What the widget is made of.
sources:
  - id: s1
    resource: aft://session/claude/s-widget-1#e-1
    quote: "we will use plain SQLite storage"
aft:
  project: p_widget
---

# Widget Materials

## Facts

- The flange tolerance is 50 microns. [observed] 2026-09-22 [^s1]

- Ship the flange in titanium. [stated] 2026-09-23 [^s1]

> [!conflict] 2026-09-24
> The vendor's sheet calls for bronze.

- Ask the vendor for the bolt torque. [stated] 2026-09-23 [^s1]

> [!question] 2026-09-24
> What torque does the vendor rate the bolt at?
"""


def test_read_abstract_keeps_the_disputed_and_waiting_lines(
    populated_vault: Config,
) -> None:
    """DESIGN 6 makes a conflict and a question an active line with a note beside it, and
    `aft inject` shows all three to a new session. An abstract that kept only the unmarked lines
    dropped the two a person most needs before acting on the page - and kept each line's mark, so
    a disputed line still reads as disputed."""
    populated_vault.vault.joinpath("projects/widget/materials.md").write_text(
        CONFLICTED_PAGE, encoding="utf-8"
    )

    abstract = search.read_page(populated_vault, "projects/widget/materials.md", level="abstract")

    assert "- The flange tolerance is 50 microns." in abstract
    assert "- Ship the flange in titanium. (disputed)" in abstract
    assert "- Ask the vendor for the bolt torque. (waiting on the user)" in abstract


# ------------------------------------------- forgotten and purged memory answers nothing


def test_search_read_and_trace_answer_nothing_once_a_session_is_forgotten(
    populated_vault: Config,
) -> None:
    """DESIGN 12 promises forgotten content leaves everything afterthought controls, and the three
    ways back into memory are the three that must go quiet: `aft search` answers no collection,
    `aft read` has no digest left to open, and `aft trace` has no context to show."""
    cfg = populated_vault
    assert "we will use plain SQLite storage" in search.trace(
        cfg, "projects/widget/overview.md", "s1"
    )
    assert search.search(cfg, "plain SQLite storage", all_projects=True, limit=20)
    # A forget commits what it changed, and the fixture wrote its pages straight to the working
    # tree: commit them first, as a finished run would have left them.
    run_git(["add", "-A"], cfg.vault)
    run_git(["commit", "-q", "-m", "fixture memory"], cfg.vault)

    forget.forget_session(cfg, "s-widget-1")

    assert search.search(cfg, "plain SQLite storage", all_projects=True, limit=20) == []
    with pytest.raises(search.SearchError):
        search.read_page(cfg, "digests/widget/2026/09/2026-09-28-claude-s-widget-1.md")
    assert search.trace(cfg, "aft://session/claude/s-widget-1", "e-1") == search.EXPIRED
    assert "we will use plain SQLite storage" not in search.trace(
        cfg, "projects/widget/overview.md", "s1"
    )


def test_search_and_trace_answer_nothing_once_a_session_is_purged(
    populated_vault: Config,
) -> None:
    """The retention window expires archived conversation with no `aft forget` behind it (DESIGN 8),
    and the index follows the archive out: the words answer no search, and a trace of a quote that
    rested on them says what DESIGN 10 has it say."""
    cfg = populated_vault
    long_ago = datetime(2026, 1, 1, tzinfo=UTC)
    evidence.store(
        cfg, "pi", "s-old", "p_widget", [Entry("e-1", long_ago, "user", "nitrile gaskets", False)]
    )
    index.update(cfg)
    assert "aft://session/pi/s-old#e-1" in {
        hit.path for hit in search.search(cfg, "nitrile", all_projects=True, limit=20)
    }

    assert evidence.purge(cfg, datetime(2026, 9, 28, 12, 0, tzinfo=UTC)) == 1
    index.update(cfg)

    assert search.search(cfg, "nitrile", all_projects=True, limit=20) == []
    assert search.trace(cfg, "aft://session/pi/s-old", "e-1") == search.EXPIRED


ARCHIVED_PROJECT_DIGEST = """---
type: Session Digest
title: Widget session 7
description: The widget's kumquat experiment.
aft:
  session: s-widget-7
---

# Widget session 7

## Summary

The widget ran a kumquat acidity trial.
"""


def test_an_archived_projects_memory_answers_only_the_archive_flag(
    populated_vault: Config, monkeypatch, capsys
) -> None:
    """F49: archiving a project takes its pages and digests out of the everyday search and
    leaves them to `aft search --archive`, without moving the files (DESIGN 10, F49). F50 gives
    `--archive` its reach from any working directory and names every hit's project. A file
    with no project of its own follows the project its directory names. Unarchiving puts
    them back, because the registry's change re-derives every row the project named."""
    digest_rel = "digests/widget/2026/09/2026-09-27-claude-s-widget-7.md"
    digest_path = populated_vault.vault / digest_rel
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    digest_path.write_text(ARCHIVED_PROJECT_DIGEST, encoding="utf-8")
    monkeypatch.chdir(populated_vault.vault / "projects" / "widget")

    assert [hit.path for hit in search.search(populated_vault, "kumquat")] == [digest_rel]
    assert [hit.path for hit in search.search(populated_vault, "milling")] == [
        "projects/widget/flange.md"
    ]

    registry = Registry.load(populated_vault.registry_path)
    registry.set_archived("widget", "2026-10-04")
    registry.save()

    assert search.search(populated_vault, "kumquat") == []
    assert search.search(populated_vault, "milling") == []
    hits = search.search(populated_vault, "kumquat", archive=True)
    assert [(hit.path, hit.collection) for hit in hits] == [(digest_rel, "archive")]
    hits = search.search(populated_vault, "milling", archive=True)
    assert [(hit.path, hit.collection) for hit in hits] == [
        ("projects/widget/flange.md", "archive")
    ]

    # F50: the archived project answers `--archive` from every working directory, another
    # project's and one in no project at all, while the everyday search reaches it from none
    # of them. Each hit names the project its resting memory belongs to.
    monkeypatch.chdir(populated_vault.vault / "projects" / "gadget")
    assert search.search(populated_vault, "kumquat") == []
    assert [hit.project for hit in search.search(populated_vault, "kumquat", archive=True)] == [
        "widget"
    ]
    monkeypatch.chdir(populated_vault.vault.parent)
    assert search.search(populated_vault, "kumquat") == []
    assert [hit.path for hit in search.search(populated_vault, "kumquat", archive=True)] == [
        digest_rel
    ]
    monkeypatch.chdir(populated_vault.vault / "projects" / "widget")
    assert [hit.path for hit in search.search(populated_vault, "kumquat", archive=True)] == [
        digest_rel
    ]

    registry = Registry.load(populated_vault.registry_path)
    registry.set_archived("widget", "")
    registry.save()
    assert [hit.path for hit in search.search(populated_vault, "kumquat")] == [digest_rel]
    assert [hit.path for hit in search.search(populated_vault, "milling")] == [
        "projects/widget/flange.md"
    ]


def test_search_links_and_read_backlinks_section(
    populated_vault: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """aft search --links finds backlinks and aft read renders backlinks section."""
    monkeypatch.setenv("AFT_HOME", str(populated_vault.home))

    # Create reference page
    ref_dir = populated_vault.vault / "references"
    ref_dir.mkdir(parents=True, exist_ok=True)
    ref_page = (
        "---\n"
        "type: Reference\n"
        "title: SQLite\n"
        "sources: []\n"
        "---\n\n"
        "## References\n\n"
        "- SQLite is an embedded database. [stated] 2026-09-28 [^s1]\n"
    )
    (ref_dir / "sqlite.md").write_text(ref_page, encoding="utf-8")

    # Create referencing page
    source_page = (
        "---\n"
        "type: Project\n"
        "title: Widget Notes\n"
        "sources: []\n"
        "aft:\n"
        "  project: p_widget\n"
        "---\n\n"
        "## Facts\n\n"
        "- The system uses [[references/sqlite]] for local storage. [stated] 2026-09-28 [^s1]\n"
    )
    (populated_vault.vault / "projects/widget/notes.md").write_text(source_page, encoding="utf-8")

    index.update(populated_vault)

    # 1. API test: search(links=...) and search_links(...)
    hits = search.search(populated_vault, links="references/sqlite")
    assert len(hits) == 1
    assert hits[0].path == "projects/widget/notes.md"
    assert "uses [[references/sqlite]]" in hits[0].snippet
    assert hits[0].heading == "Facts"
    assert hits[0].project == "widget"

    hits_helper = search.search_links(populated_vault, "references/sqlite.md")
    assert len(hits_helper) == 1
    assert hits_helper[0].path == "projects/widget/notes.md"

    # 2. CLI test: aft search --links
    capsys.readouterr()
    assert main(["search", "--links", "references/sqlite"]) == 0
    out = capsys.readouterr().out
    assert "projects/widget/notes.md" in out
    assert "uses [[references/sqlite]]" in out

    # CLI test: aft search --links with --json
    assert main(["search", "--links", "references/sqlite", "--json"]) == 0
    json_out = capsys.readouterr().out
    data = json.loads(json_out)
    assert len(data) == 1
    assert data[0]["path"] == "projects/widget/notes.md"

    # 3. Read page with backlinks renders Backlinks section
    content = search.read_page(populated_vault, "references/sqlite.md")
    assert "## Backlinks / Referenced In" in content
    assert "- projects/widget/notes.md" in content

    # CLI test: aft read
    assert main(["read", "references/sqlite.md"]) == 0
    read_out = capsys.readouterr().out
    assert "## Backlinks / Referenced In" in read_out
    assert "- projects/widget/notes.md" in read_out

    # 4. Page without backlinks does not render Backlinks section
    no_backlinks_content = search.read_page(populated_vault, "projects/widget/notes.md")
    assert "## Backlinks / Referenced In" not in no_backlinks_content


def test_search_links_filters_withdrawn_source_line(populated_vault: Config) -> None:
    """A link on a withdrawn fact line answers no query, with or without --archive (M12)."""
    ref_page = (
        "---\n"
        "type: Reference\n"
        "title: Old DB\n"
        "sources: []\n"
        "---\n\n"
        "## References\n\n"
        "- The old database held everything. [stated] 2026-09-20 [^s1]\n"
    )
    (populated_vault.vault / "references").mkdir(parents=True, exist_ok=True)
    (populated_vault.vault / "references/old-db.md").write_text(ref_page, encoding="utf-8")
    source_page = (
        "---\n"
        "type: Project\n"
        "title: Widget Notes\n"
        "sources: []\n"
        "aft:\n"
        "  project: p_widget\n"
        "---\n\n"
        "## Facts\n\n"
        "- The system uses [[references/sqlite]] for local storage. [stated] 2026-09-28 [^s1]\n"
        "- ~~The system ran on [[references/old-db]] once.~~ [stated] 2026-09-20 [^s1]"
        " withdrawn 2026-09-27\n"
    )
    (populated_vault.vault / "projects/widget/notes.md").write_text(source_page, encoding="utf-8")
    index.update(populated_vault)

    # The live line's link still answers, from the pages collection.
    hits = search.search(populated_vault, links="references/sqlite")
    assert [hit.path for hit in hits] == ["projects/widget/notes.md"]
    assert hits[0].collection == index.PAGES

    # The withdrawn line's link answers no query at all (DESIGN 12): its words left
    # memory, so no flag brings them back.
    assert search.search(populated_vault, links="references/old-db") == []
    assert search.search(populated_vault, links="references/old-db", archive=True) == []


def test_search_links_filters_archived_sources(
    populated_vault: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A link from an archived project's page, or from the archive tree, answers only
    --archive and carries the archive collection's name (M12)."""
    monkeypatch.setenv("AFT_HOME", str(populated_vault.home))
    ref_page = (
        "---\n"
        "type: Reference\n"
        "title: SQLite\n"
        "sources: []\n"
        "---\n\n"
        "## References\n\n"
        "- SQLite is an embedded database. [stated] 2026-09-28 [^s1]\n"
    )
    (populated_vault.vault / "references").mkdir(parents=True, exist_ok=True)
    (populated_vault.vault / "references/sqlite.md").write_text(ref_page, encoding="utf-8")

    # An archived project's page keeps its link, but its project is out of memory.
    reg = Registry.load(populated_vault.registry_path)
    reg.set_archived("gadget", "2026-09-28")
    reg.save()
    gadget_page = (
        "---\n"
        "type: Project\n"
        "title: Gadget Notes\n"
        "sources: []\n"
        "aft:\n"
        "  project: p_gadget\n"
        "---\n\n"
        "## Facts\n\n"
        "- The gadget stores its logs in [[references/sqlite]]. [stated] 2026-09-28 [^s1]\n"
    )
    (populated_vault.vault / "projects/gadget/notes.md").write_text(gadget_page, encoding="utf-8")

    # So does a page resting in the archive tree.
    resting_page = (
        "---\n"
        "type: Project\n"
        "title: Widget Archive\n"
        "sources: []\n"
        "---\n\n"
        "## Facts\n\n"
        "- The prototype used [[references/sqlite]] directly. [stated] 2026-09-20 [^s1]\n"
    )
    resting = populated_vault.vault / "archive/projects/widget/old.md"
    resting.parent.mkdir(parents=True, exist_ok=True)
    resting.write_text(resting_page, encoding="utf-8")

    index.update(populated_vault)

    # The default search reaches neither source: both answer only --archive.
    assert search.search(populated_vault, links="references/sqlite") == []

    hits = search.search(populated_vault, links="references/sqlite", archive=True)
    assert [hit.path for hit in hits] == [
        "archive/projects/widget/old.md",
        "projects/gadget/notes.md",
    ]
    assert all(hit.collection == index.ARCHIVE for hit in hits)
    assert [hit.project for hit in hits] == ["widget", "gadget"]

    # The CLI names the collection on the line, so an archived hit reads as archived.
    capsys.readouterr()
    assert main(["search", "--links", "references/sqlite", "--archive"]) == 0
    out = capsys.readouterr().out
    assert "projects/gadget/notes.md\tarchive\t" in out
    assert main(["search", "--links", "references/sqlite"]) == 0
    assert "projects/gadget/notes.md" not in capsys.readouterr().out


def test_search_query_stripping_preserves_page_results(populated_vault: Config) -> None:
    """Temporal phrases are stripped from FTS5 lexical queries so pages still match."""
    now = datetime(2026, 9, 28, 14, 0, tzinfo=UTC)
    # The file contains "titanium bolt" but not "past week" or "last 3 days"
    hits = search.search(populated_vault, "titanium bolt past week", now=now)
    assert any(h.path == "projects/widget/assembly.md" for h in hits)

    hits_days = search.search(populated_vault, "titanium bolt last 3 days", now=now)
    assert any(h.path == "projects/widget/assembly.md" for h in hits_days)


def test_search_temporal_filtering_evidence(populated_vault: Config) -> None:
    """Evidence queries filter by matching ISO date range boundaries."""
    # Evidence entry was stored at 2026-09-28T12:00:00+00:00
    hits_matching_date = search.search(
        populated_vault, "plain SQLite storage 2026-09-28", all_projects=True
    )
    evidence_paths = [h.path for h in hits_matching_date if h.collection == "evidence"]
    assert any("s-widget-1" in p for p in evidence_paths)

    # Different date excludes the evidence entry from matching
    hits_wrong_date = search.search(
        populated_vault, "plain SQLite storage 2026-09-10", all_projects=True
    )
    wrong_evidence = [h.path for h in hits_wrong_date if h.collection == "evidence"]
    assert not wrong_evidence


def test_search_relative_temporal_filtering_evidence(populated_vault: Config) -> None:
    """Relative temporal queries filter evidence by calculated start/end dates."""
    # Entry timestamp is 2026-09-28
    now_same_day = datetime(2026, 9, 28, 14, 0, tzinfo=UTC)
    hits_today = search.search(
        populated_vault, "plain SQLite storage today", all_projects=True, now=now_same_day
    )
    today_evidence = [h.path for h in hits_today if h.collection == "evidence"]
    assert any("s-widget-1" in p for p in today_evidence)

    # Yesterday relative to 2026-09-28 is 2026-09-27, so 2026-09-28 entry is excluded
    hits_yesterday = search.search(
        populated_vault, "plain SQLite storage yesterday", all_projects=True, now=now_same_day
    )
    yesterday_evidence = [h.path for h in hits_yesterday if h.collection == "evidence"]
    assert not yesterday_evidence


def test_search_since_temporal_filtering(populated_vault: Config) -> None:
    """Since expressions filter evidence with time >= start without an upper bound."""
    # Entry timestamp is 2026-09-28
    hits_since_before = search.search(
        populated_vault, "plain SQLite storage since 2026-09-25", all_projects=True
    )
    matching = [h.path for h in hits_since_before if h.collection == "evidence"]
    assert any("s-widget-1" in p for p in matching)

    hits_since_after = search.search(
        populated_vault, "plain SQLite storage since 2026-09-29", all_projects=True
    )
    non_matching = [h.path for h in hits_since_after if h.collection == "evidence"]
    assert not non_matching


def test_search_command_temporal_cli(populated_vault: Config, capsys) -> None:
    """search_command strips temporal expressions and formats results properly."""
    now = datetime(2026, 9, 28, 14, 0, tzinfo=UTC)
    ret = search.search_command(populated_vault, "titanium bolt past week", now=now)
    assert ret == 0
    out = capsys.readouterr().out
    assert "projects/widget/assembly.md" in out


def test_multi_term_query_matches_all_terms_before_or_fallback(populated_vault: Config) -> None:
    """A multi-term query tries AND first across collections, falling back to OR only if no matches.

    When matching documents contain all query terms, partial matches matching only some terms
    are not returned. When no document contains all terms, the query falls back to OR so partial
    matches answer.
    """
    cfg = populated_vault
    # Document 'projects/widget/assembly.md' has both 'titanium' and 'bolt'.
    # Add a page that only contains 'titanium' (without 'bolt').
    (cfg.vault / "projects/widget/fasteners.md").write_text(
        "---\n"
        "type: Project\n"
        "title: Fasteners\n"
        "description: Fastener overview.\n"
        "aft:\n  project: p_widget\n"
        "---\n\n"
        "# Fasteners\n\n"
        "## Facts\n\n"
        "- Titanium rivets provide lightweight structural joints. [observed] 2026-09-24 [^s1]\n",
        encoding="utf-8",
    )
    index.update(cfg)

    # 1. Multi-term query where conjunction matches:
    # "titanium bolt" matches assembly.md (which has both words).
    # fasteners.md only has "titanium", so it is excluded because AND succeeded.
    hits = search.search(cfg, "titanium bolt", project="widget")
    hit_paths = [h.path for h in hits]
    assert "projects/widget/assembly.md" in hit_paths
    assert "projects/widget/fasteners.md" not in hit_paths

    # 2. Multi-term query where conjunction fails:
    # "rivets bolt" has no document matching both words.
    # Conjunction returns nothing, so it falls back to OR, finding both pages.
    fallback_hits = search.search(cfg, "rivets bolt", project="widget")
    fallback_paths = [h.path for h in fallback_hits]
    assert "projects/widget/assembly.md" in fallback_paths
    assert "projects/widget/fasteners.md" in fallback_paths


def test_a_strong_evidence_match_outranks_a_weak_page_match(populated_vault: Config) -> None:
    """F79: inside a tier the BM25 score decides across collections, so an evidence entry
    that matches strongly outranks a page whose match is weak, curated or not."""
    cfg = populated_vault
    now = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    entry = Entry("e-0", now, "user", "verdigris verdigris patina", False)
    evidence.store(cfg, "claude", "s-verd-1", "p_widget", [entry])
    (cfg.vault / "projects/widget/verdigris-notes.md").write_text(
        "---\n"
        "title: Stone Notes\n"
        "description: A page about the stone.\n"
        "aft:\n  project: p_widget\n"
        "---\n"
        "# Stone Notes\n"
        "## State\n"
        "- The verdigris line sits in a long body of filler words here to stretch the "
        "document length well past the average row so the term frequency part shrinks and "
        "the score magnitude falls with it. [observed] 2026-09-28 [^s1]\n",
        encoding="utf-8",
    )
    index.update(cfg)

    hits = search.search(cfg, "verdigris", current="widget", limit=10)
    paths = [hit.path for hit in hits]
    assert paths == [
        "aft://session/claude/s-verd-1#e-0",
        "projects/widget/verdigris-notes.md",
    ], f"the strong evidence match answers first: {paths}"
    page_hit = next(hit for hit in hits if hit.collection == index.PAGES)
    evidence_hit = next(hit for hit in hits if hit.collection == index.EVIDENCE)
    # The page's raw score sits outside the near-tie range, so the curated boost cannot
    # flip the order: |page| < |evidence| / CURATED_BOOST.
    assert abs(page_hit.rank) < abs(evidence_hit.rank) / search.CURATED_BOOST, (
        "the weak page match must sit outside the near-tie range"
    )


def test_a_page_within_ten_percent_of_evidence_outranks_it(populated_vault: Config) -> None:
    """F79: a curated row wins near-ties: its score is multiplied by CURATED_BOOST, which
    puts it ahead of a raw row whose score is within 10% of its own."""
    cfg = populated_vault
    now = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    entry = Entry("e-0", now, "user", "malachite malachite patina", False)
    evidence.store(cfg, "claude", "s-mal-1", "p_widget", [entry])
    (cfg.vault / "projects/widget/malachite-notes.md").write_text(
        "---\n"
        "title: Stone Notes\n"
        "description: A page about the stone.\n"
        "aft:\n  project: p_widget\n"
        "---\n"
        "# Stone Notes\n"
        "## State\n"
        "- The malachite line sits here on the bench. [observed] 2026-09-28 [^s1]\n",
        encoding="utf-8",
    )
    index.update(cfg)

    hits = search.search(cfg, "malachite", current="widget", limit=10)
    paths = [hit.path for hit in hits]
    assert paths == [
        "projects/widget/malachite-notes.md",
        "aft://session/claude/s-mal-1#e-0",
    ], f"the page in the near-tie range answers first: {paths}"
    page_hit = next(hit for hit in hits if hit.collection == index.PAGES)
    evidence_hit = next(hit for hit in hits if hit.collection == index.EVIDENCE)
    # The near-tie holds: the raw evidence row still scores ahead, by less than the boost
    # widens the page's score, so the boosted page answers first.
    assert abs(page_hit.rank) < abs(evidence_hit.rank), (
        "the raw evidence row must still score ahead before the boost"
    )
    assert abs(page_hit.rank) > abs(evidence_hit.rank) / search.CURATED_BOOST, (
        "the page's raw score must sit inside the near-tie range"
    )


def test_current_project_row_and_global_page_interleave_by_score(
    populated_vault: Config,
) -> None:
    """F79: the current project's rows and the global pages share tier 0, so the BM25 score
    decides between them whichever way it falls."""
    cfg = populated_vault
    (cfg.vault / "global/amazonite.md").write_text(
        "---\n"
        "title: Amazonite\n"
        "description: The stone shelf.\n"
        "---\n"
        "# Amazonite\n"
        "## State\n"
        "- The amazonite shelf holds the amazonite tiles. [observed] 2026-09-28 [^s1]\n"
        "- The beryl shelf holds one row of tiles. [observed] 2026-09-28 [^s1]\n",
        encoding="utf-8",
    )
    (cfg.vault / "projects/widget/amazonite.md").write_text(
        "---\n"
        "title: Amazonite\n"
        "description: The stone shelf.\n"
        "aft:\n  project: p_widget\n"
        "---\n"
        "# Amazonite\n"
        "## State\n"
        "- The amazonite line sits in a longer body of filler words here to stretch the row "
        "and lower its score. [observed] 2026-09-28 [^s1]\n"
        "- The beryl beryl shelf holds its tiles. [observed] 2026-09-28 [^s1]\n",
        encoding="utf-8",
    )
    index.update(cfg)

    # The global page carries "amazonite" twice and the project's row once, so the global
    # page answers first on the score.
    hits = search.search(cfg, "amazonite", current="widget", limit=10)
    paths = [hit.path for hit in hits]
    assert paths == ["global/amazonite.md", "projects/widget/amazonite.md"], paths
    assert abs(hits[0].rank) > abs(hits[1].rank), "the global row outscores the project's"

    # The project's row carries "beryl" twice and the global page once, so it answers
    # first on the score the other way.
    hits = search.search(cfg, "beryl", current="widget", limit=10)
    paths = [hit.path for hit in hits]
    assert paths == ["projects/widget/amazonite.md", "global/amazonite.md"], paths
    assert abs(hits[0].rank) > abs(hits[1].rank), "the project's row outscores the global"


def test_another_projects_row_ranks_after_every_tier0_row(populated_vault: Config) -> None:
    """F79: tier 1 still ranks after all of tier 0: another project's row that outscores
    every tier-0 row answers behind them whatever its score."""
    cfg = populated_vault
    (cfg.vault / "projects/widget/sodalite.md").write_text(
        "---\n"
        "title: Stone Bin\n"
        "description: The stone bin.\n"
        "aft:\n  project: p_widget\n"
        "---\n"
        "# Sodalite\n"
        "## State\n"
        "- The sodalite bin holds one tray. [observed] 2026-09-28 [^s1]\n",
        encoding="utf-8",
    )
    (cfg.vault / "global/sodalite.md").write_text(
        "---\n"
        "title: Stone Bin\n"
        "description: The stone bin.\n"
        "---\n"
        "# Sodalite\n"
        "## State\n"
        "- The sodalite bin holds one tray as well. [observed] 2026-09-28 [^s1]\n",
        encoding="utf-8",
    )
    (cfg.vault / "projects/gadget/sodalite.md").write_text(
        "---\n"
        "title: Stone Bin\n"
        "description: The stone bin.\n"
        "aft:\n  project: p_gadget\n"
        "---\n"
        "# Sodalite\n"
        "## State\n"
        "- The sodalite sodalite sodalite bin holds every tray the workshop has."
        " [observed] 2026-09-28 [^s1]\n",
        encoding="utf-8",
    )
    index.update(cfg)

    hits = search.search(cfg, "sodalite", current="widget", limit=10)
    paths = [hit.path for hit in hits]
    assert set(paths[:2]) == {"projects/widget/sodalite.md", "global/sodalite.md"}, paths
    assert paths[-1] == "projects/gadget/sodalite.md", paths
    gadget_hit = next(hit for hit in hits if hit.project == "gadget")
    for other in hits[:2]:
        assert abs(gadget_hit.rank) > abs(other.rank), (
            "the other project's row outscores the tier-0 rows it answers behind"
        )


def test_narrowed_search_puts_global_and_project_rows_on_one_tier(
    populated_vault: Config,
) -> None:
    """F79: with --project the narrowed query takes the project's rows and the global pages
    as one tier, so a global page that outscores the project's row answers first."""
    cfg = populated_vault
    (cfg.vault / "global/lapis.md").write_text(
        "---\n"
        "title: Lapis\n"
        "description: The stone drawer.\n"
        "---\n"
        "# Lapis\n"
        "## State\n"
        "- The lapis drawer holds the lapis tiles. [observed] 2026-09-28 [^s1]\n",
        encoding="utf-8",
    )
    (cfg.vault / "projects/widget/lapis.md").write_text(
        "---\n"
        "title: Lapis\n"
        "description: The stone drawer.\n"
        "aft:\n  project: p_widget\n"
        "---\n"
        "# Lapis\n"
        "## State\n"
        "- The lapis drawer holds one tray of tiles. [observed] 2026-09-28 [^s1]\n",
        encoding="utf-8",
    )
    index.update(cfg)

    hits = search.search(cfg, "lapis", project="widget", limit=10)
    paths = [hit.path for hit in hits]
    assert paths == ["global/lapis.md", "projects/widget/lapis.md"], paths
    assert abs(hits[0].rank) > abs(hits[1].rank), "the global row outscores the project's"


def test_raw_evidence_results_collapsed_per_session(populated_vault: Config) -> None:
    """Raw evidence results are collapsed per session so multiple hits from one session
    do not flood results.
    """
    cfg = populated_vault
    now = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    # Store multiple entries in a single session all matching the same query terms
    entries = [
        Entry("e-0", now, "user", "telemetry probe calibration step 1", False),
        Entry("e-1", now, "assistant", "telemetry probe calibration step 2", False),
        Entry("e-2", now, "user", "telemetry probe calibration step 3", False),
        Entry("e-3", now, "assistant", "telemetry probe calibration verified", False),
    ]
    evidence.store(cfg, "claude", "s-telemetry-1", "p_widget", entries)

    # Store another session with matching entries
    entries2 = [
        Entry("e-0", now, "user", "telemetry probe calibration alternate method", False),
        Entry("e-1", now, "assistant", "telemetry probe calibration confirmed", False),
    ]
    evidence.store(cfg, "claude", "s-telemetry-2", "p_widget", entries2)
    index.update(cfg)

    hits = search.search(cfg, "telemetry probe calibration", project="widget", limit=10)
    evidence_hits = [h for h in hits if h.collection == index.EVIDENCE]

    # Exactly 2 evidence hits: one for s-telemetry-1 and one for s-telemetry-2
    assert len(evidence_hits) == 2, f"expected 2 collapsed hits, got {len(evidence_hits)}"
    sessions_found = {h.path.split("#")[0] for h in evidence_hits}
    assert len(sessions_found) == 2, f"each hit must come from a distinct session: {sessions_found}"


def test_a_pages_matching_line_outranks_a_long_sections_weaker_match_from_another_page(
    populated_vault: Config,
) -> None:
    """F82: with one index row per page line, a page's matching line is not penalized by
    section length, outranking a long section's weaker match from another page."""
    cfg = populated_vault

    page_a = """---
type: Project
title: Target Project
description: Target project facts.
sources: []
aft:
  project: p_widget
---

# Target

## Facts

- firstmate is cloned under the home directory [observed] 2026-10-01 [^s1]
"""
    (cfg.vault / "projects/widget/target.md").write_text(page_a, encoding="utf-8")

    distractor_text = " ".join(
        f"Unrelated telemetry entry number {i} was recorded during the routine run."
        for i in range(1, 40)
    )
    page_b = f"""---
type: Session Digest
title: Distractor Digest
description: Distractor session.
aft:
  project: p_widget
---

# Distractor

## Summary

{distractor_text}
A worker repository was cloned for test runs.
"""
    digest_dir = cfg.vault / "digests/widget/2026/09"
    digest_dir.mkdir(parents=True, exist_ok=True)
    (digest_dir / "2026-09-28-claude-distractor.md").write_text(page_b, encoding="utf-8")

    index.update(cfg)

    hits = search.search(cfg, "cloned", project="widget", limit=5)
    assert len(hits) >= 2
    assert hits[0].path == "projects/widget/target.md"
    assert "firstmate is cloned" in hits[0].snippet


def test_one_page_answers_once_per_query_at_its_best_line(
    populated_vault: Config,
) -> None:
    """F82: a page answers once per query at its best line (per-path de-duplication),
    and its snippet is drawn from that line."""
    cfg = populated_vault

    page_text = """---
type: Project
title: Multi Line Page
description: Multiple matching lines in one page.
sources: []
aft:
  project: p_widget
---

# Multi

## Decisions

- Titanium brackets are used for high stress mounts [stated] 2026-09-01 [^s1]
- Secondary titanium brackets secure the payload [observed] 2026-09-02 [^s2]

## State

- The titanium frame passed stress testing [observed] 2026-09-03 [^s3]
"""
    (cfg.vault / "projects/widget/multi.md").write_text(page_text, encoding="utf-8")
    index.update(cfg)

    hits = search.search(cfg, "brackets", project="widget", limit=10)
    matching_paths = [h.path for h in hits if h.path == "projects/widget/multi.md"]
    assert len(matching_paths) == 1, f"expected page to answer once, got {matching_paths}"
    multi_hit = next(h for h in hits if h.path == "projects/widget/multi.md")
    assert multi_hit.heading == "Decisions"
    assert "brackets" in multi_hit.snippet.lower()
