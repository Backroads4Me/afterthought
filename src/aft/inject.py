"""The injection block: what an agent is told at session start (design section 10).

It reads files only, so a cold session gets the same block in every agent without a model
call or an index. The parts come out in the design's order and ``inject.max_chars``
trims each part to what is left of the budget: a fact is either in the block or left
out, never cut short, and the closing lines on how to search and read keep room.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from aft import page as pagemod
from aft.config import Config
from aft.keys import Resolution

# The overview's sections, in the order the block shows them.
OVERVIEW_SECTIONS = ("State", "Decisions", "Lessons", "Procedures", "Facts", "References")

# A line's state decides whether the block carries it and how it is marked (design 6).
INJECTED = (pagemod.ACTIVE, pagemod.CONFLICT, pagemod.NEEDS_YOU)
MARKS = {pagemod.CONFLICT: "(disputed)", pagemod.NEEDS_YOU: "(waiting on Ted)"}

HOW_TO = 'Search memory: aft search "<words>". Read a page: aft read <path>.'

# The overview is the block itself, and a directory's index.md is a generated listing, so
# neither is a page the block points at (design 6).
OVERVIEW = "overview.md"
LISTING = "index.md"
COLLECTION = "collection.md"
PREFERENCES = Path("global/preferences.md")

# One part of the block: the heading its lines sit under, "" when it has none, and
# the lines under that heading.
Part = tuple[str, list[str]]


def build(config: Config, resolution: Resolution) -> str:
    """The block for ``resolution``.

    A registered project gets its name and description, the newest digest's handoff, its
    overview's facts, the pages and collections that cover it, and the lines on how to
    search and read. An unregistered or unresolved directory gets only global preferences
    and those lines.
    """
    parts: list[Part] = []
    slug = resolution.slug if resolution.registered else None
    if slug:
        parts += _overview_parts(config, resolution, slug)
    parts += _section_parts(_read_page(config.vault / PREFERENCES), None)
    if slug:
        pages = config.vault / "projects" / slug
        parts.append(_part("Pages", _pointers(config, resolution, pages)))
    return _fit(parts, HOW_TO, config.settings.inject.max_chars)


def _overview_parts(config: Config, resolution: Resolution, slug: str) -> list[Part]:
    """The project's own layers: its name, the last handoff, and its overview's facts."""
    overview = _read_page(config.vault / "projects" / slug / OVERVIEW)
    parts = [_heading(resolution, overview)]
    parts.append(_handoff_part(config, slug))
    parts += _section_parts(overview, OVERVIEW_SECTIONS)
    return parts


def _heading(resolution: Resolution, overview: pagemod.Page | None) -> Part:
    """The block's title line, then the overview's description as its one-line summary."""
    frontmatter = overview.frontmatter if overview else None
    project = resolution.project
    title = _text(frontmatter, "title") or (project.title if project else "") or resolution.slug
    lines = [f"# Memory: {title}"]
    description = _text(frontmatter, "description")
    if description:
        lines.append(description)
    return ("", lines)


def _handoff_part(config: Config, slug: str) -> Part:
    """The handoff of the newest digest for the project, if there is one. Digests arrive
    with M2.6, so until then this part is empty."""
    return _part("Last session", _handoff(_newest_digest(config.vault / "digests" / slug)))


def _newest_digest(directory: Path) -> Path | None:
    """The latest digest under ``directory``, which M2.6 files at
    ``digests/<slug>/<yyyy>/<mm>/<date>-<agent>-<session>.md`` (design 6)."""
    return max(directory.rglob("*.md"), key=_digest_date, default=None)


def _digest_date(digest: Path) -> tuple[int, int, str]:
    """The date a digest is filed under: its year and month directories as numbers,
    then the ``<date>-<agent>-<session>`` name that follows them. The directories read
    as numbers so an unpadded month cannot sort an older digest above a newer one."""
    parts = digest.parts
    return (_as_int(parts[-3]), _as_int(parts[-2]), digest.name)


def _as_int(part: str) -> int:
    return int(part) if part.isdigit() else 0


def _handoff(digest: Path | None) -> list[str]:
    """The handoff note of one digest: what was in flight, what failed, and what is open,
    as M2.6 writes it into the digest's frontmatter under ``aft.handoff`` (design 8)."""
    if digest is None:
        return []
    aft = _frontmatter(digest).get("aft")
    note = aft.get("handoff") if isinstance(aft, dict) else None
    if not isinstance(note, dict):
        return []
    in_flight = _text(note, "in_flight")
    lines = [f"- In flight: {in_flight}"] if in_flight else []
    for key in ("failed", "open"):
        items = note.get(key)
        if isinstance(items, list):
            lines += [
                f"- {key.capitalize()}: {item.strip()}"
                for item in items
                if isinstance(item, str) and item.strip()
            ]
    return lines


def _section_parts(page: pagemod.Page | None, sections: Iterable[str] | None) -> list[Part]:
    """The page's injected facts, in one part per section. ``None`` keeps the page's own
    section order."""
    if page is None:
        return []
    order = dict.fromkeys(page.sections) if sections is None else sections
    parts = []
    for section in order:
        facts = [
            f for f in page.facts if f.section == section and f.state in INJECTED and not f.struck
        ]
        parts.append(_part(section, [f"- {_shown(fact)}" for fact in facts]))
    return parts


def _shown(fact: pagemod.Fact) -> str:
    """A fact as the block reads it: its statement, with its state marked after it. The
    source tag and the footnotes are the page's grammar, not content to repeat."""
    mark = MARKS.get(fact.state)
    return f"{fact.statement} {mark}" if mark else fact.statement


def _pointers(config: Config, resolution: Resolution, directory: Path) -> list[str]:
    """One line per other page of the project and per collection that covers it, so the
    block stays a map rather than becoming a dump (design 10, 11)."""
    lines = [
        _pointer(path, config.vault)
        for path in sorted(directory.rglob("*.md"))
        if path.name not in (OVERVIEW, LISTING)
    ]
    return lines + _collection_lines(config, resolution)


def _collection_lines(config: Config, resolution: Resolution) -> list[str]:
    """Collections the project is one of ``aft.projects`` for (design 11)."""
    project = resolution.project
    if project is None:
        return []
    lines = []
    for path in sorted((config.vault / "references").glob(f"*/{COLLECTION}")):
        aft = _frontmatter(path).get("aft")
        projects = aft.get("projects") if isinstance(aft, dict) else None
        if isinstance(projects, list) and project.id in projects:
            lines.append(_pointer(path, config.vault))
    return lines


def _pointer(path: Path, vault: Path) -> str:
    frontmatter = _frontmatter(path)
    title = _text(frontmatter, "title") or path.stem
    summary = _text(frontmatter, "description")
    where = path.relative_to(vault).as_posix()
    return f"- {title}{f': {summary}' if summary else ''} (aft read {where})"


def _part(heading: str, lines: list[str]) -> Part:
    """One labelled part of the block, or nothing when there is nothing to say."""
    return (heading, lines) if lines else ("", [])


def _fit(parts: list[Part], closing: str, max_chars: int) -> str:
    """The parts stacked in order, each trimmed to what is left of ``max_chars``.

    Lines come off a part's end whole, so a fact is either in the block or left out
    and never cut short. A part with no room left for a line of its own drops out
    with its heading rather than showing an empty one, and ``closing`` (the block's
    fixed last line) keeps room for itself, so the longest overview still tells the
    session how to search and read (design 10).
    """
    room = max_chars - len(closing) - 2
    blocks: list[str] = []
    used = 0
    for heading, content in parts:
        label = _label(heading)
        cost = (2 if blocks else 0) + len(label)
        kept: list[str] = []
        for line in content:
            extra = len(line) + (1 if kept else 0)
            if used + cost + extra > room:
                break
            kept.append(line)
            cost += extra
        if not kept:
            continue
        blocks.append(label + "\n".join(kept))
        used += cost
    if used + (2 if blocks else 0) + len(closing) <= max_chars:
        blocks.append(closing)
    return "\n\n".join(blocks)


def _label(heading: str) -> str:
    """A part's heading as the block shows it, with the blank line under it."""
    return f"## {heading}\n\n" if heading else ""


def _frontmatter(path: Path) -> dict:
    """A page's frontmatter, or nothing when it has no readable one."""
    text = _read(path)
    if text is None:
        return {}
    frontmatter, _, _, error = pagemod.split_frontmatter(text)
    return frontmatter if not error and isinstance(frontmatter, dict) else {}


def _read_page(path: Path) -> pagemod.Page | None:
    text = _read(path)
    return None if text is None else pagemod.parse(text, path)


def _read(path: Path) -> str | None:
    """The block is built from whatever the vault holds, so an absent or unreadable page
    contributes nothing rather than failing the session that asked for it."""
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return None


def _text(mapping: dict | None, key: str) -> str:
    value = (mapping or {}).get(key)
    return value.strip() if isinstance(value, str) else ""
