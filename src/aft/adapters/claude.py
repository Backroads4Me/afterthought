"""Claude Code hook payloads to normalized events.

Claude Code runs a hook command for each event it supports and writes that event's
JSON payload to its stdin. The payloads these field names come from, and the Claude
Code version that produced them, are recorded in `tests/fixtures/hooks/`. Every one
carries `session_id`, `transcript_path` and `cwd`, and each event adds a field naming
what set it off. Only the four events afterthought installs appear here; anything
else is an unknown event (BUILD-PLAN M1.1, F87).
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

AGENT = "claude"

# Claude Code's event name -> the kind afterthought dispatches it as, and the payload
# field that says why it fired, kept as SessionEvent.detail. The hooks reference
# enumerates `source` as startup, resume, clear, compact or fork, and `trigger` as
# manual or auto; for `reason` it gives clear, logout, prompt_input_exit and other, and
# does not list resume. `detail` stores whatever the source reports verbatim, so a value
# outside these lists passes through rather than failing the event. The prompt event is
# the one exception: its field is not a reason but the event's own content, the prompt
# text, and the parser keeps it as SessionEvent.prompt (BUILD-PLAN F87).
EVENTS = {
    "SessionStart": (SESSION_START, "source"),
    "PreCompact": (COMPACTION, "trigger"),
    "SessionEnd": (SESSION_END, "reason"),
    "UserPromptSubmit": (PROMPT, "prompt"),
}


def parse_claude(hook_event: str, payload: dict, now: datetime) -> SessionEvent:
    """One Claude Code hook event and payload, as a SessionEvent."""
    kind, field = resolve_event(AGENT, hook_event, EVENTS)
    prompt = string_field(payload, field) if kind == PROMPT else None
    transcript = (
        optional_path_field(payload, "transcript_path")
        if kind == SESSION_START
        else path_field(payload, "transcript_path")
    )
    return SessionEvent(
        agent=AGENT,
        kind=kind,
        session_id=string_field(payload, "session_id"),
        cwd=path_field(payload, "cwd"),
        transcript_path=transcript,
        time=now,
        detail=None if kind == PROMPT else optional_string_field(payload, field),
        prompt=prompt,
    )
