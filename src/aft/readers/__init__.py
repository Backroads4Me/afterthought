"""Transcript readers: one session file in, normalized entries out.

This is one of the two agent-specific seams of design section 5, the adapter
boundary. A reader knows its agent's file format and nothing else; everything
downstream of it (capture, the filter, the archive, the digest) sees only
`Entry` records, so adding an agent means adding one reader here.

Readers are deliberately forgiving. An entry type, a role or a field they do
not recognize is skipped rather than fatal, so an agent update that adds bookkeeping
to its transcript cannot break capture.

That forgiveness is per entry. `iter_entries` reports the two readings that are not
forgiveness but a changed format: an entry of the conversation being handed over that
this reader cannot build, and a file of text holding no record that names its type.
Everything else, a bookkeeping-only session included, is a session with nothing to say.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
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


class UnrecognizedTranscript(ReaderError):
    """A transcript in no format this reader can read: see ``iter_entries``."""


class UnreadableRecord(ReaderError):
    """A record a reader could not build entries from: what ``iter_entries`` reports."""


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

    Raises ``ReaderError`` for an agent with no reader, and ``UnrecognizedTranscript``
    for a transcript in no format that reader can read.
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
    """The entries after ``after``, yielded as they are read rather than collected, so
    what the watermark passes over is never held in memory.

    Streaming bounds memory, not parsing: reaching the watermark reads every record
    before it, and when no entry carries the watermark the transcript is read again from
    the start rather than buffered on the chance it might be needed.
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


def iter_entries(
    path: Path,
    of_record: Callable[[dict], Iterable[Entry]],
    kept: Callable[[dict], bool] | None = None,
) -> Iterator[Entry]:
    """The entries of a JSONL session file, in file order, for one agent's reader.

    ``of_record`` builds the entries one record carries and raises ``UnreadableRecord``
    for a conversation record whose shape it cannot build an entry from. ``kept`` names the
    records that make up the conversation being handed over; the rest are not read at all,
    so a record the walk dropped cannot void a read of the branch it kept. Capture cannot
    tell a changed format from a session with nothing to say, so the read is reported when
    a kept record cannot be built, and the file when it holds text yet no record names its
    type at all. Bookkeeping alone is an empty session, and so is every entry type skipped
    as unknown. (BUILD-PLAN 5, M2.1)
    """
    envelope = False
    broken = False
    for record in iter_records(path):
        if isinstance(record.get("type"), str):
            envelope = True
        if kept is not None and not kept(record):
            continue
        try:
            yield from of_record(record)
        except UnreadableRecord:
            broken = True
    if broken:
        raise UnrecognizedTranscript(
            f"{path}: a conversation record is not in a shape this reader can read"
        )
    if not envelope and _holds_text(path):
        raise UnrecognizedTranscript(f"{path}: no record names a type, so no transcript here")


def _holds_text(path: Path) -> bool:
    """Whether the file holds anything but whitespace."""
    with open(path, encoding="utf-8", errors="replace") as handle:
        return any(line.strip() for line in handle)


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
