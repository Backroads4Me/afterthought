"""Search, read and trace: query memory, inspect pages, and trace evidence.

The search index is queried with BM25 ranking over active memory (pages, digests,
and evidence) by default. The archive is searched only with --archive, and dream
reports only with --reports. Reading opens pages at outline, abstract, or full depth.
Tracing links a curated fact's source quote to its session context in the evidence archive.
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
            "description": self.description,
            "snippet": self.snippet,
        }


def _ask(
    conn: sqlite3.Connection,
    collections: list[str],
    fts5_query: str,
    target_project_id: str | None,
    filter_global_only: bool,
    limit: int,
) -> list[dict[str, Any]]:
    """Ask each collection for its ``limit`` best-ranked documents, best row first.

    The limit belongs in SQL. Without it a search builds every match - the snippet, the row, the
    Python object - to keep eight of them, which is what a vault whose every line carries the
    queried word pays for every search.

    It cannot be a bare limit of rows, because a hit is a document while a row is a section: a
    page with twelve matching sections is twelve rows and one hit, and a limit asked for at the
    door would hand back one document's twelve sections and call the search over. So each
    collection is asked to narrow itself to one row per document - the best-ranked one, which is
    the only row of it any search could have kept - and the limit goes after that. That is what
    makes the limit mean what it says, and it is done in SQL over the whole collection rather
    than after the rows arrive, because a search that reads every row to discover the eight it
    wanted has already paid for the ones it threw away.

    Which rows survived comes back before what they say, because a snippet is cut by the match
    scan it is written into: a statement that joins back to its table by rowid snippets whatever
    the join happened to read, which is the row's first words rather than the matched ones, and
    a statement that scans the matches to snippet them correctly pays for every match rather
    than for the kept few. So the kept rowids are asked for by name in their own collection,
    which is a seek measured at 0.1 ms where snippet-izing all 11 000 matches costs 50 ms.
    """
    rows: list[dict[str, Any]] = []
    for col in collections:
        where = [f"{col} MATCH ?"]
        args: list[Any] = [fts5_query]
        if target_project_id is not None:
            where.append("(project_id = ? OR project_id IS NULL)")
            args.append(target_project_id)
        elif filter_global_only:
            where.append("project_id IS NULL")

        keep = conn.execute(
            f"SELECT rid, path, description, heading, score FROM (SELECT rid, path,"
            f" description, heading, score,"
            f" row_number() OVER (PARTITION BY path ORDER BY score) AS best"
            f" FROM (SELECT rowid AS rid, path, description, heading,"
            f" {index.rank_expression(col)} AS score"
            f" FROM {col} WHERE {' AND '.join(where)})) WHERE best = 1"
            " ORDER BY score ASC LIMIT ?",
            (*args, limit),
        ).fetchall()
        if not keep:
            continue

        rowids = ", ".join("?" for _ in keep)
        snippets = dict(
            conn.execute(
                f"SELECT rowid, snippet({col}, -1, '', '', '...', 16) FROM {col}"
                f" WHERE {col} MATCH ? AND rowid IN ({rowids})",
                (fts5_query, *(row["rid"] for row in keep)),
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
            }
            for row in keep
        )

    rows.sort(key=lambda row: row["rank"])
    return rows


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
    """Query the search index with BM25 ranking across active collections."""
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

        rows = _ask(
            conn, active_collections, fts5_query, target_project_id, filter_global_only, limit
        )

        seen_paths: set[str] = set()
        hits: list[Hit] = []

        for row in rows:
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
    """Run search and print formatted hits."""
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
            desc = hit.description or "-"
            print(f"{hit.path}\t{desc}\t{hit.snippet}")
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
    """Trace a source quote from a page back to its session evidence context."""
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

    clean_id = source_id.strip().removeprefix("[").removesuffix("]").removeprefix("^")
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
        return (
            f"{quote}\n\nThe full context has expired."
            if quote
            else "The full context has expired."
        )
    return f"{quote}\n\n{entries_text}" if quote else entries_text


def trace_command(config: Config, page: str, source_id: str | None = None) -> int:
    """Run trace and print the quote and evidence context."""
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
    """Extract page description plus active facts."""
    frontmatter, _, _, _ = page.split_frontmatter(text)
    description = ""
    if isinstance(frontmatter, dict):
        description = inject.one_line(frontmatter.get("description")) or ""
    parsed = page.parse(text, path)
    active_facts = [f for f in parsed.facts if f.state == page.ACTIVE]
    lines: list[str] = []
    if description:
        lines.append(description)
    if active_facts:
        if lines:
            lines.append("")
        for fact in active_facts:
            lines.append(f"- {fact.statement}")
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
