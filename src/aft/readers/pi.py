"""pi transcripts.

pi keeps one JSONL file per session, by default under ``~/.pi/agent/sessions``, in
a directory named for the session's working directory with its separators folded
into dashes (``--home-dev-projects-garden--``) and called
``<timestamp>_<session id>.jsonl``. Its conversation is the ``message`` entries;
the other entry types are the session's own record of a model or thinking-level
change, a compaction, extension state, and extension-injected context.

Entries form a tree rather than a list: each carries an ``id`` and a
``parentId``, and returning to an earlier point with ``/tree`` appends a new
child instead of rewriting history. Only the branch that ends at the last entry
is the conversation, which is how pi itself rebuilds context by walking from the
leaf to the root.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from aft.readers import ASSISTANT, TOOL, USER, Entry, blocks_text, iter_records, parse_time

# pi's message roles, mapped to the roles of an Entry. A system message holds the
# prompt and the tool loadout rather than either side of a conversation, and the
# roles beyond these - pi's own summaries and bash runs, or one an extension
# added - have no Entry to become.
ROLES = {"user": USER, "assistant": ASSISTANT, "toolResult": TOOL}


def read(path: Path) -> Iterator[Entry]:
    active = _active_branch(path)
    for record in iter_records(path):
        entry_id = record.get("id")
        message = record.get("message")
        time = parse_time(record.get("timestamp"))
        if (
            record.get("type") != "message"
            or not isinstance(entry_id, str)
            or entry_id not in active
            or time is None
        ):
            continue
        if not isinstance(message, dict):
            continue
        role_name = message.get("role")
        role = ROLES.get(role_name) if isinstance(role_name, str) else None
        if role is None:
            continue
        text = blocks_text(message.get("content"))
        # A turn with no text of its own is not an entry: an assistant turn that only
        # thought or called tools, or a tool result that returned nothing.
        if not text:
            continue
        yield Entry(entry_id, time, role, text, bool(message.get("isError")))


def _active_branch(path: Path) -> set[str]:
    """The ids from the last entry back to the root: the branch pi reads back."""
    parents: dict[str, str | None] = {}
    last: str | None = None
    for record in iter_records(path):
        entry_id = record.get("id")
        if not isinstance(entry_id, str):
            continue
        parent = record.get("parentId")
        parents[entry_id] = parent if isinstance(parent, str) else None
        last = entry_id
    branch: set[str] = set()
    current = last
    while isinstance(current, str) and current in parents and current not in branch:
        branch.add(current)
        current = parents[current]
    return branch
