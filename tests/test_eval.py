"""`aft eval`: a golden set of known answers, run over the two paths recall takes.

The fixture vault holds two projects, so a fact belonging to one can be shown not to reach
the other; one project's memory sits deeper than its block; one claim was superseded; and
there is one dream report, which is what makes the negative cases mean something rather
than merely find nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aft import eval as evalmod
from aft import inject, keys, model, search
from aft.cli import main
from aft.config import Config
from aft.registry import Registry, path_key
from conftest import make_repo, run_git

HOST = "testhost"

WIDGET = """\
---
type: Project
title: Widget
description: A widget that turns water into widgets.
sources: []
aft:
  project: {project_id}
---

## State

- The daemon listens on 8123. [observed] 2026-09-30 [^s1]

## Facts

- The widget is round. [observed] 2026-09-11 [^s1]

## Decisions

- Releases are cut on main. [stated] 2026-09-15 [^s1]
"""

# The storage fact lives only here, and out of this page's description: the block carries a
# pointer to the page and none of its body, so an entry expecting that fact is found only by
# the search path.
WIDGET_API = """\
---
type: Project
title: Widget API
description: How the widget is driven over HTTP.
sources: []
aft:
  project: {project_id}
---

## Facts

- Storage is SQLite, with a write-ahead log. [observed] 2026-09-14 [^s1]
"""

GASKET = """\
---
type: Project
title: Gasket
description: A gasket that keeps widgets dry.
sources: []
aft:
  project: {project_id}
---

## Facts

- The gasket ships in three sizes. [observed] 2026-09-12 [^s1]
"""

REPORT = """\
---
type: Dream Report
title: "2026-10-01"
---

## Facts

- The widget shape changed twice this quarter. [observed] 2026-10-01 [^s1]
"""

# Resting lines live under ``archive/``, which is a collection apart: the pages collection
# admits only the lines a session is told, so a superseded claim is searchable exactly when
# ``--archive`` asks for it (design 6, 10).
ARCHIVED = """\
---
title: widget archive
description: Lines that rested.
sources: []
---

## Facts

- ~~The widget is square.~~ [observed] 2026-09-10 [^s1] superseded 2026-09-11 by [^s1]
"""

REPORT_PHRASE = "the widget shape changed twice this quarter"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def memory(initialized: Config, tmp_path: Path) -> Config:
    """Two registered projects, one report, and nothing else in the vault."""
    registry = Registry.load(initialized.registry_path)
    widget = registry.add("widget", "Widget")
    gasket = registry.add("gasket", "Gasket")
    registry.add_path(widget, path_key(HOST, make_repo(tmp_path / "widget").resolve()))
    registry.add_path(gasket, path_key(HOST, make_repo(tmp_path / "gasket").resolve()))
    registry.save()
    root = initialized.vault
    write(root / "projects/widget/overview.md", WIDGET.format(project_id=widget.id))
    write(root / "projects/widget/api.md", WIDGET_API.format(project_id=widget.id))
    write(root / "projects/gasket/overview.md", GASKET.format(project_id=gasket.id))
    write(root / "reports/2026/10/2026-10-01.md", REPORT)
    return initialized


def golden(tmp_path: Path, text: str) -> Path:
    return write(tmp_path / "golden.yaml", text)


def block_for(config: Config, cwd: Path, slug: str) -> str:
    """The injection block for ``cwd``, which is what an entry's project names."""
    registry = Registry.load(config.registry_path)
    return inject.build(config, keys.resolve(cwd, registry, config.host))


def test_a_small_golden_set_passes_over_a_fixture_vault(memory: Config, tmp_path: Path):
    """The set the entry asks for: positives and negatives in one run."""
    path = golden(
        tmp_path,
        """
- question: what port does the daemon listen on
  project: widget
  expect:
    - the daemon listens on 8123
    - projects/widget/overview.md
- question: what port does the daemon listen on
  project: widget
  expect_not:
    - the gasket ships in three sizes
- question: what shape is the widget
  project: widget
  expect:
    - the widget is round
  expect_not:
    - the widget is square
""",
    )
    assert evalmod.run(memory, path) == 0


def test_a_fact_that_is_not_in_memory_fails_only_its_entry(memory: Config, tmp_path: Path):
    path = golden(
        tmp_path,
        """
- question: what port does the daemon listen on
  project: widget
  expect:
    - the daemon listens on 8123
- question: what does the widget weigh
  project: widget
  expect:
    - the widget weighs two kilograms
""",
    )
    results = evalmod.evaluate(memory, evalmod.load_golden(path))
    assert [result.passed for result in results] == [True, False]
    assert results[1].notes == ("expected and not found: the widget weighs two kilograms",)


def test_a_forbidden_line_that_surfaces_fails_the_entry(memory: Config, tmp_path: Path):
    path = golden(
        tmp_path,
        """
- question: what port does the daemon listen on
  project: widget
  expect_not:
    - the daemon listens on 8123
""",
    )
    results = evalmod.evaluate(memory, evalmod.load_golden(path))
    assert results[0].passed is False
    assert results[0].notes == ("forbidden and surfaced: the daemon listens on 8123",)


def test_a_fact_the_block_leaves_out_counts_as_found_when_search_reaches_it(
    memory: Config, tmp_path: Path
):
    """The design's block is a map, so most memory is reachable only by searching for it.

    An entry would be worthless if expecting a line demanded that the block carry it: the
    capped map points at the page instead, which is why either path counts as found.
    """
    assert "sqlite" not in block_for(memory, tmp_path / "widget", "widget").lower()
    path = golden(
        tmp_path,
        """
- question: how is widget storage implemented
  project: widget
  expect:
    - storage is sqlite
""",
    )
    assert evalmod.run(memory, path) == 0


def test_a_superseded_claim_stays_out_of_recall(memory: Config, tmp_path: Path):
    """Resting lines are searchable only when asked for, and an entry never asks (design 10)."""
    write(memory.vault / "archive/widget.md", ARCHIVED)
    archived = search.search(memory, "widget square", all_projects=True, archive=True)
    assert any("square" in f"{hit.description} {hit.snippet}".lower() for hit in archived), (
        "the superseded line is not in the archive, so the run below proves nothing"
    )
    path = golden(
        tmp_path,
        """
- question: what shape is the widget
  project: widget
  expect:
    - the widget is round
  expect_not:
    - the widget is square
""",
    )
    assert evalmod.run(memory, path) == 0


def test_a_dream_report_is_never_answered_with(memory: Config, tmp_path: Path):
    """Reports are records about memory rather than memory itself (design 10, M4)."""
    opened = search.search(memory, REPORT_PHRASE, all_projects=True, reports=True)
    assert any(REPORT_PHRASE in f"{hit.path} {hit.snippet}".lower() for hit in opened), (
        "the report is not indexed at all, so the check below proves nothing"
    )
    path = golden(
        tmp_path,
        f"""
- question: {REPORT_PHRASE}
  project: widget
  expect_not:
    - {REPORT_PHRASE}
""",
    )
    assert evalmod.run(memory, path) == 0


def test_an_entry_naming_an_unregistered_project_fails_rather_than_stopping_the_run(
    memory: Config, tmp_path: Path
):
    """A project that vanished from the registry is one thing a golden set exists to catch."""
    path = golden(
        tmp_path,
        """
- question: what port does the daemon listen on
  project: widget
  expect:
    - the daemon listens on 8123
- question: what port does the daemon listen on
  project: sprocket
  expect:
    - the daemon listens on 8123
""",
    )
    results = evalmod.evaluate(memory, evalmod.load_golden(path))
    assert [result.passed for result in results] == [True, False]
    assert results[1].notes[0].startswith("no project 'sprocket' in the registry")
    assert "gasket, widget" in results[1].notes[0]


@pytest.mark.parametrize(
    ("text", "refusal"),
    [
        ("", "expected a list"),
        ("[]\n", "the golden set is empty"),
        (
            "- question: what shape is the widget\n  project: widget\n",
            "expects nothing, so it can never fail",
        ),
        ("- what shape is the widget\n", "expected a mapping"),
        (
            "- question: what shape is the widget\n  project: widget\n  expct:\n    - round\n",
            "unknown key",
        ),
        (
            "- question: a\n  project: widget\n  expect:\n    - round\n  expect:\n    - square\n",
            "duplicate key",
        ),
        (
            "- question: what shape is the widget\n  project: widget\n  expect: round\n",
            "expect must be a list of strings",
        ),
        ("- question: [unclosed\n  project: widget\n", "not valid YAML"),
        ("question: what shape is the widget\n", "expected a list"),
        ("- question:\n  project: widget\n  expect: [round]\n", "question must be a non-empty"),
    ],
    ids=[
        "no text at all",
        "an empty set",
        "an entry that expects nothing",
        "an entry that is not a mapping",
        "a misspelled key",
        "a duplicated key",
        "expect given as text",
        "broken YAML",
        "a root that is not a list",
        "an entry with no question",
    ],
)
def test_a_golden_set_that_cannot_be_trusted_is_refused(
    memory: Config, tmp_path: Path, text: str, refusal: str
):
    with pytest.raises(evalmod.EvalError, match=refusal):
        evalmod.load_golden(golden(tmp_path, text))


def test_a_golden_set_that_is_absent_is_refused(memory: Config, tmp_path: Path):
    with pytest.raises(evalmod.EvalError, match="no golden set at"):
        evalmod.load_golden(tmp_path / "nowhere.yaml")


def test_the_default_golden_set_lives_with_the_vault(memory: Config):
    assert evalmod.golden_path(memory) == memory.vault / "golden.yaml"


def test_the_shipped_example_set_is_one_the_command_accepts(memory: Config):
    """The example set is documentation nobody runs, so this test is what keeps it true.

    Every entry passes against this fixture vault, which is why the example can state what
    each entry is for: a reader copying it sees the shape of a set that reports a real pass.
    """
    example = Path(__file__).resolve().parents[1] / "planning/golden.example.yaml"
    cases = evalmod.load_golden(example)
    results = evalmod.evaluate(memory, cases)

    assert len(cases) == 5, "the example set documents five entries"
    assert [r.case.question for r in results if not r.passed] == []


def test_eval_never_calls_the_model(memory: Config, tmp_path: Path, monkeypatch):
    """M5 tunes caps and prompts on what this reports, so a run that spent a token is useless."""

    def refuse(*args, **kwargs):
        raise AssertionError("aft eval reached for the model")

    monkeypatch.setattr(model, "complete_json", refuse)
    path = golden(
        tmp_path,
        """
- question: what port does the daemon listen on
  project: widget
  expect:
    - the daemon listens on 8123
""",
    )
    assert evalmod.run(memory, path) == 0


def test_an_eval_run_leaves_the_vault_exactly_as_it_found_it(memory: Config, tmp_path: Path):
    """The command reads memory and writes nothing back, so its own git history proves it."""
    listing = sorted(
        path.relative_to(memory.vault).as_posix()
        for path in memory.vault.rglob("*")
        if ".git" not in path.parts
    )
    head = run_git(["rev-parse", "HEAD"], memory.vault)
    path = golden(
        tmp_path,
        """
- question: what port does the daemon listen on
  project: widget
  expect:
    - the daemon listens on 8123
""",
    )
    assert evalmod.run(memory, path) == 0
    assert run_git(["rev-parse", "HEAD"], memory.vault) == head
    assert (
        sorted(
            path2.relative_to(memory.vault).as_posix()
            for path2 in memory.vault.rglob("*")
            if ".git" not in path2.parts
        )
        == listing
    )


def test_the_command_reads_the_vault_golden_set_and_exits_one_when_an_entry_fails(
    memory: Config, capsys
):
    write(
        memory.vault / "golden.yaml",
        """
- question: what port does the daemon listen on
  project: widget
  expect:
    - the daemon listens on 8123
- question: what shape is the widget
  project: widget
  expect:
    - the widget is hexagonal
""",
    )
    assert main(["eval"]) == 1
    printed = capsys.readouterr()
    assert "PASS  [widget] what port does the daemon listen on" in printed.out
    assert "FAIL  [widget] what shape is the widget" in printed.out
    assert "      expected and not found: the widget is hexagonal" in printed.out
    assert "1 of 2 golden entries passed" in printed.err


def test_the_command_exits_two_when_the_set_cannot_be_read(memory: Config, tmp_path: Path, capsys):
    assert main(["eval", str(tmp_path / "nowhere.yaml")]) == 2
    assert "no golden set at" in capsys.readouterr().err
