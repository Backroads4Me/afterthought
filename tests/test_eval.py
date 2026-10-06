"""`aft eval`: a golden set of known answers, run over the two paths recall takes.

The fixture vault holds two projects, so a fact belonging to one can be shown not to reach
the other; one project's memory sits deeper than its block; one claim was superseded; and
there is one dream report, which is what makes the negative cases mean something rather
than merely find nothing.
"""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path

import pytest

from aft import eval as evalmod
from aft import inject, keys, model, search
from aft.cli import main
from aft.config import Config
from aft.registry import Registry, path_key
from conftest import make_repo, run_git

_EVAL_SCRIPT = Path(__file__).resolve().parent.parent / "eval" / "longmemeval.py"
_spec = importlib.util.spec_from_file_location("longmemeval", _EVAL_SCRIPT)
assert _spec is not None and _spec.loader is not None
longmemeval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(longmemeval)

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


def test_compute_recall_at_k():
    hits = [
        "session-1",
        "session-2",
        "session-3",
        "session-4",
        "session-5",
        "session-6",
        "session-7",
        "session-8",
        "session-9",
        "session-10",
        "session-11",
    ]
    # Rank 0 (first position)
    assert evalmod.compute_recall_at_k(hits, "session-1", 1) == 1.0
    assert evalmod.compute_recall_at_k(hits, "session-1", 3) == 1.0
    assert evalmod.compute_recall_at_k(hits, "session-1", 5) == 1.0
    assert evalmod.compute_recall_at_k(hits, "session-1", 10) == 1.0

    # Rank 1 (second position)
    assert evalmod.compute_recall_at_k(hits, "session-2", 1) == 0.0
    assert evalmod.compute_recall_at_k(hits, "session-2", 3) == 1.0
    assert evalmod.compute_recall_at_k(hits, "session-2", 5) == 1.0

    # Rank 4 (fifth position)
    assert evalmod.compute_recall_at_k(hits, "session-5", 3) == 0.0
    assert evalmod.compute_recall_at_k(hits, "session-5", 5) == 1.0

    # Rank 9 (tenth position)
    assert evalmod.compute_recall_at_k(hits, "session-10", 5) == 0.0
    assert evalmod.compute_recall_at_k(hits, "session-10", 10) == 1.0

    # Rank 10 (eleventh position)
    assert evalmod.compute_recall_at_k(hits, "session-11", 10) == 0.0

    # Missing item
    assert evalmod.compute_recall_at_k(hits, "session-missing", 10) == 0.0

    # Edge cases
    assert evalmod.compute_recall_at_k(hits, "session-1", 0) == 0.0
    assert evalmod.compute_recall_at_k(hits, "session-1", -1) == 0.0
    assert evalmod.compute_recall_at_k([], "session-1", 5) == 0.0
    assert evalmod.compute_recall_at_k(hits, "", 5) == 0.0

    # Compound paths and word boundary checks
    hit_1 = ["evidence/claude/session-1.jsonl#e-1"]
    assert evalmod.compute_recall_at_k(hit_1, "session-1", 1) == 1.0
    hit_10 = ["evidence/claude/session-10.jsonl#e-1"]
    assert evalmod.compute_recall_at_k(hit_10, "session-1", 1) == 0.0


def test_compute_ndcg_at_k():
    hits = [
        "session-1",
        "session-2",
        "session-3",
        "session-4",
        "session-5",
        "session-6",
        "session-7",
        "session-8",
        "session-9",
        "session-10",
        "session-11",
    ]
    # Rank 0: 1 / log2(0 + 2) = 1.0
    assert evalmod.compute_ndcg_at_k(hits, "session-1", 10) == pytest.approx(1.0)

    # Rank 1: 1 / log2(1 + 2) = 1 / log2(3)
    assert evalmod.compute_ndcg_at_k(hits, "session-2", 10) == pytest.approx(1.0 / math.log2(3))

    # Rank 2: 1 / log2(2 + 2) = 1 / 2 = 0.5
    assert evalmod.compute_ndcg_at_k(hits, "session-3", 10) == pytest.approx(0.5)

    # Rank 9: 1 / log2(9 + 2) = 1 / log2(11)
    assert evalmod.compute_ndcg_at_k(hits, "session-10", 10) == pytest.approx(1.0 / math.log2(11))

    # Rank 10 (eleventh hit) is beyond k=10
    assert evalmod.compute_ndcg_at_k(hits, "session-11", 10) == 0.0

    # Cutoff at smaller k
    assert evalmod.compute_ndcg_at_k(hits, "session-3", 2) == 0.0

    # Missing hit
    assert evalmod.compute_ndcg_at_k(hits, "session-missing", 10) == 0.0

    # Edge cases
    assert evalmod.compute_ndcg_at_k(hits, "session-1", 0) == 0.0
    assert evalmod.compute_ndcg_at_k([], "session-1", 10) == 0.0
    assert evalmod.compute_ndcg_at_k(hits, "", 10) == 0.0


def test_benchmark_report_dataclass():
    report = evalmod.BenchmarkReport(
        recall_at_1=0.8,
        recall_at_3=0.9,
        recall_at_5=0.95,
        recall_at_10=1.0,
        ndcg_at_10=0.85,
        avg_latency_ms=12.5,
        peak_rss_mb=45.2,
        total_queries=10,
    )
    assert report.recall_1 == 0.8
    assert report.recall_3 == 0.9
    assert report.recall_5 == 0.95
    assert report.recall_10 == 1.0
    assert report.ndcg_10 == 0.85
    assert report.avg_latency_ms == 12.5
    assert report.peak_rss_mb == 45.2
    assert report.total_queries == 10

    d = report.to_dict()
    assert d["recall@1"] == 0.8
    assert d["recall@3"] == 0.9
    assert d["recall@5"] == 0.95
    assert d["recall@10"] == 1.0
    assert d["ndcg@10"] == 0.85
    assert d["avg_latency_ms"] == 12.5
    assert d["peak_rss_mb"] == 45.2
    assert d["total_queries"] == 10

    table = report.format_table()
    assert "LongMemEval-S Retrieval Benchmark Summary" in table
    assert "Queries evaluated : 10" in table
    assert "Recall@1          : 0.8000" in table
    assert str(report) == table


def test_longmemeval_runner_synthetic(tmp_path: Path):
    fixture_data = {
        "sessions": [
            {
                "session_id": "session-crypto",
                "agent": "claude",
                "project": "security",
                "messages": [
                    {"role": "user", "content": "How do we hash passwords?"},
                    {
                        "role": "assistant",
                        "content": "We use Argon2id with 64MB memory cost for password hashing.",
                    },
                ],
            },
            {
                "session_id": "session-cache",
                "agent": "claude",
                "project": "infra",
                "messages": [
                    {"role": "user", "content": "What is our caching strategy?"},
                    {
                        "role": "assistant",
                        "content": "Redis 7 with LRU eviction and 2GB memory limit.",
                    },
                ],
            },
        ],
        "queries": [
            {
                "question": "Argon2id password hashing memory cost",
                "ground_truth": "session-crypto",
            },
            {
                "question": "Redis caching strategy LRU eviction",
                "ground_truth": "session-cache",
            },
        ],
    }
    dataset_file = tmp_path / "longmemeval_fixture.json"
    dataset_file.write_text(json.dumps(fixture_data), encoding="utf-8")
    output_file = tmp_path / "results.json"

    report = longmemeval.run_benchmark(dataset_file, output_path=output_file)
    assert report.total_queries == 2
    assert report.recall_at_1 == 1.0
    assert report.recall_at_3 == 1.0
    assert report.recall_at_5 == 1.0
    assert report.recall_at_10 == 1.0
    assert report.ndcg_at_10 == pytest.approx(1.0)
    assert report.avg_latency_ms >= 0.0
    assert report.peak_rss_mb > 0.0

    assert output_file.is_file()
    saved = json.loads(output_file.read_text(encoding="utf-8"))
    assert saved["recall@1"] == 1.0
    assert "queries" in saved
    assert len(saved["queries"]) == 2

    # Test runner with limit=1
    limited_report = longmemeval.run_benchmark(dataset_file, limit=1)
    assert limited_report.total_queries == 1

    # Test CLI invocation
    rc = longmemeval.main(["--dataset", str(dataset_file), "--limit", "1"])
    assert rc == 0
