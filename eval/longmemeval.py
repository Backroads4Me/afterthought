#!/usr/bin/env python3
"""LongMemEval-S benchmark runner for afterthought.

Evaluates session-level retrieval (Recall-any@1/3/5/10, Recall-all@10, Session
NDCG@10) against the published LongMemEval-S schema, one question at a time, each
in an isolated temporary AFT_HOME and vault.

Each question is asked of its own haystack: the question's sessions are ingested
alone into a fresh vault, stored as evidence with the dates the dataset carries,
and mirrored as digest files that dump the same transcript text. No digest or
dream pipeline runs over those sessions, so the digests are raw dumps and the
scores measure FTS5 retrieval over raw transcripts - a raw-retrieval baseline for
the index, not a measure of afterthought's curated memory.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import resource
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from aft import config as configmod
from aft import evidence, index, search, vault
from aft.config import Config
from aft.eval import (
    BenchmarkReport,
    compute_ndcg_at_k_multi,
    compute_recall_all_at_k,
    compute_recall_any_at_k,
)
from aft.readers import Entry

# The agent and project every ingested session is filed under: LongMemEval
# sessions carry no agent or project of their own.
AGENT = "claude"
PROJECT = "default"

# How many ranked sessions the session-level metrics read.
SESSION_K = 10

# A session reaches search through at most two rows - one evidence row (search
# keeps the best row per session) and one digest row - so the raw hit limit is a
# small multiple of the session cutoff.
HITS_PER_SESSION = 2

# The date formats a LongMemEval date field arrives in, most specific first.
_DATE_FORMATS = (
    "%Y/%m/%d (%a) %H:%M",
    "%Y/%m/%d (%a) %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d",
    "%Y-%m-%d",
)

# The session id a digest file name carries, after its date and agent.
_DIGEST_STEM = re.compile(r"^\d{4}-\d{2}-\d{2}-[^-]+-(?P<session>.+)$")


@dataclass(frozen=True)
class QuestionCase:
    """One benchmark question: its haystack sessions, their dates, and the answers.

    Sessions and ids run in parallel. Every entry of a session carries that
    session's own date, and ``question_date`` is the reference time the question
    is asked at.
    """

    question: str
    question_type: str
    question_date: datetime | None
    session_ids: tuple[str, ...]
    sessions: tuple[tuple[Entry, ...], ...]
    answer_session_ids: tuple[str, ...]


@contextmanager
def isolated_vault() -> Iterator[Config]:
    """Create an isolated temporary AFT_HOME and vault sandbox."""
    with tempfile.TemporaryDirectory(prefix="aft-longmemeval-") as temp_dir:
        temp_path = Path(temp_dir)
        home_dir = temp_path / "home"
        vault_dir = temp_path / "vault"
        home_dir.mkdir(parents=True, exist_ok=True)
        vault_dir.mkdir(parents=True, exist_ok=True)

        git_env = {
            "GIT_AUTHOR_NAME": "Eval Runner",
            "GIT_AUTHOR_EMAIL": "eval@example.com",
            "GIT_COMMITTER_NAME": "Eval Runner",
            "GIT_COMMITTER_EMAIL": "eval@example.com",
        }

        config_file = home_dir / "config.toml"
        config_file.write_text(f'vault = "{vault_dir}"\nhost = "evalhost"\n', encoding="utf-8")

        old_home = os.environ.get("AFT_HOME")
        saved_git = {k: os.environ.get(k) for k in git_env}
        try:
            os.environ["AFT_HOME"] = str(home_dir)
            for k, v in git_env.items():
                os.environ[k] = v
            cfg = configmod.load()
            vault.init(cfg)
            yield cfg
        finally:
            if old_home is not None:
                os.environ["AFT_HOME"] = old_home
            else:
                os.environ.pop("AFT_HOME", None)
            for k, val in saved_git.items():
                if val is not None:
                    os.environ[k] = val
                else:
                    os.environ.pop(k, None)


def load_dataset(path: Path | str) -> Any:
    """Load evaluation dataset from JSON or YAML file."""
    target = Path(path).expanduser().resolve()
    if not target.is_file():
        raise FileNotFoundError(f"dataset file not found: {target}")
    content = target.read_text(encoding="utf-8")
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return yaml.safe_load(content)


def _parse_dataset_date(value: Any) -> datetime | None:
    """A dataset date as a UTC datetime, or None when absent or unreadable."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _session_id_value(value: Any) -> str:
    """A session id from a plain id or an id-carrying record."""
    if isinstance(value, dict):
        return str(value.get("id") or value.get("session_id") or "")
    return str(value) if value is not None else ""


def _entries_of_session(record: Any, when: datetime) -> tuple[Entry, ...]:
    """One session's turns as entries, every turn stamped with the session's date.

    The published schema stores a session as a list of turn records; the runner's
    own fixtures may hand over a dict with its turns under ``messages`` (or
    ``turns``, ``entries``, ``history``) or a bare text body.
    """
    if isinstance(record, list):
        turns: list[Any] = record
    elif isinstance(record, dict):
        turns = (
            record.get("messages")
            or record.get("turns")
            or record.get("entries")
            or record.get("history")
            or []
        )
        if not isinstance(turns, list):
            turns = []
        if not turns and ("content" in record or "text" in record):
            turns = [record]
    else:
        turns = []
    entries: list[Entry] = []
    for idx, turn in enumerate(turns):
        if isinstance(turn, dict):
            role = str(turn.get("role") or "user")
            text = str(turn.get("content") or turn.get("text") or "")
            entry_id = str(turn.get("id") or f"e-{idx}")
        else:
            role = "user"
            text = str(turn)
            entry_id = f"e-{idx}"
        entries.append(Entry(id=entry_id, time=when, role=role, text=text, is_error=False))
    return tuple(entries)


def _parse_haystack(
    item: dict[str, Any], fallback: datetime
) -> tuple[list[str], list[tuple[Entry, ...]]]:
    """A question's haystack as parallel session ids and entry tuples.

    Ids come from the parallel ``haystack_session_ids`` first, then from a session
    record that carries its own id, then from the position in the list. Session
    dates come from the parallel ``haystack_dates``; a session without one is
    stamped with ``fallback``.
    """
    raw_sessions = item.get("haystack_sessions")
    if raw_sessions is None:
        raw_sessions = item.get("sessions")
    if not isinstance(raw_sessions, list):
        raw_sessions = []
    raw_ids = item.get("haystack_session_ids")
    if not isinstance(raw_ids, list):
        raw_ids = []
    raw_dates = item.get("haystack_dates")
    if not isinstance(raw_dates, list):
        raw_dates = []

    ids: list[str] = []
    sessions: list[tuple[Entry, ...]] = []
    for pos, record in enumerate(raw_sessions):
        session_id = _session_id_value(raw_ids[pos]) if pos < len(raw_ids) else ""
        if not session_id and isinstance(record, dict):
            session_id = str(record.get("session_id") or record.get("id") or "")
        if not session_id:
            session_id = f"session-{pos}"
        when = _parse_dataset_date(raw_dates[pos]) if pos < len(raw_dates) else None
        ids.append(session_id)
        sessions.append(_entries_of_session(record, when or fallback))
    return ids, sessions


def _answer_session_ids(item: dict[str, Any]) -> tuple[str, ...]:
    """The sessions that hold the answer, from answer_session_ids or its fallbacks."""
    raw = item.get("answer_session_ids")
    if isinstance(raw, list):
        return tuple(sid for sid in (_session_id_value(el) for el in raw) if sid)
    for key in (
        "answer_session_ids",
        "answer_session_id",
        "ground_truth",
        "target",
        "expected_path",
        "answer",
    ):
        session_id = _session_id_value(item.get(key))
        if session_id:
            return (session_id,)
    return ()


def _question_case(
    item: dict[str, Any],
    shared: tuple[list[str], list[tuple[Entry, ...]]] | None = None,
) -> QuestionCase | None:
    """One dataset item as a question case, or None when it names no question.

    ``shared`` carries sessions pooled at the top of a wrapper document; they join
    every question's haystack after the item's own sessions.
    """
    question = str(item.get("question") or item.get("query") or "").strip()
    if not question:
        return None
    question_date = _parse_dataset_date(item.get("question_date"))
    ids, sessions = _parse_haystack(item, question_date or datetime.now(UTC))
    if shared is not None:
        shared_ids, shared_sessions = shared
        known = set(ids)
        for session_id, entries in zip(shared_ids, shared_sessions, strict=True):
            if session_id not in known:
                known.add(session_id)
                ids.append(session_id)
                sessions.append(entries)
    return QuestionCase(
        question=question,
        question_type=str(item.get("question_type") or ""),
        question_date=question_date,
        session_ids=tuple(ids),
        sessions=tuple(sessions),
        answer_session_ids=_answer_session_ids(item),
    )


def parse_questions(data: Any) -> list[QuestionCase]:
    """Parse a dataset into one question case per question, each with its own haystack.

    The published LongMemEval-S file is a list of question items whose
    ``haystack_sessions`` hold lists of turns, with ids and dates in the parallel
    ``haystack_session_ids`` and ``haystack_dates``. A wrapper document with a
    ``queries`` (or ``items``, ``cases``, ``examples``, ``data``) list pools its
    top-level sessions into every question's haystack.
    """
    if isinstance(data, list):
        cases = [_question_case(item) for item in data if isinstance(item, dict)]
        return [case for case in cases if case is not None]
    if isinstance(data, dict):
        raw_queries: Any = None
        for key in ("queries", "items", "cases", "examples", "data"):
            if isinstance(data.get(key), list):
                raw_queries = data[key]
                break
        if raw_queries is None:
            if "question" in data or "query" in data:
                case = _question_case(data)
                return [case] if case is not None else []
            return []
        shared = _parse_haystack(data, datetime.now(UTC))
        cases = [_question_case(item, shared) for item in raw_queries if isinstance(item, dict)]
        return [case for case in cases if case is not None]
    return []


def ingest_question(config: Config, case: QuestionCase) -> None:
    """Store one question's haystack in its vault as evidence and digests, then index.

    Every session is archived as evidence with the date the dataset gave it, and
    mirrored as a digest file that dumps the same transcript text, so both the
    evidence and digest branches of the index carry the haystack. The digests are
    raw dumps: no digest pipeline runs here, which is what makes the benchmark a
    raw-retrieval baseline.
    """
    for session_id, entries in zip(case.session_ids, case.sessions, strict=True):
        if not entries:
            continue
        evidence.store(config, AGENT, session_id, PROJECT, entries)
        when = entries[0].time
        digest_dir = config.vault / "digests" / PROJECT / f"{when.year}" / f"{when.month:02d}"
        digest_dir.mkdir(parents=True, exist_ok=True)
        digest_body = "\n\n".join(entry.text for entry in entries)
        digest_content = (
            f"---\n"
            f"type: Session Digest\n"
            f'title: "{session_id}"\n'
            f"aft:\n"
            f"  project: {PROJECT}\n"
            f"---\n\n"
            f"# {session_id}\n\n"
            f"## Summary\n\n"
            f"{digest_body}\n"
        )
        digest_file = digest_dir / f"{when:%Y-%m-%d}-{AGENT}-{session_id}.md"
        digest_file.write_text(digest_content, encoding="utf-8")
    index.reindex(config)


def _session_of_hit(path: str) -> str | None:
    """The session a hit belongs to, or None for a hit that names no session.

    An evidence hit carries an ``aft://session/`` resource whose last segment is
    the session id; a digest hit's file name carries it after the date and agent.
    """
    if path.startswith("aft://session/"):
        session_id = path.removeprefix("aft://session/").partition("#")[0].rsplit("/", 1)[-1]
        return session_id or None
    match = _DIGEST_STEM.match(Path(path).stem)
    return match.group("session") if match else None


def ranked_sessions(hits: list[search.Hit], allowed: set[str] | None = None) -> list[str]:
    """The sessions a query ranked, de-duplicated in order of first appearance.

    A session reaches search through two rows - its evidence entries and its
    digest file - and a session filling several hit rows would be counted once per
    row, so the session ranking keeps each session's best row position. Hits that
    name no session, and sessions outside ``allowed`` when it is given, are not
    part of the ranking.
    """
    ranked: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        session_id = _session_of_hit(hit.path)
        if session_id is None or session_id in seen:
            continue
        if allowed is not None and session_id not in allowed:
            continue
        seen.add(session_id)
        ranked.append(session_id)
    return ranked


def run_benchmark(
    dataset_path: Path | str,
    limit: int | None = None,
    output_path: Path | str | None = None,
) -> BenchmarkReport:
    """Run LongMemEval-S over per-question isolated vaults and report session metrics.

    Each question is asked of its own haystack: the question's sessions are
    ingested alone into a fresh vault, the question is searched with its
    ``question_date`` as the reference time, and the ranking is read at session
    level. Latency covers the search only, not the ingest and reindex around it.
    """
    questions = parse_questions(load_dataset(dataset_path))
    if limit is not None and limit > 0:
        questions = questions[:limit]

    recalls_any_1: list[float] = []
    recalls_any_3: list[float] = []
    recalls_any_5: list[float] = []
    recalls_any_10: list[float] = []
    recalls_all_10: list[float] = []
    ndcgs_10: list[float] = []
    latencies_ms: list[float] = []
    query_records: list[dict[str, Any]] = []

    for case in questions:
        with isolated_vault() as config:
            ingest_question(config, case)
            t0 = time.perf_counter()
            hits = search.search(
                config,
                case.question,
                all_projects=True,
                limit=SESSION_K * HITS_PER_SESSION,
                now=case.question_date,
            )
            t1 = time.perf_counter()
        latency = (t1 - t0) * 1000.0
        latencies_ms.append(latency)

        sessions_ranked = ranked_sessions(hits, allowed=set(case.session_ids))[:SESSION_K]
        answers = list(case.answer_session_ids)
        recall_any = {
            k: compute_recall_any_at_k(sessions_ranked, answers, k) for k in (1, 3, 5, 10)
        }
        recall_all_10 = compute_recall_all_at_k(sessions_ranked, answers, SESSION_K)
        ndcg_10 = compute_ndcg_at_k_multi(sessions_ranked, answers, SESSION_K)

        recalls_any_1.append(recall_any[1])
        recalls_any_3.append(recall_any[3])
        recalls_any_5.append(recall_any[5])
        recalls_any_10.append(recall_any[10])
        recalls_all_10.append(recall_all_10)
        ndcgs_10.append(ndcg_10)

        query_records.append(
            {
                "question": case.question,
                "question_type": case.question_type,
                "question_date": case.question_date.isoformat() if case.question_date else None,
                "answer_session_ids": answers,
                "haystack_sessions": len(case.session_ids),
                "ranked_sessions": sessions_ranked,
                "recall_any@1": recall_any[1],
                "recall_any@3": recall_any[3],
                "recall_any@5": recall_any[5],
                "recall_any@10": recall_any[10],
                "recall_all@10": recall_all_10,
                "ndcg@10": ndcg_10,
                "latency_ms": latency,
            }
        )

    total = len(recalls_any_1)

    def mean(values: list[float]) -> float:
        return (sum(values) / total) if total else 0.0

    report = BenchmarkReport(
        recall_any_at_1=mean(recalls_any_1),
        recall_any_at_3=mean(recalls_any_3),
        recall_any_at_5=mean(recalls_any_5),
        recall_any_at_10=mean(recalls_any_10),
        recall_all_at_10=mean(recalls_all_10),
        ndcg_at_10=mean(ndcgs_10),
        avg_latency_ms=mean(latencies_ms),
        peak_rss_mb=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0,
        total_queries=total,
    )

    print(report.format_table())

    if output_path is not None:
        out_target = Path(output_path).expanduser().resolve()
        out_target.parent.mkdir(parents=True, exist_ok=True)
        payload = report.to_dict()
        payload["queries"] = query_records
        out_target.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return report


def main(args: list[str] | None = None) -> int:
    """CLI entrypoint for benchmark runner."""
    parser = argparse.ArgumentParser(
        description="LongMemEval-S evaluation benchmark harness for afterthought.",
    )
    parser.add_argument(
        "--dataset",
        required=True,
        type=Path,
        help="Path to evaluation dataset file (JSON or YAML)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of queries to evaluate",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional path to write benchmark results as JSON",
    )
    parsed = parser.parse_args(args)
    run_benchmark(
        dataset_path=parsed.dataset,
        limit=parsed.limit,
        output_path=parsed.output,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
