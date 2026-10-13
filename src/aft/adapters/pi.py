"""pi hook payloads to normalized events.

pi (0.99.1) has no shell hooks of its own: its lifecycle events reach the outside
world through an extension (M1.4), which writes the payload for the event it handles
to the stdin of `aft hook pi <event>`. The event names here are pi's own extension
event names, and `reason` is pi's field for what set an event off: `session_start`
carries startup, reload, new, resume or fork, `session_shutdown` carries quit,
reload, new, resume or fork, and `session_before_compact` carries manual, threshold
or overflow (BUILD-PLAN M1.1). `before_agent_start` (BUILD-PLAN F87) is the one
exception: its `prompt` field is not a reason but the event's own content, the raw
user prompt text (pi's dist/core/extensions/types.d.ts, BeforeAgentStartEvent), and
the parser keeps it as SessionEvent.prompt.

`session_id`, `cwd` and `transcript_path` come from pi's session manager, whose
`getSessionFile()` is undefined until the session is first written, so a payload can
leave the transcript out or report it empty; either way the event still stands.
"""

from __future__ import annotations

from datetime import datetime

from aft.events import (
    COMPACTION,
    PROMPT,
    SESSION_END,
    SESSION_START,
    SessionEvent,
    optional_path_field,
    optional_string_field,
    path_field,
    resolve_event,
    string_field,
)

AGENT = "pi"

# pi's event name -> the kind afterthought dispatches it as, and the field that says
# why it fired, kept as SessionEvent.detail. pi names that field the same way on all
# three capture events; the prompt event's field is the prompt itself (BUILD-PLAN F87).
EVENTS = {
    "session_start": (SESSION_START, "reason"),
    "session_before_compact": (COMPACTION, "reason"),
    "session_compact": (COMPACTION, "reason"),
    "session_shutdown": (SESSION_END, "reason"),
    "before_agent_start": (PROMPT, "prompt"),
}


def parse_pi(hook_event: str, payload: dict, now: datetime) -> SessionEvent:
    """One pi event and payload, as a SessionEvent."""
    kind, field = resolve_event(AGENT, hook_event, EVENTS)
    prompt = string_field(payload, field) if kind == PROMPT else None
    return SessionEvent(
        agent=AGENT,
        kind=kind,
        session_id=string_field(payload, "session_id"),
        cwd=path_field(payload, "cwd"),
        transcript_path=optional_path_field(payload, "transcript_path"),
        time=now,
        detail=None if kind == PROMPT else optional_string_field(payload, field),
        prompt=prompt,
    )
