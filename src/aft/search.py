"""Search, read and trace: query memory, inspect pages, and trace evidence.

The search index is queried with BM25 ranking over active memory (pages, digests, and
evidence) by default. The archive is searched only with --archive, and dream reports only
with --reports. Reading opens pages at outline, abstract, or full depth. Tracing links a
curated fact's source quote to its session context in the evidence archive.

A hit says where it came from as well as what matched: its document, its collection, its
section and its project (`search_command`). An evidence hit names the one archived entry
it holds, so the path printed by a search is the path `aft trace` takes to open that
entry's context - otherwise a hit from the archive of conversation leads nowhere, since no
page holds it.

The default search reaches every project that is not archived (DESIGN 10): the current
directory's project ranks first, the global pages next, then the other projects' memory
and the unfiled text, each tier fixed ahead of the BM25 order inside it. A project named
with --project narrows the query to that project and the global pages. Memory that names
no project is named rather than hidden: the global pages, a digest or an archived entry
whose session resolved to no project, and the library each answer under their own name.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from aft import evidence, index, inject, keys, page, vault
from aft.config import Config
from aft.registry import Registry, path_key
from aft.temporal import TemporalConstraint, parse_temporal

#: A heading is 1 to 6 `#` followed by whitespace, so `#tag` and `#include` lines are not.
_HEADING = re.compile(r"^#{1,6}\s")
_FENCE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})(.*)$")

#: What a trace says when the archive no longer holds the session a quote rested on, which is
#: also what it says once `aft forget` took that session away (DESIGN 10, DESIGN 12).
EXPIRED = "The full context has expired."


class SearchError(Exception):
    """A search, read, or trace operation that cannot be completed."""


@dataclass(frozen=True)
class Hit:
    """One search hit from the index."""

    path: str
    description: str
    snippet: str
    collection: str = ""
    heading: str = ""
    project: str = ""
    rank: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "collection": self.collection,
            "heading": self.heading,
            "project": self.project,
            "description": self.description,
            "snippet": self.snippet,
        }


#: The rank tier of a row that names the project the query is pinned to, and of a row that
#: names no project in a collection a page can be shared through (DESIGN 10): the current
#: directory's project first, the global pages next, everything else last, each tier fixed
#: ahead of the BM25 order inside it. A query with no project to pin passes the empty string,
#: which no row's project answers, so tier 0 stays empty and the global pages keep tier 1.
_TIER_SHARED = "CASE WHEN project_id IS ? THEN 0 WHEN project_id IS NULL THEN 1 ELSE 2 END"
_TIER_OWNED = "CASE WHEN project_id IS ? THEN 0 ELSE 2 END"

#: The collections whose rows carry an explicit timestamp in the identity time column.
COLLECTIONS_WITH_TIMESTAMPS: tuple[str, ...] = (index.EVIDENCE,)


def _is_raw_evidence(collection: str, path: str) -> bool:
    """Check whether a row is raw evidence rather than curated memory."""
    return collection == index.EVIDENCE or path.startswith(index.SESSION_RESOURCE)


def _curated_priority(collection: str, path: str) -> int:
    """Priority within a tier: curated memory (pages, digests) ranks ahead of raw evidence."""
    return 1 if _is_raw_evidence(collection, path) else 0


def _query_collections(
    conn: sqlite3.Connection,
    collections: list[str],
    fts5_query: str | None,
    time_clauses: list[str],
    time_params: list[str],
    narrow_id: str | None,
    tier0_id: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Query each collection for its limit best-ranked rows."""
    rows: list[dict[str, Any]] = []
    for col in collections:
        if fts5_query is None and col not in COLLECTIONS_WITH_TIMESTAMPS:
            continue

        clause, extra = _scope(col, narrow_id)
        shared = col in index.GLOBAL_COLLECTIONS
        if narrow_id is not None and not shared:
            # The clause above admits only this project's own rows, so every row that
            # survives is tier 0 and the tier expression has nothing left to decide.
            tier = "0"
            tier_args: list[Any] = []
        else:
            tier = _TIER_SHARED if shared else _TIER_OWNED
            # A pinned project id goes in the tier; the empty string goes when nothing is
            # pinned, which no row answers, so tier 0 stays empty and the ranking is tier 1
            # and tier 2.
            tier_args = [tier0_id if tier0_id is not None else ""]

        if col in COLLECTIONS_WITH_TIMESTAMPS and time_clauses:
            time_filter = " AND " + " AND ".join(time_clauses)
            time_args = list(time_params)
        else:
            time_filter = ""
            time_args = []

        if fts5_query is not None:
            match_cond = f"{col} MATCH ?"
            match_args = [fts5_query]
        else:
            match_cond = "1=1"
            match_args = []

        where_cond = f"{match_cond}{clause}{time_filter}"
        where_args = [*match_args, *extra, *time_args]

        partition_expr = (
            "CASE WHEN session_id IS NOT NULL AND session_id != '' THEN session_id ELSE path END"
        )
        keep = conn.execute(
            f"SELECT rid, path, description, heading, score, tier, project_id, session_id FROM"
            f" (SELECT rid, path, description, heading, score, tier, project_id, session_id,"
            f" row_number() OVER (PARTITION BY {partition_expr} ORDER BY tier, score) AS best"
            f" FROM (SELECT rowid AS rid, path, description, heading, project_id, session_id,"
            f" {index.rank_expression(col)} AS score, {tier} AS tier"
            f" FROM {col} WHERE {where_cond})) WHERE best = 1"
            " ORDER BY tier ASC, score ASC LIMIT ?",
            (*tier_args, *where_args, limit),
        ).fetchall()
        if not keep:
            continue

        rowids = ", ".join("?" for _ in keep)
        snippets = dict(
            conn.execute(
                f"SELECT rowid, snippet({col}, -1, '', '', '...', 16) FROM {col}"
                f" WHERE {where_cond} AND rowid IN ({rowids})",
                (*where_args, *(row["rid"] for row in keep)),
            ).fetchall()
        )
        rows.extend(
            {
                "collection": col,
                "path": row["path"],
                "description": row["description"],
                "heading": row["heading"],
                "snippet": snippets.get(row["rid"], ""),
                "rank": row["score"],
                "tier": row["tier"],
                "project_id": row["project_id"],
                "session_id": row["session_id"],
            }
            for row in keep
        )

    rows.sort(
        key=lambda row: (
            row["tier"],
            _curated_priority(row["collection"], row["path"]),
            row["rank"],
        )
    )
    return rows


def _ask(
    conn: sqlite3.Connection,
    collections: list[str],
    query: str,
    narrow_id: str | None,
    tier0_id: str | None,
    limit: int,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Ask each collection for its limit best-ranked documents, best tier and row first.

    Curated memory (pages, digests) ranks ahead of raw evidence within each tier.
    Evidence entries are collapsed per session so a session returns at most its best-ranked hit.
    Conjunction (AND) of query terms is tried first, falling back to disjunction (OR) if
    conjunction yields no matches.
    """
    tc = parse_temporal(query, now=now)
    terms = _to_fts5_terms(tc.stripped_query) if tc.stripped_query else []
    if not terms and not (tc.start or tc.end):
        return []

    time_clauses: list[str] = []
    time_params: list[str] = []
    if tc.start is not None:
        time_clauses.append("time >= ?")
        time_params.append(tc.start.isoformat())
    if tc.end is not None:
        time_clauses.append("time <= ?")
        time_params.append(tc.end.isoformat())

    if len(terms) > 1:
        fts5_and = " AND ".join(terms)
        rows = _query_collections(
            conn, collections, fts5_and, time_clauses, time_params, narrow_id, tier0_id, limit
        )
        if rows:
            return rows
        fts5_or = " OR ".join(terms)
        return _query_collections(
            conn, collections, fts5_or, time_clauses, time_params, narrow_id, tier0_id, limit
        )
    elif len(terms) == 1:
        return _query_collections(
            conn, collections, terms[0], time_clauses, time_params, narrow_id, tier0_id, limit
        )
    else:
        return _query_collections(
            conn, collections, None, time_clauses, time_params, narrow_id, tier0_id, limit
        )


def search(
    config: Config,
    query: str = "",
    *,
    project: str | None = None,
    current: str | None = None,
    all_projects: bool = False,
    archive: bool = False,
    reports: bool = False,
    limit: int = 8,
    links: str | None = None,
    now: datetime | None = None,
    tc: TemporalConstraint | None = None,
) -> list[Hit]:
    """Query the search index with BM25 ranking across active collections.

    The query reaches every project that is not archived (DESIGN 10): the current
    directory's project ranks first, the global pages next, then the other projects and the
    unfiled text, a fixed tier ahead of the BM25 order inside each. ``project`` names one
    project to narrow the query to, with the global pages. ``current`` names the project
    that ranks first without narrowing the query, exactly as the working directory's
    project does for a session; ``project`` and ``current`` cannot be given together, and
    an unknown slug in either is an error. ``all_projects`` is accepted and
    changes nothing, since the default already reaches every project.
    """
    vault.require(config)
    index.update(config)

    if links is not None:
        blinks = index.backlinks(config, links)
        registry = Registry.load(config.registry_path)
        hits: list[Hit] = []
        for b in blinks:
            src = b["source_path"]
            context = b.get("context") or b.get("line_context") or ""
            line_idx = b["line_index"]
            heading = ""
            fact_state = ""
            frontmatter: dict | None = None
            project_slug = ""
            parts = Path(src).parts
            if parts[:2] == ("archive", "projects") and len(parts) >= 3:
                project_slug = parts[2]
            elif len(parts) >= 2 and parts[0] == "projects":
                project_slug = parts[1]
            try:
                src_path = config.vault / src
                if src_path.is_file():
                    parsed = page.parse(src_path.read_text(encoding="utf-8"), Path(src))
                    frontmatter = parsed.frontmatter
                    for f in parsed.facts:
                        if f.line == line_idx:
                            heading = f.section
                            fact_state = f.state
                            break
            except Exception:
                pass
            # A link hit answers from where its line is indexed, as any row does (DESIGN
            # 10, F49). A withdrawn line answers no query at all. A line resting in the
            # archive - superseded or stale - and the page of an archived project answer
            # only --archive, which names the archive collection on the hit.
            if fact_state == page.WITHDRAWN:
                continue
            home = index.PAGES
            if fact_state in (page.SUPERSEDED, page.STALE) or _page_archived(
                config, src, frontmatter, registry
            ):
                home = index.ARCHIVE
            if home == index.ARCHIVE and not archive:
                continue
            if not heading:
                heading = f"line {line_idx}"
            hit = Hit(
                path=src,
                description=f"links to [[{b['target']}]]",
                snippet=context,
                collection=home,
                heading=heading,
                project=project_slug,
                rank=0.0,
            )
            hits.append(hit)
        if query:
            q_lower = query.lower()
            hits = [h for h in hits if q_lower in h.snippet.lower() or q_lower in h.path.lower()]
        return hits[:limit]

    if tc is None:
        tc = parse_temporal(query, now=now)
    fts5_query = _to_fts5_query(tc.stripped_query) if tc.stripped_query else None
    if not fts5_query and not (tc.start or tc.end):
        return []

    registry = Registry.load(config.registry_path)
    narrow_id: str | None = None
    tier0_id: str | None = None

    if project is not None and current is not None:
        raise SearchError(
            "project and current cannot both be given: project narrows, current only ranks"
        )
    if project is not None:
        proj = registry.by_slug(project)
        if proj is None:
            raise SearchError(f"unknown project {project!r}")
        narrow_id = proj.id
        tier0_id = proj.id
    elif current is not None:
        proj = registry.by_slug(current)
        if proj is None:
            raise SearchError(f"unknown project {current!r}")
        tier0_id = proj.id
    else:
        resolution = keys.resolve(Path.cwd(), registry, config.host)
        if resolution.project is not None:
            tier0_id = resolution.project.id

    db_path = config.home / index.INDEX_NAME
    if not db_path.exists():
        return []

    collections = [index.PAGES, index.DIGESTS, index.EVIDENCE]
    if archive:
        collections.append(index.ARCHIVE)
    if reports:
        collections.append(index.REPORTS)

    conn = sqlite3.connect(db_path, timeout=index.BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    try:
        available_tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "library" in available_tables and "library" not in collections:
            collections.append("library")

        active_collections = [c for c in collections if c in available_tables]
        if not active_collections:
            return []

        rows = _ask(conn, active_collections, query, narrow_id, tier0_id, limit, now=now)

        slugs = {proj.id: proj.slug for proj in registry.projects}
        seen_paths: set[str] = set()
        seen_evidence_sessions: set[str] = set()
        hits: list[Hit] = []

        for row in rows:
            path_val = row["path"]
            if _is_raw_evidence(row["collection"], path_val):
                session_val = row.get("session_id")
                if not session_val:
                    parsed = _parse_session_resource(path_val)
                    if parsed:
                        session_val = parsed[0]
                if session_val:
                    if session_val in seen_evidence_sessions:
                        continue
                    seen_evidence_sessions.add(session_val)
                elif path_val in seen_paths:
                    continue
            else:
                if path_val in seen_paths:
                    continue
            seen_paths.add(path_val)

            desc = " ".join((row["description"] or "").split())
            snip = " ".join((row["snippet"] or "").split())

            hits.append(
                Hit(
                    path=path_val,
                    description=desc,
                    snippet=snip,
                    collection=row["collection"],
                    heading=row["heading"] or "",
                    project=_project_name(row["project_id"], row["collection"], slugs),
                    rank=float(row["rank"]),
                )
            )
            if len(hits) >= limit:
                break

        return hits
    except sqlite3.OperationalError as exc:
        raise SearchError(f"search failed: {exc}") from exc
    finally:
        conn.close()


#: The name a hit carries for memory that names no project, by collection (DESIGN 10): the
#: global pages are shared, a digest or an archived entry whose session resolved to no
#: project is unfiled (DESIGN 7), the library is reference text, and a report day spans
#: the projects.
_UNPROJECTED_NAMES = {
    index.PAGES: "global",
    index.ARCHIVE: "global",
    index.DIGESTS: "unfiled",
    index.EVIDENCE: "unfiled",
    index.LIBRARY: "library",
    index.REPORTS: "reports",
}


def _project_name(project_id: str | None, collection: str, slugs: dict[str, str]) -> str:
    """The name a hit carries for the project its row belongs to, when it has one to name.

    A row filed under a registered project names its slug. A row that names no project says
    what it is instead (DESIGN 7, DESIGN 10): the global pages, the unfiled text of a
    session whose directory answered no project, the library's reference text, or a report
    day that spans the projects. An id the registry no longer answers for names nothing.
    """
    if project_id:
        return slugs.get(project_id, "")
    return _UNPROJECTED_NAMES.get(collection, "")


def _scope(collection: str, narrow_id: str | None) -> tuple[str, list[Any]]:
    """The project clause one collection contributes to the query.

    With no project to narrow to, the collection is asked whole: the default search reaches
    every project that is not archived (DESIGN 10), and an archived project's rows live in
    the ``archive`` collection alone (F49), so an unscoped query reads nothing of them
    unless ``--archive`` asks for that collection.

    A narrowed query takes the project's own rows, and, in the collections a page can be
    shared through (``index.GLOBAL_COLLECTIONS``), the rows that name no project: the global
    pages and, under ``--archive``, their resting lines. An id-less row of any other
    collection is unfiled text - a session whose directory answered no project (DESIGN 7),
    a digest under a slug no registry answers for - and is no project's memory, so a
    narrowed query does not reach it. A report day names no project because it spans them
    all, and answers any query that asks for reports at all.
    """
    if collection in index.SPANNING_COLLECTIONS:
        return "", []
    if narrow_id is not None:
        if collection in index.GLOBAL_COLLECTIONS:
            return " AND (project_id IS ? OR project_id IS NULL)", [narrow_id]
        return " AND project_id IS ?", [narrow_id]
    return "", []


def search_links(config: Config, target: str, *, limit: int = 8) -> list[Hit]:
    """Query backlinks to a target as search hits."""
    return search(config, links=target, limit=limit)


def _page_archived(config: Config, rel: str, frontmatter: dict | None, registry: Registry) -> bool:
    """Whether the page a link hit names answers only ``aft search --archive`` (DESIGN 10,
    F49).

    Everything under the vault's ``archive/`` tree is archived text, and so is the page
    of a project ``aft project archive`` took out of memory. The project is named the way
    the index names it when it files the page's rows: the page's own ``aft.project``, then
    the path its ``aft.path`` records, then the directory the file sits under - and a page
    carrying none of those, or one scoped to several projects, is not archived by one of
    them being archived.
    """
    if rel.startswith("archive/"):
        return True
    aft = (frontmatter or {}).get("aft")
    if not isinstance(aft, dict):
        return False
    listed = aft.get("projects")
    if isinstance(listed, list) and listed:
        return False
    project_value = aft.get("project")
    if isinstance(project_value, str) and project_value:
        project = registry.by_id(project_value)
        return project is not None and project.archived
    recorded = aft.get("path")
    if isinstance(recorded, str) and recorded:
        project = registry.by_path_key(path_key(config.host, Path(recorded)))
        return project is not None and project.archived
    slug = vault.path_slug(rel)
    if slug is not None:
        project = registry.by_slug(slug)
        return project is not None and project.archived
    return False


backlinks = index.backlinks


def search_command(
    config: Config,
    query: str = "",
    *,
    project: str | None = None,
    all_projects: bool = False,
    archive: bool = False,
    reports: bool = False,
    as_json: bool = False,
    limit: int = 8,
    links: str | None = None,
    now: datetime | None = None,
) -> int:
    """Run search and print one tab-separated line per hit.

    The line names the document, the collection it came from, the section it sits in and
    the project it belongs to before the description and the snippet: DESIGN 10 requires a
    result to name its document and its section, F50 its project, and a hit from the
    archive, the library or another project reads differently once the line says so. An
    evidence hit's path carries the entry id, which is what makes `aft trace <path>` open
    it.
    """
    tc = parse_temporal(query, now=now)
    hits = search(
        config,
        query,
        project=project,
        all_projects=all_projects,
        archive=archive,
        reports=reports,
        limit=limit,
        links=links,
        now=now,
        tc=tc,
    )
    if as_json:
        print(json.dumps([h.to_dict() for h in hits], indent=2))
    else:
        for hit in hits:
            print(
                "\t".join(
                    (
                        hit.path,
                        hit.collection or "-",
                        hit.heading or "-",
                        hit.project or "-",
                        hit.description or "-",
                    )
                )
                + f"\t{hit.snippet}"
            )
    return 0


def read_page(config: Config, path_str: str, level: str = "full") -> str:
    """Read a page file at outline, abstract, or full depth."""
    path = _resolve_page_path(config, path_str)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SearchError(f"cannot read {path_str}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise SearchError(f"{path_str} is not UTF-8 text: {exc}") from exc

    if level == "full":
        content = text
    elif level == "outline":
        content = _extract_outline(text)
    elif level == "abstract":
        content = _extract_abstract(text, path)
    else:
        raise SearchError(f"unknown level {level!r}; expected outline, abstract, or full")

    try:
        rel_path = path.relative_to(config.vault.resolve()).as_posix()
        blinks = index.backlinks(config, rel_path)
        if blinks:
            referencing = sorted({b["source_path"] for b in blinks})
            backlink_lines = [f"- {src}" for src in referencing]
            section = "\n\n## Backlinks / Referenced In\n\n" + "\n".join(backlink_lines) + "\n"
            content = content.rstrip("\n") + section
    except Exception:
        pass

    return content


def read_command(config: Config, path: str, level: str = "full") -> int:
    """Run read and print the page content at the requested depth."""
    content = read_page(config, path, level=level)
    if content:
        print(content)
    return 0


def trace(config: Config, page_path_str: str, source_id: str) -> str:
    """Trace a fact's quote back to the session context that evidences it.

    ``page_path_str`` is a page in the vault and ``source_id`` one of its source ids. It is also
    allowed to be an evidence resource straight out of a search hit -
    ``aft://session/<agent>/<id>#<entry>`` - which is the other way a hit names itself, and then
    ``source_id`` is that entry id: the hit already carries no quote of a page's to print, only
    the archived entry, so the context window is the whole answer (DESIGN 10).
    """
    clean_id = source_id.strip().removeprefix("[").removesuffix("]").removeprefix("^")
    if page_path_str.startswith(index.SESSION_RESOURCE):
        base, _, fragment = page_path_str.partition("#")
        entries_text = _find_trace_context(config, f"{base}#{fragment or clean_id}", "")
        return entries_text if entries_text is not None else EXPIRED

    path = _resolve_page_path(config, page_path_str)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SearchError(f"cannot read {page_path_str}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise SearchError(f"{page_path_str} is not UTF-8 text: {exc}") from exc

    frontmatter, _, _, err = page.split_frontmatter(text)
    if err or not isinstance(frontmatter, dict):
        raise SearchError(
            f"cannot parse frontmatter of {page_path_str}: {err or 'missing frontmatter'}"
        )

    sources = frontmatter.get("sources")
    if not isinstance(sources, list):
        raise SearchError(f"no sources found in {page_path_str}")

    matched_source: dict | None = None
    for s in sources:
        if isinstance(s, dict) and str(s.get("id")) == clean_id:
            matched_source = s
            break

    if matched_source is None:
        raise SearchError(f"source {source_id!r} not found in {page_path_str}")

    quote = str(matched_source.get("quote") or "")
    resource = str(matched_source.get("resource") or "")

    entries_text = _find_trace_context(config, resource, quote)
    if entries_text is None:
        return f"{quote}\n\n{EXPIRED}" if quote else EXPIRED
    return f"{quote}\n\n{entries_text}" if quote else entries_text


def trace_command(config: Config, page: str, source_id: str | None = None) -> int:
    """Run trace and print the quote and evidence context.

    One argument is enough for an evidence hit: its path already names the entry, so
    ``aft trace <path>`` prints that entry and the entries around it.
    """
    if source_id is None:
        if "#" in page:
            page, _, source_id = page.partition("#")
        else:
            raise SearchError("source id required: aft trace <page> <source-id>")

    output = trace(config, page, source_id)
    print(output)
    return 0


def _to_fts5_terms(query: str) -> list[str]:
    """Extract and quote search terms for an FTS5 MATCH expression."""
    terms: list[str] = []
    for match in re.finditer(r'"([^"]*)"|(\S+)', query):
        quoted, bare = match.groups()
        if quoted:
            s = quoted.strip()
            if s:
                escaped = s.replace('"', '""')
                terms.append(f'"{escaped}"')
        elif bare:
            s = bare.strip()
            if s:
                escaped = s.replace('"', '""')
                terms.append(f'"{escaped}"')
    return terms


def _to_fts5_query(query: str, op: str = "AND") -> str | None:
    """Extract and quote search terms for an FTS5 MATCH expression."""
    terms = _to_fts5_terms(query)
    if not terms:
        return None
    return f" {op} ".join(terms)


def _resolve_page_path(config: Config, path_str: str) -> Path:
    """Resolve a page path inside the vault: the vault is searched before the cwd.

    A page path is relative to the vault root, so the vault wins over a same-named file in
    the working directory. Nothing outside the vault is read: a path whose real location
    is elsewhere, through `..`, a symlink, or a working directory outside the vault, is
    refused.
    """
    vault_root = config.vault.resolve()
    p = Path(path_str)
    candidates = [p] if p.is_absolute() else [vault_root / p, Path.cwd() / p]
    escaped: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(vault_root):
            if resolved.exists():
                escaped.append(resolved)
            continue
        if resolved.exists():
            return resolved
    if escaped:
        raise SearchError(f"{path_str} is outside the vault at {vault_root}: {escaped[0]}")
    raise SearchError(f"file not found: {path_str}")


def _extract_outline(text: str) -> str:
    """Extract headings outside fenced code blocks."""
    _, body, _, _ = page.split_frontmatter(text)
    headings: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    for line in body.splitlines():
        fence_match = _FENCE.match(line)
        if fence_match:
            chars = fence_match.group(1)
            char = chars[0]
            count = len(chars)
            if not in_fence:
                in_fence = True
                fence_char = char
                fence_len = count
                continue
            elif char == fence_char and count >= fence_len and not fence_match.group(2).strip():
                in_fence = False
                continue
        if in_fence:
            continue
        stripped = line.strip()
        if _HEADING.match(stripped):
            headings.append(stripped)
    return "\n".join(headings)


def _extract_abstract(text: str, path: Path) -> str:
    """Extract page description plus the lines the page is currently standing on.

    That is the same set `aft.inject` injects and the index indexes as ``pages``
    (``inject.INJECTED``): DESIGN 6 makes a conflict and a question an active line with a note
    beside it rather than a line that has left memory, and a summary that dropped them would
    hide exactly the two lines a person most needs to see before acting on the page. Each keeps
    the mark the block gives it, so a disputed line still reads as disputed.
    """
    frontmatter, _, _, _ = page.split_frontmatter(text)
    description = ""
    if isinstance(frontmatter, dict):
        description = inject.one_line(frontmatter.get("description")) or ""
    parsed = page.parse(text, path)
    standing = [f for f in parsed.facts if f.state in inject.INJECTED]
    lines: list[str] = []
    if description:
        lines.append(description)
    if standing:
        if lines:
            lines.append("")
        for fact in standing:
            mark = inject.MARKS.get(fact.state)
            lines.append(f"- {fact.statement} {mark}" if mark else f"- {fact.statement}")
    return "\n".join(lines)


def _find_trace_context(config: Config, resource: str, quote: str) -> str | None:
    """Find the three entries around a resource quote in the evidence archive."""
    parsed = _parse_session_resource(resource)
    if parsed is None:
        return None
    session_id, entry_id = parsed

    session_entries = evidence.entries(config, session_id)
    if not session_entries:
        return None

    target_idx: int | None = None
    for idx, entry in enumerate(session_entries):
        if entry.id == entry_id:
            target_idx = idx
            break

    if target_idx is None and quote:
        for idx, entry in enumerate(session_entries):
            if evidence.find_quote(config, session_id, entry.id, quote):
                target_idx = idx
                break

    if target_idx is None:
        return None

    if len(session_entries) <= 3:
        window = session_entries
    elif target_idx == 0:
        window = session_entries[:3]
    elif target_idx == len(session_entries) - 1:
        window = session_entries[-3:]
    else:
        window = session_entries[target_idx - 1 : target_idx + 2]

    rendered = [
        f"[{entry.id}] {entry.role.upper()} {entry.time.isoformat()}:\n{entry.text}"
        for entry in window
    ]
    return "\n\n".join(rendered)


def _parse_session_resource(resource: str) -> tuple[str, str] | None:
    """Parse session id and entry id from an aft session resource URI."""
    if not resource.startswith("aft://session/"):
        return None
    rest = resource.removeprefix("aft://session/")
    path_part, sep, entry_id = rest.partition("#")
    if not sep or not entry_id:
        return None
    parts = path_part.split("/")
    session_id = parts[-1]
    return session_id, entry_id
