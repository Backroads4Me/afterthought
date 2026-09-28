"""The one event shape every agent adapter produces.

This is the adapter boundary of DESIGN 5: agent-specific payloads become one
`SessionEvent`, defined below, and that definition is the whole boundary contract.
Nothing past this file may assume anything about Claude Code or pi.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# The three kinds every agent's lifecycle events normalize into.
SESSION_START = "session_start"
COMPACTION = "compaction"
SESSION_END = "session_end"


@dataclass(frozen=True)
class SessionEvent:
    agent: str  # "claude" | "pi"
    kind: str  # SESSION_START | COMPACTION | SESSION_END
    session_id: str
    cwd: Path
    transcript_path: Path | None
    time: datetime  # when the hook ran, UTC
    detail: str | None  # why the agent fired the event, e.g. Claude's source=compact


class AdapterError(Exception):
    """A payload or event name this adapter cannot turn into a SessionEvent."""


def resolve_event(
    agent: str, hook_event: str, events: dict[str, tuple[str, str]]
) -> tuple[str, str]:
    """The kind and the detail field an agent's table gives one of its event names.

    A name the table does not list is refused: there is no kind to normalize it to,
    and guessing one would file the event under that kind for good.
    """
    entry = events.get(hook_event)
    if entry is None:
        raise AdapterError(
            f"unknown {agent} hook event {hook_event!r}, expected one of {' or '.join(events)}"
        )
    return entry


def string_field(payload: dict, key: str) -> str:
    """A required string field of a JSON payload.

    Absent, empty, and wrongly typed values are all failures, so an event is never
    dispatched with a half-known session id or working directory.
    """
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise AdapterError(f"{key} is missing or is not a non-empty string")
    return value


def optional_string_field(payload: dict, key: str) -> str | None:
    """An optional string field: the reason an agent fired an event.

    It never decides what the hook does, so anything that is not a non-empty string -
    absent, null, empty, or of another type - reads as absent rather than refusing the
    event. Only the fields the plan requires may refuse one.
    """
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def path_field(payload: dict, key: str) -> Path:
    """A required field holding a path.

    Absent, null, empty and wrongly typed values are all failures, so an event is
    never dispatched without a working directory or, for Claude Code, a transcript.
    """
    return Path(string_field(payload, key))


def optional_path_field(payload: dict, key: str) -> Path | None:
    """A field holding a path a payload may leave out.

    pi's own session file is undefined until a session is first written, so its
    transcript may be absent or reported empty; either way the event still stands.
    Every other path an adapter reads is required.
    """
    value = optional_string_field(payload, key)
    return None if value is None else Path(value)
