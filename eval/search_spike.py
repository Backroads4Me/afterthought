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
from aft.registry import Registry
from aft.search import Hit, SearchError, hit_text

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
        text = hit_text(config, hit)[:600]
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


#: The row columns LSA reads as a row's text.
_LSA_FIELDS = ("title", "heading", "description", "body")

#: LSA models built per snapshot index and query scope, so a run builds each one once.
_LSA_CACHE: dict[tuple[Any, ...], Any] = {}


def _lsa_model(np: Any, db_path: Path, collections: list[str], narrow_id: str | None) -> Any:
    """The rows, vocabulary, IDF and truncated SVD for one index and scope, cached.

    TF-IDF is held as a sparse term list and reduced to 256 dimensions by a randomized
    truncated SVD (two power iterations), so no dense rows-by-terms matrix is ever built.
    """
    if not db_path.is_file():
        return None
    key = (str(db_path), db_path.stat().st_mtime_ns, narrow_id, tuple(collections))
    if key in _LSA_CACHE:
        return _LSA_CACHE[key]

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        cols = [c for c in [*collections, "library"] if c in tables]
        rows: list[dict[str, Any]] = []
        for col in cols:
            clause, extra = search_mod._scope(col, narrow_id)
            sql = (
                "SELECT rowid, path, description, heading, title, body, project_id, session_id"
                f" FROM {col} WHERE 1=1 {clause}"
            )
            for r in conn.execute(sql, extra).fetchall():
                d = dict(r)
                d["collection"] = col
                rows.append(d)
    finally:
        conn.close()
    if not rows:
        _LSA_CACHE[key] = None
        return None

    mem = sqlite3.connect(":memory:")
    try:
        mem.execute(f"CREATE VIRTUAL TABLE t USING fts5(x, tokenize='{index.TOKENIZER}')")
        mem.execute("CREATE VIRTUAL TABLE v USING fts5vocab(t, 'instance')")
        mem.executemany(
            "INSERT INTO t (rowid, x) VALUES (?, ?)",
            ((i, " ".join(str(r.get(f) or "") for f in _LSA_FIELDS)) for i, r in enumerate(rows)),
        )
        counts = mem.execute("SELECT term, doc, COUNT(*) FROM v GROUP BY term, doc").fetchall()
    finally:
        mem.close()

    vocab = sorted({t for t, _, _ in counts})
    term_to_idx = {t: i for i, t in enumerate(vocab)}
    n_rows, n_terms = len(rows), len(vocab)
    if n_terms == 0:
        _LSA_CACHE[key] = None
        return None
    r_idx = np.fromiter((d for _, d, _ in counts), dtype=np.int64, count=len(counts))
    c_idx = np.fromiter((term_to_idx[t] for t, _, _ in counts), dtype=np.int64, count=len(counts))
    tf = np.fromiter((c for _, _, c in counts), dtype=float, count=len(counts))
    df = np.bincount(c_idx, minlength=n_terms)
    idf_arr = np.log((n_rows + 1.0) / (df + 1.0)) + 1.0
    vals = tf * idf_arr[c_idx]
    row_norm = np.sqrt(np.bincount(r_idx, weights=vals * vals, minlength=n_rows))
    row_norm[row_norm == 0.0] = 1.0
    vals = vals / row_norm[r_idx]

    chunk = 50_000

    def x_mul(m: Any) -> Any:  # (terms x p) -> (rows x p)
        out = np.zeros((n_rows, m.shape[1]))
        for a in range(0, len(vals), chunk):
            sl = slice(a, a + chunk)
            np.add.at(out, r_idx[sl], vals[sl, None] * m[c_idx[sl]])
        return out

    def xt_mul(m: Any) -> Any:  # (rows x p) -> (terms x p)
        out = np.zeros((n_terms, m.shape[1]))
        for a in range(0, len(vals), chunk):
            sl = slice(a, a + chunk)
            np.add.at(out, c_idx[sl], vals[sl, None] * m[r_idx[sl]])
        return out

    k = max(1, min(256, n_rows - 1, n_terms - 1))
    rng = np.random.default_rng(0)
    q, _ = np.linalg.qr(x_mul(rng.standard_normal((n_terms, k + 10))))
    for _ in range(2):
        q, _ = np.linalg.qr(x_mul(xt_mul(q)))
    b = xt_mul(q).T
    ub, sv, vt = np.linalg.svd(b, full_matrices=False)
    d = (q @ ub)[:, :k] * sv[:k]
    d_norms = np.linalg.norm(d, axis=1, keepdims=True)
    d_norms[d_norms == 0.0] = 1.0
    model_ = (rows, term_to_idx, idf_arr, vt[:k].T, np.nan_to_num(d / d_norms))
    _LSA_CACHE[key] = model_
    return model_


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

    registry = Registry.load(config.registry_path)
    narrow_id = None
    if project is not None:
        proj = registry.by_slug(project)
        if proj is None:
            raise SearchError(f"unknown project {project!r}")
        narrow_id = proj.id
    elif current is not None and registry.by_slug(current) is None:
        raise SearchError(f"unknown project {current!r}")

    model_ = _lsa_model(np, db_path, collections, narrow_id)
    if model_ is None:
        return today_hits[:limit]
    rows, term_to_idx, idf_arr, v_k, d_norm = model_
    n_rows = len(rows)

    mem = sqlite3.connect(":memory:")
    try:
        mem.execute(f"CREATE VIRTUAL TABLE t USING fts5(x, tokenize='{index.TOKENIZER}')")
        mem.execute("CREATE VIRTUAL TABLE v USING fts5vocab(t, 'instance')")
        mem.execute("INSERT INTO t (rowid, x) VALUES (1, ?)", (query,))
        q_counts = mem.execute("SELECT term, COUNT(*) FROM v GROUP BY term").fetchall()
    finally:
        mem.close()
    q_vec = np.zeros(len(term_to_idx), dtype=float)
    for term, cnt in q_counts:
        if term in term_to_idx:
            q_vec[term_to_idx[term]] = cnt * idf_arr[term_to_idx[term]]
    sims = np.zeros(n_rows, dtype=float)
    q_norm = np.linalg.norm(q_vec)
    if q_norm > 0:
        q_proj = (q_vec / q_norm) @ v_k
        qp_norm = np.linalg.norm(q_proj)
        if qp_norm > 0:
            sims = d_norm @ (q_proj / qp_norm)

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
