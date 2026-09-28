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

import logging
from collections.abc import Iterator
from pathlib import Path

from aft.readers import (
    ASSISTANT,
    TOOL,
    USER,
    Entry,
    UnreadableRecord,
    blocks_text,
    iter_entries,
    iter_records,
    parse_time,
)

# pi's message roles, mapped to the roles of an Entry. A system message holds the
# prompt and the tool loadout rather than either side of a conversation, and the
# roles beyond these - pi's own summaries and bash runs, or one an extension
# added - have no Entry to become.
ROLES = {"user": USER, "assistant": ASSISTANT, "toolResult": TOOL}

# The record type pi uses for one side of the conversation.
CONVERSATION = ("message",)

LOGGER = logging.getLogger(__name__)


def read(path: Path) -> Iterator[Entry]:
    """The conversation: the entries of the file that sit on the active branch."""
    active = _active_branch(path)

    def kept(record: dict) -> bool:
        """Every record but one the walk placed on a branch it dropped."""
        entry_id = record.get("id")
        return not isinstance(entry_id, str) or entry_id in active

    return iter_entries(path, _record_entries, kept)


def _record_entries(record: dict) -> Iterator[Entry]:
    """The entry one message record carries, or none.

    Raises ``UnreadableRecord`` for a message record this reader cannot build an entry
    from. A role it does not know is an entry type it skips, not a changed format.
    """
    if record.get("type") not in CONVERSATION:
        return
    entry_id = record.get("id")
    time = parse_time(record.get("timestamp"))
    message = record.get("message")
    if not isinstance(entry_id, str) or time is None or not isinstance(message, dict):
        raise UnreadableRecord
    role_name = message.get("role")
    if not isinstance(role_name, str):
        raise UnreadableRecord
    role = ROLES.get(role_name)
    if role is None:
        return
    content = message.get("content")
    if not isinstance(content, (str, list)):
        raise UnreadableRecord
    text = blocks_text(content)
    # A turn with no text of its own is not an entry: an assistant turn that only
    # thought or called tools, or a tool result that returned nothing.
    if not text:
        return
    yield Entry(entry_id, time, role, text, bool(message.get("isError")))


def _active_branch(path: Path) -> set[str]:
    """The ids from the last entry back to the root: the branch pi reads back.

    A parent that is null or absent is a root, and the walk ends there quietly. That
    is how pi starts a branch when ``/tree`` moves the conversation to the root, and
    nothing is lost by stopping at one. A parent naming an id no record in the file
    carries is a break: the records it would have led to are lost ancestors or an
    abandoned branch, and the reader cannot tell which, so it does not guess past
    them. It ends the branch at the break and reports what the rest of the file
    holds. (BUILD-PLAN 5, M2.1)
    """
    parents: dict[str, str | None] = {}
    message_ids: list[object] = []
    last: str | None = None
    for record in iter_records(path):
        entry_id = record.get("id")
        if record.get("type") in CONVERSATION:
            message_ids.append(entry_id)
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
    if isinstance(current, str) and current not in parents:
        off_branch = sum(
            1 for entry_id in message_ids if not isinstance(entry_id, str) or entry_id not in branch
        )
        LOGGER.warning(
            "%s: no record with id %s: the active branch ends there and %d message "
            "records of the file fall off it",
            path,
            current,
            off_branch,
        )
    return branch
