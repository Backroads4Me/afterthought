"""Verified descriptions for library documents (design section 11 step 4, BUILD-PLAN F14).

Verifies what, version, and when_to_use claims with quotes against a document's extracted
text and recorded hash, writes verified claims into reference pages under ## Description
with frontmatter sources, lists failures in the import report, and commits.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

import yaml

from aft import evidence, lint, page, vault
from aft.config import Config, load
from aft.library import importing
from aft.library import manifest as manifest_mod
from aft.library.manifest import Manifest


class DescribeError(Exception):
    """Raised for fatal description errors (bad YAML, unimported collection, etc.)."""


class UniqueKeyLoader(yaml.SafeLoader):
    """YAML loader that raises on duplicate mapping keys."""

    def construct_mapping(self, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in mapping:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


@dataclass(frozen=True)
class Claim:
    name: str
    text: str
    quote: str


@dataclass(frozen=True)
class DocumentDescription:
    file: str
    claims: list[Claim]


@dataclass(frozen=True)
class ClaimResult:
    claim: Claim
    verified: bool
    reason: str = ""


@dataclass(frozen=True)
class DocumentResult:
    file: str
    reference_slug: str
    claims_results: list[ClaimResult]
    refused: bool = False
    refusal_reason: str = ""


@dataclass
class DescribeResult:
    collection: str
    total_documents: int
    total_claims: int
    verified_claims: list[tuple[str, Claim]]
    rejected_claims: list[tuple[str, Claim, str]]
    pages_modified: list[str]
    report_path: str | None
    committed: bool


REQUIRED_CLAIMS = ("what", "version", "when_to_use")
SUPPORTED_CLAIMS = (
    "what",
    "version",
    "when_to_use",
    "when-to-use",
    "when_not_to_use",
    "when-not-to-use",
)


def load_descriptions(source: str | Path | TextIO | Sequence[dict]) -> list[DocumentDescription]:
    """Load document descriptions from a YAML file, stdin, or data structure."""
    if isinstance(source, (str, Path)):
        s_str = str(source)
        if s_str == "-":
            text = sys.stdin.read()
        else:
            p = Path(source)
            if not p.is_file():
                raise DescribeError(f"descriptions file not found: {source}")
            text = p.read_text(encoding="utf-8")
        try:
            data = yaml.load(text, Loader=UniqueKeyLoader)
        except yaml.YAMLError as exc:
            raise DescribeError(f"descriptions file is not valid YAML: {exc}") from exc
    elif hasattr(source, "read"):
        text = source.read()
        try:
            data = yaml.load(text, Loader=UniqueKeyLoader)
        except yaml.YAMLError as exc:
            raise DescribeError(f"descriptions data is not valid YAML: {exc}") from exc
    else:
        data = source

    if data is None:
        raise DescribeError("no document descriptions provided")

    entries: list[dict] = []
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        if "descriptions" in data and isinstance(data["descriptions"], list):
            entries = data["descriptions"]
        else:
            for f_path, doc_val in data.items():
                if isinstance(doc_val, dict):
                    entries.append({"file": f_path, **doc_val})
                else:
                    raise DescribeError(f"expected mapping of claims for {f_path!r}")
    else:
        raise DescribeError("descriptions must be a list of document entries or a mapping")

    results: list[DocumentDescription] = []
    for item in entries:
        if not isinstance(item, dict):
            raise DescribeError("each description entry must be a mapping")

        file_path = item.get("file") or item.get("document") or item.get("path")
        if not file_path or not isinstance(file_path, str):
            raise DescribeError("each description entry must specify a 'file'")

        claims: list[Claim] = []
        for key in SUPPORTED_CLAIMS:
            if key in item:
                val = item[key]
                norm_name = key.replace("-", "_")
                if isinstance(val, dict):
                    c_text = val.get("claim") or val.get("text") or val.get("statement") or ""
                    c_quote = val.get("quote") or val.get("source_quote") or ""
                elif isinstance(val, str):
                    c_text = val
                    c_quote = item.get(f"{norm_name}_quote") or item.get(f"{key}_quote") or ""
                else:
                    c_text = str(val) if val is not None else ""
                    c_quote = ""
                claims.append(
                    Claim(name=norm_name, text=str(c_text).strip(), quote=str(c_quote).strip())
                )

        results.append(DocumentDescription(file=file_path.strip(), claims=claims))

    if not results:
        raise DescribeError("no document descriptions provided")

    return results


def _get_extracted_text(vault_file: Path, actual_hash: str, home: Path) -> str:
    """Retrieve full extracted text for a document from cache or extractor."""
    # 1. Stored extraction in home / library / <hash>.json (M7.3)
    cache_file = home / "library" / f"{actual_hash}.json"
    if cache_file.is_file():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "chunks" in data:
                return "\n".join(c.get("text", "") for c in data["chunks"])
        except Exception:
            pass

    # 2. Extract freshly
    chunks = importing.extract_document_chunks(vault_file)
    extracted = "\n".join(t for _, t in chunks)

    # For text / markdown files, also include raw text
    try:
        raw_text = vault_file.read_text(encoding="utf-8", errors="replace")
        if raw_text not in extracted:
            extracted += "\n" + raw_text
    except Exception:
        pass

    return extracted


def verify_document_descriptions(
    doc: DocumentDescription,
    manifest: Manifest,
    collection_root: Path,
    vault_path: Path,
    home: Path,
    recorded_files: dict[str, str],
    slug_map: dict[str, str],
) -> DocumentResult:
    """Verify description claims for one document against hash and extracted text."""
    target_p = Path(doc.file)
    if target_p.is_absolute():
        return DocumentResult(
            file=doc.file,
            reference_slug="",
            claims_results=[],
            refused=True,
            refusal_reason=f"file {doc.file!r} is absolute; must be relative to collection root",
        )

    resolved_root = collection_root.resolve()
    target_file = (collection_root / target_p).resolve()
    if not target_file.is_relative_to(resolved_root):
        return DocumentResult(
            file=doc.file,
            reference_slug="",
            claims_results=[],
            refused=True,
            refusal_reason=f"file {doc.file!r} steps outside collection root",
        )

    if doc.file not in manifest.files:
        return DocumentResult(
            file=doc.file,
            reference_slug="",
            claims_results=[],
            refused=True,
            refusal_reason=f"file {doc.file!r} is not in collection manifest",
        )

    entry = manifest.files[doc.file]
    if entry.role == "exclude":
        return DocumentResult(
            file=doc.file,
            reference_slug="",
            claims_results=[],
            refused=True,
            refusal_reason=f"file {doc.file!r} is excluded from collection",
        )

    vault_file = vault_path / "library" / manifest.name / doc.file
    if not vault_file.is_file():
        return DocumentResult(
            file=doc.file,
            reference_slug="",
            claims_results=[],
            refused=True,
            refusal_reason=(
                f"file {doc.file!r} not found in vault library/{manifest.name}/; "
                "run 'aft library import' first"
            ),
        )

    try:
        actual_hash = hashlib.sha256(vault_file.read_bytes()).hexdigest()
    except OSError as exc:
        return DocumentResult(
            file=doc.file,
            reference_slug="",
            claims_results=[],
            refused=True,
            refusal_reason=f"cannot read file {doc.file!r}: {exc}",
        )

    recorded_hash = recorded_files.get(doc.file)
    if not recorded_hash:
        return DocumentResult(
            file=doc.file,
            reference_slug="",
            claims_results=[],
            refused=True,
            refusal_reason=f"file {doc.file!r} has no recorded hash in collection.md",
        )

    if actual_hash != recorded_hash:
        return DocumentResult(
            file=doc.file,
            reference_slug="",
            claims_results=[],
            refused=True,
            refusal_reason=(
                f"hash mismatch for {doc.file!r}: recorded {recorded_hash}, actual {actual_hash}"
            ),
        )

    slug = slug_map.get(doc.file, importing._doc_slug(doc.file))
    ref_page = vault_path / "references" / manifest.name / f"{slug}.md"
    if not ref_page.is_file():
        return DocumentResult(
            file=doc.file,
            reference_slug=slug,
            claims_results=[],
            refused=True,
            refusal_reason=f"reference page 'references/{manifest.name}/{slug}.md' not found",
        )

    extracted_text = _get_extracted_text(vault_file, actual_hash, home)
    norm_extracted = evidence._spaced(extracted_text)

    claims_results: list[ClaimResult] = []
    present_names = {c.name for c in doc.claims}

    # Verify each provided claim
    for c in doc.claims:
        if not c.text:
            claims_results.append(
                ClaimResult(claim=c, verified=False, reason=f"claim '{c.name}' has empty text")
            )
            continue
        if not c.quote:
            claims_results.append(
                ClaimResult(claim=c, verified=False, reason=f"claim '{c.name}' has empty quote")
            )
            continue

        norm_quote = evidence._spaced(c.quote)
        if norm_quote in norm_extracted:
            claims_results.append(ClaimResult(claim=c, verified=True))
        else:
            claims_results.append(
                ClaimResult(
                    claim=c,
                    verified=False,
                    reason=f"quote for '{c.name}' not found in extracted text",
                )
            )

    # Check for missing required claims (what, version, when_to_use)
    for req in REQUIRED_CLAIMS:
        if req not in present_names:
            dummy = Claim(name=req, text="", quote="")
            claims_results.append(
                ClaimResult(claim=dummy, verified=False, reason=f"missing required claim '{req}'")
            )

    return DocumentResult(
        file=doc.file,
        reference_slug=slug,
        claims_results=claims_results,
        refused=False,
    )


def update_reference_page(
    vault_path: Path,
    manifest_name: str,
    file_rel: str,
    slug: str,
    verified_claims: list[Claim],
    home: Path,
    file_hash: str,
) -> bool:
    """Update reference page with verified claims under ## Description and frontmatter sources.

    Returns True if the page content was modified, False if already up to date.
    """
    ref_file = vault_path / "references" / manifest_name / f"{slug}.md"
    content = ref_file.read_text(encoding="utf-8")
    fm, body, _, _ = page.split_frontmatter(content)
    if fm is None:
        fm = {}

    resource = f"aft://library/{manifest_name}/{file_rel}"
    existing_sources = list(fm.get("sources") or [])

    source_id_map: dict[str, str] = {}
    for claim in verified_claims:
        found_id = None
        for s in existing_sources:
            if (
                isinstance(s, dict)
                and s.get("resource") == resource
                and s.get("quote") == claim.quote
            ):
                found_id = str(s.get("id"))
                break

        if found_id is not None:
            source_id_map[claim.name] = found_id
        else:
            used_nums = set()
            for s in existing_sources:
                if isinstance(s, dict) and s.get("id") is not None:
                    sid = str(s["id"])
                    if sid.startswith("s") and sid[1:].isdigit():
                        used_nums.add(int(sid[1:]))
            next_num = 1
            while next_num in used_nums:
                next_num += 1
            new_id = f"s{next_num}"
            existing_sources.append(
                {
                    "id": new_id,
                    "resource": resource,
                    "quote": claim.quote,
                }
            )
            source_id_map[claim.name] = new_id

        try:
            from aft.library.propose import record_verified_quote

            record_verified_quote(home, resource, claim.quote, file_hash)
        except Exception:
            pass

    order_key = {"what": 1, "version": 2, "when_to_use": 3, "when_not_to_use": 4}
    sorted_claims = sorted(verified_claims, key=lambda c: order_key.get(c.name, 99))

    desc_lines = []
    for c in sorted_claims:
        if c.name == "what":
            label = "What"
        elif c.name == "version":
            label = "Version"
        elif c.name in ("when_to_use", "when-to-use"):
            label = "When to use"
        elif c.name in ("when_not_to_use", "when-not-to-use"):
            label = "When not to use"
        else:
            label = c.name.replace("_", " ").capitalize()

        text_clean = c.text.strip()
        if text_clean.lower().startswith(label.lower() + ":"):
            text_clean = text_clean[len(label) + 1 :].strip()

        sid = source_id_map[c.name]
        desc_lines.append(f"- {label}: {text_clean} [^{sid}]")

    new_desc_section = "## Description\n\n" + "\n".join(desc_lines)

    # Replace existing ## Description section if present
    pattern = re.compile(r"^## Description\b.*?(?=\n## |\Z)", re.MULTILINE | re.DOTALL)
    if pattern.search(body):
        new_body = pattern.sub(new_desc_section + "\n", body)
    else:
        m = re.search(r"^## ", body, re.MULTILINE)
        if m:
            idx = m.start()
            new_body = (
                body[:idx].rstrip() + "\n\n" + new_desc_section + "\n\n" + body[idx:].lstrip()
            )
        else:
            new_body = body.rstrip() + "\n\n" + new_desc_section + "\n"

    fm["sources"] = existing_sources
    new_fm_yaml = yaml.dump(fm, sort_keys=False, allow_unicode=True)
    new_content = f"---\n{new_fm_yaml}---\n\n{new_body.lstrip()}"

    if new_content.strip() == content.strip():
        return False

    ref_file.write_text(new_content, encoding="utf-8")

    findings = lint.lint_file(vault_path, ref_file, None)
    if findings:
        ref_file.write_text(content, encoding="utf-8")
        finding_msgs = "; ".join(f.message for f in findings)
        raise DescribeError(f"lint findings on written page {ref_file.name}: {finding_msgs}")

    return True


def append_describe_report(
    vault_path: Path,
    manifest: Manifest,
    total_claims: int,
    verified_claims: list[tuple[str, Claim]],
    rejected_claims: list[tuple[str, Claim, str]],
    now: datetime,
) -> str:
    """Append a report section to reports/<yyyy>/<mm>/<date>.md."""
    date_str = now.strftime("%Y-%m-%d")
    year_str = now.strftime("%Y")
    month_str = now.strftime("%m")
    time_str = now.strftime("%H:%M:%SZ")

    report_dir = vault_path / "reports" / year_str / month_str
    report_dir.mkdir(parents=True, exist_ok=True)
    report_file = report_dir / f"{date_str}.md"

    verified_by_file: dict[str, list[str]] = {}
    for f, c in verified_claims:
        verified_by_file.setdefault(f, []).append(c.name)

    if verified_by_file:
        verified_lines = [
            f"- `{f}`: {', '.join(claims)}" for f, claims in sorted(verified_by_file.items())
        ]
        verified_text = "\n".join(verified_lines)
    else:
        verified_text = "- none"

    if rejected_claims:
        rejected_lines = [f"- `{f}` ({c.name}): {reason}" for f, c, reason in rejected_claims]
        rejected_text = "\n".join(rejected_lines)
    else:
        rejected_text = "- none"

    section_text = f"""

## Library descriptions: {manifest.name} ({time_str})

- Collection: `{manifest.name}`
- Title: {manifest.title}
- Descriptions checked: {total_claims}
- Descriptions verified: {len(verified_claims)}
- Descriptions rejected: {len(rejected_claims)}

### Verified descriptions

{verified_text}

### Rejected descriptions

{rejected_text}
"""
    if not report_file.exists():
        initial = f"""---
type: Report
date: {date_str}
---
# Daily Report: {date_str}
"""
        report_file.write_text(initial + section_text.lstrip(), encoding="utf-8")
    else:
        with open(report_file, "a", encoding="utf-8") as f:
            f.write(section_text)

    return f"reports/{year_str}/{month_str}/{date_str}.md"


def describe_collection(
    manifest_path: str | Path,
    descriptions_source: str | Path | TextIO | Sequence[dict],
    path_override: str | None = None,
    config: Config | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> DescribeResult:
    """Verify document descriptions, write to reference pages, report, and commit."""
    cfg = config or load()
    vault.require(cfg)
    now_dt = now or datetime.now(UTC)

    m_path = Path(manifest_path)
    p_override = Path(path_override) if path_override is not None else None
    manifest = manifest_mod.validate(m_path, p_override)
    collection_root = manifest.path

    col_file = cfg.vault / "references" / manifest.name / "collection.md"
    if not col_file.is_file():
        raise DescribeError(
            f"collection '{manifest.name}' has not been imported yet: "
            f"'references/{manifest.name}/collection.md' not found; "
            "run 'aft library import' first"
        )

    col_text = col_file.read_text(encoding="utf-8")
    col_fm, _, _, _ = page.split_frontmatter(col_text)
    recorded_files = (col_fm.get("aft") or {}).get("files", {}) if col_fm else {}

    slug_map = importing.compute_reference_slugs(manifest.files.keys())
    descriptions = load_descriptions(descriptions_source)

    doc_results: list[DocumentResult] = []
    verified_claims: list[tuple[str, Claim]] = []
    rejected_claims: list[tuple[str, Claim, str]] = []

    for doc in descriptions:
        res = verify_document_descriptions(
            doc,
            manifest,
            collection_root,
            cfg.vault,
            cfg.home,
            recorded_files,
            slug_map,
        )
        doc_results.append(res)
        if res.refused:
            for c in doc.claims:
                rejected_claims.append((doc.file, c, res.refusal_reason))
        else:
            for cr in res.claims_results:
                if cr.verified:
                    verified_claims.append((doc.file, cr.claim))
                else:
                    rejected_claims.append((doc.file, cr.claim, cr.reason))

    total_claims = len(verified_claims) + len(rejected_claims)

    if dry_run:
        return DescribeResult(
            collection=manifest.name,
            total_documents=len(descriptions),
            total_claims=total_claims,
            verified_claims=verified_claims,
            rejected_claims=rejected_claims,
            pages_modified=[],
            report_path=None,
            committed=False,
        )

    # Group verified claims by document
    verified_by_file: dict[str, list[Claim]] = {}
    for f, c in verified_claims:
        verified_by_file.setdefault(f, []).append(c)

    pages_modified: list[str] = []
    report_rel: str | None = None
    committed = False

    with vault.locked(cfg):
        for f, v_claims in verified_by_file.items():
            slug = slug_map.get(f, importing._doc_slug(f))
            f_hash = recorded_files.get(f, "")
            modified = update_reference_page(
                cfg.vault,
                manifest.name,
                f,
                slug,
                v_claims,
                cfg.home,
                f_hash,
            )
            if modified:
                pages_modified.append(f"references/{manifest.name}/{slug}.md")

        # Write report if pages were modified or there are rejections
        # If nothing was modified and no rejections (idempotent rerun), write nothing.
        if pages_modified or rejected_claims:
            report_rel = append_describe_report(
                cfg.vault,
                manifest,
                total_claims,
                verified_claims,
                rejected_claims,
                now_dt,
            )
            staged = [*pages_modified, report_rel]
            committed = vault.commit(
                cfg.vault,
                staged,
                f"aft library: describe documents in '{manifest.name}'",
            )

    return DescribeResult(
        collection=manifest.name,
        total_documents=len(descriptions),
        total_claims=total_claims,
        verified_claims=verified_claims,
        rejected_claims=rejected_claims,
        pages_modified=pages_modified,
        report_path=report_rel,
        committed=committed,
    )


def run(
    manifest_path: str,
    descriptions_path: str,
    path_override: str | None = None,
    dry_run: bool = False,
) -> int:
    """CLI entrypoint for aft library describe."""
    try:
        result = describe_collection(
            manifest_path,
            descriptions_path,
            path_override=path_override,
            dry_run=dry_run,
        )
    except manifest_mod.ManifestError as exc:
        print(f"aft library describe: {exc}", file=sys.stderr)
        return 2
    except DescribeError as exc:
        print(f"aft library describe: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"aft library describe: {exc}", file=sys.stderr)
        return 1

    if dry_run:
        print(
            f"dry-run: {len(result.verified_claims)} claim(s) verified, "
            f"{len(result.rejected_claims)} rejected"
        )
        for f, c in result.verified_claims:
            print(f"  ✓ {f} ({c.name}): {c.text}")
        for f, c, r in result.rejected_claims:
            print(f"  ✗ {f} ({c.name}): {r}")
        return 0

    if not result.verified_claims and result.rejected_claims:
        print(
            f"aft: 0 descriptions verified ({len(result.rejected_claims)} rejected)",
            file=sys.stderr,
        )
        for f, c, r in result.rejected_claims:
            print(f"  ✗ {f} ({c.name}): {r}", file=sys.stderr)
        return 1

    if result.pages_modified:
        print(
            f"verified {len(result.verified_claims)} claim(s) "
            f"({len(result.rejected_claims)} rejected); "
            f"updated {len(result.pages_modified)} reference page(s):"
        )
        for p in result.pages_modified:
            print(f"  - {p}")
        return 0

    print(
        f"all {len(result.verified_claims)} claim(s) verified; reference pages already up to date"
    )
    return 0
