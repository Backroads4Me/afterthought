"""The one event shape every agent adapter produces.

This is the adapter boundary of DESIGN 5: agent-specific payloads become a single
normalized event carrying the agent, the kind, the session id, the working
directory, the transcript path and the time. Nothing past this file may assume
anything about Claude Code or pi.
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


def resolve_kind(agent: str, hook_event: str, payload: dict, events: dict[str, str]) -> str:
    """The normalized kind for one agent's event name.

    The payload names its own event too, and a payload that disagrees with the
    event the hook was invoked for means the agent's wiring is wrong: recording
    that payload under the wrong kind would put a compaction where a session start
    belongs, so it is refused rather than guessed at.
    """
    kind = events.get(hook_event)
    if kind is None:
        raise AdapterError(
            f"unknown {agent} hook event {hook_event!r}, expected one of {' or '.join(events)}"
        )
    declared = payload.get("hook_event_name")
    if declared is not None and declared != hook_event:
        raise AdapterError(f"{agent} hook ran for {hook_event!r}, payload says {declared!r}")
    return kind


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

    It never decides what the hook does, so a payload without it still parses.
    """
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise AdapterError(f"{key} is not a string")
    return value


def path_field(payload: dict, key: str, *, required: bool = True) -> Path | None:
    """A field holding a path, or None when the payload leaves it out or nulls it.

    pi's own session file is undefined until a session is first written, so
    `transcript_path` is optional there; Claude Code always provides one.
    """
    value = payload.get(key)
    if value is None:
        if required:
            raise AdapterError(f"{key} is missing or is not a non-empty string")
        return None
    if not isinstance(value, str) or not value:
        raise AdapterError(f"{key} is not a path")
    return Path(value)
