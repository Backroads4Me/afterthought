"""The injection block: what an agent is told at session start (design section 10).

It reads files only, so a cold session gets the same block in every agent without a model
call or an index. The parts come out in the design's order and ``inject.max_chars``
trims each part to what is left of the budget: a fact is either in the block or left
out, never cut short. Firstmate's ruling on M1.2 states the allocation: the fixed costs
first, then every layer's floor, then what remains between the two layers that grow
with the vault, so no section can grow into what a cold session needs to find the rest
of memory. A budget with no room for a layer's first line leaves that layer out and
says so plainly under ``Cut``.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from aft import page as pagemod
from aft.config import Config
from aft.keys import Resolution
from aft.registry import Project

# The overview's sections, in the order the block shows them.
OVERVIEW_SECTIONS = ("State", "Decisions", "Lessons", "Procedures", "Facts", "References")

# A line's state decides whether the block carries it and how it is marked (design 6).
INJECTED = (pagemod.ACTIVE, pagemod.CONFLICT, pagemod.NEEDS_YOU)
MARKS = {pagemod.CONFLICT: "(disputed)", pagemod.NEEDS_YOU: "(waiting on Ted)"}

HOW_TO = 'Search memory: aft search "<words>". Read a page: aft read <path>.'

# What the block says when the budget leaves no room for a layer's first line (design 10).
CUT_HEADING = "Cut"
CUT_NOTE = "The rest was cut to fit this block: aft search or aft read reaches all of it."

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
    max_chars = config.settings.inject.max_chars
    project = resolution.project
    title: list[Part] = []
    own: list[Part] = []
    page_map: list[Part] = []
    if project:
        directory = config.vault / "projects" / project.slug
        overview = _read_page(directory / OVERVIEW)
        title = [_heading(project, overview)]
        own = [_handoff_part(config, project.slug), *_section_parts(overview, OVERVIEW_SECTIONS)]
        page_map = [_part("Pages", _pointers(config, project, directory))]
    preferences = _section_parts(_read_page(config.vault / PREFERENCES), None)
    return _block(title, own, preferences, page_map, max_chars)


def _heading(project: Project, overview: pagemod.Page | None) -> Part:
    """The block's title line, then the overview's description as its one-line summary.

    The registry always carries a title, falling back to the slug for an entry written
    without one.
    """
    frontmatter = overview.frontmatter if overview else None
    title = _text(frontmatter, "title") or project.title
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
            lines += [f"- {key.capitalize()}: {text}" for text in map(_line, items) if text]
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


def _pointers(config: Config, project: Project, directory: Path) -> list[str]:
    """One line per other page of the project and per collection that covers it, so the
    block stays a map rather than becoming a dump (design 10, 11)."""
    lines = [
        _pointer(path, config.vault)
        for path in sorted(directory.rglob("*.md"))
        if path.name not in (OVERVIEW, LISTING)
    ]
    return lines + _collection_lines(config, project)


def _collection_lines(config: Config, project: Project) -> list[str]:
    """Collections the project is one of ``aft.projects`` for (design 11)."""
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


def _trim(parts: list[Part], max_chars: int) -> list[Part]:
    """The parts with whole lines cut off their ends so they take ``max_chars`` at most.

    Every part that has a line holds its heading and that first line before a later
    part is offered a second one, and a part with no room for its first line costs
    itself alone: the parts after it are still offered their floor. Past the first
    line the room goes to the parts in order, whole lines only, so a fact is either in
    the block or left out, never cut short and never left under an empty heading.
    """
    # A single line longer than the whole budget can never be shown, so it costs only
    # itself: the lines after it and the parts after it keep their room.
    parts = [
        (heading, [line for line in lines if len(line) <= max_chars]) for heading, lines in parts
    ]
    kept: list[Part] = []
    later: list[list[str]] = []
    used = 0
    for heading, content in parts:
        if not content:
            continue
        cost = (2 if kept else 0) + len(_label(heading)) + len(content[0])
        if used + cost <= max_chars:
            kept.append((heading, [content[0]]))
            later.append(content[1:])
            used += cost
    for (_, lines), rest in zip(kept, later, strict=True):
        for line in rest:
            if used + len(line) + 1 > max_chars:
                break
            lines.append(line)
            used += len(line) + 1
    return kept


def _blocks(parts: list[Part]) -> list[str]:
    """The parts as the block shows them: each heading above its own lines."""
    return [_label(heading) + "\n".join(lines) for heading, lines in parts]


def _block(
    title: list[Part],
    own: list[Part],
    preferences: list[Part],
    page_map: list[Part],
    max_chars: int,
) -> str:
    """The layers in the design's order, under one stated allocation of ``max_chars``.

    The fixed costs come first - the block's title and the closing line on how to
    search and read - and then every part holds its floor, the heading and first line
    that name a layer (``_trim``). What the floors leave is the budget of the two
    producers that grow with the vault, divided evenly between them: the project's own
    text and its page map, each capped from that remainder rather than from the block,
    so they cannot over-subscribe it and neither can reach what the other needs. The
    global preferences layer, which the plan gives no limit of its own, takes whatever
    those two leave. When the budget leaves no room for a layer's first line, the block
    keeps its title and says under ``Cut`` that the rest was cut: a short block is
    acceptable, a layer missing without a word is the defect (design 10, firstmate's
    ruling on M1.2).
    """
    closing: list[Part] = [("", [HOW_TO])]
    head = _trim(title, max_chars)  # the title, as far as the budget can show it
    own_floor = _floors(own)
    preferences_floor = _floors(preferences)
    map_floor = _floors(page_map)
    floors = [*head, *own_floor, *preferences_floor, *map_floor, *closing]
    minimal = _trim(floors, max_chars)
    extra = (max_chars - _width(minimal)) // 2
    project_layer = _trim(own, min(_width(own_floor) + extra, max_chars))
    pages = _trim(page_map, min(_width(map_floor) + extra, max_chars))
    room = max_chars - _width([*head, *project_layer, *pages, *closing]) - 2
    body = [*project_layer, *_trim(preferences, room), *pages]
    cut: list[Part] = [(CUT_HEADING, [CUT_NOTE])] if len(minimal) < len(floors) else []
    parts = [*head, *cut, *body, *closing]
    return "\n\n".join(_blocks(_trim(parts, max_chars)))


def _floors(parts: list[Part]) -> list[Part]:
    """Each part reduced to its floor: its heading and the first line under it."""
    return [(heading, lines[:1]) for heading, lines in parts if lines]


def _width(parts: list[Part]) -> int:
    """The characters the parts take once they are stacked together."""
    return len("\n\n".join(_blocks(parts)))


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
    return _line((mapping or {}).get(key))


def _line(value: object) -> str:
    """A frontmatter value as one line: a block scalar keeps its words and loses its
    breaks, so no value reaches the block as a second, unbulleted line."""
    return " ".join(value.split()) if isinstance(value, str) else ""
