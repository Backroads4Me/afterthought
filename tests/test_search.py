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
    """The plain-text line names where the hit came from as well as what matched: document,
    collection, section, description, snippet, tab-separated on one line (DESIGN 10 asks a result
    to name its document and its section, and a hit from the archive reads differently once the
    line says which collection it came from)."""
    ret = main(["search", "titanium", "--all", "--limit", "5"])
    assert ret == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) >= 1
    for line in lines:
        assert len(line.split("\t")) == 5
    path, collection, heading, description, snippet = lines[0].split("\t")
    assert path == "projects/widget/assembly.md"
    assert collection == "pages"
    assert heading == "State"
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


# ------------------------------------------- scoping: an unfiled row is no project's memory

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
    """`aft search` defaults to the current project plus the global pages (BUILD-PLAN M3.2), and
    only a page row's empty project means global (DESIGN 6). An entry whose session resolved to no
    project and a digest under a slug the registry has never heard of are text that was never
    filed, so neither belongs in widget's results - while ``--all`` still reaches them."""
    cfg = populated_vault
    plant_unfiled(cfg)

    everywhere = {hit.path for hit in search.search(cfg, "zirconium", all_projects=True, limit=20)}
    assert "digests/scratchdir/2026/09/2026-09-28-pi-s-scratch.md" in everywhere
    assert "aft://session/pi/s-unreg#e-7" in everywhere, "both unfiled rows are indexed"

    assert search.search(cfg, "zirconium", project="widget") == []


def test_search_from_outside_every_project_reaches_only_the_shared_pages(
    populated_vault: Config, monkeypatch
) -> None:
    """From a working directory inside no project, the query is the person's whole memory minus
    what belongs to somebody's project: the shared pages answer, and unfiled conversation and
    digests do not sneak in because they carry no project either."""
    cfg = populated_vault
    plant_unfiled(cfg)
    outside = cfg.vault.parent / "unregistered"
    outside.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(outside)

    assert [hit.path for hit in search.search(cfg, "dark theme")] == ["global/preferences.md"]
    assert search.search(cfg, "zirconium") == []


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
    assert "aft://session/claude/s-widget-1#e-1" in paths, "the archived entry itself"
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
    collection, and which section of it (DESIGN 10 names a library result by its page or section,
    and there is no reason to make a person read the page to learn where a hit came from)."""
    assert main(["search", "titanium", "--all", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    (first,) = [hit for hit in data if hit["path"] == "projects/widget/assembly.md"]
    assert first["collection"] == "pages"
    assert first["heading"] == "State"


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
