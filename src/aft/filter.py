"""The filter: transcript entries in, evidence out (DESIGN 8, BUILD-PLAN 5 M2.3).

Four steps, and the order is the behavior:

1. drop the off-the-record spans (DESIGN 12), decided on each prompt's whole text;
2. keep the text the design keeps - Ted's prompts and the assistant's answers, and from a
   tool entry only the lines that report something breaking or name a ``path:line``;
3. cap what is left at 1,500 characters on a word boundary;
4. redact.

Span-dropping reads an entry before the cap has cut it, and redaction runs on the text
that is actually going out. Both are one pass over the entries with a single emit point,
so every path that returns kept text - a prompt, an answer, extracted tool lines, the
remnant of a cap - is capped and redacted the same way. There is no path that hands back
kept text and misses them.

Two things the design asks of capture are not here because an entry stream cannot answer
them: session exclusions and the two-prompt floor both need the session a stream came
from, which is the queue's and the digest's to know (M2.2, M2.6, M2.7).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import replace

from aft.config import Config
from aft.readers import ASSISTANT, TOOL, USER, Entry
from aft.redact import compile_patterns, redact

# A kept entry is at most this many characters of its own text, plus the marker. The
# design caps "about 1,500 characters" (DESIGN 8); the number is the plan's (M2.3).
MAX_CHARS = 1500
CAP_MARKER = " [...]"

# An off-the-record marker, matched only at the start of a prompt's text: discussing the
# An off-the-record marker, matched only at the start of a prompt's text: discussing the
# markers never triggers them (DESIGN 12). The phrase is the one the design names, with
# its words spaced; leading whitespace and case do not matter, and trailing punctuation
# is fine because the lookahead asks only that the phrase ends here. A word joined on
# after it makes a different word instead ("aft offline", "off-the-record").
OFF_MARKER = re.compile(
    r"^\s*(?:aft off|off the record)(?![\w-])",
    re.IGNORECASE,
)
ON_MARKER = re.compile(
    r"^\s*(?:aft on|on the record)(?![\w-])",
    re.IGNORECASE,
)

# The lines to keep from a tool's output (DESIGN 8: error and failure lines, and
# file:line references). Case-insensitive, so the plan's FAILED - what pytest prints -
# and its failed are the one pattern.
_ERROR_SOURCES: tuple[str, ...] = (
    # error, errors, and the error class a traceback names: OSError, ValueError.
    r"\w*error\b",
    # failed, fails, failure, and pytest's FAILED column.
    r"\w*fail\w*",
    r"\btraceback\b",
    # exception and the Exception of a Python class name.
    r"\w*exception\w*",
    # A non-zero exit: "exit code 1", "exited with status 2", "non-zero exit status 7".
    # Zero exits well and is not a line worth keeping.
    r"\bexit(?:ed|s)?[ _-]?(?:code|status)[ _-]*[=:]?\s*[1-9]\d*\b",
    r"\bexit(?:ed|s)?\s+(?:with\s+(?:an?\s+)?(?:exit\s+)?(?:code|status)\s+)?[1-9]\d*\b",
    r"non-?zero\s+exit",
)

_ERROR: tuple[re.Pattern[str], ...] = tuple(
    re.compile(source, re.IGNORECASE) for source in _ERROR_SOURCES
)

# A source reference: a path, then :line and the :column some tools add. A name that carries
# an extension has to have one starting with a letter, so an address with a port
# (10.0.0.1:8080) is not read as a file. A name without an extension is a path only when
# something says it is one: it sits below a directory (src/aft/cli:5) or it is spelled the
# way such files are (Makefile:12). A reference starts at a word boundary and not just
# after a ]: the text redaction leaves behind is not evidence of a place, so a path whose
# name was a credential leaves no half of a reference to point at.
PATH_LINE = re.compile(
    r"(?<![\w./\]-])/?"  # not a word's tail; an absolute path starts with its slash
    r"(?:[\w.+@~+-]+/)*"
    r"(?:"
    r"[\w.+@~+-]*[.][A-Za-z]\w{0,7}"  # a file named for its extension
    r"|(?:(?<=/)|[A-Z])[\w+@~+-]*[A-Za-z][\w+@~+-]*"  # a name without one, placed or capitalized
    r")"
    r"(?::\d+)+"
    r"(?![\w-])"
)


def filter_entries(entries: Iterable[Entry], config: Config) -> list[Entry]:
    """The entries worth archiving, in the order they came.

    Off-the-record spans go first, then the keep, then the cap, then redaction - see the
    module docstring for why that order is the behavior. Entries keep their id, time, role
    and error flag; only their text changes.

    Ted's own redaction patterns are compiled here, before the first entry is looked at, so
    a pattern that cannot be applied is refused before anything has been dropped rather than
    halfway through a capture.
    """
    patterns = compile_patterns(config.settings.capture.redact_patterns)
    kept: list[Entry] = []
    for entry in _outside_spans(entries):
        text = _kept_text(entry)
        if text is None:
            continue
        kept.append(replace(entry, text=redact(_cap(text), patterns)))
    return kept


def _outside_spans(entries: Iterable[Entry]) -> Iterator[Entry]:
    """The entries outside every off-the-record span, the markers among them (DESIGN 12).

    A prompt beginning with an off marker starts a span and one beginning with an on
    marker ends it; both markers are dropped along with everything between them, and a
    span left open runs to the end of the entries this call was given. Only a prompt
    carries a marker: what the assistant says is not Ted speaking for himself.

    A span is coextensive with the entries handed to this one call. Whether one is open is
    state this generator holds for its own run and does not hand back, so a marker on the
    last prompt of a batch does not reach the next batch of the same session, and the pair
    typed in a later run opens no span.

    Each marker is decided on the entry's whole text, before any cap: a phrase that only
    appears deeper in a prompt is prose, however far in it sits.
    """
    off = False
    for entry in entries:
        if entry.role == USER:
            if OFF_MARKER.match(entry.text):
                off = True
                continue
            if ON_MARKER.match(entry.text):
                off = False
                continue
        if not off:
            yield entry


def _kept_text(entry: Entry) -> str | None:
    """The text to keep from one entry, or None to drop the entry.

    Prompts and answers keep their text whole; a tool entry keeps only the lines that say
    something broke or point at a place in the code. A role the readers do not produce
    holds no conversation, and the design keeps a list rather than dropping by exception.
    """
    if entry.role in (USER, ASSISTANT):
        return entry.text
    if entry.role == TOOL:
        return _tool_lines(entry.text)
    return None


def _tool_lines(text: str) -> str | None:
    """The lines of a tool's output that survive the filter, or None when none of them do.

    An output with nothing to say about a failure and no place to point at adds nothing a
    future session could not re-run, so the entry goes rather than becoming an empty one.
    """
    kept = [line for line in text.splitlines() if _reports_a_break(line) or PATH_LINE.search(line)]
    return "\n".join(kept) if kept else None


def _reports_a_break(line: str) -> bool:
    """Whether a line names an error, a failure, or a non-zero exit."""
    return any(pattern.search(line) for pattern in _ERROR)


def _cap(text: str) -> str:
    """``text`` within the character budget, cut on a word boundary with the marker.

    The last whitespace at or before the limit is the cut, so a word is never left half in
    and half out; text with no such whitespace is a single run and is cut at the limit.
    """
    if len(text) <= MAX_CHARS:
        return text
    head = text[:MAX_CHARS]
    cut = max(head.rfind(space) for space in " \t\n\v\f\r")
    return (head if cut < 0 else head[:cut].rstrip()) + CAP_MARKER
