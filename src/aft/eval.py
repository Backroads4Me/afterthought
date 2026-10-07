"""The golden set: whether memory answers what it should and stays out of what it should not.

Design section 15 makes M5's done condition a set of known answers written by whoever owns
the vault, and this is the command that runs it: each entry asks a ``question`` of one
``project``, names text or paths that must be found (``expect``) and text that must never
surface (``expect_not``). Both checks read the two paths an agent actually meets - the block
a session starts with and the results a search returns - and neither calls a model, so a run
is reproducible and costs nothing. M5 tunes caps and prompts by what this reports, which is
why it writes nothing to the vault and asks nothing of an endpoint.

What the two keys mean follows from how the design describes them. An expected line has to
be *found*, and memory may surface it either way: a fact in the overview is in the block,
while a fact deeper in the project is a search hit or a line of the block's page map. A
forbidden line must not surface at all, so it is checked against the block and the results
together. The limit is how many results count as found, which is the width `aft search`
holds itself to by default.
"""

from __future__ import annotations

import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from aft import inject, keys, search
from aft.config import Config
from aft.keys import Resolution
from aft.library.manifest import UniqueKeyLoader
from aft.registry import Project, Registry

# The golden set sits beside the vault's other hand-written file, its registry. Design 15
# calls it ~/afterthought/golden.yaml, and ~/afterthought is the vault.
GOLDEN = "golden.yaml"

# How many results count as found: `aft search`'s own default width.
DEFAULT_LIMIT = 8

# The keys an entry may carry, because a typo there would quietly check nothing.
KEYS = ("question", "project", "expect", "expect_not")


class EvalError(Exception):
    """A golden set that is missing, malformed, or checks nothing."""


@dataclass(frozen=True)
class Case:
    """One entry of the golden set."""

    question: str
    project: str
    expect: tuple[str, ...] = ()
    expect_not: tuple[str, ...] = ()


@dataclass(frozen=True)
class Result:
    """What the two recall paths said about one entry."""

    case: Case
    passed: bool
    notes: tuple[str, ...] = ()

    def __str__(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        head = f"{verdict}  [{self.case.project}] {self.case.question}"
        return "\n".join([head, *(f"      {note}" for note in self.notes)])


def golden_path(config: Config) -> Path:
    """Where the golden set lives when the command is given no path."""
    return config.vault / GOLDEN


def load_golden(path: Path) -> list[Case]:
    """Read the golden set, refusing anything that would check less than it appears to.

    A set that is absent, empty, or half-written is an error rather than a passing run: an
    eval that reports success over no cases is the one result worse than a failing one,
    because it gets read as evidence that memory holds.
    """
    target = Path(path).expanduser()
    if not target.is_file():
        raise EvalError(f"no golden set at {target}")
    try:
        # Duplicate keys are refused for the same reason the manifest refuses them: a second
        # `expect:` would silently replace the first, and a silently dropped expectation
        # looks exactly like a passing entry.
        entries = yaml.load(target.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise EvalError(f"{target}: golden set is not valid YAML: {exc}") from exc
    if not isinstance(entries, list):
        raise EvalError(f"{target}: expected a list of entries, found {_shape(entries)}")
    if not entries:
        raise EvalError(f"{target}: the golden set is empty")
    return [_case(entry, number, target) for number, entry in enumerate(entries, start=1)]


def _case(entry: object, number: int, target: Path) -> Case:
    """One entry, checked before it is trusted to check anything."""
    where = f"{target}: entry {number}"
    if not isinstance(entry, dict):
        raise EvalError(f"{where}: expected a mapping, found {_shape(entry)}")
    unknown = sorted(str(key) for key in entry if key not in KEYS)
    if unknown:
        raise EvalError(
            f"{where}: unknown key(s) {', '.join(unknown)}; an entry holds {', '.join(KEYS)}"
        )
    question = entry.get("question")
    project = entry.get("project")
    if not isinstance(question, str) or not question.strip():
        raise EvalError(f"{where}: question must be a non-empty string")
    if not isinstance(project, str) or not project.strip():
        raise EvalError(f"{where}: project must be a non-empty string")
    expect = _lines(entry.get("expect"), "expect", where)
    expect_not = _lines(entry.get("expect_not"), "expect_not", where)
    if not expect and not expect_not:
        raise EvalError(f"{where}: {question!r} expects nothing, so it can never fail")
    return Case(
        question=question.strip(),
        project=project.strip(),
        expect=expect,
        expect_not=expect_not,
    )


def _lines(value: object, name: str, where: str) -> tuple[str, ...]:
    """A list of text lines, or nothing at all when the key is absent."""
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, list):
        raise EvalError(f"{where}: {name} must be a list of strings, found {_shape(value)}")
    lines = [str(item).strip() for item in value]
    if not all(lines):
        raise EvalError(f"{where}: {name} holds an empty line")
    return tuple(lines)


def evaluate(config: Config, cases: list[Case], *, limit: int = DEFAULT_LIMIT) -> list[Result]:
    """Run every entry against both recall paths, with no model and no writes.

    The registry is read once, and an entry naming a project that is not in it is reported
    rather than raised: a project that vanished from the registry is one of the things a
    golden set exists to catch, and the rest of the set still has to run.
    """
    registry = Registry.load(config.registry_path)
    return [_evaluate(config, registry, case, limit) for case in cases]


def _evaluate(config: Config, registry: Registry, case: Case, limit: int) -> Result:
    project = registry.by_slug(case.project)
    if project is None:
        slugs = ", ".join(sorted(p.slug for p in registry.projects)) or "no projects"
        note = f"no project {case.project!r} in the registry; it has {slugs}"
        return Result(case, False, (note,))
    block = inject.build(config, _resolution(project))
    hits = search.search(config, case.question, project=case.project, limit=limit)
    seen = [block, *(_text(config, hit) for hit in hits)]
    missing = [want for want in case.expect if not _found(want, seen)]
    notes = [f"expected and not found: {want}" for want in missing]
    leaked = [banned for banned in case.expect_not if _found(banned, seen)]
    notes += [f"forbidden and surfaced: {banned}" for banned in leaked]
    return Result(case, not notes, tuple(notes))


def _resolution(project: Project) -> Resolution:
    """A resolution for a project named outright, the way `aft redigest` builds one.

    The golden set names a project, not a directory: an eval run has no session working
    directory to resolve, and the block for a registered project is built from the registry
    and the vault rather than from wherever the question was asked.
    """
    return keys.Resolution(
        kind=keys.PATH,
        cwd=Path("."),
        project=project,
        slug=project.slug,
        root=None,
        alias_added=False,
    )


def _text(config: Config, hit: search.Hit) -> str:
    """Everything a result makes visible, which is all an entry can be asked to name.

    A snippet is FTS5's fragment of the *best-matching* column, not the row's body, so a
    line that is genuinely in the page can be missing from it. Judging a result by its
    snippet alone would report a fact the search really did reach as "expected and not
    found", so a result counts with the whole page it names. An evidence result names a
    session rather than a page, and has no page to read.
    """
    parts = [hit.path, hit.description, hit.snippet]
    try:
        parts.append(search.read_page(config, hit.path))
    except search.SearchError:
        pass
    return "\n".join(part for part in parts if part)


def _found(needle: str, seen: list[str]) -> bool:
    """Whether ``needle`` appears anywhere in the visible text, ignoring layout and case.

    A golden entry quotes a fact or names a path, and neither survives the trip through a
    snippet byte for byte: the index folds whitespace and the block may break a line. The
    comparison is done the same way on both sides so that a line which is present is never
    reported missing.
    """
    wanted = _flat(needle)
    return any(wanted and wanted in _flat(haystack) for haystack in seen)


def _flat(text: str) -> str:
    return " ".join(text.split()).casefold()


def run(config: Config, path: str | Path | None = None, *, limit: int = DEFAULT_LIMIT) -> int:
    """Print each entry's verdict, and return 1 when any entry failed.

    A failing entry is a fact about memory rather than a broken command, so it exits 1 the
    way `aft lint` does: a script can tell "memory regressed" apart from "aft could not
    run", which is the difference the M5 checklist acts on.
    """
    cases = load_golden(Path(path) if path is not None else golden_path(config))
    results = evaluate(config, cases, limit=limit)
    for result in results:
        print(result)
    failed = sum(1 for result in results if not result.passed)
    passed = len(results) - failed
    print(f"{passed} of {len(results)} golden entries passed", file=sys.stderr)
    return 1 if failed else 0


def _shape(value: object) -> str:
    """What the file actually held, for an error a person can act on."""
    if isinstance(value, dict):
        return "a mapping"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, str):
        return f"the text {value[:30]!r}"
    return type(value).__name__


def _hit_matches(hit: str, ground_truth: str) -> bool:
    """Whether a retrieved hit matches the ground-truth target."""
    if not hit or not ground_truth:
        return False
    if hit == ground_truth:
        return True
    if hit.casefold() == ground_truth.casefold():
        return True
    pattern = rf"(?:^|[\s/\\#._:-]){re.escape(ground_truth.casefold())}(?:$|[\s/\\#._:-])"
    return bool(re.search(pattern, hit.casefold()))


def compute_recall_any_at_k(ranked_hits: list[str], ground_truths: list[str], k: int) -> float:
    """Whether any one ground-truth target is retrieved within the top K hits."""
    if k <= 0 or not ground_truths:
        return 0.0
    top = ranked_hits[:k]
    return 1.0 if any(_hit_matches(hit, want) for hit in top for want in ground_truths) else 0.0


def compute_recall_all_at_k(ranked_hits: list[str], ground_truths: list[str], k: int) -> float:
    """Whether every ground-truth target is retrieved within the top K hits."""
    if k <= 0 or not ground_truths:
        return 0.0
    top = ranked_hits[:k]
    hits_wanted = (any(_hit_matches(hit, want) for hit in top) for want in ground_truths)
    return 1.0 if all(hits_wanted) else 0.0


def compute_ndcg_at_k_multi(ranked_hits: list[str], ground_truths: list[str], k: int = 10) -> float:
    """NDCG@K over a session ranking that carries one row per session, all targets relevant.

    Every position holding a relevant target contributes 1.0 / log2(position + 2), and
    the ideal ranking places all targets first, so a ranking that misses a target scores
    below 1.0 even when what it did find ranks first.
    """
    if k <= 0 or not ground_truths:
        return 0.0
    dcg = sum(
        1.0 / math.log2(rank_idx + 2)
        for rank_idx, hit in enumerate(ranked_hits[:k])
        if any(_hit_matches(hit, want) for want in ground_truths)
    )
    ideal = sum(1.0 / math.log2(rank_idx + 2) for rank_idx in range(min(len(ground_truths), k)))
    return dcg / ideal if ideal else 0.0


def compute_recall_at_k(ranked_hits: list[str], ground_truth: str, k: int) -> float:
    """Compute retrieval Recall@K for a single ground-truth target."""
    return compute_recall_any_at_k(ranked_hits, [ground_truth], k)


def compute_ndcg_at_k(ranked_hits: list[str], ground_truth: str, k: int = 10) -> float:
    """Compute ranking quality NDCG@K for a single ground-truth target."""
    return compute_ndcg_at_k_multi(ranked_hits, [ground_truth], k)


@dataclass
class BenchmarkReport:
    """Summary of LongMemEval-S session-level retrieval metrics.

    The harness scores raw transcript retrieval - evidence rows and raw-dump digest
    files - so these numbers are a raw-retrieval baseline for the index, not a
    measure of the digest and dream pipeline's curated memory.
    """

    recall_any_at_1: float = 0.0
    recall_any_at_3: float = 0.0
    recall_any_at_5: float = 0.0
    recall_any_at_10: float = 0.0
    recall_all_at_10: float = 0.0
    ndcg_at_10: float = 0.0
    avg_latency_ms: float = 0.0
    peak_rss_mb: float = 0.0
    total_queries: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "recall_any@1": self.recall_any_at_1,
            "recall_any@3": self.recall_any_at_3,
            "recall_any@5": self.recall_any_at_5,
            "recall_any@10": self.recall_any_at_10,
            "recall_all@10": self.recall_all_at_10,
            "ndcg@10": self.ndcg_at_10,
            "avg_latency_ms": self.avg_latency_ms,
            "peak_rss_mb": self.peak_rss_mb,
            "total_queries": self.total_queries,
        }

    def format_table(self) -> str:
        lines = [
            "=" * 60,
            "LongMemEval-S Retrieval Benchmark (raw-retrieval baseline)",
            "=" * 60,
            f"Queries evaluated : {self.total_queries}",
            f"Recall-any@1      : {self.recall_any_at_1:.4f}",
            f"Recall-any@3      : {self.recall_any_at_3:.4f}",
            f"Recall-any@5      : {self.recall_any_at_5:.4f}",
            f"Recall-any@10     : {self.recall_any_at_10:.4f}",
            f"Recall-all@10     : {self.recall_all_at_10:.4f}",
            f"Session NDCG@10   : {self.ndcg_at_10:.4f}",
            f"Avg Latency       : {self.avg_latency_ms:.2f} ms",
            f"Peak RSS          : {self.peak_rss_mb:.2f} MB",
            "=" * 60,
        ]
        return "\n".join(lines)

    def __str__(self) -> str:
        return self.format_table()
