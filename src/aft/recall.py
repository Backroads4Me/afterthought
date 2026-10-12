"""Recall on each prompt: the memory lines that bear on the prompt just typed.

An agent reads the block a session starts with and rarely runs ``aft search`` itself, so
each prompt brings back the few memory lines that bear on it, beside the prompt
(BUILD-PLAN F87). Recall is read-only: it reads the vault, the registry and the search
index and returns at most ``recall.max_hits`` one-line hits, and the prompt's text is
never written anywhere - no log, no archive, no index, no queue. A failure is logged as
other hook failures are, without the prompt: ``RecallError`` carries only the failing
step's kind, since a search error's own words can quote the query.
"""

from __future__ import annotations

import re
import string

from aft import inject, keys, search, vault
from aft.config import Config
from aft.gitutil import GitError
from aft.registry import Registry
from aft.search import FILLER_WORDS, Hit

#: The heading every recall block opens with, exactly as the plan states it (BUILD-PLAN
#: F87). Factual on purpose: the lines come from search, and search can be wrong.
HEADING = "Memory that may bear on this prompt (from aft search; check before relying on it):"

#: A prompt left with fewer than this many content words ("yes", "do it") recalls nothing.
MIN_QUERY_WORDS = 3

#: The query is the prompt's first this many content words (BUILD-PLAN F87).
MAX_QUERY_WORDS = 32

#: A hit stays only when its matched row holds at least this many of the query's content
#: words, or all of them when the query is shorter (BUILD-PLAN F87). Since a query with
#: fewer than MIN_QUERY_WORDS words recalls nothing, this is 3 whenever anything is asked.
BAR_WORDS = 3

#: An alphabetic query word this long or longer matches a row word sharing its first this
#: many letters, so "deploying" counts for "deploy" as the index's stemmer counts it.
STEM_CHARS = 5

#: A start-block line shorter than this is not matched inside a longer row: a short line
#: ("none", a project's one-word title) would otherwise hide any row that contains it.
MIN_HELD_CHARS = 40

#: How wide a candidate pool recall asks the search for, as a multiple of max_hits: the
#: bar and the start-block dedupe both drop hits, so the search is asked for more than
#: the block will show.
POOL_FACTOR = 4

#: Claude Code marks text pasted into a prompt with this element. It is not part of what
#: the prompt asks, so it leaves the query (BUILD-PLAN F87).
_PASTED = re.compile(r"<pasted_content[^>]*>.*?</pasted_content>", re.DOTALL | re.IGNORECASE)

#: A fence opens at a line of three or more backticks or tildes and closes at the same
#: marks alone, the way `aft.index` reads a page's fences.
_FENCE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})(.*)$")

#: The capture markers set what afterthought keeps; they are not a request to search for
#: (BUILD-PLAN F87, inject.RECORD_MARKERS). Longer alternations first, so "off the
#: record" is not left holding "on the record".
_MARKERS = re.compile(r"\b(aft off|aft on|off the record|on the record)\b", re.IGNORECASE)


class RecallError(Exception):
    """A recall that failed.

    The message names the failing step's kind and nothing else: an exception's own words
    can quote the query - an FTS5 error echoes the MATCH expression - and the prompt is
    never written anywhere, the hook log included (BUILD-PLAN F87).
    """


def query_words(prompt: str) -> list[str]:
    """The query recall searches for: the prompt's first 32 content words.

    A ``<pasted_content>`` block, fenced code and the capture markers say nothing about
    what the prompt asks, so they leave first. Each word loses the punctuation around it
    ("search?" is "search"), so the bar can find it in a row; F81's filler list then drops
    the question words, and the first MAX_QUERY_WORDS of what is left are the query.
    """
    text = _PASTED.sub(" ", prompt)
    text = _MARKERS.sub(" ", text)
    words: list[str] = []
    fence = ""
    for line in text.split("\n"):
        marker = _FENCE.match(line)
        if marker is not None:
            marks, rest = marker.group(1), marker.group(2)
            if not fence:
                fence = marks
                continue
            if marks[0] == fence[0] and len(marks) >= len(fence) and not rest.strip():
                fence = ""
                continue
        if fence:
            continue
        words.extend(token.strip(string.punctuation) for token in line.split())
    return [word for word in words if word and word.lower() not in FILLER_WORDS][:MAX_QUERY_WORDS]


def recall(prompt: str | None, cwd, config: Config) -> str:
    """The block to add for one submitted prompt, or "" when nothing bears on it.

    Reads memory only: the vault, the registry and the search index answer it, and
    nothing is written anywhere (BUILD-PLAN F87). The search runs as ``aft search`` does
    from ``cwd``, BM25 only unless ``recall.rerank`` says otherwise, and is asked for a
    pool wider than the block will show because the bar and the start-block dedupe both
    drop hits. ``recall.enabled = false`` gives nothing, as does a prompt left with
    fewer than three content words, and so does a vault that is not initialized: a
    session that has no memory to give is not a failure and logs nothing.
    """
    settings = config.settings.recall
    if not settings.enabled or not prompt:
        return ""
    if not vault.is_initialized(config.vault):
        return ""
    words = query_words(prompt)
    if len(words) < MIN_QUERY_WORDS:
        return ""
    try:
        return _recall(words, cwd, config, settings)
    except Exception as exc:  # noqa: BLE001 - the hook turns any failure into a log line
        raise RecallError(f"recall failed: {type(exc).__name__}") from exc


def _recall(words: list[str], cwd, config: Config, settings) -> str:
    """The block one query recalls, within the configured bar, dedupe and budget."""
    registry = Registry.load(config.registry_path)
    try:
        result = keys.resolve(cwd, registry, config.host)
    except GitError:
        # The directory git will not read is a session of no project, exactly as the
        # session-start injection reads it: the global pages alone, all one tier.
        result = keys.Resolution(kind=keys.NONE, cwd=cwd)
    held = _block_lines(inject.build(config, result))
    # A registered project's rows rank first, exactly as `aft search` ranks them from this
    # directory; an unregistered one has no rows to rank, so nothing narrows the tiers.
    hits = search.search(
        config,
        " ".join(words),
        current=result.slug if result.project is not None else None,
        limit=settings.max_hits * POOL_FACTOR,
        rerank=settings.rerank,
    )
    lines: list[str] = []
    for hit in hits:
        if len(lines) == settings.max_hits:
            break
        if not _holds_bar(hit, words):
            continue
        if _held_by_block(hit, held):
            continue
        lines.append(_hit_line(hit))
    return _fit(lines, settings.max_chars)


def _holds_bar(hit: Hit, words: list[str]) -> bool:
    """Whether the hit's matched row holds at least min(3, n) of the query's content words.

    The row's own text as the hit carries it is what is counted: its body, which the
    snippet is drawn from, and the heading the row sits under (DESIGN 10). A word counts
    once, however often the row repeats it.
    """
    row = f"{hit.snippet} {hit.heading}".lower()
    tokens = set(re.findall(r"\w+", row))
    found = sum(1 for word in {word.lower() for word in words} if _in_row(word, row, tokens))
    return found >= min(BAR_WORDS, len(words))


def _in_row(word: str, row: str, tokens: set[str]) -> bool:
    """Whether a row holds one query word. The index stems words (Porter), so an
    alphabetic word matches another form of itself on their shared first STEM_CHARS
    letters ("deploys" holds "deploy"), as `digest.ungrounded` matches names; a shorter
    word or one holding a digit or a symbol must be held exactly."""
    if word.isalpha() and len(word) >= STEM_CHARS:
        stem = word[:STEM_CHARS]
        return any(len(token) >= STEM_CHARS and token[:STEM_CHARS] == stem for token in tokens)
    return re.search(rf"\b{re.escape(word)}\b", row) is not None


def _block_lines(block: str) -> list[str]:
    """The content lines of a start block, normalized to what they say.

    The block's headings, bullets and state marks are its grammar rather than its
    content - `aft.inject` puts them on and `aft.index` leaves them off the rows it
    indexes - so both sides are compared as bare statements.
    """
    marks = tuple(f" {mark}" for mark in inject.MARKS.values())
    lines: list[str] = []
    for line in block.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            stripped = stripped[2:]
        for mark in marks:
            if stripped.endswith(mark):
                stripped = stripped[: -len(mark)]
        lines.append(" ".join(stripped.split()).lower())
    return lines


def _held_by_block(hit: Hit, held: list[str]) -> bool:
    """Whether the line a hit would add is one the session's start block already holds.

    The row's text is compared against every content line of the block, each side
    normalized as above: a page line the block injects is the same words its row indexes,
    and a digest section's window can sit inside the handoff line the block shows. The
    snippet's window markers are not words, so they leave before the comparison.
    """
    text = " ".join((hit.snippet or hit.description).replace("...", " ").split()).lower()
    return bool(text) and any(
        text in other or (len(other) >= MIN_HELD_CHARS and other in text) for other in held
    )


def _hit_line(hit: Hit) -> str:
    """One hit as one line: its path, its heading, and the text that matched."""
    text = " ".join((hit.snippet or hit.description).split())
    line = f"{hit.path} - {hit.heading}: {text}" if hit.heading else f"{hit.path}: {text}"
    return f"- {line}"


def _fit(lines: list[str], max_chars: int) -> str:
    """The block within ``max_chars``, whole hit lines only, never one cut mid-line.

    A line that does not fit is dropped whole and a later, shorter one may still fit.
    A block with no room for a single hit is no block at all: a heading alone would say
    there is memory when none was shown (BUILD-PLAN F87).
    """
    kept = [HEADING]
    used = len(HEADING)
    for line in lines:
        cost = len(line) + 1
        if used + cost > max_chars:
            continue
        kept.append(line)
        used += cost
    return "\n".join(kept) if len(kept) > 1 else ""
