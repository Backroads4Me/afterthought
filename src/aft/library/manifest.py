"""Manifest loading and validation for library collections (design section 11).

The manifest specifies a collection's name, title, description, target projects,
and a mapping of every surveyed file to exactly one role (original, derived,
errata, notes, exclude) and its relations (derived-from, errata-for, supersedes).
The validator enforces that every surveyed file has one role, relations point
inside the collection, instruction files and tooling are excluded, and relations fit roles.
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
AUTHORITIES = frozenset({"authoritative", "reference"})
DEFAULT_AUTHORITY = "reference"

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class ManifestError(Exception):
    """Raised when a manifest file is missing, malformed, or fails validation."""


class UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys."""

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
    authority: str = DEFAULT_AUTHORITY


def normalize_relation_name(name: str) -> str:
    """Normalize relation key to lowercase stripped form."""
    return name.lower().strip()


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
        data = yaml.load(raw_text, Loader=UniqueKeyLoader)
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
        path_override if path_override is not None else data.get("path", resolved_path.parent)
    )
    if isinstance(collection_path_val, (str, Path)):
        col_path = Path(collection_path_val)
        if not col_path.is_absolute():
            col_path = (resolved_path.parent / col_path).resolve()
        collection_path = col_path
    else:
        errors.append("path must be a valid directory path string")
        collection_path = resolved_path.parent

    # File mapping
    files_raw = data.get("files")
    files: dict[str, FileEntry] = {}
    if not isinstance(files_raw, dict):
        errors.append("files must be a dictionary mapping relative file paths to entries")
    else:
        for fpath, raw_entry in files_raw.items():
            if not isinstance(fpath, str):
                errors.append(f"file path key must be string, got {fpath!r}")
                continue

            # Allow string shorthand for role with no relations
            if isinstance(raw_entry, str):
                role = raw_entry.strip().lower()
                if role not in ROLES:
                    errors.append(
                        f"file '{fpath}': role must be one of {sorted(ROLES)}, got {role!r}"
                    )
                files[fpath] = FileEntry(path=fpath, role=role, relations={})
                continue

            if not isinstance(raw_entry, dict):
                entry_type = type(raw_entry).__name__
                errors.append(
                    f"file '{fpath}': entry must be a dictionary or string role, got {entry_type}"
                )
                continue

            role = raw_entry.get("role")
            if not isinstance(role, str) or role.strip().lower() not in ROLES:
                errors.append(f"file '{fpath}': role must be one of {sorted(ROLES)}, got {role!r}")
                role = str(role or "")
            else:
                role = role.strip().lower()

            relations_raw = raw_entry.get("relations", {})
            relations: dict[str, tuple[str, ...]] = {}
            if not isinstance(relations_raw, dict):
                errors.append(f"file '{fpath}': relations must be a mapping")
            else:
                for rel_key, rel_val in relations_raw.items():
                    if not isinstance(rel_key, str):
                        errors.append(f"file '{fpath}': relation type must be string")
                        continue
                    norm_rel = normalize_relation_name(rel_key)
                    if norm_rel not in RELATIONS:
                        errors.append(
                            f"file '{fpath}': unknown relation '{rel_key}', "
                            f"must be one of {sorted(RELATIONS)}"
                        )
                        continue

                    # Targets can be single string or list of strings
                    if isinstance(rel_val, str):
                        targets = (rel_val.strip(),)
                    elif isinstance(rel_val, list) and all(isinstance(t, str) for t in rel_val):
                        targets = tuple(t.strip() for t in rel_val)
                    else:
                        errors.append(
                            f"file '{fpath}': relation '{rel_key}' targets must be "
                            "a string path or list of string paths"
                        )
                        continue

                    relations[norm_rel] = targets

            files[fpath] = FileEntry(path=fpath, role=role, relations=relations)

    # Collection authority (DESIGN 11, BUILD-PLAN F5)
    authority_val = data.get("authority")
    if authority_val is None:
        authority = DEFAULT_AUTHORITY
    elif not isinstance(authority_val, str):
        errors.append(f"authority must be a string, got {type(authority_val).__name__}")
        authority = DEFAULT_AUTHORITY
    else:
        authority_norm = authority_val.strip().lower()
        if authority_norm not in AUTHORITIES:
            errors.append(f"authority must be one of {sorted(AUTHORITIES)}, got {authority_val!r}")
            authority = DEFAULT_AUTHORITY
        else:
            authority = authority_norm

    manifest = Manifest(
        name=name,
        title=title,
        description=description,
        projects=projects,
        path=collection_path,
        files=files,
        authority=authority,
    )
    return manifest, errors


def validate_manifest(manifest: Manifest) -> list[str]:
    """Validate a loaded manifest against its surveyed collection directory."""
    errors: list[str] = []

    if manifest.authority not in AUTHORITIES:
        errors.append(f"authority must be one of {sorted(AUTHORITIES)}, got {manifest.authority!r}")

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

    # 2. Nothing that instructs or builds is kept: DESIGN 11 step 3 validates a manifest
    # against the surveyed directory, and admits no instruction file and no tooling.
    for finfo in report.files:
        if finfo.path not in manifest.files:
            continue
        entry = manifest.files[finfo.path]
        if entry.role == "exclude":
            continue
        if finfo.is_instruction:
            errors.append(
                f"instruction file '{finfo.path}' must have role 'exclude', got '{entry.role}'"
            )
        elif finfo.is_tooling:
            errors.append(f"tooling '{finfo.path}' must have role 'exclude', got '{entry.role}'")

    # 3. Relations fit roles, point inside the collection, and follow authority rules
    for fpath, entry in manifest.files.items():
        if fpath not in surveyed_paths:
            continue

        has_derived_from = "derived-from" in entry.relations
        has_errata_for = "errata-for" in entry.relations

        if entry.role == "derived" and not has_derived_from:
            errors.append(f"file '{fpath}' with role 'derived' requires 'derived-from' relation")
        if has_derived_from and entry.role != "derived":
            errors.append(
                f"file '{fpath}' has relation 'derived-from' but role is '{entry.role}' "
                "(only allowed on 'derived')"
            )
        if has_errata_for and entry.role != "errata":
            errors.append(
                f"file '{fpath}' has relation 'errata-for' but role is '{entry.role}' "
                "(only allowed on 'errata')"
            )

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
    """CLI entry point for aft library manifest / validate."""
    out = stream if stream is not None else sys.stdout
    mpath = Path(manifest_str).expanduser()
    override = Path(path_override).expanduser() if path_override else None

    manifest = validate(mpath, override)
    file_count = len(manifest.files)
    excluded = sum(1 for f in manifest.files.values() if f.role == "exclude")
    relations_count = sum(len(f.relations) for f in manifest.files.values())

    out.write(
        f"manifest '{manifest.name}' is valid: {file_count} files accounted for "
        f"({excluded} excluded, {relations_count} relations)\n"
    )
    return 0
