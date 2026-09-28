"""Claude Code transcripts.

Claude Code keeps one JSONL file per session under
``~/.claude/projects/<project>/<session id>.jsonl``. The conversation is the
``user`` and ``assistant`` entries, each holding one API message; the rest of
the file is the CLI's own bookkeeping (``attachment``, ``system``, ``mode``,
``custom-title``, ``queue-operation`` and more), which is not conversation.

Two things about a Claude Code session are the reader's job to know: a tool
result comes back inside a ``user`` message rather than as its own entry, and
compaction adds an ``isCompactSummary`` message that restates the earlier
conversation instead of adding to it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from aft.readers import ASSISTANT, TOOL, USER, Entry, blocks_text, iter_records, parse_time


def read(path: Path) -> Iterator[Entry]:
    for record in iter_records(path):
        entry_id = record.get("uuid")
        message = record.get("message")
        kind = record.get("type")
        time = parse_time(record.get("timestamp"))
        if kind not in (USER, ASSISTANT) or not isinstance(entry_id, str) or time is None:
            continue
        # isMeta: the CLI's own prompt to the agent. isCompactSummary: the summary
        # written on compaction, whose content repeats entries already read.
        if not isinstance(message, dict) or record.get("isMeta") or record.get("isCompactSummary"):
            continue
        content = message.get("content")
        if kind == ASSISTANT:
            # A turn that only thought or called tools has no text of its own.
            text = blocks_text(content)
            if text:
                yield Entry(entry_id, time, ASSISTANT, text, False)
            continue
        text = blocks_text(content)
        if text:
            yield Entry(entry_id, time, USER, text, False)
        yield from _tool_results(entry_id, time, content)


def _tool_results(entry_id: str, time: datetime, content: object) -> Iterator[Entry]:
    """Split the tool results of one user message into entries of their own."""
    if not isinstance(content, list):
        return
    for position, block in enumerate(content):
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        # The user message holds the entry id; the result inside it has Claude's
        # own tool_use_id, which is what names this part of the entry.
        result_id = block.get("tool_use_id")
        yield Entry(
            f"{entry_id}#{result_id if isinstance(result_id, str) else position}",
            time,
            TOOL,
            blocks_text(block.get("content")),
            bool(block.get("is_error")),
        )
