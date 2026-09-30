"""Propose candidate facts from notes and excluded instruction files (BUILD-PLAN M7.6, DESIGN 11).

The agent proposes facts with quotes mined from:
- Document files with role `original`, `derived`, or `errata` (verified against `library/<name>/`).
- Files with role `notes` in the collection manifest (verified against `library/<name>/`).
- Excluded instruction files (e.g. `AGENTS.md`, `CLAUDE.md`, `.cursorrules`, etc.).

Each proposed fact is quote-checked against its file and enters the dream as a
candidate digest under `digests/<project>/<yyyy>/<mm>/<date>-library-<collection>.md`.
A library quote is never yours; all proposed facts must enter as candidates (`inferred`
or `suggested`). An excluded file is not kept in the vault, so its verified quotes are
recorded in `~/.afterthought/library/verified_quotes.json` so they remain verifiable
both in candidate digests and on curated pages.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

import yaml

from aft import evidence, pagewrite, redact, vault
from aft.config import Config, load
from aft.library import importing
from aft.library import manifest as manifest_mod
from aft.library.manifest import Manifest, UniqueKeyLoader
from aft.library.survey import file_sha256, is_instruction_file
from aft.registry import Registry

try:
    from aft import __version__
except ImportError:
    __version__ = "0.1.0"

VERIFIED_QUOTES_FILE = "verified_quotes.json"
DOCUMENT_ROLES = frozenset({"original", "derived", "errata"})


class ProposeError(Exception):
    """Raised when fact proposal fails or proposals file is invalid."""


class VerifiedRecordError(ProposeError):
    """The verified-quote record cannot be read, so it is never rewritten over or skipped."""


def _read_verified_quotes(vf_path: Path) -> dict:
    """The verified-quote record as written, or the reason it cannot be trusted.

    This record is the only proof that a quote from a file the person chose not to keep was
    checked against that file at the hash it had then. A record that does not parse is refused
    where it is about to be written or redacted: read as empty it would be saved back holding
    nothing but this run's quotes, and every other imported quote would stop verifying with no
    copy left to check against (M7 review 16).
    """
    try:
        text = vf_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VerifiedRecordError(
            f"library/{VERIFIED_QUOTES_FILE} cannot be read: {exc}; repair or remove it first"
        ) from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise VerifiedRecordError(
            f"library/{VERIFIED_QUOTES_FILE} does not parse ({exc}); repair or remove it first. "
            "It is the only proof that a quote from a file kept out of the vault was checked"
        ) from exc
    if not isinstance(data, dict):
        raise VerifiedRecordError(
            f"library/{VERIFIED_QUOTES_FILE} holds a {type(data).__name__}, not the map of "
            f"resource to verified quotes that 'aft library propose' writes; repair or remove it"
        )
    return data


@dataclass(frozen=True)
class ProposedFact:
    """One fact proposed by an agent from a library file."""

    statement: str
    quote: str
    file: str
    kind: str = "fact"
    source: str = "inferred"
    scope: str = "project"
    project: str | None = None


@dataclass(frozen=True)
class FactVerificationResult:
    """The outcome of verifying one proposed fact against its source file."""

    fact: ProposedFact
    verified: bool
    reason: str = ""
    original_retained: bool = True


@dataclass(frozen=True)
class ProposeResult:
    """Aggregated results of a propose run."""

    collection: str
    total_proposed: int
    verified_facts: list[FactVerificationResult]
    dropped_facts: list[FactVerificationResult]
    digests_written: list[str]
    report_path: str | None = None
    committed: bool = False


def record_verified_quote(
    home: Path,
    resource: str,
    quote: str,
    file_hash: str,
    timestamp: str,
) -> None:
    """Record a verified quote from an excluded library file in ~/.afterthought/library/.

    Written only by `aft library propose` to provide an untamperable proof of verification
    that survives into curated memory without retaining the excluded source file.
    """
    lib_dir = home / "library"
    lib_dir.mkdir(parents=True, exist_ok=True)
    vf_path = lib_dir / VERIFIED_QUOTES_FILE
    data: dict[str, list[dict]] = {}
    if vf_path.is_file():
        data = _read_verified_quotes(vf_path)

    entries = data.setdefault(resource, [])
    q_norm = evidence._spaced(quote)
    if not any(
        isinstance(e, dict) and e.get("quote_norm") == q_norm and e.get("file_hash") == file_hash
        for e in entries
    ):
        entries.append(
            {
                "quote": quote,
                "quote_norm": q_norm,
                "file_hash": file_hash,
                "verified_at": timestamp,
            }
        )
    vf_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def is_verified_import_quote(home: Path, resource: str, quote: str) -> bool:
    """Whether a quote from a file the person kept out of the vault was verified when proposed.

    A record that does not parse verifies nothing, and is left as it is: the commands that would
    write to it or redact it refuse and say so rather than replacing it (M7 review 16).
    """
    vf_path = home / "library" / VERIFIED_QUOTES_FILE
    if not vf_path.is_file():
        return False
    try:
        data = json.loads(vf_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
    except Exception:
        return False

    entries = data.get(resource)
    if not isinstance(entries, list):
        return False

    q_norm = evidence._spaced(quote)
    for e in entries:
        if isinstance(e, dict) and e.get("quote_norm") == q_norm:
            return True
    return False


def redact_verified_quotes(home: Path, pattern: re.Pattern[str]) -> int:
    """Replace one pattern's matches in the verified-quote record, and count the entries.

    ``aft forget pattern`` redacts the vault's pages, and an imported quote is checked against
    the page that cites it, so a record still holding the words as first written would stop
    matching the redacted quote it stands for and the line resting on it would read as
    unverifiable. Both sides take the same replacement: the record proves a quote was checked
    against a file, and says nothing about what the words were (DESIGN 12).
    """
    vf_path = home / "library" / VERIFIED_QUOTES_FILE
    if not vf_path.is_file():
        return 0
    data = _read_verified_quotes(vf_path)

    changed = 0
    for entries in data.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("quote"), str):
                continue
            redacted = pattern.sub(redact.REDACTED, entry["quote"])
            if redacted == entry["quote"]:
                continue
            entry["quote"] = redacted
            entry["quote_norm"] = evidence._spaced(redacted)
            changed += 1
    if changed:
        vf_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return changed


def load_proposals(source: str | Path | TextIO | Sequence[dict]) -> list[ProposedFact]:
    """Parse proposed facts from a file path, stdin ('-'), or raw sequence."""
    if isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
        raw_list = source
    elif isinstance(source, (str, Path)):
        if str(source) == "-":
            raw_text = sys.stdin.read()
        else:
            p = Path(source)
            if not p.is_file():
                raise ProposeError(f"proposals file not found: {source!r}")
            try:
                raw_text = p.read_text(encoding="utf-8")
            except OSError as exc:
                raise ProposeError(f"cannot read proposals file {source!r}: {exc}") from exc
        try:
            data = yaml.load(raw_text, Loader=UniqueKeyLoader)
        except Exception as exc:
            raise ProposeError(f"malformed YAML in proposals file: {exc}") from exc
        if isinstance(data, list):
            raw_list = data
        elif isinstance(data, dict):
            if "facts" in data and isinstance(data["facts"], list):
                raw_list = data["facts"]
            elif "proposals" in data and isinstance(data["proposals"], list):
                raw_list = data["proposals"]
            else:
                raise ProposeError("proposals YAML mapping must contain a 'facts' list")
        elif data is None:
            raise ProposeError("proposals file is empty")
        else:
            msg = "proposals YAML must be a list of facts or a mapping with a 'facts' list"
            raise ProposeError(msg)
    elif hasattr(source, "read"):
        try:
            raw_text = source.read()
            data = yaml.load(raw_text, Loader=UniqueKeyLoader)
        except Exception as exc:
            raise ProposeError(f"malformed YAML in proposals stream: {exc}") from exc
        if isinstance(data, list):
            raw_list = data
        elif isinstance(data, dict):
            raw_list = data.get("facts") or data.get("proposals") or []
        elif data is None:
            raise ProposeError("proposals stream is empty")
        else:
            msg = "proposals YAML must be a list of facts or a mapping with a 'facts' list"
            raise ProposeError(msg)
    else:
        raise ProposeError(f"unsupported proposals source: {type(source).__name__}")

    proposals: list[ProposedFact] = []
    for idx, item in enumerate(raw_list, start=1):
        if not isinstance(item, dict):
            raise ProposeError(f"proposed fact #{idx} must be a mapping, got {type(item).__name__}")
        statement = item.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            raise ProposeError(f"proposed fact #{idx} must have a non-empty 'statement'")
        quote = item.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            msg = f"proposed fact #{idx} ({statement!r}) must have a non-empty 'quote'"
            raise ProposeError(msg)
        f_path = item.get("file") or item.get("path")
        if not isinstance(f_path, str) or not f_path.strip():
            msg = f"proposed fact #{idx} ({statement!r}) must have a non-empty 'file'"
            raise ProposeError(msg)

        kind = str(item.get("kind", "fact")).lower().strip()
        source_tag = str(item.get("source", "inferred")).lower().strip()
        scope = str(item.get("scope", "project")).lower().strip()
        proj = item.get("project")
        if proj is not None:
            proj = str(proj).strip()

        # Normalize relative path representation
        norm_path = Path(f_path.strip()).as_posix()
        if norm_path.startswith("./"):
            norm_path = norm_path[2:]

        proposals.append(
            ProposedFact(
                statement=statement.strip(),
                quote=quote.strip(),
                file=norm_path,
                kind=kind,
                source=source_tag,
                scope=scope,
                project=proj,
            )
        )
    return proposals


def verify_fact(
    fact: ProposedFact,
    manifest: Manifest,
    collection_root: Path,
    vault_path: Path | None = None,
    home: Path | None = None,
    record: importing.ImportRecord | None = None,
) -> FactVerificationResult:
    """Verify one proposed fact against the collection as it was imported.

    Authority and role come from the import record, never from the manifest this command was
    handed: a manifest says what someone would like the files to be, and the record says what
    the person reviewed and imported. The vault copy is compared with the hash that record holds
    before any quote is looked for, and the quote is looked for in the extracted text, the same
    text the index, the reference pages and the dream read (DESIGN 11).
    """
    # 1. Statement validation
    stmt = fact.statement.strip()
    if not stmt:
        return FactVerificationResult(fact=fact, verified=False, reason="a fact needs a statement")
    if "\n" in stmt or "\r" in stmt:
        return FactVerificationResult(
            fact=fact, verified=False, reason="a fact statement is one line"
        )
    if "~~" in stmt:
        return FactVerificationResult(
            fact=fact, verified=False, reason="a fact statement cannot contain ~~"
        )

    # 2. Source tag validation (DESIGN 11 step 5, BUILD-PLAN F5)
    if fact.source == "stated":
        return FactVerificationResult(
            fact=fact,
            verified=False,
            reason="stated facts cannot be proposed from library files",
        )
    if fact.source not in ("inferred", "suggested", "observed", ""):
        return FactVerificationResult(
            fact=fact,
            verified=False,
            reason=f"unsupported source tag {fact.source!r} for library facts",
        )

    # 3. Kind validation
    if fact.kind not in pagewrite.SECTION_FOR_KIND:
        valid_kinds = ", ".join(sorted(pagewrite.SECTION_FOR_KIND.keys()))
        return FactVerificationResult(
            fact=fact,
            verified=False,
            reason=f"unknown fact kind {fact.kind!r}; expected one of: {valid_kinds}",
        )

    # 4. Scope validation: library facts are project-scoped
    if fact.scope != "project":
        return FactVerificationResult(
            fact=fact,
            verified=False,
            reason=(
                f"library facts cannot have scope {fact.scope!r}; "
                "library facts must be project-scoped (scope: 'project')"
            ),
        )

    # 5. Project validation: fact.project must be in manifest.projects
    if fact.project is not None and manifest.projects and fact.project not in manifest.projects:
        valid_projects = ", ".join(repr(p) for p in manifest.projects)
        return FactVerificationResult(
            fact=fact,
            verified=False,
            reason=(
                f"project {fact.project!r} is not in collection manifest projects "
                f"({valid_projects})"
            ),
        )

    # 6. Path containment check
    target_p = Path(fact.file)
    if target_p.is_absolute():
        return FactVerificationResult(
            fact=fact,
            verified=False,
            reason=f"file {fact.file!r} is absolute; must be relative to collection root",
        )

    resolved_col = collection_root.resolve()
    target_file = (collection_root / target_p).resolve()
    if not target_file.is_relative_to(resolved_col):
        return FactVerificationResult(
            fact=fact,
            verified=False,
            reason=f"file {fact.file!r} steps outside collection root",
        )

    # 7. Manifest containment
    if fact.file not in manifest.files:
        return FactVerificationResult(
            fact=fact,
            verified=False,
            reason=f"file {fact.file!r} is not in collection manifest",
        )

    entry = manifest.files[fact.file]
    claimed_role = entry.role
    is_inst = is_instruction_file(Path(fact.file))

    # 8a. What the file is, as the import recorded it. The manifest this command is handed is
    # not evidence of anything: importing again is what changes a role, and the person reviews
    # the record the import leaves (DESIGN 11 step 2).
    if record is None:
        return FactVerificationResult(
            fact=fact,
            verified=False,
            reason=(
                f"collection {manifest.name!r} left no import record under "
                f"references/{manifest.name}/: run 'aft library import' before proposing facts"
            ),
        )

    if record.hashes and not record.roles:
        return FactVerificationResult(
            fact=fact,
            verified=False,
            reason=(
                f"the import record under references/{manifest.name}/collection.md carries no "
                "file roles, which is a collection imported before an import recorded them: run "
                "'aft library import' again on this manifest"
            ),
        )

    if manifest.authority != record.authority:
        return FactVerificationResult(
            fact=fact,
            verified=False,
            reason=(
                f"the manifest says this collection is {manifest.authority!r}; the import "
                f"record says {record.authority!r}. Authority is set by the import, not propose"
            ),
        )
    recorded_role = record.roles.get(fact.file)
    if recorded_role is None:
        # Nothing was retained behind this file, so only an instruction file the person
        # excluded can carry a fact, and then as evidence that is not kept.
        if claimed_role != "exclude":
            return FactVerificationResult(
                fact=fact,
                verified=False,
                reason=(
                    f"file {fact.file!r} is not in the import record of collection "
                    f"{manifest.name!r}, which imported it as nothing"
                ),
            )
        role = "exclude"
    elif claimed_role != recorded_role:
        return FactVerificationResult(
            fact=fact,
            verified=False,
            reason=(
                f"the manifest says {fact.file!r} is {claimed_role!r}; the import record says "
                f"{recorded_role!r}. Import the collection again if the roles changed"
            ),
        )
    else:
        role = recorded_role

    # 8b. Role check: documents, notes, and excluded instruction files (F5)
    if role in DOCUMENT_ROLES or role == "notes":
        original_retained = True
        # Non-excluded files must be checked against the imported copy in the vault
        if vault_path is None:
            target_file = (collection_root / target_p).resolve()
        else:
            lib_root = (vault_path / "library" / manifest.name).resolve()
            lib_file = (lib_root / fact.file).resolve()
            if not (lib_file.is_relative_to(lib_root) and lib_file.is_file()):
                return FactVerificationResult(
                    fact=fact,
                    verified=False,
                    reason=(
                        f"collection has not been imported yet: "
                        f"'library/{manifest.name}/{fact.file}' not found in vault; "
                        "run 'aft library import' before proposing facts"
                    ),
                )
            target_file = lib_file
            # A quote proves what a document said, so it is looked for in the copy the import
            # left and no other: a sentence written into that copy afterwards would verify a
            # fact that no document holds and no person reviewed.
            recorded_hash = record.hashes.get(fact.file)
            if recorded_hash is None:
                return FactVerificationResult(
                    fact=fact,
                    verified=False,
                    reason=(
                        f"the import record of collection {manifest.name!r} holds no hash for "
                        f"{fact.file!r}; import the collection again"
                    ),
                )
            if file_sha256(lib_file) != recorded_hash:
                return FactVerificationResult(
                    fact=fact,
                    verified=False,
                    reason=(
                        f"'library/{manifest.name}/{fact.file}' no longer matches the hash the "
                        "import recorded: the copy in the vault was changed after the import"
                    ),
                )
    elif role == "exclude":
        if is_inst:
            original_retained = False
            if not target_file.is_file():
                return FactVerificationResult(
                    fact=fact,
                    verified=False,
                    reason=f"instruction file {fact.file!r} not found on disk",
                )
        else:
            return FactVerificationResult(
                fact=fact,
                verified=False,
                reason=(
                    f"file {fact.file!r} is excluded as tooling/data, not an instruction file; "
                    "facts may only be proposed from documents, notes, and "
                    "excluded instruction files"
                ),
            )
    else:
        return FactVerificationResult(
            fact=fact,
            verified=False,
            reason=(
                f"file {fact.file!r} has role {role!r}; "
                "facts may only be proposed from documents, notes, and excluded instruction files"
            ),
        )

    quote_norm = evidence._spaced(fact.quote)

    # 9. Authority tagging (BUILD-PLAN F5, DESIGN 11), read from the record: a document from a
    # collection the person called authoritative witnesses its contents, and everything else
    # (a reference collection, notes, an excluded instruction file) enters as a candidate.
    if record.authority == "authoritative" and role in DOCUMENT_ROLES:
        effective_source = "observed"
    else:
        effective_source = "suggested" if fact.source == "suggested" else "inferred"

    verified_fact = replace(fact, source=effective_source)

    # 10. Quote check, against the extracted text and nothing else. The bytes of an HTML or PDF
    # file hold more than the document says, and the index, the reference pages and the dream all
    # carry the extraction, so a quote a document never told anyone stays unverified here too. An
    # excluded instruction file is read without the cache: caching its text would retain what the
    # import declined to keep.
    document_text = importing.extracted_text(target_file, home if original_retained else None)
    if quote_norm in evidence._spaced(document_text):
        return FactVerificationResult(
            fact=verified_fact,
            verified=True,
            original_retained=original_retained,
        )

    return FactVerificationResult(
        fact=fact,
        verified=False,
        reason=f"quote {fact.quote!r} not found in {fact.file!r}",
    )


def _demand_registered(
    manifest: Manifest, proposed: Sequence[ProposedFact], reg: Registry | None
) -> None:
    """Refuse a proposal whose project the registry does not know, before anything is written.

    A candidate digest lands under ``digests/<slug>/``, so a project name is a path as much as it
    is a name: an unregistered slug writes a page outside the vault, and a name that is merely a
    typo gets an id nobody chose, which the dream later reads as a project someone asked for. Both
    the collection's projects and each fact's are checked here, so the run that would have written
    outside the vault writes nothing at all (DESIGN 11).
    """
    named = [slug for slug in manifest.projects if slug]
    named += [fact.project for fact in proposed if fact.project]
    if not named:
        return
    if reg is None:
        raise ProposeError(
            "the vault has no registry, so no project is registered to receive: "
            + ", ".join(repr(slug) for slug in dict.fromkeys(named))
            + "; register it with 'aft project add' first"
        )
    unknown = [
        slug
        for slug in dict.fromkeys(named)
        if reg.by_slug(slug) is None and reg.by_id(slug) is None
    ]
    if unknown:
        raise ProposeError(
            "no such project in the vault: "
            + ", ".join(repr(slug) for slug in unknown)
            + "; register it with 'aft project add' or fix the manifest"
        )


def _demand_recorded_projects(
    manifest: Manifest, reg: Registry | None, record: importing.ImportRecord | None
) -> None:
    """Refuse a manifest naming a project the import did not record, before anything is written.

    The collection's projects are part of what a person approved at import, and a digest lands
    under the slug it names, so a manifest widened afterwards aims the same verified quotes at a
    project nobody approved them for. The registry says a project exists; the import record says
    this collection was imported for it, and both are checked (DESIGN 11 step 2).
    """
    if record is None or not record.projects or not manifest.projects:
        return
    ids: set[str] = set()
    for slug in manifest.projects:
        project = None if reg is None else (reg.by_slug(slug) or reg.by_id(slug))
        ids.add(project.id if project is not None else slug)
    unrecorded = sorted(ids - set(record.projects))
    if unrecorded:
        raise ProposeError(
            f"collection {manifest.name!r} was imported for "
            + ", ".join(sorted(record.projects))
            + "; this manifest also names "
            + ", ".join(repr(name) for name in unrecorded)
            + "; import the collection again for that project, or fix the manifest"
        )


def propose_facts(
    manifest_path: str | Path,
    facts_source: str | Path | TextIO | Sequence[dict],
    path_override: str | None = None,
    config: Config | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> ProposeResult:
    """Validate proposed facts, quote-check against files, and write candidate digests."""
    cfg = config or load()
    m_path = Path(manifest_path)
    p_override = Path(path_override) if path_override is not None else None
    manifest = manifest_mod.validate(m_path, p_override)
    collection_root = manifest.path

    proposed = load_proposals(facts_source)
    if not proposed:
        raise ProposeError("no proposed facts provided")

    reg_path = cfg.vault / "registry.yaml"
    reg = Registry.load(reg_path) if reg_path.is_file() else None
    _demand_registered(manifest, proposed, reg)
    record = importing.read_import_record(cfg.vault, manifest.name)
    _demand_recorded_projects(manifest, reg, record)

    verified_list: list[FactVerificationResult] = []
    dropped_list: list[FactVerificationResult] = []

    for fact in proposed:
        res = verify_fact(fact, manifest, collection_root, cfg.vault, cfg.home, record)
        if res.verified:
            verified_list.append(res)
        else:
            dropped_list.append(res)

    if not verified_list or dry_run:
        return ProposeResult(
            collection=manifest.name,
            total_proposed=len(proposed),
            verified_facts=verified_list,
            dropped_facts=dropped_list,
            digests_written=[],
            report_path=None,
            committed=False,
        )

    # Group verified facts by target project
    by_project: dict[str, list[FactVerificationResult]] = {}
    for vf in verified_list:
        targets = [vf.fact.project] if vf.fact.project else list(manifest.projects)
        if not targets:
            raise ProposeError(f"no target project for proposed fact {vf.fact.statement!r}")
        for t in targets:
            by_project.setdefault(t, []).append(vf)

    now_dt = now or datetime.now(UTC)
    today_str = now_dt.strftime("%Y-%m-%d")
    year_str = now_dt.strftime("%Y")
    month_str = now_dt.strftime("%m")
    stamp_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    written_digests: list[str] = []

    with vault.locked(cfg):
        # Record tamper-proof verification for excluded files in ~/.afterthought/library/
        for vf in verified_list:
            if not vf.original_retained:
                inst_path = collection_root / vf.fact.file
                f_hash = file_sha256(inst_path) if inst_path.is_file() else ""
                record_verified_quote(
                    cfg.home,
                    f"aft://library/{manifest.name}/{vf.fact.file}",
                    vf.fact.quote,
                    f_hash,
                    stamp_iso,
                )

        for raw_slug, v_facts in sorted(by_project.items()):
            # `_demand_registered` refused any name the registry does not know, so the project
            # landing here has a slug to write under and an id to be named by: neither is invented.
            project = reg.by_slug(raw_slug) or reg.by_id(raw_slug)
            proj_id = project.id
            canonical_slug = project.slug

            digests_dir = cfg.vault / "digests" / canonical_slug / year_str / month_str
            digests_dir.mkdir(parents=True, exist_ok=True)

            base_name = f"{today_str}-library-{manifest.name}"
            candidate_file = digests_dir / f"{base_name}.md"
            part = 1
            while candidate_file.exists():
                part += 1
                candidate_file = digests_dir / f"{base_name}-{part}.md"

            rel_digest = f"digests/{canonical_slug}/{year_str}/{month_str}/{candidate_file.name}"

            sources_list: list[dict] = []
            aft_facts_list: list[dict] = []
            for idx, vf in enumerate(v_facts, start=1):
                sid = f"s{idx}"
                sources_list.append(
                    {
                        "id": sid,
                        "resource": f"aft://library/{manifest.name}/{vf.fact.file}",
                        "title": f"{manifest.name}/{vf.fact.file}",
                        "quote": vf.fact.quote,
                    }
                )
                aft_facts_list.append(
                    {
                        "statement": vf.fact.statement,
                        "kind": vf.fact.kind,
                        "source": vf.fact.source,
                        "scope": vf.fact.scope,
                    }
                )

            aft_dict: dict[str, object] = {
                "project": proj_id,
                "facts": aft_facts_list,
            }
            if dropped_list:
                aft_dict["dropped"] = [
                    {
                        "statement": df.fact.statement,
                        "file": df.fact.file,
                        "reason": df.reason,
                    }
                    for df in dropped_list
                ]

            fm = {
                "type": "Session Digest",
                "title": f"Library {manifest.title} session {today_str}",
                "description": f"Proposed facts from {manifest.title} ({manifest.name})",
                "generated": {
                    "by": f"aft {__version__}",
                    "at": stamp_iso,
                },
                "sources": sources_list,
                "aft": aft_dict,
            }

            fm_dump = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=100_000)
            fm_str = f"---\n{fm_dump}---\n\n"

            doc = pagewrite.PageDoc(fm_str, rel_digest)
            for vf, s_entry in zip(v_facts, sources_list, strict=True):
                doc.add_fact(
                    vf.fact.kind,
                    vf.fact.statement,
                    vf.fact.source,
                    today_str,
                    [s_entry["id"]],
                )

            candidate_file.write_text(doc.render(), encoding="utf-8")
            written_digests.append(rel_digest)

        # Write report section
        report_rel = _write_propose_report(
            cfg.vault,
            manifest,
            verified_list,
            dropped_list,
            written_digests,
            now_dt,
        )

        staged = [*written_digests, report_rel]
        committed = vault.commit(
            cfg.vault,
            staged,
            f"aft library: propose candidate facts for {manifest.name!r}",
        )

    return ProposeResult(
        collection=manifest.name,
        total_proposed=len(proposed),
        verified_facts=verified_list,
        dropped_facts=dropped_list,
        digests_written=written_digests,
        report_path=report_rel,
        committed=committed,
    )


def _write_propose_report(
    vault_path: Path,
    manifest: Manifest,
    verified: list[FactVerificationResult],
    dropped: list[FactVerificationResult],
    digests: list[str],
    now: datetime,
) -> str:
    """Append a propose-facts section to reports/<yyyy>/<mm>/<date>.md."""
    date_str = now.strftime("%Y-%m-%d")
    year_str = now.strftime("%Y")
    month_str = now.strftime("%m")
    time_str = now.strftime("%H:%M:%SZ")

    report_dir = vault_path / "reports" / year_str / month_str
    report_dir.mkdir(parents=True, exist_ok=True)
    report_file = report_dir / f"{date_str}.md"

    v_lines = []
    for r in verified:
        ret_info = "retained" if r.original_retained else "not retained"
        v_lines.append(
            f"- `{r.fact.statement}` from `{r.fact.file}` ([{r.fact.source}], {ret_info})"
        )
    v_text = "\n".join(v_lines) if v_lines else "- none"

    d_lines = []
    for r in dropped:
        d_lines.append(f"- `{r.fact.statement}` from `{r.fact.file}`: {r.reason}")
    d_text = "\n".join(d_lines) if d_lines else "- none"

    digest_bullets = "\n".join(f"- `{d}`" for d in digests) if digests else "- none"

    section_text = f"""

## Library facts proposed: {manifest.name} ({time_str})

- Collection: `{manifest.name}`
- Facts proposed: {len(verified) + len(dropped)}
- Facts verified: {len(verified)}
- Facts rejected: {len(dropped)}
- Candidate digests:
{digest_bullets}

### Verified candidate facts

{v_text}

### Rejected candidate facts

{d_text}
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


def run(
    manifest_path: str,
    facts_file: str,
    path_override: str | None = None,
    dry_run: bool = False,
) -> int:
    """CLI runner for `aft library propose <manifest> <facts-file>`."""
    try:
        result = propose_facts(
            manifest_path,
            facts_file,
            path_override=path_override,
            dry_run=dry_run,
        )
    except manifest_mod.ManifestError as exc:
        print(f"aft: {exc}", file=sys.stderr)
        return 2
    except ProposeError as exc:
        print(f"aft: {exc}", file=sys.stderr)
        return 2

    if dry_run:
        print(
            f"dry-run: {len(result.verified_facts)} fact(s) verified, "
            f"{len(result.dropped_facts)} rejected"
        )
        for vf in result.verified_facts:
            print(f"  ✓ {vf.fact.statement} ({vf.fact.file})")
        for df in result.dropped_facts:
            print(f"  ✗ {df.fact.statement} ({df.fact.file}): {df.reason}")
        return 0

    if not result.verified_facts:
        print(
            f"aft: 0 facts verified ({len(result.dropped_facts)} rejected)",
            file=sys.stderr,
        )
        for df in result.dropped_facts:
            print(f"  ✗ {df.fact.statement} ({df.fact.file}): {df.reason}", file=sys.stderr)
        return 1

    print(
        f"verified {len(result.verified_facts)} fact(s) "
        f"({len(result.dropped_facts)} rejected); wrote candidate digest(s):"
    )
    for d in result.digests_written:
        print(f"  - {d}")
    return 0
