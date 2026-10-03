"""The digest: one queued job in, one session digest page out (DESIGN 8, BUILD-PLAN M2.6).

A job is one chunk of one session. The digest reads the entries past that session's
watermark, hands what the filter keeps to the evidence archive and to the model, and keeps
only what a script can check: a fact stands when its quote is the words of the entry it
cites and, for ``stated``, when that entry is one of the user's turns (DESIGN 8). The model
decides what is worth remembering; the script decides what is allowed to become memory,
and everything it refuses is written down with the reason, so a digest that says nothing
was kept reads differently when the verifier was broken than when the session was empty.

What the model is asked for is the prompt and the schema shipped beside this module, and
what comes back is filed as an OKF page under ``digests/`` (DESIGN 6): its summary is the
page's ``description``, its handoff is ``aft.handoff`` - which is what ``aft inject``
reads as a session's "last session" layer (DESIGN 10) - and its facts are ordinary fact
lines whose footnotes name the session and entry each quote came from, so the quote a
future dream re-checks is the one on the page.

The watermark moves last and only once the chunk has been dealt with: a page that was not
written leaves the session's read position where it was, and a chunk the filter emptied
moves it with no page at all, because those entries are decided and have nothing left to
file. A watermark that runs ahead of a decision loses the session quietly and looks
exactly like success (DESIGN 14).
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from importlib.resources import files
from pathlib import Path

import yaml

from aft import (
    __version__,
    evidence,
    index,
    keys,
    model,
    page,
    pagewrite,
    queue,
    readers,
    redact,
    vault,
)
from aft import filter as filtermod
from aft import registry as registrymod
from aft.config import Config, is_excluded, is_worker
from aft.dream import rules
from aft.gitutil import GitError, git
from aft.model import ModelError
from aft.page import KIND_SECTIONS
from aft.readers import USER, Entry

# The page type a digest carries (BUILD-PLAN M2.6).
PAGE_TYPE = "Session Digest"

# The digests directory of sessions that belong to no project (DESIGN 7). No registered
# slug can take this name: a slug starts with a letter or a digit.
UNFILED = "_unfiled"

PROMPT_FILE = "prompts/digest.md"
SCHEMA_FILE = "prompts/digest.schema.json"

# The per-chunk input cap of DESIGN 13: 12,000 input tokens. A small model given a long
# chunk loses track of it and writes about things the chunk never held (finding T17).
MAX_CHUNK_TOKENS = 12_000
# Nothing in the build counts tokens (BUILD-PLAN 1.6 allows no new dependency and no
# tokenizer is vendored), so the cap is enforced on characters at three to a token. A
# session transcript is code, paths and ids as much as prose, and runs nearer three
# characters to a token than four, so the estimate errs high and a chunk never runs over.
CHARS_PER_TOKEN = 3
# The answer's own room, from the schema's limits at the same estimate: a summary of 800
# characters, a handoff of up to eleven items, and twenty facts of two 300-character fields
# plus four short ones come to about 18,000 characters, and the round-up covers the JSON
# around it. An answer cut off at its cap is lost whole, so the room is generous.
MAX_OUTPUT_TOKENS = 8_000

# An entry is shown to the model under a short label, #1 for the chunk's first entry, and
# the script maps the label back to the entry. A session's own entry ids are long uuids
# that a model copies wrongly, and a fact citing a wrong id is a fact lost.
LABEL_PREFIX = "#"

# A page's body is fact sections only (DESIGN 6), so a fact's kind is the heading it sits
# under. A fact's scope is no part of that line, so it travels in the page's own record.
SCOPES = frozenset({"project", "global"})
# DESIGN 9 R6: why dreaming passes over a global fact the user did not commit to. The
# digest records it beside the fact, so the skip is reported and the fact stays written.
GLOBAL_NOT_STATED = "a global page changes only from [stated]"
# DESIGN 6: long-term memory is what stays true; short-term is what the project's last few
# sessions were working with. A fact recorded with no term (a library or memory-import
# digest) is long-term.
LONG = "long"
SHORT = "short"
TERMS = frozenset({LONG, SHORT})
# Short-term memory holds only what the user said or a session saw, and only for a project.
SHORT_SOURCES = frozenset({"stated", "observed"})


class DigestError(Exception):
    """A job this digest cannot honour: no project to file under, no transcript to read,
    or a session id no page path can be built from."""


class AnswerRefused(DigestError):
    """The endpoint answered this chunk and its answer failed the digest schema, or twice
    did not match the chunk it was given (``mismatch``).

    The endpoint is up and the fault is this chunk's, so the job is marked failed with the
    model's own words as its reason, its watermark stays where it was, and the run moves on
    to the next session."""


# What aft.model reports when the endpoint answered and the answer was the fault: the one
# ModelError family a later job would not necessarily meet again. Every other ModelError is
# the endpoint or its setup - unreachable, redirected, a key unset, an answer that cannot be
# billed - and every job left in the queue would meet those alike, so they stay the run's
# failure (DESIGN 14).
ANSWER_REFUSED = "the answer failed its schema"

# A session id reaches this module on a queue row another process wrote, and it goes into a
# page's path, so it must be a name and nothing else: no separator, no drive form, and no
# longer than the bound the endpoint's own schema-name rule uses.
SESSION_ID = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_DIGEST_NAME = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})-(?P<agent>[^-]+)-(?P<session>.+)$")

# A page is staged through a temporary in the directory that will hold it. The suffix is not
# a page extension because a run killed before the rename leaves the file in the vault,
# where `aft lint` and `aft inject` would find a page-shaped file with no page behind it.
PAGE_TEMP_PREFIX = ".digest-"
PAGE_TEMP_SUFFIX = ".tmp"
# A digest page holds one session's conversation, so the file belongs to its owner alone.
PAGE_MODE = 0o600


@dataclass(frozen=True)
class SessionRef:
    """A reference to a session: its agent and session id."""

    agent: str
    session_id: str


@dataclass(frozen=True)
class Fact:
    """One verified fact, with the entry its quote came from."""

    statement: str
    kind: str
    source: str
    quote: str
    entry: str
    scope: str
    date: str  # the day of that entry, which is the line's date last evidenced
    role: str = rules.PRIMARY
    term: str = LONG


@dataclass(frozen=True)
class Dropped:
    """One fact the script refused, and why, as the digest's frontmatter records it."""

    statement: str
    kind: str
    source: str
    entry: str
    reason: str


@dataclass(frozen=True)
class Digest:
    """What one job left in the vault: the page, the facts it kept, the ones it refused."""

    path: str
    facts: tuple[Fact, ...]
    dropped: tuple[Dropped, ...]
    watermark: str


def prompt_text() -> str:
    """The system prompt every digest call sends, shipped as the plan's fixed text."""
    return (files("aft") / PROMPT_FILE).read_text(encoding="utf-8")


def digest_schema() -> dict:
    """The schema the model's answer is constrained and checked by (BUILD-PLAN M2.6)."""
    try:
        text = (files("aft") / SCHEMA_FILE).read_text(encoding="utf-8")
    except OSError as exc:
        raise DigestError(
            f"the shipped digest schema at {SCHEMA_FILE} cannot be read: {exc}"
        ) from exc
    try:
        return json.loads(text)
    except ValueError as exc:
        raise DigestError(f"the shipped digest schema at {SCHEMA_FILE} is not JSON: {exc}") from exc


def tokens(text: str) -> int:
    """What ``text`` costs against the chunk cap: see ``CHARS_PER_TOKEN``."""
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def render(entries: Sequence[Entry]) -> str:
    """The chunk as the prompt is told to read it: ``[#<n>] <ROLE> <time>`` and its text.

    The label leads each block because the schema asks the model to name the entry each
    quote came from, and the role and time because the prompt distinguishes the user's own
    commitments from the agent's, and dates a fact by when it was said.
    """
    return "\n\n".join(
        f"[{label}] {entry.role.upper()} {entry.time.isoformat()}:\n{entry.text}"
        for label, entry in labelled(entries).items()
    )


def labelled(entries: Sequence[Entry]) -> dict[str, Entry]:
    """Each entry of a part under the label ``render`` shows it by, in order."""
    return {f"{LABEL_PREFIX}{n}": entry for n, entry in enumerate(entries, start=1)}


def _cited(part: Sequence[Entry]) -> dict[str, Entry]:
    """The entries of a part by every name an answer may cite one by: its label, the
    label's number without its ``#``, which a model writes as often as the label, and the
    entry's own id, which a model may copy from a quoted line instead. An entry's own id
    wins over a bare number that happens to spell it."""
    labels = labelled(part)
    numbers = {label.removeprefix(LABEL_PREFIX): entry for label, entry in labels.items()}
    return {**numbers, **{entry.id: entry for entry in part}, **labels}


def split_parts(entries: Sequence[Entry], budget: int) -> list[list[Entry]]:
    """The entries in order, in parts that each fit ``budget`` input tokens.

    A part is cut between entries, never inside one, so no entry arrives split or lost
    (BUILD-PLAN M2.6). An entry larger than the whole budget stands alone in its part:
    the cap bounds what goes in one call, and the only way to honour both that and an
    entry that cannot be cut is to give the call more than the cap rather than cut up the
    conversation or drop it.
    """
    parts: list[list[Entry]] = []
    part: list[Entry] = []
    used = 0
    for entry in entries:
        cost = tokens(render([entry]))
        if part and used + tokens(_JOIN) + cost > budget:
            parts.append(part)
            part, used = [], 0
        part.append(entry)
        used += cost + (tokens(_JOIN) if len(part) > 1 else 0)
    if part:
        parts.append(part)
    return parts


_JOIN = "\n\n"


def parts(entries: Sequence[Entry]) -> list[list[Entry]]:
    """The parts one job's entries are sent in, with the prompt paid for first.

    The parts of one job are one digest and one page; what the cap bounds is one call, so
    a chunk over it is a session digested in parts and filed as one page. The cap is on the
    call's input, which is the prompt and the chunk together, so the prompt's own tokens
    come off the budget before the entries are divided.
    """
    return split_parts(entries, max(1, MAX_CHUNK_TOKENS - tokens(prompt_text())))


def _past_the_floor(job: queue.Job, chunk: Sequence[Entry]) -> bool:
    """Whether this session has earned capture: two user prompts of its own (DESIGN 8).

    The chunk is what the filter kept, so a prompt counted here is one the session said on
    the record, and two in a chunk are two in the session. A thinner chunk is measured over
    the whole transcript - the floor is about the session, and one chunk of a long
    conversation can carry a single exchange - counted by `filtermod.prompt_count`, which is
    the filter's own span-aware count rather than a second reading of the spans here.
    """
    if sum(1 for entry in chunk if entry.role == USER) >= filtermod.MIN_SESSION_PROMPTS:
        return True
    if job.transcript_path is None:
        return False
    whole = readers.read(job.agent, job.transcript_path)
    return filtermod.prompt_count(whole) >= filtermod.MIN_SESSION_PROMPTS


def digest_job(config: Config, job: queue.Job, now: datetime | None = None) -> Digest | None:
    """Digest one queued job, or report that it held nothing to digest.

    Reads, filter, the floor, archive, model, verify, page, watermark, commit, in that order:
    the archive takes the chunk before the model sees it, because the page's quotes are found
    in the archive, and the watermark moves only once the page stands in git or the chunk
    is known to hold nothing the filter would keep.

    Returns ``None`` when the job held nothing to digest, which closes it and files no
    page. A session with no entry past its watermark keeps the mark it had, and so does one
    under the two-prompt floor: its entries are not decided against keeping, only not worth
    keeping yet. A chunk the filter emptied moves the mark past the entries read, because
    those are decided and would be decided again by every later run.
    """
    if is_excluded(job.cwd, config):
        queue.drop_job(config, job)
        return None
    _require_session_id(job.session_id)
    vault.require(config)
    registry = registrymod.Registry.load(config.registry_path)
    resolution: keys.Resolution | None
    try:
        resolution = keys.resolve(job.cwd, registry, config.host)
    except GitError:
        # The directory belongs to whatever ran the session and goes away with it: a worktree
        # is made, worked in and removed. The project was decided while it still stood, at
        # session start, and that record files the session now (DESIGN 7). With no record
        # the words are still kept, under no project. Git failing in the vault is the host's
        # and still stops the run.
        resolution = _recorded_resolution(config, registry, job)
        if resolution is None and config.settings.capture.exclude_projects:
            # A project named in capture.exclude_projects is known by resolving its
            # directory, and this one can no longer be resolved. It cannot be shown to be
            # outside the exclusion, so it is treated as inside it and leaves no queue row.
            queue.drop_job(config, job)
            return None
    if resolution is None or resolution.slug is None:
        # No project: a directory outside every registered project and every repository, or
        # one gone with no record. The session is still digested, filed as unfiled, so what
        # it says about the user and their machines reaches global memory (DESIGN 7).
        resolution = keys.Resolution(kind=keys.NONE, cwd=job.cwd)
    if job.transcript_path is None:
        raise DigestError(f"job {job.id} names no transcript for session {job.session_id}")
    if not job.transcript_path.exists():
        # The transcript was deleted after the job was queued: nothing more can be read
        # from it, so the job closes as having nothing to digest instead of failing on every
        # run. What earlier jobs archived stays in the evidence archive (DESIGN 14).
        queue.complete(config, job, None, off_record=job.off_record)
        return None

    read = list(readers.read(job.agent, job.transcript_path, job.watermark))
    if not read:
        queue.complete(config, job, None, off_record=job.off_record)
        return None

    # The span state starts where the session's last digest left it and runs through the
    # job's entries in order, so a span opened in an earlier chunk still holds here and one
    # left open at the end of this chunk holds over the next (DESIGN 12, M2.3). The parts
    # below are cut from what this one pass kept, so they share its state in order.
    kept, off_record = filtermod.filter_entries(read, config, job.off_record)
    kept = without_memory_echo(kept, memory_lines(config, resolution))
    if not kept:
        # Nothing the filter left to digest: the entries were read and decided, so the
        # mark moves and the job closes. A page of nothing would stand as the session's
        # last word and speak for the work that came after it.
        queue.complete(config, job, read[-1].id, off_record=off_record)
        return None
    if not _past_the_floor(job, kept):
        # DESIGN 8's floor, decided before a token is spent or a word archived: a session
        # that never got past one prompt is not captured. The close moves no watermark,
        # because these entries are not decided against keeping, only not yet worth keeping:
        # a session that grows past the floor is read again from where its last digest
        # stopped, so nothing it said is lost to the floor it was under.
        # stopped, so nothing it said is lost to the floor it was under. The span state
        # closes as it arrived, because a span is stored against the watermark it belongs to
        # and that mark did not move.
        queue.complete(config, job, None, off_record=job.off_record)
        return None

    evidence.store(
        config,
        job.agent,
        job.session_id,
        None if resolution.project is None else resolution.project.id,
        kept,
    )
    index.update(config)

    # The page's own name is taken here, before a token is spent on it, from what already
    # stands in the day's directory: a job writes the page it is given and never a page
    # another job wrote.
    when = read[-1].time.astimezone(UTC).date()
    slug = resolution.slug or UNFILED
    rel = _path(config, slug, job.agent, job.session_id, when)

    role = rules.WORKER if is_worker(job.cwd, config) else rules.PRIMARY
    digested = _digest_parts(config, job.session_id, parts(kept), role)
    facts, dropped = digested.facts, digested.dropped

    page, on_page, refused = _page(
        config,
        job,
        rel,
        slug,
        resolution,
        digested.summaries,
        digested.handoff,
        facts,
        dropped,
        when,
        now or datetime.now(UTC),
        role=role,
        refused_parts=digested.refused_parts,
    )
    dropped.extend(refused)
    rel = _write(config, rel, page, lambda: _path(config, slug, job.agent, job.session_id, when))
    # The watermark is the last entry read, not the last one kept: what the filter threw
    # away has been decided, and reading it again would decide it again (DESIGN 8).
    queue.complete(config, job, read[-1].id, off_record=off_record)
    return Digest(path=rel, facts=on_page, dropped=tuple(dropped), watermark=read[-1].id)


# A line of the memory block this short is a heading or a bare label, which a session may
# write for reasons of its own; only a longer line is taken to be memory repeated.
MIN_ECHO_CHARS = 20


def _echo_key(line: str) -> str:
    """A line reduced to what makes it the same line: no list or quote marker, and the
    whitespace inside the line folded. A memory line an agent wraps across two lines is not
    matched; the digest prompt covers that restatement."""
    text = line.strip()
    while text[:2] in ("- ", "* ", "> "):
        text = text[2:].lstrip()
    return " ".join(text.split())


def memory_lines(config: Config, resolution: keys.Resolution) -> frozenset[str]:
    """The lines of the memory block a session in this project is given (DESIGN 10).

    A session sees its memory at start and often repeats it: asked what it knows, or
    restating a decision before acting on it. Those words are memory reflected back, not
    new evidence, and a digest built on them would confirm memory with its own echo and
    count that as recurrence (R3). The block is built now, the same way the hook builds it.
    """
    from aft import inject

    try:
        block = inject.build(config, resolution)
    except Exception:
        # Reading memory is not what a digest is for; a block that cannot be built this
        # moment only means no line of the chunk is taken to be an echo.
        return frozenset()
    keys_ = (_echo_key(line) for line in block.splitlines())
    return frozenset(key for key in keys_ if len(key) >= MIN_ECHO_CHARS)


def without_memory_echo(entries: list[Entry], memory: frozenset[str]) -> list[Entry]:
    """The entries with every line that repeats a memory line taken out.

    An entry left with nothing is dropped. The archive stores what is left, so a quote a
    digest cites is always found in the words the session added, never in memory it was
    handed.
    """
    if not memory:
        return entries
    kept: list[Entry] = []
    for entry in entries:
        lines = entry.text.splitlines()
        remaining = [line for line in lines if _echo_key(line) not in memory]
        if len(remaining) == len(lines):
            kept.append(entry)
            continue
        text = "\n".join(remaining).strip()
        if text:
            kept.append(replace(entry, text=text))
    return kept


def _recorded_resolution(
    config: Config, registry: registrymod.Registry, job: queue.Job
) -> keys.Resolution | None:
    """The project a session started in, as the queue recorded it, or None.

    The record holds the project's id when the registry knew it at session start, and its
    slug either way. A project registered since then is found by that slug; one still
    unregistered is digested under its own name, as its directory would have been.
    """
    recorded = queue.recorded_project(config, job.agent, job.session_id)
    if recorded is None:
        return None
    project_id, slug = recorded
    project = registry.by_id(project_id) if project_id else None
    if project is None:
        project = registry.by_slug(slug)
    if project is not None:
        return keys.Resolution(kind="recorded", cwd=job.cwd, project=project, slug=project.slug)
    return keys.Resolution(kind="recorded", cwd=job.cwd, slug=slug)


def archive_worker_job(config: Config, job: queue.Job) -> bool:
    """Store a worker session in the evidence archive and search index without digesting.

    Returns True if entries were archived, False if there was nothing new to store. Either
    way the row closes and the session's digest becomes owed (``queue.owe_digest``): the
    archive read this transcript through here, so the row is finished work, and what is
    still missing is a digest - which the debt on the session row remembers, and
    ``work.run`` collects when the day's budget has room after primaries. The row cannot be
    left waiting, because capture stops following a session that has a waiting row and the
    worker's later text would never be stored at all (BUILD-PLAN M2.7, the lead's review of
    #39). The session's watermark is left unmoved either way, so the digest the debt calls
    for reads the chunk this run skipped.

    False with no debt is returned only for a row that is not the archive's to handle: one
    whose session is not a worker's, or whose transcript the adapter has not written yet -
    the sweep files that path when the transcript appears, and the row is digested or
    archived then. An excluded session's row is deleted rather than closed, because
    exclusion says it leaves no queue row at all.
    """
    if is_excluded(job.cwd, config):
        queue.drop_job(config, job)
        return False
    if not is_worker(job.cwd, config):
        return False
    if job.transcript_path is None:
        return False
    vault.require(config)
    # A transcript deleted since the job was queued has nothing left to read.
    read = (
        list(readers.read(job.agent, job.transcript_path, job.watermark))
        if job.transcript_path.exists()
        else []
    )
    kept, _ = filtermod.filter_entries(read, config, job.off_record) if read else ([], False)
    if kept and not _past_the_floor(job, kept):
        # The same floor as a primary session: below it the worker's words reach no archive
        # and no digest is owed for them. Its row still closes, because a waiting row would
        # stop capture from following the session at all, and the watermark stays where it
        # was, so the worker that grows past the floor is read whole next time. The row is
        # closed without a debt: nothing was stored, so nothing is owed to be digested.
        queue.close_waiting(config, job)
        return False
    archived = False
    if kept:
        registry = registrymod.Registry.load(config.registry_path)
        try:
            resolution = keys.resolve(job.cwd, registry, config.host)
        except GitError:
            # Archiving files no page, so the project is a label rather than a requirement:
            # a directory that has gone away takes the project its session start recorded,
            # and with no record its words are still stored, under no project. Anything else
            # the resolver raises is a bug or a host failure, and travels up.
            resolution = _recorded_resolution(config, registry, job)
            if resolution is None and config.settings.capture.exclude_projects:
                # As in digest_job: a directory that cannot be resolved cannot be shown to
                # be outside capture.exclude_projects, so its row is dropped.
                queue.drop_job(config, job)
                return False
        project_id = resolution.project.id if resolution and resolution.project else None
        if resolution is not None:
            # A worker repeats its memory as a primary session does, and its archive rows
            # are what search reads, so the echo is taken out before they are written.
            kept = without_memory_echo(kept, memory_lines(config, resolution))
        if kept:
            evidence.store(config, job.agent, job.session_id, project_id, kept)
            index.update(config)
            archived = True
    queue.owe_digest(config, job)
    return archived


# A value with no letter or digit in it, such as "..." or "-", is a placeholder: a model
# that writes one instead of an answer did not read the chunk it was given.
_WORDLESS = re.compile(r"\w")


def placeholder(text: str) -> bool:
    """Whether a value is written but holds no word, such as "..." (finding T17). Empty
    text is not a placeholder: an empty handoff item means nothing to hand off."""
    return bool(text.strip()) and not _WORDLESS.search(text)


def mismatch(answer: dict, part: Sequence[Entry]) -> str | None:
    """Why an answer does not match the chunk it was given, or None when it does.

    A fact citing an entry the chunk does not hold, and a placeholder where a summary, a
    handoff item or a fact should be, are the marks of a model writing about something
    other than the chunk (finding T17). Its summary and handoff carry no quote a script
    could check, so the whole answer goes, not only the facts that show it.
    """
    cited = _cited(part)
    if not answer["summary"].strip() or placeholder(answer["summary"]):
        # A chunk reaches the model only past the two-prompt floor, so it always has
        # something to summarize; a summary of nothing is a model that did not read it.
        return "the summary is empty or a placeholder"
    for fact in answer["facts"]:
        if fact["entry"] not in cited:
            return f"a fact cites entry {fact['entry']!r}, which is not in the chunk"
        if any(not fact[key].strip() or placeholder(fact[key]) for key in ("statement", "quote")):
            return "a fact is empty or a placeholder"
    handoff = answer["handoff"]
    # An empty handoff item is how the prompt says nothing is left to hand off.
    texts = [handoff["in_flight"]["text"]]
    texts += [item["text"] for item in [*handoff["failed"], *handoff["open"]]]
    if any(placeholder(text) for text in texts):
        return "the handoff is a placeholder"
    return None


def grounded_handoff(handoff: dict, part: Sequence[Entry]) -> dict:
    """The handoff as a page records it, keeping only items that name an entry of the chunk.

    Each item of the answer names the entry it comes from; an item naming none of the
    chunk's entries is not grounded in it and is left out (DESIGN 8). The page holds the
    texts alone, the shape injection reads.
    """
    cited = _cited(part)

    def grounded(item: dict) -> bool:
        return bool(item["text"].strip()) and item["entry"] in cited

    in_flight = handoff["in_flight"]
    return {
        "in_flight": in_flight["text"] if grounded(in_flight) else "",
        "failed": [item["text"] for item in handoff["failed"] if grounded(item)],
        "open": [item["text"] for item in handoff["open"] if grounded(item)],
    }


@dataclass(frozen=True)
class Digested:
    """What one job's parts gave, part by part, before the page is written."""

    facts: list[Fact]
    dropped: list[Dropped]
    summaries: list[str]
    handoff: dict
    refused_parts: list[dict]


def _digest_parts(
    config: Config, session_id: str, chunks: Sequence[Sequence[Entry]], role: str
) -> Digested:
    """Ask for, check and verify each part of one job (DESIGN 8).

    One page per job, whatever the number of parts. Summaries are joined uncut: a page's
    description has no cap, so a reader sees every part's own words. A handoff is what
    stood at the end of the part that read it, so the last part's is the job's.

    A part whose answer is refused twice gives the page nothing, neither summary, handoff
    nor fact, and is recorded under ``aft.refused_parts`` with its first and last entry and
    why; the other parts are kept, so one part the model cannot read does not lose a long
    session. Its entries stay in the evidence archive, where ``aft redigest`` reads them
    again. When the last part is refused, nothing says where the work stood at the end, so
    the handoff is empty. A job whose every part is refused has nothing to file: it fails,
    and its watermark stays.
    """
    prompt = prompt_text()
    schema = digest_schema()
    result = Digested([], [], [], {"in_flight": "", "failed": [], "open": []}, [])
    last_refused = False
    for part in chunks:
        try:
            answer, part_facts, part_dropped = _matching_answer(
                config, prompt, part, schema, session_id, role
            )
        except AnswerRefused as exc:
            result.refused_parts.append(
                {"first": part[0].id, "last": part[-1].id, "reason": str(exc)}
            )
            last_refused = True
            continue
        result.facts.extend(part_facts)
        result.dropped.extend(part_dropped)
        result.summaries.append(answer["summary"])
        handoff = grounded_handoff(answer["handoff"], part)
        last_refused = False
        result = replace(result, handoff=handoff)
    if not result.summaries:
        raise AnswerRefused(result.refused_parts[-1]["reason"])
    if last_refused:
        result = replace(result, handoff={"in_flight": "", "failed": [], "open": []})
    return result


# An answer of at least this many facts, every one of whose quotes is missing from the
# entry it cites, was written about something other than its chunk (finding T17): a model
# that read the chunk finds at least one of its own quotes in it.
MIN_FACTS_ALL_UNQUOTED = 2
QUOTE_NOT_FOUND = "the quote is not found in entry"


def unquoted(facts: Sequence[dict], kept: Sequence[Fact], dropped: Sequence[Dropped]) -> bool:
    """Whether an answer's facts are all ones whose quotes the chunk does not hold."""
    return (
        len(facts) >= MIN_FACTS_ALL_UNQUOTED
        and not kept
        and all(fact.reason.startswith(QUOTE_NOT_FOUND) for fact in dropped)
    )


def _matching_answer(
    config: Config,
    prompt: str,
    part: Sequence[Entry],
    schema: dict,
    session_id: str,
    role: str,
) -> tuple[dict, list[Fact], list[Dropped]]:
    """One part's answer and its verified facts, asked again once when it does not match
    the chunk.

    An answer does not match when ``mismatch`` names a problem, or when every one of two or
    more facts quotes words its entry does not hold: its summary and handoff, which carry no
    quote, came from the same reading. Two such answers are that part's failure, as a
    refused schema is (finding T17); ``_digest_parts`` decides what the job keeps.
    """
    problem = ""
    for _ in range(2):
        answer = _model_answer(config, prompt, render(part), schema)
        found = mismatch(answer, part)
        if found is None:
            kept, dropped = verify(config, session_id, part, answer["facts"], role=role)
            if not unquoted(answer["facts"], kept, dropped):
                return answer, kept, dropped
            found = "no fact's quote is in the entry it cites"
        problem = found
    raise AnswerRefused(f"the answer does not match its chunk: {problem}")


def _model_answer(config: Config, prompt: str, chunk: str, schema: dict) -> dict:
    """One digest call and its answer, with a refused answer told as this job's failure.

    An answer that failed its schema on both of the model's attempts is this chunk's own
    problem: the job is marked failed with the model's reason, its watermark stays where it
    was, and the next session gets its turn. Anything else aft.model raises is the endpoint
    or its setup, which every job left would meet alike, so it stays a ModelError and the
    run stops with the queue kept for a later attempt (DESIGN 14: an endpoint that is down
    leaves the jobs queued).
    """
    try:
        return model.complete_json(config, prompt, chunk, schema, MAX_OUTPUT_TOKENS)
    except ModelError as exc:
        if exc.__cause__ is None and ANSWER_REFUSED in str(exc):
            raise AnswerRefused(str(exc)) from exc
        raise


def verify(
    config: Config,
    session_id: str,
    part: Sequence[Entry],
    facts: Sequence[dict],
    role: str = rules.PRIMARY,
) -> tuple[list[Fact], list[Dropped]]:
    """The facts of one part that pass the design's checks, and the refusals beside them.

    Each check is the design's (DESIGN 8, and the kinds and scopes the plan's schema
    enumerates), and the first one a fact fails is the reason it is recorded by. Nothing
    here judges what a fact is worth: that is the prompt's job, and a script that decided
    value as well would put its own taste into memory.
    """
    by_id = _cited(part)
    kept: list[Fact] = []
    dropped: list[Dropped] = []
    for fact in facts:
        entry = by_id.get(fact["entry"])
        if entry is None:
            dropped.append(_refused(fact, f"entry {fact['entry']} is not an entry of this chunk"))
            continue
        # A label is the prompt's name for the entry; what is recorded is the entry's own id.
        fact = {**fact, "entry": entry.id}
        if not evidence.find_quote(config, session_id, entry.id, fact["quote"]):
            dropped.append(_refused(fact, f"{QUOTE_NOT_FOUND} {entry.id}"))
            continue
        if fact["source"] == "stated" and entry.role != USER:
            dropped.append(
                _refused(
                    fact, f"[stated] cites entry {entry.id}, which is not one of the user's turns"
                )
            )
            continue
        if fact["kind"] not in KIND_SECTIONS.values():
            dropped.append(_refused(fact, f"unknown kind {fact['kind']!r}"))
            continue
        if fact["scope"] not in SCOPES:
            dropped.append(_refused(fact, f"unknown scope {fact['scope']!r}"))
            continue
        if fact["term"] not in TERMS:
            dropped.append(_refused(fact, f"unknown term {fact['term']!r}"))
            continue
        if fact["term"] == SHORT and fact["source"] not in SHORT_SOURCES:
            dropped.append(
                _refused(
                    fact, f"a short-term fact is [stated] or [observed], not [{fact['source']}]"
                )
            )
            continue
        if fact["term"] == SHORT and fact["scope"] == "global":
            dropped.append(_refused(fact, "a short-term fact belongs to a project, never global"))
            continue
        kept.append(
            Fact(
                statement=fact["statement"],
                kind=fact["kind"],
                source=fact["source"],
                quote=fact["quote"],
                entry=entry.id,
                scope=fact["scope"],
                date=entry.time.astimezone(UTC).date().isoformat(),
                role=role,
                term=fact["term"],
            )
        )
    return kept, dropped


def _refused(fact: dict, reason: str) -> Dropped:
    return Dropped(
        statement=fact["statement"],
        kind=fact["kind"],
        source=fact["source"],
        entry=fact["entry"],
        reason=reason,
    )


def _require_session_id(session_id: str) -> None:
    """The session id as a name a page can be filed under, or this job's refusal.

    The id reaches this module on a queue row another process wrote, where the only check
    was that it was not empty, and DESIGN 6 files a digest page by it - so a value that
    could step outside ``digests/`` would have a directory created and a page written
    outside the vault before the commit refused it. Refusing the name here closes every
    use the digest makes of it: the page's path, and the ``aft://session/...`` source each
    fact line cites, which is what ``aft trace`` and the dream's quote check follow.
    """
    if SESSION_ID.match(session_id) is None:
        raise DigestError(
            f"session id {session_id!r} is not a name a digest page can be filed under"
        )


def _path(config: Config, slug: str, agent: str, session_id: str, when: date) -> str:
    """The page's path in the vault, as DESIGN 6 lays it out, in the part this job owns.

    A session's first job of a day writes the plain name and every job after it takes the
    next free part - ``-2``, ``-3`` - because one job is one page and a digest's content
    never changes once written (DESIGN 8). The part comes from what already stands in the
    directory, so a job never pays for a model call on a page another job already holds.
    """
    stem = f"{when:%Y-%m-%d}-{agent}-{session_id}"

    def name(part: int) -> str:
        return f"{stem}.md" if part == 1 else f"{stem}-{part}.md"

    directory = config.vault / "digests" / slug / f"{when:%Y}" / f"{when:%m}"
    part = 1
    while (directory / name(part)).is_file():
        part += 1
    return f"digests/{slug}/{when:%Y}/{when:%m}/{name(part)}"


def _page(
    config: Config,
    session: queue.Job | SessionRef,
    rel: str,
    slug: str,
    resolution: keys.Resolution,
    summaries: Sequence[str],
    handoff: dict,
    facts: Sequence[Fact],
    dropped: Sequence[Dropped],
    when: date,
    stamp: datetime,
    role: str = rules.PRIMARY,
    refused_parts: Sequence[dict] = (),
) -> tuple[str, tuple[Fact, ...], tuple[Dropped, ...]]:
    """The digest page: the frontmatter, then the fact lines the script kept.

    Returns the page's text, the facts that made it onto the page, and any the page
    writer refused - a statement it cannot write as one line is a fact this digest did
    not write, and it belongs in the same record as the verifier's refusals rather than
    in prose, which DESIGN 6 does not allow on a fact page at all.

    The frontmatter is written whole because this page is new; the fact lines and the
    sources they cite go through the page writer, which refuses text its own parse would
    not read back, so a page that leaves here lints (DESIGN 6). What that writer took is
    what ``aft.facts`` names, so the record beside a page and the lines on it cannot part.
    """
    frontmatter = _frontmatter(
        config,
        resolution,
        slug,
        summaries,
        handoff,
        facts,
        dropped,
        when,
        stamp,
        role=role,
        refused_parts=refused_parts,
    )
    text, written, refused = _written(frontmatter, rel, session, facts)
    if refused:
        # A line the writer would not take never reached the page, so the record beside it
        # names it as dropped and keeps the kept list to the lines a reader can find.
        frontmatter = _frontmatter(
            config,
            resolution,
            slug,
            summaries,
            handoff,
            written,
            [*dropped, *refused],
            when,
            stamp,
            role=role,
            refused_parts=refused_parts,
        )
        text, written, _ = _written(frontmatter, rel, session, facts)
    return text, tuple(written), tuple(refused)


def _frontmatter(
    config: Config,
    resolution: keys.Resolution,
    slug: str,
    summaries: Sequence[str],
    handoff: dict,
    kept: Sequence[Fact],
    dropped: Sequence[Dropped],
    when: date,
    stamp: datetime,
    role: str = rules.PRIMARY,
    refused_parts: Sequence[dict] = (),
) -> dict:
    """The page's OKF frontmatter, with afterthought's own fields under one ``aft`` key.

    ``aft.facts`` is what this job verified and the page carries, the scope of each fact
    included - the fact line has no field for it (DESIGN 6) - and ``aft.dropped`` is what
    the verifier or the page writer refused, with the reason each one failed on.
    """
    aft: dict[str, object] = {}
    if role == rules.WORKER:
        aft["role"] = rules.WORKER
    if resolution.project is not None:
        aft["project"] = resolution.project.id
    aft["handoff"] = {
        "in_flight": handoff["in_flight"],
        "failed": list(handoff["failed"]),
        "open": list(handoff["open"]),
    }
    if kept:
        aft["facts"] = [_verified(fact) for fact in kept]
    if dropped:
        aft["dropped"] = [_record(fact) for fact in dropped]
    if refused_parts:
        aft["refused_parts"] = [dict(part) for part in refused_parts]
    return {
        "type": PAGE_TYPE,
        "title": f"{_title(resolution, slug)} session {when:%Y-%m-%d}",
        # DESIGN 6: a page's summary is its description, and a fact page holds no prose.
        "description": " ".join(summary for summary in summaries if summary),
        "generated": {
            "by": f"aft {__version__} / {config.settings.model.name}",
            "at": f"{stamp:%Y-%m-%dT%H:%M:%SZ}",
        },
        "aft": aft,
    }


def _written(
    frontmatter: dict,
    rel: str,
    session: queue.Job | SessionRef,
    facts: Sequence[Fact],
) -> tuple[str, list[Fact], list[Dropped]]:
    """The page these facts make, and the ones the page writer would not take.

    A refused line comes back out of the page it was tried on, so no source of its is
    left standing with no fact line to cite it, and the page the writer refuses is never
    the page that gets written.
    """
    doc = pagewrite.PageDoc(_text(frontmatter), rel)
    written: list[Fact] = []
    refused: list[Dropped] = []
    for fact in facts:
        before = doc.render()
        try:
            _add(doc, session, fact)
        except pagewrite.WriteError as exc:
            doc = pagewrite.PageDoc(before, rel)
            refused.append(_refused(_words(fact), str(exc)))
        else:
            written.append(fact)
    return doc.render(), written, refused


def _add(doc: pagewrite.PageDoc, session: queue.Job | SessionRef, fact: Fact) -> None:
    """One fact onto the page, with the source its line cites (DESIGN 6).

    The source names the session and the entry the quote came from, so `aft trace` and
    the dream's quote check both find the words again where the page says they are.
    """
    source = doc.add_source(
        f"aft://session/{session.agent}/{session.session_id}#{fact.entry}",
        title=f"{session.agent} session {session.session_id}",
        quote=fact.quote,
    )
    doc.add_fact(fact.kind, fact.statement, fact.source, fact.date, [source])


def _text(frontmatter: dict) -> str:
    """A new page's opening: the frontmatter block and the blank line the format wants."""
    return (
        "---\n"
        + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True, width=100_000)
        + "---\n\n"
    )


def _record(fact: Dropped) -> dict:
    """One dropped fact as the frontmatter carries it."""
    return {
        "statement": fact.statement,
        "kind": fact.kind,
        "source": fact.source,
        "entry": fact.entry,
        "reason": fact.reason,
    }


def _verified(fact: Fact) -> dict:
    """One fact the page carries, as the verifier verified it, scope and all.

    The fact line has no field for scope (DESIGN 6), so the page's own record names it, and
    a global fact whose source is not one of the user's own commitments carries the rule it will
    be skipped for (DESIGN 9 R6). Where such a fact is filed later is not this page's to say.
    """
    record = {
        "statement": fact.statement,
        "kind": fact.kind,
        "source": fact.source,
        "entry": fact.entry,
        "scope": fact.scope,
        "term": fact.term,
    }
    if fact.role == rules.WORKER:
        record["role"] = rules.WORKER
    if fact.scope == "global" and fact.source != "stated":
        record["skip"] = GLOBAL_NOT_STATED
    return record


def term_of(record: dict) -> str:
    """The term of one fact as a digest page records it under ``aft.facts`` (DESIGN 6).

    A record with no term, from a library or memory-import digest, is long-term.
    """
    return SHORT if record.get("term") == SHORT else LONG


def _words(fact: Fact) -> dict:
    """A verified fact in the shape a refusal is recorded from."""
    return {
        "statement": fact.statement,
        "kind": fact.kind,
        "source": fact.source,
        "entry": fact.entry,
    }


def _title(resolution: keys.Resolution, slug: str) -> str:
    if resolution.project is not None:
        return resolution.project.title
    return "Unfiled" if slug == UNFILED else slug


def _reap_orphans(directory: Path) -> None:
    """Take away the page temporaries of ``directory`` that no live writer can be holding.

    A temporary is open only for the moment between its creation and its rename, so one
    older than the floor of the queue's claim lease was left by a run that died there, and
    nothing else would ever collect it. A younger sibling is another worker's page in
    progress and stands.
    """
    oldest_allowed = datetime.now(UTC) - queue.CLAIM_LEASE_FLOOR
    for stray in directory.glob(f"{PAGE_TEMP_PREFIX}*"):
        try:
            if datetime.fromtimestamp(stray.stat().st_mtime, UTC) < oldest_allowed:
                stray.unlink()
        except OSError:
            continue


def _write(config: Config, rel: str, text: str, choose: Callable[[], str]) -> str:
    """Put the page in the vault and commit it, or leave the vault exactly as it was, and
    return the path it was filed at.

    The path arrives chosen by ``_path`` before any model call, and is checked again under
    the vault lock right before the rename: if another process working the same session (a
    job that outlived its claim lease while another run reclaimed it) filed that part in the
    meantime, ``choose`` names the next free part and the page takes that one instead. The
    lock holds until the commit, so the part found free stays free. A page written here is
    always a page that was not there a moment ago and never replaces one some other job
    wrote.

    The commit is the last step. A page the commit did not take is taken back out on the
    way as well: the file goes, and so does the index entry that staging put beside it,
    because a staged page is a page the next commit of anything at all would carry into
    memory. So a failure leaves no page standing that the watermark does not know about.
    """
    path = config.vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    _reap_orphans(path.parent)
    handle, tmp = tempfile.mkstemp(
        dir=path.parent, prefix=PAGE_TEMP_PREFIX, suffix=PAGE_TEMP_SUFFIX
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(tmp, PAGE_MODE)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise
    with vault.locked(config):
        if (config.vault / rel).exists():
            rel = choose()
        path = config.vault / rel
        try:
            os.replace(tmp, path)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            raise
        try:
            vault.commit(config.vault, [rel], f"aft: digest {rel}")
        except (GitError, vault.VaultError):
            # The page went into git's index before the commit refused it, and a staged
            # page is taken by whichever commit comes next, so the index loses it with the
            # file - and both go under the lock the commit was attempted under.
            path.unlink(missing_ok=True)
            git(["reset", "-q", "--", rel], cwd=config.vault, check=False)
            raise
    return rel


def _is_session_resource(resource: str, session_id: str) -> bool:
    """Whether an aft:// URI cites ``session_id``."""
    if not resource:
        return False
    uri = resource.split("#", 1)[0]
    if uri.startswith("aft://session/"):
        parts = uri.removeprefix("aft://session/").split("/")
        if parts[-1] == session_id:
            return True
    return False


def _is_session_digest(path: Path, session_id: str, config: Config | None = None) -> bool:
    """Whether ``path`` is a digest file belonging to ``session_id``.

    A page's own sources name its session. A page with none, a part that kept no fact,
    is read from its name: ``<session>-<n>`` is a later part of ``session_id`` unless the
    archive holds a separate session with that longer id, which ``config`` lets this ask.
    """
    if not path.is_file() or path.suffix != ".md":
        return False
    match = _DIGEST_NAME.match(path.stem)
    if match is None:
        return False
    sess = match.group("session")
    is_exact = sess == session_id
    prefix, sep, suffix = sess.rpartition("-")
    is_multipart = bool(sep and suffix.isdigit() and prefix == session_id)
    if not is_exact and not is_multipart:
        return False

    try:
        text = path.read_text(encoding="utf-8")
        frontmatter, _, _, _ = page.split_frontmatter(text)
        if isinstance(frontmatter, dict):
            sources = frontmatter.get("sources")
            if isinstance(sources, list):
                session_sources = [
                    str(s.get("resource", ""))
                    for s in sources
                    if isinstance(s, dict)
                    and str(s.get("resource", "")).startswith("aft://session/")
                ]
                if session_sources:
                    return any(_is_session_resource(res, session_id) for res in session_sources)
    except OSError:
        pass

    if is_exact:
        return True
    return config is not None and evidence.session_info(config, sess) is None


def _redacted_entries(config: Config, entries: Sequence[Entry]) -> list[Entry]:
    """The archived entries as a rebuild may read them: with today's redaction on them.

    The archive holds what the patterns known at capture time let through, and a pattern added
    since then is the person saying that text is not to leave this host. Sending the archived
    words to a model unchanged would hand over exactly what they marked secret, so the same
    replacement runs over each entry before any of it is rendered into a prompt (DESIGN 12).
    """
    patterns = config.settings.capture.redact_patterns
    builtin = config.settings.capture.builtin_redaction
    return [
        Entry(
            entry.id,
            entry.time,
            entry.role,
            redact.redact(entry.text, patterns, builtin=builtin),
            entry.is_error,
        )
        for entry in entries
    ]


def redigest(
    config: Config,
    session_id: str,
    now: datetime | None = None,
) -> Digest:
    """Rebuild a digest from archived evidence alone (DESIGN 8, BUILD-PLAN M2.8).

    The entries come from the evidence archive rather than a transcript, so this works
    even when the transcript has expired or is no longer present. The resulting digest
    replaces any existing digest page for that session in the vault.

    A rebuild starts from a session id, so the role comes from the directory the queue last
    saw that session in: a worker's digest rebuilt without its role files a primary digest,
    whose confirmed lines then count toward the one session the dream's minimum is measured
    against (BUILD-PLAN M2.7, rules.py). A session whose directory is now excluded is refused
    outright - exclusion says its words are not the vault's, and a rebuild of an archive
    written before that decision was added does not change the answer.

    Two costs the rebuild owns. Its calls are charged to the day's token budget as any
    digest's are, and a rebuild asked for after that budget is spent refuses rather than
    spending past it (DESIGN 13). And the entries are read through the redaction patterns this
    home has now, because the archive holds only what the patterns of the day it was written
    allowed: text the person has since marked secret is never sent to a model. A quote that
    would span one of those redactions matches no archived entry and is refused the way any
    unverifiable quote is; ``aft forget pattern`` redacts both sides at once, which is how a
    pattern the archive should never have held gets settled.
    """
    _require_session_id(session_id)
    vault.require(config)

    budget = config.settings.work.daily_token_budget
    if model.tokens_used_today(config, now) >= budget:
        raise DigestError(
            f"the day's token budget of {budget} tokens is already spent, so rebuilding the"
            f" digest of session {session_id!r} waits for a day with room in it (DESIGN 13)"
        )

    cwd = queue.session_cwd(config, session_id)
    if cwd is not None and is_excluded(cwd, config):
        raise DigestError(
            f"session {session_id!r} ran in {cwd}, which capture excludes,"
            " so its words are not this vault's to rebuild"
        )
    role = rules.WORKER if cwd is not None and is_worker(cwd, config) else rules.PRIMARY

    entries = _redacted_entries(config, evidence.entries(config, session_id))
    if not entries:
        raise DigestError(f"no archived evidence for session {session_id!r}")

    info = evidence.session_info(config, session_id)
    agent = info[0] if info else None
    project_id = info[1] if info else None

    registry = registrymod.Registry.load(config.registry_path)
    project = registry.by_id(project_id) if project_id else None
    slug = project.slug if project else None

    if slug is None:
        digests_root = config.vault / "digests"
        if digests_root.exists():
            for f in digests_root.rglob("*.md"):
                if _is_session_digest(f, session_id, config):
                    rel_parts = f.relative_to(config.vault).parts
                    if len(rel_parts) >= 2:
                        slug = rel_parts[1]
                        project = registry.by_slug(slug)
                        break

    if slug is None and cwd is not None and Path(cwd).is_dir():
        # An unregistered repository has a name of its own and no project id to record it.
        try:
            slug = keys.resolve(Path(cwd), registry, config.host).slug
        except GitError:
            slug = None

    if slug is None:
        # A session of no project is digested as unfiled (DESIGN 7).
        slug = UNFILED

    if agent is None:
        agent = "claude"

    when = entries[-1].time.astimezone(UTC).date()
    rel = f"digests/{slug}/{when:%Y}/{when:%m}/{when:%Y-%m-%d}-{agent}-{session_id}.md"

    resolution = (
        keys.Resolution(kind=keys.NONE, cwd=Path("."))
        if slug == UNFILED
        else keys.Resolution(
            kind=keys.PATH,
            cwd=Path("."),
            project=project,
            slug=slug,
            root=None,
            alias_added=False,
        )
    )

    entries = without_memory_echo(entries, memory_lines(config, resolution))
    if not entries:
        raise DigestError(
            f"session {session_id!r} holds nothing but the memory it was given, so it has no"
            " digest to rebuild"
        )
    digested = _digest_parts(config, session_id, parts(entries), role)
    facts, dropped = digested.facts, digested.dropped

    page_text, on_page, refused = _page(
        config,
        SessionRef(agent=agent, session_id=session_id),
        rel,
        slug,
        resolution,
        digested.summaries,
        digested.handoff,
        facts,
        dropped,
        when,
        now or datetime.now(UTC),
        role=role,
        refused_parts=digested.refused_parts,
    )
    dropped.extend(refused)

    target_path = config.vault / rel
    target_path.parent.mkdir(parents=True, exist_ok=True)
    _reap_orphans(target_path.parent)

    handle, tmp = tempfile.mkstemp(
        dir=target_path.parent, prefix=PAGE_TEMP_PREFIX, suffix=PAGE_TEMP_SUFFIX
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(page_text)
        os.chmod(tmp, PAGE_MODE)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise

    with vault.locked(config):
        digests_root = config.vault / "digests"
        old_paths: list[str] = []
        if digests_root.exists():
            for f in list(digests_root.rglob("*.md")):
                if _is_session_digest(f, session_id, config) and f != target_path:
                    old_paths.append(f.relative_to(config.vault).as_posix())
                    f.unlink()
                    parent = f.parent
                    while parent != digests_root and parent.exists():
                        try:
                            parent.rmdir()
                        except OSError:
                            break
                        parent = parent.parent
        try:
            os.replace(tmp, target_path)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            raise

        paths_to_commit = list(set([*old_paths, rel]))
        vault.commit(config.vault, paths_to_commit, f"aft: redigest {session_id}")

    index.update(config)
    return Digest(path=rel, facts=on_page, dropped=tuple(dropped), watermark=entries[-1].id)
