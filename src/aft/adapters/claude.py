"""Claude Code hook payloads to normalized events.

Claude Code runs a hook command for each event it supports and writes that event's
JSON payload to its stdin. Field names here are the hook input schemas of the
installed Claude Code (2.1.283): every payload carries `session_id`,
`transcript_path` and `cwd`, and each event adds a field naming what set it off.
Only the three events afterthought installs appear here; anything else is an
unknown event (BUILD-PLAN M1.1).
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

AGENT = "claude"

# Claude Code's event name -> the kind afterthought dispatches it as.
EVENTS = {
    "SessionStart": SESSION_START,
    "PreCompact": COMPACTION,
    "SessionEnd": SESSION_END,
}

# The field that says why each event fired, kept as SessionEvent.detail: `source` is
# startup, resume, clear, compact or fork; `trigger` is manual or auto; `reason` is
# clear, resume, logout, prompt_input_exit or other.
DETAIL_FIELD = {
    "SessionStart": "source",
    "PreCompact": "trigger",
    "SessionEnd": "reason",
}


def parse_claude(hook_event: str, payload: dict, now: datetime) -> SessionEvent:
    """One Claude Code hook event and payload, as a SessionEvent."""
    kind = resolve_kind(AGENT, hook_event, payload, EVENTS)
    return SessionEvent(
        agent=AGENT,
        kind=kind,
        session_id=string_field(payload, "session_id"),
        cwd=Path(string_field(payload, "cwd")),
        transcript_path=path_field(payload, "transcript_path"),
        time=now,
        detail=optional_string_field(payload, DETAIL_FIELD[hook_event]),
    )
