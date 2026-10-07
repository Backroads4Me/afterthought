"""The filter: transcript entries in, evidence out (DESIGN 8, BUILD-PLAN 5 M2.3).

Four steps, and the order is the behavior:

1. drop the off-the-record spans (DESIGN 12), decided on each prompt's whole text;
2. keep the text the design keeps - the user's prompts and the assistant's answers, and from a
   tool entry the agent flagged as an error its head of 500 characters plus the lines
   below it that report something breaking or name a ``path:line``, or from any other tool
   entry only the lines that report something breaking or name a ``path:line``;
3. redact;
4. cap what is left at 1,500 characters on a word boundary.

Span-dropping reads an entry before anything else touches it, and the cap cuts text that has
already been redacted: a secret run that reaches the limit is cut from the marker, never from
the secret, so no fragment is left for the patterns to miss. Both steps are one pass over the
entries with a single emit point, so every path that returns kept text - a prompt, an answer,
extracted tool lines, the remnant of a cap - is redacted and capped the same way. There is no
path that hands back kept text and misses them.

A flagged head is redacted before its 500-character budget cuts it, so a cut can never
leave half a secret behind for the patterns to miss.

Two things the design asks of capture are not decided on one chunk of an entry stream.
Session exclusion needs the directory the stream came from, which is the queue's to know
(M2.2, M2.7). The two-prompt floor needs a session rather than a chunk of one, which is why
`prompt_count` counts over entries read from a session's start and the digest (M2.6) is the
one that applies it.

Whether an off-the-record span is open when a call's entries begin is a fact from outside
the stream as well, so `filter_entries` takes it and gives back the state at the end of
the call (M2.3). Capture delivers a session in chunks, and an unclosed span runs to the
end of the session rather than to the end of the chunk it happens to sit in (DESIGN 12),
so whatever carries a session's work from one chunk to the next - the queue, beside its
watermark (M2.2) - carries this one boolean too.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import replace

from aft.config import Config
from aft.readers import ASSISTANT, TOOL, USER, Entry
from aft.redact import compile_patterns, redact

# A kept entry is at most this many characters of its own text, plus the marker. The
# design caps "about 1,500 characters" (DESIGN 8); the number is the plan's (M2.3).
MAX_CHARS = 1500
CAP_MARKER = " [...]"

FILTERED_INJECTION = "[filtered injection attempt]"

_INJECTION_PATTERNS = (
    r"(?i)\bignore\s+(?:previous|all|above|prior)\s+instructions\b",
    r"(?i)\bsystem\s+prompt\s+override\b",
    r"(?i)\byou\s+are\s+now\s+(?:an?\s+unrestricted|in\s+developer\s+mode)\b",
    r"(?i)\bdisregard\s+(?:all|any|your)\s+(?:rules|instructions|guidelines)\b",
)
_INJECTION_COMPILED: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern) for pattern in _INJECTION_PATTERNS
)


def sanitize_text(text: str) -> str:
    """The text with the characters a reader never sees gone, then normalized.

    Format characters (Unicode category ``Cf``: zero-width spaces and joiners, the soft
    hyphen, bidirectional controls and isolates), Unicode tag characters
    (U+E0000..U+E007F, the block invisible instructions are smuggled through) and
    variation selectors (U+FE00..U+FE0F and U+E0100..U+E01EF) change what a model reads
    without changing what a person sees, so they are stripped before NFKC folds what is
    left to its canonical form.
    """
    if text.isascii():
        # Every character the scan below removes is outside ASCII, and NFKC leaves ASCII
        # alone, so a text with no non-ASCII character in it has nothing to be cleaned of.
        return text
    return unicodedata.normalize("NFKC", "".join(c for c in text if not _invisible(c)))


def _invisible(character: str) -> bool:
    """Whether one character is invisible to a reader and so not to be trusted."""
    code_point = ord(character)
    return (
        unicodedata.category(character) == "Cf"
        or 0xE0000 <= code_point <= 0xE007F
        or 0xFE00 <= code_point <= 0xFE0F
        or 0xE0100 <= code_point <= 0xE01EF
    )


def strip_injections(text: str) -> str:
    """Neutralize high-confidence injection override patterns in text."""
    text = sanitize_text(text)
    for pattern in _INJECTION_COMPILED:
        text = pattern.sub(FILTERED_INJECTION, text)
    return text


# A tool entry the agent flagged as an error keeps this many leading characters whatever
# is in them, so the head of a failure a run reported survives even when its lines match
# none of the patterns below (M2.3). The rest of the entry keeps only those lines.
FLAG_HEAD_CHARS = 500

# An off-the-record marker, matched only at the start of a prompt's text: discussing the
# markers never triggers them (DESIGN 12). The phrase is the one the design names, with
# its words spaced; a leading slash from a slash command, leading whitespace, and case
# do not matter, and trailing punctuation is fine because the lookahead asks only that
# the phrase ends here. A word joined on after it makes a different word instead ("aft offline",
# "off-the-record").
OFF_MARKER = re.compile(
    r"^\s*(?:/?aft off|off the record)(?![\w-])",
    re.IGNORECASE,
)
ON_MARKER = re.compile(
    r"^\s*(?:/?aft on|on the record)(?![\w-])",
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


def filter_entries(
    entries: Iterable[Entry],
    config: Config,
    off_record: bool = False,
) -> tuple[list[Entry], bool]:
    """The entries worth archiving, in the order they came, and whether a span outlives them.

    ``off_record`` says whether an off-the-record span was already open when ``entries``
    began; the second element says whether one is still open where they ended, which is
    what the next chunk of the same session starts with (M2.2, M2.3).

    Off-the-record spans go first, then the keep, then redaction, then the cap - see the
    module docstring for why that order is the behavior. Entries keep their id, time, role
    and error flag; only their text changes.

    The user's own redaction patterns are compiled here, before the first entry is looked at, so
    a pattern that cannot be applied is refused before anything has been dropped rather than
    halfway through a capture.
    """
    patterns = compile_patterns(config.settings.capture.redact_patterns)
    builtin = config.settings.capture.builtin_redaction
    outside, off_record = _outside_spans(entries, off_record)
    kept: list[Entry] = []
    for entry in outside:
        text = sanitize_text(entry.text)
        if entry.role != USER:
            text = strip_injections(text)
        entry = replace(entry, text=text)
        text = _kept_text(entry, patterns, builtin=builtin)
        if text is None:
            continue
        kept.append(replace(entry, text=_cap(redact(text, patterns, builtin=builtin))))
    return kept, off_record


def _outside_spans(entries: Iterable[Entry], off_record: bool) -> tuple[list[Entry], bool]:
    """The entries outside every off-the-record span, and the state those entries leave behind.

    A prompt beginning with an off marker starts a span and one beginning with an on
    marker ends it; both markers are dropped along with everything between them. Only a
    prompt carries a marker: what the assistant says is not the user speaking for themselves
    (DESIGN 12).

    The span's state arrives with the call and is handed back by it. A span belongs to the
    session rather than to the chunk it sits in (DESIGN 12, M2.3), so a marker on the last
    prompt of a chunk still holds over the next chunk of that session, and an entry of the
    first chunk of a call is already inside a span that an earlier chunk opened. The queue
    is what carries the answer from one chunk to the next (M2.2).

    Each marker is decided on the entry's whole text, before any cap: a phrase that only
    appears deeper in a prompt is prose, however far in it sits.
    """
    outside: list[Entry] = []
    for entry in entries:
        text = sanitize_text(entry.text)
        if entry.role == USER:
            if OFF_MARKER.match(text):
                off_record = True
                continue
            if ON_MARKER.match(text):
                off_record = False
                continue
        if not off_record:
            outside.append(entry)
    return outside, off_record


# DESIGN 8's floor: a session that never got past one prompt holds nothing a later session
# would thank it for, so it is asked of no model and kept in no archive. Two is the number.
MIN_SESSION_PROMPTS = 2


def prompt_count(entries: Iterable[Entry]) -> int:
    """How many user prompts a session's entries hold on the record.

    An off-the-record prompt is not counted, because the design drops that span whole: a
    session that said one thing on the record and three off it holds one prompt. A marker
    prompt is dropped by the same pass, and an answer is never a prompt.

    The caller gives a whole session rather than the chunk one job read, because the floor is
    about the session (DESIGN 8) and one chunk of a long conversation can carry a single
    exchange. A prompt's text is never dropped by the keep, the redaction or the cap, so this
    count is the number of user entries the filter would archive.
    """
    outside, _ = _outside_spans(entries, False)
    return sum(1 for entry in outside if entry.role == USER)


def _kept_text(
    entry: Entry, patterns: tuple[re.Pattern[str], ...], builtin: bool = True
) -> str | None:
    """The text to keep from one entry, or None to drop the entry.

    Prompts and answers keep their text whole. From a tool entry the agent flagged as an
    error the head of its output survives along with the lines below it worth keeping, so
    the failure the agent named is never filtered down to nothing; from an unflagged tool
    entry only those lines survive. A role the readers do not produce holds no
    conversation, and the design keeps a list rather than dropping by exception.
    """
    if entry.role in (USER, ASSISTANT):
        return entry.text
    if entry.role == TOOL:
        return (
            _flagged_output(entry.text, patterns, builtin=builtin)
            if entry.is_error
            else _tool_lines(entry.text)
        )
    return None


def _flagged_output(
    text: str, patterns: tuple[re.Pattern[str], ...], builtin: bool = True
) -> str | None:
    """The head of an output the agent flagged as an error, with the lines below it to keep.

    The agent's flag is its own verdict, so what it reported is evidence whatever its lines
    happen to look like: the first ``FLAG_HEAD_CHARS`` characters are kept, by that number,
    even when none of them matches a pattern (M2.3). Redaction runs before that budget cuts
    the text, so a secret straddling the cut is the marker there rather than a fragment of
    itself. Below the head the rule for an unflagged output still holds, so a long dump
    contributes its failure lines and its references rather than everything after the head.
    Head and lines then run through the same cap and redaction as every other kept text, so
    a flagged entry is bounded like any other.
    """
    redacted = redact(text, patterns, builtin=builtin)
    head = redacted[:FLAG_HEAD_CHARS]
    below = _tool_lines(redacted[FLAG_HEAD_CHARS:])
    if not head.strip():
        return below
    head = head + (CAP_MARKER if len(redacted) > FLAG_HEAD_CHARS else "")
    return "\n".join([head, below]) if below else head


def _tool_lines(text: str) -> str | None:
    """The lines of a tool output that survive the filter, or None when none of them do.

    A line earns its place by reporting a failure or naming a place in the code. Text with
    neither adds nothing a future session could not re-run, so an output that holds none of
    them answers None and the entry goes rather than becoming an empty one.
    """
    kept = [line for line in text.splitlines() if _reports_a_break(line) or PATH_LINE.search(line)]
    return "\n".join(kept) if kept else None


def _reports_a_break(line: str) -> bool:
    """Whether a line names an error, a failure, or a non-zero exit."""
    return any(pattern.search(line) for pattern in _ERROR)


def _cap(text: str) -> str:
    """``text`` within the character budget, cut on a word boundary with the marker."""
    end = _word_end(text, MAX_CHARS)
    return text if end == len(text) else text[:end] + CAP_MARKER


def _word_end(text: str, limit: int) -> int:
    """Where ``text`` ends once ``limit`` characters bound it on a word boundary.

    Text already within the budget ends at its own end. The last whitespace at or before
    the limit is the cut, so a word is never left half in and half out; text with no such
    whitespace is a single run and is cut at the limit.
    """
    if len(text) <= limit:
        return len(text)
    head = text[:limit]
    cut = max(head.rfind(space) for space in " \t\n\v\f\r")
    return limit if cut < 0 else len(head[:cut].rstrip())


# The turns pipeline (BUILD-PLAN F55) reads a compacted rendering of the same kept
# entries, built after the filter has run: a tool entry shows only a failed command's error
# lines, and the assistant's code, diffs and tables are named rather than read. Everything a
# model is shown is verbatim archived text, so a quote it takes from this rendering is still
# found in the evidence archive.
CODE_MARKER = "[code block]"
DIFF_MARKER = "[diff]"
TABLE_MARKER = "[table]"

# A fenced block opens with ``` or ~~~; inline backticks never match, because a fence
# starts the line.
_FENCE = re.compile(r"^(```|~~~)")
_TABLE_ROW = re.compile(r"^\|.*\|\s*$")
# The row that divides a table's header from its body: pipes, dashes and colons.
_TABLE_DIVIDER = re.compile(r"^\|?[\s:|-]*-[\s:|-]*$")
# One line of a diff: a git or unified diff header, or a hunk's own header. A run of
# two or more such lines is a diff; one line on its own is prose that happens to start
# with a dash.
_DIFF_LINE = re.compile(r"^(?:diff --git |@@ -|\+\+\+ |--- \S|Index: )")


def compact(entry: Entry) -> str:
    """One kept entry as the turns pipeline shows it to a model.

    A prompt or an answer keeps its words, an answer with its fenced blocks and tables
    named by a placeholder - the named text is never longer than what it stands for, so
    no extra cap is needed. A tool entry holds a command's output, never the call that made
    it, so it shows only what a failed command left: its kept error lines, and nothing at
    all for a command that did not fail.
    """
    if entry.role == TOOL:
        return _errors(entry)
    if entry.role == ASSISTANT:
        return _collapsed(entry.text)
    return entry.text


def _errors(entry: Entry) -> str:
    """A failed command's kept lines, or nothing for a command that did not fail."""
    if not entry.is_error:
        return ""
    return "\n".join(line.strip() for line in entry.text.splitlines() if line.strip())


def _collapsed(text: str) -> str:
    """An answer with its code, diffs and tables named by a placeholder."""
    out: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if _FENCE.match(line.lstrip()):
            block = [line]
            index += 1
            while index < len(lines) and not _FENCE.match(lines[index].lstrip()):
                block.append(lines[index])
                index += 1
            if index < len(lines):
                block.append(lines[index])  # the closing fence
                index += 1
            out.append(_block_marker(block))
            continue
        end = _table_run(lines, index)
        if end > index:
            out.append(TABLE_MARKER)
            index = end
            continue
        if _DIFF_LINE.match(line):
            end = index
            while end < len(lines) and _DIFF_LINE.match(lines[end]):
                end += 1
            if end - index >= 2:
                out.append(DIFF_MARKER)
                index = end
                continue
        out.append(line)
        index += 1
    return "\n".join(out)


def _table_run(lines: list[str], start: int) -> int:
    """Where a table that begins at ``start`` ends, or ``start`` when none does.

    A table is two or more consecutive lines that begin and end with a pipe, one of
    which divides its columns with dashes.
    """
    end = start
    divided = False
    while end < len(lines) and _TABLE_ROW.match(lines[end].strip()):
        divided = divided or bool(_TABLE_DIVIDER.match(lines[end].strip()))
        end += 1
    return end if divided and end - start >= 2 else start


def _block_marker(block: list[str]) -> str:
    """The name for one collapsed block: a diff when its own words say so."""
    info = block[0].strip()[3:].strip().lower()
    if "diff" in info or "patch" in info:
        return DIFF_MARKER
    if any(_DIFF_LINE.match(line) for line in block[1:]):
        return DIFF_MARKER
    return CODE_MARKER
