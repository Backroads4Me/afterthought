from __future__ import annotations

import dataclasses
import shutil
import time
from pathlib import Path

import pytest

from aft import inject, keys
from aft.cli import main
from aft.config import Config, InjectConfig
from aft.gitutil import normalize_remote
from aft.registry import Registry, path_key
from conftest import make_repo, run_git

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
- ~~The daemon runs on systemd.~~ [observed] 2026-09-05 [^s1]
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

NOTE_PAGE = """\
---
type: Project
title: Widget Note {number:02d}
description: Notes filed for the widget project.
sources: []
aft:
  project: p_widget
---

## Facts

- Note {number:02d} records one line about the widget. [observed] 2026-09-30 [^s1]
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

# A digest page as M2.6 writes it, with the instant the digest step filed it under
# ``generated.at``.
STAMPED_DIGEST = """\
---
type: Session Digest
title: Widget session {date}
description: Digest of the widget session on {date}.
sources: []
generated:
  by: aft 0.0.0 / test-model
  at: {at}
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

- For sizes use [[widget-spec]]. (waiting on the user)

## Preferences

- Prefer short commits.

## Pages

{PAGES}

Search memory: aft search "<words>". Read a page: aft read <path>."""


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def write_pages(vault: Path, slug: str, count: int) -> None:
    """The project's own pages, one file each, as a long-lived project accumulates them."""
    for number in range(count):
        write(vault / f"projects/{slug}/note{number:02d}.md", NOTE_PAGE.format(number=number))


def parts_of(block: str) -> dict[str, list[str]]:
    """The block as it emits its parts: the lines under each ``##`` heading, the lines
    before the first heading under ``""``, and the closing line as the part it is."""
    groups: dict[str, list[str]] = {"": []}
    heading = ""
    for line in block.splitlines():
        if line.startswith("## ") or line == inject.HOW_TO:
            heading = line
            groups.setdefault(heading, [])
        if line.strip() and not line.startswith("## "):
            groups[heading].append(line)
    return groups


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
        "The daemon runs on systemd.",  # struck through with no reason recorded
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
    complete = parts_of(full)
    cut = f"## {inject.CUT_HEADING}"
    for cap in range(0, len(full) + 1, 17):
        capped = dataclasses.replace(
            widget, settings=dataclasses.replace(widget.settings, inject=InjectConfig(cap))
        )
        block = inject.build(capped, resolution)
        assert len(block) <= cap, f"cap {cap} gave {len(block)} characters"
        parts = parts_of(block)
        for heading, under in parts.items():
            if heading == cut:
                assert under == [inject.CUT_NOTE], f"cap {cap} reported {under}"
                continue
            # A line longer than the cap cannot be shown at all, so it is skipped; the
            # rest of a layer is trimmed off its end, whole.
            shown = [line for line in complete[heading] if len(line) <= cap]
            assert under == shown[: len(under)], f"cap {cap} cut into {heading or 'the title'}"
            assert not heading.startswith("## ") or under, f"cap {cap} left {heading} empty"
        hidden = [
            heading for heading in complete if heading.startswith("## ") and heading not in parts
        ]
        reported = cut in parts or len(block) + 2 + len(inject.CUT_NOTE) > cap
        assert not hidden or reported, f"cap {cap} dropped {hidden} without saying so"
        assert inject.HOW_TO in parts or len(block) + 2 + len(inject.HOW_TO) > cap


def test_a_long_overview_leaves_room_for_the_layers_after_it(widget, tmp_path):
    """The project's own layer gets its floor plus half of what the floors leave, so a
    long overview still leaves the preferences, the page map and the search and read
    lines in place."""
    project = Registry.load(widget.registry_path).by_slug("widget")
    facts = "\n".join(
        f"- Fact {number} is long enough that the overview alone fills the block."
        f" [observed] 2026-09-30 [^s1]"
        for number in range(200)
    )
    write(
        widget.vault / "projects/widget/overview.md",
        "---\ntype: Project\ntitle: Widget\n"
        "description: A widget that turns water into widgets.\nsources: []\n"
        f"aft:\n  project: {project.id}\n---\n\n## Facts\n\n{facts}\n",
    )
    block = inject.build(widget, resolve(widget, tmp_path / "widget"))
    assert len(block) <= widget.settings.inject.max_chars
    parts = parts_of(block)
    assert parts["## Preferences"] == ["- Prefer short commits."]
    assert parts["## Pages"] == PAGES.splitlines()
    assert parts[inject.HOW_TO] == [inject.HOW_TO]
    assert 1 < len(parts["## Facts"]) < 200


def test_a_long_preferences_page_cannot_bury_the_page_map(widget, tmp_path):
    """Dreaming promotes into global/preferences.md without end, so the page map and the
    closing line keep their room before the fill runs rather than after it."""
    project = Registry.load(widget.registry_path).by_slug("widget")
    facts = "\n".join(
        f"- Fact {number} says something short about the project. [observed] 2026-09-30 [^s1]"
        for number in range(20)
    )
    write(
        widget.vault / "projects/widget/overview.md",
        "---\ntype: Project\ntitle: Widget\n"
        "description: A widget that turns water into widgets.\nsources: []\n"
        f"aft:\n  project: {project.id}\n---\n\n## Facts\n\n{facts}\n",
    )
    preferences = "\n".join(
        f"- Preference {number} was promoted from an earlier session. [stated] 2026-09-01 [^s1]"
        for number in range(150)
    )
    write(
        widget.vault / "global/preferences.md",
        "---\ntype: Preferences\ntitle: Preferences\n"
        "description: Your preferences that apply in every project.\nsources: []\n"
        f"---\n\n## Preferences\n\n{preferences}\n",
    )
    block = inject.build(widget, resolve(widget, tmp_path / "widget"))
    assert len(block) <= widget.settings.inject.max_chars
    parts = parts_of(block)
    assert parts["## Pages"] == PAGES.splitlines()
    assert parts[inject.HOW_TO] == [inject.HOW_TO]
    assert len(parts["## Facts"]) == 20
    assert 1 < len(parts["## Preferences"]) < 150


def test_a_hundred_pages_cannot_starve_the_project_layers(widget, tmp_path):
    """A project accumulates page files without end, so the map stops at its share of the
    block and the layers that carry the project's identity keep their room."""
    write_pages(widget.vault, "widget", 100)
    block = inject.build(widget, resolve(widget, tmp_path / "widget"))
    assert len(block) <= widget.settings.inject.max_chars
    parts = parts_of(block)
    assert parts[""] == [
        "# Memory: Widget",
        "A widget that turns water into widgets.",
    ]
    assert parts["## State"] == ["- The daemon listens on 8123. (disputed)"]
    assert parts["## Facts"] == ["- The widget is round."]
    assert parts["## Preferences"] == ["- Prefer short commits."]
    assert parts[inject.HOW_TO] == [inject.HOW_TO]


def test_a_full_map_and_a_full_overview_still_carry_every_layer(widget, tmp_path):
    """The two layers that grow with the vault spend only what the fixed costs and the
    floors leave, so they cannot jointly bury the preferences layer, the title, or the
    read line."""
    project = Registry.load(widget.registry_path).by_slug("widget")
    facts = "\n".join(
        f"- Fact {number} is long enough that the overview alone fills the block."
        f" [observed] 2026-09-30 [^s1]"
        for number in range(200)
    )
    write(
        widget.vault / "projects/widget/overview.md",
        "---\ntype: Project\ntitle: Widget\n"
        "description: A widget that turns water into widgets.\nsources: []\n"
        f"aft:\n  project: {project.id}\n---\n\n## Facts\n\n{facts}\n",
    )
    preferences = "\n".join(
        f"- Preference {number} was promoted from an earlier session. [stated] 2026-09-01 [^s1]"
        for number in range(150)
    )
    write(
        widget.vault / "global/preferences.md",
        "---\ntype: Preferences\ntitle: Preferences\n"
        "description: Your preferences that apply in every project.\nsources: []\n"
        f"---\n\n## Preferences\n\n{preferences}\n",
    )
    write_pages(widget.vault, "widget", 100)
    resolution = resolve(widget, tmp_path / "widget")
    roomy = dataclasses.replace(
        widget, settings=dataclasses.replace(widget.settings, inject=InjectConfig(60_000))
    )
    uncapped = parts_of(inject.build(roomy, resolution))
    block = inject.build(widget, resolution)
    assert len(block) <= widget.settings.inject.max_chars
    parts = parts_of(block)
    assert parts[""] == ["# Memory: Widget", "A widget that turns water into widgets."]
    assert parts["## Last session"] == uncapped["## Last session"]
    assert parts["## Preferences"]
    assert parts["## Preferences"] == uncapped["## Preferences"][: len(parts["## Preferences"])]
    assert parts[inject.HOW_TO] == [inject.HOW_TO]
    for heading in ("## Pages", "## Facts"):
        assert len(parts[heading]) < len(uncapped[heading])
        assert parts[heading] == uncapped[heading][: len(parts[heading])]


def test_one_over_long_line_costs_only_itself(widget, tmp_path):
    """A fact statement or a page description longer than the whole budget leaves out
    its own line alone: the layers around it keep their room, so the session still gets
    the page map, the preferences, and the line telling it how to search."""
    project = Registry.load(widget.registry_path).by_slug("widget")
    facts = "\n".join(
        [f"- {'x' * 12_000} [observed] 2026-09-30 [^s1]"]
        + [
            f"- Fact {number} is short enough to show. [observed] 2026-09-30 [^s1]"
            for number in range(3)
        ]
    )
    write(
        widget.vault / "projects/widget/overview.md",
        "---\ntype: Project\ntitle: Widget\n"
        "description: A widget that turns water into widgets.\nsources: []\n"
        f"aft:\n  project: {project.id}\n---\n\n## Facts\n\n{facts}\n",
    )
    write(
        widget.vault / "projects/widget/api.md",
        "---\ntype: Project\ntitle: Widget API\nsources: []\n"
        f"description: >-\n  {'y' * 9_000}\n"
        "aft:\n  project: p_widget\n---\n\n## Facts\n\n"
        "- The API is versioned by path. [stated] 2026-09-18 [^s1]\n",
    )
    block = inject.build(widget, resolve(widget, tmp_path / "widget"))
    assert len(block) <= widget.settings.inject.max_chars
    parts = parts_of(block)
    assert parts[""] == ["# Memory: Widget", "A widget that turns water into widgets."]
    assert parts["## Preferences"] == ["- Prefer short commits."]
    assert parts["## Pages"] == [PAGES.splitlines()[1]]
    assert parts["## Facts"] == [f"- Fact {number} is short enough to show." for number in range(3)]
    assert parts[inject.HOW_TO] == [inject.HOW_TO]


def test_a_budget_below_the_floors_reports_the_cut(widget, tmp_path):
    """A cap below what the layers need keeps the title and says plainly that the rest
    was cut, so a mis-set inject.max_chars never leaves a session with memory quietly
    missing."""
    tight = dataclasses.replace(
        widget, settings=dataclasses.replace(widget.settings, inject=InjectConfig(300))
    )
    block = inject.build(tight, resolve(widget, tmp_path / "widget"))
    parts = parts_of(block)
    assert len(block) <= 300
    assert block.startswith("# Memory: Widget")
    assert parts[f"## {inject.CUT_HEADING}"] == [inject.CUT_NOTE]
    assert "## Pages" not in parts


def test_an_over_grown_page_map_drops_whole_pointer_lines(widget, tmp_path):
    """A map over its share loses whole pointers from the end, so the last pointer the
    session is given is complete and the read line still has its own room."""
    write_pages(widget.vault, "widget", 100)
    resolution = resolve(widget, tmp_path / "widget")
    roomy = dataclasses.replace(
        widget,
        settings=dataclasses.replace(widget.settings, inject=InjectConfig(20_000)),
    )
    whole = parts_of(inject.build(roomy, resolution))["## Pages"]
    block = inject.build(widget, resolution)
    kept = parts_of(block)["## Pages"]
    assert len(whole) == 102  # the api page, a hundred notes, and the collection
    assert len(kept) < len(whole)
    assert kept == whole[: len(kept)]
    assert len("## Pages\n\n" + "\n".join(kept)) <= widget.settings.inject.max_chars // 2
    assert block.endswith(inject.HOW_TO)


def test_a_digest_in_an_unpadded_month_still_loses_to_a_later_one(widget, tmp_path):
    """M2.6 files a digest at ``digests/<slug>/<yyyy>/<mm>/``; the month directory
    is read as a number, so a ``9`` cannot sort a September handoff above October."""
    write(
        widget.vault / "digests/widget/2026/9/2026-09-30-pi-old.md",
        DIGEST.format(date="2026-09-30", in_flight="the September draft"),
    )
    block = inject.build(widget, resolve(widget, tmp_path / "widget"))
    assert "- In flight: the block" in block
    assert "the September draft" not in block


@pytest.mark.parametrize("session", ["one", "550e8400-e29b-41d4-a716-446655440000"])
def test_the_highest_part_of_the_newest_day_supplies_the_handoff(widget, tmp_path, session):
    """A session digested more than once in a day files ``-2``, ``-3`` beside its first
    page, and the last session's handoff is the highest part's, whatever order the
    directory lists them in and even when the session id itself ends in digits."""
    day = widget.vault / "digests/widget/2026/10"
    for suffix, in_flight in [
        ("-3", "the third part"),
        ("", "the first part"),
        ("-2", "the second"),
    ]:
        write(
            day / f"2026-10-06-pi-{session}{suffix}.md",
            DIGEST.format(date="2026-10-06", in_flight=in_flight),
        )
    block = inject.build(widget, resolve(widget, tmp_path / "widget"))
    assert "- In flight: the third part" in block
    for earlier in ("the first part", "the second", "the block"):
        assert earlier not in block


@pytest.mark.parametrize(
    "stamp",
    ["'2026-10-07T22:30:00Z'", "2026-10-07T22:30:00"],
    ids=["a quoted utc stamp", "an unquoted stamp with no offset"],
)
def test_the_digest_written_last_supplies_the_handoff(widget, tmp_path, stamp):
    """Two sessions digested on one day: the one whose name sorts first ran on last, and
    the ``generated.at`` its page carries is what says so."""
    day = widget.vault / "digests/widget/2026/10"
    write(
        day / "2026-10-07-pi-aardvark.md",
        STAMPED_DIGEST.format(date="2026-10-07", at=stamp, in_flight="the closing edit"),
    )
    write(
        day / "2026-10-07-pi-zebra.md",
        STAMPED_DIGEST.format(
            date="2026-10-07",
            at="'2026-10-07T14:05:00Z'",
            in_flight="the abandoned draft",
        ),
    )
    block = inject.build(widget, resolve(widget, tmp_path / "widget"))
    assert "- In flight: the closing edit" in block
    for earlier in ("the abandoned draft", "the block"):
        assert earlier not in block


def test_a_frontmatter_block_scalar_stays_on_one_line(widget, tmp_path):
    """A page description or handoff note written as a YAML block reaches the session
    as one line per page and one line per handoff item."""
    write(
        widget.vault / "projects/widget/api.md",
        "---\ntype: Project\ntitle: Widget API\nsources: []\n"
        "description: |-\n  How to call a widget.\n  Over HTTP, versioned by path.\n"
        "aft:\n  project: p_widget\n---\n\n## Facts\n\n"
        "- The API is versioned by path. [stated] 2026-09-18 [^s1]\n",
    )
    write(
        widget.vault / "digests/widget/2026/11/2026-11-01-pi-one.md",
        "---\ntype: Session Digest\ntitle: Widget session\n"
        "description: Digest of the widget session on 2026-11-01.\nsources: []\n"
        "aft:\n  project: p_widget\n  handoff:\n"
        "    in_flight: |-\n      the injection block\n      and its tests\n"
        "    open:\n      - |-\n        the pi extension\n        and its hooks\n---\n",
    )
    block = inject.build(widget, resolve(widget, tmp_path / "widget"))
    lines = block.splitlines()
    assert (
        "- Widget API: How to call a widget. Over HTTP, versioned by path."
        " (aft read projects/widget/api.md)"
    ) in lines
    assert "- In flight: the injection block and its tests" in lines
    assert "- Open: the pi extension and its hooks" in lines


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
    assert block.startswith("# Memory: ")
    assert inject.HOW_TO in block
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


def test_injection_writes_nothing_even_when_it_resolves_by_remote(
    widget, tmp_path, capsys, monkeypatch
):
    """Reads files only: the alias ``aft key`` records for a re-clone is not recorded
    here, so the registry and the vault's history stay exactly as they were."""
    origin = "git@github.com:Example/Widget.git"
    registry = Registry.load(widget.registry_path)
    registry.add_remote(registry.by_slug("widget"), normalize_remote(origin))
    registry.save()
    registry_before = widget.registry_path.read_bytes()
    log_before = run_git(["log", "--format=%H"], widget.vault)
    status_before = run_git(["status", "--porcelain"], widget.vault)
    clone = make_repo(tmp_path / "re-clone", origin=origin)
    monkeypatch.setenv("AFT_HOME", str(widget.home))
    assert main(["inject", str(clone)]) == 0
    assert capsys.readouterr().out == EXPECTED + "\n"
    assert widget.registry_path.read_bytes() == registry_before
    assert run_git(["log", "--format=%H"], widget.vault) == log_before
    assert run_git(["status", "--porcelain"], widget.vault) == status_before
