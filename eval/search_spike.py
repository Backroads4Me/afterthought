"""Search-by-meaning spike: evaluation tooling for rerank, doc2query expand, and LSA.

Measures three ways to match by meaning without a second model (BUILD-PLAN F83):
- rerank: model orders top 30 BM25 hits by whether each answers the question, returning top 10.
- expand: doc2query short phrases generated once per snapshot, appended to body in a variant index.
- lsa: TF-IDF over indexed rows stemmed by SQLite Porter stemmer, reduced by SVD to 256 dimensions,
  fused with today's search by reciprocal rank fusion (k = 60).
- expand+rerank, lsa+rerank: combinations of expand/lsa retrieval followed by reranking.
"""

from __future__ import annotations

import json
import math
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from aft import config as configmod
from aft import index, model, vault
from aft import search as search_mod
from aft.config import Config
from aft.eval import _hit_text
from aft.registry import Registry
from aft.search import Hit, SearchError

RERANK_SYSTEM = (
    "You are a relevance ranker. Given a user question and a numbered list of candidate "
    "search hits, decide which candidates answer the question. Return a JSON object with "
    "a 'ranking' array containing the candidate numbers ordered from most relevant to least "
    "relevant."
)

RERANK_SCHEMA = {
    "type": "object",
    "properties": {
        "ranking": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "Candidate numbers ordered by relevance to the question.",
        }
    },
    "required": ["ranking"],
    "additionalProperties": False,
}

EXPAND_SYSTEM = (
    "You are a search query generator. Given a memory statement or document section, "
    "write up to 8 short search phrases, queries, or synonyms a person might use to look for it. "
    "Return a JSON object with a 'phrases' array of up to 8 strings."
)

EXPAND_SCHEMA = {
    "type": "object",
    "properties": {
        "phrases": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 8,
            "description": "Up to 8 short search phrases.",
        }
    },
    "required": ["phrases"],
    "additionalProperties": False,
}


@dataclass
class QuestionMetric:
    question: str
    variant: str
    tokens: int
    wall_seconds: float


QUESTION_METRICS: list[QuestionMetric] = []
EXPANSION_STATS: dict[str, Any] = {
    "total_tokens": 0,
    "generation_seconds": 0.0,
    "rows_generated": 0,
}


def get_stats() -> dict[str, Any]:
    """Metrics collected during variant runs: tokens and wall time."""
    return {
        "questions": [
            {
                "question": m.question,
                "variant": m.variant,
                "tokens": m.tokens,
                "wall_seconds": m.wall_seconds,
            }
            for m in QUESTION_METRICS
        ],
        "expansion": dict(EXPANSION_STATS),
    }


def reset_stats() -> None:
    """Clear in-memory metrics."""
    QUESTION_METRICS.clear()
    EXPANSION_STATS["total_tokens"] = 0
    EXPANSION_STATS["generation_seconds"] = 0.0
    EXPANSION_STATS["rows_generated"] = 0


def _read_usage_delta(usage_file: Path, offset: int) -> int:
    """Read prompt and completion tokens appended to usage.jsonl since offset."""
    if not usage_file.exists():
        return 0
    tokens = 0
    try:
        with open(usage_file, encoding="utf-8") as f:
            f.seek(offset)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    tokens += int(entry.get("prompt_tokens", 0)) + int(
                        entry.get("completion_tokens", 0)
                    )
                except (json.JSONDecodeError, ValueError):
                    pass
    except OSError:
        pass
    return tokens


def _hit_key(hit: Hit) -> str:
    """A unique key for deduplicating or fusing hits across collections."""
    if search_mod._is_raw_evidence(hit.collection, hit.path):
        parsed = search_mod._parse_session_resource(hit.path)
        if parsed:
            return parsed[0]
    return hit.path


def _search_rerank(
    config: Config,
    query: str,
    *,
    project: str | None = None,
    current: str | None = None,
    all_projects: bool = False,
    archive: bool = False,
    reports: bool = False,
    limit: int = 8,
    links: str | None = None,
    now: datetime | None = None,
    tc: Any = None,
    base_hits: list[Hit] | None = None,
) -> list[Hit]:
    """Fetch top 30 hits, order with the model by answer relevance, and return top 10."""
    if base_hits is None:
        base_hits = search_mod.search(
            config,
            query,
            project=project,
            current=current,
            all_projects=all_projects,
            archive=archive,
            reports=reports,
            limit=30,
            links=links,
            now=now,
            tc=tc,
        )
    target_limit = min(limit, 10) if limit is not None else 10
    if not base_hits:
        return []
    if len(base_hits) == 1:
        return base_hits[:target_limit]

    candidates_text = []
    for idx, hit in enumerate(base_hits, start=1):
        text = _hit_text(config, hit)[:600]
        clean_text = " ".join(text.split())
        candidates_text.append(f"[{idx}] {clean_text}")

    user_prompt = f"Question: {query}\n\nCandidates:\n" + "\n\n".join(candidates_text)
    usage_file = model.usage_path(config)
    offset = usage_file.stat().st_size if usage_file.exists() else 0
    t0 = time.perf_counter()
    ranking: list[int] | None = None
    try:
        reply = model.complete_json(
            config,
            system=RERANK_SYSTEM,
            user=user_prompt,
            schema=RERANK_SCHEMA,
            max_tokens=256,
        )
        if isinstance(reply, dict):
            raw_ranking = reply.get("ranking")
            if isinstance(raw_ranking, list):
                ranking = raw_ranking
    except Exception:
        ranking = None
    t1 = time.perf_counter()
    tokens = _read_usage_delta(usage_file, offset)
    QUESTION_METRICS.append(
        QuestionMetric(
            question=query,
            variant="rerank",
            tokens=tokens,
            wall_seconds=t1 - t0,
        )
    )

    if ranking is None:
        return base_hits[:target_limit]

    valid_candidates: list[int] = []
    seen = set()
    for c in ranking:
        if isinstance(c, int) and 1 <= c <= len(base_hits) and c not in seen:
            valid_candidates.append(c)
            seen.add(c)

    if not valid_candidates:
        return base_hits[:target_limit]

    reordered = [base_hits[c - 1] for c in valid_candidates]
    for idx, hit in enumerate(base_hits, start=1):
        if idx not in seen:
            reordered.append(hit)

    return reordered[:target_limit]


def _ensure_expanded_index(config: Config) -> Config:
    """Generate doc2query phrases for pages and digests, cached once per snapshot."""
    vault.require(config)
    index.update(config)
    snapshot_home = config.home
    cache_path = snapshot_home / "expansions.json"
    cache: dict[str, list[str]] = {}
    if cache_path.is_file():
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cache = {k: v for k, v in loaded.items() if isinstance(v, list)}
        except Exception:
            cache = {}

    db_path = snapshot_home / index.INDEX_NAME
    if not db_path.is_file():
        return config

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        rows_to_check: list[dict[str, Any]] = []
        for tbl in (index.PAGES, index.DIGESTS):
            if tbl in tables:
                for r in conn.execute(
                    f"SELECT row_key, path, heading, title, description, body FROM {tbl}"
                ).fetchall():
                    rows_to_check.append(dict(r))
    finally:
        conn.close()

    cache_modified = False
    usage_file = model.usage_path(config)
    for r in rows_to_check:
        rkey = r.get("row_key")
        if not rkey or rkey in cache:
            continue
        offset = usage_file.stat().st_size if usage_file.exists() else 0
        t0 = time.perf_counter()
        prompt_parts = []
        if r.get("title"):
            prompt_parts.append(f"Title: {r['title']}")
        if r.get("heading"):
            prompt_parts.append(f"Heading: {r['heading']}")
        if r.get("body"):
            prompt_parts.append(f"Content: {r['body']}")
        user_msg = "\n".join(prompt_parts)
        phrases: list[str] = []
        try:
            reply = model.complete_json(
                config,
                system=EXPAND_SYSTEM,
                user=user_msg,
                schema=EXPAND_SCHEMA,
                max_tokens=256,
            )
            if isinstance(reply, dict):
                raw_phrases = reply.get("phrases")
                if isinstance(raw_phrases, list):
                    phrases = [
                        str(p).strip()
                        for p in raw_phrases[:8]
                        if isinstance(p, str) and str(p).strip()
                    ]
        except Exception:
            phrases = []
        t1 = time.perf_counter()
        tokens = _read_usage_delta(usage_file, offset)
        EXPANSION_STATS["total_tokens"] += tokens
        EXPANSION_STATS["generation_seconds"] += t1 - t0
        EXPANSION_STATS["rows_generated"] += 1
        cache[rkey] = phrases
        cache_modified = True

    if cache_modified or not cache_path.is_file():
        cache_path.write_text(json.dumps(cache, indent=2) + "\n", encoding="utf-8")

    variant_home = snapshot_home / "variant-expand"
    variant_home.mkdir(parents=True, exist_ok=True)

    orig_config = snapshot_home / configmod.CONFIG_NAME
    if orig_config.is_file():
        (variant_home / configmod.CONFIG_NAME).write_text(
            orig_config.read_text(encoding="utf-8"), encoding="utf-8"
        )

    orig_evidence = snapshot_home / "evidence.sqlite"
    variant_evidence = variant_home / "evidence.sqlite"
    if orig_evidence.is_file() and not variant_evidence.exists():
        try:
            variant_evidence.symlink_to(orig_evidence)
        except OSError:
            shutil.copy2(orig_evidence, variant_evidence)

    variant_index = variant_home / index.INDEX_NAME
    needs_rebuild = True
    if variant_index.is_file():
        orig_mtime = db_path.stat().st_mtime
        var_mtime = variant_index.stat().st_mtime
        if var_mtime >= orig_mtime and not cache_modified:
            needs_rebuild = False

    if needs_rebuild:
        shutil.copy2(db_path, variant_index)
        vconn = sqlite3.connect(variant_index)
        try:
            for tbl in (index.PAGES, index.DIGESTS):
                if tbl not in tables:
                    continue
                rows = vconn.execute(f"SELECT rowid, row_key, body FROM {tbl}").fetchall()
                for rowid, rkey, body in rows:
                    phrases = cache.get(rkey)
                    if phrases:
                        extra = " ".join(phrases)
                        new_body = f"{body}\n{extra}" if body else extra
                        vconn.execute(
                            f"UPDATE {tbl} SET body = ? WHERE rowid = ?", (new_body, rowid)
                        )
            vconn.commit()
        finally:
            vconn.close()

    return configmod.Config(
        home=variant_home,
        vault=config.vault,
        host=config.host,
        settings=config.settings,
    )


def _search_expand(
    config: Config,
    query: str,
    *,
    limit: int = 8,
    **kwargs: Any,
) -> list[Hit]:
    """Search using variant index with doc2query expanded phrases."""
    var_config = _ensure_expanded_index(config)
    return search_mod.search(var_config, query, limit=limit, **kwargs)


def _search_lsa(
    config: Config,
    query: str,
    *,
    project: str | None = None,
    current: str | None = None,
    all_projects: bool = False,
    archive: bool = False,
    reports: bool = False,
    limit: int = 8,
    links: str | None = None,
    now: datetime | None = None,
    tc: Any = None,
) -> list[Hit]:
    """TF-IDF over indexed rows with Porter stemming, reduced by SVD to 256 dim, fused via RRF."""
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "lsa variant requires numpy: run with 'uv run --with numpy ...'"
        ) from exc

    today_hits = search_mod.search(
        config,
        query,
        project=project,
        current=current,
        all_projects=all_projects,
        archive=archive,
        reports=reports,
        limit=max(limit * 3, 60),
        links=links,
        now=now,
        tc=tc,
    )
    if not query.strip():
        return today_hits[:limit]

    db_path = config.home / index.INDEX_NAME
    if not db_path.is_file():
        return today_hits[:limit]

    collections = [index.PAGES, index.DIGESTS, index.EVIDENCE]
    if archive:
        collections.append(index.ARCHIVE)
    if reports:
        collections.append(index.REPORTS)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        available_tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "library" in available_tables:
            collections.append("library")
        active_cols = [c for c in collections if c in available_tables]
        if not active_cols:
            return today_hits[:limit]

        registry = Registry.load(config.registry_path)
        narrow_id = None
        if project is not None:
            proj = registry.by_slug(project)
            if proj is None:
                raise SearchError(f"unknown project {project!r}")
            narrow_id = proj.id
        elif current is not None:
            proj = registry.by_slug(current)
            if proj is None:
                raise SearchError(f"unknown project {current!r}")

        rows: list[dict[str, Any]] = []
        for col in active_cols:
            clause, extra = search_mod._scope(col, narrow_id)
            where = f"WHERE 1=1 {clause}"
            query_sql = (
                f"SELECT rowid, path, description, heading, title, body, project_id, session_id "
                f"FROM {col} {where}"
            )
            for r in conn.execute(query_sql, extra).fetchall():
                d = dict(r)
                d["collection"] = col
                rows.append(d)
    finally:
        conn.close()

    if not rows:
        return today_hits[:limit]

    doc_texts = []
    for r in rows:
        parts = [
            r.get("title") or "",
            r.get("heading") or "",
            r.get("description") or "",
            r.get("body") or "",
        ]
        doc_texts.append(" ".join(p for p in parts if p))

    mem = sqlite3.connect(":memory:")
    try:
        mem.execute(f"CREATE VIRTUAL TABLE t USING fts5(x, tokenize='{index.TOKENIZER}')")
        mem.execute("CREATE VIRTUAL TABLE v USING fts5vocab(t, 'instance')")
        for doc_idx, text in enumerate(doc_texts, start=1):
            mem.execute("INSERT INTO t (rowid, x) VALUES (?, ?)", (doc_idx, text))

        counts = mem.execute("SELECT term, doc, COUNT(*) FROM v GROUP BY term, doc").fetchall()
        vocab_terms = [
            r[0] for r in mem.execute("SELECT DISTINCT term FROM v ORDER BY term").fetchall()
        ]
        term_to_idx = {term: idx for idx, term in enumerate(vocab_terms)}
        V = len(vocab_terms)
        N = len(rows)
        if V == 0 or N == 0:
            return today_hits[:limit]

        df_rows = mem.execute("SELECT term, COUNT(DISTINCT doc) FROM v GROUP BY term").fetchall()
        idf_arr = np.zeros(V, dtype=float)
        for term, df in df_rows:
            if term in term_to_idx:
                idf_arr[term_to_idx[term]] = math.log((N + 1.0) / (df + 1.0)) + 1.0

        X = np.zeros((N, V), dtype=float)
        for term, doc, cnt in counts:
            if term in term_to_idx and 1 <= doc <= N:
                X[doc - 1, term_to_idx[term]] = cnt * idf_arr[term_to_idx[term]]

        norms = np.linalg.norm(X, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        X = X / norms

        k = min(256, N, V)
        U, S, Vt = np.linalg.svd(X, full_matrices=False)
        V_k = Vt[:k, :].T
        D = U[:, :k] * S[:k]
        d_norms = np.linalg.norm(D, axis=1, keepdims=True)
        d_norms[d_norms == 0.0] = 1.0
        D_norm = D / d_norms
        D_norm[np.isnan(D_norm)] = 0.0

        mem.execute("INSERT INTO t (rowid, x) VALUES (0, ?)", (query,))
        q_counts = mem.execute(
            "SELECT term, COUNT(*) FROM v WHERE doc = 0 GROUP BY term"
        ).fetchall()
        q_vec = np.zeros(V, dtype=float)
        for term, cnt in q_counts:
            if term in term_to_idx:
                q_vec[term_to_idx[term]] = cnt * idf_arr[term_to_idx[term]]
        q_norm = np.linalg.norm(q_vec)
        if q_norm > 0:
            q_vec = q_vec / q_norm
            q_proj = q_vec @ V_k
            qp_norm = np.linalg.norm(q_proj)
            if qp_norm > 0:
                q_proj = q_proj / qp_norm
                sims = D_norm @ q_proj
            else:
                sims = np.zeros(N, dtype=float)
        else:
            sims = np.zeros(N, dtype=float)
    finally:
        mem.close()

    slugs = {proj.id: proj.slug for proj in registry.projects}
    ranked_indices = np.argsort(-sims)
    lsa_hits: list[Hit] = []
    seen_paths: set[str] = set()
    seen_sessions: set[str] = set()

    for idx in ranked_indices:
        r = rows[int(idx)]
        path_val = r["path"]
        is_evidence = search_mod._is_raw_evidence(r["collection"], path_val)
        if is_evidence:
            session_val = r.get("session_id")
            if not session_val:
                parsed = search_mod._parse_session_resource(path_val)
                if parsed:
                    session_val = parsed[0]
            if session_val:
                if session_val in seen_sessions:
                    continue
                seen_sessions.add(session_val)
            elif path_val in seen_paths:
                continue
        else:
            if path_val in seen_paths:
                continue
        seen_paths.add(path_val)

        desc = " ".join((r.get("description") or "").split())
        body_snip = " ".join((r.get("body") or "").split())[:120]
        lsa_hits.append(
            Hit(
                path=path_val,
                description=desc,
                snippet=body_snip,
                collection=r["collection"],
                heading=r.get("heading") or "",
                project=search_mod._project_name(r.get("project_id"), r["collection"], slugs),
                rank=float(sims[idx]),
            )
        )
        if len(lsa_hits) >= 60:
            break

    # Reciprocal Rank Fusion (k = 60)
    rrf_scores: dict[str, float] = {}
    key_to_hit: dict[str, Hit] = {}

    for r1, hit in enumerate(today_hits, start=1):
        k_val = _hit_key(hit)
        rrf_scores[k_val] = rrf_scores.get(k_val, 0.0) + 1.0 / (60.0 + r1)
        if k_val not in key_to_hit:
            key_to_hit[k_val] = hit

    for r2, hit in enumerate(lsa_hits, start=1):
        k_val = _hit_key(hit)
        rrf_scores[k_val] = rrf_scores.get(k_val, 0.0) + 1.0 / (60.0 + r2)
        if k_val not in key_to_hit:
            key_to_hit[k_val] = hit

    sorted_keys = sorted(rrf_scores.keys(), key=lambda k: rrf_scores[k], reverse=True)
    fused_hits = [key_to_hit[k] for k in sorted_keys]
    return fused_hits[:limit]


def search(
    config: Config,
    query: str = "",
    *,
    variant: str | None = None,
    project: str | None = None,
    current: str | None = None,
    all_projects: bool = False,
    archive: bool = False,
    reports: bool = False,
    limit: int = 8,
    links: str | None = None,
    now: datetime | None = None,
    tc: Any = None,
) -> list[Hit]:
    """Query memory using a named search variant (BUILD-PLAN F83)."""
    norm_variant = (variant or "today").strip().lower()
    if norm_variant in ("today", "default"):
        return search_mod.search(
            config,
            query,
            project=project,
            current=current,
            all_projects=all_projects,
            archive=archive,
            reports=reports,
            limit=limit,
            links=links,
            now=now,
            tc=tc,
        )
    if norm_variant == "rerank":
        return _search_rerank(
            config,
            query,
            project=project,
            current=current,
            all_projects=all_projects,
            archive=archive,
            reports=reports,
            limit=limit,
            links=links,
            now=now,
            tc=tc,
        )
    if norm_variant == "expand":
        return _search_expand(
            config,
            query,
            project=project,
            current=current,
            all_projects=all_projects,
            archive=archive,
            reports=reports,
            limit=limit,
            links=links,
            now=now,
            tc=tc,
        )
    if norm_variant == "lsa":
        return _search_lsa(
            config,
            query,
            project=project,
            current=current,
            all_projects=all_projects,
            archive=archive,
            reports=reports,
            limit=limit,
            links=links,
            now=now,
            tc=tc,
        )
    if norm_variant == "expand+rerank":
        base_hits = _search_expand(
            config,
            query,
            project=project,
            current=current,
            all_projects=all_projects,
            archive=archive,
            reports=reports,
            limit=30,
            links=links,
            now=now,
            tc=tc,
        )
        return _search_rerank(
            config,
            query,
            project=project,
            current=current,
            all_projects=all_projects,
            archive=archive,
            reports=reports,
            limit=limit,
            links=links,
            now=now,
            tc=tc,
            base_hits=base_hits,
        )
    if norm_variant == "lsa+rerank":
        base_hits = _search_lsa(
            config,
            query,
            project=project,
            current=current,
            all_projects=all_projects,
            archive=archive,
            reports=reports,
            limit=30,
            links=links,
            now=now,
            tc=tc,
        )
        return _search_rerank(
            config,
            query,
            project=project,
            current=current,
            all_projects=all_projects,
            archive=archive,
            reports=reports,
            limit=limit,
            links=links,
            now=now,
            tc=tc,
            base_hits=base_hits,
        )
    raise ValueError(f"unknown search variant: {variant!r}")
