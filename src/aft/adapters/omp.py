"""omp hook payloads to normalized events.

omp (Oh My Pi, 18.16) is a fork of pi with its own package scope and its own configuration
root, ``~/.omp/agent``. Its lifecycle events reach the outside world the way pi's do: a hook
module (``integrations/omp/afterthought.ts``, installed by ``aft install omp``) writes the
payload for the event it handles to the stdin of ``aft hook omp <event>``. The event names and
the payload fields are pi's, which is why this module reads pi's own event table rather than
keeping a second copy of it; the fork's event surface is the same surface.

omp is a sibling rather than an alias because the agent name is carried past the parse: it
chooses the transcript reader and the session root (``aft.readers``, ``aft.capture``) and it
names the digest a session leaves behind. An omp session filed as pi would be read from pi's
session directory and remembered under pi's name.

One shape difference from pi is pinned in the fixtures beside this module: pi's session events
carry a ``reason`` saying what set them off, and omp's carry none. ``SessionStartEvent``,
``SessionCompactEvent`` and ``SessionShutdownEvent`` hold their type alone, and
``SessionBeforeCompactEvent`` holds the compaction's own preparation rather than a reason
(omp's extensibility/shared-events.d.ts). So an omp session event arrives with no detail, which
is what the field being absent already means to the parser.

``session_id``, ``cwd`` and ``transcript_path`` come from omp's session manager, whose
``getSessionFile()`` is undefined until the session is first written - an ephemeral
``--no-session`` run never writes one - so a payload can leave the transcript out or report it
empty; either way the event still stands.
"""

from __future__ import annotations

from datetime import datetime

from aft.adapters.pi import EVENTS
from aft.events import (
    PROMPT,
    SessionEvent,
    optional_path_field,
    optional_string_field,
    path_field,
    resolve_event,
    string_field,
)

AGENT = "omp"


def parse_omp(hook_event: str, payload: dict, now: datetime) -> SessionEvent:
    """One omp event and payload, as a SessionEvent."""
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
