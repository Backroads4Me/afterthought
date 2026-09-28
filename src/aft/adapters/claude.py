"""Claude Code hook payloads to normalized events.

Claude Code runs a hook command for each event it supports and writes that event's
JSON payload to its stdin. The payloads these field names come from, and the Claude
Code version that produced them, are recorded in `tests/fixtures/hooks/`. Every one
carries `session_id`, `transcript_path` and `cwd`, and each event adds a field naming
what set it off. Only the three events afterthought installs appear here; anything
else is an unknown event (BUILD-PLAN M1.1).
"""

from __future__ import annotations

from datetime import datetime

from aft.events import (
    COMPACTION,
    SESSION_END,
    SESSION_START,
    SessionEvent,
    optional_string_field,
    path_field,
    resolve_event,
    string_field,
)

AGENT = "claude"

# Claude Code's event name -> the kind afterthought dispatches it as, and the field
# that says why it fired, kept as SessionEvent.detail: `source` is startup, resume,
# clear, compact or fork; `trigger` is manual or auto; `reason` is clear, resume,
# logout, prompt_input_exit or other.
EVENTS = {
    "SessionStart": (SESSION_START, "source"),
    "PreCompact": (COMPACTION, "trigger"),
    "SessionEnd": (SESSION_END, "reason"),
}


def parse_claude(hook_event: str, payload: dict, now: datetime) -> SessionEvent:
    """One Claude Code hook event and payload, as a SessionEvent."""
    kind, detail_field = resolve_event(AGENT, hook_event, EVENTS)
    return SessionEvent(
        agent=AGENT,
        kind=kind,
        session_id=string_field(payload, "session_id"),
        cwd=path_field(payload, "cwd"),
        transcript_path=path_field(payload, "transcript_path"),
        time=now,
        detail=optional_string_field(payload, detail_field),
    )
