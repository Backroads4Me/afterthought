"""Transcript readers: one session file in, normalized entries out.

This is one of the two agent-specific seams of design section 5, the adapter
boundary. A reader knows its agent's file format and nothing else; everything
downstream of it (capture, the filter, the archive, the digest) sees only
`Entry` records, so adding an agent means adding one reader here.

Readers are deliberately forgiving. An entry type, a role or a field they do
not recognize is skipped rather than fatal, so an agent update that adds bookkeeping
to its transcript cannot break capture.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

CLAUDE = "claude"
PI = "pi"

USER = "user"
ASSISTANT = "assistant"
TOOL = "tool"


class ReaderError(Exception):
    pass


@dataclass(frozen=True)
class Entry:
    # The agent's own entry id when the transcript carries one; for a tool result
    # split out of a user message, that message's id joined with the tool call's
    # id, so the same result keeps one id within the session.
    id: str
    time: datetime
    role: str  # "user" | "assistant" | "tool"
    text: str  # user prompt, assistant final text, or tool output
    is_error: bool  # tool output flagged as an error by the agent


def read(agent: str, path: Path, after: str | None = None) -> Iterator[Entry]:
    """Yield the session's entries in file order, starting after entry ``after``.

    ``after`` is the watermark of a session: the id of its last already-read
    entry. An id no entry in the file carries yields the whole session, since a
    watermark the file does not have calls for re-reading it, not for skipping it.
    """
    # The readers build the Entry defined above, so they import this module:
    # importing them here keeps the package importable in either order.
    if agent == CLAUDE:
        from aft.readers.claude import read as read_session
    elif agent == PI:
        from aft.readers.pi import read as read_session
    else:
        raise ReaderError(f"no transcript reader for agent {agent!r}")
    return _after(lambda: read_session(path), after)


def _after(reopen: Callable[[], Iterator[Entry]], after: str | None) -> Iterator[Entry]:
    """The entries after ``after``, streaming so a long session costs its tail only.

    Walking to the watermark drops what it passes over; when no entry carries it
    the transcript is read again from the start rather than buffered on the chance
    it might be needed.
    """
    entries = reopen()
    if after is None:
        yield from entries
        return
    for entry in entries:
        if entry.id == after:
            yield from entries
            return
    yield from reopen()


def iter_records(path: Path) -> Iterator[dict]:
    """The JSON objects of a JSONL session file, in file order.

    A line that is not one is skipped: an agent killed mid-write leaves a
    truncated last line, and capture should read what is there.
    """
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record


def parse_time(value: object) -> datetime | None:
    """An ISO 8601 transcript timestamp as an aware UTC datetime, or None."""
    if not isinstance(value, str):
        return None
    try:
        time = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return time if time.tzinfo else time.replace(tzinfo=UTC)


def blocks_text(content: object) -> str:
    """The text of a message content, which agents store as a string or as blocks.

    Blocks of any other kind (thinking, tool calls, images) hold no text and are
    left out; paragraphs are joined with a blank line.
    """
    if isinstance(content, str):
        parts: list[object] = [content]
    elif isinstance(content, list):
        parts = [
            block.get("text")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
    else:
        return ""
    return "\n\n".join(part.strip() for part in parts if isinstance(part, str) and part.strip())
