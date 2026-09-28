"""pi transcripts.

pi keeps one JSONL file per session under
``~/.pi/agent/sessions/<cwd>/<timestamp>_<session id>.jsonl``. Its conversation
is the ``message`` entries; the other entry types are the session's own record
of a model or thinking-level change, a compaction, extension state, and
extension-injected context.

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

# pi's own message roles, to the roles of an Entry. A system message carries the
# pi's message roles, as the roles of an Entry. A system message holds the prompt and
# the tool loadout, and a role an extension adds holds neither side of a conversation,
# so a role missing here has no Entry to become.
ROLES = {"user": USER, "assistant": ASSISTANT, "toolResult": TOOL}


def read(path: Path) -> Iterator[Entry]:
    records = list(iter_records(path))
    active = _active_branch(records)
    for record in records:
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
        # An assistant turn that only thought or called tools has no text of its
        # own; a tool result is worth an entry even when it returned nothing.
        if not text and role != TOOL:
            continue
        yield Entry(entry_id, time, role, text, bool(message.get("isError")))


def _active_branch(records: list[dict]) -> set[str]:
    """The ids from the last entry back to the root: the branch pi reads back."""
    by_id = {r["id"]: r for r in records if isinstance(r.get("id"), str)}
    current = next(
        (r["id"] for r in reversed(records) if isinstance(r.get("id"), str)),
        None,
    )
    branch: set[str] = set()
    while isinstance(current, str) and current in by_id and current not in branch:
        branch.add(current)
        parent = by_id[current].get("parentId")
        current = parent if isinstance(parent, str) else None
    return branch
