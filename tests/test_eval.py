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
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from aft import eval as evalmod
from aft import index, inject, keys, model, search
from aft.cli import main
from aft.config import Config
from aft.registry import Registry, path_key
from conftest import make_repo, run_git
from fakes.model_server import MODEL_NAME, FakeEndpoint, Reply

_EVAL_SCRIPT = Path(__file__).resolve().parent.parent / "eval" / "longmemeval.py"
_spec = importlib.util.spec_from_file_location("longmemeval", _EVAL_SCRIPT)
assert _spec is not None and _spec.loader is not None
longmemeval = importlib.util.module_from_spec(_spec)
# The module must be registered before it runs: its own dataclasses resolve their
# annotations through sys.modules.
sys.modules["longmemeval"] = longmemeval
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
        recall_any_at_1=0.8,
        recall_any_at_3=0.9,
        recall_any_at_5=0.95,
        recall_any_at_10=1.0,
        recall_all_at_10=0.7,
        ndcg_at_10=0.85,
        avg_latency_ms=12.5,
        peak_rss_mb=45.2,
        total_queries=10,
    )
    assert report.recall_any_at_1 == 0.8
    assert report.recall_any_at_3 == 0.9
    assert report.recall_any_at_5 == 0.95
    assert report.recall_any_at_10 == 1.0
    assert report.recall_all_at_10 == 0.7
    assert report.ndcg_at_10 == 0.85
    assert report.avg_latency_ms == 12.5
    assert report.peak_rss_mb == 45.2
    assert report.total_queries == 10

    d = report.to_dict()
    assert d["recall_any@1"] == 0.8
    assert d["recall_any@3"] == 0.9
    assert d["recall_any@5"] == 0.95
    assert d["recall_any@10"] == 1.0
    assert d["recall_all@10"] == 0.7
    assert d["ndcg@10"] == 0.85
    assert d["avg_latency_ms"] == 12.5
    assert d["peak_rss_mb"] == 45.2
    assert d["total_queries"] == 10

    table = report.format_table()
    assert "LongMemEval-S Retrieval Benchmark (raw-retrieval baseline)" in table
    assert "Queries evaluated : 10" in table
    assert "Recall-any@1      : 0.8000" in table
    assert "Recall-all@10     : 0.7000" in table
    assert str(report) == table


def published_item(**overrides: Any) -> dict[str, Any]:
    """One LongMemEval-S item in the published shape, for the harness tests."""
    item: dict[str, Any] = {
        "question_id": "q-1",
        "question_type": "single-session-user",
        "question": "How do we hash passwords?",
        "answer": "Argon2id with a 64MB memory cost",
        "question_date": "2023/05/20 (Sat) 02:21",
        "haystack_dates": ["2023/05/18 (Thu) 10:00", "2023/05/19 (Fri) 11:30"],
        "haystack_session_ids": ["answer_1", "distractor_1"],
        "answer_session_ids": ["answer_1"],
        "haystack_sessions": [
            [
                {"role": "user", "content": "How do we hash passwords?"},
                {"role": "assistant", "content": "We use Argon2id with 64MB memory cost."},
            ],
            [
                {"role": "user", "content": "What is our caching strategy?"},
                {"role": "assistant", "content": "Redis with LRU eviction."},
            ],
        ],
    }
    item.update(overrides)
    return item


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
    assert report.recall_any_at_1 == 1.0
    assert report.recall_any_at_3 == 1.0
    assert report.recall_any_at_5 == 1.0
    assert report.recall_any_at_10 == 1.0
    assert report.recall_all_at_10 == 1.0
    assert report.ndcg_at_10 == pytest.approx(1.0)
    assert report.avg_latency_ms >= 0.0
    assert report.peak_rss_mb > 0.0

    assert output_file.is_file()
    saved = json.loads(output_file.read_text(encoding="utf-8"))
    assert saved["recall_any@1"] == 1.0
    assert "queries" in saved
    assert len(saved["queries"]) == 2
    assert saved["queries"][0]["ranked_sessions"] == ["session-crypto"]
    assert saved["queries"][1]["ranked_sessions"] == ["session-cache"]

    # Test runner with limit=1
    limited_report = longmemeval.run_benchmark(dataset_file, limit=1)
    assert limited_report.total_queries == 1

    # Test CLI invocation
    rc = longmemeval.main(["--dataset", str(dataset_file), "--limit", "1"])
    assert rc == 0


def test_published_longmemeval_schema_parses_with_parallel_session_ids():
    """Turns live in haystack_sessions, with ids and dates in the parallel lists."""
    (case,) = longmemeval.parse_questions([published_item()])
    assert case.question == "How do we hash passwords?"
    assert case.question_type == "single-session-user"
    assert case.session_ids == ("answer_1", "distractor_1")
    assert case.answer_session_ids == ("answer_1",)
    assert case.question_date == datetime(2023, 5, 20, 2, 21, tzinfo=UTC)
    assert [entry.role for entry in case.sessions[0]] == ["user", "assistant"]
    assert case.sessions[0][1].text == "We use Argon2id with 64MB memory cost."
    assert case.sessions[0][0].time == datetime(2023, 5, 18, 10, 0, tzinfo=UTC)
    assert case.sessions[1][0].time == datetime(2023, 5, 19, 11, 30, tzinfo=UTC)


def test_haystack_ids_fall_back_to_the_session_record_then_the_position():
    """A short parallel id list leaves room for record ids and positional names."""
    item = {
        "question": "what was decided",
        "haystack_sessions": [
            {
                "session_id": "from-record",
                "messages": [{"role": "user", "content": "hello there"}],
            },
            [{"role": "user", "content": "and again"}],
        ],
        "haystack_session_ids": ["parallel-id"],
    }
    (case,) = longmemeval.parse_questions([item])
    assert case.session_ids == ("parallel-id", "session-1")


def test_answer_sessions_fall_back_to_ground_truth():
    """A fixture without answer_session_ids still names its answer session."""
    item = {
        "question": "what was decided",
        "ground_truth": "golden-1",
        "haystack_sessions": [[{"role": "user", "content": "hello there"}]],
    }
    (case,) = longmemeval.parse_questions([item])
    assert case.answer_session_ids == ("golden-1",)


def test_wrapper_document_pools_top_level_sessions_into_every_question():
    """A dict of sessions plus a queries list gives each query the shared haystack."""
    data = {
        "sessions": [
            {"session_id": "one", "messages": [{"role": "user", "content": "alpha beta"}]},
            {"session_id": "two", "messages": [{"role": "user", "content": "gamma delta"}]},
        ],
        "queries": [{"question": "alpha beta", "ground_truth": "one"}],
    }
    (case,) = longmemeval.parse_questions(data)
    assert case.session_ids == ("one", "two")


def test_multi_target_recall_and_ndcg_metrics():
    """recall_all demands every answer session; NDCG prices the misses."""
    ranked = ["s1", "s2", "s3", "s4"]
    assert evalmod.compute_recall_any_at_k(ranked, ["s2"], 2) == 1.0
    assert evalmod.compute_recall_any_at_k(ranked, ["s9"], 3) == 0.0
    assert evalmod.compute_recall_any_at_k(ranked, ["s9", "s3"], 3) == 1.0
    assert evalmod.compute_recall_any_at_k(ranked, [], 3) == 0.0
    assert evalmod.compute_recall_any_at_k(ranked, ["s4"], 0) == 0.0

    assert evalmod.compute_recall_all_at_k(ranked, ["s1", "s2"], 2) == 1.0
    assert evalmod.compute_recall_all_at_k(ranked, ["s1", "s2"], 1) == 0.0
    assert evalmod.compute_recall_all_at_k(ranked, ["s1", "s4"], 3) == 0.0
    assert evalmod.compute_recall_all_at_k(ranked, ["s1", "s4"], 4) == 1.0
    assert evalmod.compute_recall_all_at_k(ranked, ["s1", "s9"], 10) == 0.0
    assert evalmod.compute_recall_all_at_k(ranked, [], 10) == 0.0

    # Two relevant sessions at ranks 0 and 1: a perfect ranking.
    assert evalmod.compute_ndcg_at_k_multi(ranked, ["s1", "s2"], 10) == pytest.approx(1.0)
    # Relevant sessions at ranks 0 and 2 against an ideal of ranks 0 and 1.
    expected = (1.0 + 1.0 / math.log2(4)) / (1.0 + 1.0 / math.log2(3))
    assert evalmod.compute_ndcg_at_k_multi(["s1", "s3", "s2"], ["s1", "s2"], 10) == pytest.approx(
        expected
    )
    # One of two relevant sessions found, and found first.
    one_of_two = 1.0 / (1.0 + 1.0 / math.log2(3))
    assert evalmod.compute_ndcg_at_k_multi(ranked, ["s1", "s9"], 10) == pytest.approx(one_of_two)
    assert evalmod.compute_ndcg_at_k_multi(ranked, ["s9"], 10) == 0.0
    assert evalmod.compute_ndcg_at_k_multi(ranked, ["s1"], 0) == 0.0


def test_ranked_sessions_deduplicates_per_session():
    """A session surfacing through both a digest row and an evidence row ranks once."""
    hits = [
        search.Hit(
            path="digests/default/2023/05/2023-05-18-claude-answer_1.md", description="", snippet=""
        ),
        search.Hit(path="aft://session/claude/answer_1#e-0", description="", snippet=""),
        search.Hit(path="aft://session/claude/distractor_1#e-3", description="", snippet=""),
        search.Hit(path="projects/widget/overview.md", description="", snippet=""),
    ]
    assert longmemeval.ranked_sessions(hits) == ["answer_1", "distractor_1"]
    assert longmemeval.ranked_sessions(hits, allowed={"answer_1"}) == ["answer_1"]


def test_published_schema_ranks_the_answer_session_end_to_end(tmp_path: Path):
    """A file in the published shape runs, and the ranking is read at session level."""
    dataset_file = tmp_path / "longmemeval_s.json"
    dataset_file.write_text(json.dumps([published_item()]), encoding="utf-8")
    output_file = tmp_path / "results.json"

    report = longmemeval.run_benchmark(dataset_file, output_path=output_file)
    assert report.total_queries == 1
    assert report.recall_any_at_1 == 1.0
    assert report.recall_all_at_10 == 1.0
    assert report.ndcg_at_10 == pytest.approx(1.0)

    saved = json.loads(output_file.read_text(encoding="utf-8"))
    (record,) = saved["queries"]
    assert record["ranked_sessions"] == ["answer_1"]
    assert record["question_date"] == "2023-05-20T02:21:00+00:00"


def test_multi_session_answers_score_recall_any_and_recall_all(tmp_path: Path):
    """recall_any needs one answer session; recall_all needs all of them."""
    database_turns = [
        {"role": "user", "content": "What do we run the database with?"},
        {
            "role": "assistant",
            "content": "We run the database on Postgres, and the cache sits beside it.",
        },
    ]
    cache_turns = [
        {"role": "user", "content": "What do we run the cache with?"},
        {
            "role": "assistant",
            "content": "We run the cache on Redis, and the database talks to it.",
        },
    ]
    logo_turns = [
        {"role": "user", "content": "What color is the logo?"},
        {"role": "assistant", "content": "The logo is blue."},
    ]
    data = [
        {
            "question_id": "q-both",
            "question_type": "multi-session",
            "question": "What do we run the database and the cache with?",
            "question_date": "2023/06/01 (Thu) 09:00",
            "haystack_dates": [
                "2023/05/20 (Sat) 10:00",
                "2023/05/21 (Sun) 10:00",
                "2023/05/22 (Mon) 10:00",
            ],
            "haystack_session_ids": ["answer_db", "answer_cache", "distractor"],
            "answer_session_ids": ["answer_db", "answer_cache"],
            "haystack_sessions": [database_turns, cache_turns, logo_turns],
        },
        {
            "question_id": "q-one",
            "question_type": "multi-session",
            "question": "What do we run the database with?",
            "question_date": "2023/06/02 (Fri) 09:00",
            "haystack_dates": ["2023/05/20 (Sat) 10:00", "2023/05/21 (Sun) 10:00"],
            "haystack_session_ids": ["answer_db2", "answer_unrelated2"],
            "answer_session_ids": ["answer_db2", "answer_unrelated2"],
            "haystack_sessions": [database_turns, logo_turns],
        },
    ]
    dataset_file = tmp_path / "longmemeval_multi.json"
    dataset_file.write_text(json.dumps(data), encoding="utf-8")
    output_file = tmp_path / "results.json"

    report = longmemeval.run_benchmark(dataset_file, output_path=output_file)
    assert report.recall_any_at_1 == 1.0
    assert report.recall_any_at_10 == 1.0
    assert report.recall_all_at_10 == pytest.approx(0.5)
    one_of_two = 1.0 / (1.0 + 1.0 / math.log2(3))
    assert report.ndcg_at_10 == pytest.approx((1.0 + one_of_two) / 2)

    saved = json.loads(output_file.read_text(encoding="utf-8"))
    both, one = saved["queries"]
    assert sorted(both["ranked_sessions"]) == ["answer_cache", "answer_db"]
    assert one["ranked_sessions"] == ["answer_db2"]


def test_temporal_filtering_uses_haystack_dates_and_question_date(tmp_path: Path):
    """Entry times come from haystack_dates, and the question is asked at question_date.

    Both sessions say the same thing months apart; only the one inside the window
    "last week" opens from the question's date may reach the evidence index.
    """
    item = {
        "question_id": "q-time",
        "question_type": "temporal-reasoning",
        "question": "Did we agree on what backup schedule to use last week?",
        "question_date": "2023/05/20 (Sat) 02:21",
        "haystack_dates": ["2023/05/16 (Tue) 10:00", "2023/04/02 (Sun) 10:00"],
        "haystack_session_ids": ["answer_recent", "answer_old"],
        "answer_session_ids": ["answer_recent"],
        "haystack_sessions": [
            [
                {"role": "user", "content": "Did we agree on what backup schedule to use?"},
                {
                    "role": "assistant",
                    "content": "The backup schedule we agreed on is nightly at 02:00.",
                },
            ],
            [
                {"role": "user", "content": "Did we agree on what backup schedule to use?"},
                {
                    "role": "assistant",
                    "content": "The backup schedule we agreed on is hourly at night.",
                },
            ],
        ],
    }
    (case,) = longmemeval.parse_questions([item])
    assert case.question_date == datetime(2023, 5, 20, 2, 21, tzinfo=UTC)
    assert case.sessions[0][0].time == datetime(2023, 5, 16, 10, 0, tzinfo=UTC)
    assert case.sessions[1][0].time == datetime(2023, 4, 2, 10, 0, tzinfo=UTC)

    with longmemeval.isolated_vault() as config:
        longmemeval.ingest_question(config, case)
        hits = search.search(
            config, case.question, all_projects=True, limit=20, now=case.question_date
        )
    evidence_sessions = {
        longmemeval._session_of_hit(hit.path) for hit in hits if hit.collection == "evidence"
    }
    assert evidence_sessions == {"answer_recent"}


def test_benchmark_asks_each_question_at_its_question_date(monkeypatch, tmp_path: Path):
    """run_benchmark hands the question's date to search as the reference time."""
    dataset_file = tmp_path / "longmemeval_s.json"
    dataset_file.write_text(json.dumps([published_item()]), encoding="utf-8")
    captured: dict[str, Any] = {}

    real_search = search.search

    def spy(config: Any, query: str, **kwargs: Any) -> list[search.Hit]:
        captured["now"] = kwargs.get("now")
        return real_search(config, query, **kwargs)

    monkeypatch.setattr(search, "search", spy)
    longmemeval.run_benchmark(dataset_file)
    assert captured["now"] == datetime(2023, 5, 20, 2, 21, tzinfo=UTC)


# ------------------------------------------------------------------ pipeline mode (F71)

_PROMPT_MARKERS = (
    ("summary", "write what it did and where the work stood"),
    ("digest", "extract what a future session would need to know"),
    ("reconcile", "check what it shows about the project's short-term memory"),
    ("turns", "You label what a user said"),
    ("observed", "You read the assistant's own words"),
    ("decide", "You maintain a project's memory"),
    ("final", "You read the items one part of a working session settled"),
)


class RoutedEndpoint(FakeEndpoint):
    """The fake endpoint that answers by the prompt a call carries, not by call order.

    One question's pipeline calls run in a fixed order, but parallel jobs interleave
    their calls on the one endpoint the caller's config names, so a script of ordered
    replies cannot serve two questions at once. Each kind of call gets its own scripted
    answer; a call the test scripted no answer for is refused with a 500, the same loud
    nothing an unscripted ordered endpoint gives.
    """

    def __init__(self, by_kind: dict[str, Any]) -> None:
        super().__init__([])
        self.by_kind = by_kind

    def reply_for(self, call: int, body: dict) -> Reply:
        messages = body.get("messages") or []
        system = str(messages[0].get("content") or "") if messages else ""
        user = str(messages[1].get("content") or "") if len(messages) > 1 else ""
        kind = next((name for name, marker in _PROMPT_MARKERS if marker in system), "")
        if kind in ("summary", "digest", "turns", "observed"):
            # A digest-side call carries the session's own words, so the session it belongs
            # to is named by the word only that session says, which the test gives as the
            # "word" key: the answer is keyed "<word>:<kind>".
            word = str(self.by_kind.get("word", ""))
            which = "answer" if word and word in user.lower() else "distractor"
            kind = f"{which}:{kind}"
        answer = self.by_kind.get(kind)
        if answer is None:
            return Reply(status=500)
        return answer if isinstance(answer, Reply) else Reply(content=answer)


def pipeline_home_config(home: Path, vault: Path, endpoint: str) -> None:
    """The caller's config: the sandbox copies its model table, so the endpoint lives here."""
    (home / "config.toml").write_text(
        f'vault = "{vault}"\nhost = "{HOST}"\n'
        f'[model]\nendpoint = "{endpoint}"\nname = "{MODEL_NAME}"\n',
        encoding="utf-8",
    )


def turn_item(turn: str, label: str, statement: str = "", quote: str = "") -> dict:
    """One turns answer item of the shape the schema describes."""
    return {
        "turn": turn,
        "label": label,
        "statement": statement,
        "quote": quote,
        "scope": "project",
        "term": "long",
        "proposal": "",
    }


HASH_QUESTION = {
    "question_id": "q-hash",
    "question_type": "single-session-user",
    "question": "What memory cost does our password hashing use?",
    "answer": "Argon2id with a 64MB memory cost",
    "question_date": "2023/05/20 (Sat) 02:21",
    "haystack_dates": ["2023/05/18 (Thu) 10:00", "2023/05/19 (Fri) 11:30"],
    "haystack_session_ids": ["answer_1", "distractor_1"],
    "answer_session_ids": ["answer_1"],
    # Two prompts a session: the digest floor is two user prompts, and a thinner session
    # is never digested at all.
    "haystack_sessions": [
        [
            {"role": "user", "content": "We hash passwords with Argon2id at a 64MB memory cost."},
            {"role": "assistant", "content": "Configured: Argon2id, 64MB per hash."},
            {"role": "user", "content": "Good. The deployment notes should carry that setting."},
            {"role": "assistant", "content": "Noted for the deployment notes."},
        ],
        [
            {"role": "user", "content": "What is our caching strategy?"},
            {"role": "assistant", "content": "Redis with LRU eviction."},
            {"role": "user", "content": "Thanks. Let us leave the cache topic for later."},
            {"role": "assistant", "content": "Sounds good."},
        ],
    ],
}

ANSWER_TURNS = {
    "summary": {
        "summary": "Settled how passwords are hashed, and the memory cost of it.",
        "handoff": {"in_flight": {"text": "", "entry": ""}, "failed": [], "open": []},
    },
    "turns": {
        "items": [
            turn_item(
                "#1",
                "fact",
                "We hash passwords with Argon2id at a 64MB memory cost.",
                "We hash passwords with Argon2id at a 64MB memory cost.",
            ),
            turn_item("#3", "none"),
        ]
    },
    "observed": {"items": []},
    "decide": {"action": "add", "line": None, "reason": "the user stated it themselves"},
}

DISTRACTOR_TURNS = {
    "summary": {
        "summary": "Asked about the cache.",
        "handoff": {"in_flight": {"text": "", "entry": ""}, "failed": [], "open": []},
    },
    "turns": {"items": [turn_item("#1", "question"), turn_item("#3", "none")]},
    "observed": {"items": []},
}


def pipeline_endpoint() -> RoutedEndpoint:
    """The routed endpoint one two-session question runs against.

    The digest-side calls carry the session's own words in their user message, so the two
    sessions are told apart by the words only one of them says, and each gets the answers
    written for it whatever order the calls arrive in. The answer session settles one fact
    the dream decides on; the distractor settles nothing, so it draws no decide call.
    """
    return RoutedEndpoint(
        {
            "word": "argon2id",
            "answer:summary": ANSWER_TURNS["summary"],
            "answer:turns": ANSWER_TURNS["turns"],
            "answer:observed": ANSWER_TURNS["observed"],
            "distractor:summary": DISTRACTOR_TURNS["summary"],
            "distractor:turns": DISTRACTOR_TURNS["turns"],
            "distractor:observed": DISTRACTOR_TURNS["observed"],
            "decide": ANSWER_TURNS["decide"],
        }
    )


def write_dataset(path: Path, *items: dict) -> Path:
    path.write_text(json.dumps(list(items)), encoding="utf-8")
    return path


ATTRIBUTION_PAGE = """\
---
type: Project
title: Widget
description: A widget that turns water into widgets.
sources:
  - id: s1
    resource: aft://session/claude/answer_1#u1
    quote: the daemon listens on 8123
  - id: s2
    resource: aft://session/claude/distractor_1#u1
    quote: the cache holds two gigabytes
aft:
  project: p_widget
---

## Facts

- The daemon listens on 8123. [observed] 2026-09-30 [^s1]
- The cache holds two gigabytes. [observed] 2026-09-30 [^s2]
"""


STEM_PAGE = """\
---
type: Project
title: Widget
description: A widget that turns water into widgets.
sources:
  - id: s1
    resource: aft://session/claude/answer_1#u1
    quote: the ingest is paused at midnight
  - id: s2
    resource: aft://session/claude/distractor_1#u1
    quote: the cache holds two gigabytes
aft:
  project: p_widget
---

## Facts

- The cache holds two gigabytes. [observed] 2026-09-30 [^s2]
- The ingest is paused at midnight. [observed] 2026-09-30 [^s1]
"""


def test_a_page_hit_credits_only_the_sessions_its_matched_line_cites(initialized: Config):
    """A section's other lines say nothing about a hit: only the matched line's sources."""
    write(initialized.vault / "projects/widget/overview.md", ATTRIBUTION_PAGE)
    hit = search.Hit(
        path="projects/widget/overview.md",
        description="Widget",
        snippet="The daemon listens on 8123.",
        collection=index.PAGES,
        heading="Facts",
    )
    sessions, statement = longmemeval._page_hit_sessions(
        initialized, hit, "what port does the daemon listen on"
    )
    assert sessions == ["answer_1"]
    assert statement == "The daemon listens on 8123."


def test_a_page_hit_whose_section_matches_no_query_word_credits_no_session(initialized: Config):
    """No line matching any query word credits no session (BUILD-PLAN F72)."""
    write(initialized.vault / "projects/widget/overview.md", ATTRIBUTION_PAGE)
    hit = search.Hit(
        path="projects/widget/overview.md",
        description="Widget",
        snippet="The daemon listens on 8123.",
        collection=index.PAGES,
        heading="Facts",
    )
    matched = longmemeval._page_hit_sessions(initialized, hit, "purple elephants giraffes")
    assert matched == ([], None)


def test_a_page_hit_matches_a_line_by_stem_not_raw_word(initialized: Config):
    """The matched line is the one the query's stems rank first (BUILD-PLAN F72).

    "pausing" shares no raw word with either line; its stem matches only the paused
    line, whose session is credited, and the cache line, which matches nothing, credits
    none - the raw word overlap this replaces would have credited neither.
    """
    write(initialized.vault / "projects/widget/overview.md", STEM_PAGE)
    hit = search.Hit(
        path="projects/widget/overview.md",
        description="Widget",
        snippet="The ingest is paused at midnight.",
        collection=index.PAGES,
        heading="Facts",
    )
    sessions, statement = longmemeval._page_hit_sessions(initialized, hit, "pausing tonight")
    assert statement == "The ingest is paused at midnight."
    assert sessions == ["answer_1"]


def test_line_ranking_orders_by_stemmed_bm25():
    """The in-memory table ranks by stemmed BM25, ties keeping statement order (F72)."""
    statements = [
        "The cache holds two gigabytes.",
        "The ingest is paused at midnight.",
    ]
    # A stem-only match ("pausing" against "paused") ranks the one line it matches.
    assert longmemeval._ranked_lines(sorted(longmemeval._terms("pausing")), statements) == [1]
    # Both lines match "the"; the cache line also matches "cache", so it ranks first.
    assert longmemeval._ranked_lines(sorted(longmemeval._terms("the cache")), statements) == [0, 1]
    # Nothing matches nothing, and there is nothing to rank without either side.
    assert longmemeval._ranked_lines(sorted(longmemeval._terms("purple")), statements) == []
    assert longmemeval._ranked_lines([], statements) == []
    assert longmemeval._ranked_lines(["cache"], []) == []


def test_pipeline_runs_a_two_session_haystack_end_to_end(home, tmp_path):
    """The answer session's fact reaches a page line, and every ranking credits its session."""
    dataset = write_dataset(tmp_path / "longmemeval_s.json", HASH_QUESTION)
    workdir = tmp_path / "work"
    output = tmp_path / "results.jsonl"
    with pipeline_endpoint() as fake:
        pipeline_home_config(home, tmp_path / "vault", fake.endpoint)
        report = longmemeval.run_pipeline(dataset, workdir, output)
    assert report.total == 1
    assert report.scored == 1
    assert report.errors == 0
    assert report.stopped == 0
    assert report.model == MODEL_NAME
    assert report.digests["recall_any@1"] == 1.0
    assert report.full["recall_any@1"] == 1.0
    assert report.coverage == 1.0

    (record,) = longmemeval._read_records(output)
    assert record["id"] == "q-hash"
    assert record["mode"] == "pipeline"
    assert record["question_date"] == "2023-05-20T02:21:00+00:00"
    assert record["answer_session_ids"] == ["answer_1"]
    assert record["scored"] is True
    assert record["stopped"] == ""
    assert record["sessions"] == 2
    assert record["sessions_digested"] == 2
    assert record["sessions_empty"] == 0
    assert record["sessions_failed"] == 0
    assert record["session_outcomes"] == {"answer_1": "digested", "distractor_1": "digested"}
    assert record["work_failures"] == []
    assert record["dream_failures"] == {}
    assert record["model"] == MODEL_NAME
    assert record["pipeline"] == "turns"
    assert len(record["prompt_hash"]) == 64
    assert record["tokens"] > 0
    assert record["sandbox"] == str(workdir / "q-hash")
    # The sandbox is kept by default: config, vault and project all still there.
    assert (workdir / "q-hash" / "home" / "config.toml").is_file()
    assert (workdir / "q-hash" / "vault" / "projects").is_dir()

    rankings = record["rankings"]
    assert rankings["digests"]["ranked_sessions"][0] == "answer_1"
    assert rankings["digests"]["recall_any@1"] == 1.0
    assert rankings["full"]["ranked_sessions"][0] == "answer_1"
    assert rankings["full"]["recall_any@1"] == 1.0
    assert rankings["full"]["recall_all@10"] == 1.0
    credited = rankings["full"]["ranked_hits"]
    assert [hit["session"] for hit in credited] == rankings["full"]["ranked_sessions"]
    assert credited and all(
        set(hit) == {"session", "collection", "path", "heading", "statement"} for hit in credited
    )
    assert rankings["coverage"] == 1.0


def test_the_sandbox_config_carries_the_caller_s_model_and_caps_that_never_bind(home, tmp_path):
    """The caller's model table goes over whole, and the sandbox's own caps never bind."""
    dataset = write_dataset(tmp_path / "longmemeval_s.json", HASH_QUESTION)
    workdir = tmp_path / "work"
    output = tmp_path / "results.jsonl"
    # A stopped run is the cheap way to a kept sandbox: the config is written before the
    # first call, and a stopped question's sandbox is kept whether it was asked for or not.
    endpoint = RoutedEndpoint({"summary": Reply(status=500)})
    with endpoint:
        url = endpoint.endpoint
        pipeline_home_config(home, tmp_path / "vault", url)
        longmemeval.run_pipeline(dataset, workdir, output)
    (record,) = longmemeval._read_records(output)
    assert record["stopped"]
    sandbox = Path(record["sandbox"])
    text = (sandbox / "home" / "config.toml").read_text(encoding="utf-8")
    assert f'endpoint = "{url}"' in text
    assert f'name = "{MODEL_NAME}"' in text
    assert f"daily_token_budget = {longmemeval.SANDBOX_BUDGET}" in text
    assert f"retention_days = {longmemeval.SANDBOX_RETENTION_DAYS}" in text
    assert "quiet_minutes = 0" in text
    assert 'host = "evalhost"' in text
    vault_dir = sandbox / "vault"
    assert f'vault = "{vault_dir}"' in text
    # The sandbox's calls are the sandbox's cost: nothing lands in the caller's home.
    assert not (home / "usage.jsonl").exists()


def test_a_stopped_question_is_flagged_not_scored_and_keeps_its_sandbox(home, tmp_path):
    """An endpoint that never answers stops the run: the record says so and scores nothing."""
    dataset = write_dataset(tmp_path / "longmemeval_s.json", HASH_QUESTION)
    workdir = tmp_path / "work"
    output = tmp_path / "results.jsonl"
    endpoint = RoutedEndpoint({"summary": Reply(status=500)})
    with endpoint:
        pipeline_home_config(home, tmp_path / "vault", endpoint.endpoint)
        report = longmemeval.run_pipeline(dataset, workdir, output)
    assert report.scored == 0
    assert report.stopped == 1
    (record,) = longmemeval._read_records(output)
    assert record["stopped"]
    assert record["scored"] is False
    assert record["sessions_digested"] == 0
    assert Path(record["sandbox"]).is_dir()


def test_parallel_jobs_run_questions_as_separate_processes(home, tmp_path):
    """Two questions at once land as two scored records, one per process, ids apart.

    The two questions carry identical session text under different ids, so their calls
    draw the same answers however the endpoint interleaves them.
    """
    questions = []
    for name in ("q-one", "q-two"):
        item = json.loads(json.dumps(HASH_QUESTION))
        item["question_id"] = name
        item["haystack_session_ids"] = [f"answer_{name}", f"distractor_{name}"]
        item["answer_session_ids"] = [f"answer_{name}"]
        questions.append(item)
    dataset = write_dataset(tmp_path / "longmemeval_s.json", *questions)
    workdir = tmp_path / "work"
    output = tmp_path / "results.jsonl"
    with pipeline_endpoint() as fake:
        pipeline_home_config(home, tmp_path / "vault", fake.endpoint)
        report = longmemeval.run_pipeline(dataset, workdir, output, jobs=2)
    assert report.scored == 2
    records = longmemeval._read_records(output)
    assert sorted(record["id"] for record in records) == ["q-one", "q-two"]
    assert all(record["scored"] is True for record in records)
    assert all(record["sessions_digested"] == 2 for record in records)
    assert all(not record["stopped"] for record in records)
    # Each question ran in the sandbox its own id named.
    assert {record["sandbox"] for record in records} == {
        str(workdir / "q-one"),
        str(workdir / "q-two"),
    }


def test_resume_skips_the_ids_a_finished_record_stands_for(home, tmp_path):
    """A rerun answers only the questions no finished record stands for."""
    second = dict(HASH_QUESTION, question_id="q-cache")
    dataset = write_dataset(tmp_path / "longmemeval_s.json", HASH_QUESTION, second)
    ids = tmp_path / "first.ids"
    ids.write_text("q-hash\n", encoding="utf-8")
    workdir = tmp_path / "work"
    output = tmp_path / "results.jsonl"
    with pipeline_endpoint() as fake:
        pipeline_home_config(home, tmp_path / "vault", fake.endpoint)
        first = longmemeval.run_pipeline(dataset, workdir, output, ids_path=ids)
        assert first.scored == 1
        rerun = longmemeval.run_pipeline(dataset, workdir, output, resume=True)
    assert rerun.scored == 1
    records = longmemeval._read_records(output)
    assert [record["id"] for record in records] == ["q-hash", "q-cache"]


def test_the_sandbox_config_writes_extra_body_as_a_toml_table():
    """An extra_body with nested tables reaches the sandbox as TOML it reads back whole."""
    import tomllib

    body = {"chat_template_kwargs": {"enable_thinking": False}, "stop": ["a", "b"], "n": 1}
    parsed = tomllib.loads(f"extra_body = {longmemeval._toml_value(body)}\n")
    assert parsed["extra_body"] == body


def test_discard_sandbox_deletes_only_a_finished_scored_sandbox(home, tmp_path):
    """Sandboxes are kept by default; --discard-sandbox deletes only a finished, scored one."""
    dataset = write_dataset(tmp_path / "longmemeval_s.json", HASH_QUESTION)
    workdir = tmp_path / "work"
    output = tmp_path / "results.jsonl"
    with pipeline_endpoint() as fake:
        pipeline_home_config(home, tmp_path / "vault", fake.endpoint)
        longmemeval.run_pipeline(dataset, workdir, output, discard_sandbox=True)
    (record,) = longmemeval._read_records(output)
    assert record["scored"] is True
    assert not Path(record["sandbox"]).exists()

    # A stopped question's sandbox survives --discard-sandbox: it is the reason a run
    # keeps what it kept, and the thing a later look at the sandbox answers for.
    stopped_output = tmp_path / "stopped.jsonl"
    endpoint = RoutedEndpoint({"summary": Reply(status=500)})
    with endpoint:
        pipeline_home_config(home, tmp_path / "vault-stopped", endpoint.endpoint)
        longmemeval.run_pipeline(dataset, workdir / "stopped", stopped_output, discard_sandbox=True)
    (stopped,) = longmemeval._read_records(stopped_output)
    assert stopped["scored"] is False
    assert Path(stopped["sandbox"]).is_dir()


def test_rescore_reranks_a_kept_sandbox_without_the_model(home, tmp_path, monkeypatch):
    """--rescore reads the sandboxes a records JSONL names and never calls the model."""
    dataset = write_dataset(tmp_path / "longmemeval_s.json", HASH_QUESTION)
    workdir = tmp_path / "work"
    output = tmp_path / "results.jsonl"
    with pipeline_endpoint() as fake:
        pipeline_home_config(home, tmp_path / "vault", fake.endpoint)
        report = longmemeval.run_pipeline(dataset, workdir, output)
    assert report.scored == 1

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("rescore reached for the model")

    monkeypatch.setattr(model, "complete_json", refuse)
    # The endpoint is down here: any model call rescore made would fail loudly twice.
    rescored_path = tmp_path / "rescored.jsonl"
    rescored = longmemeval.rescore_pipeline(output, dataset, rescored_path)
    assert rescored.scored == 1
    assert rescored.full == report.full
    (original,) = longmemeval._read_records(output)
    (fresh,) = longmemeval._read_records(rescored_path)
    assert fresh["id"] == "q-hash"
    assert fresh["rankings"] == original["rankings"]
    assert fresh["session_outcomes"] == {"answer_1": "digested", "distractor_1": "digested"}


def test_rescore_rescores_only_sandboxes_that_still_stand(home, tmp_path, monkeypatch):
    """A record whose sandbox is gone is copied unchanged; a standing one is re-scored."""
    second = dict(HASH_QUESTION, question_id="q-gone")
    dataset = write_dataset(tmp_path / "longmemeval_s.json", HASH_QUESTION, second)
    workdir = tmp_path / "work"
    output = tmp_path / "results.jsonl"
    kept_ids = tmp_path / "kept.ids"
    kept_ids.write_text("q-hash\n", encoding="utf-8")
    gone_ids = tmp_path / "gone.ids"
    gone_ids.write_text("q-gone\n", encoding="utf-8")
    with pipeline_endpoint() as fake:
        pipeline_home_config(home, tmp_path / "vault", fake.endpoint)
        longmemeval.run_pipeline(dataset, workdir, output, ids_path=kept_ids)
        longmemeval.run_pipeline(dataset, workdir, output, ids_path=gone_ids, discard_sandbox=True)
    records = {record["id"]: record for record in longmemeval._read_records(output)}
    assert Path(records["q-hash"]["sandbox"]).is_dir()
    assert not Path(records["q-gone"]["sandbox"]).exists()

    def fake_score(sandbox: Any, case: Any) -> dict[str, Any]:
        return {"marker": case.question_id}

    monkeypatch.setattr(longmemeval, "score_question", fake_score)
    rescored_path = tmp_path / "rescored.jsonl"
    longmemeval.rescore_pipeline(output, dataset, rescored_path)
    fresh = {record["id"]: record for record in longmemeval._read_records(rescored_path)}
    assert fresh["q-hash"]["rankings"] == {"marker": "q-hash"}
    assert fresh["q-gone"]["rankings"] == records["q-gone"]["rankings"]


def test_rescore_refuses_to_write_over_its_input(tmp_path):
    """The new JSONL is a new file: the records it reads stay as the run left them."""
    dataset = write_dataset(tmp_path / "longmemeval_s.json", HASH_QUESTION)
    output = tmp_path / "results.jsonl"
    with pytest.raises(longmemeval.PipelineError, match="other than the records it reads"):
        longmemeval.rescore_pipeline(output, dataset, output)


def test_the_rescore_command_wires_the_flags(home, tmp_path):
    """--mode pipeline --rescore <records> --output <new> re-scores through main()."""
    dataset = write_dataset(tmp_path / "longmemeval_s.json", HASH_QUESTION)
    workdir = tmp_path / "work"
    output = tmp_path / "results.jsonl"
    with pipeline_endpoint() as fake:
        pipeline_home_config(home, tmp_path / "vault", fake.endpoint)
        longmemeval.run_pipeline(dataset, workdir, output)
    rescored_path = tmp_path / "rescored.jsonl"
    assert (
        longmemeval.main(
            [
                "--dataset",
                str(dataset),
                "--mode",
                "pipeline",
                "--rescore",
                str(output),
                "--output",
                str(rescored_path),
            ]
        )
        == 0
    )
    (fresh,) = longmemeval._read_records(rescored_path)
    assert fresh["id"] == "q-hash"
    assert fresh["rankings"]["full"]["ranked_sessions"][0] == "answer_1"


def test_the_rescore_command_requires_an_output(tmp_path):
    """A rescore that has nowhere new to write the records is refused at the command line."""
    dataset = write_dataset(tmp_path / "longmemeval_s.json", HASH_QUESTION)
    with pytest.raises(SystemExit):
        longmemeval.main(
            [
                "--dataset",
                str(dataset),
                "--mode",
                "pipeline",
                "--rescore",
                str(tmp_path / "r.jsonl"),
            ]
        )
