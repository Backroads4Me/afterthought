from __future__ import annotations

import dataclasses
import shutil
import time
from pathlib import Path

import pytest

from aft import inject, keys
from aft.cli import main
from aft.config import Config, InjectConfig
from aft.registry import Registry, path_key
from conftest import make_repo

HOST = "testhost"

OVERVIEW = """\
---
type: Project
title: Widget
description: A widget that turns water into widgets.
sources: []
aft:
  project: p_widget
---

## References

- For sizes use [[widget-spec]]. [stated] 2026-09-13 [^s1]

> [!question] 2026-09-14
> The spec lists two bore sizes; which one ships?

## Facts

- The widget is round. [observed] 2026-09-11 [^s1]
- The widget is square. [observed] 2026-09-10 [^s1] (stale 2026-09-20: the round model replaced it)

## Procedures

- Release by tagging on main. [stated] 2026-09-15 [^s1]

## Lessons

- ~~Rebooting fixes the hang.~~ [observed] 2026-09-10 [^s1] withdrawn 2026-09-19
- A warm cache hides a missing index. [observed] 2026-09-12 [^s1]

## Decisions

- Storage is SQLite. [stated] 2026-09-28 [^s1]
- ~~Storage is plain files.~~ [suggested] 2026-09-20 [^s1] superseded 2026-09-28 by [^s1]

## State

- The daemon listens on 8123. [observed] 2026-09-30 [^s1]

> [!conflict] 2026-10-01
> One session says 8123 and another says 8124. The line above stands for now.

## Preferences

- Prefer plain hyphens in prose. [stated] 2026-09-16 [^s1]

## Candidates

- Longer widgets sell better. [inferred] 2026-09-17 [^s1] (signals 1/3, sessions 1/2)
"""

PREFERENCES = """\
---
type: Preferences
title: Preferences
description: Your preferences that apply in every project.
sources: []
---

## Preferences

- Prefer short commits. [stated] 2026-09-01 [^s1]
- ~~Prefer long commits.~~ [suggested] 2026-08-01 [^s1] superseded 2026-09-01 by [^s1]

## Candidates

- Prefer tabs. [inferred] 2026-09-02 [^s1] (signals 1/3, sessions 1/2)
"""

API_PAGE = """\
---
type: Project
title: Widget API
description: How to call a widget over HTTP.
sources: []
aft:
  project: p_widget
---

## Facts

- The API is versioned by path. [stated] 2026-09-18 [^s1]
"""

LISTING = """\
# Widget

- overview.md
- api.md
"""

COLLECTION = """\
---
type: Reference
title: Widget spec
description: Dimensions and tolerances for every widget.
sources: []
aft:
  projects:
    - {project_id}
---

## Contents

The spec covers every widget bore and thread.
"""

DIGEST = """\
---
type: Session Digest
title: Widget session {date}
description: Digest of the widget session on {date}.
sources: []
aft:
  project: p_widget
  handoff:
    in_flight: {in_flight}
    failed:
      - the parserless draft
    open:
      - the pi extension
---

## Facts

- The session wrote the injection block. [observed] {date} [^s1]
"""

PAGES = """\
- Widget API: How to call a widget over HTTP. (aft read projects/widget/api.md)
- Widget spec: Dimensions and tolerances for every widget. \
(aft read references/widget-spec/collection.md)"""

EXPECTED = f"""# Memory: Widget
A widget that turns water into widgets.

## Last session

- In flight: the block
- Failed: the parserless draft
- Open: the pi extension

## State

- The daemon listens on 8123. (disputed)

## Decisions

- Storage is SQLite.

## Lessons

- A warm cache hides a missing index.

## Procedures

- Release by tagging on main.

## Facts

- The widget is round.

## References

- For sizes use [[widget-spec]]. (waiting on Ted)

## Preferences

- Prefer short commits.

## Pages

{PAGES}

Search memory: aft search "<words>". Read a page: aft read <path>."""


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def widget(initialized: Config, tmp_path) -> Config:
    """A registered project with an overview, a second page, a collection and digests."""
    registry = Registry.load(initialized.registry_path)
    project = registry.add("widget", "Widget")
    registry.add_path(project, path_key(HOST, make_repo(tmp_path / "widget").resolve()))
    registry.save()
    vault = initialized.vault
    write(vault / "projects/widget/overview.md", OVERVIEW)
    write(vault / "projects/widget/api.md", API_PAGE)
    write(vault / "projects/widget/index.md", LISTING)
    write(vault / "global/preferences.md", PREFERENCES)
    write(
        vault / "references/widget-spec/collection.md",
        COLLECTION.format(project_id=project.id),
    )
    for date, in_flight in [("2026-09-30", "the older draft"), ("2026-10-05", "the block")]:
        year, month, _ = date.split("-")
        write(
            vault / f"digests/widget/{year}/{month}/{date}-pi-one.md",
            DIGEST.format(date=date, in_flight=in_flight),
        )
    return initialized


def resolve(config: Config, cwd: Path):
    return keys.resolve(cwd, Registry.load(config.registry_path), config.host)


def test_the_block_layers_in_design_order(widget, tmp_path):
    assert inject.build(widget, resolve(widget, tmp_path / "widget")) == EXPECTED


@pytest.mark.parametrize(
    "excluded",
    [
        "Longer widgets sell better.",  # a candidate
        "The widget is square.",  # stale
        "Storage is plain files.",  # superseded
        "Rebooting fixes the hang.",  # withdrawn
        "The session wrote the injection block.",  # a digest's facts, not its handoff
    ],
)
def test_excluded_states_stay_out(widget, tmp_path, excluded):
    block = inject.build(widget, resolve(widget, tmp_path / "widget"))
    assert excluded not in block


def test_preferences_come_from_the_global_page_only(widget, tmp_path):
    """The plan's layer 3 names the overview's injected sections and Preferences is not
    one of them; layer 4 reads ``global/preferences.md``."""
    block = inject.build(widget, resolve(widget, tmp_path / "widget"))
    assert "Prefer plain hyphens in prose." not in block
    assert "- Prefer short commits." in block


def test_the_newest_digest_supplies_the_handoff(widget, tmp_path):
    block = inject.build(widget, resolve(widget, tmp_path / "widget"))
    assert "- In flight: the block" in block
    assert "the older draft" not in block


def test_a_collection_shows_only_for_the_projects_it_lists(widget, tmp_path):
    collection = widget.vault / "references/widget-spec/collection.md"
    collection.write_text(COLLECTION.format(project_id="p_somewhere_else"))
    block = inject.build(widget, resolve(widget, tmp_path / "widget"))
    assert "references/widget-spec/collection.md" not in block
    assert "Widget API" in block


def test_the_pages_layer_points_at_every_page_but_the_overview_and_the_listing(widget, tmp_path):
    block = inject.build(widget, resolve(widget, tmp_path / "widget"))
    assert block.split("## Pages\n\n")[1].split("\n\nSearch memory")[0] == PAGES


def test_the_cap_holds_and_never_splits_a_line(widget, tmp_path):
    resolution = resolve(widget, tmp_path / "widget")
    full = inject.build(widget, resolution)
    lines = full.splitlines()
    for cap in range(0, len(full) + 1, 17):
        capped = dataclasses.replace(
            widget, settings=dataclasses.replace(widget.settings, inject=InjectConfig(cap))
        )
        block = inject.build(capped, resolution)
        assert len(block) <= cap
        assert full.startswith(block)
        assert block == "\n".join(lines[: len(block.splitlines())])


def test_an_unregistered_directory_gets_preferences_and_how_to(widget, tmp_path):
    outside = make_repo(tmp_path / "nowhere")
    resolution = resolve(widget, outside)
    assert resolution.kind == keys.UNREGISTERED
    assert inject.build(widget, resolution) == (
        "## Preferences\n\n- Prefer short commits.\n\n" + inject.HOW_TO
    )


def test_a_directory_with_no_project_gets_the_same_two_layers(widget, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    resolution = resolve(widget, plain)
    assert resolution.kind == keys.NONE
    assert resolution.slug is None
    assert inject.build(widget, resolution) == inject.build(
        widget, resolve(widget, make_repo(tmp_path / "nowhere"))
    )


def test_absent_pages_leave_the_block_with_what_exists(widget, tmp_path):
    shutil.rmtree(widget.vault / "digests")
    (widget.vault / "projects/widget/overview.md").unlink()
    (widget.vault / "global/preferences.md").unlink()
    assert inject.build(widget, resolve(widget, tmp_path / "widget")) == (
        "# Memory: Widget\n\n## Pages\n\n" + PAGES + "\n\n" + inject.HOW_TO
    )


def test_builds_50_pages_of_200_lines_in_under_150_ms(initialized, tmp_path):
    registry = Registry.load(initialized.registry_path)
    project = registry.add("wide", "Wide")
    registry.add_path(project, path_key(HOST, make_repo(tmp_path / "wide").resolve()))
    registry.save()
    for number in range(50):
        facts = "\n".join(
            f"- Fact {line} on page {number} is worth keeping. [observed] 2026-09-30 [^s1]"
            for line in range(200)
        )
        name = "overview" if number == 0 else f"page{number:02d}"
        write(
            initialized.vault / f"projects/wide/{name}.md",
            f"---\ntype: Project\ntitle: Page {number}\n"
            f"description: What page {number} is about.\nsources: []\n"
            f"aft:\n  project: {project.id}\n---\n\n## Facts\n\n{facts}\n",
        )
    resolution = resolve(initialized, tmp_path / "wide")
    inject.build(initialized, resolution)  # warm the file cache, then time one build
    started = time.perf_counter()
    block = inject.build(initialized, resolution)
    elapsed = time.perf_counter() - started
    assert block.count("\n- Fact ") > 1
    assert len(block) <= initialized.settings.inject.max_chars
    assert elapsed < 0.15, f"the block took {elapsed * 1000:.0f} ms to build"


def test_inject_prints_the_block(widget, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("AFT_HOME", str(widget.home))
    assert main(["inject", str(tmp_path / "widget")]) == 0
    assert capsys.readouterr().out == EXPECTED + "\n"


def test_inject_prints_what_it_has_elsewhere(widget, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("AFT_HOME", str(widget.home))
    plain = tmp_path / "plain"
    plain.mkdir()
    assert main(["inject", str(plain)]) == 0
    assert capsys.readouterr().out == (
        "## Preferences\n\n- Prefer short commits.\n\n" + inject.HOW_TO + "\n"
    )
