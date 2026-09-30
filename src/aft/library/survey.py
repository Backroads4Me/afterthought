"""Survey a collection directory before library import (design section 11).

The survey lists every file with its type, size and sha256 hash, flags duplicate
files, suggests likely derived pairs sharing a stem, flags instruction files and
tooling for exclusion, names any symbolic link, and prints the import procedure. It
changes nothing on disk.

A symbolic link is named and nothing more. It is not hashed or offered for a role, so the
manifest cannot ask for it and the import cannot copy what it points at, which may lie
outside the collection altogether (DESIGN 11, M7 review 4).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

# Directories skipped during traversal: VCS metadata and local caches/environments.
SKIP_DIR_NAMES = {
    ".git",
    ".svn",
    ".hg",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".cache",
    ".obsidian",
}

# Instruction file names that must not be imported into memory.
INSTRUCTION_NAMES = {
    "agents.md",
    "claude.md",
    "copilot.md",
    "gemini.md",
    ".cursorrules",
    ".windsurfrules",
    "copilot-instructions.md",
}

# Tooling and build configuration file names. A file whose name starts with a dot is
# tooling by rule below, so this list holds the names that are tooling without one.
TOOLING_EXACT_NAMES = {
    "makefile",
    "dockerfile",
    "justfile",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "cargo.toml",
    "cargo.lock",
    "tsconfig.json",
    "manifest.json",
    "index.json",
    "toc.json",
}

# Subdirectories containing tooling, build scripts, or test code.
TOOLING_DIR_NAMES = {
    ".githooks",
    "tools",
    "scripts",
    "bin",
    "tests",
    "test",
}

# Extensions a document is written in, by the kind of document it is. The import extracts
# exactly these (aft.library.importing), so a file is never classified as one kind of document
# and then extracted as another.
MARKDOWN_EXTS = frozenset({".md", ".markdown", ".mdown"})
TEXT_EXTS = frozenset({".txt", ".text", ".rst"})
JSON_EXTS = frozenset({".json", ".json5"})
HTML_EXTS = frozenset({".html", ".htm"})

# Common document originals and derived formats for pair suggestion.
ORIGINAL_EXTS = {".pdf", ".docx", ".doc", ".html", ".xml", ".csv"}
DERIVED_EXTS = {".md", ".markdown", ".txt", ".json"}

IMPORT_PROCEDURE = """\
Import procedure:
1. Write a manifest: create a YAML manifest file naming the collection, description,
   authority (authoritative or reference; ask the user, never choose),
   projects it applies to, one role for every surveyed file, and relations between files.
   Roles:
   - original: a source document (weight comes from the collection's authority, not the role)
   - derived: version made from another file (record derived-from relation)
   - errata: corrections to another file (record errata-for relation)
   - notes: working notes kept as sources and mined for facts
   - exclude: not imported (build scripts, generated indexes, git history, AGENTS.md,
     any dotfile, and everything else the survey flags as tooling)
   A symbolic link is never imported and gets no role: what it points at may lie outside
   the collection, and the library holds only files the collection owns.
   Relations:
   - derived-from: points to the original file in the collection
   - errata-for: points to the document being corrected
   - supersedes: points to an older edition being replaced
2. Validate the manifest:
   Run `aft library validate <manifest>` to check that every file has exactly one role,
   relations point inside the collection, and instruction files and tooling are exclude.
3. Import the collection:
   Run `aft library import <manifest>` to copy files, extract text, index documents,
   and generate reference page structure.
4. Add verified descriptions:
   Run `aft library describe <manifest> <descriptions-file>` to add verified
   descriptions (what, version, when to use) with quotes checked against
   extracted text and recorded hashes. The agent never edits reference pages directly.
5. Propose facts:
   Run `aft library propose <manifest> <facts-file>` to propose candidate facts
   from documents, notes, and excluded instruction files.
6. Report and commit:
   Review the import report and commit to the vault.
"""


class SurveyError(Exception):
    """Raised when a path to survey cannot be read or is not a directory."""


@dataclass(frozen=True)
class FileInfo:
    path: str
    file_type: str
    size: int
    sha256: str
    is_instruction: bool = False
    is_tooling: bool = False


@dataclass(frozen=True)
class DerivedPair:
    original: str
    derived: str


@dataclass(frozen=True)
class SurveyReport:
    root: str
    files: tuple[FileInfo, ...]
    duplicates: tuple[tuple[str, ...], ...]
    derived_pairs: tuple[DerivedPair, ...]
    instruction_files: tuple[str, ...]
    tooling_files: tuple[str, ...]
    # Named, and nothing else: a link is not a file to import, so it gets no role and is
    # never copied (see `survey`). A directory reached through a link is listed with its
    # trailing slash, since it is the directory that is the link.
    symlinks: tuple[str, ...] = ()


def classify_file_type(path: Path) -> str:
    """Identify a human-readable file category from extension or content."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        return "PDF"
    if ext in MARKDOWN_EXTS:
        return "Markdown"
    if ext in JSON_EXTS:
        return "JSON"
    if ext in (".yaml", ".yml"):
        return "YAML"
    if ext in TEXT_EXTS:
        return "Text"
    if ext in HTML_EXTS:
        return "HTML"
    if ext in (".csv", ".tsv"):
        return "CSV"
    if ext == ".xml":
        return "XML"
    if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico", ".tiff"):
        return "Image"
    if ext in (".py", ".sh", ".bash", ".js", ".ts", ".c", ".h", ".cpp", ".rs", ".go"):
        return "Code"
    try:
        with path.open("rb") as f:
            chunk = f.read(1024)
            if b"\x00" in chunk:
                return "Binary"
            return "Text"
    except OSError:
        return "Binary"


def file_sha256(path: Path) -> str:
    """Compute the SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def is_instruction_file(rel: Path) -> bool:
    """Return True if the file gives agent or editor instructions."""
    name = rel.name.lower()
    if name in INSTRUCTION_NAMES:
        return True
    parts = [p.lower() for p in rel.parts]
    if ".claude" in parts or ".cursor" in parts:
        return True
    if name.endswith(("-instructions.md", "_instructions.md", ".instruction.md")):
        return True
    return False


def is_tooling_file(rel: Path) -> bool:
    """Return True if the file is build or tool configuration, a script, or a test.

    A dotfile is tooling whatever it is called: configuration and tool state is what carries a
    leading dot, and DESIGN 11 step 3 admits no tooling into a collection - a dotfile is not a
    document a reader came to read. Instruction files are named by `is_instruction_file`, which
    answers first and owns the ones that are both.
    """
    name = rel.name.lower()
    if name in TOOLING_EXACT_NAMES:
        return True
    if name.startswith("."):
        return True
    parts = [p.lower() for p in rel.parts]
    for d in TOOLING_DIR_NAMES:
        if d in parts[:-1]:
            return True
    if name.startswith("test_") or name.endswith(("_test.py", "_spec.rb")):
        return True
    if name in ("regenerate.py", "generate.py", "build.py", "build.sh"):
        return True
    if any(p.endswith(".egg-info") for p in parts):
        return True
    return False


def survey(root: Path) -> SurveyReport:
    """Inspect every file in root without making any changes.

    A symbolic link is named in `SurveyReport.symlinks` and listed nowhere else: not hashed,
    not offered for a role, not imported. A link's target can sit outside the collection, and
    DESIGN 11 keeps the library pointing at nothing outside the vault, so copying through a
    link would pull an unrelated file in and record it as the collection's own. Because the
    link is not a surveyed file, a manifest cannot give it a role either: the validator refuses
    a role for a path the survey did not report (M7 review 4). A directory that is itself a
    link is not walked, and is named with its trailing slash.
    """
    resolved = root.resolve()
    if not resolved.exists():
        raise SurveyError(f"path does not exist: {root}")
    if not resolved.is_dir():
        raise SurveyError(f"path is not a directory: {root}")

    collected: list[FileInfo] = []
    links: list[str] = []
    for dirpath, dirnames, filenames in os.walk(resolved):
        walked: list[str] = []
        for d in sorted(dirnames):
            if d in SKIP_DIR_NAMES:
                continue
            linked = Path(dirpath) / d
            if linked.is_symlink():
                links.append(f"{linked.relative_to(resolved).as_posix()}/")
                continue
            walked.append(d)
        dirnames[:] = walked
        for fname in sorted(filenames):
            full_path = Path(dirpath) / fname
            if full_path.is_symlink():
                links.append(full_path.relative_to(resolved).as_posix())
                continue
            if not full_path.is_file():
                continue
            rel = full_path.relative_to(resolved)
            rel_str = rel.as_posix()
            ftype = classify_file_type(full_path)
            size = full_path.stat().st_size
            digest = file_sha256(full_path)
            instruction = is_instruction_file(rel)
            tooling = is_tooling_file(rel)
            collected.append(
                FileInfo(
                    path=rel_str,
                    file_type=ftype,
                    size=size,
                    sha256=digest,
                    is_instruction=instruction,
                    is_tooling=tooling,
                )
            )

    collected.sort(key=lambda f: f.path)

    # Detect duplicate files by content hash.
    by_hash: dict[str, list[str]] = {}
    for finfo in collected:
        by_hash.setdefault(finfo.sha256, []).append(finfo.path)
    duplicates = tuple(tuple(paths) for paths in by_hash.values() if len(paths) > 1)

    # Detect likely derived pairs sharing a base name with different extensions.
    pairs = _find_derived_pairs(collected)

    instruction_files = tuple(f.path for f in collected if f.is_instruction)
    tooling_files = tuple(f.path for f in collected if f.is_tooling)

    return SurveyReport(
        root=str(root),
        files=tuple(collected),
        duplicates=duplicates,
        derived_pairs=pairs,
        instruction_files=instruction_files,
        tooling_files=tooling_files,
        symlinks=tuple(sorted(links)),
    )


def _find_derived_pairs(files: list[FileInfo]) -> tuple[DerivedPair, ...]:
    """Find pairs of files that appear to be original and derived versions."""
    pairs: list[DerivedPair] = []
    seen: set[tuple[str, str]] = set()

    # Group by parent and stem first (highest confidence).
    by_parent_and_stem: dict[tuple[str, str], list[FileInfo]] = {}
    for f in files:
        p = Path(f.path)
        key = (p.parent.as_posix(), p.stem.lower())
        by_parent_and_stem.setdefault(key, []).append(f)

    for group in by_parent_and_stem.values():
        if len(group) < 2:
            continue
        _pair_group(group, pairs, seen)

    # Also check cross-directory matches by stem where one is in a source/original directory
    # or one is a document original extension and the other is a derived text format.
    by_stem: dict[str, list[FileInfo]] = {}
    for f in files:
        p = Path(f.path)
        by_stem.setdefault(p.stem.lower(), []).append(f)

    for group in by_stem.values():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                f1, f2 = group[i], group[j]
                p1, p2 = Path(f1.path), Path(f2.path)
                if p1.suffix.lower() == p2.suffix.lower():
                    continue
                pair = _order_pair(f1, f2)
                if pair and (pair.original, pair.derived) not in seen:
                    seen.add((pair.original, pair.derived))
                    pairs.append(pair)

    pairs.sort(key=lambda p: (p.original, p.derived))
    return tuple(pairs)


def _pair_group(
    group: list[FileInfo], pairs: list[DerivedPair], seen: set[tuple[str, str]]
) -> None:
    for i in range(len(group)):
        for j in range(i + 1, len(group)):
            f1, f2 = group[i], group[j]
            p1, p2 = Path(f1.path), Path(f2.path)
            if p1.suffix.lower() == p2.suffix.lower():
                continue
            pair = _order_pair(f1, f2)
            if pair and (pair.original, pair.derived) not in seen:
                seen.add((pair.original, pair.derived))
                pairs.append(pair)


def _order_pair(f1: FileInfo, f2: FileInfo) -> DerivedPair | None:
    """Decide which file is original and which is derived."""
    ext1 = Path(f1.path).suffix.lower()
    ext2 = Path(f2.path).suffix.lower()
    p1_parts = [p.lower() for p in Path(f1.path).parts]
    p2_parts = [p.lower() for p in Path(f2.path).parts]

    # Check path cues (e.g. source/ directory).
    if "source" in p1_parts and "source" not in p2_parts:
        return DerivedPair(original=f1.path, derived=f2.path)
    if "source" in p2_parts and "source" not in p1_parts:
        return DerivedPair(original=f2.path, derived=f1.path)

    # Check extension cues.
    if ext1 in ORIGINAL_EXTS and ext2 in DERIVED_EXTS:
        return DerivedPair(original=f1.path, derived=f2.path)
    if ext2 in ORIGINAL_EXTS and ext1 in DERIVED_EXTS:
        return DerivedPair(original=f2.path, derived=f1.path)

    if ext1 == ".pdf" and ext2 != ".pdf":
        return DerivedPair(original=f1.path, derived=f2.path)
    if ext2 == ".pdf" and ext1 != ".pdf":
        return DerivedPair(original=f2.path, derived=f1.path)

    # Default to alphabetical ordering if no clear hierarchy.
    if f1.path < f2.path:
        return DerivedPair(original=f1.path, derived=f2.path)
    return DerivedPair(original=f2.path, derived=f1.path)


def format_survey(report: SurveyReport) -> str:
    """Format the survey report as human-readable text."""
    count = len(report.files)
    suffix = "s" if count != 1 else ""
    lines: list[str] = [
        f"Survey of {report.root} ({count} file{suffix})",
        "",
        "Files:",
    ]
    for finfo in report.files:
        flags: list[str] = []
        if finfo.is_instruction:
            flags.append("instruction file")
        if finfo.is_tooling:
            flags.append("tooling")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        desc = f"{finfo.file_type}, {finfo.size} bytes, sha256:{finfo.sha256}"
        lines.append(f"  - {finfo.path} ({desc}){flag_str}")

    lines.append("")
    lines.append("Duplicates:")
    if not report.duplicates:
        lines.append("  none")
    else:
        for group in report.duplicates:
            lines.append(f"  - duplicate group ({len(group)} files):")
            for p in group:
                lines.append(f"      {p}")

    lines.append("")
    lines.append("Suggested derived pairs:")
    if not report.derived_pairs:
        lines.append("  none")
    else:
        for pair in report.derived_pairs:
            lines.append(f"  - {pair.derived} -> derived-from: {pair.original}")

    lines.append("")
    lines.append("Flagged for exclusion:")
    lines.append(f"  Instruction files ({len(report.instruction_files)}):")
    if not report.instruction_files:
        lines.append("    none")
    else:
        for p in report.instruction_files:
            lines.append(f"    - {p}")

    lines.append(f"  Tooling ({len(report.tooling_files)}):")
    if not report.tooling_files:
        lines.append("    none")
    else:
        for p in report.tooling_files:
            lines.append(f"    - {p}")
    lines.append("")
    lines.append("Symlinks (never imported; a link's target may lie outside the collection):")
    if not report.symlinks:
        lines.append("    none")
    else:
        for p in report.symlinks:
            lines.append(f"    - {p}")

    lines.append("")
    lines.append(IMPORT_PROCEDURE.rstrip())
    return "\n".join(lines) + "\n"


def format_json(report: SurveyReport) -> str:
    """Format the survey report as JSON."""
    return json.dumps(asdict(report), indent=2) + "\n"


def run(
    path_str: str | Path,
    as_json: bool = False,
    stream: sys.stdout.__class__ | None = None,
) -> int:
    """Execute survey command for a given path string."""
    out = sys.stdout if stream is None else stream
    path = Path(path_str).expanduser()
    report = survey(path)
    text = format_json(report) if as_json else format_survey(report)
    out.write(text)
    return 0
