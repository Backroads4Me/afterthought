"""Check the vault against OKF v0.2 and afterthought's page rules."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from aft import page as pagemod
from aft.registry import Registry, RegistryError

FACT_DIRS = pagemod.FACT_DIRS
# Directories that are not pages at all.
SKIP_DIRS = {".git", ".obsidian", "library"}
RESERVED = {"index.md", "log.md"}
STATUSES = {"draft", "stable", "deprecated"}
_FOOTNOTE = re.compile(r"\[\^([A-Za-z0-9_-]+)\]")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def lint_vault(vault: Path) -> list[Finding]:
    findings: list[Finding] = []
    registry: Registry | None = None
    try:
        registry = Registry.load(vault / "registry.yaml")
    except RegistryError as exc:
        findings.append(Finding("registry.yaml", 1, str(exc)))
    for rel in vault_pages(vault):
        findings.extend(lint_file(vault, rel, registry))
    findings.extend(lint_library(vault))
    return findings


def vault_pages(vault: Path) -> list[Path]:
    """Every page `lint_vault` checks, relative to the vault, in the order it checks them."""
    pages = []
    for path in sorted(vault.rglob("*.md")):
        rel = path.relative_to(vault)
        if SKIP_DIRS.intersection(rel.parts[:-1]) or rel.parts[0] in SKIP_DIRS:
            continue
        pages.append(rel)
    return pages


def lint_pages(
    vault: Path, rel_paths: Iterable[Path | str], registry: Registry | None = None
) -> list[Finding]:
    """Lint only the specified page paths relative to vault."""
    findings: list[Finding] = []
    if registry is None:
        try:
            registry = Registry.load(vault / "registry.yaml")
        except RegistryError as exc:
            findings.append(Finding("registry.yaml", 1, str(exc)))
    for p in sorted(rel_paths):
        rel = Path(p)
        if SKIP_DIRS.intersection(rel.parts[:-1]) or rel.parts[0] in SKIP_DIRS:
            continue
        full_path = vault / rel
        if full_path.suffix == ".md" and full_path.is_file():
            findings.extend(lint_file(vault, rel, registry))
    return findings


def lint_library(vault: Path) -> list[Finding]:
    """Check every library file against its recorded hash in references/<name>/collection.md."""
    findings: list[Finding] = []
    refs_dir = vault / "references"
    if not refs_dir.is_dir():
        return findings

    for col_dir in sorted(refs_dir.iterdir()):
        if not col_dir.is_dir():
            continue
        col_file = col_dir / "collection.md"
        if not col_file.is_file():
            continue
        try:
            col_text = col_file.read_text(encoding="utf-8")
        except OSError:
            continue
        frontmatter, _, _, _ = pagemod.split_frontmatter(col_text)
        aft = (frontmatter or {}).get("aft")
        if not isinstance(aft, dict):
            continue
        col_name = aft.get("collection") or col_dir.name
        recorded_files = aft.get("files")
        if not isinstance(recorded_files, dict):
            continue

        lib_dir = vault / "library" / col_name
        if not lib_dir.is_dir():
            findings.append(
                Finding(f"library/{col_name}", 1, "library collection directory is missing")
            )
            continue

        for rel_path, expected_hash in recorded_files.items():
            fpath = lib_dir / rel_path
            if not fpath.exists():
                findings.append(
                    Finding(
                        f"library/{col_name}/{rel_path}",
                        1,
                        "library file missing; restore it from git or re-import the collection",
                    )
                )
            elif fpath.is_file():
                try:
                    actual_hash = hashlib.sha256(fpath.read_bytes()).hexdigest()
                    if actual_hash != expected_hash:
                        findings.append(
                            Finding(
                                f"library/{col_name}/{rel_path}",
                                1,
                                f"library file hash mismatch: expected {expected_hash}, "
                                f"got {actual_hash}",
                            )
                        )
                except OSError as exc:
                    findings.append(
                        Finding(f"library/{col_name}/{rel_path}", 1, f"cannot read file: {exc}")
                    )

        for f in sorted(lib_dir.rglob("*")):
            if f.is_file() and not f.name.startswith("."):
                rel = f.relative_to(lib_dir).as_posix()
                if rel not in recorded_files:
                    findings.append(
                        Finding(
                            f"library/{col_name}/{rel}",
                            1,
                            f"unrecorded file in library collection: '{rel}'",
                        )
                    )
    return findings


def lint_file(vault: Path, rel: Path, registry: Registry | None) -> list[Finding]:
    text = (vault / rel).read_text()
    name = str(rel)
    out: list[Finding] = []

    def add(line: int, message: str) -> None:
        out.append(Finding(name, line, message))

    frontmatter, body, body_start, error = pagemod.split_frontmatter(text)
    if error:
        add(1, error)
        return out

    if rel.name == "index.md":
        if rel == Path("index.md"):
            if frontmatter != {"okf_version": "0.2"}:
                add(1, 'the root index.md must have exactly okf_version: "0.2" as frontmatter')
        elif frontmatter is not None:
            add(1, "only the root index.md may have frontmatter")
        return out
    if rel.name == "log.md":
        return out

    if frontmatter is None:
        add(1, "missing frontmatter")
        return out
    _check_okf(frontmatter, add)

    if rel.parts[0] in FACT_DIRS:
        page = pagemod.parse(text, rel)
        for line, message in page.errors:
            add(line, message)
        _check_evidence(page, frontmatter, body, add)
        _check_scope(rel, frontmatter, registry, add)
        if pagemod.is_recent(rel):
            _check_short_term(page, body, body_start, add)
    return out


def _check_short_term(page: pagemod.Page, body: str, body_start: int, add) -> None:
    """A project's short-term page holds no candidates and only [stated] or [observed]
    lines (DESIGN 6): a conclusion or an idea about current work belongs in a handoff."""
    for number, text in enumerate(body.split("\n"), start=body_start):
        if text.strip() == f"## {pagemod.CANDIDATES}":
            add(number, "short-term memory has no Candidates section")
    for fact in page.facts:
        if fact.section != pagemod.CANDIDATES and fact.source not in pagemod.SHORT_TERM_SOURCES:
            add(
                fact.line,
                f"short-term memory holds [stated] and [observed] lines, not [{fact.source}]",
            )


def _check_okf(fm: dict, add) -> None:
    kind = fm.get("type")
    if not isinstance(kind, str) or not kind.strip():
        add(1, "frontmatter needs a non-empty type")
    for key in ("title", "description"):
        if key in fm and not isinstance(fm[key], str):
            add(1, f"{key} must be a string")
    if "tags" in fm and not (
        isinstance(fm["tags"], list) and all(isinstance(t, str) for t in fm["tags"])
    ):
        add(1, "tags must be a list of strings")
    if "status" in fm and fm["status"] not in STATUSES:
        add(1, f"status must be one of {', '.join(sorted(STATUSES))}")
    if "stale_after" in fm and not _is_timestamp(fm["stale_after"]):
        add(1, "stale_after must be an ISO 8601 date or time")
    generated = fm.get("generated")
    if generated is not None and not (
        isinstance(generated, dict) and generated.get("by") and _is_timestamp(generated.get("at"))
    ):
        add(1, "generated must have by and an ISO 8601 at")
    sources = fm.get("sources")
    if sources is None:
        return
    if not isinstance(sources, list):
        add(1, "sources must be a list")
        return
    seen: set[str] = set()
    for entry in sources:
        if not isinstance(entry, dict) or not isinstance(entry.get("resource"), str):
            add(1, "each source needs a resource")
            continue
        sid = entry.get("id")
        if sid is not None:
            if str(sid) in seen:
                add(1, f"duplicate source id {sid}")
            seen.add(str(sid))


def _check_evidence(page: pagemod.Page, fm: dict, body: str, add) -> None:
    """Check the sources every footnote on the page cites.

    The body is read as written, not through the facts the parser accepted, so a line the
    parser refused and a note with no fact above it still have their citations checked: a
    `[^id]` anywhere on a fact page has to name a source (design section 6).
    """
    sources = {
        str(s["id"]): s
        for s in fm.get("sources") or []
        if isinstance(s, dict) and s.get("id") is not None
    }
    for number, row in enumerate(body.split("\n"), start=page.body_start):
        for ref in dict.fromkeys(_FOOTNOTE.findall(row)):
            source = sources.get(ref)
            if source is None:
                add(number, f"footnote [^{ref}] is not defined in sources")
            elif not isinstance(source.get("quote"), str) or not source["quote"].strip():
                add(number, f"source {ref} has no quote")
    aft = fm.get("aft") or {}
    anchors = aft.get("anchors") if isinstance(aft, dict) else None
    if anchors is None:
        return
    if not isinstance(anchors, dict):
        add(1, "aft.anchors must map a source id to an anchor")
        return
    for sid, anchor in anchors.items():
        if str(sid) not in sources:
            add(1, f"anchor for unknown source {sid}")
        if not (
            isinstance(anchor, dict)
            and isinstance(anchor.get("path"), str)
            and isinstance(anchor.get("all_of"), list)
            and anchor["all_of"]
            and all(isinstance(w, str) and w for w in anchor["all_of"])
            and isinstance(anchor.get("commit"), str)
        ):
            add(1, f"anchor {sid} needs path, a non-empty all_of list, and commit")


def _check_scope(rel: Path, fm: dict, registry: Registry | None, add) -> None:
    aft = fm.get("aft") or {}
    if not isinstance(aft, dict):
        add(1, "aft must be a mapping")
        return
    top = rel.parts[0]
    if top == "projects":
        if len(rel.parts) < 3:
            add(1, "project pages belong in projects/<slug>/")
            return
        if registry is None:
            return
        project = registry.by_slug(rel.parts[1])
        if project is None:
            add(1, f"projects/{rel.parts[1]} is not a registered project")
        elif aft.get("project") != project.id:
            add(1, f"aft.project must be {project.id}, the id of {project.slug}")
    elif top == "global" and "project" in aft:
        add(1, "global pages cannot belong to a project")
    elif top == "references" and registry is not None:
        for project_id in aft.get("projects") or []:
            if registry.by_id(str(project_id)) is None:
                add(1, f"aft.projects names an unknown project {project_id}")


def _is_timestamp(value) -> bool:
    if isinstance(value, date):  # YAML parses unquoted dates and times itself
        return True
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True
