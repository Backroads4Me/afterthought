#!/usr/bin/env python3
"""LongMemEval-S benchmark runner for afterthought.

Evaluates retrieval recall (Recall@1, Recall@3, Recall@5, Recall@10) and ranking quality
(Session NDCG@10) against an isolated temporary AFT_HOME and vault.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import resource
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from aft import config as configmod
from aft import evidence, index, search, vault
from aft.config import Config
from aft.eval import BenchmarkReport, compute_ndcg_at_k, compute_recall_at_k
from aft.readers import Entry


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


def _extract_entries_from_session(sess: dict[str, Any]) -> list[Entry]:
    """Extract list of Entry objects from a raw session record."""
    now = datetime.now(UTC)
    raw_messages = (
        sess.get("messages")
        or sess.get("turns")
        or sess.get("entries")
        or sess.get("history")
        or []
    )
    entries: list[Entry] = []
    if isinstance(raw_messages, list):
        for idx, item in enumerate(raw_messages):
            if isinstance(item, dict):
                role = str(item.get("role") or "user")
                text = str(item.get("content") or item.get("text") or "")
                entry_id = str(item.get("id") or f"e-{idx}")
            else:
                role = "user"
                text = str(item)
                entry_id = f"e-{idx}"
            entries.append(
                Entry(
                    id=entry_id,
                    time=now,
                    role=role,
                    text=text,
                    is_error=False,
                )
            )
    elif "content" in sess or "text" in sess:
        text = str(sess.get("content") or sess.get("text") or "")
        entries.append(
            Entry(
                id="e-0",
                time=now,
                role="user",
                text=text,
                is_error=False,
            )
        )
    return entries


def ingest_data(config: Config, data: Any) -> list[dict[str, Any]]:
    """Ingest sessions into the temporary vault and return extracted query cases."""
    sessions_to_store: list[dict[str, Any]] = []
    queries: list[dict[str, Any]] = []

    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            item_sessions = item.get("sessions") or item.get("haystack_sessions") or []
            if isinstance(item_sessions, list):
                sessions_to_store.extend(s for s in item_sessions if isinstance(s, dict))
            queries.append(item)
    elif isinstance(data, dict):
        top_sessions = data.get("sessions") or data.get("haystack_sessions") or []
        if isinstance(top_sessions, list):
            sessions_to_store.extend(s for s in top_sessions if isinstance(s, dict))

        raw_queries = (
            data.get("queries")
            or data.get("items")
            or data.get("cases")
            or data.get("examples")
            or data.get("data")
            or []
        )
        if isinstance(raw_queries, list):
            for item in raw_queries:
                if isinstance(item, dict):
                    item_sessions = item.get("sessions") or item.get("haystack_sessions") or []
                    if isinstance(item_sessions, list):
                        sessions_to_store.extend(s for s in item_sessions if isinstance(s, dict))
                    queries.append(item)
        elif "question" in data or "query" in data:
            queries.append(data)

    # Ingest sessions into evidence and digests
    seen_sessions: set[str] = set()
    for idx, sess in enumerate(sessions_to_store):
        session_id = str(sess.get("session_id") or sess.get("id") or f"session-{idx}")
        if session_id in seen_sessions:
            continue
        seen_sessions.add(session_id)

        agent = str(sess.get("agent") or "claude")
        project = str(sess.get("project") or "default")
        entries = _extract_entries_from_session(sess)
        if entries:
            evidence.store(config, agent, session_id, project, entries)

            # Also generate a digest so both digest and evidence BM25 branches index the text
            digest_dir = config.vault / "digests" / project / "2026" / "10"
            digest_dir.mkdir(parents=True, exist_ok=True)
            digest_body = "\n\n".join(e.text for e in entries)
            digest_content = (
                f"---\n"
                f"type: Session Digest\n"
                f"title: {session_id}\n"
                f"aft:\n"
                f"  project: {project}\n"
                f"---\n\n"
                f"# {session_id}\n\n"
                f"## Summary\n\n"
                f"{digest_body}\n"
            )
            digest_file = digest_dir / f"2026-10-01-{agent}-{session_id}.md"
            digest_file.write_text(digest_content, encoding="utf-8")

    # Ingest standalone documents if provided
    documents = data.get("documents", []) if isinstance(data, dict) else []
    for doc in documents:
        if isinstance(doc, dict) and "path" in doc and "content" in doc:
            doc_path = config.vault / str(doc["path"])
            doc_path.parent.mkdir(parents=True, exist_ok=True)
            doc_path.write_text(str(doc["content"]), encoding="utf-8")

    # Update BM25 search index
    index.reindex(config)
    return queries


def _extract_ground_truth(query_item: dict[str, Any]) -> str:
    """Extract ground-truth identifier or answer from a query definition."""
    if "answer_session_ids" in query_item and query_item["answer_session_ids"]:
        return str(query_item["answer_session_ids"][0])
    if "answer_session_id" in query_item:
        return str(query_item["answer_session_id"])
    if "ground_truth" in query_item:
        return str(query_item["ground_truth"])
    if "target" in query_item:
        return str(query_item["target"])
    if "expected_path" in query_item:
        return str(query_item["expected_path"])
    if "answer" in query_item:
        return str(query_item["answer"])
    return ""


def run_benchmark(
    dataset_path: Path | str,
    limit: int | None = None,
    output_path: Path | str | None = None,
) -> BenchmarkReport:
    """Run LongMemEval benchmark in an isolated vault and report metrics."""
    data = load_dataset(dataset_path)

    with isolated_vault() as config:
        raw_queries = ingest_data(config, data)
        if limit is not None and limit > 0:
            raw_queries = raw_queries[:limit]

        recalls_1: list[float] = []
        recalls_3: list[float] = []
        recalls_5: list[float] = []
        recalls_10: list[float] = []
        ndcgs_10: list[float] = []
        latencies_ms: list[float] = []
        query_records: list[dict[str, Any]] = []

        for item in raw_queries:
            query_str = str(item.get("question") or item.get("query") or "")
            ground_truth = _extract_ground_truth(item)
            if not query_str:
                continue

            buf = io.StringIO()
            t0 = time.perf_counter()
            with contextlib.redirect_stdout(buf):
                search.search_command(
                    config,
                    query_str,
                    all_projects=True,
                    limit=10,
                    as_json=True,
                )
            t1 = time.perf_counter()
            latency = (t1 - t0) * 1000.0
            latencies_ms.append(latency)

            raw_out = buf.getvalue().strip()
            hits_data = json.loads(raw_out) if raw_out else []
            ranked_hits = [str(h.get("path") or "") for h in hits_data if isinstance(h, dict)]

            r1 = compute_recall_at_k(ranked_hits, ground_truth, 1)
            r3 = compute_recall_at_k(ranked_hits, ground_truth, 3)
            r5 = compute_recall_at_k(ranked_hits, ground_truth, 5)
            r10 = compute_recall_at_k(ranked_hits, ground_truth, 10)
            ndcg10 = compute_ndcg_at_k(ranked_hits, ground_truth, 10)

            recalls_1.append(r1)
            recalls_3.append(r3)
            recalls_5.append(r5)
            recalls_10.append(r10)
            ndcgs_10.append(ndcg10)

            query_records.append(
                {
                    "query": query_str,
                    "ground_truth": ground_truth,
                    "ranked_hits": ranked_hits,
                    "recall@1": r1,
                    "recall@3": r3,
                    "recall@5": r5,
                    "recall@10": r10,
                    "ndcg@10": ndcg10,
                    "latency_ms": latency,
                }
            )

        total = len(recalls_1)
        mean_r1 = (sum(recalls_1) / total) if total else 0.0
        mean_r3 = (sum(recalls_3) / total) if total else 0.0
        mean_r5 = (sum(recalls_5) / total) if total else 0.0
        mean_r10 = (sum(recalls_10) / total) if total else 0.0
        mean_ndcg = (sum(ndcgs_10) / total) if total else 0.0
        mean_lat = (sum(latencies_ms) / len(latencies_ms)) if latencies_ms else 0.0
        peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0

        report = BenchmarkReport(
            recall_at_1=mean_r1,
            recall_at_3=mean_r3,
            recall_at_5=mean_r5,
            recall_at_10=mean_r10,
            ndcg_at_10=mean_ndcg,
            avg_latency_ms=mean_lat,
            peak_rss_mb=peak_rss,
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
