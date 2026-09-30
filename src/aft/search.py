"""Search, read and trace: query memory, inspect pages, and trace evidence.

The search index is queried with BM25 ranking over active memory (pages, digests,
and evidence) by default. The archive is searched only with --archive, and dream
reports only with --reports. Reading opens pages at outline, abstract, or full depth.
Tracing links a curated fact's source quote to its session context in the evidence archive.

A hit says where it came from as well as what matched: its document, its collection and its
section (`search_command`). An evidence hit names the one archived entry it holds, so the path
printed by a search is the path `aft trace` takes to open that entry's context - otherwise a
hit from the archive of conversation leads nowhere, since no page holds it.

Scoping is `_scope`'s: a project's search takes the project's own memory plus the global pages,
and a row with no project counts as global memory only in the collection where a page could not
have had a project at all.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aft import evidence, index, inject, keys, page, vault
from aft.config import Config
from aft.registry import Registry

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
    rank: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "collection": self.collection,
            "heading": self.heading,
            "description": self.description,
            "snippet": self.snippet,
        }


def search(
    config: Config,
    query: str,
    *,
    project: str | None = None,
    all_projects: bool = False,
    archive: bool = False,
    reports: bool = False,
    limit: int = 8,
) -> list[Hit]:
    """Query the search index with BM25 ranking across active collections.

    Without ``all_projects`` the query is scoped to one project, and what that admits per
    collection is `_scope`'s call: the project's own memory plus, for pages alone, the global
    pages of DESIGN 6.
    """
    vault.require(config)
    index.update(config)

    fts5_query = _to_fts5_query(query)
    if not fts5_query:
        return []

    target_project_id: str | None = None
    filter_global_only = False

    if not all_projects:
        registry = Registry.load(config.registry_path)
        if project is not None:
            proj = registry.by_slug(project)
            if proj is None:
                raise SearchError(f"unknown project {project!r}")
            target_project_id = proj.id
        else:
            resolution = keys.resolve(Path.cwd(), registry, config.host)
            if resolution.project is not None:
                target_project_id = resolution.project.id
            else:
                filter_global_only = True

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

        selects: list[str] = []
        params: list[Any] = []

        for col in active_collections:
            scope = _scope(col, target_project_id, filter_global_only)
            if scope is None:
                continue  # this collection holds no memory this query reaches
            clause, extra = scope
            rank_expr = index.rank_expression(col)
            params.append(fts5_query)
            params.extend(extra)
            where_sql = f"{col} MATCH ?{clause}"
            selects.append(
                f"SELECT '{col}' AS collection, path, description, heading, "
                f"snippet({col}, -1, '', '', '...', 16) AS snippet, "
                f"{rank_expr} AS rank "
                f"FROM {col} WHERE {where_sql}"
            )

        full_sql = " UNION ALL ".join(selects) + " ORDER BY rank ASC"
        cursor = conn.execute(full_sql, params)

        seen_paths: set[str] = set()
        hits: list[Hit] = []

        for row in cursor.fetchall():
            path_val = row["path"]
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


def _scope(
    collection: str, project_id: str | None, outside_every_project: bool
) -> tuple[str, list[Any]] | None:
    """The project clause one collection contributes to a scoped query, or None for no rows.

    BUILD-PLAN M3.2 scopes the default to "the current directory's project plus global pages",
    and only a page row's empty project means global (`index.GLOBAL_COLLECTIONS`). A digest
    filed under a slug no registry answers for, and an archived entry whose session resolved to
    no project (DESIGN 7 step 4), are text that was never filed rather than memory every project
    shares, so neither answers a project's query. A report day names no project because it spans
    them all, and DESIGN 10 reaches it only through ``--reports``, which is the request for it.
    From a working directory inside no project, only the shared pages answer at all.
    """
    if collection in index.SPANNING_COLLECTIONS:
        return "", []
    if project_id is not None:
        if collection in index.GLOBAL_COLLECTIONS:
            return " AND (project_id = ? OR project_id IS NULL)", [project_id]
        return " AND project_id = ?", [project_id]
    if outside_every_project:
        if collection in index.GLOBAL_COLLECTIONS:
            return " AND project_id IS NULL", []
        return None
    return "", []


def search_command(
    config: Config,
    query: str,
    *,
    project: str | None = None,
    all_projects: bool = False,
    archive: bool = False,
    reports: bool = False,
    as_json: bool = False,
    limit: int = 8,
) -> int:
    """Run search and print one tab-separated line per hit.

    The line names the document, the collection it came from and the section it sits in before
    the description and the snippet: DESIGN 10 requires a result to name its document and its
    section, and a hit from the archive or the library reads differently once it says so. An
    evidence hit's path carries the entry id, which is what makes `aft trace <path>` open it.
    """
    hits = search(
        config,
        query,
        project=project,
        all_projects=all_projects,
        archive=archive,
        reports=reports,
        limit=limit,
    )
    if as_json:
        print(json.dumps([h.to_dict() for h in hits], indent=2))
    else:
        for hit in hits:
            print(
                "\t".join(
                    (hit.path, hit.collection or "-", hit.heading or "-", hit.description or "-")
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
        return text
    elif level == "outline":
        return _extract_outline(text)
    elif level == "abstract":
        return _extract_abstract(text, path)
    else:
        raise SearchError(f"unknown level {level!r}; expected outline, abstract, or full")


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


def _to_fts5_query(query: str) -> str | None:
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
    if not terms:
        return None
    return " OR ".join(terms)


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
