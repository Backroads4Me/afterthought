"""Claude Code transcripts.

Claude Code keeps one JSONL file per session under
``~/.claude/projects/<project>/<session id>.jsonl``. The conversation is the
``user`` and ``assistant`` entries, each holding one API message; the rest of
the file is the CLI's own bookkeeping (``attachment``, ``system``, ``mode``,
``custom-title``, ``queue-operation`` and more), which is not conversation.

Three things about a Claude Code session are the reader's job to know: a tool
result comes back inside a ``user`` message rather than as its own entry,
compaction adds an ``isCompactSummary`` message that restates the earlier
conversation instead of adding to it, and the CLI writes its own failures and
non-answers as assistant entries, recognizable only by their markers.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from aft.readers import ASSISTANT, TOOL, USER, Entry, blocks_text, iter_entries, parse_time

# The model Claude Code records on the assistant entries it writes itself.
SYNTHETIC_MODEL = "<synthetic>"

# The record types Claude Code uses for one side of the conversation.
CONVERSATION = (USER, ASSISTANT)


def read(path: Path) -> Iterator[Entry]:
    """The session's entries: what the records of the file hold, in file order."""
    return iter_entries(path, _record_entries, _unreadable)


def _parts(record: dict) -> tuple[str, datetime, dict] | None:
    """The id, time and message of a readable conversation record, or None."""
    entry_id = record.get("uuid")
    time = parse_time(record.get("timestamp"))
    message = record.get("message")
    if not isinstance(entry_id, str) or time is None or not isinstance(message, dict):
        return None
    return entry_id, time, message


def _unreadable(record: dict) -> bool:
    """True for a conversation record whose parts this reader cannot read."""
    return record.get("type") in CONVERSATION and _parts(record) is None


def _record_entries(record: dict) -> Iterator[Entry]:
    """The entries one record carries: its message, and any tool result inside it."""
    kind = record.get("type")
    if kind not in CONVERSATION:
        return
    parts = _parts(record)
    if parts is None:
        return
    entry_id, time, message = parts
    # isMeta: the CLI's own prompt to the agent. isCompactSummary: the summary
    # written on compaction, whose content repeats entries already read.
    if record.get("isMeta") or record.get("isCompactSummary"):
        return
    content = message.get("content")
    if kind == ASSISTANT:
        # A notice the CLI wrote stands for infrastructure, not for an answer, so
        # the marker decides it and the wording never does: a digest would quote
        # that wording as though the assistant had said it. (BUILD-PLAN 5, M2.1)
        if message.get("model") == SYNTHETIC_MODEL or record.get("isApiErrorMessage"):
            return
        # A turn that only thought or called tools has no text of its own.
        text = blocks_text(content)
        if text:
            yield Entry(entry_id, time, ASSISTANT, text, False)
        return
    text = blocks_text(content)
    if text:
        yield Entry(entry_id, time, USER, text, False)
    yield from _tool_results(entry_id, time, content)


def _tool_results(entry_id: str, time: datetime, content: object) -> Iterator[Entry]:
    """Split the tool results of one user message into entries of their own."""
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        text = blocks_text(block.get("content"))
        # A result that returned no text is not an entry, whatever it carried.
        if not text:
            continue
        # The user message holds the entry id; the result inside it has Claude's
        # own tool_use_id, which is what names this part of the entry. A block with
        # no tool call to name holds no entry.
        result_id = block.get("tool_use_id")
        if not isinstance(result_id, str):
            continue
        yield Entry(f"{entry_id}#{result_id}", time, TOOL, text, bool(block.get("is_error")))
