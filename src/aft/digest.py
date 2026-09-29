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

The watermark moves last and only with the page: a page that was not written leaves the
session's read position where it was, because a watermark that runs ahead of its page
loses the session quietly and looks exactly like success (DESIGN 14).
"""

from __future__ import annotations

import json
import math
import os
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
from aft.gitutil import GitError
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
    or a page of this session's date that already stands and would be overwritten."""


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
    in the archive, and the watermark moves only once the page stands in git.

    Returns ``None`` when the session has no entry past its watermark, which closes the
    job and leaves the watermark where it was: there was nothing to digest, so there is
    nothing to file and nothing new to read.
    """
    vault.require(config)
    resolution = keys.resolve(job.cwd, registrymod.Registry.load(config.registry_path), config.host)
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
    evidence.store(
        config,
        job.agent,
        job.session_id,
        None if resolution.project is None else resolution.project.id,
        kept,
    )

    facts: list[Fact] = []
    dropped: list[Dropped] = []
    summaries: list[str] = []
    handoff: dict[str, object] = {"in_flight": "", "failed": [], "open": []}
    prompt = prompt_text()
    schema = digest_schema()
    # One page per job, whatever the number of parts: the plan names one path per session
    # and day, and a handoff is what stood at the end of the part that read it, so the
    # last part's is the job's. Summaries are joined uncut - a page's description has no
    # cap, so nothing is truncated at the join, and a reader sees every part's own words.
    for part in parts(kept):
        answer = model.complete_json(config, prompt, render(part), schema, MAX_OUTPUT_TOKENS)
        part_facts, part_dropped = verify(config, job.session_id, part, answer["facts"])
        facts.extend(part_facts)
        dropped.extend(part_dropped)
        summaries.append(answer["summary"])
        handoff = answer["handoff"]

    # A fact whose scope the page cannot carry is refused here, not written wrong.
    written, homeless = _homes(facts)
    dropped.extend(homeless)
    when = read[-1].time.astimezone(UTC).date()
    rel = _path(resolution.slug, job.agent, job.session_id, when)
    page, on_page, refused = _page(
        config,
        job,
        resolution.slug,
        resolution,
        summaries,
        handoff,
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
    own. It is refused instead, with the count and the reason in the same record that
    carries the verifier's refusals, so "the model found nothing global" and "there was
    nowhere to write it" cannot read alike. The page format is the plan author's to settle.
    """
    homeless = [fact for fact in facts if fact.scope != "project"]
    count = len(homeless)
    dropped = [
        Dropped(
            statement=fact.statement,
            kind=fact.kind,
            source=fact.source,
            entry=fact.entry,
            reason=(
                NO_HOME.format(scope=fact.scope)
                + f"; {count} of this digest's facts were dropped for it"
            ),
        )
        for fact in homeless
    ]
    return [fact for fact in facts if fact.scope == "project"], dropped


def _path(slug: str, agent: str, session_id: str, when: date) -> str:
    """The page's path in the vault, as DESIGN 6 lays it out."""
    return f"digests/{slug}/{when:%Y}/{when:%m}/{when:%Y-%m-%d}-{agent}-{session_id}.md"


def _page(
    config: Config,
    job: queue.Job,
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
    rel = _path(slug, job.agent, job.session_id, when)
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

    The commit is the last step. A page the commit did not take is removed on the way out,
    so a failure never leaves a page standing that the watermark does not know about.
    """
    path = config.vault / rel
    if path.is_file():
        raise DigestError(_collision(rel))
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=path.parent, prefix=".digest-", suffix=".md")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(tmp, path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise
    try:
        with vault.locked(config):
            vault.commit(config.vault, [rel], f"aft: digest {rel}")
    except (GitError, vault.VaultError):
        path.unlink(missing_ok=True)
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
