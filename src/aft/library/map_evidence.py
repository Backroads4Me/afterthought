"""Verification of claims from imported maps and existing indexes (BUILD-PLAN M7.4, DESIGN 11).

An existing index, table of contents, or instruction file in an imported collection
is evidence to check, never a map or a rule to follow. Each claim taken from it is
checked:
1. Target path exists in the collection.
2. Heading or key exists in the target document.
3. Quoted description appears in the target document.

Claims that fail are listed in the import report and never used.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aft.library.importing import extract_document_chunks


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


def check_claim(
    claim: Claim,
    collection_root: Path,
    extracted_chunks: dict[str, list[tuple[str, str]]] | None = None,
) -> ClaimResult:
    """Verify one claim against target documents in collection_root."""
    target_file = collection_root / claim.target_path
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

    # 1. Heading check
    if claim.heading is not None:
        target_heading = claim.heading.strip()
        chunks: list[tuple[str, str]]
        if extracted_chunks is not None and claim.target_path in extracted_chunks:
            chunks = extracted_chunks[claim.target_path]
        else:
            chunks = extract_document_chunks(target_file)

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

    # 3. Quoted description check
    if claim.quote is not None:
        quote = claim.quote.strip()
        if quote:
            doc_text = ""
            if extracted_chunks is not None and claim.target_path in extracted_chunks:
                doc_text = "\n".join(t for _, t in extracted_chunks[claim.target_path])
            else:
                try:
                    doc_text = target_file.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    chunks = extract_document_chunks(target_file)
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
    for line_num, line in enumerate(text.splitlines(), start=1):
        matches = link_re.findall(line)
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
    return claims


def load_claims_from_file(map_file: Path, rel_source: str | None = None) -> list[Claim]:
    """Load claims from a JSON or Markdown map file."""
    src = rel_source or map_file.name
    suffix = map_file.suffix.lower()
    if suffix in (".json", ".jsonl"):
        return parse_claims_from_json(map_file, src)
    if suffix in (".md", ".markdown", ".txt"):
        return parse_claims_from_markdown(map_file, src)
    return []


def check_map_file(
    map_file: Path,
    collection_root: Path,
    rel_source: str | None = None,
    extracted_chunks: dict[str, list[tuple[str, str]]] | None = None,
) -> list[ClaimResult]:
    """Load and verify all claims in map_file against collection_root."""
    claims = load_claims_from_file(map_file, rel_source)
    return [check_claim(c, collection_root, extracted_chunks) for c in claims]


def verify_map_files(
    map_files: list[Path | str],
    collection_root: Path,
    extracted_chunks: dict[str, list[tuple[str, str]]] | None = None,
) -> MapVerificationSummary:
    """Verify all claims across multiple map/index files against collection_root."""
    all_claims: list[Claim] = []
    for mf in map_files:
        p = Path(mf)
        resolved = p if p.is_absolute() else collection_root / p
        if resolved.is_file():
            rel_src = p.as_posix() if not p.is_absolute() else resolved.name
            all_claims.extend(load_claims_from_file(resolved, rel_src))

    verified: list[Claim] = []
    rejected: list[ClaimResult] = []

    for c in all_claims:
        res = check_claim(c, collection_root, extracted_chunks)
        if res.passed:
            verified.append(c)
        else:
            rejected.append(res)

    return MapVerificationSummary(
        total=len(all_claims),
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

    files_to_check = list(map_files) if map_files else list(manifest.maps)
    if not files_to_check:
        out.write(f"aft: no map files specified to check for collection '{manifest.name}'\n")
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
