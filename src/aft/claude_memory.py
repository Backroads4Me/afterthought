"""Claude Code's automatic memory, imported as digests so the normal rules decide its fate.

Design 15's M6 row says what this is for: afterthought becomes the only memory the agents
use, and the memory Claude Code wrote for itself has to arrive as evidence-backed memory
rather than as a pile of text pasted onto pages. So the command writes digests, not facts.
Each memory file becomes one digest whose lines are ``[inferred]`` facts quoted from that
file, which is the same shape a library proposal arrives in (design 6, M7): the lines then
meet R1 like any other claim, and R3's recurrence gate holds an ``[inferred]`` line in
Candidates until three signals across two primary sessions corroborate it (design 9). An
imported memory file is therefore *not* promoted by being imported: one file is one signal,
so imported lines wait as candidates, and the ones that were actually right get confirmed by
the sessions that follow. This module decides nothing about which lines are true.

Where a quote can be verified at all is the design gap this command had to settle. A
``MEMORY.md`` line sits in no archive: ``aft://session/`` evidence means a conversation the
capture sweep stored, and ``aft://edit/`` means a vault commit. So the quote is recorded the
way F5 records a quote from a library file that was deliberately not retained - through
``propose.record_verified_quote``, keyed under an ``aft://library/claude-memory/...``
resource - which ``apply._lookup_evidence`` already consults, unchanged. The alternative of
storing each line as an archive row under a made-up session id was rejected: the archive is
filtered conversation (design 8), and ``yours`` - which is what grants a line R2 standing as
something the person said - is derived from that row's role, so importing text as somebody's
own words would hand it an authority it does not have (design 11: nothing imported is
``[stated]``).
"""

from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from aft import __version__, index, pagewrite, vault
from aft.config import Config
from aft.library.propose import record_verified_quote
from aft.registry import Project, Registry, split_key

# Claude Code keeps one directory per project, named by that project's path with its
# separators replaced, and writes automatic memory into a ``memory`` directory inside it.
CLAUDE_PROJECTS = Path(".claude") / "projects"
MEMORY_DIR = "memory"

# The resource root these quotes are recorded under. It lives in the library namespace
# because that is the one resource form whose verification path reads a record rather than
# an archive row (apply._lookup_evidence), and a collection named `claude-memory` would
# collide with it, so this command reserves that name.
RESOURCE_ROOT = "claude-memory"

# A fact line is one line, and the writer refuses what it cannot read back, so a memory line
# is dropped with a reason rather than corrupting a page: struck text, a footnote that would
# name a source the page does not hold, and anything else the grammar has no room for.
STRUCK = "~~"
FOOTNOTE = "[^"

# The marker a memory line carries because it is markdown rather than because it is a claim.
LIST_MARKER = re.compile(r"^(?:[-*+]|\d+[.)])\s+")


class ImportMemoryError(Exception):
    """The memory asked for is not there, or the project named is not registered."""


@dataclass(frozen=True)
class Candidate:
    """One memory file, read and split into the lines that would become facts.

    ``lines`` holds ``(statement, quote)`` pairs, where the quote is the file's own line and
    the statement is that line without its list marker: the entry says the quote is the line
    itself, and the marker is how markdown marks a list item rather than part of the claim.
    """

    project: Project
    path: Path
    resource: str
    content_hash: str
    body_date: str
    lines: tuple[tuple[str, str], ...]
    dropped: tuple[tuple[str, str], ...]

    @property
    def digest_rel(self) -> str:
        """Where its digest goes, stable for unchanged content so a rerun adds nothing."""
        year, month = self.body_date[:4], self.body_date[5:7]
        name = f"{self.body_date}-import-{self.path.stem}-{self.content_hash[:8]}.md"
        return f"digests/{self.project.slug}/{year}/{month}/{name}"


def memory_home(source: str | Path | None = None) -> Path:
    """The directory holding one project's memory per Claude Code's own layout."""
    root = Path(source).expanduser() if source is not None else Path.home() / CLAUDE_PROJECTS
    if not root.is_dir():
        raise ImportMemoryError(f"no Claude Code memory at {root}; nothing to import")
    return root


def encoding_of(path: Path) -> str:
    """How Claude Code names a project's directory: its path with separators replaced.

    The replacement loses information - a project directory containing a dash and one
    containing a separator name alike - so this encodes the registered path and looks for
    that name, rather than decoding a directory name and guessing which path it was.
    """
    return str(path).replace("/", "-")


def plans(
    config: Config, source: str | Path | None = None, project: str | None = None
) -> tuple[list[Candidate], list[str]]:
    """What would be imported, and the memory directories that name no registered project.

    Projects are matched by encoding each registered path the way Claude Code names a
    directory, because that direction is exact while decoding is not. A directory that
    matches nothing is reported instead of guessed at: registering a project is `aft key`'s
    and `aft project add`'s job, and an import that minted projects from a directory name
    would invent memory's owners.
    """
    root = memory_home(source)
    registry = Registry.load(config.registry_path)
    wanted = None
    if project is not None:
        found = registry.by_slug(project)
        if found is None:
            slugs = ", ".join(sorted(p.slug for p in registry.projects)) or "no projects"
            raise ImportMemoryError(f"no project {project!r} in the registry; it has {slugs}")
        wanted = found.slug

    candidates: list[Candidate] = []
    matched: set[str] = set()
    for registered in registry.projects:
        if wanted is not None and registered.slug != wanted:
            continue
        for key in registered.paths:
            name = encoding_of(split_key(key)[1])
            memory_dir = root / name / MEMORY_DIR
            if not memory_dir.is_dir():
                continue
            matched.add(name)
            for path in sorted(memory_dir.glob("*.md")):
                candidate = read_file(registered, path)
                if candidate.lines:
                    candidates.append(candidate)
    unclaimed = sorted(
        entry.name
        for entry in root.iterdir()
        if entry.name not in matched and any((entry / MEMORY_DIR).glob("*.md"))
    )
    return candidates, unclaimed


def read_file(project: Project, path: Path) -> Candidate:
    """One memory file as lines that could become facts, and the lines that cannot."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    lines: list[tuple[str, str]] = []
    dropped: list[tuple[str, str]] = []
    for line in _body(raw).splitlines():
        quote = line.strip()
        if not quote:
            continue
        if quote.startswith("#"):
            # A heading says what the lines under it are about, which is a section's job in
            # a memory page and not something to remember as a claim of its own.
            dropped.append((quote, "a heading names a section rather than making a claim"))
            continue
        statement = LIST_MARKER.sub("", quote).removeprefix(">").strip()
        if not statement:
            dropped.append((quote, "the line held no text once its marker was removed"))
            continue
        if STRUCK in statement:
            dropped.append((quote, "struck text: a fact line cannot carry ~~, which means struck"))
            continue
        if FOOTNOTE in statement:
            dropped.append((quote, "the line cites a footnote this digest would not hold"))
            continue
        lines.append((statement, quote))
    return Candidate(
        project=project,
        path=path,
        resource=f"aft://library/{RESOURCE_ROOT}/{project.slug}/{path.name}",
        content_hash=content_hash,
        body_date=_date_of(path),
        lines=tuple(lines),
        dropped=tuple(dropped),
    )


def _body(raw: str) -> str:
    """The file's text with its frontmatter removed, which Claude Code writes first."""
    if not raw.startswith("---"):
        return raw
    end = raw.find("\n---", 3)
    if end == -1:
        return raw
    return raw[end + 4 :]


def _date_of(path: Path) -> str:
    """The day the file was last written, which is the date its lines were evidenced.

    A fact line's date is the last date the fact was seen, and the memory file is what says
    so here. A clock-skewed future date is clamped to today: a digest written into a future
    month would sit below the ones the dream reads in order.
    """
    written = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    now = datetime.now(UTC)
    if written > now:
        written = now
    return written.strftime("%Y-%m-%d")


def render(candidate: Candidate, stamp: str) -> tuple[str, str]:
    """The digest page for one memory file, and the path it belongs at.

    One source per line rather than one per file: R1 asks that each fact carry a source whose
    quote was verified, and the quote that stands behind a line is that line.
    """
    front = {
        "type": "Session Digest",
        "title": f"Claude memory {candidate.path.stem} ({candidate.project.title})",
        "description": (
            f"Lines imported from Claude Code's {candidate.path.name} for "
            f"{candidate.project.slug}; each is one signal, not a confirmed fact"
        ),
        "generated": {"by": f"aft {__version__}", "at": stamp},
        "sources": [],
        "aft": {
            "project": candidate.project.id,
            "facts": [
                {"statement": statement, "kind": "fact", "source": "inferred", "scope": "project"}
                for statement, _ in candidate.lines
            ],
        },
    }
    if candidate.dropped:
        front["aft"]["dropped"] = [
            {"statement": line, "file": candidate.path.name, "reason": reason}
            for line, reason in candidate.dropped
        ]
    dumped = yaml.safe_dump(front, sort_keys=False, allow_unicode=True, width=100_000)
    doc = pagewrite.PageDoc(f"---\n{dumped}---\n", candidate.digest_rel)
    for statement, quote in candidate.lines:
        source_id = doc.add_source(candidate.resource, title=candidate.path.name, quote=quote)
        doc.add_fact("fact", statement, "inferred", candidate.body_date, [source_id])
    return doc.render(), candidate.digest_rel


def run(
    config: Config,
    source: str | Path | None = None,
    *,
    project: str | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> int:
    """Write one digest per memory file and make each line's quote verifiable.

    Returns 1 when there was nothing to import, which is a fact about the machine rather
    than a broken command: an operator who expected memory to be there needs the run to say
    it was not. A rerun that changed nothing returns 0 and writes nothing.
    """
    candidates, unclaimed = plans(config, source, project)
    for candidate in candidates:
        print(
            f"{candidate.path}: {len(candidate.lines)} lines -> "
            f"{candidate.digest_rel}"
            + (" (already imported)" if (config.vault / candidate.digest_rel).is_file() else "")
        )
    for name in unclaimed:
        print(f"skipped {name}: it names no project in the registry; run `aft project add` first")
    if not candidates:
        print("no Claude Code memory for a registered project", file=sys.stderr)
        return 1
    if dry_run:
        return 0

    moment = now or datetime.now(UTC)
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    written: list[str] = []
    with vault.locked(config):
        for candidate in candidates:
            # The record is written whether or not this digest is new. Verification asks one
            # quote at a time against it, so rerunning the import has to be able to rebuild it
            # after the home directory is lost; otherwise memory would sit in the vault with
            # its proof gone and every later dream reporting its lines as unverified.
            for _, quote in candidate.lines:
                record_verified_quote(
                    config.home, candidate.resource, quote, candidate.content_hash, stamp
                )
            rel = candidate.digest_rel
            if (config.vault / rel).is_file():
                continue
            text, _ = render(candidate, stamp)
            (config.vault / rel).parent.mkdir(parents=True, exist_ok=True)
            (config.vault / rel).write_text(text, encoding="utf-8")
            written.append(rel)
        if written:
            vault.commit(config.vault, written, "aft: imported Claude Code memory")
            index.update(config)
    if not written:
        print("nothing changed: every memory file is already imported")
    else:
        print(f"imported {len(written)} memory files as digests")
    return 0
