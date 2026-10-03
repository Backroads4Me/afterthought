"""Where a line of short-term memory stands in its project's sessions (DESIGN 6, R10).

Short-term memory is counted in sessions, never in days. A project's sessions are its
primary sessions that have a digest, in session order, whatever happened in them. Each
line's sources name the sessions it was said or seen in, and two numbers follow from them:

* the line stays on ``recent.md`` while its newest source is one of the project's last
  ``dream.short_term_sessions`` sessions, and rolls off once it is not;
* the line becomes long-term memory once its oldest and newest sources are at least that
  many sessions apart: it stayed true through more than a full window.

Everything here reads files only; ``aft.dream.apply`` makes the edits.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from aft import digest, page
from aft.gitutil import git

DIGESTS = "digests"

_PART = re.compile(r"^(?P<base>.+)-(?P<part>\d+)$")
_SESSION_URI = re.compile(r"^aft://session/(?P<agent>[^/#]+)/(?P<session>[^/#]+)#")
_EDIT_URI = re.compile(r"^aft://edit/(?P<sha>[0-9a-f]{7,40})$")
# A dream run's id ends in the time it started (aft.dream.apply.run_project).
_RUN_TIME = re.compile(
    r"(?P<d>\d{4})(?P<m>\d{2})(?P<day>\d{2})T(?P<h>\d{2})(?P<mi>\d{2})(?P<s>\d{2})Z$"
)


@dataclass(frozen=True)
class Session:
    """One primary session of the project, as its first digest names it."""

    agent: str
    session_id: str
    at: str
    """When the session's first digest was written, as ``generated.at`` holds it."""

    dreamed: str = ""
    """When a dream run consumed that digest, from the run id its ``aft.consumed`` holds,
    in the same form; empty while it waits."""


def written_at(frontmatter: dict) -> str:
    """A digest's ``generated.at`` as a sortable UTC string, empty when it has none.

    YAML reads an unquoted timestamp as a datetime and a quoted one as text, so both are
    brought to the one form M2.6 writes.
    """
    generated = frontmatter.get("generated")
    at = generated.get("at") if isinstance(generated, dict) else None
    if isinstance(at, datetime):
        at = at if at.tzinfo else at.replace(tzinfo=UTC)
        return f"{at.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}"
    return at if isinstance(at, str) else ""


def names_session(path: Path, stems: set[str]) -> tuple[str, str, dict] | None:
    """The (agent, session id) one digest file names for the project's own work, with the
    file's frontmatter, or None when it names none.

    A library or memory-import digest proposes facts from imported material (DESIGN 11),
    and a worker's digest is a worker session's evidence (DESIGN 8): neither is a session
    of the project's own work, so neither counts toward the short-term window and neither
    reconciles the page (DESIGN 6, R10). A digest filed as a later part of one day names
    its session's first part, and only when that first part's file is beside it, where the
    evidence archive holds the session.
    """
    if "-library-" in path.name or "-import-" in path.name:
        return None
    named = digest._DIGEST_NAME.match(path.stem)
    if named is None:
        return None
    try:
        frontmatter = page.split_frontmatter(path.read_text(encoding="utf-8"))[0] or {}
    except (OSError, UnicodeDecodeError):
        return None
    aft = frontmatter.get("aft")
    if isinstance(aft, dict) and aft.get("role") == "worker":
        return None
    agent, session = named.group("agent"), named.group("session")
    part = _PART.match(session)
    # A later part of one day is filed as <session>-2, and only beside its first part.
    if part and f"{named.group('date')}-{agent}-{part.group('base')}" in stems:
        session = part.group("base")
    return agent, session, frontmatter


def sessions(vault: Path, slug: str) -> list[Session]:
    """The project's primary sessions that have a digest, in session order.

    Session order is the order the dream processes digests in: the order they were written
    (``generated.at``), then the path. A session's place is its first digest's. Worker,
    library and memory-import digests are not sessions of the project's own work.
    """
    root = vault / DIGESTS / slug
    if not root.is_dir():
        return []
    stems = {path.stem for path in root.rglob("*.md")}
    found: list[tuple[str, str, str, str, str]] = []
    for path in root.rglob("*.md"):
        named = names_session(path, stems)
        if named is None:
            continue
        agent, session, frontmatter = named
        aft = frontmatter.get("aft")
        dreamed = _run_time(aft.get("consumed") if isinstance(aft, dict) else None)
        found.append((written_at(frontmatter), str(path), agent, session, dreamed))
    ordered: dict[tuple[str, str], Session] = {}
    for at, _, agent, session, dreamed in sorted(found):
        ordered.setdefault((agent, session), Session(agent, session, at, dreamed))
    return list(ordered.values())


def _run_time(run_id: object) -> str:
    """The start of the dream run a digest's ``aft.consumed`` names, or empty."""
    match = _RUN_TIME.search(run_id) if isinstance(run_id, str) else None
    if match is None:
        return ""
    return "{d}-{m}-{day}T{h}:{mi}:{s}Z".format(**match.groupdict())


def positions(
    fact: page.Fact,
    sources: Mapping[str, dict],
    order: list[Session],
    vault: Path,
) -> list[int]:
    """The places in ``order`` of the sessions this line's sources come from.

    A source from a session names it in its ``aft://session/<agent>/<id>#<entry>`` resource.
    A source from a hand edit counts as the newest session dreamed before the edit's
    commit (DESIGN 6), and as coming before the first session when none was. A source
    naming neither, or a session the project has no digest for, has no place.
    """
    index = {(s.agent, s.session_id): n for n, s in enumerate(order)}
    found: list[int] = []
    for ref in fact.refs:
        resource = str((sources.get(ref) or {}).get("resource", ""))
        session = _SESSION_URI.match(resource)
        if session is not None:
            place = index.get((session.group("agent"), session.group("session")))
            if place is not None:
                found.append(place)
            continue
        edit = _EDIT_URI.match(resource)
        if edit is not None:
            place = _edit_place(vault, edit.group("sha"), order)
            if place is not None:
                found.append(place)
    return found


def rolls_off(places: list[int], order: list[Session], window: int) -> bool:
    """The line's newest source has left the project's last ``window`` sessions. A line
    with no source the project's sessions can place stays: nothing shows it has gone by."""
    return bool(places) and max(places) < len(order) - window


def outlasted(places: list[int], window: int) -> bool:
    """The line's oldest and newest sources are at least ``window`` sessions apart."""
    return bool(places) and max(places) - min(places) >= window


def _edit_place(vault: Path, sha: str, order: list[Session]) -> int | None:
    """The newest session dreamed before a hand edit's commit, or -1, before the first
    session, when no session was."""
    result = git(["show", "-s", "--format=%cI", "--end-of-options", sha], cwd=vault, check=False)
    if result.returncode != 0:
        return None
    try:
        when = datetime.fromisoformat(result.stdout.strip()).astimezone(UTC)
    except ValueError:
        return None
    stamp = f"{when:%Y-%m-%dT%H:%M:%SZ}"
    before = [n for n, s in enumerate(order) if s.dreamed and s.dreamed <= stamp]
    return before[-1] if before else -1
