"""Manifest loading and validation for library collections (design section 11).

The manifest specifies a collection's name, title, description, target projects,
and a mapping of every surveyed file to exactly one role (original, derived,
errata, notes, exclude) and its relations (derived-from, errata-for, supersedes).
The validator enforces that every surveyed file has one role, relations point
inside the collection, and instruction files and tooling are excluded.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from aft.library import survey

ROLES = frozenset({"original", "derived", "errata", "notes", "exclude"})
RELATIONS = frozenset({"derived-from", "errata-for", "supersedes"})
RELATION_ALIASES = {
    "derived_from": "derived-from",
    "errata_for": "errata-for",
}

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class ManifestError(Exception):
    """Raised when a manifest file is missing, malformed, or fails validation."""


@dataclass(frozen=True)
class FileEntry:
    path: str
    role: str
    relations: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class Manifest:
    name: str
    title: str
    description: str
    projects: tuple[str, ...]
    path: Path
    files: dict[str, FileEntry]


def normalize_relation_name(name: str) -> str:
    """Normalize relation key to hyphenated form."""
    lowered = name.lower().strip()
    return RELATION_ALIASES.get(lowered, lowered)


def load_manifest(
    manifest_path: Path, path_override: Path | None = None
) -> tuple[Manifest, list[str]]:
    """Load and parse a manifest YAML file, returning the manifest and syntactic errors."""
    errors: list[str] = []
    resolved_path = manifest_path.resolve()
    if not resolved_path.exists():
        raise ManifestError(f"manifest file does not exist: {manifest_path}")
    if not resolved_path.is_file():
        raise ManifestError(f"manifest path is not a file: {manifest_path}")

    try:
        raw_text = resolved_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"manifest is not valid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ManifestError("manifest root must be a YAML mapping")

    # Required top-level string fields
    name = data.get("name")
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        errors.append(f"name must be a lowercase alphanumeric identifier (got {name!r})")
        name = str(name or "")

    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append("title must be a non-empty string")
        title = str(title or "")

    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        errors.append("description must be a non-empty string")
        description = str(description or "")

    # Target projects
    projects_raw = data.get("projects")
    if not isinstance(projects_raw, list) or not all(isinstance(p, str) for p in projects_raw):
        errors.append("projects must be a list of project identifiers")
        projects = ()
    else:
        projects = tuple(projects_raw)

    # Collection directory location
    collection_path_val = (
        path_override
        if path_override is not None
        else data.get("path") or data.get("source") or data.get("root")
    )
    if collection_path_val is None:
        errors.append("manifest must specify 'path' to collection directory or provide an override")
        collection_dir = resolved_path.parent
    else:
        raw_p = Path(collection_path_val)
        if raw_p.is_absolute():
            collection_dir = raw_p.resolve()
        else:
            collection_dir = (resolved_path.parent / raw_p).resolve()

    # Files mapping
    files_raw = data.get("files")
    file_entries: dict[str, FileEntry] = {}
    if not isinstance(files_raw, dict):
        errors.append("files must be a mapping of file paths to role and relations")
    else:
        for fpath, fspec in files_raw.items():
            entry, entry_errs = _parse_file_entry(str(fpath), fspec)
            errors.extend(entry_errs)
            file_entries[entry.path] = entry

    manifest = Manifest(
        name=name,
        title=title,
        description=description,
        projects=projects,
        path=collection_dir,
        files=file_entries,
    )
    return manifest, errors


def _parse_file_entry(fpath: str, spec: object) -> tuple[FileEntry, list[str]]:
    """Parse one file entry from either a string role or a dictionary spec."""
    errors: list[str] = []
    norm_path = Path(fpath).as_posix()

    if isinstance(spec, str):
        role = spec.strip().lower()
        if role not in ROLES:
            errors.append(f"file '{norm_path}': unknown role '{role}'")
        return FileEntry(path=norm_path, role=role), errors

    if isinstance(spec, dict):
        role_val = spec.get("role")
        if not isinstance(role_val, str) or role_val.strip().lower() not in ROLES:
            errors.append(
                f"file '{norm_path}': role must be one of {sorted(ROLES)} (got {role_val!r})"
            )
            role = str(role_val or "")
        else:
            role = role_val.strip().lower()

        relations_raw = spec.get("relations") or {}
        relations: dict[str, tuple[str, ...]] = {}
        if not isinstance(relations_raw, dict):
            errors.append(f"file '{norm_path}': relations must be a mapping")
        else:
            for rel_k, rel_v in relations_raw.items():
                norm_k = normalize_relation_name(str(rel_k))
                if norm_k not in RELATIONS:
                    expected = sorted(RELATIONS)
                    errors.append(
                        f"file '{norm_path}': unknown relation '{rel_k}', "
                        f"expected one of {expected}"
                    )
                if isinstance(rel_v, str):
                    relations[norm_k] = (Path(rel_v).as_posix(),)
                elif isinstance(rel_v, list) and all(isinstance(x, str) for x in rel_v):
                    relations[norm_k] = tuple(Path(x).as_posix() for x in rel_v)
                else:
                    errors.append(
                        f"file '{norm_path}': relation '{rel_k}' target must be a path "
                        "string or list of paths"
                    )

        return FileEntry(path=norm_path, role=role, relations=relations), errors

    errors.append(f"file '{norm_path}': entry must be a role string or mapping")
    return FileEntry(path=norm_path, role=""), errors


def validate_manifest(manifest: Manifest) -> list[str]:
    """Validate manifest semantic rules against surveyed collection and DESIGN 11."""
    errors: list[str] = []

    # Check collection path exists and is a directory
    if not manifest.path.exists():
        errors.append(f"collection directory does not exist: {manifest.path}")
        return errors
    if not manifest.path.is_dir():
        errors.append(f"collection path is not a directory: {manifest.path}")
        return errors

    # Survey the collection to check every surveyed file
    try:
        report = survey.survey(manifest.path)
    except survey.SurveyError as exc:
        errors.append(f"failed to survey collection directory: {exc}")
        return errors

    surveyed_paths = {f.path for f in report.files}
    manifest_paths = set(manifest.files.keys())

    # 1. Every surveyed file has exactly one role in the manifest
    unassigned = sorted(surveyed_paths - manifest_paths)
    for p in unassigned:
        errors.append(f"unassigned file: '{p}' is in collection but has no role in manifest")

    # Files listed in manifest that are not in the collection
    missing_from_collection = sorted(manifest_paths - surveyed_paths)
    for p in missing_from_collection:
        errors.append(f"unknown file: '{p}' listed in manifest does not exist in collection")

    # 2. Instruction files and tooling must be role: exclude
    for finfo in report.files:
        if finfo.path not in manifest.files:
            continue
        entry = manifest.files[finfo.path]
        if finfo.is_instruction and entry.role != "exclude":
            errors.append(
                f"instruction file '{finfo.path}' must have role 'exclude', got '{entry.role}'"
            )
        if finfo.is_tooling and entry.role != "exclude":
            errors.append(
                f"tooling file '{finfo.path}' must have role 'exclude', got '{entry.role}'"
            )

    # 3. Relations point inside the collection and follow authority rules
    for fpath, entry in manifest.files.items():
        if fpath not in surveyed_paths:
            continue
        for rel_type, targets in entry.relations.items():
            for target in targets:
                if target == fpath:
                    errors.append(f"file '{fpath}': relation '{rel_type}' cannot point to itself")
                elif target not in surveyed_paths:
                    errors.append(
                        f"file '{fpath}': relation '{rel_type}' target '{target}' "
                        "does not exist in collection"
                    )
                else:
                    target_entry = manifest.files.get(target)
                    if target_entry and target_entry.role == "exclude":
                        errors.append(
                            f"file '{fpath}': relation '{rel_type}' points to "
                            f"excluded file '{target}'"
                        )

    return errors


def validate(manifest_path: Path, path_override: Path | None = None) -> Manifest:
    """Load and validate manifest, raising ManifestError on any validation finding."""
    manifest, syntax_errors = load_manifest(manifest_path, path_override)
    semantic_errors = validate_manifest(manifest)
    all_errors = syntax_errors + semantic_errors
    if all_errors:
        formatted = "\n  - ".join(all_errors)
        raise ManifestError(f"manifest validation failed:\n  - {formatted}")
    return manifest


def run(
    manifest_str: str | Path,
    path_override: str | Path | None = None,
    stream: sys.stdout.__class__ | None = None,
) -> int:
    """CLI runner for `aft library validate <manifest>`."""
    out = sys.stdout if stream is None else stream
    m_path = Path(manifest_str).expanduser()
    p_override = Path(path_override).expanduser() if path_override else None

    manifest = validate(m_path, p_override)
    file_count = len(manifest.files)
    excluded = sum(1 for f in manifest.files.values() if f.role == "exclude")
    relations_count = sum(len(f.relations) for f in manifest.files.values())

    out.write(
        f"manifest '{manifest.name}' is valid: {file_count} files accounted for "
        f"({excluded} excluded, {relations_count} relations)\n"
    )
    return 0
