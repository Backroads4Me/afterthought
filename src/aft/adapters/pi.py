"""pi hook payloads to normalized events.

pi (0.87.1) has no shell hooks of its own: its lifecycle events reach the outside
world through an extension (M1.4), which writes the payload for the event it handles
to the stdin of `aft hook pi <event>`. The event names here are pi's own extension
event names, and `reason` is pi's field for what set an event off: startup, reload,
new, resume or fork on `session_start` and `session_shutdown`, manual, threshold or
overflow on `session_before_compact` (BUILD-PLAN M1.1).

`session_id`, `cwd` and `transcript_path` come from pi's session manager, whose
`getSessionFile()` is undefined until the session is first written, so a payload can
leave the transcript out.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from aft.events import (
    COMPACTION,
    SESSION_END,
    SESSION_START,
    SessionEvent,
    optional_string_field,
    path_field,
    resolve_kind,
    string_field,
)

AGENT = "pi"

# pi's event name -> the kind afterthought dispatches it as.
EVENTS = {
    "session_start": SESSION_START,
    "session_before_compact": COMPACTION,
    "session_shutdown": SESSION_END,
}


def parse_pi(hook_event: str, payload: dict, now: datetime) -> SessionEvent:
    """One pi event and payload, as a SessionEvent."""
    kind = resolve_kind(AGENT, hook_event, payload, EVENTS)
    return SessionEvent(
        agent=AGENT,
        kind=kind,
        session_id=string_field(payload, "session_id"),
        cwd=Path(string_field(payload, "cwd")),
        transcript_path=path_field(payload, "transcript_path", required=False),
        time=now,
        detail=optional_string_field(payload, "reason"),
    )
