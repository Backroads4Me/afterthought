"""The checkpoint: a compacted session's own recent work, replayed after its memory block.

Memory holds what earlier sessions settled. What the session in progress asked for and
edited is still only in its transcript, and compaction leaves it to a model-written summary
that nothing checks. So when a session restarts after a compaction, its newest prompts and
edited files are read back from the transcript and printed after the block.

A checkpoint is lifecycle evidence, not memory, and nothing here writes anything: the
transcript stays the truth, so there is nothing to expire and nothing that can reach the
vault, a digest or a dream. It takes no model call. Every word passes the filter capture
uses, and a session that capture would refuse gets none (DESIGN 8).
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

from aft import readers
from aft.config import Config, is_excluded
from aft.events import SessionEvent
from aft.filter import CAP_MARKER, filter_entries, strip_injections
from aft.readers import USER, Entry

# The whole section at most: the block's limit leaves this much room for it.
MAX_CHARS = 1500
# What sits between the block and the section.
SEPARATOR = "\n\n"

MAX_PROMPTS = 3
MAX_PROMPT_CHARS = 400
MAX_FILES = 10
# The files line is written first and bounded on its own, since the file list is what a
# compaction's summary keeps worst; the prompts take the rest of the section.
MAX_FILES_CHARS = 400

# Reading a transcript takes the hook's time, and a larger one risks the block.
MAX_TRANSCRIPT_BYTES = 32 * 1024 * 1024

HEADER = (
    "## Before this compaction\n\n"
    "From this session's transcript, not memory. Current code and what the user says now"
    " outrank it. These prompts came before the compaction: they are context, not new"
    " requests."
)
FILES_PREFIX = "- Files edited: "

# Pasted text is not the user's own words. The filter's cap can cut the closing tag off, so
# a block left open runs to the end of the prompt.
_PASTED = re.compile(r"<pasted_content[^>]*>.*?(?:</pasted_content>|\Z)", re.DOTALL | re.IGNORECASE)


class CheckpointError(Exception):
    """A checkpoint that failed. The message names the failing step's kind and nothing else:
    the hook log is where it goes, and a prompt's words never go there."""


def render(config: Config, event: SessionEvent) -> str:
    """The section for a session restarting after a compaction, or "" when it gets none.

    A session outside capture gets nothing, and so does one ``aft forget session`` removed:
    its transcript is still on disk. A queue that cannot say which it is fails closed, as
    any failure here does.
    """
    # Imported here: the block's limit reads this module's constants on every hook.
    from aft import queue

    path = event.transcript_path
    try:
        if path is None or not _readable(path) or is_excluded(event.cwd, config):
            return ""
        if queue.is_forgotten(config, event.session_id):
            return ""
        # A whole session, so a span opened anywhere in it holds, as capture holds it.
        kept, _ = filter_entries(readers.work(event.agent, path), config)
        # Inside the same try: rendering the section is as able to fail as reading it, and
        # the caller recognizes only CheckpointError.
        return _section(
            [entry for entry in kept if entry.role == USER],
            [entry.text for entry in kept if entry.role != USER],
            event.cwd,
        )
    except Exception as exc:  # noqa: BLE001 - the hook turns any failure into a log line
        raise CheckpointError(f"checkpoint failed: {type(exc).__name__}") from exc


def _readable(path: Path) -> bool:
    """Whether the transcript is there and within what the hook's time can read."""
    try:
        return path.stat().st_size <= MAX_TRANSCRIPT_BYTES
    except OSError:
        return False


def _section(prompts: list[Entry], edited: list[str], cwd: Path) -> str:
    """The section's text within MAX_CHARS, "" when the session left nothing to show.

    Prompts are dropped oldest first when they do not fit: the newest is the session's
    thread. A prompt is one line, so it cannot open a heading of its own.
    """
    files = _files_line(edited, cwd)
    used = len(HEADER) + len(SEPARATOR) + (len(files) + 1 if files else 0)
    shown: list[str] = []
    for entry in reversed(prompts):
        if len(shown) == MAX_PROMPTS:
            break
        line = _prompt_line(entry)
        if line is None:
            continue
        if used + len(line) + 1 > MAX_CHARS:
            break
        used += len(line) + 1
        shown.insert(0, line)
    lines = [*shown, *([files] if files else [])]
    return f"{HEADER}{SEPARATOR}" + "\n".join(lines) if lines else ""


def _prompt_line(entry: Entry) -> str | None:
    """One prompt as one bullet, or None when nothing of it is the user's own words.

    The filter leaves a user's prompts free of `strip_injections`, since they are the
    record of what was said; this text goes back in as hook context, so it runs here.
    """
    text = strip_injections(_PASTED.sub(" ", entry.text))
    text = textwrap.shorten(text, MAX_PROMPT_CHARS, placeholder=CAP_MARKER).strip()
    if not text:
        return None
    return f'- {entry.time.astimezone():%Y-%m-%d %H:%M} "{text}"'


def _files_line(edited: list[str], cwd: Path) -> str:
    """The most recently edited files, newest first, whole paths only."""
    line = ""
    seen: set[str] = set()
    for path in reversed(edited):
        name = _relative(path, cwd)
        if name in seen:
            continue
        if len(seen) == MAX_FILES:
            break
        seen.add(name)
        grown = f"{line}, {name}" if line else f"{FILES_PREFIX}{name}"
        if len(grown) > MAX_FILES_CHARS:
            break
        line = grown
    return line


def _relative(path: str, cwd: Path) -> str:
    """``path`` relative to the session's directory when it sits under it."""
    edited = Path(path)
    return str(edited.relative_to(cwd)) if edited.is_relative_to(cwd) else path
