"""Import a library collection into the memory vault (BUILD-PLAN M7.3, DESIGN 11).

Validates the collection manifest, copies non-excluded files into `library/<name>/`
(marked read-only), extracts text (per page for PDF, headings for markdown/text,
top-level keys for JSON), caches extracted text by SHA-256 hash in `~/.afterthought/library/`,
indexes text in the `library` search collection, generates reference pages under
`references/<name>/`, records hashes for `aft lint`, writes an import report, and commits.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    from aft.library.map_evidence import MapVerificationSummary

from aft import index, vault
from aft.config import Config, load
from aft.library import manifest as manifest_mod
from aft.library.manifest import Manifest
from aft.library.survey import survey
from aft.registry import Registry

PDF_PAGE_PATTERN = re.compile(r"Page\s+(\d+)", re.IGNORECASE)


class ImportError(Exception):
    """Raised when a library import fails."""


@dataclass(frozen=True)
class ImportResult:
    collection: str
    title: str
    files_imported: int
    files_excluded: int
    reference_pages: list[str]
    report_path: str
    committed: bool
    map_summary: MapVerificationSummary | None = None


def extract_pdf_pages(path: Path) -> list[tuple[str, str]]:
    """Extract pages from a PDF using `pdftotext -layout`."""
    if shutil.which("pdftotext"):
        try:
            res = subprocess.run(
                ["pdftotext", "-layout", str(path), "-"],
                capture_output=True,
                text=True,
                check=True,
            )
            raw_pages = res.stdout.split("\x0c")
            pages: list[tuple[str, str]] = []
            for i, p in enumerate(raw_pages, start=1):
                clean = p.strip()
                if clean or i < len(raw_pages):
                    pages.append((f"page {i}", clean))
            if pages:
                return pages
        except (subprocess.SubprocessError, OSError):
            pass
    return [("page 1", "")]


def extract_markdown_chunks(text: str) -> list[tuple[str, str]]:
    """Extract headings and prose chunks from markdown or plain text."""
    chunks: list[tuple[str, str]] = []
    heading = "Overview"
    lines: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^(#+)\s+(.+)$", line.strip())
        if match:
            if lines:
                chunks.append((heading, "\n".join(lines).strip()))
                lines = []
            heading = match.group(2).strip()
        else:
            lines.append(line)
    if lines:
        chunks.append((heading, "\n".join(lines).strip()))
    return [(h, t) for h, t in chunks if t] or [("Overview", text.strip())]


def extract_json_chunks(text: str) -> list[tuple[str, str]]:
    """Split JSON structured data by top-level key."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [("data", text.strip())]
    if isinstance(data, dict):
        chunks: list[tuple[str, str]] = []
        for k, v in data.items():
            rendered = json.dumps(v, indent=2) if isinstance(v, (dict, list)) else str(v)
            chunks.append((str(k), rendered))
        return chunks if chunks else [("empty", "{}")]
    if isinstance(data, list):
        return [("items", json.dumps(data, indent=2))]
    return [("data", str(data))]


def extract_html_chunks(text: str) -> list[tuple[str, str]]:
    """Extract text from HTML documents."""
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"\s+", " ", clean).strip()
    return [("content", clean)] if clean else []


def extract_document_chunks(path: Path) -> list[tuple[str, str]]:
    """Extract text chunks for a document based on its extension."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf_pages(path)
    if suffix in (".md", ".txt", ".rst"):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_bytes().decode("utf-8", errors="replace")
        return extract_markdown_chunks(text)
    if suffix == ".json":
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_bytes().decode("utf-8", errors="replace")
        return extract_json_chunks(text)
    if suffix in (".html", ".htm"):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_bytes().decode("utf-8", errors="replace")
        return extract_html_chunks(text)
    return []


def cache_extracted_text(
    home: Path,
    file_hash: str,
    rel_path: str,
    chunks: list[tuple[str, str]],
) -> Path:
    """Store extracted text in ~/.afterthought/library/ keyed by content hash."""
    cache_dir = home / "library"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{file_hash}.json"
    doc_data = {
        "hash": file_hash,
        "path": rel_path,
        "chunks": [{"heading": h, "text": t} for h, t in chunks],
    }
    cache_file.write_text(json.dumps(doc_data, indent=2), encoding="utf-8")
    return cache_file


def _doc_slug(rel_path: str) -> str:
    """Derive a friendly reference page slug for a file in the collection."""
    p = Path(rel_path)
    stem = p.stem
    if p.parent != Path("."):
        prefix = p.parent.as_posix().replace("/", "-")
        return f"{prefix}-{stem}"
    return stem


def compute_reference_slugs(file_paths) -> dict[str, str]:
    """Derive unique reference page slugs for files in a collection.

    If two or more documents share the same stem (e.g. guide.pdf and guide.txt),
    their extension is appended (e.g. guide-pdf and guide-txt) to guarantee uniqueness
    and avoid reference page overwrites.
    """
    paths = sorted(file_paths)
    stems: dict[str, str] = {}
    stem_counts: dict[str, int] = {}
    for fp in paths:
        p = Path(fp)
        prefix = f"{p.parent.as_posix().replace('/', '-')}-" if p.parent != Path(".") else ""
        base = f"{prefix}{p.stem}"
        stems[fp] = base
        stem_counts[base] = stem_counts.get(base, 0) + 1

    slug_map: dict[str, str] = {}
    used_slugs: set[str] = set()

    for fp in paths:
        p = Path(fp)
        base = stems[fp]
        if stem_counts[base] > 1:
            ext = p.suffix.lstrip(".").lower()
            candidate = f"{base}-{ext}" if ext else base
        else:
            candidate = base

        slug = candidate
        counter = 2
        while slug in used_slugs:
            slug = f"{candidate}-{counter}"
            counter += 1

        used_slugs.add(slug)
        slug_map[fp] = slug

    return slug_map


def generate_reference_pages(
    vault_path: Path,
    manifest: Manifest,
    file_hashes: dict[str, str],
    file_chunks: dict[str, list[tuple[str, str]]],
) -> list[str]:
    """Generate collection.md and per-document reference pages under references/<name>/."""
    ref_dir = vault_path / "references" / manifest.name
    ref_dir.mkdir(parents=True, exist_ok=True)
    generated: list[str] = []
    slug_map = compute_reference_slugs(manifest.files.keys())

    # 1. collection.md
    reg_file = vault_path / "registry.yaml"
    reg = Registry.load(reg_file) if reg_file.is_file() else None
    resolved_projects: list[str] = []
    for p in manifest.projects:
        if reg is not None:
            proj = reg.by_slug(p) or reg.by_id(p)
            if proj is not None:
                resolved_projects.append(proj.id)
                continue
        resolved_projects.append(p)

    projects_list = [f"    - {pid}" for pid in resolved_projects]
    projects_yaml = "\n".join(projects_list) if projects_list else "    []"

    imported_hashes = {
        path: file_hashes[path]
        for path in sorted(file_hashes)
        if manifest.files.get(path) and manifest.files[path].role != "exclude"
    }
    files_yaml_lines = [f"    {path}: {imported_hashes[path]}" for path in sorted(imported_hashes)]
    files_yaml = "\n".join(files_yaml_lines)

    doc_bullets: list[str] = []
    contents_map_lines: list[str] = []

    for rel_path, entry in sorted(manifest.files.items()):
        if entry.role == "exclude":
            continue
        slug = slug_map.get(rel_path, _doc_slug(rel_path))
        doc_bullets.append(f"- [[{slug}|{rel_path}]] (role: `{entry.role}`)")
        for rel_k, targets in entry.relations.items():
            for t in targets:
                t_slug = slug_map.get(t, _doc_slug(t))
                doc_bullets.append(f"  - {rel_k}: [[{t_slug}|{t}]]")

        chunks = file_chunks.get(rel_path, [])
        if chunks:
            contents_map_lines.append(f"### {rel_path}")
            for heading, _ in chunks:
                if heading:
                    contents_map_lines.append(f"- {heading}")

    docs_section = "\n".join(doc_bullets)
    contents_map_section = "\n".join(contents_map_lines)

    collection_text = f"""---
type: Reference
title: {manifest.title}
description: {manifest.description}
aft:
  collection: {manifest.name}
  projects:
{projects_yaml}
  files:
{files_yaml}
---

# {manifest.title}

## Contents

{manifest.description}

### Documents

{docs_section}

### Contents Map

{contents_map_section}
"""
    col_file = ref_dir / "collection.md"
    col_file.write_text(collection_text, encoding="utf-8")
    generated.append(f"references/{manifest.name}/collection.md")

    # 2. Per-document reference pages
    for rel_path, entry in sorted(manifest.files.items()):
        if entry.role == "exclude":
            continue
        slug = slug_map.get(rel_path, _doc_slug(rel_path))
        f_hash = file_hashes.get(rel_path, "")
        chunks = file_chunks.get(rel_path, [])

        relations_lines: list[str] = []
        for rel_k, targets in entry.relations.items():
            for t in targets:
                t_slug = slug_map.get(t, _doc_slug(t))
                relations_lines.append(f"- {rel_k}: [[{t_slug}|{t}]]")
        relations_text = (
            "\n\n### Relations\n\n" + "\n".join(relations_lines) if relations_lines else ""
        )

        structure_items: list[str] = []
        is_pdf = rel_path.lower().endswith(".pdf")
        for heading, _ in chunks:
            if is_pdf and heading.lower().startswith("page "):
                page_num = heading.split()[-1]
                structure_items.append(f"- [[{rel_path}#page={page_num}|Page {page_num}]]")
            else:
                structure_items.append(f"- {heading}")

        structure_text = "\n".join(structure_items)

        doc_page_text = f"""---
type: Reference
title: {Path(rel_path).name}
description: {entry.role} source file {rel_path} in collection {manifest.name}
aft:
  collection: {manifest.name}
  source: library/{manifest.name}/{rel_path}
  role: {entry.role}
  hash: {f_hash}
---

# {Path(rel_path).name}

## Contents

Source: `library/{manifest.name}/{rel_path}`
Role: `{entry.role}`{relations_text}

### Structure

{structure_text}
"""
        doc_file = ref_dir / f"{slug}.md"
        doc_file.write_text(doc_page_text, encoding="utf-8")
        generated.append(f"references/{manifest.name}/{slug}.md")

    return generated


def write_import_report(
    vault_path: Path,
    manifest: Manifest,
    imported_files: list[str],
    excluded_files: list[str],
    now: datetime,
    map_summary: MapVerificationSummary | None = None,
) -> str:
    """Append report section to reports/<yyyy>/<mm>/<date>.md."""
    date_str = now.strftime("%Y-%m-%d")
    year_str = now.strftime("%Y")
    month_str = now.strftime("%m")
    time_str = now.strftime("%H:%M:%SZ")

    report_dir = vault_path / "reports" / year_str / month_str
    report_dir.mkdir(parents=True, exist_ok=True)
    report_file = report_dir / f"{date_str}.md"

    imported_bullets = "\n".join(
        f"- `{f}`: role `{manifest.files[f].role}`" for f in sorted(imported_files)
    )
    excluded_bullets = (
        "\n".join(f"- `{f}`" for f in sorted(excluded_files)) if excluded_files else "- none"
    )
    projects_str = ", ".join(manifest.projects) if manifest.projects else "none"

    section_text = f"""

## Library import: {manifest.name} ({time_str})

- Collection: `{manifest.name}`
- Title: {manifest.title}
- Source directory: `{manifest.path}`
- Projects: {projects_str}
- Files imported: {len(imported_files)}
- Files excluded: {len(excluded_files)}

### Imported files

{imported_bullets}

### Excluded files

{excluded_bullets}
"""

    if map_summary is not None and map_summary.total > 0:
        rejected_lines = []
        if map_summary.rejected:
            for r in map_summary.rejected:
                src_info = f" from `{r.claim.source_map}`" if r.claim.source_map else ""
                loc_info = f":{r.claim.line}" if r.claim.line else ""
                rejected_lines.append(f"- `{r.claim.target_path}`{src_info}{loc_info}: {r.reason}")
            rejected_text = "\n".join(rejected_lines)
        else:
            rejected_text = "- none"

        section_text += f"""
### Map evidence

- Claims checked: {map_summary.total}
- Claims verified: {len(map_summary.verified)}
- Claims rejected: {len(map_summary.rejected)}

#### Rejected claims

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


def import_collection(
    manifest_path: Path,
    path_override: Path | None = None,
    config: Config | None = None,
    now: datetime | None = None,
) -> ImportResult:
    """Import a library collection into the afterthought vault."""
    cfg = config if config is not None else load()
    vault.require(cfg)
    now_dt = now if now is not None else datetime.now(UTC)

    # 1. Validate manifest before touching anything (DESIGN 11)
    manifest = manifest_mod.validate(manifest_path, path_override)

    # 2. Collect survey info to get source content hashes
    survey_report = survey(manifest.path)
    file_hashes: dict[str, str] = {f.path: f.sha256 for f in survey_report.files}

    imported_files: list[str] = []
    excluded_files: list[str] = []
    file_chunks: dict[str, list[tuple[str, str]]] = {}

    target_lib_dir = cfg.vault / "library" / manifest.name

    with vault.locked(cfg):
        # 3. Copy non-excluded files and make them read-only (0o444)
        for rel_path, entry in manifest.files.items():
            if entry.role == "exclude":
                excluded_files.append(rel_path)
                continue

            src = manifest.path / rel_path
            dst = target_lib_dir / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                os.chmod(dst, 0o644)
            shutil.copy2(src, dst)
            os.chmod(dst, 0o444)
            imported_files.append(rel_path)

            # 4. Extract text chunks and cache in ~/.afterthought/library/
            chunks = extract_document_chunks(dst)
            file_chunks[rel_path] = chunks
            f_hash = file_hashes[rel_path]
            cache_extracted_text(cfg.home, f_hash, rel_path, chunks)

        # Write .hashes.json in library/<name>/
        target_lib_dir.mkdir(parents=True, exist_ok=True)
        hashes_file = target_lib_dir / ".hashes.json"
        imported_hashes = {p: file_hashes[p] for p in imported_files}
        hashes_file.write_text(json.dumps(imported_hashes, indent=2), encoding="utf-8")

        # 5. Generate reference pages
        ref_pages = generate_reference_pages(cfg.vault, manifest, file_hashes, file_chunks)

        # 6. Index extracted text in the `library` collection
        # Derived text is indexed in place of its original (DESIGN 11)
        derived_targets: set[str] = set()
        for _rel_path, entry in manifest.files.items():
            if entry.role == "derived":
                for target in entry.relations.get("derived-from", ()):
                    derived_targets.add(target)

        # Update search index
        index.update(cfg)

        # 7. Write import report
        report_rel = write_import_report(
            cfg.vault, manifest, imported_files, excluded_files, now_dt
        )

        # 8. Commit
        staged_paths: list[str] = [*ref_pages, report_rel]
        if cfg.settings.library.commit:
            staged_paths.append(f"library/{manifest.name}")

        committed = vault.commit(
            cfg.vault,
            staged_paths,
            f"aft library: import collection '{manifest.name}'",
        )

    return ImportResult(
        collection=manifest.name,
        title=manifest.title,
        files_imported=len(imported_files),
        files_excluded=len(excluded_files),
        reference_pages=ref_pages,
        report_path=report_rel,
        committed=committed,
    )


def run(
    manifest_str: str | Path,
    path_override: str | Path | None = None,
    stream: TextIO | None = None,
) -> int:
    """CLI runner for `aft library import <manifest>`."""
    out = sys.stdout if stream is None else stream
    m_path = Path(manifest_str).expanduser()
    p_override = Path(path_override).expanduser() if path_override else None

    result = import_collection(m_path, p_override)
    out.write(
        f"imported collection '{result.collection}' ({result.title}): "
        f"{result.files_imported} files imported, {result.files_excluded} excluded, "
        f"{len(result.reference_pages)} reference pages generated\n"
        f"report: {result.report_path}\n"
    )
    return 0
