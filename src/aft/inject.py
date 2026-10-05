"""The injection block: what an agent is told at session start (design section 10).

It reads files only, so a cold session gets the same block in every agent without a model
call or an index. The parts come out in the design's order and the block as a whole stays
within ``inject.max_chars``: a fact is either in the block or left out, never cut short.
``_block`` gives the one allocation of that budget, so no section can
grow into what a cold session needs to find the rest of memory; a budget with no room for
a layer's first line leaves that layer out and says so plainly under ``Cut``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, date, datetime, time
from pathlib import Path

from aft import page as pagemod
from aft.config import Config
from aft.keys import Resolution
from aft.registry import Project

# The overview's sections, in the order the block shows them.
OVERVIEW_SECTIONS = ("State", "Decisions", "Lessons", "Procedures", "Facts", "References")

# A line's state decides whether the block carries it and how it is marked (design 6).
INJECTED = (pagemod.ACTIVE, pagemod.CONFLICT, pagemod.NEEDS_YOU)
MARKS = {pagemod.CONFLICT: "(disputed)", pagemod.NEEDS_YOU: "(waiting on the user)"}

# Memory is an earlier record; the session in front of the agent outranks it.
PRECEDENCE = "What the user says now outranks memory; a memory line it contradicts is out of date."
HOW_TO = 'Search memory: aft search "<words>". Read a page: aft read <path>.'
RECORD_MARKERS = (
    'A prompt starting with "aft off", "aft on", "off the record" or "on the record" sets what'
    " afterthought keeps; it is not a request. Acknowledge it in a few words and do what the"
    " rest of the prompt asks."
)
NOTES_HEADING = "Needs your decision"

# What the block says when the budget leaves no room for a layer's first line (design 10).
CUT_HEADING = "Cut"
CUT_NOTE = "The rest was cut to fit this block: aft search or aft read reaches all of it."

# The overview is the block itself, and a directory's index.md is a generated listing, so
# neither is a page the block points at (design 6).
OVERVIEW = "overview.md"
LISTING = "index.md"
# Short-term memory has a layer of its own, so it is not pointed at either (design 6).
RECENT = pagemod.RECENT_PAGE
COLLECTION = "collection.md"
PREFERENCES = Path("global/preferences.md")

# One part of the block: the heading its lines sit under, "" when it has none, and
# the lines under that heading.
Part = tuple[str, list[str]]


def block_project(resolution: Resolution) -> Project | None:
    """The project a memory block is built for (DESIGN 7, F49).

    An archived project is left out, so a session in its directory is given the global
    pages alone, exactly as a session of no project is: the project keeps its files and its
    sessions are still captured and dreamed, but its memory does not reach the block until
    ``aft project unarchive`` brings it back.
    """
    project = resolution.project
    return None if project is not None and project.archived else project


def build(config: Config, resolution: Resolution, waiting: int | None = None) -> str:
    """The block for ``resolution``.

    A registered project gets its name and description, the newest digest's handoff, its
    overview's facts, the pages and collections that cover it, and the lines on how to
    search and read. An unregistered or unresolved directory gets only global preferences
    and those lines, and so does a session in an archived project's directory (DESIGN 7,
    F49). Either way, notes waiting on the user's decision come first: ``waiting`` of them,
    counted here when the caller has not counted them already.
    """
    max_chars = config.settings.inject.max_chars
    project = block_project(resolution)
    title: list[Part] = []
    own: list[Part] = []
    page_map: list[Part] = []
    recent: list[Part] = []
    if project:
        directory = config.vault / "projects" / project.slug
        overview = _read_page(directory / OVERVIEW)
        title = [_heading(project, overview)]
        own = [_handoff_part(config, project.slug), *_section_parts(overview, OVERVIEW_SECTIONS)]
        recent = [_recent_part(_read_page(directory / RECENT), config)]
        page_map = [_part("Pages", _pointers(config, project, directory))]
    preferences = _section_parts(_read_page(config.vault / PREFERENCES), None)
    slug = project.slug if project else None
    if waiting is None:
        from aft import notes

        waiting = notes.count(config, slug)
    return _block(title, own, preferences, page_map, max_chars, recent, notes_part(waiting))


def notes_part(waiting: int) -> list[Part]:
    """The notes waiting on the user's decision, as the block tells the agent about them:
    how many, and what to do (BUILD-PLAN F42). Nothing when none wait."""
    if not waiting:
        return []
    return [(NOTES_HEADING, [notes_line(waiting)])]


def notes_line(waiting: int) -> str:
    """What the block asks the agent to do about ``waiting`` notes."""
    noun = "note in memory waits" if waiting == 1 else "notes in memory wait"
    return (
        f"{waiting} {noun} on the user's decision. Before anything else, tell the user, and say"
        ' that replying "settle" goes through them now and any other reply skips them. When'
        ' the user replies "settle", follow the aft skill.'
    )


def _recent_part(page: pagemod.Page | None, config: Config) -> Part:
    """Short-term memory: the active lines of ``recent.md``, in the overview's section
    order, under a heading that says how many sessions short term covers and the date of
    its newest line, so an agent back after a long while can judge how current it is
    (design 6, 10)."""
    if page is None:
        return ("", [])
    facts = [
        fact
        for section in OVERVIEW_SECTIONS
        for fact in page.facts
        if fact.section == section and fact.state in INJECTED and not fact.struck
    ]
    if not facts:
        return ("", [])
    sessions = config.settings.dream.short_term_sessions
    newest = max(fact.date for fact in facts if fact.date)
    heading = f"Recent (last {sessions} sessions, newest {newest})"
    return _part(heading, [f"- {_shown(fact)}" for fact in facts])


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
    """The "Last session" part: the handoff of the project's newest digest, and nothing
    when the project has no digest."""
    digest = _newest_digest(config.vault / "digests" / slug)
    return _part("Last session", [] if digest is None else handoff(_frontmatter(digest)))


def _newest_digest(directory: Path) -> Path | None:
    """The newest digest of a primary session that carries a handoff under ``directory``.

    Worker, library and memory-import digests never supply or remove it.
    """
    digests = list(directory.rglob("*.md"))
    if not digests:
        return None
    days = sorted({_digest_day(d) for d in digests}, reverse=True)
    for day in days:
        same_day = [d for d in digests if _digest_day(d) == day]
        same_day.sort(key=_digest_recency, reverse=True)
        for digest in same_day:
            if "-library-" in digest.name or "-import-" in digest.name:
                continue
            fm = _frontmatter(digest)
            aft = fm.get("aft") if isinstance(fm, dict) else None
            if isinstance(aft, dict) and aft.get("role") == "worker":
                continue
            if handoff(fm):
                return digest
    return None


# A digest name's part suffix: a session's second and later digests of one day are filed
# as ``<date>-<agent>-<session>-2``, ``-3``, and a name without a suffix is part 1.
_PART = re.compile(r"-(\d+)\Z")

# A digest name's leading field: the day its digested chunk ended, which the digest step
# files the page under.
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def _digest_day(digest: Path) -> tuple[int, int, date]:
    """Where a digest sorts by the day it covers: its ``<yyyy>`` and ``<mm>`` directories
    read as numbers, then the day its name opens with.

    The directories read as numbers so an unpadded month cannot sort an older digest above
    a newer one. The day is the one the chunk ended on, not the one the page was written
    on, so a digest worked over days later does not push itself above a session that ran
    down more recently.
    """
    parts = digest.parts
    return (_as_int(parts[-3]), _as_int(parts[-2]), _day_of(digest.stem))


def _digest_recency(digest: Path) -> tuple[datetime, str, int]:
    """Where the digests of one day sort: the instant the page was written, then the
    ``<date>-<agent>-<session>`` name it carries, then its part as a number.

    A page's ``generated.at`` is when the digest step filed it, so on one day the page
    written last is the session that ran down most recently, and a session whose name
    sorts first cannot take the handoff from one that ran on after it. A page recording
    no readable time stands at the start of its day, below every page of that day that
    does record one. The name and the part keep the order whole when two pages were
    stamped alike: a session's ``-2`` sorts above its unsuffixed first page and ``-10``
    above ``-9``. A session id whose last group is digits reads here as a name and a
    large part; its own later parts carry the whole id in their name, which sorts above
    that shorter name, so the highest part still wins.
    """
    found = _PART.search(digest.stem)
    name, part = (digest.stem[: found.start()], int(found.group(1))) if found else (digest.stem, 1)
    return (_written_at(digest, _day_of(digest.stem)), name, part)


def _day_of(stem: str) -> date:
    """The day a digest name opens with. A name opening with none covers no day of its own
    and stands below every dated page.

    The date leads the name because that is how the digest step files a page: design 6's
    ``<date>-<agent>-<session>``.
    """
    found = _DAY.match(stem)
    return date.fromisoformat(found.group()) if found else date.min


def _written_at(digest: Path, day: date) -> datetime:
    """The instant the digest step wrote this page, or the start of ``day`` when the page
    records no readable one."""
    generated = _frontmatter(digest).get("generated")
    stamp = generated.get("at") if isinstance(generated, dict) else None
    if isinstance(stamp, datetime):  # YAML reads an unquoted time as a datetime itself
        return _as_utc(stamp)
    if isinstance(stamp, str):
        try:
            stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            pass
        else:
            return _as_utc(stamp)
    return datetime.combine(day, time.min, tzinfo=UTC)


def _as_utc(moment: datetime) -> datetime:
    """A stamp with no offset reads as UTC, so an aware and a naive one can be compared."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _as_int(part: str) -> int:
    return int(part) if part.isdigit() else 0


def handoff(frontmatter: dict | None) -> list[str]:
    """The handoff note of a digest: what was in flight, what failed, and what is open, as
    M2.6 writes it into the digest's frontmatter under ``aft.handoff`` (design 8). The block
    shows these lines and `aft.index` indexes them, so this is the one reading of the field."""
    aft = (frontmatter or {}).get("aft")
    note = aft.get("handoff") if isinstance(aft, dict) else None
    if not isinstance(note, dict):
        return []
    in_flight = _text(note, "in_flight")
    lines = [f"- In flight: {in_flight}"] if in_flight else []
    for key in ("failed", "open"):
        items = note.get(key)
        if isinstance(items, list):
            lines += [f"- {key.capitalize()}: {text}" for text in map(one_line, items) if text]
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
        if path.name not in (OVERVIEW, LISTING, RECENT)
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
    recent: Iterable[Part] = (),
    waiting: Iterable[Part] = (),
) -> str:
    """The layers in the design's order, under one stated allocation of ``max_chars``.

    The fixed costs come first - the block's title and the closing lines: that what the
    user says now comes first, how to search and read, and the capture markers - and then
    every part holds its floor, the heading and first line that name a layer (``_trim``).
    What the floors leave is the budget of the two producers that grow with the vault,
    divided evenly between them: the project's own text and its page map, each capped from
    that remainder rather than from the block, so they cannot over-subscribe it and neither
    can reach what the other needs.
    The global preferences layer, which the plan gives no limit of its own, takes whatever
    those two leave. When the budget leaves no room for a layer's first line, the block
    keeps its title and says under ``Cut`` that the rest was cut: a short block is
    acceptable, a layer missing without a word is the defect (design 10).
    """
    full_closing: list[Part] = [("", [PRECEDENCE, HOW_TO, RECORD_MARKERS])]
    recent_list = list(recent)
    waiting_list = list(waiting)
    head = _trim(title, max_chars)

    # Reserve the closing lines' full width before any layer whenever max_chars can hold
    # the title and both closing lines.
    if _width([*head, *full_closing]) <= max_chars:
        closing = full_closing
    else:
        closing_room = max(0, max_chars - _width(head) - (2 if head else 0))
        closing = _trim(full_closing, closing_room)

    has_waiting = any(lines for _, lines in waiting_list)
    own_sections_with_lines = [p for p in own if p[1]]
    has_own = bool(own_sections_with_lines)
    has_prefs = any(lines for _, lines in preferences)
    has_recent = any(lines for _, lines in recent_list)
    has_map = any(lines for _, lines in page_map)

    own_floor = _floors(own)
    preferences_floor = _floors(preferences)
    recent_floor = _floors(recent_list)
    map_floor = _floors(page_map)

    def _assemble(cut: list[Part]) -> tuple[list[Part], list[Part], list[Part]]:
        fixed = [*head, *cut, *closing]
        rem_room = max(0, max_chars - _width(fixed) - 2)
        waiting_layer = _trim(waiting_list, rem_room)

        body_room = max(0, max_chars - _width([*head, *cut, *waiting_layer, *closing]) - 2)
        # Short-term memory takes only what the long-term layers leave (design 10), so its
        # floor has no share in the room they divide.
        body_floors = [*own_floor, *preferences_floor, *map_floor]
        minimal = _trim(body_floors, body_room)
        extra = max(0, (body_room - _width(minimal)) // 2)

        project_layer = _trim(own, min(_width(own_floor) + extra, body_room))
        pref_layer = _trim(preferences, min(_width(preferences_floor) + extra, body_room))

        prefix = [*head, *cut, *waiting_layer, *project_layer, *pref_layer]
        room = max_chars - _width([*prefix, *map_floor, *closing]) - 2
        recent_layer = _trim(recent_list, max(0, room)) if recent_floor else []

        room = max_chars - _width([*prefix, *recent_layer, *closing]) - 2
        pages = _trim(page_map, min(_width(map_floor) + max(0, room), max_chars // 2))

        body = [*project_layer, *pref_layer, *recent_layer, *pages]
        body = _trim(body, body_room)
        return waiting_layer, body, [*head, *waiting_layer, *cut, *body, *closing]

    def _is_absent(waiting_parts: list[Part], body_parts: list[Part]) -> bool:
        if has_waiting and not any(lines for _, lines in waiting_parts):
            return True
        if has_own:
            for heading, _ in own_sections_with_lines:
                if not any(lines for h, lines in body_parts if h == heading and lines):
                    return True
        pref_headings = {h for h, _ in preferences}
        if has_prefs and not any(lines for h, lines in body_parts if h in pref_headings and lines):
            return True
        recent_headings = {h for h, _ in recent_list}
        if has_recent and not any(
            lines for h, lines in body_parts if h in recent_headings and lines
        ):
            return True
        map_headings = {h for h, _ in page_map}
        if has_map and not any(lines for h, lines in body_parts if h in map_headings and lines):
            return True
        return False

    waiting_layer, body, parts = _assemble(cut=[])
    if _is_absent(waiting_layer, body):
        waiting_layer, body, parts = _assemble(cut=[(CUT_HEADING, [CUT_NOTE])])

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
    frontmatter, _, _, error = pagemod.split_frontmatter(text, strip_sources_list=True)
    return frontmatter if not error and isinstance(frontmatter, dict) else {}


def _read_page(path: Path) -> pagemod.Page | None:
    text = _read(path)
    return None if text is None else pagemod.parse(text, path, strip_sources_list=True)


def _read(path: Path) -> str | None:
    """The block is built from whatever the vault holds, so an absent or unreadable page
    contributes nothing rather than failing the session that asked for it."""
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return None


def _text(mapping: dict | None, key: str) -> str:
    return one_line((mapping or {}).get(key))


def one_line(value: object) -> str:
    """A frontmatter value as one line: a block scalar keeps its words and loses its
    breaks, so no value reaches the injection block or the search index as a second line.

    `aft.index` files a page's title and description through it too, so a row and the block
    a session starts with read the same words."""
    return " ".join(value.split()) if isinstance(value, str) else ""
