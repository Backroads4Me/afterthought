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

import yaml

if TYPE_CHECKING:
    from aft.library.map_evidence import MapVerificationSummary

from aft import index, lint, vault
from aft.config import Config, load
from aft.library import manifest as manifest_mod
from aft.library.manifest import Manifest
from aft.library.survey import HTML_EXTS, JSON_EXTS, MARKDOWN_EXTS, TEXT_EXTS, survey
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
    authority: str = "reference"
    # One line per document whose text could not be extracted: ``"<path>: <reason>"``
    # (M7 review 13).
    extraction_problems: tuple[str, ...] = ()
    # Vault-relative paths of library files and reference pages this (re-)import removed
    # because the collection no longer describes them (M7 review 6).
    retired: tuple[str, ...] = ()


# Why a PDF gave up no text. The two reasons are told apart because they ask for different
# fixes: install the tool, or re-export the document (M7 review 13).
PDF_NO_BINARY = "pdftotext is not installed, so its text was never extracted"
PDF_NO_TEXT = "pdftotext extracted no text"

# Page names a document may never be given. `collection.md` is where the collection records its
# own authority, projects and file hashes, and `index.md` is OKF's directory listing; a document
# called `collection` or `index` lands on one of them and erases it (DESIGN 11 Reference
# pages; M7 review 7).
RESERVED_PAGE_NAMES = frozenset({"collection", "index"})

# The import's own record of what it copied, kept beside the files it copied.
HASHES_FILE_NAME = ".hashes.json"


@dataclass(frozen=True)
class PdfExtraction:
    """A PDF's pages, or the reason it has none."""

    chunks: list[tuple[str, str]]
    problem: str = ""


def extract_pdf(path: Path) -> PdfExtraction:
    """Extract a PDF page by page with `pdftotext -layout`, or say why nothing came out.

    A PDF that yields no text is a failure to report, not an empty document to file: on the
    review's corrupt file this returned one empty page, so the import recorded a document with
    no words in it and the report said nothing (M7 review 13).
    """
    if not shutil.which("pdftotext"):
        return PdfExtraction([], PDF_NO_BINARY)
    try:
        res = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return PdfExtraction([], f"pdftotext failed: {exc}")
    raw_pages = res.stdout.split("\x0c")
    pages: list[tuple[str, str]] = []
    for i, p in enumerate(raw_pages, start=1):
        clean = p.strip()
        if clean or i < len(raw_pages):
            pages.append((f"page {i}", clean))
    if not any(text for _heading, text in pages):
        # Every page came back empty: the file is scanned, corrupt, or not a PDF at all. An
        # empty page is kept in the list above only to hold a document's page numbers steady
        # while there is text somewhere in it.
        said = next((line for line in res.stderr.splitlines() if line.strip()), "")
        return PdfExtraction(
            [],
            f"{PDF_NO_TEXT} ({said})" if said else f"{PDF_NO_TEXT} (a scanned or corrupt PDF?)",
        )
    return PdfExtraction(pages)


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
    """Extract text from HTML documents, preserving headings as chunk titles."""
    heading_pattern = re.compile(
        r"<(?:h[1-6]|title)[^>]*>(.*?)</(?:h[1-6]|title)>", re.IGNORECASE | re.DOTALL
    )
    matches = list(heading_pattern.finditer(text))
    if not matches:
        clean = re.sub(r"<[^>]+>", " ", text)
        clean = re.sub(r"\s+", " ", clean).strip()
        return [("content", clean)] if clean else []

    chunks: list[tuple[str, str]] = []
    first_start = matches[0].start()
    if first_start > 0:
        clean_pre = re.sub(r"<[^>]+>", " ", text[:first_start])
        clean_pre = re.sub(r"\s+", " ", clean_pre).strip()
        if clean_pre:
            chunks.append(("Overview", clean_pre))

    for i, m in enumerate(matches):
        raw_heading = m.group(1)
        clean_h = re.sub(r"<[^>]+>", " ", raw_heading)
        clean_h = re.sub(r"\s+", " ", clean_h).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        clean_body = re.sub(r"<[^>]+>", " ", text[start:end])
        clean_body = re.sub(r"\s+", " ", clean_body).strip()
        chunks.append((clean_h or "Section", clean_body))

    return chunks


@dataclass(frozen=True)
class Extraction:
    """The text a document yielded, and the reason it yielded nothing."""

    chunks: list[tuple[str, str]]
    problem: str = ""


def _read_text(path: Path) -> str:
    """A document's text, with undecodable bytes replaced rather than refused."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_bytes().decode("utf-8", errors="replace")


def extract_document(path: Path) -> Extraction:
    """Extract a document's text, chunked by the kind of document it is.

    The extensions are the survey's own (MARKDOWN_EXTS and friends, from `aft.library.survey`),
    because a file the survey calls Markdown has to be extracted as Markdown here or it lands in
    the library with no text behind it: `.markdown` was classified, copied, and then extracted
    as nothing, so nothing of it was indexed or searchable (M7 review 17). Only a PDF comes back
    with a reason, because it is the one kind that needs a tool from outside this program
    (M7 review 13).
    """
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        pdf = extract_pdf(path)
        return Extraction(list(pdf.chunks), pdf.problem)
    if suffix in MARKDOWN_EXTS or suffix in TEXT_EXTS:
        return Extraction(extract_markdown_chunks(_read_text(path)))
    if suffix in JSON_EXTS:
        return Extraction(extract_json_chunks(_read_text(path)))
    if suffix in HTML_EXTS:
        return Extraction(extract_html_chunks(_read_text(path)))
    return Extraction([])


def extract_document_chunks(path: Path) -> list[tuple[str, str]]:
    """The text chunks of one document; see `extract_document` for what a chunk is."""
    return extract_document(path).chunks


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


def _copyable_source(root: Path, rel_path: str) -> Path:
    """The collection's own file behind one manifest entry, or the reason it cannot be copied.

    A symbolic link is refused rather than followed, and so is any path that resolves outside
    the collection directory: the library holds files the collection owns and points at nothing
    else (DESIGN 11). The survey already refuses to offer a link for a role; this is the check at
    the moment of the copy, for a directory that changed while the manifest was being written.
    A link is refused even where it points at another file of the same collection - what gets
    imported has to be a file the collection owns, not a pointer it happens to carry
    (M7 review 4).
    """
    src = root / rel_path
    if src.is_symlink():
        raise ImportError(
            f"'{rel_path}' is a symbolic link; only files the collection owns are imported"
        )
    resolved = src.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ImportError(f"'{rel_path}' resolves outside the collection directory")
    if not resolved.is_file():
        raise ImportError(f"'{rel_path}' is not a file that can be copied")
    return resolved


def _prune_empty_dirs(root: Path) -> None:
    """Remove the directories under `root` that retiring left empty, deepest first."""
    for path in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass  # not empty: something this import keeps lives in it


def _recorded_library_files(lib_dir: Path) -> set[str]:
    """The paths the last import recorded as its own copies.

    Retirement works from this record rather than from whatever is lying in the library directory:
    the import removes what it filed, not files it never copied. A record that does not read as
    the mapping of paths it should be is refused rather than treated as empty - an empty answer
    here would mean keeping every stale file silently, and guessing would mean deleting what a
    person put there themselves (M7 review 6).
    """
    path = lib_dir / HASHES_FILE_NAME
    if not path.is_file():
        return set()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ImportError(
            f"`{HASHES_FILE_NAME}` in `library/{lib_dir.name}` could not be read ({exc}); "
            "nothing is removed until this import can account for what it filed earlier"
        ) from exc
    if not isinstance(loaded, dict):
        raise ImportError(
            f"`{HASHES_FILE_NAME}` in `library/{lib_dir.name}` is not the record of copied "
            "files it should be; nothing is removed"
        )
    return {str(rel) for rel in loaded}


def _retire_library_files(lib_dir: Path, keep: set[str], recorded: set[str]) -> list[str]:
    """Delete the library files this collection no longer imports, and name what went.

    A re-import states the collection's new shape rather than patching the old one. A file the
    last import recorded and this one does not keep - now `exclude`, or gone from the collection
    directory - has to leave the vault with its reference page: left behind it stays readable,
    stays in git, and is reported by `aft lint` only as an "unrecorded file", while the import
    itself reports success (M7 review 6).
    """
    retired: list[str] = []
    if not lib_dir.is_dir():
        return retired
    for path in sorted(lib_dir.rglob("*"), reverse=True):
        if path.is_dir():
            continue
        rel = path.relative_to(lib_dir).as_posix()
        if rel in keep or rel not in recorded:
            continue
        path.chmod(0o644)  # imported files are stored read-only
        path.unlink()
        retired.append(rel)
    _prune_empty_dirs(lib_dir)
    return retired


def _retire_reference_pages(ref_dir: Path, keep: set[str]) -> list[Path]:
    """Delete the reference pages of documents this collection no longer describes.

    The pages are generated from the files, so a page whose document is gone or is now excluded
    keeps describing a file that is no longer in the library, and injection and the index read
    it. `collection.md` is the collection's own page and every import rewrites it, so it never
    retires (M7 review 6).
    """
    retired: list[Path] = []
    if not ref_dir.is_dir():
        return retired
    for page in sorted(ref_dir.glob("*.md")):
        if page.stem in keep or page.name == "collection.md":
            continue
        page.unlink()
        retired.append(page)
    return retired


def compute_reference_slugs(file_paths) -> dict[str, str]:
    """Derive the unique page name each document is filed under.

    Documents sharing a stem (`guide.pdf`, `guide.txt`) take their extension as well
    (`guide-pdf`, `guide-txt`), so neither overwrites the other. Two names are taken whatever
    else the collection holds: `collection` is where the collection records its own authority,
    projects and file hashes, and `index` is OKF's directory listing. A document called
    `collection.md` becomes `collection-md`, keeping its own page and the one it would have
    erased (DESIGN 11 Reference pages, M7 review 7).

    A reserved name with no extension to lend takes the next free number, as a repeated stem
    does.
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
    used_slugs: set[str] = set(RESERVED_PAGE_NAMES)

    for fp in paths:
        p = Path(fp)
        base = stems[fp]
        if stem_counts[base] > 1:
            ext = p.suffix.lstrip(".").lower()
            candidate = f"{base}-{ext}" if ext else base
        else:
            candidate = base
        if candidate in RESERVED_PAGE_NAMES:
            # The document's own name is already taken by the collection page or the
            # directory listing. Take the extension as the disambiguator, the way a shared
            # stem does; a file with no extension falls through to the numbering below.
            ext = p.suffix.lstrip(".").lower()
            if ext:
                candidate = f"{candidate}-{ext}"

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

    imported_hashes = {
        path: file_hashes[path]
        for path in sorted(file_hashes)
        if manifest.files.get(path) and manifest.files[path].role != "exclude"
    }

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

    collection_text = (
        _frontmatter(
            {
                "type": "Reference",
                "title": manifest.title,
                "description": manifest.description,
                "aft": {
                    "collection": manifest.name,
                    "authority": manifest.authority,
                    "projects": resolved_projects,
                    "files": imported_hashes,
                },
            }
        )
        + f"""

# {manifest.title}

## Contents

{manifest.description}

### Documents

{docs_section}

### Contents Map

{contents_map_section}
"""
    )
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

        doc_page_text = (
            _frontmatter(
                {
                    "type": "Reference",
                    "title": Path(rel_path).name,
                    "description": (
                        f"{entry.role} source file {rel_path} in collection {manifest.name}"
                    ),
                    "aft": {
                        "collection": manifest.name,
                        "source": f"library/{manifest.name}/{rel_path}",
                        "role": entry.role,
                        "hash": f_hash,
                    },
                }
            )
            + f"""

# {Path(rel_path).name}

## Contents

Source: `library/{manifest.name}/{rel_path}`
Role: `{entry.role}`{relations_text}

### Structure

{structure_text}
"""
        )
        doc_file = ref_dir / f"{slug}.md"
        doc_file.write_text(doc_page_text, encoding="utf-8")
        generated.append(f"references/{manifest.name}/{slug}.md")

    return generated


def _frontmatter(fields: dict) -> str:
    """One page's frontmatter: ``---`` lines around YAML written by the dumper.

    Dumped rather than interpolated, because a collection page carries strings that came from
    the manifest and from file names: a title carrying a colon (`Manual: edition 3`) made YAML
    that would not parse, and a description with a newline could bring its own `aft:` keys into
    the page (M7 review 8).

    What is at stake is not cosmetics: frontmatter that fails to parse
    is skipped by `aft lint` and by the index, so the collection stops being hash-checked and
    its documents stop being searchable, all without a word of complaint.
    """
    dumped = yaml.safe_dump(fields, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return f"---\n{dumped}---\n"


def write_import_report(
    vault_path: Path,
    manifest: Manifest,
    imported_files: list[str],
    excluded_files: list[str],
    now: datetime,
    map_summary: MapVerificationSummary | None = None,
    extraction_problems: dict[str, str] | None = None,
) -> str:
    """Append the import's section to `reports/<yyyy>/<mm>/<date>.md`.

    ``extraction_problems`` names each document whose text could not be extracted, with the
    reason (M7 review 13). A document that came back with no words in it is as much a finding as
    a rejected map claim is, and the report is where an import says what it could not do.
    """
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
- Authority: {manifest.authority}
- Source directory: `{manifest.path}`
- Projects: {projects_str}
- Files imported: {len(imported_files)}
- Files excluded: {len(excluded_files)}

### Imported files

{imported_bullets}

### Excluded files

{excluded_bullets}
"""

    if extraction_problems:
        problem_lines = "\n".join(
            f"- `{rel}`: {reason}" for rel, reason in sorted(extraction_problems.items())
        )
        section_text += f"""
### Text extraction problems

These documents were imported but hold no extracted text, so nothing of them is searchable:

{problem_lines}
"""

    if map_summary is not None:
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
    """Import a library collection into the afterthought vault.

    Validate, survey, copy, extract, generate the reference pages, retire what is no longer
    imported, lint, index, report, commit - in that order, under the vault's lock. Two of those
    are checks rather than steps that produce something: the manifest is validated before
    anything is touched, and the pages this run wrote are linted before anything is committed, so
    a collection that cannot be described correctly is not filed (DESIGN 11, M7 review 8).

    No file is copied through a symbolic link, and a re-import leaves no file or page behind for
    a document it no longer describes (M7 review 4, 6). A document whose text could not be
    extracted is named in the report and on stdout, not filed as an empty one (M7 review 13).
    """
    cfg = config if config is not None else load()
    vault.require(cfg)
    now_dt = now if now is not None else datetime.now(UTC)

    # 1. Validate manifest before touching anything (DESIGN 11)
    manifest = manifest_mod.validate(manifest_path, path_override)

    # A collection is filed against projects the vault already knows. `aft lint` reports a
    # reference page whose `aft.projects` names a project the registry has never seen, and a
    # mistyped slug would otherwise become a new project the next time the dream reads the page.
    # So this is said before anything is written, rather than reaching a commit that the vault's
    # own checker then disagrees with (M7 review 8).
    registry = Registry.load(cfg.registry_path) if cfg.registry_path.is_file() else None
    unknown = [
        name
        for name in manifest.projects
        if registry is None or (registry.by_slug(name) is None and registry.by_id(name) is None)
    ]
    if unknown:
        raise ImportError(
            "the manifest names a project the vault does not know: "
            + ", ".join(unknown)
            + "; register it before importing this collection"
        )

    # 2. Collect survey info to get source content hashes
    survey_report = survey(manifest.path)
    file_hashes: dict[str, str] = {f.path: f.sha256 for f in survey_report.files}

    imported_files: list[str] = []
    excluded_files: list[str] = []
    file_chunks: dict[str, list[tuple[str, str]]] = {}
    extraction_problems: dict[str, str] = {}

    target_lib_dir = cfg.vault / "library" / manifest.name

    with vault.locked(cfg):
        # 3. Copy non-excluded files and make them read-only (0o444)
        for rel_path, entry in manifest.files.items():
            if entry.role == "exclude":
                excluded_files.append(rel_path)
                continue

            src = _copyable_source(manifest.path, rel_path)
            dst = target_lib_dir / rel_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                os.chmod(dst, 0o644)
            shutil.copy2(src, dst)
            os.chmod(dst, 0o444)
            imported_files.append(rel_path)

            # 4. Extract text chunks and cache in ~/.afterthought/library/. A document that
            # gave up no text is named with its reason, and the reason travels to the report.
            extraction = extract_document(dst)
            file_chunks[rel_path] = extraction.chunks
            if extraction.problem:
                extraction_problems[rel_path] = extraction.problem
            cache_extracted_text(cfg.home, file_hashes[rel_path], rel_path, extraction.chunks)

        # What the previous import filed, read before its record is overwritten: retirement
        # removes what this program copied and nothing else (M7 review 6).
        recorded = _recorded_library_files(target_lib_dir)

        # Write the import's own hash record beside the files it copied.
        target_lib_dir.mkdir(parents=True, exist_ok=True)
        hashes_file = target_lib_dir / HASHES_FILE_NAME
        imported_hashes = {p: file_hashes[p] for p in imported_files}
        hashes_file.write_text(json.dumps(imported_hashes, indent=2), encoding="utf-8")

        # 5. Generate the reference pages, then retire what this collection no longer
        # describes. A re-import states the collection's new shape rather than patching the
        # old one: a file that is excluded now, or that the new manifest simply does not name,
        # leaves the vault with its reference page, instead of staying readable, searchable and
        # tracked while `aft lint` calls it an unrecorded file (M7 review 6).
        ref_pages = generate_reference_pages(cfg.vault, manifest, file_hashes, file_chunks)
        retired = [
            f"library/{manifest.name}/{rel}"
            for rel in _retire_library_files(target_lib_dir, set(imported_files), recorded)
        ]
        retired += [
            f"references/{manifest.name}/{page.name}"
            for page in _retire_reference_pages(
                cfg.vault / "references" / manifest.name,
                {Path(page).stem for page in ref_pages},
            )
        ]

        # 6. Lint the pages this import wrote before anything is committed. A page whose
        # frontmatter the dumper could not make of, or which breaks a page rule, stops the run
        # here rather than reaching a commit `aft lint` would then disagree with (M7 review 8).
        # The files copied in step 3 stay on disk for whoever is watching to look at; nothing
        # is committed and no report says the import finished.
        findings = lint.lint_pages(cfg.vault, ref_pages)
        if findings:
            raise ImportError(
                "the reference pages failed lint, so nothing was committed: "
                + "; ".join(str(finding) for finding in findings[:5])
            )

        # 7. Index the library as it now stands. Derived text is indexed in place of its
        # original (DESIGN 11), which the index reads off the pages written above.
        index.update(cfg)

        # 6.5 Verify claims in exclude-role map files (BUILD-PLAN M7.4, DESIGN 11)
        from aft.library.map_evidence import is_map_file, verify_map_files

        exclude_map_files = [
            rel_path
            for rel_path, entry in manifest.files.items()
            if entry.role == "exclude" and is_map_file(rel_path)
        ]
        map_summary = None
        if exclude_map_files:
            map_summary = verify_map_files(
                exclude_map_files,
                manifest.path,
                extracted_chunks=file_chunks,
                home=cfg.home,
            )

        # 8. Write the import report.
        report_rel = write_import_report(
            cfg.vault,
            manifest,
            imported_files,
            excluded_files,
            now_dt,
            map_summary=map_summary,
            extraction_problems=extraction_problems,
        )

        # 9. Make the vault's .gitignore tell the truth about `library.commit`. `aft init`
        # wrote that file once, and the setting can change afterwards: left as it was, an
        # import with the setting off left `library/<name>/` untracked and not ignored, and the
        # dream's checkpoint commit picked the source files up as human edits (M7 review 12).
        gitignore_changed = vault.maintain_library_ignore(cfg.vault, cfg.settings.library.commit)

        # 10. Commit. The two directories go on the command rather than the individual pages,
        # so the removals of step 5 are committed along with the pages that replaced them.
        staged_paths: list[str] = [f"references/{manifest.name}", report_rel]
        if cfg.settings.library.commit:
            staged_paths.append(f"library/{manifest.name}")
        if gitignore_changed:
            staged_paths.append(".gitignore")

        committed = vault.commit(
            cfg.vault,
            staged_paths,
            f"aft library: import collection '{manifest.name}' ({manifest.authority})",
        )

    return ImportResult(
        collection=manifest.name,
        title=manifest.title,
        files_imported=len(imported_files),
        files_excluded=len(excluded_files),
        reference_pages=ref_pages,
        report_path=report_rel,
        committed=committed,
        map_summary=map_summary,
        authority=manifest.authority,
        extraction_problems=tuple(
            f"{rel}: {reason}" for rel, reason in sorted(extraction_problems.items())
        ),
        retired=tuple(retired),
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
    for problem in result.extraction_problems:
        out.write(f"no text extracted: {problem}\n")
    if result.retired:
        out.write(f"retired {len(result.retired)} file(s) this collection no longer imports\n")
    return 0
