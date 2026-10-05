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

The pipeline the digest runs is a setting (BUILD-PLAN F55). ``base`` asks one model one
broad question per chunk of filtered conversation. ``turns`` asks narrower ones over a
compacted input instead: what the user settled, per turn; what the session observed; what
a later item replaced; and the summary and handoff. Both pipelines verify the same way -
the words a quote holds must be the words the evidence archive holds - and both file the
same kind of page.

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
from datetime import UTC, date, datetime, timedelta
from functools import partial
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
from aft.readers import ASSISTANT, TOOL, USER, Entry

# The page type a digest carries (BUILD-PLAN M2.6).
PAGE_TYPE = "Session Digest"

# The digests directory of sessions that belong to no project (DESIGN 7). No registered
# slug can take this name: a slug starts with a letter or a digit.
UNFILED = "_unfiled"

PROMPT_FILE = "prompts/digest.md"
SCHEMA_FILE = "prompts/digest.schema.json"

# The turns pipeline's own prompts and schemas (BUILD-PLAN F55): one question per kind
# of finding, each with the answer's shape beside it.
TURNS = "turns"
TURNS_PROMPT_FILE = "prompts/turns.md"
TURNS_SCHEMA_FILE = "prompts/turns.schema.json"
OBSERVED_PROMPT_FILE = "prompts/observed.md"
OBSERVED_SCHEMA_FILE = "prompts/observed.schema.json"
FINAL_PROMPT_FILE = "prompts/final.md"
FINAL_SCHEMA_FILE = "prompts/final.schema.json"
SUMMARY_PROMPT_FILE = "prompts/summary.md"
SUMMARY_SCHEMA_FILE = "prompts/summary.schema.json"

# What a user turn shows the per-turn call besides its own words: the end of the
# assistant message it answers, within this many characters of the compacted text. The
# number is the harness's own bound for the same context (BUILD-PLAN F55).
BEFORE_CHARS = 800
# How many user turns one turns call reads: the per-turn key was labelled in batches of
# this size, and a model given many more answers fewer of them (BUILD-PLAN F55).
TURN_BATCH = 15
# The BEFORE line of a turn that answers no assistant message: the session's first turn,
# or a turn the user sent straight after another.
NO_REPLY = "(no agent message before this turn)"
# A settled item's kind and scope, set by the script from the label the turns prompt
# asked for (BUILD-PLAN F55): a rule is the decision the user made about it; a decision
# is about the project at hand, and a preference, a rule and a stated fact hold for
# every project.
_TURN_KINDS = {
    "decision": "decision",
    "preference": "preference",
    "rule": "decision",
    "fact": "fact",
}

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
    # An approval's second source: the entry and words of the proposal it approves (R2).
    proposal: tuple[str, str] | None = None


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


def _shipped_text(name: str) -> str:
    """A shipped prompt's text, read the way an install carries it (BUILD-PLAN F55)."""
    try:
        return (files("aft") / name).read_text(encoding="utf-8")
    except OSError as exc:
        raise DigestError(f"the shipped prompt at {name} cannot be read: {exc}") from exc


def _shipped_json(name: str) -> dict:
    """A shipped schema, as the model's answer is constrained and checked by it."""
    try:
        text = (files("aft") / name).read_text(encoding="utf-8")
    except OSError as exc:
        raise DigestError(f"the shipped schema at {name} cannot be read: {exc}") from exc
    try:
        return json.loads(text)
    except ValueError as exc:
        raise DigestError(f"the shipped schema at {name} is not JSON: {exc}") from exc


def turns_prompt_text() -> str:
    """The per-turn prompt, shipped beside the module (BUILD-PLAN F55)."""
    return _shipped_text(TURNS_PROMPT_FILE)


def turns_schema() -> dict:
    """The shape of what the user settled, per turn (BUILD-PLAN F55)."""
    return _shipped_json(TURNS_SCHEMA_FILE)


def observed_prompt_text() -> str:
    """The observed prompt, shipped beside the module (BUILD-PLAN F55)."""
    return _shipped_text(OBSERVED_PROMPT_FILE)


def observed_schema() -> dict:
    """The shape of what one part of a session observed (BUILD-PLAN F55)."""
    return _shipped_json(OBSERVED_SCHEMA_FILE)


def final_prompt_text() -> str:
    """The final-state prompt, shipped beside the module (BUILD-PLAN F55)."""
    return _shipped_text(FINAL_PROMPT_FILE)


def final_schema() -> dict:
    """The shape of what one item replaced (BUILD-PLAN F55)."""
    return _shipped_json(FINAL_SCHEMA_FILE)


def summary_prompt_text() -> str:
    """The summary prompt, shipped beside the module (BUILD-PLAN F55)."""
    return _shipped_text(SUMMARY_PROMPT_FILE)


def summary_schema() -> dict:
    """The shape of a part's summary and handoff (BUILD-PLAN F55)."""
    return _shipped_json(SUMMARY_SCHEMA_FILE)


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
        resolution = keys.resolve(
            job.cwd, registry, config.host, never=config.settings.capture.exclude_paths
        )
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
        # The directory stands but answers no project: it is under the system temporary
        # directory or the downloads, or capture excludes it. What the session-start hook
        # recorded while the directory stood is still the session's project (DESIGN 7): a
        # directory moved aside does not unfile the session that ran in it.
        recorded = _recorded_resolution(config, registry, job)
        if recorded is not None and recorded.slug is not None:
            resolution = recorded
    if resolution is not None and resolution.project is None and resolution.slug is not None:
        # An unregistered directory files its digests under the name its session-start
        # record settled while the directory stood, or - a session with no record - a name
        # no other path holds (DESIGN 7, F49).
        resolution = _settle(config, registry, resolution, job.session_id, job.agent)
    if resolution is None or resolution.slug is None:
        # No project: a directory under the temporary directory or the downloads, one capture
        # excludes, or one gone with no record. The session is still digested, filed as
        # unfiled, so what it says about the user and their machines reaches global memory
        # (DESIGN 7).
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

    The record holds the project's id when the registry knew it at session start, its slug
    either way, and the directory the session ran in (DESIGN 7). A project registered since
    then is found by that id, or by the recorded path when the id names nothing; a record an
    earlier build wrote kept no directory, and is matched by its slug, which is all it named.
    What is still unregistered keeps the name its record settled and the directory it names,
    which is what the digest files under and the dream registers from (DESIGN 7, F49).
    """
    recorded = queue.recorded_project(config, job.agent, job.session_id)
    if recorded is None:
        return None
    root = Path(recorded.path) if recorded.path is not None else None
    project = registry.by_id(recorded.project_id) if recorded.project_id else None
    if project is None and root is not None:
        project = registry.by_path_key(registrymod.path_key(config.host, root))
    if project is None and root is None:
        project = registry.by_slug(recorded.slug)
    if project is not None:
        return keys.Resolution(
            kind="recorded", cwd=job.cwd, project=project, slug=project.slug, root=root
        )
    return keys.Resolution(kind="recorded", cwd=job.cwd, slug=recorded.slug, root=root)


def _settle(
    config: Config,
    registry: registrymod.Registry,
    resolution: keys.Resolution,
    session_id: str,
    agent: str,
    *,
    write: bool = True,
) -> keys.Resolution:
    """An unregistered resolution filed under the name its session-start record settled.

    The record is the answer the session-start hook decided while the directory stood
    (DESIGN 7): it holds the slug the session's digests file under, settled against the
    registry and every other path's claim inside the write that recorded it (F49). A session
    whose record never got written - a hook that failed, or a session captured before records
    were kept - claims a name here instead, by the same rule, and the record is written so
    every later digest of the session files in the same place. A record an earlier build
    wrote names no directory, so nothing can be prefixed and its slug is what there is to
    file under. A step that writes nothing - the backfill's ``--dry-run`` check - asks with
    ``write=False``, which decides the settlement the same way but claims nothing
    (queue.settle_slug, DESIGN 7, F49).
    """
    root = resolution.root
    if root is None or resolution.slug is None:
        return resolution
    recorded = queue.recorded_project(config, agent, session_id)
    if recorded is None:
        if write:
            queue.record_project(
                config, agent, session_id, None, resolution.slug, root, registry=registry
            )
            recorded = queue.recorded_project(config, agent, session_id)
        else:
            settled = queue.settle_slug(config, registry, root, resolution.slug)
            return replace(resolution, slug=settled)
    if recorded is not None and recorded.slug:
        return replace(resolution, slug=recorded.slug)
    return resolution


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
            resolution = keys.resolve(
                job.cwd, registry, config.host, never=config.settings.capture.exclude_paths
            )
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
    """Each part of one job asked, checked and verified by the pipeline the settings name
    (BUILD-PLAN F55)."""
    if config.settings.digest.pipeline == TURNS:
        return _digest_parts_turns(config, session_id, chunks, role)
    return _digest_parts_base(config, session_id, chunks, role)


def _digest_parts_base(
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


def turn_batches(turns: Sequence[str], budget: int) -> list[list[str]]:
    """The turn blocks in order, in batches of at most ``TURN_BATCH`` that each fit
    ``budget`` input tokens; a turn larger than the budget stands alone (DESIGN 13)."""
    batches: list[list[str]] = []
    for block in turns:
        last = batches[-1] if batches else None
        if (
            last is not None
            and len(last) < TURN_BATCH
            and tokens("\n\n".join([*last, block])) <= budget
        ):
            last.append(block)
        else:
            batches.append([block])
    return batches


def turn_label(block: str) -> str:
    """The label a turn block opens with, such as #3, as the model gives it back."""
    return block[1 : block.index("]")]


def turn_blocks(
    part: Sequence[Entry], before: str = "", before_id: str = ""
) -> tuple[list[str], str, str, dict[str, str]]:
    """The user turns of one part as the turns prompt reads them, and the assistant text the
    next part's first turn answers (BUILD-PLAN F55).

    Each turn is labelled as ``render`` labels its entry, so an item's ``turn`` names the
    entry its quote must come from. BEFORE is the end of the assistant message the turn
    answers, carried in from the part before when the turn opens a part, ERRORS the error
    lines of commands that failed since the user last spoke, USER the user's own words.
    Returns one block per turn, the assistant text and entry the next part's first turn answers,
    and which assistant entry each user turn answers, by entry id.
    """
    answered: dict[str, str] = {}
    blocks: list[str] = []
    errors: list[str] = []
    for label, entry in labelled(part).items():
        if entry.role == ASSISTANT:
            before = filtermod.compact(entry)
            before_id = entry.id
        elif entry.role == TOOL:
            shown = filtermod.compact(entry)
            if shown:
                errors.append(shown)
        elif entry.role == USER:
            if before:
                answered[entry.id] = before_id
            context = before[-BEFORE_CHARS:] if before else NO_REPLY
            failed = "\n".join(errors) if errors else "(none)"
            blocks.append(
                f"[{label}] TURN {entry.time:%Y-%m-%d %A}\n"
                f"BEFORE: {context}\n"
                f"ERRORS:\n{failed}\n"
                f"USER: {entry.text}"
            )
            errors = []
            before, before_id = "", ""
    return blocks, before, before_id, answered


def compacted_blocks(part: Sequence[Entry], users: bool) -> str:
    """One part compacted and labelled: the user's words (when ``users``), the assistant's
    prose with its code, diffs and tables named, and failed commands' error lines."""
    blocks: list[str] = []
    for label, entry in labelled(part).items():
        when = entry.time.isoformat()
        if entry.role == ASSISTANT:
            blocks.append(f"[{label}] ASSISTANT {when}:\n{filtermod.compact(entry)}")
        elif entry.role == TOOL:
            shown = filtermod.compact(entry)
            if shown:
                blocks.append(f"[{label}] ERROR {when}:\n{shown}")
        elif entry.role == USER and users:
            blocks.append(f"[{label}] USER {when}:\n{entry.text}")
    return "\n\n".join(blocks)


_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

_NUM_WORDS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "couple": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
}

_RELATIVE_IN = re.compile(
    r"\bin\s+(?:a\s+couple\s+(?:of\s+)?|(\d+|[a-z]+)\s+)(day|days|week|weeks)\b",
    re.IGNORECASE,
)
_WEEKDAY_RE = re.compile(
    r"\b(?:(this|next)\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTH_DATE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*,?\s*(\d{4}))?\b",
    re.IGNORECASE,
)
_DAY_FIRST_DATE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(January|February|March|April|May|June|July|August|September|October|November|December)(?:\s*,?\s*(\d{4}))?\b",
    re.IGNORECASE,
)


def relative_dates(text: str, when: datetime) -> set[date]:
    """The dates relative expressions in ``text`` mean, given the turn's ``when`` (BUILD-PLAN F59).

    Computes dates for today, tomorrow, yesterday, a weekday name with or without 'this' or
    'next', and 'in N days' or 'in N weeks'.
    """
    base = when.date()
    base_w = base.weekday()
    dates: set[date] = set()

    if re.search(r"\btoday\b", text, re.IGNORECASE):
        dates.add(base)

    if re.search(r"\btomorrow\b", text, re.IGNORECASE):
        dates.add(base + timedelta(days=1))

    if re.search(r"\byesterday\b", text, re.IGNORECASE):
        dates.add(base - timedelta(days=1))

    for match in _WEEKDAY_RE.finditer(text):
        modifier = match.group(1).lower() if match.group(1) else ""
        target_w = _WEEKDAYS[match.group(2).lower()]
        diff = (target_w - base_w) % 7
        if modifier == "next":
            if diff == 0:
                dates.add(base + timedelta(days=7))
                dates.add(base + timedelta(days=14))
            else:
                dates.add(base + timedelta(days=diff))
                dates.add(base + timedelta(days=diff + 7))
        elif modifier == "this":
            cal_diff = target_w - base_w
            dates.add(base + timedelta(days=cal_diff))
            if cal_diff < 0:
                dates.add(base + timedelta(days=cal_diff + 7))
            if cal_diff == 0:
                dates.add(base + timedelta(days=7))
        else:
            if diff == 0:
                dates.add(base)
                dates.add(base + timedelta(days=7))
            else:
                dates.add(base + timedelta(days=diff))

    for match in _RELATIVE_IN.finditer(text):
        num_str = match.group(1)
        unit = match.group(2).lower()
        if num_str is None:
            count = 2
        elif num_str.isdigit():
            count = int(num_str)
        else:
            count = _NUM_WORDS.get(num_str.lower(), 0)
        if count <= 0:
            continue
        if unit.startswith("day"):
            dates.add(base + timedelta(days=count))
        elif unit.startswith("week"):
            dates.add(base + timedelta(weeks=count))

    return dates


def ungrounded(statement: str, said: str, when: datetime | None = None) -> list[str]:
    """The names and numbers of ``statement`` that ``said`` does not hold (BUILD-PLAN F52,
    F55): a word holding a digit or one of ``_/.:-``, or a capital letter anywhere but at the
    statement's start. Plain words say how, not what, and are not checked. Punctuation around
    a word is set aside, and a word matches another form of itself on their shared first five
    letters. A date computed from a relative expression in ``said`` and the turn's ``when`` is
    grounded when written in ISO (2026-10-06), 'October 6', or day-first (6 October) style (F59)."""
    if when is not None:
        allowed = relative_dates(said, when)
        if allowed:
            spans: list[tuple[int, int]] = []
            for match in _ISO_DATE.finditer(statement):
                try:
                    dt = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
                except ValueError:
                    continue
                if dt in allowed:
                    spans.append(match.span())
            for match in _MONTH_DATE.finditer(statement):
                m = _MONTHS[match.group(1).lower()]
                d = int(match.group(2))
                y_str = match.group(3)
                if y_str is not None:
                    try:
                        dt = date(int(y_str), m, d)
                    except ValueError:
                        continue
                    if dt in allowed:
                        spans.append(match.span())
                elif any(dt.month == m and dt.day == d for dt in allowed):
                    spans.append(match.span())
            for match in _DAY_FIRST_DATE.finditer(statement):
                d = int(match.group(1))
                m = _MONTHS[match.group(2).lower()]
                y_str = match.group(3)
                if y_str is not None:
                    try:
                        dt = date(int(y_str), m, d)
                    except ValueError:
                        continue
                    if dt in allowed:
                        spans.append(match.span())
                elif any(dt.month == m and dt.day == d for dt in allowed):
                    spans.append(match.span())
            if spans:
                chars = list(statement)
                for start, end in spans:
                    for i in range(start, end):
                        chars[i] = " "
                statement = "".join(chars)
    held = {_bare(w) for w in _WORDS.findall(said)}
    missing: list[str] = []
    for number, word in enumerate(_WORDS.findall(statement)):
        low = _bare(word)
        name = word.strip(".,;:!?\"'()")
        distinctive = any(c.isdigit() or c in "_/.:-" for c in name) or any(
            c.isupper() for c in (name[1:] if number == 0 else name)
        )
        if not low or not distinctive or low in held:
            continue
        if not any(len(h) >= 5 and h[:5] == low[:5] for h in held):
            missing.append(word.strip(".,;:!?\"'()"))
    return missing


def grounding_sentence(text: str, statement: str, said: str, when: datetime | None = None) -> str:
    """The one sentence of ``text`` that, beside ``said``, grounds every name of
    ``statement``, verbatim; empty when none does or two do equally well."""
    grounding = [
        sentence.strip()
        for sentence in _SENTENCES.split(text)
        if sentence.strip()
        and len(sentence.strip()) <= 300
        and not ungrounded(statement, f"{said}\n{sentence}", when=when)
    ]
    if not grounding:
        return ""
    return best_sentence("\n".join(grounding), statement) or (
        grounding[0] if len(grounding) == 1 else ""
    )


def best_sentence(text: str, target: str) -> str:
    """The sentence of ``text`` that shares most content words with ``target``, verbatim, or
    empty when none shares at least two or two share the most (BUILD-PLAN F55: an
    approval's proposal)."""
    wanted = {_bare(w) for w in _WORDS.findall(target) if len(_bare(w)) >= 4}
    best, score, tied = "", 1, False
    for sentence in _SENTENCES.split(text):
        sentence = sentence.strip()
        if len(sentence) > 300:
            continue
        shared = len(wanted & {_bare(w) for w in _WORDS.findall(sentence)})
        if shared > score:
            best, score, tied = sentence, shared, False
        elif shared == score and best:
            tied = True
    # Two sentences matching equally (option A beside option B) leave which was approved
    # open: none is cited.
    return "" if tied else best


_SENTENCES = re.compile(r"(?<=[.!?:])\s+|\n+")


def _bare(word: str) -> str:
    """A word folded and stripped of the punctuation around it."""
    return word.casefold().strip(".,;:!?\"'()[]`")


_WORDS = re.compile(r"[A-Za-z0-9_./-]+")


def _turn_facts(
    answer: dict,
    part: Sequence[Entry],
    config: Config | None = None,
    session_id: str = "",
    answered: dict[str, str] | None = None,
    entries: dict[str, Entry] | None = None,
    labelled: set[str] | None = None,
) -> tuple[list[dict], list[Dropped]]:
    """What the user settled, as facts the shared checks read, and the items refused here
    (BUILD-PLAN F55): a statement naming words neither the user's turn nor an approved
    proposal holds, or an approval whose proposal is not in the message the turn answers.

    An approval carries ``proposal`` - the entry and words of what it approves - for the
    page to cite beside the user's own words (DESIGN R2). ``labelled`` gathers the entry of
    every turn the answer gave an item, whatever its label.
    """
    facts: list[dict] = []
    refused: list[Dropped] = []
    cited = _cited(part)
    if labelled is not None:
        labelled.clear()  # the turns of the answer of record, not of an earlier attempt
    for item in answer["items"]:
        if labelled is not None and item["turn"] in cited:
            labelled.add(cited[item["turn"]].id)
        if item["label"] not in _TURN_KINDS:
            continue  # a question, proposal, request or nothing: the outlet, never memory
        fact = {
            "statement": item["statement"],
            "kind": _TURN_KINDS[item["label"]],
            "source": "stated",
            "term": item["term"],
            "quote": item["quote"],
            "entry": item["turn"],
            "scope": item["scope"],
        }
        turn = cited.get(fact["entry"])
        if turn is not None:
            fact = {**fact, "entry": turn.id}  # a refusal names the entry, not the prompt's label
        if any(not fact[key].strip() or placeholder(fact[key]) for key in ("statement", "quote")):
            refused.append(_refused(fact, "the statement or quote is empty or a placeholder"))
            continue
        said = turn.text if turn is not None else fact["quote"]
        turn_time = turn.time if turn is not None else None
        proposal = item.get("proposal", "").strip()
        if proposal:
            before_id = (answered or {}).get(fact["entry"], "")
            if (
                before_id
                and config is not None
                and not evidence.find_quote(config, session_id, before_id, proposal)
            ):
                # The model's copy of the proposal is not verbatim: cite the sentence of the
                # answered message that best matches what was decided, verbatim from it.
                answered_entry = (entries or {}).get(before_id)
                proposal = (
                    best_sentence(answered_entry.text, f"{fact['statement']} {proposal}")
                    if answered_entry is not None
                    else ""
                )
            if (
                not before_id
                or not proposal
                or config is None
                or not evidence.find_quote(config, session_id, before_id, proposal)
            ):
                refused.append(
                    _refused(fact, "the approved proposal is not in the message the turn answers")
                )
                continue
            fact["proposal"] = (before_id, proposal)
            # DESIGN R2: an approval's statement holds no word that neither the user's words
            # of approval nor the proposal they approve says.
            said = f"{said}\n{proposal}"
        missing = ungrounded(fact["statement"], said, when=turn_time)
        before_id = (answered or {}).get(fact["entry"], "")
        answered_entry = (entries or {}).get(before_id)
        if missing and "proposal" not in fact and answered_entry is not None and config:
            # A name the user's reply takes from the message it answers is the agent's word:
            # the line cites the one sentence of that message that holds what the statement
            # needs, as an approval cites its proposal (DESIGN R2).
            sentence = grounding_sentence(
                answered_entry.text, fact["statement"], said, when=turn_time
            )
            if sentence and evidence.find_quote(config, session_id, before_id, sentence):
                fact["proposal"] = (before_id, sentence)
                missing = []
        if missing:
            refused.append(
                _refused(
                    fact,
                    "the statement names words neither the user's turn nor a proposal holds: "
                    + ", ".join(missing),
                )
            )
            continue
        facts.append(fact)
    return facts, refused


def _observed_facts(answer: dict) -> list[dict]:
    """What the session observed, as facts the shared checks read (BUILD-PLAN F55)."""
    return [
        {
            "statement": item["statement"],
            "kind": "fact",
            "source": "observed",
            "term": item["term"],
            "quote": item["quote"],
            "entry": item["entry"],
            "scope": "project",
        }
        for item in answer["items"]
        if item["statement"].strip()
        and item["quote"].strip()
        and not placeholder(item["statement"])
        and not placeholder(item["quote"])
    ]


def _call_refused(part: Sequence[Entry], call: str, reason: str) -> Dropped:
    """A call of the turns pipeline that was refused, recorded beside the refused facts: the
    part was still filed, so it is not a refused part."""
    return Dropped(
        statement="", kind="", source="", entry=part[0].id, reason=f"{call} refused: {reason}"
    )


def _final_state(
    config: Config, facts: list[Fact], order: dict[str, int]
) -> tuple[list[Fact], list[Dropped]]:
    """The facts that stand once a later one has replaced an earlier one, by the user's
    explicit word only (BUILD-PLAN F55), and the replaced ones as refusals naming why.

    The facts are listed in the session's own order, by the entry each quote came from, so
    "later" in the answer is later in the session, whatever order the calls returned them in.
    """
    if len(facts) < 2:
        return facts, []
    ordered = sorted(facts, key=lambda fact: order.get(fact.entry, 0))
    listing = "\n".join(
        f"[#{n}] [{fact.source}] {fact.kind}: {fact.statement} (words: {fact.quote})"
        for n, fact in enumerate(ordered, start=1)
    )
    try:
        answer = _model_answer(config, final_prompt_text(), listing, final_schema())
    except AnswerRefused as exc:
        return ordered, [
            Dropped(
                statement="",
                kind="",
                source="",
                entry=ordered[0].entry,
                reason=f"final state refused, nothing replaced: {exc}",
            )
        ]
    replaced: dict[int, int] = {}
    for item in answer["replacements"]:
        later = _label_number(item["later"])
        earlier = _label_number(item["earlier"])
        if later is None or earlier is None or not 1 <= earlier < later <= len(ordered):
            continue
        if ordered[later - 1].source != "stated":
            continue  # only the user's own words replace anything
        if order.get(ordered[earlier - 1].entry, 0) >= order.get(ordered[later - 1].entry, 0):
            continue  # a replacement comes later in the session, not only later in a list
        replaced[earlier] = later
    kept = [fact for n, fact in enumerate(ordered, start=1) if n not in replaced]
    dropped = [
        Dropped(
            statement=ordered[n - 1].statement,
            kind=ordered[n - 1].kind,
            source=ordered[n - 1].source,
            entry=ordered[n - 1].entry,
            reason=f"replaced by a later item the user stated: {ordered[later - 1].statement}",
        )
        for n, later in sorted(replaced.items())
    ]
    return kept, dropped


def _label_number(label: str) -> int | None:
    """``#3`` or ``3`` as 3, or None when it is neither."""
    text = label.strip().removeprefix(LABEL_PREFIX)
    return int(text) if text.isdigit() else None


def _asked(
    config: Config,
    prompt: str,
    text: str,
    schema: dict,
    session_id: str,
    part: Sequence[Entry],
    role: str,
    make,
) -> tuple[list[Fact], list[Dropped]]:
    """One call's items, verified, asked again once when two or more items all quote words
    their entries do not hold (finding T17), as the base pipeline asks."""
    kept: list[Fact] = []
    dropped: list[Dropped] = []
    for _ in range(2):
        made = make(_model_answer(config, prompt, text, schema))
        facts, refused = made if isinstance(made, tuple) else (made, [])
        kept, dropped = verify(config, session_id, part, facts, role=role)
        proposals = {
            (fact["statement"], fact["entry"]): fact["proposal"]
            for fact in facts
            if "proposal" in fact
        }
        kept = [
            replace(fact, proposal=proposals[(fact.statement, fact.entry)])
            if (fact.statement, fact.entry) in proposals
            else fact
            for fact in kept
        ]
        retry = unquoted(facts, kept, dropped)  # only verify's refusals tell T17's case
        dropped = [*refused, *dropped]
        if not retry:
            break
    return kept, dropped


def _digest_parts_turns(
    config: Config, session_id: str, chunks: Sequence[Sequence[Entry]], role: str
) -> Digested:
    """Each part asked the turns pipeline's narrow questions (BUILD-PLAN F55).

    Per part: the summary and handoff, what the user settled turn by turn, and what the
    session observed, each its own call over the part compacted. Every item goes through
    ``verify`` as the base pipeline's facts do, so a quote must stand in its entry and
    ``[stated]`` must cite one of the user's turns; a turn item whose statement names words
    the session never used is refused first. Across the job, a later item replaces an earlier
    one only where the user said so. A refused summary, turns or observed call is recorded
    beside the refused facts and the part's other calls still run; a job that gives neither
    a summary nor a fact is refused.
    """
    result = Digested([], [], [], {"in_flight": "", "failed": [], "open": []}, [])
    order = {entry.id: n for n, entry in enumerate((e for c in chunks for e in c), start=1)}
    last_refused = False
    summary_refused = ""
    before, before_id = "", ""
    entries = {entry.id: entry for chunk in chunks for entry in chunk}
    for part in chunks:
        turns, before, before_id, answered = turn_blocks(part, before, before_id)
        try:
            summary = None
            for _ in range(2):
                summary = _model_answer(
                    config,
                    summary_prompt_text(),
                    compacted_blocks(part, users=True),
                    summary_schema(),
                )
                if summary["summary"].strip() and not placeholder(summary["summary"]):
                    break
                summary = None
            if summary is None:
                raise AnswerRefused("the summary is empty or a placeholder")
        except AnswerRefused as exc:
            # The turns and observed questions do not read the summary: the part still
            # gives what its user settled and what it observed.
            result.dropped.append(_call_refused(part, "summary", str(exc)))
            summary_refused = str(exc)
            summary = None
        turn_facts = partial(
            _turn_facts,
            part=part,
            config=config,
            session_id=session_id,
            answered=answered,
            entries=entries,
        )
        cited = _cited(part)
        for batch in turn_batches(turns, MAX_CHUNK_TOKENS - tokens(turns_prompt_text())):
            # A turn the answer skipped is asked again once, on its own with the others
            # skipped: every turn gets a label, as the prompt asks (BUILD-PLAN F55).
            pending = batch
            for _ in range(2):
                labelled: set[str] = set()
                try:
                    kept, dropped = _asked(
                        config,
                        turns_prompt_text(),
                        "\n\n".join(pending),
                        turns_schema(),
                        session_id,
                        part,
                        role,
                        partial(turn_facts, labelled=labelled),
                    )
                    result.facts.extend(kept)
                    result.dropped.extend(dropped)
                except AnswerRefused as exc:
                    result.dropped.append(_call_refused(part, "turns", str(exc)))
                    break
                pending = [b for b in pending if cited[turn_label(b)].id not in labelled]
                if not pending:
                    break
        seen = compacted_blocks(part, users=False)
        if seen:
            try:
                kept, dropped = _asked(
                    config,
                    observed_prompt_text(),
                    seen,
                    observed_schema(),
                    session_id,
                    part,
                    role,
                    _observed_facts,
                )
                result.facts.extend(kept)
                result.dropped.extend(dropped)
            except AnswerRefused as exc:
                result.dropped.append(_call_refused(part, "observed", str(exc)))
        if summary is None:
            last_refused = True
            continue
        result.summaries.append(summary["summary"])
        result = replace(result, handoff=grounded_handoff(summary["handoff"], part))
        last_refused = False
    if not result.summaries and not result.facts:
        raise AnswerRefused(summary_refused)
    if last_refused:
        result = replace(result, handoff={"in_flight": "", "failed": [], "open": []})
    facts, replaced = _final_state(config, result.facts, order)
    return replace(result, facts=facts, dropped=[*result.dropped, *replaced])


# An answer of at least this many facts, every one of whose quotes is missing from the
# entry it cites, was written about something other than its chunk (finding T17): a model
# that read the chunk finds at least one of its own quotes in it.
MIN_FACTS_ALL_UNQUOTED = 2
QUOTE_NOT_FOUND = "the quote is not found in entry"


# What a turn holds that is not the user's own words (DESIGN 6, R2): a paste, a slash
# command's text, a shell command run from the prompt and its output. A block with no closing
# tag runs to the end of the turn.
_NOT_OWN = re.compile(
    r"<(pasted_content|command-name|command-message|command-args|bash-input|bash-stdout"
    r"|bash-stderr|local-command-stdout)\b[^>]*>"
)


def own_words(text: str, quote: str) -> bool:
    """Whether ``quote`` stands in ``text`` outside every paste and command block: true when
    one of its places does, or when it is not found here to judge."""
    spans = []
    for opening in _NOT_OWN.finditer(text):
        closing = re.compile(rf"</{opening.group(1)}\b[^>]*>").search(text, opening.end())
        spans.append((opening.start(), closing.end() if closing else len(text)))
    words = " ".join(quote.split()).lower()
    if not words:
        return True
    # The text with its whitespace collapsed, as quotes are matched, and where each of its
    # characters stands in the text itself.
    flat: list[str] = []
    origin: list[int] = []
    for match in re.finditer(r"\S+", text):
        if flat:
            flat.append(" ")
            origin.append(match.start())
        flat.append(match.group().lower())
        origin.extend(range(match.start(), match.end()))
    places = [m.start() for m in re.finditer(re.escape(words), "".join(flat))]
    if not places:
        return True
    return any(not any(start <= origin[p] < end for start, end in spans) for p in places)


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
        if fact["source"] == "stated" and "?" in fact["quote"]:
            # BUILD-PLAN F52: a quote holding a question mark is never [stated].
            dropped.append(_refused(fact, "the quote is a question, never a settled item"))
            continue
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
        if fact["source"] == "stated" and not own_words(entry.text, fact["quote"]):
            dropped.append(
                _refused(fact, "[stated] quotes a paste or a command block, not the user's words")
            )
            continue
        if fact["source"] == "stated":
            said = entry.text
            if "proposal" in fact and fact["proposal"]:
                said = f"{said}\n{fact['proposal'][1]}"
            missing = ungrounded(fact["statement"], said, when=entry.time)
            if missing:
                dropped.append(
                    _refused(
                        fact,
                        "the statement names words neither the user's turn nor a proposal holds: "
                        + ", ".join(missing),
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
    elif resolution.root is not None:
        # A digest of an unregistered directory carries the path its sessions ran in, so
        # the index files its facts into that directory's own scope and not into whatever
        # directory once claimed the name the digest is filed under (DESIGN 7, F49).
        aft["path"] = str(resolution.root)
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
    sources = [source]
    if fact.proposal is not None:
        entry, quote = fact.proposal
        sources.append(
            doc.add_source(
                f"aft://session/{session.agent}/{session.session_id}#{entry}",
                title=f"{session.agent} session {session.session_id} (the proposal approved)",
                quote=quote,
            )
        )
    doc.add_fact(fact.kind, fact.statement, fact.source, fact.date, sources)


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


def _recorded_target(
    config: Config,
    registry: registrymod.Registry,
    session_id: str,
    agent: str | None,
    cwd: Path | None,
    *,
    unfiled: bool,
    write: bool = True,
) -> keys.Resolution | None:
    """What a rebuild resolves this session's directory to (DESIGN 7, F49).

    The backfill resolves the directory the session-start record keeps - its sessions' own
    directories are gone more often than not - and falls back to the queue's record of where
    the session last ran. An ordinary rebuild resolves the queue's record alone, as it did
    before the record existed. A project registered at the directory is answered even when
    the directory itself is gone, by the path key it was registered under; a directory that
    resolves to none is no target at all. ``write=False`` decides an unrecorded directory's
    settlement without claiming it, for a step that writes nothing (DESIGN 7, F49).
    """
    directory: Path | None = None
    if unfiled:
        recorded = queue.recorded_project(config, agent or "claude", session_id)
        if recorded is not None and recorded.path is not None:
            directory = Path(recorded.path)
    if directory is None and cwd is not None and Path(cwd).is_dir():
        directory = Path(cwd)
    if directory is None:
        return None
    if not directory.is_dir():
        project = registry.by_path_key(registrymod.path_key(config.host, directory))
        if project is None:
            return None
        return keys.Resolution(
            kind="recorded", cwd=directory, project=project, slug=project.slug, root=directory
        )
    try:
        resolution = keys.resolve(
            directory, registry, config.host, never=config.settings.capture.exclude_paths
        )
    except GitError:
        return None
    if resolution.project is None and resolution.slug is not None:
        return _settle(config, registry, resolution, session_id, agent or "claude", write=write)
    return resolution


def _rebuild_slug(
    config: Config,
    session_id: str,
    cwd: Path | None,
    agent: str | None,
    *,
    unfiled: bool,
    write: bool = True,
) -> tuple[str, str | None, registrymod.Project | None]:
    """The agent and the slug a rebuild of this session files under, with its project.

    The archive's record of the session names its project when it has one; a digest of the
    session filed under another slug names it next; otherwise the directory - the
    session-start record's for the backfill, the queue's for an ordinary rebuild - is
    resolved, and the slug it answers is the slug the page files under. A ``None`` slug is
    a session of no project: its directory answers none (DESIGN 7, F49). ``write=False``
    decides the target without claiming anything, for a step that writes nothing
    (DESIGN 7, F49).
    """
    registry = registrymod.Registry.load(config.registry_path)
    info = evidence.session_info(config, session_id)
    if agent is None:
        agent = info[0] if info else "claude"
    project_id = info[1] if info else None
    project = registry.by_id(project_id) if project_id else None
    slug = project.slug if project else None

    if slug is None:
        digests_root = config.vault / "digests"
        if digests_root.exists():
            for f in digests_root.rglob("*.md"):
                if _is_session_digest(f, session_id, config):
                    rel_parts = f.relative_to(config.vault).parts
                    if len(rel_parts) >= 2:
                        if unfiled and rel_parts[1] == UNFILED:
                            # The backfill's whole point is a digest that sits under
                            # _unfiled moving out of it, so that directory names nothing
                            # here: the session's directory decides instead (DESIGN 7, F49).
                            continue
                        slug = rel_parts[1]
                        project = registry.by_slug(slug)
                        break

    if slug is None:
        resolution = _recorded_target(
            config, registry, session_id, agent, cwd, unfiled=unfiled, write=write
        )
        if resolution is not None and resolution.project is not None:
            project, slug = resolution.project, resolution.project.slug
        elif resolution is not None and resolution.slug is not None:
            slug = resolution.slug
    return agent, slug, project


def stays_unfiled(config: Config, session_id: str) -> bool:
    """Whether the backfill of this session has nowhere to move its digest to (DESIGN 7, F49).

    The backfill files a session under the project its directory answers for. A session
    whose directory is gone with nothing naming it, or stands under the system temporary
    directory, the downloads, or a capture exclusion, answers no project again, so a
    rebuild would write its digest onto the same ``_unfiled`` path and list it again on
    every run. Such a session stays unfiled until its directory's answer changes. The check
    claims nothing: a dry run of the backfill writes nothing at all, so the answer is
    decided without settling a name into the queue (DESIGN 7, F49).
    """
    _require_session_id(session_id)
    _, slug, _ = _rebuild_slug(
        config, session_id, queue.session_cwd(config, session_id), None, unfiled=True, write=False
    )
    return slug is None


def redigest(
    config: Config,
    session_id: str,
    now: datetime | None = None,
    *,
    unfiled: bool = False,
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

    With ``unfiled`` the rebuild is the backfill of DESIGN 7 (F49): the session's digests sit
    under ``digests/_unfiled`` and it is rebuilt to file under the project its recorded
    directory now resolves to. The directory it already sits in names nothing on purpose,
    and the session-start record is what says where the session ran, so the backfill resolves
    that record's directory first and the queue's own record of it second.

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

    agent, slug, project = _rebuild_slug(config, session_id, cwd, None, unfiled=unfiled)
    if slug is None:
        # A session of no project is digested as unfiled (DESIGN 7).
        slug = UNFILED
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
            alias_added=None,
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


def _unfiled_digests(config: Config) -> dict[str, list[Path]]:
    """The digest files filed as unfiled, by the session the rebuild names.

    The parts of one session - the ``-2``, ``-3`` files a second chunk of a day takes - are
    listed under the session the rebuild names, so a coverage check sees them all. A longer
    id the archive holds a session of its own stays its own session (DESIGN 7).
    """
    root = config.vault / "digests" / UNFILED
    named: dict[str, list[Path]] = {}
    if root.is_dir():
        for path in root.rglob("*.md"):
            match = _DIGEST_NAME.match(path.stem)
            if match is None:
                continue
            session = match.group("session")
            prefix, sep, suffix = session.rpartition("-")
            if sep and suffix.isdigit() and evidence.session_info(config, session) is None:
                # A later part of one session, unless the archive holds the longer id itself.
                session = prefix
            named.setdefault(session, []).append(path)
    return named


def _citations(config: Config, session_id: str, digests: Sequence[Path]) -> dict[Path, set[str]]:
    """The entries of ``session_id`` each digest part cites in its sources, by the part
    (DESIGN 8).

    A digest page's sources name the entries its facts were verified against, one
    ``aft://session/<agent>/<session>#<entry>`` each. A part that kept no fact cites no
    entry, so no surviving entry can be named for the span it covered. Citations of some
    other session's entries are not this session's coverage.
    """
    cited: dict[Path, set[str]] = {}
    for path in digests:
        cited.setdefault(path, set())
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        frontmatter, _, _, _ = page.split_frontmatter(text)
        if not isinstance(frontmatter, dict):
            continue
        sources = frontmatter.get("sources")
        if not isinstance(sources, list):
            continue
        for source in sources:
            if not isinstance(source, dict):
                continue
            resource = str(source.get("resource", ""))
            entry = resource.rpartition("#")[2] if "#" in resource else ""
            if entry and _is_session_resource(resource, session_id):
                cited[path].add(entry)
    return cited


def _uncovered_reason(
    config: Config, session_id: str, digests: Sequence[Path], archived: set[str]
) -> str | None:
    """Why the session's digests are not covered by the archive, or None when they are
    (DESIGN 7, F49).

    Coverage is judged part by part: every part of the session's digests must stand on
    entries the archive still holds. A part that cites no entry of its own session names no
    surviving entry of the span it covered, and a part that cites entries the archive no
    longer holds names gone ones - so the session is not rebuilt on the strength of its
    other parts. Entries leave the archive between the digest being written and the rebuild
    because retention purges them, the evidence archive's oldest first
    (`evidence.purge`, BUILD-PLAN M2.7).
    """
    by_part = _citations(config, session_id, digests)
    silent = sorted(path.name for path, cited in by_part.items() if not cited)
    if silent:
        noun = "part" if len(silent) == 1 else "parts"
        verb = "cites" if len(silent) == 1 else "cite"
        pronoun = "its" if len(silent) == 1 else "their"
        span = silent[0] if len(silent) == 1 else f"{silent[0]} through {silent[-1]}"
        return (
            f"its digests hold {len(silent)} {noun} that {verb} no entry of {pronoun} own"
            f" session ({span})"
        )
    missing = sorted(set().union(*by_part.values()) - archived)
    if missing:
        noun = "entry" if len(missing) == 1 else "entries"
        span = missing[0] if len(missing) == 1 else f"{missing[0]} through {missing[-1]}"
        return f"its digests cite {len(missing)} {noun} the archive no longer holds ({span})"
    return None


def uncovered_unfiled(config: Config) -> list[tuple[str, str]]:
    """The unfiled sessions the backfill must skip because the archive no longer covers them,
    each with the reason (DESIGN 7, F49).

    A rebuild replaces the session's digests with one built from the entries the evidence
    archive still holds. When the archive's coverage of a session has moved since its
    digests were written - retention purging entries (`evidence.purge`), or a session
    `aft forget` emptied - the rebuild would replace the fuller memory with a thinner one,
    so the session is named instead of rebuilt, and the person decides between
    re-archiving and letting it stand.
    """
    uncovered: list[tuple[str, str]] = []
    for session, digests in sorted(_unfiled_digests(config).items()):
        if evidence.session_info(config, session) is None:
            continue  # not archived at all: the backfill never sees it
        archived = {entry.id for entry in evidence.entries(config, session)}
        reason = _uncovered_reason(config, session, digests, archived)
        if reason is not None:
            uncovered.append((session, reason))
    return uncovered


def unfiled_sessions(config: Config) -> list[str]:
    """The session ids of every digest filed as unfiled the backfill can rebuild (DESIGN 7,
    F49, ``aft redigest --unfiled``).

    A session whose digests sit under ``digests/_unfiled`` is rebuilt when the archive
    still covers its digests part by part: every part stands on entries the archive holds,
    and a session `aft forget` emptied has no archive left, so it is not here. One whose
    coverage moved - retention purged entries, or a part cites none of its own session -
    is named by `uncovered_unfiled` instead, so a rebuild never replaces the fuller memory
    with a thinner one. The multipart parts of one session name it once.
    """
    backfillable: list[str] = []
    for session, digests in sorted(_unfiled_digests(config).items()):
        if evidence.session_info(config, session) is None:
            continue
        archived = {entry.id for entry in evidence.entries(config, session)}
        if _uncovered_reason(config, session, digests, archived) is None:
            backfillable.append(session)
    return backfillable


def redigest_estimate(config: Config, session_id: str) -> int | None:
    """The tokens one rebuild of this session would charge, or None with no archived entries.

    The estimate counts what the calls would cost, the way the cap is counted (DESIGN 13):
    each part's prompt and chunk at three characters to the token, plus the answer's own
    room. It is an estimate, not a promise: an answer may be shorter than its room, and a
    refused answer spends a retry.
    """
    entries = _redacted_entries(config, evidence.entries(config, session_id))
    if not entries:
        return None
    prompt = tokens(prompt_text())
    return sum(prompt + tokens(render(part)) + MAX_OUTPUT_TOKENS for part in parts(entries))
