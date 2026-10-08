#!/usr/bin/env python3
"""LongMemEval-S benchmark runner for afterthought.

Each question is asked of its own haystack: the question's sessions are ingested
alone into a fresh vault, stored as evidence with the dates the dataset carries,
and mirrored as digest files that dump the same transcript text. No digest or
dream pipeline runs over those sessions, so the digests are raw dumps and the
scores measure FTS5 retrieval over raw transcripts - a raw-retrieval baseline for
the index, not a measure of afterthought's curated memory.

``--mode pipeline`` (BUILD-PLAN F71) runs the real capture, digest and dream path
instead: each session is written as a Claude transcript and enqueued in haystack
date order, ``work.run`` and a dream run follow once per session date with ``now``
just after that date, and the question is scored three ways - the digest rows
alone, the fraction of answer sessions any page line cites, and the production
search with a page hit credited to the sessions its matched line cites. Each
question runs in its own sandbox under ``--workdir``: a home, a vault and a git
repository registered as the sandbox's one project, outside the system temporary
directory because a directory under it resolves to no project (DESIGN 7, F49).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from aft import (
    capture,
    cli,
    db,
    evidence,
    index,
    inject,
    keys,
    model,
    page,
    queue,
    readers,
    search,
    vault,
    work,
)
from aft import config as configmod
from aft import filter as filtermod
from aft import registry as registrymod
from aft.config import Config
from aft.dream import schedule
from aft.eval import (
    BenchmarkReport,
    compute_ndcg_at_k_multi,
    compute_recall_all_at_k,
    compute_recall_any_at_k,
)
from aft.events import SESSION_END, SessionEvent
from aft.readers import ASSISTANT, CLAUDE, Entry

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
    is asked at. ``question_id`` is the id the dataset item carries, so ``--ids``
    and ``--resume`` can name questions; a fixture without one takes its position
    in the file, ``q-1`` first.
    """

    question: str
    question_type: str
    question_date: datetime | None
    session_ids: tuple[str, ...]
    sessions: tuple[tuple[Entry, ...], ...]
    answer_session_ids: tuple[str, ...]
    question_id: str = ""


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
    question_id: str = "",
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
        question_id=question_id,
    )


def parse_questions(data: Any) -> list[QuestionCase]:
    """Parse a dataset into one question case per question, each with its own haystack.

    The published LongMemEval-S file is a list of question items whose
    ``haystack_sessions`` hold lists of turns, with ids and dates in the parallel
    ``haystack_session_ids`` and ``haystack_dates``. A wrapper document with a
    ``queries`` (or ``items``, ``cases``, ``examples``, ``data``) list pools its
    top-level sessions into every question's haystack. Each case takes the item's
    own id when it carries one, else its position in the file, ``q-1`` first.
    """
    if isinstance(data, list):
        cases = (
            _question_case(item, question_id=_case_id(item, position))
            for position, item in enumerate(data, start=1)
            if isinstance(item, dict)
        )
        return [case for case in cases if case is not None]
    if isinstance(data, dict):
        raw_queries: Any = None
        for key in ("queries", "items", "cases", "examples", "data"):
            if isinstance(data.get(key), list):
                raw_queries = data[key]
                break
        if raw_queries is None:
            if "question" in data or "query" in data:
                case = _question_case(data, question_id=_case_id(data, 1))
                return [case] if case is not None else []
            return []
        shared = _parse_haystack(data, datetime.now(UTC))
        cases = (
            _question_case(item, shared, question_id=_case_id(item, position))
            for position, item in enumerate(raw_queries, start=1)
            if isinstance(item, dict)
        )
        return [case for case in cases if case is not None]
    return []


def _case_id(item: dict[str, Any], position: int) -> str:
    """The id a question answers to: the dataset's own, else its position."""
    named = str(item.get("question_id") or "").strip()
    return named or f"q-{position}"


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


# ------------------------------------------------------------------ pipeline mode (F71)
#
# The sandbox one question runs in: a home, a vault and a git repository registered as
# the sandbox's one project, under --workdir. The workdir is the caller's to place
# outside the system temporary directory and ~/Downloads, because no directory under
# either becomes a project (DESIGN 7, F49) and the run asserts the project resolves to
# a slug before any session is replayed.

PIPELINE_HOST = "evalhost"
SANDBOX_BUDGET = 1_000_000_000
SANDBOX_RETENTION_DAYS = 36_500
JOBS_MAX = 3
REPLAY_AFTER = timedelta(minutes=1)
SESSION_PREFIX = "aft://session/"


class PipelineError(Exception):
    """A pipeline run that cannot start: a workdir where no project may live, a sandbox
    config that does not parse, a project that resolves to no slug."""


@dataclass(frozen=True)
class Sandbox:
    """One question's isolated home, vault and project, and the config they answer to."""

    root: Path
    home: Path
    vault: Path
    repo: Path
    config: Config


@dataclass(frozen=True)
class SessionOutcome:
    """Where one session of a haystack ended after the replay."""

    session_id: str
    outcome: str


DIGESTED = "digested"
EMPTY = "empty"
BELOW_FLOOR = "below the floor"
FAILED = "failed"
LEFT = "left queued"


@dataclass
class ReplayResult:
    """What one question's replay did, as the record reports it."""

    outcomes: list[SessionOutcome] = field(default_factory=list)
    stopped: str = ""
    failures: list[str] = field(default_factory=list)
    dream_failures: dict[str, str] = field(default_factory=dict)
    dream_deferred: list[str] = field(default_factory=list)


def _safe_name(raw: str) -> str:
    """A name a filesystem takes: any other character becomes an underscore."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", raw) or "unnamed"


def _session_file(root: Path, session_id: str) -> Path:
    """Where one replayed session's transcript is written."""
    return root / "sessions" / f"{_safe_name(session_id)}.jsonl"


def write_claude_transcript(path: Path, entries: Sequence[Entry], cwd: Path) -> Path:
    """One session as a Claude Code transcript, every record dated as the dataset gave it.

    The reader reads user and assistant entries back with their own times (M2.1), so the
    digest sees the dataset's dates, not the runner's clock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"cwd": str(cwd)}) + "\n")
        for entry in entries:
            message: dict[str, Any] = {"content": [{"type": "text", "text": entry.text}]}
            if entry.role == ASSISTANT:
                message["model"] = "claude-real"
            handle.write(
                json.dumps(
                    {
                        "type": entry.role,
                        "uuid": entry.id,
                        "timestamp": entry.time.isoformat(),
                        "message": message,
                    }
                )
                + "\n"
            )
    return path


def _toml_value(value: Any) -> str:
    """One value as TOML writes it inline: tables as ``{ key = value }``, the rest as JSON
    spells strings, numbers, booleans and arrays, which TOML reads the same way."""
    if isinstance(value, dict):
        items = ", ".join(f"{json.dumps(str(k))} = {_toml_value(v)}" for k, v in value.items())
        return "{ " + items + " }" if items else "{}"
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return json.dumps(value)


def _model_table(caller: Config) -> str:
    """The caller's ``[model]`` table, as the sandbox's config carries it.

    Every field the caller set goes over: an endpoint that needs a key or a request
    field would otherwise answer differently in the sandbox than at home.
    """
    m = caller.settings.model
    lines = ["[model]", f'endpoint = "{m.endpoint}"']
    if m.name:
        lines.append(f'name = "{m.name}"')
    if m.api_key_env:
        lines.append(f'api_key_env = "{m.api_key_env}"')
    if m.temperature:
        lines.append(f"temperature = {m.temperature!r}")
    if m.extra_body:
        lines.append(f"extra_body = {_toml_value(m.extra_body)}")
    return "\n".join(lines) + "\n"


def _sandbox_config_text(vault_dir: Path, caller: Config) -> str:
    """The sandbox's config.toml: the caller's model, caps that never bind, no quiet time.

    The budget and the retention window are set high enough never to bind, and the dream
    quiet period is zero so a dream run follows its digest run at once (BUILD-PLAN F71).
    """
    text = (
        f'vault = "{vault_dir}"\n'
        f'host = "{PIPELINE_HOST}"\n'
        "\n"
        f"{_model_table(caller)}\n"
        "[work]\n"
        f"daily_token_budget = {SANDBOX_BUDGET}\n"
        "\n"
        "[evidence]\n"
        f"retention_days = {SANDBOX_RETENTION_DAYS}\n"
        "\n"
        "[dream]\n"
        "quiet_minutes = 0\n"
    )
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PipelineError(f"the sandbox config does not parse: {exc}") from exc
    return text


def _init_repo(repo: Path) -> None:
    """A git repository with one commit, the shape a project root resolves from."""
    from aft import gitutil

    gitutil.git(["init", "-q"], cwd=repo)
    (repo / "README.md").write_text("# sandbox project\n", encoding="utf-8")
    gitutil.git(["add", "README.md"], cwd=repo)
    gitutil.git(["commit", "-q", "-m", "init"], cwd=repo)


@contextmanager
def pipeline_sandbox(root: Path, caller: Config, keep: dict[str, bool]) -> Iterator[Sandbox]:
    """One question's sandbox, registered as the runner's world for the body's duration.

    The environment (AFT_HOME, the git identity) and the sweep's session directories are
    the process's own, so they are saved and restored around the body; the sweep sees no
    session directories at all, because the replay enqueues each transcript itself, the
    way a hook does. ``keep`` is the caller's decision at the end of the body: a failed
    or stopped question's sandbox is always kept, and --keep-sandbox keeps every one.
    """
    root.mkdir(parents=True, exist_ok=True)
    home = root / "home"
    vault_dir = root / "vault"
    repo = root / "project"
    home.mkdir()
    vault_dir.mkdir()
    (home / configmod.CONFIG_NAME).write_text(
        _sandbox_config_text(vault_dir, caller), encoding="utf-8"
    )
    old_home = os.environ.get(configmod.HOME_ENV)
    git_env = {
        "GIT_AUTHOR_NAME": "Eval Runner",
        "GIT_AUTHOR_EMAIL": "eval@example.com",
        "GIT_COMMITTER_NAME": "Eval Runner",
        "GIT_COMMITTER_EMAIL": "eval@example.com",
    }
    saved_git = {k: os.environ.get(k) for k in git_env}
    saved_dirs = capture.SESSION_DIRS
    try:
        os.environ[configmod.HOME_ENV] = str(home)
        for name, value in git_env.items():
            os.environ[name] = value
        capture.SESSION_DIRS = {}
        cfg = configmod.load()
        vault.init(cfg)
        repo.mkdir()
        _init_repo(repo)
        if cli.main(["project", "add", str(repo)]) != 0:
            raise PipelineError(f"the sandbox project did not register: {repo}")
        resolution = keys.resolve(repo, registrymod.Registry.load(cfg.registry_path), cfg.host)
        if resolution.slug is None:
            raise PipelineError(f"the sandbox project resolved to no slug: {repo}")
        yield Sandbox(root=root, home=home, vault=vault_dir, repo=repo, config=cfg)
    finally:
        if old_home is not None:
            os.environ[configmod.HOME_ENV] = old_home
        else:
            os.environ.pop(configmod.HOME_ENV, None)
        for name, value in saved_git.items():
            if value is not None:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)
        capture.SESSION_DIRS = saved_dirs
        if not keep.get("keep", False):
            shutil.rmtree(root, ignore_errors=True)


def _replay_order(case: QuestionCase) -> list[tuple[datetime, str, tuple[Entry, ...]]]:
    """The haystack in session-date order, dataset order kept within one date.

    A session's date is its entries' own (every entry carries it); a session with no
    entries takes the question's date, the same fallback the parser stamped it with.
    """
    dated = []
    for session_id, entries in zip(case.session_ids, case.sessions, strict=True):
        when = entries[0].time if entries else (case.question_date or datetime.now(UTC))
        dated.append((when, session_id, entries))
    dated.sort(key=lambda item: item[0])
    return dated


def replay(sandbox: Sandbox, case: QuestionCase) -> ReplayResult:
    """Replay one question's haystack through capture, digest and dream, in date order.

    Each session is written as a Claude transcript and enqueued the way a hook enqueues
    a session end. For each session date in order, ``work.run`` then one dream run
    happen with ``now`` just after that date, so supersede and short-term expiry see the
    dataset's order (BUILD-PLAN F71). A run that stops leaves its reason in ``stopped``
    and its jobs queued, where the next date's run picks them up.
    """
    cfg = sandbox.config
    result = ReplayResult()
    skipped: set[str] = set()
    for when, session_id, entries in _replay_order(case):
        if not entries:
            # A session with nothing to say is decided now: the baseline ingests it the
            # same way, and a transcript of no records is not one the reader reads.
            skipped.add(session_id)
            continue
        transcript = _session_file(sandbox.root, session_id)
        write_claude_transcript(transcript, entries, sandbox.repo)
        queue.enqueue(
            cfg,
            SessionEvent(
                agent=CLAUDE,
                kind=SESSION_END,
                session_id=session_id,
                cwd=sandbox.repo,
                transcript_path=transcript,
                time=when,
                detail=None,
            ),
        )
        now = when + REPLAY_AFTER
        report = work.run(cfg, now=now)
        result.failures.extend(report.failures)
        if report.stopped:
            result.stopped = report.stopped
        dream = schedule.run(cfg, now=now)
        result.dream_failures.update(dream.failures)
        result.dream_deferred.extend(dream.over_budget + dream.budget_stopped)
    result.outcomes = [SessionOutcome(sid, EMPTY) for sid in case.session_ids if sid in skipped]
    result.outcomes.extend(_classify(sandbox, case, skipped))
    return result


def _classify(sandbox: Sandbox, case: QuestionCase, skipped: set[str]) -> list[SessionOutcome]:
    """Each replayed session's outcome, read from the queue, the vault and the transcript.

    A digest page standing for the session is digested; a failed job row is failed; a
    row still open is work the run left queued. A closed job with no page either read
    nothing at all or stopped under the two-prompt floor, which the transcript itself
    decides, or the filter emptied it, which closed with a watermark and is empty.
    """
    cfg = sandbox.config
    states = _session_states(cfg)
    digested = _digested_sessions(cfg)
    outcomes: list[SessionOutcome] = []
    for session_id in case.session_ids:
        if session_id in skipped:
            continue
        status, watermark = states.get(session_id, ("", None))
        if session_id in digested:
            outcome = DIGESTED
        elif status == queue.FAILED:
            outcome = FAILED
        elif status != queue.DONE:
            outcome = LEFT
        elif watermark is None:
            outcome = _unread(sandbox, session_id)
        else:
            outcome = EMPTY
        outcomes.append(SessionOutcome(session_id, outcome))
    return outcomes


def _session_states(config: Config) -> dict[str, tuple[str, str | None]]:
    """Each session's job status and its digest watermark, from the sandbox's queue."""
    path = config.home / queue.QUEUE_NAME
    if not path.is_file():
        return {}
    states: dict[str, tuple[str, str | None]] = {}
    conn = db.connect(path)
    try:
        marks = dict(conn.execute("SELECT session_id, watermark FROM sessions"))
        for session_id, status in conn.execute("SELECT session_id, status FROM jobs"):
            states[session_id] = (status, marks.get(session_id))
    finally:
        conn.close()
    return states


def _digested_sessions(config: Config) -> set[str]:
    """The sessions a digest page stands for, by the name the page carries."""
    found: set[str] = set()
    root = config.vault / schedule.DIGESTS
    if not root.is_dir():
        return found
    for path in root.glob("*/*/*/*.md"):
        match = _DIGEST_STEM.match(path.stem)
        if match:
            found.add(match.group("session"))
    return found


def _unread(sandbox: Sandbox, session_id: str) -> str:
    """Whether a session that closed with no page and no watermark was under the floor."""
    try:
        entries = list(readers.read(CLAUDE, _session_file(sandbox.root, session_id)))
    except readers.ReaderError:
        return EMPTY
    if not entries:
        return EMPTY
    if filtermod.prompt_count(entries) < filtermod.MIN_SESSION_PROMPTS:
        return BELOW_FLOOR
    return EMPTY


def _terms(query: str) -> frozenset[str]:
    """The words a query is credited by, lowercased and split on non-word characters."""
    return frozenset(re.findall(r"\w+", query.lower()))


def _session_of_source(resource: str) -> str | None:
    """The session an ``aft://session/`` source cites, or None for any other resource."""
    if not resource.startswith(SESSION_PREFIX):
        return None
    parts = resource.removeprefix(SESSION_PREFIX).split("/")
    if len(parts) < 2:
        return None
    return parts[1].partition("#")[0] or None


def _page_hit_sessions(config: Config, hit: search.Hit, query: str) -> list[str]:
    """The sessions the line a page hit matched cites, never the section's every line.

    The pages collection indexes a page's active lines one section to a row, so a hit
    names a section, not a line. The matched line is the section's line that shares the
    most of the query's words with it, the first on a tie; a section whose every line
    shares none credits no session. Only that line's own sources are credited (BUILD-PLAN
    F71): the sessions a section's other lines cite say nothing about this hit.
    """
    rel = PurePosixPath(hit.path)
    if rel.is_absolute() or ".." in rel.parts:
        return []
    try:
        text = (config.vault / Path(rel)).read_text(encoding="utf-8")
    except OSError:
        return []
    frontmatter, _, _, error = page.split_frontmatter(text)
    if error or not isinstance(frontmatter, dict):
        return []
    sources = {
        str(source.get("id")): str(source.get("resource") or "")
        for source in frontmatter.get("sources") or []
        if isinstance(source, dict)
    }
    parsed = page.parse(text, Path(rel))
    terms = _terms(query)
    best: tuple[int, page.Fact] | None = None
    for fact in parsed.facts:
        if fact.section != hit.heading or fact.struck or fact.state not in inject.INJECTED:
            continue
        score = len(terms & _terms(fact.statement))
        if score and (best is None or score > best[0]):
            best = (score, fact)
    if best is None:
        return []
    sessions: list[str] = []
    for ref in best[1].refs:
        session_id = _session_of_source(sources.get(ref, ""))
        if session_id and session_id not in sessions:
            sessions.append(session_id)
    return sessions


def _hit_sessions(config: Config, hit: search.Hit, query: str) -> list[str]:
    """The sessions one search hit answers for, by what named it."""
    if hit.collection == index.PAGES:
        return _page_hit_sessions(config, hit, query)
    session_id = _session_of_hit(hit.path)
    return [session_id] if session_id else []


def ranked_full(
    hits: list[search.Hit], config: Config, query: str, allowed: set[str] | None = None
) -> list[str]:
    """The sessions a production search ranked, in order of first appearance.

    A page hit credits the sessions its matched line cites, a digest hit the session its
    file name carries, an evidence hit the session its resource names; a session filling
    several hits ranks once, at its best position. Sessions outside ``allowed`` when it
    is given, and hits that name no session, are not part of the ranking.
    """
    ranked: list[str] = []
    seen: set[str] = set()
    for hit in hits:
        for session_id in _hit_sessions(config, hit, query):
            if session_id in seen or (allowed is not None and session_id not in allowed):
                continue
            seen.add(session_id)
            ranked.append(session_id)
    return ranked


def _cited_sessions(config: Config) -> set[str]:
    """Every session any page line's source cites, over the project and global pages."""
    cited: set[str] = set()
    for root in (config.vault / "projects", config.vault / "global"):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            frontmatter, _, _, error = page.split_frontmatter(text)
            if error or not isinstance(frontmatter, dict):
                continue
            for source in frontmatter.get("sources") or []:
                if not isinstance(source, dict):
                    continue
                session_id = _session_of_source(str(source.get("resource") or ""))
                if session_id:
                    cited.add(session_id)
    return cited


def coverage_fraction(config: Config, answers: Sequence[str]) -> float:
    """The fraction of answer sessions any page line's source cites.

    An upper bound for curated memory rather than a ranking: a page line resting on a
    session says the pipeline read that session at all, whether or not the line survived
    or answers anything (BUILD-PLAN F71).
    """
    wanted = set(answers)
    if not wanted:
        return 0.0
    return len(wanted & _cited_sessions(config)) / len(wanted)


def _session_metrics(ranked: list[str], answers: list[str]) -> dict[str, float]:
    """The session-level metrics one ranking scores, as the baseline reports them."""
    return {
        "recall_any@1": compute_recall_any_at_k(ranked, answers, 1),
        "recall_any@3": compute_recall_any_at_k(ranked, answers, 3),
        "recall_any@5": compute_recall_any_at_k(ranked, answers, 5),
        "recall_any@10": compute_recall_any_at_k(ranked, answers, 10),
        "recall_all@10": compute_recall_all_at_k(ranked, answers, SESSION_K),
        "ndcg@10": compute_ndcg_at_k_multi(ranked, answers, SESSION_K),
    }


def score_question(sandbox: Sandbox, case: QuestionCase) -> dict[str, Any]:
    """The three session rankings of one question, each scored with the session metrics.

    (a) ``digests``: the digest rows only, attributed by digest file name. (b)
    ``coverage``: the fraction of answer sessions any page line's source cites, an upper
    bound for curated memory rather than a ranking. (c) ``full``: the production search
    over everything, with a page hit credited to the sessions its matched line cites.
    """
    cfg = sandbox.config
    # Search deep enough to rank the whole haystack: search has no collection filter, so
    # a shallow limit would let evidence and page rows crowd digest rows out of the
    # digest-only ranking. The full ranking keeps hit order, so its top ten is unchanged.
    hits = search.search(
        cfg,
        case.question,
        all_projects=True,
        limit=max(SESSION_K, len(case.session_ids)) * (HITS_PER_SESSION + 1),
        now=case.question_date,
    )
    allowed = set(case.session_ids)
    answers = list(case.answer_session_ids)
    digests = ranked_sessions(
        [hit for hit in hits if hit.collection == index.DIGESTS], allowed=allowed
    )[:SESSION_K]
    full = ranked_full(hits, cfg, case.question, allowed=allowed)[:SESSION_K]
    return {
        "digests": {"ranked_sessions": digests, **_session_metrics(digests, answers)},
        "full": {"ranked_sessions": full, **_session_metrics(full, answers)},
        "coverage": coverage_fraction(cfg, answers),
    }


def _prompt_hash() -> str:
    """A digest over every shipped prompt and schema, which names the prompts a run used."""
    combined = hashlib.sha256()
    root = files("aft") / "prompts"
    for item in sorted(root.iterdir(), key=lambda f: f.name):
        if item.is_file():
            combined.update(item.name.encode("utf-8"))
            combined.update(item.read_bytes())
    return combined.hexdigest()


def _require_replayable(root: Path) -> None:
    """The workdir must sit outside the directories no directory becomes a project in."""
    refused = (Path(tempfile.gettempdir()).resolve(), Path.home().resolve() / "Downloads")
    for place in refused:
        if root == place or place in root.parents:
            raise PipelineError(
                f"--workdir {root} is under {place}, where no directory becomes a project,"
                " so the sandbox project would resolve to no slug (DESIGN 7, F49)"
            )


def _outcome_counts(outcomes: Sequence[SessionOutcome]) -> dict[str, int]:
    counts = {DIGESTED: 0, EMPTY: 0, BELOW_FLOOR: 0, FAILED: 0, LEFT: 0}
    for outcome in outcomes:
        counts[outcome.outcome] = counts.get(outcome.outcome, 0) + 1
    return counts


def _record_body(
    case: QuestionCase, replayed: ReplayResult, scoring: dict[str, Any], sandbox: Sandbox
) -> dict[str, Any]:
    """The durable record of one finished question (BUILD-PLAN F71)."""
    counts = _outcome_counts(replayed.outcomes)
    cfg = sandbox.config
    return {
        "sessions": len(case.session_ids),
        "sessions_digested": counts[DIGESTED],
        "sessions_empty": counts[EMPTY],
        "sessions_below_floor": counts[BELOW_FLOOR],
        "sessions_failed": counts[FAILED],
        "sessions_left_queued": counts[LEFT],
        "stopped": replayed.stopped,
        "work_failures": replayed.failures,
        "dream_failures": replayed.dream_failures,
        "dream_deferred": replayed.dream_deferred,
        "tokens": model.tokens_used(cfg),
        "model": cfg.settings.model.name or None,
        "pipeline": cfg.settings.digest.pipeline,
        "prompt_hash": _prompt_hash(),
        "scored": not replayed.stopped,
        "sandbox": str(sandbox.root),
        "rankings": scoring,
    }


def _question_head(case: QuestionCase) -> dict[str, Any]:
    """The fields every record of one question carries, finished or not."""
    return {
        "id": case.question_id,
        "mode": "pipeline",
        "question": case.question,
        "question_type": case.question_type,
        "question_date": case.question_date.isoformat() if case.question_date else None,
        "answer_session_ids": list(case.answer_session_ids),
    }


def _run_one_question(
    case: QuestionCase, root: Path, caller: Config, keep_sandbox: bool
) -> dict[str, Any]:
    """One question through its sandbox, as one durable record."""
    sandbox_root = root / _safe_name(case.question_id)
    keep = {"keep": keep_sandbox}
    started = time.perf_counter()
    head = _question_head(case)
    try:
        with pipeline_sandbox(sandbox_root, caller, keep) as sandbox:
            try:
                replayed = replay(sandbox, case)
                scoring = score_question(sandbox, case)
                seconds = time.perf_counter() - started
                if replayed.stopped:
                    # A stopped question's sandbox is always kept, scored or not.
                    keep["keep"] = True
                return {
                    **head,
                    "wall_seconds": round(seconds, 3),
                    **_record_body(case, replayed, scoring, sandbox),
                }
            except Exception as exc:
                keep["keep"] = True
                return {
                    **head,
                    "wall_seconds": round(time.perf_counter() - started, 3),
                    "error": str(exc),
                    "scored": False,
                    "sandbox": str(sandbox_root),
                }
    except Exception as exc:
        return {
            **head,
            "wall_seconds": round(time.perf_counter() - started, 3),
            "error": f"sandbox: {exc}",
            "scored": False,
        }


def _append_record(path: Path, record: dict[str, Any]) -> None:
    """One JSONL record appended as its question finished."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def _recorded_ids(path: Path) -> set[str]:
    """The ids a record already stands for, which --resume skips.

    A record that carries an error is not one: the question never finished, so a rerun
    answers it again once the reason is fixed.
    """
    done: set[str] = set()
    if not path.is_file():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and "error" not in record:
            question_id = record.get("id")
            if question_id:
                done.add(str(question_id))
    return done


def _read_ids(path: Path) -> set[str]:
    """The question ids a fixed list names, one per line, ``#`` comments allowed."""
    wanted: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        name = line.split("#", 1)[0].strip()
        if name:
            wanted.add(name)
    if not wanted:
        raise PipelineError(f"the ids file names no question: {path}")
    return wanted


def _progress_line(record: dict[str, Any]) -> str:
    """The one line a finished question prints, as it finishes."""
    if "error" in record:
        return f"{record['id']}: failed: {record['error']}"
    rankings = record.get("rankings") or {}
    return (
        f"{record['id']}: digested {record['sessions_digested']}/{record['sessions']},"
        f" coverage {rankings.get('coverage', 0.0):.2f},"
        f" stopped {'yes: ' + record['stopped'] if record['stopped'] else 'no'}"
    )


def _run_pipeline_parallel(
    questions: list[QuestionCase],
    root: Path,
    dataset: Path | str,
    output: Path,
    keep_sandbox: bool,
    jobs: int,
) -> list[dict[str, Any]]:
    """Questions as separate processes, since each sandbox sets the process environment.

    Each child is this script asked for one question id; the records land in the shared
    JSONL, one appended line per finished question.
    """
    jobs_dir = root / ".jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve()
    plans: list[tuple[str, Path]] = []
    for case in questions:
        ids_file = jobs_dir / f"{_safe_name(case.question_id)}.ids"
        ids_file.write_text(f"{case.question_id}\n", encoding="utf-8")
        plans.append((case.question_id, ids_file))

    def run_child(plan: tuple[str, Path]) -> tuple[str, int]:
        question_id, ids_file = plan
        command = [
            sys.executable,
            str(script),
            "--mode",
            "pipeline",
            "--dataset",
            str(dataset),
            "--workdir",
            str(root),
            "--output",
            str(output),
            "--ids",
            str(ids_file),
        ]
        if keep_sandbox:
            command.append("--keep-sandbox")
        # The child sees the same system temporary directory the caller does: a runner
        # whose tempdir was moved in process would otherwise hand its children a world
        # where the workdir sits under the system temp and no project may live there.
        env = {**os.environ, "TMPDIR": tempfile.gettempdir()}
        completed = subprocess.run(command, capture_output=True, text=True, env=env)
        return question_id, completed.returncode

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        outcomes = list(pool.map(run_child, plans))
    records: list[dict[str, Any]] = []
    written = {
        str(record.get("id")): record
        for record in _read_records(output)
        if isinstance(record, dict) and record.get("id")
    }
    for question_id, code in outcomes:
        record = written.get(question_id)
        if record is not None:
            records.append(record)
        else:
            records.append(
                {
                    "id": question_id,
                    "mode": "pipeline",
                    "error": f"the question's process exited {code} with no record",
                    "scored": False,
                }
            )
    return records


def _read_records(path: Path) -> list[Any]:
    """Every JSONL record a results file holds, in file order."""
    records: list[Any] = []
    if not path.is_file():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


_METRIC_KEYS = (
    "recall_any@1",
    "recall_any@3",
    "recall_any@5",
    "recall_any@10",
    "recall_all@10",
    "ndcg@10",
)


@dataclass
class PipelineReport:
    """What a pipeline run scored, as the command line prints it."""

    total: int = 0
    scored: int = 0
    errors: int = 0
    stopped: int = 0
    model: str | None = None
    pipeline: str = ""
    digests: dict[str, float] = field(default_factory=dict)
    full: dict[str, float] = field(default_factory=dict)
    coverage: float = 0.0

    def format_table(self) -> str:
        def means(label: str, values: dict[str, float]) -> str:
            rows = [f"{label}{key:<18}: {value:.4f}" for key, value in sorted(values.items())]
            return "\n".join(rows) if rows else f"{label}{'(none scored)':<18}: 0.0000"

        lines = [
            "LongMemEval-S pipeline benchmark",
            f"Questions evaluated : {self.scored} scored of {self.total}"
            + (f", {self.errors} failed" if self.errors else "")
            + (f", {self.stopped} stopped" if self.stopped else ""),
            f"Model               : {self.model or '(none named)'}",
            f"Digest pipeline     : {self.pipeline or '(default)'}",
            means("Coverage".ljust(24), {"coverage": self.coverage}),
            "",
            "digests ranking (recall over digest rows alone):",
            means("  ", self.digests),
            "",
            "full ranking (production search, pages by cited line):",
            means("  ", self.full),
        ]
        return "\n".join(lines)

    def __str__(self) -> str:
        return self.format_table()


def _summarize(records: Sequence[dict[str, Any]], caller: Config) -> PipelineReport:
    """The run's means over its scored records."""
    finished = [record for record in records if "error" not in record]
    scored = [record for record in finished if record.get("scored")]
    report = PipelineReport(
        total=len(records),
        scored=len(scored),
        errors=len(records) - len(finished),
        stopped=sum(1 for record in finished if record.get("stopped")),
        model=caller.settings.model.name or None,
        pipeline=caller.settings.digest.pipeline,
    )

    def mean(key: str, ranking: str) -> float:
        values = [
            record["rankings"][ranking][key]
            for record in scored
            if isinstance(record.get("rankings"), dict)
            and isinstance(record["rankings"].get(ranking), dict)
            and key in record["rankings"][ranking]
        ]
        return sum(values) / len(values) if values else 0.0

    for ranking in ("digests", "full"):
        values = {key: mean(key, ranking) for key in _METRIC_KEYS}
        if ranking == "digests":
            report.digests = values
        else:
            report.full = values
    coverages = [
        record["rankings"]["coverage"]
        for record in scored
        if isinstance(record.get("rankings"), dict) and "coverage" in record["rankings"]
    ]
    report.coverage = sum(coverages) / len(coverages) if coverages else 0.0
    return report


def run_pipeline(
    dataset_path: Path | str,
    workdir: Path | str,
    output_path: Path | str | None = None,
    *,
    ids_path: Path | str | None = None,
    resume: bool = False,
    keep_sandbox: bool = False,
    jobs: int = 1,
    limit: int | None = None,
) -> PipelineReport:
    """Run LongMemEval-S in pipeline mode over per-question sandboxes (BUILD-PLAN F71).

    Each question's haystack goes through the real capture, digest and dream path on the
    configured model; one JSONL record per question is appended to ``--output`` as the
    question finishes, and ``--resume`` skips ids a finished record already stands for.
    With ``--jobs N`` (at most 3) the questions run as separate processes, since each
    sandbox sets the process environment.
    """
    if not 1 <= jobs <= JOBS_MAX:
        raise PipelineError(f"--jobs must be between 1 and {JOBS_MAX}, not {jobs}")
    root = Path(workdir).expanduser().resolve()
    _require_replayable(root)
    questions = parse_questions(load_dataset(dataset_path))
    if ids_path is not None:
        wanted = _read_ids(Path(ids_path))
        known = {case.question_id for case in questions}
        missing = sorted(wanted - known)
        if missing:
            print(f"ids not in the dataset: {', '.join(missing)}", file=sys.stderr)
        questions = [case for case in questions if case.question_id in wanted]
    if limit is not None and limit > 0:
        questions = questions[:limit]
    if not questions:
        raise PipelineError("no question to run")
    output = Path(output_path).expanduser().resolve() if output_path else None
    if resume and output is not None:
        done = _recorded_ids(output)
        questions = [case for case in questions if case.question_id not in done]
    if not questions:
        return PipelineReport(model=configmod.load().settings.model.name or None)
    caller = configmod.load()
    if jobs == 1:
        records = []
        for case in questions:
            record = _run_one_question(case, root, caller, keep_sandbox)
            records.append(record)
            if output is not None:
                _append_record(output, record)
            print(_progress_line(record), flush=True)
    else:
        if output is None:
            raise PipelineError("--output is required with --jobs, so records land somewhere")
        records = _run_pipeline_parallel(questions, root, dataset_path, output, keep_sandbox, jobs)
    return _summarize(records, caller)


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
    parser.add_argument(
        "--mode",
        choices=("baseline", "pipeline"),
        default="baseline",
        help=(
            "baseline: raw dumps scored with the existing metrics. pipeline: each "
            "question's haystack goes through the real capture, digest and dream path "
            "in its own sandbox (BUILD-PLAN F71)"
        ),
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help=(
            "pipeline mode: directory the per-question sandboxes live under; place it "
            "outside the system temporary directory, where no directory becomes a project"
        ),
    )
    parser.add_argument(
        "--ids",
        type=Path,
        default=None,
        help="pipeline mode: file naming the question ids to run, one per line",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="pipeline mode: skip question ids a finished record already stands for",
    )
    parser.add_argument(
        "--keep-sandbox",
        action="store_true",
        help="pipeline mode: keep every sandbox instead of deleting each finished one",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="pipeline mode: questions to run at once, as separate processes (max 3)",
    )
    parsed = parser.parse_args(args)
    if parsed.mode == "pipeline":
        if parsed.workdir is None:
            parser.error("--mode pipeline requires --workdir")
        report = run_pipeline(
            dataset_path=parsed.dataset,
            workdir=parsed.workdir,
            output_path=parsed.output,
            ids_path=parsed.ids,
            resume=parsed.resume,
            keep_sandbox=parsed.keep_sandbox,
            jobs=parsed.jobs,
            limit=parsed.limit,
        )
        print(report.format_table())
        return 0 if report.errors == 0 else 1
    run_benchmark(
        dataset_path=parsed.dataset,
        limit=parsed.limit,
        output_path=parsed.output,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
