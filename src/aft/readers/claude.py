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

import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from aft.readers import (
    ASSISTANT,
    TOOL,
    USER,
    Entry,
    UnreadableRecord,
    blocks_text,
    iter_entries,
    parse_time,
)

# The model Claude Code records on the assistant entries it writes itself.
SYNTHETIC_MODEL = "<synthetic>"

# The record types Claude Code uses for one side of the conversation.
CONVERSATION = (USER, ASSISTANT)

# A slash command recorded by Claude Code carries its command name, message, and arguments
# in XML-like tags (BUILD-PLAN F45). The reader renders the command as typed (/<name> <args>),
# keeping text typed alongside it and leaving ordinary prompts byte-identical.
_COMMAND_TAG = r"<command-(?:name|message|args)>[\s\S]*?</command-(?:name|message|args)>"
_COMMAND_BLOCK = re.compile(rf"{_COMMAND_TAG}(?:\s*{_COMMAND_TAG})*", re.IGNORECASE)


def _render_slash_command(text: str) -> str:
    """Render a slash-command prompt as the text typed: /<name> <args>."""
    if "<command-name>" not in text:
        return text

    def _replace(match: re.Match[str]) -> str:
        block = match.group(0)
        name_match = re.search(r"<command-name>([\s\S]*?)</command-name>", block)
        if not name_match:
            return block
        name = name_match.group(1).strip().lstrip("/")
        if not name:
            return block
        args_match = re.search(r"<command-args>([\s\S]*?)</command-args>", block)
        args = args_match.group(1).strip() if args_match else ""
        return f"/{name} {args}" if args else f"/{name}"

    return _COMMAND_BLOCK.sub(_replace, text)


def read(path: Path) -> Iterator[Entry]:
    """The session's entries: what the records of the file hold, in file order."""
    return iter_entries(path, _record_entries)


def _record_entries(record: dict) -> Iterator[Entry]:
    """The entries one record carries: its message, and any tool result inside it.

    Raises ``UnreadableRecord`` for a user or assistant record this reader cannot build
    an entry from. A record it can read and that holds nothing - a prompt meant for the
    agent, the summary of a compaction, a turn that only thought, a result that returned
    no text - yields nothing.
    """
    kind = record.get("type")
    if kind not in CONVERSATION:
        return
    # isSidechain: a subagent's conversation, whose user messages are the main agent's prompts.
    if record.get("isSidechain"):
        return
    entry_id = record.get("uuid")
    time = parse_time(record.get("timestamp"))
    message = record.get("message")
    if not isinstance(entry_id, str) or time is None or not isinstance(message, dict):
        raise UnreadableRecord
    # isMeta: the CLI's own prompt to the agent. isCompactSummary: the summary
    # written on compaction, whose content repeats entries already read.
    if record.get("isMeta") or record.get("isCompactSummary"):
        return
    # A notice the CLI wrote on an assistant entry stands for infrastructure, not for an
    # answer, so the marker decides it and the wording never does: a digest would quote
    # that wording as though the assistant had said it. (BUILD-PLAN 5, M2.1)
    if kind == ASSISTANT and (
        message.get("model") == SYNTHETIC_MODEL or record.get("isApiErrorMessage")
    ):
        return
    content = message.get("content")
    if not isinstance(content, (str, list)):
        raise UnreadableRecord
    # A turn that only thought or called tools has no text of its own.
    text = blocks_text(content)
    if text:
        if kind == USER:
            text = _render_slash_command(text)
        yield Entry(entry_id, time, kind, text, False)
    if kind == USER:
        yield from _tool_results(entry_id, time, content)


def _tool_results(entry_id: str, time: datetime, content: object) -> Iterator[Entry]:
    """Split the tool results of one user message into entries of their own."""
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        # The API leaves content off a result that returned nothing, and a result with
        # no text is not an entry, whatever else it carried.
        result = block.get("content")
        if result is None:
            continue
        if not isinstance(result, (str, list)):
            raise UnreadableRecord
        text = blocks_text(result)
        if not text:
            continue
        # The user message holds the entry id; the result inside it has Claude's own
        # tool_use_id, which is what names this part of the entry.
        result_id = block.get("tool_use_id")
        if not isinstance(result_id, str):
            raise UnreadableRecord
        yield Entry(f"{entry_id}#{result_id}", time, TOOL, text, bool(block.get("is_error")))
