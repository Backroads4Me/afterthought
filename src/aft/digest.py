"""The digest: one queued job in, one session digest page out (DESIGN 8, BUILD-PLAN M2.6).

A job is one chunk of one session. The digest reads the entries past that session's
watermark, hands what the filter keeps to the evidence archive and to the model, and keeps
only what a script can check: a fact stands when its quote is the words of the entry it
cites and, for ``stated``, when that entry is one of Ted's turns (DESIGN 8). The model
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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from importlib.resources import files
from pathlib import Path

import yaml

from aft import __version__, evidence, keys, model, pagewrite, queue, readers, vault
from aft import filter as filtermod
from aft import registry as registrymod
from aft.config import Config
from aft.gitutil import GitError, git
from aft.model import ModelError
from aft.page import KIND_SECTIONS
from aft.readers import USER, Entry

# The page type a digest carries (BUILD-PLAN M2.6).
PAGE_TYPE = "Session Digest"

PROMPT_FILE = "prompts/digest.md"
SCHEMA_FILE = "prompts/digest.schema.json"

# The per-chunk input cap of DESIGN 13, which the plan states as 30,000 input tokens.
MAX_CHUNK_TOKENS = 30_000
# Nothing in the build counts tokens (BUILD-PLAN 1.6 allows no new dependency and no
# tokenizer is vendored), so the cap is enforced on characters at four to a token, which
# is the usual reading of English prose and the same estimate the output cap uses.
CHARS_PER_TOKEN = 4
# The answer's own room, from the schema's limits at the same estimate: a summary of 800
# characters, a 400-character handoff and twenty facts of two 300-character fields plus
# four short ones are about 14,000 characters, and the round-up covers the JSON around it.
MAX_OUTPUT_TOKENS = 4_000

# A page's body is fact sections only (DESIGN 6), so a fact's kind is the heading it sits
# under and its scope has no field to live in; see ``_homes``.
SCOPES = frozenset({"project", "global"})
NO_HOME = "scope {scope} has no field on a digest page (DESIGN 6 line grammar)"


class DigestError(Exception):
    """A job this digest cannot honour: no project to file under, no transcript to read,
    a session id no path can be built from, or a page of this session's date that already
    stands and would be overwritten."""


class AnswerRefused(DigestError):
    """The endpoint answered this chunk and its answer failed the digest schema.

    The model's own words are what the job is left behind with and the endpoint is up,
    so this is one session's failure and the run moves on (DESIGN 14)."""


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
    """The chunk as the prompt is told to read it: ``[<id>] <ROLE> <time>`` and its text.

    The entry id leads each block because the schema asks the model to name the id each
    quote came from, and the role and time because the prompt distinguishes Ted's own
    commitments from the agent's, and dates a fact by when it was said.
    """
    return "\n\n".join(
        f"[{entry.id}] {entry.role.upper()} {entry.time.isoformat()}:\n{entry.text}"
        for entry in entries
    )


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

    A session's end is one digest, and one page (DESIGN 13: one digest per session per
    day, not per turn); what the cap bounds is one call, so a chunk over it is a session
    digested in parts and filed as one page. The cap is on the call's input, which is the
    prompt and the chunk together, so the prompt's own tokens come off the budget before
    the entries are divided.
    """
    return split_parts(entries, max(1, MAX_CHUNK_TOKENS - tokens(prompt_text())))


def digest_job(config: Config, job: queue.Job, now: datetime | None = None) -> Digest | None:
    """Digest one queued job, or report that it held nothing to digest.

    Reads, filter, archive, model, verify, page, watermark, commit, in that order: the
    archive takes the chunk before the model sees it, because the page's quotes are found
    in the archive, and the watermark moves only once the page stands in git or the chunk
    is known to hold nothing the filter would keep.

    Returns ``None`` when the job held nothing to digest, which closes it and files no
    page. A session with no entry past its watermark keeps the mark it had; a chunk the
    filter emptied moves the mark past the entries read, because those are decided and
    would be decided again by every later run.
    """
    _require_session_id(job.session_id)
    vault.require(config)
    registry = registrymod.Registry.load(config.registry_path)
    try:
        resolution = keys.resolve(job.cwd, registry, config.host)
    except GitError as exc:
        # The directory belongs to whatever ran the session and goes away with it, so git
        # failing there is this session's own loss. Git failing in the vault is the host's
        # and still stops the run.
        raise DigestError(f"{job.cwd} could not be read as a project: {exc}") from exc
    if resolution.slug is None:
        raise DigestError(
            f"{job.cwd} resolves to no project, so this session has nowhere to be filed"
        )
    if job.transcript_path is None:
        raise DigestError(f"job {job.id} names no transcript for session {job.session_id}")

    read = list(readers.read(job.agent, job.transcript_path, job.watermark))
    if not read:
        queue.complete(config, job, None)
        return None

    kept = filtermod.filter_entries(read, config)
    if not kept:
        # Nothing the filter left to digest: the entries were read and decided, so the
        # mark moves and the job closes. A page of nothing would stand for the day and
        # then refuse the chunk that follows it with real work on it.
        queue.complete(config, job, read[-1].id)
        return None

    evidence.store(
        config,
        job.agent,
        job.session_id,
        None if resolution.project is None else resolution.project.id,
        kept,
    )

    # The page's path is known before a token is spent on it, so a day whose digest already
    # stands is refused here - with the chunk archived and the watermark held - rather than
    # after every part of it has been paid for. _write checks once more before the commit.
    when = read[-1].time.astimezone(UTC).date()
    rel = _path(resolution.slug, job.agent, job.session_id, when)
    if (config.vault / rel).is_file():
        raise DigestError(_collision(rel))

    facts: list[Fact] = []
    dropped: list[Dropped] = []
    summaries: list[str] = []
    handoffs: list[dict[str, object]] = []
    prompt = prompt_text()
    schema = digest_schema()
    # One page per job, whatever the number of parts: the plan names one path per session
    # and day, and a handoff is what stood at the end of the part that read it, so the
    # last part's is the job's. Summaries are joined uncut - a page's description has no
    # cap, so nothing is truncated at the join, and a reader sees every part's own words.
    for part in parts(kept):
        answer = _model_answer(config, prompt, render(part), schema)
        part_facts, part_dropped = verify(config, job.session_id, part, answer["facts"])
        facts.extend(part_facts)
        dropped.extend(part_dropped)
        summaries.append(answer["summary"])
        handoffs.append(answer["handoff"])

    # A fact whose scope the page cannot carry is refused here, not written wrong.
    written, homeless = _homes(facts)
    dropped.extend(homeless)
    page, on_page, refused = _page(
        config,
        job,
        rel,
        resolution.slug,
        resolution,
        summaries,
        handoffs[-1],
        written,
        dropped,
        when,
        now or datetime.now(UTC),
    )
    dropped.extend(refused)
    _write(config, rel, page)
    # The watermark is the last entry read, not the last one kept: what the filter threw
    # away has been decided, and reading it again would decide it again (DESIGN 8).
    queue.complete(config, job, read[-1].id)
    return Digest(path=rel, facts=on_page, dropped=tuple(dropped), watermark=read[-1].id)


def _model_answer(config: Config, prompt: str, chunk: str, schema: dict) -> dict:
    """One digest call and its answer, with a refused answer told as this job's failure.

    An answer that failed its schema twice is this chunk's own problem: the job is left
    behind with the model's reason, its watermark stays where it was, and the next session
    gets its turn. Anything else aft.model raises is the endpoint or its setup, which
    every job left would meet alike, so it stays a ModelError and the run stops with the
    queue kept for a later attempt (DESIGN 14).
    """
    try:
        return model.complete_json(config, prompt, chunk, schema, MAX_OUTPUT_TOKENS)
    except ModelError as exc:
        if exc.__cause__ is None and ANSWER_REFUSED in str(exc):
            raise AnswerRefused(str(exc)) from exc
        raise


def verify(
    config: Config, session_id: str, part: Sequence[Entry], facts: Sequence[dict]
) -> tuple[list[Fact], list[Dropped]]:
    """The facts of one part that pass the design's checks, and the refusals beside them.

    Each check is the design's (DESIGN 8, and the kinds and scopes the plan's schema
    enumerates), and the first one a fact fails is the reason it is recorded by. Nothing
    here judges what a fact is worth: that is the prompt's job, and a script that decided
    value as well would put its own taste into memory.
    """
    by_id = {entry.id: entry for entry in part}
    kept: list[Fact] = []
    dropped: list[Dropped] = []
    for fact in facts:
        entry = by_id.get(fact["entry"])
        if entry is None:
            dropped.append(_refused(fact, f"entry {fact['entry']} is not an entry of this chunk"))
            continue
        if not evidence.find_quote(config, session_id, entry.id, fact["quote"]):
            dropped.append(_refused(fact, f"the quote is not found in entry {entry.id}"))
            continue
        if fact["source"] == "stated" and entry.role != USER:
            dropped.append(
                _refused(fact, f"[stated] cites entry {entry.id}, which is not one of Ted's turns")
            )
            continue
        if fact["kind"] not in KIND_SECTIONS.values():
            dropped.append(_refused(fact, f"unknown kind {fact['kind']!r}"))
            continue
        if fact["scope"] not in SCOPES:
            dropped.append(_refused(fact, f"unknown scope {fact['scope']!r}"))
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


def _homes(facts: Sequence[Fact]) -> tuple[list[Fact], list[Dropped]]:
    """The facts this page can hold, and the ones it has no field for.

    A line's kind is its section, so a project fact arrives whole. ``scope: global`` says
    a fact applies in every project, and no field of the design's page format carries that
    (DESIGN 6), so a global fact written here would reach the next session as a project's
    own. Each is refused on its own line of the same record that carries the verifier's
    refusals, naming its statement, its scope and the entry it came from, so "the model
    found nothing global" and "there was nowhere to write it" cannot read alike. The page
    format is the plan author's to settle.
    """
    homeless = [fact for fact in facts if fact.scope != "project"]
    dropped = [
        Dropped(
            statement=fact.statement,
            kind=fact.kind,
            source=fact.source,
            entry=fact.entry,
            reason=NO_HOME.format(scope=fact.scope),
        )
        for fact in homeless
    ]
    return [fact for fact in facts if fact.scope == "project"], dropped


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


def _path(slug: str, agent: str, session_id: str, when: date) -> str:
    """The page's path in the vault, as DESIGN 6 lays it out."""
    return f"digests/{slug}/{when:%Y}/{when:%m}/{when:%Y-%m-%d}-{agent}-{session_id}.md"


def _page(
    config: Config,
    job: queue.Job,
    rel: str,
    slug: str,
    resolution: keys.Resolution,
    summaries: Sequence[str],
    handoff: dict,
    facts: Sequence[Fact],
    dropped: Sequence[Dropped],
    when: date,
    stamp: datetime,
) -> tuple[str, tuple[Fact, ...], tuple[Dropped, ...]]:
    """The digest page: the frontmatter, then the fact lines the script kept.

    Returns the page's text, the facts that made it onto the page, and any the page
    writer refused - a statement it cannot write as one line is a fact this digest did
    not write, and it belongs in the same record as the verifier's refusals rather than
    in prose, which DESIGN 6 does not allow on a fact page at all.

    The frontmatter is written whole because this page is new; the fact lines and the
    sources they cite go through the page writer, which refuses text its own parse would
    not read back, so a page that leaves here lints (DESIGN 6).
    """
    frontmatter = _frontmatter(config, resolution, slug, summaries, handoff, dropped, when, stamp)
    text, written, refused = _written(frontmatter, rel, job, facts)
    if refused:
        # The refusals join the record of dropped facts, which means writing the page
        # once more over the lines that did land - and the same lines refuse again.
        frontmatter = _frontmatter(
            config, resolution, slug, summaries, handoff, [*dropped, *refused], when, stamp
        )
        text, written, _ = _written(frontmatter, rel, job, facts)
    return text, tuple(written), tuple(refused)


def _frontmatter(
    config: Config,
    resolution: keys.Resolution,
    slug: str,
    summaries: Sequence[str],
    handoff: dict,
    dropped: Sequence[Dropped],
    when: date,
    stamp: datetime,
) -> dict:
    """The page's OKF frontmatter, with afterthought's own fields under one ``aft`` key."""
    aft: dict[str, object] = {}
    if resolution.project is not None:
        aft["project"] = resolution.project.id
    aft["handoff"] = {
        "in_flight": handoff["in_flight"],
        "failed": list(handoff["failed"]),
        "open": list(handoff["open"]),
    }
    if dropped:
        aft["dropped"] = [_record(fact) for fact in dropped]
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
    job: queue.Job,
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
            _add(doc, job, fact)
        except pagewrite.WriteError as exc:
            doc = pagewrite.PageDoc(before, rel)
            refused.append(_refused(_words(fact), str(exc)))
        else:
            written.append(fact)
    return doc.render(), written, refused


def _add(doc: pagewrite.PageDoc, job: queue.Job, fact: Fact) -> None:
    """One fact onto the page, with the source its line cites (DESIGN 6).

    The source names the session and the entry the quote came from, so `aft trace` and
    the dream's quote check both find the words again where the page says they are.
    """
    source = doc.add_source(
        f"aft://session/{job.agent}/{job.session_id}#{fact.entry}",
        title=f"{job.agent} session {job.session_id}",
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


def _words(fact: Fact) -> dict:
    """A verified fact in the shape a refusal is recorded from."""
    return {
        "statement": fact.statement,
        "kind": fact.kind,
        "source": fact.source,
        "entry": fact.entry,
    }


def _title(resolution: keys.Resolution, slug: str) -> str:
    return resolution.project.title if resolution.project is not None else slug


def _write(config: Config, rel: str, text: str) -> Path:
    """Put the page in the vault and commit it, or leave the vault exactly as it was.

    An existing page at the path stops the job rather than being replaced: the plan gives
    one path per session per day and the design has a chunk of a long session digested
    more than once a day, so the second chunk of a day would otherwise overwrite the first
    digest whole. Refusing keeps both, holds the watermark, and says so on its own line of
    the failure, which is what a doomed retry has to leave behind for a person to end it.

    The commit is the last step. A page the commit did not take is taken back out on the
    way as well: the file goes, and so does the index entry that staging put beside it,
    because a staged page is a page the next commit of anything at all would carry into
    memory. So a failure leaves no page standing that the watermark does not know about.
    """
    path = config.vault / rel
    if path.is_file():
        raise DigestError(_collision(rel))
    path.parent.mkdir(parents=True, exist_ok=True)
    # A page is staged through a temporary in the directory that will hold it. The suffix is
    # not ``.md`` because a run killed before the rename leaves the file behind, and a
    # page-shaped file there is one `aft lint` reports and `aft inject` may take for a digest.
    handle, tmp = tempfile.mkstemp(dir=path.parent, prefix=".digest-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(tmp, path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise
    with vault.locked(config):
        try:
            vault.commit(config.vault, [rel], f"aft: digest {rel}")
        except (GitError, vault.VaultError):
            # The page went into git's index before the commit refused it, and a staged
            # page is taken by whichever commit comes next, so the index loses it with the
            # file - and both go under the lock the commit was attempted under.
            path.unlink(missing_ok=True)
            git(["reset", "-q", "--", rel], cwd=config.vault, check=False)
            raise
    return path


def _collision(rel: str) -> str:
    """What a collision reports: three lines, because one failure text is a queue's row."""
    return "\n".join(
        (
            f"a digest page already stands at {rel}",
            "the plan gives a session one digest path per day (BUILD-PLAN M2.6), so this "
            "chunk has no page of its own to write",
            "the chunk is archived and the watermark holds, so nothing is lost while "
            "this page's identity is settled",
        )
    )
