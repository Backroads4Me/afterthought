"""Verification of claims from imported maps and existing indexes (BUILD-PLAN M7.4, DESIGN 11).

An existing index, table of contents, or instruction file in an imported collection
is evidence to check, never a map or a rule to follow. Each claim taken from it is
checked:
1. Target path exists in the collection (and does not escape the collection root).
2. Heading or key exists in the target document.
3. Quoted description appears in the target document (checked against extracted text).

Claims that fail are listed in the import report and never used.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aft.library.importing import extract_document_chunks

MAP_EXTENSIONS = {".json", ".jsonl", ".md", ".markdown", ".txt"}
MAP_NAME_KEYWORDS = {"index", "toc", "map", "table_of_contents", "catalog", "manifest"}


def is_map_file(rel_path: str | Path) -> bool:
    """Identify if an excluded file is a candidate map or index file.

    Matches:
    - Files starting with 'readme' (e.g. README.md, README.txt, README)
    - Files whose stem is a map keyword (index, toc, map, table_of_contents, etc.)
      with a supported extension
    """
    p = Path(rel_path)
    lower_name = p.name.lower()
    suffix = p.suffix.lower()
    stem = p.stem.lower()

    if lower_name.startswith("readme") and (suffix in MAP_EXTENSIONS or suffix == ""):
        return True
    if stem in MAP_NAME_KEYWORDS and suffix in MAP_EXTENSIONS:
        return True
    return False


@dataclass(frozen=True)
class Claim:
    """One claim made by an imported map or index about a collection document."""

    target_path: str
    heading: str | None = None
    key: str | None = None
    quote: str | None = None
    source_map: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class ClaimResult:
    """The outcome of verifying one claim against the collection documents."""

    claim: Claim
    passed: bool
    reason: str = ""


@dataclass(frozen=True)
class MapVerificationSummary:
    """Aggregated verification results for all claims checked in a collection import."""

    total: int
    verified: list[Claim]
    rejected: list[ClaimResult]


def _normalize_ws(text: str) -> str:
    """Collapse consecutive whitespace for quote comparison."""
    return " ".join(text.split())


def _get_document_chunks(
    target_file: Path,
    rel_path: str,
    extracted_chunks: dict[str, list[tuple[str, str]]] | None = None,
    home: Path | None = None,
) -> list[tuple[str, str]]:
    """Retrieve extracted text chunks for a document, using cache or extractor (BUILD-PLAN M7.3)."""
    # 1. In-memory extracted chunks if passed (e.g. during import_collection)
    if extracted_chunks is not None and rel_path in extracted_chunks:
        return extracted_chunks[rel_path]

    # 2. Cached extraction in home / library / <hash>.json (M7.3 stored extraction)
    if home is not None:
        try:
            file_bytes = target_file.read_bytes()
            f_hash = hashlib.sha256(file_bytes).hexdigest()
            cache_file = home / "library" / f"{f_hash}.json"
            if cache_file.is_file():
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "chunks" in data:
                    return [(c.get("heading", ""), c.get("text", "")) for c in data["chunks"]]
        except OSError:
            pass

    # 3. Fresh extraction via extract_document_chunks
    return extract_document_chunks(target_file)


def check_claim(
    claim: Claim,
    collection_root: Path,
    extracted_chunks: dict[str, list[tuple[str, str]]] | None = None,
    home: Path | None = None,
) -> ClaimResult:
    """Verify one claim against target documents in collection_root."""
    # Check for unparseable table row / missing target
    if claim.target_path in ("<no link>", "<missing path>"):
        reason = (
            "table row has no markdown link"
            if claim.target_path == "<no link>"
            else "map entry has no target path"
        )
        return ClaimResult(claim=claim, passed=False, reason=reason)

    # Path containment check (B2)
    resolved_root = collection_root.resolve()
    target_p = Path(claim.target_path)
    if target_p.is_absolute():
        return ClaimResult(
            claim=claim,
            passed=False,
            reason=(
                f"target path '{claim.target_path}' is absolute; "
                "must be relative to collection root"
            ),
        )

    target_file = (collection_root / target_p).resolve()
    if not target_file.is_relative_to(resolved_root):
        return ClaimResult(
            claim=claim,
            passed=False,
            reason=f"target path '{claim.target_path}' escapes collection root",
        )

    if not target_file.exists():
        return ClaimResult(
            claim=claim,
            passed=False,
            reason=f"target path '{claim.target_path}' does not exist in collection",
        )
    if not target_file.is_file():
        return ClaimResult(
            claim=claim,
            passed=False,
            reason=f"target path '{claim.target_path}' is not a file",
        )

    # 1. Heading check against extracted text (B3)
    if claim.heading is not None:
        target_heading = claim.heading.strip()
        chunks = _get_document_chunks(target_file, claim.target_path, extracted_chunks, home=home)

        known_headings = [h.strip() for h, _ in chunks if h]
        clean_known = [re.sub(r"^#+\s*", "", h).strip() for h in known_headings]
        clean_target = re.sub(r"^#+\s*", "", target_heading).strip()

        matched = any(
            target_heading == h or clean_target == ch or clean_target.lower() == ch.lower()
            for h, ch in zip(known_headings, clean_known, strict=False)
        )
        if not matched:
            return ClaimResult(
                claim=claim,
                passed=False,
                reason=f"heading '{claim.heading}' not found in '{claim.target_path}'",
            )

    # 2. Key check (for JSON structured documents)
    if claim.key is not None:
        target_key = claim.key.strip()
        try:
            raw_text = target_file.read_text(encoding="utf-8")
            data = json.loads(raw_text)
            if isinstance(data, dict):
                keys = target_key.split(".")
                curr: Any = data
                found_key = True
                for k in keys:
                    if isinstance(curr, dict) and k in curr:
                        curr = curr[k]
                    else:
                        found_key = False
                        break
                if not found_key and target_key not in data:
                    return ClaimResult(
                        claim=claim,
                        passed=False,
                        reason=f"key '{claim.key}' not found in '{claim.target_path}'",
                    )
            elif isinstance(data, list):
                found = any(isinstance(item, dict) and target_key in item for item in data)
                if not found:
                    return ClaimResult(
                        claim=claim,
                        passed=False,
                        reason=f"key '{claim.key}' not found in '{claim.target_path}'",
                    )
        except (json.JSONDecodeError, UnicodeDecodeError):
            return ClaimResult(
                claim=claim,
                passed=False,
                reason=f"cannot read JSON from '{claim.target_path}' to verify key '{claim.key}'",
            )

    # 3. Quoted description check against extracted text (B3)
    if claim.quote is not None:
        quote = claim.quote.strip()
        if quote:
            chunks = _get_document_chunks(
                target_file, claim.target_path, extracted_chunks, home=home
            )
            doc_text = "\n".join(t for _, t in chunks)

            if quote not in doc_text and _normalize_ws(quote) not in _normalize_ws(doc_text):
                return ClaimResult(
                    claim=claim,
                    passed=False,
                    reason=f"quoted description does not appear in '{claim.target_path}'",
                )

    return ClaimResult(claim=claim, passed=True)


def parse_claims_from_json(map_path: Path, rel_source: str | None = None) -> list[Claim]:
    """Parse claims from an index JSON file."""
    src = rel_source or map_path.name
    raw = map_path.read_text(encoding="utf-8")
    data = json.loads(raw)
    claims: list[Claim] = []

    # Format 1: {"documents": [{"path": ..., "headings": [{"title": ...}], "description": ...}]}
    if isinstance(data, dict) and "documents" in data and isinstance(data["documents"], list):
        for doc in data["documents"]:
            if not isinstance(doc, dict):
                continue
            path = doc.get("path") or doc.get("file")
            if not path or not isinstance(path, str):
                claims.append(
                    Claim(
                        target_path="<missing path>",
                        quote=doc.get("description") or doc.get("quote"),
                        source_map=src,
                    )
                )
                continue
            doc_quote = doc.get("description") or doc.get("quote")
            headings = doc.get("headings", [])
            if isinstance(headings, list) and headings:
                for h in headings:
                    htitle = h.get("title") if isinstance(h, dict) else str(h)
                    hquote = h.get("quote") if isinstance(h, dict) else doc_quote
                    claims.append(
                        Claim(
                            target_path=path,
                            heading=htitle,
                            quote=hquote,
                            source_map=src,
                        )
                    )
            else:
                claims.append(
                    Claim(
                        target_path=path,
                        quote=doc_quote,
                        source_map=src,
                    )
                )
        return claims

    # Format 2: List of claim objects: [{"path": ..., "heading": ..., "key": ..., "quote": ...}]
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            path = item.get("path") or item.get("file") or item.get("target")
            if not path or not isinstance(path, str):
                claims.append(
                    Claim(
                        target_path="<missing path>",
                        heading=item.get("heading") or item.get("title"),
                        key=item.get("key"),
                        quote=item.get("quote") or item.get("description"),
                        source_map=src,
                    )
                )
                continue
            claims.append(
                Claim(
                    target_path=path,
                    heading=item.get("heading") or item.get("title"),
                    key=item.get("key"),
                    quote=item.get("quote") or item.get("description"),
                    source_map=src,
                )
            )
        return claims

    # Format 3: Dict mapping path to spec
    if isinstance(data, dict):
        for path, spec in data.items():
            if not isinstance(path, str):
                continue
            if isinstance(spec, dict):
                headings = spec.get("headings", [])
                if isinstance(headings, list) and headings:
                    for h in headings:
                        htitle = h.get("title") if isinstance(h, dict) else str(h)
                        claims.append(
                            Claim(
                                target_path=path,
                                heading=htitle,
                                quote=spec.get("description") or spec.get("quote"),
                                source_map=src,
                            )
                        )
                else:
                    claims.append(
                        Claim(
                            target_path=path,
                            heading=spec.get("heading"),
                            key=spec.get("key"),
                            quote=spec.get("quote") or spec.get("description"),
                            source_map=src,
                        )
                    )
            elif isinstance(spec, str):
                claims.append(
                    Claim(
                        target_path=path,
                        quote=spec,
                        source_map=src,
                    )
                )

    return claims


def parse_claims_from_markdown(map_path: Path, rel_source: str | None = None) -> list[Claim]:
    """Parse link claims from a markdown table or list."""
    src = rel_source or map_path.name
    text = map_path.read_text(encoding="utf-8")
    claims: list[Claim] = []

    link_re = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    wikilink_re = re.compile(r"\[\[([^|\]]+)(?:\|([^\]]+))?\]\]")

    lines = text.splitlines()

    # Identify divider lines in tables: | --- | --- |
    divider_indices = set()
    for idx, line in enumerate(lines):
        if "|" in line:
            cells = [c.strip() for c in line.split("|") if c.strip()]
            if cells and all(re.match(r"^:?-+:?$", c) for c in cells):
                divider_indices.add(idx)

    # Header lines are table rows immediately preceding a divider line
    header_indices = {idx - 1 for idx in divider_indices if idx > 0 and "|" in lines[idx - 1]}

    for line_num, line in enumerate(lines, start=1):
        idx = line_num - 1
        matches = link_re.findall(line)
        wiki_matches = wikilink_re.findall(line)

        # Check for table rows without links (non-blocking finding: reported, not silent)
        if "|" in line:
            cells = [c.strip() for c in line.split("|") if c.strip()]
            if not cells or idx in divider_indices or idx in header_indices:
                continue
            if not matches and not wiki_matches:
                row_desc = cells[0] if cells else ""
                claims.append(
                    Claim(
                        target_path="<no link>",
                        quote=row_desc,
                        source_map=src,
                        line=line_num,
                    )
                )
                continue

        # Markdown links
        for link_text, link_target in matches:
            if link_target.startswith(("http://", "https://", "mailto:")):
                continue
            path = link_target.split("#")[0].strip()
            heading = link_target.split("#")[1].strip() if "#" in link_target else None
            if not path:
                continue
            quote = None
            if "|" in line:
                cells = [c.strip() for c in line.split("|") if c.strip()]
                desc_cells = [c for c in cells if link_target not in c and link_text not in c]
                if desc_cells:
                    quote = desc_cells[0]
            claims.append(
                Claim(
                    target_path=path,
                    heading=heading,
                    quote=quote,
                    source_map=src,
                    line=line_num,
                )
            )

        # Wikilinks [[target#heading|label]] or [[target]]
        for target, label in wiki_matches:
            path = target.split("#")[0].strip()
            heading = target.split("#")[1].strip() if "#" in target else None
            if not path:
                continue
            quote = label if label else None
            if "|" in line:
                cells = [c.strip() for c in line.split("|") if c.strip()]
                desc_cells = [c for c in cells if target not in c and (not label or label not in c)]
                if desc_cells:
                    quote = desc_cells[0]
            claims.append(
                Claim(
                    target_path=path,
                    heading=heading,
                    quote=quote,
                    source_map=src,
                    line=line_num,
                )
            )

    return claims


def load_claims_from_file(map_file: Path, rel_source: str | None = None) -> list[Claim]:
    """Load claims from a JSON or Markdown map file."""
    src = rel_source or map_file.name
    suffix = map_file.suffix.lower()
    if suffix in (".json", ".jsonl"):
        return parse_claims_from_json(map_file, src)
    if suffix in (".md", ".markdown", ".txt") or suffix == "":
        return parse_claims_from_markdown(map_file, src)
    return []


def check_map_file(
    map_file: Path,
    collection_root: Path,
    rel_source: str | None = None,
    extracted_chunks: dict[str, list[tuple[str, str]]] | None = None,
    home: Path | None = None,
) -> list[ClaimResult]:
    """Load and verify all claims in map_file against collection_root."""
    src = rel_source or map_file.name
    if not map_file.exists() or not map_file.is_file():
        return [
            ClaimResult(
                claim=Claim(target_path=src, source_map=src),
                passed=False,
                reason=f"map file '{src}' does not exist or is not a file",
            )
        ]
    try:
        claims = load_claims_from_file(map_file, src)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        return [
            ClaimResult(
                claim=Claim(target_path=src, source_map=src),
                passed=False,
                reason=f"cannot read map file '{src}': {exc}",
            )
        ]
    return [check_claim(c, collection_root, extracted_chunks, home=home) for c in claims]


def verify_map_files(
    map_files: list[Path | str],
    collection_root: Path,
    extracted_chunks: dict[str, list[tuple[str, str]]] | None = None,
    home: Path | None = None,
) -> MapVerificationSummary:
    """Verify all claims across multiple map/index files against collection_root."""
    all_claims: list[Claim] = []
    rejected: list[ClaimResult] = []
    total = 0

    for mf in map_files:
        p = Path(mf)
        resolved = p if p.is_absolute() else collection_root / p
        rel_src = p.as_posix() if not p.is_absolute() else resolved.name

        if not resolved.exists() or not resolved.is_file():
            rejected.append(
                ClaimResult(
                    claim=Claim(target_path=rel_src, source_map=rel_src),
                    passed=False,
                    reason=f"map file '{rel_src}' does not exist or is not a file",
                )
            )
            total += 1
            continue

        try:
            claims = load_claims_from_file(resolved, rel_src)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            rejected.append(
                ClaimResult(
                    claim=Claim(target_path=rel_src, source_map=rel_src),
                    passed=False,
                    reason=f"cannot read map file '{rel_src}': {exc}",
                )
            )
            total += 1
            continue

        all_claims.extend(claims)

    verified: list[Claim] = []
    for c in all_claims:
        total += 1
        res = check_claim(c, collection_root, extracted_chunks, home=home)
        if res.passed:
            verified.append(c)
        else:
            rejected.append(res)

    return MapVerificationSummary(
        total=total,
        verified=verified,
        rejected=rejected,
    )


def run(
    manifest_str: str | Path,
    map_files: list[str | Path] | None = None,
    path_override: str | Path | None = None,
    stream: Any = None,
) -> int:
    """CLI entry point for aft library check-map <manifest> [map-files...]."""
    import sys

    out = stream if stream is not None else sys.stdout
    m_path = Path(manifest_str).expanduser()
    p_override = Path(path_override).expanduser() if path_override else None

    from aft.library import manifest as manifest_mod

    manifest = manifest_mod.validate(m_path, p_override)

    if map_files:
        files_to_check = list(map_files)
    else:
        files_to_check = [
            rel_path
            for rel_path, entry in manifest.files.items()
            if entry.role == "exclude" and is_map_file(rel_path)
        ]

    if not files_to_check:
        out.write(
            f"aft: no map files specified or found to check for collection '{manifest.name}'\n"
        )
        return 0

    summary = verify_map_files(files_to_check, manifest.path)
    for c in summary.verified:
        target_info = c.target_path
        if c.heading:
            target_info += f"#{c.heading}"
        elif c.key:
            target_info += f" key={c.key}"
        out.write(f"✓ [{c.source_map}] {target_info}\n")

    for r in summary.rejected:
        target_info = r.claim.target_path
        if r.claim.heading:
            target_info += f"#{r.claim.heading}"
        elif r.claim.key:
            target_info += f" key={r.claim.key}"
        out.write(f"✗ [{r.claim.source_map}] {target_info}: {r.reason}\n")

    out.write(
        f"Checked {summary.total} claim(s): {len(summary.verified)} verified, "
        f"{len(summary.rejected)} rejected\n"
    )
    return 1 if summary.rejected else 0
